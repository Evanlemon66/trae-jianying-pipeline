"""Drama Cover Builder - 短剧封面生成 Skill 主脚本。

子命令:
  prepare           创建任务目录与 job.json（Stage 0，交接契约 v1.1）
  title             生成 3 版标题（Agent 调用，Ollama 离线兜底）
  art-from-video    ffmpeg select+thumbnail 从视频抽最佳帧
  compose-pillow    Pillow 排版合成（4 样式 × 2 尺寸族）

剪映草稿封面写入已下沉到 lib/jy_cover.py（pipeline 直接调用），不属于本 skill。
"""
import os, sys, json, uuid, shutil, subprocess, glob, argparse
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# 内部辅助
# ──────────────────────────────────────────────────────────────────

def _repo_root():
    """仓库根目录（skill 位于 <root>/.trae/skills/<name>/scripts）。"""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def _load_manifest_lib():
    """加载共享的 job.json 契约实现（lib/job_manifest.py）。"""
    lib_dir = os.path.join(_repo_root(), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import job_manifest
    return job_manifest

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


def _guess_ffprobe():
    return _find_tool("ffprobe")


def _guess_font():
    """候选中文字体（Windows 系统自带，字体目录取 %WINDIR%）。"""
    font_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    candidates = [
        os.path.join(font_dir, "msyhbd.ttc"),
        os.path.join(font_dir, "msyh.ttc"),
        os.path.join(font_dir, "simhei.ttf"),
        os.path.join(font_dir, "simsun.ttc"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


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


# ──────────────────────────────────────────────────────────────────
# 新增辅助：自动探测 / 冲突点抽帧
# ──────────────────────────────────────────────────────────────────

def detect_cover_from_dir(series_dir):
    """从剧集目录自动探测参考图（委托 lib/job_manifest.py，契约 §5）。"""
    try:
        jm = _load_manifest_lib()
    except ImportError:
        return None
    return jm.detect_reference_image(series_dir).get("path")


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

def _cmd_prepare(args):
    try:
        jm = _load_manifest_lib()
    except ImportError:
        print("[FAIL] 找不到 lib/job_manifest.py；请在仓库根目录运行，或确认 lib/ 完整")
        return 1

    synopsis = (args.synopsis or "").strip()
    if not synopsis and args.synopsis_file:
        if not os.path.isfile(args.synopsis_file):
            print(f"[FAIL] 简介文件不存在: {args.synopsis_file}")
            return 1
        with open(args.synopsis_file, encoding="utf-8") as f:
            synopsis = f.read().strip()
    if not synopsis and not args.series_dir:
        print("[FAIL] 需要 --synopsis、--synopsis-file，或提供含 简介.txt 的 --series-dir")
        return 1

    series = args.series
    if not series and args.series_dir:
        series = os.path.basename(os.path.normpath(args.series_dir))
    series = series or "未命名剧集"

    try:
        result = jm.create_job(
            series=series,
            synopsis=synopsis or None,
            series_dir=args.series_dir,
            reference=args.reference,
            video=args.video,
            out_dir=args.out,
            host=args.host,
            force=args.force,
        )
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1

    job = result["job"]
    print(f"[OK] 任务目录: {result['job_dir']}")
    print(f"     job.json: {result['job_path']}  revision={job['revision']}")
    print(f"     简介: {job['input']['synopsis_source']} -> {job['input']['synopsis']}")
    if job["input"]["reference_image"]:
        info = result["reference"]
        sha = job["input"]["reference_sha256"][:12]
        print(f"     参考图: {job['input']['reference_image']} <- {job['input']['reference_original']}"
              f"（{info['reason']}，sha256:{sha}）")
    else:
        print(f"     参考图: 未写入（{result['reference']['reason']}），请在 Stage 2 前补")
    if job["video"]["path"]:
        print(f"     视频: {job['video']['path']}")
    elif job["video"]["candidates_count"]:
        print(f"     视频: {job['video']['candidates_count']} 个候选（{job['video']['name_pattern']}），未选定")
    else:
        print("     视频: 未找到 mp4")
    slots = " / ".join(f"cover_{c['slot']}<-{c['title_id']}" for c in job["cover_map"])
    print(f"     默认映射: {slots}")
    print("     下一步: Stage 1 生成 12 条标题 -> 02_titles/titles.json")
    return 0


def _cmd_art_from_video(args):
    out = args.out or "best_frame.jpg"
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


def main():
    ap = argparse.ArgumentParser(prog="drama_cover.py",
        description="Drama Cover Builder — 短剧封面全链路生成")
    sub = ap.add_subparsers(dest="command", required=True)

    # --- prepare ---
    p_prep = sub.add_parser("prepare", help="创建任务目录与 job.json（Stage 0）")
    p_prep.add_argument("--series-dir", default=None, help="剧集目录，用于自动探测参考图")
    p_prep.add_argument("--series", default=None, help="剧名；默认取剧集目录名")
    p_prep.add_argument("--synopsis", default=None, help="简介原文")
    p_prep.add_argument("--synopsis-file", default=None, help="简介文本文件")
    p_prep.add_argument("--reference", default=None, help="显式指定参考图")
    p_prep.add_argument("--video", default=None, help="显式指定 mp4")
    p_prep.add_argument("--out", default=None,
                        help="任务目录；默认 <剧集目录>/cover_job，无剧集目录时 ./jobs/<剧名>")
    p_prep.add_argument("--host", default="trae", choices=["trae", "doubao", "other"])
    p_prep.add_argument("--force", action="store_true", help="覆盖已有 job.json")

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

    args = ap.parse_args()

    dispatch = {
        "prepare": _cmd_prepare,
        "title": _cmd_title,
        "art-from-video": _cmd_art_from_video,
        "compose-pillow": _cmd_compose_pillow,
    }
    rc = dispatch[args.command](args)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
