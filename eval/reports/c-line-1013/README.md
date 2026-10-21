# C 线运行产物归档（2026-10-11 ~ 2026-10-13）

> 本目录是**入库的归档副本**，用途只有一个：让 A / B `git clone` 之后能直接拿到 C 线的原始响应与复核报告。
> 归档时间 2026-10-13，基线 `main` @ `9d274aa`。

## 为什么需要这份副本

C 线的运行产物原本落在两处，**两处都被 `.gitignore` 整目录忽略**：

| 原路径 | 忽略依据 |
|---|---|
| `eval/reports/mainline/` | `.gitignore:173`（"实验中间产物与过程记录"，2026-10-12 定，理由是实测 236 MB / 5000+ 文件） |
| `agent_temp/` | `.gitignore:14`（协作规范第 6 条：中间文件统一放 `agent_temp`，不进仓库） |

后果是：报告里写的"证据位置"全是**本机路径**，A 和 B 在自己机器上根本打不开。这条证据链实际上是断的。

本次归档按体积实测判断：这批产物**合计 1.96 MB / 44 个文件**，与触发忽略规则的 236 MB 量级相差两个数量级，**不构成体积问题**。因此原样复制到本目录入库，**不修改 `.gitignore`**（根 `.gitignore` 属团队文件）。

⚠️ 原路径保留不动 —— 下游脚本与历史报告仍按原路径引用。本目录是快照，不是迁移。

## 目录结构

```
c-line-1013/
├── README.md                      ← 本文件
├── mainline/                      ← 原样复制自 eval/reports/mainline/
│   ├── C_复核_20261013_重跑T345.md      复核报告（重跑版，10-13 主力结论）
│   ├── C_复核_20261013.md               复核报告（第一版：规模差异 + 全仓失败分类）
│   ├── C_回复A_结构化规模灌库_20261012.md
│   ├── C-review-1013/                   10-13 复跑采集（节流版与首轮对照）
│   │   ├── t3-throttled/ t4-throttled/ t5-throttled/     ← 结论依据批次
│   │   └── t3/ t5/                                       ← 无节流首轮（全 429，对照用）
│   └── acceptance_20261011/             10-11 ~ 10-12 采集
│       ├── README.md                    运行总览（索引）
│       ├── C_汇报_20261012.md           会前稿旧版（数字已作废，仅存档）
│       ├── C_汇报_20261013.md           会前稿 10-13 版
│       ├── C-t3-v02/                    T3 采集 + freeze + RUBRIC_CHECK.md（逐题核对）
│       ├── C-t4-v03/ C-t4-v03b1..b4/ T4 原 run 与四批拆批重跑
│       └── C-t5-v02b1..b4/              T5 四批拆批采集
└── acceptance_run_20261011/       ← 原样复制自 agent_temp/acceptance_run_20261011/
    ├── raw_answers.jsonl                单表 20 题真实接口响应
    ├── summary.json                     逐题判定（PASS 17 / CLARIFY 2 / FAIL 1）
    └── 报告.md                          逐题比对表
```

## 报告与其原始响应的对应关系

复核某份报告的结论时，取下列对应的 `raw_answers.jsonl` + `freeze.json`：

| 报告 | 依据的原始响应 | 时间 |
|---|---|---|
| `mainline/C_复核_20261013_重跑T345.md` | `mainline/C-review-1013/{t3,t4,t5}-throttled/` | 10-13 |
| `mainline/acceptance_20261011/C_汇报_20261013.md` | 同上 + `acceptance_run_20261011/` | 10-13 |
| `mainline/acceptance_20261011/C_汇报_20261012.md`（旧版） | `mainline/acceptance_20261011/C-t3-v02/`、`C-t4-v03*/`、`C-t5-v02b*/` | 10-11~10-12 |
| `mainline/acceptance_20261011/C-t3-v02/RUBRIC_CHECK.md` | `mainline/acceptance_20261011/C-t3-v02/raw_answers.jsonl` | 10-11~10-12 |
| `acceptance_run_20261011/报告.md` | `acceptance_run_20261011/raw_answers.jsonl` | 10-11 |

**批次说明**：`t3/`、`t5/`（无 `-throttled` 后缀）是**无节流首轮**，LLM 持续 429、基本无有效 SQL，仅作"限流影响"的对照证据；`-throttled` 才是结论所依据的批次。`C-t4-v03b1..b4/`、`C-t5-v02b1..b4/` 是拆批重跑的过程尝试，**不是**报告数字的来源。

## 两处更正（原文有误，以此为准）

1. **PR base**：`mainline/acceptance_20261011/README.md` 第 66 行写"base 建议 `mainline-candidate-1010`" —— 已过时。`feat-acceptance-1011` 与 `feat-scale-db-seed` 均已合入 `main`（PR #58、#59），题集文件也已按 PR #58 改为 `nl2sql_questions.jsonl` / `nl2sql_reference.jsonl` / `nl2sql_sources.md`。
2. **产物位置**：同一份 README 第 82 行指"单表候选题真实回归另存于 `agent_temp/acceptance_run_20261011/`" —— 该指路对他人不可达，已在本目录提供入库副本 `acceptance_run_20261011/`。

## 口径声明

- 本目录全部产物为**开发回归**结果，**不是盲测 / 准确率 / 泛化证据**。
- `freeze.json` 由采集器生成，记录运行时的 commit、题集 sha256、数据清单 sha256 与 `answer_key_read_by_driver`（恒为 `false`）。其 `base_url_origin` 为本机 `http://127.0.0.1:<port>`。
- 本目录**不含**任何 API key、口令或凭据（入库前已按 `sk-` / `gho_` / `api_key=` / `Authorization` / `Bearer` 模式全量扫描，零命中）。
- 采集所用模型：本机为 `glm-4.5-flash`（备用档）；A 侧 runtime manifest 记录正式模型为 `deepseek-v4.1-flash`。
- **换行符**：按仓库级策略（`.gitattributes` 的 `* text=auto eol=lf`，入库统一为 LF），本目录文本文件在库内为 LF、工作区为 CRLF。`.jsonl` 是逐行 JSON，行尾不影响解析；`freeze.json` 中也没有记录 `raw_answers.jsonl` 的哈希，故不产生校验失配。（`data/corpus/raw/**` 是仓库里唯一用 `-text` 保留原始字节的特例，本目录不适用。）
