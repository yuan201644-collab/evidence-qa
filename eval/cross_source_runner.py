#!/usr/bin/env python
"""调用真实 /api/ask 执行跨源留出题，并与单独 gold 文件做可复核的结构化比对。

标准答案不会随请求发送给系统。自动比对检查状态、证据定位、指定数值和缺项，不替代盲审。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.tools.cross_source_policy import EARLY_STOP_ALLOWED_REASONS

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "eval" / "cross_source" / "heldout"
REPORTS = ROOT / "eval" / "reports" / "cross_source"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [row["case_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"重复 case_id：{path}")
    return rows


def ask(endpoint: str, case_id: str, question: str, timeout: float) -> tuple[dict[str, Any], float]:
    body = json.dumps(
        {"question": question, "session_id": "cross-source-heldout-" + case_id},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/api/ask",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload, (time.perf_counter() - started) * 1000


def extract_status(response: dict[str, Any]) -> str | None:
    for step in reversed(response.get("trace", [])):
        output = step.get("output")
        if step.get("tool") == "cross_source_alignment" and isinstance(output, dict):
            return output.get("status")
    for step in response.get("trace", []):
        output = step.get("output")
        if step.get("tool") == "cross_source_sql" and isinstance(output, dict):
            if output.get("status") == "not_found":
                return "not_found"
    return None


def _trace_absence_checks(response: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for step in response.get("trace", []):
        output = step.get("output")
        if not isinstance(output, dict):
            continue
        if step.get("tool") == "cross_source_sql":
            inputs = step.get("input", {})
            inputs = inputs if isinstance(inputs, dict) else {}
            executed_sql = output.get("sql")
            if not isinstance(executed_sql, str) or not executed_sql.strip():
                executed_sql = inputs.get("query")
            checks.extend(
                {
                    **check,
                    "_trace_source": "cross_source_sql",
                    "_executed_sql": executed_sql,
                    "_executed_row_count": output.get("row_count"),
                    "_sql_parameters": inputs.get("parameters", inputs.get("params")),
                    "_work_order_id": inputs.get("work_order_id"),
                }
                for check in output.get("absence_checks", [])
                if isinstance(check, dict)
            )
        elif (
            step.get("tool") == "cross_source_absence_check"
            and output.get("derived_from_executed_query") is True
        ):
            checks.append({
                "table": output.get("table"),
                "filters": output.get("filters"),
                "row_count": output.get("row_count"),
                "_trace_source": "cross_source_absence_check",
            })
    return checks


def _sql_literal(value: Any) -> str | None:
    """Render a trace-recorded bound parameter as a SQL literal for AST checking."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return repr(value)
    return None


def _bind_trace_sql_parameters(sql: str, check: dict[str, Any]) -> str | None:
    parameter_values = check.get("_sql_parameters")
    if parameter_values is None and check.get("_work_order_id") is not None:
        parameter_values = [check["_work_order_id"]]
    if not isinstance(parameter_values, (list, tuple)):
        parameter_values = []
    marker = re.compile(r"%s|\?")
    matches = list(marker.finditer(sql))
    if not matches:
        return sql
    if len(matches) != len(parameter_values):
        return None
    rendered = [_sql_literal(value) for value in parameter_values]
    if any(value is None for value in rendered):
        return None
    chunks: list[str] = []
    previous = 0
    for match, value in zip(matches, rendered):
        chunks.extend((sql[previous:match.start()], value or ""))
        previous = match.end()
    chunks.append(sql[previous:])
    return "".join(chunks)


def _sql_literal_value(expression: Any, exp: Any) -> Any:
    if isinstance(expression, exp.Literal):
        if expression.is_string:
            return expression.this
        try:
            return int(expression.this)
        except (TypeError, ValueError):
            try:
                return float(expression.this)
            except (TypeError, ValueError):
                return None
    if isinstance(expression, exp.Boolean):
        return expression.this
    if isinstance(expression, exp.Null):
        return None
    return None


def _sql_proves_absence_filters(check: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Verify a zero-row result came from exactly the declared simple table filters."""
    sql = check.get("_executed_sql")
    if not isinstance(sql, str) or not sql.strip():
        return False
    bound_sql = _bind_trace_sql_parameters(sql, check)
    if bound_sql is None:
        return False
    try:
        import sqlglot
        from sqlglot import exp

        statements = sqlglot.parse(bound_sql, read="postgres")
    except Exception:
        return False
    if len(statements) != 1:
        return False
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        return False
    expected_table = expected.get("table")
    expected_filters = expected.get("filters")
    if not isinstance(expected_table, str) or not isinstance(expected_filters, dict) or not expected_filters:
        return False

    def table_path(table: Any) -> str:
        return ".".join(
            part.casefold() for part in (table.catalog, table.db, table.name) if part
        )

    # `row_count` is the result size, so it proves no matching rows only when
    # the result is a direct, non-aggregated single-table SELECT. Keep this
    # deliberately narrow: joins, sampling, grouping, distinct, limits, and
    # nested queries can all make a zero result unrelated to the requested
    # absence condition.
    forbidden_nodes = (
        exp.Join,
        exp.Group,
        exp.Having,
        exp.Limit,
        exp.Offset,
        exp.Distinct,
        exp.Subquery,
        exp.With,
        exp.Order,
        exp.AggFunc,
        exp.Window,
    )
    if any(isinstance(node, forbidden_nodes) for node in tree.walk()):
        return False
    if any(tree.args.get(key) is not None for key in ("top", "sample", "qualify", "locks", "into")):
        return False

    from_clause = tree.args.get("from_")
    source = from_clause.this if isinstance(from_clause, exp.From) else None
    if not isinstance(source, exp.Table) or from_clause.expressions:
        return False
    tables = list(tree.find_all(exp.Table))
    if len(tables) != 1 or tables[0] is not source:
        return False
    table_paths = {table_path(source)}
    if not next(iter(table_paths)):
        return False
    aliases = {(source.alias_or_name or source.name).casefold(): next(iter(table_paths))}
    expected_path = expected_table.casefold()
    matching_tables = {
        path for path in table_paths
        if path == expected_path or (
            "." not in expected_path and path.rsplit(".", 1)[-1] == expected_path
        )
    }
    if len(matching_tables) != 1:
        return False
    target_table = next(iter(matching_tables))
    where = tree.args.get("where")
    if where is None:
        return False

    def flatten_conjunction(expression: Any) -> list[Any]:
        if isinstance(expression, exp.Paren):
            return flatten_conjunction(expression.this)
        if isinstance(expression, exp.And):
            return flatten_conjunction(expression.this) + flatten_conjunction(expression.expression)
        return [expression]

    predicates: dict[str, Any] = {}
    clauses = flatten_conjunction(where.this)
    if len(clauses) != len(expected_filters):
        return False
    for equality in clauses:
        if not isinstance(equality, exp.EQ):
            return False
        left, right = equality.this, equality.expression
        if not isinstance(left, exp.Column) and isinstance(right, exp.Column):
            left, right = right, left
        if not isinstance(left, exp.Column):
            return False
        table_alias = left.table.casefold() if left.table else ""
        if table_alias:
            source_table = aliases.get(table_alias, table_alias)
        else:
            source_table = next(iter(table_paths))
        if source_table != target_table:
            return False
        literal_value = _sql_literal_value(right, exp)
        if literal_value is None:
            return False
        column = left.name.casefold()
        if column in predicates:
            return False
        predicates[column] = literal_value
    normalized_expected: dict[str, Any] = {}
    for column, expected_value in expected_filters.items():
        if not isinstance(column, str) or not column or column.casefold() in normalized_expected:
            return False
        if expected_value is None or not isinstance(expected_value, (str, int, float, bool)):
            return False
        normalized_expected[column.casefold()] = expected_value
    return predicates.keys() == normalized_expected.keys() and all(
        type(predicates[column]) is type(value) and predicates[column] == value
        for column, value in normalized_expected.items()
    )


def _absence_checks_match(
    response: dict[str, Any], expected_checks: list[dict[str, Any]]
) -> bool:
    """Require executor metadata plus a located human-readable negative SQL reference."""
    if not expected_checks:
        return True
    observed = _trace_absence_checks(response)
    refs = response.get("refs", [])
    negative_phrases = ("未返回记录", "未返回匹配记录", "无匹配行", "没有匹配行", "0 行")
    for expected in expected_checks:
        table = expected.get("table")
        filters = expected.get("filters", {})
        matching_metadata = [
            check
            for check in observed
            if check.get("table") == table and check.get("filters") == filters
        ]
        metadata_ok = bool(matching_metadata) and all(
            type(check.get("row_count")) is int
            and check.get("row_count") == 0
            and (
                check.get("_trace_source") != "cross_source_sql"
                or (
                    type(check.get("_executed_row_count")) is int
                    and check.get("_executed_row_count") == check.get("row_count")
                    and _sql_proves_absence_filters(check, expected)
                )
            )
            for check in matching_metadata
        )
        filter_tokens = [re.compile(
            rf"(?<![\w]){re.escape(str(field))}={re.escape(str(value))}(?![\w])"
        ) for field, value in filters.items()]
        reference_ok = any(
            ref.get("kind") == "sql"
            and ref.get("table") == table
            and ref.get("row_pk") is None
            and all(token.search(str(ref.get("snippet", ""))) for token in filter_tokens)
            and any(phrase in str(ref.get("snippet", "")) for phrase in negative_phrases)
            for ref in refs
        )
        if not metadata_ok or not reference_ok:
            return False
    return True


def _canonical_sql_reference_text(value: str) -> str:
    """Normalize equivalent equipment labels while retaining every identifier/value."""
    compact = re.sub(r"\s+", "", value)
    return compact.replace("设备编号", "设备")


def _row_pk_identity(value: Any) -> tuple[tuple[str | None, str], ...] | None:
    """Normalize composite keys while retaining field names when they are present.

    Examples such as ``WO|accepted`` and
    ``work_order_id=WO&event_type=accepted`` identify the same two-column row.
    Keep component order, tuple length, and keyed field names so differently keyed
    rows with the same values do not collapse.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    parts = re.split(r"[|&]", value)
    identity: list[tuple[str | None, str]] = []
    for part in parts:
        component = part.strip()
        if not component:
            return None
        field_name: str | None = None
        if "=" in component:
            field_name, component = component.split("=", 1)
            field_name = field_name.strip().casefold()
        if not component:
            return None
        if field_name == "":
            return None
        identity.append((field_name, component))
    return tuple(identity)


def _row_pk_identity_matches(
    expected: tuple[tuple[str | None, str], ...],
    actual: tuple[tuple[str | None, str], ...],
) -> bool:
    """Match legacy unkeyed values while rejecting conflicts between keyed fields."""
    if len(expected) != len(actual):
        return False
    return all(
        expected_value == actual_value
        and not (expected_key and actual_key and expected_key != actual_key)
        for (expected_key, expected_value), (actual_key, actual_value) in zip(expected, actual)
    )


def _column_reference_covers(actual: Any, expected: Any) -> bool:
    """Allow a ref's slash-joined field list to cover one required field."""
    if not isinstance(actual, str) or not isinstance(expected, str):
        return False
    actual_columns = {part.strip() for part in actual.split("/") if part.strip()}
    expected_columns = {part.strip() for part in expected.split("/") if part.strip()}
    return bool(expected_columns) and expected_columns.issubset(actual_columns)


def _aware_iso_timestamp(value: Any) -> datetime | None:
    """Parse only timezone-aware ISO-8601 values into a comparable UTC instant."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _column_values_equivalent(actual: Any, expected: Any) -> bool:
    """Allow exact values, plus equivalent instants for two aware ISO timestamps."""
    if actual == expected:
        return True
    actual_instant = _aware_iso_timestamp(actual)
    expected_instant = _aware_iso_timestamp(expected)
    return actual_instant is not None and actual_instant == expected_instant


def _sql_reference_contains_column_value(snippet: str, column: str, value: str) -> bool:
    """Require exact field/value tags when present; prose-only refs cannot prove bindings."""
    text = re.sub(r"\s+", " ", snippet).replace("设备编号", "设备")
    expected_value = re.sub(r"\s+", " ", value).replace("设备编号", "设备").strip()
    suffix_start = snippet.rfind(" [")
    if suffix_start >= 0 and snippet.endswith("]"):
        suffix_body = snippet[suffix_start + 2:-1]
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", suffix_body):
            labels: dict[str, str] = {}
            token: list[str] = []
            tokens: list[str] = []
            quoted = False
            escaped = False
            for char in suffix_body:
                if escaped:
                    token.append(char)
                    escaped = False
                elif char == "\\" and quoted:
                    token.append(char)
                    escaped = True
                elif char == '"':
                    token.append(char)
                    quoted = not quoted
                elif char == ";" and not quoted:
                    tokens.append("".join(token).strip())
                    token = []
                else:
                    token.append(char)
            if quoted:
                return False
            tokens.append("".join(token).strip())
            try:
                for item in tokens:
                    name, separator, encoded_value = item.partition("=")
                    if (not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip())
                            or name.strip() in labels):
                        return False
                    decoded = json.loads(encoded_value.strip())
                    if not isinstance(decoded, str):
                        return False
                    labels[name.strip()] = decoded
            except (json.JSONDecodeError, TypeError):
                return False
            return _column_values_equivalent(labels.get(column), value)

    has_machine_field_labels = bool(re.search(
        r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]*\s*(?:=|:|：)", text,
    ))
    if not has_machine_field_labels:
        # A value in prose is not bound to the physical column named in the Ref.
        return False
    field_pattern = re.compile(
        rf"(?<![\w])(?:[`\"']?{re.escape(column)}[`\"']?)\s*(?:=|:|：)\s*",
        re.IGNORECASE,
    )
    for match in field_pattern.finditer(text):
        candidate = text[match.end():].lstrip(" `\"'")
        if not candidate.startswith(expected_value):
            continue
        remainder = candidate[len(expected_value):].lstrip("`\"'")
        if not remainder or not (remainder[0].isalnum() or remainder[0] == "_"):
            return True
    return False


def _required_sql_refs_match(
    sql_refs: list[dict[str, Any]], requirements: Any
) -> bool:
    """Require each structured SQL citation contract to match one final Ref."""
    if not isinstance(requirements, list):
        return False
    for expected in requirements:
        if not isinstance(expected, dict):
            return False
        table = expected.get("table")
        expected_pk = _row_pk_identity(expected.get("row_pk"))
        columns = expected.get("required_columns")
        values = expected.get("required_values")
        if (
            not isinstance(table, str) or not table.strip()
            or expected_pk is None
            or not isinstance(columns, list) or not columns
            or any(not isinstance(column, str) or not column.strip() for column in columns)
            or not isinstance(values, dict) or not values
            or any(
                not isinstance(column, str) or not column.strip()
                or not isinstance(value, str) or not value.strip()
                for column, value in values.items()
            )
            or not set(values).issubset(set(columns))
        ):
            return False
        expected_column_list = "/".join(columns)
        if not any(
            ref.get("table") == table
            and (actual_pk := _row_pk_identity(ref.get("row_pk"))) is not None
            and _row_pk_identity_matches(expected_pk, actual_pk)
            and _column_reference_covers(ref.get("column"), expected_column_list)
            and all(
                _sql_reference_contains_column_value(
                    str(ref.get("snippet", "")), column, value,
                )
                for column, value in values.items()
            )
            for ref in sql_refs
        ):
            return False
    return True


def _retrieval_attempts(doc_steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    attempts: list[dict[str, Any]] = []
    malformed = False
    for step in doc_steps:
        value = step.get("output", {}).get("attempts", [])
        if isinstance(value, list):
            attempts.extend(item for item in value if isinstance(item, dict))
            malformed = malformed or any(not isinstance(item, dict) for item in value)
        elif value:
            malformed = True
    return attempts, malformed


def _document_retrieval_steps(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize old RAG and new fact-plan trace shapes without inventing attempts.

    The legacy arm records `retrieve`/`rerank` steps with DocRef hits; the new
    arm additionally emits an explicit `cross_source_doc_retrieval` projection.
    Only the latter carries bounded-attempt metadata.
    """
    steps = [step for step in response.get("trace", [])
             if step.get("tool") == "cross_source_doc_retrieval"]
    if steps:
        return steps
    return [
        step for step in response.get("trace", [])
        if step.get("tool") in {"hybrid_retrieve", "retrieve", "rerank", "generate"}
    ]


def _document_retrieval_hit_steps(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only trace steps that carry hits from an actual RAG retrieval call.

    The private cross-source projection is for bounded-attempt policy metadata;
    it is not itself a retrieval result and must never authorize a citation.
    """
    return [
        step for step in response.get("trace", [])
        if step.get("tool") in {"hybrid_retrieve", "retrieve", "rerank"}
    ]


def _retrieval_policy_check(
    gold: dict[str, Any], response: dict[str, Any]
) -> tuple[bool, list[str]]:
    state = gold.get("expected_retrieval_state")
    doc_steps = _document_retrieval_steps(response)
    alignment_outputs = [step.get("output", {}) for step in response.get("trace", [])
                         if step.get("tool") == "cross_source_alignment"
                         and isinstance(step.get("output"), dict)]
    errors = [
        "actual_stop_reason_not_allowlisted"
        for output in alignment_outputs
        if output.get("stop_reason_code") is not None
        and output.get("stop_reason_code") not in EARLY_STOP_ALLOWED_REASONS
    ]
    if any(
        output.get("status") == "not_determined"
        and output.get("stop_reason_code") not in EARLY_STOP_ALLOWED_REASONS
        for output in alignment_outputs
    ):
        errors.append("not_determined_requires_allowlisted_reason")
    if state is None:
        errors.append("expected_retrieval_state_missing")
    if state == "clause_present_expected":
        if not doc_steps:
            errors.append("expected_document_retrieval_missing")
        if any(output.get("stop_reason_code") in EARLY_STOP_ALLOWED_REASONS
               for output in alignment_outputs):
            errors.append("early_stop_reason_conflicts_with_expected_retrieval")
        attempts, malformed = _retrieval_attempts(doc_steps)
        if malformed:
            errors.append("retrieval_attempts_malformed")
        expected_attempts = gold.get("expected_retrieval_attempts")
        if expected_attempts is not None and len(attempts) != expected_attempts:
            errors.append("retrieval_attempt_count_mismatch")
        expected_statuses = gold.get("expected_retrieval_statuses")
        if expected_statuses is not None and [a.get("status") for a in attempts] != expected_statuses:
            errors.append("expected_retrieval_statuses_mismatch")
    elif state == "bounded_not_found":
        if not doc_steps:
            errors.append("bounded_retrieval_trace_missing")
        attempts, malformed = _retrieval_attempts(doc_steps)
        if malformed:
            errors.append("retrieval_attempts_malformed")
        if len(attempts) != gold.get("expected_retrieval_attempts"):
            errors.append("retrieval_attempt_count_mismatch")
        for field in ("expected_retrieval_strategies", "expected_retrieval_statuses",
                      "expected_candidate_counts"):
            expected_values = gold.get(field)
            if expected_values is not None:
                actual_key = {
                    "expected_retrieval_strategies": "strategy",
                    "expected_retrieval_statuses": "status",
                    "expected_candidate_counts": "candidate_count",
                }[field]
                if [attempt.get(actual_key) for attempt in attempts] != expected_values:
                    errors.append(field + "_mismatch")
        if any(ref.get("kind") == "doc" for ref in response.get("refs", [])):
            errors.append("not_found_must_not_be_presented_as_clause_citation")
    elif state == "retrieval_not_required":
        expected_reason = gold.get("expected_early_stop_reason")
        actual_reason = alignment_outputs[-1].get("stop_reason_code") if alignment_outputs else None
        if expected_reason not in EARLY_STOP_ALLOWED_REASONS:
            errors.append("gold_early_stop_reason_not_allowlisted")
        if actual_reason != expected_reason:
            errors.append("early_stop_reason_mismatch")
        if doc_steps or any(ref.get("kind") == "doc" for ref in response.get("refs", [])):
            errors.append("document_retrieval_after_early_stop")
        alignment_steps = [step for step in response.get("trace", [])
                           if step.get("tool") == "cross_source_alignment"
                           and isinstance(step.get("output"), dict)]
        stop_hit = alignment_steps[-1].get("hit") or [] if alignment_steps else []
        final_refs = response.get("refs", [])
        final_keys = {json.dumps(ref, ensure_ascii=False, sort_keys=True)
                      for ref in final_refs if isinstance(ref, dict)}
        absence_required = gold.get("absence_evidence_required") is True
        located_support = any(
            isinstance(ref, dict)
            and ref.get("kind") == "sql"
            and isinstance(ref.get("table"), str) and ref.get("table")
            and isinstance(ref.get("column"), str) and ref.get("column")
            and isinstance(ref.get("snippet"), str) and ref.get("snippet")
            and ((isinstance(ref.get("row_pk"), str) and ref.get("row_pk"))
                 or (absence_required and ref.get("row_pk") is None))
            and json.dumps(ref, ensure_ascii=False, sort_keys=True) in final_keys
            for ref in stop_hit
        )
        if not located_support:
            errors.append("early_stop_evidence_not_trace_backed")
    elif state is not None:
        errors.append("unknown_expected_retrieval_state")
    return not errors, errors


def compare(case: dict[str, Any], gold: dict[str, Any], response: dict[str, Any], elapsed_ms: float) -> dict[str, Any]:
    refs = response.get("refs", [])
    sql_refs = [ref for ref in refs if ref.get("kind") == "sql"]
    sql_pks = {ref.get("row_pk") for ref in sql_refs if ref.get("row_pk")}
    sql_pk_identities = {
        identity for ref in sql_refs
        if (identity := _row_pk_identity(ref.get("row_pk"))) is not None
    }
    doc_ids = {ref.get("doc_id") for ref in refs if ref.get("kind") == "doc" and ref.get("doc_id")}
    expected_status = gold["expected_status"]
    expected_pks = set(gold.get("expected_evidence_pks", gold.get("expected_sql_pks", [])))
    expected_docs = set(gold.get("expected_doc_ids", []))
    expected_doc = gold.get("expected_contract_id")
    if expected_doc:
        expected_docs.add(expected_doc)
    required_sql_snippets = gold.get("required_sql_snippets", [])
    required_sql_refs = gold.get("required_sql_refs", [])
    required_doc_snippets = gold.get("required_doc_snippets", [])
    sql_snippets = "\n".join(
        _canonical_sql_reference_text(str(ref.get("snippet", "")))
        for ref in refs if ref.get("kind") == "sql"
    )
    status = extract_status(response)
    expected_pk_identities = {
        identity for value in expected_pks
        if (identity := _row_pk_identity(value)) is not None
    }
    pks_ok = len(expected_pk_identities) == len(expected_pks) and all(
        any(_row_pk_identity_matches(expected, actual) for actual in sql_pk_identities)
        for expected in expected_pk_identities
    )
    doc_ok = expected_docs.issubset(doc_ids)
    snippets_ok = all(
        _canonical_sql_reference_text(snippet) in sql_snippets
        for snippet in required_sql_snippets
    )
    sql_refs_ok = _required_sql_refs_match(sql_refs, required_sql_refs)
    doc_snippet_text = "\n".join(
        str(ref.get("snippet", "")) for ref in refs
        if ref.get("kind") == "doc" and ref.get("doc_id") in expected_docs
    )
    doc_snippets_ok = all(snippet in doc_snippet_text for snippet in required_doc_snippets)
    # 零结果的过滤条件和表/字段必须在同一条引用中，不能用无关来源凑齐关键词。
    required_absence_refs = gold.get("required_absence_refs", [])
    absence_ok = all(any(
        ref.get("kind") == "sql" and ref.get("row_pk") is None
        and ref.get("table") == expected["table"]
        and _column_reference_covers(ref.get("column"), expected["column"])
        and all(text in str(ref.get("snippet", "")) for text in expected["snippets"])
        for ref in refs
    ) for expected in required_absence_refs)
    expected_absence_checks = list(gold.get("required_absence_checks", []))
    # Legacy fields are translated to metadata checks; model-generated result prose is ignored.
    expected_absence_checks.extend(
        {"table": expected.get("table"), "filters": expected.get("filters", {})}
        for expected in gold.get("required_negative_evidence", [])
    )
    if expected_absence_checks:
        absence_ok = absence_ok and _absence_checks_match(response, expected_absence_checks)
    outputs = [step.get("output", {}) for step in response.get("trace", [])
               if step.get("tool") == "cross_source_alignment"]
    alignment = outputs[-1] if outputs and isinstance(outputs[-1], dict) else {}
    raw_metric_checks = alignment.get("metric_checks", [])
    metric_checks = [item for item in raw_metric_checks if isinstance(item, dict)] \
        if isinstance(raw_metric_checks, list) else []
    metrics = {
        item["metric"]: item for item in metric_checks
        if isinstance(item.get("metric"), str)
    }
    if "required_metric" in gold:
        expected = gold["required_metric"]
        metric_name = expected.get("end_event")
        matching_metrics = [item for item in metric_checks if item.get("metric") == metric_name]
        metric = matching_metrics[0] if len(matching_metrics) == 1 else None
        if expected.get("within_limit") is True or expected.get("within_limit") is False:
            metrics_ok = bool(metric) and all((
                metric.get("start_event") == expected.get("start_event"),
                metric.get("end_event") == expected.get("end_event"),
                metric.get("elapsed_seconds") == expected.get("elapsed_seconds"),
                metric.get("limit") == expected.get("limit_hours"),
                metric.get("unit") == "hours",
                metric.get("within_limit") == expected.get("within_limit"),
            ))
        elif expected.get("elapsed_seconds") is not None and expected["elapsed_seconds"] < 0:
            invalid = alignment.get("invalid_metrics", [])
            metrics_ok = not metric and any(
                item.get("metric") == metric_name
                and item.get("reason") == "end_event_before_start_event"
                for item in invalid
            )
        else:
            # Missing limit/event or a conflict must not be represented as a valid SLA pass/fail.
            metrics_ok = metric is None
    else:
        metrics_ok = all(
            expected["metric"] in metrics and all(
                metrics[expected["metric"]].get(key) == value for key, value in expected.items()
            ) for expected in gold.get("expected_metric_checks", [])
        )
    mapped_supported_status = (
        status == "supported"
        and expected_status in {"repair_within_limit", "repair_over_limit"}
        and isinstance(gold.get("required_metric"), dict)
        and gold["required_metric"].get("end_event") == "service_restored"
        and gold["required_metric"].get("within_limit") is (
            expected_status == "repair_within_limit"
        )
        and metrics_ok
    )
    status_comparison_available = isinstance(status, str) and bool(status.strip())
    status_ok = status == expected_status or mapped_supported_status
    expected_missing = gold.get("expected_missing_events", gold.get("missing_events"))
    missing_ok = expected_missing is None or sorted(alignment.get("missing_events", [])) == sorted(expected_missing)
    retrieval_ok, retrieval_errors = _retrieval_policy_check(gold, response)
    canonical = lambda ref: json.dumps(ref, sort_keys=True, ensure_ascii=False)
    trace_refs = {
        canonical(ref)
        for step in response.get("trace", [])
        for ref in (step.get("hit") or [])
    }
    # Intermediate retrieval/reranking steps legitimately expose candidates
    # that are not promoted to final citations. The invariant is that every
    # final citation is trace-backed, not that every trace candidate is cited.
    consistency_ok = {canonical(ref) for ref in refs}.issubset(trace_refs)
    retrieval_doc_refs = {
        canonical(ref)
        for step in _document_retrieval_hit_steps(response)
        for ref in (step.get("hit") or [])
        if isinstance(ref, dict) and ref.get("kind") == "doc"
    }
    final_doc_refs = [ref for ref in refs if isinstance(ref, dict) and ref.get("kind") == "doc"]
    document_retrieval_backed = all(canonical(ref) in retrieval_doc_refs for ref in final_doc_refs)
    location_ok = all(
        bool(ref.get("snippet")) and (
            bool(ref.get("table")) and bool(ref.get("column")) if ref.get("kind") == "sql"
            else bool(ref.get("doc_id")) and (bool(ref.get("section_path")) or ref.get("page") is not None)
        ) for ref in refs
    )
    retrieval_state = gold.get("expected_retrieval_state")
    # For M08, refusal is substantively correct only after the gold-declared
    # bounded search state; other retrieval/early-stop requirements are process checks.
    retrieval_decision_ok = retrieval_state is not None and (
        retrieval_state != "bounded_not_found" or retrieval_ok
    )
    correctness_ok = all((status_ok, metrics_ok, missing_ok, retrieval_decision_ok))
    citation_ok = all((
        pks_ok, doc_ok, snippets_ok, sql_refs_ok, doc_snippets_ok, absence_ok, location_ok,
        document_retrieval_backed,
    ))
    trace_ok = all((consistency_ok, retrieval_ok))
    return {
        "case_id": case["case_id"],
        "question": case["question"],
        "expected_status": expected_status,
        "actual_status": status,
        "status_comparison_available": status_comparison_available,
        "status_match": status_ok,
        "expected_sql_pks": sorted(expected_pks),
        "actual_sql_pks": sorted(sql_pks),
        "sql_evidence_present": pks_ok,
        "expected_doc_id": expected_doc,
        "expected_doc_ids": sorted(expected_docs),
        "actual_doc_ids": sorted(doc_ids),
        "document_evidence_present": doc_ok,
        "required_sql_snippets": required_sql_snippets,
        "required_sql_snippets_present": snippets_ok,
        "required_sql_refs": required_sql_refs,
        "required_sql_refs_present": sql_refs_ok,
        "required_doc_snippets": required_doc_snippets,
        "required_doc_snippets_present": doc_snippets_ok,
        "absence_evidence_present": absence_ok,
        "expected_retrieval_state": retrieval_state,
        "retrieval_state_match": retrieval_ok,
        "retrieval_decision_match": retrieval_decision_ok,
        "retrieval_policy_errors": retrieval_errors,
        "metric_values_match": metrics_ok,
        "missing_events_match": missing_ok,
        "refs_trace_consistent": consistency_ok,
        "document_refs_from_actual_retrieval": document_retrieval_backed,
        "evidence_locations_complete": location_ok,
        "correctness_pass": correctness_ok,
        "citation_completeness_pass": citation_ok,
        "strict_pass": correctness_ok and citation_ok,
        "trace_compliance_pass": trace_ok,
        "automatic_checks_pass": all((correctness_ok, citation_ok, trace_ok)),
        "elapsed_ms": round(elapsed_ms, 2),
        "answer": response.get("answer"),
        "trace": response.get("trace", []),
        "refs": refs,
        "need_clarify": response.get("need_clarify"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--questions", type=Path, default=SUITE / "questions.jsonl")
    parser.add_argument("--gold", type=Path, default=SUITE / "gold.jsonl")
    parser.add_argument("--output-dir", type=Path, default=REPORTS)
    args = parser.parse_args()

    questions = read_jsonl(args.questions)
    gold_by_id = {item["case_id"]: item for item in read_jsonl(args.gold)}
    if not questions or {item["case_id"] for item in questions} != set(gold_by_id):
        raise SystemExit("questions 与 gold 的 case_id 必须一一对应且非空")

    results = []
    for case in questions:
        try:
            response, elapsed_ms = ask(args.endpoint, case["case_id"], case["question"], args.timeout)
            results.append(compare(case, gold_by_id[case["case_id"]], response, elapsed_ms))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            results.append({
                "case_id": case.get("case_id"), "question": case.get("question"),
                "automatic_checks_pass": False, "error": f"{type(exc).__name__}: {exc}",
            })

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "created_at_utc": stamp,
        "endpoint": args.endpoint,
        "suite": str(args.questions),
        "case_count": len(results),
        "automatic_pass_count": sum(bool(row.get("automatic_checks_pass")) for row in results),
        "warning": "自动比对仅检查状态与证据定位；必须人工盲审答案语义和证据是否足以支持结论。",
        "results": results,
    }
    output_path = args.output_dir / f"cross-source-{stamp}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"逐题报告：{output_path}")
    print(f"结构化检查：{report['automatic_pass_count']}/{report['case_count']}（不等于最终验收）")
    return 0 if report["automatic_pass_count"] == report["case_count"] else 1


if __name__ == "__main__":
    sys.exit(main())
