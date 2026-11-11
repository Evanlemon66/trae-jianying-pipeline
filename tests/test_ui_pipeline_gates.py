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
