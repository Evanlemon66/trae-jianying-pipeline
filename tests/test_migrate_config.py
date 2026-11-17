# -*- coding: utf-8 -*-
"""migrate_config：只做确定性迁移，多个根不猜。"""
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pipeline"))

import migrate_config as mc  # noqa: E402
import export_config as ec  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_config_path():
    old = ec.CONFIG_PATH
    yield
    ec.CONFIG_PATH = old


def _cfg(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _root(tmp_path, name):
    root = tmp_path / name
    (root / "d1" / "cover_job").mkdir(parents=True)
    return root


def test_plan_single_valid_root(tmp_path):
    root = _root(tmp_path, "series")
    cfg = {"watch": {"roots": [str(root)]}, "export_dir": "E:\\out", "deps_root": "E:\\deps"}
    assert mc.plan(cfg)["series_root"] == str(root)


def test_plan_multiple_roots_requires_explicit(tmp_path):
    a = _root(tmp_path, "a")
    b = _root(tmp_path, "b")
    cfg = {"watch": {"roots": [str(a), str(b)]}}
    with pytest.raises(ValueError):
        mc.plan(cfg)
    assert mc.plan(cfg, str(a))["series_root"] == str(a)


def test_dry_run_does_not_write(tmp_path, capsys):
    root = _root(tmp_path, "series")
    path = _cfg(tmp_path / "presets.json",
                {"watch": {"roots": [str(root)]},
                 "export_dir": str(tmp_path), "deps_root": str(tmp_path)})
    before = path.read_text(encoding="utf-8")
    assert mc.main(["--dry-run", "--config", str(path)]) == 0
    assert path.read_text(encoding="utf-8") == before
    assert "dry-run" in capsys.readouterr().out


def test_write_backs_up_and_sets_series_root(tmp_path):
    root = _root(tmp_path, "series")
    path = _cfg(tmp_path / "presets.json",
                {"watch": {"roots": [str(root)]},
                 "export_dir": str(tmp_path), "deps_root": str(tmp_path)})
    assert mc.main(["--write", "--config", str(path)]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["series_root"] == str(root)
    assert list(tmp_path.glob("presets.json.bak-*"))


def test_write_refuses_missing_required(tmp_path):
    root = _root(tmp_path, "series")
    path = _cfg(tmp_path / "presets.json",
                {"watch": {"roots": [str(root)]}, "export_dir": "", "deps_root": ""})
    assert mc.main(["--write", "--config", str(path)]) == 2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("series_root") in (None, "")
