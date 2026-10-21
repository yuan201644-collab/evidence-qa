#!/usr/bin/env python
"""A/B 跨源配对入口：复原参考基线 A 对比在线阶段 C 候选 B。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from cross_source_reference_baseline import StageBReferenceBaseline  # noqa: E402
from cross_source_runner import REPORTS, SUITE, ask, compare, read_jsonl  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def experiment_labels(purpose: str) -> tuple[str, str]:
    """Return report labels without implying independent review has passed."""
    labels = {
        "exploratory": ("exploratory_trial", "pending_independent_review"),
        "regression": ("regression_after_review", "previously_exposed_not_unseen"),
        "independent_holdout": ("independent_holdout_trial", "pending_independent_review"),
    }
    try:
        return labels[purpose]
    except KeyError as exc:
        raise ValueError(f"unknown experiment purpose: {purpose}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--questions", type=Path, default=SUITE / "questions.jsonl")
    parser.add_argument("--gold", type=Path, default=SUITE / "gold.jsonl")
    parser.add_argument("--output-dir", type=Path, default=REPORTS)
    parser.add_argument(
        "--purpose",
        choices=("exploratory", "regression", "independent_holdout"),
        default="exploratory",
        help="independent_holdout 仅可用于已由独立成员冻结、且运行者未接触答案的新题集",
    )
    parser.add_argument("--baseline-review-status", choices=("pending_team_review", "confirmed_operational_assumptions"),
                        default="pending_team_review")
    args = parser.parse_args()
    questions = read_jsonl(args.questions)
    if not questions:
        raise SystemExit("questions 必须非空")

    baseline = StageBReferenceBaseline()
    docs_dir = Path(os.environ.get("CROSS_SOURCE_DEMO_DOCS_DIR", ROOT / "data" / "cross_source_demo" / "documents"))
    experiment_root = args.questions.resolve().parent
    gold_path = args.gold.resolve()
    sources = {args.questions.resolve(),
               ROOT / "app" / "tools" / "cross_source_demo.py",
               ROOT / "eval" / "cross_source_reference_baseline.py",
               ROOT / "eval" / "cross_source_pair_runner.py",
               ROOT / "eval" / "cross_source_runner.py"}
    # Hash the supplied dataset package, not the runner's historical default suite.
    sources.update(path.resolve() for path in experiment_root.rglob("*")
                   if path.is_file() and path.resolve() != gold_path)
    sources.update(path.resolve() for path in docs_dir.glob("*.md"))
    sources.add(Path(__file__).resolve().parent / "cross_source_heldout" / "tools" / "load_heldout.py")
    def path_label(path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)
    hashes_before = {path_label(path): sha256_file(path) for path in sorted(sources)}
    # Run both arms from questions alone. Load gold only after every raw response
    # has been collected, so no answer key participates in execution or prompting.
    raw_pairs: list[dict[str, Any]] = []
    for index, case in enumerate(questions):
        arms: dict[str, Any] = {}
        # 任一组失败仍运行另一组，保存双方真实结果；不静默剔除错误题。
        for arm in (("A", "B") if index % 2 == 0 else ("B", "A")):
            try:
                if arm == "A":
                    raw = baseline.run(case["question"])
                    http_elapsed = None
                else:
                    raw, http_elapsed = ask(args.endpoint, case["case_id"], case["question"], args.timeout)
                arms[arm] = {"raw": raw, "http_round_trip_ms": round(http_elapsed, 2) if http_elapsed is not None else None}
            except Exception as exc:
                arms[arm] = {"error_type": type(exc).__name__}
        raw_pairs.append({"case": case, "arms": arms})

    gold_by_id = {item["case_id"]: item for item in read_jsonl(args.gold)}
    if {item["case_id"] for item in questions} != set(gold_by_id):
        raise SystemExit("questions 与 gold 的 case_id 必须一一对应")

    paired: list[dict[str, Any]] = []
    for raw_pair in raw_pairs:
        case = raw_pair["case"]
        arms = raw_pair["arms"]
        gold = gold_by_id[case["case_id"]]
        for arm_name in ("A", "B"):
            arm_result = arms[arm_name]
            if "error_type" in arm_result:
                arm_result["automatic_checks_pass"] = False
                continue
            raw = arm_result.pop("raw")
            elapsed = sum(float(step.get("elapsed_ms", 0.0)) for step in raw.get("trace", []))
            compared = compare(case, gold, raw, elapsed)
            compared["http_round_trip_ms"] = arm_result.pop("http_round_trip_ms")
            arms[arm_name] = compared
        a, b = arms["A"], arms["B"]
        if "error_type" in a or "error_type" in b:
            transition = "run_error"
        elif a["automatic_checks_pass"] and b["automatic_checks_pass"]:
            transition = "both_correct"
        elif a["automatic_checks_pass"]:
            transition = "only_a_correct"
        elif b["automatic_checks_pass"]:
            transition = "only_b_correct"
        else:
            transition = "both_wrong"
        paired.append({"case_id": case["case_id"], "transition": transition, **arms})

    counts = {key: sum(item.get("transition") == key for item in paired) for key in ("both_correct", "only_a_correct", "only_b_correct", "both_wrong", "run_error")}
    hashes_after = {path_label(path): sha256_file(path) for path in sorted(sources)}
    report_hashes = dict(hashes_before)
    report_hashes[path_label(gold_path)] = sha256_file(gold_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    experiment_status, dataset_review_status = experiment_labels(args.purpose)
    if args.purpose == "independent_holdout":
        warning = (
            "该运行被操作者标记为独立留出候选；需核验独立编制、冻结清单、代码哈希及运行者在原始回答生成前未接触 gold。"
            "自动比对不替代答案语义和证据充分性的人工盲审。A 是按已接受的合成演示操作假设复原的算法，"
            "不是历史源码或行业基线。"
        )
    elif args.purpose == "regression":
        warning = (
            "本数据集对当前操作者已暴露，只能作为探索性回归；自动比对不替代答案语义和证据充分性的人工复核。"
            "A 是按已接受的合成演示操作假设复原的算法，不是历史源码或行业基线。"
        )
    else:
        warning = (
            "自动比对不替代答案语义和证据充分性的人工复核。A 是按已接受的合成演示操作假设复原的算法，"
            "不是历史源码或行业基线。"
        )
    report = {
        "created_at_utc": stamp,
        "experiment_status": experiment_status,
        "dataset_review_status": dataset_review_status,
        "baseline_review_status": args.baseline_review_status,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "baseline_label": "按阶段 B 记录复原的参考基线（不是历史阶段 B 源码）",
        "candidate_label": "当前阶段 C 候选",
        "endpoint": args.endpoint,
        "case_count": len(paired),
        "transitions": counts,
        "warning": warning,
        "sha256": report_hashes,
        "source_snapshot_unchanged": hashes_before == hashes_after,
        "results": paired,
    }
    output_path = args.output_dir / f"cross-source-paired-{stamp}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"配对报告：{output_path}")
    print("题级转移：" + json.dumps(counts, ensure_ascii=False))
    return 0 if counts["run_error"] == 0 and report["source_snapshot_unchanged"] and all(item["B"]["automatic_checks_pass"] for item in paired) else 1


if __name__ == "__main__":
    sys.exit(main())
