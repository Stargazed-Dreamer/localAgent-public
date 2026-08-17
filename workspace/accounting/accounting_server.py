"""记账审核独立服务 - Web 可视化审核页面后端

从 server/accounting.py 拆出，作为独立 FastAPI 服务运行（端口 8780）。
与 workspace/accounting/bill_converter.py 配合使用。

启动方式：
    python workspace/accounting/accounting_server.py
或通过 tools_launcher GUI 启动（见 tools_manifest.json 中 accounting_server 条目）。

访问入口：http://127.0.0.1:8780/  （自动打开 accounting.html）
"""

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import toml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict
import uvicorn

logger = logging.getLogger("localagent.accounting_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# === 路径配置（基于 __file__ 重新计算，不依赖 server 模块）===
OUTPUT_DIR = Path(__file__).parent                  # workspace/accounting/（代码 + 映射表 + HTML 仍在 workspace）
PROJECT_DIR = Path(__file__).parent.parent.parent   # 项目根目录
CONFIG_PATH = PROJECT_DIR / "config.toml"
# bill_review_data.json 是含个人消费记录的私有产出，挪到 private_vault/accounting/（obsidian vault，不进 release）
VAULT_DIR = PROJECT_DIR / "private_vault" / "accounting"
REVIEW_JSON_FILE = "bill_review_data.json"
REVIEW_CONFIG_FILE = OUTPUT_DIR / "review_config.json"
MAPPING_FILE = OUTPUT_DIR / "name_mapping.json"
HTML_FILE = OUTPUT_DIR / "accounting.html"
BILL_CONVERTER = OUTPUT_DIR / "bill_converter.py"


def _get_accounting_dir() -> Path:
    """从 config.toml [accounting] 段读取账单目录（Obsidian 仓库路径）"""
    if CONFIG_PATH.exists():
        try:
            config = toml.load(str(CONFIG_PATH))
            accounting = config.get("accounting", {})
            path = accounting.get("accounting_dir", "")
            if path:
                return Path(path)
        except Exception as e:
            logger.warning(f"读取 config.toml 失败: {e}")
    # 回退：兼容旧版未配置的用户
    return Path(r"E:\<data_drive>:\<system_data_root>\Obsidian\Task\Task\main\记账")


ACCOUNTING_DIR = _get_accounting_dir()

app = FastAPI(
    title="LocalAgent Accounting Server",
    version="1.0",
    description="记账审核独立服务 - Web 可视化审核页面后端",
)


# === Pydantic 模型 ===

class ApplyItem(BaseModel):
    """ApplyItem类表示一个申请项，用于存储申请的相关信息。参数：id (int) - 申请项的唯一标识符；sector (str) - 申请所属的部门或领域；category (str) - 申请的类别；description (str) - 申请的详细描述。返回值：无，但创建ApplyItem实例时返回该类的对象。"""
    model_config = ConfigDict(extra='forbid')  # 独立服务无法 import lib.schema，内联 forbid
    id: int
    sector: str
    category: str
    description: str


class ApplyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[ApplyItem]


class ConfigUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    """配置更新请求模型

    用于封装需要更新的配置信息，包含扇区和描述的相关数据。

    属性:
        sectors (Optional[dict[str, list[str]]]): 扇区配置字典，键为扇区名称，值为该扇区下的项目列表
        descriptions (Optional[dict[str, list[str]]]): 描述配置字典，键为描述类型，值为该类型下的详细描述列表

    使用示例:
        request = ConfigUpdateRequest(
            sectors={"扇区A": ["项目1", "项目2"]},
            descriptions={"类型1": ["描述1", "描述2"]}
        )
    """

    # 扇区配置，存储扇区名称及其关联的项目列表
    sectors: Optional[dict[str, list[str]]] = None

    # 描述配置，存储描述类型及其对应的描述内容列表
    descriptions: Optional[dict[str, list[str]]] = None


# === 工具函数 ===

def _read_review_data() -> dict | None:
    """读取评审数据文件并返回其内容。如果文件不存在，返回None。"""
    path = VAULT_DIR / REVIEW_JSON_FILE  # 构建评审数据文件的完整路径（私有产出在 private_vault/accounting/）
    if not path.exists():  # 检查文件是否存在
        return None  # 文件不存在，返回None
    with open(path, "r", encoding="utf-8") as f:  # 以UTF-8编码打开文件
        return json.load(f)  # 读取并返回JSON数据作为字典


def _read_config() -> dict:
    """
    读取配置文件并返回配置字典。

    参数：无

    返回值：包含'sectors'和'descriptions'键的字典。
    """
    if not REVIEW_CONFIG_FILE.exists():  # 如果配置文件不存在
        return {"sectors": {}, "descriptions": {}}  # 返回空配置字典
    with open(REVIEW_CONFIG_FILE, "r", encoding="utf-8") as f:  # 以读模式打开配置文件，使用UTF-8编码
        return json.load(f)  # 加载并返回JSON内容


def _write_config(config: dict):
    """将配置字典以JSON格式写入指定的配置文件。

    参数:
        config (dict): 要写入的配置字典。

    返回:
        无
    """
    with open(REVIEW_CONFIG_FILE, "w", encoding="utf-8") as f:  # 打开配置文件进行写入，指定编码为utf-8
        json.dump(config, f, ensure_ascii=False, indent=2)  # 将配置字典写入文件，确保非ASCII字符正常显示，并缩进2个空格


def _read_mapping() -> dict:
    """读取映射文件并返回其内容作为字典。

    参数：
    无参数。

    返回值：
    dict: 包含映射文件内容的字典；如果文件不存在，返回空字典。
    """
    if not MAPPING_FILE.exists():  # 检查映射文件是否存在
        return {}  # 文件不存在时返回空字典
    with open(MAPPING_FILE, "r", encoding="utf-8") as f:  # 以UTF-8编码打开文件
        return json.load(f)  # 加载JSON内容并返回字典


def _write_mapping(mapping: dict):
    """
    将传入的字典写入到指定的JSON文件中。

    参数：
    mapping (dict): 要写入的字典数据。

    返回值：
    无
    """
    # 以写入模式打开MAPPING_FILE文件，使用UTF-8编码
    with open(MAPPING_FILE, "w", encoding="utf-8") as f:
        # 将字典mapping以JSON格式写入文件f，确保非ASCII字符正确处理，并设置缩进为2个空格
        json.dump(mapping, f, ensure_ascii=False, indent=2)


def _update_md_file(month_key: str, classified: dict, md_path: Path):
    """更新记账 md；legacy 服务复用当前通用导出逻辑。"""
    from workspace.accounting.bill_converter import update_md_file
    return update_md_file(month_key, classified, md_path)

    # 以下旧移植代码保留作历史参考，不再执行。
    import re
    from collections import defaultdict

    if not md_path.exists():
        logger.warning(f"文件不存在: {md_path}")
        return

    content = md_path.read_text(encoding="utf-8")
    month_data = classified.get(month_key, {})

    category_order_expense = ["吃", "学习资料", "娱乐", "生活", "交通", "电动车"]
    category_order_income = ["商业副业", "活动返现", "宿舍共享", "妈妈", "工资"]

    # 更新截止时间：使用该月最后一条记录的时间
    month_items = []
    for cat_items in month_data.values():
        month_items.extend(cat_items)
    if month_items:
        last_time = max(item["time"] for item in month_items)
        from datetime import datetime as dt
        last_dt = dt.strptime(last_time[:16], "%Y-%m-%d %H:%M")
        new_timestamp = f"截止到 {last_dt.strftime('%Y.%m.%d %H:%M')}"
    else:
        new_timestamp = None
    if new_timestamp:
        content = re.sub(r"^截止到\s.*$", new_timestamp, content, flags=re.MULTILINE)

def fmt_amount(amount):
    """格式化金额，如果金额是整数则返回整数形式的字符串，否则返回原样字符串。
    参数:
    amount: 要格式化的金额，可以是整数或浮点数。
    返回值:
    格式化后的字符串表示。
    """
    if amount == int(amount):  # 检查金额是否等于其整数部分，即是否为整数或无小数部分
        return str(int(amount))  # 如果是整数，返回整数形式的字符串
    return str(amount)  # 否则，直接返回原样字符串

    def sanitize_name(name):
        return name.replace("+", "").replace("-", "")

    # 电动车退款对冲
def process_ev_refunds(md):
        """
        处理电动车退款数据，将收费记录与退款记录配对，并生成格式化的字符串列表。

        参数:
        md (dict): 一个字典，包含以下键：
            - "电动车": 电动车收费记录列表
            - "电动车待对冲": 待对冲的收费记录列表
            - "电动车退款": 退款记录列表

        返回:
        list: 一个字符串列表，每个元素表示一个收费记录或收费与退款的配对，使用 fmt_amount 函数格式化金额。
        """
        # 从字典中提取收费记录，合并"电动车"和"电动车待对冲"两个列表
        charges = md.get("电动车", []) + md.get("电动车待对冲", [])
        # 从字典中提取退款记录列表
        refunds = md.get("电动车退款", [])
        # 按时间排序收费记录，以便后续配对
        charges.sort(key=lambda x: x["time"])
        # 按时间排序退款记录，以便与收费记录匹配
        refunds.sort(key=lambda x: x["time"])
        # 初始化结果列表，用于存储格式化的字符串
        parts = []
        # 初始化退款记录索引，用于在配对过程中跟踪位置
        refund_idx = 0
        # 遍历每个收费记录
        for charge in charges:
            # 初始化配对的退款记录为None
            paired_refund = None
            # 循环查找时间匹配的退款记录
            while refund_idx < len(refunds):
                refund = refunds[refund_idx]
                # 如果退款时间大于或等于收费时间，则认为配对成功
                if refund["time"] >= charge["time"]:
                    paired_refund = refund
                    # 移动索引到下一个退款记录，并跳出循环
                    refund_idx += 1
                    break
                # 如果不匹配，继续检查下一个退款记录
                refund_idx += 1
            # 根据是否找到配对退款，生成不同的格式化字符串
            if paired_refund:
                # 配对成功，生成带退款金额的字符串
                parts.append(f"{fmt_amount(charge['amount'])}电费-{fmt_amount(paired_refund['amount'])}电费")
            else:
                # 没有配对退款，只生成收费字符串
                parts.append(f"{fmt_amount(charge['amount'])}电费")
        # 返回最终结果列表
        return parts

def format_entry_part(item):
        """格式化条目的一部分，用于显示交易信息。

        参数：
        item (dict): 包含交易信息的字典，必须有键'amount'和'name'，可选键'direction'，默认为"支出"。

        返回：
        str: 格式化后的字符串，如果是收入则前缀加"+"，否则直接显示金额和名称。
        """
        # 格式化金额
        amount_str = fmt_amount(item["amount"])
        # 提取名称
        name = item["name"]
        # 获取方向，如果没有则默认为"支出"
        direction = item.get("direction", "支出")
        # 根据方向决定返回格式
        if direction == "收入":
            return f"+{amount_str}{name}"
        else:
            return f"{amount_str}{name}"

def format_category_line(items):
    """格式化分类行。

    参数：
    items (list): 包含分类项的列表。

    返回值：
    str: 用 "+" 连接的格式化字符串，如果 items 为空则返回空字符串。
    """
    if not items:
        return ""  # 检查 items 是否为空，如果是则返回空字符串
    # 遍历 items 中的每个 item，调用 format_entry_part 函数，并用 "+" 连接结果
    return "+".join(format_entry_part(item) for item in items)

def format_income_line(items):
    """格式化收入条目列表。

    将收入项目列表转换为格式化的字符串表示。

    Args:
        items (list): 收入项目列表，每个项目将由 `format_entry_part` 处理。

    Returns:
        str: 所有格式化后的收入项目拼接而成的字符串。若列表为空则返回空字符串。
    """
    if not items:  # 检查列表是否为空
        return ""
    return "".join(format_entry_part(item) for item in items)  # 遍历并格式化每个项目，最后拼接

    ev_parts = process_ev_refunds(month_data)
    ev_new = "+".join(ev_parts)

    for cat in category_order_expense:
        if cat == "电动车":
            new_content = ev_new
        else:
            items = month_data.get(cat, [])
            new_content = format_category_line(items)

        pattern = rf"(###### {re.escape(cat)}：)(.*)"
        match = re.search(pattern, content)
        if match:
            existing = match.group(2).strip()
            if existing and new_content:
                combined = existing + "+" + new_content
            elif new_content:
                combined = new_content
            else:
                combined = existing
            content = re.sub(pattern, f"###### {cat}：{combined}", content)

    for cat in category_order_income:
        items = month_data.get(cat, [])
        if items:
            new_content = format_income_line(items)
        else:
            if cat in ["妈妈", "工资"]:
                new_content = ""
            else:
                continue

        pattern = rf"(###### {re.escape(cat)}：)(.*)"
        match = re.search(pattern, content)
        if match:
            existing = match.group(2).strip()
            if existing and new_content:
                combined = existing + new_content
            elif new_content:
                combined = new_content
            else:
                combined = existing
            content = re.sub(pattern, f"###### {cat}：{combined}", content)

    md_path.write_text(content, encoding="utf-8")
    logger.info(f"已更新: {md_path}")


def _git_commit(month_keys: list[str]):
    """
    功能：执行Git提交，将指定月份的账单文件添加到暂存区并提交。

    参数：
    month_keys (list[str]): 月份键的列表，如 ['2023.01', '2023.02']。

    返回值：
    None
    """
    for mk in month_keys:
        # 生成文件名：将月份键中的点替换为连字符，并加上 .md 扩展名
        fname = f"{mk.replace('.', '-')}.md"
        # 将文件添加到Git暂存区
        subprocess.run(["git", "-C", str(ACCOUNTING_DIR), "add", fname], check=True)
    # 构建提交消息：使用中文顿号连接所有月份键
    msg = f"更新{'、'.join(month_keys)}账单"
    # 执行Git提交
    subprocess.run(["git", "-C", str(ACCOUNTING_DIR), "commit", "-m", msg], check=True)
    # 记录提交成功的日志
    logger.info(f"Git commit 成功: {msg}")


# === API 端点 ===

@app.get("/accounting/status", operation_id="accounting_status")
async def status():
    """记账审核模块状态"""
    review_data = _read_review_data()
    config = _read_config()
    return {
        "available": review_data is not None,
        "review_data_path": str(VAULT_DIR / REVIEW_JSON_FILE),
        "item_count": len(review_data["items"]) if review_data else 0,
        "generated_at": review_data.get("generated_at") if review_data else None,
        "config_categories": sum(len(v) for v in config.get("sectors", {}).values()),
        "config_descriptions": sum(len(v) for v in config.get("descriptions", {}).values()),
    }


@app.get("/accounting/review-data", operation_id="accounting_get_review_data")
async def get_review_data():
    """获取审核数据"""
    data = _read_review_data()
    if not data:
        raise HTTPException(status_code=404, detail="审核数据不存在，请先运行 bill_converter.py 生成")
    return data


@app.get("/accounting/config", operation_id="accounting_get_config")
async def get_config():
    """获取审核配置（板块/大类/说明文本列表）"""
    return _read_config()


@app.post("/accounting/config", operation_id="accounting_update_config")
async def update_config(req: ConfigUpdateRequest):
    """更新审核配置"""
    config = _read_config()
    if req.sectors is not None:
        config["sectors"] = req.sectors
    if req.descriptions is not None:
        config["descriptions"] = req.descriptions
    _write_config(config)
    return {"status": "ok", "message": "配置已更新"}


@app.post("/accounting/generate", operation_id="accounting_generate")
async def generate_review_data():
    """运行 bill_converter.py 生成审核数据"""
    if not BILL_CONVERTER.exists():
        raise HTTPException(status_code=500, detail=f"脚本不存在: {BILL_CONVERTER}")

    try:
        result = subprocess.run(
            [sys.executable, str(BILL_CONVERTER)],
            capture_output=True, text=True, encoding="utf-8", timeout=60,
            cwd=str(PROJECT_DIR),
        )
        if result.returncode != 0:
            logger.error(f"bill_converter.py 执行失败: {result.stderr}")
            raise HTTPException(status_code=500, detail=f"脚本执行失败: {result.stderr[:500]}")

        review_data = _read_review_data()
        return {
            "status": "ok",
            "message": "审核数据已生成",
            "item_count": len(review_data["items"]) if review_data else 0,
            "stdout": result.stdout[-500:] if result.stdout else "",
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="脚本执行超时（60秒）")


@app.post("/accounting/apply", operation_id="accounting_apply")
async def apply_confirmed(req: ApplyRequest):
    """应用用户确认的审核结果

    流程：
    1. 读取原始审核数据
    2. 根据用户确认重建 classified 数据
    3. 写入 Obsidian 记账 md
    4. Git commit
    5. 更新映射表
    6. 更新 review_config.json（新增大类/说明文本）
    """
    review_data = _read_review_data()
    if not review_data:
        raise HTTPException(status_code=404, detail="审核数据不存在")

    original_items = {item["id"]: item for item in review_data["items"]}
    mapping = review_data.get("mapping", _read_mapping())
    classified = review_data.get("classified", {})

    # 重建 classified：先清空，再从确认数据重建
    from collections import defaultdict
    new_classified = defaultdict(lambda: defaultdict(list))

    # 统计变更
    changes = []

    for confirmed in req.items:
        item_id = confirmed.id
        if item_id not in original_items:
            continue

        orig = original_items[item_id]
        new_sector = confirmed.sector
        new_category = confirmed.category
        new_description = confirmed.description

        # 跳过项不处理
        if orig.get("is_skip"):
            continue

        # 检查是否有变更
        changed = (new_category != orig["category"] or new_description != orig["description"])
        if changed:
            changes.append({
                "id": item_id,
                "old": f"{orig['category']}/{orig['description']}",
                "new": f"{new_category}/{new_description}",
            })

        # 添加到新的 classified
        month_key = orig["month_key"]
        # 空串说明文本：只写金额，不写名称
        clean_desc = new_description.replace("+", "").replace("-", "")
        entry = {
            "name": clean_desc,
            "amount": orig["amount"],
            "time": orig["time"],
            "direction": orig["direction"],
            "counterparty": orig.get("counterparty", ""),
            "product": orig.get("product", ""),
            "map_key": orig.get("map_key", ""),
            "trade_type": orig.get("trade_type", ""),
        }

        category = new_category

        new_classified[month_key][category].append(entry)

        # 更新映射表：所有变更都写入 fixed（包括✓标记的修正）
        if changed and orig.get("map_key"):
            mapping["fixed"][orig["map_key"]] = {
                "name": clean_desc,
                "category": new_category,
            }

    # 智能检查：同一原始名称的映射是否一致
    # 如果用户把同一 map_key 的多条记录改成了不同的值，取最后一次
    # 如果同一 counterparty 出现在多条记录中且用户统一修改了，也更新映射
    counterparty_mapping = {}  # {counterparty: {category, description}}
    for confirmed in req.items:
        item_id = confirmed.id
        if item_id not in original_items:
            continue
        orig = original_items[item_id]
        if orig.get("is_skip"):
            continue
        counterparty = orig.get("counterparty", "")
        if not counterparty:
            continue
        # 记录用户最终选择的映射
        counterparty_mapping[counterparty] = {
            "category": confirmed.category,
            "description": confirmed.description,
        }

    # 检查映射表中是否有与用户确认不一致的条目
    mapping_fixes = []
    for map_key, map_val in mapping.get("fixed", {}).items():
        # 从 map_key 中提取 counterparty
        parts = map_key.split("-", 1)
        if len(parts) < 2:
            continue
        counterparty = parts[1]
        if counterparty in counterparty_mapping:
            user_choice = counterparty_mapping[counterparty]
            if (map_val["category"] != user_choice["category"] or
                map_val["name"] != user_choice["description"].replace("+", "").replace("-", "")):
                # 映射表与用户最新选择不一致，更新
                old_val = f"{map_val['category']}/{map_val['name']}"
                map_val["category"] = user_choice["category"]
                map_val["name"] = user_choice["description"].replace("+", "").replace("-", "")
                new_val = f"{map_val['category']}/{map_val['name']}"
                mapping_fixes.append(f"{map_key}: {old_val} -> {new_val}")

    if mapping_fixes:
        logger.info(f"映射表智能修正 {len(mapping_fixes)} 条: {mapping_fixes}")

    # 写入映射表
    _write_mapping(mapping)

    # 写入 Obsidian md + git commit
    month_keys = sorted(new_classified.keys())
    for month_key in month_keys:
        md_path = ACCOUNTING_DIR / f"{month_key.replace('.', '-')}.md"
        _update_md_file(month_key, dict(new_classified), md_path)

    if month_keys:
        try:
            _git_commit(month_keys)
        except subprocess.CalledProcessError as e:
            logger.error(f"Git commit 失败: {e}")
            return {"status": "partial", "message": "记账文件已更新但 git commit 失败", "changes": changes}

    # 更新 review_config.json：添加新的大类和说明文本
    config = _read_config()
    config_updated = False

    for confirmed in req.items:
        item_id = confirmed.id
        if item_id not in original_items:
            continue
        orig = original_items[item_id]
        if orig.get("is_skip"):
            continue

        # 添加新大类到对应板块
        sector = confirmed.sector
        category = confirmed.category
        if sector in config.get("sectors", {}):
            if category not in config["sectors"][sector]:
                config["sectors"][sector].append(category)
                config_updated = True

        # 添加新说明文本到对应大类
        description = confirmed.description
        if category not in config.get("descriptions", {}):
            config["descriptions"][category] = []
        if description and description not in config["descriptions"][category]:
            config["descriptions"][category].append(description)
            config["descriptions"][category].sort()
            config_updated = True

    if config_updated:
        _write_config(config)

    # 清理审核数据文件
    review_json_path = VAULT_DIR / REVIEW_JSON_FILE
    if review_json_path.exists():
        review_json_path.unlink()

    return {
        "status": "ok",
        "message": f"已应用 {len(req.items)} 条记录，{len(changes)} 条变更",
        "changes": changes,
        "mapping_fixes": mapping_fixes,
        "months_updated": month_keys,
        "config_updated": config_updated,
    }


# === HTML 页面 serve ===

@app.get("/", response_class=HTMLResponse)
async def index():
    """记账审核页面入口"""
    if not HTML_FILE.exists():
        raise HTTPException(status_code=404, detail=f"页面不存在: {HTML_FILE}")
    return FileResponse(HTML_FILE, media_type="text/html")


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok", "service": "accounting_server", "port": 8780}


if __name__ == "__main__":
    print("=" * 60)
    print("LocalAgent Accounting Server")
    print(f"访问入口: http://127.0.0.1:8780/")
    print(f"健康检查: http://127.0.0.1:8780/health")
    print(f"账单目录: {ACCOUNTING_DIR}")
    print(f"审核数据: {VAULT_DIR / REVIEW_JSON_FILE}")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8780, log_level="info")
