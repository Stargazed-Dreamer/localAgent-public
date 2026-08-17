"""MindForge 文档转换守护进程

作为常驻子进程运行，通过 stdin/stdout JSON 协议接收转换任务。
模型（MineRU、PaddleOCR）在首次转换时加载，之后常驻内存。

协议：
  请求: {"action": "convert", "source_path": "...", "output_dir": "..."}
  响应: {"status": "ok", "output_path": "..."} 或 {"status": "error", "detail": "..."}

  请求: {"action": "ping"}
  响应: {"status": "pong", "models_loaded": true/false}

  请求: {"action": "shutdown"}
  响应: {"status": "bye"} 然后退出
"""

import json
import sys
import os
import gc

# PaddlePaddle 兼容性
os.environ.setdefault("FLAGS_use_mkldnn", "0")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# 延迟加载标志
_models_loaded = False
_pdf_converter = None


def _ensure_models():
    """确保模型已加载"""
    global _models_loaded, _pdf_converter
    if _models_loaded:
        return

    from pipeline.pdf_converter import PDFConverter
    _pdf_converter = PDFConverter()
    _models_loaded = True


def handle_convert(source_path: str, output_dir: str = "") -> dict:
    """处理单个文件转换"""
    _ensure_models()

    source = os.path.abspath(source_path)
    if not os.path.exists(source):
        return {"status": "error", "detail": f"文件不存在: {source}"}

    try:
        result_path = _pdf_converter.convert_source_to_md(source, output_dir or None)
        gc.collect()  # 清理临时对象
        return {
            "status": "ok",
            "output_path": str(result_path) if result_path else None,
        }
    except Exception as e:
        gc.collect()
        return {"status": "error", "detail": str(e)}


def main():
    """主循环：从 stdin 读取 JSON 请求，写入 JSON 响应到 stdout"""
    # 写入 ready 信号
    sys.stdout.write(json.dumps({"status": "ready"}) + "\n")
    sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = {"status": "error", "detail": f"无效JSON: {e}"}
        else:
            action = req.get("action", "")

            if action == "ping":
                resp = {"status": "pong", "models_loaded": _models_loaded}

            elif action == "convert":
                source_path = req.get("source_path", "")
                output_dir = req.get("output_dir", "")
                resp = handle_convert(source_path, output_dir)

            elif action == "shutdown":
                sys.stdout.write(json.dumps({"status": "bye"}) + "\n")
                sys.stdout.flush()
                break

            else:
                resp = {"status": "error", "detail": f"未知action: {action}"}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
