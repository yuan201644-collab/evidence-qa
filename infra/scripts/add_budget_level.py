"""阶段 1 补充：为 projects 增加预算层级（预算级次）字段并回填。

为什么要这个字段：
    《政府采购法实施条例》规定的公开招标数额标准按中央/地方分档
    （中央预算单位货物服务 200 万元、工程 400 万元；地方由各省公布）。
    没有这个属性，「200 万门槛」这条规则无法判定。

判定依据（按优先级）：
    1) 公告正文里明确写了「中央预算单位」/「地方预算单位」→ 直接取；
    2) 采购单位名称特征：含 部/总局/署/委员会/中国科学院/中国社会科学院/
       中央/国家/国务院 等 → central；
    3) 采购单位名称含 省/市/区/县/州/旗/乡/镇/街道 等 → local；
    4) 兜底 local（绝大多数为地方单位）。

产出：
    在 askdata_gov.projects 上新增 budget_level 列并回填，
    同时输出分布统计。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import psycopg  # noqa: E402

DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"

CENTRAL_MARKERS = re.compile(
    r"(^|中华人民共和|中国)(?:[^\s]{0,6})?(?:部|总局|总署|署|国务院|委员会|"
    r"中国科学院|中国社会科学院|中国工程院|中央|国家[^\s]{0,8}局|"
    r"国家[^\s]{0,8}委员会|全国[^\s]{0,8}(?:会|中心|办公室))")
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


def classify(name: str, text: str = "") -> str:
    """返回 central / local / unknown。"""
    if name.startswith("中央预算单位") or "中央预算单位" in text[:2000]:
        return "central"
    if "地方预算单位" in text[:2000]:
        return "local"

    for keyword in CENTRAL_KEYWORDS:
        if keyword in name:
            return "central"
    # 「XX部」「XX总局」「XX总署」结尾
    if re.search(r"(部|总局|总署|委员会)$", name):
        # 但「XX市财政局」「XX县教育局」这类结尾是局不是部
        if not re.search(r"(省|市|区|县|州|旗|乡|镇)", name):
            return "central"
    if LOCAL_MARKERS.search(name):
        return "local"
    if re.search(r"(大学|学院|医院|研究院|研究所|公司|集团|中心)", name):
        # 高校/医院/企业：看有没有省级前缀
        return "local"
    return "unknown"


def main() -> None:
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                ALTER TABLE projects ADD COLUMN IF NOT EXISTS budget_level VARCHAR(12)
            """)
            cur.execute("COMMENT ON COLUMN projects.budget_level IS "
                        "'预算级次：central=中央预算单位，local=地方预算单位，unknown=无法判定'")

            cur.execute("""
                SELECT p.project_id, COALESCE(b.buyer_name, '') AS buyer_name, p.project_name
                FROM projects p LEFT JOIN buyers b ON b.buyer_id = p.buyer_id
            """)
            rows = cur.fetchall()

            distribution: Counter[str] = Counter()
            updates: list[tuple[str, str]] = []
            for project_id, buyer_name, _ in rows:
                level = classify(buyer_name or "")
                distribution[level] += 1
                updates.append((level, project_id))

            cur.executemany("UPDATE projects SET budget_level=%s WHERE project_id=%s", updates)
        conn.commit()

    total = sum(distribution.values())
    print("=== budget_level 分布 ===")
    for level, count in distribution.most_common():
        print(f"  {level:<10} {count:>5} / {total} = {count/total:>6.1%}")


if __name__ == "__main__":
    main()
