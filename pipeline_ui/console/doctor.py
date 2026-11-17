"""Console 统一检查的适配层。

共享检查（仓库布局、Python/依赖、ffmpeg、剪映安装与桌面会话、配置路径、
草稿索引、watcher 计划任务与运行态）由 pipeline/doctor_core.py 提供，与
`python pipeline/doctor.py` 完全同源；本文件只做三件事：
  1. 把核心的 pass 映射成 Console 的 ok；
  2. 补 Console 专属的运行态检查：目录可写探针、磁盘阈值、串流清单、监听地址；
  3. 输出 /api/env/check 需要的 JSON（checked_at / elapsed_s / summary / checks / context）。

结果由 /api/env/check 聚合，不再提供独立页面。可写探针会在目标目录写临时文件后
立即删除，不改业务文件。
"""
from __future__ import annotations

import os
import subprocess
import time

import paths
import status

import doctor_core

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _item(key, name, st, detail, fix=""):
    return {"key": key, "name": name, "status": st, "detail": detail,
            "fix": fix, "blocking": True}


def _from_core(r):
    """核心用 pass/warn/fail，Console 页面沿用 ok/warn/fail。"""
    return {"key": r.key, "name": r.title,
            "status": "ok" if r.status == "pass" else r.status,
            "detail": r.detail, "fix": r.fix, "blocking": r.blocking}


def _write_probe(d):
    if not d:
        return False, "目录未配置"
    if not os.path.isdir(d):
        return False, "目录不存在"
    probe = os.path.join(d, f".console_probe_{os.getpid()}_{int(time.time() * 1000)}")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("probe")
        os.remove(probe)
        return True, "可写"
    except OSError as e:
        try:
            os.remove(probe)
        except OSError:
            pass
        return False, f"不可写: {e}"


# ── Console 专属单项检查 ────────────────────────────────────────────
def _writable_dir(key, name, path, *, warn_missing=False):
    if not path:
        return _item(key, name, "warn" if warn_missing else "fail", "未配置")
    if not os.path.isdir(path):
        st = "warn" if warn_missing else "fail"
        return _item(key, name, st, f"不存在: {path}",
                     "归档根会在首次归档时自动创建" if warn_missing else "检查 presets.json")
    ok, why = _write_probe(path)
    if ok:
        extra = ""
        if key == "series":
            try:
                n = sum(1 for x in os.listdir(path)
                        if os.path.isdir(os.path.join(path, x)))
                extra = f"；一级目录 {n} 个"
            except OSError:
                pass
        # 容量统一由「磁盘剩余空间」一项按磁盘卷报告，避免把卷容量误读成目录自身
        return _item(key, name, "ok", path + extra)
    return _item(key, name, "fail", f"{path} {why}")


_DISK_TARGETS = (("series", "素材根"), ("export", "导出根"),
                 ("archive", "归档根"), ("projects", "草稿根"))


def _disk():
    """按磁盘卷汇总四类根目录的剩余空间（GiB）。

    只按目录路径去读 shutil.disk_usage 会拿到整卷数字，因此必须标明是
    「所在卷」而不是该目录自身占用；同一卷上的多个根目录合并成一行。
    """
    roots = paths.known_roots()
    vols = {}
    for key, label in _DISK_TARGETS:
        p = roots.get(key) or ""
        if not p or not os.path.isdir(p):
            continue
        vol = (os.path.splitdrive(os.path.abspath(p))[0] or "?").upper()
        ent = vols.setdefault(vol, {"labels": [], "path": p})
        ent["labels"].append(label)
    if not vols:
        return _item("disk", "磁盘剩余空间", "warn", "没有任何可用根目录，无法读取容量")

    parts, free_min = [], None
    for vol in sorted(vols):
        ent = vols[vol]
        u = status.disk_usage(ent["path"])
        if not u:
            parts.append(f"{vol} 容量不可读")
            continue
        if free_min is None or u["free_gib"] < free_min:
            free_min = u["free_gib"]
        parts.append(f"{vol} 剩余 {u['free_gib']} GiB / 共 {u['total_gib']} GiB"
                     f"（{'、'.join(ent['labels'])}）")
    detail = "；".join(parts) + "。数值为所在磁盘卷容量，不是目录自身占用"
    if free_min is None:
        return _item("disk", "磁盘剩余空间", "warn", detail)
    if free_min < 20:
        return _item("disk", "磁盘剩余空间", "fail", detail,
                     "清理磁盘后再跑导出（阈值 20 GiB）")
    if free_min < 60:
        return _item("disk", "磁盘剩余空间", "warn", detail,
                     "低于 60 GiB，长剧导出前建议先清理")
    return _item("disk", "磁盘剩余空间", "ok", detail)


def _manifests():
    items = status.manifest_list()
    bad = [x["draft"] for x in items if x["has_conflict"]]
    if bad:
        return _item("manifests", "串流清单", "warn",
                     f"{len(items)} 份；{len(bad)} 份 state=archived 但目标缺失："
                     + "、".join(bad[:5]), "运行 verify --manual --all 复核")
    return _item("manifests", "串流清单", "ok", f"{len(items)} 份，无归档缺失")


def _listen(host, port):
    if host not in ("127.0.0.1", "localhost", "::1"):
        return _item("listen", "监听地址", "fail",
                     f"当前监听 {host}:{port}（应仅 127.0.0.1）", "改回 127.0.0.1")
    return _item("listen", "监听地址", "ok",
                 f"仅监听 {host}:{port}，未绑定 0.0.0.0")


# ── 汇总 ────────────────────────────────────────────────────────────
def run_all(host="127.0.0.1", port=8765):
    t0 = time.time()
    roots = paths.known_roots()
    checks = [_from_core(r) for r in doctor_core.run_checks().results]
    checks.extend([
        _writable_dir("projects", "剪映草稿根可写性", roots.get("projects")),
        _writable_dir("series", "短剧根目录可写性", roots.get("series")),
        _writable_dir("archive", "归档根目录可写性", roots.get("archive"), warn_missing=True),
        _writable_dir("export", "导出根目录可写性", roots.get("export")),
        _disk(),
        _manifests(),
        _listen(host, port),
    ])
    summary = {
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
    }
    return {"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(time.time() - t0, 2),
            "summary": summary, "checks": checks,
            "context": {"roots": roots, "host": host, "port": port}}
