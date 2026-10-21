# acceptance_20261011 —— 单表问数候选题（20 道）

对应《组员测试执行单：C（数据库、问数与中级任务 1/3/4/5）》10-11 的交付项②：
在 Chinook 库上编制 **20 道单表 NL2SQL 候选题**，供验收/演示题库扩选用。
规模为**团队内部建议 20 道**；赛题要求至少 10 个单表用例。

## 三文件分离（防答案泄漏）

| 文件 | 内容 | 能否进 prompt |
|---|---|---|
| `nl2sql_questions.jsonl` | **只含 `id` / `task` / `question`** | ✅ 可以 |
| `nl2sql_reference.jsonl` | 标准 SQL + **真实执行得到的参考结果** + 判分说明 | ❌ 禁止 |
| `nl2sql_sources.md` | 题目来源（表、列、schema 依据、覆盖点、编题与核验人） | ✅ 可以（不含答案） |

> 约定：`nl2sql_questions.jsonl` 里**只允许** `id` / `task` / `question` 三个字段，
> 不得出现 `gold_sql` / `notes` / `gold_result` / `expected_columns` 等任何答案侧信息。
> 生成脚本与独立校验脚本都内置了该检查，命中即失败。
> `nl2sql_reference.jsonl` 整体属判分侧，采集阶段不得提供。

`task` 值登记为 **1**（「任务 1（要素识别与改写）/ 单表问数候选题」轮次）；
采集器 / 接口只使用 `question`，`task` 仅作来源登记，不进请求体。

## 规模与覆盖

- 题量：**20**；难度分布 **easy 7 / medium 7 / hard 6**。
- 全部单表查询（对应评分表「基础 NL2SQL 单表」统计底线）。
- 覆盖表：`artist` `media_type` `track` `playlist` `customer` `invoice` `invoice_line` `employee`（Chinook 8 张业务表）。
- 覆盖题型：清晰过滤、计数 / 求和、分组、排序 / 前 N、空结果、相似字段干扰、NULL 处理、单位换算、占比、子查询。
- 与既有 `eval/cases/nl2sql.jsonl` 的 8 道单表题**无重复**（表/题型均错开）。

## 参考结果怎么来的

`nl2sql_reference.jsonl` 的 `gold_result` **不是手写的**，而是 `build_acceptance_20261011.py`
连真实 Chinook 库执行 `gold_sql` 后落盘的值。复现与自校：

```bash
# 前置：Chinook 容器 healthy（127.0.0.1:5432），根目录 .env 有 DATABASE_URL
.venv/Scripts/python.exe eval/cases/acceptance_20261011/build_acceptance_20261011.py    # 生成
.venv/Scripts/python.exe eval/cases/acceptance_20261011/verify_acceptance_20261011.py   # 独立校验（重跑 gold_sql 比对）
```

脚本会打印每题结果规模，并做自校（id 唯一、题目文件无越权字段、难度三档覆盖）。

## 怎么喂给被测系统

`nl2sql_questions.jsonl` 每行取 `question` 字段逐条 POST 到 `/api/ask` 即可
（`session_id` 必填，每条用独立会话 ID）：

```bash
curl -s http://127.0.0.1:8000/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"平台上一共收录了多少位艺术家？","session_id":"C-acc-01"}'
```

> 注意：本集是**单表问数**题，不走 T3/T4/T5 的 `eval/mainline/driver.py`
> （driver 的问题文件只接受 `id`/`task`/`turns`/`question`，且 `task` 仅限 3/4/5）。
> 如需会话级采集，请另建 driver 兼容题包，不要把本集字段直接塞给 driver。

## 未接入 CI 的说明

本目录**未**登记进 `eval/tools/validate_cases.py` 的 `CASE_FILES`：
它是独立验收候选集，非主评测套件，避免改变既有 CI 基线。
若后续要并入主套件，需同时改 `CASE_FILES` 与 `REQUIRED`（`test_validate_cases.py` 有断言）。
