# 跨源留出集 v0.1（自 2026-10-01 起作为回归集）

状态更新：2026-09-30 已完成旧输出的独立人工盲审。针对 H03/H05 修正后，这七题已用于回归，不能再称为未见题效果。原数据/问题/gold 不变，新增 regression_gold_v0.2.jsonl 加强负例和数值检查。最新执行见 `docs/architecture/阶段C验证与定向修复记录_v0.2.md`；以下保留原运行设计。

本目录数据与 `data/cross_source_demo/` 的 7 道开发诊断题完全分离。编号、问题、记录和合同均为新建合成样例；本版本用于检验已实现的工单—合同最小链路，不代表真实业务数据，也不等同于赛题中级任务四验收。

- `data/`：只供隔离数据库导入的记录与合同文档。
- `questions.jsonl`：在线评测器可读的问题，不包含标准答案。
- `gold.jsonl`：逐题预期最终状态和证据对象。运行时不传给 `/api/ask`。
- `data/`：独立 PostgreSQL 初始化脚本、CSV 与合同文档；不要载入开发演示数据库。

题集需在正式计分前由未参与当前工具调试的队员复核并冻结。该目录已有题目一旦用于修复程序，就应降级为诊断集，并另建新的留出集。

当前含 7 题，覆盖有效达标、超时、同名无映射、过期合同、缺少修复事件、事件倒序、多适用合同歧义。由于当前数据库只允许同一设备有多条不同合同映射，歧义题检查“有多个有效候选时不下结论”。暂不覆盖首轮 BM25 漏召回后的补查挑战；该机制应另行构造真实检索难例并保存每次检索命中。2026-09-29 已完成一次修正后的在线 A/B 探索性试运行，逐题结果见 `eval/reports/cross_source/cross-source-paired-20260929T105846Z.json`；报告状态仍为未复核，不能作为验收成绩。去组别评审稿见 `eval/reports/cross_source/cross-source-blind-review-20260929T105914Z.md`，不要向评审者发送单独保存的映射密钥。

## 隔离运行

PowerShell 在项目根目录执行。留出数据库使用独立容器、数据卷和 `127.0.0.1:55632`，与开发演示库的 `55532` 端口不同；导入脚本仍会拒绝非空表。

```powershell
$env:CROSS_SOURCE_DEMO_PASSWORD = '设置本机随机密码'
docker compose -f infra/docker-compose.cross-source-eval.yml up -d
docker compose -f infra/docker-compose.cross-source-eval.yml ps
$env:CROSS_SOURCE_DEMO_DSN = "postgresql://cross_source_demo:$($env:CROSS_SOURCE_DEMO_PASSWORD)@127.0.0.1:55632/cross_source_demo"
uv run python data/cross_source_demo/load_postgres.py --data-dir eval/cross_source/heldout/data
$env:CROSS_SOURCE_DEMO_DOCS_DIR = (Resolve-Path eval/cross_source/heldout/data/documents).Path
$env:CROSS_SOURCE_DEMO_ENABLED = '1'
uv run uvicorn app.main:app --reload
```

另开 PowerShell 窗口，在项目根目录运行：

```powershell
$env:CROSS_SOURCE_DEMO_PASSWORD = '与上面相同的本机随机密码'
$env:CROSS_SOURCE_DEMO_DSN = "postgresql://cross_source_demo:$($env:CROSS_SOURCE_DEMO_PASSWORD)@127.0.0.1:55632/cross_source_demo"
$env:CROSS_SOURCE_DEMO_DOCS_DIR = (Resolve-Path eval/cross_source/heldout/data/documents).Path
uv run python eval/cross_source_pair_runner.py --endpoint http://127.0.0.1:8000
```

结果写入 `eval/reports/cross_source/`。A 组是按阶段 B 记录复原的参考算法，不是历史源码；复原规则见 `docs/architecture/阶段B参考基线复原假设_v0.1.md`，首次正式运行前应先由队员审阅。报告中的自动检查不是语义验收；仍须盲审回答是否正确、引用是否充分，并保留失败题。隔离实验后停止和删除该评测容器/卷前，应先确认评测结果已保存；不要操作开发演示库。

2026-10-01 新增不需要手动传密码、避开固定端口的一键入口 `eval/cross_source_verify.ps1`，完整命令见阶段 C v0.2 记录；只创建自身临时数据库，不复用上述持久卷。手动使用评测 compose 时可设置 CROSS_SOURCE_EVAL_PORT（例如先检查空闲/保留范围再选 15432），并同步 DSN 端口；不设置仍为 55632。
