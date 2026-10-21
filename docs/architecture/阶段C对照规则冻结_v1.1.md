# 阶段 C 跨源候选实现与评测快照 v1.1

冻结日期：2026-10-03。承接 [v1.0](阶段C对照规则冻结_v1.0.md)，不覆盖历史快照。本快照记录答案完整性修复候选在 CS-02 v2.1 新合成留出包上的运行与匿名人工复核；该批现已曝光，只能用作回归。

## 1. 验证摘要

- 全仓库：`PYTHONUTF8=1; uv run pytest -q`，242 项通过，2 条既有依赖弃用警告；`git diff --check` 通过。
- 包静态复核：PASS。10 题与 gold 一一对应，清单哈希匹配；CS02-09/10 的 `required_absence_checks` 与工具产生的表、过滤条件、零行计数及 SQL 引用相符。通过 `load_heldout.py --data-dir` 将冻结 schema 映射为工具只读兼容视图，并由运行脚本指定题包文档目录。
- 隔离 PostgreSQL/API 配对运行：`eval/reports/cross_source/cross-source-paired-20261003T072317Z.json`。A/B 各 10 题，运行错误 0，`source_snapshot_unchanged=true`。自动三维全项转移：双方通过 4、仅 B 通过 4、仅 A 通过 0、双方未通过 2。
- 匿名人工复核：匿名表 `cross-source-blind-review-20261003T072332Z.md`；已填写副本 `cross-source-blind-review-20261003T072332Z_filled.md`，20/20 份均有正确性、证据判断和备注。人工正确性：正确 9、部分正确 8、错误 2、无法判断 1；证据：充分 12、部分充分 5、不充分 1、引用不匹配 2。
- 严格人工通过定义为“正确性=正确且证据=充分”。解盲配对为 A 2/10、B 7/10；双方通过 2、仅 B 通过 5、仅 A 通过 0、双方未通过 3。
- 人工与机器口径不同：CS02-02 的 A 被机器判全项通过，人工判错误且引用不匹配；CS02-03 的自动通过项被人工判部分正确；CS02-07、CS02-09 的 B 被机器判未全项通过，人工判正确且证据充分。机器分数不替代答案语义、用户所问事实覆盖和引用是否适切的人工审查。
- 此包为合成数据且已曝光，不能作为统计显著提升、真实业务效果、竞赛验收或泛化证据。首次运行在 A/B 开始前因 Windows Docker 主机端连接未就绪而失败，无输出；运行脚本增加数据库连接探测后重跑成功。没有改动或覆盖本批 runtime/gold、v1.0 快照或历史报告。

## 2. 复现入口

项目根目录 PowerShell：

```powershell
.\eval\cross_source_heldout\run_2026Q4_v0.1.ps1 `
  -DatasetRoot 'eval\cross_source_heldout\runtime\2026-10-03-synth-cs-02-v2.1' `
  -Purpose independent_holdout `
  -GoldPath 'eval\cross_source_heldout\authoring\2026-10-03-synth-cs-02-v2.1\gold.jsonl'
```

运行与盲审完成后，该题集已不再具有未见题资格；若复跑，应使用 `regression` 语义解释结果。不要将同时间戳 `DO_NOT_USE_*` 密钥与匿名复核表一同转发。

## 3. SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `116ed6a0529cf87ff91f8ede0948a1088e694da32922e871994d3c51b2527a2e` |
| `app/tools/cross_source_policy.py` | `e9b26295c78379025261f375785490fa417b14a010ea7fa3067555d4e821a8ce` |
| `tests/test_cross_source_demo_failure_paths.py` | `7a3adf497f310960cada7bcbe384269fefc144ed527ca35873af4516c7ab70d4` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `1d760c5f8bce2c8e24c18f94d8640c24367b4339d98deeeffc185fdc5da5b147` |
| `eval/cross_source_heldout/tools/load_heldout.py` | `2e8969b94ca222eea7cec876eb661f41d0b4bf98360803af9b92a3f9680042b9` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `eval/cross_source_runner.py` | `01bfa7a399192d45b3cbf71cd7966142a85ae495cf716236ffe70ff2f4467a36` |
| `eval/cross_source_reference_baseline.py` | `f4a9ddd773cf3b28352c36e5d0b2b8019aee916eb4858b14847434b547e7360c` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| v2.1 package manifest (runtime + gold entries) | `910620464bca9393216b67b81bb9fb9fde7383555015f2602b81e3071ed3a068` |
| v2.1 package-external gold | `bd4f10a02afbde65cc63c3c3a712a414df8438c0c3ccf2478f11bec363fc710a` |
| A/B report | `b0e202b08bc6f3741106783a0a6bee595189e3b6fde2d90e762d26799df1bea9` |
| Filled anonymous review | `c6b80fe81f551e07fe40b6cb8628656bd7d3dfe745fd4d17ac5857c2e0059a7e` |
