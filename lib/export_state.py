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
        "version": 2,
        "status": "pending",
        "attempts": 0,
        "consecutive_failures": 0,
        "last_error": None,
        "next_retry_at": 0,          # epoch；0 表示立即可重试（顶层=各版本聚合）
        "progress": None,            # 0-100 整数或 None
        "heartbeat_ts": 0,           # epoch 心跳（与 .progress.json 取大者判活）
        "updated_at": None,          # 人类可读的最近写入时间
        "writer": None,              # 最近写入者 pid
        "phase": None,               # 预留：cold_start / export / verify
        "failure_class": "unknown",  # 预留：失败分类（第 3 项）
        "last_recovery_action": None,  # 最近一次自愈动作（soft_home/kill_restart/dismiss/dismiss_then_kill）
        "last_recovery_at": None,      # 最近一次自愈动作时间（ISO）
        "active_profile": None,      # 当前正在导出的版本（导出全局串行）
        "profiles": {},              # 版本维度：{版本: 与顶层同构的子状态}
    }


def _profile_default() -> dict[str, Any]:
    """单版本子状态（不含 profiles/active_profile，避免递归）。"""
    return {
        "status": "pending",
        "attempts": 0,
        "consecutive_failures": 0,
        "last_error": None,
        "next_retry_at": 0,
        "progress": None,
        "heartbeat_ts": 0,
        "updated_at": None,
        "phase": None,
        "failure_class": "unknown",
    }


def _prof(st: dict[str, Any], name: str) -> dict[str, Any]:
    """取（必要时创建）版本子状态；调用方需持有可写的整份状态副本。"""
    profiles = st.setdefault("profiles", {})
    p = dict(_profile_default())
    p.update(profiles.get(name) or {})
    profiles[name] = p
    return p


def _aggregate(st: dict[str, Any]) -> dict[str, Any]:
    """把各版本子状态聚合回顶层（顶层 status 仍是全局闸门）。

    - 所有版本 done → done；
    - 任一出错 → 依次降级 paused / failed / retrying（next_retry_at 取最小）；
    - 都没有问题时 → pending（可继续导下一版）。
    """
    profiles = st.get("profiles") or {}
    if not profiles:
        return st
    statuses = [str((v or {}).get("status") or "pending") for v in profiles.values()]
    if all(s == "done" for s in statuses):
        st["status"] = "done"
        st["progress"] = 100
        st["consecutive_failures"] = 0
        st["next_retry_at"] = 0
    elif "paused" in statuses:
        st["status"] = "paused"
        st["next_retry_at"] = 0
    elif "failed" in statuses:
        st["status"] = "failed"
        st["next_retry_at"] = 0
    elif "exporting" in statuses:
        st["status"] = "exporting"
    elif "retrying" in statuses:
        st["status"] = "retrying"
        waits = [float((v or {}).get("next_retry_at") or 0)
                 for v in profiles.values() if (v or {}).get("status") == "retrying"]
        st["next_retry_at"] = min(waits) if waits else 0
    else:
        st["status"] = "pending"
        st["next_retry_at"] = 0
    return st


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


def _infer_initial(job_dir: str, *, video_status: Optional[str] = None,
                   stage: Optional[str] = None, progress_status: Optional[str] = None,
                   progress_mtime: float = 0.0, progress_pct: Optional[int] = None,
                   now: Optional[float] = None) -> dict[str, Any]:
    """按旧数据在内存里推断初始状态（纯函数，不落盘）。优先级见模块 docstring。"""
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
    return st


def migrate(job_dir: str, *, video_status: Optional[str] = None,
            stage: Optional[str] = None, progress_status: Optional[str] = None,
            progress_mtime: float = 0.0, progress_pct: Optional[int] = None,
            now: Optional[float] = None) -> dict[str, Any]:
    """真源缺失时按旧数据一次性推断并落盘。

    入参由调用方（cover_watch）从 job.json / .progress.json 读好后传入，
    本模块不反向依赖 job_manifest / pipeline，保持单向。
    """
    st = _infer_initial(job_dir, video_status=video_status, stage=stage,
                        progress_status=progress_status, progress_mtime=progress_mtime,
                        progress_pct=progress_pct, now=now)
    _backup_legacy_retry(job_dir)
    _atomic_write(job_dir, st)
    return st


# ── 读取 ─────────────────────────────────────────────────────────

def load(job_dir: str, *, progress_mtime: float = 0.0, migrate_ctx: Optional[dict[str, Any]] = None,
         now: Optional[float] = None) -> dict[str, Any]:
    """读真源；缺失时用 migrate_ctx 迁移。返回应用了 stale 推导的有效状态（不落盘回收）。

    migrate_ctx 可选键：video_status / stage / progress_status / progress_pct。
    只读/评估路径：首次创建落盘失败（OSError）时退回纯内存推断，不打断调度。
    """
    st = _read_raw(job_dir)
    if st is None:
        ctx = migrate_ctx or {}
        kwargs = dict(
            video_status=ctx.get("video_status"),
            stage=ctx.get("stage"),
            progress_status=ctx.get("progress_status"),
            progress_mtime=progress_mtime,
            progress_pct=ctx.get("progress_pct"),
            now=now,
        )
        try:
            st = migrate(job_dir, **kwargs)
        except OSError:
            # 只读路径不能因为落盘失败把 daemon 打崩；内存态语义与 migrate 一致
            st = _infer_initial(job_dir, **kwargs)
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
    act = st.get("active_profile")
    if act:
        p = _prof(st, act)
        p["status"] = "retrying"
        p["next_retry_at"] = 0
        p["last_error"] = st["last_error"]
    st["active_profile"] = None
    _save_if_changed(job_dir, st)
    return True


# ── 状态转移 ─────────────────────────────────────────────────────

def register_profiles(job_dir: str, names: list[str]) -> dict[str, Any]:
    """登记本剧声明的全部版本（缺失的补 pending），保证顶层聚合不会提前 done。"""
    if not names:
        return _read_raw(job_dir) or _default()
    st = dict(_read_raw(job_dir) or _default())
    changed = False
    for n in names:
        n = str(n).strip().lower()
        if n and n not in (st.get("profiles") or {}):
            _prof(st, n)
            changed = True
    if changed:
        _aggregate(st)
        return _save_if_changed(job_dir, st)
    return st


def begin(job_dir: str, *, phase: Optional[str] = None,
          profile: Optional[str] = None,
          now: Optional[float] = None) -> dict[str, Any]:
    """pending/retrying/paused → exporting。已在 exporting 时只刷心跳（幂等，不重复计次）。

    profile：本次导出的版本。顶层 status 恒为 exporting（导出全局串行），
    版本自己的 status/attempts/next_retry_at 单独记在 profiles[profile]。
    """
    now = now if now is not None else _now()
    st = _read_raw(job_dir) or _default()
    if st.get("status") != "exporting":
        st["attempts"] = int(st.get("attempts") or 0) + 1
    st["status"] = "exporting"
    st["heartbeat_ts"] = now
    st["next_retry_at"] = 0
    if phase:
        st["phase"] = phase
    if profile:
        st["active_profile"] = profile
        p = _prof(st, profile)
        if p.get("status") != "exporting":
            p["attempts"] = int(p.get("attempts") or 0) + 1
        p["status"] = "exporting"
        p["heartbeat_ts"] = now
        p["next_retry_at"] = 0
        if phase:
            p["phase"] = phase
    return _save_if_changed(job_dir, st)


def heartbeat(job_dir: str, *, progress: Optional[int] = None,
              phase: Optional[str] = None, profile: Optional[str] = None,
              now: Optional[float] = None) -> None:
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
    if profile:
        st["active_profile"] = profile
        p = _prof(st, profile)
        p["heartbeat_ts"] = now
        if progress is not None:
            p["progress"] = max(0, min(100, int(progress)))
        if phase:
            p["phase"] = phase
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


def succeed(job_dir: str, *, profile: Optional[str] = None,
           now: Optional[float] = None) -> dict[str, Any]:
    """→ done：计数清零。幂等（已 done 时原样返回，不重写、不产生副作用）。

    profile：只把该版本置 done；顶层在所有版本都 done 时才变 done，
    否则回到 pending/has-error 聚合态，让下一版还能继续导。
    """
    st = _read_raw(job_dir) or _default()
    if not profile and st.get("status") == "done":
        return st
    now = now if now is not None else _now()
    st = dict(st)
    st["active_profile"] = None
    if profile:
        p = _prof(st, profile)
        p["status"] = "done"
        p["attempts"] = 0
        p["consecutive_failures"] = 0
        p["next_retry_at"] = 0
        p["last_error"] = None
        p["progress"] = 100
        p["heartbeat_ts"] = now
        st["last_error"] = None
        st["heartbeat_ts"] = now
        _aggregate(st)
        return _save_if_changed(job_dir, st)
    st["status"] = "done"
    st["attempts"] = 0
    st["consecutive_failures"] = 0
    st["next_retry_at"] = 0
    st["last_error"] = None
    st["progress"] = 100
    st["heartbeat_ts"] = now
    if st.get("profiles"):
        for _k, _v in st["profiles"].items():
            if isinstance(_v, dict):
                _v["status"] = "done"
                _v["next_retry_at"] = 0
    return _save_if_changed(job_dir, st)


def fail(job_dir: str, error: Optional[str] = None, *,
         pause_after: int = DEFAULT_PAUSE_AFTER,
         failure_class: str = "unknown",
         retryable: bool = True,
         phase: Optional[str] = None,
         profile: Optional[str] = None,
         now: Optional[float] = None) -> dict[str, Any]:
    """记录一次失败。

    - retryable=True（默认）：→ retrying（未超阈值，按 1/2/4/8/16/30 分钟退避）或
      paused（连续失败超限），并递增 consecutive_failures。
    - retryable=False（asset_missing / disk_full 等硬错误）：→ 终态 failed，
      **不递增** consecutive_failures（不计数），next_retry_at=0，等人工处理。
    phase/failure_class 写入对应字段，供第 2/3 项追溯。
    profile：只更新该版本子状态，顶层按所有版本重新聚合（退避按版本独立）。
    """
    now = now if now is not None else _now()
    st = _read_raw(job_dir) or _default()
    st = dict(st)
    st["last_error"] = error
    st["failure_class"] = failure_class
    st["heartbeat_ts"] = now
    if phase:
        st["phase"] = phase
    if profile:
        st["active_profile"] = None
        p = _prof(st, profile)
        p["last_error"] = error
        p["failure_class"] = failure_class
        p["heartbeat_ts"] = now
        if phase:
            p["phase"] = phase
        if not retryable:
            p["status"] = "failed"
            p["next_retry_at"] = 0
        else:
            fails = int(p.get("consecutive_failures") or 0) + 1
            p["consecutive_failures"] = fails
            if fails >= max(1, pause_after):
                p["status"] = "paused"
                p["next_retry_at"] = 0
            else:
                p["status"] = "retrying"
                p["next_retry_at"] = now + BACKOFF_S[min(fails - 1, len(BACKOFF_S) - 1)]
        _aggregate(st)
        return _save_if_changed(job_dir, st)
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


def clear_block(job_dir: str, *, failure_class: str = "subtitle_missing",
                now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """解除不可重试的硬失败：仅 status=="failed" 且 failure_class 匹配时 -> pending。

    用于缺字幕修复后的复位：该集重新剪辑且字幕非空、清单清空后，由 cover_watch
    调用；不匹配/非 failed 时原样返回（幂等）。attempts/consecutive_failures 保留，
    仍可追溯历史失败次数。
    """
    st = _read_raw(job_dir)
    if st is None:
        return None
    if st.get("status") != "failed" or st.get("failure_class") != failure_class:
        return st
    now = now if now is not None else _now()
    st = dict(st)
    st["status"] = "pending"
    st["last_error"] = None
    st["failure_class"] = "unknown"
    st["next_retry_at"] = 0
    st["progress"] = None
    st["heartbeat_ts"] = now
    return _save_if_changed(job_dir, st)


# ── 闸门 ─────────────────────────────────────────────────────────

def _infer_from_ctx(job_dir: str, *, progress_mtime: float = 0.0,
                    migrate_ctx: Optional[dict[str, Any]] = None,
                    now: Optional[float] = None) -> dict[str, Any]:
    """按 migrate_ctx 在内存里推断初始状态（不迁移、不写盘）。"""
    ctx = migrate_ctx or {}
    return _infer_initial(
        job_dir,
        video_status=ctx.get("video_status"),
        stage=ctx.get("stage"),
        progress_status=ctx.get("progress_status"),
        progress_mtime=progress_mtime,
        progress_pct=ctx.get("progress_pct"),
        now=now,
    )


def gate(job_dir: str, *, phase_override: Optional[str] = None,
         progress_mtime: float = 0.0,
         migrate_ctx: Optional[dict[str, Any]] = None,
         profile: Optional[str] = None) -> Optional[str]:
    """导出闸门：返回等待/暂停原因；None 表示可导。--phase export 为人工强制放行。

    纯内存推导：文件缺失时按 migrate_ctx 推断，但**不 migrate、不备份、不写盘**；
    真实创建统一交给 begin()（真实导出开始时调用）。stale 推导也只在内存生效，
    持久化回收由 reap_stale 在执行链做。
    注意：done 不在此拦截——「是否已导」由 check_trigger（stage=video_ready /
    video.status=done）负责，gate 只管 重试退避 / 暂停 / 进行中 这类可恢复阻塞，
    以保证与旧 retry_gate 语义兼容（成功后放行）。
    """
    if phase_override == "export":
        return None
    st = _read_raw(job_dir)
    if st is None:
        st = _infer_from_ctx(job_dir, progress_mtime=progress_mtime,
                             migrate_ctx=migrate_ctx)
    st = _effective(st, progress_mtime)
    if profile:
        if st.get("status") == "exporting" and st.get("active_profile") not in (None, profile):
            return "导出进行中（其他版本，导出为全局串行）"
        p = dict(_profile_default())
        p.update((st.get("profiles") or {}).get(profile) or {})
        if _is_stale_exporting(p, progress_mtime, _now(), STALE_AFTER_S):
            p = dict(p)
            p["status"] = "retrying"
            p["next_retry_at"] = 0
        ps = p.get("status")
        if ps == "paused":
            return (f"版本 {profile} 连续 {p.get('consecutive_failures')} 次导出失败已暂停"
                    f"（人工排查后删除 {STATE_NAME} 或 --phase export 强制放行）")
        if ps == "failed":
            return f"版本 {profile} 导出硬失败（failure_class={p.get('failure_class')}），需人工处理"
        if ps == "exporting":
            return "导出进行中（心跳仍新鲜）"
        nxt = float(p.get("next_retry_at") or 0)
        if ps == "retrying" and nxt > _now():
            return (f"版本 {profile} 导出失败退避中（{int(nxt - _now())}s 后重试，"
                    f"已失败 {p.get('consecutive_failures')} 次）")
        return None
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
