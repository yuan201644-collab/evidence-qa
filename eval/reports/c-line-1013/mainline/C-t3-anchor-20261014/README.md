# T3 开发回归 —— 固定时间锚点（APP_TIME_ANCHOR=2024-06-15）重跑

- 日期：2026-10-14
- 范围：**已曝光开发回归**，非未见题/泛化证据，非正式比赛成绩
- 基线：`d03c3d9`（独立 `git worktree`，HEAD = `d03c3d900efd13875107108eb642c44450fb2d6e`）
- 与旧运行的关系：本目录是**新运行**，与未设锚点的旧 T3 运行**分开留档**；旧运行不得用于证明相关用例通过。

## 收集结果
- run_id：`20261014T111816Z-bf2114e2`
- capture_status：`completed`；case_count 20；recorded_turns 43/43
- 采集进程 env 中：APP_TIME_ANCHOR = `2024-06-15`，LLM_MODEL = `glm-4.5-flash`
- 数据清单：`t3_run3_data_manifest.rev1.json`（14 表 / 126,607 行 / `0fe6c67e…`）
- 服务运行清单：`t3_anchor_runtime_manifest.json`（如实登记本次实际配置）

## 只读重放审计（`audit_t3_runtime_replay.py`，不读判据）
- ok：`True`；http_200 43/43；成功执行 SQL 20；只读重放通过 20
- failures：[]

## 未做 / 边界
- 本脚本不含 rubric 评分器；T3 判据为人工核对清单（`t3_v02_rubric.jsonl`），未做机器打分。
- 本次模型为本地可用档 `glm-4.5-flash`（与 10-13 实际运行同档），非正式通道。
