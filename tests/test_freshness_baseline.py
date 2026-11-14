"""产物新鲜度：一次性上线基线（legacy(baseline)）与 evidence_missing 的边界。

只读/临时目录自检，不碰 D:\\短剧、不启动剪映、不跑导出。
直接运行：python tests/test_freshness_baseline.py
"""

import contextlib
import importlib.util
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


def _load_generator():
    path = os.path.join(ROOT, "scripts", "freshness_baseline.py")
    spec = importlib.util.spec_from_file_location("freshness_baseline", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_job(base, name="j1", *, size=4096, with_mp4=True):
    jd = os.path.join(base, name, "cover_job")
    os.makedirs(os.path.join(jd, "06_publish"), exist_ok=True)
    mp4 = os.path.join(base, f"{name}.mp4")
    if with_mp4:
        with open(mp4, "wb") as fp:
            fp.write(b"z" * size)
    job = jm.build_job("自检剧")
    job["job_id"] = f"job-{name}"
    job["video"]["outputs"] = {
        "a": {"draft": f"draft_{name}", "export_dir": base.replace(os.sep, "/"),
              "export_file": mp4.replace(os.sep, "/"),
              "size_bytes": os.path.getsize(mp4) if with_mp4 else size,
              "state": "exported", "exported_at": jm.now_iso(), "history": []}
    }
    jm.save_job(job, jd)
    return jd, mp4


def _entry(jd, mp4, *, size=None, version="a", job_id=None, job=None):
    return {
        "job": (job or jd).replace(os.sep, "/"),
        "job_id": job_id or "job-j1",
        "series": "自检剧",
        "version": version,
        "draft": "draft_j1",
        "export_file": mp4.replace(os.sep, "/"),
        "size_bytes": size if size is not None else os.path.getsize(mp4),
        "exported_at": "2026-11-14T07:58:05+08:00",
    }


def _baseline_doc(entries):
    return {"schema": 1, "kind": f.BASELINE_KIND,
            "generated_at": "2026-11-14T00:00:00+08:00",
            "command": "unit-test", "scanned_roots": ["<test>"],
            "product_roots": [], "entries_count": len(entries),
            "entries_digest": f.baseline_entries_digest(entries),
            "entries": entries}


def _write_baseline(path, entries):
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(_baseline_doc(entries), fp, ensure_ascii=False, indent=2)


def _run(fn):
    base = tempfile.mkdtemp(prefix="jf_bl_")
    baseline = os.path.join(base, "baseline.json")
    old_root = f._drafts_root
    old_env = os.environ.get(f.BASELINE_ENV)
    f._drafts_root = lambda: base
    f._WARNED.clear()
    f._BASELINE_CACHE.update({"key": None, "data": None})
    os.environ[f.BASELINE_ENV] = baseline
    try:
        fn(base, baseline)
    finally:
        f._drafts_root = old_root
        if old_env is None:
            os.environ.pop(f.BASELINE_ENV, None)
        else:
            os.environ[f.BASELINE_ENV] = old_env
        f._BASELINE_CACHE.update({"key": None, "data": None})
        shutil.rmtree(base, ignore_errors=True)


def test_baseline_hit_is_legacy_no_warning():
    """命中冻结清单 → legacy(baseline)，不告警；快照/墓碑都标注 baseline。"""
    def body(base, baseline):
        jd, mp4 = _make_job(base)
        _write_baseline(baseline, [_entry(jd, mp4)])
        assert f.load_baseline()["valid"], f.load_baseline()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            sched = f.freshness_of(jd, "a", consumer="scheduler")
        assert sched["state"] == "legacy", sched
        assert sched["reason"] == "baseline", sched
        assert sched["allowed"] is None, sched
        assert "evidence_missing" not in err.getvalue(), err.getvalue()

        for consumer in ("archive", "clean"):
            r = f.freshness_of(jd, "a", consumer=consumer)
            assert r["state"] == "legacy" and r["allowed"] is True, r

        snap = f.archive_snapshot(jd, write=True)
        assert snap["evidence_missing"] == [], snap
        assert snap["legacy_baseline"] == ["a"], snap

        tp = f.write_tombstone(jd, "a")
        rec = json.load(open(tp, encoding="utf-8"))
        assert rec["state"] == "legacy" and rec["reason"] == "baseline", rec
    _run(body)


def test_baseline_miss_is_evidence_missing():
    """清单里没有 / size 不符 → 仍报 evidence_missing 并告警。"""
    def body(base, baseline):
        jd, mp4 = _make_job(base)
        _write_baseline(baseline, [_entry(jd, mp4, size=4095)])  # 故意错 size
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            r = f.freshness_of(jd, "a", consumer="scheduler")
        assert r["state"] == "evidence_missing", r
        assert "evidence_missing" in err.getvalue(), err.getvalue()

        _write_baseline(baseline, [_entry(jd, mp4, job_id="job-other",
                                          job=os.path.join(base, "other", "cover_job"))])
        f._BASELINE_CACHE.update({"key": None, "data": None})
        r = f.freshness_of(jd, "a", consumer="scheduler")
        assert r["state"] == "evidence_missing", r
    _run(body)


def test_digest_mismatch_fails_closed():
    """手改清单但没重算 digest → 整份失效，回到 evidence_missing。"""
    def body(base, baseline):
        jd, mp4 = _make_job(base)
        _write_baseline(baseline, [_entry(jd, mp4)])
        doc = json.load(open(baseline, encoding="utf-8"))
        doc["entries"].append(_entry(jd, mp4, version="b"))  # 偷偷加一条，不改 digest
        json.dump(doc, open(baseline, "w", encoding="utf-8"), ensure_ascii=False)
        f._BASELINE_CACHE.update({"key": None, "data": None})
        b = f.load_baseline()
        assert not b["valid"] and b["reason"] == "digest_mismatch", b
        r = f.freshness_of(jd, "a", consumer="scheduler")
        assert r["state"] == "evidence_missing", r
    _run(body)


def test_runtime_never_writes_baseline():
    """freshness_of / archive_snapshot / write_tombstone 都不得改写清单。"""
    def body(base, baseline):
        jd, mp4 = _make_job(base)
        _write_baseline(baseline, [_entry(jd, mp4)])
        before = open(baseline, "rb").read()
        mtime = os.stat(baseline).st_mtime_ns
        f.freshness_of(jd, "a", consumer="archive")
        f.archive_snapshot(jd, write=True)
        f.write_tombstone(jd, "a")
        assert open(baseline, "rb").read() == before
        assert os.stat(baseline).st_mtime_ns == mtime
    _run(body)


def test_generator_scan_and_guard():
    """生成器：只认 exported 且无 sidecar；不认识的结构列出来；写盘需显式标志。"""
    gen = _load_generator()
    base = tempfile.mkdtemp(prefix="jf_gen_")
    try:
        # a: 已导出、无 sidecar → 应进清单
        jd_a = os.path.join(base, "a", "cover_job")
        os.makedirs(jd_a)
        mp4_a = os.path.join(base, "a.mp4")
        open(mp4_a, "wb").write(b"x" * 100)
        json.dump({"job_id": "ja", "series": "A", "video": {"outputs": {
            "a": {"draft": "da", "state": "exported",
                  "export_file": mp4_a.replace(os.sep, "/"), "size_bytes": 100}}}},
            open(os.path.join(jd_a, "job.json"), "w", encoding="utf-8"))
        # b: 已导出、有 sidecar → 跳过
        jd_b = os.path.join(base, "b", "cover_job")
        os.makedirs(jd_b)
        mp4_b = os.path.join(base, "b.mp4")
        open(mp4_b, "wb").write(b"y" * 50)
        open(mp4_b + ".freshness.json", "w").write("{}")
        json.dump({"job_id": "jb", "series": "B", "video": {"outputs": {
            "a": {"draft": "db", "state": "exported",
                  "export_file": mp4_b.replace(os.sep, "/"), "size_bytes": 50}}}},
            open(os.path.join(jd_b, "job.json"), "w", encoding="utf-8"))
        # c: 不认识的结构 → 列出来，不猜
        jd_c = os.path.join(base, "c", "cover_job")
        os.makedirs(jd_c)
        json.dump({"job_id": "jc", "series": "C", "video": {"outputs": {
            "a": {"draft": "dc", "state": "mystery"}}}},
            open(os.path.join(jd_c, "job.json"), "w", encoding="utf-8"))

        out = os.path.join(base, "out.json")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gen.main(["--root", base, "--out", out, "--write"])
        assert rc == 0, rc
        doc = json.load(open(out, encoding="utf-8"))
        assert doc["entries_count"] == 1, doc
        assert doc["entries"][0]["job_id"] == "ja", doc
        assert f.baseline_entries_digest(doc["entries"]) == doc["entries_digest"]
        report_text = io.StringIO()
        with contextlib.redirect_stdout(report_text):
            entries, report = gen.scan_roots([base])
        assert len(entries) == 1, entries
        assert len(report["unrecognized"]) == 1, report
        assert report["stats"]["has_sidecar"] == 1, report

        # 已存在且不给 --append/--force → 拒绝（防悄悄扩充/覆盖）
        before = open(out, "rb").read()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gen.main(["--root", base, "--out", out, "--write"])
        assert rc == 4, rc
        assert open(out, "rb").read() == before

        # --append：新增一条，旧条目保留
        jd_d = os.path.join(base, "d", "cover_job")
        os.makedirs(jd_d)
        mp4_d = os.path.join(base, "d.mp4")
        open(mp4_d, "wb").write(b"w" * 70)
        json.dump({"job_id": "jd", "series": "D", "video": {"outputs": {
            "a": {"draft": "dd", "state": "exported",
                  "export_file": mp4_d.replace(os.sep, "/"), "size_bytes": 70}}}},
            open(os.path.join(jd_d, "job.json"), "w", encoding="utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = gen.main(["--root", base, "--out", out, "--append", "--write"])
        assert rc == 0, rc
        doc2 = json.load(open(out, encoding="utf-8"))
        assert doc2["entries_count"] == 2, doc2
        assert {e["job_id"] for e in doc2["entries"]} == {"ja", "jd"}, doc2
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main():
    tests = [test_baseline_hit_is_legacy_no_warning,
             test_baseline_miss_is_evidence_missing,
             test_digest_mismatch_fails_closed,
             test_runtime_never_writes_baseline,
             test_generator_scan_and_guard]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"OK {len(tests)} tests")


if __name__ == "__main__":
    main()
