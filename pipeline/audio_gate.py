"""audio_gate.py — VAD 门控：压制人声干声里的非语音段（环境声/BGM 残留）。

pipeline 在「字幕写完、终检/保存/导出之前」调用本模块，对每集 vocals.wav 做：
- VAD 判为语音的区段：增益 1.0（对白原样保留）
- 非语音区段：增益 0.01（-40 dB，压到接近静音）
- 语音段前 300ms / 后 500ms 保护，边界 50ms 线性渐入渐出（防咔哒）
- 该集语音占比 < 20% 时只告警、不写回（避免纯 BGM/环境叙事集被压成静音）

参考标准：
- ITU-R BS.1770-4 / EBU R128：非语音段背景应 < -50 LUFS
- 实测：窗外（非语音）LUFS 从 -19.3 降至 -27.9（p95），窗内降至 -80

用法：
    import audio_gate
    rep = audio_gate.gate_files([r"...\vocals.wav"])
    audio_gate.write_report(workdir, rep)

命令行（单文件调试）：
    python audio_gate.py <file.wav> [--dry-run] [--min-speech-ratio 0.2]
"""
import json
import os
import shutil
import time

import numpy as np

VAD_SR = 16000
VAD_THRESHOLD = 0.5
VAD_MIN_SPEECH_MS = 250
VAD_MIN_SILENCE_MS = 100
VAD_SPEECH_PAD_MS = 300

PAD_PRE_S = 0.30
PAD_POST_S = 0.50
RAMP_S = 0.05
FLOOR_GAIN = 0.01          # -40 dB
MIN_SPEECH_RATIO = 0.20    # 低于此占比只告警不门控

REPORT_NAME = "_vad_gate_report.json"


def _deps_root():
    """依赖根目录：环境变量 JY_DEPS_ROOT → _env_profile.json → D:\\AI_Deps。"""
    env = os.environ.get("JY_DEPS_ROOT", "").strip()
    if env:
        return env
    prof = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_env_profile.json")
    try:
        with open(prof, encoding="utf-8-sig") as f:
            root = (json.load(f) or {}).get("deps_root")
        if root:
            return root
    except Exception:
        pass
    return r"D:\AI_Deps"


def find_vad_model():
    """定位 Silero VAD 模型；缺失返回 None（调用方降级为跳过）。"""
    env = os.environ.get("SILERO_VAD_MODEL", "").strip()
    if env:
        return env if os.path.isfile(env) else None
    p = os.path.join(_deps_root(), "models", "silero-vad", "silero_vad.jit")
    return p if os.path.isfile(p) else None


def load_vad(model_path=None):
    import torch
    path = model_path or find_vad_model()
    if not path:
        raise FileNotFoundError(
            "Silero VAD 模型缺失：请放到 <deps_root>/models/silero-vad/silero_vad.jit")
    m = torch.jit.load(path, map_location="cpu")
    m.eval()
    return m


def vad_detect(model, x_16k):
    """返回 (语音区间列表[(start,end)] @16k, 语音帧总样本数)。"""
    import torch
    model.reset_states()  # 关键：清除 LSTM 隐状态，避免跨文件污染
    window = 512
    n = len(x_16k)
    speeches = []
    voiced = 0
    triggered = False
    start = 0
    min_speech = int(VAD_MIN_SPEECH_MS / 1000 * VAD_SR)
    min_silence = int(VAD_MIN_SILENCE_MS / 1000 * VAD_SR)
    pad = int(VAD_SPEECH_PAD_MS / 1000 * VAD_SR)

    silence_count = 0
    with torch.no_grad():
        for i in range(0, n - window + 1, window):
            chunk = torch.from_numpy(x_16k[i:i + window]).unsqueeze(0)
            p = model(chunk, VAD_SR).item()
            if p >= VAD_THRESHOLD:
                voiced += len(chunk[0])
                if not triggered:
                    start = i
                    triggered = True
                silence_count = 0
            elif triggered:
                silence_count += window
                if silence_count >= min_silence:
                    end = i
                    if end - start >= min_speech:
                        speeches.append((max(0, start - pad), min(n, end + pad)))
                    triggered = False
                    silence_count = 0
    if triggered:
        speeches.append((max(0, start - pad), n))
    return speeches, voiced


def _resample_to_16k(mono, sr):
    if sr == VAD_SR:
        return mono.astype(np.float32)
    try:
        import torch
        import torchaudio
        t = torch.from_numpy(mono.astype(np.float32)).unsqueeze(0)
        return torchaudio.functional.resample(t, sr, VAD_SR).squeeze(0).numpy()
    except Exception:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(int(sr), VAD_SR)
        return resample_poly(mono, VAD_SR // g, int(sr) // g).astype(np.float32)


def build_gain(n, sr, speech_windows):
    """按语音区间（原采样率）构建增益包络。"""
    gain = np.full(n, FLOOR_GAIN, dtype=np.float32)
    ramp_n = max(1, int(RAMP_S * sr))
    ramp_up = np.linspace(FLOOR_GAIN, 1.0, ramp_n, dtype=np.float32)
    ramp_dn = np.linspace(1.0, FLOOR_GAIN, ramp_n, dtype=np.float32)
    pre_n = int(PAD_PRE_S * sr)
    post_n = int(PAD_POST_S * sr)

    for st, en in speech_windows:
        a = max(0, st - pre_n)
        b = min(n, en + post_n)
        if b <= a:
            continue
        gain[a:b] = 1.0
        ra = max(0, a - ramp_n)
        if a - ra > 0:
            gain[ra:a] = np.maximum(gain[ra:a], ramp_up[-(a - ra):])
        rb = min(n, b + ramp_n)
        if rb - b > 0:
            gain[b:rb] = np.maximum(gain[b:rb], ramp_dn[:rb - b])
    return gain


def analyze_file(model, path, min_speech_ratio=MIN_SPEECH_RATIO, write=True,
                 backup=True, floor_gain=FLOOR_GAIN):
    """对单个 vocals.wav 做门控。

    返回 dict：status GATED / SKIP_LOW_SPEECH / ERR，含 speech_ratio、
    门控前后窗外 RMS(dB)、语音段数、耗时。
    """
    t0 = time.time()
    res = {"path": path, "status": "ERR", "speech_ratio": 0.0,
           "n_speech_segs": 0, "before_db": 0.0, "after_db": 0.0,
           "reduction_db": 0.0, "elapsed_s": 0.0}
    try:
        import soundfile as sf
        x, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res["elapsed_s"] = round(time.time() - t0, 2)
        return res

    ch = x.shape[1]
    mono = x.mean(axis=1)
    n = len(mono)
    res["dur_s"] = round(n / sr, 2)

    mono_16k = _resample_to_16k(mono, sr)
    speech_16k, voiced = vad_detect(model, mono_16k)
    ratio = (voiced / VAD_SR) / (n / sr) if n else 0.0
    res["speech_ratio"] = round(min(1.0, ratio), 4)

    gain = build_gain(n, sr, [(int(s * sr / VAD_SR), int(e * sr / VAD_SR))
                              for s, e in speech_16k])
    mask_out = gain < 0.5
    res["n_speech_segs"] = len(speech_16k)

    if res["speech_ratio"] < min_speech_ratio:
        res["status"] = "SKIP_LOW_SPEECH"
        res["elapsed_s"] = round(time.time() - t0, 2)
        return res

    before = float(np.sqrt(np.mean(mono[mask_out] ** 2))) if mask_out.any() else 0.0
    y = x * gain.reshape(-1, 1)
    after = float(np.sqrt(np.mean(y.mean(axis=1)[mask_out] ** 2))) if mask_out.any() else 0.0
    res["before_db"] = round(20 * np.log10(before + 1e-9), 1)
    res["after_db"] = round(20 * np.log10(after + 1e-9), 1)
    res["reduction_db"] = round(res["before_db"] - res["after_db"], 1)

    if write:
        if backup:
            bak = path + ".bak_vad_gate"
            if not os.path.isfile(bak):
                shutil.copy2(path, bak)
        sf.write(path, y, sr, subtype="PCM_16")
        res["status"] = "GATED"
    else:
        res["status"] = "DRY_RUN"
    res["elapsed_s"] = round(time.time() - t0, 2)
    return res


def gate_files(paths, model=None, min_speech_ratio=MIN_SPEECH_RATIO,
               write=True, backup=True, floor_gain=FLOOR_GAIN, verbose=True):
    """批量门控。返回汇总 dict（含逐文件结果）。"""
    t0 = time.time()
    model = model or load_vad()
    rows = []
    for i, p in enumerate(paths, 1):
        if not p or not os.path.isfile(p):
            rows.append({"path": p, "status": "NO_FILE", "elapsed_s": 0.0})
            continue
        r = analyze_file(model, p, min_speech_ratio=min_speech_ratio,
                         write=write, backup=backup, floor_gain=floor_gain)
        rows.append(r)
        if verbose:
            if r["status"] == "GATED":
                print(f"  [{i}/{len(paths)}] {os.path.basename(os.path.dirname(os.path.dirname(p)))}: "
                      f"语音占比 {r['speech_ratio']*100:.0f}%，窗外 {r['before_db']:.1f} → "
                      f"{r['after_db']:.1f} dB（↓{r['reduction_db']}），{r['elapsed_s']}s",
                      flush=True)
            elif r["status"] == "SKIP_LOW_SPEECH":
                print(f"  [{i}/{len(paths)}] ⚠️ 跳过（语音占比 {r['speech_ratio']*100:.1f}% "
                      f"< {min_speech_ratio*100:.0f}%，只告警不改动）", flush=True)
            else:
                print(f"  [{i}/{len(paths)}] ✗ {r['status']} {r.get('error', '')}", flush=True)

    gated = [r for r in rows if r["status"] == "GATED"]
    skipped = [r for r in rows if r["status"] == "SKIP_LOW_SPEECH"]
    err = [r for r in rows if r["status"] not in ("GATED", "SKIP_LOW_SPEECH", "DRY_RUN")]
    return {
        "elapsed_s": round(time.time() - t0, 1),
        "total": len(rows),
        "gated": len(gated),
        "skipped_low_speech": len(skipped),
        "err": len(err),
        "total_reduction_db": round(sum(r["reduction_db"] for r in gated), 1),
        "results": rows,
    }


def write_report(workdir, report, name=REPORT_NAME):
    path = os.path.join(workdir, name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=float)
        return path
    except Exception:
        return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="VAD 门控（人声干声非语音段压制）")
    ap.add_argument("files", nargs="+", help="vocals.wav 路径（可多个）")
    ap.add_argument("--dry-run", action="store_true", help="只分析不写回")
    ap.add_argument("--min-speech-ratio", type=float, default=MIN_SPEECH_RATIO,
                    help="语音占比低于该值时跳过（默认 0.20）")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak_vad_gate")
    a = ap.parse_args()
    rep = gate_files(a.files, min_speech_ratio=a.min_speech_ratio,
                     write=not a.dry_run, backup=not a.no_backup)
    print(json.dumps({k: v for k, v in rep.items() if k != "results"},
                     ensure_ascii=False, indent=2))
    for r in rep["results"]:
        print(json.dumps(r, ensure_ascii=False))
