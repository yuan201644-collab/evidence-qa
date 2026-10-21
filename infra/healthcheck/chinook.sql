-- Chinook 数据就绪探针。返回 1 = 就绪，0 = 未就绪。
--
-- 为什么需要它（这是实测出来的，不是预防性设计）：
--   pg_isready 只证明「引擎在监听端口」，它既不检查目标库是否存在，
--   也不检查表里有没有数据。而 docker-entrypoint-initdb.d 只在数据卷为空时执行一次：
--   只要 ict_pgdata 卷里已经有数据，initdb 脚本会被整体跳过，
--   此时容器依然是 healthy，track 表却可能是空的——Demo 会在台上才发现。
--   健康检查因此从单条件（引擎能连）升级为双条件（引擎能连 + 数据就绪）。
--
-- 断言值 3503 是 Chinook 官方数据的 track 行数；换库时必须同步改这里与 README。
-- 用 -f 执行本文件而不是 -c 传 SQL：SQL 里有引号，塞进 compose 的 shell 命令里
-- 要经过 YAML → compose 变量插值 → /bin/sh 三层转义，极易写错且无法评审。
-- 放进文件后，断言逻辑是一份可读、可 diff、可单独执行的 SQL。
\set ON_ERROR_STOP on

select case when count(*) = 3503 then 1 else 0 end as ready
from track;
