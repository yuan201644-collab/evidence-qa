"""明确的实验资源映射：校准后重复验证，保留原实验代码和原始成绩。"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ontology_experiment as e

original_read = e.read
original_sources = e.source_paths
original_initialize = e.initialize
original_checks = e.check_cases
original_batch = e.run_batch
ASSETS = e.ASSETS
PREVIOUS = e.ROOT / "eval/reports/run-ontology-20261002T172819Z"
MAPPING = {e.ROOT / "infra/schema_meta.adventureworks.json": ASSETS / "schema_profile_v0.1.json",
           ASSETS / "cases_v0.2.json": ASSETS / "cases_v0.3.json",
           ASSETS / "knowledge_v0.1.json": ASSETS / "knowledge_v0.2.json"}


def read(path: Path) -> Any:
    return original_read(MAPPING.get(path, path))


def sources() -> list[Path]:
    return original_sources() + list(MAPPING.values()) + [Path(__file__), ASSETS / "calibrate_data.py",
            ASSETS / "data_grounding_evidence_v0.1.json", ASSETS / "gold_calibration_log_v0.1.json",
            e.ROOT / "docs/EvoOntology数据校准与重复验证设计_v0.1.md"]


def checks(cases: list[dict[str, Any]], executor: e.GuardedExecutor) -> Any:
    gold, items = original_checks(cases, executor)
    for cid, result in gold.items():
        if any(v is None for row in result.rows for v in row.values()):
            raise RuntimeError("标准结果含NULL，不能作为有效金额匹配证据：" + cid)
    return gold, items


def initialize() -> Path:
    output = original_initialize()
    e.write_json(output / "calibration_manifest.json", {"resources": {str(k.relative_to(e.ROOT)): str(v.relative_to(e.ROOT)) for k,v in MAPPING.items()},
                  "previous_run": str(PREVIOUS.relative_to(e.ROOT)), "is_unseen_test": False,
                  "data_grounding": original_read(ASSETS / "data_grounding_evidence_v0.1.json")})
    return output


def batch(cases: list[dict[str, Any]], arms: list[str], raw: dict[str, Any],
          variants: dict[str, list[dict[str, Any]]], output: Path, phase: str,
          executor: e.GuardedExecutor, repeats: int = 2) -> list[dict[str, Any]]:
    if phase != "regression":
        return original_batch(cases, arms, raw, variants, output, phase, executor, repeats)
    # 本轮只改AW资源。已完成的Chinook同代码、同数据回归不再重复消耗模型。
    old = original_read(PREVIOUS / "freeze.json")
    for name,digest in old["source_sha256"].items():
        if name.startswith("app/") or name in {"eval/ontology_experiment.py", "eval/semantic_layer_experiment.py", "eval/cases/nl2sql.jsonl", "eval/metrics.py", "eval/runner.py", "infra/schema_meta.json", "infra/schema_meta.aliases.zh.json", "infra/schema_meta.descriptions.zh.json", "eval/semantic_layer/rules_v0.1.json"}:
            if e.sha256(e.ROOT / name) != digest:
                print("回归源代码有变，不引用旧成绩，运行新的真实回归。",flush=True)
                return original_batch(cases, arms, raw, variants, output, phase, executor, repeats)
    old_integrity = original_read(PREVIOUS / "regression_integrity.json")
    if not old_integrity["unchanged"]:
        raise RuntimeError("旧回归的数据摘要不一致")
    rows = [r for r in e.load_records(PREVIOUS) if r["phase"] == "regression"]
    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
        import json
        for r in rows:
            r["reused_from"] = str(PREVIOUS.relative_to(e.ROOT))
            stream.write(json.dumps(r, ensure_ascii=False) + "\n")
    e.write_json(output / "regression_reuse.json", {"source": str(PREVIOUS.relative_to(e.ROOT)), "records": len(rows), "reason": "本轮仅改AW资产，原Chinook代码未改；引用而非新模型请求"})
    print("引用已完成Chinook回归：" + str(len(rows)) + "次，明确标记来源",flush=True)
    return rows


e.read = read
e.source_paths = sources
e.check_cases = checks
e.initialize = initialize
e.run_batch = batch

if __name__ == "__main__":
    try:
        raise SystemExit(e.main())
    except (OSError, ValueError, RuntimeError) as exc:
        print("校准实验停止：" + str(exc),file=sys.stderr)
        raise SystemExit(1)
