# -*- coding: utf-8 -*-
"""died_late 止血层（第一层）：进程退出快速失败 + 防双开。

覆盖：
  ① _wait_edit_page：等待期间进程消失 → 立刻抛 [launch_failed:died_during_open]，
     不再把墙钟耗满；
  ② 进程存活但始终未进编辑页 → 仍按原墙钟抛“打开草稿超时”（行为不回退）；
  ③ _proc_alive：探测异常按存活处理，进程消失返回 False；
  ④ JyInstance.launch：已有进程存活时只重连/等待，spawn 次数为 0；
  ⑤ restart：kill 后进程消失 → 正常 spawn（防双开不阻断重启）。

运行：python -m pytest tests/test_died_late_escape.py -q
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", os.path.join(".trae", "skills", "jianying-editor", "examples")):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

import jy_instance as ji  # noqa: E402
import robust_auto_export as rae  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class _FakeBackend(ji.Backend):
    def __init__(self, running=False, window=False, home=False):
        self.running = running
        self.window = window
        self.home = home
        self.edit = False
        self.launch_count = 0
        self.kill_count = 0
        self.launch_effect = None

    def process_running(self):
        return self.running

    def window_exists(self):
        return self.window

    def is_home(self):
        return self.home

    def is_edit(self):
        return self.edit

    def launch(self, exe):
        self.launch_count += 1
        if self.launch_effect:
            self.launch_effect()

    def kill(self):
        self.kill_count += 1
        self.running = False
        self.window = False
        self.home = False
        self.edit = False

    def close_edit_to_home(self):
        return True

    def rebind_window(self):
        return object()


def _make(backend, clk, **kw):
    return ji.JyInstance(backend=backend, find_exe=lambda: "fake.exe",
                         time_fn=clk.now, sleep_fn=clk.sleep,
                         poll_interval=1.0, probe_timeout=2.0,
                         log=lambda *_a: None, **kw)


class _FakeExporter:
    """只承载 _get_manager 需要的字段（backend 可注入）。"""

    def __init__(self, backend):
        self._manager = type("M", (), {"backend": backend})()


def test_wait_edit_page_aborts_fast_when_process_dies():
    clk = _Clock()
    calls = {"n": 0}

    def alive():
        calls["n"] += 1
        return calls["n"] < 3  # 第 3 次探测：进程已退出

    with pytest.raises(Exception) as ei:
        rae._wait_edit_page(object(), 200, alive_fn=alive,
                            now_fn=clk.now, sleep_fn=clk.sleep,
                            poll_fn=lambda: (True, False))
    msg = str(ei.value)
    assert "[launch_failed:died_during_open" in msg
    assert "等待编辑页期间剪映进程退出" in msg
    # 快速失败：只耗了 3 个轮询周期，远小于编辑器墙钟 200s
    assert clk.t - 1000.0 <= 3.0


def test_wait_edit_page_keeps_wall_clock_timeout_when_alive():
    clk = _Clock()
    with pytest.raises(Exception) as ei:
        rae._wait_edit_page(object(), 5, alive_fn=lambda: True,
                            now_fn=clk.now, sleep_fn=clk.sleep,
                            poll_fn=lambda: (True, False))
    assert "打开草稿超时" in str(ei.value)
    assert clk.t - 1000.0 == pytest.approx(5.0)


def test_wait_edit_page_returns_elapsed_when_editor_ready():
    clk = _Clock()
    elapsed = rae._wait_edit_page(object(), 5, alive_fn=lambda: True,
                                  now_fn=clk.now, sleep_fn=clk.sleep,
                                  poll_fn=lambda: (True, True))
    assert elapsed == pytest.approx(1.0)


def test_proc_alive_treats_probe_exception_as_alive():
    be = _FakeBackend(running=True)
    exp = _FakeExporter(be)
    assert rae._proc_alive(exp) is True

    be.process_running = lambda: (_ for _ in ()).throw(RuntimeError("psutil 抖动"))
    assert rae._proc_alive(exp) is True

    be.process_running = lambda: False
    assert rae._proc_alive(exp) is False


def test_launch_reconnects_without_spawn_when_process_alive():
    clk = _Clock()
    be = _FakeBackend(running=True, window=True, home=True)
    mgr = _make(be, clk)
    ok = mgr.launch(timeout=10)
    assert ok is True
    assert mgr.state == "home"
    assert be.launch_count == 0  # 防双开：绝不再拉第二个


def test_launch_waits_without_spawn_when_alive_but_not_home():
    clk = _Clock()
    be = _FakeBackend(running=True, window=True, home=False)
    mgr = _make(be, clk)
    ok = mgr.launch(timeout=3)
    assert ok is False
    assert be.launch_count == 0
    assert mgr.state == "launching"


def test_restart_still_spawns_after_kill():
    clk = _Clock()
    be = _FakeBackend(running=True, window=False, home=False)
    be.launch_effect = lambda: (
        setattr(be, "running", True), setattr(be, "window", True),
        setattr(be, "home", True))
    mgr = _make(be, clk)
    ok = mgr.restart(timeout=30)
    assert ok is True
    assert be.kill_count == 1
    assert be.launch_count == 1  # kill 后进程消失 → 正常 spawn
