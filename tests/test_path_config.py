# -*- coding: utf-8 -*-
"""统一路径解析：唯一配置源 + 缺失即报错 + watcher 额外根。"""
import json
import os
import sys

import pytest

import export_config as ec


@pytest.fixture(autouse=True)
def _reset_warnings():
    ec._WARNED.clear()
    yield
    ec._WARNED.clear()


@pytest.fixture
def presets(tmp_path, monkeypatch):
    cfg = tmp_path / "presets.json"
    monkeypatch.setattr(ec, "CONFIG_PATH", str(cfg))

    def write(data):
        cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    return write, tmp_path


def _clean_env(monkeypatch):
    for name in ("JY_EXPORT_DIR", "JY_DEPS_ROOT", "JY_JIANYING_DIR", "JY_PROJECTS_ROOT",
                 "JY_PRESETS_ROOT", "JY_JIANYING_LOG_DIR", "JY_SERIES_ROOT",
                 "JY_ARCHIVE_ROOT"):
        monkeypatch.delenv(name, raising=False)


def _series(tmp_path, name="series", dramas=("d1",)):
    root = tmp_path / name
    root.mkdir()
    for drama in dramas:
        (root / drama / "cover_job").mkdir(parents=True)
    return root


def test_series_root_is_required(presets, monkeypatch):
    write, _tmp = presets
    write({})
    _clean_env(monkeypatch)
    with pytest.raises(ec.ConfigError) as exc:
        ec.get_series_root()
    assert exc.value.key == "series_root"


def test_series_root_does_not_fall_back_to_watch_roots(presets, monkeypatch, tmp_path):
    write, _tmp = presets
    write({"watch": {"roots": [str(_series(tmp_path))]}})
    _clean_env(monkeypatch)
    with pytest.raises(ec.ConfigError):
        ec.get_series_root()


def test_series_root_env_wins_and_invalid_env_raises(presets, monkeypatch, tmp_path):
    write, tmp = presets
    cfg_root = _series(tmp, "cfg-series")
    env_root = _series(tmp, "env-series")
    write({"series_root": str(cfg_root)})
    monkeypatch.setenv("JY_SERIES_ROOT", str(env_root))
    assert ec.get_series_root() == str(env_root)

    monkeypatch.setenv("JY_SERIES_ROOT", str(tmp / "missing"))
    with pytest.raises(ec.ConfigError):
        ec.get_series_root()


def test_watch_roots_empty_returns_series_only(presets, monkeypatch, tmp_path):
    write, tmp = presets
    root = _series(tmp)
    write({"series_root": str(root), "watch": {"roots": []}})
    _clean_env(monkeypatch)
    assert ec.get_watch_roots() == [str(root)]


def test_watch_roots_extra_must_exist(presets, monkeypatch, tmp_path):
    write, tmp = presets
    root = _series(tmp)
    write({"series_root": str(root), "watch": {"roots": [str(tmp / "missing")]}})
    _clean_env(monkeypatch)
    with pytest.raises(ec.ConfigError) as exc:
        ec.get_watch_roots()
    assert exc.value.key == "watch.roots"


def test_watch_roots_extra_requires_cover_job(presets, monkeypatch, tmp_path):
    write, tmp = presets
    root = _series(tmp)
    empty = tmp / "extra-empty"
    empty.mkdir()
    write({"series_root": str(root), "watch": {"roots": [str(empty)]}})
    _clean_env(monkeypatch)
    with pytest.raises(ec.ConfigError):
        ec.get_watch_roots()


def test_watch_roots_extra_merges_and_dedupes(presets, monkeypatch, tmp_path):
    write, tmp = presets
    root = _series(tmp, "series", ("d1",))
    extra = _series(tmp, "extra", ("d2",))
    write({"series_root": str(root), "watch": {"roots": [str(extra), str(root)]}})
    _clean_env(monkeypatch)
    assert ec.get_watch_roots() == [str(root), str(extra)]


def test_export_dir_and_deps_root_are_required(presets, monkeypatch):
    write, _tmp = presets
    write({})
    _clean_env(monkeypatch)
    with pytest.raises(ec.ConfigError):
        ec.get_export_dir()
    with pytest.raises(ec.ConfigError):
        ec.get_deps_root()


def test_export_dir_allows_not_yet_created_target(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    target = tmp / "new-export-root"
    monkeypatch.setenv("JY_EXPORT_DIR", str(target))
    assert ec.get_export_dir() == str(target)
    assert not target.exists()


def test_archive_root_optional_and_legacy_alias(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    _clean_env(monkeypatch)
    assert ec.get_archive_root() == ""

    legacy = tmp / "archive"
    legacy.mkdir()
    write({"watch": {"archive_root": str(legacy)}})
    assert ec.get_archive_root() == str(legacy)


def test_config_wins_over_autodetect_for_derived_roots(presets, monkeypatch, tmp_path):
    write, tmp = presets
    cfg_root = tmp / "cfg"
    cfg_root.mkdir()
    write({"projects_root": str(cfg_root)})
    _clean_env(monkeypatch)
    auto = tmp / "auto"
    auto.mkdir()
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [str(auto)])
    assert ec.get_projects_root() == str(cfg_root)


def test_explicit_missing_projects_root_is_returned(presets, monkeypatch, tmp_path):
    # 草稿根允许尚未创建（首次启动剪映前），显式配置原样返回。
    write, tmp = presets
    target = str(tmp / "missing-draft-root")
    write({"projects_root": target})
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [])
    assert ec.get_projects_root() == target


def test_invalid_explicit_jianying_dir_raises(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({"jianying_dir": str(tmp / "missing")})
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_autodetect_jianying_dirs", lambda: [])
    with pytest.raises(ec.ConfigError):
        ec.get_jianying_dir()


def test_autodetect_then_system_derived_default(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    _clean_env(monkeypatch)
    auto = tmp / "auto"
    auto.mkdir()
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [str(auto)])
    assert ec.get_projects_root() == str(auto)

    monkeypatch.setattr(ec, "_projects_candidates", lambda: [])
    monkeypatch.setattr(ec, "DEFAULT_PROJECTS_ROOT", str(tmp / "system-default"))
    assert ec.get_projects_root() == str(tmp / "system-default")


def test_resolve_paths_and_getters_are_consistent(presets, monkeypatch, tmp_path):
    write, tmp = presets
    root = _series(tmp)
    export = tmp / "export"
    deps = tmp / "deps"
    archive = tmp / "archive"
    for path in (export, deps, archive):
        path.mkdir()
    write({"series_root": str(root), "export_dir": str(export),
           "deps_root": str(deps), "archive_root": str(archive),
           "watch": {"roots": []}})
    _clean_env(monkeypatch)
    snap = ec.resolve_paths("run")
    assert not snap["issues"]
    assert snap["paths"]["series_root"] == ec.get_series_root() == str(root)
    assert snap["paths"]["export_dir"] == ec.get_export_dir() == str(export)
    assert snap["paths"]["deps_root"] == ec.get_deps_root() == str(deps)
    assert snap["paths"]["archive_root"] == ec.get_archive_root() == str(archive)
    assert snap["sources"]["series_root"] == "presets.json"


def test_validate_config_reports_issue(presets, monkeypatch):
    write, _tmp = presets
    write({"export_dir": "", "deps_root": ""})
    _clean_env(monkeypatch)
    report = ec.validate_config("run")
    assert report["ok"] is False
    keys = {item["key"] for item in report["issues"]}
    assert "series_root" in keys


def test_admission_root_prefers_explicit_and_rejects_root_index(tmp_path, monkeypatch):
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(repo, "pipeline_ui"))
    import admission_draft

    series = _series(tmp_path)
    explicit = _series(tmp_path, "explicit")
    monkeypatch.setattr(ec, "get_series_root", lambda: str(series))
    assert admission_draft._root_of({"root": str(explicit)}) == str(explicit)
    assert admission_draft._root_of({}) == str(series)
    with pytest.raises(ValueError):
        admission_draft._root_of({"root_index": 1})
