"""产物新鲜度：反向检测 evidence_missing 与 legacy 的边界。

只读/临时目录自检，不碰 <素材根>、不启动剪映。
直接运行：python tests/test_freshness_evidence.py
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import job_manifest as jm  # noqa: E402
import freshness as f  # noqa: E402

MINI_DRAFT = {
    "materials": {"videos": [], "audios": [], "texts": [], "stickers": [], "drafts": []},
    "tracks": [{"type": "video", "segments": []}],
    "duration": 1000000,
}


def _make_job(base, *, outputs=None, export_state=None, export_file=None,
              with_mp4=True, draft_name="ev_a"):
    jd = os.path.join(base, "cover_job")
    os.makedirs(os.path.join(jd, "06_publish"), exist_ok=True)
    job = jm.build_job("自检剧")
    mp4 = os.path.join(base, "out.mp4")
    if with_mp4:
        with open(mp4, "wb") as fp:
            fp.write(b"z" * 4096)
    if outputs is not None:
        job["video"]["outputs"] = outputs
    if export_state is not None:
        job["video"]["export_state"] = export_state
    if export_file is not None:
        job["video"]["export_file"] = export_file.replace(os.sep, "/")
    if draft_name:
        os.makedirs(os.path.join(base, draft_name), exist_ok=True)
        with open(os.path.join(base, draft_name, "draft_content.json"),
                  "w", encoding="utf-8") as fp:
            json.dump(MINI_DRAFT, fp)
    jm.save_job(job, jd)
    return jd, mp4


def _exported_output(mp4, draft_name="ev_a"):
    return {"a": {"draft": draft_name, "export_dir": os.path.dirname(mp4).replace(os.sep, "/"),
                  "export_file": mp4.replace(os.sep, "/"),
                  "size_bytes": os.path.getsize(mp4), "state": "exported",
                  "exported_at": jm.now_iso(), "history": []}}


def _run(fn):
    base = tempfile.mkdtemp(prefix="jf_ev_")
    old_root = f._drafts_root
    f._drafts_root = lambda: base
    f._WARNED.clear()
    try:
        fn(base)
    finally:
        f._drafts_root = old_root
        shutil.rmtree(base, ignore_errors=True)


def test_evidence_missing_is_not_legacy():
    """新台账登记 exported、无 sidecar → evidence_missing，而不是 legacy。"""
    def body(base):
        jd, mp4 = _make_job(base, outputs=None)
        job = jm.load_job(os.path.join(jd, "job.json"))
        job["video"]["outputs"] = _exported_output(mp4)
        jm.save_job(job, jd)

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sched = f.freshness_of(jd, "a", consumer="scheduler")
        assert sched["state"] == "evidence_missing", sched
        assert sched["reason"] == "export_recorded_no_sidecar", sched
        assert sched["allowed"] is None, sched
        assert "evidence_missing" in err.getvalue(), err.getvalue()

        arch = f.freshness_of(jd, "a", consumer="archive")
        assert arch["state"] == "evidence_missing", arch
        assert arch["allowed"] is True, arch

        clean = f.freshness_of(jd, "a", consumer="clean")
        assert clean["state"] == "evidence_missing", clean
        assert clean["allowed"] is True, clean

        snap = f.archive_snapshot(jd, write=True)
        assert snap["evidence_missing"] == ["a"], snap["evidence_missing"]
        assert snap["versions"]["a"]["state"] == "evidence_missing"

        tp = f.write_tombstone(jd, "a")
        assert tp and os.path.isfile(tp)
        rec = json.load(open(tp, encoding="utf-8"))
        assert rec["state"] == "evidence_missing", rec
        assert rec["reason"] == "export_recorded_no_sidecar", rec
        assert rec["video"]["path"], rec
    _run(body)


def test_old_ledger_stays_legacy():
    """老台账（只有顶层 export_state/export_file，无 outputs）→ legacy。"""
    def body(base):
        jd, mp4 = _make_job(base, export_state="exported",
                            export_file=mp4_path_hint(base))
        sched = f.freshness_of(jd, "a", consumer="scheduler")
        assert sched["state"] == "unknown", sched
        arch = f.freshness_of(jd, "a", consumer="archive")
        assert arch["state"] == "legacy", arch
        assert arch["allowed"] is True, arch
    _run(body)


def mp4_path_hint(base):
    return os.path.join(base, "out.mp4")


def test_sidecar_makes_it_valid():
    """登记 exported 后补写 sidecar → valid（反向检测消失）。"""
    def body(base):
        jd, mp4 = _make_job(base, outputs=None)
        job = jm.load_job(os.path.join(jd, "job.json"))
        job["video"]["outputs"] = _exported_output(mp4)
        jm.save_job(job, jd)

        assert f.freshness_of(jd, "a")["state"] == "evidence_missing"
        assert f.record_export(jd, "a", "ev_a", mp4, verify_rc=0, compute_hash=False)
        assert os.path.isfile(mp4 + ".freshness.json")
        sched = f.freshness_of(jd, "a", consumer="scheduler")
        assert sched["state"] == "valid", sched
        assert sched["allowed"] is True, sched
        assert not (f.archive_snapshot(jd, write=False)["evidence_missing"])
    _run(body)


def test_exported_but_file_gone_is_output_missing():
    """登记 exported 但成品已不在 → output_missing（不是 evidence_missing）。"""
    def body(base):
        jd, mp4 = _make_job(base, outputs=None)
        job = jm.load_job(os.path.join(jd, "job.json"))
        job["video"]["outputs"] = _exported_output(mp4)
        jm.save_job(job, jd)
        os.remove(mp4)
        sched = f.freshness_of(jd, "a", consumer="scheduler")
        assert sched["state"] == "stale", sched
        assert sched["reason"] == "output_missing", sched
        arch = f.freshness_of(jd, "a", consumer="archive")
        assert arch["state"] == "stale" and arch["allowed"] is True, arch
    _run(body)


def test_cleaned_output_is_not_evidence_missing():
    """clean 已登记 cleaned 的版本不报 evidence_missing。"""
    def body(base):
        jd, mp4 = _make_job(base, outputs=None, with_mp4=False)
        job = jm.load_job(os.path.join(jd, "job.json"))
        job["video"]["outputs"] = {"a": {"draft": "ev_a", "state": "cleaned",
                                         "export_file": mp4.replace(os.sep, "/")}}
        jm.save_job(job, jd)
        arch = f.freshness_of(jd, "a", consumer="archive")
        assert arch["state"] == "legacy", arch
    _run(body)


def main():
    tests = [test_evidence_missing_is_not_legacy,
             test_old_ledger_stays_legacy,
             test_sidecar_makes_it_valid,
             test_exported_but_file_gone_is_output_missing,
             test_cleaned_output_is_not_evidence_missing]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"OK {len(tests)} tests")


if __name__ == "__main__":
    main()
