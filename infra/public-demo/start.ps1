[CmdletBinding()]
param(
    [string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'VeriMind/public-demo'),
    [switch]$GatewayOnly
)

$ErrorActionPreference = 'Stop'
$caddyPath = Join-Path $env:LOCALAPPDATA 'VeriMind/tools/caddy.exe'
$caddyConfig = Join-Path $RuntimeDirectory 'Caddyfile'
$tunnelConfig = Join-Path $RuntimeDirectory 'cloudflared.yml'
$statePath = Join-Path $RuntimeDirectory 'processes.json'
$bridgeScript = Join-Path (Split-Path -Parent $PSCommandPath) 'proxy_bridge.py'
$caddyTemplate = Join-Path (Split-Path -Parent $PSCommandPath) 'Caddyfile.example'
$caddyVersion = '2.11.7'
$caddyArchiveName = "caddy_${caddyVersion}_windows_amd64.zip"
$caddyDownloadBase = "https://github.com/caddyserver/caddy/releases/download/v$caddyVersion"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$downloadDirectory = Join-Path $repoRoot 'agent_temp'
$gatewayConfigChanged = $false

New-Item -ItemType Directory -Force -Path $RuntimeDirectory | Out-Null

function Test-RecordedProcess {
    param([object]$Record)
    if (-not $Record) { return $false }
    $process = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    return [bool]($process -and $process.Path -eq $Record.executable)
}

if (-not (Test-Path -LiteralPath $caddyConfig -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $caddyTemplate -PathType Leaf)) {
        throw "Gateway configuration template missing: $caddyTemplate"
    }
    Copy-Item -LiteralPath $caddyTemplate -Destination $caddyConfig
    $gatewayConfigChanged = $true
    Write-Host "Created gateway configuration: $caddyConfig"
} elseif ((Get-Content -LiteralPath $caddyConfig -Raw) -match 'basic_auth|DEMO_PASSWORD_HASH') {
    $legacyBackup = Join-Path $RuntimeDirectory ("Caddyfile.shared-password-{0}.bak" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Copy-Item -LiteralPath $caddyConfig -Destination $legacyBackup
    Copy-Item -LiteralPath $caddyTemplate -Destination $caddyConfig -Force
    $gatewayConfigChanged = $true
    Write-Host "Replaced the retired shared-password gateway; previous config backed up to $legacyBackup"
}
if (-not (Test-Path -LiteralPath $caddyPath -PathType Leaf)) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $caddyPath) | Out-Null
    New-Item -ItemType Directory -Force -Path $downloadDirectory | Out-Null
    $archivePath = Join-Path $downloadDirectory $caddyArchiveName
    $checksumPath = Join-Path $downloadDirectory 'caddy_checksums.txt'
    $extractPath = Join-Path $downloadDirectory ("caddy-extract-{0}" -f $PID)
    try {
        Write-Host "Downloading Caddy $caddyVersion..."
        Invoke-WebRequest -Uri "$caddyDownloadBase/$caddyArchiveName" -OutFile $archivePath -UseBasicParsing
        Invoke-WebRequest -Uri "$caddyDownloadBase/caddy_${caddyVersion}_checksums.txt" -OutFile $checksumPath -UseBasicParsing
        $checksumLine = Get-Content -LiteralPath $checksumPath | Where-Object { $_ -match "\s$caddyArchiveName$" } | Select-Object -First 1
        if (-not $checksumLine -or $checksumLine -notmatch '^([0-9a-fA-F]{128})\s+') { throw 'Caddy checksum entry missing.' }
        $expectedHash = $Matches[1].ToLowerInvariant()
        $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA512).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) { throw 'Caddy archive checksum mismatch.' }
        if (Test-Path -LiteralPath $extractPath) { Remove-Item -LiteralPath $extractPath -Recurse -Force }
        Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force
        $extractedCaddy = Join-Path $extractPath 'caddy.exe'
        if (-not (Test-Path -LiteralPath $extractedCaddy -PathType Leaf)) { throw 'Downloaded Caddy archive did not contain caddy.exe.' }
        Copy-Item -LiteralPath $extractedCaddy -Destination $caddyPath -Force
        Write-Host "Installed Caddy: $caddyPath"
    } finally {
        Remove-Item -LiteralPath $archivePath,$checksumPath,$extractPath -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$health = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 5
if ($health.StatusCode -ne 200) { throw 'Start the backend on port 8000 first.' }

$state = @{ gateway = $null; tunnel = $null; bridge = $null }
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    $saved = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    $state.gateway = $saved.gateway
    $state.tunnel = $saved.tunnel
    $state.bridge = $saved.bridge
}

if ($gatewayConfigChanged -and (Test-RecordedProcess $state.gateway)) {
    Stop-Process -Id $state.gateway.pid -Force
    Start-Sleep -Milliseconds 400
    $state.gateway = $null
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Write-Host 'Restarted the local gateway to apply account-based authentication.'
}

if (-not $GatewayOnly -and -not (Test-Path -LiteralPath $bridgeScript -PathType Leaf)) { throw "Proxy bridge missing: $bridgeScript" }
if (-not $GatewayOnly -and -not (Test-RecordedProcess $state.bridge)) {
    $pythonPath = 'D:\python\python.exe'
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "Python missing: $pythonPath" }
    $bridge = Start-Process -FilePath $pythonPath -ArgumentList @(
        $bridgeScript, '--proxy-port', '7897', '--tcp-port', '7844', '--dns-port', '15353'
    ) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $RuntimeDirectory 'bridge.out.log') -RedirectStandardError (Join-Path $RuntimeDirectory 'bridge.err.log')
    $state.bridge = @{ pid = $bridge.Id; executable = $pythonPath }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Start-Sleep -Milliseconds 500
    if (-not (Test-RecordedProcess $state.bridge)) { throw 'Proxy bridge exited; inspect bridge.err.log.' }
}

if (-not (Test-RecordedProcess $state.gateway)) {
    & $caddyPath validate --config $caddyConfig --adapter caddyfile
    if ($LASTEXITCODE -ne 0) { throw 'Invalid gateway configuration.' }
    $gateway = Start-Process -FilePath $caddyPath -ArgumentList @(
        'run', '--config', ('"{0}"' -f $caddyConfig), '--adapter', 'caddyfile'
    ) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $RuntimeDirectory 'gateway.out.log') -RedirectStandardError (Join-Path $RuntimeDirectory 'gateway.err.log')
    $state.gateway = @{ pid = $gateway.Id; executable = $caddyPath }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
    $ready = $false
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Milliseconds 300
        if (-not (Test-RecordedProcess $state.gateway)) { throw 'Gateway exited; inspect gateway.err.log.' }
        try {
            $gatewayHealth = Invoke-WebRequest -Uri 'http://127.0.0.1:8011/health' -UseBasicParsing -TimeoutSec 1
            if ($gatewayHealth.StatusCode -eq 200) { $ready = $true; break }
        } catch {
            continue
        }
    }
    if (-not $ready) { throw 'Application gateway did not become ready.' }
}

if (-not $GatewayOnly -and -not (Test-RecordedProcess $state.tunnel)) {
    if (-not (Test-Path -LiteralPath $tunnelConfig -PathType Leaf)) { throw 'Tunnel configuration missing.' }
    # cloudflared 不读取 HTTP_PROXY/HTTPS_PROXY，务必保持这一点：它只会直连 7844。
    # 本机直连 7844 能完成 TCP 握手，但 TLS 数据会被丢弃（报 "TLS handshake with edge error: i/o timeout"）。
    # 因此必须让流量在系统层被代理接管，也就是先打开 Clash Verge 的「虚拟网卡模式」（TUN）。
    # 提前拦住，免得等了半天只在 tunnel.err.log 里看到重试。
    $tunAdapter = Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -match 'Tunnel' -and $_.Name -match 'Meta|Mihomo|Clash' }
    if (-not $tunAdapter) {
        throw "TUN mode is not active. Turn on Clash Verge's 虚拟网卡模式 (virtual NIC / TUN) first; cloudflared cannot reach the edge without it."
    }
    $cloudflaredPath = (Get-Command cloudflared.exe -ErrorAction Stop).Source
    $tunnel = Start-Process -FilePath $cloudflaredPath -ArgumentList @(
        'tunnel', '--config', ('"{0}"' -f $tunnelConfig), '--no-autoupdate', 'run', '--dns-resolver-addrs', '127.0.0.1:15353'
    ) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $RuntimeDirectory 'tunnel.out.log') -RedirectStandardError (Join-Path $RuntimeDirectory 'tunnel.err.log')
    $state.tunnel = @{ pid = $tunnel.Id; executable = $cloudflaredPath }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8
    Start-Sleep -Milliseconds 500
    if (-not (Test-RecordedProcess $state.tunnel)) { throw 'Tunnel exited; inspect tunnel.err.log.' }
}

Write-Host 'Application gateway is running on 127.0.0.1:8011; account authentication is handled by VeriMind.'
if (-not $GatewayOnly) { Write-Host 'Tunnel process started; inspect tunnel.err.log for registered connections.' }

