#!/usr/bin/env bash
# 重新抓取 Chinook 官方 PostgreSQL 建库脚本到 infra/initdb/02_chinook.sql
#
# 为什么需要这个脚本：02_chinook.sql（约 600KB）已经入库，
# 但它由官方工具生成、且带 WARNING「此文件由工具生成，改动会丢失」。
# 因此规则是：只允许用本脚本整份覆盖，不允许手工改它。
set -euo pipefail

URL="https://raw.githubusercontent.com/lerocha/chinook-database/master/ChinookDatabase/DataSources/Chinook_PostgreSql.sql"
OUT="$(cd "$(dirname "$0")/.." && pwd)/initdb/02_chinook.sql"

echo "下载 Chinook DDL+DATA  ->  $OUT"
curl -fSL --retry 3 -o "$OUT.tmp" "$URL"

# 自检：必须是建库脚本，不是 404 页面
head -20 "$OUT.tmp" | grep -q "Chinook Database" || {
  echo "抓回来的不是 Chinook 脚本，已终止。"; rm -f "$OUT.tmp"; exit 1;
}
grep -q "^CREATE TABLE track" "$OUT.tmp" || {
  echo "缺少 track 表定义，判定文件不完整。"; rm -f "$OUT.tmp"; exit 1;
}

mv "$OUT.tmp" "$OUT"
echo "完成，字节数：$(wc -c < "$OUT")"
echo "接着请重新生成 schema_meta.json："
echo "  python infra/scripts/gen_schema_meta_from_ddl.py"
