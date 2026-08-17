# Recorder 模块端到端测试说明文档

> 覆盖 L0-L4 五层架构的所有功能与外观验证。配合 [recorder-guide.md](recorder-guide.md) 使用。

## 目录

- [一、测试环境准备](#一测试环境准备)
- [二、测试运行方式](#二测试运行方式)
- [三、L0 采集层测试](#三l0-采集层测试)
- [四、L1 处理层测试](#四l1-处理层测试)
- [五、L2 时间轴层测试](#五l2-时间轴层测试)
- [六、L3 编辑层测试](#六l3-编辑层测试)
- [七、L4 消费层测试](#七l4-消费层测试)
- [八、GUI 外观验收测试](#八gui-外观验收测试)
- [九、跨层端到端测试](#九跨层端到端测试)
- [十、工具面板集成测试](#十工具面板集成测试)
- [十一、测试文件索引](#十一测试文件索引)

---

## 一、测试环境准备

### 1.1 依赖确认

| 依赖 | 用途 | 验证命令 |
|------|------|----------|
| Python 3.12+ | 运行时 | `python --version` |
| .venv 虚拟环境 | 项目依赖隔离 | `.venv\Scripts\python.exe --version` |
| pynput | 键鼠传感器 | `.venv\Scripts\python.exe -c "import pynput"` |
| sounddevice | 音频传感器 | `.venv\Scripts\python.exe -c "import sounddevice"` |
| mss | 截图传感器 | `.venv\Scripts\python.exe -c "import mss"` |
| faster-whisper | P1 STT | `.venv\Scripts\python.exe -c "import faster_whisper"` |
| PySide6 | GUI 框架 | `.venv\Scripts\python.exe -c "import PySide6"` |
| comtypes | UIA 窗口操作 | `.venv\Scripts\python.exe -c "import comtypes"` |

### 1.2 权限要求

- **管理员权限**：键鼠传感器（pynput）和截图传感器（PrintWindow 被遮挡窗口）需要管理员权限。用 `start.bat` 启动后端会自动 UAC 提权
- **麦克风权限**：Windows 设置 → 隐私 → 麦克风 → 允许桌面应用访问
- **无头环境**：GUI 测试需要显示器。CI 环境用 `QT_QPA_PLATFORM=offscreen` 或跳过 GUI 测试

### 1.3 显示器配置

- 单屏：默认全屏录制（`--range fullscreen`）
- 双屏：注意虚拟屏偏移。`--range monitor --monitor-index 0/1` 指定屏幕
- DPI 缩放：高 DPI 显示器需确认截图分辨率与窗口实际分辨率匹配

### 1.4 config.toml 配置

确保 `config.toml` 的 `[recording]` 段存在（可从 `config.example.toml` 复制）：

```toml
[recording]
default_mode = "small"
default_range = "fullscreen"
default_detail_level = "detailed"
max_duration_seconds = 1800
audio_enabled = true
base_dir = "workspace/recorder/recordings"
```

---

## 二、测试运行方式

### 2.1 全量测试

```bash
# 运行所有 recorder 测试（25 个文件）
uv run python -m pytest tests/test_recorder_*.py -v --tb=short

# 运行并输出覆盖率
uv run python -m pytest tests/test_recorder_*.py -v --tb=short --cov=lib/recorder --cov=workspace/recorder
```

### 2.2 按层运行

```bash
# L0 采集层
uv run python -m pytest tests/test_recorder_controller.py tests/test_recorder_e2e.py tests/test_recorder_smoke.py tests/test_recorder_gui.py tests/test_recorder_monitor.py tests/test_recorder_audio.py tests/test_recorder_keyboard.py tests/test_recorder_mouse.py tests/test_recorder_screen.py tests/test_recorder_window.py tests/test_recorder_audio_trimmer.py -v

# L1 处理层
uv run python -m pytest tests/test_recorder_processor_*.py -v

# L2 时间轴层
uv run python -m pytest tests/test_recorder_timeline_*.py -v

# L3 编辑层
uv run python -m pytest tests/test_recorder_editor_*.py -v

# L4 消费层
uv run python -m pytest tests/test_recorder_consumer*.py -v
```

### 2.3 单文件运行

```bash
# 单个测试文件
uv run python -m pytest tests/test_recorder_controller.py -v

# 单个测试类
uv run python -m pytest tests/test_recorder_controller.py::TestRecordingStateMachine -v

# 单个测试函数
uv run python -m pytest tests/test_recorder_e2e.py::TestEndToEndFullFlow::test_start_pause_resume_pause_save_produces_full_package -v
```

### 2.4 GUI 测试注意事项

- GUI 测试使用 `pytest-qt` 或直接实例化 QWidget，需在主线程运行
- 无显示器环境设置 `QT_QPA_PLATFORM=offscreen`
- 测试不依赖真实键鼠输入，使用 MockSensor 模拟事件

### 2.5 冒烟测试（真实传感器）

```bash
# 3 秒无 GUI 冒烟测试（验证真实传感器链路）
.venv\Scripts\python.exe -m workspace.recorder.tools.main --no-gui --autostart --max-duration 3

# 检查生成的录制包
dir workspace\recorder\recordings\rec_*
```

---

## 三、L0 采集层测试

### 3.1 控制器状态机测试

**测试文件**：`tests/test_recorder_controller.py`

#### 3.1.1 状态机合法转换

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestRecordingStateMachine` | IDLE → start() → RECORDING | 状态变为 RECORDING，`is_running=True` |
| | RECORDING → pause() → PAUSED | 状态变为 PAUSED，`is_running=False` |
| | PAUSED → resume() → RECORDING | 状态变为 RECORDING，时间轴延续 |
| | RECORDING → save() → SAVED | 状态变为 SAVED（终止态） |
| | PAUSED → save() → SAVED | 状态变为 SAVED（终止态） |
| | RECORDING → discard() → IDLE | 状态变为 IDLE，录制包目录删除 |
| | PAUSED → discard() → IDLE | 状态变为 IDLE，录制包目录删除 |

#### 3.1.2 状态机非法转换

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestStateMachineIllegalTransitions` | IDLE → resume() | 抛出 `IllegalStateError` |
| | IDLE → save() | 抛出 `IllegalStateError` |
| | SAVED → pause() | 抛出 `IllegalStateError` |
| | SAVED → resume() | 抛出 `IllegalStateError` |
| | SAVED → save() | 抛出 `IllegalStateError` |
| | SAVED → discard() | 抛出 `IllegalStateError` |

#### 3.1.3 事件接受规则

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestStateMachineEventAcceptance` | RECORDING 状态 emit_event | 事件写入 events.jsonl，`event_count` 递增 |
| | PAUSED 状态 emit_event | 事件被拒绝（不写入），`event_count` 不变 |
| | SAVED 状态 emit_event | 事件被拒绝 |
| | IDLE 状态 emit_event | 事件被拒绝 |

#### 3.1.4 段与元数据

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestSegmentsAndMeta` | start 后 segments 有 1 段 | `segments[0].start_offset ≈ 0.0`，`end_offset=None` |
| | pause 后段关闭 | `segments[0].end_offset` 有值 |
| | resume 后新增段 | `len(segments) == 2` |
| | save 后 meta.json 写入 | `status="saved"`，`effective_duration` 正确 |
| | meta.json 含 sensors 列表 | 列出所有传感器类名 |

#### 3.1.5 save 回调 hook

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestSaveHook` | save() 后调用 on_trim_complete | 回调被调用，参数为录制包根路径 |
| | 回调异常不阻塞 save | save 仍正常完成，异常被记录 |

### 3.2 传感器测试

#### 3.2.1 音频传感器

**测试文件**：`tests/test_recorder_audio.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestAudioSensor` | start/stop 生命周期 | mic.wav 文件生成 |
| | pause/resume 追加模式 | 音频时间轴连续，瞬态噪声衰减 ×0.1（D033） |
| | duration_seconds 属性 | 返回录音时长（秒） |
| `TestAudioSensorIntegration` | 真实录音 2 秒 | mic.wav 可读，时长 ≈ 2s |

#### 3.2.2 键盘传感器

**测试文件**：`tests/test_recorder_keyboard.py`

| 验证点 | 预期结果 |
|--------|----------|
| 按键事件上报 | events.jsonl 含 keyboard_input 事件 |
| 密码框保护（D021） | IsPassword 框 → payload 含 `password_masked=true` |
| 物理按键序列（D022） | supplements.physical_keys 保留物理按键序列 |
| UIA ValuePattern 差值 | 只记录变化部分，不重复记录不变值 |

#### 3.2.3 鼠标传感器

**测试文件**：`tests/test_recorder_mouse.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestNormalizeButton` | 按钮名称归一化 | left/right/middle 标准化 |
| `TestMouseSensorLifecycle` | start/stop | 传感器正常启停 |
| `TestMouseClick` | 单击事件 | events.jsonl 含 mouse_click，payload 有 x/y/button |
| `TestMouseDrag` | 拖拽事件 | 检测拖拽起止，记录路径 |
| `TestMouseScroll` | 滚动事件 | events.jsonl 含 mouse_scroll，payload 有 dx/dy |
| `TestOnClickCallback` | click 后多帧采样（D023） | 50/100/150ms 各采一帧，pHash 去重 |
| `TestMouseSensorIntegration` | 真实鼠标操作 | 事件正确记录 |

#### 3.2.4 截图传感器

**测试文件**：`tests/test_recorder_screen.py`

| 验证点 | 预期结果 |
|--------|----------|
| 定时截图 | frames/ 目录有 PNG 文件，命名 `frame_{ms}_{seq}.png` |
| detailed 模式 | 500ms 间隔截图 |
| coarse 模式 | 2s 间隔截图 |
| pHash 去重（D035/D036） | 相邻帧汉明距离 ≤ 5 的不保存 |
| PrintWindow + PW_RENDERFULLCONTENT（D024） | 被遮挡窗口也能截取 |
| 帧计数 | `frame_count` 与实际文件数一致 |

#### 3.2.5 窗口焦点传感器

**测试文件**：`tests/test_recorder_window.py`

| 验证点 | 预期结果 |
|--------|----------|
| 焦点切换检测 | focus.jsonl 含 focus_change 事件 |
| 窗口标题/进程名 | payload 含 window_title / process_name |
| 轮询间隔 | 不遗漏快速切换（轮询频率足够） |

#### 3.2.6 剪贴板传感器

| 验证点 | 预期结果 |
|--------|----------|
| 文本变化检测 | events.jsonl 含 clipboard_text 事件 |
| 图片变化检测 | events.jsonl 含 clipboard_image 事件 |
| 密码框自动隐藏 | 焦点在密码框时不采集 |
| 默认关闭 | config.toml `clipboard_enabled=false` 时不启动 |

#### 3.2.7 音频裁剪器

**测试文件**：`tests/test_recorder_audio_trimmer.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestRmsComputation` | RMS 计算 | 返回正确 dBFS 值 |
| `TestSilenceTrimming` | 静音裁剪 | RMS < 阈值且 > 最小时长的段被裁剪 |
| `TestSegmentsJsonFormat` | audio_segments.json | 格式正确，含原始/裁剪后时间映射 |
| `TestKeyMouseProtection` | 键鼠操作保护 | 有键鼠操作的时间段不被裁剪 |
| `TestCroppedWavReadable` | 裁剪后 WAV | mic_cropped.wav 可被 soundfile 读取 |
| `TestEdgeCases` | 边界情况 | 空音频/全静音/极短音频不崩溃 |

### 3.3 录制包结构测试

**测试文件**：`tests/test_recorder_controller.py::TestRecordingPackage`

| 验证点 | 预期结果 |
|--------|----------|
| 目录命名 | `rec_{YYYYMMDD}_{HHMMSS}/` 格式 |
| meta.json 存在 | 含 start_time/end_time/event_count/frame_count/status |
| events.jsonl 存在 | 每行一个 JSON 事件 |
| focus.jsonl 存在 | 仅含 focus_change 事件 |
| frames/ 目录 | 截图 PNG 文件 |
| audio/mic.wav | 音频文件（D034：完整保留不自动裁剪） |

### 3.4 GUI 小模式测试（FloatingBar）

**测试文件**：`tests/test_recorder_gui.py`

#### 3.4.1 功能测试

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestRecordingConfig` | RecordingConfig dataclass | 工厂函数 `make_detailed_config` / `make_coarse_config` 返回正确参数 |
| `TestListAvailableWindows` | 窗口列表 API | 返回可用窗口清单（hwnd + title） |
| `TestConfigDialog` | 配置对话框 | 四分组（范围/模式/采集/去重）可交互 |
| `TestHotkeyManager` | 全局热键 | Ctrl+Alt+R 注册成功，触发 start/stop 信号 |
| `TestFloatingBar` | 悬浮条基础 | start_requested / stop_requested 信号正确 |
| `TestFloatingBarThreeButtons` | 三按钮交互 | PAUSED 显示 ▶继续 / 💾保存 / 🗑丢弃 |
| `TestRecorderApp` | 协调类 | 传感器生命周期 + 30 分钟自动停止 + save 后启动编辑器 |

#### 3.4.2 小模式外观验收

| 验收项 | 标准 |
|--------|------|
| 窗口尺寸 | 高度 44px，最小宽度 520px |
| 窗口属性 | `WindowStaysOnTopHint + FramelessWindowHint + Tool` |
| 透明度 | 0.90 |
| 背景色 | 暗色 `#272a29` |
| 状态灯 | IDLE=灰 / REC=红色脉动 |
| 计时器 | `HH:MM:SS` 格式，录制中递增 |
| 计数器 | `events: N` / `frames: N` 实时更新 |
| 麦克风按钮 | `🎤 麦克风:开` / `🎤 麦克风:关` 切换 |
| 录制按钮 | IDLE=`● 开始录制` / RECORDING=`⏹ 停止录制` |
| PAUSED 三按钮 | `▶ 继续录制` / `💾 保存结束` / `🗑 丢弃重录` |
| 丢弃确认 | 弹出 `QMessageBox.warning` 确认对话框 |
| 拖拽 | 鼠标按空白区域可拖动窗口 |
| 初始位置 | `move_to_top_center()` 定位到屏幕顶部中央 |

### 3.5 GUI 大模式测试（MonitorWindow）

**测试文件**：`tests/test_recorder_monitor.py`

#### 3.5.1 功能测试

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestBytesToInt16Samples` | 字节转采样 | 正确转换 int16 数组 |
| `TestEventStreamPanel` | 事件流面板 | 增量读取 events.jsonl，显示最新事件 |
| `TestFramePreviewPanel` | 截图预览 | 显示最新帧 PNG |
| `TestAudioWaveformPanel` | 音频波形 | 绘制 RMS dBFS 柱状图 |
| `TestWaveformCanvas` | 波形画布 | QPainter 不崩溃 |
| `TestWindowInfoPanel` | 窗口信息 | 显示当前焦点窗口标题/进程 |
| `TestStatsPanel` | 统计面板 | 显示事件数/帧数/时长/音频大小 |
| `TestMonitorWindowConstruction` | 窗口构造 | 5 面板 Splitter 布局正确 |
| `TestMonitorWindowSignals` | 信号 | start/stop/mic_toggle/resume/save/discard 信号正确 |
| `TestMonitorWindowAttach` | attach/detach | `attach_recorder` 启动轮询，`detach_recorder` 停止 |
| `TestReadNewEvents` | 增量读事件 | 只读新增事件，不重复 |
| `TestReadLatestWindow` | 最新窗口 | 返回 focus.jsonl 最后一条 |
| `TestReadLatestFrame` | 最新帧 | 返回 frames/ 最新 PNG |
| `TestMonitorWindowOnTick` | 500ms 轮询 | 事件流/窗口/统计/截图预览刷新 |
| `TestMonitorWindowOnAudioTick` | 1000ms 轮询 | 音频波形/大小刷新 |
| `TestMonitorWindowClose` | 关闭 | detach_recorder，清理 QTimer |
| `TestMonitorWindowThreeButtons` | 三按钮 | PAUSED 显示继续/保存/丢弃 |

#### 3.5.2 大模式外观验收

| 验收项 | 标准 |
|--------|------|
| 窗口尺寸 | 最小 900x640，默认 1100x760 |
| 窗口属性 | 独立顶层窗口（不置顶） |
| 背景色 | 暗色主题（同小模式） |
| 顶部状态栏 | 状态灯 + 计时器 + 录制包名 |
| 上半区 Splitter | 左=截图预览 / 右=事件流 |
| 下半区 Splitter | 录制参数+窗口信息 / 音频波形 / 统计面板 |
| 底部按钮栏 | 麦克风开关 + 开始/停止 + 三按钮（PAUSED 时） |
| 轮询频率 | 500ms（事件/窗口/统计/截图）+ 1000ms（音频） |

### 3.6 配置对话框测试

**测试文件**：`tests/test_recorder_gui.py::TestConfigDialog`

| 验收项 | 标准 |
|--------|------|
| 窗口标题 | "新建录制" |
| 录制范围组 | 全屏 / 某屏（QComboBox + 刷新）/ 某窗口（QComboBox + 刷新） |
| 录制模式组 | 详细（500ms+UIA+音频）/ 粗略（2s+音频）/ 自定义（间隔+Burst+最大时长） |
| 采集选项组 | 麦克风（QCheckBox + 设备选择）/ 剪贴板（默认关）/ UIA 树（disabled） |
| 截图去重组 | pHash 阈值滑块（0-16，默认 5） |
| 按钮 | 取消 / 开始录制（默认） |
| 校验 | 选 monitor 无显示器提示 / 选 window 无窗口提示 / 无麦克风确认 |

### 3.7 CLI 参数测试

**测试文件**：`tests/test_recorder_smoke.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestArgParser` | `--mode small/large` | 正确解析 |
| | `--range fullscreen/monitor/window` | 正确解析 |
| | `--detail-level detailed/coarse` | 正确解析 |
| | `--max-duration 600` | 整数解析 |
| | `--no-audio` | flag 解析为 True |
| | `--autostart` | flag 解析为 True |
| | `--no-gui` | flag 解析为 True |
| | 非法 mode | argparse 拒绝 |
| `TestMergeCliAndConfig` | CLI 覆盖 config | CLI 参数优先 |
| | CLI None 用 config | 未指定时用 config.toml 值 |
| `TestConfigLoader` | 无 config 返回默认 | 不报错 |
| | 从 toml 加载 | 正确读取 [recording] 段 |
| | 无效 toml 返回默认 | 不报错 |
| | base_dir 相对/绝对路径 | 正确解析 |
| `TestBuildRecordingConfig` | `make_detailed_config` | 500ms 间隔 + burst + UIA |
| | `make_coarse_config` | 2s 间隔 + burst |
| | 窗口范围 | hwnd 正确 |
| | 自定义截图参数 | 间隔/burst 可自定义 |

### 3.8 端到端录制流程测试

**测试文件**：`tests/test_recorder_e2e.py`

| 测试类 | 测试函数 | 验证点 |
|--------|----------|--------|
| `TestEndToEndFullFlow` | `test_start_pause_resume_pause_save_produces_full_package` | start→pause→resume→pause→save 生成完整包，meta.json status=saved |
| | `test_save_from_recording_state_preserves_audio` | 从 RECORDING 直接 save，mic.wav 保留 |
| | `test_no_audio_save_marks_saved_directly` | 无音频录制 save 直接标记 saved |
| | `test_discard_after_pause_deletes_package` | pause→discard 录制包目录删除 |
| `TestTimelineContinuityE2E` | `test_resume_timeline_continues_with_real_events` | resume 后时间戳连续不跳跃 |
| | `test_three_segments_two_pauses` | 三段录制两次暂停，segments 长度=3 |
| `TestIllegalTransitionsE2E` | `test_save_then_pause_raises` | SAVED→pause 抛异常 |
| | `test_save_then_resume_raises` | SAVED→resume 抛异常 |
| | `test_save_then_discard_raises` | SAVED→discard 抛异常 |

### 3.9 冒烟测试

**测试文件**：`tests/test_recorder_smoke.py::TestSmokeRecording`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_smoke_recording_produces_valid_package` | 3 秒无 GUI 录制 | 生成有效录制包：meta.json + events.jsonl + frames/ |

**手动冒烟测试**：

```bash
# 3 秒冒烟
.venv\Scripts\python.exe -m workspace.recorder.tools.main --no-gui --autostart --max-duration 3

# 验证录制包
.venv\Scripts\python.exe -c "
import json; from pathlib import Path
p = sorted(Path('workspace/recorder/recordings').glob('rec_*'))[-1]
m = json.loads((p/'meta.json').read_text(encoding='utf-8'))
print(f'包名: {p.name}')
print(f'状态: {m[\"status\"]}')
print(f'事件数: {m[\"event_count\"]}')
print(f'帧数: {m[\"frame_count\"]}')
print(f'时长: {m[\"effective_duration\"]:.1f}s')
"
```

---

## 四、L1 处理层测试

### 4.1 P1 STT 测试

**测试文件**：`tests/test_recorder_processor_p1.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestIsJunkText` | 垃圾文本检测 | 重复/无意义文本被标记为 junk |
| `TestNormalizeLufs` | LUFS 归一化 | 音频响度归一化到目标 LUFS |
| `TestNormalizePercentile` | 百分位归一化 | 25% 分位归一化 |
| `TestTranscribeWithFallback` | 渐进式重试（D038） | 原始→LUFS(-15→-12→-9→-6)→25%分位→跳过 |
| `TestDoTranscribe` | 单次转录 | 返回词级时间戳片段 |
| `TestWhisperToolLifecycle` | WhisperTool 生命周期 | 线程安全 + 异步任务 |
| `TestLoadWavAsArray` | WAV 加载 | 正确加载为 numpy 数组 |
| `TestWriteTranscriptJson` | transcript.json 写入 | 格式正确，含 segments + 词级时间戳 |
| `TestRunP1EndToEnd` | P1 端到端 | audio/mic.wav → transcript.json |
| `TestPipelineP1Integration` | Pipeline P1 集成 | `process_recording_package(run_p1=True)` 产出 transcript.json |

### 4.2 P4 事件聚合测试

**测试文件**：`tests/test_recorder_processor_p4.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestAggregateEventsBasic` | 基本事件聚合 | mouse_click/keyboard_input/focus_change 聚合为操作块 |
| `TestIdleBlockGeneration` | idle 块生成 | 连续无操作超阈值生成 idle 块 |
| `TestFrameAssociation` | 帧关联 | 操作块关联 before_frame / after_frame |
| `TestRunP4EndToEnd` | P4 端到端 | events.jsonl + frames/ → blocks.json |
| `TestProcessRecordingPackageSeam` | pipeline seam | `process_recording_package(run_p4=True)` 产出 blocks.json |

### 4.3 P5 关键帧抽取测试

**测试文件**：`tests/test_recorder_processor_p5.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestOperationFrameRetention` | 操作帧保留 | mouse_click/keyboard_input 块的帧全部保留 |
| `TestTimerFrameGlobalDedup` | 定时帧全局去重 | pHash 汉明距离 ≤ 阈值的定时帧去重 |
| `TestKeyframesJsonFormat` | keyframes.json 格式 | 含 block_id + frame_path + timestamp |
| `TestRunP5EndToEnd` | P5 端到端 | blocks.json + frames/ → keyframes.json |

### 4.4 P2/P3 接口测试

**测试文件**：`tests/test_recorder_processor_e2e.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestP2Interface` | P2 Protocol 定义 | `UIASnapshotter.snapshot()` 方法存在 |
| | 无 snapshotter 返回 None | `run_p2()` 不崩溃 |
| | 委托 snapshotter | 正确调用 snapshot 方法 |
| | 默认 max_depth | 未指定时用默认值 |
| | snapshotter 失败 | 返回 None，不崩溃 |
| `TestP3Interface` | P3 Protocol 定义 | `VLAnnotator.describe()` 方法存在 |
| | 无 annotator 返回 None | `run_p3()` 不崩溃 |
| | 委托 annotator | 正确调用 describe 方法 |
| | 默认 max_chars | 未指定时用默认值 |
| | 空 question | 抛出 `ValueError` |

### 4.5 完整流水线测试

**测试文件**：`tests/test_recorder_processor_e2e.py`

| 测试类 | 测试函数 | 验证点 |
|--------|----------|--------|
| `TestP4P5EndToEnd` | `test_p4_p5_produce_both_json` | P4+P5 同时产出 blocks.json + keyframes.json |
| | `test_p4_p5_default_options` | 默认参数正常工作 |
| | `test_p4_p5_no_p1` | 不跑 P1 也能产出 P4/P5 |
| `TestP1EndToEnd` | `test_p1_produces_transcript_json` | P1 产出 transcript.json |
| | `test_p1_vad_threshold_passed_through` | VAD 阈值正确传递 |
| | `test_p1_missing_wav_records_error` | 缺 WAV 时记录错误到 errors |
| `TestTimestampAlignment` | `test_all_three_share_same_timeline` | 三产出共享同一时间轴基准 |
| | `test_block_timestamps_match_original_events` | 块时间戳匹配原始事件 |
| | `test_keyframe_timestamps_match_frame_events` | 关键帧时间戳匹配帧事件 |
| | `test_transcript_timestamps_independent_axis` | 转写时间戳独立轴 |
| `TestFullPipelineEndToEnd` | `test_full_pipeline_all_three_json` | 三 JSON 全产出 |
| | `test_full_pipeline_p1_failure_does_not_break_p4_p5` | P1 失败不影响 P4/P5 |
| | `test_full_pipeline_p4_failure_does_not_break_p5_p1` | P4 失败不影响 P5/P1 |
| | `test_empty_options_uses_defaults` | 空参数用默认 |
| | `test_none_options_uses_defaults` | None 参数用默认 |
| | `test_string_package_path_accepted` | 字符串路径接受 |

### 4.6 ProcessingResult 数据结构验证

| 字段 | 类型 | 说明 |
|------|------|------|
| `package_root` | Path | 录制包根路径 |
| `blocks_path` | Path \| None | P4 产出路径 |
| `keyframes_path` | Path \| None | P5 产出路径 |
| `transcript_path` | Path \| None | P1 产出路径 |
| `block_count` | int | 操作块数量 |
| `keyframe_count` | int | 关键帧数量 |
| `transcript_segment_count` | int | 转写片段数 |
| `errors` | dict[str, str] | 错误映射（key="p1"/"p4"/"p5"） |

---

## 五、L2 时间轴层测试

### 5.1 时间轴构建测试

**测试文件**：`tests/test_recorder_timeline_builder.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestOperationBlockMerge` | 操作块合并 | 相邻同类操作块合并 |
| `TestTimestampSorting` | 时间戳排序 | 所有块按 timestamp 升序 |
| `TestTimelineJsonFormat` | timeline.json 格式 | 含 recording_id/duration_seconds/tracks/blocks |
| `TestMissingFileDegradation` | 缺失文件降级 | 缺 blocks.json/keyframes.json/transcript.json 不崩溃 |
| `TestTimelineResult` | TimelineResult | 字段正确填充 |
| `TestImageBlockMerge` | 图片块合并 | 相邻截图合并 |
| `TestTextBlockMerge` | 文本块合并 | 相邻转写合并 |
| `TestAudioTimestampMapping` | 音频时间戳映射（D039） | audio_segments.json 正确映射 |
| `TestMissingKeyframesTranscript` | 缺失 keyframes/transcript | 降级处理不崩溃 |
| `TestThreeWayMerge` | 三方合并 | blocks + keyframes + transcript 正确合并 |
| `TestChapterGeneration` | 章节生成 | focus_change + idle 触发章节 |

### 5.2 章节切分测试

**验证点**：

| 规则 | 预期结果 |
|------|----------|
| focus_change 触发切分（D042） | 窗口切换时生成新章节 |
| idle > 阈值触发切分 | 连续空闲超阈值切新章节 |
| 首章 timestamp=0.0 | 第一个章节时间戳为 0 |
| 连续同名窗口去重 | 不生成重复章节 |
| 空操作块不生成章节 | 无事件的段不切分 |
| 自定义阈值 | `--idle-threshold 3.0` 改变切分行为 |

### 5.3 annotations 初始化测试

**测试文件**：`tests/test_recorder_timeline_annotator.py`

| 测试类 | 验证点 | 预期结果 |
|--------|--------|----------|
| `TestInitAnnotations` | 初始化空 annotations.json（D043） | 文件存在，annotations 数组为空 |
| | 已存在不覆盖 | annotations.json 已有时不被覆盖 |
| `TestFormatPreview` | 预览格式化 | 6 项摘要文本（包名/时长/块数/章节数/轨道/首尾时间） |
| `TestTimelineCli` | CLI 入口 | `python -m workspace.recorder.tools.timeline_cli <path>` 正常运行 |

### 5.4 端到端测试

**测试文件**：`tests/test_recorder_timeline_e2e.py`

| 测试类 | 测试函数 | 验证点 |
|--------|----------|--------|
| `TestV1RecordingNoCropping` | V1 录制包（无裁剪） | build_timeline 成功，格式/排序/ID/首章/tracks 正确 |
| | 无音频段 | 不崩溃，audio 轨道为空 |
| | annotations 初始化 | annotations.json 生成 |
| | CLI 预览 | `format_preview` 输出正确 |
| `TestV2RecordingWithCropping` | V2 录制包（有裁剪） | 有 audio_segments.json，音频映射正确 |
| | build + 格式 + 排序 | 同 V1 |
| `TestV2RecordingChapterSplitting` | 章节切分 | 章节数/章节名/排序正确 |
| | idle 阈值 | 自定义阈值改变切分 |
| `TestCrossRecordingComparison` | 跨录制包对比 | 所有录制包都能生成有效 timeline |

### 5.5 timeline.json 格式验证

```json
{
  "recording_id": "rec_20260723_141608",
  "duration_seconds": 58.5,
  "tracks": ["chapter", "operation", "image", "text", "audio"],
  "blocks": [
    {"id": "c001", "type": "chapter", "timestamp": 0.0, "primary": {"name": "..."}, "status": {}, "supplements": {}},
    {"id": "b001", "type": "mouse_click", "timestamp": 1.23, "primary": {"x": 100, "y": 200}, "status": {}, "supplements": {}},
    {"id": "f001", "type": "screenshot", "timestamp": 1.23, "primary": {"frame_path": "frames/..."}, "status": {}, "supplements": {}},
    {"id": "t001", "type": "stt_transcript", "timestamp": 5.0, "primary": {"text": "..."}, "status": {}, "supplements": {}}
  ]
}
```

| 字段 | 验证点 |
|------|--------|
| `recording_id` | 与目录名一致 |
| `duration_seconds` | 与 meta.json effective_duration 一致 |
| `tracks` | 包含 5 个轨道 |
| `blocks[].id` | 唯一，前缀 c=chapter/b=operation/f=image/t=text |
| `blocks[].timestamp` | 升序排列 |
| `blocks[].status` | 初始为空对象 |
| `blocks[].supplements` | 初始为空对象 |

---

## 六、L3 编辑层测试

### 6.1 13 个 Action 测试

**测试文件**：`tests/test_recorder_editor_apply.py`

| 测试函数 | Action | 验证点 |
|----------|--------|--------|
| `test_mark_key` | `mark_key` | 块 status.marked_key=True |
| `test_mark_anomaly` | `mark_anomaly` | 块 status.marked_anomaly=True |
| `test_mark_automatable` | `mark_automatable` | 块 status.marked_automatable=True |
| `test_mark_multiple_states_coexist` | 多状态 | 三个标记可叠加不互斥（D015） |
| `test_trim` | `trim` | 块 status.is_trimmed=True（软删除） |
| `test_restore` | `restore` | 块 status.is_trimmed=False |
| `test_trim_then_restore` | trim→restore | 先裁剪后恢复，状态正确 |
| `test_insert_note_after` | `insert_note` | 在父块后插入注释块，position=after |
| `test_insert_note_supplement` | `insert_note` | 作为父块素材插入，position=supplement |
| `test_insert_note_timestamp` | `insert_note` | 在指定时间戳插入，position=timestamp |
| `test_insert_chapter` | `insert_chapter` | 插入章节块 |
| `test_insert_image` | `insert_image` | 插入参考图片块 |
| `test_insert_text` | `insert_text` | 插入文件文本块 |
| `test_rename_chapter` | `rename_chapter` | 章节名称更新 |
| `test_merge_blocks` | `merge_blocks` | 相邻同类块合并 |
| `test_split_block` | `split_block` | 在指定时间戳拆分 |
| `test_move_supplement` | `move_supplement` | 素材附属关系转移 |
| `test_undone_annotation_skipped` | 已撤销 | apply 时跳过 undone 的 annotation |
| `test_target_block_not_found` | 目标不存在 | 不崩溃，返回原块列表 |

### 6.2 AnnotationStore 测试

**测试文件**：`tests/test_recorder_editor_store.py`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_add_generates_id` | 添加生成 ID | 自动生成唯一 annotation ID |
| `test_add_pushes_undo_stack_clears_redo` | undo/redo 栈 | add 压 undo 栈，清空 redo 栈 |
| `test_undo_marks_undone` | 撤销 | annotation.undone=True |
| `test_undo_empty_stack_returns_false` | 空 undo 栈 | 返回 False |
| `test_redo_restores` | 重做 | annotation.undone=False |
| `test_redo_empty_stack_returns_false` | 空 redo 栈 | 返回 False |
| `test_undo_redo_sequence` | undo/redo 序列 | 多次 undo/redo 状态正确 |
| `test_finalize` | 标记就绪（D053） | finalized=True |
| `test_save_load_roundtrip` | 保存加载往返 | JSON 序列化/反序列化正确 |
| `test_save_filters_undone` | 保存过滤 | undone 的 annotation 不写入文件 |
| `test_load_nonexistent_file` | 加载不存在文件 | 不崩溃，返回空 store |
| `test_stats` | 统计 | 返回各 action 计数 |
| `test_stats_excludes_undone` | 统计排除 | undone 的 annotation 不计入统计 |

### 6.3 merge 测试

**测试文件**：`tests/test_recorder_editor_merge.py`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_merge_no_annotations_returns_original` | 无 annotation | 返回原始 timeline |
| `test_merge_with_mark_key` | 应用 mark_key | merge 后块含 marked_key |
| `test_merge_filters_trimmed_blocks` | 过滤裁剪块 | is_trimmed 块不出现 |
| `test_merge_filters_undone_annotations` | 过滤已撤销 | undone 的 annotation 不应用 |
| `test_merge_finalized_field` | finalized 字段 | merge 结果含 finalized 布尔值 |
| `test_merge_rename_chapter` | 章节重命名 | merge 后章节名更新 |
| `test_merge_insert_note` | 插入注释 | merge 后含插入的注释块 |
| `test_merge_multiple_states_coexist` | 多状态 | 三标记共存 |
| `test_merge_move_supplement` | 移动素材 | 附属关系正确转移 |
| `test_is_finalized_no_file/true/false` | is_finalized | 无文件=False / finalized=true=True / false=False |
| `test_merge_no_timeline` | 无 timeline | 不崩溃 |
| `TestMergeRealPackages` | 真实录制包 | 真实包 merge 正确 |

### 6.4 音频调参测试

**测试文件**：`tests/test_recorder_editor_audio.py`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_preview_trim_basic` | 基本切片预览 | 返回保留段/删除段列表 |
| `test_preview_trim_all_silence` | 全静音 | 不崩溃，返回全删除 |
| `test_preview_trim_threshold_effect` | 阈值效果 | 改变阈值改变保留段 |
| `test_preview_trim_returns_rms` | 返回 RMS | 含 RMS dBFS 数组 |
| `test_waveform_paint_no_crash` | 波形绘制 | QPainter 不崩溃 |
| `test_waveform_empty_data` | 空数据 | 不崩溃 |
| `test_audio_panel_load_audio` | AudioPanel 加载 | 正确加载 mic.wav |
| `test_audio_panel_threshold_slider_updates_preview` | 滑块更新 | 阈值滑块变化触发预览更新 |
| `test_audio_panel_no_wav` | 无 WAV | 不崩溃，显示提示 |
| `test_stt_runner_init` | STTRunner 初始化 | QThread 可启动 |
| `test_preview_trim_real_package` | 真实包 | 真实录制包切片预览正确 |

### 6.5 编辑器 GUI 测试

**测试文件**：`tests/test_recorder_editor_gui.py`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_chapter_list_builds_from_timeline` | 章节列表 | 从 timeline 构建章节项 |
| `test_chapter_list_click_emits_signal` | 点击章节 | 发出跳转信号 |
| `test_chapter_list_labels_empty_name` | 空名称 | 显示"未命名章节" |
| `test_timeline_view_row_count` | 时间轴行数 | 与 timeline blocks 数量一致 |
| `test_timeline_view_selected_block_id` | 选中块 ID | 返回正确 block_id |
| `test_timeline_view_current_selection_emits_signal` | 选择信号 | 选择变化发出信号 |
| `test_timeline_view_selection_only_change_updates_current` | 仅选择变化 | 不触发完整刷新 |
| `test_timeline_view_exposes_accessible_row_text` | 无障碍文本 | 每行有 accessible 文本 |
| `test_timeline_view_multi_select` | 多选 | 支持 Ctrl/Shift 多选 |
| `test_timeline_view_trimmed_block_remains_selectable_for_restore` | 裁剪块可选 | is_trimmed 块仍可选中（用于恢复） |
| `test_timeline_view_scroll_to_block` | 滚动到块 | scrollTo 正确 |
| `test_detail_panel_show_block` | 详情面板 | 显示块详情 |
| `test_detail_panel_no_frame` | 无帧 | 不崩溃 |
| `test_detail_panel_shows_operation_evidence_and_fields` | 操作证据 | 显示截图 + 操作字段 + UIA/OCR/STT |
| `test_block_delegate_state_overlay` | 状态叠加（D015） | marked_key=加粗边框 / anomaly=红色 / automatable=蓝色虚线 / trimmed=半透明+删除线 |
| `test_editor_window_add_annotation` | 添加 annotation | annotation 存入 store，时间轴刷新 |
| `test_editor_window_initial_selection_populates_detail` | 初始选择 | 默认选中首块，详情填充 |
| `test_editor_window_annotation_refreshes_chapters_and_detail` | annotation 刷新 | 章节+详情同步更新 |
| `test_editor_window_trim_restore_actions_follow_selected_state` | 裁剪/恢复跟随 | 选中 trimmed 块时显示恢复按钮 |
| `test_editor_window_repeated_inserts_use_unique_block_ids` | 重复插入 | 每次插入生成唯一 ID |
| `test_editor_window_double_click_chapter_renames_it` | 双击重命名 | 章节进入编辑模式 |
| `test_editor_window_inserts_reference_image` | 插入参考图片 | 图片块插入正确 |
| `test_editor_window_inserts_file_text` | 插入文件文本 | 文本块插入正确 |
| `test_editor_window_handles_missing_text_file` | 缺失文本文件 | 不崩溃，提示错误 |
| `test_editor_window_undo_redo` | 撤销/重做 | undo/redo 栈正确 |
| `test_editor_window_finalize` | 标记就绪 | finalized=True，状态栏更新 |

### 6.6 端到端流程测试

**测试文件**：`tests/test_recorder_editor_e2e.py`

| 测试类 | 测试函数 | 验证点 |
|--------|----------|--------|
| `TestL3FullFlow` | `test_merge_returns_original_when_no_annotations` | 无 annotation merge 返回原始 |
| | `test_annotation_flow` | annotation 流程完整 |
| | `test_undo_redo_flow` | undo/redo 流程 |
| | `test_finalize_flow` | finalize 流程 |
| | `test_trim_filters_block_in_merge` | 裁剪在 merge 中过滤 |
| | `test_insert_note_in_merge` | 插入注释在 merge 中出现 |
| | `test_stats_diagnostic` | 统计诊断数据正确 |
| `TestL3DecisionCoverage` | `test_d044_timeline_readonly` | D044: timeline.json 只读 |
| | `test_d045_flat_action_enum` | D045: 扁平 action 枚举 |
| | `test_d046_13_actions_defined` | D046: 13 个 action |
| | `test_d050_undo_redo` | D050: 完整 undo/redo |
| | `test_d053_finalize` | D053: finalize 标记 |

---

## 七、L4 消费层测试

### 7.1 发现录制包测试

**验证点**：

| 函数 | 验证点 | 预期结果 |
|------|--------|----------|
| `list_recordings()` | 列出所有录制包 | 返回 RecordingSummary 列表 |
| | 按创建时间倒序 | 最新录制包在前 |
| | 空目录 | 返回空列表 |
| `get_recording_summary(path)` | 摘要字段 | 含 recording_id/created_at/duration/finalized/block_count/chapter_count/has_transcript |

### 7.2 读取录制包测试

**测试文件**：`tests/test_recorder_consumer_e2e.py::TestConsumerE2EFlow`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_list_recordings_includes_package` | list 包含包 | 返回列表含目标录制包 |
| `test_get_recording_summary_fields` | 摘要字段 | 所有字段正确填充 |
| `test_get_merged_view_structure` | merged_view 结构 | 含 blocks/chapters/finalized |
| `test_get_chapters_returns_chapter_blocks` | 章节列表 | 返回 type=chapter 的块 |
| `test_get_block_detail_resolves_supplement_paths` | 块详情 | supplements 路径解析为绝对路径 |
| `test_get_block_detail_returns_none_for_unknown_block` | 未知块 | 返回 None |

### 7.3 VL 候选选择测试

**测试文件**：`tests/test_recorder_consumer.py` + `test_recorder_consumer_e2e.py`

| 验证点 | 预期结果 |
|--------|----------|
| `select_vl_candidates(focus="all")` | 所有 screenshot 块 + marked_key/anomaly 关联帧 |
| `focus="key"` | 仅 marked_key 块关联帧 |
| `focus="anomaly"` | 仅 marked_anomaly 块关联帧 |
| `focus="automatable"` | 仅 marked_automatable 块关联帧 |
| `focus="trimmed"` | 仅 is_trimmed 块关联帧（通常为空） |
| 去重 | 同一帧不重复出现 |
| 路径解析 | frame_path 解析为绝对路径 |
| 排序 | 按时间戳升序 |
| 空视图 | 不崩溃，返回空列表 |
| 非法 focus | 抛出 ValueError |

### 7.4 VL 问题构建测试

| 块类型 | 预期 question |
|--------|---------------|
| mouse_click | "这个界面的可点击元素在哪里？点击位置 (x, y) 是什么控件？..." |
| keyboard_input | "这个界面的输入框在哪里？当前输入了什么内容？" |
| focus_change | "这个窗口/界面的标题是什么？主要功能区域有哪些？" |
| chapter | "这个章节的界面主要在做什么操作？" |
| screenshot | "这个截图展示了什么内容？主要界面元素有哪些？" |
| stt_transcript | 空字符串（文本已有，无需 VL） |
| 带 context | question 后追加 context 文本 |

### 7.5 消费日志测试

**测试文件**：`tests/test_recorder_consumer_e2e.py::TestConsumptionLogFormat`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_consumption_log_jsonl_format` | JSONL 格式 | 每行一个 JSON，含 action/details/timestamp |
| `test_consumption_log_does_not_store_vl_response` | 不存 VL 返回（D057） | 只记录调用元信息，不含 VL 响应内容 |
| `test_read_consumption_log_empty_for_missing_file` | 缺失文件 | 返回空列表 |
| `test_log_and_read_consumption_roundtrip` | 读写往返 | 写入后读取内容一致 |

### 7.6 多轮 VL 协议测试

**测试文件**：`tests/test_recorder_consumer_e2e.py::TestMultiRoundVLProtocol`

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_round1_build_global_awareness` | Round 1 | get_merged_view 建立全局认知 |
| `test_round2_select_and_question` | Round 2 | select_vl_candidates + build_vl_question 选帧+带问题 |
| `test_vl_call_count_within_recommended_limit` | 调用次数 | VL 调用 ≤ 30 次（D054 推荐上限） |

### 7.7 manifest 测试

| 测试函数 | 验证点 | 预期结果 |
|----------|--------|----------|
| `test_regenerate_manifest_writes_format_version` | manifest 写入 | manifest.json 含 format_version |
| `read_manifest(path)` | 读取 manifest | 返回 dict 或 None（文件不存在时） |

---

## 八、GUI 外观验收测试

> 本节为人工验收清单，逐项确认 GUI 外观符合设计规范。

### 8.1 小模式悬浮条（FloatingBar）

| 序号 | 验收项 | 标准 | 通过 |
|------|--------|------|------|
| 1 | 窗口高度 | 44px | □ |
| 2 | 最小宽度 | 520px | □ |
| 3 | 窗口置顶 | WindowStaysOnTopHint | □ |
| 4 | 无边框 | FramelessWindowHint | □ |
| 5 | 工具窗口 | Tool 属性（不显示在任务栏） | □ |
| 6 | 透明度 | 0.90 | □ |
| 7 | 背景色 | `#272a29`（暗色） | □ |
| 8 | IDLE 状态灯 | 灰色圆点 | □ |
| 9 | RECORDING 状态灯 | 红色脉动圆点 | □ |
| 10 | 计时器格式 | `HH:MM:SS` | □ |
| 11 | 事件计数 | `events: N` 实时更新 | □ |
| 12 | 帧计数 | `frames: N` 实时更新 | □ |
| 13 | 麦克风按钮 | `🎤 麦克风:开` / `🎤 麦克风:关` | □ |
| 14 | IDLE 按钮 | `● 开始录制` | □ |
| 15 | RECORDING 按钮 | `⏹ 停止录制` | □ |
| 16 | PAUSED 三按钮 | `▶ 继续录制` / `💾 保存结束` / `🗑 丢弃重录` | □ |
| 17 | 丢弃确认 | 弹出 QMessageBox.warning | □ |
| 18 | 拖拽 | 鼠标按空白区域可拖动 | □ |
| 19 | 初始位置 | 屏幕顶部中央 | □ |

### 8.2 大模式监控面板（MonitorWindow）

| 序号 | 验收项 | 标准 | 通过 |
|------|--------|------|------|
| 1 | 最小尺寸 | 900x640 | □ |
| 2 | 默认尺寸 | 1100x760 | □ |
| 3 | 窗口属性 | 独立顶层窗口（不置顶） | □ |
| 4 | 暗色主题 | 同小模式 `#272a29` | □ |
| 5 | 顶部状态栏 | 状态灯 + 计时器 + 包名 | □ |
| 6 | 上半区左面板 | 截图预览（`_FramePreviewPanel`） | □ |
| 7 | 上半区右面板 | 事件流（`_EventStreamPanel`） | □ |
| 8 | 下半区面板1 | 录制参数+窗口信息（`_WindowInfoPanel`） | □ |
| 9 | 下半区面板2 | 音频波形（`_AudioWaveformPanel`） | □ |
| 10 | 下半区面板3 | 统计（`_StatsPanel`） | □ |
| 11 | 底部按钮栏 | 麦克风 + 开始/停止 + 三按钮 | □ |
| 12 | 截图预览刷新 | 500ms 轮询更新 | □ |
| 13 | 事件流刷新 | 500ms 增量读取 | □ |
| 14 | 音频波形刷新 | 1000ms 轮询 | □ |
| 15 | 波形颜色 | 青=保留 / 灰=删除 / 红虚线=阈值 | □ |
| 16 | 统计面板 | 事件数/帧数/时长/音频大小 | □ |

### 8.3 配置对话框（ConfigDialog）

| 序号 | 验收项 | 标准 | 通过 |
|------|--------|------|------|
| 1 | 窗口标题 | "新建录制" | □ |
| 2 | 模态 | QDialog 模态 | □ |
| 3 | 录制范围组 | 全屏 / 某屏（QComboBox+刷新）/ 某窗口（QComboBox+刷新） | □ |
| 4 | 录制模式组 | 详细 / 粗略 / 自定义 | □ |
| 5 | 自定义参数 | 截图间隔 + Burst 时刻 + 最大时长 | □ |
| 6 | 麦克风选择 | QCheckBox + QComboBox | □ |
| 7 | 剪贴板 | 默认关 | □ |
| 8 | UIA 树 | disabled（L0 stub） | □ |
| 9 | pHash 滑块 | 0-16，默认 5 | □ |
| 10 | 按钮 | 取消 / 开始录制 | □ |
| 11 | 无显示器校验 | 选 monitor 但无显示器时提示 | □ |
| 12 | 无窗口校验 | 选 window 但无窗口时提示 | □ |
| 13 | 无麦克风确认 | 确认是否继续 | □ |

### 8.4 L3 编辑器（EditorWindow）

| 序号 | 验收项 | 标准 | 通过 |
|------|--------|------|------|
| 1 | 左侧章节列表 | ~190px 宽，点击跳转，双击重命名 | □ |
| 2 | 中间时间轴 | 4 列：时间 / 事件·窗口 / 证据 / 内容 | □ |
| 3 | 稳定列头 | 列头不随滚动消失 | □ |
| 4 | 下方详情 | 截图 + 操作字段 + UIA/OCR/STT | □ |
| 5 | 顶部工具栏 | 12 项图标按钮 | □ |
| 6 | 底部状态栏 | 块数/总时长/编辑数/裁剪数/就绪状态 | □ |
| 7 | marked_key 显示 | 左侧 4px 加粗边框 | □ |
| 8 | marked_anomaly 显示 | 红色边框 `#EF4444` | □ |
| 9 | marked_automatable 显示 | 蓝色虚线边框 `#3B82F6` | □ |
| 10 | is_trimmed 显示 | 半透明 + 删除线 | □ |
| 11 | 状态叠加 | 多状态可叠加不互斥（D015） | □ |
| 12 | 裁剪块可选 | is_trimmed 块仍可选中（用于恢复） | □ |

### 8.5 编辑器工具栏 12 项

| 序号 | 图标 | 功能 | 快捷键 | 通过 |
|------|------|------|--------|------|
| 1 | 🗑️ | 裁剪块 | `Delete` | □ |
| 2 | ↩️ | 恢复块 | `Ctrl+Y` | □ |
| 3 | 🔗 | 合并块 | `Ctrl+Shift+M` | □ |
| 4 | ✂️ | 拆分块 | `Ctrl+Shift+S` | □ |
| 5 | ⭐ | 标关键 | `Ctrl+H` | □ |
| 6 | ⚠️ | 标异常 | `Ctrl+E` | □ |
| 7 | 🤖 | 标可自动化 | `Ctrl+A` | □ |
| 8 | ➕ | 插入内容 | `Ctrl+I` | □ |
| 9 | ↩️ | 撤销 | `Ctrl+Z` | □ |
| 10 | ↪️ | 重做 | `Ctrl+Shift+Z` | □ |
| 11 | 🎵 | 音频调参 | `Ctrl+Shift+A` | □ |
| 12 | ✓ | 标记就绪 | `Ctrl+Return` | □ |

### 8.6 音频调参面板（AudioPanel）

| 序号 | 验收项 | 标准 | 通过 |
|------|--------|------|------|
| 1 | 波形显示 | QPainter 绘制 RMS dBFS 柱状图 | □ |
| 2 | 波形颜色 | 青=保留 / 灰=删除 / 红虚线=阈值 | □ |
| 3 | 静音阈值滑块 | -60 到 -20 dB | □ |
| 4 | 最小静音时长滑块 | 0.5 到 5.0s | □ |
| 5 | 切片预览 | 实时显示保留段/删除段/占比 | □ |
| 6 | 试音按钮 | 点击播放预览音频 | □ |
| 7 | 应用配置按钮 | trim_audio → STT 重跑 → 重建 timeline | □ |
| 8 | 进度条 | STT 重跑时显示进度 | □ |

---

## 九、跨层端到端测试

### 9.1 完整录制→处理→编辑→消费流程

**测试目标**：验证 L0→L1→L2→L3→L4 全链路数据流正确。

**前置条件**：
- 管理员权限
- 麦克风可用
- 显示器正常

**步骤**：

| 步骤 | 操作 | 验证 | 预期结果 |
|------|------|------|----------|
| 1 | 启动录制器（小模式） | `python -m workspace.recorder.tools.main --autostart --max-duration 10` | 悬浮条出现，开始录制 |
| 2 | 操作 10 秒（移动鼠标+点击+按键） | 观察悬浮条计数器 | events/frames 计数递增 |
| 3 | 等待自动停止 | 10 秒后 | 录制自动停止，meta.json status=saved |
| 4 | 验证录制包 | `ls workspace/recorder/recordings/rec_*` | 目录存在，含 meta.json/events.jsonl/frames/audio/ |
| 5 | 运行 L1 处理 | L3 编辑器启动时自动跑 P4/P5 | blocks.json + keyframes.json 生成 |
| 6 | 运行 L2 时间轴 | L3 编辑器启动时自动 build_timeline | timeline.json + annotations.json 生成 |
| 7 | 启动 L3 编辑器 | `python -m workspace.recorder.tools.editor --package-path <path>` | 编辑器窗口打开，显示时间轴 |
| 8 | 标注操作 | 标关键/裁剪/插入注释 | annotations.json 更新 |
| 9 | 标记就绪 | 点 ✓ 按钮 | finalized=True |
| 10 | L4 消费 | `from workspace.recorder.consumer import get_merged_view; get_merged_view(path)` | 返回合并视图，含标注状态 |

### 9.2 真实录制包验证

**测试目标**：使用 `workspace/recorder/recordings/` 下的真实录制包验证各层。

```bash
# 列出真实录制包
uv run python -c "
from workspace.recorder.consumer import list_recordings
for r in list_recordings():
    print(f'{r.recording_id}  {r.duration_seconds:.1f}s  finalized={r.finalized}')
"
```

**验证项**：

| 层 | 验证 | 命令 |
|----|------|------|
| L1 | P4/P5 产出 | `uv run python -c "from lib.recorder.processor.pipeline import process_recording_package; r = process_recording_package('<path>'); print(r)"` |
| L2 | timeline 构建 | `uv run python -m workspace.recorder.tools.timeline_cli --package-path <path>` |
| L3 | merge 视图 | `uv run python -c "from lib.recorder.editor.merge import merge_timeline_annotations; print(merge_timeline_annotations('<path>'))"` |
| L4 | 消费 API | `uv run python -c "from workspace.recorder.consumer import get_merged_view; print(get_merged_view('<path>'))"` |

### 9.3 resume 时间轴连续性验证

**测试目标**：验证 pause→resume 后时间轴连续（D033）。

| 步骤 | 操作 | 验证 |
|------|------|------|
| 1 | start() | segments=[{start:0, end:None}] |
| 2 | 录制 3 秒 | events 时间戳 0-3s |
| 3 | pause() | segments=[{start:0, end:3}] |
| 4 | 等待 2 秒 | - |
| 5 | resume() | segments=[{start:0,end:3},{start:5,end:None}] |
| 6 | 录制 3 秒 | events 时间戳 5-8s（连续不跳跃） |
| 7 | save() | effective_duration=6.0（3+3），不含暂停 2s |

### 9.4 传感器失败容错验证

**测试目标**：单个传感器失败不阻塞其它传感器。

| 步骤 | 操作 | 预期结果 |
|------|------|----------|
| 1 | 注入一个会抛异常的 MockSensor | start() 时抛异常 |
| 2 | 同时注入正常传感器 | 正常传感器不受影响 |
| 3 | start() | 不崩溃，异常传感器跳过 |
| 4 | stop() | 正常传感器正确停止 |
| 5 | save() | meta.json 含正常传感器，不含异常传感器 |

---

## 十、工具面板集成测试

> 验证 recorder 工具在 client 工具面板中的入口和启动。

### 10.1 工具清单验证

**验证 `tools_manifest.json` 含 recorder 分类和 4 个工具**：

```bash
uv run python -c "
import json
m = json.loads(open('tools_manifest.json', encoding='utf-8-sig').read())
cats = [c for c in m['categories'] if c['id'] == 'recorder']
tools = [t for t in m['tools'] if t['category'] == 'recorder']
print(f'分类: {cats}')
print(f'工具数: {len(tools)}')
for t in tools:
    print(f'  - {t[\"id\"]}: {t[\"name\"]}')
"
```

**预期输出**：

```
分类: [{'id': 'recorder', 'name': '操作录制', 'icon': '🎥'}]
工具数: 4
  - recorder_small: 录制器（小模式）
  - recorder_large: 录制器（大模式）
  - recorder_editor: 录制包编辑器
  - recorder_timeline: 时间轴构建
```

### 10.2 工具面板显示验证

| 序号 | 验收项 | 通过 |
|------|--------|------|
| 1 | 左侧分类列表出现 `🎥 操作录制` | □ |
| 2 | 点击分类后右侧出现 4 个 Tab | □ |
| 3 | 录制器（小模式）Tab 有 6 个选项 | □ |
| 4 | 录制器（大模式）Tab 有 5 个选项 | □ |
| 5 | 录制包编辑器 Tab 有 --package-path 选项 | □ |
| 6 | 时间轴构建 Tab 有 3 个选项 | □ |
| 7 | 所有工具 `user_facing=true`（默认显示） | □ |

### 10.3 工具启动验证

| 序号 | 操作 | 预期结果 | 通过 |
|------|------|----------|------|
| 1 | 点"录制器（小模式）"→ 运行 | 悬浮条出现 | □ |
| 2 | 勾选 `--mode large`（大模式）→ 运行 | 监控面板出现 | □ |
| 3 | 勾选 `--autostart` → 运行 | 跳过配置对话框直接录制 | □ |
| 4 | 勾选 `--no-audio` → 运行 | 不录麦克风 | □ |
| 5 | 编辑器填 --package-path → 运行 | 编辑器打开指定录制包 | □ |
| 6 | 时间轴填 --package-path → 运行 | 输出预览到 stdout | □ |
| 7 | 运行中点"停止" | taskkill 终止子进程树 | □ |

### 10.4 --package-path 选项验证

**验证编辑器和时间轴 CLI 接受 `--package-path`**：

```bash
# 编辑器 --package-path
.venv\Scripts\python.exe -m workspace.recorder.tools.editor --package-path workspace/recorder/recordings/rec_20260723_162924

# 编辑器位置参数（向后兼容）
.venv\Scripts\python.exe -m workspace.recorder.tools.editor workspace/recorder/recordings/rec_20260723_162924

# 时间轴 --package-path
.venv\Scripts\python.exe -m workspace.recorder.tools.timeline_cli --package-path workspace/recorder/recordings/rec_20260723_162924

# 时间轴位置参数（向后兼容）
.venv\Scripts\python.exe -m workspace.recorder.tools.timeline_cli workspace/recorder/recordings/rec_20260723_162924

# 无参数（应显示帮助）
.venv\Scripts\python.exe -m workspace.recorder.tools.editor
```

---

## 十一、测试文件索引

### L0 采集层（11 个文件）

| 文件 | 测试类数 | 覆盖范围 |
|------|----------|----------|
| `tests/test_recorder_controller.py` | 11 | 状态机/包/事件/段/meta/hook |
| `tests/test_recorder_e2e.py` | 3 | 完整流程/时间轴延续/非法转换 |
| `tests/test_recorder_smoke.py` | 5 | 冒烟/配置加载/CLI/参数合并 |
| `tests/test_recorder_gui.py` | 7 | Config/Dialog/Hotkey/FloatingBar/RecorderApp |
| `tests/test_recorder_monitor.py` | 17 | 大模式全部面板/信号/轮询/三按钮 |
| `tests/test_recorder_audio.py` | 2 | 音频传感器/集成 |
| `tests/test_recorder_keyboard.py` | 多 | 键盘传感器 |
| `tests/test_recorder_mouse.py` | 7 | 鼠标全场景 |
| `tests/test_recorder_screen.py` | 多 | 截图传感器 |
| `tests/test_recorder_window.py` | 多 | 窗口焦点传感器 |
| `tests/test_recorder_audio_trimmer.py` | 6 | RMS/裁剪/映射/保护/可读/边界 |

### L1 处理层（4 个文件）

| 文件 | 测试类数 | 覆盖范围 |
|------|----------|----------|
| `tests/test_recorder_processor_e2e.py` | 6 | P4P5/P1/时间戳/P2接口/P3接口/全流水线 |
| `tests/test_recorder_processor_p1.py` | 10 | STT 全流程 |
| `tests/test_recorder_processor_p4.py` | 5 | 事件聚合 |
| `tests/test_recorder_processor_p5.py` | 4 | 关键帧抽取 |

### L2 时间轴层（3 个文件）

| 文件 | 测试类数 | 覆盖范围 |
|------|----------|----------|
| `tests/test_recorder_timeline_builder.py` | 12 | 构建/合并/排序/格式/降级/章节 |
| `tests/test_recorder_timeline_annotator.py` | 3 | 初始化/预览/CLI |
| `tests/test_recorder_timeline_e2e.py` | 4 | V1/V2/章节切分/跨包对比 |

### L3 编辑层（6 个文件）

| 文件 | 测试类数 | 覆盖范围 |
|------|----------|----------|
| `tests/test_recorder_editor_apply.py` | 20 | 13 个 Action + 边界 |
| `tests/test_recorder_editor_store.py` | 13 | undo/redo/finalize/保存加载/统计 |
| `tests/test_recorder_editor_merge.py` | 16 | merge 全场景 + 真实包 |
| `tests/test_recorder_editor_audio.py` | 11 | 切片预览/波形/AudioPanel/STTRunner |
| `tests/test_recorder_editor_e2e.py` | 2 | 完整流程/决策覆盖 |
| `tests/test_recorder_editor_gui.py` | 26 | 章节列表/时间轴/详情/状态叠加/编辑器窗口 |

### L4 消费层（2 个文件）

| 文件 | 测试类数 | 覆盖范围 |
|------|----------|----------|
| `tests/test_recorder_consumer.py` | 多 | VL候选/问题构建/日志/各focus模式 |
| `tests/test_recorder_consumer_e2e.py` | 3 | 端到端/多轮VL/日志格式 |

---

## 附录：设计决策覆盖矩阵

| 决策 | 说明 | 测试覆盖 |
|------|------|----------|
| D015 | 块状态叠加显示 | `test_block_delegate_state_overlay` / `test_mark_multiple_states_coexist` |
| D021 | 密码框保护 | `test_recorder_keyboard.py` |
| D022 | 物理按键序列 | `test_recorder_keyboard.py` |
| D023 | click 后多帧采样 | `TestOnClickCallback` |
| D024 | PrintWindow 被遮挡窗口 | `test_recorder_screen.py` |
| D026 | 5 状态有限状态机 | `TestRecordingStateMachine` |
| D033 | resume 时间轴延续 | `TestTimelineContinuityE2E` |
| D034 | mic.wav 完整保留 | `test_save_from_recording_state_preserves_audio` |
| D035/D036 | pHash 去重 | `test_recorder_screen.py` / `TestTimerFrameGlobalDedup` |
| D038 | STT 渐进式重试 | `TestTranscribeWithFallback` |
| D042 | 章节切分 | `TestChapterGeneration` / `TestV2RecordingChapterSplitting` |
| D043 | annotations 只初始化 | `TestInitAnnotations` |
| D044 | timeline 只读 | `test_d044_timeline_readonly` |
| D045 | 扁平 action 枚举 | `test_d045_flat_action_enum` |
| D046 | 13 个 action | `test_d046_13_actions_defined` |
| D050 | undo/redo 栈 | `test_d050_undo_redo` / `TestAnnotationStore` |
| D052 | save 后自动启动编辑器 | `TestRecorderApp` |
| D053 | 标记就绪 | `test_d053_finalize` / `test_editor_window_finalize` |
| D057 | 不存 VL 返回内容 | `test_consumption_log_does_not_store_vl_response` |
