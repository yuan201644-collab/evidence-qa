-- AdventureWorks 数据就绪探针。返回 1 = 就绪，0 = 未就绪。
--
-- 为什么必须查对象数，而不能只靠 pg_isready：
--   本镜像把数据烘焙在 PGDATA 里，镜像自带的库名是 postgres，
--   而 compose 里的 POSTGRES_DB 只影响「entrypoint 额外创建哪个库」。
--   实测：POSTGRES_DB=adventureworks 时，entrypoint 会新建一个同名空库，
--   pg_isready -d adventureworks 照样返回成功，于是容器报 healthy，
--   而那个 adventureworks 库里对象数是 0 —— 这就是「连上了，但一张表都没有」。
--   断言对象数（同时覆盖「库不存在」与「库是空壳」两种故障）才能挡住它。
--
-- 断言值 80 = 71 张基表 + 9 个视图（5 个业务 schema + public 下的视图），
-- 与本库实测一致；换镜像版本时必须重新核对。
\set ON_ERROR_STOP on

select case
         when (
           select count(*)
           from pg_class c
           join pg_namespace n on n.oid = c.relnamespace
           where c.relkind in ('r', 'v')
             and n.nspname not in ('pg_catalog', 'information_schema')
             and n.nspname not like 'pg_toast%'
         ) = 80
         then 1
         else 0
       end as ready;
