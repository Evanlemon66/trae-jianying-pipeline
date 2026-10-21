import sys, os, json, uuid, shutil, subprocess, time, re, pathlib
from copy import deepcopy
sys.stdout.reconfigure(encoding="utf-8")

DRAFT_ROOT = os.path.join(os.environ["LOCALAPPDATA"], "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
PRESET_ROOT = os.path.join(os.environ["LOCALAPPDATA"], "JianyingPro", "User Data", "Presets", "Combination", "Presets")

SEG_KEYS_WITH_ID = {"material_id", "group_id", "raw_segment_id", "render_index", "id"}
TRACK_KEYS_WITH_ID = {"id"}
MAT_KEYS_WITH_ID = {"id"}


def gen_uuid():
    return str(uuid.uuid4()).upper()


def load_draft(name_or_path):
    if os.path.isdir(name_or_path):
        jp = os.path.join(name_or_path, "draft_content.json")
    else:
        jp = name_or_path
    with open(jp, "r", encoding="utf-8") as f:
        return json.load(f), jp


def save_draft(draft, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)


def backup_draft(path):
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = path + f".bak_{ts}"
    shutil.copy2(path, bak)
    return bak


def _jianying_running():
    try:
        import subprocess as _sp
        out = _sp.check_output(["tasklist", "/fi", "imagename eq JianyingPro.exe"], text=True)
        return "JianyingPro.exe" in out
    except Exception:
        return False


def _draft_json_locked(path):
    try:
        with open(path, "a+b") as f:
            f.write(b"")
        return False
    except PermissionError:
        return True


def find_draft_by_name(name):
    for d in os.listdir(DRAFT_ROOT):
        dp = os.path.join(DRAFT_ROOT, d)
        if os.path.isdir(dp):
            jp = os.path.join(dp, "draft_content.json")
            if os.path.exists(jp):
                if d == name:
                    with open(jp, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    return dp, jp, meta
                try:
                    with open(jp, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("name") == name:
                        return dp, jp, meta
                except Exception:
                    pass
    return None, None, None


def create_empty_draft(draft_name, template_draft_name=None):
    if template_draft_name is None:
        for candidate in ("pipeline_test", "demo_test"):
            dp, jp, meta = find_draft_by_name(candidate)
            if dp:
                template_draft_name = candidate
                break
    if template_draft_name is None:
        os.makedirs(DRAFT_ROOT, exist_ok=True)
        return None, None, None

    dp_root, jp, _ = find_draft_by_name(template_draft_name)
    with open(jp, "r", encoding="utf-8") as f:
        template = json.load(f)

    import copy
    draft = reset_draft_to_clean(copy.deepcopy(template))
    draft["id"] = gen_uuid()
    draft["name"] = draft_name
    draft["create_time"] = int(time.time() * 1_000_000)
    draft["update_time"] = int(time.time() * 1_000_000)

    new_dir = os.path.join(DRAFT_ROOT, draft_name)
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)
    shutil.copytree(dp_root, new_dir)

    jp_new = os.path.join(new_dir, "draft_content.json")
    save_draft(draft, jp_new)

    for bak in os.listdir(new_dir):
        if bak.startswith("draft_content.json.bak") or bak.endswith(".tmp"):
            try:
                os.remove(os.path.join(new_dir, bak))
            except OSError:
                pass

    meta_path = os.path.join(new_dir, "draft_meta_info.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta_info = json.load(f)
            meta_info["draft_id"] = draft["id"]
            meta_info["draft_name"] = draft_name
            meta_info["create_time"] = draft["create_time"]
            meta_info["update_time"] = draft["update_time"]
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return new_dir, jp_new, draft


def find_preset_by_name(name):
    for d in os.listdir(PRESET_ROOT):
        if name in d:
            pp = os.path.join(PRESET_ROOT, d, "preset_draft", "draft_content.json")
            if os.path.exists(pp):
                return pp
    return None


def _rewrite_id_refs(obj, id_map):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in ("material_id", "group_id", "raw_segment_id"):
                if v in id_map:
                    obj[k] = id_map[v]
            elif isinstance(v, list):
                obj[k] = _rewrite_id_refs(v, id_map)
            elif isinstance(v, dict):
                obj[k] = _rewrite_id_refs(v, id_map)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            obj[i] = _rewrite_id_refs(item, id_map)
    return obj


def reset_draft_to_clean(draft):
    keep_keys = [
        "canvas_config", "color_space", "config", "cover", "create_time",
        "duration", "extra_info", "fps", "free_render_index_mode_on",
        "group_container", "id", "keyframe_graph_list", "keyframes",
        "last_modified_platform", "mutable_config", "name", "new_version",
        "platform", "relationships", "render_index_track_mode_on",
        "retouch_cover", "source", "static_cover_image_path", "time_marks",
        "update_time", "version",
    ]
    preserved = {k: draft[k] for k in keep_keys if k in draft}
    preserved["tracks"] = []
    preserved["materials"] = {}
    return preserved


def _is_inner_empty(inner):
    inner_draft = inner.get("draft", {})
    for t in inner_draft.get("tracks", []):
        if t.get("segments", []):
            return False
    return True


def _has_text_track(inner):
    inner_draft = inner.get("draft", {})
    for t in inner_draft.get("tracks", []):
        if t.get("type") == "text" and t.get("segments", []):
            return True
    return False


def _process_flat_track(itrack, id_map, offset_us, stretch_to_duration_us, clip_alpha):
    new_track = deepcopy(itrack)
    old_track_id = new_track.get("id")
    new_track_id = gen_uuid()
    if old_track_id:
        id_map[old_track_id] = new_track_id
    new_track["id"] = new_track_id

    segs = new_track.get("segments", [])
    if segs:
        orig_total = sum(s.get("target_timerange", {}).get("duration", 0)
                         for s in segs)
        target_total = stretch_to_duration_us if stretch_to_duration_us else orig_total
        for seg in segs:
            tgt = seg.get("target_timerange", {})
            cur_dur = tgt.get("duration", 0)
            cur_start = tgt.get("start", 0)

            if stretch_to_duration_us and orig_total > 0:
                ratio = target_total / orig_total
                new_dur = int(cur_dur * ratio)
                new_start = int(cur_start * ratio) + offset_us
            else:
                new_dur = cur_dur
                new_start = cur_start + offset_us

            seg["target_timerange"] = {"start": new_start, "duration": new_dur}

            if "source_timerange" in seg and seg["source_timerange"]:
                src = seg["source_timerange"]
                src_cur = src.get("duration", 0)
                src_start = src.get("start", 0)
                if src_cur > 0:
                    src_ratio = new_dur / src_cur
                    seg["source_timerange"] = {
                        "start": src_start,
                        "duration": int(src_cur * src_ratio),
                    }

            for sid_field in ("id", "material_id"):
                old_sid = seg.get(sid_field)
                if old_sid:
                    new_sid = gen_uuid()
                    id_map[old_sid] = new_sid
                    seg[sid_field] = new_sid

            if clip_alpha is not None:
                seg.setdefault("clip", {})["alpha"] = clip_alpha

    return new_track


def _inner_track_types(inner):
    idr = inner.get("draft", {})
    types = []
    for t in idr.get("tracks", []):
        if t.get("segments", []):
            types.append(t.get("type"))
    return types


def _is_track_all_origin(itrack, force_alpha=None):
    for s in itrack.get("segments", []):
        clip = s.get("clip", {})
        if not isinstance(clip, dict):
            return False
        tf = clip.get("transform", {})
        alpha = force_alpha if force_alpha is not None else clip.get("alpha", 1.0)
        rot = clip.get("rotation", 0.0) or 0.0
        sc = clip.get("scale", {}) or {}
        sx = sc.get("x", 1.0)
        sy = sc.get("y", 1.0)
        if (abs(tf.get("x", 0)) >= 0.05
                or abs(tf.get("y", 0)) >= 0.05
                or round(alpha, 2) < 1.0
                or abs(rot) > 0.5
                or abs(sx - 1.0) > 0.01
                or abs(sy - 1.0) > 0.01):
            return False
    return True


def _dedup_key(itrack):
    seg_tf_coords = []
    for s in itrack.get("segments", []):
        clip = s.get("clip", {})
        tf = clip.get("transform", {}) if isinstance(clip, dict) else {}
        seg_tf_coords.append((
            round(tf.get("x", 0), 3),
            round(tf.get("y", 0), 3),
            round(clip.get("alpha", 1), 2) if isinstance(clip, dict) else 1,
        ))
    return (itrack.get("type"), tuple(sorted(seg_tf_coords)))


def _collect_inner_materials(inner_draft, new_materials, id_map):
    for mat_key, mat_list in inner_draft.get("materials", {}).items():
        if mat_key == "drafts":
            continue
        if not isinstance(mat_list, list) or not mat_list:
            continue
        if mat_key not in new_materials:
            new_materials[mat_key] = []
        for mat in mat_list:
            new_mat = deepcopy(mat)
            old_mid = new_mat.get("id")
            if old_mid:
                new_mid = id_map.get(old_mid, gen_uuid())
                id_map[old_mid] = new_mid
                new_mat["id"] = new_mid
            new_materials[mat_key].append(new_mat)


def merge_preset_into_draft(draft, preset_path, offset_us=0, stretch_to_duration_us=None,
                            clip_alpha=None):
    with open(preset_path, "r", encoding="utf-8") as f:
        preset = json.load(f)

    id_map = {}
    new_flat_tracks = []
    new_materials = {}
    new_nested_drafts = []
    seen_track_keys = set()
    seen_sticker_material_ids = set()

    raw_inner_drafts = preset.get("materials", {}).get("drafts", [])
    valid_inner_drafts = [i for i in raw_inner_drafts if not _is_inner_empty(i)]

    has_flattenable_inner = False
    for inner in valid_inner_drafts:
        track_types = _inner_track_types(inner)
        if any(t in ("sticker", "video") for t in track_types):
            has_flattenable_inner = True
            break

    preserve_top_tracks = not has_flattenable_inner

    for inner in valid_inner_drafts:
        inner_draft = inner.get("draft", {})
        inner_mats = inner_draft.get("materials", {})
        itracks = inner_draft.get("tracks", [])
        track_types = _inner_track_types(inner)

        if "text" in track_types:
            new_inner = deepcopy(inner)
            new_inner["combination_id"] = gen_uuid()
            inner_draft_obj = new_inner.get("draft", {})
            if inner_draft_obj.get("id"):
                inner_draft_obj["id"] = gen_uuid()

            for itrack in inner_draft_obj.get("tracks", []):
                segs = itrack.get("segments", [])
                if not segs:
                    continue
                orig_total = sum(s.get("target_timerange", {}).get("duration", 0)
                                 for s in segs)
                target_total = stretch_to_duration_us if stretch_to_duration_us else orig_total
                for seg in segs:
                    tgt = seg.get("target_timerange", {})
                    cur_dur = tgt.get("duration", 0)
                    cur_start = tgt.get("start", 0)
                    if stretch_to_duration_us and orig_total > 0:
                        ratio = target_total / orig_total
                        new_dur = int(cur_dur * ratio)
                        new_start = int(cur_start * ratio) + offset_us
                    else:
                        new_dur = cur_dur
                        new_start = cur_start + offset_us
                    seg["target_timerange"] = {"start": new_start, "duration": new_dur}
                    if "source_timerange" in seg and seg["source_timerange"]:
                        src = seg["source_timerange"]
                        src_cur = src.get("duration", 0)
                        src_start = src.get("start", 0)
                        if src_cur > 0:
                            src_ratio = new_dur / src_cur
                            seg["source_timerange"] = {
                                "start": src_start,
                                "duration": int(src_cur * src_ratio),
                            }

            new_nested_drafts.append(new_inner)
            continue

        if all(t == "sticker" for t in track_types):
            any_sticker_kept = False
            for itrack in itracks:
                if not itrack.get("segments", []):
                    continue
                key = _dedup_key(itrack)
                if key in seen_track_keys:
                    continue
                seen_track_keys.add(key)

                seg_mid = itrack["segments"][0].get("material_id", "")
                if seg_mid and seg_mid in seen_sticker_material_ids:
                    continue
                if seg_mid:
                    seen_sticker_material_ids.add(seg_mid)

                new_track = _process_flat_track(
                    itrack, id_map, offset_us, stretch_to_duration_us, clip_alpha
                )
                new_flat_tracks.append(new_track)
                any_sticker_kept = True

            if any_sticker_kept:
                _collect_inner_materials(inner_draft, new_materials, id_map)
            continue

        if all(t == "video" for t in track_types):
            for itrack in itracks:
                if not itrack.get("segments", []):
                    continue
                if _is_track_all_origin(itrack):
                    continue
                key = _dedup_key(itrack)
                if key in seen_track_keys:
                    continue
                seen_track_keys.add(key)
                new_track = _process_flat_track(
                    itrack, id_map, offset_us, stretch_to_duration_us, clip_alpha
                )
                new_flat_tracks.append(new_track)
                _collect_inner_materials(inner_draft, new_materials, id_map)
            continue

    if preserve_top_tracks:
        for pt in preset.get("tracks", []):
            if not pt.get("segments", []):
                continue
            new_track = _process_flat_track(
                pt, id_map, offset_us, stretch_to_duration_us, clip_alpha
            )
            new_flat_tracks.append(new_track)

        _collect_inner_materials(preset, new_materials, id_map)

    for t in new_flat_tracks:
        _rewrite_id_refs(t, id_map)
    for mk in new_materials:
        for m in new_materials[mk]:
            _rewrite_id_refs(m, id_map)

    existing_mat_keys = set(draft.get("materials", {}).keys())
    for mk, mv in new_materials.items():
        if not mv:
            continue
        if mk in existing_mat_keys:
            draft["materials"][mk].extend(mv)
        else:
            draft["materials"][mk] = mv

    if new_nested_drafts:
        draft["materials"].setdefault("drafts", []).extend(new_nested_drafts)

    draft.setdefault("tracks", []).extend(new_flat_tracks)

    return len(new_flat_tracks), sum(len(v) for v in new_materials.values()) + len(new_nested_drafts)


def crop_before_us(draft, cut_us):
    cut_track_segs = []
    cut_mat_ids = set()

    for track in draft.get("tracks", []):
        new_segs = []
        for seg in track.get("segments", []):
            tgt = seg.get("target_timerange", {})
            src = seg.get("source_timerange", {})
            tgt_start = tgt.get("start", 0)
            tgt_dur = tgt.get("duration", 0)
            tgt_end = tgt_start + tgt_dur

            if tgt_end <= cut_us:
                cut_mat_ids.add(seg.get("material_id", ""))
                cut_track_segs.append((track.get("type"), seg.get("material_id", ""), "entirely before"))
                continue

            if tgt_start < cut_us:
                overlap_before = cut_us - tgt_start
                new_tgt_start = cut_us
                new_tgt_dur = tgt_dur - overlap_before
                if src:
                    src_start = src.get("start", 0)
                    new_src_start = src_start + overlap_before
                    seg["source_timerange"] = {"start": new_src_start, "duration": new_tgt_dur}
                seg["target_timerange"] = {"start": new_tgt_start, "duration": new_tgt_dur}
                new_segs.append(seg)
                cut_track_segs.append((track.get("type"), seg.get("material_id", ""), f"cropped {tgt_start}->{new_tgt_start}"))
            else:
                new_segs.append(seg)

        track["segments"] = new_segs

    if cut_mat_ids:
        for mk in ("texts", "stickers", "videos", "audios"):
            existing = draft["materials"].get(mk, [])
            filtered = [m for m in existing if m.get("id") not in cut_mat_ids]
            removed = len(existing) - len(filtered)
            if removed > 0:
                cut_track_segs.append((f"materials.{mk}", removed, "removed"))
            draft["materials"][mk] = filtered

    return cut_track_segs


def find_filter_video_segments(draft):
    results = []
    for ti, track in enumerate(draft.get("tracks", [])):
        if track.get("type") != "video":
            continue
        for si, seg in enumerate(track.get("segments", [])):
            clip = seg.get("clip", {})
            alpha = clip.get("alpha", 1.0)
            if alpha < 1.0 and "source_timerange" in seg and seg["source_timerange"]:
                results.append((ti, si, seg))
    return results


def set_filter_alpha(draft, alpha=0.05):
    changed = []
    for ti, track in enumerate(draft.get("tracks", [])):
        for si, seg in enumerate(track.get("segments", [])):
            clip = seg.get("clip", {})
            old_alpha = clip.get("alpha", 1.0)
            if old_alpha < 0.9:
                clip["alpha"] = alpha
                changed.append((ti, si, old_alpha, alpha))
    return changed


def add_audio_track_with_file(draft, wav_path, offset_us=0, volume=1.0):
    import wave
    with wave.open(wav_path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        dur_us = int(frames / rate * 1_000_000)

    mid = gen_uuid()
    aid = gen_uuid()

    audio_mat = {
        "aigc_type": "video_normal",
        "audio_channel_mapping": {"audio_channel_mapping": [0], "is_config_open": False},
        "audio_fade": None,
        "app_id": "",
        "category_id": "",
        "category_name": "",
        "check_flag": False,
        "copyright_limit_type": "allow",
        "duration": dur_us,
        "effect_id": "",
        "extra_type_option": 0,
        "formula_id": "",
        "has_audio": True,
        "id": aid,
        "intensifies_path": "",
        "is_ai_clone_tone": False,
        "is_text_edit_overdub": False,
        "is_unified_resource": False,
        "local_materials": [{"file_Path": os.path.abspath(wav_path), "height": 0, "width": 0}],
        "multi_language_values": [],
        "name": os.path.basename(wav_path),
        "path": os.path.abspath(wav_path),
        "related_id": "",
        "source_platform": 0,
        "source_url": "",
        "team_id": "",
        "type": 2,
    }

    seg = {
        "business_id": "",
        "caption_info": None,
        "cartoon": False,
        "clip": {"alpha": 1.0, "volume": volume},
        "common_keyframes": [],
        "extra_material_refs": [],
        "group_id": "",
        "id": gen_uuid(),
        "intensifies_audio": False,
        "is_loop": False,
        "is_placeholder": False,
        "keyframe_refs": [],
        "last_nonzero_volume": volume,
        "material_id": aid,
        "render_index": 0,
        "render_timerange": {"duration": 0, "start": 0},
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0, "target_follow": ""},
        "reverse": False,
        "segment_color_tag": "",
        "source_timerange": {"duration": dur_us, "start": 0},
        "speed": 1.0,
        "state": 0,
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 1,
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True,
        "volume": volume,
        "target_timerange": {"duration": dur_us, "start": offset_us},
    }

    track = {
        "attribute": 0,
        "id": mid,
        "is_default_track": False,
        "material_animations": [],
        "segments": [seg],
        "speed": 1.0,
        "type": "audio",
    }

    if "audios" not in draft["materials"]:
        draft["materials"]["audios"] = []
    draft["materials"]["audios"].append(audio_mat)
    draft.setdefault("tracks", []).append(track)

    return aid, mid, dur_us


def _build_subtitle_content(text, font_path=None):
    if font_path is None:
        font_path = "D:/剪映5.9/剪映5.9版+jianying-skill/剪映5.9(win+mac)/剪映5.9Windows/JianyingPro/5.9.0.11632/Resources/Font/SystemFont/zh-hans.ttf"
    return json.dumps({
        "styles": [{
            "fill": {
                "alpha": 1.0,
                "content": {
                    "render_type": "solid",
                    "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}
                }
            },
            "font": {"id": "", "path": font_path},
            "range": [0, len(text)],
            "size": 8.0,
        }],
        "text": text,
    }, ensure_ascii=False)


def add_text_segments(draft, sub_segments, offset_us=0, font="HarmonyOS Sans SC",
                       font_size=30, color=[1.0, 1.0, 1.0, 1.0], stroke_color=[0, 0, 0, 0.8],
                       stroke_width=4, visible=True):
    text_mats = []
    segs = []

    MIN_DUR_US = 1_500_000

    for idx, sub in enumerate(sub_segments):
        text_str = sub["text"] if isinstance(sub, dict) else str(sub)
        if not text_str.strip():
            continue

        tid = gen_uuid()

        start_us = sub["start_us"] + offset_us if isinstance(sub, dict) else 0
        end_us = sub["end_us"] + offset_us if isinstance(sub, dict) else 0
        dur_us = end_us - start_us
        if dur_us < MIN_DUR_US:
            dur_us = MIN_DUR_US
            end_us = start_us + dur_us

        if idx + 1 < len(sub_segments):
            next_sub = sub_segments[idx + 1]
            next_start = (next_sub["start_us"] + offset_us) if isinstance(next_sub, dict) else 0
            if end_us > next_start:
                end_us = next_start - 100_000
                dur_us = end_us - start_us
                if dur_us < 500_000:
                    end_us = start_us + MIN_DUR_US
                    dur_us = MIN_DUR_US

        text_mat = {
            "add_type": 1,
            "alignment": 1,
            "background_alpha": 1.0,
            "background_color": [],
            "background_height": 0.14,
            "background_horizontal_offset": 0.0,
            "background_round_radius": 0.0,
            "background_style": 0,
            "background_vertical_offset": 0.0,
            "background_width": 0.14,
            "base_content": "",
            "bold_width": 0.0,
            "border_alpha": 1.0,
            "border_color": [],
            "border_width": 0.08,
            "caption_template_info": {"category_id": "", "category_name": "", "effect_id": "", "is_new": False, "path": "", "request_id": "", "resource_id": "", "resource_name": "", "source_platform": 0},
            "check_flag": 7,
            "combo_info": {"text_templates": []},
            "content": _build_subtitle_content(text_str),
            "fixed_height": -1.0,
            "fixed_width": -1.0,
            "font_category_id": "",
            "font_category_name": "",
            "font_id": "",
            "font_name": "",
            "font_path": "D:/剪映5.9/剪映5.9版+jianying-skill/剪映5.9(win+mac)/剪映5.9Windows/JianyingPro/5.9.0.11632/Resources/Font/SystemFont/zh-hans.ttf",
            "font_resource_id": "",
            "font_size": 8.0,
            "font_source_platform": 0,
            "font_team_id": "",
            "font_title": "none",
            "font_url": "",
            "fonts": [],
            "force_apply_line_max_width": False,
            "global_alpha": 1.0,
            "group_id": "",
            "has_shadow": False,
            "id": tid,
            "initial_scale": 1.0,
            "inner_padding": -1.0,
            "is_rich_text": False,
            "italic_degree": 0,
            "ktv_color": "",
            "language": "",
            "layer_weight": 1,
            "letter_spacing": 0.0,
            "line_feed": 1,
            "line_max_width": 0.82,
            "line_spacing": 0.02,
            "multi_language_current": "none",
            "name": text_str[:20],
            "original_size": [],
            "preset_category": "",
            "preset_category_id": "",
            "preset_has_set_alignment": False,
            "preset_id": "",
            "preset_index": 0,
            "preset_name": "",
            "recognize_task_id": "",
            "recognize_type": 0,
            "relevance_segment": [],
            "shadow_alpha": 0.9,
            "shadow_angle": -45.0,
            "shadow_color": [],
            "shadow_distance": 5.0,
            "shadow_point": {"x": 0.6363961030678928, "y": -0.6363961030678928},
            "shadow_smoothing": 0.45,
            "shape_clip_x": False,
            "shape_clip_y": False,
            "source_from": "",
            "style_name": "",
            "sub_type": 0,
            "subtitle_keywords": None,
            "subtitle_template_original_fontsize": 0.0,
            "text_alpha": 1.0,
            "text_color": "#FFFFFF",
            "text_curve": None,
            "text_preset_resource_id": "",
            "text_size": font_size,
            "text_to_audio_ids": [],
            "tts_auto_update": False,
            "type": "subtitle",
            "typesetting": 0,
            "underline": False,
            "underline_offset": 0.22,
            "underline_width": 0.05,
            "use_effect_default_color": True,
            "words": {"end_time": [], "start_time": [], "text": []},
        }

        seg = {
            "business_id": "",
            "caption_info": None,
            "cartoon": False,
            "clip": {"alpha": 1.0},
            "common_keyframes": [],
            "desc": "",
            "extra_material_refs": [],
            "group_id": "",
            "id": gen_uuid(),
            "keyframe_refs": [],
            "material_id": tid,
            "render_index": 0,
            "render_timerange": {"duration": 0, "start": 0},
            "responsive_layout": {"enable": False, "horizontal_pos_layout": 0, "size_layout": 0, "target_follow": ""},
            "source": "segmentsourcenormal",
            "source_timerange": None,
            "speed": 1.0,
            "state": 0,
            "template_scene": "default",
            "track_attribute": 0,
            "track_render_index": 1,
            "visible": visible,
            "target_timerange": {"duration": dur_us, "start": start_us},
        }

        text_mats.append(text_mat)
        segs.append(seg)

    track = {
        "attribute": 0,
        "id": gen_uuid(),
        "is_default_track": False,
        "material_animations": [],
        "segments": segs,
        "speed": 1.0,
        "type": "text",
    }

    if "texts" not in draft["materials"]:
        draft["materials"]["texts"] = []
    draft["materials"]["texts"].extend(text_mats)
    draft.setdefault("tracks", []).append(track)

    return len(text_mats)


def hide_text_track_by_language(draft, lang_cn=True):
    hidden_count = 0
    for track in draft.get("tracks", []):
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []):
            seg["visible"] = False
            mid = seg.get("material_id", "")
            for m in draft.get("materials", {}).get("texts", []):
                if m.get("id") == mid:
                    m["visible"] = False
                    hidden_count += 1
                    break
    return hidden_count


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".rmvb", ".rm", ".3gp"}


def _is_video_path(p):
    return os.path.splitext(p)[1].lower() in VIDEO_EXTS


def get_video_duration_us(video_path):
    """返回视频时长（微秒）。优先 ffprobe，后备 struct 解析 MP4 moov box。"""
    try:
        import shutil as _sh
        ffprobe_path = _sh.which("ffprobe")
        if ffprobe_path:
            import subprocess as _sp
            result = _sp.run(
                [ffprobe_path, "-v", "error", "-show_entries",
                 "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                 video_path],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                secs = float(result.stdout.strip())
                return int(secs * 1_000_000)
    except Exception:
        pass

    ext = os.path.splitext(video_path)[1].lower()
    if ext in (".mp4", ".m4v", ".mov", ".3gp"):
        dur = _parse_mp4_duration(video_path)
        if dur is not None:
            return dur

    print(f"  ⚠️ 无法获取视频时长: {video_path}")
    return None


def _parse_mp4_duration(path):
    """struct 解析 MP4 moov/mvhd 获取 duration。"""
    import struct
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(0)

            moov_offset = None
            while f.tell() < size - 8:
                header = f.read(8)
                if len(header) < 8:
                    break
                box_size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8].decode("ascii", errors="replace")
                if box_size == 1:
                    box_size = struct.unpack(">Q", f.read(8))[0]
                    header_len = 16
                else:
                    header_len = 8

                if box_type == "moov":
                    moov_offset = f.tell()
                    moov_end = moov_offset + box_size - header_len
                    break
                f.seek(box_size - header_len, 1)

            if moov_offset is None:
                return None

            f.seek(moov_offset)
            while f.tell() < moov_end - 8:
                header = f.read(8)
                if len(header) < 8:
                    break
                box_size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8].decode("ascii", errors="replace")
                if box_size == 1:
                    box_size = struct.unpack(">Q", f.read(8))[0]
                    header_len = 16
                else:
                    header_len = 8

                if box_type == "mvhd":
                    data_start = f.tell()
                    version = f.read(1)[0]
                    f.read(3)
                    if version == 1:
                        f.read(8)
                        f.read(8)
                        timescale = struct.unpack(">I", f.read(4))[0]
                        duration = struct.unpack(">Q", f.read(8))[0]
                    else:
                        f.read(4)
                        f.read(4)
                        timescale = struct.unpack(">I", f.read(4))[0]
                        duration = struct.unpack(">I", f.read(4))[0]
                    if timescale > 0 and duration > 0:
                        return int(duration / timescale * 1_000_000)
                    return None
                f.seek(box_size - header_len, 1)
    except Exception as e:
        print(f"  ⚠️ MP4 解析失败: {e}")
    return None


BLANK_VIDEO_MATERIAL = {
    "aigc_type": "none",
    "audio_fade": None,
    "cartoon_path": "",
    "category_id": "",
    "category_name": "local",
    "check_flag": 63487,
    "crop": {
        "lower_left_x": 0.0, "lower_left_y": 1.0,
        "lower_right_x": 1.0, "lower_right_y": 1.0,
        "upper_left_x": 0.0, "upper_left_y": 0.0,
        "upper_right_x": 1.0, "upper_right_y": 0.0,
    },
    "crop_ratio": "free",
    "crop_scale": 1.0,
    "extra_type_option": 0,
    "formula_id": "",
    "freeze": None,
    "has_audio": True,
    "height": 1920,
    "intensifies_audio_path": "",
    "intensifies_path": "",
    "is_ai_generate_content": False,
    "is_copyright": False,
    "is_text_edit_overdub": False,
    "is_unified_beauty_mode": False,
    "local_id": "",
    "local_material_id": "",
    "matting": {
        "flag": 0, "has_use_quick_brush": False, "has_use_quick_eraser": False,
        "interactiveTime": [], "path": "", "strokes": [],
    },
    "media_path": "",
    "object_locked": None,
    "origin_material_id": "",
    "picture_from": "none",
    "picture_set_category_id": "",
    "picture_set_category_name": "",
    "request_id": "",
    "reverse_intensifies_path": "",
    "reverse_path": "",
    "smart_motion": None,
    "source": 0,
    "source_platform": 0,
    "stable": {"matrix_path": "", "stable_level": 0,
               "time_range": {"duration": 0, "start": 0}},
    "team_id": "",
    "type": "video",
    "video_algorithm": {
        "algorithms": [], "complement_frame_config": None, "deflicker": None,
        "gameplay_configs": [], "motion_blur_config": None, "noise_reduction": None,
        "path": "", "quality_enhance": None, "time_range": None,
    },
    "width": 1080,
}


BLANK_VIDEO_SEGMENT = {
    "caption_info": None,
    "cartoon": False,
    "clip": {
        "alpha": 1.0,
        "flip": {"horizontal": False, "vertical": False},
        "rotation": 0.0,
        "scale": {"x": 1.0, "y": 1.0},
        "transform": {"x": 0.0, "y": 0.0},
    },
    "common_keyframes": [],
    "enable_adjust": True,
    "enable_color_correct_adjust": False,
    "enable_color_curves": True,
    "enable_color_match_adjust": False,
    "enable_color_wheels": True,
    "enable_lut": True,
    "enable_smart_color_adjust": False,
    "extra_material_refs": [],
    "group_id": "",
    "hdr_settings": {"intensity": 1.0, "mode": 1, "nits": 1000},
    "intensifies_audio": False,
    "is_placeholder": False,
    "is_tone_modify": False,
    "keyframe_refs": [],
    "last_nonzero_volume": 1.0,
    "render_index": 0,
    "responsive_layout": {
        "enable": False, "horizontal_pos_layout": 0, "size_layout": 0,
        "target_follow": "", "vertical_pos_layout": 0,
    },
    "reverse": False,
    "speed": 1.0,
    "template_id": "",
    "template_scene": "default",
    "track_attribute": 0,
    "track_render_index": 0,
    "uniform_scale": {"on": True, "value": 1.0},
    "visible": True,
    "volume": 1.0,
}


def inject_videos_into_draft(draft, video_paths):
    """
    往已有草稿里注入视频（追加主轨 + materials.videos）。
    - 多个视频合并拼接在同一条主 video track 上
    - 如果已有 video 主轨且有 segments，先清空它们（让注入的视频做第一轨）
    """
    video_paths = [os.path.abspath(p) for p in video_paths]
    for p in video_paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"视频不存在: {p}")

    print(f"🎬 往已有草稿注入 {len(video_paths)} 个视频:")

    cum_start = 0
    segs = []
    materials_videos = []

    for vp in video_paths:
        dur_us = get_video_duration_us(vp)
        if dur_us is None:
            print(f"  ⚠️ 无法获取 {os.path.basename(vp)} 时长，跳过")
            continue

        vid = gen_uuid()
        fname = os.path.basename(vp)
        print(f"  📼 {fname}  →  {dur_us/1_000_000:.2f}s  [{cum_start/1_000_000:.2f}s - {(cum_start+dur_us)/1_000_000:.2f}s]")

        mat = deepcopy(BLANK_VIDEO_MATERIAL)
        mat["id"] = vid
        mat["material_id"] = vid
        mat["path"] = vp
        mat["material_name"] = fname
        mat["duration"] = dur_us

        seg = deepcopy(BLANK_VIDEO_SEGMENT)
        seg["id"] = gen_uuid()
        seg["material_id"] = vid
        seg["source_timerange"] = {"start": 0, "duration": dur_us}
        seg["target_timerange"] = {"start": cum_start, "duration": dur_us}

        materials_videos.append(mat)
        segs.append(seg)
        cum_start += dur_us

    if not segs:
        raise RuntimeError("所有视频都无法获取时长！")

    main_track_id = gen_uuid()
    main_track = {
        "attribute": 0,
        "id": main_track_id,
        "is_default_track": True,
        "material_animations": [],
        "segments": segs,
        "speed": 1.0,
        "type": "video",
    }

    existing_videos = draft.setdefault("materials", {}).get("videos", [])
    draft["materials"]["videos"] = existing_videos + materials_videos

    tracks = draft.setdefault("tracks", [])
    tracks.insert(0, main_track)

    draft["duration"] = cum_start

    print(f"  ✅ 注入完成: {len(segs)} segments, {len(materials_videos)} materials")
    print(f"  📏 主轨总时长: {cum_start/1_000_000:.2f}s")
    print()

    return cum_start


def get_video_filepath_from_draft(draft):
    for track in draft.get("tracks", []):
        if track.get("type") != "video":
            continue
        for seg in track.get("segments", []):
            mid = seg.get("material_id")
            for vm in draft.get("materials", {}).get("videos", []):
                if vm.get("id") == mid:
                    path = vm.get("path", "")
                    if path and os.path.exists(path) and _is_video_path(path):
                        return path
                    for lm in vm.get("local_materials", []):
                        fp = lm.get("file_Path", "")
                        if fp and os.path.exists(fp) and _is_video_path(fp):
                            return fp
    return None


def get_audio_filepath_from_draft(draft):
    for track in draft.get("tracks", []):
        if track.get("type") != "audio":
            continue
        for seg in track.get("segments", []):
            mid = seg.get("material_id")
            for am in draft.get("materials", {}).get("audios", []):
                if am.get("id") == mid:
                    path = am.get("path", "")
                    if path and os.path.exists(path):
                        return path
                    for lm in am.get("local_materials", []):
                        fp = lm.get("file_Path", "")
                        if fp and os.path.exists(fp):
                            return fp
    return None


def run_demucs(input_path, output_dir, model="htdemucs"):
    import subprocess, shutil as _shutil
    py = _shutil.which("python") or _shutil.which("python3") or sys.executable
    cmd = [py, "-m", "demucs", "-n", model, "--two-stems=vocals", "-o", output_dir, input_path]
    print(f"  🎵 demucs: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        print(f"  ✗ demucs failed: {result.stderr[-500:]}")
        return None, None
    base = os.path.join(output_dir, model, os.path.splitext(os.path.basename(input_path))[0])
    vocal = os.path.join(base, "vocals.wav")
    no_vocal = os.path.join(base, "no_vocals.wav")
    if not os.path.exists(vocal):
        alt = os.path.join(base, "vocal.wav")
        if os.path.exists(alt):
            vocal = alt
    return vocal if os.path.exists(vocal) else None, no_vocal if os.path.exists(no_vocal) else None


def run_whisper(input_path, model_size="base"):
    from faster_whisper import WhisperModel
    print(f"  🗣️ loading whisper model ({model_size})...")
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    except Exception:
        model = WhisperModel(model_size, device="cpu", compute_type="default")
    print(f"  🗣️ transcribing...")
    segments, info = model.transcribe(input_path, beam_size=5, vad_filter=True)
    subs = []
    for seg in segments:
        subs.append({
            "start_us": int(seg.start * 1_000_000),
            "end_us": int(seg.end * 1_000_000),
            "text": seg.text.strip(),
        })
    return subs


def run_ffmpeg_extract_audio(video_path, output_wav):
    import shutil as _shutil
    ffmpeg = _shutil.which("ffmpeg")
    if not ffmpeg:
        import glob as _glob
        for p in _glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages", "*ffmpeg*", "ffmpeg-*", "bin", "ffmpeg.exe")):
            ffmpeg = p
            break
    if not ffmpeg:
        print("  ⚠ ffmpeg not found — please install ffmpeg to enable audio extraction")
        return False
    cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1", output_wav]
    print(f"  🎬 ffmpeg: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        print("  ⚠ ffmpeg not found")
        return False
    if result.returncode != 0:
        print(f"  ✗ ffmpeg failed: {result.stderr[-500:]}")
        return False
    return True


def summarize_draft(draft):
    tracks = draft.get("tracks", [])
    mats = draft.get("materials", {})
    out = []
    out.append(f"tracks: {len(tracks)}")
    for t in tracks:
        segs = t.get("segments", [])
        types = [s.get("material_id", "")[:8] for s in segs[:3]]
        out.append(f"  [{t.get('type'):8s}] segs={len(segs)} mids={types}")
    for mk in ("videos", "audios", "texts", "stickers", "effects"):
        v = mats.get(mk, [])
        out.append(f"  materials.{mk}: {len(v)}")
    return "\n".join(out)


def draft_duration(draft):
    max_end = 0
    for track in draft.get("tracks", []):
        for seg in track.get("segments", []):
            tgt = seg.get("target_timerange", {})
            end = tgt.get("start", 0) + tgt.get("duration", 0)
            max_end = max(max_end, end)
    return max_end


def get_main_video_duration(draft):
    for track in draft.get("tracks", []):
        if track.get("type") != "video":
            continue
        for seg in track.get("segments", []):
            tgt = seg.get("target_timerange", {})
            dur = tgt.get("duration", 0)
            if dur > 0:
                return draft_duration(draft)
    return draft_duration(draft)


def run_pipeline(draft_name_or_path, video_paths=None,
                 cut_us=None, workdir=None,
                 skip_local_ai=False, do_export=True,
                 do_demucs=True, do_presets=True, do_ffmpeg=True,
                 do_whisper=True, do_translate=True, do_hide_cn=True):
    if workdir is None:
        workdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_work")
    os.makedirs(workdir, exist_ok=True)

    PRESETS = [
        ("logo文字", None),
        ("上新", None),
        ("三连~", None),
        ("滤镜+透明度", 0.03),
    ]

    print(f"\n{'='*60}")
    print(f"  🎬 JianYing Full-Local Pipeline")
    print(f"  📂 Draft      : {draft_name_or_path}")
    print(f"  🎥 Videos     : {len(video_paths) if video_paths else '(无, 使用已有)'}")
    print(f"  ✂️ cut_us     : {cut_us}  ({cut_us/1_000_000 if cut_us else 'None (no cut)'}s)")
    print(f"  🧩 Presets    : {[p[0] for p in PRESETS] if do_presets else 'skip'}")
    print(f"  🛑 Skip AI    : {skip_local_ai}")
    print(f"  📤 Export     : {do_export}")
    print(f"  📁 Workdir    : {workdir}")
    print(f"{'='*60}\n")

    if not draft_name_or_path:
        print("✗ 必须指定草稿名!")
        return None

    dp_root = None
    draft_path = None
    draft = None

    if os.path.isdir(draft_name_or_path):
        draft_path = os.path.join(draft_name_or_path, "draft_content.json")
        dp_root = draft_name_or_path
    else:
        dp_root, jp, meta = find_draft_by_name(draft_name_or_path)
        if dp_root is None:
            print(f"📂 草稿 '{draft_name_or_path}' 不存在, 自动创建空白草稿...")
            dp_root, jp, _ = create_empty_draft(draft_name_or_path)
            if dp_root is None:
                print("✗ 创建空白草稿失败 (找不到模板)")
                return None
            print(f"✅ 已创建空白草稿: {draft_name_or_path}")
        draft_path = jp
    draft, _ = load_draft(draft_path)
    print(f"✅ Loaded existing draft: {draft.get('name') or dp_root}")
    print(f"   Pre-state:\n{summarize_draft(draft)}")

    draft = reset_draft_to_clean(draft)
    print(f"🔄 Draft reset to clean state (tracks=[], materials={{}})")

    if video_paths:
        inject_videos_into_draft(draft, video_paths)
    print()

    main_dur = get_main_video_duration(draft)
    print(f"📏 主视频轨总时长: {main_dur/1_000_000:.2f}s ({main_dur}us)")
    print()

    video_path = get_video_filepath_from_draft(draft)
    existing_audio = get_audio_filepath_from_draft(draft)
    audio_wav = None

    # ━━━━ Step 1: 人声分离 ━━━━
    if do_demucs:
        print("━ [1] 人声分离 (demucs) ━━━━━━━━━━━━━━━━━━━━━━━━━━")
        source = video_path or existing_audio
        if not skip_local_ai and source:
            try:
                tmp_wav = os.path.join(workdir, "extract_input.wav")
                src_for_demucs = source
                if video_path and not (existing_audio and existing_audio.endswith(".wav")):
                    print("  🎬 先从视频提取音频...")
                    ok = run_ffmpeg_extract_audio(video_path, tmp_wav)
                    if ok:
                        src_for_demucs = tmp_wav

                vocal, bgm = run_demucs(src_for_demucs, workdir)
                if vocal and bgm:
                    aid_v, mid_v, _ = add_audio_track_with_file(draft, vocal, volume=1.0)
                    aid_b, mid_b, _ = add_audio_track_with_file(draft, bgm, volume=0.8)
                    print(f"  ✅ 人声轨: material_id={aid_v[:12]}...")
                    print(f"  ✅ 伴奏轨: material_id={aid_b[:12]}...")
                else:
                    print(f"  ✗ demucs 输出缺失")
            except ImportError:
                print("  ⚠️ demucs 未安装 (pip install demucs)")
        else:
            print("  ⏭️ skip_local_ai=True 或无音频源")
        main_dur = get_main_video_duration(draft)
        print()

    # ━━━━ Step 2-5: 四个预设（自动拉伸对齐主轨） ━━━━
    if do_presets:
        for idx, (preset_name, alpha_override) in enumerate(PRESETS, start=2):
            print(f"━ [{idx}] 加预设: {preset_name} ━━━━━━━━━━━━━━━━━━━━━━━")
            pp = find_preset_by_name(preset_name)
            if not pp:
                print(f"  ✗ 找不到预设目录! (目录 '{preset_name}' 不存在)")
                continue
            try:
                nt, nm = merge_preset_into_draft(
                    draft, pp,
                    offset_us=0,
                    stretch_to_duration_us=main_dur,
                    clip_alpha=alpha_override,
                )
                alpha_note = f", clip.alpha={alpha_override}" if alpha_override else ""
                print(f"  ✅ 合并: +{nt} tracks, +{nm} materials{alpha_note}")

                last_tracks = draft["tracks"][-nt:]
                for ti, t in enumerate(last_tracks):
                    for si, seg in enumerate(t.get("segments", [])):
                        tgt = seg.get("target_timerange", {})
                        print(f"     track[{ti}].seg[{si}]: start={tgt.get('start')}, dur={tgt.get('duration')} "
                              f"(预期主轨总时长={main_dur})")
            except Exception as e:
                print(f"  ✗ 合并失败: {e}")
                import traceback
                traceback.print_exc()
            main_dur = get_main_video_duration(draft)
            print()

    # ━━━━ Step 6: 可选裁剪（所有预设都加完才裁） ━━━━
    if cut_us is not None:
        print(f"━ [6] 裁剪 {cut_us/1_000_000:.2f}s 之前 ━━━━━━━━━━━━━━━━━━━━━━")
        pre_dur = get_main_video_duration(draft)
        cuts = crop_before_us(draft, cut_us)
        for ct in cuts:
            if isinstance(ct[1], str):
                print(f"  ✂️  [{ct[0]}] mat_id={ct[1][:12]}... {ct[2]}")
            else:
                print(f"  🗑️  [{ct[0]}] {ct[1]} items {ct[2]}")
        main_dur = get_main_video_duration(draft)
        print(f"  📏 裁剪前 {pre_dur/1_000_000:.2f}s → 裁剪后 {main_dur/1_000_000:.2f}s")
        print()
    else:
        print("━ [6] 跳过裁剪 (cut_us=None) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print()

    # ━━━━ Step 7: 分离音频 (ffmpeg) ━━━━
    if do_ffmpeg:
        print("━ [7] 分离音频 (ffmpeg) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if not video_path:
            print("  ✗ 找不到视频!")
        else:
            audio_wav = os.path.join(workdir, "audio_extracted.wav")
            ok = run_ffmpeg_extract_audio(video_path, audio_wav)
            if ok:
                print(f"  ✅ 提取到: {audio_wav}")
        print()

    # ━━━━ Step 8: 识别字幕 ━━━━
    cn_subs = []
    if do_whisper:
        print("━ [8] 根据音频识别字幕 (faster-whisper) ━━━━━━━━━━━━━━━━")
        asr_source = audio_wav or get_audio_filepath_from_draft(draft)
        if not asr_source:
            print("  ✗ 没有音频可用于 ASR!")
        else:
            print(f"  🗣️ 使用音频: {asr_source}")
            if not skip_local_ai:
                try:
                    cn_subs = run_whisper(asr_source, model_size="base")
                    print(f"  ✅ 识别出 {len(cn_subs)} 条字幕")
                    if cn_subs:
                        nm = add_text_segments(draft, cn_subs, visible=True)
                        print(f"  ✅ 写入 {nm} 条中文字幕轨")
                except ImportError:
                    print("  ⚠️ faster-whisper 未安装 (pip install faster-whisper)")
            else:
                print("  ⏭️ skip_local_ai=True")
        print()

    # ━━━━ Step 9: 翻译英文字幕 ━━━━
    if do_translate:
        print("━ [9] 中文字幕翻译英文字幕 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if not cn_subs:
            print("  ⚠️ 没有中文字幕可翻译")
        elif skip_local_ai:
            print("  ⏭️ skip_local_ai=True，跳过翻译")
        else:
            try:
                from gemini_web import translate_texts
                cn_texts = [s["text"] for s in cn_subs]
                en_texts = translate_texts(cn_texts)
                en_subs = []
                for i, s in enumerate(cn_subs):
                    en_subs.append({
                        "start_us": s["start_us"],
                        "end_us": s["end_us"],
                        "text": en_texts[i] if i < len(en_texts) else "",
                    })
                if en_subs:
                    nm = add_text_segments(draft, en_subs, visible=True, font_size=30,
                                           color=[1.0, 1.0, 1.0, 1.0])
                    print(f"  ✅ 翻译并写入 {nm} 条英文字幕")
            except ImportError:
                if cn_subs:
                    en_subs = [{**s, "text": f"[EN] {s['text']}"} for s in cn_subs]
                    nm = add_text_segments(draft, en_subs, visible=True, font_size=30)
                    print(f"  ⚠️ gemini_web 不可用，占位英文 {nm} 条")
        print()

    # ━━━━ Step 10: 隐藏中文字幕 ━━━━
    if do_hide_cn:
        print("━ [10] 隐藏中文字幕 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        hidden = hide_text_track_by_language(draft, lang_cn=True)
        print(f"  🙈 隐藏了 {hidden} 个 text material")
        print()

    # ━━━━ 保存 ━━━━
    jy_running = _jianying_running()
    locked = _draft_json_locked(draft_path)
    if jy_running and not locked:
        print(f"⚠️  剪映运行中(未编辑此草稿) — 可以写入, 但写完后请在剪映里关闭并重新打开该草稿以刷新显示")
    if locked:
        print(f"✗ 草稿 JSON 被剪映锁定! 请先在剪映中关闭此草稿编辑页或完全退出剪映")
        print(f"  draft_path={draft_path}")
        return None
    bak = backup_draft(draft_path)
    save_draft(draft, draft_path)
    print(f"💾 Draft saved! 备份: {bak}")
    print(f"   Post-state:\n{summarize_draft(draft)}")
    print()

    print()

    # ━━━━ Step 11: 导出 ━━━━
    if do_export:
        print("━ [11] 导出 (robust_auto_export) ━━━━━━━━━━━━━━━━━━━━━━")
        try:
            import re as _re
            safe_draft_name = _re.sub(r'[\\/:*?"<>|]', '_', dp_root)
            out_path = os.path.join(workdir, f"{safe_draft_name}_export.mp4")

            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            ".trae", "skills", "jianying-editor", "examples"))
            from robust_auto_export import run as export_run

            print(f"  🎯 草稿名: {dp_root}")
            print(f"  📁 输出路径: {out_path}")
            print(f"  🔄 先确保剪映返回首页并重新打开草稿 (热加载 JSON 变更)...")
            export_run(dp_root, out_path)
            print(f"  ✅ 导出完成!")
        except Exception as e:
            print(f"  ⚠️ 自动导出异常: {e}")
            import traceback
            traceback.print_exc()
            print("  💡 请在剪映里手动点 [导出] 按钮")
        print()

    print(f"{'='*60}")
    print(f"  🎉 Pipeline 完成!")
    print(f"  📄 草稿 JSON: {draft_path}")
    print(f"{'='*60}\n")
    return draft


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JianYing Full-Local Pipeline (方案B: 注入已有草稿)")
    parser.add_argument("draft",
                        help="已有草稿名称或路径 (用剪映手动新建的草稿)")
    parser.add_argument("--video", action="append", default=None,
                        help="视频文件路径 (可多次指定, 注入到草稿主轨拼接)")
    parser.add_argument("--cut", default=None, type=float,
                        help="裁剪此时间点(秒)之前, 不指定则不裁剪")
    parser.add_argument("--workdir", default=None, help="工作目录")
    parser.add_argument("--skip-ai", action="store_true",
                        help="跳过 demucs/whisper/翻译 等本地AI")
    parser.add_argument("--no-export", action="store_true", help="跳过导出")
    parser.add_argument("--no-demucs", action="store_true", help="跳过人声分离")
    parser.add_argument("--no-presets", action="store_true", help="跳过所有预设")
    parser.add_argument("--no-asr", action="store_true", help="跳过字幕识别")
    parser.add_argument("--no-translate", action="store_true", help="跳过翻译")
    parser.add_argument("--no-hide-cn", action="store_true", help="不隐藏中文字幕")
    args = parser.parse_args()

    cut_us = int(args.cut * 1_000_000) if args.cut is not None else None

    run_pipeline(
        draft_name_or_path=args.draft,
        video_paths=args.video,
        cut_us=cut_us,
        workdir=args.workdir,
        skip_local_ai=args.skip_ai,
        do_export=not args.no_export,
        do_demucs=not args.no_demucs,
        do_presets=not args.no_presets,
        do_ffmpeg=True,
        do_whisper=not args.no_asr,
        do_translate=not args.no_translate,
        do_hide_cn=not args.no_hide_cn,
    )
