# -*- coding: utf-8 -*-
"""回归用例：clean → archive 死锁。

现象：clean 删掉成品后，freshness 判 `output_missing` stale，archive 的
stale 闸门把"已经清理干净"的剧拦下。修复后 `export_state=cleaned`（或
outputs[*].state=cleaned）的版本视作合法已清理状态，不参与 stale 阻断；
仍未清理的 exported 版本照旧阻断（闸门不能顺手放宽）。

覆盖：
  A. cleaned job：成品已删、sidecar 残留 → 归档放行，快照仍如实记 stale
  B. exported job：成品在但草稿被改（_export_stale.json）→ 仍被 stale 闸门拦住
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline", "tests"):
    sys.path.insert(0, os.path.join(ROOT, _sub))

import archive_job as aj  # noqa: E402
import freshness as f  # noqa: E402
import job_manifest as jm  # noqa: E402
from bundle_fixture import make_bundle  # noqa: E402


def _setup(tmp_path, monkeypatch, *, export_state, outputs_state, mp4_exists,
           stale_marker=False):
    """造一部剧：job + 草稿目录 + 成品（可缺）+ 成品旁 sidecar。"""
    jobs_root = tmp_path / "jobs"
    series = jobs_root / "剧A"
    job_dir = series / "cover_job"
    for rel in ("01_input", "02_titles", "03_prompts", "04_covers",
                "05_video", "06_publish", "06_publish/receipts"):
        os.makedirs(job_dir / rel, exist_ok=True)
    (job_dir / "01_input" / "synopsis.txt").write_text("简介", encoding="utf-8")

    drafts = tmp_path / "drafts"
    ddir = drafts / "剧A_a"
    os.makedirs(ddir)
    with open(ddir / "draft_content.json", "w", encoding="utf-8") as fp:
        json.dump({"materials": {"videos": [], "audios": []}, "tracks": [],
                   "duration": 1}, fp)
    if stale_marker:
        (ddir / "_export_stale.json").write_text("{}", encoding="utf-8")

    out = tmp_path / "out"
    made = make_bundle(out, "剧A_a")          # 平铺布局三件套
    mp4 = out / "剧A_a.mp4"
    if not mp4_exists:
        for p in (made["mp4"], made["progress"], made["cover"]):
            if p and os.path.isfile(p):
                os.remove(p)
    # 平铺布局 clean 只删三件套，sidecar 会留在原地：这正是死锁的触发条件
    with open(str(mp4) + ".freshness.json", "w", encoding="utf-8") as fp:
        json.dump({"state": "valid", "version": "a",
                   "exported_at": "2026-11-14T10:00:00+08:00",
                   "draft": "剧A_a", "video": {"size_bytes": 10240}}, fp)

    job = jm.build_job("剧A")
    job["stage"] = "published"
    job["publish"] = {"status": "published"}
    job["video"]["export_state"] = export_state
    job["video"]["outputs"] = {
        "a": {"draft": "剧A_a", "export_file": str(mp4).replace(os.sep, "/"),
              "size_bytes": 10240, "state": outputs_state},
    }
    jm.save_job(job, str(job_dir))
    monkeypatch.setattr(f, "_drafts_root", lambda: str(drafts))
    return job_dir, jobs_root


def test_cleaned_job_not_blocked_by_stale_gate(tmp_path, monkeypatch):
    """A：clean 已删成品 → freshness=stale，但归档应放行。"""
    job_dir, jobs_root = _setup(tmp_path, monkeypatch,
                                export_state="cleaned", outputs_state="cleaned",
                                mp4_exists=False)
    archive_root = tmp_path / "archive"

    rc = aj.archive(str(job_dir), str(archive_root), str(tmp_path / "mf"),
                    roots=[str(jobs_root)])

    assert rc == 0
    assert not os.path.isdir(str(job_dir)), "cleaned job 应已搬走"
    moved = archive_root / "剧A" / "cover_job"
    assert moved.is_dir()
    snap = json.loads((moved / "06_publish"
                       / "freshness_archive_snapshot.json").read_text(encoding="utf-8"))
    # 放行不等于篡改判定：快照仍如实记录 stale(output_missing)
    assert snap["stale"] == ["a"]
    assert snap["versions"]["a"]["reason"] == "output_missing"


def test_exported_job_stale_still_blocked(tmp_path, monkeypatch):
    """B：未清理的 exported 版本 stale，闸门照旧拦住（不能顺手放宽）。"""
    job_dir, jobs_root = _setup(tmp_path, monkeypatch,
                                export_state="exported", outputs_state="exported",
                                mp4_exists=True, stale_marker=True)
    archive_root = tmp_path / "archive"

    rc = aj.archive(str(job_dir), str(archive_root), str(tmp_path / "mf"),
                    roots=[str(jobs_root)])

    assert rc == 1
    assert os.path.isdir(str(job_dir)), "被阻断时不得移动目录"
    assert not (archive_root / "剧A").exists()
