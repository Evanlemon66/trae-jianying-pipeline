# -*- coding: utf-8 -*-
"""ASR 通道抽检：对指定集强制只用 jianying 重识别，与草稿中 bijian 兜底版中文字幕对比。
用法: python _spotcheck_asr.py <draft_name> <workdir> <ep1,ep2,...>"""
import difflib
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import vc_bridge  # noqa: E402

JY_DRAFT_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "JianyingPro", "User Data", "Projects", "com.lveditor.draft"
)


def _episode_offsets(workdir):
    """从 workdir 各集音频时长推 offset（串流主轨顺序拼接）。"""
    eps = []
    for f in sorted(os.listdir(workdir)):
        m = re.fullmatch(r"audio_ep(\d+)\.wav", f)
        if m:
            eps.append(int(m.group(1)))
    eps.sort()
    import wave
    offsets, acc = {}, 0.0
    for ep in eps:
        offsets[ep] = acc
        try:
            with wave.open(os.path.join(workdir, f"audio_ep{ep}.wav")) as w:
                acc += w.getnframes() / w.getframerate()
        except Exception:
            acc += 120.0
    return offsets, acc


def _draft_cn_texts(draft_name, offsets, total):
    """读草稿中文字幕轨，按 offset 区间归集。"""
    dc = os.path.join(JY_DRAFT_ROOT, draft_name, "draft_content.json")
    with open(dc, "r", encoding="utf-8") as f:
        data = json.load(f)
    texts = {t["id"]: t.get("content", "") for t in data.get("materials", {}).get("texts", [])}
    out = {}
    ep_list = sorted(offsets)
    for tr in data.get("tracks", []):
        if tr.get("type") != "text" or tr.get("attribute") != 2:
            continue  # 中文字幕轨约定 attribute=2
        for seg in tr.get("segments", []):
            content = texts.get(seg.get("material_id"), "")
            plain = re.sub(r"<[^>]+>", "", content).strip()
            if not plain:
                continue
            tr = seg.get("target_timerange") or seg.get("time_range") or {}
            start_s = tr.get("start", 0) / 1_000_000
            ep = None
            for i, e in enumerate(ep_list):
                hi = offsets[ep_list[i + 1]] if i + 1 < len(ep_list) else total
                if offsets[e] - 0.5 <= start_s < hi - 0.5:
                    ep = e
                    break
            if ep:
                out.setdefault(ep, []).append(plain)
    return out


def _jianying_texts(wav_path, workdir):
    out_srt = os.path.join(workdir, "_spotcheck.srt")
    subs = vc_bridge.vc_asr_with_fallback(wav_path, out_srt, engines=("jianying",))
    return [re.sub(r"<[^>]+>", "", s.get("text", "")).strip() for s in subs if s.get("text")]


def _whisper_texts(wav_path):
    """本地 faster-whisper 仲裁通道（画像默认 medium/cuda/float16，模型命中 D 盘缓存）。"""
    from export_config import get_deps_root
    os.environ.setdefault("HF_HUB_CACHE",
                          os.path.join(get_deps_root(), "models", "huggingface", "hub"))
    import pipeline as pl
    return [s["text"] for s in pl.run_whisper(wav_path)]


def main():
    draft_name, workdir = sys.argv[1], sys.argv[2]
    pick = [int(x) for x in sys.argv[3].split(",")]
    mode = sys.argv[4] if len(sys.argv) > 4 else "jianying"
    offsets, total = _episode_offsets(workdir)
    cn = _draft_cn_texts(draft_name, offsets, total)
    print(f"抽检集: {pick}  仲裁通道: {mode}  (草稿中文字幕覆盖 {len(cn)} 集)\n")
    for ep in pick:
        wav = os.path.join(workdir, f"audio_ep{ep}.wav")
        if not os.path.isfile(wav):
            print(f"第{ep}集: 音频缺失，跳过"); continue
        bijian = cn.get(ep, [])
        try:
            jy = _whisper_texts(wav) if mode == "whisper" else _jianying_texts(wav, workdir)
        except Exception as e:
            print(f"第{ep}集: {mode} 重识别失败: {e}"); continue
        b_text = "。".join(bijian)
        j_text = "。".join(jy)
        sim = difflib.SequenceMatcher(None, b_text, j_text).ratio()
        print(f"── 第{ep}集: bijian {len(bijian)} 条 / jianying {len(jy)} 条, 文本相似度 {sim:.1%}")
        sm = difflib.SequenceMatcher(None, bijian, jy)
        shown = 0
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal" and shown < 3:
                b_seg = " / ".join(bijian[i1:i2])[:60]
                j_seg = " / ".join(jy[j1:j2])[:60]
                print(f"   差异: bijian[{b_seg}] vs jianying[{j_seg}]")
                shown += 1
        print()


if __name__ == "__main__":
    main()
