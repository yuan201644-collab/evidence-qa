[CmdletBinding()]
param([ValidateSet('Enable', 'Disable')][string]$Action = 'Enable')

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]$identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell window.'
}
$hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$markerStart = '# VeriMind public-demo tunnel BEGIN'
$markerEnd = '# VeriMind public-demo tunnel END'
$lines = if (Test-Path -LiteralPath $hostsPath) { Get-Content -LiteralPath $hostsPath } else { @() }
$kept = [System.Collections.Generic.List[string]]::new()
$inside = $false
foreach ($line in $lines) {
    if ($line -eq $markerStart) { $inside = $true; continue }
    if ($line -eq $markerEnd) { $inside = $false; continue }
    if (-not $inside) { $kept.Add($line) }
}
if ($Action -eq 'Enable') {
    $kept.Add($markerStart)
    $kept.Add('127.0.0.1 region1.v2.argotunnel.com')
    $kept.Add('127.0.0.1 region2.v2.argotunnel.com')
    $kept.Add($markerEnd)
}
[System.IO.File]::WriteAllLines($hostsPath, [string[]]$kept, [System.Text.Encoding]::ASCII)
Clear-DnsClientCache
Write-Host "Hosts mapping $Action completed."
