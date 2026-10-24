# -*- coding: utf-8 -*-
# Copyright 2026 The VeriMind Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""R6 的字面锚点检索器：绕开全文索引分析器，直接按编号查 Chunk。

它解决的是这样一个断链：KAG 平台默认全文索引使用 ``standard-no-stop-words``
分析器，"字母+数字" 连写串（``NYS212001``）不进入倒排表，``search_text``
按编号查恒为 0 命中；而向量检索无法区分只差一位的编号。于是"按编号问事实"
这一类问题在默认混合检索下必然召回为空，模型只能回答"未找到"。

本检索器的做法是**不改索引、不改分析器**，只加一路字面匹配：
从问题里抽出编号 → 在 Chunk 标签上做 ``CONTAINS`` → 按命中锚点数排序。
178 个 Chunk 的量级下这是毫秒级操作，代价可以接受；换到十万级语料就必须
换成专门的字面索引（例如 ``cjk`` 分析器或倒排表），这一点在文档里写清楚。

设计约束：
- **不修改上游文件**：通过注册表挂载（``@RetrieverABC.register``）。
- **不写死凭据**：连接信息优先取显式参数，其次环境变量，
  最后从同容器 JVM 的启动参数里读 OpenSPG 自己使用的图库地址。
- **失败不打断流水线**：任何异常都降到``err_msg``，返回空结果。
"""

import logging
import os
import re
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from kag.common.config import LogicFormConfiguration
from kag.interface import ChunkData, RetrieverABC, RetrieverOutput
from kag.interface.solver.model.schema_utils import SchemaUtils
from knext.schema.client import CHUNK_TYPE

try:  # contrib 目录可以脱离 KAG 单独拷走，这里不因为缺依赖就 import 失败
    from .anchor_query import extract_anchors, rank_hits
except ImportError:  # pragma: no cover - 单文件部署（直接放进 chunk_retriever 目录）
    from anchor_query import extract_anchors, rank_hits

logger = logging.getLogger(__name__)

__all__ = ["AnchorChunkRetriever"]

# 单次 CONTAINS 扫描的候选上限。178 个 Chunk 时全表命中也就几十行，
# 上限主要防止"锚点噪声 + 大语料"把内存打满。
_SCAN_LIMIT = 50

# OpenSPG 的图库地址写在 JVM 启动参数里（--cloudext.graphstore.url），
# 形如 neo4j://host:7687?user=..&password=..&database=..
_CMDLINE = "/proc/1/cmdline"
_GRAPHSTORE_ARG = re.compile(r"--cloudext\.(?:graphstore|searchengine)\.url=(\S+)")


def _discover_graph_connection() -> Dict[str, str]:
    """从同容器 JVM 的启动参数里还原图库连接信息。

    这样做的目的不是"取巧"，而是**避免把口令写进仓库**：密码本来就在
    OpenSPG 自己的启动参数里，这里只是复用同一份配置，不新增暴露面。
    读不到时返回空字典，由调用方报错。
    """
    try:
        with open(_CMDLINE, "rb") as fh:
            cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return {}

    m = _GRAPHSTORE_ARG.search(cmdline)
    if not m:
        return {}

    raw = m.group(1)
    parsed = urlparse(raw)
    qs = parse_qs(parsed.query)
    host = parsed.hostname or ""
    port = parsed.port or 7687
    return {
        # Python 驱动的 neo4j:// 走路由协议，单机实例要用 bolt://
        "uri": "bolt://%s:%d" % (host, port),
        "user": (qs.get("user") or [""])[0],
        "password": (qs.get("password") or [""])[0],
        "database": (qs.get("database") or ["neo4j"])[0],
    }


@RetrieverABC.register("anchor_chunk_retriever")
class AnchorChunkRetriever(RetrieverABC):
    """按编号字面量召回 Chunk 的补充检索器。

    Args:
        top_k: 返回条数上限。
        max_anchors: 单次最多使用几个编号锚点。
        neo4j_uri / neo4j_user / neo4j_password / neo4j_database:
            图库连接；不传则按 环境变量 → JVM 启动参数 的顺序回退。
        scan_limit: Cypher 扫描返回的候选行数上限。
    """

    def __init__(
        self,
        top_k: int = 5,
        max_anchors: int = 8,
        neo4j_uri: Optional[str] = None,
        neo4j_user: Optional[str] = None,
        neo4j_password: Optional[str] = None,
        neo4j_database: Optional[str] = None,
        scan_limit: int = _SCAN_LIMIT,
        **kwargs,
    ):
        super().__init__(top_k, **kwargs)
        self.max_anchors = max_anchors
        self.scan_limit = scan_limit

        discovered = _discover_graph_connection()
        self.neo4j_uri = neo4j_uri or os.environ.get("KAG_NEO4J_URI") or discovered.get("uri")
        self.neo4j_user = neo4j_user or os.environ.get("KAG_NEO4J_USER") or discovered.get("user")
        self.neo4j_password = (
            neo4j_password or os.environ.get("KAG_NEO4J_PASSWORD") or discovered.get("password")
        )
        # 显式 > 环境 > 由标签前缀推（Test1.Chunk → test1），最后退回 JVM 参数里的默认库
        self._explicit_database = (
            neo4j_database or os.environ.get("KAG_NEO4J_DATABASE") or None
        )
        self._default_database = discovered.get("database", "neo4j")

        self.schema_helper: SchemaUtils = SchemaUtils(
            LogicFormConfiguration(
                {
                    "KAG_PROJECT_ID": self.kag_project_config.project_id,
                    "KAG_PROJECT_HOST_ADDR": self.kag_project_config.host_addr,
                }
            )
        )
        self._driver = None

    # ------------------------------------------------------------------ 连接
    def _get_driver(self):
        if self._driver is None:
            if not (self.neo4j_uri and self.neo4j_user and self.neo4j_password):
                raise RuntimeError(
                    "anchor_chunk_retriever: 缺少图库连接信息"
                    "（可显式传 neo4j_uri/user/password，或设 KAG_NEO4J_* 环境变量）"
                )
            from neo4j import GraphDatabase

            self._driver = GraphDatabase.driver(
                self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password)
            )
        return self._driver

    def _chunk_label(self) -> str:
        """返回带命名空间前缀的 Chunk 标签，如 ``Test1.Chunk``。"""
        return self.schema_helper.get_label_within_prefix(CHUNK_TYPE)

    def _database(self) -> str:
        if self._explicit_database:
            return self._explicit_database
        label = self._chunk_label()
        prefix = label.split(".")[0] if "." in label else ""
        # OpenSPG 的库名规则：命名空间小写即库名（Test1 → test1）
        return prefix.lower() if prefix else self._default_database

    # ------------------------------------------------------------------ 检索
    def _scan(self, anchors: List[str]) -> List[Dict]:
        label = self._chunk_label()
        database = self._database()
        # 标签来自服务端 schema（非用户输入），此处插值安全；锚点一律走参数。
        cypher = (
            "MATCH (c:`%s`) "
            "WHERE any(a IN $anchors WHERE c.content CONTAINS a) "
            "RETURN c.id AS id, c.name AS name, c.content AS content "
            "LIMIT $limit" % label
        )
        driver = self._get_driver()
        with driver.session(database=database) as session:
            result = session.run(cypher, anchors=anchors, limit=self.scan_limit)
            return [dict(record) for record in result]

    def invoke(self, task, **kwargs) -> RetrieverOutput:
        top_k = kwargs.get("top_k", self.top_k)
        arguments = getattr(task, "arguments", None) or {}
        query = arguments.get("query") or ""
        anchors = extract_anchors(query, max_anchors=self.max_anchors)
        if not anchors:
            # 没有编号就不要多扫一遍全表：这是常态路径，必须足够便宜。
            return RetrieverOutput(
                chunks=[],
                retriever_method=self.name,
                summary="no anchor in query",
            )

        try:
            rows = self._scan(anchors)
        except Exception as e:  # 图库抖动不应该让整条问答链路失败
            logger.error("anchor_chunk_retriever scan failed: %s", e, exc_info=True)
            return RetrieverOutput(retriever_method=self.name, err_msg=str(e))

        ranked = rank_hits(anchors, rows)
        chunks: List[ChunkData] = []
        for row, score, matched in ranked[:top_k]:
            chunks.append(
                ChunkData(
                    content=row.get("content") or "",
                    title=row.get("name") or "",
                    chunk_id=row.get("id") or "",
                    score=float(score),
                )
            )
        logger.info(
            "anchor_chunk_retriever: anchors=%s hits=%d returned=%d",
            anchors,
            len(rows),
            len(chunks),
        )
        return RetrieverOutput(
            chunks=chunks,
            retriever_method=self.name,
            summary="anchors=%s" % ",".join(anchors),
        )

    def schema(self):
        return {
            "name": "anchor_chunk_retriever",
            "description": (
                "Retrieve text chunks by literal identifier match "
                "(e.g. NYS212001, HT-MNT-2026-001) when the full-text index "
                "does not tokenize alphanumeric identifiers"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Question or sub-query containing identifiers",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of top results to return",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        }
