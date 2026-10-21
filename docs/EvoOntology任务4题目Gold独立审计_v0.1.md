# EvoOntology 任务4题目与 Gold 独立审计 v0.1

审计日期：2026-10-03。审计对象：`eval/experiments/evont_next/task4_cases_sealed_v0.1.json`。状态：**只读审计完成；尚未据此修改题集或 Gold**。

## 审计方法与边界

由子 agent 独立检查 32 道题的题面、`required_tables`、`required_columns` 与 `infra/schema_meta.adventureworks.json`。未查看各实验组运行输出或分数，未查询模型，也未查询数据库。本审计判断题面与结构元数据的一致性，不等同于业务专家签字确认，也不把 SQL 是否执行成功当作语义正确。

总体结果：9 题通过，17 题需修订，6 题需讨论。主要风险是：gold 漏掉实现题意必需的字段；将一种可行计算路径误写为唯一必需路径；题面未固定时间、金额、粒度或“当前”口径。

## 逐题意见

| ID | 判断 | 审计意见 |
|---|---|---|
| task4-01 | 需修订 | “各产品子类”要输出子类名称，应加入 `Production.ProductSubcategory` 及 `Name`；目前只有 `Product.ProductSubcategoryID`，只能按 ID 分组。“去年”需固定年份或数据时间锚点。销售额可用数量、单价、折扣，也可用 `SalesOrderDetail.LineTotal`。 |
| task4-02 | 需讨论 | “消费金额”口径不明确：订单头 `TotalDue` 含税和运费，明细 `LineTotal` 是商品行金额。现有 gold 强制明细计算过度限定。若需客户名称，还要补身份表；若 `CustomerID` 可作为标识，现有字段可用于排名。 |
| task4-03 | 通过 | 折扣让利可按 `OrderQty × UnitPrice × UnitPriceDiscount` 汇总并按产品排名；当前字段与名称覆盖合理。 |
| task4-04 | 通过 | 销售地区汇总所需订单地区键、明细金额字段和地区名称均有覆盖。 |
| task4-05 | 需修订 | 采购订单总金额可以直接汇总 `PurchaseOrderHeader.TotalDue`。强制采购明细表与明细字段过度约束，重算明细还可能遗漏税费、运费。 |
| task4-06 | 需修订 | 供应商采购总额可按订单头 `VendorID` 汇总 `TotalDue`，再取供应商名称。强制采购明细字段过度约束；若业务要不含税费的商品额，应明确口径。 |
| task4-07 | 需修订 | `Production.Product.StandardCost` 已提供当前标准成本。强制 `ProductCostHistory` 不必要，且历史成本需处理生效日期。建议使用产品当前标准成本和 `ListPrice`。 |
| task4-08 | 通过 | 产品当前售价、标准成本和名称均可从 `Production.Product` 获取。 |
| task4-09 | 需修订 | 若“每位员工的时薪”指当前时薪，需要 `EmployeePayHistory.RateChangeDate` 选择最新记录；当前 gold 缺少该字段。是否需要员工姓名也需确定。 |
| task4-10 | 需修订 | “目前”人数需要 `EmployeeDepartmentHistory.EndDate` 筛选当前部门记录；若口径为在职员工，还应考虑 `Employee.CurrentFlag`。 |
| task4-11 | 通过 | 各产品库存可按 `ProductInventory.ProductID` 汇总 `Quantity`，并关联产品名称。 |
| task4-12 | 通过 | 地点库存总量需要库存数量、地点键和地点名称，当前 gold 合理。 |
| task4-13 | 需修订 | 题面未说明按工单还是按产品计算废品率。若逐工单输出需 `WorkOrderID`；若按产品输出需明确粒度。当前将 `ProductID` 列为必需，却不足以标识单张工单。 |
| task4-14 | 需讨论 | 评分和产品键足以计算各产品平均分。若必须显示产品名称，则 `Production.Product` 必需；若接受 ProductID，可不强制。 |
| task4-15 | 通过 | 各子类产品数所需产品子类键、产品键和子类标签覆盖合理。 |
| task4-16 | 需修订 | 按 `CountryRegionCode` 分组只能得到代码。若题目要求国家/地区名称，建议加入 `Person.CountryRegion.Name`；若代码可接受，应在题面注明。 |
| task4-17 | 需修订 | 汇总运费只需 `SalesOrderHeader.Freight`。强制 `SalesOrderID` 对总和没有必要。 |
| task4-18 | 需修订 | “每张订单”要求结果能够归属到订单；当前 gold 缺少 `SalesOrderHeader.SalesOrderID`。金额组成字段本身合理。 |
| task4-19 | 通过 | 月销售趋势需要订单日期和明细金额字段；金额也可用 `SalesOrderDetail.LineTotal`。 |
| task4-20 | 通过 | 可按 `CustomerID` 统计订单头记录数；`SalesOrderID` 可作计数依据。若需客户姓名再补身份表，题面当前未要求姓名。 |
| task4-21 | 通过 | 产品销量排名需要产品键、销量数量，产品名称用于展示；当前 gold 合理。 |
| task4-22 | 需讨论 | 采购与销售两侧字段基本覆盖，但金额口径要确认。建议两侧分别按产品汇总后关联，避免直接联接明细造成重复计数。ProductID 可作为名称替代。 |
| task4-23 | 需修订 | “从入职到现在”需要 `Employee.HireDate`；薪资变化排序需要 `EmployeePayHistory.RateChangeDate`。还需明确“现在”是当前日期还是数据中的最新薪资日期。 |
| task4-24 | 需修订 | 若指当前各班次人数，需要 `EmployeeDepartmentHistory.EndDate` 筛当前任职记录；gold 缺当前性筛选字段。还需确认是否按 `Employee.CurrentFlag` 限定在职员工。 |
| task4-25 | 需修订 | 关联文档数可按 `ProductDocument.ProductID` 计数 `DocumentNode`。`Production.Document.Title` 不参与计数，强制 Document 表和 Title 过度约束；产品名称只在要求展示时需要。 |
| task4-26 | 需修订 | 有图产品数可对 `ProductProductPhoto.ProductID` 去重计数。外键数据完整时，Product 和 ProductPhoto 不必列为必需表；现有 gold 对表、字段约束过多。 |
| task4-27 | 需讨论 | 按产品比较库存与工单需求的字段大致覆盖；需确认是全部工单的 `OrderQty`，还是未完成工单的剩余需求。两类事实宜先分别按产品汇总再关联。 |
| task4-28 | 通过 | 按 `ScrapReasonID` 汇总报废数量并关联原因名称，字段和表合理；无原因记录的处理需业务定口径。 |
| task4-29 | 需讨论 | “运费差异”没说明比较对象或计算方式。当前字段可按地区汇总，但不能确定要总额、均值、极差还是地区间差值。 |
| task4-30 | 需修订 | 当前关联路径不成立：`Sales.Customer.CustomerID` 不能直接当作 `BusinessEntityAddress.BusinessEntityID`。若“配送地址”是订单收货地址，应从 `SalesOrderHeader.ShipToAddressID` 关联 `Person.Address`、`Person.StateProvince`；若指客户登记地址，则需按 Customer 的 PersonID 或 StoreID 分别处理。 |
| task4-31 | 需讨论 | `SellStartDate` 可筛新上市产品，订单日期与销售明细可算表现；但“去年”和“表现”指标未定义，还需确认数据覆盖范围。 |
| task4-32 | 通过 | 按类别汇总库存数量乘产品标准成本，当前库存、产品成本和类别关联字段覆盖合理。 |

## 优先修订顺序

1. **task4-30**：配送地址语义与当前关联路径不符，CustomerID 与 BusinessEntityID 混用。
2. **task4-10、task4-24**：当前人数/班次人数缺少历史记录结束日期筛选。
3. **task4-18、task4-23**：缺订单标识、入职日期和薪资变化日期等必要字段。
4. **task4-01、task4-31**：相对时间“去年”无固定时间锚点；task4-01 还缺子类名称表。
5. **task4-05、task4-06、task4-07**：gold 把非必要或不合适的数据路径写成必需。
6. **task4-13、task4-29**：题面没有定义清楚统计粒度或差异指标。

## 需要团队业务口径确认

- 金额口径：task4-02、task4-06、task4-22。
- 当前薪资与“从入职到现在”的截止口径：task4-09、task4-23。
- “目前”员工、当前班次以及未完成工单定义：task4-10、task4-24、task4-27。
- 废品率统计粒度：task4-13。
- 国家/地区是否必须输出可读名称：task4-16。
- “运费差异”的比较方法：task4-29。
- 配送地址是订单收货地址还是客户登记地址：task4-30。
- “去年”基于当前日历年还是数据集时间范围，以及“销售表现”具体指标：task4-01、task4-31。
