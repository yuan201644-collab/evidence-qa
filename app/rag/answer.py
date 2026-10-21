"""answer.py - RAG 答案生成工具（检索 -> 重排 -> 生成 -> 溯源）

把 recall 模块（fusion.HybridRetriever）、reranker、LLM 串起来，封装成
app.contracts 约定的工具：rag_answer(...) -> ToolResult(data=答案, trace=步骤, refs=引用)。

为什么返回 ToolResult 而不是裸字符串：
    题面与评测都要求“答案有迹可循”，引用（DocRef）和调用步骤（TraceStep）是
    可解释性与评测的基础，必须随答案一起返回。

忠实度约束（F15）：
    prompt 强制“只依据给定上下文、不得编造”，并把实际使用的上下文作为 refs 返回，
    让下游能核对答案是否源自检索内容。
"""

import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from app.contracts import ToolResult, TraceStep
from app.progress import emit_progress
from app.rag.config import env_int
from app.rag.llm import OpenAILikeClient
from app.rag.retrieval.fusion import HybridRetriever

_SYSTEM_PROMPT = (
    "你是一个严谨的中文问答助手。只能依据下面<context>中提供的文档片段回答用户问题，"
    "不得编造或引入上下文之外的信息。如果提供的片段不足以回答问题，请明确说明"
    "“根据现有资料无法回答”。\n"
    "引用格式（务必严格遵守）：在相关陈述后用方括号加纯数字标注出处，例如 [1]、[2]、[3]。"
    "数字只能是片段序号，即 1 到 N 的整数（N 是本次给出的片段总数）。"
    "不要写“编号”二字；也不要把原文里的章节号、条款号（如 3.1、2.2）当成标记写进方括号，"
    "那些是文档自己的编号，跟片段序号是两回事。"
)

# 检索深度与最终条数取自 C 的 infra 约定（.env.example「检索与向量库」一节）：
#   RETRIEVE_TOP_K  融合召回条数，也就是送进重排的候选数（默认 20）
#   RERANK_TOP_K    重排后保留的条数，送进生成并作为最终 refs（默认 5）
# 默认值与 .env.example 保持一致——注意 app 代码不加载 .env，变量由启动方注入，
# 所以默认值必须和约定同值，否则没人加载 .env 时口径就悄悄跑偏了。
_ENV_RETRIEVE_TOP_K = "RETRIEVE_TOP_K"
_ENV_RERANK_TOP_K = "RERANK_TOP_K"
_DEFAULT_RETRIEVE_TOP_K = 20
_DEFAULT_RERANK_TOP_K = 5

# 拒答的代码层判据：prompt 里已要求"无依据时拒答"，但 LLM 不一定听话，
# 所以用关键词在代码里兜一道，供评测统计"正确拒答率"。
# ⚠️ 这是粗判据，"未提供""没有相关"可能出现在正常答案里（如"合同未提供违约金条款"），
#   故只用于打标统计，不据此改动答案内容。
_REJECT_KEYWORDS = ("无法回答", "不足以回答", "没有相关", "未提供")


def _parse_provenance_step(corpus_index, refs, step_id):
    """把索引阶段已记录的解析事实写入 trace，不在答题时重跑解析或 OCR。"""
    parse_info = getattr(corpus_index, "parse_info", None)
    if not parse_info:
        return None

    doc_ids = []
    documents = []
    for ref in refs:
        doc_id = getattr(ref, "doc_id", None)
        if not doc_id or doc_id in doc_ids or doc_id not in parse_info:
            continue
        doc_ids.append(doc_id)
        documents.append({"doc_id": doc_id, **parse_info[doc_id]})
    if not documents:
        return None

    return TraceStep(
        step_id=step_id,
        tool="parse_provenance",
        input={"doc_ids": doc_ids},
        output={"documents": documents, "recorded_at": "index_build"},
        elapsed_ms=0.0,
    )


def _parse_citations(text, max_n):
    """从回答文本里解析 [n] 引用，返回去重后的编号列表（1-based，落在 1..max_n 内）。

    正则容忍模型多写一个"编号"前缀（[编号1]），因为早期提示词里出现过这个占位词，
    模型可能养成习惯。

    ⚠️ 本函数只认"纯片段序号"。模型把**文档条款号**写进方括号（[2.2]、[编号3.1]）
    的情况不在这里兜——那属于"非法标记"，由 _fix_illegal_citations() 在解析之前
    改写或删除（见该函数的协议说明）。
    """
    found = []
    for match in _CITATION_RE.finditer(text):
        n = _citation_number(match.group(1), max_n)
        if n is None or n < 1:
            continue
        if n in found:
            continue
        found.append(n)
    return found


def _renumber_citations(text, citation_map):
    """将上下文片段编号映射到紧凑引用列表编号。"""
    changed = {}

    def replace(match):
        old_number = _citation_number(match.group(1))
        if old_number is None:
            return match.group(0)
        new_number = citation_map.get(old_number)
        if new_number is None:
            return match.group(0)
        changed[str(old_number)] = new_number
        return "[" + str(new_number) + "]"

    rewritten = _CITATION_RE.sub(replace, text)
    return rewritten, changed


def _citation_number(digits, max_n=None):
    """Safely convert a citation number, optionally enforcing its upper bound.

    Python limits conversion of extremely long decimal strings. Citation indices are
    small, so compare significant-digit lengths before calling ``int``.
    """
    raw_digits = str(digits)
    if not raw_digits.isascii() or not raw_digits.isdecimal():
        return None
    significant = raw_digits.lstrip("0") or "0"
    if max_n is not None:
        if max_n < 0:
            return None
        limit = str(max_n)
        if len(significant) > len(limit):
            return None
        if len(significant) == len(limit) and significant > limit:
            return None
    try:
        return int(significant)
    except (TypeError, ValueError):
        return None


# 方括号标记的通用形状。只用来"扫一遍看看模型写了什么"，
# 具体某项是合法标记 / 条款号 / 普通括号内容，交给 _fix_illegal_citations 判定。
_CITE_TOKEN_RE = re.compile(r"\[([^\[\]]*)\]")
# Keep parsing, compaction, and multi-hop chain completion on one marker grammar.
# Trailing horizontal whitespace is accepted because models sometimes emit ``[2 ]``.
_CITATION_RE = re.compile(r"\[(?:编号)?[ \t]*([0-9]+)[ \t]*\]")
# 允许的"编号"前缀（模型偶尔会写 [编号2]）
_CITE_PREFIX_RE = re.compile(r"^(?:编号)?[ \t]*")
# 文档条款号的形状：X.Y / X.Y.Z
_CLAUSE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)+")


def _clause_of_snippet(text):
    """从片段正文里取条款号（行首的 X.Y 形式），取不到返回 None。

    这是"条款级溯源"的确定性来源：**条款号由代码从原文抽取，不由模型生成**。
    片段正文长这样：`2.1　质保期限：自设备验收合格之日起 36 个月…`，条款号在行首。
    """
    if not text:
        return None
    for line in text.splitlines():
        m = _CLAUSE_NUMBER_RE.match(line.strip())
        if m:
            return m.group(0)
    return None


def _fix_illegal_citations(text, context_texts, max_n):
    """把非法的方括号标记改写或删除，返回 (修正后的文本, 处理报告)。

    协议（2026-09-21 三人确认）：
      - 正文**唯一合法**的来源标记是 `[n]`（片段序号，1..N）；
      - 条款号**不由模型输出**，改由引用卡片从片段正文确定性抽取（见 _clause_of_snippet）；
      - 非法标记 `[x.y]`：**只在本轮上下文里能唯一映射**到某个片段时改写成 `[n]`，
        映射不唯一或找不到就删除该标记；**不跨文档猜**；
      - 括号里不属于"引用样式"的内容（如 [待确认]）原样保留，不做删改。

    为什么要做这一步：提示词已经明确禁止把条款号写进方括号，但实测仍然出现
    （rag-e-03 写了 [1.8]/[4.3]、rag-h-04 写了 [3.4]）。一旦正文里全是不合法的
    标记，_parse_citations 会解析为空、refs 退化成"全部上下文"，行内引用对读者也
    完全不可信。提示词能把频率压下去，压不到零，所以需要代码层兜底。
    """
    mapped = {}
    dropped = []
    pieces = []
    pos = 0
    for m in _CITE_TOKEN_RE.finditer(text):
        inner = m.group(1).strip()
        core = _CITE_PREFIX_RE.sub("", inner).strip()

        if core.isdigit():
            n = _citation_number(core, max_n)
            if n is not None and 1 <= n <= max_n:
                continue                      # 合法标记，留给 _parse_citations
            # 越界数字：既不是合法片段号，也不是条款号形状，无法映射 -> 删
            pieces.append(text[pos:m.start()])
            dropped.append(inner)
            pos = m.end()
            continue

        if not _CLAUSE_NUMBER_RE.fullmatch(core):
            continue                          # 不是引用样式的括号内容，别动

        # 条款号：只在"本轮送进上下文的片段"里找，且必须唯一命中
        hits = []
        pattern = re.compile(r"(?m)(?<![\w.])" + re.escape(core) + r"(?![\w.])")
        for idx, chunk_text in enumerate(context_texts):
            if pattern.search(chunk_text or ""):
                hits.append(idx)
        pieces.append(text[pos:m.start()])
        if len(hits) == 1:
            pieces.append("[" + str(hits[0] + 1) + "]")
            mapped[inner] = hits[0] + 1
        else:
            dropped.append(inner)
        pos = m.end()

    if pos == 0:
        return text, {"mapped": {}, "dropped": []}
    pieces.append(text[pos:])
    fixed = "".join(pieces)
    if len(dropped) > 0:
        fixed = _tidy_after_drop(fixed)
    return fixed, {"mapped": mapped, "dropped": dropped}


def _tidy_after_drop(text):
    """删掉标记后收拾残留的标点/空格，避免答案读起来是断的。

    只做"标点与标点的邻接"和"标点前多余空格"两类收拾，**不碰任何文字**：
    删标记是删内容，删悬空标点是让句子还读得通（否则会出现「覆盖部件见 。」这种）。
    """
    rules = (
        ("、。", "。"), ("，。", "。"), ("；。", "。"), ("。。", "。"),
        ("、、", "、"), ("，，", "，"), ("；；", "；"),
        (" ，", "，"), (" 。", "。"), (" 、", "、"), (" ；", "；"), (" ：", "："),
        ("，、", "，"), ("、，", "，"),
        ("、[", "["), ("，[", "["),
        ("  ", " "),
    )
    for _ in range(5):                      # 反复收敛，处理「、，。」这类连续残留
        before = text
        for a, b in rules:
            text = text.replace(a, b)
        if text == before:
            break
    return text


def _source_label(ref):
    """把 DocRef 的页码/章节/条款号拼成一句来源标注。

    条款号取自片段正文首行（_clause_of_snippet），是确定性信息——
    这样正文里不需要模型去写条款号，引用卡片照样能给出"第 2.1 条"级别的定位。
    """
    label = ""
    if ref.page is not None:
        label = label + " 页码" + str(ref.page)
    if ref.section_path:
        label = label + " 章节" + ref.section_path
    clause = _clause_of_snippet(ref.snippet)
    if clause:
        label = label + " 条款" + clause
    return label


def _check_rejection(answer_text):
    """代码层判断答案是否为明确拒答（返回 bool）。"""
    for kw in _REJECT_KEYWORDS:
        if kw in answer_text:
            return True
    return False


_CURRENCY_AMOUNT_RE = re.compile(
    r"(?<![\d.])(?P<number>\d+(?:,\d{3})*(?:\.\d+)?)\s*"
    r"(?P<unit>亿元|万元|元)(?:\s*/\s*年)?"
)
_CITATION_SENTENCE_END_RE = re.compile(r"[。！？!?；;\n]")
_CITATION_MARKER_RE = re.compile(r"\[(?:编号)?[ \t]*\d+[ \t]*\]")
_CURRENCY_TO_YUAN = {
    "元": Decimal("1"),
    "万元": Decimal("10000"),
    "亿元": Decimal("100000000"),
}


def _currency_amount_yuan(text: str) -> Decimal | None:
    """将明确写出的阿拉伯数字人民币金额转成元；不处理中文数字或推导式。"""
    match = _CURRENCY_AMOUNT_RE.fullmatch(text.strip())
    if match is None:
        return None
    try:
        value = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    return value * _CURRENCY_TO_YUAN[match.group("unit")]


def _audit_cited_currency_amounts(answer_text: str, context_texts: list[str]) -> dict[str, Any]:
    """检查紧邻引用的阿拉伯数字金额能否在所引片段中找到等值金额。

    这是文本数值一致性检查，不判断金额在业务语义上是否适用，也不处理中文数字、
    跨数字推导或非人民币单位。只检查金额后、同一句且下一个金额前出现的引用。
    """
    checks = []
    for amount_match in _CURRENCY_AMOUNT_RE.finditer(answer_text):
        amount_text = amount_match.group(0)
        amount_yuan = _currency_amount_yuan(amount_text)
        if amount_yuan is None:
            continue

        tail = answer_text[amount_match.end():]
        sentence_end = _CITATION_SENTENCE_END_RE.search(tail)
        next_amount = _CURRENCY_AMOUNT_RE.search(tail)
        limit = len(tail)
        if sentence_end is not None:
            limit = min(limit, sentence_end.start())
        if next_amount is not None:
            limit = min(limit, next_amount.start())
        citation_text = tail[:limit]
        citation_markers = _CITATION_MARKER_RE.findall(citation_text)
        citation_numbers = _parse_citations("".join(citation_markers), len(context_texts))
        if not citation_numbers:
            continue

        matched_citations = []
        for number in citation_numbers:
            source_text = context_texts[number - 1]
            source_amounts = (
                _currency_amount_yuan(match.group(0))
                for match in _CURRENCY_AMOUNT_RE.finditer(source_text)
            )
            if any(source_amount == amount_yuan for source_amount in source_amounts):
                matched_citations.append(number)

        checks.append({
            "amount": amount_text,
            "citation_indices": citation_numbers,
            "matched_citation_indices": matched_citations,
            "status": "matched" if matched_citations else "unmatched",
        })

    unmatched = [item for item in checks if item["status"] == "unmatched"]
    return {
        "scope": "sentence_scoped_arabic_rmb_amounts",
        "status": "warning" if unmatched else ("checked" if checks else "not_applicable"),
        "checked_count": len(checks),
        "unmatched": unmatched,
    }


class RagAnswerTool:
    """RAG 答案生成工具：检索 -> 重排 -> 生成 -> 溯源。

    重排是必需步骤而非可选增强：融合召回只按排名合并 BM25 与向量两路结果，
    语义精度有限，跳过重排等于把噪声片段直接送进 LLM，答案忠实度（F15）
    失去保障。因此构造函数要求 reranker，answer() 每次都会执行重排。

    两个 K 与 C 的评测口径对齐（勿与相似名字混淆）：
        retrieve_top_k  融合召回的深度，即送进 reranker 的候选数；来自 RETRIEVE_TOP_K
        rerank_top_k    重排后保留的条数，进生成、也是最终 refs 的条数；来自 RERANK_TOP_K
    """

    def __init__(self, bm25, store, embedder, llm, reranker, k=60,
                 retrieve_top_k=None, rerank_top_k=None):
        if reranker is None:
            raise ValueError("reranker 是必需的检索步骤，不能为 None；请传入 BgeReranker 或等价实现")
        self.bm25 = bm25
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.reranker = reranker
        self.k = k
        # 显式传参优先；未传则读环境变量，最后回退到与 .env.example 同值的默认值。
        if retrieve_top_k is None:
            retrieve_top_k = env_int(_ENV_RETRIEVE_TOP_K, _DEFAULT_RETRIEVE_TOP_K)
        if rerank_top_k is None:
            rerank_top_k = env_int(_ENV_RERANK_TOP_K, _DEFAULT_RERANK_TOP_K)
        self.retrieve_top_k = retrieve_top_k
        self.rerank_top_k = rerank_top_k

    # ------------------------------------------------------------------
    # 下面两个方法是**纯抽取**：把原来 answer() 里的两段逻辑拆出来，
    # 让「多跳检索」（app/rag/multihop.py）能复用它们组合出「多轮检索 + 只生成一次」。
    # 抽取只搬代码、不改行为 —— answer() 仍走同一条路径，输出必须逐字节一致。
    # ------------------------------------------------------------------

    def retrieve_and_rank(self, query, doc_id=None):
        """单轮检索：融合召回 -> 重排 -> 截断。**不生成、不建 TraceStep**。

        为什么不让这个方法和 trace 耦合：多跳时一轮要建两个步骤、且 step_id 必须全局递增，
        由调用方统一编号才不会串。所以这里只返回中间产物。

        返回 dict：
            query         本轮用的检索串
            num_hits      融合召回条数
            order         重排顺序（原 hits 的下标），进 trace 的 output
            retrieve_refs 召回命中的全部 ref（进"召回"这一步的 hit）
            context_hits  重排后截断到 rerank_top_k 的 [(Candidate, DocRef)]
            retrieve_ms / rerank_ms
        """
        hybrid = HybridRetriever(self.bm25, self.store, self.embedder, k=self.k)
        t0 = time.time()
        emit_progress("rag_retrieve", "检索相关文档片段", "started")
        hits = hybrid.search(query, top_k=self.retrieve_top_k, doc_id=doc_id)
        retrieve_ms = (time.time() - t0) * 1000.0
        emit_progress("rag_retrieve", "检索相关文档片段", "completed", elapsed_ms=retrieve_ms)
        # hits: [(Candidate, DocRef)]

        retrieve_refs = []
        for _cand, ref in hits:
            retrieve_refs.append(ref)

        docs = []
        for cand, _ref in hits:
            docs.append(cand.chunk.text)
        t0 = time.time()
        emit_progress("rag_rerank", "排序并筛选证据片段", "started")
        ranked = self.reranker.rerank(query, docs)
        rerank_ms = (time.time() - t0) * 1000.0
        emit_progress("rag_rerank", "排序并筛选证据片段", "completed", elapsed_ms=rerank_ms)
        reranked_hits = []
        for index, _score in ranked:
            reranked_hits.append(hits[index])

        # 取 rerank_top_k 作为生成上下文（reranked_hits 已按重排分降序）
        context_hits = reranked_hits[: self.rerank_top_k]

        return {
            "query": query,
            "num_hits": len(hits),
            "order": [idx for idx, _ in ranked],
            "retrieve_refs": retrieve_refs,
            "context_hits": context_hits,
            "retrieve_ms": retrieve_ms,
            "rerank_ms": rerank_ms,
        }

    def retrieve_steps(self, r, step_id, doc_id=None):
        """把一轮 retrieve_and_rank 的产物建成两个 TraceStep。

        step_id 由调用方给（单轮从 1 开始；多跳每轮 +2），这样多轮之间不会重号。

        hit 的语义是「**这一步**命中了什么」：召回这步挂全部召回的 ref，
        重排这步挂它排完序留下的那几条。不能把召回结果在重排步再挂一遍 ——
        那会让 collect_refs(trace) 汇总出同一批来源的重复条目，
        也把「召回 20 条」和「重排留下 5 条」两件事混成一件。
        """
        return [
            TraceStep(
                step_id=step_id,
                tool="hybrid_retrieve",
                input={"query": r["query"], "doc_id": doc_id, "top_k": self.retrieve_top_k},
                output={"num_hits": r["num_hits"]},
                elapsed_ms=r["retrieve_ms"],
                hit=r["retrieve_refs"],
            ),
            TraceStep(
                step_id=step_id + 1,
                tool="rerank",
                input={"query": r["query"], "num": r["num_hits"]},
                output={"order": r["order"]},
                elapsed_ms=r["rerank_ms"],
                hit=[ref for _, ref in r["context_hits"]],
            ),
        ]

    def generate(self, question, context_hits, chain_notes=None, require_chain=False,
                 known_facts=None):
        """单次生成：拼 prompt -> 调 LLM -> 修非法引用 -> 解析 [n]。**不建 TraceStep**。

        ⚠️ 引用编号 `[n]` 是**本次传进来这组 context_hits 的序号 1..N**。
        多跳时必须先把各轮结果合并成**一组**再调本方法 —— 中间轮次不要调它，
        否则模型写的 `[1]` 指哪一轮的片段就说不清了。

        Args:
            known_facts: 结构化查询（nl2sql）已经算出的事实文本。跨源问句里
                数据部分已由数据库答完，文档侧只该补法规/制度依据。
                **不传时行为与过去完全一致**（纯文档题的默认路径）。

                为什么必须传（2026-10-17 实测）：跨源题 gov-x-A01 的 SQL 已算出
                「中央预算+单一来源共 4 个」，但文档侧生成器看不到这个结果，
                只拿到法规片段，于是如实回答「文档中没有中央预算的信息」，
                最终答案变成「count_question：4。；根据现有资料无法回答…」——
                数据算对了、文档也检索对了，**拼在一起却自相矛盾**。
        """
        numbered = []
        for i, (cand, ref) in enumerate(context_hits, start=1):
            head = "[" + str(i) + "]"
            src = _source_label(ref)
            numbered.append(head + src + ":\n" + cand.chunk.text)
        context_text = "\n\n".join(numbered)
        n_ctx = len(context_hits)
        # 把片段总数写进提示词：只说"1 到 N"太抽象，给出具体数字能明显减少乱标编号
        chain_text = ""
        if chain_notes:
            chain_text = (
                "\n<verified_chain>\n以下是检索过程已由片段验证的中间事实。"
                "它们只能用于连接片段，仍须按对应编号引用，不能补充片段外事实。\n"
                + "\n".join("- " + str(note) for note in chain_notes)
                + "\n</verified_chain>\n"
            )
        facts_text = ""
        if known_facts:
            # 刻意用「已知事实」而不是「参考信息」：后者容易被模型当成又一批待引用片段，
            # 于是它会给数据库算出的数字也标一个 [n]，而那个编号根本指不到任何文档。
            #
            # 「不要声明文档未提及」这一句是 2026-10-17 实测补的：第一版只说
            # 「数据已确定、不需要重算」，模型仍然先写一句「根据现有资料无法
            # 回答库中…有几个的问题，因为提供的文档片段中没有提及…」，再接上
            # 法规——数据部分明明已经答了，它却先自我否定一遍。这类原题问的是
            # 「数据 + 规定」两件事，文档本来就不该承载前半件。
            facts_text = (
                "\n<已知事实>\n"
                "以下内容由数据库查询直接给出，是本题已经确定的事实，不需要你重新计算，"
                "也不需要为它标注片段编号。\n"
                "这类问题同时包含「数据」与「规定」两部分：数据部分已由上面的查询答完，"
                "你只负责补充规定与依据。**不要重复回答数据部分，也不要声明资料中没有该数据**——"
                "文档本来就不承载数据，说它没有是不成立的。\n"
                + str(known_facts).strip()
                + "\n</已知事实>\n"
            )
        citation_guidance = ""
        if require_chain:
            citation_guidance = (
                "\n<完整溯源要求>\n"
                "若问题需要跨文档推导，先回答问题，再另起一行写“关系证据：”，"
                "按从问题对象到最终结论的顺序列出每个必要关系。每个关系事实和最终结论都要紧邻引用"
                "直接支持它的片段；不能只引用给出最终数值的文档，漏掉该数值如何适用于问题对象的连接证据。"
                "只引用与本题关系链有关的片段；涉及数据集、设备、人员或业务对象时，必须确认被引制度明确适用于"
                "问题点名的对象或已验证的中间实体。其他对象、同类别对象或相邻制度的规定不能移用到本题。"
                "材料没有说明权限或控制要求时，只能说文档未明确说明；禁止依据常识、类别特征或其他对象的制度推测。"
                "证据不足以连接时，明确指出缺少哪一段关系，不要猜测。\n</完整溯源要求>\n"
            )
        user_prompt = (
            "请依据以下文档片段回答问题。\n"
            "本次共 " + str(n_ctx) + " 个片段，编号 1 到 " + str(n_ctx) + "。\n\n"
            + facts_text
            + "<context>\n"
            + context_text
            + "\n</context>\n"
            + chain_text
            + citation_guidance
            + "\n问题："
            + question
        )
        t0 = time.time()
        emit_progress("rag_generate", "依据证据生成回答", "started")
        answer_text = self.llm.chat(_SYSTEM_PROMPT, user_prompt)
        gen_ms = (time.time() - t0) * 1000.0
        emit_progress("rag_generate", "依据证据生成回答", "completed", elapsed_ms=gen_ms)

        # 引用协议兜底：模型可能把文档条款号写进方括号（[2.2] / [1.8] / [编号3.1]），
        # 那些不是片段序号，既让 refs 解析失败、也让行内引用对读者不可信。
        # 能唯一映射到本轮某片段的就改写成 [n]，否则删掉该标记。
        # 顺序很重要：**先修正文本，再解析引用**，否则解析看到的还是非法标记。
        answer_text, cite_fix = _fix_illegal_citations(
            answer_text, [cand.chunk.text for cand, _ in context_hits], len(context_hits)
        )

        gen_refs = []
        for _cand, ref in context_hits:
            gen_refs.append(ref)
        # 拒答只打标、不清空 refs：refs 是"检索到的依据"，与"答案是否作答"是两件事。
        # 评测的检索命中率正是拿 refs 判的（eval/rag_metrics.evaluate_retrieval），
        # 拒答时清空会把"检索对了但模型没答"误记成"检索没命中"，把两个指标混在一起。
        rejected = _check_rejection(answer_text)

        # 解析 [n] 引用：解析到就用被引用的子集，没解析到则保留全部上下文引用
        # （保留这条回退是有意的：宁可 refs 偏多，也不要让"检索对了但没标注"变成无据可查）
        cited = _parse_citations(answer_text, len(context_hits))
        renumbered = {}
        if len(cited) > 0:
            final_refs = []
            final_context_texts = []
            citation_map = {context_number: ref_number for ref_number, context_number in enumerate(cited, 1)}
            for n in cited:
                final_refs.append(context_hits[n - 1][1])
                final_context_texts.append(context_hits[n - 1][0].chunk.text)
            answer_text, renumbered = _renumber_citations(answer_text, citation_map)
        else:
            final_refs = gen_refs
            final_context_texts = []
        cite_fix["renumbered"] = renumbered

        currency_audit = _audit_cited_currency_amounts(answer_text, final_context_texts)
        if currency_audit["status"] == "warning":
            flagged_amounts = "、".join(dict.fromkeys(
                item["amount"] for item in currency_audit["unmatched"]
            ))
            answer_text += (
                "\n\n引用核验提示：金额 " + flagged_amounts
                + " 未能在同句标注的引用片段中找到等值数字，请核对来源。"
            )

        return {
            "text": answer_text,
            "gen_ms": gen_ms,
            "cite_fix": cite_fix,
            "citation_amount_audit": currency_audit,
            "rejected": rejected,
            "gen_refs": gen_refs,
            "final_refs": final_refs,
            "context_num": n_ctx,
        }

    def generate_step(self, question, g, step_id):
        """把一次 generate 的结果建成 TraceStep（step_id 由调用方给）。"""
        return TraceStep(
            step_id=step_id,
            tool="llm_generate",
            input={"query": question, "context_num": g["context_num"]},
            output={
                "answer_len": len(g["text"]),
                "rejected": g["rejected"],
                # 引用修正留痕：改写/删除了哪些非法标记，便于事后核对引用是否可靠
                "citation_fix": g["cite_fix"],
                "citation_amount_audit": g.get("citation_amount_audit", {
                    "scope": "sentence_scoped_arabic_rmb_amounts",
                    "status": "not_applicable",
                    "checked_count": 0,
                    "unmatched": [],
                }),
            },
            elapsed_ms=g["gen_ms"],
            hit=g["gen_refs"],
        )

    def answer(self, question, doc_id=None, known_facts=None, answer_question=None):
        """返回 ToolResult：data=答案文本，trace=调用步骤，refs=引用的 DocRef 列表。

        doc_id 为空表示**在全部语料里检索**（编排层 doc_search 要的就是这个语义）；
        非空则只在该文档内召回 —— 两个通道都会按它过滤，不会串出别的文档。

        known_facts: 跨源问句里结构化查询已经算出的事实，透传给生成器，
            让文档侧只补法规依据、不重复作答（见 `generate` 的说明）。
            默认 None，纯文档题的调用路径与过去逐字一致。

        answer_question: **只用于生成、不用于检索**的提问。跨源题里数据那半已被
            nl2sql 答完，交给文档侧的只有规定那半；但检索仍要用原问句，因为
            法规的召回靠原题里的法规措辞（实测换成子问后召回会掉）。
            为空时与 `question` 相同（默认行为不变）。
        """
        r = self.retrieve_and_rank(question, doc_id)
        trace = []
        provenance = _parse_provenance_step(
            getattr(self, "corpus_index", None), r["retrieve_refs"], step_id=1
        )
        if provenance is not None:
            trace.append(provenance)
        trace.extend(self.retrieve_steps(r, step_id=len(trace) + 1, doc_id=doc_id))
        gen_question = answer_question or question
        g = self.generate(gen_question, r["context_hits"], known_facts=known_facts)
        trace.append(self.generate_step(question, g, step_id=len(trace) + 1))

        return ToolResult(
            data=g["text"],
            trace=trace,
            refs=g["final_refs"],
        )


def build_rag_tool(pdf_path, doc_id, dsn=None, llm=None, reranker=None):
    """从一份 PDF 便捷构建 RagAnswerTool（loader -> toc -> chunker -> 索引 -> 工具）。

    reranker 不传时默认构建 BgeReranker —— 重排是必需步骤，这里不会跳过。
    BgeReranker 的权重是首次调用 rerank 时才惰性加载的，此处仅构造对象，
    不会立刻占用显存/内存。
    """
    from app.rag.retrieval.retriever import build_retriever_from_pdf
    from app.rag.retrieval.indexer import index_chunks
    from app.rag.retrieval.reranker import BgeReranker

    if llm is None:
        llm = OpenAILikeClient()
    if reranker is None:
        reranker = BgeReranker()
    bm25, chunks = build_retriever_from_pdf(pdf_path, doc_id)
    store, embedder = index_chunks(chunks, dsn=dsn)
    return RagAnswerTool(bm25, store, embedder, llm, reranker=reranker)


def build_rag_tool_from_corpus(
    dsn=None, llm=None, reranker=None, paths=None, *, read_only_index=None
):
    """按「整个语料」建 RagAnswerTool（走 corpus.index_corpus，可跨文档检索）。

    与 build_rag_tool 的区别：后者是「一份 PDF 一个工具」，检索范围只有那一份；
    本函数扫全语料建**一个**索引，answer(question) 不传 doc_id 就是跨文档检索 ——
    编排层的 doc_search 需要的正是这个语义。

    语料发现完全动态（扫 data/corpus/raw/，不写死文件名与数量），
    并带增量：dsn 非空时未变的文档会跳过 bge-m3 编码（详见 corpus.py）。

    返回的 tool 上挂了 `corpus_index`（corpus.CorpusIndex），
    里面是「重建/跳过/清孤儿/忽略了哪些非 PDF」的明细，供日志与排查。
    """
    from pathlib import Path

    from app.rag.corpus import index_corpus
    from app.rag.retrieval.reranker import BgeReranker

    if paths is None and os.environ.get("RAG_CORPUS_DIR", "").strip():
        corpus_dir = Path(os.environ["RAG_CORPUS_DIR"]).resolve()
        if not corpus_dir.is_dir():
            raise FileNotFoundError("RAG_CORPUS_DIR 不存在或不是目录")
        paths = sorted(
            str(path) for path in corpus_dir.iterdir()
            if path.is_file() and path.suffix.lower() in {".pdf", ".docx", ".md", ".txt"}
        )
        if not paths:
            raise FileNotFoundError("RAG_CORPUS_DIR 中没有支持的文档")
    if dsn is None:
        dsn = os.environ.get("RAG_DSN") or None
    if read_only_index is None:
        read_only_index = os.environ.get("RAG_INDEX_READ_ONLY", "0").strip() == "1"

    if llm is None:
        llm = OpenAILikeClient()
    if reranker is None:
        reranker = BgeReranker()
    corpus_index = index_corpus(paths=paths, dsn=dsn, read_only=read_only_index)
    tool = RagAnswerTool(
        corpus_index.bm25, corpus_index.store, corpus_index.embedder, llm, reranker=reranker
    )
    tool.corpus_index = corpus_index
    return tool


if __name__ == "__main__":
    from app.rag.ingest.loader import _default_pdf_paths

    paths = _default_pdf_paths()
    if len(paths) == 0:
        print("找不到 PDF：默认去 data/corpus/raw/ 下找，请把语料放到那里，或运行时传入路径")
    else:
        for pdf_path in paths:
            doc_id = os.path.splitext(os.path.basename(pdf_path))[0]
            llm = OpenAILikeClient()
            tool = build_rag_tool(pdf_path, doc_id, dsn=os.environ.get("PG_DSN"), llm=llm)
            question = "检索模块怎么设计"
            print("文件:", pdf_path, "doc_id:", doc_id)
            print("问题:", question)
            result = tool.answer(question, doc_id)
            print("答案:\n", result.data)
            print("引用数:", len(result.refs))
            for ref in result.refs:
                print("  doc_id=", ref.doc_id, "page=", ref.page, "section=", ref.section_path)
