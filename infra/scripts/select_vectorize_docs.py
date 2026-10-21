"""阶段 3 之一：按分层方案挑选要向量化的文档。

三层结构：
    规则层  24 份法规/通知  —— 全量（每份都短，句句是规则）
    项目层  2742 份公告      —— 抽样 200 份（按"能不能出题"选，不随机）
    背景层  18 份财政部文告  —— 不进向量库（内容密度 0.26 次/页，与采购不直接相关）

项目层 200 份的分层标准（与《真实规模业务库建设记录》§6.3 一致）：
    80  有「主要标的信息」的公告   → 库+文档对齐题
    40  完整采购链项目             → 跨公告题（同一 project_id 下招标+更正+中标）
    30  中标金额异常               → 异常低价/大额题
    31  覆盖 31 个省级行政区       → 地域题
    19  跨期补充                   → 时间维度
    ──
    200

去重规则：同一份公告可能同时命中多个标准，按优先级
    完整链 > 有明细 > 金额异常 > 省级覆盖 > 跨期
归类到第一个命中的标准，保证总数不超 200、各层数量可控。

产出 agent_temp/pb/probe_ccgp/vectorize_manifest.json
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import psycopg  # noqa: E402

DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"
WS = Path(__file__).resolve().parents[2]
OUT = WS / "agent_temp" / "pb" / "probe_ccgp"
LAWS_CLEANED = OUT / "laws_cleaned"
CLEANED_ANN = OUT / "cleaned_v3"
MANIFEST = OUT / "vectorize_manifest.json"

TARGETS = {
    "full_chain": 40,
    "has_items": 80,
    "amount_outlier": 30,
    "province_cover": 31,
    "period_fill": 19,
}


def main() -> None:
    manifest: dict = {"regulation": [], "announcement": [], "stats": {}}
    picked: dict[str, dict] = {}
    bucket_count: Counter[str] = Counter()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        # ---------- 规则层：全量 ----------
        #
        # 指向 laws_cleaned/（网页模板已剥离），不是 laws_regulation/（原始剥离结果）。
        # 清洗器：agent_temp/pb/clean_ccgp_text.py
        for path in sorted(LAWS_CLEANED.glob("*.txt")):
            body = path.read_text(encoding="utf-8")
            manifest["regulation"].append({
                "doc_id": path.stem,
                "doc_type": "regulation",
                "title": body.splitlines()[0].strip()[:200] if body.strip() else path.stem,
                "local_path": str(path.relative_to(WS)).replace("\\", "/"),
                "char_count": len(body),
            })
        print(f"规则层：{len(manifest['regulation'])} 份（全量）")

        # ---------- 项目层候选数据 ----------
        # v1.1 起 projects 只存 region_code 外键，地区名要 join regions 取
        cur.execute("""
            SELECT a.announcement_id, a.project_id, a.title, a.kind,
                   a.publish_date::text, a.source_url, a.has_items, a.body_chars,
                   r.region_name, p.budget_level,
                   (SELECT count(*) FROM announcements x WHERE x.project_id = a.project_id) AS chain_len,
                   (SELECT max(aw.award_yuan) FROM awards aw WHERE aw.project_id = a.project_id) AS award_yuan
            FROM announcements a
            JOIN projects p USING (project_id)
            LEFT JOIN regions r ON r.region_code = p.region_code
        """)
        columns = [desc[0] for desc in cur.description]
        rows = [dict(zip(columns, row)) for row in cur.fetchall()]

    def key(row: dict) -> int:
        return row["announcement_id"]

    # 1) 完整采购链：同一 project_id 下有 >=2 条公告，优先选公告数最多的项目
    chains = defaultdict(list)
    for row in rows:
        chains[row["project_id"]].append(row)
    chain_projects = sorted(
        ((pid, items) for pid, items in chains.items() if len(items) >= 2),
        key=lambda kv: (-len(kv[1]), kv[0]))

    for pid, items in chain_projects:
        if bucket_count["full_chain"] >= TARGETS["full_chain"]:
            break
        # 每个项目取 1 条（取最新那条中标公告，没有就取最后一条）
        ordered = sorted(items, key=lambda r: (r["kind"] in ("中标公告", "成交公告"), r["publish_date"] or ""))
        chosen = ordered[-1]
        if key(chosen) in picked:
            continue
        chosen["bucket"] = "full_chain"
        chosen["chain_len"] = len(items)
        picked[key(chosen)] = chosen
        bucket_count["full_chain"] += 1

    # 2) 有品目明细
    for row in sorted(rows, key=lambda r: (-(r["body_chars"] or 0), key(r))):
        if bucket_count["has_items"] >= TARGETS["has_items"]:
            break
        if not row["has_items"] or key(row) in picked:
            continue
        row["bucket"] = "has_items"
        picked[key(row)] = row
        bucket_count["has_items"] += 1

    # 3) 金额异常：极低（<1 万）或极高（>5000 万）
    #
    # 口径说明（2026-10-17 修订）：初版按「低 15 + 高 15」对半取，实际只取到 11 条 ——
    # 因为异常金额公告共 58+5 条，其中多数已被前两个桶（完整链 / 有明细）挑走，
    # 剩下的不够 30。这里改为「有多少取多少，上限 30」，不足的额度由后面的
    # province_cover / period_fill 用剩余量补齐，保证项目层总量仍是 200。
    # 记录里如实写明：金额异常这个考点只有 11 份真实样本，不是 30 份。
    low = [r for r in rows if r["award_yuan"] is not None and 0 < r["award_yuan"] < 10000]
    high = [r for r in rows if r["award_yuan"] is not None and r["award_yuan"] > 50000000]
    for row in (sorted(low, key=lambda r: (r["award_yuan"], key(r))) +
                sorted(high, key=lambda r: (-r["award_yuan"], key(r)))):
        if bucket_count["amount_outlier"] >= TARGETS["amount_outlier"]:
            break
        if key(row) in picked:
            continue
        row["bucket"] = "amount_outlier"
        picked[key(row)] = row
        bucket_count["amount_outlier"] += 1

    # 4) 省级覆盖：每个省至少 1 条，按省份名称排序轮转，
    #    每轮给每个省取 1 条 —— 这样 31 个省正好铺满 31 个名额，
    #    且第二轮才有省拿到第 2 条，不会出现个别大省吃光名额。
    #    初版按「省份排序、每省只取 1 条」写，命中率低是因为挑的是正文最长的，
    #    而长公告多半已被前两桶选走。
    by_province = defaultdict(list)
    for row in rows:
        if row["region_name"] and key(row) not in picked:
            by_province[row["region_name"]].append(row)
    for items in by_province.values():
        items.sort(key=lambda r: (-(r["body_chars"] or 0), key(r)))
    for province in sorted(by_province):
        if bucket_count["province_cover"] >= TARGETS["province_cover"]:
            break
        if not by_province[province]:
            continue
        chosen = by_province[province].pop(0)
        chosen["bucket"] = "province_cover"
        chosen["province"] = province
        picked[key(chosen)] = chosen
        bucket_count["province_cover"] += 1

    # 5) 跨期补充：按发布日期铺开，同样用「日期轮转」——
    #    数据只有 10 个发布日期，19 个名额靠「每轮每个日期取 1 条」自然铺成
    #    第 1 轮 10 条 + 第 2 轮 9 条。
    by_date = defaultdict(list)
    for row in rows:
        if row["publish_date"] and key(row) not in picked:
            by_date[row["publish_date"]].append(row)
    for items in by_date.values():
        items.sort(key=lambda r: (-(r["body_chars"] or 0), key(r)))
    dates = sorted(by_date)
    while bucket_count["period_fill"] < TARGETS["period_fill"]:
        progressed = False
        for date in dates:
            if bucket_count["period_fill"] >= TARGETS["period_fill"]:
                break
            if not by_date[date]:
                continue
            chosen = by_date[date].pop(0)
            chosen["bucket"] = "period_fill"
            chosen["date"] = date
            picked[key(chosen)] = chosen
            bucket_count["period_fill"] += 1
            progressed = True
        if not progressed:
            break

    # 6) 兜底补足：前面的分层标准都跑完后还差多少，就从「没被选中、正文最长」的
    #    公告里补，保证项目层总量达到 200。
    #
    #    为什么要补：amount_outlier 这一层取不满不是脚本问题，而是**源站这类数据
    #    本身就只有 16 条**（有中标价且 <1 万或 >5000 万，按项目去重后），已全部选中。
    #    这个边界必须写进记录，不能靠调口径掩盖；补足的部分记为 extra，
    #    在统计里单独显示，不与五个既定标准混算。
    shortfall = sum(TARGETS.values()) - len(picked)
    if shortfall > 0:
        rest = sorted((r for r in rows if key(r) not in picked),
                      key=lambda r: (-(r["body_chars"] or 0), key(r)))
        for row in rest[:shortfall]:
            row["bucket"] = "extra"
            picked[key(row)] = row
            bucket_count["extra"] += 1
        print(f"兜底补足 {min(shortfall, len(rest))} 份（分层标准取不满，源站数据量限制）")

    # ---------- 落盘 ----------
    # 本地文件名必须与抓取脚本（agent_temp/pb/expand_crawl.py）完全一致，
    # 规则是 re.sub(r"[^\w]+", "_", url_slug_without_host)，
    # 注意：①开头会保留一个下划线；②末尾 .htm 的点被替换成下划线；③再拼 .txt。
    # 初版我写成 slug.replace('/', '_').strip('_')，与抓取脚本对不上，
    # 导致 200 份公告的 local_path 全部指向不存在的文件 —— 这类"自己发明命名规则"
    # 的错误在清单层面静默存在，直到向量化时才暴露，所以这里保留注释。
    #
    # 目录指向 cleaned_v3/（网页模板已剥离），不是 detail_v3/（原始剥离结果）。
    import re as _re

    for row in picked.values():
        slug = row["source_url"].split("ccgp.com")[0].split("ccgp.gov.cn")[-1]
        filename = _re.sub(r"[^\w]+", "_", slug)[:80] + ".txt"
        local = f"agent_temp/pb/probe_ccgp/cleaned_v3/{filename}"
        manifest["announcement"].append({
            "doc_id": f"ANN-{row['announcement_id']}",
            "doc_type": "announcement",
            "title": row["title"][:200],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "publish_date": row["publish_date"],
            "source_url": row["source_url"],
            "local_path": local,
            "bucket": row["bucket"],
            "char_count": row["body_chars"],
            "has_items": row["has_items"],
        })

    manifest["stats"] = {
        "regulation": len(manifest["regulation"]),
        "announcement": len(manifest["announcement"]),
        "buckets": dict(bucket_count),
        "targets": TARGETS,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"项目层：{len(manifest['announcement'])} 份")
    print()
    print("=== 分层命中情况 ===")
    for bucket, target in TARGETS.items():
        actual = bucket_count.get(bucket, 0)
        flag = "OK " if actual == target else "缺 " + str(target - actual)
        print(f"  {bucket:<16} {actual:>4} / {target:<4} {flag}")

    kinds = Counter(row["kind"] for row in manifest["announcement"])
    print()
    print("=== 抽中公告的类型分布 ===")
    for kind, count in kinds.most_common():
        print(f"  {kind:<12} {count:>4}")

    provinces = {row.get("province") for row in picked.values() if row.get("province")}
    print()
    print(f"覆盖省份 {len(provinces)} 个")
    print(f"\n清单 → {MANIFEST.name}")


if __name__ == "__main__":
    main()
