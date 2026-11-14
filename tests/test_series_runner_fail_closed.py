"""series_runner fail-closed：stale 判定 / 导出收尾顺序 / 多版本台账 / 全局导出锁。"""

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _sub in ("lib", "pipeline", "scripts"):
    sys.path.insert(0, os.path.join(_ROOT, _sub))

import export_lock  # noqa: E402
import job_manifest as jm  # noqa: E402
import cover_watch as cw  # noqa: E402
import series_runner as sr  # noqa: E402

_DRAFT = "剧A串流"


def _make_job(tmp_path):
    jd = os.path.join(str(tmp_path), "剧A", "cover_job")
    for d in ("01_input", "02_titles", "03_prompts", "04_covers", "05_video", "06_publish"):
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job("剧A")
    job["workflow"]["preset_profile"] = "a,b"
    jm.save_job(job, jd)
    return jd


def _make_draft(tmp_path, monkeypatch, name=_DRAFT, stale=True):
    drafts = tmp_path / "drafts"
    ddir = drafts / name
    ddir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sr, "JY_DRAFT_ROOT", str(drafts))
    if stale:
        (ddir / "_export_stale.json").write_text("{}", encoding="utf-8")
    return ddir


def _make_mp4(tmp_path, name, size=4096):
    out = tmp_path / "export" / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"x" * size)
    return out


def _patch_freshness(monkeypatch, job_dir, version):
    monkeypatch.setattr(sr.freshness, "job_dir_for_draft", lambda _n: job_dir)
    monkeypatch.setattr(sr.freshness, "version_for_draft", lambda _j, _n: version)


def test_stale_output_not_treated_as_exported(tmp_path, monkeypatch):
    ddir = _make_draft(tmp_path, monkeypatch)
    mp4 = _make_mp4(tmp_path, _DRAFT, size=2 * 1024 * 1024)
    monkeypatch.setattr(sr, "export_mp4_path", lambda _n: str(mp4))

    assert sr._output_exists(_DRAFT) is False

    (ddir / "_export_stale.json").unlink()
    assert sr._output_exists(_DRAFT) is True


def test_finalize_clears_stale_and_writes_e2_sidecar(tmp_path, monkeypatch):
    jd = _make_job(tmp_path)
    ddir = _make_draft(tmp_path, monkeypatch)
    mp4 = _make_mp4(tmp_path, _DRAFT)
    monkeypatch.setattr(sr, "export_mp4_path", lambda _n: str(mp4))
    _patch_freshness(monkeypatch, jd, "a")

    assert sr._finalize_export(_DRAFT, str(mp4), jd) is True

    assert not (ddir / "_export_stale.json").exists(), "成功后必须清 stale"
    job = jm.load_job(os.path.join(jd, "job.json"))
    assert job["video"]["outputs"]["a"]["state"] == "exported"
    assert job["video"]["outputs"]["a"]["size_bytes"] == mp4.stat().st_size
    assert os.path.isfile(str(mp4) + ".freshness.json"), "必须写 E2 sidecar"


def test_ledger_failure_keeps_stale_and_skips_e2(tmp_path, monkeypatch):
    jd = _make_job(tmp_path)
    ddir = _make_draft(tmp_path, monkeypatch)
    mp4 = _make_mp4(tmp_path, _DRAFT)
    _patch_freshness(monkeypatch, jd, "a")

    def _boom(*_a, **_k):
        raise OSError("job.json 写入失败")

    monkeypatch.setattr(sr.jm, "record_video", _boom)

    assert sr._finalize_export(_DRAFT, str(mp4), jd) is False

    assert (ddir / "_export_stale.json").exists(), "台账失败必须保留 stale（fail-closed）"
    assert not os.path.isfile(str(mp4) + ".freshness.json"), "台账失败不写 E2"


def _patch_freshness_by_suffix(monkeypatch, job_dir):
    monkeypatch.setattr(sr.freshness, "job_dir_for_draft", lambda _n: job_dir)
    monkeypatch.setattr(sr.freshness, "version_for_draft",
                        lambda _j, n: "b" if n.endswith("_b") else "a")


def test_ledger_records_each_version(tmp_path, monkeypatch):
    jd = _make_job(tmp_path)
    _make_draft(tmp_path, monkeypatch, _DRAFT)
    _make_draft(tmp_path, monkeypatch, _DRAFT + "_b")
    out_a = _make_mp4(tmp_path, _DRAFT)
    out_b = _make_mp4(tmp_path, _DRAFT + "_b")
    _patch_freshness_by_suffix(monkeypatch, jd)

    assert sr._finalize_export(_DRAFT, str(out_a), jd) is True
    assert sr._finalize_export(_DRAFT + "_b", str(out_b), jd) is True

    outputs = jm.load_job(os.path.join(jd, "job.json"))["video"]["outputs"]
    assert set(outputs) >= {"a", "b"}
    assert outputs["a"]["draft"] == _DRAFT
    assert outputs["b"]["draft"] == _DRAFT + "_b"
    assert outputs["b"]["size_bytes"] == out_b.stat().st_size


def test_watcher_no_disk_present_ledger_missing(tmp_path, monkeypatch):
    jd = _make_job(tmp_path)
    _make_draft(tmp_path, monkeypatch, _DRAFT)
    _make_draft(tmp_path, monkeypatch, _DRAFT + "_b")
    out_a = _make_mp4(tmp_path, _DRAFT)
    out_b = _make_mp4(tmp_path, _DRAFT + "_b")
    _patch_freshness_by_suffix(monkeypatch, jd)
    sr._finalize_export(_DRAFT, str(out_a), jd)
    sr._finalize_export(_DRAFT + "_b", str(out_b), jd)

    job = jm.load_job(os.path.join(jd, "job.json"))
    monkeypatch.setattr(cw, "_missing_artifacts", lambda _dn, **_kw: [])
    conflicts = cw._output_conflicts(job, _DRAFT, ["a", "b"])

    assert conflicts == [], conflicts


def test_lock_held_blocks_export_without_spawning(tmp_path, monkeypatch):
    lock_path = tmp_path / ".export.lock"
    lock_path.write_text(json.dumps({"pid": 999999, "owner": "other"}), encoding="utf-8")
    monkeypatch.setattr(export_lock, "_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(export_lock, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(sr, "_run_export_locked",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("不应并发导出")))

    assert sr._run_export(_DRAFT) is False


def test_lock_released_after_failed_export(tmp_path, monkeypatch):
    lock_path = tmp_path / ".export.lock"
    monkeypatch.setattr(export_lock, "_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(sr, "_run_export_locked", lambda *_a, **_k: False)

    assert sr._run_export(_DRAFT) is False
    assert not lock_path.exists(), "失败路径也必须释放自己拿到的锁"


def test_own_lock_is_reused_not_released(tmp_path, monkeypatch):
    lock_path = tmp_path / ".export.lock"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "owner": "cover_watch"}),
                         encoding="utf-8")
    monkeypatch.setattr(export_lock, "_LOCK_PATH", str(lock_path))
    monkeypatch.setattr(sr, "_run_export_locked", lambda *_a, **_k: True)

    assert sr._run_export(_DRAFT) is True
    assert lock_path.exists(), "watcher 已持锁时不能把别人的锁释放掉"
