"""export_guard.py — 导出链守卫（纯逻辑，不依赖 UI / 通知）。

三条导出入口（watcher / series_runner 队列 / 直调 robust_auto_export）共用同一套
前置与收尾守卫。本模块**不 import** notifier / cover_watch / 日志模块：所有副作用
（日志、toast、子进程窗口标志）都由调用方以参数注入，因此可以直接在 pythonw
子进程里 import，不会把 UI 依赖带进去。

守卫清单
  前置（需要 job 上下文，无 job 时由调用方显式声明跳过）
    1. reap_stale              崩溃回收：上次 exporting 心跳超时 → 转 retrying
    2. apply_job_config        旧版 export_config.json → job.json cut_s / derive 覆盖
    3. archive_stale_outputs   同版本重跑前旧三件套改名 .v<n> 保留
  收尾（不依赖 job 上下文，直调也跑）
    4. missing_artifacts       成品三件套缺件（mp4 / -封面.jpg / progress）
    5. run_verify              导出后质检（帧对齐等）
    6. success_evidence_missing  mp4 正式名 + progress.done 硬证据
  组合
    gate_after_export          4→5→6 串起来，返回统一结论；调用方只负责登记和通知
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_REPO, "pipeline")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_state as es  # noqa: E402
import job_manifest as jm  # noqa: E402

EXPORT_CONFIG_NAME = "export_config.json"


# ── 守卫 1：崩溃回收 ─────────────────────────────────────────────

def progress_mtime(mp4_path: str) -> float:
    """导出进度文件 mtime（心跳）：运行中在剧名目录同级，完成后在 mp4 旁。"""
    out_dir = os.path.dirname(os.path.abspath(mp4_path))
    side = os.path.join(os.path.dirname(out_dir), os.path.basename(out_dir) + ".progress.json")
    ts = 0.0
    for p in (side, mp4_path + ".progress.json"):
        try:
            ts = max(ts, os.path.getmtime(p))
        except OSError:
            pass
    return ts


def reap_stale(job_dir: str, *, progress_ts: float = 0.0) -> bool:
    """守卫 1：exporting 且心跳超时 → 落盘回收为 retrying。返回是否发生回收。"""
    return es.reap_stale(job_dir, progress_mtime=progress_ts)


# ── 守卫 2：旧版 export_config.json ─────────────────────────────

def read_export_config(job_dir: str) -> dict[str, Any]:
    p = os.path.join(job_dir, EXPORT_CONFIG_NAME)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{EXPORT_CONFIG_NAME} 应为 JSON 对象")
    return data


def apply_job_config(job_dir: str,
                     *, log: Optional[Callable[[str], None]] = None) -> Optional[list[str]]:
    """守卫 2：旧版 export_config.json → cut_s 写 job.json、derive 返回覆盖列表（可空）。"""
    cfg = read_export_config(job_dir)
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
    if applied and log:
        log("export_config: " + ", ".join(applied))
    return derive_override


# ── 守卫 3：旧成品归档 ──────────────────────────────────────────

def archive_stale_outputs(export_file: str, *, max_versions: int = 99):
    """守卫 3：旧三件套改名 .v<n> 保留（实现仍在 pipeline，惰性 import 避免 lib→pipeline 环）。"""
    import pipeline as jp
    return jp.archive_stale_outputs(export_file, max_versions=max_versions)


# ── 守卫 4：成品三件套缺件 ──────────────────────────────────────

def artifact_paths(draft_name: str) -> dict[str, str]:
    """按草稿名解析成品三件套路径（与导出侧同口径）。"""
    import export_config as pc
    return artifact_paths_of(pc.export_mp4_path(draft_name), draft_name)


def artifact_paths_of(mp4_path: str, draft_name: str | None = None) -> dict[str, str]:
    out = os.path.abspath(mp4_path)
    out_dir = os.path.dirname(out)
    # 封面按草稿名（导出侧命名口径）；没有草稿名时退回 mp4 主名
    stem = draft_name or os.path.splitext(os.path.basename(out))[0]
    return {"mp4": out,
            "封面": os.path.join(out_dir, f"{stem}-封面.jpg"),
            "progress": out + ".progress.json"}


def missing_artifacts(draft_name: str | None = None, *, strict_progress: bool = False,
                      mp4_path: str | None = None,
                      progress_path: str | None = None) -> list[str]:
    """守卫 4：成品三件套缺失项（mp4 / -封面.jpg / progress）。

    strict_progress=True 只认成品目录内那份（mp4.progress.json）：导出成功收尾一定
    会写它并删侧写，用于"导出后判成败"；冲突巡检放宽到侧写，避免历史成品误报。
    progress_path：显式指定正片进度文件（直调无 --move 时它落在 output_path 旁）。
    """
    paths = artifact_paths_of(mp4_path, draft_name) if mp4_path else artifact_paths(draft_name or "")
    mp4 = paths["mp4"]
    missing = [k for k in ("mp4", "封面") if not os.path.isfile(paths[k])]
    out_dir = os.path.dirname(mp4)
    side = os.path.join(os.path.dirname(out_dir), os.path.basename(out_dir) + ".progress.json")
    progress_ok = os.path.isfile(progress_path or paths["progress"]) or (
        not strict_progress and os.path.isfile(side))
    if not progress_ok:
        missing.append("progress")
    return missing


# ── 守卫 5：导出后质检 ──────────────────────────────────────────

def verify_failure_brief(vlog: str, rc: int) -> str:
    """verify 未通过：退出码 + 结论行/漂移数值（必须带数值，不能只说"质检没过"）。"""
    try:
        with open(vlog, encoding="utf-8", errors="replace") as f:
            data = f.read()[-8192:]
    except OSError:
        return f"verify rc={rc}（明细日志不可读: {vlog}）"
    marker = data.rfind("=====")
    if marker >= 0:
        data = data[marker:]
    picked: list[str] = []
    for line in data.splitlines():
        s = line.strip()
        if not s:
            continue
        if (s.startswith("帧级漂移:") or s.startswith("字幕帧位:")
                or "δ_v=" in s or s.startswith("❌")):
            if s not in picked:
                picked.append(s)
    base = f"verify rc={rc}（明细 {vlog}）"
    if not picked:
        return base
    return base + "｜" + " ⏎ ".join(x[:200] for x in picked[-4:])


def run_verify(script: str, draft_name: str, mp4_path: str, job_dir: str | None = None,
               *, creationflags: int = 0,
               log: Optional[Callable[[str], None]] = None) -> tuple[int, str]:
    """守卫 5：调用导出后质检脚本，返回 (rc, 明细日志路径)。

    有 job 上下文 → 06_publish/verify.log；无 job（直调）→ 成品旁的
    `<mp4>.verify.log`，不写任何 job 目录。
    """
    if job_dir:
        log_path = os.path.join(job_dir, "06_publish", "verify.log")
    else:
        log_path = os.path.abspath(mp4_path) + ".verify.log"
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
            logf.write(f"\n===== {jm.now_iso()} {draft_name} =====\n")
            logf.flush()
            rc = subprocess.call([sys.executable, script, draft_name, mp4_path],
                                 cwd=os.path.dirname(script),
                                 stdout=logf, stderr=subprocess.STDOUT,
                                 creationflags=creationflags)
        return rc, log_path
    except OSError as e:
        if log:
            log(f"verify 启动失败: {e}")
        return 2, log_path


# ── 守卫 6：收尾硬证据 ──────────────────────────────────────────

def success_evidence_missing(mp4: str, started_at: float | None = None,
                             progress_path: str | None = None) -> list[str]:
    """守卫 6：正式名 mp4 真在（且是本轮产物）+ 旁 progress 的 done 为真。"""
    if not os.path.isfile(mp4):
        return ["mp4(正式名未出现)"]
    try:
        st = os.stat(mp4)
    except OSError:
        return ["mp4(不可 stat)"]
    if st.st_size <= 0:
        return ["mp4(空文件)"]
    if started_at is not None and st.st_mtime < started_at - 2:
        return ["mp4(旧文件：正式名本轮未更新)"]
    pj = progress_path or (mp4 + ".progress.json")
    if not os.path.isfile(pj):
        return ["progress"]
    try:
        with open(pj, encoding="utf-8") as f:
            rec = json.load(f)
        pst = os.stat(pj)
    except (OSError, ValueError):
        return ["progress(不可解析)"]
    if started_at is not None and pst.st_mtime < started_at - 2:
        return ["progress(非本轮)"]
    return [] if rec.get("done") is True else ["progress.done!=true"]


# ── 组合门禁：4 → 5 → 6 ────────────────────────────────────────

def gate_after_export(draft_name: str, mp4_path: str, *, job_dir: str | None = None,
                      started_at: float | None = None, verify: bool = True,
                      verify_script: str | None = None, creationflags: int = 0,
                      ctx: str = "", progress_path: str | None = None,
                      log: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """导出子进程返回成功后，按 三件套 → 质检 → 收尾证据 顺序判定成败。

    返回统一结论，调用方只负责登记失败 / 通知：
      ok            是否全部通过
      stage         artifacts / verify / evidence / ok
      failure_class 对应的失败分类（artifacts_incomplete / verify_failed /
                    artifacts_missing）；ok 时为 None
      phase         记到状态机的阶段（export / verify）
      missing       缺件列表（artifacts / evidence 阶段有值）
      brief         可直接写入 last_error 的失败原因
      toast_title / toast_detail  通知标题与细节（调用方补上剧名草稿名）
      verify_log    质检明细路径（未跑质检时为 None）
    """
    log = log or (lambda _m: None)
    missing = missing_artifacts(draft_name, mp4_path=mp4_path, strict_progress=True,
                                progress_path=progress_path)
    if missing:
        brief = f"导出返回成功但产物不全：缺 {', '.join(missing)}（{mp4_path}）"
        return {"ok": False, "stage": "artifacts", "failure_class": "artifacts_incomplete",
                "phase": "export", "missing": missing, "brief": brief,
                "toast_title": "❌ 导出产物不全（按失败处理）",
                "toast_detail": f"：缺 {', '.join(missing)}", "verify_log": None}
    verify_log = None
    if verify and verify_script and os.path.isfile(verify_script):
        log(f"verify start{ctx}: {draft_name}")
        rc, verify_log = run_verify(verify_script, draft_name, mp4_path, job_dir,
                                    creationflags=creationflags, log=log)
        log(f"verify rc={rc}{ctx}: {draft_name}（明细 {verify_log}）")
        if rc != 0:
            brief = verify_failure_brief(verify_log, rc)
            return {"ok": False, "stage": "verify", "failure_class": "verify_failed",
                    "phase": "verify", "missing": [], "brief": brief,
                    "toast_title": "⛔ 质检未通过（不登记成品）",
                    "toast_detail": "\n" + brief, "verify_log": verify_log}
    miss_ev = success_evidence_missing(mp4_path, started_at, progress_path)
    if miss_ev:
        brief = f"收尾缺产物证据：缺 {', '.join(miss_ev)}（{mp4_path}）"
        return {"ok": False, "stage": "evidence", "failure_class": "artifacts_missing",
                "phase": "export", "missing": miss_ev, "brief": brief,
                "toast_title": "❌ 导出未完成（缺产物证据）",
                "toast_detail": f"：缺 {', '.join(miss_ev)}", "verify_log": verify_log}
    return {"ok": True, "stage": "ok", "failure_class": None, "phase": None,
            "missing": [], "brief": "", "toast_title": "", "toast_detail": "",
            "verify_log": verify_log}
