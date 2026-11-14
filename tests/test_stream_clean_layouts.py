# -*- coding: utf-8 -*-
"""clean 的删除计划：两种导出布局 + 保护区不被放宽。

- bundle 布局（成品/封面/进度同处「剧名子目录」）：整目录一次删，目录里的其它残留一并清掉
- 平铺布局（旧）：只列 mp4 / progress / 封面三项，不误删旁边的无关文件
- `_blocked_reason` 对 cover_job / pipeline/_work / s2·s3·ss.mp4 的保护保持原样
"""
import contextlib
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline", "tests"):
    sys.path.insert(0, os.path.join(ROOT, _sub))

import job_manifest as jm  # noqa: E402
import stream_manifest as sm  # noqa: E402
from bundle_fixture import make_bundle  # noqa: E402


def _run_clean(monkeypatch, tmp_path, name, paths, **kw):
    monkeypatch.setattr(sm, "MANIFEST_DIR", str(tmp_path / "manifests"))
    sm.record(name, **paths)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sm.clean(name, **kw)
    return rc, buf.getvalue()


def _make_job(src, name, export_file):
    job_dir = os.path.join(str(src), "cover_job")
    os.makedirs(os.path.join(job_dir, "06_publish"), exist_ok=True)
    job = jm.build_job(name)
    job["stage"] = "published"
    job["publish"] = {"status": "published"}
    job["video"]["status"] = "done"
    job["video"]["export_state"] = "exported"
    job["video"]["outputs"] = {
        "a": {"draft": f"{name}_a", "export_file": export_file.replace(os.sep, "/"),
              "state": "exported"},
    }
    jm.save_job(job, job_dir)


def test_bundle_layout_deletes_whole_dir(tmp_path, monkeypatch):
    """bundle 布局：整目录删，目录内的附加残留也不会漏。"""
    name = "剧A"
    src = tmp_path / "剧A" / "src"
    bundle = tmp_path / "out" / name
    made = make_bundle(bundle, name)
    (bundle / "stall.png").write_bytes(b"x" * 10)
    (bundle / "leftover.txt").write_text("残留", encoding="utf-8")
    _make_job(src, name, str(made["mp4"]))
    assert os.path.isdir(str(bundle))

    rc, out = _run_clean(monkeypatch, tmp_path, name,
                         {"source_dir": str(src),
                          "export_file": str(made["mp4"])})

    assert rc == 0
    assert not os.path.exists(str(bundle)), "bundle 目录应整体删除"
    assert "export_bundle" in out


def test_flat_layout_deletes_only_three_items(tmp_path, monkeypatch):
    """平铺布局：只删 mp4 / progress / 封面三项，旁边无关文件保留。"""
    name = "剧A"
    src = tmp_path / "剧A" / "src"
    flat = tmp_path / "flat"
    made = make_bundle(flat, name)
    other = flat / "other.mp4"
    other.write_bytes(b"y" * 10)
    side = flat / (name + ".progress.json")
    side.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    _make_job(src, name, str(made["mp4"]))

    rc, out = _run_clean(monkeypatch, tmp_path, name,
                         {"source_dir": str(src),
                          "export_file": str(made["mp4"])})

    assert rc == 0
    assert not os.path.exists(str(made["mp4"]))
    assert not os.path.exists(str(made["progress"]))
    assert not os.path.exists(str(made["cover"]))
    assert other.exists(), "平铺布局不得误删无关文件"
    assert side.exists(), "平铺布局不额外删同级心跳（progress 已在三件套内）"
    assert out.count("[已删]") == 3, "平铺布局应删 mp4 / progress / 封面三项"
    assert "剧A.progress.json" in out
    assert "剧A-封面.jpg" in out
    assert "export_bundle" not in out


def test_bundle_clean_removes_stale_progress_heartbeat(tmp_path, monkeypatch):
    """bundle 整目录档：同级 status=running 的导出心跳一并删掉，且删除前打印状态。"""
    name = "剧A"
    src = tmp_path / "剧A" / "src"
    bundle = tmp_path / "out" / name
    made = make_bundle(bundle, name)
    side = bundle.parent / (name + ".progress.json")
    side.write_text(json.dumps({"status": "running", "done": False}),
                    encoding="utf-8")
    _make_job(src, name, str(made["mp4"]))

    rc, out = _run_clean(monkeypatch, tmp_path, name,
                         {"source_dir": str(src),
                          "export_file": str(made["mp4"])})

    assert rc == 0
    assert not bundle.exists(), "bundle 目录应整体删除"
    assert not side.exists(), "同级导出心跳应一并删除"
    assert "[心跳]" in out and "status=running" in out, "删除前必须写明心跳状态"


def test_protected_areas_not_loosened(tmp_path, monkeypatch):
    """保护断言：cover_job / pipeline/_work / 散落成品文件命中即跳过。"""
    name = "剧A"
    src = tmp_path / "剧A" / "src"
    _make_job(src, name, str(tmp_path / "nope.mp4"))
    in_cover = src / "cover_job" / "__jycache__"
    in_cover.mkdir(parents=True)
    (in_cover / "keep.bin").write_bytes(b"z")
    work = tmp_path / "pipeline" / "_work"
    work.mkdir(parents=True)
    (work / "keep.bin").write_bytes(b"z")
    loose = tmp_path / "pipeline" / "s2.mp4"
    loose.write_bytes(b"z")

    rc, out = _run_clean(
        monkeypatch, tmp_path, name,
        {"source_dir": str(src), "jycache_dir": str(in_cover),
         "workdir": str(work), "export_file": str(loose)},
        force=True)

    assert rc == 0
    assert (in_cover / "keep.bin").exists()
    assert (work / "keep.bin").exists()
    assert loose.exists()
    assert out.count("[受保护]") == 3
