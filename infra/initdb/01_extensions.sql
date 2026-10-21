-- 01_extensions.sql
-- 在默认库（postgres）里启用 pgvector。
-- 业务库（chinook）的扩展在 03_chinook_extensions.sql 里单独启用——
-- CREATE EXTENSION 是「按库」生效的，不是按实例。
CREATE EXTENSION IF NOT EXISTS vector;
