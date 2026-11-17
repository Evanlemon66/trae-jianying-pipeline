"""产物新鲜度：E1 与人工确认（manual_confirmed）的优先级。

覆盖三条边界：
  1. 人工确认后 E1 放行（不再判 externally_modified）；
  2. 没有确认记录时仍然拦（externally_modified 照旧）；
  3. 确认后产物 size 变了 → 失效（manual_confirmed_artifact_changed）。

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
    draft_dir = os.path.join(base, "mc_a")
    os.makedirs(draft_dir, exist_ok=True)
    draft_path = os.path.join(draft_dir, "draft_content.json")
    with open(draft_path, "w", encoding="utf-8") as fp:
        json.dump(MINI_DRAFT, fp)

    mp4 = os.path.join(base, "out.mp4")
    with open(mp4, "wb") as fp:
        fp.write(b"z" * 4096)

    job = jm.build_job("自检剧")
    job["video"]["outputs"] = {
        "a": {"draft": "mc_a", "export_dir": base.replace(os.sep, "/"),
              "export_file": mp4.replace(os.sep, "/"),
              "size_bytes": os.path.getsize(mp4), "state": "exported",
              "exported_at": jm.now_iso(), "history": []}}
    jm.save_job(job, jd)
    assert f.record_draft_write(jd, draft_path, writer="test")
    return jd, draft_path, mp4


def _touch_draft(draft_path):
    """让 E1 output_hash 与记录不一致（不是纯序列化差异）。"""
    d = json.load(open(draft_path, encoding="utf-8"))
    d["duration"] = 2000000
    d["materials"]["texts"] = [{"id": "t1", "content": "外部改动"}]
    with open(draft_path, "w", encoding="utf-8") as fp:
        json.dump(d, fp)


def _run(fn):
    base = tempfile.mkdtemp(prefix="jf_mc_")
    old_root = f._drafts_root
    f._drafts_root = lambda: base
    try:
        fn(base)
    finally:
        f._drafts_root = old_root
        shutil.rmtree(base, ignore_errors=True)


def test_manual_confirmed_releases_e1():
    def body(base):
        jd, draft_path, _mp4 = _setup(base)
        _touch_draft(draft_path)

        before = f.check(jd, "e1")
        assert before["state"] == "stale", before
        assert before["reason"] == "externally_modified", before

        assert f.reuse_as_is(jd, version="a", why="自检人工确认")
        after = f.check(jd, "e1")
        assert after["state"] == "manual_confirmed", after
        assert after["reason"] == "manual_confirmed", after
        assert f.freshness_of(jd, "a")["allowed"] is True
    _run(body)


def test_without_confirmation_still_blocks():
    def body(base):
        jd, draft_path, _mp4 = _setup(base)
        _touch_draft(draft_path)
        res = f.check(jd, "e1")
        assert res["state"] == "stale", res
        assert res["reason"] == "externally_modified", res
    _run(body)


def test_confirmed_artifact_size_change_invalidates():
    def body(base):
        jd, draft_path, mp4 = _setup(base)
        _touch_draft(draft_path)
        assert f.reuse_as_is(jd, version="a", why="自检人工确认")
        assert f.check(jd, "e1")["state"] == "manual_confirmed"

        with open(mp4, "ab") as fp:
            fp.write(b"x" * 1024)

        res = f.check(jd, "e1")
        assert res["state"] == "stale", res
        assert res["reason"] == "manual_confirmed_artifact_changed", res
        assert res["evidence"]["video"]["size_bytes"] == os.path.getsize(mp4)
    _run(body)
