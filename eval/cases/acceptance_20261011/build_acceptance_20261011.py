#!/usr/bin/env python
"""生成 acceptance_20261011 的三份分离文件（对齐执行单 3.1 命名）：
    nl2sql_questions.jsonl / nl2sql_reference.jsonl / nl2sql_sources.md。

执行单约束（10-11 组员测试执行_C 3.1）：
  · 题目、参考结果、来源 **三文件物理分离**；
  · `nl2sql_questions.jsonl` 只含**唯一编号、task 和原始问题**，不含任何答案 / 参考 / 判据，
    可直接喂给采集器或接口；
  · `nl2sql_reference.jsonl` 的参考结果由**真实执行 gold_sql** 得到，不手写；
  · `nl2sql_sources.md` 记录数据表 / 字段依据、题目覆盖点、编题与核验人；
  · 写盘走 staging 临时文件 + os.replace 原子替换，避免留下半成品。

用法：
    .venv/Scripts/python.exe eval/cases/acceptance_20261011/build_acceptance_20261011.py
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SCHEMA_META = ROOT / "infra" / "schema_meta.json"

#: 登记用赛题任务号：本批为「任务 1（要素识别与改写）/ 单表问数候选题」。
#: 采集器/接口只使用 question；task 仅作题目来源登记，不参与请求体。
TASK_ID = 1

# 每题字段：id / difficulty / question / tables / answer_type / expected_columns
#           / column_types(可选) / gold_sql / columns / basis / notes
CASES: list[dict] = [
    {
        "id": "acc-01", "difficulty": "easy", "answer_type": "scalar",
        "question": "平台上一共收录了多少位艺术家？",
        "tables": ["artist"], "columns": ["artist_id"], "expected_columns": ["artist_count"],
        "basis": "单表 COUNT(*)，基础 NL2SQL 单表查询（评分表功能指标 1）。",
        "gold_sql": "SELECT count(*) AS artist_count FROM artist;",
        "notes": "最基础的单表计数。与已有 nl2sql-e-04（员工计数）同型不同表，用于覆盖 artist。",
    },
    {
        "id": "acc-02", "difficulty": "easy", "answer_type": "single_column",
        "question": "把平台支持的所有媒体类型名称列出来，按名称升序排列。",
        "tables": ["media_type"], "columns": ["name"], "expected_columns": ["name"],
        "basis": "单表全列 + 排序，结果小且确定。",
        "gold_sql": "SELECT name FROM media_type ORDER BY name;",
        "notes": "媒体类型只有 5 种，结果稳定；考『媒体类型』到 media_type 表的定位。",
    },
    {
        "id": "acc-03", "difficulty": "easy", "answer_type": "scalar",
        "question": "标准单价为 0.99 的曲目一共有多少首？",
        "tables": ["track"], "columns": ["unit_price"], "expected_columns": ["cheap_track_count"],
        "basis": "单表等值过滤 COUNT。",
        "gold_sql": "SELECT count(*) AS cheap_track_count FROM track WHERE unit_price = 0.99;",
        "notes": "数据里单价只有 0.99 与 1.99 两档，本题锁定 0.99 一档，与 acc-09 互为呼应但答案不同。",
    },
    {
        "id": "acc-04", "difficulty": "easy", "answer_type": "scalar",
        "question": "全平台时长最长的一首曲目，时长是多少毫秒？",
        "tables": ["track"], "columns": ["milliseconds"], "expected_columns": ["longest_ms"],
        "basis": "单表 MAX 聚合。",
        "gold_sql": "SELECT max(milliseconds) AS longest_ms FROM track;",
        "notes": "挖空 min/max 中的 max；单位是毫秒，题面已写明，避免答成秒。",
    },
    {
        "id": "acc-05", "difficulty": "easy", "answer_type": "scalar",
        "question": "平台上一共有多少个播放列表？",
        "tables": ["playlist"], "columns": ["playlist_id"], "expected_columns": ["playlist_count"],
        "basis": "单表 COUNT(*)。",
        "gold_sql": "SELECT count(*) AS playlist_count FROM playlist;",
        "notes": "playlist 与 playlist_track 易混，考对『播放列表本身』与『关联表』的区分。",
    },
    {
        "id": "acc-06", "difficulty": "easy", "answer_type": "table",
        "question": "请按客户姓氏的字母顺序升序列出前 10 位客户的姓和名。",
        "tables": ["customer"], "columns": ["last_name", "first_name"], "expected_columns": ["last_name", "first_name"],
        "basis": "单表 ORDER BY + LIMIT，多列投影。",
        "gold_sql": "SELECT last_name, first_name FROM customer ORDER BY last_name LIMIT 10;",
        "notes": "列名以『姓、名』顺序点名，避免列序歧义；LIMIT 10 前已确认第 10/11 名姓氏不同，无并列边界。",
    },
    {
        "id": "acc-07", "difficulty": "easy", "answer_type": "scalar",
        "question": "金额最高的一张发票，总金额是多少？",
        "tables": ["invoice"], "columns": ["total"], "expected_columns": ["max_invoice_total"],
        "column_types": ["money"],
        "basis": "单表 MAX 聚合，金额语义（口径 2.3 容差 0.01）。",
        "gold_sql": "SELECT max(total) AS max_invoice_total FROM invoice;",
        "notes": "结果列标 money，用于验证金额列容差路径。",
    },
    {
        "id": "acc-08", "difficulty": "medium", "answer_type": "table",
        "question": "按客户所在国家统计客户数量，列出客户数最多的 5 个国家及其客户数；客户数相同时按国家名称升序排列。",
        "tables": ["customer"], "columns": ["country", "customer_id"], "expected_columns": ["country", "customer_count"],
        "basis": "单表 GROUP BY + ORDER BY + LIMIT。",
        "gold_sql": "SELECT country, count(*) AS customer_count FROM customer GROUP BY country ORDER BY customer_count DESC, country LIMIT 5;",
        "notes": "已在题面写死并列次级排序键；第 5 名边界已核（无并列）。",
    },
    {
        "id": "acc-09", "difficulty": "medium", "answer_type": "table",
        "question": "曲目有几种标准单价？每种单价各有多少首曲目？按单价升序排列，给出单价和曲目数。",
        "tables": ["track"], "columns": ["unit_price"], "expected_columns": ["unit_price", "track_count"],
        "column_types": ["money", "number"],
        "basis": "单表 GROUP BY，按金额列分组。",
        "gold_sql": "SELECT unit_price, count(*) AS track_count FROM track GROUP BY unit_price ORDER BY unit_price;",
        "notes": "只有 2 组结果，区分度有限，定位 easy 与 hard 之间的中间档；金额列标 money。",
    },
    {
        "id": "acc-10", "difficulty": "medium", "answer_type": "table",
        "question": "列出所有职位为『Sales Support Agent』的员工的姓、名和联系电话，按姓氏升序排列。",
        "tables": ["employee"], "columns": ["last_name", "first_name", "phone", "title"], "expected_columns": ["last_name", "first_name", "phone"],
        "basis": "单表等值过滤 + 多列投影 + 排序。",
        "gold_sql": "SELECT last_name, first_name, phone FROM employee WHERE title = 'Sales Support Agent' ORDER BY last_name;",
        "notes": "常量取自 employee.title 的真实取值枚举（见 schema_meta），不需要猜；3 行结果。",
    },
    {
        "id": "acc-11", "difficulty": "medium", "answer_type": "table",
        "question": "统计每位作曲者名下的曲目数量（作曲者为空的跳过不计），列出曲目数最多的 5 位作曲者及其曲目数；曲目数相同时按作曲者名称升序排列。",
        "tables": ["track"], "columns": ["composer", "track_id"], "expected_columns": ["composer", "track_count"],
        "basis": "单表 GROUP BY + NULL 排除 + LIMIT。",
        "gold_sql": "SELECT composer, count(*) AS track_count FROM track WHERE composer IS NOT NULL GROUP BY composer ORDER BY track_count DESC, composer LIMIT 5;",
        "notes": "NULL 处理是考点：不排除空作曲者会多出一行占位统计。",
    },
    {
        "id": "acc-12", "difficulty": "medium", "answer_type": "scalar",
        "question": "有多少位客户没有登记传真号码（fax 为空）？",
        "tables": ["customer"], "columns": ["fax"], "expected_columns": ["missing_fax_count"],
        "basis": "单表 NULL 判定 COUNT。",
        "gold_sql": "SELECT count(*) AS missing_fax_count FROM customer WHERE fax IS NULL;",
        "notes": "考 IS NULL；误写成 fax = '' 会得到 0。",
    },
    {
        "id": "acc-13", "difficulty": "medium", "answer_type": "table",
        "question": "按账单国家统计发票数量，只保留发票数达到 20 张及以上的国家；给出国家名称和发票数，按发票数从多到少、国家名称升序排列。",
        "tables": ["invoice"], "columns": ["billing_country", "invoice_id"], "expected_columns": ["billing_country", "invoice_count"],
        "basis": "单表 GROUP BY + HAVING。",
        "gold_sql": "SELECT billing_country, count(*) AS invoice_count FROM invoice GROUP BY billing_country HAVING count(*) >= 20 ORDER BY invoice_count DESC, billing_country;",
        "notes": "阈值 20 由真实分布选定（结果 6 行）；HAVING 过滤分组后结果。",
    },
    {
        "id": "acc-14", "difficulty": "medium", "answer_type": "scalar",
        "question": "所有发票明细行的购买数量加起来一共是多少？",
        "tables": ["invoice_line"], "columns": ["quantity"], "expected_columns": ["total_quantity"],
        "basis": "单表明细表 SUM 聚合。",
        "gold_sql": "SELECT sum(quantity) AS total_quantity FROM invoice_line;",
        "notes": "invoice_line 是明细表，2240 行、每行 quantity=1，合计也是 2240——可用来分辨『答成行数』与『答成数量和』。",
    },
    {
        "id": "acc-15", "difficulty": "hard", "answer_type": "table",
        "question": "找出名称重复的曲目：列出出现次数最多的 10 个重复曲目名称以及各自出现的次数，按出现次数从多到少、名称升序排列。",
        "tables": ["track"], "columns": ["name"], "expected_columns": ["name", "name_count"],
        "basis": "单表 GROUP BY + HAVING + LIMIT，重复值检测。",
        "gold_sql": "SELECT name, count(*) AS name_count FROM track GROUP BY name HAVING count(*) > 1 ORDER BY name_count DESC, name LIMIT 10;",
        "notes": "全量重复名有 199 个，直接全列会撑爆结果；取 top10 后边界干净——出现次数≥4 的恰好 10 组，第 11 名降到 3 次，无并列歧义。",
    },
    {
        "id": "acc-16", "difficulty": "hard", "answer_type": "table",
        "question": "按账单国家统计平均发票金额，只保留平均金额高于 6 的国家；给出国家名称和平均金额（保留两位小数），按平均金额从高到低、国家名称升序排列。",
        "tables": ["invoice"], "columns": ["billing_country", "total"], "expected_columns": ["billing_country", "avg_total"],
        "column_types": ["text", "money"],
        "basis": "单表 GROUP BY + HAVING(avg) + 金额四舍五入。",
        "gold_sql": "SELECT billing_country, round(avg(total), 2) AS avg_total FROM invoice GROUP BY billing_country HAVING avg(total) > 6 ORDER BY avg_total DESC, billing_country;",
        "notes": "阈值 6 由真实分布选定：24 国平均金额落在 5.37~6.66，>6 得 5 行；其中 Hungary 与 Ireland 均 6.52，靠次级键国家名定序。",
    },
    {
        "id": "acc-17", "difficulty": "hard", "answer_type": "table",
        "question": "把曲目时长从毫秒换算成秒（保留一位小数），列出时长最长的 10 首曲目的名称和秒数，按秒数从长到短、曲目名称升序排列。",
        "tables": ["track"], "columns": ["name", "milliseconds"], "expected_columns": ["name", "seconds"],
        "column_types": ["text", "number"],
        "basis": "单表计算列 + 单位换算 + 排序 + LIMIT。",
        "gold_sql": "SELECT name, round(milliseconds / 1000.0, 1) AS seconds FROM track ORDER BY milliseconds DESC, name LIMIT 10;",
        "notes": "考单位换算（毫秒→秒）与除法取整：直接用整数除法会得到 0。",
    },
    {
        "id": "acc-18", "difficulty": "hard", "answer_type": "table",
        "question": "统计每位管理者直接管理了几名下属（没有下属的人不出现在结果里）；给出管理者的员工编号和直接下属人数，按下属人数从多到少、员工编号升序排列。",
        "tables": ["employee"], "columns": ["reports_to", "employee_id"], "expected_columns": ["manager_id", "direct_reports"],
        "basis": "单表自关联字段分组（employee 自关联，见 schema_meta）。",
        "gold_sql": "SELECT reports_to AS manager_id, count(*) AS direct_reports FROM employee WHERE reports_to IS NOT NULL GROUP BY reports_to ORDER BY direct_reports DESC, manager_id;",
        "notes": "employee 表 self-join 语义外的另一条路：直接对 reports_to 分组。最强管理者（一人管 3 名）与两名各管 2 名。",
    },
    {
        "id": "acc-19", "difficulty": "hard", "answer_type": "scalar",
        "question": "在全部曲目中，作曲者为空的曲目占比是多少？以百分比表示，保留一位小数。",
        "tables": ["track"], "columns": ["composer"], "expected_columns": ["null_composer_pct"],
        "basis": "单表条件占比（CASE WHEN 计数 / 总数）。",
        "gold_sql": "SELECT round(100.0 * sum(CASE WHEN composer IS NULL THEN 1 ELSE 0 END) / count(*), 1) AS null_composer_pct FROM track;",
        "notes": "考条件计数与百分比换算；写成整数除法会丢小数。",
    },
    {
        "id": "acc-20", "difficulty": "hard", "answer_type": "scalar",
        "question": "有多少张发票的金额高于所有发票的平均金额？",
        "tables": ["invoice"], "columns": ["total"], "expected_columns": ["above_avg_invoice_count"],
        "basis": "单表标量子查询比较。",
        "gold_sql": "SELECT count(*) AS above_avg_invoice_count FROM invoice WHERE total > (SELECT avg(total) FROM invoice);",
        "notes": "考标量子查询；直接用 avg 比较会语法错或语义错。注意全局平均 5.65、结果 179 张。",
    },
]


def load_dsn() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("未在 .env 找到 DATABASE_URL")


def load_table_meta() -> dict[str, dict]:
    meta = json.loads(SCHEMA_META.read_text(encoding="utf-8"))
    return {t["table"]: t for t in meta["tables"]}


def jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    return v


def atomic_write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def render_sources_md(sources: list[dict]) -> str:
    lines: list[str] = []
    lines.append("# acceptance_20261011 单表问数候选题 —— 来源与覆盖说明")
    lines.append("")
    lines.append("- 编题与核验：C（数据库 / 问数线）")
    lines.append("- 数据来源：Chinook（容器 `ict-db`，`127.0.0.1:5432/chinook`），题目以**当前实际数据**为准")
    lines.append("- 元数据依据：`infra/schema_meta.json` 的表 / 字段描述")
    lines.append("- 覆盖点：清晰过滤、计数 / 求和、分组、排序 / 前 N、空结果、相似字段干扰、NULL 处理、"
                 "单位换算、占比与子查询")
    lines.append("- 规模：**内部候选 20 道**（赛题要求至少 10 个单表用例；20 为团队内部建议）")
    lines.append("- 参考结果：由 `nl2sql_reference.jsonl` 的 `gold_sql` 在真实库执行得到，非手写")
    lines.append("- 与既有题库关系：与 `eval/cases/nl2sql.jsonl` 的 8 道单表题**零重复**")
    lines.append("")
    lines.append("> 说明：`nl2sql_questions.jsonl` 只含 `id` / `task` / `question`，可直接喂给采集器或接口；"
                 "参考结果与判据只在采集封存后由核验方读取，**不得进入 prompt、请求体或服务端**。")
    lines.append("")
    lines.append("## 逐题依据")
    lines.append("")
    lines.append("| id | 难度 | 表 | 用到的列 | 表行数 | 结果列 | 依据 |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in sources:
        cols = " / ".join(f"`{c}`" for c in s["columns"])
        rcols = " / ".join(f"`{c}`" for c in s["result_columns"]) or "-"
        rows = s["table_rows"] if s["table_rows"] is not None else "-"
        lines.append(
            f"| {s['id']} | {s['difficulty']} | `{s['table']}` | {cols} | {rows} | {rcols} | {s['basis']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    dsn = load_dsn()
    tmeta = load_table_meta()

    questions, references, sources = [], [], []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for c in CASES:
            cur.execute(c["gold_sql"])
            rows = [[jsonable(v) for v in row] for row in cur.fetchall()]
            desc = [d[0] for d in cur.description]

            # 题目文件：只允许 id / task / question（执行单 3.1）
            q = {"id": c["id"], "task": TASK_ID, "question": c["question"]}

            ref = {
                "id": c["id"], "gold_sql": c["gold_sql"],
                "expected_columns": c["expected_columns"],
                "result_type": c["answer_type"], "row_count": len(rows),
                "gold_result": rows, "notes": c["notes"],
            }
            if "column_types" in c:
                ref["column_types"] = c["column_types"]

            t = c["tables"][0]
            sources.append({
                "id": c["id"], "difficulty": c["difficulty"], "table": t,
                "columns": c["columns"],
                "table_description": tmeta.get(t, {}).get("description", ""),
                "table_rows": tmeta.get(t, {}).get("row_count"),
                "result_columns": desc, "basis": c["basis"],
                "question": c["question"],
            })
            questions.append(q)
            references.append(ref)

    atomic_write(OUT / "nl2sql_questions.jsonl", questions)
    atomic_write(OUT / "nl2sql_reference.jsonl", references)
    atomic_write_text(OUT / "nl2sql_sources.md", render_sources_md(sources))

    # ---- 自校 ----
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "id 重复"
    allowed = {"id", "task", "question"}
    leak = [(q["id"], sorted(set(q) - allowed)) for q in questions if set(q) - allowed]
    assert not leak, f"题目文件出现越权字段：{leak}"
    dist = {}
    for q, s in zip(questions, sources):
        dist[s["difficulty"]] = dist.get(s["difficulty"], 0) + 1

    print(f"写出 {len(questions)} 条用例 -> {OUT}")
    for name in ("nl2sql_questions.jsonl", "nl2sql_reference.jsonl", "nl2sql_sources.md"):
        n = sum(1 for _ in (OUT / name).open(encoding="utf-8"))
        print(f"  {name:24s} {n} 行")
    print(f"难度分布 {dist}")
    print(f"题目文件越权字段检查：{'通过（仅 id/task/question）' if not leak else leak}")
    print("参考结果规模：")
    for ref in references:
        head = ref["gold_result"][0] if ref["gold_result"] else None
        print(f"  {ref['id']:8s} rows={ref['row_count']:>3}  head={head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
