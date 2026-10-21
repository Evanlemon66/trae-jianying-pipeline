import sys, time, os, re, glob, json
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
from moviepy import VideoFileClip

VIDEO_DIR = r"D:\短剧\10-六零糙汉：我靠打猎养全家"


def ep_num(f):
    m = re.search(r"第\s*(\d+)\s*集", f)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)", f)
    return int(m.group(1)) if m else 999999


def find_audio_peak(video_path, scan_dur=60, chunk_dur=0.25):
    try:
        clip = VideoFileClip(video_path)
        dur = clip.duration
        audio = clip.audio
        if audio is None:
            clip.close()
            return None, None

        sr = audio.fps
        audio_samples = audio.subclipped(0, min(scan_dur, dur)).to_soundarray()
        if audio_samples.ndim == 2:
            audio_samples = audio_samples.mean(axis=1)

        chunk_size = int(sr * chunk_dur)
        energies = []
        for i in range(0, len(audio_samples) - chunk_size, chunk_size):
            chunk = audio_samples[i:i + chunk_size]
            rms = np.sqrt(np.mean(chunk**2))
            energies.append((i / sr, float(rms)))

        clip.close()

        if not energies:
            return None, None

        vals = [e[1] for e in energies]
        med = np.median(vals)
        p90 = np.percentile(vals, 90)
        p95 = np.percentile(vals, 95)

        threshold = p90 * 0.5
        peaks = [(t, e) for t, e in energies if e > threshold]

        if not peaks:
            return None, None

        last_t = -2
        filtered_peaks = []
        for t, e in peaks:
            if t - last_t > 1.0:
                filtered_peaks.append((t, e))
                last_t = t

        if not filtered_peaks:
            return None, None

        target_time = filtered_peaks[0][0]
        stats = {
            "median": round(med, 4),
            "p90": round(p90, 4),
            "p95": round(p95, 4),
            "threshold": round(threshold, 4),
            "peak_count": len(filtered_peaks),
            "first_peak_energy": round(filtered_peaks[0][1], 4),
        }
        return round(target_time, 2), stats

    except Exception as e:
        print(f"    ERR: {e}")
        return None, None


def main():
    exts = ["*.mp4", "*.mov", "*.mkv", "*.avi"]
    video_files = []
    for ext in exts:
        video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext)))
        video_files.extend(glob.glob(os.path.join(VIDEO_DIR, ext.upper())))

    video_files = sorted(set(video_files), key=lambda p: ep_num(os.path.basename(p)))
    print(f"共 {len(video_files)} 集，开始音频能量分析（前 60s）...\n")

    results = []
    t0 = time.time()

    for i, vf in enumerate(video_files):
        en = ep_num(os.path.basename(vf))
        t1 = time.time()
        target_time, stats = find_audio_peak(vf)
        elapsed = time.time() - t1

        if target_time is not None:
            cut_from = max(0, target_time - 0.5)
            reason = f"audio_peak@{target_time}s (E={stats['first_peak_energy']}, med={stats['median']})"
            print(f"[{i+1}/{len(video_files)}] 第{en:>2}集 | 冲突点={target_time}s | 剪={cut_from}s | {reason} | {elapsed:.1f}s")
        else:
            target_time = 2.0
            cut_from = 0
            reason = "no_peak_found_default"
            print(f"[{i+1}/{len(video_files)}] 第{en:>2}集 | 未检测到峰值，默认从 2s 开始 | {elapsed:.1f}s")

        results.append({
            "episode": en,
            "file": os.path.basename(vf),
            "conflict_time": target_time,
            "cut_from": round(cut_from, 2),
            "reason": reason,
            "stats": stats,
            "elapsed": round(elapsed, 1),
        })

    total = time.time() - t0
    print(f"\n总耗时: {total:.0f}s ({total/max(1,len(video_files)):.1f}s/集)")

    out = "audio_peak_results.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"结果已保存: {out}")

    print(f"\n=== 汇总 ===")
    cuts = [r["cut_from"] for r in results]
    print(f"  最短: {min(cuts):.1f}s | 最长: {max(cuts):.1f}s | 平均: {np.mean(cuts):.1f}s | 中位数: {np.median(cuts):.1f}s")


if __name__ == "__main__":
    main()
