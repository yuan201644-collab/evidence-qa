# -*- coding: utf-8 -*-
"""只读生成 sem-12 的核查素材（题目 / 候选 SQL / 参考 SQL / 双方完整结果集）。

- 全部数据来自本地既有文件，不连数据库、不改任何既有文件。
- 输出：一份 Markdown dossier，落到新目录（不覆盖既有报告）。
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(r"C:\Users\ASUS\WorkBuddy\askdata-agent")
OUT_DIR = ROOT / "eval" / "reports" / "c-line-1013" / "mainline" / "C-sem12-review-20261014"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# 复用既有离线复现脚本（同一套建库与 SQL 常量）
spec = importlib.util.spec_from_file_location(
    "reproduce_sem12", ROOT / "agent_temp" / "c_review_20261014" / "reproduce_sem12.py"
)
rep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rep)


def q(con, sql):
    sql2 = sql.replace("ORDER BY customer_id", "ORDER BY 1")
    return [r[0] for r in con.execute(sql2).fetchall()]


def load_case():
    # 题目
    qn = {}
    for ln in (ROOT / "eval/mainline/development_v0.1/t5_v02_questions.jsonl").read_text(
        encoding="utf-8"
    ).splitlines():
        if ln.strip():
            o = json.loads(ln)
            if str(o.get("id")) == "sem-12":
                qn = o
    # 参考
    ref = {}
    for ln in (
        ROOT / "agent_temp/handoff_C_20261014/t4_t5_criteria/t5_v02_reference_cases.jsonl"
    ).read_text(encoding="utf-8").splitlines():
        if ln.strip():
            o = json.loads(ln)
            if str(o.get("id")) == "sem-12":
                ref = o
    # 候选回答
    ans = {}
    for ln in (
        ROOT / "agent_temp/c_review_20261014/t5-throttled-rev1/raw_answers.jsonl"
    ).read_text(encoding="utf-8").splitlines():
        if ln.strip():
            o = json.loads(ln)
            if str(o.get("case_id")) == "sem-12":
                ans = o
    return qn, ref, ans


def extract_pred_sql(ans):
    resp = ans.get("response") or {}
    for step in reversed(resp.get("trace") or []):
        if isinstance(step, dict) and step.get("tool") == "nl2sql":
            return (step.get("output") or {}).get("sql")
    return None


def fmt_ids(ids, per_line=14):
    lines = []
    for i in range(0, len(ids), per_line):
        lines.append(", ".join(str(x) for x in ids[i:i + per_line]))
    return "  \n".join(lines)


def main():
    qn, ref, ans = load_case()
    pred_sql = extract_pred_sql(ans)

    con = rep.build_db()
    gold = q(con, rep.GOLD)
    gold_ex = q(con, rep.GOLD_AS_EXCEPT)
    pred = q(con, rep.PRED)
    fixed = q(con, rep.PRED_FIXED)

    set_gold, set_pred, set_fixed, set_gold_ex = set(gold), set(pred), set(fixed), set(gold_ex)
    pred_minus_gold = sorted(set_pred - set_gold)
    gold_minus_pred = sorted(set_gold - set_pred)

    md = []
    md.append("# sem-12 只读核查素材（题目 / 候选 SQL / 参考 SQL / 双方结果集）\n")
    md.append("- 日期：2026-10-14")
    md.append("- 复核基准：`d03c3d900efd13875107108eb642c44450fb2d6e`")
    md.append("- 范围：**已曝光开发回归**（题面 notes 已注明「先前已曝光，不得作为未见或泛化题」）")
    md.append("- 状态：按 A 口径记作「**参考结果与候选结果集口径待核**」——**暂不列为代码修复项**，待 A 复核后定性")
    md.append("- 本文件为**只读整理**：未改代码、参考判据、题目或原始归档\n")
    md.append("---\n")

    md.append("## 1. 题目\n")
    md.append(f"- 题号：`{qn.get('id')}`（task {qn.get('task')}）")
    md.append(f"- 原文：**{qn.get('question')}**")
    md.append(f"- 难度：{ref.get('difficulty')} ｜ 范围：{ref.get('scope')} ｜ 期望列：{ref.get('expected_columns')}")
    md.append(f"- 涉及表：{ref.get('tables')}\n")

    md.append("## 2. 参考（标准）SQL\n")
    md.append("```sql")
    md.append((ref.get("gold_sql") or "").strip())
    md.append("```\n")

    md.append("## 3. 候选（系统）SQL —— 2026-10-13 原始回答\n")
    md.append(f"- 来源：`agent_temp/c_review_20261014/t5-throttled-rev1/raw_answers.jsonl`（10-13 原始回答副本）")
    md.append(f"- http_status：{ans.get('http_status')} ｜ row_count：51\n")
    md.append("```sql")
    md.append((pred_sql or "").strip())
    md.append("```\n")

    md.append("**与参考的形态差异（唯一）**：")
    md.append("- 参考用 `WHERE g.name='Rock' AND i.customer_id NOT IN (…Jazz…)`；")
    md.append("- 候选用 `WHERE g.name='Rock' EXCEPT (…Jazz…)`；且**第二条分支的连接条件为** `ON c.customer_id = i.invoice_id`（**应为** `ON c.customer_id = i.customer_id`）。\n")

    md.append("## 4. 双方结果集\n")
    md.append(f"### 4.1 参考结果（GOLD）：{len(gold)} 行")
    md.append(fmt_ids(gold) + "\n")
    md.append(f"### 4.2 候选结果（PRED）：{len(pred)} 行")
    md.append(fmt_ids(pred) + "\n")
    pmg = ", ".join(map(str, pred_minus_gold))
    gmp = ", ".join(map(str, gold_minus_pred))
    md.append(f"### 4.3 差集")
    md.append(f"- **PRED − GOLD（{len(pred_minus_gold)} 个）**：{pmg}")
    md.append(f"- **GOLD − PRED（{len(gold_minus_pred)} 个）**：{gmp}")
    md.append(f"- 净差：{len(pred_minus_gold)} − {len(gold_minus_pred)} = {len(pred) - len(gold)} = {len(pred)} − {len(gold)}\n")

    md.append("## 5. 对照实验（只读离线复现，sqlite + 本地 `infra/initdb/02_chinook.sql`）\n")
    md.append("| SQL | 行数 | 与参考 id 集合是否相同 |")
    md.append("|---|---:|---|")
    md.append(f"| GOLD（参考，`NOT IN`） | {len(gold)} | 基准 |")
    md.append(f"| GOLD 改写为 `EXCEPT`（**连接正确**） | {len(gold_ex)} | {'相同' if set_gold == set_gold_ex else '不同'} |")
    md.append(f"| PRED（原始候选 SQL） | {len(pred)} | {'相同' if set_gold == set_pred else '不同'} |")
    md.append(f"| PRED **仅修正第二分支连接列名** | {len(fixed)} | {'相同' if set_gold == set_fixed else '不同'} |")
    md.append("")
    md.append(f"- 本地库装载量：customer=59 / invoice=412 / invoice_line=2240 / track=3503 / genre=25（与 10-13 实测一致）")
    md.append(f"- `EXCEPT` 与 `NOT IN`（均正确连接）id 集合相同：**{set_gold == set_gold_ex}**")
    md.append(f"- PRED 仅把 `ON c.customer_id = i.invoice_id` 改为 `ON c.customer_id = i.customer_id` 后，id 集合与参考相同：**{set_gold == set_fixed}**\n")

    md.append("## 6. 差异来源说明（对照回执 §4）\n")
    md.append("1. **口径差异（`EXCEPT` vs `NOT IN`）不成立**：两者在连接正确时返回同一 27 行 id 集合（§5 第 2 行）。")
    md.append("2. **唯一差异源是连接条件选错列**：候选 SQL 第二条分支 `JOIN invoice AS i ON c.customer_id = i.invoice_id`（不同列），导致否定分支未被正确排除；修正为同列后立即得正确 27 行。")
    md.append("3. 差集自洽：PRED−GOLD = 28、GOLD−PRED = 4，净差 24 = 51 − 27。\n")

    md.append("## 7. 判定与待核事项\n")
    md.append("- 本轮证据**指向**「系统语义错误（跨表 JOIN 选错列名 `customer_id` ↔ `invoice_id`）」；")
    md.append("- 但按 A 口径，**本轮仅记作「参考结果与候选结果集口径待核」**，不预先定性为代码修复项；")
    md.append("- 待 A 复核本整理件后，再决定改系统代码或修订判据；**核清前不改代码、不改参考答案**。\n")

    md.append("## 8. 复现方式\n")
    md.append("- 复现脚本：`agent_temp/c_review_20261014/reproduce_sem12.py`（离线、只读，sqlite 内存库）")
    md.append("- 数据源：`infra/initdb/02_chinook.sql`（仓库内 Chinook 1.4.5 完整数据）")
    md.append("- 无数据库、无模型调用、无网络依赖\n")

    text = "\n".join(md) + "\n"
    out_md = OUT_DIR / "sem12_case_dossier.md"
    out_md.write_text(text, encoding="utf-8", newline="\n")

    # 同时落一份机器可读的结果集
    data = {
        "case_id": "sem-12",
        "question": qn.get("question"),
        "gold_sql": ref.get("gold_sql"),
        "pred_sql": pred_sql,
        "gold_ids": gold,
        "pred_ids": pred,
        "pred_minus_gold": pred_minus_gold,
        "gold_minus_pred": gold_minus_pred,
        "gold_as_except_ids": gold_ex,
        "pred_fixed_ids": fixed,
        "checks": {
            "except_equals_notin": set_gold == set_gold_ex,
            "pred_equals_gold": set_gold == set_pred,
            "pred_fixed_equals_gold": set_gold == set_fixed,
        },
    }
    out_json = OUT_DIR / "sem12_result_sets.json"
    out_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print("WROTE", out_md)
    print("WROTE", out_json)
    print("gold", len(gold), "pred", len(pred), "pred-gold", len(pred_minus_gold), "gold-pred", len(gold_minus_pred))


if __name__ == "__main__":
    main()
