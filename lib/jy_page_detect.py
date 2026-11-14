# -*- coding: utf-8 -*-
"""jy_page_detect.py — 剪映窗口枚举 / 页面判定 / 可交互校验（唯一实现）。

为什么要单独一层：`ui_core`（业务连接）与 `jy_instance`（启动就绪判定）原先各写
一套判页——前者按 Description 特征，后者按 ClassName 前缀。实测主窗口类名是
`Qt622QWindowIcon` 这类 Qt 通用类时，按类名那份永远判不出首页，启动就绪会一直
等到墙钟超时。本模块把枚举、类名匹配、判页、可交互校验收敛成一份，
两边都调这里；只依赖 ctypes + 懒加载的 uiautomation，不 import ui_core/jy_instance
（避免循环依赖）。
"""
from __future__ import annotations

import ctypes
import time
from typing import Any, Callable, Optional

FULLDESC_PROPERTY_ID = 30159   # LegacyIAccessible.Description

# ── 窗口类名匹配集（可配置：换版本/换 Qt 类名时不改代码）──────────────
DEFAULT_CLASS_PREFIXES = ('homepage_qmltype', 'mainwindow_qmltype', 'jianying')
DEFAULT_CLASS_SUFFIXES = ('qwindowicon',)
CLASS_PREFIXES: list[str] = list(DEFAULT_CLASS_PREFIXES)
CLASS_SUFFIXES: list[str] = list(DEFAULT_CLASS_SUFFIXES)


def set_window_classes(prefixes=None, suffixes=None) -> None:
    """覆盖类名匹配集。传 None 表示该项保持不变；传空序列表示清空该项。"""
    global CLASS_PREFIXES, CLASS_SUFFIXES
    if prefixes is not None:
        CLASS_PREFIXES = [str(p).lower() for p in prefixes]
    if suffixes is not None:
        CLASS_SUFFIXES = [str(s).lower() for s in suffixes]


def get_window_classes() -> dict:
    return {'prefixes': list(CLASS_PREFIXES), 'suffixes': list(CLASS_SUFFIXES)}


def window_class_ok(cls: str) -> bool:
    """类名是否属于剪映主窗口：前缀命中或后缀命中（大小写不敏感）。"""
    c = (cls or '').lower()
    if not c:
        return False
    return (any(c.startswith(p) for p in CLASS_PREFIXES)
            or any(c.endswith(s) for s in CLASS_SUFFIXES))


# ── 页面特征（Description 子串，小写比较）────────────────────────────
DEFAULT_PAGE_FEATS = (
    ('edit', 'maintimelineroot'),    # 编辑页时间线根节点，深度仅 2
    ('edit', 'vesettingpanel'),      # 右侧设置面板展开时才有（辅助特征）
    ('home', 'homepagedrafttitle'),  # 首页草稿卡片
)
PAGE_FEATS: list[tuple[str, str]] = list(DEFAULT_PAGE_FEATS)


def set_page_feats(feats) -> None:
    global PAGE_FEATS
    PAGE_FEATS = [(str(p), str(f).lower()) for p, f in feats]


# ── Win32 枚举 ──────────────────────────────────────────────────────
def enum_top_windows(visible_only: bool = True) -> list[tuple[int, str, str]]:
    """EnumWindows → 顶层窗口 [(hwnd, class_name, title)]。纯 Win32，毫秒级。

    visible_only=False 时包含不可见窗口：仅用于判断「实例还在、窗口正在建」
    （不可见窗口的 UIA 树为空，不能拿来判页/绑定）。
    """
    import ctypes
    from ctypes import wintypes

    out: list[tuple[int, str, str]] = []
    user32 = ctypes.windll.user32
    _Proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    buf = ctypes.create_unicode_buffer(512)
    # 64 位句柄截断防御：不设 argtypes 时 hwnd 默认按 32 位 c_int 传
    user32.EnumWindows.argtypes = [_Proc, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def _cb(hwnd, _lp):
        try:
            if visible_only and not user32.IsWindowVisible(hwnd):
                return True
            buf.value = ''
            user32.GetClassNameW(hwnd, buf, 512)
            cls = buf.value or ''
            buf.value = ''
            user32.GetWindowTextW(hwnd, buf, 512)
            out.append((int(hwnd), cls, buf.value or ''))
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(_Proc(_cb), 0)
    except Exception:
        pass
    return out


def enum_jianying_windows(title_marks=('剪映',),
                          include_invisible: bool = False) -> list[tuple[int, str, str]]:
    """所有剪映真窗口（标题命中 + 类名合格）；首页与编辑页可能同时存在。

    include_invisible=True 时额外返回「类名合格但标题还没就绪（空标题）」的
    不可见窗口：切换草稿时新窗口先创建、后可见/后设标题，这个窗口用来判断
    「实例还在」而不是拿来判页。
    """
    try:
        marks = tuple(title_marks)
        out = []
        for h, c, t in enum_top_windows(visible_only=not include_invisible):
            if not window_class_ok(c):
                continue
            if any(m in (t or '') for m in marks) or (include_invisible and not t):
                out.append((h, c, t))
        return out
    except Exception:
        return []


def jianying_pids() -> set[int]:
    """剪映主进程 pid 集合（psutil 缺失时返回空集，不影响 Win32 枚举）。"""
    try:
        import psutil
        return {p.pid for p in psutil.process_iter(['name'])
                if (p.info.get('name') or '').lower() == 'jianyingpro.exe'}
    except Exception:
        return set()


def window_pid(hwnd: int) -> int:
    """hwnd → 进程 pid（拿不到返回 0）。"""
    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def any_jianying_window() -> bool:
    """是否已存在任意剪映顶层窗口（启动期判「窗口到底有没有建出来」）。

    先按标题+类名认；认不到再按剪映进程 pid 认（标题改名/刚建窗标题未就绪
    时仍能判定"窗口已存在"）。纯 Win32，毫秒级，不走 UIA。
    """
    try:
        if enum_jianying_windows():
            return True
        pids = jianying_pids()
        if not pids:
            return False
        return any(window_pid(h) in pids for h, _c, _t in enum_top_windows())
    except Exception:
        return False


# ── 页面判定 ────────────────────────────────────────────────────────
def ctrl_desc(ctrl) -> str:
    try:
        return str(ctrl.GetPropertyValue(FULLDESC_PROPERTY_ID) or '').lower()
    except Exception:
        return ''


def has_feature(ctrl, feat: str, max_depth: int = 10) -> bool:
    """在 UIA 树里找 Description 含 feat 的节点（深度受限，命中即返回）。"""
    key = str(feat).lower()

    def walk(c, depth):
        if depth > max_depth:
            return False
        if key in ctrl_desc(c):
            return True
        try:
            for ch in c.GetChildren():
                if walk(ch, depth + 1):
                    return True
        except Exception:
            pass
        return False

    return walk(ctrl, 0)


def detect_page(ctrl, known_cls: Optional[str] = None, max_depth: int = 10) -> Optional[str]:
    """判页：Description 特征优先（唯一主口径），类名只作无特征时的兜底提示。

    返回 'edit' / 'home' / None。命中 edit 提前返回，避免首页全树扫描。
    """
    found = {'edit': False, 'home': False}

    def walk(c, depth):
        if depth > max_depth or found['edit']:
            return
        desc = ctrl_desc(c)
        for page, feat in PAGE_FEATS:
            if feat in desc:
                found[page] = True
        if found['edit']:
            return
        try:
            for ch in c.GetChildren():
                walk(ch, depth + 1)
        except Exception:
            pass

    walk(ctrl, 0)
    if found['edit']:
        return 'edit'
    if found['home']:
        return 'home'
    # 无特征（首页为空 / 页面刚加载）：退回类名提示，仍需可交互校验才算就绪
    cls = known_cls or (getattr(ctrl, 'ClassName', '') or '')
    if 'MainWindow' in cls:
        return 'edit'
    if 'HomePage' in cls:
        return 'home'
    return None


# ── 可交互校验 ──────────────────────────────────────────────────────
def _is_draft_card(control, _depth: int = 0) -> bool:
    """首页草稿卡片：Description = HomePageDraftTitle:<草稿名>。"""
    try:
        return str(control.GetPropertyValue(FULLDESC_PROPERTY_ID) or '').startswith(
            'HomePageDraftTitle:')
    except Exception:
        return False


def _enabled(ctrl) -> bool:
    try:
        return bool(ctrl.IsEnabled)
    except Exception:
        return False


def page_interactive(ctrl, page: str, max_depth: int = 8) -> bool:
    """页面是否真的可交互（不能只看类名）。

    home：草稿卡片 → 启用按钮 → 任意文本，三级降级；
    edit：必须有时间线根特征，且能枚举到启用的按钮或文本（避免只有空壳窗口）。
    UIA 阻塞/抛错一律返回 False，由外层线程超时兜底。
    """
    if page == 'home':
        try:
            card = ctrl.TextControl(searchDepth=6, Compare=_is_draft_card)
            if card.Exists(0) and _enabled(card):
                return True
        except Exception:
            pass
        try:
            btn = ctrl.ButtonControl(searchDepth=6)
            if btn.Exists(0) and _enabled(btn):
                return True
        except Exception:
            pass
        try:
            return bool(ctrl.TextControl(searchDepth=6).Exists(0))
        except Exception:
            return False
    if page == 'edit':
        if not has_feature(ctrl, 'maintimelineroot', max_depth=10):
            return False
        for factory in ('ButtonControl', 'TextControl'):
            try:
                c = getattr(ctrl, factory)(searchDepth=max_depth)
                if c.Exists(0) and _enabled(c):
                    return True
            except Exception:
                continue
        # 时间线根已在，但控件枚举不到：仍判不可交互（空壳/假死）
        return False
    return False


# ── 窗口激活（可观测；失败只告警，不参与成败判定）──────────────────
def window_state(hwnd: int) -> dict:
    """窗口当前状态：可见、最小化、前台是谁（用于激活日志）。"""
    st = {'hwnd': int(hwnd or 0), 'visible': None, 'iconic': None,
          'foreground': 0, 'fg_title': '', 'fg_class': ''}
    try:
        user32 = ctypes.windll.user32
        if hwnd:
            st['visible'] = bool(user32.IsWindowVisible(hwnd))
            st['iconic'] = bool(user32.IsIconic(hwnd))
        fg = int(user32.GetForegroundWindow() or 0)
        st['foreground'] = fg
        if fg:
            n = user32.GetWindowTextLengthW(fg)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(fg, buf, n + 1)
                st['fg_title'] = buf.value
            cb = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(fg, cb, 256)
            st['fg_class'] = cb.value
    except Exception:
        pass
    return st


def activate_window(ctrl, hwnd: Optional[int] = None,
                    log: Optional[Callable[[str], None]] = None,
                    topmost: bool = True) -> dict:
    """把窗口还原（若最小化）并尝试置前，记录每一步的真实结果。

    置前顺序（Windows 前台锁下的常见解法）：
      模拟一次 Alt 键 → SW_MINIMIZE → SW_RESTORE → SetActive
      → SetForegroundWindow → SetTopmost(True/False)

    返回 {hwnd, alt, minimize, restored, setactive, setforeground, topmost,
          before, after, in_front}。
    任何一步失败都只写日志，不抛异常、不影响调用方的就绪/成败判定。
    """
    log = log or (lambda _m: None)
    u32 = ctypes.windll.user32
    hwnd = int(hwnd or getattr(ctrl, 'NativeWindowHandle', 0) or 0)
    before = window_state(hwnd)
    rec = {'hwnd': hwnd, 'alt': None, 'minimize': None, 'restored': None,
           'setactive': 'skip', 'setforeground': None,
           'topmost': None, 'before': before, 'after': None, 'in_front': None,
           'steps_s': {}}

    _t_all = time.perf_counter()

    # 已在前台且未最小化：不再重复抢前台。重复执行 minimize→restore +
    # SetTopmost(True/False) 会让窗口明显闪一下（用户看到的「刷新」），而前台
    # 本来就没丢；只有真不在前台/被最小化时才走完整序列。
    if hwnd and not before.get('iconic') and before.get('foreground') == hwnd:
        rec['already_front'] = True
        rec['after'] = before
        rec['in_front'] = True
        rec['total_s'] = round(time.perf_counter() - _t_all, 3)
        log(f"[activate] hwnd={hwnd} 已在前台 → 跳过重复激活（不刷窗口）")
        return rec

    def _timed(name, fn):
        """执行一步并记录耗时（只做观测；异常交回调用处处理）。"""
        _t = time.perf_counter()
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001
            return None, e
        finally:
            rec['steps_s'][name] = round(time.perf_counter() - _t, 3)

    # 1) 模拟一次 Alt 键：解除前台锁（Windows 只在"用户输入"后才允许抢前台）
    if not hwnd:  # 没有有效句柄时不做任何真实输入注入
        rec['alt'] = rec['minimize'] = rec['restored'] = 'skip'
    else:
        def _alt():
            u32.keybd_event(0x12, 0, 0, 0)   # VK_MENU down
            time.sleep(0.05)
            u32.keybd_event(0x12, 0, 2, 0)   # VK_MENU up (KEYEVENTF_KEYUP)

        _r, _e = _timed('alt', _alt)
        rec['alt'] = 'ok' if _e is None else f'err:{_e}'

        # 2) 最小化 → 还原（同时把窗口从"被其他前台窗口压住"的状态里拉出来）
        _r, _e = _timed('minimize', lambda: u32.ShowWindow(hwnd, 6))   # SW_MINIMIZE
        rec['minimize'] = f'err:{_e}' if _e else bool(_r)

        def _restore():
            time.sleep(0.05)
            return u32.ShowWindow(hwnd, 9)   # SW_RESTORE

        _r, _e = _timed('restore', _restore)
        rec['restored'] = f'err:{_e}' if _e else bool(_r)

    _r, _e = _timed('setactive', ctrl.SetActive)
    rec['setactive'] = 'ok' if _e is None else f'err:{type(_e).__name__}:{str(_e)[:60]}'

    _r, _e = _timed('setforeground', lambda: u32.SetForegroundWindow(hwnd))
    rec['setforeground'] = f'err:{_e}' if _e else bool(_r)

    if topmost:
        def _topmost():
            ctrl.SetTopmost(True)
            ctrl.SetTopmost(False)

        _r, _e = _timed('topmost', _topmost)
        rec['topmost'] = True if _e is None else f'err:{_e}'

    after = window_state(hwnd)
    rec['after'] = after
    rec['in_front'] = bool(hwnd and after.get('foreground') == hwnd)
    rec['total_s'] = round(time.perf_counter() - _t_all, 3)
    log(f"[activate] hwnd={hwnd} iconic={before.get('iconic')}->{after.get('iconic')} "
        f"visible={before.get('visible')}->{after.get('visible')} "
        f"fg_before={before.get('foreground')}({before.get('fg_title')!r}) "
        f"fg_after={after.get('foreground')}({after.get('fg_title')!r}) "
        f"alt={rec['alt']} minimize={rec['minimize']} restore={rec['restored']} "
        f"setactive={rec['setactive']} "
        f"setforeground={rec['setforeground']} topmost={rec['topmost']} "
        f"in_front={rec['in_front']} total={rec['total_s']}s {rec['steps_s']}"
        + ("" if rec['in_front'] else "  [warn] 激活未生效（前台锁/权限），仅告警"))
    return rec
