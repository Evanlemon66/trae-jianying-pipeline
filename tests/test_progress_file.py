# -*- coding: utf-8 -*-
"""`_progress.json` 契约：pipeline 写、Console 只读。

覆盖：正常写入→读取、损坏/版本不符/过期 → None、done>total 不越界、
写失败（同名目录）不影响主流程。
"""
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("pipeline", os.path.join("pipeline_ui", "console")):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pipeline as pl  # noqa: E402
import status  # noqa: E402


def test_write_then_read(tmp_path):
    wd = str(tmp_path)
    pl._write_progress(wd, "demucs", "人声分离 + 音频提取", 3, 10)
    data = status.read_pipeline_progress(wd)
    assert data["v"] == 1
    assert data["stage"] == "demucs"
    assert data["label"] == "人声分离 + 音频提取"
    assert (data["done"], data["total"]) == (3, 10)
    # 权重插值：demucs 1 → presets_cut 27
    assert status.progress_percent(data) == 8.8


def test_asr_translate_counts(tmp_path):
    wd = str(tmp_path)
    pl._write_progress(wd, "asr_translate", "识别字幕 + 翻译字幕", 8, 10,
                       asr_done=4, tr_done=4)
    data = status.read_pipeline_progress(wd)
    assert (data["asr_done"], data["tr_done"]) == (4, 4)
    # asr_translate 28 → cleanup 85
    assert status.progress_percent(data) == 73.6


def test_corrupt_json_returns_none(tmp_path):
    wd = str(tmp_path)
    (tmp_path / "_progress.json").write_text("{broken", encoding="utf-8")
    assert status.read_pipeline_progress(wd) is None


def test_version_or_stage_mismatch_returns_none(tmp_path):
    wd = str(tmp_path)
    p = tmp_path / "_progress.json"
    p.write_text(json.dumps({"v": 2, "stage": "demucs"}), encoding="utf-8")
    assert status.read_pipeline_progress(wd) is None
    p.write_text(json.dumps({"v": 1}), encoding="utf-8")
    assert status.read_pipeline_progress(wd) is None


def test_stale_file_returns_none(tmp_path):
    wd = str(tmp_path)
    (tmp_path / "_progress.json").write_text(json.dumps({
        "v": 1, "stage": "demucs", "updated_at": time.time() - 600,
    }), encoding="utf-8")
    assert status.read_pipeline_progress(wd) is None
    assert status.read_pipeline_progress(wd, max_age_s=3600) is not None


def test_done_over_total_is_clamped(tmp_path):
    wd = str(tmp_path)
    pl._write_progress(wd, "asr_translate", "识别字幕 + 翻译字幕", 12, 10)
    data = status.read_pipeline_progress(wd)
    assert status.progress_percent(data) == 85


def test_export_stage_has_no_weight_percent(tmp_path):
    wd = str(tmp_path)
    pl._write_progress(wd, "export", "导出", 0, 0)
    data = status.read_pipeline_progress(wd)
    assert status.progress_percent(data) is None


def test_write_failure_does_not_raise(tmp_path):
    wd = str(tmp_path)
    os.makedirs(os.path.join(wd, "_progress.json"))  # 同名目录 → open 失败
    pl._write_progress(wd, "build", "建稿 · 注入视频", 1, 4)
    assert status.read_pipeline_progress(wd) is None
