import sys, os, json, time, re, glob, subprocess
sys.stdout.reconfigure(encoding="utf-8")

FFMPEG = r"D:\AI_Deps\tools\ffmpeg\bin\ffmpeg.exe" if os.path.exists(r"D:\AI_Deps\tools\ffmpeg\bin\ffmpeg.exe") else "ffmpeg"

VIDEO_DIR = r"D:\短剧\10-六零糙汉：我靠打猎养全家"
COVER_DIR = os.path.join(VIDEO_DIR, "covers")

if not os.path.exists(COVER_DIR):
    os.makedirs(COVER_DIR)

with open("audio_peak_results.json", "r", encoding="utf-8") as f:
    peak_data = json.load(f)

peak_map = {d["episode"]: d for d in peak_data}


def ep_num(f):
    m = re.search(r"第\s*(\d+)\s*集", f)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)", f)
    return int(m.group(1)) if m else 999999


def save_frame(video_path, t, out_path):
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-ss", f"{max(0, t):.2f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True, timeout=60,
        )
        return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception as e:
        print(f"    截图失败: {e}")
        return False


exts = ["*.mp4", "*.mov", "*.mkv", "*.avi"]
video_files = []
for ext in exts:
    video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext)))
    video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext.upper())))

video_files = sorted(set(video_files), key=lambda p: ep_num(os.path.basename(p)))

print(f"共 {len(video_files)} 集，开始截取冲突点封面帧...\n")

t0 = time.time()
saved = 0
skipped = 0

for i, vf in enumerate(video_files):
    en = ep_num(os.path.basename(vf))
    info = peak_map.get(en, {})
    conflict_time = info.get("conflict_time", 2.0)

    if conflict_time <= 0.5:
        print(f"[{i+1}/{len(video_files)}] 第{en:>2}集: 开头就是高潮 → 跳过截图")
        skipped += 1
        continue

    target_t = max(0, conflict_time - 0.25)
    out_name = f"cover_ep{en:02d}.jpg"
    out_path = os.path.join(COVER_DIR, out_name)

    t1 = time.time()
    ok = save_frame(vf, target_t, out_path)
    elapsed = time.time() - t1

    if ok:
        size_kb = os.path.getsize(out_path) / 1024
        print(f"[{i+1}/{len(video_files)}] 第{en:>2}集: 截@{target_t:.1f}s → {out_name} ({size_kb:.0f}KB, {elapsed:.1f}s)")
        saved += 1
    else:
        print(f"[{i+1}/{len(video_files)}] 第{en:>2}集: 截图失败")

total = time.time() - t0
print(f"\n完成: 保存 {saved} 张封面 | 跳过 {skipped} 集（开头无片头）| 耗时 {total:.0f}s")
print(f"封面目录: {COVER_DIR}")
