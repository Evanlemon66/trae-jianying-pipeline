import sys, os, json, uuid, shutil, subprocess, time, re, pathlib, glob
from copy import deepcopy

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))
sys.path.insert(0, _HERE)

from export_config import get_deps_root, get_font_path

from utils.formatters import (
    get_default_drafts_root as _upstream_get_default_drafts_root,
    get_all_drafts as _upstream_get_all_drafts,
    get_duration_ffprobe_cached as _upstream_get_duration,
)
from utils.media_normalizer import should_normalize_video_for_jianying, normalize_video_for_jianying
from vendor.pyJianYingDraft.draft_folder import DraftFolder

sys.stdout.reconfigure(encoding="utf-8")


# ━━━━ 分步耗时统计（供流程优化分析）━━━━
import contextlib

_TIMINGS = []  # [(stage_name, seconds)]，run_pipeline 开始时清零


@contextlib.contextmanager
def timed_step(name):
    """记录一个步骤耗时：打印单行并累积到 _TIMINGS"""
    t0 = time.time()
    try:
        yield
    finally:
        dt = time.time() - t0
        _TIMINGS.append((name, dt))
        print(f"  ⏱️ [{name}] {dt:.1f}s")


def _timing_summary(workdir, tag=""):
    """按阶段聚合打印耗时表，并追加到 workdir/_timings.jsonl"""
    if not _TIMINGS:
        return
    agg = {}
    for name, dt in _TIMINGS:
        key = name.split("/")[0]
        a = agg.setdefault(key, [0.0, 0])
        a[0] += dt
        a[1] += 1
    total = sum(dt for _, dt in _TIMINGS)
    print(f"\n{'='*52}\n  ⏱️ 分步耗时统计（总计 {total/60:.1f} 分钟）\n{'='*52}")
    for key, (s, n) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        avg = s / n
        print(f"  {key:<10} 合计 {s:>7.1f}s  ×{n:<3} 均值 {avg:>6.1f}s  占比 {s/total*100:>5.1f}%")
    print("=" * 52)
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag,
            "total_s": round(total, 1),
            "stages": {k: {"total_s": round(v[0], 1), "count": v[1]} for k, v in agg.items()},
            "detail": [[n, round(d, 1)] for n, d in _TIMINGS],
        }
        with open(os.path.join(workdir, "_timings.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _find_ffmpeg():
    """定位 ffmpeg.exe：优先 D:/AI_Deps（统一依赖目录）→ WinGet → PATH。"""
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    import glob as _g
    candidates = [
        os.path.join(get_deps_root(), "tools", "ffmpeg", "bin", "ffmpeg.exe"),
        * _g.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                               "Packages", "*ffmpeg*", "ffmpeg-*", "bin", "ffmpeg.exe")),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def _ensure_ffmpeg_on_path():
    """把 ffmpeg/ffprobe 所在 bin 目录加入 PATH（ffprobe、demucs、videocaptioner 子进程都依赖）。"""
    if shutil.which("ffprobe") and shutil.which("ffmpeg"):
        return
    ff = _find_ffmpeg()
    if ff:
        os.environ["PATH"] = os.environ["PATH"] + os.pathsep + os.path.dirname(ff)


_ensure_ffmpeg_on_path()

# 模型统一存放在 deps_root/models（用户级环境变量 HF_HUB_CACHE/TORCH_HOME 已设置，此处兜底）
os.environ.setdefault("HF_HUB_CACHE", os.path.join(get_deps_root(), "models", "huggingface", "hub"))
os.environ.setdefault("TORCH_HOME", os.path.join(get_deps_root(), "models", "torch"))


def _load_env_profile():
    """读取 install_deps.ps1 生成的 _env_profile.json 机器画像（含 whisper/ollama 分级配置）。

    画像缺失或损坏时返回 {}，所有消费方必须能回退到历史默认值（向后兼容）。
    可用环境变量 JY_ENV_PROFILE 显式指定路径。
    """
    try:
        p = os.environ.get("JY_ENV_PROFILE", "").strip() or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "_env_profile.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8-sig") as f:  # PS5.1 Out-File utf8 带 BOM
                return json.load(f)
    except Exception:
        pass
    return {}


_ENV_PROFILE = _load_env_profile()

DRAFT_ROOT = _upstream_get_default_drafts_root()
# 草稿构建暂存区：放在剪映草稿目录之外（同盘），构建完成后整体搬到 DRAFT_ROOT。
# 剪映只扫描 DRAFT_ROOT，因此构建中的半成品永远不会被剪映看到/缓存。
_STAGING_ROOT = os.path.join(os.path.dirname(DRAFT_ROOT), "__jybuild__")
PRESET_ROOT = os.path.join(os.environ["LOCALAPPDATA"], "JianyingPro", "User Data", "Presets", "Combination", "Presets")

SEG_KEYS_WITH_ID = {"material_id", "group_id", "raw_segment_id", "render_index", "id"}
TRACK_KEYS_WITH_ID = {"id"}
MAT_KEYS_WITH_ID = {"id"}


def gen_uuid():
    return str(uuid.uuid4()).upper()


def register_draft_in_root_meta(draft_name, draft_id, duration_us=0):
    """往剪映全局索引 root_meta_info.json 注册草稿，规避剪映运行中重置陌生草稿。

    剪映在运行中检测到磁盘上新出现的草稿文件夹时，会查此索引；
    索引无记录则判定非法并重置 draft_content.json。注册后即视为合法草稿。
    若剪映把索引缓存在内存并在退出时覆盖磁盘，最坏情况退化为原行为，无副作用。
    """
    meta_path = os.path.join(DRAFT_ROOT, "root_meta_info.json")
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            root_meta = json.load(f)
        store = root_meta.setdefault("all_draft_store", [])
        now_us = int(time.time() * 1_000_000)
        root_slash = DRAFT_ROOT.replace("\\", "/").rstrip("/")
        # 已存在同名条目则更新时间戳，否则追加
        entry = next((e for e in store if e.get("draft_name") == draft_name), None)
        if entry is None:
            entry = {
                "draft_cloud_last_action_download": False,
                "draft_cloud_purchase_info": "",
                "draft_cloud_template_id": "",
                "draft_cloud_tutorial_info": "",
                "draft_cloud_videocut_purchase_info": "",
                "draft_cover": f"{root_slash}/{draft_name}\\draft_cover.jpg",
                "draft_fold_path": f"{root_slash}/{draft_name}",
                "draft_id": draft_id,
                "draft_is_ai_shorts": False,
                "draft_is_invisible": False,
                "draft_json_file": f"{root_slash}/{draft_name}\\draft_content.json",
                "draft_name": draft_name,
                "draft_new_version": "",
                "draft_root_path": DRAFT_ROOT,
                "draft_timeline_materials_size": 0,
                "draft_type": "",
                "tm_draft_cloud_completed": "",
                "tm_draft_cloud_modified": 0,
                "tm_draft_create": now_us,
                "tm_draft_modified": now_us,
                "tm_draft_removed": 0,
                "tm_duration": duration_us,
            }
            store.append(entry)
        else:
            entry["draft_id"] = draft_id
            entry["tm_duration"] = duration_us
            entry["draft_fold_path"] = f"{root_slash}/{draft_name}"
            entry["draft_json_file"] = f"{root_slash}/{draft_name}\\draft_content.json"
            entry["tm_draft_modified"] = now_us
            entry["tm_draft_removed"] = 0
            entry["draft_is_invisible"] = False
        root_meta["draft_ids"] = len(store)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(root_meta, f, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"  [warn] 注册剪映全局索引失败: {e}")
        return False


def publish_staged_draft(stage_dir, draft_name, duration_us=0):
    """把暂存区构建好的草稿整体搬进剪映草稿目录（原子发布）。

    剪映只扫描 DRAFT_ROOT，构建期间看不到半成品，避免「构建中被打开 → 缓存空模板 →
    后几集空白」的问题。同名草稿已存在时先改名移走，再放入新目录。
    """
    target = os.path.join(DRAFT_ROOT, draft_name)
    trash = None
    if os.path.exists(target):
        trash = target + f".removed_{time.strftime('%Y%m%d_%H%M%S')}"
        os.rename(target, trash)
    if not os.path.isdir(os.path.dirname(target)):
        os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.move(stage_dir, target)

    content_path = os.path.join(target, "draft_content.json")
    draft = {}
    try:
        with open(content_path, "r", encoding="utf-8") as f:
            draft = json.load(f)
        draft["name"] = draft_name
        draft["update_time"] = int(time.time() * 1_000_000)
        with open(content_path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False)
    except Exception as e:
        print(f"  [warn] 发布时更新 draft_content 失败: {e}")

    meta_path = os.path.join(target, "draft_meta_info.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            now_us = int(time.time() * 1_000_000)
            meta["draft_id"] = draft.get("id") or meta.get("draft_id")
            meta["draft_name"] = draft_name
            meta["draft_fold_path"] = target.replace("\\", "/")
            meta["tm_duration"] = duration_us or draft.get("duration") or 0
            meta["draft_timeline_materials_size_"] = os.path.getsize(content_path)
            if "draft_last_edit_time" in meta:
                meta["draft_last_edit_time"] = now_us
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except Exception as e:
            print(f"  [warn] 发布时更新 draft_meta_info 失败: {e}")

    register_draft_in_root_meta(
        draft_name, draft.get("id", ""),
        duration_us or draft.get("duration") or 0)

    print(f"🚀 草稿已发布到剪映目录: {target}")
    if _jianying_running():
        print("⚠️  剪映正在运行：若该草稿此刻处于打开状态，请先关闭该草稿再刷新草稿列表")
    if trash:
        shutil.rmtree(trash, ignore_errors=True)
    # 发布成功后清理暂存区空壳（move 已搬走本体，残留的是封面/配置等小文件壳目录）
    if os.path.isdir(stage_dir):
        shutil.rmtree(stage_dir, ignore_errors=True)
    return target, content_path


def _pipeline_backup_summary(draft):
    """备份摘要：video/audio 各轨 segment 数，供导出前快速比对是否被剪映合并。"""
    parts = []
    for t in draft.get("tracks", []):
        if t.get("type") in ("video", "audio"):
            n = len(t.get("segments") or [])
            parts.append(f"{t['type']}={n}")
    return ", ".join(parts[:4])


def _write_pipeline_backup(draft_path, draft):
    """写 _pipeline_backup.json：防止剪映打开草稿时自动合并串流 video segments
    （把 117 个首尾相连的集合成 1 段大段，丢失 crop 后的 source_timerange.duration）。

    新建草稿（staged）与注入已有草稿（直接 save）两条发布路径都要走这里。
    """
    try:
        _bk = os.path.join(os.path.dirname(draft_path), "_pipeline_backup.json")
        shutil.copy2(draft_path, _bk)
        print(f"  🛡️ 草稿已备份: {_pipeline_backup_summary(draft)}")
    except Exception as _be:
        print(f"  [warn] 备份 _pipeline_backup.json 失败: {_be}")


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


def set_draft_cover(draft_dir, cover_image=None, extract_from_video=None,
                    extract_time_s=None, smart_select=True,
                    scene_threshold=0.35, thumbnail_batch=500,
                    video_start_s=0.0):
    """设置草稿封面（draft_cover.jpg）。

    Args:
        draft_dir:  草稿目录（里面应存在 draft_content.json）。
        cover_image: 可选，本地图片路径（jpg/png/bmp/webp 等）。传入则直接转码为 jpg 覆盖封面。
        extract_from_video: 可选，视频路径；cover_image 未传时从该视频抽帧当封面。
        extract_time_s: 抽帧时间（秒）。仅 smart_select=False 时生效。
        smart_select: True（默认）使用 ffmpeg thumbnail + select 场景变换帧，
                      自动挑选全片最具代表性的帧（避开片头/黑场/模糊）。
                      False 则硬抽 extract_time_s 处。
        scene_threshold: select 滤镜的场景变化阈值（0-1），越高越严格。默认 0.35。
        thumbnail_batch: thumbnail 滤镜每批处理帧数上限，默认 500（足够覆盖一部短剧）。
        video_start_s: 分析起点（秒）。裁剪场景传裁点，使封面不从被裁掉的开头取帧。
    """
    import shutil as _sh
    import subprocess as _sp
    import glob as _glob

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
            _sh.copy2(cover_image, dst)
        else:
            if PILImage is None:
                print(f"  ⚠️ 需要 Pillow 把 {ext} 转 jpg，未安装，跳过封面")
                return False
            img = PILImage.open(cover_image).convert("RGB")
            img.save(dst, "JPEG", quality=96)
        print(f"  🖼️ 封面: {'复制本地图' if ext in ('.jpg','.jpeg') else 'PIL 转码'} "
              f"{os.path.basename(cover_image)} → {os.path.getsize(dst)/1024:.0f}KB")
        return True

    # ── 分支 2：从视频抽帧 ──
    if not (extract_from_video and os.path.exists(extract_from_video)):
        print(f"  ⚠️ 无可用封面来源")
        return False

    ffmpeg = _sh.which("ffmpeg")
    if ffmpeg is None:
        for p in _glob.glob(os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages",
            "*ffmpeg*", "ffmpeg-*", "bin",
        )):
            ffmpeg = os.path.join(p, "ffmpeg.exe")
            break
    if ffmpeg is None:
        print(f"  ⚠️ 找不到 ffmpeg，无法从视频抽帧做封面")
        return False

    if smart_select:
        # 方案 B：select 筛掉静止帧 → thumbnail 挑全片最具代表性的一帧
        filter_expr = (
            f"select=gt(scene\\,{scene_threshold}),"
            f"thumbnail=n={thumbnail_batch}"
        )
        try:
            _sp.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-ss", f"{max(0.0, float(video_start_s))}",
                 "-i", extract_from_video,
                 "-vf", filter_expr,
                 "-frames:v", "1", "-q:v", "2", dst],
                check=True,
            )
        except _sp.CalledProcessError as e:
            print(f"  ⚠️ smart-select 失败({e.returncode})，回退到固定时间抽帧 @3s")
            smart_select = False
            extract_time_s = 3.0

    if not smart_select:
        t = max(0.0, float(video_start_s)) + max(0.0, float(extract_time_s or 3.0))
        _sp.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{t}", "-i", extract_from_video,
             "-frames:v", "1", "-q:v", "2", dst],
            check=True,
        )

    print(f"  🖼️ 封面: {'smart-select' if smart_select else f'@{extract_time_s}s'}"
          f"{f' (起点 {video_start_s:.2f}s)' if video_start_s else ''} "
          f"{os.path.basename(extract_from_video)} → {os.path.getsize(dst)/1024:.0f}KB")
    return True


# 剪映草稿路径占位符（与 vocal_separation production_path 同款，运行时解析为草稿根目录）
_DRAFTPATH_PLACEHOLDER = "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"


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
    import shutil as _sh
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
    _sh.copy2(cover_image, dst_path)

    # 读取图片尺寸
    try:
        from PIL import Image as PILImage
        with PILImage.open(cover_image) as _im:
            img_w, img_h = _im.size
    except Exception:
        img_w, img_h = 1080, 1920

    # 2) 生成各组件 UUID
    comp_id = gen_uuid()                       # composition draft 的 id
    comp_mat_id = f"{comp_id}_material"        # materials.drafts 里的 id
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

    # 完整 materials 字典（空列表 + 4 个有内容的）
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
    print(f"  🧩 封面 composition 已挂载: {os.path.basename(cover_image)} → {dst_name}")
    return True


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
    for d in _upstream_get_all_drafts():
        dp = d["path"]
        if d["name"] == name or os.path.basename(dp) == name:
            jp = os.path.join(dp, "draft_content.json")
            meta = {}
            if os.path.exists(jp):
                try:
                    with open(jp, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass
            return dp, jp, meta
    return None, None, None


def create_empty_draft(draft_name, template_draft_name=None, register=True, staging=False):
    if template_draft_name is None:
        for candidate in ("default", "pipeline_test", "demo_test"):
            dp, jp, meta = find_draft_by_name(candidate)
            if dp:
                template_draft_name = candidate
                break
    if template_draft_name is None:
        os.makedirs(DRAFT_ROOT, exist_ok=True)
        return None, None, None

    # ── 暂存区构建：不进入剪映草稿目录、不登记索引，等全部写完再整体发布 ──
    if staging:
        dp_root, jp, _ = find_draft_by_name(template_draft_name)
        if not dp_root:
            return None, None, None
        with open(jp, "r", encoding="utf-8") as f:
            template = json.load(f)
        draft = reset_draft_to_clean(deepcopy(template))
        now_us = int(time.time() * 1_000_000)
        draft["id"] = gen_uuid()
        draft["name"] = draft_name
        draft["create_time"] = now_us
        draft["update_time"] = now_us

        os.makedirs(_STAGING_ROOT, exist_ok=True)
        new_dir = os.path.join(_STAGING_ROOT, f"{draft_name}__{gen_uuid()[:8]}")
        if os.path.exists(new_dir):
            shutil.rmtree(new_dir, ignore_errors=True)
        shutil.copytree(dp_root, new_dir)
        jp_new = os.path.join(new_dir, "draft_content.json")
        save_draft(draft, jp_new)
        for junk in os.listdir(new_dir):
            if junk.startswith("draft_content.json.bak") or junk.endswith(".tmp"):
                try:
                    os.remove(os.path.join(new_dir, junk))
                except OSError:
                    pass

        meta_path = os.path.join(new_dir, "draft_meta_info.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta_info = json.load(f)
                meta_info["draft_id"] = draft["id"]
                meta_info["draft_name"] = draft_name
                meta_info["draft_fold_path"] = new_dir.replace("\\", "/")
                meta_info["create_time"] = draft["create_time"]
                meta_info["update_time"] = draft["update_time"]
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta_info, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        print(f"  🏗️ 暂存区构建（剪映不可见）: {new_dir}")
        return new_dir, jp_new, draft

    try:
        df = DraftFolder(DRAFT_ROOT)
        df.duplicate_as_template(template_draft_name, draft_name, allow_replace=True)
        new_dir = os.path.join(DRAFT_ROOT, draft_name)
        jp_new = os.path.join(new_dir, "draft_content.json")
        with open(jp_new, "r", encoding="utf-8") as f:
            draft = json.load(f)
    except Exception:
        dp_root, jp, _ = find_draft_by_name(template_draft_name)
        with open(jp, "r", encoding="utf-8") as f:
            template = json.load(f)
        import copy as _copy
        draft = reset_draft_to_clean(_copy.deepcopy(template))
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

    draft = reset_draft_to_clean(draft)
    draft["id"] = gen_uuid()
    draft["name"] = draft_name
    draft["create_time"] = int(time.time() * 1_000_000)
    draft["update_time"] = int(time.time() * 1_000_000)
    save_draft(draft, jp_new)

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

    if register and register_draft_in_root_meta(draft_name, draft["id"]):
        print(f"  [ok] 已向剪映全局索引注册草稿: {draft_name}")

    return new_dir, jp_new, draft


def find_preset_by_name(name):
    for d in os.listdir(PRESET_ROOT):
        if name in d:
            pp = os.path.join(PRESET_ROOT, d, "preset_draft", "draft_content.json")
            if os.path.exists(pp):
                return pp
    return None


def _rewrite_id_refs(obj, id_map):
    """递归改写草稿内的材质 ID 引用。

    注意 extra_material_refs 等是「字符串 ID 列表」，必须逐项映射，
    否则预设轨道会保留模板旧 ID，导致引用悬空（剪映打开后清空草稿）。
    """
    _ID_KEYS = ("material_id", "group_id", "raw_segment_id")
    _LIST_ID_KEYS = ("extra_material_refs", "material_refs", "audio_material_refs",
                     "keyframe_refs", "sticker_animation_ids")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and k in _ID_KEYS:
                if v in id_map:
                    obj[k] = id_map[v]
            elif isinstance(v, list) and k in _LIST_ID_KEYS:
                obj[k] = [id_map.get(x, x) if isinstance(x, str)
                          else _rewrite_id_refs(x, id_map) for x in v]
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
                    # 同一个旧 ID 可能被多条轨道共享（如预设轨共用 speed/canvas），
                    # 必须复用已有映射，否则后处理轨道会覆盖映射导致前一条轨道悬空
                    if old_sid not in id_map:
                        id_map[old_sid] = gen_uuid()
                    seg[sid_field] = id_map[old_sid]

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


def rebase_timeline(draft, cut_us):
    """裁剪后把整条时间线左移 cut_us，使内容从 0 秒开始（消除片头黑场）。

    配合 crop_before_us 使用：跨越裁点的段已被置为 start=cut_us，左移后归零；
    裁点之后的段同步前移，段间相对位置保持不变。返回被移动的段数。
    """
    moved = 0
    for track in draft.get("tracks", []):
        for seg in track.get("segments") or []:
            tgt = seg.get("target_timerange") or {}
            start = tgt.get("start", 0)
            new_start = start - cut_us if start > cut_us else 0
            if new_start != start:
                tgt["start"] = new_start
                seg["target_timerange"] = tgt
                moved += 1
    return moved


def _cover_start_for_cut(video_path, cut_us):
    """裁点对应的封面分析起点（秒）：裁点超出片长时钳制在末帧前，避免抽帧为空。"""
    if not cut_us:
        return 0.0
    cut_s = cut_us / 1_000_000
    try:
        import subprocess as _sp
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            ffprobe = os.path.join(get_deps_root(), "tools", "ffmpeg", "bin", "ffprobe.exe")
        out = _sp.run(
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


def add_audio_track_with_file(draft, wav_path, offset_us=0, volume=1.0,
                              track_id=None):
    """新增音轨（本地 wav），字段对齐剪映 5.9 原生音频段。

    注意：音频材质 type 必须是 "extract_music"（本地导入音频）；
    旧实现写 type=2 会被剪映打开时改成 "none"、清空 local_materials，
    音轨变成无源空轨（这也是 A 方案看不到音轨/听不到声音的原因）。

    track_id: 传入已有音轨 id 时，把新片段追加到该轨（串流模式按集追加，
              避免集数增长导致轨道无限膨胀）；为 None 时新建一条轨。
    """
    import wave
    with wave.open(wav_path, "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        dur_us = int(frames / rate * 1_000_000)

    abs_path = os.path.abspath(wav_path).replace("\\", "/")
    aid = gen_uuid()       # 音频素材 id
    sid = gen_uuid()       # speed 材质 id

    audio_mat = {
        "app_id": 0,
        "category_id": "",
        "category_name": "local",
        "check_flag": 3,
        "copyright_limit_type": "none",
        "duration": dur_us,
        "effect_id": "",
        "formula_id": "",
        "id": aid,
        "intensifies_path": "",
        "is_ai_clone_tone": False,
        "is_text_edit_overdub": False,
        "is_ugc": False,
        "local_material_id": os.path.splitext(os.path.basename(wav_path))[0],
        "music_id": aid,
        "name": os.path.basename(wav_path),
        "path": abs_path,
        "source_platform": 0,
        "team_id": "",
        "type": "extract_music",
        "video_id": "",
        "wave_points": [],
    }

    seg = {
        "caption_info": None,
        "cartoon": False,
        "clip": None,
        "common_keyframes": [],
        "enable_adjust": False,
        "enable_color_correct_adjust": False,
        "enable_color_curves": True,
        "enable_color_match_adjust": False,
        "enable_color_wheels": True,
        "enable_lut": False,
        "enable_smart_color_adjust": False,
        "extra_material_refs": [sid],
        "group_id": "",
        "hdr_settings": None,
        "id": gen_uuid(),
        "intensifies_audio": False,
        "is_placeholder": False,
        "is_tone_modify": False,
        "keyframe_refs": [],
        "last_nonzero_volume": 1.0,
        "material_id": aid,
        "render_index": 0,
        "responsive_layout": {"enable": False, "horizontal_pos_layout": 0,
                              "size_layout": 0, "target_follow": "",
                              "vertical_pos_layout": 0},
        "reverse": False,
        "source_timerange": {"duration": dur_us, "start": 0},
        "speed": 1.0,
        "target_timerange": {"duration": dur_us, "start": offset_us},
        "template_id": "",
        "template_scene": "default",
        "track_attribute": 0,
        "track_render_index": 0,
        "uniform_scale": None,
        "visible": True,
        "volume": volume,
    }

    mats = draft.setdefault("materials", {})
    mats.setdefault("audios", []).append(audio_mat)
    mats.setdefault("speeds", []).append(
        {"curve_speed": None, "id": sid, "mode": 0, "speed": 1.0, "type": "speed"})

    # 追加到已有音轨（串流模式复用同一条轨，避免每集新增一条轨）
    if track_id:
        for t in draft.get("tracks", []):
            if t.get("id") == track_id and t.get("type") == "audio":
                t["segments"].append(seg)
                t["segments"].sort(key=lambda s: (s.get("target_timerange") or {}).get("start", 0))
                return aid, track_id, dur_us

    new_track_id = track_id or gen_uuid()
    draft.setdefault("tracks", []).append({
        "attribute": 0,
        "flag": 0,
        "id": new_track_id,
        "is_default_name": True,
        "name": "",
        "segments": [seg],
        "type": "audio",
    })
    return aid, new_track_id, dur_us


def attach_vocal_as_audio_track(draft, vocals_wav, offset_us=0, volume=1.0,
                                mute_video=True, track_id=None):
    """A 方案：视频段静音 + 独立 vocals 音轨（推荐，可控且二次剪辑灵活）。

    与 attach_native_vocal_separation（C）的区别：
    - A：pipeline 用 demucs 直接给最终答案，剪映打开即 Ready，不会再做云端分离
    - C：只预置 audioAlg PCM，剪映打开时可能自己重做（ep12/13 会卡顿）

    返回: (audio_material_id, audio_track_id, duration_us)
    """
    if mute_video:
        n = 0
        for t in draft.get("tracks", []):
            if t.get("type") != "video":
                continue
            for s in t.get("segments") or []:
                s["volume"] = 0.0
                s["last_nonzero_volume"] = 1.0
                clip = s.setdefault("clip", {})
                clip["volume"] = 0.0
                n += 1
        if n:
            print(f"  🔇 已静音 {n} 个视频段")
    return add_audio_track_with_file(draft, vocals_wav, offset_us, volume,
                                     track_id=track_id)


def attach_native_vocal_separation(draft, draft_dir, vocals_wav, duration_us,
                                   target_seg=None, time_range=None):
    """按剪映原生「人声分离」结构挂载人声（参考草稿 vocal_v2_1792603202）。

    做法：
      1. 把 demucs 的 vocals.wav 去掉 RIFF 头，写成 Resources/audioAlg/<hash>_0_<dur>.wav
         （剪映 audioAlg 目录里存的是裸 PCM：44100Hz / 立体声 / 16bit）
      2. 视频 segment 的 extra_material_refs 挂 speed / canvas / sound_channel_mapping /
         vocal_separation(choice=2, production_path 指向上面那个文件)
      3. 不新增 audio 轨、不修改视频 volume（由剪映内部按 production_path 播放人声）

    target_seg: 只给这个视频 segment 挂分离材质（串流模式按集挂载）；None 表示全部视频段。
    time_range: vocal_separation 材质的时间轴，默认取该 segment 的 target_timerange。
    """
    import wave

    alg_dir = os.path.join(draft_dir, "Resources", "audioAlg")
    os.makedirs(alg_dir, exist_ok=True)

    with wave.open(vocals_wav, "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"需要 16bit PCM，实际 {wf.getsampwidth() * 8}bit")
        if wf.getframerate() != 44100 or wf.getnchannels() != 2:
            raise ValueError(f"需要 44100Hz 立体声，实际 {wf.getframerate()}Hz {wf.getnchannels()}ch")
        raw = wf.readframes(wf.getnframes())

    fname = f"{gen_uuid().replace('-', '').lower()}_0_{duration_us}.wav"
    with open(os.path.join(alg_dir, fname), "wb") as f:
        f.write(raw)

    # 目标 segment 与时间轴（串流模式每集各挂一份）
    # 说明：materials.time_range 是「素材自身范围」，audioAlg 里的 PCM 从该集 0 开始，
    # 所以 start 恒为 0，duration 取该 segment 的时长；在时间轴上的位置由 segment 决定。
    if target_seg is not None:
        seg_list = [target_seg]
        if time_range is None:
            _tr = target_seg.get("target_timerange") or {}
            time_range = {"start": 0,
                          "duration": _tr.get("duration", duration_us)}
    else:
        seg_list = [s for t in draft.get("tracks", []) if t.get("type") == "video"
                    for s in t.get("segments", [])]
        if time_range is None:
            time_range = {"start": 0, "duration": duration_us}

    speed_id, canvas_id, scm_id, sep_id = gen_uuid(), gen_uuid(), gen_uuid(), gen_uuid()
    mats = draft.setdefault("materials", {})
    mats.setdefault("speeds", []).append(
        {"curve_speed": None, "id": speed_id, "mode": 0, "speed": 1.0, "type": "speed"})
    mats.setdefault("canvases", []).append(
        {"album_image": "", "blur": 0.0, "color": "", "id": canvas_id, "image": "",
         "image_id": "", "image_name": "", "source_platform": 0, "team_id": "",
         "type": "canvas_color"})
    mats.setdefault("sound_channel_mappings", []).append(
        {"audio_channel_mapping": 0, "id": scm_id, "is_config_open": False, "type": "none"})
    mats.setdefault("vocal_separations", []).append({
        "choice": 2,
        "id": sep_id,
        "production_path": (
            "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"
            f"/Resources/audioAlg/{fname}"
        ),
        "time_range": dict(time_range),
        "type": "vocal_separation",
    })

    for seg in seg_list:
        refs = seg.setdefault("extra_material_refs", [])
        for mid in (speed_id, canvas_id, scm_id, sep_id):
            if mid not in refs:
                refs.append(mid)
        seg["volume"] = 1.0

    return os.path.join(alg_dir, fname)


def _build_subtitle_content(text, font_path=None, style_size=16.0):
    if font_path is None:
        font_path = get_font_path()
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
            "size": style_size,
        }],
        "text": text,
    }, ensure_ascii=False)


def detect_silences_ffmpeg(audio_path, noise_db=-35.0, min_sil_s=0.22):
    """ffmpeg silencedetect 提取静音区间 [(start_s, end_s)]，末尾未闭合静音补到音频结尾。

    用于字幕尾裁：应在人声干声（demucs vocals）上检测——原混音里 BGM 持续
    播放会把语句间隙填成非静音，干声上无人声即真静音。
    """
    import re
    ff = _find_ffmpeg()
    if not ff or not os.path.exists(audio_path):
        return []
    cmd = [ff, "-i", audio_path, "-af",
           f"silencedetect=noise={noise_db}dB:d={min_sil_s}", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except Exception:
        return []
    sils, cur = [], None
    for m in re.finditer(r"silence_(start|end):\s*([\d.]+)", r.stderr or ""):
        if m.group(1) == "start":
            cur = float(m.group(2))
        elif cur is not None:
            sils.append((cur, float(m.group(2))))
            cur = None
    if cur is not None:
        try:
            import wave
            with wave.open(audio_path, "rb") as wf:
                sils.append((cur, wf.getnframes() / wf.getframerate()))
        except Exception:
            pass
    return sils


def refine_subtitle_timing_by_silence(subs, silences, audio_dur_us,
                                      min_keep_us=900_000, tail_us=80_000):
    """按静音区间回截每句字幕的结束时间，让字幕在语句间隙消失（"掉字"）。

    云端 ASR 返回的相邻字幕段首尾相接（end_i ≈ start_{i+1}），句间停顿被
    字幕完全覆盖 → 播放时字幕常驻不切换。人声干声上的静音即真实语句间隙，
    把段尾回截到「下一个字幕开始前最后一个静音」的起点 + 80ms 尾巴。
    min_keep_us 保证每句至少显示 0.9s（可读性），被回截的段打 "_refined"
    标记，add_text_segments 对其跳过 MIN_DUR 扩展（否则间隙又被填掉）。
    """
    if not subs or not silences:
        return subs
    sils_us = [(int(a * 1e6), int(b * 1e6)) for a, b in silences]
    n = 0
    for i, sub in enumerate(subs):
        if not isinstance(sub, dict):
            continue
        st, en = sub["start_us"], sub["end_us"]
        if i + 1 < len(subs) and isinstance(subs[i + 1], dict):
            nxt = subs[i + 1]["start_us"]
        else:
            nxt = audio_dur_us
        cand = [s for s in sils_us if st + min_keep_us <= s[0] < nxt - 50_000]
        if not cand:
            continue
        new_en = max(cand[-1][0] + tail_us, st + min_keep_us)
        if new_en < en:
            sub["end_us"] = new_en
            sub["_refined"] = True
            n += 1
    if n:
        print(f"  ✂️ 字幕尾裁: {n}/{len(subs)} 段按语音间隙回截")
    return subs


def _find_episode_vocals(workdir, idx, total_units):
    """定位该集 demucs 人声干声：串流 demucs_ep{idx:02d}/<model>/<stem>/vocals.wav，
    单集模式直接 <workdir>/<model>/<stem>/vocals.wav。"""
    import glob as _g
    pats = [os.path.join(workdir, f"demucs_ep{idx:02d}", "*", "*", "vocals.wav")]
    if total_units == 1:
        pats.append(os.path.join(workdir, "*", "*", "vocals.wav"))
    for p in pats:
        hits = _g.glob(p)
        if hits:
            return hits[0]
    return None


def add_text_segments(draft, sub_segments, offset_us=0, font="HarmonyOS Sans SC",
                       font_size=30, color=[1.0, 1.0, 1.0, 1.0], stroke_color=[0, 0, 0, 0.8],
                       stroke_width=4, visible=True, sub_type=0):
    """写入字幕轨（严格复刻剪映原生双语字幕结构）。

    原生约定（参考 10月22日 草稿）：
      中文字幕: track.attribute=2, seg.track_attribute=2, render_index=14000, transform.y=-0.56
      英文字幕: track.attribute=0, seg.track_attribute=0, render_index=14052, transform.y=-0.64
      两条 track 均 flag=1 / is_default_name=true，segment.visible 控制显隐。
    """
    text_mats = []
    segs = []
    anim_mats = []

    is_cn = (sub_type == 0)
    track_attribute = 2 if is_cn else 0
    render_index = 14000 if is_cn else 14052
    transform_y = -0.56 if is_cn else -0.64

    MIN_DUR_US = 1_500_000

    for idx, sub in enumerate(sub_segments):
        text_str = sub["text"] if isinstance(sub, dict) else str(sub)
        if not text_str.strip():
            continue

        tid = gen_uuid()
        aid = gen_uuid()

        start_us = sub["start_us"] + offset_us if isinstance(sub, dict) else 0
        end_us = sub["end_us"] + offset_us if isinstance(sub, dict) else 0
        dur_us = end_us - start_us
        if dur_us < MIN_DUR_US and not (isinstance(sub, dict) and sub.get("_refined")):
            dur_us = MIN_DUR_US
            end_us = start_us + dur_us

        if idx + 1 < len(sub_segments):
            next_sub = sub_segments[idx + 1]
            next_start = (next_sub["start_us"] + offset_us) if isinstance(next_sub, dict) else 0
            if end_us > next_start:
                # 截断到下一条开始（同轨重叠会被剪映拆轨 → 同位置双字幕叠加闪烁）；
                # 不能再回扩到 MIN_DUR_US，否则重新造成重叠
                end_us = next_start - 100_000
                dur_us = end_us - start_us
                if dur_us <= 0:
                    continue

        text_mat = {
            "add_type": 1 if is_cn else 0,
            "alignment": 1,
            "background_alpha": 1.0,
            "background_color": "",
            "background_height": 0.14,
            "background_horizontal_offset": 0.0,
            "background_round_radius": 0.0,
            "background_style": 0,
            "background_vertical_offset": 0.0,
            "background_width": 0.14,
            "base_content": "",
            "bold_width": 0.0,
            "border_alpha": 1.0,
            "border_color": "",
            "border_width": 0.08,
            "caption_template_info": {"category_id": "", "category_name": "", "effect_id": "", "is_new": False, "path": "", "request_id": "", "resource_id": "", "resource_name": "", "source_platform": 0},
            "check_flag": 7,
            "combo_info": {"text_templates": []},
            "content": _build_subtitle_content(text_str, style_size=8.0),
            "fixed_height": -1.0,
            "fixed_width": -1.0,
            "font_category_id": "",
            "font_category_name": "",
            "font_id": "",
            "font_name": "",
            "font_path": get_font_path(),
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
            "language": "" if is_cn else "en-US",
            "layer_weight": 1,
            "letter_spacing": 0.0,
            "line_feed": 1,
            "line_max_width": 0.82,
            "line_spacing": 0.02,
            "multi_language_current": "none",
            "name": "",
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
            "shadow_color": "",
            "shadow_distance": 5.0,
            "shadow_point": {"x": 0.6363961030678928, "y": -0.6363961030678928},
            "shadow_smoothing": 0.45,
            "shape_clip_x": False,
            "shape_clip_y": False,
            "source_from": "",
            "style_name": "",
            "sub_type": sub_type,
            "subtitle_keywords": None,
            "subtitle_template_original_fontsize": 0.0,
            "text_alpha": 1.0,
            "text_color": "#FFFFFF",
            "text_curve": None,
            "text_preset_resource_id": "",
            "text_size": 30,
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
            "caption_info": None,
            "cartoon": False,
            "clip": {
                "alpha": 1.0,
                "flip": {"horizontal": False, "vertical": False},
                "rotation": 0.0,
                "scale": {"x": 1.0, "y": 1.0},
                "transform": {"x": 0.0, "y": transform_y},
            },
            "common_keyframes": [],
            "enable_adjust": False,
            "enable_color_correct_adjust": False,
            "enable_color_curves": True,
            "enable_color_match_adjust": False,
            "enable_color_wheels": True,
            "enable_lut": False,
            "enable_smart_color_adjust": False,
            "extra_material_refs": [aid],
            "group_id": "",
            "hdr_settings": None,
            "id": gen_uuid(),
            "intensifies_audio": False,
            "is_placeholder": False,
            "is_tone_modify": False,
            "keyframe_refs": [],
            "last_nonzero_volume": 1.0,
            "material_id": tid,
            "render_index": render_index,
            "responsive_layout": {
                "enable": False, "horizontal_pos_layout": 0,
                "size_layout": 0, "target_follow": "", "vertical_pos_layout": 0,
            },
            "reverse": False,
            "source_timerange": None,
            "speed": 1.0,
            "target_timerange": {"duration": dur_us, "start": start_us},
            "template_id": "",
            "template_scene": "default",
            "track_attribute": track_attribute,
            "track_render_index": 1,
            "uniform_scale": {"on": True, "value": 1.0},
            "visible": visible,
            "volume": 1.0,
        }

        anim_mats.append({
            "animations": [],
            "id": aid,
            "multi_language_current": "none",
            "type": "sticker_animation",
        })
        text_mats.append(text_mat)
        segs.append(seg)

    track = {
        "attribute": track_attribute,
        "flag": 1,
        "id": gen_uuid(),
        "is_default_name": True,
        "name": "",
        "segments": segs,
        "type": "text",
    }

    mats = draft.setdefault("materials", {})
    mats.setdefault("texts", []).extend(text_mats)
    mats.setdefault("material_animations", []).extend(anim_mats)
    draft.setdefault("tracks", []).append(track)

    return len(text_mats)


def delete_text_track_by_sub_type(draft, sub_type):
    """删除指定 sub_type 的所有文字段与素材（如翻译失败时清掉英文轨）。"""
    texts = draft.get("materials", {}).get("texts", [])
    by_id = {m.get("id"): m for m in texts}
    remove_mat_ids = set()
    removed = 0
    keep_tracks = []

    for track in draft.get("tracks", []):
        if track.get("type") != "text":
            keep_tracks.append(track)
            continue
        kept = []
        for seg in track.get("segments", []):
            mat = by_id.get(seg.get("material_id", ""))
            if mat is not None and mat.get("sub_type", 0) == sub_type:
                remove_mat_ids.add(mat.get("id"))
                removed += 1
            else:
                kept.append(seg)
        if kept:
            track["segments"] = kept
            keep_tracks.append(track)

    draft["tracks"] = keep_tracks
    if remove_mat_ids:
        draft["materials"]["texts"] = [m for m in texts if m.get("id") not in remove_mat_ids]
    return removed


def hide_text_track_by_language(draft, lang_cn=True):
    """隐藏指定语言字幕轨（只改 segment.visible，保持剪映原生 materials 结构）。"""
    hidden_count = 0
    target_sub_type = 0 if lang_cn else 5
    for track in draft.get("tracks", []):
        if track.get("type") != "text":
            continue
        for seg in track.get("segments", []):
            mid = seg.get("material_id", "")
            for m in draft.get("materials", {}).get("texts", []):
                if m.get("id") == mid:
                    if m.get("sub_type", 0) == target_sub_type:
                        seg["visible"] = False
                        hidden_count += 1
                    break
    return hidden_count


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".rmvb", ".rm", ".3gp"}


def _is_video_path(p):
    return os.path.splitext(p)[1].lower() in VIDEO_EXTS


def get_video_duration_us(video_path):
    """返回视频时长（微秒）。优先上游缓存 ffprobe，后备本地 MP4 解析。"""
    try:
        secs = _upstream_get_duration(video_path)
        if secs and secs > 0:
            return int(secs * 1_000_000)
    except Exception:
        pass

    try:
        import shutil as _sh
        import subprocess as _sp
        ffprobe_path = _sh.which("ffprobe")
        if ffprobe_path:
            result = _sp.run(
                [ffprobe_path, "-v", "error", "-show_entries",
                 "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                 video_path],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(float(result.stdout.strip()) * 1_000_000)
    except Exception:
        pass

    ext = os.path.splitext(video_path)[1].lower()
    if ext in (".mp4", ".m4v", ".mov", ".3gp"):
        dur = _parse_mp4_duration(video_path)
        if dur is not None:
            return dur

    print(f"  ⚠️ 无法获取视频时长: {video_path}")
    return None


def get_video_size(video_path):
    """返回视频分辨率 (width, height)。ffprobe 不可用时返回 None。"""
    try:
        import shutil as _sh
        import subprocess as _sp
        ffprobe_path = _sh.which("ffprobe")
        if not ffprobe_path:
            return None
        result = _sp.run(
            [ffprobe_path, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", video_path],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            w, h = result.stdout.strip().split("x")[:2]
            return int(w), int(h)
    except Exception:
        pass
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
    - 多个视频按传入顺序（调用方按文件名 a→z 排序）串在同一条主 video track 上
    - 每集记录 start_us / duration_us / segment，供后续字幕和人声分离按集偏移
    - 如果已有 video 主轨且有 segments，先清空它们（让注入的视频做第一轨）

    返回: episodes 列表 [{"path", "name", "start_us", "duration_us", "segment", "material_id"}]
    """
    video_paths = [os.path.abspath(p) for p in video_paths]
    for p in video_paths:
        if not os.path.exists(p):
            raise FileNotFoundError(f"视频不存在: {p}")

    print(f"🎬 往已有草稿注入 {len(video_paths)} 个视频:")

    cum_start = 0
    segs = []
    materials_videos = []
    episodes = []
    first_size = None

    for vp in video_paths:
        _norm = normalize_video_for_jianying(vp)
        if _norm is None:
            print(f"  ⚠️ 转封装失败，跳过: {os.path.basename(vp)}")
            continue
        if _norm != vp:
            print(f"  🔧 自动转封装: {os.path.basename(vp)} → {os.path.basename(_norm)}")
        vp = _norm

        dur_us = get_video_duration_us(vp)
        if dur_us is None:
            print(f"  ⚠️ 无法获取 {os.path.basename(vp)} 时长，跳过")
            continue

        vid = gen_uuid()
        fname = os.path.basename(vp)
        size = get_video_size(vp)
        print(f"  📼 {fname}  →  {dur_us/1_000_000:.2f}s  [{cum_start/1_000_000:.2f}s - {(cum_start+dur_us)/1_000_000:.2f}s]"
              + (f"  {size[0]}x{size[1]}" if size else ""))

        mat = deepcopy(BLANK_VIDEO_MATERIAL)
        mat["id"] = vid
        mat["material_id"] = vid
        mat["path"] = vp
        mat["material_name"] = fname
        mat["duration"] = dur_us
        if size:
            mat["width"], mat["height"] = size
        if first_size is None and size:
            first_size = size

        seg = deepcopy(BLANK_VIDEO_SEGMENT)
        seg["id"] = gen_uuid()
        seg["material_id"] = vid
        seg["source_timerange"] = {"start": 0, "duration": dur_us}
        seg["target_timerange"] = {"start": cum_start, "duration": dur_us}

        materials_videos.append(mat)
        segs.append(seg)
        episodes.append({
            "path": vp,
            "name": fname,
            "start_us": cum_start,
            "duration_us": dur_us,
            "segment": seg,
            "material_id": vid,
        })
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

    # 画布必须与源视频方向一致（竖屏 1080x1920），否则字幕/贴纸按横屏坐标布局会错位
    if first_size:
        draft["canvas_config"] = {
            "width": first_size[0],
            "height": first_size[1],
            "ratio": "original",
        }
        print(f"  🖼️ 画布: {first_size[0]}x{first_size[1]} (ratio=original)")

    print(f"  ✅ 注入完成: {len(segs)} segments, {len(materials_videos)} materials")
    print(f"  📏 主轨总时长: {cum_start/1_000_000:.2f}s")
    print()

    return episodes


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


def run_asr_chain(audio_source, workdir, skip_local_ai=False):
    """单段音频 ASR 降级链：剪映云ASR → Paraformer → faster-whisper。

    返回集内相对时间轴的字幕列表（未加集偏移），失败返回 []。
    """
    if not audio_source:
        print("  ✗ 没有音频可用于 ASR!")
        return []
    print(f"  🗣️ 使用音频: {audio_source}")
    if skip_local_ai:
        print("  ⏭️ skip_local_ai=True")
        return []

    # 第一优先: 剪映云 ASR (免费, 中文质量最好)
    cn_subs = run_vc_asr(audio_source, workdir)
    if cn_subs:
        print(f"  ✅ 剪映云识别出 {len(cn_subs)} 条字幕")
        return cn_subs

    # 降级: 本地 Paraformer (优先) → faster-whisper
    print("  ↩️ 降级到本地 Paraformer ASR...")
    try:
        cn_subs = run_paraformer(audio_source, workdir)
        if cn_subs:
            print(f"  ✅ Paraformer 识别出 {len(cn_subs)} 条字幕")
            return cn_subs
    except ImportError:
        print("  ⚠️ funasr-onnx 未安装 (pip install funasr-onnx)")
    except Exception as e:
        print(f"  ⚠️ Paraformer 失败: {type(e).__name__}: {e}")

    print("  ↩️ 再降级到 faster-whisper...")
    try:
        cn_subs = run_whisper(audio_source)
        print(f"  ✅ 识别出 {len(cn_subs) if cn_subs else 0} 条字幕")
    except ImportError:
        print("  ⚠️ faster-whisper 未安装 (pip install faster-whisper)")
        return []
    return cn_subs or []


def translate_texts(cn_texts, workdir):
    """翻译降级链：VC(智谱→讯飞) → 本地Ollama。

    每级都做软失败校验，全部失败返回 None（调用方跳过该集英文字幕）。
    Google/MyMemory 已移除（2026-10-24）：前者易 429 软失败且与 VC 内部重复，
    后者机翻味重会无声混进成片，宁可留空待 repair_translation.py 高质量补翻。
    """
    try:
        import vc_bridge as _vcb
    except ImportError:
        _vcb = None

    # 主/备: videocaptioner providers(GLM → 讯飞) → google
    en_texts = run_translate_vc(cn_texts, workdir)
    if en_texts:
        print(f"  ✅ VideoCaptioner 翻译 {len(en_texts)} 条 "
              f"(LLM={'已配置' if (_vcb and _vcb.llm_configured()) else '未配置'})")

    # 兜底: 本地 Ollama（Google 层已移除 2026-10-24：免费端易 429 软失败，
    # 且与 VC 内部 google 重复，净贡献≈0，不如快速收敛到本地模型）
    if en_texts is None:
        try:
            en_texts = _vcb.translate_texts_via_ollama(cn_texts) if _vcb else None
            if en_texts:
                print(f"  ✅ 本地 Ollama 翻译 {len(en_texts)} 条")
        except Exception as _e:
            print(f"  ⚠️ 本地 Ollama 翻译失败: {str(_e)[:100]}")

    return en_texts


_MIN_CUT_FRAG_US = 250_000  # 被裁点切断的残片不足 0.25s 只剩闪字，丢弃


def _repair_translation_round(gaps, en_all, workdir):
    """翻译修复轮：对留空集重跑全链一次，成功补入 en_all，返回仍失败的集列表。"""
    still_gap = []
    for g in gaps:
        with timed_step(f"翻译修复/ep{g['idx']}"):
            en_texts = translate_texts([s["text"] for s in g["cn_subs"]], workdir)
        if not en_texts:
            print(f"  ✗ {g['tag']}修复仍失败，保留空位")
            still_gap.append(g)
            continue
        added = 0
        for s, t in zip(g["cn_subs"], en_texts):
            if not str(t).strip():
                continue
            rel_start = s["start_us"] - g["src_off"]
            rel_end = s["end_us"] - g["src_off"]
            if rel_end <= 0 or (rel_start < 0 and rel_end < _MIN_CUT_FRAG_US):
                continue
            en_all.append({
                "start_us": max(0, rel_start) + g["ep_start_us"],
                "end_us": rel_end + g["ep_start_us"],
                "text": str(t),
                "_refined": s.get("_refined", False),
            })
            added += 1
        print(f"  ✅ {g['tag']}修复成功，补入 {added} 条英文字幕")
    return still_gap


def _toast_safe(title, msg, icon="info"):
    """桌面通知（可选依赖，任何失败都静默）。"""
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))
        from utils.notifier import toast
        toast(title, msg, icon=icon)
    except Exception:
        pass


def run_demucs(input_path, output_dir, model=None, fallback=None):
    """人声分离。优先 fine-tuned 模型（分离更干净），失败时回退标准模型。

    模型对默认取自 _env_profile.json 的 demucs 段（无画像时 htdemucs_ft → htdemucs）。
    """
    import subprocess, shutil as _shutil
    py = _shutil.which("python") or _shutil.which("python3") or sys.executable

    _dm = (_ENV_PROFILE.get("demucs") or {})
    model = model or _dm.get("model") or "htdemucs_ft"
    fallback = fallback if fallback is not None else (_dm.get("fallback") or "htdemucs")

    models = [model] + ([fallback] if fallback and fallback != model else [])
    vocal = no_vocal = None
    for m in models:
        cmd = [py, "-m", "demucs", "-n", m, "--two-stems=vocals", "-o", output_dir, input_path]
        print(f"  🎵 demucs ({m}): {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode != 0:
            print(f"  ✗ demucs ({m}) failed: {result.stderr[-300:]}")
            continue
        base = os.path.join(output_dir, m, os.path.splitext(os.path.basename(input_path))[0])
        vocal = os.path.join(base, "vocals.wav")
        no_vocal = os.path.join(base, "no_vocals.wav")
        if not os.path.exists(vocal):
            alt = os.path.join(base, "vocal.wav")
            if os.path.exists(alt):
                vocal = alt
        if os.path.exists(vocal):
            return vocal, (no_vocal if os.path.exists(no_vocal) else None)
        print(f"  ✗ demucs ({m}) 输出缺失")
    return None, None


def run_whisper(input_path, model_size=None, device=None, compute_type=None):
    """faster-whisper 最终兜底 ASR。

    默认配置取自 _env_profile.json（install_deps.ps1 按硬件分级选择）；
    无画像时回退历史默认 base/cpu/int8。加载按候选链降级，保证可用性优先。
    """
    from faster_whisper import WhisperModel
    wp = _ENV_PROFILE.get("whisper") or {}
    model_size = model_size or wp.get("model") or "base"
    device = device or wp.get("device") or "cpu"
    compute_type = compute_type or wp.get("compute_type") or "int8"
    candidates = list(dict.fromkeys([(device, compute_type), ("cpu", "int8"), ("cpu", "default")]))
    model = None
    for dev, ct in candidates:
        try:
            print(f"  🗣️ loading whisper model ({model_size}, {dev}/{ct})...")
            model = WhisperModel(model_size, device=dev, compute_type=ct)
            break
        except Exception as e:
            print(f"  ⚠️ whisper ({dev}/{ct}) 加载失败: {str(e)[:80]}")
    if model is None:
        raise RuntimeError("faster-whisper 所有后端均加载失败")
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


_pp = (_ENV_PROFILE.get("paraformer") or {})
PARAFORMER_DIR = _pp.get("model_dir") or os.path.join(get_deps_root(), "models", "paraformer", "iic_onnx")
PARAFORMER_VAD_DIR = _pp.get("vad_dir") or os.path.join(get_deps_root(), "models", "paraformer", "vad_onnx")
PARAFORMER_QUANTIZE = bool(_pp.get("quantize", True))  # int8；fp32 实测验证后才在画像开放


def run_paraformer(input_path, workdir):
    """本地 Paraformer 兜底 ASR（模型来自 ModelScope，国内 CDN 可下载）。

    VAD 切段 + 逐段识别，返回与 run_whisper 同结构的字幕列表
    （start_us/end_us/text）。funasr_onnx 未安装时抛 ImportError。
    quantize 由 _env_profile.json 的 paraformer.quantize 决定（默认 int8）。
    """
    import wave as _wave
    import shutil as _sh
    import numpy as _np
    from funasr_onnx import Paraformer, Fsmn_vad

    ffmpeg = _sh.which("ffmpeg") or _find_ffmpeg()
    wav16k = os.path.join(workdir, "paraformer_16k.wav")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", input_path,
                    "-vn", "-ar", "16000", "-ac", "1", wav16k],
                   capture_output=True, text=True, check=True)

    print(f"  🗣️ loading Paraformer ({'int8' if PARAFORMER_QUANTIZE else 'fp32'})...")
    asr = Paraformer(PARAFORMER_DIR, batch_size=1, quantize=PARAFORMER_QUANTIZE)
    vad = Fsmn_vad(PARAFORMER_VAD_DIR, batch_size=1, quantize=PARAFORMER_QUANTIZE, max_end_sil=500)

    w = _wave.open(wav16k, "rb")
    audio = _np.frombuffer(w.readframes(w.getnframes()), dtype=_np.int16).astype(_np.float32) / 32768.0
    w.close()

    print("  🗣️ VAD 切段...")
    segs = vad(wav16k)[0]  # [[start_ms, end_ms], ...]

    print(f"  🗣️ 逐段识别 {len(segs)} 段...")
    subs = []
    for s_ms, e_ms in segs:
        seg = audio[int(s_ms) * 16: int(e_ms) * 16]
        if seg.size < 1600:  # <0.1s 丢弃
            continue
        r = asr(seg)
        text = r[0].get("preds") if isinstance(r[0], dict) else r[0]
        if isinstance(text, (list, tuple)):
            text = text[0] if text else ""
        text = (text or "").strip()
        if not text:
            continue
        subs.append({
            "start_us": int(s_ms) * 1000,
            "end_us": int(e_ms) * 1000,
            "text": text,
        })
    return subs


def run_vc_asr(asr_source, workdir, engines=("jianying", "bijian")):
    """剪映云 ASR（via videocaptioner），失败返回 None 由调用方降级 whisper。"""
    try:
        import vc_bridge
    except ImportError:
        print("  ⚠️ vc_bridge 不可用")
        return None
    out_srt = os.path.join(workdir, "vc_asr.srt")
    subs = vc_bridge.vc_asr_with_fallback(asr_source, out_srt, engines=engines)
    if not subs:
        print("  ✗ 剪映云 ASR 全链失败")
    return subs


def run_translate_vc(cn_texts, workdir):
    """翻译降级链第一梯队: videocaptioner llm(智谱) → bing → google。失败返回 None。"""
    try:
        import vc_bridge
    except ImportError:
        return None
    return vc_bridge.translate_texts_via_vc(cn_texts, workdir, tag="pipe")


def run_ffmpeg_extract_audio(video_path, output_wav):
    import shutil as _shutil
    ffmpeg = _shutil.which("ffmpeg") or _find_ffmpeg()
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


# ---- 组合预设解析（分层，无硬编码个人配置） ----
# 优先级: 显式传入/--presets > pipeline/presets.json(个人, gitignored) > 预设库 pipeline_* 前缀自动发现 > 不用预设
# 预设本身在剪映「我的预设」库中创建；alpha 为可选滤镜透明度，仅对滤镜类生效
PRESET_PREFIX = "pipeline_"
PRESETS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets.json")


def list_available_presets():
    """扫描本机剪映组合预设库，返回可用预设名列表。"""
    names = []
    if os.path.isdir(PRESET_ROOT):
        for name in sorted(os.listdir(PRESET_ROOT)):
            if os.path.isdir(os.path.join(PRESET_ROOT, name, "preset_draft")):
                names.append(name)
    return names


def parse_presets_arg(s):
    """解析 '名字1,名字2:0.03' 为 [(name, alpha), ...]；alpha 为可选滤镜透明度。"""
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            name, alpha = part.rsplit(":", 1)
            out.append((name.strip(), float(alpha)))
        else:
            out.append((part, None))
    return out


def resolve_presets():
    """返回 (presets, 来源描述)。presets 为 [(name, alpha), ...]，可能为空列表。"""
    if os.path.isfile(PRESETS_JSON):
        try:
            with open(PRESETS_JSON, encoding="utf-8") as f:
                items = json.load(f).get("presets", [])
            out = [(it["name"], it.get("alpha")) for it in items if it.get("name")]
            if out:
                return out, f"配置文件 {os.path.basename(PRESETS_JSON)}"
        except Exception as e:
            print(f"  ⚠️ presets.json 解析失败({e})，回退自动发现")
    prefixed = [(n, None) for n in list_available_presets() if n.startswith(PRESET_PREFIX)]
    if prefixed:
        return prefixed, f"预设库前缀 {PRESET_PREFIX}* 自动发现"
    return [], None


def warn_missing_presets(presets):
    """校验解析出的预设在预设库中真实存在，缺失项给出醒目告警。"""
    avail = set(list_available_presets())
    missing = [n for n, _ in presets if n not in avail]
    for n in missing:
        print(f"  ⚠️ 预设 '{n}' 在本机剪映预设库中不存在，合并时将被跳过")
        print(f"     可用预设: {sorted(avail) or '(空)'}")
    return not missing


DEFAULT_PRESETS = []  # 兼容旧引用；实际解析走 resolve_presets()


def default_workdir_for_draft(draft_name_or_path):
    """未显式指定 workdir 时，按草稿名派生独立工作目录。

    demucs 产物按 demucs_epNN 命名，且被草稿以绝对路径引用；
    若所有草稿共用 pipeline/_work，后一次运行会原地覆盖前一部剧的音频素材。
    """
    import hashlib
    p = str(draft_name_or_path)
    name = os.path.basename(os.path.normpath(p))
    if name.lower().endswith(".json"):
        name = os.path.basename(os.path.dirname(os.path.abspath(p)))
    safe = re.sub(r'[<>:"/\\|?*\s]+', "", name)[:24] or "draft"
    tag = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    return os.path.join(_HERE, f"_work_{safe}_{tag}")


def run_pipeline(draft_name_or_path, video_paths=None,
                 cut_us=None, workdir=None,
                 skip_local_ai=False, do_export=True,
                 do_demucs=True, do_presets=True, do_ffmpeg=True,
                 do_whisper=True, do_translate=True, do_hide_cn=True,
                 do_subtitle_check=True,
                 do_vad_gate=True, vad_min_speech_ratio=None,
                 presets=None,
                 cover_image=None, cover_extract_from_video=None, cover_extract_time_s=3.0,
                 vocal_mode="audio_track"):
    if workdir is None:
        workdir = default_workdir_for_draft(draft_name_or_path)
    os.makedirs(workdir, exist_ok=True)
    _TIMINGS.clear()  # 每次运行重新计时

    if presets is None:
        presets, _preset_src = resolve_presets()
        if _preset_src:
            print(f"  🧩 预设来源: {_preset_src}")
        if do_presets and not presets:
            print("  ⚠️ 未解析到任何预设（无 presets.json 且无 pipeline_* 前缀预设），本次不使用预设")
        elif do_presets:
            warn_missing_presets(presets)
    PRESETS = presets

    print(f"\n{'='*60}")
    print(f"  🎬 JianYing Full-Local Pipeline")
    print(f"  📂 Draft      : {draft_name_or_path}")
    print(f"  🎥 Videos     : {len(video_paths) if video_paths else '(无, 使用已有)'}")
    print(f"  ✂️ cut_us     : {cut_us}  ({cut_us/1_000_000 if cut_us else 'None (no cut)'}s)")
    print(f"  🧩 Presets    : {[p[0] for p in PRESETS] if do_presets else 'skip'}")
    print(f"  🛑 Skip AI    : {skip_local_ai}")
    print(f"  📤 Export     : {do_export}")
    print(f"  📁 Workdir    : {workdir}")
    print(f"  🎚️ VAD Gate   : {'on' if do_vad_gate else 'off'}"
          f"{' (需 demucs 干声)' if do_vad_gate else ''}")
    print(f"{'='*60}\n")

    if not draft_name_or_path:
        print("✗ 必须指定草稿名!")
        return None

    dp_root = None
    draft_path = None
    draft = None
    staged = False

    if os.path.isdir(draft_name_or_path):
        draft_path = os.path.join(draft_name_or_path, "draft_content.json")
        dp_root = draft_name_or_path
    else:
        dp_root, jp, meta = find_draft_by_name(draft_name_or_path)
        if dp_root is None:
            print(f"📂 草稿 '{draft_name_or_path}' 不存在, 自动创建空白草稿...")
            # 在剪映草稿目录之外的暂存区构建，全部写完后再整体发布，
            # 避免剪映在构建过程中打开草稿、缓存空模板导致后几集空白。
            dp_root, jp, _ = create_empty_draft(draft_name_or_path, staging=True, register=False)
            if dp_root is None:
                print("✗ 创建空白草稿失败 (找不到模板)")
                return None
            staged = True
            print(f"✅ 已创建空白草稿(暂存区): {draft_name_or_path}")
        draft_path = jp
    draft, _ = load_draft(draft_path)
    print(f"✅ Loaded existing draft: {draft.get('name') or dp_root}")
    print(f"   Pre-state:\n{summarize_draft(draft)}")

    draft = reset_draft_to_clean(draft)
    print(f"🔄 Draft reset to clean state (tracks=[], materials={{}})")

    if cover_image or cover_extract_from_video:
        print(f"🖼️ 设置草稿封面: cover_image={cover_image}, extract_from={cover_extract_from_video}")
        set_draft_cover(
            dp_root,
            cover_image=cover_image,
            extract_from_video=cover_extract_from_video,
            extract_time_s=cover_extract_time_s,
            video_start_s=(_cover_start_for_cut(cover_extract_from_video, cut_us)
                           if cover_extract_from_video else 0.0),
        )

    episodes = []
    if video_paths:
        with timed_step("注入视频"):
            episodes = inject_videos_into_draft(draft, video_paths)
    print()

    multi = len(episodes) > 1
    video_path = get_video_filepath_from_draft(draft)
    existing_audio = get_audio_filepath_from_draft(draft)

    main_dur = get_main_video_duration(draft)
    print(f"📏 主视频轨总时长: {main_dur/1_000_000:.2f}s ({main_dur}us)"
          + (f"  ← {len(episodes)} 集串流" if multi else ""))
    print()

    # ━━━━ Step 1: 人声分离 ━━━━
    # 收集待门控的干声；门控延后到字幕写完（见 Step 10.2），
    # 因为字幕尾裁的静音检测必须在未门控的干声上做。
    vocal_paths = []
    if do_demucs:
        print("━ [1] 人声分离 (demucs) ━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if skip_local_ai:
            print("  ⏭️ skip_local_ai=True，跳过")
        elif episodes:
            # 串流模式：每集独立分离，各自挂到自己的视频 segment（时间轴=该集位置）
            if vocal_mode == "audio_track":
                # A 方案：先静音所有视频段一次，再按集加 vocals 轨
                n = 0
                for t in draft.get("tracks", []):
                    if t.get("type") != "video":
                        continue
                    for s in t.get("segments") or []:
                        s["volume"] = 0.0
                        s["last_nonzero_volume"] = 1.0
                        if s.get("clip"):
                            s["clip"]["volume"] = 0.0
                        n += 1
                print(f"  🔇 已静音 {n} 个视频段")
            vocal_track_id = None
            bgm_track_id = None
            for i, ep in enumerate(episodes, 1):
                tag = f"第{i}集 " if multi else ""
                print(f"  ── {tag}{ep['name']}")
                try:
                    src_for_demucs = ep["path"]
                    if not src_for_demucs.lower().endswith(".wav"):
                        tmp_wav = os.path.join(workdir, f"demucs_input_ep{i:02d}.wav")
                        print("  🎬 先从视频提取音频...")
                        if run_ffmpeg_extract_audio(src_for_demucs, tmp_wav):
                            src_for_demucs = tmp_wav

                    with timed_step(f"人声分离/ep{i}"):
                        vocal, bgm = run_demucs(
                            src_for_demucs, os.path.join(workdir, f"demucs_ep{i:02d}"))
                    if not vocal:
                        print("  ✗ demucs 输出缺失")
                        continue
                    if vocal_mode == "audio_track":
                        vocal_paths.append((i, vocal))
                        # A 方案：按集偏移；所有集复用同一条 vocals 轨 / no_vocals 轨
                        offset = ep["segment"]["target_timerange"]["start"]
                        _, vocal_track_id, _ = attach_vocal_as_audio_track(
                            draft, vocal, offset_us=offset, mute_video=False,
                            track_id=vocal_track_id)
                        if bgm:
                            _, bgm_track_id, _ = add_audio_track_with_file(
                                draft, bgm, offset_us=offset, volume=0.0,
                                track_id=bgm_track_id)
                        print(f"  ✅ 人声已挂为独立音轨 (偏移 {offset/1e6:.2f}s)")
                    else:
                        # C 方案：原生预制（剪映打开时可能重做）
                        sep_file = attach_native_vocal_separation(
                            draft, os.path.dirname(draft_path), vocal,
                            ep["duration_us"], target_seg=ep["segment"])
                        print(f"  ✅ 原生人声分离已挂载: {os.path.basename(sep_file)}")
                except ImportError:
                    print("  ⚠️ demucs 未安装 (pip install demucs)")
                    break
                except Exception as e:
                    print(f"  ✗ 人声分离失败: {e}")
        else:
            source = video_path or existing_audio
            if not source:
                print("  ⏭️ 无音频源")
            else:
                try:
                    tmp_wav = os.path.join(workdir, "extract_input.wav")
                    src_for_demucs = source
                    if video_path and not (existing_audio and existing_audio.endswith(".wav")):
                        print("  🎬 先从视频提取音频...")
                        ok = run_ffmpeg_extract_audio(video_path, tmp_wav)
                        if ok:
                            src_for_demucs = tmp_wav

                    vocal, bgm = run_demucs(src_for_demucs, workdir)
                    if vocal:
                        try:
                            if vocal_mode == "audio_track":
                                vocal_paths.append((1, vocal))
                                # A 方案：视频静音 + 独立 vocals 音轨；no_vocals 静音对照
                                attach_vocal_as_audio_track(draft, vocal)
                                if bgm:
                                    add_audio_track_with_file(draft, bgm, offset_us=0,
                                                              volume=0.0)
                                print(f"  ✅ 人声已挂为独立音轨")
                            else:
                                # C 方案：原生预制（剪映打开时可能重做）
                                sep_file = attach_native_vocal_separation(
                                    draft, os.path.dirname(draft_path), vocal, main_dur)
                                print(f"  ✅ 原生人声分离已挂载: {os.path.basename(sep_file)}")
                        except Exception as e:
                            print(f"  ✗ 人声分离挂载失败: {e}")
                    else:
                        print(f"  ✗ demucs 输出缺失")
                except ImportError:
                    print("  ⚠️ demucs 未安装 (pip install demucs)")
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
        print(f"━ [6] 裁剪 {cut_us/1_000_000:.2f}s 之前"
              + ("（串流：整条时间线前移）" if multi else "") + " ━━━━━━━━")
        pre_dur = get_main_video_duration(draft)
        cuts = crop_before_us(draft, cut_us)
        for ct in cuts:
            if isinstance(ct[1], str):
                print(f"  ✂️  [{ct[0]}] mat_id={ct[1][:12]}... {ct[2]}")
            else:
                print(f"  🗑️  [{ct[0]}] {ct[1]} items {ct[2]}")
        moved = rebase_timeline(draft, cut_us)
        if moved:
            print(f"  ⏪ 时间线整体前移 {cut_us/1_000_000:.2f}s（{moved} 个段）")
        main_dur = get_main_video_duration(draft)
        print(f"  📏 裁剪前 {pre_dur/1_000_000:.2f}s → 裁剪后 {main_dur/1_000_000:.2f}s")
        if main_dur <= 0:
            print("  ✗ 裁点超出总时长, 裁剪后没有内容, 已中止（草稿未保存）")
            return None
        print()
    else:
        print("━ [6] 跳过裁剪 (cut_us=None) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print()

    # ━━━━ Step 7-9: 逐集 提取音频 → ASR → 翻译 → 写字幕（带集偏移）━━━━
    cn_all, en_all = [], []
    translation_gaps = []  # 翻译全链失败的集，主循环结束后立即进入修复轮
    if do_whisper or do_translate:
        print("━ [7-9] 提取音频 → 识别字幕 → 翻译 (按集串流) ━━━━━━━━━━━━━")

        units = []
        if episodes:
            for i, ep in enumerate(episodes, 1):
                tr = ep["segment"].get("target_timerange") or {}
                sr = ep["segment"].get("source_timerange") or {}
                units.append({
                    "idx": i,
                    "path": ep["path"],
                    "name": ep["name"],
                    "start_us": tr.get("start", ep["start_us"]),
                    "duration_us": tr.get("duration", ep["duration_us"]),
                    # 裁剪后该集音频源的起点（ASR 跑的是完整音源，需减去这段）
                    "src_start_us": sr.get("start", 0),
                })
        else:
            src = video_path or existing_audio
            if src:
                units.append({"idx": 1, "path": src, "name": os.path.basename(src),
                              "start_us": 0, "duration_us": main_dur,
                              "src_start_us": 0})

        if not units:
            print("  ✗ 找不到可用的视频/音频源!")
        for u in units:
            tag = f"第{u['idx']}集 " if len(units) > 1 else ""
            print(f"  ── {tag}{u['name']}  offset={u['start_us']/1_000_000:.2f}s")

            # Step 7: 分离音频
            asr_source = u["path"]
            if do_ffmpeg and not asr_source.lower().endswith(
                    (".wav", ".mp3", ".m4a", ".aac", ".flac")):
                wav = os.path.join(workdir, f"audio_ep{u['idx']:02d}.wav")
                if run_ffmpeg_extract_audio(asr_source, wav):
                    asr_source = wav
                    print(f"  ✅ 提取到: {wav}")

            # Step 8: 识别字幕（集内相对时间轴）
            if do_whisper:
                with timed_step(f"ASR/ep{u['idx']}"):
                    cn_subs = run_asr_chain(asr_source, workdir, skip_local_ai)
            else:
                cn_subs = []
            if not cn_subs:
                print(f"  ⚠️ {tag}无字幕, 跳过")
                continue

            # 句间间隙回截：云端 ASR 段首尾相接（字幕常驻不掉字），按该集人声
            # 干声的静音区间把段尾裁回真实语句末尾，恢复句间字幕间隙
            vocals_for_sil = _find_episode_vocals(workdir, u["idx"], len(units))
            if vocals_for_sil:
                try:
                    import wave as _wave
                    with _wave.open(vocals_for_sil, "rb") as _wf:
                        vocal_dur_us = int(_wf.getnframes() / _wf.getframerate() * 1_000_000)
                    sils = detect_silences_ffmpeg(vocals_for_sil)
                    cn_subs = refine_subtitle_timing_by_silence(cn_subs, sils, vocal_dur_us)
                except Exception as e:
                    print(f"  ⚠️ 静音检测失败（保持原时间轴）: {type(e).__name__}: {e}")

            # 裁剪后源起点偏移：ASR 基于完整音源识别，裁点之前的字幕要丢弃，其余左移
            # 被裁点切断的残片若不足 0.25s，只剩闪字无意义，一并丢弃
            src_off = u["src_start_us"]
            MIN_CUT_FRAG_US = 250_000
            dropped = 0
            for s in cn_subs:
                rel_start = s["start_us"] - src_off
                rel_end = s["end_us"] - src_off
                if rel_end <= 0 or (rel_start < 0 and rel_end < MIN_CUT_FRAG_US):
                    dropped += 1
                    continue
                cn_all.append({
                    "start_us": max(0, rel_start) + u["start_us"],
                    "end_us": rel_end + u["start_us"],
                    "text": s["text"],
                    "_refined": s.get("_refined", False),
                })
            if dropped:
                print(f"  ✂️ {tag}丢弃裁点前字幕 {dropped} 条")

            # Step 9: 翻译本集字幕，时间轴同样加集偏移
            if do_translate and not skip_local_ai:
                with timed_step(f"翻译/ep{u['idx']}"):
                    en_texts = translate_texts([s["text"] for s in cn_subs], workdir)
                if not en_texts:
                    # 立即修复：GLM 429 多为瞬时抖动，45s 后全链重试一次
                    print(f"  ⚠️ {tag}翻译全链失败，45s 后立即重试一次")
                    time.sleep(45)
                    en_texts = translate_texts([s["text"] for s in cn_subs], workdir)
                if not en_texts:
                    print(f"  ✗ {tag}所有翻译渠道失败, 本集英文字幕留空待修复轮")
                    translation_gaps.append({
                        "idx": u["idx"], "tag": tag.strip(),
                        "cn_subs": [dict(s) for s in cn_subs],
                        "src_off": src_off, "ep_start_us": u["start_us"],
                    })
                    continue
                for s, t in zip(cn_subs, en_texts):
                    if not str(t).strip():
                        continue
                    rel_start = s["start_us"] - src_off
                    rel_end = s["end_us"] - src_off
                    if rel_end <= 0 or (rel_start < 0 and rel_end < MIN_CUT_FRAG_US):
                        continue
                    en_all.append({
                        "start_us": max(0, rel_start) + u["start_us"],
                        "end_us": rel_end + u["start_us"],
                        "text": str(t),
                        "_refined": s.get("_refined", False),
                    })

        # 翻译空位修复轮：主循环结束后对留空集重跑全链，最多 3 轮、轮间退避
        # 120s/300s —— GLM 夜间抖动常持续数分钟，单轮立即重试大概率仍撞墙；
        # Ollama 层有探针把关，不可用时自动跳过，不会白烧 600s 超时
        if translation_gaps and do_translate and not skip_local_ai:
            still_gap = list(translation_gaps)
            for rnd, wait in enumerate((0, 120, 300), 1):
                if not still_gap:
                    break
                if wait:
                    print(f"  ⏳ {len(still_gap)} 集仍未补翻，{wait}s 后进入第 {rnd} 轮...")
                    time.sleep(wait)
                print(f"━ 翻译修复轮(第{rnd}轮)：{len(still_gap)} 集待补 ━━━━━━━━━━━━━━━━━")
                still_gap = _repair_translation_round(still_gap, en_all, workdir)
            translation_gaps = still_gap
            if still_gap:
                gaps_file = os.path.join(workdir, "_translation_gaps.json")
                with open(gaps_file, "w", encoding="utf-8") as _f:
                    json.dump([{"idx": g["idx"], "tag": g["tag"]} for g in still_gap],
                              _f, ensure_ascii=False, indent=2)
                print(f"  ⚠️ 仍有 {len(still_gap)} 集未修复，已记录到 {gaps_file}，"
                      f"可用 repair_translation.py 事后补翻")
            print()

        # 所有集写进同两条原生字幕轨（中文隐藏 / 英文显示）
        cn_all.sort(key=lambda s: s["start_us"])
        en_all.sort(key=lambda s: s["start_us"])
        if cn_all:
            nm = add_text_segments(draft, cn_all, visible=False, sub_type=0)
            print(f"  ✅ 写入 {nm} 条中文字幕轨 (sub_type=0, 已隐藏)")
        if en_all:
            nm = add_text_segments(draft, en_all, visible=True, font_size=30,
                                   color=[1.0, 1.0, 1.0, 1.0], sub_type=5)
            print(f"  ✅ 写入 {nm} 条英文字幕 (sub_type=5)")
        if not cn_all and not en_all:
            print("  ⚠️ 没有任何字幕写入")
        print()

    # ━━━━ Step 10: 隐藏中文字幕 ━━━━
    if do_hide_cn:
        print("━ [10] 隐藏中文字幕 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        hidden = hide_text_track_by_language(draft, lang_cn=True)
        print(f"  🙈 隐藏了 {hidden} 个 text material")
        print()

    # ━━━━ Step 10.2: VAD 门控（人声干声非语音段压制 -40 dB）━━━━
    # 放在字幕写完之后：字幕尾裁的静音检测已在未门控干声上完成（Step 3），
    # 门控只影响最终播放/导出的音频，不改变字幕时间轴。
    if do_vad_gate and vocal_paths:
        _min_ratio = vad_min_speech_ratio
        if _min_ratio is None:
            _min_ratio = float((_ENV_PROFILE.get("vad_gate") or {})
                               .get("min_speech_ratio", 0.2))
        print("━ [10.2] VAD 门控（非语音段 -40 dB）━━━━━━━━━━━━━━━━━")
        try:
            import audio_gate as _ag
            with timed_step("VAD门控"):
                _grep = _ag.gate_files([p for _, p in vocal_paths],
                                       min_speech_ratio=_min_ratio)
            _ag.write_report(workdir, _grep)
            print(f"  ✅ 门控 {_grep['gated']}/{_grep['total']} 集"
                  f"（低语音跳过 {_grep['skipped_low_speech']}，异常 {_grep['err']}），"
                  f"累计 {_grep['total_reduction_db']} dB，耗时 {_grep['elapsed_s']}s")
            print(f"  📄 报告: {os.path.join(workdir, _ag.REPORT_NAME)}")
        except FileNotFoundError as e:
            print(f"  ⚠️ 跳过 VAD 门控: {e}")
        except Exception as e:
            print(f"  ⚠️ VAD 门控失败（保留原干声）: {type(e).__name__}: {e}")
        print()
    elif do_vad_gate and do_demucs and not skip_local_ai and vocal_mode != "audio_track":
        print("  ⚠️ vocal_mode=native_prefab：干声已转为剪映内部格式，跳过 VAD 门控")
        print()

    # ━━━━ 保存 ━━━━
    main_dur = get_main_video_duration(draft)
    if main_dur > 0:
        old_dur = draft.get("duration", 0)
        draft["duration"] = main_dur
        if old_dur != main_dur:
            print(f"📏 duration 更新: {old_dur/1_000_000:.2f}s → {main_dur/1_000_000:.2f}s")

    # ━━━━ Step 10.5: 字幕/音频终检（发布前 gate）━━━━
    _missing_eps_pending = []  # 残缺集软阻断：非空时发布后自动补翻+复验门控导出
    if do_subtitle_check:
        print("━ [10.5] 字幕/音频终检（中文残留 / 超长 / 结构 / 音频素材）━━")
        import verify_draft as _vd
        with timed_step("字幕/音频终检"):
            _rep, _hist = _vd.verify_and_fix(draft, workdir, rounds=3)
        _vd.write_report(workdir, os.path.basename(draft_name_or_path), _rep, _hist)
        if _vd.has_blocking(_rep):
            print(_vd.format_report(_rep))
            _blk = _vd.blocking_keys(_rep)
            _missing = {x["ep"] for x in _rep["issues"]["missing"]}
            _uncov = {x["ep"] for x in _rep["issues"]["uncovered"]}
            # 软阻断：阻断项只含「残缺集」及其派生的「未覆盖」（同一根因：
            # 翻译全链失败留下的空位）时照常保存发布，随后自动补翻复验
            if set(_blk) <= {"missing", "uncovered"} and _missing and _uncov <= _missing:
                _missing_eps_pending = sorted({x["ep"] for x in _rep["issues"]["missing"]})
                print(f"  ⚠️ 仅剩残缺集 {len(_missing_eps_pending)} 集 "
                      f"(eps={_missing_eps_pending})，草稿照常保存发布，"
                      f"随后自动补翻复验，通过才导出")
            else:
                print("  ✗ 终检未通过，已阻断发布（草稿未保存、未导出）")
                raise RuntimeError(
                    "字幕/音频终检未通过，发布已阻断（详见上方报告与 workdir 终检报告）")
        else:
            print("  ✅ 终检通过")
        print()

    # 封面必须在所有处理完成后、保存前写入——否则封面 mtime 早于草稿 update_time，
    # 剪映打开时会判定封面过期并用第一帧重生成（覆盖自定义封面）
    if cover_image or cover_extract_from_video:
        print(f"🖼️ 设置草稿封面(末尾): cover_image={cover_image}")
        set_draft_cover(
            dp_root,
            cover_image=cover_image,
            extract_from_video=cover_extract_from_video,
            extract_time_s=cover_extract_time_s,
            video_start_s=(_cover_start_for_cut(cover_extract_from_video, cut_us)
                           if cover_extract_from_video else 0.0),
        )
        # 本地图片封面还需挂载 composition 材质，否则剪映打开草稿时会用第一帧覆盖
        if cover_image:
            set_cover_composition(draft, dp_root, cover_image)

    jy_running = _jianying_running()
    locked = _draft_json_locked(draft_path)
    if jy_running and not locked:
        print(f"⚠️  剪映运行中(未编辑此草稿) — 可以写入, 但写完后请在剪映里关闭并重新打开该草稿以刷新显示")
    if locked:
        print(f"✗ 草稿 JSON 被剪映锁定! 请先在剪映中关闭此草稿编辑页或完全退出剪映")
        print(f"  draft_path={draft_path}")
        return None
    bak = backup_draft(draft_path)
    with timed_step("保存发布"):
        save_draft(draft, draft_path)
        if staged:
            dp_root, draft_path = publish_staged_draft(
                dp_root, draft_name_or_path, draft.get("duration", 0))
        _write_pipeline_backup(draft_path, draft)
        # 记录串流产物清单（供 stream_manifest.py 一键清理）
        try:
            import stream_manifest as _sm
            _src_dir = (os.path.dirname(os.path.abspath(video_paths[0]))
                        if video_paths else None)
            _sm.record(
                os.path.basename(os.path.dirname(draft_path)),
                source_dir=_src_dir,
                draft_dir=os.path.dirname(draft_path),
                jycache_dir=os.path.join(_src_dir, "__jycache__") if _src_dir else None,
                workdir=workdir,
            )
        except Exception as _me:
            print(f"  [warn] 记录串流清单失败: {_me}")
    print(f"💾 Draft saved! 备份: {bak}")
    print(f"   Post-state:\n{summarize_draft(draft)}")
    print()

    # ━━━━ Step 10.8: 残缺集自动补翻 + 复验（软阻断出口，通过才导出）━━━━
    if _missing_eps_pending:
        print("━ [10.8] 残缺集自动补翻 + 复验 ━━━━━━━━━━━━━━━━━━━━━━━━━")
        _pub_root = os.path.dirname(draft_path)
        _pub_name = os.path.basename(_pub_root)
        if os.path.normcase(os.path.dirname(_pub_root)) != os.path.normcase(DRAFT_ROOT):
            # 草稿不在剪映草稿目录（外部路径直改场景），repair_translation 按名加载不到
            print(f"  ⚠️ 草稿不在剪映草稿目录（{_pub_root}），无法自动补翻，不导出")
            _toast_safe("🎬 草稿待人工修复",
                        f"{draft_name_or_path}: 残缺集 {len(_missing_eps_pending)} 集，"
                        f"草稿已保存但未导出", icon="error")
            raise RuntimeError(
                "残缺集未修复且草稿不在剪映草稿目录，无法自动补翻：草稿已保存未导出")
        _repair = os.path.join(_HERE, "repair_translation.py")
        _eps_arg = ",".join(str(e) for e in _missing_eps_pending)
        with timed_step("残缺集补翻"):
            _rc = subprocess.call(
                [sys.executable, "-u", _repair, _pub_name, "--eps", _eps_arg])
        if _rc != 0:
            print(f"  ✗ 自动补翻脚本退出码 {_rc}")
        # 复验：repair 直接改的是已发布草稿文件，需从磁盘重读
        import verify_draft as _vd2
        try:
            with open(draft_path, encoding="utf-8") as _f:
                _re_draft = json.load(_f)
            _rep2, _hist2 = _vd2.verify_and_fix(_re_draft, workdir, rounds=1)
            _vd2.write_report(workdir, os.path.basename(draft_name_or_path),
                              _rep2, _hist2)
        except Exception as e:
            print(f"  ⚠️ 复验异常（按未通过处理，不导出）: {type(e).__name__}: {e}")
            _toast_safe("🎬 草稿待人工修复",
                        f"{draft_name_or_path}: 补翻复验异常 {str(e)[:60]}", icon="error")
            raise RuntimeError(f"残缺集补翻复验异常: {e}")
        if _vd2.has_blocking(_rep2):
            print(_vd2.format_report(_rep2))
            print("  ✗ 自动补翻后仍未过终检，不导出（草稿已保存，可稍后手动 "
                  "repair_translation.py 补翻）")
            _toast_safe("🎬 草稿待人工修复",
                        f"{draft_name_or_path}: 自动补翻后仍未过终检，草稿已保存未导出",
                        icon="error")
            raise RuntimeError(
                "残缺集自动补翻后仍未通过终检：草稿已保存发布但未导出，"
                "可稍后手动执行 repair_translation.py 补翻")
        print("  ✅ 补翻复验通过，继续导出")
        print()

    print()

    # ━━━━ Step 11: 导出 ━━━━
    if do_export:
        print("━ [11] 导出 (robust_auto_export) ━━━━━━━━━━━━━━━━━━━━━")
        try:
            from export_config import export_mp4_path, get_export_dir
            out_path = export_mp4_path(os.path.basename(dp_root or draft_name_or_path))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            exporter = os.path.join(
                os.path.dirname(_HERE), ".trae", "skills", "jianying-editor",
                "examples", "robust_auto_export.py")

            print(f"  🎯 草稿名: {draft_name_or_path}")
            print(f"  📁 输出路径: {out_path}")
            with timed_step("导出"):
                rc = subprocess.call(
                    [sys.executable, "-u", exporter, draft_name_or_path, out_path, "--move",
                     "--fallback-dir", get_export_dir()])
            if rc == 0:
                print(f"  ✅ 导出完成!")
            else:
                print(f"  ⚠️ 导出返回非零退出码: {rc}")
        except Exception as e:
            print(f"  ⚠️ 自动导出异常: {e}")
            import traceback
            traceback.print_exc()
            print("  💡 请在剪映里手动点 [导出] 按钮")
        print()

    _timing_summary(workdir, tag=os.path.basename(dp_root or draft_name_or_path))
    print(f"{'='*60}")
    print(f"  🎉 Pipeline 完成!")
    print(f"  📄 草稿 JSON: {draft_path}")
    print(f"{'='*60}\n")
    return draft


_JIANYING_DRAFT_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                    "JianyingPro", "User Data", "Projects", "com.lveditor.draft")


def _media_key(path):
    """路径归一化，用于跨草稿/工作目录比对（Windows 下大小写与分隔符不敏感）。"""
    return os.path.normcase(os.path.abspath(str(path)))


def referenced_media_paths():
    """扫描所有草稿，收集仍被 audio 材质引用的文件路径。

    剪映草稿只记录素材绝对路径，不会复制音频进草稿；清理工作目录前必须先
    知道哪些文件仍被引用，否则草稿会变成指向不存在文件的空轨。
    任一草稿解析失败时返回 None（调用方必须放弃清理，不能用残缺的引用表删文件）。
    """
    refs = set()
    if not os.path.isdir(_JIANYING_DRAFT_ROOT):
        return refs
    for name in os.listdir(_JIANYING_DRAFT_ROOT):
        jp = os.path.join(_JIANYING_DRAFT_ROOT, name, "draft_content.json")
        if not os.path.isfile(jp):
            continue
        try:
            with open(jp, encoding="utf-8") as f:
                draft = json.load(f)
        except (OSError, ValueError):
            print(f"  ⚠ 草稿读取失败，无法确认引用关系: {os.path.basename(name)}")
            return None
        for m in (draft.get("materials") or {}).get("audios") or []:
            p = m.get("path")
            if p:
                refs.add(_media_key(p))
    return refs


def clean_workdir(workdir, protect_referenced=True):
    """清理 workdir 中的人声分离中间产物。

    protect_referenced=True（默认）：先扫描全部草稿，仍被引用的文件、
    以及内部还有被引用文件的目录，一律跳过。草稿只存素材路径不复制音频，
    旧实现按通配符直接删 demucs_ep*，会删掉草稿正在播放的干声。
    保留 _batch_state.json（断点状态机）、_*.json（报告）与 *.log。
    被剪映等进程锁定的文件会跳过并列入 locked。
    返回 {"freed_mb": float, "locked": [路径], "kept": [路径]}
    """
    prot = referenced_media_paths() if protect_referenced else set()
    if prot is None:
        print("  ⚠ 引用关系扫描不完整，已跳过清理（不删任何文件）")
        return {"freed_mb": 0.0, "locked": [], "kept": []}
    patterns = ["demucs_ep*", "htdemucs", "htdemucs_ft",
                "audio_ep*.wav", "demucs_input_ep*.wav",
                "audio_extracted.wav", "extract_input.wav"]
    freed = 0.0
    locked, kept = [], []
    for pat in patterns:
        for p in glob.glob(os.path.join(workdir, pat)):
            if os.path.isdir(p):
                files = [f for f in pathlib.Path(p).rglob("*") if f.is_file()]
                if any(_media_key(f) in prot for f in files):
                    kept.append(p)
                    continue
                try:
                    size = sum(f.stat().st_size for f in files)
                    shutil.rmtree(p)
                    freed += size
                except OSError as e:
                    print(f"  ⚠ 跳过被占用的目录: {os.path.basename(p)} ({e.strerror})")
                    locked.append(p)
            else:
                if _media_key(p) in prot:
                    kept.append(p)
                    continue
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                except OSError as e:
                    print(f"  ⚠ 跳过被占用的文件: {os.path.basename(p)} ({e.strerror})")
                    locked.append(p)
    msg = f"  🧹 workdir 已清理: 释放 {freed / 1048576:.1f} MB"
    if kept:
        msg += f"，保留 {len(kept)} 个仍被草稿引用的产物"
    if locked:
        msg += f"，{len(locked)} 个被占用待清"
    print(msg)
    return {"freed_mb": freed / 1048576, "locked": locked, "kept": kept}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="JianYing Full-Local Pipeline (方案B: 注入已有草稿)")
    parser.add_argument("draft", nargs="?", default=None,
                        help="已有草稿名称或路径 (用剪映手动新建的草稿)")
    parser.add_argument("--video", action="append", default=None,
                        help="视频文件路径 (可多次指定, 注入到草稿主轨拼接)")
    parser.add_argument("--cut", default=None, type=float,
                        help="裁剪此时间点(秒)之前(从视频开头计时, 支持串流), 不指定则不裁剪")
    parser.add_argument("--workdir", default=None, help="工作目录")
    parser.add_argument("--skip-ai", action="store_true",
                        help="跳过 demucs/whisper/翻译 等本地AI")
    parser.add_argument("--no-export", action="store_true", help="跳过导出")
    parser.add_argument("--no-demucs", action="store_true", help="跳过人声分离")
    parser.add_argument("--no-presets", action="store_true", help="跳过所有预设")
    parser.add_argument("--presets", default=None, metavar="'名1,名2:0.03'",
                        help="显式指定预设(逗号分隔, 名字后可带:滤镜透明度); 优先级高于 presets.json 与前缀自动发现")
    parser.add_argument("--list-presets", action="store_true",
                        help="列出本机剪映预设库及当前生效来源后退出")
    parser.add_argument("--no-asr", action="store_true", help="跳过字幕识别")
    parser.add_argument("--no-translate", action="store_true", help="跳过翻译")
    parser.add_argument("--no-hide-cn", action="store_true", help="不隐藏中文字幕")
    parser.add_argument("--skip-subtitle-check", action="store_true",
                        help="跳过发布前字幕终检（中文残留/超长/结构）")
    parser.add_argument("--no-vad-gate", action="store_true",
                        help="跳过 VAD 门控（默认开启：干声非语音段压 -40 dB）")
    args = parser.parse_args()

    if args.list_presets:
        avail = list_available_presets()
        resolved, src = resolve_presets()
        resolved_names = {n for n, _ in resolved}
        print("本机剪映组合预设库:")
        for n in avail:
            marks = []
            if n in resolved_names:
                marks.append("将生效")
            if n.startswith(PRESET_PREFIX):
                marks.append("前缀匹配")
            print(f"  {'✅' if marks else '  '} {n}" + (f"  ({', '.join(marks)})" if marks else ""))
        if not avail:
            print("  (空) — 在剪映中框选元素 → 保存为我的预设 后此处可见")
        print(f"\n当前解析来源: {src or '无（本次将不使用预设）'}")
        print(f"生效预设: {[n for n, _ in resolved] or '无'}")
        print("提示: --presets '名1,名2:0.03' 可临时覆盖; 个人固定配置写 pipeline/presets.json")
        sys.exit(0)

    if not args.draft:
        parser.error("必须指定草稿名（或使用 --list-presets 查看预设库）")

    cut_us = int(args.cut * 1_000_000) if args.cut is not None else None

    # 单集直调 toast（batch_runner 进程内调用 run_pipeline 不经此处，不会重复通知）
    try:
        sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))
        from utils.notifier import toast as _toast
    except Exception:
        def _toast(*a, **k):  # noqa: ANN001
            pass

    try:
        run_pipeline(
            draft_name_or_path=args.draft,
            video_paths=args.video,
            cut_us=cut_us,
            workdir=args.workdir,
            skip_local_ai=args.skip_ai,
            do_export=not args.no_export,
            do_demucs=not args.no_demucs,
            do_vad_gate=not args.no_vad_gate,
            do_presets=not args.no_presets,
            presets=parse_presets_arg(args.presets) if args.presets else None,
            do_ffmpeg=True,
            do_whisper=not args.no_asr,
            do_translate=not args.no_translate,
            do_hide_cn=not args.no_hide_cn,
            do_subtitle_check=not args.skip_subtitle_check,
        )
        _toast("🎬 单集草稿完成", str(args.draft))
    except Exception as e:
        _toast("🎬 单集草稿失败", f"{args.draft}: {str(e)[:80]}", icon="error")
        raise
