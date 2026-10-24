"""
剪映本地 ONNX ASR 模型推理
========================
绕过剪映 UI，直接调用剪映自带的 ONNX CTC 模型做中文语音识别。

模型位置: %LOCALAPPDATA%/JianyingPro/User Data/SupplysStore/local-asr-supplies/*/
模型架构: Encoder(367MB) → CTC Probs → Greedy Decode → Text
输入: 80 维 Mel fbank (16kHz, 25ms/10ms, Kaldi 标准参数)
输出: 带时间戳的中文 transcript
"""

import os, sys, json, re, subprocess, time
import numpy as np
from scipy import signal
import onnxruntime as ort


JY_ASR_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "JianyingPro", "User Data", "SupplysStore", "local-asr-supplies",
)

ENC_MODEL = "asr-model-encoder.onnx"
CLS_MODEL = "asr-model-classifier.onnx"
TOKENS_FILE = "asr-model-token.txt"
ITN_FST = "asr-itn-fst.fst"


def find_model_dir():
    if not os.path.isdir(JY_ASR_DIR):
        return None
    for sub in os.listdir(JY_ASR_DIR):
        full = os.path.join(JY_ASR_DIR, sub)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, ENC_MODEL)):
            return full
    return None


def load_tokens(path):
    id2tok = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().rsplit(" ", 1)
            if len(parts) == 2:
                tok, tid = parts[0], int(parts[1])
                id2tok[tid] = tok
    return id2tok


def _hz_to_mel(hz):
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel):
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _make_mel_filterbank(num_mel_bins, fft_bins, sample_rate, low_freq, high_freq):
    """Kaldi-style mel filter bank"""
    assert high_freq <= sample_rate // 2
    mel_high = _hz_to_mel(high_freq)
    mel_low = _hz_to_mel(low_freq)
    mel_points = np.linspace(mel_low, mel_high, num_mel_bins + 2)
    bin_points = _mel_to_hz(mel_points) * (fft_bins + 1) / sample_rate

    fbank = np.zeros((num_mel_bins, fft_bins), dtype=np.float32)
    for m in range(1, num_mel_bins + 1):
        f_left = bin_points[m - 1]
        f_center = bin_points[m]
        f_right = bin_points[m + 1]

        for k in range(int(f_left), int(f_center)):
            if k >= 0 and k < fft_bins:
                fbank[m - 1, k] = (k - f_left) / max(f_center - f_left, 1e-6)
        for k in range(int(f_center), int(f_right)):
            if k >= 0 and k < fft_bins:
                fbank[m - 1, k] = (f_right - k) / max(f_right - f_center, 1e-6)
    return fbank


def extract_fbank(wav_mono, sample_rate=16000, num_mel_bins=80,
                  frame_length_ms=25, frame_shift_ms=10,
                  low_freq=0, high_freq=8000):
    """
    Kaldi-style fbank feature extraction.
    Returns: (num_frames, num_mel_bins) float32
    """
    frame_length = int(sample_rate * frame_length_ms / 1000.0)
    frame_shift = int(sample_rate * frame_shift_ms / 1000.0)
    fft_size = 1 << (frame_length - 1).bit_length()

    window = np.hanning(frame_length).astype(np.float32)

    num_samples = len(wav_mono)
    num_frames = (num_samples - frame_length) // frame_shift + 1

    frames = np.zeros((num_frames, frame_length), dtype=np.float32)
    for i in range(num_frames):
        start = i * frame_shift
        frames[i] = wav_mono[start:start + frame_length]
    frames *= window

    spec = np.fft.rfft(frames, n=fft_size, axis=1)
    power_spec = np.abs(spec) ** 2
    power_spec = power_spec[:, :fft_size // 2 + 1]

    fbank = _make_mel_filterbank(num_mel_bins, fft_size // 2 + 1,
                                 sample_rate, low_freq, high_freq)
    mel_energy = power_spec @ fbank.T
    mel_energy = np.maximum(mel_energy, 1e-10)
    log_mel = np.log(mel_energy)

    mean = log_mel.mean(axis=0)
    std = log_mel.std(axis=0) + 1e-10
    log_mel = (log_mel - mean) / std

    return log_mel.astype(np.float32)


def load_wav(path, target_sr=16000):
    import soundfile as sf
    data, sr = sf.read(path)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != target_sr:
        num_out = int(len(data) * target_sr / sr)
        data = signal.resample(data, num_out)
    data = data.astype(np.float32)
    if np.max(np.abs(data)) > 1.0:
        data = data / np.max(np.abs(data))
    return data, target_sr


def ctc_greedy_decode(ctc_probs, blank_id=0, pad_id=2, sos_id=3, eos_id=4, unk_id=1):
    """
    CTC greedy decode.
    ctc_probs: (T, vocab_size) numpy array
    Returns: list of (token_id, time_idx)
    """
    T = len(ctc_probs)
    best = np.argmax(ctc_probs, axis=1)

    result = []
    prev = -1
    for t in range(T):
        tok = best[t]
        if tok == blank_id:
            prev = blank_id
            continue
        if tok == prev:
            continue
        result.append((int(tok), int(t)))
        prev = tok

    result = [(tid, t) for tid, t in result if tid not in (pad_id, sos_id, eos_id, unk_id)]
    return result


def ids_to_text(tok_ids, id2tok):
    """Convert token IDs to text, handle SentencePiece underscores."""
    parts = []
    for tid in tok_ids:
        tok = id2tok.get(tid, f"<{tid}>")
        parts.append(tok)
    raw = "".join(parts)
    text = raw.replace("\u2581", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def wav_to_transcript(wav_path, model_dir=None):
    """Run ONNX ASR on a wav file. Returns (text, segments)."""
    if model_dir is None:
        model_dir = find_model_dir()
    if model_dir is None:
        raise RuntimeError("剪映本地 ASR 模型目录未找到")

    enc_path = os.path.join(model_dir, ENC_MODEL)
    tok_path = os.path.join(model_dir, TOKENS_FILE)

    print(f"  📦 模型: {model_dir}")
    print(f"  🔊 加载音频: {wav_path}")
    audio, sr = load_wav(wav_path, target_sr=16000)
    dur_sec = len(audio) / sr
    print(f"  📏 时长: {dur_sec:.1f}s, 采样率: {sr}Hz")

    print(f"  🎛️  提取 fbank (80 维, Kaldi 标准)...")
    t0 = time.time()
    fbank = extract_fbank(audio, sample_rate=sr, num_mel_bins=80)
    feat_time = time.time() - t0
    print(f"     fbank 耗时 {feat_time:.2f}s → {fbank.shape}")

    print(f"  🧠 加载 ONNX encoder...")
    sess = ort.InferenceSession(enc_path, providers=["CPUExecutionProvider"])

    speech = fbank[np.newaxis, ...]
    lengths = np.array([fbank.shape[0]], dtype=np.int32)

    print(f"  🚀 推理中...")
    t1 = time.time()
    ctc_probs, out_lens = sess.run(None, {
        "speech": speech,
        "speech_lengths": lengths,
    })
    infer_time = time.time() - t1
    print(f"     推理耗时 {infer_time:.2f}s → ctc_probs {ctc_probs.shape}")

    print(f"  📝 CTC greedy decode...")
    decoded = ctc_greedy_decode(ctc_probs[0])

    id2tok = load_tokens(tok_path)
    text = ids_to_text([tid for tid, _ in decoded], id2tok)

    frame_shift_ms = 10.0
    segments = []
    for tid, t_idx in decoded:
        seg_start = t_idx * frame_shift_ms / 1000.0
        if segments:
            segments[-1]["end"] = seg_start
        segments.append({
            "token_id": tid,
            "text": id2tok.get(tid, f"?{tid}"),
            "start": seg_start,
            "end": seg_start,
        })
    if segments:
        segments[-1]["end"] = dur_sec

    print(f"\n{'='*60}")
    print(f"⏱️  总耗时: {feat_time + infer_time:.2f}s (fbank {feat_time:.2f}s + 推理 {infer_time:.2f}s)")
    print(f"🎙️  音频时长: {dur_sec:.1f}s, 实时率: {(feat_time + infer_time)/dur_sec:.2f}x")
    print(f"📜 Transcript:")
    print(f"   {text}")
    print(f"{'='*60}")

    return text, segments


def mp4_to_wav(mp4_path, wav_path, tta_start=None, tta_end=None):
    """用 ffmpeg 从 mp4 提取 16kHz 单声道 wav"""
    cmd = ["ffmpeg", "-y", "-i", mp4_path,
           "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", wav_path]
    if tta_start is not None:
        cmd = ["ffmpeg", "-y", "-ss", str(tta_start), "-i", mp4_path,
               "-t", str(tta_end - tta_start),
               "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", wav_path]
    subprocess.run(cmd, capture_output=True, check=True)
    return wav_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python jy_onnx_asr.py <wav_or_mp4> [start_sec end_sec]")
        print("模型目录会自动从剪映安装位置查找")
        sys.exit(1)

    input_path = sys.argv[1]
    start_sec = float(sys.argv[2]) if len(sys.argv) > 2 else None
    end_sec = float(sys.argv[3]) if len(sys.argv) > 3 else None

    tmp_wav = os.path.join(os.path.dirname(input_path) or ".", "_jy_asr_tmp.wav")

    ext = os.path.splitext(input_path)[1].lower()
    if ext in (".mp4", ".mkv", ".mov", ".avi", ".webm"):
        print(f"📹 视频 → 提取音频...")
        if start_sec is not None and end_sec is not None:
            mp4_to_wav(input_path, tmp_wav, start_sec, end_sec)
        else:
            mp4_to_wav(input_path, tmp_wav)
        asr_input = tmp_wav
    elif ext in (".wav", ".mp3", ".flac", ".m4a"):
        if ext != ".wav":
            mp4_to_wav(input_path, tmp_wav)
            asr_input = tmp_wav
        else:
            asr_input = input_path
    else:
        asr_input = input_path

    text, segments = wav_to_transcript(asr_input)

    if tmp_wav != input_path and os.path.exists(tmp_wav):
        try:
            os.remove(tmp_wav)
        except OSError:
            pass
