"""把已抽取的政采数据灌进 askdata_gov 库。

输入（agent_temp/pb/probe_ccgp/）：
    cross_source_key_all.json   2742 条公告记录
    extracted_full_v6.jsonl     公告级补抽字段（way / project_id / supplier 等）
    items_v7.jsonl              856 行品目明细
    detail_v3/*.txt             公告正文（清洗后）

写入顺序（受外键约束）：
    regions → buyers → agencies → suppliers → projects
    → announcements → packages → items → awards → documents

设计要点：
  1) 供应商名要清洗——v6 抽出过「详见中标（定标）候选人公示名单」这类非公司名；
  2) 项目编号为空的公告不能挂到 projects，用标题哈希造一个伪编号并标记；
  3) awards 一条 = 一个中标/成交公告的供应商，金额取该公告的 award_yuan；
  4) documents 与 announcements 一一对应，doc_code 用公告 URL 的 slug。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import psycopg  # noqa: E402

WORKSPACE = Path(__file__).resolve().parents[2]
OUT = WORKSPACE / "agent_temp" / "pb" / "probe_ccgp"
DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"

PROVINCES = ["北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
             "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
             "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海",
             "内蒙古", "广西", "西藏", "宁夏", "新疆"]

PROVINCE_CODES = {
    "北京": "110000", "天津": "120000", "河北": "130000", "山西": "140000",
    "内蒙古": "150000", "辽宁": "210000", "吉林": "220000", "黑龙江": "230000",
    "上海": "310000", "江苏": "320000", "浙江": "330000", "安徽": "340000",
    "福建": "350000", "江西": "360000", "山东": "370000", "河南": "410000",
    "湖北": "420000", "湖南": "430000", "广东": "440000", "广西": "450000",
    "海南": "460000", "重庆": "500000", "四川": "510000", "贵州": "520000",
    "云南": "530000", "西藏": "540000", "陕西": "610000", "甘肃": "620000",
    "青海": "630000", "宁夏": "640000", "新疆": "650000",
}

COMPANY_MARKERS = re.compile(
    r"(有限公司|股份有限公司|集团|研究院|事务所|中心|公司|大学|医院|工厂|商行|经营部|社|厂|工作室)")
BAD_SUPPLIER = re.compile(
    r"(详见|候选人公示|名单|附件|见附|无|/|－|—|、|，|。|；|：|:)")

# ---- 预算级次判定 ----
# 《政府采购法实施条例》的公开招标数额标准按中央/地方分档
# （中央预算单位货物服务 200 万元、工程 400 万元；地方由各省公布），
# 没有这个属性「200 万门槛」这条规则无法判定。
# 判定按采购单位名称特征，顺序即优先级，兜底 local（绝大多数为地方单位）。
CENTRAL_KEYWORDS = (
    "中国科学院", "中国社会科学院", "中国工程院", "国务院", "中央",
    "国家税务总局", "国家发展和改革委员会", "教育部", "财政部", "工业和信息化部",
    "科学技术部", "自然资源部", "生态环境部", "交通运输部", "水利部", "农业农村部",
    "商务部", "文化和旅游部", "国家卫生健康委员会", "应急管理部", "海关总署",
    "国家市场监督管理总局", "国家统计局", "国家知识产权局", "中国气象局",
    "国家中医药管理局", "国家药品监督管理局", "国家林业和草原局",
)
LOCAL_MARKERS = re.compile(
    r"(省|市|区|县|州|旗|乡|镇|街道|人民政府|管委会|管理委员会|开发区|新区)"
    r"(?!.*(?:部|总局|总署))")


def classify_budget_level(name: str, text: str = "") -> str:
    """按采购单位判定预算级次：central / local / unknown。"""
    if name.startswith("中央预算单位") or "中央预算单位" in text[:2000]:
        return "central"
    if "地方预算单位" in text[:2000]:
        return "local"
    for keyword in CENTRAL_KEYWORDS:
        if keyword in name:
            return "central"
    # 「XX部」「XX总局」「XX总署」结尾，且名称里没有省市县等地方标记
    if re.search(r"(部|总局|总署|委员会)$", name):
        if not re.search(r"(省|市|区|县|州|旗|乡|镇)", name):
            return "central"
    if LOCAL_MARKERS.search(name):
        return "local"
    if re.search(r"(大学|学院|医院|研究院|研究所|公司|集团|中心)", name):
        # 高校/医院/企业：看有没有省级前缀，没有则按地方处理
        return "local"
    return "unknown"


def clean_supplier(text: str) -> str:
    value = (text or "").strip().strip("。，,；;：:")
    value = re.sub(r"^(?:中标|成交)?(?:供应商|供应商名称|中标供应商)\s*[：:]\s*", "", value)
    if not value or len(value) < 3 or len(value) > 80:
        return ""
    if BAD_SUPPLIER.search(value):
        return ""
    if not COMPANY_MARKERS.search(value):
        return ""
    return value


def clean_text(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text[:limit]


def pick_province(text: str) -> str:
    for province in PROVINCES:
        if text.startswith(province):
            return province
    for province in PROVINCES:
        if province in text:
            return province
    return ""


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    records = json.loads((OUT / "cross_source_key_all.json").read_text(encoding="utf-8"))
    v6_rows = load_jsonl(OUT / "extracted_full_v8.jsonl")
    items = load_jsonl(OUT / "items_v7.jsonl")

    v6_by_url = {row.get("url"): row for row in v6_rows if row.get("url")}
    print(f"公告 {len(records)} 条 / 字段抽取 {len(v6_by_url)} 条 / 品目 {len(items)} 行")

    # ---------- 聚合阶段 ----------
    buyer_counts: Counter[str] = Counter()
    agency_counts: Counter[str] = Counter()
    projects: dict[str, dict] = {}
    supplier_awards: Counter[str] = Counter()
    regions_seen: set[str] = set()

    announcement_rows: list[dict] = []
    packages_by_project: dict[str, set[str]] = defaultdict(set)
    award_specs: list[dict] = []

    skipped_no_project = 0

    for record in records:
        url = record["url"]
        extra = v6_by_url.get(url, {})
        slug = re.sub(r"[^\w]+", "_", url.split("ccgp.gov.cn")[-1])[:80]

        project_id = (extra.get("project_id") or "").strip()
        if not project_id:
            raw_ids = record.get("ids") or []
            project_id = raw_ids[0].strip() if raw_ids else ""
        if not project_id:
            # 没有项目编号的公告，按标题造一个稳定的伪编号
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            project_id = f"TITLE-{digest}"
            skipped_no_project += 1
        project_id = project_id[:120]

        project_name = clean_text(extra.get("project_name") or record.get("project") or record.get("title"), 500)
        buyer = clean_text(extra.get("buyer") or record.get("buyer") or "", 300)
        agency = clean_text(extra.get("agency") or "", 300)
        region_raw = clean_text(extra.get("region") or "", 64)
        province = pick_province(region_raw) or pick_province(buyer) or pick_province(project_name)
        way = clean_text(extra.get("way") or "", 24)
        publish = record.get("date") or ""
        kind = record.get("kind") or "其他"

        if buyer:
            buyer_counts[buyer] += 1
        if agency:
            agency_counts[agency] += 1
        if province:
            regions_seen.add(province)

        # 供应商
        supplier_raw = extra.get("supplier") or ""
        supplier = clean_supplier(supplier_raw)
        if not supplier:
            for item in items:
                if item.get("announcement_url") == url and item.get("supplier"):
                    candidate = clean_supplier(item["supplier"])
                    if candidate:
                        supplier = candidate
                        break

        # 项目聚合
        entry = projects.setdefault(project_id, {
            "project_id": project_id,
            "project_name": project_name,
            "buyer": buyer,
            "agency": agency,
            "region": province,
            "way": way,
            "budget": None,
            "first": publish,
            "last": publish,
            "count": 0,
        })
        entry["count"] += 1
        if publish:
            if not entry["first"] or publish < entry["first"]:
                entry["first"] = publish
            if not entry["last"] or publish > entry["last"]:
                entry["last"] = publish
        if entry["budget"] is None and extra.get("budget_yuan"):
            entry["budget"] = extra["budget_yuan"]
        if not entry["way"] and way:
            entry["way"] = way
        if not entry["region"] and province:
            entry["region"] = province

        # 包组
        pkg_nos = extra.get("pkg_nos") or []
        if not pkg_nos:
            pkg_nos = [""]
        for pkg_no in pkg_nos:
            packages_by_project[project_id].add((pkg_no or "1")[:16])

        announcement_rows.append({
            "project_id": project_id,
            "title": clean_text(record.get("title"), 600),
            "kind": kind[:24],
            "publish_date": publish or None,
            "source_url": url,
            "body_chars": record.get("chars"),
            "has_items": any(item.get("announcement_url") == url for item in items),
            "slug": slug,
        })

        # 中标事实
        if kind in ("中标公告", "成交公告"):
            amount = extra.get("award_yuan")
            score_raw = extra.get("score") or ""
            score = None
            score_match = re.search(r"([\d.]+)", score_raw)
            if score_match:
                try:
                    score = float(score_match.group(1))
                except ValueError:
                    score = None
            if supplier:
                supplier_awards[supplier] += 1
                award_specs.append({
                    "project_id": project_id,
                    "supplier": supplier,
                    "amount": amount,
                    "score": score,
                    "date": publish or None,
                    "url": url,
                })

    print(f"伪项目编号（无编号公告）{skipped_no_project} 条")
    print(f"项目 {len(projects)} 个 / 采购单位 {len(buyer_counts)} 家 / "
          f"代理机构 {len(agency_counts)} 家 / 供应商 {len(supplier_awards)} 家 / "
          f"中标事实 {len(award_specs)} 条")

    # ---------- 写库 ----------
    stats: Counter[str] = Counter()
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            # 清空
            cur.execute("TRUNCATE regulation_rules, documents, awards, items, packages, "
                        "announcements, projects, suppliers, agencies, buyers, regions "
                        "RESTART IDENTITY CASCADE;")

            # 1. regions
            for province in sorted(regions_seen):
                cur.execute(
                    "INSERT INTO regions(region_code, region_name, level) VALUES (%s,%s,1) "
                    "ON CONFLICT DO NOTHING",
                    (PROVINCE_CODES.get(province, province), province))
                stats["regions"] += 1

            # 2. buyers
            buyer_ids: dict[str, int] = {}
            for name, count in buyer_counts.items():
                cur.execute(
                    "INSERT INTO buyers(buyer_name, announcement_count) VALUES (%s,%s) "
                    "ON CONFLICT (buyer_name) DO UPDATE SET announcement_count=EXCLUDED.announcement_count "
                    "RETURNING buyer_id", (name, count))
                buyer_ids[name] = cur.fetchone()[0]
                stats["buyers"] += 1

            # 3. agencies
            agency_ids: dict[str, int] = {}
            for name, count in agency_counts.items():
                cur.execute(
                    "INSERT INTO agencies(agency_name, announcement_count) VALUES (%s,%s) "
                    "ON CONFLICT (agency_name) DO UPDATE SET announcement_count=EXCLUDED.announcement_count "
                    "RETURNING agency_id", (name, count))
                agency_ids[name] = cur.fetchone()[0]
                stats["agencies"] += 1

            # 4. suppliers
            supplier_ids: dict[str, int] = {}
            for name, count in supplier_awards.items():
                cur.execute(
                    "INSERT INTO suppliers(supplier_name, award_count) VALUES (%s,%s) "
                    "ON CONFLICT (supplier_name) DO UPDATE SET award_count=EXCLUDED.award_count "
                    "RETURNING supplier_id", (name, count))
                supplier_ids[name] = cur.fetchone()[0]
                stats["suppliers"] += 1

            # 5. projects
            # budget_level 由采购单位名称判定（中央/地方预算单位），
            # 规则原在 infra/scripts/add_budget_level.py，现并入灌数流程 ——
            # 否则重建库时该列会丢，题集随之报 UndefinedColumn。
            for entry in projects.values():
                region = entry["region"] or ""
                # region 存外键码而非文本（v1.1）：文本列没有外键，
                # 按地区聚合的两表题建不出连接边。抽不到的留空。
                region_code = PROVINCE_CODES.get(region, region) or None if region else None
                cur.execute(
                    "INSERT INTO projects(project_id, project_name, buyer_id, agency_id, "
                    "region_code, procurement_way, budget_yuan, first_announce_date, "
                    "last_announce_date, announcement_count, budget_level) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (project_id) DO NOTHING",
                    (entry["project_id"], entry["project_name"] or "(无项目名)",
                     buyer_ids.get(entry["buyer"]), agency_ids.get(entry["agency"]),
                     region_code, entry["way"] or None, entry["budget"],
                     entry["first"] or None, entry["last"] or None, entry["count"],
                     classify_budget_level(entry["buyer"] or "")))
                stats["projects"] += 1

            # 6. announcements
            ann_ids: dict[str, int] = {}
            for row in announcement_rows:
                cur.execute(
                    "INSERT INTO announcements(project_id, title, kind, publish_date, "
                    "source_url, body_chars, has_items) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (source_url) DO NOTHING RETURNING announcement_id",
                    (row["project_id"], row["title"], row["kind"], row["publish_date"],
                     row["source_url"], row["body_chars"], row["has_items"]))
                fetched = cur.fetchone()
                if fetched:
                    ann_ids[row["source_url"]] = fetched[0]
                    stats["announcements"] += 1

            # 7. packages
            package_ids: dict[tuple[str, str], int] = {}
            for project_id, pkg_nos in packages_by_project.items():
                for pkg_no in pkg_nos:
                    cur.execute(
                        "INSERT INTO packages(project_id, package_no) VALUES (%s,%s) "
                        "ON CONFLICT (project_id, package_no) DO NOTHING RETURNING package_id",
                        (project_id, pkg_no))
                    fetched = cur.fetchone()
                    if fetched:
                        package_ids[(project_id, pkg_no)] = fetched[0]
                        stats["packages"] += 1

            # 8. items
            for row in items:
                ann_id = ann_ids.get(row.get("announcement_url"))
                pkg_id = package_ids.get((row.get("project_id"), (row.get("pkg_no") or "1")[:16]))
                cur.execute(
                    "INSERT INTO items(announcement_id, package_id, item_seq, item_name, brand, "
                    "model, category_code, category_name, quantity, quantity_unit, "
                    "unit_price_yuan, total_price_yuan) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (ann_id, pkg_id, row.get("item_seq"),
                     clean_text(row.get("item_name"), 500) or None,
                     clean_text(row.get("brand"), 300) or None,
                     clean_text(row.get("model"), 300) or None,
                     clean_text(row.get("category_code"), 32) or None,
                     clean_text(row.get("category_name"), 300) or None,
                     row.get("quantity"), clean_text(row.get("quantity_unit"), 16) or None,
                     row.get("unit_price_yuan"), row.get("total_price_yuan")))
                stats["items"] += 1

            # 9. awards
            for spec in award_specs:
                supplier_id = supplier_ids.get(spec["supplier"])
                if not supplier_id:
                    continue
                pkg_list = packages_by_project.get(spec["project_id"]) or {""}
                first_pkg = sorted(pkg_list)[0]
                pkg_id = package_ids.get((spec["project_id"], first_pkg))
                cur.execute(
                    "INSERT INTO awards(project_id, package_id, supplier_id, award_yuan, "
                    "review_score, award_date, announcement_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (spec["project_id"], pkg_id, supplier_id, spec["amount"],
                     spec["score"], spec["date"], ann_ids.get(spec["url"])))
                stats["awards"] += 1

            # 10. documents
            for row in announcement_rows:
                ann_id = ann_ids.get(row["source_url"])
                if not ann_id:
                    continue
                cur.execute(
                    "INSERT INTO documents(doc_code, project_id, announcement_id, doc_type, "
                    "title, source_url, local_path, page_count, char_count) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (doc_code) DO NOTHING",
                    (row["slug"], row["project_id"], ann_id, "announcement",
                     row["title"], row["source_url"],
                     f"agent_temp/pb/probe_ccgp/detail_v3/{row['slug']}.txt",
                     1, row["body_chars"]))
                stats["documents"] += 1

        conn.commit()

    print()
    print("=== 灌数结果 ===")
    for table, count in stats.items():
        print(f"  {table:<16} {count:>7}")


if __name__ == "__main__":
    main()
