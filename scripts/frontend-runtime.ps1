# 前端来源检查：HTTP 200 不能证明启动的是本项目的当前页面。
function Test-VerimindFrontendIdentity {
    param([object]$Identity, [string]$WebRoot, [string]$UiId)
    if (-not $Identity -or $Identity.schema -ne 'verimind_frontend_v1' -or $Identity.ui_id -ne $UiId) {
        return $false
    }
    if (-not $Identity.source_root) { return $false }
    try {
        $actual = [System.IO.Path]::GetFullPath([string]$Identity.source_root).TrimEnd('\', '/')
        $expected = [System.IO.Path]::GetFullPath($WebRoot).TrimEnd('\', '/')
        return [string]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Get-VerimindFrontendIdentity {
    param([int]$Port)
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/__verimind_frontend" -TimeoutSec 3
    } catch { return $null }
}

function Test-VerimindFrontendSource {
    param([int]$Port, [string]$WebRoot)
    # 仅供显式 -Restart 识别本目录的旧 Vite；其他项目的服务不自动停止。
    $identity = Get-VerimindFrontendIdentity $Port
    if ($identity -and $identity.source_root) {
        return Test-VerimindFrontendIdentity $identity $WebRoot ([string]$identity.ui_id)
    }
    try {
        $module = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/src/views/WorkspaceView.vue" -UseBasicParsing -TimeoutSec 3
        $expected = (Join-Path $WebRoot 'src/views/WorkspaceView.vue').Replace('\', '/')
        return $module.Content.Contains($expected)
    } catch { return $false }
}
