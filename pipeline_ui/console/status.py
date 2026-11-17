"""只读状态聚合：全部读现有落盘文件，不写、不加锁、不改 watcher 配置。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

import paths

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MANIFEST_DIR = os.path.join(paths.REPO, "pipeline", "_stream_manifests")
# watcher 进程心跳：lib/cover_watch.py 的 _WatcherHeartbeat 启动时同步写一次，
# 之后每 HEARTBEAT_INTERVAL_S=30s 原子写一次；超过 3 个周期（90s）视为过期。
WATCHER_HEARTBEAT = os.path.join(paths.REPO, "pipeline", "_work", "watcher_heartbeat.json")
WATCHER_PAUSE = os.path.join(paths.REPO, "pipeline", "_work", "watcher.pause")
WATCHER_HEARTBEAT_STALE_S = 90


# ── 基础 ────────────────────────────────────────────────────────────
def pid_state(pid):
    """alive / dead / unknown；任何不确定都归 unknown（不据此删锁）。"""
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return "dead"
    try:
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, timeout=10,
                             creationflags=_NO_WINDOW)
    except Exception:
        return "unknown"
    if res.returncode != 0:
        return "unknown"
    raw = res.stdout or b""
    if not raw:
        return "unknown"
    text = ""
    for enc in ("utf-8-sig", "gbk", "mbcs", "latin-1"):
        try:
            text = raw.decode(enc).strip()
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        return "unknown"
    for line in text.splitlines():
        cols = [c.strip().strip('"') for c in line.split('","')]
        if len(cols) > 1 and cols[1] == str(pid):
            return "alive"
    low = text.lower()
    if any(k in low for k in ("no tasks", "not found", "没有", "不存在")):
        return "dead"
    return "unknown"


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError):
        return ""


# ── 剧集 ────────────────────────────────────────────────────────────
def _ep_of(path, idx):
    name = os.path.basename(path)
    m = re.search(r"[第E]\s*(\d{1,3})\s*集", name)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,3})", name)
    if m:
        return int(m.group(1))
    return idx + 1


def _natural_key(path):
    s = os.path.basename(path)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def scan_episodes(series_dir):
    """与 batch_runner 一致的视频发现口径（只读）。返回 [{ep, name, path}]。"""
    vids = []
    for pat in ("*.mp4", "*.mkv", "*.mov"):
        vids += [os.path.join(series_dir, n) for n in os.listdir(series_dir)
                 if n.lower().endswith(pat[1:]) and
                 os.path.isfile(os.path.join(series_dir, n))]
    if not vids:
        cache = os.path.join(series_dir, "__jycache__")
        if os.path.isdir(cache):
            vids = [os.path.join(cache, n) for n in sorted(os.listdir(cache))
                    if n.lower().endswith(".mp4")]
    vids = sorted(set(vids), key=_natural_key)
    out = []
    for i, v in enumerate(vids):
        out.append({"ep": _ep_of(v, i), "name": os.path.basename(v), "path": v})
    return out


def list_series():
    """列出有效扫描根（series_root + watch.roots 额外根）下的剧集目录。"""
    roots = paths.known_roots()
    watch = roots.get("watch") or ([roots["series"]] if roots.get("series") else [])
    items = []
    seen = set()
    for root in watch:
        if not root or not os.path.isdir(root):
            continue
        key = os.path.normcase(os.path.realpath(root))
        if key in seen:
            continue
        seen.add(key)
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            eps = scan_episodes(p)
            if not eps:
                continue
            cover = ""
            for f in ("封面.png", "封面.jpg", "封面.jpeg", "封面.webp"):
                if os.path.isfile(os.path.join(p, f)):
                    cover = f
                    break
            items.append({
                "name": name, "path": p, "episodes": len(eps),
                "first_ep": eps[0]["ep"], "last_ep": eps[-1]["ep"], "cover": cover,
                "has_job": os.path.isfile(os.path.join(p, "cover_job", "job.json")),
                "mtime": os.path.getmtime(p),
            })
    # 选择剧集时优先展示最近修改的目录；同一时间按名称排序，保证顺序稳定。
    items.sort(key=lambda x: x["name"].casefold())
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def derive_draft_name(prefix, eps):
    eps = sorted(eps)
    if not eps:
        return ""
    if len(eps) > 1:
        return f"{prefix}_第{eps[0]:02d}-{eps[-1]:02d}集串流"
    return f"{prefix}_第{eps[0]:02d}集"


def select_episodes(series_dir, selection):
    """按 UI 选区计算集号与索引；返回 (eps, indices) 均 1-based。"""
    eps_all = scan_episodes(series_dir)
    mode = (selection or {}).get("mode") or "all"
    if mode == "range":
        a = int(selection.get("from") or 1)
        b = int(selection.get("to") or a)
        picked = [e for e in eps_all if a <= e["ep"] <= b]
    elif mode == "first":
        n = max(0, int(selection.get("n") or 0))
        picked = eps_all[:n]
    else:
        picked = eps_all
    picked_paths = {e["path"] for e in picked}
    return ([e["ep"] for e in picked],
            [i + 1 for i, e in enumerate(eps_all) if e["path"] in picked_paths])


# ── 导出进度 ────────────────────────────────────────────────────────
def read_progress(draft_name, extra_dirs=None):
    if not draft_name:
        return None
    roots = paths.known_roots()
    cands = []
    if roots.get("export"):
        cands += [os.path.join(roots["export"], f"{draft_name}.progress.json"),
                  os.path.join(roots["export"], draft_name,
                               f"{draft_name}.mp4.progress.json")]
    for d in extra_dirs or []:
        if d:
            cands += [os.path.join(d, f"{draft_name}.mp4.progress.json"),
                      os.path.join(d, f"{draft_name}.progress.json")]
    best = None
    for p in cands:
        if os.path.isfile(p):
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if best is None or mt > best[0]:
                best = (mt, p)
    if not best:
        return None
    data = read_json(best[1]) or {}
    data["_path"] = best[1]
    data["_mtime"] = _fmt_ts(best[0])
    return data


# ── 台账 ────────────────────────────────────────────────────────────
def job_info(series_dir):
    p = os.path.join(series_dir, "cover_job", "job.json")
    data = read_json(p)
    if not isinstance(data, dict):
        return None
    video = data.get("video") if isinstance(data.get("video"), dict) else {}
    titles = data.get("titles") if isinstance(data.get("titles"), dict) else {}
    return {
        "path": p,
        "stage": data.get("stage"),
        "revision": data.get("revision"),
        "video_status": video.get("status"),
        "export_state": video.get("export_state"),
        "gate": titles.get("gate"),
        "updated_at": data.get("updated_at") or _fmt_ts(os.path.getmtime(p)),
    }


# ── 清单 ────────────────────────────────────────────────────────────
def _is_archived_missing(ent):
    state = (ent or {}).get("state")
    if state != "archived":
        return False
    target = ent.get("archived_to") or ent.get("export_dir") or ""
    return bool(target) and not os.path.isdir(target)


def manifest_list():
    out = []
    if not os.path.isdir(MANIFEST_DIR):
        return out
    for fn in os.listdir(MANIFEST_DIR):
        if not fn.lower().endswith(".json"):
            continue
        path = os.path.join(MANIFEST_DIR, fn)
        data = read_json(path)
        if not isinstance(data, dict):
            continue
        versions = data.get("versions") if isinstance(data.get("versions"), dict) else {}
        vsum = {}
        for prof, ent in versions.items():
            if not isinstance(ent, dict):
                continue
            vsum[prof] = {
                "state": ent.get("state"),
                "draft": ent.get("draft"),
                "archived_to": ent.get("archived_to"),
                "export_file": ent.get("export_file"),
                "export_dir": ent.get("export_dir"),
                "missing": _is_archived_missing(ent),
            }
        p = data.get("paths") if isinstance(data.get("paths"), dict) else {}
        out.append({
            "draft": data.get("draft_name") or os.path.splitext(fn)[0],
            "file": fn,
            "source_dir": p.get("source_dir"),
            "export_file": p.get("export_file"),
            "draft_dir": p.get("draft_dir"),
            "workdir": p.get("workdir"),
            "jycache_dir": p.get("jycache_dir"),
            "updated_at": data.get("updated_at") or data.get("recorded_at"),
            "versions": vsum,
            "has_conflict": any(v["missing"] for v in vsum.values()),
            "mtime": os.path.getmtime(path),
        })
    # 归档草稿同样优先展示最近修改的清单；同一时间按草稿名稳定排序。
    out.sort(key=lambda x: x["draft"].casefold())
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ── 锁 ──────────────────────────────────────────────────────────────
def lock_diag():
    try:
        import export_lock as el
        p = el._LOCK_PATH
        data = el.read_lock()
    except Exception:
        p = os.path.join(paths.REPO, ".export.lock")
        data = read_json(p)
    if not os.path.exists(p):
        return {"path": p, "exists": False, "state": "free"}
    pid = 0
    try:
        pid = int((data or {}).get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    state = pid_state(pid)
    return {
        "path": p, "exists": True, "pid": pid, "pid_state": state,
        "owner": (data or {}).get("owner"),
        "started": (data or {}).get("started"),
        "state": "busy" if state == "alive" else ("stale" if state == "dead"
                                                 else "unknown"),
    }


def _age_text(seconds):
    if seconds is None:
        return "无心跳"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


def _iter_watcher_locks():
    """按剧扫 cover_job/.watcher.lock（只在 watcher 处理某部剧期间存在）。"""
    roots = paths.known_roots()
    root = roots.get("series") or ""
    locks = []
    if root and os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            lp = os.path.join(root, name, "cover_job", ".watcher.lock")
            if not os.path.isfile(lp):
                continue
            data = read_json(lp) or {}
            pid = data.get("pid")
            locks.append({
                "series": name, "path": lp, "pid": pid,
                "pid_state": pid_state(pid),
                "phase": data.get("phase"),
                "started": data.get("started"),
                "updated_at": data.get("updated_at") or _fmt_ts(os.path.getmtime(lp)),
            })
    return locks


def _find_watcher_pids():
    """无心跳时的进程兜底：从命令行里找正在跑 cover_watch.py 的 python 进程。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
        "Where-Object { $_.CommandLine -like '*cover_watch.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        return []
    text = (res.stdout or b"").decode("utf-8", "ignore")
    return [int(x) for x in re.findall(r"\d+", text)]


def watcher_info():
    """watcher 存活判定：心跳 + 进程 + job 锁，三证据合一。

    state:
      running        在跑（可能空闲无锁，也可能正在处理某部剧）
      lock_only_dead 锁在但进程已死（残留锁）
      stale          有心跳但已过期，且进程不在
      stopped        没在跑（无心跳、无锁、无进程）
    """
    locks = _iter_watcher_locks()
    lock_alive = [x for x in locks if x["pid_state"] == "alive"]

    hb_path = WATCHER_HEARTBEAT
    hb_exists = os.path.isfile(hb_path)
    hb = read_json(hb_path) or {}
    try:
        hb_pid = int(hb.get("pid") or 0)
    except (TypeError, ValueError):
        hb_pid = 0
    try:
        hb_ts = float(hb.get("updated_ts") or 0)
    except (TypeError, ValueError):
        hb_ts = 0.0
    if not hb_ts and hb_exists:
        try:
            hb_ts = os.path.getmtime(hb_path)
        except OSError:
            hb_ts = 0.0
    age = (time.time() - hb_ts) if hb_ts else None
    hb_fresh = bool(age is not None and age <= WATCHER_HEARTBEAT_STALE_S)
    hb_pid_state = pid_state(hb_pid) if hb_exists else "dead"

    if hb_exists and hb_pid_state == "alive":
        pids = [hb_pid]
    else:
        pids = _find_watcher_pids()
    proc_alive = bool(pids)

    if proc_alive or lock_alive or (hb_fresh and hb_pid_state != "dead"):
        state = "running"
    elif locks:
        state = "lock_only_dead"
    elif hb_exists:
        state = "stale"
    else:
        state = "stopped"

    heartbeat = {
        "path": hb_path, "exists": hb_exists,
        "pid": hb_pid, "pid_state": hb_pid_state,
        "updated_ts": hb_ts,
        "updated_at": hb.get("updated_at") or _fmt_ts(hb_ts),
        "age_s": round(age, 1) if age is not None else None,
        "age_text": _age_text(age),
        "fresh": hb_fresh, "stale_after_s": WATCHER_HEARTBEAT_STALE_S,
        "stage": hb.get("stage"), "job": hb.get("job"),
        "phase": hb.get("phase"), "round": hb.get("round"),
        "code_fingerprint": hb.get("code_fingerprint"),
    }
    return {"locks": locks, "alive": len(lock_alive), "total": len(locks),
            "heartbeat": heartbeat,
            "process": {"pids": pids, "alive": proc_alive},
            "paused": os.path.isfile(WATCHER_PAUSE),
            "pause_path": WATCHER_PAUSE,
            "state": state}


# ── 外部任务 / watcher（只读，不做停止控制）─────────────────────────
_PIPELINE_SCRIPTS = {
    "batch_runner.py": "标准串流",
    "series_runner.py": "多剧串流",
    "ui_pipeline.py": "UI 自动剪辑",
}
# 带值的参数：解析命令行时跳过其值，避免把参数值误认成剧目录
_VALUE_FLAGS = {
    "--video-dir", "--workdir", "--draft-name", "--draft-prefix", "--cut",
    "--pattern", "--from", "--to", "--only", "--limit", "--delay", "--retries",
    "--export-timeout", "--export-dir", "--presets", "--profile", "--vocal-mode",
    "--resolution", "--workers", "--config", "--cover-image", "--cover-source",
    "--start", "--end", "--mode", "--episodes", "--draft",
}
_TASK_SCAN_PS = (
    "Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
    "Where-Object { $_.CommandLine -match 'batch_runner\\.py|series_runner\\.py|ui_pipeline\\.py' } | "
    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
)
_WATCHER_STAGE_TEXT = {
    "starting": "启动中", "scan": "扫描中", "idle": "空闲",
    "edit": "剪辑中", "export": "导出中", "wait-lock": "等待导出锁",
    "stop": "停止中", "stopped": "已停止",
}


def _cmd_tokens(cmdline):
    """按 Windows 命令行习惯切分：双引号内保留空格，返回去引号的 token。"""
    return [t.strip('"') for t in re.findall(r'"[^"]*"|[^\s"]+', cmdline or "")]


def _flag_value(tokens, flag):
    for i, tok in enumerate(tokens):
        if tok == flag and i + 1 < len(tokens):
            return tokens[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return ""


def _positionals(tokens, start):
    """从 start 起收集位置参数，跳过选项及其值。"""
    out, skip_value = [], False
    for tok in tokens[start:]:
        if skip_value:
            skip_value = False
            continue
        if tok.startswith("-"):
            skip_value = tok in _VALUE_FLAGS
            continue
        out.append(tok)
    return out


def cmd_series_dir(cmdline, script):
    """从命令行推剧目录：ui_pipeline 用 --video-dir，其余取第一个位置参数。"""
    tokens = _cmd_tokens(cmdline)
    if script == "ui_pipeline.py":
        return _flag_value(tokens, "--video-dir")
    idx = None
    for i, tok in enumerate(tokens):
        if tok.replace("\\", "/").endswith("/" + script) or tok == script:
            idx = i
            break
    if idx is None:
        return ""
    args = _positionals(tokens, idx + 1)
    return args[0] if args else ""


def _default_workdir(series_dir):
    """与 batch_runner.py 的默认工作目录规则保持一致。"""
    import hashlib
    base = os.path.basename(series_dir.rstrip("\\/")) or "series"
    safe = re.sub(r'[<>:"/\\|?*\s]+', "", base)[:24] or "series"
    digest = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
    return os.path.join(paths.REPO, "pipeline", f"_work_{safe}_{digest}")


def cmd_workdir(cmdline, script, series_dir):
    workdir = _flag_value(_cmd_tokens(cmdline), "--workdir")
    if workdir:
        return workdir
    return _default_workdir(series_dir) if series_dir else ""


def batch_state_summary(workdir):
    """读 <workdir>/_batch_state.json，汇总逐集进度；无状态文件返回 None。

    只统计带 status 的逐集条目：`run`（运行记录快照）不是集，不能计入 total。
    """
    data = read_json(os.path.join(workdir, "_batch_state.json")) if workdir else None
    if not isinstance(data, dict) or not data:
        return None
    eps = {k: v for k, v in data.items()
           if isinstance(v, dict) and k != "run" and v.get("status")}
    if not eps:
        return None
    total = len(eps)
    counts = {"done": 0, "running": 0, "failed": 0}
    cur_ep = None
    for ent in eps.values():
        st = ent.get("status")
        if st in counts:
            counts[st] += 1
        if st == "running":
            cur_ep = ent.get("ep")
    finished = counts["done"] + counts["failed"]
    percent = round(finished * 100.0 / total) if total else None
    if counts["running"] and cur_ep:
        stage = f"第{cur_ep}集处理中"
    elif finished >= total:
        stage = "全部完成"
    elif counts["failed"]:
        stage = f"完成 {finished}/{total}"
    else:
        stage = "等待开始"
    detail = f"已完成 {finished}/{total}"
    if counts["failed"]:
        detail += f"，失败 {counts['failed']}"
    if counts["running"] and cur_ep:
        detail += f"，第{cur_ep}集处理中"
    return {"total": total, "done": counts["done"], "running": counts["running"],
            "failed": counts["failed"], "percent": percent,
            "stage": stage, "detail": detail,
            "run": read_run(workdir) or (data.get("run") if isinstance(data.get("run"), dict) else None)}


def read_run(workdir):
    """读取 <workdir>/_run.json（最近一次运行记录）；缺失或损坏返回 None。"""
    if not workdir:
        return None
    data = read_json(os.path.join(workdir, "_run.json"))
    return data if isinstance(data, dict) and data.get("run_id") else None


# ── pipeline 结构化进度（_progress.json）与阶段权重 ──
# 权重与 runner.STAGES 一致：0-90 为流水线阶段，90-99 为导出。
STAGE_WEIGHTS = {
    "build": 0, "demucs": 1, "presets_cut": 27, "asr": 28,
    "asr_translate": 28, "translate": 44, "cleanup": 85, "verify": 88, "export": 90,
}
_NEXT_STAGE = {
    "build": "demucs", "demucs": "presets_cut", "presets_cut": "asr",
    "asr": "translate", "asr_translate": "cleanup", "translate": "cleanup",
    "cleanup": "verify", "verify": "export", "export": "export",
}


def read_pipeline_progress(workdir, max_age_s=300):
    """读取 pipeline 写的 <workdir>/_progress.json；过期/损坏/版本不符返回 None。"""
    if not workdir:
        return None
    data = read_json(os.path.join(workdir, "_progress.json"))
    if not isinstance(data, dict) or data.get("v") != 1 or not data.get("stage"):
        return None
    ts = data.get("updated_at")
    if not isinstance(ts, (int, float)) or time.time() - ts > max_age_s:
        return None
    return data


def progress_percent(prog):
    """把进度文件换算成阶段权重百分比（导出阶段返回 None，交由导出进度文件）。"""
    if not isinstance(prog, dict):
        return None
    stage = prog.get("stage")
    base = STAGE_WEIGHTS.get(stage)
    if base is None or stage == "export":
        return None
    nxt = STAGE_WEIGHTS.get(_NEXT_STAGE.get(stage, stage), base)
    total = prog.get("total") or 0
    done = prog.get("done") or 0
    if total > 0:
        ratio = min(1.0, max(0.0, float(done) / float(total)))
        return round(base + (nxt - base) * ratio, 1)
    return base


def latest_runs(limit=20):
    """扫描 pipeline/_work* 与 Console 工作目录，按 started_at 倒序返回最近几次运行记录。"""
    dirs = []
    base = os.path.dirname(MANIFEST_DIR)
    try:
        dirs.extend(os.path.join(base, n) for n in os.listdir(base) if n.startswith("_work"))
    except OSError:
        pass
    if os.path.isdir(paths.WORK_ROOT):
        try:
            dirs.extend(os.path.join(paths.WORK_ROOT, n) for n in os.listdir(paths.WORK_ROOT))
        except OSError:
            pass
    out = []
    for workdir in dirs:
        data = read_json(os.path.join(workdir, "_run.json"))
        if isinstance(data, dict) and data.get("run_id"):
            out.append(data)
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out[:limit]


def _file_heartbeat(path):
    """以文件 mtime 作为该任务最后一次落盘的“心跳”。"""
    if not path:
        return None
    try:
        ts = os.path.getmtime(path)
    except OSError:
        return None
    age = max(0.0, time.time() - ts)
    return {"path": path, "updated_at": _fmt_ts(ts),
            "age_s": round(age, 1), "age_text": _age_text(age)}


def _watcher_stage_text(stage):
    if not stage:
        return "—"
    return _WATCHER_STAGE_TEXT.get(str(stage), str(stage))


def _decode_output(raw):
    """PowerShell 管道输出的编码随系统变化，依次尝试常见编码。"""
    for enc in ("utf-8-sig", "utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def scan_pipeline_processes():
    """只读扫描命令行里的 batch_runner / series_runner / ui_pipeline 进程。"""
    if os.name != "nt":
        return []
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TASK_SCAN_PS],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        return []
    if res.returncode != 0:
        return []
    text = _decode_output(res.stdout or b"").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cmdline = str(item.get("CommandLine") or "")
        script = next((s for s in _PIPELINE_SCRIPTS if s in cmdline), "")
        if not script:
            continue
        try:
            pid = int(item.get("ProcessId") or 0)
        except (TypeError, ValueError):
            pid = 0
        series_dir = cmd_series_dir(cmdline, script)
        if series_dir and not os.path.isabs(series_dir):
            series_dir = os.path.join(paths.REPO, series_dir)
        out.append({"pid": pid, "script": script, "cmdline": cmdline,
                    "series_dir": series_dir,
                    "workdir": cmd_workdir(cmdline, script, series_dir)})
    return out


def external_tasks(exclude_pids=()):
    """外部任务只读快照：watcher + 命令行串流 + 全局导出锁。

    只读展示，不提供停止/控制入口；exclude_pids 用于排除 Console 自己启动的
    任务（避免与“任务状态”卡片重复）。
    """
    excluded = {int(p) for p in exclude_pids if str(p).isdigit()}
    watcher = watcher_info()
    lock = lock_diag()
    hb = watcher.get("heartbeat") or {}
    tasks, seen = [], set()

    for lk in watcher.get("locks") or []:
        if lk.get("pid_state") != "alive":
            continue
        try:
            pid = int(lk.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid:
            seen.add(pid)
        tasks.append({
            "kind": "watcher", "source": "watcher",
            "series": lk.get("series") or "",
            "stage": "处理中" + (f"（{lk.get('phase')}）" if lk.get("phase") else ""),
            "percent": None, "pid": pid,
            "task_dir": os.path.dirname(lk.get("path") or ""),
            "heartbeat": _file_heartbeat(lk.get("path")),
            "detail": "cover_job 活锁",
        })

    if not tasks and watcher.get("state") == "running":
        job = hb.get("job") or ""
        series = os.path.basename(os.path.dirname(job)) if job else ""
        pid = int(hb.get("pid") or 0) if str(hb.get("pid") or "0").isdigit() else 0
        if pid:
            seen.add(pid)
        wd = _default_workdir(job) if job else ""
        prog = read_pipeline_progress(wd)
        tasks.append({
            "kind": "watcher", "source": "watcher",
            "series": series,
            "stage": (prog or {}).get("label") or _watcher_stage_text(hb.get("stage")),
            "percent": progress_percent(prog) if prog else None, "pid": pid,
            "task_dir": wd or job or os.path.dirname(WATCHER_HEARTBEAT),
            "heartbeat": _file_heartbeat(WATCHER_HEARTBEAT),
            "detail": (f"{prog['label']} {prog.get('done', 0)}/{prog['total']}"
                       if prog and prog.get("total") else "心跳存活，当前无剧目录锁"),
        })

    for proc in scan_pipeline_processes():
        pid = int(proc.get("pid") or 0)
        if pid in excluded or pid in seen:
            continue
        workdir = proc.get("workdir") or ""
        series_dir = proc.get("series_dir") or ""
        summary = batch_state_summary(workdir)
        prog = read_pipeline_progress(workdir)
        state_path = os.path.join(workdir, "_batch_state.json") if workdir else ""
        tasks.append({
            "kind": "pipeline",
            "source": _PIPELINE_SCRIPTS.get(proc.get("script"), proc.get("script") or ""),
            "series": os.path.basename(series_dir.rstrip("\\/")) or series_dir,
            "stage": (prog or {}).get("label") or (summary or {}).get("stage") or "运行中",
            "percent": (progress_percent(prog) if prog
                        else (summary or {}).get("percent")),
            "pid": pid,
            "task_dir": workdir or series_dir,
            "heartbeat": _file_heartbeat(state_path),
            "detail": (f"{prog['label']} {prog.get('done', 0)}/{prog['total']}"
                       if prog and prog.get("total")
                       else (summary or {}).get("detail")
                       or "无 _batch_state.json，无法给出逐集进度"),
        })
        if pid:
            seen.add(pid)

    lock_pid = int(lock.get("pid") or 0)
    if lock.get("state") == "busy" and lock_pid not in seen and lock_pid not in excluded:
        owner = str(lock.get("owner") or "")
        tasks.append({
            "kind": "export", "source": "导出锁",
            "series": owner.replace("batch_runner", "").strip(),
            "stage": "导出占用", "percent": None, "pid": lock_pid,
            "task_dir": lock.get("path"),
            "heartbeat": _file_heartbeat(lock.get("path")),
            "detail": owner or "全局导出锁被占用",
        })

    return {"scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tasks": tasks, "watcher": watcher, "lock": lock}


# ── 外部任务历史（只读聚合：watcher / 手工命令行串流）──────────────
# 唯一写者仍是 Runner（runs.jsonl）；这里只做「读 + 拼装」，不新增任何落盘。
_JOB_STAGE_TEXT = {
    "new": "新建", "prompts_ready": "提示词就绪", "covers_ready": "封面就绪",
    "edit_ready": "待剪辑", "editing": "剪辑中", "video_ready": "视频就绪",
    "published": "已发布",
}


def _job_stage_text(stage):
    if not stage:
        return "—"
    return _JOB_STAGE_TEXT.get(str(stage), str(stage))


def _derive_job_status(video_status, export_status, watcher_running):
    """job.json video.status / .export_state.json status → 统一四态。"""
    vs, es = str(video_status or ""), str(export_status or "")
    if vs == "done" or es == "done":
        return "succeeded"
    if vs == "failed" or es == "failed":
        return "failed"
    if vs == "stopped" or es == "stopped":
        return "stopped"
    if es == "exporting":
        # 没有 watcher 进程还在跑 ⇒ 这条记录是上次中断留下的快照
        return "running" if watcher_running else "stopped"
    return "pending"


def _watcher_job_entries(watcher_running):
    """扫 <series_root>/*/cover_job/job.json，映射成历史条目（只读）。"""
    root = paths.known_roots().get("series") or ""
    if not root or not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        job_dir = os.path.join(root, name, "cover_job")
        jp = os.path.join(job_dir, "job.json")
        if not os.path.isfile(jp):
            continue
        data = read_json(jp)
        if not isinstance(data, dict):
            continue
        video = data.get("video") if isinstance(data.get("video"), dict) else {}
        hist = [h for h in (data.get("history") or []) if isinstance(h, dict)]
        es = read_json(os.path.join(job_dir, ".export_state.json"))
        if not isinstance(es, dict):
            es = {}
        first_at = str(hist[0].get("at") or "") if hist else ""
        last = hist[-1] if hist else {}
        try:
            mtime = os.path.getmtime(jp)
        except OSError:
            continue
        draft = str(video.get("draft_name") or "")
        pct = es.get("progress")
        out.append({
            "id": "watcher:" + str(data.get("job_id") or name),
            "source": "watcher",
            "series": name,
            "series_dir": os.path.join(root, name),
            "draft": draft,
            "status": _derive_job_status(video.get("status"), es.get("status"),
                                        watcher_running),
            "percent": int(pct) if isinstance(pct, (int, float)) else None,
            "started_at": first_at,
            "ended_at": str(last.get("at") or data.get("updated_at") or ""),
            "stage": _job_stage_text(data.get("stage")),
            "detail": str(last.get("detail") or ""),
            "artifact": str(video.get("export_file") or ""),
            "attempts": es.get("attempts"),
            "last_error": es.get("last_error"),
            "mtime": mtime,
        })
    return out


def _batch_state_entries(watcher_running, exclude_drafts):
    """扫 pipeline/_work_*/_batch_state.json（按 mtime），还原手工串流记录。

    这份快照只有逐集 status/finished_at，没有 run id、退出码和完整阶段，
    因此状态是推断值，不能当作与 runs.jsonl 等价的运行记录。
    """
    base = os.path.join(paths.REPO, "pipeline")
    if not os.path.isdir(base):
        return []
    out = []
    for name in os.listdir(base):
        if not name.startswith("_work_"):
            continue
        bpath = os.path.join(base, name, "_batch_state.json")
        if not os.path.isfile(bpath):
            continue
        data = read_json(bpath)
        if not isinstance(data, dict) or not data:
            continue
        ents = [e for e in data.values() if isinstance(e, dict)]
        drafts = {str(e.get("draft") or "") for e in ents if e.get("draft")}
        if drafts & exclude_drafts:
            continue  # watcher / Console 已有同草稿的记录，避免重复
        total = len(ents)
        done = sum(1 for e in ents if e.get("status") == "done")
        failed = sum(1 for e in ents if e.get("status") == "failed")
        busy = sum(1 for e in ents if e.get("status") == "running")
        if total and done + failed >= total:
            status = "succeeded" if failed == 0 else "failed"
        elif busy:
            status = "running" if watcher_running else "stopped"
        else:
            status = "pending"
        stamps = sorted(str(e.get("finished_at") or "") for e in ents
                        if e.get("finished_at"))
        try:
            mtime = os.path.getmtime(bpath)
        except OSError:
            continue
        label = name[len("_work_"):]
        head, _, tail = label.rpartition("_")
        if head and re.fullmatch(r"[0-9a-f]{6,}", tail):
            label = head
        detail = f"已完成 {done}/{total}"
        if failed:
            detail += f"，失败 {failed}"
        if busy:
            detail += f"，{busy} 集进行中"
        out.append({
            "id": "manual:" + name,
            "source": "manual",
            "series": label,
            "series_dir": "",
            "draft": next(iter(drafts), ""),
            "status": status,
            "percent": round((done + failed) * 100.0 / total) if total else None,
            "started_at": "",
            "ended_at": stamps[-1] if stamps else _fmt_ts(mtime),
            "stage": "逐集快照",
            "detail": detail,
            "artifact": "",
            "attempts": None,
            "last_error": None,
            "mtime": mtime,
        })
    return out


def external_history(limit=100, exclude_drafts=()):
    """外部任务历史：watcher 的 job.json 记录 + 手工串流的逐集快照。

    完全只读：不写 runs.jsonl、不加锁、不改 job.json / _batch_state.json。
    """
    watcher_running = watcher_info().get("state") == "running"
    wjobs = _watcher_job_entries(watcher_running)
    covered = {e["draft"] for e in wjobs if e.get("draft")}
    covered |= {str(d) for d in exclude_drafts if d}
    manual = _batch_state_entries(watcher_running, covered)
    rows = wjobs + manual
    rows.sort(key=lambda x: (x.get("ended_at") or "", x.get("mtime") or 0),
              reverse=True)
    for r in rows:
        r.pop("mtime", None)
    return rows[:max(1, int(limit or 100))]


def disk_usage(path):
    """返回所在磁盘卷的容量（GiB，1024³ 口径，不再写成 GB）。"""
    try:
        u = __import__("shutil").disk_usage(path)
        return {"total_gib": round(u.total / 1024 ** 3, 1),
                "free_gib": round(u.free / 1024 ** 3, 1)}
    except OSError:
        return None
