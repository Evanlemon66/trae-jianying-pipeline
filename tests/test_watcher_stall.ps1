# -*- coding: utf-8 -*-
# 回归用例：watcher_stall.ps1 的卡死判定纯函数。
#
# 运行：powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_watcher_stall.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $root 'pipeline\watcher_stall.ps1')

$script:fails = @()

function Check([string]$Name, [bool]$Cond, [string]$Extra = '') {
    if ($Cond) {
        Write-Host "  OK   $Name"
    } else {
        Write-Host "  FAIL $Name  [$Extra]"
        $script:fails += $Name
    }
}

$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

# ① 心跳过期 → stalled
$hb = [pscustomobject]@{ pid = 123; updated_ts = $now - 2000; started_ts = $now - 3000 }
Check '心跳过期 → stalled' ((Get-WatcherStallDecision -Heartbeat $hb -NowEpoch $now `
    -StallSeconds 900 -StartupGraceSeconds 180 -RunningPid 123) -eq 'stalled')

# ② 心跳新鲜 → healthy
$hb = [pscustomobject]@{ pid = 123; updated_ts = $now - 10; started_ts = $now - 3000 }
Check '心跳新鲜 → healthy' ((Get-WatcherStallDecision -Heartbeat $hb -NowEpoch $now `
    -StallSeconds 900 -StartupGraceSeconds 180 -RunningPid 123) -eq 'healthy')

# ③ 心跳缺失 → noheartbeat（不判卡死）
Check '心跳缺失 → noheartbeat' ((Get-WatcherStallDecision -Heartbeat $null -NowEpoch $now `
    -StallSeconds 900 -StartupGraceSeconds 180 -RunningPid 123) -eq 'noheartbeat')

# ④ 刚启动（心跳很旧但 started_ts 在余量内）→ startup（不判卡死）
$hb = [pscustomobject]@{ pid = 123; updated_ts = $now - 2000; started_ts = $now - 30 }
Check '刚启动 → startup' ((Get-WatcherStallDecision -Heartbeat $hb -NowEpoch $now `
    -StallSeconds 900 -StartupGraceSeconds 180 -RunningPid 123) -eq 'startup')

# ⑤ 心跳 pid 与运行进程不一致 → pidmismatch（不判卡死）
$hb = [pscustomobject]@{ pid = 111; updated_ts = $now - 2000; started_ts = $now - 3000 }
Check 'pid 不一致 → pidmismatch' ((Get-WatcherStallDecision -Heartbeat $hb -NowEpoch $now `
    -StallSeconds 900 -StartupGraceSeconds 180 -RunningPid 222) -eq 'pidmismatch')

# ⑥ 时间戳不可解析 → unparsable（不判卡死）
$hb = [pscustomobject]@{ pid = 123; updated_at = 'not-a-date' }
Check '时间戳不可解析 → unparsable' ((Get-WatcherStallDecision -Heartbeat $hb -NowEpoch $now `
    -StallSeconds 900 -StartupGraceSeconds 180 -RunningPid 123) -eq 'unparsable')

# ⑦ 配置阈值：Override 优先；无 Override 时回落默认 900
Check '阈值 Override 生效' ((Get-WatchStallSeconds -RepoRoot '' -Override 60 -Default 900) -eq 60)
Check '阈值缺失回落默认' ((Get-WatchStallSeconds -RepoRoot '' -Override $null -Default 900) -eq 900)

if ($script:fails.Count -gt 0) {
    Write-Host "`nFAILED: $($script:fails.Count) 项 → $($script:fails -join ', ')"
    exit 1
}
Write-Host "`nALL PASS"
exit 0
