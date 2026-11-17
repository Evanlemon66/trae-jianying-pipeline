<#
  Console 启动器（配合根目录「启动Console.bat」双击使用）
  - start          : 未运行则后台拉起服务，健康检查通过后打开浏览器
  - autostart-on   : 在「启动」文件夹放快捷方式（登录时静默起服务，不弹浏览器）
  - autostart-off  : 删除该快捷方式
  - status         : 打印服务与开机自启状态
#>
param(
    [ValidateSet('start', 'autostart-on', 'autostart-off', 'status')]
    [string]$Action = 'start',
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$Repo = Split-Path -Parent $PSScriptRoot
$Url = 'http://127.0.0.1:8765/'
$Health = $Url + 'api/health'
$LogDir = Join-Path $Repo 'pipeline_ui\_console\logs'
$LnkPath = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\TraeJianyingConsole.lnk'

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri $Health -UseBasicParsing -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Start-Console {
    if (Test-Health) { return $true }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
    if (-not $py) {
        Write-Host '[错误] 找不到 python，请先安装 Python 3.12 并加入 PATH。' -ForegroundColor Red
        return $false
    }
    Start-Process -FilePath $py -ArgumentList 'pipeline_ui\console\app.py' -WorkingDirectory $Repo -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'server.out.log') `
        -RedirectStandardError (Join-Path $LogDir 'server.err.log')
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Health) { return $true }
    }
    Write-Host ('[错误] 服务 20 秒内未就绪，请查看日志：' + $LogDir) -ForegroundColor Red
    return $false
}

switch ($Action) {
    'start' {
        if (Start-Console) {
            Write-Host ('Console 已就绪：' + $Url) -ForegroundColor Green
            if (-not $NoBrowser) { Start-Process $Url }
        } else { exit 1 }
    }
    'autostart-on' {
        $sh = New-Object -ComObject WScript.Shell
        $s = $sh.CreateShortcut($LnkPath)
        $s.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $s.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
                       (Join-Path $PSScriptRoot 'console_launcher.ps1') + '" -Action start -NoBrowser'
        $s.WorkingDirectory = $Repo
        $s.WindowStyle = 7
        $s.Description = 'Trae 剪映流水线 Console 登录自启'
        $s.Save()
        Write-Host '已开启登录自启（只起服务，不弹浏览器）。' -ForegroundColor Green
    }
    'autostart-off' {
        if (Test-Path $LnkPath) {
            Remove-Item $LnkPath -Force
            Write-Host '已关闭登录自启。' -ForegroundColor Green
        } else {
            Write-Host '当前未开启登录自启。' -ForegroundColor Yellow
        }
    }
    'status' {
        if (Test-Health) { Write-Host ('服务运行中：' + $Url) -ForegroundColor Green }
        else { Write-Host '服务未运行。' -ForegroundColor Yellow }
        if (Test-Path $LnkPath) { Write-Host '登录自启：已开启' }
        else { Write-Host '登录自启：未开启' }
    }
}
