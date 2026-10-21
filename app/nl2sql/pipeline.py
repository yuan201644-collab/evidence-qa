"""pipeline.py - 把「生成 → 校验 → 执行」串成一条**可解释**的链路。

**它是结构化链路的唯一入口**：`app/tools/nl2sql.py` 只做「把结果包成契约的
ToolResult」这一层薄壳，真正的业务编排在这里。

**为什么可解释性（trace）在这一层埋，而不是在工具层或模型提示词里**：
    项目硬规范要求「可解释性用代码埋点，禁止用 LLM 思维链充当」。
    这条链路里真正发生的事只有三件 —— 模型写了一条 SQL、校验器放行了它、
    数据库执行了它。三件事各自的入参、出参、耗时在这里都是**确定的事实**，
    原地记下来即可；交给模型复述反而会掺进它自己的合理化叙述。

**每一步都记，失败的步也记**：校验没过、执行报错同样是"推理链路"的一部分。
    用户看到"生成 → 校验失败（表 x 没有字段 y）→ 重新生成 → 校验通过 → 执行
    → 执行失败（别名 p 未定义）→ 重新生成 → 通过 → 执行成功"
    这条链，比只看到一个错误码有用得多，也正好是评分要的"推理链路可追溯"。

**重试策略：只有"模型改一遍就可能对"的失败才重试。**
    判据是"重写有没有信息量"，不是"错误严不严重"：

    | 失败在哪 | 重试？ | 为什么 |
    |---|---|---|
    | 生成（网络 / 鉴权 / 输出被截断） | 否 | 成因不在提示词内容上，重试大概率同样结果，白等一轮（客户端内部本来就有带退避的重试） |
    | 静态校验（表名 / 字段名 / 写操作 / 语法） | 是 | 把错误原文回灌，模型知道往哪改 |
    | 数据库执行（类型不匹配 / 别名未定义 / GROUP BY 漏列…） | **按错误码判** | 见 `is_retryable_execution_error`：**写错了**就重试，**跑不了**（超时 / 断连 / 权限不足）就不重试 |

    执行层重试是 2026-10-01 补的缺口。此前执行写在重试循环**之外**、只做一次，
    而数据库的报错原文恰恰是修复重试最有价值的那份信息：实测线上拿到的
    `missing FROM-clause entry for table "p"`（别名写错，SQLSTATE 42P01）与
    `invalid input syntax for type numeric: ""`（22P02）都是回灌一轮就能改对的类型。

    **一次尝试 = 一次生成**：校验通过就当场执行，不在循环外另起一段。
    `Nl2SqlOutcome.attempts` 因此始终等于"调了几次大模型"，含义单一、对账方便。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace

from sqlglot import exp, parse_one

from app.contracts import SqlRef, TraceStep
from app.progress import emit_progress
from app.nl2sql.config import env_bool, env_int, env_str
from app.nl2sql.executor import SqlExecutor
from app.nl2sql.generator import generate_sql
from app.nl2sql.disambiguator import LiteralDisambiguator
from app.nl2sql.linker import SchemaLinker, ambiguity_groups as _ambiguity_groups
from app.nl2sql.join_planner import JoinPlanner
from app.nl2sql.semantic_validator import SqlSemanticValidator
from app.nl2sql.planning_models import JoinPlan, LinkResult
from app.nl2sql.planning_models import RequirementCoverage, QueryRequirements
from app.nl2sql.requirement_validator import RequirementValidator
from app.nl2sql.followup import validate_followup_sql
from app.nl2sql.schema import SchemaCatalog
from app.nl2sql.validator import validate_sql

#: 写进 `TraceStep.tool` 的工具名。必须与 `MockNl2SqlTool.spec.name` 一致 ——
#: 换实现不改名字，编排层的路由规则与前端的历史记录才不用跟着改。
TOOL_NAME = "nl2sql"

#: 生成的最大尝试次数（首次 + 最多 2 次带错误信息的修复重试）。
DEFAULT_MAX_ATTEMPTS = 3
#: 答案里最多挂几条引用。引用卡片塞太多会把答案淹没；评测只要求"有引用"。
DEFAULT_MAX_REFS = 3


# ============================================================================
# 执行失败：值不值得回灌给模型重写
# ============================================================================

#: 值得重试的 SQLSTATE **类别**（取前两位）。判据是"模型改一遍 SQL 有没有可能改对"。
#:   22 = 数据异常（类型不匹配、除零、非法文本表示、数值越界…）
#:   42 = 语法与访问规则违例（字段/表不存在、GROUP BY 漏列、别名未定义、方言写错…）
RETRYABLE_SQLSTATE_CLASSES = ("22", "42")

#: 除按类别外、另需单独放行的具体错误码。
RETRYABLE_SQLSTATES = frozenset({
    "21000",  # cardinality_violation：子查询返回多行（改写法就能修）
})

#: 同样落在上面两个类别里、但**必须排除**的例外。顺序上先查它、再查类别。
#: 42501 最要紧：权限不足往往正是只读闸门在生效，重试等于鼓励模型换个写法再试，
#: 属于对安全边界的反复试探，一次就该停。
NON_RETRYABLE_SQLSTATES = frozenset({
    "42501",  # insufficient_privilege：权限不足（含只读闸门拒绝写操作）
})


def is_retryable_execution_error(result) -> bool:
    """这次执行失败值不值得回灌给模型重写。

    判的是"模型重写一遍有没有可能改对"，不是"错误严不严重"：
      - **写错了**（字段名、GROUP BY、类型、除零）→ 重写一次确实可能对 → 重试；
      - **跑不了**（语句超时 57014、只读闸门拒写 25006、权限不足、连接断开）
        → 重写再多遍也是同样结果，还白烧一次大模型调用、提前把 3 次机会用光。

    **拿不到 SQLSTATE 一律不重试**：实测连接层故障（连不上、握手/认证超时）的异常上
    根本没有 `sqlstate`，而它正是最不该重试的一类 —— 这个默认值方向必须保守。
    """
    code = getattr(result, "sqlstate", None)
    if not code:
        return False
    code = str(code).strip().upper()
    if not code:
        return False
    if code in NON_RETRYABLE_SQLSTATES:
        return False
    if code in RETRYABLE_SQLSTATES:
        return True
    return code[:2] in RETRYABLE_SQLSTATE_CLASSES


@dataclass
class Nl2SqlOutcome:
    """一次问数的完整产出。工具层把它翻译成契约的 `ToolResult`。

    Attributes:
        sql: 本次**真正执行**的 SQL；没走到执行时是 None。
            ⚠️ 执行失败时**也保留**（那是"我们试过哪条"，trace 要对得上）；
               所以**不能拿 sql 是否为空判成败**，判成败只看 error。
        rows: 结果行（已 JSON 化归一）。
        columns: 结果列名。空结果集也有列名。
        trace: 这条链路的步骤记录（生成 / 校验 / 执行，失败的步也在）。
        refs: 引用（命中哪张表、哪一行）。执行失败时为**空**，不伪造成"没有命中行"。
        error: 整条链路最终是否失败；None 表示成功执行并取回了结果。
        attempts: 实际用了几次生成（= 调用了几次大模型）。
        automatic_limit: 校验器实际自动添加的结果上限；SQL 原有 LIMIT 时为 None。
        result_limit_reached: 返回行数达到自动上限时为 True，表示结果可能被截断。
    """

    question: str
    sql: str | None = None
    rows: list[dict] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    trace: list[TraceStep] = field(default_factory=list)
    refs: list[SqlRef] = field(default_factory=list)
    error: str | None = None
    attempts: int = 0
    need_clarify: bool = False
    clarify_question: str | None = None
    link_result: LinkResult | None = None
    join_plan: JoinPlan | None = None
    requirement_coverage: RequirementCoverage | None = None
    automatic_limit: int | None = None
    result_limit_reached: bool = False

    @property
    def row_count(self):
        return len(self.rows)

class Nl2SqlPipeline:
    """结构化链路的编排器：一次 `run` = 一次问数。"""

    def __init__(
        self,
        client,
        *,
        catalog=None,
        executor=None,
        dialect=None,
        max_attempts=None,
        max_refs=None,
        auto_limit=None,
        large_result_max_rows=None,
        large_result_max_bytes=None,
        check_columns=True,
        max_tokens=None,
        linker=None,
        join_planner=None,
        semantic_validator=None,
        disambiguator=None,
        semantic_planning_enabled=None,
        requirement_validator=None,
        requirement_gate=None,
    ):
        """
        Args:
            client: 大模型客户端（任何有 `chat(system, user) -> str` 的对象）。
                **由外层注入**：本包不 import `app/rag/llm.py`（见包 docstring 的零耦合纪律）。
            catalog: 库结构目录。None 时首次用到才从 `infra/schema_meta.json` 读。
            executor: SQL 执行器。None 时造一个（执行时才连库，构造不连）。
            dialect / auto_limit / check_columns: 透传给 `validator.validate_sql`。
            max_attempts / max_refs / max_tokens: None 时取本模块默认值。
            requirement_validator / requirement_gate: 要求覆盖核对器与闸门档位。
                None 时从环境变量 `NL2SQL_REQUIREMENT_GATE` 读（off/warn/enforce）。
                档位语义（负责人 2026-10-16 修正）：**repair（已确认条件没落实）
                在任何非 off 档都拦在执行器之前**，档位只决定 unknown（证不出）
                的处理——warn 放行留痕、enforce 修复重试；off 不核对不拦。
        """
        self.client = client
        self.executor = executor or SqlExecutor()
        self._catalog = catalog
        self.dialect = dialect
        self.max_attempts = max_attempts or DEFAULT_MAX_ATTEMPTS
        self.max_refs = max_refs or DEFAULT_MAX_REFS
        self.auto_limit = auto_limit
        self.large_result_max_rows = (
            env_int("SQL_LARGE_RESULT_MAX_ROWS", 5000, minimum=0)
            if large_result_max_rows is None
            else max(0, int(large_result_max_rows))
        )
        self.large_result_max_bytes = (
            env_int("RESULT_SET_CACHE_MAX_BYTES", 16 * 1024 * 1024, minimum=1024)
            if large_result_max_bytes is None
            else max(1024, int(large_result_max_bytes))
        )
        self.check_columns = check_columns
        self.max_tokens = max_tokens
        self.semantic_planning_enabled = (
            env_bool("NL2SQL_SEMANTIC_PLANNING_ENABLED", False)
            if semantic_planning_enabled is None
            else bool(semantic_planning_enabled)
        )
        self.linker = linker or SchemaLinker(
            max_tables=env_int("NL2SQL_LINK_MAX_TABLES", 8, minimum=1),
            max_columns=env_int("NL2SQL_LINK_MAX_COLUMNS", 32, minimum=1),
            max_related_tables=env_int("NL2SQL_LINK_MAX_RELATED_TABLES", 12, minimum=1),
        )
        self.join_planner = join_planner or JoinPlanner()
        self.semantic_validator = semantic_validator or SqlSemanticValidator()
        # 消歧是**可选增强**：默认接在当前执行器上（复用同一份 DSN 与只读纪律）。
        # 传 None 可显式关掉（老行为：同分即澄清）。详见 nl2sql/disambiguator.py。
        self.disambiguator = (
            LiteralDisambiguator(self.executor) if disambiguator is None else disambiguator
        )
        # ── 要求覆盖核对闸门（S2）────────────────────────────────────────
        # 三档：off = 完全不核对；warn = 核对并留痕但照样执行；
        #        enforce = repair 直接拦在执行器之前。
        # 默认 warn 的理由见 docs/architecture/S2-..._v0.1_20261015.md 决策 B：
        # 一次全量生效，误判会把 T4（AdventureWorks 71 表）与 T5 从满分打成
        # 零分，所以先留痕观察 unknown/repair 的真实分布再谈升级。
        self.requirement_gate = (
            env_str("NL2SQL_REQUIREMENT_GATE", "warn").strip().lower()
            if requirement_gate is None
            else str(requirement_gate).strip().lower()
        )
        if self.requirement_gate not in ("off", "warn", "enforce"):
            raise ValueError(
                f"不支持的 NL2SQL_REQUIREMENT_GATE：{self.requirement_gate}"
                "（可选 off/warn/enforce）"
            )
        # 闸门自己**不重抽要求清单**：清单由编排层用原文抽好后随问题一起传进来
        # （`run(..., requirements=...)`）。这里若再抽一次，抽的会是改写后的问句，
        # span 对不上原文，对外解释就失真。
        self.requirement_validator = (
            requirement_validator
            if requirement_validator is not None
            else (None if self.requirement_gate == "off" else RequirementValidator())
        )
        # 能力依据单独建，validator 与它解耦（validator 可用注入的假能力测试）。
        # 自己读 schema_meta.descriptions 会让每次构造都吃文件 IO，故惰性加载。
        self._capability = None

    @property
    def catalog(self):
        """库结构目录。**惰性加载**：构造链路时不读文件，第一次真要用才读。"""
        if self._catalog is None:
            self._catalog = SchemaCatalog.load()
        return self._catalog

    @property
    def _requirement_capability(self):
        """能力判定器（惰性）。读不到 schema_meta 说明时返回 None，
        此时核对器对依赖能力的判定一律 unknown —— 不是 pass。"""
        if self._capability is None:
            try:
                from app.nl2sql.capability_runtime import load_capability

                self._capability = load_capability(self.catalog)
            except Exception:
                self._capability = False  # 已试过且失败，哨兵值，不再重试
        return self._capability or None

    @_requirement_capability.setter
    def _requirement_capability(self, value):
        self._capability = value

    @property
    def _dialect(self):
        """方言：显式参数 > 环境变量 SQL_DIALECT > 元数据里记的方言 > postgres。

        数据层预留方言适配（换达梦/人大金仓只改这里）是项目定死的原则之一。
        """
        if self.dialect:
            return self.dialect
        return (
            env_str("SQL_DIALECT")
            or getattr(self.catalog, "dialect", None)
            or "postgres"
        )

    @property
    def _auto_limit(self):
        """自动补 LIMIT 的行数上限。0 = 不补（读 SQL_AUTO_LIMIT，见 config.py）。"""
        if self.auto_limit is not None:
            return self.auto_limit
        return env_int("SQL_AUTO_LIMIT", 1000, minimum=0)

    # ── 对外主入口 ─────────────────────────────────────────────────────
    def run(
        self, question, *, hint="", time_anchor=None, target_grain=None,
        query_intent=None, followup_context=None, requirements=None,
    ):
        """回答一个"要算的数"的问题。**不抛异常**。"""
        catalog = self.catalog
        dialect = self._dialect
        steps: list[TraceStep] = []
        sql = None
        execution = None
        refs: list[SqlRef] = []
        error = None
        attempts = 0

        link_result = None
        join_plan = None
        selected_table_names = None
        planning_constraints = None
        requirement_coverage = None
        # 闸门为 off 时 requirements 允许传 None；非 off 却没传清单 = 调用方
        # 漏接线，此时**照常执行但留痕说明没核对**，不静默跳过。
        if self.requirement_gate != "off" and requirements is None:
            requirements = QueryRequirements(
                version="absent",
                original=question,
                normalized_question=question,
            )
        if self.semantic_planning_enabled:
            # 表字段定位与关联规划在评估前保持显式启用。
            link_started = time.perf_counter()
            emit_progress("sql_link", "定位相关数据表与字段", "started")
            link_result = self.linker.link(question, catalog, intent=query_intent)
            emit_progress(
                "sql_link", "定位相关数据表与字段", "completed",
                elapsed_ms=(time.perf_counter() - link_started) * 1000.0,
            )
            steps.append(_step(
                len(steps) + 1,
                {"question": question, "catalog_tables": len(catalog.tables)},
                {
                    "selected_tables": [_candidate_dict(item) for item in link_result.selected_tables],
                    "selected_columns": [_candidate_dict(item) for item in link_result.selected_columns],
                    "relation_tables": [_candidate_dict(item) for item in link_result.relation_tables],
                    "ambiguous_groups": [list(group) for group in link_result.ambiguous_groups],
                    "degraded": link_result.degraded,
                    "degradation_reason": link_result.degradation_reason,
                    "metadata_version": link_result.metadata_version,
                },
                (time.perf_counter() - link_started) * 1000.0,
                tool="schema_linker",
            ))
            ambiguous_groups = link_result.ambiguous_groups
            if ambiguous_groups and self.disambiguator is not None:
                # 同分候选常源于“多张表各含一个通用列”，表分 = 列分 × 0.8。
                # 若问题里写明了编号（如 WO-2026-0001），拿它去候选表实查一次
                # 真值归属，能站住的候选才留下；站不住的（字面量在其任何列里
                # 都不存在）不参与歧义判定。
                disambiguation = self.disambiguator.run(question, link_result.selected_tables, catalog)
                if disambiguation is not None and disambiguation.tables:
                    surviving = [item for item in link_result.selected_tables
                                 if f"{item.schema}.{item.table}".lower() in disambiguation.tables]
                    if surviving and len(surviving) < len(link_result.selected_tables):
                        steps.append(_step(
                            len(steps) + 1,
                            {
                                "question": question,
                                "literals": list(disambiguation.literals),
                                "lookups": disambiguation.lookups,
                            },
                            {
                                "tables": sorted(disambiguation.tables),
                                "kept": [item.qualified_name for item in surviving],
                                "dropped": [item.qualified_name for item in link_result.selected_tables
                                            if f"{item.schema}.{item.table}".lower() not in disambiguation.tables],
                            },
                            0.0,
                            tool="literal_disambiguation",
                        ))
                        link_result = replace(
                            link_result,
                            selected_tables=tuple(surviving),
                            ambiguous_groups=_ambiguity_groups(surviving),
                        )
                        ambiguous_groups = link_result.ambiguous_groups
            if ambiguous_groups:
                return Nl2SqlOutcome(
                    question=question, trace=steps, error="定位到多个相近业务对象，不能安全地自动选取",
                    need_clarify=True,
                    clarify_question="问题可能指向多个业务对象，请补充具体对象或指标。",
                    link_result=link_result,
                )

            planning_started = time.perf_counter()
            emit_progress("sql_join_plan", "核对数据表关联路径", "started")
            join_plan = self.join_planner.plan(link_result, catalog, target_grain=target_grain)
            emit_progress(
                "sql_join_plan", "核对数据表关联路径", "completed",
                elapsed_ms=(time.perf_counter() - planning_started) * 1000.0,
            )
            steps.append(_step(
                len(steps) + 1,
                {"selected_tables": list(join_plan.tables)},
                {
                    "tables": list(join_plan.tables),
                    "edges": [_edge_dict(edge) for edge in join_plan.edges],
                    "candidate_paths": [list(path) for path in join_plan.candidate_paths],
                    "ambiguous": join_plan.ambiguous,
                    "warnings": list(join_plan.warnings),
                    "evidence": list(join_plan.evidence),
                },
                (time.perf_counter() - planning_started) * 1000.0,
                tool="join_planner",
            ))

            if not link_result.selected_tables and len(catalog.tables) > 12:
                return Nl2SqlOutcome(
                    question=question, trace=steps,
                    error="当前定位器未找到足够的表字段候选，暂不把整库结构交给生成器猜测",
                    need_clarify=True,
                    clarify_question="请补充具体业务对象或指标名称，帮助定位相关数据表。",
                    link_result=link_result,
                    join_plan=join_plan,
                )
            if join_plan.ambiguous or join_plan.warnings:
                return Nl2SqlOutcome(
                    question=question, trace=steps,
                    error="无法建立唯一且有元数据依据的表关联路径",
                    need_clarify=join_plan.ambiguous,
                    clarify_question=(
                        "问题可能涉及多条不同关联路径，请补充需要分析的业务关系。"
                        if join_plan.ambiguous else None
                    ),
                    link_result=link_result,
                    join_plan=join_plan,
                )

            selected_table_names = list(join_plan.tables)
            if not selected_table_names and join_plan.optional_tables:
                # 没有一张表被问题点名（候选都只是“拥有命中的列”）。此时
                # `join_plan.tables` 为空是刻意的：这些表不该被当成必用表，
                # 但仍应作为提示词里的表范围，避免生成器在整个库结构里乱猜。
                selected_table_names = list(join_plan.optional_tables)
            planning_constraints = _planning_constraints(link_result, join_plan)

        # 一次尝试 = 一次生成。校验通过就**当场执行**；执行失败若属于"模型能改对"
        # 的那类（见 is_retryable_execution_error），把数据库的报错原文回灌，进入下一轮。
        last_error = None
        last_sql = None
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt

            # ── ① 生成 ────────────────────────────────────────────────
            t0 = time.time()
            emit_progress("sql_generate", "生成只读查询语句", "started")
            generation = generate_sql(
                question,
                catalog,
                self.client,
                hint=hint,
                dialect=dialect,
                previous_error=last_error,
                previous_sql=last_sql,
                time_anchor=time_anchor,
                max_tokens=self.max_tokens,
                selected_table_names=selected_table_names or None,
                planning_constraints=planning_constraints,
                followup_context=followup_context,
                requirements=requirements,
            )
            emit_progress(
                "sql_generate", "生成只读查询语句",
                "failed" if generation.error else "completed",
                elapsed_ms=_ms(t0),
            )
            gen_output = {"attempt": attempt, "dialect": dialect}
            if generation.sql:
                gen_output["sql"] = generation.sql
                gen_output["notes"] = list(generation.notes)
            else:
                gen_output["error"] = generation.error
            steps.append(
                _step(
                    len(steps) + 1,
                    {
                        "question": question,
                        "hint": hint or "",
                        "attempt": attempt,
                        "catalog_tables": len(catalog.tables),
                        "prompt_tables": selected_table_names,
                    },
                    gen_output,
                    _ms(t0),
                )
            )

            if generation.error:
                # 成因不在提示词内容上（网络/鉴权/输出截断），重试无信息量 → 直接结束
                error = generation.error
                break

            # ── ② 校验 ────────────────────────────────────────────────
            t1 = time.time()
            emit_progress("sql_validate", "校验查询安全与业务关系", "started")
            validation = validate_sql(
                generation.sql,
                catalog,
                dialect=dialect,
                auto_limit=self._auto_limit,
                check_columns=self.check_columns,
            )
            emit_progress(
                "sql_validate", "校验查询安全与业务关系",
                "completed" if validation.ok else "blocked",
                elapsed_ms=_ms(t1),
            )
            val_output = {
                "ok": validation.ok,
                "tables": list(validation.tables),
                "notes": list(validation.notes),
            }
            if not validation.ok:
                val_output["error"] = validation.error
            steps.append(
                _step(
                    len(steps) + 1,
                    {"sql": generation.sql, "dialect": dialect},
                    val_output,
                    _ms(t1),
                )
            )

            if not validation.ok:
                # 校验没过 → 把错误原文回灌，下一轮带着它重写
                error = validation.error
                last_error = validation.error
                last_sql = generation.sql
                continue

            if followup_context:
                followup_started = time.perf_counter()
                followup_check = validate_followup_sql(
                    str(followup_context.get("previous_sql") or ""),
                    generation.sql,
                    followup_context,
                    dialect=dialect,
                )
                steps.append(_step(
                    len(steps) + 1,
                    {
                        "operation": followup_context.get("operation"),
                        "target": followup_context.get("target"),
                    },
                    followup_check,
                    (time.perf_counter() - followup_started) * 1000.0,
                    tool="followup_sql_validator",
                ))
                if not followup_check["ok"]:
                    error = followup_check["message"]
                    if attempt < self.max_attempts:
                        last_error = "多轮追问约束校验失败：" + error
                        last_sql = generation.sql
                        continue
                    break

            # 多表 SQL 必须满足目录中可确认的关系约束。
            if self.semantic_planning_enabled:
                semantic_started = time.perf_counter()
                emit_progress("sql_semantic_validate", "核对查询关系语义", "started")
                semantic = self.semantic_validator.check(
                    validation.sql, join_plan, dialect=dialect, question=question,
                )
                emit_progress(
                    "sql_semantic_validate", "核对查询关系语义",
                    "completed" if semantic.status == "pass" else "blocked",
                    elapsed_ms=(time.perf_counter() - semantic_started) * 1000.0,
                )
                steps.append(_step(
                    len(steps) + 1,
                    {"sql": validation.sql, "planned_edges": [_edge_dict(edge) for edge in join_plan.edges]},
                    {"status": semantic.status, "reason_code": semantic.reason_code,
                     "message": semantic.message, "details": semantic.details},
                    (time.perf_counter() - semantic_started) * 1000.0,
                    tool="sql_semantic_validator",
                ))
                if semantic.status != "pass":
                    error = semantic.message
                    if semantic.status == "repair" and attempt < self.max_attempts:
                        last_error = semantic.message
                        last_sql = generation.sql
                        continue
                    break

            # ── ③ 执行（只有校验通过才会走到这里）──────────────────────
            # ③.5 要求覆盖核对（S2 闸门）：SQL 真的执行之前，把用户的要求
            # 逐条与 SQL 结构对账。enforce 档下 repair 直接拦住；warn 档只留痕。
            if self.requirement_validator is not None:
                requirement_started = time.perf_counter()
                emit_progress("sql_requirement_validate", "核对查询条件是否落实", "started")
                # validator 可能由调用方注入（测试用假能力）；自建的才补能力依据。
                # 能力读不到（元数据缺失）时**不炸链路**：核对器自己会把
                # 依赖能力的判定降成 unknown —— 那是"证不出"，不是"没要求"。
                if self.requirement_validator.capability is None:
                    try:
                        self.requirement_validator.capability = self._requirement_capability
                    except Exception:
                        self.requirement_validator.capability = None
                requirement_coverage = self.requirement_validator.check(
                    validation.sql, requirements, join_plan, dialect=dialect,
                )
                emit_progress(
                    "sql_requirement_validate", "核对查询条件是否落实",
                    "completed" if requirement_coverage.status != "repair" else "blocked",
                    elapsed_ms=(time.perf_counter() - requirement_started) * 1000.0,
                )
                steps.append(_step(
                    len(steps) + 1,
                    {"sql": validation.sql},
                    {
                        "status": requirement_coverage.status,
                        "version": requirement_coverage.version,
                        "checks": [_requirement_check_dict(c) for c in requirement_coverage.checks],
                    },
                    (time.perf_counter() - requirement_started) * 1000.0,
                    tool="sql_requirement_validator",
                ))
                # repair 口径（负责人 2026-10-16 修正）：repair = **已确认**
                # 条件没落实，必须阻止 SQL 执行——这与闸门档位无关。
                # warn/enforce 只决定 unknown（证不出）的处理方式：
                # warn = 放行留痕，enforce = 有未落实证据时先修复重试。
                if (
                    requirement_coverage.repaired
                    and attempt < self.max_attempts
                    and self.requirement_gate != "off"
                ):
                    # 有明确"要求被丢掉"的证据：先重生成一次，让生成器看见
                    # 是哪几条没落实。修不好再拦。
                    last_error = _requirement_repair_message(requirement_coverage)
                    last_sql = generation.sql
                    continue
                if requirement_coverage.repaired:
                    error = _requirement_repair_message(requirement_coverage)
                    break

            sql = validation.sql
            t2 = time.time()
            emit_progress("sql_execute", "执行数据库只读查询", "started")
            execution = self.executor.execute(sql)
            emit_progress(
                "sql_execute", "执行数据库只读查询",
                "failed" if execution.error else "completed",
                elapsed_ms=_ms(t2),
            )
            automatic_limit = (
                self._auto_limit
                if any(note.startswith("已自动补 LIMIT ") for note in validation.notes)
                else None
            )
            result_limit_reached = bool(
                automatic_limit is not None
                and automatic_limit > 0
                and execution.row_count >= automatic_limit
            )
            # ⚠️ 执行**失败**时不造引用。空结果与"根本没查成"是两件事：
            #    前者可以说"没有符合条件的行"，后者连查都没查成，说这句话就是伪造证据。
            refs = (
                build_sql_refs(
                    validation.tables,
                    catalog,
                    execution.rows,
                    execution.columns,
                    max_refs=self.max_refs,
                    sql=sql,
                    dialect=dialect,
                )
                if execution.error is None
                else []
            )
            exec_output = {
                "sql": sql,
                "row_count": execution.row_count,
                "columns": list(execution.columns),
            }
            if automatic_limit is not None:
                exec_output["automatic_limit"] = automatic_limit
                exec_output["possible_truncation"] = result_limit_reached
            if execution.error:
                exec_output["error"] = execution.error
                # 标准错误码也记进 trace：它既是"这一轮为什么重试"的判据来源，
                # 也是评审复现问题时的抓手（比一句自然语言报错精确得多）。
                if execution.sqlstate:
                    exec_output["sqlstate"] = execution.sqlstate
            steps.append(
                _step(
                    len(steps) + 1,
                    {"sql": sql},
                    exec_output,
                    _ms(t2),
                    hit=refs or None,
                )
            )

            # 普通查询先受 SQL_AUTO_LIMIT 保护。若该限制确实触顶，再用同一条
            # 已生成 SQL 做一次受控扩展：SQLGlot 重新校验并补上硬上限 + 1，
            # 执行器同时限制缓存行数与序列化字节数。扩展失败时保留原结果并
            # 明确标记不完整，不把失败的补查伪装成完整查询。
            if (
                execution.error is None
                and result_limit_reached
                and automatic_limit is not None
                and self.large_result_max_rows > automatic_limit
            ):
                probe_step_index = len(steps) - 1
                expanded_limit = self.large_result_max_rows + 1
                expanded_validation_started = time.time()
                expanded_validation = validate_sql(
                    generation.sql,
                    catalog,
                    dialect=dialect,
                    auto_limit=expanded_limit,
                    check_columns=self.check_columns,
                )
                expanded_validation_output = {
                    "ok": expanded_validation.ok,
                    "purpose": "bounded_result_materialization",
                    "tables": list(expanded_validation.tables),
                    "notes": list(expanded_validation.notes),
                    "max_rows": self.large_result_max_rows,
                }
                if not expanded_validation.ok:
                    expanded_validation_output["error"] = expanded_validation.error
                steps.append(
                    _step(
                        len(steps) + 1,
                        {"sql": generation.sql, "dialect": dialect},
                        expanded_validation_output,
                        _ms(expanded_validation_started),
                    )
                )

                expanded_semantic_ok = True
                if expanded_validation.ok and self.semantic_planning_enabled:
                    semantic_started = time.time()
                    expanded_semantic = self.semantic_validator.check(
                        expanded_validation.sql,
                        join_plan,
                        dialect=dialect,
                        question=question,
                    )
                    expanded_semantic_ok = expanded_semantic.status == "pass"
                    steps.append(
                        _step(
                            len(steps) + 1,
                            {
                                "sql": expanded_validation.sql,
                                "planned_edges": [_edge_dict(edge) for edge in join_plan.edges],
                                "purpose": "bounded_result_materialization",
                            },
                            {
                                "status": expanded_semantic.status,
                                "reason_code": expanded_semantic.reason_code,
                                "message": expanded_semantic.message,
                                "details": expanded_semantic.details,
                            },
                            _ms(semantic_started),
                            tool="sql_semantic_validator",
                        )
                    )

                if expanded_validation.ok and expanded_semantic_ok:
                    expanded_sql = expanded_validation.sql
                    expanded_started = time.time()
                    expanded_execution = self.executor.execute(
                        expanded_sql,
                        max_result_rows=expanded_limit,
                        max_result_bytes=self.large_result_max_bytes,
                    )
                    expanded_truncated = bool(
                        expanded_execution.result_truncated
                        or len(expanded_execution.rows) > self.large_result_max_rows
                    )
                    expanded_rows = expanded_execution.rows[: self.large_result_max_rows]
                    expanded_refs = (
                        build_sql_refs(
                            expanded_validation.tables,
                            catalog,
                            expanded_rows,
                            expanded_execution.columns,
                            max_refs=self.max_refs,
                            sql=expanded_sql,
                            dialect=dialect,
                        )
                        if expanded_execution.error is None
                        else []
                    )
                    expanded_output = {
                        "sql": expanded_sql,
                        "row_count": expanded_execution.row_count,
                        "columns": list(expanded_execution.columns),
                        "purpose": "bounded_result_materialization",
                        "max_rows": self.large_result_max_rows,
                        "possible_truncation": expanded_truncated,
                    }
                    if expanded_execution.result_truncated:
                        expanded_output["truncation_reason"] = "row_or_byte_limit"
                    if expanded_execution.error:
                        expanded_output["error"] = expanded_execution.error
                        if expanded_execution.sqlstate:
                            expanded_output["sqlstate"] = expanded_execution.sqlstate
                    steps.append(
                        _step(
                            len(steps) + 1,
                            {"sql": expanded_sql, "purpose": "bounded_result_materialization"},
                            expanded_output,
                            _ms(expanded_started),
                            hit=expanded_refs or None,
                        )
                    )
                    if expanded_execution.error is None:
                        # The first capped query was only a probe; use only the
                        # bounded materialization as final evidence and output.
                        steps[probe_step_index] = steps[probe_step_index].model_copy(
                            update={"hit": None}
                        )
                        execution = expanded_execution
                        execution.rows = expanded_rows
                        sql = expanded_sql
                        automatic_limit = self.large_result_max_rows
                        result_limit_reached = expanded_truncated
                        refs = expanded_refs
                    else:
                        result_limit_reached = True
                else:
                    result_limit_reached = True

            if execution.error is None:
                error = None
                break

            error = execution.error
            if not is_retryable_execution_error(execution):
                # 超时 / 连不上 / 权限不足：重写再多遍也是同样结果 → 停，别白烧一次调用
                break
            # 库说"这条 SQL 我不认" —— 这是修复重试最有价值的一份输入，回灌给模型
            last_error = "这条 SQL 通过了静态校验，但数据库执行时报错：" + execution.error
            last_sql = sql

        if execution is not None and execution.error is None:
            return Nl2SqlOutcome(
                question=question,
                sql=sql,
                rows=execution.rows,
                columns=execution.columns,
                trace=steps,
                refs=refs,
                error=None,
                attempts=attempts,
                link_result=link_result,
                join_plan=join_plan,
                automatic_limit=automatic_limit,
                result_limit_reached=result_limit_reached,
                requirement_coverage=requirement_coverage,
            )

        # 没成功。走到过执行就把那条 SQL 一并带回去（评测按 error 判错，但 trace 要对得上）；
        # 一次都没走到执行时 sql 仍是 None。失败路径下 refs 恒为空 —— 见上面那条 ⚠️。
        return Nl2SqlOutcome(
            question=question,
            sql=sql,
            trace=steps,
            error=error or last_error or "未能生成可通过校验的 SQL",
            attempts=attempts,
            link_result=link_result,
            join_plan=join_plan,
            requirement_coverage=requirement_coverage,
        )


# ============================================================================
# 引用与结论
# ============================================================================


def build_sql_refs(
    tables, catalog, rows, columns, *, max_refs=DEFAULT_MAX_REFS, sql=None, dialect="postgres"
):
    """从一次查询的结果造引用。规则见下（三条，按优先级）。

    ① **能逐行定位时**（结果列里含主表的完整主键）→ 每行一条引用，
       `row_pk` 写完整主键值；`column` 列出可明确归属该表的投影字段，片段
       同时保留可读预览和字段—原始值标签，便于逐项核验。
     ② **否则** → 每张被引用的表一条（最多 `max_refs` 条），`column` 只在
       结果里确实出现过该表的真实字段时才填。
       为什么不是"只挂第一张表"：JOIN 查询的数据来源本来就是多张表，
       只挂第一张会把其余来源从引用卡片里抹掉，属于**漏报证据**。
       `column` 为什么宁可留空：`SELECT count(*) AS customer_count` 里的
       `customer_count` 是聚合别名、不是表里的字段，把它填进 `column`
       等于伪造"命中了这个字段"。
        此时用 `result_id` 标识本次查询结果级证据；它不是、也不冒充数据库行主键。
    ③ **执行成功但结果为空**同样给引用（引用非空是评测硬要求），snippet 写明
       没有命中行 —— 这是事实陈述，不是伪造证据。
       ⚠️ 边界：**执行失败**（SQL 报错/超时）不在此列，那种情况一条引用都不该有，
          由调用方（`Nl2SqlPipeline.run`）在调本函数前判掉。
    """
    table_list = [t for t in (tables or []) if t]
    if not table_list:
        # 一条 SQL 也可能不引用任何表（如 SELECT 1）；此时没有"来源"可引，
        # 返回空表让上层回退到 trace 汇总 —— 不编一个假来源出来。
        return []

    main = table_list[0]
    meta = catalog.get_table(main)
    primary_key = list(meta.primary_key if meta else ())
    bindings = _direct_projection_bindings(sql, catalog, dialect=dialect)
    real_projections = _pick_real_columns(columns, meta, bindings)
    outputs_by_source_column = {}
    for output_name, binding in bindings.items():
        if binding is None:
            continue
        source_table, source_column = binding
        if source_table == meta:
            outputs_by_source_column[source_column.casefold()] = output_name
    has_complete_pk = bool(primary_key) and all(
        str(column).casefold() in outputs_by_source_column
        and not any(separator in str(column) for separator in "&|=")
        for column in primary_key
    )

    if rows and has_complete_pk:
        row_refs = []
        for row in rows[:max_refs]:
            cited_projections = _row_reference_fields(row, real_projections)
            cited_source_columns = {column.casefold() for column, _ in cited_projections}
            if any(str(column).casefold() not in cited_source_columns for column in primary_key):
                row_refs = []
                break
            row_values = {str(name).casefold(): value for name, value in row.items()}
            key_values = [
                row_values.get(outputs_by_source_column[str(column).casefold()].casefold())
                for column in primary_key
            ]
            # A missing/null component is not a row identity. Do not manufacture a partial key.
            if any(
                value is None or not str(value) or any(sep in str(value) for sep in "&|")
                for value in key_values
            ):
                row_refs = []
                break
            row_pk = "&".join(
                str(column) + "=" + str(value)
                for column, value in zip(primary_key, key_values)
            )
            row_refs.append(SqlRef(
                table=main,
                column="/".join(column for column, _ in cited_projections) or None,
                row_pk=row_pk,
                snippet=_row_reference_snippet(row, cited_projections),
            ))
        if row_refs:
            return row_refs

    # 聚合、投影不含完整主键及零行查询都属于查询结果级证据。
    # 为同一次执行共享一个独立标识，不从聚合值伪造数据库行主键。
    result_id = uuid.uuid4().hex
    single_table = len(table_list) == 1
    # 引用片段里点名是哪个库。原先只写「业务库表 xxx」，用户看不出这条记录
    # 来自哪一份数据 —— 库里同时跑着 Chinook 与 AdventureWorks 两个示例库，
    # 光说「业务库」等于没说。库名取不到时退回原来的说法，**不编名字**。
    source_label = ""
    source = catalog.data_source() if catalog is not None else None
    if source:
        source_label = str(source.get("database") or "")
    refs = []
    for name in table_list[:max_refs]:
        table_meta = catalog.get_table(name)
        where = ("库 " + source_label + " 的表 ") if source_label else "业务库表 "
        if rows:
            snippet = "本次查询命中" + where + name + "，共 " + str(len(rows)) + " 行"
            if single_table:
                row_columns = _pick_real_columns(columns, table_meta, bindings)
                cited_projections = _row_reference_fields(rows[0], row_columns)
                snippet += "；首行：" + _row_reference_snippet(rows[0], cited_projections)
        else:
            snippet = "本次查询命中" + where + name + "，没有符合条件的行"
        refs.append(
            SqlRef(
                table=name,
                column=("/".join(column for column, _ in (
                    _row_reference_fields(rows[0], _pick_real_columns(columns, table_meta, bindings))
                    if rows and single_table
                    else _pick_real_columns(columns, table_meta, bindings)[:32]
                )) or None)
                if single_table else None,
                row_pk=None,
                result_id=result_id,
                snippet=snippet,
            )
        )
    return refs


def _direct_projection_bindings(sql, catalog, *, dialect):
    """Map result labels to source fields only for direct SELECT projections."""
    if not isinstance(sql, str) or not sql.strip():
        return {}
    try:
        query = parse_one(sql, read=dialect)
    except Exception:
        return {}
    if not isinstance(query, exp.Select) or query.args.get("with_"):
        return {}
    from_clause = query.args.get("from_")
    if from_clause is None or from_clause.expressions:
        return {}
    source_nodes = [from_clause.this]
    joins = query.args.get("joins") or []
    source_nodes.extend(join.this for join in joins)
    if any(not isinstance(node, exp.Table) for node in source_nodes):
        return {}

    source_by_alias = {}
    source_metas = []
    for node in source_nodes:
        if node.args.get("catalog"):
            return {}
        lookup = f"{node.db}.{node.name}" if node.db else node.name
        table_meta = catalog.get_table(lookup) or catalog.get_table(node.name)
        if table_meta is None:
            return {}
        source_metas.append(table_meta)
        alias_key = node.alias_or_name.casefold()
        if alias_key in source_by_alias and source_by_alias[alias_key] != table_meta:
            source_by_alias[alias_key] = None
        else:
            source_by_alias[alias_key] = table_meta

    bindings = {}

    def add_binding(output_name, candidate):
        if not output_name:
            return
        output_key = str(output_name).casefold()
        if output_key in bindings:
            # Duplicate SQL result labels are driver-dependent when converted to
            # dictionaries. Even identical source expressions are ambiguous.
            bindings[output_key] = None
        else:
            bindings[output_key] = candidate

    for projection in query.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        output_name = projection.alias if isinstance(projection, exp.Alias) else None
        if isinstance(expression, exp.Star):
            if len(source_metas) != 1:
                continue
            source_table = source_metas[0]
            for column in source_table.columns:
                add_binding(column.name, (source_table, column.name))
            continue
        if isinstance(expression, exp.Column) and expression.is_star:
            source_table = source_by_alias.get(expression.table.casefold()) if expression.table else None
            if source_table is not None:
                for column in source_table.columns:
                    add_binding(column.name, (source_table, column.name))
            continue
        if isinstance(expression, exp.Column) and not output_name:
            output_name = expression.name
        elif not output_name:
            output_name = getattr(projection, "alias_or_name", None)
        if not output_name:
            continue
        candidate = None
        if not isinstance(expression, exp.Column):
            add_binding(output_name, candidate)
            continue
        if expression.catalog or expression.db:
            add_binding(output_name, candidate)
            continue
        if expression.table:
            source_table = source_by_alias.get(expression.table.casefold())
        else:
            owners = [table for table in source_metas if table.column(expression.name)]
            source_table = owners[0] if len(owners) == 1 else None
        source_column = source_table.column(expression.name) if source_table is not None else None
        if source_column is not None:
            candidate = (source_table, source_column.name)
        add_binding(output_name, candidate)
    return bindings


def _pick_real_columns(columns, table_meta, bindings):
    """Return projected fields whose AST-proven source is the requested table."""
    if table_meta is None:
        return []
    real_columns = []
    seen = set()
    for output_name in columns:
        binding = bindings.get(str(output_name).casefold())
        if binding is None or binding[0] != table_meta:
            continue
        column_name = binding[1]
        if column_name.casefold() in seen:
            continue
        seen.add(column_name.casefold())
        real_columns.append((column_name, str(output_name)))
    return real_columns


def _row_reference_fields(row, real_columns):
    """Keep exact value citations bounded to the existing six-field preview."""
    preview_names = {str(name).casefold() for name, _ in list(row.items())[:6]}
    row_values = {str(name).casefold(): value for name, value in row.items()}
    selected = []
    for column, output_name in real_columns:
        if output_name.casefold() not in preview_names:
            continue
        value = row_values.get(output_name.casefold())
        if value is not None and len(str(value)) > 60:
            continue
        selected.append((column, output_name))
    return selected


def _row_reference_snippet(row, real_columns):
    """Keep the readable row preview and bind projected source fields to raw values."""
    row_values = {str(name).casefold(): value for name, value in row.items()}
    field_tags = []
    preview = {}
    seen = set()
    for column, output_name in real_columns:
        if column.casefold() in seen:
            continue
        seen.add(column.casefold())
        value = row_values.get(output_name.casefold())
        preview[column] = value
        if value is None:
            encoded = "null"
        else:
            raw_value = str(value)
            encoded = json.dumps(raw_value, ensure_ascii=False)
        field_tags.append(column + "=" + encoded)
    if not preview:
        return "查询返回的列不是可确认的源表直出字段，未将其作为源字段引用。"
    snippet = _row_snippet(preview)
    if field_tags:
        snippet += " [" + ";".join(field_tags) + "]"
    return snippet


def build_conclusion(
    rows,
    columns,
    *,
    error=None,
    max_preview=3,
    automatic_limit=None,
    result_limit_reached=False,
    sql=None,
):
    """把结果行拼成一句人话结论。

    为什么由本模块产出、而不是留给编排层的通用拼装：编排层不认识"这一列是金额、
    那一列是编号"，只能做 `字段：值` 的机械拼接；而这里知道结果集有几行、
    是不是单值。**结论只描述结果集本身**，不替用户下判断（不做"所以业务上应该…"
    这种超出证据的延伸）。
    """
    if error:
        return "这条问题没能算出结果：" + error
    if not rows:
        return "没有查到符合条件的数据。"
    priority_columns = _aggregate_output_columns(sql)
    if len(rows) == 1:
        text = "查询结果：" + _row_snippet(rows[0], priority_columns=priority_columns) + "。"
    else:
        preview = "；".join(
            _row_snippet(row, priority_columns=priority_columns)
            for row in rows[:max_preview]
        )
        text = "共 " + str(len(rows)) + " 行，前 " + str(min(len(rows), max_preview)) + " 行：" + preview
        if len(rows) > max_preview:
            # 省略号后面不再补句号：中文里「……。」连写是错的，此前会拼出
            # 「… customer_count：1 …。」这种尾巴。省略号本身已表示话没说完。
            text += " …"
        else:
            text += "。"
    if result_limit_reached and automatic_limit:
        text += (
            "注意：本次仅取得前 " + str(automatic_limit)
            + " 行，已触及安全上限，不能视为完整结果。"
        )
    return text


def _pick_real_column(columns, table_meta):
    """从结果列里挑一个**确实是该表字段**的列名；挑不到返回 None。

    只认真实字段（见 build_sql_refs ② 的说明）：聚合别名不算。
    """
    if table_meta is None:
        return None
    for name in columns:
        if table_meta.column(name) is not None:
            return name
    return None


def _aggregate_output_columns(sql):
    """Return aliases of selected aggregate expressions for concise answer rendering."""
    if not isinstance(sql, str) or not sql.strip():
        return ()
    try:
        query = parse_one(sql, read="postgres")
    except Exception:  # noqa: BLE001 - summary rendering must not fail a successful query
        return ()
    if not isinstance(query, exp.Select):
        return ()
    result = []
    for projection in query.expressions:
        expression = projection.this if isinstance(projection, exp.Alias) else projection
        if any(isinstance(node, exp.AggFunc) for node in expression.walk()):
            name = projection.alias if isinstance(projection, exp.Alias) else projection.output_name
            if name:
                result.append(str(name))
    return tuple(result)


def _row_snippet(row, max_pairs=6, priority_columns=()):
    """把一行结果渲染成引用片段 / 结论片段。"""
    pairs = []
    items = list(row.items())
    selected_indices = set(range(min(max_pairs, len(items))))
    priorities = {str(name).casefold() for name in priority_columns}
    if priorities and len(items) > max_pairs:
        for index, (key, _value) in enumerate(items):
            if key.casefold() not in priorities or index in selected_indices:
                continue
            replaceable = [
                current for current in selected_indices
                if items[current][0].casefold() not in priorities
            ]
            if replaceable:
                selected_indices.remove(max(replaceable))
                selected_indices.add(index)
    for index in sorted(selected_indices):
        key, value = items[index]
        if value is None:
            continue
        formatted = _fmt(value)
        if str(key).lower().endswith(("_percent", "_percentage")):
            formatted += "%"
        pairs.append(str(key) + "：" + formatted)
    return "、".join(pairs) if pairs else "（该行所有字段均为空）"


def _fmt(value, limit=60):
    if isinstance(value, float):
        # 121.6224 这类浮点直接 str 会给出 121.62240000000001；格式化到 6 位再去尾零，
        # 展示上稳定，且不会改变评测口径（评测比的是数据库执行出来的原始结果集）。
        text = ("%.6f" % value).rstrip("0").rstrip(".")
        return text or "0"
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _step(step_id, step_input, step_output, elapsed_ms, hit=None, tool=TOOL_NAME):
    return TraceStep(
        step_id=step_id,
        tool=tool,
        input=step_input,
        output=step_output,
        elapsed_ms=max(elapsed_ms, 0.0),
        hit=hit,
    )


def _requirement_check_dict(check):
    """把一条要求核对结果压成可 JSON 化的小字典（进 trace）。

    `details.kind` 是测试与前端筛同类要求的唯一依据，**不能省**。
    """
    return {
        "requirement_id": check.requirement_id,
        "kind": check.details.get("kind", ""),
        "surface": check.details.get("surface", ""),
        "outcome": check.outcome,
        "reason_code": check.reason_code,
        "message": check.message,
        "sql_scope": check.sql_scope,
    }


def _requirement_repair_message(coverage):
    """把 repair 级结论压成一句给人看的错误文案。

    刻意**不只说"校验未通过"**：说不出是哪几条没落实，用户与评委都无法判断
    该改问题还是该改系统。这正是 S2 要消灭的"静默丢弃"的翻版。
    """
    reasons = [
        c.message for c in coverage.checks if c.outcome == "repair" and c.message
    ]
    head = "查询条件未能完全落实：" + "；".join(reasons[:3])
    if len(reasons) > 3:
        head += f"；等 {len(reasons)} 项"
    return head


def _candidate_dict(candidate):
    return {
        "object_type": candidate.object_type,
        "qualified_name": candidate.qualified_name,
        "score": candidate.score,
        "score_parts": candidate.score_parts,
        "matched_terms": list(candidate.matched_terms),
        "relation_only": candidate.relation_only,
    }


def _edge_dict(edge):
    return {
        "left": f"{edge.left_schema}.{edge.left_table}",
        "left_columns": list(edge.left_columns),
        "right": f"{edge.right_schema}.{edge.right_table}",
        "right_columns": list(edge.right_columns),
        "source": edge.source,
        "join_type": edge.join_type,
        "cardinality": edge.cardinality,
    }


def _planning_constraints(link_result, join_plan):
    lines = []
    for candidate in link_result.selected_tables:
        lines.append(f"候选表 {candidate.qualified_name}，分数 {candidate.score:.3f}，"+
                     "命中依据：" + ("、".join(candidate.matched_terms) or "目录关联补全"))
    for candidate in link_result.selected_columns:
        lines.append(f"候选字段 {candidate.qualified_name}，分数 {candidate.score:.3f}")
    for edge in join_plan.edges:
        lines.append(f"允许关联 {edge.left_table}.{','.join(edge.left_columns)} = "
                     f"{edge.right_table}.{','.join(edge.right_columns)}；依据 {edge.source}")
    if join_plan.warnings:
        lines.extend("注意：" + warning for warning in join_plan.warnings)
    if link_result.degraded:
        lines.append("定位状态：关键词降级。候选分数不是业务事实，不得仅凭相似度关联。")
    return "\n".join(lines) or "未找到可靠候选；只能使用库结构事实并保持保守。"


def _ms(t0):
    return round((time.time() - t0) * 1000.0, 3)


__all__ = [
    "TOOL_NAME",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_REFS",
    "RETRYABLE_SQLSTATE_CLASSES",
    "RETRYABLE_SQLSTATES",
    "NON_RETRYABLE_SQLSTATES",
    "is_retryable_execution_error",
    "Nl2SqlOutcome",
    "Nl2SqlPipeline",
    "build_sql_refs",
    "build_conclusion",
]
