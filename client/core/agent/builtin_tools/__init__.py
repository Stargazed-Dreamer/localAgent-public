"""v6.1 T09-T20: 内置基本工具集（agent 自带，不经 HTTP）

设计依据：
- spec D8.1：10 个内置基本工具（file_read/write/edit/grep/glob/ls/delete + ask_user + web_search/fetch）
- spec D8：内置工具在 client 本地执行，不依赖 server HTTP
- spec D12：自适应详略说明书（从 tool_specs.yaml 加载）
- 回收 v6-07:289-339 路径安全 4 重校验

工具列表：
1. file_read    — 读文件（offset/limit）
2. file_write   — 写文件（覆盖）
3. file_edit    — 精确字符串替换
4. file_grep    — 内容搜索（ripgrep）
5. file_glob    — 文件名匹配
6. file_ls      — 列目录
7. file_delete  — 删文件
8. ask_user     — 向用户提问
9. web_search   — 网络搜索
10. web_fetch   — 抓取网页
"""

from client.core.agent.builtin_tools.ask_user import AskUserTool
from client.core.agent.builtin_tools.base import BuiltinTool, is_path_safe
from client.core.agent.builtin_tools.file_delete import FileDeleteTool
from client.core.agent.builtin_tools.file_edit import FileEditTool
from client.core.agent.builtin_tools.file_glob import FileGlobTool
from client.core.agent.builtin_tools.file_grep import FileGrepTool
from client.core.agent.builtin_tools.file_ls import FileLsTool
from client.core.agent.builtin_tools.file_read import FileReadTool
from client.core.agent.builtin_tools.file_write import FileWriteTool
from client.core.agent.builtin_tools.web_fetch import WebFetchTool
from client.core.agent.builtin_tools.web_search import WebSearchTool

ALL_BUILTIN_TOOLS = [
    FileReadTool,
    FileWriteTool,
    FileEditTool,
    FileGrepTool,
    FileGlobTool,
    FileLsTool,
    FileDeleteTool,
    AskUserTool,
    WebSearchTool,
    WebFetchTool,
]

__all__ = [
    "BuiltinTool",
    "is_path_safe",
    "FileReadTool",
    "FileWriteTool",
    "FileEditTool",
    "FileGrepTool",
    "FileGlobTool",
    "FileLsTool",
    "FileDeleteTool",
    "AskUserTool",
    "WebSearchTool",
    "WebFetchTool",
    "ALL_BUILTIN_TOOLS",
]
