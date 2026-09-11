"""
微信收藏导出整理脚本

将 temp/微信收藏导出 下的内容整理为可读的 md 格式：
- 聊天记录 txt（图文混编）→ md，图片复制到 /imgs
- [链接] txt → md（含超链接）
- [文本]/[小程序]/[视频号] txt → 原样复制
- .jpg/.png → 原样复制
- .pdf/.doc/.docx/.xls/.xlsx/.mp4 → 复制到 /files

输出结构：
temp/收藏整理/
├── 2024/
│   ├── *.md / *.txt / *.jpg / *.png  (扁平)
│   ├── imgs/   (聊天记录 md 引用的图片)
│   └── files/  (其他格式文件)
└── 2025/
    └── ...
"""
import re
import shutil
import sys
from pathlib import Path

# 修复 Windows 控制台编码问题
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 源目录
SRC_ROOT = Path(r"<project_root>\temp\微信收藏导出")
# 输出目录
DST_ROOT = Path(r"<project_root>\temp\收藏整理")

# 子文件夹映射：源文件夹名 → 输出子文件夹名 + 推断年份
SUBFOLDERS = {
    "微信导出2024": ("2024", 2024),
    "微信收藏备份——2025-09-30": ("2025", 2025),
}

# 图片扩展名
IMG_EXTS = {".jpg", ".png", ".jpeg", ".gif", ".bmp", ".webp"}
# 其他文件扩展名（放到 files/）
OTHER_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".mp4", ".m4a", ".mp3", ".wav", ".zip", ".unitypackage"}


def read_text(path: Path) -> str:
    """读取文本文件，自动处理 BOM"""
    return path.read_text(encoding="utf-8-sig", errors="replace")


def parse_chat_record(txt_path: Path, default_year: int):
    """
    解析聊天记录 txt，返回消息列表。

    文件格式：
        发送者\t日期时间
        (空行)
        内容（可能是图片文件名、文本、[动画表情]等）
        (空行)
        下一个发送者\t日期时间
        ...

    逐行解析：遇到 "发送者\t日期时间" 行开始新消息，后续非空行作为内容。

    返回：[(发送者, 日期时间字符串, [内容行]), ...]
    """
    text = read_text(txt_path)

    # 从文件名推断年份
    fname = txt_path.stem
    year_match = re.search(r"(\d{4})年", fname)
    file_year = int(year_match.group(1)) if year_match else default_year

    messages = []
    current_sender = None
    current_dt = None
    current_content = []

    # 匹配消息头行：发送者\t日期时间
    # 日期时间格式：01-03 10:48:00 / 4-9 下午12:41 / 09-19 11:26:04
    header_pattern = re.compile(r"^(.+?)\t(\d{1,2}-\d{1,2}\s+(?:上午|下午)?\s*\d{1,2}(?::\d{2}){0,2})\s*$")

    for line in text.split("\n"):
        line = line.rstrip("\r")
        m = header_pattern.match(line)
        if m:
            # 新消息开始：保存前一条消息
            if current_sender is not None:
                messages.append((current_sender, current_dt, current_content))
            current_sender = m.group(1).strip()
            current_dt = normalize_datetime(m.group(2).strip(), file_year)
            current_content = []
        else:
            stripped = line.strip()
            if stripped:
                current_content.append(stripped)

    # 保存最后一条消息
    if current_sender is not None:
        messages.append((current_sender, current_dt, current_content))

    return messages


def normalize_datetime(dt_str: str, year: int) -> str:
    """
    将日期时间字符串规范化为 YYYY-MM-DD HH-MM-SS 格式。

    输入格式：
    - "01-03 10:48:00"  (MM-DD HH:MM:SS)
    - "4-9 下午12:41"   (MM-DD 上午/下午H:MM)
    - "09-19 11:26:04" (MM-DD HH:MM:SS)
    """
    dt_str = dt_str.strip()
    # 匹配 "MM-DD HH:MM:SS"
    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})", dt_str)
    if m:
        mo, d, h, mi, s = m.groups()
        return f"{year:04d}-{int(mo):02d}-{int(d):02d}ㅤ{int(h):02d}-{int(mi):02d}-{int(s):02d}"
    # 匹配 "MM-DD 上午/下午H:MM"
    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(上午|下午)(\d{1,2}):(\d{2})", dt_str)
    if m:
        mo, d, ampm, h, mi = m.groups()
        h = int(h)
        if ampm == "下午" and h < 12:
            h += 12
        elif ampm == "上午" and h == 12:
            h = 0
        return f"{year:04d}-{int(mo):02d}-{int(d):02d}ㅤ{int(h):02d}-{int(mi):02d}-00"
    # 匹配 "MM-DD HH:MM"
    m = re.match(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", dt_str)
    if m:
        mo, d, h, mi = m.groups()
        return f"{year:04d}-{int(mo):02d}-{int(d):02d}ㅤ{int(h):02d}-{int(mi):02d}-00"
    # 无法解析，原样返回
    return dt_str


def is_image_filename(line: str) -> bool:
    """判断一行是否是图片文件名（如 1.png, 2.jpg）"""
    line = line.strip()
    return bool(re.match(r"^[\w\-]+\.(png|jpg|jpeg|gif|bmp|webp)$", line, re.IGNORECASE))


def is_url(line: str) -> bool:
    """判断一行是否是 URL"""
    line = line.strip()
    return line.startswith("http://") or line.startswith("https://")


def convert_chat_to_md(txt_path: Path, default_year: int, imgs_dir: Path, chat_name: str) -> str:
    """
    将聊天记录 txt 转为 md 字符串，图片复制到 imgs_dir。

    chat_name: 聊天记录名称（用于图片重命名前缀）
    """
    messages = parse_chat_record(txt_path, default_year)
    if not messages:
        return ""

    # 同名图片目录
    img_src_dir = txt_path.parent / txt_path.stem

    lines_out = []
    # 收集所有图片文件名（用于重命名避免冲突）
    img_counter = 0

    for sender, dt, content_lines in messages:
        # 消息头
        lines_out.append(f"###### 【{sender}】ㅤ{dt}")

        if not content_lines:
            # 空内容
            lines_out.append("")
            lines_out.append("")
            continue

        # 处理内容行
        has_content = False
        for line in content_lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            has_content = True
            if is_image_filename(line_stripped):
                # 图片：复制到 imgs/，重命名为 chat_name_N.ext
                img_counter += 1
                ext = Path(line_stripped).suffix.lower()
                new_img_name = f"{chat_name}_{img_counter}{ext}"
                src_img = img_src_dir / line_stripped
                if src_img.exists():
                    dst_img = imgs_dir / new_img_name
                    if not dst_img.exists():
                        shutil.copy2(src_img, dst_img)
                lines_out.append(f"![图片](./imgs/{new_img_name})")
            elif is_url(line_stripped):
                # URL：转为链接
                lines_out.append(f"[链接]({line_stripped})")
            elif line_stripped == "[动画表情]":
                lines_out.append("`[动画表情]`")
            else:
                # 普通文本：用反引号包裹
                # 多行文本合并处理
                lines_out.append(f"`{line_stripped}`")

        if not has_content:
            lines_out.append("")

        lines_out.append("")

    # 去除末尾多余空行
    while lines_out and lines_out[-1] == "":
        lines_out.pop()

    return "\n".join(lines_out) + "\n"


def convert_link_to_md(txt_path: Path) -> str:
    """将 [链接] txt 转为 md"""
    text = read_text(txt_path).strip()
    if not text:
        return ""

    # 文件名中的名称（去掉 [链接] 前缀和日期后缀）
    fname = txt_path.stem
    # [链接]Cui_2022年11月18日 → Cui
    name_match = re.match(r"\[链接\](.+?)(?:_(\d{4}年\d{1,2}月\d{1,2}日|今天|星期[一二三四五六日天]))?$", fname)
    title = name_match.group(1) if name_match else fname

    lines = []
    lines.append(f"# {title}")
    lines.append("")
    # 如果是单行 URL
    url_lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(url_lines) == 1 and is_url(url_lines[0]):
        lines.append(f"[{title}]({url_lines[0]})")
        lines.append("")
        lines.append(f"原始链接：{url_lines[0]}")
    else:
        # 多行内容
        for line in url_lines:
            if is_url(line):
                lines.append(f"[链接]({line})")
            else:
                lines.append(line)
    lines.append("")
    return "\n".join(lines)


def safe_copy(src: Path, dst: Path):
    """安全复制文件，如果目标已存在则加序号"""
    if not dst.exists():
        shutil.copy2(src, dst)
        return dst.name
    # 加序号
    stem = dst.stem
    suffix = dst.suffix
    for i in range(1, 1000):
        new_dst = dst.parent / f"{stem}_{i}{suffix}"
        if not new_dst.exists():
            shutil.copy2(src, new_dst)
            return new_dst.name
    return dst.name


def process_subfolder(src_subfolder: Path, dst_subfolder: Path, default_year: int):
    """处理一个子文件夹"""
    print(f"\n=== 处理: {src_subfolder.name} → {dst_subfolder.name} ===")

    # 创建子目录
    imgs_dir = dst_subfolder / "imgs"
    files_dir = dst_subfolder / "files"
    imgs_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    stats = {"chat_md": 0, "link_md": 0, "txt_copy": 0, "img_copy": 0, "file_copy": 0, "skip": 0}

    # 遍历所有文件
    for item in sorted(src_subfolder.rglob("*")):
        if item.is_dir():
            continue
        rel_path = item.relative_to(src_subfolder)
        # 跳过 聊天记录/ 下的图片目录（已通过 txt 处理）
        # 但要处理 聊天记录/ 下的 txt

        # 判断文件类型
        ext = item.suffix.lower()
        fname = item.name

        # 聊天记录 txt（在 聊天记录/ 目录下，且不是 [文本]/[链接] 等前缀）
        is_chat_record = ("聊天记录" in str(rel_path.parent)) and (not fname.startswith("["))
        is_link_txt = fname.startswith("[链接]")
        is_other_prefix_txt = fname.startswith("[文本]") or fname.startswith("[小程序]") or fname.startswith("[视频号]")

        if ext == ".txt" and is_chat_record:
            # 聊天记录 → md
            chat_name = item.stem
            try:
                md_content = convert_chat_to_md(item, default_year, imgs_dir, chat_name)
                if md_content:
                    md_path = dst_subfolder / f"{chat_name}.md"
                    md_path.write_text(md_content, encoding="utf-8")
                    stats["chat_md"] += 1
                    print(f"  [聊天→md] {fname} → {chat_name}.md")
                else:
                    print(f"  [跳过-空] {fname}")
                    stats["skip"] += 1
            except Exception as e:
                print(f"  [错误] {fname}: {e}")
                stats["skip"] += 1
        elif ext == ".txt" and is_link_txt:
            # [链接] txt → md
            try:
                md_content = convert_link_to_md(item)
                if md_content:
                    md_name = item.stem + ".md"
                    md_path = dst_subfolder / md_name
                    md_path.write_text(md_content, encoding="utf-8")
                    stats["link_md"] += 1
                    print(f"  [链接→md] {fname} → {md_name}")
                else:
                    print(f"  [跳过-空链接] {fname}")
                    stats["skip"] += 1
            except Exception as e:
                print(f"  [错误] {fname}: {e}")
                stats["skip"] += 1
        elif ext == ".txt":
            # 其他 txt（[文本]/[小程序]/[视频号] 等）→ 原样复制
            dst = dst_subfolder / fname
            safe_copy(item, dst)
            stats["txt_copy"] += 1
            print(f"  [txt复制] {fname}")
        elif ext in IMG_EXTS:
            # 图片 → 原样复制（非聊天记录目录下的图片）
            # 聊天记录目录下的图片已通过 txt 处理，跳过
            if "聊天记录" in str(rel_path.parent):
                # 聊天记录目录下的图片，已通过 txt 处理时复制
                # 但如果对应的 txt 不存在，也要复制
                # 这里跳过，因为已通过 txt 处理
                continue
            dst = dst_subfolder / fname
            safe_copy(item, dst)
            stats["img_copy"] += 1
            print(f"  [图片复制] {fname}")
        elif ext in OTHER_EXTS:
            # 其他文件 → files/
            dst = files_dir / fname
            safe_copy(item, dst)
            stats["file_copy"] += 1
            print(f"  [文件→files] {fname}")
        else:
            print(f"  [未知类型] {fname} (ext={ext})")
            stats["skip"] += 1

    print(f"\n统计: {stats}")
    return stats


def main():
    # 清空输出目录（如果存在）
    if DST_ROOT.exists():
        print(f"清空旧输出目录: {DST_ROOT}")
        shutil.rmtree(DST_ROOT)
    DST_ROOT.mkdir(parents=True)

    total_stats = {}
    for src_name, (dst_name, default_year) in SUBFOLDERS.items():
        src_sub = SRC_ROOT / src_name
        dst_sub = DST_ROOT / dst_name
        if not src_sub.exists():
            print(f"源文件夹不存在: {src_sub}")
            continue
        dst_sub.mkdir(parents=True, exist_ok=True)
        stats = process_subfolder(src_sub, dst_sub, default_year)
        total_stats[src_name] = stats

    print("\n=== 全部完成 ===")
    for name, stats in total_stats.items():
        print(f"{name}: {stats}")
    print(f"输出目录: {DST_ROOT}")


if __name__ == "__main__":
    main()
