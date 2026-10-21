# EvoOntology 实验现状总结

更新时间：2026-10-04  
实验范围：EvoOntology Builder 知识对 AdventureWorks 任务4（多表 SQL 问答）中 schema 定位及最终 SQL 答案的作用。结论不外推到其他数据集、任务或真实项目流量。

## 当前结论

**现阶段不建议把整包 EvoOntology 知识或完整按需检索流程接入项目正式主链路。** 目前实验没有证明它能稳定提高最终 SQL 正确率；整包知识在最新试跑里明显增加了词元和延迟。这个结论表示“收益证据不足以支持接入”，不表示 EvoOntology 已被证明无效。

## 做过哪些实验

| 实验 | 规模与比较 | 主要结果 | 能得出的结论 |
|---|---|---|---|
| 任务4直接定位 | 32 题；A/B/C 定位条件 | 整包知识 B−A 的字段召回差为 +15.62 个百分点，95% 区间 0 至 +31.25；C−A 为 +2.97，区间 −16.41 至 +22.66 | 整包知识可能帮助定位，但区间含零；字段定位提升不能直接等同于最终 SQL 答案提升 |
| 任务4消融 | 32 题；拆分知识、检索和结构裁剪等因素 | 全 schema 下加入知识 D−A 为 +1.02 个百分点，95% 区间 −20.86 至 +21.88；完整按需流程 C−A 为 −1.17，区间 −23.05 至 +20.70 | 未能从消融实验中确认稳定的知识贡献；估计仍不精确 |
| Builder 独立验证 | 新查询集 A/B/C 各 30 题；另做受控缺陷修复 | 新查询正确率没有观察到增益；一个受控缺陷经修复后转为通过 | 验证了窄范围的缺陷修复可行性，不能证明常规查询质量普遍提高 |
| 最终 SQL A/B 试跑 | 35 题，每题每臂 2 次，共 140 次模型生成请求 | A（全 schema、无知识）61/70（87.1%）；B（全 schema、整包知识）63/70（90.0%）。配对差 +2.9 个百分点，95% 区间 −2.9 至 +8.6 | 有小幅正向点估计，但不确定性仍包括无增益；不足以支持接入 |

最新 A/B 试跑中，知识覆盖题为 10 题，B−A 点估计 +10 个百分点，95% 区间 0 至 +25；未覆盖题为 23 题，差值 0，区间 −6.5 至 +6.5。覆盖题数量太少，不能据此宣称稳定收益。整包知识使总词元从 606,570 增至 992,197（+63.6%），延迟中位数从 1,915 毫秒增至 2,347 毫秒（+22.6%）。两臂输出列契约均为 70/70；没有用量缺失、长度截断或请求失败。

各实验关注的指标和题集不同，以上结果不能合并成一个总增益，也不能把模型重复请求当成独立题目增加样本量。

## 可信度与边界

- 最新 A/B 试跑使用相同的完整 schema、系统提示和生成设置，唯一计划差异是 B 加入冻结的 16 条知识；每题每臂两次请求，先在题内平均，再以题为统计单位。
- Gold 与错误变体未放入模型提示；题集和提示经过独立检查，Gold 可执行且非空，评分器校准检查 12/12 通过。模型请求均有完整用量记录，没有失败或长度截断。
- 试跑题集包含独立编写题和知识覆盖补充题，因此覆盖层是有意构造的工程测试，不代表自然业务问题分布。
- 独立确认题集的可行性审查中，25 道候选题经数据库检查后，语义盲审最终判为 16 道有效、7 道存疑、2 道近重复；明确可保留的新 covered 题约 10 道，远低于计划的 80 道。因此未冻结该题集、未对其调用模型，也未将其纳入准确率结果。这是避免低功效小样本被误称为确认实验的停止决定。
- 当前模型端点未公开模型权重修订号，结果只对记录的端点与运行时间成立；若端点后续更新，需重新验证。

## 下一步建议

正式项目主链路暂不加入全量知识提示或整套按需流程。保留 `verified-v0.1` 知识包及实验结果作为研究资产。若要重新判断接入价值，应从项目实际积累的、独立且有代表性的问数问题中建立新确认集，保证 covered 题数量和概念覆盖达到预设要求，再比较最终正确率、词元、时延与检索成本；题目必须先盲审、冻结，不能靠同义改写或更换年份凑数。

## 相关文件

### 结论与设计

- [最终接入价值实验结论](EvoOntology最终接入价值实验结论_v0.1.md)
- [任务4独立确认实验方案及本轮状态](EvoOntology任务4独立确认实验方案_v0.1.md)
- [最终 SQL A/B 试跑设计](EvoOntology任务4A_B试跑设计_v0.2.md)
- [最终 SQL A/B 试跑题集独立审计](EvoOntology任务4A_B试跑题集独立审计_v0.2.md)
- [直接定位实验报告](EvoOntology任务4直接定位实验报告_v0.2.md)
- [消融实验报告](EvoOntology任务4消融实验报告_v0.3.md)
- [Builder 独立验证报告](EvoOntology构建器独立验证实验报告_v0.1.md)

### 最终 SQL A/B 原始材料

- 冻结清单：[`eval/experiments/evont_next/run_task4_ab_pilot_v02/freeze.json`](../eval/experiments/evont_next/run_task4_ab_pilot_v02/freeze.json)
- 冻结题集：[`eval/experiments/evont_next/run_task4_ab_pilot_v02/cases_frozen.json`](../eval/experiments/evont_next/run_task4_ab_pilot_v02/cases_frozen.json)
- 原始模型记录（140 条）：[`eval/experiments/evont_next/run_task4_ab_pilot_v02/records.jsonl`](../eval/experiments/evont_next/run_task4_ab_pilot_v02/records.jsonl)
- 逐题评分：[`eval/experiments/evont_next/run_task4_ab_pilot_v02/scored_pilot_v02.json`](../eval/experiments/evont_next/run_task4_ab_pilot_v02/scored_pilot_v02.json)
- 汇总数据：[`eval/experiments/evont_next/run_task4_ab_pilot_v02/summary.json`](../eval/experiments/evont_next/run_task4_ab_pilot_v02/summary.json)

### 未运行的独立确认题候选及审计

- 候选题：[`eval/experiments/evont_next/task4_ab_confirm_cases_candidate_v01.json`](../eval/experiments/evont_next/task4_ab_confirm_cases_candidate_v01.json)
- 数据库检查：[`eval/experiments/evont_next/task4_ab_confirm_cases_dbcheck_v01.json`](../eval/experiments/evont_next/task4_ab_confirm_cases_dbcheck_v01.json)
- covered/adjacent/uncovered 标签：[`eval/experiments/evont_next/task4_ab_confirm_coverage_labels_v01.json`](../eval/experiments/evont_next/task4_ab_confirm_coverage_labels_v01.json)
- 语义盲审报告：[任务4 A/B 独立确认题集盲审](EvoOntology任务4A_B独立确认题集盲审_v0.1.md)
- 盲审过程记录：[任务4 A/B 独立确认题集盲审过程记录](EvoOntology任务4A_B独立确认题集盲审_过程记录_v0.1.md)
