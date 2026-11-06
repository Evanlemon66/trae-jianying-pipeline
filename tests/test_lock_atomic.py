# -*- coding: utf-8 -*-
"""回归用例：两把锁的原子抢占 / 陈旧锁接管 / release pid 归属。

运行：python tests\\test_lock_atomic.py（纯临时目录；_pid_alive 用 mock，不碰真实进程）
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import export_lock as el  # noqa: E402
import cover_watch as cw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def concurrent_acquire(fn):
    """两个线程同时抢占，返回成功次数。"""
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(fn())

    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return sum(1 for r in results if r)


def test_export_lock(tmp):
    path = os.path.join(tmp, ".export.lock")
    with mock.patch.object(el, "_LOCK_PATH", path), \
            mock.patch.object(el, "_pid_alive", return_value=True):
        check("导出锁：并发抢占只有一个成功",
              concurrent_acquire(lambda: el.acquire("t")) == 1)

        data = read_json(path)
        check("导出锁：锁内 pid 为当前进程", data.get("pid") == os.getpid(), data)

        el.release()
        check("导出锁：自己的锁可释放", not os.path.exists(path))

    # 陈旧锁：pid 已死 → 接管一次
    write_json(path, {"pid": 999999, "owner": "dead"})
    with mock.patch.object(el, "_LOCK_PATH", path), \
            mock.patch.object(el, "_pid_alive", return_value=False):
        check("导出锁：陈旧锁可被接管", el.acquire("new") is True)
        check("导出锁：接管后 pid 为自己",
              read_json(path).get("pid") == os.getpid(), read_json(path))
        el.release()

    # release 不误删别人的锁（pid 是别的活进程）
    write_json(path, {"pid": os.getppid(), "owner": "other"})
    with mock.patch.object(el, "_LOCK_PATH", path):
        el.release()
    check("导出锁：release 不删别人的锁", os.path.exists(path), read_json(path))


def test_watcher_lock(tmp):
    job_dir = os.path.join(tmp, "job")
    os.makedirs(job_dir, exist_ok=True)
    path = os.path.join(job_dir, ".watcher.lock")

    with mock.patch.object(cw, "_pid_alive", return_value=True):
        check("watcher 锁：并发抢占只有一个成功",
              concurrent_acquire(lambda: cw.acquire_lock(job_dir, "edit")) == 1)
        check("watcher 锁：锁内 pid 为当前进程",
              read_json(path).get("pid") == os.getpid(), read_json(path))
        cw.release_lock(job_dir)
        check("watcher 锁：自己的锁可释放", not os.path.exists(path))

    write_json(path, {"pid": 999999, "job": job_dir, "phase": "dead"})
    with mock.patch.object(cw, "_pid_alive", return_value=False):
        check("watcher 锁：陈旧锁可被接管",
              cw.acquire_lock(job_dir, "export") is True)
        cw.release_lock(job_dir)

    write_json(path, {"pid": os.getppid(), "job": job_dir, "phase": "other"})
    cw.release_lock(job_dir)
    check("watcher 锁：release 不删别人的锁", os.path.exists(path), read_json(path))

    # _write_lock 只更新本进程持有的锁；别人的锁不改
    os.remove(path)
    with mock.patch.object(cw, "_pid_alive", return_value=True):
        cw.acquire_lock(job_dir, "triggered")
    cw._write_lock(job_dir, "edit", pipeline_pid=12345)
    data = read_json(path)
    check("watcher 锁：_write_lock 更新 phase/pipeline_pid",
          data.get("phase") == "edit" and data.get("pipeline_pid") == 12345, data)
    check("watcher 锁：_write_lock 保留原 pid", data.get("pid") == os.getpid(), data)
    write_json(path, {"pid": os.getppid(), "job": job_dir, "phase": "other"})
    cw._write_lock(job_dir, "export", pipeline_pid=1)
    check("watcher 锁：_write_lock 不改别人的锁",
          read_json(path).get("phase") == "other", read_json(path))


def main():
    tmp = tempfile.mkdtemp(prefix="lock_atomic_")
    try:
        test_export_lock(tmp)
        test_watcher_lock(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nFAILED: {len(fails)} 项 → {fails}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
