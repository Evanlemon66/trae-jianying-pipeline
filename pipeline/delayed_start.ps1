# delayed_start.ps1 — 延迟写入裁剪配置并拉起 watcher
#
# 用途：新剧进入前先把 watcher 停掉，等 30 分钟后（并确认 job.json 已生成）
#       把裁剪秒数写进 cover_job/export_config.json，再用 WMI 拉起 cover_watch。
#
# 裁剪落盘规则（严格对齐 lib/cover_watch.py 的 _effective_cut 与 apply_export_config）：
#   - 裁剪来源优先级：rev=0 时 export_config.json.cut_s > job.json workflow.cut_s > video.cut_s
#   - 因此这里只写 <剧目录>/cover_job/export_config.json 的 cut_s 字段（可被 watch 的
#     _read_export_config 读到；已有文件则保留其它键，只更新 cut_s）
#   - 不写 job.json：video.cut_s 由 cover_watch 的 apply_export_config 在 edit 阶段自行落盘
#
# 流程：
#   1) 等待 -DelayMinutes 分钟（默认 30）
#   2) 之后每分钟检查一次 <JobDir>\cover_job\job.json，最多再等 -WaitMinutes 分钟（默认 30）
#   3) 写 export_config.json（cut_s）
#   4) 按 supervisor 同一套 WMI 方式启动 watcher（Win32_Process.Create，脱离进程树）；
#      启动前先确认没有 cover_watch 进程在跑，有就只记日志跳过启动
#   5) 每一步带时间戳写 pipeline/_work/delayed_start.log
#
# 超时（默认共 60 分钟）仍无 job.json：不写配置、不启动 watcher，退出码 3。
#
# 退出码：0 = 正常（已启动 / watcher 已在跑）
#         1 = 异常（读配置或启动失败）
#         3 = 超时未等到 job.json（放弃写入与启动）
#
# 无人值守运行：不弹窗、不等待输入。脚本用 UTF-8（带 BOM）保存。

param(
    [int]$DelayMinutes = 30,
    [Parameter(Mandatory = $true)][string]$JobDir,
    [Parameter(Mandatory = $true)][double]$CutSeconds,
    [int]$WaitMinutes = 30
)

$ErrorActionPreference = 'Stop'

$scriptDir  = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$repoRoot   = Split-Path -Parent $scriptDir
$logDir     = Join-Path $repoRoot 'pipeline\_work'
$logFile    = Join-Path $logDir 'delayed_start.log'
$consoleLog = Join-Path $logDir 'watcher_console.log'
$watcherPy  = Join-Path $repoRoot 'lib\cover_watch.py'

# 隐藏本进程的控制台窗口（脚本要挂机 30 分钟以上，避免窗口长期留在桌面）
try {
    $sig = '[DllImport("user32.dll")] public static extern IntPtr GetConsoleWindow(); ' +
           '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
    $api = Add-Type -MemberDefinition $sig -Name 'TraeDelayedWinApi' -Namespace 'TraeWatcher' -PassThru -ErrorAction Stop
    $hwnd = $api::GetConsoleWindow()
    if ($hwnd -ne [IntPtr]::Zero) { $api::ShowWindow($hwnd, 0) | Out-Null }
} catch {
    # 隐藏失败不影响主逻辑
}

# 统一时间戳格式：全脚本的日志与时间显示都走这里，避免多处格式串写法不一致
function Format-Timestamp([datetime]$Time) {
    return $Time.ToString('yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
}

# 日志轮转：写之前检查大小，超过上限就 删 .3 → .2→.3 → .1→.2 → .log→.1，再新建空文件继续写
# 轮转失败（例如文件被占用）一律吞掉，继续写当前文件，不打断业务逻辑
function Invoke-LogRotation([string]$Path, [int64]$MaxBytes = 1048576, [int]$Keep = 3) {
    # 返回 $true = 正常（含无需轮转、文件不存在刚新建）；返回 $false = 改名失败（如被占用）
    try {
        if (-not (Test-Path -LiteralPath $Path)) {
            New-Item -ItemType File -Path $Path -Force | Out-Null
            return $true
        }
        if ((Get-Item -LiteralPath $Path).Length -lt $MaxBytes) { return $true }
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
        return $true
    } catch {
        # 轮转失败不抛出：原文件继续照常写，由调用方决定是否记日志
        return $false
    }
}

function Write-Log([string]$Message) {
    try {
        if (-not (Test-Path -LiteralPath $logDir)) {
            New-Item -ItemType Directory -Path $logDir -Force | Out-Null
        }
        Invoke-LogRotation -Path $logFile
        $line = '[{0}] {1}' -f (Format-Timestamp (Get-Date)), $Message
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
    } catch {
    }
}

# WMI 判定：进程名为 python*，且命令行包含 cover_watch（与 watcher_supervisor.ps1 同一套规则）
function Get-WatcherProcesses {
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop)
    } catch {
        return @()
    }
    return @($procs | Where-Object { $_.CommandLine -and $_.CommandLine -match 'cover_watch' })
}

# 按 supervisor 的同一套方式拉起 watcher（Win32_Process.Create）
function Start-Watcher {
    if (-not (Test-Path -LiteralPath $watcherPy)) {
        Write-Log "启动失败: 未找到脚本 $watcherPy"
        return 1
    }
    $pythonExe = $null
    foreach ($cand in @('python.exe', 'python')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { $pythonExe = $cmd.Source; break }
    }
    if (-not $pythonExe) {
        Write-Log '启动失败: PATH 中找不到 python'
        return 1
    }
    # 与手工启动一致：python -u "lib/cover_watch.py"
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

function Invoke-DelayedStart {
    $coverJob = Join-Path $JobDir 'cover_job'
    $jobJson  = Join-Path $coverJob 'job.json'
    $ecPath   = Join-Path $coverJob 'export_config.json'

    Write-Log ("进程启动: pid=" + $PID + " DelayMinutes=" + $DelayMinutes + " WaitMinutes=" + $WaitMinutes +
               " JobDir=" + $JobDir + " CutSeconds=" + $CutSeconds)

    if ($CutSeconds -le 0) {
        Write-Log ("参数错误: CutSeconds 必须为正数，实际 " + $CutSeconds)
        return 1
    }

    # 1) 先等 DelayMinutes 分钟
    $wakeAt = (Get-Date).AddMinutes($DelayMinutes)
    Write-Log ("开始等待 " + $DelayMinutes + " 分钟，预计 " + (Format-Timestamp $wakeAt) + " 开始检查")
    Start-Sleep -Seconds ([int]($DelayMinutes * 60))
    Write-Log '等待结束，开始检查 job.json'

    # 2) 每分钟检查一次 job.json，最多再等 WaitMinutes 分钟
    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    $found = $false
    $tryNo = 0
    while ($true) {
        $tryNo++
        if (Test-Path -LiteralPath $jobJson) {
            $found = $true
            break
        }
        if ((Get-Date) -ge $deadline) { break }
        Write-Log ("第 " + $tryNo + " 次检查: job.json 尚不存在（" + $jobJson + "）")
        Start-Sleep -Seconds 60
    }
    if (-not $found) {
        $total = $DelayMinutes + $WaitMinutes
        Write-Log ("超时: 启动后共 " + $total + " 分钟仍未生成 job.json（" + $jobJson +
                   "），放弃写入裁剪配置，也不启动 watcher（避免按默认不裁剪开跑）")
        return 3
    }
    Write-Log ("job.json 已就绪: " + $jobJson + "（第 " + $tryNo + " 次检查命中）")

    # 裁剪优先级提示：rev>0 时 workflow.cut_s 压过 export_config
    $rev = 0
    try {
        $job = Get-Content -LiteralPath $jobJson -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($job.workflow -and $job.workflow.intent_revision) { $rev = [int]$job.workflow.intent_revision }
    } catch {
        Write-Log ("读取 job.json 失败（仍继续写 export_config）: " + $_.Exception.Message)
    }
    if ($rev -gt 0) {
        Write-Log ("⚠️ job.json workflow.intent_revision=" + $rev + "（>0）：按 _effective_cut 规则 " +
                   "workflow.cut_s 优先，export_config.json 的 cut_s 可能不被采纳，需人工核对该剧意图")
    }

    # 3) 写 export_config.json：只动 cut_s，保留已有其它键（derive 等）
    try {
        $obj = $null
        if (Test-Path -LiteralPath $ecPath) {
            $obj = Get-Content -LiteralPath $ecPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($obj -isnot [System.Management.Automation.PSCustomObject]) {
                throw 'export_config.json 不是 JSON 对象，拒绝覆盖'
            }
            $obj | Add-Member -NotePropertyName cut_s -NotePropertyValue $CutSeconds -Force
        } else {
            $obj = [pscustomobject]@{ cut_s = $CutSeconds }
        }
        $json = $obj | ConvertTo-Json -Depth 5
        $tmp = $ecPath + '.tmp'
        [IO.File]::WriteAllText($tmp, $json + "`r`n", [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $tmp -Destination $ecPath -Force
        Write-Log ("已写入裁剪配置: " + $ecPath + " cut_s=" + $CutSeconds +
                   "（rev=0 时按 _effective_cut，export_config 优先于 workflow/video）")
    } catch {
        Write-Log ("写入 export_config.json 失败: " + $_.Exception.Message)
        return 1
    }

    # 4) 启动前必须确认没有 watcher 在跑；有就跳过启动只记日志
    $running = @(Get-WatcherProcesses)
    if ($running.Count -gt 0) {
        Write-Log ("watcher 已在运行: pid=" + $running[0].ProcessId + "（共 " + $running.Count +
                   " 个），跳过启动，仅记录")
        return 0
    }
    Write-Log '当前无 cover_watch 进程，开始按 WMI 方式启动 watcher'
    $code = Start-Watcher
    if ($code -eq 0) {
        Write-Log '延迟启动流程完成'
    } else {
        Write-Log '延迟启动流程失败（未能拉起 watcher）'
    }
    return $code
}

$exitCode = 1
try {
    $exitCode = Invoke-DelayedStart
} catch {
    Write-Log ("异常: " + $_.Exception.Message)
    $exitCode = 1
}

exit $exitCode
