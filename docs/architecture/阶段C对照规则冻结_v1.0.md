# 阶段 C 跨源候选实现与评测快照 v1.0

冻结日期：2026-10-03。承接 [v0.9](阶段C对照规则冻结_v0.9.md)，不覆盖历史快照。此次冻结的是答案完整性修复后的候选代码及既有比较规则，供下一批全新留出集运行前核对。v0.1.2 题包已运行并解盲，之后只能作为回归集，不再是未见题。

## 1. 相对 v0.9 的修订

- SLA 表格解析兼容两列（项目/时限）和三列（项目/事件类型/时限）格式，避免把已检索到的两列表误报为未命中。
- 多事实问题按实际数据输出事件耗时、单独的映射期/合同期覆盖判断和所问文档条款；明确缺少 `accepted` 时，区分“查询没有事件记录”与“业务事件没有发生”，并把非合同定义的可见时间间隔标明为非 SLA 耗时。
- 缺合同主记录时把合同有效期覆盖结果标为“无法核实”，不把未知状态折叠成“不覆盖”；停用合同问题按题目要求输出日期窗口与合同状态。
- 非受控映射依据的拒判同时呈现演示白名单，并将观察到的依据和允许列表记录在结构化 trace 中。
- 未修改公共契约、默认后端、团队任务书、比较器、参考基线或 v0.1.2 运行包/gold。

## 2. 验证结果与边界

- 定向测试：`tests/test_cross_source_demo_failure_paths.py` 28 项通过。
- 全仓库测试：`PYTHONUTF8=1; uv run pytest -q` 240 项通过；有 2 条既有依赖弃用警告。`git diff --check` 通过。
- v0.1.2 隔离 PostgreSQL/API 修复后回归：`eval/reports/cross_source/cross-source-paired-20261003T063809Z.json`。9 题、运行错误 0、运行期间源码快照未变；自动全项转移为双方通过 4、仅 B 通过 5、仅 A 0、双方未通过 0。
- 该回归复用了已解盲的合成数据。此前匿名人工意见对应修复前输出，不能当作当前版本的人工成绩；当前结果只说明固定题包上的回归行为，不代表统计显著性、真实业务效果、赛事验收或泛化能力。
- 下一次泛化试验应由未看过本次 gold 的独立人员编制另一批题；先做运行包/gold 静态独立复核，再由运行者在 gold 隔离下生成原始回答，最后匿名盲审。

## 3. 冻结文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `a101ad544c8e435e3f5bfe755c2ea68fc1467c65b96f37a7c37edecccd8b9483` |
| `app/tools/cross_source_policy.py` | `e9b26295c78379025261f375785490fa417b14a010ea7fa3067555d4e821a8ce` |
| `tests/test_cross_source_demo_failure_paths.py` | `63929a7b6e3a359f2d6b6edd22ea106bca7c16e6684d34e7f4c232843b3deb8a` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_runner.py` | `01bfa7a399192d45b3cbf71cd7966142a85ae495cf716236ffe70ff2f4467a36` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| `eval/cross_source_reference_baseline.py` | `f4a9ddd773cf3b28352c36e5d0b2b8019aee916eb4858b14847434b547e7360c` |
| v0.1.2 runtime `sha256_manifest.json` | `9f0c6f20d006d86ab253cf106c0d2fc174776ebaff73636c55e5d7fe7c29af3f` |
| v0.1.2 `gold.jsonl` | `d96436708bd8477957d4e97b6e300414bc420664619e716191a08cc193a7c7b8` |
| 修复后回归报告 | `48eee8c6699f13f4fbfebc5a3c9470032c17a6c785c49f92db28193737abad06` |

