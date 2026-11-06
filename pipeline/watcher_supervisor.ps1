# watcher_supervisor.ps1 — cover_watch 守护脚本（单实例 + WMI 拉起）
#
# 用途：计划任务 TraeDramaWatcher 周期性调用本脚本，保证 lib/cover_watch.py 常驻。
#   1) 命名互斥体做单实例保护：已有 supervisor 在跑就直接退出，避免叠加。
#   2) WMI（Win32_Process）按「进程名是 python* 且命令行含 cover_watch」判定是否已在跑。
#   3) 未在跑则用 Win32_Process.Create 启动：只有这种方式能脱离调用方进程树，
#      不会被 Job Object（KILL_ON_JOB_CLOSE）连带杀掉（Start-Process 会）。
#   4) 每轮向 pipeline/_work/watcher_supervisor.log 追加一行带时间戳的记录。
#
# 说明：cover_watch.py 没有 --watch 开关，持续监听就是「直接运行脚本」，
#       监听参数（roots/settle/interval/export_policy 等）来自 pipeline/presets.json 的 watch 段。
#
# 退出码：0 = 已启动 / 已在运行（正常）
#         1 = 启动失败（Win32_Process.Create 失败或抛出异常）
#         2 = 环境缺失（找不到 python 或找不到 cover_watch.py）
#
# 本脚本无人值守运行：不弹窗、不等待输入；用 UTF-8 编码保存。

$ErrorActionPreference = 'Stop'

$scriptDir  = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repoRoot   = Split-Path -Parent $scriptDir
$logDir     = Join-Path $repoRoot 'pipeline\_work'
$logFile    = Join-Path $logDir 'watcher_supervisor.log'
$consoleLog = Join-Path $logDir 'watcher_console.log'
$watcherPy  = Join-Path $repoRoot 'lib\cover_watch.py'

# 隐藏本进程的控制台窗口（计划任务每 5 分钟唤起一次，避免窗口闪烁）
try {
    $sig = '[DllImport("user32.dll")] public static extern IntPtr GetConsoleWindow(); ' +
           '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
    $api = Add-Type -MemberDefinition $sig -Name 'TraeWatcherWinApi' -Namespace 'TraeWatcher' -PassThru -ErrorAction Stop
    $hwnd = $api::GetConsoleWindow()
    if ($hwnd -ne [IntPtr]::Zero) { $api::ShowWindow($hwnd, 0) | Out-Null }
} catch {
    # 隐藏失败不影响主逻辑
}

# 日志轮转：写之前检查大小，超过上限就 删 .3 → .2→.3 → .1→.2 → .log→.1，再新建空文件继续写
# 轮转失败（例如文件被占用）一律吞掉，继续写当前文件，不打断业务逻辑
function Invoke-LogRotation([string]$Path, [int64]$MaxBytes = 1048576, [int]$Keep = 3) {
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            New-Item -ItemType File -Path $Path -Force | Out-Null
            return
        }
        if ((Get-Item -LiteralPath $Path).Length -lt $MaxBytes) { return }
        $oldest = $Path + '.' + $Keep
        if (Test-Path -LiteralPath $oldest) { Remove-Item -LiteralPath $oldest -Force -ErrorAction Stop }
        for ($i = $Keep - 1; $i -ge 1; $i--) {
            $src = $Path + '.' + $i
            if (Test-Path -LiteralPath $src) {
                Move-Item -LiteralPath $src -Destination ($Path + '.' + ($i + 1)) -Force -ErrorAction Stop
            }
        }
        Move-Item -LiteralPath $Path -Destination ($Path + '.1') -Force -ErrorAction Stop
        New-Item -ItemType File -Path $Path -Force | Out-Null
    } catch {
        # 轮转失败不抛出：下一行日志仍写原文件
    }
}

# 日志：目录不存在自动创建；写失败吞掉，不阻断守护流程
function Write-Log([string]$Message) {
    try {
        if (-not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        Invoke-LogRotation -Path $logFile
        $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch {
    }
}

# WMI 判定：进程名为 python*，且命令行包含 cover_watch（不只看进程名）
function Get-WatcherProcesses {
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop)
    } catch {
        return @()
    }
    return @($procs | Where-Object { $_.CommandLine -and $_.CommandLine -match 'cover_watch' })
}

function Invoke-Supervisor {
    # 日志目录先行创建（同时给 watcher 的 stdout 重定向准备落点）
    try {
        if (-not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
    } catch {
    }

    if (-not (Test-Path -LiteralPath $watcherPy)) {
        Write-Log "启动失败: 未找到脚本 $watcherPy"
        return 2
    }

    $pythonExe = $null
    foreach ($cand in @('python.exe', 'python')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { $pythonExe = $cmd.Source; break }
    }
    if (-not $pythonExe) {
        Write-Log '启动失败: PATH 中找不到 python（计划任务环境变量可能不完整）'
        return 2
    }

    $running = @(Get-WatcherProcesses)
    if ($running.Count -gt 0) {
        Write-Log ("已在运行: pid=" + $running[0].ProcessId + "（共 " + $running.Count + " 个 cover_watch 进程），本次不重复启动")
        return 0
    }

    # cmd /c 包一层只为把 watcher 的输出落到 watcher_console.log；
    # 真正的 python 子进程命令行仍含 cover_watch，后续 WMI 判定照常能识别。
    # 与手工启动一致：python -u "lib/cover_watch.py"（-u 让输出即时落到日志）
    $cmdLine = 'cmd.exe /d /s /c ""{0}" -u "{1}" >> "{2}" 2>&1"' -f $pythonExe, $watcherPy, $consoleLog
    try {
        $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
                -Arguments @{ CommandLine = $cmdLine; CurrentDirectory = $repoRoot } -ErrorAction Stop
    } catch {
        Write-Log ("启动失败: 调用 Win32_Process.Create 异常: " + $_.Exception.Message)
        return 1
    }
    if ($res.ReturnValue -ne 0) {
        Write-Log ("启动失败: Win32_Process.Create 返回值 " + $res.ReturnValue)
        return 1
    }

    Start-Sleep -Seconds 1
    $started = @(Get-WatcherProcesses)
    if ($started.Count -gt 0) {
        Write-Log ("启动 watcher: pid=" + $started[0].ProcessId + "（wrapper pid=" + $res.ProcessId + "）")
    } else {
        Write-Log ("启动 watcher: 已下发（wrapper pid=" + $res.ProcessId + "），1 秒内未观测到 python 进程，详见 watcher_console.log")
    }
    return 0
}

$exitCode = 1
$mutex = New-Object System.Threading.Mutex($false, 'Local\TraeDramaWatcherSupervisor')
$acquired = $false
try {
    try {
        $acquired = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        # 上一个 supervisor 异常退出未释放：互斥体被本进程接管，继续正常流程
        $acquired = $true
    }
    if (-not $acquired) {
        Write-Log '已在运行: 另一个 supervisor 持有互斥体，本次直接退出'
        $exitCode = 0
    } else {
        $exitCode = Invoke-Supervisor
    }
} catch {
    Write-Log ("异常: " + $_.Exception.Message)
    $exitCode = 1
} finally {
    if ($acquired) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    try { $mutex.Dispose() } catch { }
}

exit $exitCode
