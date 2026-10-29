"""Drama Cover Builder - 短剧封面生成 Skill 主脚本。

子命令:
  title             生成 3 版标题（Agent 调用，Ollama 离线兜底）
  art-from-video    ffmpeg select+thumbnail 从视频抽最佳帧
  compose-pillow    Pillow 排版合成（4 样式 × 2 尺寸族）
  write-draft       写剪映草稿（draft_cover.jpg + 原生 composition）
  full              一键全链路 title → art → compose → write-draft
"""
import os, sys, json, uuid, shutil, subprocess, glob, argparse
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# 内部辅助
# ──────────────────────────────────────────────────────────────────

def _guess_ffmpeg():
    """三重查找 ffmpeg 路径（which → AI_Deps → WinGet）。"""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    # AI_Deps 硬编码路径
    p = r"D:\AI_Deps\tools\ffmpeg\bin\ffmpeg.exe"
    if os.path.isfile(p):
        return p
    # WinGet 路径
    for w in glob.glob(os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
            "Packages", "*ffmpeg*", "ffmpeg-*", "bin",
    )):
        fp = os.path.join(w, "ffmpeg.exe")
        if os.path.isfile(fp):
            return fp
    return None


def _guess_ffprobe():
    fp = shutil.which("ffprobe")
    if fp:
        return fp
    p = r"D:\AI_Deps\tools\ffmpeg\bin\ffprobe.exe"
    if os.path.isfile(p):
        return p
    return None


def _guess_font():
    """候选中文字体（Windows 系统自带）。"""
    candidates = [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


# ──────────────────────────────────────────────────────────────────
# 从 pipeline.py 搬入的封面写入能力（skill 独立版本）
# ──────────────────────────────────────────────────────────────────

def gen_uuid():
    return str(uuid.uuid4()).upper()


_DRAFTPATH_PLACEHOLDER = "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"


def set_draft_cover(draft_dir, cover_image=None, extract_from_video=None,
                    extract_time_s=None, smart_select=True,
                    scene_threshold=0.35, thumbnail_batch=500,
                    video_start_s=0.0):
    """设置草稿封面（draft_cover.jpg）。搬自 pipeline.py L267-362。"""
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
            shutil.copy2(cover_image, dst)
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
    """为草稿创建剪映原生封面结构（composition 材质）。搬自 pipeline.py L369-563。

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

    # 1) 复制封面图到 Resources/cover/
    cover_dir = os.path.join(draft_dir, "Resources", "cover")
    os.makedirs(cover_dir, exist_ok=True)
    img_uuid = gen_uuid()
    dst_name = f"{img_uuid}{ext}"
    dst_path = os.path.join(cover_dir, dst_name)
    shutil.copy2(cover_image, dst_path)

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

    rel_path = f"{_DRAFTPATH_PLACEHOLDER}/Resources/cover/{dst_name}"

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
        "target_timerange": {"duration": 3000000, "start": 7207200000},
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
        "canvas_config": {"height": 0, "ratio": "original", "width": 0},
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
        "cover": None, "create_time": 0, "duration": 0, "extra_info": None,
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


def _cover_start_for_cut(video_path, cut_us):
    """裁点对应的封面分析起点（秒）。搬自 pipeline.py L1115-1135。"""
    if not cut_us:
        return 0.0
    cut_s = cut_us / 1_000_000
    try:
        ffprobe = _guess_ffprobe()
        if ffprobe:
            out = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", video_path],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout.strip()
            dur = float(out)
            if dur > 0:
                return max(0.0, min(cut_s, dur - 1.0))
    except Exception:
        pass
    return cut_s


def save_draft(draft, path):
    """把 draft dict 写回 JSON。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)


def load_draft(path):
    """从 JSON 读 draft dict。"""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ──────────────────────────────────────────────────────────────────
# 新增辅助：自动探测 / 冲突点抽帧
# ──────────────────────────────────────────────────────────────────

def detect_cover_from_dir(series_dir):
    """从剧集目录自动探测封面图。（batch_runner.py L186-201 泛化）"""
    for name in ("封面.png", "封面.jpg", "封面.jpeg", "封面.webp"):
        p = os.path.join(series_dir, name)
        if os.path.isfile(p):
            return p
    imgs = sorted(
        f for f in os.listdir(series_dir)
        if os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        and os.path.isfile(os.path.join(series_dir, f))
    )
    if imgs:
        return os.path.join(series_dir, imgs[0])
    return None


def extract_at_conflict_point(video_path, conflict_time_s=None,
                               peak_json_path=None, offset_before_s=0.25,
                               out_path=None):
    """从冲突点抽帧。save_covers.py 泛化版本。"""
    if conflict_time_s is None and peak_json_path:
        try:
            with open(peak_json_path, encoding="utf-8") as f:
                peak_data = json.load(f)
            # 支持 {"conflict_time": 12.3} 或 episode dict
            if isinstance(peak_data, dict) and "conflict_time" in peak_data:
                conflict_time_s = peak_data["conflict_time"]
        except Exception:
            pass
    if not conflict_time_s:
        print(f"  ⚠️ 无冲突时间点，跳过抽帧")
        return None

    ffmpeg = _guess_ffmpeg()
    if not ffmpeg:
        print(f"  ⚠️ 找不到 ffmpeg")
        return None

    target_t = max(0.0, conflict_time_s - offset_before_s)
    if out_path is None:
        out_path = os.path.join(
            os.path.dirname(video_path) or ".",
            f"conflict_cover_{int(target_t*1000)}ms.jpg"
        )
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{target_t:.2f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_path],
            check=True,
        )
        print(f"  🎯 冲突点封面: @{target_t:.1f}s → {out_path}")
        return out_path
    except Exception as e:
        print(f"  ⚠️ 冲突点抽帧失败: {e}")
        return None


# ──────────────────────────────────────────────────────────────────
# Pillow 合成
# ──────────────────────────────────────────────────────────────────

def _load_styles():
    styles_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "assets", "compose_styles", "pillow_styles.json"
    )
    with open(os.path.normpath(styles_path), encoding="utf-8") as f:
        return json.load(f)


def compose_cover(base_image, out_path, width=None, height=None,
                  title=None, style="classic_red_stroke",
                  size_family="vertical"):
    """合成一张封面图。"""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    styles_cfg = _load_styles()
    canvas_cfg = styles_cfg["canvas"][size_family]
    if width is None:
        width = canvas_cfg["width"]
    if height is None:
        height = canvas_cfg["height"]

    # 底图覆盖 + 裁切
    img = Image.open(base_image).convert("RGB")
    # 中心裁切到目标比例
    src_ratio = img.width / img.height
    tgt_ratio = width / height
    if src_ratio > tgt_ratio:
        new_w = int(img.height * tgt_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:
        new_h = int(img.width / tgt_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))
    img = img.resize((width, height), Image.LANCZOS)

    # 蒙版（有标题时加）
    has_title = bool(title and title.strip())
    style_cfg = styles_cfg["styles"].get(style, styles_cfg["styles"]["classic_red_stroke"])

    if has_title:
        mask_cfg = style_cfg.get("mask") or {}
        if mask_cfg.get("type") == "bottom":
            mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            draw = ImageDraw.Draw(mask)
            m_color = mask_cfg.get("color", "#000000")
            m_opacity = int(mask_cfg.get("opacity", 0.55) * 255)
            gradient = mask_cfg.get("gradient", True)
            if gradient:
                # 渐变：顶部透明 → 底部 m_opacity
                band_h = int(height * styles_cfg["title_safe_area"][size_family]["top_ratio"])
                for y in range(height - band_h, height):
                    alpha = int((y - (height - band_h)) / band_h * m_opacity)
                    draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
            else:
                band_h = int(height * (1 - styles_cfg["title_safe_area"][size_family]["top_ratio"]))
                draw.rectangle([0, height - band_h, width, height],
                               fill=(0, 0, 0, m_opacity))
            img = Image.alpha_composite(img.convert("RGBA"), mask).convert("RGB")

    # 标题绘制
    if has_title:
        img = _draw_title(img, title, style_cfg, styles_cfg, size_family)

    img.save(out_path, "JPEG", quality=92)
    return out_path


def _draw_title(img, title, style_cfg, styles_cfg, size_family):
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

    W, H = img.size
    safe = styles_cfg["title_safe_area"][size_family]
    top_y = int(H * safe["top_ratio"])
    side_margin = int(W * safe["side_margin_ratio"])
    max_text_w = W - side_margin * 2
    max_text_lines = 3

    # 字号自动适配：从估算值递减，直到标题完整放下（≤max_text_lines 行且不超出标题区）
    is_vert = size_family == "vertical"
    font_path = _guess_font()
    draw = ImageDraw.Draw(img)

    # 自动换行
    def _wrap(text, font, max_w):
        lines, cur = [], ""
        for ch in text:
            test = cur + ch
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] > max_w and cur:
                lines.append(cur); cur = ch
            else:
                cur = test
        if cur: lines.append(cur)
        return lines

    avail_h = H - top_y - int(H * 0.04)
    font_size = int(W / (14 if is_vert else 10))
    font = ImageFont.load_default()
    lines = []
    while font_size > 16:
        try:
            font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        line_h = font_size + max(4, int(font_size * 0.12))
        lines = _wrap(title, font, max_text_w)
        if len(lines) <= max_text_lines and line_h * len(lines) <= avail_h:
            break
        font_size = int(font_size * 0.85)
    else:
        lines = lines[:max_text_lines]
    if not lines:
        return img

    line_h = font_size + max(4, int(font_size * 0.12))
    total_h = line_h * len(lines)
    start_y = top_y
    # 标题居中
    start_x = (W - max_text_w) // 2

    text_color = style_cfg.get("text_color", "#FFFFFF")
    stroke_color = style_cfg.get("stroke_color", "#D90404")
    stroke_w = style_cfg.get("stroke_width", 6)

    # neon_glow：先铺模糊发光层（必须在文字底下，否则会糊住正文）
    glow_cfg = style_cfg.get("glow_color")
    if glow_cfg and style_cfg.get("glow_radius"):
        glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        for i, line in enumerate(lines):
            line_w = draw.textbbox((0, 0), line, font=font)[2]
            x = (W - line_w) // 2
            y = start_y + i * line_h
            gd.text((x, y), line, font=font, fill=glow_cfg,
                    stroke_width=stroke_w, stroke_fill=glow_cfg)
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=style_cfg["glow_radius"]))
        img = Image.alpha_composite(img.convert("RGBA"), glow_layer).convert("RGB")
        draw = ImageDraw.Draw(img)  # 合成后重建 draw，指向新 img

    for i, line in enumerate(lines):
        line_w = draw.textbbox((0, 0), line, font=font)[2]
        x = (W - line_w) // 2
        y = start_y + i * line_h

        # multi-shadow（shadow_outline 风格）
        if "shadow_offsets" in style_cfg:
            for off in style_cfg["shadow_offsets"]:
                draw.text((x + off[0], y + off[1]), line,
                          font=font, fill=style_cfg.get("shadow_color", "#000000"))

        # stroke + text（PIL 10+ stroke_width 内建）
        draw.text((x, y), line, font=font, fill=text_color,
                  stroke_width=stroke_w, stroke_fill=stroke_color)

    # bottom_tape：红布条
    tape_cfg = style_cfg.get("tape")
    if tape_cfg:
        tape_h = int(H * tape_cfg.get("height_ratio", 0.12))
        tape_y = H - tape_h - int(H * tape_cfg.get("padding_ratio", 0.03))
        tape_draw = ImageDraw.Draw(img)
        tape_draw.rectangle([side_margin, tape_y, W - side_margin, tape_y + tape_h],
                            fill=tape_cfg.get("color", "#D90404"))

    return img


def compose_full_set(base_image, out_dir, titles=None, styles_cfg=None):
    """一键生成 4 张封面：3 横(hit/revenge/legend 对应 classic_red_stroke / shadow_outline / neon_glow) + 1 竖。"""
    os.makedirs(out_dir, exist_ok=True)
    mapping = [
        ("hit",     "horizontal", "classic_red_stroke"),
        ("revenge", "horizontal", "shadow_outline"),
        ("legend",  "horizontal", "neon_glow"),
        ("vertical","vertical",   "bottom_tape"),
    ]
    results = {}
    for key, size_family, style in mapping:
        t = (titles or {}).get(key) or (titles or {}).get("default")
        t_label = "_" + key if t else ""
        out = os.path.join(out_dir, f"cover_{key}{t_label}.jpg")
        compose_cover(base_image, out, title=t, style=style, size_family=size_family)
        results[key] = out
        print(f"  ✅ {key}: {out}")
    return results


# ──────────────────────────────────────────────────────────────────
# CLI 子命令
# ──────────────────────────────────────────────────────────────────

def _cmd_art_from_video(args):
    out = args.out or "best_frame.jpg"
    set_draft_cover(
        draft_dir=os.path.dirname(out) or ".",
        extract_from_video=args.video,
        smart_select=True,
        scene_threshold=args.scene_threshold,
        thumbnail_batch=args.thumbnail_batch,
        extract_time_s=args.fallback_time,
    )
    # art-from-video 的语义是输出一张图片，不是写 draft_cover.jpg
    # 但 set_draft_cover 的 dst 固定是 draft_cover.jpg
    # 改：直接调 ffmpeg 输出到 out
    ffmpeg = _guess_ffmpeg()
    if not ffmpeg:
        print("✗ 找不到 ffmpeg"); return 1
    filter_expr = (f"select=gt(scene\\,{args.scene_threshold}),"
                   f"thumbnail=n={args.thumbnail_batch}")
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", args.video, "-vf", filter_expr,
             "-frames:v", "1", "-q:v", "2", out], check=True)
        print(f"✅ best_frame: {out}")
        return 0
    except subprocess.CalledProcessError:
        t = args.fallback_time or 3.0
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t}", "-i", args.video,
             "-frames:v", "1", "-q:v", "2", out], check=True)
        print(f"✅ best_frame (fallback @{t}s): {out}")
        return 0


def _cmd_compose_pillow(args):
    if args.style == "all":
        out_dir = args.out or "./composed"
        title_map = {"hit": args.title, "revenge": args.title,
                     "legend": args.title, "vertical": args.title}
        # size 由 style 决定，不走 compose_full_set 但类似
        return _cmd_compose_all(args)
    # 单张
    size_fam = "horizontal" if args.size == "horizontal" else "vertical"
    compose_cover(args.base, args.out or "cover.jpg",
                  title=args.title, style=args.style, size_family=size_fam)
    print(f"✅ composed: {args.out or 'cover.jpg'}")
    return 0


def _cmd_compose_all(args):
    out_dir = args.out or "./composed"
    title_map = {"hit": args.title, "revenge": args.title,
                 "legend": args.title, "vertical": args.title}
    compose_full_set(args.base, out_dir, titles=title_map)
    print(f"✅ all composed → {out_dir}")
    return 0


def _cmd_write_draft(args):
    draft_path = os.path.join(args.draft_dir, "draft_content.json")
    if not os.path.isfile(draft_path):
        print(f"✗ 找不到 draft_content.json: {draft_path}"); return 1
    draft = load_draft(draft_path)

    # Step 1: draft_cover.jpg
    set_draft_cover(args.draft_dir, cover_image=args.cover)
    # Step 2: 原生 composition
    set_cover_composition(draft, args.draft_dir, args.cover)
    # Step 3: 写回
    save_draft(draft, draft_path)
    print(f"✅ 已写入草稿: {args.draft_dir}")
    return 0


def _cmd_title(args):
    spec_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "references", "title-spec.md"
    )
    print(f"📋 标题规范: {os.path.normpath(spec_path)}")
    print()
    print("Agent 调用模式：请将 synopsis + 上面规范一起发给 Trae 在线模型。")
    print("离线 Ollama 兜底（需本地 qwen2.5:7b）:")
    # ... Ollama 实现可后续加
    return 0


def _cmd_full(args):
    print("=== Drama Cover Builder Full ===")
    base = args.base_image
    if not base and args.video:
        out_dir = args.out or "./cover_out"
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, "best_frame.jpg")
        _cmd_art_from_video(argparse.Namespace(
            video=args.video, out=base,
            scene_threshold=0.35, thumbnail_batch=500, fallback_time=3.0))
    if not base or not os.path.isfile(base):
        print("✗ 无可用底图（--base-image 或 --video 至少传一个）"); return 1

    out_dir = args.out or "./cover_out"
    os.makedirs(out_dir, exist_ok=True)
    compose_cover(base, os.path.join(out_dir, "cover_vertical.jpg"),
                  title=args.title, style="bottom_tape", size_family="vertical")
    print(f"✅ full → {out_dir}")
    if args.draft_dir:
        _cmd_write_draft(argparse.Namespace(
            cover=os.path.join(out_dir, "cover_vertical.jpg"),
            draft_dir=args.draft_dir))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="drama_cover.py",
        description="Drama Cover Builder — 短剧封面全链路生成")
    sub = ap.add_subparsers(dest="command", required=True)

    # --- title ---
    p_title = sub.add_parser("title", help="打印标题规范路径（12 条标题由 Agent 在线生成）")
    p_title.add_argument("--synopsis", nargs="?", default=None,
                         help="短剧简介文字或 .txt 路径（保留参数兼容旧调用，当前不参与生成）")
    p_title.add_argument("--out", default="./titles", help="输出目录")

    # --- art-from-video ---
    p_art = sub.add_parser("art-from-video", help="ffmpeg select+thumbnail 抽最佳帧")
    p_art.add_argument("--video", required=True)
    p_art.add_argument("--out", default="best_frame.jpg")
    p_art.add_argument("--scene-threshold", type=float, default=0.35)
    p_art.add_argument("--thumbnail-batch", type=int, default=500)
    p_art.add_argument("--fallback-time", type=float, default=3.0)

    # --- compose-pillow ---
    p_cp = sub.add_parser("compose-pillow", help="Pillow 排版合成")
    p_cp.add_argument("--base", required=True, help="底图路径")
    p_cp.add_argument("--title", default=None, help="标题文字（不传则无标题）")
    p_cp.add_argument("--out", default="cover.jpg", help="单张输出路径；--style all 时为目录")
    p_cp.add_argument("--style", default="classic_red_stroke",
                      choices=["classic_red_stroke", "shadow_outline",
                               "neon_glow", "bottom_tape", "all"])
    p_cp.add_argument("--size", default="vertical",
                      choices=["vertical", "horizontal"])

    # --- write-draft ---
    p_wd = sub.add_parser("write-draft", help="写剪映草稿（draft_cover.jpg + 原生 composition）")
    p_wd.add_argument("--cover", required=True, help="封面图片路径")
    p_wd.add_argument("--draft-dir", required=True, help="剪映草稿目录")

    # --- full ---
    p_full = sub.add_parser("full", help="一键全链路 title → art → compose → write-draft")
    p_full.add_argument("--synopsis", default=None)
    p_full.add_argument("--video", default=None)
    p_full.add_argument("--base-image", default=None)
    p_full.add_argument("--title", default=None)
    p_full.add_argument("--out", default="./cover_out")
    p_full.add_argument("--draft-dir", default=None)

    args = ap.parse_args()

    dispatch = {
        "title": _cmd_title,
        "art-from-video": _cmd_art_from_video,
        "compose-pillow": _cmd_compose_pillow,
        "write-draft": _cmd_write_draft,
        "full": _cmd_full,
    }
    rc = dispatch[args.command](args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
