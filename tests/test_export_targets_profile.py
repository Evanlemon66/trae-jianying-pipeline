"""多账号导出目标映射：`--profile` 过滤后仍要按版本→草稿名正确取目标。

历史缺陷：`_export_targets` 假定 profiles[0] 一定是基础版本 a，只有
profiles[1:] 才加 `_<版本>` 后缀；`--profile b` 过滤后 profiles=['b']，
目标退化成基础草稿 a，导致"导 b 实际导 a"。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import cover_watch as cw  # noqa: E402

BASE = "某剧_第01-10集串流"


def _root(tmp_path, monkeypatch, clone=True):
    for name in (BASE, BASE + "_b"):
        if name.endswith("_b") and not clone:
            continue
        os.makedirs(tmp_path / name, exist_ok=True)
    monkeypatch.setenv("JY_PROJECTS_ROOT", str(tmp_path))
    return tmp_path


def test_profile_b_only(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch)
    assert cw._export_targets(BASE, ["b"], "a") == [BASE + "_b"]


def test_profile_a_only(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch)
    assert cw._export_targets(BASE, ["a"], "a") == [BASE]


def test_profile_a_and_b(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch)
    assert cw._export_targets(BASE, ["a", "b"], "a") == [BASE, BASE + "_b"]


def test_missing_clone_is_skipped(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch, clone=False)
    assert cw._export_targets(BASE, ["a", "b"], "a") == [BASE]
