# -*- coding: utf-8 -*-
"""串流恢复的内容门槛：duration 与素材 id 集合必须同时相等才允许备份覆盖 live。

覆盖：
  - duration 不等 → 跳过恢复，日志说明原因，草稿不被改写
  - 素材 id 集合不等 → 跳过恢复，日志说明原因
  - 两者相等 → 恢复轨道，并经 draft_io 留 freshness
    （writer=stream_restore，E1 valid / E2 stale）
  - 恢复内容违反草稿不变量 → DraftInvariantError 上抛，调用方不降级成继续导出

运行：pytest tests/test_stream_restore_gate.py -q
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline", os.path.join(".trae", "skills", "jianying-editor", "examples")):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import draft_io  # noqa: E402
import freshness  # noqa: E402
import robust_auto_export as rae  # noqa: E402

NAME = "测试剧_第01-2集串流"
DURATION = 6_000_000  # 6s @30fps


def _seg(mid, dur):
    return {
        "material_id": mid,
        "source_timerange": {"start": 0, "duration": dur},
        "target_timerange": {"start": 0, "duration": dur},
        "extra_material_refs": [],
    }


def _draft(duration, segs, material_duration=DURATION, extra_material=None):
    mats = {"videos": [{"id": "m1", "path": "", "duration": material_duration}]}
    if extra_material:
        mats["audios"] = [{"id": extra_material, "duration": 1000}]
    return {
        "duration": duration,
        "fps": 30,
        "materials": mats,
        "tracks": [
            {"type": "video", "segments": segs},
            {"type": "text", "segments": []},
        ],
    }


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _setup(tmp_path, monkeypatch, cur, bak, with_job=True):
    drafts = tmp_path / "drafts"
    monkeypatch.setenv("JY_PROJECTS_ROOT", str(drafts))
    ddir = drafts / NAME
    _write(str(ddir / "draft_content.json"), cur)
    _write(str(ddir / "_pipeline_backup.json"), bak)
    monkeypatch.setattr(freshness, "_drafts_root", lambda: str(drafts))
    job_dir = None
    if with_job:
        series = tmp_path / "剧"
        job_dir = series / "cover_job"
        _write(str(job_dir / "job.json"),
               {"revision": 1, "workflow": {}, "video": {}})
        pipeline = tmp_path / "pipeline"
        _write(str(pipeline / "_stream_manifests" / (NAME + ".json")),
               {"paths": {"source_dir": str(series)}})
        monkeypatch.setattr(freshness, "_PIPELINE_DIR", str(pipeline))
    return ddir, job_dir


def _merged():
    """剪映合并后的 live：整条 1 段。"""
    return _draft(DURATION, [_seg("m1", DURATION)])


def _backup_ok():
    """pipeline 备份：3 段，段数与素材集合都和 live 对得上。"""
    return _draft(DURATION, [_seg("m1", 2_000_000) for _ in range(3)])


# ── 门槛拒绝 ─────────────────────────────────────────────────────

def test_duration_mismatch_skips_restore(tmp_path, monkeypatch, capsys):
    cur = _merged()
    bak = _backup_ok()
    bak["duration"] = DURATION + 1_000_000  # 备份属于另一版时间线
    ddir, _ = _setup(tmp_path, monkeypatch, cur, bak)
    before = (ddir / "draft_content.json").read_bytes()

    assert rae._restore_streams_from_backup(NAME) is False

    out = capsys.readouterr().out
    assert "跳过串流恢复" in out and "duration" in out and "不等" in out
    assert (ddir / "draft_content.json").read_bytes() == before


def test_material_set_mismatch_skips_restore(tmp_path, monkeypatch, capsys):
    cur = _merged()
    bak = _backup_ok()
    bak["materials"]["audios"] = [{"id": "a-only-in-backup", "duration": 1000}]
    ddir, _ = _setup(tmp_path, monkeypatch, cur, bak)
    before = (ddir / "draft_content.json").read_bytes()

    assert rae._restore_streams_from_backup(NAME) is False

    out = capsys.readouterr().out
    assert "跳过串流恢复" in out and "素材 id 集合" in out and "不等" in out
    assert "仅备份 1 个" in out
    assert (ddir / "draft_content.json").read_bytes() == before


# ── 门槛通过：恢复 + 留痕 ────────────────────────────────────────

def test_equal_duration_and_materials_restores_with_freshness(tmp_path, monkeypatch):
    ddir, job_dir = _setup(tmp_path, monkeypatch, _merged(), _backup_ok())

    assert rae._restore_streams_from_backup(NAME) is True

    after = _read(str(ddir / "draft_content.json"))
    video = [t for t in after["tracks"] if t["type"] == "video"][0]
    assert len(video["segments"]) == 3
    # 恢复前自动留了一份现场
    assert (ddir / "draft_content.json.before_restore.bak").is_file()

    job = _read(str(job_dir / "job.json"))
    e1 = job["freshness"]["edges"]["prompts_to_draft"]
    e2 = job["freshness"]["edges"]["draft_to_video"]
    assert e1["state"] == "valid"
    assert e1["last_writer"] == "stream_restore"
    assert e1["draft"] == NAME
    assert e2["state"] == "stale"
    assert e2["detail"] == "writer=stream_restore"


# ── 不变量失败必须上抛 ──────────────────────────────────────────

def test_invariant_violation_raises(tmp_path, monkeypatch):
    # 素材只有 1s，备份段却写了 6s → 恢复后草稿必然被剪映截断/漂移
    cur = _draft(DURATION, [_seg("m1", DURATION)], material_duration=1_000_000)
    bak = _draft(DURATION, [_seg("m1", DURATION) for _ in range(3)],
                 material_duration=1_000_000)
    ddir, _ = _setup(tmp_path, monkeypatch, cur, bak, with_job=False)
    before = (ddir / "draft_content.json").read_bytes()

    with pytest.raises(draft_io.DraftInvariantError):
        rae._restore_streams_from_backup(NAME)

    # 校验失败不落盘
    assert (ddir / "draft_content.json").read_bytes() == before


def test_wrapper_reraises_invariant_error(monkeypatch):
    monkeypatch.setattr(rae, "_flow", lambda *a, **k: None)

    def boom(_name):
        raise draft_io.DraftInvariantError("boom")

    monkeypatch.setattr(rae, "_restore_streams_from_backup", boom)
    with pytest.raises(draft_io.DraftInvariantError):
        rae._restore_streams_before_export(NAME)


def test_wrapper_swallows_other_errors(monkeypatch):
    monkeypatch.setattr(rae, "_flow", lambda *a, **k: None)

    def boom(_name):
        raise OSError("读取失败")

    monkeypatch.setattr(rae, "_restore_streams_from_backup", boom)
    assert rae._restore_streams_before_export(NAME) is False
