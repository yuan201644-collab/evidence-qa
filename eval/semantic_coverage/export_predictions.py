"""S2-H 导出：把语义覆盖采集适配为 eval/runner.py 的 JsonlPredictor 输入格式。

用途（S2 计划书 §10.4 / 母方案 :589-603）：
    python -m eval.semantic_coverage.export_predictions \
        --run-dir eval/reports/semantic_coverage/<运行号> --out predictions.jsonl
    python -m eval.runner.py --predictor jsonl --suite all --dialect postgres \
        --predictions predictions.jsonl

硬边界：
  - 只导出**真实采集**的记录；题号、最终 SQL、回答、错误、延迟、澄清状态原样保留；
  - 引用字段：`answer_refs` = 响应里的 `refs`（最终回答引用），`retrieved` 只在
    响应真实携带检索记录时填——**绝不把 answer_refs 复制成 retrieved**（否则
    `retrieval_is_approximated` 误判 False，把不可信分数伪装成真召回）；
  - **绝不用参考 SQL / Gold 填预测 SQL**——导出脚本根本不读 gold 文件；
  - 未运行的项写 `{"id": ..., "error": "未运行：<原因>"}`，**不填历史分数**；
  - 语义覆盖题面（SC-*）不在 runner.py 现有套件 id 空间内，导出时按 id 原样带出，
    由调用方决定喂给哪个 suite；匹配不上的题 runner 会自然跳过。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    raw_path = run_dir / "raw_responses.jsonl"
    if not raw_path.is_file():
        raise SystemExit(f"采集记录不存在：{raw_path}")
    out_path = Path(args.out)
    if out_path.exists():
        raise SystemExit(f"输出文件已存在：{out_path}。拒绝覆盖（§10.4）")

    rows: list[dict] = []
    for line in raw_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        scenario = json.loads(line)
        case_id = str(scenario.get("id"))
        for index, turn in enumerate(scenario.get("turns") or []):
            row_id = case_id if len(scenario.get("turns") or []) == 1 else f"{case_id}-t{index + 1}"
            status = turn.get("status")
            body = turn.get("body") or {}
            if status != 200:
                rows.append({
                    "id": row_id,
                    "error": f"未运行：HTTP {status}；{str(turn.get('error') or body.get('error'))[:120]}",
                })
                continue
            row: dict = {
                "id": row_id,
                "sql": body.get("sql"),
                "answer": body.get("answer"),
                "latency_ms": turn.get("elapsed_ms"),
                "clarified": bool(body.get("need_clarify")),
            }
            if body.get("error"):
                row["error"] = str(body["error"])
            # refs → answer_refs（最终回答引用）；retrieved 留空 = 检索未单独记录，
            # runner 会按 approximation 口径标注，绝不冒称真召回。
            refs = body.get("refs") or []
            row["answer_refs"] = [
                {"doc_id": r.get("doc_id"), "page": r.get("page")}
                for r in refs if isinstance(r, dict)
            ]
            rows.append(row)

    out_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"导出 {len(rows)} 条预测 → {out_path}")
    print("下一步：python -m eval.runner.py --predictor jsonl --suite all --dialect postgres "
          f"--predictions {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
