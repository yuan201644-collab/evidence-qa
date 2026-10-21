"""工作问题引导的半自动知识构建；不读取标准答案或封存问题。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))
import ontology_experiment as core
from semantic_layer_experiment import MeasuredClient, sha256, write_json

ASSETS = ROOT / "eval/builder"
SOURCE_URL = "https://raw.githubusercontent.com/microsoft/sql-server-samples/master/samples/databases/adventure-works/oltp-install-script/instawdb.sql"


def official_definitions() -> dict[str, Any]:
    """机械提取官方字段注释和建表声明；来源文件只读，不执行其中DDL。"""
    source = ASSETS / "official_instawdb_source.sql"
    text = source.read_text(encoding="utf-8-sig")
    descriptions: dict[str, Any] = {}
    pattern = re.compile(r"N'MS_Description', N'((?:[^']|'')*)', N'SCHEMA', \[([^]]+)\], N'TABLE', \[([^]]+)\](?:, N'COLUMN', \[([^]]+)\])?")
    for number, line in enumerate(text.splitlines(), 1):
        match = pattern.search(line)
        if match:
            description, schema, table, column = match.groups()
            key = schema + "." + table + ("." + column if column else "")
            descriptions[key] = {"description": description.replace("''", "'"), "line": number, "url": SOURCE_URL}
    return {"source_url": SOURCE_URL, "source_sha256": sha256(source), "definitions": descriptions}


def indexes(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], set[tuple[str, str]]]:
    tables = {t["schema"] + "." + t["table"]: t for t in raw["tables"]}
    fields = {key + "." + c["name"]: c for key, t in tables.items() for c in t["columns"]}
    edges: set[tuple[str, str]] = set()
    for key, table in tables.items():
        for fk in table.get("foreign_keys", []):
            ref = fk["references"]
            target = ref.get("schema", "public") + "." + ref["table"]
            for left, right in zip(fk["columns"], ref["columns"]):
                pair = (key + "." + left, target + "." + right)
                edges.add(pair)
                edges.add(pair[::-1])
    return tables, fields, edges


def audit_entry(entry: dict[str, Any], raw: dict[str, Any], executor: core.GuardedExecutor,
                definitions: dict[str, Any]) -> dict[str, Any]:
    """结构事实检查与只读探查，不把执行成功当业务口径正确。"""
    tables, fields, edges = indexes(raw)
    issues: list[dict[str, str]] = []
    observations: list[dict[str, Any]] = []
    references = entry.get("evidence_refs", [])
    if not isinstance(references, list) or not references:
        issues.append({"code": "no_source_reference", "detail": "缺少来源引用，需要审查"})
    for reference in references:
        if reference.startswith("metadata:") and reference[9:] not in tables and reference[9:] not in fields:
            issues.append({"code": "unknown_reference", "detail": reference})
        if reference.startswith("official:") and reference[9:] not in definitions["definitions"]:
            issues.append({"code": "unknown_reference", "detail": reference})
    for key in entry.get("tables", []):
        if key not in tables:
            issues.append({"code": "unknown_table", "detail": key})
    mapped = entry.get("mapping", {}).get("fields", [])
    for key in mapped:
        if key not in fields:
            issues.append({"code": "unknown_field", "detail": str(key)})
            continue
        schema, table, column = key.split(".")
        query = f'SELECT count(*) AS total,count("{column}") AS nonnull,count(DISTINCT "{column}") AS distinct_count FROM "{schema}"."{table}"'
        result = executor.execute(query)
        observation = {"id": "field:" + key, "sql": query, "rows": result.rows, "error": result.error}
        if result.error:
            issues.append({"code": "field_probe_error", "detail": key})
        elif result.rows[0]["nonnull"] == 0:
            issues.append({"code": "all_null_field", "detail": key + "；引用作观察可以，直接计算需修订"})
        elif result.rows[0]["distinct_count"] <= 30:
            values_sql = f'SELECT DISTINCT "{column}" AS value FROM "{schema}"."{table}" WHERE "{column}" IS NOT NULL ORDER BY "{column}" LIMIT 30'
            values = executor.execute(values_sql)
            observation["value_probe"] = {"sql": values_sql, "rows": values.rows, "error": values.error}
            expression = entry.get("mapping", {}).get("expression", "")
            if isinstance(expression, str) and expression and not values.error:
                # 只检查低基数字段的明确字符串等值条件，不能用有限样例反证高基数值。
                try:
                    parsed = core.sqlglot.parse_one(expression, read="postgres")
                    known = {str(row["value"]) for row in values.rows}
                    for equality in parsed.find_all(core.exp.EQ):
                        left, right = equality.this, equality.expression
                        if isinstance(right, core.exp.Column):
                            left, right = right, left
                        if isinstance(left, core.exp.Column) and left.name == column and isinstance(right, core.exp.Literal) and right.is_string and right.this not in known:
                            issues.append({"code": "unsupported_value", "detail": key + "=" + right.this + "不在完整低基数取值中"})
                except (ValueError, core.sqlglot.errors.ParseError):
                    issues.append({"code": "expression_needs_review", "detail": key})
        observations.append(observation)
    for edge in entry.get("mapping", {}).get("join_edges", []):
        if isinstance(edge, dict):
            pair = (edge.get("left", ""), edge.get("right", ""))
            if any(value not in fields for value in pair):
                issues.append({"code": "unknown_join_field", "detail": str(pair)})
            elif pair not in edges:
                issues.append({"code": "not_declared_fk", "detail": str(pair) + "；可能合法但需另证"})
    for probe in entry.get("probes", [])[:3]:
        query = probe.get("sql", "")
        result = executor.execute(query)
        observations.append({"id": "proposed:" + str(probe.get("id", "")), "sql": query,
                             "purpose": probe.get("purpose"), "rows": result.rows[:30], "error": result.error})
        if result.error:
            issues.append({"code": "proposed_probe_error", "detail": str(probe.get("id", "")) + ":" + result.error})
    evidence = {key: definitions["definitions"][key] for key in set(mapped + entry.get("tables", []))
                if key in definitions["definitions"]}
    return {"id": entry["id"], "issues": issues, "observations": observations, "official_definitions": evidence,
            "semantic_truth_automatically_proved": False}


def extract_json(response: str) -> dict[str, Any]:
    text = response.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("模型输出不是对象")
    return value


def check_entries(value: dict[str, Any]) -> list[dict[str, Any]]:
    entries = value.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
        raise ValueError("条目数量必须1至20")
    ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or entry["id"] in ids:
            raise ValueError("条目编号格式或唯一性错误")
        ids.add(entry["id"])
        for key in ("title", "description"):
            if not isinstance(entry.get(key), str) or not entry[key] or len(entry[key]) > 1600:
                raise ValueError("文字格式或长度超界：" + key)
        for key in ("aliases", "tables", "constraints", "evidence_refs"):
            if not isinstance(entry.get(key), list) or len(entry[key]) > 20:
                raise ValueError("列表格式或长度超界：" + key)
        if not isinstance(entry.get("mapping"), dict) or not isinstance(entry["mapping"].get("fields"), list):
            raise ValueError("映射格式错误")
        if len(entry.get("probes", [])) > 3:
            raise ValueError("探查次数超界")
    return entries


FORMAT = '''只输出JSON对象 {"entries":[{"id":"英文稳定编号","title":"业务概念","aliases":["业务别名"],"tables":["模式.表"],"description":"口径、粒度、用途，未确定的明确注明","mapping":{"fields":["模式.表.字段"],"expression":"可选计算表达式，不能包含整题SQL或答案","join_edges":[{"left":"模式.表.字段","right":"模式.表.字段"}]},"constraints":["适用条件，含歧义应澄清"],"evidence_refs":["metadata:模式.表.字段"],"probes":[{"id":"编号","purpose":"需检验的具体事实","sql":"仅SELECT，只读统计或有限取值，不能计算某道题的最终答案"}]}]}。最多16个概念，每概念探查最多2个。覆盖重复出现的业务实体、度量、条件与操作，不逐题建立答案条目，不写题号、整题、年度最终汇总值。只能使用提供的数据字段，不推测未定义的行业默认口径。'''


def generate(output: Path, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
    system = ("你是业务知识构建器。根据构建问题和真实元数据起草可复用业务知识，模型不观察标准答案。" if stage == "draft" else
              "你是来源核验后的业务知识修订器。保持候选编号和概念范围，只根据真实探查、字段定义和审查意见修订候选。优先区分字段有无数据与业务定义，不因SQL成功就声称语义正确。若口径无证据，保留歧义并要求澄清。可移除无依据映射，不新增超出原候选范围的概念。证据refs允许metadata:字段/表、official:字段/表、probe:条目id。") + FORMAT
    started = time.perf_counter()
    response = client.chat(system, json.dumps(payload, ensure_ascii=False, default=str))
    (output / (stage + "_raw_response.txt")).write_text(response, encoding="utf-8")
    write_json(output / (stage + "_calls.json"), {"calls": client.calls, "elapsed_seconds": time.perf_counter() - started})
    parsed = extract_json(response)
    check_entries(parsed)
    parsed.update(version=stage + "-v0.1", model=client.model)
    write_json(output / (stage + "_knowledge.json"), parsed)
    return parsed


def lock_builder_sources(output: Path, allow_initial: bool) -> None:
    paths = [Path(__file__), ROOT / "eval/ontology_experiment.py", ROOT / "eval/semantic_layer_experiment.py",
             ROOT / "app/nl2sql/validator.py", ROOT / "app/nl2sql/executor.py", ROOT / "app/nl2sql/config.py",
             ROOT / "app/rag/llm.py", ROOT / "app/rag/config.py", ASSETS / "official_instawdb_source.sql",
             ASSETS / "review_protocol_v0.1.json"]
    hashes = {str(p.relative_to(ROOT)): sha256(p) for p in paths}
    lock = output / "builder_source_lock.json"
    if lock.exists():
        if core.read(lock)["source_sha256"] != hashes:
            raise RuntimeError("构建首次调用后源码变化，不允许静默继续")
    elif allow_initial:
        write_json(lock, {"source_sha256": hashes, "before_first_model_call": True,
                   "preparation_hashes_superseded_before_model_calls": True})
        for path in paths:
            target = output / "builder_source_snapshot" / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    else:
        raise RuntimeError("尚未锁定构建源码")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["prepare", "draft", "ground", "revise", "finalize", "challenges"], required=True)
    parser.add_argument("--run-dir", type=Path)
    args = parser.parse_args()
    core.load_env_file()
    if args.stage == "prepare":
        output = ROOT / "eval/reports" / ("run-builder-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        output.mkdir(parents=True, exist_ok=False)
        old = core.read(ROOT / "eval/ontology/cases_v0.3.json")
        workload = [{"id": c["id"], "question": c["question"]} for c in old["cases"] if c["split"] == "dev"]
        write_json(output / "construction_questions.json", workload)
        raw = core.read(ROOT / "eval/ontology/schema_profile_v0.1.json")
        write_json(output / "common_schema.json", raw)
        write_json(output / "official_definitions.json", official_definitions())
        sources = [Path(__file__), ASSETS / "official_instawdb_source.sql", ROOT / "eval/ontology_experiment.py", ROOT / "eval/semantic_layer_experiment.py"]
        write_json(output / "construction_freeze.json", {"source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in sources},
                   "workload_sha256": sha256(output / "construction_questions.json"), "schema_sha256": sha256(output / "common_schema.json"),
                   "data_before": core.fingerprint(raw, core.aw_dsn()), "builder_reads_heldout": False,
                   "max_candidate_concepts": 16, "model": MeasuredClient().model})
        print(str(output), flush=True)
        return 0
    if not args.run_dir:
        raise ValueError("后续阶段须给run-dir")
    output = args.run_dir.resolve(strict=True)
    lock_builder_sources(output, allow_initial=args.stage == "draft")
    raw = core.read(output / "common_schema.json")
    executor = core.GuardedExecutor(raw, core.aw_dsn())
    definitions = core.read(output / "official_definitions.json")
    if args.stage == "draft":
        generate(output, "draft", {"construction_questions": core.read(output / "construction_questions.json"),
                                    "schema": core.PromptCatalog(raw).render_ddl()})
    elif args.stage == "ground":
        draft = core.read(output / "draft_knowledge.json")
        started = time.perf_counter()
        audits = [audit_entry(entry, raw, executor, definitions) for entry in draft["entries"]]
        write_json(output / "grounding_audit.json", {"audits": audits, "elapsed_seconds": time.perf_counter() - started})
        generate(output, "grounded", {"draft": draft, "source_audits": audits})
    elif args.stage == "revise":
        review = core.read(output / "independent_review.json")
        generate(output, "reviewed", {"candidate": core.read(output / "grounded_knowledge.json"),
                                      "source_audits": core.read(output / "grounding_audit.json"), "review": review})
    elif args.stage == "finalize":
        review = core.read(output / "final_review.json")
        source = output / ("reviewed_knowledge.json" if (output / "reviewed_knowledge.json").exists() else "grounded_knowledge.json")
        candidate = core.read(source)
        accepted = {r["id"] for r in review["entries"] if r["decision"] == "accept"}
        entries = [entry for entry in candidate["entries"] if entry["id"] in accepted]
        write_json(output / "verified_knowledge.json", {"version": "verified-v0.1", "entries": entries,
                  "rejected_ids": [entry["id"] for entry in candidate["entries"] if entry["id"] not in accepted],
                  "source_sha256": sha256(source), "review_sha256": sha256(output / "final_review.json"),
                  "human_reviewed": True, "is_fully_autonomous": False})
        write_json(output / "construction_integrity.json", {"unchanged": core.read(output / "construction_freeze.json")["data_before"] == core.fingerprint(raw, core.aw_dsn())})
    else:
        challenges = core.read(ASSETS / "verifier_challenges_v0.1.json")
        records = [dict(challenge_id=entry["id"], audit=audit_entry(entry, raw, executor, definitions), expected=entry.get("expected_auto_issue"), expected_review=entry.get("expected_review_issue")) for entry in challenges["entries"]]
        write_json(output / "challenge_results.json", {"records": records, "is_controlled_test": True})
    print("构建阶段完成：" + args.stage, flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print("构建中止：" + type(error).__name__ + ":" + str(error), file=sys.stderr)
        raise SystemExit(1)
