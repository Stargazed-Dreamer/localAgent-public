"""文件分类工具的工作线程（QThread）

从 classifier_gui.py 拆出，避免主文件过大。
包含：FileScanThread / PredictThread / MoveFilesThread

依赖：predictor 模块（由 classifier_gui.py 入口先做 sys.path.insert 后再 import 本模块）
"""
import os
import shutil

from predictor import classify_folder, predict_categories
from PySide6.QtCore import QThread, Signal


class FileScanThread(QThread):
    """文件扫描线程，避免扫描大目录时冻结 GUI"""
    progress = Signal(int, int, str)  # current, total, message
    finished_signal = Signal(list)  # 文件信息列表

    def __init__(self, directory: str):
        super().__init__()
        self.directory = directory

    def run(self):
        files = []
        try:
            entries = list(os.scandir(self.directory))
            total = len(entries)
            for i, entry in enumerate(entries):
                # 跳过符号链接避免循环，同时扫描文件和文件夹（ticket 05）
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if not (is_dir or is_file):
                    continue
                try:
                    stat = entry.stat()
                    files.append({
                        "path": entry.path,
                        "filename": entry.name,
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "created": stat.st_ctime,
                        "is_dir": is_dir,  # ticket 05：标记文件夹
                    })
                except OSError:
                    continue
                if i % 50 == 0:
                    self.progress.emit(i, total, f"扫描中... {i}/{total}")
            # 文件夹置顶，其次按修改时间倒序（最新在前）
            files.sort(key=lambda x: (not x.get("is_dir", False), -x["modified"]))
            self.progress.emit(total, total, f"扫描完成，共 {len(files)} 个条目")
            self.finished_signal.emit(files)
        except Exception as e:
            self.progress.emit(0, 0, f"扫描失败: {e}")
            self.finished_signal.emit([])


class PredictThread(QThread):
    """LLM 预测线程（ticket 07：支持文件 + 文件夹混合预测）

    ticket F（R1 流式审核）：
    - item_predicted(dict)：每确定一条预测立即发出，供流式审核窗口实时渲染
    - 中断机制从零新增：request_stop() 置位 _should_stop，run() 在每批/每条前后检查
    """
    progress = Signal(int, int, str)  # current, total, message
    item_predicted = Signal(dict)     # 单条预测完成 {filename, category, confidence, ...}
    finished_signal = Signal(list)  # 预测结果列表
    error_signal = Signal(str)

    def __init__(self, sample_files: list, all_files: list, categories: list,
                 folder_entries: list = None,
                 source_dir: str = "", other_sessions: list = None):
        """
        Args:
            sample_files: 用户手动分类的样例文件（仅文件，不含文件夹）
            all_files: 待预测的文件列表（仅文件）
            categories: 分类配置
            folder_entries: 待预测的文件夹列表 [{"path", "filename", ...}, ...]
                           ticket 07 新增，走 classify_folder 类交互式协议
            source_dir: 当前会话源目录（ticket J：prompt 文字上下文）
            other_sessions: 其它会话摘要（ticket J：跨会话样本借补）
        """
        super().__init__()
        self.sample_files = sample_files
        self.all_files = all_files
        self.categories = categories
        self.folder_entries = folder_entries or []
        self.source_dir = source_dir
        self.other_sessions = other_sessions
        # ticket F：中断标志（从零新增，原代码无任何 stop 机制）
        self._should_stop = False
        self.stopped = False  # run 结束后标记是否因中断提前退出

    def request_stop(self):
        """请求中断预测（由 GUI 线程调用，线程安全：仅布尔标志置位）"""
        self._should_stop = True

    def run(self):
        try:
            all_predictions = []

            # 1. 文件预测（predict_categories 分批处理，逐条流式回调）
            if self.all_files and not self._should_stop:
                file_preds = predict_categories(
                    self.sample_files, self.all_files, self.categories,
                    use_llm=True,
                    progress_callback=lambda c, t, m: self.progress.emit(c, t, m),
                    item_callback=lambda p: self.item_predicted.emit(p),
                    should_stop=lambda: self._should_stop,
                    source_dir=self.source_dir,
                    other_sessions=self.other_sessions,
                )
                all_predictions.extend(file_preds)

            # 2. 文件夹预测（classify_folder 类交互式协议，逐个检查中断）
            total_folders = len(self.folder_entries)
            for i, folder in enumerate(self.folder_entries):
                if self._should_stop:
                    break
                self.progress.emit(i, total_folders,
                                   f"文件夹分类 {i+1}/{total_folders}: {folder['filename']}")
                try:
                    # B023 修复：用默认参数立即绑定 i/folder，避免回调异步触发时读到循环末值
                    result = classify_folder(
                        folder["path"], self.categories,
                        progress_callback=lambda cc, mc, m, _i=i, _f=folder: self.progress.emit(
                            _i, total_folders,
                            f"文件夹 {_f['filename']} - {m}（第 {cc}/{mc} 轮）"),
                    )
                    pred = {
                        "filename": folder["filename"],
                        "category": result["classification"],
                        "confidence": result["confidence"],
                        "is_folder": True,
                        "degraded": result.get("degraded", False),
                        "reason": result.get("reason", ""),
                    }
                except Exception as e:
                    # 单个文件夹分类失败不阻塞其他文件夹
                    pred = {
                        "filename": folder["filename"],
                        "category": "未知",
                        "confidence": 0.0,
                        "is_folder": True,
                        "degraded": True,
                        "reason": f"分类异常: {e}",
                    }
                all_predictions.append(pred)
                # ticket F：文件夹预测同样逐条流式发出
                self.item_predicted.emit(pred)

            self.stopped = self._should_stop
            self.finished_signal.emit(all_predictions)
        except Exception as e:
            self.error_signal.emit(f"{type(e).__name__}: {e}")


class MoveFilesThread(QThread):
    """文件/文件夹移动线程（ticket 07：文件夹冲突支持 skip/rename/merge）"""
    progress = Signal(int, int, str)  # current, total, message
    finished_signal = Signal(int, int)  # moved_count, skipped_count
    conflict_signal = Signal(str, str, bool)  # src, dst, is_folder - 需要用户决策（同步等待）
    error_signal = Signal(str)

    def __init__(self, moves: list, conflict_mode: str = "ask"):
        """
        Args:
            moves: [{"src": ..., "dst": ..., "is_folder": bool}, ...]
            conflict_mode: "ask" 询问 / "overwrite" 覆盖（仅文件） / "skip" 跳过 /
                           "rename" 重命名 / "merge" 合并（仅文件夹）
        """
        super().__init__()
        self.moves = moves
        self.conflict_mode = conflict_mode
        self._conflict_resolution = None  # 用户对冲突的决策
        self._apply_to_all = False

    def set_conflict_resolution(self, resolution: str, apply_to_all: bool):
        """用户对冲突的决策（由主线程调用）"""
        self._conflict_resolution = resolution
        self._apply_to_all = apply_to_all

    def run(self):
        moved = 0
        skipped = 0
        total = len(self.moves)
        for i, move in enumerate(self.moves):
            src = move["src"]
            dst = move["dst"]
            is_folder = move.get("is_folder", False)
            self.progress.emit(i, total, f"移动: {os.path.basename(src)}")

            if not os.path.exists(src):
                skipped += 1
                continue

            # 确保目标目录存在
            dst_dir = os.path.dirname(dst)
            if dst_dir:
                os.makedirs(dst_dir, exist_ok=True)

            # 处理冲突
            if os.path.exists(dst):
                resolution = self.conflict_mode
                # 文件夹冲突不支持 overwrite，强制降级为 ask
                if is_folder and resolution == "overwrite":
                    resolution = "ask"
                if resolution == "ask":
                    # 等待用户决策
                    self._conflict_resolution = None
                    self.conflict_signal.emit(src, dst, is_folder)
                    # 阻塞等待用户决策
                    while self._conflict_resolution is None:
                        self.msleep(50)
                    resolution = self._conflict_resolution
                    if self._apply_to_all:
                        self.conflict_mode = resolution

                if resolution == "skip":
                    skipped += 1
                    continue
                elif resolution == "overwrite" and not is_folder:
                    try:
                        os.remove(dst)
                    except OSError:
                        skipped += 1
                        continue
                elif resolution == "rename":
                    dst = self._get_unique_path(dst, is_folder)
                elif resolution == "merge" and is_folder:
                    # 合并文件夹：src 内的文件逐个移动到 dst，冲突走文件冲突逻辑
                    merged, merge_skipped = self._merge_folders(src, dst)
                    moved += merged
                    skipped += merge_skipped
                    continue

            try:
                shutil.move(src, dst)
                moved += 1
            except (OSError, shutil.Error) as e:
                self.error_signal.emit(f"移动失败 {os.path.basename(src)}: {e}")
                skipped += 1

        self.progress.emit(total, total, f"完成: 移动 {moved}，跳过 {skipped}")
        self.finished_signal.emit(moved, skipped)

    def _merge_folders(self, src: str, dst: str) -> tuple:
        """合并文件夹：src 内的文件/子文件夹逐个移动到 dst，冲突跳过

        Returns:
            (moved_count, skipped_count)
        """
        moved = 0
        skipped = 0
        try:
            entries = os.listdir(src)
        except OSError as e:
            self.error_signal.emit(f"读取文件夹失败 {os.path.basename(src)}: {e}")
            return 0, 0
        for entry in entries:
            src_entry = os.path.join(src, entry)
            dst_entry = os.path.join(dst, entry)
            if os.path.exists(dst_entry):
                # 冲突跳过（merge 模式不递归询问）
                skipped += 1
                continue
            try:
                shutil.move(src_entry, dst_entry)
                moved += 1
            except (OSError, shutil.Error) as e:
                self.error_signal.emit(f"合并移动失败 {entry}: {e}")
                skipped += 1
        # 尝试删除空源文件夹
        try:
            if not os.listdir(src):
                os.rmdir(src)
        except OSError:
            pass
        return moved, skipped

    def _get_unique_path(self, path: str, is_folder: bool = False) -> str:
        """生成不冲突的路径（添加编号后缀）

        文件夹无扩展名，直接在末尾加 _N；文件按 ext 拆分。
        """
        if is_folder:
            counter = 1
            while os.path.exists(f"{path}_{counter}"):
                counter += 1
            return f"{path}_{counter}"
        base, ext = os.path.splitext(path)
        counter = 1
        while os.path.exists(f"{base}_{counter}{ext}"):
            counter += 1
        return f"{base}_{counter}{ext}"
