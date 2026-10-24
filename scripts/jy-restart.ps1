param(
    [string]$DraftName,
    [string]$JyExe = "D:\剪映5.9\剪映5.9版+jianying-skill\剪映5.9(win+mac)\剪 映5.9Windows\JianyingPro\5.9.0.11632\JianyingPro.exe"
)

$draftDir = Join-Path $env:USERPROFILE "AppData\Local\JianyingPro\User Data\Projects\com.lveditor.draft"

if (-not (Test-Path $draftDir)) {
    Write-Host "[ERROR] draft dir not found: $draftDir" -ForegroundColor Red
    exit 1
}

if ($DraftName) {
    $target = Get-ChildItem $draftDir -Directory | Where-Object { $_.Name -like "*$DraftName*" } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $target) { Write-Host "[ERROR] draft '$DraftName' not found" -ForegroundColor Red; exit 1 }
    $targets = @($target)
} else {
    $targets = Get-ChildItem $draftDir -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 3
}

foreach ($d in $targets) {
    $json = Join-Path $d.FullName "draft_content.json"
    $bak  = Join-Path $d.FullName "draft_content.json.bak"
    if ((Test-Path $bak) -and (Test-Path $json)) {
        $bakSize = (Get-Item $bak).Length
        $jsonSize = (Get-Item $json).Length
        if ($bakSize -gt $jsonSize) {
            Write-Host "[RESTORE] $($d.Name) — json=$jsonSize bak=$bakSize" -ForegroundColor Yellow
            Copy-Item $bak $json -Force
        }
    }
}

Write-Host "[KILL] JianyingPro.exe ..." -ForegroundColor Cyan
Get-Process JianyingPro -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 800

Write-Host "[START] $JyExe" -ForegroundColor Green
Start-Process $JyExe
Write-Host "[DONE]" -ForegroundColor Green
