# 阶段 C 独立留出评测冻结 v0.2

冻结日期：2026-10-02。此版本记录阶段 C 候选、操作性参考基线、评测程序和判定口径的当前哈希，供下一批独立题集使用。它替代 v0.1 对“后续评测源码版本”的引用，不改写 v0.1 或任何历史报告。此处是可校验的工作区快照，不是 Git 提交或签名发布；运行前必须重算哈希确认未漂移。

## 1. 冻结对象与现状

| 方案 | 定义 |
|---|---|
| A：参考基线 | `eval/cross_source_reference_baseline.py`，按阶段 B 文档复原的操作性算法；不是历史阶段 B 源码、经典 KAG 或行业基线 |
| B：候选 | `app/tools/cross_source_demo.py`，当前阶段 C 工单—合同跨源候选 |
| 最终人工判定 | 匿名复核中“正确性=正确且证据=充分”才计通过；部分正确、无法判断或证据不足不计通过。自动检查作为结构化诊断，不替代语义/证据人工判定 |
| 原始问题集 | `eval/cross_source_heldout/2026Q4_v0.1` 已曝光，只能回归，不得再称独立留出 |

已曝光题集最近一次在线回归为 `eval/reports/cross_source/cross-source-paired-20261001T164018Z.json`，与人工复核所依据的 `...152632Z.json` 相比，24 份 A/B 回答的状态、答案和引用内容均无差异。独立人工复核结论仍为 A 7/12、B 12/12；该结果仅是已知题集回归，不是本冻结版本对新题的成绩。自动检查转移数不替代上述人工判定。

## 2. 冻结参数

- A/B 使用同一结构化快照、合同文档和问题；只比较决策/补查行为差异。A 单次文档查询；B 最多两次有界查询，第二次为定向补查。
- PostgreSQL 隔离实例；运行脚本使用无持久卷的一次性容器，容器名或端口冲突即停止，不覆盖已有服务。
- BM25 参数沿用 v0.1：`k1=1.5`、`b=0.75`、`top_k=8`；文档过滤逻辑不变。
- 候选预算沿用：`CROSS_SOURCE_DEMO_TIME_BUDGET_MS=5000`、`CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT=2`。预算不是严格端到端硬截止，需报告实测耗时。
- A/B 交错执行；系统回答生成前只向 API 提交问题，不把 gold 注入提示词或请求。配对程序收齐双方原始回答后才读取 gold 做机器核对。
- 匿名复核者只收到匿名 Markdown（回答和引用），不收 gold、A/B 配对报告或映射密钥。复核完成后再由整理者解盲。

## 3. 冻结文件 SHA-256

运行前须确认下表哈希完全匹配。任一项变化即不属于本冻结快照：先形成新版本、跑回归、重新冻结，再开始新题集编制；不得在看到新题答案后悄悄修代码或评分口径。

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `2004250de1afc120c70b09fa4c13805972487da213f4ea83087b220ae3091a78` |
| `eval/cross_source_reference_baseline.py` | `5b51acb5030f95d76612e326d9e70ebe815f1168aa22d85f821d553a7a1bd288` |
| `eval/cross_source_runner.py` | `6a5ebc445aeff18d0ee6e586cf6273097178c261e2f7aae770c200554107894e` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| `eval/cross_source_boundary_runner.py` | `142432aae59b26129798d8927736a30c3e078df40b55e4d454a143903706fa1b` |
| `eval/cross_source_heldout/tools/load_heldout.py` | `2e8969b94ca222eea7cec876eb661f41d0b4bf98360803af9b92a3f9680042b9` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `a1071536623193d3efae4c435f341eebc8d686e16a9fb4b172122981e7ae6e60` |
| `tests/test_cross_source_demo_failure_paths.py` | `61ba8f95cfb64536d22cc56692a7608f5d1e3bfb02fef58fe1eff6b7a3995271` |
| `tests/test_cross_source_real_retrieval.py` | `60e76f82ce4998d1b8eb79bf37d0f8bd244a682d8c2bc2773f1cdd89723d2383` |
| `tests/test_cross_source_comparison.py` | `de42f00e460862a70c28606aef0c38151ba30ba4dc01c54953d29aa74d280076` |

## 4. 新题集独立编制与保管

由未参与上述候选/评测程序开发、调试或 2026Q4 v0.1 题目修改的成员负责编制。编制者交付两个物理分离的部分：

1. **运行包**：`questions.jsonl`、`data/schema.sql`、`data/structured/*.csv`、`data/documents/*.md` 及只含这些运行文件的清单。结构和字段需兼容当前加载器；如果业务定义需要改变，停止并先更新、测试、冻结代码，不得为迁就固定 schema 强行造题。
2. **受控答案**：单独保管的 `gold.jsonl`，列预期状态、事实计算、所需来源定位、负例/停止依据。另存独立 SHA-256 记录。运行者在 A/B 原始回答全部生成前不得查看 gold；运行数据包目录内不得附 gold。

编制者另请未参与编制的人复核题目—结构化事实—文档原文—gold 的逐项一致性，并在运行前冻结两份清单。题目要检验真实的跨源关系、证据或口径边界；不得只改旧题编号、实体名或数字。题数和组合由团队确定，不宣称是赛题规定。

当前通用入口可指定独立运行包和包外 gold（运行数据包只含结构化数据、文档和 questions）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\eval\cross_source_heldout\run_2026Q4_v0.1.ps1 `
  -DatasetRoot '受控运行包目录' `
  -GoldPath '受控目录\gold.jsonl' `
  -Purpose independent_holdout
```

路径以项目根目录为相对基准，也可使用绝对路径。脚本对已曝光的 `2026Q4_v0.1` 拒绝 `independent_holdout` 模式；独立模式必须显式给出运行包目录外的 gold。程序会在两组原始回答生成后才将 gold 传给比较步骤。其报告在人工复核前仍标记为 `pending_independent_review`，不会自行证明独立性；编制隔离、哈希与操作者未见 gold 需由团队另行核实。

如果输出生成前运行者/开发者打开 gold、或依据新题改代码/判分规则，该批即转为回归集；需重新冻结并换一批未见题才能再测泛化。

## 5. 当前验证记录与限制

- 本冻结版本相关跨源测试：27 项通过；Windows PowerShell 解析检查通过。
- 跨源单测 28 项通过，Windows PowerShell 解析检查通过；以已曝光数据验证 `independent_holdout` 会拒绝已知题集路径。
- 使用默认 regression 模式对已曝光 12 题完成一次隔离 PostgreSQL + API 复跑；无请求运行错误，运行期源码快照未变化。机器转移为两组均通过 1、仅 A 通过 1、仅 B 通过 3、两组均未全项通过 7。与此前接受人工复核的 24 份答案/引用内容一致。
- 独立新题集尚未编制，本文件不报告未见题成绩；合成 A/B 的人工对照也不是统计显著性、真实业务泛化或赛事验收。
