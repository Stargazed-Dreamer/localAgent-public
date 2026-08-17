"""MarkdownViewer — 轻量 Markdown 查看器/编辑器

基于 QTextEdit.setMarkdown()（PySide6 内置），支持标题/列表/代码块/加粗/斜体。
读模式渲染 markdown，编辑模式显示原始文本便于修改。
内部保存 _raw_md，避免读模式下 toPlainText() 丢失格式标记。
"""


from PySide6.QtWidgets import QTextEdit


class MarkdownViewer(QTextEdit):
    """Markdown 查看器/编辑器

    用法：
        viewer = MarkdownViewer()
        viewer.set_markdown("# 标题\\n内容")
        viewer.set_readonly(True)   # 渲染模式
        viewer.set_readonly(False)  # 编辑模式（显示原始 markdown）
        md = viewer.get_markdown()  # 取回（读模式返回原 md，编辑模式返回编辑后文本）
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._raw_md: str = ""
        self._readonly: bool = True
        self._editing: bool = False

        # 默认只读（样式由 lib/ui 全局主题统一处理）
        self.setReadOnly(True)

    def set_markdown(self, md: str) -> None:
        """设置 markdown 内容。根据当前模式渲染或显示原文。"""
        self._raw_md = md or ""
        if self._editing:
            self.setPlainText(self._raw_md)
        else:
            self.setMarkdown(self._raw_md)

    def get_markdown(self) -> str:
        """取回 markdown 文本。

        读模式：返回原始 markdown（可能已被 set_markdown 更新）
        编辑模式：返回用户编辑后的文本
        """
        if self._editing:
            return self.toPlainText()
        return self._raw_md

    def set_readonly(self, ro: bool) -> None:
        """切换只读/编辑模式。

        切换到只读：捕获编辑结果到 _raw_md，渲染 markdown
        切换到编辑：从 _raw_md 加载原文到编辑框
        """
        if ro == self._readonly:
            return

        if not ro:
            # 切换到编辑模式：保存当前 _raw_md，显示原文
            self._editing = True
            self.setPlainText(self._raw_md)
        else:
            # 切换到只读模式：捕获编辑结果，渲染 markdown
            self._raw_md = self.toPlainText()
            self._editing = False
            self.setMarkdown(self._raw_md)

        self._readonly = ro
        self.setReadOnly(ro)

    def is_editing(self) -> bool:
        """是否处于编辑模式"""
        return self._editing

    def discard_edit(self) -> None:
        """放弃编辑，恢复到原始 markdown（上次 set_markdown 的内容）。"""
        if self._editing:
            self._editing = False
            self._readonly = True
            self.setReadOnly(True)
            self.setMarkdown(self._raw_md)
