"""E3 按版本分槽：给 B 补封面基线不再把 A 误判成 cover_changed。"""

import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import freshness as f  # noqa: E402
import job_manifest as jm  # noqa: E402


def _setup(tmp_path, monkeypatch):
    jd = tmp_path / "cover_job"
    os.makedirs(jd)
    job = jm.build_job("剧A")
    jm.save_job(job, str(jd))
    covers = {"a": "hash-a", "b": "hash-b"}
    for ver in ("a", "b"):
        (tmp_path / ("cover_%s.png" % ver)).write_bytes(b"img-" + ver.encode())
    monkeypatch.setattr(
        f, "_cover_input_hash", lambda job_dir, version: covers[version])
    # record_cover 里走的是 file_pixel_hash(cover_image)，同一套映射
    monkeypatch.setattr(
        f, "file_pixel_hash",
        lambda p: covers[os.path.basename(str(p)).split("_")[1].split(".")[0]])
    monkeypatch.setattr(f, "_cover_output_hash", lambda ddir, draft: None)
    return jd, covers


def test_b_baseline_does_not_invalidate_a(tmp_path, monkeypatch):
    jd, covers = _setup(tmp_path, monkeypatch)

    assert f.record_cover(str(jd), "a", None, str(tmp_path / "cover_a.png"))
    # 后给 B 写基线：旧单槽结构下 A 会变成 stale/cover_changed
    assert f.record_cover(str(jd), "b", None, str(tmp_path / "cover_b.png"))

    assert f.check(str(jd), "e3", "a")["state"] == "valid"
    assert f.check(str(jd), "e3", "b")["state"] == "valid"
    job = jm.load_job(str(jd / "job.json"))
    e3 = job["freshness"]["edges"][f.EDGE_E3]
    assert set(e3["versions"]) == {"a", "b"}
    assert e3["version"] == "b"  # 平铺字段保留最近一次写的兼容副本


def test_changing_a_cover_does_not_affect_b(tmp_path, monkeypatch):
    jd, covers = _setup(tmp_path, monkeypatch)
    assert f.record_cover(str(jd), "a", None, str(tmp_path / "cover_a.png"))
    assert f.record_cover(str(jd), "b", None, str(tmp_path / "cover_b.png"))

    covers["a"] = "in-a-v2"

    ra = f.check(str(jd), "e3", "a")
    assert ra["state"] == "stale" and ra["reason"] == "cover_changed"
    assert f.check(str(jd), "e3", "b")["state"] == "valid"


def test_missing_version_slot_does_not_borrow_other_version(tmp_path, monkeypatch):
    jd, _covers = _setup(tmp_path, monkeypatch)
    assert f.record_cover(str(jd), "a", None, str(tmp_path / "cover_a.png"))
    assert f.record_cover(str(jd), "b", None, str(tmp_path / "cover_b.png"))

    job = jm.load_job(str(jd / "job.json"))
    del job["freshness"]["edges"][f.EDGE_E3]["versions"]["a"]
    jm.save_job(job, str(jd))

    assert f.check(str(jd), "e3", "b")["state"] == "valid"
    # A 没有自己的证据：不得回落到平铺的 B 记录判 valid
    ra = f.check(str(jd), "e3", "a")
    assert ra["state"] == "unknown", ra


def test_legacy_flat_e3_still_readable(tmp_path, monkeypatch):
    jd, _covers = _setup(tmp_path, monkeypatch)
    assert f.record_cover(str(jd), "a", None, str(tmp_path / "cover_a.png"))

    job = jm.load_job(str(jd / "job.json"))
    e3 = job["freshness"]["edges"][f.EDGE_E3]
    e3.pop("versions", None)
    e3.pop("version", None)
    jm.save_job(job, str(jd))

    res = f.check(str(jd), "e3", "a")
    assert res["state"] == "valid", res
