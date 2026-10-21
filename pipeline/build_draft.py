import os, sys, glob, json, uuid, copy
sys.stdout.reconfigure(encoding="utf-8")

current_dir = os.path.dirname(os.path.abspath(__file__))
skill_root = os.path.abspath(".trae/skills/jianying-editor")
scripts_path = os.path.join(skill_root, "scripts")
sys.path.insert(0, scripts_path)

from jy_wrapper import JyProject
import pyJianYingDraft as draft

BASE = r"D:\短剧\10-六零糙汉：我靠打猎养全家"
PRESETS = r"C:\Users\唐唐\AppData\Local\JianyingPro\User Data\Presets\Combination\Presets"
DRAFT_NAME = "六零糙汉_精剪版_v2"
CLIP_START_SEC = 19
WHITEBOARD_ALPHA = 0.05

def natural_sort_key(s):
    import re
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', s)]

print("=" * 60)
print(f"Step 1: 生成基础草稿 - {DRAFT_NAME}")
print("=" * 60)

project = JyProject(DRAFT_NAME, overwrite=True, width=1080, height=1920)

episodes = sorted(glob.glob(os.path.join(BASE, "第*集.mp4")), key=lambda p: natural_sort_key(os.path.basename(p)))
print(f"找到 {len(episodes)} 集 (自然排序)")

from moviepy import VideoFileClip

for i, ep in enumerate(episodes):
    ep_name = os.path.basename(ep)
    with VideoFileClip(ep) as vc:
        total_dur = vc.duration

    avail = max(0, total_dur - CLIP_START_SEC)
    if avail <= 1:
        print(f"  [{i+1}] {ep_name}: 剪掉{CLIP_START_SEC}s后剩 {avail:.1f}s，跳过")
        continue

    track_name = "VideoTrack"
    cur_end_us = project.get_track_duration(track_name)
    cur_end_sec = cur_end_us / 1_000_000
    dur_us = int(avail * 1_000_000)

    seg = project.add_media_safe(
        media_path=ep,
        target_start=f"{cur_end_sec:.6f}s",
        duration_us=dur_us,
        track_name=track_name,
        source_start=f"{CLIP_START_SEC}s",
    )
    actual_end = seg.target_timerange.end / 1_000_000
    print(f"  [{i+1}] {ep_name}: 源19s剪{avail:.1f}s → 时间轴 {cur_end_sec:.2f}s~{actual_end:.2f}s")

total_us = project.get_track_duration(track_name)
print(f"\n基础草稿生成完成！总时长: {total_us/1_000_000:.1f}s ({total_us/60_000_000:.1f}min)")
project.save()
print("✅ 基础草稿已保存")

print("\n" + "=" * 60)
print("Step 2: JSON层面 merge 预设")
print("=" * 60)

def load_draft_content(draft_dir):
    with open(os.path.join(draft_dir, "draft_content.json"), "r", encoding="utf-8") as f:
        return json.load(f)

def save_draft_content(draft_dir, data):
    with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def new_uuid():
    return uuid.uuid4().hex.upper()

def merge_preset_into_main(main_data, preset_dir, total_duration_us):
    dc = os.path.join(preset_dir, "preset_draft", "draft_content.json")
    if not os.path.exists(dc):
        return

    with open(dc, "r", encoding="utf-8") as f:
        preset_data = json.load(f)

    inner_drafts = preset_data.get("materials", {}).get("drafts", [])
    if not inner_drafts:
        return

    for inner in inner_drafts:
        id_map = {}
        def _map(old):
            if old not in id_map:
                id_map[old] = new_uuid()
            return id_map[old]

        inner_draft = inner.get("draft", {})
        for track in inner_draft.get("tracks", []):
            tc = copy.deepcopy(track)
            tc["id"] = _map(tc.get("id", ""))
            for seg in tc.get("segments", []):
                seg["id"] = _map(seg.get("id", ""))
                if "material_id" in seg:
                    seg["material_id"] = _map(seg["material_id"])
                seg["target_timerange"] = {"start": 0, "duration": total_duration_us}
                if "source_timerange" in seg:
                    seg["source_timerange"] = {"start": 0, "duration": total_duration_us}
            main_data["tracks"].append(tc)

        for mat_type, mat_list in inner_draft.get("materials", {}).items():
            if mat_type not in main_data["materials"]:
                main_data["materials"][mat_type] = []
            for mat in mat_list:
                mc = copy.deepcopy(mat)
                mc["id"] = _map(mc.get("id", ""))
                main_data["materials"][mat_type].append(mc)
            print(f"    +materials.{mat_type}: {len(mat_list)}")

draft_root = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "JianyingPro", "User Data", "Projects", "com.lveditor.draft",
)
main_dir = os.path.join(draft_root, DRAFT_NAME)
main_data = load_draft_content(main_dir)
main_data["duration"] = total_us

preset_names = ["logo文字", "三连~", "爆款推荐", "白板滤镜"]
for pn in preset_names:
    preset_dir = os.path.join(PRESETS, pn)
    if os.path.isdir(preset_dir):
        print(f"\n  合并预设: {pn}")
        merge_preset_into_main(main_data, preset_dir, total_us)

print("\n" + "=" * 60)
print("Step 3: 设置白板滤镜不透明度 ≤5%")
print("=" * 60)

wb_count = 0
for t in main_data.get("tracks", []):
    for seg in t.get("segments", []):
        mid = seg.get("material_id", "")
        for mat in main_data.get("materials", {}).get("videos", []):
            if mat.get("id") == mid and ("onlineMaterial" in mat.get("path", "") or "Cache" in mat.get("path", "")):
                cs = seg.get("clip_settings", {})
                cs["alpha"] = WHITEBOARD_ALPHA
                seg["clip_settings"] = cs
                wb_count += 1
                print(f"  ✅ 白板滤镜 seg: alpha={WHITEBOARD_ALPHA}")

if wb_count == 0:
    print("  ⚠️ 未找到白板滤镜 segment")

print("\n" + "=" * 60)
print("Step 4: 保存最终草稿")
print("=" * 60)

save_draft_content(main_dir, main_data)

final = load_draft_content(main_dir)
print(f"时长: {final.get('duration',0)/1_000_000:.1f}s")
print(f"轨道: {len(final.get('tracks', []))}")
for i, t in enumerate(final.get("tracks", [])):
    print(f"  轨道[{i}] type={t.get('type','?')} segs={len(t.get('segments',[]))}")
for mt, ml in final.get("materials", {}).items():
    if ml:
        print(f"  materials.{mt}: {len(ml)}")

print(f"\n🎉 完成！请打开剪映查看草稿: {DRAFT_NAME}")
