#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""F 项实测：证据来源在账本层被折叠时，投影层是否仍报 complete。

A 10-15 确认「结论有完整、可追溯的证据支持」是项目核心能力（非可选项），
并要求：证据去重发生来源碰撞时，系统不得仍标记为 complete。

本脚本只读验证三件事（不改任何实现/契约/判据）：
  Q1 `_locator` 与 `evidence_key` 是否同源？（决定「不同来源同定位」是否可能）
  Q2 `EvidenceLedger.record_fact` 是否校验 evidence_ids 与 evidence_refs 一一对应？
  Q3 当账本记了 N 条来源、refs 只有 M<N 条时，`_map_evidence` 返回什么状态？

隔离探测：不 import app 私有符号之外的副作用，全部在内存中构造。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.contracts import DocRef, SqlRef  # noqa: E402
from app.evidence_ledger import (  # noqa: E402
    EvidenceLedger,
    FactRecord,
    FactTask,
    evidence_key,
)
from app.fact_review import _locator, _map_evidence  # noqa: E402


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def q1_locator_vs_key() -> None:
    section("Q1  _locator 与 evidence_key 是否同源？")
    doc_char = DocRef(doc_id="d1", page=3, char_start=100, char_end=200,
                      snippet="原文片段")
    doc_page = DocRef(doc_id="d1", page=3, char_start=None, char_end=None,
                      section_path="第三章", snippet="章节片段")
    other = DocRef(doc_id="d2", page=3, char_start=100, char_end=200,
                   snippet="另一份文档同偏移")
    same_key_diff_snippet = DocRef(doc_id="d1", page=3, char_start=100,
                                   char_end=200, snippet="更长的原文片段内容")
    sql = SqlRef(table="customer", column="customer_id", row_pk="1", snippet="x")

    for label, ref in [
        ("DocRef(有 char 偏移)", doc_char),
        ("DocRef(无 char，有 page+section)", doc_page),
        ("DocRef(不同 doc_id，同偏移)", other),
        ("DocRef(同 doc 同偏移，snippet 不同)", same_key_diff_snippet),
        ("SqlRef", sql),
    ]:
        print(f"  {label}")
        print(f"    _locator    = {_locator(ref)}")
        print(f"    evidence_key= {evidence_key(ref)}")

    print("\n  → DocRef(不同 doc_id，同偏移)：locator 不同 → 不会互相折叠")
    print("  → DocRef(同 doc 同偏移)：locator 与 evidence_key 同时相同"
          " → 属同一定位，属设计内去重")


def q2_record_fact_alignment() -> None:
    section("Q2  record_fact 是否校验 evidence_ids 与 evidence_refs 一一对应？")
    ledger = EvidenceLedger()
    ledger.add_task(FactTask(task_id="t1", target_fact="f1"))
    ref_a = SqlRef(table="customer", column="customer_id", row_pk="1", snippet="A")
    ref_b = SqlRef(table="invoice", column="invoice_id", row_pk="7", snippet="B")
    k_a = ledger.add_candidate(ref_a)
    k_b = ledger.add_candidate(ref_b)
    print(f"  候选键：k_a={k_a}  k_b={k_b}")

    # 账本声称有两条来源，但只挂一条 ref 对象
    mismatched = FactRecord(
        fact_id="f1", task_id="t1", value="x", state="supported",
        evidence_ids=(k_a, k_b), evidence_refs=(ref_a,),
    )
    try:
        ledger.record_fact(mismatched)
        print("  构造 evidence_ids=(k_a,k_b) / evidence_refs=(ref_a,) →"
              " **record_fact 接受**")
    except Exception as exc:  # noqa: BLE001
        print(f"  record_fact 拒绝：{type(exc).__name__}: {exc}")

    matched = FactRecord(
        fact_id="f2", task_id="t1", value="x", state="supported",
        evidence_ids=(k_a,), evidence_refs=(ref_a,),
    )
    try:
        ledger.record_fact(matched)
        print("  构造一一对应的情况 → record_fact 接受（基线正常）")
    except Exception as exc:  # noqa: BLE001
        print(f"  一一对应也被拒绝（异常）：{type(exc).__name__}: {exc}")


def q3_projection_status() -> None:
    section("Q3  账本记 N 条来源、refs 只挂 M<N 条时，投影报什么？")
    ref_a = SqlRef(table="customer", column="customer_id", row_pk="1", snippet="A")
    ref_b = SqlRef(table="invoice", column="invoice_id", row_pk="7", snippet="B")

    indices, status = _map_evidence((ref_a,), [ref_a, ref_b])
    print(f"  refs 只挂 1 条、最终引用含 2 条 → status={status!r} indices={indices}")
    print("    ↑ 投影层只看得到挂上来的 ref，**看不到账本里还有第 2 条来源**")

    indices2, status2 = _map_evidence((ref_a, ref_a), [ref_a])
    print(f"  同一 ref 重复两次（设计内去重）→ status={status2!r} indices={indices2}")

    indices3, status3 = _map_evidence((ref_a, ref_b), [ref_a])
    print(f"  挂 2 条但最终引用只含第 1 条 → status={status3!r} indices={indices3}")
    print("    ↑ 这一路是保守的：多出的来源会落成 partial")


def main() -> int:
    q1_locator_vs_key()
    q2_record_fact_alignment()
    q3_projection_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())