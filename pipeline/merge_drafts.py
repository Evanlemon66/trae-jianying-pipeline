"""合并多个独立草稿为一个串集草稿（字幕/贴纸/人声分离时间轴自动偏移）。

用法:
  python merge_drafts.py --drafts liuling_第11集 liuling_第12集 liuling_第13集 --out merged_ep11-13
  python merge_drafts.py --state <batch_state.json> --out merged_all
  python merge_drafts.py --dir <草稿目录> --out combined

关键处理:
- 在剪映草稿目录之外的 __jybuild__ 暂存区构建，校验通过后整体发布（剪映全程看不到半成品）
- 基础集(第一集)保持原样，不重生成任何 ID（segment 引用天然有效）
- 追加集逐集建 旧ID→新ID 映射，且新 ID 全局唯一，避免跨集碰撞
- 追加集必须重映射 segment 顶层 material_id（剪映 5.9 不在 clip 内）
- material 列表按 src 实际键全量克隆，不依赖白名单
- segment.target_timerange.start += 累积时长偏移
- Resources/audioAlg/*.wav 复制到目标草稿（文件名天然唯一）
- vocal_separation.production_path 重写为目标草稿路径占位符
- draft_meta_info.json 换新 draft_id/名称/时长，避免与源草稿冲突
- 写盘前校验零悬空引用、零重复 ID，校验失败直接终止
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import uuid as _uuid

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
_DRAFT_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
# 合并暂存区：放在剪映草稿目录之外（同盘），合并完再整体搬进去。
# 剪映只扫描 _DRAFT_ROOT，构建中的半成品不会被剪映看到，避免缓存中间状态导致后几集空白。
_STAGING_ROOT = os.path.join(os.path.dirname(_DRAFT_ROOT), "__jybuild__")
_PATH_PLACEHOLDER = "##_draftpath_placeholder_0E685133-18CE-45ED-8CB8-2904A212EC80_##"


def gen_uuid():
    return str(_uuid.uuid4()).upper()


def _copy_audioalg_files(src_draft_dir, dst_draft_dir):
    """把 src 的 Resources/audioAlg/ 下的裸 PCM 复制到 dst。文件名带 md5 天然唯一。"""
    src_alg = os.path.join(src_draft_dir, "Resources", "audioAlg")
    if not os.path.isdir(src_alg):
        return []
    dst_alg = os.path.join(dst_draft_dir, "Resources", "audioAlg")
    os.makedirs(dst_alg, exist_ok=True)
    copied = []
    for fn in os.listdir(src_alg):
        src = os.path.join(src_alg, fn)
        dst = os.path.join(dst_alg, fn)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            copied.append(fn)
    return copied


def _material_ids(draft):
    ids = set()
    for items in draft.get("materials", {}).values():
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    ids.add(item["id"])
    return ids


def _mat_lookup(draft):
    """material_id → (materials 键名, 材质对象)"""
    out = {}
    for key, items in draft.get("materials", {}).items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out[item["id"]] = (key, item)
    return out


def _track_signature(track, lookup):
    """轨道合并签名：同类型/同属性/同素材的轨道视为同一条轨，段级追加。"""
    kind, mat = "", {}
    for seg in track.get("segments") or []:
        mid = seg.get("material_id")
        if mid in lookup:
            kind, mat = lookup[mid]
            break
    ttype = track.get("type")
    if ttype == "text":
        return (ttype, track.get("attribute"), track.get("flag"), "text")
    if kind == "videos":
        path = mat.get("path") or ""
        if path.lower().endswith((".mp4", ".mov", ".mkv", ".flv", ".webm")):
            sub = "main"          # 各集主视频，归到同一条主轨
        elif path:
            sub = f"{mat.get('type')}:{os.path.basename(path)}"
        else:
            sub = f"empty:{mat.get('type')}"
    elif kind == "stickers":
        sub = "sticker:" + str(mat.get("name") or mat.get("resource_id"))
    elif kind == "effects":
        sub = "effect:" + str(mat.get("name"))
    elif kind == "audios":
        sub = "audio:" + str(mat.get("name") or mat.get("type"))
    else:
        sub = kind or "unknown"
    flag = track.get("flag")
    if ttype == "video" and sub == "main":
        flag = None  # 主轨忽略 flag 差异，保证各集主视频续接同一条轨
    return (ttype, flag, sub)


# 剪映转场元数据（与 pyJianYingDraft.TransitionType 一致）
_TRANSITIONS = {
    "叠化": {"effect_id": "6724845717472416269", "resource_id": "322577", "is_overlap": True},
    "闪白": {"effect_id": "6724845376098013708", "resource_id": "322575", "is_overlap": False},
    "上移": {"effect_id": "6724846395116753416", "resource_id": "2917279", "is_overlap": True},
}


def _add_transitions(draft, main_track, name="叠化", duration_us=500000):
    """在主视频轨的每个集与集衔接处挂转场（作用于后一个 segment）。

    注意：合并草稿里各集视频段严格首尾相接（零重叠）。叠化 is_overlap=True
    需要两段画面重叠才能交叉混合，若挂在前一片段尾部时渲染器要向前取尚未
    开始的那一帧，实测会卡顿。挂后一片段头部才是实测正常的位置。
    """
    meta = _TRANSITIONS.get(name)
    if not meta:
        print(f"  ⚠️ 未知转场 '{name}'，跳过（可选: {'/'.join(_TRANSITIONS)}）")
        return 0
    mats = draft.setdefault("materials", {}).setdefault("transitions", [])
    segs = main_track.get("segments") or []
    n = 0
    for seg in segs[1:]:
        dur = min(duration_us, int(seg["target_timerange"]["duration"] // 4))
        tid = gen_uuid()
        mats.append({
            "category_id": "", "category_name": "", "duration": dur,
            "effect_id": meta["effect_id"], "id": tid,
            "is_overlap": meta["is_overlap"], "name": name,
            "platform": "all", "resource_id": meta["resource_id"],
            "type": "transition",
        })
        seg.setdefault("extra_material_refs", []).append(tid)
        n += 1
    return n


def _validate(draft):
    """校验零悬空引用、零重复 ID。返回问题列表。"""
    problems = []
    mat_ids = _material_ids(draft)
    mat_list_ids = [
        item["id"]
        for items in draft.get("materials", {}).values() if isinstance(items, list)
        for item in items if isinstance(item, dict) and item.get("id")
    ]
    if len(mat_list_ids) != len(set(mat_list_ids)):
        problems.append(f"materials 存在重复 ID ({len(mat_list_ids)} 条 / {len(set(mat_list_ids))} 唯一)")

    track_ids, seg_ids = [], []
    for t in draft.get("tracks", []):
        track_ids.append(t.get("id"))
        for seg in t.get("segments") or []:
            seg_ids.append(seg.get("id"))
            # 剪映 5.9: segment.material_id 在顶层；旧版本可能在 clip 内，两处都查
            for mid in (seg.get("material_id"), (seg.get("clip") or {}).get("material_id")):
                if mid and mid not in mat_ids:
                    problems.append(f"segment.material_id 悬空: {mid}")
            for key in ("extra_material_refs", "material_refs", "audio_material_refs"):
                for ref in seg.get(key) or []:
                    if ref not in mat_ids:
                        problems.append(f"{key} 悬空: {ref}")
    for name, arr in (("track", track_ids), ("segment", seg_ids)):
        if len(arr) != len(set(arr)):
            problems.append(f"{name} 存在重复 ID ({len(arr)} / {len(set(arr))})")
    return problems


def _rewrite_production_path(mat):
    pp = mat.get("production_path", "")
    m = re.search(r"(audioAlg/[^/]+)$", pp)
    if m:
        mat["production_path"] = f"{_PATH_PLACEHOLDER}/Resources/{m.group(1)}"


def _collect_drafts(args):
    """收集源草稿目录列表（去重、保持顺序）。"""
    if args.state:
        state = json.load(open(args.state, encoding="utf-8"))
        drafts = []
        for v in state.values():
            if isinstance(v, dict) and v.get("status") == "done" and v.get("draft"):
                p = os.path.join(_DRAFT_ROOT, v["draft"])
                if os.path.exists(os.path.join(p, "draft_content.json")):
                    drafts.append(p)
        return drafts

    if args.drafts:
        out = []
        for d in args.drafts:
            name = d if os.path.isdir(d) else os.path.join(_DRAFT_ROOT, d)
            out.append(os.path.abspath(name))
        return out

    if args.dir:
        out = []
        for name in sorted(os.listdir(args.dir)):
            p = os.path.join(args.dir, name)
            if os.path.isdir(p) and os.path.exists(os.path.join(p, "draft_content.json")):
                out.append(p)
        return out

    return []


def _bridge_subtitle_boundaries(base, boundaries_us, max_gap_us=1_000_000):
    """衔接集与集交界处的字幕（仅转场级别的小空隙）。

    规则（两侧独立判断，保证声画同步）：
    - 前一集末尾字幕延长到衔接点：仅当其原结束点距衔接点 ≤ max_gap_us
      （只是让台词在转场期间多停留一瞬，音频已播完，不影响同步）
    - 后一集首条字幕提前到衔接点：仅当其原开始点距衔接点 ≤ max_gap_us
      （避免把字幕拉到静默段里，造成字幕先于声音出现）
    大空隙是真实静默，保持原样。
    """
    n = 0
    for tr in base.get("tracks", []):
        if tr.get("type") != "text":
            continue
        segs = tr.get("segments") or []
        segs.sort(key=lambda s: s["target_timerange"]["start"])
        for b in boundaries_us:
            prev = None
            nxt = None
            for s in segs:
                st = s["target_timerange"]["start"]
                en = st + s["target_timerange"]["duration"]
                if en <= b:
                    prev = s
                elif st >= b and nxt is None:
                    nxt = s
            if prev is None or nxt is None:
                continue
            prev_end = prev["target_timerange"]["start"] + prev["target_timerange"]["duration"]
            gap_before = b - prev_end
            gap_after = nxt["target_timerange"]["start"] - b
            bridged = False
            if 0 < gap_before <= max_gap_us:
                prev["target_timerange"]["duration"] = b - prev["target_timerange"]["start"]
                bridged = True
            if 0 < gap_after <= max_gap_us:
                old_start = nxt["target_timerange"]["start"]
                nxt["target_timerange"]["duration"] += old_start - b
                nxt["target_timerange"]["start"] = b
                bridged = True
            if bridged:
                n += 1
    return n


def _deoverlap_text_tracks(draft):
    """清洗同轨字幕重叠：前一条结束点超过后一条开始点则截断前一条。

    剪映不允许同轨字幕重叠——打开草稿时会把冲突段拆到新轨道，
    两条同位置字幕同时渲染，观感就是"字幕闪烁"。
    时长被截到 ≤0 的段直接移除。
    """
    n = 0
    for tr in draft.get("tracks", []):
        if tr.get("type") != "text":
            continue
        segs = sorted(tr.get("segments") or [], key=lambda s: s["target_timerange"]["start"])
        drop_ids = set()
        for i in range(1, len(segs)):
            prev, cur = segs[i - 1], segs[i]
            if id(prev) in drop_ids:
                continue
            p_tr, c_tr = prev["target_timerange"], cur["target_timerange"]
            if p_tr["start"] + p_tr["duration"] > c_tr["start"]:
                p_tr["duration"] = c_tr["start"] - p_tr["start"]
                n += 1
                if p_tr["duration"] <= 0:
                    drop_ids.add(id(prev))
        if drop_ids:
            tr["segments"] = [s for s in segs if id(s) not in drop_ids]
    return n


def merge(src_dirs, out_name, force=False, transition="none", transition_duration=0.5,
          bridge_gap=1.0):
    """执行合并；out_name 是目标草稿名（相对 _DRAFT_ROOT）。"""
    if not src_dirs:
        print("✗ 没有源草稿")
        sys.exit(1)
    for d in src_dirs:
        if not os.path.exists(os.path.join(d, "draft_content.json")):
            print(f"✗ 源草稿不存在: {d}")
            sys.exit(1)

    target_dir = os.path.join(_DRAFT_ROOT, out_name)
    if os.path.exists(target_dir) and not force:
        print(f"✗ 目标草稿已存在: {target_dir}（--force 可覆盖）")
        sys.exit(1)

    # 在草稿目录之外构建（剪映不可见），校验通过后再整体发布
    os.makedirs(_STAGING_ROOT, exist_ok=True)
    out_dir = os.path.join(_STAGING_ROOT, f"{out_name}__{gen_uuid()[:8]}")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)

    # 用第一集为模板复制完整目录；基础集内容与 ID 完全保留
    shutil.copytree(src_dirs[0], out_dir)
    print(f"🏗️ 暂存区构建（剪映不可见）: {out_dir}")
    for junk in os.listdir(out_dir):
        if junk.startswith("draft_content.json.bak") or junk.startswith("draft_meta_info.json.bak") \
                or junk.startswith("template") and junk.endswith(".tmp"):
            os.remove(os.path.join(out_dir, junk))

    base_path = os.path.join(out_dir, "draft_content.json")
    base = json.load(open(base_path, encoding="utf-8"))
    cum_dur = int(base.get("duration") or 0)
    boundaries_us = []  # 集与集衔接点（微秒）
    used_ids = _material_ids(base) | {
        t.get("id") for t in base.get("tracks", []) if t.get("id")
    }

    # 基础集轨道签名表：后续集按签名追加到同一条轨，而不是新建轨道
    base_lookup = _mat_lookup(base)
    base_tracks = {}
    for t in base.get("tracks", []):
        if t.get("type") in ("filter_config", "video_config"):
            continue
        base_tracks.setdefault(_track_signature(t, base_lookup), t)

    def fresh_id(old=None):
        new = gen_uuid()
        while new in used_ids:
            new = gen_uuid()
        used_ids.add(new)
        return new

    print(f"📦 基础: {os.path.basename(src_dirs[0])}  (时长 {cum_dur/1e6:.2f}s)")

    # 追加后续每集
    for src_dir in src_dirs[1:]:
        print(f"➕ 合并: {os.path.basename(src_dir)} (偏移 {cum_dur/1e6:.2f}s)")
        src = json.load(open(os.path.join(src_dir, "draft_content.json"), encoding="utf-8"))
        offset_us = cum_dur

        _copy_audioalg_files(src_dir, out_dir)

        # 逐集独立映射：同一个旧 ID 在本集内保持一致，跨集互不影响
        idmap = {}
        src_mat_ids = _material_ids(src)
        stripped_refs = 0

        def map_id(old):
            if old not in idmap:
                idmap[old] = fresh_id(old)
            return idmap[old]

        # 素材全量克隆（按 src 实际键，不依赖白名单）
        for key, src_items in src.get("materials", {}).items():
            if not isinstance(src_items, list):
                continue
            dst_list = base["materials"].setdefault(key, [])
            for item in src_items:
                if not isinstance(item, dict):
                    continue
                cloned = json.loads(json.dumps(item))
                if cloned.get("id"):
                    cloned["id"] = map_id(cloned["id"])
                if key == "vocal_separations":
                    _rewrite_production_path(cloned)
                dst_list.append(cloned)

        # 轨道段级追加（filter_config/video_config 属于全局设置，不复制）
        src_lookup = _mat_lookup(src)
        for t in src.get("tracks", []):
            if t.get("type") in ("filter_config", "video_config"):
                continue
            cloned = json.loads(json.dumps(t))
            cloned["id"] = fresh_id()
            for seg in cloned.get("segments") or []:
                seg["id"] = fresh_id()
                # 剪映 5.9 的 segment.material_id 在顶层（不在 clip 内），必须一并重映射
                mid = seg.get("material_id")
                if mid in src_mat_ids:
                    seg["material_id"] = map_id(mid)
                tr = seg.get("target_timerange")
                if isinstance(tr, dict) and "start" in tr:
                    tr["start"] = int(tr["start"]) + offset_us
                clip = seg.get("clip")
                if isinstance(clip, dict) and clip.get("material_id"):
                    clip["material_id"] = map_id(clip["material_id"])
                for key in ("extra_material_refs", "material_refs", "audio_material_refs"):
                    if isinstance(seg.get(key), list):
                        kept = []
                        for x in seg[key]:
                            if x in src_mat_ids:
                                kept.append(map_id(x))
                            else:
                                stripped_refs += 1
                        seg[key] = kept
            # 同签名的轨道直接追加段，不再新建轨道
            sig = _track_signature(t, src_lookup)
            target = base_tracks.get(sig)
            if target is None:
                base_tracks[sig] = cloned
                base["tracks"].append(cloned)
            else:
                target.setdefault("segments", []).extend(cloned.get("segments") or [])

        if stripped_refs:
            print(f"   ⚠️ 丢弃源草稿中 {stripped_refs} 个悬空材质引用（预设轨可选材质）")

        boundaries_us.append(cum_dur)
        cum_dur += int(src.get("duration") or 0)

    base["duration"] = cum_dur

    # 填平集与集衔接处的字幕空隙
    n_bridge = _bridge_subtitle_boundaries(base, boundaries_us,
                                           int(round(float(bridge_gap) * 1_000_000)))
    if n_bridge:
        print(f"📝 已填平 {n_bridge} 个字幕衔接空隙（阈值 {bridge_gap}s）")

    # 同轨字幕重叠清洗（剪映不允许同轨重叠，会把冲突段拆到新轨，
    # 两条同位置字幕同时渲染 → 观感就是"字幕闪烁"）
    n_fix = _deoverlap_text_tracks(base)
    if n_fix:
        print(f"📝 已截断 {n_fix} 条与后一条重叠的字幕")

    # 集与集衔接处加转场，避免硬切
    main_track = base_tracks.get(("video", None, "main"))
    if main_track and transition and str(transition).lower() != "none":
        td_us = int(round(float(transition_duration) * 1_000_000))
        n_tr = _add_transitions(base, main_track, transition, td_us)
        if n_tr:
            print(f"🔗 已添加 {n_tr} 个转场（{transition} {transition_duration}s）")

    # 写盘前校验
    problems = _validate(base)
    if problems:
        print(f"✗ 校验失败，共 {len(problems)} 个问题（前 5 条）：")
        for p in problems[:5]:
            print(f"   - {p}")
        shutil.rmtree(out_dir, ignore_errors=True)
        sys.exit(1)

    # 换新 draft_id/name（内容与 meta 保持一致，避免与源草稿同 ID）
    new_draft_id = gen_uuid()
    base["id"] = new_draft_id
    base["name"] = out_name
    with open(base_path, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=1)

    meta_path = os.path.join(out_dir, "draft_meta_info.json")
    if os.path.exists(meta_path):
        meta = json.load(open(meta_path, encoding="utf-8"))
        meta["draft_id"] = new_draft_id
        meta["draft_name"] = out_name
        meta["draft_fold_path"] = out_dir.replace("\\", "/")
        meta["tm_duration"] = cum_dur
        meta["draft_timeline_materials_size_"] = os.path.getsize(base_path)
        now_us = int(time.time() * 1_000_000)
        if "draft_last_edit_time" in meta:
            meta["draft_last_edit_time"] = now_us
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)

    # 整体发布到剪映草稿目录（原子搬入 + 索引登记）
    try:
        import pipeline as _pl
        target_dir, base_path = _pl.publish_staged_draft(out_dir, out_name, cum_dur)
        print("✅ 已发布到剪映草稿目录并登记索引")
    except Exception as e:
        print(f"✗ 发布失败: {e}")
        sys.exit(1)

    total = cum_dur / 1e6
    v_n = len([t for t in base["tracks"] if t["type"] == "video"])
    t_n = len([t for t in base["tracks"] if t["type"] == "text"])
    s_n = len([t for t in base["tracks"] if t["type"] == "sticker"])
    print(f"\n🏁 合并完成: {len(src_dirs)} 集 → {out_name}")
    print(f"   总时长 {total:.2f}s | video 轨 {v_n} | text 轨 {t_n} | sticker 轨 {s_n}")
    print(f"   ID 校验: 通过（0 悬空引用 / 0 重复）")
    print(f"   路径: {base_path}")


def main():
    ap = argparse.ArgumentParser(description="合并多个独立草稿")
    ap.add_argument("--state", default=None, help="_batch_state.json 路径")
    ap.add_argument("--drafts", nargs="*", default=None, help="草稿名或目录")
    ap.add_argument("--dir", default=None, help="草稿目录(自动合并其中全部子目录)")
    ap.add_argument("--out", required=True, help="目标草稿名")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的目标草稿")
    ap.add_argument("--transition", default="none",
                    help="集间转场名称（none/叠化/闪白/上移），默认 none 不加转场")
    ap.add_argument("--transition-duration", type=float, default=0.3,
                    help="转场时长秒数，默认 0.3")
    ap.add_argument("--bridge-gap", type=float, default=1.0,
                    help="字幕衔接最大调整秒数（只处理转场级小空隙，0 关闭），默认 1")
    args = ap.parse_args()

    src_dirs = _collect_drafts(args)
    if not src_dirs:
        print("✗ 未指定源草稿（需 --state / --drafts / --dir 之一）")
        sys.exit(1)

    merge(src_dirs, args.out, force=args.force,
          transition=args.transition, transition_duration=args.transition_duration,
          bridge_gap=args.bridge_gap)


if __name__ == "__main__":
    main()
