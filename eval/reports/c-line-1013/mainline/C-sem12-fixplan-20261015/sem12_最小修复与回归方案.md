# sem-12 最小修复与回归方案（2026-10-15，C 线）

> **A 10-15 口径**：sem-12 按「候选 SQL 连接列错误」登记为**系统问题**；
> 给出**最小修复和回归方案**，**先不要自行调整评分口径**。
> 本件据此办理：**未改参考答案、未改判据、未改评分口径、未改任何实现代码**。
> 本轮只做**只读实测 + 方案**，落地需 A 批准。

定性沿用 A 的判定并补上根因证据：**属系统语义错误（跨表 JOIN 选错列名），不属判据或口径问题。**
但根因与最初的猜测**不同** —— 不是"校验规则缺失"，而是**已有校验规则从未被执行**。

---

## 1. 结论先行

| 项 | 实测结论 |
|---|---|
| 差异是否口径问题 | **否**。参考改 `EXCEPT`（连接正确）仍得同一 27 行 id 集合 |
| 差异是否语义错误 | **是**。候选第二分支 `ON c.customer_id = i.invoice_id` 选错列 |
| 现有校验规则能否发现 | **能**。实测判 `repair / nested_scope_join_condition_unverified`，不放行 |
| 那为什么错 SQL 被执行了 | **校验从未执行** —— 开关默认关闭且装配处未传参 |
| 修复是否属于新增能力 | **不是**，属于**把已有闸门接通并让它默认生效** |
| 打开闸门的副作用 | 实测 **T4 拦截 0/12、T5 拦截 1/15（仅 sem-12）** |

## 2. 根因（实证链，非推断）

### 2.1 校验规则本身是对的

`app/nl2sql/semantic_validator.py:157-164` 已有 JOIN 外键列组核验
（`join_condition_unverified`）；`pipeline.py:431-453` 在校验不通过时
`status == "repair"` 会**带错误回灌重试**、其他情况 `break`，**都不会执行 SQL**。

只读探针（`probe_sem12_validator.py`，读 `infra/schema_meta.json` 构造 JoinPlan）实测：

| SQL | check() 结果 | 是否放行执行 |
|---|---|---|
| A 参考 SQL（`NOT IN` 子查询） | `pass` / `declared_fk_path` | 放行 |
| **B 候选 SQL（`EXCEPT` + 第二分支错列）** | **`repair` / `nested_scope_join_condition_unverified`** | **拦下** |
| C 候选 SQL 仅修第二分支连接列 | `pass` / `nested_scopes_validated` | 放行 |

`EXCEPT` 的两个分支都能被捕获（`SetOperation` 本身是 `Query`，
`_immediate_nested_queries` 实测捕获 2 个 nested query），**不存在"组合查询逃过校验"的结构漏洞**。

### 2.2 那为什么错 SQL 真的被执行了

三处事实叠加，导致闸门整块被跳过：

1. `app/nl2sql/pipeline.py:199-203`
   `self.semantic_planning_enabled = env_bool("NL2SQL_SEMANTIC_PLANNING_ENABLED", False)`
   —— **默认 False**。
2. `app/tools/nl2sql.py:164-170` 构造 `Nl2SqlPipeline(...)` 时
   **没有传 `semantic_planning_enabled`** → 走上面的默认值分支。
3. `.env` **未设置** `NL2SQL_SEMANTIC_PLANNING_ENABLED` → 取默认 `False`。

于是 `pipeline.py:428` 的 `if self.semantic_planning_enabled:` 整块跳过，
trace 里**根本不出现** `sql_semantic_validator` 段。

**trace 实测佐证**：10-13 T5 采集中 sem-12 的 trace 只有 5 段 ——
`normalizer` → `semantic_clarifier` → `nl2sql` → `nl2sql` → `nl2sql`，
**没有任何语义校验段**；其中 3 次 `nl2sql` 说明重试机制在工作，
但重试来自 `validate_sql` 层，**与 JOIN 外键校验无关**。

**清单误声明**：`t4_v03_runtime_manifest.json` 与 `t5_v02_runtime_manifest.json`
都写着 `semantic_planning_enabled: true`，与实际运行**不一致**。
这是手写声明而非实测，`driver.py` 只登记清单文件哈希、不校验内容，
所以这类不一致只能在事后查 trace 才发现。

### 2.3 影响面（不是单题问题）

`audit_semantic_validation_coverage.py` 只读解析三轮 trace：

| 运行 | 用例数 | trace 含 `sql_semantic_validator` | 含 JOIN 用例 | 其中未过语义校验 |
|---|---:|---:|---:|---:|
| T3（`b3c8041` 带锚点正式重跑） | 43 | 0 | 0 | 0（无多表题） |
| **T4** | 20 | **0** | 12 | **12** |
| **T5** | 20 | **0** | 15 | **15** |

**三轮共 27 个多表用例，其跨表连接列从未经过外键一致性校验。**
sem-12 只是其中**第一个被结果集比对暴露出来**的用例；其余多表用例"结果正确"
不等于"连接列经过校验"。

## 3. 最小修复方案

分三处，**不改任何对外契约、不改题目/判据/答案**。

### 修复 1（必须）：让闸门默认生效

改 `app/nl2sql/pipeline.py:200`：

```
- env_bool("NL2SQL_SEMANTIC_PLANNING_ENABLED", False)
+ env_bool("NL2SQL_SEMANTIC_PLANNING_ENABLED", True)
```

保留环境变量作为**应急关闭开关**（`=0` 仍可回退），因此**不破坏任何现有部署方式**。

### 修复 2（必须）：装配处显式传参，消除"默认值决定行为"

`app/tools/nl2sql.py:164` 显式传 `semantic_planning_enabled=env_bool(...)`，
让"谁决定闸门状态"在装配点一眼可见，避免再次出现"靠默认值静默决定"。

### 修复 3（必须）：把声明变成实测

`freeze.json` 增加一个**实测开关快照**字段（如 `runtime_switches_observed`），
由采集器从**进程内实际 settings** 读取并写入，而不是抄运行清单里的手写值。
这样"清单说开、实际关"这类不一致**在采集当场就能看出来**，不必事后查 trace 反推。
> 字段名与落点涉及采集脚本与冻结记录格式，**属契约面变更，需 A 确认后再动**（见 §6）。

### 不做的事

- 不改 `semantic_validator.py` 的判定逻辑（实测已正确，改它属过度设计）
- 不改参考答案、不改 `t5_v02_rubric` / `t5_v02_reference_cases`、不改 `score_t5_development.py`
- 不改 T3/T4/T5 任何既有分数与归档

## 4. 为什么不能"直接开闸门就完事"（诚实标注）

打开闸门后，除了 `repair`，还有若干路径会返回 `unknown`
（`ambiguous_join_path`、`join_plan_incomplete`、`implicit_multi_table`、
`possible_grain_risk`、`join_without_supported_on` 等），而 `pipeline.py:447-453`
对 `unknown` 是**直接 `break`（不重试、不执行）**。运行时的 `JoinPlanner` 可能给出
`ambiguous` / `warnings`，这类情况会**直接失败**而不是降级重试。

因此必须**先离线评估、再开启、再在线回归**，不能一步到位。

## 5. 回归方案

### 5.1 离线影响评估（**已完成，结果见 §6**）

把 T4/T5 既有回答里的候选 SQL 逐条喂给校验器，统计拦截面。
脚本 `assess_semantic_gate_impact.py`，明细 `impact_assessment.json`：

| 运行 | 多表用例 | `pass` | 被拦下 | 拦下的用例 |
|---|---:|---:|---:|---|
| T4 | 12 | **12** | **0** | — |
| T5 | 15 | 14 | **1** | sem-12（`nested_scope_join_condition_unverified`） |

⚠️ **局限（必须随结论引用）**：该评估用 `schema_meta` 的**全部外键边**构造 JoinPlan，
比运行时 `JoinPlanner` 针对单题生成的 plan **更宽松**，所以这是「拦截面」的**近似上界**，
不等于精确预测；真实拦截率必须靠开启后的在线回归确认。

### 5.2 单测方案（新增，不改现有断言）

建议放 `tests/test_semantic_planning_gate.py`，四组：

1. **默认生效**：`Nl2SqlPipeline(...)` 不传参时 `semantic_planning_enabled is True`；
   设 `NL2SQL_SEMANTIC_PLANNING_ENABLED=0` 时为 `False`（回退开关仍有效）。
2. **拦截即不执行**：用假 executor + 假 client 产出 sem-12 原形 SQL
   （`EXCEPT` 第二分支错列），断言 **executor 一次都没被调用**，且结果里带
   `reason_code = nested_scope_join_condition_unverified`。
3. **修正后放行**：同一条 SQL 仅把 `ON c.customer_id = i.invoice_id` 改为
   `= i.customer_id`，断言 executor 被调用一次。
4. **组合查询不漏检**：对 `UNION` / `INTERSECT` / `EXCEPT` 三种集合运算各造一条
   "第二分支错列"SQL，断言**全部**被拦（防止将来再出现结构逃逸）。

第 2、4 组是**回归护栏**，直接对应本次缺陷；第 1 组防止再次被默认值悄悄关掉。

### 5.3 在线回归（开启后）

1. 先只跑 **sem-12 单题**，确认：trace 出现 `sql_semantic_validator`、错列被拦、
   触发重试后若改对则结果为 27 行。
2. 再跑 **T5 全量 20 题**、**T4 全量 20 题**，与现有基线对比：
   - T4 基线 **13/20**（`C-t4-registration-20261015/`）；
   - T5 基线 **19/20**（`C-rev1-20261014/`）。
   **预期**：T4 不变（离线评估 0/12 拦截）；T5 的 sem-12 由失败转通过（若重试改对），
   即理论上限 20/20。**若实际下降，说明存在误伤，须先定位误伤原因再决定是否保留闸门。**
3. 耗时影响需一并记录（`repair` 会多走 `max_attempts` 轮，多表题耗时上升属预期）。

## 6. 需要 A 确认的两点

1. **修复 3 是否要做**（`freeze.json` 增加实测开关快照）——
   它改的是采集/冻结记录格式，**属契约面变更**，按 A 口径先等确认。
2. **开启闸门的时点** —— 建议顺序：先合修复 1+2（默认开、可回退）→ 跑 5.3 在线回归 →
   再决定是否做修复 3。在线回归要调模型、耗时较长，需要 A 给定窗口。

## 7. 复现方式（本件所有结论均可复现，只读、不调模型）

```bash
cd C:/Users/ASUS/WorkBuddy/askdata-agent
D=eval/reports/c-line-1013/mainline/C-sem12-fixplan-20261015
# 1) 校验规则本身能拦住 sem-12（输出 repair / pass / pass）
.venv/Scripts/python.exe $D/probe_sem12_validator.py
# 2) 三轮 trace 里语义校验的实际覆盖（输出 T4 12/12、T5 15/15 未覆盖）
.venv/Scripts/python.exe $D/audit_semantic_validation_coverage.py
# 3) 打开闸门的拦截面评估（输出 T4 0/12、T5 1/15），明细写 impact_assessment.json
.venv/Scripts/python.exe $D/assess_semantic_gate_impact.py
# 4) sem-12 结果集层面的原始复现（sqlite 离线，27/27/51/27 四行对照）
.venv/Scripts/python.exe agent_temp/c_review_20261014/reproduce_sem12.py
```

> 四个脚本**全部只读**：不连数据库、不调用模型、不修改任何实现/判据/答案。
> 第 4 项的副本亦随 `C-rev1-20261014/` 入库（`reproduce_sem12.py` + `sem12_reproduction_output.txt`），
> 在 `agent_temp/` 下的原件保持未动。

## 8. 与 sem-12 原有只读整理的关系

- 本件**不替代** `C-sem12-review-20261014/`（题目 / 候选 SQL / 参考 SQL / 双方结果集 / 差集 / 离线复现）
- 本件在其基础上**追加**：根因实证、影响面量化、最小修复、单测与回归方案
- A 10-15 已把状态从「口径待核」改为**系统问题**，该定性同时写回原 dossier 的状态行（原文件只追加状态说明，不删改既有内容）