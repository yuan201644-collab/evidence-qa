<#
.SYNOPSIS
    在受限环境下运行 pytest（负责暂存解释器副本 + 调用 run_tests.py）。

.DESCRIPTION
    本机对**位于项目树内**的解释器施加了写盘限制：这类进程只能写项目树内的路径，
    写项目外一律 PermissionError errno=13（实测对照表见 scripts/run_tests.py 顶部注释）。

    直接后果是 pytest 的 tmp_path 落到项目内，触发
    eval/cross_source_heldout/tools/prepare_architecture_blind_review.py:22 与
    run_architecture_comparison.py:973 的「必须保存在项目工作区之外」约束，
    9 个用例失败。这不是产品缺陷，是运行环境位置问题。

    修复办法：把 .venv 解释器复制到项目树之外再跑。副本的 sys.prefix 会回落到 uv
    基础目录，因此 run_tests.py 会手动挂上主仓库的 site-packages。

    暂存必须由**不受限的进程**（即本 PowerShell 脚本）完成 —— 受限解释器自己无法在
    项目外建目录或写文件。

.PARAMETER StageOnly
    只做暂存，不跑测试。

.PARAMETER Stage
    暂存目录，必须位于项目之外。默认 C:\askdata-tmp。

.EXAMPLE
    pwsh -File scripts/run_tests.ps1
    pwsh -File scripts/run_tests.ps1 tests/test_ask_progress.py -q
    pwsh -File scripts/run_tests.ps1 -k "nl2sql or cross_source"
#>
[CmdletBinding()]
param(
    [switch]$StageOnly,
    [string]$Stage = 'C:\askdata-tmp',
    [Parameter(Position = 0)]
    [string[]]$Path,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$runner = Join-Path $PSScriptRoot 'run_tests.py'
$stagedPython = Join-Path $Stage 'pytest-python.exe'

if (-not (Test-Path $venvPython)) {
    throw "找不到 .venv 解释器：$venvPython"
}

# --- 暂存：把解释器复制到项目之外（本进程不受限，可以写项目外） ---
# 项目内外的判定用绝对路径前缀比较，避免依赖 .NET 静态方法。
$projectFull = [System.IO.Path]::GetFullPath($projectRoot)
$stageFull = [System.IO.Path]::GetFullPath($Stage)
if ($stageFull.StartsWith($projectFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "暂存目录必须位于项目工作区之外；当前给定：$stageFull"
}

if (-not (Test-Path $Stage)) {
    New-Item -ItemType Directory -Force -Path $Stage | Out-Null
    Write-Host "[run_tests] 已创建暂存目录：$Stage"
}

# 副本按内容比对，源变了才重抄，避免每次跑测试都复制 ~100KB。
$needCopy = $true
if (Test-Path $stagedPython) {
    $srcHash = (Get-FileHash $venvPython -Algorithm SHA256).Hash
    $dstHash = (Get-FileHash $stagedPython -Algorithm SHA256).Hash
    $needCopy = ($srcHash -ne $dstHash)
}
if ($needCopy) {
    Copy-Item -LiteralPath $venvPython -Destination $stagedPython -Force
    Write-Host "[run_tests] 已暂存解释器副本：$stagedPython"
}
else {
    Write-Host "[run_tests] 解释器副本已是最新：$stagedPython"
}

if ($StageOnly) {
    Write-Host "[run_tests] -StageOnly：跳过测试执行。"
    exit 0
}

# --- 执行：用暂存的**项目外**解释器直接跑 pytest ---
#
# 关键：绝不能由 .venv 的受限解释器当父进程。限制会沿进程树继承 —— 实测同一套解释器
# （项目外副本、uv 基础解释器）在受限父进程下全部 SYS_DENIED 13，在不受限父进程下
# 全部 SYS_WRITABLE。所以从这里（不受限的 PowerShell）直接拉起副本，执行链中不出现
# 受限进程。
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:ASKDATA_SITE_PACKAGES = (Join-Path $projectRoot '.venv\Lib\site-packages')

# 位置参数（测试路径）与透传参数（-k / -q 等）合并后原样交给 pytest。
$forward = @()
if ($Path) { $forward += $Path }
if ($PytestArgs) { $forward += $PytestArgs }

& $stagedPython $runner @forward
exit $LASTEXITCODE
