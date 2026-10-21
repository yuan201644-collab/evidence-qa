"""汇总三轮结构化规模实测；每轮先按档位计算统计量，再跨轮取中位数和范围。"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIERS = ("sales_scale_1000", "sales_scale_10000", "sales_scale_100000")
METRICS = ("p50", "p95", "max")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(score_paths: list[Path], index_path: Path) -> dict[str, Any]:
    if len(score_paths) != 3:
        raise ValueError("本汇总要求恰好三轮评分文件")
    scores = [load_json(path) for path in score_paths]
    if len({score.get("question_sha256") for score in scores}) != 1:
        raise ValueError("三轮问题包哈希不一致")
    if len({score.get("manifest_sha256") for score in scores}) != 1:
        raise ValueError("三轮数据清单哈希不一致")
    if not all(score.get("data_fingerprint", {}).get("verified") for score in scores):
        raise ValueError("至少一轮数据指纹未通过")

    tier_summary: dict[str, Any] = {}
    for tier in TIERS:
        per_round = []
        for score in scores:
            metrics = score["latency_summary_by_table"][tier]["successful_latency_ms"]
            per_round.append({name: metrics[name] for name in METRICS})
        cross_round: dict[str, Any] = {}
        for name in METRICS:
            values = [float(row[name]) for row in per_round if row[name] is not None]
            cross_round[name] = {
                "median_of_round_statistics_ms": round(statistics.median(values), 2) if values else None,
                "round_range_ms": [round(min(values), 2), round(max(values), 2)] if values else None,
                "per_round_ms": values,
            }
        case_rows = [
            row for score in scores for row in score["cases"] if row["table"].split(".", 1)[1] == tier
        ]
        tier_summary[tier] = {
            "attempted": len(case_rows),
            "sql_executed": sum(bool(row["sql_executed"]) for row in case_rows),
            "strict_matches": sum(bool(row["exact_result_match"]) for row in case_rows),
            "semantic_matches": sum(bool(row["semantic_result_match"]) for row in case_rows),
            "latency_success_counts_per_round": [score["latency_summary_by_table"][tier]["successful_latency_count"] for score in scores],
            "end_to_end_latency_ms": cross_round,
        }

    index_data = load_json(index_path)
    index_summary = {}
    for tier in TIERS:
        observations = [item for item in index_data["observations"] if item["table"].endswith(tier)]
        values = [float(item["index_build_ms"]) for item in observations]
        index_summary[tier] = {
            "round_count": len(values),
            "median_build_ms": round(statistics.median(values), 2) if values else None,
            "range_ms": [round(min(values), 2), round(max(values), 2)] if values else None,
            "per_round_ms": values,
        }

    all_cases = [row for score in scores for row in score["cases"]]
    failures = [
        {"round": index + 1, "id": row["id"], "http_status": row["http_status"], "sql_executed": row["sql_executed"], "error": row["error"]}
        for index, score in enumerate(scores)
        for row in score["cases"]
        if not row["sql_executed"] or row["request_error"] is not None or row["http_status"] != 200
    ]
    return {
        "schema": "structured_scale_three_round_summary_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rounds": [score["run_id"] for score in scores],
        "question_sha256": scores[0]["question_sha256"],
        "data_manifest_sha256": scores[0]["manifest_sha256"],
        "database": scores[0]["database"],
        "data_fingerprints_verified_all_rounds": all(score["data_fingerprint"]["verified"] for score in scores),
        "total_attempted": len(all_cases),
        "http_200": sum(row["http_status"] == 200 for row in all_cases),
        "sql_executed": sum(bool(row["sql_executed"]) for row in all_cases),
        "strict_matches": sum(bool(row["exact_result_match"]) for row in all_cases),
        "semantic_matches": sum(bool(row["semantic_result_match"]) for row in all_cases),
        "index_build": {
            "method": index_data["method"],
            "not_measured": index_data["not_measured"],
            "by_tier": index_summary,
        },
        "end_to_end_by_tier": tier_summary,
        "failed_or_unexecuted_cases": failures,
        "rounds_detail": [
            {
                "run_id": score["run_id"],
                "score_file": str(score_path),
                "http_200": sum(row["http_status"] == 200 for row in score["cases"]),
                "sql_executed": score["executed_sql_count"],
                "strict_matches": score["exact_matches"],
                "semantic_matches": score["semantic_matches"],
                "score_sha256": hashlib.sha256(score_path.read_bytes()).hexdigest(),
            }
            for score_path, score in zip(score_paths, scores, strict=True)
        ],
        "interpretation_limits": [
            "合成数据效率观察，不是业务质量或真实业务效果评估。",
            "端到端时延为同一 Windows 机器、同一模型与服务设置下的 /api/ask 实测；重启 API 不清空 PostgreSQL 或操作系统缓存。",
            "P95 每轮每档只有 10 个样本，长尾估计不稳定。",
            "严格匹配与语义匹配分开报告；语义匹配仅将 Q08 月份键统一成 YYYY-MM，避免把日期和月份文本的等价格式差异计为语义错误。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round1", type=Path, required=True)
    parser.add_argument("--round2", type=Path, required=True)
    parser.add_argument("--round3", type=Path, required=True)
    parser.add_argument("--index-build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有三轮汇总：{output}")
    result = summarize(
        [args.round1.resolve() / "structured_scale_score.json", args.round2.resolve() / "structured_scale_score.json", args.round3.resolve() / "structured_scale_score.json"],
        args.index_build.resolve(),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"三轮汇总完成：HTTP 200 {result['http_200']}/{result['total_attempted']}，SQL 执行 {result['sql_executed']}/{result['total_attempted']}，语义匹配 {result['semantic_matches']}/{result['total_attempted']}，文件 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
