# 一键复核：只创建本次专用的临时 PostgreSQL，不使用已有业务库或数据卷。
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
$verificationId = [guid]::NewGuid().ToString('N')
$containerName = 'askdata-cross-source-verify-' + $verificationId.Substring(0, 12)
$temporaryRoot = Join-Path $projectRoot 'agent_temp'
$temporaryDirectory = Join-Path $temporaryRoot ('cross-source-verify-' + $verificationId)
$serverOut = Join-Path $temporaryDirectory 'api.out.log'
$serverErr = Join-Path $temporaryDirectory 'api.err.log'
$serverProcess = $null
$environmentKeys = @('CROSS_SOURCE_DEMO_PASSWORD', 'CROSS_SOURCE_DEMO_DSN', 'CROSS_SOURCE_DEMO_DOCS_DIR',
                     'CROSS_SOURCE_DEMO_ENABLED', 'CROSS_SOURCE_DEMO_TIME_BUDGET_MS',
                     'CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT', 'DOC_SEARCH_BACKEND', 'GRAPH_BACKEND')
$originalEnvironment = @{}
foreach ($key in $environmentKeys) {
    $originalEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
}

function Get-AvailableLocalPort {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return $listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
}

function Invoke-VerificationPython {
    param([string[]]$PythonArguments)
    & $pythonExecutable @PythonArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python 检查失败（退出码 $LASTEXITCODE）；保留已生成的逐题报告，不计为验收通过。"
    }
}

Push-Location -LiteralPath $projectRoot
try {
    if (-not (Test-Path -LiteralPath $pythonExecutable)) {
        throw '缺少项目 .venv，请先准备项目依赖；此脚本不自动安装或修改依赖。'
    }
    $null = Get-Command docker -ErrorAction Stop
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw 'Docker 引擎不可用，请打开 Docker Desktop；不要恢复出厂设置。' }

    $databasePort = Get-AvailableLocalPort
    $apiPort = Get-AvailableLocalPort
    while ($apiPort -eq $databasePort) { $apiPort = Get-AvailableLocalPort }
    $demoPassword = [guid]::NewGuid().ToString('N')
    $env:CROSS_SOURCE_DEMO_PASSWORD = $demoPassword
    $env:CROSS_SOURCE_DEMO_DSN = "postgresql://cross_source_demo:${demoPassword}@127.0.0.1:${databasePort}/cross_source_demo"
    $env:CROSS_SOURCE_DEMO_DOCS_DIR = Join-Path $projectRoot 'eval\cross_source\heldout\data\documents'
    $env:CROSS_SOURCE_DEMO_ENABLED = '1'
    $env:CROSS_SOURCE_DEMO_TIME_BUDGET_MS = '5000'
    $env:CROSS_SOURCE_DEMO_DOC_ATTEMPT_LIMIT = '2'
    $env:DOC_SEARCH_BACKEND = 'mock'
    $env:GRAPH_BACKEND = 'mock'

    Write-Host "创建专用临时评测容器；数据库端口 $databasePort，API 端口 $apiPort。"
    $image = 'pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b'
    docker run --detach --rm --name $containerName --label "com.askdata.verification.id=$verificationId" `
        --publish "127.0.0.1:${databasePort}:5432" --env 'POSTGRES_DB=cross_source_demo' `
        --env 'POSTGRES_USER=cross_source_demo' --env "POSTGRES_PASSWORD=$demoPassword" `
        --env 'POSTGRES_INITDB_ARGS=--encoding=UTF8 --locale=C' --env 'TZ=Asia/Shanghai' $image
    if ($LASTEXITCODE -ne 0) { throw '临时评测容器启动失败；请检查端口/镜像，脚本不会更改 Windows 保留端口。' }
    $databaseReady = $false
    for ($attempt = 0; $attempt -lt 45; $attempt++) {
        docker exec $containerName pg_isready -q -U cross_source_demo -d cross_source_demo
        if ($LASTEXITCODE -eq 0) { $databaseReady = $true; break }
        Start-Sleep -Milliseconds 1000
    }
    if (-not $databaseReady) { throw '临时 PostgreSQL 45 秒内未就绪。' }

    Invoke-VerificationPython @('-X', 'utf8', 'data/cross_source_demo/load_postgres.py', '--data-dir', 'eval/cross_source/heldout/data')
    $systemIdentifier = (docker exec $containerName psql -U cross_source_demo -d cross_source_demo -Atc 'SELECT system_identifier FROM pg_control_system()').Trim()
    if ($LASTEXITCODE -ne 0 -or $systemIdentifier -notmatch '^\d+$') { throw '未取得隔离实例标识。' }

    $null = New-Item -ItemType Directory -Path $temporaryDirectory -Force
    $serverProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @('-X', 'utf8', '-m', 'uvicorn', 'app.main:app',
        '--host', '127.0.0.1', '--port', "$apiPort") -WorkingDirectory $projectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr -PassThru
    $endpoint = "http://127.0.0.1:$apiPort"
    $apiReady = $false
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if ($serverProcess.HasExited) { throw '辅助 API 启动失败。' }
        try {
            $health = Invoke-RestMethod -Uri "$endpoint/health" -TimeoutSec 1
            if ($health.status -eq 'ok') { $apiReady = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 200
    }
    if (-not $apiReady) { throw '辅助 API 未就绪。' }

    Invoke-VerificationPython @('-X', 'utf8', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_cross_source_*.py')
    Invoke-VerificationPython @('-X', 'utf8', 'app/test_contracts.py')
    Invoke-VerificationPython @('-X', 'utf8', 'eval/tools/validate_cases.py')
    Invoke-VerificationPython @('-X', 'utf8', 'eval/runner.py', '--suite', 'all', '--predictor', 'oracle', '--dialect', 'sqlite', '--tag', 'cross_source_judge_selfcheck')
    Write-Host '上面的标准答案自证只检查判分器，不是模型成绩。开始真实跨源回归。'
    Invoke-VerificationPython @('-X', 'utf8', 'eval/cross_source_pair_runner.py', '--endpoint', $endpoint,
        '--gold', 'eval/cross_source/regression_gold_v0.2.jsonl', '--purpose', 'regression',
        '--baseline-review-status', 'confirmed_operational_assumptions')
    Invoke-VerificationPython @('-X', 'utf8', 'eval/cross_source_boundary_runner.py', '--expected-system-identifier', $systemIdentifier)
    Write-Host '复核通过；原始输出保存在 eval/reports/，此结论仅限合成演示范围，不等于整体架构验收。'
} finally {
    if ($null -ne $serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id
        $null = $serverProcess.WaitForExit(5000)
    }
    # 只清理带有本次随机标识的自建临时容器；不遍历或操作已有业务容器/卷。
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $inspection = docker inspect $containerName 2>$null
    $actualLabel = $null
    if ($LASTEXITCODE -eq 0) {
        $details = ($inspection | ConvertFrom-Json)[0]
        $actualLabel = $details.Config.Labels.'com.askdata.verification.id'
    }
    if ($actualLabel -eq $verificationId) {
        docker stop --time 3 $containerName
        Write-Host '本次临时容器已停止并自动清理；未使用持久数据卷。'
    }
    $ErrorActionPreference = $savedPreference
    foreach ($key in $environmentKeys) {
        [Environment]::SetEnvironmentVariable($key, $originalEnvironment[$key], 'Process')
    }
    # 只删除本脚本创建的两个日志和空目录，校验绝对路径后使用同一 PowerShell 执行。
    $resolvedTemporary = [IO.Path]::GetFullPath($temporaryDirectory)
    $allowedTemporary = [IO.Path]::GetFullPath($temporaryRoot).TrimEnd('\') + '\'
    if ($resolvedTemporary.StartsWith($allowedTemporary, [StringComparison]::OrdinalIgnoreCase)) {
        foreach ($file in @($serverOut, $serverErr)) {
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
        }
        if ((Test-Path -LiteralPath $temporaryDirectory) -and -not (Get-ChildItem -LiteralPath $temporaryDirectory -Force)) {
            Remove-Item -LiteralPath $temporaryDirectory
        }
    }
    Pop-Location
}
