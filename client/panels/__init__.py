"""client/panels/ — 面板自动发现目录

新增面板只需在本目录添加一个 .py 文件，定义一个 PanelBase 子类并设置 PANEL_META，
PanelRegistry.discover() 会自动扫描注册，无需修改 core/ 任何文件。
"""
