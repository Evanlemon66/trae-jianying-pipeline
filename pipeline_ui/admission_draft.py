# -*- coding: utf-8 -*-
"""准入草稿的纯逻辑（不碰 UI、不 import pyautogui）。

admission_test.py 负责编排（连接/建稿/打开），本模块只做可单测的判定：
  - presets.json `admission` 段归一化与校验（draft_name 唯一真源，缺失即失败）
  - 素材剧集目录解析（watch.roots[root_index] → sorted 第 series_index 个，无匹配视频顺延）
  - 文件层判据（草稿目录是否有效）
  - 结构 baseline 计算 / 判脏（video_segments 不参与判脏）
  - 9 档决策表 → 动作

判据口径：文件层或 UI 层任一为真即不算缺失；baseline 不匹配视为不可用（默认重建）。
"""
from __future__ import annotations

import glob
import json
import os
import time

# 打开等待默认值（只用于 admission 路径；ui_nav.open_draft 自身默认仍是 30/90/2）
DEFAULT_OPEN = {
    'hot_wait_after_build': 0.0,   # 刚建完列表必陈旧，直接走冷启动
    'cold_wait': 150.0,            # 冷启动后每轮找卡片秒数（比现状 90 宽）
    'max_launch': 2,               # 冷启动重拉次数（与现状一致）
    'edit_timeout': 90,            # 等进入编辑页秒数
    'ready_timeout': 120.0,        # 冷启动就绪墙钟（透传 ensure_for_export）
    'allow_cold_restart': True,    # false 时禁止 taskkill/重开剪映
    'close_on_exit': True,         # 收尾正常关闭本轮拉起的剪映，并清理草稿临时文件
}

DEFAULT_VIDEO = {
    'root_index': 0,
    'series_index': 0,
    'root': '',
    'series': '',
    'glob': '第*集.mp4',
    'limit': 5,
    'range': None,
}

_ILLEGAL_NAME_CHARS = set('\\/:*?"<>|')


def _as_int(value, default, minimum=None):
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None and out < minimum:
        return default
    return out


def _as_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ('1', 'true', 'yes', 'on'):
            return True
        if low in ('0', 'false', 'no', 'off'):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def normalize_config(raw):
    """校验并补齐 admission 配置。draft_name 缺失/非法直接 ValueError（不回落硬编码名）。"""
    if not isinstance(raw, dict):
        raise ValueError('缺少 presets.json 的 admission 段')
    name = str(raw.get('draft_name') or '').strip()
    if not name:
        raise ValueError('缺少 admission.draft_name（准入草稿名，唯一真源）')
    if name in ('.', '..') or any(ch in _ILLEGAL_NAME_CHARS for ch in name):
        raise ValueError(f'admission.draft_name 不是合法草稿目录名: {name!r}')

    video_raw = raw.get('video') if isinstance(raw.get('video'), dict) else {}
    limit_raw = video_raw.get('limit', DEFAULT_VIDEO['limit'])
    limit = None if limit_raw in (None, '') else _as_int(limit_raw, DEFAULT_VIDEO['limit'], minimum=0)

    video_range = video_raw.get('range', DEFAULT_VIDEO['range'])
    video_range = str(video_range).strip() if video_range not in (None, '') else None

    cut_raw = raw.get('cut_s')
    cut_s = None
    if cut_raw not in (None, ''):
        cut = _as_float(cut_raw, 0.0)
        cut_s = cut if cut > 0 else None

    profile = raw.get('preset_profile')
    profile = str(profile).strip() if profile not in (None, '') else None

    open_raw = raw.get('open') if isinstance(raw.get('open'), dict) else {}
    return {
        'draft_name': name,
        'video': {
            'root_index': _as_int(video_raw.get('root_index'), DEFAULT_VIDEO['root_index'], minimum=0),
            'series_index': _as_int(video_raw.get('series_index'), DEFAULT_VIDEO['series_index'], minimum=0),
            'root': str(video_raw.get('root') or '').strip(),
            'series': str(video_raw.get('series') or '').strip(),
            'glob': str(video_raw.get('glob') or DEFAULT_VIDEO['glob']).strip() or DEFAULT_VIDEO['glob'],
            'limit': limit,
            'range': video_range,
        },
        'preset_profile': profile,
        'normalize': _as_bool(raw.get('normalize'), False),
        'cut_s': cut_s,
        'open': {
            'hot_wait_after_build': _as_float(
                open_raw.get('hot_wait_after_build'), DEFAULT_OPEN['hot_wait_after_build']),
            'cold_wait': _as_float(open_raw.get('cold_wait'), DEFAULT_OPEN['cold_wait']),
            'max_launch': _as_int(open_raw.get('max_launch'), DEFAULT_OPEN['max_launch'], minimum=1),
            'edit_timeout': _as_int(open_raw.get('edit_timeout'), DEFAULT_OPEN['edit_timeout'], minimum=1),
            'ready_timeout': _as_float(
                open_raw.get('ready_timeout'), DEFAULT_OPEN['ready_timeout']),
            'allow_cold_restart': _as_bool(
                open_raw.get('allow_cold_restart'), DEFAULT_OPEN['allow_cold_restart']),
            'close_on_exit': _as_bool(
                open_raw.get('close_on_exit'), DEFAULT_OPEN['close_on_exit']),
        },
    }


def expected_track_count(presets):
    """预期轨道数 = 主轨 1 + 该预设组实际预设条数。

    不允许"默认轨道数"兜底：预设组为空/未配置时直接报错，避免 initial_layout
    的记录行数与 tracks 校验口径再次打架。
    """
    if not isinstance(presets, (list, tuple)) or not presets:
        raise ValueError('预设组为空或未配置：请检查 presets.json 的 '
                         'admission.preset_profile / default_preset_profile 与 presets 列表')
    return 1 + len(presets)


def decide_window_action(completed_all, stop_after, close_on_exit,
                         keep_window=False, close_window=False):
    """准入收尾的窗口决策 → ('keep'|'close', 原因)。

    优先级：显式开关 > 中途停点默认留窗 > 整轮跑完按 close_on_exit。
      --keep-window   任何情况下强制保留（不关闭、不清临时文件）
      --close-window  任何情况下强制关闭
      未跑完全部阶段（--stop-after 中途停 / 失败）默认保留：这种停点就是给人肉眼确认的
      整轮跑完才看 admission.open.close_on_exit
    """
    if keep_window and close_window:
        raise ValueError('--keep-window 与 --close-window 不能同时使用')
    if keep_window:
        return 'keep', '命令行 --keep-window 强制保留'
    if close_window:
        return 'close', '命令行 --close-window 强制关闭'
    if not completed_all:
        return 'keep', f'中途停点保留窗口，stop_after={stop_after}'
    if close_on_exit:
        return 'close', '整轮跑完，close_on_exit=true'
    return 'keep', '整轮跑完，close_on_exit=false'


def _root_of(video_cfg, watch_roots=None):
    """素材根：admission.video.root 显式优先，否则 series_root。

    watch_roots 仅用于测试/临时覆盖；生产调用不再读取 watch.roots。
    """
    root = str(video_cfg.get('root') or '').strip()
    if root:
        return root
    try:
        idx = int(video_cfg.get('root_index', 0) or 0)
    except (TypeError, ValueError):
        raise ValueError('admission.video.root_index 必须是整数')
    if idx != 0:
        raise ValueError('admission.video.root_index 已废弃（>0 不再支持）；'
                         '请改用 admission.video.root 显式指定素材根')
    if watch_roots:
        return str(watch_roots[0])
    from export_config import get_series_root
    return get_series_root()


def resolve_series_dir(video_cfg, watch_roots=None):
    """解析素材剧集目录，返回 (目录, 匹配视频数)。

    顺序：显式 series 优先；否则 root 下 sorted(os.listdir()) 的第 series_index 个目录起，
    往后找第一个含 glob 匹配视频的目录（没有匹配就顺延，不失败退出）。
    """
    root = _root_of(video_cfg, watch_roots)
    if not os.path.isdir(root):
        raise ValueError(f'素材根目录不存在: {root}')

    series = video_cfg.get('series') or ''
    if series:
        candidates = [os.path.join(root, series)]
    else:
        names = sorted(os.listdir(root))
        dirs = [os.path.join(root, n) for n in names
                if os.path.isdir(os.path.join(root, n))]
        idx = video_cfg.get('series_index', 0)
        candidates = dirs[idx:]
        if not candidates:
            raise ValueError(f'{root} 下没有可用的剧集目录（series_index={idx}）')

    pattern = video_cfg.get('glob') or DEFAULT_VIDEO['glob']
    for cand in candidates:
        hits = glob.glob(os.path.join(cand, pattern))
        if hits:
            return cand, len(hits)
    raise ValueError(f'从 {root} 第 {video_cfg.get("series_index", 0)} 个目录起，'
                     f'没有匹配 {pattern} 的剧集目录（已检查 {len(candidates)} 个）')


def draft_fs_state(draft_dir):
    """文件层判据：{'state': 'ok'|'invalid'|'missing', 'detail', 'duration_us', 'video_segments'}"""
    content = os.path.join(draft_dir, 'draft_content.json')
    base = {'state': 'missing', 'detail': f'无 draft_content.json: {content}',
            'duration_us': 0, 'video_segments': 0}
    if not os.path.isfile(content):
        return base
    try:
        with open(content, encoding='utf-8') as f:
            draft = json.load(f)
    except Exception as e:
        return {**base, 'state': 'invalid', 'detail': f'draft_content.json 不可解析: {e}'}
    duration_us = _as_int(draft.get('duration'), 0, minimum=0)
    segs = 0
    for tr in draft.get('tracks') or []:
        if tr.get('type') == 'video':
            segs = len(tr.get('segments') or [])
            break
    if duration_us <= 0 or segs <= 0:
        return {**base, 'state': 'invalid', 'duration_us': duration_us,
                'video_segments': segs,
                'detail': f'草稿结构不完整（duration={duration_us}us, 主轨段={segs}）'}
    return {'state': 'ok', 'detail': '', 'duration_us': duration_us, 'video_segments': segs}


def active_draft_name(draft_root):
    """当前打开的草稿 ≈ draft_content.json 最近修改的那个（与 ui_recognize_translate 同口径）。"""
    try:
        best = None
        for name in os.listdir(draft_root):
            p = os.path.join(draft_root, name, 'draft_content.json')
            if os.path.isfile(p):
                ts = os.path.getmtime(p)
                if best is None or ts > best[0]:
                    best = (ts, name)
        return best[1] if best else None
    except Exception:
        return None


def compute_baseline(draft):
    """结构指纹：轨道组成 + 素材数 + 时长；video_segments 只记录不参与判脏。"""
    track_types = {}
    video_segments = 0
    for tr in draft.get('tracks') or []:
        ty = str(tr.get('type') or '?')
        track_types[ty] = track_types.get(ty, 0) + 1
        if ty == 'video' and not video_segments:
            video_segments = len(tr.get('segments') or [])
    mats = draft.get('materials') or {}
    return {
        'duration_us': _as_int(draft.get('duration'), 0, minimum=0),
        'track_types': dict(sorted(track_types.items())),
        'materials': {
            'videos': len(mats.get('videos') or []),
            'texts': len(mats.get('texts') or []),
            'audios': len(mats.get('audios') or []),
        },
        'video_segments': video_segments,
    }


def read_draft_structure(draft_dir):
    """读草稿并计算结构指纹；读不到返回 None。"""
    p = os.path.join(draft_dir, 'draft_content.json')
    try:
        with open(p, encoding='utf-8') as f:
            draft = json.load(f)
    except Exception:
        return None
    return compute_baseline(draft)


def read_baseline(path, draft_name):
    """读 baseline；文件缺失、损坏或名字不匹配 → None（按不可用处理）。"""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get('draft_name') != draft_name:
        return None
    return data


def write_baseline(path, draft_name, structure):
    payload = {'draft_name': draft_name,
               'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
               **(structure or {})}
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return payload
    except Exception:
        return None


def baseline_mismatch(baseline, current):
    """返回差异列表（空列表=干净）。无 baseline 视为不匹配。

    判脏三条：轨道组成变化、字幕素材数增加、时长偏差 >1%。
    video_segments 明确不参与（剪映打开草稿可能合并首尾相连片段，拿它判脏会假阳性）。
    """
    if not isinstance(baseline, dict):
        return ['无 baseline（首次运行或外部创建）']
    if not isinstance(current, dict):
        return ['当前草稿不可解析']
    diffs = []
    b_tracks = baseline.get('track_types') or {}
    c_tracks = current.get('track_types') or {}
    if b_tracks != c_tracks:
        diffs.append(f'轨道组成变化 {b_tracks} → {c_tracks}')

    b_texts = _as_int((baseline.get('materials') or {}).get('texts'), 0, minimum=0)
    c_texts = _as_int((current.get('materials') or {}).get('texts'), 0, minimum=0)
    if c_texts > b_texts:
        diffs.append(f'字幕素材增加 {b_texts} → {c_texts}')

    b_dur = _as_int(baseline.get('duration_us'), 0, minimum=0)
    c_dur = _as_int(current.get('duration_us'), 0, minimum=0)
    if b_dur > 0 and abs(c_dur - b_dur) > b_dur * 0.01:
        diffs.append(f'时长偏差 {b_dur / 1e6:.1f}s → {c_dur / 1e6:.1f}s')
    return diffs


def decide_action(editor_is_target, fs_ok, ui_found, dirty):
    """9 档决策表 → 'reuse' | 'open' | 'retry_open' | 'rebuild'。

    九档决策：
    reuse       编辑页已是目标稿、文件有效且干净：不建不重开
    open        首页有卡片、文件有效、干净：直接打开
    retry_open  文件有效但首页无卡片：先热等/冷重载尝试打开，失败再重建
    rebuild     缺失/损坏/脏/列表与磁盘不一致：重建后打开
    """
    if not fs_ok:
        return 'rebuild'
    if editor_is_target:
        return 'rebuild' if dirty else 'reuse'
    if dirty:
        return 'rebuild'
    return 'open' if ui_found else 'retry_open'
