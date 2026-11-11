# -*- coding: utf-8 -*-
"""
窗口基准态管理（Ctrl+F 沉浸全屏）
==================================
实测结论（本机剪映 5.9 / 1920x1080）：
  Ctrl+F **确实生效**，但全屏后的窗口 rect 就是 (0,0,1920,1032)，
  与"最大化"rect 相同 —— 因此不能用 rect.bottom == 1080 来判断全屏。

正确做法：
  · 基准态 rect 由「进全屏后的实测值」记录在 JSON env.fullscreen_rect（=1032）
  · 判断是否在全屏：当前 rect == JSON 记录的基准 rect
  · 进全屏的可靠方式是「重置」：ESC 退出全屏 → Ctrl+F 进入
    （Ctrl+F 是 toggle，若已在全屏再按会退出，所以必须先 ESC 归位）

职责：
  1. enter_fullscreen(win, force_reset)  ESC→Ctrl+F 重置到全屏基准态
  2. is_fullscreen(win)                当前 rect 是否等于 JSON 基准 rect
  3. ensure_fullscreen(win)            阶段守卫：rect 漂移才重进
"""
from __future__ import annotations

import time

import pyautogui
pyautogui.FAILSAFE = False

from ui_core import connect_jianying, get_window_rect, dprint, op_mark


def baseline_rect():
    """JSON 里记录的基准全屏 rect（进全屏后实测值）。没有则 None。"""
    try:
        import ui_calibration
        calib = ui_calibration.load_calibration()
        if calib and ui_calibration.check_env_match(calib):
            r = calib.get('env', {}).get('fullscreen_rect')
            if r and len(r) == 4:
                return tuple(int(v) for v in r)
    except Exception:
        pass
    return None


def is_fullscreen(win):
    """当前窗口是否已处于基准全屏态：rect 与 JSON 基准 rect 完全一致。
    没有基准记录时退化为「窗口贴满屏幕宽、从(0,0)起」。"""
    if win is None:
        return False
    rect = get_window_rect(win)
    if not rect:
        return False
    base = baseline_rect()
    if base:
        return tuple(rect) == base
    import ctypes
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    L, T, R, B = rect
    return (L == 0 and T == 0 and R == sw)


def enter_fullscreen(win, force_reset=False):
    """进入全屏基准态。返回 (win, rect)。

    force_reset=True：无条件 ESC → Ctrl+F 重置（流程开头用，ESC 会取消选中，
                      所以绝不能在流程中段调用）。
    force_reset=False：rect 已等于基准 rect 则零动作，否则重置。

    Ctrl+F 是 toggle：已全屏时再按会退出，所以重置前必须先 ESC 归位。
    """
    cur = get_window_rect(win)
    if not force_reset and is_fullscreen(win):
        dprint(f"  ✅ 已在全屏基准态 rect={cur}")
        return win, cur

    # —— 重置：ESC 退出全屏 → Ctrl+F 进入全屏 ——
    dprint(f"  重置全屏基准态：ESC 退出 → Ctrl+F 进入（当前 rect={cur}）")
    try:
        win.SetActive()
        time.sleep(0.3)
    except Exception:
        pass

    op_mark('按键', 'ESC（退出残留全屏并取消选中）')
    pyautogui.press('escape')       # 退出可能残留的全屏态（并取消选中）
    time.sleep(1.0)
    op_mark('组合键', 'Ctrl+F（进入全屏基准态）')
    pyautogui.hotkey('ctrl', 'f')   # 进入全屏
    time.sleep(1.0)

    win2, _ = connect_jianying(retry=False)
    if win2:
        win = win2
    rect = get_window_rect(win)
    dprint(f"  ✅ 全屏基准态 rect={rect}")
    return win, rect


def ensure_fullscreen(win):
    """阶段守卫：rect 偏离基准态 → 重置全屏。
    注意：这里用 force_reset=False（已在基准态就零动作），
    避免流程中段反复 ESC 取消选中。"""
    win2, _ = connect_jianying(retry=False)
    if win2:
        win = win2
    return enter_fullscreen(win, force_reset=False)[0]
