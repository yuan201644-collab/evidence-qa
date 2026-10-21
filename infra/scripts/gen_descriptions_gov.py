"""生成 infra/schema_meta_gov.descriptions.zh.json（政采库的表/字段中文说明）。

为什么单独成文件而不是并进 schema_meta_gov.json：沿用 Chinook 的既有分工 ——
`schema_meta*.json` 是从库反射生成的技术元数据（每次重建都会覆盖），
`*.descriptions.zh.json` 是人工维护的业务语义资产（生成器不碰）。
`app/nl2sql/normalizer.py` 的 `_metadata_paths()` 在设置了 `SCHEMA_META_PATH`
时会自动去找**同目录同名**的 `.descriptions.zh.json`，本文件正是那个名字。

这份说明的作用有两处，第二处是关键：
  ① 喂给大模型看表含义；
  ② `load_vocab()` 用 `_desc_keyword()` 从每张表的说明里取主干词
     （"采购项目表。一行代表…" → "采购项目"）建成 `table_desc_alias`，
     澄清器靠它判断"用户到底要统计什么对象"。缺这个文件时该词表为空，
     「政采库里一共有多少个采购项目？」会被判成没说对象而追问。
"""
from __future__ import annotations

import json
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
OUT = WS / "infra" / "schema_meta_gov.descriptions.zh.json"

#: 表说明。**首句主干词必须就是业务上对该表的称呼**，因为 `_desc_keyword()`
#: 只取首个标点前的片段、剥掉结尾「表」—— 写成"本表记录…"会让主干词变成
#: "本表记录"，词表就建废了。格式统一为「<主干词>表。<补充说明>」。
#:
#: ⚠️ 主干词要取**用户真会说的那个词**，不要取"采购公告""中标供应商"这类
#: 带限定语的全称。理由：`table_desc_alias` 是拿主干词去扫用户原话的，
#: 用户问「一共发布了多少条公告？」里没有"采购"二字，写成"采购公告"就匹配不上。
#: 限定语放进第二句，那里不参与主干词提取，只给大模型看。
TABLES = {
    "regions": "省级行政区表。一行代表一个省级行政区（含港澳台）。",
    "buyers": "采购单位表。一行代表一个采购人，如学校、医院、机关。也叫采购人、招标人。",
    "agencies": "采购代理机构表。一行代表一家受委托组织采购的代理机构，简称代理机构。",
    "suppliers": "供应商表。一行代表一家在中标或成交公告里出现过的供应商，即中标供应商。",
    "projects": "项目表。一行代表一个采购项目，按项目编号去重，是结构化数据的中心表。也叫采购项目。",
    "announcements": "公告表。一行代表一份采购公告（招标、中标、成交、更正、废标等），与采购项目多对一。",
    "packages": "包组表。一行代表一个采购项目里的一个包（标段），也叫标段。",
    "items": "品目表。一行代表一个采购标的（货物或服务）的品目，含品牌、型号、数量、单价。",
    "awards": "中标结果表。一行代表一个包组的中标记录，含中标金额与中标供应商。也叫中标、中标记录。",
    "documents": "公告原文表。一行代表一份公告正文全文，是结构化数据与文档知识库之间的跨源桥。",
    "regulation_rules": "法规条款表。一行代表一条可从政府采购法规机械判定的规则，带原文引用。",
}

#: 字段说明。键名格式「表名.字段名」。只写业务含义与口径坑，不写类型。
COLUMNS = {
    # ── regions ──
    "regions.region_code": "行政区代码。",
    "regions.region_name": "省级行政区名称，如「北京」「云南」。",
    "regions.level": "行政区层级，1 表示省级。",

    # ── buyers ──
    "buyers.buyer_id": "采购单位主键。",
    "buyers.buyer_name": "采购单位名称，如「清华大学」。",
    "buyers.announcement_count": "该采购单位名下的公告条数。",

    # ── agencies ──
    "agencies.agency_id": "代理机构主键。",
    "agencies.agency_name": "采购代理机构名称。",
    "agencies.announcement_count": "该代理机构名下的公告条数。",

    # ── suppliers ──
    "suppliers.supplier_id": "供应商主键。",
    "suppliers.supplier_name": "中标供应商名称。",
    "suppliers.award_count": "该供应商的中标记录条数。",

    # ── projects ──
    "projects.project_id": "项目编号。源站原文，形态不统一（含中文、括注冗余），查询时须整体匹配不要截断。",
    "projects.project_name": "项目名称。",
    "projects.buyer_id": "所属采购单位，外键指向 buyers.buyer_id。",
    "projects.agency_id": "所属代理机构，外键指向 agencies.agency_id。",
    "projects.region_code": "项目所属省级行政区，外键指向 regions.region_code。要显示地区名须 join regions 取 region_name。",
    "projects.procurement_way": "采购方式：公开招标、竞争性磋商、单一来源、竞争性谈判、询价、框架协议。约两成为空。",
    "projects.budget_yuan": "项目预算金额，单位元。约一半为空。",
    "projects.first_announce_date": "该项目最早一份公告的发布日。",
    "projects.last_announce_date": "该项目最晚一份公告的发布日。",
    "projects.announcement_count": "该采购项目下的公告条数。注意统计项目数不能用公告条数代替。",
    "projects.budget_level": "预算级次：central 中央预算、local 地方预算、unknown 未知。统计中央/地方占比时 unknown 会改变分母。",

    # ── announcements ──
    "announcements.announcement_id": "公告主键。",
    "announcements.project_id": "所属采购项目，外键指向 projects.project_id。",
    "announcements.title": "公告标题，含公告类型后缀。",
    "announcements.kind": "公告类型：招标公告、中标公告、成交公告、磋商公告、更正公告、废标终止公告、其他公告等。",
    "announcements.publish_date": "公告发布日。",
    "announcements.source_url": "源站原始地址。",
    "announcements.body_chars": "正文清洗后的字符数。",
    "announcements.has_items": "该公告是否含品目明细区块。",

    # ── packages ──
    "packages.package_id": "包组主键。",
    "packages.project_id": "所属采购项目，外键指向 projects.project_id。",
    "packages.package_no": "包组编号，如「1」「A」。",
    "packages.budget_yuan": "包组预算金额，单位元。",
    "packages.award_yuan": "包组中标金额，单位元。当前数据中该列全部为空，包级金额必须从 awards 关联获取。",

    # ── items ──
    "items.item_id": "品目主键。",
    "items.announcement_id": "所属公告，外键指向 announcements.announcement_id。本表没有 project_id，要统计到项目须经公告挂载。",
    "items.package_id": "所属包组，外键指向 packages.package_id，可为空。",
    "items.item_seq": "品目在该公告区块内的序号。",
    "items.item_name": "品目名称，如「服务器」「声学多普勒流速剖面仪」。",
    "items.brand": "品牌。",
    "items.model": "规格型号。",
    "items.category_code": "品目分类编码。",
    "items.category_name": "品目分类名称。",
    "items.quantity": "采购数量。",
    "items.quantity_unit": "数量单位，如「台」「套」「项」。",
    "items.unit_price_yuan": "单价，单位元。",
    "items.total_price_yuan": "总价，单位元。当前数据中基本为空（源站表格没有总价列），求总额须用单价×数量或从 awards 取。",

    # ── awards ──
    "awards.award_id": "中标记录主键。",
    "awards.project_id": "所属采购项目，外键指向 projects.project_id。",
    "awards.package_id": "所属包组，外键指向 packages.package_id。",
    "awards.supplier_id": "中标供应商，外键指向 suppliers.supplier_id。",
    "awards.award_yuan": "中标金额，单位元。源站「0.0000000 万元」的空占位会被抽成 0，统计金额必须过滤大于 0，否则拉低均值。",
    "awards.review_score": "评审得分，多数为空。",
    "awards.award_date": "中标或成交公告的发布日。",
    "awards.announcement_id": "来源公告，外键指向 announcements.announcement_id。",

    # ── documents ──
    "documents.document_id": "文档主键。",
    "documents.project_id": "所属采购项目，外键指向 projects.project_id。这是跨源问答的桥：从结构化侧定位到项目，再用它去原文里找依据。",
    "documents.announcement_id": "对应公告，外键指向 announcements.announcement_id。",
    "documents.title": "公告标题。",
    "documents.doc_type": "文档类型，与 announcements.kind 对应。",
    "documents.source_url": "源站原始地址。",
    "documents.local_path": "正文文件相对路径。",
    "documents.char_count": "正文字符数。",
    "documents.sha256": "正文内容哈希。",

    # ── regulation_rules ──
    "regulation_rules.rule_id": "规则主键，如 R001。",
    "regulation_rules.regulation": "来源法规名称，如「财政部令第87号《政府采购货物和服务招标投标管理办法》」。",
    "regulation_rules.article": "法规条号，如「第四条」。",
    "regulation_rules.condition_field": "该规则判定的字段，如 procurement_way、budget_level。",
    "regulation_rules.condition_op": "判定操作符，如 =、in、ratio_max、window_days。",
    "regulation_rules.condition_value": "判定取值。",
    "regulation_rules.conclusion": "规则结论（人话）。",
    "regulation_rules.source_snippet": "法规原文逐字片段，回答里引用条文时用这一列。",
    "regulation_rules.source_document": "法规正文文件路径。",
    "regulation_rules.source_url": "法规原文地址。",
    "regulation_rules.note": "补充说明，如适用范围限制。",
}


def main() -> None:
    missing_tables = [
        t for t in TABLES if not t
    ]
    assert not missing_tables
    # contract_version 必须与 schema_meta_gov.json 逐字一致，否则
    # app/nl2sql/linker.py 的 `_descriptions()` 在一致性校验处会**整份丢弃**
    # 说明文件（那边 `sidecar_version != catalog_version` 就 return {}），
    # 后果是所有表说明失效、linker 退化成只认表名与别名。实测这一条曾让
    # 「政采库里一共有多少个采购项目？」里 buyers/items/projects 三张表
    # 同为 0.300 并列，7 张表全被选进计划、生成器答错表。
    # 所以这里从 schema 文件读，不写字面量。
    schema_path = WS / "infra" / "schema_meta_gov.json"
    contract_version = "0.1"
    if schema_path.is_file():
        with schema_path.open(encoding="utf-8") as handle:
            contract_version = str(json.load(handle).get("contract_version") or contract_version)
    else:
        print(f"[警告] 未找到 {schema_path.name}，contract_version 用默认值 {contract_version}")
    payload = {
        "_readme": (
            "政采业务库的表/字段中文说明。与 schema_meta_gov.json 分开存放："
            "后者是可用 gen_schema_meta_gov.py 重新生成的技术元数据，本文件是"
            "人工维护的业务语义资产（生成器不覆盖）。键名格式：表名 / 表名.字段名。"
            '像「采购项目表。…也叫采购项目。」这样第二句列出的同义说法，'
            "会被 app/nl2sql/normalizer.py 的 _desc_aliases 抽进澄清器词表。"
        ),
        "contract_version": contract_version,
        "database": "askdata_gov",
        "tables": TABLES,
        "columns": COLUMNS,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {len(TABLES)} 张表 / {len(COLUMNS)} 个字段说明 → {OUT.relative_to(WS)}")

    # 主干词自检：干词为空或明显退化时报警，这是澄清器能不能认出对象的关键
    degenerate = []
    for name, desc in TABLES.items():
        head = desc.split("。")[0].replace("，", "").replace("（", "（")
        kw = head.split("。")[0]
        if len(kw) < 2:
            degenerate.append((name, kw))
    if degenerate:
        print(f"[警告] 主干词过短: {degenerate}")


if __name__ == "__main__":
    main()
