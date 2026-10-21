"""诊断 14 道被澄清器误伤的 RAG 题：逐题给出走到哪条分支、为什么。

诊断脚本，不是评测脚本。目的是把「误伤」从印象变成可复核的归因表：
每道题分别列出
  ① clarifier.decide() 的 reason_code（哪条分支拦下的）
  ② query_requirements 清单里各 kind 的命中情况
  ③ 该题是否命中 document_scope（决定 :280 那个豁免能不能救它）
  ④ normalizer 的 unresolved 内容（决定 :215 那条歧义分支）

用法：
    D:\\python\\python.exe -X utf8 eval\\diagnose_clarifier_blocks.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES_DIR = ROOT / "eval" / "cases"

#: 首轮采集里 refs=0（未触检索）的题，即被澄清器拦下的题。
BLOCKED = [
    "rag-e-04", "rag-m-03", "rag-m-05", "rag-h-02",
    "rag-m-08", "rag-m-09", "rag-m-10", "rag-m-11",
    "rag-mh-07", "rag-mh-08", "rag-mh-09", "rag-mh-12",
    "rag-mh-16", "rag-mh-17",
]


def load_cases() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ("rag.jsonl", "rag_multihop.jsonl"):
        for line in (CASES_DIR / name).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            case = json.loads(line)
            out[case["id"]] = case
    return out


def main() -> int:
    from app.clarifier import SemanticClarifier
    from app.nl2sql.normalizer import normalize
    from app.nl2sql.config import time_anchor_from_env
    from app.query_requirements import extract_requirements

    cases = load_cases()
    clarifier = SemanticClarifier()

    rows = []
    print("=" * 100)
    for cid in BLOCKED:
        case = cases.get(cid)
        if case is None:
            print(f"{cid}: **题集里找不到**")
            continue
        question = case["question"]
        norm = normalize(question, time_anchor=time_anchor_from_env())
        # 澄清器 decide 里对 normalized=None 的兜底用的是 question 原话；
        # 这里传真实 normalize 结果，与编排链路一致。
        clist = extract_requirements(norm.original, norm)
        decision = clarifier.decide(question, norm, clist)

        kinds: dict[str, list[str]] = {}
        for item in clist.requirements:
            kinds.setdefault(item.kind, []).append(
                item.surface + ("(未解析)" if item.unresolved else "")
            )

        has_doc_scope = bool(clist.of_kind("document_scope"))
        unresolved_norm = tuple(norm.unresolved or ())

        rows.append({
            "id": cid,
            "reason": decision.reason_code,
            "status": decision.status,
            "doc_scope": has_doc_scope,
            "kinds": {k: v for k, v in kinds.items()},
            "norm_unresolved": list(unresolved_norm),
        })

        print(f"## {cid}  [{case.get('kind','?')}/{case.get('difficulty','?')}]")
        print(f"   Q: {question}")
        print(f"   裁决: status={decision.status}  reason={decision.reason_code}")
        print(f"   话术: {decision.question}")
        print(f"   document_scope 命中: {has_doc_scope}"
              + ("   ← :280 豁免本可救它" if has_doc_scope else "   ← 豁免不生效，这是被拦的直接原因"))
        print(f"   清单 kinds: {kinds}")
        if unresolved_norm:
            print(f"   normalizer.unresolved: {list(unresolved_norm)}")
        print()

    # 归因汇总
    print("=" * 100)
    print("## 归因汇总（按 reason_code）")
    by_reason: dict[str, list[str]] = {}
    for row in rows:
        by_reason.setdefault(row["reason"], []).append(row["id"])
    for reason, ids in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  {reason:<32} {len(ids):>2} 题  {ids}")
    print()
    no_doc = [r["id"] for r in rows if not r["doc_scope"]]
    print(f"## document_scope 未命中（:280 豁免救不了）: {len(no_doc)} 题")
    print(f"   {no_doc}")

    out = ROOT / "eval" / "reports" / "clarifier_block_diagnosis.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[产出] {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
