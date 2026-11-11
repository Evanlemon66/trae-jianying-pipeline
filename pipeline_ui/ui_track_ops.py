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
    """
    rows, img, _, _, _, eye_x = scan_tracks(win)
    if not rows:
        dprint("hide_tracks_except_main: 没找到轨道行")
        return []
    main, _m2 = find_main_track(win, rows, img)
    if main is None:
        main = rows[-1]
        dprint("  ⚠️ 无 video 行，按最底行当主轨")
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


def restore_all_tracks(win, max_rounds=2, scan_hint=None) -> dict:
    """
    收尾合并操作：批量解锁 → 批量恢复可见 → 轻量复检。
    全批量化：不再逐行调 lock_track/_set_row_hidden（它们内部各自 scan+截图 ≈ 2-3s/行），
    而是裸点 lock_x/eye_x 坐标让剪映批量响应。

    scan_hint=(rows, img, WL, WT, WW, eye_x)：复用调用方刚扫过的结果，
    跳过入口 scan_tracks（全轨可见时 UIA 枚举可达 ~17s）。
    收尾验证用 _light_row_state 轻量像素判定；若不干净才回退完整 scan_tracks 重试一轮。
    返回 {'unlocked': n, 'unhidden': n, 'ok': bool}
    """
    from ui_calibration import get_param
    WL, WT, WW, WH = get_window_rect(win)
    _ensure_geom(win, WL, WT, WW, WH)
    lock_x = WL + _hdr_x(int(get_param('track_header', 'lock_x', 50)))

    total_unlocked = total_unhidden = 0
    ok = True
    clean_confirmed = False
    rows = img = eye_x = None
    if scan_hint and scan_hint[0]:
        rows, img, eye_x = scan_hint[0], scan_hint[1], scan_hint[5]
        dprint(f"  [reuse] 复用上一阶段轨道扫描（{len(rows)} 行，跳过入口 scan_tracks）")

    for rnd in range(max_rounds):
        if rows is None:
            rows, img, _, WT, _, eye_x = scan_tracks(win)
            if not rows:
                return {'unlocked': total_unlocked, 'unhidden': total_unhidden, 'ok': False}

        locked = [r['ymid'] for r in rows if is_row_locked(img, WT, r['ymid'])]
        hidden = [r['ymid'] for r in rows if r['hidden']]

        if not locked and not hidden:
            if rnd == 0:
                dprint("  所有轨道均未锁定且可见")
            break

        # ① 批量裸点解锁（跳过 lock_track 内部的逐行 scan+截图验证）
        if locked:
            dprint(f"  解锁 {len(locked)} 行: {locked}")
            for y in locked:
                op_mark('点击锁定', f'ymid={y} → 解锁 @({lock_x},{y}) 尝试1')
                pyautogui.click(lock_x, y)
                time.sleep(0.4)
            time.sleep(0.8)  # 让剪映批量响应解锁

        # ② 批量裸点恢复可见（跳过 _set_row_hidden 内部的 scan+候选偏移重试）
        if hidden:
            dprint(f"  恢复可见 {len(hidden)} 行: {hidden}")
            for y in hidden:
                op_mark('点击眼睛', f'ymid={y} → 恢复可见 @({eye_x},{y}) 尝试1')
                pyautogui.click(eye_x, y)
                time.sleep(0.4)
            time.sleep(0.8)  # 让剪映批量响应恢复

        total_unlocked += len(locked)
        total_unhidden += len(hidden)

        # ③ 轻量复检（单次抓屏 + 已知 y 像素判定，~0.8s）
        #    剪映批量响应有渲染延迟（实测批量点完 5 行后立刻抓屏仍全判为隐藏），
        #    所以先等 1s 再检，最多重试 3 次（间隔 1s），仍不干净才回退完整扫描。
        time.sleep(1)
        still_l = still_h = None
        for _try in range(3):
            img, still_l, still_h = _light_row_state(win, WL, WT, WW, WH, rows)
            if not still_l and not still_h:
                break
            time.sleep(1)
        if not still_l and not still_h:
            clean_confirmed = True
            rows = None          # 干净了，无需再扫
            break
        dprint(f"  ⚠️ 轻量复检未干净: 仍锁定={still_l} 仍隐藏={still_h}，回退完整扫描重试")
        rows = None              # 下一轮用完整 scan_tracks 重扫

    # 最终验证：未被轻量复检明确确认干净时，必须完整扫描；扫不到行也不能判绿。
    if not clean_confirmed:
        rows2, img2, _, WT2, _, _ = scan_tracks(win)
        still_locked = [r['ymid'] for r in rows2 if is_row_locked(img2, WT2, r['ymid'])]
        still_hidden = [r['ymid'] for r in rows2 if r['hidden']]
        if not rows2:
            ok = False
            dprint("  ⚠️ 收尾终验未扫到轨道，无法确认干净")
        elif still_locked or still_hidden:
            ok = False
            dprint(f"  ⚠️ 收尾未干净: 仍锁定={still_locked} 仍隐藏={still_hidden}")
    if ok:
        dprint(f"  ✅ 收尾完成：解锁 {total_unlocked} 行、恢复可见 {total_unhidden} 行")
    return {'unlocked': total_unlocked, 'unhidden': total_unhidden, 'ok': ok}


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
    主轨识别（双重校验机制）：
      M1: type=='video' 且 ymid 最大（剪映主轨恒在时间线最底部）
      M2: 该行 cover_x_range 区间对比度列数 >= min_cols（主轨专属 📌封面按钮）
    返回 (main_row, m2_ok)；无 video 行返回 (None, False)。
    M2 未通过时仍按 M1 返回（打警告），由调用方决定是否中止/肉眼复核。
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
    if m2_ok:
        dprint(f"  ✅ 主轨双重校验通过: ymid={main['ymid']} (M1最底video + M2封面按钮)")
    else:
        dprint(f"  ⚠️ M2 封面校验未过 (ymid={main['ymid']})，按 M1 采用——建议肉眼复核")
    return main, m2_ok


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
    锁定态检测：
      ① 若准入已学习 lock_on_mean / lock_off_mean → RGB 均值最近邻（可靠）
      ② 否则退回蓝像素规则（签名为校准前的临时值，可能误判）
    """
    strip = get_param('track_header', 'lock_strip', [38, 62])
    on = get_param('track_header', 'lock_on_mean', None)
    off = get_param('track_header', 'lock_off_mean', None)
    cur = _strip_mean(img, WT, ymid, strip)
    if cur is not None and on and off:
        d_on = float(np.linalg.norm(cur - np.array(on, dtype=float)))
        d_off = float(np.linalg.norm(cur - np.array(off, dtype=float)))
        return d_on < d_off
    return _strip_blue_count(img, WT, ymid, strip) >= int(
        get_param('track_header', 'lock_blue_threshold', 8))


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
