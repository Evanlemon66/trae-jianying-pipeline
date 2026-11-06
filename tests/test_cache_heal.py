# -*- coding: utf-8 -*-
"""回归用例：坏缓存自愈 + faster-whisper transcribe 降级。

背景（2026-11-07）：22:08–22:13 双 batch_runner 并发写了 6 个损坏的
`__jy_norm__.mp4`（第04/08/11/14/19 集等），其中第04集音频抽取失败。
旧逻辑「缓存存在就复用」→ 每轮都在第 4 集 ASR 失败，并且 whisper 兜底在
`model.transcribe()` 抛 `cublas64_12.dll is not found` 时直接打挂 batch_runner。

覆盖：
  ① 音频抽取失败 → 删除该集缓存 → 用源文件重转一次，路径不变；
  ② 非 __jycache__ 路径 / 缺源文件 → 不做任何删除或重转；
  ③ 只重试一次：重转后仍失败就原样返回，交给既有失败计数；
  ④ run_whisper 在 transcribe 抛 cublas 异常时切 cpu/int8 重试且不中断；
  ⑤ 所有后端都失败才抛异常（不会死循环）。

运行：python tests\\test_cache_heal.py（临时目录，不真转码）
"""
import os
import shutil
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import pipeline as pl  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "cache_heal_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

print("\n== ① 坏缓存自愈：删缓存 + 源文件重转一次 ==")
d1 = os.path.join(TMP, "剧A")
cache_dir = os.path.join(d1, "__jycache__")
os.makedirs(cache_dir)
src1 = os.path.join(d1, "第04集.mp4")
with open(src1, "wb") as f:
    f.write(b"src")
cache1 = os.path.join(cache_dir, "第04集.__jy_norm__.mp4")
with open(cache1, "wb") as f:
    f.write(b"bad")

norm_calls = []


def fake_norm(path, force=False):
    norm_calls.append((path, force))
    check("重建时坏缓存已删除", not os.path.exists(cache1))
    with open(cache1, "wb") as f:
        f.write(b"good")
    return cache1


_real_norm = pl.normalize_video_for_jianying
pl.normalize_video_for_jianying = fake_norm
try:
    got = pl._heal_norm_cache(cache1)
finally:
    pl.normalize_video_for_jianying = _real_norm

check("返回重建后的同路径缓存", got == cache1, got)
check("normalize 只调用一次", len(norm_calls) == 1, len(norm_calls))
check("以 force=True 重转", norm_calls and norm_calls[0][1] is True, norm_calls)
check("源文件路径正确", norm_calls and norm_calls[0][0] == src1, norm_calls)

print("\n== ② 非缓存路径 / 缺源文件 → 不动手 ==")
norm_calls.clear()
pl.normalize_video_for_jianying = fake_norm
try:
    other = os.path.join(d1, "第05集.mp4")
    with open(other, "wb") as f:
        f.write(b"ok")
    check("普通视频路径 → None", pl._heal_norm_cache(other) is None, "返回非 None")
    orphan = os.path.join(cache_dir, "第99集.__jy_norm__.mp4")
    with open(orphan, "wb") as f:
        f.write(b"bad")
    check("缺源文件 → None", pl._heal_norm_cache(orphan) is None, "返回非 None")
    check("缺源文件时不重转", len(norm_calls) == 0, norm_calls)
    check("缺源文件时不删缓存", os.path.exists(orphan), "缓存被误删")
finally:
    pl.normalize_video_for_jianying = _real_norm

print("\n== ③ _extract_audio_with_heal 只重试一次 ==")
extract_calls = []
heal_calls = []
_real_extract = pl.run_ffmpeg_extract_audio
_real_heal = pl._heal_norm_cache
try:
    # 3a: 第一次失败 → 重建 → 第二次成功
    seq = [False, True]


    def fake_extract(video_path, wav_path):
        extract_calls.append(video_path)
        return seq.pop(0) if seq else False


    def fake_heal(path):
        heal_calls.append(path)
        return path


    pl.run_ffmpeg_extract_audio = fake_extract
    pl._heal_norm_cache = fake_heal
    asr_src, ok = pl._extract_audio_with_heal(cache1, os.path.join(TMP, "a.wav"))
    check("重建后抽取成功", ok is True, ok)
    check("返回 wav 作为 ASR 源", asr_src.endswith("a.wav"), asr_src)
    check("抽取共 2 次", len(extract_calls) == 2, len(extract_calls))
    check("自愈只 1 次", len(heal_calls) == 1, len(heal_calls))

    # 3b: 第一次失败 → 无法重建 → 直接返回失败（不重试）
    extract_calls.clear()
    heal_calls.clear()
    seq = [False]
    pl._heal_norm_cache = lambda p: (heal_calls.append(p), None)[1]
    asr_src, ok = pl._extract_audio_with_heal(cache1, os.path.join(TMP, "b.wav"))
    check("无法重建 → 失败", ok is False, ok)
    check("抽取只 1 次", len(extract_calls) == 1, len(extract_calls))
    check("回退原视频源", asr_src == cache1, asr_src)

    # 3c: 重建成功但再抽仍失败 → 只试一次，不进入第二轮自愈
    extract_calls.clear()
    heal_calls.clear()
    seq = [False, False, True]
    pl._heal_norm_cache = fake_heal
    asr_src, ok = pl._extract_audio_with_heal(cache1, os.path.join(TMP, "c.wav"))
    check("重转后仍失败 → 失败", ok is False, ok)
    check("抽取共 2 次（无第三轮）", len(extract_calls) == 2, len(extract_calls))
    check("自愈只 1 次", len(heal_calls) == 1, len(heal_calls))
    check("ASR 源为重建后的缓存", asr_src == cache1, asr_src)
finally:
    pl.run_ffmpeg_extract_audio = _real_extract
    pl._heal_norm_cache = _real_heal

print("\n== ④⑤ whisper transcribe 降级 ==")
tries = []


class FakeWhisperModel:
    def __init__(self, model_size, device="cpu", compute_type="int8"):
        self.device = device
        tries.append(device)

    def transcribe(self, path, **kw):
        if self.device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return iter([types.SimpleNamespace(start=0.4, end=1.2, text=" 你好 ")]), {"language": "zh"}


fake_mod = types.SimpleNamespace(WhisperModel=FakeWhisperModel)
sys.modules["faster_whisper"] = fake_mod
try:
    subs = pl.run_whisper("dummy.wav", model_size="tiny", device="cuda", compute_type="float16")
    check("cublas 异常后切 CPU 成功", len(subs) == 1 and subs[0]["text"] == "你好", subs)
    check("先 cuda 后 cpu", tries == ["cuda", "cpu"], tries)

    # ⑤ 全后端失败才抛异常，且不会无限循环
    class AlwaysFailModel:
        def __init__(self, model_size, device="cpu", compute_type="int8"):
            self.device = device
            all_tries.append(device)

        def transcribe(self, path, **kw):
            raise RuntimeError("boom")

    all_tries = []
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=AlwaysFailModel)
    raised = False
    try:
        pl.run_whisper("dummy.wav", model_size="tiny", device="cuda", compute_type="float16")
    except RuntimeError:
        raised = True
    check("全部失败 → 抛异常", raised, raised)
    check("最多 3 个候选后端", len(all_tries) == 3, all_tries)
finally:
    sys.modules.pop("faster_whisper", None)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
