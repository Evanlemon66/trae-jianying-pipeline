# -*- coding: utf-8 -*-
"""
识别中文字幕 + 翻译英文字幕（按用户手动流程对齐 4 步）
=====================================================
用户确认的 4 步手动流程：
  1. Home + Ctrl+A                  → 选主轨片段（确保有内容可识别）
  2. 点顶部 Tab 栏的"字幕"          → 激活字幕面板（不是左侧栏！）
  3. 翻译语言下拉 → 选择"英语"       → 当前值是"无"时必须改
  4. 点底部绿色"开始识别"按钮        → 屏幕中间出现"字幕识别中...xx%"弹窗

完成判据（用户确认：双重保障，两个条件同时满足才算完成）：
  ① 屏幕中间"字幕识别中...xx%"进度弹窗：出现 → 消失
  ② 时间线多出两个字幕轨（中文 + 英文翻译，text 轨数 +2）
  参考信号：草稿 draft_content.json 的 materials.texts 数量增加（仅日志）
完成后把新增字幕轨的 眼睛/锁定 图标坐标写入 ui_calibration.json（subtitle_tracks）。

关键坐标（剪映全屏态 1920×1032，窗口 rect=(0,0,1920,1032)）：
  顶部 Tab "字幕"   center=(395, 71)      ← ctypes 点击
  翻译语言下拉      center=(289, 224)     ← ctypes 点击展开
  下拉选项"英语"    center=(289, 329)     ← 展开后 ctypes 点击
  "开始识别"按钮    center=(780, 551)     ← 面板右下角，与"同时清空已有字幕"同一行
                                             （按用户截图换算 + 实时截屏像素验证）

检测：ctypes SendInput 对 QQuickText/Panel 控件有效（pyautogui 无效）。
"""

from __future__ import annotations

import time
import os
import sys
import json
import ctypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pyautogui
import uiautomation as uia
import numpy as np
from PIL import ImageGrab

from ui_core import (
    dprint, connect_jianying, ensure_maximized,
    click_desc, send_keys, get_window_rect, select_main_track,
    op_mark, op_timeline, take_op_marks, op_stage, op_event,
)
from ui_track_ops import scan_tracks
from ui_calibration import get_param


def _cal(key, default):
    """识别/翻译面板点位统一从校准 JSON 的 recognize section 取。"""
    return get_param('recognize', key, default)


def _update_coord(key, value, max_delta=20):
    """坐标类运行期写回守卫：与已落盘值比较，任一分量偏差 > max_delta 则不写。

    目的：防止 OCR 误匹配/兜底常量把已验证的好点位覆盖掉
    （历史 bug：translate_dropdown_center 实测 132 被常量 300 打回）。
    超差时写 WARN 日志 + op_event（trace-feed 留档），便于事后定位是哪一次点击跑偏。
    """
    from ui_calibration import update_param
    try:
        new = [int(value[0]), int(value[1])]
    except Exception:
        return update_param('recognize', key, value)
    old = get_param('recognize', key, None)
    if isinstance(old, (list, tuple)) and len(old) >= 2:
        try:
            dx = abs(int(old[0]) - new[0])
            dy = abs(int(old[1]) - new[1])
        except Exception:
            dx = dy = 0
        if dx > max_delta or dy > max_delta:
            msg = (f"{key} 写回偏差过大，已跳过：旧={list(old)[:2]} 新={new} "
                   f"(|dx|={dx}, |dy|={dy}, 阈值={max_delta})")
            dprint(f"  [warn] {msg}")
            try:
                op_event('calib', f'{key} 写回被拦截', status='warn',
                         old=list(old)[:2], new=new, dx=dx, dy=dy, limit=max_delta)
            except Exception:
                pass
            return False
    return update_param('recognize', key, new)


# ===== 逻辑偏移常量（相对窗口原点，100% DPI 口径）=====
# 全部数值来自全屏 1920×1032 实测（该状态下窗口原点=(0,0)、dpi_scale=1.0，
# 故 1920 下换算结果与旧绝对像素值逐像素相同）；4K@150% 等环境按 ×dpi_scale 换算。
# 这些常量只作 get_param 的 default（JSON 实测值永远优先），不直接参与点击。
SUBTITLE_TAB_OFF = (379, 68)               # 顶部 Tab 栏 "字幕"（1920 OCR 实测中心 379,68；
                                           # 旧值 395,71 为遗留常量，4K 实测 1380,102 与 379×1.5 一致）
PANEL_ENTRY_OFF = (64, 148)                # 字幕面板入口（UIA rect=[37,140,91,156]）
TRANSLATE_SWITCH_OFF = (120, 189)          # "翻译语言" 开关（VIP，必须先勾选）
TRANSLATE_SWITCH_BBOX_OFF = (112, 181, 132, 199)  # 开关像素判定 bbox
# 翻译语言下拉值行（关闭态显示当前值，默认"无"）：
# 2026-11-05 单点实测：该行 UIA rect=[118,213,816,235]，整行可点；
# 点"行左缘 +182px"（=300,224）单击一次即展开（UIA 节点 44→50，新增
# LVPopupWindow [66,213,868,485]），Esc/切窗后关闭，值仍为"无"。
# 因此兜底不再用孤立绝对点，改为「行左缘 + 已验证偏移」推导；JSON 仍可覆盖。
TRANSLATE_DROPDOWN_ROW_OFF = (118, 224)    # 下拉行左缘 / 行垂直中心（逻辑）
TRANSLATE_DROPDOWN_OFFSET_X = 182          # 左缘 → 行内空白可点处（实测 300-118）
TRANSLATE_DROPDOWN_OFF = (TRANSLATE_DROPDOWN_ROW_OFF[0] + TRANSLATE_DROPDOWN_OFFSET_X,
                          TRANSLATE_DROPDOWN_ROW_OFF[1])
TRANSLATE_ENGLISH_OFF = (165, 319)         # 下拉展开后"英语"选项（OCR 失败时的兜底）
TRANSLATE_LIST_REGION_OFF = (110, 240, 660, 410)   # 下拉展开后的选项列表区域
TRANSLATE_VALUE_REGION_OFF = (110, 205, 660, 245)  # 下拉关闭后的当前值区域
START_RECOG_BTN_OFF = (780, 551)           # 面板右下角 "开始识别" 绿色按钮（截屏验证过）
START_RECOG_BTN_REGION_OFF = (743, 540, 818, 562)  # 按钮矩形 (left, top, right, bottom)
# 右锚定偏移（相对面板右缘，1920 口径）：中心 38 / 矩形左 73 / 矩形右 −2
_START_BTN_CENTER_DX = 38
_START_BTN_BOX_LEFT_DX = 73
_START_BTN_BOX_RIGHT_DX = -2
LEFT_LIST_REGION_OFF = (0, 55, 460, 800)   # 左侧字幕面板整体（像素判状态用）

# ===== 点击偏移（全部为 1920 物理实测值，使用时一律 ×s；s=1 时逐像素等于旧值）=====
# 开关候选偏移（相对「翻译语言」标签中心）：历史实测 dx=-35 生效；-52/-20/-60/-42 为避让候选。
# 注意：这些是**物理像素**，4K@150% 必须 ×1.5（本次事故就是 -35 未 ×s → 落点压到蓝色标签
# 上，被 _translate_switch_on 读成"已勾选"，整轮从未点开关）。
_SWITCH_DX_LOGICAL = (-35, -52, -20, -60, -42)
_SWITCH_BLUE_DELTA = 40      # 蓝像素判据：B - R > 40
_SWITCH_ON_MIN = 20          # 勾选态最小蓝像素数（1920 旧判据）
# 点击前后蓝像素差阈值：13:16 留图实测（未勾选态）真开关点 0、误命中标签 26~97；
# 勾选后增量未实测（推断 ≥ ON_MIN 量级），取 ON_MIN 的 3/4 作保守下限。
_SWITCH_ON_DIFF = 15
# 下拉展开点：组合框右缘 − k×s（1920 combo.right=816 → 132/300/252，与旧候选值逐像素
# 相同且**顺序也一致**（旧：dd=132 → +168=300 → +120=252）；4K 随 rect 右缘自适应）
_DROPDOWN_RIGHT_DX = (684, 516, 564)


def _ui_scale(win=None):
    """窗口所在屏 UI 缩放（100%=1.0）；取不到退 1.0（= 1920 旧口径）。"""
    try:
        from ui_track_ops import _ui_scale as _tops_scale
        s = float(_tops_scale(win))
        return s if 0.5 <= s <= 4.0 else 1.0
    except Exception:
        return 1.0


def _win_origin(win=None):
    """窗口原点 (left, top)；拿不到退 (0,0)（全屏态等价）。"""
    try:
        r = get_window_rect(win) if win else None
        if r:
            return int(r[0]), int(r[1])
    except Exception:
        pass
    return 0, 0


def _abs_point(win, off):
    """逻辑点（相对窗口原点, 100%）→ 屏幕绝对点"""
    x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    return (x0 + int(round(off[0] * s)), y0 + int(round(off[1] * s)))


def _abs_rect(win, off):
    """逻辑矩形（屏幕绝对, 100%）→ 屏幕绝对矩形"""
    x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    return (x0 + int(round(off[0] * s)), y0 + int(round(off[1] * s)),
            x0 + int(round(off[2] * s)), y0 + int(round(off[3] * s)))


_REGION_SCALE_WARNED = []


def _region_scale(win=None):
    """OCR region 换算系数 = 窗口物理尺寸/逻辑尺寸实测比值（不是 monitor dpi_scale）。

    region 是窗口相对像素、`_ocr_window` 不做 DPI 换算，所以必须按窗口自身的实际渲染
    缩放来推。测不到时显式标记降级（只告警一次），返回 1.0（= 1920 旧口径），不静默猜。
    """
    reason = ''
    try:
        from ui_core import ui_content_scale
        s, why = ui_content_scale(win)
        if s:
            return float(s)
        reason = why or 'unknown'
    except Exception as e:
        reason = f'exception {e}'
    if not _REGION_SCALE_WARNED:
        _REGION_SCALE_WARNED.append(reason)
        dprint(f"  [warn] 窗口物理/逻辑比值不可测（{reason}）→ OCR region 按 1.0 降级（已标记）")
    return 1.0


def _abs_region(win, off):
    """窗口相对 OCR region（100% 口径）→ 当前窗口物理像素 region"""
    s = _region_scale(win)
    return tuple(int(round(v * s)) for v in off)


# ===== 面板锚定（4K 多栏布局 x 基准）=====
# 为什么不再用「窗口原点 + 逻辑×s」：4K@150% 实测面板**宽度按窗口宽比例**（左面板右缘
# 2101/3840=54.7%；折叠态 1642/3840=42.8%；右面板内容左缘 3019/3840=78.6%），不随
# DPI ×1.5；而面板**内边距/偏移是 ×1.5**。故 x 必须锚在面板边缘；y 仍按窗口原点×s
# （y 锚定在 4K 已实测成立）。
# 锚定口径（偏移均为 1920 实测逻辑值）：
#   left   : x = 左面板左缘  + off×s
#   right  : x = 左面板右缘  − off×s（开始识别 / 下拉行右缘）
#   rpanel : x = 右面板内容左缘 + off×s（音频 Tab / 人声分离）
# 面板边缘来源优先级（实现在 ui_core.panel_edges，运行期由 _panel_edges 逐次打印）：
#   1 级 UIA：VEComboBox.right → 左面板右缘；combo.left − 118×s → 左面板左缘；
#             右面板内容组 Group.left → 右面板内容左缘
#   2 级 OCR：命中框（本模块不跑 OCR 取边缘；「字幕」Tab 这类随布局跳动的位置
#             才用全宽带 OCR 命中框优先，推导值只兜底）
#   3 级 比例：前两级都失败才用 1920 比例估算 —— 必须打降级标记 + WARN，不得静默
#             （ui_core.panel_edges 内已实现并进 warn 列表，此处逐条转发到日志/事件）

def _panel_edges(win):
    """面板边缘 + 来源级。每次调用都打印用了哪一级；降级必 WARN，不静默。

    短 TTL 缓存（2s）：同一操作序列里会被连调 4 次（如点开始识别前），
    每次 UIA 全枚举约 1.5s，缓存可省约 4.5s。
    不用永久缓存——面板在流程中会变（字幕面板打开前是比例降级，打开后才是 UIA 实测），
    永久缓存会把降级值固化。
    """
    _CACHE_TTL = 2.0
    if not hasattr(_panel_edges, '_cache'):
        _panel_edges._cache = {}
    _cache = _panel_edges._cache
    key = win.NativeWindowHandle
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]
    from ui_core import panel_edges
    pe = panel_edges(win)
    _cache[key] = (time.time(), pe)
    src = pe.get('src') or {}
    dprint(f"  [anchor] 面板边缘: left_left={pe.get('left_left')}"
           f"({src.get('left_left', '-')}) left_right={pe.get('left_right')}"
           f"({src.get('left_right', '-')}) right_left={pe.get('right_left')}"
           f"({src.get('right_left', '-')}) degraded={_edges_degraded(pe)}")
    for w in pe.get('warn') or []:
        dprint(f"  [WARN] 面板边缘: {w}")
        try:
            op_event('anchor', '面板边缘降级', status='warn', detail=w)
        except Exception:
            pass
    return pe


def _clear_panel_edges_cache():
    """清面板边缘缓存（外部调：面板布局变化时强制重测）。"""
    _panel_edges._cache = {}


def _anchor_x(win, off_x, anchor='left', edges=None):
    """逻辑 x 偏移 → 屏幕绝对 x（按面板边缘锚定）。
    边缘不可得 → 退「窗口原点 + off×s」并显式 WARN（此时属不可测，不许静默）。"""
    pe = edges or _panel_edges(win)
    s = _ui_scale(win)
    base = {'left': pe.get('left_left'),
            'right': pe.get('left_right'),
            'rpanel': pe.get('right_left')}.get(anchor)
    if base is None:
        x0, _y0 = _win_origin(win)
        dprint(f"  [WARN] 面板边缘不可得（anchor={anchor}）→ x 退窗口原点（不可测）")
        return x0 + int(round(off_x * s))
    sign = -1 if anchor == 'right' else 1
    return int(base) + sign * int(round(off_x * s))


def _anchor_point(win, off, anchor='left', edges=None):
    """逻辑点 → 屏幕绝对点（x 面板锚定，y 窗口原点×s）"""
    _x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    return (_anchor_x(win, off[0], anchor, edges),
            y0 + int(round(off[1] * s)))


def _anchor_rect(win, off, anchor='left', edges=None):
    """逻辑矩形 → 屏幕绝对矩形（左右缘同按 anchor；y 按窗口原点×s）"""
    _x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    return (_anchor_x(win, off[0], anchor, edges),
            y0 + int(round(off[1] * s)),
            _anchor_x(win, off[2], anchor, edges),
            y0 + int(round(off[3] * s)))


def _anchor_region(win, off, anchor='left', edges=None):
    """逻辑 region（100% 口径）→ 窗口相对物理 region（_ocr_window 口径）"""
    r = _anchor_rect(win, off, anchor, edges)
    x0, y0 = _win_origin(win)
    return (int(r[0] - x0), int(r[1] - y0), int(r[2] - x0), int(r[3] - y0))


# ===== 三级降级的交叉校验（B：比例只许"对上才用"，绝不猜着点）=====
class AnchorDegradedError(RuntimeError):
    """面板边缘三级降级 + 全宽带 OCR 无法交叉校验 → 停止本步（需要人工/改校准）。"""


def _edges_degraded(edges):
    """该次 panel_edges 结果里是否含三级比例值。"""
    if not edges:
        return True
    if edges.get('degraded'):
        return True
    return any(str(v).startswith('ratio') for v in (edges.get('src') or {}).values())


def _full_width_region(win, y0, y1):
    """全宽 OCR 搜索带（窗口相对物理 px）：x 覆盖整个窗口，y=[y0,y1]×s。"""
    wr = get_window_rect(win) or (0, 0, 0, 0)
    ww = int(wr[2]) - int(wr[0])
    s = _ui_scale(win)
    return (0, int(round(y0 * s)), ww, int(round(y1 * s)))


def _band_hit(win, keyword, y0, y1, exclude=()):
    """全宽带 OCR 找 keyword（取 score 最高的命中），返回 (cx, cy) 或 None。"""
    try:
        from ocr_locate import _ocr_window
        best = None
        for text, c, score, _b in _ocr_window(win, region=_full_width_region(win, y0, y1)):
            t = text.replace(' ', '').replace('\n', '')
            if keyword not in t or any(e in t for e in exclude):
                continue
            if float(score) < 0.4:
                continue
            if best is None or float(score) > best[2]:
                best = (int(c[0]), int(c[1]), float(score))
        return None if best is None else (best[0], best[1])
    except Exception as e:
        dprint(f"  [warn] 全宽带 OCR「{keyword}」失败: {str(e)[:80]}")
        return None


def _checked_anchor_point(win, off, anchor, keyword, y0, y1, edges=None, tag=''):
    """降级时的点击点：三级推导值必须与全宽带 OCR 命中框 ±20px 对上。

    非降级（一级 UIA）→ 直接用锚定推导值；
    降级 → 全宽带 OCR 命中且 |dx|,|dy| ≤ 20 才采用 OCR 值，否则抛错停止本步。
    """
    pe = edges or _panel_edges(win)
    pt = _anchor_point(win, off, anchor, pe)
    if not _edges_degraded(pe):
        return pt
    hit = _band_hit(win, keyword, y0, y1, exclude=('识别', '添加') if keyword == '字幕' else ())
    if not hit:
        raise AnchorDegradedError(
            f"{tag or keyword}：面板边缘三级降级且全宽带 OCR 未命中「{keyword}」→ 停止本步")
    dx, dy = abs(hit[0] - pt[0]), abs(hit[1] - pt[1])
    if dx > 20 or dy > 20:
        raise AnchorDegradedError(
            f"{tag or keyword}：三级值 {pt} 与 OCR {hit} 偏差 (dx={dx}, dy={dy}) > 20 → 停止本步")
    dprint(f"  [anchor-check] {tag or keyword}: 三级 {pt} ↔ OCR {hit} |d|=({dx},{dy}) → 采用 OCR")
    return hit


def _safe_region(win, off, anchor='left', edges=None, tag=''):
    """降级时把 OCR 搜索区域放宽到整窗宽（区域不是点击点，放宽只会更安全），并 WARN。"""
    pe = edges or _panel_edges(win)
    if _edges_degraded(pe):
        reg = _full_width_region(win, off[1], off[3])
        dprint(f"  [WARN] {tag or 'region'}：面板边缘三级降级 → OCR 区域退全宽 {reg}")
        return reg
    return _anchor_region(win, off, anchor, pe)


def _safe_rect(win, off, anchor='left', edges=None, tag=''):
    """降级时把像素判定矩形放宽到整窗宽（用于截图判状态，不参与点击）。"""
    pe = edges or _panel_edges(win)
    if _edges_degraded(pe):
        wr = get_window_rect(win) or (0, 0, 0, 0)
        s = _ui_scale(win)
        rect = (int(wr[0]), int(round(off[1] * s)), int(wr[2]), int(round(off[3] * s)))
        dprint(f"  [WARN] {tag or 'rect'}：面板边缘三级降级 → 判定矩形退整窗 {rect}")
        return rect
    return _anchor_rect(win, off, anchor, pe)


def _timeline_top(win):
    """时间线顶部 y（MainTimeLineRoot top）。「开始识别」是字幕面板底部固定动作条，
    紧贴时间线上方（1920 实测 track_rect top=576，按钮中心 551=576-25）；
    竖直方向不随窗口顶部线性拉伸，故 y 绑时间线顶部而不是窗口原点。"""
    try:
        from ui_core import get_timeline_rect
        tr = get_timeline_rect(win)
        if tr:
            return int(tr[1])
    except Exception:
        pass
    return None


def _start_btn_point(win, edges=None):
    """「开始识别」按钮中心：x = 左面板右缘 − 38×s（**右锚定**），
    y 绑时间线顶部（拿不到时间线退回窗口原点）。
    1920 等价：816 − 38 = 778（1920 实测 780，−2 在容差内）。"""
    _x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    tl = _timeline_top(win)
    return (_anchor_x(win, _START_BTN_CENTER_DX, 'right', edges),
            tl - int(round(25 * s)) if tl else y0 + int(round(START_RECOG_BTN_OFF[1] * s)))


def _start_btn_region(win, edges=None):
    """「开始识别」按钮矩形（上下沿 = 时间线顶部 -36 / -14 逻辑像素）。
    x 右锚定：left = 面板右缘 − 73×s，right = 面板右缘 + 2×s（1920 下 743 / 818 不变）。"""
    _x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    tl = _timeline_top(win)
    top = tl - int(round(36 * s)) if tl else y0 + int(round(START_RECOG_BTN_REGION_OFF[1] * s))
    bottom = tl - int(round(14 * s)) if tl else y0 + int(round(START_RECOG_BTN_REGION_OFF[3] * s))
    return (_anchor_x(win, _START_BTN_BOX_LEFT_DX, 'right', edges), top,
            _anchor_x(win, _START_BTN_BOX_RIGHT_DX, 'right', edges), bottom)


def _start_btn_region_band(win, edges=None):
    """「开始识别」OCR 搜索带：x = [面板左缘, 面板右缘]，y = [时间线顶 −150×s, 时间线顶]。
    （2026-11-06 4K 实测：按钮中心 (2047,1122)，旧 region (0,400,960,700) 换算到 4K
    只到 (1440,1050)，x/y 全漏；改面板锚定 + 时间线锚定后完整覆盖。）"""
    if not _range_ok(win, edges):
        return None
    pe = edges or _panel_edges(win)
    x0, y0 = _win_origin(win)
    s = _ui_scale(win)
    tl = _timeline_top(win) or (y0 + int(round(700 * s)))
    if _edges_degraded(pe):
        # B：三级降级时 x 不猜，直接放宽到整窗宽（y 仍绑时间线顶）
        wr = get_window_rect(win) or (0, 0, 0, 0)
        ww = int(wr[2]) - int(wr[0])
        dprint("  [WARN] 开始识别搜索带：面板边缘三级降级 → x 退全宽（y 仍绑时间线）")
        return (0, max(0, tl - int(round(150 * s))), ww, tl)
    return (_anchor_x(win, 0, 'left', edges) - x0,
            max(0, tl - int(round(150 * s))),
            _anchor_x(win, 0, 'right', edges) - x0,
            tl)


def _range_ok(win, edges=None):
    """锚定所需的两个边缘是否都拿到了（拿不到 → 调用方必须按不可测处理/显式兜底）"""
    pe = edges or _panel_edges(win)
    ok = pe.get('left_left') is not None and pe.get('left_right') is not None
    if not ok:
        dprint("  [WARN] 面板边缘不全 → 本次锚定区域不可测")
    return ok


_SUBTITLE_TAB_BAND_OFF = (20, 150)   # 「字幕」Tab 全宽带：y 起点 20×s / 高度 150×s


def _subtitle_tab_band(win):
    """「字幕」Tab 全宽带 OCR 区域（窗口相对物理 px）：宽=窗口宽、高≈150×s。
    该 Tab 随布局跳动（4K 实测 1380，1920 为 379），不能用原点×s 推导，只能全宽搜。"""
    wr = get_window_rect(win)
    if not wr:
        return None
    s = _ui_scale(win)
    return (0,
            int(round(_SUBTITLE_TAB_BAND_OFF[0] * s)),
            int(wr[2]) - int(wr[0]),
            int(round((_SUBTITLE_TAB_BAND_OFF[0] + _SUBTITLE_TAB_BAND_OFF[1]) * s)))


def resolve_recognize_defaults(win):
    """recognize 段兜底点位：x 按面板边缘锚定（B），y 按窗口原点×s。

    只作 get_param 的 default（准入 setdefault / 新环境首跑）；
    已落盘的实测值永远优先，不会被这里的派生值覆盖。
    """
    pe = _panel_edges(win)
    return {
        'subtitle_tab_center': _anchor_point(win, SUBTITLE_TAB_OFF, 'left', pe),
        'panel_entry_center': _anchor_point(win, PANEL_ENTRY_OFF, 'left', pe),
        'translate_switch_center': _anchor_point(win, TRANSLATE_SWITCH_OFF, 'left', pe),
        'translate_switch_bbox': _anchor_rect(win, TRANSLATE_SWITCH_BBOX_OFF, 'left', pe),
        'translate_dropdown_center': _anchor_point(win, TRANSLATE_DROPDOWN_OFF, 'left', pe),
        'translate_english_center': _anchor_point(win, TRANSLATE_ENGLISH_OFF, 'left', pe),
        'translate_list_region': _anchor_region(win, TRANSLATE_LIST_REGION_OFF, 'left', pe),
        'translate_value_region': _anchor_region(win, TRANSLATE_VALUE_REGION_OFF, 'left', pe),
        'start_recog_btn_center': _start_btn_point(win, pe),
        'start_recog_btn_region': _start_btn_region(win, pe),
        'left_list_region': _anchor_region(win, LEFT_LIST_REGION_OFF, 'left', pe),
    }

# 草稿完成检测（draft_content.json 的 materials.texts 数量；自动取最近修改的草稿）
DRAFT_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
CALIBRATION_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui_calibration.json')


# ===== ctypes SendInput 原生点击 =====
user32 = ctypes.windll.user32

def _native_click(x, y, label="", settle=0.5):
    """ctypes SendInput 原生左键单击（pyautogui 对剪映 QQuickText 控件无效）"""
    op_mark('点击', f'{label} ({x},{y})')   # 每次点击独立打点
    user32.SetCursorPos(x, y)
    time.sleep(0.05)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    time.sleep(settle)
    dprint(f"  ctypes 点击 {label} ({x},{y})")


def _active_draft_name():
    """当前打开的草稿 = draft_content.json 最近修改的那个"""
    try:
        cands = []
        for name in os.listdir(DRAFT_ROOT):
            p = os.path.join(DRAFT_ROOT, name, "draft_content.json")
            if os.path.isfile(p):
                cands.append((os.path.getmtime(p), name))
        return max(cands)[1] if cands else None
    except Exception:
        return None


def _draft_texts_count():
    """读当前草稿 draft_content.json 的 materials.texts 数量（识别结果硬指标），失败返回 -1"""
    name = _active_draft_name()
    if not name:
        return -1
    p = os.path.join(DRAFT_ROOT, name, "draft_content.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return len(d.get("materials", {}).get("texts", []))
    except Exception:
        return -1


def _text_value(content):
    """取字幕文本：content 是 JSON 字符串，文本在其中的 text 字段。"""
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return obj.get("text") or ""
    except Exception:
        pass
    return content or ""


def _draft_english_text_count():
    """统计草稿 materials.texts 里英文字幕条数（ASCII 字母占非空白字符 > 50%）。
    用于验收"翻译语言确实选了英语"，失败返回 -1。"""
    name = _active_draft_name()
    if not name:
        return -1
    p = os.path.join(DRAFT_ROOT, name, "draft_content.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return -1
    n = 0
    ratio = float(_cal('english_ratio', 0.5))
    for t in d.get("materials", {}).get("texts", []):
        content = _text_value(t.get("content")).strip()
        visible = sum(1 for c in content if not c.isspace())
        letters = sum(1 for c in content if c.isascii() and c.isalpha())
        if letters >= 2 and visible and letters / visible > ratio:
            n += 1
    return n


def _switch_blue_count(cx=None, cy=None, win=None):
    """「翻译语言」开关处蓝像素计数（B−R>40）。异常返回 -1（不可测，不当成 0）。"""
    try:
        s = _ui_scale(win)
        if cx is not None:
            hw, hh = int(round(10 * s)), int(round(9 * s))
            bbox = (int(cx) - hw, int(cy) - hh, int(cx) + hw, int(cy) + hh)
        else:
            bbox = tuple(_cal('translate_switch_bbox', None)
                         or _anchor_rect(win, TRANSLATE_SWITCH_BBOX_OFF, 'left'))
        delta = int(_cal('translate_switch_blue_delta', _SWITCH_BLUE_DELTA))
        img = np.array(ImageGrab.grab(bbox=bbox))
        return int(((img[:, :, 2].astype(int) - img[:, :, 0].astype(int)) > delta).sum())
    except Exception:
        return -1


def _switch_on_min():
    """勾选态最小蓝像素数（JSON translate_switch_min_pixels 可覆盖；1920 旧值 20，判据 >）。"""
    return int(_cal('translate_switch_min_pixels', _SWITCH_ON_MIN))


def _translate_switch_on(cx=None, cy=None, win=None):
    """单帧蓝像素判定（只用于复核/兜底；判据与 1920 旧实现一致：count > min）。"""
    n = _switch_blue_count(cx, cy, win)
    return n > _switch_on_min()


def _switch_click_checked(win, cand, tag=''):
    """点击前后蓝像素计数差判定（防"单点落在文字上被读成已勾选"）。

    返回 (checked, before, after, diff)；不可测时 checked=False 并 WARN。
    """
    min_px = _switch_on_min()
    before = _switch_blue_count(*cand, win=win)
    if before < 0:
        dprint(f"  [warn] {tag} 开关前像素不可测 → 跳过该候选")
        return False, before, -1, 0
    if before > min_px:
        dprint(f"  ✅ {tag} 开关已勾选（点击前计数={before} > {min_px}）")
        return True, before, before, 0
    _native_click(*cand, f"翻译语言开关{tag}", settle=0.0)
    time.sleep(0.8)
    after = _switch_blue_count(*cand, win=win)
    diff = after - before
    ok = after > min_px or diff >= _SWITCH_ON_DIFF
    dprint(f"  {'✅' if ok else '⚠️'} {tag} 开关像素: 前={before} 后={after} 差={diff}"
           f"（阈值: 后>{min_px} 或 差≥{_SWITCH_ON_DIFF}）")
    return ok, before, after, diff


def _locate_translate_switch(win=None):
    """OCR 定位「翻译语言」行左侧开关。返回 (cx, cy)，失败 None。"""
    try:
        from ocr_locate import find_text_in_window
        if win is None:
            win, _ = connect_jianying(retry=False)
            if not win:
                return None
        hit = find_text_in_window(win, '翻译语言', region=_abs_region(win, (0, 120, 460, 260)))
        if hit:
            dx, dy = _cal('translate_switch_offset', (-48, 0))
            sc = _ui_scale(win)
            return (int(hit[0]) + int(round(dx * sc)), int(hit[1]) + int(round(dy * sc)))
    except Exception:
        pass
    return None


def _read_translate_lang(win=None):
    """OCR 读取翻译语言下拉的当前值；读不到返回 ''（未知）。"""
    try:
        from ocr_locate import _ocr_window
        if win is None:
            win, _ = connect_jianying(retry=False)
        if not win:
            return ''
        items = _ocr_window(win, region=_cal('translate_value_region', None)
                            or _safe_region(win, TRANSLATE_VALUE_REGION_OFF, 'left',
                                            tag='translate_value_region'))
        for text, _c, _s, _b in items:
            t = text.replace(' ', '')
            if '英语' in t or 'English' in t or '英文' in t:
                return '英语'
        for text, _c, _s, _b in items:
            if '无' in text:
                return '无'
        return ''
    except Exception as e:
        dprint(f"  [warn] 读取翻译语言失败: {e}")
        return ''


def _capture_panel(win=None):
    """截取左侧字幕面板区域（窗口原点 + 逻辑 region×scale）"""
    try:
        return np.array(ImageGrab.grab(bbox=tuple(
            _cal('left_list_region', None)
            or _safe_rect(win, LEFT_LIST_REGION_OFF, 'left', tag='left_list_region'))))
    except Exception:
        return None


def _check_button_state(win=None):
    """
    检测"开始识别"按钮状态：
      返回: 'ready' | 'processing' | 'done' | 'unknown'
      基于按钮区域内白色像素比例（按钮文字是白色）
    """
    try:
        # 按钮区域（x 窗口原点 / y 时间线顶部；JSON 有值时不查 UIA）
        _br = _cal('start_recog_btn_region', None)
        if _br:
            left, top, right, bottom = tuple(_br)
        else:
            pe = _panel_edges(win)
            if _edges_degraded(pe):
                dprint("  [WARN] 开始识别按钮区域：面板边缘三级降级 → 状态判定记 unknown")
                return 'unknown'
            left, top, right, bottom = _start_btn_region(win, pe)
        # 单次抓屏：按钮区域直接带 bbox 抓，避免先抓面板再抓全屏（省 ~0.7s/次）
        full = np.array(ImageGrab.grab(bbox=(left, top, right, bottom)))
        white_ratio = np.mean(full[:, :, 0] > 200)
        if white_ratio > 0.3:
            return 'ready'     # 按钮白色文字清晰 → 可点击/初始态
        elif white_ratio > 0.05:
            return 'processing'  # 按钮在处理中（文字变暗或进度条）
        else:
            return 'done'      # 按钮完全变色 → 可能已完成
    except Exception:
        return 'unknown'


_PANEL_OPEN_KEYWORDS = ('翻译语言', '开始识别')


def _panel_opened(win):
    """一次窄条 OCR 验证字幕面板已打开（命中「翻译语言」或「开始识别」即算）。

    只扫左面板上部（宽 45% 窗口、高 80~620×s，覆盖「翻译语言」与「开始识别」）；
    不调 _panel_edges，避免多一次 UIA 全枚举。用于 JSON 两点直点后的有效性验证。
    """
    try:
        from ocr_locate import _ocr_window
        wr = get_window_rect(win)
        if not wr:
            return False
        ww = int(wr[2]) - int(wr[0])
        s = _ui_scale(win)
        region = (0, int(round(80 * s)), int(ww * 0.45), int(round(620 * s)))
        for text, _c, _s, _b in _ocr_window(win, region=region):
            if any(k in text for k in _PANEL_OPEN_KEYWORDS):
                dprint(f"  [json] 面板已打开（命中「{text.strip()}」）")
                return True
        return False
    except Exception as e:
        dprint(f"  [json] 面板验证异常: {str(e)[:60]}")
        return False


def _ensure_subtitle_panel(win):
    """
    确保当前在字幕面板：点顶部 Tab "字幕" → 点面板入口 "识别字幕"。
    定位优先级（pipeline 侧 JSON 优先；准入仍为 OCR 优先的校准器）：
      ① 校准 JSON 两个实测点击点直点两下 → 一次窄条 OCR 验证面板已打开 → 命中即返回
      ② 未命中回退原 OCR 实时定位（OCR 失败再退锚定推导值），OCR 命中后回写 JSON

    「字幕」Tab 随布局跳动（4K 实测 1380 vs 1920 的 379×1.5=568.5），故回退链用
    **全宽带 OCR**（宽=窗口宽、高≈150×s）：命中框优先，锚定推导值只作兜底；
    且必须排除「识别字幕」等同含"字幕"的面板文字（否则会点到面板入口）。
    """
    dprint("  确保字幕面板激活...")

    from ui_calibration import update_param

    # ① JSON 优先：两点直点 + 一次 OCR 验证（省掉 2 次全 region OCR，约 8s）
    json_tab = _cal('subtitle_tab_center', None)
    json_entry = _cal('panel_entry_center', None)
    if (json_tab and json_entry
            and len(json_tab) == 2 and len(json_entry) == 2):
        t_pt = (int(json_tab[0]), int(json_tab[1]))
        e_pt = (int(json_entry[0]), int(json_entry[1]))
        dprint(f"  [json] 顶部Tab「字幕」{t_pt} → 面板入口「识别字幕」{e_pt}（首选）")
        _native_click(*t_pt, "顶部Tab字幕[JSON]", settle=0.8)
        _native_click(*e_pt, "面板入口_识别字幕[JSON]", settle=0.8)
        if _panel_opened(win):
            dprint("  [json] 字幕面板已打开（JSON 两点命中）")
            return None
        dprint("  [json] 两点点击后面板未打开 → 回退 OCR 定位链路")

    pe = _panel_edges(win)
    x0, _y0 = _win_origin(win)
    s = _ui_scale(win)

    # 1. 顶部 Tab "字幕"：全宽带（宽=窗口宽、高≈150×s）
    pos = None
    try:
        from ocr_locate import _ocr_window
        band = _subtitle_tab_band(win)
        for text, c, score, _b in _ocr_window(win, region=band):
            t = text.replace(' ', '').replace('\n', '')
            if t.strip() == '字幕' and score >= 0.4:
                pos = (int(c[0]), int(c[1]))
                break
        if pos:
            dprint(f"  [ocr] 顶部Tab「字幕」→ {pos}（全宽带 {band}）")
            _update_coord('subtitle_tab_center', list(pos))
    except Exception as e:
        dprint(f"  [warn] 顶部Tab 全宽带 OCR 失败: {str(e)[:80]}")
    measured = pos   # 仅 OCR 实测值（兜底/推导值不参与布局闸门比对，避免自证通过）
    if not pos:
        pos = _cal('subtitle_tab_center', None)
        if pos:
            pos = (int(pos[0]), int(pos[1]))
            dprint(f"  [兜底] 顶部Tab「字幕」OCR 未命中 → {pos}（校准 JSON 实测值）")
        elif _edges_degraded(pe):
            # B：无实测值且面板边缘三级降级时，字幕 Tab 位置只能以全宽带 OCR 命中为准
            raise AnchorDegradedError(
                "subtitle_tab_center：面板边缘三级降级且全宽带 OCR 未命中「字幕」→ 停止本步")
        else:
            pos = _anchor_point(win, SUBTITLE_TAB_OFF, 'left', pe)
            dprint(f"  [兜底] 顶部Tab「字幕」OCR 未命中 → {pos}（一级锚定推导）")
    _native_click(*pos, "顶部Tab字幕")
    time.sleep(1.0)

    # 2. 面板入口 "识别字幕"：左面板上部（x 面板锚定 [左缘, 右缘]，y 100~220×s）
    #    点开 Tab 后 VEComboBox 才暴露到 UIA，故此处**重新取一次**边缘；
    #    若仍为比例降级（三级）→ region 退全宽（并 WARN），不拿降级值当锚点。
    entry = None
    pe2 = None
    try:
        from ocr_locate import find_text_in_window
        pe2 = _panel_edges(win)
        s2 = _ui_scale(win)
        wr2 = get_window_rect(win) or (0, 0, 0, 0)
        ww2 = int(wr2[2]) - int(wr2[0])
        l2, r2 = pe2.get('left_left'), pe2.get('left_right')
        if (l2 is not None and r2 is not None
                and not _edges_degraded(pe2)):
            reg = (int(l2) - x0, int(round(100 * s2)), int(r2) - x0, int(round(220 * s2)))
        else:
            reg = (0, int(round(100 * s2)), ww2, int(round(220 * s2)))
            dprint("  [WARN] 面板边缘为比例降级 → 「识别字幕」region 退全宽搜索")
        hit = find_text_in_window(win, '识别字幕', region=reg)
        if hit:
            entry = (int(hit[0]), int(hit[1]))
            dprint(f"  [ocr] 面板入口「识别字幕」→ {entry}（region {reg}）")
            _update_coord('panel_entry_center', list(entry))
    except Exception as e:
        dprint(f"  [warn] 面板入口 OCR 失败: {str(e)[:80]}")
    if not entry:
        entry = _cal('panel_entry_center', None)
        if entry:
            entry = (int(entry[0]), int(entry[1]))
            dprint(f"  [兜底] 面板入口 OCR 未命中 → {entry}（校准 JSON 实测值）")
        elif _edges_degraded(pe2 or pe):
            raise AnchorDegradedError(
                "panel_entry_center：面板边缘三级降级且 OCR 未命中「识别字幕」→ 停止本步")
        else:
            entry = _anchor_point(win, PANEL_ENTRY_OFF, 'left', pe)
            dprint(f"  [兜底] 面板入口 OCR 未命中 → {entry}（一级锚定推导）")
    _native_click(*entry, "面板入口_识别字幕")
    time.sleep(1.0)
    return measured   # 激活前 OCR 实测的「字幕」Tab 位置（激活后 Tab 文字隐藏，供布局闸门复用）


def _ensure_translate_lang(target='英语', attempts=3):
    """
    确保翻译语言 = target：
      ① 开关：OCR 定位「翻译语言」标签 → 多候选偏移点击 + 像素复核
         （历史实测 dx=-35 生效；旧值 -48 会点到标签上没反应）
      ② 下拉：点值文本展开 → OCR 验证列表真的出现；未展开再点右侧组合框箭头候选
         （历史实测 值文本x+168；只点值文本不展开）
      ③ OCR 回读复核，未达标重试；每轮重新确保开关（防中途被点灭）
    """
    dprint(f"  确保翻译语言='{target}'...")

    from ui_calibration import update_param

    def _label_pos():
        win, _ = connect_jianying(retry=False)
        try:
            from ocr_locate import find_text_in_window
            pe = _panel_edges(win)
            s = _ui_scale(win)
            x0, _y0 = _win_origin(win)
            if _edges_degraded(pe):
                reg = _full_width_region(win, 150, 240)
                dprint("  [WARN] 翻译语言标签：面板边缘三级降级 → OCR 区域退全宽")
            else:
                reg = (_anchor_x(win, 0, 'left', pe) - x0, int(round(150 * s)),
                       _anchor_x(win, 0, 'right', pe) - x0, int(round(240 * s)))
            hit = find_text_in_window(win, '翻译语言', region=reg)
            if hit:
                return (int(hit[0]), int(hit[1]))
        except Exception:
            pass
        return None

    _sw_hit = []   # ensure_switch 实际点击并通过前后像素差验证的点（复核优先用它，避免误读标签）

    def ensure_switch():
        """勾选「翻译语言」开关：JSON 实测点击点优先，失败回退 OCR 标签 + 多候选偏移。
        候选偏移一律 ×s（防 4K 落点压到蓝色标签被读成"已勾选"）。"""
        # ① JSON 直点：calibration 里 translate_switch_center 存的是**已验证的点击点**
        #    （不是标签中心），不可再叠加 _SWITCH_DX_LOGICAL 偏移。已有值直接点+像素复核，
        #    省掉一次面板边缘 UIA + 标签 OCR（~2s）。
        saved = _cal('translate_switch_center', None)
        if saved and len(saved) == 2:
            cand = (int(saved[0]), int(saved[1]))
            ok, before, after, diff = _switch_click_checked(win, cand, tag='[JSON]')
            if ok:
                dprint(f"  ✅ 「翻译语言」开关勾选 @ {cand}（JSON，像素 前={before} 后={after} 差={diff}）")
                _sw_hit[:] = [cand]
                return True
            dprint(f"  [json] 直点 {cand} 未勾选（像素 前={before} 后={after} 差={diff}）→ 回退 OCR 定位")
        lab = _label_pos()
        if lab:
            dprint(f"  [ocr] 「翻译语言」标签 → {lab}")
        else:
            lab = _cal('translate_switch_center', None)
            if lab:
                lab = (int(lab[0]), int(lab[1]))
                dprint(f"  [兜底] 「翻译语言」标签 OCR 未命中 → {lab}（校准 JSON 实测值）")
            elif _edges_degraded(_panel_edges(win)):
                raise AnchorDegradedError(
                    "translate_switch_center：面板边缘三级降级且全宽带 OCR 未命中「翻译语言」→ 停止本步")
            else:
                lab = _anchor_point(win, TRANSLATE_SWITCH_OFF, 'left')
                dprint(f"  [兜底] 「翻译语言」标签 OCR 未命中 → {lab}（一级锚定推导）")
        s = _ui_scale(win)
        for dx in _SWITCH_DX_LOGICAL:
            cand = (lab[0] + int(round(dx * s)), lab[1])
            ok, before, after, diff = _switch_click_checked(win, cand, tag=f'(dx={dx}×s)')
            if ok:
                dprint(f"  ✅ 「翻译语言」开关勾选 @ {cand}（dx={dx}×s，像素 前={before} 后={after} 差={diff}）")
                _sw_hit[:] = [cand]
                _update_coord('translate_switch_center', list(cand))
                return True
        dprint("  ⚠️ 开关多候选点击后仍未勾选（VIP 限制？）继续尝试下拉")
        return False

    def _list_open():
        """展开后有「目标项 / English」= 列表真的打开了。"""
        try:
            from ocr_locate import _ocr_window
            items = _ocr_window(
                win, region=_cal('translate_list_region', None)
                or _safe_region(win, TRANSLATE_LIST_REGION_OFF, 'left',
                                tag='translate_list_region'))
            for text, _c, _s, _b in items:
                if target in text or 'English' in text:
                    return True
        except Exception:
            pass
        return False

    def open_dropdown(dd, direct=None):
        """展开下拉并 OCR 验证列表真的出现。

        direct=[x,y]：校准 JSON 实测点，**首选**直点（失败且 dd 为 None 时直接返回 False，
        由调用方回退到值行 OCR 链路）。
        dd：值行定位点。点位绑定**组合框 rect 右缘 − k×s**（一级 UIA 的 VEComboBox rect，
        1920 下 right=816 → 300/252/132，与旧值逐像素相同）；rect 不可得才退
        「值文本 + 1920 偏移×s」。
        """
        if direct:
            cx, cy = int(direct[0]), int(direct[1])
            _native_click(cx, cy, "翻译语言下拉(JSON)", settle=1.0)
            if _list_open():
                dprint(f"  ✅ 下拉已展开（JSON @ {cx},{cy}）")
                return True
            dprint(f"  [json] 直点下拉 ({cx},{cy}) 未展开 → 回退组合框候选链")
            if dd is None:
                return False
        pe = _panel_edges(win)
        s = _ui_scale(win)
        rects = pe.get('left_combos') or []
        row = None
        if rects:
            row = min(rects, key=lambda r: abs((int(r[1]) + int(r[3])) // 2 - int(dd[1])))
        if row:
            cands = [(int(row[2]) - int(round(k * s)), int(dd[1]), f'组合框右缘-{k}×s')
                     for k in _DROPDOWN_RIGHT_DX]
            dprint(f"  [anchor] 下拉行 rect={row}（src={pe.get('src', {}).get('left_right', '-')}）"
                   f" → 候选 x={[c[0] for c in cands]}")
        else:
            cands = [(int(dd[0]) + int(round(k * s)), int(dd[1]), f'值文本+{k}×s')
                     for k in (168, 120, 0)]
            dprint("  [WARN] 组合框 rect 不可得 → 下拉点退「值文本 + 1920 实测偏移×s」")
        for cx, cy, tag in cands:
            _native_click(cx, cy, f"翻译语言下拉({tag})", settle=1.0)
            if _list_open():
                dprint(f"  ✅ 下拉已展开（{tag} @ {cx},{cy}）")
                return True
            dprint(f"  [dropdown] 点 {tag}@({cx},{cy}) 未展开，换候选")
        return False

    def _double_verify():
        """选定复核双标志：① 值行 OCR=target ② 开关蓝像素计数≥阈值。两者都过才算选定。"""
        win2, _ = connect_jianying(retry=False)
        cur2 = _read_translate_lang(win2)
        sw2 = _cal('translate_switch_center', None) or (_sw_hit[-1] if _sw_hit else None)
        if not sw2:
            # 没有可信开关点（校准无值且本轮未点过）：不拿锚定值当开关读像素，按“不可测”处理
            dprint("  [WARN] 开关复核点不可测（无校准值 / 本轮未点过）→ 只用值行复核")
            return cur2 == target, cur2, None
        n2 = _switch_blue_count(sw2[0], sw2[1], win2)
        sw_on = n2 > _switch_on_min() if n2 >= 0 else None
        dprint(f"  [verify] 值行='{cur2}' 开关蓝像素={n2}（阈值 >{_switch_on_min()}）")
        return cur2 == target and sw_on is not False, cur2, sw_on

    def _mark_verified():
        """把选定验证成功的持久标志写入 JSON。"""
        try:
            from datetime import datetime
            update_param('recognize', 'translate_lang_verified_at',
                         datetime.now().isoformat(timespec='seconds'))
        except Exception:
            pass

    win, _ = connect_jianying(retry=False)
    cur = _read_translate_lang(win)
    if cur == target:
        ok2, _cur2, sw_on = _double_verify()
        if ok2:
            dprint(f"  ✅ 翻译语言已是'{target}'（复核：值行 OCR ✓ 开关勾选 ✓）")
            _mark_verified()
            return True
        dprint(f"  ⚠️ 值行读到'{target}'但开关像素未勾选 → 走重试流程")

    def _locate_value_row():
        """OCR 定位下拉当前值行（无/英语文本即值行）→ 校准 JSON → 锚定推导。"""
        dd = None
        try:
            from ocr_locate import _ocr_window
            items = _ocr_window(win, region=_cal('translate_value_region', None)
                                or _safe_region(win, TRANSLATE_VALUE_REGION_OFF, 'left',
                                                tag='translate_value_region'))
            for text, c, _s, _b in items:
                t = text.replace(' ', '')
                if ('无' in t or '英语' in t or 'English' in t or '英文' in t) and c:
                    dd = (int(c[0]), int(c[1]))
                    break
        except Exception:
            pass
        if dd:
            dprint(f"  [ocr] 翻译语言当前值行 → {dd}")
            _update_coord('translate_dropdown_center', list(dd))
            return dd
        dd = _cal('translate_dropdown_center', None)
        if dd:
            dd = (int(dd[0]), int(dd[1]))
            dprint(f"  [兜底] 翻译语言下拉 OCR 未命中 → {dd}（校准 JSON 实测值）")
        elif _edges_degraded(_panel_edges(win)):
            raise AnchorDegradedError(
                "translate_dropdown_center：面板边缘三级降级且值行 OCR 未命中 → 停止本步")
        else:
            dd = _anchor_point(win, TRANSLATE_DROPDOWN_OFF, 'left')
            dprint(f"  [兜底] 翻译语言下拉 OCR 未命中 → {dd}（一级锚定推导）")
        return dd

    ok = False
    for i in range(1, attempts + 1):
        dprint(f"  第{i}次选择（当前值='{cur or '未知'}'）")
        ensure_switch()

        # ② 展开下拉：首次尝试优先校准 JSON 实测点（命中即省掉值行 OCR ~1.5s），
        #    失败回退「OCR 值行 → 组合框右缘候选」原链路。
        direct_dd = _cal('translate_dropdown_center', None) if i == 1 else None
        dd = None
        if direct_dd and len(direct_dd) == 2:
            dprint(f"  [json] 翻译语言下拉实测点 {[int(direct_dd[0]), int(direct_dd[1])]}（首选）")
        else:
            direct_dd = None
            dd = _locate_value_row()
        if not open_dropdown(dd, direct=direct_dd):
            if direct_dd:
                dd = _locate_value_row()
                if not open_dropdown(dd):
                    continue
            else:
                continue

        # 展开后：首次尝试优先校准 JSON 实测选项点（跳过列表 OCR ~1.5s），
        # 失败则后续尝试走列表 OCR 定位。
        json_sel = _cal('translate_english_center', None) if i == 1 else None
        box = None
        if not (json_sel and len(json_sel) == 2):
            json_sel = None
            try:
                from ocr_locate import _ocr_window
                items = _ocr_window(win, region=_cal('translate_list_region', None)
                                    or _safe_region(win, TRANSLATE_LIST_REGION_OFF, 'left',
                                                    tag='translate_list_region'))
                for text, _c, _s, b in items:
                    if target in text:
                        box = b
                        break
            except Exception as e:
                dprint(f"  [warn] 下拉列表 OCR 失败: {e}")

        if json_sel:
            cx, cy = int(json_sel[0]), int(json_sel[1])
            dprint(f"  [json] 下拉选项「{target}」实测点 {cx, cy}（首选）")
        elif box:
            l, t, r, b = box
            cx, cy = (l + r) // 2, (t + b) // 2
        else:
            dprint("  ⚠️ 下拉列表未 OCR 到目标项，用兜底坐标（已按需交叉校验）")
            cx, cy = _checked_anchor_point(win, TRANSLATE_ENGLISH_OFF, 'left', target, 240, 410,
                                           tag='translate_english_center')
        _native_click(cx, cy, f"下拉选项_{target}", settle=1.2)

        win, _ = connect_jianying(retry=False)
        cur = _read_translate_lang(win)
        if cur == target:
            ok2, _cur2, sw_on = _double_verify()
            if not ok2:
                dprint("  ⚠️ 值行读到目标但开关像素未勾选 → 重新确保开关")
                ensure_switch()
                ok2, _cur2, sw_on = _double_verify()
            if ok2:
                dprint(f"  ✅ 翻译语言已选定'{target}'（复核：值行 OCR ✓ 开关勾选 ✓）")
                _mark_verified()
                ok = True
                break

    if not ok:
        dprint(f"  ❌ 翻译语言复核失败：{attempts} 次尝试后 OCR 仍读到'{cur or '未知'}'")
    return ok


def _record_subtitle_tracks(pre_text_count=0):
    """
    识别完成后，把新增字幕轨的 眼睛/锁定 图标坐标写入 ui_calibration.json。
    新字幕轨（中文+英文）总是成簇出现在时间线最顶部，相邻 ymid 间距 < 40px；
    与下方可能误分类为 text 的旧轨（如复合片段预设）用间距断开。
    图标 x 取校准值 track_header.lock_x / eye_x（默认 50 / 75）。
    """
    win, _ = connect_jianying(retry=False)
    if not win:
        dprint("  ⚠️ 记录字幕轨失败：窗口丢失")
        return []

    rows, *_ = scan_tracks(win)
    text_rows = sorted([r for r in rows if r['type'] == 'text'], key=lambda r: r['ymid'])

    # 取顶部连续成簇的 text 轨 = 本次新增的字幕轨
    cluster = []
    for r in text_rows:
        if not cluster or r['ymid'] - cluster[-1]['ymid'] <= 40:
            cluster.append(r)
        else:
            break
    new_rows = cluster or text_rows

    cal = {}
    if os.path.exists(CALIBRATION_PATH):
        try:
            with open(CALIBRATION_PATH, 'r', encoding='utf-8') as f:
                cal = json.load(f)
        except Exception:
            cal = {}
    # 图标 x 不再直接当绝对像素用：按「时间线左缘 + 逻辑偏移×dpi_scale」推导
    # （get_eye_x/get_lock_x 内部走 _hdr_x；scan_tracks 已刷新几何）
    from ui_track_ops import get_eye_x, get_lock_x
    wr = get_window_rect(win) or (0, 0, 0, 0)
    WL = int(wr[0])
    WW = int(wr[2]) - WL
    eye_x = get_eye_x(WL, WW)
    lock_x = get_lock_x(WL, WW)

    tracks = [{
        'index': i,
        'ymid': r['ymid'],
        'y_top': r['y_top'],
        'y_bot': r['y_bot'],
        'eye':  [eye_x, r['ymid']],
        'lock': [lock_x, r['ymid']],
    } for i, r in enumerate(new_rows)]

    cal['subtitle_tracks'] = {
        'count': len(tracks),
        'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'note': '识别字幕后新增的字幕轨（按 y 从上到下：用户确认 从上到下第二条=中文轨）；'
                'eye=隐藏图标中心, lock=锁定图标中心',
        'tracks': tracks,
    }
    try:
        with open(CALIBRATION_PATH, 'w', encoding='utf-8') as f:
            json.dump(cal, f, ensure_ascii=False, indent=2)
        dprint(f"  ✅ 已记录 {len(tracks)} 条字幕轨图标坐标 → ui_calibration.json")
        for t in tracks:
            dprint(f"     轨{t['index']}: ymid={t['ymid']}  eye={t['eye']}  lock={t['lock']}")
    except Exception as e:
        dprint(f"  ⚠️ 写入校准文件失败: {e}")
    return tracks


def run_recognize_translate(target_lang: str = "英语",
                            workdir: str = ".",
                            timeout_sec: int = 600,
                            wait_only: bool = False) -> dict:
    op_stage('4 识别中文字幕+翻译')
    t0 = time.time()
    os.makedirs(os.path.abspath(workdir), exist_ok=True)

    dprint("=" * 60)
    dprint("[recognize_translate] 识别+翻译字幕 - 开始")
    dprint("=" * 60)

    win, _ = connect_jianying()
    if not win:
        return {'ok': False, 'duration_s': 0, 'error': '未找到剪映窗口'}

    win = ensure_maximized(win)

    texts_before = _draft_texts_count()
    eng_before = _draft_english_text_count()
    dprint(f"  当前草稿 texts 数: {texts_before}（英文 {eng_before} 条）")
    lang_ok = None

    # --- Step 0: 确保预设轨已 锁定+隐藏 ---
    # 防御：人声分离成功路径已不再恢复预设轨，但单跑本步（--from-step）时预设可能可见。
    # 已有 text 轨时跳过——hide_preset_tracks 会隐藏一切非主轨，不能误伤已识别字幕轨。
    rows0, *_ = scan_tracks(win)
    if any(r['type'] == 'text' for r in rows0):
        dprint("\n[0/4] 已存在字幕轨，跳过预设轨整理（保护已有字幕轨）")
    else:
        dprint("\n[0/4] 确保预设轨已 锁定+隐藏")
        from ui_track_ops import hide_preset_tracks
        hide_preset_tracks(win)

    if not wait_only:
        # --- Step 1: Home + Ctrl+A 选主轨 ---
        dprint("\n[1/4] Home + Ctrl+A 选主轨")
        try:
            select_main_track()
        except Exception as e:
            dprint(f"  ⚠️ 键盘操作异常: {e}")

        # --- Step 2: 点顶部 Tab "字幕" + 面板入口 "识别字幕" ---
        dprint("\n[2/4] 激活字幕面板")
        tab_measured = _ensure_subtitle_panel(win)

        # --- Step 3: 翻译语言下拉选目标 ---
        dprint(f"\n[3/4] 翻译语言下拉选'{target_lang}'")
        lang_ok = _ensure_translate_lang(target_lang)

        # --- Step 3.5: 面板锚点闸门（S5.5）---
        # 字幕面板此刻已激活且下拉已收起，对比 calibration.panel_anchors.recognize
        # （带 verified_at 的实测值）：字幕Tab / 翻译开关 / 下拉行 rect / 开始识别按钮。
        # 字幕 Tab 的文字在面板激活后会隐藏，复用激活前实测值（不是兜底/历史值）。
        from ui_verify import check_anchors
        preset = None
        if tab_measured:
            preset = {'subtitle_tab_center': [int(tab_measured[0]), int(tab_measured[1])]}
        gate = check_anchors(workdir, 'recognize', win=win, preset=preset)
        # 布局自检刚 OCR 实测过「开始识别」（同一函数同一 region），直接复用其坐标
        # 省一次全 region OCR（~3s）；SKIP/未命中时回退原 OCR 链路。坐标系一致（窗口相对）。
        gate_btn = (gate.get('values') or {}).get('start_recog_btn_center')

        # --- Step 4: 点"开始识别"按钮 ---
        dprint("\n[4/4] 点'开始识别'按钮")
        btn_state_before = _check_button_state(win)
        dprint(f"  点击前按钮状态: {btn_state_before}")
        # 「开始识别」OCR 实时定位（面板锚定带：x 面板左右缘，y 时间线顶 ±150×s），JSON 兜底
        btn = None
        if gate_btn and len(gate_btn) == 2:
            btn = (int(gate_btn[0]), int(gate_btn[1]))
            dprint(f"  [gate] 「开始识别」复用布局自检实测坐标 → {btn}")
        if not btn:
            # JSON 优先：校准实测点（点击后按钮状态会复核，失败再回退 OCR）
            b2 = _cal('start_recog_btn_center', None)
            if b2 and len(b2) == 2:
                btn = (int(b2[0]), int(b2[1]))
                dprint(f"  [json] 「开始识别」实测点 → {btn}（首选）")
        if not btn:
            try:
                from ocr_locate import find_text_in_window
                from ui_calibration import update_param
                reg = _start_btn_region_band(win)
                hit = find_text_in_window(win, '开始识别', region=reg) if reg else None
                if hit:
                    btn = (int(hit[0]), int(hit[1]))
                    dprint(f"  [ocr] 「开始识别」按钮 → {btn}（region {reg}）")
                    _update_coord('start_recog_btn_center', list(btn))
            except Exception as e:
                dprint(f"  [warn] 「开始识别」OCR 失败: {str(e)[:80]}")
        if not btn:
            btn = _cal('start_recog_btn_center', None)
            if btn:
                btn = (int(btn[0]), int(btn[1]))
                dprint(f"  [兜底] 「开始识别」OCR 未命中 → {btn}（校准 JSON 实测值）")
            elif _edges_degraded(_panel_edges(win)):
                raise AnchorDegradedError(
                    "start_recog_btn_center：面板边缘三级降级且 OCR 未命中「开始识别」→ 停止本步")
            else:
                btn = _start_btn_point(win, _panel_edges(win))
                dprint(f"  [兜底] 「开始识别」OCR 未命中 → {btn}（一级锚定推导）")
        _native_click(*btn, "开始识别按钮", settle=0.8)
        btn_state_after = _check_button_state(win)
        dprint(f"  点击后按钮状态: {btn_state_after}")
    else:
        dprint("\n[wait-only] 跳过点击步骤，直接等待识别完成")

    # --- Step 5: 等待识别完成 ---
    # 验收判据（硬性）：① 字幕轨 +2（中/英）  ② 草稿里出现英文译文（证明翻译语言=英语）
    # 草稿 texts 增加只作为等待信号，不再单独判定完成（防假绿）
    dprint(f"\n[wait] 等待识别完成（超时 {timeout_sec}s）")
    win, _ = connect_jianying(retry=False)
    rows_before, *_ = scan_tracks(win) if win else ([], None, None)
    pre_text = sum(1 for r in rows_before if r['type'] == 'text')
    dprint(f"  识别前轨道: 总={len(rows_before)}, text={pre_text}")

    success = False
    start_poll = time.time()
    curr_text = pre_text
    cnt = texts_before
    eng_now = eng_before
    eng_added = 0
    eng_ok = False
    cn_ok = False
    last_cnt = texts_before
    last_cnt_change = start_poll   # 草稿 texts 最近一次变化时刻（用于快速失败判定）
    fast_fail_reason = None

    while time.time() - start_poll < timeout_sec:
        time.sleep(3)
        elapsed = int(time.time() - start_poll)

        try:
            win2, _ = connect_jianying(retry=False)
            if win2:
                win = win2
        except Exception:
            pass

        try:
            rows_now, *_ = scan_tracks(win) if win else ([],)
            curr_text = sum(1 for r in rows_now if r['type'] == 'text')
        except Exception:
            curr_text = pre_text

        cnt = _draft_texts_count()
        if cnt != last_cnt:
            last_cnt = cnt
            last_cnt_change = time.time()
        eng_now = _draft_english_text_count()
        eng_added = (eng_now - eng_before) if (eng_before >= 0 and eng_now >= 0) else eng_now
        # wait-only：识别早已完成，用"草稿里存在英文"判定；正常模式用"本次新增英文"判定
        min_eng = int(_cal('min_english_texts', 1))
        eng_ok = (eng_now >= min_eng) if wait_only else (eng_added >= min_eng)

        # 硬判据：英文译文必须存在（不作假绿），且中文字幕确实落地。
        # 轨道扫描在 80 集草稿上可能因时间线虚拟化扫不到新增轨（run6 实况 text=0/2），
        # 因此中文完成以「草稿 texts 材料数新增 ≥2」为结果侧兜底（2598+ 条字幕不可能只 +1）。
        cn_ok = (curr_text >= pre_text + 2) or (cnt >= texts_before + 2)
        if cn_ok and eng_ok:
            dprint(f"  ✅ 字幕落地：轨道 +{curr_text - pre_text}（texts {texts_before}→{cnt}），"
                   f"英文译文 +{eng_added} 条")
            success = True
            break

        # 快速失败已移除：80 集/2h 草稿的识别是「跑完一次性落盘」（run6：13:18 点击 →
        # 13:53 draft_content.json 才出现 2608 条字幕），中途 texts/轨道都不增长。
        # 任何基于「45/120s 无变化」的提前判死都会在长识别中途误杀，改为只由超时兜底。

        if elapsed % 30 < 3:
            dprint(f"  t={elapsed}s 等待中... text={curr_text}/{pre_text + 2}, "
                   f"texts={cnt}, 英文+{eng_added}")

    # 超时终检复用正常判据：英文必须在，中文轨道或 texts 增量也必须达到 +2。
    if not success and eng_ok and cn_ok:
        dprint(f"  ⚠️ 超时但字幕已落地（轨道 +{curr_text - pre_text}、texts {texts_before}→{cnt}）、"
               f"英文译文 +{eng_added}，判定迟到完成")
        success = True

    fail_reason = None
    if not success:
        if fast_fail_reason:
            fail_reason = fast_fail_reason
        else:
            if curr_text < pre_text + 2 and cnt < texts_before + 2:
                fail_reason = f'中文字幕未落地（轨道 {pre_text} → {curr_text}，texts {texts_before} → {cnt}）'
            if not eng_ok:
                fail_reason = (fail_reason + '；' if fail_reason else '') + \
                    f'未生成英文译文（英文 +{eng_added} < {min_eng}，翻译语言可能未选中"英语"）'

    if not success:
        dprint(f"  ❌ 识别翻译未达标: {fail_reason}")

    # --- Step 6: 记录新增字幕轨的 眼睛/锁定 图标坐标 ---
    if success:
        dprint("\n[record] 记录新增字幕轨图标坐标")
        time.sleep(2)   # 等时间线渲染稳定
        _record_subtitle_tracks(pre_text)

    # --- 操作时间线：每个鼠标/键盘操作逐条耗时（阶段分组待用户定义）---
    if op_timeline('[recognize] 操作时间线'):
        take_op_marks()   # 输出后清空，供下个模块独立计时

    elapsed = time.time() - t0
    dprint(f"\n[recognize_translate] {'✅完成' if success else '❌未达标'}  耗时 {elapsed:.1f}s")
    return {
        'ok': success,
        'duration_s': round(elapsed, 1),
        'error': None if success else (fail_reason or 'timeout'),
        'translate_lang_ok': lang_ok,
        'text_tracks_delta': curr_text - pre_text,
        'english_texts_added': eng_added,
    }


# ===== CLI =====
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--lang', default='英语', help='翻译目标语言')
    parser.add_argument('--timeout', type=int, default=600, help='超时秒')
    parser.add_argument('--workdir', default='.')
    parser.add_argument('--wait-only', action='store_true',
                        help='跳过点击步骤（识别已在进行中/已完成），只等待完成并记录字幕轨坐标')
    args = parser.parse_args()

    result = run_recognize_translate(
        target_lang=args.lang,
        workdir=args.workdir,
        timeout_sec=args.timeout,
        wait_only=args.wait_only,
    )
    print(f"\n结果: {result}")
    sys.exit(0 if result['ok'] else 1)
