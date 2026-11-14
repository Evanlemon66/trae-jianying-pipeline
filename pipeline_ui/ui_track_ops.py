# -*- coding: utf-8 -*-
"""
轨道操作：隐藏/恢复轨道（眼睛图标点击）
======================================
剪映 5.9 轨道头无 UIA Description 标记，采用实测验证的图像方案：

  行检测  : 轨道头图标列亮度扫描（rowmax>90 聚类成行，过滤工具栏）
  眼睛列  : JSON track_header.eye_x（逻辑偏移，锚定时间线左缘 + UI 缩放）
             —— 1920@100% / 4K@150% 均按同一逻辑偏移换算，不再依赖窗口宽比例
  状态识别: 眼睛条带蓝像素 >= blue_threshold → 隐藏（隐藏后眼睛变蓝，实测 19 个蓝像素）
            可见时眼睛为亮灰色（亮度 174-234），蓝像素=0
  类型分类: UIA 片段 (MTLSVideoP:/MTLSTextP:) Y 聚类，按 y 与亮度行最近匹配

点位/阈值统一从 ui_calibration.json 读取（ui_calibration.get_param，
环境指纹匹配才生效，否则用默认值兜底）——JSON 是点位唯一事实源。
行位置(ymid)运行时扫描，不进 JSON。
"""

from __future__ import annotations

import json
import os
import time
from typing import List, Optional, Tuple

import numpy as np
import pyautogui
pyautogui.FAILSAFE = False

from ui_core import (
    dprint, _find_nodes_by_desc, _resolve_prop, capture_region,
    PROPERTY_ID_FULLDESC, op_mark,
)
from ui_calibration import get_param

ICON_L_RATIO = 25 / 1920    # 图标扫描左界（旧口径：窗口宽比例，仅作兜底）
ICON_R_RATIO = 110 / 1920   # 图标扫描右界（旧口径：窗口宽比例，仅作兜底）

# S3-B1：轨道头 x 基准 = 时间线左缘（MainTimeLineRoot.left）+ 逻辑偏移×UI 缩放。
# 实测（2026-11-03）：1920@100% 图标列在窗口 x≈20..110（timeline 左缘≈0）；
# 4K@150% 图标列在 timeline_left+15..150（物理px），旧「窗口宽比例」算出的 x=[50,220)
# 落在左侧面板上 → 0 行。逻辑偏移（100% 基准，相对 time线左缘），JSON 可用
# track_header.icon_l_offset / icon_r_offset 覆盖；旧 icon_*_ratio 兼容换算为逻辑px。
ICON_L_OFF = 25     # 逻辑px
ICON_R_OFF = 110    # 逻辑px

_HDR_ORIGIN = 0     # 轨道头 x 原点（截图坐标 = 时间线左缘 - 窗口左缘）
_HDR_SCALE = 1.0    # UI 缩放（100%=1.0，4K@150%=1.5）
_HDR_INSET = 0      # 内缩物理px（时间线左缘比图标列原点右移的量）
_HDR_KEY = None     # 已缓存的几何键 (hwnd, WL, WT, WW, WH)
_SCALE_CACHE = {}
_BAND_LOG_SIG = None


def _ui_scale(win=None, WL=None, WT=None, WW=None, WH=None) -> float:
    """窗口所在屏的 UI 缩放（100%=1.0）。取不到退 1.0（等价旧逻辑，不阻塞扫描）。"""
    key = (getattr(win, 'NativeWindowHandle', 0), WL, WT, WW, WH)
    if key in _SCALE_CACHE:
        return _SCALE_CACHE[key]
    s = 1.0
    try:
        from ui_calibration import env_fingerprint
        s = float(env_fingerprint(win).get('dpi_scale') or 1.0)
    except Exception:
        s = 1.0
    if not (0.5 <= s <= 4.0):
        s = 1.0
    _SCALE_CACHE[key] = s
    return s


def _set_header_geom(win, WL, TR_L, WW, WT=None, WH=None):
    """刷新轨道头 x 基准：origin=时间线左缘（截图坐标），scale=UI 缩放。
    TR_L=None（拿不到 timeline rect）→ origin=0，等价旧口径（窗口左缘）。
    S3-B1 修正：MainTimeLineRoot 左缘比轨道头图标列右移约 9 逻辑px（内缩），
    实测 4K@150% TR_L=397 时图标列在窗口 x=40/76/112/148，扣除内缩后与 1920 同构。"""
    global _HDR_ORIGIN, _HDR_SCALE, _HDR_INSET, _HDR_KEY
    _HDR_SCALE = _ui_scale(win, WL, WT, WW, WH)
    if TR_L is not None:
        inset = float(get_param('track_header', 'timeline_inset', 9))
        _HDR_INSET = int(round(inset * _HDR_SCALE))
        _HDR_ORIGIN = int(TR_L) - int(WL) - _HDR_INSET
    else:
        _HDR_INSET = 0
        _HDR_ORIGIN = 0
    _HDR_KEY = (getattr(win, 'NativeWindowHandle', 0), WL, WT, WW, WH)
    return _HDR_ORIGIN, _HDR_SCALE


def _ensure_geom(win, WL, WT, WW, WH):
    """确保几何已按当前窗口刷新（同窗口尺寸不重复跑 UIA 查找时间线）。"""
    key = (getattr(win, 'NativeWindowHandle', 0), WL, WT, WW, WH)
    if key == _HDR_KEY:
        return _HDR_ORIGIN, _HDR_SCALE
    tr = get_timeline_rect(win)
    return _set_header_geom(win, WL, tr[0] if tr else None, WW, WT, WH)


def _hdr_x(offset) -> int:
    """逻辑偏移 → 截图 x（相对窗口左缘）= origin + offset×scale"""
    return int(_HDR_ORIGIN) + int(round(float(offset) * _HDR_SCALE))


def header_abs_x(WL, offset) -> int:
    """逻辑偏移 → 屏幕绝对 x（供轨道头/片段点击复用同一基准）"""
    return int(WL) + _hdr_x(offset)


def _eye_x_offset():
    """眼睛列逻辑偏移（相对时间线左缘，JSON track_header.eye_x，默认 74）"""
    return int(get_param('track_header', 'eye_x', 74))


# ============ 基础几何 ============
def get_window_rect(win):
    wr = win.BoundingRectangle
    WL, WT = int(wr.left), int(wr.top)
    return WL, WT, int(wr.right) - WL, int(wr.bottom) - WT


def get_timeline_rect(win) -> Optional[Tuple[int, int, int, int]]:
    nodes = _find_nodes_by_desc(win, 'maintimelineroot')
    for n in nodes:
        try:
            r = n.BoundingRectangle
            if r.right - r.left > 400:
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            pass
    return None


def get_eye_x(WL, WW):
    # 眼睛列 = 时间线左缘 + 逻辑偏移×UI缩放（S3-B1）
    return WL + _hdr_x(_eye_x_offset())


def get_lock_x(WL, WW):
    # 锁定列 = 时间线左缘 + 逻辑偏移×UI缩放（S3-B1）
    return WL + _hdr_x(int(get_param('track_header', 'lock_x', 50)))


def _icon_offsets() -> Tuple[float, float]:
    """图标列逻辑偏移（相对时间线左缘）。优先 JSON icon_*_offset；
    兼容旧 ratio 口径（ratio × 1920 设计宽 = 逻辑px）。"""
    l = get_param('track_header', 'icon_l_offset', None)
    r = get_param('track_header', 'icon_r_offset', None)
    if l is not None and r is not None:
        return float(l), float(r)
    return (float(get_param('track_header', 'icon_l_ratio', ICON_L_RATIO)) * 1920.0,
            float(get_param('track_header', 'icon_r_ratio', ICON_R_RATIO)) * 1920.0)


# ============ 行检测（亮度扫描）============
def detect_track_rows(img, WL, WT, WW, TR_T, TR_B, TR_L=None, scale=None) -> List[Tuple[int, int]]:
    """
    图标列亮度扫描检测轨道行。
    x 基准 = 时间线左缘（TR_L）+ 逻辑偏移×UI缩放（S3-B1）；
    TR_L=None 时回退旧口径（窗口宽比例）并打日志。
    img: capture_region 截的窗口图（numpy 数组）
    返回 [(y_top, y_bot), ...] 绝对坐标，从上到下。
    """
    global _BAND_LOG_SIG
    off_l, off_r = _icon_offsets()
    if TR_L is not None:
        s = float(scale if scale is not None else _HDR_SCALE)
        x0 = int(round((int(TR_L) - int(WL)) + off_l * s))
        x1 = int(round((int(TR_L) - int(WL)) + off_r * s))
        anchor = f'时间线左缘(TR_L={int(TR_L)})'
        origin = int(TR_L) - int(WL)
    else:
        s = None
        x0 = round(WW * float(get_param('track_header', 'icon_l_ratio', ICON_L_RATIO)))
        x1 = round(WW * float(get_param('track_header', 'icon_r_ratio', ICON_R_RATIO)))
        anchor = '兜底:窗口宽比例'
        origin = 0
        dprint("  ⚠️ 未取到时间线 rect，图标列回退旧口径（窗口宽比例）")
    x0 = max(0, min(int(x0), img.shape[1]))
    x1 = max(0, min(int(x1), img.shape[1]))
    sig = (x0, x1, anchor, s)
    if sig != _BAND_LOG_SIG:
        _BAND_LOG_SIG = sig
        dprint(f"  轨道行扫描带: x=[{x0},{x1}) 锚点={anchor} origin={origin} "
               f"scale={('%.2f' % s) if s is not None else 'n/a(旧口径)'}")
    if x1 <= x0:
        dprint(f"  ⚠️ 轨道行扫描带无效 x=[{x0},{x1})，跳过行检测")
        return []
    y0, y1 = TR_T - WT, TR_B - WT
    band = img[y0:y1, x0:x1, :3].astype(int)
    rowmax = band.max(axis=(1, 2))

    rows, in_row, start = [], False, 0
    bright_thresh = int(get_param('track_header', 'row_brightness_thresh'))
    for i, v in enumerate(rowmax):
        if v > bright_thresh and not in_row:
            in_row, start = True, i
        elif v <= bright_thresh and in_row:
            in_row = False
            rows.append((y0 + start + WT, y0 + i + WT))
    if in_row:
        rows.append((y0 + start + WT, y1 + WT))

    # 合并相邻（间距<merge_gap），过滤高度>max_height 和紧贴时间线顶的工具栏
    merge_gap = int(get_param('track_header', 'row_merge_gap'))
    max_height = int(get_param('track_header', 'row_max_height'))
    top_offset = int(get_param('track_header', 'row_top_offset'))
    merged = []
    for a, b in rows:
        if merged and a - merged[-1][1] < merge_gap:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a <= max_height and a > TR_T + top_offset]


# ============ 状态识别（蓝像素，JSON 驱动）============
# 轨道头按钮布局（所有行统一，窗口相对 x）：
# 🔒锁定(50)  👁眼睛(74)  🔊关闭原声(98)  📌封面(143)  🎵工具栏(171-299)

def _strip_blue_count(img, WT, ymid, strip) -> int:
    """通用条带蓝像素计数（strip 为逻辑偏移，锚定时间线左缘+DPI 缩放）"""
    x0, x1 = _hdr_x(strip[0]), _hdr_x(strip[1])
    if x1 <= x0:
        return 0
    reg = img[ymid - WT - 12:ymid - WT + 12, x0:x1, :3].astype(int)
    return int(((reg[:, :, 2] > reg[:, :, 0] + 20) &
                (reg[:, :, 2] > reg[:, :, 1] + 10) &
                (reg[:, :, 2] > 110)).sum())


def is_row_hidden(img, WL, WT, WW, ymid) -> bool:
    """眼睛条带蓝像素 >= blue_threshold → 隐藏"""
    return count_blue(img, WL, WT, WW, ymid) >= int(
        get_param('track_header', 'blue_threshold', 8))


def count_blue(img, WL, WT, WW, ymid) -> int:
    strip = get_param('track_header', 'eye_strip', [60, 90])
    return _strip_blue_count(img, WT, ymid, strip)


# ============ 类型分类（片段颜色 + UIA 细分）============
def classify_row_color(img, WL, WT, ymid, TR_L) -> Optional[str]:
    """
    采样该行片段区域主导色调：
      字幕轨片段 = 橙红 (R 显著大于 G/B) → 'text'
      视频/音频片段 = 青 (G/B >= R)       → 'nontext'
      无明显片段（行被隐藏/空白）          → None
    """
    # S1-6：颜色判据阈值可校准（calibration track_classify.*；缺省=硬编码实测值，行为不变）
    text_margin = float(get_param('track_classify', 'text_r_over_gb', 40))
    audio_b_margin = float(get_param('track_classify', 'audio_b_over_r', 30))
    audio_g_margin = float(get_param('track_classify', 'audio_g_over_r', 20))
    x0 = (TR_L - WL) + int(round(60 * _HDR_SCALE))
    x1 = min((TR_L - WL) + int(round(560 * _HDR_SCALE)), img.shape[1])
    if x1 <= x0:
        return None
    reg = img[ymid - WT - 6:ymid - WT + 6, x0:x1, :3].astype(int)
    mask = (reg.max(axis=2) > 70) & ((reg.max(axis=2) - reg.min(axis=2)) > 25)
    if int(mask.sum()) < 50:
        return None
    px = reg[mask]
    r_m, g_m, b_m = float(px[:, 0].mean()), float(px[:, 1].mean()), float(px[:, 2].mean())
    # 字幕橙红实测 R-G≈67/R-B≈82；暖色视频缩略图 R-G≈16/R-B≈20；阈值 40 区分
    if r_m > g_m + text_margin and r_m > b_m + text_margin:
        return 'text'
    # 分离音频轨片段 = 深蓝（实测 RGB(29,65,98)：B-R≈+69, G-R≈+36）→ 'audio'
    # 视频缩略图偏灰 B-R≈0，不会误判
    if b_m > r_m + audio_b_margin and g_m > r_m + audio_g_margin:
        return 'audio'
    return 'nontext'


def classify_track_ys(win) -> List[Tuple[str, float]]:
    """
    扫描时间线片段，按 Y 聚类成 (类型, y_center) 列表，按 y 排序。
    类型: 'video' | 'audio' | 'text'（字幕片段 UIA 不稳定，仅作细分参考）
    """
    clusters: List[Tuple[str, float]] = []

    def scan(ctrl, depth=0):
        if depth > 20:
            return
        fd = (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or '').strip()
        if fd.startswith(('MTLSVideoP:', 'MTLSTextP:', 'MTLSText:')):
            try:
                r = ctrl.BoundingRectangle
                cy = (int(r.top) + int(r.bottom)) / 2
            except Exception:
                cy = None
            if cy:
                label = fd.split(':', 1)[1] if ':' in fd else fd
                if fd.startswith('MTLSText'):
                    cat = 'text'
                elif 'pipeline_sx' in label.lower() or 'audio' in label.lower():
                    cat = 'audio'
                else:
                    cat = 'video'
                clusters.append((cat, cy))
        try:
            for c in ctrl.GetChildren():
                scan(c, depth + 1)
        except Exception:
            pass

    scan(win, 0)

    # Y 聚类（间距 <25 视为同一轨道）
    clusters.sort(key=lambda x: x[1])
    grouped: List[Tuple[str, float]] = []
    for cat, cy in clusters:
        if grouped and abs(cy - grouped[-1][1]) < 25:
            # 同轨道的片段，更新平均 y（类型以第一条为准）
            pass
        else:
            grouped.append((cat, cy))
    return grouped


# ============ 完整扫描 ============
def scan_tracks(win) -> Tuple[list, np.ndarray, int, int, int, int]:
    """
    完整扫描当前时间线轨道。
    返回 (rows, img, WL, WT, WW, eye_x)
      rows: [{'type': str, 'y_top': int, 'y_bot': int, 'ymid': int, 'hidden': bool}]
             按 y 从上到下排序
    """
    WL, WT, WW, WH = get_window_rect(win)
    tr = get_timeline_rect(win)
    if not tr:
        dprint("scan_tracks: 找不到 MainTimeLineRoot")
        _set_header_geom(win, WL, None, WW, WT, WH)   # 兜底：origin=窗口左缘
        return [], None, WL, WT, WW, 0
    TR_T, TR_B = tr[1], tr[3]
    _set_header_geom(win, WL, tr[0], WW, WT, WH)       # S3-B1：锚定时间线左缘

    img = np.array(capture_region(WL, WT, WW, WH))
    raw_rows = detect_track_rows(img, WL, WT, WW, TR_T, TR_B, tr[0], _HDR_SCALE)

    # UIA 优先（video/audio 检测稳定）；UIA 无信息时用颜色判 text
    type_ys = classify_track_ys(win)
    rows = []
    for a, b in raw_rows:
        ymid = (a + b) // 2
        uia_cat = None
        best_d = 30
        for t, ty in type_ys:
            d = abs(ty - ymid)
            if d < best_d:
                best_d = d
                uia_cat = t
        if uia_cat in ('video', 'audio'):
            cat = uia_cat
        elif uia_cat == 'text':
            cat = 'text'
        else:
            color_cat = classify_row_color(img, WL, WT, ymid, tr[0])
            if color_cat == 'text':
                cat = 'text'
            elif color_cat == 'audio':
                cat = 'audio'
            elif color_cat == 'nontext':
                cat = 'video'
            else:
                cat = 'unknown'
        rows.append({
            'type': cat,
            'y_top': a, 'y_bot': b, 'ymid': ymid,
            'hidden': is_row_hidden(img, WL, WT, WW, ymid),
        })
    return rows, img, WL, WT, WW, get_eye_x(WL, WW)


# ============ 轨道控制（状态感知 + 自校准点击）============
_EYE_X_CACHE = None   # 校准成功的眼睛列偏移（相对 WL 固定像素），会话内复用


def _eye_candidates():
    """眼睛列候选逻辑偏移（相对时间线左缘）：JSON校准 > 实测概率排序"""
    # 实测所有行眼睛统一 x≈74（旧注释"主轨≈125"是误判，已证伪）
    base = [75, 95, 55, 115, 125, 135, 45, 155, 175, 195, 215]
    cal = _calib_eye_offset()
    if cal:
        if cal in base:
            base.remove(cal)
        base.insert(0, cal)
    return base


def _calib_eye_offset():
    """JSON 里的眼睛列偏移（track_header.eye_x，环境指纹匹配才生效）"""
    return get_param('track_header', 'eye_x', None)


def _row_icon_anchor(img, WT, ymid, scale):
    """从行内亮列反推图标列原点（第一段亮列=类型图标，逻辑偏移 icon_l_offset）。
    返回 origin（截图坐标，相对窗口左缘）或 None；用于点击前的经验锚定，
    对 100%/150% 缩放、窗口内缩差异都稳（不依赖固定像素）。"""
    h = img.shape[0]
    y0 = max(0, ymid - WT - 10)
    y1 = min(h, ymid - WT + 10)
    x_max = min(img.shape[1], int(round(340 * max(scale, 1.0))))
    if y1 <= y0 or x_max <= 1:
        return None
    col = img[y0:y1, 0:x_max, :3].astype(int).max(axis=(0, 2))
    runs, s = [], None
    for i, v in enumerate(col):
        if v > 90 and s is None:
            s = i
        elif v <= 90 and s is not None:
            if i - s >= 4:
                runs.append((s + i - 1) // 2)
            s = None
    if s is not None and x_max - s >= 4:
        runs.append((s + x_max - 1) // 2)
    if not runs:
        return None
    off_l, _ = _icon_offsets()
    return runs[0] - int(round(off_l * scale))


def _eye_click_xs(img, WL, WT, WW, ymid):
    """眼睛列点击候选（绝对 x，按优先级）：
    ① calibration JSON（track_header.eye_x，准入已实测确认 → 首选，run9 教训）
    ② 会话缓存（已验证命中的物理偏移，原样复用）
    ③ 行内亮列经验锚定（calibration 失效时的备选）
    ④ 逻辑偏移候选：内缩帧 / 旧口径帧交替兜底
    根因：run9 经验锚定 icon_l_offset+5=83 vs calibration eye_x=78 差 5px，
    经验锚定插队导致重试 3 次；calibration 是准入实测值，必须排首位。
    """
    model = WL + _hdr_x(_eye_x_offset())
    xs = []

    def add(x, tag=''):
        x = int(round(x))
        if x not in xs:
            xs.append(x)

    # ① calibration JSON（准入已校准 → 最可信）
    add(model, 'calibration')
    dprint(f"  📌 眼睛列首选: calibration track_header.eye_x → x={model}")

    # ② 会话缓存（曾验证命中的物理偏移）
    if _EYE_X_CACHE:
        add(WL + int(_EYE_X_CACHE), 'cache')

    # ③ 经验锚定（calibration 失效时的备选，与 model 差 5px 以内才纳入）
    a = _row_icon_anchor(img, WT, ymid, _HDR_SCALE)
    if a is not None:
        emp = WL + a + int(round(_eye_x_offset() * _HDR_SCALE))
        if abs(emp - model) <= int(round(20 * max(_HDR_SCALE, 1.0))):
            add(emp, '经验锚定')
            dprint(f"  📌 眼睛列备选: 经验锚定 → x={emp}（Δ={emp - model}px）")

    # ④ 其他候选兜底
    for off in _eye_candidates():
        add(WL + _hdr_x(off))
        add(WL + _hdr_x(off) + _HDR_INSET)
    return xs or [model]


def _set_row_hidden(win, eye_x, ymid, want_hidden: bool, label="", max_retry=10) -> bool:
    """
    把指定行设置为目标状态：读状态→不符才点→点完验证。
    点击位置自校准：依次尝试候选偏移，命中（状态翻转）后缓存供后续行复用。
    返回最终是否达标。
    """
    global _EYE_X_CACHE
    WL, WT, WW, WH = get_window_rect(win)
    _ensure_geom(win, WL, WT, WW, WH)   # S3-B1：眼睛列同样锚定时间线左缘
    xs = None
    for attempt in range(max_retry):
        img = np.array(capture_region(WL, WT, WW, WH))
        if is_row_hidden(img, WL, WT, WW, ymid) == want_hidden:
            return True
        if xs is None:
            xs = _eye_click_xs(img, WL, WT, WW, ymid)
        x = xs[min(attempt, len(xs) - 1)]
        off = x - WL
        op_mark('点击眼睛',
                f'ymid={ymid} → {"隐藏" if want_hidden else "显示"} {label} '
                f'@({x},{ymid}) 尝试{attempt + 1}')
        dprint(f"  点击眼睛 @ ({x}, {ymid}) {label} (尝试{attempt+1}, x偏移{off})")
        pyautogui.click(x, ymid)
        time.sleep(0.7)   # 等隐藏/显示动画完成再检测
        # 立即验证是否翻转，翻转则缓存该偏移
        img2 = np.array(capture_region(WL, WT, WW, WH))
        if is_row_hidden(img2, WL, WT, WW, ymid) == want_hidden:
            if _EYE_X_CACHE != off:
                dprint(f"  📌 眼睛列校准成功: x偏移 {off}")
                _EYE_X_CACHE = off
            return True
    return False


def hide_tracks_except_main(win) -> List[int]:
    """
    只保留主视频轨（双重校验版：M1 最底 video + M2 封面按钮），其余行全部隐藏。
    剪映主轨恒在最底部。返回被隐藏的行的索引列表。

    主轨识别失败直接抛异常：绝不拿最底行/下方预设轨顶替——那样"只隐藏预设轨"
    会变成"隐藏主轨"，上一轮事故就是这么发生的。
    """
    rows, img, _, _, _, eye_x = scan_tracks(win)
    if not rows:
        raise RuntimeError('hide_tracks_except_main: 没找到任何轨道行')
    main, _m2 = find_main_track(win, rows, img)
    if main is None:
        raise RuntimeError('hide_tracks_except_main: 主轨识别失败（M2 封面校验未过），'
                           '拒绝用最底行顶替')
    keep_idx = next(i for i, r in enumerate(rows) if r is main)
    dprint(f"轨道行 ({len(rows)}): {[(r['type'], r['hidden']) for r in rows]}  保留主轨: 行{keep_idx}")

    hidden_idx = []
    for i, r in enumerate(rows):
        want = i != keep_idx
        if _set_row_hidden(win, eye_x, r['ymid'], want, f"行{i}[{r['type']}]"):
            if want:
                hidden_idx.append(i)
        else:
            dprint(f"  ⚠️ 行 {i} [{r['type']}] 设置 hidden={want} 失败")
    return hidden_idx


def hide_tracks_except(win, keep_types: List[str]) -> List[int]:
    """
    隐藏除 keep_types 外的所有轨道（状态感知+点击后验证重试）。
    返回被隐藏的行的索引列表。
    """
    rows, _, _, _, _, eye_x = scan_tracks(win)
    if not rows:
        dprint("hide_tracks_except: 没找到轨道行")
        return []
    dprint(f"轨道行 ({len(rows)}): {[(r['type'], r['hidden']) for r in rows]}  保留: {keep_types}")

    hidden_idx = []
    for i, r in enumerate(rows):
        want = r['type'] not in keep_types
        if _set_row_hidden(win, eye_x, r['ymid'], want, f"[{r['type']}]"):
            if want:
                hidden_idx.append(i)
        else:
            dprint(f"  ⚠️ 行 {i} [{r['type']}] 设置 hidden={want} 失败")
    return hidden_idx


def unhide_all_tracks(win) -> List[int]:
    """解除所有轨道的隐藏（点击后验证重试）。"""
    rows, _, _, _, _, eye_x = scan_tracks(win)
    restored = []
    for i, r in enumerate(rows):
        if r['hidden']:
            if _set_row_hidden(win, eye_x, r['ymid'], False, f"恢复 [{r['type']}]"):
                restored.append(i)
            else:
                dprint(f"  ⚠️ 行 {i} 恢复失败")
    if not restored and not any(r['hidden'] for r in rows):
        dprint("所有轨道均可见")
    return restored


def unlock_all_tracks(win) -> List[int]:
    """解除所有轨道的锁定（收尾用，与 unhide_all_tracks 配对：先解锁再恢复可见）。"""
    rows, img, WL, WT, WW, _ = scan_tracks(win)
    unlocked = []
    for i, r in enumerate(rows):
        if is_row_locked(img, WT, r['ymid']):
            if lock_track(win, r['ymid'], want_locked=False):
                unlocked.append(i)
            else:
                dprint(f"  ⚠️ 行 {i} 解锁失败")
    if not unlocked:
        dprint("所有轨道均未锁定")
    return unlocked


def _light_row_state(win, WL, WT, WW, WH, rows):
    """轻量状态复检：只抓一次全窗截图，按已知 y 判眼睛/锁定像素。
    不做 UIA 枚举、不做类型分类——restore 收尾只需要知道「还是不是锁着/藏着」。
    返回 (img, still_locked[], still_hidden[])。"""
    img = np.array(capture_region(WL, WT, WW, WH))
    still_locked = [r['ymid'] for r in rows if is_row_locked(img, WT, r['ymid'])]
    still_hidden = [r['ymid'] for r in rows if is_row_hidden(img, WL, WT, WW, r['ymid'])]
    return img, still_locked, still_hidden


# ============ 可视区判定 + 多态纵向滚动 ============
# 不设固定优先级：实测同一台机器上 wheel / scrollbar / keyboard 的有效性会随轮次变化
# （上一轮只有键盘有位移，本轮只有滚轮/滚动条有位移），任何固定顺序都会白跑两轮。
# 改为轮转起手：每轮从上一次成功模态的下一个开始试，谁先产生位移就用谁。
SCROLL_MODES = ('keyboard', 'wheel', 'scrollbar')
_SCROLL_ORDER_START = 0     # 轮转起手索引（相对 SCROLL_MODES）


def _safe_timeline_rect(win):
    """时间线 rect；取不到返回 None（不抛异常，调用方按"滚动不可用"处理）"""
    try:
        return get_timeline_rect(win)
    except Exception:
        return None


def _row_in_view(ymid, tr, edge=4) -> bool:
    """行中心是否落在时间线可视纵向区间内。tr=None 时不做判断（视为可见）。"""
    if not tr:
        return True
    return int(tr[1]) + edge <= int(ymid) <= int(tr[3]) - edge


def _light_scan(win):
    """
    只跑亮度行检测（不枚举 UIA，~0.8s），返回 (rows, tr, img, WL, WT)。
    用途：判断"某个 ymid 上现在还是不是那一行"，以及"还有没有轨道在视口外"。
    """
    WL, WT, WW, WH = get_window_rect(win)
    tr = _safe_timeline_rect(win)
    if not tr:
        return [], None, None, WL, WT
    try:
        _set_header_geom(win, WL, tr[0], WW, WT, WH)
        img = np.array(capture_region(WL, WT, WW, WH))
        spans = detect_track_rows(img, WL, WT, WW, tr[1], tr[3], tr[0], _HDR_SCALE)
    except Exception as e:
        dprint(f"  ⚠️ 轻量行检测失败: {e}")
        return [], tr, None, WL, WT
    rows = []
    for a, b in spans:
        ymid = (a + b) // 2
        rows.append({'type': 'unknown', 'y_top': a, 'y_bot': b, 'ymid': ymid,
                     'hidden': is_row_hidden(img, WL, WT, WW, ymid)})
    return rows, tr, img, WL, WT


def _ymid_present(ymid, spans, tol=6) -> bool:
    """ymid 是否落在某个已检测到的行带内（容差 tol）"""
    return any(a - tol <= int(ymid) <= b + tol for a, b in spans)


def _scroll_anchor(tr):
    """滚轮落点：时间线中右部。避开左侧轨道头图标列，避免误点锁定/眼睛。"""
    left, top, right, bottom = (int(v) for v in tr[:4])
    x = right - max(40, (right - left) // 10)
    y = (top + bottom) // 2
    return x, y


def _scroll_once(win, direction, mode, tr):
    """执行一次指定模态的滚动；失败抛异常，由调用方记为该模态不可用。"""
    left, top, right, bottom = (int(v) for v in tr[:4])
    down = direction == 'down'
    if mode == 'wheel':
        x, y = _scroll_anchor(tr)
        pyautogui.moveTo(x, y)
        pyautogui.scroll(-600 if down else 600, x=x, y=y)
    elif mode == 'scrollbar':
        sx = right - 10
        if down:
            pyautogui.moveTo(sx, bottom - 60)
            pyautogui.dragTo(sx, bottom - 160, duration=0.3)
        else:
            pyautogui.moveTo(sx, top + 60)
            pyautogui.dragTo(sx, top + 160, duration=0.3)
    elif mode == 'keyboard':
        # 点时间线标尺（只移动播放头，不选中片段）后 PageDown/PageUp
        pyautogui.click(left + min(400, max(60, (right - left) // 2)), top + 6)
        key = 'pagedown' if down else 'pageup'
        pyautogui.press(key)
        time.sleep(0.15)
        pyautogui.press(key)
    else:
        raise ValueError(f'未知滚动模态 {mode}')


def scroll_timeline_vertical(win, direction='down', modes=SCROLL_MODES) -> dict:
    """
    多态纵向滚动时间线：keyboard / wheel / scrollbar **轮转起手**，谁有位移就用谁。
    down=看下方轨道，up=看上方轨道。

    不设固定优先级：本机实测模态有效性逐轮变化（上一轮只有键盘动，这一轮只有
    滚轮/滚动条动），固定顺序会让每轮都先白试一遍无效模态。维护一个模块级
    轮转索引：本次从上次命中的下一个模态开始试，命中后索引后移；全失败也后移，
    保证下一轮起手模态与这一轮不同。

    每个模态执行后对比亮度行检测的 y 集合：真的动了才算生效，没动记为该模态无位移。
    返回 {'ok','mode','tried','reason'}；不可用时 reason 明确（no_timeline_rect /
    all_modes_failed），**不静默跳过**。
    """
    global _SCROLL_ORDER_START
    rows0, tr, _img, _WL, _WT = _light_scan(win)
    if not tr:
        dprint("  ⚠️ 滚动不可用：拿不到时间线 rect")
        return {'ok': False, 'mode': None, 'tried': [], 'reason': 'no_timeline_rect'}
    sig0 = tuple(r['ymid'] for r in rows0)
    order = tuple(modes)
    n = len(order) or 1
    start = _SCROLL_ORDER_START % n
    ordered = order[start:] + order[:start]
    dprint(f"  滚动模态轮转起手: {ordered[0]}（上次起手索引={start}）")
    tried = []
    for mode in ordered:
        try:
            _scroll_once(win, direction, mode, tr)
        except Exception as e:
            tried.append({'mode': mode, 'ok': False, 'error': str(e)})
            dprint(f"  ⚠️ 滚动模态 {mode} 不可用: {e}")
            continue
        time.sleep(0.6)
        rows1, _tr, _img1, _wl1, _wt1 = _light_scan(win)
        sig1 = tuple(r['ymid'] for r in rows1)
        moved = bool(sig0) and sig1 != sig0
        tried.append({'mode': mode, 'ok': moved, 'error': ''})
        dprint(f"  {'✅' if moved else '⚠️'} 滚动模态 {mode} "
               f"{'生效' if moved else '无位移'}（方向={direction}，"
               f"{len(sig0)} 行 → {len(sig1)} 行）")
        if moved:
            _SCROLL_ORDER_START = (order.index(mode) + 1) % n
            return {'ok': True, 'mode': mode, 'tried': tried, 'reason': 'ok'}
    _SCROLL_ORDER_START = (start + 1) % n
    return {'ok': False, 'mode': None, 'tried': tried, 'reason': 'all_modes_failed'}


def _reflow_rows(win, rows, tr=None):
    """
    轻量重排：用当前屏幕的亮度行检测刷新 rows 的 ymid（不跑 UIA）。
    插入新轨/滚动后整摞轨道会漂移（实测主轨 1282→1030），旧 y 上已经不是那一行 ——
    这是"解锁点了没反应、复检仍锁定"的主因。
    返回 (new_rows, drifted, img, WT)；检测不到行时原样返回 drifted=False。
    """
    fresh, tr2, img, WL, WT = _light_scan(win)
    tr = tr or tr2
    if not fresh:
        return rows, False, img, WT
    if len(fresh) == len(rows):
        drifted = any(abs(a['ymid'] - b['ymid']) > 6 for a, b in zip(rows, fresh))
    else:
        drifted = True
    if not drifted:
        for a, b in zip(rows, fresh):   # 位置没变，只刷新隐藏状态
            a['hidden'] = b['hidden']
        return rows, False, img, WT
    out = []
    for b in fresh:
        cand = min(rows, key=lambda r: abs(r['ymid'] - b['ymid']))
        b['type'] = cand['type'] if abs(cand['ymid'] - b['ymid']) <= 40 else 'unknown'
        out.append(b)
    return out, True, img, WT


def _draft_track_count() -> int:
    """当前草稿 draft_content.json 的真实轨道数（expected_tracks 的唯一事实源）。

    取代旧的「可见行数 + 1」下界：可见行数受滚动视口影响，视口外的轨道会被
    当成不存在，expected 永远差一条 → 上一轮 admission 落到 scroll_unavailable。
    读不到草稿（无活动草稿/解析失败）返回 0，调用方退化为"当前可视区干净即完成"。
    """
    try:
        from ui_recognize_translate import DRAFT_ROOT, _active_draft_name
        name = _active_draft_name()
        if not name:
            dprint("  ⚠️ 无活动草稿，expected_tracks 退化为最可视区判据")
            return 0
        p = os.path.join(DRAFT_ROOT, name, 'draft_content.json')
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        n = len(d.get('tracks') or [])
        dprint(f"  草稿真实轨道数 = {n}（读自 {p}）")
        return n
    except Exception as e:
        dprint(f"  ⚠️ 读取草稿轨道数失败（{type(e).__name__}: {e}），"
               f"expected_tracks 退化为最可视区判据")
        return 0


def _draft_track_ledger() -> list:
    """draft_content.json 轨道台账（取证用）：按数组顺序列出 idx/type/attribute/段数。

    数组自下而上：idx 0 = 时间线最底部（主轨）。读不到草稿返回 []（不阻断）。
    """
    try:
        from ui_recognize_translate import DRAFT_ROOT, _active_draft_name
        name = _active_draft_name()
        if not name:
            return []
        p = os.path.join(DRAFT_ROOT, name, 'draft_content.json')
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return [{'idx': i, 'type': t.get('type'), 'attribute': t.get('attribute'),
                 'segments': len(t.get('segments') or [])}
                for i, t in enumerate(d.get('tracks') or [])]
    except Exception as e:
        dprint(f"  ⚠️ 读取草稿轨道台账失败（{type(e).__name__}: {e}）")
        return []


def _band_at(ymid, rows, tol=10):
    """在行带列表里找 ymid 对应的行：优先判定 ymid 是否落在某行带内（±tol），
    否则取中心最近且距离 ≤ tol 的行。找不到返回 None（该行可能已滚出视口）。"""
    for r in rows or []:
        if r['y_top'] - tol <= ymid <= r['y_bot'] + tol:
            return r
    best = None
    for r in rows or []:
        d = abs(r['ymid'] - ymid)
        if d <= tol and (best is None or d < abs(best['ymid'] - ymid)):
            best = r
    return best


def _type_at(ymid, fresh_rows, rows_hint):
    """该 ymid 的行类型：先看本次重扫（轻扫 type 恒为 unknown），退回上一轮完整扫描。"""
    band = _band_at(ymid, fresh_rows, 10)
    if band and band.get('type') not in (None, 'unknown'):
        return band['type']
    if rows_hint:
        cand = min(rows_hint, key=lambda r: abs(r['ymid'] - ymid))
        if abs(cand['ymid'] - ymid) <= 40:
            return cand.get('type')
    return band.get('type') if band else None


def _guess_track_index(ymid, screen_rows, ledger):
    """屏幕行 → 草稿轨序号猜测（数组自下而上：屏幕最上一行 = 最高 idx）。
    只有屏幕行数与草稿轨数一致时才给猜测，否则 None（绝不硬猜）。"""
    if not ledger or not screen_rows or len(screen_rows) != len(ledger):
        return None
    idx = next((i for i, r in enumerate(screen_rows) if r['ymid'] == ymid), None)
    if idx is None:
        return None
    return len(ledger) - 1 - idx


def _lock_strip_evidence(img, WT, ymid) -> dict:
    """锁列条带取证：RGB 均值 + 蓝像素（判断"看着像锁着"是否有像素依据）。"""
    strip = get_param('track_header', 'lock_strip', [38, 62])
    mean = _strip_mean(img, WT, ymid, strip)
    return {'lock_mean': [round(float(v), 1) for v in mean] if mean is not None else None,
            'lock_blue': _strip_blue_count(img, WT, ymid, strip)}


def _row_target_state(img, WL, WT, WW, kind, ymid) -> bool:
    """目标状态是否已达成：lock → 不再锁定；eye → 不再隐藏。"""
    if kind == 'lock':
        return not is_row_locked(img, WT, ymid)
    return not is_row_hidden(img, WL, WT, WW, ymid)


def _restore_dirty_row(win, kind, y_prev, rows_hint, lock_x, eye_x,
                       ledger=None, max_attempts=2, tol=10) -> dict:
    """恢复单行：**点击前重扫** → 用扫描到的当前位置 → 点击 → 立即复检。

    把两种"点了没生效"分开（用户口径）：
      ① 坐标过期：重扫发现整摞位移，旧 ymid 已落不到那一行 → 改用扫描到的 ymid，
         drifted=True / shift=位移，日志据此确认是旧坐标问题；
      ② 行本身不响应：重扫命中同一位置（无位移），点击后像素状态仍不翻转 →
         不再盲点，返回 reason='unresponsive' + 取证（type/hidden/锁列像素/点击坐标）。
    返回 {'ok','kind','prev_ymid','click_ymid','click_x','drifted','shift','attempts',
          'reason','evidence'}。
    """
    res = {'ok': False, 'kind': kind, 'prev_ymid': int(y_prev), 'click_ymid': None,
           'click_x': int(lock_x if kind == 'lock' else eye_x), 'drifted': False,
           'shift': 0, 'attempts': 0, 'reason': 'not_run', 'evidence': {}}
    x = res['click_x']
    target = int(y_prev)

    while res['attempts'] < max_attempts:
        WL, WT, WW, WH = get_window_rect(win)
        fresh, _tr, img, _wl, _wt = _light_scan(win)
        if not fresh or img is None:
            res['reason'] = 'no_image'
            return res
        band = _band_at(target, fresh, tol)
        if band is None:
            shift = _estimate_vshift([r['ymid'] for r in (rows_hint or [])],
                                     [r['ymid'] for r in fresh])
            moved = _band_at(target + shift, fresh, tol) if shift else None
            if moved is None:
                res['reason'] = 'row_lost'
                return res
            res['drifted'] = True
            res['shift'] = int(moved['ymid']) - int(y_prev)
            target = int(moved['ymid'])
        else:
            if abs(band['ymid'] - target) > 2:
                res['drifted'] = True
                res['shift'] = int(band['ymid']) - int(y_prev)
            target = int(band['ymid'])
        res['click_ymid'] = target
        ev = _lock_strip_evidence(img, WT, target)
        ev['type'] = _type_at(target, fresh, rows_hint)
        ev['hidden'] = bool(band.get('hidden'))
        ev['track_guess'] = _guess_track_index(target, rows_hint, ledger)
        res['evidence'] = ev

        if _row_target_state(img, WL, WT, WW, kind, target):
            res.update(ok=True, reason='already')
            return res

        res['attempts'] += 1
        op_mark('点击锁定' if kind == 'lock' else '点击眼睛',
                f'ymid={target} → {"解锁" if kind == "lock" else "恢复可见"} '
                f'@({x},{target}) 尝试{res["attempts"]}（点击前重扫）')
        dprint(f"  点击{'锁定' if kind == 'lock' else '眼睛'} @({x},{target}) "
               f"尝试{res['attempts']}（点击前重扫命中 ymid={target}）")
        pyautogui.click(x, target)
        time.sleep(0.5 if kind == 'lock' else 0.7)
        img2 = np.array(capture_region(WL, WT, WW, WH))
        if _row_target_state(img2, WL, WT, WW, kind, target):
            res.update(ok=True, reason='flipped')
            return res
        # 未翻转：下一轮循环重扫，若行又漂移会重新对齐（①），否则继续判不响应（②）

    res['reason'] = 'unresponsive'
    return res


def _estimate_vshift(pre_ymids, post_ymids, tol=6) -> int:
    """滚动前后整摞轨道的纵向位移（post - pre），用于平移跨滚动累计的覆盖坐标。

    取"能匹配上最多行"的位移；匹配不足 2 行视为估计失败返回 0（不平移，
    宁可覆盖少算而判失败，也不能虚增覆盖把脏轨道放过）。
    """
    best_d, best_m = 0, 0
    for p in pre_ymids:
        for q in post_ymids:
            d = q - p
            if d == 0:
                continue
            m = sum(1 for x in pre_ymids
                    if any(abs(x + d - y) <= tol for y in post_ymids))
            if m > best_m or (m == best_m and abs(d) > abs(best_d)):
                best_d, best_m = d, m
    return best_d if best_m >= 2 else 0


def _count_distinct_ys(ys, tol=10) -> List[float]:
    """把一组 y 坐标按容差聚类，返回聚类中心列表（用于累计"见过几行"）。"""
    out: List[float] = []
    for y in sorted(float(v) for v in ys):
        if not out or y - out[-1] > tol:
            out.append(y)
    return out


def _prescan_track_coverage(win, expected_tracks, max_steps=12) -> dict:
    """恢复前覆盖预扫：**只扫不点**，滚遍整条时间线确认全部轨道都能扫到。

    用户口径：覆盖不够就不许动手。上一轮只复用 4 行扫描就硬点（3/8 覆盖），
    ymid=855 点 2 次判 unresponsive，本质是"只看得见 3 行还在硬干"。

    计数：每次滚动后用整摞位移把已见行坐标平移到新视口再并入当前行；
    位移估计失败（无法对齐累计坐标）时不猜，最终判覆盖证伪。
    返回 {'ok','count','steps','ambiguous','reason'}。
    """
    rows, _img, _wl, _wt, _ww, _eye = scan_tracks(win)
    if not rows:
        return {'ok': False, 'count': 0, 'steps': 0, 'ambiguous': False,
                'reason': 'no_rows'}
    acc = [float(r['ymid']) for r in rows]
    uniq = _count_distinct_ys(acc)
    dprint(f"  覆盖预扫起点: 本屏 {len(rows)} 行，累计不同行 "
           f"{len(uniq)}/{expected_tracks or '?'}")
    if expected_tracks and len(uniq) >= expected_tracks:
        return {'ok': True, 'count': len(uniq), 'steps': 0,
                'ambiguous': False, 'reason': 'ok'}

    done = {'up': False, 'down': False}
    steps = 0
    ambiguous = False
    direction = 'up'
    while steps < max_steps and not (done['up'] and done['down']):
        if done[direction]:
            direction = 'down'
            if done['down']:
                break
        pre = [r['ymid'] for r in rows]
        rr = scroll_timeline_vertical(win, direction)
        steps += 1
        if not rr.get('ok'):
            done[direction] = True
            dprint(f"  覆盖预扫[{direction}] 滚不动（{rr.get('reason')}）→ 该端到头")
            continue
        rows, _img, _wl, _wt, _ww, _eye = scan_tracks(win)
        if not rows:
            continue
        shift = _estimate_vshift(pre, [r['ymid'] for r in rows])
        if shift:
            acc = [y + shift for y in acc]
        else:
            ambiguous = True
            dprint(f"  ⚠️ 覆盖预扫[{direction}] 位移估计失败，累计坐标无法对齐")
        acc.extend(float(r['ymid']) for r in rows)
        uniq = _count_distinct_ys(acc)
        dprint(f"  覆盖预扫[{direction}] 第{steps}步: 本屏 {len(rows)} 行，"
               f"累计不同行 {len(uniq)}/{expected_tracks or '?'}")
        if expected_tracks and len(uniq) >= expected_tracks:
            return {'ok': True, 'count': len(uniq), 'steps': steps,
                    'ambiguous': ambiguous, 'reason': 'ok'}

    ok = (not expected_tracks) or (len(uniq) >= expected_tracks and not ambiguous)
    if not ok:
        dprint(f"  ❌ 覆盖预扫不足：累计 {len(uniq)}/{expected_tracks or '?'} 行"
               f"（ambiguous={ambiguous}，steps={steps}）→ 不允许开始点击")
    return {'ok': ok, 'count': len(uniq), 'steps': steps,
            'ambiguous': ambiguous, 'reason': 'ok' if ok else 'coverage_insufficient'}


def restore_all_tracks(win, max_rounds=6, scan_hint=None, expected_tracks=None) -> dict:
    """
    收尾合并操作：**先覆盖预扫（只扫不点）**，确认能扫到全部轨道后，再边扫边
    解锁/恢复可见，边滚边累计覆盖，最后终验。

    判据：
      ⓪ 恢复前预扫：滚遍时间线累计不同行，覆盖 < expected_tracks 直接返回
         reason='coverage_insufficient'，**一次都不点**。不再复用调用方那 4 行
         局部扫描（上一轮 3/8 覆盖硬点就是它导致的）。
      ① 每轮只处理**当前可视区**里的脏行：锁定行点锁列、隐藏行点眼睛列，
         点后轻量复检；复检不干净就下一轮重扫重试（行可能漂移）。
      ② 可视区干净后向未走到的方向滚一屏，把视口外的轨道滚进来继续处理；
         每轮记录覆盖（已确认干净的轨道 ymid），滚动后按整摞位移平移坐标。
      ③ expected_tracks 默认读 draft_content.json 的真实轨道数（不再用
         可见行数 + 1）；累计覆盖 >= expected_tracks 才算齐。
      ④ 上下两端都滚不动仍覆盖不足 → reason='scroll_unavailable'（看不见 ≠ 锁着），
         上层据此落 track_restore_failed，不再混进 unknown。

    scan_hint 仅在读不到草稿轨道数（expected_tracks=0）时兜底复用。
    返回 {'unlocked','unhidden','ok','reason','still_locked','still_hidden',
          'scroll_unavailable','scroll_tried','drift_rounds','covered',
          'expected_tracks','prescan'}
    """
    from ui_calibration import get_param
    WL, WT, WW, WH = get_window_rect(win)
    _ensure_geom(win, WL, WT, WW, WH)
    lock_x = WL + _hdr_x(int(get_param('track_header', 'lock_x', 50)))

    if not expected_tracks:
        expected_tracks = _draft_track_count()
    expected_tracks = int(expected_tracks or 0)
    if expected_tracks:
        dprint(f"  预期轨道数 = {expected_tracks}（draft_content.json tracks 长度）")
    ledger = _draft_track_ledger()
    if ledger:
        dprint(f"  草稿轨道台账（数组自下而上，idx0=时间线最底部）: {ledger}")
    else:
        dprint("  ⚠️ 草稿轨道台账为空（无法把屏幕行映射回 draft 轨序号）")

    total_unlocked = total_unhidden = 0
    covered = set()          # 已确认干净的轨道（ymid，滚动后按位移补偿）
    scroll_tried = []
    drift_rounds = 0
    top_reached = bottom_reached = False
    clean_confirmed = False
    scroll_unavailable = False
    still_locked: List[int] = []
    still_hidden: List[int] = []
    unresponsive_rows: List[dict] = []
    rows = img = eye_x = None
    tr = _safe_timeline_rect(win)

    # ⓪ 覆盖预扫（只扫不点）：必须先把全部轨道滚进视野，覆盖不足一次都不点。
    #    不再复用调用方 scan_hint —— 上一轮复用的 4 行只覆盖 3/8，却照着硬点。
    prescan = {'ok': True, 'count': 0, 'steps': 0, 'ambiguous': False,
               'reason': 'skipped'}
    if expected_tracks:
        prescan = _prescan_track_coverage(win, expected_tracks)
        dprint(f"  覆盖预扫结果: {prescan}")
        if not prescan.get('ok'):
            return {'unlocked': 0, 'unhidden': 0, 'ok': False,
                    'reason': 'coverage_insufficient',
                    'still_locked': [], 'still_hidden': [],
                    'scroll_unavailable': False, 'scroll_tried': [],
                    'drift_rounds': 0, 'covered': [],
                    'expected_tracks': expected_tracks,
                    'unresponsive': [], 'ledger': ledger, 'prescan': prescan}
    elif scan_hint and scan_hint[0]:
        rows, img, eye_x = scan_hint[0], scan_hint[1], scan_hint[5]
        dprint(f"  [reuse] 未读到草稿轨道数，仅此情况复用上一阶段扫描（{len(rows)} 行）")

    for rnd in range(max_rounds):
        if rows is None:
            rows, img, _, WT, _, eye_x = scan_tracks(win)
            tr = _safe_timeline_rect(win)
            if not rows:
                return {'unlocked': total_unlocked, 'unhidden': total_unhidden,
                        'ok': False, 'reason': 'no_rows', 'still_locked': [],
                        'still_hidden': [], 'scroll_unavailable': False,
                        'scroll_tried': scroll_tried, 'drift_rounds': drift_rounds,
                        'covered': sorted(covered), 'expected_tracks': expected_tracks}

        # ① 行位置复检：插入新轨/滚动都会让 ymid 失效，先按当前屏幕重排
        rows, drifted, img, WT = _reflow_rows(win, rows, tr)
        if drifted:
            drift_rounds += 1
            dprint(f"  ⚠️ 第{rnd + 1}轮: 轨道行位置漂移，按当前可视位置重排"
                   f"（{len(rows)} 行，旧 ymid 不再作为判据）")
        if img is None:      # 轻量检测拿不到图时，退回一次整窗抓屏
            img, _sl0, _sh0 = _light_row_state(win, WL, WT, WW, WH, rows)
            WT = get_window_rect(win)[1]

        in_view = [r for r in rows if _row_in_view(r['ymid'], tr)]
        locked = [r['ymid'] for r in in_view if is_row_locked(img, WT, r['ymid'])]
        hidden = [r['ymid'] for r in in_view if r['hidden']]

        # 逐行恢复：每次点击前重扫定位（防坐标过期），点击后立即复检（防假成功）
        dirty = [('lock', y) for y in locked] + [('eye', y) for y in hidden]
        unresponsive = []
        for kind, y in dirty:
            rr = _restore_dirty_row(win, kind, y, rows, lock_x, eye_x, ledger)
            if rr.get('drifted'):
                dprint(f"  📍 坐标修正[{kind}]：上一轮 ymid={rr['prev_ymid']} → "
                       f"点击前重扫 ymid={rr['click_ymid']}（位移 {rr['shift']:+d}px，"
                       f"旧坐标已过期）")
            if rr['ok']:
                if kind == 'lock':
                    total_unlocked += 1
                else:
                    total_unhidden += 1
                dprint(f"  {'🔓' if kind == 'lock' else '👁'} {kind} 已恢复 "
                       f"ymid={rr['click_ymid']}（尝试{rr['attempts']}次，{rr['reason']}）")
            else:
                unresponsive.append(rr)
                ev = rr.get('evidence') or {}
                dprint(f"  ❌ {kind} 点击无效 ymid={rr['prev_ymid']}"
                       f"（点击前重扫 ymid={rr.get('click_ymid')}）reason={rr['reason']} "
                       f"尝试{rr['attempts']}次 坐标=({rr.get('click_x')},{rr.get('click_ymid')}) "
                       f"type={ev.get('type')} hidden={ev.get('hidden')} "
                       f"锁定条带均值={ev.get('lock_mean')} 蓝像素={ev.get('lock_blue')} "
                       f"草稿轨猜测={ev.get('track_guess')}")
        if unresponsive:
            # 不再"点一次再看还是锁着"地空转：同一行重扫命中同一位置且点击无效即停
            unresponsive_rows = unresponsive
            rows = None
            break

        # ② 轻量复检 + 本轮覆盖累计（点击后行可能漂移，先重排再判）
        time.sleep(0.8)
        rows, drifted, img, WT = _reflow_rows(win, rows, tr)
        if drifted:
            drift_rounds += 1
            dprint("  ⚠️ 点击后行位置再次漂移，复检按新 y 重判")
        if img is None:      # 没有可用图（拿不到时间线）→ 退回整窗抓屏直读
            img, _sl1, _sh1 = _light_row_state(win, WL, WT, WW, WH, rows)
            WT = get_window_rect(win)[1]
        in_view = [r for r in rows if _row_in_view(r['ymid'], tr)]
        still_locked = [r['ymid'] for r in in_view if is_row_locked(img, WT, r['ymid'])]
        still_hidden = [r['ymid'] for r in in_view if r['hidden']]
        clean_now = [r['ymid'] for r in in_view
                     if r['ymid'] not in still_locked and r['ymid'] not in still_hidden]
        covered.update(clean_now)
        dprint(f"  第{rnd + 1}轮覆盖累计: 可视干净 {len(clean_now)} 行，"
               f"累计 {len(covered)}/{expected_tracks or '?'} 行")

        if still_locked or still_hidden:
            dprint(f"  ⚠️ 复检未干净: 仍锁定={still_locked} 仍隐藏={still_hidden}，"
                   f"下一轮重扫重试")
            rows = None          # 下一轮用完整 scan_tracks 重扫
            continue

        if not expected_tracks:
            # 读不到草稿轨道数：退化为旧判据（当前可视区干净即完成），不做无尽滚动
            clean_confirmed = True
            break
        if len(covered) >= expected_tracks:
            clean_confirmed = True
            break
        if top_reached and bottom_reached:
            break

        # ③ 边滚边清：朝还没走到的方向滚一屏，把视口外的轨道滚进来
        direction = 'up' if bottom_reached else 'down'
        pre_ymids = [r['ymid'] for r in rows]
        rr = scroll_timeline_vertical(win, direction)
        scroll_tried.append({'dir': direction, **rr})
        if not rr['ok']:
            if direction == 'down':
                bottom_reached = True
            else:
                top_reached = True
            dprint(f"  ⚠️ {direction} 方向滚不动（{rr.get('reason')}）→ 标记该端到底")
            rows = None
            continue
        rows, img, _, WT, _, eye_x = scan_tracks(win)
        tr = _safe_timeline_rect(win)
        if not rows:
            continue
        shift = _estimate_vshift(pre_ymids, [r['ymid'] for r in rows])
        if shift:
            covered = {y + shift for y in covered}
            dprint(f"  滚动后轨道整体位移 {shift:+d}px，已干净坐标同步平移")
        else:
            dprint("  ⚠️ 滚动后位移估计失败，已干净坐标不平移（可能低估覆盖）")

    # 终验：循环内没明确确认干净时，必须完整扫描复检，扫不到行也不能判绿
    if not clean_confirmed:
        rows2, img2, _, WT2, _, _ = scan_tracks(win)
        tr = _safe_timeline_rect(win)
        if not rows2:
            still_locked, still_hidden = [], []
            dprint("  ⚠️ 收尾终验未扫到轨道，无法确认干净")
        else:
            rows2, _d, img2, WT2 = _reflow_rows(win, rows2, tr)
            in_view = [r for r in rows2 if _row_in_view(r['ymid'], tr)]
            still_locked = [r['ymid'] for r in in_view
                            if is_row_locked(img2, WT2, r['ymid'])]
            still_hidden = [r['ymid'] for r in in_view if r['hidden']]
            covered.update(r['ymid'] for r in in_view
                           if r['ymid'] not in still_locked and r['ymid'] not in still_hidden)

    # 终验若已把"不响应"的行判干净（点击延迟生效），从失败明细里剔除；
    # 仍脏或已滚出可视区（无法确认）的保留——宁可判失败也不虚报
    if unresponsive_rows and not clean_confirmed:
        dirty_now = set(still_locked) | set(still_hidden)
        in_view_now = {r['ymid'] for r in (rows2 or [])}
        kept = []
        for u in unresponsive_rows:
            y = u.get('click_ymid')
            if y in dirty_now or y not in in_view_now:
                kept.append(u)
            else:
                dprint(f"  ℹ️ ymid={y} 终验已干净（点击延迟生效），不计入不响应")
        unresponsive_rows = kept

    # 覆盖不足 = 有轨道始终没被滚进可视区/没被扫到（不是"锁定"，也不能判绿）
    if expected_tracks and len(covered) < expected_tracks:
        scroll_unavailable = True
        dprint(f"  ⚠️ 覆盖不足：已确认干净 {len(covered)}/{expected_tracks} 条轨道，"
               f"缺失轨道仍在可视区外或未被扫到（看不见 ≠ 已解锁）")

    ok = (not scroll_unavailable and not still_locked and not still_hidden
          and not unresponsive_rows)

    # 结论分类：把"看不见"、"不响应"和"真锁着"分开
    if ok:
        reason = 'clean'
    elif unresponsive_rows:
        reason = 'lock_unresponsive'
    elif still_locked or still_hidden:
        reason = 'still_dirty'
    elif scroll_unavailable:
        reason = 'scroll_unavailable'
    else:
        reason = 'unverified'

    if reason == 'clean':
        dprint(f"  ✅ 收尾完成：解锁 {total_unlocked} 行、恢复可见 {total_unhidden} 行，"
               f"覆盖 {len(covered)}/{expected_tracks or '?'} 条轨道")
    elif reason == 'lock_unresponsive':
        dprint(f"  ❌ 有 {len(unresponsive_rows)} 行点击后状态不翻转（重扫命中同一位置，"
               f"不是坐标过期）："
               f"{[(u['kind'], u.get('click_ymid'), (u.get('evidence') or {}).get('type')) for u in unresponsive_rows]}"
               f" → 判定该行不响应锁列/眼睛点击，已停止盲点")
    elif reason == 'scroll_unavailable':
        dprint("  ⚠️ 有轨道不在可视区，且三种滚动模态都滚不到："
               "这是「看不见」，不能判成「锁定」；需人工滚动后复跑")
    return {'unlocked': total_unlocked, 'unhidden': total_unhidden, 'ok': ok,
            'reason': reason, 'still_locked': still_locked, 'still_hidden': still_hidden,
            'scroll_unavailable': scroll_unavailable, 'scroll_tried': scroll_tried,
            'drift_rounds': drift_rounds, 'covered': sorted(covered),
            'expected_tracks': expected_tracks, 'unresponsive': unresponsive_rows,
            'ledger': ledger, 'prescan': prescan}


def hide_text_tracks_only(win) -> List[int]:
    """
    只隐藏字幕轨（text），其他轨道确保可见（点击后验证重试）。
    返回被隐藏的行索引。
    """
    rows, _, _, _, _, eye_x = scan_tracks(win)
    if not rows:
        dprint("hide_text_tracks_only: 没找到轨道行")
        return []
    dprint(f"轨道行 ({len(rows)}): {[(r['type'], r['hidden']) for r in rows]}")

    toggled = []
    for i, r in enumerate(rows):
        want = (r['type'] == 'text')
        if _set_row_hidden(win, eye_x, r['ymid'], want, f"[{r['type']}]"):
            if want:
                toggled.append(i)
        else:
            dprint(f"  ⚠️ 行 {i} [{r['type']}] 设置 hidden={want} 失败")
    return toggled


# ============ 主轨识别 + 预设轨整理（JSON 驱动，准入/正式共用） ============

def find_main_track(win, rows, img=None):
    """
    主轨识别（双重校验，两项都过才算主轨）：
      M1: type=='video' 且 ymid 最大（剪映主轨恒在时间线最底部）
      M2: 该行 cover_x_range 区间对比度列数 >= min_cols（主轨专属 📌封面按钮）
    返回 (main_row, m2_ok)；无 video 行或 M2 未过 → (None, False)。

    ⚠️ 不再"M2 未过也按 M1 采用"：上一轮主轨片段被转成音频后主轨变空行，
    最底 video 行实际是下方预设轨，M1 把它当主轨 → 后面的判据全部跟着错。
    宁可返回 None 让调用方报错/人工复核，也不拿预设轨顶替主轨。
    """
    WL, WT, WW, WH = get_window_rect(win)
    _ensure_geom(win, WL, WT, WW, WH)   # S3-B1：封面列同样锚定时间线左缘
    if img is None:
        img = np.array(capture_region(WL, WT, WW, WH))
    video_rows = [r for r in rows if r['type'] == 'video']
    if not video_rows:
        dprint("find_main_track: 无 video 类型行")
        return None, False
    main = max(video_rows, key=lambda r: r['ymid'])
    cxr = get_param('main_track', 'cover_x_range', [120, 160])
    cx0, cx1 = _hdr_x(cxr[0]), _hdr_x(cxr[1])
    ct = get_param('main_track', 'contrast_threshold', 15)
    mc = int(get_param('main_track', 'min_cols', 3))
    h = img.shape[0]
    y0 = max(0, main['ymid'] - WT - 25)
    y1 = min(h, main['ymid'] - WT + 25)
    m2_ok = False
    if y1 > y0 and cx1 > cx0:
        strip = img[y0:y1, cx0:cx1, :3].astype(int)
        m2_ok = int(((strip.max(axis=(0, 2)) - strip.min(axis=(0, 2))) > ct).sum()) >= mc
    if not m2_ok:
        dprint(f"  ❌ 主轨校验失败: 最底 video 行 ymid={main['ymid']} 未通过 M2 封面按钮校验，"
               f"判为下方预设轨而非主轨（拒绝顶替）")
        return None, False
    dprint(f"  ✅ 主轨双重校验通过: ymid={main['ymid']} (M1最底video + M2封面按钮)")
    return main, True


def _strip_mean(img, WT, ymid, strip, half_h=12):
    """取锁列条带的 RGB 均值（用于锁定态签名比对）"""
    x0, x1 = _hdr_x(strip[0]), _hdr_x(strip[1])
    y0 = max(0, ymid - WT - half_h)
    y1 = min(img.shape[0], ymid - WT + half_h)
    if x1 <= x0 or y1 <= y0:
        return None
    reg = img[y0:y1, x0:x1, :3].astype(float)
    if reg.size == 0:
        return None
    return reg.reshape(-1, 3).mean(0)


def is_row_locked(img, WT, ymid) -> bool:
    """
    锁定态检测（**蓝像素为主判据**）：
      ① 蓝像素 >= 阈值 → 确实锁着（真锁时蓝量充足，判据不退化）；
      ② 蓝像素 == 0 → 一定没锁。深色轨道底色的亮度条带不作为锁态依据
         （上一轮 ymid=855 就是被 lock_on_mean 亮度最近邻误判成"已锁"，
         点击后状态不翻转，被判 unresponsive）；
      ③ 0 < 蓝像素 < 阈值（弱蓝，图标边缘/半透明）→ 有已学习签名时用 RGB
         最近邻兜底，否则按未锁处理。
    """
    strip = get_param('track_header', 'lock_strip', [38, 62])
    thresh = int(get_param('track_header', 'lock_blue_threshold', 8))
    n_blue = _strip_blue_count(img, WT, ymid, strip)
    if n_blue >= thresh:
        return True
    if n_blue == 0:
        return False
    on = get_param('track_header', 'lock_on_mean', None)
    off = get_param('track_header', 'lock_off_mean', None)
    cur = _strip_mean(img, WT, ymid, strip)
    if cur is not None and on and off:
        d_on = float(np.linalg.norm(cur - np.array(on, dtype=float)))
        d_off = float(np.linalg.norm(cur - np.array(off, dtype=float)))
        return d_on < d_off
    return False


def lock_track(win, ymid, want_locked=True, max_retry=3) -> bool:
    """
    锁定/解锁某行轨道（点 🔒 x=lock_x，JSON 驱动）。
    状态感知：已是目标态直接返回；点击后验证失败自动重试。
    首次点击会**自动学习**锁定态像素签名（lock_on_mean / lock_off_mean）并落盘，
    之后 is_row_locked 用 RGB 均值最近邻判定，不再依赖未校准的蓝像素猜测。
    """
    from ui_calibration import update_param
    WL, WT, WW, WH = get_window_rect(win)
    _ensure_geom(win, WL, WT, WW, WH)   # S3-B1：锁定列同样锚定时间线左缘
    lock_x = int(get_param('track_header', 'lock_x', 50))
    lx = WL + _hdr_x(lock_x)
    strip = get_param('track_header', 'lock_strip', [38, 62])
    for attempt in range(max_retry):
        img = np.array(capture_region(WL, WT, WW, WH))
        if is_row_locked(img, WT, ymid) == want_locked:
            return True
        before = _strip_mean(img, WT, ymid, strip)
        op_mark('点击锁定',
                f'ymid={ymid} → {"锁定" if want_locked else "解锁"} '
                f'@({lx},{ymid}) 尝试{attempt + 1}')
        pyautogui.click(lx, ymid)
        time.sleep(0.5)
        img2 = np.array(capture_region(WL, WT, WW, WH))
        after = _strip_mean(img2, WT, ymid, strip)

        # 首次学习锁定签名（只在未学习时触发一次）
        if before is not None and after is not None and not get_param(
                'track_header', 'lock_on_mean', None):
            if want_locked:
                on_v, off_v = after, before
            else:
                on_v, off_v = before, after
            update_param('track_header', 'lock_off_mean',
                         [round(float(v), 1) for v in off_v])
            update_param('track_header', 'lock_on_mean',
                         [round(float(v), 1) for v in on_v])
            dprint(f"  📌 锁定态签名已学习 ymid={ymid}")
            return True

        if is_row_locked(img2, WT, ymid) == want_locked:
            dprint(f"  🔒 锁定验证通过 ymid={ymid}")
            return True
    dprint(f"  ⚠️ 锁定验证未通过 ymid={ymid}（已重试 {max_retry} 次）")
    return False


def hide_preset_tracks(win):
    """
    预设轨整理（准入/正式流程共用的标准入口）：
      ① find_main_track 双重校验定主轨
      ② 非主轨 **从下往上** 逐行：lock_track 锁定 → _set_row_hidden 隐藏（验证重试）
      ③ 每轮结束重扫，未隐藏干净的继续下一轮（最多 3 轮）
      ④ 主轨确保可见
    返回 (main_row, preset_rows)；识别失败 main_row=None。

    ⚠️ 关键教训：隐藏某行后，**该行下方各行会整体上移**（约 46px），
    若沿用隐藏前的 ymid 继续点，点击会落在错误行上（实测第 3 条轨连点 10 次全败）。
    所以必须：从最下面一条预设轨开始（下方只有主轨，互不影响），
    并且每轮重新 scan_tracks 拿最新 ymid。
    """
    rows, img, WL, WT, WW, eye_x = scan_tracks(win)
    if not rows:
        dprint("hide_preset_tracks: 没找到轨道行")
        return None, []
    main, m2_ok = find_main_track(win, rows, img)
    if main is None:
        return None, []
    all_presets = [r for r in rows if r is not main]
    dprint(f"预设轨 {len(all_presets)} 条待 锁定+隐藏（主轨 ymid={main['ymid']}, M2={'过' if m2_ok else '未过'}）")

    for rnd in range(3):
        rows, img, WL, WT, WW, eye_x = scan_tracks(win)
        main, _ = find_main_track(win, rows, img)
        if main is None:
            dprint("  ⚠️ 重扫后主轨丢失，中止预设轨整理")
            break
        todo = [r for r in rows if r is not main and not r['hidden']]
        if not todo:
            break
        if rnd:
            dprint(f"  第 {rnd + 1} 轮重扫：仍有 {len(todo)} 条未隐藏")
        # 从下往上（ymid 降序）：隐藏靠下的行不影响上方行的位置
        for r in sorted(todo, key=lambda x: -x['ymid']):
            lock_track(win, r['ymid'], want_locked=True)
            if not _set_row_hidden(win, eye_x, r['ymid'], True,
                                   f"预设轨[{r['type']}] ymid={r['ymid']}"):
                dprint(f"  ⚠️ 预设轨 ymid={r['ymid']} 隐藏失败，下轮重试")

    # 主轨保可见（用最新扫描的 ymid）
    rows, img, WL, WT, WW, eye_x = scan_tracks(win)
    main_now, _ = find_main_track(win, rows, img)
    if main_now:
        _set_row_hidden(win, eye_x, main_now['ymid'], False, "主轨[保可见]")
    return main_now or main, all_presets
