"""requirement_validator.py —— 用户要求覆盖核对器（S2 核心）。

**它解决什么问题**：用户把条件说清楚了，系统却把它**静默丢掉**。
典型是 `2023年有多少客户？` —— `customer` 表没有任何时间列，
系统本该说「客户表没有注册时间依据」，现实却是丢掉年份、返回全量客户数。
这正是母方案 §5.4 的九类检查要拦的东西。

**与 `semantic_validator.py` 的分工（不可混淆）**：

- `semantic_validator` 回答「这条 SQL 结构上合不合法」：外键路径对不对、
  必用表有没有出现、粒度风险。**它不知道用户要什么**。
- 本模块回答「用户明确提出的要求，SQL 有没有落实」。同一个 SQL 在
  结构校验下可以完全合法，同时静默丢掉用户的时间条件。

**三态口径（负责人 m10899 裁定 P1 = (c) 放行 + 标黄）**：

- `pass` —— 找到 SQL 里承载这条要求的明确证据；
- `repair` —— 证明这条要求**不可能**被满足（如表里没有对应列），
  或 SQL 结构可判定地绕过了它（如 LIMIT 冒充前 N 条）；
- `unknown` —— 看得懂 SQL，但证不出落实。**不是错误，不许当成通过**。
  `unknown` 的处置见 `docs/architecture/S2-SQL条件核对完整实施计划_v0.1_20261015.md`
  决策 A：warn 档下标黄放行，enforce 档下拦截。

**硬纪律**：

1. **证明不了就 `unknown`，绝不猜 `pass`**（母方案 §5.4 末句）。
2. **不用字符串匹配 SQL**：全部判据走 `app/nl2sql/sql_scopes.py` 的
   作用域解析，覆盖 WHERE / HAVING / JOIN 的 ON / CASE 判定分支 / 子查询。
3. `mandatory_conjuncts` 遇到 OR 整条作废 —— OR 分支里的谓词可能在某个
   分支为假，不能算"必然成立"。
4. **没有要求清单时整体返回 `unknown`**，不做任何判定（宁可不判，不乱判）。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterable

from app.nl2sql.capability_runtime import RuntimeCapability
from app.nl2sql.planning_models import (
    JoinPlan,
    QueryRequirement,
    QueryRequirements,
    RequirementCheck,
    RequirementCoverage,
    ResolvedRequirement,
)
from app.nl2sql.sql_scopes import (
    mandatory_conjuncts as _mandatory_conjuncts,
    parse_sql as _parse_sql,
    scope_condition_columns as _scope_condition_columns,
    scope_distinct as _scope_distinct,
    scope_group_keys as _scope_group_keys,
    scope_limit as _scope_limit,
    scope_order_keys as _scope_order_keys,
    scope_predicates as _scope_predicates,
    scope_select_columns as _scope_select_columns,
    table_identity as _table_identity,
    unwrap_query as _unwrap_query,
)
from sqlglot import exp
from app.query_requirements import REQUIREMENTS_VERSION  # noqa: F401  （对外复述版本）

#: 本模块自己的版本。与 `REQUIREMENTS_VERSION` 分开记：清单格式变了
#: 与核对逻辑变了是两件事，报告里要能分清是哪一边。
VALIDATOR_VERSION = "s2.v1"


class RequirementValidator:
    """把要求清单逐条与 SQL 语法树对账。**不查库、不调模型、无 IO。**

    能力依据（哪些表有哪些时间列/维度列）由 `capability` 提供；缺了它，
    凡是依赖能力的判定一律 `unknown` —— 不得因为"没数据"就默认通过。
    """

    def __init__(self, capability: RuntimeCapability | None = None) -> None:
        #: 能力判定器。None 表示"没有能力依据"，此时凡是依赖能力的判定
        #: 一律 `unknown` —— 不得因为"没数据"就默认通过。
        self.capability = capability

    # ── 对外主入口 ──────────────────────────────────────────────────────
    def check(
        self,
        sql: str,
        requirements: QueryRequirements | None,
        join_plan: JoinPlan | None = None,
        *,
        dialect: str = "postgres",
    ) -> RequirementCoverage:
        """逐条核对要求。SQL 解析失败时全部判 `unknown`（如实报告，不阻断）。"""
        if requirements is None:
            # 没有清单 = 本轮没提过要求，不能声称"要求都已满足"。
            return RequirementCoverage(version=VALIDATOR_VERSION, checks=())

        items = requirements.requirements
        if not items:
            return RequirementCoverage(version=VALIDATOR_VERSION, checks=())

        parsed = _parse_sql(sql, dialect)
        if parsed is None:
            return RequirementCoverage(
                version=VALIDATOR_VERSION,
                checks=tuple(
                    self._unknown(item, "sql_ast_unavailable",
                                  "无法解析 SQL 结构，不能证明这条要求被落实。")
                    for item in items
                ),
            )

        scope = _unwrap_query(parsed)
        checks: list[RequirementCheck] = []
        for item in items:
            if not item.required:
                # 提示性要求不产 repair：一次系统提示不该打坏整条 SQL。
                checks.append(self._check_one(item, scope, requirements, join_plan,
                                              mode="advisory"))
                continue
            checks.append(self._check_one(item, scope, requirements, join_plan))

        # ── 反向核对：SQL 里凭空出现的取值过滤 ─────────────────────────
        # 正向（上面）只回答"用户要的 SQL 做到没有"；这一段回答相反的问题
        # "SQL 里做的用户要过没有"。实测缺陷（意见单 20261016）：
        # `那去年的呢？` 生成了 `WHERE country = '中国' AND postal_code
        # >= '20250000'…` —— 用户没提中国，Chinook 也没有这个取值，
        # 恒假条件算出一个自信的 0，正向检查全部放行。
        # 只查**字面量等值过滤**（`col = '常量'`）：LIMIT/ORDER BY/类型转换
        # 等合法片段不算多余；判 `unknown` 不判 `repair`（静态证不出它
        # 一定错，可能是合理的业务默认），与 P1 裁定 (c) 一致。
        checks.extend(self._check_extraneous_filters(scope, requirements))
        return RequirementCoverage(version=VALIDATOR_VERSION, checks=tuple(checks))

    # ── 单条要求的分派 ──────────────────────────────────────────────────
    def _check_one(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        *,
        mode: str = "strict",
    ) -> RequirementCheck:
        if not item.actionable:
            # 未解析要求：不判 pass，也不判 repair（它甚至还不是一条完整要求）。
            return self._unknown(
                item, "requirement_unresolved",
                f"这条要求未解析：{item.surface}。系统说不清它指什么，因此不能声称已落实。",
            )

        # 能力依据缺席是"全类"降级：凡是需要知道"库里有什么"的判定
        # （时间/取值/数值/分组/关联粒度）都证不出。纯 SQL 结构判定
        # （否定/排名/条件作用域）不受影响，继续走正常分支。
        guard = self._capability_guard(item, requirements, join_plan)
        if guard is not None and item.kind in _CAPABILITY_BOUND_KINDS:
            return guard

        handler = _DISPATCH.get(item.kind)
        if handler is None:
            return self._unknown(
                item, "requirement_kind_unsupported",
                f"暂不支持核对这类要求：{item.kind}。宁可如实报告未核对，也不当成已通过。",
            )
        return handler(self, item, scope, requirements, join_plan, mode)

    # ── 各类检查 ────────────────────────────────────────────────────────

    def _capability_guard(
        self,
        item: QueryRequirement,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
    ) -> RequirementCheck | None:
        """能力依据缺席时，凡是要查库结构的核对一律判 unknown。

        ⚠ 这一支**不是**"检查通过"：没有能力依据就证明不了任何事。
        返回 None 表示有能力依据，可以继续走正常判定。
        """
        if self.capability is not None:
            return None
        return self._unknown(
            item,
            reason_code="capability_unavailable",
            message=(
                f"系统读不到库结构说明，无法核实「{item.surface}」"
                "有没有对应的数据依据。已按无法核实处理，不声称已落实。"
            ),
        )

    def _check_time(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """时间范围：必须能在某个真实列上找到承载该时间范围的谓词。

        判据分三层，**顺序不可调换**（先看能不能绑，再看得没做到）：

        1. 绑不上列（能力层说相关表没有时间列）→ `repair`。
           这是 SC-05：`2023年有多少客户？` 的 `customer` 表没有任何时间列。
        2. 绑得上，但 SQL 里没有任何谓词提到那列 → `repair`
           （条件被静默丢掉）。
        3. 绑得上、谓词也在，但运算符/区间形态对不上 → `unknown`
           （看懂了，证不出等价）。
        """
        tables = self._requirement_tables(requirements, join_plan, scope)
        time_columns = self._columns_of_kind(tables, "time")
        if not tables or not time_columns:
            return self._repair(
                item,
                reason_code="time_condition_unsupported",
                message=(
                    f"问题限定了时间范围（{item.surface}），但相关业务表"
                    "没有可用的时间依据。系统不能凭空推断年份，也不会"
                    "把全量数据当作该时间范围的结果。"
                ),
                tables=tables,
            )

        predicates = _scope_predicates(scope)
        for column_key in time_columns:
            found = self._predicate_touching(predicates, column_key)
            if found is None:
                continue
            normalized = (item.normalized or "").strip()
            # 有确定区间时按区间比对，**优先于**下面的形态粗判。
            # 相对时间（「去年」「上个月」）的 normalized 存的是原词，
            # 粗判抽不出年份，只能判"不可证"放行；而区间是日历归一化的结果，
            # 能直接和 SQL 里的日期字面量对齐（2026-10-17 接上）。
            if item.time_range is not None:
                verdict = _compare_time_range(found, item.time_range)
                if verdict is True:
                    return self._pass(
                        item,
                        f"时间范围 {item.time_range[0]}~{item.time_range[1]}"
                        f"已在 {column_key} 上落实。",
                        column=column_key, sql_scope=found,
                    )
                if verdict is False:
                    actual = self._dates_in(found)
                    shown = f"{actual[0]}~{actual[1]}" if len(actual) >= 2 else "未给出完整区间"
                    return self._repair(
                        item,
                        reason_code="time_range_mismatch",
                        message=(
                            f"问题要求的时间范围是 {item.time_range[0]}~{item.time_range[1]}，"
                            f"但 SQL 在 {column_key} 上写的是 {shown}，两者不一致。"
                        ),
                        column=column_key, sql_scope=found,
                    )
                # verdict is None：SQL 里没有可解析的日期字面量（如 relative 计算），
                # 退回原来的形态粗判，不因新判据反而放宽旧口径。
            if normalized and not self._time_expression_matches(found, normalized):
                # 相对/锚点词（「至今」「去年」）没有确定的日历边界，
                # 说"形态对不上"没用 —— 要说清楚**缺的是什么**：一个可
                # 核对的截止锚点。SC-12 的教训：「今年至今」被套成全年
                # 得 0，答案里没说边界是哪天，用户拿到的是碰巧数字。
                if normalized in {"至今", "到现在", "到目前为止", "今年以来"} or "至今" in normalized:
                    return self._unknown(
                        item, "time_predicate_form_unclear",
                        f"「{item.surface}」需要明确的截止锚点（截至今日还是截至数据"
                        f"快照日）才能核实，SQL 在 {column_key} 上写的时间范围"
                        "无法静态证明与之等价。",
                        column=column_key, sql_scope=found,
                    )
                return self._unknown(
                    item, "time_predicate_form_unclear",
                    f"SQL 在 {column_key} 上写了条件，但其形态与"
                    f"问题要求的时间范围（{normalized}）是否等价无法静态证明。",
                    column=column_key, sql_scope=found,
                )
            return self._pass(
                item, f"时间范围已在 {column_key} 上落实。", column=column_key,
                sql_scope=found,
            )
        return self._repair(
            item,
            reason_code="time_condition_dropped",
            message=(
                f"问题限定了时间范围（{item.surface}），但 SQL 的筛选条件里"
                "没有对应的时间列比较，条件被丢弃了。"
            ),
            tables=tables, columns=time_columns,
        )

    def _check_value_filter(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """取值筛选：要求的取值必须出现在某条谓词里。"""
        predicates = _scope_predicates(scope)
        wanted = (item.normalized or item.surface or "").strip()
        if not wanted:
            return self._unknown(item, "value_filter_empty", "取值条件为空，无法核对。")

        for predicate in predicates:
            if self._predicate_mentions_value(predicate, wanted):
                return self._pass(
                    item, f"取值筛选（{wanted}）已落实。", sql_scope=predicate.sql(),
                )

        # 取值不在任何谓词里。能不能确定它"应该"在哪一列？
        # 确定不了就 unknown —— 可能是相关列没有登记取值样例。
        tables = self._requirement_tables(requirements, join_plan, scope)
        candidates = self._category_candidates(tables, wanted)
        if candidates:
            return self._repair(
                item,
                reason_code="value_filter_dropped",
                message=(
                    f"问题限定了取值（{item.surface}），相关字段 "
                    f"{', '.join(candidates)} 可以表达这个筛选，但 SQL 没有使用它。"
                ),
                tables=tables, columns=candidates,
            )
        return self._unknown(
            item, "value_filter_column_unclear",
            f"问题限定了取值（{item.surface}），但无法确定它应落在哪个字段上，"
            "因此不能声称该筛选已落实。",
            tables=tables,
        )

    def _check_negation(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """否定：必须有 NOT / != / NOT IN 之类的反向谓词。

        只找"列名出现"是不够的 —— `WHERE country = 'Brazil'` 与
        `WHERE country != 'Brazil'` 的列完全相同、语义相反。
        """
        predicates = _scope_predicates(scope)
        negations = [p for p in predicates if _is_negated(p)]
        if not negations:
            return self._repair(
                item,
                reason_code="negation_dropped",
                message=(
                    f"问题要求排除某些取值（{item.surface}），但 SQL 里没有"
                    "任何反向筛选条件。"
                ),
            )
        # 有反向谓词。至于是不是排的那一项，交给取值筛选去判 ——
        # 这里只证明"否定这个动作发生了"。
        return self._pass(
            item, f"否定条件已落实（{len(negations)} 处反向筛选）。",
            sql_scope="\n".join(p.sql() for p in negations),
        )

    def _check_ranking(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """排序与前 N 条：必须同时有 ORDER BY 与对应 LIMIT。

        `LIMIT 1000` 不能冒充前 N 条（母方案 §5.4 第 8 类）——
        它是"最多返回这么多行"的兜底，不是"只要前 3 条"。
        """
        order_keys = _scope_order_keys(scope)
        limit = _scope_limit(scope)
        wanted = _ranking_count(item)
        if not order_keys:
            return self._repair(
                item,
                reason_code="ranking_order_missing",
                message=f"问题要求取前几名（{item.surface}），但 SQL 没有排序条件。",
            )
        if wanted is None:
            return self._unknown(
                item, "ranking_count_unclear",
                f"问题要求取前几名，但具体条数无法从「{item.surface}」确定。",
                sql_scope="\n".join(key.sql() for key in order_keys),
            )
        if limit is None:
            return self._repair(
                item,
                reason_code="ranking_limit_missing",
                message=(
                    f"问题要求取前 {wanted} 名，但 SQL 没有对应的 LIMIT；"
                    "不能用 LIMIT 1000 之类的兜底行数冒充前 N 条。"
                ),
                sql_scope="\n".join(key.sql() for key in order_keys),
            )
        if limit != wanted:
            return self._repair(
                item,
                reason_code="ranking_limit_mismatch",
                message=(
                    f"问题要求取前 {wanted} 名，SQL 的 LIMIT 是 {limit}，"
                    "条数不一致。"
                ),
                sql_scope="\n".join(key.sql() for key in order_keys),
            )
        return self._pass(
            item, f"前 {wanted} 名已通过 ORDER BY + LIMIT {limit} 落实。",
            sql_scope="\n".join(key.sql() for key in order_keys),
        )

    def _check_grouping(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """分组维度：GROUP BY 必须出现。"""
        group_keys = _scope_group_keys(scope)
        if not group_keys:
            return self._repair(
                item,
                reason_code="grouping_dropped",
                message=(
                    f"问题要求按维度分组（{item.surface}），但 SQL 没有 GROUP BY，"
                    "分组维度被丢掉了。"
                ),
            )
        return self._pass(
            item, f"分组维度已落实（{len(group_keys)} 个分组键）。",
            sql_scope="\n".join(key.sql() for key in group_keys),
        )

    def _check_distinct_grain(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """去重粒度：要求 DISTINCT，或分组汇总本身保证唯一。"""
        if _scope_distinct(scope):
            return self._pass(item, "去重粒度已通过 DISTINCT 落实。", sql_scope="DISTINCT")
        if _scope_group_keys(scope):
            # 按某维度分组汇总时，结果天然每组一行 —— 这满足"不重复"。
            return self._pass(
                item, "去重粒度已通过分组汇总落实。",
                sql_scope="\n".join(key.sql() for key in _scope_group_keys(scope)),
            )
        return self._repair(
            item,
            reason_code="distinct_grain_dropped",
            message=(
                f"问题要求按去重口径统计（{item.surface}），但 SQL 既没有 DISTINCT"
                "也没有分组汇总，同一实体可能被重复计数。"
            ),
        )

    def _check_join_grain(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """关联粒度：一对多展开时，父表字段不能直接合计。

        判据只看**可静态证明**的两种安全形态：DISTINCT、或按子表键分组。
        其它写法（如 SELECT 父表字段 + SUM(子表行)）证不出是否重复累计，
        按母方案 §5.4 末句处理成 `unknown`，不猜 `pass`。
        """
        if _scope_distinct(scope):
            return self._pass(item, "关联粒度已通过 DISTINCT 落实。", sql_scope="DISTINCT")
        group_keys = _scope_group_keys(scope)
        if group_keys:
            return self._pass(
                item, "关联粒度已通过分组汇总落实。",
                sql_scope="\n".join(key.sql() for key in group_keys),
            )
        return self._unknown(
            item, "join_grain_unverified",
            f"问题对结果粒度有要求（{item.surface}），但当前 SQL 既没有 DISTINCT"
            "也没有分组汇总，无法静态证明是否重复计数。",
        )

    def _check_measure(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """指标：聚合类指标必须有聚合函数。

        **只有"用户真的要聚合"时才检查**。`_is_aggregate_measure` 认两种情况：
        ① 显式聚合词（合计/总计/平均/count_question）② 词表里本就带聚合语义的指标
        （客户数/订单数/发票数）。

        ⚠️ 裸指标词（`normalized="noun"`，如「金额最高的3张发票」里的"金额"）
        **不是聚合要求**，直接 pass：用户问的是"哪几张发票"，SQL 写
        `ORDER BY total DESC LIMIT 3` 完全正确，若在这里返回 unknown，
        会让满屏正确查询被标黄，且 `unknown` 永远归不了零、闸门永远升不到 enforce。
        这不是"证不出"，而是"根本没有这条要求" —— 不该由核对器承担。
        """
        if not _is_aggregate_measure(item):
            if _is_noun_measure(item):
                return self._pass(item, f"指标（{item.surface}）是名词性指标，未要求聚合。")
            return self._unknown(
                item, "measure_kind_unclear",
                f"这条指标（{item.surface}）是不是聚合口径无法从字面确定。",
            )
        if _has_aggregate(scope):
            return self._pass(item, f"指标（{item.surface}）已通过聚合表达式落实。")
        return self._repair(
            item,
            reason_code="measure_aggregate_missing",
            message=(
                f"问题要求聚合指标（{item.surface}），但 SQL 里没有聚合函数；"
                "不能用单行值或 NULL 代替统计结果。"
            ),
        )

    def _check_object(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """对象：SQL 必须真的查到那张表。"""
        tables = self._requirement_tables(requirements, join_plan, scope)
        if not tables:
            return self._unknown(
                item, "object_table_unclear",
                f"说不清「{item.surface}」对应哪张业务表，因此不能声称已查询到它。",
            )
        used = self._used_tables(scope, tables)
        if not used:
            return self._repair(
                item,
                reason_code="object_table_missing",
                message=(
                    f"问题要查的业务对象（{item.surface}）对应表 "
                    f"{', '.join(tables)}，但 SQL 没有查询这些表。"
                ),
                tables=tables,
            )
        return self._pass(
            item, f"业务对象已落实（查询了 {', '.join(used)}）。", tables=used,
        )

    def _check_document_scope(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """文档范围：**不适用** SQL 核对。"""
        # 文档类问句走的是检索链路，不是 NL2SQL。把它交给 SQL 核对器
        # 会得到必然失败的结论，所以明确记为"不适用"而非 unknown。
        return self._not_applicable(
            item, "document_scope_out_of_sql",
            "文档范围类要求由文档检索链路核对，不在 SQL 覆盖范围内。",
        )

    def _check_cross_source_object(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """跨源对象：编号字面量必须出现在 SQL 的某条谓词里。"""
        wanted = (item.normalized or item.surface or "").strip()
        if not wanted:
            return self._unknown(item, "cross_source_object_empty", "跨源对象编号为空。")
        for predicate in _scope_predicates(scope):
            if self._predicate_mentions_value(predicate, wanted):
                return self._pass(
                    item, f"跨源对象（{wanted}）已在筛选条件中落实。",
                    sql_scope=predicate.sql(),
                )
        return self._repair(
            item,
            reason_code="cross_source_object_dropped",
            message=(
                f"问题点名了具体对象（{wanted}），但 SQL 的筛选条件里没有它，"
                "等于把点名的对象换成了全量。"
            ),
        )

    def _check_value_scope(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """条件作用域：条件必须出现在 WHERE/HAVING/JOIN ON 之一。"""
        # 本类要求的 surface 是「筛选」这类动作词，真正的条件由同句其它
        # 要求承载。故本检查只证明"SQL 里存在承载条件的结构"。
        predicates = _scope_predicates(scope)
        if predicates:
            return self._pass(
                item, "条件作用域已落实（存在筛选/连接条件）。",
                sql_scope="\n".join(p.sql() for p in predicates),
            )
        return self._repair(
            item,
            reason_code="condition_scope_dropped",
            message=(
                f"问题提到「{item.surface}」，但 SQL 里没有任何筛选或连接条件，"
                "等于把要筛的数据整表返回。"
            ),
        )

    def _check_numeric_range(
        self,
        item: QueryRequirement,
        scope: Any,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        mode: str,
    ) -> RequirementCheck:
        """数值比较：必须有对应运算符与数值的谓词。

        这里刻意**不是**"数值出现就算过"：`WHERE total < 100` 与
        `WHERE total > 100` 语义相反，`>=` 与 `>` 也不等价。
        运算符与数值必须同时匹配，只匹配一边就判 `unknown`。
        """
        operator, value = _numeric_range_parts(item)
        if not operator or not value:
            return self._unknown(
                item, "numeric_range_unparsed",
                f"这条数值条件（{item.surface}）的运算符或数值无法从字面确定。",
            )
        tables = self._requirement_tables(requirements, join_plan, scope)
        loose_hit = False
        for predicate in _scope_predicates(scope):
            if self._predicate_matches_comparison(predicate, operator, value):
                return self._pass(
                    item, f"数值条件（{item.surface}）已落实。", sql_scope=predicate.sql(),
                )
            if self._predicate_mentions_value(predicate, value):
                loose_hit = True
        if loose_hit:
            # 数值在，运算符对不上：证不出等价，但也不能说条件被丢了。
            return self._unknown(
                item, "numeric_range_operator_unclear",
                f"SQL 里出现了 {value}，但找不到与「{operator}」等价的比较，"
                "无法静态证明这条数值条件被落实。",
                tables=tables,
            )
        return self._repair(
            item,
            reason_code="numeric_range_dropped",
            message=(
                f"问题限定了数值范围（{item.surface}），但 SQL 的条件里"
                f"没有与之匹配的 {operator} {value} 比较。"
            ),
            tables=tables,
        )

    # ── 绑定辅助 ────────────────────────────────────────────────────────

    def _requirement_tables(
        self,
        requirements: QueryRequirements,
        join_plan: JoinPlan | None,
        scope: Any = None,
    ) -> tuple[str, ...]:
        """这条要求可能落在哪些表上。

        优先用 join_plan 的定位结果；没有计划时退回清单里的对象
        （`normalized` 已被 normalizer 归一成表名，形如 `customer`）；
        两者都没有时退回 SQL 实际引用的表 —— **仍不猜全库**。
        """
        if join_plan is not None and join_plan.tables:
            return tuple(item.lower() for item in join_plan.tables)
        objects = requirements.of_kind("object", "cross_source_object")
        tables: list[str] = []
        for item in objects:
            normalized = (item.normalized or "").strip().lower()
            # 带点的是 `schema.table` 规范身份；裸表名也接受 ——
            # normalizer 给的就是裸表名，拒掉会让对象检查永远 unknown。
            if normalized and normalized not in tables:
                tables.append(normalized)
        if not tables and scope is not None:
            # 清单里没点名对象（例如只说了"2023年"）。此时从 SQL 实际
            # 用到的表反推 —— **不猜全库**：只认 SQL 真正出现的表。
            for name in _sql_table_names(scope):
                key = name.lower()
                if key not in tables:
                    tables.append(key)
        return tuple(tables)

    def _columns_of_kind(self, tables: Iterable[str], kind: str) -> tuple[str, ...]:
        """从能力层取这些表里某一类列。能力层缺席 → 空元组（调用方判 unknown/repair）。"""
        if self.capability is None:
            return ()
        found: list[str] = []
        for table in tables:
            if kind == "time":
                columns = self.capability.time_columns(table)
            elif kind == "category":
                columns = self.capability.category_columns(table)
            elif kind == "amount":
                columns = self.capability.amount_columns(table)
            elif kind == "quantity":
                columns = self.capability.quantity_columns(table)
            else:
                columns = ()
            for column in columns:
                key = f"{table}.{column}".lower()
                if key not in found:
                    found.append(key)
        return tuple(found)

    def _category_candidates(
        self,
        tables: Iterable[str],
        wanted: str,
    ) -> tuple[str, ...]:
        """哪些维度列**真的登记过**这个取值。

        只认 `sample_values` 里出现过的值 —— 母方案 §5.2 明令
        「不能按常识猜取值」。一列都没登记过 → 空元组 → 调用方判 unknown。
        """
        if self.capability is None:
            return ()
        wanted_folded = (wanted or "").casefold()
        found: list[str] = []
        for table in tables:
            for column in self.capability.category_columns(table):
                values = self.capability.normalized_values(table, column)
                if any(value.casefold() == wanted_folded for value in values):
                    key = f"{table}.{column}".lower()
                    if key not in found:
                        found.append(key)
        return tuple(found)

    def _used_tables(self, scope: Any, tables: Iterable[str]) -> tuple[str, ...]:
        """SQL 里实际查到的表（按规范身份比较）。"""
        from app.nl2sql.sql_scopes import resolve_identity as _resolve_identity

        known = {item.lower() for item in tables}
        used: list[str] = []
        for node in scope.find_all(_table_type()):
            identity = _resolve_identity(node, known)
            if identity and identity not in used:
                used.append(identity)
        return tuple(used)

    def _predicate_touching(
        self,
        predicates: Iterable[Any],
        column_key: str,
    ) -> str | None:
        """找第一条真的比较了这个列的谓词，返回其 SQL 文本。

        只看 `mandatory_conjuncts` 给出的必然成立谓词 —— OR 分支里的
        比较可能在某个分支为假，不能算"条件已落实"。
        """
        _, _, column = column_key.rpartition(".")
        wanted = column.lower()
        for predicate in predicates:
            conjuncts = _mandatory_conjuncts(predicate)
            if conjuncts is None:
                continue
            for conjunct in conjuncts:
                for node in conjunct.find_all(_column_type()):
                    if (node.name or "").lower() != wanted:
                        continue
                    table = _table_of(node)
                    if table and not column_key.lower().endswith("." + table.lower()):
                        continue
                    if not _is_comparison(conjunct):
                        continue
                    return conjunct.sql()
        return None

    def _predicate_mentions_value(self, predicate: Any, wanted: str) -> bool:
        """谓词里是否出现这个取值字面量（字符串或数字）。"""
        folded = (wanted or "").casefold()
        for conjunct in _mandatory_conjuncts(predicate) or ():
            for node in conjunct.find_all(_literal_type()):
                text = str(getattr(node, "this", "")).casefold()
                if text and (text == folded or folded in text or text in folded):
                    return True
        return False

    def _predicate_matches_comparison(
        self,
        predicate: Any,
        operator: str,
        value: str,
    ) -> bool:
        """谓词里是否有 `operator value` 这种比较。

        运算符与数值必须**同时**对上，且运算符必须真的是比较节点的一部分
        （不是字符串字面量里恰好出现的字符）。
        """
        wanted_op = _operator_symbol(operator)
        for conjunct in _mandatory_conjuncts(predicate) or ():
            if not _conjunct_has_operator(conjunct, wanted_op):
                continue
            if str(getattr(conjunct, "this", "")).casefold() == value.casefold():
                return True
            for node in conjunct.find_all(_literal_type()):
                if str(getattr(node, "this", "")).casefold() == value.casefold():
                    return True
        return False

    def _time_expression_matches(self, sql_scope: str, normalized: str) -> bool:
        """时间形态是否对得上。

        目前只做**粗判**：SQL 文本里出现要求的年份就算对得上。
        区间端点（`2023-01-01` 与 `2023-12-31`）需要列类型与边界语义，
        那超出静态可证范围，故这里只要年份出现即算 `pass`，
        更细的等价性由 `time_predicate_form_unclear` 兜底。
        """
        year = _year_of(normalized)
        if not year:
            return False
        return year in (sql_scope or "")

    # ── 结论构造 ────────────────────────────────────────────────────────

    def _pass(
        self,
        item: QueryRequirement,
        message: str,
        *,
        column: str = "",
        tables: tuple[str, ...] = (),
        sql_scope: str = "",
    ) -> RequirementCheck:
        return RequirementCheck(
            requirement_id=item.id,
            outcome="pass",
            reason_code="requirement_covered",
            message=message,
            sql_scope=sql_scope,
            details=_details(item, column=column, tables=tables),
        )

    def _repair(
        self,
        item: QueryRequirement,
        *,
        reason_code: str,
        message: str,
        column: str = "",
        tables: tuple[str, ...] = (),
        columns: tuple[str, ...] = (),
        sql_scope: str = "",
    ) -> RequirementCheck:
        return RequirementCheck(
            requirement_id=item.id,
            outcome="repair",
            reason_code=reason_code,
            message=message,
            sql_scope=sql_scope,
            details=_details(item, column=column, tables=tables, columns=columns),
        )

    def _unknown(
        self,
        item: QueryRequirement,
        reason_code: str,
        message: str,
        *,
        column: str = "",
        tables: tuple[str, ...] = (),
        sql_scope: str = "",
    ) -> RequirementCheck:
        return RequirementCheck(
            requirement_id=item.id,
            outcome="unknown",
            reason_code=reason_code,
            message=message,
            sql_scope=sql_scope,
            details=_details(item, column=column, tables=tables),
        )

    def _not_applicable(
        self,
        item: QueryRequirement,
        reason_code: str,
        message: str,
    ) -> RequirementCheck:
        # 刻意不复用四值：`RequirementOutcome` 只有 pass/repair/unknown，
        # "不适用"必须有地方放，否则文档类要求会被迫落进某一态。
        return RequirementCheck(
            requirement_id=item.id,
            outcome="unknown",
            reason_code=reason_code,
            message=message,
            details={**_details(item), "applicable": False},
        )

    # ── 反向核对：多余条件 ──────────────────────────────────────────────

    def _check_extraneous_filters(
        self,
        scope: Any,
        requirements: QueryRequirements,
    ) -> list[RequirementCheck]:
        """SQL 里凭空出现的**字面量等值过滤**（意见单 20261016 方向 2）。

        与正向检查方向相反：正向问"用户要的做到没有"，这里问
        "SQL 做的用户要过没有"。依据 = 要求清单（含 `source="session"`
        的继承条，它们已经并进 `requirements`）里的 value_filter/时间/
        数值条件的 `normalized` 值。对不上依据的文本字面量报 `unknown`
        —— 静态证不出它一定错，只说明"这一条没有用户的依据"。

        **刻意收窄**（意见单建议 2）：只查 `col = '文本常量'` 形态。
        数字/日期常量是范围谓词的组成部分，由正向 time/numeric 检查
        对账；ORDER BY/LIMIT/类型转换不在本检查射程内。避免把合法
        片段判成多余、打坏 T4/T5。
        """
        # 有依据的取值集合：本轮清单 + 继承条（后者已并入 requirements）。
        backed: set[str] = set()
        for item in requirements.requirements:
            if not item.actionable:
                continue
            for value in (item.normalized or "", item.surface or ""):
                value = value.strip().casefold()
                if value:
                    backed.add(value)

        seen: set[tuple[str, str]] = set()
        checks: list[RequirementCheck] = []
        for predicate in _scope_predicates(scope):
            for conjunct in _mandatory_conjuncts(predicate) or []:
                if conjunct.key != "eq":
                    continue
                # 等值两侧任一侧是列、另一侧是文本字面量才算取值过滤。
                left, right = conjunct.this, conjunct.expression
                column_node = left if isinstance(left, exp.Column) else (
                    right if isinstance(right, exp.Column) else None
                )
                literal_node = right if column_node is left else (
                    left if column_node is right else None
                )
                if column_node is None or literal_node is None:
                    continue
                value = _literal_value(literal_node)
                if not value:
                    continue
                key = (column_node.name, value)
                if key in seen:
                    continue
                seen.add(key)
                if value.strip().casefold() in backed:
                    continue  # 有依据（本轮说了或继承来的）
                check = RequirementCheck(
                    requirement_id=f"extraneous:{key[0]}={value}",
                    outcome="unknown",
                    reason_code="extraneous_literal_filter",
                    message=(
                        f"SQL 在 {column_node.name} 上过滤了取值「{value}」，"
                        "但用户本轮没有提出这个条件，继承的上下文里也没有依据。"
                        "静态核对无法证明它一定错（可能是合理的业务默认），"
                        "但结果可能因这个未声明的条件而与预期不同。"
                    ),
                    details={
                        "kind": "extraneous_filter",
                        "surface": f"{column_node.name}={value}",
                        "span": [0, 0],
                        "column": column_node.name,
                        "value": value,
                    },
                )
                checks.append(check)
        return checks


# ── 分派表 ──────────────────────────────────────────────────────────────

_DISPATCH = {
    "time": RequirementValidator._check_time,
    "value_filter": RequirementValidator._check_value_filter,
    "negation": RequirementValidator._check_negation,
    "ranking": RequirementValidator._check_ranking,
    "grouping": RequirementValidator._check_grouping,
    "distinct_grain": RequirementValidator._check_distinct_grain,
    "join_grain": RequirementValidator._check_join_grain,
    "measure": RequirementValidator._check_measure,
    "object": RequirementValidator._check_object,
    "document_scope": RequirementValidator._check_document_scope,
    "cross_source_object": RequirementValidator._check_cross_source_object,
    "value_scope": RequirementValidator._check_value_scope,
    "numeric_range": RequirementValidator._check_numeric_range,
}

#: 需要"知道库里有什么"才能判定的要求类型。能力依据缺席时这些一律 unknown，
#: 不能因为"没数据"就默认通过（那会变成静默放行）。
_CAPABILITY_BOUND_KINDS = frozenset({
    "time",
    "value_filter",
    "numeric_range",
    "grouping",
    "join_grain",
    "object",
    "measure",
})


# ── 模块级纯函数 ────────────────────────────────────────────────────────


#: 不算"多余"的常量形态。日期/数字常量通常是生成器拼的范围（已由正向
#: 时间/数值检查对账），本检查只盯**文本字面量**——那是"取值"该出现的地方。
_TEXT_LITERAL_PATTERN = re.compile(r"^N?'(.*)'$", re.DOTALL)


def _literal_value(node: Any) -> str:
    """取等值比较右端的文本字面量；非文本字面量返回空串。"""
    if node is None:
        return ""
    text = getattr(node, "sql", lambda *_: "")()
    if not text:
        return ""
    match = _TEXT_LITERAL_PATTERN.match(text.strip())
    return match.group(1) if match else ""


def _details(
    item: QueryRequirement,
    *,
    column: str = "",
    tables: tuple[str, ...] = (),
    columns: tuple[str, ...] = (),
) -> dict[str, Any]:
    """核对结果的公共附注：把要求原文一并带上，便于人工回查。"""
    return {
        "kind": item.kind,
        "surface": item.surface,
        "span": list(item.span),
        "normalized": item.normalized,
        "column": column,
        "tables": list(tables),
        "columns": list(columns),
    }


def _column_type():
    import sqlglot

    return sqlglot.exp.Column


def _table_type():
    import sqlglot

    return sqlglot.exp.Table


def _literal_type():
    import sqlglot

    return sqlglot.exp.Literal


def _table_of(node: Any) -> str:
    """列节点的表限定名（`a.b` 取 `a`）；没有则空串。"""
    table = getattr(node, "table", "")
    return str(table or "")


def _sql_table_names(scope: Any) -> tuple[str, ...]:
    """SQL 实际引用到的表名（`schema.table` 与裸 `table` 都收，不猜）。

    调用方（`_requirement_tables`）只在「清单没点名对象」时用它兜底 ——
    拿 SQL 真正用到的表去问能力层，比把全库表都拉进来靠谱得多。
    """
    table_type = _table_type()
    names: list[str] = []
    for node in scope.find_all(table_type):
        identity = _table_identity(node)
        if identity and identity not in names:
            names.append(identity)
    return tuple(names)


def _is_comparison(node: Any) -> bool:
    """这个表达式是不是一次比较（=, !=, <, >, IN, BETWEEN…）。"""
    """这个表达式是不是一次比较（=, !=, <, >, IN, BETWEEN…）。"""
    import sqlglot

    comparison_types = (
        sqlglot.exp.EQ, sqlglot.exp.NEQ, sqlglot.exp.GT, sqlglot.exp.GTE,
        sqlglot.exp.LT, sqlglot.exp.LTE, sqlglot.exp.In, sqlglot.exp.Between,
        sqlglot.exp.Is, sqlglot.exp.Like,
    )
    return isinstance(node, comparison_types)


def _is_negated(node: Any) -> bool:
    """这个表达式里是否有反向语义（NOT / != / NOT IN / IS NOT）。"""
    import sqlglot

    if isinstance(node, (sqlglot.exp.NEQ, sqlglot.exp.Not)):
        return True
    if isinstance(node, sqlglot.exp.Is):
        return bool(node.args.get("expression"))
    if isinstance(node, sqlglot.exp.In):
        return False
    return False


def _is_noun_measure(item: QueryRequirement) -> bool:
    """这条指标是"名词性"的（说了问什么指标，没说要不要聚合）。

    提取器对裸指标词（金额/数量/利润/成本/收入）写 `normalized="noun"`。
    它们与 `_is_aggregate_measure` 认的聚合词**互斥**：一个词要么在
    `_AGGREGATE_TERMS`/`count_question` 里，要么是裸名词，不会两边都算。
    """
    return (item.normalized or "").casefold() == "noun"


def _is_aggregate_measure(item: QueryRequirement) -> bool:
    """这条指标是不是聚合口径。

    只认提取器写明的聚合词。认不出来的（如"增长率"）交回 `unknown` ——
    增长率可以写成窗口函数、可以写成两次查询相除，静态判不出"必须聚合"。
    """
    normalized = (item.normalized or "").casefold()
    surface = (item.surface or "").casefold()
    aggregate_markers = ("合计", "总计", "总共", "一共", "平均", "均值", "count_question")
    return any(marker in normalized or marker in surface for marker in aggregate_markers)


def _has_aggregate(scope: Any) -> bool:
    import sqlglot

    return any(isinstance(node, sqlglot.exp.AggFunc) for node in scope.walk())


def _ranking_count(item: QueryRequirement) -> int | None:
    """从 normalized 里取前 N 条的 N。取不到返回 None（调用方判 unknown）。"""
    match = re.search(r"(\d+)", item.normalized or "")
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    # 没说数字的"最高/最低"默认一条。
    if item.surface:
        return 1
    return None


#: 中文比较词 → SQL 运算符。**必须覆盖提取器 `_NUMERIC_COMPARISON_RE` 的全部方向词**：
#: 漏一个就会出现「SQL 写对了却判 unknown」的假阴性。
_NUMERIC_OPERATOR_MAP: dict[str, tuple[str, ...]] = {
    "大于等于": (">=",),
    "不小于": (">=",),
    "不低于": (">=",),
    "大于": (">",),
    "超过": (">",),
    "多于": (">",),
    "高于": (">",),
    "小于等于": ("<=",),
    "不大于": ("<=",),
    "不超过": ("<=",),
    "低于": ("<",),
    "少于": ("<",),
    "小于": ("<",),
    "等于": ("=",),
    "就是": ("=",),
}


def _numeric_range_parts(item: QueryRequirement) -> tuple[str, str]:
    """把 `大于100元` 拆成 (`>`, `100`)。拆不开返回两个空串。"""
    normalized = (item.normalized or "").strip()
    if not normalized:
        return "", ""
    for word, symbols in _NUMERIC_OPERATOR_MAP.items():
        if normalized.startswith(word):
            return symbols[0], normalized[len(word):].strip()
    # 已经是符号开头（`>=100`）时直接拆。
    match = re.match(r"^(>=|<=|!=|>|<|=)\s*(.+)$", normalized)
    if match:
        return match.group(1), match.group(2).strip()
    return "", ""


def _operator_symbol(operator: str) -> str:
    return operator.strip() if operator else ""


def _conjunct_has_operator(conjunct: Any, operator: str) -> bool:
    """这个合取项里是否**结构上**含 `operator` 比较。

    刻意不查 SQL 文本：`WHERE note = '> 100'` 的文本里也有 `>`，
    但它不是一次真正的比较。只认比较节点的 `key`
    （`gt`/`gte`/`lt`/`lte`/`eq`/`neq`），这是 sqlglot 的结构事实。
    """
    if not operator:
        return True

    wanted = {
        ">": "gt", ">=": "gte", "<": "lt", "<=": "lte",
        "=": "eq", "!=": "neq", "<>": "neq",
    }.get(operator)
    if wanted is None:
        return False
    return any(node.key == wanted for node in conjunct.walk() if _is_comparison(node))


def _year_of(normalized: str) -> str:
    """从 `2023` / `2023-03` / `2023-Q1` 里取年份。"""
    match = re.search(r"(19|20)\d{2}", normalized or "")
    return match.group(0) if match else ""


#: SQL 片段里的 ISO 日期字面量。只认同 `YYYY-MM-DD` 全写形态：
#: `DATE '2024-05-01'` 与裸 `'2024-05-01'` 都算，`2024`、`'2024'` 不算 ——
#: 年份本身就是粗判要处理的，交给 `_time_expression_matches`，不在这里重复。
_ISO_DATE_RE = re.compile(r"((?:19|20)\d{2}-\d{2}-\d{2})")


def _dates_in(sql_scope: str) -> list[str]:
    """按出现顺序取出 SQL 片段里的 ISO 日期字面量。"""
    return _ISO_DATE_RE.findall(sql_scope or "")


def _compare_time_range(
    sql_scope: str,
    expected: tuple[str, str],
    *,
    tolerance_days: int = 1,
) -> bool | None:
    """把 SQL 片段里的日期字面量与要求区间对齐。

    返回三态，**刻意不用布尔**，因为"证不出"与"证伪"必须分开：
      - True  —— 区间对上了
      - False —— 区间明确对不上（这是 repair，会打回重写）
      - None  —— SQL 里没有可解析的日期字面量（相对日期运算、`CURRENT_DATE`
        之类），无从比对，由调用方退回原有判据

    **容差必须是 1 天，不能再放宽**：本函数要认的是"月份/季度有没有选对"，
    而端点上唯一需要容忍的写法差异是左闭右开与闭合区间的互换 ——
    `invoice_date < '2024-06-01'` 与 `<= '2024-05-31'` 是同一天，差 1 天。
    2026-10-17 最初取 31 天，实测把"上个月"（端点差整整 30 天）判成了对上，
    把要抓的错放过 —— 这类偏移恰好落在一个月内，放宽容差等于关掉判据。

    比较方式：取 SQL 里最早与最晚的日期字面量作为实际区间端点。
    SQL 里有多段区间时（如两次比较），这样做会把区间撑大，宁可判对上也不
    误伤 —— 多段时间条件由 `_scope_predicates` 另行给出，不在本函数判。
    """
    dates = _dates_in(sql_scope)
    if len(dates) < 2:
        return None
    actual_start, actual_end = dates[0], dates[-1]
    expected_start, expected_end = expected
    try:
        a_start = datetime.strptime(actual_start, "%Y-%m-%d").date()
        a_end = datetime.strptime(actual_end, "%Y-%m-%d").date()
        e_start = datetime.strptime(expected_start, "%Y-%m-%d").date()
        e_end = datetime.strptime(expected_end, "%Y-%m-%d").date()
    except ValueError:
        # 区间本身不合法（上游给的格式不对）—— 不在这里报错，
        # 交给调用方按"证不出"走原判据，避免新判据成为新的失败源。
        return None
    return (
        abs((a_start - e_start).days) <= tolerance_days
        and abs((a_end - e_end).days) <= tolerance_days
    )


__all__ = [
    "RequirementValidator",
    "VALIDATOR_VERSION",
    "REQUIREMENTS_VERSION",
]
