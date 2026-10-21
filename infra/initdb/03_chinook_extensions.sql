-- 03_chinook_extensions.sql
-- 切到业务库后再启用 pgvector。\c 是 psql 元命令，
-- 官方 postgres 镜像的 initdb 入口就是用 psql 执行 .sql 文件的，所以可用。
\c chinook;

CREATE EXTENSION IF NOT EXISTS vector;

-- 知识库检索用的表由 B（RAG 线）在应用启动时建，
-- 这里只保证扩展可用，不越界建别人的表。
