# -*- coding: utf-8 -*-
"""多账号预设组 + 克隆草稿换预设层 的回归测试。"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))

import pipeline as pl  # noqa: E402
import preset_ops  # noqa: E402


def _write_cfg(tmp_path, cfg):
    p = tmp_path / "presets.json"
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(p)


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "list_available_presets", lambda: [])
    monkeypatch.setattr(pl, "warn_missing_presets", lambda *a, **k: None)

    def _set(cfg):
        monkeypatch.setattr(pl, "PRESETS_JSON", _write_cfg(tmp_path, cfg))
    return _set


def test_profile_selects_group(cfg_file):
    cfg_file({"presets": [{"name": "默认"}],
              "presets_a": [{"name": "A1"}, {"name": "A2", "alpha": 0.03}],
              "presets_b": [{"name": "B1"}]})
    assert pl.resolve_presets("a")[0] == [("A1", None), ("A2", 0.03)]
    assert pl.resolve_presets("B")[0] == [("B1", None)]
    # 未指定 profile：优先 presets（兼容旧配置）
    assert pl.resolve_presets()[0] == [("默认", None)]


def test_default_profile_wins_without_explicit_profile(cfg_file):
    cfg_file({"presets": [{"name": "默认"}],
              "presets_a": [{"name": "A1"}],
              "default_preset_profile": "a"})
    assert pl.resolve_presets()[0] == [("A1", None)]


def test_default_profile_accepts_full_key(cfg_file):
    """用户配置写完整键名 presets_a 也能选中（文档推荐写法）。"""
    cfg_file({"presets": [{"name": "默认"}],
              "presets_a": [{"name": "A1"}],
              "default_preset_profile": "presets_a"})
    assert pl.resolve_presets()[0] == [("A1", None)]


def test_profile_name_accepts_full_key(cfg_file):
    cfg_file({"presets_a": [{"name": "A1"}]})
    assert pl.resolve_presets("presets_a")[0] == [("A1", None)]
    assert pl.resolve_presets("Presets_A")[0] == [("A1", None)]


def test_parse_preset_profiles_multi():
    import export_config as pc
    assert pc.parse_preset_profiles(None) == []
    assert pc.parse_preset_profiles("") == []
    assert pc.parse_preset_profiles("a") == ["a"]
    assert pc.parse_preset_profiles("a,b") == ["a", "b"]
    assert pc.parse_preset_profiles("presets_a，presets_b") == ["a", "b"]
    assert pc.parse_preset_profiles(["presets_a", "b", "a"]) == ["a", "b"]
    assert pc.parse_preset_profiles(" presets_b , a ") == ["b", "a"]
    # 3+ 版本：不得截断（上限只作用于旧任务反推，不作用于显式声明）
    assert pc.parse_preset_profiles("a,b,c,d") == ["a", "b", "c", "d"]
    assert pc.parse_preset_profiles(["presets_a", "presets_b", "presets_c"]) == ["a", "b", "c"]


def test_watcher_export_targets_and_missing(tmp_path, monkeypatch):
    """watcher 多账号导出目标：只包含已生成的克隆；缺的单独报出来（用于挡住漏发）。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    (tmp_path / "A_串流").mkdir()
    (tmp_path / "A_串流_b").mkdir()
    assert cw._missing_account_drafts("A_串流", ["a", "b", "c"]) == ["A_串流_c"]
    assert cw._export_targets("A_串流", ["a", "b", "c"]) == ["A_串流", "A_串流_b"]
    assert cw._export_targets("A_串流", ["a"]) == ["A_串流"]


@pytest.mark.parametrize("stage,video,pending,expect_ok,reason_kw", [
    ("video_ready", {"status": "done"}, ["A_串流_b"], True, None),
    ("video_ready", {"status": "done"}, [], False, "无待补版本"),
    ("video_ready", {"status": "exported"}, [], False, "无待补版本"),
    ("video_ready", {"status": "done", "export_state": "cleaned"},
     ["A_串流_b"], False, "cleaned"),
    ("archived", {"status": "done"}, ["A_串流_b"], False, "archived"),
])
def test_check_trigger_video_ready_needs_pending_version(
        tmp_path, monkeypatch, stage, video, pending, expect_ok, reason_kw):
    """video_ready 只在有 pending 版本时放行；archived/cleaned 一律挡住。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    jd = tmp_path / "cover_job"
    (jd / "03_prompts").mkdir(parents=True)
    (jd / "03_prompts" / "prompts.json").write_text("{}", encoding="utf-8")
    (jd / "job.json").write_text("{}", encoding="utf-8")
    job = {"stage": stage, "titles": {"gate": "done"}, "video": video}
    monkeypatch.setattr(cw.jm, "detect_cover_files",
                        lambda d, apply=False: ({1, 2, 3, "vertical"}, {}))
    monkeypatch.setattr(cw.jm, "account_cover_errors", lambda d: [])
    monkeypatch.setattr(cw.jm, "load_job", lambda p: job)
    monkeypatch.setattr(cw, "_pending_account_outputs", lambda j, d: pending)
    ok, reasons = cw.check_trigger(str(jd))
    assert ok is expect_ok, reasons
    if reason_kw:
        assert any(reason_kw in r for r in reasons), reasons


def test_unknown_profile_raises(cfg_file):
    cfg_file({"presets": [{"name": "默认"}]})
    with pytest.raises(RuntimeError):
        pl.resolve_presets("nope")


def test_empty_profile_is_valid(cfg_file):
    cfg_file({"presets": [{"name": "默认"}], "presets_b": []})
    assert pl.resolve_presets("b")[0] == []


def _draft_with_preset_tracks():
    # 兜底 GC 只认 UUID 形态的引用，测试数据也用 UUID
    m_video = "11111111-1111-1111-1111-111111111111"
    m_preset = "22222222-2222-2222-2222-222222222222"
    m_speed = "33333333-3333-3333-3333-333333333333"
    m_draft = "44444444-4444-4444-4444-444444444444"
    m_orphan = "55555555-5555-5555-5555-555555555555"
    return {
        "tracks": [
            {"id": "t-main", "type": "video",
             "segments": [{"material_id": m_video, "extra_material_refs": [m_speed]}]},
            {"id": "t-preset", "type": "video",
             "segments": [{"material_id": m_preset, "extra_material_refs": [m_draft]}]},
        ],
        "materials": {
            "videos": [
                {"id": m_video, "type": "video", "path": "C:/a.mp4",
                 "material_name": "第1集.mp4"},
                {"id": m_preset, "type": "video", "path": "",
                 "material_name": "我的预设1"},
            ],
            "speeds": [{"id": m_speed}],
            "drafts": [{"id": m_draft}],
            "canvases": [{"id": m_orphan}],
        },
    }, {"video": m_video, "preset": m_preset, "speed": m_speed,
        "draft": m_draft, "orphan": m_orphan}


def test_strip_with_manifest_removes_exact_ids():
    draft, mid = _draft_with_preset_tracks()
    manifest = {"tracks": ["t-preset"],
                "materials": {"videos": [mid["preset"]], "drafts": [mid["draft"]]}}
    stat = preset_ops.strip_preset_layers(draft, manifest)
    assert stat == {"tracks": 1, "materials": 2, "legacy": False}
    assert [t["id"] for t in draft["tracks"]] == ["t-main"]
    assert [m["id"] for m in draft["materials"]["videos"]] == [mid["video"]]
    assert draft["materials"]["drafts"] == []
    # 清单模式下不做兜底 GC，未列出的素材保持原样
    assert [m["id"] for m in draft["materials"]["canvases"]] == [mid["orphan"]]


def test_strip_legacy_detects_preset_track_and_gc_materials():
    draft, mid = _draft_with_preset_tracks()
    stat = preset_ops.strip_preset_layers(draft, None)
    assert stat["legacy"] is True
    assert stat["tracks"] == 1
    assert [t["id"] for t in draft["tracks"]] == ["t-main"]
    assert [m["id"] for m in draft["materials"]["videos"]] == [mid["video"]]
    assert [m["id"] for m in draft["materials"]["speeds"]] == [mid["speed"]]
    # 预设轨引用过的组合片段与孤立素材都被回收
    assert draft["materials"]["drafts"] == []
    assert draft["materials"]["canvases"] == []


def test_strip_falls_back_when_manifest_is_stale():
    """草稿在剪映里被编辑后轨道 ID 变了：清单失效也要识别并剥掉旧预设。"""
    draft, _mid = _draft_with_preset_tracks()
    stale_manifest = {"tracks": ["t-deleted"], "materials": {"videos": ["gone"]}}
    stat = preset_ops.strip_preset_layers(draft, stale_manifest)
    assert stat["legacy"] is True
    assert stat["tracks"] == 1
    assert [t["id"] for t in draft["tracks"]] == ["t-main"]


def test_manifest_roundtrip(tmp_path):
    pl.write_preset_manifest(str(tmp_path), {"tracks": ["x"], "materials": {}})
    assert pl.read_preset_manifest(str(tmp_path))["tracks"] == ["x"]
    assert pl.read_preset_manifest(str(tmp_path / "missing")) is None


def test_clone_refuses_missing_media(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    draft, _ = _draft_with_preset_tracks()
    draft["materials"]["videos"][0]["path"] = str(tmp_path / "gone.mp4")
    (src / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")
    (src / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError) as ei:
        preset_ops.clone_draft_with_presets(str(src), target_name="B", presets=[])
    assert "素材缺失" in str(ei.value)


def test_clone_refuses_preset_on_main_track(tmp_path, monkeypatch):
    """预设素材被压进主轨（非独立预设轨）时剥离不到，必须中止避免新旧预设叠加。"""
    src = tmp_path / "src"
    src.mkdir()
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    m_video = "77777777-7777-7777-7777-777777777777"
    m_preset = "66666666-6666-6666-6666-666666666666"
    draft = {
        "tracks": [{"id": "t-main", "type": "video",
                    "segments": [{"material_id": m_video}, {"material_id": m_preset}]}],
        "materials": {"videos": [
            {"id": m_video, "type": "video", "path": str(media), "material_name": "第1集.mp4"},
            {"id": m_preset, "type": "video", "path": "", "material_name": "我的预设X"}]},
    }
    (src / "draft_content.json").write_text(json.dumps(draft), encoding="utf-8")
    (src / "draft_meta_info.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(preset_ops, "DRAFT_ROOT", str(tmp_path / "drafts"))
    monkeypatch.setattr(preset_ops, "_STAGING_ROOT", str(tmp_path / "stage"))
    with pytest.raises(RuntimeError) as ei:
        preset_ops.clone_draft_with_presets(str(src), target_name="B", presets=[])
    assert "未剥离到预设轨" in str(ei.value)


def test_export_stale_marker_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "DRAFT_ROOT", str(tmp_path))
    d = tmp_path / "D1"
    d.mkdir()
    assert not pl.export_is_stale(str(d))
    pl.mark_export_stale(str(d), "publish")
    assert pl.export_is_stale(str(d))
    pl.clear_export_stale(str(d))
    assert not pl.export_is_stale(str(d))


def test_clone_draft_replaces_preset_layer_without_touching_source(tmp_path, monkeypatch):
    src = tmp_path / "src_draft"
    src.mkdir()
    draft, mid = _draft_with_preset_tracks()
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")  # 克隆前会校验引用素材存在
    draft["materials"]["videos"][0]["path"] = str(media)
    draft.update({"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                  "name": "A", "duration": 10_000_000})
    (src / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    (src / "draft_meta_info.json").write_text(
        json.dumps({"draft_id": draft["id"], "draft_name": "A"}, ensure_ascii=False),
        encoding="utf-8")
    pl.write_preset_manifest(str(src), {
        "tracks": ["t-preset"],
        "materials": {"videos": [mid["preset"]], "drafts": [mid["draft"]]},
    })

    monkeypatch.setattr(preset_ops, "DRAFT_ROOT", str(tmp_path / "drafts"))
    monkeypatch.setattr(preset_ops, "_STAGING_ROOT", str(tmp_path / "stage"))
    monkeypatch.setattr(preset_ops, "get_main_video_duration", lambda _d: 10_000_000)
    monkeypatch.setattr(pl, "DRAFT_ROOT", str(tmp_path / "drafts"))

    r = preset_ops.clone_draft_with_presets(str(src), target_name="A_b", presets=[])

    out, _ = pl.load_draft(r["content_path"])
    assert [t["id"] for t in out["tracks"]] == ["t-main"]
    assert out["id"] != draft["id"]
    assert out["name"] == "A_b"
    # 源草稿保持原样
    src_after, _ = pl.load_draft(src / "draft_content.json")
    assert len(src_after["tracks"]) == 2
    # 备份同步为 B 的内容，导出时不会被 A 的预设轨恢复
    backup, _ = pl.load_draft(tmp_path / "drafts" / "A_b" / "_pipeline_backup.json")
    assert [t["id"] for t in backup["tracks"]] == ["t-main"]


# ── 多账号封面 ──────────────────────────────────────────────

def _png(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def _cover_names():
    return ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical")


def test_split_account_cover_stem():
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import job_manifest as jm
    assert jm.split_account_cover_stem("cover_01_hit_b") == ("cover_01_hit", "b")
    assert jm.split_account_cover_stem("Cover_04_Vertical-A") == ("cover_04_vertical", "a")
    assert jm.split_account_cover_stem("cover_01_hit") == ("cover_01_hit", None)
    assert jm.split_account_cover_stem("随便一张") == ("随便一张", None)


def test_detect_cover_files_ignores_account_variants(tmp_path):
    """8 张图（基础 4 + 账号 4）：账号图不参与基础识别，也绝不被改名覆盖。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import job_manifest as jm
    covers = tmp_path / "04_covers"
    for n in _cover_names():
        _png(covers / f"{n}.png")
    for n in _cover_names():
        _png(covers / f"{n}_b.png")

    found, renames = jm.detect_cover_files(str(tmp_path), apply=False)
    assert set(found) == {1, 2, 3, "vertical"}
    assert all("_b" not in os.path.basename(p) for p in found.values())
    assert renames == []
    assert set(jm.detect_account_cover_sets(str(tmp_path))["b"]) == {1, 2, 3, "vertical"}
    assert jm.account_cover_errors(str(tmp_path)) == []


def test_account_cover_group_must_be_complete(tmp_path):
    """账号封面只给了一半 → 明确报缺哪几张（门禁会拦）。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import job_manifest as jm
    covers = tmp_path / "04_covers"
    for n in _cover_names():
        _png(covers / f"{n}.png")
    for n in ("cover_01_hit", "cover_02_contrast"):
        _png(covers / "b" / f"{n}.png")
    errs = jm.account_cover_errors(str(tmp_path))
    assert len(errs) == 1 and "账号 b" in errs[0] and "vertical" in errs[0]


def test_account_cover_group_dir_with_suffix(tmp_path):
    """线上实际命名：04_covers/b_group/cover_01_hit_B.png（目录名 b_group + 文件名 _B）。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import job_manifest as jm
    covers = tmp_path / "04_covers"
    for n in _cover_names():
        _png(covers / f"{n}.png")
    for n in _cover_names():
        _png(covers / "b_group" / f"{n}_B.png")
    sets = jm.detect_account_cover_sets(str(tmp_path))
    assert set(sets) == {"b"}
    assert set(sets["b"]) == {1, 2, 3, "vertical"}
    assert jm.account_cover_errors(str(tmp_path)) == []


def test_resolve_profile_cover_group_first_and_no_silent_fallback(tmp_path):
    """版本封面解析：账号组优先；不存在的组必须返回 None，绝不回落到 A。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import job_manifest as jm
    covers = tmp_path / "04_covers"
    for n in _cover_names():
        _png(covers / f"{n}.png")
    _png(covers / "b_group" / "cover_04_vertical_B.png")
    (tmp_path / "job.json").write_text(json.dumps(
        {"vertical_cover": {"file": "04_covers/cover_04_vertical.png"}}), encoding="utf-8")

    assert os.path.basename(jm.resolve_profile_cover(str(tmp_path), "a")) == "cover_04_vertical.png"
    assert jm.resolve_profile_cover(str(tmp_path), "presets_b").endswith("cover_04_vertical_B.png")
    assert jm.resolve_profile_cover(str(tmp_path), "c") is None


def test_clone_applies_profile_cover(tmp_path, monkeypatch):
    """克隆换预设必须同时换封面：draft_cover 与 cover composition 都指向 B 组竖版图。"""
    src = tmp_path / "src_draft"
    src.mkdir()
    draft, mid = _draft_with_preset_tracks()
    media = tmp_path / "a.mp4"
    media.write_bytes(b"x")
    draft["materials"]["videos"][0]["path"] = str(media)
    draft.update({"id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                  "name": "A", "duration": 10_000_000})
    (src / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    (src / "draft_meta_info.json").write_text(
        json.dumps({"draft_id": draft["id"], "draft_name": "A"}, ensure_ascii=False),
        encoding="utf-8")
    pl.write_preset_manifest(str(src), {
        "tracks": ["t-preset"],
        "materials": {"videos": [mid["preset"]], "drafts": [mid["draft"]]},
    })

    covers = tmp_path / "cover_job" / "04_covers"
    (covers / "b_group").mkdir(parents=True)
    cover_b = covers / "b_group" / "cover_04_vertical_B.jpg"
    cover_b.write_bytes(b"\xff\xd8\xff\xd9")
    (covers / "cover_04_vertical.jpg").write_bytes(b"\xff\xd8\xff\xda")

    monkeypatch.setattr(preset_ops, "DRAFT_ROOT", str(tmp_path / "drafts"))
    monkeypatch.setattr(preset_ops, "_STAGING_ROOT", str(tmp_path / "stage"))
    monkeypatch.setattr(preset_ops, "get_main_video_duration", lambda _d: 10_000_000)
    monkeypatch.setattr(pl, "DRAFT_ROOT", str(tmp_path / "drafts"))

    r = preset_ops.clone_draft_with_presets(
        str(src), target_name="A_b", presets=[], preset_profile="b",
        job_dir=str(tmp_path / "cover_job"))

    assert r["cover"] == str(cover_b)
    out_dir = tmp_path / "drafts" / "A_b"
    assert (out_dir / "draft_cover.jpg").read_bytes() == cover_b.read_bytes()
    out, _ = pl.load_draft(r["content_path"])
    ids = {d["id"] for d in out["materials"]["drafts"]}
    assert out["cover"]["cover_draft_id"] in ids


def test_validate_job_allows_multi_group_prompts(tmp_path):
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import job_manifest as jm
    items = [{"slot": s, "group": g}
             for g in ("A", "B") for s in (1, 2, 3, "vertical")]
    job = {"prompts": {"count": 8, "items": items}}
    errs = [e for e in jm.validate_job(job) if "prompts" in e]
    assert errs == []
    # 少一个槽位仍然报错
    job["prompts"] = {"count": 8, "items": items[:-1]}
    errs = [e for e in jm.validate_job(job) if "prompts" in e]
    assert any("不一致" in e for e in errs)
    assert any("组 B 缺槽位" in e for e in errs)


def test_profiles_from_job_outputs_requires_three_conditions_and_caps_two(tmp_path, monkeypatch):
    """反推必须三条件齐备（prompts 组 + 4 张封面 + presets_<组>），且默认上限 2 组。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_available_preset_profiles",
                        lambda: ["a", "b", "c", "d"])
    jd = tmp_path / "cover_job"
    (jd / "03_prompts").mkdir(parents=True)
    (jd / "03_prompts" / "prompts.json").write_text(
        json.dumps({"items": [{"slot": s, "group": g}
                              for g in "ABCD" for s in (1, 2, 3, "vertical")]}),
        encoding="utf-8")
    for g in "abcd":
        for i in range(4):
            _png(jd / "04_covers" / f"{g}_group" / f"c{i}.png")
    assert cw._profiles_from_job_outputs(str(jd)) == ["a", "b"]  # C/D 不自动扩

    # 只剩 A/B 时，B 少一张封面 → 该组第②条不满足，不足 2 组
    import shutil
    shutil.rmtree(jd / "04_covers" / "c_group")
    shutil.rmtree(jd / "04_covers" / "d_group")
    (jd / "04_covers" / "b_group" / "c0.png").unlink()
    assert cw._profiles_from_job_outputs(str(jd)) == []

    # presets.json 里没有对应组 → 第③条不满足
    (jd / "04_covers" / "b_group" / "c0.png").write_bytes(b"x")
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: [])
    assert cw._profiles_from_job_outputs(str(jd)) == []


def test_pending_account_outputs_reads_job_profiles_only(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    (tmp_path / "剧_串流").mkdir()
    job_dir = str(tmp_path / "剧" / "cover_job")
    assert cw._pending_account_outputs({"workflow": {}}, job_dir) == []
    assert cw._pending_account_outputs(
        {"workflow": {"preset_profile": "a,b"}}, job_dir) == ["剧_串流_b"]


def test_existing_clone_profiles_detects_clone_drafts(tmp_path, monkeypatch):
    """草稿目录里的 `<基础>_<版本>` 克隆就是待补版本；非目录/非法版本名忽略。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    (tmp_path / "剧_串流").mkdir()
    (tmp_path / "剧_串流_b").mkdir()
    (tmp_path / "剧_串流_c1").mkdir()
    (tmp_path / "剧_串流_BAD").mkdir()
    (tmp_path / "剧_串流_x.tmp").write_text("x", encoding="utf-8")
    (tmp_path / "剧_串流_b2_backup").mkdir()
    assert cw._existing_clone_profiles("剧_串流") == ["b", "c1"]


def test_pending_account_outputs_uses_existing_clone_draft(tmp_path, monkeypatch):
    """job 没声明多版本但 `_b` 草稿已建好：必须算 pending，否则补不了导出。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: [])
    (tmp_path / "剧_串流").mkdir()
    job_dir = str(tmp_path / "剧" / "cover_job")
    # 没有克隆草稿：单账号旧任务，不能被判成 pending（否则会触发自动克隆）
    assert cw._pending_account_outputs({"workflow": {}}, job_dir) == []
    (tmp_path / "剧_串流_b").mkdir()
    monkeypatch.setattr(cw, "_draft_exported", lambda n: False)
    assert cw._pending_account_outputs({"workflow": {}}, job_dir) == ["剧_串流_b"]
    # 克隆已导出：不再是待补版本
    monkeypatch.setattr(cw, "_draft_exported", lambda n: True)
    assert cw._pending_account_outputs({"workflow": {}}, job_dir) == []


def test_check_trigger_passes_with_existing_clone_draft(tmp_path, monkeypatch):
    """端到端：video_ready + 已导出 + 存在未导出的 `_b` 克隆草稿 → 放行。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: [])
    monkeypatch.setattr(cw, "_draft_exported", lambda n: False)
    (tmp_path / "剧_串流").mkdir()
    (tmp_path / "剧_串流_b").mkdir()
    jd = tmp_path / "剧" / "cover_job"
    (jd / "03_prompts").mkdir(parents=True)
    (jd / "03_prompts" / "prompts.json").write_text("{}", encoding="utf-8")
    (jd / "job.json").write_text("{}", encoding="utf-8")
    job = {"stage": "video_ready", "titles": {"gate": "done"},
           "video": {"status": "done", "export_state": "exported"}}
    monkeypatch.setattr(cw.jm, "detect_cover_files",
                        lambda d, apply=False: ({1, 2, 3, "vertical"}, {}))
    monkeypatch.setattr(cw.jm, "account_cover_errors", lambda d: [])
    monkeypatch.setattr(cw.jm, "load_job", lambda p: job)
    ok, reasons = cw.check_trigger(str(jd))
    assert ok is True, reasons


def test_build_plan_exports_existing_clone_when_base_done(tmp_path, monkeypatch):
    """八零猎王场景：A 已导出、`_b` 已建但未导 → 计划只补导 `_b`。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: [])
    monkeypatch.setattr(cw, "_effective_cut", lambda *a, **k: None)
    monkeypatch.setattr(cw, "_migrate_ctx", lambda job: {})
    monkeypatch.setattr(cw, "_progress_info", lambda p: (0.0, None, None))
    monkeypatch.setattr(cw, "retry_gate", lambda *a, **k: None)
    monkeypatch.setattr(cw, "_draft_exported", lambda n: n == "剧_串流")
    (tmp_path / "剧_串流").mkdir()
    (tmp_path / "剧_串流_b").mkdir()
    jd = tmp_path / "剧" / "cover_job"
    jd.mkdir(parents=True)
    job = {"stage": "video_ready", "titles": {"gate": "done"},
           "video": {"status": "done", "export_state": "exported"}}
    plan = cw.build_plan(str(jd), job, {"export_policy": "immediate"}, pending_edit=0,
                         notify=False, write_log=False)
    assert plan["action"] == "export", plan
    assert plan["profiles"] == ["a", "b"]
    assert plan["export_targets"] == ["剧_串流_b"]
