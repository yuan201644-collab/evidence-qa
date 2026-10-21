<#
    知证 VeriMind - 停止服务

    停掉前端和后端进程。默认**不停**数据库容器（下次启动更快，
    而且容器里存着向量索引，停掉只是省点内存）。
    要连数据库一起停，加 -IncludeDatabase。

    用法:
      .\stop.ps1                     # 只停前后端
      .\stop.ps1 -IncludeDatabase    # 连 ict-db 容器一起停
#>

[CmdletBinding()]
param(
    [switch]$IncludeDatabase
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = $PSScriptRoot

function Write-Ok($text)   { Write-Host "  [OK]   " -NoNewline -ForegroundColor Green;  Write-Host $text }
function Write-Warn2($text){ Write-Host "  [注意] " -NoNewline -ForegroundColor Yellow; Write-Host $text }
function Write-Info($text) { Write-Host "         $text" -ForegroundColor DarkGray }

function Stop-PortOwner([int]$Port, [string]$Label) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) {
        Write-Info "$Label (端口 $Port) 未运行"
        return
    }
    foreach ($c in $conns) {
        $procId = $c.OwningProcess
        if ($procId -and $procId -ne 0) {
            try {
                $p = Get-Process -Id $procId -ErrorAction Stop
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Ok "已停止 $Label : $($p.ProcessName) (PID $procId)"
            } catch {
                Write-Warn2 "无法停止 PID $procId （可能已退出）"
            }
        }
    }
}

Write-Host ''
Write-Host '  知证 VeriMind  ·  停止服务' -ForegroundColor Cyan
Write-Host ''

Stop-PortOwner 5184 '前端'
Stop-PortOwner 8000 '后端'

if ($IncludeDatabase) {
    Write-Host ''
    Write-Info '正在停止数据库容器 ict-db ...'
    Push-Location (Join-Path $Root 'infra')
    try {
        docker compose stop db 2>&1 | ForEach-Object { Write-Info $_ }
        Write-Ok '数据库容器已停止'
    } finally { Pop-Location }
} else {
    Write-Info '数据库容器保持运行（要一起停：.\stop.ps1 -IncludeDatabase）'
}

Write-Host ''
