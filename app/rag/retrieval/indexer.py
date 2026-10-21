"""indexer.py - RAG 链路的生产向量索引模块（bge-m3 嵌入 + pgvector 存储）

作用：
    把 Chunk 变成语义向量并存入向量库，供语义检索。
    嵌入用生产级的 bge-m3（本地加载权重，离线推理，无需联网）；
    存储默认用 PostgreSQL + pgvector（生产），无可用 Postgres 时降级到
    内存向量库做本地开发 / 测试（零数据库依赖）。

嵌入（阶段 B 落地，替代阶段 A 的 TF-IDF 占位）：
    BgeEmbedder：bge-m3 稠密向量，维度 1024。
    阶段 A 的 TF-IDF 占位嵌入已废弃——它没有语义能力，只是为跑通接口而存在。

存储后端（呼应原则③ 适配预留）：
    - PgVectorStore（生产，需 PostgreSQL + pgvector 扩展）
    - MemoryVectorStore（本地开发 / 测试，无需数据库）
    dim 必须与 embedder 输出一致（bge-m3 = 1024），否则写入 pgvector 会报维度错。

混合召回：
    BM25 关键词召回见 retriever.py（Bm25Retriever），与本条语义通道互补，
    上层用 RRF 融合即可（尚未在此实现，作为下一步）。
"""

import os

# Windows 默认不支持符号链接，huggingface_hub 每次加载权重都会打一条降级警告。
# 在真正 import sentence_transformers 之前关掉它（HF 会自动改用文件复制，功能不受影响）。
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from app.contracts import DocRef
from app.rag.retrieval.pgvector_store import PgVectorStore

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
ENV_EMBEDDING_MODEL = "BGE_EMBEDDING_MODEL"


class BgeEmbedder:
    """生产级嵌入：bge-m3 稠密向量（维度 1024）。

    首次使用时从 HuggingFace / ModelScope 拉取模型权重（约 2GB+），之后离线推理。
    延迟 import sentence_transformers，未安装时不在模块加载期报错。

    环境变量：
        BGE_EMBEDDING_MODEL  嵌入模型名，默认 BAAI/bge-m3
                             （变量名取自 .env.example「本地小模型」一节，C 的 infra 约定）
    """

    DIM = 1024

    def __init__(self, model_name=None):
        # 维度写死：模型固定输出 1024 维。留 dim 参数会让调用方写出 dim=512 这种
        # 与真实向量不符的值——后果是相似度只按前 512 个分量算（错），
        # 或 pgvector 按 VECTOR(512) 建表后插 1024 维直接报错。
        self.dim = self.DIM
        # 显式传参优先；未传则读环境变量，最后回退到默认模型名。
        # 用 `or` 而不是 os.environ.get(name, default)：第二参只在键缺失时生效，
        # 键存在但值是空串会返回空串，于是 "BGE_EMBEDDING_MODEL="（空）会让
        # SentenceTransformer("") 报一个看不出原因的错。
        if model_name is None:
            model_name = os.environ.get(ENV_EMBEDDING_MODEL) or DEFAULT_EMBEDDING_MODEL
        self.model_name = model_name
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from app.rag import preflight

            # 必须在 import sentence_transformers **之前**确定离线与否：
            # huggingface_hub 在 import 期就把 HF_HUB_OFFLINE 快照进
            # constants.HF_HUB_OFFLINE，之后再改 os.environ 不会生效
            # （constants 仍是旧值）。放在 import 之后 = 这个开关形同虚设。
            preflight.configure_hf_offline(self.model_name)

            from sentence_transformers import SentenceTransformer

            # 加载前检查 + 无条件报一行进度：权重缺失时立刻报错（而不是回落去下载），
            # 正常情况下也让人知道"卡在哪一步、还要等多久"。
            preflight.ensure_model_ready(self.model_name, ENV_EMBEDDING_MODEL, "嵌入模型")
            preflight.announce(self.model_name, "嵌入模型")
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed(self, text):
        """text -> 长度为 dim 的稠密向量（list[float]）。"""
        model = self._ensure_model()
        vec = model.encode(text, normalize_embeddings=True)
        result = []
        for x in vec:
            result.append(float(x))
        return result

    def embed_many(self, texts,batch_size=32):
        """批量编码：一次把一批文本变成向量列表（list[list[float]]）。

        sentence-transformers 的 model.encode(list) 内部按 batch_size 批量推理，
        比逐条 embed 快很多——索引 N 个 chunk 时尤其明显。
        """
        model = self._ensure_model()
        vecs = model.encode(texts, normalize_embeddings=True, batch_size=batch_size)
        # result = []
        # for vec in vecs:
        #     row = []
        #     for x in vec:
        #         row.append(float(x))
        #     result.append(row)
        return vecs.tolist()   #vecs.tolist() 替代双层循环，快 10 倍  tolist() 返回的 float 是 Python 原生 float，效果和循环一致


class MemoryVectorStore:
    """内存向量库（本地开发 / 测试用，无需数据库）。

    生产实现见 pgvector_store.PgVectorStore（需 PostgreSQL + pgvector）。
    """

    def __init__(self, dim):
        self.dim = dim
        self.vectors = []
        self.chunks = []

    def add(self, chunk, vector, idx=None):
        # idx 对内存库没有用（chunk 按 append 顺序存、直接随结果返回），
        # 保留是为了和 PgVectorStore.add 签名一致，index_chunks 才能统一调用。
        self.chunks.append(chunk)
        self.vectors.append(vector)

    def commit(self):
        # 内存库无需提交，仅为了保持与 PgVectorStore 接口一致。
        pass

    def search(self, query_vec, top_k=5, doc_id=None):
        """余弦相似度取 top_k，返回 [(Chunk, 相似度)]，相似度越大越相关。

        直接返回 Chunk 本身而不是下标，与 PgVectorStore 保持一致：
        pgvector 在「只查询」的进程里没有进程内 chunk 列表，
        上层若靠 store.chunks[idx] 回查会越界（实测 IndexError）。

        doc_id 非空时只保留属于该文档的切片，与 PgVectorStore 行为对齐：
        多文档并存的内存库测试里，向量召回也只针对当前文档，不会串。
        """
        scored = []
        for i in range(len(self.vectors)):
            if doc_id is not None and self.chunks[i].doc_id != doc_id:
                continue
            sim = self._cosine(query_vec, self.vectors[i])
            scored.append((self.chunks[i], sim))
        return sorted(scored, key=lambda item: item[1], reverse=True)[:top_k]

    def _cosine(self, a, b):
        # 入库与查询向量均已归一化，点积即余弦。
        # 用 zip 而不是 range(self.dim)：维度万一不一致时按短的截断，
        # 至少不会 IndexError（实际入库/查询走同一个 embedder，维度天然一致）。
        dot = 0.0
        for x, y in zip(a, b):
            dot = dot + x * y
        return dot


def index_chunks(chunks, dsn=None, embedder=None):
    """构建生产索引：bge-m3 嵌入 + 向量库。返回 (store, embedder)。

    dsn 为空 -> 降级到 MemoryVectorStore（本地开发 / 测试，无需数据库）；
    dsn 非空 -> 写入 PostgreSQL + pgvector（生产）。
    """
    if embedder is None:
        embedder = BgeEmbedder()
    # 两个后端 add 签名一致，循环不用再分叉
    store = PgVectorStore(dsn, embedder.dim) if dsn else MemoryVectorStore(embedder.dim)
    if dsn:
        store.init_schema()
        # 先按 doc_id 删净再写。ON CONFLICT (doc_id, chunk_idx) 只能覆盖「已存在」的下标，
        # 文档改短后多出来的旧行（93 片 → 80 片，第 80~92 行）既不会被更新也不会被删，
        # 会一直留在库里被检索到 —— 这是残留脏数据的来源。
        # 语料级路径见 corpus.py（那里还多一层指纹比对，未变就整个跳过）。
        for doc_id in sorted({c.doc_id for c in chunks}):
            store.delete_by_doc(doc_id)
    # 批量编码一次，再循环写入，避免逐条 encode 的低效
    texts = [c.text for c in chunks]
    if hasattr(embedder, "embed_many"):
        vectors = embedder.embed_many(texts)
    else:
        vectors = [embedder.embed(t) for t in texts]
    for idx, c in enumerate(chunks):
        store.add(c, vectors[idx], idx)
    # 统一提交：add 已不再各自 commit，这里收尾一次，
    # 多文档大批量时省去逐行提交开销（PgVectorStore 才是真提交，
    # MemoryVectorStore.commit 是空操作）。
    store.commit()
    return store, embedder


def search_index(store, embedder, query, top_k=5, doc_id=None):
    """用索引查询，返回 [(chunk, score, ref)]。

    store 无论是 PgVectorStore 还是 MemoryVectorStore，search 都返回
    [(Chunk, 分数)]，分数都是余弦相似度（越大越相似、同尺度可比）。
    因此这里统一处理，不再依赖 store.chunks[idx] 回查——
    pgvector 在「只查询」的进程里本就没有那份列表。

    doc_id 透传给向量库：多文档并存时让向量召回只针对当前文档。
    """
    qvec = embedder.embed(query)
    hits = store.search(qvec, top_k, doc_id=doc_id)
    results = []
    for chunk, score in hits:
        ref = DocRef(
            doc_id=chunk.doc_id,
            page=chunk.page,
            section_path=" > ".join(chunk.section_path),
            snippet=chunk.text[:200],
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            # 与 BM25 通道一致：几何高亮三件套取自 Chunk。
            # 向量通道同样要能画高亮（RRF 融合后胜出的常是这条路），
            # 只补 retriever 会漏掉这里。
            bboxes=chunk.bboxes,
            page_width=chunk.page_width,
            page_height=chunk.page_height,
            doc_title=chunk.doc_title,
            # 提取方式随切片带上来（向量通道）。与 BM25 通道同口径，
            # 漏了这里就会出现「同一条证据，走不同通道标签不一样」。
            extraction_method=getattr(chunk, "extraction_method", None),
        )
        results.append((chunk, score, ref))
    return results


if __name__ == "__main__":
    from app.rag.ingest.loader import extract_blocks, _default_pdf_paths
    from app.rag.ingest.toc import build_tree
    from app.rag.ingest.chunker import build_chunks

    # 生产环境设 PG_DSN 走 pgvector；不设则走内存库（本地调试）。
    # 默认语料发现走 data/corpus/raw/（见 _default_pdf_paths），不再写死单份。
    dsn = os.environ.get("PG_DSN")
    paths = _default_pdf_paths()
    if len(paths) == 0:
        print("找不到 PDF：默认去 data/corpus/raw/ 下找，请把语料放到那里，或运行时传入路径")
    else:
        for pdf_path in paths:
            doc_id = os.path.splitext(os.path.basename(pdf_path))[0]
            blocks = extract_blocks(pdf_path)
            sections = build_tree(pdf_path)
            chunks = build_chunks(blocks, sections, doc_id)
            store, embedder = index_chunks(chunks, dsn=dsn)
            print("文件:", pdf_path, "doc_id:", doc_id)
            print("构建索引：", len(chunks), "个 Chunk，向量维度", embedder.dim,
                  "（后端：", "pgvector" if dsn else "内存库", "）")
            query = "检索模块怎么设计"
            print("查询：", query)
            hits = search_index(store, embedder, query, top_k=5)
            for chunk, score, ref in hits:
                pp = ref.section_path or ""
                print("  分数", round(score, 4), "| 页码", ref.page, "| 章节", pp)
