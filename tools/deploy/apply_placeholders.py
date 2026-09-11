#!/usr/bin/env python3
"""
apply_placeholders.py - 占位符自动替换脚本

用途：
    将 friend-full profile 导出包中的占位符（<project_root>、<username>、<data_drive>
    等）替换为接收方用户实际的路径与名称。用于导出包的部署阶段。

用法：
    交互式（依次询问必填占位符，可选占位符直接回车跳过）：
        python tools/deploy/apply_placeholders.py <export_dir>

    命令行（必填通过参数提供）：
        python tools/deploy/apply_placeholders.py <export_dir> ^
            --project-root D:\\code\\localAgent ^
            --username your_name ^
            --data-drive D ^
            [--dry-run] [--skip-optional]

    预演模式（只输出将修改的文件清单和替换次数，不实际写入）：
        python tools/deploy/apply_placeholders.py <export_dir> --dry-run ^
            --project-root D:\\code\\localAgent --username your_name --data-drive D

注意：
    <data_drive> 应传盘符字母（如 D），不要带冒号。导出包中占位符已含冒号
    （如 <data_drive>:\\<data_drive>:\Documents），传 D 才能得到 D:\\<data_drive>:\Documents；若传 D: 会产生
    D::\\<data_drive>:\Documents 双冒号异常路径，脚本会检测并报错退出。

    占位符清单需与 release/profiles/friend-full.toml 的
    [deployment_mapping.required_mappings] 段保持同步。

约束：
    - 只用 Python 标准库
    - 用 str.replace 做替换，禁止正则（避免反斜杠转义问题）
    - 只处理文本文件，跳过二进制和指定目录
    - UTF-8 编码读写，保留原换行符风格
    - 路径值自动规范化：所有替换值中的反斜杠 \\ 会被转为正斜杠 /，
      避免 Windows 单反斜杠路径写入 TOML 后触发非法转义（如 \\T）。
      正斜杠在 Windows 上兼容，TOML/JSON/Python 均不解析 / 为转义符。
"""
import argparse
import os
import sys

# 占位符清单
# 此清单需与 release/profiles/friend-full.toml 的 [deployment_mapping.required_mappings] 保持同步
PLACEHOLDERS = [
    # 必填占位符
    {"placeholder": "<username>", "required": True,
     "prompt": "本地 Windows 用户名 (例: your_name)"},
    {"placeholder": "<project_root>", "required": True,
     "prompt": "LocalAgent 项目根目录的绝对路径 (例: D:\\code\\localAgent)"},
    {"placeholder": "<data_drive>", "required": True,
     "prompt": "数据盘盘符字母 (例: D，不要带冒号)"},
    # 可选占位符
    {"placeholder": "<project_root_parent>", "required": False,
     "prompt": "项目根目录的父目录 (例: D:\\code)"},
    {"placeholder": "<external_project_root>", "required": False,
     "prompt": "外部项目存放目录 (例: D:\\external_projects)"},
    {"placeholder": "<source_images_root>", "required": False,
     "prompt": "图片源根目录 (例: <data_drive>:\Pictures\\collection)"},
    {"placeholder": "<classified_root>", "required": False,
     "prompt": "已分类图片根目录 (例: <data_drive>:\Pictures\\classified)"},
    {"placeholder": "<image_organize_output>", "required": False,
     "prompt": "image_organizer 输出目录 (例: output\\image_organizer)"},
    {"placeholder": "<bilibili_videos>", "required": False,
     "prompt": "B 站视频归档目录 (例: Videos\\bilibili)"},
    {"placeholder": "<bilibili_videos_alt>", "required": False,
     "prompt": "B 站视频归档备用目录 (例: Videos\\bilibili_alt)"},
    {"placeholder": "<ipad_backup>", "required": False,
     "prompt": "iPad 备份目录 (例: Backup\\ipad)"},
    {"placeholder": "<system_data_root>", "required": False,
     "prompt": "系统数据根目录 (例: SystemData)"},
    {"placeholder": "<backup_root>", "required": False,
     "prompt": "备份根目录 (例: Backups)"},
    {"placeholder": "<miniconda_root>", "required": False,
     "prompt": "Miniconda 安装根目录 (例: Miniconda3)"},
    {"placeholder": "<academic_root>", "required": False,
     "prompt": "学业资料根目录 (例: Academic)"},
    {"placeholder": "<articles_root>", "required": False,
     "prompt": "文字篇章根目录 (例: Articles)"},
    {"placeholder": "<working_root>", "required": False,
     "prompt": "工作目录根 (例: Working)"},
    {"placeholder": "<projects_root>", "required": False,
     "prompt": "项目目录根 (例: Projects)"},
    {"placeholder": "<bilibili_organized_output>", "required": False,
     "prompt": "B 站视频整理输出目录 (例: output\\bilibili_organized)"},
    {"placeholder": "<game_install_root>", "required": False,
     "prompt": "游戏安装根目录 (例: Games)"},
    {"placeholder": "<copyright_holder>", "required": False,
     "prompt": "版权持有人名称 (例: Your-Name)"},
]

# 只处理这些扩展名的文本文件
TEXT_EXTENSIONS = {".py", ".toml", ".bat", ".md", ".json", ".html", ".qss", ".txt", ".cfg", ".ini"}

# 跳过这些目录（不递归进入）
SKIP_DIRS = {".venv", "weights", "data", "chrome_debug", ".git", "temp", "__pycache__"}

# 双冒号异常路径标记（Python 字符串 "::\\" 即 3 字符 ::\ ）
DOUBLE_COLON = "::\\"


def is_text_file(file_path):
    """判断是否为文本文件（按扩展名）"""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in TEXT_EXTENSIONS


def should_skip_dir(dir_name):
    """判断目录是否应跳过"""
    return dir_name in SKIP_DIRS


def collect_text_files(root_dir):
    """收集所有需要处理的文本文件"""
    text_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # 原地修改 dirnames 跳过指定目录（os.walk 推荐用法）
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for filename in filenames:
            full_path = os.path.join(dirpath, filename)
            if is_text_file(full_path):
                text_files.append(full_path)
    return text_files


def replace_placeholders_in_content(content, replacements):
    """
    对内容依次做 str.replace。
    replacements: list of (placeholder, value) 元组，已过滤掉空值
    返回 (new_content, replace_counts) 其中 replace_counts 是每个占位符的替换次数
    """
    new_content = content
    replace_counts = {}
    for placeholder, value in replacements:
        count = new_content.count(placeholder)
        if count > 0:
            new_content = new_content.replace(placeholder, value)
        replace_counts[placeholder] = count
    return new_content, replace_counts


def process_files(text_files, replacements, dry_run=False):
    r"""
    处理所有文本文件。
    返回 (scanned_count, modified_count, total_replace_counts, modified_files, double_colon_files)
    modified_files: list of (file_path, per_file_counts)
    double_colon_files: 内容变化后产生 ::\ 双冒号异常的文件列表
    """
    scanned = 0
    modified = 0
    total_counts = {ph: 0 for ph, _ in replacements}
    modified_files = []
    double_colon_files = []

    for file_path in text_files:
        scanned += 1
        try:
            # newline="" 让 Python 不做换行符转换，保留原 CRLF/LF 风格
            with open(file_path, encoding="utf-8", newline="") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            # 跳过无法以 UTF-8 解码的文件（可能是二进制或非 UTF-8 编码）
            continue

        new_content, counts = replace_placeholders_in_content(content, replacements)

        for ph, c in counts.items():
            total_counts[ph] = total_counts.get(ph, 0) + c

        if new_content != content:
            modified += 1
            modified_files.append((file_path, counts))
            # 检测是否产生 ::\ 双冒号异常路径
            if DOUBLE_COLON in new_content:
                double_colon_files.append(file_path)
            if not dry_run:
                with open(file_path, "w", encoding="utf-8", newline="") as f:
                    f.write(new_content)

    return scanned, modified, total_counts, modified_files, double_colon_files


def verify_replacements(text_files, required_placeholders, optional_placeholders):
    """
    验证替换结果（扫描所有文本文件）。
    返回 (required_residual, optional_residual)
    required_residual: dict {placeholder: [files still containing it]}
    optional_residual: dict {placeholder: [files still containing it]}
    """
    required_residual = {ph: [] for ph in required_placeholders}
    optional_residual = {ph: [] for ph in optional_placeholders}

    for file_path in text_files:
        try:
            with open(file_path, encoding="utf-8", newline="") as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue

        for ph in required_placeholders:
            if ph in content:
                required_residual[ph].append(file_path)
        for ph in optional_placeholders:
            if ph in content:
                optional_residual[ph].append(file_path)

    return required_residual, optional_residual


def prompt_for_values_interactive(prefilled=None):
    """交互式询问占位符值。prefilled: CLI 预填的值 dict"""
    prefilled = prefilled or {}
    values = dict(prefilled)

    print("=" * 60)
    print("占位符替换 - 交互式输入")
    print("=" * 60)
    print()

    print("必填占位符（必须输入）：")
    for entry in PLACEHOLDERS:
        if not entry["required"]:
            continue
        ph = entry["placeholder"]
        if ph in values:
            print(f"  {entry['prompt']}: [已通过命令行提供] {values[ph]}")
            continue
        while True:
            value = input(f"  {entry['prompt']}: ").strip()
            if value:
                values[ph] = value
                break
            print("    必填占位符不能为空，请重新输入。")
    print()

    print("可选占位符（直接回车跳过，保留占位符）：")
    for entry in PLACEHOLDERS:
        if entry["required"]:
            continue
        ph = entry["placeholder"]
        if ph in values:
            print(f"  {entry['prompt']}: [已通过命令行提供] {values[ph]}")
            continue
        value = input(f"  {entry['prompt']}: ").strip()
        if value:
            values[ph] = value
    print()

    return values


def main():
    parser = argparse.ArgumentParser(
        description="占位符自动替换脚本：将导出包中的占位符替换为用户实际路径",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
        "示例:\n"
        "  交互式:\n"
        "    python apply_placeholders.py <export_dir>\n"
        "  命令行:\n"
        "    python apply_placeholders.py <export_dir> "
        "--project-root D:/code/localAgent --username your_name --data-drive D\n"
        "  预演模式:\n"
        "    python apply_placeholders.py <export_dir> --dry-run "
        "--project-root D:/code/localAgent --username your_name --data-drive D\n"
        "\n"
        "注意：--project-root 可用单反斜杠（D:\\code）、双反斜杠（D:\\\\code）或正斜杠\n"
        "（D:/code），脚本会自动规范化为正斜杠，避免 TOML 转义问题。\n"
    ),
    )
    parser.add_argument("export_dir", help="导出包根目录")
    parser.add_argument("--project-root", dest="project_root",
                        help="<project_root> 替换值（项目根目录绝对路径）")
    parser.add_argument("--username", dest="username",
                        help="<username> 替换值（本地 Windows 用户名）")
    parser.add_argument("--data-drive", dest="data_drive",
                        help="<data_drive> 替换值（盘符字母，不要带冒号）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只输出将修改的文件清单和每个占位符替换次数，不实际写入")
    parser.add_argument("--skip-optional", action="store_true",
                        help="跳过可选占位符交互（保留占位符不替换）")
    args = parser.parse_args()

    export_dir = os.path.abspath(args.export_dir)
    if not os.path.isdir(export_dir):
        print(f"错误：导出包目录不存在: {export_dir}", file=sys.stderr)
        return 1

    # 收集 CLI 预填的必填值
    prefilled = {}
    if args.project_root:
        prefilled["<project_root>"] = args.project_root
    if args.username:
        prefilled["<username>"] = args.username
    if args.data_drive:
        prefilled["<data_drive>"] = args.data_drive

    required_keys = ["<project_root>", "<username>", "<data_drive>"]
    has_all_required = all(k in prefilled for k in required_keys)

    if has_all_required and args.skip_optional:
        # 纯 CLI 模式：必填已提供，跳过可选交互
        values = dict(prefilled)
        print("必填占位符已通过命令行提供：")
        for k in required_keys:
            print(f"  {k} = {values[k]}")
        print("（--skip-optional 已跳过可选占位符交互）")
        print()
    elif has_all_required:
        # CLI 提供必填，交互询问可选
        values = dict(prefilled)
        print("必填占位符已通过命令行提供：")
        for k in required_keys:
            print(f"  {k} = {values[k]}")
        print()
        print("可选占位符（直接回车跳过，保留占位符）：")
        for entry in PLACEHOLDERS:
            if entry["required"]:
                continue
            value = input(f"  {entry['prompt']}: ").strip()
            if value:
                values[entry["placeholder"]] = value
        print()
    else:
        # 交互式：询问未通过 CLI 提供的占位符
        values = prompt_for_values_interactive(prefilled)

    # 路径值规范化：反斜杠 → 正斜杠
    # 避免 Windows 单反斜杠路径写入 TOML 后触发非法转义（如 \T、\D）。
    # 正斜杠在 Windows 上兼容，TOML/JSON/Python 均不解析 / 为转义符。
    # 非路径值（username、copyright_holder 等）不含反斜杠，不受影响。
    # 双反斜杠（D:\\code）会先变 D://code，再用 while 折叠为 D:/code。
    normalized = []
    for ph, val in values.items():
        if not val:
            continue
        if "\\" in val:
            new_val = val.replace("\\", "/")
            while "//" in new_val:
                new_val = new_val.replace("//", "/")
            normalized.append((ph, val, new_val))
            values[ph] = new_val
    if normalized:
        print("路径规范化（反斜杠 → 正斜杠，避免 TOML 转义问题）：")
        for ph, old, new in normalized:
            print(f"  {ph}: {old} → {new}")
        print()

    # 构建替换列表（只含有值的占位符）
    replacements = [(ph, val) for ph, val in values.items() if val]

    # 收集文本文件
    print(f"扫描目录: {export_dir}")
    text_files = collect_text_files(export_dir)
    print(f"找到文本文件: {len(text_files)} 个")
    print()

    # 处理文件
    if args.dry_run:
        print("[DRY-RUN 模式] 只输出将修改的文件清单和替换次数，不实际写入")
        print()

    scanned, modified, total_counts, modified_files, double_colon_files = process_files(
        text_files, replacements, dry_run=args.dry_run
    )

    # 输出替换结果
    print("=" * 60)
    print("替换结果")
    print("=" * 60)
    print(f"扫描文件数: {scanned}")
    print(f"修改文件数: {modified}")
    print()
    print("各占位符替换次数:")
    for ph, _ in replacements:
        print(f"  {ph}: {total_counts.get(ph, 0)}")
    print()

    if args.dry_run and modified_files:
        print("将修改的文件清单:")
        for file_path, counts in modified_files:
            rel = os.path.relpath(file_path, export_dir)
            non_zero = {ph: c for ph, c in counts.items() if c > 0}
            print(f"  {rel}: {non_zero}")
        print()
    elif args.dry_run:
        print("（无文件将被修改）")
        print()

    # 替换后验证
    print("=" * 60)
    print("替换后验证")
    print("=" * 60)

    required_placeholders = [e["placeholder"] for e in PLACEHOLDERS if e["required"]]
    optional_placeholders = [e["placeholder"] for e in PLACEHOLDERS if not e["required"]]

    required_residual, optional_residual = verify_replacements(
        text_files, required_placeholders, optional_placeholders
    )

    has_required_residual = False
    print("必填占位符残留检查:")
    for ph in required_placeholders:
        files = required_residual.get(ph, [])
        if files:
            has_required_residual = True
            print(f"  {ph}: 残留在 {len(files)} 个文件中")
            for f in files:
                print(f"    - {os.path.relpath(f, export_dir)}")
        else:
            print(f"  {ph}: 无残留 OK")
    print()

    print("可选占位符残留检查（仅警告，不退出）:")
    any_optional_residual = False
    for ph in optional_placeholders:
        files = optional_residual.get(ph, [])
        if files:
            any_optional_residual = True
            print(f"  警告: {ph}: 残留在 {len(files)} 个文件中（可选占位符未替换，不影响）")
            for f in files[:3]:
                print(f"    - {os.path.relpath(f, export_dir)}")
            if len(files) > 3:
                print(f"    ... 还有 {len(files) - 3} 个文件")
    if not any_optional_residual:
        print("  无残留 OK")
    print()

    print("双冒号异常路径检查 (::\\):")
    if double_colon_files:
        print(f"  错误: 检测到 {len(double_colon_files)} 个文件含 ::\\ 双冒号异常路径")
        for f in double_colon_files:
            print(f"    - {os.path.relpath(f, export_dir)}")
        print("  说明替换值可能有问题（如 <data_drive> 传了带冒号的 D: 而非 D）")
    else:
        print("  无异常 OK")
    print()

    # 退出码判定
    if has_required_residual:
        print("错误：必填占位符仍有残留，替换未完成。", file=sys.stderr)
        return 1
    if double_colon_files:
        print("错误：检测到双冒号异常路径，请检查替换值。", file=sys.stderr)
        return 1

    print("替换完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
