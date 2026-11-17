"""预期体积回退：同版本重跑后旧成品变成 .v1.mp4，仍要能估算进度。"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".trae", "skills", "jianying-editor", "examples"))

import robust_auto_export as rae  # noqa: E402


def test_archived_v1_used_when_output_missing(tmp_path):
    out = str(tmp_path / "剧A_b.mp4")
    (tmp_path / "剧A_b.v1.mp4").write_bytes(b"x" * 2048)

    size, source = rae._expected_export_bytes(out, None)

    assert size == 2048
    assert source == "归档副本 .v1"


def test_newest_archive_wins_over_older(tmp_path):
    out = str(tmp_path / "剧A_b.mp4")
    (tmp_path / "剧A_b.v1.mp4").write_bytes(b"x" * 2048)
    (tmp_path / "剧A_b.v2.mp4").write_bytes(b"y" * 4096)

    size, source = rae._expected_export_bytes(out, None)

    assert size == 2048 and source == "归档副本 .v1"


def test_current_output_beats_archive(tmp_path):
    out = str(tmp_path / "剧A_b.mp4")
    (tmp_path / "剧A_b.mp4").write_bytes(b"c" * 1024)
    (tmp_path / "剧A_b.v1.mp4").write_bytes(b"x" * 2048)

    size, source = rae._expected_export_bytes(out, None)

    assert size == 1024 and source == "上一版成品"


def test_no_archive_no_duration_stays_unknown(tmp_path):
    size, source = rae._expected_export_bytes(str(tmp_path / "none.mp4"), None)

    assert size is None
    assert source == "无估算"


def _calib_file(tmp_path, monkeypatch, samples):
    p = tmp_path / "calib.json"
    p.write_text(json.dumps({"samples": samples}), encoding="utf-8")
    monkeypatch.setattr(rae, "_CALIB_FILE", str(p))
    monkeypatch.setattr(rae, "_draft_canvas_key", lambda _d: "1080x1920")
    return p


def test_calibrated_rate_is_median_of_same_canvas(tmp_path, monkeypatch):
    """首次导出没有旧成品时，用同分辨率样本的中位码率，不混入其它分辨率。"""
    _calib_file(tmp_path, monkeypatch, [
        {"key": "1080x1920", "bytes_per_s": 1000.0},
        {"key": "1080x1920", "bytes_per_s": 3000.0},
        {"key": "1080x1920", "bytes_per_s": 2000.0},
        {"key": "720x1280", "bytes_per_s": 999.0},
    ])

    bps, n = rae._calibrated_bytes_per_sec("剧A")

    assert bps == 2000.0 and n == 3


def test_expected_bytes_uses_calibration_first(tmp_path, monkeypatch):
    _calib_file(tmp_path, monkeypatch, [{"key": "1080x1920", "bytes_per_s": 500.0}])

    size, source = rae._expected_export_bytes(str(tmp_path / "none.mp4"), 100, "剧A")

    assert size == 50000
    assert "自校准" in source


def test_record_calibration_appends_samples(tmp_path, monkeypatch):
    p = _calib_file(tmp_path, monkeypatch, [])

    rae._record_calibration("剧A", 100, 2000)
    rae._record_calibration("剧A", 100, 4000)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert [s["bytes_per_s"] for s in data["samples"]] == [20.0, 40.0]
