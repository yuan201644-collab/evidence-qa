# C-t3-v02 逐题 rubric 核对（T3 = 多轮澄清与会话）

## 运行元数据

| 项 | 值 |
|---|---|
| 运行 ID | `20261011T154750Z-6b663a19` |
| 代码提交 | `44cc318`（工作区另含 `app/fact_planner.py` 本地补丁，未提交） |
| 服务 | Chinook，`http://127.0.0.1:8000`；`GRAPH_BACKEND=mock`，`NL2SQL_BACKEND=real` |
| 模型 | `glm-4.5-flash`（免费档） |
| 采集规模 | 20 案例 / 43 轮；HTTP 200 = 42，超时 = 1 |
| 判据 | `t3_v02_rubric.jsonl`（每题 2–3 条 checks，人工核对） |
| 封存 SQL 审计 | `read_only_replay_successes = 14` |

## 逐题判定

| 案例 | checks | 通过 | 关键证据 |
|---|---|---|---|
| T3-01 | ✅ 首轮澄清指标+时间 / ❌ 补充后完成 / ❌ 百分数+SQL | **1/3** | 补「销售额，2023 相比 2022」→ `sql_semantic_validator`：**子查询关联校验未通过：当前语句结构不在语义检查范围内** |
| T3-02 | ✅ 只追问时间 / ❌ 补充后完成 / ✅ 不丢指标 | **2/3** | 补「2023 相比 2022」→ LLM 生成 SQL 语法错 `Expecting ). Line 1, Col: 20`（`SELECT (2023_sales`） |
| T3-03 | ✅✅✅ | **3/3** | 补「销售额」→ `-2.465469%` |
| T3-04 | ✅✅✅ | **3/3** | 单轮完整问题 → `-2.465469%` |
| T3-05 | ✅✅ | **2/2** | `customer_count=59`（完整简单问题不追问） |
| T3-06 | ✅✅ | **2/2** | growth 澄清 → count=59 → 补 growth 后 `-2.465469%`，两会话不串线 |
| T3-single-period-01 | ✅✅✅ | **3/3** | 补「2022 年」→ `-2.465469%`（2023 本期、2022 基期） |
| T3-period-explicit-comparator | ✅✅✅ | **3/3** | 补「同比」→ `-2.465469%` |
| T3-period-explicit-pair | ✅✅✅ | **3/3** | 补「2023 年相比 2022 年」→ `-2.465469%` |
| T3-period-relative-pair | ❌ ✅ ❌ | **1/3** | 未固定 `APP_TIME_ANCHOR` → 「去年/前年」得 `-5.643624%`，**非** rubric 要求的 2023 vs 2022 |
| T3-pending-cancel | ✅✅✅ | **3/3** | 「取消」→ 不执行 SQL；后续计数=59 不继承上下文 |
| T3-pending-replaced | ✅ ❌ ✅ | **2/3** | 替换成功（59），但 **trace 无 `pending_replaced_by_new_question` 标记** |
| T3-STEP-01 | ❌ ❌ ❌ | **0/3** | T2「去年」未继续追问、直接出 SQL（`4.216787%`）；T3「前年」才追问；无锚点致相对时间解析偏差 |
| T3-STEP-02 | ✅✅✅ | **3/3** | 补 2023 仍追问 → 再补 2022 才执行（`-2.465469%`） |
| T3-STEP-03 | ✅ ❌ ❌ | **2/3** | 首查 OK；「改成 2021 年」→ **子查询关联校验未通过** |
| T3-METRIC-01 | ✅ ❌ ❌ | **1/3** | T2 超时（`TimeoutError`，120002 ms） |
| T3-METRIC-02 | ✅ ❌ ❌ | **1/3** | 补「订单数」→ **子查询关联校验未通过** |
| T3-METRIC-03 | ✅ ❌ ❌ | **1/3** | T2 **HTTP 429**（LLM 限流，重试 3 次） |
| T3-METRIC-04 | ✅ ❌ ❌ | **1/3** | T2 **HTTP 429** |
| T3-METRIC-05 | ✅ ❌ ❌ | **1/3** | T2 **HTTP 429** |

## 汇总

- **案例全 checks 通过 = 9 / 20**：T3-03、T3-04、T3-05、T3-06、T3-single-period-01、T3-period-explicit-comparator、T3-period-explicit-pair、T3-pending-cancel、T3-STEP-02。
- **澄清行为**：凡首轮缺条件者（15 个多轮案例），**首轮追问均正确**（问对缺失的指标 / 时间范围）——澄清策略本身表现良好。
- **封存 SQL 只读重放**：14 条成功执行（审计 `ok=false` 仅因子项 T3-METRIC-01 超时）。

## 缺陷分类（交负责人）

| # | 类别 | 案例 | 说明 |
|---|---|---|---|
| 1 | **语义校验器过严** | T3-01、T3-STEP-03、T3-METRIC-02 | 合法的「增长率子查询」被 `sql_semantic_validator` 拒：*当前语句结构不在语义检查范围内* |
| 2 | **LLM 生成 SQL 语法错** | T3-02 | `SELECT (2023_sales ...` 括号未闭合 |
| 3 | **LLM 限流 429** | T3-METRIC-03/04/05 | 免费档限流（重试 3 次仍 429）；**非系统缺陷**，属环境 |
| 4 | **请求超时** | T3-METRIC-01 | 120 s `TimeoutError` |
| 5 | **时间锚点缺失** | T3-period-relative-pair、T3-STEP-01 | 本次未设 `APP_TIME_ANCHOR=2024-06-15`；rubric 明确要求锚点下「去年=2023、前年=2022」 |
| 6 | **pending 替换无 trace 标记** | T3-pending-replaced | trace 未标 `pending_replaced_by_new_question` |

## 与历史对比（谨慎）

- 10-07 历史 run 记为 **20/20 案例判据通过**（见 `eval/mainline/development_v0.1/README.md`）。
- 本轮 **9/20**。**不可直接对比**：本轮 LLM 换为 `glm-4.5-flash`（免费档有限流），且 `linker` / `join_planner` / `semantic_validator` 均为候选分支上的新实现——模型与实现双变量同时变化。
- 本结论一律标注为**开发回归**，不作盲测 / 准确率 / 泛化证据。
