"""真实文档检索工具：把 RAG 链路接到统一工具协议。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from app.contracts import DocRef, ToolResult, TraceStep
from app.rag.retrieval.named_doc import extract_title_line, resolve_named_doc
from app.tools.base import CAPABILITY_DOC, BaseTool, ToolSpec


def _corpus_titles(rag_tool: Any):
    """产出语料里所有 (doc_id, 标题)，供书名号点名解析用。

    标题来源按优先级：
        1. Chunk.doc_title（chunker 建切片时写入的显示名）
        2. 该文档第一个切片正文里的**独立标题行**（`extract_title_line`）

    为什么需要第 2 条：政采语料的 doc_title 全是 None（清洗后的纯文本
    没有标题元数据），只用第 1 条时书名号点名解析整套失效，题面明明点了
    「《…招标公告》」却仍然全库检索，被同项目的更正公告顶掉（2026-10-18）。

    切片来自内存里已加载的 BM25 索引，不额外查库。

    ⚠ 标题行不一定落在该文档的**第一个**切片里（政采公告的前几个切片是
    「公告概要 / 采购单位 / 预算金额」等键值对，标题行夹在中间），
    所以这里先把每份文档的所有切片攒起来，再按出现顺序找第一个能抽出
    标题的切片 —— 只看首片会漏掉整整一半文档（2026-10-18 实测）。
    """
    bm25 = getattr(rag_tool, "bm25", None)
    chunks = getattr(bm25, "chunks", None) or []
    texts: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    for chunk in chunks:
        doc_id = getattr(chunk, "doc_id", None)
        if not doc_id:
            continue
        title = getattr(chunk, "doc_title", None)
        if title and title != doc_id:
            titles.setdefault(doc_id, title)
        texts.setdefault(doc_id, []).append(getattr(chunk, "text", "") or "")

    for doc_id, doc_title in titles.items():
        yield doc_id, doc_title

    for doc_id, parts in texts.items():
        if doc_id in titles:
            continue
        found = None
        for part in parts:
            found = extract_title_line(part)
            if found:
                break
        yield doc_id, found or doc_id


class RagDocSearchTool(BaseTool):
    """按需构建语料级 RAG 工具，避免应用启动时加载模型和重建索引。"""

    def __init__(
        self,
        rag_tool: Any | None = None,
        tool_factory: Callable[[], Any] | None = None,
        kag_supplementer: Any | None = None,
    ) -> None:
        self._rag_tool = rag_tool
        self._tool_factory = tool_factory
        if kag_supplementer is None:
            from app.kag_supplement import KagProcessSupplementer

            kag_supplementer = KagProcessSupplementer.from_env()
        self._kag_supplementer = kag_supplementer
        # `/api/ask` 是同步处理函数，FastAPI 会把它放进线程池并发执行，
        # 所以「首次请求建索引」这件事必须加锁：两个并发首次请求各建一套 RAG
        # 就是两份模型常驻内存（单套实测峰值约 6GB），叠加后足以触发 OOM。
        self._lock = threading.Lock()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="doc_search",
            capabilities=(CAPABILITY_DOC,),
            description=(
                "在合同、制度、报告等文稿中检索证据并生成带引用的回答。"
                "适用于质保、条款、流程、规程、响应时限等非结构化问题。"
            ),
            input_schema={
                "question": "用户原话",
                "hint": "预留的检索线索，可空",
                "retrieval_mode": "standard 或 deep；deep 由项目 RAG 与 KAG 补充候选共同检索",
                "known_facts": (
                    "跨源问句中结构化查询已算出的事实文本；文档侧据此只补规定与依据，"
                    "不重复作答。纯文档题不传"
                ),
                "answer_question": (
                    "只用于生成、不用于检索的提问（跨源题里只问规定那半）；"
                    "为空时与 question 相同"
                ),
            },
            returns="基于语料的回答、检索过程与文档引用",
            cost_hint="慢",
        )

    def _ensure_rag_tool(self) -> Any:
        if self._rag_tool is None:
            # 双重检查：拿到锁的线程建索引，其余线程等它建好直接用。
            # 代价是首个请求变慢（本来就要等），收益是模型只加载一份。
            with self._lock:
                if self._rag_tool is None:
                    if self._tool_factory is None:
                        from app.rag.answer import build_rag_tool_from_corpus

                        self._tool_factory = build_rag_tool_from_corpus
                    self._rag_tool = self._tool_factory()
        return self._rag_tool

    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question", "")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("doc_search 需要非空 question")
        retrieval_mode = payload.get("retrieval_mode", "standard")
        if retrieval_mode not in {"standard", "deep"}:
            raise ValueError("doc_search retrieval_mode 只能是 standard 或 deep")
        known_facts = payload.get("known_facts")
        if known_facts is not None and not isinstance(known_facts, str):
            raise ValueError("doc_search known_facts 必须是字符串或 None")
        answer_question = payload.get("answer_question")
        if answer_question is not None and not isinstance(answer_question, str):
            raise ValueError("doc_search answer_question 必须是字符串或 None")
        rag_tool = self._ensure_rag_tool()
        # 用户在题面里用书名号点了名（「《某某招标公告》中载明的预算金额是多少？」），
        # 就只到那份文档里检索。不这么做时，同一项目的**更正公告**会顶掉
        # 招标公告 —— 更正公告里写着「本项目招标公告中预算金额因笔误录入有误」，
        # 字面上比招标公告本身还像答案，答案因此答成更正后的错误金额
        # （2026-10-18 实测：grag-12 答 685 万，正确是 617500 元）。
        # 解析不出或语料里有歧义时返回 None，行为与不加这一步完全一致。
        named_doc_id = resolve_named_doc(question, lambda: _corpus_titles(rag_tool))
        # known_facts 只在 standard 分支透传：deep 分支有自己的 KAG 融合生成路径，
        # 本次不改它（改动面越小越好，且 deep 默认未启用）。
        if retrieval_mode == "deep":
            result = self._answer_deep(question, rag_tool, doc_id=named_doc_id)
        elif known_facts:
            result = rag_tool.answer(
                question,
                doc_id=named_doc_id,
                known_facts=known_facts,
                answer_question=answer_question,
            )
        else:
            result = rag_tool.answer(question, doc_id=named_doc_id)
        # 带上语料索引，好让编排层回答「这段答案来自哪一批文档」
        # （AskResponse.data_source）。与 nl2sql 侧带 catalog 是同一个道理：
        # 界面只显示文档标题时，用户分不清答案来自哪一批材料。
        corpus_index = getattr(self._rag_tool, "corpus_index", None)
        if corpus_index is not None and isinstance(result.data, str):
            result = ToolResult(
                data={"text": result.data, "corpus_index": corpus_index},
                trace=result.trace,
                refs=result.refs,
            )
        return result

    def _answer_deep(self, question: str, configured_tool: Any, doc_id: str | None = None) -> ToolResult:
        """Retrieve RAG and KAG candidates, then rerank and answer in the project.

        doc_id 非空时只在该文档内检索（题面用书名号点名了文档，见 `run`）。
        """
        from app.progress import emit_progress
        from app.rag.answer import _parse_provenance_step
        from app.rag.retrieval.fusion import HybridRetriever
        from app.rag.schemas import Candidate

        # A multihop wrapper exposes the same underlying project RAG object.
        # Deep mode deliberately starts with its base RAG retrieval, then adds
        # KAG candidates once; it does not recursively invoke the wrapper.
        rag_tool = getattr(configured_tool, "_rag", configured_tool)
        required = ("bm25", "store", "embedder", "reranker", "corpus_index", "generate")
        if any(not hasattr(rag_tool, name) for name in required):
            result = configured_tool.answer(question, doc_id=doc_id)
            return self._append_kag_status(result, "unavailable", "rag_backend_not_compatible")

        emit_progress("rag_retrieve", "检索项目文档", "started")
        retrieval_started = time.perf_counter()
        hybrid = HybridRetriever(rag_tool.bm25, rag_tool.store, rag_tool.embedder, k=rag_tool.k)
        rag_hits = hybrid.search(
            question,
            top_k=rag_tool.retrieve_top_k,
            doc_id=doc_id,
        )
        if doc_id:
            emit_progress(
                "rag_retrieve", "按文档标题锁定检索范围", "completed",
                detail=doc_id,
            )
        retrieval_ms = (time.perf_counter() - retrieval_started) * 1000.0
        emit_progress("rag_retrieve", "检索项目文档", "completed", elapsed_ms=retrieval_ms)

        supplementer = self._kag_supplementer

        supplement_request = None
        normalized_texts: dict[str, str] = {}
        kag_result = None
        kag_hits: list[tuple[Any, DocRef]] = []
        kag_reason = None
        if supplementer is None:
            from app.evidence_supplement import RouteExecution, SupplementResult

            kag_result = SupplementResult(
                request_id=f"unconfigured-{time.time_ns()}",
                status="unavailable",
                elapsed_ms=0,
                reason_code="runtime_not_configured",
                executed_routes=[RouteExecution(
                    route="hybrid", status="unavailable", elapsed_ms=0, candidate_count=0
                )],
            )
        else:
            emit_progress("kag_supplement", "补充图谱与向量检索", "started")
            kag_started = time.perf_counter()
            try:
                supplement_request, normalized_texts = supplementer.prepare_request(
                    question,
                    rag_hits,
                    rag_tool.corpus_index.chunks,
                )
                kag_result = supplementer.supplement(supplement_request)
                if kag_result.status in {"completed", "partial"}:
                    kag_hits = supplementer.validate_and_convert(
                        supplement_request,
                        kag_result,
                        normalized_texts,
                        rag_tool.corpus_index.chunks,
                    )
            except Exception as exc:
                kag_reason = "candidate_validation_failed" if supplement_request else "scope_preparation_failed"
                from app.evidence_supplement import RouteExecution, SupplementResult

                kag_result = SupplementResult(
                    request_id=(supplement_request.request_id if supplement_request else f"invalid-{time.time_ns()}"),
                    status="failed",
                    elapsed_ms=(time.perf_counter() - kag_started) * 1000.0,
                    reason_code=kag_reason,
                    executed_routes=[RouteExecution(
                        route="hybrid", status="failed", elapsed_ms=(time.perf_counter() - kag_started) * 1000.0,
                        candidate_count=0,
                    )],
                )
            emit_progress(
                "kag_supplement", "补充图谱与向量检索",
                "failed" if kag_result.status in {"unavailable", "failed", "timed_out"} else "completed",
                elapsed_ms=(time.perf_counter() - kag_started) * 1000.0,
            )

        # Exact-location duplicates prefer the RAG copy because it carries the
        # project parser's complete citation metadata. Keep partial overlaps:
        # the later project reranker decides whether each passage is useful.
        fused_hits: list[tuple[Any, DocRef]] = []
        seen: set[tuple[Any, ...]] = set()
        for candidate, ref in [*rag_hits, *kag_hits]:
            if ref.char_start is not None and ref.char_end is not None:
                key = (ref.doc_id, ref.page, ref.char_start, ref.char_end)
            else:
                key = (ref.doc_id, ref.page, ref.section_path, ref.snippet)
            if key in seen:
                continue
            seen.add(key)
            fused_hits.append((candidate, ref))

        emit_progress("rag_rerank", "联合排序 RAG 与 KAG 证据", "started")
        rerank_started = time.perf_counter()
        if fused_hits:
            ranked = rag_tool.reranker.rerank(question, [candidate.chunk.text for candidate, _ in fused_hits])
            order = [int(index) for index, _score in ranked if 0 <= int(index) < len(fused_hits)]
            # Preserve source ordering for any candidate omitted by a faulty
            # reranker result; it remains visible in trace as unranked input.
            order.extend(index for index in range(len(fused_hits)) if index not in set(order))
            rerank_scores = {int(index): float(score) for index, score in ranked if 0 <= int(index) < len(fused_hits)}
            ranked_hits = []
            for index in order:
                candidate, ref = fused_hits[index]
                if index in rerank_scores:
                    candidate = Candidate(
                        chunk=candidate.chunk,
                        score=rerank_scores[index],
                        method=candidate.method,
                    )
                ranked_hits.append((candidate, ref))
        else:
            order, ranked_hits = [], []
        rerank_ms = (time.perf_counter() - rerank_started) * 1000.0
        context_hits = ranked_hits[:rag_tool.rerank_top_k]
        emit_progress("rag_rerank", "联合排序 RAG 与 KAG 证据", "completed", elapsed_ms=rerank_ms)

        generated = rag_tool.generate(question, context_hits, require_chain=True)
        trace: list[TraceStep] = []
        provenance = _parse_provenance_step(
            rag_tool.corpus_index,
            [ref for _candidate, ref in rag_hits],
            step_id=1,
        )
        if provenance is not None:
            trace.append(provenance)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="hybrid_retrieve",
            input={"query": question, "top_k": rag_tool.retrieve_top_k},
            output={"candidate_count": len(rag_hits)},
            elapsed_ms=retrieval_ms,
            hit=[ref for _candidate, ref in rag_hits],
        ))
        kag_refs = [ref for _candidate, ref in kag_hits]
        diagnostics = getattr(kag_result, "diagnostics", None)
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="kag_supplement",
            input={"requested": True, "retrieval_mode": "deep"},
            output={
                "status": kag_result.status,
                "reason_code": kag_reason or kag_result.reason_code,
                "candidate_count": len(kag_hits),
                "executed_routes": [route.model_dump(mode="json") for route in kag_result.executed_routes],
                "diagnostics": diagnostics.model_dump(mode="json") if diagnostics else None,
            },
            elapsed_ms=kag_result.elapsed_ms,
            hit=kag_refs,
        ))
        trace.append(TraceStep(
            step_id=len(trace) + 1,
            tool="joint_rerank",
            input={"query": question, "candidate_count": len(fused_hits)},
            output={"order": order, "retained": len(context_hits)},
            elapsed_ms=rerank_ms,
            hit=[ref for _candidate, ref in context_hits],
        ))
        trace.append(rag_tool.generate_step(question, generated, step_id=len(trace) + 1))
        answer_text = generated["text"]
        diagnostics = getattr(kag_result, "diagnostics", None)
        if kag_result.status in {"unavailable", "failed", "timed_out"}:
            answer_text += "\n\n提示：本次深度检索未取得 KAG 补充结果，回答仅依据项目 RAG 检索证据。"
        elif not kag_hits:
            answer_text += "\n\n提示：KAG 未提供可定位到原文的补充证据，本次回答依据项目 RAG 检索结果。"
        elif diagnostics and (diagnostics.unmapped_candidate_count or diagnostics.rejected_candidate_count):
            answer_text += "\n\n提示：部分 KAG 候选无法核对到当前原文，已剔除；回答仅使用可定位的证据。"
        elif kag_result.status == "partial":
            answer_text += "\n\n提示：KAG 返回受预算限制，本次回答使用已核验的候选证据。"
        return ToolResult(data=answer_text, trace=trace, refs=generated["final_refs"])

    @staticmethod
    def _append_kag_status(result: ToolResult, status: str, reason: str) -> ToolResult:
        trace = [*result.trace, TraceStep(
            step_id=len(result.trace) + 1,
            tool="kag_supplement",
            input={"requested": True, "retrieval_mode": "deep"},
            output={"status": status, "reason_code": reason, "candidate_count": 0},
            elapsed_ms=0,
            hit=[],
        )]
        return ToolResult(data=result.data, trace=trace, refs=result.refs)
