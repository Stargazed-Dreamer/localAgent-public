"""本地 ONNX 嵌入引擎

使用 onnxruntime + tokenizers 进行本地向量嵌入推理，
无需 API key，无需 PyTorch，与 PaddlePaddle 无冲突。

模型：BAAI/bge-small-zh-v1.5（中文优化，512 维，~90MB）
"""

import gc
import hashlib
import logging
import os
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingEngine:
    """本地 ONNX 嵌入引擎"""

    def __init__(self, model_name: str = "BAAI/bge-small-zh-v1.5",
                     cache_dir: str = "weights/embeddings",
                     hf_mirror: str = ""):
            """
            初始化Embedding模型的实例，用于文本嵌入任务。
            参数：
                model_name (str): 模型名称，指定要加载的预训练模型，默认为"BAAI/bge-small-zh-v1.5"。
                cache_dir (str): 模型权重和缓存文件的存储目录，默认为"weights/embeddings"。
                hf_mirror (str): Hugging Face镜像地址，用于加速模型下载，默认为空字符串表示不使用镜像。
            返回值：
                无返回值，直接初始化实例属性以准备模型加载和推理。
            """
            self.model_name = model_name
            self.cache_dir = cache_dir
            # 构建模型本地目录路径：容错两种命名约定
            #   ① org_model（如 "BAAI_bge-small-zh-v1.5"，model_name.replace("/", "_")）
            #   ② model（如 "bge-small-zh-v1.5"，去掉 org 前缀，HF snapshot 常见结构）
            # 优先用配置的 model_name 派生，不存在则尝试去掉 org 前缀，避免硬编码路径
            _candidates = [
                os.path.join(cache_dir, model_name.replace("/", "_")),
                os.path.join(cache_dir, model_name.split("/")[-1]),
            ]
            self.model_dir = _candidates[0]
            for _c in _candidates:
                if os.path.isdir(_c):
                    self.model_dir = _c
                    break
            self.dim: int = 512  # 设置模型输出的嵌入向量维度
            self.max_length: int = 512  # 设置模型处理的最大输入序列长度
            # onnxruntime InferenceSession / tokenizers.Tokenizer / sentence_transformers
            # .SentenceTransformer 的 typing stub 不全，用 Any 承载（推理侧已有判空）
            self._session: Any = None  # 初始化模型推理会话为空，待后续加载
            self._tokenizer: Any = None  # 初始化文本分词器为空，待后续加载
            self._ready = False  # 标记模型是否已完全加载并准备好推理
            self._download_attempted = False  # 标记是否已尝试下载模型，避免重复尝试
            self._hf_mirror = hf_mirror  # 存储Hugging Face镜像地址，用于模型下载优化
            self._st_model: Any = None  # sentence-transformers 降级模型（预初始化避免 hasattr 检查）
            self._use_st_fallback = False  # 是否使用 sentence-transformers 降级路径

    @property
    def ready(self) -> bool:
        return self._ready

    def initialize(self) -> bool:
        """加载模型和分词器，返回是否成功"""
        if self._ready:
            return True

        try:
            return self._load_model()
        except Exception as e:
            logger.warning(f"嵌入引擎加载失败: {e}，语义检索将不可用")
            return False

    def unload(self) -> None:
        """释放模型资源（design §5.2，model-lifecycle-manager T3）

        - 置空 ONNX session / tokenizer / ST 模型
        - `_ready=False`（消费者先查 `ready`，自动降级到 BM25-only / 返回零向量）
        - **重置 `_use_st_fallback`**：避免卸载后再次初始化误走降级路径
          （_load_model 路径由 _has_local_st_structure 重判，重置后语义干净）
        - **不重置 `_download_attempted`**：避免每次 unload/load 循环重复触发下载
        - gc.collect() 触发 ONNX session 立即回收
        """
        self._session = None
        self._tokenizer = None
        self._st_model = None
        self._use_st_fallback = False
        self._ready = False
        gc.collect()
        logger.info(f"嵌入引擎已卸载: {self.model_name}")

    def _load_model(self) -> bool:
        """尝试加载模型：ONNX 优先，其次本地权重转 ONNX，最后才联网下载

        加载优先级（2026-08-31 修订）：
            1. 本地已有 model.onnx                → 直接加载
            2. 本地有 sentence-transformers 结构   → 先试 ST 降级；失败则本地权重导 ONNX（均不联网）
            3. 以上都不行                          → 联网下载

        历史 bug：原实现在分支 2 无条件 `return self._ready`，一旦运行环境未安装
        sentence-transformers，降级失败后直接返回 False，永远走不到分支 3 与
        torch→ONNX 导出路径，记忆系统静默退化为 BM25-only 且不报错。
        """
        onnx_path = os.path.join(self.model_dir, "model.onnx")
        tokenizer_path = os.path.join(self.model_dir, "tokenizer.json")

        # 本地有 ONNX：直接加载
        if os.path.exists(onnx_path) and os.path.exists(tokenizer_path):
            return self._load_onnx(onnx_path, tokenizer_path)

        # 本地有 sentence-transformers 结构（safetensors/pytorch + 1_Pooling + modules.json）：
        # 不联网，先试 ST 降级，失败再用本地权重导出 ONNX
        if self._has_local_st_structure():
            logger.info(
                f"本地模型 {self.model_dir} 非 ONNX 格式，先尝试 sentence-transformers 降级加载"
            )
            self._setup_sentence_transformers_fallback()
            if self._ready:
                return True
            # 降级不可用时不能就此放弃——本地权重仍在，可离线转 ONNX
            logger.info(
                "sentence-transformers 不可用，改为从本地权重导出 ONNX（不联网）"
            )
            self._export_onnx(fallback_to_st=False)
            if os.path.exists(onnx_path) and os.path.exists(tokenizer_path):
                return self._load_onnx(onnx_path, tokenizer_path)

        # 本地既无 ONNX 也无可用权重结构：尝试下载
        if not self._download_attempted:
            self._download_attempted = True
            logger.info(f"嵌入模型未找到，尝试下载 {self.model_name}...")
            if self._download_model():
                return self._load_model()
        logger.warning("嵌入模型不可用，将使用 BM25-only 模式")
        return False

    def _has_local_st_structure(self) -> bool:
        """检测本地 model_dir 是否有 sentence-transformers 结构（非 ONNX 但可直接加载）

        判据：有权重文件（model.safetensors 或 pytorch_model.bin）+ 1_Pooling/ + modules.json
        """
        if not os.path.isdir(self.model_dir):
            return False
        has_weights = any(
            os.path.exists(os.path.join(self.model_dir, w))
            for w in ("model.safetensors", "pytorch_model.bin")
        )
        has_pooling = os.path.isdir(os.path.join(self.model_dir, "1_Pooling"))
        has_modules = os.path.exists(os.path.join(self.model_dir, "modules.json"))
        return has_weights and has_pooling and has_modules

    def _load_onnx(self, onnx_path: str, tokenizer_path: str) -> bool:
        """加载 ONNX 模型 + 分词器 + 配置"""
        config_path = os.path.join(self.model_dir, "config.json")

        # 加载 ONNX 模型
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(onnx_path, opts, providers=["CPUExecutionProvider"])

        # 加载分词器
        from tokenizers import Tokenizer
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        self._tokenizer.enable_truncation(max_length=self.max_length)
        self._tokenizer.enable_padding(length=self.max_length)

        # 读取配置
        if os.path.exists(config_path):
            import json
            with open(config_path) as f:
                cfg = json.load(f)
            self.dim = cfg.get("hidden_size", 512)
            self.max_length = cfg.get("max_position_embeddings", 512)

        self._ready = True
        logger.info(f"嵌入引擎就绪: {self.model_name}, dim={self.dim}")
        return True

    def _download_model(self) -> bool:
        """从 HuggingFace 下载模型文件（支持镜像源和 SSL 跳过）"""
        try:
            from huggingface_hub import snapshot_download
            os.makedirs(self.model_dir, exist_ok=True)
            logger.info(f"正在下载嵌入模型 {self.model_name}，这可能需要几分钟...")

            # 支持 HF 镜像源（中国用户）
            if self._hf_mirror:
                os.environ["HF_ENDPOINT"] = self._hf_mirror
            download_kwargs = {
                "repo_id": self.model_name,
                "local_dir": self.model_dir,
                # 2026-08-31 修订：原 allow_patterns 只有 "model.onnx" + tokenizer 配置。
                # 但 BAAI/bge-small-zh-v1.5 官方仓库（HF 与 ModelScope 均无 ONNX 变体）
                # 只有 model.safetensors / pytorch_model.bin，导致三重失效：
                #   ① "model.onnx" 匹配不到任何文件；
                #   ② 即便下载成功本地也没有权重 → _export_onnx 的 from_pretrained 必失败；
                #   ③ 缺 1_Pooling/ 与 modules.json → sentence-transformers 降级同样缺件。
                # 现按三类补齐：ONNX（含 onnx/ 子目录）、权重、ST 结构与词表。
                "allow_patterns": [
                    # ONNX：仓库若提供则取（bge-small-zh-v1.5 实际没有，兼容其他模型）
                    "model.onnx", "onnx/*",
                    # tokenizer 与配置（vocab.txt 是 BERT 中文分词必需）
                    "tokenizer.json", "tokenizer_config.json", "vocab.txt",
                    "config.json", "special_tokens_map.json",
                    # 权重：导出 ONNX 必需
                    "model.safetensors", "pytorch_model.bin",
                    # sentence-transformers 结构：ST 降级路径必需
                    "modules.json", "config_sentence_transformers.json",
                    "sentence_bert_config.json", "1_Pooling/*",
                ],
            }

            try:
                snapshot_download(**download_kwargs)
            except Exception as ssl_err:
                if "SSL" in str(ssl_err) or "certificate" in str(ssl_err).lower():
                    logger.info("SSL 验证失败，尝试跳过 SSL 验证重试...")
                    _saved_env = {}
                    for _k in ("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE"):
                        _saved_env[_k] = os.environ.get(_k)
                        os.environ[_k] = ""
                    try:
                        snapshot_download(**download_kwargs)
                    finally:
                        for _k, _v in _saved_env.items():
                            if _v is None:
                                os.environ.pop(_k, None)
                            else:
                                os.environ[_k] = _v
                else:
                    # 未配镜像源时，自动回退到 hf-mirror.com 重试一次（中国用户友好）
                    if not self._hf_mirror:
                        logger.warning(f"嵌入模型从默认源下载失败 ({ssl_err})，自动尝试 hf-mirror.com 镜像源...")
                        _orig_endpoint = os.environ.get("HF_ENDPOINT")
                        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
                        try:
                            snapshot_download(**download_kwargs)
                            logger.warning("嵌入模型已从镜像源 hf-mirror.com 下载成功。建议在 config.toml [memory] 段显式配置 hf_mirror 以避免每次启动重试。")
                        finally:
                            if _orig_endpoint is None:
                                os.environ.pop("HF_ENDPOINT", None)
                            else:
                                os.environ["HF_ENDPOINT"] = _orig_endpoint
                    else:
                        raise

            onnx_path = os.path.join(self.model_dir, "model.onnx")

            # 部分仓库把 ONNX 放在 onnx/ 子目录（onnx/model.onnx），提到 model_dir 根，
            # 否则 _load_model 永远找不到它
            nested_onnx = os.path.join(self.model_dir, "onnx", "model.onnx")
            if not os.path.exists(onnx_path) and os.path.exists(nested_onnx):
                os.replace(nested_onnx, onnx_path)
                logger.info("已从 onnx/ 子目录提取 model.onnx")

            # 检查 ONNX 模型是否存在，如果不存在则尝试转换
            if not os.path.exists(onnx_path):
                logger.info("ONNX 模型不存在，尝试从 PyTorch 转换...")
                self._export_onnx()

            # 导出失败时 _export_onnx 可能已通过 ST 降级把 _ready 置 True，此时也算成功
            return os.path.exists(onnx_path) or self._ready
        except Exception as e:
            logger.warning(f"模型下载失败: {e}")
            return False

    def _export_onnx(self, fallback_to_st: bool = True) -> None:
        """从 PyTorch 模型导出 ONNX（需要 torch + transformers，纯本地不联网）

        fallback_to_st: 导出失败时是否回退 sentence-transformers。
        _load_model 的分支 2 调用时传 False——ST 已在那里试过并失败，不应重试。
        """
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            onnx_path = os.path.join(self.model_dir, "model.onnx")
            tokenizer = AutoTokenizer.from_pretrained(self.model_dir)
            model = AutoModel.from_pretrained(self.model_dir)
            model.eval()

            # 创建 dummy 输入
            dummy = tokenizer("测试", return_tensors="pt", padding="max_length",
                              max_length=self.max_length, truncation=True)

            torch.onnx.export(
                model,
                (dummy["input_ids"], dummy["attention_mask"], dummy.get("token_type_ids", torch.zeros_like(dummy["input_ids"]))),
                onnx_path,
                input_names=["input_ids", "attention_mask", "token_type_ids"],
                output_names=["last_hidden_state", "pooler_output"],
                dynamic_axes={
                    "input_ids": {0: "batch"},
                    "attention_mask": {0: "batch"},
                    "token_type_ids": {0: "batch"},
                    "last_hidden_state": {0: "batch"},
                    "pooler_output": {0: "batch"},
                },
                opset_version=14,
                dynamo=False,  # torch 2.9+ 默认 dynamo=True 需 onnxscript，用旧引擎避免额外依赖
            )
            logger.info(f"ONNX 模型已导出: {onnx_path}")
        except Exception as e:
            logger.warning(f"ONNX 导出失败: {e}")
            if fallback_to_st:
                logger.info("将尝试使用 sentence-transformers 直接推理")
                self._setup_sentence_transformers_fallback()

    def _setup_sentence_transformers_fallback(self) -> None:
        """设置 sentence-transformers 降级方案

        优先加载本地 model_dir（已通过 _has_local_st_structure 验证），
        避免用 model_name + cache_folder 触发重复下载。
        """
        try:
            from sentence_transformers import SentenceTransformer
            # 优先用本地 model_dir（非 ONNX 但已是 sentence-transformers 结构）
            if os.path.isdir(self.model_dir) and os.path.exists(
                os.path.join(self.model_dir, "modules.json")
            ):
                self._st_model = SentenceTransformer(self.model_dir)
            else:
                self._st_model = SentenceTransformer(
                    self.model_name, cache_folder=self.cache_dir
                )
            self._use_st_fallback = True
            self._ready = True
            # 从模型读取真实向量维度（替代默认 512）
            # sentence-transformers 5.x: get_sentence_embedding_dimension → get_embedding_dimension
            if hasattr(self._st_model, "get_embedding_dimension"):
                self.dim = self._st_model.get_embedding_dimension()
            else:
                self.dim = self._st_model.get_sentence_embedding_dimension()
            logger.info(
                f"使用 sentence-transformers 降级模式: {self.model_dir}, dim={self.dim}"
            )
        except Exception as e:
            logger.warning(f"sentence-transformers 降级也失败: {e}")
            self._use_st_fallback = False
            self._st_model = None

    def embed(self, texts: list[str]) -> np.ndarray:
        """生成文本嵌入向量

        Args:
            texts: 文本列表

        Returns:
            numpy 数组，shape=(len(texts), dim)
        """
        if not self._ready:
            return np.zeros((len(texts), self.dim), dtype=np.float32)

        # sentence-transformers 降级路径
        if self._use_st_fallback:
            return self._embed_st(texts)

        return self._embed_onnx(texts)

    def _embed_onnx(self, texts: list[str]) -> np.ndarray:
        """ONNX Runtime 推理路径"""
        # 局部快照（design §5.2 竞态微修）：unload 在推理期间清空 _session 时
        # 仍持有引用完成本次推理，避免 AttributeError 上抛 → search 500。
        # 快照为 None（unload 已发生但 _ready 检查与快照之间存在窗口）→ 返回零向量降级。
        session = self._session
        tokenizer = self._tokenizer
        if session is None or tokenizer is None:
            return np.zeros((len(texts), self.dim), dtype=np.float32)
        # 分词
        encoded = tokenizer.encode_batch(texts)
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        # 检查模型是否需要 token_type_ids
        input_names = {inp.name for inp in session.get_inputs()}
        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in input_names:
            inputs["token_type_ids"] = np.zeros_like(input_ids)

        # 推理
        outputs = session.run(None, inputs)

        # Mean pooling + 归一化
        last_hidden = outputs[0]  # (batch, seq_len, dim)
        mask_expanded = attention_mask[:, :, np.newaxis].astype(np.float32)
        sum_embeddings = np.sum(last_hidden * mask_expanded, axis=1)
        sum_mask = np.clip(mask_expanded.sum(axis=1), a_min=1e-9, a_max=None)
        embeddings = sum_embeddings / sum_mask

        # L2 归一化
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.clip(norms, a_min=1e-9, a_max=None)
        embeddings = embeddings / norms

        return embeddings.astype(np.float32)

    def _embed_st(self, texts: list[str]) -> np.ndarray:
        """sentence-transformers 降级路径"""
        # 局部快照（design §5.2 竞态微修，同 _embed_onnx）
        st = self._st_model
        if st is None:
            return np.zeros((len(texts), self.dim), dtype=np.float32)
        embeddings = st.encode(texts, normalize_embeddings=True)
        return embeddings.astype(np.float32)

    def text_hash(self, text: str) -> str:
        """计算文本哈希，用于向量索引去重"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def vector_to_blob(self, vector: np.ndarray) -> bytes:
        """向量序列化为 BLOB"""
        return vector.astype(np.float32).tobytes()

    def blob_to_vector(self, blob: bytes) -> np.ndarray:
        """BLOB 反序列化为向量"""
        return np.frombuffer(blob, dtype=np.float32)
