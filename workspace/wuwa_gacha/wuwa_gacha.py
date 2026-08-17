"""
鸣潮抽卡记录采集脚本 v3
支持两种URL提取方式：内存扫描 + 日志解密，通过API获取结构化抽卡数据

URL提取方式：
1. 内存扫描（需管理员权限）：扫描游戏进程内存查找 aki-gm-resources URL
2. 日志解密（无需管理员权限）：解密 Client.log 提取 URL
   - 新版本鸣潮(3.4+)的 Client.log 已被库洛加密（LUT XOR 方案）
   - Scheme A: 奇数位 XOR 0xA5，偶数位 XOR 0xEF（PC端常见）
   - Scheme B: 全部 XOR 0x55
   - 解密后搜索 aki-gm-resources 即可获取抽卡URL

使用前：
1. 打开鸣潮游戏
2. 进入游戏内的"唤取记录"页面（停留几秒让URL写入日志/内存）
3. 运行本脚本

安全策略：
- 每次请求间隔 1 秒，避免触发限流
- 最多重试 3 次，指数退避
- URL 有效期约 1 小时
- 内存扫描仅读取进程内存，不修改任何游戏数据

增量更新：
- 每次采集保存原始数据到 raw/ 目录（永不删除）
- 合并数据到 wuwa_gacha_database.json（按完整记录内容的多重集增量合并）
- 保留同秒同名的合法重复记录，重复运行同一快照新增为 0
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import requests

# ============ 配置 ============

# 兼容直接运行和通过 exec/python 执行
try:
    OUTPUT_DIR = Path(__file__).parent.parent.parent / "workspace" / "wuwa_gacha"
except NameError:
    OUTPUT_DIR = Path("workspace/wuwa_gacha")

# API 端点
CN_API_BASE = "https://gmserver-api.aki-game2.com"
GLOBAL_API_BASE = "https://gmserver-api.aki-game2.net"

# 卡池类型（API cardPoolType → 游戏内名称，已通过记录数+内容验证）
CARD_POOL_TYPES = {
    1: "角色活动唤取",
    2: "武器活动唤取",
    3: "角色常驻唤取",
    4: "武器常驻唤取",
    5: "新手唤取",
    6: "新手自选唤取",
    7: "武器新旅唤取",
    8: "角色新旅唤取",
    10: "角色联动唤取",
    11: "武器联动唤取",
}

# 请求间隔（秒）
REQUEST_INTERVAL = 1.0
# 最大重试次数
MAX_RETRIES = 3
# ============ 内存扫描提取URL ============

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)


class _MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64),
        ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD),
        ("_pad1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64),
        ("State", wintypes.DWORD),
        ("RegionProtect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("_pad2", wintypes.DWORD),
    ]


_MEM_COMMIT = 0x1000
_READABLE = 0x04 | 0x02 | 0x08 | 0x20 | 0x40 | 0x80


def _scan_process_memory(pid: int, pattern: bytes = b"aki-gm-resources") -> set[str]:
    """扫描单个进程的内存，查找包含指定模式的URL"""
    urls = set()
    process = _kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not process:
        return urls

    addr = ctypes.c_uint64(0)
    mbi = _MEMORY_BASIC_INFORMATION64()
    mbi_size = ctypes.sizeof(mbi)
    count = 0

    while True:
        ret = _kernel32.VirtualQueryEx(process, addr, ctypes.byref(mbi), mbi_size)
        if ret == 0:
            break
        count += 1
        if count > 5000:
            break

        base = mbi.BaseAddress
        size = mbi.RegionSize

        if (mbi.State == _MEM_COMMIT and mbi.RegionProtect & _READABLE and 0 < size < 100 * 1024 * 1024):
            buf = ctypes.create_string_buffer(size)
            n = ctypes.c_size_t()
            if _kernel32.ReadProcessMemory(process, ctypes.c_void_p(base), buf, size, ctypes.byref(n)):
                data = buf.raw[:n.value]
                off = 0
                while True:
                    idx = data.find(pattern, off)
                    if idx == -1:
                        break
                    start = max(0, idx - 30)
                    end = min(len(data), idx + 800)
                    chunk = data[start:end]
                    try:
                        text = chunk.decode('utf-8', errors='replace')
                        for m in re.finditer(r'https://aki-gm-resources[^\s"\'<>\x00]+', text):
                            url = m.group(0)
                            if '/gacha/' in url:
                                urls.add(url)
                    except Exception:
                        pass
                    off = idx + len(pattern)

        next_addr = base + size
        if next_addr <= addr.value:
            break
        addr = ctypes.c_uint64(next_addr)

    _kernel32.CloseHandle(process)
    return urls


def _find_pids(process_name: str) -> list[int]:
    """查找指定进程名的所有PID"""
    result = subprocess.run(
        ['tasklist', '/FI', f'IMAGENAME eq {process_name}', '/FO', 'CSV', '/NH'],
        capture_output=True, text=True
    )
    pids = []
    for line in result.stdout.strip().split('\n'):
        parts = line.strip().split(',')
        if len(parts) >= 2 and process_name.replace('.exe', '') in line:
            try:
                pids.append(int(parts[1].strip('"')))
            except ValueError:
                pass
    return pids


def find_gacha_url() -> str | None:
    """从游戏进程内存中提取抽卡记录认证URL"""
    print("[扫描] 查找鸣潮游戏进程...")

    target_names = ['Client-Win64-Shipping.exe', 'KRWebView.exe']
    all_urls = set()

    for target in target_names:
        pids = _find_pids(target)
        if not pids:
            print(f"  {target}: 未运行")
            continue
        print(f"  {target}: {len(pids)}个进程 (PID: {', '.join(str(p) for p in pids)})")

        for pid in pids:
            urls = _scan_process_memory(pid)
            all_urls.update(urls)

    if not all_urls:
        print("[错误] 未在进程内存中找到抽卡URL")
        print("请确保：")
        print("  1. 游戏已启动并登录")
        print("  2. 已在游戏内打开'唤取记录'页面")
        print("  3. 以管理员权限运行本脚本")
        print("  4. URL未过期（打开后约1小时有效）")
        return None

    # 优先选择包含 record_id 的完整URL
    best_url = None
    for url in all_urls:
        if 'record_id=' in url:
            if best_url is None or len(url) > len(best_url):
                best_url = url

    if not best_url:
        best_url = max(all_urls, key=len)

    print(f"  [找到] 抽卡URL ({len(all_urls)}个候选)")
    return best_url


def parse_gacha_url(url: str) -> dict:
    """解析抽卡URL中的参数"""
    if "#" in url:
        fragment = url.split("#", 1)[1]
        if "?" in fragment:
            query_part = fragment.split("?", 1)[1]
        else:
            query_part = ""
    else:
        query_part = ""

    params = parse_qs(query_part)

    result = {
        "server_id": params.get("svr_id", [None])[0],
        "player_id": params.get("player_id", [None])[0],
        "record_id": params.get("record_id", [None])[0],
        "resources_id": params.get("resources_id", [None])[0],
        "lang": params.get("lang", ["zh-Hans"])[0],
        "svr_area": params.get("svr_area", ["cn"])[0],
    }

    is_oversea = "aki-gm-resources-oversea" in url
    result["api_base"] = GLOBAL_API_BASE if is_oversea else CN_API_BASE

    return result


# ============ 日志解密提取URL ============

def _get_game_install_path() -> str | None:
    """从注册表读取鸣潮安装路径"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\KRInstall Wuthering Waves"
        )
        install_path, _ = winreg.QueryValueEx(key, "InstallPath")
        winreg.CloseKey(key)
        return install_path
    except (FileNotFoundError, OSError):
        return None


def _decrypt_log(data: bytes) -> str | None:
    """
    解密库洛加密的 Client.log

    支持两种加密方案：
    - Scheme A (PC端常见): 前3字节为 ?? 54 50 (加密后)，body用交替LUT解密
      奇数位 XOR 0xA5，偶数位 XOR 0xEF
    - Scheme B: 前3字节为 00 4C 4F，body用 0x55 XOR 解密
    """
    if len(data) < 3:
        return None

    # 检测加密方案
    # Scheme A: 解密后 bytes[1]==0x54('T'), bytes[2]==0x50('P')
    # 加密后 bytes[1]^0xA5==0x54 → bytes[1]==0xF1, bytes[2]^0xA5==0x50 → bytes[2]==0xF5
    # 但更通用的检测方式：直接检查加密后的特征
    scheme = None

    if data[1] == 0x54 and data[2] == 0x50:
        # 明文 Scheme A header
        scheme = 'A'
    elif data[0] == 0x00 and data[1] == 0x4C and data[2] == 0x4F:
        # 明文 Scheme B header
        scheme = 'B'
    elif (data[1] ^ 0xA5) == 0x54 and (data[2] ^ 0xA5) == 0x50:
        # 加密后的 Scheme A header (常见于当前版本)
        scheme = 'A'
    elif (data[0] ^ 0x55) == 0x00 and (data[1] ^ 0x55) == 0x4C and (data[2] ^ 0x55) == 0x4F:
        # 加密后的 Scheme B header
        scheme = 'B'

    if scheme is None:
        # 尝试作为明文处理
        try:
            text = data.decode('utf-8', errors='strict')
            if 'aki-gm-resources' in text or 'Log file' in text:
                return text
        except Exception:
            pass
        return None

    # 构建 LUT
    if scheme == 'A':
        lut = bytearray(256)
        for i in range(256):
            lut[i] = (i ^ 0xA5) if (i & 1) else (i ^ 0xEF)
    else:
        lut = bytearray(256)
        for i in range(256):
            lut[i] = i ^ 0x55

    # 跳过3字节header，解密body
    body = data[3:]
    decrypted = bytearray(len(body))
    for i in range(len(body)):
        decrypted[i] = lut[body[i]]

    # 重建header（明文）
    if scheme == 'A':
        header = bytes([data[0] ^ (0xA5 if data[0] & 1 else 0xEF), 0x54, 0x50])
    else:
        header = bytes([0x00, 0x4C, 0x4F])

    full = header + bytes(decrypted)
    return full.decode('utf-8', errors='replace')


def _search_gacha_url_in_text(text: str) -> str | None:
    """从解密后的日志文本中搜索抽卡URL"""
    # 搜索最后一个 aki-gm-resources URL（最新的）
    urls = []
    for m in re.finditer(r'https://aki-gm-resources[^\s"\'<>\x00]+', text):
        url = m.group(0)
        if '/gacha/' in url and 'record_id=' in url:
            urls.append(url)

    if not urls:
        return None

    # 返回最后一个（最新的）
    return urls[-1]


def find_gacha_url_from_log() -> str | None:
    """从加密的 Client.log 中解密并提取抽卡URL"""
    print("[日志] 查找鸣潮游戏日志...")

    # 1. 从注册表获取安装路径
    install_path = _get_game_install_path()
    if not install_path:
        print("  注册表中未找到鸣潮安装路径")
        return None
    print(f"  安装路径: {install_path}")

    # 2. 定位日志文件
    log_path = Path(install_path) / "Wuthering Waves Game" / "Client" / "Saved" / "Logs" / "Client.log"
    if not log_path.exists():
        print(f"  日志文件不存在: {log_path}")
        return None
    print(f"  日志文件: {log_path} ({log_path.stat().st_size / 1024:.0f} KB)")

    # 3. 读取并解密
    data = log_path.read_bytes()
    text = _decrypt_log(data)
    if text is None:
        print("  无法解密日志文件（未知加密方案或文件损坏）")
        return None

    # 4. 搜索抽卡URL
    url = _search_gacha_url_in_text(text)
    if url:
        print(f"  [找到] 从日志解密获取抽卡URL")
        return url

    print("  日志中未找到抽卡URL（请确认已在游戏内打开唤取记录）")
    return None


# ============ API 请求 ============

def fetch_gacha_records(
    api_base: str,
    server_id: str,
    player_id: str,
    record_id: str,
    resources_id: str,
    card_pool_type: int,
    lang: str = "zh-Hans",
) -> list[dict] | None:
    """
    获取指定卡池类型的全部抽卡记录

    API端点: POST {api_base}/gacha/record/query
    请求体: cardPoolType, playerId, recordId, serverId, resourcesId, languageCode
    """
    url = f"{api_base}/gacha/record/query"
    body = {
        "cardPoolType": card_pool_type,
        "playerId": player_id,
        "recordId": record_id,
        "serverId": server_id,
        "resourcesId": resources_id,
        "languageCode": lang,
    }

    data = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(url, json=body, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            break
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"    请求失败 (尝试 {attempt + 1}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES - 1:
                print(f"    等待 {wait}s 后重试...")
                time.sleep(wait)
            else:
                print(f"    [错误] 达到最大重试次数，跳过此卡池")
                return None

    if data is None:
        return None

    if data.get("code") != 0:
        msg = data.get("message", "未知错误")
        print(f"    API返回错误: code={data.get('code')}, message={msg}")
        return None

    records = data.get("data", [])
    if not isinstance(records, list):
        print("    API返回格式异常: data 不是列表")
        return None

    return records


# ============ 增量更新 ============

def _record_key(record: dict) -> tuple:
    """生成记录内容键；同一十连中完全相同的记录用出现次数区分。"""
    return (
        str(record.get("cardPoolType", "")),
        str(record.get("resourceId", "")),
        int(record.get("qualityLevel", 0) or 0),
        str(record.get("resourceType", "")),
        str(record.get("name", "")),
        int(record.get("count", 1) or 1),
        str(record.get("time", "")),
    )


def load_database() -> dict:
    """加载数据库文件，返回 {卡池名: [记录]} 格式"""
    db_path = OUTPUT_DIR / "wuwa_gacha_database.json"
    if not db_path.exists():
        return {}
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取已有数据库 {db_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"已有数据库格式异常（应为对象）: {db_path}")
    return data


def build_existing_counts(db_data: dict) -> dict[str, Counter]:
    """从数据库构建每个卡池的记录内容多重集。"""
    result = {}
    for pool_name, records in db_data.items():
        if not isinstance(records, list):
            raise RuntimeError(f"数据库卡池 {pool_name} 的记录格式异常（应为列表）")
        result[pool_name] = Counter(_record_key(r) for r in records)
    return result


def merge_records(
    existing: list[dict],
    new: list[dict],
    existing_counts: Counter | None = None,
) -> tuple[list[dict], int]:
    """按内容多重集合并快照，保留同秒同名的合法重复记录。"""
    merged = list(existing)
    counts = Counter(_record_key(r) for r in existing) if existing_counts is None else existing_counts
    snapshot_counts = Counter()
    added = 0
    for r in new:
        key = _record_key(r)
        snapshot_counts[key] += 1
        if snapshot_counts[key] > counts[key]:
            merged.append(r)
            counts[key] += 1
            added += 1
    merged.sort(key=lambda r: str(r.get("time", "")), reverse=True)
    return merged, added


def _atomic_write_json(path: Path, data) -> None:
    """先完整写入同目录临时文件，再原子替换目标文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
    temp_path.replace(path)


def save_database(db_data: dict):
    """保存数据库文件"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUTPUT_DIR / "wuwa_gacha_database.json"
    _atomic_write_json(db_path, db_data)


def save_raw(result: dict):
    """保存原始数据到 raw/ 目录（永不删除）"""
    raw_dir = OUTPUT_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    raw_path = raw_dir / f"wuwa_gacha_raw_{timestamp}.json"
    _atomic_write_json(raw_path, result)
    print(f"[保存] 原始数据: {raw_path}")


# ============ 统计 ============

def generate_summary(all_data: dict) -> dict:
    """生成统计摘要"""
    summary = {}
    for pool_name, records in all_data.items():
        if not records:
            continue

        star_counts = {}
        for r in records:
            quality = r.get("qualityLevel", 0)
            star_counts[quality] = star_counts.get(quality, 0) + 1

        item_counts = {}
        for r in records:
            name = r.get("name", "未知")
            quality = r.get("qualityLevel", 0)
            key = f"{'★' * quality} {name}"
            item_counts[key] = item_counts.get(key, 0) + 1

        five_star_records = [r for r in records if r.get("qualityLevel") == 5]

        pity_intervals = []
        counter = 0
        for r in sorted(records, key=lambda item: str(item.get("time", ""))):
            counter += 1
            if r.get("qualityLevel") == 5:
                pity_intervals.append(counter)
                counter = 0

        summary[pool_name] = {
            "total": len(records),
            "star_counts": star_counts,
            "item_counts": dict(sorted(item_counts.items(), key=lambda x: -x[1])),
            "five_star_count": len(five_star_records),
            "five_star_names": [r.get("name", "未知") for r in five_star_records],
            "pity_intervals": pity_intervals,
            "avg_pity": round(sum(pity_intervals) / len(pity_intervals), 1) if pity_intervals else 0,
        }

    return summary


def print_summary(summary: dict):
    """打印简要统计"""
    print("\n" + "=" * 50)
    print("抽卡统计摘要")
    print("=" * 50)

    for pool_name, stats in summary.items():
        print(f"\n【{pool_name}】")
        print(f"  总抽数: {stats['total']}")
        star_str = "  ".join(
            f"{'★' * k}:{v}" for k, v in sorted(stats["star_counts"].items(), reverse=True)
        )
        print(f"  星级分布: {star_str}")
        if stats["five_star_count"] > 0:
            print(f"  五星数量: {stats['five_star_count']}")
            print(f"  五星列表: {', '.join(stats['five_star_names'])}")
            print(f"  平均保底: {stats['avg_pity']}抽")
            print(f"  保底间隔: {stats['pity_intervals']}")


# ============ 主流程 ============

def main():
    print("=" * 50)
    print("鸣潮抽卡记录采集 v3")
    print("=" * 50)

    # Step 1: 提取URL（优先日志解密，回退内存扫描）
    gacha_url = None

    # 方式1: 日志解密（无需管理员权限，更稳定）
    print("\n[Step 1a] 从游戏日志解密提取认证URL...")
    gacha_url = find_gacha_url_from_log()

    if not gacha_url:
        # 方式2: 内存扫描（需管理员权限，作为回退方案）
        print("\n[Step 1b] 日志方式未获取到URL，尝试内存扫描...")
        gacha_url = find_gacha_url()

    if not gacha_url:
        print("\n[错误] 所有方式均未获取到抽卡URL")
        print("请确保：")
        print("  1. 游戏已启动并登录")
        print("  2. 已在游戏内打开'唤取记录'页面")
        print("  3. URL未过期（打开后约1小时有效）")
        print("  4. 内存扫描方式需要管理员权限")
        sys.exit(1)

    # Step 2: 解析参数
    print("\n[Step 2] 解析URL参数...")
    params = parse_gacha_url(gacha_url)
    print(f"  服务器ID: {params['server_id']}")
    print(f"  玩家ID: {params['player_id']}")
    print(f"  区域: {params['svr_area']}")
    print(f"  API: {params['api_base']}")

    if not params["record_id"]:
        print("[错误] URL中缺少 record_id，可能URL已过期或不完整")
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

        # API 返回完整快照；逐条做多重集合并，禁止因少量重复跳过整池。
        existing_records = db_data.get(pool_name, [])
        pool_existing_counts = existing_counts.get(pool_name, Counter())
        merged, added = merge_records(existing_records, records, pool_existing_counts)
        db_data[pool_name] = merged
        existing_counts[pool_name] = pool_existing_counts
        total_new += added
        if added > 0:
            print(f"    新增 {added} 条 (总计 {len(merged)} 条)")
        else:
            print(f"    无新增记录")

        time.sleep(REQUEST_INTERVAL)

    # Step 5: 保存原始数据（永不删除）
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

    # Step 7: 打印统计
    summary = generate_summary(db_data)
    summary_file = OUTPUT_DIR / "wuwa_gacha_summary.json"
    _atomic_write_json(summary_file, summary)
    print_summary(summary)


if __name__ == "__main__":
    main()
