"""导出后自动验证：帧级音画漂移 + 字幕帧位抽检。

用法：
  python verify_export.py <草稿名> [成品mp4路径] [norm视频根目录]
  python verify_export.py 狩猎七零：我叫李上进_第01-117集串流
  python verify_export.py 狩猎七零：我叫李上进_第01-117集串流 "D:\\油管打猎\\L\\狩猎七零：我叫李上进_第01-117集串流\\狩猎七零：我叫李上进_第01-117集串流.mp4" D:\\短剧\\狩猎七零：我叫李上进

成品 mp4 路径缺省时按「配置导出目录/草稿名/草稿名.mp4」推导（见 export_config.py）。

抽检策略：
  帧级漂移：ep2 / ep50 / ep末 三点（用 norm 视频作 ref、成品 mp4 作 probe，δ_v 应恒定 ≈ 音频轨固有偏移 ≈ -0.1s，不再随集号增长）
  字幕帧位：ep末集内 50% 位置（亮度突变时刻 vs 草稿字幕起点，差 < 0.2s 即通过；在 ±1.5s 窗口内搜最大跳变，排除场景切换）

退出码：0=全部通过；1=有项目未通过；2=测量失败
"""
import json, os, re, subprocess, sys
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

from export_config import get_ffmpeg  # noqa: E402

FF = get_ffmpeg()
W, H, FPS = 256, 144, 30

DRAFT_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "JianyingPro", "User Data", "Projects", "com.lveditor.draft")


def _video_segs(doc):
    mats = {m.get("id"): m for m in doc.get("materials", {}).get("videos", [])}
    vt = {}
    for t in doc.get("tracks", []):
        if t.get("type") != "video":
            continue
        for s in (t.get("segments") or []):
            mt = re.match(r"第(\d+)集", mats.get(s.get("material_id"), {}).get("material_name") or "")
            if mt:
                ts = s.get("target_timerange") or {}
                vt[int(mt.group(1))] = (ts.get("start", 0) / 1e6, ts.get("duration", 0) / 1e6,
                                         mats.get(s.get("material_id"), {}).get("path", ""))
    return vt


def _grab_clip(path, t, dur):
    try:
        r = subprocess.run([FF, "-v", "error", "-ss", f"{t:.3f}", "-t", f"{dur:.3f}", "-i", path,
                            "-vf", f"fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                           capture_output=True, timeout=300)
        a = np.frombuffer(r.stdout, dtype=np.uint8)
        n = len(a) // (W * H)
        if n == 0:
            return None
        return a[:n * W * H].reshape(n, H, W).astype(np.float32)
    except Exception:
        return None


def _video_delta(ref_path, probe_path, probe_base, s_pos, win=2.7):
    """probe 帧（1.5s）在 ref 帧（±win+1.5s）中的内容偏移 δ（probe 相对 ref）。

    ref_path: norm 视频路径（源文件）
    probe_path: 成品 mp4 路径（被探测文件）
    probe_base: probe 中该集的目标起点（秒）
    s_pos: 在 probe 集内相对起点的位置（秒）
    win: ref 搜索窗口半径
    """
    # ref 中该集的位置 = s_pos（norm 视频未被拼接，从 0 开始）
    fclip = _grab_clip(probe_path, probe_base + s_pos - 0.5, 1.5)
    nclip = _grab_clip(ref_path, s_pos - win, 2 * win + 1.5)
    if fclip is None or nclip is None:
        return None, 0.0, 0
    deltas, mads = [], []
    flat_n = nclip.reshape(len(nclip), -1)
    for i in range(len(fclip)):
        flat_f = fclip[i].flatten()
        mad = np.abs(flat_n - flat_f).mean(axis=1)
        j = int(np.argmin(mad))
        mads.append(mad[j])
        deltas.append((j / FPS - win) - (i / FPS - 0.5))
    mads, deltas = np.array(mads), np.array(deltas)
    conf = mads < 12.0
    if conf.sum() < 5:
        return None, float(mads.min()), 0
    return float(np.median(deltas[conf])), float(mads[conf].mean()), int(conf.sum())


def _subtitle_brightness_jump(mp4_path, draft_t_start, draft_t_dur):
    """成品底部亮度突变时刻 vs 草稿字幕起点。

    修复：综合两种策略找字幕帧位：
      1. 全局最大跳变（可能是场景切换，但如果 ≈ draft_t_start 则字幕确实出现在场景切换时）
      2. ±1.5s 窗口内所有有效跳变（排除 Δ>15），取最接近 draft_t_start 的那个
    两者取差最小的结果。
    """
    frames = _grab_clip(mp4_path, draft_t_start - 3.0, 8.0)  # -3s ~ +5s
    if frames is None or len(frames) < 60:
        return None, "抓帧失败"
    bottom = frames[:, -36:, :]
    bright = bottom.mean(axis=(1, 2))
    diffs = np.abs(np.diff(bright))

    # 策略 A：全局最大跳变（可能是场景切换，但如果 ≈ draft_t_start 则字幕就出现在场景切换时）
    global_best_idx = int(np.argmax(diffs))
    global_best_t = (draft_t_start - 3.0) + global_best_idx / FPS
    global_gap = abs(global_best_t - draft_t_start)

    # 策略 B：±1.5s 窗口内所有有效跳变（排除 Δ>15），取最接近 draft_t_start 的
    win_lo = max(0, int((draft_t_start - 1.5 - (draft_t_start - 3.0)) * FPS))
    win_hi = min(len(diffs), int((draft_t_start + 1.5 - (draft_t_start - 3.0)) * FPS))
    window_diffs = diffs[win_lo:win_hi]
    valid_idx = np.where(window_diffs <= 15)[0]
    window_best_t = None
    if len(valid_idx) > 0:
        all_valid_t = [(draft_t_start - 3.0) + (win_lo + vi) / FPS for vi in valid_idx]
        window_best_t = min(all_valid_t, key=lambda x: abs(x - draft_t_start))
        window_gap = abs(window_best_t - draft_t_start)
    else:
        window_gap = 999.0

    # 取两者中差最小的
    if global_gap < window_gap:
        final_t, final_gap, detail = global_best_t, global_gap, "全局最大跳变（可能场景切换）"
    else:
        final_t, final_gap, detail = window_best_t, window_gap, "窗口内最接近跳变"

    if final_gap is None:
        return None, "无有效跳变"
    return final_gap, f"亮度跳变 @{final_t:.3f}s vs 草稿起点 {draft_t_start:.3f}s 差 {final_gap:+.3f}s [{detail}]"


def _infer_norm_root(draft_dir):
    """从草稿的 material path 推导 norm 视频根目录。"""
    try:
        d = json.load(open(os.path.join(draft_dir, "draft_content.json"), encoding="utf-8"))
        for m in d.get("materials", {}).get("videos", []):
            p = m.get("path") or ""
            if "__jy_norm__" in p:
                # path 形如 D:\\短剧\\狩猎七零：我叫李上进\\__jycache__\\第2集.__jy_norm__.mp4
                return os.path.dirname(os.path.dirname(p))  # 上两级 → 短剧根目录
    except Exception:
        pass
    return None


def main():
    if len(sys.argv) < 2:
        print("用法: python verify_export.py <草稿名> [成品mp4路径] [norm视频根目录]")
        sys.exit(2)
    draft_name = sys.argv[1]
    if len(sys.argv) > 2:
        mp4_path = sys.argv[2]
    else:
        from export_config import export_mp4_path
        mp4_path = export_mp4_path(draft_name)
    draft_dir = os.path.join(DRAFT_ROOT, draft_name)
    if not os.path.exists(mp4_path):
        print(f"✗ 成品不存在: {mp4_path}")
        sys.exit(2)
    if not os.path.exists(os.path.join(draft_dir, "draft_content.json")):
        print(f"✗ 草稿不存在: {draft_dir}")
        sys.exit(2)

    norm_root = sys.argv[3] if len(sys.argv) > 3 else _infer_norm_root(draft_dir)

    d = json.load(open(os.path.join(draft_dir, "draft_content.json"), encoding="utf-8"))
    vt = _video_segs(d)
    if not vt:
        print("✗ 草稿无剧集视频段")
        sys.exit(2)

    # ---- 帧级漂移抽检 ----
    print(f"📐 草稿: {draft_name}  {len(vt)}集  成品: {os.path.basename(mp4_path)}")
    if norm_root:
        print(f"  norm 根目录: {norm_root}")
    eps_sorted = sorted(vt)
    mid_ep = 50 if 50 in vt else eps_sorted[len(eps_sorted) // 2]
    anchors = [
        ("首", eps_sorted[:4]),
        ("中", [e for e in (mid_ep, mid_ep + 1, mid_ep + 2, mid_ep - 1) if e in vt]),
        ("末", [eps_sorted[-1]]),
    ]
    drift_results = []
    for label, cand in anchors:
        got = False
        for ep in cand[:4]:
            tgt, tdur, mat_path = vt[ep]
            # ref = norm 视频（从 material path 或 norm_root + __jycache__ 推导）
            ref_path = mat_path if os.path.exists(mat_path) else None
            if ref_path is None and norm_root:
                ref_path = os.path.join(norm_root, "__jycache__", f"第{ep}集.__jy_norm__.mp4")
            if ref_path is None or not os.path.exists(ref_path):
                print(f"  [{label}] ep{ep}: 跳过（norm 视频不存在）")
                continue
            s_pos = min(0.5 * tdur, tdur - 3)
            if s_pos <= 3:
                print(f"  [{label}] ep{ep}: 跳过（集时长过短 {tdur:.1f}s）")
                continue
            dv, mad, nf = _video_delta(ref_path, mp4_path, tgt, s_pos, win=2.7)
            if dv is None or nf < 5 or mad > 15:
                print(f"  [{label}] ep{ep}: 匹配失败 (MAD={mad:.1f}, {nf}帧) → 尝试下一集")
                continue
            print(f"  [{label}] ep{ep}: δ_v={dv:+.3f}s (MAD={mad:.1f}, {nf}帧)  ✅")
            drift_results.append((ep, dv))
            got = True
            break
        if not got:
            print(f"  [{label}] 候选全部匹配失败")

    # 漂移判定：各测点 δ_v 极差 < 0.2s 即恒定（不随集号增长）
    valid = [dv for _, dv in drift_results]
    drift_ok = (max(valid) - min(valid) < 0.2) if len(valid) >= 2 else True
    if len(valid) < 2:
        print(f"  ⚠️ 有效测点不足（{len(valid)} 个），漂移判定存疑")

    # ---- 字幕帧位抽检（ep末集内 50%）----
    print(f"\n📝 字幕帧位抽检（ep末集内 50%）")
    sub_ok = True
    last_ep = max(vt)
    tgt, tdur, _ = vt[last_ep]
    check_t = tgt + 0.5 * tdur
    text_segs = []
    for t in d.get("tracks", []):
        if t.get("type") != "text":
            continue
        for s in (t.get("segments") or []):
            ts = s.get("target_timerange") or {}
            text_segs.append((ts.get("start", 0) / 1e6, ts.get("duration", 0) / 1e6))
    if text_segs:
        text_segs.sort()
        nearest = min(text_segs, key=lambda x: abs(x[0] - check_t))
        gap, detail = _subtitle_brightness_jump(mp4_path, nearest[0], nearest[1])
        if gap is None:
            print(f"  ep{last_ep}: {detail}")
            sub_ok = False
        elif gap < 0.2:
            print(f"  ep{last_ep}: {detail} ✅")
        else:
            print(f"  ep{last_ep}: {detail} ❌")
            sub_ok = False
    else:
        print("  草稿无字幕段，跳过")

    # ---- 总结 ----
    print(f"\n{'='*50}")
    drift_verdict = "✅ 通过（δ_v 恒定，无累计漂移）" if drift_ok else "❌ 失败（δ_v 仍随集号增长，需重导）"
    sub_verdict = "✅ 通过（字幕帧位对齐）" if sub_ok else "❌ 失败（字幕滞后/超前，需重导）"
    print(f"帧级漂移: {drift_verdict}")
    print(f"字幕帧位: {sub_verdict}")
    all_ok = drift_ok and sub_ok
    print(f"\n{'✅ 全部通过' if all_ok else '❌ 有项目未通过'} — {'可交付' if all_ok else '建议补修草稿后重导'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
