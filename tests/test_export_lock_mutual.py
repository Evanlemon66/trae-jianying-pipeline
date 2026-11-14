# -*- coding: utf-8 -*-
"""clean / archive 与导出侧共用 .export.lock：动文件前必须持锁。

- 拿不到锁（有导出在跑）→ 直接退出并说明占用者，不删、不搬
- 拿到锁 → 正常执行，结束后释放
"""
import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline", "tests"):
    sys.path.insert(0, os.path.join(ROOT, _sub))

import archive_job as aj  # noqa: E402
import job_manifest as jm  # noqa: E402
import stream_manifest as sm  # noqa: E402
from bundle_fixture import make_bundle  # noqa: E402


def _run(func, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = func(*a, **kw)
    return rc, buf.getvalue()


def _prep_clean(tmp_path, monkeypatch):
    name = "剧A"
    src = tmp_path / "剧A" / "src"
    os.makedirs(src / "cover_job" / "06_publish")
    bundle = tmp_path / "out" / name
    made = make_bundle(bundle, name)
    job = jm.build_job(name)
    job["stage"] = "published"
    job["publish"] = {"status": "published"}
    job["video"]["outputs"] = {
        "a": {"draft": f"{name}_a", "export_file": str(made["mp4"]).replace(os.sep, "/"),
              "state": "exported"}}
    jm.save_job(job, str(src / "cover_job"))
    monkeypatch.setattr(sm, "MANIFEST_DIR", str(tmp_path / "manifests"))
    sm.record(name, source_dir=str(src), export_file=str(made["mp4"]))
    return name, bundle


def test_clean_exits_when_lock_busy(tmp_path, monkeypatch):
    name, bundle = _prep_clean(tmp_path, monkeypatch)
    monkeypatch.setattr(sm.export_lock, "acquire", lambda owner="": False)
    monkeypatch.setattr(sm.export_lock, "read_lock",
                        lambda: {"pid": 4321, "owner": "series_runner 剧A"})
    released = []
    monkeypatch.setattr(sm.export_lock, "release", lambda: released.append(1))

    rc, out = _run(sm.clean, name)

    assert rc == 1
    assert bundle.exists(), "拿不到锁时不得删除"
    assert "全局导出锁被占用" in out and "series_runner 剧A" in out
    assert not released, "没拿到锁就不该释放"


def test_clean_releases_lock_after_delete(tmp_path, monkeypatch):
    name, bundle = _prep_clean(tmp_path, monkeypatch)
    owners = []
    released = []
    monkeypatch.setattr(sm.export_lock, "acquire",
                        lambda owner="": owners.append(owner) or True)
    monkeypatch.setattr(sm.export_lock, "release", lambda: released.append(1))

    rc, _out = _run(sm.clean, name)

    assert rc == 0
    assert not bundle.exists()
    assert owners and owners[0].startswith("stream_manifest clean")
    assert released == [1]


def _prep_archive(tmp_path):
    series = tmp_path / "jobs" / "剧A"
    job_dir = series / "cover_job"
    for rel in ("01_input", "02_titles", "03_prompts", "04_covers",
                "05_video", "06_publish", "06_publish/receipts"):
        os.makedirs(job_dir / rel)
    (job_dir / "01_input" / "synopsis.txt").write_text("简介", encoding="utf-8")
    job = jm.build_job("剧A")
    job["stage"] = "published"
    job["publish"] = {"status": "published"}
    jm.save_job(job, str(job_dir))
    return job_dir, series, tmp_path / "jobs"


def test_archive_exits_when_lock_busy(tmp_path, monkeypatch):
    job_dir, series, jobs_root = _prep_archive(tmp_path)
    monkeypatch.setattr(aj.export_lock, "acquire", lambda owner="": False)
    monkeypatch.setattr(aj.export_lock, "read_lock",
                        lambda: {"pid": 4321, "owner": "series_runner 剧A"})
    released = []
    monkeypatch.setattr(aj.export_lock, "release", lambda: released.append(1))
    archive_root = tmp_path / "archive"

    rc, out = _run(aj.archive, str(job_dir), str(archive_root), str(tmp_path / "mf"),
                   roots=[str(jobs_root)])

    assert rc == 1
    assert series.exists(), "拿不到锁时不得移动剧集目录"
    assert "全局导出锁被占用" in out
    assert jm.load_job(str(job_dir / "job.json"))["stage"] == "published"
    assert not released


def test_archive_releases_lock_after_move(tmp_path, monkeypatch):
    job_dir, series, jobs_root = _prep_archive(tmp_path)
    owners = []
    released = []
    monkeypatch.setattr(aj.export_lock, "acquire",
                        lambda owner="": owners.append(owner) or True)
    monkeypatch.setattr(aj.export_lock, "release", lambda: released.append(1))
    archive_root = tmp_path / "archive"

    rc, _out = _run(aj.archive, str(job_dir), str(archive_root), str(tmp_path / "mf"),
                    roots=[str(jobs_root)])

    assert rc == 0
    assert not series.exists()
    assert (archive_root / "剧A" / "cover_job").is_dir()
    assert owners and owners[0].startswith("archive_job")
    assert released == [1]
