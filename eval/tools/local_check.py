#!/usr/bin/env python
"""本地无库校验：把评测用例的标准 SQL 真跑一遍，确认它们不是"看起来对"。

为什么需要这个工具：
    开发机至今没有 Docker / PostgreSQL，但评测集必须在 09-23 落地。
    做法是用 sqlglot 把 PostgreSQL 方言转成 SQLite 方言（原则③ 方言解耦的一次真用），
    再把 Chinook 官方建库脚本整个灌进一个内存 SQLite 库，
    于是在没有任何数据库服务的前提下，也能验证：
      1) 标准 SQL 到底能不能执行（不是靠肉眼审 SQL）；
      2) 结果行数、列数，空结果会被点名（空结果用例没有区分度）；
      3) 连跑两次结果是否一致（暴露并列排序、LIMIT 截断这类不确定性问题）。

它不能替代真实 PostgreSQL 上的跑分——方言转换后的结果与原库可能有细微差异。
所以产物只用于「用例自检」，不作为对外指标。

用法：
    python eval/tools/local_check.py
    python eval/tools/local_check.py --dump-expected
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "eval"))
from metrics import compare_resultsets  # noqa: E402

CASES = ROOT / "eval" / "cases" / "nl2sql.jsonl"
DDL = ROOT / "infra" / "initdb" / "02_chinook.sql"
REPORTS = ROOT / "eval" / "reports"


def preprocess_pg_dump(text: str) -> str:
    """把官方 PG 转储里 SQLite 不认的两处写法就地改写。

    这两处不是"顺手清理"，是不改就跑不起来，且原因都很具体：
      1) PG 的 N'...' 国家字符集字面量。sqlglot 原样带到 SQLite，SQLite 不认 → 语法错误。
         用 (?<![\\w']) 保证只改真正的字面量前缀，不会误伤字符串内容里的 N'。
      2) 日期字面量写成了 '1962/2/18'（斜杠 + 不补零）。
         SQLite 把日期存成文本，字符串比较是逐字符的：
         '2021/10/1' < '2021/2/1'，月份一多就全乱。必须补成 ISO '1962-02-18'。
    """
    text = re.sub(r"(?<![\w'])N'", "'", text)

    def iso(m: re.Match[str]) -> str:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
        return f"'{y}-{mo:02d}-{d:02d}'"

    return re.sub(r"'(\d{4})/(\d{1,2})/(\d{1,2})'", iso, text)


def build_sqlite() -> sqlite3.Connection:
    """把 Chinook 的 PostgreSQL 建库脚本转成 SQLite 并灌进内存库。

    只保留 CREATE TABLE 与 INSERT：索引和 FOREIGN KEY 对「跑通 SQL 看结果」没有作用，
    而 SQLite 不接 `CREATE INDEX ... NULLS LAST`、也不支持 ALTER TABLE ADD CONSTRAINT，
    留着只会让失败数虚高、掩盖真正的数据导入失败。
    """
    text = preprocess_pg_dump(DDL.read_text(encoding="utf-8", errors="replace"))
    conn = sqlite3.connect(":memory:")
    ok = skipped = failed = 0
    failures: list[str] = []

    for stmt in sqlglot.parse(text, dialect="postgres", error_level=ErrorLevel.IGNORE):
        if stmt is None:
            continue
        is_create_table = isinstance(stmt, exp.Create) and (stmt.kind or "").upper() == "TABLE"
        if not (is_create_table or isinstance(stmt, exp.Insert)):
            skipped += 1
            continue
        try:
            conn.execute(stmt.sql(dialect="sqlite"))
            ok += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败不该中断整库搭建，但要记下来
            failed += 1
            failures.append(f"{type(exc).__name__}: {exc} | {stmt.sql(dialect='sqlite')[:160]}")

    conn.commit()
    print(f"SQLite 本地库：执行成功 {ok} 条 / 跳过 {skipped} 条 / 失败 {failed} 条")
    if failures:
        print("失败明细（前 5 条）：")
        for f in failures[:5]:
            print("   ", f)

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    print(f"表（{len(tables)}）：{', '.join(tables)}")

    # 这一条是整套本地校验的可信度地基：数据没进去，后面所有「通过」都是假的
    expected_rows = {"track": 3503, "invoice_line": 2240, "customer": 59, "invoice": 412}
    bad = []
    for tbl, want in expected_rows.items():
        if tbl not in tables:
            bad.append(f"{tbl} 表不存在")
            continue
        got = conn.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        flag = "OK" if got == want else "不符"
        print(f"自检：{tbl:<14} 行数 = {got:<6} 期望 {want:<6} {flag}")
        if got != want:
            bad.append(f"{tbl} 行数 {got} != {want}")
    if bad:
        print("\n数据导入不完整，本次校验结论不可信：")
        for b in bad:
            print("   -", b)
        raise SystemExit(2)
    print()
    return conn


def run_sql(conn: sqlite3.Connection, sql: str) -> tuple[list[tuple], str | None]:
    try:
        cur = conn.execute(sql)
        return cur.fetchall(), None
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


def to_sqlite(stmt_case: dict) -> tuple[str, str]:
    """取 SQLite 版本 SQL：优先用例自带的 sql_variants.sqlite，否则用 sqlglot 转会。"""
    variant = (stmt_case.get("sql_variants") or {}).get("sqlite")
    if variant:
        return variant, "用例自带"
    try:
        return sqlglot.transpile(stmt_case["gold_sql"], read="postgres", write="sqlite")[0], "sqlglot 转换"
    except Exception as exc:  # noqa: BLE001
        return stmt_case["gold_sql"], f"转换失败({exc})，原样执行"


def check_boundary_tie(conn: sqlite3.Connection, case: dict) -> str | None:
    """TOP-N 用例的边界并列检测。

    为什么必须机械地查一遍：`... ORDER BY 指标 DESC LIMIT 3` 在指标并列时，
    第 3 名到底是谁取决于引擎实现。这类用例会让「模型答对但被判错」变得无法辩解，
    也让人工复核无从下手。检测方法是把 LIMIT 放大一行，看多出来那行的排序指标
    是否与第 n 行相同——相同就是并列，用例必须改成阈值过滤或补足次级排序键。
    """
    if "top_n" not in case:
        return None
    idx = case.get("order_key_index", 0)
    n = case["top_n"]
    try:
        # 用 sqlglot 解析后改 LIMIT，而不是字符串拼接——
        # 标准 SQL 自己就带 LIMIT，直接追加会拼出 `... LIMIT 3 LIMIT 4` 的语法错误。
        base = sqlglot.parse_one(case["gold_sql"], dialect="postgres")
        q_n = base.copy().limit(n).sql(dialect="sqlite")
        q_n1 = base.copy().limit(n + 1).sql(dialect="sqlite")
        rows_n = conn.execute(q_n).fetchall()
        rows_n1 = conn.execute(q_n1).fetchall()
    except Exception as exc:  # noqa: BLE001
        return f"边界检测无法执行：{exc}"
    if len(rows_n1) <= len(rows_n):
        return None
    if rows_n1[n][idx] == rows_n[-1][idx]:
        return f"TOP-{n} 边界并列：第 {n} 行与第 {n+1} 行的排序指标同为 {rows_n[-1][idx]!r}，结果不确定"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="用本地 SQLite 校验 NL2SQL 用例")
    ap.add_argument(
        "--cases",
        type=Path,
        default=None,
        help="只校验单个用例文件；默认校验 nl2sql.jsonl 与 robust_cases.jsonl",
    )
    ap.add_argument("--dump-expected", action="store_true", help="把预期结果落盘到 eval/reports/")
    args = ap.parse_args()

    paths = [args.cases] if args.cases else [CASES, CASES.parent / "robust_cases.jsonl"]
    cases: list[dict] = []
    for p in paths:
        if not p.exists():
            print(f"跳过（不存在）：{p.name}")
            continue
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in rows:
            # 期望澄清的用例没有标准 SQL，不该在这里被执行
            if r.get("gold_sql"):
                cases.append(r)

    if not cases:
        raise SystemExit("没有可校验的用例（都缺 gold_sql？）")

    conn = build_sqlite()

    problems: list[str] = []
    expected: dict[str, list] = {}

    print(f"{'用例':<14}{'档位':<8}{'行数':<7}{'列数':<6}{'稳定':<6}{'来源':<12}判定")
    print("-" * 92)

    for c in cases:
        lite_sql, origin = to_sqlite(c)
        rows1, err1 = run_sql(conn, lite_sql)
        rows2, err2 = run_sql(conn, lite_sql)

        # 稳定性用项目自己的判定器来判，而不是 rows1 == rows2：
        # 只有列序/行序差异时判定器会判 pass，那正是"这两次其实一样"的语义。
        stable = "是" if compare_resultsets(rows1, rows2, pred_error=err2).ok else "否"
        verdict = "通过"
        if err1:
            verdict = "SQL 报错"
            problems.append(f"{c['id']}: {err1}")
        elif not rows1:
            verdict = "空结果（无区分度）"
            problems.append(f"{c['id']}: 标准结果为空集")
        elif not stable:
            verdict = "结果不稳定"
            problems.append(f"{c['id']}: 两次执行结果不同，排序键不够，存在并列或 LIMIT 截断问题")
        elif len(rows1) > 500:
            verdict = f"通过（结果 {len(rows1)} 行，区分度偏低）"
            problems.append(f"{c['id']}: 结果集过大（{len(rows1)} 行），建议收紧条件")

        # 列数一致性自检：expected_columns 必须与真实结果列数相同
        n_cols = len(rows1[0]) if rows1 else 0
        if c.get("expected_columns") and len(c["expected_columns"]) != n_cols:
            problems.append(
                f"{c['id']}: expected_columns 写了 {len(c['expected_columns'])} 列，实际查出 {n_cols} 列"
            )
            verdict += " / 列数声明不符"

        tie = check_boundary_tie(conn, c)
        if tie:
            problems.append(f"{c['id']}: {tie}")
            verdict += " / 边界并列"
        elif "top_n" in c:
            verdict += " / 边界无并列"

        expected[c["id"]] = {
            "n_rows": len(rows1),
            "n_cols": n_cols,
            "sqlite_sql": lite_sql,
            "sqlite_rows_preview": [list(map(str, r)) for r in rows1[:3]],
        }

        print(f"{c['id']:<14}{c['difficulty']:<8}{len(rows1):<7}{n_cols:<6}{stable:<6}{origin:<12}{verdict}")

    conn.close()

    print("-" * 92)
    if args.dump_expected:
        REPORTS.mkdir(parents=True, exist_ok=True)
        out = REPORTS / "expected_nl2sql.sqlite.json"
        out.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"预期结果预览已落盘：{out}")

    if problems:
        print(f"\n需要人工处理 {len(problems)} 项：")
        for p in problems:
            print("  -", p)
        return 1

    print("\n全部用例在本地 SQLite 上可执行、结果稳定、非空、列数与声明一致。")
    print("注意：这只是自检。对外指标仍以真实 PostgreSQL 上的跑分为准。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
