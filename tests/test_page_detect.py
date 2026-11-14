# -*- coding: utf-8 -*-
"""判页/类名口径单测：换版本换了真实类名，这里必须先红。

覆盖：
  - 真实观测到的三类主窗口类名（Qt622QWindowIcon / HomePage_QMLTYPE_* /
    MainWindow_QMLTYPE_*）以及负例（别家应用窗口）；
  - 类名匹配集可配置（换版本不改代码）；
  - 页面判定以 Description 特征为主，类名只作无特征兜底；
  - 编辑页可交互必须有时间线根 + 启用控件（不许只认类名）。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, ".trae", "skills", "jianying-editor", "examples"))

import jy_page_detect as jpd  # noqa: E402
import jy_instance as jy      # noqa: E402

FULLDESC = jpd.FULLDESC_PROPERTY_ID


# ── 假控件（只实现被测代码用到的 UIA 面）─────────────────────────────
class FakeLeaf:
    def __init__(self, exists=True, enabled=True):
        self._exists = exists
        self.IsEnabled = enabled

    def Exists(self, _timeout=0):  # noqa: N802
        return self._exists


class FakeCtrl:
    def __init__(self, desc="", children=(), cls="", enabled=True,
                 has_button=False, has_text=False):
        self._desc = desc
        self._children = list(children)
        self.ClassName = cls
        self.IsEnabled = enabled
        self._has_button = has_button
        self._has_text = has_text

    def GetPropertyValue(self, pid):  # noqa: N802
        if pid == FULLDESC:
            return self._desc
        raise KeyError(pid)

    def GetChildren(self):  # noqa: N802
        return list(self._children)

    def _walk(self):
        yield self
        for c in self._children:
            if isinstance(c, FakeCtrl):
                yield from c._walk()
            else:
                yield c

    def ButtonControl(self, searchDepth=8):  # noqa: N802
        hit = self._has_button or any(
            isinstance(c, FakeCtrl) and c._has_button for c in self._walk())
        return FakeLeaf(exists=hit)

    def TextControl(self, searchDepth=8, Compare=None):  # noqa: N802
        nodes = list(self._walk())
        if Compare is not None:
            # 只有 Description 形如 HomePageDraftTitle:xxx 的节点才算草稿卡片
            return FakeLeaf(exists=any(Compare(n) for n in nodes))
        return FakeLeaf(exists=self._has_text or any(
            isinstance(c, FakeCtrl) and c._has_text for c in nodes))


# ── 类名匹配集 ──────────────────────────────────────────────────────
def test_window_class_ok_real_class_names():
    for cls in ("Qt622QWindowIcon",       # 5.9 实测主窗口类名（Qt 通用名）
                "Qt5152QWindowIcon",      # 换 Qt 版本后仍是同一后缀
                "HomePage_QMLTYPE_123",
                "MainWindow_QMLTYPE_45",
                "JianyingMainWindow"):
        assert jpd.window_class_ok(cls), cls


def test_window_class_ok_rejects_other_apps():
    for cls in ("Chrome_WidgetWin_1", "Notepad", "", None,
                "Qt622QWindowIconX"):   # 后缀差一个字符不算
        assert not jpd.window_class_ok(cls), cls


def test_window_classes_configurable():
    saved = jpd.get_window_classes()
    try:
        jpd.set_window_classes(prefixes=["futurewindow"], suffixes=[])
        assert jpd.window_class_ok("FutureWindow_Main")
        assert not jpd.window_class_ok("Qt622QWindowIcon")
    finally:
        jpd.set_window_classes(prefixes=saved["prefixes"],
                               suffixes=saved["suffixes"])
    assert jpd.window_class_ok("Qt622QWindowIcon")


# ── 页面判定 ────────────────────────────────────────────────────────
def test_detect_page_edit_by_timeline_root():
    win = FakeCtrl(cls="MainWindow_QMLTYPE_45",
                   children=[FakeCtrl("MainTimelineRoot"), FakeCtrl("vue-setting-panel")])
    assert jpd.detect_page(win) == "edit"


def test_detect_page_home_by_draft_title():
    win = FakeCtrl(cls="HomePage_QMLTYPE_123",
                   children=[FakeCtrl("HomePageDraftTitle:测试草稿")])
    assert jpd.detect_page(win) == "home"


def test_detect_page_edit_wins_over_home_in_same_tree():
    win = FakeCtrl(children=[FakeCtrl("HomePageDraftTitle:x"),
                             FakeCtrl("MainTimelineRoot")])
    assert jpd.detect_page(win) == "edit"


def test_detect_page_without_feature_is_none():
    # 页面刚加载、无任何 Description 特征：不得凭类名硬判成编辑页/首页之外的东西
    win = FakeCtrl(cls="Qt622QWindowIcon")
    assert jpd.detect_page(win) is None


def test_detect_page_class_fallback_only_when_no_feature():
    assert jpd.detect_page(FakeCtrl(), known_cls="MainWindow_QMLTYPE_45") == "edit"
    assert jpd.detect_page(FakeCtrl(), known_cls="HomePage_QMLTYPE_1") == "home"
    assert jpd.detect_page(FakeCtrl(), known_cls="Qt622QWindowIcon") is None


# ── 可交互校验 ──────────────────────────────────────────────────────
def test_edit_interactive_requires_timeline_root():
    # 类名像编辑页，但树里没有时间线根 → 不可交互（不许只认类名）
    win = FakeCtrl(cls="MainWindow_QMLTYPE_45", has_button=True, has_text=True)
    assert not jpd.page_interactive(win, "edit")


def test_edit_interactive_requires_enabled_control():
    win = FakeCtrl(cls="MainWindow_QMLTYPE_45",
                   children=[FakeCtrl("MainTimelineRoot")])
    assert not jpd.page_interactive(win, "edit")      # 空壳：没有可枚举控件
    win2 = FakeCtrl(cls="MainWindow_QMLTYPE_45",
                    children=[FakeCtrl("MainTimelineRoot")], has_button=True)
    assert jpd.page_interactive(win2, "edit")


def test_home_interactive_three_level_fallback():
    # 一级：草稿卡片
    with_card = FakeCtrl(desc="", children=[FakeCtrl("HomePageDraftTitle:草稿A")])
    assert jpd.page_interactive(with_card, "home")
    # 二级：启用按钮
    assert jpd.page_interactive(FakeCtrl(has_button=True), "home")
    # 三级：任意文本
    assert jpd.page_interactive(FakeCtrl(has_text=True), "home")
    # 全空：不可交互
    assert not jpd.page_interactive(FakeCtrl(), "home")


# ── 窗口激活可观测（失败只告警，不抛异常、不参与成败判定）──────────
def test_activate_window_logs_steps_and_never_raises():
    lines = []

    class BadCtrl:
        NativeWindowHandle = 0

        def SetActive(self):  # noqa: N802
            raise RuntimeError('SetActive refused')

        def SetTopmost(self, _flag):  # noqa: N802
            raise RuntimeError('no topmost')

    rec = jpd.activate_window(BadCtrl(), log=lines.append)
    assert rec['setactive'].startswith('err:'), rec
    assert str(rec['topmost']).startswith('err:'), rec
    assert rec['in_front'] is False
    joined = '\n'.join(lines)
    assert '[activate]' in joined
    # 激活失败必须在日志里能直接读到原因，且只是告警
    assert '仅告警' in joined
    assert 'SetActive refused' in joined


def test_activate_window_reports_iconic_restore():
    calls = []

    class Ctrl:
        NativeWindowHandle = 0

        def SetActive(self):  # noqa: N802
            calls.append('setactive')

        def SetTopmost(self, _flag):  # noqa: N802
            pass

    rec = jpd.activate_window(Ctrl(), log=lambda _m: None)
    # hwnd=0（无有效句柄）：不注入 Alt、不做最小化/还原，但状态字段必须齐全
    assert rec['restored'] == 'skip'
    assert rec['alt'] == 'skip' and rec['minimize'] == 'skip'
    assert rec['hwnd'] == 0
    assert calls == ['setactive']
    assert set(rec) >= {'hwnd', 'alt', 'minimize', 'restored', 'setactive',
                        'setforeground', 'before', 'after', 'in_front'}


def test_enum_jianying_windows_can_include_untitled_invisible(monkeypatch):
    """include_invisible=True 时，标题还没就绪的不可见剪映窗口也要能枚举到。"""
    def fake(visible_only=True):
        assert visible_only is False
        return [(1, 'Qt622QWindowIcon', ''),          # 刚建、无标题、不可见
                (2, 'Chrome_WidgetWin_1', '剪映'),      # 假窗口：类名不合格
                (3, 'MainWindow_QMLTYPE_260', '剪映专业版')]
    monkeypatch.setattr(jpd, 'enum_top_windows', fake)
    hits = jpd.enum_jianying_windows(include_invisible=True)
    assert [h for h, _c, _t in hits] == [1, 3], hits


def test_activate_window_skips_when_already_foreground(monkeypatch):
    """已在前台且未最小化 → 不做 minimize→restore / SetTopmost 切换（不再刷窗口）。"""
    calls = []

    class Ctrl:
        NativeWindowHandle = 12345

        def SetActive(self):  # noqa: N802
            calls.append('setactive')

        def SetTopmost(self, flag):  # noqa: N802
            calls.append(('topmost', flag))

    monkeypatch.setattr(jpd, 'window_state', lambda _h: {
        'hwnd': 12345, 'visible': True, 'iconic': False,
        'foreground': 12345, 'fg_title': '剪映专业版', 'fg_class': 'Qt622QWindowIcon'})
    rec = jpd.activate_window(Ctrl(), log=lambda _m: None)
    assert rec['already_front'] is True, rec
    assert rec['in_front'] is True, rec
    assert calls == [], calls


def test_jy_instance_delegates_to_shared_detect(monkeypatch):
    backend = jy.UIABackend()
    fake_win = object()
    monkeypatch.setattr(backend, "_main_window", lambda: fake_win)
    # 本用例测的是"判页交给 jy_page_detect"，窗口存在性用桩固定为真
    monkeypatch.setattr(jpd, "any_jianying_window", lambda: True)

    calls = []

    def fake_detect(ctrl, **kw):
        calls.append(("detect", ctrl))
        return "home"

    def fake_interactive(ctrl, page, **kw):
        calls.append(("interactive", ctrl, page))
        return True

    monkeypatch.setattr(jpd, "detect_page", fake_detect)
    monkeypatch.setattr(jpd, "page_interactive", fake_interactive)

    assert backend.is_home() is True
    assert ("detect", fake_win) in calls
    assert ("interactive", fake_win, "home") in calls

    calls.clear()

    def fake_detect_edit(ctrl, **kw):
        calls.append(("detect", ctrl))
        return "edit"

    monkeypatch.setattr(jpd, "detect_page", fake_detect_edit)
    assert backend.is_edit() is True
    assert ("interactive", fake_win, "edit") in calls
