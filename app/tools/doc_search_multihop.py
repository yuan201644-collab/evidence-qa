# -*- coding: utf-8 -*-
"""多跳文档检索工具：在 doc_search 外面包一层「查询分解 + 多轮检索」。

**工具名与协议与 doc_search 完全一致**（都是 `name="doc_search"`、同样的
`spec` 与 `run` 语义），所以编排层感知不到差别 —— 这正是「统一工具协议」
要达到的效果，与 `GRAPH_BACKEND` 的处理方式一致（见 app/orchestrator.py）。

⚠️ 这里**故意不改 spec.description**：description 会参与编排层的工具选择，
   改了它，多跳与单轮的对比就不再是"只换了检索方式"，A/B 结论会失真。
   要体现多跳，应当走 trace（`decompose` / `sufficiency_check` / `context_merge`
   / `cross_rerank` 四个步骤），而不是改工具描述。

开关（后者是应急手段）：
    DOC_SEARCH_BACKEND=rag              单轮检索（默认，行为与现在完全一致）
    DOC_SEARCH_BACKEND=rag_multihop     多跳检索
    RAG_MULTIHOP=0                      即使选了多跳，也强制退回单轮
"""

from __future__ import annotations

from typing import Any

from app.tools.doc_search import RagDocSearchTool


class MultiHopDocSearchTool(RagDocSearchTool):
    """复用父类的「惰性建索引 + 加锁」逻辑，只在构建时把工具包成多跳。

    沿用父类那套加锁是必须的：`/api/ask` 由 FastAPI 放进线程池并发执行，
    两个并发首次请求各建一套 RAG 就是两份模型常驻内存（单套实测峰值约 6GB）。
    """

    def _ensure_rag_tool(self) -> Any:
        if self._rag_tool is None:
            with self._lock:
                if self._rag_tool is None:
                    if self._tool_factory is None:
                        from app.rag.answer import build_rag_tool_from_corpus

                        self._tool_factory = build_rag_tool_from_corpus
                    from app.rag.multihop import MultiHopRagTool

                    self._rag_tool = MultiHopRagTool(self._tool_factory())
        return self._rag_tool
