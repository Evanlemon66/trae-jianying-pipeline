# -*- coding: utf-8 -*-
"""Console Runner：剪辑任务（batch 多预设组）的封面兜底参数贯通。

导出任务的多预设走 _rebase_then_export（已有 --cover-fallback），
剪辑任务的多预设走 batch_runner 的 --profile 克隆，这里保证两边口径一致：
勾选“缺少预设组封面时，复用源草稿封面” → 命令里出现 --cover-fallback source。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSOLE = os.path.join(_ROOT, "pipeline_ui", "console")
if _CONSOLE not in sys.path:
    sys.path.insert(0, _CONSOLE)

import runner  # noqa: E402


@pytest.fixture(autouse=True)
def _skip_path_whitelist(monkeypatch):
    """只测参数/命令拼装，不碰真实业务根：绕开路径白名单校验。"""
    monkeypatch.setattr(runner.paths, "check",
                        lambda p, **kw: os.path.abspath(str(p)))


def _payload(**kw):
    base = {
        "mode": "batch", "series_dir": _ROOT, "selection": {"mode": "all"},
        "cut": 0, "cover_image": "", "profiles": ["presets_a", "presets_b"],
        "export": True, "draft_prefix": "T", "retries": 1,
        "export_timeout": 60, "delay": None,
    }
    base.update(kw)
    return base


def _cmd(payload):
    params = runner.Runner.normalize(payload)
    return params, runner.Runner.build_command(params, "preview")[0]


def test_multi_profile_cover_fallback_source():
    params, cmd = _cmd(_payload(cover_fallback="source"))
    assert params["cover_fallback"] == "source"
    assert cmd[cmd.index("--profile") + 1] == "a,b"
    assert cmd[cmd.index("--cover-fallback") + 1] == "source"


def test_multi_profile_default_abort():
    params, cmd = _cmd(_payload())
    assert params["cover_fallback"] == "abort"
    assert "--cover-fallback" not in cmd


def test_single_profile_ignores_cover_fallback():
    params, cmd = _cmd(_payload(profiles=["presets_a"], cover_fallback="source"))
    assert "--cover-fallback" not in cmd
