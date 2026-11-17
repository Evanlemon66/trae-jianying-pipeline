"""产物级内容缓存（断点续跑）：只复用耗时中间产物，不参与写盘/发布决策。

约定见 specs/console-resume-cache/spec.md：
- 缓存根 `<workdir>/__cache__/<stage>/<key>.json`；key 含 CACHE_VERSION + 阶段参数 + 源指纹；
- 原子写（tmp + os.replace）；清单缺失 / 版本不符 / 产物大小不符 → miss；
- 目录型大产物不复制，只记清单（rel + size）原地校验；
- 任何缓存异常只降级为 miss，绝不打断流水线；`--no-cache`（configure(False)）时不读不写不打印。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

CACHE_VERSION = 1
CACHE_DIRNAME = "__cache__"

_ENABLED = os.environ.get("JY_NO_CACHE", "").strip() != "1"


def configure(use_cache=True):
    """全局开关：一次进程内生效（batch_runner 每轮传入同一值）。"""
    global _ENABLED
    _ENABLED = bool(use_cache)


def enabled():
    return _ENABLED


def fingerprint(path):
    """源文件指纹：路径 + 大小 + mtime_ns（同内容重拷会变，属预期 miss）。"""
    p = os.path.abspath(str(path))
    st = os.stat(p)
    return {"path": p, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def make_key(stage, params, sources):
    payload = {"version": CACHE_VERSION, "stage": stage,
               "params": params or {}, "sources": sources or []}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def cache_file(workdir, stage, key):
    return os.path.join(workdir, CACHE_DIRNAME, stage, f"{key}.json")


def rel_path(workdir, path):
    """产物 → 相对 workdir 的 posix 路径（跨盘/异常返回 None，调用方按不缓存处理）。"""
    try:
        return os.path.relpath(os.path.abspath(str(path)),
                               os.path.abspath(workdir)).replace("\\", "/")
    except (OSError, ValueError):
        return None


def abs_path(workdir, rel):
    return os.path.join(os.path.abspath(workdir), str(rel).replace("/", os.sep)) if rel else None


def files_manifest(workdir, paths):
    """显式文件列表 → [{"rel","size"}]；不可相对化的路径跳过（该次不缓存）。"""
    out = []
    for p in paths:
        if not p:
            continue
        rel = rel_path(workdir, p)
        if rel is None:
            return []
        try:
            out.append({"rel": rel, "size": os.path.getsize(p)})
        except OSError:
            return []
    out.sort(key=lambda e: e["rel"])
    return out


def dir_manifest(root, workdir):
    """目录树 → [{"rel","size"}]，用于 demucs 产物原地校验（不复制大文件）。"""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = rel_path(workdir, p)
            try:
                out.append({"rel": rel, "size": os.path.getsize(p)})
            except OSError:
                continue
    out.sort(key=lambda e: e["rel"])
    return out


def _files_ok(workdir, files):
    for e in files or []:
        p = abs_path(workdir, e.get("rel"))
        try:
            if not p or not os.path.isfile(p) or os.path.getsize(p) != int(e.get("size")):
                return False
        except (OSError, TypeError, ValueError):
            return False
    return True


def load(workdir, stage, key):
    if not _ENABLED:
        return None
    try:
        with open(cache_file(workdir, stage, key), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("version") != CACHE_VERSION or data.get("key") != key:
        return None
    if not _files_ok(workdir, data.get("files")):
        return None
    return data


def save(workdir, stage, key, payload):
    if not _ENABLED:
        return None
    path = cache_file(workdir, stage, key)
    data = dict(payload or {})
    data.update({"version": CACHE_VERSION, "stage": stage, "key": key,
                 "created_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return path
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None


def note(_stage, label, detail=""):
    """命中提示：`♻️ 复用第N集 人声分离（demucs 缓存命中）`。"""
    if not _ENABLED:
        return
    extra = f" {detail}" if detail else ""
    print(f"  ♻️ 复用{label}（{_stage} 缓存命中）{extra}")
