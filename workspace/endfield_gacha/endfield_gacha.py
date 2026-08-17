#!/usr/bin/env python3
"""终末地寻访记录采集脚本 v2

通过读取游戏日志或浏览器登录获取认证 token，再通过 API 直接获取寻访记录。

参考 endfield-gacha (https://github.com/bhaoo/endfield-gacha) 的 API 结构。

数据采集方式（优先级从高到低）：
1. 读取游戏日志：从 HGWebview.log 中提取 u8_token（需先在游戏内打开寻访记录页）
2. 浏览器登录：打开鹰角用户中心，用户手动登录，拦截认证 token
3. 命令行传入：--token 直接使用已有 token

Token 获取后流程：
- 日志方式：直接拿到 u8_token，无需交换
- 浏览器/命令行方式：auth_token → oauth2 grant → binding_list → u8_token_by_uid

支持国服(hypergryph)和国际服(gryphline)。

用法：
    python workspace/endfield_gacha/endfield_gacha.py                # 采集所有记录（优先读日志，回退浏览器登录）
    python workspace/endfield_gacha/endfield_gacha.py --force        # 重新采集全量并与历史合并
    python workspace/endfield_gacha/endfield_gacha.py --global       # 使用国际服
    python workspace/endfield_gacha/endfield_gacha.py --token TOKEN  # 直接使用已有 token（跳过登录）
    python workspace/endfield_gacha/endfield_gacha.py --log          # 仅从日志获取 token（不回退浏览器）
"""

import asyncio
import json
import random
import re
import sys
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

_stealth = Stealth()
CDP_PORT = 9222

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.component_config import get_component_config

# 输出目录
OUTPUT_DIR = PROJECT_ROOT / "workspace" / "endfield_gacha"
RAW_DIR = OUTPUT_DIR / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

# ========== 常量 ==========

# 鹰角登录页
LOGIN_URLS = {
    "hypergryph": "https://user.hypergryph.com/",
    "gryphline": "https://user.gryphline.com/",
}

# API 基础 URL
API_BASE = {
    "hypergryph": "https://ef-webview.hypergryph.com",
    "gryphline": "https://ef-webview.gryphline.com",
}

# 认证相关 URL
AUTH_URLS = {
    "hypergryph": {
        "grant": "https://as.hypergryph.com/user/oauth2/v2/grant",
        "u8_token": "https://binding-api-account-prod.hypergryph.com/account/binding/v1/u8_token_by_uid",
        "poll": "https://web-api.hypergryph.com/account/info/hg",
        "app_code": "be36d44aa36bfb5b",
    },
    "gryphline": {
        "grant": "https://as.gryphline.com/user/oauth2/v2/grant",
        "u8_token": "https://binding-api-account-prod.gryphline.com/account/binding/v1/u8_token_by_uid",
        "poll": "https://web-api.gryphline.com/account/info/hg",
        "app_code": "3dacefa138426cfe",
    },
}

# 角色池类型
POOL_TYPES = [
    "E_CharacterGachaPoolType_Special",   # 特许寻访
    "E_CharacterGachaPoolType_Joint",     # 辉光庆典
    "E_CharacterGachaPoolType_Standard",  # 基础寻访
    "E_CharacterGachaPoolType_Beginner",  # 启程寻访
]

POOL_NAME_MAP = {
    "E_CharacterGachaPoolType_Special": "特许寻访",
    "E_CharacterGachaPoolType_Joint": "辉光庆典",
    "E_CharacterGachaPoolType_Standard": "基础寻访",
    "E_CharacterGachaPoolType_Beginner": "启程寻访",
}

# 稀有度映射
RARITY_MAP = {4: "★4", 5: "★5", 6: "★6"}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"

# 游戏日志路径
LOG_PATHS = {
    "hypergryph": Path.home() / "AppData" / "LocalLow" / "Hypergryph" / "Endfield" / "sdklogs" / "HGWebview.log",
    "gryphline": Path.home() / "AppData" / "LocalLow" / "Gryphline" / "Endfield" / "sdklogs" / "HGWebview.log",
}


# ========== 日志读取获取 token ==========

def read_token_from_log(provider: str = "hypergryph") -> dict | None:
    """从游戏日志中提取 u8_token 和相关参数

    ⚠️ 此功能未经测试！执行过程中可能遇到问题，请全程跟进并记录。

    日志路径：
    - 国服: %USERPROFILE%/AppData/LocalLow/Hypergryph/Endfield/sdklogs/HGWebview.log
    - 国际服: %USERPROFILE%/AppData/LocalLow/Gryphline/Endfield/sdklogs/HGWebview.log

    日志中包含类似以下 URL：
    https://ef-webview.hypergryph.com/page/gacha_xxx?u8_token=xxx&server=1&...

    前置条件：用户需先在游戏内打开寻访记录页面，让 URL 写入日志。
    """
    log_path = LOG_PATHS.get(provider)
    if not log_path or not log_path.exists():
        print(f"  日志文件不存在: {log_path}")
        return None

    file_size = log_path.stat().st_size
    print(f"  日志文件: {log_path} ({file_size / 1024:.0f} KB)")

    if file_size == 0:
        print("  日志文件为空")
        return None

    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  读取日志失败: {e}")
        return None

    # 搜索包含 ef-webview 的 gacha 页面 URL
    # 匹配 https://ef-webview.{provider}.com/page/gacha_... 格式
    pattern = rf'https://ef-webview\.{provider}\.com/page/gacha_[^\s"\'<>\x00]+'
    urls = re.findall(pattern, text)

    if not urls:
        print("  日志中未找到寻访记录URL（请确认已在游戏内打开寻访记录页面）")
        return None

    # 取最后一个 URL（最新的）
    url = urls[-1]
    print("  找到寻访记录 URL（认证参数已隐藏）")

    # 解析 URL 参数
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    # 当前页面使用 u8_token/server；兼容早期 token/server_id 命名。
    u8_token = params.get("u8_token", params.get("token", [None]))[0]
    server_id = params.get("server", params.get("server_id", [None]))[0]

    if not u8_token:
        print("  URL 中未找到 u8_token/token 参数")
        return None

    print(f"  从日志获取到 u8_token (长度={len(u8_token)}), server_id={server_id or '未指定'}")

    return {
        "u8_token": u8_token,
        "uid": params.get("uid", [""])[0],
        "roleId": params.get("roleId", [""])[0],
        "roleName": "",
        "serverName": "",
        "server_id": server_id or ("1" if provider == "hypergryph" else ""),
        "provider": provider,
        "source": "log",
    }


# ========== 反风控工具函数 ==========

async def human_delay(min_s: float = 1.5, max_s: float = 4.0):
    """随机等待，模拟人类操作间隔"""
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


# ========== 登录相关 ==========

async def login_browser(page, provider: str = "hypergryph") -> str | None:
    """通过浏览器登录鹰角通行证，拦截认证 token

    策略：
    1. 拦截 as.{provider}.com/user/auth 的 XHR/Fetch 响应
    2. 轮询 web-api.{provider}.com/account/info/hg（带 Cookie）
    """
    login_url = LOGIN_URLS[provider]
    auth_match = f"as.{provider}.com/user/auth"
    poll_url = AUTH_URLS[provider]["poll"]

    captured_token = None

    # 注入拦截脚本
    inject_script = f"""
    (function() {{
        var hasCaptured = false;
        window.__ef_captured_token = null;

        // 策略一：拦截 XHR
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
                                window.__ef_captured_token = res.data.token;
                                console.log('[终末地] XHR拦截到token');
                            }}
                        }}
                    }}
                }} catch (e) {{}}
            }});
            return originalSend.apply(this, arguments);
        }};

        // 策略二：拦截 Fetch
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
                                window.__ef_captured_token = data.data.token;
                                console.log('[终末地] Fetch拦截到token');
                            }}
                        }}
                    }}).catch(e => {{}});
                }}
            }} catch (e) {{}}
            return response;
        }};

        // 策略三：轮询
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
                        window.__ef_captured_token = data.data.content;
                        console.log('[终末地] 轮询获取到token');
                        clearInterval(timer);
                    }}
                }}
            }})
            .catch(e => {{}});
        }}, 1500);
    }})();
    """

    await page.evaluate(inject_script)

    # 等待用户登录
    print("  >>> 请在浏览器中登录鹰角通行证（手机号+验证码/密码/扫码）<<<")
    print("  >>> 登录成功后将自动获取 token <<<\n")

    for i in range(180):  # 最多等6分钟
        await asyncio.sleep(2)
        # 检查是否已捕获 token
        try:
            token = await page.evaluate("window.__ef_captured_token")
            if token:
                captured_token = token
                print("  已获取到认证 token！")
                break
        except Exception:
            pass
        if i % 15 == 14:
            print("  等待登录中...")

    return captured_token


# ========== Token 交换 ==========

async def exchange_u8_token(auth_token: str, provider: str = "hypergryph") -> dict | None:
    """用认证 token 换取 u8_token 和用户信息

    流程：auth_token → oauth2 grant → binding_list(获取uid) → u8_token_by_uid
    """
    urls = AUTH_URLS[provider]
    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: oauth2 grant
        grant_resp = await client.post(
            urls["grant"],
            json={
                "type": 1,
                "appCode": urls["app_code"],
                "token": auth_token,
            },
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        if grant_resp.status_code != 200:
            print(f"  oauth2 grant 失败: HTTP {grant_resp.status_code}")
            return None

        grant_data = grant_resp.json()
        if grant_data.get("status") != 0 or not grant_data.get("data", {}).get("token"):
            print(f"  oauth2 grant 失败: {json.dumps(grant_data, ensure_ascii=False)[:300]}")
            return None

        oauth_token = grant_data["data"]["token"]
        print(f"  grant 成功: token长度={len(oauth_token)}")

        # Step 2: binding_list 获取 uid 和角色信息
        binding_url = f"https://binding-api-account-prod.{provider}.com/account/binding/v1/binding_list"
        binding_resp = await client.get(
            binding_url,
            params={"token": oauth_token, "appCode": "endfield"},
            headers={"User-Agent": USER_AGENT},
        )
        if binding_resp.status_code != 200:
            print(f"  binding_list 失败: HTTP {binding_resp.status_code}")
            return None

        binding_data = binding_resp.json()
        if binding_data.get("status") != 0:
            print(f"  binding_list 失败: {json.dumps(binding_data, ensure_ascii=False)[:300]}")
            return None

        # 解析绑定列表，找到 endfield 应用的 uid 和角色
        app_list = binding_data.get("data", {}).get("list", [])
        app_info = next((x for x in app_list if x.get("appCode") == "endfield"), app_list[0] if app_list else None)
        if not app_info:
            print("  未找到终末地绑定信息")
            return None

        bindings = app_info.get("bindingList", [])
        if not bindings:
            print("  绑定列表为空")
            return None

        # 取第一个绑定
        first_binding = bindings[0]
        uid = str(first_binding.get("uid", ""))
        if not uid:
            print("  uid 为空")
            return None

        # 解析角色信息
        roles = first_binding.get("roles", [])
        role_info = {"uid": uid, "roleId": "", "roleName": "", "serverName": "", "serverId": ""}
        if roles:
            role = roles[0]
            role_info["roleId"] = str(role.get("roleId", ""))
            role_info["roleName"] = str(role.get("nickName", role.get("nickname", "")))
            role_info["serverName"] = str(role.get("serverName", ""))
            role_info["serverId"] = str(role.get("serverId", ""))

        print(f"  binding_list 成功: uid={uid}, 角色={role_info['roleName']}, 服务器={role_info['serverName']}")

        # Step 3: u8_token_by_uid
        server_id = role_info["serverId"] or ("1" if provider == "hypergryph" else "")
        u8_resp = await client.post(
            urls["u8_token"],
            json={"uid": uid, "token": oauth_token},
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        if u8_resp.status_code != 200:
            print(f"  u8_token 获取失败: HTTP {u8_resp.status_code}, {u8_resp.text[:300]}")
            return None

        u8_data = u8_resp.json()
        if not u8_data.get("data", {}).get("token"):
            print(f"  u8_token 获取失败: {json.dumps(u8_data, ensure_ascii=False)[:300]}")
            return None

        u8_token = u8_data["data"]["token"]
        print(f"  u8_token 获取成功: token长度={len(u8_token)}")

        return {
            "u8_token": u8_token,
            "uid": uid,
            "roleId": role_info["roleId"],
            "roleName": role_info["roleName"],
            "serverName": role_info["serverName"],
            "server_id": server_id,
            "provider": provider,
        }


async def authenticate_via_browser(provider: str) -> dict | None:
    """连接隔离调试浏览器，获取并交换认证 token。"""
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        context = browser.contexts[0]

        page = next(
            (
                pg for pg in context.pages
                if f"{provider}.com" in pg.url
            ),
            None,
        )
        if page is None:
            page = await context.new_page()
            await _stealth.apply_stealth_async(page)
            await page.goto(LOGIN_URLS[provider], wait_until="domcontentloaded", timeout=30000)
            await human_delay(3, 5)

        print(f"  当前页面: {page.url}")
        auth_token = await login_browser(page, provider)
        if not auth_token:
            return None

    user_info = await exchange_u8_token(auth_token, provider)
    if user_info:
        user_info["source"] = "browser"
    return user_info


# ========== 数据采集 ==========

def compare_seq_id(a: str, b: str) -> int:
    """比较两个 seqId 的大小（数字字符串安全比较）"""
    if a == b:
        return 0
    a_digits = a.isdigit()
    b_digits = b.isdigit()
    if a_digits and b_digits:
        if len(a) != len(b):
            return 1 if len(a) > len(b) else -1
        return 1 if a > b else -1
    if a_digits != b_digits:
        return 1 if a_digits else -1
    return 1 if a > b else -1


async def fetch_paginated_data(
    u8_token: str,
    base_url: str,
    server_id: str,
    extra_params: dict,
    stop_seq_id: str = "",
    max_pages: int = 500,
) -> list:
    """分页获取寻访记录

    基于 seq_id 游标分页，支持增量同步（stop_seq_id）。
    """
    all_data = []
    next_seq_id = ""
    has_more = True
    page = 0
    seen_seq_ids = set()

    async with httpx.AsyncClient(timeout=30) as client:
        while has_more and page < max_pages:
            page += 1

            query = {
                "lang": "zh-cn",
                "token": u8_token,
                "server_id": server_id,
                **extra_params,
            }
            if next_seq_id:
                query["seq_id"] = next_seq_id

            try:
                resp = await client.get(
                    base_url,
                    params=query,
                    headers={"User-Agent": USER_AGENT},
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"第{page}页请求失败: HTTP {resp.status_code}")

                data = resp.json()
                if data.get("code") != 0:
                    raise RuntimeError(f"API错误: code={data.get('code')} msg={data.get('msg', '')}")

                payload = data.get("data", {})
                items = payload.get("list", [])
                if not isinstance(items, list):
                    raise RuntimeError("API data.list 不是列表")

                if not items:
                    if payload.get("hasMore"):
                        raise RuntimeError("API 返回空列表但 hasMore=true")
                    break

                seq_ids = [str(item.get("seqId", "")) for item in items]
                if any(not seq_id for seq_id in seq_ids):
                    raise RuntimeError("API 记录缺少 seqId")
                if any(compare_seq_id(a, b) <= 0 for a, b in zip(seq_ids, seq_ids[1:])):
                    raise RuntimeError("API seqId 顺序异常，无法安全使用增量游标")

                # 增量同步：过滤已存在的记录
                if stop_seq_id:
                    new_only = [item for item in items if compare_seq_id(str(item.get("seqId", "")), stop_seq_id) > 0]
                    for item in new_only:
                        seq_id = str(item["seqId"])
                        if seq_id not in seen_seq_ids:
                            all_data.append(item)
                            seen_seq_ids.add(seq_id)
                    if len(new_only) < len(items):
                        # 遇到已同步过的记录，后续只会更旧，提前停止
                        has_more = False
                        break
                else:
                    for item in items:
                        seq_id = str(item["seqId"])
                        if seq_id not in seen_seq_ids:
                            all_data.append(item)
                            seen_seq_ids.add(seq_id)

                has_more = bool(payload.get("hasMore"))
                new_cursor = seq_ids[-1]
                if has_more and new_cursor == next_seq_id:
                    raise RuntimeError("API 分页游标未前进")
                next_seq_id = new_cursor

                if has_more:
                    await asyncio.sleep(random.uniform(0.5, 1.0))

                if page % 10 == 0:
                    print(f"    已翻{page}页, {len(all_data)}条")

            except Exception as e:
                if isinstance(e, RuntimeError):
                    raise
                raise RuntimeError(f"第{page}页请求异常: {e}") from e

    if has_more and page >= max_pages:
        raise RuntimeError(f"达到最大页数 {max_pages}，仍有下一页")

    return all_data


async def fetch_char_records(u8_token: str, provider: str, server_id: str,
                             stop_seq_ids: dict | None = None) -> dict:
    """获取角色寻访记录（所有池类型）"""
    base_url = f"{API_BASE[provider]}/api/record/char"
    result = {}

    for pool_type in POOL_TYPES:
        pool_name = POOL_NAME_MAP.get(pool_type, pool_type)
        print(f"\n  [{pool_name}]")

        stop_seq_id = ""
        if stop_seq_ids and pool_type in stop_seq_ids:
            stop_seq_id = stop_seq_ids[pool_type]

        records = await fetch_paginated_data(
            u8_token, base_url, server_id,
            {"pool_type": pool_type},
            stop_seq_id=stop_seq_id,
        )
        result[pool_type] = records
        print(f"    {len(records)}条")

    return result


async def fetch_weapon_pools(u8_token: str, provider: str, server_id: str) -> list:
    """获取武器池列表"""
    base_url = f"{API_BASE[provider]}/api/record/weapon/pool"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(
                base_url,
                params={"lang": "zh-cn", "token": u8_token, "server_id": server_id},
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"武器池列表请求失败: HTTP {resp.status_code}")
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"武器池列表API错误: {data.get('msg', '')}")
            pools = data.get("data", [])
            if not isinstance(pools, list):
                raise RuntimeError("武器池列表 data 不是列表")
            return pools
        except Exception as e:
            if isinstance(e, RuntimeError):
                raise
            raise RuntimeError(f"武器池列表请求异常: {e}") from e


async def fetch_weapon_records(u8_token: str, provider: str, server_id: str,
                               stop_seq_ids: dict | None = None) -> dict:
    """获取武器寻访记录"""
    # 先获取武器池列表
    pools = await fetch_weapon_pools(u8_token, provider, server_id)
    if not pools:
        print("  未找到武器池")
        return {}

    print(f"  武器池: {[p.get('poolName', p.get('poolId', '?')) for p in pools]}")

    base_url = f"{API_BASE[provider]}/api/record/weapon"
    result = {}

    for pool in pools:
        pool_id = pool.get("poolId", "")
        pool_name = pool.get("poolName", pool_id)
        print(f"\n  [武器池: {pool_name}]")

        stop_seq_id = ""
        if stop_seq_ids and pool_id in stop_seq_ids:
            stop_seq_id = stop_seq_ids[pool_id]

        records = await fetch_paginated_data(
            u8_token, base_url, server_id,
            {"pool_id": pool_id},
            stop_seq_id=stop_seq_id,
        )
        result[pool_id] = records
        print(f"    {len(records)}条")

    return result


# ========== 数据处理 ==========

def process_char_records(raw_data: dict) -> list:
    """处理角色寻访原始记录，转换为统一格式"""
    processed = []
    for pool_type, records in raw_data.items():
        pool_name = POOL_NAME_MAP.get(pool_type, pool_type)
        for rec in records:
            gacha_ts = rec.get("gachaTs", 0)
            try:
                ts_sec = int(gacha_ts) / 1000 if gacha_ts else 0
            except (ValueError, TypeError):
                ts_sec = 0

            processed.append({
                "type": "char",
                "timestamp": ts_sec,
                "time": datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S") if ts_sec else "",
                "poolType": pool_type,
                "poolId": rec.get("poolId", ""),
                "pool": rec.get("poolName", pool_name),
                "charId": rec.get("charId", ""),
                "name": rec.get("charName", ""),
                "rarity": rec.get("rarity", 0),
                "rarity_label": RARITY_MAP.get(rec.get("rarity", 0), f"★{rec.get('rarity', '?')}"),
                "is_new": rec.get("isNew", False),
                "is_free": rec.get("isFree", False),
                "seqId": str(rec.get("seqId", "")),
            })

    # 按时间排序（最新在前）
    processed.sort(key=lambda x: x["timestamp"], reverse=True)
    return processed


def process_weapon_records(raw_data: dict) -> list:
    """处理武器寻访原始记录，转换为统一格式"""
    processed = []
    for pool_id, records in raw_data.items():
        for rec in records:
            gacha_ts = rec.get("gachaTs", 0)
            try:
                ts_sec = int(gacha_ts) / 1000 if gacha_ts else 0
            except (ValueError, TypeError):
                ts_sec = 0

            processed.append({
                "type": "weapon",
                "timestamp": ts_sec,
                "time": datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d %H:%M:%S") if ts_sec else "",
                "poolId": pool_id,
                "pool": rec.get("poolName", pool_id),
                "weaponId": rec.get("weaponId", ""),
                "name": rec.get("weaponName", ""),
                "rarity": rec.get("rarity", 0),
                "rarity_label": RARITY_MAP.get(rec.get("rarity", 0), f"★{rec.get('rarity', '?')}"),
                "is_new": rec.get("isNew", False),
                "seqId": str(rec.get("seqId", "")),
            })

    processed.sort(key=lambda x: x["timestamp"], reverse=True)
    return processed


def get_max_seq_ids(records: list) -> dict:
    """从记录中提取每个池子的最大 seqId"""
    max_ids = {}
    for rec in records:
        pool_key = rec.get("poolType") or rec.get("poolId", "")
        seq_id = rec.get("seqId", "")
        if not pool_key or not seq_id:
            continue
        if pool_key not in max_ids or compare_seq_id(seq_id, max_ids[pool_key]) > 0:
            max_ids[pool_key] = seq_id
    return max_ids


def generate_summary(char_records: list, weapon_records: list, user_info: dict) -> dict:
    """生成统计摘要"""
    public_user_info = {
        key: value for key, value in user_info.items()
        if key != "u8_token"
    }
    all_records = char_records + weapon_records
    total = len(all_records)
    if total == 0:
        return {"total": 0, "user": public_user_info}

    # 按稀有度统计
    rarity_count = {}
    six_star = []
    for rec in all_records:
        r = rec["rarity_label"]
        rarity_count[r] = rarity_count.get(r, 0) + 1
        if rec["rarity"] == 6:
            six_star.append(f"{rec['name']}({rec['pool']})")

    # 按类型统计
    char_count = len(char_records)
    weapon_count = len(weapon_records)

    # 角色池分布
    char_pool_count = {}
    for rec in char_records:
        pool = rec["pool"] or "未知卡池"
        char_pool_count[pool] = char_pool_count.get(pool, 0) + 1

    # 武器池分布
    weapon_pool_count = {}
    for rec in weapon_records:
        pool = rec["pool"] or "未知卡池"
        weapon_pool_count[pool] = weapon_pool_count.get(pool, 0) + 1

    return {
        "total": total,
        "char_count": char_count,
        "weapon_count": weapon_count,
        "rarity_count": rarity_count,
        "six_star_count": len(six_star),
        "six_star_names": six_star,
        "char_pool_count": char_pool_count,
        "weapon_pool_count": weapon_pool_count,
        "user": public_user_info,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def print_summary(summary: dict):
    """打印统计摘要"""
    print("\n" + "=" * 50)
    print("  终末地寻访记录统计")
    print("=" * 50)

    user = summary.get("user", {})
    if user.get("roleName"):
        print(f"  角色: {user['roleName']} ({user.get('serverName', '')})")

    print(f"  总抽数: {summary['total']} (角色{summary.get('char_count', 0)} + 武器{summary.get('weapon_count', 0)})")

    if summary["total"] == 0:
        return

    print("\n  稀有度分布:")
    for r in ["★6", "★5", "★4"]:
        count = summary["rarity_count"].get(r, 0)
        if count > 0:
            pct = count / summary["total"] * 100
            print(f"    {r}: {count} ({pct:.1f}%)")

    if summary["six_star_names"]:
        print(f"\n  六星: {', '.join(summary['six_star_names'])}")

    if summary.get("char_pool_count"):
        print("\n  角色池分布:")
        for pool, count in sorted(summary["char_pool_count"].items(), key=lambda x: -x[1]):
            print(f"    {pool}: {count}抽")

    if summary.get("weapon_pool_count"):
        print("\n  武器池分布:")
        for pool, count in sorted(summary["weapon_pool_count"].items(), key=lambda x: -x[1]):
            print(f"    {pool}: {count}抽")

    print("=" * 50)


def incremental_update(new_records: list, record_type: str) -> list:
    """增量更新：合并新记录到已有数据"""
    db_path = OUTPUT_DIR / f"{record_type}_records.json"

    if db_path.exists():
        with open(db_path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, list):
            raise RuntimeError(f"已有数据库格式异常（应为列表）: {db_path}")
        existing_ids = {
            (r.get("poolType") or r.get("poolId", ""), str(r.get("seqId", "")))
            for r in existing if r.get("seqId")
        }
    else:
        existing = []
        existing_ids = set()

    added = 0
    for rec in new_records:
        seq_id = rec.get("seqId", "")
        record_id = (rec.get("poolType") or rec.get("poolId", ""), str(seq_id))
        if seq_id and record_id not in existing_ids:
            existing.append(rec)
            existing_ids.add(record_id)
            added += 1

    # 重新排序
    existing.sort(key=lambda x: x["timestamp"], reverse=True)

    print(f"  {record_type}增量更新: 新增 {added} 条，总计 {len(existing)} 条")
    return existing


def _atomic_write_json(path: Path, data) -> None:
    """先写完同目录临时文件，再原子替换目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
    temp_path.replace(path)


def load_existing_max_seq_ids() -> tuple[dict, dict]:
    """加载已有记录的最大 seqId（用于增量同步）"""
    char_max = {}
    weapon_max = {}

    char_path = OUTPUT_DIR / "char_records.json"
    if char_path.exists():
        with open(char_path, "r", encoding="utf-8") as f:
            records = json.load(f)
        char_max = get_max_seq_ids(records)

    weapon_path = OUTPUT_DIR / "weapon_records.json"
    if weapon_path.exists():
        with open(weapon_path, "r", encoding="utf-8") as f:
            records = json.load(f)
        weapon_max = get_max_seq_ids(records)

    return char_max, weapon_max


# ========== 主流程 ==========

async def main_async(provider: str = "hypergryph", force: bool = False,
                     token: str = "", log_only: bool = False):
    """主流程"""
    config = get_component_config("endfield_gacha") or {}
    if provider == "hypergryph":
        provider = config.get("provider", provider)

    user_info = None

    # === Step 1: 获取认证信息 ===
    # 优先级：命令行 token > 游戏日志 > 浏览器登录
    if token:
        print("[1/4] 使用命令行提供的 token...")
        auth_token = token
        print("  使用命令行提供的 auth_token")
        # 需要交换 u8_token
        print("\n[2/4] 获取 u8_token...")
        user_info = await exchange_u8_token(auth_token, provider)
        if not user_info:
            print("  u8_token 获取失败，退出")
            return
    else:
        # 尝试从游戏日志获取
        print("[1/4] 获取认证信息...")
        print("  尝试从游戏日志获取 token...")
        log_result = read_token_from_log(provider)

        if log_result:
            # 日志方式：直接拿到 u8_token，无需交换
            user_info = log_result
            print("  从游戏日志获取成功，跳过浏览器登录")
        elif log_only:
            print("  --log 模式：日志获取失败，不回退浏览器登录，退出")
            return
        else:
            # 回退到浏览器登录
            print("  日志获取失败，回退到浏览器登录...")
            user_info = await authenticate_via_browser(provider)
            if not user_info:
                raise RuntimeError("浏览器登录或 u8_token 获取失败")

    print(f"  UID: {user_info['uid']}")
    if user_info["roleName"]:
        print(f"  角色: {user_info['roleName']} ({user_info['serverName']})")

    u8_token = user_info["u8_token"]
    server_id = user_info["server_id"]

    # 加载增量同步的 stop_seq_id
    char_stop_ids = {}
    weapon_stop_ids = {}
    if not force:
        char_stop_ids, weapon_stop_ids = load_existing_max_seq_ids()
        if char_stop_ids or weapon_stop_ids:
            print(f"  增量同步: 角色{len(char_stop_ids)}个池, 武器{len(weapon_stop_ids)}个池有已存记录")

    # 获取角色寻访记录
    print("\n[3/4] 获取寻访记录...")
    print("  === 角色池 ===")
    try:
        raw_char = await fetch_char_records(u8_token, provider, server_id, char_stop_ids if not force else None)

        print("\n  === 武器池 ===")
        raw_weapon = await fetch_weapon_records(u8_token, provider, server_id, weapon_stop_ids if not force else None)
    except RuntimeError as exc:
        if user_info.get("source") == "log" and not log_only and not token:
            print(f"  日志 token 无法完成采集: {exc}")
            print("  回退到浏览器登录后重试...")
            user_info = await authenticate_via_browser(provider)
            if not user_info:
                raise RuntimeError("浏览器登录或 u8_token 获取失败") from exc
            u8_token = user_info["u8_token"]
            server_id = user_info["server_id"]
            raw_char = await fetch_char_records(u8_token, provider, server_id, char_stop_ids if not force else None)
            print("\n  === 武器池 ===")
            raw_weapon = await fetch_weapon_records(u8_token, provider, server_id, weapon_stop_ids if not force else None)
        else:
            raise

    # 处理记录
    print("\n[4/4] 处理数据...")
    char_records = process_char_records(raw_char)
    weapon_records = process_weapon_records(raw_weapon)

    # 保存原始数据
    raw_path = RAW_DIR / ("raw_%s.json" % datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    _atomic_write_json(raw_path, {"char": raw_char, "weapon": raw_weapon})
    print(f"  原始数据已保存: {raw_path.name}")

    # 增量更新
    if force:
        print("  强制模式: 已重新采集全量，但仍与历史库合并，不替换历史")
    all_char = incremental_update(char_records, "char")
    all_weapon = incremental_update(weapon_records, "weapon")

    # 保存完整数据
    char_path = OUTPUT_DIR / "char_records.json"
    _atomic_write_json(char_path, all_char)

    weapon_path = OUTPUT_DIR / "weapon_records.json"
    _atomic_write_json(weapon_path, all_weapon)

    # 生成并打印统计
    summary = generate_summary(all_char, all_weapon, user_info)
    summary_path = OUTPUT_DIR / "summary.json"
    _atomic_write_json(summary_path, summary)

    print_summary(summary)


def main():
    parser = argparse.ArgumentParser(description="终末地寻访记录采集 v2")
    parser.add_argument("--force", action="store_true", help="强制重新采集全量（仍与历史库合并）")
    parser.add_argument("--global", dest="use_global", action="store_true", help="使用国际服 (gryphline)")
    parser.add_argument("--token", type=str, default="", help="直接使用已有的认证 token（跳过浏览器登录）")
    parser.add_argument("--log", dest="log_only", action="store_true", help="仅从游戏日志获取 token（不回退浏览器登录）")
    args = parser.parse_args()

    provider = "gryphline" if args.use_global else "hypergryph"
    asyncio.run(main_async(provider=provider, force=args.force, token=args.token, log_only=args.log_only))


if __name__ == "__main__":
    main()
