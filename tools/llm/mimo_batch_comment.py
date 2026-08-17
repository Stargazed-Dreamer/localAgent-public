#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MiMo 批量代码注释生成器
使用小米 MiMo API 为 Python/C# 源码中的函数/类添加中文注释。
每个函数/类是一个独立的 API 调用（原子任务），支持断点续传。

用法:
  uv run python tools/llm/mimo_batch_comment.py --project F:\\codex\\MuseArc --project F:\\codex\\MusePlayer
  uv run python tools/llm/mimo_batch_comment.py --project F:\\codex\\ElasticBreath --dry-run
  uv run python tools/llm/mimo_batch_comment.py --project F:\\codex\\MuseArc --force
"""

import ast
import json
import os
import re
import sys
import time
import argparse
import requests
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==================== 配置 ====================

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from server.llm_pool import call_via_backend, check_backend_pool, print_backend_pool_status

MAX_WORKERS = 30        # 单文件内并发 HTTP 调用数（后端统一管理 key 级并发）
FILE_WORKERS = 3       # 多文件并发数
RETRY_TIMES = 1        # 单元失败后额外重试次数
MIN_UNIT_LINES = 3     # 太短的函数/方法不处理
MAX_UNIT_LINES = 1000   # 太长的函数/方法跳过（避免 token 爆炸）

EXCLUDE_DIRS = {
    ".venv", "__pycache__", "obj", "bin", ".git", "node_modules",
    ".idea", ".vs", "Debug", "Release", ".trae", ".agents",
    "migrations", ".build", "build", "dist", "egg-info",
    ".参考", "testLib", "testFile", "testFile_duplicate",
    "memory", "static", "weights",
    # Unity 相关
    "Library", "PackageCache", "Packages", "ProjectSettings",
    # 第三方参考代码
    "参考", "留痕源码 - WeChatMsg-master",
    # 构建输出
    "out", ".next",
}

STATE_FILE = Path("temp/mimo_comment_state.json")
STATE_LOCK = threading.Lock()

# ==================== 状态管理 ====================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"files": {}}

def save_state(state):
    with STATE_LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ==================== API 调用 ====================

def call_mimo(prompt, project="default"):
    """通过后端代理调用 MiMo API，返回文本内容（project 用于 per-project token 统计）"""
    result = call_via_backend(prompt, max_tokens=16384, temperature=0.3, timeout=180,
                              project=project, http_timeout=300)
    if result:
        # 去除可能的 markdown 标记
        result = re.sub(r'^```(?:python|csharp|cs|c|cs)?\s*\n?', '', result)
        result = re.sub(r'\n?```\s*$', '', result)
        return result.strip()
    return None

def init_llm_pool():
    """检查后端 LLM 池是否可用"""
    if not check_backend_pool():
        print("后端 LLM 池不可用，退出。")
        return False
    # 快速测试
    result = call_mimo("回复OK")
    if result and "OK" in result.upper():
        print("[API 检查] 后端池可用")
        return True
    print("[API 检查] 后端池调用测试失败！")
    return False

# ==================== Python 代码处理 ====================

def extract_python_units(code):
    """从 Python 代码中提取没有 docstring 的函数/类

    重要：类和方法不会同时提取，避免行范围重叠导致替换时互相覆盖。
    - 如果类没有 docstring，提取类（LLM 只给类加 docstring）
    - 如果类有 docstring 但方法没有，提取方法
    - 顶层函数总是独立提取
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        print(f"  [跳过] 语法错误: {e}")
        return []

    lines = code.splitlines(keepends=True)
    units = []

    def has_docstring(node):
        """检查节点是否已有 docstring"""
        if node.body and isinstance(node.body[0], ast.Expr):
            val = node.body[0].value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                return True
        return False

    # 第一遍：收集所有顶层定义和类的方法
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 顶层函数
            if not hasattr(node, 'end_lineno') or node.end_lineno is None:
                continue
            if has_docstring(node):
                continue
            start, end = node.lineno, node.end_lineno
            source = ''.join(lines[start-1:end])
            line_count = len(source.strip().splitlines())
            if line_count < MIN_UNIT_LINES:
                continue
            if line_count > MAX_UNIT_LINES:
                print(f"  [跳过] {node.name} 过长 ({line_count} 行)")
                continue
            units.append({
                'name': node.name,
                'kind': "函数",
                'start': start,
                'end': end,
                'source': source,
            })
        elif isinstance(node, ast.ClassDef):
            if not hasattr(node, 'end_lineno') or node.end_lineno is None:
                continue

            class_has_doc = has_docstring(node)

            # 检查类内方法是否需要注释
            methods_needing_doc = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not has_docstring(child):
                        if hasattr(child, 'end_lineno') and child.end_lineno is not None:
                            methods_needing_doc.append(child)

            if not class_has_doc and not methods_needing_doc:
                continue  # 类和方法都有 docstring，跳过

            if not class_has_doc and len(methods_needing_doc) == 0:
                # 类没有 docstring，也没有方法需要处理 → 提取类
                start, end = node.lineno, node.end_lineno
                source = ''.join(lines[start-1:end])
                line_count = len(source.strip().splitlines())
                if line_count < MIN_UNIT_LINES:
                    continue
                if line_count > MAX_UNIT_LINES:
                    print(f"  [跳过] {node.name} 过长 ({line_count} 行)")
                    continue
                units.append({
                    'name': node.name,
                    'kind': "类",
                    'start': start,
                    'end': end,
                    'source': source,
                })
            elif not class_has_doc and len(methods_needing_doc) > 0:
                # 类和方法都没有 docstring → 只提取方法（避免重叠）
                # 类的 docstring 稍后可以通过单独的请求添加
                print(f"  [注意] 类 {node.name} 无 docstring，但优先处理其方法（避免行范围重叠）")
                for child in methods_needing_doc:
                    start, end = child.lineno, child.end_lineno
                    source = ''.join(lines[start-1:end])
                    line_count = len(source.strip().splitlines())
                    if line_count < MIN_UNIT_LINES:
                        continue
                    if line_count > MAX_UNIT_LINES:
                        print(f"  [跳过] {child.name} 过长 ({line_count} 行)")
                        continue
                    units.append({
                        'name': child.name,
                        'kind': "方法",
                        'start': start,
                        'end': end,
                        'source': source,
                    })
            elif class_has_doc and len(methods_needing_doc) > 0:
                # 类有 docstring，方法没有 → 提取方法
                for child in methods_needing_doc:
                    start, end = child.lineno, child.end_lineno
                    source = ''.join(lines[start-1:end])
                    line_count = len(source.strip().splitlines())
                    if line_count < MIN_UNIT_LINES:
                        continue
                    if line_count > MAX_UNIT_LINES:
                        print(f"  [跳过] {child.name} 过长 ({line_count} 行)")
                        continue
                    units.append({
                        'name': child.name,
                        'kind': "方法",
                        'start': start,
                        'end': end,
                        'source': source,
                    })

    return units

def _strict_retry_note(retry_reason=None):
    if not retry_reason:
        return ""
    return f"""

上一次输出未通过安全校验，原因：{retry_reason}
这次必须严格遵守：
- 原始代码的每一行都必须保留，不能删除、改写、合并或拆分
- 函数/方法/类定义行必须逐字保留，包括 def/class、参数、冒号、换行和缩进
- 多行函数签名的每一行都必须逐字保留
- 只允许新增独立注释行或行尾注释，不允许重排任何代码行"""


def make_python_prompt(unit, retry_reason=None):
    return f"""请为以下Python代码添加中文注释。

这是一个名为 {unit['name']} 的{unit['kind']}。
{_strict_retry_note(retry_reason)}

要求：
1. 在{unit['kind']}定义后的第一行添加三引号docstring（\"\"\"...\"\"\"），说明功能、参数、返回值
2. 为复杂逻辑添加 # 行内注释
3. 绝不修改代码逻辑，只添加注释
4. 保持原有缩进和代码结构
5. 必须完整保留定义行，例如 def {unit['name']}(...) 这一行不能丢失或改写
6. 如果函数签名跨多行，签名中的每一行都必须逐字保留
7. 直接返回添加注释后的完整代码，不要加```python标记或任何解释文字

{unit['source']}"""

# ==================== C# 代码处理 ====================

def extract_cs_units(code):
    """从 C# 代码中提取没有 XML 注释的方法/类"""
    lines = code.splitlines(keepends=True)
    units = []

    # 方法签名正则
    sig_re = re.compile(
        r'^(\s*)((?:public|private|protected|internal|static|virtual|override|async|'
        r'sealed|abstract|partial|readonly|new|extern|unsafe|volatile|\s)+)'
        r'\s*([\w<>\[\],\?\s]+?)\s+'
        r'(\w+)\s*\('
    )
    # 类定义正则
    class_re = re.compile(
        r'^(\s*)((?:public|private|protected|internal|static|sealed|abstract|partial|\s)*)'
        r'class\s+(\w+)'
    )
    # 接口/结构体定义
    struct_re = re.compile(
        r'^(\s*)((?:public|private|protected|internal|static|sealed|partial|\s)*)'
        r'(struct|interface|record|enum)\s+(\w+)'
    )

    i = 0
    while i < len(lines):
        line = lines[i]

        cm = class_re.match(line)
        sm = struct_re.match(line)
        mm = sig_re.match(line)

        match = None
        kind = None
        name = None

        if cm:
            match = cm
            kind = "类"
            name = cm.group(3)
        elif sm:
            match = sm
            kind = sm.group(2)  # struct/interface/record/enum
            name = sm.group(3)
        elif mm:
            match = mm
            kind = "方法"
            name = mm.group(4)

        if not match:
            i += 1
            continue

        # 检查上方是否已有 /// 注释
        if i > 0 and lines[i-1].strip().startswith('///'):
            i += 1
            continue

        # 用花括号匹配找到结束行
        start_line = i + 1  # 1-indexed
        brace_count = 0
        found_open = False
        end_line = start_line

        for j in range(i, min(i + MAX_UNIT_LINES + 50, len(lines))):
            ch_line = lines[j]
            # 简单处理：跳过字符串中的花括号（不完美，但够用）
            in_string = False
            in_comment = False
            k = 0
            while k < len(ch_line):
                ch = ch_line[k]
                if in_comment:
                    if ch == '\n':
                        in_comment = False
                elif in_string:
                    if ch == '"':
                        in_string = False
                    elif ch == '\\' and k + 1 < len(ch_line):
                        k += 1
                elif ch == '"':
                    in_string = True
                elif ch == '/' and k + 1 < len(ch_line) and ch_line[k+1] == '/':
                    break  # 行注释，跳过剩余
                elif ch == '{':
                    brace_count += 1
                    found_open = True
                elif ch == '}':
                    brace_count -= 1
                k += 1

            if found_open and brace_count <= 0:
                end_line = j + 1
                break
        else:
            # 没找到匹配的花括号，跳过
            i += 1
            continue

        source = ''.join(lines[start_line-1:end_line])
        line_count = len(source.strip().splitlines())

        if line_count < MIN_UNIT_LINES:
            i = end_line
            continue
        if line_count > MAX_UNIT_LINES:
            print(f"  [跳过] {name} 过长 ({line_count} 行)")
            i = end_line
            continue

        units.append({
            'name': name,
            'kind': kind,
            'start': start_line,
            'end': end_line,
            'source': source,
        })
        i = end_line

    return units

def make_cs_prompt(unit, retry_reason=None):
    return f"""请为以下C#代码添加中文注释。

这是一个名为 {unit['name']} 的{unit['kind']}。
{_strict_retry_note(retry_reason)}

要求：
1. 在{unit['kind']}定义上方添加 /// XML文档注释（<summary>标签）
2. 为复杂逻辑添加 // 行内注释
3. 绝不修改代码逻辑，只添加注释
4. 保持原有缩进和代码结构
5. 必须完整保留方法/类定义行，不能丢失或改写签名
6. 如果签名跨多行，签名中的每一行都必须逐字保留
7. 直接返回添加注释后的完整代码，不要加```csharp标记或任何解释文字

{unit['source']}"""

# ==================== TypeScript/JavaScript 代码处理 ====================

def extract_ts_units(code):
    """从 TypeScript/JavaScript 代码中提取没有 JSDoc 注释的函数/类/接口"""
    lines = code.splitlines(keepends=True)
    units = []

    # 函数声明: function name(  /  export function name(  /  async function name(
    func_re = re.compile(
        r'^(\s*)(export\s+)?(default\s+)?(async\s+)?function\s+(\w+)\s*\('
    )
    # 箭头函数/变量函数: const name = (...) =>  /  const name = function(
    arrow_re = re.compile(
        r'^(\s*)(export\s+)?(const|let|var)\s+(\w+)\s*=\s*'
        r'(?:async\s*)?(?:\([^)]*\)|\w+)\s*(?:=>|function)'
    )
    # 类声明: class name  /  export class name
    class_re = re.compile(
        r'^(\s*)(export\s+)?(default\s+)?(abstract\s+)?class\s+(\w+)'
    )
    # 接口声明: interface name
    iface_re = re.compile(
        r'^(\s*)(export\s+)?interface\s+(\w+)'
    )

    i = 0
    while i < len(lines):
        line = lines[i]

        fm = func_re.match(line)
        am = arrow_re.match(line)
        cm = class_re.match(line)
        im = iface_re.match(line)

        match = None
        kind = None
        name = None

        if fm:
            match = fm
            kind = "函数"
            name = fm.group(5)
        elif am:
            match = am
            kind = "函数"
            name = am.group(4)
        elif cm:
            match = cm
            kind = "类"
            name = cm.group(5)
        elif im:
            match = im
            kind = "接口"
            name = im.group(3)

        if not match:
            i += 1
            continue

        # 检查上方是否已有 JSDoc 注释 /** */
        has_jsdoc = False
        if i > 0:
            prev = lines[i-1].strip()
            if prev.endswith('*/') or prev.startswith('*'):
                # 往上找 /**
                for k in range(i-1, max(i-10, -1), -1):
                    if '/**' in lines[k]:
                        has_jsdoc = True
                        break
                    if not lines[k].strip().startswith('*') and not lines[k].strip().endswith('*/'):
                        break
        if has_jsdoc:
            i += 1
            continue

        # 用花括号匹配找到结束行（接口可能没有花括号，用分号或下一个声明）
        start_line = i + 1  # 1-indexed
        brace_count = 0
        found_open = False
        end_line = start_line

        for j in range(i, min(i + MAX_UNIT_LINES + 50, len(lines))):
            ch_line = lines[j]
            in_string = False
            in_comment = False
            in_block_comment = False
            k = 0
            while k < len(ch_line):
                ch = ch_line[k]
                if in_block_comment:
                    if ch == '*' and k + 1 < len(ch_line) and ch_line[k+1] == '/':
                        in_block_comment = False
                        k += 1
                elif in_string:
                    if ch in ('"', "'", '`'):
                        in_string = False
                    elif ch == '\\' and k + 1 < len(ch_line):
                        k += 1
                elif ch in ('"', "'", '`'):
                    in_string = ch
                elif ch == '/' and k + 1 < len(ch_line) and ch_line[k+1] == '/':
                    break  # 行注释
                elif ch == '/' and k + 1 < len(ch_line) and ch_line[k+1] == '*':
                    in_block_comment = True
                    k += 1
                elif ch == '{':
                    brace_count += 1
                    found_open = True
                elif ch == '}':
                    brace_count -= 1
                k += 1

            if found_open and brace_count <= 0:
                end_line = j + 1
                break
        else:
            # 没找到匹配的花括号，可能是接口声明（无 body）或单行函数
            if kind == "接口":
                # 接口可能以 } 结尾或没有 body
                end_line = i + 1
            else:
                i += 1
                continue

        source = ''.join(lines[start_line-1:end_line])
        line_count = len(source.strip().splitlines())

        if line_count < MIN_UNIT_LINES:
            i = end_line
            continue
        if line_count > MAX_UNIT_LINES:
            print(f"  [跳过] {name} 过长 ({line_count} 行)")
            i = end_line
            continue

        units.append({
            'name': name,
            'kind': kind,
            'start': start_line,
            'end': end_line,
            'source': source,
        })
        i = end_line

    return units

def make_ts_prompt(unit, retry_reason=None):
    return f"""请为以下TypeScript/JavaScript代码添加中文注释。

这是一个名为 {unit['name']} 的{unit['kind']}。
{_strict_retry_note(retry_reason)}

要求：
1. 在{unit['kind']}定义上方添加 /** */ JSDoc 注释（含 @param、@returns 等）
2. 为复杂逻辑添加 // 行内注释
3. 绝不修改代码逻辑，只添加注释
4. 保持原有缩进和代码结构
5. 必须完整保留函数/类/接口定义行，不能丢失或改写签名
6. 如果签名跨多行，签名中的每一行都必须逐字保留
7. 直接返回添加注释后的完整代码，不要加```typescript标记或任何解释文字

{unit['source']}"""

# ==================== 文件处理 ====================

def read_file_safe(filepath):
    """安全读取文件，自动检测编码（优先 utf-8-sig 以处理 BOM）"""
    for enc in ('utf-8-sig', 'utf-8', 'gbk', 'latin-1'):
        try:
            return Path(filepath).read_text(encoding=enc), enc
        except (UnicodeDecodeError, Exception):
            continue
    return None, None


def get_indent(line):
    """获取行的缩进空格数"""
    return len(line) - len(line.lstrip())


def _is_comment_line(stripped, ext):
    """判断 stripped 行是否是纯注释行"""
    if ext == '.py':
        return stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''")
    elif ext == '.cs':
        return stripped.startswith('//') or stripped.startswith('///')
    elif ext in ('.ts', '.tsx', '.js', '.jsx'):
        return stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/*')
    return False


def fix_response_indentation(response, original_source, filepath=None):
    """修正 LLM 响应的缩进，使其与原始代码一致。

    LLM 有时会返回错误缩进的代码：
    - def 行缩进正确，但 body 过度缩进（常见于类方法）
    - 整体缩进为 0（常见于类内方法被提到模块级）
    - C#/TS: 在 def 行上方添加注释，导致缩进检测错误

    此函数比较原始代码和响应的缩进模式，分别调整 def 行和 body 行。
    跳过注释行来检测真正的 def/body 缩进。
    """
    ext = Path(filepath).suffix if filepath else '.py'
    resp_lines = response.splitlines(keepends=True)
    orig_lines = original_source.splitlines(keepends=True)

    if not resp_lines or not orig_lines:
        return response

    # 找原始代码的 def 行缩进和第一个 body 行缩进（跳过注释行）
    orig_def_indent = None
    orig_body_indent = None
    for line in orig_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_comment_line(stripped, ext):
            continue
        indent = get_indent(line)
        if orig_def_indent is None:
            orig_def_indent = indent
        elif indent > orig_def_indent and orig_body_indent is None:
            orig_body_indent = indent
            break

    # 找响应的 def 行缩进和第一个 body 行缩进（跳过注释行）
    resp_def_indent = None
    resp_body_indent = None
    for line in resp_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_comment_line(stripped, ext):
            continue
        indent = get_indent(line)
        if resp_def_indent is None:
            resp_def_indent = indent
        elif indent > resp_def_indent and resp_body_indent is None:
            resp_body_indent = indent
            break

    if orig_def_indent is None or resp_def_indent is None:
        return response

    def_diff = orig_def_indent - resp_def_indent

    # 如果有 body 行，检查 body 缩进差异
    body_diff = None
    if orig_body_indent is not None and resp_body_indent is not None:
        body_diff = orig_body_indent - resp_body_indent

    # 如果 def 和 body 的调整量都为 0，无需调整
    if def_diff == 0 and (body_diff is None or body_diff == 0):
        return response

    # 如果 def 和 body 的调整量不同，需要分别处理
    adjusted = []
    for line in resp_lines:
        if not line.strip():
            adjusted.append(line)
            continue

        current_indent = get_indent(line)
        stripped = line.strip()
        # 注释行跟随 def 行的调整量（注释通常与 def 同级或更外层）
        if _is_comment_line(stripped, ext):
            diff = def_diff
        elif current_indent <= resp_def_indent:
            # def 行（或更外层）
            diff = def_diff
        else:
            # body 行
            diff = body_diff if body_diff is not None else def_diff

        if diff > 0:
            adjusted.append(' ' * diff + line)
        elif diff < 0:
            remove = min(-diff, current_indent)
            adjusted.append(line[remove:])
        else:
            adjusted.append(line)

    return ''.join(adjusted)


def verify_python_integrity(original_code, modified_code):
    """验证 Python 文件修改后的结构完整性。

    检查：
    1. 修改后的代码能通过 AST 解析
    2. 顶层函数/类名集合不变（防止结构被破坏）
    """
    try:
        orig_tree = ast.parse(original_code)
        mod_tree = ast.parse(modified_code)
    except SyntaxError:
        return False, "AST 解析失败"

    # 收集顶层定义名称
    def get_top_level_names(tree):
        names = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
        return names

    orig_names = get_top_level_names(orig_tree)
    mod_names = get_top_level_names(mod_tree)

    if orig_names != mod_names:
        removed = orig_names - mod_names
        added = mod_names - orig_names
        return False, f"顶层定义变化: 移除{removed}, 新增{added}"

    return True, "OK"


def verify_cs_ts_integrity(original_code, modified_code, filepath):
    """验证 C#/TS/JS 文件修改后的基本完整性。

    检查：
    1. 大括号数量平衡
    2. 文件行数变化在合理范围内（不超过 3 倍）
    """
    orig_braces = original_code.count('{') - original_code.count('}')
    mod_braces = modified_code.count('{') - modified_code.count('}')

    if orig_braces != mod_braces:
        return False, f"大括号不平衡: 原始{orig_braces}, 修改后{mod_braces}"

    orig_lines = len(original_code.splitlines())
    mod_lines = len(modified_code.splitlines())

    if mod_lines > orig_lines * 3:
        return False, f"行数异常增长: {orig_lines} -> {mod_lines}"

    return True, "OK"


def verify_unit_code_preserved(original_source, response, filepath):
    """验证 LLM 响应保留了原始单元的所有代码行（只添加注释，不修改代码）。

    强不变量：添加注释不能修改任何现有代码的逻辑或缩进。
    检查原始源码中每一行的"代码部分"（去除行内注释后）是否在响应中
    以相同缩进出现。允许 LLM 添加/修改行内注释，但不允许修改代码本身。

    这是最关键的安全检查，能捕获：
    - 缩进被修改（如类内方法被提到模块级）
    - 代码逻辑被修改
    - 代码行被删除
    """
    ext = Path(filepath).suffix
    orig_lines = original_source.splitlines()
    resp_lines = response.splitlines()

    # 构建响应代码部分的集合
    resp_code_parts = set()
    for line in resp_lines:
        if not line.strip():
            continue
        code_part = _extract_code_part(line, ext)
        if code_part.strip():
            resp_code_parts.add(code_part)

    for line in orig_lines:
        stripped = line.strip()
        if not stripped:
            continue  # 跳过空行
        # 跳过原始的纯注释行（这些可能被 LLM 重组）
        if ext == '.py':
            if stripped.startswith('#'):
                continue
            if stripped.startswith('"""') or stripped.startswith("'''"):
                continue
        elif ext == '.cs':
            if stripped.startswith('//') or stripped.startswith('///'):
                continue
        elif ext in ('.ts', '.tsx', '.js', '.jsx'):
            if stripped.startswith('//') or stripped.startswith('*') or stripped.startswith('/*'):
                continue
        # 提取代码部分（去除行内注释）
        code_part = _extract_code_part(line, ext)
        if not code_part.strip():
            continue  # 纯注释行
        # 代码部分（含缩进）必须在响应中存在
        if code_part not in resp_code_parts:
            return False, f"代码行被修改或丢失: {stripped[:80]}"

    return True, "OK"


def build_validated_unit(unit, filepath, prompt_fn, project, retry_reason=None):
    original_source = unit['source']
    result = call_mimo(prompt_fn(unit, retry_reason), project)
    if not result:
        return None, "空响应"
    if unit['name'] not in result:
        return None, "返回内容不包含函数名"
    fixed_result = fix_response_indentation(result, original_source, filepath)
    ok, msg = verify_unit_code_preserved(original_source, fixed_result, filepath)
    if ok:
        return fixed_result, "OK"
    ok2, msg2 = verify_unit_code_preserved(original_source, result, filepath)
    if ok2:
        return result, "OK_RAW"
    return None, msg2


def process_unit_with_retry(unit, filepath, prompt_fn, project):
    last_msg = None
    for attempt in range(RETRY_TIMES + 1):
        result, msg = build_validated_unit(unit, filepath, prompt_fn, project, last_msg)
        if result:
            return result, msg, attempt
        last_msg = msg
    return None, last_msg or "未知错误", RETRY_TIMES


def _extract_code_part(line, ext):
    """提取行的代码部分（去除行内注释），保留前导缩进和代码内容。

    简单处理：扫描字符串外的注释标记（# 或 //），返回注释前的部分。
    不会修改前导缩进，只 rstrip 尾部空白。
    """
    in_string = False
    string_char = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_string:
            if ch == string_char:
                in_string = False
            elif ch == '\\' and i + 1 < len(line):
                i += 1  # 跳过转义字符
        elif ch in ('"', "'", '`'):
            in_string = True
            string_char = ch
        elif ext == '.py' and ch == '#':
            return line[:i].rstrip()
        elif ext in ('.cs', '.ts', '.tsx', '.js', '.jsx') and ch == '/' and i + 1 < len(line) and line[i+1] == '/':
            return line[:i].rstrip()
        i += 1
    return line.rstrip()


def process_file(filepath, dry_run=False, project="default", debug=False):
    """处理单个文件，返回 (处理成功数, 发现总数)"""
    ext = Path(filepath).suffix
    if ext == '.py':
        extract_fn = extract_python_units
        prompt_fn = make_python_prompt
    elif ext == '.cs':
        extract_fn = extract_cs_units
        prompt_fn = make_cs_prompt
    elif ext in ('.ts', '.tsx', '.js', '.jsx'):
        extract_fn = extract_ts_units
        prompt_fn = make_ts_prompt
    else:
        return 0, 0

    code, encoding = read_file_safe(filepath)
    if code is None:
        print(f"  [跳过] 无法读取: {filepath}")
        return 0, 0

    units = extract_fn(code)
    if not units:
        return 0, 0

    print(f"  发现 {len(units)} 个待注释单元")

    if dry_run:
        for u in units:
            print(f"    - {u['kind']} {u['name']} (L{u['start']}-{u['end']}, {u['end']-u['start']+1}行)")
        return 0, len(units)

    # 并发调用 API（传 project 标签用于 token 统计）
    results = {}  # idx -> commented_code
    rejected = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {}
        for idx, unit in enumerate(units):
            future = pool.submit(process_unit_with_retry, unit, filepath, prompt_fn, project)
            futures[future] = idx

        for future in as_completed(futures):
            idx = futures[future]
            unit = units[idx]
            try:
                result, msg, attempt = future.result()
                if result:
                    results[idx] = result
                    retry_text = "" if attempt == 0 else f" (重试 {attempt} 次后通过)"
                    raw_text = " (原始响应通过验证)" if msg == "OK_RAW" else ""
                    print(f"    [完成] {unit['kind']} {unit['name']}{retry_text}{raw_text}")
                else:
                    rejected += 1
                    print(f"    [拒绝] {unit['kind']} {unit['name']}: {msg}")
                    if debug:
                        debug_path = filepath + f".debug_{unit['name']}.txt"
                        Path(debug_path).write_text(
                            f"=== 原始 ===\n{unit['source']}\n\n=== 失败原因 ===\n{msg}\n",
                            encoding='utf-8')
                        print(f"    [调试] 已保存到: {debug_path}")
            except Exception as e:
                print(f"    [异常] {unit['name']}: {e}")

    if not results:
        print(f"  [跳过] 无可用结果 (拒绝 {rejected}, 失败 {len(units)-len(results)-rejected})")
        return 0, len(units)

    # Python 专属：逐单元 AST 验证（防止 docstring 缩进错误等导致文件级 AST 失败）
    if ext == '.py':
        bad_units = []
        for idx in list(results.keys()):
            unit = units[idx]
            test_lines = code.splitlines(keepends=True)
            test_code = results[idx]
            if not test_code.endswith('\n'):
                test_code += '\n'
            test_lines[unit['start']-1:unit['end']] = [test_code]
            test_content = ''.join(test_lines)
            try:
                ast.parse(test_content)
            except SyntaxError as e:
                print(f"    [单元AST失败] {unit['kind']} {unit['name']}: {e}")
                bad_units.append(idx)
        for idx in bad_units:
            del results[idx]
            rejected += 1
        if bad_units and not results:
            print(f"  [跳过] 所有单元均未通过 AST 验证")
            return 0, len(units)

    # 从底部到顶部替换（避免行号偏移）
    lines = code.splitlines(keepends=True)
    for idx in sorted(results.keys(), key=lambda i: units[i]['start'], reverse=True):
        unit = units[idx]
        new_code = results[idx]
        if not new_code.endswith('\n'):
            new_code += '\n'
        lines[unit['start']-1:unit['end']] = [new_code]

    # 写回文件前进行完整性验证
    new_content = ''.join(lines)

    # 完整性验证
    if ext == '.py':
        ok, msg = verify_python_integrity(code, new_content)
    else:
        ok, msg = verify_cs_ts_integrity(code, new_content, filepath)

    if not ok:
        # 优雅降级：逐个移除单元直到验证通过
        if ext == '.py' and len(results) > 1:
            print(f"    [文件级验证失败] {msg}，尝试逐个移除单元...")
            for idx in sorted(results.keys(), key=lambda i: units[i]['start'], reverse=True):
                trial_results = {k: v for k, v in results.items() if k != idx}
                if not trial_results:
                    break
                trial_lines = code.splitlines(keepends=True)
                for tidx in sorted(trial_results.keys(), key=lambda i: units[i]['start'], reverse=True):
                    tunit = units[tidx]
                    tcode = trial_results[tidx]
                    if not tcode.endswith('\n'):
                        tcode += '\n'
                    trial_lines[tunit['start']-1:tunit['end']] = [tcode]
                trial_content = ''.join(trial_lines)
                ok2, msg2 = verify_python_integrity(code, trial_content)
                if ok2:
                    print(f"    [降级] 移除 {units[idx]['name']} 后验证通过")
                    del results[idx]
                    rejected += 1
                    new_content = trial_content
                    ok = True
                    msg = "OK (降级)"
                    break
        if not ok:
            print(f"    [安全回退] 文件完整性验证失败: {msg}，跳过此文件")
            if debug:
                debug_path = filepath + ".debug_failed"
                Path(debug_path).write_text(new_content, encoding=encoding or 'utf-8')
                print(f"    [调试] 修改后内容已保存到: {debug_path}")
            return 0, len(units)

    # 写回文件
    Path(filepath).write_text(new_content, encoding=encoding or 'utf-8')
    print(f"    [写入成功] 完整性验证通过 (接受 {len(results)}/{len(units)}, 拒绝 {rejected})")

    return len(results), len(units)

def scan_files(project_dir):
    """扫描项目目录下的源文件"""
    project_dir = Path(project_dir)
    files = []
    for path in project_dir.rglob('*'):
        if not path.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix in ('.py', '.cs', '.ts', '.tsx', '.js', '.jsx'):
            files.append(str(path))
    return sorted(files)


def process_one_file_task(filepath, project, project_name, args, state):
    rel = os.path.relpath(filepath, project)
    if not args.force and filepath in state["files"]:
        entry = state["files"][filepath]
        if entry.get("completed"):
            print(f"  [跳过] {rel} (已处理 {entry.get('units_processed', 0)} 单元)")
            return 0, 0, 1
    print(f"\n  处理: {rel}")
    processed, found = process_file(filepath, args.dry_run, project=project_name, debug=args.debug)
    if not args.dry_run:
        state["files"][filepath] = {
            "completed": True,
            "units_processed": processed,
            "units_found": found,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_state(state)
    return processed, found, 0

# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description='MiMo 批量代码注释生成器')
    parser.add_argument('--project', action='append', required=True,
                        help='项目目录（可多次指定）')
    parser.add_argument('--dry-run', action='store_true',
                        help='只扫描不调用API')
    parser.add_argument('--force', action='store_true',
                        help='强制重新处理已处理的文件')
    parser.add_argument('--no-check', action='store_true',
                        help='跳过 API 健康检查')
    parser.add_argument('--debug', action='store_true',
                        help='保存被拒绝的单元到调试文件')
    args = parser.parse_args()

    if not args.dry_run and not args.no_check:
        if not init_llm_pool():
            print("并发池不可用，退出。使用 --no-check 跳过检查。")
            sys.exit(1)

    state = load_state()

    total_processed = 0
    total_skipped = 0
    total_units = 0

    for project in args.project:
        # 项目标签 = 路径最后一段（用于 token 统计区分）
        project_name = Path(project).name
        print(f"\n{'='*60}")
        print(f"项目: {project} (标签: {project_name})")
        print(f"{'='*60}")

        files = scan_files(project)
        py_count = sum(1 for f in files if f.endswith('.py'))
        cs_count = sum(1 for f in files if f.endswith('.cs'))
        ts_count = sum(1 for f in files if f.endswith(('.ts', '.tsx', '.js', '.jsx')))
        lang_parts = [f"{py_count} Python" if py_count else "",
                      f"{cs_count} C#" if cs_count else "",
                      f"{ts_count} TS/JS" if ts_count else ""]
        lang_str = ", ".join(p for p in lang_parts if p)
        print(f"找到 {len(files)} 个源文件 ({lang_str})")

        with ThreadPoolExecutor(max_workers=1 if args.dry_run else FILE_WORKERS) as file_pool:
            future_map = {
                file_pool.submit(process_one_file_task, filepath, project, project_name, args, state): filepath
                for filepath in files
            }
            for future in as_completed(future_map):
                try:
                    processed, found, skipped = future.result()
                    total_processed += processed
                    total_units += found
                    total_skipped += skipped
                except Exception as e:
                    rel = os.path.relpath(future_map[future], project)
                    print(f"  [异常] {rel}: {e}")

    print(f"\n{'='*60}")
    print(f"完成！")
    print(f"  处理单元: {total_processed}")
    print(f"  发现单元: {total_units}")
    print(f"  跳过文件: {total_skipped}")
    print(f"  状态文件: {STATE_FILE}")

    # 打印 token 统计
    if not args.dry_run:
        try:
            print_backend_pool_status()
        except Exception:
            pass
    print(f"{'='*60}")

if __name__ == '__main__':
    main()
