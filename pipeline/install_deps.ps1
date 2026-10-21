Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  JianYing Pipeline Install Deps" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/3] Installing ffmpeg ..." -ForegroundColor Yellow
winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
Write-Host ""

Write-Host "[2/3] Installing Python packages ..." -ForegroundColor Yellow
pip install faster-whisper demucs psutil
Write-Host ""

Write-Host "[3/3] Verify ..." -ForegroundColor Yellow
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ff) { Write-Host "  ffmpeg : OK ($($ff.Source))" -ForegroundColor Green }
else { Write-Host "  ffmpeg : MISSING (reopen terminal)" -ForegroundColor Red }

foreach ($p in @("faster_whisper", "demucs", "psutil")) {
    $code = @"
import importlib.util
print(importlib.util.find_spec('$p') is not None)
"@
    $ok = python -c $code 2>$null
    if ($ok -eq "True") { Write-Host "  $p : OK" -ForegroundColor Green }
    else { Write-Host "  $p : MISSING" -ForegroundColor Red }
}

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
