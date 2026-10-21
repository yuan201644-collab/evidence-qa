[CmdletBinding()]
param([string]$RuntimeDirectory = (Join-Path $env:LOCALAPPDATA 'VeriMind/public-demo'))
$ErrorActionPreference = 'Stop'
$statePath = Join-Path $RuntimeDirectory 'processes.json'
if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
    Write-Host 'No demo processes recorded.'
    exit 0
}
$state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
foreach ($key in @('tunnel', 'gateway', 'bridge')) {
    $record = $state.$key
    if (-not $record) { continue }
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $record.executable) {
        Stop-Process -Id $record.pid -ErrorAction Stop
        Write-Host "$key stopped."
    }
    $state.$key = $null
}
$state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8

