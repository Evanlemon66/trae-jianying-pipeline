"""预期体积回退：同版本重跑后旧成品变成 .v1.mp4，仍要能估算进度。"""

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
