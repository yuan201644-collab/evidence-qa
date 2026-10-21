"""为自然语言转 SQL 提供保守的表、字段候选定位。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable

from app.nl2sql.normalizer import AliasTable, load_alias_table
from app.nl2sql.planning_models import LinkCandidate, LinkResult
from app.nl2sql.schema import SchemaCatalog, TableMeta


DEFAULT_DESCRIPTIONS_PATH = Path(__file__).resolve().parents[2] / "infra" / "schema_meta.descriptions.zh.json"


class SchemaLinker:
    """按元数据排序候选；相似度绝不直接视为业务关联。"""

    def __init__(
        self,
        *,
        aliases: AliasTable | None = None,
        descriptions_path: str | Path | None = None,
        semantic_scorer: Callable[[str, str], float] | None = None,
        max_tables: int = 8,
        max_columns: int = 32,
        max_related_tables: int = 12,
    ) -> None:
        self.aliases = aliases
        self.descriptions_path = Path(descriptions_path) if descriptions_path else None
        self.semantic_scorer = semantic_scorer
        self.max_tables = max(1, max_tables)
        self.max_columns = max(1, max_columns)
        self.max_related_tables = max(self.max_tables, max_related_tables)

    def link(self, question: str, catalog: SchemaCatalog, *, intent: dict | None = None) -> LinkResult:
        intent = intent or {}
        source_question = _strip_output_column_instruction(
            str(intent.get("source_question") or question)
        )
        normalized_question = _strip_output_column_instruction(question)
        scoring_question = "\n".join(dict.fromkeys(
            value for value in (source_question, normalized_question) if value
        ))
        text = _norm(scoring_question)
        intent_objects = {str(value).casefold() for value in intent.get("objects", ())}
        intent_measures = tuple(dict.fromkeys(
            str(value).strip() for value in intent.get("measures", ()) if str(value).strip()
        ))
        self._semantic_failed = False
        aliases = self.aliases
        degraded = False
        reason = ""
        if aliases is None:
            try:
                aliases = load_alias_table()
            except (OSError, ValueError):
                aliases = AliasTable(version="unavailable", updated_at="unknown")
                degraded = True
                reason = "中文别名文件不可用，仅使用 schema 元数据匹配"

        descriptions = self._descriptions(catalog)
        table_descriptions = [
            descriptions.get(table.name.lower(), table.description)
            for table in catalog.tables.values()
        ]
        column_descriptions = [
            descriptions.get(
                f"{table.name}.{column.name}".lower(), column.description
            )
            for table in catalog.tables.values()
            for column in table.columns
        ]

        def discriminative_measures(descriptions_for_scope: list[str]) -> tuple[str, ...]:
            selected: list[str] = []
            for measure in intent_measures:
                normalized_measure = _norm(measure)
                if not normalized_measure:
                    continue
                occurrences = sum(
                    normalized_measure in _norm(description)
                    for description in descriptions_for_scope
                )
                # A short measure such as “数量” is useful only when it points
                # to one metadata description; if it appears in many tables it
                # must not fan those tables into the join plan.
                if len(normalized_measure) >= 3 or occurrences == 1:
                    selected.append(measure)
            return tuple(selected)

        table_measures = discriminative_measures(table_descriptions)
        column_measures = discriminative_measures(column_descriptions)
        explicit_alias_owners: dict[str, set[str]] = {}
        for candidate_table in {id(item): item for item in catalog.tables.values()}.values():
            for candidate_column in candidate_table.columns:
                column_aliases = _column_aliases(
                    aliases, candidate_table.name, candidate_column.name
                )
                if column_aliases and self._score(source_question, column_aliases)[0] >= 0.55:
                    explicit_alias_owners.setdefault(
                        candidate_column.name.casefold(), set()
                    ).add(_table_key(candidate_table))

        table_rows: list[LinkCandidate] = []
        column_rows: list[LinkCandidate] = []
        for key in catalog.table_names():
            table = catalog.get_table(key)
            if table is None:
                continue
            qualified = _table_key(table)
            table_aliases = _table_aliases(aliases, table)
            description = descriptions.get(table.name.lower(), table.description)
            table_score, table_terms, table_parts = self._score(
                scoring_question, (table.name, qualified, description, *table_aliases)
            )
            if table.name.casefold() in intent_objects or qualified.casefold() in intent_objects:
                table_score = max(table_score, 0.98)
                table_parts = {**table_parts, "intent_object": 0.98}
                table_terms = tuple(dict.fromkeys(table_terms + (table.name,)))
            measure_matches = tuple(
                measure for measure in table_measures
                if _norm(measure) and _norm(measure) in _norm(description)
            )
            if measure_matches:
                table_score = max(table_score, 0.72)
                table_parts = {**table_parts, "intent_measure_description": 0.72}
                table_terms = tuple(dict.fromkeys(table_terms + measure_matches))
            if table_score > 0:
                table_rows.append(LinkCandidate(
                    object_type="table", schema=table.schema, table=table.name,
                    column=None, score=table_score, score_parts=table_parts,
                    matched_terms=table_terms,
                ))

            for column in table.columns:
                column_key = f"{table.name}.{column.name}"
                column_description = descriptions.get(column_key.lower(), column.description)
                aliases_for_column = _column_aliases(aliases, table.name, column.name)
                value_matches = tuple(
                    value for value, alias_terms in _column_values(
                        aliases, table.schema, table.name, column.name
                    )
                    if any(
                        _value_occurs(source_question, term)
                        for term in (value, *alias_terms)
                    )
                )
                col_score, col_terms, col_parts = self._score(
                    scoring_question,
                    (column.name, column_key, column_description,
                     *aliases_for_column),
                )
                if value_matches:
                    col_score = max(col_score, 0.86)
                    col_parts = {**col_parts, "explicit_value_match": 0.86}
                    col_terms = tuple(dict.fromkeys(col_terms + value_matches))
                alias_score, _, _ = self._score(source_question, aliases_for_column)
                if alias_score:
                    col_parts = {**col_parts, "alias_match": alias_score}
                explicit_owners = explicit_alias_owners.get(column.name.casefold(), set())
                if (
                    explicit_owners
                    and _table_key(table) not in explicit_owners
                    and not value_matches
                ):
                    source_score, _, _ = self._score(
                        source_question,
                        (column.name, column_key, column_description, *aliases_for_column),
                    )
                    if source_score < 0.55:
                        # A normalized alias can become a bare SQL column name.
                        # Do not fan that canonical name or a weak token overlap
                        # back out to every table with a same-named field when the
                        # user's wording identified one specific owner. Exact alias
                        # evidence (or a sufficiently strong semantic match) may
                        # still legitimately select another owner.
                        col_score = 0.0
                        col_terms = ()
                        col_parts = {}
                measure_matches = tuple(
                    measure for measure in column_measures
                    if _norm(measure) and _norm(measure) in _norm(column_description)
                )
                if measure_matches:
                    col_score = max(col_score, 0.78)
                    col_parts = {**col_parts, "intent_measure_description": 0.78}
                    col_terms = tuple(dict.fromkeys(col_terms + measure_matches))
                if col_score > 0:
                    column_rows.append(LinkCandidate(
                        object_type="column", schema=table.schema,
                        table=table.name, column=column.name, score=col_score,
                        score_parts=col_parts, matched_terms=col_terms,
                    ))

        table_rows.sort(key=lambda item: (-item.score, item.schema, item.table))
        column_rows.sort(key=lambda item: (-item.score, item.schema, item.table, item.column or ""))
        selected_tables = table_rows[: self.max_tables]
        selected_columns = column_rows[: self.max_columns]

        # 字段命中可提升其所属表，但不会因此与其他表建立业务关联。
        for column in selected_columns:
            owner = _candidate_table_key(column)
            owner_parts = {"owner_of_column": column.score}
            alias_score = column.score_parts.get("alias_match")
            value_score = column.score_parts.get("explicit_value_match")
            if alias_score:
                owner_parts["explicit_column_alias"] = alias_score
            if value_score:
                owner_parts["explicit_value_match"] = value_score
            existing_index = next(
                (index for index, item in enumerate(selected_tables)
                 if _candidate_table_key(item) == owner),
                None,
            )
            owner_score = column.score * 0.8
            if existing_index is None:
                selected_tables.append(LinkCandidate(
                    object_type="table", schema=column.schema, table=column.table,
                    column=None, score=owner_score,
                    score_parts=owner_parts,
                    matched_terms=column.matched_terms,
                ))
            elif owner_score > selected_tables[existing_index].score:
                existing = selected_tables[existing_index]
                selected_tables[existing_index] = LinkCandidate(
                    object_type="table", schema=existing.schema, table=existing.table,
                    column=None, score=owner_score,
                    score_parts={**existing.score_parts, **owner_parts},
                    matched_terms=tuple(dict.fromkeys(existing.matched_terms + column.matched_terms)),
                )
            elif alias_score or value_score:
                existing = selected_tables[existing_index]
                selected_tables[existing_index] = LinkCandidate(
                    object_type="table", schema=existing.schema, table=existing.table,
                    column=None, score=existing.score,
                    score_parts={**existing.score_parts,
                                 "explicit_column_alias": alias_score},
                    matched_terms=tuple(dict.fromkeys(existing.matched_terms + column.matched_terms)),
                )
        selected_tables = sorted(
            selected_tables, key=lambda item: (-item.score, item.schema, item.table)
        )[: self.max_tables]

        relation_tables = self._foreign_key_closure(selected_tables, catalog)
        ambiguities = _ambiguity_groups(selected_tables)
        metadata_version = getattr(catalog, "contract_version", "") or ""
        degradation_reason = reason
        if not degradation_reason and self._semantic_failed:
            degradation_reason = "向量候选召回失败，仅保留关键词与别名结果"
        elif not degradation_reason and self.semantic_scorer is None:
            degradation_reason = "未注入向量语义召回，采用关键词与别名定位"
        return LinkResult(
            selected_tables=tuple(selected_tables),
            selected_columns=tuple(selected_columns),
            relation_tables=tuple(relation_tables),
            ambiguous_groups=tuple(ambiguities),
            degraded=degraded or self.semantic_scorer is None or self._semantic_failed,
            degradation_reason=degradation_reason,
            metadata_version=metadata_version,
        )

    def _score(self, question: str, terms: tuple[str, ...]) -> tuple[float, tuple[str, ...], dict[str, float]]:
        matches: list[str] = []
        exact = 0.0
        overlap = 0.0
        semantic = 0.0
        normalized_question = _norm(question)
        for raw in terms:
            term = _norm(raw)
            if len(term) < 2:
                continue
            exact_match = (
                _identifier_occurs(question, raw)
                if _is_identifier_term(raw)
                else term in normalized_question
            )
            if exact_match:
                matches.append(raw)
                exact = max(exact, 1.0 if term == normalized_question else min(0.92, 0.55 + len(term) / 30))
            elif _overlap(normalized_question, term):
                overlap = max(overlap, 0.3)
            # 列/表说明是"业务名 + 取值枚举 + 注意事项"的长句（如「采购方式：公开招标/
            # 竞争性磋商/…，约 22% 为空。」）。整段说明永远不可能逐字出现在问句里，
            # 所以上面的 exact 分支对说明永远不成立，只剩 0.3 的 token_overlap 底分——
            # 而 0.5 的选表阈值正好把它挡在外面，于是「按采购方式分组」这类题
            # 只靠弱信号瞎猜表。这里把说明的首段业务名单独拿出来做一次 exact 判定。
            for label in _description_labels(raw):
                normalized_label = _norm(label)
                if len(normalized_label) < 3:
                    continue
                if normalized_label in normalized_question:
                    matches.append(label)
                    exact = max(exact, min(0.92, 0.55 + len(normalized_label) / 30))
                    break
            if self.semantic_scorer is not None:
                try:
                    semantic = max(semantic, float(self.semantic_scorer(normalized_question, raw)))
                except Exception:
                    # 语义召回失败只会降低召回范围，仍可保留关键词候选。
                    self._semantic_failed = True
                    continue
        score = max(exact, overlap, semantic if semantic >= 0.62 else 0.0)
        parts = {key: round(value, 4) for key, value in {
            "exact_or_alias": exact, "token_overlap": overlap, "semantic": semantic
        }.items() if value}
        return score, tuple(dict.fromkeys(matches)), parts

    def _descriptions(self, catalog: SchemaCatalog) -> dict[str, str]:
        path = self.descriptions_path
        if path is None:
            configured = os.environ.get("SCHEMA_META_DESCRIPTIONS_PATH", "").strip()
            path = Path(configured) if configured else _sibling_descriptions_path(catalog)
            if path is None or not path.is_file():
                path = DEFAULT_DESCRIPTIONS_PATH
        if path is None or not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        catalog_database = str(getattr(catalog, "database", "")).casefold()
        catalog_version = str(getattr(catalog, "contract_version", ""))
        sidecar_database = str(raw.get("database", "")).casefold()
        sidecar_version = str(raw.get("contract_version", ""))
        if sidecar_database and sidecar_database != catalog_database:
            return {}
        if sidecar_version and catalog_version and sidecar_version != catalog_version:
            return {}
        descriptions: dict[str, str] = {}
        tables = raw.get("tables") or {}
        if isinstance(tables, list):
            descriptions.update({
                str(item.get("table", "")).lower(): str(item.get("description", ""))
                for item in tables if isinstance(item, dict)
            })
        elif isinstance(tables, dict):
            descriptions.update({str(key).lower(): str(value) for key, value in tables.items()})
        columns = raw.get("columns") or {}
        if isinstance(columns, list):
            descriptions.update({
                str(item.get("column", "")).lower(): str(item.get("description", ""))
                for item in columns if isinstance(item, dict)
            })
        elif isinstance(columns, dict):
            descriptions.update({str(key).lower(): str(value) for key, value in columns.items()})
        return descriptions

    def _foreign_key_closure(
        self, selected: list[LinkCandidate], catalog: SchemaCatalog
    ) -> list[LinkCandidate]:
        seeds = {_candidate_table_key(item) for item in selected}
        visited = set(seeds)
        frontier = set(seeds)
        related: list[LinkCandidate] = []
        for _ in range(2):
            next_frontier: set[str] = set()
            for key in sorted(frontier):
                for source in {id(table): table for table in catalog.tables.values()}.values():
                    for fk in source.foreign_keys:
                        target = _resolve_table(catalog, fk.ref_table)
                        if target is None:
                            continue
                        source_key = _table_key(source)
                        target_key = _table_key(target)
                        neighbor = (target_key if source_key == key else
                                    source_key if target_key == key else None)
                        if neighbor is None or neighbor in visited:
                            continue
                        related_table = _get_table_by_key(catalog, neighbor)
                        if related_table is None:
                            continue
                        visited.add(neighbor)
                        next_frontier.add(neighbor)
                        related.append(LinkCandidate(
                            object_type="table", schema=related_table.schema,
                            table=related_table.name, column=None, score=0.0,
                            score_parts={"foreign_key_closure": 1.0},
                            matched_terms=(), relation_only=True,
                        ))
            frontier = next_frontier
            if len(visited) >= self.max_related_tables or not frontier:
                break
        return related[: max(0, self.max_related_tables - len(seeds))]


def _strip_output_column_instruction(question: str) -> str:
    """Exclude presentation-only output labels from source candidate retrieval."""
    return re.sub(r"输出列名[^\r\n]*", " ", question or "")


def _sibling_descriptions_path(catalog: SchemaCatalog) -> Path | None:
    source_path = getattr(catalog, "source_path", None)
    if not source_path:
        configured_schema = os.environ.get("SCHEMA_META_PATH", "").strip()
        source_path = configured_schema or None
    if not source_path:
        return None
    schema_path = Path(source_path)
    return schema_path.with_name(schema_path.stem + ".descriptions.zh.json")


def _table_aliases(aliases: AliasTable, table: TableMeta) -> tuple[str, ...]:
    names = (table.name, f"{table.schema}.{table.name}")
    return tuple(alias for name in names for alias in aliases.tables.get(name, ()))


def _column_aliases(aliases: AliasTable, table: str, column: str) -> tuple[str, ...]:
    names = (f"{table}.{column}", f"public.{table}.{column}")
    return tuple(alias for name in names for alias in aliases.columns.get(name, ()))


def _column_values(
    aliases: AliasTable, schema: str, table: str, column: str
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    keys = (
        f"{table}.{column}",
        f"{schema}.{table}.{column}",
        f"public.{table}.{column}",
    )
    values: dict[str, tuple[str, ...]] = {}
    for key in keys:
        for value, alias_terms in aliases.values.get(key, {}).items():
            values[value] = tuple(alias_terms)
    return tuple(values.items())


def _value_occurs(question: str, value: str) -> bool:
    """Match a canonical database value as a distinct literal, not a substring."""
    term = (value or "").strip()
    if not term:
        return False
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]*", term):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
        return re.search(pattern, question or "", flags=re.IGNORECASE) is not None
    normalized_term = _norm(term)
    return len(normalized_term) >= 3 and normalized_term in _norm(question)


def _norm(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", (value or "").casefold(), flags=re.UNICODE)


def _is_identifier_term(value: str) -> bool:
    """Treat schema-like English names as tokens, not arbitrary substrings."""
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", (value or "").strip()))


def _identifier_occurs(question: str, identifier: str) -> bool:
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(identifier.strip())}(?![A-Za-z0-9_])"
    return re.search(pattern, question or "", flags=re.IGNORECASE) is not None


def _description_labels(raw: str) -> tuple[str, ...]:
    """从列/表说明里取出业务名候选。

    说明的写法是「业务名：取值枚举或补充说明」或「业务名，单位元。…」，
    业务名总在最前面、被冒号或逗号截断。只取首段，不猜后面的话——
    后面的内容是取值枚举与注意事项，拿去匹配问句只会制造假命中。
    """
    text = (raw or "").strip()
    if not text or len(text) < 8:
        return ()
    head = re.split(r"[:：,，。;；]", text, maxsplit=1)[0].strip()
    labels = [head] if head and head != text else []
    # 「项目预算金额」这类说明没有了冒号，首段就是整句，仍可作一条候选。
    return tuple(labels)


def _overlap(question: str, term: str) -> bool:
    # 两字符碰撞在中英混排中极常见（例如 Chinook 中的 "in" 命中 invoice）。
    # 只把至少三字符的共同片段作为弱证据；正式业务词优先走别名/意图/描述匹配。
    for length in range(min(len(term), len(question), 6), 2, -1):
        if any(term[index:index + length] in question for index in range(len(term) - length + 1)):
            return True
    return False


def _table_key(table: TableMeta) -> str:
    return f"{table.schema}.{table.name}".lower()


def _candidate_table_key(candidate: LinkCandidate) -> str:
    return f"{candidate.schema}.{candidate.table}".lower()


def _get_table_by_key(catalog: SchemaCatalog, key: str) -> TableMeta | None:
    for table in catalog.tables.values():
        if _table_key(table) == key:
            return table
    return None


def _resolve_table(catalog: SchemaCatalog, name: str) -> TableMeta | None:
    target = (name or "").lower()
    matches = [table for table in catalog.tables.values()
               if table.name.lower() == target or _table_key(table) == target]
    return matches[0] if len(matches) == 1 else None


def _ambiguity_groups(candidates: list[LinkCandidate]) -> list[tuple[str, ...]]:
    if len(candidates) < 2:
        return []
    first, second = candidates[:2]
    shared_evidence = set(map(_norm, first.matched_terms)) & set(map(_norm, second.matched_terms))
    if (first.score >= 0.72 and second.score >= 0.72
            and abs(first.score - second.score) <= 0.05 and shared_evidence):
        return [(first.qualified_name, second.qualified_name)]
    return []


#: 公开别名。消歧后需要用同一把尺子重算歧义（见 nl2sql/pipeline.py），
#: 直接 import 私有名会让跨模块依赖显得可疑，故给一个稳定的公开名字。
ambiguity_groups = _ambiguity_groups


__all__ = ["SchemaLinker", "ambiguity_groups"]
