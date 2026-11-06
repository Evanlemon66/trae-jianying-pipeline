# watcher_stall.ps1 — 心跳卡死判定纯函数
#
# 只定义函数，不执行主流程；供 watcher_supervisor.ps1 dot-source，
# 也供 tests/test_watcher_stall.ps1 单独加载做回归。
#
# 判定输入：watcher_heartbeat.json（lib/cover_watch.py 每 30s 原子写）。
# 判定输出：noheartbeat | unparsable | pidmismatch | startup | healthy | stalled
#
# 误杀保护：
#   1) 心跳文件不存在 / 不可解析 → 不判卡死（旧版本或刚启动）；
#   2) 心跳里的 pid 与当前运行进程不一致 → 不判卡死（旧实例残留）；
#   3) 进程启动后 StartupGraceSeconds 内 → 不判卡死（给启动余量）。

function Get-HeartbeatTimestamp {
    param([object]$Heartbeat)
    if ($null -eq $Heartbeat) { return $null }

    # 优先 epoch 秒（无时区解析歧义）
    $epoch = $Heartbeat.updated_ts
    if ($null -ne $epoch) {
        try { return [double]$epoch } catch { }
    }

    # 回退 ISO 字符串（state_io 写入的 updated_at）
    $iso = $Heartbeat.updated_at
    if ($null -ne $iso -and "$iso".Trim().Length -gt 0) {
        try {
            return [double]([datetimeoffset]::Parse(
                [string]$iso,
                [System.Globalization.CultureInfo]::InvariantCulture).ToUnixTimeSeconds())
        } catch { }
    }
    return $null
}

function Get-WatchStallSeconds {
    param(
        [string]$RepoRoot,
        [object]$Override,
        [int]$Default = 900
    )
    if ($null -ne $Override) {
        try { return [int]$Override } catch { }
    }
    if ($RepoRoot) {
        $presets = Join-Path $RepoRoot 'pipeline\presets.json'
        try {
            if (Test-Path -LiteralPath $presets) {
                $cfg = Get-Content -LiteralPath $presets -Raw -Encoding UTF8 | ConvertFrom-Json
                $watch = $cfg.watch
                if ($null -ne $watch -and $null -ne $watch.stall_heartbeat_s) {
                    return [int]$watch.stall_heartbeat_s
                }
            }
        } catch {
            # 配置不可读 → 用默认值，不阻断 supervisor
        }
    }
    return $Default
}

function Get-WatcherStallDecision {
    param(
        [object]$Heartbeat,
        [long]$NowEpoch,
        [int]$StallSeconds = 900,
        [int]$StartupGraceSeconds = 180,
        [long]$RunningPid = 0
    )

    if ($null -eq $Heartbeat) { return 'noheartbeat' }

    $ts = Get-HeartbeatTimestamp -Heartbeat $Heartbeat
    if ($null -eq $ts) { return 'unparsable' }

    # 心跳必须属于当前正在运行的进程，否则是旧实例残留，不能据此杀新进程
    if ($RunningPid -gt 0) {
        $hbPid = $Heartbeat.pid
        if ($null -eq $hbPid) { return 'pidmismatch' }
        try {
            if ([long]$hbPid -ne $RunningPid) { return 'pidmismatch' }
        } catch {
            return 'unparsable'
        }
    }

    # 启动余量：以心跳自带的 started_ts 为准（避免 CIM CreationDate 时区歧义）
    $started = $Heartbeat.started_ts
    if ($null -ne $started) {
        try {
            if (($NowEpoch - [long]$started) -lt $StartupGraceSeconds) {
                return 'startup'
            }
        } catch { }
    }

    if (($NowEpoch - [long]$ts) -gt $StallSeconds) { return 'stalled' }
    return 'healthy'
}
