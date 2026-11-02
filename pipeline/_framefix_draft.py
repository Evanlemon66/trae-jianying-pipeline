"""帧对齐修复：消除剪映渲染器逐集 -20ms 的视频量化漂移。

原理：每集视频槽位时长若非 1/fps 整数倍，渲染器按整帧截断并把下一集视频
提前 frac/fps 开始，117 集累计可达 -2.5s（音画渐 desync）。
本脚本把全部 video/audio/text 段重排到帧对齐时间线上：
  - 每集新时长 = norm 视频流实际帧数 / fps（ffprobe 实测，回退 floor(时长×fps)/fps）
  - 新目标位 = 各集新时长累加（ep1 起点不变）
  - 音频段：目标位=对应集新视频起点，目标/源时长=新时长
  - 字幕段：按所属集平移 delta
  - 全时间线覆盖段（预设/白场）：时长改为新总长

单元测试：帧对齐完成后自动校验字幕 delta 与视频 delta 一致（防回归 bug）。

用法：
  python _framefix_draft.py <草稿名> [drafts根目录]     # 命令行
  from _framefix_draft import fix_draft                 # 库调用
"""
import json, os, re, shutil, subprocess, sys

sys.stdout.reconfigure(encoding="utf-8")


def _ffprobe_path():
    p = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffprobe.EXE")
    if os.path.exists(p):
        return p
    try:
        from export_config import get_ffprobe
        return get_ffprobe()
    except Exception:
        return "ffprobe"


def _probe_frames(path, fp):
    """ffprobe 读视频流 nb_frames + fps；失败回退 duration*30。"""
    try:
        r = subprocess.run([fp, "-v", "error", "-select_streams", "v:0", "-show_entries",
                            "stream=nb_frames,avg_frame_rate,duration", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        st = json.loads(r.stdout)["streams"][0]
        num, den = (st.get("avg_frame_rate") or "30/1").split("/")
        fps = float(num) / float(den) if float(den) else 30.0
        if st.get("nb_frames"):
            return int(st["nb_frames"]), fps
        return int(float(st.get("duration", 0)) * fps), fps
    except Exception:
        return None, 30.0


def _load_episode_map(draft_dir):
    """读 episode_map.json（batch_runner 写入，草稿内集身份的唯一收口）。

    结构 v1: {version, draft, series_dir, cut_s, created_at,
              episodes:[{seq 串流序号, ep 真实集号, demucs, material,
                         slot_start_us, slot_dur_us}]}
    缺失/损坏返回 None → 调用方走几何兜底。
    """
    p = os.path.join(draft_dir, "episode_map.json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        if not isinstance(m, dict) or m.get("version") != 1 or not m.get("episodes"):
            return None
        return m
    except (OSError, ValueError):
        return None


def _assert_layout(d, new_start, seq_to_ep):
    """断言 D：帧对齐后的音频布局自检，写盘前调用。

    A1 每段 demucs 音频落在其映射集的目标槽（start == new_start[ep]）
    A2 同一音频轨相邻段 target 不重叠
    A3 集完整：按材质身份归属的每集段数 == 实际落位段数（不丢不重）
    返回 (通过, 原因)。无 map 时 A1/A3 空真（身份不可知），A2 仍生效。
    """
    mats_a = {m.get("id"): m for m in d.get("materials", {}).get("audios", [])}
    tol_us = 500  # 0.5ms 舍入容差
    ident, placed = {}, {}
    for t in d.get("tracks", []):
        if t.get("type") != "audio":
            continue
        for s in t.get("segments") or []:
            m = mats_a.get(s.get("material_id"), {})
            mt = re.search(r"demucs_input_ep(\d+)/", (m.get("path") or "").replace("\\", "/"))
            if not mt:
                continue
            ep = seq_to_ep.get(int(mt.group(1)))
            if ep is None or ep not in new_start:
                continue
            ident[ep] = ident.get(ep, 0) + 1
            start = (s.get("target_timerange") or {}).get("start", 0)
            if abs(start - round(new_start[ep] * 1e6)) > tol_us:
                return False, (f"A1 demucs_ep{int(mt.group(1)):02d}(第{ep}集) 未落在本集槽位: "
                               f"start={start/1e6:.3f}s 期望 {new_start[ep]:.3f}s")
            placed[ep] = placed.get(ep, 0) + 1
    if ident != placed:
        diff = {e: (ident.get(e, 0), placed.get(e, 0))
                for e in set(ident) | set(placed)
                if ident.get(e, 0) != placed.get(e, 0)}
        return False, f"A3 每集段数不符 ep:(身份,落位)={diff}"
    for ti, t in enumerate(d.get("tracks", [])):
        if t.get("type") != "audio":
            continue
        rng = sorted(((s.get("target_timerange") or {}) for s in t.get("segments") or []),
                     key=lambda x: x.get("start", 0))
        for a, b in zip(rng, rng[1:]):
            if b.get("start", 0) + tol_us < a.get("start", 0) + a.get("duration", 0):
                return False, f"A2 音频轨#{ti} 相邻段重叠 @{a.get('start', 0)/1e6:.3f}s"
    return True, ""


def fix_draft(draft_name, draft_root=None, run_tests=True):
    """对指定草稿执行帧对齐修复。

    Args:
        draft_name: 草稿名（即 drafts 根目录下的子目录名）
        draft_root: drafts 根目录，默认 %LOCALAPPDATA%\\JianyingPro\\User Data\\Projects\\com.lveditor.draft
        run_tests: 是否跑字幕平移单元测试断言（防回归）

    Returns:
        dict: {
            "draft_dir": str, "ep_count": int,
            "old_total": float, "new_total": float,
            "new_start": {ep: float}, "delta": {ep: float},
            "n_text": int, "n_text_miss": int,
            "n_audio": int, "n_cover_segs": int,
            "test_passed": bool | None, "test_details": str,
        }
    """
    fp = _ffprobe_path()
    if draft_root is None:
        draft_root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                  "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
    draft_dir = os.path.join(draft_root, draft_name)
    cur_p = os.path.join(draft_dir, "draft_content.json")
    pub_p = os.path.join(draft_dir, "_pipeline_backup.json")
    if not os.path.exists(cur_p):
        raise FileNotFoundError(f"草稿不存在: {cur_p}")

    with open(cur_p, encoding="utf-8") as f:
        d = json.load(f)
    mats_v = {m.get("id"): m for m in d.get("materials", {}).get("videos", [])}
    mats_a = {m.get("id"): m for m in d.get("materials", {}).get("audios", [])}

    # ---- episode_map.json：集身份唯一收口（batch_runner 写入）----
    emap = _load_episode_map(draft_dir)
    mat_name_to_ep, seq_to_ep = {}, {}
    if emap:
        for it in emap.get("episodes") or []:
            try:
                seq_to_ep[int(it["seq"])] = int(it["ep"])
                if it.get("material"):
                    mat_name_to_ep[str(it["material"])] = int(it["ep"])
            except (KeyError, TypeError, ValueError):
                continue
        print(f"episode_map: 已加载（{len(seq_to_ep)} 集）")
    else:
        print("episode_map: 缺失 → 音频按几何兜底归属")
    degraded = 0

    # ---- 收集各集视频段 ----
    ep_seg, ep_path = {}, {}
    old_total = 0.0
    for t in d.get("tracks", []):
        if t.get("type") != "video":
            continue
        for s in (t.get("segments") or []):
            m = mats_v.get(s.get("material_id"), {})
            mname = m.get("material_name") or ""
            ep = mat_name_to_ep.get(mname)  # map 优先
            if ep is None:
                mt = re.match(r"第(\d+)集", mname)  # regex 兜底
                ep = int(mt.group(1)) if mt else None
            ts = s.get("target_timerange") or {}
            if ep is not None:
                ep_seg[ep] = s
                ep_path[ep] = m.get("path", "")
                old_total = max(old_total, ts.get("start", 0) / 1e6 + ts.get("duration", 0) / 1e6)

    print(f"草稿: {draft_name}")
    print(f"集数: {len(ep_seg)}  旧总长: {old_total:.3f}s")

    # ---- ffprobe 实测 → 新时长 ----
    new_dur, new_start = {}, {}
    cur = 0.0
    for ep in sorted(ep_seg):
        old_d = (ep_seg[ep].get("target_timerange") or {}).get("duration", 0) / 1e6
        nf, fps = _probe_frames(ep_path[ep], fp) if os.path.exists(ep_path[ep]) else (None, 30.0)
        if nf:
            nd = nf / fps
        else:
            nd = float(int(old_d * fps)) / fps
        if nd > old_d:
            nd = float(int(old_d * fps)) / fps
        new_dur[ep] = nd
        new_start[ep] = cur
        cur += nd
    new_total = cur
    print(f"新总长: {new_total:.3f}s  (帧对齐)")

    # ---- 快照旧时长（必须在视频段重排之前）----
    old_durs = {ep: (ep_seg[ep].get("target_timerange") or {}).get("duration", 0) / 1e6
                for ep in ep_seg}

    # ---- 旧时间轴各集位置快照（必须在任何重排之前）：字幕平移与音频几何兜底共用 ----
    old_start_ep = {}
    c = 0.0
    for ep in sorted(ep_seg):
        old_start_ep[ep] = c
        c += old_durs[ep]

    ep_list = [(ep, old_start_ep[ep], old_durs[ep]) for ep in sorted(ep_seg)]

    def old_ep_of(t):
        lo, hi = 0, len(ep_list) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s0, d0 = ep_list[mid][1], ep_list[mid][2]
            if t < s0:
                hi = mid - 1
            elif t >= s0 + d0:
                lo = mid + 1
            else:
                return ep_list[mid][0]
        return None

    # ---- 重排视频段 ----
    for ep, s in ep_seg.items():
        ts = s["target_timerange"]
        ss = s.get("source_timerange") or {}
        ts["start"] = round(new_start[ep] * 1e6)
        ts["duration"] = round(new_dur[ep] * 1e6)
        if ss.get("duration"):
            ss["duration"] = round(new_dur[ep] * 1e6)

    # ---- 重排音频段 ----
    # 集身份：优先 episode_map（demucs 串流序号 → 真实集号）。旧实现把 demucs 序号
    # 直接当集号，剧集从非第1集开始时整体错位（八仙 02-83 集片头串音根因）。
    # map 缺失/缺项时按段当前所在视频槽几何归属（兜底 A）。
    n_audio = 0
    for t in d.get("tracks", []):
        if t.get("type") != "audio":
            continue
        for s in (t.get("segments") or []):
            m = mats_a.get(s.get("material_id"), {})
            mt = re.search(r"demucs_input_ep(\d+)/", (m.get("path") or "").replace("\\", "/"))
            if not mt:
                continue
            ep = seq_to_ep.get(int(mt.group(1)))
            if ep is None:
                ep = old_ep_of((s.get("target_timerange") or {}).get("start", 0) / 1e6)
                if ep is None:
                    continue
                degraded += 1
            if ep not in new_start:
                continue
            ts, ss = s["target_timerange"], s.get("source_timerange") or {}
            ts["start"] = round(new_start[ep] * 1e6)
            ts["duration"] = round(new_dur[ep] * 1e6)
            if ss.get("duration"):
                ss["duration"] = round(new_dur[ep] * 1e6)
            n_audio += 1
    if degraded:
        print(f"  ⚠️ [degraded:framefix-no-map] {degraded} 个音频段走几何兜底归属"
              f"（episode_map.json 缺失/缺项）")
    print(f"音频段重排: {n_audio}")

    # ---- 字幕段平移 ----
    delta = {ep: new_start[ep] - old_start_ep[ep] for ep in sorted(ep_seg)}
    n_text, n_text_miss = 0, 0
    for t in d.get("tracks", []):
        if t.get("type") != "text":
            continue
        for s in (t.get("segments") or []):
            ts = s.get("target_timerange") or {}
            st = ts.get("start", 0) / 1e6
            ep = old_ep_of(st)
            if ep is None:
                n_text_miss += 1
                continue
            ts["start"] = round((st + delta[ep]) * 1e6)
            n_text += 1
    print(f"字幕段平移: {n_text}  未归属: {n_text_miss}")

    # ---- 覆盖段 ----
    n_cover = 0
    for t in d.get("tracks", []):
        if t.get("type") != "video":
            continue
        for s in (t.get("segments") or []):
            m = mats_v.get(s.get("material_id"), {})
            if re.match(r"第\d+集", m.get("material_name") or ""):
                continue
            ts, ss = s.get("target_timerange") or {}, s.get("source_timerange") or {}
            if abs(ts.get("duration", 0) / 1e6 - old_total) < 1.0:
                ts["duration"] = round(new_total * 1e6)
                if ss.get("duration"):
                    ss["duration"] = round(new_total * 1e6)
                print(f"  覆盖段 {m.get('material_name','?')}: 时长 → {new_total:.3f}s")
                n_cover += 1

    # ---- 叠加轨（贴纸/logo/三连）对齐主轨：消除片尾超出导致的导出黑场 ----
    n_overlay, n_text_clamp = _clamp_tail(d, round(new_total * 1e6), old_total)

    d["duration"] = round(new_total * 1e6)

    # ---- 单元测试（防回归：字幕 delta 必须与视频 delta 一致）----
    test_passed, test_details = None, ""
    if run_tests and len(ep_seg) >= 2:
        test_passed, test_details = _run_subtitle_test(d, draft_dir, delta, new_start, old_start_ep, old_durs)

    # ---- 断言 D：布局自检失败则拒写，草稿保持 framefix 前状态 ----
    ok_layout, reason = _assert_layout(d, new_start, seq_to_ep)
    if not ok_layout:
        print(f"  ❌ 帧对齐断言失败，拒绝写盘: {reason}")
        return {
            "draft_dir": draft_dir, "ep_count": len(ep_seg),
            "old_total": old_total, "new_total": new_total,
            "new_start": new_start, "delta": delta,
            "n_text": n_text, "n_text_miss": n_text_miss,
            "n_audio": n_audio, "n_cover_segs": n_cover,
            "n_overlay": n_overlay, "n_text_clamp": n_text_clamp,
            "test_passed": test_passed, "test_details": test_details,
            "map_used": bool(emap), "degraded": degraded, "assert_failed": reason,
        }

    # ---- 写回 ----
    for f in ("draft_content.json", "_pipeline_backup.json"):
        p = os.path.join(draft_dir, f)
        if not os.path.exists(p):
            continue
        bak = p + ".pre_framefix.json"
        if not os.path.exists(bak):
            shutil.copy2(p, bak)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)

    print("\n✅ 已写回 draft_content.json / _pipeline_backup.json")
    print(f"   总长 {old_total:.3f}s → {new_total:.3f}s")
    if test_passed is True:
        print(f"   🧪 字幕平移测试: 通过 ✅")
    elif test_passed is False:
        print(f"   🧪 字幕平移测试: 失败 ❌ — {test_details}")

    return {
        "draft_dir": draft_dir, "ep_count": len(ep_seg),
        "old_total": old_total, "new_total": new_total,
        "new_start": new_start, "delta": delta,
        "n_text": n_text, "n_text_miss": n_text_miss,
        "n_audio": n_audio, "n_cover_segs": n_cover,
        "n_overlay": n_overlay, "n_text_clamp": n_text_clamp,
        "test_passed": test_passed, "test_details": test_details,
        "map_used": bool(emap), "degraded": degraded, "assert_failed": None,
    }


def _clamp_tail(d, new_end_us, old_total):
    """把超出主轨末尾的全时间线叠加轨/字幕夹到主轨末尾。

    贴纸轨（logo/三连/上新）在预设合并时按帧对齐前的总长拉伸，帧对齐后主轨
    变短，贴纸轨却仍是旧总长 → 导出时尾部多出 1~1.7s 黑场。此处统一夹紧。

    Returns:
        (n_overlay, n_text_clamp)
    """
    n_overlay = n_text_clamp = 0
    old_end_us = round(old_total * 1e6)
    for t in d.get("tracks", []):
        ty = t.get("type")
        for s in (t.get("segments") or []):
            ts = s.get("target_timerange") or {}
            if not ts:
                continue
            start, dur = ts.get("start", 0), ts.get("duration", 0)
            end = start + dur
            if end <= new_end_us:
                continue
            if ty not in ("video", "audio", "text"):
                # 叠加轨：起点 0、时长≈旧总长的整条全轴段
                if start == 0 and dur >= old_end_us - 1_500_000:
                    ts["duration"] = new_end_us
                    ss = s.get("source_timerange") or {}
                    if ss.get("duration"):
                        ss["duration"] = new_end_us
                    n_overlay += 1
            elif ty == "text" and start < new_end_us:
                # 字幕：尾部溢出主轨的段，夹到主轨末尾
                ts["duration"] = new_end_us - start
                n_text_clamp += 1
    if n_overlay or n_text_clamp:
        print(f"叠加轨夹尾: {n_overlay}  字幕夹尾: {n_text_clamp}")
    return n_overlay, n_text_clamp


def fix_overlay_tail(draft_name, draft_root=None):
    """对已生成的草稿做叠加轨夹尾修复（不需重新帧对齐）。

    用于修复历史上帧对齐过、但贴纸轨仍超主轨末尾的草稿。

    Returns:
        dict: {draft_dir, video_end, old_max_end, n_overlay, n_text_clamp, changed}
    """
    if draft_root is None:
        draft_root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                  "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
    draft_dir = os.path.join(draft_root, draft_name)
    cur_p = os.path.join(draft_dir, "draft_content.json")
    if not os.path.exists(cur_p):
        raise FileNotFoundError(f"草稿不存在: {cur_p}")

    with open(cur_p, encoding="utf-8") as f:
        d = json.load(f)

    # 主轨末尾 = 集视频轨（段数最多的 video 轨）的终点
    vid_end_us = 0
    for t in d.get("tracks", []):
        segs = t.get("segments") or []
        if t.get("type") == "video" and len(segs) > 5:
            vid_end_us = max(s["target_timerange"]["start"] + s["target_timerange"]["duration"] for s in segs)
    if not vid_end_us:
        raise ValueError("找不到主视频轨")

    old_max_end = 0
    for t in d.get("tracks", []):
        for s in (t.get("segments") or []):
            ts = s["target_timerange"]
            old_max_end = max(old_max_end, ts["start"] + ts["duration"])

    n_overlay, n_text_clamp = _clamp_tail(d, vid_end_us, old_max_end / 1e6)
    d["duration"] = vid_end_us

    changed = bool(n_overlay or n_text_clamp) or (old_max_end != vid_end_us)
    if changed:
        for fn in ("draft_content.json", "_pipeline_backup.json"):
            p = os.path.join(draft_dir, fn)
            if not os.path.exists(p):
                continue
            bak = p + ".pre_tailfix.json"
            if not os.path.exists(bak):
                shutil.copy2(p, bak)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)

    print(f"{draft_name}: 主轨 {vid_end_us/1e6:.3f}s | 修复前末位 {old_max_end/1e6:.3f}s "
          f"| 叠加 {n_overlay} / 字幕 {n_text_clamp} | {'已写回' if changed else '无需修复'}")
    return {"draft_dir": draft_dir, "video_end": vid_end_us / 1e6,
            "old_max_end": old_max_end / 1e6, "n_overlay": n_overlay,
            "n_text_clamp": n_text_clamp, "changed": changed}


def _run_subtitle_test(d, draft_dir, delta, new_start, old_start_ep, old_durs):
    """字幕平移单元测试：验证字幕 delta 与视频 delta 方向/量级一致。

    检查项：
      T1: delta[ep] 必须 = new_start[ep] - old_start_ep[ep]（防 delta=0 bug）
      T2: 字幕段数 > 0（有字幕轨且至少有一段）
      T3: 抽样字幕段 start 已不在旧区间内（说明真平移了）
    """
    # T1: delta 计算正确
    for ep in delta:
        expected = new_start[ep] - old_start_ep[ep]
        if abs(delta[ep] - expected) > 1e-9:
            return False, f"T1 delta[{ep}]={delta[ep]:.9f} != {expected:.9f}"
    # 排除 trivial 情况（delta 全 0 说明 old_start_ep 算错了）
    non_zero = sum(1 for v in delta.values() if abs(v) > 1e-6)
    if non_zero == 0 and len(delta) >= 3:
        return False, f"T1b delta 全 0（{len(delta)} 集）——疑似 old_durs 快照没生效"

    # T2+T3: 抽样字幕段
    text_segs = []
    for t in d.get("tracks", []):
        if t.get("type") != "text":
            continue
        for s in (t.get("segments") or []):
            text_segs.append((s.get("target_timerange") or {}).get("start", 0) / 1e6)
    if not text_segs:
        return True, "无字幕段（可能纯音频），跳过"

    # 用 pre_framefix 备份读旧字幕位置做对照
    bak_p = os.path.join(draft_dir, "draft_content.json.pre_framefix.json")
    if os.path.exists(bak_p):
        with open(bak_p, encoding="utf-8") as f:
            old_d = json.load(f)
        old_text_segs = []
        for t in old_d.get("tracks", []):
            if t.get("type") != "text":
                continue
            for s in (t.get("segments") or []):
                old_text_segs.append((s.get("target_timerange") or {}).get("start", 0) / 1e6)
        # 抽样前 20 段 + 中段
        indices = sorted(set(list(range(0, min(20, len(text_segs)))) +
                              [len(text_segs) // 2, len(text_segs) - 1]))
        for i in indices:
            if i >= len(old_text_segs):
                continue
            diff = text_segs[i] - old_text_segs[i]
            # 允许 ±0.005s 浮点数误差
            if abs(diff) > 0.005 and abs(diff - _expected_delta(old_text_segs[i], old_start_ep, old_durs, delta)) > 0.05:
                return False, f"T3 段#{i} start差{diff:+.3f}s 与期望不符"

    return True, f"delta 非零={non_zero}/{len(delta)}; 字幕段={len(text_segs)}"


def _expected_delta(old_start, old_start_ep, old_durs, delta):
    """给定旧时间轴字幕起点，找到所属集 → 返回 delta。"""
    eps = sorted(old_start_ep)
    for ep in eps:
        s0, d0 = old_start_ep[ep], old_durs[ep]
        if s0 <= old_start < s0 + d0:
            return delta.get(ep, 0.0)
    return 0.0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python _framefix_draft.py <草稿名> [drafts根目录]\n"
              "      python _framefix_draft.py --tailfix <草稿名|all> [drafts根目录]  # 只修叠加轨片尾")
        sys.exit(1)
    if sys.argv[1] == "--tailfix":
        root = sys.argv[3] if len(sys.argv) > 3 else None
        if root is None:
            root = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                "JianyingPro", "User Data", "Projects", "com.lveditor.draft")
        targets = ([d for d in os.listdir(root) if d.endswith("集串流")]
                   if sys.argv[2] == "all" else [sys.argv[2]])
        for name in targets:
            try:
                fix_overlay_tail(name, root)
            except Exception as e:
                print(f"✗ {name}: {e}")
        sys.exit(0)
    fix_draft(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None, run_tests=True)
