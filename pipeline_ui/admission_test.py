# -*- coding: utf-8 -*-
"""
准入测试（分阶段版：先走通 pipeline 全流程，全绿后才允许跑正式 pipeline）
=====================================================================
前提：presets.json 的 admission 段已配置。准入会按三层判据（文件层/UI层/baseline）自动
确保草稿存在：缺失或跑脏时复用 ui_pipeline 的建稿链路新建（默认 ui_pipeline_ac），并自动
拉起剪映；不再要求先手动启动剪映并打开草稿。

预设与轨道口径：本轮预设组由 admission.preset_profile 指定，留空则回落 presets.json 的
default_preset_profile；预期轨道数 = 主轨 1 + 该组实际预设条数，用于 tracks 校验，
不再使用写死的轨道数。initial_layout 里 visible_row_count（记录时可见行）与
expected_track_count（应校验的轨道总数）分开命名，两者不是同一口径。

进程归属：准入只接管"本轮由自己拉起/重启"的剪映。收尾窗口策略：
  1) --keep-window / --close-window（互斥）显式覆盖一切；
  2) 未跑完全部阶段（--stop-after 中途停或失败）默认保留窗口——这种停点就是给人肉眼确认的，
     不关闭、不清理临时文件；
  3) 整轮跑完才看 admission.open.close_on_exit（true=正常关闭并清理临时文件，false=保留）。
收尾日志会明确打印本次是"中途停点保留窗口"还是"跑完按配置关闭/保留"。

阶段（--stop-after，默认 locate）：
  fullscreen  Ctrl+F 进全屏基准态 → 记录 fullscreen_rect + 四象限
              （此后所有坐标均以该态为参照；每阶段前 ensure_fullscreen 守卫）
  tracks      先重置全 visible（排除遗留状态）→ 主轨双重校验(M1最底video + M2封面按钮)
              → 预设轨逐行 锁定+隐藏 → 主轨保可见
  select      Home 回首帧 → 重扫定位主轨（隐藏后行上移）→ 点主轨 → Ctrl+A
  audio_tab   Q2 OCR 点「音频」Tab → 滚面板到底 → 切「基础」子Tab → Q4 校验面板
  locate      Q4 OCR 找「人声分离」文字 → 对勾坐标 → 截图+标记
              【常用停点：截图留证；中途停点默认保留窗口，方便开窗肉眼确认】
  enable      点对勾 + 蓝像素验证 → 立即选下拉模式（--vocal-mode）
  wait        End 跳末帧 → 等分离完成（= pipeline [3]）
              ⚠️ stop_after=wait 时收尾恢复所有轨道；后续还有阶段则保持预设隐藏
  recognize   识别中文字幕+翻译英文（= pipeline [4]，调 ui_recognize_translate）
  extract     分离音频+字幕轨收尾+布局落盘（= pipeline [5]，调 ui_extract_audio）
  export      导出 MP4（= pipeline [6]，调 ui_pipeline.step_export）

输出：pipeline_ui/ui_calibration.json（所有已校准点位，环境指纹绑定）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

_UI_DIR = os.path.dirname(os.path.abspath(__file__))
if _UI_DIR not in sys.path:
    sys.path.insert(0, _UI_DIR)
# presets.json / export_config / init_draft 在 ..\pipeline
_PIPELINE_DIR = os.path.normpath(os.path.join(_UI_DIR, '..', 'pipeline'))
if _PIPELINE_DIR not in sys.path:
    sys.path.insert(0, _PIPELINE_DIR)

from export_config import get_ui_pylibs_dir

# UI OCR 依赖目录由统一配置解析；目录不存在时插入不报错，缺包在首次 OCR 时给出可读错误
_UI_PYLIBS = get_ui_pylibs_dir()
if _UI_PYLIBS not in sys.path:
    sys.path.insert(0, _UI_PYLIBS)

from admission_draft import (active_draft_name, baseline_mismatch, decide_action,
                             decide_window_action, draft_fs_state, expected_track_count,
                             normalize_config, read_baseline, read_draft_structure,
                             resolve_series_dir, write_baseline)
from export_config import get_admission_config, get_watch_config

import pyautogui
pyautogui.FAILSAFE = False
from PIL import Image, ImageDraw

from ui_core import (connect_jianying, dprint, get_window_rect,
                     send_keys, save_screenshot, PROPERTY_ID_FULLDESC, op_mark,
                     op_stage, stage_timeline)
from ui_fullscreen import ensure_fullscreen, enter_fullscreen
from ui_calibration import compute_quadrants, quadrant_range
import ui_calibration
import ui_track_ops
from ui_track_ops import (scan_tracks, find_main_track, hide_preset_tracks,
                          unhide_all_tracks, unlock_all_tracks)

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(OUT_DIR, '_admission_report.json')
# S3-A3 保护：过程打点写 partial，只有整轮跑完才覆盖正式报告 —— 失败/中断不再污染
# _admission_ratios() 读取的基线（A3 失败时正式报告被写成半截数据）
REPORT_PARTIAL_PATH = os.path.join(OUT_DIR, '_admission_report.partial.json')
# 准入草稿的结构基线：建稿成功后写入；复用前比对，判定草稿是否已被上一轮跑脏
BASELINE_PATH = os.path.join(OUT_DIR, '_admission_draft_baseline.json')
STAGES = ['fullscreen', 'tracks', 'select', 'audio_tab', 'locate', 'enable', 'wait',
          'recognize', 'extract', 'export']

# ===== 逻辑偏移常量（相对基准，100% DPI 口径；读取方按 ×dpi_scale 换算）=====
# 这些不是"1920 绝对像素"，而是跨 DPI 复用的逻辑量：
#   - 时间线左缘类：ui_track_ops._hdr_x / header_abs_x 已 ×scale
#   - 窗口原点类：使用处会加窗口 left/top 并 ×scale
# 1920 下（origin=(0,0), dpi_scale=1.0）换算结果与旧字面量完全一致。
# 音频Tab 贴面板右缘（与 ui_vocal_separate.click_audio_tab 的 -452 同一锚定模型），
# 故兜底偏移从窗口右缘反推：1920 下 1920-458=1462，与旧字面量等价；4K 下随右缘走。
AUDIO_TAB_FALLBACK_FROM_RIGHT_LOGICAL = (458, 57)
AUDIO_TAB_RETRY_DX_LOGICAL = (-22, 22, -40, 40, -60, 60)   # 面板校验失败横移候选
VOCAL_CHECKBOX_OFFSET_LOGICAL = [-3, 0]        # 「人声分离」文字框 → 对勾
MAIN_TRACK_LOGICAL = {
    'cover_x_range': [120, 160],   # 主轨封面按钮 M2 的 x 区间（相对时间线左缘）
    'lock_x': 50, 'mute_x': 98, 'cover_x': 143,
    'eye_strip': [60, 90], 'lock_strip': [38, 62],
    'main_seg_click_x': 300,
}


_RUN_STATE = {'ui_params': {}, 'results': {}, 'calib': {}, 'marks': [], 'error': None}


def _infer_failed_stage(marks, results):
    """失败阶段 = 已成功打点之后的下一阶段；无打点时为首阶段。"""
    done = [n.split('.')[0] for n, _ in marks if n.split('.')[0] in STAGES]
    idx = max(STAGES.index(s) for s in done) if done else -1
    nxt = idx + 1
    if nxt >= len(STAGES):
        for s in STAGES:
            if s not in results:
                return s
        return None
    return STAGES[nxt]


def _classify_failure(error, stage):
    """把准入异常映射到 lib/failure_class 的类别（导不出时返回 None，不影响落盘）。"""
    if not error:
        return None
    try:
        from failure_class import classify
        cat, *_ = classify(error, stage or 'open_draft')
        return cat
    except Exception:
        return None


def _dump_report(ui_params, results, calib, marks, final=False,
                 error=None, failed_stage=None):
    """准入输出：每阶段读到的 UI 入参 + 结果/耗时/关键数值 + 打点时间线。

    generated_by=admission_test；ui_pipeline 只是调用方，不负责记录。
    正式报告（final=True）在跑完即写：失败也落正式报告并带 ok/failed_stage/
    failure_class，partial 只表示「仍在进行中」。
    """
    try:
        stages = {}
        for name in list(dict.fromkeys(list(ui_params) + list(results))):
            r = results.get(name, {})
            stages[name] = {
                'ui_params': ui_params.get(name, []),
                'ok': r.get('ok'),
                'duration_s': r.get('duration_s'),
                'error': r.get('error'),
                'metrics': r.get('metrics', {}),
            }
        if error and not failed_stage:
            failed_stage = _infer_failed_stage(marks, results)
        t_base = marks[0][1] if marks else time.time()
        rec = {
            'generated_by': 'admission_test',
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'ok': bool(stages) and all(s.get('ok') is True for s in stages.values())
                  and not error,
            'failed_stage': failed_stage if error else None,
            'failure_class': _classify_failure(error, failed_stage),
            'error': error,
            'env': calib.get('env', {}),
            'stages': stages,
            'timeline': [{'step': n, 't_s': round(t - t_base, 1)} for n, t in marks],
        }
        with open(REPORT_PATH if final else REPORT_PARTIAL_PATH, 'w', encoding='utf-8') as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception as e:
        dprint(f"  [warn] 准入报告落盘失败: {e}")


def _shot(win, name):
    return save_screenshot(win, os.path.join(OUT_DIR, name))


def _mark_point(img_path, rect, points, out_path):
    """在窗口截图上标记点位（红十字）供肉眼确认。points=[(x,y,label)]，坐标为屏幕绝对值。"""
    try:
        img = Image.open(img_path).convert('RGB')
        dr = ImageDraw.Draw(img)
        for (x, y, label) in points:
            px, py = x - rect[0], y - rect[1]
            dr.ellipse([px - 12, py - 12, px + 12, py + 12], outline=(255, 0, 0), width=3)
            dr.line([px - 18, py, px + 18, py], fill=(255, 0, 0), width=2)
            dr.line([px, py - 18, px, py + 18], fill=(255, 0, 0), width=2)
            dr.text((px + 16, py - 10), label, fill=(255, 0, 0))
        img.save(out_path)
        return out_path
    except Exception as e:
        dprint(f"  标记截图失败: {e}")
        return None


def _click_panel_tab_by_desc(win, tab_name):
    """用 UIA desc 找面板子Tab点（如 'VESettingPanelSubTabControl:基础'）"""
    target = f'VESettingPanelSubTabControl:{tab_name}'
    t = win.TextControl(searchDepth=10,
                        Compare=lambda c, d: (c.GetPropertyValue(PROPERTY_ID_FULLDESC) or '') == target)
    if t.Exists(1):
        op_mark('点击', f'音频子Tab「{tab_name}」')
        t.Click(simulateMove=False)
        dprint(f"  点音频子Tab desc='{target}' ✅")
        return True
    dprint(f"  ⚠️ 未找到子Tab desc='{target}'")
    return False


def _load_admission_cfg():
    """读 presets.json 的 admission 段并校验；draft_name 缺失/非法直接退出。"""
    try:
        raw = get_admission_config()
    except Exception as e:
        raise SystemExit(f'❌ 读取 presets.json 的 admission 段失败: {e}')
    try:
        return normalize_config(raw)
    except ValueError as e:
        raise SystemExit(f'❌ admission 配置不可用: {e}')


def _admission_draft_dir(cfg):
    from pipeline import DRAFT_ROOT
    return os.path.join(DRAFT_ROOT, cfg['draft_name'])


SNAPSHOT_DIR = os.path.join(OUT_DIR, '_draft_snapshots')


def _snapshot_draft(draft_dir):
    """本轮准入开始前给 draft_content.json 存快照（失败退出时自动回滚用）。

    快照点在「草稿已按三层判据就绪、任何点击/按键之前」，即本轮唯一的可信起点。
    存到 pipeline_ui/_draft_snapshots（独立目录，剪映不会读写这里）。
    返回快照路径；存不了返回 None（不阻塞准入，失败时退化为不回滚）。
    """
    src = os.path.join(draft_dir, 'draft_content.json')
    if not os.path.isfile(src):
        dprint('  ⚠️ 草稿快照跳过：draft_content.json 不存在')
        return None
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        name = os.path.basename(os.path.normpath(draft_dir))
        dst = os.path.join(SNAPSHOT_DIR,
                           f'{name}_{time.strftime("%Y%m%d_%H%M%S")}.json')
        shutil.copy2(src, dst)
        dprint(f'  📸 本轮草稿快照: {dst}')
        return dst
    except Exception as e:
        dprint(f'  ⚠️ 草稿快照失败（继续准入，失败时无法自动回滚）: {e}')
        return None


def _rollback_draft(snapshot, draft_dir):
    """失败退出时用本轮起点快照覆盖 draft_content.json，避免把跑脏的草稿留给用户。

    必须在剪映关闭之后调用（_finalize_jianying 之后），否则内存态会在退出时覆盖。
    """
    if not snapshot or not draft_dir or not os.path.isfile(snapshot):
        dprint('  ⚠️ 无本轮草稿快照，跳过自动回滚')
        return False
    dst = os.path.join(draft_dir, 'draft_content.json')
    try:
        shutil.copy2(snapshot, dst)
        dprint(f'  ↩️ 已把草稿回滚到本轮起点快照: {dst}')
    except Exception as e:
        dprint(f'  ❌ 草稿自动回滚失败: {e}')
        return False
    try:
        out = os.popen('tasklist /FI "IMAGENAME eq JianyingPro.exe" /NH').read()
        if 'JianyingPro.exe' in out:
            dprint('  ⚠️ 剪映仍在运行：回滚可能被其内存态覆盖，请关闭草稿后确认磁盘版本')
    except Exception:
        pass
    return True


def _resolve_preset_plan(cfg):
    """解析本轮预设组 → (presets, 来源, 预期轨道数)。

    预设组来源：admission.preset_profile；为空时回落到 presets.json 的
    default_preset_profile（由 resolve_presets 内部处理）。解析不到或该组为空
    直接报错，不使用任何写死的轨道数兜底。
    """
    from pipeline import resolve_presets
    try:
        presets, src = resolve_presets(cfg['preset_profile'])
    except RuntimeError as e:
        raise SystemExit(f'❌ 预设组解析失败（admission.preset_profile / '
                         f'default_preset_profile）: {e}')
    try:
        expected = expected_track_count(presets)
    except ValueError as e:
        raise SystemExit(f'❌ {e}')
    return presets, src, expected


def _build_admission_draft(cfg, presets):
    """复用 ui_pipeline 建稿链路（init_draft）新建准入草稿，并记录结构基线。

    链路不变：暂存区建骨架 → 注入主轨 → 合并预设 → 构建期校验 → 原子发布覆盖同名目录。
    模板候选顺序 default → pipeline_test → demo_test 由 create_empty_draft 内部保证。
    presets 与预期轨道数由同一份解析结果传入，避免记录口径与实际合并不一致。
    """
    roots = get_watch_config().get('roots') or []
    series_dir, hits = resolve_series_dir(cfg['video'], roots)
    dprint(f"  素材来源: {series_dir}（匹配 {hits} 个视频）")
    from init_draft import init_draft
    r = init_draft(draft_name=cfg['draft_name'], video_dir=series_dir,
                   presets=presets, preset_profile=cfg['preset_profile'],
                   video_glob=cfg['video']['glob'],
                   video_limit=cfg['video']['limit'],
                   video_range=cfg['video']['range'],
                   cut_s=cfg['cut_s'], normalize=cfg['normalize'], open_after=False)
    if not r.get('ok'):
        raise RuntimeError(f"建稿失败（{cfg['draft_name']}）: {r.get('error')}")
    draft_dir = _admission_draft_dir(cfg)
    structure = read_draft_structure(draft_dir)
    if not structure:
        raise RuntimeError(f'建稿后读不到草稿结构: {draft_dir}')
    write_baseline(BASELINE_PATH, cfg['draft_name'], structure)
    dprint(f"  baseline 已记录: 轨道={structure['track_types']} "
           f"字幕={structure['materials']['texts']} "
           f"时长={structure['duration_us'] / 1e6:.1f}s")
    return structure


def _open_admission_draft(cfg, after_build=False):
    """打开准入草稿。刚建完时列表必陈旧 → hot_wait=0 直接冷启动，省掉热等。"""
    import ui_nav
    op = cfg['open']
    ui_nav.open_draft(
        cfg['draft_name'], timeout=op['edit_timeout'],
        hot_wait=op['hot_wait_after_build'] if after_build else 30,
        cold_wait=op['cold_wait'], max_launch=op['max_launch'],
        allow_cold_restart=op['allow_cold_restart'],
        ready_timeout=op.get('ready_timeout', 120.0))


def _ensure_draft(cfg, presets):
    """三层判据（文件层/UI层/baseline）→ 复用/打开/重建；找不到就自己建。

    返回时编辑页就是准入草稿；失败直接抛错（调用方统一回首页兜底）。
    """
    import ui_nav
    name = cfg['draft_name']
    draft_dir = _admission_draft_dir(cfg)
    op = cfg['open']

    win, pt = connect_jianying(retry=False)
    editor_is_target = (bool(win) and pt == 'edit'
                        and active_draft_name(os.path.dirname(draft_dir)) == name)
    ui_found = False
    if not editor_is_target:
        if win and pt != 'home':
            ui_nav.switch_to_home(max_wait=30)
        ui_found = ui_nav._find_draft_ctrl_any(name) is not None

    fs = draft_fs_state(draft_dir)
    fs_ok = fs['state'] == 'ok'
    diffs = []
    if fs_ok:
        diffs = baseline_mismatch(read_baseline(BASELINE_PATH, name),
                                  read_draft_structure(draft_dir))
    dirty = bool(diffs)
    action = decide_action(editor_is_target, fs_ok, ui_found, dirty)
    _bl = ('脏（' + '；'.join(diffs) + '）') if dirty else ('干净' if fs_ok else '不可判')
    dprint(f"  [ensure] 草稿「{name}」: 文件层={fs['state']} "
           f"UI层={'有卡片' if ui_found else '无卡片'} "
           f"编辑页={'是' if editor_is_target else '否'} baseline={_bl} → {action}")
    if not fs_ok:
        dprint(f"           文件层原因: {fs['detail']}")

    if action == 'reuse':
        dprint('  ✅ 复用当前编辑页草稿（未重建）')
        return
    if action == 'open':
        _open_admission_draft(cfg)
        return
    if action == 'rebuild':
        if editor_is_target or pt == 'edit':
            ui_nav.switch_to_home(max_wait=30)
        dprint('  🏗 重建准入草稿（缺失/损坏/跑脏/列表与磁盘不一致）')
        _build_admission_draft(cfg, presets)
        _open_admission_draft(cfg, after_build=True)
        return
    # retry_open：文件在但首页无卡片 —— 先热/冷重载尝试打开，仍失败才重建
    try:
        _open_admission_draft(cfg)
    except RuntimeError as e:
        if not op['allow_cold_restart']:
            raise RuntimeError(f'草稿存在但当前打不开，且已禁止冷启动: {e}')
        dprint(f'  ⚠️ 首页无卡片且重载后仍打不开（{e}）→ 重建兜底')
        _build_admission_draft(cfg, presets)
        _open_admission_draft(cfg, after_build=True)


def calibrate(stop_after='locate', vocal_mode='none', target_screen=None):
    assert vocal_mode in ('none', 'vocal', 'background'), \
        f"vocal_mode 必须是 none/vocal/background，Got {vocal_mode!r}"
    # S1-2：env 占位——实际值在 connect_jianying（内含放置补丁，窗口已挪主屏）
    # 之后用 env_fingerprint(win) 取窗口所在屏，与 pipeline 侧口径对称
    calib = {'env': {}}
    from ui_core import enable_timing
    enable_timing()          # 每行 dprint 带 [+耗时s] 前缀
    _marks = []              # [(阶段名, 时间点)]
    ui_param_log = {}        # 阶段 → 该阶段读取的 UI 入参
    stage_results = {}       # 阶段 → {ok, duration_s, error, metrics}
    # 供 __main__ 在异常路径写正式报告（失败也要留痕）
    _RUN_STATE.update(ui_params=ui_param_log, results=stage_results, calib=calib,
                      marks=_marks, error=None)

    def _mark(name):
        stage = name.split('.')[0]
        _marks.append((name, time.time()))
        trace = ui_calibration.take_trace()
        if trace:
            ui_param_log.setdefault(stage, []).extend(trace)
        _dump_report(ui_param_log, stage_results, calib, _marks)

    t0 = time.time()
    _mark('start')
    stop_idx = STAGES.index(stop_after)

    def _reached(stage):
        return STAGES.index(stage) <= stop_idx

    dprint('=' * 60)
    dprint(f'[准入测试] 开始  stop_after={stop_after}')
    dprint('=' * 60)

    # ---------- 阶段 fullscreen：确保准入草稿存在（归属阶段 2 UI初始化）----------
    op_stage('2 UI初始化')
    # S3-A2：准入目标屏（默认 None=主屏，现状）；set_screen_policy 必须在任何 connect 之前
    if target_screen:
        from ui_core import set_screen_policy
        set_screen_policy('target', target_screen)
        dprint(f'  目标屏={target_screen}（放置策略=target）')
    # 三层判据 → 复用/打开/重建（可能要冷启动重开剪映）
    _adm_cfg = _load_admission_cfg()
    _presets, _preset_src, _expected_tracks = _resolve_preset_plan(_adm_cfg)
    dprint(f'  预设组: {_preset_src}（{len(_presets)} 条）'
           f'→ 预期轨道 {_expected_tracks} 条（主轨 1 + 预设 {len(_presets)}）')
    calib['preset_tracks'] = {
        'source': str(_preset_src),
        'preset_count': len(_presets),
        'expected_track_count': _expected_tracks,
    }
    # 三层判据 → 复用/打开/重建（可能要冷启动重开剪映）
    _ensure_draft(_adm_cfg, _presets)
    # 本轮起点快照：必须在任何点击/按键之前，失败退出时据此自动回滚
    _RUN_STATE['draft_dir'] = _admission_draft_dir(_adm_cfg)
    _RUN_STATE['draft_snapshot'] = _snapshot_draft(_RUN_STATE['draft_dir'])
    # 可能刚冷启动重开过，旧 win 句柄已失效：重新取当前编辑页窗口后再算 env
    win, _pt = connect_jianying(retry=False)
    if not win:
        raise RuntimeError('准入草稿已定位，但连接不到剪映窗口（可能刚退出/重开中）')
    # S1-2：env 统一取窗口所在屏。顺序依赖：建稿/打开（可能冷启动）之后才取，
    # 否则会绑到已被替换的旧窗口；默认主屏，S3-A2 --target-screen 时为指定屏。
    calib['env'] = ui_calibration.env_fingerprint(win)
    dprint(f"  env 取值（建稿/打开后，窗口所在屏）: screen={calib['env']['screen']} "
           f"dpi={calib['env']['dpi_scale']} jy={calib['env']['jianying_version']}")
    if _pt != 'edit':
        if _pt is None:
            dprint('  ⚠️ 页面类型无法判定（Qt 通用窗口类），假定已在编辑页继续')
        else:
            raise RuntimeError(f'草稿已定位但未进入编辑页（pt={_pt}）')

    # ---------- 起点布局快照（任何点击/按键之前，全屏/预设轨处理之前） ----------
    # 供 pipeline「命中即自检」：进入编辑页后立刻对账，确保起点状态与准入一致。
    if _reached('tracks'):
        from ui_verify import _scan_with_evidence
        _il_t0 = time.time()
        # 草稿刚打开时轨道懒加载（pipeline 侧 wait_timeline_ready 最多等 90s）：
        # 此处只读轮询，不发任何点击/按键，位置仍在全屏/预设轨处理之前。
        while time.time() - _il_t0 < 90:
            _rows_ready, *_ = scan_tracks(win)
            if _rows_ready:
                break
            time.sleep(2)
        _rows0, _img0, _wl0, _wt0, _ww0, _ex0, _tr0, _rrs0 = _scan_with_evidence(win)
        _icon_l, _icon_r = ui_track_ops._icon_offsets()
        calib['initial_layout'] = {
            'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            # 记录点是「进编辑页、轨道行刚可见时的快照」，与 tracks 阶段应校验的
            # 轨道总数不是同一口径：前者随懒加载/折叠变化，后者由预设组条数推导。
            'visible_row_count': len(_rrs0),
            'expected_track_count': _expected_tracks,
            'rows': [{'idx': i, 'type': rr['type'], 'ymid': int(rr['ymid']),
                      'hidden': bool(rr['hidden']),
                      'type_source': rr['type_source'],
                      'confidence': rr['confidence']}
                     for i, rr in enumerate(_rrs0)],
            'track_rect': list(_tr0) if _tr0 else None,
            'wl': int(_wl0), 'ww': int(_ww0),
            'scale': round(float(ui_track_ops._HDR_SCALE), 3),
            # x 锚点：屏幕绝对 px（字段名沿用 track_header 现有命名）
            'eye_x': int(_ex0),
            'lock_x': int(ui_track_ops.get_lock_x(_wl0, _ww0)),
            'icon_l_offset': int(ui_track_ops.header_abs_x(_wl0, _icon_l)),
            'icon_r_offset': int(ui_track_ops.header_abs_x(_wl0, _icon_r)),
        }
        dprint(f"  起点布局: 可见 {len(_rrs0)} 行 / 预期 {_expected_tracks} 条轨道 "
               f"rows={[(rr['type'], rr['ymid']) for rr in _rrs0]} "
               f"x=[eye={int(_ex0)} lock={calib['initial_layout']['lock_x']}] "
               f"耗时 {time.time() - _il_t0:.1f}s")

    # 流程开头强制重置全屏基准态：ESC 退出（清可能残留的全屏）→ Ctrl+F 进入
    # Ctrl+F 是 toggle，不先 ESC 归位就无法保证"当前一定在全屏"
    win, rect = enter_fullscreen(win, force_reset=True)
    calib['env']['fullscreen_rect'] = list(rect)
    calib['env']['quadrants'] = compute_quadrants(rect)
    calib['env']['rect_tolerance'] = 5
    dprint(f"  全屏基准态 rect={rect}  screen={calib['env']['screen']}")
    dprint("  象限: Q2=右上(Tab区)  Q4=右下(人声分离开关区)")
    _shot(win, '_admission_fullscreen.png')
    _mark('fullscreen')

    # ---------- 阶段 tracks：双重校验主轨 + 预设轨锁定隐藏 ----------
    if _reached('tracks'):
        dprint('\n[tracks] 重置全 visible → 双重校验主轨 → 预设轨 锁定+隐藏')
        win = ensure_fullscreen(win)
        _mark('tracks.ensure_fullscreen')
        unhide_all_tracks(win)                   # 用户规则：先排除遗留状态
        _mark('tracks.unhide_all')
        win = ensure_fullscreen(win)
        main, presets = hide_preset_tracks(win)
        _mark('tracks.hide_presets')
        if main is None:
            raise RuntimeError('主轨识别失败（无 video 行）')
        rows_chk, *_ = scan_tracks(win)
        if len(rows_chk) != _expected_tracks:
            raise RuntimeError(
                f'轨道数 {len(rows_chk)} != 预期 {_expected_tracks}'
                f'（主轨 1 + 预设 {len(_presets)} 条的 {_preset_src}；'
                f'请确认预设组配置与实际合并结果一致）')
        dprint(f"  轨道数校验通过: {len(rows_chk)} 条（预期 {_expected_tracks}"
               f" = 主轨 1 + 预设 {len(_presets)}）")
        stage_results['tracks'] = {'ok': True, 'duration_s': None, 'metrics': {
            'track_count': len(rows_chk), 'expected_track_count': _expected_tracks,
            'main_ymid': int(main['ymid']),
            'preset_tracks': len(presets)}}
        calib['main_track'] = {
            'method': 'dual',
            'm1': 'max_ymid_video',
            'm2': {'cover_x_range': list(MAIN_TRACK_LOGICAL['cover_x_range']),
                   'contrast_threshold': 15, 'min_cols': 3},
            'lock_before_hide': True,
            'main_ymid': int(main['ymid']),
        }
        # 眼睛列/图标列按「相对时间线左缘的逻辑偏移」落盘（跨分辨率/DPI 可复用）；
        # 会话缓存是物理偏移，需按当前 origin/scale 反算回逻辑值（S3-B1）。
        _sc = max(float(ui_track_ops._HDR_SCALE), 0.5)
        _eye_log = 74
        if ui_track_ops._EYE_X_CACHE:
            _eye_log = int(round((int(ui_track_ops._EYE_X_CACHE) - int(ui_track_ops._HDR_ORIGIN)) / _sc))
        if not (30 <= _eye_log <= 200):
            _eye_log = 74
        _icon_l, _icon_r = ui_track_ops._icon_offsets()
        calib['track_header'] = {
            'lock_x': MAIN_TRACK_LOGICAL['lock_x'], 'eye_x': _eye_log,
            'mute_x': MAIN_TRACK_LOGICAL['mute_x'], 'cover_x': MAIN_TRACK_LOGICAL['cover_x'],
            'eye_strip': list(MAIN_TRACK_LOGICAL['eye_strip']), 'blue_threshold': 8,
            'lock_strip': list(MAIN_TRACK_LOGICAL['lock_strip']),  # ⚠️ 锁定态像素签名待实测校准
            'icon_l_ratio': round(ui_track_ops.ICON_L_RATIO, 6),
            'icon_r_ratio': round(ui_track_ops.ICON_R_RATIO, 6),
            'icon_l_offset': round(float(_icon_l), 1),
            'icon_r_offset': round(float(_icon_r), 1),
            'main_seg_click_x': MAIN_TRACK_LOGICAL['main_seg_click_x'],
        }
        dprint(f"  主轨 ymid={main['ymid']}  预设轨 {len(presets)} 条已 锁定+隐藏")
        _shot(win, '_admission_tracks.png')

    # ---------- 阶段 select：Home 回首帧 + Ctrl+A ----------
    if _reached('select'):
        dprint('\n[select] Home 回首帧 → Ctrl+A（不点轨道，避免固定坐标）')
        win = ensure_fullscreen(win)
        op_mark('按键', 'Home（播放头回第一帧）')
        send_keys(win, '{Home}')                 # 播放头回第一帧（同时把焦点交给时间轴）
        time.sleep(0.4)
        op_mark('组合键', 'Ctrl+A（全选主轨片段）')
        pyautogui.hotkey('ctrl', 'a')
        time.sleep(1.0)                          # 等面板懒加载
        dprint('  已 Home + Ctrl+A 全选')
        _mark('select')

    # ---------- 阶段 audio_tab：Q2 点Tab + 滚到「人声分离」+ 面板校验 ----------
    audio_pos, switch = None, None
    if _reached('audio_tab'):
        op_stage('3 人声分离')
        dprint('\n[audio_tab] Q2 OCR 点「音频」Tab → 滚轮下滚看见完整人声分离 → 面板校验')
        from ocr_locate import find_text_in_window
        from ui_vocal_separate import scroll_until_vocal_visible, panel_is_audio
        win = ensure_fullscreen(win)
        _mark('audio.ensure_fullscreen')
        from ui_calibration import get_param
        from ui_vocal_separate import panel_is_audio as _is_audio
        WL, WT, WW, WH = get_window_rect(win)
        # ① OCR 实时定位（准入=校准器，不消费 JSON 坐标）：扫 Q2 顶部窄条（~0.6s）
        #    x 用实际窗口宽（WW//2, WW）；高度按窗口物理/逻辑实测比值换算（1920 下 = 120）
        from ui_recognize_translate import _region_scale
        _rs = _region_scale(win)
        hit = find_text_in_window(win, '音频',
                                  region=(WW // 2, 0, WW, int(round(120 * _rs))))
        if hit:
            audio_pos = (int(hit[0]), int(hit[1]))
            dprint(f'  Q2 音频Tab = {audio_pos}（OCR 命中）')
            from ui_calibration import update_param
            update_param('audio_tab', 'coord', [audio_pos[0], audio_pos[1]])
        else:
            # ② OCR 未命中才允许 JSON 兜底（仅救急，定位结果仍会校验+回写）
            coord = get_param('audio_tab', 'coord', None)
            if coord:
                audio_pos = (int(coord[0]), int(coord[1]))
                dprint(f'  ⚠️ Q2 音频Tab OCR 未命中，用 JSON 兜底 {audio_pos}')
            else:
                # 兜底 = 窗口右缘 - 逻辑偏移×dpi_scale（不再写死 1920 绝对点）
                _wr = get_window_rect(win) or (0, 0, 0, 0)
                _sc = ui_track_ops._ui_scale(win)
                audio_pos = (int(_wr[2]) - int(round(AUDIO_TAB_FALLBACK_FROM_RIGHT_LOGICAL[0] * _sc)),
                             int(_wr[1]) + int(round(AUDIO_TAB_FALLBACK_FROM_RIGHT_LOGICAL[1] * _sc)))
                dprint(f'  ⚠️ OCR 未命中音频Tab，用相对推导 {audio_pos}（scale={_sc}）')
        _mark('audio.locate_tab')

        op_mark('点击', f'音频Tab({audio_pos[0]},{audio_pos[1]})')
        pyautogui.click(*audio_pos)
        time.sleep(0.5)                      # 等面板切换（滚动本身也会触发懒加载）

        # 校验真的切到「音频」面板；否则横向微调重点
        if not _is_audio(win):
            _sc = ui_track_ops._ui_scale(win)
            for dx in [int(round(v * _sc)) for v in AUDIO_TAB_RETRY_DX_LOGICAL]:
                dprint(f'  ⚠️ 未切到音频面板，横移 {dx}px 重点 Tab')
                op_mark('点击', f'音频Tab 横移微调({int(audio_pos[0]) + dx},{int(audio_pos[1])})')
                pyautogui.click(int(audio_pos[0]) + dx, int(audio_pos[1]))
                time.sleep(0.5)
                win, _ = connect_jianying()
                if _is_audio(win):
                    audio_pos = (int(audio_pos[0]) + dx, int(audio_pos[1]))
                    break
            else:
                raise RuntimeError('点「音频」Tab 后仍未切到音频面板')
        _mark('audio.click_tab')

        # 切「基础」子Tab（人声分离在该子Tab下；找不到也不阻断，面板默认就在基础页）
        try:
            _click_panel_tab_by_desc(win, '基础')
        except Exception:
            pass
        _mark('audio.base_subtab')

        # 滚轮往下滚，直到真正看见完整「人声分离」
        win, found = scroll_until_vocal_visible(win, max_rounds=20)
        if not found:
            raise RuntimeError('滚动 20 轮仍未在右半看到「人声分离」')
        _mark('audio.scroll_to_vocal')
        # ⚠️ 已删除重复的 panel_is_audio 校验（click_tab 处已校验过一次）

    # ---------- 阶段 locate：定位人声分离对勾（默认停止点） ----------
    # 若目标停在 enable/wait，这一步的 OCR 是多余的（enable 自己会定位并落盘坐标）
    if _reached('locate') and stop_idx < STAGES.index('enable'):
        dprint('\n[locate] 窗口右半 OCR 定位「人声分离」对勾 → 停止等确认')
        from ui_vocal_separate import find_vocal_switch
        win = ensure_fullscreen(win)
        switch = find_vocal_switch(win)
        if switch:
            dprint(f'  ✅ 对勾坐标 = {switch}')
            shot = _shot(win, '_admission_locate.png')
            if shot:
                marked = _mark_point(shot, get_window_rect(win),
                                     [(switch[0], switch[1], 'switch')],
                                     os.path.join(OUT_DIR, '_admission_locate_marked.png'))
                if marked:
                    dprint(f'  📸 截图: _admission_locate_marked.png（红十字=对勾坐标）')
        else:
            dprint('  ⚠️ 右半未找到「人声分离」文字（面板可能未滚到底）')
        _mark('locate')

    # ---------- 记录点位（JSON 是点位唯一事实源） ----------
    if audio_pos:
        calib['audio_tab'] = {
            'quadrant': 'Q2', 'method': 'ocr', 'keyword': '音频',
            'coord': [int(audio_pos[0]), int(audio_pos[1])],
            'fallback': {'ref': 'window_right_top', 'dx': -452, 'dy': 55},
            'ocr_region': [WW // 2, 0, WW, 120],
            'verify': {'method': 'ocr', 'keyword': '人声分离', 'region': 'right_half'},
        }
    if switch:
        calib['vocal_switch'] = {
            'region': 'right_half', 'method': 'ocr', 'keyword': '人声分离',
            'checkbox_offset': list(VOCAL_CHECKBOX_OFFSET_LOGICAL),
            'switch_coord': [int(switch[0]), int(switch[1])],
            'blue_check': {'half': 15, 'color': [0, 193, 205], 'threshold': 40},
            'appear_timeout': 90,
        }

    # ---------- 默认停止点：locate ----------
    if stop_idx <= STAGES.index('locate'):
        _save_merged(calib)
        dprint('\n' + '=' * 60)
        dprint(f'[准入测试] ⏸️ 已到 stop_after={stop_after}，停止等肉眼确认')
        dprint('  状态保持原样：预设轨锁定+隐藏、面板停在音频页（未点开关）')
        dprint('  截图: _admission_tracks.png / _admission_audio_panel.png / _admission_locate_marked.png')
        dprint(f'  配置已写入 {ui_calibration.CALIB_PATH}')
        dprint('=' * 60)
        dprint(json.dumps(calib, ensure_ascii=False, indent=2))
        return calib

    # ---------- 阶段 enable：勾选人声分离 → 立即选下拉模式 ----------
    if _reached('enable'):
        dprint(f'\n[enable] 勾选「人声分离」（vocal_mode={vocal_mode}）')
        from ui_vocal_separate import ensure_vocal_on, select_vocal_mode
        win = ensure_fullscreen(win)
        enabled = ensure_vocal_on(win)
        calib.setdefault('vocal_switch', {})['enabled_verified'] = bool(enabled)
        calib['vocal_switch']['vocal_mode'] = vocal_mode
        dprint(f'  勾选验证 = {enabled}')
        _shot(win, '_admission_enabled.png')
        _mark('enable.check_switch')

        # 用户明确：勾选后立刻选下拉模式，再等分离完成；只有一次分离
        if vocal_mode != 'none' and enabled:
            label = '仅保留人声' if vocal_mode == 'vocal' else '仅保留背景声'
            dprint(f'  立即下拉选「{label}」')
            win, _ = connect_jianying()
            selected = select_vocal_mode(win, mode=vocal_mode)
            calib['vocal_switch']['mode_selected'] = bool(selected)
            dprint(f'  下拉选择 = {selected}')
            _shot(win, '_admission_mode_selected.png')
            _mark('enable.select_mode')

    # ---------- 阶段 wait：End 跳末帧 → 等这次分离完成（唯一一次） ----------
    if _reached('wait'):
        dprint('\n[wait] End 跳末帧 → 等分离完成')
        from ui_vocal_separate import wait_vocal_done
        win, _ = connect_jianying()
        win = ensure_fullscreen(win)     # 守卫：万一前面误退全屏，这里恢复基准态
        res = wait_vocal_done(win, timeout=600, appear_timeout=120)
        dprint(f'  分离等待结果: {res}')
        _mark('wait.separation_done')

        calib['processing_keywords'] = ['人声分离等待处理', '等待处理', '处理中', '正在处理']
        win, _ = connect_jianying()
        _shot(win, '_admission_processing_done.png')

    # 恢复轨道：一次扫描完成「解锁 + 恢复可见」（用户要求收尾必须结束锁定）
    # ⚠️ 仅当 wait 是最后一个阶段才恢复；后续还有 recognize/extract/export 时
    # 预设轨必须保持 锁定+隐藏（与 pipeline 状态机一致：识别+翻译 要求预设不可见，
    # 否则 Ctrl+A 全选带上预设片段、字幕轨簇被预设 text 轨污染 —— 用户实测定位的根因）
    if stop_idx <= STAGES.index('wait'):
        dprint('\n恢复所有轨道（解锁 → 可见，合并批量操作）')
        win, _ = connect_jianying()
        win = ensure_fullscreen(win)     # 守卫：收尾坐标必须在全屏基准态下
        from ui_track_ops import restore_all_tracks
        restore_all_tracks(win)
        _mark('cleanup')
    else:
        dprint('\n后续还有阶段：预设轨保持 锁定+隐藏（不恢复）')

    # ---------- 阶段 recognize：识别中文字幕 + 翻译英文（走通 pipeline [4]） ----------
    if _reached('recognize'):
        op_stage('4 识别中文字幕+翻译')
        dprint('\n[recognize] 识别+翻译字幕（对齐 pipeline [4]）')
        win = ensure_fullscreen(win)
        from ui_recognize_translate import (
            run_recognize_translate, resolve_recognize_defaults)
        # 只补磁盘缺失的键：recognize 段的实测点位由 run_recognize_translate 里
        # 的 update_param 实时落盘，磁盘 = 本轮最新事实源。这里若用模块常量整段
        # 重建，会把刚写盘的实测值覆盖成常量（历史 bug：translate_dropdown_center
        # 实测 132 → 被常量 300 打回）。setdefault 语义：已有值一律保留。
        _disk_rec = (ui_calibration.load_calibration() or {}).get('recognize') or {}
        _rec = calib.setdefault('recognize', {})
        # 兜底值按当前窗口原点 + 逻辑偏移×dpi_scale 运行时推导（不再注入 1920 绝对常量）
        _defaults = resolve_recognize_defaults(win)
        for _k, _v in _defaults.items():
            # 磁盘有 → 用磁盘（实测），否则才用常量
            _rec.setdefault(_k, _disk_rec.get(_k, list(_v)))
        # 磁盘上已有、但不在 _defaults 里的键（panel_entry_center 等）一并保留
        for _k, _v in _disk_rec.items():
            _rec.setdefault(_k, _v)
        r = run_recognize_translate(workdir=OUT_DIR)
        dprint(f"  recognize 结果: ok={r.get('ok')} error={r.get('error')}")
        if not r.get('ok'):
            raise RuntimeError(f"recognize 失败: {r.get('error')}")
        stage_results['recognize'] = {
            'ok': r.get('ok'), 'duration_s': r.get('duration_s'), 'error': r.get('error'),
            'metrics': {k: r.get(k) for k in ('text_tracks_delta', 'english_texts_added',
                                             'translate_lang_ok') if k in r}}
        _mark('recognize')

    # ---------- 阶段 extract：分离音频 + 字幕轨收尾（走通 pipeline [5]） ----------
    if _reached('extract'):
        dprint('\n[extract] 分离音频+收尾（对齐 pipeline [5]）')
        win = ensure_fullscreen(win)
        from ui_extract_audio import run_extract_audio
        r = run_extract_audio(workdir=OUT_DIR)
        dprint(f"  extract 结果: ok={r.get('ok')} error={r.get('error')}")
        if not r.get('ok'):
            raise RuntimeError(f"extract 失败: {r.get('error')}")
        stage_results['extract'] = {
            'ok': r.get('ok'), 'duration_s': r.get('duration_s'), 'error': r.get('error'),
            'metrics': {k: v for k, v in r.items()
                        if k not in ('ok', 'duration_s', 'error', 'inputs')}}
        _mark('extract')

    # ---------- 阶段 export：导出 MP4（走通 pipeline [6]） ----------
    if _reached('export'):
        op_stage('8 导出')
        dprint('\n[export] 导出 MP4（对齐 pipeline [6]）')
        win = ensure_fullscreen(win)
        from ui_pipeline import step_export
        r = step_export(OUT_DIR)
        dprint(f"  export 结果: ok={r.get('ok')} error={r.get('error')}")
        if not r.get('ok'):
            raise RuntimeError(f"export 失败: {r.get('error')}")
        stage_results['export'] = {
            'ok': r.get('ok'), 'duration_s': r.get('duration_s'), 'error': r.get('error'),
            'metrics': {'cmd': (r.get('inputs') or {}).get('cmd')}}

    _save_merged(calib)
    _dump_report(ui_param_log, stage_results, calib, _marks, final=True)
    dprint('\n' + '=' * 60)
    dprint(f'[准入测试] ✅ 完成  耗时 {int(time.time() - t0)}s')
    _print_timing(_marks)
    stage_timeline()
    dprint(f'配置 → {ui_calibration.CALIB_PATH}')
    dprint('=' * 60)
    dprint(json.dumps(calib, ensure_ascii=False, indent=2))
    return calib


def _save_merged(calib):
    """
    保存校准结果：把磁盘上已经由 update_param 落盘的实测点位合并进 calib，
    避免 save_calibration(calib) 用内存字典覆盖掉跑过程中写入的坐标
    （历史 bug：switch_click_coord / arrow_dx / list_dy 全被覆盖丢失）。
    """
    disk = ui_calibration.load_calibration() or {}
    for sec, val in disk.items():
        if sec.startswith('_'):
            continue
        if sec in calib and isinstance(calib[sec], dict) and isinstance(val, dict):
            if sec == 'recognize':
                # recognize 段：运行期 OCR 实测由 update_param 实时落盘，磁盘是最新
                # 事实源；内存只是本轮起点副本（可能是上一轮的旧常量/旧坐标）。
                # 用磁盘覆盖内存，避免把本轮实测值压回起点值（历史 bug：132→300）。
                calib[sec].update(val)
            else:
                for k, v in val.items():
                    calib[sec].setdefault(k, v)      # 内存值优先，缺失的从磁盘补齐
        elif sec not in calib:
            calib[sec] = val
    # 运行期实测快照：每次跑都会变，标注出来避免被当成「可复用点位」
    for sec in ('main_track', 'subtitle_tracks', 'final_layout', 'initial_layout'):
        if isinstance(calib.get(sec), dict):
            calib[sec]['snapshot'] = True
    ui_calibration.save_calibration(calib)
    # 合并后打印每个 section 的键，便于肉眼确认「记录了什么」
    for sec, val in calib.items():
        if isinstance(val, dict) and sec not in ('env',):
            dprint(f"  [json] {sec}: {sorted(val.keys())[:8]}")


def _print_timing(marks):
    """打印分段耗时表：每段 = 相邻两个打点的间隔"""
    if not marks:
        return
    _start = marks[0][1]
    dprint('\n' + '-' * 56)
    dprint(f'  {"步骤":<28} {"本段":>9} {"累计":>9}')
    dprint('-' * 56)
    prev_t = None
    for name, t in marks:
        seg = (t - prev_t) if prev_t is not None else 0.0
        dprint(f'  {name:<28} {seg:>8.2f}s {t - _start:>8.2f}s')
        prev_t = t
    dprint('-' * 56)


def _finalize_jianying(stop_after='export', completed=False,
                       keep_window=False, close_window=False):
    """准入收尾：按「是否跑完全部阶段 + 显式开关 + close_on_exit」决定留窗还是关闭。

    归属口径：只有 ui_nav.JIANYING_LAUNCHED=True（本轮确实拉起过）才默认接管；
    复用的用户实例不关闭、不清理，除非显式 --close-window。
    保留窗口：不关闭、不清临时文件（中途停点就是给人肉眼确认的）。
    关闭窗口：先回首页再正常关闭（避免草稿打开态触发保存弹窗），剪映自删 .locked
    并落盘 autosave，随后清掉 template*.tmp / draft_content.json.bak 等残留。
    """
    import ui_nav
    launched = getattr(ui_nav, 'JIANYING_LAUNCHED', False)
    if not launched and not close_window:
        dprint('[收尾] 窗口策略：保留窗口（本轮未拉起剪映，不接管用户实例）')
        return
    close_on_exit = True
    draft_dir = None
    try:
        cfg = _load_admission_cfg()
        close_on_exit = bool(cfg['open'].get('close_on_exit', True))
        draft_dir = _admission_draft_dir(cfg)
    except SystemExit as e:
        if not close_window:
            print(f'[warn] 收尾读取 admission 配置失败，跳过关闭/清理: {e}')
            return
        print(f'[warn] 收尾读取 admission 配置失败，仅关闭剪映、跳过临时文件清理: {e}')

    completed_all = bool(completed) and stop_after == STAGES[-1]
    action, reason = decide_window_action(
        completed_all, stop_after, close_on_exit,
        keep_window=keep_window, close_window=close_window)
    if action == 'keep':
        dprint(f'[收尾] 窗口策略：{reason}；不关闭、不清临时文件')
        return
    dprint(f'[收尾] 窗口策略：关闭窗口（{reason}）')
    # 先退编辑页回首页再关：避免草稿打开态下关闭触发保存/恢复弹窗，也便于剪映自行落盘
    try:
        ui_nav.switch_to_home(max_wait=15)
    except Exception as e:
        dprint(f'  [warn] 收尾回首页失败（忽略，继续关闭）: {e}')
    if not ui_nav.close_jianying():
        dprint('  ⚠️ 剪映未能关闭，跳过草稿临时文件清理')
        return
    if not draft_dir:
        return
    from pipeline import cleanup_draft_temp_files
    removed = cleanup_draft_temp_files(draft_dir)
    dprint(f'  🧹 草稿临时文件清理: {len(removed)} 个')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='准入测试（分阶段校准）')
    ap.add_argument('--stop-after', default='export', choices=STAGES,
                    help='跑到哪个阶段停（默认 export：全程跑完含导出）')
    ap.add_argument('--vocal-mode', default='none', choices=['none', 'vocal', 'background'],
                    help='二次分离模式：none=不选下拉（默认），vocal=仅保留人声，background=仅保留背景声')
    ap.add_argument('--target-screen', default=None,
                    help='目标屏：primary|secondary|<index>（S3-A2；不给=主屏，现状）')
    _win_group = ap.add_mutually_exclusive_group()
    _win_group.add_argument('--keep-window', action='store_true',
                            help='收尾强制保留剪映窗口（不关闭、不清临时文件；覆盖默认决策）')
    _win_group.add_argument('--close-window', action='store_true',
                            help='收尾强制关闭剪映窗口（中途停点也关；覆盖默认决策）')
    args = ap.parse_args()
    from ui_core import install_timestamp_stdout
    install_timestamp_stdout()   # 所有 print（含 init_draft 阶段）统一带墙钟毫秒前缀
    completed = False
    try:
        calibrate(stop_after=args.stop_after, vocal_mode=args.vocal_mode,
                  target_screen=args.target_screen)
        completed = True
    except Exception as e:
        _RUN_STATE['error'] = str(e)
        # 失败兜底：尽量退出编辑页回首页（草稿留在编辑页会阻塞下次热复用，也不便人工检查）
        try:
            from ui_core import dprint
            from ui_nav import switch_to_home
            dprint("...准入失败，尝试退出编辑页回首页...")
            switch_to_home(max_wait=15)
            dprint("  ✅ 已回首页")
        except Exception as e:
            print(f"[warn] 回首页失败（忽略）: {e}")
        raise
    finally:
        if not completed:
            # 失败/中断也落正式报告（带 failed_stage / failure_class），partial 不再是唯一记录
            _dump_report(_RUN_STATE['ui_params'], _RUN_STATE['results'],
                         _RUN_STATE['calib'], _RUN_STATE['marks'],
                         final=True, error=_RUN_STATE.get('error') or '准入未跑完')
        _finalize_jianying(stop_after=args.stop_after, completed=completed,
                           keep_window=args.keep_window, close_window=args.close_window)
        if not completed:
            # 失败/中断：剪映已按收尾策略处理完，再用本轮起点快照回滚草稿，
            # 不把跑脏的草稿留在磁盘上等人工还原
            _rollback_draft(_RUN_STATE.get('draft_snapshot'),
                            _RUN_STATE.get('draft_dir'))
