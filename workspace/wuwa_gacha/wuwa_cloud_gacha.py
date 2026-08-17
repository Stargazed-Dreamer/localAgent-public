"""
鸣潮抽卡记录采集 - 云游戏方式

通过云游戏网页入口（mc.kurogames.com/cloud）获取认证 URL，调用官方 API 获取抽卡记录，
增量合并到与 wuwa_gacha.py 共用的数据库。

与 wuwa_gacha.py（日志解密/内存扫描）相比：
- 优点：无需启动游戏客户端、无需管理员权限、无需解密日志
- 限制：依赖调试 Chrome 登录态、record_id 约1小时过期需重新点击刷新

前置条件：
1. 启动调试 Chrome（CDP 端口 9222）：uv run python tools/browser/start_debug_chrome.py
2. 在调试 Chrome 中打开 https://mc.kurogames.com/cloud/index.html#/tools 并登录库洛账号

工作流程：
1. 连接调试 Chrome（CDP）
2. 加载云游戏工具页，检查登录态（通行证ID）
3. 点击"唤取记录"卡片（通过 JS dispatch 事件到父元素，绕过 SVG 点击限制）
4. 从 iframe.webview-core.src 提取完整认证 URL
5. 解析参数（与游戏内 URL 格式相同，可复用 parse_gacha_url）
6. 调用现有 API 获取抽卡记录（与 wuwa_gacha.py 共用 fetch_gacha_records）
7. 增量合并到 wuwa_gacha_database.json，原始数据保存到 raw/
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

# 复用 wuwa_gacha.py 的函数和常量（parse_gacha_url 完全兼容 iframe URL 格式）
sys.path.insert(0, str(Path(__file__).parent))
from wuwa_gacha import (
    parse_gacha_url,
    fetch_gacha_records,
    load_database,
    save_database,
    save_raw,
    build_existing_counts,
    merge_records,
    generate_summary,
    print_summary,
    _atomic_write_json,
    CARD_POOL_TYPES,
    OUTPUT_DIR,
    REQUEST_INTERVAL,
)

# ============ 配置 ============

CLOUD_URL = "https://mc.kurogames.com/cloud/index.html#/tools"
CDP_ENDPOINT = "http://127.0.0.1:9222"


# ============ 云游戏提取认证 URL ============

async def find_gacha_url_from_cloud() -> str | None:
    """
    通过云游戏网页提取抽卡认证 URL

    流程：
    1. 连接 CDP（要求调试 Chrome 已启动并已登录库洛账号）
    2. 加载云游戏工具页，检查登录态
    3. 点击"唤取记录"卡片（JS dispatch 到父元素，因为 SVG text 不能直接点击）
    4. 从 iframe.webview-core.src 提取认证 URL
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("[错误] 未安装 playwright，请运行: uv add playwright")
        return None

    print("[云游戏] 连接调试 Chrome (CDP 9222)...")
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)
        except Exception as e:
            print(f"[错误] 无法连接 CDP: {e}")
            print("  请先启动调试 Chrome: uv run python tools/browser/start_debug_chrome.py")
            return None

        ctx = browser.contexts[0] if browser.contexts else None
        if ctx is None:
            print("[错误] 调试 Chrome 无 context")
            return None

        # 优先复用已打开的云游戏 tab，否则新建
        page = next((pg for pg in ctx.pages if 'kurogames' in pg.url), None)
        if page is None:
            page = await ctx.new_page()
            print("[云游戏] 新建 tab")

        # 1. 加载云游戏工具页
        print("[云游戏] 加载页面...")
        try:
            await page.goto(CLOUD_URL, wait_until='domcontentloaded', timeout=30000)
        except Exception as e:
            print(f"[错误] 页面加载失败: {e}")
            return None
        await asyncio.sleep(4)

        # 2. 检查登录态
        body_text = await page.evaluate('document.body.innerText')
        m = re.search(r'通行证ID:\s*(\d+)', body_text)
        if m:
            print(f"[云游戏] 已登录 (通行证ID: {m.group(1)})")
        elif '登录' in body_text or '登入' in body_text:
            print("[错误] 未登录！请在调试 Chrome 中手动登录库洛账号")
            print(f"  入口: {CLOUD_URL}")
            return None
        else:
            print("[警告] 登录态不明确，继续尝试...")
            print(f"  body 前 200 字: {body_text[:200]}")

        # 3. 等待卡片渲染
        await asyncio.sleep(2)

        # 4. 点击唤取记录卡片
        #    SVG <text> 不能直接点击，先尝试 Playwright click，失败回退到 JS dispatch
        print('[云游戏] 点击"唤取记录"卡片...')
        try:
            target = page.locator('g[aria-label="唤取记录"]').first
            if await target.count() == 0:
                raise RuntimeError("not found")
            await target.click(timeout=3000)
            print("  点击成功 (Playwright click)")
        except Exception:
            result = await page.evaluate('''
                () => {
                    const g = document.querySelector('g[aria-label="唤取记录"]');
                    if (!g) return 'not found';
                    let cur = g;
                    for (let i = 0; i < 6; i++) {
                        if (!cur) break;
                        cur = cur.parentElement;
                        if (cur && (cur.tagName === 'DIV' || cur.tagName === 'A')) {
                            cur.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                            return 'dispatched on ' + cur.tagName + '.' + cur.className;
                        }
                    }
                    g.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                    return 'dispatched on g';
                }
            ''')
            print(f"  JS dispatch: {result}")
            if result == 'not found':
                print("[错误] 未找到唤取记录卡片，可能页面未完全加载")
                return None

        # 5. 等待 iframe 加载并提取 URL
        try:
            await page.wait_for_selector("iframe.webview-core", state="attached", timeout=15000)
        except Exception:
            pass
        iframe_url = await page.locator("iframe.webview-core").first.get_attribute("src")
        if not iframe_url:
            print("[错误] 点击后未出现 iframe.webview-core")
            iframes = await page.evaluate(
                'Array.from(document.querySelectorAll("iframe")).map(f => ({src: f.src.slice(0,100), class: f.className}))'
            )
            print(f"  当前页面 iframes: {iframes}")
            return None

        print(f"[云游戏] 提取到 iframe URL ({len(iframe_url)} 字符)")
        return iframe_url


# ============ 主流程 ============

def main():
    print("=" * 50)
    print("鸣潮抽卡记录采集 - 云游戏方式")
    print("=" * 50)
    print(f"输出目录: {OUTPUT_DIR}")

    # Step 1: 提取 URL
    print("\n[Step 1] 通过云游戏提取认证 URL...")
    gacha_url = asyncio.run(find_gacha_url_from_cloud())

    if not gacha_url:
        print("\n[错误] 未获取到抽卡 URL")
        print("请确保：")
        print("  1. 调试 Chrome 已启动 (端口 9222): uv run python tools/browser/start_debug_chrome.py")
        print("  2. 已在调试 Chrome 登录库洛账号")
        print(f"  3. 云游戏入口可访问: {CLOUD_URL}")
        sys.exit(1)

    # Step 2: 解析参数
    print("\n[Step 2] 解析 URL 参数...")
    params = parse_gacha_url(gacha_url)
    print(f"  服务器ID: {params['server_id']}")
    print(f"  玩家ID: {params['player_id']}")
    print(f"  区域: {params['svr_area']}")
    print(f"  API: {params['api_base']}")

    if not params["record_id"]:
        print("[错误] URL 中缺少 record_id，可能 URL 已过期或不完整")
        print("  record_id 有效期约1小时，重新运行本脚本即可刷新")
        sys.exit(1)

    # Step 3: 加载已有数据库
    print("\n[Step 3] 加载已有数据库...")
    db_data = load_database()
    existing_counts = build_existing_counts(db_data)
    if db_data:
        total_existing = sum(len(v) for v in db_data.values())
        print(f"  已有 {total_existing} 条记录 ({', '.join(f'{k}:{len(v)}' for k, v in db_data.items())})")
    else:
        print("  无已有数据，全量采集")

    # Step 4: 按卡池类型拉取记录（增量更新）
    print("\n[Step 4] 拉取抽卡记录（增量更新）...")
    session_data = {}
    total_new = 0
    failed_pools = []

    for pool_type, pool_name in CARD_POOL_TYPES.items():
        print(f"\n  [{pool_name}] (cardPoolType={pool_type})")

        records = fetch_gacha_records(
            api_base=params["api_base"],
            server_id=params["server_id"],
            player_id=params["player_id"],
            record_id=params["record_id"],
            resources_id=params["resources_id"],
            card_pool_type=pool_type,
            lang=params["lang"],
        )

        if records is None:
            failed_pools.append(pool_name)
            session_data[pool_name] = []
            time.sleep(REQUEST_INTERVAL)
            continue

        if not records:
            print(f"    无记录，跳过")
            session_data[pool_name] = []
            time.sleep(REQUEST_INTERVAL)
            continue

        session_data[pool_name] = records
        print(f"    获取 {len(records)} 条记录")

        # API 返回完整快照；逐条做多重集合并。
        existing_records = db_data.get(pool_name, [])
        pool_existing_counts = existing_counts.get(pool_name)
        merged, added = merge_records(existing_records, records, pool_existing_counts)
        db_data[pool_name] = merged
        existing_counts[pool_name] = build_existing_counts({pool_name: merged})[pool_name]
        total_new += added
        if added > 0:
            print(f"    新增 {added} 条 (总计 {len(merged)} 条)")
        else:
            print(f"    无新增记录")

        time.sleep(REQUEST_INTERVAL)

    # Step 5: 保存原始数据
    print("\n[Step 5] 保存数据...")
    session_data["_meta"] = {"complete": not failed_pools, "failed_pools": failed_pools}
    save_raw(session_data)

    if failed_pools:
        print(f"[错误] 以下卡池请求失败，数据库保持不变: {', '.join(failed_pools)}")
        raise SystemExit(1)

    # Step 6: 保存合并后的数据库
    save_database(db_data)
    print(f"[保存] 数据库已更新: {OUTPUT_DIR / 'wuwa_gacha_database.json'}")

    total_db = sum(len(v) for v in db_data.values())
    print(f"\n[完成] 本次新增 {total_new} 条，数据库共 {total_db} 条记录")

    # Step 7: 统计
    summary = generate_summary(db_data)
    summary_file = OUTPUT_DIR / "wuwa_gacha_summary.json"
    _atomic_write_json(summary_file, summary)
    print_summary(summary)


if __name__ == "__main__":
    main()
