# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline_ui'))

import ui_calibration
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
    hint = ([row], img, 0, 0, 200, 75)

    result = ui_track_ops.restore_all_tracks(win, max_rounds=1, scan_hint=hint)

    assert scan_calls == [win]
    assert result['ok'] is False


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
    monkeypatch.setattr(ui_extract_audio, '_top_text_cluster', lambda _win: ([], 75, win))
    monkeypatch.setattr(ui_extract_audio, '_safe_scan', lambda _win: next(scans))
    monkeypatch.setattr(ui_extract_audio, 'find_main_track', lambda *args: (main, True))
    monkeypatch.setattr(ui_extract_audio, 'select_main_track', lambda: None)
    monkeypatch.setattr(ui_extract_audio.pyautogui, 'hotkey', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(ui_extract_audio, 'restore_all_tracks',
                        lambda *args, **kwargs: {'ok': False})
    monkeypatch.setattr(ui_extract_audio, '_subs_from_hint',
                        lambda _hint: ([dict(main, type='text'), dict(main, type='text', ymid=140)], 75))
    monkeypatch.setattr(ui_extract_audio, '_set_row_hidden',
                        lambda *args: forbidden_calls.append('hide_chinese'))
    monkeypatch.setattr(ui_extract_audio, '_write_final_layout',
                        lambda *args: forbidden_calls.append('write_layout'))

    def unexpected_timeline(*args):
        forbidden_calls.append('timeline')
        return False

    monkeypatch.setattr(ui_extract_audio, 'op_timeline', unexpected_timeline)

    result = ui_extract_audio.run_extract_audio(workdir=str(tmp_path))

    assert result['ok'] is False
    assert '恢复轨道失败' in result['error']
    assert forbidden_calls == []


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

    monkeypatch.setattr(ui_track_ops, '_light_scan',
                        lambda _w: ([{'ymid': 100}], (0, 0, 500, 300), object(), 0, 0))
    monkeypatch.setattr(ui_track_ops, '_scroll_once', boom)
    monkeypatch.setattr(ui_track_ops.time, 'sleep', lambda _s: None)
    monkeypatch.setattr(ui_track_ops, 'dprint', lambda *a: None)

    r = ui_track_ops.scroll_timeline_vertical(win)

    assert r['ok'] is False
    assert r['reason'] == 'all_modes_failed'
    assert [t['mode'] for t in r['tried']] == ['keyboard', 'wheel', 'scrollbar']
    assert all(t['ok'] is False for t in r['tried'])


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
    img = object()
    clicks = []

    monkeypatch.setattr(ui_track_ops, 'get_window_rect', lambda _w: (0, 0, 200, 900))
    monkeypatch.setattr(ui_track_ops, '_ensure_geom', lambda *a: None)
    monkeypatch.setattr(ui_track_ops, '_hdr_x', lambda v: v)
    monkeypatch.setattr(ui_track_ops, '_light_scan',
                        lambda _w: ([dict(fresh)], (0, 0, 200, 900), img, 0, 0))
    monkeypatch.setattr(ui_track_ops, 'scan_tracks',
                        lambda _w: ([dict(fresh)], img, 0, 0, 200, 900))
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
    monkeypatch.setattr(ui_extract_audio, '_top_text_cluster', lambda _win: ([], 75, win))
    monkeypatch.setattr(ui_extract_audio, '_safe_scan', lambda _win: next(scans))
    monkeypatch.setattr(ui_extract_audio, 'find_main_track', lambda *args: (main, True))
    monkeypatch.setattr(ui_extract_audio, 'select_main_track', lambda: None)
    monkeypatch.setattr(ui_extract_audio.pyautogui, 'hotkey', lambda *args: None)
    monkeypatch.setattr(ui_extract_audio.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(ui_extract_audio, 'restore_all_tracks',
                        lambda *args, **kwargs: {'ok': False, 'reason': 'scroll_unavailable',
                                                 'still_locked': [], 'still_hidden': []})

    result = ui_extract_audio.run_extract_audio(workdir=str(tmp_path))

    assert result['ok'] is False
    assert '可视区外' in result['error']          # 与"仍锁定或隐藏"区分
    assert result['restore']['reason'] == 'scroll_unavailable'


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
