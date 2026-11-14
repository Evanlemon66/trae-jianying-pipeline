# -*- coding: utf-8 -*-
"""R05：素材缺失 fail-closed。

覆盖：
  - 草稿缺 draft_content.json 时，引用扫描不得静默跳过（备份兜底 / 返回 None）
  - 草稿内容损坏时返回 None
  - 无草稿标记的普通目录直接跳过
  - 导出预检在 ffprobe 缺失时仍拦住不存在的素材

运行：pytest tests/test_media_refs_fail_closed.py -q
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline", os.path.join(".trae", "skills", "jianying-editor", "examples")):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pipeline as pl  # noqa: E402
import robust_auto_export as rae  # noqa: E402


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, ensure_ascii=False)


def _draft(audio=None, video=None):
    mats = {"audios": [], "videos": []}
    if audio:
        mats["audios"].append({"id": "a1", "path": audio})
    if video:
        mats["videos"].append({"id": "v1", "path": video})
    return {"materials": mats, "tracks": []}


def _draft_root(tmp_path, monkeypatch):
    root = tmp_path / "drafts"
    root.mkdir()
    monkeypatch.setattr(pl, "_JIANYING_DRAFT_ROOT", str(root))
    return root


# ── 引用扫描 ─────────────────────────────────────────────────────

def test_missing_content_falls_back_to_pipeline_backup(tmp_path, monkeypatch, capsys):
    root = _draft_root(tmp_path, monkeypatch)
    d = root / "测试剧A"
    d.mkdir()
    _write(str(d / "draft_meta_info.json"), {})
    audio = str(tmp_path / "a.wav")
    video = str(tmp_path / "v.mp4")
    _write(str(d / "_pipeline_backup.json"), _draft(audio=audio, video=video))

    refs = pl.referenced_media_paths()

    assert refs == {pl._media_key(audio), pl._media_key(video)}
    assert "已从 _pipeline_backup.json 补全引用" in capsys.readouterr().out


def test_missing_content_without_backup_returns_none(tmp_path, monkeypatch, capsys):
    root = _draft_root(tmp_path, monkeypatch)
    d = root / "测试剧A"
    d.mkdir()
    _write(str(d / "draft_meta_info.json"), {})

    assert pl.referenced_media_paths() is None
    out = capsys.readouterr().out
    assert "缺少 draft_content.json" in out and "无法确认引用关系" in out


def test_corrupt_content_returns_none(tmp_path, monkeypatch):
    root = _draft_root(tmp_path, monkeypatch)
    d = root / "测试剧A"
    d.mkdir()
    _write(str(d / "draft_content.json"), "{not-json")

    assert pl.referenced_media_paths() is None


def test_non_draft_dir_is_skipped(tmp_path, monkeypatch):
    root = _draft_root(tmp_path, monkeypatch)
    d = root / "回收站"
    d.mkdir()
    _write(str(d / "notes.txt"), "x")

    assert pl.referenced_media_paths() == set()


# ── 导出预检 ─────────────────────────────────────────────────────

def test_preflight_blocks_missing_media_without_ffprobe(tmp_path, monkeypatch):
    root = tmp_path / "drafts"
    d = root / "测试剧A"
    d.mkdir(parents=True)
    missing = str(tmp_path / "missing.mp4")
    _write(str(d / "draft_content.json"), _draft(audio=None, video=missing))
    monkeypatch.setenv("JY_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(rae, "_find_ffprobe", lambda: None)

    ok_n, problems = rae._preflight_media("测试剧A")

    assert ok_n == 0
    assert problems == [(missing, "文件不存在")]


def test_preflight_without_ffprobe_passes_existing_file(tmp_path, monkeypatch):
    root = tmp_path / "drafts"
    d = root / "测试剧A"
    d.mkdir(parents=True)
    exists = tmp_path / "ok.mp4"
    exists.write_bytes(b"x" * 2048)
    _write(str(d / "draft_content.json"), _draft(audio=None, video=str(exists)))
    monkeypatch.setenv("JY_PROJECTS_ROOT", str(root))
    monkeypatch.setattr(rae, "_find_ffprobe", lambda: None)

    ok_n, problems = rae._preflight_media("测试剧A")

    assert ok_n == 1
    assert problems == []
