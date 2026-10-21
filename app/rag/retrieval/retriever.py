"""retriever.py - RAG 链路的召回模块（阶段 A：内存 BM25）

作用：
    给定一个查询，从一批 Chunk 里找出最相关的若干条，
    每条返回 Candidate（含 Chunk、分数、来源）和 DocRef（溯源信息）。

为什么阶段 A 用 BM25 而不是 bge 向量：
    检索闭环要先跑通接口和数据流。BM25 是纯算法、零额外依赖，
    能在不引入 bge / pgvector / Docker 的前提下验证
    「问题 -> 候选 Chunk + Ref」这条主链路。

阶段 B 会替换的部分（当前刻意留白）：
    1) 嵌入换成 bge-m3 本地模型，存储换成 pgvector（见 indexer.py）
    2) 召回改成 BM25 + 向量的 RRF 融合，再加 reranker 重排
    3) 检索从「内存全量扫描」改成「查存储」

可解释性：
    原则②要求可解释性靠代码埋点，不用 LLM 思维链。
    本模块把分词、IDF、打分都做成了可追踪的步骤，方便排查。
"""

import math
import os

from app.rag.schemas import  Candidate
from app.contracts import DocRef
from app.rag.ingest.loader import extract_blocks, _default_pdf_paths
from app.rag.ingest.toc import build_tree
from app.rag.ingest.chunker import build_chunks
from app.rag.tokenizer import tokenize


class Bm25Retriever:   #需要保存 idf、doc_tokens 等状态，多次复用 所以用class
    """内存 BM25 召回器。

    构造时一次性算好 IDF 和平均文档长度，
    retrieve 时对每个 Chunk 算 BM25 分数并取 top_k。
    一个 Chunk 就等于 BM25 算法里的一篇“文档
    """

    def __init__(self, chunks, k1=1.5, b=0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        # 对每个 chunk 做分词，并预存每篇文档的词频 doc_tf（term frequency）
        self.doc_tokens = []
        self.doc_tf = []
        total_len = 0  # 文档总 token 数（与 score_one 的 doc_len 同单位，BM25 长度归一化才正确）
        for c in chunks:
            toks = tokenize(c.text)
            total_len = total_len + len(toks)
            self.doc_tokens.append(toks)  #    chunk.text 切片文字
            tf = {}
            for tok in toks:  #    doc_tf 是词频字典：每个 token 在这篇文档里出现多少次
                tf[tok] = tf.get(tok, 0) + 1
            self.doc_tf.append(tf)
        self.n_docs = len(self.doc_tokens)
        '''
        self.doc_tokens = [
    ["设备", "质保期", "是", "多久"],   # 第 0 篇文档的 tokens  一个 Chunk 就等于 BM25 算法里的一篇“文档
    ["质保期", "三年"],                # 第 1 篇文档的 tokens
    ]
        '''
        # 平均文档长度
        if self.n_docs > 0:
            self.avg_len = total_len / self.n_docs
        else:
            self.avg_len = 0.0
        # 文档频率 df：每个 token 出现在多少篇文档里
        self.df = {}
        for t in self.doc_tokens:
            seen = set()
            for tok in t:  #t是一个chunk 一篇文档 tok是每个文档里的一个词  在一篇文档里 一个词不会多次的加频率 一个词在一篇文档里只会加一次
                if tok not in seen:
                    seen.add(tok)
                    self.df[tok] = self.df.get(tok, 0) + 1

        # 平滑 IDF
        self.idf = {}
        for tok, freq in self.df.items():  #self.df 是之前算好的文档频率字典，结构是  {"质保期": 2, "设备": 1, "是": 1, ...}
            '''
            tok 是词  freq 是这个 tok 出现在多少篇文档里（df）
            '''
            self.idf[tok] = math.log((self.n_docs - freq + 0.5) / (freq + 0.5) + 1)

    def score_one(self, query_tokens, doc_index):  #给定一个 query 和一个文档的索引，算出这个文档对 query 的相关性分数
        """算单个文档对查询的 BM25 分数。"""
        # doc_tf 已在 __init__ 预先算好（self.doc_tf[doc_index]），这里直接查，不再重复统计词频
        doc_tf = self.doc_tf[doc_index]
        doc_len = len(self.doc_tokens[doc_index])
        score = 0.0
        for q in query_tokens:
            if q in doc_tf:
                tf = doc_tf[q]
                idf = self.idf.get(q, 0.0)
                denom = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avg_len)
                score = score + idf * (tf * (self.k1 + 1)) / denom
        return score

    def retrieve(self, query, top_k=5, doc_id=None):
        """返回 top_k 个 Candidate（附带 DocRef 溯源）。

        doc_id 非空时只对该文档的切片打分。语料级 BM25（见 corpus.py，
        一个索引里装着全部文档）必须靠它守住「查 A 不串出 B」；
        单文档索引下传不传都一样。

        注意 IDF 是在**构造时的全部切片**上算的，所以语料级索引过滤后
        分数与「只为这一份文档建的 BM25」不完全相同 —— 这是有意的：
        语料级 IDF 更能反映一个词在整个语料里的稀有度。

        溯源结构统一用 A 在 app.contracts 定义的 DocRef（契约 v0.3）。
        文本级定位：doc_id / page / section_path / snippet / char_start / char_end。
        几何高亮：bboxes + page_width / page_height（直接取自 Chunk，
        chunker 已从 loader 带上来），前端据此在 PDF 上画高亮框。
        """
        query_tokens = tokenize(query)  #把用户问题切成 token。比如 "质保期多久" → ["质保期", "多久"]
        # 算每个 chunk 的分数
        scored = []
        for i in range(self.n_docs):   #对所有文档打分 遍历每个 Chunk（每个 Chunk 就是一篇“文档”）   调 score_one 算它对当前 query 的 BM25 分数   scored 是 [(文档索引, 分数), ...]
            if doc_id is not None and self.chunks[i].doc_id != doc_id:
                continue   #先按文档过滤再截断，不能等取完 top_k 再筛（否则候选会被别的文档占满）
            s = self.score_one(query_tokens, i)  #第i篇文档
            scored.append((i, s))

            # 按分数从高到低排，取前 top_k  score是x[1] 元组的第二份参数  过滤0分
        selected = []
        for idx, s in sorted(scored, key=lambda x: x[1], reverse=True):
            if s <= 0:
                continue
            selected.append((idx, s))
            if len(selected) >= top_k:
                break
        # 组装 Candidate + Ref
        results = []
        for idx, sc in selected:
            chunk = self.chunks[idx]  #取出 Chunk  idx 是文档索引，从 self.chunks 里取出对应的 Chunk 对象
            ref = DocRef(
                doc_id=chunk.doc_id,
                page=chunk.page,
                section_path=" > ".join(chunk.section_path),
                snippet=chunk.text[:200],  #前 200 字，前端引用卡片显示
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                # 几何高亮三件套：bboxes 是框，page_width/page_height 是换算像素的
                # 比例基准（缺了尺寸，光有框前端也算不出位置，见契约 v0.3 说明）。
                # 三者都来自 Chunk，由 chunker 从 loader 的页面尺寸 + block 坐标带上来。
                bboxes=chunk.bboxes,
                page_width=chunk.page_width,
                page_height=chunk.page_height,
                doc_title=chunk.doc_title,   # 引用卡片显示名（doc_id 是内部值，不可读）
                # 提取方式随切片带上来（BM25 通道）。用 getattr 兜底：从向量库/旧索引
                # 反序列化回来的 Chunk 可能没有该字段，缺了就如实留 None。
                extraction_method=getattr(chunk, "extraction_method", None),
            )
            cand = Candidate(
                chunk=chunk,
                score=sc,
                method="bm25",
            )
            results.append((cand, ref))
        return results


def build_retriever_from_pdf(pdf_path, doc_id):
    """从一份 PDF 跑通 loader -> toc -> chunker -> retriever 的便捷函数。"""
    blocks = extract_blocks(pdf_path)
    sections = build_tree(pdf_path)
    chunks = build_chunks(blocks, sections, doc_id)
    return Bm25Retriever(chunks), chunks


if __name__ == "__main__":
    paths = _default_pdf_paths()
    if len(paths) == 0:
        print("找不到 PDF：默认去 data/corpus/raw/ 下找，请把语料放到那里，或运行时传入路径")
    else:
        for path in paths:
            doc_id = os.path.splitext(os.path.basename(path))[0]
            retriever, chunks = build_retriever_from_pdf(path, doc_id)
            print("文件:", path, "doc_id:", doc_id, "共", len(chunks), "个 Chunk")
            query = "我今天应该先做什么"
            print("查询：", query)
            hits = retriever.retrieve(query, top_k=5)
            print("命中", len(hits), "条：")
            for cand, ref in hits:
                pp = ref.section_path or ""
                print("  分数", round(cand.score, 4), "| 页码", ref.page, "| 章节", pp)
                print("    片段:", ref.snippet[:60])
