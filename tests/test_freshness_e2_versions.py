"""R06：freshness E2 按版本分槽，A/B 各自留证、互不覆盖。

覆盖三点：
  1. 两版都导出后，删掉 A 的 sidecar 仍能靠 A 自己的 E2 槽判 valid；
  2. 改 A 草稿只把 A 置 stale，B 的证据不受影响；
  3. 旧 job.json（没有 versions 分槽）仍按平铺字段判，保持兼容。

只读/临时目录自检，不碰 <素材根>、不启动剪映。
"""

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


def _setup(base):
    jd = os.path.join(base, "cover_job")
    os.makedirs(os.path.join(jd, "06_publish"), exist_ok=True)
    outs = {}
    drafts = {}
    mp4s = {}
    for i, ver in enumerate(("a", "b")):
        name = "multi_" + ver
        ddir = os.path.join(base, name)
        os.makedirs(ddir, exist_ok=True)
        draft_path = os.path.join(ddir, "draft_content.json")
        with open(draft_path, "w", encoding="utf-8") as fp:
            json.dump(MINI_DRAFT, fp)
        mp4 = os.path.join(base, "out_%s.mp4" % ver)
        with open(mp4, "wb") as fp:
            fp.write(b"x" * (4096 * (i + 1)))
        outs[ver] = {
            "draft": name, "export_dir": base.replace(os.sep, "/"),
            "export_file": mp4.replace(os.sep, "/"),
            "size_bytes": os.path.getsize(mp4), "state": "exported",
            "exported_at": jm.now_iso(), "history": [],
        }
        drafts[ver] = draft_path
        mp4s[ver] = mp4
    job = jm.build_job("自检剧")
    job["video"]["outputs"] = outs
    jm.save_job(job, jd)
    return jd, drafts, mp4s


def _run(fn):
    base = tempfile.mkdtemp(prefix="jf_e2v_")
    old_root = f._drafts_root
    f._drafts_root = lambda: base
    try:
        fn(base)
    finally:
        f._drafts_root = old_root
        shutil.rmtree(base, ignore_errors=True)


def test_each_version_keeps_its_own_e2_slot():
    def body(base):
        jd, _drafts, mp4s = _setup(base)
        assert f.record_export(jd, "a", "multi_a", mp4s["a"])
        assert f.record_export(jd, "b", "multi_b", mp4s["b"])
        job = jm.load_job(os.path.join(jd, "job.json"))
        e2 = job["freshness"]["edges"][f.EDGE_E2]
        assert e2["version"] == "b" and e2["versions"]["a"]["version"] == "a"
        assert e2["versions"]["a"]["output"]["size_bytes"] == os.path.getsize(mp4s["a"])
        assert e2["versions"]["b"]["output"]["size_bytes"] == os.path.getsize(mp4s["b"])

        # 删掉 A 的 sidecar：只能靠 A 自己的分槽判，不得借用 B 的记录
        os.remove(mp4s["a"] + ".freshness.json")
        ra = f.freshness_of(jd, "a")
        assert ra["state"] == "valid", ra
        assert ra["evidence"]["record"]["version"] == "a", ra
        assert f.freshness_of(jd, "b")["state"] == "valid"
    _run(body)


def test_draft_write_only_invalidates_that_version():
    def body(base):
        jd, drafts, mp4s = _setup(base)
        assert f.record_export(jd, "a", "multi_a", mp4s["a"])
        assert f.record_export(jd, "b", "multi_b", mp4s["b"])
        # 两版都删 sidecar，走 E2 记录兜底路径
        os.remove(mp4s["a"] + ".freshness.json")
        os.remove(mp4s["b"] + ".freshness.json")
        assert f.freshness_of(jd, "a")["state"] == "valid"
        assert f.freshness_of(jd, "b")["state"] == "valid"

        assert f.record_draft_write(jd, drafts["a"], writer="test")

        ra = f.freshness_of(jd, "a")
        rb = f.freshness_of(jd, "b")
        assert ra["state"] == "stale" and ra["reason"] == "draft_changed", ra
        assert rb["state"] == "valid", rb
    _run(body)


def test_legacy_job_without_slots_still_valid():
    def body(base):
        jd, _drafts, mp4s = _setup(base)
        assert f.record_export(jd, "a", "multi_a", mp4s["a"])
        # 模拟旧 job.json：没有 versions 分槽，也没有 version 字段
        job = jm.load_job(os.path.join(jd, "job.json"))
        e2 = job["freshness"]["edges"][f.EDGE_E2]
        e2.pop("versions", None)
        e2.pop("version", None)
        jm.save_job(job, jd)
        os.remove(mp4s["a"] + ".freshness.json")
        ra = f.freshness_of(jd, "a")
        assert ra["state"] == "valid", ra
    _run(body)
