"""nl2sql 工具的 mock 实现（假结果、真接口）。

**它在做什么**：拿到一句问题和一点上下文，吐出「一次结构化查询的结果」。
真实版本要做的是「自然语言转 SQL → 查库 → 回结果行」，现在先写死。

**为什么可以先写死**：编排层真正要处理的东西 —— 路由判断、trace 拼接、
refs 去重、答案拼装 —— 与 SQL 真假完全无关。把 SQL 换成真的，编排层
一行都不用改（前提是接口形态按最终版定，见下）。

**接口按最终形态定**（不是随便糊一个）：
    入参 payload 的字段名、返回 ToolResult 的三字段结构，
    与将来真实 nl2sql 工具**完全一致**。这是 Mock A 的纪律。
"""

from __future__ import annotations

from typing import Any

from app.contracts import SqlRef, ToolResult, TraceStep
from app.tools.base import CAPABILITY_TABLE, BaseTool, ToolSpec


class MockNl2SqlTool(BaseTool):
    """结构化查询工具的假实现。"""

    def __init__(self, table: str = "chinook.customer") -> None:
        # 写死一个「主力表」，让 mock 的结果看起来像真查了库
        self.table = table

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="nl2sql",
            capabilities=(CAPABILITY_TABLE,),
            description=(
                "查询业务数据库里的结构化数据（客户、订单、专辑、员工等）。"
                "适用于需要算出来的问题：谁排第一、一共多少、分组的汇总值。"
            ),
            input_schema={
                "question": "用户原话",
                "hint": "上游传下来的线索（如已经问到的某个人名），可空",
            },
            returns="结果行（每条是一组「字段名 -> 值」）",
            cost_hint="快",
        )

    def run(self, payload: dict[str, Any]) -> ToolResult:
        question = payload.get("question", "")
        hint = payload.get("hint") or ""

        # ── 假结果一：被要求查「销售冠军」──────────────────────────────
        if "冠军" in question or "第一" in question or "最好" in question:
            sql = (
                "SELECT c.first_name || ' ' || c.last_name AS name, "
                "SUM(i.total) AS total_sales "
                "FROM chinook.invoice i "
                "JOIN chinook.customer c ON c.customer_id = i.customer_id "
                "WHERE EXTRACT(YEAR FROM i.invoice_date) = 2024 "
                "GROUP BY c.customer_id, name ORDER BY total_sales DESC LIMIT 1"
            )
            data = [{"name": "李伟", "total_sales": 128340.5, "year": 2024}]
            ref = SqlRef(
                table="chinook.invoice",
                column="total",
                row_pk="customer_id=42",
                snippet="2024 年销售额第一名：李伟，¥128340.50",
            )
        # ── 假结果二：被问到某个人的业绩 ───────────────────────────────
        elif hint:
            sql = (
                "SELECT SUM(total) AS total_sales FROM chinook.invoice "
                "WHERE customer_id = (SELECT customer_id FROM chinook.customer "
                "WHERE first_name || ' ' || last_name = '"
                + hint
                + "')"
            )
            data = [{"name": hint, "total_sales": 128340.5}]
            ref = SqlRef(
                table="chinook.invoice",
                column="total",
                row_pk="customer_id=42",
                snippet="李伟 2024 年累计业绩：¥128340.50",
            )
        # ── 兜底：给个像样的空结果，别让编排层拿到 None ─────────────────
        else:
            sql = "SELECT COUNT(*) AS n FROM " + self.table
            data = [{"n": 59}]
            ref = SqlRef(
                table=self.table,
                column=None,
                row_pk=None,
                snippet="业务库中记录数：59 条",
            )

        step = TraceStep(
            step_id=1,
            tool=self.name,
            input={"question": question, "hint": hint},
            output={"row_count": len(data), "sql_preview": sql[:60] + "..."},
            elapsed_ms=42.0,
            hit=[ref],
        )
        return ToolResult(data=data, trace=[step], refs=[ref])

    # 真实实现里才有的东西，这里先把「形状」占位，避免下游写错假设
    def last_sql(self) -> str | None:
        """返回上一次生成的 SQL。编排层要把它填进 `AskResponse.sql`。"""
        return None
