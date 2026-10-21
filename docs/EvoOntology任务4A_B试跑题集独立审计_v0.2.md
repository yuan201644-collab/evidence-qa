# EvoOntology 任务4 A/B 试跑题集独立审计 v0.2（最终35题候选）

审计日期：2026-10-04  
候选文件：`eval/experiments/evont_next/task4_ab_pilot_cases_v02.json`  
候选 SHA-256：`a43f4139d9bb9f7a5ee71e323287d839ed8eeb35c8ea93d8558023b6748f4fa0`  
题数：35

## 审核判定：通过

本次重审没有发现阻止35题进入试跑的题面、Gold逻辑或输出列契约问题。先前提出的三项语义修订已落实到 JSON 的实际 `question` 字段：S01明说只累计折扣明细行；E02按 DepartmentID 区分部门实体并输出部门编号；U33明说当前在职按 `Employee.CurrentFlag = TRUE` 判断。U17/U18写明统计全体历史BOM关系且不按StartDate/EndDate筛选；U19写明StartDate含当天、EndDate不含当天、NULL EndDate持续有效。

**通过只表示题面与 Gold 逻辑审查通过，不表示固定数据库非空、mutant可区分或模型效果已通过。** 这些问题不在本次盲审范围内，仍应按只读数据库校验结果独立报告。

## 范围与方法

本次只读候选JSON、`infra/schema/adventureworks_schema.sql`、sealed v0.1/v0.2题集、Builder sealed题集和 `construction_questions.json`。没有读取知识包、模型输出、数据库校验报告、运行记录或实验预测；没有执行SQL或模型。

用 SQL AST 解析 Gold（不执行语句）核对顶层输出别名。35个 Gold 均可解析，别名、顺序均与各题 `output_columns` 一致。Schema 不能证明具体数据分布、结果非空、NULL分布、并列样本是否存在或mutant是否能与Gold区分。

## 逐题审查

“有效”只表示题面、Gold、字段/粒度及输出契约之间一致。

| 题目 | 状态 | 结论 |
|---|---|---|
| U01 | 有效 | 按币对汇总2013年每日AverageRate记录的年度平均值，时间筛选及聚合粒度一致。 |
| U02 | 有效 | 按币对取最高EndOfDayRate日期，RANK保留并列；输出契约还要求返回该最高值。mutant是否区分取决于是否存在并列。 |
| U03 | 有效 | 按促销Category/Type统计定义行，Gold不把促销产品关联数混进来。 |
| U04 | 有效 | 按StartDate、EndDate均包含的日期区间判断与2013年至少重叠一天；Gold条件吻合。 |
| U06 | 有效 | 按ReasonType统计不同订单数，连接键及去重粒度正确。 |
| U08 | 有效 | 按卡类型、到期年份、月份统计不同卡片数。 |
| U09 | 有效 | 按供应商关系统计供应产品种类及平均标准价，数据范围明确。 |
| U10 | 有效 | 按产品对ProductVendor中的AverageLeadTime取最小、最大值。 |
| U11 | 有效 | OnOrderQty为可空字段；题面明确NULL未知且不作为0，Gold排除NULL后只保留有填写值的供应商。 |
| U13 | 有效 | 按采购状态计订单数并汇总SubTotal+TaxAmt+Freight，题面给出公式，Gold字段与粒度吻合。 |
| U16 | 有效 | 按状态计算拒收量总和/订购量总和，Gold是总量加权比率；订购量有正数约束。 |
| U17 | 有效 | 问题明确全体历史BOM关系、不筛有效期；按非空装配ID计不同直接组件。 |
| U18 | 有效 | 问题明确全体历史BOM关系、不筛有效期；按计量单位统计PerAssemblyQty平均值。 |
| U19 | 有效 | 有效期边界已写入实际题面，Gold使用StartDate含当天、EndDate不含当天、NULL持续有效。 |
| U20 | 有效 | 只统计TransactionHistory并明确排除Archive；Gold符合指定表范围。 |
| U22 | 有效 | 将TransactionDate转为日历日期后聚合每天交易数和成本。 |
| U24 | 有效 | 从ProductCostHistory按StartDate比较最早与最晚成本记录日期。 |
| U29 | 有效 | 按州省实体汇总关联税率记录平均值，Gold按StateProvinceID分组且输出ID。 |
| U30 | 有效 | 按TaxType统计税率记录数及均值。 |
| U31 | 有效 | 按CountryRegionCode统计不同州省ID数。 |
| U32 | 有效 | 按PersonType统计Person表内业务实体数。 |
| U33 | 有效 | 题面明确CurrentFlag=true作为当前在职定义，Gold计总员工及当前员工数。 |
| U34 | 有效 | 按Gender统计VacationHours均值。 |
| U35 | 有效 | 按员工统计EmployeeDepartmentHistory关系行，同部门不同任职时段仍分别计数。 |
| U36 | 有效 | 题面只按历史表StartDate筛2012年，EndDate只输出；日期边界与Gold一致。 |
| U37 | 有效 | 按ShiftID统计全部历史关系行数和不同员工数。 |
| U39 | 有效 | 按PayFrequency统计不同员工数和薪资历史记录行数。 |
| S01 | 有效 | 题面现在明确金额只累计UnitPriceDiscount>0的折扣明细；Gold按同一筛选求订单数与折后金额。 |
| S02 | 有效 | 先按SalesOrderID算折后明细额，再汇总有明细的2013销售订单数、最大绝对差及超过1美分的订单数。 |
| E02 | 有效 | 按DepartmentID与Name分组，输出department_id，避免同名部门合并；统计2013年开始任职的不同员工数。 |
| E03 | 有效 | 按部门ID分别统计2013年开始、结束部门任职的不同员工数。 |
| PO01 | 有效 | 按订单头OrderDate年份，先总计订购与收货数量，再计算比例。 |
| PO02 | 有效 | 先逐采购订单聚合明细金额，再按状态汇总与头部小计的差额统计。 |
| PAY01 | 有效 | 按员工首末薪资记录输出时间、Rate、记录数和相对变化率；薪资时间键在schema主键中唯一。 |
| PAY02 | 有效 | 按员工首末Rate筛选严格超过20%的增长，Gold与百分比定义吻合。 |

## 近重复与统计相关性

此前发现的近重复题已从当前候选移除，包括U12、U14、U15、U38、U40、P01、P02、E01和S03。当前35题未发现与旧sealed或Builder构建题完全重复的题，但有同域或局部相似题，最终结果解释应保留这些限制：

- E02与E03共用2013年部门任职开始事件；E03另统计结束事件，两题观测相关。
- PAY01与PAY02共享薪资首末记录计算；PAY02增加增长阈值筛选，两题不是独立构念。
- U35与Builder历史任职记录题同表同员工粒度；旧题只统计已结束记录并取前几名，本题统计全部关系行。
- U16与Builder采购拒收数量题使用相近事实字段，但分组维度和指标不同。
- PAY01/PAY02与旧工资历史题同域；S01与旧折扣金额题同域；S02与销售订单金额组成题相邻，但题面目标和输出不同。

这些相关性不阻止试跑，但不应把35题简单当作35个相互独立的证据。按构念/题簇汇报效果更稳妥。

## 非阻塞的mutant质量提醒

U33的Gold与题面已一致，但其mutant把Employee连接到EmployeeDepartmentHistory后用`COUNT(*)`统计员工总数，同时还用EndDate定义当前员工。因此变体同时改变了在职定义和员工计数粒度，无法单独归因于CurrentFlag与历史结束日期的差别。若需要把mutant作为“只改变一个语义点”的评分器校准题，建议通过先去重部门历史的子查询/EXISTS构造变体，避免join fan-out影响`employee_count`。这不影响本次Gold题面通过，也不妨碍主A/B模型试跑；但解释mutant诊断时应标明它是复合错误变体。

## 试跑前仍需独立确认

按现有只读校验流程确认每题Gold结果非空、关键值无异常，并检查变体可区分性。尤其U02的ROW_NUMBER变体只有在实际有最高汇率并列时才会和RANK结果不同。完成数据检查后，应把检查结果留在独立校验记录中，不反向改写本次盲审范围或声称盲审者已看过该结果。
