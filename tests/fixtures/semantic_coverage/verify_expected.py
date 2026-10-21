"""独立重算 L2 隔离样本的参考结果，与 expected.json 逐条比对。

母方案 :470：「上述样本和判据分别保存、**独立重算**」。
所以本脚本**不读 expected.json 去拼 SQL**，而是把每条参考结果写成显式查询，
再拿结果去比 expected.json。两者不一致就是其中一方错了。

用法：
    python verify_expected.py --dsn 'postgresql://.../semantic_coverage_fixture'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPECTED_JSON = HERE / "expected.json"

sys.path.insert(0, str(HERE))
from load import database_name, reject_business_database  # noqa: E402

#: 锚点语义：截至 2024-06-15 = 当日结束，即 < 2024-06-16T00:00:00Z
ANCHOR_END = "2024-06-16T00:00:00Z"

#: (expected.json 里的路径, 描述, SQL)。SQL 一律返回单值或单行。
CHECKS: list[tuple[str, str, str]] = [
    # ── customer ──────────────────────────────────────────────────────
    ("customer.total", "客户总数",
     "SELECT count(*) FROM customer"),
    ("customer.brazil", "巴西客户",
     "SELECT count(*) FROM customer WHERE country = 'Brazil'"),
    ("customer.non_brazil_excluding_unknown", "非巴西（排除未知国家）",
     "SELECT count(*) FROM customer WHERE country IS NOT NULL AND country <> 'Brazil'"),
    ("customer.unknown_country", "未知国家",
     "SELECT count(*) FROM customer WHERE country IS NULL"),

    # ── invoice ───────────────────────────────────────────────────────
    ("invoice.total_count", "全部发票张数",
     "SELECT count(*) FROM invoice"),
    ("invoice.count_2023", "2023 发票张数",
     "SELECT count(*) FROM invoice WHERE invoice_date >= '2023-01-01T00:00:00Z' "
     "AND invoice_date < '2024-01-01T00:00:00Z'"),
    ("invoice.distinct_customers_2023", "2023 购买客户去重",
     "SELECT count(DISTINCT customer_id) FROM invoice WHERE invoice_date >= '2023-01-01T00:00:00Z' "
     "AND invoice_date < '2024-01-01T00:00:00Z'"),
    ("invoice.total_2024_through_anchor", "2024 截至锚点合计",
     f"SELECT sum(total) FROM invoice WHERE invoice_date >= '2024-01-01T00:00:00Z' "
     f"AND invoice_date < '{ANCHOR_END}'"),
    ("invoice.total_2024_full_year", "2024 全年合计",
     "SELECT sum(total) FROM invoice WHERE invoice_date >= '2024-01-01T00:00:00Z' "
     "AND invoice_date < '2025-01-01T00:00:00Z'"),
    ("invoice.sum_all", "全量合计",
     "SELECT sum(total) FROM invoice"),
    ("invoice.avg_all", "全量平均",
     "SELECT round(avg(total), 2) FROM invoice"),
    ("invoice.gt_100_count", "金额严格大于 100",
     "SELECT count(*) FROM invoice WHERE total > 100"),
    ("invoice.gte_100_count", "金额不小于 100",
     "SELECT count(*) FROM invoice WHERE total >= 100"),
    ("invoice.customers_without_invoice", "无发票客户",
     "SELECT array_agg(customer_id ORDER BY customer_id) FROM customer c "
     "WHERE NOT EXISTS (SELECT 1 FROM invoice i WHERE i.customer_id = c.customer_id)"),

    # ── employee ──────────────────────────────────────────────────────
    ("employee.new_hires_2023", "2023 新入职",
     "SELECT count(*) FROM employee WHERE hire_date >= '2023-01-01' AND hire_date < '2024-01-01'"),

    # ── SC-20 分组 ─────────────────────────────────────────────────────
    ("sc20_country_group.countries_over_5", "客户数 >5 的国家",
     "SELECT array_agg(DISTINCT country) FROM country_group "
     "WHERE country IN (SELECT country FROM country_group GROUP BY country HAVING sum(customer_count) > 5)"),
    ("sc20_country_group.brazil_city_breakdown", "Brazil 城市细分",
     "SELECT jsonb_object_agg(city, customer_count) FROM country_group WHERE country = 'Brazil'"),
]

#: 需要按行取数的检查：路径 -> (SQL, 列索引)
ROW_CHECKS: list[tuple[str, str, str, int]] = [
    ("invoice.top3_by_amount", "最高 3 张（发票号）",
     "SELECT array_agg(invoice_id ORDER BY total DESC, invoice_id DESC) FROM "
     "(SELECT invoice_id, total FROM invoice ORDER BY total DESC, invoice_id DESC LIMIT 3) t", 0),
    ("invoice.top3_by_amount_values", "最高 3 张（金额）",
     "SELECT array_agg(total ORDER BY total DESC) FROM "
     "(SELECT total FROM invoice ORDER BY total DESC LIMIT 3) t", 0),
]


def dig(obj, path: str):
    for part in path.split("."):
        obj = obj[part]
    return obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    args = parser.parse_args(argv)

    reject_business_database(args.dsn)
    print(f"目标库：{database_name(args.dsn)!r}")

    expected = json.loads(EXPECTED_JSON.read_text(encoding="utf-8"))

    import psycopg
    from decimal import Decimal

    def norm(value):
        """Decimal → float，list/其他原样，便于与 JSON 比对。"""
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, list):
            return [norm(item) for item in value]
        if isinstance(value, dict):
            return {k: norm(v) for k, v in value.items()}
        return value

    failures: list[str] = []
    with psycopg.connect(args.dsn) as conn:
        with conn.cursor() as cur:
            for path, label, sql in CHECKS:
                cur.execute(sql)
                actual = norm(cur.fetchone()[0])
                want = dig(expected, path)
                ok = actual == want
                print(f"{'OK  ' if ok else 'FAIL'}  {label:24} actual={actual!r} expected={want!r}  [{path}]")
                if not ok:
                    failures.append(path)

            for path, label, sql, idx in ROW_CHECKS:
                cur.execute(sql)
                actual = norm(cur.fetchone()[idx])
                want = dig(expected, path)
                ok = actual == want
                print(f"{'OK  ' if ok else 'FAIL'}  {label:24} actual={actual!r} expected={want!r}  [{path}]")
                if not ok:
                    failures.append(path)

    print()
    if failures:
        print(f"不一致 {len(failures)} 项：{', '.join(failures)}")
        return 1
    print(f"全部 {len(CHECKS) + len(ROW_CHECKS)} 项参考结果自洽。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
