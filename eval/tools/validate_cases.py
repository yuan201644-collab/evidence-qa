#!/usr/bin/env python
"""用例结构校验：把「用例写错了」和「系统答错了」分开。

为什么必须单独一步：
    jsonl 少一个字段、id 重了、answer_points 写成字符串，
    在跑分时的表现都是"这条用例没过"——于是你会去查 NL2SQL 生成是不是有问题，
    而真正的原因是用例文件本身有毛病。这个脚本把这类错误在跑分之前拦下来。

用法：
    python eval/tools/validate_cases.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "eval" / "cases"
sys.path.insert(0, str(ROOT / "eval"))
# 列语义取值只在 `eval/metrics.py` 定义一处，这里引用而不是另写一份，避免两处漂移。
from metrics import COLUMN_KINDS  # noqa: E402
# 文本归一（去空白/标点）同样只留一处 —— 提示型校验复用**判定器的同一套归一**，
# 免得出现「校验器认为有字面交集、判定器认为没有」的两套标准。
from rag_metrics import normalise  # noqa: E402

#: 允许的结果列语义标注取值。
COLUMN_TYPES = set(COLUMN_KINDS)
#: 「gold_sql 是否触碰金额字段」的启发式词表 —— **只用于单向审计**。
#: 规则是「标了 money 就必须在 gold_sql 里找得到金额来源」，措辞宽松、宁可漏报不误报：
#:   · 子串匹配而非 `\b` 词边界 —— `TotalDue` / `LineTotal` 这类驼峰字段也要能命中；
#:   · 只做「标了 money → 必须有来源」这一个方向。
#: 反向（有金额字段却没标 money）**刻意不做**：`SELECT count(*) FROM invoice WHERE total > 5`
#: 这类题的结果列不是金额，反向审计会大面积误报，拦住正常用例。
MONEY_FIELD_TOKENS = (
    "unit_price",
    "total",
    "amount",
    "revenue",
    "price",
    "freight",
    "taxamt",
    "cost",
)

REQUIRED = {
    "nl2sql.jsonl": ["id", "difficulty", "scope", "question", "gold_sql", "tables", "answer_type"],
    "rag.jsonl": ["id", "difficulty", "kind", "question", "gold_refs", "answer_points"],
    "rag_multihop.jsonl": ["id", "difficulty", "kind", "question", "gold_refs", "answer_points"],
    "robust_cases.jsonl": ["id", "difficulty", "degradation", "question", "expected_behavior", "tables"],
}
DIFFICULTIES = {"easy", "medium", "hard"}
NL2SQL_SCOPES = {"single_table", "multi_table"}
RAG_KINDS = {"single_doc", "scanned", "outline_broken", "formula", "multi_hop"}
#: 鲁棒性退化类型白名单。新增取值前先确认语义不与已有项重叠。
#: `time` —— 时间词标准化（「去年 / 上个月 / 本季度」→ 左闭右开绝对区间）。
#:   2026-09-22 由 C 放行：对应 `app` 侧 `ChangeKind.TIME`，题面 F10 点名要求
#:   （别名 / 错别字 / 时间词标准化 三样），此前 robust 用例零覆盖。
#:   ⚠️ 时间词用例必须**显式指定时间基准**（跑分时 `time_anchor` 固定 2024-06-15）：
#:   基准取真实当天会让「今年」落在数据区间（2021-01-01 ~ 2025-12-22）之外 → gold 恒为 0。
#: `format` —— 保留：形态不改语义的格式扰动（如多余的标点、空格），与 `time` 不同。
DEGRADATIONS = {"typo", "alias", "fullwidth", "mixed_lang", "filler", "ambiguous", "format", "time"}
EXPECTED_BEHAVIORS = {"answer", "clarify"}
LANG_SUITES = {"nl2sql.jsonl", "robust_cases.jsonl"}
#: 走同一套 RAG 结构校验的文件集合。多跳集与主集字段逐字同构（2026-09-30 实测确认：
#: 并集完全一致，`gold_refs` / `answer_points` 结构零不合规），因此复用同一分支。
RAG_SUITES = {"rag.jsonl", "rag_multihop.jsonl"}
#: 要求「easy / medium / hard 三档全覆盖」的文件。
#: `rag_multihop.jsonl` **不在其中**：它是专项难度题集（20/20 全 hard），
#: 区分度来自「跳数 / 涉及条款数」而不是难度档，硬凑三档只会逼出失真的难度标签。
#: 它的档位分布仍照常打印，不隐藏。
DIFFICULTY_SPREAD_SUITES = {"nl2sql.jsonl", "rag.jsonl", "robust_cases.jsonl"}
#: 参与校验的用例文件。新增用例集时改这里，并同步在 REQUIRED 里补字段声明
#: （`test_validate_cases.py` 会断言两者一致，漏了会红）。
CASE_FILES = ("nl2sql.jsonl", "rag.jsonl", "rag_multihop.jsonl", "robust_cases.jsonl")


def points_without_evidence(case: dict) -> list[str]:
    """挑出「所有 match_any 关键词都与本题 gold 片段无字面交集」的 answer_point。

    ⚠️ 这是**提示（advisory），不是判错** —— 绝不据此拦截用例。
    原因：派生型答案本来就**不会逐字出现在原文片段里**，例如
      · 按新参数换算出来的风险值（原文只有公式，没有结果数）；
      · 「两份文件不一致」这类判断结论；
      · 多跳推理才得出的「不能直接比较」。
    把它们当错误拦下，等于禁止一切需要计算/推理的题 —— 与校验器
    「宁可漏报不误报」的总体原则相悖。

    它只做一件事：把「要点在证据里找不到字面落点」的条目摆到人眼前，
    让人复核「这条到底是不是题目问的」。2026-10-07 的 `rag-e-03` ③
    （服务热线 7×24）正是这种「题目没问、证据里也没有」的额外要点 ——
    被 B 侧的真实链路实测暴露后，由 C 移出必答点。本提示就是防它复发。

    没有 snippet 可查时返回空（无从判断，不制造噪声）。
    """
    snippet = " ".join(
        str(r.get("snippet") or "") for r in (case.get("gold_refs") or []) if isinstance(r, dict)
    )
    snip_norm = normalise(snippet)
    if not snip_norm:
        return []
    out: list[str] = []
    for p in case.get("answer_points") or []:
        if not isinstance(p, dict):
            continue
        kws = [str(k) for k in (p.get("match_any") or []) if k]
        if kws and not any(normalise(k) in snip_norm for k in kws):
            out.append(str(p.get("point") or ""))
    return out


def validate(path: Path) -> list[str]:
    problems: list[str] = []
    #: 提示项：只打印给人看，**不计入 problems、不影响退出码**（见 points_without_evidence）。
    advice: list[str] = []
    if not path.exists():
        return [f"{path.name}: 文件不存在"]
    try:
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except json.JSONDecodeError as exc:
        return [f"{path.name}: 第 {exc.lineno} 行不是合法 JSON —— {exc.msg}"]

    if not rows:
        return [f"{path.name}: 没有任何用例"]

    seen: set[str] = set()
    for i, r in enumerate(rows, start=1):
        where = f"{path.name}#{i}"

        for field in REQUIRED[path.name]:
            if field not in r or r[field] in (None, "", []):
                problems.append(f"{where}: 缺字段 {field}")

        cid = r.get("id", f"<无 id 行{i}>")
        if cid in seen:
            problems.append(f"{where}: id 重复 —— {cid}")
        seen.add(cid)

        if r.get("difficulty") not in DIFFICULTIES:
            problems.append(f"{where}: difficulty 必须是 {sorted(DIFFICULTIES)}，实际 {r.get('difficulty')!r}")

        if path.name == "robust_cases.jsonl":
            if r.get("degradation") not in DEGRADATIONS:
                problems.append(f"{where}: degradation 必须是 {sorted(DEGRADATIONS)}，实际 {r.get('degradation')!r}")
            behavior = r.get("expected_behavior")
            if behavior not in EXPECTED_BEHAVIORS:
                problems.append(f"{where}: expected_behavior 必须是 {sorted(EXPECTED_BEHAVIORS)}，实际 {behavior!r}")
            # 这类用例的核心约束：要答就必须有标准 SQL；要澄清就必须没有标准 SQL。
            # 允许「既要澄清又有标准 SQL」的模糊用例，等于允许判定器随便挑一头算通过。
            if behavior == "answer" and not r.get("gold_sql"):
                problems.append(f"{where}: expected_behavior=answer 却没给 gold_sql，无法判定")
            if behavior == "clarify" and r.get("gold_sql"):
                problems.append(f"{where}: expected_behavior=clarify 却给了 gold_sql，判定会失去意义")
            if behavior == "answer" and not r.get("canonical_question"):
                problems.append(f"{where}: 缺 canonical_question，退化输入失去对照基准")

        if path.name in LANG_SUITES:
            if r.get("scope") not in NL2SQL_SCOPES:
                problems.append(f"{where}: scope 必须是 {sorted(NL2SQL_SCOPES)}，实际 {r.get('scope')!r}")
            if not isinstance(r.get("tables"), list) or not r["tables"]:
                problems.append(f"{where}: tables 必须是非空数组")
            if "top_n" in r and "order_key_index" not in r:
                problems.append(f"{where}: 声明了 top_n 却没给 order_key_index，边界并列检测会失效")

        # 列语义标注（可选字段 column_types）：只有题目显式声明时才校验。
        # 为什么必须校验：标错不会报错，只会让判定**静静地用错容差** —— 与 runner 读不到
        # DATABASE_URL 就静默回落 SQLite 属同一类风险（2026-10-04 已把后者改成显式告警）。
        col_types = r.get("column_types")
        if col_types is not None:
            if not isinstance(col_types, list):
                problems.append(f"{where}: column_types 必须是数组，实际 {type(col_types).__name__}")
            else:
                expected_cols = r.get("expected_columns")
                if not isinstance(expected_cols, list) or not expected_cols:
                    problems.append(
                        f"{where}: 声明了 column_types 却没有 expected_columns，"
                        f"列语义无从对齐（column_types 按列标注，必须与结果列一一对应）"
                    )
                elif len(col_types) != len(expected_cols):
                    problems.append(
                        f"{where}: column_types 长度 {len(col_types)} 与 expected_columns "
                        f"长度 {len(expected_cols)} 不一致 —— 列语义会张冠李戴"
                    )
                bad = [c for c in col_types if c not in COLUMN_TYPES]
                if bad:
                    problems.append(
                        f"{where}: column_types 含非法取值 {bad}，允许值 {sorted(COLUMN_TYPES)}"
                    )
                # 单向交叉审计：标了 money 就必须能在 gold_sql 里找到金额来源。
                # 防的是「标注漂移」：用例改了 gold_sql 却没同步改标注（或反之）。
                if "money" in col_types:
                    gold_sql = str(r.get("gold_sql") or "").lower()
                    if not any(tok in gold_sql for tok in MONEY_FIELD_TOKENS):
                        problems.append(
                            f"{where}: column_types 标了 money，但 gold_sql 里找不到任何金额字段"
                            f"（词表 {list(MONEY_FIELD_TOKENS)}）—— 标注与标准 SQL 不一致，请核对"
                        )

        if path.name in RAG_SUITES:
            if r.get("kind") not in RAG_KINDS:
                problems.append(f"{where}: kind 必须是 {sorted(RAG_KINDS)}，实际 {r.get('kind')!r}")
            refs = r.get("gold_refs") or []
            if not refs:
                problems.append(f"{where}: gold_refs 为空，检索命中率无判定依据")
            for j, ref in enumerate(refs):
                if not isinstance(ref, dict) or "doc" not in ref:
                    problems.append(f"{where}: gold_refs[{j}] 必须是含 doc 的对象")
            points = r.get("answer_points") or []
            if not isinstance(points, list) or not points:
                problems.append(f"{where}: answer_points 必须是非空数组")
            for j, p in enumerate(points):
                if not isinstance(p, dict):
                    problems.append(f"{where}: answer_points[{j}] 必须是对象，不能是字符串")
                    continue
                if not p.get("point"):
                    problems.append(f"{where}: answer_points[{j}] 缺 point")
                if not (p.get("match_any") or []):
                    problems.append(f"{where}: answer_points[{j}] 缺 match_any，该要点永远判不中")
            # 禁答实体与标准答案的自洽性 —— 这条是「oracle 必须 100%」闸门在用例层的自动化。
            # oracle 回放的答案 = 所有要点文本用「；」拼接（`runner.py` 的 OraclePredictor），
            # 所以禁答实体只要是被某条要点的**子串**，标准答案自己就必然触发幻觉判据 →
            # oracle 永久掉分，且看起来像判定器的错。中文里最容易踩：
            #   「不一致」包含「一致」、「不是同一家公司」包含「是同一家公司」。
            # 也查 match_any：模型按关键词答对时同样会被误判。
            # （2026-09-30：多跳集 4 条踩了这个，oracle 单题通过 16/20，见 to-B 的信）
            point_texts = [str(p.get("point") or "") for p in points if isinstance(p, dict)]
            keywords = [
                str(kw) for p in points if isinstance(p, dict) for kw in (p.get("match_any") or [])
            ]
            for j, ent in enumerate(r.get("forbidden_entities") or []):
                e = str(ent)
                if not e:
                    continue
                clash = [f"要点 {t!r}" for t in point_texts if e in t]
                clash += [f"要点关键词 {k!r}" for k in keywords if e in k]
                if clash:
                    problems.append(
                        f"{where}: forbidden_entities[{j}]={e!r} 与标准答案冲突"
                        f"（是 {' / '.join(clash[:3])} 的子串）——"
                        f" oracle 回放必然触发幻觉判据，用例自相矛盾、永远判不过"
                    )
            # 多跳的判据是「跨条款 / 跨文档」：只盯同一份文档的同一处，那不叫多跳。
            # ⚠️ 判据**不能**简化成「必须跨文档」—— `rag-mh-09` 是同一份预案里
            #    第 5 章「应急响应分级」+ 第 2 章「组织机构」的跨条款推理，属于合法多跳。
            #    取「(doc, anchor) 去重后 >= 2」，既拦得住假多跳，又不误伤跨条款。
            if path.name == "rag_multihop.jsonl" or r.get("kind") == "multi_hop":
                combos = {
                    (ref.get("doc"), ref.get("anchor")) for ref in refs if isinstance(ref, dict)
                }
                if len(combos) < 2:
                    problems.append(
                        f"{where}: kind=multi_hop，但 gold_refs 只指向同一处"
                        f"（去重后 {len(combos)} 个「文档+章节」组合），跨条款/跨文档推理无从成立"
                    )

            # 提示（不拦截）：要点与 gold 片段无字面交集 —— 供人眼复核「是否题目未问」。
            no_ev = points_without_evidence(r)
            if no_ev:
                advice.append(
                    f"{where}: 要点与 gold 片段无字面交集，请复核是否为「题目未直接询问」的额外要点"
                    f" —— {no_ev}"
                )

    # 档位分布：任务书风险清单要求用例有区分度，三档都要有。
    # 仅对主用例集要求；专项难度题集（多跳）照常记录分布、但不要求覆盖三档 —— 见常量区说明。
    dist: dict[str, int] = {}
    for r in rows:
        dist[r.get("difficulty", "?")] = dist.get(r.get("difficulty", "?"), 0) + 1
    if path.name in DIFFICULTY_SPREAD_SUITES:
        for d in sorted(DIFFICULTIES):
            if not dist.get(d):
                problems.append(f"{path.name}: {d} 档没有用例，结果无法反映水平分层")

    print(f"{path.name:<24}{len(rows):>3} 条   档位分布 {dist}")
    if advice:
        print(f"  ⚠️ 提示 {len(advice)} 条（不拦截，供人眼复核；派生型答案属正常）：")
        for a in advice:
            print("     ·", a)
    return problems


def main() -> int:
    all_problems: list[str] = []
    for name in CASE_FILES:
        all_problems.extend(validate(CASES / name))
    print()
    if all_problems:
        print(f"发现 {len(all_problems)} 个问题：")
        for p in all_problems:
            print("  -", p)
        return 1
    print("用例结构校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
