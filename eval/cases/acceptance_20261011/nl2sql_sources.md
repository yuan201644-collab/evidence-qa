# acceptance_20261011 单表问数候选题 —— 来源与覆盖说明

- 编题与核验：C（数据库 / 问数线）
- 数据来源：Chinook（容器 `ict-db`，`127.0.0.1:5432/chinook`），题目以**当前实际数据**为准
- 元数据依据：`infra/schema_meta.json` 的表 / 字段描述
- 覆盖点：清晰过滤、计数 / 求和、分组、排序 / 前 N、空结果、相似字段干扰、NULL 处理、单位换算、占比与子查询
- 规模：**内部候选 20 道**（赛题要求至少 10 个单表用例；20 为团队内部建议）
- 参考结果：由 `nl2sql_reference.jsonl` 的 `gold_sql` 在真实库执行得到，非手写
- 与既有题库关系：与 `eval/cases/nl2sql.jsonl` 的 8 道单表题**零重复**

> 说明：`nl2sql_questions.jsonl` 只含 `id` / `task` / `question`，可直接喂给采集器或接口；参考结果与判据只在采集封存后由核验方读取，**不得进入 prompt、请求体或服务端**。

## 逐题依据

| id | 难度 | 表 | 用到的列 | 表行数 | 结果列 | 依据 |
|---|---|---|---|---|---|---|
| acc-01 | easy | `artist` | `artist_id` | 275 | `artist_count` | 单表 COUNT(*)，基础 NL2SQL 单表查询（评分表功能指标 1）。 |
| acc-02 | easy | `media_type` | `name` | 5 | `name` | 单表全列 + 排序，结果小且确定。 |
| acc-03 | easy | `track` | `unit_price` | 3503 | `cheap_track_count` | 单表等值过滤 COUNT。 |
| acc-04 | easy | `track` | `milliseconds` | 3503 | `longest_ms` | 单表 MAX 聚合。 |
| acc-05 | easy | `playlist` | `playlist_id` | 18 | `playlist_count` | 单表 COUNT(*)。 |
| acc-06 | easy | `customer` | `last_name` / `first_name` | 59 | `last_name` / `first_name` | 单表 ORDER BY + LIMIT，多列投影。 |
| acc-07 | easy | `invoice` | `total` | 412 | `max_invoice_total` | 单表 MAX 聚合，金额语义（口径 2.3 容差 0.01）。 |
| acc-08 | medium | `customer` | `country` / `customer_id` | 59 | `country` / `customer_count` | 单表 GROUP BY + ORDER BY + LIMIT。 |
| acc-09 | medium | `track` | `unit_price` | 3503 | `unit_price` / `track_count` | 单表 GROUP BY，按金额列分组。 |
| acc-10 | medium | `employee` | `last_name` / `first_name` / `phone` / `title` | 8 | `last_name` / `first_name` / `phone` | 单表等值过滤 + 多列投影 + 排序。 |
| acc-11 | medium | `track` | `composer` / `track_id` | 3503 | `composer` / `track_count` | 单表 GROUP BY + NULL 排除 + LIMIT。 |
| acc-12 | medium | `customer` | `fax` | 59 | `missing_fax_count` | 单表 NULL 判定 COUNT。 |
| acc-13 | medium | `invoice` | `billing_country` / `invoice_id` | 412 | `billing_country` / `invoice_count` | 单表 GROUP BY + HAVING。 |
| acc-14 | medium | `invoice_line` | `quantity` | 2240 | `total_quantity` | 单表明细表 SUM 聚合。 |
| acc-15 | hard | `track` | `name` | 3503 | `name` / `name_count` | 单表 GROUP BY + HAVING + LIMIT，重复值检测。 |
| acc-16 | hard | `invoice` | `billing_country` / `total` | 412 | `billing_country` / `avg_total` | 单表 GROUP BY + HAVING(avg) + 金额四舍五入。 |
| acc-17 | hard | `track` | `name` / `milliseconds` | 3503 | `name` / `seconds` | 单表计算列 + 单位换算 + 排序 + LIMIT。 |
| acc-18 | hard | `employee` | `reports_to` / `employee_id` | 8 | `manager_id` / `direct_reports` | 单表自关联字段分组（employee 自关联，见 schema_meta）。 |
| acc-19 | hard | `track` | `composer` | 3503 | `null_composer_pct` | 单表条件占比（CASE WHEN 计数 / 总数）。 |
| acc-20 | hard | `invoice` | `total` | 412 | `above_avg_invoice_count` | 单表标量子查询比较。 |
