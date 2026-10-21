<#
    知证 VeriMind - 一键启动脚本
    ============================================================

    这个脚本做三件事，按顺序：
      1. 检查 PostgreSQL 容器（ict-db:5432）是否在跑，没跑就拉起来
      2. 启动后端 uvicorn（127.0.0.1:8000），等它健康
      3. 启动前端 Vite dev server（127.0.0.1:5184），核对版本与源码目录
      最后打开浏览器。

    设计原则：
      - 每一步失败都给出人话原因和修法，不静默跳过
      - 已在运行的前端只有版本和源码目录都一致才复用
      - 模型加载慢是正常的（6.5 GB），要明确告诉用户"在等什么"

    用法:
      .\start.ps1              # 正常启动
      .\start.ps1 -SkipBrowser # 不自动开浏览器
      .\start.ps1 -Restart     # 先杀掉已占端口的进程再启动
#>

[CmdletBinding()]
param(
    [switch]$SkipBrowser,
    [switch]$Restart
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---------------------------------------------------------------- 常量
$Root       = $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$WebDir     = Join-Path $Root 'web'
$NpmCmd     = Join-Path $WebDir 'node_modules\..\package.json'   # 仅用于存在性检查
$LogDir     = Join-Path $Root 'agent_temp\startup'
$BackendLog = Join-Path $LogDir 'backend.log'
$FrontendLog= Join-Path $LogDir 'frontend.log'
$BackendPort  = 8000
$FrontendPort = 5184
$DbPort       = 5432
$DbContainer  = 'ict-db'
. (Join-Path $Root 'scripts\frontend-runtime.ps1')
$FrontendIdentity = Get-Content -LiteralPath (Join-Path $WebDir 'public\frontend-version.json') -Raw -Encoding UTF8 | ConvertFrom-Json

# ---------------------------------------------------------------- 输出helper
function Write-Title($text) {
    Write-Host ''
    Write-Host ('=' * 64) -ForegroundColor DarkCyan
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 64) -ForegroundColor DarkCyan
}
function Write-Step($n, $total, $text) {
    Write-Host ''
    Write-Host "[$n/$total] " -NoNewline -ForegroundColor Yellow
    Write-Host $text -ForegroundColor White
}
function Write-Ok($text)   { Write-Host "  [OK]   " -NoNewline -ForegroundColor Green;  Write-Host $text }
function Write-Warn2($text){ Write-Host "  [注意] " -NoNewline -ForegroundColor Yellow; Write-Host $text }
function Write-Err($text)  { Write-Host "  [失败] " -NoNewline -ForegroundColor Red;    Write-Host $text }
function Write-Info($text) { Write-Host "         $text" -ForegroundColor DarkGray }

function Test-PortListening([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Test-HttpOk([string]$Url, [int]$TimeoutSec = 5) {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return $r.StatusCode -eq 200
    } catch { return $false }
}

function Stop-PortOwner([int]$Port, [string]$Label) {
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $pid2 = $c.OwningProcess
        if ($pid2 -and $pid2 -ne 0) {
            try {
                $p = Get-Process -Id $pid2 -ErrorAction Stop
                Write-Warn2 "正在结束占用 $Label ($Port) 的进程: $($p.ProcessName) (PID $pid2)"
                Stop-Process -Id $pid2 -Force -ErrorAction Stop
                Start-Sleep -Milliseconds 800
            } catch {
                Write-Warn2 "无法结束 PID $pid2（可能已退出）"
            }
        }
    }
}

# ---------------------------------------------------------------- 开场
Clear-Host
Write-Host ''
Write-Host '  知证 VeriMind  ·  可解释精准问数问答智能体' -ForegroundColor Cyan
Write-Host '  一键启动' -ForegroundColor DarkGray
Write-Host ''
Write-Host "  项目目录: $Root" -ForegroundColor DarkGray
Write-Host "  启动日志: $LogDir" -ForegroundColor DarkGray

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$totalSteps = 4

# ================================================================ 1. 检查环境
Write-Step 1 $totalSteps '检查运行环境'

if (-not (Test-Path $VenvPython)) {
    Write-Err "找不到 Python 虚拟环境: $VenvPython"
    Write-Info '修法：在项目目录执行  uv sync'
    Write-Info '如果没装 uv：pip install uv'
    exit 1
}
Write-Ok 'Python 虚拟环境就绪'

if (-not (Test-Path (Join-Path $WebDir 'node_modules'))) {
    Write-Err '找不到 web\node_modules（前端依赖未安装）'
    Write-Info "修法：cd web 然后执行  npm install"
    exit 1
}
Write-Ok '前端依赖就绪'

# .env 检查
$envFile = Join-Path $Root '.env'
if (-not (Test-Path $envFile)) {
    Write-Warn2 '找不到 .env 文件'
    Write-Info "修法：copy .env.example .env"
} else {
    # 逐行找真正生效的赋值：跳过注释行，只认行首的 KEY=value。
    # 必须用 .NET ReadAllText(UTF8) 读 —— PowerShell 5.1 的 Get-Content 对
    # 无 BOM 的 UTF-8 文件按 ANSI 解码，中文注释会乱码并吞掉换行符，
    # 实测 145 行的 .env 只能读出 89 行，PG_DSN 那行直接消失。
    $pgDsn = $null
    try {
        $envText = [System.IO.File]::ReadAllText($envFile, [System.Text.Encoding]::UTF8)
        foreach ($line in ($envText -split "`r?`n")) {
            $trimmed = $line.Trim()
            if ($trimmed.StartsWith('#')) { continue }
            if ($trimmed -match '^PG_DSN\s*=\s*(\S.*)$') { $pgDsn = $Matches[1].Trim(); break }
        }
    } catch {
        Write-Warn2 "读取 .env 出错: $($_.Exception.Message)"
    }
    if (-not $pgDsn) {
        Write-Warn2 '.env 里没有生效的 PG_DSN —— 向量库会退化成内存模式（每次重启重新索引，很慢）'
        Write-Info '修法：在 .env 里加一行（注意不要放在注释里）'
        Write-Info '  PG_DSN=postgresql://ict:ict_dev_only_change_me@127.0.0.1:5432/chinook'
    } else {
        Write-Ok '.env 已配置 PG_DSN（使用 pgvector 持久化向量）'
    }
}

# ================================================================ 2. 数据库
Write-Step 2 $totalSteps '检查 PostgreSQL 数据库'

$dockerOk = $false
try {
    $null = docker version --format '{{.Server.Version}}' 2>$null
    $dockerOk = $LASTEXITCODE -eq 0
} catch { $dockerOk = $false }

if (-not $dockerOk) {
    Write-Err 'Docker 未运行或未安装'
    Write-Info '修法：启动 Docker Desktop，等托盘图标变绿后重新运行本脚本'
    exit 1
}
Write-Ok 'Docker 可用'

$containerRunning = $false
if (Test-PortListening $DbPort) {
    $containerRunning = $true
    $name = (docker ps --filter "publish=$DbPort" --format '{{.Names}}' 2>$null | Select-Object -First 1)
    if ($name) {
        Write-Ok "数据库已就绪：容器 $name 监听 $DbPort"
    } else {
        Write-Ok "端口 $DbPort 已有服务监听"
    }
} else {
    Write-Warn2 "数据库容器未运行，正在拉起 $DbContainer ..."
    Push-Location (Join-Path $Root 'infra')
    try {
        docker compose up -d db 2>&1 | ForEach-Object { Write-Info $_ }
    } finally { Pop-Location }

    Write-Info '等待数据库通过健康检查（最多 60 秒）...'
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $status = (docker inspect --format '{{.State.Health.Status}}' $DbContainer 2>$null)
        if ($status -eq 'healthy') { $containerRunning = $true; break }
        if (-not $status) { break }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
    }
    Write-Host ''
    if ($containerRunning) {
        Write-Ok '数据库已启动并通过健康检查'
    } else {
        Write-Err '数据库启动超时或失败'
        Write-Info "排查：docker logs $DbContainer"
        exit 1
    }
}

# ================================================================ 3. 后端
Write-Step 3 $totalSteps "启动后端 (127.0.0.1:$BackendPort)"

$backendStarted = $false

if (Test-PortListening $BackendPort) {
    if ($Restart) {
        Stop-PortOwner $BackendPort '后端'
    } else {
        if (Test-HttpOk "http://127.0.0.1:$BackendPort/health") {
            Write-Ok "后端已在运行（/health 正常，复用现有进程）"
            $backendStarted = $true
        } else {
            Write-Warn2 "端口 $BackendPort 被占用但不是健康的后端，正在清理..."
            Stop-PortOwner $BackendPort '后端'
        }
    }
}

if (-not $backendStarted) {
    Write-Info '正在启动 uvicorn ...'
    Write-Info '提示：首次启动要加载 AI 模型（约 6.5 GB），需要 30~60 秒'
    Write-Host ''

    $backendArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort")
    $backendProc = Start-Process -FilePath $VenvPython `
        -ArgumentList $backendArgs `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $BackendLog `
        -RedirectStandardError (Join-Path $LogDir 'backend.err.log') `
        -PassThru -WindowStyle Hidden

    Write-Info "后端进程 PID: $($backendProc.Id)"

    # 判定"就绪"必须满足两个条件，缺一不可：
    #   ① 端口上有监听者，且 ② 那个监听者就是本次启动的 PID
    # 只看 /health 返回 200 是不够的 —— 上一个还没来得及释放 socket 的
    # 旧进程会替新进程回答，表现为"2 秒就绪"，而新进程其实还在加载模型。
    $waitTimeout = 300
    $deadline = (Get-Date).AddSeconds($waitTimeout)
    $waited = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $waited += 2

        if ($backendProc.HasExited) { break }

        $owners = @(Get-NetTCPConnection -LocalPort $BackendPort -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty OwningProcess -Unique)
        $mine = $owners -contains $backendProc.Id
        if ($mine -and (Test-HttpOk "http://127.0.0.1:$BackendPort/health" 3)) {
            $backendStarted = $true
            break
        }

        if ($waited % 15 -eq 0) {
            Write-Host ''
            if ($owners.Count -gt 0 -and -not $mine) {
                Write-Host "          ... 已等待 $waited 秒（端口仍被旧进程 PID $($owners -join ',') 占用，等它释放）" -ForegroundColor DarkGray
            } else {
                Write-Host "          ... 已等待 $waited 秒（模型加载中）" -ForegroundColor DarkGray
            }
        } else {
            Write-Host '.' -NoNewline -ForegroundColor DarkGray
        }
    }
    Write-Host ''

    if ($backendStarted) {
        Write-Ok "后端已就绪（本次进程，用时约 $waited 秒）"
    } else {
        Write-Err '后端启动失败'
        if ($backendProc.HasExited) {
            Write-Info "进程已退出，退出码 $($backendProc.ExitCode)"
        }
        Write-Info "日志：$BackendLog"
        Write-Info "      $(Join-Path $LogDir 'backend.err.log')"
        if (Test-Path $BackendLog) {
            Write-Host ''
            Write-Host '  --- 日志末尾 20 行 ---' -ForegroundColor DarkGray
            Get-Content $BackendLog -Tail 20 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
        exit 1
    }
}

# ================================================================ 4. 前端
Write-Step 4 $totalSteps "启动前端 (127.0.0.1:$FrontendPort)"

$frontendStarted = $false

if (Test-PortListening $FrontendPort) {
    if ($Restart) {
        if (-not (Test-VerimindFrontendSource $FrontendPort $WebDir)) {
            Write-Err "端口 $FrontendPort 不属于本目录前端，拒绝自动停止。请核对占用进程。"
            exit 1
        }
        Stop-PortOwner $FrontendPort '前端'
    } elseif (Test-VerimindFrontendIdentity (Get-VerimindFrontendIdentity $FrontendPort) $WebDir $FrontendIdentity.ui_id) {
        Write-Ok "新版三入口前端已在本目录运行（版本与来源匹配）"
        $frontendStarted = $true
    } else {
        Write-Err "端口 $FrontendPort 是旧页面或另一份工作目录，不能直接复用。"
        Write-Info "请关闭旧实例后重试；本目录的旧实例可用 .\start.ps1 -Restart 更新。"
        exit 1
    }
}

if (-not $frontendStarted) {
    Write-Info '正在启动 Vite dev server ...'

    $npmExe = 'npm.cmd'
    $frontendProc = Start-Process -FilePath $npmExe `
        -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1', '--port', "$FrontendPort", '--strictPort') `
        -WorkingDirectory $WebDir `
        -RedirectStandardOutput $FrontendLog `
        -RedirectStandardError (Join-Path $LogDir 'frontend.err.log') `
        -PassThru -WindowStyle Hidden

    Write-Info "前端进程 PID: $($frontendProc.Id)"

    $deadline = (Get-Date).AddSeconds(60)
    $waited = 0
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 1
        $waited += 1
        if (Test-VerimindFrontendIdentity (Get-VerimindFrontendIdentity $FrontendPort) $WebDir $FrontendIdentity.ui_id) {
            $frontendStarted = $true
            break
        }
        if ($frontendProc.HasExited) { break }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
    }
    Write-Host ''

    if ($frontendStarted) {
        Write-Ok "前端已就绪（用时约 $waited 秒）"
    } else {
        Write-Err '前端启动失败'
        Write-Info "日志：$FrontendLog"
        if (Test-Path $FrontendLog) {
            Write-Host ''
            Write-Host '  --- 日志末尾 20 行 ---' -ForegroundColor DarkGray
            Get-Content $FrontendLog -Tail 20 -Encoding UTF8 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
        exit 1
    }
}

# ================================================================ 收尾
Write-Title '全部就绪'
Write-Host ''
Write-Host "  访问地址:  " -NoNewline; Write-Host "http://127.0.0.1:$FrontendPort" -ForegroundColor Green
Write-Host "  接口文档:  " -NoNewline; Write-Host "http://127.0.0.1:$BackendPort/docs" -ForegroundColor DarkGray
Write-Host "  日志目录:  $LogDir" -ForegroundColor DarkGray
Write-Host ''
Write-Host '  两个服务在后台运行。要停止它们，运行 stop.cmd' -ForegroundColor DarkGray

if (-not $SkipBrowser) {
    Write-Host ''
    Write-Info '正在打开浏览器 ...'
    Start-Process "http://127.0.0.1:$FrontendPort"
}

Write-Host ''
