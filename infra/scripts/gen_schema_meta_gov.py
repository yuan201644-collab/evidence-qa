"""从 askdata_gov 数据库反射生成 infra/schema_meta_gov.json。

为什么需要：`app/nl2sql/normalizer.py:51` 的 `SCHEMA_META_PATH` 默认指向
`infra/schema_meta.json`（Chinook 的 13 张表）。切到政采库后，
`_object_matches` 用 `vocab.table_desc_alias` 扫原文，词表里没有
「采购项目」「公告」「品目」「供应商」，于是「政采库里一共有多少个采购项目？」
被判定为"没说要统计的业务对象"，澄清器直接追问。

做法：直接从 information_schema 与系统目录读表、列、主键、外键、行数、示例值，
不手抄 —— 手抄的元数据一定会和库漂移（这条教训在 b108 的规则条文上已经吃过一次）。

中文说明由 COLUMN_DOC / TABLE_DOC 两张词表提供，缺词的表/列留空，
让 linker 只靠表名列名也能工作，而不是编一个说明出来。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import psycopg  # noqa: E402

DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"
OUT = WS / "infra" / "schema_meta_gov.json"

# 表级中文说明：写清这张表"装什么、一行代表什么"，这是 Schema Linking 判断
# 「问题在问哪张表」的主要依据，比列名重要。
TABLE_DOC = {
    "regions": "省级行政区。一行代表一个省级行政区，含港澳台。",
    "buyers": "采购单位。一行代表一个采购人（学校、医院、机关等）。",
    "agencies": "采购代理机构。一行代表一家代理机构。",
    "suppliers": "中标供应商。一行代表一家在中标公告里出现过的供应商。",
    "projects": "采购项目。一行代表一个采购项目，按项目编号去重，是结构化数据的中心表。",
    "announcements": "采购公告。一行代表一份公告（招标/中标/成交/更正/废标等），与 projects 多对一。",
    "packages": "采购包组。一行代表一个项目里的一个包（标段），与 announcements 多对一。",
    "items": "品目明细。一行代表一个采购标的（货物或服务）的品目，含品牌、型号、数量、单价。",
    "awards": "中标结果。一行代表一个包的中标记录，含中标金额与中标供应商。",
    "documents": "公告原文归档。一行代表一份公告的正文全文，是结构化数据与文档知识库之间的跨源桥。",
    "regulation_rules": "法规条款。一行代表一条可从政府采购法规中机械判定的规则，带原文引用。",
}

# 列级中文说明：只写"这一列在业务上是什么"，不写类型（类型从库里读）。
COLUMN_DOC = {
    "regions": {
        "region_code": "省级行政区代码，主键。",
        "region_name": "省级行政区名称，如「北京」「云南」。",
    },
    "buyers": {
        "buyer_id": "采购单位主键。",
        "buyer_name": "采购单位名称，如「清华大学」。",
    },
    "agencies": {
        "agency_id": "代理机构主键。",
        "agency_name": "采购代理机构名称。",
    },
    "suppliers": {
        "supplier_id": "供应商主键。",
        "supplier_name": "中标供应商名称。",
    },
    "projects": {
        "project_id": "项目编号（源站原文，形态不统一）。",
        "project_name": "项目名称。",
        "buyer_id": "所属采购单位，外键指向 buyers。",
        "agency_id": "所属代理机构，外键指向 agencies。",
        "region_code": "项目所属省级行政区，外键指向 regions.region_code。要显示地区名须 join regions 取 region_name。",
        "procurement_way": "采购方式：公开招标/竞争性磋商/单一来源/竞争性谈判/询价/框架协议，约 22% 为空。",
        "budget_yuan": "项目预算金额，单位元。",
        "first_announce_date": "最早一份公告的发布日。",
        "last_announce_date": "最晚一份公告的发布日。",
        "announcement_count": "该项目下的公告条数。",
        "budget_level": "预算级次：central（中央预算）/local（地方预算）/unknown（未知）。",
    },
    "announcements": {
        "announcement_id": "公告主键。",
        "project_id": "所属采购项目，外键指向 projects。",
        "title": "公告标题，含公告类型后缀。",
        "kind": "公告类型：招标公告/中标公告/成交公告/磋商公告/更正公告/废标终止公告/其他公告等。",
        "publish_date": "公告发布日。",
        "source_url": "源站原始地址。",
        "body_chars": "正文清洗后的字符数。",
        "has_items": "该公告是否含品目明细区块。",
    },
    "packages": {
        "package_id": "包组主键。",
        "project_id": "所属采购项目，外键指向 projects。",
        "package_no": "包组编号，如「1」「A」。",
        "budget_yuan": "包组预算金额，单位元。",
        "award_yuan": "包组中标金额，单位元。注意：当前数据中该列全部为空，包级金额需从 awards 关联获取。",
    },
    "items": {
        "item_id": "品目主键。",
        "announcement_id": "所属公告，外键指向 announcements。注意 items 没有 project_id，需经公告挂到项目。",
        "package_id": "所属包组，外键指向 packages，可为空。",
        "item_seq": "品目在区块内的序号。",
        "item_name": "品目名称，如「服务器」「声学多普勒流速剖面仪」。",
        "brand": "品牌。",
        "model": "规格型号。",
        "category_code": "品目分类编码。",
        "category_name": "品目分类名称。",
        "quantity": "数量。",
        "quantity_unit": "数量单位，如「台」「套」「项」。",
        "unit_price_yuan": "单价，单位元。",
        "total_price_yuan": "总价，单位元。当前数据中基本为空（源站表格无总价列）。",
    },
    "awards": {
        "award_id": "中标记录主键。",
        "project_id": "所属采购项目，外键指向 projects。",
        "package_id": "所属包组，外键指向 packages。",
        "supplier_id": "中标供应商，外键指向 suppliers。",
        "award_yuan": "中标金额，单位元。注意：源站「0.0000000 万元」空占位会被抽成 0，统计金额须过滤 > 0。",
        "review_score": "评审得分，多数为空。",
        "award_date": "中标/成交公告发布日。",
        "announcement_id": "来源公告，外键指向 announcements。",
    },
    "documents": {
        "doc_id": "文档主键。",
        "project_id": "所属采购项目，外键指向 projects（跨源桥）。",
        "announcement_id": "对应公告，外键指向 announcements。",
        "title": "公告标题。",
        "doc_type": "文档类型。",
        "local_path": "正文文件相对路径。",
        "char_count": "正文字符数。",
        "sha256": "正文内容哈希。",
    },
    "regulation_rules": {
        "rule_id": "规则主键，如 R001。",
        "regulation": "来源法规名称，如「财政部令第87号《政府采购货物和服务招标投标管理办法》」。",
        "article": "法规条号，如「第四条」。",
        "condition_field": "该规则判定的字段，如 procurement_way、budget_level。",
        "condition_op": "判定操作符，如 =、in、ratio_max、window_days。",
        "condition_value": "判定取值。",
        "conclusion": "规则结论（人话）。",
        "source_snippet": "法规原文逐字片段。",
        "source_document": "法规正文文件路径。",
        "source_url": "法规原文地址。",
        "note": "补充说明。",
    },
}


#: 业务表之外的实现细节表。向量检索用的两张表与业务问题无关，
#: 放进 schema 元数据会让 Schema Linking 把它们当成可查对象 ——
#: 「有多少条切片」这种问题本不该由问数链路回答。
NON_BUSINESS_TABLES = frozenset({"chunk_vectors", "doc_meta"})


def main() -> None:
    tables_payload = []
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """)
        table_names = [
            r[0] for r in cur.fetchall() if r[0] not in NON_BUSINESS_TABLES
        ]

        for table in table_names:
            cur.execute("""
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
            """, (table,))
            columns_meta = cur.fetchall()

            cur.execute("""
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = 'public' AND tc.table_name = %s
                  AND tc.constraint_type = 'PRIMARY KEY'
                ORDER BY kcu.ordinal_position
            """, (table,))
            pk = [r[0] for r in cur.fetchall()]

            cur.execute(f'SELECT count(*) FROM public."{table}"')
            row_count = cur.fetchone()[0]

            # 外键必须读出来：`app/nl2sql/join_planner.py:117` 的 `_foreign_edges`
            # 靠 `table.foreign_keys` 建连接图，缺了它多表题一律报
            # "无法建立唯一且有元数据依据的表关联路径"。
            cur.execute("""
                SELECT kcu.column_name,
                       ccu.table_name AS ref_table,
                       ccu.column_name AS ref_column
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage ccu
                  ON ccu.constraint_name = tc.constraint_name
                 AND ccu.table_schema = tc.table_schema
                WHERE tc.table_schema = 'public' AND tc.table_name = %s
                  AND tc.constraint_type = 'FOREIGN KEY'
                ORDER BY kcu.ordinal_position
            """, (table,))
            foreign_keys = [
                {
                    "columns": [col],
                    "references": {
                        "schema": "public",
                        "table": ref_table,
                        "columns": [ref_col],
                    },
                }
                for col, ref_table, ref_col in cur.fetchall()
            ]

            col_docs = COLUMN_DOC.get(table, {})
            columns = []
            for name, dtype, nullable in columns_meta:
                # 示例值：低基数列取去重前三，高基数列取前三条，都限长防撑爆元数据
                samples: list[str] = []
                try:
                    cur.execute(
                        f'SELECT DISTINCT "{name}" FROM public."{table}" '
                        f'WHERE "{name}" IS NOT NULL LIMIT 3'
                    )
                    samples = [str(r[0])[:60] for r in cur.fetchall()]
                except Exception:  # noqa: BLE001 - 示例值拿不到不该阻断元数据生成
                    samples = []
                columns.append({
                    "name": name,
                    "type": dtype.upper(),
                    "nullable": nullable == "YES",
                    "is_primary_key": name in pk,
                    "description": col_docs.get(name, ""),
                    "sample_values": samples,
                })

            tables_payload.append({
                "table": table,
                "schema": "public",
                "description": TABLE_DOC.get(table, ""),
                "row_count": row_count,
                "primary_key": pk,
                "columns": columns,
                "foreign_keys": foreign_keys,
            })
            print(f"  {table:<20} {row_count:>6} 行  {len(columns):>2} 列  PK={pk}")

    payload = {
        "contract_version": "0.1",
        "database": "askdata_gov",
        "dialect": "postgres",
        "source": "information_schema（反射生成，非手抄）",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "infra/scripts/gen_schema_meta_gov.py",
        "tables": tables_payload,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写出 {len(tables_payload)} 张表 → {OUT.relative_to(WS)}")
    missing = [t["table"] for t in tables_payload if not t["description"]]
    if missing:
        print(f"[警告] 这些表缺中文说明，Schema Linking 只能靠表名：{missing}")


if __name__ == "__main__":
    main()
