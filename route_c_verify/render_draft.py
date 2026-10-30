"""Route C 验证：不用剪映，直接用 ffmpeg 把 pipe_v3_final 草稿渲染成 MP4。

渲染内容（与剪映草稿逐轨对应）:
  - 主视频轨: 1080x1920@30 (原声 vol=1.0)
  - 人声轨 vocals.wav (vol=1.0) + 伴奏轨 no_vocals.wav (vol=0.8)
  - 复合片段(logo文字): "荒原狩獵短劇社" 浅黄填充+深棕描边+橙棕投影, 底部
  - 贴纸1 新品推荐: 序列帧动画(8帧16fps循环), 左上
  - 贴纸2 三连: GIF动画循环, 右上
  - 滤镜轨: 白场PNG 旋转90° alpha=0.03 全覆盖
  - 英文字幕轨: 32条 drawtext (白色, 无描边, 居中——位置待与剪映预览校准)

坐标系换算（已从 draft_cover.jpg 实证）:
  JianYing transform (tx,ty) -> 像素中心: cx = W/2 + tx*W/2, cy = H/2 - ty*H/2 (ty正=上)
  字号换算（封面实测）: 像素字高 ≈ size * 3.0
"""
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, "work")
OUT = os.path.join(HERE, "out")

DRAFT_JSON = os.path.join(
    os.environ["LOCALAPPDATA"],
    "JianyingPro", "User Data", "Projects", "com.lveditor.draft",
    "pipe_v3_final", "draft_content.json",
)


def _resolve_font_src() -> str:
    """剪映自带字体（zh-hans.ttf）：JY_JIANYING_DIR → 系统中文字体兜底，找不到返回空串。"""
    jy_dir = os.environ.get("JY_JIANYING_DIR", "").strip()
    if jy_dir:
        p = os.path.join(jy_dir, "Resources", "Font", "SystemFont", "zh-hans.ttf")
        if os.path.isfile(p):
            return p
    fonts = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        p = os.path.join(fonts, name)
        if os.path.isfile(p):
            return p
    return ""


FONT_SRC = _resolve_font_src()

CANVAS_W, CANVAS_H = 1080, 1920
FONT_K = 3.0            # 普通文字(复合片段内): 像素字高 = size * 3.0（封面实测校准）
SUB_FONT_K = 5.7        # 字幕轨(type=subtitle): 像素字高 = size * 5.7（剪映导出帧实测 131px/23）
SUB_MAX_W = 620 / 1080  # 字幕行宽上限: 草稿 line_max_width=0.75, 但剪映排版引擎字距比 freetype 宽
                        # 实测前缀宽: "[EN] 不过"/"[EN] 这个"=560px, "[EN] 不过说"/"[EN] 这个是"=691px
                        # 阈值取 620px(0.574) 可让剪映的所有换行点全部复现
DRAFT_DURATION = 60.76  # 草稿时长 s

US = 1_000_000


def ensure_ffmpeg():
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    import glob
    for p in glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft",
                                    "WinGet", "Packages", "*ffmpeg*", "ffmpeg-*", "bin")):
        os.environ["PATH"] = os.environ["PATH"] + os.pathsep + p
        return


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd)[:200])
    subprocess.run(cmd, check=True, **kw)


def jy_center(tx, ty):
    """JianYing transform -> 主画布像素中心点"""
    return (CANVAS_W / 2 + tx * CANVAS_W / 2, CANVAS_H / 2 - ty * CANVAS_H / 2)


def main():
    ensure_ffmpeg()
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    with open(DRAFT_JSON, encoding="utf-8") as f:
        draft = json.load(f)
    mats = draft["materials"]

    mat_by_id = {}
    for k, v in mats.items():
        if isinstance(v, list):
            for m in v:
                if isinstance(m, dict) and m.get("id"):
                    mat_by_id[m["id"]] = (k, m)

    tracks = draft["tracks"]

    # ---------- 1. 收集素材 ----------
    main_video = mat_by_id[tracks[0]["segments"][0]["material_id"]][1]["path"]
    vocals = mat_by_id[tracks[1]["segments"][0]["material_id"]][1]["path"]
    bgm = mat_by_id[tracks[2]["segments"][0]["material_id"]][1]["path"]
    bgm_vol = tracks[2]["segments"][0].get("volume", 1.0)

    # 贴纸
    st1_seg = tracks[4]["segments"][0]
    st1_mat = mat_by_id[st1_seg["material_id"]][1]
    st2_seg = tracks[5]["segments"][0]
    st2_mat = mat_by_id[st2_seg["material_id"]][1]

    # 白场滤镜
    white_seg = tracks[6]["segments"][0]
    white_mat = mat_by_id[white_seg["material_id"]][1]

    # 复合片段（logo文字）
    inner_draft = mats["drafts"][0]["draft"]
    inner_seg = inner_draft["tracks"][1]["segments"][0]
    inner_mat = next(m for m in inner_draft["materials"]["texts"]
                     if m["id"] == inner_seg["material_id"])
    logo_text = json.loads(inner_mat["content"])["text"]
    logo_size = inner_mat["font_size"]
    itf = inner_seg["clip"]["transform"]
    logo_cx = (inner_draft["canvas_config"]["width"] / 2 + itf["x"] * inner_draft["canvas_config"]["width"] / 2) * (CANVAS_W / inner_draft["canvas_config"]["width"])
    logo_cy = (inner_draft["canvas_config"]["height"] / 2 - itf["y"] * inner_draft["canvas_config"]["height"] / 2) * (CANVAS_H / inner_draft["canvas_config"]["height"])
    logo_px = logo_size * FONT_K * (CANVAS_W / inner_draft["canvas_config"]["width"])

    # 英文字幕
    en_events = []
    for s in tracks[8]["segments"]:
        m = mat_by_id[s["material_id"]][1]
        c = json.loads(m["content"])
        tr = s["target_timerange"]
        en_events.append({
            "t0": tr["start"] / US,
            "t1": (tr["start"] + tr["duration"]) / US,
            "text": c["text"],
            "size": m["font_size"],
        })

    print(f"主视频: {main_video}")
    print(f"人声: {vocals}")
    print(f"伴奏: {bgm} vol={bgm_vol}")
    print(f"logo: {logo_text!r} @ ({logo_cx:.0f},{logo_cy:.0f}) px={logo_px:.0f}")
    print(f"英文字幕: {len(en_events)} 条")

    # ---------- 2. 准备资产 ----------
    frames_dir = os.path.join(WORK, "st1_frames")
    os.makedirs(frames_dir, exist_ok=True)
    sprite = os.path.join(st1_mat["path"], "SequenceMap.png")
    from PIL import Image
    im = Image.open(sprite)
    ani = json.load(open(os.path.join(st1_mat["path"], "ani_info.json"), encoding="utf-8"))
    for i, fr in enumerate(ani["frames"]):
        r = fr["frame"]
        im.crop((r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])).save(
            os.path.join(frames_dir, f"f{i}.png"))
    print(f"贴纸1序列帧: {len(ani['frames'])} 帧")

    gif_dst = os.path.join(WORK, "sticker2.gif")
    shutil.copy2(os.path.join(st2_mat["path"], "final.gif"), gif_dst)
    white_dst = os.path.join(WORK, "white.png")
    shutil.copy2(white_mat["path"], white_dst)
    font_dst = os.path.join(WORK, "font.ttf")
    shutil.copy2(FONT_SRC, font_dst)

    subs_dir = os.path.join(WORK, "subs")
    os.makedirs(subs_dir, exist_ok=True)
    # 按 line_max_width 换行（用 PIL 实测字宽），drawtext 渲染多行
    from PIL import ImageFont
    sub_px = round(en_events[0]["size"] * SUB_FONT_K)
    pil_font = ImageFont.truetype(font_dst, sub_px)
    max_w = CANVAS_W * SUB_MAX_W
    for i, ev in enumerate(en_events):
        lines, cur_line = [], ""
        for ch in ev["text"]:
            if pil_font.getlength(cur_line + ch) > max_w and cur_line:
                lines.append(cur_line)
                cur_line = ch
            else:
                cur_line += ch
        if cur_line:
            lines.append(cur_line)
        with open(os.path.join(subs_dir, f"sub_{i:02d}.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

    # ---------- 3. 组装 filter_complex ----------
    c1 = st1_seg["clip"]
    s1_size = round(280 * c1["scale"]["x"])
    s1_cx, s1_cy = jy_center(c1["transform"]["x"], c1["transform"]["y"])
    c2 = st2_seg["clip"]
    s2_size = round(280 * c2["scale"]["x"])
    s2_cx, s2_cy = jy_center(c2["transform"]["x"], c2["transform"]["y"])
    wc = white_seg["clip"]
    w_scale = wc["scale"]["x"]
    w_alpha = wc["alpha"]

    print(f"贴纸1: size={s1_size} 中心=({s1_cx:.0f},{s1_cy:.0f})")
    print(f"贴纸2: size={s2_size} 中心=({s2_cx:.0f},{s2_cy:.0f})")

    fc = []
    fc.append(f"[0:v]fps=30,scale={CANVAS_W}:{CANVAS_H},setsar=1[base]")
    # 白场滤镜: 旋转90° -> 放大1.769 -> alpha 0.03 -> 居中覆盖
    fc.append(f"[3:v]rotate=90*PI/180,scale=trunc({CANVAS_W}*{w_scale}/2)*2:trunc({CANVAS_H}*{w_scale}/2)*2,"
              f"format=rgba,colorchannelmixer=aa={w_alpha}[wf]")
    fc.append(f"[base][wf]overlay=(W-w)/2:(H-h)/2[b1]")
    # 贴纸1: 序列帧 16fps
    fc.append(f"[4:v]scale={s1_size}:{s1_size}[s1]")
    fc.append(f"[b1][s1]overlay={s1_cx - s1_size/2:.1f}:{s1_cy - s1_size/2:.1f}[b2]")
    # 贴纸2: GIF
    fc.append(f"[5:v]scale={s2_size}:{s2_size}[s2]")
    fc.append(f"[b2][s2]overlay={s2_cx - s2_size/2:.1f}:{s2_cy - s2_size/2:.1f}[b3]")
    # logo 文字: 浅黄填充 + 深棕描边 + 橙棕投影
    fc.append(
        f"[b3]drawtext=fontfile=font.ttf:textfile=logo.txt:fontsize={logo_px:.0f}:"
        f"fontcolor=0xFEFED2:borderw={logo_px*0.05:.1f}:bordercolor=0x641809:"
        f"shadowcolor=0xB23510@0.9:shadowx=2.1:shadowy=2.1:"
        f"x={logo_cx:.1f}-text_w/2:y={logo_cy:.1f}-text_h/2[b4]"
    )
    # 英文字幕: 白色无描边, 垂直居中(剪映无transform字幕的默认位), 多行 line_spacing≈0.02*H
    cur = "b4"
    for i, ev in enumerate(en_events):
        nxt = f"bs{i}"
        fc.append(
            f"[{cur}]drawtext=fontfile=font.ttf:textfile=subs/sub_{i:02d}.txt:"
            f"fontsize={sub_px}:fontcolor=white:line_spacing=38:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"enable='between(t,{ev['t0']:.3f},{ev['t1']:.3f})'[{nxt}]"
        )
        cur = nxt
    # 补齐时长到草稿 duration（尾帧克隆）
    fc.append(f"[{cur}]tpad=stop_mode=clone:stop_duration=1,trim=0:{DRAFT_DURATION},setpts=PTS-STARTPTS[vout]")
    # 音频: 视频原声1.0 + 人声1.0 + 伴奏0.8
    fc.append(f"[2:a]volume={bgm_vol}[bgm]")
    fc.append(f"[0:a][1:a][bgm]amix=inputs=3:duration=longest:normalize=0,apad,atrim=0:{DRAFT_DURATION}[aout]")

    with open(os.path.join(WORK, "logo.txt"), "w", encoding="utf-8") as f:
        f.write(logo_text)

    filter_script = os.path.join(WORK, "filter.txt")
    with open(filter_script, "w", encoding="utf-8") as f:
        f.write(";\n".join(fc))
    with open(filter_script, encoding="utf-8") as f:
        filter_graph = f.read()  # ffmpeg9 已移除 -filter_complex_script, 内联传入

    out_mp4 = os.path.join(OUT, "pipe_v3_final_ffmpeg.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-i", main_video,                      # 0
        "-i", vocals,                          # 1
        "-i", bgm,                             # 2
        "-loop", "1", "-i", white_dst,         # 3
        "-stream_loop", "-1", "-framerate", "16", "-i", os.path.join(frames_dir, "f%d.png"),  # 4
        "-ignore_loop", "0", "-i", gif_dst,    # 5
        "-filter_complex", filter_graph,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "192k",
        "-t", str(DRAFT_DURATION),
        out_mp4,
    ]
    run(cmd, cwd=WORK)
    print(f"\n✅ 输出: {out_mp4}")

    # ---------- 4. 抽帧 ----------
    for t in (0.5, 1.0, 10.0, 30.0, 55.0, 60.5):
        fp = os.path.join(OUT, f"frame_{t:05.1f}s.png")
        run(["ffmpeg", "-y", "-ss", str(t), "-i", out_mp4,
             "-frames:v", "1", fp])
    print("✅ 抽帧完成")


if __name__ == "__main__":
    main()
