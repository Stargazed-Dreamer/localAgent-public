#!/usr/bin/env python3
"""明日方舟寻访记录采集脚本 v4

通过 Playwright + CDP 操控浏览器获取寻访记录。
v4 优化：用 page.route 将 API 的 size=10 改为 size=50，翻页次数减少 5 倍。

v3 → v4 变更：
- 每页数据量：10条 → 50条（翻页次数减少 5 倍，速度大幅提升）
- 登录方式：自动填写密码/验证码 → 用户在浏览器中手动登录（更安全，不怕人机验证）
- 翻页间隔：1.2秒 → 0.5秒（API 直接返回数据，无需等待页面渲染）

API 结构：
- 分类: GET /user/api/inquiry/gacha/cate?uid={uid}
  → [{id: "anniver_fest", name: "限定寻访\\n庆典"}, ...]
- 记录: GET /user/api/inquiry/gacha/history?uid={uid}&category={catId}&size=50
  → {code:0, data:{list:[{poolId, poolName, charId, charName, rarity(2-5), isNew, gachaTs(ms), pos}], hasMore:bool}}

注意：明日方舟 API 不支持直接 HTTP 调用（需要浏览器认证 cookie），
必须通过页面操作触发请求，用 page.on("response") 拦截获取数据。

用法：
    python workspace/arknights_gacha/arknights_gacha.py                # 采集所有记录（默认增量）
    python workspace/arknights_gacha/arknights_gacha.py --force        # 重新采集官网全量并与历史合并
    python workspace/arknights_gacha/arknights_gacha.py --token TOKEN  # 直接使用已有 token（跳过登录，仍需浏览器）
"""

import asyncio
import json
import random
import sys
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import async_playwright

CDP_PORT = 9222

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from arknights_import import build_pool_registry

# 输出目录
OUTPUT_DIR = PROJECT_ROOT / "workspace" / "arknights_gacha"
RAW_DIR = OUTPUT_DIR / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

# 官网页面
AK_HOME_URL = "https://ak.hypergryph.com/user/home"
AK_GACHA_URL = "https://ak.hypergryph.com/user/headhunting?menu=1"

# 每页数据量（v3 是 10，v4 改为 50，减少翻页次数）
PAGE_SIZE = 50

# 稀有度映射（API返回2-5，对应3-6星）
RARITY_MAP = {2: "★3", 3: "★4", 4: "★5", 5: "★6"}


def _atomic_write_json(path: Path, data) -> None:
    """先写完同目录临时文件，再原子替换目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
    temp_path.replace(path)


def _record_key(record: dict) -> tuple:
    """跨官网/小黑盒统一唯一键，pos 区分同秒同名的十连结果。"""
    try:
        timestamp = int(float(record.get("timestamp", 0) or 0))
    except (TypeError, ValueError):
        timestamp = 0
    try:
        pos = int(record.get("pos", 0) or 0)
    except (TypeError, ValueError):
        pos = 0
    return (
        timestamp,
        str(record.get("pool", "")),
        str(record.get("name", "")),
        pos,
        int(record.get("rarity", 0) or 0),
        bool(record.get("is_new", False)),
    )


def _prefer_record(first: dict, second: dict) -> dict:
    """合并同一抽的跨来源副本，优先保留字段更完整的官网记录。"""
    def score(record: dict) -> tuple:
        source_score = 2 if record.get("source") == "official" else 1 if record.get("source") else 0
        populated = sum(value not in (None, "", [], {}) for value in record.values())
        return source_score, populated

    preferred, other = (first, second) if score(first) >= score(second) else (second, first)
    merged = dict(preferred)
    for key, value in other.items():
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


async def _has_visible_login_prompt(page) -> bool:
    """只检查可见登录控件，忽略页面中常驻的隐藏登录模板。"""
    buttons = page.get_by_role("button", name="登录", exact=True)
    for index in range(await buttons.count()):
        if await buttons.nth(index).is_visible():
            return True
    return "前往登录" in await page.evaluate("() => document.body.innerText")


# ========== 登录相关 ==========

async def login_browser(page) -> str | None:
    """通过浏览器登录鹰角通行证，拦截认证 token"""
    auth_match = "as.hypergryph.com/user/auth"
    poll_url = "https://web-api.hypergryph.com/account/info/hg"

    captured_token = None

    inject_script = f"""
    (function() {{
        var hasCaptured = false;
        window.__ak_captured_token = null;

        var originalOpen = XMLHttpRequest.prototype.open;
        var originalSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url) {{
            this._url = url;
            return originalOpen.apply(this, arguments);
        }};
        XMLHttpRequest.prototype.send = function(body) {{
            this.addEventListener('load', function() {{
                try {{
                    if (this._url && this._url.includes('{auth_match}')) {{
                        var res = JSON.parse(this.responseText);
                        if (res.status === 0 && res.data && res.data.token) {{
                            if (!hasCaptured) {{
                                hasCaptured = true;
                                window.__ak_captured_token = res.data.token;
                            }}
                        }}
                    }}
                }} catch (e) {{}}
            }});
            return originalSend.apply(this, arguments);
        }};

        var originalFetch = window.fetch;
        window.fetch = async function(...args) {{
            const response = await originalFetch(...args);
            try {{
                const url = response.url;
                if (url && url.includes('{auth_match}')) {{
                    const clone = response.clone();
                    clone.json().then(data => {{
                        if (data.status === 0 && data.data && data.data.token) {{
                            if (!hasCaptured) {{
                                hasCaptured = true;
                                window.__ak_captured_token = data.data.token;
                            }}
                        }}
                    }}).catch(e => {{}});
                }}
            }} catch (e) {{}}
            return response;
        }};

        var timer = setInterval(function() {{
            if (hasCaptured) {{ clearInterval(timer); return; }}
            fetch('{poll_url}', {{
                method: 'GET',
                credentials: 'include'
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.code === 0 && data.data && data.data.content) {{
                    if (!hasCaptured) {{
                        hasCaptured = true;
                        window.__ak_captured_token = data.data.content;
                        clearInterval(timer);
                    }}
                }}
            }})
            .catch(e => {{}});
        }}, 1500);
    }})();
    """

    await page.evaluate(inject_script)

    print("  >>> 请在浏览器中登录鹰角通行证（手机号+验证码/密码/扫码）<<<")
    print("  >>> 登录成功后将自动获取 token <<<\n")

    for i in range(180):
        await asyncio.sleep(2)
        try:
            token = await page.evaluate("window.__ak_captured_token")
            if token:
                captured_token = token
                print("  已获取到认证 token！")
                break
        except Exception:
            pass
        if i % 15 == 14:
            print("  等待登录中...")

    return captured_token


# ========== 数据采集 ==========

async def _get_categories(page) -> list:
    """获取页面上的卡池分类列表"""
    return await page.evaluate("""
        () => {
            const items = document.querySelectorAll('div.LbzcSW');
            const result = [];
            for (const item of items) {
                const rect = item.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    result.push({
                        text: item.innerText?.trim(),
                        x: rect.x + rect.width / 2,
                        y: rect.y + rect.height / 2
                    });
                }
            }
            return result;
        }
    """)


async def _restore_window_via_cdp(page) -> None:
    """把浏览器窗口恢复 normal 并移到主屏可见区（采集的前提）。

    窗口挂在副屏/最小化时 Chromium occlusion tracker 会挂起渲染，
    getBoundingClientRect 返回全 0 → 可见性过滤把分类全丢掉 → 误判"未登录"。
    tab 级 bring_to_front 解不了窗口级挂起，必须用 CDP 窗口级恢复。
    """
    try:
        cdp = await page.context.new_cdp_session(page)
        win = await cdp.send("Browser.getWindowForTarget")
        window_id = win["windowId"]
        await cdp.send(
            "Browser.setWindowBounds",
            {"windowId": window_id, "bounds": {"windowState": "normal"}},
        )
        await cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"left": 60, "top": 60, "width": 1440, "height": 950},
            },
        )
        print("  已将浏览器窗口恢复 normal 并移至主屏（副屏窗口渲染被挂起）")
    except Exception as e:  # noqa: BLE001
        print(f"  CDP 窗口恢复失败（继续重试）: {e}")


async def _wait_categories(page, timeout_s: float = 25.0) -> list:
    """轮询等待寻访分类渲染完成。

    SPA 渲染可能远超固定 sleep(3)；页面刷新/重载后分类出现耗时波动大，
    轮询（每秒一次，最多 timeout_s）代替单次查询，避免把"渲染中"误判为"未登录"。
    """
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    restored = False
    while True:
        cats = await _get_categories(page)
        if cats:
            return cats
        # DOM 里有分类但 rect 全 0：窗口渲染被挂起（副屏/最小化），CDP 恢复窗口
        dom_count = await page.evaluate("() => document.querySelectorAll('div.LbzcSW').length")
        if dom_count and not restored:
            print("  分类在 DOM 中但不可见（窗口渲染被挂起）")
            await _restore_window_via_cdp(page)
            restored = True
        if loop.time() > deadline:
            return []
        await _asyncio.sleep(1)


async def _find_next_page_btn(page) -> dict | None:
    """查找'下一页'按钮的位置和状态"""
    return await page.evaluate("""
        () => {
            const divs = document.querySelectorAll('div');
            for (const d of divs) {
                if (d.innerText?.trim() === '下一页') {
                    const r = d.getBoundingClientRect();
                    const parent = d.parentElement;
                    const isDisabled = (parent?.className?.includes('disabled') || false) ||
                                       d.className?.includes('disabled') ||
                                       r.width === 0;
                    return {
                        x: r.x + r.width / 2,
                        y: r.y + r.height / 2,
                        w: r.width,
                        disabled: isDisabled
                    };
                }
            }
            return null;
        }
    """)


async def collect_category(page, cat_x: float, cat_y: float, cat_name: str, max_pages: int = 200) -> list:
    """采集单个分类的全部数据（翻页+拦截响应，size=50）"""
    print(f"\n  [{cat_name}]")

    responses = []
    response_event = asyncio.Event()
    response_errors = []

    async def on_response(response):
        url = response.url
        if "inquiry/gacha/history" in url:
            try:
                body = await response.json()
                if body.get("code") != 0:
                    response_errors.append(f"code={body.get('code')} msg={body.get('message', '')}")
                    response_event.set()
                    return
                data = body.get("data", {})
                lst = data.get("list", [])
                has_more = data.get("hasMore")
                if not isinstance(lst, list):
                    response_errors.append("API data.list 不是列表")
                else:
                    responses.append({"list": lst, "hasMore": bool(has_more)})
            except Exception as exc:
                response_errors.append(str(exc))
            finally:
                response_event.set()

    page.on("response", on_response)

    async def wait_for_history_response() -> None:
        try:
            await asyncio.wait_for(response_event.wait(), timeout=15)
        except TimeoutError as exc:
            raise RuntimeError(f"{cat_name} 点击后 15 秒内没有 history API 响应") from exc
        if response_errors:
            raise RuntimeError(f"{cat_name} history API 异常: {response_errors[-1]}")

    page_num = 0
    try:
        response_event.clear()
        await page.mouse.click(cat_x, cat_y)
        await wait_for_history_response()
        page_num = 1

        while responses[-1].get("hasMore"):
            if page_num >= max_pages:
                raise RuntimeError(f"{cat_name} 达到最大页数 {max_pages}，仍有下一页")

            next_btn = await _find_next_page_btn(page)
            if not next_btn or next_btn.get("disabled") or next_btn.get("w", 0) == 0:
                raise RuntimeError(f"{cat_name} API 标记 hasMore=true，但页面没有可用的下一页按钮")

            response_event.clear()
            await page.mouse.click(next_btn["x"], next_btn["y"])
            await wait_for_history_response()
            page_num += 1

            if page_num % 10 == 0:
                total = sum(len(r["list"]) for r in responses)
                print(f"    已翻{page_num}页, {total}条")
    finally:
        page.remove_listener("response", on_response)

    records = []
    for resp in responses:
        records.extend(resp["list"])

    if not records:
        print("    暂无数据")
    print(f"    {len(records)}条 (翻{page_num}页)")
    return records


async def fetch_gacha_records(page) -> list:
    """获取全部寻访记录（v4: size=50 优化版）"""
    print("[2/3] 获取寻访记录...")

    # 用 route 将 size=10 改为 size=50
    async def enlarge_page_size(route):
        url = route.request.url
        if "inquiry/gacha/history" in url:
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["size"] = str(PAGE_SIZE)
            enlarged = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            await route.continue_(url=enlarged)
        else:
            await route.continue_()

    await page.route("**/inquiry/gacha/**", enlarge_page_size)

    # 确保在寻访记录页面
    if "headhunting" not in page.url:
        await page.goto(AK_GACHA_URL, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(3)

    # 刷新页面获取初始数据
    print("  刷新页面...")
    try:
        await page.reload(wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(3)

    # 获取分类
    categories = await _get_categories(page)
    if not categories:
        await page.unroute("**/inquiry/gacha/**")
        if await _has_visible_login_prompt(page):
            raise RuntimeError("登录态失效：寻访页显示登录提示")
        raise RuntimeError("未找到卡池分类，页面结构可能已变化或尚未加载完成")

    print(f"  分类: {[c['text'] for c in categories]}")

    # 逐分类采集
    all_records = []
    # 默认第一类通常已选中；先点其他类再回到第一类，确保每次点击都会发请求。
    ordered_categories = categories[1:] + categories[:1] if len(categories) > 1 else categories
    try:
        for cat in ordered_categories:
            records = await collect_category(page, cat['x'], cat['y'], cat['text'])
            all_records.extend(records)
    finally:
        await page.unroute("**/inquiry/gacha/**")

    # 去重
    seen = set()
    unique = []
    for rec in all_records:
        key = (rec.get("poolId"), rec.get("gachaTs"), rec.get("charId"), rec.get("pos"))
        if key not in seen:
            seen.add(key)
            unique.append(rec)

    if len(unique) < len(all_records):
        print(f"  去重: {len(all_records)} → {len(unique)}")

    return unique


# ========== 数据处理 ==========

def process_records(records: list) -> list:
    """处理原始API记录，转换为统一格式"""
    processed = []
    for rec in records:
        gacha_ts = rec.get("gachaTs", "0")
        try:
            ts_sec = int(gacha_ts) / 1000
        except (ValueError, TypeError):
            ts_sec = 0

        processed.append({
            "timestamp": ts_sec,
            "time": datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S") if ts_sec else "",
            "poolId": rec.get("poolId", ""),
            "pool": rec.get("poolName", ""),
            "charId": rec.get("charId", ""),
            "name": rec.get("charName", ""),
            "rarity": rec.get("rarity", 0),
            "rarity_label": RARITY_MAP.get(rec.get("rarity", 0), "?"),
            "is_new": rec.get("isNew", False),
            "pos": rec.get("pos", 0),
            "source": "official",
        })

    processed.sort(key=lambda x: x["timestamp"], reverse=True)
    return processed


def generate_summary(records: list) -> dict:
    """生成统计摘要"""
    if not records:
        return {"total": 0}

    rarity_count = {}
    six_star = []
    for rec in records:
        r = rec["rarity_label"]
        rarity_count[r] = rarity_count.get(r, 0) + 1
        if rec["rarity"] == 5:
            six_star.append(rec["name"])

    pool_count = {}
    for rec in records:
        pool = rec["pool"] or "未知卡池"
        pool_count[pool] = pool_count.get(pool, 0) + 1

    return {
        "total": len(records),
        "rarity_count": rarity_count,
        "six_star_count": len(six_star),
        "six_star_names": six_star,
        "pool_count": pool_count,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def print_summary(summary: dict):
    """打印统计摘要"""
    print("\n" + "=" * 50)
    print("  明日方舟寻访记录统计")
    print("=" * 50)
    print("  总抽数: %d" % summary["total"])

    if summary["total"] == 0:
        return

    print("\n  稀有度分布:")
    for r in ["★6", "★5", "★4", "★3"]:
        count = summary["rarity_count"].get(r, 0)
        if count > 0:
            pct = count / summary["total"] * 100
            print("    %s: %d (%.1f%%)" % (r, count, pct))

    if summary["six_star_names"]:
        print("\n  六星干员: %s" % ", ".join(summary["six_star_names"]))

    print("\n  卡池分布:")
    for pool, count in sorted(summary["pool_count"].items(), key=lambda x: -x[1]):
        print("    %s: %d抽" % (pool, count))

    print("=" * 50)


def incremental_update(new_records: list) -> list:
    """增量更新：合并新记录到已有数据"""
    db_path = OUTPUT_DIR / "gacha_records.json"

    if db_path.exists():
        try:
            with open(db_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取已有数据库 {db_path}: {exc}") from exc
        if not isinstance(existing, list):
            raise RuntimeError(f"已有数据库格式异常（应为列表）: {db_path}")
    else:
        existing = []

    # 先修复旧版本因毫秒/秒时间戳差异留下的跨来源重复。
    deduped = []
    index_by_key = {}
    removed_duplicates = 0
    for rec in existing:
        key = _record_key(rec)
        if key in index_by_key:
            index = index_by_key[key]
            deduped[index] = _prefer_record(deduped[index], rec)
            removed_duplicates += 1
        else:
            index_by_key[key] = len(deduped)
            deduped.append(rec)
    existing = deduped

    added = 0
    for rec in new_records:
        key = _record_key(rec)
        if key not in index_by_key:
            index_by_key[key] = len(existing)
            existing.append(rec)
            added += 1
        else:
            index = index_by_key[key]
            existing[index] = _prefer_record(existing[index], rec)

    existing.sort(key=lambda x: x["timestamp"], reverse=True)

    if removed_duplicates:
        print("  历史去重: 合并 %d 条跨来源重复记录" % removed_duplicates)
    print("  增量更新: 新增 %d 条，总计 %d 条" % (added, len(existing)))
    return existing


# ========== 主流程 ==========

async def main_async(force: bool = False, token: str = ""):
    """主流程"""
    print("[1/3] 登录鹰角通行证...")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = browser.contexts[0]

        # 找到或打开鹰角页面：优先 ak 主站页面，排除 protocol 等子域遗留 tab 的干扰
        page = None
        for pg in context.pages:
            if 'ak.hypergryph.com' in pg.url:
                page = pg
                break
        if not page:
            for pg in context.pages:
                if 'hypergryph.com' in pg.url:
                    page = pg
                    break

        if not page:
            page = await context.new_page()
            await page.goto(AK_HOME_URL, wait_until="domcontentloaded", timeout=30000)

        print(f"  当前页面: {page.url}")

        # 窗口挂在副屏/被遮挡时 Chromium 会挂起渲染（rect 全 0），点击坐标与可见性过滤都会失效，
        # 先把 tab 激活到前台
        await page.bring_to_front()

        # 登录态由"寻访分类成功加载"确认（SKILL.md 关键规则的最终证据）；
        # 轮询等待渲染，页面刷新/重载后分类出现耗时波动大，固定 sleep 会误判。
        if "headhunting" not in page.url:
            await page.goto(AK_GACHA_URL, wait_until="domcontentloaded", timeout=30000)
        categories = await _wait_categories(page)
        is_logged_in = bool(categories)

        if not is_logged_in:
            print("  未检测到有效登录态（分类未加载），需要在浏览器中完成登录")
            auth_token = await login_browser(page)
            if not auth_token:
                print("  登录超时或失败，退出")
                raise RuntimeError("登录超时或失败")
            if "headhunting" not in page.url:
                await page.goto(AK_GACHA_URL, wait_until="domcontentloaded", timeout=30000)
            categories = await _wait_categories(page)
            if not categories:
                raise RuntimeError("登录后仍未加载出寻访分类")
        else:
            print(f"  已确认登录：寻访分类已加载（{len(categories)} 个）")

        # 获取寻访记录
        raw_records = await fetch_gacha_records(page)

        if not raw_records:
            raise RuntimeError("所有分类均未获取到记录，拒绝写入空结果")

        # 保存原始数据
        raw_path = RAW_DIR / ("raw_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        _atomic_write_json(raw_path, raw_records)
        print("\n  原始数据已保存: %s" % raw_path.name)

        # 处理记录
        print("[3/3] 处理数据...")
        processed = process_records(raw_records)

        # 增量更新
        if force:
            print("  强制模式: 已重新采集官网可见全量，但仍与历史库合并，不替换历史")
        all_records = incremental_update(processed)

        # 保存完整数据
        db_path = OUTPUT_DIR / "gacha_records.json"
        _atomic_write_json(db_path, all_records)

        pool_registry = build_pool_registry(all_records)
        _atomic_write_json(OUTPUT_DIR / "pool_registry.json", pool_registry)

        # 生成并打印统计
        summary = generate_summary(all_records)
        summary_path = OUTPUT_DIR / "summary.json"
        _atomic_write_json(summary_path, summary)

        print_summary(summary)


def main():
    parser = argparse.ArgumentParser(description="明日方舟寻访记录采集 v4")
    parser.add_argument("--force", action="store_true", help="强制重新采集官网可见全量（仍与历史库合并）")
    parser.add_argument("--token", type=str, default="", help="预留参数（明日方舟API不支持直接token调用）")
    args = parser.parse_args()

    asyncio.run(main_async(force=args.force, token=args.token))


if __name__ == "__main__":
    main()
