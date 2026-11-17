# -*- coding: utf-8 -*-
"""
人声分离（剪映 VIP 云功能，异步处理）
====================================
流程：
  0. Ctrl+F 全屏基准态（ui_fullscreen.ensure_fullscreen，尺寸漂移自动重进）
  1. 预设轨整理：主轨双重校验(M1最底video+M2封面) → 非主轨 锁定+隐藏
  2. Home 回首帧 → 点主视频片段 → Ctrl+A 全选
  3. 点顶部"音频"Tab（UIA 滚动条锚定，比例兜底）
  4. 滚动面板到底 → UIA 找 PanelSettingsGroup(含 VEComboBox) → 开关在组头部左侧
  5. 像素判状态（蓝=开）→ 关则点击，点击后验证+重试
  6. 轮询：按 End 跳最后一帧 + scan_tracks，新轨道出现即完成
  7. 成功后【不恢复】预设轨（保持锁定+隐藏，恢复会污染后续识别/分离步骤；
     统一恢复点 = [5] 分离音频收尾的 restore_all_tracks）

跨机器说明：
  - 开关/ComboBox 全部走 UIA BoundingRectangle，与分辨率无关
  - 音频Tab 不在 UIA 树：优先用面板右侧 ScrollBar 的 UIA 位置锚定，失败退回比例坐标
  - 点击后像素验证（蓝对勾 (0,193,205)），失败自动重试
"""

from __future__ import annotations

import time
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pyautogui
pyautogui.FAILSAFE = False
from PIL import Image

from ui_core import (
    dprint, connect_jianying,
    send_keys, capture_region, get_window_rect, op_mark, op_stage,
    _resolve_prop, PROPERTY_ID_FULLDESC,
)
from ui_track_ops import (scan_tracks, find_main_track, hide_preset_tracks,
                          unhide_all_tracks, _ui_scale)
from ui_fullscreen import ensure_fullscreen


# ================= UIA 定位 =================

def _iter_tree(ctrl, max_depth=25, _d=0):
    """生成器：遍历 UIA 子树"""
    if _d > max_depth:
        return
    yield ctrl
    try:
        for c in ctrl.GetChildren():
            yield from _iter_tree(c, max_depth, _d + 1)
    except Exception:
        pass


def _rect(ctrl):
    try:
        r = ctrl.BoundingRectangle
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        return None


def find_panel_scrollbar(win):
    """找右侧面板滚动条（音频Tab锚点）。返回 rect 或 None。"""
    wr = get_window_rect(win)
    if not wr:
        return None
    for ctrl in _iter_tree(win, max_depth=12):
        cls = ''
        try:
            cls = ctrl.ClassName or ''
        except Exception:
            pass
        if 'ScrollBar' in cls:
            r = _rect(ctrl)
            # 面板滚动条：贴近窗口右缘、从面板顶部开始
            if r and r[2] >= wr[2] - 30 and r[1] < wr[1] + 120:
                return r
    return None


def click_audio_tab(win, dx=0):
    """
    点顶部"音频"Tab。定位链：OCR"音频"文字 → 滚动条锚定 → 右缘反推。
    dx 为重试时的横移补偿。
    """
    wr = get_window_rect(win)
    ww = wr[2] - wr[0] if wr else 0
    if not wr or ww < 800:
        dprint(f"  ⚠️ 窗口矩形异常 {wr}，重连")
        win, _ = connect_jianying()
        win = ensure_fullscreen(win)
        time.sleep(0.5)
        wr = get_window_rect(win)

    # 1) JSON 坐标优先（准入已校准 → 0ms，跳过 OCR）
    try:
        import ui_calibration
        calib = ui_calibration.load_calibration()
        if calib and ui_calibration.check_env_match(calib):
            at = calib.get('audio_tab') or {}
            if at.get('coord'):
                x, y = int(at['coord'][0]), int(at['coord'][1])
                dprint(f"  音频Tab（JSON coord）: ({x}, {y})")
                op_mark('点击', f'音频Tab({x + dx},{y})')
                pyautogui.click(x + dx, y)
                time.sleep(0.6)
                return
    except Exception:
        pass

    # 2) OCR 兜底（只扫顶部右侧带，排除 Q1 媒体面板同名「音频」）
    #    x 左界 = 右面板内容左缘 −20（4K=2999 / 1920=1374），替代 WW//2：
    #    4K 下 WW//2=1920，那条 1920×180 极端长宽比的裁切实测偶发漏检。
    try:
        from ocr_locate import find_text_in_window
        from ui_calibration import get_param
        region = get_param('audio_tab', 'ocr_region', None)
        if not region:
            from ui_core import panel_edges
            pe = panel_edges(win)
            for w in pe.get('warn') or []:
                dprint(f"  [WARN] 面板边缘: {w}")
            rl = pe.get('right_left')
            h = int(round(120 * _ui_scale(win)))
            if rl is not None:
                vx0 = max(0, int(rl) - wr[0] - 20)
                region = (vx0, 0, ww, h)
                dprint(f"  音频Tab OCR region（右面板内容左缘锚定）: {region} "
                       f"src={pe.get('src', {}).get('right_left')}")
            else:
                region = (ww // 2, 0, ww, h)
                dprint("  [WARN] 右面板内容左缘不可测 → 音频Tab OCR region 退 WW//2")
        pos = find_text_in_window(win, '音频', region=region)
        if pos:
            dprint(f"  音频Tab（OCR）: {pos}")
            op_mark('点击', f'音频Tab({pos[0] + dx},{pos[1]})')
            pyautogui.click(pos[0] + dx, pos[1])
            time.sleep(0.8)
            return
        dprint("  OCR 未找到'音频'Tab，退兜底链")
    except Exception as e:
        dprint(f"  OCR 定位异常（退兜底链）: {str(e)[:60]}")

    # 3) JSON fallback 相对窗口右缘
    try:
        import ui_calibration
        calib = ui_calibration.load_calibration()
        if calib and ui_calibration.check_env_match(calib):
            fb = (calib.get('audio_tab') or {}).get('fallback')
            if fb:
                x, y = wr[2] + int(fb['dx']), wr[1] + int(fb['dy'])
                dprint(f"  音频Tab（JSON fallback）: ({x}, {y})")
                op_mark('点击', f'音频Tab fallback({x + dx},{y})')
                pyautogui.click(x + dx, y)
                time.sleep(0.6)
                return
    except Exception:
        pass

    # 2) 滚动条 UIA 锚定（面板右贴边、固定宽度）
    #    偏移为逻辑量（100% DPI 口径）→ 实际 = 基准 + 偏移×dpi_scale（1920 下等价旧值）
    _s = _ui_scale(win)
    y = wr[1] + int(round(55 * _s))
    sb = find_panel_scrollbar(win)
    if sb:
        x = sb[0] - int(round(433 * _s))
        dprint(f"  音频Tab（滚动条锚定）: ({x}, {y})")
    else:
        # 3) 右缘反推（右面板固定宽度，不能用窗口宽比例）
        x = wr[2] - int(round(452 * _s))
        dprint(f"  音频Tab（右缘反推）: ({x}, {y})")
    op_mark('点击', f'音频Tab 滚动条锚定({x + dx},{y})')
    pyautogui.click(x + dx, y)
    time.sleep(1.0)


_AUDIO_PANEL_KEYWORDS = ('人声分离', '响度统一', '人声美化', '音频降噪', '声道配置', '变声')


def _ocr_audio_panel(win):
    """一次 OCR 同时完成两件事：① 校验右侧是「音频」面板（特征词）② 取「人声分离」标签框。

    原先 panel_is_audio 与 ensure_vocal_on 各扫一次右半屏（合计 ~7s），
    其实两次扫的是同一帧同一区域——合并后 ~2s，且标签框直接复用，
    省掉 ensure_vocal_on 里那次独立 OCR。

    返回 (is_audio: bool, vocal_box or None)；vocal_box 为 (l, t, r, b)。
    """
    try:
        from ocr_locate import _ocr_window
        r = get_window_rect(win)
        if not r:
            return False, None
        WL, WT, WW, WH = r
        hits, vbox = [], None
        for text, _c, _s, b in _ocr_window(win, region=(WW // 2, 0, WW, WH)):
            if any(k in text for k in _AUDIO_PANEL_KEYWORDS):
                hits.append(text)
            if vbox is None and '人声分离' in text and b:
                vbox = b
        if hits:
            dprint(f"  [面板校验] 音频面板特征词: {hits[:4]}")
        return bool(hits), vbox
    except Exception as e:
        dprint(f"  [面板校验] 异常: {str(e)[:60]}")
        return False, None


def panel_is_audio(win) -> bool:
    """OCR 校验当前右侧面板是「音频」面板（外部调用窄封装，内部走合并扫描）。"""
    return _ocr_audio_panel(win)[0]


def find_vocal_anchor(win):
    """
    定位人声分离开关锚点 = (面板左缘 x, 声道配置下拉 top)。
    面板左缘 = 所有 VEComboBox 的最小 left（视频翻译下拉有缩进，左缘列是人声分离/声道配置）；
    声道配置的"无"下拉两种状态下都在 UIA 树且 top 稳定，取左缘列最下一个。
    返回 (left_x, anchor_top) 或 None。
    """
    combos = []
    for ctrl in _iter_tree(win):
        try:
            if 'VEComboBox' in (ctrl.ClassName or ''):
                r = _rect(ctrl)
                if r:
                    combos.append(r)
        except Exception:
            continue
    if not combos:
        return None
    left_x = min(c[0] for c in combos)
    col = sorted([c for c in combos if abs(c[0] - left_x) < 15], key=lambda r: r[1])
    if not col:
        return None
    return left_x, col[-1][1]


# ================= 像素状态判定 =================

def _count_blue(img, x0, y0, x1, y1):
    """统计区域内蓝色对勾像素数（蓝 = G>130 且 B>130 且 R<100），返回 (数量, 质心)"""
    h, w = img.shape[0], img.shape[1]
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return 0, None
    reg = img[y0:y1, x0:x1, :].astype(int)
    mask = (reg[:, :, 1] > 130) & (reg[:, :, 2] > 130) & (reg[:, :, 0] < 100)
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return 0, None
    return len(ys), (x0 + int(xs.mean()), y0 + int(ys.mean()))


# ================= 面板滚动 =================

def _wheel_panel(x, y, notches):
    """在右侧属性面板上发滚轮（正=上滚，负=下滚，单位=1 个滚轮档）。

    为什么不用 pyautogui.scroll：2026-11-15 探针实测，同一坐标下 pyautogui.scroll
    对剪映 Qt 属性面板零位移，ctypes mouse_event(WHEEL) 立即生效；并且
    WM_MOUSEWHEEL 走焦点窗口，必须先 SetCursorPos 把鼠标指针移进面板再发。
    """
    import ctypes
    u = ctypes.windll.user32
    u.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    n = int(notches)
    if not n:
        return
    delta = 120 if n > 0 else -120
    for _ in range(min(abs(n), 30)):
        u.mouse_event(0x0800, 0, 0, delta, 0)   # MOUSEEVENTF_WHEEL
        time.sleep(0.04)


def _scroll_panel_to_bottom(win):
    """滚动音频面板到与准入**完全一致**的终态：先回顶部 2 轮 → 再下滚 1 轮×3。

    准入 scroll_until_vocal_visible 的 37 份历史日志（1920 + 4K）全部在第 1 轮
    下滚即命中「人声分离」——它在面板上部第 3-4 行，不需要盲滚 12 轮到底。
    旧实现盲滚 12×3=36 个 scroll，把整列多压下去 ~39px，导致 calibration 里
    vocal_switch.switch_click_coord 与真实位置长期对不上（只能 OCR 优先）。
    统一终态后 scroll 事件 36 → 5，JSON 坐标偏差 ~39px → ≤2px，可作首选。
    """
    from ui_calibration import get_param
    wr = get_window_rect(win)
    mx = int((wr[0] + wr[2]) * float(get_param('vocal_switch', 'scroll_mx_ratio')))
    # y 锚点必须落在右侧面板可视区内：面板底边≈时间线上沿（本布局 ≈0.49×H），
    # 用 0.30×H 保证在面板内（旧值 0.48×H 会压到分界线/时间线，滚轮打空）。
    my = int(wr[1] + wr[3] * float(get_param('vocal_switch', 'scroll_my_ratio_search')))
    rounds_top = int(get_param('vocal_switch', 'scroll_rounds_top'))
    rounds_each = int(get_param('vocal_switch', 'scroll_rounds_each'))
    up_notches = int(get_param('vocal_switch', 'scroll_notches_top'))
    down_notches = int(get_param('vocal_switch', 'scroll_notches_each'))
    sleep_t = float(get_param('vocal_switch', 'scroll_sleep_after'))
    op_mark('滚轮', f'音频面板回顶部×{rounds_top}+下滚×{rounds_each}({mx},{my})')
    for _ in range(rounds_top):
        _wheel_panel(mx, my, up_notches)
        time.sleep(sleep_t)
    for _ in range(rounds_each):
        _wheel_panel(mx, my, -down_notches)
        time.sleep(sleep_t)


def scroll_until_vocal_visible(win, max_rounds=10):
    """
    滚动音频面板直到右半 OCR 看到「人声分离」。
    先滚回顶部再逐轮向下（否则从底部往下滚永远找不回已滚过的选项）。
    返回 (win, found: bool)
    """
    from ui_calibration import get_param
    from ocr_locate import find_text_in_window
    r = get_window_rect(win)
    if not r:
        return win, False
    WL, WT, WW, WH = r
    mx_ratio = float(get_param('vocal_switch', 'scroll_mx_ratio'))
    my_ratio = float(get_param('vocal_switch', 'scroll_my_ratio_search'))
    mx, my = int(WL + WW * mx_ratio), int(WT + WH * my_ratio)

    # ① 滚回顶部
    top_rounds = int(get_param('vocal_switch', 'scroll_rounds_top'))
    up_notches = int(get_param('vocal_switch', 'scroll_notches_top'))
    sleep_t = float(get_param('vocal_switch', 'scroll_sleep_after'))
    op_mark('滚轮', f'音频面板滚回顶部({mx},{my}) ×{top_rounds}')
    for _ in range(top_rounds):
        _wheel_panel(mx, my, up_notches)
        time.sleep(sleep_t)

    # ② 逐轮向下滚 + OCR 检查
    each_rounds = int(get_param('vocal_switch', 'scroll_rounds_each'))
    down_notches = int(get_param('vocal_switch', 'scroll_notches_each'))
    for i in range(max_rounds):
        op_mark('滚轮', f'音频面板第{i + 1}轮下滚({mx},{my}) ×{each_rounds}')
        for _ in range(each_rounds):
            _wheel_panel(mx, my, -down_notches)
            time.sleep(sleep_t)
        pos = find_text_in_window(win, '人声分离', region=(WW // 2, 0, WW, WH))
        if pos:
            dprint(f"  ✅ 面板已滚到「人声分离」{pos}（第 {i + 1} 轮滚动）")
            return win, True
    dprint(f"  ⚠️ 滚动 {max_rounds} 轮仍未看到「人声分离」")
    return win, False


# ================= 开关操作 =================

VOCAL_CONFIRMED_AT = None   # 开关确认成功时刻；wait_vocal_done 用它起算真实分离耗时


def _vocal_row_box(win, WW=None, WH=None):
    """OCR 定位「人声分离」标签框（绝对屏幕坐标），失败返回 None。

    find_text_box 返回 (left, top, right, bottom) 绝对坐标；region 是窗口相对坐标。
    """
    try:
        from ocr_locate import find_text_box
        if WW is None or WH is None:
            r = get_window_rect(win)
            WW, WH = r[2], r[3]
        return find_text_box(win, '人声分离', region=(WW // 2, 0, WW, WH))
    except Exception:
        return None


def _vocal_checkbox_blue(img, WL, WT, WW, WH, box, s):
    """在「人声分离」行内测蓝勾（开态）。

    搜索窗 = 标签左缘往左 45 逻辑px ~ 标签左缘往右 6 逻辑px，行中心上下 22 逻辑px。
    返回 (蓝像素数, 质心绝对坐标) —— 质心就是复选框实测中心（上一轮实测 (1860,554)）。
    """
    box_left = int(box[0])
    row_cy = (int(box[1]) + int(box[3])) // 2
    x0 = max(0, box_left - WL - int(round(45 * s)))
    x1 = min(int(WW), box_left - WL + int(round(6 * s)))
    y0 = max(0, row_cy - WT - int(round(22 * s)))
    y1 = min(int(WH), row_cy - WT + int(round(22 * s)))
    n, c = _count_blue(img, x0, y0, x1, y1)
    if n and c:
        return n, (int(WL) + c[0], int(WT) + c[1])
    return 0, None


def _vocal_row_shows_processing(win, box):
    """该行右侧是否出现「处理中」或百分比；命中返回关键词，否则 None。"""
    try:
        from ocr_locate import find_text_box
        WL, WT, WW, WH = get_window_rect(win)
        s = _ui_scale(win)
        x0 = max(0, int(box[0]) - WL + int(round(24 * s)))
        y0 = max(0, int(box[1]) - WT - int(round(8 * s)))
        y1 = min(WH, int(box[3]) - WT + int(round(8 * s)))
        if x0 >= WW or y1 <= y0:
            return None
        for kw in ('处理中', '%'):
            if find_text_box(win, kw, region=(x0, y0, WW, y1)):
                return kw
    except Exception:
        return None
    return None


def ensure_vocal_on(win, max_retries=1, vocal_box=None) -> bool:
    """
    确保人声分离开关为开启状态（**单次点击 + 轮询确认**，不重复点）。

    定位（用户口径）：不再用「OCR 文字框 − 4.5px」。
      开态：在标签左侧搜蓝像素簇，取包围盒质心 = 复选框实测中心（上一轮 (1860,554)）；
      关态（灰勾）：用实测几何中心 = 标签左缘 + checkbox_offset(−8 逻辑px)×dpi。
    点击前**必须重扫该行 y**（两次取样，位移 >10 逻辑px 以新值为准），且不使用任何
    历史 JSON 坐标——上一轮 attempt1/2 的 57px 过期坐标由此杜绝。

    点击策略：只点一下。点击前已是蓝勾 → 不点，直接判已开（缓存命中同路径，
    避免把已开的开关点回去；多选下"对勾只反映首段"的风险由后续分离对账兜底）。

    确认：点击后轮询最多 confirm_poll_timeout（默认 20s），命中「蓝勾出现」
    「该行处理中/百分比」「播放器处理中」任一即成功。缓存命中的典型形态是
    "蓝勾出现但无百分比、audioAlg 不新增"——按已开处理，结果正确性交给 extract
    的分离对账三条拦截。

    保留 max_retries 形参兼容调用方，内部固定单次点击。
    """
    global VOCAL_CONFIRMED_AT

    from ui_calibration import get_param, update_param
    BLUE_ON = int(get_param('vocal_switch', 'blue_on_min'))
    CONFIRM_TIMEOUT = float(get_param('vocal_switch', 'confirm_poll_timeout', 20))
    t_start = time.time()
    clicks = 0

    WL, WT, WW, WH = get_window_rect(win)
    s = _ui_scale(win)

    box = vocal_box or _vocal_row_box(win, WW, WH)
    if not box:
        dprint("  ⚠️ 未定位到「人声分离」行，滚动后重试")
        win, _ = scroll_until_vocal_visible(win, max_rounds=4)
        box = _vocal_row_box(win)
    if not box:
        dprint("  ❌ 仍定位不到「人声分离」行 → enable 失败")
        return False

    def _fresh_box():
        """点击前重扫：两次 OCR 取行 y，位移 >10 逻辑px 用新值并记录修正。"""
        nonlocal box
        b1 = _vocal_row_box(win) or box
        y1 = (int(b1[1]) + int(b1[3])) // 2
        time.sleep(0.4)
        b2 = _vocal_row_box(win) or b1
        y2 = (int(b2[1]) + int(b2[3])) // 2
        if abs(y2 - y1) > int(round(10 * s)):
            dprint(f"  📍 点击前行 y 重扫修正：{y1} → {y2}（旧坐标作废）")
        box = b2
        return b2

    box = _fresh_box()
    img = np.array(capture_region(WL, WT, WW, WH))
    n0, c0 = _vocal_checkbox_blue(img, WL, WT, WW, WH, box, s)
    if n0 >= BLUE_ON and c0:
        dprint(f"  ✅ 点击前已是开启状态（蓝勾 {n0}px @{c0}，点击 0 次）")
        if VOCAL_CONFIRMED_AT is None:
            VOCAL_CONFIRMED_AT = time.time()
        update_param('vocal_switch', 'switch_click_coord', [int(c0[0]), int(c0[1])])
        return True

    # 单次点击：关态没有蓝簇，用实测几何偏移（标签左缘 −8 逻辑px ≈ 复选框中心）
    dx = int(round(float(get_param('vocal_switch', 'checkbox_offset', [-8, 0])[0]) * s))
    tx = int(box[0]) + dx
    ty = (int(box[1]) + int(box[3])) // 2
    dprint(f"  点击开关 ({tx}, {ty}) [复选框中心/关态几何]（单次点击，不重试）")
    op_mark('点击', f'人声分离开关({tx},{ty})[单次]')
    pyautogui.click(tx, ty)
    clicks = 1
    try:
        time.sleep(0.15)
        shot = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_vocal_click_single.png')
        Image.fromarray(np.array(capture_region(WL, WT, WW, WH)).astype('uint8')).save(shot)
        dprint(f"  📸 点击后截图: {shot}")
    except Exception as e:
        dprint(f"  ⚠️ 点击后截图失败: {e}")

    # 轮询确认：蓝勾 / 该行处理中或百分比 / 播放器处理中，任一命中即成功
    t0 = time.time()
    last_ocr = 0.0
    while time.time() - t0 < CONFIRM_TIMEOUT:
        time.sleep(0.5)
        win, _ = connect_jianying()
        WL, WT, WW, WH = get_window_rect(win)
        img = np.array(capture_region(WL, WT, WW, WH))
        nb, cb = _vocal_checkbox_blue(img, WL, WT, WW, WH, box, s)
        if nb >= BLUE_ON and cb:
            dprint(f"  ✅ 已确认开启：蓝勾 {nb}px @{cb}"
                   f"（{time.time() - t0:.1f}s，点击 {clicks} 次）")
            if VOCAL_CONFIRMED_AT is None:
                VOCAL_CONFIRMED_AT = time.time()
            update_param('vocal_switch', 'switch_click_coord', [int(tx), int(ty)])
            return True
        if player_status_text(win) == 'processing':
            dprint(f"  ✅ 已确认开启：播放器「处理中」"
                   f"（{time.time() - t0:.1f}s，点击 {clicks} 次）")
            if VOCAL_CONFIRMED_AT is None:
                VOCAL_CONFIRMED_AT = time.time()
            update_param('vocal_switch', 'switch_click_coord', [int(tx), int(ty)])
            return True
        if time.time() - last_ocr >= 2.0:
            last_ocr = time.time()
            fresh = _vocal_row_box(win)
            if fresh:
                oy = (int(box[1]) + int(box[3])) // 2
                ny = (int(fresh[1]) + int(fresh[3])) // 2
                if abs(ny - oy) > int(round(10 * s)):
                    dprint(f"  📍 确认中行 y 漂移 {oy} → {ny}，按新位置判断")
                    box = fresh
            kw = _vocal_row_shows_processing(win, box)
            if kw:
                dprint(f"  ✅ 已确认开启：该行出现「{kw}」"
                       f"（{time.time() - t0:.1f}s，点击 {clicks} 次）")
                if VOCAL_CONFIRMED_AT is None:
                    VOCAL_CONFIRMED_AT = time.time()
                update_param('vocal_switch', 'switch_click_coord', [int(tx), int(ty)])
                return True
    dprint(f"  ❌ 单次点击后 {int(CONFIRM_TIMEOUT)}s 内未见蓝勾/处理中/百分比 → enable 失败"
           f"（点击 {clicks} 次，总耗时 {time.time() - t_start:.1f}s）")
    return False


def find_vocal_switch(win):
    """
    定位人声分离开关对勾坐标（窗口右半部分 OCR 语义锚定，分辨率无关）。
    对勾 = 「人声分离」文字框左侧偏移（JSON vocal_switch.checkbox_offset，默认 [-25, 0]）。
    纯定位不点击——准入测试 locate 阶段专用（找到即停，等肉眼确认）。
    返回 (x, y) 绝对屏幕坐标；找不到返回 None。
    注意：人声分离文字可能在 Q2（y≈400，未滚到底）或 Q4（滚到底后下移），
    所以只用 x >= WW//2（窗口右半）约束，不限制 y。
    """
    try:
        from ocr_locate import find_text_box
        from ui_calibration import get_param
        r = get_window_rect(win)
        if not r:
            return None
        WL, WT, WW, WH = r
        # 始终实时 OCR（准入校准专用路径）：面板滚动/展开会使历史坐标漂移，不可作首选
        box = find_text_box(win, '人声分离', region=(WW // 2, 0, WW, WH))
        if not box:
            return None
        s = _ui_scale(win)
        dx, dy = get_param('vocal_switch', 'checkbox_offset', [-8, 0])
        vx = int(box[0] + int(round(dx * s)))
        vy = (box[1] + box[3]) // 2 + int(round(dy * s))
        dprint(f"  [ocr] 右半 '人声分离' box={box} → 对勾 ({vx}, {vy})")
        return (vx, vy)
    except Exception as e:
        dprint(f"  find_vocal_switch 异常: {str(e)[:60]}")
        return None


# ===== 人声模式下拉锚点（1920 物理基准；使用时 ×s，s=1 与旧值逐像素相同）=====
_VOCAL_LIST_Y = (250, 700)      # 下拉区 OCR 的窗口相对 y 范围（4K@150% → 375..1050）
_VOCAL_ARROW_DX = -148          # 箭头 x = 窗口右缘 + dx×s
_VOCAL_ARROW_DY = -4            # 箭头 y = 当前值文字 y + dy×s


def _vocal_s(win) -> float:
    try:
        return float(_ui_scale(win)) or 1.0
    except Exception:
        return 1.0


def _vocal_list_region(win):
    """下拉区 OCR 区域（窗口相对 x0,y0,x1,y1）。y 按 UI 缩放 ×s。"""
    from ui_calibration import get_param
    r = get_window_rect(win)
    if not r:
        return None
    _wl, _wt, wr, _wb = r
    s = _vocal_s(win)
    y_range = get_param('vocal_switch', 'list_region_y')
    return (wr // 2, int(round(y_range[0] * s)),
            wr, int(round(y_range[1] * s)))


def _uia_vocal_items(win, deadline_s=2.0):
    """一级判定：UIA 是否直接暴露「仅保留人声」「仅保留背景声」（= 列表真的展开）。
    返回 [(关键词, rect)]；QML 下拉未暴露时为 []。
    deadline_s：全树枚举限时。剪映 5.9 的 UIA 树随轨道增多可达 8-10s，
    超过预算就放弃一级判定、交给二级局部 OCR（更快且判定等价）。"""
    out = []
    t_end = time.time() + float(deadline_s)
    truncated = False
    for ctrl in _iter_tree(win):
        if time.time() > t_end:
            truncated = True
            break
        txt = ''
        try:
            txt = ctrl.Name or ''
        except Exception:
            pass
        try:
            txt += ' ' + (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or '')
        except Exception:
            pass
        for kw in ('仅保留人声', '仅保留背景声'):
            if kw in txt:
                rr = _rect(ctrl)
                if rr:
                    out.append((kw, rr))
                break
    if truncated:
        dprint(f"  [uia] 枚举超 {deadline_s:.1f}s 提前结束（已命中 {len(out)} 项），转二级 OCR")
    return out


def read_vocal_mode_current(win):
    """OCR 读人声分离下拉框【当前值】。收起状态下拉区只渲染当前值一项。
    返回 'vocal' / 'background' / None（展开中或未识别）。"""
    from ocr_locate import find_all_texts
    REGION = _vocal_list_region(win)
    if not REGION:
        return None
    items = find_all_texts(win, region=REGION)
    has_v = any('仅保留人声' in txt for (_x, _y, txt, _s) in items)
    has_b = any('仅保留背景声' in txt for (_x, _y, txt, _s) in items)
    if has_v and has_b:        # 两项同现 = 下拉处于展开态，读不出当前值
        return None
    if has_v:
        return 'vocal'
    if has_b:
        return 'background'
    return None


def select_vocal_mode(win, mode='vocal', max_retry=3) -> bool:
    """
    人声分离下拉选择「仅保留人声」或「仅保留背景声」。

    职责：纯 UI 操作（展开下拉 → 点目标列表项 → 收起）。
    时序（用户明确）：勾选人声分离后**立即**调本函数选模式，选完再等分离完成。
    全程只有**一次**分离，不存在二次分离。

    参数：
      mode: 'vocal'     → 选「仅保留人声」
            'background' → 选「仅保留背景声」

    展开校验：「仅保留人声」和「仅保留背景声」同时出现（两个选项都渲染出来）。
    """
    assert mode in ('vocal', 'background'), f"mode 必须是 'vocal' 或 'background'，Got {mode!r}"
    TARGET = '仅保留人声' if mode == 'vocal' else '仅保留背景声'
    OTHER = '仅保留背景声' if mode == 'vocal' else '仅保留人声'

    from ocr_locate import find_all_texts
    from ui_calibration import get_param, update_param
    r = get_window_rect(win)
    if not r:
        return False
    WL, WT, WW, WH = r
    _sticky_win = win   # 缓存窗口句柄（不每次重连，避免 SetTopmost 关掉下拉）

    # 下拉区：只扫右半面板的窄条（全窗口 6.5s → 窄条约 0.8s；y 按 UI 缩放 ×s）
    REGION = _vocal_list_region(win)
    if not REGION:
        return False

    def _conn():
        nonlocal _sticky_win, WL, WT, WW, WH, REGION
        try:
            if _sticky_win and _sticky_win.Exists(0):
                return _sticky_win
        except Exception:
            pass
        try:
            w2, _ = connect_jianying(retry=False)
            if w2:
                _sticky_win = w2
                r2 = get_window_rect(w2)
                if r2:
                    WL, WT, WW, WH = r2
                    REGION = _vocal_list_region(w2)
                return w2
        except Exception:
            pass
        return _sticky_win

    def _scan():
        """一次 OCR 拿全部文字，本地过滤出 TARGET / OTHER 的命中点。"""
        cur = _conn()
        if cur is None:
            return [], []
        items = find_all_texts(cur, region=REGION)
        t = [(x, y) for (x, y, txt, _s) in items if TARGET in txt]
        o = [(x, y) for (x, y, txt, _s) in items if OTHER in txt]
        return t, o

    def _collapse():
        """收起下拉：点箭头 toggle（坐标已知）。
        ⚠️ 绝不能用 ESC——剪映里 ESC 会退出全屏，之后所有基于全屏 rect
        的坐标（轨道 y、面板 x）全部失效，收尾必然失败。"""
        op_mark('点击', f'人声模式下拉箭头({arrow[0]},{arrow[1]}) 收起')
        pyautogui.click(arrow[0], arrow[1])
        time.sleep(0.4)
        a2, b2 = _scan()
        if a2 and b2:                # 仍展开 → 再点一次
            pyautogui.click(arrow[0], arrow[1])
            time.sleep(0.3)
        return True

    # 等下拉框渲染：未展开时只显示当前值（默认「仅保留人声」），
    # 所以只要 TARGET / OTHER 任一出现就说明下拉框已渲染，可点箭头。
    dprint(f"  等下拉框渲染（当前值可能是「{OTHER}」）")
    t_hits, o_hits = [], []
    t0 = time.time()
    while time.time() - t0 < 30:
        t_hits, o_hits = _scan()
        if t_hits or o_hits:
            break
        time.sleep(1)
    if not (t_hits or o_hits):
        dprint("  ⚠️ 下拉框未渲染（人声/背景声文本均未见）")
        return False

    # 展开下拉（3种点击方式依次降级）
    import ctypes
    _PUL = ctypes.windll.user32
    _MOD_NAMES = ((0x11, 'Ctrl'), (0x10, 'Shift'), (0x12, 'Alt'))
    # 箭头 = 下拉行右端：dx/dy 走 JSON（1920 逻辑基准，使用时 ×s）
    s = _vocal_s(win)
    adx = int(get_param('vocal_switch', 'arrow_dx', _VOCAL_ARROW_DX))
    ady = int(get_param('vocal_switch', 'arrow_dy', _VOCAL_ARROW_DY))
    cur_hits = t_hits or o_hits
    top = min(cur_hits, key=lambda h: h[1])
    arrow = (WL + WW + int(round(adx * s)), top[1] + int(round(ady * s)))
    expanded = False
    target = None

    for attempt in range(3):
        # 修饰键卡住检查 + 强制释放
        stuck = [nm for vk, nm in _MOD_NAMES if _PUL.GetAsyncKeyState(vk) & 0x8000]
        if stuck:
            dprint(f"  [diag] 修饰键卡住={stuck}，强制 keyUp 释放")
            for vk, _nm in _MOD_NAMES:
                _PUL.keybd_event(vk, 0, 0x0002, 0)
            time.sleep(0.2)
        dprint(f"  ① 点下拉箭头展开 {arrow}（尝试 {attempt + 1}/3）")
        op_mark('点击', f'人声模式下拉箭头({arrow[0]},{arrow[1]}) 展开尝试{attempt + 1}')
        if attempt == 0:
            pyautogui.click(arrow[0], arrow[1])
        elif attempt == 1:
            _PUL.SetCursorPos(int(arrow[0]), int(arrow[1]))
            time.sleep(0.1)
            _PUL.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.03)
            _PUL.mouse_event(0x0004, 0, 0, 0, 0)
        else:
            pyautogui.moveTo(arrow[0], arrow[1])
            time.sleep(0.2)
            pyautogui.click(arrow[0], arrow[1])
        time.sleep(0.8)

        # 展开成功 = 点击后列表真的出现（一级 UIA → 二级全宽带 OCR；三级降级交叉校验 ±20）
        cur_win = _conn()
        uia_items = _uia_vocal_items(cur_win) if cur_win is not None else []
        uia_t = [rr for kw, rr in uia_items if kw == TARGET]
        uia_o = [rr for kw, rr in uia_items if kw == OTHER]
        if uia_t and uia_o:
            expanded = True
            tg = max(uia_t, key=lambda rr: rr[1])
            target = ((tg[0] + tg[2]) // 2, (tg[1] + tg[3]) // 2)
            dprint(f"  ✅ 下拉已展开（一级 UIA：{TARGET}×{len(uia_t)} + {OTHER}×{len(uia_o)}）")
            break

        a, b = _scan()
        if a and b:
            # 三级降级：命中来自 OCR（箭头锚点是窗口右缘比例推导，非 UIA 实测）
            # → 列表项 x 必须与收起态当前值 x 在 ±20 内（同一列），对不上就报错，不许猜
            cur_x = top[0]
            if min(abs(a[0][0] - cur_x), abs(b[0][0] - cur_x)) > 20:
                raise RuntimeError(
                    f'下拉展开校验失败：列表项 x={a[0][0]}/{b[0][0]} 与当前值 x={cur_x} 偏差 >20')
            expanded = True
            dprint(f"  ✅ 下拉已展开（二级 OCR：{TARGET}×{len(a)} + {OTHER}×{len(b)}）")
            break
        dprint(f"  ⚠️ 展开校验未通过（{TARGET}×{len(a)} / {OTHER}×{len(b)}），收起重试")
        _collapse()
    if not expanded:
        dprint("  ⚠️ 下拉展开失败")
        return False

    # ② 点列表项：目标关键词的 y 最大命中（列表项在下方）——复用刚才那次 OCR
    if target is None:
        a, _b = _scan()
        if not a:
            a, _b = _scan()          # 展开短暂收起时再扫一次
        if not a:
            dprint("  ⚠️ 展开后目标项消失")
            return False
        target = max(a, key=lambda h: h[1])
    dprint(f"  ② 点列表项({target[0]}, {target[1]}) 选「{TARGET}」")
    op_mark('点击', f'人声模式下拉列表项({target[0]},{target[1]}) 选「{TARGET}」')
    pyautogui.click(target[0], target[1])
    time.sleep(0.8)
    # 选中结果落盘：逻辑偏移（÷s），换分辨率可直接换算
    update_param('vocal_switch', 'list_dy', int(round((target[1] - arrow[1]) / s)))

    # ③ 收起下拉（点箭头 toggle，不用 ESC——ESC 会退出全屏）
    _collapse()
    return True


def player_status_text(win, debug=False) -> str:
    """
    判断人声分离处理状态的**唯一可信信号**（用户指定）：
      处理中 → 预览区标题栏显示「人声分离等待处理...」  → 'processing'
      已完成 → 标题恢复「播放器」                      → 'idle'
      其他   → 'unknown'
    ⚠️ 只裁预览区标题栏条带（窗口右半、顶部 y∈[10,110]）做 OCR，
       避免整窗口 OCR 的噪声（时间线轨名、面板项等）误判。
    ⚠️ 禁止用「轨道数量/轨道类型」判断完成 —— 用户已明确否定。
    """
    try:
        from ocr_locate import ocr_image
        r = get_window_rect(win)
        if not r:
            return 'unknown'
        WL, WT, WW, WH = r
        img = np.array(capture_region(WL, WT, WW, WH))
        # 预览器标题（「播放器」/「人声分离处理中 x%」）固定在**预览器左上角**：
        # 属性面板展开时 x≈853，收起时 x≈1193 —— 都在窗口中央带，不能裁 x≥960（实测漏掉「播放器」）。
        # 条带取 y∈[25,115]、x∈[WW//3, WW]：覆盖两种布局，右上工具栏文字不含关键词，无干扰。
        band = img[25:115, WW // 3:, :3]
        joined = ' '.join(t for t, _c, _s, _b in ocr_image(band))
        if debug:
            dprint(f"  [status] 标题栏 OCR = {joined[:140]!r}")
        if any(k in joined for k in ('等待处理', '处理中', '正在处理', '分离中', '正在分离')):
            return 'processing'
        if '播放器' in joined:
            return 'idle'
        return 'unknown'
    except Exception as e:
        dprint(f"  player_status_text 异常: {str(e)[:60]}")
        return 'unknown'


def wait_vocal_done(win, timeout=600, appear_timeout=120) -> dict:
    """
    等「人声分离等待处理」→「播放器」。
      阶段A：按 End 跳最后一帧触发刷新，轮询等 'processing' 出现（appear_timeout）
      阶段B：等 'idle'（标题栏恢复「播放器」= 处理完成）

    判据：
      - 见到「处理中」且随后恢复「播放器」→ ok=True（reason='done'）。
      - 见到「处理中」但超时仍未恢复「播放器」→ ok=False（reason='timeout'）。
      - 开关已确认开启、且在 cache_hit_window（默认 30s，上限 60s）内始终没有
        「处理中」提示 → 判定算法缓存命中，ok=True（reason='cache_hit'），
        不等满 120s。缓存命中的正确性由 extract 的分离对账三条兜底：
        草稿里没出 audio 轨时 _check_separation 会直接失败。
      - 开关未确认开启且全程无「处理中」→ ok=False（reason='never_started'）。
    返回 {'ok','seen_processing','reason','elapsed_s'}
    """
    from ui_core import send_keys
    from ui_calibration import get_param
    global VOCAL_CONFIRMED_AT
    t0 = time.time()
    # 缓存命中观察窗：足够看到「等待处理」提示（正常 1-3s 内出现），又不拖长重跑耗时
    try:
        cache_window = float(get_param('vocal_switch', 'cache_hit_window', 30))
    except Exception:
        cache_window = 30.0
    cache_window = max(10.0, min(60.0, cache_window))
    switch_on = VOCAL_CONFIRMED_AT is not None
    # 真实分离耗时从「开关确认成功」起算（含排队等待），而非等待阶段自己开始观察的时刻
    anchor = VOCAL_CONFIRMED_AT if VOCAL_CONFIRMED_AT else t0
    if VOCAL_CONFIRMED_AT:
        dprint(f"  [计时] 真实耗时自开关确认时刻起算（已过 {int(t0 - anchor)}s）")
    seen = False

    dprint(f"  验证A：按 End 跳末帧，等「人声分离等待处理」提示（最多 {appear_timeout}s）")
    while time.time() - t0 < appear_timeout:
        win, _ = connect_jianying()
        op_mark('按键', 'End（跳末帧触发刷新）')
        send_keys(win, '{End}')
        time.sleep(1.2)
        st = player_status_text(win)
        if st == 'processing':
            seen = True
            dprint(f"  ✅ 已出现「人声分离等待处理」（{int(time.time() - t0)}s）")
            try:
                r = get_window_rect(win)
                img = np.array(capture_region(r[0], r[1], r[2] - r[0], r[3] - r[1]))
                Image.fromarray(img.astype('uint8')).save(
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 '_vocal_processing_capture.png'))
            except Exception:
                pass
            break
        el = time.time() - t0
        if switch_on and el >= cache_window:
            dprint(f"  ✅ 判定缓存命中：开关已确认开启，{int(el)}s 内无「人声分离等待处理」"
                   f"（窗口 {int(cache_window)}s）→ 结果已在缓存，继续流程")
            dprint("     兜底：extract 分离对账三条会核验草稿是否真的新增了 audio 轨")
            return {'ok': True, 'seen_processing': False, 'reason': 'cache_hit',
                    'elapsed_s': int(time.time() - anchor)}
        time.sleep(2)
    if not seen:
        dprint(f"  ❌ {appear_timeout}s 内从未出现「人声分离等待处理」→ 判定从未开始处理，"
               f"不能按已完成处理")
        return {'ok': False, 'seen_processing': False, 'reason': 'never_started',
                'elapsed_s': int(time.time() - t0)}

    dprint(f"  验证B：等标题栏恢复「播放器」（最多 {timeout}s）")
    t1 = time.time()
    while time.time() - t1 < timeout:
        time.sleep(5)
        win, _ = connect_jianying()
        op_mark('按键', 'End（轮询末帧查完成）')
        send_keys(win, '{End}')
        time.sleep(1.0)
        st = player_status_text(win)
        if st == 'idle':
            true_elapsed = int(time.time() - anchor)
            dprint(f"  ✅ 处理完成（真实耗时 {true_elapsed}s，自开关确认起算；"
                   f"等待阶段观察耗时 {int(time.time() - t0)}s）")
            return {'ok': True, 'seen_processing': True, 'reason': 'done',
                    'elapsed_s': true_elapsed}
        el = int(time.time() - t1)
        if el % 30 < 6:
            dprint(f"  ...处理中（{el}s）status={st}")
    dprint(f"  ❌ 已确认开始处理，但 {timeout}s 内未恢复「播放器」→ 判定未完成")
    return {'ok': False, 'seen_processing': True, 'reason': 'timeout',
            'elapsed_s': int(time.time() - t0)}


def wait_timeline_ready(win, timeout=90):
    """等时间线渲染完成（草稿刚打开时剪映懒加载，轨道行可能几十秒后才可扫到）"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            win, _ = connect_jianying()
            rows, *_ = scan_tracks(win)
            if rows:
                dprint(f"  时间线已渲染（{len(rows)} 行，{int(time.time() - t0)}s）")
                return win, True
        except Exception:
            pass
        time.sleep(2)
    dprint(f"  ⚠️ 等 {timeout}s 时间线仍未渲染")
    return win, False


# ================= 完成检测 =================

def player_processing_text(win, debug=False):
    """
    检测界面是否有"人声分离处理中"类提示文字。
    处理未完成时，按 End 跳到最后一帧，播放器上方会显示人声分离相关提示；
    处理完成后提示消失。

    纯 UIA 全树搜索，无任何坐标过滤——已实测面板的"人声分离"标签
    是 QML 直绘（QQuickText，Name 为空），不进 UIA 树，
    因此任何 Name/FullDescription 命中关键词的节点只可能是处理提示 toast。
    返回找到的文字（处理中）或 None（已完成）。
    """
    KEYWORDS = ('人声分离', '处理中', '正在处理', '分离中', '正在分离', '加载中')
    for ctrl in _iter_tree(win, max_depth=25):
        try:
            name = ctrl.Name or ''
        except Exception:
            name = ''
        if name.strip() and any(k in name for k in KEYWORDS):
            return name
        try:
            desc = ctrl.GetPropertyValue(30159) or ''
        except Exception:
            continue
        if desc and any(k in desc for k in KEYWORDS):
            return desc
    return None


# ================= 主流程 =================

def _vocal_alg_artifacts():
    """人声分离的结果侧证据：当前草稿 Resources/audioAlg 下的算法产物（文件数, 总MB）。

    剪映对每段主轨片段跑一次人声分离会在该目录落缓存文件（人声+背景声各一个，
    即每段约 2 个文件）；云缓存秒回同样命中产物。
    「处理中」提示在 2h 长草稿上可能全程不出现（旧逻辑据此直接判完成 = 假绿，
    run6 草稿 audios=0 也放行），所以完成判定必须落到这个目录上。
    """
    try:
        from ui_recognize_translate import DRAFT_ROOT, _active_draft_name
        name = _active_draft_name()
        if not name:
            return 0, 0.0
        alg_dir = os.path.join(DRAFT_ROOT, name, 'Resources', 'audioAlg')
        if not os.path.isdir(alg_dir):
            return 0, 0.0
        n, total = 0, 0
        for root_dir, _dirs, files in os.walk(alg_dir):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root_dir, f))
                    n += 1
                except OSError:
                    pass
        return n, total / 1048576.0
    except Exception:
        return 0, 0.0


def _draft_video_segment_count():
    """当前草稿主轨视频段数（人声分离的预期产物基数）。

    剪映对每段主轨视频做一次人声分离，audioAlg 产物数 ≈ 段数 × 2（人声+背景声）。
    读 draft_content.json 所有 type=video 轨道中 segments 数最多的那条（预设轨各 1 段，
    主轨 80 段，取 max 即主轨，避免把预设轨算入 expected）。
    """
    try:
        from ui_recognize_translate import DRAFT_ROOT, _active_draft_name
        name = _active_draft_name()
        if not name:
            return 0
        p = os.path.join(DRAFT_ROOT, name, 'draft_content.json')
        if not os.path.isfile(p):
            return 0
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        # 取 segments 数最多的 video 轨 = 主轨（预设轨各 1 段，主轨 80 段）
        video_segs = [len(t.get('segments', []))
                      for t in d.get('tracks', [])
                      if t.get('type') == 'video']
        if not video_segs:
            return 0
        return max(video_segs)
    except Exception:
        return 0


def _vocal_separation_progress():
    """人声分离进度（结果侧）。

    返回 (count_ok: bool, files: int, expected: int, mb: float)。
    count_ok = 文件数 ≥ 主轨视频段数（expected=0 时退化为 True，只看稳定性）。
    """
    n, mb = _vocal_alg_artifacts()
    expected = _draft_video_segment_count()
    count_ok = (expected == 0) or (n >= expected)
    return count_ok, n, expected, mb


def _wait_vocal_done_by_artifacts(timeout_sec: int, label: str = ''):
    """轮询 audioAlg 产物直到分离真正完成（30s 不再增长）或超时。

    产物停止增长 = 分离真正结束。run8 教训：13/162 就被放行 → 必须等 30s 稳定。
    expected = 主轨 video segments × 2（人声+背景声各一个产物，精确值）。

    返回 (done: bool, files: int, expected: int)。
    """
    t_wait = time.time()
    prev_n = None
    stable_since = None
    last_n = 0
    video_segs = _draft_video_segment_count()
    expected = video_segs * 2   # 每段产人声+背景声 2 个文件
    while time.time() - t_wait < timeout_sec:
        if expected <= 0:
            video_segs = _draft_video_segment_count()
            expected = video_segs * 2
        n, mb = _vocal_alg_artifacts()
        if n != prev_n:
            stable_since = None   # 还在增长，重置稳定计时
        elif stable_since is None and n > 0:
            stable_since = time.time()
        prev_n = n
        last_n = n
        stable_ok = (stable_since is not None) and (time.time() - stable_since >= 30)
        if stable_ok and expected > 0 and n >= expected:
            dprint(f"  ✅ 分离产物完整且稳定 30s：{n} 个文件，"
                   f"{mb:.0f}MB（expected={expected} = {video_segs}段 × 2）")
            return True, n, expected
        elapsed = int(time.time() - t_wait)
        if elapsed % 30 < 6:
            dprint(f"  ...等待分离产物（{elapsed}s）: {n}/{expected} 个, {mb:.0f}MB"
                   f"{'（增长中）' if stable_since is None else '（稳定中待30s）'}")
        time.sleep(10)
    _, mb = _vocal_alg_artifacts()
    dprint(f"  ⏰ 等待分离产物超时（{timeout_sec}s）: {last_n}/{expected} 个, {mb:.0f}MB")
    return False, last_n, expected


def run_vocal_separate(workdir: str = ".", timeout_sec: int = 300,
                       vocal_mode: str = 'none') -> dict:
    from ui_calibration import get_param
    op_stage('2 UI初始化')
    t0 = time.time()
    os.makedirs(os.path.abspath(workdir), exist_ok=True)

    dprint("=" * 60)
    dprint("[3] 人声分离 - 开始")
    dprint("=" * 60)

    win, page = connect_jianying()
    if not win:
        return {'ok': False, 'duration_s': 0, 'error': '未找到剪映窗口'}

    win = ensure_fullscreen(win)

    from ui_verify import check_layout

    # Step 0: 等时间线渲染（草稿刚打开时控件懒加载）
    dprint("\n[0/5] 等待时间线渲染")
    win, ready = wait_timeline_ready(win, timeout=90)
    if not ready:
        # 判不出 edit 页/未渲染：布局自检只记 WARN 跳过，不改变原有失败行为
        try:
            check_layout(workdir, win=win, page=page)
        except Exception:
            pass
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': 'timeline not rendered'}
    win = ensure_fullscreen(win)  # 渲染后窗口矩形可能变化，重回全屏基准态

    # 命中即自检：进入编辑页、动任何 UI 之前，实测布局 vs calibration final_layout
    check_layout(workdir, win=win, page=page)

    # Step 1: 预设轨整理（主轨双重校验 → 非主轨 锁定+隐藏 → 主轨保可见）
    dprint("\n[1/5] 预设轨 锁定+隐藏（双重校验主轨）")
    hide_preset_tracks(win)
    time.sleep(0.5)

    # Step 2: Home 回首帧 → Ctrl+A 全选（Home+Ctrl+A 始终可靠，无需显式点主轨片段）
    dprint("\n[2/5] Home 回首帧 + Ctrl+A 全选")
    op_mark('按键', 'Home（回首帧）')
    send_keys(win, '{Home}')   # 标准前置：回 00:00 首帧
    time.sleep(0.5)
    # 注意：必须用 pyautogui 真实按键——SendKeys('^a') 会被剪映 QML 时间线忽略
    # （实测 SendKeys 只选中1个片段，pyautogui 才全选）
    op_mark('组合键', 'Ctrl+A（全选）')
    pyautogui.hotkey('ctrl', 'a')
    time.sleep(1.5)  # 等面板渲染（懒加载）

    # Step 3+4: 切"音频"Tab（点击-验证闭环：OCR校验面板含"人声分离"才算切对）
    # （阶段边界：UI初始化=连接/全屏/等渲染/轨道整理/主轨选中；从这里起=人声分离）
    op_stage('3 人声分离')
    dprint("\n[3/5] 切换到音频Tab + 开启人声分离")
    enabled = False
    for k, dx in enumerate([int(round(v * _ui_scale(win)))
                            for v in (0, -70, 70, -140)]):
        if k > 0:
            dprint(f"  ⚠️ 面板校验失败，横移 {dx}px 重新点音频Tab")
        win, _ = connect_jianying()
        click_audio_tab(win, dx=dx)

        win, _ = connect_jianying()
        _scroll_panel_to_bottom(win)

        win, _ = connect_jianying()
        ok_panel, vbox = _ocr_audio_panel(win)
        if ok_panel:
            enabled = ensure_vocal_on(win, vocal_box=vbox)
            break
    if not enabled:
        dprint("  ❌ 人声分离开启失败")
        win, _ = connect_jianying()
        unhide_all_tracks(win)
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': 'failed to enable vocal separation'}

    # Step 4.5: 勾选成功后【立即】选下拉模式（人声/背景声）——只有一次分离
    #   用户明确：勾选 → 选模式 → 再等分离完成；不存在二次分离。
    if vocal_mode != 'none':
        label = '仅保留人声' if vocal_mode == 'vocal' else '仅保留背景声'
        dprint(f"\n[4.5/5] 下拉选「{label}」")
        win, _ = connect_jianying()
        selected = select_vocal_mode(win, mode=vocal_mode)
        dprint(f"  下拉选择 = {selected}")
        if not selected:
            dprint("  ⚠️ 下拉选择失败，仍继续等分离完成")

    # --- 面板锚点闸门（S5.5）---
    # 音频面板此刻已滚到「人声分离」且模式下拉已收起，对比
    # calibration.panel_anchors.vocal（带 verified_at 的实测值）：
    # 音频Tab / 人声分离开关 / 模式选项行。x 绝对比，面板行的 y 只比相对偏移（防滚动假拦）。
    from ui_verify import check_anchors
    check_anchors(workdir, 'vocal', win=win)

    # Step 5: 等待分离完成（AND 判据：两条路必须都成功）
    #   产物侧（硬证据）：audioAlg 产物稳定 30s（停止增长）
    #   UI 侧：若曾看到处理提示 → 必须消失；若从没看到（云缓存秒回）→ 天然通过
    # ===== 一条路单独成功不算，必须 AND =====
    dprint(f"\n[5/5] 等待人声分离完成（超时 {timeout_sec}s，AND 判据）")

    success = False
    APPEAR_TIMEOUT = int(get_param('vocal_switch', 'appear_timeout', 90))
    POLL_INTERVAL = float(get_param('vocal_switch', 'processing_poll_interval', 5.0))

    # ---- 阶段A：可选等"处理中"出现（OCR 扫预览区标题栏，没出现=云缓存秒回）----
    # 用 player_status_text（OCR）而非 player_processing_text（UIA）：
    # 剪映 5.9 处理提示是 QML 直绘（QQuickText），不进 UIA 树，
    # 只有 OCR 扫预览区标题栏条带（y∈[25,115], x∈[WW//3,WW]）能搜到。
    processing_seen = False
    dprint(f"  验证A：可选等处理提示出现（{APPEAR_TIMEOUT}s，OCR 扫标题栏）…")
    t_appear = time.time()
    while time.time() - t_appear < APPEAR_TIMEOUT:
        try:
            win, _ = connect_jianying()
            op_mark('按键', 'End（跳最后一帧查处理提示）')
            send_keys(win, '{End}')
            time.sleep(1.0)
            st = player_status_text(win, debug=True)
            if st == 'processing':
                processing_seen = True
                dprint(f"  处理已开始（OCR 标题栏命中 processing）")
                try:
                    WL, WT, WW, WH = get_window_rect(win)
                    shot = np.array(capture_region(WL, WT, WW, WH))
                    shot_path = os.path.join(workdir, '_vocal_processing_capture.png')
                    Image.fromarray(shot.astype('uint8')).save(shot_path)
                    dprint(f"  已截图: {shot_path}")
                except Exception:
                    pass
                break
        except Exception as e:
            dprint(f"  轮询异常: {e}")
        time.sleep(POLL_INTERVAL)
    if not processing_seen:
        dprint(f"  {APPEAR_TIMEOUT}s 内未见处理提示（可能云缓存秒回），UI 侧天然通过")

    # ---- AND 判据：产物稳定 30s 且 UI 提示消失（若出现过）----
    dprint("  AND 判据：开始等待…")
    done_artifact, n_files, exp = _wait_vocal_done_by_artifacts(timeout_sec, 'AND')

    if not done_artifact:
        dprint(f"  ❌ AND 判据未通过：产物超时（最后 {n_files}/{exp} 个文件）")
        success = False
    elif processing_seen:
        # 曾看到处理提示 → UI 侧必须等它消失（OCR 扫标题栏）
        dprint("  产物稳定，确认 UI 处理提示消失（OCR 扫标题栏）…")
        ui_timeout = min(timeout_sec, 180)
        ui_ok = False
        t_ui = time.time()
        while time.time() - t_ui < ui_timeout:
            try:
                win, _ = connect_jianying()
                op_mark('按键', 'End（确认处理提示消失）')
                send_keys(win, '{End}')
                time.sleep(1.0)
                st = player_status_text(win)
                if st == 'idle':
                    ui_ok = True
                    dprint("  ✅ UI 处理提示已消失（标题栏恢复「播放器」）")
                    break
                elapsed = int(time.time() - t_ui)
                if elapsed % 15 < 6:
                    dprint(f"  ...UI 提示仍在（{elapsed}s）: status={st}")
            except Exception as e:
                dprint(f"  UI 轮询异常: {e}")
            time.sleep(POLL_INTERVAL)
        if ui_ok:
            dprint("  ✅ AND 判据通过：产物稳定 + UI 提示消失")
            success = True
        else:
            dprint("  ❌ AND 判据未通过：产物稳定但 UI 处理提示超时未消失")
            success = False
    else:
        # 从没看到处理提示 → UI 侧天然通过
        dprint("  ✅ AND 判据通过：产物稳定 + 无 UI 处理提示（云缓存秒回）")
        success = True

    # 终态校验：读下拉框当前值，确认与所选模式一致
    # （云缓存秒回时处理提示全程不出现，阶段A/B 都是兜底判定，这里给正向确认）
    if vocal_mode != 'none':
        try:
            win, _ = connect_jianying()
            cur = read_vocal_mode_current(win)
            dprint(f"  终态校验：下拉当前值 = {cur}（期望 {vocal_mode}）")
            if cur is None:
                dprint("  ⚠️ 终态校验读不出下拉当前值，不阻断（保留原判定）")
            elif cur != vocal_mode:
                success = False
                dprint(f"  ❌ 终态校验失败：期望「{vocal_mode}」实际「{cur}」")
            else:
                dprint("  ✅ 终态校验通过")
        except Exception as e:
            dprint(f"  ⚠️ 终态校验异常（不阻断）: {str(e)[:60]}")

    # ⚠️ 成功路径【不恢复】预设轨：保持 锁定+隐藏 交给后续步骤。
    # 根因（用户实测定位）：此前这里 unhide_all_tracks 把预设轨恢复可见，
    # 导致 [4]识别+翻译 时 Ctrl+A 全选带上预设片段、字幕轨簇被预设 text 轨污染。
    # 统一的「解除所有锁定和隐藏」由 [5] 分离音频收尾步骤执行（restore_all_tracks）。
    dprint("\n预设轨保持 锁定+隐藏（恢复动作留给 [5] 分离音频收尾）")

    elapsed = time.time() - t0
    mode_txt = {'none': '默认', 'vocal': '仅保留人声', 'background': '仅保留背景声'}[vocal_mode]
    dprint(f"\n[3] 人声分离（{mode_txt}）{'✅完成' if success else '❌失败'}  耗时 {elapsed:.1f}s")
    return {'ok': success, 'duration_s': round(elapsed, 1),
            'vocal_mode': vocal_mode,
            'error': None if success else '分离未完成或终态校验不符（详见日志）'}
