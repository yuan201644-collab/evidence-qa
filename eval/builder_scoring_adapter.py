"""仅给既有评分器补统计标签，不更改已冻结的求解流程、题目或知识。"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import builder_benchmark_runner as benchmark

ORIGINAL_SCORE = benchmark.run_nl2sql


def score_with_metadata(cases: list[dict[str, Any]], executor: Any, predictor: Any) -> dict[str, Any]:
    copies = []
    for case in cases:
        item = dict(case)
        item.setdefault("difficulty", "not_labeled")
        item.setdefault("scope", "not_labeled")
        copies.append(item)
    return ORIGINAL_SCORE(copies, executor, predictor)


def get_arg(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


def lock_adapter(output: Path) -> None:
    path = Path(__file__).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lock = output / "scoring_adapter_lock.json"
    if lock.exists():
        if benchmark.read(lock)["sha256"] != digest:
            raise RuntimeError("评分适配器冻结后改变")
        return
    snapshot = output / "scoring_adapter_snapshot.py"
    snapshot.write_bytes(path.read_bytes())
    existing = benchmark.load_records(output)
    benchmark.write_json(lock, {"time": datetime.now(timezone.utc).isoformat(),
        "path": str(path), "sha256": digest, "snapshot": str(snapshot.resolve()),
        "reason": "首轮validation全部48次求解完成后，既有run_nl2sql要求difficulty/scope统计标签触发KeyError",
        "records_preserved": len(existing), "existing_model_calls": sum(len(r["calls"]) for r in existing),
        "change_scope": "仅评分时传入case副本补统计标签，未改题目/标准SQL/求解提示/源码/知识",
        "gold_sent_to_solver": False, "no_model_calls_repeated": True,
        "original_freeze_verified": True})


def offline_checks() -> None:
    original = ORIGINAL_SCORE
    case = {"id": "check", "question": "fixed", "gold_sql": "SELECT 1"}
    original_copy = dict(case)
    def fake(cases: list[dict[str, Any]], executor: Any, predictor: Any) -> dict[str, Any]:
        assert cases[0]["difficulty"] == "not_labeled"
        assert cases[0]["scope"] == "not_labeled"
        assert cases[0]["question"] == "fixed" and cases[0]["gold_sql"] == "SELECT 1"
        return {"ok": True}
    globals()["ORIGINAL_SCORE"] = fake
    try:
        assert score_with_metadata([case], None, None)["ok"]
        assert case == original_copy
    finally:
        globals()["ORIGINAL_SCORE"] = original


def main() -> int:
    offline_checks()
    benchmark.run_nl2sql = score_with_metadata
    benchmark.load_env_file()
    output = Path(get_arg("--run-dir"))
    benchmark.verify_sources(output)
    lock_adapter(output)
    if get_arg("--stage") == "validation-recheck":
        raw = benchmark.read(Path(get_arg("--schema")))
        source = benchmark.read(Path(get_arg("--cases")))
        cases = [benchmark.canonical_case(c) for c in source["cases"] if c["split"] == "validation"]
        rows = [r for r in benchmark.load_records(output) if r["phase"] == "validation"]
        if len(rows) != len(cases) * 3 * 2:
            raise RuntimeError("验证记录不完整")
        benchmark.crosscheck(cases, rows, raw, benchmark.GuardedExecutor(raw, benchmark.aw_dsn()), output, "validation")
        benchmark.write_json(output / "summary.json", benchmark.summarize(benchmark.load_records(output)))
        print("验证评分复核完成；未重复任何模型调用。", flush=True)
        return 0
    return benchmark.main()


if __name__ == "__main__":
    raise SystemExit(main())
