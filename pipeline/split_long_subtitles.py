#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超长字幕拆分：把英文字幕轨里过长的行按句/分句边界拆成多条，并按字符比例重排时间。

背景：ASR（剪映云）有时会把多句话合成一个 segment（中文 60~70 字 / 6~12s），
英文按 1:1 翻译后变成 300+ 字符，在 1080x1920 画面上会溢出 5~7 行。

规则：
  1. 只在句末（. ! ? …）切；不够短再退到 ; : ，最后退到逗号、空格
  2. 每条不超过 --max-chars（默认 100 字符，约 3 行）
  3. 时间按各条字符数比例分配（总时长不变），每条尽量不少于 --min-dur 秒（默认 0.8s）；
     若原段时长连两条 min_dur 都放不下，则不再合并回一条，改用均分保证不溢出
  4. 中文字幕轨不动（默认隐藏），只修可见的英文轨

用法：
  python pipeline\\split_long_subtitles.py <草稿名> [--max-chars 100] [--min-dur 0.8]
                                          [--dry-run] [--tracks en]
写回前自动备份 draft_content.json；剪映正在运行且该草稿已打开时不要执行。
"""
import argparse
import copy
import io
import json
import os
import re
import shutil
import sys
import time
import uuid

sys.stdout.reconfigure(encoding="utf-8")

DRAFT_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "JianyingPro", "User Data", "Projects", "com.lveditor.draft")

# 优先句末，其次分号/冒号，再退到逗号
_CUT_PATTERNS = [
    re.compile(r"(?<=[.!?…])\s+"),
    re.compile(r"(?<=[;:])\s+"),
    re.compile(r"(?<=,)\s+"),
]


def _chunks(text, max_len):
    """把一段长文本切成若干 <= max_len 的片段，尽量落在句/分句边界。"""
    out = []
    rest = text.strip()
    while len(rest) > max_len:
        window = rest[:max_len + 1]
        cut = -1
        for pat in _CUT_PATTERNS:
            hits = [m.start() for m in pat.finditer(window)]
            if hits:
                cut = hits[-1]
                break
        if cut <= 0:
            cut = rest.rfind(" ", 0, max_len + 1)
        if cut <= 0:
            cut = max_len
        piece = rest[:cut].strip()
        if piece:
            out.append(piece)
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def _alloc(duration_us, chunks, min_us):
    """按字符数比例分配时长，保证总和不变、每段尽量不低于 min_us。"""
    weights = [max(1, len(c)) for c in chunks]
    total = sum(weights)
    durs = [duration_us * w / total for w in weights]
    deficit = sum(max(0.0, min_us - d) for d in durs)
    if deficit > 0:
        donors = [i for i, d in enumerate(durs) if d > min_us]
        pool = sum(durs[i] - min_us for i in donors)
        if pool >= deficit:
            for i in donors:
                durs[i] -= (durs[i] - min_us) * deficit / pool
            durs = [max(min_us, d) for d in durs]
        else:
            # 总时长根本不够：均分，避免出现 0.1~0.3s 的闪烁字幕
            durs = [duration_us / len(chunks)] * len(chunks)
    durs = [int(d) for d in durs]
    durs[-1] += duration_us - sum(durs)
    return durs


def _text_of(material):
    try:
        return json.loads(material["content"]).get("text", "")
    except Exception:
        return ""


def _set_text(material, text):
    c = json.loads(material["content"])
    c["text"] = text
    for st in c.get("styles") or []:
        st["range"] = [0, len(text)]
    material["content"] = json.dumps(c, ensure_ascii=False)
    if material.get("words"):
        material["words"] = {"end_time": [], "start_time": [], "text": []}


def _add_text_material(draft, template):
    nm = copy.deepcopy(template)
    nm["id"] = str(uuid.uuid4()).upper()
    draft.setdefault("materials", {}).setdefault("texts", []).append(nm)
    return nm


def _add_anim_material(draft, src_id):
    """为拆分出来的段各建一个独立空动画材质（避免多段共用）。"""
    for m in draft.get("materials", {}).get("material_animations", []):
        if m.get("id") == src_id:
            nm = copy.deepcopy(m)
            nm["id"] = str(uuid.uuid4()).upper()
            draft["materials"]["material_animations"].append(nm)
            return nm["id"]
    return src_id


def split_long_segments(draft, max_chars=100, min_dur=0.8, tracks="en", dry_run=False, log=print):
    """就地拆分 draft 里的超长字幕段，返回净新增段数（可被 pipeline 直接调用）。

    :param draft: 剪映草稿 dict（原地修改）
    :param tracks: en=只拆英文轨（默认），all=中英都拆
    """
    mats = {m["id"]: m for m in draft.get("materials", {}).get("texts", [])}
    text_tracks = [t for t in draft.get("tracks", []) if t.get("type") == "text"]
    targets = [t for t in text_tracks if t.get("attribute", 0) == 0]
    if tracks == "all":
        targets = text_tracks
    if not targets:
        log("✗ 未找到字幕轨")
        return 0

    min_us = int(min_dur * 1_000_000)
    total_new = 0
    for track in targets:
        long_segs = []
        for s in track.get("segments", []):
            m = mats.get(s["material_id"])
            if m and len(_text_of(m)) > max_chars:
                long_segs.append(s)
        log(f"轨道 attribute={track.get('attribute', 0)}：{len(track.get('segments', []))} 段，"
            f"超过 {max_chars} 字符的 {len(long_segs)} 条")
        if not long_segs:
            continue

        to_add = []
        starts = sorted(s["target_timerange"]["start"] for s in track["segments"])
        for s in long_segs:
            m = mats[s["material_id"]]
            text = _text_of(m)
            chunks = _chunks(text, max_chars)
            if len(chunks) < 2:
                continue
            tr = s["target_timerange"]
            start, dur = tr["start"], tr["duration"]
            # 时长不够分（拆完会低于 min_dur）→ 先借后续空隙，再合并尾部
            next_start = next((x for x in starts if x > start), start + dur)
            if len(chunks) * min_us > dur:
                gap = next_start - (start + dur)
                if gap > 0:
                    dur += min(gap, len(chunks) * min_us - dur)
                max_n = int(dur // min_us)
                if max_n >= 2:
                    while len(chunks) > max_n and len(chunks) > 1:
                        i = min(range(len(chunks) - 1),
                                key=lambda k: len(chunks[k]) + len(chunks[k + 1]))
                        chunks[i:i + 2] = [chunks[i] + " " + chunks[i + 1]]
                # max_n < 2：这段连两条 min_dur 都放不下，优先避免巨字溢出，
                # 保持拆分，由 _alloc 退化为均分（时长本身已不可能正常阅读）
            durs = _alloc(dur, chunks, min_us)
            if dry_run:
                log(f"  {start/1e6:8.1f}s [{len(text)}字符/{tr['duration']/1e6:.1f}s"
                    f"→{dur/1e6:.1f}s] → {len(chunks)} 条: "
                    + " | ".join(f"{len(c)}字/{d/1e6:.1f}s" for c, d in zip(chunks, durs)))
            cur = start
            for c, d in zip(chunks, durs):
                ns = copy.deepcopy(s)
                ns["id"] = str(uuid.uuid4()).upper()
                ns["target_timerange"] = {"start": cur, "duration": d}
                if ns.get("source_timerange"):
                    ns["source_timerange"] = None
                nm = _add_text_material(draft, m)
                _set_text(nm, c)
                ns["material_id"] = nm["id"]
                refs = ns.get("extra_material_refs") or []
                if len(refs) == 1:
                    ns["extra_material_refs"] = [_add_anim_material(draft, refs[0])]
                to_add.append(ns)
                cur += d
            total_new += len(chunks) - 1

        if not dry_run:
            ids = {s["id"] for s in long_segs}
            track["segments"] = [s for s in track["segments"] if s["id"] not in ids] + to_add
            track["segments"].sort(key=lambda s: s["target_timerange"]["start"])

    return total_new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft_name")
    ap.add_argument("--max-chars", type=int, default=100)
    ap.add_argument("--min-dur", type=float, default=0.8)
    ap.add_argument("--tracks", default="en", choices=["en", "all"], help="en=只拆英文轨（默认），all=中英都拆")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = os.path.join(DRAFT_ROOT, args.draft_name, "draft_content.json")
    if not os.path.isfile(path):
        raise SystemExit(f"✗ 草稿不存在: {path}")
    with io.open(path, encoding="utf-8") as f:
        draft = json.load(f)

    total_new = split_long_segments(draft, args.max_chars, args.min_dur,
                                    args.tracks, args.dry_run)

    if args.dry_run:
        print(f"\n[dry-run] 预计新增 {total_new} 条字幕，未写回")
        return
    if total_new <= 0:
        print("无需拆分")
        return

    bak = path + f".splitbak_{int(time.time())}"
    shutil.copy2(path, bak)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False)
    print(f"\n💾 已写回（备份: {os.path.basename(bak)}），净增 {total_new} 条字幕")


if __name__ == "__main__":
    main()
