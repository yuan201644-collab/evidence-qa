"""corpus.py - 语料级索引：动态发现 → 变化检测 → 增量重建 → 清孤儿

为什么单独一个模块：
    index_chunks 是「拿到一批切片就编码入库」的单文档动作；本模块管的是
    「整个语料目录怎么索引」——扫哪些文件、哪些变了要重建、哪些删了要清掉。
    两件事混在一起，index_chunks 会越滚越大，而且「哪些该重建」的判断也没法单测
    （见 plan_corpus，纯函数，不碰数据库）。

⚠️ 先纠正一个容易误解的点：这里的「跳过」跳过的是什么
    BM25 索引是**进程内**的，进程退出就没了，所以每次启动都必须拿到全部切片的文本
    才能重建它 → **每次都要解析全部 PDF**。doc_meta 省掉的是 bge-m3 编码
    （全链路最慢的一步），不是文件解析。别把 skip 理解成「不读文件」。

适配「以后放不同文档」（本模块存在的理由）：
    一切以「这次扫目录的结果」为准，不写死任何文件名或数量：
    - 新增文档   → doc_meta 里没有 → 索引
    - 内容改过   → 指纹不一致 → 先按 doc_id 删净再重建（根治旧行残留）
    - 文档被删   → 库里还有、语料里没了 → 连切片带元数据一起清掉（清孤儿）
    - 索引格式改 → _INDEX_VERSION + 1 → 全部指纹失效 → 自动全量重建
      （改 Chunk 字段或切片逻辑必须 bump，否则老行缺列会被当成「没变」留着）
"""

import hashlib
import os
from dataclasses import dataclass, field

from app.rag.ingest import ocr
from app.rag.ingest.chunker import build_chunks
from app.rag.ingest.loader import discover_corpus_files, extract_blocks
from app.rag.ingest.toc import build_tree
from app.rag.retrieval.indexer import BgeEmbedder, MemoryVectorStore
from app.rag.retrieval.pgvector_store import PgVectorStore
from app.rag.retrieval.retriever import Bm25Retriever

# 索引格式版本。改了 Chunk 字段、切片规则、DocRef 取材方式等「入库内容的结构」
# 就要 +1 —— 它的作用是让所有文档的指纹一起失效，强制全量重建。
#
# ⚠️ 光靠指纹不够：指纹只对**切片文本**算哈希，而 section_path / bbox / 页尺寸
# 这些也是入库内容。凡是只改这些、不改文字的改动（例如把「标题之前」的占位章节名
# 改成空列表），指纹前后一致 → 会被判成「没变」→ 老库里仍是旧值。
# 所以这类改动必须靠 bump 版本号来强制重建。
#
# 变更记录：
#   1  初版
#   2  section_path 允许为空（去掉占位串「该block在第一个标题之前」）；
#      MAX_CHUNK_CHARS 240 → 480。两者都会改变入库的 section_path / 切片边界。
#   3  纯文本（md/txt）的 bbox 改用行号伪坐标，修掉「章节栈被一次压满、
#      所有切片都归到最后一章」的错位（实测 KB-010.md 全片归到 ['5 版本变更']）；
#      同时不再把退化（零面积）框收进 Chunk.bboxes，md/txt 的 bboxes 变为空。
#      两者都只改入库字段、不改切片文本，指纹发现不了，必须 bump。
#   4  章节标题统一空白归一（全角空格 → 半角）。内嵌书签那条路径原先直接用
#      书签原文，KB-005（DOCX，唯一带书签的语料）产出 `第二章　数据分级`，
#      与用例写的 `第二章 数据分级` 字面不等，导致 rag-e-02 检索命中却被判未命中。
#      同样只改 section_path，不改切片文本。
#   5  Chunk 新增 extraction_method（ocr / text_layer / plain），pgvector 建表与
#      写入同步加列。旧索引里该列为空，引用卡片会显示不出来源，所以要重建。
#   6  乱序文档按正文条款编号回归到对应章节；OCR 中文枚举条款按冒号前字段名
#      生成章节锚点。两者均改变 section_path，但不改变正文文本，必须 bump。
_INDEX_VERSION = 6


@dataclass
class CorpusIndex:
    """一次语料索引的结果。

    前半是给上层用的（bm25 / store / embedder 拿去建检索链路），
    后半是给人看的（indexed / skipped / pruned / ignored，排查「我放的文件进没进」）。
    """

    bm25: Bm25Retriever
    store: object
    embedder: object
    chunks: list = field(default_factory=list)
    indexed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    pruned: list = field(default_factory=list)
    ignored: list = field(default_factory=list)
    empty: list = field(default_factory=list)
    backend: str = "memory"
    #: 本次索引期间的 OCR 统计（见 ocr.stats()）。放这里是为了让
    #: 「扫描件到底走没走 OCR」在一行摘要里看得见，不必靠切片数反推。
    ocr: dict = field(default_factory=dict)
    #: 每份文档本轮解析的事实，供 answer trace 展示；不入库，不影响索引版本。
    parse_info: dict = field(default_factory=dict)

    def summary(self):
        """一句话摘要，供 __main__ / 日志打印。"""
        total = len(self.indexed) + len(self.skipped)
        line = (
            "后端=" + self.backend
            + " | 文档 " + str(total)
            + "（重建 " + str(len(self.indexed)) + " / 跳过 " + str(len(self.skipped)) + "）"
            + " | 切片 " + str(len(self.chunks))
        )
        if len(self.pruned) > 0:
            line += " | 清掉已不在语料里的 " + str(len(self.pruned)) + " 份：" + "、".join(self.pruned)
        if len(self.ignored) > 0:
            line += " | 跳过非 PDF " + str(len(self.ignored)) + " 个：" + "、".join(self.ignored)
        line += self._ocr_summary()
        if len(self.empty) > 0:
            # 必须显式报出来：这类文档在检索里永远不会被命中，但不报就是静默失效。
            # 最常见的原因是扫描件没有文本层（manifest 里 has_text_layer=false）。
            # 这里把 OCR 的可用性一起说明，否则"0 切片"会被误读成"OCR 没生效"，
            # 而真实原因可能是这台机器压根没装 OCR 依赖。
            line += (
                " | ⚠️ 切不出任何切片的文档 " + str(len(self.empty)) + " 份（检索永远命中不到）："
                + "、".join(self.empty)
                + "；" + self._empty_hint()
            )
        return line

    def _ocr_summary(self):
        """OCR 段的摘要：跑了就报页数与耗时，没跑就报为什么没跑。"""
        info = self.ocr or {}
        if not info:
            return ""
        if not info.get("enabled", True):
            return " | OCR 已关闭（OCR_ENABLED=0）"
        if not info.get("available", False):
            return " | OCR 不可用（未装 rapidocr_onnxruntime）"
        ran = info.get("pages_ocr", 0)
        cached = info.get("pages_cached", 0)
        if ran == 0 and cached == 0:
            return " | OCR 可用但本次没有需要处理的扫描页"
        secs = info.get("seconds", 0.0)
        # 缓存命中按「次」计数：同一页会被 extract_blocks 与 extract_lines 各读一次，
        # 打印成「页」会让人以为页数对不上（单页文档出现 3 次命中）。
        text = " | OCR 本次识别 " + str(ran) + " 页（缓存命中 " + str(cached) + " 次）"
        if ran > 0:
            text += "，耗时 " + str(round(secs, 1)) + " 秒（约 " + str(round(secs / ran, 1)) + " 秒/页）"
        return text

    def _empty_hint(self):
        """给「0 切片」一份可执行的下一步，而不是一句笼统的"需 OCR"。"""
        info = self.ocr or {}
        if not info.get("available", False):
            return "这批文档多为无文本层的扫描件，当前未装 OCR 依赖，装上后才会进检索池"
        if info.get("pages_ocr", 0) > 0 or info.get("pages_cached", 0) > 0:
            return "OCR 已跑过但仍切不出文字，需要看这几份是不是空白页或版式异常"
        return "通常是扫描件无文本层，需要 OCR"


def discover_corpus(paths=None):
    """发现语料，返回 (docs, ignored)。

    paths=None  → 动态扫目录（loader.discover_corpus_files）：不写死文件名、不写死数量，
                  以后往目录里丢新 PDF、删掉旧 PDF、换一批语料都会自动反映
    paths=[...] → 只用给定的这几份（局部重建 / 单测）

    docs    = [(doc_id, path)]，按文件名排序；doc_id 取文件名去扩展名（沿用既有约定）
    ignored = 被跳过的非 PDF 文件名（目前只支持 PDF，列出来免得「文件明明放进去了
              却没进检索池」变成悬案）
    """
    if paths is None:
        pdfs, ignored = discover_corpus_files()
    else:
        pdfs, ignored = list(paths), []
    docs = []
    for p in pdfs:
        docs.append((os.path.splitext(os.path.basename(p))[0], p))
    return docs, ignored


def _document_parse_info(path, blocks, ocr_before, ocr_after):
    """汇总单份文档的解析事实；只读 OCR 缓存，不会再次触发 OCR。"""
    methods = sorted({block.extraction_method for block in blocks if block.extraction_method})
    pages_ocr = max(0, ocr_after.get("pages_ocr", 0) - ocr_before.get("pages_ocr", 0))
    cache_hits = max(0, ocr_after.get("pages_cached", 0) - ocr_before.get("pages_cached", 0))
    ocr_blocks = [block for block in blocks if block.extraction_method == "ocr"]
    info = {
        "extraction_methods": methods,
        "block_count": len(blocks),
        "ocr_block_count": len(ocr_blocks),
    }
    if not ocr_blocks and pages_ocr == 0 and cache_hits == 0:
        return info

    scores = []
    for page_no in sorted({block.page for block in ocr_blocks}):
        score = ocr.average_score(path, None, page_no)
        if score is not None:
            scores.append(score)
    info["ocr"] = {
        "pages_ocr": pages_ocr,
        "cache_hits": cache_hits,
        "elapsed_ms": round(
            max(0.0, ocr_after.get("seconds", 0.0) - ocr_before.get("seconds", 0.0)) * 1000.0,
            1,
        ),
        "average_score": round(sum(scores) / len(scores), 4) if scores else None,
    }
    return info


def _corpus_source_label(document_count, formats):
    """语料库的展示名，如「文档语料库（15 份 · 4 种格式）」。

    为什么要把份数与格式写进标签：光说「文档语料库」跟原先的「业务库表」一样
    等于没说。份数说明规模、格式说明覆盖面 —— 赛题要求「不少于 10 份涵盖不同
    格式」，这两个数正好是那条要求的直接证据。
    """
    if not document_count:
        return "文档语料库"
    label = "文档语料库（" + str(document_count) + " 份"
    if formats:
        label += " · " + str(len(formats)) + " 种格式"
    return label + "）"


def corpus_data_source(corpus_index):
    """把语料索引转成 `app.contracts.DataSource`（供界面标「答案来自哪一批材料」）。

    索引拿不到时返回 None —— 绝不回落成"未知语料库"之类的假值，
    那会让界面显示一个并不存在的来源（口径同 `SchemaCatalog.data_source`）。

    **格式不能从 doc_id 推**：`discover_corpus` 把 doc_id 取成文件名去扩展名
    （`corpus.py:160`），实测 `indexed` 里是 `KB-001_智能计算设备年度维保服务合同`
    这种没有后缀的串。早先按 `doc_id.rsplit('.')` 取格式，真索引上恒为空列表，
    标签就退化成「15 份」而说不出覆盖面。所以改为回问真实语料目录。
    """
    if corpus_index is None:
        return None
    from app.contracts import DataSource

    indexed = list(getattr(corpus_index, "indexed", None) or [])
    skipped = list(getattr(corpus_index, "skipped", None) or [])
    doc_ids = indexed + [doc for doc in skipped if doc not in indexed]
    if not doc_ids:
        return None
    formats = _corpus_formats()
    return DataSource(
        kind="corpus",
        label=_corpus_source_label(len(doc_ids), formats),
        document_count=len(doc_ids),
        formats=formats,
    )


def _corpus_formats():
    """语料目录里实际出现的文件格式（小写、去重排序）。

    扫目录而不是读索引：格式是"这批材料覆盖了哪些类型"，属语料目录的事实，
    与某一份文档这次是重建还是跳过无关。复用 `loader.discover_corpus_files()`
    而不是自己拼路径 —— 它本来就负责"哪些文件算语料"（支持的后缀、跳过哪些），
    在这儿重写一遍就会在两处各有一份格式清单，迟早对不上。
    读不到就返回空列表，标签退化成只报份数，**不报错的格式**。
    """
    try:
        from app.rag.ingest import loader

        paths, _ignored = loader.discover_corpus_files()
    except Exception:
        return []
    return sorted({
        os.path.splitext(path)[1].lstrip(".").lower()
        for path in paths
        if os.path.splitext(path)[1]
    })


def content_fingerprint(chunks):
    """算内容指纹：对「这次真正要入库的切片文本」算 sha256。

    为什么不用文件字节 sha256：PDF 每次另存 / 复制都可能改内嵌时间戳，
    字节 hash 会没来由地变，触发整份重建。
    为什么不用 mtime：复制、git checkout、解压都会改 mtime，同样会误判。

    指纹里带 _INDEX_VERSION，且切片之间补一个分隔符 ——
    否则 ["ab", "c"] 和 ["a", "bc"] 会算出同一个指纹（拼起来都是 "abc"）。
    """
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.text.encode("utf-8"))
        h.update(b"\n")
    return "v" + str(_INDEX_VERSION) + ":" + h.hexdigest()


def plan_corpus(doc_chunks, meta, row_counts, prune=False):
    """决定每份文档「重建还是跳过」，并算出要清的孤儿。纯函数，不碰数据库，可直接单测。

    doc_chunks: {doc_id: [Chunk]}        本次扫到的语料
    meta:       {doc_id: (指纹, 切片数)}   doc_meta 里记录的
    row_counts: {doc_id: 行数}            chunk_vectors 里实际的行数
    prune:      是否清孤儿。默认目录全量发现时为 True；
                显式只传了几份 paths 时必须 False，否则会把没传进来的文档全删了

    返回 (pending, skipped, orphans)：
      pending  [(doc_id, [Chunk], 指纹)]  要重建：首次 / 内容变 / 行数对不上
      skipped  [doc_id]                   指纹与行数都一致，跳过编码
      orphans  [doc_id]                   库里有、当前语料里已经没有的

    行数也要对得上才算「没变」：指纹一致但行数不符（表被截断、上次写到一半崩了）
    就跳过的话，会得到一个「以为有索引、其实查不到」的空壳。
    ⚠️ 行数比较必须用 get(doc_id, 0)：切不出任何切片的文档在 chunk_vectors 里
    一行都没有，用 None 去比 0 会永远不相等 —— 那份文档就会被每轮都重建一次。
    """
    pending = []
    skipped = []
    for doc_id in sorted(doc_chunks):
        chunks = doc_chunks[doc_id]
        fp = content_fingerprint(chunks)
        recorded = meta.get(doc_id)
        unchanged = (
            recorded is not None
            and recorded[0] == fp
            and recorded[1] == len(chunks)
            and row_counts.get(doc_id, 0) == len(chunks)
        )
        if unchanged:
            skipped.append(doc_id)
        else:
            pending.append((doc_id, chunks, fp))

    orphans = []
    if prune:
        known = set(doc_chunks)
        orphans = sorted((set(meta) | set(row_counts)) - known)
    return pending, skipped, orphans


def validate_read_only_index(doc_chunks, meta, row_counts):
    """校验冻结索引恰好对应当前语料；只返回跳过项，不允许静默补建或清理。"""
    expected_doc_ids = set(doc_chunks)
    indexed_doc_ids = set(meta) | set(row_counts)
    if indexed_doc_ids != expected_doc_ids:
        raise RuntimeError("只读语料索引与冻结文档集合不一致")
    pending, skipped, orphans = plan_corpus(doc_chunks, meta, row_counts, prune=False)
    if pending or orphans:
        raise RuntimeError("冻结向量索引与当前语料/切片指纹不一致；只读模式拒绝重建")
    return skipped


def index_corpus(paths=None, dsn=None, embedder=None, *, read_only=False):
    """建 / 更新整个语料的索引，返回 CorpusIndex。

    每次调用都：动态发现语料 → 逐份切片 → 建一个装全部文档的 BM25 →
    对「变了或没索引过的」文档做「删净 + 编码 + 写入」→ 清掉已不在语料里的孤儿。

    dsn 为空 → MemoryVectorStore：进程内、退出即丢，doc_meta 无从谈起，
               所以**每份文档每轮都重新编码**（本地开发 / 测试的代价，无增量）。
    dsn 非空 → PgVectorStore：靠 doc_meta 指纹比对，没变的文档跳过 bge-m3 编码。

    返回的 bm25 装的是**全部文档**的切片；要限定在某一份文档检索，
    把 doc_id 传给 HybridRetriever.search / Bm25Retriever.retrieve（两路都会过滤）。
    """
    docs, ignored = discover_corpus(paths)
    if embedder is None:
        embedder = BgeEmbedder()

    # 每轮索引单独统计 OCR（否则反复建索引时页数会累加，耗时分母失真）
    ocr.reset_stats()

    # 逐份切片。全部文档都要做 —— BM25 每次都得重建（见模块开头说明）。
    doc_chunks = {}
    parse_info = {}
    for doc_id, path in docs:
        ocr_before = ocr.stats()
        blocks = extract_blocks(path)
        sections = build_tree(path)
        doc_chunks[doc_id] = build_chunks(blocks, sections, doc_id)
        parse_info[doc_id] = _document_parse_info(path, blocks, ocr_before, ocr.stats())

    all_chunks = []
    for doc_id, _ in docs:
        all_chunks.extend(doc_chunks[doc_id])
    bm25 = Bm25Retriever(all_chunks)

    if read_only and not dsn:
        raise ValueError("只读语料索引必须连接已经冻结的 PostgreSQL 向量库")
    store = (
        PgVectorStore(dsn, embedder.dim, read_only=read_only)
        if dsn else MemoryVectorStore(embedder.dim)
    )
    if dsn:
        if not read_only:
            store.init_schema()
        meta = store.load_meta()
        row_counts = store.count_chunks_by_doc()
        # 只有「按默认目录全量发现」时才敢清孤儿；显式传 paths 说明这次只想管这几份，
        # 清孤儿会把其他文档误删。
        prune = paths is None and not read_only
    else:
        meta, row_counts, prune = {}, {}, False

    if read_only:
        skipped = validate_read_only_index(doc_chunks, meta, row_counts)
        pending, orphans = [], []
    else:
        pending, skipped, orphans = plan_corpus(doc_chunks, meta, row_counts, prune=prune)

    if len(pending) > 0:
        # 一次批量编码（比逐份 encode 快），写入时再按文档切分
        texts = []
        for doc_id, chunks, fp in pending:
            for c in chunks:
                texts.append(c.text)
        if hasattr(embedder, "embed_many"):
            vectors = embedder.embed_many(texts)
        else:
            vectors = [embedder.embed(t) for t in texts]

        cursor = 0
        for doc_id, chunks, fp in pending:
            if dsn:
                # 先删净再写：ON CONFLICT 只覆盖「已存在」的下标，
                # 文档改短后多出来的旧行删不掉（这正是残留脏数据的来源）。
                store.delete_by_doc(doc_id)
            for idx, c in enumerate(chunks):
                store.add(c, vectors[cursor + idx], idx)
            cursor += len(chunks)
            if dsn:
                store.save_meta(doc_id, fp, len(chunks))

    if len(orphans) > 0:
        store.delete_docs(orphans)

    if not read_only:
        store.commit()
    return CorpusIndex(
        bm25=bm25,
        store=store,
        embedder=embedder,
        chunks=all_chunks,
        indexed=[doc_id for doc_id, _, _ in pending],
        skipped=skipped,
        pruned=orphans,
        ignored=ignored,
        # 切不出切片的文档单独列出来（扫描件没文本层时就是这样），
        # 不列出来「我放的文件检索永远命中不到」会变成静默失效，见 summary()。
        empty=sorted(doc_id for doc_id, cs in doc_chunks.items() if len(cs) == 0),
        backend="pgvector" if dsn else "memory",
        ocr=ocr.stats(),
        parse_info=parse_info,
    )


if __name__ == "__main__":
    from app.rag.retrieval.indexer import search_index

    idx = index_corpus(dsn=os.environ.get("PG_DSN"))
    print(idx.summary())
    print()
    # 不传 doc_id = 全语料检索（编排层的 doc_search 要的就是这个语义）
    query = "质保期是多长时间"
    print("全语料检索：", query)
    for chunk, score, ref in search_index(idx.store, idx.embedder, query, top_k=5):
        print("  分数", round(score, 4), "|", ref.doc_title, "| 页码", ref.page)
