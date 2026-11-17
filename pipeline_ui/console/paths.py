"""路径白名单与运行目录。

Console 不写任何业务文件：只读 pipeline/ lib/ 的落盘状态，只在
pipeline_ui/_console/ 下写自己的日志与运行记录。
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (os.path.join(REPO, "pipeline"), os.path.join(REPO, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CONSOLE_ROOT = os.path.join(REPO, "pipeline_ui", "_console")
LOG_DIR = os.path.join(CONSOLE_ROOT, "logs")
WORK_ROOT = os.path.join(CONSOLE_ROOT, "workdir")
UPLOAD_DIR = os.path.join(CONSOLE_ROOT, "uploads")
ENV_JOB_DIR = os.path.join(CONSOLE_ROOT, "env_jobs")
RUNS_FILE = os.path.join(CONSOLE_ROOT, "runs.jsonl")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def ensure_runtime_dirs():
    for d in (CONSOLE_ROOT, LOG_DIR, WORK_ROOT, UPLOAD_DIR, ENV_JOB_DIR):
        os.makedirs(d, exist_ok=True)


def _probe(fn):
    try:
        v = fn()
        return os.path.abspath(v) if v else ""
    except Exception:
        return ""


def known_roots():
    """当前生效的根目录（统一来自 export_config.resolve_paths）。

    series/archive/export/projects 是唯一业务根；watch 是有效扫描根列表
    （series_root + watch.roots 额外根），Console 选剧与白名单都以它为准。
    """
    try:
        import export_config as ec
        paths = ec.resolve_paths("console")["paths"]
    except Exception:
        return {}
    return {
        "series": _probe(lambda: paths.get("series_root")),
        "archive": _probe(lambda: paths.get("archive_root")),
        "export": _probe(lambda: paths.get("export_dir")),
        "projects": _probe(lambda: paths.get("projects_root")),
        "watch": [p for p in (_probe(lambda: x) for x in (paths.get("watch_roots") or []))
                  if p],
    }


def _real(p):
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def within(p, root):
    if not root:
        return False
    p, root = _real(p), _real(root)
    return p == root or p.startswith(root + os.sep)


def check(path, *, kind="dir", allow_repo=False):
    """校验路径落在白名单内并归一化；非法直接 ValueError。"""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("路径为空")
    if "\x00" in path:
        raise ValueError("路径非法")
    p = os.path.abspath(path.strip().strip('"'))
    roots = known_roots()
    allowed = [roots.get("series"), roots.get("archive"),
               roots.get("export"), roots.get("projects"), UPLOAD_DIR]
    allowed.extend(roots.get("watch") or [])
    if allow_repo:
        allowed.append(REPO)
    if not any(within(p, r) for r in allowed if r):
        raise ValueError(f"路径不在白名单内: {p}")
    if kind == "dir" and not os.path.isdir(p):
        raise ValueError(f"目录不存在: {p}")
    if kind == "file" and not os.path.isfile(p):
        raise ValueError(f"文件不存在: {p}")
    return p
