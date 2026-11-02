"""jy_cover — 剪映草稿封面注入（旧 drama-cover-builder Stage 3 下沉版）。

供 pipeline 与任何本地脚本共用的库模块，不是 skill 阶段。

两阶段写入，缺一不可（剪映 5.9）：
  1) draft_cover.jpg 作为剪映缩略图；
  2) 原生 composition 结构（Resources/cover/ + materials.drafts + 顶层 cover 字段）。
只做第 1 步时，剪映 5.9 打开草稿会用第一帧覆盖 draft_cover.jpg。

用法：
    from jy_cover import set_draft_cover, set_cover_composition
    set_draft_cover(draft_dir, cover_image="cover.jpg")
    set_cover_composition(draft_dict, draft_dir, "cover.jpg")
"""
import os, sys, json, uuid, shutil, subprocess, glob


def gen_uuid():
    return str(uuid.uuid4()).upper()


def _presets_deps_root():
    """向上查找 pipeline/presets.json 的 deps_root（可选配置，读不到返回空串）。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        d = os.path.dirname(d)
        if not d:
            break
        preset = os.path.join(d, "pipeline", "presets.json")
        if os.path.isfile(preset):
            try:
                with open(preset, encoding="utf-8") as f:
                    return (json.load(f).get("deps_root") or "").strip()
            except (OSError, ValueError):
                return ""
    return ""


def _find_tool(name):
    """定位 ffmpeg/ffprobe：JY_DEPS_ROOT → presets.json deps_root → PATH → WinGet。"""
    exe = name + (".exe" if os.name == "nt" else "")
    for base in (os.environ.get("JY_DEPS_ROOT", "").strip(), _presets_deps_root()):
        if not base:
            continue
        for rel in (os.path.join("tools", "ffmpeg", "bin", exe), os.path.join("bin", exe), exe):
            candidate = os.path.join(base, rel)
            if os.path.isfile(candidate):
                return candidate
    found = shutil.which(name)
    if found:
        return found
    for w in glob.glob(os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
            "Packages", "*ffmpeg*", "ffmpeg-*", "bin",
    )):
        fp = os.path.join(w, exe)
        if os.path.isfile(fp):
            return fp
    return None


def _guess_ffmpeg():
    return _find_tool("ffmpeg")


def set_draft_cover(draft_dir, cover_image=None, extract_from_video=None,
                    extract_time_s=None, smart_select=True,
                    scene_threshold=0.35, thumbnail_batch=500,
                    video_start_s=0.0):
    """设置草稿封面（draft_cover.jpg）。"""
    if not os.path.isdir(draft_dir):
        raise FileNotFoundError(f"草稿目录不存在: {draft_dir}")
    dst = os.path.join(draft_dir, "draft_cover.jpg")

    try:
        from PIL import Image as PILImage
    except ImportError:
        PILImage = None

    # ── 分支 1：用户直接给图 ──
    if cover_image and os.path.exists(cover_image):
        ext = os.path.splitext(cover_image)[1].lower()
        if ext in (".jpg", ".jpeg"):
            shutil.copy(cover_image, dst)  # copy 不保留源 mtime，目标文件天然"现在"
        else:
            if PILImage is None:
                print(f"  ⚠️ 需要 Pillow 把 {ext} 转 jpg，未安装，跳过封面")
                return False
            img = PILImage.open(cover_image).convert("RGB")
            img.save(dst, "JPEG", quality=96)
        print(f"  🖼️ draft_cover.jpg: {os.path.basename(cover_image)} "
              f"→ {os.path.getsize(dst)/1024:.0f}KB")
        return True

    # ── 分支 2：从视频抽帧 ──
    if not (extract_from_video and os.path.exists(extract_from_video)):
        print(f"  ⚠️ 无可用封面来源")
        return False

    ffmpeg = _guess_ffmpeg()
    if ffmpeg is None:
        print(f"  ⚠️ 找不到 ffmpeg，无法从视频抽帧做封面")
        return False

    if smart_select:
        filter_expr = (
            f"select=gt(scene\\,{scene_threshold}),"
            f"thumbnail=n={thumbnail_batch}"
        )
        try:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{max(0.0, float(video_start_s))}",
                 "-i", extract_from_video,
                 "-vf", filter_expr,
                 "-frames:v", "1", "-q:v", "2", dst],
                check=True,
            )
        except subprocess.CalledProcessError as e:
            print(f"  ⚠️ smart-select 失败({e.returncode})，回退固定时间抽帧 @3s")
            smart_select = False
            extract_time_s = 3.0

    if not smart_select:
        t = max(0.0, float(video_start_s)) + max(0.0, float(extract_time_s or 3.0))
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t}", "-i", extract_from_video,
             "-frames:v", "1", "-q:v", "2", dst],
            check=True,
        )

    print(f"  🖼️ draft_cover.jpg: {'smart-select' if smart_select else f'@{extract_time_s}s'} "
          f"{os.path.basename(extract_from_video)} → {os.path.getsize(dst)/1024:.0f}KB")
    return True


def set_cover_composition(draft, draft_dir, cover_image):
    """为草稿创建剪映原生封面结构（composition 材质）。

    仅写 draft_cover.jpg 不够——剪映 5.9 打开草稿时若顶层 cover 字段为 None，
    会用第一帧覆盖 draft_cover.jpg。必须同时：
      1) 把封面图复制到 <草稿>/Resources/cover/<uuid>.<ext>
      2) 在 materials.drafts 里建一个 type=composition 的材质，内嵌一个含
         photo 视频段的 mini-draft 指向该图
      3) 顶层 cover 字段设为 {cover_draft_id, type:'image', sub_type:'local'}
    """
    if not (cover_image and os.path.exists(cover_image)):
        return False
    ext = os.path.splitext(cover_image)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".bmp", ".webp"):
        print(f"  ⚠️ 封面格式 {ext} 不受支持，跳过 composition")
        return False

    # 1) 复制封面图到 Resources/cover/，用 copy 不保留源 mtime，避免被剪映按"过期资源"清理
    cover_dir = os.path.join(draft_dir, "Resources", "cover")
    os.makedirs(cover_dir, exist_ok=True)

    # 先摘掉旧的封面 composition（重复应用时不叠加，避免残留多个）
    old_cover = draft.get("cover") or {}
    old_id = old_cover.get("cover_draft_id")
    mats0 = draft.get("materials") or {}
    if old_id and isinstance(mats0.get("drafts"), list):
        mats0["drafts"] = [x for x in mats0["drafts"]
                          if not (x.get("type") == "composition" and x.get("id") == old_id)]

    img_uuid = gen_uuid()
    dst_name = f"{img_uuid}{ext}"
    dst_path = os.path.join(cover_dir, dst_name)
    shutil.copy(cover_image, dst_path)

    # 读取图片尺寸
    try:
        from PIL import Image as PILImage
        with PILImage.open(cover_image) as _im:
            img_w, img_h = _im.size
    except Exception:
        img_w, img_h = 1080, 1920

    # 2) 生成各组件 UUID
    comp_id = gen_uuid()
    comp_mat_id = f"{comp_id}_material"
    video_mat_id = gen_uuid()
    track_id = gen_uuid()
    seg_id = gen_uuid()
    canvas_id = gen_uuid()
    speed_id = gen_uuid()
    scm_id = gen_uuid()
    vs_id = gen_uuid()

    # 用绝对路径（正斜杠）：与草稿内其它已能正常加载的素材保持一致，
    # 避免 ##_draftpath_placeholder## 解析不到导致封面渲染成黑帧
    rel_path = os.path.abspath(dst_path).replace("\\", "/")

    # 内嵌 photo 视频材质
    photo_mat = {
        "aigc_type": "none", "audio_fade": None, "cartoon_path": "",
        "category_id": "", "category_name": "", "check_flag": 63487,
        "crop": {"lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0,
                 "lower_right_y": 1.0, "upper_left_x": 0.0, "upper_left_y": 0.0,
                 "upper_right_x": 1.0, "upper_right_y": 0.0},
        "crop_ratio": "free", "crop_scale": 1.0, "duration": 10800000000,
        "extra_type_option": 0, "formula_id": "", "freeze": None,
        "has_audio": False, "height": img_h, "id": video_mat_id,
        "intensifies_audio_path": "", "intensifies_path": "",
        "is_ai_generate_content": False, "is_copyright": False,
        "is_text_edit_overdub": False, "is_unified_beauty_mode": False,
        "local_id": "", "local_material_id": "", "material_id": "",
        "material_name": "", "material_url": "",
        "matting": {"flag": 0, "has_use_quick_brush": False,
                    "has_use_quick_eraser": False, "interactiveTime": [],
                    "path": "", "strokes": []},
        "media_path": "", "object_locked": None, "origin_material_id": "",
        "path": rel_path, "picture_from": "none",
        "picture_set_category_id": "", "picture_set_category_name": "",
        "request_id": "", "reverse_intensifies_path": "", "reverse_path": "",
        "smart_motion": None, "source": 0, "source_platform": 0,
        "stable": {"matrix_path": "", "stable_level": 0,
                   "time_range": {"duration": 0, "start": 0}},
        "team_id": "", "type": "photo",
        "video_algorithm": {"algorithms": [], "complement_frame_config": None,
                            "deflicker": None, "gameplay_configs": [],
                            "motion_blur_config": None, "noise_reduction": None,
                            "path": "", "quality_enhance": None, "time_range": None},
        "width": img_w,
    }

    # 内嵌 track + segment
    seg = {
        "caption_info": None, "cartoon": False,
        "clip": {"alpha": 1.0, "flip": {"horizontal": False, "vertical": False},
                 "rotation": 0.0, "scale": {"x": 1.0, "y": 1.0},
                 "transform": {"x": 0.0, "y": 0.0}},
        "common_keyframes": [], "enable_adjust": True,
        "enable_color_correct_adjust": False, "enable_color_curves": True,
        "enable_color_match_adjust": False, "enable_color_wheels": True,
        "enable_lut": True, "enable_smart_color_adjust": False,
        "extra_material_refs": [speed_id, canvas_id, scm_id, vs_id],
        "group_id": "", "hdr_settings": None, "id": seg_id,
        "intensifies_audio": False, "is_placeholder": False,
        "is_tone_modify": False, "keyframe_refs": [],
        "last_nonzero_volume": 1.0, "material_id": video_mat_id,
        "render_index": 101,
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                              "size_layout": 0, "target_follow": "",
                              "vertical_pos_layout": 0},
        "reverse": False,
        "source_timerange": {"duration": 3000000, "start": 0},
        "speed": 1.0,
        # 封面画布是从 0 帧开始渲染的：片段必须落在时间线起点，
        # 否则第 0 帧没有任何可见素材 → 剪映生成纯黑封面
        "target_timerange": {"duration": 3000000, "start": 0},
        "template_id": "", "template_scene": "default", "track_attribute": 0,
        "track_render_index": 101,
        "uniform_scale": {"on": True, "value": 1.0},
        "visible": True, "volume": 1.0,
    }
    inner_track = {
        "attribute": 0, "flag": 0, "id": track_id,
        "is_default_name": True, "name": "", "segments": [seg], "type": "video",
    }

    inner_mats = {
        "ai_translates": [], "audio_balances": [], "audio_effects": [],
        "audio_fades": [], "audio_track_indexes": [], "audios": [], "beats": [],
        "canvases": [{"album_image": "", "blur": 0.0, "color": "", "id": canvas_id,
                      "image": "", "image_id": "", "image_name": "",
                      "source_platform": 0, "team_id": "", "type": "canvas_color"}],
        "chromas": [], "color_curves": [], "digital_humans": [], "drafts": [],
        "effects": [], "flowers": [], "green_screens": [], "handwrites": [],
        "hsl": [], "images": [], "log_color_wheels": [], "loudnesses": [],
        "manual_deformations": [], "masks": [], "material_animations": [],
        "material_colors": [], "multi_language_refs": [], "placeholders": [],
        "plugin_effects": [], "primary_color_wheels": [], "realtime_denoises": [],
        "shapes": [], "smart_crops": [], "smart_relights": [],
        "sound_channel_mappings": [{"audio_channel_mapping": 0, "id": scm_id,
                                   "is_config_open": False, "type": ""}],
        "speeds": [{"curve_speed": None, "id": speed_id, "mode": 0,
                    "speed": 1.0, "type": "speed"}],
        "stickers": [], "tail_leaders": [], "text_templates": [], "texts": [],
        "time_marks": [], "transitions": [], "video_effects": [],
        "video_trackings": [], "videos": [photo_mat], "vocal_beautifys": [],
        "vocal_separations": [{"choice": 0, "id": vs_id, "production_path": "",
                              "time_range": None, "type": "vocal_separation"}],
    }

    comp_draft = {
        # 画布尺寸必须与封面图一致；0x0 会让剪映渲染出空白/黑帧
        "canvas_config": {"height": img_h, "ratio": "original", "width": img_w},
        "color_space": -1,
        "config": {
            "adjust_max_index": 1, "attachment_info": [], "combination_max_index": 1,
            "export_range": None, "extract_audio_last_index": 1,
            "lyrics_recognition_id": "", "lyrics_sync": True, "lyrics_taskinfo": [],
            "maintrack_adsorb": True, "material_save_mode": 0,
            "multi_language_current": "none", "multi_language_list": [],
            "multi_language_main": "none", "multi_language_mode": "none",
            "original_sound_last_index": 1, "record_audio_last_index": 1,
            "sticker_max_index": 14000, "subtitle_keywords_config": None,
            "subtitle_recognition_id": "", "subtitle_sync": True,
            "subtitle_taskinfo": [], "system_font_list": [], "video_mute": False,
            "zoom_info_params": None,
        },
        "cover": None, "create_time": 0, "duration": 3000000, "extra_info": None,
        "fps": 30.0, "free_render_index_mode_on": False, "group_container": None,
        "id": comp_id,
        "keyframe_graph_list": [],
        "keyframes": {"adjusts": [], "audios": [], "effects": [], "filters": [],
                      "handwrites": [], "stickers": [], "texts": [], "videos": []},
        "last_modified_platform": {"app_id": 0, "app_source": "", "app_version": "",
                                   "device_id": "", "hard_disk_id": "",
                                   "mac_address": "", "os": "", "os_version": ""},
        "materials": inner_mats,
        "mutable_config": None, "name": "", "new_version": "110.0.0",
        "platform": {"app_id": 0, "app_source": "", "app_version": "",
                     "device_id": "", "hard_disk_id": "", "mac_address": "",
                     "os": "", "os_version": ""},
        "relationships": [], "render_index_track_mode_on": False,
        "retouch_cover": None, "source": "default",
        "static_cover_image_path": "", "time_marks": None,
        "tracks": [inner_track], "update_time": 0, "version": 360000,
    }

    comp_mat = {
        "category_id": "", "category_name": "", "combination_id": "",
        "draft": comp_draft, "formula_id": "", "id": comp_mat_id, "name": "",
        "precompile_combination": False, "type": "composition",
    }

    # 3) 挂到 materials.drafts
    mats = draft.setdefault("materials", {})
    drafts_list = mats.setdefault("drafts", [])
    drafts_list.append(comp_mat)

    # 4) 顶层 cover 字段
    draft["cover"] = {
        "cover_draft_id": comp_mat_id,
        "cover_template": None,
        "sub_type": "local",
        "type": "image",
        "web_cover_info": None,
    }
    print(f"  🧩 composition 已挂载: {os.path.basename(cover_image)} → {dst_name}")
    return True
