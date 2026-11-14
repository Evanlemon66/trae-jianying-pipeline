"""R09：归档默认阻断 stale/evidence_missing，`--allow-stale` 放行并留理由；clean 维持放行。"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline"):
    sys.path.insert(0, os.path.join(ROOT, _sub))

import archive_job as aj  # noqa: E402
import freshness as f  # noqa: E402
import job_manifest as jm  # noqa: E402

MINI = {"materials": {"videos": [], "audios": []}, "tracks": [], "duration": 1}


def _setup(tmp_path, monkeypatch):
    jobs_root = tmp_path / "jobs"
    series = jobs_root / "剧A"
    job_dir = series / "cover_job"
    for rel in ("01_input", "02_titles", "03_prompts", "04_covers",
                "05_video", "06_publish", "06_publish/receipts"):
        os.makedirs(job_dir / rel, exist_ok=True)
    (job_dir / "01_input" / "synopsis.txt").write_text("简介", encoding="utf-8")
    publish = job_dir / "06_publish"

    drafts = tmp_path / "drafts"
    ddir = drafts / "剧A_a"
    os.makedirs(ddir)
    with open(ddir / "draft_content.json", "w", encoding="utf-8") as fp:
        json.dump(MINI, fp)
    # 硬 stale 信号：导出后草稿又被改过
    (ddir / "_export_stale.json").write_text("{}", encoding="utf-8")

    mp4 = publish / "a.mp4"
    mp4.write_bytes(b"x" * 1024)
    with open(str(mp4) + ".freshness.json", "w", encoding="utf-8") as fp:
        json.dump({"state": "valid", "version": "a",
                   "exported_at": "2026-11-14T10:00:00+08:00",
                   "draft": "剧A_a", "video": {"size_bytes": 1024}}, fp)

    job = jm.build_job("剧A")
    job["stage"] = "published"
    job["publish"] = {"status": "published"}
    job["video"]["outputs"] = {
        "a": {"draft": "剧A_a", "export_file": str(mp4).replace(os.sep, "/"),
              "size_bytes": 1024, "state": "exported"},
    }
    jm.save_job(job, str(job_dir))
    monkeypatch.setattr(f, "_drafts_root", lambda: str(drafts))
    return job_dir, jobs_root, mp4


def test_archive_blocks_stale_without_allow_reason(tmp_path, monkeypatch):
    job_dir, jobs_root, _mp4 = _setup(tmp_path, monkeypatch)
    archive_root = tmp_path / "archive"

    rc = aj.archive(str(job_dir), str(archive_root), str(tmp_path / "mf"),
                    force=True, roots=[str(jobs_root)])

    assert rc == 1
    assert os.path.isdir(str(job_dir)), "被阻断时不得移动目录"
    assert not os.path.exists(str(archive_root / "剧A"))


def test_archive_allows_stale_with_reason_and_records_it(tmp_path, monkeypatch):
    job_dir, jobs_root, _mp4 = _setup(tmp_path, monkeypatch)
    archive_root = tmp_path / "archive"

    rc = aj.archive(str(job_dir), str(archive_root), str(tmp_path / "mf"),
                    force=True, roots=[str(jobs_root)],
                    allow_stale_reason="已人工核对 A 版时间轴")

    assert rc == 0
    moved = archive_root / "剧A" / "cover_job" / "06_publish" \
        / "freshness_archive_snapshot.json"
    snap = json.loads(moved.read_text(encoding="utf-8"))
    assert snap["stale"] == ["a"]
    assert snap["allow_stale_reason"] == "已人工核对 A 版时间轴"


def test_clean_still_allows_stale(tmp_path, monkeypatch):
    job_dir, _jobs_root, _mp4 = _setup(tmp_path, monkeypatch)
    res = f.freshness_of(str(job_dir), "a", consumer="clean")
    assert res["state"] == "stale"
    assert res["allowed"] is True
