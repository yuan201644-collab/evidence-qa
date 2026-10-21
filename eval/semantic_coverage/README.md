# S2-H 语义覆盖采集与评分（eval/semantic_coverage/）

**建立日期**：2026-10-16　**建立人**：A（袁润熙）
**上游**：S2 计划书 `docs/architecture/S2-SQL条件核对完整实施计划_v0.1_20261015.md` §10（`:763-801`）、母方案 `:563-603`
**前置切片**：C5（闸门接线）——driver 采集的响应里应能看到 `sql_requirement_validator` 步骤。

## 目录

| 文件 | 作用 |
|---|---|
| `development_v0.1/questions.jsonl` | **题面**（19 个 SC 场景）。只含 id/dataset/turns，**绝不携带判据**（driver 会拒绝带 rubric 字段的题面） |
| `development_v0.1/rubric.jsonl` | **判据**，与题面分开保存（母方案 `:470`）。scorer 独立读取 |
| `development_v0.1/collection_config.json` | 采集配置（超时、会话前缀、数据集口径） |
| `driver.py` | 采集：真实请求落盘 `raw_responses.jsonl`，**不打分** |
| `scorer.py` | 打分：独立读 rubric + 原始响应，产出 `scores.json` |
| `export_predictions.py` | 导出为 `eval/runner.py` 的 JsonlPredictor 输入 |

## 场景归属（计划书 §11）

本批题面 = **S2 主责 14 项（SC-05~09/11/13~19）+ S1 已覆盖负控制（SC-01~04/30）= 19 项**。
SC-10 在 S1 已覆盖且本轮归 S1；SC-12 保留；SC-20~23/28/29/31/32 属 S3，未纳入。

## 判分口径（计划书 §12.1，P1 裁定 (c) 后）

三态并列，`unknown` 单列计数：

- `pass`：判据全部满足；
- `repair`：判据明确违反（该澄清没澄清、条件被丢、**静默遗漏**）；
- `unknown`：采集失败/超时/响应缺字段。**不算拦截成功也不算静默遗漏**；保留分母。

**静默遗漏判据**：答案给出完整业务结果（如无条件总数 59）且无任何「时间未落实」类说明 → repair。
「有数字但也有如实说明」→ 不算遗漏（记 notes）。

## 用法

```powershell
# 0. 起后端（闸门档位由服务进程 .env 决定；driver 不冒称）
# 1. 采集
$env:VERIMIND_EVAL_USERNAME='admin'; $env:VERIMIND_EVAL_PASSWORD=<已移除：请按 .env.example 自行配置>
python -m eval.semantic_coverage.driver --base http://127.0.0.1:8000 `
    --out-dir eval/reports/semantic_coverage/semcov-20261016-run1
# 2. 打分
python -m eval.semantic_coverage.scorer --run-dir eval/reports/semantic_coverage/semcov-20261016-run1
# 3. （可选）导出给 runner.py 跑三项指标
python -m eval.semantic_coverage.export_predictions `
    --run-dir eval/reports/semantic_coverage/semcov-20261016-run1 --out agent_temp/predictions.jsonl
python -m eval.runner.py --predictor jsonl --suite all --dialect postgres --predictions agent_temp/predictions.jsonl
```

## 硬边界（违反即失效）

1. driver **只读题面**；题面混入 rubric 字段会被拒绝（已验证）；
2. 输出目录已有结果**拒绝覆盖**，复跑另建运行号（已验证）；
3. 凭据走环境变量，不进命令行/日志/记录；
4. 超时登记未完成，**不自动重放**；
5. export **不读 gold**、**不把 answer_refs 复制成 retrieved**（否则 runner 的 `retrieval_is_approximated` 会把近似召回伪装成真召回）；
6. 未运行项写 `{"error":"未运行：<原因>"}`，不填历史分数（已验证 JsonlPredictor 可加载）。

## 离线验证记录（2026-10-16，无后端）

- 三脚本 `ast.parse` 通过；questions/rubric 各 19 条、id 对齐；
- driver：带判据题面被拒 / 已有结果目录被拒覆盖；
- scorer：合成三类记录（正确澄清 SC-02 / 静默遗漏 SC-05 / 正常 SC-01）→ 判定全部符合预期，静默遗漏数=1 被正确点名；
- export：伪造采集（含超时项）→ `answer_refs` 不串 `retrieved`、未运行项如实标注、JsonlPredictor 回环加载成功。

**尚未做**：真实后端采集（需先起 8000 服务）。C8 验证记录里将注明。
