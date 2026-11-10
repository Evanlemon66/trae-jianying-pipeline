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

# pythonw 托管下没有控制台，tasklist 子进程必须显式隐藏，否则每次探活弹窗
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".export.lock")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_NO_WINDOW).stdout.strip()
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


def _claim(data: dict[str, Any]) -> bool:
    """原子抢占：O_CREAT|O_EXCL 创建成功才算拿到锁。"""
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        # 写完失败：清掉自己刚创建的文件，避免留下不可读锁
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            pass
        raise
    return True


def acquire(owner: str = "") -> bool:
    """原子抢占导出锁：成功返回 True；活锁返回 False；陈旧锁接管一次。"""
    data = {"pid": os.getpid(), "owner": owner,
            "started": datetime.now().astimezone().isoformat(timespec="seconds")}
    if _claim(data):
        return True

    existing = read_lock()
    if not existing:
        # 文件存在但不可解析（极短写入窗口 / 损坏）：不删，避免误删别人刚创建的锁
        return False
    pid = int(existing.get("pid") or 0)
    if pid > 0 and not _pid_alive(pid):
        # 陈旧锁：再读一次确认 pid 未变，删掉后重试一次抢占
        again = read_lock()
        if not again or int(again.get("pid") or 0) != pid:
            return False
        try:
            os.remove(_LOCK_PATH)
        except OSError:
            return False
        return _claim(data)
    return False


def release() -> None:
    """只释放自己持有的锁：pid 不是本进程就不删。"""
    data = read_lock()
    if not data or int(data.get("pid") or 0) != os.getpid():
        return
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass
