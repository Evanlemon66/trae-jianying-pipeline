"""本地 Web Console 后端（stdlib http.server，零第三方依赖）。

启动：
    python pipeline_ui/console/app.py
访问：
    http://127.0.0.1:8765/

只监听 127.0.0.1；不访问外网、不引 CDN。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import doctor
import envinit
import paths
import runner as runner_mod
import status

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUNNER = runner_mod.Runner()
PLANS = {}

_UPLOAD_MAX = 20 * 1024 * 1024
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def _cover_warnings(w, h):
    """封面校验（只提示不阻断，最终以 pipeline 行为为准）。"""
    out = []
    if h < w:
        out.append("不是竖版：宽 %d ≥ 高 %d" % (w, h))
    if h:
        target = 9.0 / 16.0
        if abs(w / h - target) / target > 0.02:
            out.append("比例 %d:%d 偏离 9:16 超过 2%%" % (w, h))
    short = min(w, h)
    if short < 720:
        out.append("短边 %dpx < 720px" % short)
    return out

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def _list_drafts(limit=800):
    """只读扫描草稿根（projects_root），返回 {name, mtime} 列表。"""
    root = paths.known_roots().get("projects") or ""
    if not root or not os.path.isdir(root):
        return []
    out = []
    try:
        with os.scandir(root) as it:
            for e in it:
                if not e.is_dir():
                    continue
                try:
                    if not any(os.path.isfile(os.path.join(e.path, f))
                               for f in ("draft_content.json", "draft_meta_info.json")):
                        continue
                    out.append({"name": e.name, "mtime": int(e.stat().st_mtime)})
                except OSError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def _presets_info():
    p = os.path.join(paths.REPO, "pipeline", "presets.json")
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {"profiles": [], "default": "", "error": "presets.json 不可读"}
    # 只有「数组」才是预设组；presets_root（剪映预设库目录）是字符串，不是组。
    profiles = [k for k, v in cfg.items()
                if (str(k).lower() == "presets"
                    or str(k).lower().startswith("presets_"))
                and isinstance(v, list)]
    return {"profiles": profiles,
            "default": cfg.get("default_preset_profile") or "",
            "names": {k: [x.get("name") for x in (cfg.get(k) or [])] for k in profiles}}


def _run_manifest_cli(args, timeout=900):
    cmd = [sys.executable, os.path.join(paths.REPO, "pipeline", "stream_manifest.py")] + args
    proc = subprocess.run(cmd, cwd=paths.REPO, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=NO_WINDOW)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or ""), cmd


_LINE_RE = re.compile(
    r"^\s*\[(计划|待搬|待删|保留|受保护|skip|冲突|错误|warn)\]\s*([^:：]+)[:：]\s*(.*)$")
_SIZE_RE = re.compile(r"\(([\d.]+\s*[KMGT]?B)\)\s*$")


def _parse_plan(action, text):
    items = {"delete": [], "move": [], "keep": [], "blocked": [], "skip": [], "notes": []}
    total = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.strip().startswith("[模式]"):
            items["notes"].append(line.strip())
            continue
        if line.strip().startswith("合计"):
            total = line.strip()
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        kind, label, rest = m.group(1), m.group(2).strip(), m.group(3).strip()
        size = ""
        sm = _SIZE_RE.search(rest)
        if sm:
            size = sm.group(1)
            rest = _SIZE_RE.sub("", rest).strip()
        item = {"label": label, "detail": rest, "size": size, "raw": line.strip()}
        if kind == "计划":
            items["move" if action == "archive" else "delete"].append(item)
        elif kind == "待搬":
            items["move"].append(item)
        elif kind == "待删":
            items["delete"].append(item)
        elif kind == "保留":
            items["keep"].append(item)
        elif kind in ("受保护", "冲突", "错误"):
            items["blocked"].append({**item, "type": kind})
        elif kind in ("skip", "warn"):
            items["skip"].append({**item, "type": kind})
    items["total"] = total
    return items


class QuietServer(ThreadingHTTPServer):
    """浏览器关页/SSE 断开是常态，不打印连接重置堆栈。"""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "JianyingConsole/0.1"
    protocol_version = "HTTP/1.1"

    # ── 基础响应 ────────────────────────────────────────────────────
    def log_message(self, fmt, *args):  # 不刷屏
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": str(msg)}, code)

    def _text(self, msg, code=404):
        body = (str(msg) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")

    def _static(self, rel):
        rel = rel.lstrip("/") or "index.html"
        p = os.path.normpath(os.path.join(paths.STATIC_DIR, rel))
        if not p.startswith(os.path.normpath(paths.STATIC_DIR) + os.sep):
            return self._err("非法路径", 404)
        if not os.path.isfile(p):
            return self._err("文件不存在", 404)
        ext = os.path.splitext(p)[1].lower()
        with open(p, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── GET ─────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            # 浏览器自动请求的图标：直接 204，避免落到「未知接口」
            if path in ("/favicon.ico", "/apple-touch-icon.png",
                        "/apple-touch-icon-precomposed.png"):
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/health":
                return self._json({"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            if path == "/api/config":
                roots = paths.known_roots()
                return self._json({
                    "roots": roots, "presets": _presets_info(),
                    "modes": [{"key": "batch", "label": "标准串流（batch_runner）"},
                              {"key": "ui", "label": "UI 自动剪辑（ui_pipeline）"}],
                    "resolutions": ["", "480P", "720P", "1080P", "2K", "4K"],
                    "stages": [{"key": s[0], "label": s[1], "percent": s[2]}
                               for s in runner_mod.STAGES],
                    "ui_stages": [{"key": s[0], "label": s[1], "percent": s[2]}
                                  for s in runner_mod.UI_STAGES],
                    "runs_file": paths.RUNS_FILE, "log_dir": paths.LOG_DIR,
                })
            if path == "/api/series":
                return self._json({"series": status.list_series(),
                                   "roots": paths.known_roots()})
            if path == "/api/drafts":
                return self._json({"drafts": _list_drafts(),
                                   "project_root": paths.known_roots().get("projects") or ""})
            if path == "/api/doctor":
                return self._json(doctor.run_all(self.server.server_address[0],
                                                 self.server.server_address[1]))
            if path == "/api/lock":
                return self._json({"lock": status.lock_diag(),
                                   "watcher": status.watcher_info()})
            if path == "/api/external":
                cur = RUNNER.snapshot().get("run") or {}
                return self._json(status.external_tasks(
                    exclude_pids=(cur.get("_pid"),) if cur.get("_pid") else ()))
            if path == "/api/run/current":
                return self._json(RUNNER.snapshot())
            if path == "/api/events":
                return self._sse()
            if path == "/api/runs":
                runs = RUNNER.history()
                drafts = {r.get("draft_name") for r in runs if r.get("draft_name")}
                return self._json({
                    "runs": runs,
                    "external": status.external_history(exclude_drafts=drafts),
                    "pipeline_runs": status.latest_runs(10),
                })
            if path.startswith("/api/runs/"):
                rid = path[len("/api/runs/"):]
                rec = RUNNER.get_run(rid)
                if not rec:
                    return self._err("记录不存在", 404)
                log = ""
                lp = rec.get("log_path")
                if lp and os.path.isfile(lp):
                    try:
                        with open(lp, encoding="utf-8", errors="replace") as f:
                            log = "".join(f.readlines()[-300:])
                    except OSError:
                        log = ""
                return self._json({"run": rec, "log_tail": log})
            if path == "/api/manifests":
                return self._json({"manifests": status.manifest_list()})
            # ── 环境初始化 ──
            if path == "/api/env/check":
                return self._json(envinit.full_report(self.server.server_address[0],
                                                      self.server.server_address[1]))
            if path == "/api/env/inventory":
                return self._json(envinit.inventory())
            if path == "/api/env/deps":
                return self._json(envinit.deps_report())
            if path == "/api/env/job":
                return self._json(envinit.job_status())
            if path == "/api/env/presets":
                return self._json(envinit.read_presets())
            if path == "/api/env/jianying":
                return self._json(envinit.redetect_jianying())
            if path == "/api/console/autostart":
                return self._json(envinit.autostart_status())
            if path.startswith("/api/"):
                return self._err("未知接口", 404)
            return self._text("Not Found", 404)
        except ValueError as e:
            return self._err(e, 400)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    # ── POST ────────────────────────────────────────────────────────
    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/upload/cover":
                return self._handle_upload_cover()
            body = self._body()
            if path == "/api/preview":
                return self._json(RUNNER.preview(body))
            if path == "/api/run/start":
                if envinit.is_busy():
                    return self._err("依赖安装进行中，请先等待完成或停止安装", 409)
                return self._json(RUNNER.start(body))
            if path == "/api/run/stop":
                return self._json(RUNNER.stop())
            if path.startswith("/api/runs/") and path.endswith("/rerun"):
                rid = path[len("/api/runs/"):-len("/rerun")].strip("/")
                rec = RUNNER.get_run(rid)
                if not rec:
                    return self._err("记录不存在", 404)
                params = rec.get("params") or {}
                if params.get("mode") == "export_only":
                    return self._json(RUNNER.export_only(params))
                return self._json(RUNNER.start(params))
            if path == "/api/run/export-only":
                items = body.get("items")
                if items:
                    if not isinstance(items, list):
                        return self._err("items 必须是数组", 400)
                    if any(not isinstance(it, dict) or not (it.get("draft") or "").strip()
                           for it in items):
                        return self._err("items 中存在空草稿名", 400)
                elif not (body.get("draft_name") or "").strip():
                    return self._err("缺少 draft_name 或 items", 400)
                return self._json(RUNNER.export_only(body))
            if path == "/api/plan":
                return self._handle_plan(body)
            if path == "/api/execute":
                return self._handle_execute(body)
            # ── 环境初始化：白名单动作（二次确认由前端负责）──
            if path == "/api/env/presets/validate":
                return self._json(envinit.validate_presets(body.get("config")))
            if path == "/api/env/presets/save":
                return self._json(envinit.save_presets(body.get("config")))
            if path == "/api/env/action":
                return self._handle_env_action(body)
            if path == "/api/console/autostart":
                return self._json(envinit.set_autostart(bool(body.get("enabled"))))
            if path.startswith("/api/"):
                return self._err("未知接口", 404)
            return self._text("Not Found", 404)
        except ValueError as e:
            return self._err(e, 400)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    # ── 封面上传 ────────────────────────────────────────────────────
    def _handle_upload_cover(self):
        raw = self.headers.get("X-Filename") or "cover.png"
        name = os.path.basename(unquote(raw).replace("\\", "/"))
        ext = os.path.splitext(name)[1].lower()
        if ext not in _IMG_EXT:
            return self._err("只支持 PNG / JPG / WEBP", 400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return self._err("文件为空", 400)
        if n > _UPLOAD_MAX:
            return self._err("图片不能超过 20 MB", 413)
        data = self.rfile.read(n)
        if len(data) != n:
            return self._err("上传不完整，请重试", 400)
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
            w, h = img.size
            fmt = (img.format or "").upper()
        except Exception:
            return self._err("不是可解析的图片文件", 400)
        if fmt not in ("PNG", "JPEG", "WEBP"):
            return self._err("只支持 PNG / JPG / WEBP", 400)
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        dest_dir = os.path.join(paths.UPLOAD_DIR, stamp)
        os.makedirs(dest_dir, exist_ok=True)
        safe = re.sub(r"[^\w.\-]+", "_", name) or ("cover" + ext)
        dest = os.path.join(dest_dir, safe)
        with open(dest, "wb") as f:
            f.write(data)
        return self._json({"path": dest, "name": name, "size": n,
                           "width": w, "height": h,
                           "warnings": _cover_warnings(w, h)})

    # ── 归档 / 清理 ─────────────────────────────────────────────────
    def _handle_plan(self, body):
        action = (body.get("action") or "").strip()
        draft = (body.get("draft") or "").strip()
        if action not in ("archive", "clean"):
            raise ValueError("action 只能是 archive 或 clean")
        if not draft:
            raise ValueError("缺少草稿名")
        args = [action, "--manual", "--job", draft, "--dry-run"]
        if action == "clean" and body.get("intermediates_only"):
            args.append("--intermediates-only")
        rc, text, cmd = _run_manifest_cli(args)
        plan = _parse_plan(action, text)
        token = uuid.uuid4().hex
        PLANS[token] = {"action": action, "draft": draft,
                        "intermediates_only": bool(body.get("intermediates_only")),
                        "created": time.time()}
        return self._json({
            "token": token, "action": action, "draft": draft, "exit_code": rc,
            "cmd": subprocess.list2cmdline(cmd), "plan": plan,
            "log": text, "expires_in": 900,
        })

    def _handle_execute(self, body):
        token = (body.get("token") or "").strip()
        p = PLANS.get(token)
        if not p:
            raise ValueError("计划不存在或已执行，请重新生成计划")
        if time.time() - p["created"] > 900:
            PLANS.pop(token, None)
            raise ValueError("计划已过期（>15 分钟），请重新生成")
        PLANS.pop(token, None)
        args = [p["action"], "--manual", "--job", p["draft"]]
        if p["action"] == "clean" and p["intermediates_only"]:
            args.append("--intermediates-only")
        rc, text, cmd = _run_manifest_cli(args)
        return self._json({"exit_code": rc, "log": text,
                           "cmd": subprocess.list2cmdline(cmd),
                           "action": p["action"], "draft": p["draft"]})

    # ── 环境初始化动作 ──────────────────────────────────────────────
    def _handle_env_action(self, body):
        action = (body.get("action") or "").strip()
        if action == "deps.install":
            run = RUNNER.snapshot().get("run")
            if run and run.get("status") in ("running", "queued"):
                return self._err("有剪辑任务在跑，请先停止或等它结束再安装依赖", 409)
            return self._json(envinit.start_install("full"))
        if action == "install.stop":
            return self._json(envinit.stop_install())
        if action == "dirs.ensure":
            return self._json(envinit.ensure_dirs())
        if action == "presets.init":
            return self._json(envinit.init_presets())
        if action == "task.register":
            return self._json(envinit.register_task(enable=body.get("enable", True)))
        if action == "task.pause":
            return self._json(envinit.pause_watcher(reason=body.get("reason") or "Console 手动暂停"))
        if action == "task.resume":
            return self._json(envinit.resume_watcher())
        if action == "task.stop":
            return self._json(envinit.request_watcher_stop(
                reason=body.get("reason") or "Console 手动停止"))
        if action == "task.stop-cancel":
            return self._json(envinit.cancel_watcher_stop())
        if action == "ollama.start":
            return self._json(envinit.start_ollama())
        if action == "config.migrate":
            try:
                res = envinit.migrate_config(write=bool(body.get("write", True)))
            except ValueError as exc:
                return self._err(str(exc), 400)
            changes = res.get("changes") or {}
            res["changes"] = [f"{k} = {v}" for k, v in changes.items()]
            res["detail"] = "迁移完成" if res["changes"] else "无需迁移"
            return self._json(res)
        return self._err(f"未知动作: {action}", 400)

    # ── SSE ─────────────────────────────────────────────────────────
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        beat = 0
        try:
            while True:
                snap = RUNNER.snapshot()
                payload = json.dumps({"type": "state", **snap}, ensure_ascii=False)
                if payload != last:
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last = payload
                beat += 1
                if beat % 15 == 0:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def _open_log():
    """启动横幅与异常落到 pipeline_ui/_console/console.log（也兼容无控制台运行）。"""
    paths.ensure_runtime_dirs()
    path = os.path.join(paths.CONSOLE_ROOT, "console.log")
    fh = open(path, "a", encoding="utf-8", buffering=1)

    def log(msg):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
        try:
            if sys.stdout:
                print(msg)
        except Exception:
            pass
        fh.write(line + "\n")
    return log, path


def main():
    ap = argparse.ArgumentParser(description="本地 Web Console（仅 127.0.0.1）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    log, log_path = _open_log()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log("✗ 只允许监听 127.0.0.1")
        return 2
    paths.ensure_runtime_dirs()
    httpd = None
    port = args.port
    for cand in range(args.port, args.port + 10):
        try:
            httpd = QuietServer((args.host, cand), Handler)
            httpd.daemon_threads = True
            port = cand
            break
        except OSError:
            continue
    if httpd is None:
        log(f"✗ 端口 {args.port}–{args.port + 9} 均被占用")
        return 1
    try:
        with open(os.path.join(paths.CONSOLE_ROOT, "server.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    log("=" * 56)
    log("  本地 Web Console 已启动")
    log(f"  访问地址: http://{args.host}:{port}/")
    log(f"  前端入口: {os.path.join(paths.STATIC_DIR, 'index.html')}")
    log(f"  运行数据: {paths.CONSOLE_ROOT}")
    log(f"  服务日志: {log_path}  pid={os.getpid()}")
    log("  仅监听本机回环")
    log("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("已退出")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
