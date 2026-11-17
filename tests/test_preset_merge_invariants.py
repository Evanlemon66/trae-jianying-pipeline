# -*- coding: utf-8 -*-
"""预设合并时长自洽 + 落盘前不变量断言 的回归测试。

覆盖三处修复：
  ① 文本型组合预设（logo_b/logo/logo文字/复合片段预设1 同一分支）拉伸后，
     内层草稿 duration 与包装视频材质 duration 同步涨到整帧；
  ② _process_flat_track 的 source 夹紧到素材真实时长（并打告警）；
  ③ save_draft / publish_staged_draft 的落盘前不变量断言（超标拒绝落盘）。
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))

import pipeline as pl  # noqa: E402

MAIN_US = 8511470759          # 真实事故里的主轨时长（255345 帧 @30fps + 余数）
FRAME_ALIGNED_US = 8511500000  # 向上取整到整帧后的 8511.5s


def _seg(material_id, dur, src=None, extra=None):
    return {
        "id": "seg-" + material_id,
        "material_id": material_id,
        "extra_material_refs": list(extra or []),
        "target_timerange": {"start": 0, "duration": dur},
        "source_timerange": ({"start": 0, "duration": src} if src is not None else None),
    }


def _text_preset(inner_dur):
    """构造与剪映「复合片段」预设同构的最小预设：内层草稿 + 包装视频材质 + 顶层轨。"""
    comb_id, wrapper_id = "COMB-1", "WRAP-1"
    inner = {
        "id": comb_id,
        "type": "combination",
        "draft": {
            "duration": inner_dur,
            "fps": 30.0,
            "materials": {},
            "tracks": [
                {"id": "inner-text", "type": "text",
                 "segments": [_seg("TEXT-1", inner_dur)]},
                {"id": "inner-video", "type": "video", "segments": []},
            ],
        },
    }
    return {
        "duration": 0,
        "fps": 30.0,
        "materials": {
            "drafts": [inner],
            "videos": [{"id": wrapper_id, "duration": inner_dur,
                        "extra_type_option": 2, "material_name": "复合片段预设2"}],
        },
        "tracks": [{"id": "top-video", "type": "video",
                    "segments": [_seg(wrapper_id, inner_dur, src=inner_dur,
                                      extra=[comb_id])]}],
    }


def _merge(tmp_path, preset, stretch=MAIN_US):
    p = tmp_path / "preset.json"
    p.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")
    draft = {"id": "draft", "fps": 30.0, "materials": {}, "tracks": []}
    pl.merge_preset_into_draft(draft, str(p), stretch_to_duration_us=stretch)
    return draft


def test_text_preset_syncs_inner_and_wrapper_duration(tmp_path):
    """logo_b 场景：52.3s 内层被拉到 8.5s 级时间线后，两处 duration 都要涨。"""
    draft = _merge(tmp_path, _text_preset(52300000))
    inner = draft["materials"]["drafts"][0]["draft"]
    assert inner["duration"] == FRAME_ALIGNED_US
    wrapper = draft["materials"]["videos"][0]
    assert wrapper["duration"] == FRAME_ALIGNED_US

    top_seg = draft["tracks"][-1]["segments"][0]
    assert top_seg["source_timerange"]["duration"] <= inner["duration"]
    # 合并结果必须能通过落盘断言
    pl.assert_draft_invariants(draft)


def test_text_preset_does_not_shrink_long_inner(tmp_path):
    """logo文字 场景：内层 12101.467s 已长于时间线，保持原值。"""
    draft = _merge(tmp_path, _text_preset(12101466666))
    assert draft["materials"]["drafts"][0]["draft"]["duration"] == 12101466666
    assert draft["materials"]["videos"][0]["duration"] == 12101466666
    pl.assert_draft_invariants(draft)


def test_flat_track_clamps_source_to_real_material_duration(capsys):
    track = {"id": "t", "type": "video", "segments": [
        _seg("V1", 5_000_000, src=5_000_000)]}
    out = pl._process_flat_track(track, {}, 0, 10_000_000, None,
                                 material_durations={"V1": 3_000_000})
    seg = out["segments"][0]
    assert seg["target_timerange"]["duration"] == 10_000_000
    assert seg["source_timerange"]["duration"] == 3_000_000
    assert "夹紧" in capsys.readouterr().out


def test_flat_track_keeps_source_when_within_material(capsys):
    track = {"id": "t", "type": "video", "segments": [
        _seg("V1", 5_000_000, src=5_000_000)]}
    out = pl._process_flat_track(track, {}, 0, 10_000_000, None,
                                 material_durations={"V1": 20_000_000})
    assert out["segments"][0]["source_timerange"]["duration"] == 10_000_000
    assert "夹紧" not in capsys.readouterr().out


def _video_draft(src_dur, mat_dur):
    return {
        "fps": 30.0,
        "materials": {"videos": [{"id": "V1", "duration": mat_dur}]},
        "tracks": [{"type": "video", "segments": [_seg("V1", src_dur, src=src_dur)]}],
    }


def test_save_draft_rejects_source_longer_than_material(tmp_path):
    target = tmp_path / "draft_content.json"
    with pytest.raises(pl.DraftInvariantError) as ei:
        pl.save_draft(_video_draft(8_000_000, 5_000_000), str(target))
    assert "拒绝落盘" in str(ei.value)
    assert not target.exists(), "断言失败时不允许写出文件"


def test_save_draft_rejects_segment_longer_than_inner_draft(tmp_path):
    draft = {
        "fps": 30.0,
        "materials": {
            "drafts": [{"id": "COMB", "draft": {
                "duration": 52_300_000, "fps": 30.0, "materials": {}, "tracks": []}}],
            # 包装 material.duration 足够大，确保失败点是内层草稿时长
            "videos": [{"id": "WRAP", "duration": 9_000_000_000}],
        },
        "tracks": [{"type": "video", "segments": [
            _seg("WRAP", 8_511_470_759, src=8_511_470_759, extra=["COMB"])]}],
    }
    with pytest.raises(pl.DraftInvariantError) as ei:
        pl.save_draft(draft, str(tmp_path / "draft_content.json"))
    assert "内层草稿" in str(ei.value)


def test_save_draft_recurses_into_nested_draft(tmp_path):
    inner = {
        "duration": 52_300_000, "fps": 30.0,
        "materials": {"videos": [{"id": "IV", "duration": 2_000_000}]},
        "tracks": [{"type": "video", "segments": [_seg("IV", 3_000_000, src=3_000_000)]}],
    }
    draft = {
        "fps": 30.0,
        "materials": {
            "drafts": [{"id": "COMB", "draft": inner}],
            "videos": [{"id": "WRAP", "duration": 52_300_000}],
        },
        "tracks": [{"type": "video", "segments": [
            _seg("WRAP", 5_000_000, src=5_000_000, extra=["COMB"])]}],
    }
    with pytest.raises(pl.DraftInvariantError) as ei:
        pl.save_draft(draft, str(tmp_path / "draft_content.json"))
    assert "materials.drafts[0]" in str(ei.value)


def test_save_draft_writes_when_valid(tmp_path):
    target = tmp_path / "draft_content.json"
    pl.save_draft(_video_draft(4_000_000, 5_000_000), str(target))
    assert json.loads(target.read_text(encoding="utf-8"))["tracks"]


def test_publish_rejects_bad_stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "DRAFT_ROOT", str(tmp_path / "drafts"))
    monkeypatch.setattr(pl, "_cleanup_removed_leftovers", lambda *a, **k: None)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "draft_content.json").write_text(
        json.dumps(_video_draft(8_000_000, 5_000_000), ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(pl.DraftInvariantError):
        pl.publish_staged_draft(str(stage), "bad_draft")
    assert not (tmp_path / "drafts" / "bad_draft").exists()


# ━━━━━━━━━━━━━━━━ 按帧比较 ━━━━━━━━━━━━━━━━

def _composite_draft(inner_us, wrap_us, seg_us):
    return {
        "fps": 30.0,
        "materials": {
            "drafts": [{"id": "COMB", "draft": {
                "duration": inner_us, "fps": 30.0, "materials": {}, "tracks": []}}],
            "videos": [{"id": "WRAP", "duration": wrap_us}],
        },
        "tracks": [{"type": "video", "segments": [
            _seg("WRAP", seg_us, src=seg_us, extra=["COMB"])]}],
    }


def test_invariant_ignores_1us_rounding(tmp_path):
    """复合片段预设1 的源文件：inner=161633333 / target=161633334，1µs 不算非法。"""
    draft = _composite_draft(161_633_333, 161_633_334, 161_633_334)
    pl.assert_draft_invariants(draft)
    pl.save_draft(draft, str(tmp_path / "draft_content.json"))


def test_invariant_rejects_two_frame_overshoot(tmp_path):
    draft = _composite_draft(1_000_000, 1_033_334, 1_033_334)
    with pytest.raises(pl.DraftInvariantError) as ei:
        pl.save_draft(draft, str(tmp_path / "draft_content.json"))
    assert "帧" in str(ei.value)


def test_frame_helpers_match_255345_frames():
    assert pl.draft_io.frame_count(8_511_470_759, 30) == 255_345
    assert pl.draft_io.ceil_to_frame_us(8_511_470_759, 30) == 8_511_500_000


# ━━━━━━━━━━━━━━━━ 唯一写盘原语 ━━━━━━━━━━━━━━━━

def test_write_draft_content_backs_up_and_replaces_atomically(tmp_path):
    target = tmp_path / "draft_content.json"
    target.write_text(json.dumps(_video_draft(4_000_000, 5_000_000)), encoding="utf-8")
    old_text = target.read_text(encoding="utf-8")
    draft = _video_draft(3_000_000, 5_000_000)
    bak = pl.draft_io.write_draft_content(
        draft, str(target), backup_suffix=".bak_{ts}")
    assert bak and os.path.isfile(bak)
    assert open(bak, encoding="utf-8").read() == old_text
    assert json.loads(target.read_text(encoding="utf-8"))["tracks"][0]["segments"][0][
        "source_timerange"]["duration"] == 3_000_000
    assert not os.path.exists(str(target) + ".tmp")


def test_write_draft_content_rejects_without_touching_file(tmp_path):
    target = tmp_path / "draft_content.json"
    target.write_text(json.dumps(_video_draft(4_000_000, 5_000_000)), encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    with pytest.raises(pl.DraftInvariantError):
        pl.draft_io.write_draft_content(
            _video_draft(8_000_000, 5_000_000), str(target),
            backup_suffix=".bak_{ts}")
    assert target.read_text(encoding="utf-8") == before
    assert not [p for p in os.listdir(tmp_path) if ".bak_" in p]
    assert not os.path.exists(str(target) + ".tmp")


def test_write_draft_content_keeps_existing_backup(tmp_path):
    target = tmp_path / "draft_content.json"
    target.write_text(json.dumps(_video_draft(4_000_000, 5_000_000)), encoding="utf-8")
    bak = str(target) + ".pre_framefix.json"
    with open(bak, "w", encoding="utf-8") as f:
        f.write("SENTINEL")
    pl.draft_io.write_draft_content(
        _video_draft(3_000_000, 5_000_000), str(target),
        backup_suffix=".pre_framefix.json", keep_existing_backup=True)
    assert open(bak, encoding="utf-8").read() == "SENTINEL"


def test_write_draft_content_records_freshness(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pl.draft_io, "_record_freshness",
                        lambda job_dir, path, writer: calls.append((job_dir, path, writer)))
    target = tmp_path / "draft_content.json"
    pl.draft_io.write_draft_content(
        _video_draft(3_000_000, 5_000_000), str(target),
        job_dir="JOB", writer="unit_test", indent=2)
    assert calls == [("JOB", str(target), "unit_test")]


def test_save_draft_is_the_primitive(tmp_path):
    """pipeline.save_draft 必须等同于原语（同一实现、indent=2）。"""
    target = tmp_path / "draft_content.json"
    pl.save_draft(_video_draft(3_000_000, 5_000_000), str(target))
    raw = target.read_text(encoding="utf-8")
    assert "\n  \"tracks\"" in raw  # indent=2


# ━━━━━━━━━━━━━━━━ merge_drafts 拒写清理 ━━━━━━━━━━━━━━━━

def test_merge_drafts_rejects_and_removes_staging(tmp_path, monkeypatch):
    """不变量拒写时：暂存目录不残留、源草稿一个字节都不动。"""
    import merge_drafts as md

    src = tmp_path / "src_ep01"
    src.mkdir()
    src_json = src / "draft_content.json"
    bad = _video_draft(2_000_000, 1_000_000)   # source 2s > 素材 1s，能过 _validate
    bad.update({"id": "SRC", "name": "src"})
    src_json.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    (src / "draft_meta_info.json").write_text(
        json.dumps({"draft_id": "SRC", "draft_name": "src"}), encoding="utf-8")
    before = src_json.read_text(encoding="utf-8")

    root, stage = tmp_path / "drafts", tmp_path / "__jybuild__"
    root.mkdir()
    monkeypatch.setattr(md, "_DRAFT_ROOT", str(root))
    monkeypatch.setattr(md, "_STAGING_ROOT", str(stage))

    with pytest.raises(SystemExit) as ei:
        md.merge([str(src)], "merged_out", force=True)
    assert ei.value.code == 1
    assert not stage.exists() or not list(stage.iterdir()), "拒写后暂存目录必须清空"
    assert not (root / "merged_out").exists()
    assert src_json.read_text(encoding="utf-8") == before


# ━━━━━━━━━━━━━━━━ 预设源文件校验 ━━━━━━━━━━━━━━━━

def test_merge_preset_accepts_1us_rounding(tmp_path):
    """真实预设源文件的 +1µs：按帧比较后 1 帧容差内，必须放行。"""
    preset = _text_preset(161_633_333)
    preset["materials"]["videos"][0]["duration"] = 161_633_334
    preset["tracks"][0]["segments"][0]["target_timerange"]["duration"] = 161_633_334
    p = tmp_path / "复合片段预设1" / "preset_draft" / "draft_content.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")

    draft = {"id": "d", "fps": 30.0, "materials": {}, "tracks": []}
    pl.merge_preset_into_draft(draft, str(p))   # 不抛即通过
    pl.assert_draft_invariants(draft)


def test_merge_preset_rejects_bad_source_and_names_it(tmp_path):
    """越界预设源文件：合并前就拒，并报出预设名、段位置与差帧数。"""
    preset = _text_preset(1_000_000)
    preset["materials"]["videos"][0]["duration"] = 1_100_000
    seg = preset["tracks"][0]["segments"][0]
    seg["target_timerange"]["duration"] = 1_100_000
    seg["source_timerange"]["duration"] = 1_100_000
    p = tmp_path / "bad_preset" / "preset_draft" / "draft_content.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(preset, ensure_ascii=False), encoding="utf-8")

    draft = {"id": "d", "fps": 30.0, "materials": {}, "tracks": []}
    with pytest.raises(pl.DraftInvariantError) as ei:
        pl.merge_preset_into_draft(draft, str(p))
    msg = str(ei.value)
    assert "bad_preset" in msg and "tracks[" in msg and "帧" in msg
    assert draft["tracks"] == [], "拒写时不得改动目标草稿"

