"""L0 传感器封装

每个传感器对应一个 M 模块：
- base.Sensor: 抽象协议（start / stop）
- mock.MockSensor: 测试用，同步发预设事件
- keyboard.KeyboardSensor: M1 键盘（pynput，Ticket 02）
- mouse.MouseSensor: M1 鼠标（pynput，Ticket 02）
- screen.ScreenCaptureSensor: M2 屏幕（mss + PrintWindow，Ticket 03）
- audio.AudioSensor: M3 音频（sounddevice，Ticket 04）
- window.WindowFocusSensor: M4 窗口焦点（pywin32，Ticket 05）

传感器通过持有 controller 引用，调用 controller.emit_event(kind, payload) 上报事件。
"""
