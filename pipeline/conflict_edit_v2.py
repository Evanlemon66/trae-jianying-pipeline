import os, sys, re, glob, json
sys.stdout.reconfigure(encoding="utf-8")

current_dir = os.path.dirname(os.path.abspath(__file__))
env_root = os.getenv("JY_SKILL_ROOT", "").strip()
skill_candidates = [
    env_root,
    os.path.join(current_dir, ".trae", "skills", "jianying-editor"),
    os.path.join(current_dir, ".agent", "skills", "jianying-editor"),
    os.path.join(current_dir, "skills", "jianying-editor"),
]

scripts_path = None
for p in skill_candidates:
    if p:
        ap = os.path.abspath(p)
        if os.path.exists(os.path.join(ap, "scripts", "jy_wrapper.py")):
            scripts_path = os.path.join(ap, "scripts")
            break

if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from jy_wrapper import JyProject

VIDEO_DIR = r"D:\短剧\10-六零糙汉：我靠打猎养全家"
COVER_DIR = os.path.join(VIDEO_DIR, "covers")

with open("audio_peak_results.json", "r", encoding="utf-8") as f:
    peak_data = json.load(f)

peak_map = {d["episode"]: d for d in peak_data}


def ep_num(f):
    m = re.search(r"第\s*(\d+)\s*集", f)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)", f)
    return int(m.group(1)) if m else 999999


def main():
    exts = ["*.mp4", "*.mov", "*.mkv", "*.avi"]
    video_files = []
    for ext in exts:
        video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext)))
        video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext.upper())))

    video_files = sorted(set(video_files), key=lambda p: ep_num(os.path.basename(p)))
    print(f"共 {len(video_files)} 集，使用音频能量检测精准裁剪\n")

    project = JyProject(project_name="六零糙汉：我靠打猎养全家_冲突点版", overwrite=True)

    cover_path = os.path.join(VIDEO_DIR, "六零糙汉：我靠打猎养全家.jpg")
    if os.path.exists(cover_path):
        print(f"[总封面] 添加 {os.path.basename(cover_path)} 作为视频开头 (3s)")
        project.add_media_safe(cover_path, duration="3s", track_name="VideoTrack")
    else:
        print("[总封面] 未找到封面 jpg，跳过")

    print()

    ok = 0
    fail = 0
    no_cut = 0
    did_cut = 0

    for i, vf in enumerate(video_files):
        en = ep_num(os.path.basename(vf))
        info = peak_map.get(en, {})
        cut_from = info.get("cut_from", 0)

        try:
            if cut_from > 0:
                project.add_media_safe(
                    vf,
                    source_start=f"{cut_from:.2f}s",
                    track_name="VideoTrack"
                )
                status = f"剪前 {cut_from:.2f}s → 冲突点@{info.get('conflict_time','?')}s"
                did_cut += 1
            else:
                project.add_media_safe(
                    vf,
                    track_name="VideoTrack"
                )
                status = "开头就是高潮，不剪"
                no_cut += 1

            ok += 1
            print(f"[{i+1}/{len(video_files)}] 第{en:>2}集 | {status}")

        except Exception as e:
            fail += 1
            print(f"[{i+1}/{len(video_files)}] 第{en:>2}集 FAIL: {e}")

    project.save()

    print(f"\n{'='*60}")
    print(f"完成! 成功 {ok} 集 / 失败 {fail} 集")
    print(f"  - 精准裁剪: {did_cut} 集")
    print(f"  - 不剪(开头即高潮): {no_cut} 集")
    print(f"草稿: 六零糙汉：我靠打猎养全家_冲突点版")
    print(f"\n剪映里打开即可看效果！")


if __name__ == "__main__":
    main()
