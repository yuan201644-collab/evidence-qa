# 阶段 C 跨源候选实现与回归规则冻结 v0.5

冻结日期：2026-10-02。此快照承接 v0.4，修复了操作性参考基线对悬空合同映射的空值异常，也修正阶段 C 候选对同一情形的理由和引用。v0.2、v0.3、v0.3.1 数据均已曝光；本快照用于已曝光集回归和新留出运行前核对，不代表独立留出成绩、真实业务验证或比赛验收。

## 1. 对照定义与人工判定口径

| 方案 | 定义 |
|---|---|
| A：操作性参考基线 | `eval/cross_source_reference_baseline.py`，按阶段 B 记录复原的算法，不是历史阶段 B 源码、经典 KAG 或行业基线 |
| B：阶段 C 候选 | `app/tools/cross_source_demo.py`，固定只读 SQL、按受控编号检索合同文档、代码核验跨源关系与事件时限 |

人工严格通过定义为正确性“正确”且证据“充分”。部分正确、无法判断、错误或证据非充分均不计严格通过。自动检查只作结构化诊断，不替代人工语义和证据审核。完整对照隔离、解盲和复核规则沿用[阶段 C 对照规则冻结 v0.4](阶段C对照规则冻结_v0.4.md)第 1–2 节。

## 2. v0.3.1 已曝光回归资产与当前结果边界

v0.3.1 的独立数据复核报告为 `eval/cross_source_heldout/2026Q4_v0.3.1_review/independent_review_report.md`；既有 A/B 在线回归报告为 `eval/reports/cross_source/cross-source-paired-20261002T045349Z.json`。两者均保留原状。该次 A 组 M05 因悬空合同映射触发 `TypeError`；此次为操作性基线增加空值保护，未重跑完整 PostgreSQL/API A/B，因此完整配对效果待验证。

截至本快照：悬空映射定向测试通过；跨源筛选 `uv run python -m pytest tests -k cross_source -q` 为 36 项通过；全仓库 `PYTHONUTF8=1; PYTHONPATH=.; uv run python -m pytest -q` 为 169 项通过，含 2 条依赖弃用警告。随后在隔离 PostgreSQL/API 上以 `regression` 用途在线运行 v0.3.1，报告 `eval/reports/cross_source/cross-source-paired-20261002T052852Z.json`：15 题、A/B 运行异常 0、运行快照未变，自动转移为两组均通过 1、仅 A 0、仅 B 6、两组均未通过自动全项检查 8、运行异常 0。M05 两组均返回 `no_active_contract`；B 现说明合同主表缺少 HT-MED-199 匹配记录并将合同 SQL 引用主键留空。但 M05 仍未通过自动检查：v0.3.1 gold 把事件行列为预期主键，且悬空记录的缺失证据要求未提供检查器所需的字段；自动失败不能替代人工语义复核。已生成匿名回归审阅表 `eval/reports/cross_source/cross-source-blind-review-20261002T053355Z.md`（15 题、30 份候选；未发现组别或 gold 泄露），等待另一位未参与本轮实现/回归的人复核。该数据已曝光，因此这不是新一轮盲测。在线运行已清理临时 API 和数据库容器；检查未发现活动端口监听。

## 3. 关键文件 SHA-256

开始新一轮运行前，重算并确认以下哈希；任何一项改变都先更新快照和回归记录。表中清单覆盖候选、操作性基线、比较/匿名流程、隔离载入流程和主要专项测试，不表示整个仓库无其他未提交改动。

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `c350b76999c2ce63ea47c6fe69cd65739af30f55a995965432b83bf934f6c3be` |
| `eval/cross_source_reference_baseline.py` | `5105e515a67e83dfb0884e244b03afb949eadd9add3db08efb14fc830541055a` |
| `eval/cross_source_runner.py` | `6a5ebc445aeff18d0ee6e586cf6273097178c261e2f7aae770c200554107894e` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| `eval/cross_source_boundary_runner.py` | `142432aae59b26129798d8927736a30c3e078df40b55e4d454a143903706fa1b` |
| `eval/cross_source_heldout/tools/load_heldout.py` | `2e8969b94ca222eea7cec876eb661f41d0b4bf98360803af9b92a3f9680042b9` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `a1071536623193d3efae4c435f341eebc8d686e16a9fb4b172122981e7ae6e60` |
| `tests/test_cross_source_demo_failure_paths.py` | `ba18bfdded227e1be5deec9f778a0e3192246240897db116aa04458939f4bbdf` |
| `tests/test_cross_source_real_retrieval.py` | `64cc7174b64c41a78bfe0488aebff1cc1f8e1ab3f890c917582323d78f63221f` |
| `tests/test_cross_source_comparison.py` | `de42f00e460862a70c28606aef0c38151ba30ba4dc01c54953d29aa74d280076` |
| `tests/test_cross_source_reference_baseline.py` | `724f08f39dd054f152ae3da5a40c821f486ef753a8944c33d6db1cfcb8b702fc` |

## 4. 下一步

如要验证泛化，须由未参与本轮代码修改和已曝光题集修订的成员独立编制并复核全新运行包及包外 gold；运行者在原始回答生成前不得接触 gold。任何改代码或判据后的题集均转为回归集。M05 的自动检查缺口应在新一轮开始前由数据作者/评测负责人确认：是完善缺失合同场景判据，还是把该题保留作人工语义检查；不得为了追求自动通过而放宽其他题判据。 
