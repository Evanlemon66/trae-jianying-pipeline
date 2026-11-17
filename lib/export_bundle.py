"""export_bundle.py — 导出成品三件套校验（clean / 归档与导出口径共用）。

判定口径（与导出侧一致，缺一不可）：
  1. `<草稿名>.mp4` 存在且 > 0 B
  2. `<草稿名>.mp4.progress.json` 存在且 done == true
  3. `<草稿名>-封面.jpg` 存在（兼容 `_封面` / 任意含「封面」的图片名），且为竖版 9:16

设计约束：
  - 校验纯只读：不写、不删、不改任何文件；每次调用重新扫描磁盘，
    因此用户手工补一张 `-封面.jpg` 后重新执行即可自动通过，无需额外登记。
  - 唯一写入点是 log_forced_use：命令带 --force 显式放行时，把被放过的缺件
    追加到 06_publish/audit_forced.log（谁/何时/放过什么/哪条命令）。
  - 不依赖 Pillow：PNG 读 IHDR、JPEG 扫 SOF 段取宽高，避免运行环境缺库导致误拦。
"""

from __future__ import annotations

import getpass
import glob
import json
import os
import socket
import struct
import sys
import time

# 竖版封面基准：1080x1920（剪映导出草稿封面尺寸），允许 2% 比例误差
COVER_RATIO = 1920.0 / 1080.0
COVER_RATIO_TOL = 0.02
COVER_MIN_SHORT_SIDE = 720
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

FORCED_LOG = "06_publish/audit_forced.log"


def log_forced_use(job_dir, op, conflicts, *, dry_run=False):
    """把「--force 放过了哪些缺件」追加到 06_publish/audit_forced.log。

    op: clean | archive。conflicts: [(draft, profile, [missing...]), ...]。
    dry-run 只打印不落盘；留痕字段固定为 谁/何时/放过什么/哪条命令。
    """
    if not conflicts:
        return
    user = (f"{os.environ.get('USERNAME') or getpass.getuser()}@"
            f"{os.environ.get('COMPUTERNAME') or socket.gethostname()}")
    cmd = " ".join(sys.argv)
    path = os.path.join(job_dir, FORCED_LOG)
    lines = []
    for draft, profile, missing in conflicts:
        lines.append(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] forced  op={op}  user={user}  "
                     f"draft={draft}  profile={profile}  missing={' | '.join(missing)}  "
                     f"cmd={cmd}")
    for ln in lines:
        print(f"  [forced] {ln}")
    if dry_run:
        print(f"  [forced] dry-run 不写日志（{FORCED_LOG}）")
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"  [forced] 已留痕: {path}")
    except OSError as e:
        # 留痕失败不阻断放行，但必须显式告警，不能静默
        print(f"  [warn] 写 {FORCED_LOG} 失败（不影响本次 --force 放行）: {e}")


def _image_size(path):
    """读 PNG/JPEG 宽高；失败返回 None。不依赖第三方库。"""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            f.seek(0)
            data = f.read()
    except OSError:
        return None
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0-SOF15（排除 DHT/JPG/DAC）
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return int(w), int(h)
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def find_cover(bundle_dir, draft_name):
    """按导出侧同款候选名找封面；返回第一个命中的路径或 None。"""
    if not bundle_dir or not os.path.isdir(bundle_dir):
        return None
    stem = os.path.splitext(draft_name)[0]
    names = [
        f"{stem}-封面.jpg", f"{stem}_封面.jpg",
        f"{stem}-封面.png", f"{stem}_封面.png",
        f"{draft_name}-封面.jpg", f"{draft_name}_封面.jpg",
        "封面.jpg", "封面.png",
    ]
    for name in names:
        p = os.path.join(bundle_dir, name)
        if os.path.isfile(p) and os.path.getsize(p) > 0:
            return p
    hits = []
    for ext in IMAGE_EXTS:
        hits.extend(glob.glob(os.path.join(bundle_dir, f"*封面*{ext}")))
        hits.extend(glob.glob(os.path.join(bundle_dir, f"*封面*{ext.upper()}")))
    hits = sorted({os.path.normpath(p) for p in hits
                   if os.path.isfile(p) and os.path.getsize(p) > 0})
    return hits[0] if hits else None


def _resolve_mp4(export_file, export_dir, draft_name):
    if export_file and os.path.isfile(export_file):
        return export_file
    if export_dir and os.path.isdir(export_dir):
        direct = os.path.join(export_dir, f"{draft_name}.mp4")
        if os.path.isfile(direct):
            return direct
        mp4s = sorted(glob.glob(os.path.join(export_dir, "*.mp4")))
        if len(mp4s) == 1:
            return mp4s[0]
    return export_file or None


def audit_bundle(draft_name, export_file=None, export_dir=None):
    """校验单版本成品三件套（严格判定；缺件即失败）。

    缺件不再有任何"登记后自动放行"的旁路：clean / archive 的人工放行统一走
    命令的 --force，并由 export_bundle.log_forced_use 写 06_publish/audit_forced.log。

    返回 {"ok": bool, "mp4"/"progress"/"cover": 路径|None, "missing": [str, ...]}
    missing 里每项都是可直接打印的「缺什么 + 期望路径」。
    """
    draft_name = str(draft_name or "").strip()
    export_file = os.path.normpath(export_file) if export_file else None
    bundle_dir = export_dir or (os.path.dirname(export_file) if export_file else None)
    if bundle_dir:
        bundle_dir = os.path.normpath(bundle_dir)

    missing = []
    mp4 = _resolve_mp4(export_file, bundle_dir, draft_name)
    if not mp4 or not os.path.isfile(mp4):
        missing.append(f"mp4 缺失（期望 {mp4 or os.path.join(bundle_dir or '?', draft_name + '.mp4')}）")
    elif os.path.getsize(mp4) <= 0:
        missing.append(f"mp4 为空文件: {mp4}")

    progress = None
    if mp4:
        cand = [mp4 + ".progress.json"]
        if bundle_dir:
            cand.append(os.path.join(bundle_dir, f"{draft_name}.progress.json"))
            cand.append(os.path.join(bundle_dir, f"{draft_name}.mp4.progress.json"))
        for p in cand:
            if os.path.isfile(p):
                progress = p
                break
    if not progress:
        missing.append(f"progress.json 缺失（期望 {os.path.join(bundle_dir or '?', draft_name + '.mp4.progress.json')}）")
    else:
        try:
            with open(progress, encoding="utf-8-sig") as f:
                pj = json.load(f)
        except (OSError, ValueError) as e:
            missing.append(f"progress.json 不可解析: {os.path.basename(progress)}（{e}）")
            pj = {}
        if pj and pj.get("done") is not True:
            missing.append(f"progress.json done != true: {os.path.basename(progress)}")

    cover = find_cover(bundle_dir, draft_name) if bundle_dir else None
    if not cover:
        missing.append(f"封面缺失（期望 {os.path.join(bundle_dir or '?', draft_name + '-封面.jpg')}）")
    else:
        size = _image_size(cover)
        if not size:
            missing.append(f"封面无法读取尺寸: {os.path.basename(cover)}")
        else:
            w, h = size
            ratio = (h / float(w)) if w else 0
            if h <= w:
                missing.append(f"封面不是竖版: {os.path.basename(cover)} {w}x{h}")
            elif abs(ratio - COVER_RATIO) / COVER_RATIO > COVER_RATIO_TOL:
                missing.append(
                    f"封面比例异常: {os.path.basename(cover)} {w}x{h}"
                    f"（期望约 9:16，容差 {int(COVER_RATIO_TOL * 100)}%）")
            elif min(w, h) < COVER_MIN_SHORT_SIDE:
                missing.append(
                    f"封面尺寸过小: {os.path.basename(cover)} {w}x{h}"
                    f"（短边需 ≥ {COVER_MIN_SHORT_SIDE}）")

    return {"ok": not missing, "mp4": mp4, "progress": progress,
            "cover": cover, "missing": missing}
