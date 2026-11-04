# -*- coding: utf-8 -*-
"""回归用例：_parse_mp4_duration 遇到非法/截断 box 时必须快速返回，不得自旋。

背景（2026-11-04 实测）：被 kill 的 ffmpeg 在 __jycache__ 留下半截
`第1集.__jy_norm__.mp4`，其根级 mdat 的 size 字段为 0。旧实现用
`f.seek(box_size - header_len, 1)` 相对移动，box_size=0 时游标回退 8 字节，
每次迭代读到同一个 box → 死循环、单核 100% CPU 长达 6 小时。

覆盖：
  ① 根级 mdat size=0（半截文件）→ 返回 None 且 <10s
  ② 非法小 size（box_size < header_len）→ 返回 None 且 <10s
  ③ 正常 moov/mvhd → 正确解析出 2.0s
  ④ moov 内子 box size=0 → 不进入死循环，返回 None
  ⑤ media_normalizer._is_cache_fresh 不认 0 字节残片

运行：python tests\\test_mp4_duration_guard.py
"""
import os
import struct
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "utils"))

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _ftyp():
    return b"\x00\x00\x00\x1c" + b"ftyp" + b"isom" + b"\x00\x00\x02\x00" + b"isomiso2mp41"


def _mvhd(timescale=1000, duration=2000):
    payload = b"\x00\x00\x00\x00" + b"\x00" * 4 + b"\x00" * 4 \
        + struct.pack(">I", timescale) + struct.pack(">I", duration)
    body = struct.pack(">I", 8 + len(payload)) + b"mvhd" + payload
    return body


def _moov(children):
    body = b"".join(children)
    return struct.pack(">I", 8 + len(body)) + b"moov" + body


def _box(typ, size, payload=b""):
    return struct.pack(">I", size) + typ.encode() + payload


TMP = os.path.join(tempfile.gettempdir(), "mp4_duration_guard")
os.makedirs(TMP, exist_ok=True)


def _write(name, data):
    p = os.path.join(TMP, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def parse_in_subprocess(path, timeout=10):
    """在子进程里解析：旧实现会死循环，用超时保护测试自身。"""
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{os.path.join(_ROOT, 'pipeline')}');"
        "import pipeline;"
        f"print(pipeline._parse_mp4_duration(r'{path}'))"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return "TIMEOUT", ""
    lines = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()]
    return (lines[-1].strip() if lines else ""), (r.stderr or "")


print("① 根级 mdat size=0（半截 __jy_norm__.mp4 复刻）")
p_trunc = _write("trunc_mdat_zero.mp4", _ftyp() + _box("mdat", 0))
out, err = parse_in_subprocess(p_trunc)
check("返回 None（未解析出 duration）", out == "None", out)
check("10 秒内返回（无自旋）", out != "TIMEOUT", out)

print("② 非法小 size")
p_small = _write("invalid_small.mp4", _ftyp() + _box("free", 6))
out2, _ = parse_in_subprocess(p_small)
check("返回 None", out2 == "None", out2)
check("10 秒内返回", out2 != "TIMEOUT", out2)

print("③ 正常 moov/mvhd")
p_ok = _write("valid.mp4", _ftyp() + _moov([_mvhd()]))
out3, err3 = parse_in_subprocess(p_ok)
check("解析出 2000000us", out3 == "2000000", f"{out3} {err3}")

print("④ moov 内子 box size=0")
p_inner = _write("moov_zero_child.mp4", _ftyp() + _moov([_box("free", 0)] + [_mvhd()]))
out4, _ = parse_in_subprocess(p_inner)
check("不死循环（返回 None）", out4 == "None", out4)

print("⑤ _is_cache_fresh 不认 0 字节残片")
import media_normalizer as mn  # noqa: E402
src = _write("src.mp4", _ftyp())
empty = _write("cached_empty.__jy_norm__.mp4", b"")
check("0 字节缓存判为不新鲜", mn._is_cache_fresh(src, empty) is False)

print("⑥ 转封装临时路径必须以 .mp4 结尾")
tmp = mn._tmp_output_path(os.path.join(TMP, "x.__jy_norm__.mp4"))
check("临时路径以 .mp4 结尾（否则 ffmpeg 无法推断格式）", tmp.endswith(".mp4"), tmp)
check("临时路径与正式路径不同", tmp != os.path.join(TMP, "x.__jy_norm__.mp4"), tmp)

print()
if fails:
    print(f"❌ {len(fails)} 项失败: {fails}")
    sys.exit(1)
print("✅ 全部通过")
