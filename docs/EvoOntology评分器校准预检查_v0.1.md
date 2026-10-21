# EvoOntology 评分器校准预检查 v0.1

日期：2026-10-04。性质：离线预检查，不含新模型调用，不构成完整评分校准，也不更改或重算任何历史实验结果。

## 预检查目的

在创建新题集和启动 A/B 调用前，核对现有结果集比较器会怎样处理几种关键反例。检查脚本在内存中构造 Gold/预测结果行，调用现有 `eval.metrics.compare_resultsets`；未访问数据库、网络或模型服务。

## 观测结果

| 校准样例 | 现有判定 | 预期用途 / 风险 |
|---|---|---|
| 只改变输出列顺序 | `pass_loose` | 能检测并宽容处理列顺序变化，但需要按答案契约报告为宽松通过。 |
| 没有排序要求，仅改变行顺序 | `pass` | 行无序结果用多重集比较。 |
| Gold 为名称+金额，预测多返回一个标识编号 | `fail` | 当前比较器不能知道该编号是否被题面允许；必须在题目冻结时定义输出契约。 |
| Gold 为姓、名、电话，预测把姓名合成一列并返回电话 | `fail` | 当前比较器不支持经题意认可的字段组合变换；新题应明确要求分别输出姓名字段，或事先登记变换。 |
| 预测多出一条重复连接行 | `fail` | 行多重性比较能发现该重复计数问题。 |
| 错误日期字段在当前数据库快照碰巧返回同一结果 | `pass` | 结果集评分器无法识别 SQL 语义错误；必须由逐题语义契约、专门错误查询/反例和盲审补足。 |

本次结果确认：现有比较器适合固定数据库快照上的结果集一致性粗筛，不足以单独证明 SQL 业务语义正确。历史 H09 是真实对应案例；H04/H16/H23 说明输出列口径须先明确。

## 启动模型实验前的阻断项

1. 新题每道必须冻结输出列、列含义、粒度、允许等价表达、分组键、时间字段/边界、去重和空值规则。
2. 对错误日期字段、边界、重复关联、计数和粒度至少准备与题意相配的错误 SQL。执行 Gold 与错误 SQL 的校验应能区分；若固定快照不能区分，另用仅测试程序内部的最小反例或标为需要人工语义复核。
3. 对允许的列名/列排列/额外标识信息，在运行前写出统一的接受规则。避免看过预测后逐题开例外。
4. 后续完整校准需实现并保存不少于 12 个正反案例的期望/实际判定表，只有所有预期均通过，才冻结最终评分器并允许首次 A/B 请求。

## 复现

在仓库根目录运行（输出中的中文受 PowerShell 当前代码页影响不改变判定）：

```powershell
@'
from eval.metrics import compare_resultsets
cases = [
    ("column reorder", [["Ada", 2]], [[2, "Ada"]]),
    ("unordered rows", [["Ada", 2], ["Lin", 4]], [["Lin", 4], ["Ada", 2]]),
    ("extra identifier", [["Desk", 10]], [[7, "Desk", 10]]),
    ("combined name", [["Ada", "Lovelace", "555"]], [["Ada Lovelace", "555"]]),
    ("duplicate join row", [["Desk", 10]], [["Desk", 10], ["Desk", 10]]),
    ("wrong date but same snapshot result", [["Plant", 42]], [["Plant", 42]]),
]
for name, gold, pred in cases:
    print(name, compare_resultsets(gold, pred).kind)
'@ | .\.venv\Scripts\python.exe -
```

检查实现：`eval/metrics.py` 的 `compare_resultsets`。历史评分修订说明：`docs/EvoOntology任务4SQL结果判定口径修订_v0.1a.md`。独立审计定位记录：`eval/experiments/evont_next/run_task4_holdout_sql_v01/records.jsonl` 与 `result_equivalence_report.json`。
