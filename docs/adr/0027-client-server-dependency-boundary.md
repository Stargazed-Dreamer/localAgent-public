# Client-Server 模块依赖边界

client/ 有 3 处直接 import server/ 模块（chat.py→server.config Medium，2 处→server.component_manifest Low），影响 release profile 和分层（batch 10 M2 + batch 11 C2）。决策：提取共享逻辑到 `lib/config_reader.py` + `lib/component_manifest.py`，server 和 client 都从 lib 读。物理隔离 + 单一真源；client 自带配置读取副本会代码重复，保持依赖不解决 release profile 问题。
