"""reranker.py - 融合后的候选重排（bge-reranker-v2-m3 交叉编码器，本地推理）

为什么需要 reranker：
    BM25 + 向量 RRF 融合按“排名”融合，量纲被绕开但语义精度有限；交叉编码器把
    (query, doc) 拼在一起过一次 transformer，给出更准的相关性分数，能在送入 LLM
    前把真正相关的几段挑出来，减少噪声上下文、提升答案忠实度（题面 F15）。

为什么用 bge-reranker-v2-m3：
    和检索用的 bge-m3 同家族，sentence_transformers 的 CrossEncoder 直接支持，
    不引入新依赖；权重本地加载，离线推理。首次使用会从 HF / HF 镜像拉取（约 1.x GB）。

环境变量：
    BGE_RERANKER_MODEL  重排模型名，默认 BAAI/bge-reranker-v2-m3
                        （变量名取自 .env.example「本地小模型」一节，C 的 infra 约定）
"""

import os

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
ENV_MODEL = "BGE_RERANKER_MODEL"


class BgeReranker:
    """本地交叉编码器重排器。"""

    def __init__(self, model_name=None):
        # 显式传参优先；未传则读环境变量，最后回退到默认模型名。
        # 与 llm.py 的读法保持一致（不在 import 期读环境变量，构造时才读）。
        # 用 `or` 而不是 os.environ.get(name, default)：第二参只在键缺失时生效，
        # 键存在但值是空串会返回空串，于是 "BGE_RERANKER_MODEL="（空）会让
        # CrossEncoder("") 报 Repo id must be in the form ...，很难看出是空值造成的。
        if model_name is None:
            model_name = os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.model_name = model_name
        self._model = None

    def _ensure(self):
        if self._model is None:
            from app.rag import preflight

            # 必须在 import sentence_transformers 之前：huggingface_hub 在
            # import 期把 HF_HUB_OFFLINE 快照进 constants.HF_HUB_OFFLINE，
            # 之后再改 os.environ 不生效（见 indexer 同处注释）。
            preflight.configure_hf_offline(self.model_name)

            from sentence_transformers import CrossEncoder

            # 加载前检查 + 无条件报一行进度，理由同 indexer：这个环境里
            # "权重缺失正在下载 2GB"和"正在正常加载"在终端上都是没有输出。
            preflight.ensure_model_ready(self.model_name, ENV_MODEL, "重排模型")
            preflight.announce(self.model_name, "重排模型")
            self._model = CrossEncoder(self.model_name)
        return self._model

    def score(self, query, docs):
        """返回与 docs 等长的分数列表（越大越相关）。docs 为空直接返回空列表。"""
        if len(docs) == 0:
            return []
        model = self._ensure()
        pairs = []
        for d in docs:
            pairs.append((query, d))
        raw = model.predict(pairs)
        result = []
        for s in raw:
            result.append(float(s))
        return result

    def rerank(self, query, docs, top_n=None):
        """返回按分数降序的 (index, score) 列表；index 是 docs 中的原始下标。

        top_n 非空时只保留前 top_n 个。
        """
        scores = self.score(query, docs)
        indexed = []
        for i, s in enumerate(scores):
            indexed.append((i, s))
        indexed.sort(key=lambda item: item[1], reverse=True)
        if top_n is not None and len(indexed) > top_n:
            indexed = indexed[:top_n]
        return indexed
