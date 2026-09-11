"""DocViewer 模块测试

测试 /docviewer 路由的状态查询、文档读取等功能。
docviewer 路由已在 server/main.py 中注册，使用 conftest.py 中的 client fixture。
"""

import tempfile
from pathlib import Path


class TestDocViewerStatus:
    """GET /docviewer/status - 模块状态查询"""

    def test_status_returns_200(self, client):
        """应返回 200"""
        resp = client.get("/docviewer/status")
        assert resp.status_code == 200

    def test_status_has_available(self, client):
        """应包含 available 字段"""
        data = client.get("/docviewer/status").json()
        assert "available" in data
        assert isinstance(data["available"], bool)

    def test_status_has_supported_formats(self, client):
        """应包含 supported_formats 列表"""
        data = client.get("/docviewer/status").json()
        assert "supported_formats" in data
        assert isinstance(data["supported_formats"], list)
        assert len(data["supported_formats"]) > 0

    def test_status_contains_pdf(self, client):
        """应支持 PDF 格式"""
        data = client.get("/docviewer/status").json()
        assert ".pdf" in data["supported_formats"]


class TestDocViewerRead:
    """POST /docviewer/read - 文档读取（JSON Body）"""

    def test_read_nonexistent_file_returns_404(self, client):
        """读取不存在的文件应返回 404"""
        resp = client.post("/docviewer/read", json={
            "path": "Z:/nonexistent/file.pdf",
            "pages": None,
        })
        assert resp.status_code == 404

    def test_read_unsupported_format_returns_400(self, client):
        """读取不支持的格式应返回 400"""
        # 创建一个临时 .txt 文件
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as f:
            f.write("hello")
            tmp_path = f.name
        try:
            resp = client.post("/docviewer/read", json={
                "path": tmp_path,
                "pages": None,
            })
            assert resp.status_code == 400
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_read_missing_path_returns_422(self, client):
        """缺少 path 字段应返回 422"""
        resp = client.post("/docviewer/read", json={})
        assert resp.status_code == 422


class TestDocViewerReadFile:
    """POST /docviewer/read/file - 文档读取（Form 上传）"""

    def test_read_file_unsupported_format_returns_400(self, client):
        """上传不支持的格式应返回 400"""
        resp = client.post(
            "/docviewer/read/file",
            files={"file": ("test.txt", b"hello", "text/plain")},
            data={"pages": ""},
        )
        assert resp.status_code == 400
