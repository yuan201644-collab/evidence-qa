"""结构化链路（自然语言 → SQL）：把"要算的数"变成一条只读 SQL 并查回结果。

目录职责（对应 docs/功能清单与技术框架_v1.md §3.4 定死的路径）：

    config.py      本链路的配置读取（环境变量 / 连接串）
    schema.py      库结构目录（Schema Catalog）—— 生成 SQL 的输入，也是校验 SQL 的判据
    generator.py   把自然语言 + 库结构交给大模型，产出候选 SQL
    validator.py   sqlglot 静态校验：只读、单语句、表/字段存在、必要时补 LIMIT
    executor.py    真正连库执行（只读事务 + 语句超时）
    pipeline.py    把「生成 → 校验 → 执行」串起来，产出契约的 TraceStep / SqlRef

**本包不 import `app.rag.*`**：结构化链路对非结构化链路保持零 import 依赖
（依据 docs/要素识别与改写_接口_v0.1.md §12.4）。需要大模型客户端时由外层注入 ——
默认装配在 `app/tools/nl2sql.py` 的工具工厂里，一处、惰性。

本模块刻意不在 import 期做任何事（不读文件、不连库、不加载模型）：
上面几个子模块会 import sqlglot / psycopg，放在 `__init__` 里会让"只想用其中一小块"
的调用方也被迫付出代价。
"""
