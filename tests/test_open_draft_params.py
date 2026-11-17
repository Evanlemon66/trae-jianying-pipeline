# -*- coding: utf-8 -*-
"""ui_nav.open_draft 参数化回归：默认值不变 + 禁止冷启动时明确报错。"""
import inspect
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, 'pipeline_ui'), os.path.join(_ROOT, 'pipeline')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ui_nav


def test_open_draft_defaults_unchanged():
    sig = inspect.signature(ui_nav.open_draft)
    assert sig.parameters['timeout'].default == 60
    assert sig.parameters['hot_wait'].default == 30
    assert sig.parameters['cold_wait'].default == 90
    assert sig.parameters['max_launch'].default == 2
    assert sig.parameters['allow_cold_restart'].default is True


def _patch_no_card(monkeypatch):
    monkeypatch.setattr(ui_nav, '_jianying_running', lambda: True)
    monkeypatch.setattr(ui_nav, 'connect_jianying', lambda *a, **k: (object(), 'home'))
    monkeypatch.setattr(ui_nav, '_find_draft_ctrl_any', lambda _n: None)
    monkeypatch.setattr(ui_nav, 'refresh_home_list', lambda _w: None)
    monkeypatch.setattr(ui_nav, 'dprint', lambda *a, **k: None)
    monkeypatch.setattr(ui_nav.time, 'sleep', lambda _s: None)

    def _no_cold():
        raise AssertionError('allow_cold_restart=false 时不允许冷启动')

    monkeypatch.setattr(ui_nav, '_cold_restart', _no_cold)


def test_open_draft_hot_timeout_without_cold_raises(monkeypatch):
    _patch_no_card(monkeypatch)
    with pytest.raises(RuntimeError) as ei:
        ui_nav.open_draft('ui_pipeline_ac', hot_wait=0, allow_cold_restart=False)
    assert '允许冷启动' in str(ei.value) or '禁止冷启动' in str(ei.value)


def test_open_draft_not_running_without_cold_raises(monkeypatch):
    _patch_no_card(monkeypatch)
    monkeypatch.setattr(ui_nav, '_jianying_running', lambda: False)
    with pytest.raises(RuntimeError) as ei:
        ui_nav.open_draft('ui_pipeline_ac', allow_cold_restart=False)
    assert '剪映未运行' in str(ei.value)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
