[CmdletBinding()]
param(
    [string]$Subdomain = 'verimind',
    [int]$Port = 8011
)

$ErrorActionPreference = 'Stop'
$runtimeDirectory = Join-Path $env:LOCALAPPDATA 'VeriMind/public-demo'
$outLog = Join-Path $runtimeDirectory 'localtunnel.out.log'
$errLog = Join-Path $runtimeDirectory 'localtunnel.err.log'

New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
if (-not (Test-Path -LiteralPath $outLog -PathType Leaf)) { $null = New-Item -ItemType File -Path $outLog }
if (-not (Test-Path -LiteralPath $errLog -PathType Leaf)) { $null = New-Item -ItemType File -Path $errLog }
try {
    $health = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5
    if ($health.StatusCode -ne 200) { throw "Unexpected gateway status: $($health.StatusCode)" }
} catch {
    if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) {
        throw "The local gateway still runs the old shared-password version on port $Port; restart it using start.ps1 first."
    }
    throw "The local gateway is not healthy on port $Port. $($_.Exception.Message)"
}

$existingProcesses = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -match 'localtunnel.*--port\s+' + $Port + '\b' } |
    Select-Object)
$existing = $existingProcesses | Select-Object -First 1
if ($existing) {
    $activeConnections = @($existingProcesses | ForEach-Object {
        Get-NetTCPConnection -State Established -OwningProcess $_.ProcessId -ErrorAction SilentlyContinue
    } | Where-Object { $_ })
    if ($activeConnections.Count -gt 0) {
        $existingUrl = Select-String -LiteralPath $outLog -Pattern 'https://[^\s]+' -AllMatches -ErrorAction SilentlyContinue |
            ForEach-Object { $_.Matches.Value } | Select-Object -Last 1
        if ($existingUrl) {
            Write-Host "Localtunnel is already running at $existingUrl (PID $($existing.ProcessId))."
        } else {
            Write-Host "Localtunnel is already running (PID $($existing.ProcessId)); inspect $outLog for its URL."
        }
        return
    }
    foreach ($stale in $existingProcesses) {
        Stop-Process -Id $stale.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
}

$npx = (Get-Command npx.cmd -ErrorAction Stop).Source
$proxyAvailable = Test-NetConnection -ComputerName '127.0.0.1' -Port 7897 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($proxyAvailable) {
    $env:HTTP_PROXY = 'http://127.0.0.1:7897'
    $env:HTTPS_PROXY = 'http://127.0.0.1:7897'
    Write-Host 'Using local HTTP proxy 127.0.0.1:7897 for tunnel setup.'
}
$process = Start-Process -FilePath $npx -ArgumentList @('--yes', 'localtunnel', '--port', "$Port", '--subdomain', $Subdomain) -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Seconds 1
    if (-not (Get-Process -Id $process.Id -ErrorAction SilentlyContinue)) {
        throw "Localtunnel exited; inspect $errLog."
    }
    $url = Select-String -LiteralPath $outLog -Pattern 'https://[^\s]+' -AllMatches -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Matches.Value } | Select-Object -Last 1
    if ($url) {
        Start-Sleep -Seconds 2
        $tunnelProcess = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
            Where-Object { $_.CommandLine -match 'localtunnel.*--port\s+' + $Port + '\b' } |
            Select-Object -First 1
        if (-not $tunnelProcess) {
            throw "Localtunnel exited after receiving $url; inspect $errLog."
        }
        Write-Host "Localtunnel URL: $url"
        Write-Host "Localtunnel logs: $outLog"
        return
    }
}
throw "Localtunnel did not report a URL within 30 seconds; inspect $outLog and $errLog."
