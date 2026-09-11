# Media Classifier — 媒体分类工具集
使用 LLM 对多种媒体内容（视频、文章、歌词）进行自动分类和特征提取。

所有脚本通过后端 LLM 池（`http://127.0.0.1:8766`）调用，支持多 key 并发、断点续传。

注意不要直接使用脚本，大多是写死了路径，当前文件很可能已经移动且不适配用户构想。代码仅供参考，需要先明确用户真实意图。

## 工具列表

### <data_drive>:\<bilibili_videos>视频分类

两阶段流程：先分类，再按置信度移动文件。

```bat
:: Phase 1: 扫描 + 弹幕提取 + LLM 10类分类
python tools/media_classifier/bilibili_classifier.py

:: Phase 2: 按分类+置信度分层 move
python tools/media_classifier/bilibili_mover.py

:: 查看动漫分类结果
python tools/media_classifier/print_anime.py
```

- `bilibili_classifier.py` — 扫描 <data_drive>:\<bilibili_videos>视频目录，提取文件名+弹幕样本，LLM 并发分类（10类），输出 JSONL
- `bilibili_mover.py` — 读取分类结果，高置信(≥0.8)直接移动，低置信分到"待确认"目录
- `print_anime.py` — 打印"动漫二次元"分类的详细列表

### 文字篇章整理

```bat
:: 全量整理
python tools/media_classifier/wenzhang_classifier.py

:: 先扫描了解文件结构
python tools/media_classifier/scan_wenzhang.py
python tools/media_classifier/scan_docx.py
python tools/media_classifier/count_segments.py
```

- `wenzhang_classifier.py` — 解析 txt/docx/json（按空行分割文段），LLM 15类分类+特征提取，输出 JSONL+报告，支持断点续传
- `scan_wenzhang.py` — 扫描文件夹统计文件类型和数量
- `scan_docx.py` — 读取 docx 段落结构，判断是短文段集合还是长文章
- `count_segments.py` — 统计所有文件的文段数量

### 歌词分析

```bat
:: 分析 MuseArc 库中的歌词
python tools/media_classifier/mimo_lrc_analyze.py --musearc-db <external_project_root>\MuseArc\realLib\db\musearc.db --musearc-lib <external_project_root>\MuseArc\realLib

:: 分析独立 LRC 文件
python tools/media_classifier/mimo_lrc_analyze.py --test-dir "E:\<data_drive>:\<projects_root>\歌曲分类\测试文件"
```

- `mimo_lrc_analyze.py` — 使用 LLM 分析歌词的情感、主题、风格等。支持 MuseArc 数据库关联（保证歌词与歌曲对应关系）和独立 LRC 文件

## 前置条件

1. 后端服务运行中（`start.bat`）
2. LLM 池已配置可用 key
3. 各脚本内的源数据路径需根据实际情况修改

## 数据文件

运行时产生的状态和数据文件保存在 `temp/` 目录：
- `mimo_lrc_state.json` / `mimo_lrc_results.json` — 歌词分析进度和结果
- `llm_pool_stats_lrc.json` — LRC 分析的 token 统计
