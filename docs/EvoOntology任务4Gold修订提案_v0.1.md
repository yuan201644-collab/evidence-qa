# EvoOntology 任务4 Gold 修订提案 v0.1

更新日期：2026-10-03。依据：`docs/EvoOntology任务4题目Gold独立审计_v0.1.md`。状态：**提案稿，未写入封存题集，未启动正式实验**。

本文把独立审计拆成两类：可直接修正的结构性 gold 问题，以及必须先确认业务定义的问题。只有后者确认、全题集复核并重新封存后，才能用于正式运行。

## 建议直接修正的 gold

| ID | 当前问题 | 建议修订 |
|---|---|---|
| task4-01 | 缺子类名称来源 | 增加 `Production.ProductSubcategory` 和 `Production.ProductSubcategory.Name`。保留产品子类键及销售额字段。时间范围仍待确认。 |
| task4-05 | 强制采购明细路径 | 采购订单总金额若采用订单头总额，保留 `Purchasing.PurchaseOrderHeader.TotalDue`，移除 `PurchaseOrderDetail` 及其明细字段。 |
| task4-06 | 强制采购明细路径 | 若采用订单总额，保留订单头 `VendorID`、`TotalDue` 与供应商 `Name`，移除明细表和明细字段。金额口径需与 task4-02/22 一并确认。 |
| task4-07 | 把历史成本当当前成本 | 改为 `Production.Product` 的 `ProductID`、`StandardCost`、`ListPrice`；移除 `Production.ProductCostHistory`。 |
| task4-10 | 缺当前记录筛选字段 | 增加 `EmployeeDepartmentHistory.EndDate`；若定义为当前在职人数，也增加 `Employee.CurrentFlag`。 |
| task4-13 | 逐工单缺工单标识 | 若保留“工单的废品率”逐工单输出，增加 `WorkOrder.WorkOrderID`；`ProductID` 只有在要求按产品标注时才列为必需。 |
| task4-16 | 只有地区代码、没有名称 | 增加 `Person.CountryRegion` 和 `Person.CountryRegion.Name`，并保留区域代码关联字段。若允许输出代码则可改为可选。 |
| task4-17 | 将非必要键列为必需 | 从必需字段中移除 `SalesOrderHeader.SalesOrderID`，保留 `Freight`。 |
| task4-18 | 每张订单缺标识 | 增加 `SalesOrderHeader.SalesOrderID`。 |
| task4-23 | 缺入职日期与薪资变更时间 | 增加 `Employee.HireDate`、`EmployeePayHistory.RateChangeDate`；截止时间定义仍待确认。 |
| task4-24 | 当前班次人数缺历史筛选 | 增加 `EmployeeDepartmentHistory.EndDate`；若只统计当前在职员工，增加 `Employee.CurrentFlag`。 |
| task4-25 | 强制连接文档内容表和标题 | 文档数可依据 `Production.ProductDocument` 中每个产品的关联行计数；移除 `Production.Document.Title` 及仅为取标题而要求的表。是否保留 `DocumentNode` 为必要字段，建议按关联表主键与去重口径复核。 |
| task4-26 | 强制无须参与计数的表和字段 | 有图产品数可对 `Production.ProductProductPhoto.ProductID` 去重计数；建议只要求该关联表和 `ProductID`。 |

## 需先确认定义，再冻结 Gold

| ID | 必须确认的问题 | 对 gold 的影响 |
|---|---|---|
| task4-01 | “去年”按哪个固定年份或数据时间锚点？ | 冻结 `OrderDate` 的确定范围；不应依赖运行当天。 |
| task4-02 | “消费金额”含不含税、运费？是否展示客户姓名？ | 决定采用订单头 `TotalDue` 还是商品行金额及所需客户身份表。 |
| task4-06 | 供应商采购总额含税/运费，还是只计商品金额？ | 决定订单头汇总还是明细重算。 |
| task4-09 | “时薪”是历史每次变更的费率，还是当前费率？员工需用姓名还是 LoginID 标识？ | 决定是否按 `RateChangeDate` 选最新记录及是否要求身份字段。 |
| task4-10 | “目前员工”指目前在职员工，还是目前归属该部门的员工？ | 决定 `CurrentFlag` 与部门历史 `EndDate` 的过滤组合。 |
| task4-13 | 废品率是逐工单、按产品还是全库整体？ | 决定是否需要 `WorkOrderID`、`ProductID` 及分组粒度。 |
| task4-14 | 产品标识是否必须是名称，还是 ProductID 可接受？ | 决定 `Production.Product.Name` 是否列为必需字段。 |
| task4-16 | 国家（地区）是否必须显示名称？ | 决定 `Person.CountryRegion.Name` 是必需还是可选。 |
| task4-22 | 采购额与销售额分别采用含税总额还是明细商品额？是否需要产品名称？ | 决定两侧金额字段、名称表和可接受替代路径。 |
| task4-24 | “各班组对应人数”是当前分配人数还是历史任职人数？是否排除离职员工？ | 决定 `EndDate`、`CurrentFlag` 的过滤规则。 |
| task4-27 | 工单需求量包含所有工单，还是只含未完成工单的剩余需求？ | 决定是否需要工单状态/剩余量字段及过滤条件。 |
| task4-29 | “运费差异”指地区总额、平均值、最大最小差，还是地区间逐一比较？ | 当前题面不足以构造唯一 gold；需重写问题与评分规则。 |
| task4-30 | 配送地址指订单收货地址，还是客户登记地址？ | 订单收货地址应从 `SalesOrderHeader.ShipToAddressID` 关联地址；客户登记地址要按个人客户 `PersonID` / 门店 `StoreID` 分支处理。两条路径不能混作一个 gold。 |
| task4-31 | “去年”时间范围是什么？“销售表现”具体指销量、销售额还是其他指标？ | 冻结时间条件及销售指标字段。 |

## 冻结前检查

- 对所有 32 题重写明确粒度、时间范围、金额定义和显示标签要求。
- 对每题分别记录“必须字段”“可接受替代字段/路径”“仅用于展示的可选字段”。不要把某个参考 SQL 的写法当成唯一答案。
- 对金标准执行结构合法性检查，并由业务口径确认者签字或留下审阅记录。
- 生成新的题集版本和 SHA-256；保留 v0.1 原文件及试跑记录不变。
- 评分程序需按可接受替代路径计分，并让多余候选表/字段在精确率中体现，而不只看召回率。
