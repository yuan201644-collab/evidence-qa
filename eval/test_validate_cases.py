"""validate_cases.py 的单元测试。

跑法（不需要 pytest）：
    python eval/test_validate_cases.py

为什么要有这批测试（2026-09-30）：
    多跳用例集 `rag_multihop.jsonl`（B 于 M6 加入，20 条）一度**没进结构校验**——
    它过了 `run_rag`，却绕过了 `validate_cases.py`，等于题集结构无人把关。
    补进去时才发现两个必须钉死的判断：

    1) **档位豁免不能靠「忘记加」**。多跳集 20/20 全是 `hard`，若沿用主集的
       「三档都要有」检查会直接误报。豁免是有意的设计（见 `DIFFICULTY_SPREAD_SUITES`
       的注释），所以要用测试把它固定住 —— 否则日后有人把文件加回该集合，
       会逼着给多跳题贴失真的难度标签。
    2) **「多跳必须跨文档」是错的判据**。`rag-mh-09` 是同一份预案里
       第 5 章「应急响应分级」+ 第 2 章「组织机构」的**跨条款**推理，合法。
       正确判据是「(doc, anchor) 去重后 >= 2」——既拦得住假多跳，又不误伤跨条款。

    另外把 `CASE_FILES` 与 `REQUIRED` 的一致性也断言上：新增用例集时只改一处、
    另一处漏了，这里会红。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
import validate_cases as vc  # noqa: E402

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"[{name}] 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL  {name}: 期望 {want!r}，实际 {got!r}")
    else:
        print(f"  ok    {name}")


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


GOOD = {
    "id": "rag-mh-01",
    "difficulty": "hard",
    "kind": "multi_hop",
    "question": "跨文档问句",
    "gold_refs": [
        {"doc": "A.pdf", "anchor": "第 1 章", "snippet": "s1"},
        {"doc": "B.pdf", "anchor": "第 2 节", "snippet": "s2"},
    ],
    "answer_points": [{"point": "要点", "match_any": ["x"]}],
}


def _validate(rows: list[dict]) -> tuple[list[str], str]:
    """把行喂给真校验器（走临时目录，不碰工作区），返回（问题列表, 终端输出）。"""
    with tempfile.TemporaryDirectory(prefix="vc-test-") as td:
        f = Path(td) / "rag_multihop.jsonl"
        f.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            problems = vc.validate(f)
    return problems, buf.getvalue()


def test_case_files_cover_required() -> None:
    """`CASE_FILES` 与 `REQUIRED` 必须一一对应，且多跳在其中。"""
    print("用例文件覆盖（CASE_FILES / REQUIRED 一致性）")
    check("CASE_FILES 与 REQUIRED 键集一致", set(vc.CASE_FILES), set(vc.REQUIRED))
    check_true("CASE_FILES 含 rag_multihop.jsonl", "rag_multihop.jsonl" in vc.CASE_FILES)
    check_true("REQUIRED 已声明多跳字段", "rag_multihop.jsonl" in vc.REQUIRED)
    check(
        "多跳 REQUIRED 与 rag 主集同构",
        vc.REQUIRED["rag_multihop.jsonl"],
        vc.REQUIRED["rag.jsonl"],
    )
    check_true("多跳走 RAG 校验分支", "rag_multihop.jsonl" in vc.RAG_SUITES)
    check_true(
        "多跳被档位检查豁免", "rag_multihop.jsonl" not in vc.DIFFICULTY_SPREAD_SUITES
    )


def test_multihop_baseline_and_difficulty_exempt() -> None:
    """合法多跳应零问题；且**不得**因全 hard 被要求三档覆盖。"""
    print("多跳基线 + 档位豁免")
    problems, out = _validate([GOOD])
    check("合法多跳零问题", problems, [])
    check_true("档位分布照常打印（不隐藏）", "hard" in out)
    check_true("未被要求补 easy 档", not any("档没有用例" in p for p in problems))


def test_multihop_intra_doc_is_legal() -> None:
    """同一文档的不同章节 = 合法跨条款多跳，不能误伤（`rag-mh-09` 的形状）。"""
    print("同文档跨条款（合法，不得误伤）")
    row = {
        **GOOD,
        "gold_refs": [
            {"doc": "预案.pdf", "anchor": "5 应急响应分级", "snippet": "s1"},
            {"doc": "预案.pdf", "anchor": "2 组织机构", "snippet": "s2"},
        ],
    }
    problems, _ = _validate([row])
    check("同文档跨条款零问题", problems, [])


def test_multihop_same_anchor_is_caught() -> None:
    """两条 ref 指向同一处 —— 跨条款/跨文档推理无从成立，必须抓。"""
    print("假多跳（同一 doc+anchor）应被抓")
    row = {
        **GOOD,
        "gold_refs": [
            {"doc": "A.pdf", "anchor": "第 1 章", "snippet": "s1"},
            {"doc": "A.pdf", "anchor": "第 1 章", "snippet": "s1"},
        ],
    }
    problems, _ = _validate([row])
    check_true("抓到同一处", any("multi_hop" in p for p in problems))


def test_multihop_field_and_value_checks() -> None:
    """缺字段 / 非法 kind / 非法难度 / id 重复 —— 都要抓。"""
    print("字段与取值检查")
    miss = {k: v for k, v in GOOD.items() if k != "answer_points"}
    problems, _ = _validate([miss])
    check_true("缺 answer_points", any("缺字段 answer_points" in p for p in problems))

    problems, _ = _validate([{**GOOD, "kind": "weird"}])
    check_true("非法 kind", any("kind 必须是" in p for p in problems))

    problems, _ = _validate([{**GOOD, "difficulty": "super_hard"}])
    check_true("非法难度", any("difficulty 必须是" in p for p in problems))

    problems, _ = _validate([GOOD, GOOD])
    check_true("id 重复", any("id 重复" in p for p in problems))


def test_forbidden_entities_must_not_clash_with_answers() -> None:
    """禁答实体若被标准答案包含，oracle 必然掉分 —— 用例自相矛盾，必须抓。

    这是 2026-09-30 那个 bug 的根因：多跳集 4 条写了裸词「一致」「是同一家公司」，
    而判据是子串包含（`rag_metrics.py`），中文里正确的否定答法必然包含它们 ——
    oracle 回放的答案就是要点文本拼接，于是标准答案自己判自己不过。
    """
    print("禁答实体与标准答案的自洽性")
    bare = {
        **GOOD,
        "forbidden_entities": ["一致"],
        "answer_points": [{"point": "两份文件不一致", "match_any": ["不一致"]}],
    }
    problems, _ = _validate([bare])
    check_true("裸词被『不一致』包含 -> 抓", any("forbidden_entities" in p for p in problems))

    ok = {
        **GOOD,
        "forbidden_entities": ["两份文件一致"],
        "answer_points": [{"point": "两份文件不一致", "match_any": ["不一致"]}],
    }
    problems, _ = _validate([ok])
    check("带主语的完整断言不冲突 -> 零问题", problems, [])

    kw = {
        **GOOD,
        "forbidden_entities": ["差异"],
        "answer_points": [{"point": "两者不同", "match_any": ["有差异"]}],
    }
    problems, _ = _validate([kw])
    check_true("撞上 match_any 也要抓", any("要点关键词" in p for p in problems))


def test_column_types_validation() -> None:
    """列语义标注 `column_types` 的校验（2026-10-04 口径 2.3 落地时新增）。

    为什么必须校验：`column_types` 标错**不会报错**，只会让判定**静静地用错容差**
    （金额列被标成普通数值列 → 1 分钱差异仍判错；反之 → 该判错的放过去）。
    这与「runner 读不到 DATABASE_URL 就静默回落 SQLite」是同一类静默失效，
    必须在用例层就被拦下。
    """
    print("列语义标注 column_types")
    base = {
        **GOOD,
        "expected_columns": ["amount", "name"],
        "gold_sql": "SELECT sum(total) AS amount, name FROM t GROUP BY name",
    }

    problems, _ = _validate([{**base, "column_types": ["money", "text"]}])
    check("合法标注 -> 零问题", problems, [])

    problems, _ = _validate([{**base, "column_types": ["money"]}])
    check_true("长度与 expected_columns 不符 -> 抓", any("长度" in p for p in problems))

    problems, _ = _validate([{**base, "column_types": ["money", "datetime"]}])
    check_true("非法取值 -> 抓", any("非法取值" in p for p in problems))

    problems, _ = _validate(
        [
            {
                **base,
                "column_types": ["money", "text"],
                "gold_sql": "SELECT count(*) AS cnt, name AS nm FROM t GROUP BY name",
            }
        ]
    )
    check_true(
        "标了 money 但 gold_sql 里找不到金额字段 -> 抓",
        any("找不到任何金额字段" in p for p in problems),
    )

    problems, _ = _validate([{**GOOD, "column_types": ["money"]}])
    check_true("有 column_types 却没 expected_columns -> 抓", any("没有 expected_columns" in p for p in problems))


def test_points_without_evidence_is_advisory() -> None:
    """「要点与证据无字面交集」只做**提示**、绝不拦截（2026-10-07 新增）。

    为什么这条只能是 advisory：派生型答案（换算出来的新风险值、两份文件是否一致的
    判断、多跳推理的结论）本来就**逐字不在原文里** —— 做成硬拦截等于禁止一切需要
    计算/推理的题。实测四份真实用例里有 13 个这样的要点，**全部合法**。
    它的用途只是把这类要点摆到人眼前，复核「是不是题目没问的额外要点」。
    """
    print("要点-证据字面交集（提示型，不拦截）")
    hit = {
        **GOOD,
        "gold_refs": [
            {"doc": "A.pdf", "anchor": "第 1 章", "snippet": "风险值 270，属高度危险"},
            {"doc": "B.pdf", "anchor": "第 2 节", "snippet": "补充说明"},
        ],
        "answer_points": [{"point": "风险值 270", "match_any": ["270"]}],
    }
    check("关键词在片段里 -> 不提示", vc.points_without_evidence(hit), [])

    derived = {
        **GOOD,
        "gold_refs": [
            {"doc": "A.pdf", "anchor": "第 1 章", "snippet": "公式 D = L×E×C"},
            {"doc": "B.pdf", "anchor": "第 2 节", "snippet": "风险分级标准"},
        ],
        "answer_points": [{"point": "新风险值 144", "match_any": ["144"]}],
    }
    check("关键词不在片段里 -> 提示", vc.points_without_evidence(derived), ["新风险值 144"])
    problems, _ = _validate([derived])
    check("提示不进入 problems（不拦截）", problems, [])

    nosnip = {
        **GOOD,
        "gold_refs": [{"doc": "A.pdf", "anchor": "第 1 章"}, {"doc": "B.pdf", "anchor": "第 2 节"}],
        "answer_points": [{"point": "x", "match_any": ["y"]}],
    }
    check("无 snippet 可查 -> 不制造噪声", vc.points_without_evidence(nosnip), [])


def test_real_cases_pass() -> None:
    """工作区里的四份真实用例必须全部通过 —— 校验器存在的意义就在这。"""
    print("真实用例文件全量校验")
    root = Path(__file__).resolve().parents[1]
    all_problems: list[str] = []
    for name in vc.CASE_FILES:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            all_problems.extend(vc.validate(root / "eval" / "cases" / name))
    if all_problems:
        for p in all_problems:
            print("    -", p)
    check("四份用例零问题", all_problems, [])
    check_true("rag_multihop.jsonl 在受检名单里", "rag_multihop.jsonl" in vc.CASE_FILES)


def main() -> int:
    test_case_files_cover_required()
    test_multihop_baseline_and_difficulty_exempt()
    test_multihop_intra_doc_is_legal()
    test_multihop_same_anchor_is_caught()
    test_multihop_field_and_value_checks()
    test_forbidden_entities_must_not_clash_with_answers()
    test_column_types_validation()
    test_points_without_evidence_is_advisory()
    test_real_cases_pass()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
