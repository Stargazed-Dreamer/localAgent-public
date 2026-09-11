"""exec_python 内部 runner：执行用户代码，支持 sync 和 async（top-level await）。

用法：python -X utf8 -u server/_exec_runner.py <code_file> [working_dir]

替代旧的 _PYTHON_STDIN_RUNNER（stdin 管道方式），改用文件方式：
- 用户代码写入 temp/exec_<uuid>.py
- 本 runner 读取该文件，编译时启用 PyCF_ALLOW_TOP_LEVEL_AWAIT
- 若 eval 结果是 awaitable，自动 asyncio.run
"""
import ast
import asyncio
import inspect
import sys


async def _await_result(awaitable):
    """把任意 awaitable 包装成 Coroutine（asyncio.run 只接受 Coroutine）"""
    return await awaitable


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: _exec_runner.py <code_file> [working_dir]", file=sys.stderr)
        sys.exit(2)

    code_file = sys.argv[1]
    working_dir = sys.argv[2] if len(sys.argv) > 2 else ""

    if working_dir and working_dir not in sys.path:
        sys.path.insert(0, working_dir)

    with open(code_file, encoding="utf-8") as f:
        source = f.read()

    globals_dict = {
        "__name__": "__main__",
        "__file__": code_file,
        "__package__": None,
    }
    code = compile(source, code_file, "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
    result = eval(code, globals_dict)
    if inspect.isawaitable(result):
        asyncio.run(_await_result(result))


if __name__ == "__main__":
    main()
