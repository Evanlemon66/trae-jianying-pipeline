# -*- coding: utf-8 -*-
"""
OCR 文字定位层（RapidOCR / PP-OCRv4 中文模型）
==============================================
职责：窗口截图 → OCR → 按关键词返回文字中心坐标（绝对屏幕坐标）。
只负责"找到初始坐标"，点击成败由各模块的验证闭环判定。

依赖装在 D 盘 pipeline_ui/pylibs（pip --target），模型随包自带。
"""
from __future__ import annotations

import os
import sys

_PYLIBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pylibs')
if _PYLIBS not in sys.path:
    sys.path.insert(0, _PYLIBS)

import numpy as np

from ui_core import dprint, capture_region, get_window_rect

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def ocr_image(img: np.ndarray):
    """
    对窗口截图（numpy RGB）做 OCR。
    返回 [(text, (cx, cy), score), ...]，坐标为相对 img 左上角的像素。
    """
    eng = _get_engine()
    result, _ = eng(img)
    out = []
    if not result:
        return out
    for box, text, score in result:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append((text, (int(sum(xs) / 4), int(sum(ys) / 4)),
                    float(score), (min(xs), min(ys), max(xs), max(ys))))
    return out


def _ocr_window(win, region=None):
    """
    截窗口（或指定区域）→ OCR。
    region: (x0, y0, x1, y1) 窗口相对像素；只扫该区域可大幅提速
            （全窗口 ≈6.5s，右半 ≈2s，Q2 小条 ≈0.5s）。
    返回 [(text, (绝对cx, 绝对cy), score, 绝对box), ...]
    """
    wr = get_window_rect(win)
    if not wr:
        return []
    WL, WT, WW, WH = wr
    img = np.array(capture_region(WL, WT, WW, WH))
    ox, oy = 0, 0
    if region:
        x0, y0, x1, y1 = region
        x0 = max(0, min(int(x0), WW - 1))
        y0 = max(0, min(int(y0), WH - 1))
        x1 = min(max(x0 + 1, int(x1)), WW)
        y1 = min(max(y0 + 1, int(y1)), WH)
        img = img[y0:y1, x0:x1]
        ox, oy = x0, y0
    out = []
    for text, (cx, cy), score, box in ocr_image(img):
        l, t, r, b = box
        out.append((text, (WL + ox + cx, WT + oy + cy), float(score),
                    (WL + ox + int(l), WT + oy + int(t),
                     WL + ox + int(r), WT + oy + int(b))))
    return out


def find_text_in_window(win, keyword, y_range=None, x_range=None,
                        min_score=0.4, debug=False, region=None):
    """
    在剪映窗口截图中找包含 keyword 的文字，返回其中心绝对坐标 (x, y)。
    y_range/x_range: 相对窗口的 (min, max) 像素范围，用于排除同名干扰（可选）。
    region: 只 OCR 该窗口相对区域 (x0,y0,x1,y1)，大幅提速（可选）。
    找不到返回 None。
    """
    wr = get_window_rect(win)
    if not wr:
        return None
    WL, WT, WW, WH = wr
    items = _ocr_window(win, region=region)
    if debug:
        dprint(f"  [ocr] 识别 {len(items)} 条文字")
    for text, (ax, ay), score, box in items:
        if score < min_score:
            continue
        if keyword not in text:
            continue
        cx, cy = ax - WL, ay - WT
        if y_range and not (y_range[0] <= cy <= y_range[1]):
            continue
        if x_range and not (x_range[0] <= cx <= x_range[1]):
            continue
        dprint(f"  [ocr] 命中 {text!r} @窗口相对({cx},{cy}) score={score:.2f}")
        return (ax, ay)
    return None


def find_text_box(win, keyword, y_range=None, x_range=None, min_score=0.4,
                  region=None):
    """
    找包含 keyword 的文字，返回其外接框绝对坐标 (left, top, right, bottom)。
    找不到返回 None。
    """
    wr = get_window_rect(win)
    if not wr:
        return None
    WL, WT, WW, WH = wr
    for text, (ax, ay), score, (l, t, r, b) in _ocr_window(win, region=region):
        if score < min_score or keyword not in text:
            continue
        cx, cy = ax - WL, ay - WT
        if y_range and not (y_range[0] <= cy <= y_range[1]):
            continue
        if x_range and not (x_range[0] <= cx <= x_range[1]):
            continue
        return (l, t, r, b)
    return None


def find_all_in_window(win, keyword, min_score=0.4, region=None):
    """返回所有命中项 [(绝对cx, 绝对cy, text, score), ...]"""
    out = []
    for text, (ax, ay), score, _box in _ocr_window(win, region=region):
        if score >= min_score and keyword in text:
            out.append((ax, ay, text, score))
    return out


def find_all_texts(win, min_score=0.4, region=None):
    """一次 OCR 返回该区域全部文字 [(绝对cx, 绝对cy, text, score), ...]。
    调用方在本地按多个关键词过滤——避免同一画面重复 OCR（每次 ≈2s）。"""
    return [(ax, ay, text, float(score))
            for text, (ax, ay), score, _box in _ocr_window(win, region=region)
            if score >= min_score]
