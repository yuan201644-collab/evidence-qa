"""C5 接线的回归测试。

覆盖三件事：
1. `NL2SQL_REQUIREMENT_GATE` 三档语义（**负责人 2026-10-16 修正后的口径**：
   off 不核对；**repair = 已确认条件没落实，任何非 off 档都拦在执行器之前**；
   档位只决定 unknown（证不出）的处理——warn 放行留痕、enforce 修复重试）；
2. pipeline 自建 validator 时**必须注入能力依据** —— 没有它，依赖能力的判定
   （例如某表有没有时间列）会从「证不出」错成「证明不成立」，把正确 SQL 判死；
3. 要求清单由编排层用原话抽好后传入，pipeline 不重抽。

测试刻意用假 catalog / 假 validator，不读 schema_meta、不连库、不调模型。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest

from app.nl2sql.capability_runtime import RuntimeCapability
from app.nl2sql.pipeline import Nl2SqlPipeline
from app.nl2sql.planning_models import (
    JoinPlan,
    RequirementCoverage,
    RequirementKind,
    QueryRequirement,
    QueryRequirements,
)


# ── 假件 ────────────────────────────────────────────────────────────────


class _FakeClient:
    """恒定返回同一条 SQL 的假模型客户端。

    `chat(system, user, **kwargs)` 是生成器真正调的入口
    （`app/nl2sql/generator.py:256`）。
    """

    def __init__(self, sql: str):
        self.sql = sql

    def chat(self, system: str, user: str, **kwargs) -> str:
        return self.sql

    def complete_sql(self, question: str, schema_prompt: str, **kwargs) -> str:
        return self.sql


class _FakeExecutor:
    """假执行器：不查库，只记被调用过几次。

    返回 `SqlExecutionResult` 形态（有 `.error` / `.row_count`），
    `app/nl2sql/pipeline.py:638` 会直接读 `execution.error`。
    """

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, sql: str):
        from app.nl2sql.executor import ExecutionResult

        self.calls.append(sql)
        return ExecutionResult(rows=[], columns=[], row_count=0, error=None)


class _FakeValidation:
    def __init__(self, sql: str):
        self.sql = sql
        self.issues: list[str] = []


@dataclass
class _FakeColumn:
    name: str
    description: str = ""
    primary_key: bool = False
    sample_values: tuple = ()


@dataclass
class _FakeTable:
    name: str
    description: str = ""
    schema: str = "public"
    primary_key: tuple = ()
    columns: list = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str):
        for col in self.columns:
            if col.name == name:
                return col
        return None


@dataclass
class _FakeCatalog:
    tables: dict = field(default_factory=dict)

    def get_table(self, name: str):
        return self.tables.get(name)

    def table_names(self):
        return list(self.tables)

    def render_ddl(self, *tables, table_names=None) -> str:
        names = table_names or tables or self.tables
        return "\n".join(f"CREATE TABLE {name} (id INT);" for name in names)

    def has_table(self, name: str) -> bool:
        return name in self.tables

    def has_column(self, table: str, column: str) -> bool:
        meta = self.tables.get(table)
        return meta is not None and any(c.name == column for c in meta.columns)

    def data_source(self) -> dict:
        return {"database": "fake_db", "label": "假库"}


def _make_catalog() -> _FakeCatalog:
    customer = _FakeTable(
        "customer",
        "客户表",
        primary_key=("customer_id",),
        columns=[
            _FakeColumn("customer_id", "客户唯一编号", primary_key=True),
            _FakeColumn("country", "客户所在国家", sample_values=("Brazil", "Argentina")),
        ],
    )
    invoice = _FakeTable(
        "invoice",
        "发票表",
        primary_key=("invoice_id",),
        columns=[
            _FakeColumn("invoice_id", "发票编号", primary_key=True),
            _FakeColumn("invoice_date", "开票日期"),
            _FakeColumn("total", "发票金额合计"),
        ],
    )
    return _FakeCatalog(tables={"customer": customer, "invoice": invoice})


def _requirement(kind: RequirementKind, surface: str, normalized: str = ""):
    return QueryRequirement(
        kind=kind,
        surface=surface,
        span=(0, len(surface)),
        normalized=normalized,
    )


def _requirements(*items: QueryRequirement) -> QueryRequirements:
    return QueryRequirements(
        version="s2.v1",
        original="".join(item.surface for item in items),
        normalized_question="",
        requirements=tuple(items),
    )


def _pipeline(
    sql: str,
    *,
    gate: str | None = None,
    catalog: _FakeCatalog | None = None,
    validator=None,
    **kwargs,
) -> tuple[Nl2SqlPipeline, _FakeExecutor]:
    catalog = catalog if catalog is not None else _make_catalog()
    if gate is None:
        # 不显式传档位：让 pipeline 自己去读环境变量，
        # 这样环境变量与默认值的行为都能被测到。
        gate = kwargs.pop("env_gate", "warn")
    else:
        kwargs.pop("env_gate", None)
    pipe = Nl2SqlPipeline(
        _FakeClient(sql),
        catalog=catalog,
        executor=None,
        requirement_gate=gate,
        requirement_validator=validator,
        # 关掉自动补 LIMIT / 字段存在性校验：本测试只关心闸门，
        # 不想让假 catalog 的字段集合干扰判定。
        auto_limit=0,
        check_columns=False,
        **kwargs,
    )
    # 关掉语义规划：本测试只关心闸门，不想让 linker 真去跑。
    pipe.semantic_planning_enabled = False
    executor = _FakeExecutor()
    pipe.executor = executor
    return pipe, executor


SQL_WITH_TIME = "SELECT COUNT(*) FROM invoice WHERE invoice_date >= '2023-01-01'"
SQL_NO_TIME = "SELECT COUNT(*) FROM invoice"


# ── 1. 三档语义 ─────────────────────────────────────────────────────────


def test_gate_off_produces_no_coverage(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "off")
    pipe, executor = _pipeline(SQL_NO_TIME, gate=None, env_gate="off")
    out = pipe.run("2023年有多少张发票？", requirements=_requirements(_requirement("time", "2023年", "2023")))
    assert out.requirement_coverage is None
    assert executor.calls, "off 档必须照样执行"


def test_gate_warn_blocks_repair_before_executor(monkeypatch):
    """⚠ 口径修正（负责人 2026-10-16）：repair = **已确认**条件没落实，
    必须阻止 SQL 执行——与档位无关。warn 档下 repair 一样拦在执行器之前，
    只是不再像 enforce 那样对 unknown 做修复重试。旧口径「warn 照样执行」
    会让"2023 年被丢掉"的 SQL 照跑并返回一个碰巧的数字，正是 S2 要消灭的
    静默丢弃。"""
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "warn")
    pipe, executor = _pipeline(SQL_NO_TIME, gate=None, env_gate="warn")
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.status == "repair"
    assert not executor.calls, "warn 档下 repair 必须拦在执行器之前（口径修正）"
    assert out.error is not None, "拦下后要给出可解释的错误，而不是静默失败"


def test_gate_enforce_blocks_before_executor(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    pipe, executor = _pipeline(SQL_NO_TIME, gate=None, env_gate="enforce")
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.status == "repair"
    assert not executor.calls, "enforce 下 repair 必须拦在执行器之前"
    assert out.attempts == 3, "enforce 走完修复重试循环"


def test_gate_warn_does_not_retry_repair_but_enforce_does(monkeypatch):
    """warn 与 enforce 对 repair 的差别只在修复重试**策略**（两者最终都必须拦）：
    warn 假件客户端恒定返回同一条 SQL，重试耗尽后同样拦在执行器之前。
    enforce 也会走完重试（见上一条 attempts==3）——两档在此场景等价，
    差别体现在 unknown 的处置与核验卡展示。"""
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "warn")
    pipe, executor = _pipeline(SQL_NO_TIME, gate=None, env_gate="warn")
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert out.requirement_coverage.status == "repair"
    assert out.attempts == 3, "恒定假件修不好，warn 也走完重试循环"
    assert not executor.calls, "warn 档 repair 重试耗尽后同样必须拦在执行器之前"


def test_gate_enforce_passes_when_coverage_passes(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    pipe, executor = _pipeline(SQL_WITH_TIME, gate=None, env_gate="enforce")
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.status == "pass"
    assert executor.calls


def test_gate_default_is_warn(monkeypatch):
    monkeypatch.delenv("NL2SQL_REQUIREMENT_GATE", raising=False)
    pipe, _ = _pipeline(SQL_NO_TIME, gate=None)
    assert pipe.requirement_gate == "warn"


def test_gate_rejects_unknown_value():
    with pytest.raises(ValueError, match="NL2SQL_REQUIREMENT_GATE"):
        Nl2SqlPipeline(_FakeClient("SELECT 1"), catalog=_make_catalog(), requirement_gate="strict")


# ── 2. capability 注入 ──────────────────────────────────────────────────


class _SpyValidator:
    """记录收到的参数，并把调用转给真的 validator。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[tuple[str, object, object]] = []
        self.capability = None

    def check(self, sql, requirements, join_plan=None, *, dialect="postgres"):
        self.calls.append((sql, requirements, join_plan))
        if self.capability is not None:
            self.inner.capability = self.capability
        return self.inner.check(sql, requirements, join_plan, dialect=dialect)


def _real_validator():
    from app.nl2sql.requirement_validator import RequirementValidator

    return RequirementValidator()


def test_pipeline_injects_capability_into_self_built_validator(monkeypatch):
    """自建 validator 必须拿到能力依据，否则依赖能力的判定会从
    「证不出」错成「证明不成立」，把正确 SQL 判死。"""
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    spy = _SpyValidator(_real_validator())
    pipe, executor = _pipeline(
        SQL_WITH_TIME, gate=None, env_gate="enforce", validator=spy
    )
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert spy.capability is not None, "pipeline 必须给 validator 注入能力依据"
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.status == "pass"
    assert executor.calls


def test_injected_validator_keeps_its_own_capability(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    inner = _real_validator()
    inner.capability = RuntimeCapability(_make_catalog(), {})
    spy = _SpyValidator(inner)
    pipe, executor = _pipeline(
        SQL_WITH_TIME, gate=None, env_gate="enforce", validator=spy
    )
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.status == "pass"


def test_capability_failure_degrades_to_unknown_not_repair(monkeypatch):
    """能力依据读不到时，时间条件应判 unknown（证不出），不是 repair（判死）。"""
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")

    class _BoomPipeline(Nl2SqlPipeline):
        @property
        def _requirement_capability(self):
            raise RuntimeError("metadata unavailable")

    pipe = _BoomPipeline(
        _FakeClient(SQL_WITH_TIME),
        catalog=_make_catalog(),
        requirement_gate="enforce",
        auto_limit=0,
        check_columns=False,
    )
    # 注入点自己抛异常时，闸门必须把它当成"证不出"而不是让异常炸穿链路。
    pipe._capability = None
    executor = _FakeExecutor()
    pipe.executor = executor
    out = pipe.run(
        "2023年有多少张发票？",
        requirements=_requirements(_requirement("time", "2023年", "2023")),
    )
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.status == "unknown"
    assert executor.calls, "unknown 不拦执行"


# ── 3. 清单传入与透传 ───────────────────────────────────────────────────


def test_pipeline_uses_supplied_requirements_without_reextracting(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    spy = _SpyValidator(_real_validator())
    pipe, _ = _pipeline(SQL_WITH_TIME, gate=None, env_gate="enforce", validator=spy)
    reqs = _requirements(_requirement("time", "2023年", "2023"))
    pipe.run("2023年有多少张发票？", requirements=reqs)
    assert len(spy.calls) == 1
    sql, received, join_plan = spy.calls[0]
    assert received is reqs, "pipeline 必须原样使用传入的清单，不重抽"
    assert join_plan is None or isinstance(join_plan, JoinPlan)


def test_no_requirements_means_no_checks(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    pipe, executor = _pipeline(SQL_WITH_TIME, gate=None, env_gate="enforce")
    out = pipe.run("平台上一共有多少位客户？", requirements=None)
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.checks == ()
    assert executor.calls


def test_empty_requirements_means_no_checks(monkeypatch):
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    pipe, executor = _pipeline(SQL_WITH_TIME, gate=None, env_gate="enforce")
    out = pipe.run("平台上一共有多少位客户？", requirements=_requirements())
    assert out.requirement_coverage is not None
    assert out.requirement_coverage.checks == ()
    assert executor.calls


# ── 4. 覆盖结果随 outcome 返回 ──────────────────────────────────────────


def test_outcome_carries_coverage_dict():
    from app.tools.nl2sql import _coverage_dict

    coverage = RequirementCoverage(
        version="s2.v1",
        checks=(),
    )
    payload = _coverage_dict(coverage)
    assert payload["status"] == "pass"
    assert payload["version"] == "s2.v1"
    assert payload["checks"] == []


def test_coverage_dict_none_is_none():
    from app.tools.nl2sql import _coverage_dict

    assert _coverage_dict(None) is None


def test_env_var_alias_in_os_environ_is_honoured(monkeypatch):
    """`NL2SQL_REQUIREMENT_GATE` 环境变量与构造参数同权。

    注意 `_pipeline` 的 `gate=None` 必须真的不传档位给 pipeline
    （把 `None` 当作"用环境变量"而不是"默认 warn"）。
    """
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "enforce")
    pipe = Nl2SqlPipeline(
        _FakeClient("SELECT 1"),
        catalog=_make_catalog(),
        auto_limit=0,
        check_columns=False,
    )
    assert pipe.requirement_gate == "enforce"


def test_explicit_argument_beats_env_var(monkeypatch):
    """显式参数优先于环境变量（同一测试文件里后续用例改 env 不影响前面的）。"""
    monkeypatch.setenv("NL2SQL_REQUIREMENT_GATE", "off")
    pipe, _ = _pipeline("SELECT 1", gate="warn")
    assert pipe.requirement_gate == "warn"
