# -*- coding: utf-8 -*-
"""Console 阶段识别：按链路选表 + 串流注入阶段的真实进度。

回归点：
- 串流开头的 `🧩 预设来源:` 只是配置回显（打印在注入之前），
  绝不能被当成「预设合并」推进阶段；
- 串流逐集注入/转封装时按 `📼` 行推进「建稿 · 注入视频 x/N」；
- UI 自动剪辑与串流各用一张阶段表，步骤名不会互相误匹配。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONSOLE = os.path.join(_ROOT, "pipeline_ui", "console")
if _CONSOLE not in sys.path:
    sys.path.insert(0, _CONSOLE)

import runner  # noqa: E402


def _run(mode="batch"):
    return {"status": "running", "mode": mode, "last_line": "",
            "accounts": [], "account_total": 0, "account_index": 0}


def _feed(run, *lines):
    r = runner.Runner()
    for line in lines:
        r._handle_line(run, line)
    return run


def test_batch_preset_source_echo_is_not_a_stage():
    run = _feed(_run(), "  🧩 预设来源: presets.json / 默认组 a")
    assert run.get("stage") is None


def test_batch_inject_progress_follows_each_episode():
    run = _feed(
        _run(),
        "🎬 单草稿串流: 4 集 → 测试_第01-04集串流",
        "🎬 往已有草稿注入 4 个视频:",
    )
    assert run["stage"]["key"] == "build"
    assert run["stage"]["label"] == "建稿 · 注入视频 + 草稿初始化"

    _feed(run,
          "  🔧 自动转封装: 第1集.mp4 → 第1集.__jy_norm__.mp4",
          "  📼 第1集.__jy_norm__.mp4  →  46.21s  [0.00s - 46.21s]  1080x1920")
    assert run["stage"]["key"] == "build"
    assert run["stage"]["label"] == "建稿 · 注入视频 1/4"
    assert 1 <= run["stage"]["percent"] <= 7


def test_batch_presets_stage_only_on_real_merge():
    run = _feed(_run(), "━ [1] 加预设: 我的预设 ━━━━━━━━━━━━━━━━━")
    assert run["stage"]["key"] == "presets_cut"


def test_ui_uses_its_own_stage_table():
    # 串流专属行在 UI 链路里不产生阶段
    run = _feed(_run("ui"), "🎬 单草稿串流: 4 集 → 测试_第01-04集串流")
    assert run.get("stage") is None

    _feed(run, "▶️ 执行 [4] 识别+翻译字幕")
    assert run["stage"]["key"] == "asr"

    # 串流的「建稿 · 注入视频」不适用于 UI：注入行不推进阶段
    _feed(run, "  📼 第1集.mp4  →  46.21s  [0.00s - 46.21s]  1080x1920")
    assert run["stage"]["key"] == "asr"


def test_batch_ignores_ui_step_names():
    run = _feed(_run("batch"), "▶️ 执行 [1] 预设合并")
    assert run.get("stage") is None


def test_stage_tables_are_distinct():
    assert [s[0] for s in runner.STAGES] != [s[0] for s in runner.UI_STAGES]
    assert runner.stages_for("ui") is runner.UI_STAGES
    assert runner.stages_for("batch") is runner.STAGES
