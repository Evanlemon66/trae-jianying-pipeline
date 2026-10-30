param(
    [string]$DraftName,
    [string]$JyExe = ""
)

if (-not $JyExe) {
    # 剪映可执行文件：JY_JIANYING_DIR → %LOCALAPPDATA% 常见安装位置
    $candidates = @()
    if ($env:JY_JIANYING_DIR) { $candidates += (Join-Path $env:JY_JIANYING_DIR "JianyingPro.exe") }
    $candidates += (Join-Path $env:LOCALAPPDATA "JianyingPro\Apps\JianyingPro.exe")
    $candidates += (Join-Path $env:USERPROFILE "AppData\Local\JianyingPro\Apps\JianyingPro.exe")
    $JyExe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $JyExe) {
        Write-Host "[ERROR] JianyingPro.exe not found; pass -JyExe or set JY_JIANYING_DIR" -ForegroundColor Red
        exit 1
    }
}

$draftDir = if ($env:JY_PROJECTS_ROOT) {
    $env:JY_PROJECTS_ROOT
} else {
    Join-Path $env:LOCALAPPDATA "JianyingPro\User Data\Projects\com.lveditor.draft"
}

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
