# -*- coding: utf-8 -*-
"""统一路径解析顺序：环境变量 > presets.json > 自动探测 > 内置默认值。"""
import json
import os

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
                 "JY_ARCHIVE_ROOT", "JY_UI_PYLIBS"):
        monkeypatch.delenv(name, raising=False)


def test_builtin_defaults_are_module_constants():
    # 不写死本机路径：只校验默认值确实是模块级常量且非空，换机器也能跑
    for name in ("DEFAULT_EXPORT_DIR", "DEFAULT_DEPS_ROOT", "DEFAULT_SERIES_ROOT",
                 "DEFAULT_ARCHIVE_ROOT", "DEFAULT_PROJECTS_ROOT",
                 "DEFAULT_PRESETS_ROOT", "DEFAULT_JIANYING_LOG_DIR"):
        value = getattr(ec, name)
        assert isinstance(value, str) and value.strip()


def test_paths_fall_back_to_module_constants(presets, monkeypatch):
    write, _tmp = presets
    write({})
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [])
    monkeypatch.setattr(ec, "_presets_candidates", lambda: [])
    monkeypatch.setattr(ec, "_log_candidates", lambda: [])
    assert ec.get_projects_root() == ec.DEFAULT_PROJECTS_ROOT
    assert ec.get_presets_root() == ec.DEFAULT_PRESETS_ROOT
    assert ec.get_jianying_log_dir() == ec.DEFAULT_JIANYING_LOG_DIR
    assert ec.get_export_dir() == ec.DEFAULT_EXPORT_DIR
    assert ec.get_deps_root() == ec.DEFAULT_DEPS_ROOT
    assert ec.get_series_root() == ec.DEFAULT_SERIES_ROOT
    assert ec.get_archive_root() == ec.DEFAULT_ARCHIVE_ROOT


def test_env_wins_over_config(presets, monkeypatch):
    write, tmp = presets
    env_root, cfg_root = tmp / "env", tmp / "cfg"
    env_root.mkdir()
    cfg_root.mkdir()
    write({"projects_root": str(cfg_root)})
    monkeypatch.setenv("JY_PROJECTS_ROOT", str(env_root))
    assert ec.get_projects_root() == str(env_root)


def test_config_wins_over_autodetect(presets, monkeypatch, tmp_path):
    write, tmp = presets
    cfg_root = tmp / "cfg"
    cfg_root.mkdir()
    write({"projects_root": str(cfg_root)})
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [str(tmp / "auto")])
    (tmp / "auto").mkdir()
    assert ec.get_projects_root() == str(cfg_root)


def test_autodetect_wins_over_default(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    _clean_env(monkeypatch)
    auto = tmp / "auto"
    auto.mkdir()
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [str(auto)])
    monkeypatch.setattr(ec, "DEFAULT_PROJECTS_ROOT", str(tmp / "default"))
    assert ec.get_projects_root() == str(auto)


def test_default_is_last_resort(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [str(tmp / "missing")])
    monkeypatch.setattr(ec, "DEFAULT_PROJECTS_ROOT", str(tmp / "default"))
    assert ec.get_projects_root() == str(tmp / "default")


def test_invalid_env_warns_then_falls_through(presets, monkeypatch, capsys, tmp_path):
    write, tmp = presets
    cfg_root = tmp / "cfg"
    cfg_root.mkdir()
    write({"projects_root": str(cfg_root)})
    monkeypatch.setenv("JY_PROJECTS_ROOT", str(tmp / "does-not-exist"))
    assert ec.get_projects_root() == str(cfg_root)
    err = capsys.readouterr().err
    assert "JY_PROJECTS_ROOT" in err
    assert "不存在" in err or "不是可用" in err


def test_invalid_config_warns_then_autodetects(presets, monkeypatch, capsys, tmp_path):
    write, tmp = presets
    write({"projects_root": str(tmp / "bad-config")})
    _clean_env(monkeypatch)
    auto = tmp / "auto"
    auto.mkdir()
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [str(auto)])
    assert ec.get_projects_root() == str(auto)
    err = capsys.readouterr().err
    assert "projects_root" in err
    assert "不存在" in err or "不是可用" in err


def test_export_dir_allows_not_yet_created_target(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    target = tmp / "new-export-root"
    monkeypatch.setenv("JY_EXPORT_DIR", str(target))
    assert ec.get_export_dir() == str(target)
    assert not target.exists()


def test_series_root_uses_watch_roots(presets, monkeypatch, tmp_path):
    write, tmp = presets
    watch_root = tmp / "series"
    watch_root.mkdir()
    write({"watch": {"roots": [str(watch_root)]}})
    _clean_env(monkeypatch)
    assert ec.get_series_root() == str(watch_root)


def test_archive_root_uses_watch_archive(presets, monkeypatch, tmp_path):
    write, tmp = presets
    archive = tmp / "archive"
    archive.mkdir()
    write({"watch": {"archive_root": str(archive)}})
    _clean_env(monkeypatch)
    assert ec.get_archive_root() == str(archive)


def test_ui_pylibs_env_wins_over_config(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({"ui_pylibs": str(tmp / "cfg-ui")})
    _clean_env(monkeypatch)
    env_dir = tmp / "env-ui"
    monkeypatch.setenv("JY_UI_PYLIBS", str(env_dir))
    # 输入型依赖目录也允许显式指定尚未创建的路径（供 install_deps 首次安装）
    assert ec.get_ui_pylibs_dir() == str(env_dir)


def test_ui_pylibs_config_wins_over_autodetect(presets, monkeypatch, tmp_path):
    write, tmp = presets
    cfg_dir = tmp / "cfg-ui"
    write({"ui_pylibs": str(cfg_dir)})
    _clean_env(monkeypatch)
    auto = tmp / "auto-ui"
    auto.mkdir()
    monkeypatch.setattr(ec, "_ui_pylibs_candidates", lambda: [str(auto)])
    assert ec.get_ui_pylibs_dir() == str(cfg_dir)


def test_ui_pylibs_autodetect_wins_over_default(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    _clean_env(monkeypatch)
    auto = tmp / "auto-ui"
    auto.mkdir()
    monkeypatch.setattr(ec, "_ui_pylibs_candidates", lambda: [str(auto), str(tmp / "missing-ui")])
    assert ec.get_ui_pylibs_dir() == str(auto)


def test_ui_pylibs_default_is_last_resort(presets, monkeypatch, tmp_path):
    write, tmp = presets
    write({})
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_ui_pylibs_candidates", lambda: [str(tmp / "missing-ui")])
    assert ec.get_ui_pylibs_dir() == ec.DEFAULT_UI_PYLIBS
    assert ec.DEFAULT_UI_PYLIBS == os.path.join(ec.DEFAULT_DEPS_ROOT, "ui_pylibs")


def test_missing_presets_file_uses_defaults(presets, monkeypatch, tmp_path):
    # presets fixture 指向的文件从未创建
    _clean_env(monkeypatch)
    monkeypatch.setattr(ec, "_projects_candidates", lambda: [])
    monkeypatch.setattr(ec, "DEFAULT_PROJECTS_ROOT", str(tmp_path / "draft-default"))
    assert ec.get_projects_root() == str(tmp_path / "draft-default")
