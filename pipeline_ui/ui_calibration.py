# -*- coding: utf-8 -*-
"""
校准配置读写（ui_calibration.json）
====================================
准入测试把关键点位以"参照系+偏移"形式写入 JSON；
正式 pipeline 启动时读取并校验环境指纹，指纹不符提示重跑准入。
"""
from __future__ import annotations

import json
import os
import time

from ui_core import dprint, ddebug

# 多文件 calibration 存储目录（每屏/每分辨率/DPI/剪映版本一套）
_CALIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'calibrations')
# 兼容路径：旧单文件（存在则优先读取，过渡用）
CALIB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui_calibration.json')


def _calib_filename(env: dict) -> str:
    """env → 稳定文件名（分辨率+DPI+剪映版本，不含日期）。"""
    w, h = env.get('screen', [1920, 1080])
    scale = int(round(env.get('dpi_scale', 1.0) * 100))
    ver = env.get('jianying_version', 'unknown') or 'unknown'
    safe_ver = ver.replace('.', '')
    return f"env_{w}x{h}_dpi{scale}_jy{safe_ver}.json"


def _calib_path(env: dict) -> str:
    return os.path.join(_CALIB_DIR, _calib_filename(env))


def _all_calibration_files() -> list[str]:
    if not os.path.isdir(_CALIB_DIR):
        return []
    return sorted(
        [os.path.join(_CALIB_DIR, f) for f in os.listdir(_CALIB_DIR)
         if f.startswith('env_') and f.endswith('.json')],
        key=lambda p: os.path.getmtime(p), reverse=True,
    )


def _project_dirs():
    """剪映安装目录 / 成品导出目录（读 presets.json；失败返回空串）。"""
    import sys
    pipe = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pipeline'))
    if pipe not in sys.path:
        sys.path.insert(0, pipe)
    try:
        from export_config import get_jianying_dir, get_export_dir
        return get_jianying_dir(), get_export_dir()
    except Exception:
        return '', ''


def env_fingerprint(win=None):
    """当前环境指纹：屏幕分辨率 + DPI 缩放 + 剪映版本/目录/导出目录。
    win 传入时用 MonitorFromWindow 取窗口**实际所在屏**的分辨率（多屏/副屏场景关键）；
    win=None 时退到 GetSystemMetrics（主屏）。
    注意：必须用 GetSystemMetrics 而不是 GetDeviceCaps(hdc,117/118)，
    后者在非 DPI 感知进程里返回 0（之前踩过这个坑）。
    """
    import ctypes
    from ctypes import wintypes

    # --- 屏分辨率：优先用窗口所在屏 ---
    w, h = 0, 0
    monitor = None
    if win is not None:
        try:
            hwnd = win.NativeWindowHandle
            user32 = ctypes.windll.user32
            HMONITOR = wintypes.HMONITOR
            MONITOR_DEFAULTTONEAREST = 2
            monitor = HMONITOR(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))

            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', wintypes.DWORD),
                    ('rcMonitor', wintypes.RECT),
                    ('rcWork', wintypes.RECT),
                    ('dwFlags', wintypes.DWORD),
                ]
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                w = mi.rcMonitor.right - mi.rcMonitor.left
                h = mi.rcMonitor.bottom - mi.rcMonitor.top
        except Exception:
            pass
    if w <= 0 or h <= 0:
        try:
            w = ctypes.windll.user32.GetSystemMetrics(0)   # SM_CXSCREEN（主屏兜底）
            h = ctypes.windll.user32.GetSystemMetrics(1)   # SM_CYSCREEN
        except Exception:
            w, h = 0, 0

    # --- DPI 缩放（S3-A1）：按窗口所在显示器取（GetScaleFactorForMonitor），
    # 取不到回落主屏逻辑。诊断结论（2026-11-03）：GetDpiForMonitor 各 MDT 口径在
    # 本机均不反映用户缩放（EFFECTIVE 恒 96）；GetScaleFactorForMonitor 实测
    # mon0=100% / mon1(4K副屏)=150%，与用户缩放一致。
    scale = 0.0
    if monitor:
        try:
            sf = wintypes.UINT(0)
            if ctypes.windll.shcore.GetScaleFactorForMonitor(
                    monitor, ctypes.byref(sf)) == 0 and sf.value > 0:
                scale = sf.value / 100.0
        except Exception:
            scale = 0.0
    if scale <= 0:
        try:
            scale = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100.0
        except Exception:
            scale = 1.0

    # --- 剪映版本 ---
    jd, ed = _project_dirs()
    return {'screen': [w, h], 'dpi_scale': scale,
            'jianying_version': os.path.basename(jd.rstrip('\\/')) if jd else '',
            'jianying_dir': jd, 'export_dir': ed}


def save_calibration(data: dict, win=None):
    """写 calibration：同时写新多文件目录 + 兼容旧单文件（过渡）。
    多文件路径由 data['env'] 决定（准入写时必须有 env）；
    旧单文件始终更新，方便过渡期排查。"""
    data['calibrated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    # 旧单文件（兼容）
    with open(CALIB_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # 新多文件
    try:
        os.makedirs(_CALIB_DIR, exist_ok=True)
        env = data.get('env')
        if env:
            target = _calib_path(env)
            with open(target, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            dprint(f"  📌 calibration 已写入多文件: {os.path.basename(target)}")
    except Exception as e:
        dprint(f"  [warn] 多文件写入失败（旧单文件已更新可忽略）: {e}")
    # 清缓存
    _CACHE.clear()
    _CACHE['mtime'] = None


_CACHE = {'mtime': None, 'data': None, 'path': None}


# 当前剪映窗口（供 load_calibration / get_param 自动识别窗口所在屏）
# 由 click_audio_tab 等定位函数开头设置，pipeline 入口会 connect_jianying 并设一次
_CURRENT_WIN = None


def set_current_win(win):
    """设置当前剪映窗口（供 calibration 自动识别窗口所在屏的分辨率）。"""
    global _CURRENT_WIN
    _CURRENT_WIN = win


def load_calibration(win=None):
    """读校准 JSON：优先从多文件目录挑 env 匹配的，没有则回退旧单文件。
    win 传入时用窗口所在屏的分辨率做 env 匹配（多屏/副屏场景关键）。"""
    win = win or _CURRENT_WIN
    # 先找多文件目录里 env 匹配的
    cur = env_fingerprint(win)
    for p in _all_calibration_files():
        try:
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
            saved = data.get('env', {})
            if check_env_match(data, cur_env=cur):
                return data  # ✅ 多文件命中
        except Exception:
            continue
    # 没找到 → 回退旧单文件
    if os.path.exists(CALIB_PATH):
        try:
            with open(CALIB_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if check_env_match(data, cur_env=cur):
                return data
        except Exception:
            pass
    return None


def load_calibration_from(path: str) -> dict | None:
    """从指定路径读（pick_calibration 内部用）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def pick_calibration(win=None) -> tuple[dict | None, str]:
    """根据当前剪映窗口所在屏选 calibration。
    返回 (calibration_dict | None, path | reason_string)：
      - 命中多文件 → (dict, path)
      - 命中旧单文件 → (dict, CALIB_PATH) 并提示迁移
      - 没有任何可用 calibration → (None, 'no_calibration')
    不匹配当前 env 时返回 (None, 'env_mismatch')。
    """
    cur = env_fingerprint(win)
    dprint(f"  pick_calibration: 当前 env = {cur['screen']} dpi={cur['dpi_scale']} "
           f"jy={cur['jianying_version']}")

    # 1) 多文件目录：精确匹配
    for p in _all_calibration_files():
        data = load_calibration_from(p)
        if not data:
            continue
        saved = data.get('env', {})
        if check_env_match(data, cur_env=cur):
            dprint(f"  ✅ 命中多文件 calibration: {os.path.basename(p)}")
            return data, p

    # 2) 旧单文件：env 匹配 → 复用并提示迁移到多文件
    if os.path.exists(CALIB_PATH):
        data = load_calibration_from(CALIB_PATH)
        if data and check_env_match(data, cur_env=cur):
            dprint(f"  ⚠️ 旧单文件 env 匹配但无多文件副本 → 复用旧文件"
                   f"（下次准入自动迁移到 calibrations/）")
            return data, CALIB_PATH

    # 3) 都没命中（S1-3：区分两种情形，env_mismatch 带差异明细，避免误导读重跑准入）
    files = _all_calibration_files()
    if not files:
        return None, 'no_calibration'
    parts = [f"当前 env: screen={cur['screen']} dpi={cur['dpi_scale']} "
             f"jy={cur['jianying_version']}"]
    best, best_diffs = None, None
    for p in files:
        data = load_calibration_from(p)
        if not data:
            continue
        saved = data.get('env', {})
        diffs = []
        if cur['screen'] != saved.get('screen'):
            diffs.append(f"screen {saved.get('screen')}→{cur['screen']}")
        if abs(cur['dpi_scale'] - saved.get('dpi_scale', 1.0)) >= 0.01:
            diffs.append(f"dpi {saved.get('dpi_scale')}→{cur['dpi_scale']}")
        if saved.get('jianying_version') and saved['jianying_version'] != cur['jianying_version']:
            diffs.append(f"jy {saved['jianying_version']}→{cur['jianying_version']}")
        if best is None or len(diffs) < len(best_diffs):
            best, best_diffs = p, diffs
    if best:
        parts.append(f"最接近: {os.path.basename(best)}"
                     f"（差异: {'；'.join(best_diffs) or '无（疑似环境瞬态）'}）")
    parts.append("先确认窗口已放置主屏再重试，不要盲目重跑准入")
    return None, 'env_mismatch | ' + ' | '.join(parts)


def check_env_match(calib: dict, cur_env: dict | None = None) -> bool:
    """环境指纹与校准时是否一致（分辨率/缩放/剪映版本）。
    cur_env 可传入（避免重复读 env），否则按当前剪映窗口所在屏读取。"""
    cur = cur_env or env_fingerprint(_CURRENT_WIN)
    saved = calib.get('env', {})
    if cur['screen'] != saved.get('screen'):
        return False
    if abs(cur['dpi_scale'] - saved.get('dpi_scale', 1.0)) >= 0.01:
        return False
    sv = saved.get('jianying_version')
    if sv and sv != cur.get('jianying_version'):
        return False
    return True


# ================= 初检：校准 JSON 完整性校验 =================
# value 含义: 0=任意非 None；正整数 n=长度为 n 的数值列表；'nonempty'=非空字符串
# 范围 = 准入全流程（--stop-after export）应写入、pipeline 会读取的参数
REQUIRED_PARAMS = {
    'env': {'screen': 2, 'dpi_scale': 0, 'jianying_version': 'nonempty',
            'jianying_dir': 'nonempty', 'export_dir': 'nonempty',
            'fullscreen_rect': 4, 'rect_tolerance': 0},
    'main_track': {'cover_x_range': 2, 'contrast_threshold': 0, 'min_cols': 0},
    'track_header': {'lock_x': 0, 'eye_x': 0, 'mute_x': 0, 'cover_x': 0,
                     'eye_strip': 2, 'blue_threshold': 0, 'lock_strip': 2,
                     'icon_l_ratio': 0, 'icon_r_ratio': 0, 'main_seg_click_x': 0},
    'audio_tab': {'coord': 2, 'ocr_region': 4},
    'vocal_switch': {'switch_coord': 2, 'switch_click_coord': 2,
                     'checkbox_offset': 2, 'blue_check': 0, 'appear_timeout': 0},
    'recognize': {'subtitle_tab_center': 2, 'translate_switch_center': 2,
                  'translate_dropdown_center': 2, 'translate_english_center': 2,
                  'translate_list_region': 4, 'translate_value_region': 4,
                  'start_recog_btn_center': 2, 'start_recog_btn_region': 4,
                  'left_list_region': 4},
}
_SNAPSHOT_SECTIONS = ('subtitle_tracks', 'final_layout', 'processing_keywords')


def _check_value(val, spec):
    """按 spec 校验单个值，返回错误描述或 None。"""
    if val is None:
        return '缺失'
    if spec == 'nonempty':
        if not isinstance(val, str) or not val.strip():
            return '为空'
        return None
    if isinstance(spec, int) and spec > 0:
        if not (isinstance(val, (list, tuple)) and len(val) == spec
                and all(isinstance(v, (int, float)) for v in val)):
            return f'应为长度 {spec} 的数值列表'
    return None


def validate_calibration(calib: dict = None) -> dict:
    """初检：ui_calibration.json 是否包含 pipeline 所需全部内容。
    返回 {'ok', 'errors', 'warnings', 'info'}——errors 非空应阻断 pipeline：
      - errors：JSON 缺失/环境指纹不符/缺必需 section 或 key（get_param 会退默认值或失效）
      - warnings：降级可用（运行期自学习/默认值兜底）
      - info：快照类 section（运行期重写，只读留档）
    """
    errors, warnings, info = [], [], []
    if calib is None:
        calib = load_calibration()
    if not calib:
        errors.append('ui_calibration.json 不存在或无法解析')
        return {'ok': False, 'errors': errors, 'warnings': warnings, 'info': info}
    info.append(f"calibrated_at = {calib.get('calibrated_at', '未知')}")

    # 环境指纹：不匹配时 get_param 会静默退回默认值，必须提前拦截
    cur = env_fingerprint(_CURRENT_WIN)
    if not check_env_match(calib, cur_env=cur):
        saved = calib.get('env', {})
        diff = []
        if cur['screen'] != saved.get('screen'):
            diff.append(f"分辨率 {saved.get('screen')}→{cur['screen']}")
        if abs(cur['dpi_scale'] - saved.get('dpi_scale', 1.0)) >= 0.01:
            diff.append(f"DPI {saved.get('dpi_scale')}→{cur['dpi_scale']}")
        if saved.get('jianying_version') and saved['jianying_version'] != cur['jianying_version']:
            diff.append(f"剪映版本 {saved['jianying_version']}→{cur['jianying_version']}")
        errors.append('环境指纹不匹配（' + ('；'.join(diff) or '未知原因') + '）')
    else:
        info.append('环境指纹匹配（分辨率/DPI/剪映版本）')

    for section, keys in REQUIRED_PARAMS.items():
        sec = calib.get(section)
        if not isinstance(sec, dict):
            errors.append(f'缺少 section: {section}')
            continue
        for key, spec in keys.items():
            # main_track 兼容两种写法：pipeline 读平铺 cover_x_range，准入写在 m2 子层
            if (section == 'main_track' and key not in sec
                    and isinstance(sec.get('m2'), dict) and key in sec['m2']):
                warnings.append(f'main_track.{key} 在 m2 子层（pipeline 读平铺路径，暂用默认值）')
                continue
            err = _check_value(sec.get(key), spec)
            if err:
                errors.append(f'{section}.{key} {err}')

    # 运行期自学习参数（缺失会降级，首次操作时自动学习落盘，不阻断）
    th = calib.get('track_header') or {}
    if not (th.get('lock_on_mean') and th.get('lock_off_mean')):
        warnings.append('track_header.lock_on/off_mean 未学习，锁定检测先用蓝像素规则')
    vs = calib.get('vocal_switch') or {}
    for k in ('arrow_dx', 'arrow_dy'):
        if k not in vs:
            warnings.append(f'vocal_switch.{k} 未落盘，用默认值')

    for s in _SNAPSHOT_SECTIONS:
        info.append(f"快照 {s}: {'有' if s in calib else '无'}（运行期重写，不阻断）")
    return {'ok': not errors, 'errors': errors, 'warnings': warnings, 'info': info}


# ================= JSON 驱动参数（操作函数统一取参入口） =================

# DEFAULTS：所有可校准项的集中默认值表。
# 优先级：calibration JSON 实测值 > DEFAULTS 集中默认 > 调用方显式传入的 default > None
# 新硬编码常量必须先在此声明，再加 get_param('section', 'key') 调用——杜绝散落硬编码。
DEFAULTS = {
    # --- 人声分离面板 ---
    'vocal_switch': {
        'scroll_delta': -800,           # 滚轮幅度（滚到底/回顶）
        'scroll_rounds_bottom': 12,     # 滚到底次数
        'scroll_rounds_top': 2,         # 滚回顶部次数
        'scroll_rounds_each': 3,        # 每轮下滚次数
        'scroll_sleep_after': 0.08,     # 每次 scroll 后的 sleep(s)
        'scroll_mx_ratio': 0.86,        # 滚动鼠标锚点（窗口宽比例）
        'scroll_my_ratio': 0.48,        # 滚动鼠标锚点（窗口高比例）
        'scroll_my_ratio_search': 0.55, # 搜索面板时的 my 比例
        'blue_on_min': 40,              # 蓝像素判"开关已开"的门槛
        'click_offsets': (2, 6, -2, 10, -6, 14),  # 开关点击横移覆盖序列
        'switch_y_offset_pair': (121, 87),         # 候选对勾点 y 偏移（两种状态）
        'switch_x_offset': 6,           # 候选对勾点 x 偏移
        'banner_poll_timeout': 12,      # 点击后轮询 banner 出现的超时(s)
        'banner_poll_interval': 0.5,    # banner 轮询间隔(s)
        'processing_poll_interval': 5.0,  # AND 判据阶段A/B 处理提示轮询间隔(s)，可通过 calibration 覆盖
        'list_region_y': [250, 700],    # 下拉 OCR y 范围（逻辑px）
    },
    # --- 轨道头几何 ---
    'track_header': {
        'row_brightness_thresh': 90,    # 轨道行亮度门槛（>90 才算亮列）
        'row_max_height': 40,           # 合并后行高上限（>40 判为非轨道）
        'row_merge_gap': 15,            # 相邻行合并间距（<15 合并）
        'row_top_offset': 30,           # 排除紧贴时间线顶的工具栏
    },
    # --- 面板边缘锚定 ---
    'panel_edges': {
        'fallback_ratio': [0.0, 0.425, 0.726],  # 面板右缘三级降级比例
        'combo_left_logical': 118,       # 声道配置下拉左缘（逻辑px）
        'right_content_left_logical': 1394,  # 右面板内容左缘（逻辑px，1920基准）
        'right_right_logical': 816,      # 面板右缘（逻辑px，相对右面板左缘）
    },
    # --- 识别/翻译 ---
    'recognize': {
        'switch_blue_delta': 40,         # 翻译开关蓝像素 delta（B-R）
        'switch_on_min_pixels': 20,      # 翻译开关蓝像素开启门槛
        'switch_on_diff_ratio': 0.15,    # 开关态白像素比率差
        'dropdown_row_offset_x': 182,    # 翻译下拉箭头相对左侧的 x
    },
}


_TRACE = []   # 本步骤所有 get_param 读取记录（由 pipeline 每步取走落盘）


def take_trace():
    """取走并清空当前步骤的 get_param 读取记录。"""
    out = list(_TRACE)
    _TRACE.clear()
    return out


def get_param(section: str, key: str, default=None):
    """读校准参数：JSON > DEFAULTS > 显式 default > None。
    所有 UI 操作函数的坐标/阈值/偏移统一从这里取。
    调用方不传 default 时自动查 DEFAULTS 集中表（杜绝散落硬编码）。"""
    calib = load_calibration()
    if calib and check_env_match(calib, cur_env=env_fingerprint(_CURRENT_WIN)):
        has = isinstance(calib.get(section), dict) and key in calib[section]
        if has:
            value = calib[section][key]
            _TRACE.append({'section': section, 'key': key, 'value': value, 'source': 'json'})
            return value
    # JSON 没命中 → 查 DEFAULTS 集中表
    def_val = DEFAULTS.get(section, {}).get(key)
    if def_val is not None:
        _TRACE.append({'section': section, 'key': key, 'value': def_val, 'source': 'defaults'})
        return def_val
    # DEFAULTS 也没有 → 调用方显式传入的 default（可能还是 None）
    _TRACE.append({'section': section, 'key': key, 'value': default, 'source': 'none'})
    return default


def update_param(section: str, key: str, value, coord_tolerance: int = 5):
    """把实测成功的点位/参数回写 JSON（下次直接复用，跳过 OCR 重算）。
    环境指纹不匹配（换了机器/分辨率）时不写，避免污染错误环境的坐标。

    坐标写回守卫：新值是 [x,y] 二元组且与旧值偏差 ≤ coord_tolerance(px) 时跳过落盘，
    避免每次 pipeline 都重写相同坐标导致不必要的文件 I/O 和时间戳抖动。"""
    calib = load_calibration()
    if not calib:
        return False
    if not check_env_match(calib, cur_env=env_fingerprint(_CURRENT_WIN)):
        return False
    # 坐标写回守卫：偏差 ≤ 容差跳过
    old = calib.get(section, {}).get(key)
    if (isinstance(old, (list, tuple)) and len(old) == 2
            and isinstance(value, (list, tuple)) and len(value) == 2):
        try:
            dx = abs(int(value[0]) - int(old[0]))
            dy = abs(int(value[1]) - int(old[1]))
            if dx <= coord_tolerance and dy <= coord_tolerance:
                ddebug(f"  [skip] {section}.{key} 偏差 ({dx},{dy})px ≤ {coord_tolerance}px，跳过写回")
                return True
        except (TypeError, ValueError):
            pass  # 非整数坐标格式，正常写盘
    calib.setdefault(section, {})[key] = value
    save_calibration(calib)
    dprint(f"  📌 点位已落盘 {section}.{key} = {value}")
    return True


def quadrant_range(win, q: str):
    """象限 → OCR 搜索范围（窗口相对 x_range/y_range 二元组）。
    人声分离相关 UI 只出现在 Q2（顶栏音频Tab）/ Q4（面板底部开关）——
    用象限约束排除 Q1 媒体面板「音频」同名文字干扰。
    q ∈ 'Q1' | 'Q2' | 'Q3' | 'Q4'；窗口不可用时返回 (None, None)。"""
    try:
        r = win.BoundingRectangle
        WL, WT = int(r.left), int(r.top)
        WW, WH = int(r.right) - WL, int(r.bottom) - WT
    except Exception:
        return None, None
    if WW <= 0 or WH <= 0:
        return None, None
    half_w, half_h = WW // 2, WH // 2
    right = q in ('Q2', 'Q4')
    bottom = q in ('Q3', 'Q4')
    return ((half_w, WW) if right else (0, half_w),
            (half_h, WH) if bottom else (0, half_h))


def compute_quadrants(rect):
    """全屏 rect (L,T,R,B) → 四象限字典（准入时写入 JSON env 留档）。
    运行时定位用 quadrant_range（按当前窗口动态算，与全屏守卫配合）。"""
    L, T, R, B = rect
    hw, hh = (R - L) // 2, (B - T) // 2
    return {
        'Q1': {'x': [L, L + hw], 'y': [T, T + hh]},      # 左上：媒体面板（同名干扰区）
        'Q2': {'x': [L + hw, R], 'y': [T, T + hh]},      # 右上：预览区 + 顶栏Tab（音频Tab在这）
        'Q3': {'x': [L, L + hw], 'y': [T + hh, B]},      # 左下：时间线轨道区
        'Q4': {'x': [L + hw, R], 'y': [T + hh, B]},      # 右下：面板底部（人声分离开关在这）
    }
