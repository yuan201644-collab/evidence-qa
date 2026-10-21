# 阶段 C 跨源候选实现与回归规则冻结 v0.6

冻结日期：2026-10-02。本快照承接 v0.5，新增悬空设备主数据的明确拒判处理。v0.2、v0.3、v0.3.1 题集均已曝光；该版本只用于已曝光回归与后续新留出运行前的代码核验，不代表独立留出成绩、真实业务验证或比赛验收。

## 1. 对照与评审口径

| 方案 | 定义 |
|---|---|
| A：操作性参考基线 | `eval/cross_source_reference_baseline.py`，按阶段 B 记录复原，不是历史源码、经典 KAG 或行业基线 |
| B：阶段 C 候选 | `app/tools/cross_source_demo.py`，固定只读 SQL，基于受控编号检索合同文档，以代码核验跨源关系与事件时限 |

人工严格通过定义为正确性“正确”且证据“充分”。自动判定仅为结构化诊断，不替代答案语义与来源证据复核。新留出题的作者、数据复核者、盲审者与运行者职责分离；gold 在两组原始回答生成前保持受控。流程细则沿用 v0.4/v0.5，不因本版变化。

## 2. M05/M06 暴露分支处理

- M05：设备—合同映射存在，但合同主表无对应记录。A/B 均返回 `no_active_contract`；候选引用保留映射行，并以空 `row_pk` 表明合同主表零结果，不继续按缺失日期推断过期。
- M06：工单和事件存在，设备主数据不存在。SQL 改用左连接，并显式返回设备主表匹配标记；系统区分“工单不存在”与“工单存在、设备主数据缺失”，返回 `missing_reference_data`，附工单行和设备查询零结果证据。A/B 操作性基线具有同样的明确分支。
- v0.3.1 的自动检查仍未通过 M05/M06：M05 gold 要求了对拒判无必要的事件主键，且检查器把 `no row` 当作字面 token，无法识别中文“未返回匹配记录”；M06 的 gold 同样用 `no row` 描述零结果，而实际中文证据为“未返回记录（0 行）”。未改 gold 或检查器，也未将自动未通过解释为人工错误。

## 3. 验证记录

| 验证 | 结果 | 边界 |
|---|---|---|
| 跨源专项测试 | `uv run python -m pytest tests -k cross_source -q`：38 项通过 | 单测不替代在线正确性 |
| 全仓库测试 | `PYTHONUTF8=1; PYTHONPATH=.; uv run python -m pytest -q`：171 项通过 | 有 2 条依赖弃用警告；不是业务效果分数 |
| v0.3.1 隔离 PostgreSQL/API 回归 | `eval/reports/cross_source/cross-source-paired-20261002T055906Z.json`：15 题、运行错误 0、源码快照未变 | 自动转移：两组均通过 1、仅 A 0、仅 B 6、两组均未通过自动全项检查 8；不是人工准确率 |
| 匿名 AI 子 agent 复核 | `eval/reports/cross_source/cross-source-blind-review-20261002T053355Z_subagent_completed.md`：30 份填写 | 它评的是 M06 修复前版本；只是内部匿名回归辅助，不是团队人工独立评审或原始数据核验 |

在线回归脚本完成后已清理临时 PostgreSQL 容器与 API。M05/M06 自动判据问题、M08 未命中时的证据呈现、M14 拒绝非受控映射时 gold 对文档引用的要求与“提前停止”冲突，均需团队先统一判据；不通过增加无必要检索来迎合自动分数。

## 4. 关键文件 SHA-256

新运行前重新核对这些受控文件哈希。任一变化应先更新快照与回归记录。此表不是整个仓库的干净状态证明。

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `702c7b293695138e90f28780ab03174034ab2b1dbfb3a5cbeccabc503ffcf250` |
| `eval/cross_source_reference_baseline.py` | `ef0353146957bfd8b6dd4d4a2a0bf64f804193d022eead298bd6bc97778ecd4d` |
| `eval/cross_source_runner.py` | `6a5ebc445aeff18d0ee6e586cf6273097178c261e2f7aae770c200554107894e` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| `eval/cross_source_boundary_runner.py` | `142432aae59b26129798d8927736a30c3e078df40b55e4d454a143903706fa1b` |
| `eval/cross_source_heldout/tools/load_heldout.py` | `2e8969b94ca222eea7cec876eb661f41d0b4bf98360803af9b92a3f9680042b9` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `a1071536623193d3efae4c435f341eebc8d686e16a9fb4b172122981e7ae6e60` |
| `tests/test_cross_source_demo_failure_paths.py` | `72b2b19f2a84d398802fed2375ddd0a4382c9b051e604521b081bbfa7ccb265c` |
| `tests/test_cross_source_real_retrieval.py` | `64cc7174b64c41a78bfe0488aebff1cc1f8e1ab3f890c917323d78f63221f` |
| `tests/test_cross_source_comparison.py` | `de42f00e460862a70c28606aef0c38151ba30ba4dc01c54953d29aa74d280076` |
| `tests/test_cross_source_reference_baseline.py` | `d2c955403e577db2b4deb62c885ed74f1430e9014b2cfab0180ad8285be59a58` |

## 5. 下一步

先由团队确认 M05/M06 负证据检查、M08 文档未命中、M14 非受控映射的评分口径，再冻结评测器；旧题只作回归。要检验泛化，安排未参与本轮代码修改及 v0.2/v0.3/v0.3.1 题集编制/审阅的成员独立生成全新运行包、gold 并另由他人核验。运行者在 A/B 原始回答生成前不得读取 gold。任何看过答案后的代码或判据变动都会使该题集降级为回归集。
