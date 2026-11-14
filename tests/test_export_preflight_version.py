# -*- coding: utf-8 -*-
"""回归用例：导出预检的剪映版本门槛。

要求（版本门槛）：
  - 版本不在支持范围时在**预检阶段**明确拒绝，并说明支持版本；
  - 该失败不可重试（重试不会改变剪映版本）；
  - 预检未通过时不得提前置 `_PREFLIGHT_DONE`，否则下一轮会跳过预检直接启剪映。

不启停剪映、不跑导出：只替换版本解析函数，run() 在预检处即失败退出。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.join(_ROOT, ".trae", "skills", "jianying-editor", "examples"))

import failure_class as fc        # noqa: E402
import jianying_launch as jl      # noqa: E402
import robust_auto_export as rae  # noqa: E402


def test_version_preflight_message_is_terminal():
    """版本预检失败走 version_unsupported：不可重试、不计连续失败。"""
    msg = (f"剪映版本预检不通过（仅支持 {jl.REQUIRED_JY_VERSION}）："
           f"未找到剪映 {jl.REQUIRED_JY_VERSION}（不会静默拉起其它版本）")
    category, strat = rae._classify_failure(msg)
    assert category == fc.VERSION_UNSUPPORTED
    assert strat["retryable"] is False


def test_version_message_not_misread_as_ui_hang():
    """消息里带「未找到」也不能落到可重试的 ui_hang。"""
    msg = f"未找到剪映 {jl.REQUIRED_JY_VERSION}（不会静默拉起其它版本）"
    category, _strat = fc.classify(msg, phase="cold_start")
    assert category == fc.VERSION_UNSUPPORTED


def test_preflight_flag_stays_false_when_version_rejected(monkeypatch, tmp_path):
    """版本预检失败后 `_PREFLIGHT_DONE` 必须仍为 False，下一轮会重跑预检。"""

    def _boom(log_fn=None):
        raise jl.JianyingLaunchError(
            f"未找到剪映 {jl.REQUIRED_JY_VERSION}（不会静默拉起其它版本）")

    monkeypatch.setattr(jl, "resolve_jianying_exe", _boom)
    rae._PREFLIGHT_DONE[0] = False

    with pytest.raises(Exception) as excinfo:
        rae.run("不存在的草稿", str(tmp_path / "out.mp4"))

    assert "剪映版本预检不通过" in str(excinfo.value)
    assert f"仅支持 {jl.REQUIRED_JY_VERSION}" in str(excinfo.value)
    assert rae._PREFLIGHT_DONE[0] is False
    assert rae._TASK["stage"] == "环境预检失败"
