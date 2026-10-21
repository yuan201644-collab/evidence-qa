"""检索/溯源可验证性审计：把「答题正确率」拆成可复核的检索指标

为什么需要它
------------
`eval/` 里已有的判断题只给「答对/答错」，无法回答「答错是因为没检索到，
还是检索到了但生成说错」。本脚本只做前者：拿系统自己输出的引用
（citation）与人工标注的标准出处（gold）比对，算出

  - doc_hit   ：引用里是否出现了正确的文档
  - page_hit  ：正确文档的页码是否对上（gold 没有页码时不参与统计）
  - section_hit：正确章节是否对上（gold 没有章节时不参与统计）
  - mrr       ：第一个正确文档出现在第几位引用的倒数均值

这些都是纯字符串/整数比对，评审可以拿同一份 std 文件复算。

输入格式
--------
citations.json —— 系统输出，一个列表，每项：
    {"qid": "q1",
     "sources": [{"doc_id": "KB-001", "page": 1, "section": "第二章 质保条款"}, ...]}
  字段要求：qid 必填；sources 可以为空列表（表示无引用）；page 可为 null。

gold.json —— 人工标注，一个列表，每项：
    {"qid": "q1", "doc_id": "KB-001", "page": 1, "section": "第二章 质保条款"}
  doc_id 必填；page / section 可为 null，为 null 时该维度不计入分母。

用法
----
    python audit_retrieval.py --citations cites.json --gold gold.json
    python audit_retrieval.py --citations cites.json --gold gold.json --out report.json

注意（写进报告时要一并说明）
----------------------------
1. 本脚本衡量「引用是否指向对的出处」，不衡量「引用的原文片段是否支持结论」。
   后者需要人工抽读，或在有蕴含模型时另做。
2. gold 由人标注，标注口径会显著影响 section_hit。gold.json 里每条都应能
   追溯到文档的具体页，建议与 `data/corpus/` 里的原文一起归档。
3. 正确的「拒答」（问题本身无答案）不应计入 doc_hit 分母，
   这类题请在 gold 里标记 `"answerable": false`，脚本会自动跳过。

依赖：仅标准库。
许可：Apache-2.0。
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional


def _norm(text: Any) -> str:
    """章节名比对用的归一化：去空白、去常见分隔符、统一小写。

    只做「同一章节的两种写法」对齐，不做同义改写 —— 同义改写会让
    section_hit 变成主观判断，失去可复算性。
    """
    if text is None:
        return ""
    s = str(text).lower()
    for ch in (" ", "\t", "\n", "\u3000", "-", "_", "、", "，", ",", "：", ":"):
        s = s.replace(ch, "")
    return s


def _section_match(cited: Optional[str], gold: Optional[str]) -> bool:
    """章节是否算命中。

    采用「双向包含」：引用写「第二章 质保条款」、gold 写「质保条款」算命中，
    反之也算。比全等宽松，但比模糊匹配严格 —— 不引入编辑距离阈值。
    """
    a, b = _norm(cited), _norm(gold)
    if not a or not b:
        return False
    return a in b or b in a


def audit(citations: List[Dict[str, Any]], gold: List[Dict[str, Any]]) -> Dict[str, Any]:
    gold_by_qid = {str(g.get("qid")): g for g in gold}
    cites_by_qid = {str(c.get("qid")): c for c in citations}

    missing_citation = sorted(set(gold_by_qid) - set(cites_by_qid))
    missing_gold = sorted(set(cites_by_qid) - set(gold_by_qid))

    total = 0
    doc_hits = 0
    page_denom = page_hits = 0
    sec_denom = sec_hits = 0
    reciprocal_ranks: List[float] = []
    per_question: List[Dict[str, Any]] = []
    skipped_unanswerable = 0

    for qid, g in gold_by_qid.items():
        if g.get("answerable") is False:
            skipped_unanswerable += 1
            continue
        total += 1
        sources = (cites_by_qid.get(qid) or {}).get("sources") or []
        gold_doc = g.get("doc_id")

        rank = 0
        for i, src in enumerate(sources, start=1):
            if str(src.get("doc_id")) == str(gold_doc):
                rank = i
                break
        doc_hit = rank > 0
        doc_hits += int(doc_hit)
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)

        # 页码 / 章节只在「文档命中」且 gold 有该维度时统计 ——
        # 文档都没命中时讨论页码没有意义。
        matched_src = sources[rank - 1] if rank else None
        page_ok: Optional[bool] = None
        sec_ok: Optional[bool] = None
        if matched_src is not None:
            if g.get("page") is not None:
                page_denom += 1
                page_ok = matched_src.get("page") == g.get("page")
                page_hits += int(bool(page_ok))
            if g.get("section") is not None:
                sec_denom += 1
                sec_ok = _section_match(matched_src.get("section"), g.get("section"))
                sec_hits += int(bool(sec_ok))

        per_question.append(
            {
                "qid": qid,
                "doc_hit": doc_hit,
                "doc_rank": rank,
                "page_hit": page_ok,
                "section_hit": sec_ok,
                "cited_docs": [s.get("doc_id") for s in sources],
            }
        )

    def _ratio(num: int, den: int) -> Optional[float]:
        if not den:
            return None
        return round(float(Decimal(num) / Decimal(den)), 4)

    return {
        "questions_scored": total,
        "questions_skipped_unanswerable": skipped_unanswerable,
        "doc_hit_rate": _ratio(doc_hits, total),
        "doc_hits": doc_hits,
        "page_hit_rate": _ratio(page_hits, page_denom),
        "page_scored": page_denom,
        "section_hit_rate": _ratio(sec_hits, sec_denom),
        "section_scored": sec_denom,
        "mrr": round(sum(reciprocal_ranks) / len(reciprocal_ranks), 4)
        if reciprocal_ranks
        else None,
        "gold_without_citation": missing_citation,
        "citation_without_gold": missing_gold,
        "per_question": per_question,
        "definition": {
            "doc_hit_rate": "引用里出现正确文档的题目占比",
            "page_hit_rate": "文档命中且 gold 有页码的题里，页码对上的占比",
            "section_hit_rate": "文档命中且 gold 有章节的题里，章节对上的占比",
            "mrr": "第一个正确文档位置的倒数均值；无引用记 0",
            "section_match": "归一化后双向包含即算命中，不做同义改写",
        },
    }


def _load(path: Path) -> List[Dict[str, Any]]:
    # utf-8-sig：Windows 上用 PowerShell / 记事本存出来的 JSON 常带 BOM，
    # 用 utf-8 会直接解析失败。utf-8-sig 对无 BOM 的文件行为一致。
    with path.open("r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path} 顶层应为列表，实际是 {type(data).__name__}")
    return data


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="检索/溯源可验证性审计")
    ap.add_argument("--citations", required=True, type=Path, help="系统输出引用 JSON")
    ap.add_argument("--gold", required=True, type=Path, help="人工标注标准出处 JSON")
    ap.add_argument("--out", type=Path, help="报告输出路径（省略则打到标准输出）")
    args = ap.parse_args(argv)

    report = audit(_load(args.citations), _load(args.gold))
    text = json.dumps(report, ensure_ascii=False, indent=2)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"written: {args.out}")
        print(
            f"doc_hit={report['doc_hit_rate']} "
            f"page_hit={report['page_hit_rate']} "
            f"section_hit={report['section_hit_rate']} "
            f"mrr={report['mrr']} n={report['questions_scored']}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
