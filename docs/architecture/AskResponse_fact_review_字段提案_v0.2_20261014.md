# 问答响应逐事实展示字段提案

版本：提案 v0.2（待 A/B/C 确认；不是已生效契约）
日期：2026-10-14
状态：**按 B、C 首轮意见补充；未修改 `app/contracts.py`、前后端响应类型或业务执行行为。**

## 1. 本版结论

建议继续采用 `AskResponse` 中可选的 `fact_review`，但界面不能只凭事实状态或一条有效引用索引就显示“已核验通过”。只有后端明确记录了严格核验模式、任务确已完成、事实状态受支持，并且所需证据能无歧义地关联到同一响应的最终 `refs`，前端才可使用“已核验”类强文案。

本版把 B、C 的反馈归并为四项后端护栏，并补充两个呈现 / 兼容边界。它仍是**待确认提案**：在三方对具体字段和规则明确同意前，不改共享契约、不接前端事实卡。

## 2. 范围与实现对照

只覆盖实际运行的“工单—合同服务时限事实计划”，不覆盖普通 SQL、普通 RAG、澄清请求或 KAG。`fact_review` 表示计划事实与证据的关系，不表示自然语言答案中的每个句子均已核验。

本次对照了提案分支 `f24fdee` 所基于的 `origin/main`（`d03c3d9`）及相关实现：

- `app/evidence_ledger.py`：`FactRecord` 有 `value`、`state`、`diagnostic_code`、`evidence_ids`、`evidence_refs`；账本有候选证据和最终响应证据。`FactRecord.value` 是通用值，不能整体透传到浏览器。
- `app/fact_executor.py`：`FactPlanRun.skipped` 同时包含“未执行”“任务失败”“处理器未提供本轮可核验证据”等不同情况；`fact_task_not_run` 和 `fact_task_failure` 才是区分未执行与失败的专用轨迹。正常处理器返回后会形成 `FactRecord`，状态不足不等于任务没有运行。
- `app/cross_source_fact_plan.py` / `app/orchestrator.py`：严格 / 消融模式在装配时通过 `verification_enabled` 影响校验行为；部分最终 trace 会写 `validator_mode`，但 `FactPlanRun` 未显式保存模式，且早停路径不保证有带模式的最终判定 trace。
- `app/evidence_ledger.py:evidence_key`、`app/contracts.py:collect_refs` 和 `app/orchestrator.py:_dedup_refs` 使用定位字段去重。SQL 引用缺少 `row_pk`、文档引用缺少字符偏移时，定位键可能不足以区分不同来源片段。索引越界校验只能检查数字范围，不能发现“索引有效但对应错了来源”。
- `/api/ask` 与 `/api/ask/stream` 的最终结果均使用 `AskResponse`；阶段事件另行发送。`AskResponse` 配置 `extra="forbid"`，因此不能仅凭“新字段可选”就宣称所有严格旧客户端都兼容。

## 3. 候选响应结构

字段名及枚举仍待团队确认。为明确本版新增的保护信息，草案如下：

```json
{
  "fact_review": {
    "schema_version": "1",
    "coverage": "cross_source_work_order_sla",
    "verification_mode": "strict",
    "scope_note": "仅展示本轮工单—合同事实计划记录的事实，不代表答案中的其他表述均已核验。",
    "items": [
      {
        "fact_id": "controlled_contract_mapping",
        "label": "设备与合同关系",
        "status": "supported",
        "execution_status": "completed",
        "evidence_link_status": "complete",
        "summary": "按设备编号找到唯一适用合同。",
        "checks": [
          {"code": "customer_scope", "label": "设备与合同客户一致", "result": "passed"}
        ],
        "depends_on": ["work_order_facts"],
        "evidence_ref_indices": [2, 3],
        "reason": null
      }
    ]
  }
}
```

候选枚举：

- `status`：沿用账本状态 `candidate` / `supported` / `contradicted` / `insufficient` / `ambiguous`。
- `execution_status`：`completed` / `not_run` / `failed`。
- `verification_mode`：`strict` / `ablation`。模式必须来自本轮执行结果中的显式值；若缺失或无效，不生成声称严格核验的对象。
- `evidence_link_status`：`complete` / `partial` / `unavailable`，表示事实证据到最终 `refs` 的映射质量，不改变后端事实状态。
- `checks[].result`：`passed` / `failed` / `unknown` / `not_run`。检查项只由已登记、可定位的后端结构化结果生成。

## 4. 必须落实的后端护栏

### 4.1 检查项只能来自明确的结构化依据

不能从通用 `FactRecord.value`、答案文本或工具名称推断一串“检查通过”。实现时须为每种受支持的事实任务明确列出可投影字段及其权威来源，例如该任务的校验结果对象或专用结构化 trace 字段。

- 能从明确结构化结果还原的检查项，才输出 `passed` / `failed`。
- 执行过但结果不足以还原单项检查时，不得造出 `passed`；可输出 `unknown`，或不输出该检查项，并在事实摘要 / 原因中说明现状。
- 整体 `status` 仍来自 `FactRecord.state`，不可由检查项个数反向计算。
- 不得把原始 SQL、任意 `FactRecord.value`、内部异常对象或提示词透传给浏览器。

### 4.2 证据索引必须无歧义地对应最终 `refs`

`evidence_ref_indices` 只指向同一最终响应的 `AskResponse.refs`。映射在最终引用选择和去重后进行；除检查索引范围外，还必须确认来源身份是一对一的。

- 有稳定行主键的 SQL 引用、具有稳定字符范围的文档引用，可按完整定位身份尝试精确映射。
- SQL 缺 `row_pk`，或文档缺字符偏移且 `(doc_id, page, section_path)` 不能唯一识别来源时，不得仅凭当前粗粒度去重键强行映射。
- 若多个账本来源折叠到同一最终引用，或存在多个可能目标，输出 `evidence_link_status="partial"` / `"unavailable"`，仅保留能精确匹配的索引；无任何精确匹配时索引为空。
- 在引用身份不完整时，不得为了让界面出现可点击证据而选取“看起来相近”的引用。若需强化全局引用身份或调整既有去重规则，应另做兼容评审，不在本增量字段中悄悄改变 `refs` 语义。

### 4.3 执行状态必须由任务级终态记录决定

不依据 `FactPlanRun.skipped` 单独推断执行状态，因为它承载多个不同原因。按与同一 `task_id` 绑定的权威记录判定：

1. 存在 `fact_task_not_run` 专用轨迹 → `not_run`。
2. 存在 `fact_task_failure` 专用轨迹 → `failed`。
3. 存在处理器正常返回后形成的 `FactRecord`，且无前述终态 → `completed`。例如处理器执行成功但证据不足，应为 `completed + insufficient`。
4. 记录缺失或终态互相矛盾 → 不得默认 `completed`；该项投影失败，响应不得展示为已核验。

### 4.4 严格核验 / 消融模式必须显式随运行结果传递

模式值须由装配配置显式传到事实计划执行结果，再由投影函数读取；不能从答案措辞、某个可选 trace 字段或“看起来执行了哪些检查”推测。早停题没有最终 verdict trace 时，模式仍须可识别。

- `strict` 下才可能显示“已核验”类文案，并且还须满足下方前端门槛。
- `ablation` 必须明确标注“消融模式 / 未按严格规则核验”，不显示绿色“已核验通过”状态。
- 模式值缺失、未知或传递链断裂时，省略 `fact_review` 或安全降级；不得默认视为 `strict`。

## 5. 前端语义和回答绑定边界

事实卡只表示“事实项—后端状态—来源引用”，不是自然语言答案主张的逐句校验。当前提案**不**定义“回答中的某句话对应哪些事实项”，不在句子旁贴“此句已核验”标记。

前端只有同时满足以下条件时，才可显示“已核验”类强状态：

1. `verification_mode="strict"`；
2. `execution_status="completed"`；
3. 后端 `status="supported"`；
4. `evidence_link_status="complete"` 且至少有一条有效证据索引。

否则分别呈现“候选 / 未能确定 / 未执行 / 执行失败 / 证据关联不完整”等状态；不可将 `supported` 等同于业务结论“满足时限”。它只表示后端事实账本的状态。消融模式、模式缺失、引用映射不完整或旧响应缺少 `fact_review` 时，不得呈现为严格核验通过。

若将来要在答案句子旁显示核验标记，须另行设计显式 `claim_id` 与主张文本片段的关联；不能把本提案的事实卡扩大解释为逐句溯源。当前账本虽有 `ClaimEvidenceLink` 类型，但本提案不假设生产链已登记答案主张绑定。

## 6. 接口兼容与范围

- `/api/ask` 和 `/api/ask/stream` 的最终响应复用同一个可选 `fact_review` 结构；流式处理中事件继续只反映真实阶段，不新增或伪造核验进度事件。
- 必须用冻结的旧版响应模型和旧前端消费路径做兼容测试。若严格旧客户端拒绝额外字段，应先明确兼容策略（例如版本化或协商），不能以字段“可选”为由跳过发布门。
- 前端遇到字段缺失、未知枚举或旧后端时，维持普通“回答—来源依据—处理详情”布局；不显示空事实卡，不推断已核验。
- 字段只在实际运行此事实计划时返回。普通 NL2SQL、普通 RAG、早期澄清及其他不含本事实计划的路径不生成该对象。
- 契约获三方一致同意后，按 `README.md` §6.7 同步 `app/contracts.py`、示例 JSON、契约测试，并递增契约版本；评分口径、历史响应和跨源业务判定不随本字段改动。

## 7. 验收门

1. **状态来源**：每个可见事实状态可追溯到同一任务的 `FactRecord`；任意模型文本或工具命中本身不能产生 `supported`。
2. **检查来源**：逐种任务验证允许输出的检查项与后端结构化字段对应；删除 / 缺失依据后，不得仍显示通过勾选。
3. **执行状态**：覆盖 `fact_task_not_run`、`fact_task_failure`、正常完成但 `insufficient`、正常完成且 `supported`、记录缺失和矛盾轨迹。
4. **模式隔离**：严格与消融模式、模式缺失、含早停无最终 verdict trace 的样例分别验证；消融与未知模式不得显示“已核验通过”。
5. **引用对应**：最终 `refs` 重排 / 去重后索引仍指向相同来源；索引越界拒绝；SQL 无主键、文档无偏移、粗粒度键碰撞、多候选折叠等情形必须降级为不完整 / 不可用，不得错绑。
6. **双接口一致**：相同请求经 `/api/ask` 与 `/api/ask/stream` 得到一致的最终 `fact_review`；阶段事件不伪造事实结论。
7. **范围与兼容**：普通 SQL、普通 RAG、澄清、旧后端和旧客户端均走普通回答显示；通过旧模型 / 旧消费端兼容检查后才允许发布。
8. **前端**：只展示事实卡，不声称句子级核验；桌面与手机端均能区分事实状态、执行状态、模式和证据关联缺口。
9. **全量回归**：契约、响应序列化、流式消费、引用面板、历史会话和评分器相关测试全部通过；若引用身份规则另有改动，单独列项审查。

## 8. 请 B、C 对修订版逐项确认

请明确回复“同意”或指出需修改的具体条款：

1. 是否同意四项后端护栏：检查项须有结构化来源、证据映射须无歧义、执行状态依据任务级终态、严格 / 消融模式显式传递？
2. 是否接受新增候选字段 `verification_mode`、`evidence_link_status`，以及引用映射不完整时不显示“已核验”类强文案？
3. 是否同意当前只做事实卡，不把它解释为答案句子级核验？
4. 是否接受把严格旧客户端兼容检查设为发布门槛；如不兼容，先确定版本协商策略？

**只有 A/B/C 对这些具体规则均明确同意后，才进入契约和实现修改。**本文件不是实现授权，也不改变 v0.1 原稿及已推送分支历史。
