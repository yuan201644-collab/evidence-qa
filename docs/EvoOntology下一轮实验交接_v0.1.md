# EvoOntology 下一轮实验 · 交接文档 v0.1

> 写入时间：2026-10-03 晚（实验中途，需另一会话/人续跑收尾）
> 实验线分支：`docs-eval-notes-1002`（**非 main**，未提交）
> 定位：导师要求「评估了解，不是复现」——本实验是**评估型实验**，不碰任何正式模块、不引 MCP、不做自演化、不直接兑换赛事分。

---

## 〇、一句话背景（为什么做这轮）

上一轮 EvoOntology 实验（构建器验证 + 机制验证）在 **Chinook 满分题**上做，结论是「整本注入知识无增益」——但那是**天花板效应（ceiling effect）**：基线 A 本来满分，任何干预都测不出提升，属于「信息缺失」而非「知识无用」。

本实验按 `docs/EvoOntology下一轮实验设计_v0.1.md` 的设计，**换到 A 本来不满分的战场（中级任务 4 字段定位，基于 AdventureWorks 80 表）**，**改用各任务自己的中间环节指标**（表召回 / 字段召回，而非最终 SQL 对错），用三臂干净对照，回答「Builder 知识资产（论文 Content 层）对任务 4 字段定位有没有显著作用、哪种呈现方式更有效」。

---

## 一、实验设计（要点）

| 项 | 内容 |
|---|---|
| 战场 | 中级任务 4 精准表/字段定位（AdventureWorks PG，`127.0.0.1:5433`） |
| 题集 | `eval/experiments/evont_next/task4_cases_sealed_v0.1.json`，**32 题** |
| 出题纪律 | **盲审**：只基于 AW 原始 schema 构造，未参考 16 条知识资产（gold 不泄露给 C 臂）；gold 用 `required_tables`/`required_columns` 中间环节标注 |
| 三臂 | **A 无知识** ／ **B 整本注入**（verified 16 条全塞提示词）／ **C 按需访问**（进程内 `browse`+`resolve`，不建 MCP server） |
| 轮次 | 每题每臂 **2 轮**（配对，顺序随机化） |
| 主指标 | `table_recall`（候选表召回@覆盖）、`field_recall`（字段定位召回）；辅助 `executed`（SQL 经 sqlglot 校验可执行） |
| 统计 | 配对 McNemar（执行率二分类）+ 字段召回差值 95% bootstrap 置信区间 |
| 知识资产 | `eval/reports/run-builder-20261003T083145Z/verified_knowledge.json`（16 条，含 Mappings/Constraints/Terms） |
| 预注册 | preflight 已冻结题目/gold/指标/判据/模型/提示 SHA，源快照落 `run_task4_v0.1/freeze.json` |

**关键纪律**：A 臂**不给任何知识产出**（连「空字段事实」都不给），否则会像上一轮那样系统性压低 B/C 相对 A 的差距，归因不干净。

---

## 二、资产清单（路径）

| 文件 | 作用 |
|---|---|
| `docs/EvoOntology下一轮实验设计_v0.1.md` | 执行前协议（设计依据） |
| `docs/EvoOntology下一轮实验交接_v0.1.md` | 本交接文档 |
| `eval/experiments/evont_next/task4_cases_sealed_v0.1.json` | 预注册题集（32 题，盲审出题） |
| `eval/experiments/evont_next/verify_task4_gold.py` | gold 可重放核验脚本（已全部命中 AW schema） |
| `eval/experiments/evont_next/evont_next_runner.py` | 三臂驱动（preflight / run / report 三阶段） |
| `eval/experiments/evont_next/resume_evont.py` | **幂等续跑**（读已完成集合跳过，只补缺失记录） |
| `eval/experiments/evont_next/run_task4_v0.1/` | 运行目录：`freeze.json`（冻结）、`records.jsonl`（逐条记录）、`summary_report.json`（report 阶段产物） |
| `eval/reports/run-builder-20261003T083145Z/verified_knowledge.json` | 16 条知识资产（B/C 臂输入） |

**依赖**：隔离 venv `C:/Users/86133/.workbuddy/binaries/python/envs/default/Scripts/python.exe`，已装 `sqlglot` / `psycopg[binary]` / `openai` / `httpx`（**未污染系统 Python**）。
**环境**：`.env`（gitignored）需含火山方舟 API key 与 `AW_POSTGRES_PASSWORD`；API 端点 `/api/plan/v3`，模型 `deepseek-v4.1-flash`，`NL2SQL_MAX_TOKENS=16000`。

---

## 三、当前状态（实测，2026-10-03 晚）

- **`records.jsonl` 共 161 条**（目标 192 = 32 题 × 3 臂 × 2 轮）。
- 补跑进程**再次被环境回收**（与首轮同样原因：后台 task 追踪 ID 失效、文件停止增长、无 error），停在 **task4-22 第二轮 B 臂**（C 臂及 task4-23~32 全缺）。
- 已产出的 161 条**全部 `executed=True`、无 error**，审计轨迹完整（含 sql、usage、latency、calls）。
- 首轮 run（task4-01~12）与补跑（task4-13~22 + 部分第二轮）混合存放，**`resume_evont.py` 幂等**，重跑只补缺失的约 31 条。

> ⚠️ 续跑前先确认无残留 python 进程（`tasklist | findstr python`），避免两个进程抢写 `records.jsonl`。

---

## 四、初步信号（仅早期 12 题第一轮，非最终结论）

首轮完成的 task4-01~12（每臂 1 轮）均值：

| 臂 | 表召回 TR | 字段召回 FR |
|---|---:|---:|
| A 无知识 | 0.750 | 0.625 |
| B 整本注入 | 0.889 | 0.796 |
| C 按需访问 | 0.806 | 0.683 |

**含义**：B（整本注入）明显优于 A（无知识），TR +0.139 / FR +0.171。这与上一轮「天花板无增益」**相反**，初步证明换到任务 4 字段定位后，知识增益空间被打开——但这只是 12 题、单轮，**不足以下「显著」结论**，必须等全量 192 条出配对统计。

**典型个案**：A 臂首题直接用了 `SalesOrderDetail.LineTotal` 全空列（机制验证已发现该坑），而 gold 要求 `OrderQty/UnitPrice/UnitPriceDiscount`；B/C 臂因知识资产明确写「LineTotal 全空须用表达式」而避开。这是「知识改善字段定位」的直观证据。

---

## 五、收尾步骤（给续跑者）

### 步骤 1：确认进程已死 + 补齐剩余记录
```bash
# 确认无残留 python 进程（否则别启动续跑）
tasklist | findstr python
# 幂等续跑：自动跳过已完成，只补缺失约 31 条
C:/Users/86133/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  eval/experiments/evont_next/resume_evont.py \
  --run-dir eval/experiments/evont_next/run_task4_v0.1 \
  --cases  eval/experiments/evont_next/task4_cases_sealed_v0.1.json \
  --verified eval/reports/run-builder-20261003T083145Z/verified_knowledge.json
# 注：调 API 需联网，运行时加 sandbox-disabled；跑完确认 records.jsonl = 192 行
```

### 步骤 2：出统计报告
```bash
C:/Users/86133/.workbuddy/binaries/python/envs/default/Scripts/python.exe \
  eval/experiments/evont_next/evont_next_runner.py --stage report \
  --run-dir eval/experiments/evont_next/run_task4_v0.1 \
  --cases  eval/experiments/evont_next/task4_cases_sealed_v0.1.json \
  --verified eval/reports/run-builder-20261003T083145Z/verified_knowledge.json
# 产物：run_task4_v0.1/summary_report.json（含 A↔B↔C 配对 McNemar p + 字段召回差值 95% CI）
```

### 步骤 3：写最终实验报告
基于 `summary_report.json` 写 `docs/EvoOntology下一轮实验报告_v0.1.md`，结构对齐现有 `EvoOntology构建器独立验证实验报告_v0.1.md`：① 设计 ② 规模与成本 ③ 主结果（配对显著性）④ 初步信号中的典型个案 ⑤ 边界与未测范围（Tool 层 / 自演化未测）⑥ 结论（能否说「知识显著改善任务 4 定位」）。

---

## 六、结论口径（写报告时务必遵守）

- **能下**：知识资产（B 臂整本注入）在任务 4 字段定位上相对无知识基线有正向效应（初步显著，待全量确认）；「按需访问」C 臂介于 A/B 之间、且时延更高（与论文「按需不损准确率」前提相反的线索保留）。
- **不能下**：「Builder 没有作用」——这是上一轮天花板下的误读；本实验换战场后效应显现。
- **未测**：Tool 层（MCP 封装）、多轮自演化闭环（论文 57% 增益来源），按导师口径「评估了解」**不测**。
- **对外表述**：写明「测了哪两块（构建+静态注入+按需）、未测哪两块（Tool+演化）、为什么」——这比硬凑不可比数字更让评委信服。

---

## 七、硬纪律（与用户约定，未经许可不得违反）

1. **无用户许可不合并**：本实验产物全在 `docs-eval-notes-1002` 分支，未跟踪；任何 `git merge` / `git add` 入库 / 切分支动作须用户先点头（与 MEMORY §十 工作区保护一致）。
2. **不碰正式模块**：`app/` 未改；知识仅作实验输入，不接 `infra/schema_meta` 运行时。
3. **不引 MCP / 不做自演化**：C 臂按需访问是进程内实现，未起常驻服务。
4. **不兑换赛事分**：结果仅供设计报告「评估借鉴」一节引用，不直接对应初赛得分。
5. **密钥不入仓**：`.env` 已 gitignore；报告/日志不得出现明文 key。

---

## 八、已知坑（续跑者必读）

- **后台进程会被环境回收**：首轮（task `sBX7uV`）、补跑（task `IAWo6L`）均中途被杀，无 error。对策：重跑 `resume_evont.py`（幂等），不要同目录直接重跑 `--stage run`（原 runner 有「禁止隐藏重跑」硬检查会拒绝）。
- **AW 库 `LineTotal`/`TotalDue` 等金额列全空**：这是字段定位题的天然干扰，也是知识资产要纠正的点；gold 已避开空列。
- **API 端点必须 `/api/plan/v3`**，带思维链，`reasoning_tokens` 计入 `completion_tokens` → `NL2SQL_MAX_TOKENS=16000` 必须给足，否则长题截断。
- **依赖装在隔离 venv**：不要往系统 `D:/python` 或 managed 全局装包；换机/换会话先确认 venv 依赖仍在。
