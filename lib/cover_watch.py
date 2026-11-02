"""cover_watch.py — cover_job 落盘监听 → 本地执行链自动触发（Stage 2 之后）。

触发条件（全部满足，契约见 references/handoff-contract.md）：
  1. 04_covers/ 有 4 张图，可被自动识别为 3 横 + 1 竖（文件名可任意，宽高比/编号容错）
  2. 03_prompts/prompts.json 存在
  3. job.json 可解析
  4. titles.gate == "done"
  5. stage ∈ {titles_selected, prompts_ready, covers_ready}（pipeline 未跑）
  6. video.status != "done"（未导出，双保险）
  7. 无活锁，且相关文件静默 >= --settle 秒（防抖）

触发后执行链：validate --fix → ingest → batch_runner 串流导出（默认 --verify 自动质检）→ record-video → derive → Toast。
进度观测：Toast 通知 / cover_job/watcher.log / cover_job/06_publish/pipeline.log / status 命令 / job.json。

用法：
  python lib/cover_watch.py                                          # 读 presets.json 的 watch 段（roots 自动发现 / jobs 显式）
  python lib/cover_watch.py --root "D:/短剧"                          # 直接扫盘，自动发现所有 <剧集>/cover_job
  python lib/cover_watch.py --job "<cover_job>" [--job "<另一个>"]   # 只监听指定任务
  python lib/cover_watch.py --root "D:/短剧" --once --dry-run         # 预览这一轮会捕获哪些、为什么跳过
  python lib/cover_watch.py status --job "<cover_job>"               # 查看锁与进度

配套文件：
  <repo>/pipeline/presets.json   本机配置的 watch 段（每轮热加载，改完不用重启）：
                                 {"watch": {"roots": ["D:/短剧"], "jobs": [], "exclude": [], "settle": 30, "interval": 5, "derive": true}}
                                 roots 下的 <剧集>/cover_job 自动发现，无需逐个登记
  --config <file>                也可单独指定监听配置文件（JSON 对象，或含 watch 段）
  <cover_job>/export_config.json 豆包/用户的一句话偏好：{"cut_s": 7200, "derive": ["youtube","tiktok"]}
                                 cut_s 省略=不裁剪；derive 省略=全平台，[]=不派生
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job_manifest as jm  # noqa: E402
import export_lock  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BATCH_RUNNER = os.path.join(_REPO, "pipeline", "batch_runner.py")
sys.path.insert(0, os.path.join(_REPO, "pipeline"))
import export_config as pc  # noqa: E402  # 本机配置（presets.json）唯一读取入口
sys.path.insert(0, os.path.join(_REPO, "scripts", "utils"))
try:
    from notifier import toast  # noqa: E402
except Exception:  # 通知不可用时退化为打印，不影响主流程
    def toast(title: str, message: str, icon: str = "info") -> None:
        print(f"[toast:{icon}] {title} | {message}")

REQUIRED_COVERS = ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical")
TRIGGER_STAGES = ("titles_selected", "prompts_ready", "covers_ready")
EXTS = (".png", ".jpg", ".jpeg", ".webp")
LOCK_NAME = ".watcher.lock"
EXPORT_CONFIG_NAME = "export_config.json"


def _norm_job_dir(path: str) -> str:
    path = os.path.abspath(path)
    if os.path.isfile(path) and os.path.basename(path) == "job.json":
        path = os.path.dirname(path)
    return path


# ── 触发判定 ──────────────────────────────────────────────

def check_trigger(job_dir: str) -> tuple[bool, list[str]]:
    """触发判定：返回 (可否触发, 未满足原因列表)。"""
    reasons: list[str] = []
    # 允许任意文件名：交给 ingest 的识别逻辑判定（竖版看宽高比，横版看编号/顺序）
    found, _ = jm.detect_cover_files(job_dir, apply=False)
    for slot in (1, 2, 3, "vertical"):
        if slot not in found:
            reasons.append(f"缺图 slot={slot}")
    if not os.path.isfile(os.path.join(job_dir, "03_prompts", "prompts.json")):
        reasons.append("缺 03_prompts/prompts.json")
    try:
        job = jm.load_job(os.path.join(job_dir, "job.json"))
    except Exception as e:
        reasons.append(f"job.json 不可解析: {e}")
        return False, reasons
    if (job.get("titles") or {}).get("gate") != "done":
        reasons.append("titles.gate != done")
    stage = jm.canon_stage(job.get("stage"))
    if stage not in TRIGGER_STAGES:
        reasons.append(f"stage={job.get('stage')} 不在可触发集合")
    if (job.get("video") or {}).get("status") == "done":
        reasons.append("video 已导出（status=done）")
    return (not reasons), reasons


def last_change_ts(job_dir: str) -> float:
    """相关文件（job.json / prompts.json / 04_covers 下任意图片）的最大 mtime，用于防抖。

    04_covers 里文件名可以任意（ingest 才改名），所以这里不能只看契约名，
    必须扫目录下所有图片扩展名，否则"刚落盘的新图"不会刷新静默计时。
    """
    files = [os.path.join(job_dir, "job.json"),
             os.path.join(job_dir, "03_prompts", "prompts.json")]
    covers_dir = os.path.join(job_dir, "04_covers")
    try:
        for name in os.listdir(covers_dir):
            if os.path.splitext(name)[1].lower() in EXTS:
                files.append(os.path.join(covers_dir, name))
    except OSError:
        pass
    ts = 0.0
    for p in files:
        try:
            ts = max(ts, os.path.getmtime(p))
        except OSError:
            pass
    return ts


# ── 锁与进度 ──────────────────────────────────────────────

def lock_path(job_dir: str) -> str:
    return os.path.join(job_dir, LOCK_NAME)


def read_lock(job_dir: str) -> Optional[dict[str, Any]]:
    try:
        with open(lock_path(job_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        for line in out.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) > 1 and cols[1] == str(pid):
                return True
    except Exception:
        pass
    return False


def lock_alive(job_dir: str) -> bool:
    data = read_lock(job_dir)
    return bool(data) and _pid_alive(int(data.get("pid") or 0))


def _write_lock(job_dir: str, phase: str, pipeline_pid: Optional[int] = None) -> None:
    data = {"pid": os.getpid(), "job": job_dir, "phase": phase,
            "started": jm.now_iso(), "pipeline_pid": pipeline_pid}
    with open(lock_path(job_dir), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def acquire_lock(job_dir: str) -> bool:
    if lock_alive(job_dir):
        return False
    _write_lock(job_dir, "triggered")
    return True


def release_lock(job_dir: str) -> None:
    try:
        os.remove(lock_path(job_dir))
    except OSError:
        pass


def _log(job_dir: str, msg: str) -> None:
    line = f"{jm.now_iso()} {msg}"
    print(line)
    try:
        with open(os.path.join(job_dir, "watcher.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── export_config（豆包一句话偏好）─────────────────────────────

def _read_export_config(job_dir: str) -> dict[str, Any]:
    """读 cover_job/export_config.json；不存在返回 {}。"""
    p = os.path.join(job_dir, EXPORT_CONFIG_NAME)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as f:  # 兼容 Windows 记事本存出的 BOM
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{EXPORT_CONFIG_NAME} 应为 JSON 对象")
    return data


def apply_export_config(job_dir: str) -> Optional[list[str]]:
    """把 export_config.json 落到 job.json（cut_s 走 update_job 校验）。

    返回 derive 平台覆盖列表：None=未指定（用默认全量），[]=不派生。
    """
    cfg = _read_export_config(job_dir)
    if not cfg:
        return None
    applied: list[str] = []
    cut = cfg.get("cut_s")
    if cut is not None:
        if isinstance(cut, bool) or not isinstance(cut, (int, float)) or cut <= 0:
            raise ValueError(f"export_config.cut_s 应为正数秒: {cut!r}")
        job = jm.load_job(os.path.join(job_dir, "job.json"))
        if (job.get("video") or {}).get("cut_s") != cut:
            jm.update_job(job_dir, {"video": {"cut_s": cut}},
                          event="export_config", detail=f"cut_s={cut}")
        applied.append(f"cut_s={cut}")
    derive_override: Optional[list[str]] = None
    if "derive" in cfg:
        raw = cfg.get("derive")
        if raw is None or raw == [] or (isinstance(raw, str) and raw.strip().lower() in ("", "none")):
            derive_override = []
        else:
            names = [raw] if isinstance(raw, str) else list(raw)
            if any(str(n).strip().lower() == "all" for n in names):
                derive_override = list(jm.DERIVE_PRESETS)
            else:
                alias = {"ig": "instagram-reels", "facebook": "facebook-reels"}
                derive_override = []
                for n in names:
                    key = alias.get(str(n).strip().lower(), str(n).strip().lower())
                    if key not in jm.DERIVE_PRESETS:
                        raise ValueError(f"export_config.derive 未知平台: {n}")
                    if key not in derive_override:
                        derive_override.append(key)
        applied.append(f"derive={derive_override if derive_override else '不派生'}")
    if applied:
        _log(job_dir, "export_config: " + ", ".join(applied))
    return derive_override


def _build_cmd(series_dir: str, cut: Any, verify: bool = True) -> list[str]:
    # --force：watcher 自己持有全局导出锁，子进程不再重复抢
    cmd = [sys.executable, _BATCH_RUNNER, series_dir, "--force"]
    if isinstance(cut, (int, float)) and not isinstance(cut, bool) and cut > 0:
        cmd += ["--cut", str(cut)]
    # 质检是 batch_runner 默认行为（--verify 隐含 --wait-export，导出完成后再登记）；仅在关闭时显式传
    if not verify:
        cmd += ["--no-verify"]
    return cmd


# ── 执行链 ──────────────────────────────────────────────

def run_chain(job_dir: str, *, derive: bool, dry_run: bool, verify: bool = True) -> bool:
    """validate --fix → ingest → export_config → batch_runner 串流 → record-video → derive → Toast。"""
    series_dir = os.path.dirname(job_dir)  # cover_job 的父目录即剧集目录
    name = os.path.basename(series_dir.rstrip("\\/"))
    log_dir = os.path.join(job_dir, "06_publish")
    plog = os.path.join(log_dir, "pipeline.log")

    if dry_run:
        job = jm.load_job(os.path.join(job_dir, "job.json"))
        cut = (job.get("video") or {}).get("cut_s")
        try:
            cfg = _read_export_config(job_dir)
            if cfg.get("cut_s") is not None:
                cut = cfg["cut_s"]
        except Exception:
            pass
        print(f"[dry-run] {name}: validate --fix → ingest → export_config → "
              f"{' '.join(_build_cmd(series_dir, cut, verify))} → record-video → derive")
        return True

    os.makedirs(log_dir, exist_ok=True)
    toast("🎬 封面任务触发", f"{name}：开始本地执行链", icon="info")
    _log(job_dir, "chain start")

    # 1) validate --fix（补 hash）
    try:
        jm.backfill_hashes(job_dir)
        _log(job_dir, "validate --fix ok")
    except Exception as e:
        _log(job_dir, f"validate --fix 失败: {e}")
        toast("❌ 本地执行链失败", f"{name} validate --fix: {e}", icon="error")
        return False

    # 2) ingest（登记 + 规范化 + 推进 stage）
    try:
        rep = jm.ingest_job(job_dir)
        _log(job_dir, f"ingest ok stage={rep['stage_to']}")
    except Exception as e:
        _log(job_dir, f"ingest 失败: {e}")
        toast("❌ 本地执行链失败", f"{name} ingest: {e}", icon="error")
        return False

    # 2.5) export_config（豆包/用户偏好：裁剪秒数写 job.json、派生平台覆盖）
    try:
        derive_override = apply_export_config(job_dir)
    except Exception as e:
        _log(job_dir, f"export_config 失败: {e}")
        toast("❌ 本地执行链失败", f"{name} export_config: {e}", icon="error")
        return False

    # 3) pipeline 串流导出（封面自动探测 cover_job 竖版图；cut 从 job.json 读取）
    # 全局导出锁：拿到才准跑，拿不到本轮放弃（下轮轮询重试）
    if not export_lock.acquire(owner=f"cover_watch {name}"):
        holder = export_lock.read_lock() or {}
        _log(job_dir, f"全局导出锁被占用（pid={holder.get('pid')} "
                      f"owner={holder.get('owner')}），稍后重试")
        return False
    job = jm.load_job(os.path.join(job_dir, "job.json"))
    cmd = _build_cmd(series_dir, (job.get("video") or {}).get("cut_s"), verify)
    _log(job_dir, "pipeline start: " + " ".join(cmd))
    toast("📤 串流导出启动", f"{name}（日志: {plog}）", icon="info")
    try:
        with open(plog, "a", encoding="utf-8") as lf:
            lf.write(f"\n===== {jm.now_iso()} cover_watch 启动 =====\n")
            lf.flush()
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=_REPO)
        _write_lock(job_dir, "pipeline", pipeline_pid=proc.pid)
        rc = proc.wait()
    finally:
        export_lock.release()
    _log(job_dir, f"pipeline exit rc={rc}")
    if rc != 0:
        toast("❌ 导出失败", f"{name} rc={rc}，见 {plog}", icon="error")
        return False

    # 4) record-video（登记导出目录；失败不阻断）
    try:
        rep = jm.record_video(job_dir)
        _log(job_dir, f"record-video ok export_dir={rep['video']['export_dir']}")
    except Exception as e:
        _log(job_dir, f"record-video 失败: {e}")
        toast("⚠️ 导出完成但登记失败", f"{name} record-video: {e}", icon="warning")

    # 5) derive 平台派生图（可选；失败不阻断）
    if derive:
        platforms = derive_override if derive_override is not None else list(jm.DERIVE_PRESETS)
        if platforms:
            try:
                rep = jm.derive_covers(job_dir, platforms)
                _log(job_dir, f"derive ok: {[d['platform'] for d in rep['derived']]}")
            except Exception as e:
                _log(job_dir, f"derive 失败: {e}")
        else:
            _log(job_dir, "derive 跳过（export_config 指定不派生）")

    # 6) 完成
    job = jm.load_job(os.path.join(job_dir, "job.json"))
    export_dir = (job.get("video") or {}).get("export_dir") or "（见 pipeline.log）"
    toast("✅ mp4 导出完成", f"{name}\n{export_dir}", icon="success")
    _log(job_dir, "chain done")
    return True


# ── 命令 ──────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    for j in args.job:
        job_dir = _norm_job_dir(j)
        try:
            job = jm.load_job(os.path.join(job_dir, "job.json"))
            video = (job.get("video") or {})
            head = (f"stage={job.get('stage')} rev={job.get('revision')} "
                    f"video.status={video.get('status')} export_dir={video.get('export_dir')}")
        except Exception as e:
            head = f"job.json 不可读: {e}"
        print(f"{job_dir}\n  {head}")
        lock = read_lock(job_dir)
        if lock:
            alive = lock_alive(job_dir)
            print(f"  lock: phase={lock.get('phase')} pid={lock.get('pid')}"
                  f"（{'活' if alive else '死'}）pipeline_pid={lock.get('pipeline_pid')} "
                  f"started={lock.get('started')}")
        else:
            print("  lock: 无")
    return 0


def _load_watch_config(path: Optional[str], *, quiet: bool = False) -> dict[str, Any]:
    """监听配置来源：--config 显式文件 > pipeline/presets.json 的 watch 段。

    默认走本机唯一配置 presets.json（与本机 export_dir/deps_root 同源），
    脚本内不写死任何剧集目录；每轮重新读取即热加载。
    """
    if path:
        try:
            with open(path, encoding="utf-8-sig") as f:  # 兼容 BOM
                data = json.load(f)
        except (OSError, ValueError) as e:
            print(f"[WARN] 监听配置不可读（沿用上一轮）: {e}")
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"监听配置应为 JSON 对象: {path}")
        if isinstance(data.get("watch"), dict):  # 兼容直接指向 presets.json
            data = data["watch"]
        if not quiet:
            print(f"使用监听配置: {path}")
        return data
    data = pc.get_watch_config()
    if not quiet:
        if data:
            print(f"使用监听配置: {pc.CONFIG_PATH}（watch 段）")
        else:
            print(f"[WARN] 未配置 watch 段（{pc.CONFIG_PATH}），可用 --root/--job 指定")
    return data


def _discover_jobs(roots: list[str], exclude: list[str]) -> list[str]:
    """扫描 roots 下所有 <剧集目录>/cover_job，自动发现任务（无需预先登记）。"""
    found: list[str] = []
    ex = {str(x).replace("\\", "/").strip("/").lower() for x in exclude or []}
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            job_dir = os.path.join(root, name, "cover_job")
            if not os.path.isfile(os.path.join(job_dir, "job.json")):
                continue
            key = job_dir.replace("\\", "/").lower()
            if any(key.endswith(e) or name.lower() == e for e in ex):
                continue
            found.append(os.path.abspath(job_dir))
    return found


def _collect_jobs(args: argparse.Namespace, cfg: dict[str, Any]) -> tuple[list[str], list[str]]:
    """显式 --job / 配置 jobs + roots 自动发现，去重。"""
    explicit = args.job or cfg.get("jobs") or []
    roots = list(args.root or []) + list(cfg.get("roots") or [])
    merged = [_norm_job_dir(j) for j in explicit]
    merged += _discover_jobs(roots, cfg.get("exclude") or [])
    uniq: list[str] = []
    seen: set[str] = set()
    for j in merged:
        if j.lower() not in seen:
            seen.add(j.lower())
            uniq.append(j)
    return uniq, roots


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        cfg = _load_watch_config(getattr(args, "config", None))
    except (OSError, ValueError) as e:
        print(f"[FAIL] {e}")
        return 1
    jobs, roots = _collect_jobs(args, cfg)
    if not jobs:
        print("[FAIL] 没有可监听的任务：给 --job <cover_job>、--root <目录>，"
              "或在 pipeline/presets.json 的 watch 段配置 roots（如 {\"watch\":{\"roots\":[\"D:/短剧\"]}}）")
        return 1
    settle = args.settle if args.settle is not None else float(cfg.get("settle", 30.0))
    interval = args.interval if args.interval is not None else float(cfg.get("interval", 5.0))
    derive_on = (not args.no_derive) and bool(cfg.get("derive", True))
    verify_on = not args.no_verify
    print(f"cover_watch 监听 {len(jobs)} 个任务（静默 {settle:.0f}s 防抖，"
          f"轮询 {interval:.0f}s，roots={roots or '无'}，配置热加载）")
    done: set[str] = set()
    known: set[str] = set()
    while True:
        # 配置热加载：新增 roots/jobs 无需重启；显式 --job 始终保留
        try:
            cfg_new = _load_watch_config(getattr(args, "config", None), quiet=True)
        except (OSError, ValueError):
            cfg_new = {}
        if cfg_new:
            cfg = cfg_new
        jobs, roots = _collect_jobs(args, cfg)
        if args.settle is None:
            settle = float(cfg.get("settle", settle))
        if args.interval is None:
            interval = float(cfg.get("interval", interval))
        derive_on = (not args.no_derive) and bool(cfg.get("derive", True))
        fresh = [j for j in jobs if j.lower() not in known]
        for j in fresh:
            print(f"[new] 自动发现任务: {j}")
            known.add(j.lower())

        for job_dir in jobs:
            if job_dir in done:
                continue
            if lock_alive(job_dir):
                if args.verbose:
                    print(f"[skip] {job_dir}: 活锁存在（链在跑）")
                continue
            ok, reasons = check_trigger(job_dir)
            if not ok:
                if args.once or args.verbose:
                    print(f"[skip] {job_dir}: {'；'.join(reasons)}")
                continue
            idle = time.time() - last_change_ts(job_dir)
            if idle < settle:
                print(f"[wait] {job_dir}: 条件满足，静默确认中（{idle:.0f}/{settle:.0f}s）")
                continue
            if args.dry_run:
                run_chain(job_dir, derive=False, dry_run=True)
                done.add(job_dir)
                continue
            if not acquire_lock(job_dir):
                continue
            try:
                success = run_chain(job_dir, derive=derive_on, dry_run=False,
                                    verify=verify_on)
            except Exception as e:
                _log(job_dir, f"chain 异常: {e}")
                toast("❌ 本地执行链异常",
                      f"{os.path.basename(os.path.dirname(job_dir))}: {e}", icon="error")
                success = False
            finally:
                release_lock(job_dir)
            if success:
                done.add(job_dir)
        if args.once:
            return 0
        time.sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", action="append", default=None,
                    help="cover_job 目录或其中的 job.json（可多个）；省略时读 presets.json 的 watch 段")
    ap.add_argument("--root", action="append", default=None,
                    help="自动发现根目录（扫描 <root>/*/cover_job，可多个；无需预先登记任务）")
    ap.add_argument("--config", default=None,
                    help="监听配置 JSON（默认读 pipeline/presets.json 的 watch 段）")
    ap.add_argument("--settle", type=float, default=None, help="静默确认秒数（默认 30 或配置值）")
    ap.add_argument("--interval", type=float, default=None, help="轮询间隔秒数（默认 5 或配置值）")
    ap.add_argument("--once", action="store_true", help="只检查一轮就退出")
    ap.add_argument("--dry-run", action="store_true", help="只判定与打印，不执行")
    ap.add_argument("--no-derive", action="store_true", help="跳过平台派生图")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过导出后自动质检（默认开启：verify_export.py 帧级音画漂移+字幕帧位）")
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command")
    p_st = sub.add_parser("status", help="查看锁与进度")
    p_st.add_argument("--job", action="append", required=True)
    args = ap.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
