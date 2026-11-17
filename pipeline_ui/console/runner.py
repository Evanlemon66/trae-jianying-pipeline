"""单队列执行器：spawn batch_runner / ui_pipeline，维护实时状态与历史。

硬约束：
- 固定追加 --no-watch；不构造 --force / --fresh / --clean-workdir / --kill-jianying；
- 不做排队抢占，全局导出锁被占时任务会失败并如实上报；
- 只写 pipeline_ui/_console/（runs.jsonl + logs/ + workdir/）。
"""
from __future__ import annotations

import json
import os
import re
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid

import paths
import status

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 串流/标准链路（batch_runner → pipeline.run_pipeline）的真实执行顺序：
#   建稿（逐集转封装 + 注入主轨）→ 人声分离 → 合并预设 → 裁剪 → 识别翻译
#   → 隐藏中文 → VAD → 终检 → 补翻 → 帧对齐 → 导出
# 注意：`🧩 预设来源:` 只是配置回显，打印在注入之前，绝不能用它推进阶段。
STAGES = [
    ("build", "建稿 · 注入视频 + 草稿初始化", 0,
     ["单草稿串流:", "暂存区构建草稿:", "🎬 往已有草稿注入", "初始化草稿", "🎬 草稿:"]),
    ("demucs", "人声分离 + 音频提取", 1,
     ["人声分离 (demucs)", "🎬 先从视频提取音频"]),
    ("presets_cut", "预设合并 + 裁剪", 27,
     ["加预设:", "[6] 裁剪 ", "→ 裁剪后"]),
    ("asr", "识别字幕 + 音频提取", 28,
     ["提取音频 → 识别字幕 → 翻译", "逐段识别", "✅ 提取到:"]),
    ("translate", "翻译字幕", 44,
     ["翻译全链失败", "翻译修复轮"]),
    ("cleanup", "隐藏中文字幕 + VAD 门控", 85,
     ["[10] 隐藏中文字幕", "[10.2] VAD"]),
    ("verify", "终检 + 残缺集补翻 + 帧对齐", 88,
     ["字幕/音频终检", "终检通过", "[10.8]", "📐 帧对齐", "帧对齐:"]),
    ("export", "导出（含多账号）", 90,
     ["[11] 导出", "后台导出 →", "多账号串行导出"]),
]

# UI 自动剪辑链路（ui_pipeline）阶段与串流不同：按 STEP_ORDER 的步骤名单独建表，
# 避免两条链路的同名片段互相误匹配（如 [1] 预设合并 是 UI 的步骤、串流里没有）。
UI_STAGES = [
    ("precheck", "校准初检", 0, ["[P] 校准初检"]),
    ("init_draft", "初始化草稿", 2, ["[0] 初始化草稿"]),
    ("presets", "预设合并", 10, ["[1] 预设合并"]),
    ("cut_timeline", "时间线裁剪", 18, ["[2] 时间线裁剪"]),
    ("vocal", "人声分离", 25, ["[3] 人声分离"]),
    ("asr", "识别翻译", 40, ["[4] 识别+翻译字幕"]),
    ("finalize", "收尾终检", 75, ["[5] 分离音频+收尾+终检"]),
    ("export", "导出", 90, ["[6] 导出 MP4"]),
]
_STAGE_BY_KEY = {s[0]: s for s in STAGES}
_UI_STAGE_BY_KEY = {s[0]: s for s in UI_STAGES}


def stages_for(mode):
    """按链路返回阶段表：UI 自动剪辑与串流各自一套。"""
    return UI_STAGES if mode == "ui" else STAGES


_DRAFT_PATTERNS = [
    re.compile(r"单草稿串流:.*?→\s*(.+?)\s*$"),
    re.compile(r"暂存区构建草稿:\s*(.+?)\s*$"),
    re.compile(r"🎬 草稿:\s*(.+?)\s*$"),
]
# 串流建稿：`🎬 往已有草稿注入 N 个视频:` 给总数，每行 `📼 ...` 表示一集注入完成
# （注入前会按需转封装成 __jy_norm__.mp4，这是整季最耗时的一段）。
_INJECT_TOTAL_RE = re.compile(r"🎬 往已有草稿注入\s*(\d+)\s*个视频")
_EXPORT_PATH_RE = re.compile(r"后台导出 →\s*(.+?)\s*$")
# ── 多账号（--profile a,b）日志识别 ──
_CLONE_OK_RE = re.compile(r"账号预设草稿就绪:\s*(.+?)\s*（(.+?)）")
_CLONE_FAIL_RE = re.compile(r"克隆账号预设草稿失败（(.+?)）:\s*(.*)$")
_SEQ_EXPORT_RE = re.compile(r"串行导出\s*(\d+)/(\d+):\s*(.+?)\s*→\s*(.+?)\s*$")
_SEQ_FAIL_LIST_RE = re.compile(r"以下账号草稿导出失败:\s*(.+?)\s*$")
_EXPORT_EXC_RE = re.compile(r"导出异常:\s*(.+?):\s*(.*)$")
_MISSING_MP4_RE = re.compile(r"成品未落盘:\s*(.+?)\s*$")


def _export_percent(index, total, frac=0.0):
    """串行导出阶段的全局进度：90→99 按账号数均分，绝不回退。"""
    if not total or total <= 1:
        return 90
    frac = max(0.0, min(1.0, float(frac or 0)))
    pct = 90 + 10.0 * ((max(1, index) - 1) + frac) / total
    return max(90, min(99, int(round(pct))))


# `_progress.json` 的 stage → 计时表里的中文阶段名（只用于有 done/total 的阶段）
_HIST_STAGE_KEY = {
    "demucs": ("人声分离",),
    "asr_translate": ("ASR", "翻译"),
}
_TIMING_HISTORY = {}


def _hist_per_ep(workdir, stage, max_runs=3):
    """从 `<workdir>/_timings.jsonl` 最近 N 条记录估当前阶段的每集耗时。

    只读、无样本返回 None；文件按 mtime 缓存解析结果，避免每秒重读。
    """
    keys = _HIST_STAGE_KEY.get(stage) or ()
    if not keys or not workdir:
        return None
    path = os.path.join(workdir, "_timings.jsonl")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    cached = _TIMING_HISTORY.get(path)
    if not cached or cached[0] != mt:
        rows = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return None
        cached = (mt, rows[-max_runs:])
        _TIMING_HISTORY[path] = cached
    vals = []
    for rec in cached[1]:
        stages = rec.get("stages") if isinstance(rec, dict) else None
        if not isinstance(stages, dict):
            continue
        secs, count = 0.0, 0
        for k in keys:
            ent = stages.get(k) or {}
            secs += float(ent.get("total_s") or 0)
            count = max(count, int(ent.get("count") or 0))
        if secs > 0 and count > 0:
            vals.append(secs / count)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _new_accounts(profiles, draft):
    """多账号运行记录：每个预设组一行，顺序即导出顺序。"""
    return [{"profile": p, "draft": draft if i == 0 else None, "status": "pending",
             "artifact": None, "export_s": None, "reason": None}
            for i, p in enumerate(profiles)]


def _account_by(run, profile=None, draft=None, artifact=None):
    for a in run.get("accounts") or []:
        if profile and a.get("profile") == profile:
            return a
        if draft and a.get("draft") == draft:
            return a
        if artifact and a.get("artifact") == artifact:
            return a
    return None


def _export_view(prog):
    return {"status": prog.get("status"),
            "percent_value": prog.get("percent_value"),
            "rate_mbps": prog.get("rate_mbps"),
            "eta_s": prog.get("eta_s"),
            "phase": prog.get("phase"),
            "exported_mb": prog.get("exported_mb"),
            "total_mb": prog.get("total_mb")}


def _mb_text(mb):
    """体积文案：>=1GB 用 GB，其余 MB。"""
    if not mb:
        return "0 MB"
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{round(mb)} MB"


def _loads(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _norm_profiles(payload):
    """预设组列表：去重保序、归一化 presets_ 前缀，顺序即账号顺序。

    payload["profiles"]（前端多选，列表）优先，兼容旧的 payload["profile"]（逗号串）。
    """
    raw = payload.get("profiles")
    if raw in (None, ""):
        raw = payload.get("profile")
    items = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;，；\s]+", str(raw or ""))
    out = []
    for item in items:
        name = str(item or "").strip()
        if name.lower().startswith("presets_"):
            name = name[len("presets_"):].strip()
        name = name.lower()
        if name and name not in out:
            out.append(name)
    return out


class Runner:
    def __init__(self):
        self._lock = threading.RLock()
        self._current = None
        self._queue = queue.Queue()
        self._worker = None
        self._proc = None
        self._stop_requested = False

    # ── 参数校验 / 命令构造 ──────────────────────────────────────────
    @staticmethod
    def normalize(payload):
        if not isinstance(payload, dict):
            raise ValueError("参数格式错误")
        mode = payload.get("mode") or "batch"
        if mode not in ("batch", "ui"):
            raise ValueError("mode 只能是 batch 或 ui")
        series = paths.check(payload.get("series_dir"), kind="dir")
        sel = payload.get("selection") or {"mode": "all"}
        sel_mode = sel.get("mode") or "all"
        if sel_mode not in ("range", "first", "all"):
            raise ValueError("选集方式非法")
        sel_norm = {"mode": sel_mode}
        if sel_mode == "range":
            a, b = int(sel.get("from") or 0), int(sel.get("to") or 0)
            if a < 1 or b < a:
                raise ValueError("集数区间非法")
            sel_norm.update({"from": a, "to": b})
        elif sel_mode == "first":
            n = int(sel.get("n") or 0)
            if n < 1:
                raise ValueError("前 N 集必须是正整数")
            sel_norm["n"] = n

        cut = payload.get("cut")
        if cut in (None, ""):
            cut = None
        else:
            try:
                cut = float(cut)
            except (TypeError, ValueError):
                raise ValueError("裁剪秒数必须是数字")
            if cut != cut:  # NaN
                raise ValueError("裁剪秒数必须是数字")
            if cut < 0:
                raise ValueError("裁剪秒数不能为负")
            if cut > 3600:
                raise ValueError("裁剪秒数应在 0–3600 秒之间")
            cut = round(cut, 3)
            if cut == 0:
                cut = None  # 0 = 不裁剪：命令行不出现 --cut
        cover = (payload.get("cover_image") or "").strip()
        if cover:
            cover = paths.check(cover, kind="file")
        cover_source = ("upload" if cover and paths.within(cover, paths.UPLOAD_DIR)
                        else "path" if cover else "auto")

        prefix = (payload.get("draft_prefix") or "").strip()
        if prefix and re.search(r'[<>:"/\\|?*]', prefix):
            raise ValueError("草稿前缀含非法字符")

        retries = int(payload.get("retries") if payload.get("retries") not in (None, "")
                      else 2)
        if not 0 <= retries <= 5:
            raise ValueError("重试次数应在 0–5")
        timeout = float(payload.get("export_timeout") or 60)
        if not 1 <= timeout <= 600:
            raise ValueError("导出超时应在 1–600 分钟")
        delay = payload.get("delay")
        delay = None if delay in (None, "") else float(delay)
        if delay is not None and delay < 0:
            raise ValueError("间隔秒数不能为负")

        profiles = _norm_profiles(payload)
        if mode == "ui" and len(profiles) > 1:
            raise ValueError("UI 自动剪辑链路一次只能用一个预设组（底层只能生成一个草稿）")
        resolution = payload.get("export_resolution") or None
        if resolution and resolution not in ("480P", "720P", "1080P", "2K", "4K"):
            raise ValueError("导出分辨率非法")
        vocal = payload.get("vocal_mode") or "none"
        if vocal not in ("none", "vocal", "background"):
            raise ValueError("人声模式非法")
        cb = str(payload.get("cover_fallback") or "").strip().lower()
        cover_fallback = "source" if cb in ("1", "true", "source", "yes", "on") else "abort"

        return {
            "mode": mode, "series_dir": series, "selection": sel_norm,
            "cut": cut, "cover_image": cover, "cover_source": cover_source,
            "profiles": profiles,
            "profile": profiles[0] if profiles else "",
            "cover_fallback": cover_fallback,
            "export": bool(payload.get("export", True)),
            "draft_prefix": prefix, "retries": retries,
            "export_timeout": timeout, "delay": delay,
            "export_resolution": resolution if mode == "ui" else None,
            "vocal_mode": vocal if mode == "ui" else "none",
            "normalize": bool(payload.get("normalize")) if mode == "ui" else False,
        }

    @staticmethod
    def build_command(params, run_id=None):
        repo = paths.REPO
        series = params["series_dir"]
        sel = params["selection"]
        prefix = params["draft_prefix"] or os.path.basename(series.rstrip("\\/"))

        if params["mode"] == "batch":
            cmd = [sys.executable, "-u", os.path.join(repo, "pipeline", "batch_runner.py"), series]
            if sel["mode"] == "range":
                cmd += ["--only", f"{sel['from']}..{sel['to']}"]
            elif sel["mode"] == "first":
                cmd += ["--limit", str(sel["n"])]
            if params["cut"] is not None:
                cmd += ["--cut", str(params["cut"])]
            if params["cover_image"]:
                cmd += ["--cover-image", params["cover_image"]]
            if params["profiles"]:
                cmd += ["--profile", ",".join(params["profiles"])]
            if params.get("cover_fallback") == "source" and len(params["profiles"]) > 1:
                cmd += ["--cover-fallback", "source"]
            if not params["export"]:
                cmd += ["--no-export"]
            cmd += ["--retries", str(params["retries"])]
            cmd += ["--export-timeout", str(params["export_timeout"])]
            if params["draft_prefix"]:
                cmd += ["--draft-prefix", params["draft_prefix"]]
            if params["delay"] is not None:
                cmd += ["--delay", str(params["delay"])]
            cmd += ["--no-watch"]
            return cmd, ""

        # UI 自动剪辑链路
        eps, idxs = status.select_episodes(series, sel)
        if not eps:
            raise ValueError("选区没有匹配到视频")
        draft = status.derive_draft_name(prefix, eps)
        workdir = os.path.join(paths.WORK_ROOT, run_id or "preview")
        cmd = [sys.executable, "-u", os.path.join(repo, "pipeline_ui", "ui_pipeline.py"),
               "--workdir", workdir, "--video-dir", series, "--draft-name", draft,
               "--vocal-mode", params["vocal_mode"]]
        if params["export_resolution"]:
            cmd += ["--export-resolution", params["export_resolution"]]
        if params["normalize"]:
            cmd += ["--normalize"]
        if params["cut"] is not None:
            cmd += ["--cut", str(params["cut"])]
        if params["cover_image"]:
            cmd += ["--cover-image", params["cover_image"]]
        if params["profiles"]:
            cmd += ["--profile", params["profiles"][0]]
        if sel["mode"] == "range":
            cmd += ["--video-range", f"{idxs[0]}-{idxs[-1]}"]
        elif sel["mode"] == "first":
            cmd += ["--limit", str(sel["n"])]
        return cmd, draft

    @staticmethod
    def preview(payload):
        params = Runner.normalize(payload)
        cmd, draft = Runner.build_command(params)
        return {"cmd": subprocess.list2cmdline(cmd), "draft_name": draft,
                "mode": params["mode"], "profiles": params["profiles"]}

    # ── 启动 / 停止 ─────────────────────────────────────────────────
    def start(self, payload):
        params = self.normalize(payload)
        with self._lock:
            if self._current and self._current["status"] in ("queued", "running"):
                raise ValueError("已有任务在跑，P0 单队列不并发")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        cmd, draft = self.build_command(params, run_id)
        run = {
            "id": run_id, "status": "queued", "mode": params["mode"],
            "series_dir": params["series_dir"], "params": params,
            "cover_source": params.get("cover_source", "auto"),
            "cover_image": params.get("cover_image"),
            "draft_name": draft or None, "stage": None, "percent": None,
            "progress_basis": "无估算", "error_class": None, "error_line": None,
            "exit_code": None, "started_at": None, "ended_at": None,
            "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log_path": os.path.join(paths.LOG_DIR, f"{run_id}.log"),
            "cmd": subprocess.list2cmdline(cmd), "artifact": None,
            "last_line": "", "stop_requested": False,
            # 多账号：accounts 为空列表 = 单账号（保持改动前行为）
            "accounts": (_new_accounts(params["profiles"], draft)
                         if len(params["profiles"]) > 1 else []),
            "account_total": len(params["profiles"]) if len(params["profiles"]) > 1 else 0,
            "account_index": 0, "outcome": None,
            "cleaned_drafts": [],
        }
        with self._lock:
            self._current = run
            self._stop_requested = False
        self._queue.put(run)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="runner",
                                            daemon=True)
            self._worker.start()
        return self.snapshot()

    # ── 仅导出：草稿已存在，跳过建稿 / 帧对齐 / 分镜图 ───────────────
    def export_only(self, payload):
        items = payload.get("items")
        if not items:
            name = (payload.get("draft_name") or "").strip()
            if not name:
                raise ValueError("缺少 draft_name 或 items")
            items = [{"draft": name, "profile": ""}]
        norm = []
        for it in items:
            if not isinstance(it, dict):
                raise ValueError("items 中存在非法项")
            draft = (it.get("draft") or "").strip()
            if not draft:
                raise ValueError("items 中存在空草稿名")
            profile = (it.get("profile") or "").strip()
            norm.append({"draft": draft, "profile": profile})
        draft_name = norm[0]["draft"]
        profiles = []
        for it in norm:
            if it["profile"] and it["profile"] not in profiles:
                profiles.append(it["profile"])
        # 组封面缺失时的兜底：source = 复用源草稿封面；否则抽帧/中止
        cb = str(payload.get("cover_fallback") or "").strip().lower()
        cover_fallback = "source" if cb in ("1", "true", "source", "yes", "on") else "abort"
        with self._lock:
            if self._current and self._current["status"] in ("queued", "running"):
                raise ValueError("已有任务在跑，P0 单队列不并发")
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-e" + uuid.uuid4().hex[:4]
        # 读取默认 export_dir：series_runner 同路径约定 → 用 presets.json 里的 export_dir
        export_dir = payload.get("export_dir") or self._default_export_dir()
        timeout_min = float(payload.get("export_timeout") or 60)
        run = {
            "id": run_id, "status": "queued", "mode": "export_only",
            "series_dir": payload.get("series_dir") or "",
            "params": {"export": True, "draft_name": draft_name,
                       "items": norm, "batch": len(norm) > 1,
                       "export_dir": export_dir, "export_timeout": timeout_min,
                       "profiles": profiles or ["default"],
                       "cover_fallback": cover_fallback,
                       "mode": "export_only"},
            "cover_source": "", "cover_image": "",
            "draft_name": draft_name,
            "stage": {"key": "export", "label": "导出", "percent": 0}, "percent": 0,
            "progress_basis": "串行导出", "error_class": None, "error_line": None,
            "exit_code": None, "started_at": None, "ended_at": None,
            "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "log_path": os.path.join(paths.LOG_DIR, f"{run_id}.log"),
            "cmd": f"export_only(draft={draft_name})", "artifact": None,
            "last_line": "", "stop_requested": False,
            "accounts": [], "account_total": 0, "account_index": 0,
            "drafts": [{"draft": it["draft"], "profile": it["profile"],
                        "status": "pending", "percent_value": None,
                        "rate_mbps": None, "eta_s": None, "exported_mb": None,
                        "total_mb": None, "artifact": None, "seconds": None}
                       for it in norm],
            "outcome": None, "cleaned_drafts": [],
        }
        with self._lock:
            self._current = run
            self._stop_requested = False
        self._queue.put(run)
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="runner",
                                            daemon=True)
            self._worker.start()
        return self.snapshot()

    @staticmethod
    def _default_export_dir():
        """从 pipeline/presets.json 读 export_dir，兜底 pipeline/_artifacts。"""
        presets_path = os.path.join(paths.REPO, "pipeline", "presets.json")
        try:
            if os.path.isfile(presets_path):
                with open(presets_path, encoding="utf-8") as f:
                    cfg = json.load(f)
                ed = cfg.get("export_dir")
                if ed and os.path.isdir(ed):
                    return ed
        except Exception:
            pass
        return os.path.join(paths.REPO, "pipeline", "_artifacts")

    def stop(self):
        with self._lock:
            run = self._current
            proc = self._proc
            if not run or run["status"] not in ("queued", "running"):
                return {"stopped": False, "reason": "没有在跑的任务"}
            self._stop_requested = True
            run["stop_requested"] = True
            pid = proc.pid if proc else None
        if pid:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=20,
                               creationflags=_NO_WINDOW)
            except Exception as e:
                return {"stopped": False, "reason": f"终止失败: {e}"}
        return {"stopped": True, "pid": pid}

    # ── 工作线程 ────────────────────────────────────────────────────
    def _loop(self):
        while True:
            run = self._queue.get()
            if run is None:
                return
            try:
                self._execute(run)
            except Exception as e:  # 兜底：不让线程死掉
                with self._lock:
                    run["status"] = "failed"
                    run["error_class"] = "runner_internal"
                    run["error_line"] = str(e)
                    run["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._record(run)

    def _execute(self, run):
        if run["mode"] == "export_only":
            self._execute_export_only(run)
            return
        paths.ensure_runtime_dirs()
        params = run["params"]
        cmd, _ = self.build_command(params, run["id"])
        with self._lock:
            run["status"] = "running"
            run["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = subprocess.Popen(cmd, cwd=paths.REPO, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", bufsize=1,
                                    env=env, creationflags=_NO_WINDOW)
        except Exception as e:
            with self._lock:
                run["status"] = "failed"
                run["error_class"] = "spawn_failed"
                run["error_line"] = str(e)
                run["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                run["exit_code"] = -1
            self._record(run)
            return
        with self._lock:
            self._proc = proc
            run["_pid"] = proc.pid
        log = open(run["log_path"], "a", encoding="utf-8", errors="replace")
        try:
            log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} {run['cmd']}\n")
            log.flush()
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                try:
                    log.write(line + "\n")
                    log.flush()
                except OSError:
                    pass
                self._handle_line(run, line)
            rc = proc.wait()
        finally:
            try:
                log.close()
            except OSError:
                pass
            with self._lock:
                self._proc = None
        self._finalize(run, rc)

    def _handle_line(self, run, line):
        with self._lock:
            run["last_line"] = line[:500]
            for pat in _DRAFT_PATTERNS:
                m = pat.search(line)
                if m:
                    name = m.group(1).strip()
                    run["draft_name"] = name
                    accs = run.get("accounts") or []
                    if len(accs) > 1 and not accs[0].get("draft"):
                        accs[0]["draft"] = name
                        accs[0]["status"] = "building"
                        # 克隆目标名可预测：<源草稿>_<组名>（与 preset_ops 命名一致）
                        for a in accs[1:]:
                            if not a.get("draft"):
                                a["draft"] = f"{name}_{a['profile']}"
                    break
            for key, label, pct, pats in stages_for(run.get("mode")):
                if any(p in line for p in pats):
                    run["stage"] = {"key": key, "label": label, "percent": pct}
                    break
            self._track_injection(run, line)
            m = _EXPORT_PATH_RE.search(line)
            if m:
                run["artifact"] = m.group(1).strip()
            self._track_accounts(run, line)
            low = line.lower()
            if ("traceback" in low or line.startswith("✗")
                    or "[错误]" in line or "❌" in line or "失败:" in line):
                run["error_line"] = line[:300]

    # ── 多账号日志 → 每个账号的状态 / 草稿 / 成品 / 耗时 ──────────────
    @staticmethod
    def _stamp_export_time(acc):
        t0 = acc.pop("_t0", None)
        if t0 and acc.get("export_s") is None:
            acc["export_s"] = round(time.time() - t0, 1)

    @staticmethod
    def _track_injection(run, line):
        """串流建稿：逐集推进「转封装 + 注入主轨」进度。

        这一步会为每集按需重编码成 __jy_norm__.mp4，整季耗时远超其它阶段；
        不跟踪的话阶段会一直停在注入开始前的配置回显上。
        """
        if run.get("mode") == "ui":
            return
        m = _INJECT_TOTAL_RE.search(line)
        if m:
            run["_inject_total"] = int(m.group(1))
            run["_inject_done"] = 0
        if "📼" not in line:
            return
        total = int(run.get("_inject_total") or 0)
        done = int(run.get("_inject_done") or 0) + 1
        run["_inject_done"] = done
        pct = min(7, max(1, int(round(7.0 * done / total)))) if total else 3
        label = f"建稿 · 注入视频 {done}/{total}" if total else f"建稿 · 注入视频 {done}"
        run["stage"] = {"key": "build", "label": label, "percent": pct}

    @classmethod
    def _fail_account(cls, run, draft=None, artifact=None, reason=None):
        acc = _account_by(run, draft=draft, artifact=artifact)
        if not acc or acc["status"] == "ok":
            return
        cls._stamp_export_time(acc)
        acc["status"] = "failed"
        if reason:
            acc["reason"] = reason

    @classmethod
    def _track_accounts(cls, run, line):
        """单账号时直接返回，保证改动前的行为完全不变。"""
        accounts = run.get("accounts") or []
        if len(accounts) < 2:
            return
        total = run.get("account_total") or len(accounts)

        m = _CLONE_OK_RE.search(line)
        if m:
            draft, prof = m.group(1).strip(), m.group(2).strip().lower()
            acc = _account_by(run, profile=prof) or _account_by(run, draft=draft)
            if acc:
                acc["draft"] = draft
                acc["status"] = "cloned"
            done = sum(1 for a in accounts
                       if a["status"] in ("cloned", "exporting", "ok"))
            run["stage"] = {"key": "export",
                            "label": f"账号草稿克隆 · {prof}（{max(1, done)}/{total}）",
                            "percent": 90}
            return

        m = _CLONE_FAIL_RE.search(line)
        if m:
            prof, err = m.group(1).strip().lower(), m.group(2).strip()
            acc = _account_by(run, profile=prof)
            if acc:
                acc["status"] = "failed"
                acc["reason"] = "克隆草稿失败：" + (err or "未知原因")
            return

        m = _SEQ_EXPORT_RE.search(line)
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            draft, out = m.group(3).strip(), m.group(4).strip()
            prev_idx = run.get("account_index") or 0
            if 0 < prev_idx <= len(accounts):
                prev = accounts[prev_idx - 1]
                if prev["status"] == "exporting":
                    prev["status"] = "ok"  # 已切到下一份 ⇒ 上一份成功
                    cls._stamp_export_time(prev)
            run["account_index"] = i
            acc = (_account_by(run, draft=draft)
                   or (accounts[i - 1] if 1 <= i <= len(accounts) else None))
            if acc:
                acc["draft"] = draft
                acc["artifact"] = out
                acc["status"] = "exporting"
                acc["_t0"] = time.time()
            label = f"导出 · 账号 {i}/{n}（{acc['profile'] if acc else '?'}）"
            run["stage"] = {"key": "export", "label": label,
                            "percent": _export_percent(i, n, 0)}
            run["percent"] = _export_percent(i, n, 0)
            run["progress_basis"] = f"串行导出 {i}/{n}（按账号折算）"
            return

        if "导出超时" in line:
            cls._fail_account(run, draft=line.rsplit(":", 1)[-1].strip(),
                              reason="导出超时")
            return

        m = _EXPORT_EXC_RE.search(line)
        if m:
            cls._fail_account(run, draft=m.group(1).strip(),
                              reason="导出异常：" + (m.group(2).strip() or "未知"))
            return

        m = _MISSING_MP4_RE.search(line)
        if m:
            cls._fail_account(run, artifact=m.group(1).strip(),
                              reason="导出成功但成品未落盘")
            return

        m = _SEQ_FAIL_LIST_RE.search(line)
        if m:
            for dn in re.split(r"[、,，]\s*", m.group(1)):
                cls._fail_account(run, draft=dn.strip(),
                                  reason="导出失败（详见日志）")

    def _execute_export_only(self, run):
        """直接调用 batch_runner._export_drafts_sequential；
        进度通过 run.stage / run.percent / run.artifact 回写。"""
        try:
            import batch_runner  # runtime import：避免启动阶段拉整条链
        except Exception as e:
            with self._lock:
                run["status"] = "failed"
                run["error_class"] = "import_failed"
                run["error_line"] = f"无法导入 batch_runner: {e}"
                run["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                run["exit_code"] = -1
            self._record(run)
            return
        params = run["params"]
        items = params.get("items") or [{"draft": params["draft_name"], "profile": ""}]
        export_dir = params["export_dir"]
        timeout_min = float(params.get("export_timeout") or 60)
        total = max(1, len(items))
        cover_fallback = params.get("cover_fallback") or "abort"
        artifacts, failed = [], []
        log = open(run["log_path"], "w", encoding="utf-8", errors="replace")
        try:
            with self._lock:
                run["status"] = "running"
                run["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            log.write(f"# export_only  items={len(items)}  export_dir={export_dir}\n")
            log.flush()
            for idx, it in enumerate(items):
                draft = it["draft"]
                profile = it.get("profile") or ""
                if run.get("stop_requested"):
                    break
                label = f"导出 {draft}" + (f"（换预设 {profile}）" if profile else "")
                base = int(idx * 100 / total)
                with self._lock:
                    run["stage"] = {"key": "export",
                                    "label": f"{label} [{idx + 1}/{total}]",
                                    "percent": base + 2}
                    run["stage_label"] = f"导出 {idx + 1}/{total} · {draft}"
                    run["percent"] = base + 2
                    if idx < len(run.get("drafts") or []):
                        run["drafts"][idx]["status"] = "exporting"
                t0 = time.time()
                log.write(f"\n# [{idx + 1}/{total}] {label}\n")
                log.flush()
                if profile:
                    ok, out_mp4 = self._rebase_then_export(
                        draft, profile, timeout_min, log, cover_fallback)
                else:
                    try:
                        results = batch_runner._export_drafts_sequential(
                            [draft], export_dir, timeout_min=timeout_min, verify=True)
                    except Exception as e:
                        log.write(f"[error] _export_drafts_sequential 抛错: {e}\n")
                        log.flush()
                        results = {}
                    log.write(f"# 导出结果: {results}\n")
                    log.flush()
                    ok = bool(results.get(draft))
                    try:
                        from export_config import export_mp4_path
                        out_mp4 = export_mp4_path(draft, export_dir) if ok else None
                    except Exception:
                        out_mp4 = None
                if ok:
                    artifacts.append(out_mp4)
                else:
                    failed.append(draft)
                with self._lock:
                    run["percent"] = int((idx + 1) * 100 / total)
                    run["artifact"] = out_mp4 or run.get("artifact")
                    run["artifacts"] = list(artifacts)
                    if idx < len(run.get("drafts") or []):
                        d = run["drafts"][idx]
                        d["status"] = "ok" if ok else (
                            "stopped" if run.get("stop_requested") else "failed")
                        d["artifact"] = out_mp4 or None
                        d["seconds"] = round(time.time() - t0, 1)
            stopped = bool(run.get("stop_requested"))
            ok_all = (not failed) and (not stopped)
            with self._lock:
                run["artifact"] = artifacts[-1] if artifacts else None
                run["artifacts"] = list(artifacts)
                run["percent"] = 100 if not stopped else (run.get("percent") or 0)
                run["stage"] = {"key": "export",
                                "label": "导出完成" if ok_all else "导出结束",
                                "percent": run["percent"]}
                run["status"] = "stopped" if stopped else ("succeeded" if ok_all else "failed")
                run["outcome"] = "ok" if ok_all else "fail"
                run["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                run["exit_code"] = 0 if ok_all else 1
                # 停止时还没轮到的草稿标「已停止」，别让 UI 一直停在「待导出」
                for d in run.get("drafts") or []:
                    if stopped and d.get("status") == "pending":
                        d["status"] = "stopped"
                if failed:
                    run["error_class"] = "export_failed"
                    run["error_line"] = "导出失败：" + "、".join(failed)
        finally:
            try: log.close()
            except Exception: pass
            self._record(run)

    def _rebase_then_export(self, draft, profile, timeout_min, log,
                            cover_fallback="abort"):
        """换预设导出：rebase_presets.py 克隆草稿 → 换预设组 → 立即导出。
        导出根由 presets.json 的 export_dir 决定，不受本次自定义目录影响。
        cover_fallback=source 时，组封面缺失则复用源草稿封面（默认：抽帧/中止）。"""
        try:
            from export_config import preset_profile_name
            suffix = preset_profile_name(profile) or "copy"
        except Exception:
            suffix = profile
        new_draft = f"{draft}_{suffix}"
        script = os.path.join(paths.REPO, "pipeline", "rebase_presets.py")
        cmd = [sys.executable, script, draft, "--profile", profile,
               "--export", "--overwrite"]
        if cover_fallback == "source":
            cmd += ["--cover-fallback", "source"]
        log.write(f"# rebase 命令: {' '.join(cmd)}\n")
        log.flush()
        try:
            proc = subprocess.run(cmd, cwd=paths.REPO, stdout=log,
                                  stderr=subprocess.STDOUT,
                                  timeout=max(60, int(timeout_min * 60)),
                                  creationflags=_NO_WINDOW)
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            log.write("# rebase 超时\n")
            log.flush()
            ok = False
        out_mp4 = None
        if ok:
            try:
                from export_config import export_mp4_path
                out_mp4 = export_mp4_path(new_draft, self._default_export_dir())
            except Exception:
                out_mp4 = None
        return ok, out_mp4

    def _finalize(self, run, rc):
        with self._lock:
            stop_req = self._stop_requested or run.get("stop_requested")
            run["exit_code"] = rc
            run["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            if stop_req:
                run["status"] = "stopped"
            elif rc == 0:
                run["status"] = "succeeded"
                run["stage"] = {"key": "done", "label": "完成", "percent": 100}
            else:
                run["status"] = "failed"
                run["error_class"] = run["error_class"] or "subprocess_exit_nonzero"
            self._close_accounts(run, rc, stop_req)
            run["outcome"] = self._outcome(run)
            run["percent"] = self._percent(run)
            run["progress_basis"] = self._basis(run)
            draft = run.get("draft_name")
        self._cleanup_lock(run)
        self._cleanup_partial_accounts(run)
        if draft:
            prog = status.read_progress(draft)
            if prog and prog.get("final_mb"):
                # 取成品 mp4（output），不能取 _path——那是进度文件自身
                run["artifact"] = run.get("artifact") or prog.get("output")
        self._record(run)

    # ── 多账号收尾 ──────────────────────────────────────────────────
    @classmethod
    def _close_accounts(cls, run, rc, stopped):
        accounts = run.get("accounts") or []
        if len(accounts) < 2:
            return
        for a in accounts:
            cls._stamp_export_time(a)
            a.pop("_t0", None)
            if stopped and a["status"] in ("exporting", "cloning"):
                a["status"] = "stopped"
            elif a["status"] == "exporting":
                if rc == 0:
                    a["status"] = "ok"
                else:
                    a["status"] = "failed"
                    a["reason"] = a.get("reason") or f"导出进程退出码 {rc}"
            elif not stopped and rc == 0 and a["status"] in ("pending", "building") \
                    and a.get("draft"):
                a["status"] = "cloned"  # --no-export：草稿已就绪但未导出

    @staticmethod
    def _outcome(run):
        """整体结论三态：all_ok / partial / all_failed（停止单独一态）。"""
        accounts = run.get("accounts") or []
        if len(accounts) < 2:
            return None
        if run.get("status") == "stopped":
            return "stopped"
        ok = sum(1 for a in accounts if a["status"] in ("ok", "cloned"))
        if ok == len(accounts):
            return "all_ok"
        return "all_failed" if ok == 0 else "partial"

    def _cleanup_partial_accounts(self, run):
        """只清理「克隆到一半」的账号草稿；完整草稿一律保留（避免误删成品/用户草稿）。"""
        accounts = run.get("accounts") or []
        if len(accounts) < 2:
            return
        root = paths.known_roots().get("projects") or ""
        if not root:
            return
        base = accounts[0].get("draft") or ""
        removed = []
        for a in accounts[1:]:
            if a.get("status") not in ("pending", "building", "stopped"):
                continue
            dn = a.get("draft")
            if not dn or dn == base or os.sep in dn or "/" in dn:
                continue
            p = os.path.join(root, dn)
            if not (os.path.isdir(p) and paths.within(p, root)):
                continue
            valid = False
            try:
                with open(os.path.join(p, "draft_content.json"), encoding="utf-8") as f:
                    json.load(f)
                valid = True
            except (OSError, ValueError):
                valid = False
            if valid:
                a["status"] = "cloned"
                a["artifact"] = None  # 导出中断，成品不存在；避免历史里显示成已出片
                a["reason"] = a.get("reason") or "停止时副本已完整，已保留"
                continue
            try:
                shutil.rmtree(p)
                removed.append(dn)
                a["status"] = "cleaned"
            except OSError as e:
                a["reason"] = f"半成品草稿清理失败: {e}"
        if removed:
            run["cleaned_drafts"] = removed

    def _cleanup_lock(self, run):
        """只清理「本任务子进程自己持有、且进程已死」的陈旧锁，其它情况一律不动。"""
        try:
            import export_lock as el
            data = el.read_lock() or {}
            pid = int(data.get("pid") or 0)
            if pid and pid == run.get("_pid"):
                if status.pid_state(pid) == "dead":
                    os.remove(el._LOCK_PATH)
                    run["lock_cleaned"] = True
        except Exception:
            pass

    def _record(self, run):
        rec = {k: v for k, v in run.items() if not k.startswith("_")}
        try:
            paths.ensure_runtime_dirs()
            with open(paths.RUNS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── 状态 ────────────────────────────────────────────────────────
    @staticmethod
    def _percent(run):
        if run["status"] == "succeeded":
            return 100
        st = run.get("stage")
        pct = st.get("percent") if st else None
        floor = run.get("_pct_floor")
        if isinstance(floor, (int, float)) and (pct is None or pct < floor):
            pct = round(floor, 1)
        return pct

    @staticmethod
    def _basis(run):
        if run["status"] == "succeeded":
            return "完成"
        if run.get("_basis_live"):
            return run["_basis_live"]
        st = run.get("stage")
        if not st:
            return "无估算"
        if st["key"] == "export":
            if (run.get("account_total") or 0) > 1:
                return f"串行导出 {run.get('account_index') or 1}/{run['account_total']}（按账号折算）"
            return "阶段权重（导出进度未落盘）"
        return "阶段权重（阶段内不插值）"

    @staticmethod
    def _eta(run, prog, workdir):
        """当前阶段剩余秒数：优先瞬时速率（≥3 集样本），退回历史每集基线。"""
        stage = prog.get("stage")
        total = int(prog.get("total") or 0)
        done = int(prog.get("done") or 0)
        if run.get("_eta_stage") != stage:
            run["_eta_stage"] = stage
            run["_eta_t0"] = None
            run["_eta_done0"] = None
            run["eta_s"] = None
        if total <= 0 or done >= total:
            return run.get("eta_s")
        now = time.time()
        if run.get("_eta_t0") is None:
            run["_eta_t0"] = now
            run["_eta_done0"] = done
        prev = run.get("eta_s")
        delta = done - int(run["_eta_done0"] or 0)
        elapsed = now - run["_eta_t0"]
        if delta >= 3 and elapsed > 0:
            eta = elapsed / delta * (total - done)
            if isinstance(prev, (int, float)) and prev > 0:
                if eta > prev * 1.2:
                    return prev
                eta = prev * 0.6 + eta * 0.4
            return round(eta, 1)
        if isinstance(prev, (int, float)):
            return prev
        per = _hist_per_ep(workdir, stage)
        if per:
            return round(per * (total - done), 1)
        return None

    def snapshot(self):
        with self._lock:
            run = json.loads(json.dumps(self._current)) if self._current else None
            qsize = self._queue.qsize()
        if not run:
            return {"run": None, "queue": 0}
        if run.get("mode") == "export_only":
            # 仅导出没有剪辑阶段：进度=各草稿串行导出的整体完成度，当前草稿细看 percent_value
            return {"run": self._snapshot_export_only(run), "queue": qsize}
        multi = (run.get("account_total") or 0) > 1
        accounts = run.get("accounts") or []
        idx = run.get("account_index") or 0
        active = accounts[idx - 1] if 0 < idx <= len(accounts) else None
        target = (active or {}).get("draft") if multi else run.get("draft_name")
        prog = (status.read_progress(target, [os.path.join(paths.WORK_ROOT, run["id"])])
                if target else None)
        if prog:
            run["progress_file"] = prog.get("_path")
            if prog.get("status"):
                run["export_status"] = prog.get("status")
            pv = prog.get("percent_value")
            if run["status"] == "running" and isinstance(pv, (int, float)):
                if multi and active:
                    run["percent"] = _export_percent(idx, run["account_total"],
                                                     float(pv) / 100.0)
                    run["progress_basis"] = (f"串行导出 {idx}/{run['account_total']}"
                                             f"（{active.get('profile')}）")
                else:
                    run["percent"] = min(99, int(90 + float(pv) / 10))
                    run["progress_basis"] = "导出 percent_value"
            if prog.get("failure_class"):
                run["error_class"] = prog.get("failure_class")
            run["export"] = _export_view(prog)
        if run["status"] == "running" and (run.get("stage") or {}).get("key") != "export":
            # 导出前用 pipeline 写的 _progress.json 出数字（B 方案）；
            # 导出阶段 percent 完全交给上面的 percent_value 逻辑（A 兜底）。
            workdir = (os.path.join(paths.WORK_ROOT, run["id"])
                       if run.get("mode") == "ui"
                       else status._default_workdir(run["series_dir"])
                       if run.get("series_dir") else "")
            pprog = status.read_pipeline_progress(workdir)
            pct = status.progress_percent(pprog) if pprog else None
            if pct is not None:
                stage_pct = (run.get("stage") or {}).get("percent")
                if isinstance(stage_pct, (int, float)):
                    pct = max(float(pct), float(stage_pct))
                floor = run.get("_pct_floor") or 0
                pct = max(float(floor), float(pct))
                run["_pct_floor"] = pct
                run["percent"] = round(pct, 1)
                label = pprog.get("label") or ""
                total = int(pprog.get("total") or 0)
                done = int(pprog.get("done") or 0)
                run["stage_label"] = label
                run["_basis_live"] = f"{label} {done}/{total}" if total else (label or "阶段权重")
                run["progress_basis"] = run["_basis_live"]
                run["eta_s"] = self._eta(run, pprog, workdir)
        if run["status"] == "running":
            # snapshot 每次都是深拷贝，进度/ETA 的单调状态必须写回 _current，
            # 否则下一次快照会丢掉 _pct_floor 与 ETA 基准。
            with self._lock:
                cur = self._current
                if cur and cur.get("id") == run.get("id"):
                    for k in ("percent", "progress_basis", "stage_label", "eta_s",
                              "_pct_floor", "_basis_live", "_eta_stage",
                              "_eta_t0", "_eta_done0"):
                        if k in run:
                            cur[k] = run[k]
        run["job"] = status.job_info(run["series_dir"]) if run.get("series_dir") else None
        return {"run": run, "queue": qsize}

    def _snapshot_export_only(self, run):
        """仅导出：逐草稿串行，整体 percent = 已完成稿数 + 当前稿百分比。

        当前草稿的百分比/速率/剩余体积直接取导出侧 `.progress.json`；
        没轮到的草稿保持 pending（前端显示「待导出」）。
        """
        drafts = run.get("drafts") or []
        if not drafts and run.get("draft_name"):
            drafts = [{"draft": run["draft_name"], "profile": "", "status": "pending"}]
        total = max(1, len(drafts))
        cur = next((d for d in drafts if d.get("status") == "exporting"), None)
        # 结束后也回读成品目录里的进度文件：历史记录能保留流程日志
        look = cur or (drafts[-1] if run.get("status") in
                       ("succeeded", "failed", "stopped") else None)
        prog = (status.read_progress(look["draft"],
                                     [os.path.join(paths.WORK_ROOT, run["id"])])
                if look and look.get("draft") else None)
        if prog and cur:
            view = _export_view(prog)
            for k in ("percent_value", "rate_mbps", "eta_s", "exported_mb", "total_mb"):
                if view.get(k) is not None:
                    cur[k] = view.get(k)
            run["export"] = view
            if run.get("status") == "running":
                run["percent"] = round(
                    100.0 * (sum(1 for d in drafts if d.get("status") == "ok")
                             + max(0.0, min(1.0, float(view.get("percent_value") or 0) / 100.0)))
                    / total, 1)
                run["stage_label"] = (f"导出 {drafts.index(cur) + 1}/{total} · {cur['draft']}")
                if view.get("total_mb"):
                    run["progress_basis"] = (f"{view.get('exported_mb')}"
                                             f"/{view.get('total_mb')} MB")
                run["eta_s"] = view.get("eta_s")
        if run.get("status") == "succeeded":
            run["percent"] = 100.0
        # 实时日志：导出侧 .progress.json 的 flow 就是流程节点流水，取末尾几条给前端；
        # 运行中再补一行当前体积/速率，避免最后一条节点停住时看起来「没有输出」。
        log_lines = []
        if prog:
            for f in (prog.get("flow") or [])[-8:]:
                if f.get("step"):
                    log_lines.append(f"[{f.get('t')}] {f['step']}")
            if run.get("status") == "running" and not prog.get("done"):
                live = []
                if prog.get("exported_mb") is not None:
                    live.append(f"已导出 {_mb_text(prog.get('exported_mb'))}")
                if prog.get("rate_mbps"):
                    live.append(f"{prog['rate_mbps']} MB/s")
                if prog.get("elapsed_s"):
                    s = int(prog["elapsed_s"])
                    live.append(f"已用 {s // 60}m {s % 60}s")
                if live:
                    log_lines.append(f"[{time.strftime('%H:%M:%S')}] " + " · ".join(live))
        if log_lines:
            run["log_lines"] = log_lines
            run["last_line"] = log_lines[-1]
        elif not run.get("last_line") and run.get("log_path"):
            # 没有进度文件（例如预检就失败）时退回日志文件末尾一行
            try:
                with open(run["log_path"], "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 4096))
                    tail = f.read().decode("utf-8", "ignore").strip().splitlines()
                if tail:
                    run["last_line"] = tail[-1][:300]
            except OSError:
                pass
        run["drafts"] = drafts
        run["stage"] = {"key": "export",
                        "label": run.get("stage_label") or "导出",
                        "percent": run.get("percent") or 0}
        return run

    # ── 历史 ────────────────────────────────────────────────────────
    @staticmethod
    def history(limit=200):
        if not os.path.isfile(paths.RUNS_FILE):
            return []
        rows = []
        try:
            with open(paths.RUNS_FILE, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return list(reversed(rows[-limit:]))

    @staticmethod
    def get_run(run_id):
        for r in Runner.history(2000):
            if r.get("id") == run_id:
                return r
        return None
