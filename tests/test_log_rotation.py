# -*- coding: utf-8 -*-
"""回归用例：lib/cover_watch.py 的 watcher.log 按大小轮转。

覆盖：
  ① 未超上限 → 不产生 .1~.5，正常追加
  ② 超上限 → 产生 .1~.5，最老的 .5 被删，当前文件继续可写
  ③ 备份链顺序正确（.1 比 .5 新，当前文件最新）
  ④ 改名失败（模拟文件被占用）→ 不抛异常，继续写当前文件并输出 stderr 告警
  ⑤ 全部日志都走唯一写入口 _log（_effective_cut 的分歧日志也走它）

运行：python tests\\test_log_rotation.py（纯临时目录，不碰真实 watcher.log）
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import cover_watch as cw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "log_rotation_test")
shutil.rmtree(TMP, ignore_errors=True)
JD = os.path.join(TMP, "cover_job")
os.makedirs(JD, exist_ok=True)
LOG = os.path.join(JD, "watcher.log")

MAX = 2048
PAD = "x" * 150


def line(i):
    return f"L{i:04d} {PAD}"


def idx_max(path):
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return -1
    nums = [int(n) for n in re.findall(r"L(\d{4})", txt)]
    return max(nums) if nums else -1


def idx_min(path):
    txt = open(path, encoding="utf-8").read()
    nums = [int(n) for n in re.findall(r"L(\d{4})", txt)]
    return min(nums) if nums else -1


def backup_paths():
    return [f"{LOG}.{i}" for i in range(1, 6)]


old_max, old_count = cw.LOG_MAX_BYTES, cw.LOG_BACKUP_COUNT
cw.LOG_MAX_BYTES, cw.LOG_BACKUP_COUNT = MAX, 5
try:
    print("\n== ① 未超上限不轮转 ==")
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(1, 4):
            cw._log(JD, line(i))
    check("当前文件已写入", os.path.exists(LOG) and idx_max(LOG) == 3)
    check("未产生 .1", not os.path.exists(f"{LOG}.1"))

    print("\n== ②/③ 超上限轮转 ==")
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(4, 121):
            cw._log(JD, line(i))
    check("产生 .1~.5", all(os.path.exists(p) for p in backup_paths()),
          [os.path.exists(p) for p in backup_paths()])
    check("最老的 .5 已被删（最早的 L0001 不出现在任何文件）",
          not any("L0001 " in open(p, encoding="utf-8").read()
                  for p in [LOG] + backup_paths() if os.path.exists(p)))
    check("当前文件保留最新一行 L0120", idx_max(LOG) == 120, idx_max(LOG))
    check("备份链顺序：.1 比 .5 新", idx_min(f"{LOG}.5") < idx_max(f"{LOG}.1"),
          (idx_min(f"{LOG}.5"), idx_max(f"{LOG}.1")))
    check(".1 比当前文件旧", idx_max(f"{LOG}.1") < idx_max(LOG))

    with contextlib.redirect_stdout(io.StringIO()):
        cw._log(JD, line(121))
    check("轮转后继续追加成功", idx_max(LOG) == 121, idx_max(LOG))

    print("\n== ④ 改名失败容错 ==")
    err = io.StringIO()
    cw.LOG_MAX_BYTES = 10  # 强制触发轮转
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
            mock.patch.object(cw.os, "replace", side_effect=OSError("模拟被占用")):
        cw._log(JD, line(122))
    check("改名失败不抛异常且仍写入当前文件", idx_max(LOG) == 122, idx_max(LOG))
    check("失败时输出 stderr 告警", "[warn]" in err.getvalue(), err.getvalue())

    print("\n== ⑤ 轮转挂在唯一写入口 ==")
    with mock.patch.object(cw, "_rotate_log") as rot, \
            contextlib.redirect_stdout(io.StringIO()):
        cw._log(JD, line(123))
    check("_log 每次写入前都调用轮转", rot.call_count == 1, rot.call_count)
    check("轮转目标是 job 目录下的 watcher.log", rot.call_args[0][0] == LOG, rot.call_args)
finally:
    cw.LOG_MAX_BYTES, cw.LOG_BACKUP_COUNT = old_max, old_count
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
