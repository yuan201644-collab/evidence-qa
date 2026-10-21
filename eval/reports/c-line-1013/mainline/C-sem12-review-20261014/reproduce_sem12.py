# -*- coding: utf-8 -*-
"""离线复现 sem-12：用本地 Chinook 数据在 sqlite 中对比 gold / pred / 修正版三条 SQL。

目的：确定性判定 27 vs 51 的差异来源 —— 是 EXCEPT/NOT IN 的口径差异，
还是预测 SQL 的连接条件错误。
"""
import re
import sqlite3
from pathlib import Path

ROOT = Path(r"C:\Users\ASUS\WorkBuddy\askdata-agent")
SRC = ROOT / "infra" / "initdb" / "02_chinook.sql"


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )


def split_statements(text: str):
    stmts, buf, in_str = [], [], False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            buf.append(ch)
            if in_str and i + 1 < n and text[i + 1] == "'":
                buf.append(text[i + 1])
                i += 2
                continue
            in_str = not in_str
        elif ch == ";" and not in_str:
            stmts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    if "".join(buf).strip():
        stmts.append("".join(buf))
    return stmts


def strip_n_prefix(text: str) -> str:
    """去掉 PostgreSQL 字符串字面量的 N 前缀；字符串内部（如 'Guns N'' Roses'）不动。"""
    out, in_str = [], False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append(text[i + 1])
                    i += 2
                    continue
                in_str = False
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch in "Nn" and i + 1 < n and text[i + 1] == "'":
            j = len(out) - 1
            if j >= 0 and (out[j].isalnum() or out[j] == "_"):
                out.append(ch)
                i += 1
                continue
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def build_db() -> sqlite3.Connection:
    text = strip_comments(SRC.read_text(encoding="utf-8"))
    text = re.sub(r"\\c\s+\w+", "", text)
    stmts = split_statements(text)
    con = sqlite3.connect(":memory:")
    cur = con.cursor()
    counts = {"create_table": 0, "insert": 0, "index": 0}
    skipped = []
    for s in stmts:
        st = s.strip()
        if not st:
            continue
        low = st.lower()
        if low.startswith("create table"):
            kind = "create_table"
        elif low.startswith("insert into"):
            kind = "insert"
        elif low.startswith("create index"):
            kind = "index"
        else:
            skipped.append(st[:70].replace("\n", " "))
            continue
        st2 = re.sub(r"\bpublic\.", "", st)
        st2 = re.sub(r"\s+USING\s+btree\b", "", st2, flags=re.I)
        st2 = strip_n_prefix(st2)
        try:
            cur.execute(st2)
            counts[kind] += 1
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"[FAIL {type(exc).__name__}] {st[:70]}")
    con.commit()
    print("executed:", counts, "| skipped:", len(skipped))
    for s in skipped:
        print("   skip:", s[:130].replace("\n", " "))
    for t in ("customer", "invoice", "invoice_line", "track", "genre"):
        try:
            print(f"   {t}: {cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}")
        except Exception as exc:  # noqa: BLE001
            print(f"   {t}: ERR {exc}")
    return con


GOLD = (
    "SELECT DISTINCT i.customer_id FROM invoice i "
    "JOIN invoice_line il ON il.invoice_id=i.invoice_id "
    "JOIN track t ON t.track_id=il.track_id "
    "JOIN genre g ON g.genre_id=t.genre_id "
    "WHERE g.name='Rock' AND i.customer_id NOT IN ("
    "SELECT i2.customer_id FROM invoice i2 "
    "JOIN invoice_line il2 ON il2.invoice_id=i2.invoice_id "
    "JOIN track t2 ON t2.track_id=il2.track_id "
    "JOIN genre g2 ON g2.genre_id=t2.genre_id "
    "WHERE g2.name='Jazz') ORDER BY i.customer_id"
)

PRED = (
    "SELECT DISTINCT c.customer_id FROM customer AS c "
    "JOIN invoice AS i ON c.customer_id = i.customer_id "
    "JOIN invoice_line AS il ON i.invoice_id = il.invoice_id "
    "JOIN track AS t ON il.track_id = t.track_id "
    "JOIN genre AS g ON t.genre_id = g.genre_id "
    "WHERE g.name = 'Rock' EXCEPT "
    "SELECT DISTINCT c.customer_id FROM customer AS c "
    "JOIN invoice AS i ON c.customer_id = i.invoice_id "
    "JOIN invoice_line AS il ON i.invoice_id = il.invoice_id "
    "JOIN track AS t ON il.track_id = t.track_id "
    "JOIN genre AS g ON t.genre_id = g.genre_id "
    "WHERE g.name = 'Jazz' ORDER BY customer_id LIMIT 1000"
)

# 只修正第二分支那处连接条件（该串在第一分支不出现）
PRED_FIXED = PRED.replace(
    "ON c.customer_id = i.invoice_id", "ON c.customer_id = i.customer_id"
)

# 反向对照：把 gold 改写成 EXCEPT（正确连接），验证 EXCEPT 与 NOT IN 等价
GOLD_AS_EXCEPT = (
    "SELECT DISTINCT i.customer_id FROM invoice i "
    "JOIN invoice_line il ON il.invoice_id=i.invoice_id "
    "JOIN track t ON t.track_id=il.track_id "
    "JOIN genre g ON g.genre_id=t.genre_id "
    "WHERE g.name='Rock' EXCEPT "
    "SELECT DISTINCT i2.customer_id FROM invoice i2 "
    "JOIN invoice_line il2 ON il2.invoice_id=i2.invoice_id "
    "JOIN track t2 ON t2.track_id=il2.track_id "
    "JOIN genre g2 ON g2.genre_id=t2.genre_id "
    "WHERE g2.name='Jazz' ORDER BY customer_id"
)


def run(con, label, sql):
    sql = sql.replace("ORDER BY customer_id", "ORDER BY 1")
    try:
        rows = con.execute(sql).fetchall()
        ids = [r[0] for r in rows]
        print(f"\n[{label}] rows={len(ids)}")
        print(f"   ids={ids[:40]}{' ...' if len(ids) > 40 else ''}")
        return ids
    except Exception as exc:  # noqa: BLE001
        print(f"\n[{label}] ERROR {type(exc).__name__}: {exc}")
        return None


def main():
    con = build_db()
    gold = run(con, "GOLD (NOT IN, 参考)", GOLD)
    gold_ex = run(con, "GOLD 改写为 EXCEPT（正确连接）", GOLD_AS_EXCEPT)
    pred = run(con, "PRED (原始系统 SQL)", PRED)
    fixed = run(con, "PRED 修正第二分支连接条件", PRED_FIXED)
    print("\n=== 判定 ===")
    if gold is not None and gold_ex is not None:
        print(f"NOT IN 与 EXCEPT（均正确连接）等价: {set(gold) == set(gold_ex)}"
              f"  ({len(gold)} vs {len(gold_ex)})")
    if pred is not None and gold is not None:
        print(f"原始 PRED 与 GOLD 差异: {len(pred)} vs {len(gold)}"
              f" | 差集(PRED-GOLD)={len(set(pred) - set(gold))}"
              f" | 差集(GOLD-PRED)={len(set(gold) - set(pred))}")
    if fixed is not None and gold is not None:
        print(f"修正连接后与 GOLD 一致: {set(fixed) == set(gold)}"
              f"  ({len(fixed)} vs {len(gold)})")


if __name__ == "__main__":
    main()
