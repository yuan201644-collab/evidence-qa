from app.nl2sql.followup import (
    followup_prompt,
    resolve_followup,
    validate_followup_sql,
)


def _instruction(question: str, sql: str, columns: list[str] | None = None) -> dict:
    result = resolve_followup(
        question,
        {"sql": sql, "columns": columns or [], "row_count": 24},
    )
    assert result is not None
    return result.as_payload()


def test_resolver_records_group_replacement_without_guessing_other_turns() -> None:
    previous_sql = (
        "SELECT billing_country, SUM(total) AS total_amount "
        "FROM invoice GROUP BY billing_country"
    )

    instruction = _instruction("改成按月拆开", previous_sql)

    assert instruction["operation"] == "replace_group"
    assert instruction["target"] == "月"
    assert "上一轮实际执行 SQL" in followup_prompt(instruction)
    assert resolve_followup("新问题：有多少客户", {"sql": previous_sql}) is None


def test_resolver_detects_additive_dimension_average_and_ranking() -> None:
    previous_sql = "SELECT city, COUNT(*) AS customer_count FROM customer GROUP BY city"

    assert _instruction("再按国家分一下", previous_sql)["operation"] == "add_group"
    assert _instruction("那平均每张是多少", previous_sql)["operation"] == "average_measure"
    assert _instruction("这个最多的是哪个", previous_sql)["operation"] == "rank_top"
    assert _instruction("改成看发票张数", previous_sql)["operation"] == "replace_measure"


def test_that_followed_by_a_new_group_replaces_prior_group_but_zai_adds_one() -> None:
    previous_sql = (
        "SELECT city, COUNT(*) AS customer_count FROM customer "
        "WHERE country = 'USA' GROUP BY city"
    )

    replacement = _instruction("那按员工分组呢", previous_sql)
    additive = _instruction("再按国家分一下", previous_sql)

    assert replacement["operation"] == "replace_group"
    assert additive["operation"] == "add_group"


def test_replacement_rejects_old_group_and_dropped_filter() -> None:
    previous_sql = (
        "SELECT billing_country, SUM(total) AS total_amount FROM invoice "
        "WHERE customer_id = 42 GROUP BY billing_country"
    )
    instruction = _instruction("改成按月拆开", previous_sql)

    invalid = validate_followup_sql(
        previous_sql,
        "SELECT billing_country, TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice WHERE customer_id = 42 "
        "GROUP BY billing_country, TO_CHAR(invoice_date, 'YYYY-MM')",
        instruction,
    )
    dropped_filter = validate_followup_sql(
        previous_sql,
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice "
        "GROUP BY TO_CHAR(invoice_date, 'YYYY-MM')",
        instruction,
    )

    assert not invalid["ok"]
    assert "old_dimension_retained" in {
        item["reason_code"] for item in invalid["details"]["failures"]
    }
    assert not dropped_filter["ok"]
    assert "previous_filter_dropped" in {
        item["reason_code"] for item in dropped_filter["details"]["failures"]
    }


def test_replacement_accepts_new_month_group_and_preserved_filter() -> None:
    previous_sql = (
        "SELECT billing_country, SUM(total) AS total_amount FROM invoice "
        "WHERE customer_id = 42 GROUP BY billing_country"
    )
    instruction = _instruction("改成按月拆开", previous_sql)
    candidate = (
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice WHERE customer_id = 42 "
        "GROUP BY TO_CHAR(invoice_date, 'YYYY-MM')"
    )

    result = validate_followup_sql(previous_sql, candidate, instruction)

    assert result["ok"], result
    assert result["details"]["preserved_filter_count"] == 1


def test_country_dimension_keeps_the_customer_owner_established_by_prior_filter() -> None:
    previous_sql = (
        "SELECT e.first_name, e.last_name, COUNT(*) AS customer_count "
        "FROM customer c JOIN employee e ON c.support_rep_id = e.employee_id "
        "WHERE c.country = 'USA' GROUP BY e.first_name, e.last_name"
    )
    instruction = _instruction("这个再按国家分一下", previous_sql)
    wrong_owner = (
        "SELECT e.first_name, e.last_name, e.country, COUNT(*) AS customer_count "
        "FROM customer c JOIN employee e ON c.support_rep_id = e.employee_id "
        "WHERE e.country = 'USA' GROUP BY e.first_name, e.last_name, e.country"
    )
    correct_owner = (
        "SELECT e.first_name, e.last_name, c.country, COUNT(*) AS customer_count "
        "FROM customer c JOIN employee e ON c.support_rep_id = e.employee_id "
        "WHERE c.country = 'USA' GROUP BY e.first_name, e.last_name, c.country"
    )

    rejected = validate_followup_sql(previous_sql, wrong_owner, instruction)
    accepted = validate_followup_sql(previous_sql, correct_owner, instruction)

    assert not rejected["ok"]
    assert "dimension_source_mismatch" in {
        item["reason_code"] for item in rejected["details"]["failures"]
    }
    assert "previous_filter_dropped" in {
        item["reason_code"] for item in rejected["details"]["failures"]
    }
    assert accepted["ok"], accepted


def test_employee_group_replacement_preserves_unqualified_filter_across_aliases() -> None:
    previous_sql = (
        "SELECT city, COUNT(*) AS customer_count FROM customer "
        "WHERE country = 'USA' GROUP BY city"
    )
    instruction = _instruction("那按员工分组呢", previous_sql)
    candidate = (
        "SELECT e.first_name || ' ' || e.last_name AS employee_name, "
        "COUNT(*) AS customer_count FROM customer c "
        "JOIN employee e ON c.support_rep_id = e.employee_id "
        "WHERE c.country = 'USA' GROUP BY e.first_name, e.last_name"
    )

    result = validate_followup_sql(previous_sql, candidate, instruction)

    assert instruction["operation"] == "replace_group"
    assert result["ok"], result


def test_sql_alias_or_ordinal_group_by_is_compared_to_its_projection() -> None:
    previous_sql = (
        "SELECT city, COUNT(*) AS customer_count FROM customer GROUP BY city"
    )
    instruction = _instruction("再按国家分一下", previous_sql)
    candidate = (
        "SELECT city, country, COUNT(*) AS customer_count FROM customer "
        "GROUP BY 1, 2"
    )

    result = validate_followup_sql(previous_sql, candidate, instruction)

    assert result["ok"], result


def test_average_and_top_one_keep_existing_month_group() -> None:
    previous_sql = (
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice GROUP BY TO_CHAR(invoice_date, 'YYYY-MM')"
    )
    average_instruction = _instruction("那平均每张是多少", previous_sql)
    average_sql = (
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "AVG(total) AS avg_invoice_amount FROM invoice "
        "GROUP BY TO_CHAR(invoice_date, 'YYYY-MM')"
    )
    top_instruction = _instruction("这个最多的是哪个", previous_sql)
    top_sql = (
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice "
        "GROUP BY TO_CHAR(invoice_date, 'YYYY-MM') ORDER BY total_amount DESC LIMIT 1"
    )

    assert validate_followup_sql(previous_sql, average_sql, average_instruction)["ok"]
    top_result = validate_followup_sql(previous_sql, top_sql, top_instruction)
    assert top_result["ok"], top_result

    average_top_instruction = _instruction("这个最多的是哪个", average_sql)
    wrong_rank_key = (
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "AVG(total) AS avg_invoice_amount FROM invoice "
        "GROUP BY TO_CHAR(invoice_date, 'YYYY-MM') ORDER BY invoice_month DESC LIMIT 1"
    )
    rejected = validate_followup_sql(average_sql, wrong_rank_key, average_top_instruction)
    assert not rejected["ok"]
    assert "ranking_measure_mismatch" in {
        item["reason_code"] for item in rejected["details"]["failures"]
    }


def test_explicit_ranking_metric_must_be_returned_for_the_answer() -> None:
    previous_sql = (
        "SELECT t.name AS track_name FROM track t "
        "GROUP BY t.name, t.milliseconds"
    )
    instruction = _instruction("只看播放时长最长的", previous_sql)
    hidden_metric = (
        "SELECT t.name AS track_name FROM track t "
        "GROUP BY t.name, t.milliseconds "
        "ORDER BY t.milliseconds DESC LIMIT 1"
    )
    visible_metric = (
        "SELECT t.name AS track_name, t.milliseconds AS duration_ms FROM track t "
        "GROUP BY t.name, t.milliseconds "
        "ORDER BY t.milliseconds DESC LIMIT 1"
    )

    rejected = validate_followup_sql(previous_sql, hidden_metric, instruction)
    accepted = validate_followup_sql(previous_sql, visible_metric, instruction)

    assert not rejected["ok"]
    assert "ranking_measure_not_projected" in {
        item["reason_code"] for item in rejected["details"]["failures"]
    }
    assert accepted["ok"], accepted


def test_pipeline_retries_when_generated_sql_ignores_dimension_replacement() -> None:
    from types import SimpleNamespace

    from app.nl2sql.pipeline import Nl2SqlPipeline
    from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta

    first = (
        "SELECT billing_country, TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice "
        "GROUP BY billing_country, TO_CHAR(invoice_date, 'YYYY-MM')"
    )
    corrected = (
        "SELECT TO_CHAR(invoice_date, 'YYYY-MM') AS invoice_month, "
        "SUM(total) AS total_amount FROM invoice "
        "GROUP BY TO_CHAR(invoice_date, 'YYYY-MM')"
    )

    class Client:
        def __init__(self):
            self.calls = []

        def chat(self, _system, prompt):
            self.calls.append(prompt)
            return first if len(self.calls) == 1 else corrected

    class Executor:
        def __init__(self):
            self.calls = []

        def execute(self, sql):
            self.calls.append(sql)
            return SimpleNamespace(
                rows=[{"invoice_month": "2024-09", "total_amount": 7.785}],
                columns=["invoice_month", "total_amount"],
                row_count=1,
                error=None,
                sqlstate=None,
            )

    catalog = SchemaCatalog([TableMeta(
        name="invoice",
        schema="public",
        primary_key=("invoice_id",),
        columns=(
            ColumnMeta("invoice_id", "integer", nullable=False, is_primary_key=True),
            ColumnMeta("billing_country", "text"),
            ColumnMeta("total", "numeric"),
            ColumnMeta("invoice_date", "date"),
            ColumnMeta("customer_id", "integer"),
        ),
    )])
    client = Client()
    executor = Executor()
    previous_sql = (
        "SELECT billing_country, SUM(total) AS total_amount FROM invoice "
        "GROUP BY billing_country"
    )
    instruction = _instruction("改成按月拆开", previous_sql)
    pipeline = Nl2SqlPipeline(
        client,
        catalog=catalog,
        executor=executor,
        auto_limit=0,
        max_attempts=2,
        semantic_planning_enabled=False,
    )

    outcome = pipeline.run(
        "所有发票总金额是多少？\n用户补充或修改：改成按月拆开",
        followup_context=instruction,
    )

    checks = [step.output for step in outcome.trace if step.tool == "followup_sql_validator"]
    assert outcome.error is None, outcome.error
    assert len(client.calls) == 2
    assert "上一轮实际执行 SQL" in client.calls[0]
    assert [item["ok"] for item in checks] == [False, True]
    assert executor.calls == [corrected]


def test_pipeline_retries_when_ranked_metric_is_not_returned() -> None:
    from types import SimpleNamespace

    from app.nl2sql.pipeline import Nl2SqlPipeline
    from app.nl2sql.schema import ColumnMeta, SchemaCatalog, TableMeta

    first = (
        "SELECT t.name AS track_name FROM track t "
        "GROUP BY t.name, t.milliseconds "
        "ORDER BY t.milliseconds DESC LIMIT 1"
    )
    corrected = (
        "SELECT t.name AS track_name, t.milliseconds AS duration_ms FROM track t "
        "GROUP BY t.name, t.milliseconds "
        "ORDER BY t.milliseconds DESC LIMIT 1"
    )

    class Client:
        def __init__(self):
            self.calls = []

        def chat(self, _system, _prompt):
            self.calls.append(_prompt)
            return first if len(self.calls) == 1 else corrected

    class Executor:
        def __init__(self):
            self.calls = []

        def execute(self, sql):
            self.calls.append(sql)
            return SimpleNamespace(
                rows=[{"track_name": "Heaven Can Wait", "duration_ms": 440555}],
                columns=["track_name", "duration_ms"],
                row_count=1,
                error=None,
                sqlstate=None,
            )

    catalog = SchemaCatalog([TableMeta(
        name="track",
        schema="public",
        primary_key=("track_id",),
        columns=(
            ColumnMeta("track_id", "integer", nullable=False, is_primary_key=True),
            ColumnMeta("name", "text"),
            ColumnMeta("milliseconds", "integer"),
        ),
    )])
    client = Client()
    executor = Executor()
    previous_sql = (
        "SELECT t.name AS track_name FROM track t "
        "GROUP BY t.name, t.milliseconds"
    )
    instruction = _instruction("只看播放时长最长的", previous_sql)
    pipeline = Nl2SqlPipeline(
        client,
        catalog=catalog,
        executor=executor,
        auto_limit=0,
        max_attempts=2,
        semantic_planning_enabled=False,
    )

    outcome = pipeline.run("只看播放时长最长的", followup_context=instruction)

    checks = [step.output for step in outcome.trace if step.tool == "followup_sql_validator"]
    assert outcome.error is None, outcome.error
    assert len(client.calls) == 2
    assert "同时展示排名对象和用于排序的指标值" in client.calls[0]
    assert [item["ok"] for item in checks] == [False, True]
    assert executor.calls == [corrected]


def test_memory_keeps_query_state_per_session_and_clears_it_after_non_sql_answer() -> None:
    from app.memory import SessionMemory

    memory = SessionMemory(max_sessions=4, ttl_seconds=1800)
    memory.save_success(
        "session-a", "invoice total",
        query_state={"sql": "SELECT SUM(total) FROM invoice", "columns": ["sum"], "row_count": 1},
    )
    memory.save_success("session-b", "document question")

    assert memory.query_state("session-a")["columns"] == ["sum"]
    assert memory.query_state("session-b") is None
    detached = memory.query_state("session-a")
    detached["columns"].append("mutated")
    assert memory.query_state("session-a")["columns"] == ["sum"]


def test_orchestrator_passes_resolved_sql_edit_and_saves_last_sql(monkeypatch) -> None:
    import app.orchestrator as orchestrator_module
    from app.contracts import ToolResult, TraceStep
    from app.nl2sql.normalizer import NormalizeResult
    from app.orchestrator import Orchestrator, RoutePlan

    first_sql = "SELECT billing_country, SUM(total) AS total_amount FROM invoice GROUP BY billing_country"
    second_sql = "SELECT invoice_month, SUM(total) AS total_amount FROM invoice GROUP BY invoice_month"

    class CaptureTool:
        name = "nl2sql"

        def __init__(self):
            self.payloads = []

        def run(self, payload):
            self.payloads.append(payload)
            sql = first_sql if len(self.payloads) == 1 else second_sql
            return ToolResult(
                data={"sql": sql, "columns": ["billing_country", "total_amount"], "row_count": 24,
                      "conclusion": "查询完成。"},
                trace=[TraceStep(step_id=1, tool="nl2sql", input={}, output={"sql": sql}, elapsed_ms=0)],
            )

    class Registry:
        def get(self, _name):
            return tool

    tool = CaptureTool()
    orchestrator = Orchestrator(Registry())
    monkeypatch.setattr(
        orchestrator_module,
        "normalize",
        lambda question, **_kwargs: NormalizeResult(original=question, normalized=question),
    )
    monkeypatch.setattr(
        orchestrator_module,
        "route",
        lambda _question, _registry: RoutePlan(steps=[("nl2sql", "")], reason="follow-up test"),
    )

    orchestrator.answer("所有发票总金额按国家汇总", "followup-session")
    response = orchestrator.answer("改成按月拆开", "followup-session")

    assert tool.payloads[1]["followup_context"]["operation"] == "replace_group"
    assert tool.payloads[1]["followup_context"]["previous_sql"] == first_sql
    assert any(step.tool == "followup_resolver" for step in response.trace)
    assert orchestrator.session_memory.query_state("followup-session")["sql"] == second_sql
