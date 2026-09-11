r"""图片整理工具：Florence-2 caption+detailed+OCR → LLM 多维度分类 → move 文件 → SQLite 标签库
运行环境：主项目虚拟环境（.venv，CUDA torch + transformers，Florence-2 已随 OmniParser 安装）
运行命令：
  .venv\Scripts\python.exe tools\image_organizer\organize.py
  .venv\Scripts\python.exe tools\image_organizer\organize.py --limit 10 --dry-run  # 测试
需要后端运行（LLM 池）：start.bat

v2 改进：
- 3 个 Florence 任务（CAPTION + DETAILED_CAPTION + OCR），给 LLM 更多上下文
- 存储 Florence 原始完整输出（florence_raw 列，JSON）
- 提取 EXIF 元数据（exif_data 列）
- 传递目录路径 + 同目录文件数作为上下文
- 改进 prompt：强调二次元识别、IP 谨慎判断、使用图像尺寸作为提示
"""
import argparse
import concurrent.futures
import json
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

# ============ 配置 ============
API = "http://127.0.0.1:8766"
PROJECT = "图片整理"
CONCURRENCY = 60
MAX_TOKENS = 65536
COMMIT_EVERY = 100
# 默认模型（可用 --model 覆盖）
DEFAULT_MODEL = "microsoft/Florence-2-large"

TARGET_ROOT = Path(r"I:\<data_drive>:\<image_organize_output>")
OUTPUT_DIR = Path(r"<project_root>\output\image_organizer")
DB_PATH = OUTPUT_DIR / "files.db"
MAPPING_FILE = OUTPUT_DIR / "mapping.jsonl"

SOURCE_DIRS = [
    r"I:\<data_drive>:\<source_images_root>",
    r"I:\<data_drive>:\<classified_root>\图片-来自下载-乱",
    r"I:\<data_drive>:\<classified_root>\图片-Picture",
    r"I:\<data_drive>:\<classified_root>\图片",
    r"G:\<data_drive>:\<ipad_backup>",
]
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".gif", ".tif", ".tiff", ".jfif"}

CATEGORIES = {
    1: "二次元-角色立绘", 2: "二次元-场景插画", 3: "二次元-漫画条漫",
    4: "梗图搞笑", 5: "表情包", 6: "壁纸",
    7: "生活-人物照", 8: "生活-风景场景", 9: "社媒截图",
    10: "游戏截图", 11: "绘画参考", 12: "设计素材", 13: "无法分类",
}

SYSTEM_PROMPT = """你是图片分类与标签提取引擎。根据图片的路径、文件名、图像描述(caption/detailed_caption)、OCR文字、图像尺寸和目录上下文，输出结构化分类。

## 主分类（必须选一个编号）
1.二次元-角色立绘 2.二次元-场景插画 3.二次元-漫画条漫 4.梗图搞笑 5.表情包 6.壁纸 7.生活-人物照 8.生活-风景场景 9.社媒截图 10.游戏截图 11.绘画参考 12.设计素材 13.无法分类

## 分类要点（重要！）
- **二次元优先判断**：如果图像描述提到 anime/illustration/drawing/art/digital art/animated/character design 等词，或图像风格明显是手绘/数字绘画，优先归入二次元类别（1/2/3），而非生活类（7/8）
- **二次元 vs 生活照**：二次元图像通常有明显的线条、平涂色彩、夸张比例；生活照是真实相机拍摄。如果不确定，看图像尺寸——大尺寸高分辨率(>1500px)更可能是插画/壁纸
- **二次元-角色立绘**：动漫/游戏角色立绘、单人角色图、人物为主体
- **二次元-场景插画**：动漫风格场景、无明确主体的插画、风景类插画
- **二次元-漫画条漫**：漫画分镜、多格漫画、有对话气泡
- **梗图搞笑**：表情包、搞笑图、玩梗图（含或不含文字），通常尺寸较小
- **表情包**：QQ/微信表情、贴纸，尺寸很小
- **壁纸**：风景壁纸、抽象壁纸、桌面背景，通常高分辨率
- **生活-人物照**：真人照片、毕业照、自拍（确认是真实照片才归此）
- **生活-风景场景**：真实风景照、建筑照（确认是真实照片才归此）
- **社媒截图**：聊天记录、评论截图、帖子截图（文字为主）
- **游戏截图**：游戏内画面（3D游戏或2D游戏画面）
- **绘画参考**：教程图、姿势参考、人体参考、色彩参考、素描草图
- **设计素材**：logo、图标、模板、UI元素
- **无法分类**：信息不足或无法归入以上

## 多维度标签（能填就填，无则留空数组或空串）
- scene: 场景(室内/室外/演出现场/教室/自然风景/街头等)
- subject: 主体(人物/动物/物品/风景/文字等)
- props: 道具列表(吉他/电脑/手机/武器等)
- activity: 活动(音乐/运动/学习/工作等)
- style: 风格(写实/二次元/卡通/像素/油画等)
- mood: 情绪(欢乐/悲伤/温馨/紧张等)
- ip: 来源IP（**谨慎判断！只有明确识别出IP才填，如"原神""明日方舟""蔚蓝档案""Fate"等。不确定时留空，不要猜测！**）
- reference: 绘画参考维度(姿势/视角/构图/光影/人体/色彩等，适合学画参考的维度，无则空)
- colors: 主色调(暖色/冷色/黑白/鲜艳/低饱和等)

## 输出格式
严格输出一行JSON(不要markdown，不要多余文字)：
{"main_category":"二次元-角色立绘","main_category_id":1,"dimensions":{"scene":"...","subject":"...","props":[],"activity":"...","style":"...","mood":"...","ip":"...","reference":"...","colors":"..."},"tags":["标签1","标签2"],"confidence":0.8}

若内容涉及色情/违规被拒绝，输出 {"main_category":"无法分类","main_category_id":13,"refused":true,"dimensions":{},"tags":["审查拒绝"],"confidence":0}
若信息严重不足无法判断，main_category_id=13。"""


# ============ SQL ============
def init_db(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_path TEXT UNIQUE,
        source_root TEXT,
        rel_path TEXT,
        filename TEXT,
        size INTEGER,
        status TEXT DEFAULT 'pending',
        caption TEXT,
        detailed_caption TEXT,
        ocr_text TEXT,
        florence_raw TEXT,
        exif_data TEXT,
        width INTEGER,
        height INTEGER,
        main_category TEXT,
        main_category_id INTEGER,
        dimensions TEXT,
        tags TEXT,
        confidence REAL,
        target_path TEXT,
        error TEXT,
        florence_time REAL,
        llm_time REAL,
        created_at TEXT,
        updated_at TEXT
    )""")
    # 兼容旧库（v1 没有 detailed_caption/florence_raw/exif_data/width/height 列）
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
    for col, decl in [
        ("detailed_caption", "TEXT"), ("florence_raw", "TEXT"), ("exif_data", "TEXT"),
        ("width", "INTEGER"), ("height", "INTEGER"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} {decl}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON files(main_category_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_source ON files(source_root)")
    conn.commit()
    return conn


# ============ 扫描 ============
def scan_files():
    files = []
    for root in SOURCE_DIRS:
        root_path = Path(root)
        if not root_path.exists():
            print(f"  [跳过] 不存在: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过 macOS 资源叉目录
            dirnames[:] = [d for d in dirnames if d != "__MACOSX"]
            for fn in filenames:
                # 跳过 macOS 资源叉文件
                if fn.startswith("._"):
                    continue
                ext = os.path.splitext(fn)[1].lower()
                if ext in IMAGE_EXTS:
                    p = Path(dirpath) / fn
                    files.append((str(p), root, str(p.relative_to(root_path)), fn))
    return files


# ============ Florence-2 ============
def load_florence(model_name=DEFAULT_MODEL):
    import torch
    from PIL import Image
    from transformers import AutoModelForCausalLM, AutoProcessor
    print(f"  加载模型: {model_name}")
    print(f"  torch={torch.__version__}, cuda={torch.cuda.is_available()}")
    proc = AutoProcessor.from_pretrained(model_name, trust_remote_code=True, local_files_only=True)
    mdl = AutoModelForCausalLM.from_pretrained(
        model_name, torch_dtype=torch.float16, trust_remote_code=True, local_files_only=True
    ).to("cuda").eval()
    return proc, mdl, torch, Image


def run_task(proc, mdl, torch, image, task_prompt, max_tokens):
    inputs = proc(text=task_prompt, images=image, return_tensors="pt")
    mi = {}
    for k, v in inputs.items():
        mi[k] = v.to(device="cuda", dtype=torch.float16) if k == "pixel_values" else v.to("cuda")
    with torch.inference_mode():
        ids = mdl.generate(
            input_ids=mi["input_ids"], pixel_values=mi["pixel_values"],
            max_new_tokens=max_tokens, num_beams=2, do_sample=False,
        )
    text = proc.batch_decode(ids, skip_special_tokens=False)[0]
    parsed = proc.post_process_generation(text, task=task_prompt, image_size=(image.width, image.height))
    return parsed


def get_caption_ocr(proc, mdl, torch, Image, img_path):
    """3 个任务：CAPTION + DETAILED_CAPTION + OCR，返回完整信息"""
    img = Image.open(img_path).convert("RGB")
    width, height = img.width, img.height
    cap = run_task(proc, mdl, torch, img, "<CAPTION>", 96)
    detailed = run_task(proc, mdl, torch, img, "<DETAILED_CAPTION>", 160)
    ocr = run_task(proc, mdl, torch, img, "<OCR>", 256)
    cap_text = cap.get("<CAPTION>", "") if isinstance(cap, dict) else str(cap)
    detailed_text = detailed.get("<DETAILED_CAPTION>", "") if isinstance(detailed, dict) else str(detailed)
    ocr_text = ocr.get("<OCR>", "") if isinstance(ocr, dict) else str(ocr)
    raw = {"caption": cap, "detailed_caption": detailed, "ocr": ocr}
    return cap_text.strip(), detailed_text.strip(), ocr_text.strip(), raw, width, height


def extract_exif(img_path):
    """提取 EXIF 元数据（如果有）"""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(img_path) as img:
            exif_data = img._getexif()
            if not exif_data:
                return None
            result = {}
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ("Make", "Model", "DateTime", "DateTimeOriginal", "LensModel",
                           "FocalLength", "FNumber", "ExposureTime", "ISO", "Software"):
                    try:
                        result[tag] = str(value)
                    except Exception:
                        pass
            return result if result else None
    except Exception:
        return None


# ============ LLM ============
def call_llm(messages, max_tokens=MAX_TOKENS, timeout=120):
    payload = {
        "messages": messages, "temperature": 0.2, "max_tokens": max_tokens,
        "timeout": 100, "retries": 3, "project": PROJECT,
    }
    empty_retry = 0
    rl_retry = 0
    MAX_RL = 5
    while True:
        try:
            r = requests.post(f"{API}/llm/pool/call", json=payload, timeout=timeout)
            if r.status_code != 200:
                if rl_retry < MAX_RL:
                    rl_retry += 1
                    time.sleep(15)
                    continue
                return None, f"HTTP {r.status_code}: {r.text[:200]}"
            data = r.json()
            if data.get("ok"):
                content = data.get("content", "")
                if not content.strip() and empty_retry < 1:
                    empty_retry += 1
                    time.sleep(3)
                    continue
                return content, None
            error = data.get("error", "unknown")
            if "429" in error or "rate_limited" in error:
                if rl_retry < MAX_RL:
                    rl_retry += 1
                    time.sleep(60)
                    continue
                return None, error
            return None, error
        except Exception as e:
            if rl_retry < MAX_RL:
                rl_retry += 1
                time.sleep(10)
                continue
            return None, f"{type(e).__name__}: {str(e)[:200]}"


def parse_json(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            pass
    return None


# ============ 文件移动 ============
def unique_target(target):
    if not target.exists():
        return target
    stem, ext = target.stem, target.suffix
    i = 1
    while True:
        p = target.parent / f"{stem}_{i}{ext}"
        if not p.exists():
            return p
        i += 1


def move_file(source, main_category, target_root):
    cat_dir = target_root / main_category
    cat_dir.mkdir(parents=True, exist_ok=True)
    src = Path(source)
    target = unique_target(cat_dir / src.name)
    shutil.move(str(src), str(target))
    return str(target)


# ============ 主流程 ============
def main():
    parser = argparse.ArgumentParser(description="图片整理：Florence-2 + LLM 分类 + move")
    parser.add_argument("--limit", type=int, default=0, help="只处理N个文件（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="不移动文件，只跑 Florence-2 + LLM + SQL")
    parser.add_argument("--force-florence", action="store_true", help="强制重跑 Florence-2（忽略已有 caption）")
    parser.add_argument("--target", type=str, default=str(TARGET_ROOT), help="目标目录")
    parser.add_argument("--random", action="store_true", help="随机采样（配合 --limit 使用，从各来源随机抽取）")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help=f"视觉模型 (默认: {DEFAULT_MODEL})")
    args = parser.parse_args()

    target_root = Path(args.target)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = init_db(DB_PATH)
    lock = threading.Lock()

    # 检查后端
    try:
        r = requests.get(f"{API}/llm/pool/status", timeout=10)
        st = r.json()
        print(f"LLM 池: keys={st.get('total_keys')}, active={st.get('active_keys')}")
    except Exception as e:
        print(f"后端不可用: {e}，请先运行 start.bat")
        return

    # 扫描文件
    print("扫描文件...")
    files = scan_files()
    print(f"共 {len(files)} 个图片文件")

    # 注册到 DB
    new_count = 0
    for source, root, rel, fn in files:
        try:
            size = os.path.getsize(source)
        except OSError:
            size = 0
        with lock:
            cur = conn.execute(
                "INSERT OR IGNORE INTO files (source_path, source_root, rel_path, filename, size, status, created_at) VALUES (?,?,?,?,?,'pending',?)",
                (source, root, rel, fn, size, datetime.now().isoformat()),
            )
            new_count += cur.rowcount
    conn.commit()
    print(f"新增注册: {new_count}")

    # 查询待处理
    if args.force_florence:
        with lock:
            conn.execute("UPDATE files SET status='pending' WHERE status IN ('florence_done','failed')")
            conn.commit()

    if args.random and args.limit > 0:
        # 随机采样：从各来源均匀抽取
        per_source = max(1, args.limit // len(SOURCE_DIRS))
        rows = []
        for src in SOURCE_DIRS:
            r = conn.execute(
                "SELECT source_path, rel_path, filename, status FROM files "
                "WHERE status IN ('pending','florence_done','llm_done','failed') AND source_root=? "
                f"ORDER BY RANDOM() LIMIT {per_source}",
                (src,)
            ).fetchall()
            rows.extend(r)
        # 如果某个来源不够，补充其他来源
        if len(rows) < args.limit:
            extra = conn.execute(
                "SELECT source_path, rel_path, filename, status FROM files "
                "WHERE status IN ('pending','florence_done','llm_done','failed') "
                f"ORDER BY RANDOM() LIMIT {args.limit - len(rows)}"
            ).fetchall()
            rows.extend(extra)
        rows = rows[:args.limit]
    else:
        rows = conn.execute(
            "SELECT source_path, rel_path, filename, status FROM files WHERE status IN ('pending','florence_done','llm_done','failed') ORDER BY id"
        ).fetchall()
        if args.limit > 0:
            rows = rows[:args.limit]
    total = len(rows)
    print(f"待处理: {total}")

    if total == 0:
        print("全部已处理")
        generate_report(conn)
        return

    # 加载视觉模型
    print(f"加载视觉模型: {args.model}")
    proc, mdl, torch, Image = load_florence(args.model)

    # 统计
    stats = {"florence": 0, "llm": 0, "moved": 0, "failed": 0, "skipped": 0}
    mapping_fp = open(MAPPING_FILE, "a", encoding="utf-8")
    commit_counter = [0]
    stop = [False]
    start = time.time()

    def process_llm(source_path, caption, detailed_caption, ocr, rel_path, filename, width, height, exif_data, dir_context):
        """LLM 分类 + move，在 worker 线程执行"""
        if stop[0]:
            return
        user_msg = (
            f"## 文件路径\n{rel_path}\n\n## 文件名\n{filename}\n\n"
            f"## 图像尺寸\n{width}x{height}px\n\n"
            f"## 目录上下文\n{dir_context}\n\n"
            f"## 图像描述(caption)\n{caption}\n\n"
            f"## 详细描述(detailed_caption)\n{detailed_caption if detailed_caption else '(无)'}\n\n"
            f"## OCR文字\n{ocr if ocr else '(无)'}\n\n"
            f"## EXIF元数据\n{json.dumps(exif_data, ensure_ascii=False) if exif_data else '(无)'}"
        )
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_msg}]
        t0 = time.time()
        content, err = call_llm(messages)
        llm_time = time.time() - t0
        now = datetime.now().isoformat()
        if err:
            with lock:
                conn.execute(
                    "UPDATE files SET status='florence_done', error=?, llm_time=?, updated_at=? WHERE source_path=?",
                    (err, llm_time, now, source_path),
                )
                stats["failed"] += 1
            return
        result = parse_json(content)
        if not result:
            with lock:
                conn.execute(
                    "UPDATE files SET status='florence_done', error='JSON解析失败', llm_time=?, updated_at=? WHERE source_path=?",
                    (llm_time, now, source_path),
                )
                stats["failed"] += 1
            return
        cat_id = result.get("main_category_id", 13)
        try:
            cat_id = int(cat_id)
        except (TypeError, ValueError):
            cat_id = 13
        cat_name = CATEGORIES.get(cat_id, "无法分类")
        dimensions = result.get("dimensions", {}) or {}
        tags = result.get("tags", []) or []
        confidence = result.get("confidence", 0)
        refused = result.get("refused", False)

        target = None
        move_err = None
        if not args.dry_run and not refused:
            try:
                target = move_file(source_path, cat_name, target_root)
            except Exception as e:
                move_err = f"move失败: {e}"

        with lock:
            if target:
                mapping = {
                    "source_path": source_path, "target_path": target,
                    "main_category": cat_name, "dimensions": dimensions, "tags": tags,
                }
                mapping_fp.write(json.dumps(mapping, ensure_ascii=False) + "\n")
                mapping_fp.flush()
                conn.execute(
                    "UPDATE files SET status='moved', main_category=?, main_category_id=?, dimensions=?, tags=?, confidence=?, target_path=?, llm_time=?, updated_at=? WHERE source_path=?",
                    (cat_name, cat_id, json.dumps(dimensions, ensure_ascii=False),
                     json.dumps(tags, ensure_ascii=False), confidence, target, llm_time, now, source_path),
                )
                stats["moved"] += 1
            else:
                conn.execute(
                    "UPDATE files SET status='llm_done', main_category=?, main_category_id=?, dimensions=?, tags=?, confidence=?, error=?, llm_time=?, updated_at=? WHERE source_path=?",
                    (cat_name, cat_id, json.dumps(dimensions, ensure_ascii=False),
                     json.dumps(tags, ensure_ascii=False), confidence, move_err or "dry-run", llm_time, now, source_path),
                )
            stats["llm"] += 1
            commit_counter[0] += 1
            if commit_counter[0] % COMMIT_EVERY == 0:
                conn.commit()

    # 主循环：Florence-2 串行 + LLM 并发
    print(f"开始处理，并发 {CONCURRENCY}，dry-run={args.dry_run}")
    futures = set()
    # 目录文件数缓存（用于上下文）
    dir_file_count = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            processed = 0
            for source_path, rel_path, filename, status in rows:
                if stop[0]:
                    break

                if status == "pending" or status == "failed":
                    # Florence-2 (3 tasks: CAPTION + DETAILED_CAPTION + OCR)
                    t0 = time.time()
                    try:
                        caption, detailed_caption, ocr, raw, width, height = get_caption_ocr(proc, mdl, torch, Image, source_path)
                        exif_data = extract_exif(source_path)
                        ft = time.time() - t0
                        with lock:
                            conn.execute(
                                "UPDATE files SET status='florence_done', caption=?, detailed_caption=?, ocr_text=?, florence_raw=?, exif_data=?, width=?, height=?, florence_time=?, error=NULL, updated_at=? WHERE source_path=?",
                                (caption, detailed_caption, ocr, json.dumps(raw, ensure_ascii=False),
                                 json.dumps(exif_data, ensure_ascii=False) if exif_data else None,
                                 width, height, ft, datetime.now().isoformat(), source_path),
                            )
                        stats["florence"] += 1
                    except Exception as e:
                        with lock:
                            conn.execute(
                                "UPDATE files SET status='failed', error=?, updated_at=? WHERE source_path=?",
                                (f"Florence失败: {e}", datetime.now().isoformat(), source_path),
                            )
                        stats["failed"] += 1
                        continue
                else:
                    # florence_done / llm_done，读已有数据
                    row = conn.execute(
                        "SELECT caption, detailed_caption, ocr_text, width, height, exif_data FROM files WHERE source_path=?", (source_path,)
                    ).fetchone()
                    if not row or not row[0]:
                        # caption 缺失，重跑 Florence
                        try:
                            caption, detailed_caption, ocr, raw, width, height = get_caption_ocr(proc, mdl, torch, Image, source_path)
                            exif_data = extract_exif(source_path)
                            with lock:
                                conn.execute(
                                    "UPDATE files SET caption=?, detailed_caption=?, ocr_text=?, florence_raw=?, exif_data=?, width=?, height=?, updated_at=? WHERE source_path=?",
                                    (caption, detailed_caption, ocr, json.dumps(raw, ensure_ascii=False),
                                     json.dumps(exif_data, ensure_ascii=False) if exif_data else None,
                                     width, height, datetime.now().isoformat(), source_path),
                                )
                        except Exception as e:
                            with lock:
                                conn.execute(
                                    "UPDATE files SET status='failed', error=?, updated_at=? WHERE source_path=?",
                                    (f"Florence重试失败: {e}", datetime.now().isoformat(), source_path),
                                )
                            stats["failed"] += 1
                            continue
                    else:
                        caption = row[0]
                        detailed_caption = row[1] or ""
                        ocr = row[2] or ""
                        width = row[3] or 0
                        height = row[4] or 0
                        exif_data = json.loads(row[5]) if row[5] else None
                        stats["skipped"] += 1

                # 构造目录上下文
                dir_path = str(Path(source_path).parent)
                if dir_path not in dir_file_count:
                    try:
                        dir_file_count[dir_path] = len([f for f in os.listdir(dir_path) if os.path.splitext(f)[1].lower() in IMAGE_EXTS])
                    except Exception:
                        dir_file_count[dir_path] = 0
                dir_context = f"目录: {Path(rel_path).parent} (同目录约{dir_file_count[dir_path]}个图片文件)"

                # 提交 LLM 任务
                f = ex.submit(process_llm, source_path, caption, detailed_caption, ocr, rel_path, filename, width, height, exif_data, dir_context)
                futures.add(f)

                # 清理已完成 future（控制内存）
                if len(futures) > 500:
                    done = {f for f in futures if f.done()}
                    for f in done:
                        try:
                            f.result()
                        except Exception as e:
                            print(f"  [worker异常] {e}")
                    futures -= done

                processed += 1
                if processed % 100 == 0:
                    el = time.time() - start
                    rate = processed / el if el > 0 else 0
                    eta = (total - processed) / rate / 3600 if rate > 0 else 0
                    pending_llm = len(futures)
                    print(
                        f"  进度: {processed}/{total} ({processed / total * 100:.1f}%) "
                        f"速率:{rate:.1f}/s ETA:{eta:.1f}h 待LLM:{pending_llm} "
                        f"fl:{stats['florence']} llm:{stats['llm']} moved:{stats['moved']} fail:{stats['failed']} skip:{stats['skipped']}"
                    )
                    with lock:
                        conn.commit()

            # 等待剩余 LLM
            print(f"Florence-2 完成，等待 {len(futures)} 个 LLM 任务...")
            for f in concurrent.futures.as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    print(f"  [worker异常] {e}")
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，停止提交新任务，等待进行中的 LLM 完成...")
        stop[0] = True
        for f in concurrent.futures.as_completed(futures):
            try:
                f.result()
            except Exception:
                pass

    conn.commit()
    mapping_fp.close()
    el = time.time() - start
    print(f"\n本次耗时: {el / 3600:.1f}h")
    print(f"统计: florence={stats['florence']} llm={stats['llm']} moved={stats['moved']} failed={stats['failed']} skipped={stats['skipped']}")
    generate_report(conn)


def generate_report(conn):
    """分类统计报告"""
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    by_status = conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
    by_cat = conn.execute(
        "SELECT main_category, COUNT(*) FROM files WHERE status IN ('moved','llm_done') GROUP BY main_category_id ORDER BY COUNT(*) DESC"
    ).fetchall()
    print(f"\n=== 总计 {total} 文件 ===")
    print("状态分布:")
    for s, n in by_status:
        print(f"  {s:15s} {n}")
    print("分类分布:")
    for cat, n in by_cat:
        print(f"  {cat or '未分类':20s} {n}")


if __name__ == "__main__":
    main()
