"""文档查看路由 - 支持 PDF / Word / Excel / PPT 文档内容提取

依赖:
  - pymupdf (fitz): PDF 文本/表格/元信息提取
  - python-docx: Word .docx 文本提取
  - openpyxl: Excel .xlsx 数据提取（已在项目依赖中）
  - python-pptx: PowerPoint .pptx 文本提取
"""

import asyncio
import logging
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import Field

from lib.schema import BaseSchema

logger = logging.getLogger("localagent.docviewer")
router = APIRouter(prefix="/docviewer", tags=["DocViewer"])

# 支持的文件扩展名 → MIME 类型映射
SUPPORTED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


# ========== 响应模型 ==========

class DocContent(BaseSchema):
    """单页/单sheet/单slide的内容"""
    index: int          # 页码/sheet索引/slide编号（从1开始）
    title: str = ""     # 页面标题/sheet名/slide标题
    text: str           # 提取的文本内容
    tables: list[list[list[str]]] = Field(default_factory=list)  # 表格数据 [行[列[单元格]]]


class DocViewerResponse(BaseSchema):
    """文档查看的响应模型。
    用于封装文件查看器返回的文档信息、内容和元数据。
    属性:
        success (bool): 请求是否成功。
        file_name (str): 原始文件名。
        file_type (str): 文档类型。
        page_count (int): 文档的总页数、工作表数或幻灯片数。
        metadata (dict, optional): 文档的元信息，如作者、创建时间等。默认为空字典。
        content (list[DocContent]): 文档内容的分页或分部分列表。
        elapsed_ms (int): 处理文档所花费的时间，单位为毫秒。
    """
    success: bool
    file_name: str
    file_type: str      # 文档类型：pdf / docx / xlsx / pptx
    page_count: int     # 总页数/sheet数/slide数，具体含义取决于文件类型
    metadata: dict = Field(default_factory=dict)  # 文档元信息（作者、创建时间等），可选字段
    content: list[DocContent]
    elapsed_ms: int     # 处理耗时，单位为毫秒


class DocViewerStatusResponse(BaseSchema):
    """文档查看器状态响应模型。

    表示文档查看器的当前状态，用于封装查看器的可用性和支持格式信息。

    属性：
        available (bool): 指示文档查看器是否可用，True表示可用，False表示不可用。
        supported_formats (list[str]): 支持的文档格式列表，例如['pdf', 'docx']。
    """
    available: bool
    supported_formats: list[str]


# ========== 请求模型（MCP兼容） ==========

class DocViewerPathRequest(BaseSchema):
    """
    文档查看路径请求模型。

    用于封装请求查看特定文档文件所需的信息。

    参数:
        path (str): 文件的路径或地址。
        pages (Optional[str]): 可选参数，用于指定要查看的页码范围。
                               字符串格式示例: "1-5,8,10-12"。
                               若值为 None，则表示查看文档的全部页面。

    返回值:
        该类本身是一个数据模型，其实例将包含已验证的路径和页码信息。
    """
    path: str  # 必填参数，指定文档路径
    pages: str | None = None  # 页码范围，如 "1-5,8,10-12"，空=全部


# ========== 提取函数 ==========

def _parse_page_range(pages_str: str | None, total: int) -> list[int]:
    """解析页码范围字符串，返回1-based页码列表"""
    if not pages_str:
        return list(range(1, total + 1))
    result = set()
    for part in pages_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start), int(end)
            result.update(range(max(1, start), min(total, end) + 1))
        else:
            n = int(part)
            if 1 <= n <= total:
                result.add(n)
    return sorted(result)


def _extract_pdf(file_path: str, pages: str | None = None) -> dict:
    """提取 PDF 文档内容"""
    import fitz  # pymupdf

    doc = fitz.open(file_path)
    try:
        total = doc.page_count
        doc_meta = doc.metadata or {}
        metadata = {
            "title": doc_meta.get("title", ""),
            "author": doc_meta.get("author", ""),
            "subject": doc_meta.get("subject", ""),
            "creator": doc_meta.get("creator", ""),
            "creation_date": doc_meta.get("creationDate", ""),
            "mod_date": doc_meta.get("modDate", ""),
        }
        page_nums = _parse_page_range(pages, total)
        contents = []
        for page_num in page_nums:
            page = doc[page_num - 1]
            text = str(page.get_text("text"))

            # 提取表格
            tables = []
            try:
                tab = page.find_tables()
                if tab is not None:
                    for t in tab.tables:
                        table_data = []
                        for row in t.extract():
                            table_data.append([str(cell) if cell else "" for cell in row])
                        if table_data:
                            tables.append(table_data)
            except Exception:
                pass  # 表格提取失败不影响文本

            contents.append(DocContent(
                index=page_num,
                title=f"Page {page_num}",
                text=text,
                tables=tables,
            ))
        return {
            "page_count": total,
            "metadata": metadata,
            "content": contents,
        }
    finally:
        doc.close()


def _extract_docx(file_path: str, pages: str | None = None) -> dict:
    """提取 Word .docx 文档内容"""
    from docx import Document

    doc = Document(file_path)
    metadata = {
        "title": doc.core_properties.title or "",
        "author": doc.core_properties.author or "",
        "subject": doc.core_properties.subject or "",
        "created": str(doc.core_properties.created) if doc.core_properties.created else "",
        "modified": str(doc.core_properties.modified) if doc.core_properties.modified else "",
    }

    # 按段落分块，模拟"页"
    paragraphs = []
    for para in doc.paragraphs:
        if para.text.strip():
            paragraphs.append(para.text)

    # 提取表格
    all_tables = []
    for table in doc.tables:
        table_data = []
        for row in table.rows:
            table_data.append([cell.text for cell in row.cells])
        all_tables.append(table_data)

    # Word 没有真正的分页概念，把所有内容作为一"页"
    full_text = "\n".join(paragraphs)
    contents = [DocContent(
        index=1,
        title=metadata.get("title", "") or "Document",
        text=full_text,
        tables=all_tables,
    )]

    return {
        "page_count": 1,
        "metadata": metadata,
        "content": contents,
    }


def _extract_xlsx(file_path: str, pages: str | None = None) -> dict:
    """提取 Excel .xlsx 文档内容"""
    from openpyxl import load_workbook

    wb = load_workbook(file_path, read_only=True, data_only=True)
    try:
        metadata = {
            "title": wb.properties.title or "",
            "creator": wb.properties.creator or "",
            "created": str(wb.properties.created) if wb.properties.created else "",
            "modified": str(wb.properties.modified) if wb.properties.modified else "",
        }

        sheet_names = wb.sheetnames
        total = len(sheet_names)
        page_nums = _parse_page_range(pages, total)

        contents = []
        for idx in page_nums:
            sheet_name = sheet_names[idx - 1]
            ws = wb[sheet_name]
            rows_data = []
            for row in ws.iter_rows(values_only=True):
                rows_data.append([str(cell) if cell is not None else "" for cell in row])

            # 过滤掉全空行
            non_empty_rows = [r for r in rows_data if any(c.strip() for c in r)]
            text = "\n".join("\t".join(r) for r in non_empty_rows)

            contents.append(DocContent(
                index=idx,
                title=sheet_name,
                text=text,
                tables=[non_empty_rows] if non_empty_rows else [],
            ))

        return {
            "page_count": total,
            "metadata": metadata,
            "content": contents,
        }
    finally:
        wb.close()


def _extract_pptx(file_path: str, pages: str | None = None) -> dict:
    """提取 PowerPoint .pptx 文档内容"""
    from pptx import Presentation

    prs = Presentation(file_path)
    metadata = {
        "title": prs.core_properties.title or "",
        "author": prs.core_properties.author or "",
        "subject": prs.core_properties.subject or "",
        "created": str(prs.core_properties.created) if prs.core_properties.created else "",
        "modified": str(prs.core_properties.modified) if prs.core_properties.modified else "",
    }

    total = len(prs.slides)
    page_nums = _parse_page_range(pages, total)

    contents = []
    for idx in page_nums:
        slide = prs.slides[idx - 1]
        texts = []
        tables = []

        for shape in slide.shapes:
            text_frame = getattr(shape, "text_frame", None)
            if text_frame is not None:
                for para in text_frame.paragraphs:
                    text = para.text.strip()
                    if text:
                        texts.append(text)
            table = getattr(shape, "table", None)
            if table is not None:
                table_data = []
                for row in table.rows:
                    table_data.append([cell.text for cell in row.cells])
                tables.append(table_data)

        # 尝试获取slide标题
        slide_title = ""
        if slide.shapes.title:
            slide_title = slide.shapes.title.text

        contents.append(DocContent(
            index=idx,
            title=slide_title or f"Slide {idx}",
            text="\n".join(texts),
            tables=tables,
        ))

    return {
        "page_count": total,
        "metadata": metadata,
        "content": contents,
    }


# ========== 提取分发 ==========

EXTRACTORS = {
    ".pdf": _extract_pdf,
    ".docx": _extract_docx,
    ".xlsx": _extract_xlsx,
    ".pptx": _extract_pptx,
}


def _read_document(file_path: str, pages: str | None = None) -> DocViewerResponse:
    """读取文档并返回结构化内容"""
    path = Path(file_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {file_path}")

    ext = path.suffix.lower()
    if ext not in EXTRACTORS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持: {', '.join(SUPPORTED_EXTENSIONS.keys())}",
        )

    t0 = time.perf_counter()
    try:
        result = EXTRACTORS[ext](file_path, pages)
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"缺少依赖库: {e}") from None
    except Exception as e:
        logger.error(f"文档读取失败 [{file_path}]: {e}")
        raise HTTPException(status_code=500, detail=f"文档读取失败: {e}") from None

    elapsed = int((time.perf_counter() - t0) * 1000)
    return DocViewerResponse(
        success=True,
        file_name=path.name,
        file_type=ext.lstrip("."),
        page_count=result["page_count"],
        metadata=result["metadata"],
        content=result["content"],
        elapsed_ms=elapsed,
    )


# ========== 路由 ==========

@router.get("/status", response_model=DocViewerStatusResponse, operation_id="docviewer_status")
async def docviewer_status():
    """查询文档查看模块状态"""
    # 检查各库是否可导入
    available = True
    missing = []
    for mod_name in ["fitz", "docx", "openpyxl", "pptx"]:
        try:
            __import__(mod_name)
        except ImportError:
            missing.append(mod_name)
            available = False

    if missing:
        logger.warning(f"文档查看模块缺少依赖: {missing}")

    return DocViewerStatusResponse(
        available=available,
        supported_formats=list(SUPPORTED_EXTENSIONS.keys()),
    )


@router.post("/read", response_model=DocViewerResponse, operation_id="docviewer_read")
async def read_document(req: DocViewerPathRequest):
    """读取文档内容（JSON Body，MCP兼容）

    支持 PDF / Word(.docx) / Excel(.xlsx) / PowerPoint(.pptx)
    pages: 可选页码范围，如 "1-5,8,10-12"，空=全部
    """
    return await asyncio.to_thread(_read_document, req.path, req.pages)


@router.post("/read/file", response_model=DocViewerResponse, operation_id="docviewer_read_file")
async def read_document_file(file: UploadFile = File(...), pages: str = Form("")):  # noqa: B008
    """读取上传的文档内容（Form 上传）

    支持 PDF / Word(.docx) / Excel(.xlsx) / PowerPoint(.pptx)
    """
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in EXTRACTORS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {ext}，支持: {', '.join(SUPPORTED_EXTENSIONS.keys())}",
        )

    # 保存到临时文件（部分库需要文件路径而非字节流）
    contents = await file.read()
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        return await asyncio.to_thread(_read_document, tmp_path, pages if pages else None)
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
