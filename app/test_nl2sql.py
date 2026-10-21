"""结构化链路自检：校验闸门、生成器剥壳、pipeline 编排、真库执行。

跑法（项目根目录下）：
    D:/python/python.exe app/test_nl2sql.py

测什么（每条都对应一个容易写错、且写错了后果不轻的点）：

    一、离线（不需要大模型、不需要数据库）
        1. 库结构目录能加载，表/字段判断正确
        2. **校验闸门挡得住写操作**（这是硬规范，挡不住就是安全事故）
        3. 校验闸门**不误杀**正确的复杂查询（CTE / 子查询 / 别名 / JOIN）
        4. 补 LIMIT 只在没有 GROUP BY 时发生
        5. 生成器的剥壳：代码块 / 标签 / 前言的混合形态
        6. 生成失败被收敛成错误值，**不抛异常**（抛出去就是 HTTP 500）
        7. pipeline：校验失败会带错误信息重试；执行失败**按错误码判**要不要重试；不伪造引用
        8. 真工具返回完整 ToolResult（三字段齐）
        9. 编排层能把 SQL 填进 AskResponse.sql

    二、真库（数据库连不上就自动跳过）
       10. gold SQL 全部能过校验闸门（**不误杀**的实证）
       11. gold SQL 走完整 pipeline 执行，结果与直连执行逐行一致
       12. 执行器带出 SQLSTATE（重试判据表的实测依据）
       13. 真库里执行失败 → 回灌报错 → 自愈（端到端）

为什么第 10、11 条值得单独写：校验器最危险的失败模式不是"漏放"（后面还有
数据库自己兜），而是**误杀** —— 一条完全正确的 SQL 被判不通过，链路就永远
答不出这道题，而且报错信息看着还很"有道理"。用评测集里 15 条人工写定的
gold SQL 当靶子，是能拿到的最强证据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.contracts import ToolResult                      # noqa: E402
from app.nl2sql.executor import ExecutionResult, SqlExecutor  # noqa: E402
from app.nl2sql.generator import build_prompts, extract_sql, generate_sql  # noqa: E402
from app.nl2sql.pipeline import (                        # noqa: E402
    Nl2SqlPipeline,
    build_conclusion,
    build_sql_refs,
    is_retryable_execution_error,
)
from app.nl2sql.schema import SchemaCatalog               # noqa: E402
from app.nl2sql.validator import is_read_only, validate_sql  # noqa: E402
from app.tools.nl2sql import Nl2SqlTool, build_nl2sql_tool  # noqa: E402

PASSED = 0
SKIPPED: list[str] = []


# ============================================================================
# 测试替身（假件）
# ============================================================================


class ScriptedClient:
    """按剧本回复的大模型假件。剧本里的元素可以是 str（当回答）或 Exception（当故障）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def chat(self, system, user):
        self.calls.append((system, user))
        if not self.replies:
            raise AssertionError("假件剧本已用完：链路多调了一次大模型")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeExecutor:
    """不连库的执行器假件。`sqlstate` 用来模拟不同类别的数据库报错。"""

    def __init__(self, rows=None, columns=None, error=None, sqlstate=None):
        self.rows = rows if rows is not None else []
        self.columns = columns if columns is not None else []
        self.error = error
        self.sqlstate = sqlstate
        self.seen: list[str] = []

    def execute(self, sql):
        self.seen.append(sql)
        return ExecutionResult(
            rows=list(self.rows),
            columns=list(self.columns),
            row_count=len(self.rows),
            elapsed_ms=1.0,
            error=self.error,
            sqlstate=self.sqlstate,
        )


def tiny_catalog() -> SchemaCatalog:
    """两张表的小目录。用它跑校验，测试不依赖 infra/schema_meta.json 的具体内容。"""
    return SchemaCatalog.from_dict(
        {
            "database": "chinook",
            "dialect": "postgres",
            "contract_version": "test",
            "tables": [
                {
                    "table": "customer",
                    "schema": "public",
                    "description": "客户",
                    "primary_key": ["customer_id"],
                    "columns": [
                        {"name": "customer_id", "type": "INTEGER", "is_primary_key": True,
                         "nullable": False, "description": "客户编号"},
                        {"name": "first_name", "type": "TEXT", "description": "名"},
                        {"name": "last_name", "type": "TEXT", "description": "姓"},
                    ],
                    "foreign_keys": [],
                },
                {
                    "table": "invoice",
                    "schema": "public",
                    "description": "发票",
                    "primary_key": ["invoice_id"],
                    "columns": [
                        {"name": "invoice_id", "type": "INTEGER", "is_primary_key": True,
                         "nullable": False, "description": "发票编号"},
                        {"name": "customer_id", "type": "INTEGER", "description": "客户编号"},
                        {"name": "total", "type": "NUMERIC", "description": "金额合计"},
                    ],
                    "foreign_keys": [
                        {"columns": ["customer_id"],
                         "references": {"table": "customer", "columns": ["customer_id"]}}
                    ],
                },
            ],
        }
    )


def check(name: str, fn) -> None:
    global PASSED
    try:
        fn()
    except AssertionError as e:
        print(f"[FAIL] {name}\n       {e}")
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {name}\n        {type(e).__name__}: {e}")
        raise
    PASSED += 1
    print(f"[ ok ] {name}")


# ============================================================================
# 一、离线
# ============================================================================


def test_schema_catalog() -> None:
    cat = tiny_catalog()
    assert cat.table_names() == ["customer", "invoice"], cat.table_names()
    assert cat.has_table("CUSTOMER"), "表名判断应当大小写不敏感"
    assert not cat.has_table("orders")
    assert cat.has_column("invoice", "Total"), "字段判断应当大小写不敏感"
    assert not cat.has_column("invoice", "amount")
    assert cat.owner_tables("customer_id") == ["customer", "invoice"], cat.owner_tables("customer_id")
    ddl = cat.render_ddl()
    assert "客户编号" in ddl, "渲染的库结构里必须带中文业务说明（模型靠它做表定位）"
    assert "→ customer(customer_id)" in ddl, "外键指向要渲染出来（多表关联靠它）"


def test_validator_blocks_writes() -> None:
    cat = tiny_catalog()
    for bad in [
        "INSERT INTO customer (customer_id) VALUES (1)",
        "UPDATE customer SET first_name = 'x'",
        "DELETE FROM customer",
        "DROP TABLE customer",
        "ALTER TABLE customer ADD COLUMN x INT",
        "TRUNCATE TABLE customer",
        "SELECT * INTO new_customer FROM customer",
        "SELECT customer_id FROM customer; DELETE FROM customer",
    ]:
        result = validate_sql(bad, cat)
        assert not result.ok, f"这条必须被挡下：{bad}"
    assert not is_read_only("DELETE FROM customer")
    assert not is_read_only("SELECT 1; SELECT 2"), "多语句一律不算只读"


def test_validator_blocks_unknown_names() -> None:
    cat = tiny_catalog()
    r = validate_sql("SELECT customer_id FROM orders", cat)
    assert not r.ok and "orders" in r.error, r.error
    r = validate_sql("SELECT amount FROM invoice", cat)
    assert not r.ok and "amount" in r.error, r.error


def test_validator_does_not_false_reject() -> None:
    """**最要紧的一条**：正确的复杂查询不能被误杀。"""
    cat = tiny_catalog()
    good = [
        "SELECT count(*) AS customer_count FROM customer",
        "SELECT c.first_name, sum(i.total) AS amount FROM customer c JOIN invoice i ON i.customer_id = c.customer_id GROUP BY c.customer_id, c.first_name",
        "WITH big AS (SELECT customer_id FROM invoice WHERE total > 10) SELECT c.first_name FROM customer c JOIN big b ON b.customer_id = c.customer_id",
        "SELECT customer_id FROM invoice WHERE customer_id IN (SELECT customer_id FROM customer WHERE last_name = 'x')",
        "SELECT count(*) AS n FROM customer WHERE EXISTS (SELECT 1 FROM invoice WHERE invoice.customer_id = customer.customer_id)",
        "SELECT first_name || ' ' || last_name AS full_name FROM customer",
        "SELECT customer_id, count(*) AS n FROM invoice GROUP BY customer_id ORDER BY n DESC",
    ]
    for sql in good:
        r = validate_sql(sql, cat, auto_limit=0)
        assert r.ok, f"这条是正确的，不该被挡：{sql}\n原因：{r.error}"


def test_auto_limit() -> None:
    cat = tiny_catalog()
    r = validate_sql("SELECT customer_id FROM customer", cat, auto_limit=100)
    assert r.ok and "LIMIT 100" in r.sql.upper(), r.sql
    assert r.notes, "补了 LIMIT 必须留痕（否则结果被截断却没人知道）"

    r = validate_sql("SELECT customer_id FROM customer", cat, auto_limit=0)
    assert "LIMIT" not in r.sql.upper(), "auto_limit=0 表示不补"

    r = validate_sql("SELECT customer_id, count(*) AS n FROM invoice GROUP BY customer_id", cat, auto_limit=100)
    assert "LIMIT" not in r.sql.upper(), "有 GROUP BY 时不该补 LIMIT（结果集已被分组收敛）"

    r = validate_sql("SELECT customer_id FROM customer LIMIT 5", cat, auto_limit=100)
    assert r.sql.upper().endswith("LIMIT 5"), "已有 LIMIT 时不应被覆盖"


def test_extract_sql() -> None:
    assert extract_sql("SELECT 1") == "SELECT 1"
    assert extract_sql("```sql\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"
    assert extract_sql("好的，以下是 SQL：\nSELECT 1") == "SELECT 1"
    assert extract_sql("<sql>SELECT 1</sql>") == "SELECT 1"
    assert extract_sql("SQL: SELECT 1") == "SELECT 1"
    assert extract_sql("") == ""
    assert extract_sql("抱歉，我无法回答这个问题。") == "", "没有 SQL 就该返回空，而不是把整段话当 SQL"


def test_build_prompts() -> None:
    cat = tiny_catalog()
    system, user = build_prompts("一共多少客户", cat)
    assert "PostgreSQL" in system and "只输出" in system
    assert "库结构" in user and "customer" in user
    assert "一共多少客户" in user

    _, user2 = build_prompts("x", cat, hint="李伟", previous_error="表 customer 里没有字段 name")
    assert "李伟" in user2, "线索要进提示词（多跳链路靠它）"
    assert "表 customer 里没有字段 name" in user2, "失败原因要回灌，否则重试等于再猜一次"


def test_generate_sql_offline() -> None:
    cat = tiny_catalog()

    client = ScriptedClient(["```sql\nSELECT count(*) AS n FROM customer\n```"])
    gen = generate_sql("多少客户", cat, client)
    assert gen.error is None and gen.sql == "SELECT count(*) AS n FROM customer", gen
    assert gen.notes, "剥离了 Markdown 要留痕"

    # 大模型故障 → 收敛成 error，不抛异常
    gen = generate_sql("x", cat, ScriptedClient([RuntimeError("连接超时")]))
    assert gen.error and "连接超时" in gen.error, gen

    # 输出里没有 SQL → 也算失败，不能把整段话交给校验器
    gen = generate_sql("x", cat, ScriptedClient(["我无法回答这个问题。"]))
    assert gen.error is not None and gen.sql is None, gen


def test_pipeline_happy_path() -> None:
    cat = tiny_catalog()
    client = ScriptedClient(["SELECT customer_id, first_name FROM customer"])
    executor = FakeExecutor(
        rows=[{"customer_id": 1, "first_name": "Luís"}, {"customer_id": 2, "first_name": "Leonie"}],
        columns=["customer_id", "first_name"],
    )
    # 本例验证包含表字段定位的完整路径，显式启用，不依赖运行机配置。
    pipeline = Nl2SqlPipeline(
        client, catalog=cat, executor=executor, auto_limit=0,
        semantic_planning_enabled=True,
    )
    outcome = pipeline.run("列出客户")

    assert outcome.error is None, outcome.error
    assert outcome.attempts == 1
    assert outcome.sql == "SELECT customer_id, first_name FROM customer"
    assert outcome.row_count == 2
    # 链路后来加了定位/关联规划/语义校验三组步骤（schema_linker、join_planner、
    # sql_semantic_validator），不再是早期「生成 / 校验 / 执行」三步。这里只钉住
    # 与 SQL 直接相关的部分，不锁死总步数与绝对下标，免得每加一步就整片变红。
    tools = [s.tool for s in outcome.trace]
    assert tools[0] == "schema_linker", tools
    assert tools.count("nl2sql") >= 3, tools
    assert [s.step_id for s in outcome.trace] == list(range(1, len(tools) + 1)), \
        "step_id 必须从 1 连续编号"
    assert any(s.output.get("sql") == outcome.sql for s in outcome.trace), \
        "生成步要记下 SQL（可解释性的单一事实来源）"
    assert any(s.output.get("ok") is True for s in outcome.trace), "校验步要记下 ok"
    assert any(s.output.get("row_count") == 2 for s in outcome.trace), "执行步要记下行数"
    # 结果里含主键 → 逐行引用
    assert len(outcome.refs) == 2 and all(r.row_pk for r in outcome.refs), outcome.refs
    assert outcome.refs[0].table == "customer"
    assert outcome.refs[0].row_pk == "customer_id=1", outcome.refs[0].row_pk


def test_pipeline_retries_on_invalid_sql() -> None:
    cat = tiny_catalog()
    client = ScriptedClient([
        "SELECT first_name FROM orders",              # 表不存在 → 校验不过
        "SELECT first_name FROM customer",            # 修正后通过
    ])
    executor = FakeExecutor(rows=[], columns=["first_name"])
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=executor, auto_limit=0)
    outcome = pipeline.run("列出客户姓名")

    assert outcome.attempts == 2, "校验失败要重试"
    assert outcome.error is None, outcome.error
    assert len(client.calls) == 2
    # 第二次的提示词里必须带着上一次的失败原因
    assert "orders" in client.calls[1][1], "重试时没把失败原因回灌给模型"
    # 失败的那一步也要留在 trace 里
    errors = [s.output.get("error") for s in outcome.trace if s.output.get("error")]
    assert errors, "被否决的那次校验必须在 trace 里留痕"


def test_pipeline_gives_up_after_max_attempts() -> None:
    cat = tiny_catalog()
    client = ScriptedClient(["SELECT 1 FROM orders", "SELECT 1 FROM orders", "SELECT 1 FROM orders"])
    executor = FakeExecutor()
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=executor, max_attempts=3, auto_limit=0)
    outcome = pipeline.run("x")

    assert outcome.sql is None and outcome.error, outcome
    assert outcome.attempts == 3
    assert executor.seen == [], "没过校验的 SQL 绝不能送到数据库"
    # 每次尝试都留「生成 + 校验」两步；定位/规划/语义校验等固定步骤另算，
    # 所以只数生成次数，不钉死总步数。
    assert [s.tool for s in outcome.trace].count("nl2sql") == 3 * 2, outcome.trace


def test_pipeline_generation_failure_no_retry() -> None:
    """生成层面失败（网络/空输出）不重试 —— 成因不在提示词里，重试没有信息量。"""
    cat = tiny_catalog()
    client = ScriptedClient([RuntimeError("网络不可达"), "SELECT 1 FROM customer"])
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=FakeExecutor(), auto_limit=0)
    outcome = pipeline.run("x")

    assert outcome.error and "网络不可达" in outcome.error, outcome
    assert outcome.attempts == 1
    assert len(client.calls) == 1, "不该再调一次大模型"


def test_pipeline_execution_failure_has_no_fake_refs() -> None:
    """执行失败时**不能**造一条「没有命中任何行」的引用 —— 那是伪造证据。

    顺带锁住一条保守默认：假件没给 SQLSTATE（= 拿不到错误码，连接层故障就长这样），
    一律按"不可重试"处理，不许白白多调一次大模型。
    """
    cat = tiny_catalog()
    client = ScriptedClient(["SELECT customer_id FROM customer"])
    executor = FakeExecutor(error='SQL 执行失败：column "x" does not exist')
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=executor, auto_limit=0)
    outcome = pipeline.run("x")

    assert outcome.error and "does not exist" in outcome.error
    assert outcome.refs == [], "执行都没成功，不该有引用"
    assert any(s.output.get("error") for s in outcome.trace), "错误要记进 trace"
    assert outcome.attempts == 1 and len(client.calls) == 1, "拿不到 SQLSTATE 时不该重试"
    assert outcome.sql == "SELECT customer_id FROM customer", "执行过的 SQL 要保留（trace 要对得上）"


def test_retry_classification() -> None:
    """重试判据：**写错了**重试、**跑不了**不重试、**拿不到 SQLSTATE** 不重试。

    表里这些码全部是在本机真库上实测出来的（见 test_executor_reports_real_sqlstate），
    不是从文档抄的。
    """
    def result(code):
        return ExecutionResult(error="x", sqlstate=code)

    for code in ("42703",   # 字段不存在
                 "42P01",   # 表/别名未定义（h-05 那句 missing FROM-clause entry）
                 "42803",   # GROUP BY 漏列
                 "42601",   # 语法错
                 "22P02",   # 类型不匹配（h-04 那句 numeric 与空串混用）
                 "22012",   # 除零
                 "21000"):  # 子查询返回多行
        assert is_retryable_execution_error(result(code)), code + " 属于模型能改对的类型，应当重试"

    for code in ("57014",   # 语句超时：改写法也没用（是我们自己的 statement_timeout 掐的）
                 "25006",   # 只读闸门拒写：安全边界在生效，不该鼓励模型换个写法再试
                 "42501",   # 权限不足（也落在 42 类里，属于必须排除的例外）
                 "53300",   # 连接数超限
                 "3D000",   # 库不存在
                 None,      # 连接层故障根本拿不到码
                 ""):
        assert not is_retryable_execution_error(result(code)), str(code) + " 不该重试"


def test_pipeline_retries_on_retryable_execution_error() -> None:
    """执行失败**可重试**：数据库报错原文要回灌，让模型把 SQL 改对。

    这是 2026-10-01 补的缺口 —— 此前执行写在重试循环**之外**、只做一次，
    而库的报错原文恰恰是修复重试最有价值的那份输入。
    """
    cat = tiny_catalog()
    client = ScriptedClient([
        "SELECT customer_id, first_name FROM customer",   # 第一次：库会拒绝
        "SELECT customer_id FROM customer",               # 第二次：改对
    ])

    class FlakyExecutor(FakeExecutor):
        """第一次回 42703（字段不存在），之后就正常。"""

        def execute(self, sql):
            self.seen.append(sql)
            if len(self.seen) == 1:
                return ExecutionResult(
                    elapsed_ms=1.0,
                    error='SQL 执行失败：column "amount" does not exist',
                    sqlstate="42703",
                )
            return ExecutionResult(
                rows=[{"customer_id": 1}], columns=["customer_id"],
                row_count=1, elapsed_ms=1.0,
            )

    executor = FlakyExecutor()
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=executor, auto_limit=0)
    outcome = pipeline.run("列出客户")

    assert outcome.error is None, outcome.error
    assert outcome.attempts == 2, "执行失败属于可修类型时必须重试"
    assert len(client.calls) == 2
    assert "does not exist" in client.calls[1][1], "数据库的报错原文必须回灌给模型"
    assert "数据库执行时报错" in client.calls[1][1], "要说清这是执行层报的错，不是静态校验没过"
    # 两轮各自的「生成 / 校验 / 执行」共 6 次 nl2sql；定位/规划等固定步骤另算，
    # 所以按 tool 过滤后再数，不钉死总步数、也不按绝对下标取步。
    nl2sql_steps = [s for s in outcome.trace if s.tool == "nl2sql"]
    assert len(nl2sql_steps) == 6, [s.output for s in outcome.trace]
    failed = [s for s in nl2sql_steps if s.output.get("sqlstate") == "42703"]
    assert failed, "错误码要记进 trace"
    assert failed[0].output.get("error")
    assert failed[0].hit is None, "失败的那次执行不该带引用"
    assert any(s.output.get("row_count") == 1 for s in nl2sql_steps)
    assert len(executor.seen) == 2
    assert outcome.refs and outcome.refs[0].row_pk == "customer_id=1"


def test_pipeline_does_not_retry_unfixable_execution_error() -> None:
    """跑不动的错误不重试：重写再多遍也是同样结果，还白烧一次调用。"""
    cat = tiny_catalog()
    client = ScriptedClient([
        "SELECT customer_id FROM customer",
        "SELECT customer_id FROM customer",   # 多备一条：真被调用就说明不该重试的也重试了
    ])
    executor = FakeExecutor(
        error="SQL 执行失败：canceling statement due to statement timeout",
        sqlstate="57014",
    )
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=executor, auto_limit=0)
    outcome = pipeline.run("列出客户")

    assert outcome.error and "timeout" in outcome.error
    assert outcome.attempts == 1, "超时不该重试"
    assert len(client.calls) == 1, "不该再调一次大模型"
    assert len(executor.seen) == 1
    assert outcome.trace[-1].output.get("sqlstate") == "57014"


def test_refs_rules() -> None:
    cat = tiny_catalog()

    # ① 含主键 → 逐行
    refs = build_sql_refs(["customer"], cat, [{"customer_id": 7, "first_name": "a"}],
                          ["customer_id", "first_name"],
                          sql="SELECT customer_id, first_name FROM customer")
    assert len(refs) == 1 and refs[0].row_pk == "customer_id=7"

    # ② 不含主键 → 整体一条，且 column 不能填聚合别名（那是伪造字段命中）
    refs = build_sql_refs(["customer"], cat, [{"customer_count": 59}], ["customer_count"])
    assert len(refs) == 1 and refs[0].row_pk is None
    assert refs[0].column is None, "customer_count 是聚合别名，不是表字段，不该填进 column"
    assert refs[0].snippet, "snippet 必须非空（评测硬要求）"

    # ②' JOIN 多表 → 每张来源表一条（漏了就是把证据抹掉）
    refs = build_sql_refs(["customer", "invoice"], cat, [{"n": 3}], ["n"])
    assert [r.table for r in refs] == ["customer", "invoice"], refs

    # ③ 空结果 → 仍给引用
    refs = build_sql_refs(["invoice"], cat, [], ["total"])
    assert len(refs) == 1 and "没有符合条件" in refs[0].snippet, refs

    # ④ 没有任何表引用（如 SELECT 1）→ 不编造来源
    assert build_sql_refs([], cat, [{"n": 1}], ["n"]) == []


def test_conclusion() -> None:
    assert build_conclusion([], []).startswith("没有查到")
    assert "59" in build_conclusion([{"customer_count": 59}], ["customer_count"])
    text = build_conclusion([{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}], ["a"], max_preview=3)
    assert "共 4 行" in text and "…" in text, text
    assert build_conclusion([], [], error="连接失败").startswith("这条问题没能算出结果")


def test_conclusion_keeps_aggregate_value_when_it_falls_after_preview_columns() -> None:
    columns = [
        "customer_id", "first_name", "last_name", "city", "country", "phone",
        "email", "total_spent",
    ]
    row = dict(zip(columns, [16, "Frank", "Harris", "Mountain View", "USA",
                             "+1-555-0100", "frank@example.test", 37.62]))
    sql = (
        "SELECT c.customer_id, c.first_name, c.last_name, c.city, c.country, "
        "c.phone, c.email, COALESCE(SUM(i.total), 0) AS total_spent "
        "FROM customer c LEFT JOIN invoice i ON i.customer_id = c.customer_id "
        "GROUP BY c.customer_id, c.first_name, c.last_name, c.city, c.country, "
        "c.phone, c.email"
    )

    conclusion = build_conclusion([row], columns, sql=sql)

    assert "total_spent：37.62" in conclusion


def test_tool_returns_full_result() -> None:
    cat = tiny_catalog()
    client = ScriptedClient(["SELECT customer_id, first_name FROM customer"])
    executor = FakeExecutor(rows=[{"customer_id": 1, "first_name": "Luís"}],
                            columns=["customer_id", "first_name"])
    pipeline = Nl2SqlPipeline(client, catalog=cat, executor=executor, auto_limit=0)
    tool = Nl2SqlTool(pipeline)

    assert tool.spec.name == "nl2sql", "工具名必须与 mock 一致（改历史记录就废了）"
    result = tool.run({"question": "列出客户", "hint": ""})
    assert isinstance(result, ToolResult)
    assert len(result.trace) >= 1 and len(result.refs) >= 1
    # 工具名集合随链路演进（新增 schema_linker / join_planner /
    # sql_semantic_validator / sql_requirement_validator 等步骤），
    # 这里只要求「生成/执行」仍由 nl2sql 承担，且没有来路不明的工具混进来。
    tools = {s.tool for s in result.trace}
    assert "nl2sql" in tools, tools
    assert tools <= {
        "nl2sql", "schema_linker", "join_planner", "sql_semantic_validator",
        "sql_requirement_validator",
    }, tools
    assert all(r.snippet for r in result.refs)
    assert result.data["conclusion"] and result.data["rows"], result.data
    assert result.data["sql"] == "SELECT customer_id, first_name FROM customer"
    assert tool.last_sql() == result.data["sql"]

    # 序列化往返（HTTP 会做这件事）
    back = ToolResult.model_validate_json(result.model_dump_json())
    assert back.data["row_count"] == 1


def test_tool_factory() -> None:
    from app.tools.mock_nl2sql import MockNl2SqlTool

    assert isinstance(build_nl2sql_tool("mock"), MockNl2SqlTool)
    assert isinstance(build_nl2sql_tool("real"), Nl2SqlTool)
    try:
        build_nl2sql_tool("乱写的值")
    except ValueError as e:
        assert "real" in str(e) and "mock" in str(e), e
    else:
        raise AssertionError("非法的后端名必须报错，不能静默回落")


def test_orchestrator_wiring_and_sql_field() -> None:
    """编排层：后端开关生效；**SQL 能一路走到 AskResponse.sql**。

    第二条是 2026-10-01 修的一个真实缺陷：`_extract_sql` 原先读 ToolResult 上
    不存在的属性，导致响应体的 `sql` 恒为 None —— 而评测脚本正是从那里取
    预测 SQL 的，等于真实链路没法被打分。
    """
    from app.orchestrator import Orchestrator, build_default_orchestrator
    from app.tools.base import BaseTool, ToolSpec
    from app.tools.mock_nl2sql import MockNl2SqlTool
    from app.tools.registry import ToolRegistry
    from app.contracts import SqlRef, TraceStep

    assert isinstance(
        build_default_orchestrator(doc_search_backend="mock", nl2sql_backend="mock")
        .registry.require("nl2sql"),
        MockNl2SqlTool,
    )
    assert isinstance(
        build_default_orchestrator(doc_search_backend="mock", nl2sql_backend="real")
        .registry.require("nl2sql"),
        Nl2SqlTool,
    )

    class StubTool(BaseTool):
        @property
        def spec(self):
            return ToolSpec(
                name="nl2sql",
                capabilities=("structured_table",),
                description="测试替身",
                input_schema={"question": "问题"},
            )

        def run(self, payload):
            ref = SqlRef(table="customer", column="customer_id", row_pk="customer_id=1", snippet="一条证据")
            step = TraceStep(
                step_id=1, tool="nl2sql",
                input={"question": payload.get("question")},
                output={"sql": "SELECT customer_id FROM customer", "row_count": 1},
                elapsed_ms=3.0, hit=[ref],
            )
            return ToolResult(
                data={"conclusion": "查询结果：customer_id：1。", "rows": [{"customer_id": 1}],
                      "columns": ["customer_id"], "row_count": 1,
                      "sql": "SELECT customer_id FROM customer"},
                trace=[step], refs=[ref],
            )

    registry = ToolRegistry()
    registry.register(StubTool())
    orch = Orchestrator(registry)
    resp = orch.answer("一共有多少位客户", "s-nl2sql")
    assert resp.sql == "SELECT customer_id FROM customer", resp.sql
    assert resp.answer and "customer_id" in resp.answer, resp.answer
    assert len(resp.refs) == 1 and resp.refs[0].snippet


# ============================================================================
# 二、真库（连不上就跳过）
# ============================================================================

_REAL_CATALOG = None
_REAL_CASES = None


def _real_catalog():
    global _REAL_CATALOG
    if _REAL_CATALOG is None:
        _REAL_CATALOG = SchemaCatalog.load()
    return _REAL_CATALOG


def _load_cases():
    global _REAL_CASES
    if _REAL_CASES is None:
        path = ROOT / "eval" / "cases" / "nl2sql.jsonl"
        _REAL_CASES = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return _REAL_CASES


def _db_or_skip(label):
    try:
        version = SqlExecutor().ping()
    except Exception as exc:  # noqa: BLE001
        SKIPPED.append(label + "（" + type(exc).__name__ + "：" + str(exc).strip().splitlines()[0][:120] + "）")
        return False
    print("       数据库：" + version.split(",")[0])
    return True


def test_gold_sql_passes_validator() -> None:
    """不误杀的实证：15 条人工写定的 gold SQL 必须全部过闸门。"""
    if not _db_or_skip("gold SQL 过校验闸门"):
        return
    catalog = _real_catalog()
    public_tables = {
        name for name, table in catalog.tables.items()
        if table.schema.casefold() == "public"
    }
    assert len(public_tables) == 11, "Chinook public schema 应有 11 张表，实际 " + str(len(public_tables))

    bad = []
    for case in _load_cases():
        result = validate_sql(case["gold_sql"], catalog, dialect="postgres", auto_limit=0)
        if not result.ok:
            bad.append((case["id"], result.error))
    assert not bad, "校验器误杀了正确的 SQL：" + json.dumps(bad, ensure_ascii=False)


def test_gold_sql_pipeline_end_to_end() -> None:
    """把 gold SQL 当"模型输出"喂进完整链路：校验 → 真库执行 → 与直连结果逐行比对。

    这样验证不需要任何大模型 API key，却覆盖了整条链路里最关键的两步。
    """
    if not _db_or_skip("gold SQL 端到端（校验 + 真库执行）"):
        return
    catalog = _real_catalog()
    cases = _load_cases()
    executor = SqlExecutor()
    mismatched = []
    empty = []

    for case in cases:
        pipeline = Nl2SqlPipeline(
            ScriptedClient([case["gold_sql"]]),
            catalog=catalog,
            executor=executor,
            auto_limit=0,
        )
        outcome = pipeline.run(case["question"])
        if outcome.error:
            mismatched.append((case["id"], outcome.error))
            continue
        direct = executor.execute(case["gold_sql"])
        if outcome.rows != direct.rows:
            mismatched.append((case["id"], "与直连执行结果不一致"))
            continue
        if not outcome.refs:
            mismatched.append((case["id"], "没有产生引用（评测硬要求）"))
            continue
        if outcome.row_count == 0:
            empty.append(case["id"])
        assert outcome.sql == case["gold_sql"]

    assert not mismatched, "端到端比对失败：" + json.dumps(mismatched, ensure_ascii=False)
    assert not empty, "这些用例的结果集为空，可能是数据或 SQL 有问题：" + str(empty)


def test_executor_is_readonly_and_times_out() -> None:
    """数据库侧的两道安全带：只读连接真的拒绝写；语句超时会生效。"""
    if not _db_or_skip("执行器只读与超时"):
        return
    executor = SqlExecutor()
    result = executor.execute("CREATE TABLE should_not_exist (id int)")
    assert result.error, "只读连接必须拒绝建表"
    assert "read-only" in result.error.lower() or "只读" in result.error, result.error

    # 超时：主动设一个极短的上限，跑一条必然超时的语句
    slow = SqlExecutor(timeout_seconds=1)
    timed = slow.execute("SELECT pg_sleep(5)")
    assert timed.error, "pg_sleep(5) 在 1 秒超时下必须被中断"
    assert not timed.rows


def test_executor_reports_real_sqlstate() -> None:
    """真库实测：执行器必须把 SQLSTATE 带出来 —— 上面整张重试判据表都建立在它之上。

    这里断言的码是本机 PostgreSQL 16 的实测值，不是从文档抄的。
    一并证伪两件事：只读闸门拒写（25006）与语句超时（57014）**都在 42/22 之外或
    属于必须排除的例外** —— 也就是说"按类别放行"这套判据不会误把它们当成可重试。
    """
    if not _db_or_skip("执行器 SQLSTATE"):
        return
    executor = SqlExecutor()

    r = executor.execute("SELECT amount FROM invoice")
    assert r.error and r.sqlstate == "42703", (r.error, r.sqlstate)
    assert is_retryable_execution_error(r)

    r = executor.execute("SELECT COALESCE(total, '') FROM invoice")
    assert r.error and r.sqlstate == "22P02", (r.error, r.sqlstate)
    assert is_retryable_execution_error(r)

    r = executor.execute("CREATE TABLE probe_sqlstate (id int)")
    assert r.error and r.sqlstate == "25006", (r.error, r.sqlstate)
    assert not is_retryable_execution_error(r), "只读闸门拒写不该被当成可重试（那是安全边界）"

    r = SqlExecutor(timeout_seconds=1).execute("SELECT pg_sleep(5)")
    assert r.error and r.sqlstate == "57014", (r.error, r.sqlstate)
    assert not is_retryable_execution_error(r)


def test_pipeline_recovers_from_real_db_error() -> None:
    """真库端到端：第一次送一条**数据库会拒绝**的 SQL，第二次改对 → 链路自愈。

    这条 SQL 能过静态校验（列都真实存在），是数据库在执行时才拒的 ——
    正是此前拿不到、也就无法据以重试的那一环。
    """
    if not _db_or_skip("执行失败重试（真库端到端）"):
        return
    catalog = _real_catalog()
    client = ScriptedClient([
        "SELECT COALESCE(total, '') AS amount FROM invoice",  # 22P02：numeric 与空串混用
        "SELECT count(*) AS invoice_count FROM invoice",
    ])
    pipeline = Nl2SqlPipeline(client, catalog=catalog, executor=SqlExecutor(), auto_limit=0)
    outcome = pipeline.run("一共有多少张发票？")

    assert outcome.error is None, outcome.error
    assert outcome.attempts == 2, "真库报错必须触发一次修复重试"
    codes = [s.output.get("sqlstate") for s in outcome.trace]
    assert "22P02" in codes, codes
    assert outcome.row_count == 1 and int(outcome.rows[0]["invoice_count"]) > 0
    assert outcome.refs, "自愈之后引用照常要有"


if __name__ == "__main__":
    tests = [
        ("库结构目录：加载 / 表名字段名判断 / 渲染中文说明", test_schema_catalog),
        ("校验闸门：写操作与多语句一律挡下", test_validator_blocks_writes),
        ("校验闸门：不存在的表名/字段名挡下", test_validator_blocks_unknown_names),
        ("校验闸门：**不误杀**正确的复杂查询", test_validator_does_not_false_reject),
        ("补 LIMIT：只在无 GROUP BY 时补，已有 LIMIT 不覆盖", test_auto_limit),
        ("生成器：剥壳（代码块 / 标签 / 前言 / 无 SQL）", test_extract_sql),
        ("生成器：提示词含库结构、线索与失败原因", test_build_prompts),
        ("生成器：成功取到 SQL；故障收敛成错误不抛异常", test_generate_sql_offline),
        ("pipeline：生成→校验→执行三步齐、逐行引用", test_pipeline_happy_path),
        ("pipeline：校验失败带原因重试", test_pipeline_retries_on_invalid_sql),
        ("pipeline：连续失败后放弃，不把坏 SQL 送进库", test_pipeline_gives_up_after_max_attempts),
        ("pipeline：生成层面失败不重试", test_pipeline_generation_failure_no_retry),
        ("pipeline：执行失败不伪造引用（无错误码时不重试）", test_pipeline_execution_failure_has_no_fake_refs),
        ("执行失败重试判据：写错了重试 / 跑不了不重试 / 无码不重试", test_retry_classification),
        ("pipeline：执行失败带数据库报错原文重试", test_pipeline_retries_on_retryable_execution_error),
        ("pipeline：不可修的执行错误不重试（省一次调用）", test_pipeline_does_not_retry_unfixable_execution_error),
        ("引用规则：逐行 / 整体 / 多表 / 空结果 / 无表", test_refs_rules),
        ("结论拼装：空结果 / 单值 / 多行截断 / 出错", test_conclusion),
        ("工具：返回完整 ToolResult（三字段齐、可序列化往返）", test_tool_returns_full_result),
        ("工具工厂：real / mock 分流，非法值报错", test_tool_factory),
        ("编排层接线：后端开关 + SQL 填进 AskResponse.sql", test_orchestrator_wiring_and_sql_field),
        ("[真库] gold SQL 全部通过校验闸门（不误杀实证）", test_gold_sql_passes_validator),
        ("[真库] gold SQL 端到端执行并与直连结果逐行一致", test_gold_sql_pipeline_end_to_end),
        ("[真库] 只读连接真的拒绝写、语句超时生效", test_executor_is_readonly_and_times_out),
        ("[真库] 执行器带出 SQLSTATE（重试判据的实测依据）", test_executor_reports_real_sqlstate),
        ("[真库] 执行失败 → 回灌报错 → 链路自愈", test_pipeline_recovers_from_real_db_error),
    ]
    for name, fn in tests:
        check(name, fn)

    print(f"\nALL PASSED ({PASSED}/{len(tests)})")
    if SKIPPED:
        print("跳过（依赖真库，未连上）：")
        for item in SKIPPED:
            print("  - " + item)
