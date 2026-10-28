# -*- coding: utf-8 -*-
"""v18a：答案与账本的数一致性核验（治 v17 的 M-07 型错误）。

要解决的具体问题
----------------
v17 全量里出现了一类最危险的错误：

    M-07  账本算对 7 项
          模型答：「根据结构化记录聚合（确定性计算）的信息……共有 **3 项**」

它**借用了权威措辞，却写了错数字**。读者看到「确定性计算」就会采信，
比直接答错危害更大。而 v17 的机制对此完全无感：注入发生、prompt 也给了优先级，
但没有任何环节去核对"模型最终写出来的数"和"账本算出来的数"是否一致。

做法
----
生成之后做一次**纯字符串比对**（不调模型）：

    ① 从答案里抽「总数」主张（共有 N 项 / 共 N 项 / 合计 N 项 …）
    ② 从账本 groups 取合法总数集合（每个文档的 total，以及多文档的合计）
    ③ 主张不在合法集合里 ⇒ 判为矛盾

只核「总数」一层。类别分布、编号明细暂不核——那需要锚点抽取，
属于 v14/v15 的活，混进来会让归因不清（预注册 §1 单变量纪律）。

边界
----
- **不修改模型答案**。检出矛盾时只返回判定，由调用方决定是否回灌重答。
- 答案里抽不到总数（纯解释型回答）时，`ok=True` 且 `reason="no_total"`，
  不误伤 D/B 层。
- 账本 groups 为空时不判定。

用法
----
    # 离线自测（零请求）：对既有 31 题答案跑一遍，看触发率与命中率
    D:/python/python.exe build/feva_override/verify.py --run reports/_run4arm_arm2_v17_full.json
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.dirname(HERE)

# 总数主张的句式，按可信度排序。
# 「共 10 项」优先于「合计 10 项」，因为后者常出现在分类小计里；
# 但两者都先收，再看是否落在合法集合内。
_TOTAL_PATS = [
    r"共有\s*(\d+)\s*(?:个)?\s*项",
    r"共承担了?\s*(\d+)\s*(?:个)?\s*项",
    r"共\s*(\d+)\s*(?:个)?\s*项",
    r"总计\s*(\d+)\s*(?:个)?\s*项",
    r"合计\s*(\d+)\s*(?:个)?\s*项",
    r"共有\s*(\d+)\s*个?项目",
    r"共\s*(\d+)\s*个?项目",
    r"(\d+)\s*(?:个)?\s*项(?:项目)?[。，,\n]",
]


def _clean(txt):
    """全角数字与空格归一，避免「７ 项」这类抽不到。"""
    if not txt:
        return ""
    out = []
    for ch in txt:
        if "\uff10" <= ch <= "\uff19":
            out.append(chr(ord(ch) - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out)
    return re.sub(r"[ \t\u3000]+", " ", s)


def extract_claimed_total(answer, window=400):
    """抽答案里最靠前的「总数」主张。返回 (int|None, 原文片段)。

    只看开头 window 字：总数出现在开头的概率最高，靠后通常是分类明细。
    """
    s = _clean(answer)[:window]
    best = None
    for pat in _TOTAL_PATS:
        m = re.search(pat, s)
        if m:
            pos = m.start()
            if best is None or pos < best[0]:
                best = (pos, int(m.group(1)), m.group(0))
    if best is None:
        return None, ""
    return best[1], best[2]


_CAT_WORDS = ("类", "规划", "青年", "重点", "重大", "论文", "基础", "应用",
              "研究", "课题", "专项", "一般", "自筹", "面上")


def _subject_before(s, pos, span=14):
    return s[max(0, pos - span):pos]


def _is_cat_subject(subject):
    """「共有」前面的主语是类别还是单位/整体。

    M-07「中国石油大学胜利学院共有3项」——主语是单位 ⇒ 这个数是**总数**
    M-09「『重大』类项目共有4项」        ——主语带类别词 ⇒ 是**类别小计**
    M-15「规划项目共有3项」             ——同上

    离线自测证明这个区分是必须的：只比总数会把 M-09/M-15 的正确回答误判，
    只把类别小计也放进合法集合又会把 M-07 的真错漏掉。
    """
    return any(w in subject for w in _CAT_WORDS)


def extract_claim(answer, window=400):
    """抽答案里最靠前的一条「N 项」主张。

    返回 (value|None, kind, snippet)，kind ∈ {"total", "cat"}。
    只扫开头 window 字：总数/主结论几乎总在开头，靠后是明细。
    """
    s = _clean(answer)[:window]
    best = None
    for pat in _TOTAL_PATS:
        for m in re.finditer(pat, s):
            if best is None or m.start() < best[0]:
                best = (m.start(), int(m.group(1)), m.group(0))
            break                      # 每个句式只取首次出现即可
    if best is None:
        return None, "total", ""
    pos, val, snip = best
    kind = "cat" if _is_cat_subject(_subject_before(s, pos)) else "total"
    return val, kind, snip


def extract_claimed_total(answer, window=400):
    """兼容旧签名：只返回数值与片段。"""
    val, _kind, snip = extract_claim(answer, window)
    return val, snip


def acceptable_totals(result, with_cats=False):
    """账本允许的「总数」集合：各文档 total ∪ 多文档合计。"""
    if not result:
        return set()
    groups = result.get("groups") or []
    if not groups:
        return set()
    per = {int(g.get("total") or 0) for g in groups if int(g.get("total") or 0) > 0}
    if not per:
        return set()
    ok = set(per)
    if len(per) > 1:
        ok.add(sum(per))
    if with_cats:
        ok |= category_counts(result)
    return ok


def category_counts(result):
    """账本里出现的所有类别小计值（只用于核对「某类共有 N 项」这类主张）。"""
    out = set()
    for g in (result or {}).get("groups") or []:
        for _label, cnt in (g.get("by_cat") or {}).items():
            for v in (cnt or {}).values():
                try:
                    out.add(int(v))
                except (TypeError, ValueError):
                    pass
    return out


_CLAIM_MARKS = ("确定性计算", "结构化记录聚合", "确定性计算结果", "由结构化记录做集合运算")


def claims_ledger(answer):
    """答案有没有**声称**自己的数字来自账本。

    这是判定的前置条件，不是可有可无的修饰。
    离线自测（v17 全量 31 题）证明：不加这个门槛会有 4/6 误报——
    D-03/M-02/M-09/M-15 问的是「某类多少项」或「两年度相差」，
    而账本 groups 给的「total」是整份文档的记录数，两者根本不同量纲，
    拿来比必然对不上。

    加上门槛后，只核「借了账本措辞」的答案——这正是 K2 要治的 M-07 型错误：
    借用权威措辞 + 写错数字。
    """
    a = answer or ""
    return any(m in a for m in _CLAIM_MARKS)


def check(answer, result, question="", require_claim=True):
    """返回 dict：ok / claimed / kind / allowed / reason。不抛异常。

    判定分两步（缺一步都会误伤，见 extract_claim 与 claims_ledger 的注释）：
      ① 答案有没有**声称**数字来自账本——没有就不核，避免拿"整份文档总数"
         去比"某类小计"这种不同量纲的误报；
      ② 这个数说的是**总数**还是**类别小计**，分别对应两套合法集合。
    """
    out = {"ok": True, "claimed": None, "kind": None, "allowed": [],
           "reason": "no_ledger", "snippet": "", "claims": claims_ledger(answer)}
    if not (result or {}).get("groups"):
        return out

    if require_claim and not out["claims"]:
        out["reason"] = "no_claim"
        out["allowed"] = sorted(acceptable_totals(result))
        return out

    val, kind, snip = extract_claim(answer)
    out["claimed"], out["kind"], out["snippet"] = val, kind, snip
    allowed = category_counts(result) if kind == "cat" else acceptable_totals(result)
    out["allowed"] = sorted(allowed)

    if not allowed:
        out["reason"] = "no_scope"
        return out
    if val is None:
        out["reason"] = "no_total"
        return out
    if val in allowed:
        out["reason"] = "match"
        return out

    out["ok"] = False
    out["reason"] = "mismatch"
    return out


def corrective_text(verdict):
    """回灌用的纠正指令。刻意简短，避免又变成一段可被忽略的长文。"""
    return (
        "\n\n【一致性核验】你上一版回答给出的总数是 %s，"
        "而「结构化记录聚合（确定性计算）」给出的确定性结论是 %s。"
        "两者不一致。请以确定性结论为准重写答案，"
        "总数与分类必须与该结论逐字一致，不得再依据其他引用片段自行清点。"
        % (verdict.get("claimed"), "/".join(str(x) for x in verdict.get("allowed") or []))
    )


# ---------------------------------------------------------------- 离线自测

def _ledger():
    """按文件路径直接加载 ledger.py。

    不能 `from feva_override.ledger import Ledger`——包 `__init__` 会连带
    import executor，而 executor 依赖容器内的 `kag`，在本地跑不起来。
    ledger 本身只用标准库，路径加载即可。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "feva_ledger_standalone", os.path.join(HERE, "ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for p in (os.path.join(HERE, "records.json"),
              os.path.join(BUILD, "_corpus_probe", "_records.json")):
        if os.path.exists(p):
            return mod.Ledger(p)
    return mod.Ledger()


def _gold_map():
    p = os.path.join(os.path.dirname(BUILD), "qbank_v4.json")
    if not os.path.exists(p):
        return {}
    q = json.load(open(p, encoding="utf-8"))["questions"]
    return {x["qid"]: x for x in q}


def _score_ok(qid, answer, gold):
    """粗判：gold 里的首个整数有没有出现在答案里。仅用于看触发方向，不是正式判分。"""
    nums = re.findall(r"\d+", str(gold or ""))
    if not nums:
        return None
    return nums[0] in (answer or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    L = _ledger()
    gold = _gold_map()
    d = json.load(open(args.run, encoding="utf-8"))
    items = d.get("items") or []
    if args.limit:
        items = items[: args.limit]

    hits = trg = 0
    print("%-7s %-9s %-6s %-8s %s" % ("qid", "判定", "主张", "账本", "备注"))
    print("-" * 74)
    for it in items:
        q = it.get("question") or ""
        a = it.get("answer") or ""
        r = L.query(q)
        v = check(a, r, q)
        mark = "OK" if v["ok"] else "**矛盾**"
        note = ""
        if not v["ok"]:
            trg += 1
            g = gold.get(it.get("qid"), {})
            before = _score_ok(it.get("qid"), a, g.get("gold"))
            note = "答案%s gold" % ("含" if before else "不含")
            if before is False:
                hits += 1
                note += " ← 检出的错被独立证实"
        print("%-7s %-9s %-6s %-8s %s" % (
            it.get("qid"), mark, v["claimed"], v["allowed"], note))

    print("-" * 74)
    print("触发 %d / %d；其中答案本身就不含 gold 首个数字的 %d 例（检出方向正确）"
          % (trg, len(items), hits))


if __name__ == "__main__":
    main()
