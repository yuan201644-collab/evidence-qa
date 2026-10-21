# acceptance_20261011 运行总览（C 线 · 10-11 ~ 10-13）

> 本目录对应执行单《组员测试执行_C数据库与中级任务_20261011-13》第 3、4、5 节的运行产物。
> 原始响应（`raw_answers.jsonl`）为**真实 HTTPS 采集**，逐条落盘。
> 所有结论一律标注为**开发回归**，不作盲测 / 准确率 / 泛化证据。

---

## 一、三天任务与完成情况

### 10-11（执行单第 2、3 节）

| # | 任务 | 状态 | 证据 |
|---|---|---|---|
| 1 | 环境与数据源确认 | ✅ **完成** | `ict-db`(5432, chinook) / `ict-aw`(5433, postgres) 均 healthy；`/health` 200 |
| 2 | 20 道单表问数候选题（题目/参考/来源三文件分离） | ✅ **完成并入库** | `eval/cases/acceptance_20261011/`，提交 `44cc318`（分支 `feat-acceptance-1011`） |
| 3 | 任务 1 / 单表候选题真实接口采集 | ✅ **完成** | 20 题真实回归：**PASS 17 / CLARIFY 2 / FAIL 1**，产物见 `agent_temp/acceptance_run_20261011/` |

### 10-11 ~ 10-12（执行单第 4 节：中级任务 3/4/5 已曝光回归）

| 套件 | 采集 | 评分 | 状态 |
|---|---|---|---|
| **T3**（多轮澄清，Chinook:8000） | ✅ 20 案例 / 43 轮，42×200 + 1 超时 | ✅ 逐题 rubric 核对：**9/20 全 checks 通过** | ✅ 完成（缺陷已分类，见 `C-t3-v02/RUBRIC_CHECK.md`） |
| **T4**（Schema Linking，AW:8017） | ❌ 20/20 **LLM 429**（原 run 与拆批重跑均失败） | ⛔ **无法评分**（判据/参考缺失） | ❌ 阻塞（见 `C-t4-v03/COLLECTION_NOTES.md`） |
| **T5**（多表 JOIN，Chinook:8018） | ⚠️ 仅 **4/20 产出 SQL**（其余 429） | ⛔ **无法评分**（参考缺失） | ⚠️ 部分完成（见 `C-t5-v02b1/COLLECTION_NOTES.md`） |

### 10-13（执行单第 5 节：18:00 前交接 7 项）

| # | 交接项 | 状态 |
|---|---|---|
| 1 | 单表问题/参考/来源文件（注明"官方 10 / 内部 20"） | ✅ 就绪 |
| 2 | 单表真实接口原始响应 + 逐题比对表 | ✅ 就绪 |
| 3 | T3 raw_answers + freeze + 只读 SQL 审计 + 逐题澄清检查 | ✅ 就绪 |
| 4 | T4/T5 raw_answers + freeze + **评分 JSON** + 执行命令 + 完整错误 | ⚠️ raw_answers/freeze/命令齐；**评分 JSON 缺**（判据缺失） |
| 5 | 数据库/表数/指纹、代码状态、端口、数据与运行清单 | ✅ 记录在各自 `freeze.json` |
| 6 | 失败分类 | ✅ 已分类（见下） |
| 7 | 待负责人决定项 | ✅ 已列（见下） |

---

## 二、失败分类

| 类别 | 出现处 | 说明 |
|---|---|---|
| **LLM 配额 / 限流** | T4 全部、T5 16/20、T3 3 题 | `glm-4.5-flash` 免费档持续 429；T4 因 AW 大 schema（≈1.2 MB）触发 token 级限流 |
| **语义校验器过严** | T3-01 / STEP-03 / METRIC-02 | 合法的「增长率子查询」被 `sql_semantic_validator` 拒：*当前语句结构不在语义检查范围内* |
| **LLM 生成 SQL 语法错** | T3-02 | `SELECT (2023_sales ...` 括号未闭合 |
| **请求超时** | T3-METRIC-01、T4-U01 | 120 s `TimeoutError` |
| **时间锚点缺失** | T3-period-relative-pair、T3-STEP-01 | T3 未设 `APP_TIME_ANCHOR`，「去年/前年」解析偏差 |
| **pending 替换无 trace 标记** | T3-pending-replaced | trace 未标 `pending_replaced_by_new_question` |
| **判据 / 参考文件缺失** | T4、T5 | A 侧候选分支漏提交（见下） |

---

## 三、需要负责人决定 / 处理的阻塞

1. **LLM 通道容量**（P0）：当前 `glm-4.5-flash` 免费档**无法支撑 T4/T5 的批量回归**。需换高配额通道
   （A 的 key / DeepSeek / 更高档 GLM），否则 T4/T5 采集无法完成。
2. **判据 / 参考文件缺失**（P0）：
   - 缺 `t4_v03_rubric.jsonl`、`t4_v03_reference_queries.jsonl`（T4 评分必需）
   - 缺 `t5_v02_reference_cases.jsonl`、`t5_v02_rubric.jsonl`（T5 评分必需）
   - 已核：工作区 / `mainline-candidate-1010` / `main` / 全历史均无，且未被 `.gitignore` 排除 → **A 侧漏提交**。
3. **T3 是否需要设 `APP_TIME_ANCHOR`**：`t3_v02_rubric.jsonl` 要求锚点 `2024-06-15` 下解析相对时间词，
   但执行单 T3 段未要求设置 → 需澄清 T3 是否应带锚点重跑。
4. **T3 的 `sql_semantic_validator` 边界**：增长率子查询被拒，是否需放宽（属实现缺陷，交负责人判断）。
5. **PR 状态**：`feat-acceptance-1011`（`44cc318`）**待开 PR**，base 建议 `mainline-candidate-1010`；
   PR#52（阻断修复）与 PR#51（演示脚本）仍待合，A 侧零回复。

---

## 四、产物索引

```
C-t3-v02/            T3 采集（20 案例 / 43 轮）+ freeze + RUBRIC_CHECK.md（逐题核对）
C-t4-v03/            T4 原 run（全 429）+ COLLECTION_NOTES.md
C-t4-v03b1..b4/      T4 拆批重跑
C-t4-v03-run2/       T4 四批合并
C-t5-v02b1..b4/      T5 拆批采集（含 COLLECTION_NOTES.md）
C-t5-v02-run2/       T5 四批合并
```

单表候选题真实回归（10-11）另存于 `agent_temp/acceptance_run_20261011/`。
