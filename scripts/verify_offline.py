#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线自检：不联网、不调模型、不需要任何第三方包。

评审/复现者只需 Python 3.10+，在仓库根目录执行：

    python scripts/verify_offline.py

它会做三件事：
1. 读 `contrib/kag/feva_override/data/_records.json`（1,031 条结构化记录）
   与 `qbank_v4.json`（31 道冻结题）；
2. 逐题调用应用侧 `app.kag_ask.fact_table()`，统计哪些题能拆出事实表、各几行；
3. 与技术报告附录 E / 第五章的数字对照，打印 PASS / FAIL 并以退出码返回。

**这不是完整问答的复现**：完整问答要走 KAG 服务（部署在实验服务器上，地址不入库）。
这里复算的是「生成前事实判定」这一层——它不检索原文、不调用任何模型，因此结果确定：
同一份 `_records.json` 无论跑多少次、在哪台机器上跑，都是 31 题 / 19 出表 / 77 行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

EXPECT_QUESTIONS = 31
EXPECT_TABLES = 19
EXPECT_ROWS = 77


def main() -> int:
    from app.kag_ask import fact_table  # 只用标准库，无第三方依赖

    qbank = ROOT / "contrib" / "kag" / "feva_override" / "data" / "qbank_v4.json"
    records = ROOT / "contrib" / "kag" / "feva_override" / "data" / "_records.json"

    for p in (qbank, records):
        if not p.exists():
            print(f"[FAIL] 缺少数据文件：{p.relative_to(ROOT)}")
            return 1

    questions = json.loads(qbank.read_text(encoding="utf-8"))["questions"]
    rec = json.loads(records.read_text(encoding="utf-8"))
    n_records = rec["n"] if isinstance(rec, dict) and "n" in rec else len(rec.get("items", rec))

    print("=" * 62)
    print("循证问答 · 离线自检（无网络、无模型、无第三方包）")
    print("=" * 62)
    print(f"结构化记录 : {n_records} 条  (_records.json)")
    print(f"冻结题库   : {len(questions)} 题  (qbank_v4.json)")
    print("-" * 62)
    print(f"{'题号':<10}{'出表':<8}{'行数':<6}说明")
    print("-" * 62)

    tables = rows = 0
    for q in questions:
        qid = str(q.get("id") or q.get("qid") or "?")
        text = q.get("text") or q.get("question") or ""
        out = fact_table(text)["rows"]
        has = bool(out)
        if has:
            tables += 1
            rows += len(out)
        note = "" if has else "不可拆（指代/条款/字段语义类）"
        print(f"{qid:<10}{'是' if has else '否':<8}{len(out):<6}{note}")

    print("-" * 62)
    print(f"合计：{len(questions)} 题 / {tables} 出表 / {rows} 行")
    print()

    checks = [
        ("题数与题库一致", len(questions), EXPECT_QUESTIONS),
        ("出表题数", tables, EXPECT_TABLES),
        ("事实行数", rows, EXPECT_ROWS),
    ]
    ok = True
    for name, got, want in checks:
        flag = "PASS" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  [{flag}] {name}：实测 {got}，报告 {want}")

    print()
    if ok:
        print("结论：与技术报告附录 E / 第五章的数字一致。")
        print("注意：本自检只验证「可拆性与行数」，不检索原文、不涉及模型，")
        print("      因此不能证明 77 行都取得了在线证据——在线证据需连 KAG 服务另核。")
        return 0
    print("结论：与报告数字不一致，请检查数据文件是否被改动。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
