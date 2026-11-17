# -*- coding: utf-8 -*-
"""导出进度可见性：百分比口径、速率/剩余时间、通知频次、进度 json 字段。

覆盖：
  - _expected_export_bytes 三级回退（上一版成品 → 上次完成记录 → 时长估算 → 无估算）
  - _progress_snapshot 百分比 = 已写体积 / 预期总量（封顶 99%）
  - _progress_rate_mbps 两次采样差算 MB/s；体积回落（分片转正式文件）重置基线
  - _fmt_progress 文案：`4.2/8.7 GB（48%）约剩 12 分钟`，算不出来写「无估算」
  - _progress_toast_due 只在跨 25%/50%/75% 时通知，且算不出百分比不通知
    （与 300s 定时通知并存，定时那条不经过此判定）
  - _progress_toast_text 带草稿名、体积/百分比与成品路径
  - _write_progress 落盘的 percent 不为 null

运行：pytest tests/test_export_progress.py -q
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", os.path.join(".trae", "skills", "jianying-editor", "examples")):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import robust_auto_export as rae  # noqa: E402


def _reset():
    rae._PROGRESS_SAMPLES.clear()
    rae._PROGRESS_TOASTED.clear()
    rae._TASK["expected_bytes"] = None
    rae._TASK["total_source"] = None
    # 其它测试跑过 main() 会把真实草稿名留在 _TASK，这里清掉，避免假草稿不落盘
    rae._TASK["draft"] = None


# ── 预期总量：三级回退 ──────────────────────────────────────────

def test_expected_bytes_prefers_previous_output(tmp_path):
    _reset()
    out = tmp_path / "剧名.mp4"
    out.write_bytes(b"x" * 1000)
    size, source = rae._expected_export_bytes(str(out))
    assert size == 1000
    assert source == "上一版成品"


def test_expected_bytes_falls_back_to_done_record(tmp_path):
    _reset()
    out = tmp_path / "L" / "剧名" / "剧名.mp4"
    final_path, side_path = rae._progress_paths(str(out))
    os.makedirs(os.path.dirname(side_path), exist_ok=True)
    with open(side_path, "w", encoding="utf-8") as f:
        json.dump({"done": True, "final_mb": 10}, f)
    size, source = rae._expected_export_bytes(str(out))
    assert size == 10 * 1048576
    assert source == "上次进度记录"


def test_expected_bytes_ignores_unfinished_record(tmp_path):
    _reset()
    out = tmp_path / "L" / "剧名" / "剧名.mp4"
    _final, side_path = rae._progress_paths(str(out))
    os.makedirs(os.path.dirname(side_path), exist_ok=True)
    with open(side_path, "w", encoding="utf-8") as f:
        json.dump({"done": False, "exported_mb": 2466.2}, f)
    size, source = rae._expected_export_bytes(str(out), expected_duration=10)
    assert size == 10 * rae._EST_BYTES_PER_SEC
    assert source == "时长估算"


def test_expected_bytes_none_when_nothing_available(tmp_path):
    _reset()
    size, source = rae._expected_export_bytes(str(tmp_path / "不存在.mp4"))
    assert size is None
    assert source == "无估算"


# ── 百分比 / 速率 / 剩余时间 ────────────────────────────────────

def test_snapshot_percent_is_size_over_total(tmp_path):
    _reset()
    out = tmp_path / "a.mp4"
    out.write_bytes(b"x" * 480)
    rae._TASK["expected_bytes"] = 1000
    snap = rae._progress_snapshot(str(out))
    assert snap["percent"] == 48
    assert snap["total"] == 1000
    assert rae._estimate_percent(str(out), None) == "约 48%"


def test_snapshot_percent_caps_at_99(tmp_path):
    _reset()
    out = tmp_path / "a.mp4"
    out.write_bytes(b"x" * 2000)
    rae._TASK["expected_bytes"] = 1000
    assert rae._progress_snapshot(str(out))["percent"] == 99


def test_rate_uses_sample_delta_and_resets_on_shrink():
    _reset()
    assert rae._progress_rate_mbps(10 * 1048576, 100.0) is None   # 首个样本，无差值
    rate = rae._progress_rate_mbps(70 * 1048576, 160.0)           # 60s 写了 60MB
    assert abs(rate - 1.0) < 0.01
    # 分片转正式文件时体积回落：清空基线，宁可不报也不给负速率
    assert rae._progress_rate_mbps(5 * 1048576, 170.0) is None
    rate = rae._progress_rate_mbps(15 * 1048576, 180.0)           # 新基线 10s 写 10MB
    assert abs(rate - 1.0) < 0.01


def test_fmt_progress_shape_and_no_estimate():
    snap = {"size": 4509715660, "total": 9341553868, "percent": 48,
            "rate": 100.0, "eta_s": 720}
    assert rae._fmt_progress(snap) == "4.2/8.7 GB（48%） 约剩 12 分钟"
    assert rae._fmt_progress({"size": 0, "total": None, "percent": None,
                              "rate": None, "eta_s": None}) == "0.0 GB（无估算）"
    assert rae._fmt_progress({"size": 0, "total": 9341553868, "percent": None,
                              "rate": None, "eta_s": None}) == "0.0/8.7 GB（无估算）"


def test_estimate_percent_never_empty(tmp_path):
    _reset()
    assert rae._estimate_percent(str(tmp_path / "缺.mp4"), None) == "无估算"


# ── 通知频次：只在 25/50/75 各一次 ──────────────────────────────

def test_toast_only_at_stage_thresholds():
    _reset()
    assert not rae._progress_toast_due({"percent": 10})
    assert rae._progress_toast_due({"percent": 25})
    assert not rae._progress_toast_due({"percent": 25})   # 同档不重复
    assert rae._progress_toast_due({"percent": 60})       # 跨过 50%，一次即可
    assert not rae._progress_toast_due({"percent": 74})
    assert rae._progress_toast_due({"percent": 75})
    assert not rae._progress_toast_due({"percent": 99})


def test_no_toast_without_percent():
    _reset()
    assert not rae._progress_toast_due({"percent": None})
    assert not rae._PROGRESS_TOASTED   # 不通知也不占用档位


def test_toast_text_has_name_bytes_and_path():
    snap = {"size": 4509715660, "total": 9341553868, "percent": 48,
            "rate": 100.0, "eta_s": 720}
    text = rae._progress_toast_text("八零猎王", snap, r"L:\短剧\八零猎王.mp4")
    assert "八零猎王" in text
    assert "4.2/8.7 GB（48%）" in text and "约剩 12 分钟" in text
    assert r"L:\短剧\八零猎王.mp4" in text
    # 无百分比时定时通知也照发，但明确写「无估算」，不装精确
    text = rae._progress_toast_text("八零猎王", {"size": 0, "total": None, "percent": None,
                                              "rate": None, "eta_s": None}, "out.mp4")
    assert "无估算" in text and "None" not in text


# ── 进度 json 落盘字段 ──────────────────────────────────────────

def test_write_progress_records_percent_and_rate(tmp_path):
    _reset()
    output = str(tmp_path / "L" / "剧名" / "剧名.mp4")
    real = tmp_path / "L" / "剧名" / "uuid临时.mp4"
    os.makedirs(real.parent, exist_ok=True)
    real.write_bytes(b"x" * 480_000)
    rae._TASK["expected_bytes"] = 1_000_000
    rae._TASK["total_source"] = "上一版成品"
    rae._write_progress(output, str(real), None, 0.0, done=False)
    _final, side = rae._progress_paths(output)
    with open(side, encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["percent"] == "约 48%"
    assert rec["percent_value"] == 48
    assert rec["exported_mb"] > 0
    assert rec["total_mb"] is not None
    assert rec["total_source"] == "上一版成品"
    assert "rate_mbps" in rec and "eta_s" in rec
    assert rec["percent"] is not None  # 不能是 null
