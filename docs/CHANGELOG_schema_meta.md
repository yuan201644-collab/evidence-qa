# schema_meta.json 契约变更记录

规则：`infra/scripts/schema_meta_contract.py` 里的 `CONTRACT_VERSION` 只要动过，本文件必须追加一条。
理由：`schema_meta.json` 是 C 交给 A 的接口产物，字段增删改会让下游的解析代码静默出错，
把「改了什么、为什么改、下游要不要动」写在同一处，是唯一能让三个人不互相等的方式。

引用本文件的位置（改文件名或挪位置时，这 5 处要一起改）：

| 位置 | 引用形式 |
|---|---|
| `infra/scripts/schema_meta_contract.py:14` | 文件头「契约版本历史见 docs/CHANGELOG_schema_meta.md。」 |
| `infra/scripts/schema_meta_contract.py:52` | `CONTRACT_VERSION` 上方注释「…并写进 docs/CHANGELOG_schema_meta.md」 |
| `infra/scripts/export_schema_meta.py:80` | 注释「见 docs/CHANGELOG_schema_meta.md 1.1.0」 |
| `infra/README.md` | 「schema_meta.json 的契约」一节 |
| `infra/schema/README.md` | 「复查要点」第 3 条 |

> **别把这里的 `1.1.1` 和 `docs/契约设计_v0.1.md` 的 `v0.1` 混起来**：前者是
> `schema_meta.json` 的**字段结构版本**（只有字段增删改才升），后者是**设计文档版本**
> （改措辞就升）。两者独立、不换算。对照表见 `infra/README.md` 的
> 「两套版本号不是一回事（容易混，先看这里）」一节。

---

## 1.1.1 — 2026-09-18

**变更**：`TableMeta.row_count` 补上 `ge=0` 约束。字段集合一个没动，只收紧了取值。

**为什么**：写契约的负例测试时发现 `row_count = -1` 能通过校验。行数不可能为负，
一个拦不住非法值的约束等于没有约束——而这份契约是要交给 A 当校验依据用的，
放过去会让下游误判「校验过了就没问题」。

**影响面**：
- 合法产物不受影响：`None` 与 `>= 0` 均通过。压力库 `public.ErrorLog` 行数为 0，实测正常通过
- 原本被放过的负数现在会被拒
- **A 侧无需任何改动**：字段没有增删

**同期修正（同一处工程缺陷）**：`verify_infra.py` 曾把契约版本号 `"1.1.0"` 硬编码两份副本，
与契约模块的 `CONTRACT_VERSION` 构成两个事实来源。已改为从契约模块导入：
否则每次升契约版本都要记得改验收脚本，漏改就会拿错误的期望值去比对产物。

---

## 1.1.0 — 2026-09-18

**变更**：`TableMeta` 与 `ForeignKeyRef` 各新增 `schema` 字段，默认值 `"public"`。

**为什么**：v1.0.0 只按表名索引，隐含假设「所有表都在 public」。Chinook 恰好成立，
但压力库 AdventureWorks 实测不成立——它有 5 个保留大小写的 schema：

| schema | 对象数 | 说明 |
|---|---|---|
| HumanResources | 9 | |
| Person | 13 | |
| Production | 25 | |
| Purchasing | 7 | |
| Sales | 23 | |
| public | 3 | 3 个视图 |
| 合计 | 80 | 71 张基表 + 9 个视图 |

缺这个字段会引发三个具体故障：

1. **表名键冲突**：多 schema 下允许同名表，按裸表名建字典会让一张表覆盖另一张，
   而且是静默缺表，不报错。
2. **生成的 SQL 跑不通**：`select * from "Person"."Person"` 少了 schema 限定，
   PostgreSQL 直接报 `relation "person.person" does not exist`（实测确认）。
   这不是 A 的解析 bug，是接口没给够信息。
3. **大小写被折叠**：这批 schema 建库时带双引号创建，裸写会被折叠成小写而查不到对象。

**下游影响**：
- A 的 Schema Linking 与 NL2SQL 在拼 SQL 时请用 `TableMeta.sql_name()`，
  它按 PostgreSQL 规则决定是否加双引号，不要自己 `f"{schema}.{table}"`。
- 旧产物（v1.0.0，无 schema 字段）读进来仍然合法，`schema` 会落到默认值 `public`——
  升级不破坏存量，Chinook 链路无需改动。
- 静态解析路径对未限定的建库脚本（Chinook 的 `CREATE TABLE album`）同样落 `public`，
  与连库路径的结论一致。

**同一版还改了唯一性校验**：`SchemaMeta._no_duplicate_qualified_name`（`schema_meta_contract.py:185`）
从「按裸表名查重」**改为**「按限定名 `(schema, table)` 查重」。v1.0.0 的校验按裸表名，
挡不住跨 schema 的同名表；一旦发生，下游拿到的是静默缺表而不是报错，这类故障最难定位。

**新增自检**：`stats()` 增加 `schemas` 计数。

---

## 1.0.0 — 2026-09-17

**初始版本**。字段：`database` / `dialect` / `source` / `generated_at` / `generator` / `tables`，
表级 `table` / `description` / `row_count` / `primary_key` / `columns` / `foreign_keys`，
字段级 `name` / `type` / `nullable` / `is_primary_key` / `description` / `sample_values`。

**已知缺口（当时未发现，1.1.0 修复）**：无法表达多 schema；Chinook 单库场景下未暴露。

---

## 附：`schema_meta.descriptions.zh.json` 里的 `contract_version`

中文业务说明文件（`infra/schema_meta.descriptions.zh.json`）也带一个 `contract_version` 字段，
但它**不是独立编号**，而是声明「本文件是按哪一版契约的键名格式写的」。

需要注意它的实际状态：**当前没有任何脚本读取这个字段。**
`export_schema_meta.py` 的 `load_descriptions()`（第 287 行起）只取 `tables` 与 `columns`
两个键，不校验版本号；`verify_infra.py` 的版本比对（第 206 行）只针对 `schema_meta.json`。

所以它目前是纯粹的声明性元信息。保留它是为了让读文件的人知道该文件面向哪版键名格式，
但**契约升版时升级它不会带来任何自动化效果，漏升也不会触发任何报错**。
如果将来希望它真的起作用，正确做法是在 `load_descriptions()` 里加一次版本校验，
而不是指望它自己生效。

> 2026-09-19：该文件的 `contract_version` 已从 `1.0.0` 同步到 `1.1.1`（此前落后主契约两个版本号）。
> 这次同步纯粹为了消除表面的版本不一致，键名格式本来就没变。

## 相关文件

- 契约本体：`infra/scripts/schema_meta_contract.py`（pydantic v2）
- 产物：`infra/schema_meta.json`（Chinook，连真库导出）、`infra/schema_meta.adventureworks.json`（压力库）
- 中文业务说明：`infra/schema_meta.descriptions.zh.json`（人工维护，生成器只读取不覆盖）
- 静态生成器（无需数据库）：`infra/scripts/gen_schema_meta_from_ddl.py`
- 连库导出（权威口径）：`infra/scripts/export_schema_meta.py`
- 一键验收：`infra/scripts/verify_infra.py`
