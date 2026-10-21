# 问答响应逐事实展示字段提案

版本：提案 v0.1（待 A/B/C 确认；不是已生效契约）
日期：2026-10-14
状态：**等待 B、C 对共享接口变更的意见；未修改 `app/contracts.py`、前后端类型或业务执行行为。**

## 1. 问题与目标

跨源工单—合同流程在内部已经生成事实计划和 `FactRecord`，记录事实值、状态、依赖与证据。但 `FactPlanRun` 在编排器中仅用于生成文本答案和工具轨迹，现有公开 `AskResponse` 没有事实—证据关系字段。前端因此无法可靠地区分“已核验”“已检索但存在冲突”“前置条件阻断后未执行”；从 trace 工具名或答案措辞猜状态会产生错误展示。

本提案仅解决：**把本轮已生成的真实事实记录及其最终引用关系，以可选、限定范围的结构返回给前端。** 不引入新的事实推断，不改 SQL/RAG/跨源判定，不接入 KAG，不覆盖普通单源问答。

## 2. 建议方案

在共享 `AskResponse` 增加可选 `fact_review`：

- 仅在当前工单—合同事实计划实际运行时提供；普通 NL2SQL、普通 RAG、澄清和未进入事实计划的请求保持缺省 / `null`。
- 在兼容问答接口与流式问答接口的最终响应中使用同一结构。处理中事件继续只代表实际收到的阶段事件。
- `coverage` 明确限定为 `cross_source_work_order_sla`，并提供用户可读的范围说明；不得表达为整份答案或所有问题都逐事实核验。
- 每个 item 表示计划中一个真实事实任务。`status` 来自 `FactRecord.state`；`execution_status` 独立表示 `completed` / `not_run` / `failed`，不能把“未执行”冒充为“已检查但不充分”。
- 只输出后端白名单投影后的事实摘要和检查项，不把原始 SQL、内部错误对象、提示词或任意 `FactRecord.value` 整体透传给浏览器。
- 证据采用 `evidence_ref_indices`，索引只指向**同一响应最终 `refs` 数组**。投影须在最终引用去重完成后生成，并对每个索引做范围断言；不可使用工具内部候选集或去重前位置。
- 每项包含显式依赖、实际检查内容及可读原因。未执行的检查标记 `not_run` 或省略，不允许伪造通过状态。

### 建议 JSON 草案

以下仅为讨论样例；字段名、枚举和“是否允许空数组”须经团队批准后才能进入共享契约：

```json
{
  "fact_review": {
    "schema_version": "1",
    "coverage": "cross_source_work_order_sla",
    "scope_note": "仅展示本轮工单—合同事实计划实际记录的核验，不代表其他回答内容均已逐事实核验。",
    "items": [
      {
        "fact_id": "controlled_contract_mapping",
        "label": "设备与合同关系",
        "status": "supported",
        "execution_status": "completed",
        "summary": "已按设备编号找到唯一适用合同。",
        "checks": [
          {"code": "mapping_basis", "label": "映射依据受控", "result": "passed"},
          {"code": "customer_scope", "label": "设备与合同客户一致", "result": "passed"},
          {"code": "mapping_validity", "label": "映射有效期覆盖工单日期", "result": "passed"},
          {"code": "contract_validity", "label": "合同有效期覆盖工单日期", "result": "passed"},
          {"code": "contract_status", "label": "合同状态有效", "result": "passed"},
          {"code": "unique_mapping", "label": "适用合同唯一", "result": "passed"}
        ],
        "depends_on": ["work_order_facts"],
        "evidence_ref_indices": [2, 3],
        "reason": null
      }
    ]
  }
}
```

候选枚举：`status` 沿用 `candidate` / `supported` / `contradicted` / `insufficient` / `ambiguous`；`execution_status` 用 `completed` / `not_run` / `failed`；检查结果用 `passed` / `failed` / `unknown` / `not_run`。各状态的人类可见文案由前端本地映射，不从模型生成。

**界面语义必须拆开**：事实 `status=supported` 表示当前记录的值有证据且通过了对应核验，不表示业务结果一定“满足时限”；一条已可靠算出的超时结果也可以是 `supported`。业务结论（满足 / 超时）应在摘要中明确说明，不能把证据状态当业务判定。

## 3. 投影和响应装配位置

建议新增一个后端纯投影函数，将 `FactPlan`、`FactPlanRun` 与最终 `AskResponse.refs` 作为输入：

1. 以计划任务的 `target_fact` 和固定任务 ID 产生受控中文标签，不暴露任意内部任务串。
2. 按每种已支持任务类型抽取白名单字段，生成简洁摘要与实际检查条目；不支持的值形态须安全降级为“无法形成摘要”，不能尝试 `str()` 拼出任意内容。
3. 根据 `fact_run.skipped`、`fact_task_not_run`、`fact_task_failure` 的真实轨迹区分未执行和失败，再与事实状态并列返回。
4. 在 `merged_refs` 完成去重后，将事实的 `evidence_refs` 映射至最终数组；找不到的来源不生成索引，并记录测试可见的投影问题，不能错指另一条引用。
5. 在有事实计划的普通回答与工具澄清响应上附加投影；早期澄清、普通 SQL/RAG 和其他路径维持兼容。

前端事实卡只读取此对象与现有 `refs`；点击证据时仍复用既有证据面板。无 `fact_review` 时继续显示现有“回答—回答依据—处理详情”，不显示空事实卡。

## 4. 兼容和影响面（待代码审计后定稿）

- `AskResponse.extra="forbid"`，新增字段会改变公开 OpenAPI schema。虽然字段可选，旧客户端是否允许额外字段仍须检查，不能仅凭“可选”宣称完全无破坏。
- 必须同步 `app/contracts.py`、`web/src/types/contracts.ts`、契约示例与契约测试；核对 `/api/ask`、`/api/ask/stream`、本地会话序列化、评分器解析和其他 HTTP 客户端。
- 契约版本依 `README.md` §6.7 在三人批准后递增；本提案不预定版本号、不修改评分口径，也不改写历史响应或评测产物。
- 新字段缺失时，前端按当前界面降级；任何旧响应都不能被补造为逐事实已核验。

## 5. 必要验收测试

1. 公共模型：字段可选、默认缺省兼容；枚举非法值、越界证据索引被拒绝。
2. 事实来源绑定：正常支持、客户冲突、多合同歧义、合同未约定、关键事件缺失、依赖未满足、执行异常各有预期状态与证据索引。
3. 引用映射：最终引用排序变化和去重后仍指向正确来源；未进入最终 `refs` 的内部候选不能被引用。
4. 双接口一致：相同受控事实计划通过 `/api/ask` 与 `/api/ask/stream` 得到相同 `fact_review`。
5. 范围限制：普通 SQL/RAG 与澄清响应不产生该对象；消融或诊断模式不能被错误标成严格核验覆盖。
6. 前端：缺字段时旧布局正常；有字段时逐条展示事实、检查、依赖、缺口和证据；手机端可读、关闭证据后回到当前回答；历史消息刷新后关系不漂移。
7. 全量回归：现有问答、trace、引用、响应 schema 与评分器测试保持通过；单独记录任何 trace 形状变化，避免误认为业务算法效果改变。

## 6. 请团队确认

请 B、C 分别回复：

1. 是否同意 `AskResponse` 增加上述可选且限定跨源工单—合同覆盖范围的 `fact_review` 对象？不同意时请指出接口/前端/评测风险及建议替代方案。
2. 是否接受 `evidence_ref_indices` 绑定同一响应的最终 `refs`，并要求后端投影做严格索引校验？若偏好稳定证据 ID，请指出所需 ID 生成和兼容约束。

**只有 A/B/C 均明确同意后，才按 README §6.7 修改共享契约。** 当前只登记提案，不改运行代码或契约。
