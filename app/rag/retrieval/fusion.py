"""fusion.py - BM25 + 向量双通道的 RRF 融合召回（阶段 B）

为什么需要融合：
    BM25 擅长关键词精确匹配（术语、编号、字段名），向量召回擅长语义近义
    （"怎么设计" 和 "设计方案" 这种同义改写）。单通道都会漏：纯 BM25 抓不到
    同义改写，纯向量会把强关键词信号稀释。两条通道互补，融合才稳。

为什么用 RRF 而不是把分数直接相加：
    BM25 分（可能几十）和余弦相似度（0~1）量纲完全不同，直接相加必然偏科——
    谁的刻度大谁就说了算。RRF（Reciprocal Rank Fusion）只看"排第几"，
    把每个通道的排名换成 1/(k+rank) 再相加，彻底绕开量纲问题，最稳也最常用。

RRF 公式：
    fused_score(d) = sum_{每个通道 c} 1 / (k + rank_c(d))
    rank 从该通道的第 1 名开始（1-based）。k 默认 60：压一压头部、拉一拉长尾，
    避免单通道把个别结果顶得太高。

去重键：
    两个通道命中"同一段原文"时要合并分数、不能出两张重复卡片。键用 A 在
    contracts v0.2 的溯源字段拼成：doc_id + page + section_path + char_start +
    char_end。char 偏移为空时也能靠前三项区分不同段落，不会误并。

阶段 B 后续（当前留白）：
    融合之后再加 reranker 重排（见 retriever.py 阶段说明），本文件只做召回融合。
"""

from app.contracts import DocRef
from app.rag.schemas import Candidate


def _hit_key(ref):
    """用 DocRef 的溯源字段拼融合键，保证两个通道命中同一段原文时归一累加。"""
    return ref.doc_id, ref.page, ref.section_path, ref.char_start, ref.char_end


def _by_score(item):
    """排序键：取三元组里的 fused score。"""
    chunk, ref, score = item
    return score


def rrf_fuse(rankings, k=60, top_k=5):
    """把多个已排序的结果列表融合成按 RRF 分数降序的列表。

    参数：
        rankings: list[list[(chunk, ref, method)]]，每个内层列表按相关性排好
                  （第 0 个 = 该通道第 1 名）。各通道可返回不同长度。
        k:        RRF 平滑常数，默认 60。
        top_k:    最终返回条数。

    返回：
        list[(Candidate, DocRef)]，Candidate.score = RRF 融合分，method = "rrf"。
    """
    fused = {}  #字典，用来存融合结果   键：_hit_key(ref) 算出的五元组，唯一标识一段原文。  值：(chunk, ref, 累计分数) 三元组
    for ranking in rankings:   #遍历每个通道    第 1 轮：ranking 是 BM25 的排序列表    第 2 轮：ranking 是向量的排序列表
        rank = 0
        for item in ranking:  #内层循环：遍历这个通道的每一个结果    第 1 次：item = (chunk_A, ref_A, "bm25")   第 2 次：item = (chunk_B, ref_B, "bm25")
            rank = rank + 1
            chunk, ref, method = item
            key = _hit_key(ref)
            contrib = 1.0 / (k + rank)  #RRF 贡献。第 1 名得 1/61，第 10 名得 1/70
            '''排名越靠前，贡献越大。
            但衰减很慢——第 1 名 0.0164，第 10 名 0.0143，只差 13%。
            这就是 k=60 的作用：压头部、拉长尾，让排名差异不那么悬殊
            '''
            if key in fused:  #判断这段原文是否已经被之前的通道命中过
                old_chunk, old_ref, old_score = fused[key]
                fused[key] = (chunk, ref, old_score + contrib)  #分数要累加
            else:
                fused[key] = (chunk, ref, contrib)

    merged = []
    for key in fused:
        chunk, ref, score = fused[key]
        merged.append((chunk, ref, score))
    merged.sort(key=_by_score, reverse=True)  #把字典转成列表，方便排序  按分数降序排

    results = []
    limit = top_k
    if len(merged) < limit:
        limit = len(merged)
    for i in range(limit):
        chunk, ref, score = merged[i]
        cand = Candidate(chunk=chunk, score=score, method="rrf")
        results.append((cand, ref))  #Candidate 给下游模块（reranker、generator）用    DocRef 给前端引用卡片用
    return results


class HybridRetriever:
    """把 BM25 通道与向量通道接起来，对外只暴露一个 search。

    用法：
        retriever, chunks = build_retriever_from_pdf(path, doc_id)
        store, embedder = index_chunks(chunks)            # 内存库或 pgvector
        hybrid = HybridRetriever(retriever, store, embedder)
        hits = hybrid.search("检索模块怎么设计", top_k=5)
    """

    def __init__(self, bm25_retriever, store, embedder, k=60):
        self.bm25 = bm25_retriever
        self.store = store
        self.embedder = embedder
        self.k = k

    def search(self, query, top_k=5, per_channel=10, doc_id=None):
        """两通道各取 per_channel 条进候选池，RRF 融合后返回 top_k。

        per_channel 故意比 top_k 大：融合要在更大的候选池里做，
        否则单通道 top_k 太小会把互补项提前截断，融合失去意义。

        doc_id 透传给**两个通道**：多文档并存时只召回当前文档的切片，
        避免查 A 串出 B。两路都过滤才算对齐——语料级 BM25（corpus.py 建的
        一个索引装全部文档）里若不传 doc_id，BM25 会跨文档召回，而向量通道
        被限制在当前文档，融合结果就成了两份文档的混合。
        """
        bm25_hits = self.bm25.retrieve(query, top_k=per_channel, doc_id=doc_id)      # [(Candidate, DocRef)]
        vector_hits = search_index(self.store, self.embedder, query, top_k=per_channel, doc_id=doc_id)  # [(chunk, score, ref)]

        bm25_ranking = []
        for cand, ref in bm25_hits:
            bm25_ranking.append((cand.chunk, ref, "bm25"))

        vector_ranking = []
        for chunk, score, ref in vector_hits:
            vector_ranking.append((chunk, ref, "vector"))

        return rrf_fuse([bm25_ranking, vector_ranking], k=self.k, top_k=top_k)


# search_index 在文件末尾才 import，避免与 indexer 顶层互相引用的阅读负担
from app.rag.retrieval.indexer import search_index  # noqa: E402


if __name__ == "__main__":
    import os

    from app.rag.ingest.loader import extract_blocks, _default_pdf_paths
    from app.rag.ingest.toc import build_tree
    from app.rag.ingest.chunker import build_chunks
    from app.rag.retrieval.retriever import build_retriever_from_pdf
    from app.rag.retrieval.indexer import index_chunks

    dsn = os.environ.get("PG_DSN")
    paths = _default_pdf_paths()
    if len(paths) == 0:
        print("找不到 PDF：默认去 data/corpus/raw/ 下找，请把语料放到那里，或运行时传入路径")
    else:
        for pdf_path in paths:
            doc_id = os.path.splitext(os.path.basename(pdf_path))[0]
            retriever, chunks = build_retriever_from_pdf(pdf_path, doc_id)
            store, embedder = index_chunks(chunks, dsn=dsn)
            hybrid = HybridRetriever(retriever, store, embedder)
            print("文件:", pdf_path, "doc_id:", doc_id, "共", len(chunks), "个 Chunk")
            print("融合召回后端：", "pgvector" if dsn else "内存库")
            query = "检索模块怎么设计"
            print("查询：", query)
            hits = hybrid.search(query, top_k=5, doc_id=doc_id)
            print("命中", len(hits), "条（RRF 融合）：")
            for cand, ref in hits:
                pp = ref.section_path or ""
                print("  分数", round(cand.score, 4), "| 页码", ref.page, "| 章节", pp)
                print("    片段:", ref.snippet[:60])
