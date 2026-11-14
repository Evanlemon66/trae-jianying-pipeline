# UI 准入运行器：始终把 stdout/stderr 同时落盘到 pipeline_ui\logs\admission_<时间戳>.log
# 用法: powershell -ExecutionPolicy Bypass -File pipeline_ui\run_admission.ps1 -StopAfter locate
param(
    [string]$StopAfter = 'locate',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Python) {
    $Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    if (-not (Test-Path $Python)) { $Python = 'python' }
}
$logDir = Join-Path $here 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('admission_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '.log')

& $Python (Join-Path $here 'admission_test.py') --stop-after $StopAfter 2>&1 |
    Tee-Object -FilePath $log
$code = $LASTEXITCODE
"EXITCODE=$code  LOG=$log" | Tee-Object -FilePath $log -Append
exit $code
