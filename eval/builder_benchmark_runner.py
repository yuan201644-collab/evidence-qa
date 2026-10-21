"""构建器三组对照：固定全结构问答，分别使用无新增、草稿、核验知识。"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import sqlglot
from sqlglot import exp
from app.nl2sql.config import load_env_file
from app.nl2sql.executor import ExecutionResult
from app.nl2sql.generator import build_prompts
from app.nl2sql.normalizer import normalize
from metrics import compare_resultsets, gold_sql_has_order_by
from ontology_experiment import (
    ANCHOR, GuardedExecutor, PromptCatalog, TupleExecutor, aw_dsn,
    fingerprint, security_check, table_keys,
)
from runner import JsonlPredictor, load_cases, run_nl2sql
from semantic_layer_experiment import MeasuredClient, result_rows, sha256, write_json

ARMS = ("A", "B", "C")
PASS = {"pass", "pass_loose"}
KNOWLEDGE_FIELDS = ("id", "title", "aliases", "tables", "description", "mapping", "constraints")


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_case(case: dict[str, Any]) -> dict[str, Any]:
    """仅统一预先定义的评测字段，不修改题目或标准答案。"""
    item = dict(case)
    item.setdefault("kind", "clarify" if item.get("expected_action") == "clarify" else "sql")
    item.setdefault("task", item.get("primary_task"))
    item.setdefault("split", item.get("phase"))
    return item


def prompt_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """三组共享呈现；探查脚本、审核标签和外部答案不能进入求解提示。"""
    return [{k: entry[k] for k in KNOWLEDGE_FIELDS if k in entry} for entry in entries]


def make_prompts(question: str, raw: dict[str, Any], entries: list[dict[str, Any]],
                 previous_error: str | None = None, previous_sql: str = "") -> tuple[str, str]:
    common = "标识符使用双引号，非public表使用模式限定名。严格遵循用户要求的列数、列顺序和排序。"
    common += "月份未要求年月时输出月份数字；内部编号使用ID字段，业务编码使用Number字段。"
    hint = common + "\n【补充业务知识】\n" + json.dumps(prompt_entries(entries), ensure_ascii=False)
    system, user = build_prompts(question, PromptCatalog(raw), hint=hint,
                                previous_error=previous_error, previous_sql=previous_sql,
                                time_anchor="2024-06-15")
    # 显式替换原生成器的纯SQL输出格式，保留其只读与SQL语义约束。
    system = system.replace("只输出一条 SELECT 语句。", "选择SQL动作时只生成一条只读查询。")
    system = system.replace("只输出 SQL 本身：不要解释、不要 Markdown 代码块、不要加任何前后缀文字。",
                            "只输出JSON对象，不要Markdown或其他前后缀。")
    system += ('\n输出格式唯一：{"action":"sql或clarify","sql":"查询或空串",'
               '"clarification":"具体澄清问题或空串"}。'
               '\n问题缺少必要槽位或存在不能由明确业务知识消除的口径歧义时，action=clarify，'
               'sql必须空串，clarification必须指出具体缺失或冲突，并给出选择。'
               '问题已明确时action=sql，clarification空串。不要根据数据库恰好有哪些值替用户选择口径。')
    user = user.replace("请只输出修正后的 SQL。", "请按约定JSON格式修正。")
    user = user.replace("请输出 SQL：", "请输出约定JSON对象：")
    return system, user


def parse_answer(text: str) -> dict[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    obj = json.loads(text)
    if not isinstance(obj, dict) or obj.get("action") not in {"sql", "clarify"}:
        raise ValueError("必须返回sql或clarify动作的JSON对象")
    if not isinstance(obj.get("sql"), str) or not isinstance(obj.get("clarification"), str):
        raise ValueError("sql和clarification必须为字符串")
    if obj["action"] == "sql" and (not obj["sql"].strip() or obj["clarification"].strip()):
        raise ValueError("SQL动作应包含查询且澄清文本为空")
    if obj["action"] == "clarify" and (obj["sql"].strip() or not obj["clarification"].strip()):
        raise ValueError("澄清动作应包含具体问题且SQL为空")
    return {k: obj[k].strip() for k in ("action", "sql", "clarification")}


def clarification_verdict(case: dict[str, Any], answer: dict[str, str]) -> dict[str, Any]:
    groups = case.get("clarification_keywords", [])
    if not groups:
        raise ValueError("澄清题缺少预先定义的关键内容组：" + case["id"])
    text = answer["clarification"].lower()
    hits = [any(str(term).lower() in text for term in group) for group in groups]
    ok = answer["action"] == "clarify" and all(hits)
    return {"kind": "pass" if ok else "fail", "ok": ok,
            "keyword_groups_matched": hits, "rubric": groups,
            "note": "关键词预评分；澄清文本保留供独立语义复核"}


def sql_diagnostics(sql: str, raw: dict[str, Any], case: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
        actual_tables = table_keys(sql, raw)
        metadata = {t["schema"] + "." + t["table"]: {c["name"] for c in t["columns"]} for t in raw["tables"]}
        alias_map = {t.alias_or_name: (t.db or "public") + "." + t.name for t in tree.find_all(exp.Table)}
        fields: set[str] = set()
        ambiguous: list[str] = []
        for col in tree.find_all(exp.Column):
            if col.table:
                target = alias_map.get(col.table)
                if target in metadata and col.name in metadata[target]:
                    fields.add(target + "." + col.name)
            else:
                matches = [t for t in actual_tables if col.name in metadata.get(t, set())]
                if len(matches) == 1:
                    fields.add(matches[0] + "." + col.name)
                elif len(matches) > 1:
                    ambiguous.append(col.name)
        expected_t = set((case or {}).get("required_tables", []))
        expected_f = set((case or {}).get("required_columns", []))
        return {"tables": sorted(actual_tables),
                "columns": sorted({c.sql(dialect="postgres") for c in tree.find_all(exp.Column)}),
                "resolved_fields": sorted(fields), "unqualified_ambiguous_columns": sorted(set(ambiguous)),
                "required_table_coverage": len(expected_t & actual_tables) / len(expected_t) if expected_t else None,
                "required_field_coverage": len(expected_f & fields) / len(expected_f) if expected_f else None,
                "joins": [j.sql(dialect="postgres") for j in tree.find_all(exp.Join)],
                "note": "实际语法树诊断；列解析不处理作用域遮蔽/等价表达，只作诊断；不以唯一标准SQL路径判错，最终执行结果为主"}
    except Exception as exc:
        return {"tables": [], "columns": [], "joins": [], "parse_error": type(exc).__name__}


def solve(case: dict[str, Any], arm: str, repeat: int, raw: dict[str, Any],
          entries: list[dict[str, Any]], executor: GuardedExecutor,
          gold: ExecutionResult | None, phase: str,
          client: Any | None = None) -> dict[str, Any]:
    if arm not in ARMS:
        raise ValueError("未知组别")
    if arm == "A" and entries:
        raise ValueError("A组不得收到新增知识")
    client = client or MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
    started = time.perf_counter()
    norm = normalize(case["question"], time_anchor=ANCHOR)
    trace: list[dict[str, Any]] = [{"tool": "normalizer", "input": case["question"],
                                   "output": norm.model_dump(mode="json")},
                                  {"tool": "knowledge_input", "entry_ids": [e["id"] for e in entries]}]
    answer = {"action": "error", "sql": "", "clarification": ""}
    error: str | None = None
    output = ExecutionResult(error="未执行")
    attempts = 0
    for attempts in range(1, 4):
        system, user = make_prompts(norm.normalized, raw, entries, error, answer["sql"])
        try:
            response = client.chat(system, user)
            if client.calls:
                client.calls[-1]["response"] = response
        except Exception as exc:
            error = "模型请求失败：" + type(exc).__name__
            if client.calls:
                client.calls[-1]["error"] = type(exc).__name__
            trace.append({"tool": "model_error", "error": error})
            break
        try:
            answer = parse_answer(response)
        except (ValueError, KeyError, IndexError) as exc:
            error = "结构化输出错误：" + str(exc)
            trace.append({"tool": "format_check", "output": error})
            continue
        if answer["action"] == "clarify":
            error = None
            trace.append({"tool": "clarification", "output": answer["clarification"]})
            break
        output = executor.execute(answer["sql"])
        error = output.error
        trace.append({"tool": "validate_execute", "input": {"sql": answer["sql"]},
                      "output": {"error": error, "columns": output.columns, "rows": output.rows},
                      "elapsed_ms": output.elapsed_ms})
        if not error:
            break
    kind = case.get("kind", "sql")
    if kind == "clarify":
        verdict = clarification_verdict(case, answer)
    elif answer["action"] != "sql":
        verdict = {"kind": "fail", "ok": False, "note": "可回答题被澄清或调用失败"}
    else:
        assert gold is not None
        verdict = compare_resultsets(result_rows(gold), result_rows(output), pred_error=error,
                                    order_matters=gold_sql_has_order_by(case["gold_sql"])).to_dict()
    usage = {k: sum((c.get("usage") or {}).get(k, 0) or 0 for c in client.calls)
             for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    usage["cached_tokens"] = sum(((c.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0 for c in client.calls)
    return {"id": case["id"], "question": case["question"], "phase": phase, "kind": kind,
            "task": case.get("task"), "arm": arm, "repeat": repeat, **answer,
            "error": error, "attempts": attempts, "verdict": verdict,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "usage": usage, "usage_complete": all(all(isinstance((c.get("usage") or {}).get(k), int)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")) for c in client.calls),
            "trace": trace, "calls": client.calls,
            "diagnostics": sql_diagnostics(answer["sql"], raw, case)}


def check_cases(cases: list[dict[str, Any]], executor: GuardedExecutor) -> tuple[dict[str, ExecutionResult], list[dict[str, Any]]]:
    gold: dict[str, ExecutionResult] = {}
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for case in cases:
        if case["id"] in seen:
            raise ValueError("题号重复：" + case["id"])
        seen.add(case["id"])
        if case.get("kind", "sql") == "clarify":
            if not case.get("clarification_keywords"):
                raise ValueError("澄清题缺少评分关键组")
            checks.append({"id": case["id"], "kind": "clarify", "rubric_present": True})
            continue
        first, second = executor.execute(case["gold_sql"]), executor.execute(case["gold_sql"])
        if first.error or second.error or not first.rows:
            raise RuntimeError(f"标准查询失败或为空：{case['id']} {first.error or second.error}")
        if any(value is None for row in first.rows for value in row.values()):
            raise RuntimeError("标准结果有NULL，需明确口径后重新封存：" + case["id"])
        if not compare_resultsets(result_rows(first), result_rows(second),
                                  order_matters=gold_sql_has_order_by(case["gold_sql"])).ok:
            raise RuntimeError("标准结果两次不一致：" + case["id"])
        check: dict[str, Any] = {"id": case["id"], "rows": first.row_count, "stable_nonnull": True}
        if case.get("mutant_sql"):
            wrong = executor.execute(case["mutant_sql"])
            if wrong.error:
                raise RuntimeError("变异查询执行错误：" + case["id"] + " " + wrong.error)
            check["mutant_distinguishable"] = not compare_resultsets(result_rows(first), result_rows(wrong),
                order_matters=gold_sql_has_order_by(case["gold_sql"])).ok
            if not check["mutant_distinguishable"]:
                raise RuntimeError("变异口径不能区分：" + case["id"])
        gold[case["id"]] = first
        checks.append(check)
    return gold, checks


def load_records(output: Path) -> list[dict[str, Any]]:
    path = output / "records.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def score_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, list[bool]] = {}
    for row in rows:
        by_id.setdefault(row["id"], []).append(row["verdict"]["kind"] in PASS)
    lat = sorted(r["latency_ms"] for r in rows)
    return {"passed": sum(r["verdict"]["kind"] in PASS for r in rows), "total": len(rows),
            "unique_cases": len(by_id), "both_rounds_passed": sum(len(v) == 2 and all(v) for v in by_id.values()),
            "by_round": {str(rep): {"passed": sum(r["verdict"]["kind"] in PASS for r in rows if r["repeat"] == rep),
                "total": sum(r["repeat"] == rep for r in rows)} for rep in (1, 2)},
            "mean_tokens": round(statistics.mean(r["usage"]["total_tokens"] for r in rows), 1) if rows else None,
            "total_tokens": sum(r["usage"]["total_tokens"] for r in rows),
            "model_calls": sum(len(r["calls"]) for r in rows),
            "network_requests": sum(c.get("network_attempts", 0) for r in rows for c in r["calls"]),
            "p50_ms": statistics.median(lat) if lat else None,
            "p95_ms": lat[max(0, math.ceil(len(lat) * .95) - 1)] if lat else None,
            "unnecessary_clarifications": sum(r["kind"] == "sql" and r["action"] == "clarify" for r in rows),
            "usage_complete": all(r["usage_complete"] for r in rows)}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for phase in sorted({r["phase"] for r in records}):
        phase_rows = [r for r in records if r["phase"] == phase]
        groups: dict[str, Any] = {}
        for arm in ARMS:
            rows = [r for r in phase_rows if r["arm"] == arm]
            if rows:
                groups[arm] = {kind: score_group([r for r in rows if r["kind"] == kind])
                              for kind in ("sql", "clarify") if any(r["kind"] == kind for r in rows)}
                groups[arm]["tasks"] = {str(task): score_group([r for r in rows if r.get("task") == task])
                    for task in (1, 4, 5) if any(r.get("task") == task for r in rows)}
        paired: dict[str, Any] = {}
        for left, right in (("A", "B"), ("B", "C"), ("A", "C")):
            keyed = {(r["id"], r["repeat"], r["arm"]): r for r in phase_rows}
            pairs = []
            for key, row in keyed.items():
                if key[2] != left or (key[0], key[1], right) not in keyed:
                    continue
                other = keyed[(key[0], key[1], right)]
                a, b = row["verdict"]["kind"] in PASS, other["verdict"]["kind"] in PASS
                pairs.append({"id": key[0], "repeat": key[1], "kind": row["kind"],
                              "left_pass": a, "right_pass": b,
                              "change": "win" if b and not a else "loss" if a and not b else "tie"})
            if pairs:
                paired[left + "_to_" + right] = {"pairs": len(pairs),
                    "wins": sum(p["change"] == "win" for p in pairs),
                    "losses": sum(p["change"] == "loss" for p in pairs), "details": pairs}
        result[phase] = {"groups": groups, "paired": paired}
    return result


def crosscheck(cases: list[dict[str, Any]], records: list[dict[str, Any]],
               raw: dict[str, Any], executor: GuardedExecutor, output: Path, phase: str) -> None:
    sql_cases = [c for c in cases if c.get("kind", "sql") == "sql"]
    scores: dict[str, Any] = {}
    for arm in sorted({r["arm"] for r in records}):
        for rep in (1, 2):
            rows = [r for r in records if r["arm"] == arm and r["repeat"] == rep and r["kind"] == "sql"]
            path = output / f"predictions-{phase}-{arm}-r{rep}.jsonl"
            predictions = [{"id": r["id"], "sql": r["sql"],
                "error": r["error"] or ("可回答问题被澄清" if r["action"] != "sql" else None),
                "latency_ms": r["latency_ms"]} for r in rows]
            path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in predictions) + "\n", encoding="utf-8")
            score = run_nl2sql(sql_cases, TupleExecutor(executor), JsonlPredictor(path))
            expected = sum(r["verdict"]["kind"] in PASS for r in rows)
            if score["overall"]["passed"] != expected:
                raise RuntimeError("现有runner复核计分不一致")
            scores[f"{arm}-r{rep}"] = score
    write_json(output / f"{phase}_runner_crosscheck.json", scores)


def run_batch(cases: list[dict[str, Any]], arms: tuple[str, ...], raw: dict[str, Any],
              variants: dict[str, list[dict[str, Any]]], output: Path,
              executor: GuardedExecutor, phase: str) -> list[dict[str, Any]]:
    if any(r["phase"] == phase for r in load_records(output)):
        raise RuntimeError("该阶段已有记录，禁止隐藏重跑；请保留原运行并使用新目录")
    gold, checks = check_cases(cases, executor)
    write_json(output / f"{phase}_checks.json", checks)
    records = []
    for rep in (1, 2):
        def one(index: int, case: dict[str, Any]) -> list[dict[str, Any]]:
            order = arms if (index + rep) % 2 else tuple(reversed(arms))
            rows = []
            for arm in order:
                row = solve(case, arm, rep, raw, variants[arm], executor, gold.get(case["id"]), phase)
                rows.append(row)
                if any(c.get("error") for c in row["calls"]):
                    break
            return rows
        fatal = False
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(one, idx, case) for idx, case in enumerate(cases)]
            for future in as_completed(futures):
                if future.cancelled():
                    continue
                for record in future.result():
                    records.append(record)
                    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                    print(f"{phase} r{rep} {record['id']} {record['arm']} {record['action']} {record['verdict']['kind']}", flush=True)
                    if any(c.get("error") for c in record["calls"]):
                        fatal = True
                        for pending in futures:
                            pending.cancel()
        if fatal:
            # 仍收集正在执行的另一个线程，避免请求已发生却未进入用量账。
            raise RuntimeError("接口故障，已取消排队题目并保留所有已发请求；不算知识能力失败")
    crosscheck(cases, records, raw, executor, output, phase)
    write_json(output / "summary.json", summarize(load_records(output)))
    return records


def paths_to_freeze(args: argparse.Namespace) -> list[Path]:
    paths = [Path(__file__), ROOT / "eval/builder_runner_checks.py", args.cases, args.schema,
             args.draft, args.verified, ROOT / "eval/ontology_experiment.py",
             ROOT / "eval/semantic_layer_experiment.py", ROOT / "eval/runner.py", ROOT / "eval/metrics.py",
             ROOT / "app/rag/llm.py", ROOT / "app/rag/config.py", ROOT / "infra/schema_meta.json",
             ROOT / "infra/schema_meta.aliases.zh.json", ROOT / "infra/schema_meta.descriptions.zh.json",
             ROOT / "eval/cases/nl2sql.jsonl"] + list((ROOT / "app/nl2sql").glob("*.py"))
    optional = [ROOT / "docs/EvoOntology构建器独立验证实验设计_v0.1.md",
                ROOT / "eval/builder/case_audit_v0.1.json", ROOT / "eval/builder/case_audit.json"]
    paths.extend(p for p in optional if p.exists())
    return list(dict.fromkeys(p.resolve() for p in paths))


def preflight(args: argparse.Namespace, cases: list[dict[str, Any]], raw: dict[str, Any], executor: GuardedExecutor) -> None:
    output = args.run_dir
    if (output / "freeze.json").exists():
        raise RuntimeError("冻结文件已存在")
    output.mkdir(parents=True, exist_ok=True)
    if (output / "records.jsonl").exists():
        raise RuntimeError("输出目录已有评测记录")
    security_check(raw, executor)
    _, checks = check_cases(cases, executor)
    write_json(output / "preflight_checks.json", checks)
    frozen: dict[str, str] = {}
    for path in paths_to_freeze(args):
        rel = path.relative_to(ROOT).as_posix()
        digest = sha256(path)
        dest = output / "source_snapshot" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
        if sha256(dest) != digest:
            raise RuntimeError("文件在冻结过程中改变")
        frozen[rel] = digest
    client = MeasuredClient(max_tokens=16000, timeout=60, max_retries=2)
    raw_chinook = read(ROOT / "infra/schema_meta.json")
    write_json(output / "freeze.json", {"time": datetime.now(timezone.utc).isoformat(),
        "source_sha256": frozen, "inputs": {k: str(getattr(args, k).resolve()) for k in ("cases", "schema", "draft", "verified")},
        "data_aw": fingerprint(raw, aw_dsn()), "data_chinook": fingerprint(raw_chinook, None),
        "model": client.model, "temperature": 0, "sql_or_format_attempts": 3,
        "timeout": 60, "http_retries": 2, "workers": 2, "rounds": 2,
        "arm_order": "each case reversed on alternating index+round", "planner_calls": 0,
        "knowledge_prompt_fields": KNOWLEDGE_FIELDS,
        "claim_boundary": "半自动构建；同库新题泛化；不检验反馈演化或按需访问"})
    print("冻结完成：" + str(output), flush=True)


def verify_sources(output: Path) -> dict[str, Any]:
    frozen = read(output / "freeze.json")
    for rel, digest in frozen["source_sha256"].items():
        if sha256(ROOT / rel) != digest:
            raise RuntimeError("冻结后源文件改变：" + rel)
    return frozen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("preflight", "validation", "test", "regression", "report"), required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--verified", type=Path, required=True)
    args = parser.parse_args()
    load_env_file()
    raw = read(args.schema)
    cases_raw = read(args.cases)
    cases = [canonical_case(c) for c in (cases_raw["cases"] if isinstance(cases_raw, dict) else cases_raw)]
    variants = {"A": [], "B": read(args.draft)["entries"], "C": read(args.verified)["entries"]}
    executor = GuardedExecutor(raw, aw_dsn())
    if args.stage == "preflight":
        preflight(args, cases, raw, executor)
        return 0
    frozen = verify_sources(args.run_dir)
    for key in ("cases", "schema", "draft", "verified"):
        if str(getattr(args, key).resolve()) != frozen["inputs"][key]:
            raise RuntimeError("输入路径与冻结版本不一致")
    if args.stage in ("validation", "test"):
        chosen = [c for c in cases if c.get("split") == args.stage]
        if not chosen:
            raise RuntimeError("所选分区没有题目")
        run_batch(chosen, ARMS, raw, variants, args.run_dir, executor, args.stage)
    elif args.stage == "regression":
        regression = [{**c, "kind": "sql"} for c in load_cases(ROOT / "eval/cases/nl2sql.jsonl")]
        chinook = read(ROOT / "infra/schema_meta.json")
        # AdventureWorks知识不适用于Chinook；此回归仅验证共同提示适配的稳定性。
        run_batch(regression, ("A",), chinook, {"A": []}, args.run_dir,
                  GuardedExecutor(chinook, None), "regression")
    elif args.stage == "report":
        final_aw, final_chinook = fingerprint(raw, aw_dsn()), fingerprint(read(ROOT / "infra/schema_meta.json"), None)
        integrity = {"aw_unchanged": final_aw == frozen["data_aw"],
                     "chinook_unchanged": final_chinook == frozen["data_chinook"]}
        write_json(args.run_dir / "data_integrity.json", integrity)
        if not all(integrity.values()):
            raise RuntimeError("只读实验前后数据库摘要不同")
        summary = summarize(load_records(args.run_dir))
        write_json(args.run_dir / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
