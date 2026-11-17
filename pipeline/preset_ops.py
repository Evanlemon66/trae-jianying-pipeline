# -*- coding: utf-8 -*-
"""预设层操作：剥离旧预设 → 合并新预设组 → 生成独立新草稿。

用途（同一条剧分两个账号发布）：
  A 账号完整剪辑好一条草稿后，B 账号不需要重跑 ASR/翻译/人声分离，
  只要复制 A 的草稿、剥掉 A 的预设轨、换上 B 的预设组，发布成一条新草稿，
  最后两条草稿各自导出即可。

预设层识别：
  1. 优先读草稿目录下的 _pipeline_presets.json（新版 pipeline 构建时写入的轨道/素材清单）
  2. 没有清单（旧草稿）时，按剪映组合预设的素材特征兜底识别：
     主轨之外 type=video、且 segment.material_id 指向 material_name 以「我的预设」开头、
     path 为空的视频素材 —— 这就是剪映「我的预设」组合片段。
"""
import copy
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from pipeline import (  # noqa: E402
    DRAFT_ROOT,
    _STAGING_ROOT,
    _write_pipeline_backup,
    find_draft_by_name,
    find_preset_by_name,
    gen_uuid,
    get_main_video_duration,
    load_draft,
    merge_preset_into_draft,
    publish_staged_draft,
    read_preset_manifest,
    resolve_presets,
    preset_profile_name,
    save_draft,
    write_preset_manifest,
)

import job_manifest as jm  # noqa: E402  # 版本封面解析（与 ingest/契约共用一套组目录规则）
from jy_cover import set_cover_composition, set_draft_cover  # noqa: E402

LEGACY_PRESET_MATERIAL_PREFIX = "我的预设"


# ==================== 素材引用工具 ====================

def _material_index(draft):
    """id -> (类别, 素材对象)。"""
    idx = {}
    for key, items in (draft.get("materials") or {}).items():
        if not isinstance(items, list):
            continue
        for m in items:
            if isinstance(m, dict) and m.get("id"):
                idx[m["id"]] = (key, m)
    return idx


def _is_uuid(s):
    s = s.strip()
    return len(s) == 36 and s.count("-") == 4


def _collect_refs(obj, out):
    """递归收集对象中的 UUID 字符串（宁多勿少，只用于判断素材是否仍被引用）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "draft":      # 复合片段内部是独立素材命名空间，不并入顶层
                continue
            _collect_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_refs(v, out)
    elif isinstance(obj, str) and _is_uuid(obj):
        out.add(obj)


def _remove_materials_by_id(draft, ids):
    if not ids:
        return 0
    removed = 0
    mats = draft.get("materials") or {}
    for key, items in list(mats.items()):
        if not isinstance(items, list):
            continue
        kept = [m for m in items
                if not (isinstance(m, dict) and m.get("id") in ids)]
        removed += len(items) - len(kept)
        mats[key] = kept
    return removed


def _gc_unreferenced_materials(draft):
    """无清单兜底：删除已没有任何轨道引用的素材（含其级联引用）。

    引用根不能只看 tracks：草稿封面（顶层 cover.cover_draft_id → materials.drafts
    里的 composition）不挂在任何轨道上，漏掉它会把封面材质一起回收。
    """
    keep = set()
    for key, value in draft.items():
        if key in ("tracks", "materials"):
            continue
        _collect_refs(value, keep)
    for t in draft.get("tracks") or []:
        _collect_refs(t, keep)
    index = _material_index(draft)
    changed = True
    while changed:
        changed = False
        for mid in list(keep):
            entry = index.get(mid)
            if not entry:
                continue
            before = len(keep)
            _collect_refs(entry[1], keep)
            if len(keep) != before:
                changed = True
    return _remove_materials_by_id(draft, set(index) - keep)


# ==================== 预设层识别 / 剥离 ====================

def find_legacy_preset_track_ids(draft):
    """按「我的预设」组合片段特征识别预设轨（供无清单的旧草稿）。"""
    index = _material_index(draft)
    ids = set()
    for t in draft.get("tracks") or []:
        if t.get("type") != "video":
            continue
        segs = t.get("segments") or []
        if not segs:
            continue
        hit = True
        for s in segs:
            entry = index.get(s.get("material_id"))
            if not entry:
                hit = False
                break
            m = entry[1]
            name = str(m.get("material_name") or "")
            if (m.get("type") != "video" or m.get("path")
                    or not name.startswith(LEGACY_PRESET_MATERIAL_PREFIX)):
                hit = False
                break
        if hit:
            ids.add(t.get("id"))
    return ids


def strip_preset_layers(draft, manifest=None):
    """移除草稿里的预设层。返回 {'tracks': n, 'materials': n, 'legacy': bool}。

    清单优先；同时用「我的预设」特征兜底（防止草稿在剪映里被编辑后轨道 ID 变了、
    清单失效导致旧预设残留 → 新预设叠上去出现双重 logo）。
    """
    legacy_ids = find_legacy_preset_track_ids(draft)
    if manifest is None:
        track_ids = legacy_ids
    else:
        manifest_ids = set(manifest.get("tracks") or [])
        track_ids = manifest_ids | legacy_ids
        if legacy_ids - manifest_ids:
            print("  ⚠️ 预设清单与草稿不一致（轨道 ID 已变），改用特征兜底识别并回收孤立素材")

    before = len(draft.get("tracks") or [])
    draft["tracks"] = [t for t in (draft.get("tracks") or [])
                       if t.get("id") not in track_ids]
    removed_tracks = before - len(draft["tracks"])

    stale = manifest is not None and bool(legacy_ids - set(manifest.get("tracks") or []))
    if manifest is None or stale:
        removed_materials = _gc_unreferenced_materials(draft)
    else:
        mat_ids = set()
        for ids in (manifest.get("materials") or {}).values():
            mat_ids.update(ids or [])
        removed_materials = _remove_materials_by_id(draft, mat_ids)

    return {"tracks": removed_tracks, "materials": removed_materials,
            "legacy": manifest is None or stale}


# ==================== 克隆 + 换预设 ====================

def _resolve_source(source):
    """source 可以是草稿名，也可以是草稿目录。返回 (dir, content_path, name)。"""
    if os.path.isdir(source):
        d = os.path.abspath(source)
        return d, os.path.join(d, "draft_content.json"), os.path.basename(d.rstrip("\\/"))
    d, content, _meta = find_draft_by_name(source)
    if not d:
        raise FileNotFoundError(f"找不到草稿: {source}")
    return d, content, os.path.basename(d.rstrip("\\/"))


def _missing_media_paths(draft, limit=50):
    """草稿引用的本地素材文件中已不存在的路径（去重保序，最多 limit 条）。

    只查绝对路径；跳过 http(s)、空 path（组合预设内部素材）与占位符。
    防的是工作目录被手工清理/搬盘后，克隆出来的草稿导出无声或缺画面。
    """
    out = []
    seen = set()
    for _key, m in _material_index(draft).values():
        p = m.get("path")
        if not isinstance(p, str) or not p.strip():
            continue
        if p.startswith(("http://", "https://")) or not os.path.isabs(p):
            continue
        if p in seen:
            continue
        seen.add(p)
        if not os.path.exists(p):
            out.append(p)
            if len(out) >= limit:
                break
    return out


def _profile_key(preset_profile):
    """预设组名归一：presets_b / b_group / B组 → b（与 resolve_profile_cover 同口径）。"""
    key = preset_profile_name(preset_profile).strip().lower()
    for suf in ("_group", "-group", "组"):
        if key.endswith(suf):
            key = key[:-len(suf)].strip("_-")
            break
    return key


def _is_version_profile(preset_profile):
    """是否为扩展版本组（B/C…）。A/基础版/未指定走源草稿封面。"""
    return _profile_key(preset_profile) not in ("", "a", "base")


def _freshness():
    """产物新鲜度模块（best-effort；缺失不影响主流程）。"""
    try:
        _lib = os.path.join(os.path.dirname(_HERE), "lib")
        if _lib not in sys.path:
            sys.path.insert(0, _lib)
        import freshness
        return freshness
    except Exception:
        return None


def _first_local_video(draft):
    """源草稿主轨里第一个可抽帧的本地视频（按轨道/片段顺序，预设素材 path 为空会跳过）。"""
    index = _material_index(draft)
    for track in draft.get("tracks") or []:
        if track.get("type") != "video":
            continue
        for seg in track.get("segments") or []:
            entry = index.get(seg.get("material_id"))
            if not entry:
                continue
            m = entry[1]
            p = m.get("path")
            if m.get("type") != "video" or not isinstance(p, str) or not p.strip():
                continue
            if os.path.isabs(p) and os.path.isfile(p):
                return p
    return None


def _missing_group_cover_message(job_dir, group, video, reason=""):
    """组封面缺失且抽帧失败时的硬失败消息：说清缺哪一组、哪几张、该补什么。"""
    have = {}
    if job_dir:
        have = jm.detect_account_cover_sets(job_dir).get(group) or {}
    labels = {1: "横版1", 2: "横版2", 3: "横版3", "vertical": "竖版"}
    missing = [label for slot, label in labels.items() if slot not in have]
    locate = (f"04_covers/{group}_group/cover_04_vertical_{group}.*" if job_dir
              else "未定位到 cover_job，无法查找该组封面")
    why = reason or ("源草稿主轨没有可直接抽帧的本地视频" if not video
                     else f"从 {os.path.basename(video)} 抽帧失败")
    return (
        f"预设组 {group} 缺竖版封面，且无法抽帧兜底（{why}）：绝不沿用 A 版封面。\n"
        f"  应补齐：{locate}\n"
        f"  组内缺失：{'、'.join(missing) if missing else '竖版'}\n"
        f"  处理：补齐该组 4 张封面后重试，或确认源草稿主轨视频可读后重跑。")


def _read_stable_source(content_path, probes=4, interval=0.6):
    """读取源草稿，并确认读取窗口内剪映没有在写它。

    剪映只对「当前打开的那份草稿」持有内存副本，并在自动保存时回写；开着剪映剪
    别的草稿不会影响本文件。所以这里不按进程判断，只盯这个文件是否稳定：
      1) JSON 必须能解析（自动保存写到一半时读到的是半份文件）
      2) 解析成功后再确认 mtime/size 没变（变了说明刚刚被写入）
    """
    last_err = ""
    for _ in range(probes):
        try:
            st = os.stat(content_path)
            sig = (st.st_mtime_ns, st.st_size)
            draft, _ = load_draft(content_path)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(interval)
            continue
        time.sleep(0.4)
        try:
            st2 = os.stat(content_path)
        except OSError as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        if (st2.st_mtime_ns, st2.st_size) == sig:
            return draft
        last_err = "文件在读取期间被改写"
        time.sleep(interval)
    name = os.path.basename(os.path.dirname(os.path.abspath(content_path)))
    raise RuntimeError(
        f"源草稿「{name}」正在被剪映写入（打开中/自动保存）或文件已损坏，"
        f"请先在剪映里关闭这份草稿再重试。详情: {last_err}")


def clone_draft_with_presets(source, target_name=None, preset_profile=None,
                             presets=None, overwrite=False, job_dir=None,
                             cover_image=None, cover_fallback="abort"):
    """复制已剪辑草稿 → 剥离旧预设层 → 合并 preset_profile 的预设 → 发布为独立新草稿。

    presets 为 None 时按 preset_profile 从 presets.json 解析。
    版本封面：cover_image 显式优先；否则按 job_dir + preset_profile 从该组竖版封面
    取图（B/C 组绝不沿用源草稿的 A 封面）；B/C 组缺组封面时默认用源草稿主轨视频抽帧
    兜底并在台账记 cover.source=frame，无法抽帧则硬失败。
    cover_fallback="source"：缺组封面时不做抽帧，直接复用源草稿封面
    （台账记 cover.source=source_draft）。返回统计 dict。
    """
    src_dir, _src_content, src_name = _resolve_source(source)
    if presets is None:
        presets, src_desc = resolve_presets(preset_profile)
        print(f"🧩 B 预设来源: {src_desc or '无（不合并预设）'}")
    else:
        src_desc = "显式传入"

    if not target_name:
        # 组名归一化（presets_b / Presets_B → b），保证与 watcher 推导的克隆名一致
        suffix = preset_profile_name(preset_profile) or "copy"
        target_name = f"{src_name}_{suffix}"
    if os.path.basename(target_name) != target_name:
        raise ValueError(f"目标草稿名不能带路径: {target_name}")

    target_dir = os.path.join(DRAFT_ROOT, target_name)
    if os.path.exists(target_dir):
        if not overwrite:
            raise FileExistsError(
                f"目标草稿已存在: {target_name}（加 --overwrite 会覆盖它）")
        print(f"⚠️ 目标草稿已存在，将覆盖: {target_name}")

    manifest = read_preset_manifest(src_dir)
    if manifest is None:
        print("  ⚠️ 源草稿没有预设清单（旧版构建），按「我的预设」素材特征兜底识别旧预设轨")

    # 只关心「这份源草稿」是否正被剪映写入，不关心剪映是否在运行（开着剪映剪别的草稿
    # 不影响复制）。先读源草稿并做完校验，再复制文件：避免白复制一份，也避免把自动
    # 保存写到一半的 JSON 复制进新草稿。
    src_draft = _read_stable_source(_src_content)
    missing = _missing_media_paths(src_draft)
    if missing:
        raise RuntimeError(
            f"源草稿引用的素材缺失 {len(missing)} 个（工作目录被清理/搬盘会这样），"
            f"换预设后导出会缺音频/画面，已中止。例如: " + "；".join(missing[:3]))

    draft = copy.deepcopy(src_draft)
    stat = strip_preset_layers(draft, manifest)
    print(f"  🧹 剥离旧预设层: 轨道 -{stat['tracks']} / 素材 -{stat['materials']}"
          + ("（旧草稿兜底识别）" if stat["legacy"] else ""))
    if stat["tracks"] == 0:
        # 一条预设轨都没剥到：若草稿里仍有「被轨道引用」的我的预设素材，说明预设
        # 不在独立组合轨上（被拖进主轨/改成普通贴图），继续换会新旧叠加 → 直接中止。
        refs = set()
        for t in draft.get("tracks") or []:
            _collect_refs(t, refs)
        stuck = [m.get("material_name") for mid, (_c, m) in _material_index(draft).items()
                 if mid in refs and not m.get("path")
                 and str(m.get("material_name") or "").startswith(LEGACY_PRESET_MATERIAL_PREFIX)]
        if stuck:
            raise RuntimeError(
                f"未剥离到预设轨，但草稿里仍有被引用的「{LEGACY_PRESET_MATERIAL_PREFIX}」素材 "
                f"{len(stuck)} 个：预设可能不在独立组合轨上（被拖进主轨/改成普通贴图），"
                f"继续换会新旧预设叠加，已中止。请在剪映里确认预设是组合预设轨。")
        print("  ℹ️ 源草稿没有可剥离的独立预设轨（本来没加预设，或预设不是组合轨）")

    os.makedirs(_STAGING_ROOT, exist_ok=True)
    stage_dir = os.path.join(_STAGING_ROOT, f"{target_name}__{gen_uuid()[:8]}")
    if os.path.exists(stage_dir):
        shutil.rmtree(stage_dir, ignore_errors=True)
    shutil.copytree(src_dir, stage_dir)

    content_path = os.path.join(stage_dir, "draft_content.json")

    new_manifest = {
        "profile": preset_profile,
        "presets": [],
        "tracks": [],
        "materials": {},
        "source_draft": src_name,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    main_dur = get_main_video_duration(draft)
    for name, alpha in presets:
        pp = find_preset_by_name(name)
        if not pp:
            print(f"  ⚠️ 找不到预设目录，跳过: {name}")
            continue
        one = {}
        nt, nm = merge_preset_into_draft(
            draft, pp, offset_us=0, stretch_to_duration_us=main_dur,
            clip_alpha=alpha, out_manifest=one)
        new_manifest["presets"].append({"name": name, "alpha": alpha})
        new_manifest["tracks"].extend(one.get("tracks") or [])
        for mk, ids in (one.get("materials") or {}).items():
            new_manifest["materials"].setdefault(mk, []).extend(ids)
        print(f"  ✅ 合并预设: {name} (+{nt} 轨 / +{nm} 素材)"
              + (f" clip.alpha={alpha}" if alpha is not None else ""))

    if presets and not new_manifest["presets"]:
        raise RuntimeError("新预设组一个都没找到（预设库名称不匹配）：生成 B 草稿会完全没有预设，已中止")
    if presets and not new_manifest["tracks"]:
        print("  ⚠️ 新预设合并后没有任何轨道，请检查预设内容是否为组合预设")

    # 版本封面：克隆不会自动带封面（draft_cover 是目录级文件，A/B 会共用同一张），
    # 必须按该组自己的竖版封面重写 composition + draft_cover.jpg。
    # B/C 组缺组封面时绝不沿用源草稿（=A 版）的封面：先用源草稿主轨视频抽帧兜底
    # （台账记 cover.source=frame，不冒充生成图）；连可抽帧的视频都没有就硬失败。
    _profile = _profile_key(preset_profile)
    cover_src = cover_image or (jm.resolve_profile_cover(job_dir, preset_profile)
                                if job_dir else None)
    cover_source = (("explicit" if cover_image else
                     ("group" if _is_version_profile(preset_profile) else "base"))
                    if cover_src else None)
    cover_frame_video = None
    cover_written = False
    if not cover_src and _is_version_profile(preset_profile):
        if cover_fallback == "source":
            # 开关打开：组封面缺失时直接沿用源草稿封面（stage 是完整拷贝，无需重写）
            cover_source = "source_draft"
            cover_written = False
            print(f"  ℹ️ 预设组 {_profile} 缺竖版封面：复用源草稿封面"
                  "（cover.source=source_draft）")
        else:
            cover_frame_video = _first_local_video(draft)
            frame_ok = False
            if cover_frame_video:
                try:
                    frame_ok = set_draft_cover(stage_dir,
                                               extract_from_video=cover_frame_video)
                except Exception as e:  # ffmpeg 失败/无 ffmpeg 都落到下面的硬失败
                    print(f"  ⚠️ 抽帧兜底失败: {e}")
            if not frame_ok:
                raise RuntimeError(_missing_group_cover_message(
                    job_dir, _profile, cover_frame_video))
            cover_src = os.path.join(stage_dir, "draft_cover.jpg")  # 抽帧已直接落盘
            cover_source = "frame"
            cover_written = True
            print(f"  ⚠️ 预设组 {_profile} 缺竖版封面：已从 "
                  f"{os.path.basename(cover_frame_video)} 抽帧兜底（cover.source=frame）")
    elif not cover_src:
        print("  ⚠️ 未指定扩展版本组（A/基础版）：沿用源草稿封面")
    if cover_src:
        set_cover_composition(draft, stage_dir, cover_src)

    now_us = int(time.time() * 1_000_000)
    draft["id"] = gen_uuid()
    draft["name"] = target_name
    draft["create_time"] = now_us
    draft["update_time"] = now_us
    save_draft(draft, content_path)
    # 台账：说明本版封面来源（group=该组竖版图 / explicit=显式传入 / frame=抽帧兜底）
    new_manifest["cover"] = {
        "source": cover_source or "source_draft",
        "group": _profile or None,
        "file": ("draft_cover.jpg" if cover_source in ("frame", "source_draft")
                 else cover_src),
        "extracted_from": cover_frame_video,
    }
    write_preset_manifest(stage_dir, new_manifest)
    if cover_src and not cover_written:
        set_draft_cover(stage_dir, cover_image=cover_src)
        print(f"  🖼️ 版本封面: {os.path.basename(cover_src)} → draft_cover.jpg")

    target, final_content = publish_staged_draft(
        stage_dir, target_name, draft.get("duration", 0))
    # 导出前恢复用的备份必须同步成 B 的内容，否则 robust_auto_export 会用 A 的预设轨恢复
    _write_pipeline_backup(final_content, draft)
    # 写时确权：克隆发布完成后在最终草稿上记 E1/E3（job 缺失时内部 no-op）
    _fr = _freshness()
    if _fr and job_dir:
        try:
            _cover_arg = (os.path.join(target, "draft_cover.jpg")
                          if cover_source in ("frame", "source_draft")
                          else cover_src)
            _fr.record_draft_write(job_dir, final_content, "preset_clone")
            _fr.record_cover(job_dir,
                             _fr.version_for_draft(job_dir, target_name),
                             target, _cover_arg)
        except Exception as _fe:
            print(f"  [warn] 记录版本新鲜度失败（不影响主流程）: {_fe}")

    return {
        "source": src_name,
        "target": target_name,
        "target_dir": target,
        "content_path": final_content,
        "presets": [p["name"] for p in new_manifest["presets"]],
        "profile": preset_profile,
        # 抽帧兜底时 staging 路径发布后会失效，返回成品草稿里的 draft_cover.jpg
        "cover": (os.path.join(target, "draft_cover.jpg")
                  if cover_source == "frame" else cover_src),
        "cover_source": cover_source or "source_draft",
        "cover_extracted_from": cover_frame_video,
        "stripped": stat,
        "duration_us": draft.get("duration", 0),
    }
