# 政采口径一键起服务（不改 .env）
#
# 为什么不直接改 .env：.env 保持交付包原样（Chinook 11 表）时，
# `python -m pytest` 是 1048 passed / 0 failed；一旦把 SCHEMA_META_PATH
# 全局指到政采库，那 47 个按 Chinook 词表写的用例就会红。
# 所以政采口径只在**进程级**注入，测试与评测各用各的。
#
# 用法（在项目根目录）：
#   .\start-gov.ps1              # 起后端 127.0.0.1:8000，单端口托管 web/dist
#   .\start-gov.ps1 -Port 8004   # 换端口
#
# 前置：ict-db 容器已跑（docker ps --filter name=ict-db），askdata_gov 已导入快照。

param(
    [int]$Port = 8000,
    [string]$BindHost = '127.0.0.1'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    throw "找不到 $Python —— 先在项目根跑一次 `uv sync`。"
}

# —— 政采口径（与 docs/交付包目录说明_20261018.md 三、3.3 方式 A 一致）——
$env:DATABASE_URL     = 'postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/askdata_gov'
$env:PG_DSN           = $env:DATABASE_URL
$env:RAG_DSN          = $env:DATABASE_URL
$env:SCHEMA_META_PATH = 'infra/schema_meta_gov.json'   # 相对路径按当前目录解析，故上面已 Set-Location 到项目根
$env:APP_TIME_ANCHOR  = '2026-10-17'                   # 政采数据窗口 2026-10-08 ~ 10-17

# 账号库固定在本项目内，避免落到 %LOCALAPPDATA% 与别的副本共用
if (-not $env:VERIMIND_AUTH_DB_PATH) {
    $env:VERIMIND_AUTH_DB_PATH = './agent_temp/verimind-accounts.sqlite3'
}

Write-Host "政采口径：askdata_gov + schema_meta_gov.json + APP_TIME_ANCHOR=2026-10-17" -ForegroundColor Cyan
Write-Host "监听 http://${BindHost}:${Port}/  （首次请求会加载 bge-m3，冷启动约 90 秒）" -ForegroundColor Cyan

& $Python -m uvicorn app.main:app --host $BindHost --port $Port
