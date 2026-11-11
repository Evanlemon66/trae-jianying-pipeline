# -*- coding: utf-8 -*-
"""UI 平台边界回归测试；全部使用静态检查或内存替身。"""
import ast
import importlib
import os
import sys
import types

import numpy as np
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UI_DIR = os.path.join(_ROOT, 'pipeline_ui')
if _UI_DIR not in sys.path:
    sys.path.insert(0, _UI_DIR)

import ocr_locate  # noqa: E402
import ui_calibration  # noqa: E402
import ui_core  # noqa: E402


def _has_no_window_flag(node):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'getattr' and len(node.args) == 3):
        return False
    owner, name, default = node.args
    return (isinstance(owner, ast.Name) and owner.id == 'subprocess'
            and isinstance(name, ast.Constant) and name.value == 'CREATE_NO_WINDOW'
            and isinstance(default, ast.Constant) and default.value == 0)


@pytest.mark.parametrize(('relative_path', 'command'), [
    ('pipeline_ui/ui_core.py', 'Popen'),
    ('pipeline_ui/ui_nav.py', 'tasklist'),
    ('pipeline_ui/ui_nav.py', 'taskkill'),
    ('.trae/skills/jianying-editor/examples/jy_instance.py', 'taskkill'),
])
def test_subprocess_calls_explicitly_hide_windows(relative_path, command):
    with open(os.path.join(_ROOT, relative_path), encoding='utf-8') as f:
        tree = ast.parse(f.read())
    calls = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'subprocess'):
            continue
        if command == 'Popen' and node.func.attr == 'Popen':
            calls.append(node)
        elif node.func.attr == 'run' and node.args and isinstance(node.args[0], ast.List):
            argv = node.args[0].elts
            if argv and isinstance(argv[0], ast.Constant) and argv[0].value == command:
                calls.append(node)
    assert len(calls) == 1
    flags = [kw.value for kw in calls[0].keywords if kw.arg == 'creationflags']
    assert len(flags) == 1 and _has_no_window_flag(flags[0])


@pytest.mark.parametrize('rect', [
    (100, 200, 300, 400),       # 非零原点
    (-300, -100, -220, -40),    # 负原点
])
def test_ocr_window_converts_ltrb_and_clamps_region(monkeypatch, rect):
    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    full = np.arange(height * width * 3, dtype=np.int32).reshape(height, width, 3)
    captured = []
    recognized = []

    monkeypatch.setattr(ocr_locate, 'get_window_rect', lambda _win: rect)

    def fake_capture(x, y, w, h):
        captured.append((x, y, w, h))
        return full

    def fake_ocr(img):
        recognized.append(img.copy())
        return [('目标', (10, 11), 0.9, (2, 3, 18, 19))]

    monkeypatch.setattr(ocr_locate, 'capture_region', fake_capture)
    monkeypatch.setattr(ocr_locate, 'ocr_image', fake_ocr)
    items = ocr_locate._ocr_window(object(), region=(-20, 5, 25, height + 20))

    assert captured == [(left, top, width, height)]
    assert np.array_equal(recognized[0], full[5:height, 0:25])
    assert items == [('目标', (left + 10, top + 16), 0.9,
                      (left + 2, top + 8, left + 18, top + 24))]


def test_panel_ratio_fallback_adds_window_left(monkeypatch):
    class EmptyWindow:
        ClassName = ''

        def GetChildren(self):
            return []

    monkeypatch.setattr(ui_core, 'get_window_rect',
                        lambda _win: (300, 50, 1300, 650))
    monkeypatch.setattr(ui_core, '_ctrl_rect', lambda _ctrl: None)

    def fake_get_param(_section, key):
        return {'fallback_ratio': [0.0, 0.425, 0.726],
                'combo_left_logical': 118}[key]

    monkeypatch.setattr(ui_calibration, 'get_param', fake_get_param)
    edges = ui_core.panel_edges(EmptyWindow(), s=1.0)

    assert edges['left_left'] == 300
    assert edges['left_right'] == 725
    assert edges['right_left'] == 1026
    assert edges['degraded'] is True


def _env(screen, dpi, name):
    return {'screen': screen, 'dpi_scale': dpi, 'jianying_version': '5.9',
            'jianying_dir': name, 'export_dir': name,
            'fullscreen_rect': [0, 0, *screen], 'rect_tolerance': 0}


def _complete_calibration(env):
    data = {'env': env}
    for section, fields in ui_calibration.REQUIRED_PARAMS.items():
        if section == 'env':
            continue
        sec = data.setdefault(section, {})
        for key, spec in fields.items():
            if spec == 'nonempty':
                sec[key] = 'value'
            elif isinstance(spec, int) and spec > 0:
                sec[key] = [0] * spec
            else:
                sec[key] = 0
    data['vocal_switch']['scroll_delta'] = -321
    return data


def test_secondary_monitor_env_is_used_for_all_calibration_checks(monkeypatch):
    primary = _env([1920, 1080], 1.0, 'primary')
    secondary = _env([3840, 2160], 1.5, 'secondary')
    selected_win = object()
    calibration = _complete_calibration(secondary)
    env_calls = []
    saved = []

    def fake_env_fingerprint(win=None):
        env_calls.append(win)
        return secondary if win is selected_win else primary

    monkeypatch.setattr(ui_calibration, '_CURRENT_WIN', selected_win)
    monkeypatch.setattr(ui_calibration, 'env_fingerprint', fake_env_fingerprint)
    monkeypatch.setattr(ui_calibration, 'load_calibration', lambda: calibration)
    monkeypatch.setattr(ui_calibration, 'save_calibration', lambda data: saved.append(data))

    assert ui_calibration.check_env_match(calibration) is True
    validated = ui_calibration.validate_calibration()
    assert validated['ok'], validated['errors']
    assert ui_calibration.get_param('vocal_switch', 'scroll_delta') == -321
    assert ui_calibration.update_param('audio_tab', 'coord', [900, 900]) is True
    assert saved == [calibration]
    assert env_calls == [selected_win, selected_win, selected_win, selected_win]


def test_open_draft_updates_current_window_to_new_editor(monkeypatch):
    import ui_nav

    home_win, edit_win = object(), object()
    connections = iter([(home_win, 'home'), (edit_win, 'edit')])
    selected = []
    parent = types.SimpleNamespace(Click=lambda **_kwargs: None)
    ctrl = types.SimpleNamespace(GetParentControl=lambda: parent)

    monkeypatch.setattr(ui_nav, 'connect_jianying',
                        lambda *args, **kwargs: next(connections))
    monkeypatch.setattr(ui_nav, '_find_draft_ctrl_any', lambda _name: ctrl)
    monkeypatch.setattr(ui_nav, 'op_mark', lambda *args: None)
    monkeypatch.setattr(ui_nav, 'dprint', lambda *args: None)
    monkeypatch.setattr(ui_nav.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(ui_calibration, 'set_current_win', selected.append)

    ui_nav.open_draft('demo', timeout=1)

    assert selected == [edit_win]


def test_empty_cut_track_is_blocked_before_save_or_publish(monkeypatch):
    pipeline = types.ModuleType('pipeline')
    for name in ('create_empty_draft', 'inject_videos_into_draft',
                 'merge_preset_into_draft', 'publish_staged_draft',
                 'find_preset_by_name', 'save_draft'):
        setattr(pipeline, name, lambda *args, **kwargs: None)
    pipeline.DRAFT_ROOT = 'unused'
    monkeypatch.setitem(sys.modules, 'pipeline', pipeline)
    monkeypatch.delitem(sys.modules, 'init_draft', raising=False)
    module = importlib.import_module('init_draft')

    draft = {
        'duration': 10_000_000,
        'tracks': [
            {'type': 'video', 'segments': [
                {'target_timerange': {'start': 0, 'duration': 10_000_000}},
            ]},
            {'type': 'text', 'segments': []},
        ],
    }
    monkeypatch.setattr(module.glob, 'glob', lambda _pattern: [r'fake\第1集.mp4'])
    monkeypatch.setattr(module, 'create_empty_draft',
                        lambda *args, **kwargs: ('stage', 'content-path', draft))
    monkeypatch.setattr(module, 'inject_videos_into_draft',
                        lambda *args, **kwargs: [{'duration_us': 10_000_000}])
    monkeypatch.setattr(ui_core, 'op_stage', lambda *_args: None)
    def crop_main_track(current, _cut_us):
        current['tracks'][0]['segments'] = []
        current['tracks'][1]['segments'] = [
            {'target_timerange': {'start': 0, 'duration': 2_000_000}},
        ]
        return ['cut']

    def all_track_duration(current):
        return max(
            (segment['target_timerange']['start'] + segment['target_timerange']['duration']
             for track in current['tracks'] for segment in track['segments']),
            default=0,
        )

    pipeline.crop_before_us = crop_main_track
    pipeline.rebase_timeline = lambda _draft, _cut_us: 0
    pipeline.get_main_video_duration = all_track_duration

    def forbidden(*_args, **_kwargs):
        raise AssertionError('空轨不得保存或发布')

    monkeypatch.setattr(module, 'save_draft', forbidden)
    monkeypatch.setattr(module, 'publish_staged_draft', forbidden)
    result = module.init_draft(video_dir='fake', presets=[], video_limit=None,
                               cut_s=10, open_after=False)

    assert result['ok'] is False
    assert result['episodes'] == 1
    assert result['duration_s'] == 0.0
    assert '裁剪后主轨时长为 0' in result['error']
    assert '已取消保存和发布' in result['error']
