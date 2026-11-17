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


def _write_prompts(job_dir, groups):
    d = job_dir / "03_prompts"
    d.mkdir(parents=True, exist_ok=True)
    items = [{"slot": s, "group": g} for g in groups for s in (1, 2, 3, "vertical")]
    (d / "prompts.json").write_text(json.dumps({"items": items}), encoding="utf-8")


def test_job_profiles_job_first_then_prompts(tmp_path, monkeypatch):
    """版本声明 job 优先、prompts 兜底；缺 presets_<组> 只标 blocked，不整体阻断。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_available_preset_profiles",
                        lambda: ["a", "b", "c"])
    jd = tmp_path / "cover_job"
    assert cw._job_profiles({}, str(jd)) == ["a"]          # 无 job 组 / 无 prompts → 单版本
    _write_prompts(jd, ["A"])
    assert cw._job_profiles({}, str(jd)) == ["a"]          # 仅 A → 单版本
    _write_prompts(jd, ["A", "B"])
    assert cw._job_profiles({}, str(jd)) == ["a", "b"]     # 回落 prompts → 两版
    assert cw._profile_groups({}, str(jd))[1] == "prompts"
    # B 组缺封面不影响版本判定（封面由克隆入口兜底/硬失败）
    assert not (jd / "04_covers" / "b_group").exists()
    # job 显式声明 B（prompts 只有 A）→ job 优先，两版
    job_b = {"titles": {"selected_groups": [{"group": "A"}, {"group": "B"}]}}
    assert cw._job_profiles(job_b, str(jd)) == ["a", "b"]
    assert cw._profile_groups(job_b, str(jd))[1] == "job"
    # job 只声明 A，prompts 有 A+B → job 优先：B 只告警、不参与判定
    job_a = {"titles": {"selected_groups": [{"group": "A"}]}}
    assert cw._job_profiles(job_a, str(jd)) == ["a"]
    groups, source, ignored = cw._profile_groups(job_a, str(jd))
    assert (groups, source, ignored) == (["a"], "job", ["b"])
    _write_prompts(jd, ["A", "B", "C"])
    assert cw._job_profiles({}, str(jd)) == ["a", "b", "c"]
    # 缺 presets_b → 不报错：声明仍是三版，b 标 blocked，A/C 照常可跑
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: ["a", "c"])
    assert cw._job_profiles({}, str(jd)) == ["a", "b", "c"]
    assert cw._blocked_profiles(["a", "b", "c"]) == [
        {"profile": "b", "reason": "missing_presets", "missing": "presets_b"}]
    assert cw._runnable_profiles(["a", "b", "c"]) == ["a", "c"]


def test_pending_account_outputs_follows_prompt_groups(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: ["a", "b"])
    # 本用例只测账号草稿判定，隔离真实导出目录（否则本机同名成品会造成台账冲突）
    monkeypatch.setattr(cw, "_output_conflicts", lambda *a, **k: [])
    (tmp_path / "剧_第01-100集串流").mkdir()
    job_dir = tmp_path / "剧" / "cover_job"
    # 单组 prompts 就是单版本（workflow 不声明组）
    _write_prompts(job_dir, ["A"])
    assert cw._pending_account_outputs({"workflow": {}}, str(job_dir)) == []
    # workflow.preset_profile 显式声明 a,b 时优先于 prompts（multi-account-todo 的
    # 版本优先级：workflow > job > global > 反推），b 版缺成品应算待补
    assert cw._pending_account_outputs(
        {"workflow": {"preset_profile": "a,b"}}, str(job_dir)) == ["剧_第01-100集串流_b"]
    _write_prompts(job_dir, ["A", "B"])
    assert cw._pending_account_outputs({"workflow": {}}, str(job_dir)) == ["剧_第01-100集串流_b"]


def test_check_trigger_passes_with_unexported_clone(tmp_path, monkeypatch):
    """端到端：video_ready + 已导出基础版 + B 组未导出 → 放行补 B。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    monkeypatch.setattr(cw, "_draft_root", lambda: str(tmp_path))
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: ["a", "b"])
    monkeypatch.setattr(cw, "_draft_exported", lambda n: False)
    (tmp_path / "剧_第01-100集串流").mkdir()
    (tmp_path / "剧_第01-100集串流_b").mkdir()
    jd = tmp_path / "剧" / "cover_job"
    _write_prompts(jd, ["A", "B"])
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
    monkeypatch.setattr(cw, "_available_preset_profiles", lambda: ["a", "b"])
    monkeypatch.setattr(cw, "_effective_cut", lambda *a, **k: None)
    monkeypatch.setattr(cw, "_migrate_ctx", lambda job: {})
    monkeypatch.setattr(cw, "_progress_info", lambda p: (0.0, None, None))
    monkeypatch.setattr(cw, "retry_gate", lambda *a, **k: None)
    monkeypatch.setattr(cw, "_draft_exported", lambda n: n == "剧_第01-100集串流")
    # 导出目录隔离：本用例只测"只补导 _b"，不引入真实导出目录的台账冲突
    monkeypatch.setattr(cw, "_output_conflicts", lambda *a, **k: [])
    (tmp_path / "剧_第01-100集串流").mkdir()
    (tmp_path / "剧_第01-100集串流_b").mkdir()
    jd = tmp_path / "剧" / "cover_job"
    jd.mkdir(parents=True)
    _write_prompts(jd, ["A", "B"])
    job = {"stage": "video_ready", "titles": {"gate": "done"},
           "video": {"status": "done", "export_state": "exported"}}
    plan = cw.build_plan(str(jd), job, {"export_policy": "immediate"}, pending_edit=0,
                         notify=False, write_log=False)
    assert plan["action"] == "export", plan
    assert plan["profiles"] == ["a", "b"]
    assert plan["export_targets"] == ["剧_第01-100集串流_b"]


# ── 发布闸门（三件套 / 台账冲突 / edit 闸门） ──────────────────

def _export_root(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    out_dir = tmp_path / "export"
    out_dir.mkdir()
    monkeypatch.setattr(cw.pc, "export_mp4_path",
                        lambda name: str(out_dir / f"{name}.mp4"))
    return cw, out_dir


def test_missing_artifacts_three_piece(tmp_path, monkeypatch):
    """成品三件套：mp4 / -封面.jpg / .progress.json 缺件逐项报出；导出后判成败只认正式 progress。"""
    cw, out_dir = _export_root(tmp_path, monkeypatch)
    # 本用例的断言对象就是「缺件」与「内容不合法」：内容用 b"x"/{} 是故意的
    # （_missing_artifacts 只查存在性），不要套 make_bundle 改成合法三件套。
    assert cw._missing_artifacts("D") == ["mp4", "封面", "progress"]
    (out_dir / "D.mp4").write_bytes(b"x")
    assert cw._missing_artifacts("D") == ["封面", "progress"]
    (out_dir / "D-封面.jpg").write_bytes(b"x")
    (tmp_path / "export.progress.json").write_text("{}", encoding="utf-8")  # 侧写
    assert cw._missing_artifacts("D") == []
    assert cw._missing_artifacts("D", strict_progress=True) == ["progress"]
    (out_dir / "D.mp4.progress.json").write_text("{}", encoding="utf-8")
    assert cw._missing_artifacts("D", strict_progress=True) == []


def test_output_conflicts_both_directions(tmp_path, monkeypatch):
    """台账 exported 但磁盘缺件 / 磁盘齐全但台账没登记，两个方向都要报冲突。"""
    cw, out_dir = _export_root(tmp_path, monkeypatch)
    job = {"video": {"export_state": "exported"}}
    assert [c["kind"] for c in cw._output_conflicts(job, "剧_串流", ["a"])] == [
        "ledger_exported_disk_missing"]
    # 台账⇄磁盘冲突判定只认存在性；这里的 b"x" 是故意的残缺/占位，保持原样
    for suffix in (".mp4", "-封面.jpg", ".mp4.progress.json"):
        (out_dir / f"剧_串流{suffix}").write_bytes(b"x")
    assert cw._output_conflicts(job, "剧_串流", ["a"]) == []
    assert [c["kind"] for c in cw._output_conflicts({"video": {}}, "剧_串流", ["a"])] == [
        "disk_present_ledger_missing"]


def test_edit_gate_blocks_export_until_fixed(tmp_path):
    """edit 失败闸门：无状态文件不拦也不建文件；硬失败后拦；--phase edit 可强制。"""
    sys.path.insert(0, os.path.join(_ROOT, "lib"))
    import cover_watch as cw
    import edit_state as eds
    assert cw._edit_gate(str(tmp_path)) is None
    assert eds.peek(str(tmp_path)) is None
    eds.fail(str(tmp_path), "素材缺失", retryable=False, failure_class="asset_missing")
    assert "剪辑硬失败" in (cw._edit_gate(str(tmp_path)) or "")
    assert cw._edit_gate(str(tmp_path), "edit") is None
    assert cw._edit_gate(str(tmp_path), "export") is not None
