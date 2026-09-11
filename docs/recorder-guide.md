# 操作录制器用户指南（L0 采集层 + L1 处理层 + L2 时间轴层 + L3 编辑层 + L4 消费层）

> 录制键鼠操作 + 屏幕截图 + 麦克风音频 + 窗口焦点，生成可回放的操作录制包，供 L1 处理层（STT/VL/事件聚合）和 L2-L4（时间轴/编辑/消费）使用。
>
> 测试说明见 [recorder-test-guide.md](recorder-test-guide.md)（L0-L4 五层端到端测试 + GUI 外观验收 + 跨层集成测试）。

## 一、快速开始

### 1. 启动录制器

```bash
# 默认配置（小模式 + 全屏 + 详细模式）
python -m workspace.recorder.tools.main

# 大模式（双屏监控场景）
python -m workspace.recorder.tools.main --mode large

# 跳过配置对话框，直接开始录制
python -m workspace.recorder.tools.main --range fullscreen --detail-level detailed --autostart
```

### 2. 配置对话框

启动时（未指定 `--autostart`）会弹出配置对话框：

- **录制范围**：全屏 / 某个屏幕 / 某个窗口
- **录制模式**：详细（500ms 截图）/ 粗略（2s 截图）
- **音频**：是否录麦克风
- **自定义参数**：截图间隔 / 最大时长

### 3. 开始/停止录制

- **GUI 模式**：点击悬浮条上的"开始录制"按钮，或按全局热键 `Ctrl+Alt+R`
- **无 GUI 模式**（冒烟测试）：`--no-gui --autostart --max-duration 180`（3 分钟后自动停止）
- **自动停止**：到 `max_duration_seconds`（默认 1800 秒 = 30 分钟）自动停止

### 4. 查看录制包

录制包默认保存在 `workspace/recorder/recordings/rec_{YYYYMMDD}_{HHMMSS}/`：

```
rec_20260723_143000/
├── meta.json           # 录制元信息（开始/结束时间/事件数/帧数/版本）
├── events.jsonl        # 全部事件流（每行一个 JSON 事件）
├── focus.jsonl         # 焦点切换事件子集（便于快速定位章节）
├── frames/             # 截图帧（PNG 无损）
│   ├── frame_0000500_000000.png
│   ├── frame_0001000_000001.png
│   └── ...
└── audio/
    └── mic.wav         # 麦克风录音（int16 PCM, 16kHz, mono）
```

## 组件化迁移说明

录制系统已于 2026-07-27 完成组件化迁移，符合项目插拔规范：
- **L4 消费层** + **L3 编辑器 GUI** + **L0 GUI 入口** 迁移至 `workspace/recorder/`
- **L0-L3 核心库**（sensors/processor/timeline/editor 核心）保留在 `lib/recorder/`
- 通过 `workspace/recorder/manifest.toml` 声明插入点（agent_guide/skill）
- 主代码库 `server/agent_guide.py` 零硬编码引用，删除 `workspace/recorder/` 后路由自动注销

## 二、CLI 参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--mode` | `small` \| `large` | 从 config.toml | GUI 模式：small=悬浮条 / large=监控面板 |
| `--range` | `fullscreen` \| `monitor` \| `window` | 从 config.toml | 录制范围 |
| `--monitor-index` | int | 0 | 显示器索引（`--range=monitor` 时用，0=主屏） |
| `--window-hwnd` | int | 0 | 目标窗口句柄（`--range=window` 时用，10 进制） |
| `--detail-level` | `detailed` \| `coarse` | 从 config.toml | 录制模式：detailed=500ms 截图 / coarse=2s 截图 |
| `--max-duration` | int | 1800 | 最大录制时长（秒），到时自动停止 |
| `--no-audio` | flag | false | 禁用麦克风录制 |
| `--config` | str | config.toml | 配置文件路径 |
| `--base-dir` | str | workspace/recorder/recordings | 录制包根目录 |
| `--autostart` | flag | false | 跳过 ConfigDialog，直接用 CLI 参数开始录制 |
| `--no-gui` | flag | false | 无 GUI 模式（仅 CLI + 传感器，用于冒烟测试） |

**CLI 参数优先级高于 config.toml**：CLI 传了某参数就用 CLI 的，没传就用 config.toml 的，config.toml 也没有就用内置默认值。

## 三、config.toml 配置项

配置文件位于项目根 `config.toml`，`[recording]` 段：

```toml
[recording]
default_mode = "small"                      # GUI 启动模式
default_range = "fullscreen"                # 录制范围
default_detail_level = "detailed"           # 录制模式
default_monitor_index = 0                   # 显示器索引
default_window_hwnd = 0                     # 窗口句柄
max_duration_seconds = 1800                 # 最大录制时长（秒）
audio_enabled = true                        # 是否录麦克风
base_dir = "workspace/recorder/recordings"           # 录制包根目录

[recording.capture]
interval_detailed = 0.5                     # 详细模式截图间隔
interval_coarse = 2.0                       # 粗略模式截图间隔
burst_intervals_detailed = [0.05, 0.10, 0.15]   # 详细模式 click 后多帧采样时刻
burst_intervals_coarse = [0.10, 0.20, 0.30]     # 粗略模式 click 后多帧采样时刻

[recording.audio]
sample_rate = 16000                         # 采样率（Hz）
channels = 1                                # 声道数

[recording.stt]                             # 预留：L1 处理层使用，L0 不消费
enabled = false
model = ""
```

## 四、热键清单

| 热键 | 动作 | 说明 |
|------|------|------|
| `Ctrl+Alt+R` | 开始/停止录制 | 全局热键，不依赖 Qt 事件循环，可在任何应用前台时触发 |

## 五、GUI 模式说明

### 小模式（FloatingBar 悬浮条）

- 单屏场景，最小化干扰
- 高度 ~40px，半透明，始终置顶
- 显示：录制状态（●REC / ●IDLE）+ 计时器 + 事件数 + 帧数 + 麦克风开关 + 开始/停止按钮
- 可拖拽到任意位置（按空白区域拖动）

### 大模式（MonitorWindow 监控面板）

- 双屏场景，一个屏操作另一个屏监控
- 实时屏幕预览（最新帧 + 历史缩略图，最多 20 张）
- 实时事件流（最近 100 条，颜色区分：鼠标橙 / 键盘蓝 / 焦点绿 / 截图青 / 热键紫 / 密码灰）
- 录制参数面板（时长 / 事件数 / 帧数 / 音频时长）
- 当前窗口信息（标题 / 句柄 / PID）
- 音频波形（实时绘制，QPainter 自定义绘制柱状波形）
- 底部按钮栏（停止 / 麦克风开关）

### 大/小模式切换

- 通过 `--mode` 参数指定启动模式
- 运行时切换：未来版本支持热键切换（当前版本需重启）
- 切换时共享同一个 RecordingController，不中断录制

## 六、录制范围说明

### 全屏（fullscreen）

- 截取主显示器全屏画面
- 适用场景：常规操作录制

### 某个屏幕（monitor）

- 通过 `--monitor-index` 指定显示器（0=主屏，1=副屏）
- 用 mss 库截取指定显示器
- 适用场景：双屏环境下截取其中一个屏

### 某个窗口（window）

- 通过 `--window-hwnd` 指定窗口句柄
- 用 PrintWindow + PW_RENDERFULLCONTENT 截取（能截到被遮挡的窗口内容）
- 窗口最小化时自动跳过截图（无内容可截）
- 适用场景：录制特定应用的操作

## 七、录制模式说明

### 详细模式（detailed）

- 截图间隔 500ms（2 fps）
- click 后多帧采样（50ms / 100ms / 150ms 三帧，帧哈希自动去重）
- 适用场景：需要精确回放的操作（如 bug 复现）

### 粗略模式（coarse）

- 截图间隔 2s（0.5 fps）
- click 后多帧采样（100ms / 200ms / 300ms 三帧）
- 适用场景：日常操作记录（节省存储空间）

## 八、事件类型说明

`events.jsonl` 每行一个 JSON 事件，字段：`timestamp`（相对开始的秒数）/ `abs_timestamp`（绝对时间戳）/ `kind` / `payload`。

| kind | 说明 | payload 关键字段 |
|------|------|------------------|
| `keyboard_input` | 键盘输入块（300ms 时间窗口聚合） | `text`（UIA 差值）/ `physical_keys`（物理按键序列）/ `detection_method` |
| `mouse_click` | 鼠标点击 | `x` / `y` / `button` / `clicks` |
| `mouse_drag` | 鼠标拖拽 | `start` / `end` / `track`（轨迹点列表） |
| `mouse_scroll` | 鼠标滚轮 | `dx` / `dy` / `x` / `y` |
| `focus_change` | 窗口焦点切换（同时写 focus.jsonl） | `title` / `hwnd` / `pid` / `process_name` |
| `screen_frame` | 截图帧 | `frame_path` / `frame_seq` / `mode` |
| `hotkey` | 热键（Ctrl+C/V/X） | `combo`（copy/paste/cut） |
| `ime_switch` | 输入法切换（Shift+Space / Ctrl+Space） | `key` |
| `password_masked` | 密码框输入（不记录内容） | `physical_keys=[]` |

## 九、录制状态机

L0 v2 重构（D026-D033）后，录制器从"开始/停止两态"升级为 **5 状态有限状态机**，支持暂停/恢复/保存/丢弃四种操作，让用户能灵活控制录制流程。

### 状态定义

| 状态 | 说明 | 可见指示 |
|------|------|---------|
| `IDLE` | 未录制 | 悬浮条 ●IDLE 灰色 |
| `RECORDING` | 录制中 | 悬浮条 ●REC 红色 + 计时器递增 |
| `PAUSED` | 已暂停（停止后） | 三按钮显示（继续录制/保存结束/丢弃重录） |
| `SAVED` | 已保存（不可逆） | 窗口关闭，后台裁剪进行中 |

### 状态转换图

```
                 start()
       IDLE ───────────────► RECORDING
         ▲                      │
         │                      │ pause() / stop()
         │                      ▼
         │                    PAUSED ◄───────┐
         │                      │            │
         │     discard()        │            │ resume()
         └──────────────────────┤            │
                                │            │
                                │ save()     │
                                ▼            │
                              SAVED          │
                                │            │
                                └──── X ─────┘
                                  （SAVED 后任何操作
                                   都抛 RuntimeError）
```

### 操作说明

| 操作 | 起始状态 | 目标状态 | 行为 |
|------|---------|---------|------|
| `start()` | IDLE | RECORDING | 创建录制包 + 启动所有传感器 + 重置 TimestampService |
| `pause()` / `stop()` | RECORDING | PAUSED | 停所有传感器 + 记录 segment 的 end_offset + 保留录制包 |
| `resume()` | PAUSED | RECORDING | 重启传感器 + 记录新 segment 的 start_offset + 时间轴延续（不归零） |
| `save()` | PAUSED/RECORDING | SAVED | 标记不可逆 + 启动后台裁剪线程（Ticket 11）+ 立即返回 |
| `discard()` | PAUSED/RECORDING | IDLE | 删除录制包目录（shutil.rmtree） |

### 非法转换拒绝

- **SAVED 后**任何操作（pause/resume/save/discard）都抛 `RuntimeError`，SAVED 是不可逆终态
- **IDLE 后** resume/pause/save 抛 `RuntimeError`，必须先 `start()` 进入 RECORDING

### 时间轴延续（D033）

`resume()` 时**不重新构造 TimestampService**，保持 base_abs 与 monotonic 基准不变，事件时间戳持续累计。这意味着：
- 5 分钟录制 + 2 分钟暂停 + 3 分钟录制 = `duration_seconds=600s`（含暂停段）
- 但 `effective_duration=480s`（剔除暂停段后的净录制时长）
- segments 列表 `[{start_offset:0, end_offset:300}, {start_offset:420, end_offset:720}]` 记录各段区间

## 十、三按钮交互

L0 v2 重构（D030）后，**停止录制后底部弹三按钮**让用户三选一，不再强制保存也不允许直接继续录制。

### 三按钮说明

| 按钮 | 颜色 | 调用方法 | 行为 |
|------|------|---------|------|
| **继续录制** | 蓝色 | `controller.resume()` | 重启传感器 + 回到 RECORDING + 计时器继续递增 + 时间轴延续 |
| **保存结束** | 绿色 | `controller.save()` | 标记 SAVED + 关闭录制窗口 + 后台启动音频裁剪（不弹提示） |
| **丢弃重录** | 红色 | `controller.discard()`（弹确认对话框） | 删除录制包目录 + 回 IDLE + 可重新 start 新录制 |

### 按钮显示规则

- **RECORDING 状态**：只显示【停止】按钮，三按钮隐藏
- **PAUSED 状态**：隐藏【停止】按钮，显示三按钮
- 用户点击【停止】→ 进入 PAUSED → 三按钮显示
- 用户点击【继续录制】→ 回到 RECORDING → 三按钮隐藏 + 【停止】显示
- 用户点击【保存结束】→ 进入 SAVED → 关闭窗口（后台裁剪继续）
- 用户点击【丢弃重录】→ 弹 QMessageBox 确认 → 确认后删除录制包 + 关窗

### 丢弃确认对话框

点击【丢弃重录】后会弹出确认对话框：

```
┌─────────────────────────────────────┐
│  丢弃录制                            │
├─────────────────────────────────────┤
│  确定丢弃？此操作不可恢复             │
│                                     │
│              [是(Y)]  [否(N)]        │
└─────────────────────────────────────┘
```

- **是(Y)**：调 `controller.discard()` 删除录制包目录 + 关窗 + 回 IDLE
- **否(N)**：保持 PAUSED 状态 + 三按钮继续显示

### 保存关窗策略

点击【保存结束】后：
1. 调 `controller.save()` 标记 SAVED + 启动后台裁剪 daemon thread
2. **立即关闭录制窗口**（不等裁剪完成，D029 决策"save() 立即返回不阻塞"）
3. 后台裁剪线程在 daemon thread 中继续执行，不阻塞进程退出
4. meta.json 的 `status` 字段三态机：`processing`（裁剪中）→ `saved`（成功）/ `failed`（失败）

用户体感为"点保存即可关窗，裁剪在后台静默执行"。

### FloatingBar 和 MonitorWindow 一致

两个 GUI 模式（小模式悬浮条 + 大模式监控面板）的三按钮行为完全一致：
- 按钮位置：FloatingBar 底部 / MonitorWindow 底部按钮栏
- 按钮颜色：蓝（继续录制）/ 绿（保存结束）/ 红（丢弃重录）
- 信号 wire：main.py 的 `_GUIController` 统一 wire 两个 GUI 的三按钮信号到 RecorderApp

## 十一、音频裁剪

L0 v2 重构（D027/D033）后，**保存录制时自动后台裁剪静音段**，减小硬盘占用，并生成 `audio_segments.json` 时间戳映射表供 L1/L4 还原原始时间轴。

### 裁剪触发时机

- **触发**：`controller.save()` 时自动启动 daemon thread 执行 `audio_trimmer.trim_audio()`
- **异步**：save() 立即返回不阻塞，裁剪在后台 daemon thread 中执行
- **不弹提示**：用户无感知，meta.json 的 `status` 字段记录裁剪进度（processing/saved/failed）

### 裁剪算法

1. **RMS 计算**：按 0.1s 帧分块计算 RMS_dB = 20·log10(RMS/32768)（int16 满量程）
2. **静音段识别**：连续帧 RMS_dB < **-40 dB** 且总时长 > **1.5s** → 标记为删除段
3. **键鼠声保护（D033）**：扫描 events.jsonl 的 mouse_click/keyboard_input 时间戳，标记 ±100ms 受保护区
   - 受保护区内的静音段**不删除**（即使 RMS 低于阈值，可能是用户敲键盘的间隙）
   - 受保护区内的高 RMS 瞬态噪声**衰减 ×0.1**（不删除，避免时间轴断裂）
4. **删除 + 拼接**：删除静音段，拼接保留段生成 `mic_cropped.wav`
5. **全静音占位**：所有段都被判为静音时，保留 0.5s 占位（避免空 WAV）

### audio_segments.json 映射表

裁剪后在录制包 `audio/` 目录生成 `audio_segments.json`：

```json
{
  "original_duration": 10.0,
  "cropped_duration": 6.0,
  "segments": [
    {
      "original_start": 0.0,
      "original_end": 2.0,
      "cropped_start": 0.0,
      "cropped_end": 2.0
    },
    {
      "original_start": 4.0,
      "original_end": 6.0,
      "cropped_start": 2.0,
      "cropped_end": 4.0
    },
    {
      "original_start": 8.0,
      "original_end": 10.0,
      "cropped_start": 4.0,
      "cropped_end": 6.0
    }
  ],
  "silence_threshold_db": -40.0,
  "silence_min_duration": 1.5,
  "version": "0.1.0"
}
```

- `original_duration`：原始 WAV 时长（秒）
- `cropped_duration`：裁剪后 WAV 时长（秒）
- `segments`：保留段列表，每段记录原始时间区间 `[original_start, original_end]` 和裁剪后时间区间 `[cropped_start, cropped_end]`
- 各段 `cropped_start` == 前一段 `cropped_end`（首尾相连）
- 最后一段 `cropped_end` == `cropped_duration`

### meta.json 状态字段

裁剪期间 meta.json 的 `status` 字段三态机：

| status | 说明 | 时机 |
|--------|------|------|
| `recording` | 录制中 | start() 后 |
| `processing` | 后台裁剪中 | save() 后立即标记 |
| `saved` | 裁剪完成 | 裁剪 daemon thread 成功完成 |
| `failed` | 裁剪失败 | 裁剪 daemon thread 抛异常，meta.json 记录 `error` 字段 + 保留原始音频 |

### 裁剪失败处理

- 裁剪 daemon thread 异常时 try/except 兜底，**不崩进程**
- meta.json 标记 `status="failed"` + `error` 字段记录异常信息
- **原始音频保留**：`mic.wav` 不删除，可作为备份供 L1 手动处理
- `on_trim_complete` 回调仍触发（让 L1 知道流程结束）

### 录制包目录结构（v2 重构后）

```
rec_YYYYMMDD_HHMMSS/
├── meta.json           # meta（含 status/segments/effective_duration 新字段）
├── events.jsonl        # 全部事件流
├── focus.jsonl         # 焦点切换事件子集
├── frames/             # 截图帧
│   └── *.png
└── audio/
    ├── mic.wav               # 原始音频（裁剪后保留作为备份）
    ├── mic_cropped.wav       # 裁剪后音频（L1 STT 输入）
    └── audio_segments.json   # 时间戳映射表
```

### on_trim_complete 回调 hook（L1 接入点）

`controller.__init__` 接受可选 `on_trim_complete: Callable[[Path], None]` 参数：
- 默认 no-op（L0 阶段不接入 L1）
- 裁剪 daemon thread 完成后调用（无论成功失败都触发）
- 参数为录制包根路径
- **L1 就绪后注入实际回调**触发 STT 流水线（D032 决策）

## 十二、故障排查

### 启动失败：`ModuleNotFoundError: No module named 'pynput'`

录制器依赖 `pynput`（全局键鼠监听）/ `mss`（屏幕截图）/ `sounddevice`（音频录制）。如缺失：

```bash
uv pip install pynput mss sounddevice soundfile
```

### 启动失败：`PermissionError` 或键鼠无响应

Windows 下全局键鼠监听需要管理员权限（部分系统）。右键命令行 → "以管理员身份运行"。

### 截图全黑或截图失败

- **全屏模式**：检查显示器是否正常工作
- **窗口模式**：检查窗口是否最小化（最小化时跳过截图），或窗口是否以管理员权限运行（UIPI 阻止）
- **某个屏幕模式**：检查 `--monitor-index` 是否正确

### 音频录制失败

- 检查麦克风是否连接且未被占用
- 检查 `audio_enabled` 是否为 `true`
- 用 `--no-audio` 跳过音频录制

### 录制包为空（events.jsonl 无内容）

- 检查是否真的开始录制（悬浮条应显示 ●REC）
- 检查传感器是否启动成功（控制台无报错）
- 用 `--no-gui --autostart --max-duration 10` 跑 10 秒冒烟测试验证

### 录制包目录结构

```
rec_YYYYMMDD_HHMMSS/
├── meta.json           # 必须存在，含开始/结束时间/事件数/帧数/版本号
├── events.jsonl        # 必须存在，每行一个 JSON 事件
├── focus.jsonl         # 必须存在（可能为空，如果无窗口切换）
├── frames/             # 必须存在（可能为空，如果截图全失败）
│   └── *.png
└── audio/
    └── mic.wav         # audio_enabled=true 时必须存在
```

### meta.json 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `package_name` | str | 录制包目录名（如 `rec_20260723_143000`） |
| `start_time` | float | 开始时间戳（time.time()） |
| `end_time` | float | 结束时间戳 |
| `duration_seconds` | float | 录制时长（秒，含暂停段） |
| `event_count` | int | 事件总数 |
| `frame_count` | int | 截图帧总数 |
| `audio_seconds` | float | 音频时长（秒） |
| `sensors` | list[str] | 启用的传感器类名列表 |
| `version` | str | 录制包格式版本（当前 `0.1.0`） |
| `format` | str | 格式标识（`localagent.recorder.v1`） |
| `status` | str | 录制状态（v2 新增）：`recording` / `processing` / `saved` / `failed` |
| `segments` | list | pause/resume 时间点（v2 新增）：`[{start_offset, end_offset}, ...]` |
| `effective_duration` | float | 净录制时长（v2 新增，剔除暂停段后的有效时长） |
| `original_wav` | str | 原始音频路径（v2 新增，裁剪后保留作为备份） |
| `cropped_wav` | str | 裁剪后音频路径（v2 新增，L1 STT 输入） |
| `audio_segments` | str | audio_segments.json 路径（v2 新增，时间戳映射表） |
| `error` | str | 裁剪失败时的错误信息（v2 新增，仅 status=failed 时存在） |

### 停止后无法继续（已修复）

**症状**：v1 版本中，停止录制后悬浮条/监控面板的停止按钮被禁用（`setEnabled(False)` + 文字"● 未在录制"），用户无法再次开始新录制，必须重启录制器。

**根因**：v1 仅有 start/stop 两状态，停止后没有清晰的"接下来做什么"路径，按钮直接禁用避免误操作。

**修复（v2 重构）**：升级为 5 状态有限状态机 + 三按钮交互（见[十、三按钮交互](#十三按钮交互)）：
- 停止后进入 PAUSED 状态，底部弹三按钮【继续录制】【保存结束】【丢弃重录】
- 用户选择【继续录制】回到 RECORDING 继续录（时间轴延续）
- 用户选择【保存结束】保存录制包 + 关窗（后台裁剪继续）
- 用户选择【丢弃重录】删除录制包 + 回 IDLE + 可重新 start 新录制

**修复版本**：L0 v2 重构（D026-D033，2026-07-23）


## 十三、冒烟测试

### 自动化冒烟测试（3 秒）

```bash
uv run pytest workspace/recorder/tests/test_recorder_smoke.py -v
```

用 MockSensor 模拟键鼠事件 + 假截图 + 假音频，3 秒后自动停止，验证所有数据文件可解析：
- events.jsonl JSON 合法 + 按时间戳排序
- frames/ 下 PNG 可被 PIL 打开
- audio/mic.wav 可被 wave 模块读回
- focus.jsonl JSON 合法
- meta.json 含全部必要字段

### 真实录制 3 分钟冒烟测试

```bash
# 无 GUI 模式，3 分钟后自动停止
python -m workspace.recorder.tools.main --no-gui --autostart --range fullscreen --detail-level detailed --max-duration 180

# GUI 模式，手动开始/停止
python -m workspace.recorder.tools.main --mode small
# 按 Ctrl+Alt+R 开始录制
# 操作 3 分钟
# 按 Ctrl+Alt+R 停止录制
```

## 十四、设计决策（L0 相关）

### v1 决策（D016-D025，Ticket 01-08）

| 决策 | 说明 |
|------|------|
| D016 | 录制 GUI 支持大小两种模式（小模式悬浮条 + 大模式监控面板） |
| D017 | 录制范围可选全屏/某屏/某窗口 |
| D018 | L0 采集器独立进程运行（不走后端 API） |
| D019 | 公用库 lib/ + 代码位置 lib/recorder/ + workspace/recorder/tools/ |
| D021 | 密码框保护（IsPassword → password_masked，不记录内容） |
| D022 | 物理按键序列保留到 supplements.physical_keys |
| D023 | click 后多帧采样（50/100/150ms）+ 帧哈希自动去重 |
| D024 | PrintWindow + PW_RENDERFULLCONTENT 截取被遮挡窗口 + IsIconic 检测最小化暂停 |
| D025 | 详细/粗略两种采样模式 + 30 分钟自动停止 |

### v2 重构决策（D026-D033，Ticket 09-13）

| 决策 | 说明 |
|------|------|
| D026 | RecordingController 状态机扩展为 5 状态有限状态机（IDLE/RECORDING/PAUSED/SAVED + discard 回 IDLE），支持 pause/resume/save/discard |
| D027 | 音频静音裁剪：RMS 低于 -40dB 且持续 > 1.5s 判为静音段删除，生成 audio_segments.json 映射表 |
| D028 | L0 v2 重构 5 ticket 完成后跑全部 recorder 测试 + 端到端验证 + 文档更新 |
| D029 | save() 立即返回不阻塞关窗 + 后台启动音频裁剪（不弹提示） |
| D030 | 停止后必须三选一：继续录制（resume）/ 保存结束（save）/ 丢弃重录（discard） |
| D031 | 音频裁剪异步后台执行（daemon thread）+ meta.json status 三态机（processing/saved/failed） |
| D032 | L0 预留 on_trim_complete 回调 hook，L1 就绪后接入 STT 流水线 |
| D033 | resume 时间轴延续（不重新构造 TimestampService，timestamp 基准不变）+ 键鼠声可选处理（瞬态噪声衰减 ×0.1 不删除避免时间轴断裂） |

## 十五、L0 采集层架构

```
┌─────────────────────────────────────────────────────────────┐
│  workspace/recorder/tools/                                            │
│  ├── main.py           启动入口（CLI + GUI 协调 + 三按钮 wire）│
│  ├── config_loader.py  config.toml [recording] 加载器        │
│  ├── config.py         RecordingConfig dataclass + 工厂函数  │
│  ├── config_dialog.py  ConfigDialog（范围/模式/参数选择）     │
│  ├── hotkey_manager.py HotkeyManager（Ctrl+Alt+R 全局热键）  │
│  ├── floating_bar.py   FloatingBar（小模式 + 三按钮交互）    │
│  ├── monitor_window.py MonitorWindow（大模式 + 三按钮交互）  │
│  └── recorder_app.py   RecorderApp（协调类 + 30 分钟自动停止）│
├─────────────────────────────────────────────────────────────┤
│  lib/recorder/                                              │
│  ├── controller.py     RecordingController（5 状态机 + 后台裁剪线程）│
│  ├── package.py        RecordingPackage（目录 schema + 读写 + update_status）│
│  ├── audio_trimmer.py  audio_trimmer（静音裁剪 + audio_segments.json）│
│  ├── timestamp.py      TimestampService（时间戳基准）        │
│  ├── types.py          make_event 工厂 + KIND_* 常量         │
│  └── sensors/                                               │
│      ├── base.py       Sensor Protocol                      │
│      ├── mock.py       MockSensor（测试用）                  │
│      ├── keyboard.py   KeyboardSensor（pynput + UIA 差值）   │
│      ├── mouse.py      MouseSensor（pynput + 拖拽检测）      │
│      ├── screen.py     ScreenCaptureSensor（mss + PrintWindow）│
│      ├── audio.py      AudioSensor（sounddevice + WAV + pause/resume 追加模式）│
│      └── window.py     WindowFocusSensor（焦点轮询）         │
├─────────────────────────────────────────────────────────────┤
│  lib/uia.py             UIA helper（ValuePattern 差值主路径）│
└─────────────────────────────────────────────────────────────┘
```

## 十六、L1 处理层

L1 处理层把 L0 采集的原始信号（events.jsonl + frames/ + audio/mic.wav）加工成结构化 JSON，供 L2 时间轴引擎对齐为 timeline.json，最终供 L3 编辑器和 L4 agent 消费层使用。

### 1. 入口：process_recording_package

L1 处理层统一入口在 `lib/recorder/processor/pipeline.py`：

```python
from lib.recorder.processor import process_recording_package, ProcessingResult

result = process_recording_package(
    package_path="workspace/recorder/recordings/rec_20260723_143000",
    options={
        "run_p4": True,              # P4 事件聚合（默认 True，L3 编辑器启动时自动跑）
        "run_p5": True,              # P5 关键帧抽取（默认 True，L3 编辑器启动时自动跑）
        "run_p1": False,             # P1 STT（默认 False，用户配好 VAD 参数后手动触发）
        "p1_vad_threshold": 0.5,     # P1 VAD 阈值（faster-whisper vad_parameters.threshold）
        "p1_language": "zh",         # P1 语言（initial_prompt 按语言配置）
        "p1_model": "large-v3",      # P1 STT 模型（D037，可在 config.toml 改为 large-v3-turbo）
        "p1_model_dir": None,        # P1 模型存放目录（None 时用 faster-whisper 默认缓存）
        "p1_transcriber": None,      # P1 已加载模型的 WhisperTool（避免重复加载，测试用 mock）
        "p4_idle_threshold": 2.0,    # P4 idle 块阈值（秒，操作间隔 > 此值插入 idle 块）
        "p4_before_frame_window": 0.2,  # P4 前截图窗口（秒，操作前 200ms 内最近帧）
        "p4_after_frame_window": 0.3,   # P4 后截图窗口（秒，操作后 300ms 内最近帧）
        "p5_phash_threshold": 5,     # P5 全局 pHash 阈值（汉明距离 ≤ 此值视为相似）
    },
)

# ProcessingResult 字段：
# - blocks_path / keyframes_path / transcript_path：三个 JSON 产出路径（未跑时为 None）
# - block_count / keyframe_count / transcript_segment_count：各产出计数
# - errors：各阶段错误信息（key=阶段名 "p1"/"p4"/"p5"，value=错误描述）
```

**触发时机**（spec-l1.md）：
- **P4/P5 自动跑**：L3 编辑器启动时自动调 `process_recording_package(run_p4=True, run_p5=True, run_p1=False)`
- **P1 手动触发**：用户在 L3 编辑器配好 VAD 阈值后手动点"开始 STT"按钮触发 `process_recording_package(run_p1=True, ...)`
- **L1 不走后端 API**（延续 D018）：L1 是离线处理，不依赖后端服务

### 2. 三个产出 JSON

L1 在录制包目录下产出三个独立 JSON 文件：

```
rec_YYYYMMDD_HHMMSS/
├── meta.json           # L0 产出
├── events.jsonl        # L0 产出
├── focus.jsonl         # L0 产出
├── frames/             # L0 产出（已做相邻帧 pHash 去重 D035）
├── audio/
│   └── mic.wav         # L0 产出（完整保留，D034 移除自动裁剪）
├── blocks.json         # L1 P4 产出（操作块序列）
├── keyframes.json      # L1 P5 产出（关键帧列表）
└── transcript.json     # L1 P1 产出（STT 转写文本，含词级时间戳）
```

#### blocks.json（P4 事件聚合器产出）

P4 把 L0 的 events.jsonl 聚合为操作块序列。每个底层操作事件转为一个操作块，操作间隔 >2s 插入 idle 块，自动关联前后截图。

```json
[
  {
    "id": "b001",
    "category": "operation",
    "type": "mouse_click",
    "timestamp": 1.0,
    "duration": 0,
    "primary": {"button": "left", "clicks": 1, "x": 100, "y": 200},
    "supplements": {
      "before_frame": "frames/frame_0000050_000000.png",
      "after_frame": "frames/frame_0000100_000001.png",
      "uia_snapshot": null,
      "linked_text": []
    },
    "status": {"marked_key": false, "marked_anomaly": false, "marked_automatable": false, "is_trimmed": false}
  },
  {
    "id": "b002",
    "category": "operation",
    "type": "keyboard_input",
    "timestamp": 2.0,
    "duration": 0,
    "primary": {"text": "admin", "physical_keys": ["a", "d", "m", "i", "n"], "detection_method": "uia_value_diff"},
    "supplements": {"before_frame": null, "after_frame": null, "uia_snapshot": null, "linked_text": []},
    "status": {"marked_key": false, "marked_anomaly": false, "marked_automatable": false, "is_trimmed": false}
  }
]
```

**块类型映射**（02-block-design.md）：
- `mouse_click` / `mouse_scroll` / `mouse_drag` → 同名块
- `keyboard_input` / `hotkey` / `ime_switch` / `password_masked` → `keyboard_input` 块
- `focus_change` → `focus_change` 块（强制章节边界）
- 操作间隔 > `p4_idle_threshold`（默认 2.0s）→ 插入 `idle` 块（可被 L3 编辑器裁剪）
- `screen_frame` 事件不生成块（用于截图关联）

#### keyframes.json（P5 关键帧抽取器产出）

P5 把 L0 的 frames/ + events.jsonl 处理为关键帧列表，目标 360 帧降到 ~50 帧。

```json
[
  {
    "frame_path": "frames/frame_0000050_000000.png",
    "timestamp": 0.5,
    "retain_reason": "timer_unique"
  },
  {
    "frame_path": "frames/frame_0000100_000001.png",
    "timestamp": 1.0,
    "retain_reason": "operation"
  }
]
```

**保留策略**：
- **操作帧**（`retain_reason="operation"`）：在 mouse_click / mouse_drag / focus_change 事件后 200ms 内的截图直接保留，不去重（确保重要操作视觉证据全保留）
- **定时帧**（`retain_reason="timer_unique"`）：与已保留帧做全局 pHash 对比，汉明距离 ≤ `p5_phash_threshold`（默认 5）则跳过（相似帧只留第一张，检测滚动后回到原位置等跨时间相似场景）

pHash 算法复用 L0 D035 实现（8×8 灰度 + 均值哈希 + 汉明距离），保证 L0/L1 算法统一。

#### transcript.json（P1 STT 转换器产出）

P1 把 L0 的 audio/mic.wav 转写为文本，含词级时间戳。

```json
[
  {
    "line": 1,
    "start": 1.23,
    "end": 4.50,
    "text": "现在我要点击登录按钮"
  },
  {
    "line": 2,
    "start": 5.00,
    "end": 7.80,
    "text": "然后输入用户名和密码"
  }
]
```

- `start` / `end` 是 float 秒，**与 events.jsonl 时间轴同源**（spec-l1.md："时间戳直接是原始时间轴，不需要映射"）
- L3 编辑器可按语速切片，L4 agent 可按时间戳关联操作块

### 3. P1 STT 重试策略（D038）

Whisper 对纯中文音频易输出"请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"等垃圾文本（模型训练数据偏英文/多语种）。P1 照抄 stt-main 项目的渐进式归一化重试策略：

1. **原始音频转写** → `is_junk_text` 检测垃圾文本占比（`JUNK_PHRASES` + `JUNK_THRESHOLD=0.3`）
2. **LUFS 归一化重试**（最多 4 次，每次 `target_lufs += 3.0`，初始 -15.0 → -12 → -9 → -6）
3. **25% 分位归一化**（LUFS 全失败后）
4. **全部失败 → 写空 transcript.json**（不抛异常，L3 编辑器显示"STT 失败"提示）

**垃圾文本检测**（`is_junk_text`）：逐项检查 transcript 的 text 是否完全等于 `JUNK_PHRASES` 任一项，命中比例 > `JUNK_THRESHOLD`（默认 0.3）判垃圾。

**配置项**（`config.toml [recording.stt]`）：

```toml
[recording.stt]
enabled = false                    # L1 是否启用 STT（L3 编辑器读）
model = "large-v3"                 # STT 模型（D037，可改 large-v3-turbo）
model_dir = ""                     # 模型存放目录（空时用 faster-whisper 默认缓存）
language = "zh"                    # 语言（initial_prompt 按语言配置）
vad_threshold = 0.5                # VAD 阈值（faster-whisper vad_parameters.threshold）
junk_threshold = 0.3               # 垃圾文本判定阈值（占比 > 此值判垃圾）
target_lufs = -15.0                # LUFS 归一化初始目标响度
lufs_attempt = 4                   # LUFS 归一化最大重试次数
lufs_step = 3.0                    # 每次 LUFS 目标响度增量
max_chars_per_line = 40            # 单条字幕最大字符数（L3 编辑器用）
min_duration_per_line = 1.5        # 单条字幕最短持续时间秒（L3 编辑器用）
```

### 4. P2 UIA 结构化器（接口定义，不实现）

P2 定义按需采集 UIA 快照的接口契约（`lib/recorder/processor/p2_uia.py`）：

```python
from lib.recorder.processor import UIASnapshotter, run_p2

# Protocol 接口契约（实现方需遵守）
class UIASnapshotter(Protocol):
    def snapshot(
        self,
        package_root: Path,
        timestamp: float,
        *,
        hwnd: int | None = None,
        max_depth: int = 5,
    ) -> str | None:
        """采集 UIA 快照到 uia_snapshots/*.json，返回相对路径。"""
        ...

# L1 入口（未实现，需注入 snapshotter）
snapshot_path = run_p2(
    package_root=package_root,
    timestamp=1.23,
    snapshotter=my_snapshotter,  # None 时返回 None（L1 不提供默认实现）
    hwnd=12345,
)
```

**L1 SDD 范围**：仅定义接口签名 + Protocol，不实现具体采集逻辑。

**调用时机**（未来）：
- L1 不实时跑 P2（spec-l1.md 明确"L1 不走 VL/UIA 等慢操作"）
- L4 agent 消费录制包时按需调用 P2 采集当前 UIA 状态
- 或 L3 编辑器用户手动点击"采集 UIA"按钮触发

**实现参考**：`lib/uia.py` 的 `take_focused_value_snapshot()` 已用 uiautomation 库取焦点控件的 ValuePattern，可在此基础上扩展为完整控件树采集。

### 5. P3 VL 标注器（接口定义，不实现）

P3 定义按需调 VL 标注的接口契约（`lib/recorder/processor/p3_vl.py`）：

```python
from lib.recorder.processor import VLAnnotator, run_p3

# Protocol 接口契约（实现方需遵守）
class VLAnnotator(Protocol):
    def describe(
        self,
        image_path: Path,
        question: str,
        *,
        max_chars: int = 200,
    ) -> str | None:
        """对截图带问题调 VL，返回 ≤max_chars 描述。"""
        ...

# L1 入口（未实现，需注入 annotator）
description = run_p3(
    image_path=package_root / "frames" / "frame_00001230_000000.png",
    question="登录按钮在哪里？是否可点击？",  # 必须非空（"带问题看图"硬约束）
    annotator=my_annotator,  # None 时返回 None（L1 不提供默认实现）
)
```

**L1 SDD 范围**：仅定义接口签名 + Protocol，不实现具体 VL 调用。

**核心原则**（07-agent-consumption.md）：
- "vl和stt都不应该实时，因为它们很慢。截图考虑采集去重后不处理，等agent开始理解过程时按需说明关注焦点再vl"
- **带问题看图（必须）**：每次 VL 调用必须带 `question` 参数（`run_p3` question 为空时抛 `ValueError`）
- **不预存结果**：录制包不做 VL 预处理，agent 按需调用或直接看图
- **多轮 VL 协议**：Round 1 建立全局认知 → Round 2 选关键帧 + 带问题 → Round 3 收敛

**调用时机**（未来）：
- L1 不实时跑 P3
- L4 agent 消费录制包时，根据全局认知选关键帧 + 带问题调 VL
- 调用走现有 `understand_image` MCP 工具，或 agent 原生 VL 能力直接读图

### 6. L1 处理层架构

```
┌─────────────────────────────────────────────────────────────┐
│  lib/recorder/processor/                                    │
│  ├── __init__.py            导出 process_recording_package  │
│  │                          + ProcessingResult              │
│  │                          + UIASnapshotter/run_p2         │
│  │                          + VLAnnotator/run_p3            │
│  ├── pipeline.py            process_recording_package seam  │
│  │                          （编排 P1/P4/P5 + 选项字典）     │
│  ├── p4_event_aggregator.py P4 事件聚合器                    │
│  │                          （events.jsonl → blocks.json）  │
│  ├── p5_keyframe_extractor.py P5 关键帧抽取器                │
│  │                          （frames/ → keyframes.json）    │
│  ├── p1_stt.py              P1 STT 转换器入口                │
│  │                          （mic.wav → transcript.json）   │
│  ├── stt_engine.py          WhisperTool（照抄 stt-main）     │
│  ├── stt_retry.py           重试策略（照抄 stt-main）        │
│  │                          （is_junk_text + LUFS 归一化）   │
│  ├── p2_uia.py              P2 UIA 接口定义（不实现）        │
│  └── p3_vl.py               P3 VL 接口定义（不实现）         │
└─────────────────────────────────────────────────────────────┘
```

### 7. L1 设计决策（D034-D038）

| 决策 | 说明 |
|------|------|
| D034 | 移除自动音频裁剪：`controller.save()` 不再调 `audio_trimmer`，mic.wav 完整保留（裁剪移到 L3 编辑器） |
| D035 | L0 采集时 pHash 去重：`ScreenCaptureSensor` 相邻帧 pHash 对比（8×8 灰度 + 均值哈希 + 汉明距离），相似帧不保存 |
| D036 | pHash 阈值滑块：`ConfigDialog` 新增"截图去重"QSlider（range 0-16, default 5），config.toml `[recording.capture] phash_threshold` |
| D037 | STT 默认模型 large-v3（用户可在 config.toml 改为 large-v3-turbo） |
| D038 | STT 重试策略照抄 stt-main 的 `transcribe_with_fallback`（原始 → LUFS ×4 → 25% 分位 → 跳过） |

## 十七、L2 时间轴层

L2 时间轴层把 L1 处理层产出的三个独立 JSON（`blocks.json` + `keyframes.json` + `transcript.json`）合并为统一时间轴 `timeline.json`，并初始化空标注文件 `annotations.json` 供 L3 编辑器使用。L2 有独立入口 `build_timeline(package_path)` + CLI 预览，L3 编辑器启动时自动调用。

### 17.1 入口与 CLI

**Python 入口**（`lib/recorder/timeline/`）：

```python
from lib.recorder.timeline import build_timeline, init_annotations

# 构建 timeline.json + 初始化 annotations.json
result = build_timeline(package_path)
# result.timeline_path / result.annotations_path / result.block_count / result.chapter_count / result.errors

# 单独初始化 annotations.json（已存在时不覆盖）
init_annotations(package_path)
```

**CLI 入口**（`workspace/recorder/tools/timeline_cli.py`）：

```bash
# 默认：构建 timeline.json + 初始化 annotations.json + 打印预览
uv run python -m workspace.recorder.tools.timeline_cli workspace/recorder/recordings/rec_20260723_162924

# 只预览不重新构建（要求 timeline.json 已存在）
uv run python -m workspace.recorder.tools.timeline_cli <package_path> --preview-only

# 自定义章节切分空闲阈值（默认 2.0s）
uv run python -m workspace.recorder.tools.timeline_cli <package_path> --idle-threshold 3.0
```

CLI 构建信息输出到 stderr，预览内容输出到 stdout（便于管道处理）。

### 17.2 timeline.json 格式

```json
{
    "recording_id": "rec_20260723_162924",
    "duration_seconds": 203.141,
    "block_count": 63,
    "tracks": ["operation", "image", "chapter"],
    "blocks": [
        {"id": "c001", "category": "text", "type": "chapter", "timestamp": 0.0, ...},
        {"id": "b001", "category": "operation", "type": "focus_change", "timestamp": 0.031, ...},
        {"id": "f001", "category": "image", "type": "screenshot", "timestamp": 3.672, ...},
        {"id": "t001", "category": "text", "type": "stt_transcript", "timestamp": 5.2, ...}
    ]
}
```

**块 ID 前缀**（D041）：
- `b001/b002/...`：操作块（category=operation，来自 blocks.json）
- `f001/f002/...`：图像块（category=image，来自 keyframes.json）
- `t001/t002/...`：转写块（category=text, type=stt_transcript，来自 transcript.json）
- `c001/c002/...`：章节块（category=text, type=chapter，T2 章节切分器生成）

**tracks**：根据实际有的块类型构建，4-track 独立设计（chapter 是独立 track，虽然章节块 category=text）。

### 17.3 章节切分规则（D042）

T2 章节切分器扫描操作块序列，按以下规则切章节：

1. **第一个章节**：timestamp=0.0，章节名取第一个 focus_change 的 title 或"开始"
2. **focus_change 切章节**：每个窗口切换事件切一个新章节，章节名取 focus_change 的 title
3. **idle > 阈值切章节**：连续空闲超过阈值（默认 2.0s，可用 `--idle-threshold` 配置）切一个新章节，章节名 "idle Xs"
4. **连续同名去重**：连续同名章节只保留第一个
5. **空操作块不生成章节**：blocks.json 为空时返回空章节列表

### 17.4 音频时间戳映射（D039）

`transcript.json` 的 `start/end` 是音频相对秒数（STT 在裁剪音频上跑的时间轴），需要映射到录制包时间轴：

- **有 `audio/audio_segments.json`**（D034 前的裁剪包）：用 segments 映射表，公式 `original_start + (cropped_time - cropped_start)`
- **无 `audio_segments.json`**（D034 后 mic.wav 完整保留）：音频相对秒数直接等于录制包时间轴，无需映射

注：spec-l2.md D039 中的公式 `cropped_start + (audio_time - original_start)` 方向写反，正确方向是 `original_start + (cropped_time - cropped_start)`。

### 17.5 CLI 预览 6 项内容

`format_preview(package_path)` 生成 6 项摘要文本：

1. **录制包概览**：duration / effective_duration / event_count / frame_count / audio_seconds / status
2. **章节列表**：章节 ID + 时间范围 + 章节名 + 块数
3. **块类型分布**：category/type 统计 + 总计
4. **时间跨度**：首尾时间戳 + 时间跨度 + 录制时长
5. **三时间戳对齐验证**：操作块/关键帧/转写 时间戳范围 + 越界警告（>录制时长+1s 容差）
6. **帧压缩比**：原始帧→关键帧→image 块 + 两个压缩百分比

### 17.6 annotations.json（D043）

L2 只初始化空文件，不做自动标注（避免误标）。L3 编辑器保存时追加标注。

```json
{
    "annotations": []
}
```

`init_annotations(package_path)` 已存在时不覆盖（保留 L3 编辑器的标注）。

### 17.7 设计决策（L2 相关）

| 决策 ID | 决策内容 |
|---------|---------|
| D039 | 音频时间戳映射：有 audio_segments.json 时映射，无时直接使用；映射方向 `original_start + (cropped_time - cropped_start)` |
| D040 | keyframes 转独立 image 块：keyframes.json 每项转为 category=image, type=screenshot 的块，ID 用 f001/f002... |
| D041 | 块 ID 前缀：b=operation / f=image / t=text / c=chapter；4-track 独立设计（chapter 是独立 track） |
| D042 | 章节切分粒度：focus_change + idle>阈值 切章节；第一个章节 timestamp=0.0；连续同名去重；空操作块不生成章节 |
| D043 | annotations.json 只初始化不标注：L2 创建空文件，L3 编辑器保存时追加标注 |

### 17.8 模块结构

```
lib/recorder/timeline/
├── __init__.py        # 包入口，导出 build_timeline / TimelineResult / init_annotations
├── builder.py         # T1 时间轴引擎：build_timeline seam（合并四类块按时间戳排序）
├── chapterizer.py     # T2 章节切分器：focus_change + idle 切章节（D042）
├── annotator.py       # T3 重点标注器：init_annotations 初始化空 annotations.json（D043）
└── preview.py         # CLI 预览：format_preview 生成 6 项摘要文本

workspace/recorder/tools/
└── timeline_cli.py    # CLI 入口：python -m workspace.recorder.tools.timeline_cli <package_path>
```

### 17.9 真实录制包测试结果

用三个 L0 真实录制包跑完整 L2 流程，验证通过：

| 录制包 | 格式 | 块总数 | 章节数 | 帧压缩比 | 状态 |
|--------|------|--------|--------|----------|------|
| rec_20260723_141608 | v1 无裁剪 | 125 | 16 | 13.3% (128→17) | ✅ |
| rec_20260723_161415 | v2 有裁剪 | 56 | 8 | 20.3% (64→13) | ✅ |
| rec_20260723_162924 | v2 有裁剪 | 63 | 11 | 5.8% (86→5) | ✅ |

## 十八、L3 编辑层

L3 编辑层把 L2 时间轴层产出的 `timeline.json`（只读）+ 空 `annotations.json` 接入可视化编辑器，用户可标注关键/异常/可自动化、移除/合并/拆分块、插入注释/章节/图片/文件文本、交互式调音频阈值并重跑 STT。所有编辑写入 `annotations.json`，`timeline.json` 不修改。L4 agent 读时 merge timeline + annotations 获取最终视图。

### 18.1 启动方式

**CLI 入口**（`workspace/recorder/tools/editor/__main__.py`）：

```bash
# 独立启动编辑器
uv run python -m workspace.recorder.tools.editor workspace/recorder/recordings/rec_20260723_162924

# 查看用法
uv run python -m workspace.recorder.tools.editor --help
```

**录制器保存后自动启动**（D032/D052）：`controller.save()` 完成后，`recorder_app.py` 调 `subprocess.Popen([sys.executable, "-m", "workspace.recorder.tools.editor", str(package_path)])` 异步拉起编辑器，用户保存录制包后无缝进入编辑流程。

**启动流程**（`workspace/recorder/tools/editor/editor_app.py` 的 `run_editor`）：
1. 解析 CLI 参数（package_path，不存在则报错退出）
2. 检查 `timeline.json` 是否存在，不存在则调 `build_timeline(package_path)` 自动生成（L2 时间轴层）
3. 加载 `timeline.json` + `annotations.json`（`AnnotationStore.load`，不存在时为空）
4. 启动 PySide6 GUI（`EditorWindow`，应用 `lib/ui` 深色工程主题）

L3 延续 D018 不走后端 API，是本地独立 PySide6 进程。

### 18.2 GUI 布局

`EditorWindow`（`workspace/recorder/tools/editor/editor_window.py`）三区域布局 + 顶部工具栏，全部接入 `lib/ui` 深色工程主题（设计 token + QSS 由 `lib/ui/theme.py` 统一生成，禁止硬编码色值/emoji）：

- **左侧章节列表**（~190px，`ChapterList`）：按 chapter 块生成的章节树，点击跳转到对应时间戳；双击可直接重命名，空名称显示"未命名章节"
- **中间时间轴**（自适应宽度，`TimelineView`）：纵向滚动的块列表，每块一行，行高 36；4 列布局（时间 / 事件·窗口 / 证据 / 内容）。证据列为真实缩略图（40×28，`QThreadPool` 异步加载，无截图时留空，双击打开截图查看器）。类型色统一用 `tokens.BLOCK_COLORS`；选中行 accent wash + 左侧 3px ACCENT 竖条
- **下方详情预览**（`DetailPanel`）：左右两栏布局。左栏截图预览（点击打开查看器 + 前后帧切换），右栏结构化字段 + 状态标记 chips + 可编辑内容 + 折叠的原始数据（默认收起，无裸 JSON）。状态 chips 与工具栏双向同步
- **顶部工具栏**：5 组共 11 项动作（编辑 | 标记(checkable) | 插入 | 历史 | 音频 + 就绪），图标全部来自 `lib/ui/icons.py`
- **底部状态栏**：实时显示 `共 N 块 · 已移除 M | E 项编辑 | 总时长 | 就绪状态`
- **空状态**：未加载包时中央 EmptyState + [打开包] 按钮

工具栏 11 项（分组用分隔符隔开，5 个 checkable 动作点击时按目标状态追加正向/反向标注）：

| 组 | 图标 | 功能 | 快捷键 | checkable | 对应 action |
|----|------|------|--------|-----------|------------|
| 编辑 | `eye-off`/`eye` | 移除块/恢复块 | `Delete` | ✅（checked=已移除） | `trim` / `untrim` |
| 编辑 | `merge` | 合并到前块 | `M` | | `merge_blocks` |
| 编辑 | `scissors` | 拆分块 | `S` | | `split_block` |
| 标记 | `star` | 标关键 | `K` | ✅ | `mark_key` / `unmark_key` |
| 标记 | `alert-triangle` | 标异常 | `X` | ✅ | `mark_anomaly` / `unmark_anomaly` |
| 标记 | `bot` | 标可自动化 | `A` | ✅ | `mark_automatable` / `unmark_automatable` |
| 插入 | `plus` | 插入块 | `I` | | `insert_note` / `insert_chapter` / `insert_image` / `insert_text` |
| 历史 | `undo` | 撤销 | `Ctrl+Z` | | （undo 栈） |
| 历史 | `redo` | 重做 | `Ctrl+Shift+Z` | | （redo 栈） |
| 音频 | `sliders` | 音频调参 | | | （打开 AudioPanel） |
| 就绪 | `check-circle` | 标记就绪 | `F` | ✅（checked=已就绪） | （finalize） |

**checkable 状态回显**：选中块变化时，工具栏 4 个块级 checkable 动作（移除/关键/异常/自动化）的 checked 态 + 图标 + statusTip 同步刷新（用 `blockSignals` 避免 setChecked 触发 triggered 回调）。详情面板状态 chips 与工具栏共用同一组 QAction 状态源，双向同步。点击 checkable 动作读 `isChecked()` 得到目标状态：True → 追加正向标注（MARK/TRIM），False → 追加反向标注（UNMARK/UNTRIM）。"就绪"动作 checked 态由包级 finalize 状态驱动，与块选择无关。

**F1 快捷键帮助**：任意时刻按 `F1` 弹出 `ShortcutsHelpDialog`（`workspace/recorder/tools/editor/widgets/shortcuts_help_dialog.py`），分组列出主窗口与截图查看器的全部快捷键。

**截图查看器**（`ScreenshotViewer`，`workspace/recorder/tools/editor/widgets/screenshot_viewer.py`）：模态 QDialog，从详情面板截图单击或证据列缩略图双击打开。支持 Ctrl+滚轮缩放（10%-800%，光标锚点）、左键拖拽平移、双击或 maximize 按钮适应窗口 ↔ 100% 切换、←/→ 键前后帧导航（标题显示 N/M）、Esc 关闭。

**插入块对话框**（`InsertBlockDialog`，`workspace/recorder/tools/editor/widgets/insert_block_dialog.py`）：自定义 QDialog 替代 QInputDialog，类型列表带图标 + 类型名 + 一行说明；显示位置提示（"将插入到选中块（00:03.2 截图）之后"或"追加到时间轴末尾"）；双击列表项直接插入。

块状态叠加显示（D015，可叠加不互斥）：marked_key（左侧 3px BLOCK_KEY 琥珀条纹）/ marked_anomaly（BLOCK_ANOMALY 红条纹）/ marked_automatable（BLOCK_AUTOMABLE 青条纹）/ is_trimmed（50% 透明 + 删除线 + `[已移除]` 前缀，保留在时间轴中，可恢复）。移除/恢复、合并、拆分、插入和标记等工具按钮会随当前选择及移除状态启用或禁用（无选中块时编辑/标记/插入组禁用，statusTip 提示原因）。

"插入块"支持注释、章节、参考图片和文件文本。连续插入会生成唯一的 `u001/u002/...` 块 ID；文本文件限制为 2 MB，并支持 UTF-8（含 BOM）和 GB18030 回退读取。

### 18.3 17 个 action 枚举

所有编辑操作编码为 17 个 action 枚举（D046 + T1 unmark/untrim 扩展，`lib/recorder/editor/annotation.py` 的 `Action`）：

| action | target_block_id | payload | 说明 |
|--------|-----------------|---------|------|
| `mark_key` | 块 ID | `{}` | 标记关键步骤 |
| `mark_anomaly` | 块 ID | `{}` | 标记异常 |
| `mark_automatable` | 块 ID | `{}` | 标记可自动化 |
| `unmark_key` | 块 ID | `{}` | 取消关键标记（幂等，对未标记块为 no-op） |
| `unmark_anomaly` | 块 ID | `{}` | 取消异常标记（幂等） |
| `unmark_automatable` | 块 ID | `{}` | 取消可自动化标记（幂等） |
| `trim` | 块 ID | `{}` | 移除块（标记为 trimmed，可恢复） |
| `untrim` | 块 ID | `{}` | 恢复已移除的块（幂等，对未移除块为 no-op） |
| `insert_note` | 父块 ID | `{text, position, inserted_block_id}` | 插入文字注释 |
| `insert_chapter` | 父块 ID | `{name, position, inserted_block_id}` | 插入章节 |
| `insert_image` | 父块 ID | `{image_path, position, inserted_block_id}` | 插入参考图片 |
| `insert_text` | 父块 ID | `{text, source_file, position, inserted_block_id}` | 插入文件文本 |
| `rename_chapter` | 章节 ID | `{new_name}` | 章节重命名 |
| `merge_blocks` | 主块 ID | `{merged_block_ids: [...]}` | 合并相邻同类块 |
| `split_block` | 块 ID | `{split_timestamp}` | 在指定时间戳拆分块 |
| `move_supplement` | 素材块 ID | `{new_parent_block_id}` | 移动素材附属关系（素材丢失原时间戳，跟随新父块） |

**关键澄清**（D046）：原 02-block-design 的"重排"(reorder) 实际是"素材移动"(move_supplement)——辅助素材重新附属到不同操作块，不是时序重排。基本操作块不能移动；移动的素材丢失原时间戳，跟随新父块。

**幂等性**（T1）：unmark_*/untrim 对未标记/未移除的块执行是合法 no-op，不报错。annotations.json 仍是 append-only；旧包（无 unmark 记录）重放结果不变。schema 版本不变，仅枚举扩展。

**插入块 ID 生成**：插入的新块 ID 用 `u001/u002...`（u=user inserted），category/text/image 根据插入类型决定（`next_inserted_block_id`）。

**position 字段**（D051）：`"supplement"`（附属到父块，时间戳=父块）/ `"after"`（父块之后，独立时间戳=父块+0.001s）/ `"timestamp"`（指定时间戳位置，payload 含 timestamp）。

### 18.4 annotations.json 格式

**数据模型**（D044）：L3 所有编辑（标注 + 内容增强 + 修正）写入 `annotations.json`，`timeline.json` 只读不修改。修正通过 annotation 覆盖原始值（如 chapter 重命名 = annotation 记录新名）。L4 agent 读时 merge timeline + annotations。

**结构**（D045 扁平 action 枚举型）：

```json
{
  "finalized": false,
  "annotations": [
    {
      "id": "a001",
      "target_block_id": "b002",
      "action": "mark_key",
      "payload": {},
      "created_at": "2026-07-23T14:32:00+08:00",
      "undone": false
    },
    {
      "id": "a002",
      "target_block_id": "c001",
      "action": "rename_chapter",
      "payload": {"new_name": "登录操作"},
      "created_at": "2026-07-23T14:33:00+08:00",
      "undone": false
    },
    {
      "id": "a003",
      "target_block_id": "b005",
      "action": "insert_note",
      "payload": {"text": "这里输入了用户名", "position": "after", "inserted_block_id": "u001"},
      "created_at": "2026-07-23T14:34:00+08:00",
      "undone": false
    }
  ]
}
```

每条 annotation 字段：
- `id`：annotation 唯一 ID（`a001/a002...`，`next_annotation_id` 生成）
- `target_block_id`：目标块 ID（`b001/c001/...`，insert_* 时为父块 ID）
- `action`：13 个枚举值之一
- `payload`：随 action 变化的字典
- `created_at`：ISO 8601 时间戳（`datetime.now().astimezone().isoformat()`）
- `undone`：是否已撤销（D050 undo/redo 栈用，merge 时跳过）

**finalized 字段**（D053）："标记就绪"按钮点击后设为 `true`。L4 agent 检查此字段判断录制包是否编辑完成。

**undone 字段**（D050）：被撤销的 annotation 标记 `undone=true`，merge 时跳过；保存时过滤掉 undone 记录，保持 annotations.json 干净。

### 18.5 音频调参面板

L3 编辑器内置音频调参面板（D049，`workspace/recorder/tools/editor/audio_panel.py`），工具栏 `sliders` 图标按钮打开。延续 D048（移除 L0 自动裁剪，mic.wav 完整保留），裁剪/调参移到 L3 交互式进行。

**界面布局**（`AudioPanel`）：
- **上半**：音频波形显示（`WaveformWidget`，QPainter 绘制 RMS dBFS 柱状图，青=保留 / 灰=删除 / 红虚线=阈值）
- **中间**：静音阈值滑块（-60 到 -20 dB，默认 -40 dB）+ 最小静音时长滑块（0.5 到 5.0s，默认 1.5s）
- **下半**：切片预览（实时显示当前阈值下的保留段 + 删除段 + 保留/删除时长占比，`preview_trim` 计算）
- **底部**：▶️ 试音按钮（系统默认播放器播放 mic.wav）+ ✓ 应用配置按钮 + 进度条

**切片预览算法**：复用 `lib/recorder/audio_trimmer.py` 的 RMS 计算 + 静音段识别逻辑（`preview_trim`），但不实际裁剪，只计算切片结果用于预览。滑块变化时实时刷新。

**应用配置流程**（D049，`_on_apply` + `STTRunner`）：
1. 调 `audio_trimmer.trim_audio()` 生成 `mic_cropped.wav` + `audio_segments.json`
2. 后台 `STTRunner`（QThread）跑 `run_p1(vad_threshold=0.3, language="zh", model_name="large-v3", device="cpu")`，用 stt-main 模型缓存（D012）
3. STT 完成后覆盖 `transcript.json`
4. 自动调 `build_timeline(package_path)` 重建 `timeline.json`（因为 transcript 变了）
5. 编辑器重新加载 timeline + annotations + 刷新视图

**覆盖上一次设置**：用户可再次进入音频调参界面，重新调阈值 + 应用配置，覆盖 `mic_cropped.wav` + `transcript.json`。

### 18.6 undo/redo 栈

完整 undo/redo 栈（D050，`lib/recorder/editor/store.py` 的 `AnnotationStore`），支持撤销任何 annotation 操作（包括 rename/merge/split）。

| 操作 | 方法 | 行为 |
|------|------|------|
| 添加 | `add(annotation)` | 追加到 annotations + 压入 undo 栈 + 清空 redo 栈 |
| 撤销 | `undo()` | 弹出 undo 栈顶，标记 `undone=true`，压入 redo 栈 |
| 重做 | `redo()` | 弹出 redo 栈顶，取消 `undone` 标记，压回 undo 栈 |
| 标记就绪 | `finalize()` | 设置 `finalized=true` |
| 保存 | `save(path)` | 写 annotations.json（过滤 undone 记录，保持干净） |
| 加载 | `load(path)` | 读 annotations.json（加载时不恢复 undo/redo 栈，历史已丢失） |

- `active_annotations` 属性返回未 undone 的 annotation（merge 时用）
- `stats()` 返回各 action 类型的数量统计（D047 诊断用，不强制约束）
- 工具栏 `Ctrl+Z` 撤销 / `Ctrl+Shift+Z` 重做，每次操作后自动 save + 刷新视图

**D047 40% 辅助约束**：40% 不是硬约束，是诊断指标。L3 GUI 显示统计信息（改了多少、改什么类型最多），不阻止用户操作。如果用户改很多，说明前面层（L1/L2）需要改进。

### 18.7 merge 逻辑（L4 agent 用）

L4 agent 不直接读 `timeline.json`，而是调 merge 函数获取最终视图（`lib/recorder/editor/merge.py`）：

```python
from lib.recorder.editor.merge import merge_timeline_annotations, is_finalized

# merge timeline + annotations，返回 L4 agent 读的最终视图
view = merge_timeline_annotations(package_path)
# view = {
#   "finalized": bool,
#   "recording_id": str,
#   "duration_seconds": float,
#   "block_count": int,
#   "tracks": list[str],
#   "blocks": list[dict],  # merge 后的块列表（已过滤 is_trimmed）
# }

# 快速检查录制包是否已标记就绪（D053）
ready = is_finalized(package_path)
```

**算法**：
1. 读 `timeline.json`（原始块列表）
2. 读 `annotations.json`（annotation 列表 + finalized 字段）
3. 按顺序 apply 每条非 undone 的 annotation（`apply_annotation`，纯函数）：
   - `mark_*`：更新对应块的 status 字段
   - `trim`/`restore`：切换 `status.is_trimmed`
   - `insert_*`：在指定位置插入新块（payload.inserted_block_id）
   - `rename_chapter`：更新章节块的 `primary.name`
   - `merge_blocks`：保留主块，被合并块标记 `is_trimmed=true` + `supplements.merged_into`
   - `split_block`：在 split_timestamp 拆分块为两个（新块 ID = 原 ID + `_split`）
   - `move_supplement`：素材块时间戳跟随新父块 + `supplements.parent_block_id`
4. 过滤掉 `is_trimmed=True` 的块（trim 是"软删除"，merge 视图默认排除；L4 agent 如需查看可读原始 timeline）

**缺失 annotations.json 时**：返回原始 timeline（finalized=false）。

### 18.8 决策记录表 D044-D053

| 决策 ID | 决策内容 |
|---------|---------|
| D044 | 数据模型：annotations.json 承载所有变更，timeline.json 只读不修改；L4 agent 读时 merge |
| D045 | annotation 结构：扁平 action 枚举型 `{id, target_block_id, action, payload, created_at, undone}` |
| D046 | action 枚举 13 个：move_supplement 替代原 reorder（素材附属关系调整，非时序重排） |
| D047 | 40% 辅助约束不强制，只统计用于诊断（改多了说明 L1/L2 需改进） |
| D048 | 音频裁剪执行 D034：移除 L0 自动裁剪，mic.wav 完整保留，裁剪移到 L3 交互式调参 |
| D049 | STT 重跑：L3 编辑器内置专门界面（试音 + 阈值滑块 + 切片预览 + 应用配置触发 STT） |
| D050 | 完整 undo/redo 栈：undo 标记 undone=true，redo 取消；保存时过滤 undone |
| D051 | 插入位置灵活：supplement（附属）/ after（之后）/ timestamp（指定时间戳）/ 拖动释放 |
| D052 | 启动方式：独立进程 + CLI `python -m workspace.recorder.tools.editor` + 录制器保存后自动启动（D032） |
| D053 | 提交按钮改为"标记就绪"：annotations.json 写 finalized=true，L4 agent 检查此字段 |

### 18.9 模块结构

```
workspace/recorder/tools/editor/              # L3 编辑器 GUI + CLI 入口（独立进程）
├── __main__.py                     # CLI 入口：python -m workspace.recorder.tools.editor <package_path>
├── editor_app.py                   # run_editor 启动流程 + load_timeline
├── editor_window.py                # EditorWindow 主窗口（三区域 + 工具栏 11 项 + F1 帮助）
├── audio_panel.py                  # AudioPanel 音频调参面板（D049）
├── stt_runner.py                   # STTRunner 后台线程（QThread，STT 重跑）
└── widgets/
    ├── chapter_list.py             # ChapterList 章节列表
    ├── timeline_view.py            # TimelineView 时间轴块列表（异步缩略图）
    ├── detail_panel.py             # DetailPanel 详情（截图+chips+折叠原始数据）
    ├── screenshot_viewer.py        # ScreenshotViewer 截图查看器（缩放/平移/前后帧）
    ├── insert_block_dialog.py      # InsertBlockDialog 插入块类型选择对话框
    └── shortcuts_help_dialog.py    # ShortcutsHelpDialog F1 快捷键帮助对话框

lib/recorder/editor/                # L3 annotation 引擎 + merge 逻辑（公用库，L4 也用）
├── __init__.py
├── annotation.py                   # Action 枚举(17, 含 unmark/untrim) + Annotation dataclass + ID 生成
├── store.py                        # AnnotationStore（读写 + undo/redo + finalize）
├── apply.py                        # apply_annotation（纯函数，apply 单条 annotation 到块列表）
├── merge.py                        # merge_timeline_annotations + is_finalized（L4 agent 入口）
├── editor_app.py                   # run_editor + load_timeline（启动逻辑）
└── audio_preview.py                # preview_trim（切片预览，复用 audio_trimmer RMS 逻辑）
```

## 十九、L4 消费层

L4 消费层（Ticket 29-33）已落地。本章节介绍 agent 如何发现和消费录制包：consumer 公用库 API、多轮 VL 协议、场景路由表、L4 决策记录。

> **D055 变更（2026-07-23）**：移除后端 HTTP 端点（原 `server/recordings.py` 的 8 个 `/recordings/*` 端点），agent 直接 import consumer 公用库读本地文件。

### 19.1 consumer 公用库 API

位置：`workspace/recorder/consumer/`，agent 直接 import 使用。录制包是本地文件，agent 同进程直接读，无需 HTTP 端点。

```python
from workspace.recorder.consumer import (
    # 发现录制包
    list_recordings,              # 列出所有录制包摘要（按 created_at 倒序）
    get_recording_summary,        # 单个录制包摘要
    # 读内容
    get_merged_view,              # timeline + annotations merge 后的最终视图
    get_block_detail,             # 单个块详情（supplements 路径已解析为绝对路径）
    get_chapters,                 # 章节列表（从 merged view 过滤 chapter 块）
    # 多轮 VL 协议辅助
    select_vl_candidates,         # 选 VL 候选帧（focus=all/key/anomaly/automatable/trimmed）
    build_vl_question,            # 按块类型生成 VL question 模板
    # 消费日志
    log_consumption,              # 记录 agent 消费行为到 consumption.log（JSONL）
    read_consumption_log,         # 读消费日志
    # 总索引
    regenerate_manifest,          # 重新生成 manifest.json
    read_manifest,                # 读 manifest.json
    # 数据结构
    RecordingSummary,             # 录制包摘要 dataclass
    VLCandidate,                  # VL 候选帧 dataclass
)

# 典型用法
summaries = list_recordings()  # 发现所有录制包
view = get_merged_view("workspace/recorder/recordings/rec_xxx")  # 读最终视图
candidates = select_vl_candidates(view, focus="anomaly",
                                   package_path="workspace/recorder/recordings/rec_xxx")
for cand in candidates:
    vl_result = understand_image(image=cand.frame_path, question=cand.suggested_question)
    log_consumption("workspace/recorder/recordings/rec_xxx", "vl_call",
                    {"block_id": cand.block_id, "frame": cand.frame_path})
```

### 19.2 多轮 VL 协议（D057）

**协议三阶段**：

1. **Round 1 建立全局认知**：调 `get_merged_view(package_path)` 读 merge 后的 timeline，扫描所有块建立全局认知。无需 VL，纯文本阅读。

2. **Round 2 选关键帧 + 调 VL**：
   ```python
   candidates = select_vl_candidates(view, focus="anomaly", package_path=package_path)
   for cand in candidates:
       question = cand.suggested_question + " 上下文：用户报告登录按钮无反应"
       vl_result = understand_image(image=cand.frame_path, question=question)
       log_consumption(package_path, "vl_call",
                       {"block_id": cand.block_id, "frame": cand.frame_path, "question": question})
   ```

3. **Round 3 收敛**：读 VL 返回结果，若需深入再调 VL（更细的问题），通常 2-3 轮收敛。

**VL 调用统计**（D054：无硬限制 + 统计预警）：
- `log_consumption` 记录每次 VL 调用元信息（不记录 VL 返回内容，避免录制包膨胀）
- 推荐上限：单次消费 15-30 次 VL（spec 文档建议，不 enforce）
- 事后调 `read_consumption_log(package_path)` 统计本次消费的 VL 次数

**不预存 VL 结果**（D005）：VL 返回结果只在 agent 会话内存中，不写入录制包。`consumption.log` 只记录调用元信息。

### 19.3 场景路由

L4 在 `server/agent_guide.py` 的 GUIDE_REGISTRY 注册两个 task_type（D013 不新增 skill，只注册路由）。

> **D056 变更（2026-07-23）**：移除 `routes_to` 字段，agent 消费录制包后自主决定后续路由（不预设）。

| task_type | 触发场景 | first_action |
|-----------|---------|--------------|
| `recording.discover` | 用户说"看一下最近的录制"/"发现录制" | 调 `list_recordings()` 列出录制包 |
| `recording.consume` | 用户说"分析录制"/"处理录制 bug"/"蒸馏录制" | 调 `get_merged_view` + agent 自主决定后续 + 多轮 VL 协议 |

**agent 自主决定后续场景**（不预设 routes_to，agent 根据录制包内容判断）：

| 用户指令 | agent 行为 | 可能路由 skill |
|----------|-----------|------------|
| "看一下 xx 记录，处理问题" | 读 merged view + 选异常帧调 VL + 诊断 | `dev.triage`（agent 自主判断） |
| "看一下 xx 记录，尝试生成 skill" | 读 merged view + 提炼方法论 | `daily.cangjie_extraction`（agent 自主判断） |
| "看一下 xx 记录，生成自动化脚本" | 读 merged view + 选 automatable 块调 VL + 生成脚本 | `dev.implement`（agent 自主判断） |
| "看一下 xx 记录，总结一下" | 读 merged view + 输出文字总结 | 直接总结，不路由 skill |

### 19.4 L4 决策记录

| 决策 ID | 内容 | 状态 |
|---------|------|------|
| D054 | VL 调用配额 — 无硬限制 + 统计预警（consumption.log 记录次数，不阻止） | 已确认 |
| D055 | 录制包 agent 发现机制 — agent 直接读本地文件 + consumer 公用库（**移除后端 HTTP 端点**） | 已确认（变更） |
| D056 | L4 代码范围 — 文档 + consumer 公用库 + agent_guide 场景路由（**移除后端端点，移除 routes_to**） | 已确认（变更） |
| D057 | 多轮 VL 协议 — 文档 + 辅助函数（不预存结果，符合 D005） | 已确认 |

**复用的已有决策**：
- D005：VL 不实时，agent 消费时按需调用（L4 延续，不预存 VL 结果）
- D009：录制包不压缩，本地直读（L4 延续）
- D013：不新增 skill，agent 直接读录制包文件（L4 延续，只注册 agent_guide task_type）
- D019：公用库 lib/ + tools/ 分层（L4 延续，consumer 放 workspace/recorder/consumer/）
- D044：annotations.json 承载所有变更，timeline.json 只读（L4 延续，get_merged_view 调 merge）
- D053：提交按钮标记就绪（L4 延续，manifest.json 的 finalized 字段读 annotations.json）

### 19.5 录制包格式新增文件

L4 在录制包根目录新增 3 个文件（详见 `temp/planning_archive/sdd/recording-feature/06-recording-format.md` 第七章）：

- `meta.json` 新增 `format_version` 字段（当前值 `"1.0"`，由 `regenerate_manifest` 自动补写）
- `manifest.json` 总索引（扫描所有文件 + 统计 + stt_status，agent 一次读全包）
- `consumption.log` 消费日志（JSONL，记录 agent 消费行为 + VL 调用统计）

## 二十、待实现功能

L0 采集层 + L1 处理层 + L2 时间轴层 + L3 编辑层 + L4 消费层已全部落地。

L0/L1 当前未实现的功能（留待未来 ticket）：
- ~~暂停录制（传感器暂停状态保存复杂度高）~~ **已实现（v2 重构 D026/D033）**——5 状态机支持 pause/resume/save/discard，AudioSensor 追加模式保证暂停后音频连续
- 标记重点（需要在 events.jsonl 写 marker 事件，侵入 RecordingController）
- 运行时大/小模式切换热键
- 麦克风运行时开关（AudioSensor 不支持运行时切换，下次录制时按 config 生效）
- 多屏拼接截图（fullscreen 当前等同于截主屏，多屏拼接留给 L1）
- OCR 差异检测（spec 第三节 Strategy 3，性能开销大属 VL 范畴，留 L1）
- ~~L1 处理层（STT/事件聚合/关键帧抽取）~~ **已实现（Ticket 14-18，D034-D038）**——P4 事件聚合器 + P5 关键帧抽取器 + P1 STT 转换器 + P2/P3 接口定义全部落地
- ~~L1 自动音频裁剪~~ **已移除（D034）**——mic.wav 完整保留，裁剪移到 L3 编辑器交互式调参
- ~~L4 消费层~~ **已实现（Ticket 29-33，D054-D057）**——consumer 公用库 + 多轮 VL 协议辅助函数 + 场景路由全部落地（D055 变更：移除后端端点，agent 直接读本地文件；D056 变更：移除 routes_to，agent 自主决定后续路由）
