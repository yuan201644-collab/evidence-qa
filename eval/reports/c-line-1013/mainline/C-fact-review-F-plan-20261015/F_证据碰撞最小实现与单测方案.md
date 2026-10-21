# F 项：证据来源碰撞不得报 complete —— 最小实现与单测方案（2026-10-15，C 线）

> **A 10-15 确认**：「结论有完整、可追溯的证据支持」是项目核心能力，**不是可选项**。
> 因此：**证据去重发生来源碰撞时，系统不得仍标记为 `complete`**；
> 要求给出**最小实现与单测方案**，覆盖**碰撞检测**与**状态降级**；
> **若涉及公共契约变更，先列出影响并等确认。**

本件据此办理：**只读实测 + 方案**。**未改实现、未改契约、未改判据、未改测试**。

---

## 1. 实测结论（先摆事实，修正了一个错误假设）

我最初的假设是「账本 `candidates` 是 `dict[evidence_key→RefT]`，不同来源塌缩成一条，
投影层无法自证」。**实测否掉了这个假设**，必须先纠正：

### 1.1 「不同来源塌缩成同一定位」在当前定位规则下**不成立**（实测）

| ref 形态 | `_locator`（fact_review.py:274） | `evidence_key`（evidence_ledger.py:85） |
|---|---|---|
| `SqlRef` | `('sql','customer','customer_id','1')` | `('sql','customer','customer_id','1')` |
| `DocRef` 有 char 偏移 | `('doc','d1','3','100','200')` | `('doc','d1','100','200')` |
| `DocRef` 无 char | `None`（不参与映射） | `('doc','d1','3','第三章')` |
| `DocRef` 不同 `doc_id` 同偏移 | `('doc','d2','3','100','200')` | `('doc','d2','100','200')` |

两套键的**元组形状不同，但同由 `(doc_id, char 区间)` 或 `(sql 表/列/主键)` 派生**，
且 **`_locator` 的信息严格多于 `evidence_key`**（多带 `page`）。
因此 **同 locator ⟹ 同 evidence_key**：不存在"两个不同来源被投影层静默折叠成一条"的路径。
`_map_evidence` 里的 `seen_locators` 去重是**设计内的同一定位去重**，不构成谎报。

### 1.2 真实漏网点：**投影层看不到账本记了几条来源**（实测）

`EvidenceLedger.record_fact`（`app/evidence_ledger.py:135-142`）只校验
「`evidence_ids` 与 `evidence_refs` 里的键都存在于 `candidates`」，
**不校验两者一一对应**。实测：

```
构造 evidence_ids=(k_a, k_b) / evidence_refs=(ref_a,) → record_fact 接受
投影 _map_evidence((ref_a,), [ref_a, ref_b])           → status='complete'
```

即：**账本明明记了 2 条来源，证据对象只挂了 1 条，投影层无从知晓，仍报 `complete`。**
这就是 A 指出的「证据去重发生来源碰撞时仍标记 complete」在本项目的**真实落点**。

### 1.3 三条路径的当前行为（实测对照）

| 情形 | 当前 `evidence_link_status` | 评价 |
|---|---|---|
| 账本记 N 条来源、refs 只挂 M<N 条 | **`complete`** | ❌ **漏网**（A 要修的就是这条） |
| 同一 ref 重复登记（设计内去重） | `complete` | ✅ 合理 |
| 挂 M 条、最终引用只含其中一部分 | `partial` | ✅ 已保守 |

### 1.4 修复不会误伤生产路径（实测前提）

`app/fact_executor.py:339-342` 生产路径**严格 1:1**：

```python
evidence_ids=tuple(evidence_ids),
evidence_refs=tuple(ledger.candidates[key] for key in evidence_ids),
```

refs 由 ids 逐个映射而来 → **新增的「来源数 vs 成功映射数」一致性检查在生产路径上恒为真**，
只会在账本被破坏或将来引入不一致时降级，**属保守方向，零误伤**。

## 2. 最小实现（2 处，零公共契约变更）

### 修复 A（核心）：把账本来源数纳入 `complete` 判定

改 `app/fact_review.py`：

1. 调用处 `build_fact_review` 第 73-75 行，把 `record.evidence_ids` 一并传入：
   ```python
   evidence_indices, evidence_status = _map_evidence(
       record.evidence_refs if record is not None else (),
       final_refs,
       record.evidence_ids if record is not None else (),   # 新增：账本来源集合
   )
   ```
2. `_map_evidence` 增加一个可选参数 `declared_keys: tuple[tuple[str, ...], ...] = ()`，
   在现有去重与匹配逻辑之后、返回之前补一段**碰撞检测**：
   ```python
   if declared_keys:
       mapped_keys = {evidence_key(ref) for ref in mapped_refs}   # 成功映射的来源键
       dangling = set(declared_keys) - mapped_keys
       if dangling:
           unresolved += len(dangling)      # 账本记了、但没落到最终引用上 → 不算完整
   ```
   其余判定逻辑与返回分支**完全不动**，碰撞只把结果从 `complete` 降到 `partial`。

**为什么这样就是"最小"**：
- 只改**内部函数签名 + 一处调用**，不动任何 `contracts.py` 定义；
- `evidence_link_status` 的 `Literal["complete","partial","unavailable"]`（`app/contracts.py:441`）
  **不变** → 前端、评测脚本、既有测试**零改动**；
- 语义方向保守：宁降级不谎报，与「complete 必须可自证」一致。

### 修复 B（双保险，fail fast）：账本层加不变量

在 `EvidenceLedger.record_fact` 增加断言，拒绝 ids 与 refs 不对应的记录：

```python
if [evidence_key(ref) for ref in fact.evidence_refs] != list(fact.evidence_ids):
    raise ValueError("事实的来源键与证据对象必须一一对应")
```

这让问题在**账本写入当场**暴露，而不是等到投影层。
需注意：现有 `fact_executor` 产出的记录满足该等式（1:1），但**若同一 key 重复登记**，
`ids` 与 `refs` 仍逐项相等（重复项两边都重复），所以断言不会误伤。
**落地前须先跑一遍全量跨源回归，确认没有历史路径违反此不变量。**

> 修复 A 与 B 建议**同时上**：A 保证对外不谎报，B 保证内部不自欺。

## 3. 单测方案（新增文件，不改既有断言）

建议新增 `tests/test_fact_review_evidence_collision.py`，四组，全部用假件、离线：

### 第 1 组：碰撞检测（本次缺陷的直接回归护栏）

| # | 构造 | 期望 |
|---|---|---|
| 1 | `evidence_ids=(k_a,k_b)`、`evidence_refs=(ref_a,)`，`final_refs=[ref_a,ref_b]` | `evidence_link_status == "partial"`，`reason`/`reason_code` 非空 |
| 2 | 同上但 `final_refs=[ref_a]`（连未挂的来源都没有） | `partial` |
| 3 | `evidence_ids=(k_a,k_b)`、`evidence_refs=(ref_a,ref_b)`、`final_refs=[ref_a,ref_b]` | **`complete`**（回归护栏：正常 1:1 不被误伤） |
| 4 | `evidence_refs=(ref_a,)`、`evidence_ids=()` | 维持现有行为（`complete`），确认未引入新误报 |

### 第 2 组：状态降级的可观测性

| # | 构造 | 期望 |
|---|---|---|
| 5 | 第 1 组用例走完整 `build_fact_review` | `FactReviewItem.reason_code` 非空、`reason` 有中文说明（**不靠前端猜测**） |
| 6 | 同上 | `evidence_ref_indices` 只含真正映射成功的下标，**不把未映射来源算进去** |

### 第 3 组：账本不变量（修复 B）

| # | 构造 | 期望 |
|---|---|---|
| 7 | ids/refs 不对应 → `record_fact` | 抛 `ValueError`，且异常信息含「一一对应」 |
| 8 | 同一 key 重复登记（ids 与 refs 各重复两次） | **不抛错**（设计内去重不得被误伤） |
| 9 | refs 引用未登记候选 | 抛错（既有行为不回归） |

### 第 4 组：契约面守门

| # | 断言 | 期望 |
|---|---|---|
| 10 | `FactReviewItem(evidence_link_status="collided")` | **抛 `ValidationError`** —— 证明本方案**没有**偷加契约取值 |
| 11 | `evidence_link_status` 的合法取值集合 | 仍为 `{complete, partial, unavailable}` |

> 第 10 组是刻意的**反向断言**：A 要求"涉及契约变更先等确认"，
> 这条测试把"没有变更契约"变成可执行事实，而不是口头承诺。

## 4. 契约影响清单（**需 A 确认才可执行的部分**）

### 4.1 本方案主体（修复 A + B + 上述单测）：**契约零变更**

| 面 | 是否变更 |
|---|---|
| `app/contracts.py`（`FactReviewItem` / `FactReview`） | **不变** |
| `evidence_link_status` 取值集合 | **不变**（仍三态） |
| 前端 `web/` | **不变** |
| 评测脚本 / 判据 / 参考答案 | **不变** |
| 既有测试 | **不变**（新增文件，不改旧断言） |

### 4.2 若 A 认为「partial 不足以表达碰撞」：新增 `collided` 态属**契约变更**，影响如下

| 面 | 影响 |
|---|---|
| `app/contracts.py:441` | `Literal["complete","partial","unavailable"]` 需扩容 → **公共契约变更** |
| `web/` 前端 | 消费 `evidence_link_status` 的组件需处理新取值，否则可能落到兜底文案 |
| 既有测试 | 若有穷举断言该三态的用例会失败，需一并更新 |
| 评测/文档 | 口径文档中"证据关联状态"的取值表需同步 |
| 客户端 | 任何按三态分支的外部消费方（当前仓库内未见，需 A 确认是否有外部） |

**建议**：先落 4.1（零风险、不阻塞），把 4.2 作为增强项另议；
若 A 要求更强的可观测性，可先只加 `reason_code`（字符串字段，非 Literal 枚举），
在不碰契约的前提下让"碰撞"在响应里**有名字可查**。

## 5. 回归方案

1. **离线单测**：第 3 节四组全绿（不调模型、不连库）。
2. **跨源全量回归**：修复后跑 `cross_source_demo` 全路径，核对
   **① 所有原本 `complete` 的项是否仍 `complete`**（除命中碰撞者）、
   **② 有多少项因新增检查降级为 `partial`**。降级数量若超出预期，说明账本存在历史不一致，
   须先定位再决定是否保留修复 B。
3. **投影一致性核对**：同一 `run` 的账本 `evidence_ids` 与投影 `evidence_ref_indices`
   逐项对账，确认无"账本记了但投影没体现"的残留。
4. **只读演示核对**：走一次跨源问答，确认前端展示的证据条数与账本一致。

## 6. 需要 A 确认的两点

1. 是否同意**先落 4.1（零契约变更）**，把 4.2（新增 `collided` 态 / 或仅加 `reason_code`）单独议？
2. 修复 B 的账本不变量断言**是否要 fail fast**：它会让历史上的不一致记录直接抛错。
   建议做法是**先只读扫描一遍现有跨源产物**确认无违规，再开启断言——若 A 同意，
   C 可先做扫描并回报结果。

## 7. 复现方式（只读、离线、不调模型）

```bash
cd C:/Users/ASUS/WorkBuddy/askdata-agent
.venv/Scripts/python.exe eval/reports/c-line-1013/mainline/C-fact-review-F-plan-20261015/probe_f_evidence_collision.py
```

预期输出：Q1 显示两套定位键的关系（含"同 locator ⟹ 同 evidence_key"的依据）；
Q2 显示 `record_fact` 接受 ids/refs 不对应；Q3 显示 `complete` / `complete` / `partial` 三行对照。