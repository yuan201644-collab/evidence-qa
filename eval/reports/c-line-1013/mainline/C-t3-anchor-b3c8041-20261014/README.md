# T3 开发回归 —— 在确认代码版本 main@b3c8041 上带固定锚点的正式重跑

- 日期：2026-10-14
- 范围：**已曝光开发回归**，非未见题/泛化证据，非正式比赛成绩
- **代码版本**：`b3c804133ed463f1b933e499c8dff2ae2f24bfc5`（= 当时 `main` HEAD；独立 `git worktree`，`git_dirty=False`）
- 与旧运行的关系：本目录是**新运行**，与 `C-t3-anchor-20261014/`（基于 `d03c3d9` 的同锚点运行）**分开留档**，互不覆盖。

## 收集结果
- run_id：`20261014T132355Z-1a1d4f9c`
- capture_status：`completed`；case_count 20；recorded_turns 43/43
- 采集进程 env：APP_TIME_ANCHOR = `2024-06-15`，LLM_MODEL = `glm-4.5-flash`
- 数据清单：`t3_run3_data_manifest.rev1.json`（14 表 / 126,607 行 / `0fe6c67e…`）
- 服务运行清单：`t3_anchor_runtime_manifest.json`

## 只读重放审计（`audit_t3_runtime_replay.py`，不读判据）
- ok：`True`；http_200 43/43；成功执行 SQL 22；只读重放通过 22
- failures：[]

## 锚点实证
- 见 `t3_anchor_check.md`：去年→2023、前年→2022；与显式期间用例数值一致（-2.465469%）。

## 未做 / 边界
- 本目录不含 rubric 评分器；T3 判据为人工核对清单（`t3_v02_rubric.jsonl`），未做机器打分。
- 模型为本地可用档 `glm-4.5-flash`（与 10-13 实际运行同档），非正式通道。
