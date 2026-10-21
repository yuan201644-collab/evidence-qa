"""图谱推理工具的 mock 实现（假结果、真接口）。

**这是本方案新增的第三个工具**，也是与导师建议（融合 OpenSPG + KAG）对接的落点。

**它和另外两个工具的本质区别**：
    nl2sql 查一行、doc_search 捞一段，都是「一次取回」。
    graph_reason 要**顺着关系走多跳**：客户 → 他的订单 → 订单对应的专辑 →
    相关人员的经验文档。走完再把路径和沿途原文一起带回来。

**为什么一定要先有 mock**：
    真实图谱能力 09-24 之后才有（3 天验证期在跑）。让编排层白等 3 天不可接受。
    mock 版把「接口形态」先钉死，编排层的规划、trace 合并、refs 去重、
    答案拼装全都能真跑真测 —— 这些逻辑与图谱真假无关。

**mock 的纪律（两条，违反了这三天就白跑了）**：
    ① **返回值必须含 trace 和 refs，不能只给 data**。编排层要处理的恰恰是
       「一次调用返回 2~3 条步骤、refs 怎么去重」，只给 data 等于没测到。
    ② **接口形态按最终版定**。将来换真实现时编排层不改代码，只换后端。

**跳数上限**：边界约定多跳**不超过 3 跳**。mock 里走 2 跳，跳过 1 跳的问题
    （直接命中文档）由 doc_search 负责，分工清晰。
"""

from __future__ import annotations

from typing import Any

from app.contracts import DocRef, SqlRef, ToolResult, TraceStep
from app.tools.base import CAPABILITY_GRAPH, BaseTool, ToolSpec


class MockGraphReasonTool(BaseTool):
    """图谱推理工具的假实现：假装走了 2 跳，路径与出处都是预置的。"""

    #: 跳数上限（与骨架边界 B2 一致），mock 也守着这个数，避免演示时超限
    MAX_HOPS = 3

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="graph_reason",
            capabilities=(CAPABILITY_GRAPH,),
            description=(
                "顺着知识图谱里的关系走多跳，回答需要「由 A 找到 B、再由 B 得出结论」的问题。"
                "适用于跨来源的串联问题：某人的业绩出自哪张单、他的做法写在哪儿。"
            ),
            input_schema={
                "question": "用户原话",
                "hint": "起点实体（如已查到的人名），可空",
                "max_hops": "最多走几跳，默认 3",
            },
            returns="结论 + 走过的路径（每跳跳到了哪）+ 沿途命中的原文出处",
            cost_hint="慢",
        )

    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question", "")
        start = payload.get("hint") or "李伟"
        max_hops = int(payload.get("max_hops") or self.MAX_HOPS)
        if max_hops > self.MAX_HOPS:
            max_hops = self.MAX_HOPS

        # ── 第 1 跳：从起点实体走到他的业务记录（业务库侧）────────────
        sql_ref = SqlRef(
            table="chinook.invoice",
            column="total",
            row_pk="customer_id=42",
            snippet="2024 年销售额第一名：李伟，¥128340.50",
        )
        # ── 第 2 跳：从业务记录走到「这个人的做法」所在的文档（语料侧）──
        doc_ref = DocRef(
            doc_id="KB-007_金牌销售经验分享",
            doc_title="金牌销售经验分享",
            page=3,
            section_path="第 2 章 > 2.1 老客户经营方法",
            snippet=(
                "李伟在分享中提到，他维护客户的核心做法是「先记账、再回访」："
                "每完成一笔订单，当天就在自己的表里记下客户的偏好与下一次联系时点。"
            ),
            char_start=1180,
            char_end=1180 + 66,
            bboxes=[(72.0, 180.0, 523.0, 300.0)],
            page_width=595.28,
            page_height=841.89,
        )

        # 两次工具调用 = 两条 TraceStep。这一步是 mock 最容易被偷懒跳过的：
        # 只返回 data 的话，「多条 trace 怎么合并」根本测不到。
        step_hop1 = TraceStep(
            step_id=1,
            tool=self.name,
            input={"question": question, "start_node": start, "hop": 1},
            output={
                "from": "客户:" + start,
                "relation": "is_top_seller_of_year",
                "to": "业绩:2024 销售冠军",
            },
            elapsed_ms=95.0,
            hit=[sql_ref],
        )
        step_hop2 = TraceStep(
            step_id=2,
            tool=self.name,
            input={"start_node": start, "hop": 2},
            output={
                "from": "业绩:2024 销售冠军",
                "relation": "method_described_in",
                "to": "文档:金牌销售经验分享",
            },
            elapsed_ms=120.0,
            hit=[doc_ref],
        )

        trace = [step_hop1, step_hop2]
        if max_hops < 2:
            # 被显式限制成 1 跳时，只回第一跳 —— 证明 max_hops 真的生效
            trace = [step_hop1]

        data = {
            "conclusion": (
                start + "的做法可以概括为「先记账、再回访」："
                "当天记录客户偏好与回访时点，每月固定回访，淡季主动推荐新品。"
            ),
            "hops_used": len(trace),
            "path": [s.output for s in trace],
        }
        refs = []
        for s in trace:
            for r in s.hit or []:
                refs.append(r)
        return ToolResult(data=data, trace=trace, refs=refs)
