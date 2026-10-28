# -*- coding: utf-8 -*-
"""Structured aggregate retriever —— 结构化聚合检索器（KAG 第 6 路检索）。

与既有的 5 路检索器的分工
  · kg_cs_open_spg / kg_fr_knowledge_unit：走实体链路的子图检索（chunk 级召回）；
  · rc_open_spg / vector_chunk_retriever / text_chunk_retriever / anchor_chunk_retriever：块级召回；
  · 本检索器：**不做相似度、不遍历子图**，直接把问题里的「单位 + 文档 + 类别」解析成
    参数化 Cypher，在图上做确定性计数/列举，返回一段可逐项复算的证据文本。

为什么需要它
  块级检索对「某单位在某年度名单里各类项目各多少项」这类聚合问题天然无力：
  答案不在任何单个块里，而在跨块的计数关系上。图里其实有这个关系（项目-[:承担单位]->单位、
  项目-[:项目类别]->类别、项目-[:source]->文档块），缺的是一条会做计数的检索路。

边界（写报告时必须保留）
  · 返回的是「证据 + 口径」，不是最终答案，答案仍由生成器给出；
  · 文档定位依赖问题里出现省份/年度/单位名；「上述两份名单」这类无指代的问题无法定位，直接返回空；
  · 只读：生成的 Cypher 只有 MATCH / RETURN。

配置（环境变量，均有默认值）
  KAG_STRUCT_AGG_NEO4J   默认 http://release-openspg-neo4j:7474
  KAG_STRUCT_AGG_DB      默认 edugov4armds
  KAG_STRUCT_AGG_USER    默认 neo4j:neo4j@openspg
"""
import base64
import json
import logging
import os
import urllib.request

from kag.interface import RetrieverABC, ChunkData, RetrieverOutput

from kag.common.tools.algorithm_tool.chunk_retriever.struct_agg_core import (
    StructAggCore,
)

logger = logging.getLogger()

_NEO4J = os.environ.get("KAG_STRUCT_AGG_NEO4J", "http://release-openspg-neo4j:7474").rstrip("/")
_DB = os.environ.get("KAG_STRUCT_AGG_DB", "edugov4armds")
_USER = os.environ.get("KAG_STRUCT_AGG_USER", "neo4j:neo4j@openspg")
_AUTH = "Basic " + base64.b64encode(_USER.encode()).decode()

_CORE = None
_CORE_LOCK = None


def _runner(stmt, params=None, timeout=90):
    url = "%s/db/%s/tx/commit" % (_NEO4J, _DB)
    payload = {"statements": [{"statement": stmt, "parameters": params or {}}]}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": _AUTH},
    )
    d = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
    if d.get("errors"):
        raise RuntimeError(d["errors"][0].get("message", "cypher error")[:300])
    res = d["results"][0]
    cols = res["columns"]
    return [{c: row[i] for i, c in enumerate(cols)}
            for x in res["data"] for row in [x["row"]]]


def _get_core():
    global _CORE, _CORE_LOCK
    if _CORE is None:
        import threading
        if _CORE_LOCK is None:
            _CORE_LOCK = threading.Lock()
        with _CORE_LOCK:
            if _CORE is None:
                _CORE = StructAggCore(_runner)
    return _CORE


@RetrieverABC.register("structured_aggregate_retriever")
class StructuredAggregateRetriever(RetrieverABC):
    def __init__(self, top_k: int = 3, **kwargs):
        super().__init__(top_k, **kwargs)

    def invoke(self, task, **kwargs) -> RetrieverOutput:
        query = ""
        try:
            query = (task.arguments or {}).get("query", "") or ""
        except Exception:
            query = ""
        if not query.strip():
            return RetrieverOutput(retriever_method=self.name)
        mode = ""
        try:
            with open("/tmp/kag_struct_agg") as f:
                mode = f.read().strip()
        except Exception:
            mode = ""
        try:
            core = _get_core()
            text = core.build_evidence(query, require_intent=(mode in ("2", "3", "4")))
        except Exception as e:                                  # 图不可用时静默降级
            logger.warning("structured_aggregate_retriever failed: %s", e)
            return RetrieverOutput(retriever_method=self.name, err_msg=str(e)[:200])
        if not text:
            return RetrieverOutput(retriever_method=self.name)
        chunk = ChunkData(
            content=text,
            title="结构化聚合证据（知识图谱确定性计数）",
            chunk_id="structured_aggregate#0",
            score=1.0,
            properties={"retriever": self.name},
        )
        chunks = [chunk]
        # mode 3/4：把被计数的那几个真实源块一并返回，让出处能落到真实文档上。
        # 证据块永远排第 0 位，源块只作引用锚点与人工复核，不参与答案组织的主依据。
        if mode in ("3", "4"):
            for nm, snip in getattr(core, "last_sources", []) or []:
                chunks.append(ChunkData(
                    content=snip or "",
                    title=nm.split("_v2")[0],
                    chunk_id=nm,
                    score=0.5,
                    properties={"retriever": self.name, "role": "citation_anchor"},
                ))
        return RetrieverOutput(chunks=chunks, retriever_method=self.name, summary=text)

    def schema(self):
        return {
            "name": "structured_aggregate_retriever",
            "description": (
                "结构化聚合检索：当问题需要**跨多条记录做计数、分组、求和、求最大、求差值或列举**时使用，"
                "例如「某单位在某年度名单中各类项目各多少项」「三个年度重大类项目分别多少项」"
                "「验收结果为撤销的是哪一项」。它会在知识图谱上按承担单位/项目类别/所属文档做确定性计数，"
                "返回带口径说明的计数明细。"
                "必须把用户的原始问题整句传入 query（包含省份、年度、单位全名、项目类别等限定词），"
                "不要改写或省略这些限定词，否则无法定位文档。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "用户的原始问题整句，保留省份、年度、单位名、类别等全部限定词",
                    },
                },
                "required": ["query"],
            },
        }
