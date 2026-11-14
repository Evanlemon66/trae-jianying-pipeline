"""R13：窗口定位不依赖中文标题，按进程名 + ClassName 兜底。"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, ".trae", "skills", "jianying-editor", "examples"))

import jy_instance as jy  # noqa: E402


class FakeWindow:
    def __init__(self, pid, cls):
        self.ProcessId = pid
        self.ClassName = cls


def test_pick_main_window_by_process_and_class():
    wins = [
        FakeWindow(1, "Chrome_WidgetWin_1"),
        FakeWindow(2, "MainWindow_QMLTYPE_6263"),   # 别的进程的主窗口
        FakeWindow(3, "MainWindow_QMLTYPE_9999"),   # 剪映，类名随版本变
        FakeWindow(4, "HomePage_QMLTYPE_1"),        # 剪映首页
    ]
    assert jy._pick_main_window(wins, {3}) is wins[2]
    assert jy._pick_main_window(wins, {4}) is wins[3]
    # 剪映进程不在 → 不得拿别的进程窗口冒充
    assert jy._pick_main_window(wins, {99}) is None
    assert jy._pick_main_window([], {3}) is None


def test_window_by_process_returns_none_when_process_not_running(monkeypatch):
    monkeypatch.setattr(jy, "_jianying_pids", lambda: set())

    class FakeUia:
        def GetRootControl(self):  # noqa: N802
            raise AssertionError("进程不在时不应枚举 UIA 顶层窗口")

    assert jy._window_by_process(FakeUia()) is None


def test_window_by_process_swallows_uia_errors(monkeypatch):
    monkeypatch.setattr(jy, "_jianying_pids", lambda: {7})

    class BadUia:
        def GetRootControl(self):  # noqa: N802
            raise RuntimeError("COMError")

    assert jy._window_by_process(BadUia()) is None
