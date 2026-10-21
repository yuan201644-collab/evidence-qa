# S2「SQL 条件核对」完整实施计划

> 范围来源：`docs/architecture/限定条件保真与主动澄清修复实施方案_v0.1_20261015.md`（753 行，下称「母方案」）
> 本文件把母方案 §5.1／§5.4—§5.8／§6.1／§7／§8／§9／§10 中属于 **S2** 的部分，落成可逐项勾选的实施计划。
> **不包含 S3（多轮与文档范围）、S4（展示与兼容）、S5（综合验收）** 的具体步骤，只标注它们的接口影响点。
> 不写时间成本 —— 只列依赖顺序与完成判据。

> ### ⚠ 本文对母方案的两处实质偏离（经实测证据支持，需同步修订母方案）
>
> | # | 母方案 | 本文 | 依据 |
> |---|---|---|---|
> | 1 | §5.2／§6.1 要求新增静态能力目录 `infra/query_capabilities.v0.1.json` | **不建静态目录**，改为运行时从 `SchemaCatalog` + 列中文说明判定（`app/nl2sql/capability_runtime.py`） | 见 §3.2 的四条实测证据。静态表对 `customer.country` 与 `invoice.billing_country` 只能都标 `category`，而列说明里 `billing_country` 自陈「（而非客户所在国家）」—— **静态表恰好会丢掉这条判据** |
> | 2 | §3.3／§6.1 要求新增 `undetermined` 第三态 | **沿用既有 `pass/repair/unknown` 三态**，不新造 | `app/nl2sql/planning_models.py:14` 已有 `SemanticCheckStatus = Literal["pass","repair","unknown"]`，`app/fact_review.py` 侧 `FactReviewCheck.result` 也已有 `unknown`，新造词只会分裂两套判据 |
>
> 另：母方案 §5.4 `:248`「不受 `NL2SQL_SEMANTIC_PLANNING_ENABLED` 控制」本文同意，但**须分两档落地** —— `semantic_planning_enabled=False` 时 `join_plan is None`（`app/nl2sql/pipeline.py:268`），粒度类检查无输入，只能降级 `unknown`。见 §5.4。

> ### ✅ 负责人已裁定的两条（2026-10-15）
>
> | # | 问题 | 裁定 |
> |---|---|---|
> | **P1** | 第三态 `unknown` 怎么处置？ | 采用 **(c) 放行 + 核验卡标黄**；与 `enforce` 档构成同一开关的两级，**不是二选一**。详见决策 A 之后的 P1 小节 |
> | **P2** | 要不要 `off/warn/enforce` 三级开关？ | **要，默认 `warn`** —— 符合项目架构设计初衷；避免一次全量生效把 T4/T5 打红。详见决策 B |

---

## 0. 目标与范围

### 0.1 S2 要解决的行为

**用户说了限定条件，系统做不到却不说，照样返回一个数字。**

| 现在 | S2 之后 |
|---|---|
| `2023年有多少客户？` → 静默丢掉年份，返回全量 59 | 拦下，说明客户表没有注册时间依据，询问可支持口径 |
| `发票平均金额` → 可能执行 `SUM` 候选 | `AVG` 对应的 SQL 才放行 |
| `金额最高的3张发票` → 缺排序或缺 LIMIT | 排序方向、指标、N 全部核对 |
| `不在巴西的客户` → 否定被改成正向 | 否定保留，方向一致才放行 |
| `日期条件 OR TRUE` → 通过现有校验 | 条件作用域核对，挡在执行器之前 |

### 0.2 与 S1 的分工（不要混）

| | S1（已完成，commit `661dc59`） | S2（本计划） |
|---|---|---|
| 管什么 | 用户**没说的** → 追问 | 用户**说了系统做不到** → 拦截或追问 |
| 时机 | 生成 SQL **之前** | 生成 SQL **之后、执行之前** |
| 位置 | `app/clarifier.py` / `app/orchestrator.py` | `app/nl2sql/requirement_validator.py` + `app/nl2sql/pipeline.py` |
| 典型场景 | SC-02/03/26/27 | SC-05/06/07/08/09/11/13/14/15/16/17/18/19 |

> ⚠ **S1 尚未完全收口**：用户复核还留了两笔旧账（隔离样本加载器仍是黑名单、历史清单哈希校验被我上轮撤掉），已列为切片 **C0**，**必须最先还清**再做 S2。详见 §14。

### 0.3 S2 交付物清单（母方案 §6.1 `:372-388`）

| 文件 | 状态 | 归属 |
|---|---|---|
| `app/query_requirements.py` | **已存在**（583 行） | S1 建的，S2 要扩 |
| `app/nl2sql/planning_models.py` | **已存在**（203 行） | S1 加过类型，S2 再加 |
| `infra/query_capabilities.v0.1.json` | **不建**（见 §3.2） | 原计划新建，**已取消** |
| `app/nl2sql/requirement_validator.py` | **不存在** | S2 新建（核心） |
| `app/nl2sql/pipeline.py` | 已存在 | S2 接线 |
| `app/fact_review.py` | 已存在 | S2 补检查项 |
| `app/nl2sql/followup.py` / `app/memory.py` | 已存在 | **S3 主责**，S2 只留接口 |
| 6 个新测试文件 | **不存在** | S2 新建（其中 `tests/test_query_capabilities.py` 随能力判定方案一并保留，只换被测对象） |
| `eval/semantic_coverage/*` 采集与评分 | **不存在** | S2 新建 |

### 0.4 硬边界（母方案原文，不可放宽）

1. **`:248`** 新增闸门**不受** `NL2SQL_SEMANTIC_PLANNING_ENABLED` 控制，也**不受**是否展示 `fact_review` 控制。
   > 「关闭表定位增强不等于允许丢条件。」
2. **`:270`** 不能用「全部拒绝复杂 SQL」实现安全 —— 先覆盖既有 T4/T5 正确样例使用的结构。
3. **`:271`** 支持确定可证的等价形式（别名变化、AND 交换、同义区间）；**无法证明等价时保留明确原因，不声称一定错误或已经通过** —— 对应本计划三态里的 `unknown`。
4. **`:277`** 复用 `Nl2SqlPipeline` 既有最大尝试次数，**不加无限重试循环**。
5. **`:279`** 修复用尽后 `error` 有值、**不得执行最后一条错误候选**；`sql` 继续表示「真正执行过的 SQL」。
6. **`:661`** 每支持一种新结构，**必须同时加入正确写法和绕过写法**两套测试。
7. **`:720`** 历史题包、原始回答、Gold、外部锚点**不得被本修复改写**。

---

## 1. 前置决策

### 决策 A：第三态已经存在，**不新造 `undetermined`**（已定案）

- **矛盾原文**：母方案 `:271` 说「无法证明等价时……**不声称一定错误或已经通过**」；`:363` 的 S2 完成门槛却写「**正确等价 SQL 通过**」。
- **结论（已核实，不再是待决项）**：代码里**已经有第三态** —— `app/nl2sql/planning_models.py:14`：

```python
SemanticCheckStatus = Literal["pass", "repair", "unknown"]
```

  现有 `semantic_validator.py` 全程用这三态。`RequirementOutcome` 应**与之对齐**，不引入新词：

| 既有状态 | S2 含义 | 处置 |
|---|---|---|
| `pass` | 已证明要求被满足 | 放行执行 |
| `repair` | 已证明要求**未**满足 | 回灌重写；用尽则**不执行** |
| `unknown` | **无法证明**（结构不支持/信息不足/说明缺失） | 见决策 B 的模式分流 |

  核验卡侧的映射同样现成：`app/contracts.py:439` `FactReviewCheck.result` 取值域为 `Literal["passed","failed","unknown","not_run"]`，**不需要扩契约**。

- **`RequirementCoverage.status` 汇总规则**：任一 `repair` → `repair`；否则任一 `unknown` → `unknown`；否则 `pass`。
- **对母方案 `:363` 的建议修订**：把「正确等价 SQL **通过**」改为「正确等价 SQL **不返回 repair**」（允许 `unknown`）—— 等价性判定不可能完备，用 `pass` 作门槛会逼出"放宽到全放行"。

> ### ✅ P1 已裁定：第三态 `unknown` → 采用 (c) 放行 + 核验卡标黄
>
> 三个候选的对比（**负责人 2026-10-15 选定 (c)**；代码结构上三个都只是改一个分支，随时可切）：
>
> | # | 处置 | 代价 |
> |---|---|---|
> | (a) | 放行 | 保守，**可能漏** —— 条件其实没落实，用户拿到一个看起来正常的数字 |
> | (b) | 拦截 | 激进，**可能误杀 T4/T5** —— 现有正确 SQL 用了我们还没支持的结构就被拒 |
> | **(c)** | **放行 + 核验卡标黄** | ✅ **已选定**：不阻断现有能力，也不假装已确认 |
>
> **(c) 的落地形态**（已按此写进 §5.4／§7.2）：
> - SQL **照常执行**（与 (a) 相同）；
> - 但三处必须留痕，不能只执行了事：
>   1. trace 步骤 `sql_requirement_validate` 的 `status="unknown"` + `unknown: [...]` 明确列出**哪几条要求没验证**；
>   2. 核验卡 `sql_requirement_coverage` 映射为 **`unknown`（黄）**，**绝不映射 `passed`**；
>   3. 回答正文附**限定条件说明**（如"其中的时间范围未能确认是否生效"）。
> - **与决策 B 的关系**：(c) 就是 `warn` 模式下 `unknown` 的行为；切到 `enforce` 时 `unknown` 转为拦截（即 (b)）。
>   也就是说 **(c) 与 (b) 不是二选一，而是同一套分级开关的两个档位** —— 这正是"要开关"的直接收益。
> - **(a) 是唯一需要额外代码的选项**：要显式把 `unknown` 吞掉、核验卡不许标黄，等于主动放弃已获取的信息，**本计划不推荐**。
>
> **晋级条件**：默认档为 (c)。**切到 (b) 语义（`enforce`）的前置** = §9.5 的 T4/T5 负控制全绿，且 T4/T5 场景 `unknown` 计数为 0（计数口径见 §12.1）。

### 决策 B：`off/warn/enforce` 三级开关（**已定案：要开关，默认 `warn`**）

- **母方案未提开关**，是评审补充项（`docs/opinions/to-all/20261015-限定条件方案v0.1评审意见.md` 的 P3）。
- **负责人裁定（2026-10-15）**：**要开关，默认 `warn`**。理由：符合项目架构设计初衷；`:363` 是**全量生效**语义，一次误判直接把 T4（AdventureWorks 71 表，现 50/50）、T5（现 20/20）打红。原话要点：「否则一次全量生效，误判直接把 T4/T5 打红。」
- **`unknown` 的处置按模式分开**（同一句"无法证明"，在演示与评测里该做完全不同的事）：

| 模式 | `repair` | `unknown` | 理由 |
|---|---|---|---|
| `enforce`（演示/验收） | 不执行 | **不执行**，如实说明"无法确认条件已生效" | 宁可不答，不答错 |
| `warn`（**默认**） | 不执行 | 执行，trace 标 `requirement_gate_status=unknown`，核验卡显示"部分条件未能确认" | 不阻断现有能力 |
| `off`（仅对照实验） | 不检查 | 不检查 | 紧急回退，**不得作为发布配置** |

- **默认取 `warn` 的硬理由**：T4/T5 是已验收能力，`unknown` 一旦默认拦截等于把既有战绩作废。等 L1/L2 层证明 `unknown` 在 T4/T5 场景下为 **0**，再由负责人决定是否转 `enforce`。
- **开关名**：`NL2SQL_REQUIREMENT_GATE`，取值 `off|warn|enforce`，默认 `warn`。
- **默认值写在哪里**：读取点必须与既有 `env_bool` 风格一致（参考 `app/nl2sql/pipeline.py:199-203` 读 `NL2SQL_SEMANTIC_PLANNING_ENABLED` 的写法）；非法取值（如 `foo`）→ **抛错而不是静默回退**（与 `fingerprint_database` 的 scope 处理同一纪律，见 `eval/mainline/fingerprint_postgres.py`）。
- **同步 `.env` / `.env.example`**：两处都要登记该键并注明默认 `warn` 与切 `enforce` 的前置条件；`.env` 被 `.gitignore` 忽略，队友可见的只有 `.env.example`。

### 决策 C：`semantic_planning_enabled=False` 时的降级（新增，母方案 `:248` 的必要补充）

母方案 `:248` 要求本闸门**不受** `NL2SQL_SEMANTIC_PLANNING_ENABLED` 控制。本文同意（关掉表定位不等于允许丢条件），但**须分两档落地**，否则会出现"看起来在检查、实际没有输入"的假象：

| 检查档 | 依赖 `join_plan` | `semantic_planning_enabled=False` 时 |
|---|---|---|
| 时间 / 数值 / 否定 / 排名 / 分组 / 取值筛选 / 条件作用域 | 否 | **照常检查**（只比对 SQL AST 与要求清单） |
| 关联粒度 / 基数口径 | **是** | `join_plan is None`（`app/nl2sql/pipeline.py:268`）→ 降级 `unknown` |

- 降级原因必须写进 trace（`reason_code="join_plan_unavailable"`），不能静默跳过。
- **禁止**为了"照常检查"而在闸门内部自己重跑一遍 planner —— 那会绕过 S1 的定位结果，且引入第二次模型调用。

---

## 2. 实施顺序（按依赖，非按人日）

```
S2-A 运行时能力判定 ──┐
                      ├─→ S2-C 覆盖核对器 ─→ S2-D 管道接线 ─→ S2-E 记录与展示
S2-B 清单扩展 ────────┘                            │
                                                   └─→ S2-H 采集与评分
S2-G 测试  ←── 与 A~F 同步进行（每完成一项即补正/反例）
```

**关键路径**：A → C → D。A 和 B 可并行。
**A 与 B 对既有代码零风险**（纯新增模块），`C` 起才接入主链路。

---

## 3. S2-A：能力判定（**改为运行时，不建静态目录**）

### 3.1 目标

回答「这张表/这个字段能不能支持这类条件」。没有它，`2023年有多少客户？` 无法判定"做不到"。

### 3.2 为什么不建 `infra/query_capabilities.v0.1.json`

母方案 §5.2／§6.1 要求新增静态能力目录。**本文不采纳，改为运行时从既有元数据判定。** 四条实测证据：

**证据 1：静态表对"国家"两个字段只能给出相同标注，恰好丢掉唯一判据。**

`infra/schema_meta.descriptions.zh.json` 里两条列说明逐字是：

```
customer.country          → 客户所在国家。销售额按国家汇总时用本字段。
invoice.billing_country   → 账单国家。按国家统计销售额时用本字段（而非客户所在国家）。
```

母方案 §5.4 第 3 类明确要求拦截「**账单国家替代客户国家**」。但静态目录若按 `kind: category` 标注这两列，二者**完全等价**，判不出差异；而 `billing_country` 的说明自陈「**而非客户所在国家**」，这句话本身就足以静态判定用错了列。**静态表丢掉的正是我们要用的判据。**

**证据 2：行数与表数说明该文件是生成物。**

`infra/schema_meta.json` 有 1,243,931 字节（AdventureWorks 那套），列说明文件 64 条（Chinook）。手写维护三套 schema 的第三份声明，成本高于收益。

**证据 3：S1 已经选过同一条路，且已写进代码注释。**

`app/query_requirements.py:27-33` 的模块 docstring 逐字记录了该决定：

> 「**能力判定为什么不在本模块做**：方案 §5.2 设想用 `infra/query_capabilities.v0.1.json` 静态声明……本项目实跑过三个不同 schema（Chinook / AdventureWorks / 跨源演示库），静态表要手工维护三套且必然与真实元数据漂移。改为**运行时从 `SchemaCatalog` 的列说明判定**……零配置、自动适配任意 schema」

> ⚠ **但该 docstring 引用的 `app/clarifier.py::SemanticClarifier._capability_question` 并不存在**（`grep -n "_capability" app/clarifier.py` 无匹配）。这是 S1 留下的**未兑现注释**，必须一并处理，见 §3.7 待办。

**证据 4：运行时判定所需的数据已经全部在位。**

| 需要的信息 | 现成来源 | 字段 |
|---|---|---|
| 表/列是否存在 | `app/nl2sql/schema.py:82 SchemaCatalog` | `tables` / `columns` |
| 主键（去重粒度） | 同上 | `ColumnMeta.is_primary_key` |
| 外键与关联基数 | 同上 | `TableMeta.foreign_keys`（`ForeignKeyMeta`） |
| 可空性（空值语义） | 同上 | `ColumnMeta.nullable` |
| 取值样例（规范化） | 同上 | `ColumnMeta.sample_values` |
| **业务语义（时间含义/口径）** | `infra/schema_meta.descriptions.zh.json` | 按 `表名.字段名` 的中文说明 |
| **表级业务含义** | 同上 | `tables` 段（如 `invoice`：「一次结账生成一张发票，`total` 为该次结账的金额合计，也是按发票头统计销售额/营收时采用的口径」） |

`ColumnMeta` 现有字段：`name / type / nullable / is_primary_key / description / sample_values`。
`TableMeta` 现有字段：`name / schema / description / row_count / primary_key / columns / foreign_keys`。

### 3.3 运行时判定的实现

**产物**：`app/nl2sql/capability_runtime.py`（**替代** 原计划的 `capability_catalog.py`）。

```python
class RuntimeCapability:
    """从 SchemaCatalog + 列中文说明按需回答"能不能支持"。

    不做 IO：`SchemaCatalog` 与说明字典都由调用方注入。
    """

    def __init__(self, catalog: SchemaCatalog, descriptions: dict[str, str]): ...

    def time_columns(self, table: str) -> tuple[str, ...]:
        """该表的时间列。判据 = descriptions 的说明含时间语义词。
        无 → 空元组（表示"没有可用时间依据"）。"""

    def business_meaning(self, table: str, column: str) -> str:
        """列的业务含义原文（用于给用户解释为什么不能这么算）。"""

    def primary_key(self, table: str) -> tuple[str, ...]: ...

    def entity_grain(self, table: str) -> str:
        """行粒度。单列主键 → one_row_per_<pk>；复合主键 → 关联表，无独立实体。"""

    def supports_history(self, table: str) -> bool:
        """有无离职/生效期/快照类列。
        当前判据 = descriptions 说明含「离职/有效期/生效/失效/快照/历史」。
        注意：**chinook 全部 11 表的答案都是 False**，这是正确结论而非缺失。"""

    def category_columns(self, table: str) -> tuple[str, ...]: ...

    def normalized_values(self, table: str, column: str) -> tuple[str, ...]:
        """规范化取值，取 ColumnMeta.sample_values。拿不到 → 空元组（调用方判 unknown）。"""
```

**关键纪律（对齐母方案 `:204` 末句「声明缺失或不匹配时不能按声明通过」）**：

- [ ] **说明文件缺失或某列无说明 → 判 `unknown`，不判 `pass`**；
- [ ] 说明存在但不足以判定（如没说清是不是时间列）→ 同样 `unknown`；
- [ ] **运行时必须核对声明的列真的存在**：`catalog.tables[table].columns` 里查得到才算；
- [ ] **绝不从列名猜语义**（`*_date` 后缀猜测被明令禁止）—— 只用 `descriptions` 的说明文本。

### 3.4 母方案 `:206-212` 的口径表逐条落地

| 典型要求 | 运行时判定方式 | 结论 |
|---|---|---|
| 当前客户总数 | `catalog` 有 `customer` 实体 + 主键 | `pass` |
| 2023 年注册客户数 | `time_columns("customer")` → **空元组**（`customer` 的 7 列说明里无任何时间语义） | **`repair`**，话术告知"客户表没有注册时间依据" |
| 2023 年购买过的客户数 | `time_columns("invoice")` → `("invoice_date",)`（说明：「开票时间（带时分秒）。按月份/季度汇总时需先截断到月」）+ `customer_id` 外键 + `customer` 主键去重 | `pass` |
| 去年新入职员工数 | `time_columns("employee")` → `("hire_date",)`（说明：「入职日期。」） | `pass` |
| 去年**在职**员工数 | `hire_date` 存在，但 `supports_history("employee")` → **False**（无离职列） | **`repair`**：只有入职日期不能证明历史在职状态 |
| 客户国家 vs 账单国家 | 两条列说明的**文本差异** | 可判：问客户数必须用 `customer.country` |

**验证输出（实测）**：

```
customer.customer_id       -> 客户主键。
customer.country           -> 客户所在国家。销售额按国家汇总时用本字段。
invoice.invoice_date       -> 开票时间（带时分秒）。按月份/季度汇总时需先截断到月。
invoice.total              -> 发票金额合计，也是按发票头统计销售额/营收时采用的口径。
invoice.billing_country    -> 账单国家。按国家统计销售额时用本字段（而非客户所在国家）。
employee.hire_date         -> 入职日期。
employee.birth_date        -> 出生日期。
总列说明数: 64
```

### 3.5 覆盖范围

**零配置** —— 这是本方案相对静态目录的主要收益：

- `chinook.public` 11 张表：自动覆盖（`infra/schema_meta.json` + `schema_meta.descriptions.zh.json`）。
- `adventureworks` 71 张表：自动覆盖（`infra/schema_meta.adventureworks.json` 1.2MB + `schema_meta.adventureworks.descriptions.zh.json`）。
- `cross_source_demo`：需确认是否有对应 descriptions 文件；**若无，该源全部能力判定返回 `unknown`**（不阻断，只降级），并在验证记录里如实登记这一缺口。

### 3.6 完成判据

- [ ] `tests/test_query_capabilities.py` 通过；
- [ ] 五个判定全部正确：`customer` 无时间列、`invoice_date` 是开票时间、`hire_date` ≠ 在职、`billing_country` ≠ 客户国家、`supports_history` 全 False；
- [ ] **说明缺失时不返回 `pass`**（用一个空 descriptions 字典跑，断言全部 `unknown`）；
- [ ] 不从列名猜语义（构造一个说明为空的 `*_date` 列，断言不被当成时间列）；
- [ ] **覆盖范围：`cross_source_demo` 无 descriptions 文件时全部返回 `unknown`（不阻断、不报错）**，并在验证记录里登记这一缺口。

### 3.7 待办：修掉 S1 的未兑现注释

- [ ] 修正 `app/query_requirements.py:27-33` docstring 里对 `app/clarifier.py::SemanticClarifier._capability_question` 的引用 —— **该函数不存在**（`grep -n "_capability" app/clarifier.py` 无匹配）。改为引用 S2 新建的 `app/nl2sql/capability_runtime.py`，或删去该句。验证：`grep -n "_capability_question" app/` 应无残留。
- [ ] 同步修订母方案 §5.2／§6.1（删除静态目录要求）与 §3.3（三态口径），见文首「偏离说明」。

---

## 4. S2-B：要求清单扩展

### 4.1 现状

`app/query_requirements.py`（583 行）已有 11 类 `RequirementKind`（`app/nl2sql/planning_models.py:19-31`）：

```
object / measure / time / value_filter / numeric_range / negation
/ grouping / ranking / distinct_grain / document_scope / cross_source_object
```

### 4.2 S2 要补的

| 补充项 | 母方案依据 | 说明 |
|---|---|---|
| **条件作用域**（`value_scope`） | `:261` | 条件是否真正约束目标数据行；对抗 `OR TRUE`、无关子查询、注释 |
| **关联粒度**（`join_grain`） | `:262` | 一对多后 `COUNT(*)` 当客户数、父表金额重复累计 |
| **`ResolvedRequirement` 类型** | `:179` | 区分「用户说了什么」与「系统已确定什么」 |
| **`RequirementCheck` 类型** | `:181` | 记录检查证据，不能只记「启用了检查」 |
| **`QueryRequirement.requirement_id`** | `:178` | 每条要求有稳定编号，供 trace 与重写回灌引用 |
| **`required` 字段** | `:178` | 区分「必须落实」与「背景信息」 |

### 4.3 类型定义（加到 `app/nl2sql/planning_models.py`）

```python
RequirementKind = Literal[
    # —— S1 已有 ——
    "object", "measure", "time", "value_filter", "numeric_range",
    "negation", "grouping", "ranking", "distinct_grain",
    "document_scope", "cross_source_object",
    # —— S2 新增 ——
    "value_scope",   # 条件作用域
    "join_grain",    # 关联粒度
]

# ⚠ 对齐既有 SemanticCheckStatus（app/nl2sql/planning_models.py:14），不新造词：
#    pass   = 已证明要求被满足
#    repair = 已证明要求未被满足（回灌重写）
#    unknown = 无法证明（结构不支持 / 说明缺失 / join_plan 不可用）
RequirementOutcome = Literal["pass", "repair", "unknown"]

@dataclass(frozen=True)
class ResolvedRequirement:
    """用户说了什么 → 系统确定成什么。"""
    requirement_id: str
    requirement: QueryRequirement
    source_id: str = ""            # 逻辑数据源
    table_candidates: tuple[str, ...] = ()
    column: str | None = None
    operator: str = ""
    normalized_value: str = ""
    unit: str = ""
    time_bounds: tuple[str, str] | None = None
    target_grain: str | None = None
    resolution: Literal["resolved", "unresolved", "unsupported"] = "unresolved"

@dataclass(frozen=True)
class RequirementCheck:
    """一项要求的核对证据。"""
    requirement_id: str
    outcome: RequirementOutcome
    reason_code: str = ""
    message: str = ""
    sql_scope: str = ""            # 命中的 SQL 作用域（哪个 SELECT/子查询）
    expression: str = ""           # 命中的表达式原文（截断）
    details: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class RequirementCoverage:
    """一轮核对的完整结论。"""
    version: str
    checks: tuple[RequirementCheck, ...] = ()
    unknown: tuple[str, ...] = ()          # 未能证明的要求编号
    @property
    def status(self) -> RequirementOutcome:
        """汇总：任一 repair → repair；否则任一 unknown → unknown；否则 pass。"""
```

**汇总规则不可调换顺序**：`repair` 优先于 `unknown`。若先看 `unknown`，一句里既有明确漏项、又有拿不准的项时会被降级成"拿不准"，漏项就没人修了。

### 4.4 `QueryRequirement` 的破坏性改动处理

`requirement_id` 与 `required` 是**新增带默认值的字段**，`frozen=True` 的 dataclass 加默认值不破坏现有构造点。但 `app/query_requirements.py` 的 `_to_requirements()` 要负责生成稳定 id。

**id 生成规则**（必须稳定，否则重写回灌无法对齐）：
```
f"{kind}:{span[0]}-{span[1]}"      # 例如 "time:3-8"
```
用 span 而不是序号：span 来自原文位置，同一轮内唯一且可复现。

### 4.5 完成判据

- [ ] `tests/test_query_requirements.py` 从 28 项扩到覆盖新类型；
- [ ] `requirement_id` 在同一输入下逐次可复现（跑两次比对）；
- [ ] 新增的两类 kind 有提取用例（作用域类通常来自组合条件，粒度类来自聚合 + 一对多关系）。

---

## 5. S2-C：条件覆盖核对器（核心）

### 5.1 产物

**新增 `app/nl2sql/requirement_validator.py`**。

### 5.2 接口

```python
class SqlRequirementValidator:
    def check(
        self,
        sql: str,
        requirements: QueryRequirements,
        capability: RuntimeCapability,       # S2-A 产物（运行时判定，非静态目录）
        *,
        dialect: str = "postgres",
        join_plan: JoinPlan | None = None,   # None → 粒度类检查降级 unknown（决策 C）
        resolved: tuple[ResolvedRequirement, ...] = (),
    ) -> RequirementCoverage:
        """在执行前核对 SQL 是否落实了本轮全部要求。

        返回 RequirementCoverage；status 汇总见 RequirementCoverage.status。
        **不做 IO，不调模型。**
        """
```

### 5.3 九类检查（母方案 `:252-262`）

逐条对应「通过所需依据」与「典型必须拦截的错误」：

| # | 检查 | 通过依据 | 必须拦截 |
|---|---|---|---|
| 1 | 数据源/实体 | 执行器、目录与本轮来源一致；目标表/实体有依据 | 显示跨源库，实际统计 Chinook 客户 |
| 2 | 时间 | 正确业务时间列、区间、边界、时区语义一致 | 没过滤、年份错、**出生日期代入入职日期** |
| 3 | 地点/类别/编号 | 目标字段与规范化取值匹配，谓词实际约束目标对象 | 漏国家、错客户编号、**账单国家替代客户国家** |
| 4 | 数值与否定 | 比较方向、开闭边界、单位、排除含义一致 | `>` 改成 `>=`、`不在巴西` 改为 `在巴西` |
| 5 | 聚合 | 对象粒度、聚合类型、参与字段和去重依据一致 | 平均变合计、客户数变发票数、常量替代计算 |
| 6 | 分组 | 所需维度来自正确对象，分组键与结果含义一致 | 客户国家与开票国家混用、删除分组 |
| 7 | 排名 | 指标、方向、前 N 条及已有并列规则一致 | 漏排序、最高变最低、**前 3 变 1000** |
| 8 | 条件作用域 | 目标数据行受到所要求条件约束 | 条件只在无关子查询/注释、`OR TRUE` |
| 9 | 关联粒度 | 外键和基数共同支持统计口径 | 一客多发票后 `COUNT(*)` 当客户数、父表金额重复累计 |

### 5.4 实现约束（母方案 `:264-273`，逐条落实）

- [ ] **不用字符串匹配**。SQL 里出现年份、字段名或 `WHERE` 都不足以证明要求落实。
- [ ] 用 sqlglot 的**作用域、表别名、列来源**解析，逐个核对 `WHERE`、`HAVING`、必要的 `ON`、条件聚合和子查询中的有效条件。
- [ ] 条件置于 `CASE` 等条件聚合时，须确认**只有满足条件的数据贡献目标指标**。
- [ ] `LEFT JOIN` 的 `ON` 与 `WHERE` 不天然等价；外连接、空值及否定语义单独判断。
- [ ] 先覆盖**既有 T4/T5 正确样例使用的结构**（`adventureworks` 的多表 JOIN、`chinook` 的多表与嵌套），不得全拒绝。
- [ ] 支持可证等价：别名变化、`AND` 条件交换、相同含义的区间表达。**无法证明 → `unknown`**（决策 A/B）。
- [ ] 前 N 条**不能**被自动补的 `LIMIT 1000` 满足；聚合结果行数不等于被统计数据量。
- [ ] 原文要求存在歧义时**不从候选 SQL 中挑一种解释**，回到澄清阶段。

### 5.5 可复用的既有实现（不要重写）

`app/nl2sql/semantic_validator.py`（614 行）已经实现了部分底层能力，S2 应复用而非另起：

| 既有函数 | 位置 | 可复用点 |
|---|---|---|
| `_mandatory_conjuncts(node)` | `:192` | 只用 AND 保证的谓词；遇 `OR` 返回 `None` —— **正是作用域检查要的语义** |
| `_plan_table_keys(join_plan)` | `:365` | 必用表 |
| `_allowed_table_keys(join_plan)` | `:375` | 必用 + 可选表 |
| `_immediate_nested_queries(parsed)` | `:390` | 子查询作用域切分 |
| `_scope_aliases(scope, join_plan)` | `:430` | 别名 → 表身份映射 |
| `_scope_columns(expression)` | `:541` | 取作用域内列 |
| `_table_identity` / `_table_alias` / `_resolve_identity` | `:588/:594/:598` | 表身份解析 |
| `_constant_predicate(node)` | `:331` | 恒真/恒假判定 |

**做法**：把这些函数从 `semantic_validator.py` 提到一个共享模块（建议 `app/nl2sql/sql_scopes.py`），两边 import。**不建议复制一份** —— 复制会让两处的作用域语义漂移。

### 5.6 逐类检查的实现要点

**时间检查（第 2 类，最高优先）**

```
输入：requirements 里的 time 要求（如 "2023"）
步骤：
  1. capability.time_columns(目标实体)  ← 运行时从列说明判定
  2. 若无时间列 → outcome = "repair"，reason_code = "entity_has_no_time_field"
     （SC-05：customer 无时间列 → 不能返回无条件总数。message 引用列说明原文）
  3. 若有 → 在 SQL 的 WHERE/HAVING 里找对**该列**的约束
     - 列来源必须是目标实体（防 billing_country 替 customer.country 这类）
     - 值域必须覆盖要求的年份/区间
  4. 找到且等价 → "pass"；找到但不等价（年份改成相邻年）→ "repair"
  5. 结构超出支持范围 → "unknown"
```

**第 2 步为什么是 `repair` 而不是 `unknown`**：「客户表没有注册时间依据」是**能证明的结论**（列说明里 7 列无一含时间语义），不是"拿不准"。这正是 SC-05 要拦的场景，判成 `unknown` 会在 `warn` 模式下直接放行。

**数值与否定（第 4 类）**

- 比较方向：`>` vs `>=` 要区分 —— 母方案 `:257` 明确「`>` 改成 `>=`」必须拦。
- 开闭边界：`time_bounds` 的左闭右开要能表达 `2023-01-01T00:00:00Z <= x < 2024-01-01T00:00:00Z`。
- 单位：`unit` 不一致 → `unknown`（不能默认通过，也不断言错）。
- 否定：`negation` 要求存在时，SQL 必须是 `NOT IN` / `!=` / `<>` / `NOT LIKE`，且**不能**被 `OR TRUE` 中和。

**聚合（第 5 类）**

- `measure` 要求 `avg` → SQL 必须有 `exp.Avg`（可参考 `app/nl2sql/followup.py:181` 的既有写法）。
- `measure` 要求 `count` → 必须有 `exp.Count`；若实体是多的一方，须是 `COUNT(DISTINCT pk)`。
- **常量替代计算**：`SELECT 59 AS customer_count` 必须拦 —— 可用「聚合节点是否依赖表列」判定。

**关联粒度（第 9 类）**

- 复用 `semantic_validator.py:256 _grain_warnings(parsed, join_plan, physical_tables)`。
- `join_plan is None`（语义规划关闭）→ 本类整体 `unknown`，`reason_code="join_plan_unavailable"`（决策 C）。
- 一对多 JOIN 后对父表实体做 `COUNT(*)` → `repair`，reason_code = `grain_doubled_count`。

### 5.7 完成判据

- [ ] `tests/test_sql_requirement_coverage.py` 通过；
- [ ] §7.3 的错误/等价 SQL 注入清单**全部**符合预期；
- [ ] T4/T5 全部正确样例**零误杀**（负控制）；
- [ ] 每一类检查都有「正确写法」与「绕过写法」成对用例（母方案 `:661`）。

---

## 6. S2-D：管道接线

### 6.1 接入位置（母方案 `:239-246` 的顺序不可变）

```text
生成候选 SQL
  → 既有 sqlglot 只读/语法/表字段检查          app/nl2sql/pipeline.py:443 validate_sql(...)
  → 既有关联/粒度检查（适用时）                 app/nl2sql/pipeline.py:508 semantic_validator.check(...)
  → 本轮要求覆盖检查          ← S2 新增        接在 :530 之后、"③ 执行" 之前
  → 追问合并后的要求检查      ← S3 主责
  → 实际执行                                    app/nl2sql/pipeline.py:536 executor.execute(sql)
```

**插入锚点**：`app/nl2sql/pipeline.py` 第 `532` 行 `# ── ③ 执行（只有校验通过才会走到这里）` 之前。

### 6.2 接线代码形态

```python
# 接在既有 semantic_validator 块（:505-530）之后
if self.requirements is not None and self.requirement_gate != "off":
    emit_progress("sql_requirement_validate", "核对查询是否落实本轮条件", "started")
    coverage = self.requirement_validator.check(
        validation.sql, self.requirements, self.capability_runtime,
        dialect=dialect, join_plan=join_plan,
    )
    steps.append(_step(
        len(steps) + 1,
        {"sql": validation.sql,
         "requirements": [asdict(item) for item in self.requirements.requirements]},
        {"status": coverage.status,
         "mode": self.requirement_gate,
         "checks": [asdict(c) for c in coverage.checks],
         "unknown": list(coverage.unknown)},
        elapsed_ms, tool="sql_requirement_validate",
    ))
    blocked = coverage.status == "repair"
    if blocked or (coverage.status == "unknown" and self.requirement_gate == "enforce"):
        error = <汇总原因>
        if attempt < self.max_attempts:
            last_error = <回灌遗漏要求及原因码>     # 母方案 :277
            last_sql = generation.sql
            continue
        break
```

### 6.3 必须遵守的行为

- [ ] **不受语义规划开关控制**（母方案 `:248`）：`self.requirement_gate` 独立于 `self.semantic_planning_enabled`；但 `join_plan is None` 时粒度类降级 `unknown`（决策 C）。
  - ⚠ **不要把这条读成"独立开关"**：母方案 `:248` 的原意是「关掉表定位不等于允许丢条件」，即**闸门必须照常跑**；本文额外保留的 `NL2SQL_REQUIREMENT_GATE` 是**降级/回退开关**（评审 P3），不是与语义规划并列的第二个业务开关。两者不是一回事。
- [ ] **修复回灌**（`:277`）：重写时把遗漏要求 + 原因码回灌给生成器；**每次重写重新跑全部适用检查**，不能只验证刚修的一项。
- [ ] **不加循环**（`:277`）：复用 `self.max_attempts`（`app/nl2sql/pipeline.py:188`，`DEFAULT_MAX_ATTEMPTS`）。
- [ ] **不执行错误候选**（`:279`）：`error` 有值即 break；`sql` 字段仍表示「真正执行过的 SQL」，未执行候选只进 trace。
- [ ] **失败分类**（`:281`）：配置缺失 / 模型失败 / 检查器不支持当前结构 **分别记原因**，不伪装成用户表达不清。
- [ ] **结果扩展复用已验 SQL**（`:283`）：分页、导出若生成新表达式，**仍需重新校验**；新轮次不得靠旧结果缓存绕开检查。
  - 受影响的既有代码：`app/nl2sql/pipeline.py:632` 的 `expanded_semantic`（结果扩展路径）——**必须同样接闸门**。

### 6.4 新增构造参数

`Nl2SqlPipeline.__init__`（`app/nl2sql/pipeline.py:162` 起）新增：

```python
requirements=None,              # QueryRequirements | None；None = 未提取（旧调用方）
requirement_validator=None,     # None 时用 SqlRequirementValidator()
capability_runtime=None,        # None 时用默认 SchemaCatalog + descriptions 构造
requirement_gate=None,          # None → 读 NL2SQL_REQUIREMENT_GATE，默认 "warn"
```

**`requirements=None` 的语义与 S1 一致**：表示「本轮未经提取」（旧调用方、假件测试），与空清单是两回事 —— 不传就不启用闸门，避免打坏既有测试。

**一轮只抽一次**：`requirements` 由 `app/orchestrator.py` 在 S1 那步 `extract_requirements(question, norm)`（`:395` 附近）抽好后传入，**pipeline 内部不重抽**（母方案 §5.1「每轮使用同一份快照」）。

### 6.5 完成判据

- [ ] `tests/test_requirement_pipeline_gate.py` 通过；
- [ ] 断言：**错误候选必须零业务执行**（记录型执行器调用次数为 0）；
- [ ] 断言：修复后通过才执行，修复前所有失败仍留痕；
- [ ] 断言：重试次数不超过既有上限；
- [ ] 断言：**同一反例在语义规划开、关两种配置下都不能通过新增闸门**（母方案 `:512`）；
- [ ] 断言：无 `fact_review` 协商时同样生效（母方案 `:512`）。

---

## 7. S2-E：记录与展示

### 7.1 新增 trace 步骤（母方案 `:332`）

步骤名（建议逐字采用）：

```
query_requirements            # 要求清单（S1 已有 clarification_step 内嵌，S2 独立成步）
query_capability_check        # 能力判定（运行时：时间列/主键/粒度/历史支持）
sql_requirement_validate      # 条件覆盖核对
followup_requirement_merge    # S3 主责，S2 只留名字
```

**输出内容**：要求编号、处理状态、有限原因、可定位检查依据。**不记录秘密，不输出模型思维链**。

> **P1 的落点在这里**：选 (c)「放行 + 核验卡标黄」意味着 `sql_requirement_validate` 步骤**必须**在 `status="unknown"` 时列出 `unknown: [...]`（哪几条要求没验证），且核验卡**必须**显示黄 —— 三处留痕缺一不可，只执行不标黄等于退化成 (a)。

### 7.2 核验卡接线（母方案 `:334-344`）

- [ ] 在 `app/fact_review.py::_source_checks`（`:330`）的 `nl2sql` 分支追加两项检查：

```python
checks.append(FactReviewCheck(
    code="sql_requirement_coverage",
    result=<coverage 汇总>,      # pass→passed / repair→failed / unknown→unknown / 未跑→not_run
))
checks.append(FactReviewCheck(
    code="query_capability_check",
    result=<能力判定>,           # 时间列缺失等 → failed；说明缺失 → unknown
))
```

- [ ] 映射规则（母方案 `:338-342`）：

| 条件覆盖状态 | `FactReviewCheck.result` |
|---|---|
| `pass`（已证明要求被满足） | `passed` |
| `repair`（明确遗漏或错误条件） | `failed` |
| `unknown`（不能证明） | `unknown` |
| 未执行检查 | `not_run` |
| 不适用 | **不生成「通过」项**；原因写入摘要或 trace |

- [ ] 核验卡增加「已识别的条件」与「尚未解决的条件」摘要（复用 `FactReviewItem.summary/reason`）；
  - ⚠ **P1 选 (c) 的硬要求**：`unknown` 必须在核验卡上**显示为黄**（`result="unknown"`），**不得**因 SQL 执行成功就显示 `passed`。这是 (c) 区别于 (a) 的唯一可见落点，前端要能看出"哪几条没确认"，不能只给一个整体绿灯。
- [ ] **单源事实不因检查通过而升级语义状态**（`:344`）：`status` 不自动从 `candidate` 升为 `supported`。HTTP 200、流程结束、引用有效、`*_checked=true` **均不能代替**条件检查结果。

### 7.3 前端（母方案 `:346-353`）

- [ ] 同步 `web/src/utils/trace.ts`（新增步骤名与字段）；
- [ ] 同步 `web/src/components/FactReviewCard.vue`（新增两项 check 的中文标签，参考既有 `app/fact_review.py:19 _FACT_LABELS` 与 `web/src/components/FactReviewCard.vue:63` 的 code→中文映射写法）；
- [ ] 必要时改 `AnswerBubble.vue` 的处理过程面板（新的 `sql_requirement_validate` 步骤要能显示）；
- [ ] **不擅自增加公共类型字段依赖**（`:353`）—— 若必须扩 `FactReviewCheck`，另列契约版本方案后执行。**当前不需要**：`:439` 已有 `unknown`。

### 7.4 完成判据

- [ ] 旧客户端（无 v2 协商）仍正常显示，**不补造「条件均通过」**（`:352`）；
- [ ] 澄清以正常对话卡呈现，不显示成服务错误，**不继续播放不存在的查询步骤**（`:349`）；
- [ ] SQL 生成失败与需用户补信息使用**不同文案**（`:350`）；
- [ ] **`unknown` 在核验卡上是 `unknown` 而非 `passed`**（P1 选 (c) 的验收点）：造一条结构未支持、证明不了 `pass` 的样例，端到端断言核验卡显示黄。

---

## 8. S2-F：多轮与跨源的接口预留（S3 主责）

**S2 只做接口预留，不改行为。**

- [ ] `app/nl2sql/followup.py::validate_followup_sql`（`:128`）当前只比 `WHERE`（`_where_conjuncts` `:297`），**HAVING 被删仍通过**（母方案 P09）。S2 阶段：在 `RequirementCoverage` 里为 `join_grain` / `value_scope` 预留 `requirement_id`，使 S3 能把「前轮已确认条件」纳入核对输入。
- [ ] `app/memory.py` 记录已确认条件及来源（**不存模型自由总结**，`:287`）—— S3 实施，S2 只确认接口能承载。
- [ ] `app/fact_planner.py` / `app/tools/cross_source_demo.py`：SC-31（两工单只处理第一个）、SC-32（带工单号但只问设备名称）在 S2 阶段**只登记不实现**，因为母方案 `:306-316` 归在路由收紧，跨源部分属 S3。
  - **例外**：SC-32 的「不运行修复时限判定」若能在 `requirement_validator` 里以「对象数 > 1 或指标不匹配」判 `repair`，则 S2 可低成本覆盖 —— 待实现时评估。

---

## 9. S2-G：测试

### 9.1 新增测试文件（母方案 `:492-505`）

```
tests/test_query_requirements.py       # 已有（28 项），扩
tests/test_query_capabilities.py       # 新（运行时判定，非静态目录）
tests/test_sql_requirement_coverage.py # 新（核心）
tests/test_requirement_pipeline_gate.py# 新
tests/test_requirement_followup.py     # 新（S3 主责，S2 建骨架）
tests/test_requirement_routing.py      # 新（S1/S2 交界）
tests/fixtures/semantic_coverage/      # 已有（6 文件，19 项参考结果自洽）
```

**现有存量（不得退化）**：`tests/test_query_requirements.py` 28 项、`tests/test_clarification_integration.py` 10 项、`tests/test_nl2sql_literal_disambiguation.py` 12 项。

### 9.2 分层测试（母方案 `:476-483`）

| 层 | 方法 | 能证明 | **不能冒充** | S2 必做 |
|---|---|---|---|---|
| L1 纯函数 | 条件提取、能力判断、合并、AST 校验 | 规则与类型正确 | 模型在线效果 | ✅ 必做 |
| L2 错误候选注入 | 固定模型候选 + 真实管道 + **记录型执行器** | 错误是否在执行前被阻止 | 线上错误发生比例 | ✅ 必做 |
| L3 隔离数据 | 有边界与干扰项的只读测试库 | SQL 语义和结果正确、等价写法可通过 | 当前业务真实效果 | ✅ 必做 |
| L4 已曝光回归 | T1/T3/T4/T5/T7、原问数/RAG、多轮 | 已有能力是否退化 | 未见题泛化 | ✅ 负控制必做 |
| L5 真实三路 | 合法登录、实际模型、只读库、实际文档、浏览器 | 当前候选可运行 | 无偏总体效果 | 建议做 |
| L6 独立新题 | 未用于修复的新写题 | 改进超出已知开发例子 | 对全部业务的保证 | 建议做（24 题） |

### 9.3 错误 SQL 与等价 SQL 注入清单（母方案 `:517-540`，逐条落实）

**执行器用记录型替身，统计被调用次数。**

| # | 变体 | 预期 | 本计划归属 |
|---|---|---|---|
| 1 | 删除明确时间条件 | 拦截 | 时间类 |
| 2 | 年份换成相邻年、边界日改错 | 拦截 | 时间类 |
| 3 | 用错误业务时间列替换 | 拦截 | 时间类 |
| 4 | 地点/编号过滤删除或取值改变 | 拦截 | 地点类 |
| 5 | `>` 替换成 `>=`、金额单位错换 | 拦截 | 数值类 |
| 6 | 否定改成肯定 | 拦截 | 否定类 |
| 7 | 过滤条件与 `OR TRUE` 组合 | 拦截 | 作用域类 |
| 8 | 把条件放进无关子查询/仅写进注释 | 拦截 | 作用域类 |
| 9 | `HAVING` 删除或移到错误粒度 | 拦截 | 分组类 |
| 10 | `AVG` 改 `SUM`、`COUNT DISTINCT` 改普通计数 | 拦截 | 聚合类 |
| 11 | 排名方向、指标、N 值改变 | 拦截 | 排名类 |
| 12 | 一对多关联重复父表统计 | 拦截或**明确未能证明** | 粒度类 |
| 13 | 常量代替用户要求的实际计算 | 拦截 | 聚合类 |
| 14 | 仅重命名表别名、输出别名 | **通过** | 等价类 |
| 15 | 同义日期表达、AND 条件顺序交换 | **通过** | 等价类 |
| 16 | 条件在 CTE/子查询中等价下推 | **可证时通过** | 等价类 |

### 9.4 反向检查（母方案 `:540`，易漏）

> 「在测试专用替身中让某个校验器**固定放行**，相关错误候选测试**必须失败**，证明验收不是只检查日志里有一个步骤。**不要临时修改生产校验器做此检查。**」

- [ ] 实现方式：测试里注入一个 `AlwaysPassRequirementValidator`，跑错误候选用例，断言**测试套件报失败**（说明用例真的在验行为，不是验日志）。
- [ ] 同样对 `AlwaysPassCapabilityCatalog`（替身：`capability_runtime.py` 的默认实现）做一次。

### 9.5 测试数据（母方案 `:472-470` + `:651`）

已建成的 `tests/fixtures/semantic_coverage/`（6 文件）满足 `:435-468` 的规格：

- 时间锚点 `2024-06-15`；客户 4 条（含 `country=NULL` 一条）、发票 8 条、员工 3 条；
- 19 项参考结果已自洽验证（客户总数 4 / 巴西 2 / 非巴西 1 / 未知 1；2023 发票 3 张 / 去重客户 2；全部 8 张；`>100` 为 4 张 / `>=100` 为 6 张；全量合计 1350.00 / 平均 168.75；最高 3 张 18、14、12；2024 截至 6-15 = 499.99 vs 全年 899.99）；
- **未污染 Chinook**（指纹仍 `base_table_count=23 / total_rows=126887 / content_sha256=4f418185…`）；
- 加载器**必须显式 `--dsn`**，当前为**黑名单拒放**（拒 `chinook`/`postgres`/`cross_source_demo`/`template0`/`template1`）。
  - ⚠ **已知缺陷（复核第 3 条，必须在本轮改掉）**：黑名单＝默认放行。实测 13 个库名中 `adventureworks`/`ict_aw`/`scale_bench`/`verimind_accounts`/任意未知库名/中文库名 **6 个该拒的都放行**；而 `schema.sql:14` 会先 `DROP TABLE IF EXISTS` 同名表。
  - **改法**：改为**允许名单**，仅放行明确前缀（如 `semantic_coverage_*`）+ 库内标记表（两张目标表存在且行数指纹符合 `expected.json`）双重确认，否则 exit 1。

**待补**（母方案 `:466`，SC-20 专用）：分组样本 —— Brazil 6 位（Sao Paulo 4 / Rio 2）、USA 5 位、Canada 1 位。

**待补**（母方案 `:468`）：文档样本 `TEST-DOC-A` / `TEST-DOC-B`（同名不同合同号，设备 EQ-A/EQ-B，首次响应 30/60 分钟，修复 4/8 小时）。**S3 主责**。

### 9.6 测试必须断言的内容（母方案 `:507-515`）

- [ ] 不仅看答案是否含「请补充」，还检查 `need_clarify`、**原因码**、**生成器/执行器调用次数**、**实际 SQL** 和**终态**；
- [ ] 前置缺项必须**零业务调用**；错误候选必须**零业务执行**；候选 SQL **不能被当成执行结果返回**；
- [ ] 修复后通过才执行，修复前所有失败**仍留痕**；重试次数不超过既有上限；
- [ ] 同一反例在语义规划**开、关**两种配置下都不能通过新增硬闸门；无核验展示协商时同样生效；
- [ ] 普通接口与流式 `complete.response` 对澄清、失败、正常回答给**一致结果**；
- [ ] **能力判定缺依据、字段不一致、未知 SQL 结构**分别有明确结果，**不默认通过**；
- [ ] **负控制**：正常短问、正确等价 SQL、原有复杂查询**必须通过** —— 禁止「全部拒答」取巧。

---

## 10. S2-H：采集与评分

### 10.1 新增（母方案 `:499-503`）

```
eval/semantic_coverage/development_v0.1/questions.jsonl   # 只含题面
eval/semantic_coverage/development_v0.1/rubric.jsonl      # 判据，独立保存
eval/semantic_coverage/driver.py
eval/semantic_coverage/scorer.py
eval/semantic_coverage/export_predictions.py
eval/reports/semantic_coverage/<运行号>/
```

### 10.2 `driver.py` 行为要求（母方案 `:563-571`）

- [ ] `--cases` **只读题面**，不读判据、Gold 或历史答案；
- [ ] `--base-url` / `--out-dir` / `--timeout` / `--repeat` 为明确参数；**输出目录已有结果时拒绝覆盖**；
- [ ] `--username-env` / `--password-env` 从环境变量读合法测试账号，复用 `eval/multiturn_runner.py::authenticated_client` 的会话及 `Origin` 处理；
- [ ] 每个场景**独立会话**；多轮场景保持本场景会话；**完整保存原始响应、异常、延迟与失败次数，不挑最好一次**；
- [ ] 请求超时**先登记未完成状态**；不自动重放原 POST，也不重跑整包覆盖首次结果；复跑另建运行号并记录理由；
- [ ] 保存来源/配置清单及哈希、数据库/文档指纹、提交号和源码差异哈希；**采集进程环境不能冒称服务进程实际配置**；
- [ ] 日志**不保存**登录密码、会话 cookie、令牌或含凭据连接串。

### 10.3 `scorer.py` 行为要求（母方案 `:580`）

- [ ] 独立读取 `rubric.jsonl` 和原始响应；
- [ ] 验证澄清/停止、条件覆盖、执行记录、实际结果与证据范围；
- [ ] 结果重放使用**只读事务**；
- [ ] **约束不全但偶然数字相同，仍判失败**；
- [ ] 执行错误和超时**保留分母**。

### 10.4 原项目三项指标回归（母方案 `:589-603`）

- [ ] 用 `export_predictions.py` 把本候选真实采集适配为 `JsonlPredictor` 格式；
- [ ] 保留题号、最终 SQL、回答、错误、延迟、澄清状态、**真实召回引用与最终回答引用**；
- [ ] **不能**把所有引用复制成召回结果后假称拥有真实检索记录；**不能**用参考 SQL 填预测 SQL；
- [ ] 运行 `eval/runner.py --predictor jsonl --suite all --dialect postgres`，报告 NL2SQL 执行准确率、RAG 忠实度、检索命中率；
- [ ] 未运行的项**明确写「未运行/阻断」和原因，不能填历史分数**。

---

## 11. 验收场景（母方案 §7，32 项）

标注 S2 主责（✅）、S1 已覆盖（S1）、S3 主责（S3）：

| ID | 输入 | 期望 | 归属 |
|---|---|---|---|
| SC-01 | `一共有多少位客户？` | 正常回答，说明范围 | S1 ✅ |
| SC-02 | 新会话 `有多少位？` | 询问对象；生成/执行/检索调用数为 0 | S1 ✅ |
| SC-03 | 新会话 `帮我看下最多的那个。` | 询问对象和指标 | S1 ✅ |
| SC-04 | `客户 数量` | 与客户总数同口径；不误拒答 | S1 ✅ |
| **SC-05** | `2023年有多少客户？`，无历史口径 | **询问可支持口径；不得返回无条件总数** | **S2 ✅** |
| **SC-06** | `2023年购买过的客户有多少？` | 发票日期约束 + 按客户主键去重 | **S2 ✅** |
| **SC-07** | `2023年一共开了多少张发票？` | 正确日期字段 + 全年左闭右开；遗漏/错年份被挡 | **S2 ✅** |
| **SC-08** | `去年有多少员工？` | 澄清在职/新入职；不默认 `hire_date` | **S2 ✅** |
| **SC-09** | `去年新入职员工有多少？` | 用 `hire_date`；不改当前员工总数 | **S2 ✅** |
| SC-10 | `销售额同比增长率是多少？`，无时间上下文 | 询问本期 | S1 ✅ |
| **SC-11** | `2023年第一季度相比2023年第二季度…` | 两季度不误判缺时间；比较方向按原话 | **S2 ✅** |
| **SC-12** | `今年至今开了多少张发票？` | 尊重「至今」边界 | **S2 ✅** |
| **SC-13** | `巴西有多少客户？` | 客户国家过滤；全量 SQL 不得执行 | **S2 ✅** |
| **SC-14** | `不在巴西的客户有多少？` | 保留否定 | **S2 ✅** |
| **SC-15** | `发票总金额大于100元的有多少张？` | 严格大于；100 与 100.01 边界可区分 | **S2 ✅** |
| **SC-16** | `金额不小于100元的发票有多少张？` | 大于等于；与 SC-15 边界可验证差异 | **S2 ✅** |
| **SC-17** | `发票平均金额是多少？` | `AVG`；`SUM` 候选不得执行 | **S2 ✅** |
| **SC-18** | `金额最高的3张发票` | 正确指标降序、`LIMIT 3`；缺排序/错方向/默认 LIMIT 均失败 | **S2 ✅** |
| **SC-19** | `买过商品的客户有多少位？` | 统计客户实体，防一对多重复 | **S2 ✅** |
| SC-20 | `每个国家客户数，只看客户数大于5的国家` → `再按城市分组` | 保留原人群条件 | S3 |
| SC-21 | `2023年巴西发票金额合计` → `那只看上个月呢` | 替换时间，保留地点与指标 | S3 |
| SC-22 | 有地点过滤 → `取消国家限制` | 允许显式删除，记录来源 | S3 |
| SC-23 | 不同用户/会话交错查询 | 互不串用 | S3 |
| SC-24 | 两位具体客户 → `他消费了多少？` | 询问是哪位 | S1 ✅ |
| SC-25 | 已明确单一客户编号 → `他消费了多少？` | 编号是合法锚点 | S1 ✅ |
| SC-26 | 新会话 `它的质保期是多久？` | 询问对象 | S1 ✅ |
| SC-27 | 无文档上下文 `合同里怎么规定的？` | 询问合同及条款对象 | S1 ✅ |
| SC-28 | 已明确 KB-001 → `那GPU呢？` | 用本轮继承的文档范围 | S3 |
| SC-29 | 比较指定两份合同响应时限 | 正常跨文档取证 | S3 |
| SC-30 | 已支持的单工单修复时限例问 | 既有严格核验正常；**不被新增澄清误挡** | S2 负控制 ✅ |
| SC-31 | 同时询问两个工单是否达标 | 不支持时澄清/提示分别问 | S3 |
| SC-32 | 带工单号但仅问设备名称 | 不运行修复时限判定 | S2 评估 |

**S2 主责：SC-05/06/07/08/09/11/12/13/14/15/16/17/18/19 —— 共 14 项。**

> **`unknown` 口径在这张表上的体现（P1 裁定为 (c)）**：上表「期望」列写的是**应该发生的行为**，不是"必须判 `pass`"。
> 按决策 A/B，`warn` 模式下 `unknown` **照常回答**，但核验卡标黄并列出未确认的条件。
> 例如 SC-30（已支持的单工单修复时限）若因结构未支持而判 `unknown`，**不算失败** —— 但核验卡上必须看得出来。

---

## 12. 验收门槛（母方案 §10）

### 12.1 六项指标（`:693-701`），**不合并为一个含糊的通过率**

| 指标 | 计算方式 | 内部发布要求 |
|---|---|---|
| 必要澄清正确率 | 应前置澄清且实际澄清、零业务调用 / 应前置澄清场景 | 已登记必测场景全部满足 |
| **错误候选拦截率** | **错误 SQL 未进入执行器的候选 / 注入错误候选** | **§9.3 已支持检查范围内全部拦截** |
| **静默遗漏数** | **已识别的实质要求未落实、未告知仍返回完整业务答案的次数** | **必测场景为 0；任何新发现均登记阻断** |
| 正常问题保持率 | 应可回答且语义/结果正确 / 应可回答场景 | 必测正常控制全部通过；**原有正确关键样例无新增失败** |
| 条件覆盖率 | 已证明覆盖的要求数 / 本轮要求总数 | **辅助诊断；不能用大量简单条件稀释一个关键漏项** |
| 端到端场景通过率 | 行为/条件/结果/引用均满足 / 全部场景 | 分开发/未见题报告，**失败和超时不删分母** |
| 实际开销 | 新增模型调用数、DB 调用数、拒答前耗时、总耗时 | **前置澄清不触发业务调用**；不开无限循环，耗时前后实测 |

**`:703` 关键判定**：
- 结果数值**碰巧正确**，但漏必要条件、查错来源或漏处理对象 → **仍判失败**；
- 明确问题因系统生成失败而安全失败 → **不能计为「正确回答」**，只在拦截指标中体现有效停止。

> **`unknown` 在评分里的位置（P1 裁定为 (c) 后的口径）**：
> - `unknown` **不算拦截成功**，也**不算静默遗漏** —— 它是第三类，必须**单列计数**，不能混进「错误候选拦截率」的分子；
> - 静默遗漏的判据是「**未落实、未告知**仍返回完整业务答案」。`unknown` 若如实标黄并附说明，就**不构成静默遗漏**；若吞掉不显示，则**构成**（退化成 (a)）。
> - 因此 `summary.md` 必须同时给出：`pass` / `repair`（拦截）/ `unknown`（未确认）**三个数**，只报一个总数等于掩盖第三态。
> - 转 `enforce` 的前置条件据此可量化：**T4/T5 场景下 `unknown` 计数为 0**。

### 12.2 发布检查表（`:709-721`，S2 相关项）

- [ ] 无对象/指代/必需口径不明时，实际前置停止，**未交给模型自行猜**；
- [ ] 时间、地点、运算符、否定、指标、分组、排名和粒度**有明确支持范围及反例**；
- [ ] **错误候选没有在首次执行后才被指出**；**所有执行入口均受适用闸门约束**；
- [ ] **`unknown` 有明确处置**：`warn` 下放行 + 核验卡标黄 + trace 列出未确认项；`enforce` 下拦截；
- [ ] **`NL2SQL_REQUIREMENT_GATE` 的当前值在验证记录里写明**（不写默认值等于让读者猜是哪档跑的）；
- [ ] 多轮增加/替换/删除条件、`HAVING`、必要子查询语义受保护（S3）；
- [ ] 多工单和不支持指标**不被静默缩减成单工单 SLA 题**；
- [ ] 既有 T1/T3/T4/T5/T7、分页导出、登录、普通/流式以及三路真实请求**均有结果**；
- [ ] 单源卡片**未扩大语义核验声明**，检查通过有真实后端依据；
- [ ] `eval/runner.py` 三项指标来自**本候选真实采集**；未运行项明确列出；
- [ ] **历史题包、原始回答、Gold 和外部锚点未被本修复改写**；
- [ ] 新报告列出**支持范围、未覆盖结构、失败、超时和未完成项**。

---

## 13. 记录与交付

### 13.1 新增验证记录（母方案 `:727-733`）

```
docs/architecture/限定条件保真与主动澄清修复验证记录_v0.1_20261015.md
eval/reports/semantic_coverage/<运行号>/runtime_manifest.json
eval/reports/semantic_coverage/<运行号>/raw_responses.jsonl
eval/reports/semantic_coverage/<运行号>/case_results.jsonl
eval/reports/semantic_coverage/<运行号>/summary.md
```

记录中逐项标明：**计划、已实现、受控通过、真实接口通过、尚未验证**。
**禁止仅凭方案落盘就把功能标为已完成。**

### 13.2 T3 台账（母方案 `:737`）

- [ ] `infra/tasks_evidence.json` 的 T3 历史 20/20 **保留历史范围**，增加本次已确认缺口及新候选证据；
- [ ] 若调整能力状态，明确写「历史开发回归通过，当前通用条件覆盖未完成/已完成哪些范围」；
- [ ] **不篡改旧分数**，也不继续把历史 20/20 解释为开放问题全部通过。

### 13.3 交付给负责人只报四件事（`:741`）

1. 哪些静默错误已拦住；
2. 哪些正常功能仍能用；
3. 当前尚不支持什么；
4. 测试证据在哪里。

**存在关键未通过项时明确阻断发布，不把「安全失败」计成业务回答成功。**

---

## 14. 分阶段可提交切片

母方案 `:368` 要求「每次提交只包含对应改动，避免把当前工作区全部暂存」。

| 切片 | 内容 | 前置 |
|---|---|---|
| **C1** | `app/nl2sql/capability_runtime.py`（运行时能力判定，**不建静态目录**）+ `tests/test_query_capabilities.py` | 无 |
| **C2** | `app/nl2sql/sql_scopes.py`（从 `semantic_validator.py` 提取共享作用域工具）+ 回归确认 614 行文件行为不变 | 与 C1 无关，可先做 |
| **C3** | `app/nl2sql/planning_models.py` 新类型 + `app/query_requirements.py` 扩展 + `tests/test_query_requirements.py` 扩 | 无 |
| **C4** | `app/nl2sql/requirement_validator.py` + `tests/test_sql_requirement_coverage.py`（**核心切片**） | C1+C2+C3 |
| **C5** | `app/nl2sql/pipeline.py` 接线（含 `:632` 扩展路径）+ `tests/test_requirement_pipeline_gate.py` | C4 |
| **C6** | `app/fact_review.py` + `web/src/utils/trace.ts` + `FactReviewCard.vue` | C5 |
| **C7** | `eval/semantic_coverage/*` 采集与评分 | C5 |
| **C8** | 验证记录 + T3 台账更新 | C6+C7 |
| **C0** | 先还清两笔旧账：`tests/fixtures/semantic_coverage/load.py` 改允许名单（复核第 3 条）、恢复 `score_t4_development.py` 的历史清单哈希校验（复核第 4 条） | 无，**最先做** |

**C4 之前的切片对既有存量测试零风险**（纯新增，不改既有路径）。

---

## 15. 风险与已知缺口

| # | 风险 | 依据 | 缓解 |
|---|---|---|---|
| R1 | **误杀 T4/T5 正确 SQL** | T4 现 50/50（AdventureWorks 71 表）、T5 现 20/20 | §6.4 的 `warn` 默认 + §9.5 负控制全绿后才切 `enforce` |
| R2 | **`unknown` 三态处置若被理解成一态** | 母方案 `:271` 与 `:363` 措辞冲突（已按决策 A/B 定案） | 文档与 trace 里三态始终并列出现，`unknown` 不入 `passed` |
| R3 | **覆盖面与测试量同步增长，不可压缩** | 母方案 `:661` | 明确列出「本轮支持的结构」清单，其余如实标 `unknown`，不宣称覆盖完成 |
| R4 | **`semantic_validator.py` 提取共享工具时改坏既有行为** | 该文件 614 行，被 `pipeline.py:508` 与 `:632` 两处调用 | 切片 C2 单独提交，提取后先跑既有回归确认零差异 |
| R5 | **运行时能力判定与实际元数据不一致** | 判定依赖 `schema_meta.json` + 列中文说明，说明缺失即无依据 | §3.6：缺依据 → `unknown`，**不默认通过**；换 schema 时自动适配而非手工维护第二份清单 |
| R6 | **多轮路径未接闸门**（S3 主责） | `app/nl2sql/followup.py:297` 只比 `WHERE`，`HAVING` 被删仍通过 | S2 只留接口；在验证记录里**明确标注该缺口未闭合** |
| R7 | **扩展/分页路径绕过闸门** | `app/nl2sql/pipeline.py:632` 是第二条执行路径 | §6.3 明确要求同样接入，并在测试里断言 |
| R8 | **工作区有大量未提交队友改动** | 分支 `codex/unified-fact-verification-implementation-20261015` | 提交时**只 `git add` 本切片文件**，逐项核对，不整体暂存 |

---

## 16. 待确认事项（需负责人或方案作者答复）

**剩余 3 项**：

1. **SC-32**（带工单号仅问设备名称）是否纳入 S2 低成本覆盖，还是随 S3 的路由收紧一起做？
2. **`semantic_validator.py` 共享工具提取**（切片 C2）是否接受？若否，需评估复制带来的语义漂移风险。
3. **能力判定的判据口径**：`time_columns` 是否只认「时间/日期/年/月/日」类说明词？若 T4（AdventureWorks，中英混排说明）实测出现漏判，需补词表 —— 这个补充不影响架构，先按最小词表起步。

**已定案、无需再决策的四项**：

| 项 | 定案 | 出处 |
|---|---|---|
| 第三态表述 | 沿用既有 `pass/repair/unknown`，不新造 `undetermined` | 决策 A |
| **`unknown` 处置（P1）** | **(c) 放行 + 核验卡标黄**；`enforce` 档下等同 (b) 拦截 | 决策 A 后的 P1 小节 |
| **闸门开关（P2）** | **要，`off/warn/enforce` 三级，默认 `warn`** | 决策 B |
| 能力判定方案 | **不建静态能力目录**，运行时从 `SchemaCatalog` 判定；AdventureWorks 71 张表无需登记 | §3.2 |

---

## 17. 与母方案的差异说明

本计划**未偏离**母方案的任何硬边界。相对母方案的补充：

| 项 | 母方案 | 本计划 | 理由 |
|---|---|---|---|
| 闸门开关 | 未提 | **`off/warn/enforce` 三级，默认 `warn`（负责人已裁定）** | 降低 R1 风险（评审 P3）；符合项目架构设计初衷 |
| `undetermined` 处置 | `:271` 与 `:363` 矛盾 | 定案：沿用既有 `pass/repair/unknown`；`unknown` 放行 + **核验卡标黄**，`enforce` 档转拦截 | 代码里本来就有三态，新造词只会分裂两套判据；两个候选其实是同一开关的两级 |
| 共享作用域工具 | 未提 | 切片 C2 提取 | 复用而非复制 `semantic_validator.py` 已有能力，避免语义漂移 |
| 提交切片 | `:368` 只给原则 | 列 9 个可提交切片 C0–C8 | 落地「每次提交只包含对应改动」 |
| 风险清单 | 分散在正文 | 归到 §15 单表 | 便于逐项跟踪 |
