# S2-F 多轮条件合并完整实施计划 v0.1

> 负责人 m13866（原文）：「完整修好，你来规划方案，然后尽快开工」
> 母方案：`docs/architecture/限定条件保真与主动澄清修复实施方案_v0.1_20261015.md` §5.5（`:276-298`）、§5.8（`:332-353`）
> 前置：S1（`661dc59`）、S2-A~E（`45cc421`）、四缺陷修复（`d9caa55`）已落地
> 本计划只描述 **做什么、为什么、判据**，不写时间成本。

---

## §0 要解决的真实问题（用户实测）

```
第1轮：AdventureWorks 公司员工有多少？   → employee_count：7        ✅
第2轮：男的有多少？                      → ❌「请补充要统计的业务对象」
```

三条独立缺陷叠在一起，缺任何一条都答不出来：

| # | 缺陷 | 位置 | 后果 |
|---|---|---|---|
| F1 | **澄清判定不看会话记忆** | `app/clarifier.py::decide` | 第2轮缺业务对象 → 直接追问，上一轮已确认的「员工」不参与判定 |
| F2 | **上下文对条件本身无感知** | `models_sql.py::?` / `app/memory.py` | 存的是上一轮**原句文本**，不是结构化条件；接续时靠拼字符串，无法逐条对账 |
| F3 | **「男」不是词表里的取值** | `app/query_requirements.py` + `schema_meta` | 「男/女」是 `gender` 列的取值，现行提取器只认 `value_filter` 的别名表命中，认不出 |

对照实测（本轮探针 `agent_temp/_locate.py`）：

| 问句 | normalized | entities | requirements | decide |
|---|---|---|---|---|
| `男的有多少？` | 男的有多少？ | `[]` | `[measure]` | **ask** `measure_without_object` |
| `女性员工有多少？` | 女性员工有多少？ | `[员工→employee]` | `[object, measure]` | continue ✅ |
| `只看巴西的呢？` | 只看Brazil的呢？ | `[巴西→Brazil value]` | `[value_filter]` | continue ✅ |
| `2023年有多少客户？` | 2023年有多少客户？ | `[客户→customer]` | `[time, measure, object]` | continue ✅ |

**关键结论**：「男的有多少？」**单独问也答不出**（见 `agent_temp/_probe_s2f2.py` 场景3）——所以 F3 是硬缺陷，不能靠「上下文继承对象」掩盖。反过来，继承对象后若 F3 未修，SQL 仍不会带 `gender='M'`，会答出「全部 7 个员工」——**比追问更糟**（静默给错数）。故 F3 必须先修或同时修。

第二组对照（同一探针场景2）：
```
第1轮：2023年有多少客户？  → 说明「customer 没有时间依据」（SC-05 已修，诚实）
第2轮：只看巴西的呢？      → invoice_count：35   ❌
```
第2轮**丢掉了「2023年」**（invoice 表有 `invoice_date`，2023 年的巴西发票不该是 35），而且答案里没有任何说明。这是 S2-E「静默丢弃」在多轮下的形态。

---

## §1 母方案要求的 8 种多轮操作（`:289-298`）

| # | 用户操作 | 例子 | 保护规则 | 现行支持 |
|---|---|---|---|---|
| O1 | 增加条件 | 「只看巴西的呢」 | 旧条件全保留 + 新条件叠加 | ❌ 拼字符串，无对账 |
| O2 | 替换条件 | 「改成 2024 年的呢」 | 新换旧，不得两个都留 | ⚠ 仅 `replace_group` 有 |
| O3 | 删除条件 | 「不要时间限制了」 | 显式丢弃 **且答案要说明丢了什么** | ❌ 无 |
| O4 | 收窄/放宽 | 「只看上个月」 | 时间条件替换 | ⚠ 靠模型 |
| O5 | 改指标 | 「那平均数呢」 | 条件不动，聚合变 | ✅ `average_measure` |
| O6 | 改分组 | 「按国家分呢」 | 条件不动，分组变 | ✅ `add_group`/`replace_group` |
| O7 | 改对象 | 「那发票呢」 | 对象替换，**旧对象专属条件要重新判定** | ❌ 无 |
| O8 | 改排序 | 「倒过来排呢」 | 只有排序变 | ✅ `rank_top` 部分 |

现况：`app/nl2sql/followup.py` 的 `resolve_followup()` 覆盖 O5/O6/O8 与半个 O2；`validate_followup_sql()` 只在**替换分组**时校验旧维度没被保留。**O1/O3/O4/O7 完全没有实现**，而 O1 恰恰是最常见的追问形式。

---

## §2 设计决策（本计划的三条核心选择）

### 决策 1：新增结构化「条件档案」，而不是继续拼字符串

现行 `SessionMemory._combine()` 把上一轮原句 + `"用户补充或修改："` + 本轮拼成一个长句再交给 normalizer 重新解析。问题：

- **不可对账**：无法知道哪条条件来自哪一轮，无法判断「该继承的有没有继承」。
- **会退化**：拼接后句子变长，normalizer 的改写规则（如「去年」→「2023 年」）作用在两轮文本上，时间基准可能串。
- **已被实测咬过**：SC-11 修复时发现「2023 年销售额增长率…去年」拼在一句里后，字面出现两个同年 token。

改为：**每轮成功执行后，把该轮的要求清单（`QueryRequirements`）与 SQL 一起存进会话状态**（`SessionState.condition_archive`），下一轮做**结构化合并**，合并结果再渲染成给模型的追问指令。原句仍保留（供展示与兜底），但**判定与合并都走结构化路径**。

> 向后兼容：`condition_archive` 缺失（老会话、测试直调）时退回现行拼字符串路径，行为不变。

### 决策 2：澄清判定接入会话记忆，且**继承优先于追问**

`app/clarifier.py::decide` 新增可选入参 `inherited: QueryRequirements | None`。判定顺序调整为：

1. 先算「本轮清单 ∪ 继承清单」（继承的条件标 `source="session"`，本轮标 `source="literal"`）
2. `_decide_from_requirements` 的缺对象/缺度量判定**看并集**，不看本轮单独
3. 只有**并集**仍缺对象才追问

**边界（防止过度放行）**：

- 继承只在 `session_action == "short_followup_attached"` 或 `pending_clarification_resumed` 时生效（`app/orchestrator.py:388` 已有该信号）。孤立提问不继承。
- **换对象时不继承旧对象的专属条件**（O7）：本轮若提取到新 object 且与继承 object 不同 → 旧 object 及其绑定条件（value_filter/time 若绑在旧对象上）标记为 `stale`，不参与并集，且在答案里说明。
- 显式丢弃（O3）优先于继承：用户说「不要时间限制了」→ 继承的 time 条件标记 `dropped`，**不得**再出现在 SQL 里，且答案说明。

### 决策 3：取值条件（F3）从能力层取，不硬编码词表

「男/女」不写进 `query_requirements.py` 的常量表，改为查 `RuntimeCapability.normalized_values(table, column)`（已实现，读 `schema_meta` 的 `sample_values`）与列中文说明。

具体：

- **新增取值匹配器**：把用户词与候选列取值做**双向包含 + 常见同义**匹配（`男` ↔ `M`/`Male`/`男性`；`女` ↔ `F`/`Female`/`女性`）。
- **只在有唯一列命中时才产生条件**：`gender` 列同时有 M/F 时，「男的」唯一命中 `gender`；若两个列都有「男」这个取值 → `unknown`，不猜。
- **产出的条件 kind 仍是 `value_filter`**，但 `normalized` 记**列的规范值**（如 `gender=M`），`surface` 记用户原词（「男的」）——与 S2 已有的「surface 给用户看、normalized 给机器用」口径一致。
- **认不出就不产生条件**（不报错、不猜），后续若因此答错，由 S2-C 闸门的 unknown 如实标黄。

---

## §3 切片与顺序

依赖关系：**F3 独立**（可先做，纯收益）→ F2 是状态层 → F1 依赖 F2 → O1–O8 逐个接入。

| 切片 | 内容 | 依赖 | 判据 |
|---|---|---|---|
| **F0** | 会话条件档案：`SessionState.condition_archive` + `save_success` 写入 + 读取接口 | — | 单测：两轮后档案含第1轮清单与 SQL |
| **F1** | 取值条件识别（F3）：`app/nl2sql/value_matcher.py` + `query_requirements` 接入 | — | 「男的有多少？」提取出 `value_filter gender=M`；「女性员工有多少？」同 |
| **F2** | 多轮合并器：`app/nl2sql/followup_merge.py`（8 种操作 → `MergedRequirements`） | F0 | 8 种操作各 2 个正例 + 1 个反例 |
| **F3** | 澄清判定接入继承（F1） | F0, F2 | 用户的例子跑通；孤立提问不被放行 |
| **F4** | 管道接线：合并结果 → `followup_prompt` → 生成 → `validate_followup_sql` 扩到全操作 | F2, F3 | 端到端两连问 |
| **F5** | 留痕与展示：trace 步骤 `followup_requirement_merge` + 答案说明（继承了/丢弃了哪些条件） | F4 | 核验卡与答案文本 |
| **F6** | 测试矩阵：8 操作 × 正/反 + 用户例子 + 回归 | 全部 | 全量回归不劣化 |

**关键路径**：F0 → F2 → F3 → F4；F1 可并行。

---

## §4 各切片细则

### F0 会话条件档案

`app/memory.py`：

```python
@dataclass
class SessionState:
    ...
    condition_archive: dict[str, Any] | None = None   # 新增
```

- `save_success(session_id, question, confirmed_context=None, query_state=None, condition_archive=None)` —— **新增末位可选参数**（不改既有位置参数，现有调用点零影响）。
  - `tests/test_mainline_clarifier_memory.py` 有 5 处按关键字调用 `save_success`，需核对不被破坏。
- 新方法 `archive(session_id) -> dict | None`（深拷贝，与 `query_state()` 同款）。
- 档案内容：`{"question": str, "requirements": [...], "sql": str, "columns": [...], "table": str, "turn_index": int}`。**要求清单序列化用 `QueryRequirement` 的既有字段**，不新增序列化格式。
- 上限：`max_context_chars`（4000）已限长；档案再限 `requirements` 最多 64 项。

### F1 取值条件识别

新建 `app/nl2sql/value_matcher.py`：

```python
class ValueMatcher:
    def __init__(self, capability: RuntimeCapability | None) -> None: ...
    def match(self, text: str, *, tables: tuple[str, ...]) -> tuple[ValueMatch, ...]: ...
```

- `ValueMatch`：`table/column/value(规范值)/surface(用户原词)/span/confidence`。
- 候选列：`capability.category_columns(table)`（已有，含 `gender`? 需实测确认 —— **F1 第一步必须实测 `gender` 是否在 `category_columns`**；不在则扩 `_CATEGORY_TERMS`，并把理由写进注释）。
- 匹配规则（保守，宁可漏不可误）：
  1. 用户词长度 ≥ 1（中文）且**不是**问法词（多少/几/哪个/哪些…）
  2. 与候选值做 casefold 后 `==` 或互为子串（长度 ≥ 2 防「男」误配「男女」以外内容）
  3. 命中列数 > 1 → 返回 `confidence="ambiguous"`，**不产生条件**
  4. 命中列数 == 1 且值唯一 → 产生条件
- 反例（必须**不**产生条件）：「男性化程度」这类词不在取值表里 → 无命中；「去年」是时间不是取值。

### F2 多轮合并器

新建 `app/nl2sql/followup_merge.py`：

```python
@dataclass(frozen=True)
class MergedRequirements:
    operation: str                     # O1..O8 的枚举名
    kept: tuple[QueryRequirement, ...]
    added: tuple[QueryRequirement, ...]
    replaced: tuple[tuple[QueryRequirement, QueryRequirement], ...]  # (旧, 新)
    dropped: tuple[QueryRequirement, ...]
    stale: tuple[QueryRequirement, ...]     # 换对象后失效的
    unresolved: tuple[str, ...]             # 需要澄清的
    reason: str

def merge_requirements(previous: ArchiveLike, current: QueryRequirements) -> MergedRequirements
```

判定各操作的规则（均**确定性**，不调模型）：

| 操作 | 触发 | 合并行为 |
|---|---|---|
| O1 增加 | 本轮有 object 且 == 上一轮 object，且本轮有新的 value_filter/time | 旧全 kept + 新 added |
| O2 替换 | 本轮 time 与旧 time **同类不同值**（都是绝对年）且出现「改成/改为/换成/换成」 | 旧 time → dropped，新 time → replaced |
| O3 删除 | 出现「不要…限制」「去掉…条件」「不限…」 | 匹配到的旧条件 → dropped |
| O4 收窄/放宽 | 本轮相对时间（上个月/本月）与旧绝对时间 | 旧 time → replaced |
| O5 改指标 | 沿用 `resolve_followup` 的 `average_measure` | 条件全 kept，指标变更记 added |
| O6 改分组 | 沿用 `add_group`/`replace_group` | 条件全 kept（保证 O1 语义） |
| O7 改对象 | 本轮 object ≠ 旧 object | 旧 object → stale；旧对象绑定条件 → stale；本轮条件 → added |
| O8 改排序 | 本轮含排序词、无新条件 | 条件全 kept |

**无法归类时**：`operation="unknown"`，**保持现状**（不做破坏性合并），并把 `current` 的条件全部当 added —— 即退化成「旧 + 新」，不静默丢。

### F3 澄清判定接入继承

`app/clarifier.py`：

```python
def decide(self, question, normalized=None, requirements=None, inherited=None) -> ClarificationDecision
```

- 并集构造：`merged = requirements.merge_inherited(inherited)`（`QueryRequirements` 加一个方法，或就地拼 tuple）。
- `_decide_from_requirements` 内的 `of_kind(...)` 查询全部改看并集。
- **必须新增的反例测试**（防止过度放行）：
  - 新会话第一轮「男的有多少？」→ 仍 `ask`（无继承）
  - 「那它呢？」这类无对象的接续 → 若并集有 object 则 continue，否则 ask

### F4 管道接线

`app/orchestrator.py`：

- `:387` 处多读一个 `previous_archive = self.session_memory.archive(session_id)`
- `:424` 后：若 `session_action` 是接续类且 `previous_archive` 存在 → `merged = merge_requirements(previous_archive, requirements)`；否则 `merged = None`
- `:425` `decide(..., inherited=merged.kept if merged else None)`
- `:510` 的 `resolve_followup` 之外，新增 `merged` 参与 `followup_context`（`followup_prompt` 渲染 kept/added/dropped/stale 四段）
- `:635` `save_success(..., condition_archive=self._archive_from(runs, requirements))`

`app/nl2sql/followup.py`：

- `followup_prompt(instruction)` 扩：若 `instruction` 带 `merged` 字段，渲染「必须保留的条件 / 本轮新增 / 已显式丢弃（不得出现）/ 已失效（不得出现）」四段。
- `validate_followup_sql` 扩到全部操作：至少新增
  - `dropped` 里的条件**不得**出现在候选 SQL（复用 `_where_conjuncts` 做键比对）
  - `kept` 里的 WHERE 条件必须仍在（现行只对 replace_group 校验了旧的没被留）
  - `stale` 里的列**不得**出现

### F5 留痕与展示

- trace 新步骤 `followup_requirement_merge`：`tool="followup_requirement_merge"`，output = `MergedRequirements` 的可序列化形式。
- 答案文本：新增 `_followup_note(merged)`，与 S2 已有的 `_coverage_note` 同款风格 ——
  - 有 kept/added：「本轮在上一轮基础上…」
  - 有 dropped：**必须说明**「已按你的要求去掉：『2023年』」
  - 有 stale：「换了统计对象后，上一轮的『巴西』不再适用」
- 核验卡：新增 check `code="followup_condition_merge"`（沿用 `passed/failed/unknown/not_run` 四值；无多轮上下文的题 → `not_run`）。

### F6 测试

新建 `tests/test_followup_merge.py`、`tests/test_value_matcher.py`、`tests/test_memory_condition_archive.py`，并扩 `tests/test_clarification_integration.py`：

- 用户的例子端到端（两连问）—— 必须打 **`Orchestrator.answer`**，不直调子模块（教训：S1 集成测试第一版直调绕过了接线点，变异测不出）。
- 8 操作 × (2 正例 + 1 反例)。
- **变异验证**（强制）：至少三个变异必须被抓 ——
  - M1：F3 的继承判定改成「只看本轮」→ 用户例子必须失败
  - M2：F4 的 `save_success` 不写 `condition_archive` → 两轮测试必须失败
  - M3：`dropped` 条件不做校验 → O3 反例必须失败

---

## §5 硬边界（不做的事）

1. **不新建独立的对话状态机**：结构化的东西只加在 `SessionState` 与 `MergedRequirements`，不引入第二个 memory 实现。
2. **不改 `prepare_turn` 的返回值签名**（`tests/test_mainline_clarifier_memory.py` 有 5 处二元组解包；`prepare_question` 是薄封装）。新增能力一律走新方法。
3. **不把「男/女」等取值写进常量表**：一律走 `RuntimeCapability`，否则换库（AdventureWorks 的 Gender 是 `M`/`F`）会失效。
4. **合并失败不静默丢**：`operation="unknown"` 时退化为「旧 + 新」，宁可多带条件（闸门会如实标注）不可少带。
5. **不放松 S2-C 闸门**：本切片只让闸门看到更完整的条件，不改闸门判定逻辑。
6. **继承只在接续轮生效**：孤立提问不继承，避免「上一轮的对象」污染新会话。
7. **不做指代的语义解析**：「它/这个」仍走 S1 的 `unresolved_reference` 追问，不在本切片内改成猜。

---

## §6 验收门槛

| # | 门槛 | 判据 |
|---|---|---|
| A1 | 用户举的例子端到端通过 | 「AdventureWorks 公司员工有多少？」→「男的有多少？」第二轮答出**男性员工数**（且不等于 7） |
| A2 | 单独问「男的有多少？」不再追问 | 答出男性员工数（F1 生效） |
| A3 | 8 种操作全覆盖 | 每种的 2 正例 + 1 反例全过 |
| A4 | 显式丢弃有说明 | O3 的答案含「已按你的要求去掉…」 |
| A5 | 换对象不串条件 | O7 的 SQL 不含旧对象专属条件，且答案说明失效 |
| A6 | 不静默丢条件 | 合并后 kept∪added 之外的条件只能出现在 dropped/stale，且二者都必须在答案里出现 |
| A7 | 全量回归不劣化 | `tests app eval infra` 的通过数 ≥ 现有基线（1071 项 / 1069 通过 / 1 既有失败） |
| A8 | 语义覆盖集不劣化 | `eval/semantic_coverage` r2 的 19/19 保持 |

---

## §7 风险

| # | 风险 | 缓解 |
|---|---|---|
| R1 | 继承过度放行 → 缺对象的问题不再追问 | A2 的反例测试 + 只在接续轮生效 + 全量回归看 T5/多轮用例 |
| R2 | 取值匹配误命中（如「男」命中「男士用品」类目） | 命中多列即 `ambiguous` 不产条件；只认 `sample_values` 与说明文本，不从列名猜 |
| R3 | `save_success` 加参数破坏既有调用点 | 参数加在末位且默认 `None`；先跑 `test_mainline_clarifier_memory.py` |
| R4 | 合并器规则过多导致不可维护 | 8 种操作用**表驱动**（一个判定函数表），不写 8 个 if 分支树 |
| R5 | 与队友未提交的 pipeline/linker 重写冲突 | 本切片只碰 `app/memory.py`、`app/clarifier.py`、`app/orchestrator.py`、`app/nl2sql/followup.py`、新增 2 模块；`app/nl2sql/pipeline.py` **不改** |

---

## §8 开工顺序

```
F1（取值识别，独立，先做，立刻让「男的有多少？」可答）
  ↓
F0（会话档案）
  ↓
F2（合并器 + 表驱动规则）
  ↓
F3（澄清接入继承）→ F4（管道接线）→ F5（留痕展示）
  ↓
F6（测试矩阵 + 变异验证 + 全量回归 + 语义覆盖集复跑）
```

F1 先做的理由：它单独就能把用户例子里的「男的」认出来；即便 F0/F2 尚未完成，「女性员工有多少？」这条已经能跑（实测 continue），说明**对象+取值的组合是可以立即验证的**。
