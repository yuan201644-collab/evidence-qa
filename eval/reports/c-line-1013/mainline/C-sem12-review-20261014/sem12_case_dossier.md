# sem-12 只读核查素材（题目 / 候选 SQL / 参考 SQL / 双方结果集）

- 日期：2026-10-14
- 复核基准：`d03c3d900efd13875107108eb642c44450fb2d6e`
- 范围：**已曝光开发回归**（题面 notes 已注明「先前已曝光，不得作为未见或泛化题」）
- 状态：**已定性为系统问题**（A 2026-10-15 裁定：按「候选 SQL 连接列错误」登记）。
  原 2026-10-14 的「参考结果与候选结果集口径待核」状态**到此结束**，其下内容原样保留作为核清过程记录。
- 根因与最小修复 / 回归方案见 `../C-sem12-fixplan-20261015/sem12_最小修复与回归方案.md`
  （实测结论：**不是校验规则缺失，而是已有 JOIN 外键校验因开关默认关闭而从未执行**）
- 本文件为**只读整理**：未改代码、参考判据、题目或原始归档

---

## 1. 题目

- 题号：`sem-12`（task 5）
- 原文：**买过Rock但从未买过Jazz的客户有哪些？返回客户编号，按编号升序，不重复。**
- 难度：hard ｜ 范围：multi_table ｜ 期望列：['customer_id']
- 涉及表：['genre', 'invoice', 'invoice_line', 'track']

## 2. 参考（标准）SQL

```sql
SELECT DISTINCT i.customer_id FROM invoice i JOIN invoice_line il ON il.invoice_id=i.invoice_id JOIN track t ON t.track_id=il.track_id JOIN genre g ON g.genre_id=t.genre_id WHERE g.name='Rock' AND i.customer_id NOT IN (SELECT i2.customer_id FROM invoice i2 JOIN invoice_line il2 ON il2.invoice_id=i2.invoice_id JOIN track t2 ON t2.track_id=il2.track_id JOIN genre g2 ON g2.genre_id=t2.genre_id WHERE g2.name='Jazz') ORDER BY i.customer_id;
```

## 3. 候选（系统）SQL —— 2026-10-13 原始回答

- 来源：`agent_temp/c_review_20261014/t5-throttled-rev1/raw_answers.jsonl`（10-13 原始回答副本）
- http_status：200 ｜ row_count：51

```sql
SELECT DISTINCT c.customer_id FROM customer AS c JOIN invoice AS i ON c.customer_id = i.customer_id JOIN invoice_line AS il ON i.invoice_id = il.invoice_id JOIN track AS t ON il.track_id = t.track_id JOIN genre AS g ON t.genre_id = g.genre_id WHERE g.name = 'Rock' EXCEPT SELECT DISTINCT c.customer_id FROM customer AS c JOIN invoice AS i ON c.customer_id = i.invoice_id JOIN invoice_line AS il ON i.invoice_id = il.invoice_id JOIN track AS t ON il.track_id = t.track_id JOIN genre AS g ON t.genre_id = g.genre_id WHERE g.name = 'Jazz' ORDER BY customer_id LIMIT 1000
```

**与参考的形态差异（唯一）**：
- 参考用 `WHERE g.name='Rock' AND i.customer_id NOT IN (…Jazz…)`；
- 候选用 `WHERE g.name='Rock' EXCEPT (…Jazz…)`；且**第二条分支的连接条件为** `ON c.customer_id = i.invoice_id`（**应为** `ON c.customer_id = i.customer_id`）。

## 4. 双方结果集

### 4.1 参考结果（GOLD）：27 行
1, 2, 4, 6, 8, 9, 10, 11, 12, 13, 15, 24, 25, 26  
27, 28, 29, 33, 34, 36, 41, 45, 47, 48, 52, 55, 57

### 4.2 候选结果（PRED）：51 行
1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 16, 17, 18, 20  
21, 22, 23, 24, 25, 27, 28, 29, 30, 31, 32, 33, 34, 35  
36, 37, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50  
51, 52, 53, 54, 55, 56, 57, 58, 59

### 4.3 差集
- **PRED − GOLD（28 个）**：3, 7, 16, 17, 18, 20, 21, 22, 23, 30, 31, 32, 35, 37, 39, 40, 42, 43, 44, 46, 49, 50, 51, 53, 54, 56, 58, 59
- **GOLD − PRED（4 个）**：4, 13, 15, 26
- 净差：28 − 4 = 24 = 51 − 27

## 5. 对照实验（只读离线复现，sqlite + 本地 `infra/initdb/02_chinook.sql`）

| SQL | 行数 | 与参考 id 集合是否相同 |
|---|---:|---|
| GOLD（参考，`NOT IN`） | 27 | 基准 |
| GOLD 改写为 `EXCEPT`（**连接正确**） | 27 | 相同 |
| PRED（原始候选 SQL） | 51 | 不同 |
| PRED **仅修正第二分支连接列名** | 27 | 相同 |

- 本地库装载量：customer=59 / invoice=412 / invoice_line=2240 / track=3503 / genre=25（与 10-13 实测一致）
- `EXCEPT` 与 `NOT IN`（均正确连接）id 集合相同：**True**
- PRED 仅把 `ON c.customer_id = i.invoice_id` 改为 `ON c.customer_id = i.customer_id` 后，id 集合与参考相同：**True**

## 6. 差异来源说明（对照回执 §4）

1. **口径差异（`EXCEPT` vs `NOT IN`）不成立**：两者在连接正确时返回同一 27 行 id 集合（§5 第 2 行）。
2. **唯一差异源是连接条件选错列**：候选 SQL 第二条分支 `JOIN invoice AS i ON c.customer_id = i.invoice_id`（不同列），导致否定分支未被正确排除；修正为同列后立即得正确 27 行。
3. 差集自洽：PRED−GOLD = 28、GOLD−PRED = 4，净差 24 = 51 − 27。

## 7. 判定与待核事项

- 本轮证据**指向**「系统语义错误（跨表 JOIN 选错列名 `customer_id` ↔ `invoice_id`）」；
- 但按 A 口径，**本轮仅记作「参考结果与候选结果集口径待核」**，不预先定性为代码修复项；
- 待 A 复核本整理件后，再决定改系统代码或修订判据；**核清前不改代码、不改参考答案**。

## 8. 复现方式

- 复现脚本：`agent_temp/c_review_20261014/reproduce_sem12.py`（离线、只读，sqlite 内存库）
- 数据源：`infra/initdb/02_chinook.sql`（仓库内 Chinook 1.4.5 完整数据）
- 无数据库、无模型调用、无网络依赖

