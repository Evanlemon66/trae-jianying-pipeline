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

# python 把日志写到 stderr 时，PowerShell 会把它包成 ErrorRecord；在
# $ErrorActionPreference='Stop' 下这条记录会升级为终止错误，脚本在写入
# EXITCODE= 之前就退出（上一轮日志缺 EXITCODE 行的原因）。所以调用期间降级为
# Continue，并放进 try/finally 保证末尾一定写 EXITCODE。
$code = 1
try {
    $ErrorActionPreference = 'Continue'
    & $Python (Join-Path $here 'admission_test.py') --stop-after $StopAfter 2>&1 |
        Tee-Object -FilePath $log
    if ($null -ne $LASTEXITCODE) { $code = [int]$LASTEXITCODE }
} catch {
    "PIPELINE_ERROR=$($_.Exception.Message)" | Tee-Object -FilePath $log -Append
} finally {
    "EXITCODE=$code  LOG=$log" | Tee-Object -FilePath $log -Append
}
exit $code
