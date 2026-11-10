# -*- coding: utf-8 -*-
"""no_window 兜底补丁测试：无控制台时自动给 Popen 补 CREATE_NO_WINDOW。

覆盖：
  A. 模拟无控制台 → subprocess.run 未传 creationflags 时自动带上 CREATE_NO_WINDOW
  B. 显式传 creationflags=0 / DETACHED_PROCESS → 原样尊重，不被改写
  C. 有控制台 → 不打补丁（终端调试行为不变）
  D. 幂等：重复安装不叠加包装
  E. 真实子进程在补丁下仍可正常跑（仅 Windows）

运行：python tests\\test_no_window_default.py
"""
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import no_window as nw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


RECORDED = []


class _RecordingPopen:
    """只记录 kwargs、不真正起进程的 Popen 替身（subprocess.run 需要的最小接口）。"""

    def __init__(self, *args, **kwargs):
        RECORDED.append({"args": args, "kwargs": dict(kwargs)})
        self.args = args
        self.returncode = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, *a, **k):
        return (b"", b"")

    def wait(self, *a, **k):
        return 0

    def poll(self):
        return 0


_real_popen = subprocess.Popen
_real_console = nw._console_available
try:
    # ── A：模拟无控制台 → 默认补 NO_WINDOW ──────────────────────────
    nw._installed = False
    nw._console_available = lambda: False
    subprocess.Popen = _RecordingPopen
    RECORDED.clear()
    installed = nw.install_default_hidden()
    check("A1 无控制台时安装补丁", installed is True, installed)
    subprocess.run(["cmd", "/c", "exit"])
    check("A2 未传 creationflags 时自动补 NO_WINDOW",
          bool(RECORDED) and RECORDED[0]["kwargs"].get("creationflags") == nw.NO_WINDOW,
          RECORDED[:1])

    # ── D：幂等 ────────────────────────────────────────────────────
    first_init = _RecordingPopen.__init__
    again = nw.install_default_hidden()
    check("D1 重复安装返回 False", again is False, again)
    check("D2 重复安装不叠加包装", _RecordingPopen.__init__ is first_init)

    # ── B：显式 creationflags 一律尊重 ────────────────────────────
    RECORDED.clear()
    subprocess.run(["cmd", "/c", "exit"], creationflags=0)
    check("B1 显式 0 不被改写",
          bool(RECORDED) and RECORDED[-1]["kwargs"].get("creationflags") == 0,
          RECORDED[-1:])
    RECORDED.clear()
    subprocess.run(["cmd", "/c", "exit"], creationflags=0x00000008)
    check("B2 显式 DETACHED_PROCESS 不被改写",
          bool(RECORDED) and RECORDED[-1]["kwargs"].get("creationflags") == 0x00000008,
          RECORDED[-1:])

    # ── C：有控制台 → 不打补丁 ────────────────────────────────────
    nw._installed = False
    nw._console_available = lambda: True
    before = _RecordingPopen.__init__
    subprocess.Popen = _RecordingPopen
    installed = nw.install_default_hidden()
    check("C1 有控制台时不安装", installed is False, installed)
    check("C2 有控制台时 Popen.__init__ 未被替换",
          _RecordingPopen.__init__ is before)

    # ── E：真实子进程（仅 Windows）───────────────────────────────
    if os.name == "nt" and nw.NO_WINDOW:
        subprocess.Popen = _real_popen
        nw._installed = False
        nw._console_available = lambda: False
        nw.install_default_hidden()
        r = subprocess.run([sys.executable, "-c", "print('ok')"],
                           capture_output=True, text=True)
        check("E1 补丁下真实子进程正常执行",
              r.returncode == 0 and "ok" in (r.stdout or ""),
              (r.returncode, r.stdout, r.stderr))
finally:
    subprocess.Popen = _real_popen
    nw._console_available = _real_console
    nw._installed = False

print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
