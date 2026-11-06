# -*- coding: utf-8 -*-
"""回归用例：子进程 stderr 含非 UTF-8 字节时不得抛 UnicodeDecodeError。

背景（2026-11-07）：一轮 pipeline.log 出现 702 次
`subprocess.py:1599 _readerthread → fh.read() → UnicodeDecodeError: 'utf-8' codec
can't decode byte 0xbc ...`。根因是 run_demucs / run_ffmpeg_extract_audio 用
encoding="utf-8" 严格解码，而 ffmpeg/demucs 的 stderr 混有 GBK 字节。该异常在线程
里只打印不传播，但会清空 stdout/stderr 缓冲，把 last_error 的日志尾部覆盖成噪音。

覆盖：
  ① run_ffmpeg_extract_audio 遇非 UTF-8 stderr → 不抛异常、返回 False；
  ② run_demucs 两个模型均遇非 UTF-8 stderr → 不抛异常、返回 (None, None)；
  ③ errors="replace" 保住了 stderr 内容（非法字节替换为 U+FFFD，正常文本仍在）。

运行：python tests\\test_subprocess_decode.py（不真跑 ffmpeg/demucs）
"""
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import pipeline as pl  # noqa: E402
import subprocess  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "subprocess_decode_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

# 输出 GBK 字节 0xbc 0xa8（作为 UTF-8 非法），再退出 1
EMIT_GBK = (
    "import sys\n"
    "sys.stderr.buffer.write(b'\\xbc\\xa8 bad gbk byte\\n')\n"
    "sys.stderr.buffer.flush()\n"
    "sys.exit(1)\n"
)

_real_run = subprocess.run


def fake_run(cmd, **kw):
    """忽略真实命令，改跑一个输出非 UTF-8 stderr 的 Python 子进程，转发解码参数。"""
    return _real_run([sys.executable, "-c", EMIT_GBK], **kw)


print("\n== ③ 解码机制：errors=replace 保住 stderr 内容 ==")
r = _real_run([sys.executable, "-c", EMIT_GBK], capture_output=True, text=True,
              encoding="utf-8", errors="replace")
check("非法字节被替换而非抛异常", "\ufffd" in r.stderr, repr(r.stderr))
check("正常文本仍保留", "bad gbk byte" in r.stderr, repr(r.stderr))

print("\n== ① run_ffmpeg_extract_audio 遇非 UTF-8 stderr 不抛 ==")
pl._find_ffmpeg = lambda: "ffmpeg"  # 保证能走到 subprocess.run
pl.subprocess.run = fake_run
try:
    raised = None
    try:
        ok1 = pl.run_ffmpeg_extract_audio("dummy.mp4", os.path.join(TMP, "a.wav"))
    except Exception as e:  # noqa: BLE001 - 这里就是要证明它不抛
        raised = e
        ok1 = None
    check("不抛 UnicodeDecodeError", raised is None, raised)
    check("rc=1 → 返回 False", ok1 is False, ok1)
finally:
    pl.subprocess.run = _real_run

print("\n== ② run_demucs 遇非 UTF-8 stderr 不抛 ==")
pl.subprocess.run = fake_run
try:
    raised = None
    try:
        vocal, no_vocal = pl.run_demucs("dummy.mp4", TMP, model="m1", fallback="m2")
    except Exception as e:  # noqa: BLE001
        raised = e
        vocal = no_vocal = "raised"
    check("不抛 UnicodeDecodeError", raised is None, raised)
    check("两个模型都失败 → (None, None)", vocal is None and no_vocal is None,
          (vocal, no_vocal))
finally:
    pl.subprocess.run = _real_run

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
