# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from pipeline_ui import ui_pipeline  # noqa: E402


_EXPORT_SCRIPT = os.path.join(
    str(_ROOT), '.trae', 'skills', 'jianying-editor', 'examples', 'robust_auto_export.py')


def _stub_export(monkeypatch, tmp_path, *, script_exists=True, lock_ok=True, timeout=60):
    acquire = Mock(return_value=lock_ok)
    release = Mock()
    rmtree = Mock()
    remove = Mock()
    monkeypatch.setattr(ui_pipeline.export_lock, 'acquire', acquire)
    monkeypatch.setattr(ui_pipeline.export_lock, 'release', release)
    monkeypatch.setattr(shutil, 'rmtree', rmtree)
    monkeypatch.setattr(ui_pipeline.os, 'remove', remove)

    real_exists = os.path.exists
    script_key = os.path.normcase(os.path.abspath(_EXPORT_SCRIPT))

    def fake_exists(path):
        if os.path.normcase(os.path.abspath(path)) == script_key:
            return script_exists
        return real_exists(path)

    monkeypatch.setattr(ui_pipeline.os.path, 'exists', fake_exists)
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setitem(sys.modules, 'ui_recognize_translate',
                        SimpleNamespace(DRAFT_ROOT=str(tmp_path / 'no_drafts')))
    monkeypatch.setattr(ui_pipeline, '_step_timeout', lambda *_args: timeout)
    return SimpleNamespace(acquire=acquire, release=release, rmtree=rmtree, remove=remove)


def test_missing_export_script_is_failure(monkeypatch, tmp_path):
    env = _stub_export(monkeypatch, tmp_path, script_exists=False)
    popen = Mock()
    monkeypatch.setattr(subprocess, 'Popen', popen)

    result = ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    assert result['ok'] is False
    assert result['error'] == 'script_not_found'
    popen.assert_not_called()
    env.release.assert_called_once_with()


@pytest.mark.parametrize(('pipeline_ok', 'exit_code'), [(False, 1), (True, 0)])
def test_main_returns_pipeline_exit_code(monkeypatch, pipeline_ok, exit_code):
    import ui_core

    monkeypatch.setattr(sys, 'argv', ['ui_pipeline.py'])
    monkeypatch.setattr(ui_core, 'install_timestamp_stdout', lambda: None)
    monkeypatch.setattr(ui_pipeline, 'run_pipeline_ui', Mock(return_value=pipeline_ok))
    power = Mock()
    monkeypatch.setattr(ui_pipeline, '_power_keepalive', power)

    assert ui_pipeline.main() == exit_code
    assert power.call_args_list == [call(True), call(False)]


def test_busy_export_lock_skips_cleanup_and_process(monkeypatch, tmp_path):
    env = _stub_export(monkeypatch, tmp_path, lock_ok=False)
    popen = Mock()
    monkeypatch.setattr(subprocess, 'Popen', popen)
    old_output = tmp_path / 'demo.mp4'
    old_output.write_text('old', encoding='utf-8')
    temp_dir = tmp_path / 'Videos' / '.__jianying_export_temp_folder__'
    temp_dir.mkdir(parents=True)

    result = ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    assert result['ok'] is False
    assert result['error'] == 'export_lock_busy'
    env.rmtree.assert_not_called()
    env.remove.assert_not_called()
    popen.assert_not_called()
    env.release.assert_not_called()
    assert old_output.read_text(encoding='utf-8') == 'old'
    assert temp_dir.is_dir()


def test_export_timeout_stops_jianying_before_wrapper(monkeypatch, tmp_path):
    env = _stub_export(monkeypatch, tmp_path, timeout=0)
    events = []

    class FakeJyInstance:
        def kill(self):
            events.append('jianying_kill')

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            events.append('wrapper_kill')
            self.returncode = -1

        def communicate(self):
            events.append('wrapper_communicate')
            return '', ''

        def wait(self, timeout):
            raise AssertionError('超时时不应再等待包装进程')

    monkeypatch.setattr(ui_pipeline, 'JyInstance', FakeJyInstance)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=FakeProcess()))

    result = ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    assert result['ok'] is False
    assert result['error'] == 'export_timeout_0s'
    assert events == ['jianying_kill', 'wrapper_kill', 'wrapper_communicate']
    env.release.assert_called_once_with()


@pytest.mark.parametrize('error', [RuntimeError('wait failed'), KeyboardInterrupt()])
def test_export_exception_cleans_live_process_before_lock_release(
        monkeypatch, tmp_path, error):
    env = _stub_export(monkeypatch, tmp_path)
    events = []
    env.release.side_effect = lambda: events.append('release')

    class FakeJyInstance:
        def kill(self):
            events.append('jianying_kill')

    class LiveProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            raise error

        def kill(self):
            events.append('wrapper_kill')
            self.returncode = -1

        def communicate(self):
            events.append('wrapper_communicate')
            return '', ''

    monkeypatch.setattr(ui_pipeline, 'JyInstance', FakeJyInstance)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=LiveProcess()))

    if isinstance(error, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            ui_pipeline.step_export(str(tmp_path), draft_name='demo')
    else:
        result = ui_pipeline.step_export(str(tmp_path), draft_name='demo')
        assert result['ok'] is False
        assert result['error'] == 'wait failed'

    assert events == ['jianying_kill', 'wrapper_kill', 'wrapper_communicate', 'release']
    env.release.assert_called_once_with()


@pytest.mark.parametrize('error', [KeyboardInterrupt(), SystemExit()])
def test_cleanup_interrupt_continues_reaping_then_releases_and_raises(
        monkeypatch, tmp_path, error):
    env = _stub_export(monkeypatch, tmp_path, timeout=0)
    events = []
    env.release.side_effect = lambda: events.append('release')

    class InterruptedJyInstance:
        def kill(self):
            events.append('jianying_kill')
            raise error

    class FakeProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            events.append('wrapper_kill')
            self.returncode = -1

        def communicate(self):
            events.append('wrapper_communicate')
            return '', ''

    monkeypatch.setattr(ui_pipeline, 'JyInstance', InterruptedJyInstance)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=FakeProcess()))

    with pytest.raises(type(error)):
        ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    assert events == ['jianying_kill', 'wrapper_kill', 'wrapper_communicate', 'release']
    env.release.assert_called_once_with()


def test_first_cleanup_interrupt_is_preserved(monkeypatch, tmp_path):
    env = _stub_export(monkeypatch, tmp_path, timeout=0)

    class InterruptedJyInstance:
        def kill(self):
            raise SystemExit('second interrupt')

    class FakeProcess:
        returncode = None
        poll_calls = 0

        def poll(self):
            self.poll_calls += 1
            if self.poll_calls == 1:
                raise KeyboardInterrupt('first interrupt')
            return self.returncode

        def kill(self):
            self.returncode = -1

        def communicate(self):
            return '', ''

    monkeypatch.setattr(ui_pipeline, 'JyInstance', InterruptedJyInstance)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=FakeProcess()))

    with pytest.raises(KeyboardInterrupt, match='first interrupt'):
        ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    env.release.assert_called_once_with()


def test_interrupted_wrapper_kill_keeps_lock_while_process_is_alive(monkeypatch, tmp_path):
    env = _stub_export(monkeypatch, tmp_path, timeout=0)
    events = []

    class FakeJyInstance:
        def kill(self):
            events.append('jianying_kill')

    class LiveProcess:
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            events.append('wrapper_kill')
            raise KeyboardInterrupt()

        def communicate(self):
            events.append('wrapper_communicate')
            return '', ''

    monkeypatch.setattr(ui_pipeline, 'JyInstance', FakeJyInstance)
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=LiveProcess()))

    with pytest.raises(KeyboardInterrupt):
        ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    assert events == ['jianying_kill', 'wrapper_kill', 'wrapper_communicate']
    env.release.assert_not_called()


def test_export_popen_uses_no_window_creationflags(monkeypatch, tmp_path):
    env = _stub_export(monkeypatch, tmp_path)
    captured = {}

    class SuccessfulProcess:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            return 0

        def communicate(self):
            return '', ''

    def fake_popen(*args, **kwargs):
        captured['kwargs'] = kwargs
        return SuccessfulProcess()

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)

    result = ui_pipeline.step_export(str(tmp_path), draft_name='demo')

    assert result['ok'] is True
    assert captured['kwargs']['creationflags'] == getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    assert captured['kwargs']['env']['JY_HIDE_WINDOW'] == '1'
    assert str(_ROOT / 'pipeline') in captured['kwargs']['env']['PYTHONPATH'].split(os.pathsep)
    env.release.assert_called_once_with()


@pytest.mark.parametrize('selector', [
    {'only': ['vocal_separate']},
    {'from_step': 'vocal_separate'},
])
def test_pipeline_initializes_current_window_when_precheck_is_skipped(
        monkeypatch, tmp_path, selector):
    import ui_core

    win = object()
    events = []
    monkeypatch.setattr(ui_pipeline, 'connect_jianying', lambda: (win, 'edit'))
    monkeypatch.setattr(ui_core, 'set_screen_policy', lambda _policy: None)
    monkeypatch.setattr(ui_pipeline.ui_calibration, 'set_current_win',
                        lambda current: events.append(('window', current)))
    monkeypatch.setattr(ui_pipeline.ui_calibration, 'take_trace', lambda: None)
    monkeypatch.setattr(ui_pipeline, '_step_timeout', lambda *_args: None)
    monkeypatch.setattr(ui_pipeline, 'append_timing', lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_pipeline, 'op_timeline', lambda *args, **kwargs: None)
    monkeypatch.setattr(ui_pipeline, 'take_op_marks', lambda: None)
    monkeypatch.setattr(ui_core, 'stage_timeline', lambda: None)

    def fail_step(**_kwargs):
        events.append(('step', 'vocal_separate'))
        return {'ok': False, 'duration_s': 0, 'error': 'stop after probe'}

    monkeypatch.setitem(ui_pipeline.STEP_IMPL, 'vocal_separate', fail_step)

    assert ui_pipeline.run_pipeline_ui(workdir=str(tmp_path), **selector) is False
    assert events == [('window', win), ('step', 'vocal_separate')]
