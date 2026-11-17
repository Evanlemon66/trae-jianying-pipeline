# -*- coding: utf-8 -*-
"""准入草稿纯逻辑单测：不启动剪映、不碰 UI、不触发导出。

覆盖：配置归一化/校验、剧集目录顺延、文件层判据、baseline 判脏、9 档决策表、
baseline 落盘与读取。可直接 `pytest tests/test_admission_draft.py`，
也可 `python tests/test_admission_draft.py` 独立运行。
"""
import json
import os
import sys
import tempfile
import time
import traceback
from copy import deepcopy
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, 'pipeline_ui'), os.path.join(_ROOT, 'pipeline')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import admission_draft as ad


def _mk_series(root, name, episodes=1):
    d = os.path.join(str(root), name)
    os.makedirs(d, exist_ok=True)
    for i in range(1, episodes + 1):
        Path(os.path.join(d, f'第{i}集.mp4')).write_bytes(b'')
    return d


def _draft(video_segments=2, texts=0, extra_tracks=None):
    draft = {
        'duration': 5_000_000,
        'tracks': [{'type': 'video', 'segments': [{'id': i} for i in range(video_segments)]},
                   {'type': 'text', 'segments': [{'id': 't0'}]},
                   {'type': 'audio', 'segments': [{'id': 'a0'}]}],
        'materials': {'videos': [{}] * video_segments,
                      'texts': [{'id': i} for i in range(texts)],
                      'audios': [{}]},
    }
    draft['tracks'].extend(extra_tracks or [])
    return draft


def test_normalize_config_requires_draft_name():
    for raw in ({}, {'draft_name': ''}, {'draft_name': '   '}):
        try:
            ad.normalize_config(raw)
        except ValueError as e:
            assert 'draft_name' in str(e)
        else:
            raise AssertionError(f'draft_name 缺失必须失败: {raw!r}')


def test_normalize_config_rejects_illegal_name():
    for bad in ('a/b', 'a\\b', '..', '.', 'a:b'):
        try:
            ad.normalize_config({'draft_name': bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f'非法草稿名必须失败: {bad!r}')


def test_normalize_config_defaults():
    cfg = ad.normalize_config({'draft_name': ' ui_pipeline_ac '})
    assert cfg['draft_name'] == 'ui_pipeline_ac'
    assert cfg['video']['glob'] == '第*集.mp4'
    assert cfg['video']['limit'] == 5
    assert cfg['video']['range'] is None
    assert cfg['normalize'] is False
    assert cfg['cut_s'] is None
    assert cfg['preset_profile'] is None
    assert cfg['open']['hot_wait_after_build'] == 0
    assert cfg['open']['cold_wait'] == 150
    assert cfg['open']['max_launch'] == 2
    assert cfg['open']['edit_timeout'] == 90
    assert cfg['open']['allow_cold_restart'] is True
    assert cfg['open']['close_on_exit'] is True


def test_normalize_config_close_on_exit_override():
    cfg = ad.normalize_config({'draft_name': 'ac', 'open': {'close_on_exit': False}})
    assert cfg['open']['close_on_exit'] is False


def test_expected_track_count_uses_preset_count():
    assert ad.expected_track_count([{'name': 'a'}]) == 2
    assert ad.expected_track_count(['a', 'b', 'c', 'd']) == 5
    for empty in ([], (), None):
        try:
            ad.expected_track_count(empty)
        except ValueError as e:
            assert '预设组' in str(e)
        else:
            raise AssertionError(f'空预设组必须报错: {empty!r}')


def test_normalize_config_parses_overrides():
    cfg = ad.normalize_config({
        'draft_name': 'ac',
        'video': {'root_index': 1, 'series_index': 3, 'glob': '*.mp4', 'limit': 8,
                  'range': ' 2-4 ', 'series': '某剧'},
        'preset_profile': 'b',
        'normalize': True,
        'cut_s': 12.5,
        'open': {'cold_wait': 200, 'max_launch': 1, 'allow_cold_restart': False},
    })
    assert cfg['video'] == {'root_index': 1, 'series_index': 3, 'root': '', 'series': '某剧',
                            'glob': '*.mp4', 'limit': 8, 'range': '2-4'}
    assert cfg['preset_profile'] == 'b'
    assert cfg['normalize'] is True
    assert cfg['cut_s'] == 12.5
    assert cfg['open']['cold_wait'] == 200
    assert cfg['open']['max_launch'] == 1
    assert cfg['open']['allow_cold_restart'] is False


def test_resolve_series_skips_empty_dir(tmp_path):
    root = tmp_path / 'root'
    os.makedirs(str(root))
    _mk_series(root, 'A_空目录', 0)
    second = _mk_series(root, 'B_有视频', 2)
    _mk_series(root, 'C_有视频', 1)

    cfg = ad.normalize_config({'draft_name': 'ac'})['video']
    series, hits = ad.resolve_series_dir(cfg, [str(root)])
    assert series == second, f'应顺延到第一个有视频的目录，实际 {series}'
    assert hits == 2

    cfg2 = dict(cfg, series_index=2)
    series2, hits2 = ad.resolve_series_dir(cfg2, [str(root)])
    assert series2.endswith('C_有视频') and hits2 == 1

    cfg3 = dict(cfg, series_index=9)
    try:
        ad.resolve_series_dir(cfg3, [str(root)])
    except ValueError:
        pass
    else:
        raise AssertionError('series_index 越界应失败')


def test_resolve_series_explicit_override(tmp_path):
    root = tmp_path / 'root'
    os.makedirs(str(root))
    _mk_series(root, 'A_空目录', 0)
    good = _mk_series(root, 'B_有视频', 1)

    cfg = ad.normalize_config({'draft_name': 'ac', 'video': {'series': 'B_有视频'}})['video']
    series, _ = ad.resolve_series_dir(cfg, [str(root)])
    assert series == good

    bad = ad.normalize_config({'draft_name': 'ac', 'video': {'series': 'A_空目录'}})['video']
    try:
        ad.resolve_series_dir(bad, [str(root)])
    except ValueError:
        pass
    else:
        raise AssertionError('显式目录没有匹配视频应失败')


def test_resolve_series_root_index(tmp_path):
    """root_index 只允许 0（series_root）；>0 已废弃，显式指定请用 video.root。"""
    r0 = tmp_path / 'r0'
    r1 = tmp_path / 'r1'
    os.makedirs(str(r0))
    os.makedirs(str(r1))
    _mk_series(r1, 'B_有视频', 1)

    for bad_idx in (1, 5):
        cfg_bad = ad.normalize_config(
            {'draft_name': 'ac', 'video': {'root_index': bad_idx}})['video']
        try:
            ad.resolve_series_dir(cfg_bad, [str(r0), str(r1)])
        except ValueError as e:
            assert 'root_index' in str(e)
        else:
            raise AssertionError(f'root_index={bad_idx} 应失败')

    # video.root 显式指定优先于 watch_roots
    cfg = ad.normalize_config(
        {'draft_name': 'ac', 'video': {'root': str(r1)}})['video']
    series, hits = ad.resolve_series_dir(cfg, [str(r0)])
    assert series.startswith(str(r1)) and hits == 1
    assert cfg['root_index'] == 0


def test_draft_fs_state(tmp_path):
    d = tmp_path / 'ac'
    d.mkdir()
    assert ad.draft_fs_state(str(d))['state'] == 'missing'

    (d / 'draft_content.json').write_text('{bad json', encoding='utf-8')
    assert ad.draft_fs_state(str(d))['state'] == 'invalid'

    (d / 'draft_content.json').write_text(
        json.dumps({'duration': 0, 'tracks': [{'type': 'video', 'segments': [{}]}]}),
        encoding='utf-8')
    assert ad.draft_fs_state(str(d))['state'] == 'invalid'

    (d / 'draft_content.json').write_text(json.dumps(_draft(3)), encoding='utf-8')
    st = ad.draft_fs_state(str(d))
    assert st['state'] == 'ok' and st['video_segments'] == 3 and st['duration_us'] == 5_000_000


def test_baseline_clean_and_dirty():
    cur = ad.compute_baseline(_draft())
    baseline = {'draft_name': 'ac', **cur}
    assert ad.baseline_mismatch(baseline, cur) == []

    # 多一条音轨（人声分离/分离音频）→ 脏
    more_audio = ad.compute_baseline(_draft(extra_tracks=[{'type': 'audio', 'segments': [{}]}]))
    assert ad.baseline_mismatch(baseline, more_audio), '多音轨必须判脏'

    # 字幕素材增加 → 脏
    more_text = ad.compute_baseline(_draft(texts=50))
    diffs = ad.baseline_mismatch(baseline, more_text)
    assert any('字幕' in d for d in diffs), diffs

    # 只变主轨段数（剪映可能合并首尾相连片段）→ 不判脏
    merged = ad.compute_baseline(_draft(video_segments=1))
    assert ad.baseline_mismatch(baseline, merged) == []

    # 时长偏差 >1% → 脏
    longer = deepcopy(cur)
    longer['duration_us'] = int(cur['duration_us'] * 1.05)
    assert ad.baseline_mismatch(baseline, longer)

    # 无 baseline / 当前不可解析 → 不匹配
    assert ad.baseline_mismatch(None, cur)
    assert ad.baseline_mismatch(baseline, None)


def test_decide_action_matrix():
    assert ad.decide_action(True, True, True, False) == 'reuse'
    assert ad.decide_action(True, True, False, False) == 'reuse'
    assert ad.decide_action(True, True, True, True) == 'rebuild'
    assert ad.decide_action(False, True, True, False) == 'open'
    assert ad.decide_action(False, True, True, True) == 'rebuild'
    assert ad.decide_action(False, True, False, False) == 'retry_open'
    assert ad.decide_action(False, False, False, False) == 'rebuild'
    assert ad.decide_action(False, False, True, False) == 'rebuild'
    # 编辑页开着但磁盘目录被删/损坏 → 仍要重建（决策表第 9 档）
    assert ad.decide_action(True, False, False, False) == 'rebuild'


def test_decide_window_action_priority():
    # 中途停点：不论 close_on_exit 都默认留窗
    assert ad.decide_window_action(False, 'locate', True) == (
        'keep', '中途停点保留窗口，stop_after=locate')
    assert ad.decide_window_action(False, 'locate', False)[0] == 'keep'
    # 整轮跑完：才看 close_on_exit
    assert ad.decide_window_action(True, 'export', True)[0] == 'close'
    assert ad.decide_window_action(True, 'export', False)[0] == 'keep'
    # 显式开关覆盖一切默认决策
    assert ad.decide_window_action(False, 'locate', True, keep_window=True)[0] == 'keep'
    assert ad.decide_window_action(True, 'export', False, close_window=True)[0] == 'close'
    try:
        ad.decide_window_action(True, 'export', True,
                                keep_window=True, close_window=True)
    except ValueError as e:
        assert 'keep-window' in str(e) and 'close-window' in str(e)
    else:
        raise AssertionError('--keep-window 与 --close-window 同时给必须报错')


def test_active_draft_name(tmp_path):
    root = tmp_path / 'drafts'
    d1 = root / 'old'
    d2 = root / 'new'
    for d in (d1, d2):
        d.mkdir(parents=True)
        (d / 'draft_content.json').write_text('{}', encoding='utf-8')
    now = time.time()
    os.utime(str(d1 / 'draft_content.json'), (now - 100, now - 100))
    os.utime(str(d2 / 'draft_content.json'), (now, now))
    assert ad.active_draft_name(str(root)) == 'new'
    assert ad.active_draft_name(str(tmp_path / 'nope')) is None


def test_write_read_baseline(tmp_path):
    p = str(tmp_path / 'baseline.json')
    cur = ad.compute_baseline(_draft())
    assert ad.write_baseline(p, 'ac', cur) is not None
    got = ad.read_baseline(p, 'ac')
    assert got and got['draft_name'] == 'ac' and got['duration_us'] == cur['duration_us']
    assert ad.read_baseline(p, 'other') is None
    assert ad.read_baseline(str(tmp_path / 'missing.json'), 'ac') is None


if __name__ == '__main__':
    import inspect
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_') or not callable(fn):
            continue
        try:
            if 'tmp_path' in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f'PASS {name}')
        except Exception:
            failed += 1
            print(f'FAIL {name}')
            traceback.print_exc()
    print(f'\n{len([n for n in globals() if n.startswith("test_")]) - failed} passed, {failed} failed')
    sys.exit(1 if failed else 0)
