"""从冻结评测记录派生报告数据，不修改预测、评分或知识。"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

PASS = {"pass", "pass_loose"}


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def passed(row: dict[str, Any]) -> bool:
    return row["verdict"]["kind"] in PASS


def compare(rows: list[dict[str, Any]], left: str, right: str) -> dict[str, Any]:
    keyed = {(r["id"], r["repeat"], r["arm"]): r for r in rows}
    details = []
    for (case_id, repeat, arm), row in keyed.items():
        if arm != left or (case_id, repeat, right) not in keyed:
            continue
        other = keyed[(case_id, repeat, right)]
        before, after = passed(row), passed(other)
        details.append({"id": case_id, "repeat": repeat,
                        "change": "win" if after and not before else "loss" if before and not after else "tie",
                        "left_action": row["action"], "right_action": other["action"]})
    return {"wins": sum(d["change"] == "win" for d in details),
            "losses": sum(d["change"] == "loss" for d in details), "details": details}


def analyze(output: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in (output / "records.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    validations = [r for r in records if r["phase"] == "validation" and r["kind"] == "sql"]
    gates = {}
    for repeat in (1, 2):
        rows = [r for r in validations if r["repeat"] == repeat]
        comparison = compare(rows, "A", "C")
        a = sum(passed(r) for r in rows if r["arm"] == "A")
        c = sum(passed(r) for r in rows if r["arm"] == "C")
        gates[str(repeat)] = {"A_pass": a, "C_pass": c, "regressions": comparison["losses"],
                              "passed": c >= a and comparison["losses"] == 0}
    pairs = {}
    for phase in ("validation", "test"):
        for kind in ("sql", "clarify"):
            rows = [r for r in records if r["phase"] == phase and r["kind"] == kind]
            if rows:
                pairs[phase + "_" + kind] = {left + "_to_" + right: compare(rows, left, right)
                                             for left, right in (("A", "B"), ("B", "C"), ("A", "C"))}
    failures = [{k: r.get(k) for k in ("phase", "arm", "repeat", "id", "task", "kind", "question", "action", "sql", "clarification", "verdict", "error")}
                for r in records if not passed(r)]
    construction = {}
    for stage in ("draft", "grounded", "reviewed"):
        path = output / (stage + "_calls.json")
        if not path.exists():
            continue
        data = read(path)
        calls = data["calls"]
        construction[stage] = {"model_calls": len(calls), "network_attempts": sum(c.get("network_attempts", 0) for c in calls),
                               "elapsed_seconds": data["elapsed_seconds"],
                               "usage": {k: sum(c.get("usage", {}).get(k, 0) or 0 for c in calls)
                                         for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                               "cached_prompt_tokens": sum((c.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0 for c in calls)}
    review = {}
    for stage, filename in (("draft", "draft_independent_review.json"), ("grounded", "independent_review.json"), ("final", "final_review.json")):
        path = output / filename
        if path.exists():
            value = read(path)
            review[stage] = dict(Counter(e["decision"] for e in value["entries"]))
    challenges = read(output / "challenge_results.json")
    auto_issues = {r["challenge_id"]: r["audit"].get("issues", []) for r in challenges["records"]}
    artifacts = {p.name: digest(p) for p in output.iterdir()
                 if p.is_file() and p.name != "report_analysis.json" and p.suffix in {".json", ".jsonl", ".txt"}}
    strict_counts = {}
    for phase in ("validation", "test", "regression"):
        strict_counts[phase] = {arm: dict(Counter(r["verdict"]["kind"] for r in records
                                                if r["phase"] == phase and r["arm"] == arm and r["kind"] == "sql"))
                                for arm in ("A", "B", "C")}
    return {"validation_gate": {"by_round": gates, "passed": all(g["passed"] for g in gates.values())},
            "paired_separate_kinds": pairs, "failed_predictions": failures,
            "construction_usage": construction, "knowledge_review": review, "strict_sql_verdicts": strict_counts,
            "challenge_auto_issues": auto_issues, "artifact_sha256": artifacts,
            "score_policy": "派生统计；关键词澄清预评分保持原样，独立语义复核单列；两轮不是双倍独立样本。",
            "reviewer_identity": "AI subagent；无真人专家参与。verified_knowledge.human_reviewed是旧式字段名，不能据此声称真人审核。"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.run_dir.resolve(strict=True)
    data = analyze(output)
    (output / "report_analysis.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: data[k] for k in ("validation_gate", "construction_usage", "knowledge_review")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
