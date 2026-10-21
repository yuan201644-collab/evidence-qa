# 阶段 C 独立留出评测冻结 v0.3

冻结日期：2026-10-02。该快照用于修复 N02 引用截断、明确区分 N04 的映射有效期与合同有效期后，准备下一批全新题集。它不修改 v0.1/v0.2 历史记录或公共接口，也不构成比赛验收结论。

## 1. 对照方案与判定口径

| 方案 | 定义 |
|---|---|
| A：操作性参考基线 | `eval/cross_source_reference_baseline.py`，按阶段 B 记录复原的算法；不是历史阶段 B 源码、经典 KAG 或行业基线 |
| B：阶段 C 候选 | `app/tools/cross_source_demo.py`，固定只读 SQL、按受控编号检索合同文档、代码核验跨源关系与事件时限 |

最终人工严格通过定义为：正确性=“正确”且证据=“充分”。“部分正确”“无法判断”“错误”或证据不是“充分”均不计严格通过。正确性评估事实/结论和关键理由是否成立；证据评估引用能否独立支持结论，包括文档条款、结构化字段和拒判依据。拒判题应评估实际支持拒判的证据，不要求不存在的 SLA 正例字段。自动程序只报告结构化一致性诊断，不代替人工语义/证据判断。

## 2. 冻结的运行规则

- A/B 读取同一结构化数据、文档和问题；A 单次检索，B 按当前候选策略执行至多两次有界文档检索。
- 运行者在 A/B 原始回答生成前不得读取 gold；gold 与运行包分开保存。题集编制者不得查看配对报告或密钥后再担任该批独立复核人。
- 两组原始答案收齐后才运行自动结构检查；随后匿名复核者只收到含问题、回答和证据的匿名表，不收到 gold、A/B 配对报告、密钥或作者自审意见。
- 运行异常、语义正确性、证据充分性分开记录。任何新题见答案后改代码或判定规则，该题集转为回归，不得再报未见题结果。
- v0.2（`eval/cross_source_heldout/2026Q4_v0.2`）已完成盲审并解盲，只能作为已曝光回归题集。下一批必须是重新独立编制的题集；问题不限定必须 12 题，具体组合由团队确认。

## 3. 修复后回归记录

截至冻结日，N02 完整引用“修复完成 / service_restored / 10小时内”原文行并校正对应字符偏移；N04 的映射与合同日期核验分别写入回答理由和 trace，N10 同时展示两个日期窗口都不覆盖工单日期。针对上述行为新增真实文档/BM25和日期组合测试。

- 跨源专项测试：30 项通过。
- 全仓库测试：`PYTHONUTF8=1 uv run python -m pytest -q` 通过；UTF-8 环境变量用于规避 Windows 默认 GBK 读取 Git 中文路径的问题。
- 已曝光 v0.2 隔离 PostgreSQL/API 回归：12 题，运行错误 0，报告 `eval/reports/cross_source/cross-source-paired-20261001T175255Z.json`。本轮没有新的独立人工盲审，不报告自动统计为人工分数。
- 通用 `eval/runner.py` 本轮 NL2SQL 为 15/15，SQLite 执行成功 35 条、跳过 24 条、失败 0；运行器指出工作区有未提交改动，结果不可复现，且与跨源成绩无关。

## 4. 冻结文件 SHA-256

运行新题集前应重算并确认以下关键实现、比较/匿名流程、隔离运行脚本及回归测试的 SHA-256 完全一致。若任一变化，先记录新快照并重新回归；不能在查看新题答案后临时改动。

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `2e0cd94bd2b6f727f89537117d0ef61419c93147d4415df5fd5bdac2707d306a` |
| `eval/cross_source_reference_baseline.py` | `5b51acb5030f95d76612e326d9e70ebe815f1168aa22d85f821d553a7a1bd288` |
| `eval/cross_source_runner.py` | `6a5ebc445aeff18d0ee6e586cf6273097178c261e2f7aae770c200554107894e` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| `eval/cross_source_boundary_runner.py` | `142432aae59b26129798d8927736a30c3e078df40b55e4d454a143903706fa1b` |
| `eval/cross_source_heldout/tools/load_heldout.py` | `2e8969b94ca222eea7cec876eb661f41d0b4bf98360803af9b92a3f9680042b9` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `a1071536623193d3efae4c435f341eebc8d686e16a9fb4b172122981e7ae6e60` |
| `tests/test_cross_source_demo_failure_paths.py` | `92672b748e52fa5fea13e2b74a9cabe6f91d6abb199876198419911bab34b12e` |
| `tests/test_cross_source_real_retrieval.py` | `593e2c09879990388e8c46015e0dbd22d00ffc4c475a7ed2c79669770cf955ad` |
| `tests/test_cross_source_comparison.py` | `de42f00e460862a70c28606aef0c38151ba30ba4dc01c54953d29aa74d280076` |

## 5. 下一步前置条件

工作区存在其他未提交改动；本冻结表只对列出的文件有效，不代表整个仓库为干净提交，也不表示队友已确认跨模块决策。开始下一批独立评测前，由团队确认本冻结口径与题目范围，再安排未参与本轮实现和 v0.2 出题/修订的成员编制运行包、独立复核运行包与 gold。运行者只接触运行包；两组回答生成前 gold 保持受控。
