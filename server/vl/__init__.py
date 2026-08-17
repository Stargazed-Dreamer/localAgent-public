"""视觉语言模型（VL）基础设施包

归集远程 VL 调用与视觉路由：
- remote_vl: 远程 VL 客户端（多 provider failover + 429 处理）
- vision: VL 路由（远程 VL 图像理解 + 元素定位）
- feedback_vl: 操作后 VL 视觉反馈
"""
