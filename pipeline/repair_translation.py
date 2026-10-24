#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""英文字幕修复：对「已发布草稿」中翻译失败的集做高质量补翻。

检测模式（可叠加）：
  1. 自动空位检测：某集时间范围内有中文字幕但英文字幕为 0 → 视为空位
  2. 自动中文残留检测：英文字幕里仍夹带中文（人名未音译、整句没翻）→ 重翻该集
  3. --mymemory-log <日志>：扫描串流日志中 MyMemory 命中的集（机翻质量差），
     删除其现有英文字幕后重翻

用法：
  python pipeline\\repair_translation.py <草稿名> [--mymemory-log pipeline\\_stream_xx.log]
                                                [--eps 28,55] [--dry-run]

翻译链复用 pipeline.translate_texts（GLM→讯飞→Google→Ollama，全部软失败校验）。
写回前自动备份 draft_content.json；剪映打开该草稿时拒绝执行。
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

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import pipeline as pl  # noqa: E402

DRAFT_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                          "JianyingPro", "User Data", "Projects", "com.lveditor.draft")


def _load_draft(name):
    path = os.path.join(DRAFT_ROOT, name, "draft_content.json")
    if not os.path.isfile(path):
        raise SystemExit(f"✗ 草稿不存在: {path}")
    with io.open(path, encoding="utf-8") as f:
        return json.load(f), path


def _episode_ranges(draft):
    """主视频轨（segment 最多的 video 轨）按时间序给出每集 (start, end)。"""
    video_tracks = [t for t in draft.get("tracks", []) if t.get("type") == "video"]
    if not video_tracks:
        return []
    main = max(video_tracks, key=lambda t: len(t.get("segments", [])))
    segs = sorted(main.get("segments", []),
                  key=lambda s: (s.get("target_timerange") or {}).get("start", 0))
    out = []
    for s in segs:
        tr = s.get("target_timerange") or {}
        out.append((tr.get("start", 0), tr.get("start", 0) + tr.get("duration", 0)))
    return out


def _text_tracks(draft):
    """返回 (中文轨, 英文轨)：按 attribute 区分（2=中文，0=英文）。"""
    cn = en = None
    for t in draft.get("tracks", []):
        if t.get("type") != "text":
            continue
        attr = t.get("attribute", 0)
        if attr == 2 and cn is None:
            cn = t
        elif attr == 0 and en is None:
            en = t
    return cn, en


def _segs_in_range(track, start, end):
    out = []
    for s in track.get("segments", []):
        tr = s.get("target_timerange") or {}
        st = tr.get("start", 0)
        if start <= st < end:
            out.append(s)
    return out


def _mymemory_eps_from_log(log_path):
    """日志中「── 第N集」段落内出现 MyMemory 命中 → 集号列表。"""
    txt = io.open(log_path, encoding="utf-8", errors="ignore").read()
    hits = set()
    for m in re.finditer(r"── 第(\d+)集(.*?)── 第\d+集", txt, re.S):
        if "MyMemory 翻译" in m.group(2):
            hits.add(int(m.group(1)))
    # 最后一段没有下一个「第N集」边界
    tail = re.search(r"── 第(\d+)集(.*?)$", txt, re.S)
    if tail and "MyMemory 翻译" in tail.group(2):
        hits.add(int(tail.group(1)))
    return sorted(hits)


_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff01-\uff65]")


def _cjk_eps(draft, en_track, eps_ranges):
    """英文字幕里残留中文字符（含全角标点）的集号。"""
    hits = set()
    for i, (st, en) in enumerate(eps_ranges, 1):
        for seg in _segs_in_range(en_track, st, en):
            if _CJK_RE.search(_seg_text(draft, seg)):
                hits.add(i)
                break
    return sorted(hits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("draft_name")
    ap.add_argument("--mymemory-log", default=None)
    ap.add_argument("--eps", default=None, help="手工指定集号，逗号分隔")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    draft, path = _load_draft(args.draft_name)
    eps_ranges = _episode_ranges(draft)
    cn_track, en_track = _text_tracks(draft)
    if cn_track is None:
        raise SystemExit("✗ 未找到中文字幕轨（attribute=2）")
    if en_track is None:
        raise SystemExit("✗ 未找到英文字幕轨（attribute=0），无法克隆样式，暂不支持从零建轨")
    if not eps_ranges:
        raise SystemExit("✗ 未找到主视频轨")

    # 确定待修复集
    target = set()
    if args.eps:
        target |= {int(x) for x in args.eps.split(",") if x.strip()}
    if args.mymemory_log:
        target |= set(_mymemory_eps_from_log(args.mymemory_log))
    auto = set()
    for i, (st, en) in enumerate(eps_ranges, 1):
        cn_n = len(_segs_in_range(cn_track, st, en))
        en_n = len(_segs_in_range(en_track, st, en))
        if cn_n > 0 and en_n == 0:
            auto.add(i)
    cjk = set(_cjk_eps(draft, en_track, eps_ranges))
    target |= auto
    target |= cjk
    print(f"共 {len(eps_ranges)} 集；自动检测空位: {sorted(auto) or '无'}；"
          f"中文残留: {sorted(cjk) or '无'}；最终待修复: {sorted(target) or '无'}")
    if not target:
        print("✅ 无需修复")
        return

    if pl._jianying_running():
        print("⚠️ 剪映正在运行，若该草稿处于打开状态写回可能不生效，建议先关闭")

    en_template = next((s for s in en_track.get("segments", [])), None)
    if en_template is None:
        raise SystemExit("✗ 英文轨无任何现存 segment 可作样式模板，暂不支持")

    workdir = os.path.join(_HERE, "_work_repair")
    os.makedirs(workdir, exist_ok=True)

    total_fixed = 0
    for idx in sorted(target):
        if idx < 1 or idx > len(eps_ranges):
            print(f"  ✗ 第{idx}集超出范围，跳过")
            continue
        st, en = eps_ranges[idx - 1]
        cn_segs = _segs_in_range(cn_track, st, en)
        if not cn_segs:
            print(f"  ✗ 第{idx}集无中文字幕可参照，跳过")
            continue
        if args.dry_run:
            print(f"  [dry-run] 第{idx}集 将重翻 {len(cn_segs)} 条")
            continue

        # 删除该集范围现有英文段（MyMemory 场景），再整集重翻
        before = len(en_track["segments"])
        removed_segs = [s for s in en_track["segments"]
                        if st <= (s.get("target_timerange") or {}).get("start", 0) < en]
        en_track["segments"] = [s for s in en_track["segments"]
                                if not (st <= (s.get("target_timerange") or {}).get("start", 0) < en)]
        removed = len(removed_segs)

        cn_texts = [_seg_text(draft, s) for s in cn_segs]
        en_texts = pl.translate_texts(cn_texts, workdir)
        if not en_texts:
            # 翻译仍失败则放回旧段，保持草稿原状不丢字幕
            en_track["segments"].extend(removed_segs)
            print(f"  ✗ 第{idx}集修复翻译仍失败，已恢复原英文段 {removed} 条")
            continue
        left_cjk = sum(1 for t in en_texts if str(t).strip() and _CJK_RE.search(str(t)))
        for s, t in zip(cn_segs, en_texts):
            if not str(t).strip():
                continue
            ns = copy.deepcopy(en_template)
            ns["id"] = str(uuid.uuid4()).upper()
            tr = s.get("target_timerange") or {}
            ns["target_timerange"] = dict(tr)
            if "source_timerange" in ns:
                ns["source_timerange"] = dict(tr)
            _set_seg_text(draft, ns, str(t), en_template)
            en_track["segments"].append(ns)
            total_fixed += 1
        note = f"，⚠️ 仍有 {left_cjk} 条夹带中文" if left_cjk else ""
        print(f"  ✅ 第{idx}集修复完成（删旧 {removed} 条，"
              f"补入 {len([x for x in en_texts if str(x).strip()])} 条{note}）")

    if args.dry_run:
        return
    en_track["segments"].sort(key=lambda s: (s.get("target_timerange") or {}).get("start", 0))
    left_total = sum(1 for s in en_track["segments"] if _CJK_RE.search(_seg_text(draft, s)))
    if left_total:
        print(f"⚠️ 全片仍有 {left_total} 条英文字幕夹带中文（供应商未按 prompt 音译），"
              f"可再执行一次本脚本重试")
    if total_fixed > 0:
        backup = path + f".repairbak_{int(time.time())}"
        shutil.copy2(path, backup)
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False)
        print(f"💾 已写回草稿（备份: {os.path.basename(backup)}），共修复 {total_fixed} 条英文字幕")
    else:
        print("无可写回的修复内容")


def _find_material(draft, mid):
    for m in draft.get("materials", {}).get("texts", []):
        if m.get("id") == mid:
            return m
    return None


def _seg_text(draft, seg):
    m = _find_material(draft, seg.get("material_id"))
    if m and isinstance(m.get("content"), str):
        try:
            return json.loads(m["content"]).get("text", "")
        except Exception:
            pass
    return ""


def _set_seg_text(draft, seg, new_text, template_seg):
    """复用模板 segment 的 text material：新建一个材质副本替换文本（JSON 往返，转义安全）。"""
    m = _find_material(draft, template_seg.get("material_id"))
    if m is None:
        raise SystemExit("✗ 模板 segment 的 text material 解析失败")
    nm = copy.deepcopy(m)
    nm["id"] = str(uuid.uuid4()).upper()
    c = json.loads(m["content"])
    c["text"] = new_text
    # 关键：样式 range 必须跟着文本长度走，否则超出范围的字会用剪映默认大字号渲染
    rng = [0, len(new_text)]
    for st in c.get("styles") or []:
        st["range"] = list(rng)
    nm["content"] = json.dumps(c, ensure_ascii=False)
    draft.setdefault("materials", {}).setdefault("texts", []).append(nm)
    seg["material_id"] = nm["id"]


if __name__ == "__main__":
    main()
