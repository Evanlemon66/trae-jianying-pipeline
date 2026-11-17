# -*- coding: utf-8 -*-
"""drama_cover 写盘收敛到唯一原语 pipeline.draft_io 的回归测试。

覆盖：
  ① save_draft 就是把活交给原语（同一套 校验→备份→原子替换→留痕）；
  ② 不变量不过时拒绝落盘，草稿一个字节不动、不留备份/临时文件；
  ③ write-draft 端到端：cover jpg + 原生 composition 落盘，无 .tmp 残留。
"""
import argparse
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.join(_ROOT, ".trae", "skills", "drama-cover-builder", "scripts"))

import draft_io  # noqa: E402
import drama_cover as dc  # noqa: E402


def _video_draft(src_us, mat_us):
    """单轨单段草稿：source 引用 videos 素材，超出素材时长即违反不变量。"""
    return {
        "fps": 30.0,
        "materials": {"videos": [{"id": "VID", "duration": mat_us, "path": "a.mp4"}]},
        "tracks": [{"type": "video", "segments": [{
            "id": "seg-VID", "material_id": "VID", "extra_material_refs": [],
            "source_timerange": {"start": 0, "duration": src_us},
            "target_timerange": {"start": 0, "duration": src_us},
        }]}],
    }


def _backups(tmp_path):
    return sorted(p for p in os.listdir(tmp_path) if ".coverbak_" in p)


def test_save_draft_delegates_to_primitive(tmp_path, monkeypatch):
    """save_draft 必须调用原语，并带上备份后缀 / writer / indent=2。"""
    calls = []
    monkeypatch.setattr(draft_io, "write_draft_content",
                        lambda draft, path, **kw: calls.append((path, kw)))
    dc.save_draft(_video_draft(3_000_000, 5_000_000),
                  str(tmp_path / "draft_content.json"))
    assert len(calls) == 1
    path, kw = calls[0]
    assert os.path.basename(path) == "draft_content.json"
    assert kw["backup_suffix"] == ".coverbak_{ts}"
    assert kw["writer"] == "drama_cover"
    assert kw["indent"] == 2


def test_save_draft_backs_up_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "draft_content.json"
    target.write_text(json.dumps(_video_draft(4_000_000, 5_000_000)),
                      encoding="utf-8")
    old_text = target.read_text(encoding="utf-8")

    bak = dc.save_draft(_video_draft(3_000_000, 5_000_000), str(target))

    assert bak and os.path.isfile(bak)
    assert os.path.basename(bak).startswith("draft_content.json.coverbak_")
    assert open(bak, encoding="utf-8").read() == old_text
    assert _backups(tmp_path) == [os.path.basename(bak)]
    assert json.loads(target.read_text(encoding="utf-8"))["tracks"][0][
        "segments"][0]["source_timerange"]["duration"] == 3_000_000
    assert not os.path.exists(str(target) + ".tmp")


def test_save_draft_rejects_without_touching_file(tmp_path):
    target = tmp_path / "draft_content.json"
    before = json.dumps(_video_draft(4_000_000, 5_000_000))
    target.write_text(before, encoding="utf-8")

    with pytest.raises(draft_io.DraftInvariantError):
        dc.save_draft(_video_draft(8_000_000, 5_000_000), str(target))

    assert target.read_text(encoding="utf-8") == before
    assert _backups(tmp_path) == []
    assert not os.path.exists(str(target) + ".tmp")


def test_save_draft_records_freshness(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(draft_io, "_record_freshness",
                        lambda job_dir, path, writer: calls.append((job_dir, path, writer)))
    target = tmp_path / "draft_content.json"
    dc.save_draft(_video_draft(3_000_000, 5_000_000), str(target))
    assert len(calls) == 1
    assert calls[0][2] == "drama_cover"


def test_write_draft_attaches_cover_and_composition(tmp_path):
    draft_dir = tmp_path / "ep01"
    draft_dir.mkdir()
    (draft_dir / "draft_content.json").write_text(
        json.dumps(_video_draft(3_000_000, 5_000_000)), encoding="utf-8")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    rc = dc._cmd_write_draft(argparse.Namespace(
        cover=str(cover), draft_dir=str(draft_dir)))

    assert rc == 0
    assert (draft_dir / "draft_cover.jpg").is_file()
    saved = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    comps = [m for m in saved["materials"]["drafts"] if m.get("type") == "composition"]
    assert len(comps) == 1
    assert saved["cover"]["cover_draft_id"] == comps[0]["id"]
    cover_files = os.listdir(draft_dir / "Resources" / "cover")
    assert len(cover_files) == 1
    # 原子写：备份有、临时文件没有
    assert len(_backups(draft_dir)) == 1
    assert not os.path.exists(str(draft_dir / "draft_content.json") + ".tmp")


def test_write_draft_refuses_bad_invariant(tmp_path):
    """不变量不过：write-draft 返回 1，draft_content.json 保持原样。"""
    draft_dir = tmp_path / "ep02"
    draft_dir.mkdir()
    before = json.dumps(_video_draft(4_000_000, 5_000_000))
    (draft_dir / "draft_content.json").write_text(before, encoding="utf-8")
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")

    bad = _video_draft(8_000_000, 5_000_000)
    (draft_dir / "draft_content.json").write_text(json.dumps(bad), encoding="utf-8")
    before = (draft_dir / "draft_content.json").read_text(encoding="utf-8")

    rc = dc._cmd_write_draft(argparse.Namespace(
        cover=str(cover), draft_dir=str(draft_dir)))

    assert rc == 1
    assert (draft_dir / "draft_content.json").read_text(encoding="utf-8") == before
    assert _backups(draft_dir) == []
    assert not os.path.exists(str(draft_dir / "draft_content.json") + ".tmp")
