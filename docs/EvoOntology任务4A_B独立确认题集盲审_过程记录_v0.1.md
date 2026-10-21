# EvoOntology 任务 4A/B 独立确认题集盲审过程记录 v0.1

日期：2026-10-04

本轮未形成正式盲审结论，不计作独立盲审。

实际读取范围：

- `eval/experiments/evont_next/task4_ab_confirm_cases_candidate_v01.json`
- `eval/experiments/evont_next/task4_ab_confirm_prior_questions_v01.json`
- `infra/schema/adventureworks_schema.sql`

未读取知识包、verified_knowledge、knowledge_relation、数据库校验报告或实验模型输出/记录；未执行 SQL，未调用模型。

字段范围事故：首次读取候选文件时使用了全量文本读取，输出中包含并暴露给审阅者的 `mutant_sql` 和 `candidate_title` 字段，超出原定只读字段范围。其后曾尝试只筛选 `id`、`question`、`gold_sql`、`output_columns`，但首次越界已使本轮不满足盲审条件。

候选文件版本记录：首次读取时 SHA-256 为 `DF971D99C87E5D779445C4A9C181805EC10796FE0164B31508FF30AAD0541FBA`（当时为 33 题版本）。收到负责人更新后核对到候选文件 SHA-256 为 `464CF0D6730C6A511360E7DD92F968ED825430F92B8EDC725C4E22C8106F30C7`（25 题版本）。本过程不对 25 题版本作任何正式有效性判断。
