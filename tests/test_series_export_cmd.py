"""R14：watcher/series_runner 导出路径也要传 --expect-duration。"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import series_runner as sr  # noqa: E402


def _write_draft(tmp_path, name, segments_us):
    ddir = tmp_path / name
    os.makedirs(ddir, exist_ok=True)
    start = 0
    segs = []
    for dur in segments_us:
        segs.append({"target_timerange": {"start": start, "duration": dur}})
        start += dur
    with open(ddir / "draft_content.json", "w", encoding="utf-8") as f:
        json.dump({"tracks": [{"type": "video", "segments": segs}]}, f)


def test_export_cmd_carries_expected_duration(tmp_path, monkeypatch):
    _write_draft(tmp_path, "draft_x", [5_000_000, 2_500_000])
    monkeypatch.setattr(sr, "_drafts_root", lambda: str(tmp_path))
    monkeypatch.setattr(sr, "get_export_dir", lambda: "D:/out")

    cmd = sr._export_cmd("draft_x", "D:/out/x.mp4", False, False)

    assert "--expect-duration" in cmd
    assert cmd[cmd.index("--expect-duration") + 1] == "7.5"


def test_export_cmd_omits_duration_when_draft_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_drafts_root", lambda: str(tmp_path))
    monkeypatch.setattr(sr, "get_export_dir", lambda: "D:/out")

    cmd = sr._export_cmd("missing", "D:/out/x.mp4", True, True)

    assert "--expect-duration" not in cmd
    assert "--cold-start" in cmd and "--keep-open" in cmd
