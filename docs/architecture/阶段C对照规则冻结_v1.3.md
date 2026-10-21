# 阶段 C 跨源候选实现与评测快照 v1.3

冻结日期：2026-10-03。承接 [v1.2](阶段C对照规则冻结_v1.2.md)，不覆盖历史快照。本快照记录针对“关键事件记录缺失”回答边界的单点修正、全仓库测试和已曝光题集机器回归。此快照之后在线代码不再变更；下一批新留出集应以本快照为候选版本。

## 1. 修正内容

- `app/tools/cross_source_demo.py`：当查询没有返回 `service_restored` 事件记录时，明确说明“数据中缺少该记录”不等于“现实中尚未恢复”。原有 `accepted` 缺失说明保持不变。
- `tests/test_cross_source_demo_failure_paths.py`：扩展缺事件路径断言，分别保证 `accepted` 与 `service_restored` 缺行都披露数据记录与现实业务事实的边界。
- 不因 CS03 错误 gold 改变客户范围冲突早停行为；未修改 CS03 v0.1.1 的 runtime、gold、原始运行报告或匿名表。

## 2. 测试与已曝光回归

- 定向：`PYTHONUTF8=1; PYTHONPATH=.; uv run pytest tests/test_cross_source_demo_failure_paths.py -q`，33 项通过。
- 全仓库：`PYTHONUTF8=1; uv run pytest -q`，247 项通过，2 条既有依赖弃用警告。
- 已曝光 CS-02 v2.1 regression：报告 `eval/reports/cross_source/cross-source-paired-20261003T091402Z.json`，10 题、运行错误 0、双方通过 4、仅 B 通过 6、其余 0。
- 已曝光 v0.1.2 regression：报告 `eval/reports/cross_source/cross-source-paired-20261003T091428Z.json`，9 题、运行错误 0、双方通过 4、仅 B 通过 5、其余 0。
- 以上机器转移仅验证已曝光数据中的结构化判据，不代表自然语言总体正确率或泛化效果。`git diff --check` 通过。

## 3. CS03 v0.1.1 评估有效性勘误

- CS03 v0.1.1 原始 A/B 报告和匿名盲审材料均保留，但不得引用初始 9/12 严格人工通过数作为效果结论。
- 后续全题跨源审计确认两处 gold 与早停契约冲突：CS03-06 客户冲突应早停、gold 却要求合同文档编号；CS03-09 客户冲突应早停、gold 却要求继续评定文档中的双时限。报告：`eval/cross_source_heldout/2026-10-03-synth-cs-03-v0.1.1_review/postrun_gold_audit.md`。
- 两份盲审表各 24 项完整，但正确性/证据判断存在多处分歧，并有复核者漏读已展示的 4 小时修复条款。原始标签不覆盖、不篡改；解盲解释见 `eval/reports/cross_source/cross-source-postreview-20261003T085017Z.md`。
- 因数据 gold 有误且人工标签不稳定，CS03 v0.1.1 只能作已曝光审计/回归资料，不是有效独立留出成绩。新评估应由独立人员编制并复核，并重点核验设备—映射—合同客户一致性、早停时不得要求后续文档，以及双来源指标判据。

## 4. SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `58141a0070e26c5ecedcfe60a60b0d935b0f55ac073ee024ac9b1bbfdf0c37dc` |
| `tests/test_cross_source_demo_failure_paths.py` | `46c84b3edd8ae78271953852e552c5c207f07606ae34c921ac2c4fb65521fe63` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `1d760c5f8bce2c8e24c18f94d8640c24367b4339d98deeeffc185fdc5da5b147` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_runner.py` | `a695f5753e60f33f7edd93db298207d9ac3c6e71c6d3bc5ded8461a3897ef67f` |
| CS-02 v2.1 regression report | `68f1c85d3b1e60ea883f4ad5d6400985d6375f15b47e1796b2aaf83a2470480f` |
| v0.1.2 regression report | `b2e7dcb10ba3e94c564b42531053cf8a58e28af7c03603cf4130af5d29bb8822` |

继续进行泛化评估前，应按本快照代码编制另一套未曝光题包；题包须由编制者之外的人静态复核，A/B 前运行者不得查看 gold。评分需覆盖机器判据、人工答案完整性，并对客户冲突/安全早停使用明确一致的判分规则。
