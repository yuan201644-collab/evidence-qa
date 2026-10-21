param([Parameter(Mandatory=$true)][string]$ReportDirectory)
$ErrorActionPreference = 'Stop'
# 导出本目录下的报告为 PDF，并回填目录页的域与页码。
# 依赖本机 Word；若 Word COM 不可用，可改用同仓库的 LibreOffice 转换（见 页数预算核算_20261015.md）。
$reportRoot = (Resolve-Path -LiteralPath $ReportDirectory).Path
$officeInstance = New-Object -ComObject Word.Application
$officeInstance.Visible = $false
$officeInstance.DisplayAlerts = 0
try {
    foreach ($reportName in @('技术实现说明书_v0.1')) {
        $sourceDocument = Join-Path $reportRoot ($reportName + '.docx')
        $pdfDocument = Join-Path $reportRoot ($reportName + '.pdf')
        $reportDocument = $officeInstance.Documents.Open($sourceDocument, $false, $false)
        try {
            $reportDocument.Fields.Update() | Out-Null
            foreach ($contents in $reportDocument.TablesOfContents) { $contents.Update() }
            $reportDocument.Repaginate()
            foreach ($contents in $reportDocument.TablesOfContents) { $contents.UpdatePageNumbers() }
            $reportDocument.Save()
            $reportDocument.ExportAsFixedFormat($pdfDocument, 17)
            [pscustomobject]@{ report=$reportName; pages=$reportDocument.ComputeStatistics(2); pdf=$pdfDocument } | ConvertTo-Json -Compress
        } finally { $reportDocument.Close(0); [Runtime.InteropServices.Marshal]::ReleaseComObject($reportDocument) | Out-Null }
    }
} finally { $officeInstance.Quit(); [Runtime.InteropServices.Marshal]::ReleaseComObject($officeInstance) | Out-Null }
