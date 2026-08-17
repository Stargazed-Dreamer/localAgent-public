# 模型路径管理

## 设计目标

所有 AI 模型路径通过 `config.toml` 的 `[models]` 段统一管理，避免硬编码绝对路径。
发布时只需改一个配置项（`external_dir = ""`），所有模块自动回退到项目内 `weights/` 目录，
无需搜索替换。

## 配置方式

### 个人部署（开发环境）

```toml
# config.toml
[models]
external_dir = "<data_drive>:\\ai_models"
```

各模块自动使用以下子目录：

| 模块 | 路径 | 代码位置 |
|------|------|----------|
| PaddleOCR | `<data_drive>:\ai_models\paddleocr` | `server/ocr.py` |
| 嵌入模型 | `<data_drive>:\ai_models\embeddings` | `server/memory/config.py` → `server/memory/embeddings.py` |
| Whisper STT | `<data_drive>:\ai_models\faster_whisper` | 非本项目，仅共享存储 |
| CosyVoice TTS | `<data_drive>:\ai_models\cosyvoice` | 非本项目，仅共享存储 |
| HF 缓存 | `<data_drive>:\ai_models\huggingface` | 环境变量 `HF_HOME` |
| ModelScope 缓存 | `<data_drive>:\ai_models\modelscope` | 环境变量 `MODELSCOPE_CACHE` |

### 发布模式

```toml
# config.toml
[models]
external_dir = ""
```

各模块回退到项目内 `weights/` 目录：

| 模块 | 回退路径 |
|------|----------|
| PaddleOCR | `weights/paddlex` |
| 嵌入模型 | `weights/embeddings` |

## 核心函数

```python
from server.config import get_models_config

cfg = get_models_config()
# cfg["paddleocr_dir"]   -> "<data_drive>:\\ai_models\\paddleocr" 或 "weights/paddlex"
# cfg["embeddings_dir"]  -> "<data_drive>:\\ai_models\\embeddings" 或 "weights/embeddings"
```

## 调用关系

```
config.toml [models].external_dir
    │
    ├─► get_models_config()  (server/config.py)
    │       │
    │       ├─► server/ocr.py        → paddleocr_dir
    │       └─► server/memory/config.py → embeddings_dir
    │               │
    │               └─► MemoryConfig.embedding_cache_dir
    │                       │
    │                       └─► EmbeddingEngine(cache_dir=...)  (server/memory/embeddings.py)
    │
    └─► 环境变量 (start.bat 设置)
            ├─► HF_HOME = <data_drive>:\ai_models\huggingface
            ├─► MODELSCOPE_CACHE = <data_drive>:\ai_models\modelscope
            └─► PADDLEX_HOME = <data_drive>:\ai_models\paddleocr (冗余，代码已用 get_models_config)
```

## 新增模型引用时的规范

1. **禁止硬编码绝对路径**（如 `r"<data_drive>:\ai_models\xxx"`）
2. 在 `get_models_config()` 中添加新的子目录映射
3. 在调用处用 `get_models_config()["xxx_dir"]` 获取路径
4. 更新本文档的表格
5. 更新 `config.example.toml` 的注释

## 环境变量与配置的关系

`start.bat` 设置的环境变量（`HF_HOME`、`MODELSCOPE_CACHE`）用于第三方库的缓存重定向，
与 `get_models_config()` 是互补关系：

- **环境变量**：控制第三方库（HuggingFace/ModelScope SDK）的下载缓存位置
- **`get_models_config()`**：控制本项目代码加载模型的路径

两者指向同一目录（`<data_drive>:\ai_models`），确保模型只存一份。
