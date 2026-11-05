# -*- coding: utf-8 -*-
"""export_state.py — 导出状态机「单一真源」（cover_job/.export_state.json）。

背景：导出状态此前分散在三处，互相打架、无法判定：
  1. job.json 的 video.status / video.export_file / video.cut_s
     —— video.status 实际从未被写过（死字段），真正"已导出"信号是 stage=video_ready；
  2. 导出进度侧写 <export_dir>/<剧名>.progress.json（robust_auto_export 每 30s 刷新）；
  3. 重试计数 cover_job/.export_retry.json（fails / paused / next_ts）。

本模块把「是否已导、失败几次、是否暂停、进度到哪」收敛为一份可判定、幂等、可恢复
的真源文件 `.export_state.json`，与 `.watcher.lock` / `.export_retry.json` 同级。

唯一写者
  本文件（lib/export_state.py）是 .export_state.json 的唯一写者，且只由
  cover_watch 的导出链路调用。剪映导出子进程（robust_auto_export）、
  job_manifest、series_runner 均不写它。
  派生约定：job.json 的 video.status 是展示/契约字段，由本真源派生（cover_watch
  在 succeed 后写 "done"），任何地方不得绕过本模块直接改 video.status。

原子性 / 幂等
  - 写入走 .tmp + os.replace，崩溃也不会留下半截文件。
  - succeed() 幂等：已 done 再调用不变；原子写同一内容不产生副作用。
  - 不假设优雅退出：崩溃恢复靠 reap（见下）。

状态机（取值 / 谁写 / 何时写 / 下一步允许）
  pending    迁移或初始化（无失败记录）            → exporting
  exporting  begin()：run_export 启动导出前         → done / retrying / paused /（stale→retrying）
  retrying   fail()：失败未超阈值；reap：stale 回收 → exporting（退避结束）/ paused
  paused     fail()：连续失败 ≥ pause_after        → exporting（仅 --phase export 人工放行）
  done       succeed()：导出成功（record_video 后）  终态；重导需人工删本文件或 --phase export
  failed     预留：不可重试的硬错误（失败分类，第 3 项，当前无写入路径）→ exporting（人工）

崩溃恢复（不假设优雅退出）
  status==exporting 且心跳超过 STALE_AFTER_S 无更新 → 判定 stale，回收到 retrying。
  心跳 = max(heartbeat_ts, progress_mtime)：heartbeat_ts 由本模块在 begin/heartbeat
  刷新；progress_mtime 来自导出子进程的 .progress.json（每 30s 刷新）。只要子进程还
  在写盘就不会被误回收；进程死了两者都停摆，超时即回收。

兼容迁移（一次性，优先级从高到低）
  真源缺失时按旧数据推断并落盘，原 .export_retry.json 备份为 .export_retry.json.bak：
    1. job.json video.status=="done"（或 stage==video_ready）→ done（已导出事实不可回退）
    2. .export_retry.json paused==true                        → paused
    3. .export_retry.json fails>0                             → retrying（带计数与退避点）
    4. .progress.json status==running 且 mtime 新鲜           → exporting（交给 reap 判定）
    5. 其余                                                   → pending

预留接口（第 2/3 项，本轮不实现）
  - phase：cold_start / export / verify，供「实例生命周期统一」标注当前子阶段；
  - failure_class：先恒为 "unknown"，供「失败分类与自愈策略表」落点。
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

import state_io as _sio  # 原子写/幂等落盘/version 的公共实现

STATE_NAME = ".export_state.json"
LEGACY_RETRY_NAME = ".export_retry.json"

STATUSES = ("pending", "exporting", "retrying", "paused", "done", "failed")
# 退避秒数：1 / 2 / 4 / 8 / 16 / 30 分钟（与旧 .export_retry.json 语义一致）
BACKOFF_S = (60, 120, 240, 480, 960, 1800)
DEFAULT_PAUSE_AFTER = 5
STALE_AFTER_S = 180  # exporting 心跳超过 3 分钟无更新 → 判定 stale
_PROGRESS_FRESH_S = 240  # 迁移时 .progress.json 视为"新鲜"的窗口


def state_path(job_dir: str) -> str:
    return os.path.join(job_dir, STATE_NAME)


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else _now()))


def _default() -> dict[str, Any]:
    return {
        "version": 1,
        "status": "pending",
        "attempts": 0,
        "consecutive_failures": 0,
        "last_error": None,
        "next_retry_at": 0,          # epoch；0 表示立即可重试
        "progress": None,            # 0-100 整数或 None
        "heartbeat_ts": 0,           # epoch 心跳（与 .progress.json 取大者判活）
        "updated_at": None,          # 人类可读的最近写入时间
        "writer": None,              # 最近写入者 pid
        "phase": None,               # 预留：cold_start / export / verify
        "failure_class": "unknown",  # 预留：失败分类（第 3 项）
        "last_recovery_action": None,  # 最近一次自愈动作（soft_home/kill_restart/dismiss/dismiss_then_kill）
        "last_recovery_at": None,      # 最近一次自愈动作时间（ISO）
    }


def _atomic_write(job_dir: str, data: dict[str, Any]) -> None:
    _sio.atomic_write(job_dir, STATE_NAME, data)


def _read_raw(job_dir: str) -> Optional[dict[str, Any]]:
    return _sio.read_raw(job_dir, STATE_NAME)


def _save_if_changed(job_dir: str, new: dict[str, Any]) -> dict[str, Any]:
    """幂等落盘：与现内容一致则跳过（不刷 mtime、不产生副作用）。"""
    return _sio.save_if_changed(job_dir, STATE_NAME, new)


# ── 心跳 / 存活判定 ──────────────────────────────────────────────

def _last_beat(st: dict[str, Any], progress_mtime: float) -> float:
    return max(float(st.get("heartbeat_ts") or 0), float(progress_mtime or 0))


def _is_stale_exporting(st: dict[str, Any], progress_mtime: float,
                        now: float, stale_after: float) -> bool:
    if st.get("status") != "exporting":
        return False
    return (now - _last_beat(st, progress_mtime)) > stale_after


def _effective(st: dict[str, Any], progress_mtime: float = 0.0,
               now: Optional[float] = None,
               stale_after: float = STALE_AFTER_S) -> dict[str, Any]:
    """返回应用了 stale 回收后的状态（只在内存里推导，不落盘）。"""
    now = now if now is not None else _now()
    if _is_stale_exporting(st, progress_mtime, now, stale_after):
        st = dict(st)
        st["status"] = "retrying"
        st["next_retry_at"] = 0  # stale 回收后立即可重试
    return st


# ── 迁移 ─────────────────────────────────────────────────────────

def parse_percent(raw: Any) -> Optional[int]:
    """从 .progress.json 的 percent（'约 42%' / '100%' / None）提取整数。"""
    if raw is None:
        return None
    m = re.search(r"(\d+)", str(raw))
    if not m:
        return None
    return max(0, min(100, int(m.group(1))))


def _read_legacy_retry(job_dir: str) -> dict[str, Any]:
    try:
        with open(os.path.join(job_dir, LEGACY_RETRY_NAME), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _backup_legacy_retry(job_dir: str) -> None:
    src = os.path.join(job_dir, LEGACY_RETRY_NAME)
    bak = src + ".bak"
    if os.path.isfile(src) and not os.path.isfile(bak):
        try:
            with open(src, encoding="utf-8") as f:
                content = f.read()
            with open(bak, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            pass


def migrate(job_dir: str, *, video_status: Optional[str] = None,
            stage: Optional[str] = None, progress_status: Optional[str] = None,
            progress_mtime: float = 0.0, progress_pct: Optional[int] = None,
            now: Optional[float] = None) -> dict[str, Any]:
    """真源缺失时按旧数据一次性推断并落盘。优先级见模块 docstring。

    入参由调用方（cover_watch）从 job.json / .progress.json 读好后传入，
    本模块不反向依赖 job_manifest / pipeline，保持单向。
    """
    now = now if now is not None else _now()
    st = _default()
    legacy = _read_legacy_retry(job_dir)
    fails = int(legacy.get("fails") or 0)

    if video_status == "done" or stage == "video_ready":
        st["status"] = "done"
    elif legacy.get("paused"):
        st["status"] = "paused"
        st["consecutive_failures"] = fails
        st["attempts"] = fails
    elif fails > 0:
        st["status"] = "retrying"
        st["consecutive_failures"] = fails
        st["attempts"] = fails
        st["next_retry_at"] = float(legacy.get("next_ts") or 0)
    elif progress_status == "running" and progress_mtime \
            and (now - progress_mtime) <= _PROGRESS_FRESH_S:
        st["status"] = "exporting"
        st["heartbeat_ts"] = progress_mtime
    else:
        st["status"] = "pending"

    if progress_pct is not None:
        st["progress"] = progress_pct
    st["heartbeat_ts"] = st["heartbeat_ts"] or now
    _backup_legacy_retry(job_dir)
    _atomic_write(job_dir, st)
    return st


# ── 读取 ─────────────────────────────────────────────────────────

def load(job_dir: str, *, progress_mtime: float = 0.0, migrate_ctx: Optional[dict[str, Any]] = None,
         now: Optional[float] = None) -> dict[str, Any]:
    """读真源；缺失时用 migrate_ctx 迁移。返回应用了 stale 推导的有效状态（不落盘回收）。

    migrate_ctx 可选键：video_status / stage / progress_status / progress_pct。
    """
    st = _read_raw(job_dir)
    if st is None:
        ctx = migrate_ctx or {}
        st = migrate(job_dir,
                     video_status=ctx.get("video_status"),
                     stage=ctx.get("stage"),
                     progress_status=ctx.get("progress_status"),
                     progress_mtime=progress_mtime,
                     progress_pct=ctx.get("progress_pct"),
                     now=now)
    return _effective(st, progress_mtime, now=now)


def reap_stale(job_dir: str, *, progress_mtime: float = 0.0,
               stale_after: float = STALE_AFTER_S,
               now: Optional[float] = None) -> bool:
    """崩溃恢复：exporting 且心跳超时 → 落盘回收到 retrying。返回是否发生回收。"""
    st = _read_raw(job_dir)
    if st is None:
        return False
    now = now if now is not None else _now()
    if not _is_stale_exporting(st, progress_mtime, now, stale_after):
        return False
    st = dict(st)
    st["status"] = "retrying"
    st["next_retry_at"] = 0
    st["last_error"] = (st.get("last_error") or "") and st.get("last_error") or \
        "心跳超时，判定上次导出崩溃（stale 回收）"
    _save_if_changed(job_dir, st)
    return True


# ── 状态转移 ─────────────────────────────────────────────────────

def begin(job_dir: str, *, phase: Optional[str] = None,
          now: Optional[float] = None) -> dict[str, Any]:
    """pending/retrying/paused → exporting。已在 exporting 时只刷心跳（幂等，不重复计次）。"""
    now = now if now is not None else _now()
    st = _read_raw(job_dir) or _default()
    if st.get("status") != "exporting":
        st["attempts"] = int(st.get("attempts") or 0) + 1
    st["status"] = "exporting"
    st["heartbeat_ts"] = now
    st["next_retry_at"] = 0
    if phase:
        st["phase"] = phase
    return _save_if_changed(job_dir, st)


def heartbeat(job_dir: str, *, progress: Optional[int] = None,
              phase: Optional[str] = None, now: Optional[float] = None) -> None:
    """导出进行中刷新心跳（幂等）。仅处于 exporting 时生效。"""
    now = now if now is not None else _now()
    st = _read_raw(job_dir)
    if not st or st.get("status") != "exporting":
        return
    st = dict(st)
    st["heartbeat_ts"] = now
    if progress is not None:
        st["progress"] = max(0, min(100, int(progress)))
    if phase:
        st["phase"] = phase
    _save_if_changed(job_dir, st)


def record_recovery(job_dir: str, action: Optional[str], *,
                    now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """记录最近一次自愈动作（由 cover_watch 从 progress sidecar 读回后经本接口写入）。

    唯一写者仍是本模块；本函数只存**最近一次**（不做历史数组，避免真源膨胀）。
    兼容：action 为 None/空（sidecar 无该字段，旧版本产物）时不写、不报错；
    幂等：重复写入同一 action 不重写。真源尚不存在时不创建（返回 None）。
    """
    if not action:
        return _read_raw(job_dir)
    st = _read_raw(job_dir)
    if st is None:
        return None
    if st.get("last_recovery_action") == action:
        return st  # 幂等：同一动作不重写
    now = now if now is not None else _now()
    st = dict(st)
    st["last_recovery_action"] = action
    st["last_recovery_at"] = _iso(now)
    return _save_if_changed(job_dir, st)


def succeed(job_dir: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """→ done：计数清零。幂等（已 done 时原样返回，不重写、不产生副作用）。"""
    st = _read_raw(job_dir) or _default()
    if st.get("status") == "done":
        return st
    now = now if now is not None else _now()
    st = dict(st)
    st["status"] = "done"
    st["attempts"] = 0
    st["consecutive_failures"] = 0
    st["next_retry_at"] = 0
    st["last_error"] = None
    st["progress"] = 100
    st["heartbeat_ts"] = now
    return _save_if_changed(job_dir, st)


def fail(job_dir: str, error: Optional[str] = None, *,
         pause_after: int = DEFAULT_PAUSE_AFTER,
         failure_class: str = "unknown",
         retryable: bool = True,
         phase: Optional[str] = None,
         now: Optional[float] = None) -> dict[str, Any]:
    """记录一次失败。

    - retryable=True（默认）：→ retrying（未超阈值，按 1/2/4/8/16/30 分钟退避）或
      paused（连续失败超限），并递增 consecutive_failures。
    - retryable=False（asset_missing / disk_full 等硬错误）：→ 终态 failed，
      **不递增** consecutive_failures（不计数），next_retry_at=0，等人工处理。
    phase/failure_class 写入对应字段，供第 2/3 项追溯。
    """
    now = now if now is not None else _now()
    st = _read_raw(job_dir) or _default()
    st = dict(st)
    st["last_error"] = error
    st["failure_class"] = failure_class
    st["heartbeat_ts"] = now
    if phase:
        st["phase"] = phase
    if not retryable:
        st["status"] = "failed"
        st["next_retry_at"] = 0
        return _save_if_changed(job_dir, st)
    fails = int(st.get("consecutive_failures") or 0) + 1
    st["consecutive_failures"] = fails
    if fails >= max(1, pause_after):
        st["status"] = "paused"
        st["next_retry_at"] = 0
    else:
        st["status"] = "retrying"
        st["next_retry_at"] = now + BACKOFF_S[min(fails - 1, len(BACKOFF_S) - 1)]
    return _save_if_changed(job_dir, st)


# ── 闸门 ─────────────────────────────────────────────────────────

def gate(job_dir: str, *, phase_override: Optional[str] = None,
         progress_mtime: float = 0.0,
         migrate_ctx: Optional[dict[str, Any]] = None) -> Optional[str]:
    """导出闸门：返回等待/暂停原因；None 表示可导。--phase export 为人工强制放行。

    只读（不落盘回收）：stale 推导仅在内存生效，持久化回收由 reap_stale 在执行链做。
    注意：done 不在此拦截——「是否已导」由 check_trigger（stage=video_ready /
    video.status=done）负责，gate 只管 重试退避 / 暂停 / 进行中 这类可恢复阻塞，
    以保证与旧 retry_gate 语义兼容（成功后放行）。
    """
    if phase_override == "export":
        return None
    st = load(job_dir, progress_mtime=progress_mtime, migrate_ctx=migrate_ctx)
    status = st.get("status")
    if status == "paused":
        return (f"连续 {st.get('consecutive_failures')} 次导出失败已暂停"
                f"（人工排查后删除 {STATE_NAME} 或 --phase export 强制放行）")
    if status == "failed":
        return f"导出硬失败（failure_class={st.get('failure_class')}），需人工处理"
    if status == "exporting":
        return "导出进行中（心跳仍新鲜）"
    nxt = float(st.get("next_retry_at") or 0)
    if status == "retrying" and nxt > _now():
        return (f"导出失败退避中（{int(nxt - _now())}s 后重试，"
                f"已失败 {st.get('consecutive_failures')} 次）")
    return None
