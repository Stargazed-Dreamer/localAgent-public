# UIA 语义动作豁免焦点校验

UIA 语义动作（invoke/select/expand）通过 UIA 树定位元素 + invoke pattern，不依赖窗口焦点，与 Computer Use 铁律"所有动作强制焦点"冲突（batch 5）。决策：豁免 UIA 语义动作的焦点校验。UIA COM 调用本身隔离了窗口焦点，invoke pattern 直接通过 UIA 树触发元素，不需要 SetForegroundWindow；强制焦点会让后台窗口/托盘图标动作失败。
