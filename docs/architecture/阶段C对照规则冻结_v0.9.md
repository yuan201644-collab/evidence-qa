# 阶段 C 跨源候选实现与评测快照 v0.9

冻结日期：2026-10-02。承接 v0.8，记录针对复合问题答案完整性缺口的候选代码修订。v0.8 及其历史内容保留不改；v0.6.8 与此前题集均已曝光，本快照只用于复现和回归，不构成泛化、真实业务效果或赛事验收。

## 1. 本轮修订

- 当问题明确询问服务范围、站点、适用性、映射/合同有效期等信息时，答案追加已查询结构化行中的设备、工单日期、映射窗口、合同窗口与合同状态。
- 有效期窗口逐项回答是否覆盖工单日期；设备名称末尾括号中的站点信息单独标注为“站点”。跨 UTC 午夜时按合同配置时区换算工单本地日期，避免将 2026-08-15 本地工单误按 2026-08-14 比较。
- 当问题询问复测、校准、采样或读数要求时，只从本次实际命中的合同文档片段中摘取同时对应当前设备编号的原文行；不推断片段外内容，不把操作要求说成操作已完成或结果已通过。
- 题目明确标注题设假设时，答案标明结果按题设条件计算；明确未约定恢复总时限时，说明不进行时限达标比较、不能判断是否超时。
- “合同明确未约定服务恢复总时限”的拒判分支也呈现该问题要求的结构化日期及已检索到的服务范围条款。
- 不改公共契约、比较器、参考基线、冻结题集、gold 或历史报告。此改动针对已曝光匿名复核中的答案漏项。

## 2. 验证与适用范围

- 定向文件：`tests/test_cross_source_demo_failure_paths.py`，24 项通过。
- 跨源筛选：`$env:PYTHONUTF8='1'; uv run python -m pytest -q -k cross_source`，54 项通过。
- 全仓库：`$env:PYTHONUTF8='1'; uv run python -m pytest -q`，236 项通过，2 条既有依赖弃用警告。
- `git diff --check` 通过。
- 使用隔离 PostgreSQL/API 四次重放 v0.6.8 七题，当前最终报告 `eval/reports/cross_source/cross-source-paired-20261002T122647Z.json`：7 题、运行错误 0、源码快照未变化；自动转移为双方通过 1、仅 B 通过 5、双方未通过 1、仅 A 通过 0。H02 的引用片段自动检查仍因冻结 gold 要求 `2026-10-02 09:30:00+08:00`、PostgreSQL 返回同一时刻的 `2026-10-02 01:30:00+00:00` 而不匹配；不能把该引用字符串差异解读为日期计算错误。B 已在答案正文明确逐项否认两个窗口覆盖并直接回答适用性为“否”。机器字段不检查复合答案完整性，且数据已曝光，不能作为泛化或准确率证明。
- 实现者只读抽查 H01/H02/H03/H04/H06：题设假设有明确标注；H02 分别给出映射期、合同期覆盖判断及直接适用性结论；H03/H06 显示站点与复测要求；H04 显示适用范围及不做时限比较；H06 的跨 UTC 午夜日期按合同 `Asia/Shanghai` 显示为 2026-08-15。该检查不是独立匿名盲审。此前匿名复核均对应早期输出，不作为当前版本的独立复核。
- 最终匿名表 `eval/reports/cross_source/cross-source-blind-review-20261002T122721Z.md` 由未参与本轮代码修改的复核者填写，完成副本为 `eval/reports/cross_source/cross-source-blind-review-20261002T122721Z_fresh_filled.md`。14 份标签均填写：正确 9、部分正确 4、错误 1、无法判断 0；证据充分 11、部分充分 1、不充分 2、引用不匹配 0。解盲后，阶段 C 候选 B 严格通过（正确且证据充分）7/7；操作性参考基线 A 为 2/7。审阅者仅看到匿名回答和其引用证据，没有查看源数据或 gold；这是已曝光七题的匿名答案与证据复核，不是准确率、因果效果或泛化证明。

## 3. 关键文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `app/tools/cross_source_demo.py` | `9b91c4da90eaa71d3615b7456c821617185ec77d4cc014d0fbf4df48dce5ecdb` |
| `app/tools/cross_source_policy.py` | `e9b26295c78379025261f375785490fa417b14a010ea7fa3067555d4e821a8ce` |
| `eval/cross_source_runner.py` | `01bfa7a399192d45b3cbf71cd7966142a85ae495cf716236ffe70ff2f4467a36` |
| `eval/cross_source_blind_review.py` | `769f41d8bd4b7346bfbf9fe211332280d3ebd2f3bac209a4d5688a3eec1e6391` |
| `eval/cross_source_pair_runner.py` | `9eba0b0f1733966783f1910b25e85832f8d6861961b5cf5866cd058564a1d839` |
| `tests/test_cross_source_demo_failure_paths.py` | `0ca8640453c6ae677b3b1dfd7cc14970856916c8c6ae595c211ebc36efb6a085` |
| `tests/test_cross_source_comparison.py` | `a20d23c9ce2acc3f1ee211dfb23aeb08f75c975f50f4e992294f4b3893fb66e8` |
| `eval/reports/cross_source/cross-source-paired-20261002T122647Z.json` | `ab2f4741450c05f32178c29c0dd26203fd560e8c4a59a66a91d29fe96b7cd8d8` |
| v0.6.8 runtime `SHA256SUMS.txt` | `1a553df3dd4a264dd7d75d2fcee39871517ea324368986210ed63684baaafc1d` |
| v0.6.8 `gold.jsonl` | `e62049b186f3898eb837b4a09de51898f6a5aaef20e925c695dc38cc37731b01` |

## 4. 下一步

已完成 v0.6.8 修复后在线回归和最新匿名答案复核。当前最终匿名表与填写副本如上；此前 115122Z、121118Z 表对应较早输出，应弃用。若后续要声明泛化，应另编此前未见题集，由独立人员复核题目、数据与 gold，并在原始回答生成前隔离 gold。合成集仍不能替代真实业务验证。
