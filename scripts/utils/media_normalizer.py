import os
import json
import subprocess
from typing import Optional

# pythonw 托管下每起一个控制台子进程都会弹黑框：ffmpeg/ffprobe 一律隐藏
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _norm_output_path(input_path: str) -> str:
    abs_in = os.path.abspath(input_path)
    parent = os.path.dirname(abs_in)
    stem, _ = os.path.splitext(os.path.basename(abs_in))
    cache_dir = os.path.join(parent, "__jycache__")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{stem}.__jy_norm__.mp4")


def _tmp_output_path(dst: str) -> str:
    """转封装的临时输出路径。

    必须以 .mp4 结尾：ffmpeg 靠扩展名推断封装格式，写成 `.part` 会直接报
    “Unable to choose an output format”。
    """
    return dst + ".tmp.mp4"


def _is_cache_fresh(src: str, dst: str) -> bool:
    if not os.path.exists(dst):
        return False
    try:
        if os.path.getsize(dst) <= 0:  # 0 字节残片一律视为不新鲜
            return False
        return os.path.getmtime(dst) >= os.path.getmtime(src)
    except OSError:
        return False


def _probe_video(input_path: str) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt",
        "-of",
        "json",
        input_path,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        return {}
    try:
        streams = json.loads(proc.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return {}
    return streams[0] if streams else {}


_STANDARD_DIMS = {(1080, 1920), (1920, 1080)}


def should_normalize_video_for_jianying(input_path: str) -> bool:
    info = _probe_video(input_path)
    if not info:
        return False
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    # 标准画布尺寸（1080x1920 / 1920x1080）虽然宽不是 16 的倍数，但剪映完全支持，
    # 直接放行，避免对已规范化过的素材二次转码。
    if (
        info.get("codec_name") == "h264"
        and info.get("pix_fmt") == "yuv420p"
        and (width, height) in _STANDARD_DIMS
    ):
        return False
    return (
        info.get("codec_name") != "h264"
        or info.get("pix_fmt") != "yuv420p"
        or width <= 0
        or height <= 0
        or width % 16 != 0
        or height % 2 != 0
    )


def normalize_video_for_jianying(input_path: str, force: bool = False) -> Optional[str]:
    """
    Convert video to JianYing-friendly MP4 before timeline import.

    Output profile:
    - Video: H.264 (libx264), yuv420p
    - Audio: AAC (optional if source has audio)
    - Geometry: 1920x1080 landscape OR 1080x1920 portrait (auto-detect)
    """
    src = os.path.abspath(input_path)
    if not os.path.exists(src):
        return None
    if not force and not should_normalize_video_for_jianying(src):
        return src

    dst = _norm_output_path(src)
    if _is_cache_fresh(src, dst):
        return dst

    # 先写临时文件再原子改名：进程中途被 kill 时不会留下半截 __jy_norm__.mp4
    # 注意临时名必须以 .mp4 结尾，否则 ffmpeg 无法按扩展名推断封装格式
    tmp_dst = _tmp_output_path(dst)
    try:
        if os.path.exists(tmp_dst):
            os.remove(tmp_dst)
    except OSError:
        pass

    info = _probe_video(src)
    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    if w > 0 and h > 0 and h > w:
        tgt_w, tgt_h = 1080, 1920
    else:
        tgt_w, tgt_h = 1920, 1080

    scale_pad = f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        src,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        scale_pad,
        "-r",
        "30",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        tmp_dst,
    ]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              creationflags=_NO_WINDOW)
    except FileNotFoundError:
        print("❌ FFmpeg not found. Cannot normalize video for JianYing import.")
        return None
    except Exception as e:
        print(f"❌ Video normalization failed: {e}")
        return None

    if proc.returncode != 0 or not os.path.exists(tmp_dst):
        err = (proc.stderr or proc.stdout or "").strip()
        print(f"❌ Video normalization failed (ffmpeg={proc.returncode}): {err}")
        try:
            os.remove(tmp_dst)
        except OSError:
            pass
        return None

    os.replace(tmp_dst, dst)
    return dst


def normalize_webm_for_jianying(input_path: str) -> Optional[str]:
    return normalize_video_for_jianying(input_path, force=True)
