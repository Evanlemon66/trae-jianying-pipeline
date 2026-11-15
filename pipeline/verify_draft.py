#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕/音频终检：体检 + 自动修复（中文残留 / 超长句 / styles.range）+ 音频素材校验。

两个入口，同一套逻辑：
  pipeline 内：from verify_draft import verify_and_fix, has_blocking, write_report
  单独执行  ：python pipeline\\verify_draft.py <草稿名|draft_content.json> [--fix]

体检项（阻断级）：中文残留、超长(>100字符)、styles.range 失配、空文本、时间重叠、
悬空材质引用、重复 segment/material id、中文字幕无英文覆盖、英文起点不在中文区间、
英文字幕残缺集、音频素材缺失或时长与人声轨不符、字幕成轨结构异常（零时长段 /
同起点压堆，见 text_pack_issues）。
仅报告级：>40 字符/秒高密度、<0.8s 碎片（ASR 压缩导致，不改视频无法解决）。

修复策略：每轮 ①修 range ②只修残留行（定向回译）③拆分超长；
若残留行修不掉，升级为整集重翻；最多 --rounds 轮（默认 3），未通过由调用方阻断。
"""
import argparse
import bisect
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import time
import uuid

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import draft_io  # noqa: E402  草稿写盘唯一原语
import repair_translation as rp  # noqa: E402  复用字幕轨/集范围/材质读写工具
import split_long_subtitles as sl  # noqa: E402
from no_window import NO_WINDOW  # noqa: E402

DRAFT_ROOT = rp.DRAFT_ROOT
CJK_RE = rp._CJK_RE

BLOCKING_KEYS = ("cjk", "long", "range", "empty", "overlap",
                 "dangling", "dup_seg", "dup_mat", "uncovered", "missing",
                 "zero_dur", "stacked", "audio")
REPORT_ONLY_KEYS = ("off_anchor", "dense", "short")

# 成轨结构判据阈值（text_pack_issues）
_TEXT_MIN_DUR_US = 50_000        # 单条字幕 <50ms：不可能读完，视作无效段（异常样例为 33ms）
_TEXT_STACK_MIN = 3              # 同一时刻 ≥3 条字幕且全部短于下一行阈值 → 视为整批压堆
_TEXT_STACK_MAX_DUR_US = 100_000 # 压堆段的时长上限（正常字幕不会这么短）
# 零时长段不再"一条就阻断"：同一条轨上达到 _ZERO_DUR_BLOCK_COUNT 段，
# 或占该轨总段数超过 _ZERO_DUR_BLOCK_RATIO 才阻断；否则只记 warn（写进 audit + 日志）。
# 每条轨各算各的（中文轨、英文轨不合并计数，避免长轨稀释比例）。
_ZERO_DUR_BLOCK_COUNT = 3
_ZERO_DUR_BLOCK_RATIO = 0.01     # 1%

_DEMUCS_STEMS = ("vocals.wav", "no_vocals.wav", "vocal.wav")
_FFPROBE_PATH = "unset"
_DUR_CACHE = {}


def _ffprobe_path():
    """定位 ffprobe（PATH → WinGet 安装目录）；找不到返回 None。"""
    global _FFPROBE_PATH
    if _FFPROBE_PATH != "unset":
        return _FFPROBE_PATH
    import glob as _glob
    p = shutil.which("ffprobe")
    if not p:
        pat = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                           "Packages", "*ffmpeg*", "ffmpeg-*", "bin", "ffprobe.exe")
        hit = _glob.glob(pat)
        p = hit[0] if hit else None
    _FFPROBE_PATH = p
    return p


def _media_duration(path):
    """用 ffprobe 读素材真实时长（秒）；失败返回 None。按 mtime+size 缓存。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _DUR_CACHE:
        return _DUR_CACHE[key]
    ff = _ffprobe_path()
    if not ff:
        return None
    r = subprocess.run([ff, "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True,
                       creationflags=NO_WINDOW)
    try:
        d = float((r.stdout or "").strip())
    except ValueError:
        d = None
    _DUR_CACHE[key] = d
    return d


def _audit_audio(draft, ep_at, tol=1.0):
    """音频素材一致性：文件缺失 / 短于时间轴 / demucs 干声与段长不符（阻断级）。

    背景：剪映只记录素材绝对路径，不会把音频复制进草稿；当共享 workdir 被别的
    任务按同名文件覆盖后（demucs_epNN），草稿会静默播放错误音频。
    """
    mats = {m.get("id"): m for m in draft.get("materials", {}).get("audios", [])}
    out = []
    for ti, t in enumerate(draft.get("tracks", [])):
        if t.get("type") != "audio":
            continue
        for s in t.get("segments") or []:
            m = mats.get(s.get("material_id")) or {}
            path = m.get("path")
            if not path:
                continue
            tr = s.get("target_timerange") or {}
            sr = s.get("source_timerange") or {}
            need = (sr.get("start", 0) + sr.get("duration", 0)
                    or tr.get("duration", 0)) / 1e6
            ep = ep_at(tr.get("start", 0))
            if not os.path.isfile(path):
                out.append({"ep": ep, "track": ti, "path": path,
                            "actual": None, "need": round(need, 2), "why": "文件缺失"})
                continue
            d = _media_duration(path)
            if d is None:
                continue
            base = os.path.basename(path).lower()
            if d < need - tol:
                out.append({"ep": ep, "track": ti, "path": path,
                            "actual": round(d, 2), "need": round(need, 2),
                            "why": "音频短于时间轴"})
            elif d > need + tol and base in _DEMUCS_STEMS:
                out.append({"ep": ep, "track": ti, "path": path,
                            "actual": round(d, 2), "need": round(need, 2),
                            "why": "人声素材与段长不符"})
    return out


# ────────────────────────── 载入/保存 ──────────────────────────

def load_draft(src):
    """草稿名 或 draft_content.json 路径 → (draft, path)。"""
    path = src if str(src).lower().endswith(".json") else \
        os.path.join(DRAFT_ROOT, src, "draft_content.json")
    if not os.path.isfile(path):
        raise SystemExit(f"✗ 草稿不存在: {path}")
    with io.open(path, encoding="utf-8") as f:
        return json.load(f), path


def save_draft(draft, path, writer=None):
    """写回草稿（备份 .verifybak_<ts>；校验/原子写/留痕由 draft_io 统一处理）。"""
    return draft_io.write_draft_content(
        draft, path, backup_suffix=".verifybak_{ts}", writer=writer)


# ────────────────────────── 体检 ──────────────────────────

def _text_track_kind(track):
    """字幕轨角色标签（只用于报错/日志定位）：中文轨 attr=2，英文轨 attr=0。"""
    attr = track.get("attribute", 0)
    if attr == 2:
        return "中文"
    if attr == 0:
        return "英文"
    return f"attr{attr}"


def text_pack_issues(draft):
    """识别/翻译成轨结构判据（只读，与草稿写入无关）。

    针对剪映「识别+翻译」偶发把整批译文压到同一时刻、每条各自单独成轨的异常
    （实测样例：25 条英文译文同落 647.733s、每条 33ms、25 条轨 → 草稿 32 轨/629 条 text）。
    只数字幕条数增量的验收看不出这种产出，必须在结构上拦。

    两条判据（都按轨分别统计，中/英不合并计数）：
      stacked   同一 start 上 ≥3 条且全部 <100ms 的压堆（≥3 照旧直接阻断）
      zero_dur  单条时长 ≤0 或 <50ms。**不再一条就阻断**：同一条轨上达到
                _ZERO_DUR_BLOCK_COUNT 段，或占该轨总段数超过 _ZERO_DUR_BLOCK_RATIO
                才阻断；达不到只记 warn（zero_dur_warn），照样写进 audit 与日志。

    返回 {"zero_dur": [...], "zero_dur_warn": [...], "zero_dur_stats": [...],
          "stacked": [...], "text_tracks": n}：
      zero_dur       阻断轨上的零时长段（逐段，含 ep/start/dur_ms/track/kind）
      zero_dur_warn  未达阈值的轨级统计（track/kind/total/count/ratio/first_*）
      zero_dur_stats 所有命中零时长的轨级统计（含 blocking/reason，便于复查）

    注：不按中/英总段数差判失败——长句拆分后英文轨合法地多于中文轨
    （实测生产草稿 CN 3687 / EN 3815、CN 2950 / EN 3839），该方向由
    missing（逐集 EN 少于 CN）与 uncovered 负责。
    """
    tracks = [(ti, t) for ti, t in enumerate(draft.get("tracks", []) or [])
              if t.get("type") == "text"]

    ep_starts = [a for a, _ in rp._episode_ranges(draft)]

    def _ep_at(us):
        if not ep_starts:
            return 1
        return max(1, bisect.bisect_right(ep_starts, us))

    zero_dur, zero_dur_warn, stats, by_start = [], [], [], {}
    for ti, t in tracks:
        segs = t.get("segments", []) or []
        kind = _text_track_kind(t)
        hits = []
        for s in segs:
            tr = s.get("target_timerange") or {}
            st, dur = tr.get("start", 0), tr.get("duration", 0)
            if dur <= 0 or dur < _TEXT_MIN_DUR_US:
                hits.append({"ep": _ep_at(st), "start": st,
                             "dur_ms": int(round(dur / 1000)),
                             "track": ti, "kind": kind})
            by_start.setdefault(st, []).append((dur, ti))
        if not hits:
            continue
        total = len(segs)
        ratio = (len(hits) / total) if total else 1.0
        by_count = len(hits) >= _ZERO_DUR_BLOCK_COUNT
        by_ratio = ratio > _ZERO_DUR_BLOCK_RATIO
        blocking = by_count or by_ratio
        info = {"track": ti, "kind": kind, "total": total,
                "count": len(hits), "ratio": round(ratio, 4),
                "ratio_pct": round(ratio * 100, 2),
                "first_start": hits[0]["start"],
                "first_dur_ms": hits[0]["dur_ms"],
                "blocking": blocking,
                "reason": (f"段数≥{_ZERO_DUR_BLOCK_COUNT}" if by_count
                           else (f"占比>{_ZERO_DUR_BLOCK_RATIO:.0%}" if by_ratio
                                 else "warn"))}
        stats.append(info)
        if blocking:
            zero_dur.extend(hits)
        else:
            zero_dur_warn.append(info)

    stacked = []
    for st, segs in sorted(by_start.items()):
        if len(segs) >= _TEXT_STACK_MIN and \
                all(d < _TEXT_STACK_MAX_DUR_US for d, _ in segs):
            stacked.append({"ep": _ep_at(st), "start": st, "count": len(segs),
                            "tracks": len({ti for _, ti in segs}),
                            "dur_ms": int(round(min(d for d, _ in segs) / 1000))})

    return {"zero_dur": zero_dur, "zero_dur_warn": zero_dur_warn,
            "zero_dur_stats": stats, "stacked": stacked,
            "text_tracks": len(tracks)}


def pack_brief(pack):
    """text_pack_issues 的阻断摘要（命中哪条判据、涉及多少段/哪条轨）；无阻断返回空串。"""
    if not pack or pack.get("error"):
        return ""
    parts = []
    if pack.get("stacked"):
        total = sum(x["count"] for x in pack["stacked"])
        x = pack["stacked"][0]
        parts.append(f"同起点堆叠{len(pack['stacked'])}处/{total}段"
                     f"(首处 {x['start'] / 1e6:.3f}s ×{x['count']}条/{x['tracks']}轨)")
    bad = [s for s in (pack.get("zero_dur_stats") or []) if s.get("blocking")]
    if bad:
        total = sum(s["count"] for s in bad)
        z = bad[0]
        parts.append(f"零时长超标{len(bad)}轨/{total}段"
                     f"（{z['kind']}轨#{z['track']} {z['count']}/{z['total']}"
                     f"={z['ratio_pct']:.2f}%，{z['reason']}，"
                     f"首条 {z['first_start'] / 1e6:.3f}s {z['first_dur_ms']}ms）")
    return " ".join(parts)


def pack_warn_brief(pack):
    """零时长未达阻断阈值时的告警明细（必须进日志，不许静默）；无告警返回空串。"""
    if not pack or pack.get("error"):
        return ""
    ws = pack.get("zero_dur_warn") or []
    return "；".join(
        f"{w['kind']}轨#{w['track']} 零时长{w['count']}/{w['total']}段"
        f"={w['ratio_pct']:.2f}%，首条 {w['first_start'] / 1e6:.3f}s {w['first_dur_ms']}ms"
        for w in ws)



def audit(draft):
    """只读体检，返回结构化报告 dict。"""
    eps = rp._episode_ranges(draft)
    cn, en = rp._text_tracks(draft)
    cn_segs = cn.get("segments", []) if cn else []
    en_segs = sorted(en.get("segments", []) if en else [],
                     key=lambda s: s["target_timerange"]["start"])
    texts = draft.get("materials", {}).get("texts", [])
    mid_set = {m["id"] for m in texts}

    issues = {k: [] for k in BLOCKING_KEYS + ("off_anchor", "zero_dur_warn")}
    dense, short = [], []
    all_ids = []
    for t in draft.get("tracks", []):
        all_ids.extend(s["id"] for s in t.get("segments", []))

    ep_starts = [a for a, _ in eps]

    def ep_at(us):
        k = bisect.bisect_right(ep_starts, us) - 1
        return max(1, k + 1)

    # 按集归属，便于报告定位
    for s in en_segs:
        st = s["target_timerange"]["start"]
        ep = ep_at(st)
        text = rp._seg_text(draft, s)
        dur = s["target_timerange"]["duration"]

        if CJK_RE.search(text):
            issues["cjk"].append({"ep": ep, "start": st, "text": text})
        if len(text) > 100:
            issues["long"].append({"ep": ep, "start": st, "chars": len(text)})
        if not text.strip():
            issues["empty"].append({"ep": ep, "start": st})

        m = rp._find_material(draft, s["material_id"])
        try:
            c = json.loads(m["content"]) if m and isinstance(m.get("content"), str) else {}
        except Exception:
            c = {}
        if any(st_.get("range") != [0, len(c.get("text", ""))] for st_ in (c.get("styles") or [])):
            issues["range"].append({"ep": ep, "start": st})

        cps = len(text) / max(0.1, dur / 1e6)
        if cps > 40:
            dense.append({"ep": ep, "start": st, "cps": round(cps, 1), "chars": len(text)})
        if dur < 800_000:
            short.append({"ep": ep, "start": st, "dur": round(dur / 1e6, 2)})

    # 重复 id / 悬空引用
    issues["dup_seg"] = len(all_ids) - len(set(all_ids))
    issues["dup_mat"] = len(texts) - len(mid_set)
    dangling = sum(1 for s in cn_segs + en_segs if s.get("material_id") not in mid_set)
    issues["dangling"] = dangling

    # 时间重叠
    for a, b in zip(en_segs, en_segs[1:]):
        ae = a["target_timerange"]["start"] + a["target_timerange"]["duration"]
        if b["target_timerange"]["start"] < ae - 1:
            issues["overlap"].append({
                "ep": None, "start": a["target_timerange"]["start"],
                "next": b["target_timerange"]["start"]})

    # 中英文覆盖关系
    cn_iv = sorted((s["target_timerange"]["start"],
                    s["target_timerange"]["start"] + s["target_timerange"]["duration"])
                   for s in cn_segs)
    en_starts = [s["target_timerange"]["start"] for s in en_segs]
    ci = 0
    for st in en_starts:
        while ci + 1 < len(cn_iv) and cn_iv[ci][1] <= st:
            ci += 1
        if ci >= len(cn_iv) or not (cn_iv[ci][0] <= st < cn_iv[ci][1]):
            issues["off_anchor"].append({"ep": ep_at(st), "start": st})
    j = 0
    for a, b in cn_iv:
        while j < len(en_starts) and en_starts[j] < a:
            j += 1
        if j >= len(en_starts) or en_starts[j] >= b:
            issues["uncovered"].append({"ep": ep_at(a), "start": a})

    # 逐集段落数（英文少于中文 3 条以上 → 疑似残缺）
    for i, (st, ed) in enumerate(eps, 1):
        nc = len(rp._segs_in_range(cn, st, ed))
        ne = len(rp._segs_in_range(en, st, ed))
        if nc > 0 and ne < nc - 2:
            issues["missing"].append({"ep": i, "cn": nc, "en": ne})

    # 成轨结构（识别/翻译异常：零时长、同起点压堆）
    # 零时长已改为「按轨计数/占比超阈值才阻断」，未达阈值落 zero_dur_warn（仅告警，不阻断）
    pack = text_pack_issues(draft)
    issues["zero_dur"] = pack["zero_dur"]
    issues["zero_dur_warn"] = pack["zero_dur_warn"]
    issues["stacked"] = pack["stacked"]

    # 音频素材一致性（防共享 workdir 被别的任务覆盖）
    issues["audio"] = _audit_audio(draft, ep_at)

    return {
        "episodes": len(eps),
        "duration_s": round((eps[-1][1] if eps else 0) / 1e6, 1),
        "tracks": len(draft.get("tracks", [])),
        "en_segments": len(en_segs),
        "cn_segments": len(cn_segs),
        "audio_check": "ok" if _ffprobe_path() else "skipped(未找到 ffprobe)",
        "issues": issues,
        "dense": dense,
        "short": short,
    }


def blocking_keys(rep):
    """返回实际非空的阻断项类别列表（如 ["missing"]）。"""
    def _bad(v):
        return v > 0 if isinstance(v, int) else bool(v)

    return [k for k in BLOCKING_KEYS if _bad(rep["issues"][k])]


def has_blocking(rep):
    return bool(blocking_keys(rep))


def brief(rep):
    it = rep["issues"]
    return (f"残留{len(it['cjk'])} 超长{len(it['long'])} range{len(it['range'])} "
            f"空{len(it['empty'])} 重叠{len(it['overlap'])} 悬空{it['dangling']} "
            f"重复{it['dup_seg']}/{it['dup_mat']} 未覆盖{len(it['uncovered'])} "
            f"错位{len(it['off_anchor'])} 残缺{len(it['missing'])} "
            f"零时长{len(it['zero_dur'])} 堆叠{len(it['stacked'])} "
            f"零时长告警{len(it.get('zero_dur_warn') or [])} "
            f"音频{len(it['audio'])}")


def format_report(rep):
    """人类可读摘要（单独执行时打印）。"""
    it = rep["issues"]
    lines = [
        f"  {rep['episodes']}集 {rep['tracks']}轨 {rep['duration_s']}s  "
        f"EN {rep['en_segments']} / CN {rep['cn_segments']}  "
        f"音频检查 {rep.get('audio_check', 'ok')}",
        f"  中文残留 {len(it['cjk'])} | 超长 {len(it['long'])} | range异常 {len(it['range'])} | "
        f"空文本 {len(it['empty'])} | 重叠 {len(it['overlap'])}",
        f"  悬空引用 {it['dangling']} | 重复 segment {it['dup_seg']} / 材质 {it['dup_mat']} | "
        f"中文段无英文 {len(it['uncovered'])} | 起点错位 {len(it['off_anchor'])} | "
        f"残缺集 {len(it['missing'])} | 音频素材 {len(it['audio'])}",
        f"  成轨结构：零时长 {len(it['zero_dur'])} | 同起点堆叠 {len(it['stacked'])} | "
        f"零时长告警(不阻断) {len(it.get('zero_dur_warn') or [])}",
        f"  仅报告：高密度 {len(rep['dense'])} | <0.8s {len(rep['short'])}",
    ]
    for w in it.get("zero_dur_warn") or []:
        lines.append(f"    [零时长告警] {w['kind']}轨#{w['track']} "
                     f"{w['count']}/{w['total']}段={w['ratio_pct']:.2f}% "
                     f"首条 {w['first_start']/1e6:.3f}s {w['first_dur_ms']}ms（不阻断）")
    for key, label in (("cjk", "残留"), ("long", "超长"), ("missing", "残缺"),
                       ("zero_dur", "零时长"), ("stacked", "堆叠"),
                       ("audio", "音频"), ("off_anchor", "起点错位(仅报告)"), ("range", "range")):
        for x in it[key][:5]:
            if key == "cjk":
                lines.append(f"    [{label}] EP{x['ep']} {x['start']/1e6:.2f}s  {x['text'][:70]}")
            elif key == "long":
                lines.append(f"    [{label}] EP{x['ep']} {x['start']/1e6:.2f}s  {x['chars']}字符")
            elif key == "missing":
                lines.append(f"    [{label}] 第{x['ep']}集 CN{x['cn']} / EN{x['en']}")
            elif key == "zero_dur":
                lines.append(f"    [{label}] EP{x['ep']} {x['start']/1e6:.3f}s  "
                             f"{x['dur_ms']}ms")
            elif key == "stacked":
                lines.append(f"    [{label}] EP{x['ep']} {x['start']/1e6:.3f}s  "
                             f"{x['count']}条同起点/{x['tracks']}轨（{x['dur_ms']}ms）")
            elif key == "audio":
                lines.append(f"    [{label}] EP{x['ep']} 轨{x['track']} {x['why']}  "
                             f"实际{x['actual']}s / 需要{x['need']}s  {os.path.basename(x['path'])}")
            else:
                lines.append(f"    [{label}] {x['start']/1e6:.2f}s")
    return "\n".join(lines)


def write_report(workdir, name, rep, history=None):
    """落盘 JSON 报告，便于事后复查。"""
    try:
        os.makedirs(workdir, exist_ok=True)
        path = os.path.join(workdir, "_subtitle_audit.json")
        payload = {"draft": name, "ts": int(time.time()), "report": rep,
                   "history": history or []}
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        return path
    except Exception as e:
        print(f"  [warn] 终检报告写入失败: {e}")
        return None


# ────────────────────────── 修复 ──────────────────────────

def fix_ranges(draft):
    """把 styles.range 修正为 [0, len(text)]，返回修正条数。"""
    cn, en = rp._text_tracks(draft)
    n = 0
    for s in (en.get("segments", []) if en else []):
        m = rp._find_material(draft, s["material_id"])
        if not m or not isinstance(m.get("content"), str):
            continue
        try:
            c = json.loads(m["content"])
        except Exception:
            continue
        text = c.get("text", "")
        rng = [0, len(text)]
        if any(st.get("range") != rng for st in (c.get("styles") or [])):
            for st in c.get("styles") or []:
                st["range"] = list(rng)
            m["content"] = json.dumps(c, ensure_ascii=False)
            n += 1
    return n


def _cjk_segments(draft):
    """[(集号, segment, 文本)]：英文字幕轨里夹带中文的段。"""
    eps = rp._episode_ranges(draft)
    cn, en = rp._text_tracks(draft)
    if en is None:
        return []
    out = []
    for i, (st, ed) in enumerate(eps, 1):
        for s in rp._segs_in_range(en, st, ed):
            t = rp._seg_text(draft, s)
            if CJK_RE.search(t):
                out.append((i, s, t))
    return out


def fix_cjk_lines(draft, log=print):
    """只重译夹带中文的行（GLM→讯飞→Ollama 定向回译），返回修好的条数。"""
    import vc_bridge as vb

    hits = _cjk_segments(draft)
    if not hits:
        return 0
    texts = [t for _, _, t in hits]
    idxs = list(range(len(texts)))
    cfg = vb.load_config()

    out = []
    for p in cfg.get("providers", []):
        if not p.get("api_key"):
            continue
        out = vb._rewrite_cjk_openai(p, texts, idxs)
        if out and len(out) == len(texts):
            log(f"  [cjk] {p.get('name')} 回译 {len(texts)} 行")
            break
        out = []
    if not out:
        oc = cfg.get("ollama") or {}
        if oc.get("enabled", True):
            out = vb._rewrite_cjk_ollama(
                oc.get("host", "http://127.0.0.1:11434"),
                oc.get("model", "qwen3:8b"), texts, idxs)
            if out:
                log(f"  [cjk] ollama 回译 {len(texts)} 行")
    if not out or len(out) != len(texts):
        log("  [cjk] 回译未返回有效结果，留给整集重翻")
        return 0

    n = 0
    for (ep, seg, old), new in zip(hits, out):
        new = (new or "").strip()
        if new and not vb.has_cjk(new):
            rp._set_seg_text(draft, seg, new, seg)
            log(f"  [cjk] EP{ep} {seg['target_timerange']['start']/1e6:.2f}s  "
                f"{old[:40]} → {new[:60]}")
            n += 1
    return n


def repair_episodes(draft, ep_idxs, workdir, log=print):
    """整集重翻（删旧英文段，按中文字幕逐条重建），返回补入条数。"""
    import pipeline as pl

    eps = rp._episode_ranges(draft)
    cn, en = rp._text_tracks(draft)
    if not en or not en.get("segments"):
        return 0
    tpl = en["segments"][0]
    total = 0
    for idx in ep_idxs:
        if idx < 1 or idx > len(eps):
            continue
        st, ed = eps[idx - 1]
        cn_segs = rp._segs_in_range(cn, st, ed)
        if not cn_segs:
            continue
        removed = [s for s in en["segments"]
                   if st <= s["target_timerange"]["start"] < ed]
        en["segments"] = [s for s in en["segments"]
                          if not (st <= s["target_timerange"]["start"] < ed)]
        out = pl.translate_texts([rp._seg_text(draft, s) for s in cn_segs], workdir)
        if not out:
            en["segments"].extend(removed)
            log(f"  ✗ 第{idx}集重翻失败，已保留原英文字幕 {len(removed)} 条")
            continue
        added = 0
        for s, t in zip(cn_segs, out):
            if not str(t).strip():
                continue
            ns = copy.deepcopy(tpl)
            ns["id"] = str(uuid.uuid4()).upper()
            ns["target_timerange"] = dict(s["target_timerange"])
            if "source_timerange" in ns:
                ns["source_timerange"] = dict(s["target_timerange"])
            rp._set_seg_text(draft, ns, str(t), tpl)
            en["segments"].append(ns)
            added += 1
        total += added
        log(f"  ✅ 第{idx}集整集重翻（删旧 {len(removed)} 条，补入 {added} 条）")
    en["segments"].sort(key=lambda s: s["target_timerange"]["start"])
    return total


def verify_and_fix(draft, workdir, rounds=3, max_chars=100, min_dur=0.8, log=print):
    """体检 → 修复 → 复扫，最多 rounds 轮。返回 (最终报告, 轮次历史)。"""
    history = []
    rep = audit(draft)
    if not has_blocking(rep):
        log(f"  ✅ 字幕终检通过：{brief(rep)}")
        return rep, history

    for r in range(1, rounds + 1):
        log(f"  [终检] 第{r}/{rounds}轮  待修：{brief(rep)}")
        acts = {
            "range": fix_ranges(draft),
            "cjk_lines": fix_cjk_lines(draft, log),
            "split": sl.split_long_segments(draft, max_chars, min_dur, "en", False, log),
        }
        rep = audit(draft)
        retry = sorted({x["ep"] for k in ("cjk", "missing", "uncovered")
                        for x in rep["issues"][k]})
        if retry:
            acts["retranslate_eps"] = repair_episodes(draft, retry, workdir, log)
            rep = audit(draft)
        history.append({"round": r, "actions": acts, "after": brief(rep)})
        log(f"  [终检] 第{r}轮结果：{brief(rep)}  （修复 {sum(acts.values())} 处）")
        if not has_blocking(rep) or sum(acts.values()) == 0:
            break
    return rep, history


# ────────────────────────── CLI ──────────────────────────

def main():
    ap = argparse.ArgumentParser(description="草稿字幕终检（体检 + 自动修复）")
    ap.add_argument("draft", help="草稿名 或 draft_content.json 路径")
    ap.add_argument("--fix", action="store_true", help="体检并自动修复（默认只体检）")
    ap.add_argument("--rounds", type=int, default=3, help="最多修复轮数（默认 3）")
    ap.add_argument("--max-chars", type=int, default=100)
    ap.add_argument("--min-dur", type=float, default=0.8)
    ap.add_argument("--json", action="store_true", help="输出 JSON 报告")
    ap.add_argument("--dry-run", action="store_true", help="配合 --fix：只演练不写回")
    args = ap.parse_args()

    draft, path = load_draft(args.draft)
    name = os.path.basename(os.path.dirname(path))
    print(f"📄 {path}")

    if not args.fix:
        rep = audit(draft)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(format_report(rep))
        print("\n" + ("✗ 存在阻断级问题" if has_blocking(rep) else "✅ 体检通过"))
        raise SystemExit(2 if has_blocking(rep) else 0)

    workdir = os.path.join(_HERE, "_work")
    rep, history = verify_and_fix(draft, workdir, args.rounds, args.max_chars, args.min_dur)
    print(format_report(rep))
    changed = sum(sum(h["actions"].values()) for h in history)
    if changed and not args.dry_run:
        bak = save_draft(draft, path, writer="verify_fix")
        print(f"\n💾 已写回（修复 {changed} 处，备份: {os.path.basename(bak)}）")
    elif changed:
        print(f"\n[dry-run] 预计修复 {changed} 处，未写回")
    else:
        print("\n无需修复")
    path_rep = write_report(workdir, name, rep, history)
    if path_rep:
        print(f"📝 报告: {path_rep}")
    print("✗ 仍有阻断级问题" if has_blocking(rep) else "✅ 终检通过")
    raise SystemExit(2 if has_blocking(rep) else 0)


if __name__ == "__main__":
    main()
