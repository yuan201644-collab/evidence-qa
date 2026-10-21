"""政采跨源融合题判分器（按答案点给分）。

## 与 eval/cross_source_runner.py 的区别

`eval/cross_source_runner.py` 判的是**另一套**留出题：它调用 `/api/ask`
的跨源演示链路，读 trace 里的 `cross_source_alignment` / `cross_source_sql`
工具输出，比对证据定位与缺项。那套题跑的是工单—合同这类演示场景。

本文件判的是政采题集：库里是 2742 份真实采购公告建成的 11 张表，
文档侧是 24 份法规 + 200 份公告原文。两者题库、链路、判据都不同，
所以分开两个文件，不合并。

## 判分口径

跨源题不能比结果集，因为答案里除了数字还有条文和判断。
这里按 `answer_points` 逐条判二元命中。

## 为什么要分「库侧」与「文档侧」

只报总分看不出跨源能力有没有真训出来。所以每题同时检查：
  - `db_expect`：答案里有没有库内事实（数字、项目名、网址）
  - `doc_expect`：答案里有没有文档条文要素（期限、批准机关、法条用语）

只答出库侧、文档侧全空的答案，说明系统只是查了数没查文档 ——
那和单向问数没区别。分项报出来才能看见这件事。

## 判据的性质（必须在报告里写明）

判定是**关键词子串匹配**，不是语义判定。
答对了但换了个说法，会被判未命中。所以本判分器的分数是**下界**。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "eval" / "cases" / "gov_procurement" / "cross_source.jsonl"


def load_cases() -> list[dict]:
    with CASES.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_predictions(path: Path) -> dict[str, dict]:
    table: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            table[row["id"]] = row
    return table


def _norm(text: str) -> str:
    """归一化：去空白、全角标点转半角。

    不这么做的话，`96.00%` 与 `96.00 %`、`5 个工作日` 与 `5个工作日`
    都会被判成不一样，而这些显然是同一个意思。
    """
    text = text.replace("，", ",").replace("。", ".").replace("：", ":")
    text = text.replace("；", ";").replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", text)


def _hit_number(raw_answer: str, token: str) -> bool:
    """数字命中：允许千分位与尾部多余零。"""
    answer = raw_answer.replace(",", "").replace("，", "")
    digits = re.sub(r"[^\d.]", "", token)
    if not digits:
        return False
    candidates = {digits}
    if "." in digits:
        candidates.add(digits.rstrip("0").rstrip("."))
    for candidate in candidates:
        if candidate and candidate in answer:
            return True
    return False


def _hit(answer_norm: str, raw_answer: str, token: str) -> bool:
    """一个要素是否命中。数字走宽松匹配，文字走归一化子串。"""
    if not token:
        return True
    if re.fullmatch(r"[\d.,%]+", token):
        return _hit_number(raw_answer, token)
    token_norm = _norm(token)
    if token_norm in answer_norm:
        return True
    # 并列多个**数字**的要点：必须全部命中（2026-10-17 新增）。
    # 实测起因：要点 `3749507.09、3200000.00`（两个项目预算）只答对一个也被
    # 判命中——它落进了下面「分片过半」的规则（2 片中 1 片 = 50%）。
    # 但少一个金额就是答错了一个项目，不能算一半。
    # 所以多数字要点单独走全等判定，且**排在分片过半之前**。
    numbers_in_token = re.findall(r"\d+(?:\.\d+)?", token_norm)
    if len(numbers_in_token) >= 2:
        return all(_hit_number(raw_answer, n) for n in numbers_in_token)
    # 长要素（带顿号或分号的复合短语）按分片过半判定，
    # 否则一个「A、B、C」形式的要素会因为少写其中一项就整条判负，
    # 那会把「答了大部分」和「完全没答」混为一谈。
    pieces = [p for p in re.split(r"[、,;]", token_norm) if len(p) >= 2]
    if len(pieces) >= 2:
        hits = sum(1 for p in pieces if p in answer_norm)
        # 严格过半，不是「至少半数」（2026-10-17 修）。
        # 原写法 hits*2 >= len(pieces) 在 2 片时等于「中 1 片就算过」，
        # 于是并列两个实体只答对一个也判命中。实测 A02 的
        # `怀远县、香港大学深圳医院` 只答出怀远县仍判过，而同一条规则的
        # 本意是「答了大部分算过」，答一半不是大部分。
        return hits * 2 > len(pieces)
    # 「带单位的数字」要点（2026-10-17 新增）。
    # 实测起因：答案点写成「691 条」「5 个工作日」「297 个」时一直判不中，
    # 而答案原文里明明写着 `announcement_count：691`、`期限为5个工作日`。
    # 根因是这类要点既不是纯数字（会走 `[\d.,%]+` 分支），也不是纯文字——
    # 它要求数字与单位**紧邻**出现，但答案里数字与单位常常被结论模板的
    # 分隔符切开（`count：691、`），于是归一化子串永远匹配不上。
    # 处理：只要要点里的**数字**在答案中出现，且要点里的**中文词**也至少
    # 出现一个，就算命中。单位本身（条/个/工作日）不作硬要求——
    # 用户写「共 691 项」和「691 条」是同一件事，不该因为量词不同判错。
    numbers = re.findall(r"\d+(?:\.\d+)?", token)
    if numbers:
        cjk = [part for part in re.findall(r"[\u4e00-\u9fff]+", token_norm) if len(part) >= 1]
        number_hit = all(_hit_number(raw_answer, n) for n in numbers)
        if not cjk:
            return number_hit
        # 纯量词不构成实质要求：`691 条` / `691 个` / `691 项` 是同一件事。
        # 只有含**实义词**的要点（`5 个工作日`、`297 个项目`）才额外核对文字。
        content = [part for part in cjk if not _is_measure_word(part)]
        if not content:
            return number_hit
        word_hit = any(part in answer_norm for part in content)
        return number_hit and word_hit
    return False


# 量词表：只列「单独出现时不带实质语义」的计量单位。
# 刻意不含「工作日」「项目」「元」—— 那些是实质语义（`5 个工作日` 与 `5 天`
# 在法规语境下不同；`297 个项目` 与 `297 条公告` 也不同）。
_MEASURE_WORDS = frozenset(
    "个 条 项 笔 家 次 批 份 件 台 套 只 张 人 位 名 户 类 种 组 款 期 年 月 日".split()
)


def _is_measure_word(text: str) -> bool:
    """这段中文是不是纯量词（不含实义）。"""
    return text in _MEASURE_WORDS


def score_case(case: dict, answer: str) -> dict:
    answer_norm = _norm(answer)

    points = case["answer_points"]
    points_hit = sum(1 for p in points if _hit(answer_norm, answer, p))

    db_tokens = case.get("db_expect") or []
    db_hit = sum(1 for t in db_tokens if _hit(answer_norm, answer, t))
    doc_tokens = case.get("doc_expect") or []
    doc_hit = sum(1 for t in doc_tokens if _hit(answer_norm, answer, t))

    return {
        "id": case["id"],
        "difficulty": case["difficulty"],
        "category": case["category"],
        "points_total": len(points),
        "points_hit": points_hit,
        "db_total": len(db_tokens),
        "db_hit": db_hit,
        "doc_total": len(doc_tokens),
        "doc_hit": doc_hit,
        "both_sides": bool(db_hit) and bool(doc_hit),
        "answer_chars": len(answer),
        # 单题通过线：答案点命中 >= 60%
        "kind": "pass" if points and points_hit * 10 >= len(points) * 6 else "fail",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="政采跨源融合题判分")
    parser.add_argument("--predictions", required=True, help="预测 jsonl，每行 {id, answer}")
    parser.add_argument("--tag", default="cross", help="报告标签")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cases = load_cases()
    predictions = load_predictions(Path(args.predictions))

    results: list[dict] = []
    missing: list[str] = []
    for case in cases:
        row = predictions.get(case["id"])
        if row is None:
            missing.append(case["id"])
            continue
        results.append(score_case(case, str(row.get("answer") or "")))

    total = len(cases)
    scored = len(results)
    passed = sum(1 for r in results if r["kind"] == "pass")
    sum_points = sum(r["points_total"] for r in results)
    sum_hit = sum(r["points_hit"] for r in results)
    db_total = sum(r["db_total"] for r in results)
    db_hit = sum(r["db_hit"] for r in results)
    doc_total = sum(r["doc_total"] for r in results)
    doc_hit = sum(r["doc_hit"] for r in results)
    both = sum(1 for r in results if r["both_sides"])

    payload = {
        "suite": "gov_cross",
        "tag": args.tag,
        "overall": {
            "total": total,
            "scored": scored,
            "missing": missing,
            "passed": passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "answer_points_total": sum_points,
            "answer_points_hit": sum_hit,
            "answer_points_rate": round(sum_hit / sum_points, 4) if sum_points else 0.0,
            "db_side_rate": round(db_hit / db_total, 4) if db_total else 0.0,
            "doc_side_rate": round(doc_hit / doc_total, 4) if doc_total else 0.0,
            "both_sides_cases": both,
        },
        "cases": results,
    }

    if not args.quiet:
        o = payload["overall"]
        print("=" * 64)
        print("跨源融合题：%d/%d 通过 = %.2f%%" % (passed, total, o["pass_rate"] * 100))
        print("答案点命中：%d/%d = %.2f%%"
              % (sum_hit, sum_points, o["answer_points_rate"] * 100))
        print("库侧要素：%.2f%%     文档侧要素：%.2f%%"
              % (o["db_side_rate"] * 100, o["doc_side_rate"] * 100))
        print("两边都有内容：%d/%d 题（只答出一边 = 跨源没做出来）" % (both, scored))
        if missing:
            print("缺预测：", ", ".join(missing))
        print("=" * 64)
        for r in results:
            mark = "√" if r["kind"] == "pass" else "×"
            print("  %s %-11s %-7s 点 %d/%d   库 %d/%d   文 %d/%d"
                  % (mark, r["id"], r["difficulty"],
                     r["points_hit"], r["points_total"],
                     r["db_hit"], r["db_total"], r["doc_hit"], r["doc_total"]))
        print()
        print("注意：判据是关键词子串匹配，换说法答对也判未命中 —— 分数是下界，不是准确率。")

    report = ROOT / "eval" / "reports" / ("run-cross-%s.json" % args.tag)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告：", report.relative_to(ROOT))


if __name__ == "__main__":
    main()
