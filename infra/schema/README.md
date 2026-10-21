# infra/schema —— 数据库 schema 定义文件

对应赛题提交要求 (3)「技术数据 · 作品原始文件（ZIP 数据包）应包含」中的
**数据库 schema 定义文件**一项。

| 文件 | 内容 | 来源 |
|---|---|---|
| `chinook_schema.sql` | 主库 11 张表 + 11 条外键约束的完整 DDL | 从**运行中的** `ict-db` 实例导出 |
| `adventureworks_schema.sql` | 压力库 5 个显式 schema（外加 public）/ 71 张基表 / 90 条外键 | 从**运行中的** `ict-aw` 实例导出 |

## 为什么是「从运行实例导出」而不是手抄

手抄的 DDL 和真正跑起来的库会悄悄分叉——改了没同步、少抄一个约束、
大小写照抄错。这两份文件用 `pg_dump` 从活着的容器导出，
所以它们与评测跑分时用的那个库**必然一致**；
`infra/scripts/verify_infra.py` 又能证明元数据与真库一致，三者互相咬合。

## 怎么重新导出

```bash
# 主库
docker exec ict-db pg_dump -U ict -d chinook \
  --schema-only --no-owner --no-privileges > infra/schema/chinook_schema.sql

# 压力库（注意库名是 postgres，不是 adventureworks）
docker exec ict-aw pg_dump -U postgres -d postgres \
  --schema-only --no-owner --no-privileges > infra/schema/adventureworks_schema.sql
```

`--schema-only` 只要结构不要数据（数据由 `infra/initdb/` 与镜像自带）；
`--no-owner --no-privileges` 去掉依赖具体角色的授权语句，
否则换台机器用别的用户名恢复会报角色不存在。

## 复查要点（实测得出，不是推测）

- **两次导出不是字节一致的。** PostgreSQL 16.9 起，`pg_dump` 会在首尾写入
  `\restrict <随机串>` / `\unrestrict <随机串>`（PSQL 元命令安全加固，
  CVE-2025-1094 后续）。所以不要拿整个文件的哈希当「有没有被改」的依据，
  要比就忽略首尾这两行。
- **该文件用于展示与建表，不是本项目的建库路径。** 真正建库走
  `infra/initdb/*.sql`。若有人想用这两份文件恢复结构，需要 psql ≥ 16.9，
  否则 `\restrict` 是未知元命令。
- 压力库的表名是**大小写敏感**的（`"Person"."Person"`、`"Sales"."SalesOrderHeader"`），
  这是它和主库最大的结构差异——拼 SQL 时漏了双引号会直接报 relation does not exist。
  元数据侧的对应处理见 `docs/CHANGELOG_schema_meta.md`。

## 与 `infra/schema_meta.json` 的分工

不是重复的两份东西：

- **本目录的 .sql**：给人看、给工具建表用，是**结构**的权威表达。
- **`infra/schema_meta.json`**：给 A 的表定位（Schema Linking）用，
  含中文业务注释、样例值、行数、外键指向，是**语义 + 统计**的表达。

取哪个看用途：`sqlglot` 校验 SQL 合法性时想比对表/列是否存在，
用 .sql 解析出的结构更直接；做召回排序时用 schema_meta 的注释和样例值。
