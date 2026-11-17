# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline_ui'))

import numpy as np

import ui_calibration
import ui_core
import ui_extract_audio
import ui_recognize_translate
import ui_track_ops
import ui_verify
import ui_vocal_separate


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_restore_last_light_failure_forces_full_scan_and_fails(monkeypatch):
    win = object()
    row = {'type': 'text', 'ymid': 100, 'hidden': True}
    img = object()
    scan_calls = []

    monkeypatch.setattr(ui_track_ops, 'get_window_rect', lambda _win: (0, 0, 200, 200))
    monkeypatch.setattr(ui_track_ops, '_ensure_geom', lambda *args: None)
    monkeypatch.setattr(ui_track_ops, '_hdr_x', lambda value: value)
    monkeypatch.setattr(ui_track_ops, 'is_row_locked', lambda *args: False)
    monkeypatch.setattr(ui_track_ops, '_light_row_state',
                        lambda *args: (img, [], [row['ymid']]))
    monkeypatch.setattr(ui_track_ops.pyautogui, 'click', lambda *args: None)
    monkeypatch.setattr(ui_track_ops.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(ui_track_ops, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_calibration, 'get_param',
                        lambda _section, key, default=None: 50 if key == 'lock_x' else default)

    def dirty_scan(_win):
        scan_calls.append(_win)
        return [row], img, 0, 0, 200, 75

    monkeypatch.setattr(ui_track_ops, 'scan_tracks', dirty_scan)
    monkeypatch.setattr(ui_track_ops, '_draft_track_count', lambda: 0)
    hint = ([row], img, 0, 0, 200, 75)

    result = ui_track_ops.restore_all_tracks(win, max_rounds=1, scan_hint=hint)

    assert scan_calls == [win]
    assert result['ok'] is False


def _draft_sig_before():
    """分离前的草稿指纹：主轨 5 段 / videos=7 / 没有 audio 轨。"""
    return {
        'mtime': 0.0, 'size': 40000, 'n_tracks': 5, 'main_id': 'main1',
        'main_segments': 5,
        'track_index': {'main1': {'type': 'video', 'n_segments': 5}},
        'n_videos': 7, 'video_paths': set(), 'video_ids': set(),
        'audio_tracks': [], 'audio_shape': (), 'audios_by_id': {},
    }


def _draft_sig_after_ok():
    """分离成功：主轨 5 段不变、videos 不变、新增 audio 轨且素材带原声标记。"""
    sig = _draft_sig_before()
    sig.update({
        'mtime': 1.0, 'size': 41000, 'n_tracks': 6,
        'track_index': {'main1': {'type': 'video', 'n_segments': 5}},
        'audio_tracks': [{'id': 'aud1', 'segments': [{'material_id': 'am1'}]}],
        'audio_shape': (1,),
        'audios_by_id': {'am1': {'id': 'am1', 'video_original_sound': True}},
    })
    return sig


def _patch_extract_draft(monkeypatch, after_sig=None):
    """把分离前后的 draft 对账换成固定指纹（单测不读真实草稿）。"""
    monkeypatch.setattr(ui_extract_audio, '_draft_path', lambda: 'draft.json')
    monkeypatch.setattr(ui_extract_audio, '_draft_sig', lambda _p: _draft_sig_before())
    monkeypatch.setattr(
        ui_extract_audio, '_wait_separation_applied',
        lambda *_a, **_k: {
            'state': 'stable', 'sig': after_sig or _draft_sig_after_ok(),
            'samples': 1, 'first_drop': None, 'stable_at': 2.0,
            'reason': '', 'key_sends': 1,
        })


def test_extract_audio_propagates_restore_failure(monkeypatch, tmp_path):
    win = object()
    main = {'type': 'video', 'ymid': 100, 'hidden': False}
    audio = {'type': 'audio', 'ymid': 150, 'hidden': False}
    scans = iter([
        ([main], None, 0, 0, 200, 75, win),
        ([main, audio], object(), 0, 0, 200, 75, win),
    ])
    forbidden_calls = []

    monkeypatch.setattr(ui_extract_audio, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, '_ensure_edit_page', lambda _win: (win, True))
    monkeypatch.setattr(ui_extract_audio, 'ensure_maximized', lambda value: value)
    # [1/6] 回顶后能看到顶部两条字幕轨（新版少于此数会直接拒绝继续）
    monkeypatch.setattr(ui_extract_audio, '_top_text_cluster',
                        lambda _win: ([{'type': 'text', 'ymid': 80},
                                       {'type': 'text', 'ymid': 40}], 75, win))
    monkeypatch.setattr(ui_extract_audio, 'lock_track', lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_extract_audio, '_safe_scan', lambda _win: next(scans))
    monkeypatch.setattr(ui_extract_audio, 'find_main_track', lambda *args: (main, True))
    monkeypatch.setattr(ui_extract_audio, 'select_main_track', lambda **kwargs: True)
    # 分离前复检依赖真实 UI 扫描，不在流程用例覆盖范围内，直接放行
    monkeypatch.setattr(ui_extract_audio, '_pre_separation_recheck', lambda _win, _subs: '')
    monkeypatch.setattr(ui_extract_audio.pyautogui, 'hotkey', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio.time, 'sleep', lambda _seconds: None)
    # 回顶依赖真实窗口几何，流程用例里直接放行
    monkeypatch.setattr(ui_extract_audio, '_scroll_to_top', lambda *args, **kwargs: (True, ''))
    _patch_extract_draft(monkeypatch)
    monkeypatch.setattr(ui_extract_audio, 'restore_all_tracks',
                        lambda *args, **kwargs: {'ok': False})
    hides = []
    monkeypatch.setattr(ui_extract_audio, '_set_row_hidden',
                        lambda _win, _eye, ymid, _want, _tag: hides.append(ymid))
    monkeypatch.setattr(ui_extract_audio, '_write_final_layout',
                        lambda *args: forbidden_calls.append('write_layout'))

    def unexpected_timeline(*args):
        forbidden_calls.append('timeline')
        return False

    monkeypatch.setattr(ui_extract_audio, 'op_timeline', unexpected_timeline)

    result = ui_extract_audio.run_extract_audio(workdir=str(tmp_path))

    assert result['ok'] is False
    assert '恢复轨道失败' in result['error']
    assert sorted(hides) == [40, 80]        # [1/6] 只隐藏顶部两条字幕轨
    assert forbidden_calls == []            # 恢复失败后不再落盘/进时间线


def _wait_for_artifacts(monkeypatch, video_segments, file_count):
    clock = _FakeClock()
    monkeypatch.setattr(ui_vocal_separate, 'time', clock)
    monkeypatch.setattr(ui_vocal_separate, '_draft_video_segment_count',
                        lambda: video_segments)
    monkeypatch.setattr(ui_vocal_separate, '_vocal_alg_artifacts',
                        lambda: (file_count, float(file_count)))
    monkeypatch.setattr(ui_vocal_separate, 'dprint', lambda *args: None)
    return ui_vocal_separate._wait_vocal_done_by_artifacts(45)


@pytest.mark.parametrize('video_segments,file_count,expected_done,expected_files', [
    (2, 3, False, 4),
    (2, 4, True, 4),
    (0, 4, False, 0),
])
def test_vocal_artifacts_require_complete_expected_stable_set(
        monkeypatch, video_segments, file_count, expected_done, expected_files):
    done, files, expected = _wait_for_artifacts(monkeypatch, video_segments, file_count)

    assert done is expected_done
    assert files == file_count
    assert expected == expected_files


def test_vocal_expected_recovers_after_transient_draft_read_failure(monkeypatch):
    clock = _FakeClock()
    segment_counts = iter([0, 2])
    monkeypatch.setattr(ui_vocal_separate, 'time', clock)
    monkeypatch.setattr(ui_vocal_separate, '_draft_video_segment_count',
                        lambda: next(segment_counts))
    monkeypatch.setattr(ui_vocal_separate, '_vocal_alg_artifacts', lambda: (4, 4.0))
    monkeypatch.setattr(ui_vocal_separate, 'dprint', lambda *args: None)

    assert ui_vocal_separate._wait_vocal_done_by_artifacts(45) == (True, 4, 4)


def _verify_with_row(monkeypatch, tmp_path, expected_overrides, actual_overrides):
    expected = {'type': 'video', 'ymid': 100, 'hidden': False}
    expected.update(expected_overrides)
    actual = {
        'type': 'video', 'ymid': 100, 'hidden': False,
        'uia_type': 'video', 'color_type': 'video',
        'type_source': 'uia', 'confidence': 'high',
        'segments_uia': 1, 'channel_conflict': False,
    }
    actual.update(actual_overrides)
    win = object()

    monkeypatch.setattr(ui_verify, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_verify, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_verify, 'connect_jianying', lambda: (win, 'edit'))
    monkeypatch.setattr(ui_verify, '_scan_with_evidence',
                        lambda _win: ([actual], object(), 0, 0, 200, 75, None, [actual]))
    monkeypatch.setattr(ui_verify, 'save_screenshot', lambda *args: None)

    def fake_get_param(section, key, default=None):
        if section == 'final_layout' and key == 'tracks':
            return [expected]
        return default

    monkeypatch.setattr(ui_verify, 'get_param', fake_get_param)
    result = ui_verify.run_verify(workdir=str(tmp_path))
    report = json.loads(Path(result['report']).read_text(encoding='utf-8'))
    return result, report


def test_verify_video_audio_channel_conflict_stays_warn(monkeypatch, tmp_path):
    win = object()
    row = {'type': 'video', 'ymid': 100, 'hidden': False}
    monkeypatch.setattr(ui_verify, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_verify, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_verify, 'connect_jianying', lambda: (win, 'edit'))
    monkeypatch.setattr(ui_verify, 'scan_tracks',
                        lambda _win: ([row], object(), 0, 0, 200, 75))
    monkeypatch.setattr(ui_verify, 'get_timeline_rect', lambda _win: (0, 80, 500, 300))
    monkeypatch.setattr(ui_verify, '_collect_uia_segments', lambda _win: [])
    monkeypatch.setattr(ui_verify, 'classify_track_ys', lambda _win: [('video', 100)])
    monkeypatch.setattr(ui_verify, 'classify_row_color', lambda *args: 'audio')
    monkeypatch.setattr(ui_verify, 'save_screenshot', lambda *args: None)
    monkeypatch.setattr(
        ui_verify, 'get_param',
        lambda section, key, default=None: (
            [{'type': 'audio', 'ymid': 100, 'hidden': False}]
            if section == 'final_layout' and key == 'tracks' else default
        ),
    )

    result = ui_verify.run_verify(workdir=str(tmp_path))
    report = json.loads(Path(result['report']).read_text(encoding='utf-8'))

    assert result['ok'] is True
    assert result['state'] == 'WARN'
    assert report['recon'][0]['actual']['channel_conflict'] is True
    assert report['recon'][0]['verdict'] == 'warn'


@pytest.mark.parametrize('actual_overrides', [
    {'hidden': True},
    {'type': 'audio', 'uia_type': 'audio', 'color_type': 'audio'},
])
def test_verify_definite_hidden_or_uia_type_mismatch_fails(
        monkeypatch, tmp_path, actual_overrides):
    result, report = _verify_with_row(monkeypatch, tmp_path, {}, actual_overrides)
    assert result['ok'] is False
    assert result['state'] == 'FAIL'
    assert report['recon'][0]['verdict'] == 'fail'


# ============ 轨道可视区 / 多态滚动（T5 ymid=855 卡点）============

def test_row_in_view_excludes_rows_outside_timeline():
    tr = (0, 80, 500, 300)
    assert ui_track_ops._row_in_view(150, tr) is True
    assert ui_track_ops._row_in_view(70, tr) is False
    assert ui_track_ops._row_in_view(310, tr) is False
    # 拿不到 timeline rect 时不误判（交给后续真实扫描）
    assert ui_track_ops._row_in_view(855, None) is True


def test_scroll_all_modes_fail_is_reported(monkeypatch):
    win = object()

    def boom(*args, **kwargs):
        raise RuntimeError('模态不可用')

    monkeypatch.setattr(ui_track_ops, '_SCROLL_ORDER_START', 0)
    monkeypatch.setattr(ui_track_ops, '_light_scan',
                        lambda _w: ([{'ymid': 100}], (0, 0, 500, 300), object(), 0, 0))
    monkeypatch.setattr(ui_track_ops, '_scroll_once', boom)
    monkeypatch.setattr(ui_track_ops.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(ui_track_ops, 'dprint', lambda *a: None)

    r = ui_track_ops.scroll_timeline_vertical(win)

    assert r['ok'] is False
    assert r['reason'] == 'all_modes_failed'
    assert [t['mode'] for t in r['tried']] == ['wheel', 'scrollbar']
    assert all(t['ok'] is False for t in r['tried'])

    # 模态无固定优先级：全失败后起手索引后移，下一轮换 scrollbar 先试
    r2 = ui_track_ops.scroll_timeline_vertical(win)
    assert [t['mode'] for t in r2['tried']] == ['scrollbar', 'wheel']


def test_restore_marks_scroll_unavailable_instead_of_still_locked(monkeypatch):
    win = object()
    row = {'type': 'video', 'ymid': 100, 'hidden': False}
    img = object()

    monkeypatch.setattr(ui_track_ops, 'get_window_rect', lambda _w: (0, 0, 200, 300))
    monkeypatch.setattr(ui_track_ops, '_ensure_geom', lambda *a: None)
    monkeypatch.setattr(ui_track_ops, '_hdr_x', lambda v: v)
    monkeypatch.setattr(ui_track_ops, 'is_row_locked', lambda *a: False)
    monkeypatch.setattr(ui_track_ops, 'scan_tracks', lambda _w: ([row], img, 0, 0, 200, 300))
    monkeypatch.setattr(ui_track_ops, '_light_scan',
                        lambda _w: ([dict(row)], (0, 0, 200, 300), img, 0, 0))
    monkeypatch.setattr(ui_track_ops, 'scroll_timeline_vertical',
                        lambda *a, **k: {'ok': False, 'mode': None,
                                         'tried': [], 'reason': 'all_modes_failed'})
    monkeypatch.setattr(ui_track_ops.pyautogui, 'click', lambda *a: None)
    monkeypatch.setattr(ui_track_ops.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(ui_track_ops, 'dprint', lambda *a: None)
    monkeypatch.setattr(ui_calibration, 'get_param',
                        lambda _s, key, default=None: 50 if key == 'lock_x' else default)

    r = ui_track_ops.restore_all_tracks(win, max_rounds=1, expected_tracks=2)

    assert r['ok'] is False
    assert r['reason'] == 'scroll_unavailable'
    assert r['scroll_unavailable'] is True
    assert r['still_locked'] == []          # 不能报成"仍锁定"


def test_restore_reflows_stale_hint_before_unlock(monkeypatch):
    win = object()
    stale = {'type': 'video', 'ymid': 855, 'hidden': False}   # 插入新轨前的旧坐标
    fresh = {'type': 'unknown', 'y_top': 590, 'y_bot': 616, 'ymid': 603, 'hidden': False}
    # 真实数组：锁列取证（_strip_mean/_strip_blue_count）要读 img.shape 与像素切片
    img = np.zeros((900, 200, 3), dtype=np.uint8)
    clicks = []

    monkeypatch.setattr(ui_track_ops, 'get_window_rect', lambda _w: (0, 0, 200, 900))
    monkeypatch.setattr(ui_track_ops, '_ensure_geom', lambda *a: None)
    monkeypatch.setattr(ui_track_ops, '_hdr_x', lambda v: v)
    monkeypatch.setattr(ui_track_ops, '_light_scan',
                        lambda _w: ([dict(fresh)], (0, 0, 200, 900), img, 0, 0))
    monkeypatch.setattr(ui_track_ops, 'scan_tracks',
                        lambda _w: ([dict(fresh)], img, 0, 0, 200, 900))
    monkeypatch.setattr(ui_track_ops, '_draft_track_count', lambda: 0)
    monkeypatch.setattr(ui_track_ops, 'is_row_locked',
                        lambda _img, _wt, ymid: int(ymid) == 603 and not clicks)
    monkeypatch.setattr(ui_track_ops.pyautogui, 'click',
                        lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(ui_track_ops.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(ui_track_ops, 'dprint', lambda *a: None)
    monkeypatch.setattr(ui_calibration, 'get_param',
                        lambda _s, key, default=None: 50 if key == 'lock_x' else default)

    r = ui_track_ops.restore_all_tracks(win, max_rounds=1,
                                        scan_hint=([stale], img, 0, 0, 200, 900))

    assert clicks == [(50, 603)]        # 点在重排后的新 y，而不是失效的 855
    assert r['drift_rounds'] >= 1
    assert r['ok'] is True


def test_extract_audio_reports_scroll_unavailable_distinctly(monkeypatch, tmp_path):
    win = object()
    main = {'type': 'video', 'ymid': 100, 'hidden': False}
    audio = {'type': 'audio', 'ymid': 150, 'hidden': False}
    scans = iter([
        ([main], None, 0, 0, 200, 75, win),
        ([main, audio], object(), 0, 0, 200, 75, win),
    ])

    monkeypatch.setattr(ui_extract_audio, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, '_ensure_edit_page', lambda _win: (win, True))
    monkeypatch.setattr(ui_extract_audio, 'ensure_maximized', lambda value: value)
    # [1/6] 回顶后能看到顶部两条字幕轨（新版少于此数会直接拒绝继续）
    monkeypatch.setattr(ui_extract_audio, '_top_text_cluster',
                        lambda _win: ([{'type': 'text', 'ymid': 80},
                                       {'type': 'text', 'ymid': 40}], 75, win))
    monkeypatch.setattr(ui_extract_audio, 'lock_track', lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_extract_audio, '_safe_scan', lambda _win: next(scans))
    monkeypatch.setattr(ui_extract_audio, 'find_main_track', lambda *args: (main, True))
    monkeypatch.setattr(ui_extract_audio, 'select_main_track', lambda **kwargs: True)
    # 分离前复检依赖真实 UI 扫描，不在流程用例覆盖范围内，直接放行
    monkeypatch.setattr(ui_extract_audio, '_pre_separation_recheck', lambda _win, _subs: '')
    monkeypatch.setattr(ui_extract_audio.pyautogui, 'hotkey', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio.time, 'sleep', lambda _seconds: None)
    # 本用例只验证 restore 的 scroll_unavailable 与回顶失败区分开，回顶本身放行
    monkeypatch.setattr(ui_extract_audio, '_scroll_to_top', lambda *args, **kwargs: (True, ''))
    _patch_extract_draft(monkeypatch)
    monkeypatch.setattr(ui_extract_audio, 'restore_all_tracks',
                        lambda *args, **kwargs: {'ok': False, 'reason': 'scroll_unavailable',
                                                 'still_locked': [], 'still_hidden': []})
    monkeypatch.setattr(ui_extract_audio, '_set_row_hidden', lambda *args: None)

    result = ui_extract_audio.run_extract_audio(workdir=str(tmp_path))

    assert result['ok'] is False
    assert '可视区外' in result['error']          # 与"仍锁定或隐藏"区分
    assert result['restore']['reason'] == 'scroll_unavailable'


# ============ 分离音频：draft 前后对账硬校验 ============
def test_check_separation_rejects_main_track_converted_to_audio():
    """上一轮事故复现：主轨 5 段被转成音频（segments=0、videos 7→2）必须判失败。"""
    before = _draft_sig_before()
    after = _draft_sig_before()
    after.update({
        'n_tracks': 8, 'main_segments': 0,
        'track_index': {'main1': {'type': 'video', 'n_segments': 0}},
        'n_videos': 2,
        'audio_tracks': [{'id': 'aud1',
                          'segments': [{'material_id': f'am{i}'} for i in range(5)]}],
        'audios_by_id': {f'am{i}': {'id': f'am{i}'} for i in range(5)},
    })

    ok, detail = ui_extract_audio._check_separation(before, after)

    assert ok is False
    assert '主轨 segments 减少' in detail


def test_check_separation_rejects_video_material_loss():
    before = _draft_sig_before()
    after = _draft_sig_after_ok()
    after['n_videos'] = 6

    ok, detail = ui_extract_audio._check_separation(before, after)

    assert ok is False
    assert 'materials.videos 减少' in detail


def test_check_separation_requires_new_audio_from_video_original_sound():
    before = _draft_sig_before()
    after = _draft_sig_after_ok()
    after['audios_by_id'] = {'am1': {'id': 'am1'}}       # 没有任何原声标记

    ok, detail = ui_extract_audio._check_separation(before, after)

    assert ok is False
    assert 'video_original_sound' in detail

    after['audios_by_id'] = {'am1': {'id': 'am1', 'type': 'extract'}}
    ok, detail = ui_extract_audio._check_separation(before, after)
    assert ok is True


def test_extract_audio_fails_when_separation_check_rejects(monkeypatch, tmp_path):
    """分离被判失败时必须立刻返回失败，不能继续 restore / 落盘（避免二次破坏）。"""
    win = object()
    main = {'type': 'video', 'ymid': 100, 'hidden': False}
    scans = iter([([main], None, 0, 0, 200, 75, win)])
    forbidden = []
    damaged = _draft_sig_before()
    damaged.update({'n_tracks': 8, 'main_segments': 0, 'n_videos': 2})

    monkeypatch.setattr(ui_extract_audio, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, '_ensure_edit_page', lambda _win: (win, True))
    monkeypatch.setattr(ui_extract_audio, 'ensure_maximized', lambda value: value)
    # [1/6] 回顶后能看到顶部两条字幕轨（新版少于此数会直接拒绝继续）
    monkeypatch.setattr(ui_extract_audio, '_top_text_cluster',
                        lambda _win: ([{'type': 'text', 'ymid': 80},
                                       {'type': 'text', 'ymid': 40}], 75, win))
    monkeypatch.setattr(ui_extract_audio, 'lock_track', lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_extract_audio, '_safe_scan', lambda _win: next(scans))
    monkeypatch.setattr(ui_extract_audio, 'find_main_track', lambda *args: (main, True))
    monkeypatch.setattr(ui_extract_audio, 'select_main_track', lambda **kwargs: True)
    # 分离前复检依赖真实 UI 扫描，不在流程用例覆盖范围内，直接放行
    monkeypatch.setattr(ui_extract_audio, '_pre_separation_recheck', lambda _win, _subs: '')
    monkeypatch.setattr(ui_extract_audio.pyautogui, 'hotkey', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio.time, 'sleep', lambda _seconds: None)
    # 回顶依赖真实窗口几何，流程用例里直接放行
    monkeypatch.setattr(ui_extract_audio, '_scroll_to_top', lambda *args, **kwargs: (True, ''))
    _patch_extract_draft(monkeypatch, after_sig=damaged)
    monkeypatch.setattr(ui_core, 'save_screenshot', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, 'restore_all_tracks',
                        lambda *args, **kwargs: forbidden.append('restore'))
    monkeypatch.setattr(ui_extract_audio, '_set_row_hidden', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio, '_write_final_layout',
                        lambda *args: forbidden.append('layout'))

    result = ui_extract_audio.run_extract_audio(workdir=str(tmp_path))

    assert result['ok'] is False
    assert '分离音频失败' in result['error']
    assert forbidden == []


# ============ 主轨识别：不允许用预设轨顶替 ============
def _patch_main_track_probe(monkeypatch):
    monkeypatch.setattr(ui_track_ops, 'get_window_rect', lambda _w: (0, 0, 300, 200))
    monkeypatch.setattr(ui_track_ops, '_ensure_geom', lambda *args: (0, 1.0))
    monkeypatch.setattr(ui_track_ops, '_hdr_x', lambda off: int(off))
    monkeypatch.setattr(ui_track_ops, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_track_ops, 'get_param',
                        lambda _section, _name, default=None: default)


def test_find_main_track_rejects_row_without_cover_button(monkeypatch):
    """M2 封面按钮校验不过 → 返回 None（不再按 M1 采用下方预设轨）。"""
    _patch_main_track_probe(monkeypatch)
    rows = [{'type': 'video', 'ymid': 100}]

    main, ok = ui_track_ops.find_main_track(object(), rows, np.zeros((200, 300, 3), np.uint8))

    assert main is None
    assert ok is False


def test_find_main_track_accepts_cell_with_cover_button(monkeypatch):
    _patch_main_track_probe(monkeypatch)
    rows = [{'type': 'video', 'ymid': 100}]
    img = np.zeros((200, 300, 3), np.uint8)
    img[75:95, 120:160] = 255          # 封面按钮列有亮度对比

    main, ok = ui_track_ops.find_main_track(object(), rows, img)

    assert ok is True
    assert main['ymid'] == 100


@pytest.mark.parametrize('expected_overrides,actual_overrides', [
    ({}, {
        'type': 'audio', 'uia_type': None, 'color_type': 'audio',
        'type_source': 'color', 'confidence': 'color-only', 'segments_uia': 0,
    }),
    ({}, {
        'type': 'text', 'uia_type': 'text', 'color_type': 'video',
        'channel_conflict': True,
    }),
    ({'type': 'text', 'hidden': True}, {
        'type': 'unknown', 'hidden': True, 'uia_type': None, 'color_type': None,
        'type_source': 'none', 'confidence': 'unverifiable_hidden', 'segments_uia': 0,
    }),
])
def test_verify_low_confidence_type_evidence_stays_warn(
        monkeypatch, tmp_path, expected_overrides, actual_overrides):
    result, report = _verify_with_row(
        monkeypatch, tmp_path, expected_overrides, actual_overrides)

    assert result['ok'] is True
    assert result['state'] == 'WARN'
    assert report['recon'][0]['verdict'] == 'warn'


def test_recognize_timeout_rejects_only_one_added_chinese_track(monkeypatch, tmp_path):
    clock = _FakeClock()
    win = object()
    existing = {'type': 'text', 'ymid': 100}
    added = {'type': 'text', 'ymid': 140}
    scans = iter([([existing],), ([existing],), ([existing, added],)])
    text_counts = iter([10, 11])
    english_counts = iter([0, 1])
    record_calls = []

    monkeypatch.setattr(ui_recognize_translate, 'time', clock)
    monkeypatch.setattr(ui_recognize_translate, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_recognize_translate, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_recognize_translate, 'connect_jianying',
                        lambda *args, **kwargs: (win, 'edit'))
    monkeypatch.setattr(ui_recognize_translate, 'ensure_maximized', lambda value: value)
    monkeypatch.setattr(ui_recognize_translate, 'scan_tracks', lambda _win: next(scans))
    monkeypatch.setattr(ui_recognize_translate, '_draft_texts_count',
                        lambda: next(text_counts))
    monkeypatch.setattr(ui_recognize_translate, '_draft_english_text_count',
                        lambda: next(english_counts))
    monkeypatch.setattr(ui_recognize_translate, '_cal', lambda _key, default: default)
    monkeypatch.setattr(ui_recognize_translate, '_record_subtitle_tracks',
                        lambda *args: record_calls.append(args))
    monkeypatch.setattr(ui_recognize_translate, 'op_timeline', lambda *args: False)

    result = ui_recognize_translate.run_recognize_translate(
        workdir=str(tmp_path), timeout_sec=1, wait_only=True)

    assert result['ok'] is False
    assert result['text_tracks_delta'] == 1
    assert result['english_texts_added'] == 1
    assert record_calls == []


# ── 识别/翻译成轨结构闸门（异常样例：25 条译文压在同一点 → 32 轨/629 条 text）──

def _text_mat(mid, text):
    return {'id': mid, 'content': json.dumps({'text': text}, ensure_ascii=False)}


def _seg(sid, mid, start, dur):
    return {'id': sid, 'material_id': mid,
            'target_timerange': {'start': start, 'duration': dur}}


def _recognized_draft(strays=25):
    """cn/en 各 300 段正常成两轨；strays 条 33ms 译文全落在 647.733s、各自单独成轨。"""
    import verify_draft

    tracks, texts, cn_segs, en_segs = [], [], [], []
    for i in range(300):
        st = i * 1_000_000
        cn_segs.append(_seg(f'cn{i}', f'cnm{i}', st, 900_000))
        en_segs.append(_seg(f'en{i}', f'enm{i}', st, 900_000))
        texts += [_text_mat(f'cnm{i}', f'中文字幕{i}'), _text_mat(f'enm{i}', f'subtitle {i}')]
    tracks.append({'type': 'text', 'attribute': 2, 'segments': cn_segs})
    tracks.append({'type': 'text', 'attribute': 0, 'segments': en_segs})
    for i in range(strays):
        tracks.append({'type': 'text', 'attribute': 0,
                       'segments': [_seg(f'st{i}', f'stm{i}', 647_733_000, 33_000)]})
        texts.append(_text_mat(f'stm{i}', f'stray {i}'))
    tracks.append({'type': 'video', 'segments': [_seg('v0', 'vm0', 0, 682_100_000)]})
    return verify_draft, {'tracks': tracks, 'materials': {'texts': texts, 'videos': []}}


def _dur_track(attr, prefix, durs):
    """按给定时长序列造一条 text 轨（起点间隔 1s，保证不触发同起点堆叠）。"""
    return {'type': 'text', 'attribute': attr, 'segments': [
        _seg(f'{prefix}{i}', f'{prefix}m{i}', i * 1_000_000, d)
        for i, d in enumerate(durs)]}


def _two_track_draft(cn_durs, en_durs):
    texts = []
    for prefix, durs in (('cn', cn_durs), ('en', en_durs)):
        texts += [_text_mat(f'{prefix}m{i}', f'{prefix} {i}') for i in range(len(durs))]
    return {'tracks': [_dur_track(2, 'cn', cn_durs), _dur_track(0, 'en', en_durs)],
            'materials': {'texts': texts, 'videos': []}}


def test_text_pack_issues_passes_normal_two_track_draft():
    vd, draft = _recognized_draft(strays=0)

    pack = vd.text_pack_issues(draft)

    assert pack['zero_dur'] == []
    assert pack['zero_dur_warn'] == []
    assert pack['stacked'] == []
    assert vd.pack_brief(pack) == ''
    assert vd.pack_warn_brief(pack) == ''


def test_text_pack_issues_allows_english_split_and_flag_text_tracks():
    """生产草稿的真实形态：英文轨因长句拆分多于中文轨、另有 1~16 段的预设文字轨。"""
    import verify_draft

    tracks, texts, cn_segs, en_segs = [], [], [], []
    for i in range(100):
        st = i * 2_000_000
        cn_segs.append(_seg(f'cn{i}', f'cnm{i}', st, 1_800_000))
        texts.append(_text_mat(f'cnm{i}', f'中文字幕{i}'))
    for i in range(135):                       # 英文拆分后段数合法地多于中文
        st = i * 1_500_000
        en_segs.append(_seg(f'en{i}', f'enm{i}', st, 1_300_000))
        texts.append(_text_mat(f'enm{i}', f'subtitle {i}'))
    tracks.append({'type': 'text', 'attribute': 2, 'segments': cn_segs})
    tracks.append({'type': 'text', 'attribute': 0, 'segments': en_segs})
    for k, (start, n) in enumerate(((0, 1), (30_000_000, 5))):
        tracks.append({'type': 'text', 'attribute': 0, 'segments': [
            _seg(f'p{k}_{j}', f'pm{k}_{j}', start + j * 3_000_000, 2_000_000)
            for j in range(n)]})
        texts += [_text_mat(f'pm{k}_{j}', f'预设文字{k}{j}') for j in range(n)]
    tracks.append({'type': 'video', 'segments': [_seg('v0', 'vm0', 0, 200_000_000)]})

    pack = verify_draft.text_pack_issues(
        {'tracks': tracks, 'materials': {'texts': texts, 'videos': []}})

    assert pack['text_tracks'] == 4
    assert pack['zero_dur'] == [] and pack['stacked'] == []
    assert verify_draft.pack_brief(pack) == ''


def test_text_pack_issues_catches_piled_stray_tracks():
    vd, draft = _recognized_draft(strays=25)

    pack = vd.text_pack_issues(draft)

    assert len(pack['zero_dur']) == 25
    assert pack['zero_dur'][0]['dur_ms'] == 33
    assert len(pack['stacked']) == 1
    assert pack['stacked'][0]['count'] == 25
    assert pack['stacked'][0]['tracks'] == 25
    brief = vd.pack_brief(pack)
    assert '堆叠1处/25段' in brief and '零时长超标25轨/25段' in brief


def test_text_pack_issues_single_zero_dur_is_warn_not_fail():
    """单条一帧（33ms）不再一条就阻断 → warn，且告警明细必须可打印（不静默）。"""
    vd, _ = _recognized_draft(strays=0)
    durs = [900_000] * 300
    durs[7] = 33_000

    pack = vd.text_pack_issues(_two_track_draft(durs, [900_000] * 300))

    assert pack['zero_dur'] == []
    assert len(pack['zero_dur_warn']) == 1
    w = pack['zero_dur_warn'][0]
    assert w['kind'] == '中文' and w['count'] == 1 and w['total'] == 300
    assert w['first_dur_ms'] == 33 and w['first_start'] == 7_000_000
    assert vd.pack_brief(pack) == ''
    warn = vd.pack_warn_brief(pack)
    assert '中文轨#0 零时长1/300段' in warn and '0.33%' in warn


def test_text_pack_issues_two_zero_dur_is_still_warn():
    vd, _ = _recognized_draft(strays=0)
    durs = [900_000] * 300
    durs[3] = durs[9] = 33_000

    pack = vd.text_pack_issues(_two_track_draft(durs, [900_000] * 300))

    assert pack['zero_dur'] == []
    assert len(pack['zero_dur_warn']) == 1
    assert pack['zero_dur_warn'][0]['count'] == 2
    assert vd.pack_brief(pack) == ''


def test_text_pack_issues_three_zero_dur_blocks_by_count():
    vd, _ = _recognized_draft(strays=0)
    durs = [900_000] * 300
    durs[3] = durs[9] = durs[20] = 33_000

    pack = vd.text_pack_issues(_two_track_draft(durs, [900_000] * 300))

    assert len(pack['zero_dur']) == 3
    assert pack['zero_dur_warn'] == []
    st = pack['zero_dur_stats'][0]
    assert st['blocking'] is True and st['reason'] == '段数≥3'
    brief = vd.pack_brief(pack)
    assert '零时长超标1轨/3段' in brief and '中文轨#0 3/300=1.00%' in brief


def test_text_pack_issues_blocks_when_zero_dur_ratio_over_one_percent():
    """段数不到 3，但占该轨总段数 >1% → 阻断（短轨里 1 条就超标）。"""
    vd, _ = _recognized_draft(strays=0)
    en_durs = [900_000] * 50
    en_durs[4] = 33_000

    pack = vd.text_pack_issues(_two_track_draft([900_000] * 300, en_durs))

    st = pack['zero_dur_stats'][0]
    assert st['kind'] == '英文' and st['count'] == 1 and st['total'] == 50
    assert st['blocking'] is True and st['reason'] == '占比>1%'
    assert len(pack['zero_dur']) == 1
    assert '英文轨#1 1/50=2.00%' in vd.pack_brief(pack)


def test_text_pack_issues_zero_dur_counted_per_track_not_merged():
    """中/英各算各的：CN 2/1000=0.2% 只告警，EN 2/100=2% 阻断，不因合并成 4/1100 互相稀释。"""
    vd, _ = _recognized_draft(strays=0)
    cn_durs = [900_000] * 1000
    cn_durs[10] = cn_durs[500] = 33_000
    en_durs = [900_000] * 100
    en_durs[2] = en_durs[60] = 33_000

    pack = vd.text_pack_issues(_two_track_draft(cn_durs, en_durs))

    by_kind = {s['kind']: s for s in pack['zero_dur_stats']}
    assert by_kind['中文']['blocking'] is False
    assert by_kind['英文']['blocking'] is True
    assert len(pack['zero_dur']) == 2
    assert {x['kind'] for x in pack['zero_dur']} == {'英文'}
    assert len(pack['zero_dur_warn']) == 1 and pack['zero_dur_warn'][0]['kind'] == '中文'
    brief = vd.pack_brief(pack)
    assert '零时长超标1轨/2段' in brief and '英文轨#1 2/100=2.00%' in brief
    assert '中文轨#0 零时长2/1000段' in vd.pack_warn_brief(pack)


def test_text_pack_issues_ignores_short_pair_at_same_start():
    """同一时刻 2 条（中英对照）是正常形态，不能误判为压堆。"""
    vd, draft = _recognized_draft(strays=0)
    # 把第二段英文挪到与第一段中文同起点
    en0 = draft['tracks'][1]['segments'][0]
    en0['target_timerange']['start'] = draft['tracks'][0]['segments'][1]['target_timerange']['start']

    pack = vd.text_pack_issues(draft)

    assert pack['stacked'] == []


def _recognize_success_env(monkeypatch):
    """把一轮「条数增量达标」的识别流程搭起来（文本轨 +1、texts 10→12、英文 300）。"""
    clock = _FakeClock()
    win = object()
    existing = {'type': 'text', 'ymid': 100}
    scans = iter([([existing],), ([existing],)])
    text_counts = iter([10, 12])
    english_counts = iter([0, 300])
    monkeypatch.setattr(ui_recognize_translate, 'time', clock)
    monkeypatch.setattr(ui_recognize_translate, 'op_stage', lambda *args: None)
    monkeypatch.setattr(ui_recognize_translate, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_recognize_translate, 'connect_jianying',
                        lambda *args, **kwargs: (win, 'edit'))
    monkeypatch.setattr(ui_recognize_translate, 'ensure_maximized', lambda value: value)
    monkeypatch.setattr(ui_recognize_translate, 'scan_tracks', lambda _win: next(scans))
    monkeypatch.setattr(ui_recognize_translate, '_draft_texts_count',
                        lambda: next(text_counts))
    monkeypatch.setattr(ui_recognize_translate, '_draft_english_text_count',
                        lambda: next(english_counts))
    monkeypatch.setattr(ui_recognize_translate, '_cal', lambda _key, default: default)
    monkeypatch.setattr(ui_recognize_translate, '_record_subtitle_tracks',
                        lambda *args: None)
    monkeypatch.setattr(ui_recognize_translate, 'op_timeline', lambda *args: False)
    deselected = []
    monkeypatch.setattr(ui_recognize_translate, 'deselect_all',
                        lambda _win: deselected.append(True) or {'ok': True})
    return deselected


def test_recognize_rejects_piled_translation_even_if_counts_pass(monkeypatch, tmp_path):
    deselected = _recognize_success_env(monkeypatch)
    vd, draft = _recognized_draft(strays=25)
    pack = vd.text_pack_issues(draft)
    monkeypatch.setattr(ui_recognize_translate, '_draft_text_pack', lambda: pack)

    result = ui_recognize_translate.run_recognize_translate(
        workdir=str(tmp_path), timeout_sec=1, wait_only=True)

    assert result['ok'] is False
    assert '字幕成轨结构异常' in result['error']
    assert '同起点堆叠1处/25段' in result['error']
    assert '零时长超标25轨/25段' in result['error']
    assert result['text_pack'] is pack
    assert deselected == [True]     # 判失败也要清掉全选


def test_recognize_accepts_normal_draft_after_pack_gate(monkeypatch, tmp_path):
    deselected = _recognize_success_env(monkeypatch)
    vd, draft = _recognized_draft(strays=0)
    monkeypatch.setattr(ui_recognize_translate, '_draft_text_pack',
                        lambda: vd.text_pack_issues(draft))

    result = ui_recognize_translate.run_recognize_translate(
        workdir=str(tmp_path), timeout_sec=1, wait_only=True)

    assert result['ok'] is True
    assert result['error'] is None
    assert deselected == [True]
