# SpaceSniffer .sns 快照格式（逆向笔记）

> 逆向验证基线：本机 C 盘快照（67,676,974 字节，852,241 条记录 = 1 卷 + 706,340 文件 + 145,898 目录 + 2 伪记录），
> 纯结构遍历指针恰好走到文件尾、栈恰好清空、0 违例；另对拍 GitHub 参考
> [jerrylususu/SpaceSniffer_Format](https://github.com/jerrylususu/SpaceSniffer_Format) 的 608 字节示例
> （该仓库框架正确但字段宽度有误：称"大小 16 字节/占用 8 字节"，实测 8+4，以本文为准）。

## 记录布局（little-endian，顺序流式）

| 记录内偏移 | 长度 | 含义 |
|---|---|---|
| +0 | 1 | 固定魔数 `0x02` |
| +1 | 1 | 类型：`01`=卷(根) `02`=文件 `03`=目录 `04`=可用空间 `05`=未知空间 |
| +2 | 4 | uint32：Base64 文件名长度 len |
| +6 | len | Base64(ASCII) 文件名（不含路径） |
| +6+len | 8 | int64 逻辑大小（字节）。**目录 = 子树文件逻辑大小之和**；卷 = 卷已用空间（来自卷统计，含 NTFS 元数据，比顶层求和大 ~4GiB，差额是 $MFT 等） |
| …+8 | 4 | uint32 疑似簇尾浪费（文件 = 4096 − logical mod 4096，整簇为 0；目录无明确语义） |
| …+12 | 4 | uint32 Windows FILE_ATTRIBUTE（文件常见 0x20=ARCHIVE；目录 = 0） |
| …+16 | 24 | 3×int64 FILETIME（创建/访问/修改，顺序未细究；目录 = 全 0） |
| …+40 | 2 | `00 00` 当前记录结束标志 |
| …+42 | 2×k | `01 00` × k：弹出最近 k 个已打开层级 |

## 层级：栈语义（不是缩进、不保证字典序）

- 每条记录（含文件）逻辑入栈；记录尾部的 `01 00`×k 从栈顶弹出 k 层
- 文件/伪记录（04/05）：第 1 个 pop 关闭自己，多余的关闭祖先
- 目录：有子项 → 自身记录 pops=0（由最后一个子项记录的额外 pop 关闭）；空目录 → 自身 pops=1
- **记录顺序不保证字典序**，层级只能靠弹出标记恢复

## 名称编码

- Base64，解码后按 `utf-8 → gbk → utf-16-le → latin-1(replace)` 尝试（本机实测中文为 GBK）
- Base64 中可能嵌入 `\r\n`（MIME 换行）；`base64.b64decode()` 默认 `validate=False` 自动丢弃

## 伪记录（卷的直接子项）

- `04` = 可用空间（卷剩余，**不要**计入已用量）
- `05` = 未知（尚未扫描）空间（=0 说明扫描完整）

## 尺寸口径与对账

- 已用空间以**卷记录**的 logical 为准（含 NTFS 元数据）
- 校验：顶层目录求和 + 根级文件 + NTFS 元数据 ≈ 卷记录值
- 文件大小是逻辑字节（非簇占用），对清理决策无影响

## 快照体积特征

百万级文件的整盘快照 ≈ 67MB（约为盘内容 0.06%），可长期留档做清理前后对比。

## 与 scan_disk 缓存的字段映射

| .sns | scan_disk 缓存 |
|---|---|
| 卷/目录记录 | node（complete=True，mtime=None） |
| 文件记录 | 父节点 `files_top[]` 条目 `{name, size, mtime}`（mtime 格式 `YYYY-MM-DD HH:MM`） |
| 卷名 `C:\` | `scan_meta.path = "C:"`（归一化，--path 过滤用） |
| 04/05 伪记录 | `scan_meta.volume.free_bytes / unknown_bytes`（不进树，避免污染 find/caches） |
| 卷 logical | `scan_meta.volume.used_bytes` |
| —（快照无） | `files/dirs` 子树计数由导入器后序计算 |

导入器：`scripts/parse_sns.py`；缓存写入 `temp/disk_scan_cache/sns_*.json`（`sns_` 前缀避免与 live scan 缓存混用 slug 匹配）。
