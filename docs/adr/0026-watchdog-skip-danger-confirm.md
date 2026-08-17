# watchdog 模式跳过 danger=confirm

watchdog 模式自动跳过 danger=confirm 动作（不弹窗），与"危险操作必须人工确认"铁律冲突（batch 5）。决策：保持跳过。watchdog 授权即同意 confirm 范围内的动作——watchdog 模式是用户授权后的自动执行模式，danger=confirm 动作（删除文件/关闭窗口）在授权范围内，弹窗确认会破坏 watchdog 的自动执行体验。
