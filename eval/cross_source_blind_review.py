#!/usr/bin/env python
"""从配对报告生成去组别审阅稿，并将 A/B 对照键单独保存。

只把 Markdown 审阅稿交给评审者；同目录的 key 文件仅由实验整理者保管。
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "eval" / "reports" / "cross_source"


def latest_report(directory: Path) -> Path:
    reports = sorted(directory.glob("cross-source-paired-*.json"), key=lambda path: path.stat().st_mtime)
    if not reports:
        raise FileNotFoundError(f"没有找到配对报告：{directory}")
    return reports[-1]


def render_ref(ref: dict[str, Any]) -> str:
    kind = ref.get("kind", "source")
    if kind == "sql":
        location = " / ".join(str(ref.get(field)) for field in ("table", "column", "row_pk") if ref.get(field))
    else:
        location = " / ".join(str(ref.get(field)) for field in ("doc_title", "doc_id", "page", "section_path") if ref.get(field))
    snippet = str(ref.get("snippet", "")).replace("\r", " ").replace("\n", " ").strip()
    return f"- 来源类型：{kind}；定位：{location or '未提供'}\n  证据片段：{snippet or '（无片段）'}"


def build_packet(report: dict[str, Any], seed: int | None = None) -> tuple[str, dict[str, Any]]:
    rng = random.SystemRandom() if seed is None else random.Random(seed)
    lines = [
        "# 跨源问答盲审表",
        "",
        "本表隐藏了各回答所属方案。请独立判断，不要查询配对密钥或标准答案。每个候选均展示系统回答及本次返回的来源证据；若证据不足，请在备注中指出缺少什么。",
        "",
        "判分建议：正确性填写“正确 / 部分正确 / 错误 / 无法判断”；证据填写“充分 / 部分充分 / 不充分 / 引用不匹配”。",
    ]
    if report.get("experiment_status") == "regression_after_review":
        lines[0] = "# 跨源问答匿名回归复核表"
        lines.extend(["", "本题集已参与开发。本表仅复核修正后的回答与证据，不是新的未见题盲测，不能据此证明泛化提升。"])
    key_rows: list[dict[str, str]] = []
    response_number = 1
    for result in report.get("results", []):
        if not isinstance(result.get("A"), dict) or not isinstance(result.get("B"), dict):
            continue
        candidates = [("A", result["A"]), ("B", result["B"])]
        rng.shuffle(candidates)
        lines.extend(["", "---", "", f"## 问题 {result['case_id']}", "", f"{result.get('A', {}).get('question', '')}"])
        for arm, output in candidates:
            response_id = f"R{response_number:03d}"
            response_number += 1
            key_rows.append({"response_id": response_id, "case_id": result["case_id"], "arm": arm})
            lines.extend(["", f"### 候选回答 {response_id}", "", "回答：", "", str(output.get("answer", "（无回答）")), "", "来源证据："])
            refs = output.get("refs", [])
            lines.extend(render_ref(ref) for ref in refs)
            if not refs:
                lines.append("- （系统未返回引用）")
            lines.extend([
                "",
                "评审：正确性（正确 / 部分正确 / 错误 / 无法判断）：__________",
                "",
                "评审：证据（充分 / 部分充分 / 不充分 / 引用不匹配）：__________",
                "",
                "备注：____________________________________________________________",
            ])
    packet = "\n".join(lines) + "\n"
    key = {
        "warning": "仅实验整理者保管；不要与盲审表一并发给评审者。",
        "source_report": report.get("created_at_utc"),
        "mapping": key_rows,
    }
    return packet, key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="指定配对 JSON；缺省取目录中最新一份")
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    parser.add_argument("--seed", type=int, help="仅用于可复现的内部检查；正式盲审建议不设置")
    args = parser.parse_args()
    report_path = args.report or latest_report(args.output_dir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    packet, key = build_packet(report, seed=args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    packet_path = args.output_dir / f"cross-source-blind-review-{stamp}.md"
    key_path = args.output_dir / f"DO_NOT_USE_cross-source-blind-key-{stamp}.json"
    packet_path.write_text(packet, encoding="utf-8")
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"盲审表：{packet_path}")
    print(f"对照密钥（仅整理者保管）：{key_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
