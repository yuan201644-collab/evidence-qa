"""校验并自动修正规则条文的 source_snippet。

问题：RULES 里的条文用多行字符串手工拼接，拼接处容易漏字符或漏空格
     （实测 R004/R019 都是「。 」变「。」、「： 」变「：」）。

做法：不再手抄整段，改为「锚点 + 自动截取」——
    每条规则给 (起始锚点, 结束锚点)，脚本从法规原文里精确截出这一段，
    并断言截出的文字以起始锚点开头、以结束锚点结尾。

产出：把精确截取的条文写回 seed_rules.py 的 RULES（通过单独的 snippets 模块），
     再由 seed_rules.py 引用。

用法：python infra/scripts/fix_rule_snippets.py   （dry-run，只报告）
     python infra/scripts/fix_rule_snippets.py --write  （写回 rule_snippets.json）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

WS = Path(__file__).resolve().parents[2]
LAWS = WS / "agent_temp" / "pb" / "probe_ccgp" / "laws_regulation"
OUT_JSON = Path(__file__).resolve().parent / "rule_snippets.json"

F87 = "t20170718_8541199.htm.txt"
F74 = "t20140103_4650784.htm.txt"
F94 = "t20180103_9430153.htm.txt"
F110 = "t20220127_17561525.htm.txt"
F101 = "t20191211_13537333.htm.txt"

# rule_id -> (原文文件, 起始锚点, 结束锚点)
# 结束锚点用「该条最后一句的结尾」，脚本找到它并把这一段整体抽出
ANCHORS: dict[str, tuple[str, str, str]] = {
    "R001": (F87, "第三条 货物服务招标分为公开招标和邀请招标。", "分为公开招标和邀请招标。"),
    "R002": (F87, "邀请招标，是指采购人依法从符合相应资格条件的供应商中随机抽取3家以上供应商",
             "的方式邀请其参加投标的采购方式。"),
    "R003": (F87, "第四条 属于地方预算的政府采购项目", "县级公开招标数额标准。"),
    "R004": (F74, "第三条 采购人、采购代理机构采购以下货物、工程和服务之一的",
             "必须进行招标的工程建设项目以外的政府采购工程。"),
    "R005": (F74, "第四条 达到公开招标数额标准的货物、服务采购项目", "申请批准。"),
    "R006": (F74, "第二十七条 符合下列情形之一的采购项目，可以采用竞争性谈判方式采购：",
             "可以采用竞争性谈判方式采购："),
    "R007": (F74, "（三）非采购人所能预见的原因或者非采购人拖延造成采用招标所需时间不能满足用户紧急需要的；",
             "不能满足用户紧急需要的；"),
    "R008": (F74, "（四）因艺术品采购、专利、专有技术或者服务的时间、数量事先不能确定",
             "不能事先计算出价格总额的。"),
    "R009": (F74, "第十四条 采购人、采购代理机构可以要求供应商在提交响应文件截止时间之前交纳保证金",
             "保证金数额应当不超过采购项目预算的2%。"),
    "R010": (F74, "第十二条 采购人、采购代理机构应当通过发布公告", "参与竞争性谈判或者询价采购活动。"),
    "R011": (F74, "第二十九条 从谈判文件发出之日起至供应商提交首次响应文件截止之日止不得少于3个工作日。",
             "不得少于3个工作日。"),
    "R012": (F74, "第十八条 采购人或者采购代理机构应当在成交供应商确定后2个工作日内",
             "并将竞争性谈判文件、询价通知书随成交结果同时公告。"),
    "R013": (F74, "第三十八条 属于政府采购法第三十一条第一项情形", "公示期不得少于5个工作日，"),
    "R014": (F87, "第十六条 招标公告、资格预审公告的公告期限为5个工作日。",
             "公告期限自省级以上财政部门指定媒体最先发布公告之日起算。"),
    "R015": (F87, "第十八条 采购人或者采购代理机构应当按照招标公告、资格预审公告或者投标邀请书规定的时间",
             "计算不得少于5个工作日。"),
    "R016": (F87, "澄清或者修改的内容可能影响投标文件编制的",
             "采购人或者采购代理机构应当顺延提交投标文件的截止时间。"),
    "R017": (F87, "第十二条 采购人根据价格测算情况", "但不得设定最低限价。"),
    "R018": (F87, "第十三条 公开招标公告应当包括以下主要内容：", "公开招标公告应当包括以下主要内容："),
    "R019": (F87, "第二十九条 采购人、采购代理机构在发布招标公告、资格预审公告或者发出投标邀请书后",
             "应当及时在原公告发布媒体上发布终止公告，"),
    "R020": (F94, "第十条 供应商认为采购文件、采购过程、中标或者成交结果使自己的权益受到损害的",
             "以书面形式向采购人、采购代理机构提出质疑。"),
    "R021": (F94, "对采购文件提出质疑的，应当在获取采购文件或者采购文件公告期限届满之日起7个工作日内提出。",
             "7个工作日内提出。"),
    "R022": (F94, "第十三条 采购人、采购代理机构不得拒收质疑供应商在法定质疑期内发出的质疑函",
             "并以书面形式通知质疑供应商和其他有关供应商。"),
    "R023": (F94, "第十七条 质疑供应商对采购人、采购代理机构的答复不满意",
             "规定的财政部门提起投诉。"),
    "R024": (F94, "第六条 供应商投诉按照采购人所属预算级次，由本级财政部门处理。",
             "由本级财政部门处理。"),
    "R025": (F110, "第三条 符合下列情形之一的，可以采用框架协议采购方式采购：",
             "（四）国务院财政部门规定的其他情形。"),
    "R026": (F101, "第八条 中央预算单位政府采购信息应当在中国政府采购网发布",
             "的中国政府采购网省级分网发布。"),
}


def main() -> None:
    write = "--write" in sys.argv
    cache: dict[str, str] = {}
    snippets: dict[str, str] = {}
    problems: list[str] = []

    for rule_id, (filename, start_anchor, end_anchor) in ANCHORS.items():
        if filename not in cache:
            text = (LAWS / filename).read_text(encoding="utf-8")
            cache[filename] = re.sub(r"\s+", " ", text).strip()
        haystack = cache[filename]

        start = haystack.find(start_anchor)
        if start < 0:
            problems.append(f"{rule_id}: 起始锚点未找到")
            continue
        end = haystack.find(end_anchor, start)
        if end < 0:
            problems.append(f"{rule_id}: 结束锚点未找到")
            continue
        snippet = haystack[start:end + len(end_anchor)].strip()
        snippets[rule_id] = snippet
        print(f"  OK  {rule_id}  {len(snippet):>4} 字  {snippet[:36]}...")

    if problems:
        print()
        for problem in problems:
            print(f"  BAD {problem}")
        print(f"\n{len(problems)} 处锚点问题，未写入。")
        return

    print(f"\n{len(snippets)} 条条文从原文精确截取成功")

    if write:
        OUT_JSON.write_text(json.dumps(snippets, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"已写入 {OUT_JSON.name}")
    else:
        print("（dry-run，未写入；加 --write 落盘）")


if __name__ == "__main__":
    main()
