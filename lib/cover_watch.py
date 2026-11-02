"""cover_watch.py — cover_job 落盘监听 → 两阶段调度（剪辑 / 导出）。

触发条件（全部满足，契约见 references/handoff-contract.md）：
  1. 04_covers/ 有 4 张图（文件名可任意，ingest 自动识别改名）
  2. 03_prompts/prompts.json 存在
  3. job.json 可解析
  4. titles.gate == "done"
  5. stage ∈ {titles_selected, prompts_ready, covers_ready}（含别名映射）
  6. video.status != "done"（未导出）
  7. 无活锁，且相关文件静默 >= --settle 秒（防抖）

调度（串行，每轮只推进一部）：
  阶段推导：无串流草稿 → edit（batch_runner --no-export，含帧对齐）
            有草稿 + workflow.mode=full + 策略放行 → export（导出→质检→record-video→derive）
            mode=edit_only → 停在草稿等人工改意图
  意图来源：job.json.workflow（ingest 时由 dispatch.json 归并；人工改 job.json
            并把 workflow.intent_revision +1 即为最终意图，watcher 每轮比对采纳）
  export_policy：immediate（默认，剪完即导）/ after_edit_queue_drained（编辑队列
            清空才导出）/ manual（等 --phase export 放行）

用法：
  python lib/cover_watch.py                        # 读 presets.json 的 watch 段
  python lib/cover_watch.py --root "D:/短剧" --once --dry-run   # 预览计划
  python lib/cover_watch.py plan                   # 列表：阶段/意图/下一步/被挡原因
  python lib/cover_watch.py --hold                 # 只列计划不执行（同配置 hold:true）
  python lib/cover_watch.py --phase export --once  # 单轮强制只推进导出阶段
  python lib/cover_watch.py status --job "<cover_job>"

配套文件：
  <repo>/pipeline/presets.json  watch 段（每轮热加载）：
      {"roots":[...], "jobs":[], "exclude":[], "settle":30, "interval":5,
       "export_policy":"immediate", "hold":false, "derive":[]}
  <cover_job>/dispatch.json     豆包侧意图声明（ingest 归并进 job.json.workflow）
  <cover_job>/export_config.json 旧版一句话偏好（仍兼容：cut_s/derive）
"""
from __future__ import annotations

import argparse
import glob
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
import series_runner as sr  # noqa: E402  # 复用 _run_export（preclean+导出+归拢）
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
EXPORT_POLICIES = ("immediate", "after_edit_queue_drained", "manual")


def _norm_job_dir(path: str) -> str:
    path = os.path.abspath(path)
    if os.path.isfile(path) and os.path.basename(path) == "job.json":
        path = os.path.dirname(path)
    return path


# ── 触发判定 ──────────────────────────────────────────────

def check_trigger(job_dir: str) -> tuple[bool, list[str]]:
    """触发判定：返回 (可否触发, 未满足原因列表)。"""
    reasons: list[str] = []
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
    """相关文件（job.json / prompts.json / 04_covers 下任意图片）的最大 mtime，用于防抖。"""
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


def acquire_lock(job_dir: str, phase: str = "triggered") -> bool:
    if lock_alive(job_dir):
        return False
    _write_lock(job_dir, phase)
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


# ── 意图与计划推导 ──────────────────────────────────────────────

def _draft_root() -> str:
    return (os.environ.get("JY_PROJECTS_ROOT", "").strip()
            or os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "JianyingPro", "User Data", "Projects", "com.lveditor.draft"))


def find_stream_draft(series_name: str) -> Optional[str]:
    """定位串流草稿目录（与 series_runner._find_draft 同规则：剧名*串流，取最新）。"""
    cands = [d for d in glob.glob(os.path.join(_draft_root(), f"{series_name}*串流"))
             if os.path.isdir(d)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _intent_of(job: dict[str, Any]) -> dict[str, Any]:
    wf = dict(job.get("workflow") or {})
    for k, v in jm.WORKFLOW_DEFAULTS.items():
        wf.setdefault(k, v)
    return wf


def _effective_cut(wf: dict[str, Any], job: dict[str, Any]) -> Optional[float]:
    """裁剪解析：workflow.cut_s（意图收口）> video.cut_s（set-cut/export_config 旧路）。"""
    cut = wf.get("cut_s")
    if cut is None:
        cut = (job.get("video") or {}).get("cut_s")
    if isinstance(cut, (int, float)) and not isinstance(cut, bool) and cut > 0:
        return float(cut)
    return None


def _effective_derive(wf: dict[str, Any], cfg: dict[str, Any],
                      legacy_override: Optional[list[str]]) -> list[str]:
    """派生解析：workflow.derive > export_config 旧覆盖 > 全局 watch.derive（默认 []）。"""
    if wf.get("derive"):
        return list(wf["derive"])
    if legacy_override is not None:
        return legacy_override
    g = cfg.get("derive", [])
    if isinstance(g, str):
        g = [g]
    return list(g) if g else []


def build_plan(job_dir: str, job: dict[str, Any], cfg: dict[str, Any],
               pending_edit: int, phase_override: Optional[str] = None) -> dict[str, Any]:
    """推导单个剧的计划：{action: edit|export|blocked, reason, draft, intent...}"""
    series_dir = os.path.dirname(job_dir)
    name = os.path.basename(series_dir.rstrip("\\/"))
    wf = _intent_of(job)
    draft = find_stream_draft(name)
    policy = str(cfg.get("export_policy", "immediate")).strip().lower()
    if policy not in EXPORT_POLICIES:
        policy = "immediate"
    plan: dict[str, Any] = {"job_dir": job_dir, "name": name, "draft": draft,
                            "intent": wf, "policy": policy,
                            "action": "blocked", "reason": "", "cmd": ""}
    cut = _effective_cut(wf, job)

    def _edit_cmd() -> list[str]:
        cmd = [sys.executable, _BATCH_RUNNER, series_dir, "--force", "--no-export"]
        if cut:
            cmd += ["--cut", str(cut)]
        return cmd

    def _export_cmd() -> list[str]:
        dn = os.path.basename(draft.rstrip("\\/"))
        return [sys.executable, os.path.join(_REPO, ".trae", "skills", "jianying-editor",
                                             "examples", "robust_auto_export.py"),
                dn, pc.export_mp4_path(dn), "--move", "--fallback-dir", pc.get_export_dir()]

    if phase_override == "edit":
        plan.update(action="edit", reason="--phase edit 强制", cmd=_edit_cmd())
        return plan
    if phase_override == "export":
        if not draft:
            plan.update(reason="--phase export 强制但无草稿，先跑 edit")
            return plan
        plan.update(action="export", reason="--phase export 强制", cmd=_export_cmd())
        return plan

    if draft is None:
        plan.update(action="edit", reason="无串流草稿 → 剪辑阶段", cmd=_edit_cmd())
        return plan
    if wf.get("mode") == "edit_only":
        plan.update(reason="mode=edit_only：停在草稿，人工改 workflow.mode 后才导出")
        return plan
    if policy == "manual":
        plan.update(reason="export_policy=manual：等待 --phase export 放行")
        return plan
    if policy == "after_edit_queue_drained" and pending_edit > 0:
        plan.update(reason=f"export_policy=after_edit_queue_drained：还有 {pending_edit} 部待剪辑")
        return plan
    plan.update(action="export", reason=f"草稿就绪 + mode=full + policy={policy}",
                cmd=_export_cmd())
    return plan


def _plan_line(plan: dict[str, Any]) -> str:
    wf = plan["intent"]
    intent = (f"mode={wf.get('mode')} cut={wf.get('cut_s')} derive={wf.get('derive')} "
              f"来源={wf.get('intent_source')} rev={wf.get('intent_revision')} "
              f"优先级={wf.get('priority')}")
    head = f"[plan] {plan['name']}: {plan['action']}"
    line = f"{head}（{plan['reason']}） | 意图: {intent}"
    if plan.get("cmd"):
        line += f"\n        cmd: {' '.join(plan['cmd'])}"
    return line


# ── export_config（旧版一句话偏好，保持兼容）─────────────────────

def _read_export_config(job_dir: str) -> dict[str, Any]:
    p = os.path.join(job_dir, EXPORT_CONFIG_NAME)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{EXPORT_CONFIG_NAME} 应为 JSON 对象")
    return data


def apply_export_config(job_dir: str) -> Optional[list[str]]:
    """旧版 export_config.json：cut_s 写 job.json、derive 返回覆盖列表（可空）。"""
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
                derive_override = []
                for n in names:
                    key = jm.DERIVE_ALIASES.get(str(n).strip().lower(), str(n).strip().lower())
                    if key not in jm.DERIVE_PRESETS:
                        raise ValueError(f"export_config.derive 未知平台: {n}")
                    if key not in derive_override:
                        derive_override.append(key)
        applied.append(f"derive={derive_override if derive_override else '不派生'}")
    if applied:
        _log(job_dir, "export_config: " + ", ".join(applied))
    return derive_override


# ── 阶段执行 ──────────────────────────────────────────────

def run_edit(job_dir: str, plan: dict[str, Any], *, dry_run: bool) -> bool:
    """edit 阶段：validate --fix → ingest（含 workflow 归并）→ batch_runner --no-export。"""
    name = plan["name"]
    if dry_run:
        print(_plan_line(plan))
        return True
    toast("🎬 剪辑阶段开始", f"{name}", icon="info")
    _log(job_dir, "edit start")
    try:
        jm.backfill_hashes(job_dir)
        _log(job_dir, "validate --fix ok")
    except Exception as e:
        _log(job_dir, f"validate --fix 失败: {e}")
        toast("❌ 剪辑阶段失败", f"{name} validate --fix: {e}", icon="error")
        return False
    try:
        rep = jm.ingest_job(job_dir)
        wfr = rep.get("workflow") or {}
        if wfr.get("error"):
            _log(job_dir, f"⚠️ dispatch 归并失败（按现有意图继续）: {wfr['error']}")
            toast("⚠️ dispatch.json 非法", f"{name}: {wfr['error']}（按现有意图继续）", icon="warning")
        else:
            _log(job_dir, f"ingest ok stage={rep['stage_to']} "
                          f"intent={wfr.get('intent_source')} rev={wfr.get('intent_revision')}")
    except Exception as e:
        _log(job_dir, f"ingest 失败: {e}")
        toast("❌ 剪辑阶段失败", f"{name} ingest: {e}", icon="error")
        return False
    cmd = list(plan["cmd"])
    _log(job_dir, "pipeline start: " + " ".join(cmd))
    with open(os.path.join(job_dir, "06_publish", "pipeline.log"), "a",
              encoding="utf-8") as lf:
        lf.write(f"\n===== {jm.now_iso()} cover_watch edit 阶段 =====\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=_REPO)
    _write_lock(job_dir, "edit", pipeline_pid=proc.pid)
    rc = proc.wait()
    _log(job_dir, f"edit exit rc={rc}")
    if rc != 0:
        toast("❌ 剪辑失败", f"{name} rc={rc}，见 06_publish/pipeline.log", icon="error")
        return False
    toast("✅ 草稿就绪", f"{name}（等待导出阶段）", icon="success")
    _log(job_dir, "edit done")
    return True


def run_export(job_dir: str, plan: dict[str, Any], cfg: dict[str, Any],
               *, dry_run: bool, verify: bool = True, no_derive: bool = False) -> bool:
    """export 阶段：export_config(旧) → 导出 → 质检 → record-video → derive。"""
    name = plan["name"]
    draft_name = os.path.basename(plan["draft"].rstrip("\\/"))
    if dry_run:
        print(_plan_line(plan))
        print(f"        export: {draft_name} → {pc.export_mp4_path(draft_name)}")
        return True
    toast("📤 导出阶段开始", f"{name}", icon="info")
    _log(job_dir, "export start")
    try:
        legacy_override = apply_export_config(job_dir)
    except Exception as e:
        _log(job_dir, f"export_config 失败: {e}")
        toast("❌ 导出阶段失败", f"{name} export_config: {e}", icon="error")
        return False
    job = jm.load_job(os.path.join(job_dir, "job.json"))
    wf = _intent_of(job)
    platforms = [] if no_derive else _effective_derive(wf, cfg, legacy_override)

    if not export_lock.acquire(owner=f"cover_watch {name}"):
        holder = export_lock.read_lock() or {}
        _log(job_dir, f"全局导出锁被占用（pid={holder.get('pid')} "
                      f"owner={holder.get('owner')}），稍后重试")
        return False
    _write_lock(job_dir, "export")
    try:
        ok = sr._run_export(draft_name, timeout=1800)
    finally:
        export_lock.release()
    _log(job_dir, f"export {'ok' if ok else '失败'}")
    if not ok:
        toast("❌ 导出失败", name, icon="error")
        return False
    # 质检（与 batch_runner 默认行为一致；--no-verify 关闭）
    out_mp4 = pc.export_mp4_path(draft_name)
    verify_script = os.path.join(_REPO, "pipeline", "verify_export.py")
    if verify and os.path.isfile(verify_script):
        _log(job_dir, "verify start")
        rc = subprocess.call([sys.executable, verify_script, draft_name, out_mp4],
                             cwd=os.path.dirname(verify_script))
        _log(job_dir, f"verify rc={rc}")
        if rc != 0:
            toast("⚠️ 导出完成但质检未通过", name, icon="warning")
    try:
        rep = jm.record_video(job_dir)
        _log(job_dir, f"record-video ok export_dir={rep['video']['export_dir']}")
    except Exception as e:
        _log(job_dir, f"record-video 失败: {e}")
        toast("⚠️ 导出完成但登记失败", f"{name} record-video: {e}", icon="warning")
    if platforms:
        try:
            rep = jm.derive_covers(job_dir, platforms)
            _log(job_dir, f"derive ok: {[d['platform'] for d in rep['derived']]}")
        except Exception as e:
            _log(job_dir, f"derive 失败: {e}")
    else:
        _log(job_dir, "derive 跳过（意图/全局均为不派生）")
    job = jm.load_job(os.path.join(job_dir, "job.json"))
    export_dir = (job.get("video") or {}).get("export_dir") or "（见 pipeline.log）"
    toast("✅ mp4 导出完成", f"{name}\n{export_dir}", icon="success")
    _log(job_dir, "chain done")
    return True


def _sync_intent(job_dir: str, name: str, state: dict[str, Any]) -> None:
    """intent_revision 跟踪：变大 → 人工最终意图 → 标 manual + 重归并 + toast。"""
    try:
        job = jm.load_job(os.path.join(job_dir, "job.json"))
    except Exception:
        return
    rev = int((job.get("workflow") or {}).get("intent_revision") or 0)
    prev = state.get("rev")
    if prev is None:
        state["rev"] = rev
        return
    if rev <= prev:
        return
    try:
        jm.update_job(job_dir, {"workflow": {"intent_source": "manual"}},
                      event="intent_manual",
                      detail=f"intent_revision {prev}->{rev}")
        rep = jm.merge_workflow(job_dir, notify=toast)
        _log(job_dir, f"intent 人工更新（rev {prev}->{rev}），已按人工意图重归并 "
                      f"conflicts={len(rep['conflicts'])}")
        toast("✋ 意图被人工修改", f"{name}：采用人工意图（intent_revision={rev}）",
              icon="info")
    except Exception as e:
        _log(job_dir, f"intent 重归并失败: {e}")
    state["rev"] = rev


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
            print(f"  lock: 无")
    return 0


def _load_watch_config(path: Optional[str], *, quiet: bool = False) -> dict[str, Any]:
    """监听配置来源：--config 显式文件 > pipeline/presets.json 的 watch 段（每轮热加载）。"""
    if path:
        try:
            with open(path, encoding="utf-8-sig") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            print(f"[WARN] 监听配置不可读（沿用上一轮）: {e}")
            return {}
        if not isinstance(data, dict):
            raise ValueError(f"监听配置应为 JSON 对象: {path}")
        if isinstance(data.get("watch"), dict):
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


def _eval_round(args: argparse.Namespace, cfg: dict[str, Any],
                jobs: list[str], done: set[str],
                intent_states: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """一轮评估：意图同步 + 触发判定 + 计划推导。返回候选计划列表（未执行）。

    两遍推导：第一遍数出待剪辑队列（pending_edit），第二遍用它套策略门槛，
    保证 after_edit_queue_drained 基于本轮全局编辑队列生效。
    """
    qualified: list[str] = []
    for job_dir in jobs:
        if job_dir in done:
            continue
        if lock_alive(job_dir):
            if args.verbose:
                print(f"[skip] {job_dir}: 活锁存在（链在跑）")
            continue
        ok, reasons = check_trigger(job_dir)
        if not ok:
            if getattr(args, "once", False) or args.verbose:
                print(f"[skip] {job_dir}: {'；'.join(reasons)}")
            continue
        name = os.path.basename(os.path.dirname(job_dir).rstrip("\\/"))
        _sync_intent(job_dir, name, intent_states.setdefault(job_dir.lower(), {}))
        idle = time.time() - last_change_ts(job_dir)
        if idle < args._settle:
            print(f"[wait] {job_dir}: 条件满足，静默确认中（{idle:.0f}/{args._settle:.0f}s）")
            continue
        qualified.append(job_dir)
    pending_edit = sum(
        1 for j in qualified
        if find_stream_draft(os.path.basename(os.path.dirname(j).rstrip("\\/"))) is None)
    plans: list[dict[str, Any]] = []
    for j in qualified:
        job = jm.load_job(os.path.join(j, "job.json"))
        wf = _intent_of(job)
        # dispatch 预览叠加（仅 rev==0 且只读不落盘）：首轮 edit 命令就要带上
        # dispatch 声明的 cut/mode；正式落盘仍由 edit 阶段的 ingest 归并完成
        if int(wf.get("intent_revision") or 0) == 0:
            try:
                raw = jm.read_dispatch(j)
                if raw:
                    eff = dict(wf)
                    eff.update(jm._normalize_dispatch(raw))
                    eff["intent_source"] = "dispatch"
                    job["workflow"] = eff
            except ValueError:
                pass  # dispatch 非法：按现有 workflow 出计划，ingest 时会告警
        plans.append(build_plan(j, job, cfg, pending_edit=pending_edit,
                                phase_override=getattr(args, "phase", None)))
    return plans


def _pick(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """每轮只推进一部：edit 优先（不占剪映），再按优先级降序、路径升序。"""
    actionable = [p for p in candidates if p["action"] in ("edit", "export")]
    if not actionable:
        return None
    actionable.sort(key=lambda p: (0 if p["action"] == "edit" else 1,
                                   -int(p["intent"].get("priority") or 0),
                                   p["job_dir"].lower()))
    return actionable[0]


def cmd_plan(args: argparse.Namespace) -> int:
    """查看所有剧的阶段/意图/下一步/被挡原因（不执行）。"""
    cfg = _load_watch_config(getattr(args, "config", None), quiet=True) or {}
    jobs, roots = _collect_jobs(args, cfg)
    if not jobs:
        print("没有发现任务：配置 roots 或传 --root/--job")
        return 1
    policy = cfg.get("export_policy", "immediate")
    print(f"watch 计划总览（{len(jobs)} 个任务，export_policy={policy}，"
          f"hold={bool(cfg.get('hold', False))}）")
    states: dict[str, dict[str, Any]] = {}
    args._settle = 0.0  # plan 模式不做防抖等待
    args.verbose = False
    args.once = True
    args.phase = getattr(args, "phase", None)
    candidates = _eval_round(args, cfg, jobs, set(), states)
    cand_by_dir = {p["job_dir"]: p for p in candidates}
    for job_dir in jobs:
        name = os.path.basename(os.path.dirname(job_dir).rstrip("\\/"))
        p = cand_by_dir.get(job_dir)
        if p:
            print("  " + _plan_line(p).replace("\n", "\n  "))
            continue
        try:
            job = jm.load_job(os.path.join(job_dir, "job.json"))
            ok, reasons = check_trigger(job_dir)
            wf = _intent_of(job)
            status = "未触发" if not ok else "防抖/锁"
            print(f"  [plan] {name}: {status}（{'；'.join(reasons) if not ok else '等待中'}）"
                  f" | 意图: mode={wf.get('mode')} 来源={wf.get('intent_source')} "
                  f"rev={wf.get('intent_revision')}")
        except Exception as e:
            print(f"  [plan] {name}: job.json 不可读（{e}）")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    try:
        cfg = _load_watch_config(getattr(args, "config", None))
    except (OSError, ValueError) as e:
        print(f"[FAIL] {e}")
        return 1
    jobs, roots = _collect_jobs(args, cfg)
    if not jobs:
        print("[FAIL] 没有可监听的任务：给 --job <cover_job>、--root <目录>，"
              "或在 pipeline/presets.json 的 watch 段配置 roots")
        return 1
    settle = args.settle if args.settle is not None else float(cfg.get("settle", 30.0))
    interval = args.interval if args.interval is not None else float(cfg.get("interval", 5.0))
    args._settle = settle
    print(f"cover_watch 监听 {len(jobs)} 个任务（静默 {settle:.0f}s，轮询 {interval:.0f}s，"
          f"roots={roots or '无'}，export_policy={cfg.get('export_policy', 'immediate')}，"
          f"配置热加载）")
    done: set[str] = set()
    known: set[str] = set()
    intent_states: dict[str, dict[str, Any]] = {}
    while True:
        # 配置热加载
        try:
            cfg_new = _load_watch_config(getattr(args, "config", None), quiet=True)
        except (OSError, ValueError):
            cfg_new = {}
        if cfg_new:
            cfg = cfg_new
        jobs, roots = _collect_jobs(args, cfg)
        if args.settle is None:
            args._settle = float(cfg.get("settle", settle))
        if args.interval is None:
            interval = float(cfg.get("interval", interval))
        hold_on = args.hold or bool(cfg.get("hold", False))
        fresh = [j for j in jobs if j.lower() not in known]
        for j in fresh:
            print(f"[new] 自动发现任务: {j}")
            known.add(j.lower())

        candidates = _eval_round(args, cfg, jobs, done, intent_states)
        if hold_on or args.dry_run:
            # 只列计划不执行：全部候选（含被挡原因）
            for p in candidates:
                print(_plan_line(p))
            if not candidates and (args.once or args.verbose):
                print("[plan] 本轮无可推进任务")
            if args.once:
                return 0
            time.sleep(interval)
            continue
        pick = _pick(candidates)
        if pick is None:
            if args.once:
                return 0
            time.sleep(interval)
            continue
        if args.verbose:
            for p in candidates:
                if p is not pick and p["action"] == "blocked":
                    print(_plan_line(p))
        if not acquire_lock(pick["job_dir"], phase=pick["action"]):
            time.sleep(interval)
            continue
        try:
            if pick["action"] == "edit":
                success = run_edit(pick["job_dir"], pick, dry_run=False)
            else:
                success = run_export(pick["job_dir"], pick, cfg, dry_run=False,
                                     verify=not args.no_verify,
                                     no_derive=args.no_derive)
        except Exception as e:
            _log(pick["job_dir"], f"chain 异常: {e}")
            toast("❌ 本地执行链异常", f"{pick['name']}: {e}", icon="error")
            success = False
        finally:
            release_lock(pick["job_dir"])
        if success and pick["action"] == "export":
            done.add(pick["job_dir"])
        if args.once:
            return 0
        time.sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", action="append", default=None,
                    help="cover_job 目录或其中的 job.json（可多个）；省略时读 presets.json 的 watch 段")
    ap.add_argument("--root", action="append", default=None,
                    help="自动发现根目录（扫描 <root>/*/cover_job，可多个）")
    ap.add_argument("--config", default=None,
                    help="监听配置 JSON（默认读 pipeline/presets.json 的 watch 段）")
    ap.add_argument("--settle", type=float, default=None, help="静默确认秒数（默认 30 或配置值）")
    ap.add_argument("--interval", type=float, default=None, help="轮询间隔秒数（默认 5 或配置值）")
    ap.add_argument("--once", action="store_true", help="只评估/推进一轮就退出")
    ap.add_argument("--dry-run", action="store_true", help="只打印被选中任务的计划，不执行")
    ap.add_argument("--hold", action="store_true",
                    help="只列计划不执行（等价配置 hold:true，供先核对 job.json 再放行）")
    ap.add_argument("--phase", choices=["edit", "export"], default=None,
                    help="单轮强制阶段（覆盖阶段推导与策略门槛）")
    ap.add_argument("--no-derive", action="store_true", help="跳过平台派生图（保留兼容）")
    ap.add_argument("--no-verify", action="store_true",
                    help="（保留兼容；导出阶段质检随 batch_runner/verify_export 默认开启）")
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command")
    p_st = sub.add_parser("status", help="查看锁与进度")
    p_st.add_argument("--job", action="append", required=True)
    p_pl = sub.add_parser("plan", help="查看所有剧的阶段/意图/下一步/被挡原因（不执行）")
    p_pl.add_argument("--job", action="append", default=None)
    p_pl.add_argument("--root", action="append", default=None)
    p_pl.add_argument("--config", default=None)
    p_pl.add_argument("--phase", choices=["edit", "export"], default=None)
    args = ap.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "plan":
        return cmd_plan(args)
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
