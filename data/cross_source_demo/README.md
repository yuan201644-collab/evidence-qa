# 工单与维保合同跨源样例数据 v0.1

状态：合成演示数据；已接入一个默认关闭的主项目工具，并完成在线样例及隔离失败分支验证。所有组织、设备、合同、工单和时间均为虚构。  
日期口径：时间戳均带 `+08:00`，即东八区；合同有效期按包含首尾日期理解。

## 用途

这套数据用于讨论和验证“结构化记录 + 文档条款”如何支持同一结论，不替代项目现有 Chinook / AdventureWorks 或知识库语料。它刻意保持小规模，重点覆盖：

- 能用稳定编号连接工单、设备、合同和条款；设备名称仅供阅读，不作为连接键。
- 区分首次响应、现场到达和修复完成，防止拿一个时限代替另一个。
- 根据合同编号、设备范围和有效期判断适用条款。
- 处理超时、合同已过期、同名不同设备、缺失完成事件等情况。

## 文件

### 结构化记录

| 文件 | 一行代表什么 | 主要连接字段 |
|---|---|---|
| `structured/equipment.csv` | 一台设备 | `equipment_id` |
| `structured/contracts.csv` | 合同元数据与有效期 | `contract_id` |
| `structured/contract_equipment.csv` | 合同覆盖设备的明确映射及有效期 | `contract_id`, `equipment_id` |
| `structured/work_orders.csv` | 一张工单及其设备 | `work_order_id`, `equipment_id` |
| `structured/work_order_events.csv` | 工单的一个业务事件 | `work_order_id`, `event_type`, `event_at` |

### 文档与评测参考

- `documents/HT-MNT-2026-001.md`：设备 DEV-001、DEV-002 的服务期限定义。
- `documents/HT-MNT-2026-002.md`：设备 DEV-003、DEV-004 的服务期限定义。
- `documents/HT-MNT-2026-003.md`：设备 DEV-005 的已过期合同条款。
- `cases.jsonl`：少量问题、期望判断和期望证据，用于后续评审/评测集设计，不是系统生成的答案。
- `manifest.json`：文件清单、版本和口径说明。
- `schema.sql` 与 `load_postgres.py`：在独立演示数据库建表并载入 CSV；不是在线问答入口。

## 核心关系与口径

工单通过 `equipment_id` 对应设备，再通过 `contract_equipment.csv` 找合同；合同关系必须覆盖工单发生日期。合同编号还需与文档标题/正文中的编号一致。正式回答“是否满足修复时限”之前，必须取到该合同条款。

合同条款中三种计时口径独立：

1. 首次响应：从工单受理到首次响应。
2. 现场到达：从工单受理到现场到达。
3. 修复完成：从工单受理到服务恢复。

时间差按同一工单事件时间戳计算；CSV 时间均含时区。当前条款将“修复完成”定义为服务恢复事件。缺少任一端点事件时不计算该指标。该口径是为样例明确设定的，不代表现实合同的通用定义。

## 建议的最小核验路径

以 `WO-2026-0002` 为例：

1. 从工单记录取 `DEV-002`。
2. 按工单日期找到有效的 DEV-002—合同映射，得到 `HT-MNT-2026-001`。
3. 检索同编号合同，确认“修复完成”时限为受理后 8 小时。
4. 从事件表计算受理至服务恢复的时长，再与 8 小时比较；不要用“首次响应”事件代替。
5. 最终结论需同时引用事件记录和合同条款。缺映射、无有效合同或缺事件时，停止该结论或说明无法判断。

`WO-2026-0005` 对应设备 DEV-005，但其合同于 2026-08-31 到期，工单在 2026-09-18 发生；尽管事件数据齐全，也不能用过期合同给它下达标结论。DEV-005 与 DEV-006 名称相同但编号不同；DEV-006 没有映射记录，不能按名称继承 DEV-005 的合同。

## 局限与使用边界

- 这是小型、人工设计的合成数据，能够验证关系、口径和失败分支的处理，不代表真实企业数据分布或行业泛化能力。
- 问题与期望证据是人工写定的，若据此调试后再在同一批问题上汇报成绩，会产生评测泄漏。正式评估应另留未参与设计的题目，并让人工复核答案与逐事实证据。
- CSV 是中立交换格式；通过 `schema.sql` 和 `load_postgres.py` 可显式导入独立演示数据库。本文件夹不改变默认 Chinook / AdventureWorks 数据库，也不自动导入数据。
- 不应把标准答案回放、模拟工具结果或本数据本身称为主系统实测能力。

## 可选的本地接入方式

这条接入默认关闭。它使用独立 PostgreSQL 容器及独立数据卷，不覆盖项目默认数据库；在线连接仅执行固定参数化 `SELECT`，并在只读事务中运行。合同文档使用项目现有的 BM25 文档切片与来源定位，只检索本目录三份 Markdown，不调用生成模型。这个最小链路尚不等同于完整的“BM25 + 向量融合 + 重排 + 生成”通用RAG。

在 PowerShell 项目根目录启动独立数据库（密码只放在当前终端环境，不写入仓库）：

```powershell
$env:CROSS_SOURCE_DEMO_PASSWORD = '请替换为本机随机密码'
docker compose -f infra/docker-compose.cross-source-demo.yml up -d
$env:CROSS_SOURCE_DEMO_DSN = "postgresql://cross_source_demo:$($env:CROSS_SOURCE_DEMO_PASSWORD)@127.0.0.1:55532/cross_source_demo"
uv run python data/cross_source_demo/load_postgres.py
$env:CROSS_SOURCE_DEMO_ENABLED = '1'
# 可选：单次总预算默认 5000 ms；合同条款检索默认最多 2 次（初查 + 1 次定向补查）
# $env:CROSS_SOURCE_DEMO_TIME_BUDGET_MS = '5000'
# $env:CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT = '2'
uv run uvicorn app.main:app --reload
```

以上是合成演示的工程参数，不是赛题要求或性能承诺。预算允许 1000–30000 ms，文档尝试允许 1–3 次；该工具不调用模型，模型调用数为 0。预算耗尽或补查仍缺条款时停止后续任务并记录状态。2026-10-01 实测显示，驱动连接超时存在粒度限制，1 秒配置约 2 秒返回；同步检索也只能调用前后检查预算。因此不是严格的端到端毫秒级硬截止。

容器启动后先用 `docker compose -f infra/docker-compose.cross-source-demo.yml ps` 确认数据库为 `healthy`，再载入数据。另开一个 PowerShell 请求演示问题：

```powershell
$demoBody = @{
  question = '工单 WO-2026-0001 是否在合同修复时限内完成？'
  session_id = 'cross-source-demo'
} | ConvertTo-Json
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/ask' `
  -Method Post -ContentType 'application/json' -Body $demoBody
```

密码若含 URL 特殊字符，设置 `CROSS_SOURCE_DEMO_DSN` 时要做 URL 编码。初始化脚本只接受数据库名 `cross_source_demo`，且要求五张目标表为空；如果目标表已有数据会拒绝载入，不会清空或覆盖。演示工具只在显式启用后，对包含 `WO-2026-xxxx` 编号的问题优先触发。关闭 `$env:CROSS_SOURCE_DEMO_ENABLED` 或将其设为 `0`，原有默认路由保持不变。

开发演示在线记录见 `docs/architecture/`。原独立七题已完成在线配对及 2026-09-30 盲审，针对意见修复后现作为回归集，不再是未见题。2026-10-01 补强证据、真实检索/数据库故障验证和一键运行入口见 `docs/architecture/阶段C验证与定向修复记录_v0.2.md`，不能当作赛事或真实业务成绩。

### 可选：阶段一私有事实计划原型

当前默认关闭的事实计划原型复用项目真实 NL2SQL（自然语言转 SQL）和完整 RAG（检索增强生成）链路，并在其上执行对象、客户、有效期、条款及事件核验。它与上面的 `cross_source_demo` 原始工具路径是两种隔离实现：仅设置 `CROSS_SOURCE_DEMO_ENABLED=1` 时仍走固定查询与 BM25 演示工具；同时设置以下开关才走事实任务计划。数据库连接串沿用上文设置的隔离 DSN；不要将 DSN 或口令写入文件或提交到仓库。

```powershell
$env:CROSS_SOURCE_DEMO_ENABLED = '1'
$env:CROSS_SOURCE_FACT_PLAN_ENABLED = '1'
$env:CROSS_SOURCE_FACT_PLAN_DSN = $env:CROSS_SOURCE_DEMO_DSN
uv run uvicorn app.main:app --reload
```

事实计划仅在工单编号路由上生效；简单单源问题和默认配置不因此改变。真实 RAG 会加载本地嵌入与重排模型，并将合同索引写入隔离数据库的 `cross_source_demo` 搜索路径。合成样例上的阶段一在线验证和边界说明见 [`docs/architecture/主链路事实核验原型_阶段1实现与验证记录_v0.1.md`](../../docs/architecture/主链路事实核验原型_阶段1实现与验证记录_v0.1.md)。

## 变更记录

| 日期 | 版本 | 说明 |
|---|---|---|
| 2026-09-28 | v0.1 | 新增独立的合成工单—合同样例，用于架构评审和后续适配设计。 |
