"""建库质量验收：行数、外键完整性、字段质量、可出题性。

《技术实现说明书》7.1 要的规模与复杂度数据全部从这里出，不手填。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import psycopg  # noqa: E402

DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"
OUT = Path(__file__).resolve().parents[2] / "agent_temp" / "pb" / "probe_ccgp"
REPORT = OUT / "db_acceptance.json"

TABLES = ["regions", "buyers", "agencies", "suppliers", "projects",
          "announcements", "packages", "items", "awards", "documents", "regulation_rules"]


def main() -> None:
    result: dict = {"tables": {}, "checks": {}, "quality": {}}
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        print("=== 一、行数 ===")
        total = 0
        for table in TABLES:
            cur.execute(f"SELECT count(*) FROM {table}")
            count = cur.fetchone()[0]
            result["tables"][table] = count
            if table != "documents":
                total += count
            print(f"  {table:<18} {count:>7}")
        result["tables"]["_structured_total_excl_documents"] = total
        print(f"  {'结构化合计(不含documents)':<18} {total:>7}")
        print(f"  {'含 documents':<18} {total + result['tables']['documents']:>7}")

        print()
        print("=== 二、外键完整性 ===")
        fk_checks = [
            ("announcements 挂在有效项目上",
             "SELECT count(*) FROM announcements a LEFT JOIN projects p USING(project_id) WHERE p.project_id IS NULL"),
            ("awards 供应商有效",
             "SELECT count(*) FROM awards a LEFT JOIN suppliers s USING(supplier_id) WHERE s.supplier_id IS NULL"),
            ("awards 项目有效",
             "SELECT count(*) FROM awards a LEFT JOIN projects p USING(project_id) WHERE p.project_id IS NULL"),
            ("items 公告有效",
             "SELECT count(*) FROM items i LEFT JOIN announcements a USING(announcement_id) WHERE i.announcement_id IS NOT NULL AND a.announcement_id IS NULL"),
            ("projects.buyer_id 有效",
             "SELECT count(*) FROM projects p LEFT JOIN buyers b USING(buyer_id) WHERE p.buyer_id IS NOT NULL AND b.buyer_id IS NULL"),
            ("projects.agency_id 有效",
             "SELECT count(*) FROM projects p LEFT JOIN agencies g USING(agency_id) WHERE p.agency_id IS NOT NULL AND g.agency_id IS NULL"),
            ("documents 项目有效",
             "SELECT count(*) FROM documents d LEFT JOIN projects p USING(project_id) WHERE p.project_id IS NULL"),
        ]
        for label, sql in fk_checks:
            cur.execute(sql)
            broken = cur.fetchone()[0]
            result["checks"][label] = broken
            print(f"  {'OK ' if broken == 0 else 'BAD'} {label:<32} 断裂 {broken}")

        print()
        print("=== 三、字段质量 ===")
        quality_queries = [
            ("projects 有采购方式", "SELECT count(*) FROM projects WHERE procurement_way IS NOT NULL"),
            ("projects 有预算", "SELECT count(*) FROM projects WHERE budget_yuan IS NOT NULL"),
            ("projects 名称非空且非占位", "SELECT count(*) FROM projects WHERE project_name NOT IN ('(无项目名)','')"),
            ("announcements 有发布日期", "SELECT count(*) FROM announcements WHERE publish_date IS NOT NULL"),
            ("announcements 带品目明细", "SELECT count(*) FROM announcements WHERE has_items"),
            ("awards 有金额", "SELECT count(*) FROM awards WHERE award_yuan IS NOT NULL"),
            ("awards 有评分", "SELECT count(*) FROM awards WHERE review_score IS NOT NULL"),
            ("items 有单价", "SELECT count(*) FROM items WHERE unit_price_yuan IS NOT NULL"),
            ("items 有数量", "SELECT count(*) FROM items WHERE quantity IS NOT NULL"),
            ("items 有品牌", "SELECT count(*) FROM items WHERE brand IS NOT NULL AND brand <> ''"),
        ]
        for label, sql in quality_queries:
            cur.execute(sql)
            result["quality"][label] = cur.fetchone()[0]
            print(f"  {label:<32} {result['quality'][label]:>7}")

        print()
        print("=== 四、规模与复杂度（写报告用）===")
        cur.execute("""
            SELECT table_name, count(*) AS cols,
                   count(DISTINCT data_type) AS types
            FROM information_schema.columns
            WHERE table_schema='public'
            GROUP BY table_name ORDER BY table_name
        """)
        col_stats = cur.fetchall()
        total_cols = sum(row[1] for row in col_stats)
        all_types: set[str] = set()
        for _, _, _ in col_stats:
            pass
        cur.execute("SELECT DISTINCT data_type FROM information_schema.columns WHERE table_schema='public'")
        all_types = {row[0] for row in cur.fetchall()}
        print(f"  表数 {len(TABLES)} / 字段总数 {total_cols} / 数据类型种类 {len(all_types)}")
        print(f"  数据类型：{'、'.join(sorted(all_types))}")
        result["schema"] = {"columns": total_cols, "data_types": sorted(all_types)}

        cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname='public'")
        index_count = cur.fetchone()[0]
        result["schema"]["indexes"] = index_count
        print(f"  索引数 {index_count}")

        print()
        print("=== 五、可出题性检查 ===")
        cur.execute("""
            SELECT count(*) FROM projects p
            WHERE EXISTS (SELECT 1 FROM announcements a WHERE a.project_id=p.project_id AND a.kind IN ('中标公告','成交公告'))
        """)
        awarded_projects = cur.fetchone()[0]
        print(f"  有中标结果的项目 {awarded_projects}")

        cur.execute("""
            SELECT count(*) FROM (
                SELECT project_id FROM announcements GROUP BY project_id HAVING count(*) >= 2
            ) t
        """)
        multi_ann = cur.fetchone()[0]
        print(f"  有 2 条以上公告的项目 {multi_ann}（可出跨公告题）")

        cur.execute("""
            SELECT count(*) FROM projects p
            WHERE EXISTS (SELECT 1 FROM announcements a WHERE a.project_id=p.project_id AND a.has_items)
        """)
        with_items = cur.fetchone()[0]
        print(f"  有品目明细的项目 {with_items}（可出库+文档对齐题）")

        cur.execute("SELECT min(award_yuan), max(award_yuan), percentile_cont(0.5) WITHIN GROUP (ORDER BY award_yuan) FROM awards WHERE award_yuan IS NOT NULL")
        lo, hi, mid = cur.fetchone()
        print(f"  中标金额：最小 {lo:,.0f} / 中位 {mid:,.0f} / 最大 {hi:,.0f}")

        cur.execute("SELECT count(*) FROM awards WHERE award_yuan < 10000")
        print(f"  异常低价（<1 万）{cur.fetchone()[0]} 条")
        cur.execute("SELECT count(*) FROM awards WHERE award_yuan > 50000000")
        print(f"  异常高价（>5000 万）{cur.fetchone()[0]} 条")

        result["scoring"] = {
            "awarded_projects": awarded_projects,
            "multi_announcement_projects": multi_ann,
            "projects_with_items": with_items,
        }

    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"验收报告 → {REPORT.name}")


if __name__ == "__main__":
    main()
