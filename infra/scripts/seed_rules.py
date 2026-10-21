"""阶段 2：政府采购核心规则结构化。

原则：
  1) 每条规则的 source_snippet **不由人书写**，而是由 fix_rule_snippets.py
     按「起始锚点 + 结束锚点」从法规原文里精确截取，结果存 rule_snippets.json；
  2) 本脚本只保存规则的**结构化描述**（字段映射、阈值、结论），
     以及指向原文的锚点坐标，条文正文从 rule_snippets.json 读取；
  3) 写库前再跑一次全文校验：rule_snippets.json 里的每条都必须能在原文中找到；
  4) condition_field 只填数据库里真实存在的字段，无对应字段的留空，表示"定性规则"。

为什么这么改：
     最初手抄 26 条条文，5 条校验失败——R026 整句是凭记忆编的，R004/R019/R025
     是拼接处漏了空格。人的记忆在多行拼接时不可靠，改为机器截取。

字段映射参考：
  projects.procurement_way   采购方式（公开招标/竞争性磋商/竞争性谈判/询价/单一来源/框架协议）
  projects.budget_yuan       预算金额（元）
  projects.budget_level      预算级次（central/local/unknown）
  awards.award_yuan          中标金额（元）
  announcements.kind         公告类型
  announcements.publish_date 发布日期
  items.*                    采购标的
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

import psycopg  # noqa: E402

WS = Path(__file__).resolve().parents[2]
LAWS = WS / "agent_temp" / "pb" / "probe_ccgp" / "laws_regulation"
DSN = "postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov"
SNIPPETS = Path(__file__).resolve().parent / "rule_snippets.json"

F87 = "t20170718_8541199.htm.txt"
F74 = "t20140103_4650784.htm.txt"
F94 = "t20180103_9430153.htm.txt"
F110 = "t20220127_17561525.htm.txt"
F101 = "t20191211_13537333.htm.txt"

BASE_URL = "http://www.ccgp.gov.cn/zcfg/mofgz/"

R87 = "财政部令第87号《政府采购货物和服务招标投标管理办法》"
R74 = "财政部令第74号《政府采购非招标采购方式管理办法》"
R94 = "财政部令第94号《政府采购质疑和投诉办法》"
R110 = "财政部令第110号《政府采购框架协议采购方式管理暂行办法》"
R101 = "财政部令第101号《政府采购信息发布管理办法》"

# rule_id, 法规, 条文号, 条件字段, 条件运算符, 条件值, 结论, 原文文件, 备注
RULES = [
    ("R001", R87, "第三条", "procurement_way", "in", "公开招标,邀请招标",
     "货物服务招标只有两种方式：公开招标、邀请招标", F87, "采购方式分类依据"),

    ("R002", R87, "第三条", "procurement_way", "=", "邀请招标",
     "邀请招标须从符合资格条件的供应商中随机抽取3家以上并发出投标邀请书", F87,
     "邀请招标的法定形式要件"),

    ("R003", R87, "第四条", "budget_level", "=", "local",
     "地方预算项目的公开招标数额标准由省级政府确定，分省/市/县三级，全国不统一", F87,
     "本库有 1991 个 local 项目，判断「是否达到公开招标标准」必须按项目所在省标准，不能套用单一数字"),

    ("R004", R74, "第三条", "procurement_way", "in", "竞争性谈判,单一来源,询价",
     "非招标采购方式适用于四类情形：集中采购目录内未达公开招标标准的货物服务；"
     "目录外但限额标准以上且未达公开招标标准的货物服务；"
     "达到公开招标标准但经批准采用非公开招标方式的货物服务；"
     "按招标投标法必须招标的工程建设项目以外的政府采购工程", F74,
     "非招标方式的适用边界，是本库非公开招标项目的合规判据"),

    ("R005", R74, "第四条", "procurement_way", "in", "竞争性谈判,单一来源,询价",
     "达到公开招标数额标准却拟采用非招标方式的，须在采购活动开始前经主管预算单位同意并报市级以上财政部门批准",
     F74, "批准程序要求"),

    ("R006", R74, "第二十七条", "procurement_way", "=", "竞争性谈判",
     "竞争性谈判的法定适用情形（共四项，见 R007、R008 及原文第一、二项）", F74,
     "本条为定性规则，具体情形分散在各项"),

    ("R007", R74, "第二十七条第三项", "procurement_way", "=", "竞争性谈判",
     "非采购人所能预见的原因或非采购人拖延导致招标所需时间不能满足用户紧急需要的，可采用竞争性谈判",
     F74, "紧急需求情形"),

    ("R008", R74, "第二十七条第四项", "procurement_way", "=", "竞争性谈判",
     "因艺术品采购、专利、专有技术或服务的时间数量事先不能确定等原因不能事先计算价格总额的，可采用竞争性谈判",
     F74, "技术复杂情形"),

    ("R009", R74, "第十四条", "projects.budget_yuan", "ratio_max", "0.02",
     "非招标方式下投标保证金不得超过采购项目预算的 2%", F74, "钱款类阈值，可计算"),

    ("R010", R74, "第十二条", "procurement_way", "in", "竞争性谈判,询价",
     "竞争性谈判或询价须邀请不少于 3 家符合资格条件的供应商参与", F74, "供应商数量下限"),

    ("R011", R74, "第二十九条", "announcements.kind", "=", "磋商公告",
     "从谈判文件发出之日起至供应商提交首次响应文件截止之日止不得少于 3 个工作日", F74,
     "时间下限"),

    ("R012", R74, "第十八条", "announcements.kind", "=", "成交公告",
     "成交供应商确定后 2 个工作日内应公告成交结果并发出成交通知书", F74, "成交公告时限"),

    ("R013", R74, "第三十八条", "procurement_way", "=", "单一来源",
     "达到公开招标数额的货物服务拟采用单一来源的，须在省级以上财政部门指定媒体公示，公示期不少于 5 个工作日",
     F74, "本库有法定单一来源项目，可据此核查公示期"),

    ("R014", R87, "第十六条", "announcements.kind", "=", "招标公告",
     "招标公告、资格预审公告的公告期限为 5 个工作日", F87, "公告期限硬指标"),

    ("R015", R87, "第十八条", "announcements.kind", "=", "招标公告",
     "招标文件提供期限自招标公告发布之日起不得少于 5 个工作日", F87, "提供期限下限"),

    ("R016", R87, "第二十七条", "announcements.kind", "=", "更正公告",
     "澄清或修改内容可能影响投标文件编制的，应在投标截止时间至少 15 日前书面通知；不足 15 日应顺延截止时间",
     F87, "本库有更正公告，可核查更正与投标截止的时间差"),

    ("R017", R87, "第十二条", "projects.budget_yuan", "cap", "最高限价",
     "采购人可在采购预算额度内设定最高限价，但不得设定最低限价", F87, "价格约束"),

    ("R018", R87, "第十三条", "announcements.kind", "=", "招标公告",
     "公开招标公告必须载明八项内容（采购人及代理机构信息、项目名称与预算金额、采购需求、"
     "投标人资格要求、获取招标文件的时间地点方式及售价、公告期限、投标截止与开标时间地点、联系人电话）",
     F87, "公告要素完整性判据，与 announcements 表字段对应"),

    ("R019", R87, "第二十九条", "announcements.kind", "=", "废标终止",
     "除因重大变故采购任务取消外，不得擅自终止招标；终止须在原公告媒体发布终止公告", F87,
     "本库有废标终止公告"),

    ("R020", R94, "第十条", "announcements.publish_date", "window_days", "7",
     "供应商可在知道或应知权益受损之日起 7 个工作日内以书面形式提出质疑", F94, "质疑期 7 个工作日"),

    ("R021", R94, "第十一条", "announcements.publish_date", "window_days", "7",
     "对采购文件提出质疑的，应在获取采购文件或采购文件公告期限届满之日起 7 个工作日内提出", F94,
     "质疑期起算点"),

    ("R022", R94, "第十三条", "announcements.publish_date", "window_days", "7",
     "采购人、代理机构应在收到质疑函后 7 个工作日内作出答复并书面通知质疑供应商", F94,
     "答复期 7 个工作日"),

    ("R023", R94, "第十七条", "announcements.publish_date", "window_days", "15",
     "对答复不满意或未在规定时间答复的，可在答复期满后 15 个工作日内向财政部门投诉", F94,
     "投诉期 15 个工作日"),

    ("R024", R94, "第六条", "budget_level", "=", "central",
     "投诉按采购人所属预算级次由本级财政部门处理", F94, "预算级次决定投诉受理机关"),

    ("R025", R110, "第三条", "procurement_way", "=", "框架协议",
     "框架协议采购适用四类情形：目录内品目配套耗材属小额零星采购的；目录外限额以上的鉴证咨询服务属小额零星采购的；"
     "目录外限额以上为系统外服务对象提供服务需确定2家以上供应商的；国务院财政部门规定的其他情形", F110,
     "本库有框架协议项目"),

    ("R026", R101, "第八条", "budget_level", "=", "central",
     "中央预算单位的政府采购信息应当在中国政府采购网发布；地方预算单位应当在所在行政区域的中国政府采购网省级分网发布",
     F101, "发布渠道按预算级次区分，与本库 budget_level 字段对应"),
]


def main() -> None:
    snippets: dict[str, str] = json.loads(SNIPPETS.read_text(encoding="utf-8"))

    print("=== 一、条文来源校验（snippet 必须逐字出现在原文中）===")
    cache: dict[str, str] = {}
    failures: list[str] = []
    for rule in RULES:
        rule_id, _, _, _, _, _, _, filename, _ = rule
        if filename not in cache:
            text = (LAWS / filename).read_text(encoding="utf-8")
            cache[filename] = " ".join(text.split())
        snippet = " ".join(snippets.get(rule_id, "").split())
        if not snippet:
            print(f"  BAD {rule_id}  无条文")
            failures.append(rule_id)
        elif snippet in cache[filename]:
            print(f"  OK  {rule_id}  {len(snippet):>4} 字")
        else:
            print(f"  BAD {rule_id}  条文不在原文中")
            failures.append(rule_id)

    if failures:
        print(f"\n{len(failures)} 条校验失败，拒绝写库。")
        return
    print(f"\n{len(RULES)} 条全部通过\n")

    print("=== 二、写库 ===")
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE regulation_rules")
            for rule in RULES:
                (rule_id, regulation, article, field, op, value,
                 conclusion, filename, note) = rule
                cur.execute("""
                    INSERT INTO regulation_rules
                    (rule_id, regulation, article, condition_field, condition_op,
                     condition_value, conclusion, source_snippet, source_document,
                     source_url, note)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (rule_id, regulation, article, field, op, value, conclusion,
                      snippets[rule_id],
                      f"agent_temp/pb/probe_ccgp/laws_regulation/{filename}",
                      BASE_URL, note))
        conn.commit()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM regulation_rules")
        print(f"  写入 {cur.fetchone()[0]} 条规则")
        cur.execute("SELECT regulation, count(*) FROM regulation_rules GROUP BY regulation ORDER BY 2 DESC")
        print()
        print("=== 三、按法规分布 ===")
        for regulation, count in cur.fetchall():
            print(f"  {count:>4}  {regulation}")
        cur.execute("SELECT condition_field, count(*) FROM regulation_rules GROUP BY condition_field ORDER BY 2 DESC")
        print()
        print("=== 四、按可判定字段分布 ===")
        for field, count in cur.fetchall():
            print(f"  {count:>4}  {field}")


if __name__ == "__main__":
    main()
