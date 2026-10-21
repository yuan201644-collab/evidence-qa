"""文档检索工具的 mock 实现（假结果、真接口）。

**它在做什么**：拿到一句问题，在非结构化语料里找相关段落，回片段 + 引用。
真实版本是 B 已经写好的 `app/rag/answer.py`（检索 → 重排 → 生成 → 溯源）。

**为什么这里不直接用 B 的真实现**：
    验证期要的是「不依赖数据库、不依赖模型、随时可跑」的确定性链路。
    B 的实现需要 PDF 语料 + pgvector + 大模型接口，任何一环没配好就断。
    mock 版让编排层今天就能跑通，与 B 的进度解耦。

**接真的那天怎么换**：`MockDocSearchTool.run` 换成调 `RagAnswerTool.answer`，
    入参出参形状完全一致，编排层不用动。
"""

from __future__ import annotations

from typing import Any

from app.contracts import DocRef, ToolResult, TraceStep
from app.tools.base import CAPABILITY_DOC, BaseTool, ToolSpec


class MockDocSearchTool(BaseTool):
    """文档检索工具的假实现。"""

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="doc_search",
            capabilities=(CAPABILITY_DOC,),
            description=(
                "在合同、制度、报告等文稿里检索相关段落。"
                "适用于查得到、不用算的问题：某人怎么做的、条款怎么写的。"
            ),
            input_schema={
                "question": "用户原话",
                "hint": "限定检索范围或关键词（如某个人名），可空",
            },
            returns="相关文档片段（每段带出处：文档名、页码、章节）",
            cost_hint="中",
        )

    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question", "")
        hint = payload.get("hint") or ""

        # 假结果：假装在《金牌销售经验分享》里检索到了方法论
        snippet = (
            "李伟在分享中提到，他维护客户的核心做法是「先记账、再回访」："
            "每完成一笔订单，当天就在自己的表里记下客户的偏好与下一次联系时点；"
            "每月固定回访一次，淡季主动推新专辑试听。"
            "他说这样做的结果是老客户复购率明显高于团队平均。"
        )
        ref = DocRef(
            doc_id="KB-007_金牌销售经验分享",
            doc_title="金牌销售经验分享",
            page=3,
            section_path="第 2 章 > 2.1 老客户经营方法",
            snippet=snippet,
            char_start=1180,
            char_end=1180 + len(snippet),
            # 几何高亮三件套：前端靠它们画框（A4 纸的 point 尺寸）
            bboxes=[(72.0, 180.0, 523.0, 300.0)],
            page_width=595.28,
            page_height=841.89,
        )

        step = TraceStep(
            step_id=1,
            tool=self.name,
            input={"question": question, "hint": hint},
            output={"hit_count": 1, "doc_id": ref.doc_id},
            elapsed_ms=180.0,
            hit=[ref],
        )
        return ToolResult(data=[{"text": snippet, "doc_title": ref.doc_title}], trace=[step], refs=[ref])
