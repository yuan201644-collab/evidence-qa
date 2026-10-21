#!/usr/bin/env python
"""独立校验 acceptance_20261011 三文件：结构、分离、参考结果自洽。

对齐执行单 3.1 命名：`nl2sql_questions.jsonl` / `nl2sql_reference.jsonl` / `nl2sql_sources.md`。
与 build 脚本内置的自校互补：这里**重跑一遍 gold_sql**，逐条比对
`nl2sql_reference.jsonl` 里落盘的 `gold_result` 是否与真库当前结果一致
（防「改了题目忘了重跑参考」「参考被手改」两类漂移）。

用法：
    .venv/Scripts/python.exe eval/cases/acceptance_20261011/verify_acceptance_20261011.py
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
#: 题目文件只允许这三个字段（执行单 3.1：唯一编号、task、原始问题）。
ALLOWED_IN_QUESTIONS = {"id", "task", "question"}


def load(name: str) -> list[dict]:
    return [json.loads(l) for l in (OUT / name).read_text(encoding="utf-8").splitlines() if l.strip()]


def load_dsn() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("未在 .env 找到 DATABASE_URL")


def jsonable(v):
    return float(v) if isinstance(v, Decimal) else v


def main() -> int:
    problems: list[str] = []
    q = load("nl2sql_questions.jsonl")
    ref = load("nl2sql_reference.jsonl")
    src_md = (OUT / "nl2sql_sources.md").read_text(encoding="utf-8")

    qids = [r["id"] for r in q]
    rids = [r["id"] for r in ref]

    if len(qids) != len(set(qids)):
        problems.append("nl2sql_questions.jsonl: id 重复")
    if set(qids) != set(rids):
        problems.append(
            f"questions 与 reference 的 id 不一致：仅题 {set(qids)-set(rids)} / 仅参考 {set(rids)-set(qids)}"
        )

    # 分离性 + 字段最小化：题目文件只能有 id / task / question
    for r in q:
        extra = set(r) - ALLOWED_IN_QUESTIONS
        if extra:
            problems.append(f"{r['id']}: 题目文件混入越权字段 {sorted(extra)}")
        for field in ("id", "task", "question"):
            if r.get(field) in (None, "", []):
                problems.append(f"{r['id']}: 缺字段 {field}")

    # sources.md 必须覆盖全部 id
    missing_in_src = [i for i in qids if f"| {i} |" not in src_md]
    if missing_in_src:
        problems.append(f"nl2sql_sources.md 未覆盖 id：{missing_in_src}")

    # 参考结果自洽：重跑 gold_sql 比对
    dsn = load_dsn()
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for r in ref:
            cur.execute(r["gold_sql"])
            live = [[jsonable(v) for v in row] for row in cur.fetchall()]
            stored = r["gold_result"]
            if live != stored:
                problems.append(f"{r['id']}: 参考结果与真库不一致（stored {stored[:2]} vs live {live[:2]}）")
            if r["row_count"] != len(stored):
                problems.append(f"{r['id']}: row_count={r['row_count']} 与 gold_result 长度 {len(stored)} 不符")

    # 难度分布（从 sources.md 表格解析）
    dist: dict[str, int] = {}
    for m in re.finditer(r"^\|\s*(acc-\d+)\s*\|\s*(easy|medium|hard)\s*\|", src_md, re.M):
        dist[m.group(2)] = dist.get(m.group(2), 0) + 1

    print(f"题量 {len(q)} 条  难度分布 {dist}")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  -", p)
        return 1
    print("三文件结构 / 分离 / 参考结果自洽 —— 全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
