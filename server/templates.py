"""操作模板网关 - 双层 MCP 架构的第三个网关

预定义常见操作模式（截图+OCR、浏览器导航+等待、表单填写、游戏抽卡循环等），
通过 localagent_template_tool + localagent_list_templates 访问。

设计目标：
  - 减少 AI 重复编写 exec_python 代码
  - 标准化常见工作流，降低出错率
  - 模板参数化，灵活适配不同场景

模板分类：
  - screen: 截图+OCR、截图+Vision、区域截图、差异截图
  - browser: 导航+等待、表单填写、滚动加载、元素提取
  - game: 抽卡循环、自动点击、画面等待
  - file: 文件搜索、批量重命名、JSON处理
  - system: 窗口管理、进程查询
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import Field

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.templates")
router = APIRouter(prefix="/templates", tags=["操作模板网关"])


# ========== 模板定义 ==========

# 每个模板是一个 dict，包含：
# - name: 模板名（唯一标识）
# - category: 分类
# - description: 描述（供 AI 选择模板时参考）
# - prompt_hint: 提示词（告诉 AI 何时用此模板）
# - parameters: 参数定义 [{name, type, required, default, description}]
# - template: exec_python 代码模板（用 {param} 占位）

TEMPLATES: list[dict] = [
    # ========== Screen 模板 ==========
    {
        "name": "screenshot_and_ocr",
        "category": "screen",
        "description": "截图并 OCR 识别文字，一步完成避免 base64 撑爆上下文",
        "prompt_hint": "当你需要识别屏幕上的文字时使用此模板，而不是 capture_screen(base64)+ocr_path",
        "parameters": [
            {"name": "mode", "type": "str", "required": False, "default": "fullscreen",
             "description": "fullscreen 或 window"},
            {"name": "window_title", "type": "str", "required": False, "default": "",
             "description": "窗口标题（mode=window 时使用，模糊匹配）"},
            {"name": "region", "type": "str", "required": False, "default": "",
             "description": "区域裁剪 left,top,right,bottom（逗号分隔），留空则全图"},
        ],
        "template": '''import requests, base64, os, tempfile
API = '{backend_url}'
mode = "{mode}"
win = "{window_title}"
region = "{region}"
cap = requests.post(f'{API}/screen/capture',
    json={'mode': mode, 'window_title': win or None, 'format': 'base64'}).json()
img_path = os.path.join(tempfile.gettempdir(), 'shot.png')
with open(img_path, 'wb') as f: f.write(base64.b64decode(cap['image']))
ocr = requests.post(f'{API}/ocr/path/json', json={'path': img_path}).json()
texts = [it.get('text','') for it in ocr.get('details', [])]
print('=== OCR 结果 ===')
for t in texts: print(t)
if region:
    print(f'(区域: {region})')
''',
    },
    {
        "name": "screenshot_inline_for_llm",
        "category": "screen",
        "description": "截图压缩后供多模态 LLM 直接查看（返回 ImageContent）",
        "prompt_hint": "当你（多模态模型）需要直接看屏幕内容时使用，比 screenshot_and_ocr 更适合视觉理解",
        "parameters": [
            {"name": "mode", "type": "str", "required": False, "default": "fullscreen",
             "description": "fullscreen 或 window"},
            {"name": "window_title", "type": "str", "required": False, "default": "",
             "description": "窗口标题"},
            {"name": "max_size", "type": "int", "required": False, "default": 512,
             "description": "最长边像素上限"},
        ],
        "template": '''import requests
API = '{backend_url}'
r = requests.post(f'{API}/screen/capture',
    json={'mode': '{mode}', 'window_title': '{window_title}' or None, 'format': 'inline', 'max_edge': {max_size}}).json()
print(f"截图完成: {r['width']}x{r['height']} (原始 {r['original_width']}x{r['original_height']})")
# 内联截图会通过 MCP ImageContent 自动返回给多模态 LLM
''',
    },
    {
        "name": "screen_diff",
        "category": "screen",
        "description": "对比两次截图的像素差异，判断画面是否变化",
        "prompt_hint": "游戏自动化中判断画面是否变化（如抽卡结果出现、动画结束）",
        "parameters": [
            {"name": "wait_seconds", "type": "float", "required": False, "default": 1.0,
             "description": "两次截图间隔秒数"},
        ],
        "template": '''import requests, base64, io, time
import numpy as np
from PIL import Image
API = '{backend_url}'
def shot():
    r = requests.post(f'{API}/screen/capture', json={'format':'base64'}).json()
    return base64.b64decode(r['image'])
a = shot()
time.sleep({wait_seconds})
b = shot()
ia = np.asarray(Image.open(io.BytesIO(a)).convert('L').resize((128,128)))
ib = np.asarray(Image.open(io.BytesIO(b)).convert('L').resize((128,128)))
diff = np.abs(ia.astype(int) - ib.astype(int)) > 20
ratio = float(diff.sum()) / diff.size
print(f'变化比例: {ratio:.3f} ({'已变化' if ratio > 0.1 else '未变化'})')
''',
    },

    # ========== Browser 模板 ==========
    # 基于 browser use 三件套（browser_session_create + browser_wait_for + browser_evaluate），
    # 端点语义见 docs/api-reference.md 浏览器段。
    {
        "name": "browser_navigate_and_wait",
        "category": "browser",
        "description": "创建 session 导航到 URL 并等待加载完成（热态 <250ms）",
        "prompt_hint": "需要打开网页并确保加载完成时使用；自动复用或新建持久 session",
        "parameters": [
            {"name": "url", "type": "str", "required": True, "default": "",
             "description": "目标 URL"},
            {"name": "wait_until", "type": "str", "required": False, "default": "networkidle",
             "description": "load | domcontentloaded | networkidle | commit"},
        ],
        "template": '''import requests
API = '{backend_url}'
# 1. 创建 session（自动 goto url，自动注入站点经验）
r1 = requests.post(f'{API}/browser/sessions',
    json={'url': '{url}'}, timeout=30).json()
if not r1.get('success'):
    print(f"创建 session 失败: {r1}"); raise SystemExit(1)
session_id = r1['session_id']
print(f"session={session_id} tab={r1.get('tab_id','')} url={r1.get('url','')}")
# 2. 等待加载状态
r2 = requests.post(f'{API}/browser/wait_for',
    json={'session_id': session_id,
          'wait_type': 'load_state',
          'load_state': '{wait_until}',
          'timeout': 30}, timeout=35).json()
print(f"加载完成: matched={r2.get('matched')} elapsed_ms={r2.get('elapsed_ms')}")
# 3. 提示用完关闭（也可保留复用）
print(f"# 清理: POST /browser/sessions/close json={{'session_id': '{session_id}', 'close_page': false}}")
''',
    },
    {
        "name": "browser_extract_article",
        "category": "browser",
        "description": "通过 browser_evaluate 提取网页正文文本（替代已删除的 extract_text）",
        "prompt_hint": "需要读取网页文章内容时使用，比 OCR 更准确；用 querySelector 定位 + textContent 取值",
        "parameters": [
            {"name": "session_id", "type": "str", "required": True, "default": "",
             "description": "已 attach 到目标页面的 session_id（先调 browser_session_create）"},
            {"name": "selector", "type": "str", "required": False, "default": "article",
             "description": "正文 CSS 选择器，常见值: article, .content, .post-body, main"},
            {"name": "max_length", "type": "int", "required": False, "default": 10000,
             "description": "最大返回字符数（自动传给 browser_evaluate 的 max_length）"},
        ],
        "template": '''import requests, json
API = '{backend_url}'
# 用 browser_evaluate 取 textContent（替代已删除的 /browser/extract_text）
# 表达式只读、不命中写操作正则，可安全执行
expr = "(document.querySelector('{selector}') || document.body).textContent.slice(0, {max_length})"
r = requests.post(f'{API}/browser/evaluate',
    json={'session_id': '{session_id}',
          'expression': expr,
          'return_json': False,
          'max_length': {max_length}}, timeout=30).json()
if r.get('success'):
    print(r.get('value', ''))
else:
    err = r.get('error') or {{}}
    print(f"提取失败: code={err.get('error_code')} detail={err.get('debug_detail')}")
''',
    },
    {
        "name": "browser_scroll_and_collect",
        "category": "browser",
        "description": "滚动页面并收集动态加载的内容（如瀑布流、评论）",
        "prompt_hint": "需要收集动态加载内容（如评论列表、商品列表）时使用",
        "parameters": [
            {"name": "url_pattern", "type": "str", "required": False, "default": "",
             "description": "标签页 URL 子串匹配"},
            {"name": "selector", "type": "str", "required": True, "default": "",
             "description": "要收集的元素 CSS 选择器"},
            {"name": "scroll_count", "type": "int", "required": False, "default": 5,
             "description": "滚动次数"},
            {"name": "wait_per_scroll", "type": "float", "required": False, "default": 1.5,
             "description": "每次滚动后等待秒数"},
        ],
        "template": '''import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp('{cdp_url}')
        context = browser.contexts[0]
        url_pat = '{url_pattern}'
        page = None
        for pg in context.pages:
            if not url_pat or url_pat in pg.url:
                page = pg; break
        if not page:
            print('ERROR: no matching tab'); return
        collected = []
        seen = set()
        for i in range({scroll_count}):
            els = await page.query_selector_all('{selector}')
            for el in els:
                text = (await el.inner_text()).strip()
                if text and text not in seen:
                    seen.add(text)
                    collected.append(text)
            await page.evaluate('window.scrollBy(0, window.innerHeight * 0.8)')
            await asyncio.sleep({wait_per_scroll})
        print(f'=== 收集到 {{len(collected)}} 条 ===')
        for t in collected:
            print(t)

asyncio.run(main())
''',
    },

    # ========== Game 模板 ==========
    {
        "name": "game_wait_and_click",
        "category": "game",
        "description": "等待画面出现指定文字后点击坐标（游戏自动化常用模式）",
        "prompt_hint": "游戏自动化中'等待某文字出现→点击'的循环，如等待'确认'按钮出现后点击",
        "parameters": [
            {"name": "wait_text", "type": "str", "required": True, "default": "",
             "description": "等待出现的文字（OCR 检测）"},
            {"name": "click_x", "type": "int", "required": True, "default": 0,
             "description": "点击 X 坐标"},
            {"name": "click_y", "type": "int", "required": True, "default": 0,
             "description": "点击 Y 坐标"},
            {"name": "timeout", "type": "float", "required": False, "default": 30.0,
             "description": "最大等待秒数"},
            {"name": "window_title", "type": "str", "required": False, "default": "",
             "description": "游戏窗口标题（模糊匹配）"},
        ],
        "template": '''import requests
API = '{backend_url}'
# 1. 等待文字出现
wait_req = {{
    'condition_type': 'ocr_contains',
    'expected': '{wait_text}',
    'timeout': {timeout},
    'interval': 0.5,
}}
if '{window_title}':
    wait_req['mode'] = 'window'
    wait_req['window_title'] = '{window_title}'
r = requests.post(f'{API}/screen/wait-for', json=wait_req).json()
if not r.get('condition_met'):
    print(f"超时未出现: '{wait_text}'"); exit()
print(f"检测到: '{wait_text}'，准备点击")
# 2. 点击
click_req = {{'action': 'click', 'x': {click_x}, 'y': {click_y}, 'require_confirm': False}}
if '{window_title}':
    click_req['window_title'] = '{window_title}'
r2 = requests.post(f'{API}/screen/action', json=click_req).json()
print(f"点击结果: {r2['message']}")
''',
    },
    {
        "name": "game_auto_click_loop",
        "category": "game",
        "description": "循环点击某坐标 N 次，每次间隔等待（如自动点击'继续'按钮）",
        "prompt_hint": "需要重复点击同一位置（如跳过对话、自动钓鱼）",
        "parameters": [
            {"name": "click_x", "type": "int", "required": True, "default": 0,
             "description": "点击 X 坐标"},
            {"name": "click_y", "type": "int", "required": True, "default": 0,
             "description": "点击 Y 坐标"},
            {"name": "count", "type": "int", "required": False, "default": 10,
             "description": "点击次数"},
            {"name": "interval", "type": "float", "required": False, "default": 1.0,
             "description": "每次点击间隔秒数"},
            {"name": "window_title", "type": "str", "required": False, "default": "",
             "description": "窗口标题"},
        ],
        "template": '''import requests, time
API = '{backend_url}'
actions = []
for i in range({count}):
    actions.append({{'action': 'click', 'x': {click_x}, 'y': {click_y}}})
    actions.append({{'action': 'wait', 'wait': {interval}}})
req = {{'actions': actions, 'require_confirm': False, 'stop_on_error': True}}
if '{window_title}':
    req['window_title'] = '{window_title}'
r = requests.post(f'{API}/screen/batch-actions', json=req).json()
print(f"执行: {r['executed']}/{r['total']} 步, 耗时 {r['elapsed_ms']}ms")
if not r['success']:
    print(f"失败: {r['message']}")
''',
    },

    # ========== File 模板 ==========
    {
        "name": "file_search_content",
        "category": "file",
        "description": "在指定目录搜索文件内容（基于 ripgrep）",
        "prompt_hint": "需要在项目中搜索代码/文本内容时使用，比手写 os.walk 更快",
        "parameters": [
            {"name": "directory", "type": "str", "required": True, "default": ".",
             "description": "搜索目录"},
            {"name": "pattern", "type": "str", "required": True, "default": "",
             "description": "搜索正则表达式"},
            {"name": "file_glob", "type": "str", "required": False, "default": "*.py",
             "description": "文件名 glob，如 *.py, *.md, *.json"},
            {"name": "max_results", "type": "int", "required": False, "default": 50,
             "description": "最大返回结果数"},
        ],
        "template": '''import subprocess, os
result = subprocess.run(
    ['rg', '--no-heading', '-n', '--glob', '{file_glob}', '{pattern}', '{directory}'],
    capture_output=True, text=True, cwd=os.getcwd()
)
lines = result.stdout.splitlines()[:{max_results}]
print(f'=== 找到 {len(lines)} 条匹配 ===')
for line in lines:
    print(line)
if result.returncode not in (0, 1):
    print(f'rg 错误: {result.stderr}')
''',
    },
    {
        "name": "file_list_recent",
        "category": "file",
        "description": "列出目录下最近修改的文件",
        "prompt_hint": "需要查看最近修改的文件（如检查输出、日志）",
        "parameters": [
            {"name": "directory", "type": "str", "required": False, "default": ".",
             "description": "目录路径"},
            {"name": "count", "type": "int", "required": False, "default": 20,
             "description": "返回文件数"},
            {"name": "pattern", "type": "str", "required": False, "default": "*",
             "description": "文件名 glob"},
        ],
        "template": '''import os, glob, time
from pathlib import Path
files = list(Path('{directory}').rglob('{pattern}'))
files = [f for f in files if f.is_file()]
files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
print(f'=== 最近修改的 {min({count}, len(files))} 个文件 ===')
for f in files[:{count}]:
    mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))
    size = f.stat().st_size
    unit = 'B' if size < 1024 else ('KB' if size < 1024*1024 else 'MB')
    size_str = f'{size} {unit}' if unit == 'B' else f'{size/1024 if unit=="KB" else size/1024/1024:.1f} {unit}'
    print(f'{mtime}  {size_str:>10}  {f}')
''',
    },

    # ========== System 模板 ==========
    {
        "name": "system_window_focus",
        "category": "system",
        "description": "查找并激活指定标题的窗口",
        "prompt_hint": "需要把某窗口置前（如切换到游戏窗口）",
        "parameters": [
            {"name": "window_title", "type": "str", "required": True, "default": "",
             "description": "窗口标题（模糊匹配）"},
        ],
        "template": '''import win32gui
def callback(hwnd, target):
    title = win32gui.GetWindowText(hwnd)
    if target.lower() in title.lower() and win32gui.IsWindowVisible(hwnd):
        win32gui.SetForegroundWindow(hwnd)
        print(f'已激活: {title}')
        return False  # 停止枚举
    return True
win32gui.EnumWindows(callback, '{window_title}')
''',
    },
]


# ========== Pydantic 模型 ==========

class TemplateParameter(BaseSchema):
    """这是一个用于定义模板参数的类，继承自BaseModel，用于存储和验证模板参数的属性。

    功能：TemplateParameter类用于表示模板参数的定义，可作为数据模型使用。

    参数：

    - name: 参数名称，类型为字符串，表示参数的标识符。

    - type: 参数类型，类型为字符串，描述参数的数据类型。

    - required: 是否必需，类型为布尔值，指示参数是否必须提供。

    - default: 默认值，类型为任意，在参数未提供时使用的值。

    - description: 参数描述，类型为字符串，提供参数的详细说明。

    返回值：实例化一个TemplateParameter对象，包含所有属性值。

    """
    name: str
    type: str
    required: bool
    default: Any
    description: str

class TemplateInfo(BaseSchema):
    """TemplateInfo 类用于表示模板信息。

    功能: 存储和管理模板的基本信息，包括名称、类别、描述、提示提示和参数列表。
    参数:
        name (str): 模板的名称。
        category (str): 模板的类别。
        description (str): 模板的详细描述。
        prompt_hint (str): 模板的提示提示，用于引导使用。
        parameters (list[TemplateParameter]): 模板参数的列表，定义模板的可配置项。
    返回值: 当实例化时，返回一个 TemplateInfo 对象，包含上述所有属性。
    """
    name: str
    category: str
    description: str
    prompt_hint: str
    parameters: list[TemplateParameter]

class ListTemplatesResponse(BaseSchema):
    """表示列出模板的响应。

    参数:
        total (int): 模板的总数。
        categories (dict[str, list[TemplateInfo]]): 分类字典，键为分类名称，值为该分类下的模板信息列表。

    返回值:
        ListTemplatesResponse: 一个包含总数量和分类信息的响应对象。
    """
    total: int
    categories: dict[str, list[TemplateInfo]]

class TemplateExecRequest(BaseSchema):
    """用于封装模板执行请求数据的模型。

    功能：
        作为一个数据模型，封装执行模板时所需的基本信息。
    参数：
        template (str): 需要执行的模板的名称。
        params (dict, optional): 传递给模板的参数字典，默认为空字典。
    返回值：
        类的实例，包含已验证和结构化的请求数据。
    """
    template: str  # 模板名
    params: dict = Field(default_factory=dict)  # 参数值

class TemplateExecResponse(BaseSchema):
    """
    用于表示模板执行结果的响应模型。
    该类封装了执行任务后的关键信息，包括是否成功、执行输出、耗时等。
    继承自 Pydantic 的 BaseModel，用于数据验证和序列化。
    """
    success: bool  # 任务是否执行成功。
    template: str  # 用于执行的原始模板文本。
    exec_id: str | None = None  # 与该次执行关联的唯一标识符，用于追踪。
    stdout: str | None = None  # 任务执行的标准输出内容。当无输出时为None。
    stderr: str | None = None  # 任务执行的标准错误输出内容。当无错误时为None。
    stdout_truncated: bool | None = None  # 标识标准输出内容是否因过长而被截断。
    elapsed_ms: int  # 任务执行所耗费的时间，单位为毫秒。
    message: str  # 附加的人类可读的信息或提示，通常用于反馈状态或错误详情。


# ========== 注册表索引 ==========

_TEMPLATE_INDEX: dict[str, dict] = {t["name"]: t for t in TEMPLATES}


def _render_template(template_str: str, params: dict) -> str:
    """渲染模板，用参数值替换占位符。

    T03 安全修复：字符串参数转义引号，防止注入。模板中占位符在引号内
    （如 '{url}'），用户传 `'; os.system('calc'); '` 会逃逸引号注入代码。
    修复：字符串值中的 \\ 和引号转义，使其无法逃逸字符串字面量。
    """
    # 自动注入系统级占位符（避免模板硬编码端口/URL）
    if "{backend_url}" in template_str or "{cdp_url}" in template_str:
        from .config import get_chrome_config, get_server_config
        _srv = get_server_config()
        _chr = get_chrome_config()
        params = {
            **params,
            "backend_url": f'http://{_srv["host"]}:{_srv["port"]}',
            "cdp_url": f'http://127.0.0.1:{_chr["debug_port"]}',
        }
    rendered = template_str
    for key, value in params.items():
        placeholder = "{" + key + "}"
        if isinstance(value, str):
            # T03: 转义反斜杠和引号，防止逃逸字符串字面量注入代码
            safe_value = value.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
            rendered = rendered.replace(placeholder, safe_value)
        elif isinstance(value, bool):
            rendered = rendered.replace(placeholder, str(value).lower())
        elif value is None:
            rendered = rendered.replace(placeholder, "")
        else:
            # 6-7: 数值/其他类型也过转义——params 可能传入字符串（如
            # count="1)\nos.system('x')#"），str() 后必须转义防逃逸注入
            # （合并层已按声明 type 强制转换，此处兜底）
            safe_value = str(value).replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')
            rendered = rendered.replace(placeholder, safe_value)
    return rendered


# ========== 路由 ==========

@router.get("/list", response_model=ListTemplatesResponse, operation_id="localagent_list_templates")
async def list_templates(category: str | None = None):
    """List all available operation templates.

    Templates are pre-defined exec_python patterns for common workflows, avoiding
    the need to write boilerplate code. Grouped by category:
    - screen: screenshot+OCR, inline capture, screen diff
    - browser: navigate+wait, article extraction, scroll+collect
    - game: wait+click, auto-click loop
    - file: content search, recent files
    - system: window focus

    Each template has parameters with defaults. Call via localagent_template_tool.
    Pass category= to filter.
    """
    cats: dict[str, list[TemplateInfo]] = {}
    for t in TEMPLATES:
        if category and t["category"] != category:
            continue
        params = [
            TemplateParameter(
                name=p["name"], type=p["type"],
                required=p["required"], default=p["default"],
                description=p["description"],
            ) for p in t["parameters"]
        ]
        info = TemplateInfo(
            name=t["name"], category=t["category"],
            description=t["description"], prompt_hint=t["prompt_hint"],
            parameters=params,
        )
        cats.setdefault(t["category"], []).append(info)
    return ListTemplatesResponse(
        total=sum(len(v) for v in cats.values()), categories=cats,
    )


@router.post("/run", response_model=TemplateExecResponse, operation_id="localagent_template_tool")
async def template_tool(req: TemplateExecRequest):
    """Execute a pre-defined operation template by name.

    This is a TEMPLATE GATEWAY (second-tier MCP tool) that runs pre-built exec_python
    code for common workflows, avoiding the need to write boilerplate.

    Use localagent_list_templates to discover available templates and their parameters.
    Pass template=<name> and params={...}.

    Examples:
    - localagent_template_tool(template="screenshot_and_ocr", params={"mode":"window","window_title":"游戏"})
    - localagent_template_tool(template="browser_navigate_and_wait", params={"url":"https://example.com"})
    - localagent_template_tool(template="game_wait_and_click", params={"wait_text":"确认","click_x":500,"click_y":400})
    - localagent_template_tool(template="file_search_content", params={"directory":"server","pattern":"def capture","file_glob":"*.py"})

    Templates handle parameter escaping and code generation internally.
    Returns exec_python stdout/stderr.
    """
    tmpl = _TEMPLATE_INDEX.get(req.template)
    if not tmpl:
        available = sorted(_TEMPLATE_INDEX.keys())
        raise HTTPException(
            status_code=404,
            detail=f"未知模板: {req.template}。用 localagent_list_templates 查看可用模板。已注册: {available}",
        )

    # 合并参数（用默认值填充缺失项）
    params = {}
    for p in tmpl["parameters"]:
        if p["name"] in req.params:
            value = req.params[p["name"]]
            # 6-7: 按声明 type 强制转换——数值占位符被 str() 原样拼进代码，
            # 传 "1)\nos.system('x')#" 可注入任意代码；int/float 转换失败 422
            if p["type"] in ("int", "float") and not isinstance(value, bool):
                try:
                    value = int(value) if p["type"] == "int" else float(value)
                except (TypeError, ValueError):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"模板 '{req.template}' 参数 '{p['name']}' 需要 "
                            f"{p['type']} 类型，收到: {value!r}"
                        ),
                    )
            params[p["name"]] = value
        else:
            params[p["name"]] = p["default"]

    # 检查必填参数
    for p in tmpl["parameters"]:
        if p["required"] and not params.get(p["name"]):
            raise HTTPException(
                status_code=422,
                detail=f"模板 '{req.template}' 缺少必填参数: {p['name']} ({p['description']})",
            )

    # 渲染代码
    try:
        code = _render_template(tmpl["template"], params)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"模板渲染失败: {e}") from None

    # 通过 exec_python 执行
    import time as _time
    t0 = _time.perf_counter()
    try:
        from server.exec import ExecRequest, exec_python
        # 注：ExecRequest 已无 timeout 字段（extra="forbid"），内联等待由全局配置承担
        result = await exec_python(ExecRequest(code=code))
        elapsed = int((_time.perf_counter() - t0) * 1000)
        return TemplateExecResponse(
            success=result.success,
            template=req.template,
            exec_id=getattr(result, "exec_id", None),
            stdout=result.stdout,
            stderr=result.stderr,
            stdout_truncated=getattr(result, "stdout_truncated", None),
            elapsed_ms=elapsed,
            message="模板执行完成" if result.success else "模板执行失败",
        )
    except Exception as e:
        elapsed = int((_time.perf_counter() - t0) * 1000)
        return TemplateExecResponse(
            success=False, template=req.template,
            elapsed_ms=elapsed, message=f"执行异常: {e}",
        )
