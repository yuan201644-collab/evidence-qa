# 问答响应逐事实展示字段提案

版本：提案 v0.3（A/B/C 已同意；不是已生效契约）
日期：2026-10-14
状态：根据 B、C 对 v0.2 的回复收紧规则；负责人同意采用独立 Compose 隔离演示数据库。此文件保留为已评审设计基线；实现已在隔离分支启动，公共契约增量记录见 `docs/契约设计_v0.4.md` 和实施决策记录。v0.1、v0.2 原稿保留。

## 1. 本版结论与决策边界

B、C 对增量字段方案及四项护栏方向均同意。v0.3 将他们的共同意见整理为可验收规则，并明确“执行到终态不等于查询成功”“检查已启用不等于检查通过”“引用索引有效不等于引用对应正确”。

本提案仍只讨论实际运行的工单—合同服务时限事实计划。它表示“事实项—后端状态—证据引用”的关系，不代表答案中的每句话均已核验；不扩大普通 SQL、普通 RAG、澄清或 KAG 路径的行为。三方确认前，不改公共契约、不接入事实卡。

**数据库隔离决策已确认：使用独立 Compose / 独立数据库承载演示数据，严禁灌入 Chinook，以免污染 T3/T5 指纹基准。本提案不创建容器、不导入数据；具体执行步骤见配套实施计划。**

## 2. v0.3 对 v0.2 的明确修订

### 2.1 `checks`：只投影实际结果，不投影检查开关

以下字段表示“该类检查在本轮是否启用”，不是检查结果，**不得映射成 `checks[].result="passed"`**：

- `customer_scope_checked`
- `event_order_checked`
- `mapping_and_contract_windows_checked_separately`

可展示的检查结果必须有值域明确、可定位到同一任务的结构化结果来源：

- 时限比较可从 `metric_checks[].within_limit` 投影 `passed` / `failed`；缺字段、计算失败或上下文不足时为 `unknown` 或不输出。
- 映射依据合规性须读取 `cross_source_policy_check.output.observations[].status`（`allowed` / `denied` / `undefined`）。`mapping_basis` 原始字符串只是被检查值，本身不能证明通过。
- `excluded_mapping_candidates` 可用于呈现哪些候选被排除及其结构化原因；除非其结果模式明确表达某项检查的成败，不得把“存在排除项”泛化成整个映射检查通过。
- 客户范围、事件顺序、映射与合同有效期等检查，只有在后端存在明确的结果字段或专用结果记录时才可出现在 `checks`。仅有 `*_checked` 布尔值时不展示通过勾选。

检查结果不得从答案文本、`FactRecord.value`、工具名称、检索命中或“流程走到了某一步”推断。没有结构化依据时，省略该检查项或标 `unknown`；不得补造通过状态。整体事实 `status` 仍来自 `FactRecord.state`，不由检查项数量反推。

### 2.2 `execution_status`：任务到达终态，不代表查询成功

`completed` 的定义是：该任务处理器已运行并到达终态、形成对应事实记录；**不保证 SQL 查询成功、数据齐全或事实已支持**。查询 / 检索失败、材料不足和配置缺失必须通过该任务的结构化事实状态及安全诊断信息区分，不得藏在一个“已完成”状态下。

判定顺序：

1. 与该 `task_id` 对应的 `fact_task_not_run` → `not_run`。
2. 与该 `task_id` 对应的 `fact_task_failure` → `failed`。
3. 处理器正常返回并形成 `FactRecord`，且无前两类终态 → `completed`；随后仍按 `FactRecord.state` 表达 `supported`、`insufficient` 等业务结果。
4. 记录缺失或终态矛盾 → 不得默认 `completed`，投影失败或安全降级。

不得仅凭 `FactPlanRun.skipped` 判状态。保留经过允许清单筛选的 `reason_code` / 诊断类别，用于区分“业务数据不足”与“配置 / 执行问题”；不向前端透传原始异常、SQL 或秘密信息。

### 2.3 `evidence_link_status`：最终引用上的唯一精确关联

字段定义：

- `complete`：每条账本 `evidence_ref` 均能唯一、精确映射到同一最终响应 `refs` 中的引用，且没有漏项。
- `partial`：至少一条证据可精确映射，但仍有证据未映射或映射不完整。
- `unavailable`：账本没有证据，或没有任何证据能可靠映射。

`evidence_ref_indices` 使用 **0-based** 索引，在最终 `refs` 完成选择、排序和去重后计算；索引与最终 `refs` 必须在同一个序列化响应中持久化。映射需同时检查索引范围和来源身份的一对一关系。

当前 `evidence_key` 与引用收集使用相同键规则，有助于精确关联，但不能单独证明来源身份总是唯一：SQL 引用缺 `row_pk`，或文档引用缺字符偏移时，`(doc_id, page, section_path)` 等较粗定位可能碰撞。因此：

- 只有完整定位身份唯一匹配时才生成索引。
- 多个不同来源折叠到一个最终引用、候选目标不唯一或定位信息不足时，不得猜配；降为 `partial` / `unavailable`，并隐藏“证据齐全 / 已核验”强状态。
- 不借本提案改变 `refs` 现有去重语义；如需改全局引用键或去重规则，另行评审。

### 2.4 `verification_mode`：由执行结果显式携带

`verification_mode` 只允许公共值 `strict` / `ablation`，必须由装配层已校验的模式值明确传入本轮事实计划结果（例如保存在 `FactPlanRun`），即使提前停止且没有最终 verdict trace 也可读取。

内部 trace 名 `strict_cross_source_verification` / `relaxed_rule_ablation` 必须经显式映射后才可输出为短公共值；不得把内部长名原样透传，也不得从 trace 缺失、答案措辞或检查列表猜模式。模式未知或缺失时，省略 `fact_review` 或安全降级，绝不默认为 `strict`。

## 3. 候选响应结构

字段仍为待审提案，不代表已写入 `AskResponse`：

```json
{
  "fact_review": {
    "schema_version": "1",
    "coverage": "cross_source_work_order_sla",
    "verification_mode": "strict",
    "scope_note": "仅展示本轮事实计划记录的事实，不代表答案中的其他表述均已核验。",
    "items": [
      {
        "fact_id": "repair_duration_within_limit",
        "label": "修复耗时是否符合合同限时",
        "summary": "修复耗时 7 小时 40 分钟，合同限时 8 小时。",
        "status": "supported",
        "execution_status": "completed",
        "evidence_link_status": "complete",
        "checks": [
          {"code": "repair_duration", "result": "passed"}
        ],
        "evidence_ref_indices": [2, 3],
        "reason_code": null,
        "reason": null
      }
    ]
  }
}
```

候选取值：

- `status`：账本状态 `candidate` / `supported` / `contradicted` / `insufficient` / `ambiguous`。
- `summary`：可选、面向用户的事实摘要；只能按任务类型从显式字段白名单确定性组装，不可透传通用 `FactRecord.value` 或 verdict 整段答案。
- `execution_status`：`completed` / `not_run` / `failed`。
- `verification_mode`：`strict` / `ablation`。
- `evidence_link_status`：`complete` / `partial` / `unavailable`。
- `checks[].result`：`passed` / `failed` / `unknown` / `not_run`；只能来自 §2.1 列出的结构化结果。
- `reason_code`：来自后端允许清单的稳定诊断类别；具体代码表须在实现前与相应任务处理器一起确认，不在提案阶段虚构已有代码。

特别禁止透传 `FactRecord.value`：它是通用 `Any`，在 verdict 记录中可能是整段自然语言答案，而不是适合展示的单个事实摘要。

## 4. 前端展示与接口兼容

事实卡只说明事实记录状态与来源引用，不给答案句子贴逐句核验标记。当前账本虽定义 `ClaimEvidenceLink` / `bind_claim`，但生产链路没有调用点；句子级“此句已核验”须另行设计契约。

只有同时满足以下条件，前端才可使用“严格核验通过”类强文案：

1. `verification_mode="strict"`；
2. `execution_status="completed"`；
3. `status="supported"`；
4. `evidence_link_status="complete"`，且证据索引非空并有效。

否则分别呈现事实状态、未执行 / 执行失败、证据关联不足或消融模式，不得用绿色“已核验”暗示通过。`supported` 仅指账本中的事实受到支持，不自动等同于业务结论“满足时限”。

为让严格旧客户端继续收到完全相同的 JSON 键集，本版选择显式协商：客户端请求头 `X-AskData-Fact-Review: 1` 时，`/api/ask` 与 `/api/ask/stream` 才可在最终响应中附带 `fact_review`；缺少该头时必须省略新键，而不是发送 `null`。不支持的头值返回明确的 400，不静默降级。前端新版本显式发送该头，旧客户端不发送该头，流式与非流式使用同一规则；阶段事件仍不得伪造核验进度。

新增字段对本仓客户端的测试只能证明本仓消费端未受影响；不能据此宣称所有外部严格客户端兼容。发布前必须：

- 对冻结的旧响应模型 / 消费端执行兼容检查；
- 明确外部 `extra="forbid"` 客户端的影响边界；
- 通过旧版 `extra="forbid"` 响应模型验证无协商请求仍可解析；有协商请求的消费者必须采用 v0.3 契约。

## 5. 验收门

1. **状态来源**：每个事实状态可回溯到同一任务的 `FactRecord`；文本或工具命中不能生成 `supported`。
2. **检查结果来源**：证明 `*_checked` 开关不会显示为通过；`metric_checks[].within_limit` 与映射策略观察状态逐项正确投影；无结果来源时不能通过。
3. **任务终态**：覆盖 `fact_task_not_run`、`fact_task_failure`、正常终态但事实 `insufficient`、正常支持、查询错误诊断、记录缺失和终态矛盾。
4. **模式隔离**：覆盖严格 / 消融、模式缺失和无 verdict trace 的提前停止；未知与消融不得显示严格核验通过。
5. **引用精确性**：最终 refs 重排 / 去重后映射正确；覆盖越界、无主键 SQL、无字符偏移文档、粗定位碰撞、多来源折叠；任何非唯一情况均降级而不误绑。
6. **双接口一致**：`/api/ask` 与 `/api/ask/stream` 最终 `fact_review` 一致，阶段事件不伪造事实结论。
7. **兼容与范围**：普通 SQL、普通 RAG、澄清、旧后端 / 旧响应均不生成假事实卡；本仓消费端检查与外部严格客户端兼容边界分开记录。
8. **显示边界**：只展示事实卡，不展示答案句子级已核验；桌面 / 手机端清楚表达事实、执行、模式和证据状态。
9. **数据隔离**：跨源验证数据只能进入负责人批准的独立数据库 / Compose；不得改动 Chinook 与其基准指纹。
10. **全量回归**：契约、序列化、流式消费、引用面板、历史会话与评分器相关测试全部通过。任何全局引用键 / 去重语义变更需另列影响评审。

## 6. 确认记录

2026-10-14，负责人转述 B、C 已完整认可本版字段与护栏；负责人同意采用独立 Compose / 独立数据库。本提案据此作为后续实施计划的设计基线，但仍不是已生效的公共契约。

三方意见摘要、负责人决定、兼容协商头选择及本提案哈希已记入 `docs/architecture/AskResponse_fact_review_实施决策记录_v0.1_20261014.md`。实现分支据此同步修改契约、示例 JSON 和测试；精确请求头名称属于实现选择，仍需在合并前让 B/C 对照契约复核。

## 7. 负责人待裁定：演示数据库隔离

本地 `cross_source_demo` 表缺失会阻断验收门 2/3/4。负责人已选择**独立 Compose / 独立数据库与数据卷**，与现有 Chinook 完全隔离。实施前仍须检查容器、数据卷和主机端口；如发现既有状态或端口冲突，不得删除数据卷或转连 Chinook，应先停下核实 / 处理。本提案本身不授权运行 A/B。

## 8. 变更记录

- v0.1：原始字段提案，保留不改。
- v0.2：根据首轮意见补上结构化依据、执行状态、模式、引用关联、句子级边界及兼容门。
- v0.3：吸收 B、C 对 v0.2 的回复，进一步限定检查结果权威来源、终态语义、唯一证据映射与显式模式传递；记录三方同意及独立演示数据库决策。
