# 逐事实核验响应字段：可执行实施与验收计划

版本：v0.1
日期：2026-10-14
状态：实施中；负责人同意独立 Compose 隔离数据库，B、C 已确认 v0.3 方案。实施在 `codex/fact-evidence-workbench-v01` 隔离分支开展；不改主分支和既有数据库。

## 1. 目标与范围

目标是在跨源工单—合同事实计划执行后，为同一份回答可选地返回 `fact_review`：逐项呈现账本事实状态、任务执行状态、严格 / 消融模式、检查结果和指向最终 `refs` 的证据索引。用户可看到哪些事实有依据、哪些未能确定，以及证据在哪里。

不做答案句子级事实标注；不改变普通 NL2SQL、普通 RAG、澄清、KAG 或默认路由；不修改跨源业务判定、评测集和 Gold；不运行新 A/B，也不宣称准确率提升。唯一依据是已获三方确认的 [字段提案 v0.3](AskResponse_fact_review_字段提案_v0.3_20261014.md)。

## 2. 变更边界与预期文件

实施分支限于本功能，合并前先同步最新 `main` 并确认工作区没有混入无关改动。预计触及：

| 区域 | 预期文件 / 产物 | 目的 |
|---|---|---|
| 共享契约 | `app/contracts.py` | 定义 `FactReview`、事实项与检查项模型；`AskResponse.fact_review` 为可选字段；契约版本由 v0.2 升至 v0.3 |
| 投影逻辑 | 建议新增 `app/fact_review.py` | 将账本、任务终态、显式模式和最终 refs 转成安全的响应对象；保持纯函数、可单测 |
| 事实计划执行 | `app/fact_executor.py`、`app/cross_source_fact_plan.py`，必要时 `app/orchestrator.py` | 传递严格 / 消融模式；仅补充有结构化结果依据的检查信息；保留安全诊断类别 |
| 前端类型与呈现 | `web/src/api/ask.ts`、`web/src/components/AnswerBubble.vue` 及对应样式 | 对已有 `fact_review` 显示事实卡；字段缺失时保持当前回答布局 |
| 契约示例 | `docs/architecture/AskResponse_fact_review_响应示例_v0.4.json` | 固定一个严格完整、一个证据不足 / 未执行的契约示例 |
| 自动测试 | `app/test_contracts.py`、`tests/test_fact_review_projection.py`（新增）、`tests/test_fact_executor.py`、`tests/test_cross_source_fact_plan.py`、`tests/test_ask_progress.py`、相关前端检查 | 固化字段、状态来源、索引、流式一致性、客户端兼容与显示边界 |

不改全局 `refs` 去重键或语义；如测试发现必须改变引用身份规则，暂停该改动，另开影响评审。所有实现差异要先回填提案或本计划，再通知 A/B/C。

## 3. 分阶段执行顺序

### 阶段 0：开工门与兼容策略（未通过不得改契约）

1. 在 PR / 项目交流记录中登记 A、B、C 对 v0.3 的确认，并绑定提案文件版本 / 哈希。
2. 确认实施分支以哪个 `main` 提交为基线；保留 v0.1、v0.2、v0.3 提案和全部历史评测产物，不覆盖。
3. 兼容路径采用请求头协商：新消费者发送 `X-AskData-Fact-Review: 1`，旧请求无此头时两条 API 均必须省略 `fact_review` 键、保持原有响应键集；未知版本返回 400。前端新版本显式发送该头；旧后端忽略未知请求头时，前端保持普通回答布局。精确头名称由负责人确认后用于本分支实现，合并前仍作为 B/C 契约复核项。
4. 明确面向 UI 的 `reason_code` 白名单：仅区分业务材料不足、任务未运行、工具 / 配置失败等必要类别；不传异常文本、SQL、提示词、凭据。

**通过条件**：三方确认有记录；头部协商机制与未知版本错误行为有测试；范围仍局限于工单—合同事实计划；无未决接口语义。

### 阶段 1：建立隔离验证数据库

仓库已有 `infra/docker-compose.cross-source-demo.yml`，服务名 `askdata-cross-source-demo-db`、库 `cross_source_demo`、独立卷 `askdata_cross_source_demo_pgdata`，当前映射主机端口 `55532`。不得使用默认 Compose、Chinook 或 AdventureWorks 装载跨源演示表。

**首次启动前预检（PowerShell，项目根目录）：**

```powershell
docker compose -f infra/docker-compose.cross-source-demo.yml ps -a
docker ps -a --filter "name=askdata-cross-source-demo-db"
docker volume inspect askdata_cross_source_demo_pgdata
Get-NetTCPConnection -LocalPort 55532 -State Listen -ErrorAction SilentlyContinue
netsh interface ipv4 show excludedportrange protocol=tcp
```

- 预期是端口无监听、容器 / 卷状态已识别且确认属于此隔离 Compose。若容器或卷已有数据，先查明内容；不得 `down -v`、`volume rm` 或重灌覆盖。
- 若 55532 被占用 / 保留，暂停环境启动，先在实施分支审查 Compose 端口参数化并做配置测试；不得换连默认数据库。
- 空白新环境启动及载入命令（密码只存在当前 PowerShell 进程环境；不要写入仓库、日志或聊天）：

```powershell
$env:CROSS_SOURCE_DEMO_USER = 'cross_source_demo'
$env:CROSS_SOURCE_DEMO_PASSWORD = [guid]::NewGuid().ToString('N')
docker compose -f infra/docker-compose.cross-source-demo.yml up -d
docker compose -f infra/docker-compose.cross-source-demo.yml ps
$env:CROSS_SOURCE_DEMO_DSN = "postgresql://cross_source_demo:$($env:CROSS_SOURCE_DEMO_PASSWORD)@127.0.0.1:55532/cross_source_demo"
uv run python data/cross_source_demo/load_postgres.py
```

载入脚本只接受 `cross_source_demo` 数据库且要求五张表为空。若库已有表 / 行，不重跑载入；先用只读查询核对五表行数与 `data/cross_source_demo/structured/*.csv`，不符则停下调查。

**当前环境预检已完成（2026-10-14）**：隔离 Compose 容器 `askdata-cross-source-demo-db` 已健康运行，独立卷 `askdata_cross_source_demo_pgdata` 已存在，端口 55532 属于该容器。只读查询确认 `cross_source_demo` 中 5 张业务表行数与 CSV 完全一致（equipment 6、contracts 3、contract_equipment 5、work_orders 7、work_order_events 24）；另有向量 / 文档元数据表。未运行载入脚本、未重置容器 / 卷、未连接 Chinook 或 AdventureWorks。后续继续使用该隔离数据做开发回归。

**通过条件**：Compose 数据库健康；数据库名与 schema 确认为独立 `cross_source_demo`；五表行数与 CSV 对应；未接触 Chinook 的表、卷和指纹。数据库阶段证据记录 Compose 文件哈希、镜像摘要、端口、schema / 行数与日期，禁止记录 DSN / 密码。

### 阶段 2：实现后端投影与纯单元测试

按 v0.3 实现数据模型与独立投影函数，投影输入至少包括同一轮 `FactPlanRun`、其任务级 trace / `FactRecord`、显式 `verification_mode` 和已定序去重的最终 `refs`。投影不得访问模型、数据库、文件系统或读取答案文本。

必须先做并通过以下用例，再进入端到端：

1. `checks`：`*_checked=True` 单独存在时不生成通过项；`metric_checks[].within_limit` 的 true / false 分别映射通过 / 失败；映射依据只有在策略观察 `status=allowed` 时通过，`denied` / `undefined` 不通过；没有权威结果时 unknown / 省略。
2. 状态与执行解耦：`completed + insufficient` 合法；未运行、任务失败、正常完成且 supported 均分别覆盖；`FactPlanRun.skipped` 不能单独定状态；日志缺失或矛盾时不默认完成。
3. 诊断安全：允许的 `reason_code` 可保留；任意异常文本、SQL、答案长文本和秘密不得进入 JSON。`FactRecord.value` 不作为用户摘要透传。
4. 模式：严格、消融、提前停止无 verdict trace、未知 / 缺失模式均覆盖；只有显式 `strict` 可进入严格展示门。
5. 引用链接：最终 refs 排序 / 去重后重算 0-based 索引；完整、部分、零证据；越界；重复来源；SQL 缺主键；文档缺字符偏移；粗定位碰撞；多个账本来源折叠等都覆盖。不能一对一精确定位时必须降级，禁止模糊绑定。
6. 摘要白名单：只按任务类型从已审定字段生成 `summary`；verdict 的整段 `FactRecord.value` 和任意未识别结构均不得透传。

建议命令：

```powershell
uv run python app/test_contracts.py
uv run python -m pytest tests/test_fact_review_projection.py tests/test_evidence_ledger.py tests/test_fact_executor.py tests/test_cross_source_fact_plan.py -q
```

**通过条件**：新增投影测试全过；`AskResponse` 严格校验、JSON 序列化 / 反序列化和默认旧响应路径符合已选兼容机制。

### 阶段 3：数据库 / API 集成验证

仅使用阶段 1 的隔离库和 `data/cross_source_demo/cases.jsonl` 中已曝光样例作为**开发回归**，不把它们当新留出题或泛化证据。确认下面的事实链至少各有覆盖：正常支持、超时、合同 / 映射阻断、受控依据拒绝、事件缺失、事件倒序、检索后条款不确定。

PowerShell 中显式启用试验路径，不改变默认行为：

```powershell
$env:CROSS_SOURCE_DEMO_ENABLED = '1'
$env:CROSS_SOURCE_FACT_PLAN_ENABLED = '1'
$env:CROSS_SOURCE_FACT_PLAN_DSN = $env:CROSS_SOURCE_DEMO_DSN
$env:CROSS_SOURCE_FACT_PLAN_DB_SCHEMA = 'cross_source_demo'
$env:CROSS_SOURCE_FACT_PLAN_VALIDATOR_MODE = 'strict' # 单独用 'ablation' 再跑同一组回归
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动服务前先在项目根目录执行离线环境检查 `uv run python eval/preflight_rag_env.py`；若本地嵌入 / 重排模型未就绪，记录为环境阻断，不临时改用未经预注册的模型或服务。

另开 PowerShell 请求。非流式：

```powershell
$body = @{
  question = '工单 WO-2026-0001 是否在合同修复时限内完成？修复耗时是多少？'
  session_id = 'fact-review-acceptance'
} | ConvertTo-Json
$plain = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/ask' `
  -Method Post -ContentType 'application/json' -Body $body
$plain | ConvertTo-Json -Depth 20
```

随后以同一 `$body` 请求 `/api/ask/stream`，保留 `complete.response` 与阶段事件；比较两种接口的最终 `fact_review`，确认阶段事件没有提前声称事实已核验。严格与消融模式分别运行时，先停止服务、切换 `CROSS_SOURCE_FACT_PLAN_VALIDATOR_MODE`、重新启动，再用相同请求复测。使用其他样例问题前，先核对 `cases.jsonl` 中对应事实与证据，不用模型文本充当真值。

验证矩阵：

| 路径 | 验收 |
|---|---|
| `/api/ask` 严格模式 | `fact_review` 字段、事实状态、检查来源、任务状态、证据索引有效且与最终 refs 对应 |
| `/api/ask/stream` 严格模式 | `complete.response.fact_review` 与非流式同请求一致；阶段事件只报告真实执行阶段 |
| 消融模式 | `verification_mode=ablation`；不能显示严格核验通过，即使部分指标或引用齐全 |
| 映射 / 事件早停 | 模式仍明确；未执行任务显示 not_run，不要求伪造后续查询或文档证据 |
| 查询 / 文档故障 | completed/failed 语义按任务终态和结构化诊断分别呈现；不能把失败伪装成“已核验但资料不足” |
| 普通 SQL / RAG / 澄清 | 按协商机制保持旧形状 / 不出现空事实卡，不改变原答案和 refs |

**通过条件**：上述路径都没有伪通过、错绑引用、模式猜测或流式 / 非流式分歧；数据库查询仍受只读执行约束。发生失败先保留原始脱敏响应、trace 与日志，不改题包或判据来让测试变绿。

### 阶段 4：前端事实卡与兼容检查

只在响应含有效 `fact_review` 时显示事实卡；旧后端 / 未协商响应继续原样显示“回答—来源依据—处理详情”。事实卡不能替代答案，不能声称句子级核验。

人工检查桌面和手机窄屏：

- 标签清楚区分事实状态、任务执行状态、严格 / 消融模式、证据是否完整；
- 点击 `evidence_ref_indices` 跳到同响应 `refs` 的对应来源；
- `partial` / `unavailable`、`not_run` / `failed` 不用绿色“已核验通过”；
- 缺字段、未知枚举和旧响应均无空白卡、报错或误导性状态；
- 尊重减少动态效果设置，动效不伪装实时进度。

运行现有前端检查：

```powershell
cd web
npm run build
npm run test:ui
npm run test:process
npm run test:motion
```

旧客户端门槛要明确区分：本仓旧 UI / 冻结响应消费测试通过，只能证明本仓已知消费端；若没有外部客户端样本，报告写“外部兼容性未验证”，不可写“完全向后兼容”。

### 阶段 5：完整回归、独立复核与交付

```powershell
uv run python app/test_contracts.py
uv run python -m pytest -q
git diff --check
```

由未编写投影函数的人按 §4 测试矩阵独立抽查真实 API 响应：至少各抽一条严格通过、证据不完整、早停、消融、查询 / 检索失败。复核内容只对照运行配置、结构化 trace、最终 refs 与展示结果，不读取 Gold 私密材料（本轮不运行正式 A/B）。

交付记录需包含：基线与最终 commit、变更文件、契约版本、三方同意记录、数据库隔离信息（不含凭据）、运行命令 / 退出码、分项测试结果、桌面 / 手机截图、外部严格客户端未知边界、剩余问题。计划阶段的开发样例回归只用于验证功能链，不能表述成泛化或效果提升。

**发布门槛**：所有单测、API / 流式一致性、前端构建与受控界面检查通过；引用非唯一时 fail-closed；兼容策略有记录；Chinook 基准未变；独立复核无阻断。任一门未通过，功能保持显式试验 / 不发布状态。

## 4. 测试命令速查

| 范围 | 命令 | 阶段 |
|---|---|---|
| 共享契约自检 | `uv run python app/test_contracts.py` | 2、5 |
| 事实投影与底层账本 | `uv run python -m pytest tests/test_fact_review_projection.py tests/test_evidence_ledger.py tests/test_fact_executor.py tests/test_cross_source_fact_plan.py -q` | 2 |
| API 流式协议 | `uv run python -m pytest tests/test_ask_progress.py -q` | 3 |
| 跨源路由 / 策略 / 故障 | `uv run python -m pytest tests/test_cross_source_routing.py tests/test_cross_source_policy.py tests/test_cross_source_demo_failure_paths.py -q` | 3 |
| 前端构建与交互 | `npm run build`、`npm run test:ui`、`npm run test:process`、`npm run test:motion` | 4 |
| 全仓库回归 | `uv run python -m pytest -q`、`git diff --check` | 5 |

全量测试必须在隔离环境中执行；如失败，不以排除失败用例、覆盖基线、改历史评测题或放宽引用规则作为通过方式。

## 5. 执行中止条件

出现下列任一情况即停止对应阶段并记录，不自行绕过：

1. B / C 对最终字段语义有新异议，或实现必须超出 v0.3 范围；
2. 端口 / 容器 / 卷归属不清，或隔离 DSN 指向 Chinook / AdventureWorks；
3. 测试发现仅凭现有 trace 无法分辨真实检查结果、终态或引用唯一性；
4. 旧严格客户端兼容策略未定或兼容测试失败；
5. 任一响应会显示没有后端依据的“通过 / 已核验”，或将部分证据错绑为完整证据。

处理方式：保留现场、附失败用例和最小复现，回到契约 / 设计讨论；不得通过更改 Gold、题目或人工标签消除阻断。
