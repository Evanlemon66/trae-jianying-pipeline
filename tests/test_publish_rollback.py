# -*- coding: utf-8 -*-
"""publish_staged_draft 的失败回滚与 .removed_* 残留清理单测。

只操作临时目录里的假草稿，不启动剪映、不触发导出。
"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PIPELINE = os.path.join(_ROOT, 'pipeline')
if _PIPELINE not in sys.path:
    sys.path.insert(0, _PIPELINE)

import pipeline as pl


def _write_draft(d, draft_name):
    os.makedirs(str(d), exist_ok=True)
    payload = {
        'id': 'id-1',
        'name': draft_name,
        'duration': 1_000_000,
        'tracks': [{'type': 'video', 'segments': [{}]}],
    }
    Path(os.path.join(str(d), 'draft_content.json')).write_text(
        json.dumps(payload), encoding='utf-8')


def _patch_common(monkeypatch, root):
    monkeypatch.setattr(pl, 'DRAFT_ROOT', str(root))
    monkeypatch.setattr(pl, '_jianying_running', lambda: False)
    monkeypatch.setattr(pl, 'mark_export_stale', lambda *a, **k: None)


def test_publish_rollback_on_move_failure(tmp_path, monkeypatch):
    root = tmp_path / 'drafts'
    root.mkdir()
    target = root / 'ui_pipeline_ac'
    _write_draft(target, 'ui_pipeline_ac')
    stage = tmp_path / 'stage'
    _write_draft(stage, 'ui_pipeline_ac')
    _patch_common(monkeypatch, root)

    def _boom(src, dst):
        raise OSError('move failed')

    monkeypatch.setattr(pl.shutil, 'move', _boom)
    try:
        pl.publish_staged_draft(str(stage), 'ui_pipeline_ac')
    except OSError as e:
        assert 'move failed' in str(e)
    else:
        raise AssertionError('move 失败应向上抛出')

    # 旧草稿必须被改名还原，不能出现「目标目录消失」
    assert os.path.isfile(str(target / 'draft_content.json')), '回滚后旧草稿应回到原路径'
    leftovers = [n for n in os.listdir(str(root)) if '.removed_' in n]
    assert leftovers == [], f'回滚后不应留 .removed_ 残留: {leftovers}'


def test_publish_cleans_stale_removed_only_same_name(tmp_path, monkeypatch):
    root = tmp_path / 'drafts'
    root.mkdir()
    target = root / 'ui_pipeline_ac'
    _write_draft(target, 'ui_pipeline_ac')

    stale = root / 'ui_pipeline_ac.removed_20200101_000000'
    _write_draft(stale, 'ui_pipeline_ac')
    os.utime(str(stale), (1, 1))          # 早于本次操作

    other = root / 'other.removed_20200101_000000'
    _write_draft(other, 'other')
    os.utime(str(other), (1, 1))

    stage = tmp_path / 'stage'
    _write_draft(stage, 'ui_pipeline_ac')
    _patch_common(monkeypatch, root)

    pl.publish_staged_draft(str(stage), 'ui_pipeline_ac')

    assert os.path.isfile(str(target / 'draft_content.json'))
    assert not stale.exists(), '本名历史残留应被清理'
    assert other.exists(), '不得清理其它草稿的残留'
    leftovers = [n for n in os.listdir(str(root)) if n.startswith('ui_pipeline_ac.removed_')]
    assert leftovers == [], f'发布后不应留本名残留: {leftovers}'


def test_cleanup_skips_when_target_missing(tmp_path, monkeypatch):
    root = tmp_path / 'drafts'
    root.mkdir()
    stale = root / 'ui_pipeline_ac.removed_20200101_000000'
    _write_draft(stale, 'ui_pipeline_ac')
    os.utime(str(stale), (1, 1))
    _patch_common(monkeypatch, root)

    removed = pl._cleanup_removed_leftovers('ui_pipeline_ac', time.time())
    assert removed == []
    assert stale.exists(), '目标不存在（回滚窗口）时不能清残留'


def test_cleanup_draft_temp_files_keeps_locked(tmp_path):
    d = tmp_path / 'ui_pipeline_ac'
    d.mkdir()
    keep = ['draft_content.json', 'draft_meta_info.json', '.locked', 'draft_cover.jpg']
    junk = ['template.tmp', 'template-2.tmp', 'draft_content.json.bak']
    for n in keep + junk:
        (d / n).write_bytes(b'x')

    removed = pl.cleanup_draft_temp_files(str(d))

    assert sorted(os.path.basename(p) for p in removed) == sorted(junk)
    for n in keep:
        assert (d / n).exists(), f'{n} 不应被清理'
    for n in junk:
        assert not (d / n).exists(), f'{n} 应被清理'
    # 目录不存在时静默返回空
    assert pl.cleanup_draft_temp_files(str(tmp_path / 'nope')) == []


if __name__ == '__main__':
    import inspect
    failed = 0

    class _MP:
        """极简 monkeypatch（pytest 外的独立运行兜底）。"""
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)
            self._undo = []

    for name, fn in sorted(globals().items()):
        if not name.startswith('test_') or not callable(fn):
            continue
        mp = _MP()
        try:
            if 'tmp_path' in inspect.signature(fn).parameters:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td), mp)
            else:
                fn(mp)
            print(f'PASS {name}')
        except Exception:
            failed += 1
            print(f'FAIL {name}')
            traceback.print_exc()
        finally:
            mp.undo()
    print(f'\n{failed} failed')
    sys.exit(1 if failed else 0)
