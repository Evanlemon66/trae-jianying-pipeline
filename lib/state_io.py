# -*- coding: utf-8 -*-
"""state_io.py — cover_job 状态文件（.edit_state.json / .export_state.json）的公共读写层。

本模块只收敛两个状态机里逐字重复的机械 IO，不持有任何状态机语义：

原子写
  先写 <name>.tmp 再 os.replace；Windows 上目标文件可能被杀软/索引短暂占用，
  os.replace 偶发 PermissionError，故短重试 5 次（等待 0.05/0.10/0.15/0.20s），
  最后一次仍失败则抛 OSError，并清理残留 .tmp。

幂等落盘
  与现内容一致（忽略 updated_at / writer）则跳过，不刷 mtime、不产生副作用。
  旧文件缺 version 时会补写一次（见下），即使其余字段完全一致。

version
  写入处集中补 version=1（缺省时）；读取旧文件（无 version）视为 1，
  不因缺字段报错，也不影响既有读取方（它们只按需读自己关心的键）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

STATE_VERSION = 1
WRITE_RETRIES = 5


def state_path(job_dir: str, state_name: str) -> str:
    return os.path.join(job_dir, state_name)


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts if ts is not None else _now()))


def _read_file(job_dir: str, state_name: str) -> Optional[dict[str, Any]]:
    """原样读取（不做 version 归一化）；缺失/损坏/非对象 → None。"""
    try:
        with open(state_path(job_dir, state_name), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_raw(job_dir: str, state_name: str) -> Optional[dict[str, Any]]:
    """读取状态；缺失/损坏 → None。旧文件无 version 视为 STATE_VERSION（仅在内存）。"""
    st = _read_file(job_dir, state_name)
    if st is not None and st.get("version") is None:
        st = dict(st)
        st["version"] = STATE_VERSION
    return st


def atomic_write(job_dir: str, state_name: str, data: dict[str, Any]) -> None:
    """原子落盘（不修改调用方的 dict）：补 updated_at / writer / version。

    失败语义：任何一步（建目录/写 tmp/替换）最终失败都会清理 .tmp 后抛 OSError，
    不静默吞掉。只读/评估路径（如 load 首次创建）若允许失败，必须在调用处
    单独兜住；begin/succeed/fail 等真实写入必须让异常冒到调用方。
    """
    data = dict(data)
    data["updated_at"] = _iso()
    data["writer"] = os.getpid()
    if data.get("version") is None:
        data["version"] = STATE_VERSION
    path = state_path(job_dir, state_name)
    tmp = path + ".tmp"
    try:
        os.makedirs(job_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        # 原子替换必须成功才算写入，故短重试而不是静默吞掉。
        for i in range(WRITE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except OSError:
                if i == WRITE_RETRIES - 1:
                    raise
                time.sleep(0.05 * (i + 1))
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise  # 清理完 tmp 后必须继续上抛，不能被本 except 吃掉


def save_if_changed(job_dir: str, state_name: str,
                    new: dict[str, Any]) -> dict[str, Any]:
    """幂等落盘：与现内容一致则跳过（不刷 mtime、不产生副作用）。

    旧文件缺 version 时不得跳过——下次写入需补上 version（见模块 docstring）。
    """
    cur = _read_file(job_dir, state_name)
    if cur is not None:
        same = all(cur.get(k) == v for k, v in new.items()
                   if k not in ("updated_at", "writer"))
        if same and cur.get("version") is not None:
            return cur
    atomic_write(job_dir, state_name, new)
    return new
