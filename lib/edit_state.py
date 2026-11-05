# -*- coding: utf-8 -*-
"""edit_state.py — 剪辑（edit）阶段失败退避/暂停的状态机（cover_job/.edit_state.json）。

背景：此前 edit 阶段失败（如 ingest 校验不过、batch_runner 崩溃）每 5s 立即重试，
不计数、不退避、不暂停，坏数据会无限空转。本模块给 edit 阶段一套与导出侧同构、
但互相独立的状态机（不塞进 .export_state.json，两阶段各管各的）。

唯一写者
  本文件（lib/edit_state.py）是 .edit_state.json 的唯一写者，且只由 cover_watch
  的剪辑链路（run_edit / note_edit_result / build_plan 闸门读取时的初始化）调用。
  batch_runner、job_manifest 均不写它。stage 等契约字段仍由 job_manifest 写，
  本模块不碰 job.json。

状态机（取值 / 谁写 / 何时写）
  pending   初始化 / 自愈补目录后回到可重试          → editing
  editing   begin()：run_edit 启动 batch_runner 前    → done / retrying / paused / failed /（自愈→pending）
  retrying  fail()：可重试失败未超阈值（退避窗口内）   → editing（窗口结束）/ paused
  paused    fail()：连续失败 ≥ DEFAULT_PAUSE_AFTER(3) → editing（仅 --phase edit 人工放行）
  done      succeed()：剪辑成功（草稿建成）           终态；无草稿才会再进 edit，届时重新计数
  failed    fail(retryable=False)：硬失败（失败分类）  终态，需人工处理

退避与暂停
  退避序列复用导出侧 BACKOFF_S（1/2/4/8/16/30 分钟）；连续 3 次可重试失败置 paused
  + toast（比导出侧的 5 次更早——edit 失败多为数据问题，重试救不回来）。

可自愈失败（不占额度）
  「缺少目录」这类环境缺陷由 cover_watch 识别并自动补齐后调用 heal()：
  状态回 pending、不递增 consecutive_failures、不进退避，下一轮立即重试；
  已补过的目录再次缺失视为普通失败（防止无限自愈）。

闸门
  gate()：paused / failed / 退避窗口内 → 返回可读原因（格式对齐导出侧 retry_gate）；
  phase_override=="edit" 强制放行且不清零计数。editing 不拦截：watcher 串行执行 +
  .watcher.lock 已防并发，崩溃后残留的 editing 应允许下一轮直接重试。
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import export_state as _es  # 复用导出侧退避序列，保证两阶段一致
import state_io as _sio  # 原子写/幂等落盘/version 的公共实现

STATE_NAME = ".edit_state.json"

STATUSES = ("pending", "editing", "retrying", "paused", "done", "failed")
BACKOFF_S = _es.BACKOFF_S          # 1 / 2 / 4 / 8 / 16 / 30 分钟
DEFAULT_PAUSE_AFTER = 3            # 比导出侧（5）更早：edit 失败多为数据问题


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
        "next_retry_at": 0,          # epoch；0 表示立即可重试
        "last_error": None,
        "failure_class": None,
        "phase": None,
        "healed_dirs": [],           # 已自动补齐过的目录（正斜杠），同一目录只自愈一次
        "updated_at": None,
        "writer": None,
    }


def _atomic_write(job_dir: str, data: dict[str, Any]) -> None:
    _sio.atomic_write(job_dir, STATE_NAME, data)


def _read_raw(job_dir: str) -> Optional[dict[str, Any]]:
    return _sio.read_raw(job_dir, STATE_NAME)


def _save_if_changed(job_dir: str, new: dict[str, Any]) -> dict[str, Any]:
    """幂等落盘：与现内容一致则跳过（不刷 mtime、不产生副作用）。"""
    return _sio.save_if_changed(job_dir, STATE_NAME, new)


# ── 读取 ─────────────────────────────────────────────────────────

def load(job_dir: str) -> dict[str, Any]:
    """读状态；缺失时落盘初始 pending（与 export_state.load 的创建语义一致）。"""
    st = _read_raw(job_dir)
    if st is None:
        st = _default()
        _atomic_write(job_dir, st)
    return st


# ── 状态转移 ─────────────────────────────────────────────────────

def begin(job_dir: str, *, phase: Optional[str] = "edit",
          now: Optional[float] = None) -> dict[str, Any]:
    """→ editing。已在 editing 时只刷新（幂等，不重复计次）。"""
    st = _read_raw(job_dir) or _default()
    st = dict(st)
    if st.get("status") != "editing":
        st["attempts"] = int(st.get("attempts") or 0) + 1
    st["status"] = "editing"
    st["next_retry_at"] = 0
    if phase:
        st["phase"] = phase
    return _save_if_changed(job_dir, st)


def succeed(job_dir: str) -> dict[str, Any]:
    """→ done：计数清零。幂等（已 done 时原样返回）。"""
    st = _read_raw(job_dir) or _default()
    if st.get("status") == "done":
        return st
    st = dict(st)
    st["status"] = "done"
    st["attempts"] = 0
    st["consecutive_failures"] = 0
    st["next_retry_at"] = 0
    st["last_error"] = None
    return _save_if_changed(job_dir, st)


def fail(job_dir: str, error: Optional[str] = None, *,
         pause_after: int = DEFAULT_PAUSE_AFTER,
         failure_class: str = "unknown",
         retryable: bool = True,
         phase: Optional[str] = "edit",
         now: Optional[float] = None) -> dict[str, Any]:
    """记录一次失败。

    - retryable=True：→ retrying（按 1/2/4/8/16/30 分钟退避）或
      paused（连续失败 ≥ pause_after），递增 consecutive_failures。
    - retryable=False（asset_missing / disk_full 等硬错误）：→ 终态 failed，
      **不递增** consecutive_failures（不计数），等人工处理。
    """
    now = now if now is not None else _now()
    st = _read_raw(job_dir) or _default()
    st = dict(st)
    st["last_error"] = error
    st["failure_class"] = failure_class
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


def heal(job_dir: str, dirs: list[str]) -> dict[str, Any]:
    """可自愈失败（缺目录已自动补齐）：回 pending、不进退避、不递增失败计数。

    补齐的目录记入 healed_dirs（正斜杠归一），同一目录只自愈一次——
    补过仍缺失说明是别的问题，按普通失败处理。
    """
    st = _read_raw(job_dir) or _default()
    st = dict(st)
    healed = list(st.get("healed_dirs") or [])
    for d in dirs:
        norm = d.replace("\\", "/").strip("/")
        if norm and norm not in healed:
            healed.append(norm)
    st["healed_dirs"] = healed
    st["status"] = "pending"
    st["next_retry_at"] = 0
    st["last_error"] = None
    st["failure_class"] = None
    return _save_if_changed(job_dir, st)


def already_healed(job_dir: str, dirs: list[str]) -> bool:
    """dirs 是否全部已自愈过（是 → 不应再自愈，按普通失败计数）。"""
    healed = set((_read_raw(job_dir) or {}).get("healed_dirs") or [])
    norm = {d.replace("\\", "/").strip("/") for d in dirs}
    return bool(norm) and norm <= healed


# ── 闸门 ─────────────────────────────────────────────────────────

def gate(job_dir: str, *, phase_override: Optional[str] = None,
         now: Optional[float] = None) -> Optional[str]:
    """edit 闸门：返回等待/暂停原因；None 表示可剪辑。--phase edit 人工强制放行
    （不清零计数）。格式与导出侧 retry_gate 一致。

    editing 不拦截：watcher 串行 + .watcher.lock 防并发；崩溃残留的 editing
    允许下一轮直接重试（无子进程心跳可判，不做 stale 回收）。
    """
    if phase_override == "edit":
        return None
    now = now if now is not None else _now()
    st = load(job_dir)
    status = st.get("status")
    if status == "paused":
        return (f"连续 {st.get('consecutive_failures')} 次剪辑失败已暂停"
                f"（人工排查后删除 {STATE_NAME} 或 --phase edit 强制放行）")
    if status == "failed":
        return f"剪辑硬失败（failure_class={st.get('failure_class')}），需人工处理"
    nxt = float(st.get("next_retry_at") or 0)
    if status == "retrying" and nxt > now:
        return (f"剪辑失败退避中（{int(nxt - now)}s 后重试，"
                f"已失败 {st.get('consecutive_failures')} 次）")
    return None
