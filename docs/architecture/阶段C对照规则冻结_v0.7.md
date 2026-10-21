# 阶段 C 跨源候选实现与判据冻结 v0.7

冻结日期：2026-10-02（本轮复核更新）。承接 v0.6，纳入用户确认的三条评测护栏、v0.3.2 判据副本和最终已曝光回归。v0.2、v0.3、v0.3.1 均已曝光；本快照只证明本地实现与回归流程的状态，不构成新留出、真实业务验证或赛事验收。

## 1. 已确认的统一判据

1. 零结果证据由 SQL 执行层根据查询结果构造，至少包含表名、过滤字段和值、实际行数；不得采信模型自由文本里的“没有结果”。引用仍须指向同一表和对象，主键为空，并用受控的中文否定表达说明。
2. 检索状态须明示。`clause_present_expected` 要求检索与引用；`bounded_not_found` 要逐项核对尝试次数、策略、未命中状态和候选数，但不证明原合同“不约定”；`retrieval_not_required` 仅允许固定原因码，并要求停止后没有文档检索。
3. 早停原因码固定为：`mapping_unresolved`、`equipment_master_missing`、`mapping_basis_unapproved`、`validity_window_not_cover`、`key_event_missing`、`customer_scope_conflict`、`event_time_reversed`。未列入者不得以“最小证据”为由跳过预期来源。
4. 自动判分分别报告正确性、引用完整性、trace 合规；汇总自动通过不替代人工语义/证据复核。已曝光题只能叫“判据修订后回归”。

## 2. v0.3.2 判据回归

- 独立 gold 副本：`eval/cross_source_heldout/2026Q4_v0.3.2_criteria_gold/gold.jsonl`；交付压缩包：`eval/cross_source_heldout/2026Q4_v0.3.2_gold.zip`。每题已显式标注检索状态。运行数据复用 v0.3.1，运行包 SHA-256 未变；v0.3.1 gold、包、清单、核验报告均保留原样。
- M05/M06/M10 使用执行层 `absence_checks`；M08 核对两次定向检索 trace；M14 与其他许可早停题要求无后续文档检索。
- 隔离 PostgreSQL/API 配对报告：`eval/reports/cross_source/cross-source-paired-20261002T064636Z.json`。15 题，A/B 运行错误 0，运行期源码快照未变。自动字段统计：B 正确性/引用完整性/trace 合规各 15/15；A 分别为 6/15、13/15、11/15；三项都通过的配对为双方 6、仅 B 9。此为已曝光合成题机器回归，不是人工准确率、因果效果或泛化结论。v0.3.2 gold 和本轮比较器仍待另一位未参与实现者复核。
- 最终隔离 PostgreSQL/API 配对报告：`eval/reports/cross_source/cross-source-paired-20261002T070317Z.json`。15 题，A/B 运行错误 0，运行期源码快照未变。自动字段统计：B 正确性/引用完整性/trace 合规各 15/15；A 分别为 6/15、13/15、11/15；三项都通过的配对为双方 6、仅 B 9。此为已曝光合成题机器回归，不是人工准确率、因果效果或泛化结论。
- 比较器边界已补严：没有 `expected_retrieval_state` 的旧 gold 仍检查实际早停原因码；缺项证据的过滤字段和值按完整边界匹配，拒绝字段前缀、值后缀及漏掉多字段条件的引用。团队独立审阅报告为 `eval/cross_source_heldout/2026Q4_v0.3.2_review/independent_review_report.md`，结论通过、无阻断问题。根据非阻断建议，又补充了元数据和引用分别缺失/错配的显式组合测试。
- 测试：跨源专项 47 项通过，全仓库 229 项通过（2 条依赖弃用警告）；全量测试需设置 `PYTHONUTF8=1`，否则现有 `eval/test_metrics.py::test_git_revision` 在 Windows GBK 解码 Git 输出时出现环境性失败。v0.3.1 原清单自检及 v0.3.2 新包/继承清单核验均通过；`git diff --check` 通过。
- 独立审阅的其余非阻断观察：错误项列表可能重复累积；`|` 等非单词分隔符可能使 token 匹配放行，但 absence 引用还受 `row_pk=null` 等约束；参考基线的检索尝试 trace 缺少 `elapsed_ms` 对称字段。均不影响本轮判定，留作后续维护项，不据此扩大此次改动。

## 3. 当前关键文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `9adc88bb75e2d0f5c7aba0e27a1db29ba5b24b7827976909aa9fb1458e27cbd7` |
| `app/tools/cross_source_policy.py` | `a72c131d0f9c1aa0cbc118b49c8b1e34b6b61a77cdca618b5c30774e48c75655` |
| `eval/cross_source_runner.py` | `01bfa7a399192d45b3cbf71cd7966142a85ae495cf716236ffe70ff2f4467a36` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_reference_baseline.py` | `e2ff2cf281b742ad914c93b4e6361e90fe183da508c0d88a37f5c5572e3bce1e` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `736837245057e140901824f94e28c08fa14d55e6f8be43bd697082a8f370be69` |
| `eval/cross_source_heldout/tools/build_v032_criteria_revision.py` | `339e1aa68b73a695cca5570ac1914dca8b912bb349d0c394506ba00bdd2a0f2c` |
| `eval/cross_source_heldout/tools/verify_v032_criteria.py` | `8f484be47c5923c994401806883896f5c6753322f2a12a07e9256bd284e5c0e0` |
| `eval/cross_source_heldout/2026Q4_v0.3.2_gold.zip` | `ba775fe0c9a397d18f4490d5494d5ccf400828045029110b7498024b4192c12d` |
| `eval/reports/cross_source/cross-source-paired-20261002T070317Z.json` | `e2ce8ffc26dc78e54af75e1c64bf5a634c0278a1782088e3a9627e69179217e4` |
| `tests/test_cross_source_comparison.py` | `a20d23c9ce2acc3f1ee211dfb23aeb08f75c975f50f4e992294f4b3893fb66e8` |
| `tests/test_cross_source_demo_failure_paths.py` | `1a11f2f57b85ffe569d179629a6ccca09a5b5747e52f106d5b53c56ad3c0eea0` |
| `tests/test_cross_source_reference_baseline.py` | `be0bd9093eb6a4b489f2aab53924a78b2a155da3ee538720412ad5215d71cfcc` |
| `tests/test_cross_source_real_retrieval.py` | `e7d8ed69311fcaeced0fc831d4b74f892aa92d5d5982c4c98a1f15f34d7801a5` |

## 4. 下一步

v0.3.2 比较逻辑与回归报告已通过团队独立审阅；该批仍已曝光，只作回归。下一步若需验证泛化，应由未参与本轮实现和旧题修订者编制全新运行集，由另一人独立核验，运行者在原始回答生成前不得接触 gold。
