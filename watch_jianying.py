"""
实时监控剪映操作时的草稿变化
用法：python watch_jianying.py <草稿名>
然后在剪映里操作，这个脚本会实时打印 JSON 变化
"""
import os, sys, time, json
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))
from utils.formatters import get_all_drafts

def find_draft(name):
    drafts = get_all_drafts()
    for d in drafts:
        if name in d.get("name", "") or name in d.get("path", ""):
            return d
    return None

def get_track_summary(json_path):
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            draft = json.load(f)
    except:
        return {}
    summary = {"tracks": [], "materials": {}}
    for t in draft.get("tracks", []):
        segs = t.get("segments", [])
        summary["tracks"].append({
            "type": t.get("type"),
            "segments": len(segs),
            "group_id": t.get("group_id", ""),
        })
    for mtype in ("videos", "audios", "texts", "euleries", "sticker_animations", "stickers", "videos_editor"):
        mats = draft.get("materials", {}).get(mtype, [])
        if mats:
            summary["materials"][mtype] = len(mats)
    return summary

def main():
    if len(sys.argv) < 2:
        print("用法: python watch_jianying.py <草稿名>")
        print("监控 %LOCALAPPDATA% 下的草稿目录变化")
        print()
        drafts = get_all_drafts()
        print("当前草稿列表:")
        for d in drafts:
            print(f"  {d.get('name', '?'):30s} {os.path.basename(d['path'])}")
        sys.exit(0)

    draft_name = sys.argv[1]
    draft = find_draft(draft_name)
    if not draft:
        print(f"❌ 找不到草稿: {draft_name}")
        sys.exit(1)

    json_path = os.path.join(draft["path"], "draft_content.json")
    print(f"✅ 监控草稿: {draft.get('name')}")
    print(f"   路径: {draft['path']}")
    print(f"   JSON: {json_path}")
    print()
    print("开始监控... 在剪映里操作吧！(Ctrl+C 停止)\n")

    last = get_track_summary(json_path)
    last_mtime = os.path.getmtime(json_path) if os.path.exists(json_path) else 0
    print(f"初始状态: tracks={len(last.get('tracks', []))}, materials={last.get('materials', {})}")
    print()

    # 同时监控整个草稿目录的文件变化
    file_snapshot = {}
    for root, dirs, files in os.walk(draft["path"]):
        for f in files:
            full = os.path.join(root, f)
            file_snapshot[full] = os.path.getsize(full)

    try:
        while True:
            time.sleep(1.0)
            
            # 检查 JSON 变化
            cur_mtime = os.path.getmtime(json_path) if os.path.exists(json_path) else 0
            if cur_mtime != last_mtime:
                last_mtime = cur_mtime
                cur = get_track_summary(json_path)
                
                # 对比
                changes = []
                old_tracks = last.get("tracks", [])
                new_tracks = cur.get("tracks", [])
                
                print(f"[{time.strftime('%H:%M:%S')}] 📝 JSON 变化!")
                
                for i, nt in enumerate(new_tracks):
                    ot = old_tracks[i] if i < len(old_tracks) else None
                    if ot is None:
                        changes.append(f"  ➕ 新轨道: type={nt['type']}, segments={nt['segments']}")
                    elif ot["segments"] != nt["segments"]:
                        changes.append(f"  📈 track[{i}]({nt['type']}): {ot['segments']}→{nt['segments']} segments")
                    elif ot.get("group_id") != nt.get("group_id"):
                        changes.append(f"  🔄 track[{i}]({nt['type']}): group_id changed")
                
                for mt in sorted(set(list(cur.get("materials", {}).keys()) + list(last.get("materials", {}).keys()))):
                    old_c = last.get("materials", {}).get(mt, 0)
                    new_c = cur.get("materials", {}).get(mt, 0)
                    if old_c != new_c:
                        changes.append(f"  📦 materials.{mt}: {old_c}→{new_c}")
                
                if not changes:
                    changes.append(f"  (JSON mtime changed but content summary same)")
                
                for c in changes:
                    print(c)
                print()
                last = cur

            # 检查新文件
            cur_files = {}
            for root, dirs, files in os.walk(draft["path"]):
                for f in files:
                    full = os.path.join(root, f)
                    try:
                        cur_files[full] = os.path.getsize(full)
                    except:
                        pass
            
            new_files = set(cur_files.keys()) - set(file_snapshot.keys())
            for nf in sorted(new_files):
                rel = os.path.relpath(nf, draft["path"])
                print(f"[{time.strftime('%H:%M:%S')}] 🆕 新文件: {rel} ({cur_files[nf]/1024:.1f}KB)")
            
            file_snapshot = cur_files

    except KeyboardInterrupt:
        print("\n\n停止监控")
        print(f"最终状态: {json.dumps(get_track_summary(json_path), indent=2, ensure_ascii=False)}")

if __name__ == "__main__":
    main()
