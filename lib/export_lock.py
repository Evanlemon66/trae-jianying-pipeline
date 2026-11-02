"""repo 级全局导出锁（.export.lock）：同一时刻只允许一个导出流程。

防的是两个 batch_runner（或 watcher 链 + 手工）同时打剪映云 / 操作剪映草稿。
锁是建议性的：pid 判活，进程崩了锁自动失效；--force 可显式绕过。
被 cover_watch（导出步）与 pipeline/batch_runner（启动自检）共用。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Optional

_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".export.lock")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        for line in out.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) > 1 and cols[1] == str(pid):
                return True
    except Exception:
        pass
    return False


def read_lock() -> Optional[dict[str, Any]]:
    try:
        with open(_LOCK_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def lock_alive() -> bool:
    data = read_lock()
    return bool(data) and _pid_alive(int(data.get("pid") or 0))


def acquire(owner: str = "") -> bool:
    """活锁存在返回 False；否则写入 {pid, owner, started} 返回 True。"""
    if lock_alive():
        return False
    data = {"pid": os.getpid(), "owner": owner,
            "started": datetime.now().astimezone().isoformat(timespec="seconds")}
    with open(_LOCK_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True


def release() -> None:
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass
