"""rag_metrics.py 的单元测试。

跑法（不需要 pytest）：
    python eval/test_rag_metrics.py

为什么这批测试必须存在（2026-09-20 的两次返工都出在这里）：
    1) **字段名错配**。契约 `DocRef` 用 `doc_id`/`section_path`，用例 `gold_refs` 用
       `doc`/`anchor`，判定器当初两边都按 `doc`/`anchor` 取值。接上真实契约 refs 后
       命中率会**静默归零**（不报错，只是全判未命中）；而 oracle 回放 `gold_refs`
       天然同形，自测全绿 —— 缺陷被自己的自测掩盖。下面 `test_contract_shape_refs()`
       就是钉死这一条的回归用例。
    2) **引用校验的两件事被合成一个布尔**。口径要求「片段非空」且「指向真实文档」，
       合成之后哪一项没过看不出来，演示时无法解释。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rag_metrics import (  # noqa: E402
    RagTally,
    evaluate_citations,
    evaluate_faithfulness,
    evaluate_retrieval,
    judge_case,
    normalize_doc_id,
    only_doc_refs,
    ref_anchor,
    ref_doc_id,
)

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"[{name}] 期望 {want!r}，实际 {got!r}")
        print(f"  FAIL  {name}: 期望 {want!r}，实际 {got!r}")
    else:
        print(f"  ok    {name}")


FILENAME = "KB-001_智能计算设备年度维保服务合同.pdf"
CONTRACT_REF = {
    "kind": "doc",
    "doc_id": FILENAME,
    "section_path": "第二章 质保条款",
    "snippet": "2.1　质保期限：自设备验收合格之日起 36 个月。",
}
GOLD_REF = {"doc": FILENAME, "anchor": "第二章 质保条款"}

CASE = {
    "id": "rag-t-01",
    "gold_refs": [dict(GOLD_REF, snippet="2.1　质保期限：自设备验收合格之日起 36 个月。")],
    "answer_points": [
        {"point": "整机 36 个月", "match_any": ["36个月", "36 个月"]},
        {"point": "GPU 单独 24 个月", "match_any": ["GPU", "加速卡"]},
    ],
    "forbidden_entities": ["48 个月"],
}
GOOD_ANSWER = "整机质保期 36 个月，GPU 加速卡单独按 24 个月计算。"


# --------------------------------------------------------------------------------------
# 文档标识归一
# --------------------------------------------------------------------------------------
def test_normalize_doc_id() -> None:
    print("normalize_doc_id：编号与文件名互认")
    check("编号原样", normalize_doc_id("KB-001"), "KB-001")
    check("文件名取前缀", normalize_doc_id(FILENAME), "KB-001")
    check("小写编号归一为大写", normalize_doc_id("kb-007_x.pdf"), "KB-007")
    check("不符合编号规范的原样返回（交给 unresolved 暴露）", normalize_doc_id("合同.pdf"), "合同.pdf")
    check("空值返回空", normalize_doc_id(None), "")

    check("两侧写法一致才可比", normalize_doc_id(FILENAME) == normalize_doc_id("KB-001"), True)


def test_dual_shape_accessors() -> None:
    print("字段取值：契约形状与用例形状都要能读")
    check("契约形状取 doc_id", ref_doc_id(CONTRACT_REF), FILENAME)
    check("用例形状取 doc", ref_doc_id(GOLD_REF), FILENAME)
    check("契约形状取 section_path", ref_anchor(CONTRACT_REF), "第二章 质保条款")
    check("用例形状取 anchor", ref_anchor(GOLD_REF), "第二章 质保条款")


def test_only_doc_refs_filters_sql() -> None:
    print("引用列表混装：SqlRef 必须被剔除")
    refs = [{"kind": "sql", "table": "customer", "snippet": "SELECT ..."}, CONTRACT_REF]
    kept, dropped = only_doc_refs(refs)
    check("只留文档引用", len(kept), 1)
    check("剔除条数记账", dropped, 1)
    check("保留的是文档那条", kept[0]["section_path"], "第二章 质保条款")
    # gold_refs 形状没有 kind，按文档引用处理，旧用例不必改
    check("无 kind 字段按文档引用处理", only_doc_refs([GOLD_REF]), ([GOLD_REF], 0))


# --------------------------------------------------------------------------------------
# 检索命中 —— 字段名错配的回归闸门
# --------------------------------------------------------------------------------------
def test_contract_shape_refs() -> None:
    """**本文件最重要的用例**：预测侧只给契约字段名时，必须命中。

    若把 `evaluate_retrieval` 改回只认 `doc`/`anchor`，这条立刻由 pass 变 fail。
    """
    print("检索命中：契约形状 refs（doc_id/section_path）")
    r = evaluate_retrieval(CASE, [CONTRACT_REF])
    check("契约形状必须命中", r.hit, True)
    check("命中位置记为第 1 条", r.matched_rank, 1)
    check("命中理由是精确命中", "精确" in r.reason, True)
    check("参与判定的文档引用条数", r.considered, 1)

    # 契约形状 + 文件名/编号混用：归一之后仍要命中
    mixed = dict(CONTRACT_REF, doc_id="KB-001")
    check("doc_id 写成 KB-001 也命中", evaluate_retrieval(CASE, [mixed]).hit, True)


def test_retrieval_miss_and_degrade() -> None:
    print("检索命中：未命中 / 退化为文档级")
    other = {"kind": "doc", "doc_id": "KB-009_x.pdf", "section_path": "一 入职", "snippet": "…"}
    r = evaluate_retrieval(CASE, [other])
    check("不在标准证据块内 → 未命中", r.hit, False)
    check("未命中理由可读", "未命中" in r.reason, True)

    doc_only = {"kind": "doc", "doc_id": FILENAME, "snippet": "片段"}
    r2 = evaluate_retrieval(CASE, [doc_only])
    check("只给文档不给锚点 → 仍算命中", r2.hit, True)
    check("但要标注是宽松判定", "仅文档级命中" in r2.reason, True)


def test_retrieval_drops_sql_refs() -> None:
    print("检索命中：检索列表里混入 SqlRef")
    sql_ref = {"kind": "sql", "table": "customer", "snippet": "SELECT 1"}
    r = evaluate_retrieval(CASE, [sql_ref, CONTRACT_REF])
    check("剔除 SqlRef 后仍命中", r.hit, True)
    check("剔除条数记进结果", r.dropped_non_doc, 1)

    r2 = evaluate_retrieval(CASE, [sql_ref])
    check("只有 SqlRef → 未命中（不能算文档命中）", r2.hit, False)
    check("理由说明剔除了非 doc 引用", "非 doc" in r2.reason, True)


def test_retrieval_empty() -> None:
    print("检索命中：空列表")
    check("空列表未命中", evaluate_retrieval(CASE, []).hit, False)


# --------------------------------------------------------------------------------------
# 引用校验 —— 两件事分开报
# --------------------------------------------------------------------------------------
def test_citations_two_conditions_split() -> None:
    print("引用校验：片段非空 / 指向真实文档，分开报")
    known = ["KB-001", "KB-009"]

    ok = evaluate_citations([CONTRACT_REF], known_doc_ids=known)
    check("两条子条件都满足", (ok.snippet_nonempty, ok.points_to_real_doc, ok.ok), (True, True, True))

    no_snip = evaluate_citations([dict(CONTRACT_REF, snippet="")], known_doc_ids=known)
    check("片段空 → snippet_nonempty=False", no_snip.snippet_nonempty, False)
    check("但文档是真的 → points_to_real_doc=True", no_snip.points_to_real_doc, True)
    check("合成结果 False", no_snip.ok, False)

    fake = evaluate_citations([dict(CONTRACT_REF, doc_id="KB-999_编造的.pdf")], known_doc_ids=known)
    check("片段非空 → snippet_nonempty=True", fake.snippet_nonempty, True)
    check("文档不存在 → points_to_real_doc=False", fake.points_to_real_doc, False)
    check("解析不到的编号被单独列出（疑似编造）", fake.unresolved_doc_ids, ["KB-999"])
    check("合成结果 False", fake.ok, False)

    # 没给基准时不许假装通过，也不许假装失败：如实降级，由调用方决定采信与否
    blind = evaluate_citations([CONTRACT_REF])
    check("无基准时不虚构 resolved", blind.resolved_doc_ids, [])
    check("无基准时仍能判片段非空", blind.snippet_nonempty, True)


# --------------------------------------------------------------------------------------
# 忠实度与单题通过
# --------------------------------------------------------------------------------------
def test_faithfulness_subconditions() -> None:
    print("忠实度：全能中 / 漏要点 / 出现禁答实体")
    good = evaluate_faithfulness(CASE, GOOD_ANSWER)
    check("要点全中", good.all_hit, True)
    check("无禁答实体", good.forbidden_absent, True)
    check("禁答实体为空表", good.hallucinated, [])

    partial = evaluate_faithfulness(CASE, "整机质保期 36 个月。")
    check("漏一个要点 → all_hit=False（口径要的是全中，不是覆盖率高）", partial.all_hit, False)
    check("但仍无禁答实体", partial.forbidden_absent, True)

    bad = evaluate_faithfulness(CASE, GOOD_ANSWER + "质保期也可能是 48 个月。")
    check("出现禁答实体 → forbidden_absent=False", bad.forbidden_absent, False)
    check("禁答实体被列出", bad.hallucinated, ["48 个月"])
    check("幻觉扣分后得分低于满分", bad.score() < 1.0, True)


def test_judge_case_three_subconditions() -> None:
    print("单题通过：三个子条件各自独立，passed 是它们的与")
    known = ["KB-001"]
    v = judge_case(CASE, GOOD_ANSWER, [CONTRACT_REF], known_doc_ids=known)
    check("三条都满足 → passed", v.passed, True)
    check("基准来源标注为 known", v.citation_basis, "known")
    check("明细里带未命中要点", v.details["missed_points"], [])
    check("明细里带引用体检", v.details["refs"]["snippet_nonempty"], True)

    v1 = judge_case(CASE, "整机质保期 36 个月。", [CONTRACT_REF], known_doc_ids=known)
    check("漏要点 → 不通过，且能指出是这一条", (v1.passed, v1.gold_points_all_hit), (False, False))
    check("其余两条仍为真", (v1.forbidden_absent, v1.citation_ok), (True, True))

    v2 = judge_case(CASE, GOOD_ANSWER, [dict(CONTRACT_REF, snippet="")], known_doc_ids=known)
    check("引用片段空 → 不通过，且只有引用这条为假", (v2.passed, v2.citation_ok, v2.gold_points_all_hit), (False, False, True))

    v3 = judge_case(CASE, GOOD_ANSWER, [CONTRACT_REF])  # 不给基准
    check("无基准时退到「片段非空」，并留痕", (v3.citation_basis, v3.citation_ok), ("none", True))

    v4 = judge_case(CASE, GOOD_ANSWER, [], known_doc_ids=known)
    check("没有引用 → 不通过（口径要求必须带出处）", (v4.passed, v4.citation_ok), (False, False))


def test_rag_tally() -> None:
    print("汇总：通过计数 + 未通过下钻")
    known = ["KB-001"]
    t = RagTally()
    t.add("a", evaluate_retrieval(CASE, [CONTRACT_REF]), evaluate_faithfulness(CASE, GOOD_ANSWER),
          judge_case(CASE, GOOD_ANSWER, [CONTRACT_REF], known_doc_ids=known))
    t.add("b", evaluate_retrieval(CASE, [CONTRACT_REF]), evaluate_faithfulness(CASE, "整机 36 个月。"),
          judge_case(CASE, "整机 36 个月。", [CONTRACT_REF], known_doc_ids=known))
    t.add("c", evaluate_retrieval(CASE, []), None)

    s = t.summary()
    check("总数", s["total"], 3)
    check("命中率 2/3", s["hit_rate"], round(2 / 3, 4))
    check("单题通过数 1/3", (s["passed"], s["pass_rate"]), (1, round(1 / 3, 4)))

    failing = t.failing_cases()
    check("未通过只列判过而不通过的（没判的不算）", [c["id"] for c in failing], ["b"])
    check("下钻能指出漏了哪个要点", "GPU" in "".join(failing[0]["missed_points"]) or failing[0]["gold_points_all_hit"] is False, True)


def test_prediction_ref_split() -> None:
    """`retrieved` 与 `answer_refs` 的职责分工必须固定住。

    2026-09-22 按 A/B 的意见拆分：检索命中率看**召回**，引用校验看**答案实际引用**。
    不拆开时，B 的工具把全量召回挂在 trace 上，命中率会按「被引用的条数」算 ——
    检索对了但模型没引用，也判成没命中（系统性低估）。
    这里直接测 `runner.Prediction` 的选择逻辑，不依赖网络与数据库。

    ⚠️ 本条测试的两个方向都必须覆盖（A 于 2026-09-22 19:05 审查指出：
    当时只测了 `refs_for_citation`，**引用侧**，没测命中率侧）：
      · 引用侧：`answer_refs` 优先，缺省退 `retrieved`
      · **命中率侧**：`retrieved` 优先，缺省退 `answer_refs` —— 漏了这条，
        拆分时把 `refs` 兜底删掉也不会有人发现，旧格式预测命中率会静默归零。
    """
    print("预测字段拆分：召回 / 答案引用")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from runner import JsonlPredictor, Prediction  # noqa: PLC0415

    recall = {"kind": "doc", "doc_id": "KB-001_a.pdf", "snippet": "s"}
    cited = {"kind": "doc", "doc_id": "KB-999_b.pdf", "snippet": "s"}

    # ---- 引用侧（`refs_for_citation`）----
    # 只有召回：退回召回列表，旧预测文件不能因为缺字段被判 0
    check("只有 retrieved 时退回它", Prediction(retrieved=[recall]).refs_for_citation(), [recall])
    # 两份都有：引用类判定取答案实际引用
    check(
        "两份都有时取 answer_refs",
        Prediction(retrieved=[recall], answer_refs=[cited]).refs_for_citation(),
        [cited],
    )
    # 两份都没有：空列表，不抛异常
    check("都没有时返回空", Prediction().refs_for_citation(), [])
    # 字段名混淆：`refs`（契约叫法）要落进 answer_refs，`retrieved` 不被它覆盖
    p = Prediction(retrieved=[recall], answer_refs=[cited])
    check("retrieved 不被 answer_refs 影响", p.retrieved, [recall])

    # ---- 命中率侧（`refs_for_retrieval`）----
    check(
        "两份都有时命中率取 retrieved（不是 answer_refs）",
        Prediction(retrieved=[recall], answer_refs=[cited]).refs_for_retrieval(),
        [recall],
    )
    check(
        "只有 answer_refs 时退回它（否则命中率静默归零）",
        Prediction(answer_refs=[cited]).refs_for_retrieval(),
        [cited],
    )
    check("都没有时返回空", Prediction().refs_for_retrieval(), [])

    # ---- 近似标记：报告要如实标注，不能把近似值当真召回 ----
    check("只有答案引用 → 标记为近似", Prediction(answer_refs=[cited]).retrieval_is_approximated, True)
    check("有真召回 → 不标近似", Prediction(retrieved=[recall], answer_refs=[cited]).retrieval_is_approximated, False)
    check("两样都没有 → 不标近似", Prediction().retrieval_is_approximated, False)


def test_jsonl_predictor_old_format() -> None:
    """旧格式预测文件（只写契约名 `refs`）的检索命中率不能归零。

    这是 A 于 2026-09-22 19:05 报出的 bug 的**端到端**回归：
    走真实的 `JsonlPredictor.predict_rag` 解析路径，
    而不是手工构造 `Prediction` —— 缺陷恰恰出在解析那一步
    （`retrieved = row["retrieved"] or []` 把 `refs` 兜底删了）。

    变异验证的方式：把 `refs_for_retrieval()` 改回 `self.retrieved`，
    本用例立刻红（hit=False），而引用校验仍然绿 —— 正是缺陷难以察觉之处。
    """
    print("旧格式预测（只有 refs）：命中率不能归零")
    import json
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from runner import JsonlPredictor  # noqa: PLC0415

    # 用真实用例的标准答案反着写：refs 填 gold 形状，
    # 保证只要兜底链路通着，命中率就必然是 True。
    gold = {"kind": "doc", "doc_id": "KB-001_智算中心维保合同.pdf", "section_path": "第二章 质保条款"}
    rows = [
        # 只有契约名 refs —— 用两条不同 id 的行，`JsonlPredictor` 按 id 建索引，
        # 同一个 id 写两行会被后一行覆盖（那不是本用例要测的东西）。
        {"id": "OLD", "answer": GOOD_ANSWER, "refs": [gold]},
        {"id": "NEW", "answer": GOOD_ANSWER, "retrieved": [gold]},
    ]
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "preds.jsonl"
        fp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
        )
        predictor = JsonlPredictor(fp)
        old = predictor.predict_rag({"id": "OLD"})
        new = predictor.predict_rag({"id": "NEW"})

    check("旧格式：refs 落进 answer_refs", len(old.answer_refs), 1)
    check("旧格式：retrieved 仍为空（不复制）", old.retrieved, [])
    check("旧格式：命中率走兜底、不为空", len(old.refs_for_retrieval()), 1)
    check(
        "旧格式：命中率算出来是真命中",
        evaluate_retrieval(CASE, old.refs_for_retrieval()).hit,
        True,
    )
    check("旧格式：如实标记为近似", old.retrieval_is_approximated, True)
    check("新格式：不标近似", new.retrieval_is_approximated, False)


def test_retrieval_exact_scanned_before_doc_level() -> None:
    """精确锚点必须**先扫完**再退文档级 —— 逐题 reason 不能把精确命中误标成宽松。

    2026-10-05 B 实测报出：评分器逐题 `reason` 只标出 10 题精确，独立复核其实是 14 题。
    根因是单遍循环遇到「同文档但无锚点」的结果就提前 `return` 文档级命中。
    变异验证：把 `evaluate_retrieval` 改回单遍（遇到文档级候选就 return），
    本用例立刻红 —— reason 变成「仅文档级命中」、rank 变成 1。
    """
    print("检索命中：精确锚点先扫完，再退文档级")
    doc_only = {"kind": "doc", "doc_id": FILENAME, "snippet": "片段"}  # 同文档，无锚点
    exact = dict(CONTRACT_REF)  # 同文档 + 正确锚点
    r = evaluate_retrieval(CASE, [doc_only, exact])
    check("仍算命中（布尔口径不变）", r.hit, True)
    check("标为精确命中，而非宽松", "精确" in r.reason, True)
    check("exact=True", r.exact, True)
    check("命中位置指向第 2 条（精确那条）", r.matched_rank, 2)

    r2 = evaluate_retrieval(CASE, [doc_only])
    check("没有精确候选才退宽松", (r2.hit, r2.exact), (True, False))
    check("宽松理由如实标注", "仅文档级命中" in r2.reason, True)

    # 同文档 + 错锚点：给了锚点但不是标准锚点 → 不算命中（与单遍版本的行为一致）
    wrong = {"kind": "doc", "doc_id": FILENAME, "section_path": "第九章 不存在", "snippet": "x"}
    check("同文档 + 错锚点 → 未命中（口径不变）", evaluate_retrieval(CASE, [wrong]).hit, False)


def test_rag_tally_exact_vs_doc_level() -> None:
    """严格/宽松**并列**计数：主命中率不变，exact 只数精确锚点。"""
    print("汇总：严格/宽松并列计数")
    doc_only = {"kind": "doc", "doc_id": FILENAME, "snippet": "片段"}
    other = {"kind": "doc", "doc_id": "KB-009_x.pdf", "section_path": "一 入职", "snippet": "x"}
    t = RagTally()
    t.add("exact", evaluate_retrieval(CASE, [CONTRACT_REF]), None)
    t.add("loose", evaluate_retrieval(CASE, [doc_only]), None)
    t.add("miss", evaluate_retrieval(CASE, [other]), None)
    s = t.summary()
    check("主命中率仍含宽松 2/3", s["hit_rate"], round(2 / 3, 4))
    check("精确命中数 1", s["exact_hits"], 1)
    check("宽松命中数 1", s["doc_level_hits"], 1)
    check("严格命中率 1/3", s["exact_hit_rate"], round(1 / 3, 4))
    check("精确+宽松 == 命中总数", s["exact_hits"] + s["doc_level_hits"], s["retrieval_hits"])


def test_rag_tally_counts_approximated() -> None:
    """近似命中率的题数要进汇总——对外报分时必须能一眼看出哪一档不可信。"""
    print("汇总：近似命中率的计数")
    t = RagTally()
    t.add("a", evaluate_retrieval(CASE, [CONTRACT_REF]), None, approximated=True)
    t.add("b", evaluate_retrieval(CASE, [CONTRACT_REF]), None)
    s = t.summary()
    check("近似题数只数标了的", s["retrieval_approximated"], 1)
    check("命中率照常算", s["hit_rate"], 1.0)
    check("逐题明细留痕", [c.get("retrieval_approximated") for c in t.cases], [True, None])


def main() -> int:
    for fn in (
        test_normalize_doc_id,
        test_dual_shape_accessors,
        test_only_doc_refs_filters_sql,
        test_contract_shape_refs,
        test_retrieval_miss_and_degrade,
        test_retrieval_drops_sql_refs,
        test_retrieval_empty,
        test_citations_two_conditions_split,
        test_faithfulness_subconditions,
        test_judge_case_three_subconditions,
        test_rag_tally,
        test_prediction_ref_split,
        test_jsonl_predictor_old_format,
        test_retrieval_exact_scanned_before_doc_level,
        test_rag_tally_exact_vs_doc_level,
        test_rag_tally_counts_approximated,
    ):
        fn()
        print()

    print("=" * 60)
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for f in FAILURES:
            print("  -", f)
        return 1
    print("全部通过。RAG 判定口径与契约字段名一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
