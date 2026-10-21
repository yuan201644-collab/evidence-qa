# 阶段 C 跨源候选实现与评测快照 v1.2

冻结日期：2026-10-03。承接 [v1.1](阶段C对照规则冻结_v1.1.md)，不覆盖历史快照。本版本记录 v1.1 之后的修复候选及对已曝光题集的机器回归。CS-02 v2.1 与 v0.1.2 均已曝光，仅可继续作为回归集。

## 1. 修复内容

- `app/nl2sql/normalizer.py`：短词模糊纠错在识别歧义后，不再把唯一的两字模糊命中自动替换为其他表或字段，避免“合同期”等普通中文被改写。
- `app/tools/cross_source_demo.py`：对映射与合同有效期分别给出起止日期、是否覆盖工单日期及合同状态；识别“间隔”类问题；缺失映射引用附带由查询构造的精确过滤条件和零候选数。
- `eval/cross_source_runner.py`：SQL 证据引用比较仅规范化“设备/设备编号”标签和空白，不放宽编号、值或文档原文的精确要求。
- 新增/更新定向测试，覆盖普通中文短词、映射与合同窗口分别说明、事件间隔、缺失映射零结果证据、可接受标签差异及错误设备编号拒绝。

## 2. 验证结果与限制

- 全仓库：`PYTHONUTF8=1; uv run pytest -q`，247 项通过，2 条既有依赖弃用警告；`git diff --check` 通过。
- CS-02 v2.1 已曝光回归：`eval/reports/cross_source/cross-source-paired-20261003T081444Z.json`，10 题、运行错误 0、运行期源码快照未变；B 自动检查 10/10 通过。
- v0.1.2 已曝光回归：`eval/reports/cross_source/cross-source-paired-20261003T081509Z.json`，9 题、运行错误 0、运行期源码快照未变；B 自动检查 9/9 通过。
- 修复后没有新的独立人工盲审。v1.1 的人工判断针对修复前答案，不适用于本快照。自动判据不评定完整自然语言语义、用户要求字段覆盖或所有引用是否适切；不能据此声称人工验收、准确率提升、真实业务效果或泛化。
- 后续泛化检查须另建未曝光题集：编制者与静态复核者独立，运行包与 gold 分离；在 A/B 原始回答生成前，运行者不得接触 gold。若题集被运行者查看或用于调试，立即改标为已曝光回归集。

## 3. SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/nl2sql/normalizer.py` | `01f2067c8fae1b433cdd1f26853d345aa37846e1d2a4845840372f648bcb543a` |
| `app/tools/cross_source_demo.py` | `8bab04ca019a88591fd1e963a07d951392821e94316c1f3bac033a8486d2c481` |
| `eval/cross_source_runner.py` | `a695f5753e60f33f7edd93db298207d9ac3c6e71c6d3bc5ded8461a3897ef67f` |
| `tests/test_cross_source_comparison.py` | `2151c1111abd6e5fc95e9f5e4c4542b9d95f0149be4bd38ad1c359ca71fdf3bb` |
| `tests/test_cross_source_demo_failure_paths.py` | `3ca7b520815b165fbd04d204f671624304d6cd72c2ae2cc4248517b3a0a2e78c` |
| `app/test_normalizer.py` | `eacc3fb00c88c4a989ca455ab428d784727a6c0e1933bd1ad569614fcac0fbd8` |
| `eval/cross_source_heldout/run_2026Q4_v0.1.ps1` | `1d760c5f8bce2c8e24c18f94d8640c24367b4339d98deeeffc185fdc5da5b147` |
| CS-02 v2.1 回归报告 | `b8bf45a3dce41e30c94163ad40d3ed13124f4eeeb579ba442ed20b4ef04f5096` |
| v0.1.2 回归报告 | `a87a2099a3a473cb220a8d76b4d31d50e93b1cd24efe99e63386c20fd5a985e2` |
| CS-02 v2.1 package manifest (`SHA256SUMS.json`) | `910620464bca9393216b67b81bb9fb9fde7383555015f2602b81e3071ed3a068` |
| CS-02 v2.1 package-external gold | `bd4f10a02afbde65cc63c3c3a712a414df8438c0c3ccf2478f11bec363fc710a` |

题集哈希只用于追溯历史材料；它们已曝光，不具备未见题资格。评测台账见 [`eval/reports/cross_source/README.md`](../../eval/reports/cross_source/README.md)。
