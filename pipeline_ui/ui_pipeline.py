# -*- coding: utf-8 -*-
"""
ui_pipeline.py - 纯 UI 自动化版 pipeline 主入口
=================================================
CLI 调用：
  py -3.12 pipeline_ui/ui_pipeline.py --workdir workdir
  py -3.12 pipeline_ui/ui_pipeline.py --workdir workdir --only vocal_separate
  py -3.12 pipeline_ui/ui_pipeline.py --workdir workdir --from-step vocal_separate
  py -3.12 pipeline_ui/ui_pipeline.py --resume workdir/_state.json   # 断点续跑

完整流程（⚠️ 跑本 pipeline 前必须先跑准入 admission_test.py 全流程走通）：
  [P] 校准初检           step_precheck —— ui_calibration.json 完整性 + 环境指纹，
                          不合格直接阻断（提示重跑准入）
  [0] 初始化草稿          init_draft（重建后预设轨全部恢复可见）
  [1] 预设合并 (不涉及 UI, 原 JSON pipeline) — 占位
  [2] 时间线裁剪 (不涉及 UI) — 占位
  [3] 人声分离            ui_vocal_separate.run_vocal_separate()
                          开头 hide_preset_tracks 锁定+隐藏预设轨；
                          成功后【保持预设隐藏不恢复】（恢复会污染后续步骤）
  [4] 识别+翻译字幕       ui_recognize_translate.run_recognize_translate()
                          前置防御：无字幕轨时确保预设已 锁定+隐藏
  [5] 分离音频+收尾+终检  ui_extract_audio.run_extract_audio()
                          锁定+隐藏两条字幕轨 → Ctrl+Shift+S → 验证新音频轨
                          → restore_all_tracks 解除全部锁定/隐藏（唯一恢复点）
                          → 只隐藏中文字幕轨 → 最终布局落盘 → ui_verify.run_verify()
  [6] 导出 MP4           robust_auto_export.py
  耗时统计 → workdir/_timings.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

# 确保能 import pipeline 下的所有模块（pipeline.py 需把它所在目录加进来）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'pipeline')))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, 'lib'))
sys.path.insert(0, os.path.join(
    _ROOT, '.trae', 'skills', 'jianying-editor', 'examples'))

import export_lock
from jy_instance import JyInstance
from no_window import hidden_child_env
import ui_calibration
from ui_core import dprint, connect_jianying, op_timeline, take_op_marks


# ================= 步骤注册表 =================
STEP_ORDER = [
    ('precheck',        '[P] 校准初检',           'step_precheck'),
    ('init_draft',      '[0] 初始化草稿',         'step_init_draft'),
    ('presets',         '[1] 预设合并',          'step_presets'),
    ('cut_timeline',    '[2] 时间线裁剪',         'step_cut_timeline'),
    ('vocal_separate',  '[3] 人声分离',           'step_vocal_separate'),
    ('recognize_translate', '[4] 识别+翻译字幕',  'step_recognize_translate'),
    ('finalize',        '[5] 分离音频+收尾+终检',  'step_finalize'),
    ('export',          '[6] 导出 MP4',           'step_export'),
]


# ================= 工具 =================
def _timings_path(workdir):
    return os.path.join(workdir, '_timings.jsonl')


def _state_path(workdir):
    return os.path.join(workdir, '_state.json')


def append_timing(workdir, step_key, step_label, result, impl=None):
    """落盘一步的执行记录（UI 入参由准入测试统一记录，这里只记调用与耗时）。"""
    path = _timings_path(workdir)
    record = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'step': step_key,
        'label': step_label,
        'impl': impl,
        'inputs': result.get('inputs'),
        'placeholder': bool(result.get('placeholder')),
        'ok': result.get('ok', False),
        'duration_s': result.get('duration_s', 0),
        'error': result.get('error'),
    }
    os.makedirs(workdir, exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    dprint(f"  📊 耗时 {record['duration_s']:.1f}s → {path}")


def save_state(workdir, completed_steps):
    path = _state_path(workdir)
    state = {
        'completed': list(completed_steps),
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_state(workdir):
    path = _state_path(workdir)
    if not os.path.exists(path):
        return {'completed': []}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ================= 超时自适应（S1-5：基线改为 S0 全绿实测，不再沿用准入比率） =================
# 超时 = 实测比率(阶段耗时/草稿时长) × 当前草稿时长 × 130%
# 口径说明（重要）：
#   vocal/recognize 的 timeout_sec 实际只包步骤内层等待段（run_vocal_separate [5/5]、
#   run_recognize_translate [wait]），而实测取 step 端到端（_timings.jsonl 口径，含面板
#   操作）——刻意偏松：内层等待 + 面板操作叠加时不会出现「预算比实测紧」的假超时。
# 归属核对（S1-5 澄清）：84s=识别等待(recognize ratio 0.117)，87s=人声分离(vocal ratio
#   0.122)，归属无错配；问题是 vocal 比率沿自准入内层口径，对整步偏紧 37%（S0 实测
#   step 端到端 132.9~149.6s vs 预算 87s）。
ADMISSION_REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_admission_report.json')
_TIMEOUT_MARGIN = 1.3
# 主基线：2026-11-03 S0 两轮全绿实测（551.77s 草稿，_timings.jsonl step 端到端口径）
_S0_RATIO = {
    'vocal_separate': 149.6 / 551.77,      # 两轮 146.2/149.6 取 max（替代准入 0.122 贴边值）
    # 识别基线：5 集 551.77s → 64.5s 只对短草稿成立。2026-11-09 全量 80 集/7269s 实测：
    #   13:18:17 点开始识别 → 13:53:11 draft_content.json 落盘（2608 条字幕）= 2100s，
    #   即 ~0.289 比率（是 5 集基线的 2.47 倍），且此后还要跑英译。
    #   旧值 0.117 在 80 集上只能给 1105s，必然在识别中途假超时（run6 实况）。
    #   取 4200/7269≈0.578（= 实测识别时间 ×2，给英译阶段留等量预算），×1.3 后 ≈ 5463s。
    'recognize_translate': 4200.0 / 7269.0,
    'export': 311.9 / 551.77,              # 两轮 295.3/328.5 取均值
}
_EXPORT_JITTER = 0.10   # 导出两轮实测抖动 ≈10%（295.3s vs 328.5s），显式计入
# 兜底基线（2026-10-30 准入实测 551.77s 草稿：分离 70.7s / 识别 53.6s / 导出 563.1s）
_FALLBACK_RATIO = {'vocal_separate': 0.128, 'recognize_translate': 0.097, 'export': 1.021}


def _admission_ratios():
    """从准入报告算各阶段耗时基线（阶段耗时/视频总时长，s/s）；失败返回 {}。
    分离取时间线 enable.select_mode → wait.separation_done 的间隔。"""
    try:
        with open(ADMISSION_REPORT, encoding='utf-8') as f:
            rep = json.load(f)
        cmd = ((rep.get('stages') or {}).get('export') or {}).get('metrics', {}).get('cmd') or []
        base = None
        if '--expect-duration' in cmd:
            base = float(cmd[cmd.index('--expect-duration') + 1])
        if not base:
            return {}
        out = {}
        for key, stage in (('recognize_translate', 'recognize'), ('extract', 'extract'),
                           ('export', 'export')):
            d = ((rep.get('stages') or {}).get(stage) or {}).get('duration_s')
            if d:
                out[key] = float(d) / base
        tl = {m.get('step'): m.get('t_s') for m in rep.get('timeline') or []}
        if tl.get('enable.select_mode') is not None and tl.get('wait.separation_done') is not None:
            out['vocal_separate'] = (tl['wait.separation_done'] - tl['enable.select_mode']) / base
        return out
    except Exception:
        return {}


def _step_timeout(step_key, duration_s):
    """超时 = 基线比率 × 当前草稿时长 × 130%（S1-5：S0 实测 > 准入报告 > 硬兜底）。
    导出额外乘 (1+_EXPORT_JITTER) 显式计入实测抖动。
    草稿时长或基线取不到时直接抛错（草稿读不了说明流水线本身已异常）。"""
    import math
    ratio = (_S0_RATIO.get(step_key) or _admission_ratios().get(step_key)
             or _FALLBACK_RATIO.get(step_key))
    if not duration_s or not ratio:
        raise ValueError(f'无法计算 {step_key} 超时：'
                         f'草稿时长={duration_s!r} 基线={ratio!r}（需先成功建草稿/准入报告在位）')
    if step_key == 'export':
        ratio *= (1 + _EXPORT_JITTER)
    return math.ceil(ratio * float(duration_s) * _TIMEOUT_MARGIN)


def _draft_duration_s():
    """当前打开草稿的时长（秒），失败返回 None。
    候选名依次尝试：活动草稿名（可能陈旧，如 default）→ init_draft 默认草稿名；
    取第一个「文件夹存在且时长>0」的结果；duration 字段缺失时按主轨片段求和。"""
    try:
        from ui_recognize_translate import DRAFT_ROOT, _active_draft_name
        try:
            candidates = [_active_draft_name()]
        except Exception:
            candidates = []
        from init_draft import DRAFT_NAME
        candidates.append(DRAFT_NAME)
        for name in filter(None, candidates):
            try:
                with open(os.path.join(DRAFT_ROOT, name, 'draft_content.json'), encoding='utf-8') as f:
                    d = json.load(f)
            except Exception:
                continue
            dur = float(d.get('duration') or 0)
            if dur <= 0:
                for tr in d.get('tracks') or []:
                    if tr.get('type') == 'video':
                        dur = sum(float((s.get('target_timerange') or {}).get('duration') or 0)
                                  for s in tr.get('segments') or [])
                        break
            if dur > 0:
                return round(dur / 1e6, 2)
        return None
    except Exception:
        return None


# ================= 步骤实现 =================
def step_precheck(workdir):
    """[P] 校准初检：先 pick_calibration(窗口所在屏) 确认命中正确环境 → 再跑完整性校验。
    多屏/副屏场景下必须用 win 取所在屏的分辨率，不能用主屏的 GetSystemMetrics。"""
    from ui_calibration import validate_calibration, pick_calibration
    from ui_core import connect_jianying
    t0 = time.time()

    # 先拿当前剪映窗口（不管首页/编辑页），用于识别窗口所在屏
    win, _pt = connect_jianying(retry=False, max_wait=15)
    if win:
        ui_calibration.set_current_win(win)
    calib, info = pick_calibration(win)
    if calib is None:
        dprint(f"  ❌ 未找到匹配 calibration ({info})")
        if str(info).startswith('no_calibration'):
            dprint("  👉 请先跑准入: py -3.12 pipeline_ui\\admission_test.py --init "
                   "--stop-after export --vocal-mode vocal")
        else:
            # env_mismatch：有文件但环境读数对不上——先查窗口放置/环境变化，不引导重跑准入
            dprint("  👉 env 不匹配 ≠ 缺校准：确认窗口已放置主屏后重试；"
                   "确认环境真变了（换屏/换 DPI/换剪映版本）才重跑准入")
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': f'calibration 未命中: {info}'}

    # validate_calibration 内部会 load_calibration() —— 现在多文件优先，自动命中
    r = validate_calibration(calib=calib)
    for item in r['info']:
        dprint(f"  ℹ️ {item}")
    for w in r['warnings']:
        dprint(f"  ⚠️ {w}")
    if r['ok']:
        dprint(f"[P] 校准初检 ✅  calibration 完整（{len(r['warnings'])} 条警告）")
        return {'ok': True, 'duration_s': round(time.time() - t0, 1), 'error': None,
                'inputs': {'warnings': r['warnings'], 'calib_path': info}}
    for e in r['errors']:
        dprint(f"  ❌ {e}")
    dprint("  👉 请重跑准入: py -3.12 pipeline_ui\\admission_test.py --init "
           "--stop-after export --vocal-mode vocal")
    return {'ok': False, 'duration_s': round(time.time() - t0, 1),
            'error': '；'.join(r['errors']), 'inputs': {'errors': r['errors']}}


def step_init_draft(workdir, draft_name=None, video_dir=None, video_limit=None,
                    video_range=None, cut_s=None, normalize=False, preset_profile=None):
    """[0] 初始化草稿：创建/重置 → 注入视频 → 合并预设 → 发布 → 打开。
    video_limit=None 表示全部集数；传 N 表示取前 N 集。
    normalize=False 直接引用原始视频（跳过逐集重编码）。
    preset_profile 选 presets.json 里的 presets_<组名>。"""
    from init_draft import init_draft
    t0 = time.time()
    kwargs = {'video_limit': video_limit, 'video_range': video_range,
              'cut_s': cut_s, 'normalize': normalize,
              'preset_profile': preset_profile}
    if draft_name:
        kwargs['draft_name'] = draft_name
    if video_dir:
        kwargs['video_dir'] = video_dir
    r = init_draft(**kwargs)
    return {'ok': r['ok'], 'duration_s': round(time.time() - t0, 1), 'error': r.get('error')}


def step_presets(workdir):
    """[1] 占位：预设合并走 JSON pipeline（实际已在 [0] init_draft 内完成），不涉及 UI。"""
    dprint("[1] 预设合并 (占位 — 已在 [0] 内完成，无 UI 操作)")
    return {'ok': True, 'duration_s': 0, 'error': None, 'placeholder': True}


def step_cut_timeline(workdir, cut_s=None):
    """[2] 时间线裁剪 — 占位。裁剪已在 [0] init_draft 发布前完成（cut_s 透传）。

    原因：旧方案在本步直接改盘上 draft_content.json，但剪映正打开着该草稿
    （[0] 打开的），人声分离/识别触发的剪映保存会用内存里的未裁剪时间线把
    改动覆盖掉（run14 实测：1168.89s 被覆盖回 1188.89s）。发布前裁剪让剪映
    一打开就是裁好的稿，且天然规避重复裁剪。"""
    t0 = time.time()
    dprint(f"[2] 占位 — 裁剪已在 [0] 发布前完成（cut_s={cut_s}），无 UI 操作")
    return {'ok': True, 'duration_s': round(time.time() - t0, 1), 'error': None,
            'inputs': {'cut_s': cut_s, 'skipped': True, 'moved_to_init': True}}


def step_vocal_separate(workdir, vocal_mode='none'):
    from ui_vocal_separate import run_vocal_separate
    timeout = _step_timeout('vocal_separate', _draft_duration_s())
    dprint(f"  分离等待超时（按草稿时长×基线×130%）：{timeout}s")
    return run_vocal_separate(workdir=workdir, vocal_mode=vocal_mode, timeout_sec=timeout)


def step_recognize_translate(workdir):
    from ui_recognize_translate import run_recognize_translate
    timeout = _step_timeout('recognize_translate', _draft_duration_s())
    dprint(f"  识别等待超时（按草稿时长×基线×130%）：{timeout}s")
    return run_recognize_translate(workdir=workdir, timeout_sec=timeout)


def step_finalize(workdir):
    """[5] 分离音频（含字幕轨锁定/隐藏、收尾恢复、只隐藏中文轨、布局落盘）→ UI 终检。"""
    from ui_extract_audio import run_extract_audio
    from ui_verify import run_verify

    t0 = time.time()

    r1 = run_extract_audio(workdir=workdir)
    if not r1.get('ok'):
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': f"extract_audio: {r1.get('error')}"}

    r3 = run_verify(workdir=workdir)
    ok = bool(r3.get('ok'))
    return {'ok': ok, 'duration_s': round(time.time() - t0, 1),
            'error': None if ok else 'verify failed'}


def step_export(workdir, draft_name=None, resolution=None):
    """导出 MP4。调用 .trae/skills/jianying-editor/examples/robust_auto_export.py。
    其 CLI: robust_auto_export.py <draft_name> <output_path> [--move] [--retries N] [--resolution X]"""
    import subprocess
    t0 = time.time()

    if not export_lock.acquire(owner='pipeline_ui export'):
        dprint("  ⚠️ 全局导出锁被占用，取消本次导出")
        return {'ok': False, 'duration_s': round(time.time()-t0, 1), 'error': 'export_lock_busy'}
    proc = None

    def _cleanup_export_process():
        """返回 (首个清理中断, 包装进程已停止)；中断时也继续尝试后续清理。"""
        if proc is None:
            return None, True
        cleanup_error = None
        try:
            if proc.poll() is not None:
                return None, True
        except Exception as e:
            dprint(f"  [warn] 读取导出进程状态失败，按仍在运行清理: {e}")
        except BaseException as e:
            cleanup_error = e
        try:
            JyInstance().kill()
        except Exception as e:
            dprint(f"  [warn] 停止剪映失败: {e}")
        except BaseException as e:
            if cleanup_error is None:
                cleanup_error = e
        try:
            proc.kill()
        except Exception as e:
            dprint(f"  [warn] 终止导出包装进程失败: {e}")
        except BaseException as e:
            if cleanup_error is None:
                cleanup_error = e
        try:
            proc.communicate()
        except Exception as e:
            dprint(f"  [warn] 回收导出包装进程失败: {e}")
        except BaseException as e:
            if cleanup_error is None:
                cleanup_error = e
        try:
            stopped = proc.poll() is not None
        except Exception as e:
            dprint(f"  [warn] 无法确认导出包装进程已停止，保留导出锁: {e}")
            stopped = False
        except BaseException as e:
            if cleanup_error is None:
                cleanup_error = e
            stopped = False
        return cleanup_error, stopped

    try:
        export_script = os.path.join(
            _ROOT, '.trae', 'skills', 'jianying-editor', 'examples', 'robust_auto_export.py'
        )
        if not os.path.exists(export_script):
            dprint(f"  ⚠️ 找不到 {export_script}，导出失败")
            return {'ok': False, 'duration_s': round(time.time()-t0, 1), 'error': 'script_not_found'}

        if not draft_name:
            try:
                from ui_recognize_translate import _active_draft_name
                draft_name = _active_draft_name()
            except Exception:
                draft_name = None
        if not draft_name:
            return {'ok': False, 'duration_s': round(time.time()-t0, 1), 'error': 'no_draft_name'}

        os.makedirs(workdir, exist_ok=True)
        output_path = os.path.join(os.path.abspath(workdir), f'{draft_name}.mp4')

        # 草稿时长：传给导出脚本做「成品不得短于草稿 2s」的校验
        expected_s = None
        try:
            from ui_recognize_translate import DRAFT_ROOT
            with open(os.path.join(DRAFT_ROOT, draft_name, 'draft_content.json'), encoding='utf-8') as f:
                expected_s = round(float(json.load(f).get('duration') or 0) / 1e6, 2) or None
        except Exception as e:
            dprint(f"  [warn] 读草稿时长失败，导出不做时长校验: {e}")

        cmd = [sys.executable, export_script, draft_name, output_path, '--move']
        if expected_s:
            cmd += ['--expect-duration', str(expected_s)]
        if resolution:
            cmd += ['--resolution', resolution]
        timeout = _step_timeout('export', expected_s)
        dprint(f"  导出草稿「{draft_name}」→ {output_path}（时长 {expected_s}s，"
               f"分辨率 {resolution or '沿用剪映当前值'}，导出后搬回 workdir，超时 {timeout}s）")

        try:
            # 清理上一轮导出残留（否则首读体积=旧产物，会骗过「已写盘」看门狗、干扰进度判读）
            _tmp_root = os.path.join(os.environ.get('USERPROFILE', ''),
                                     'Videos', '.__jianying_export_temp_folder__')
            if os.path.isdir(_tmp_root):
                import shutil
                shutil.rmtree(_tmp_root, ignore_errors=True)
                dprint("  已清理导出临时目录残留")
            # 同理删除旧成品（同路径上轮产物会污染体积读数）
            try:
                if os.path.exists(output_path):
                    os.remove(output_path)
                    dprint("  已删除旧成品文件")
            except OSError:
                pass

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding='utf-8', errors='replace',
                stdin=subprocess.DEVNULL,   # 避免脚本失败路径 input() 在无 tty 时 EOFError
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env=hidden_child_env(),
            )

            def _sizes_mb():
                """(临时目录体积, 成品体积) MB——分开统计，防旧文件污染判读"""
                tmp = 0
                for dp, _d, files in os.walk(_tmp_root):
                    for fn in files:
                        try:
                            tmp += os.path.getsize(os.path.join(dp, fn))
                        except OSError:
                            pass
                try:
                    out = os.path.getsize(output_path)
                except OSError:
                    out = 0
                return tmp / 1e6, out / 1e6

            last_tmp = 0.0
            deadline = t0 + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    dprint(f"[6] 导出 MP4 ❌  超时 {timeout}s")
                    return {'ok': False, 'duration_s': round(time.time()-t0, 1),
                            'error': f'export_timeout_{timeout}s', 'inputs': {'cmd': cmd}}
                try:
                    proc.wait(timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    tmp_mb, out_mb = _sizes_mb()
                    delta = tmp_mb - last_tmp
                    last_tmp = tmp_mb
                    dprint(f"  ...导出中（已 {time.time()-t0:.0f}s）"
                           f"临时 {tmp_mb:.0f}MB / 成品 {out_mb:.0f}MB"
                           + (f"（临时新增 {delta:.0f}MB，写盘中）" if delta > 0.5
                              else "（临时无新增，准备阶段/未写盘）"))

            out, err = proc.communicate()
            elapsed = time.time() - t0
            if proc.returncode == 0:
                dprint(f"[6] 导出 MP4 ✅  耗时 {elapsed:.1f}s")
                return {'ok': True, 'duration_s': round(elapsed, 1), 'error': None,
                        'inputs': {'cmd': cmd}}
            else:
                dprint(f"[6] 导出 MP4 ❌  返回码 {proc.returncode}")
                dprint(f"  stderr: {(err or '')[-500:]}")
                return {'ok': False, 'duration_s': round(elapsed, 1), 'error': (err or '')[-300:],
                        'inputs': {'cmd': cmd}}
        except Exception as e:
            elapsed = time.time() - t0
            dprint(f"[6] 导出 MP4 ❌  异常: {e}")
            return {'ok': False, 'duration_s': round(elapsed, 1), 'error': str(e),
                    'inputs': {'cmd': cmd}}
    finally:
        cleanup_error, stopped = _cleanup_export_process()
        if stopped:
            export_lock.release()
        if cleanup_error is not None:
            raise cleanup_error


STEP_IMPL = {
    'precheck': step_precheck,
    'init_draft': step_init_draft,
    'presets': step_presets,
    'cut_timeline': step_cut_timeline,
    'vocal_separate': step_vocal_separate,
    'recognize_translate': step_recognize_translate,
    'finalize': step_finalize,
    'export': step_export,
}


def step_kwargs(step_key, workdir, vocal_mode, draft_name, video_dir, cut_s=None,
                video_limit=None, video_range=None, normalize=False, export_resolution=None,
                preset_profile=None):
    """每个步骤的实参（调度统一 impl(**kw)）。"""
    if step_key == 'init_draft':
        return {'workdir': workdir, 'draft_name': draft_name, 'video_dir': video_dir,
                'video_limit': video_limit, 'video_range': video_range,
                'cut_s': cut_s, 'normalize': normalize, 'preset_profile': preset_profile}
    if step_key == 'vocal_separate':
        return {'workdir': workdir, 'vocal_mode': vocal_mode}
    if step_key == 'cut_timeline':
        return {'workdir': workdir, 'cut_s': cut_s}
    if step_key == 'export':
        return {'workdir': workdir, 'draft_name': draft_name, 'resolution': export_resolution}
    return {'workdir': workdir}


# ================= 主流程 =================
def _parse_only(only):
    """--only 多值解析（S1-4）：支持逗号分隔（--only a,b）与多次传入（--only a --only b）。
    返回去重后的 step_key 列表；非法名抛 ValueError 并列出合法值。"""
    if not only:
        return []
    items = only if isinstance(only, (list, tuple)) else [only]
    out = []
    for it in items:
        for k in str(it).split(','):
            k = k.strip()
            if k and k not in out:
                out.append(k)
    valid = [k for k, _l, _f in STEP_ORDER]
    bad = [k for k in out if k not in valid]
    if bad:
        raise ValueError(f'--only 未知步骤: {bad}；合法值: {valid}')
    return out


def run_pipeline_ui(workdir: str,
                    only: str = None,
                    from_step: str = None,
                    resume: bool = False,
                    skip: list = None,
                    vocal_mode: str = 'none',
                    draft_name: str = None,
                    video_dir: str = None,
                    cut_s: float = None,
                    video_limit: int = None,
                    video_range: str = None,
                    normalize: bool = False,
                    screen_policy: str = 'primary',
                    preset_profile: str = None,
                    export_resolution: str = None):
    """
    执行纯 UI 自动化 pipeline。

    Args:
        workdir: 工作目录（存放截图、耗时统计、状态文件）
        only: 只跑这一个步骤
        from_step: 从这个步骤开始跑（跳过前面的）
        resume: 断点续跑（从 state.json 里已完成的步骤之后开始）
        skip: 要跳过的步骤名列表
    """
    os.makedirs(workdir, exist_ok=True)
    skip = set(skip or [])

    # S3-A2：窗口放置策略（必须在任何 connect_jianying 之前设置）
    from ui_core import set_screen_policy
    set_screen_policy(screen_policy)

    # 断点续跑
    completed = []
    if resume:
        state = load_state(workdir)
        completed = state.get('completed', [])
        dprint(f"🔄 断点续跑: 已完成 = {completed}")

    # 决定要跑哪些步骤
    if only:
        wanted = set(_parse_only(only))   # S1-4：逗号/多次传入的多值列表
        to_run = [(k, l, f) for k, l, f in STEP_ORDER if k in wanted]
    elif from_step:
        started = False
        to_run = []
        for k, l, f in STEP_ORDER:
            if k == from_step:
                started = True
            if started:
                to_run.append((k, l, f))
    else:
        to_run = list(STEP_ORDER)

    # 过滤断点续跑中已完成的
    to_run = [(k, l, f) for k, l, f in to_run if k not in completed]
    # 过滤 skip
    to_run = [(k, l, f) for k, l, f in to_run if k not in skip]

    dprint(f"\n{'='*60}")
    dprint(f"🚀 pipeline_ui 启动")
    dprint(f"  工作目录: {workdir}")
    dprint(f"  入参: vocal_mode={vocal_mode} skip={sorted(skip)} only={only} "
           f"from_step={from_step} resume={resume}")
    dprint(f"        draft_name={draft_name or '(默认 init_draft 配置)'} "
           f"video_dir={video_dir or '(默认)'} cut_s={cut_s} limit={video_limit} "
           f"range={video_range or '(未指定)'}")
    dprint(f"        normalize={normalize} screen_policy={screen_policy} "
           f"export_resolution={export_resolution or '(未指定)'}")
    dprint(f"  待跑步骤: {[k for k,_,_ in to_run]}")
    dprint(f"{'='*60}\n")

    # 确保剪映窗口就绪（[P] 只查 JSON、[0] 初始化草稿会自行打开，允许无窗口）
    dprint("检查剪映窗口...")
    win, page = connect_jianying()
    ui_calibration.set_current_win(win)
    if not win:
        if any(k in ('precheck', 'init_draft') for k, _, _ in to_run):
            dprint("  ⚠️ 未找到剪映窗口，交给 [P]/[0] 处理")
        else:
            dprint("❌ 未找到剪映窗口，请先启动剪映并打开草稿")
            return False
    else:
        dprint(f"  ✅ 剪映窗口就绪 (page={page})")

    overall_t0 = time.time()
    all_ok = True

    from ui_core import (op_stage, stage_timeline, PipelineBudget, op_event,
                         describe_error, JianyingError)
    # 层级预算：总上限 6h（防无限挂）+ 每 step 用它自己的动态超时做子预算；
    # teardown 独立 15s——即使某 step 超时，收尾清理仍能跑。
    _budget = PipelineBudget(total_s=21600, teardown_s=15)
    dprint(f"  预算: {_budget}")
    # 阶段划分（用户定义）：1 生成草稿（含预设+时间线裁剪）→ 2 UI初始化 → 3 人声分离
    # → 4 识别中文字幕+翻译 → 5 分离音频 → 6 草稿最终形态 → 7 终检 → 8 导出
    # 2/3 由 run_vocal_separate 内部打标；5/6/7 由 run_extract_audio / run_verify 打标
    _STEP_STAGE = {'init_draft': '1 生成草稿（包含预设+时间线裁剪）',
                   'recognize_translate': '4 识别中文字幕+翻译',
                   'export': '8 导出'}

    for step_key, step_label, _ in to_run:
        # 总预算耗尽 → 不再硬跑剩余步骤（e2e AttemptBudget 语义）
        if _budget.expired():
            dprint(f"  ⏰ 总预算已耗尽（已跑 {_budget.elapsed():.0f}s），跳过剩余步骤: "
                   f"{[k for k, _, _ in to_run]}")
            all_ok = False
            break
        _st = _STEP_STAGE.get(step_key)
        if _st:
            op_stage(_st)
        dprint(f"\n{'─'*50}")
        dprint(f"▶️ 执行 {step_label}")
        dprint(f"{'─'*50}")

        impl = STEP_IMPL.get(step_key)
        if not impl:
            dprint(f"  ⚠️ 步骤 {step_key} 无实现，跳过")
            continue

        step_t0 = time.time()
        kw = step_kwargs(step_key, workdir, vocal_mode, draft_name, video_dir, cut_s,
                         video_limit, video_range, normalize, export_resolution,
                         preset_profile=preset_profile)
        ui_calibration.take_trace()          # 清掉轨迹；UI 入参由准入测试记录落盘
        try:
            # step 子预算：用动态超时（准入基线×草稿时长×130%）；
            # 算不出来（无基线/无草稿时长）就不额外限制，只受总预算约束。
            try:
                _step_max = _step_timeout(step_key, _draft_duration_s())
            except Exception:
                _step_max = None
            if _step_max:
                dprint(f"  子预算: ≤{_step_max}s")
                with _budget.capped(_step_max):
                    result = impl(**kw)
            else:
                result = impl(**kw)
        except Exception as e:
            elapsed = time.time() - step_t0
            dprint(f"  ❌ {step_label} 异常: {describe_error(e)}")
            traceback.print_exc()
            op_event('step', step_label, status='fail',
                     duration_ms=elapsed * 1000,
                     code=getattr(e, 'code', type(e).__name__),
                     retryable=getattr(e, 'retryable', None),
                     error=str(e)[:300])
            result = {'ok': False, 'duration_s': round(elapsed, 1), 'error': str(e)}

        # 各阶段独立输出操作时间线并清空，避免上一阶段的操作混入下一阶段
        # （之前 [3] 人声分离的操作会累积到 [4] 识别结束才一起 dump，标题错位）
        op_timeline(step_label, wall_t0=step_t0)
        take_op_marks()

        # 记录耗时 + 调用函数
        append_timing(workdir, step_key, step_label, result, impl=impl.__name__)

        # 断点续跑状态
        if result.get('ok'):
            completed.append(step_key)
            save_state(workdir, completed)
        else:
            dprint(f"\n  ❌ {step_label} 失败，停止 pipeline")
            all_ok = False
            break

    total_elapsed = time.time() - overall_t0
    dprint(f"\n{'='*60}")
    dprint(f"🏁 pipeline_ui {'✅全部完成' if all_ok else '⚠️部分失败'}")
    dprint(f"  总耗时 {total_elapsed:.1f}s  ({total_elapsed/60:.1f}min)")
    dprint(f"  耗时明细 → {_timings_path(workdir)}")
    dprint(f"{'='*60}")

    stage_timeline()
    return all_ok


def _power_keepalive(on: bool) -> bool:
    """防睡眠：on=True 置 ES_CONTINUOUS|SYSTEM_REQUIRED|DISPLAY_REQUIRED，
    on=False 恢复 ES_CONTINUOUS。只影响本进程，不改系统电源设置；失败只告警不致命。"""
    es_continuous = 0x80000000
    es_system_required = 0x00000001
    es_display_required = 0x00000002
    flags = (es_continuous | es_system_required | es_display_required) if on else es_continuous
    try:
        import ctypes
        r = ctypes.windll.kernel32.SetThreadExecutionState(flags)
        print(f'[power] 防睡眠{"已启用" if on else "已恢复"}'
              f'（SetThreadExecutionState(0x{flags:08X}) → {r}）', flush=True)
        return bool(r)
    except Exception as e:
        print(f'[power] 防睡眠{"启用" if on else "恢复"}失败（WARN，不阻断）: {e}', flush=True)
        return False


def main():
    from ui_core import install_timestamp_stdout
    install_timestamp_stdout()   # 所有 print 统一带墙钟毫秒前缀
    parser = argparse.ArgumentParser(description="纯 UI 自动化版剪映 pipeline")
    parser.add_argument('--workdir', default='workdir', help='工作目录')
    parser.add_argument('--only', action='append',
                        help='只跑指定步骤；支持逗号分隔多个（--only precheck,init_draft）'
                             '或多次传入（--only a --only b）')
    parser.add_argument('--from-step', dest='from_step', help='从这个步骤开始跑')
    parser.add_argument('--resume', action='store_true', help='断点续跑')
    parser.add_argument('--skip', nargs='*', default=[], help='跳过哪些步骤')
    parser.add_argument('--vocal-mode', default='none', choices=['none', 'vocal', 'background'],
                        help='人声分离模式：none=不选下拉（默认），vocal=仅保留人声，background=仅保留背景声')
    parser.add_argument('--export-resolution', default=None,
                        choices=['480P', '720P', '1080P', '2K', '4K'],
                        help='[8] 导出分辨率（默认 None=沿用剪映当前值）')
    parser.add_argument('--draft-name', default=None, help='[0] 初始化草稿的草稿名（默认取 init_draft 配置）')
    parser.add_argument('--video-dir', default=None, help='[0] 初始化草稿的视频目录（默认取 init_draft 配置）')
    parser.add_argument('--cut', dest='cut_s', type=float, default=None,
                        help='[2] 时间线裁剪起点（秒）；不传则不裁剪')
    parser.add_argument('--profile', dest='preset_profile', default=None, metavar='A',
                        help='[0] 预设组名（presets.json 里的 presets_<组名>），多账号各用各的预设')
    parser.add_argument('--limit', dest='video_limit', type=int, default=None,
                        help='[0] 取前 N 集；默认全部集数')
    parser.add_argument('--video-range', dest='video_range', default=None,
                        help="[0] 按集号选集（1-based，按文件名自然排序）：'5-15'=第5到15集；"
                             "'5'=单集；'5-'=第5集到末尾；'-10'=前10集；'1,3,5-8'=逗号组合")
    parser.add_argument('--screen-policy', default='primary',
                        choices=['primary', 'match-calibration', 'keep'],
                        help='窗口放置策略：primary=挪主屏（默认）；'
                             'match-calibration=挪到已有 calibration 的屏（无匹配回主屏）；'
                             'keep=不挪动（用窗口当前所在屏）')
    parser.add_argument('--normalize', dest='normalize', action='store_true',
                        help="[0] 强制逐集 HEVC→H.264 重编码（默认关闭：直接引用原始视频，"
                             "剪映 5.9 原生支持 HEVC，首次建稿从 ~85 分钟降到秒级）")
    args = parser.parse_args()

    _power_keepalive(True)
    try:
        ok = run_pipeline_ui(
            workdir=args.workdir,
            only=args.only,
            from_step=args.from_step,
            resume=args.resume,
            skip=args.skip,
            vocal_mode=args.vocal_mode,
            draft_name=args.draft_name,
            video_dir=args.video_dir,
            cut_s=args.cut_s,
            video_limit=args.video_limit,
            video_range=args.video_range,
            normalize=args.normalize,
            screen_policy=args.screen_policy,
            preset_profile=args.preset_profile,
            export_resolution=args.export_resolution,
        )
    finally:
        _power_keepalive(False)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
