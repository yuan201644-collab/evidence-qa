# 阶段 C 跨源候选实现与评测快照 v0.8

冻结日期：2026-10-02。承接 v0.7，记录 v0.4.1 修复后回归时的在线候选、测试与评测报告。v0.4/v0.4.1 均已向当前操作者暴露；这份快照和结果仅用于可复现回归，不构成新留出泛化、真实业务验证或赛事验收。

## 1. 本轮规则变更

- 合同状态仅精确接受去除首尾空白、忽略大小写后的 `active` 和中文 `生效`；其他状态（如 `suspended`）不视为生效。
- 当检索结果明确表明合同未约定服务恢复总时限时，引用聚焦到该明确说明，并同步校正字符偏移；不得用“未检索到条款”误述为“文档未约定”。停用合同的 SLA 文档只可作为背景，不能据此判定 SLA 生效。
- 这些规则来自 v0.4.1 暴露题上的缺陷修复，不能用于声称对未见数据的表现提升。

## 2. v0.4.1 包状态和在线结果

- v0.4 首版审阅未通过，首版文件与报告保留，未覆盖；v0.4.1 另建运行包和 gold。v0.4.1 静态包复核通过，复核未运行系统。运行包目录：`eval/cross_source_heldout/v0.4.1_20261002/`；gold 独立目录：`eval/cross_source_heldout/v0.4.1_20261002_gold/`。
- 运行包 ZIP SHA-256：`57900ced9a378f9b57d3a600122607b3c6389cd681151e051280838426966fbf`；`gold.jsonl` SHA-256：`bf7414eb287dae26437c0ba7e9554362074d818337faa089bb65a905173e2dc5`。
- 修复后隔离 PostgreSQL/API 报告：`eval/reports/cross_source/cross-source-paired-20261002T084015Z.json`，SHA-256：`9b638eb5e43871bfbc15b13f1504e7d81af34ea67950bfea26e932ed92e4131f`。报告状态 `regression_after_review`，数据状态 `previously_exposed_not_unseen`；12 题，运行错误 0，源码快照未变化。自动字段题级转移：双方全项通过 1、仅 B 通过 11、仅 A 通过 0、双方均未通过 0。它是自动判据结果，不是经人工复核的准确率。
- 修复前的匿名盲审表来自 v0.4.1 首次运行，不能用于判断修复后输出；没有对修复后的 12 题重新做独立语义盲审。具体历史见 `eval/reports/cross_source/cross-source-blind-review-20261002T081521Z_completed.md` 和阶段 C 验证记录 §26。

## 3. 测试

- 跨源专项：`uv run python -m pytest tests -k cross_source -q`，50 项通过。
- 全仓库：PowerShell 设置 `$env:PYTHONUTF8='1'` 后运行 `uv run python -m pytest -q`，232 项通过，2 条弃用警告。
- `git diff --check` 通过。
- Windows 上全仓库测试需要 `PYTHONUTF8=1`，否则已有 Git 输出解码测试可能受系统 GBK 编码影响。测试通过范围不等同生产环境或业务验收。
- 补充只读代码复核未发现阻断问题，确认合同状态白名单、聚焦引用偏移和停用合同文案有对应覆盖；复核者未运行系统、未接触 gold 或答案，不视为留出数据审阅。

## 4. 本快照关键 SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `aa34c88fdb877678b2b90e90d3e174e9de1b2735c34e306bd424d20c4fae2b72` |
| `app/tools/cross_source_policy.py` | `e9b26295c78379025261f375785490fa417b14a010ea7fa3067555d4e821a8ce` |
| `tests/test_cross_source_demo_failure_paths.py` | `5e2046c8f64e0558ecf2d9011f329331d88ff154a847683dc6c6425b48a7baa4` |
| `tests/test_cross_source_comparison.py` | `a20d23c9ce2acc3f1ee211dfb23aeb08f75c975f50f4e992294f4b3893fb66e8` |
| `eval/reports/cross_source/cross-source-paired-20261002T084015Z.json` | `9b638eb5e43871bfbc15b13f1504e7d81af34ea67950bfea26e932ed92e4131f` |
| v0.4.1 runtime ZIP | `57900ced9a378f9b57d3a600122607b3c6389cd681151e051280838426966fbf` |
| v0.4.1 gold JSONL | `bf7414eb287dae26437c0ba7e9554362074d818337faa089bb65a905173e2dc5` |

## 5. 下一步及边界

v0.4.1 以及所有旧题都只能用于回归。若需评估泛化，应使用此前未曝光的新题，由独立编制者提供运行包与隔离 gold，另一位审阅者在运行前核验题目/数据/gold；运行者在 A/B 原始回答产生前不接触 gold。即便新合成集通过，也只能证明该合成范围内的表现，不能替代真实业务数据和实际用户任务验证。
