# -*- coding: utf-8 -*-
"""多剧集串行编排器：串流生成草稿 / 导出 / 联动，状态机断点续跑。

三种模式:
    full     （默认）生成草稿 → 导出，逐部联动
    generate 只生成草稿不导出（部间零等待），供稍后统一导出
    export   只导出不生成，输入支持三种：
               - 剧集目录（按 系列名*串流 + 最新 mtime 找草稿）
               - 草稿名（com.lveditor.draft 下存在优先按草稿名）
               - --all-stream：草稿库中所有名字含「串流」的草稿全部导出

用法:
    python pipeline\\series_runner.py "<剧集根>\\剧A" "<剧集根>\\剧B"
    python pipeline\\series_runner.py A B C --mode generate
    python pipeline\\series_runner.py A B --mode export
    python pipeline\\series_runner.py --mode export --all-stream
    python pipeline\\series_runner.py A --mode export --profile b,tiktok   # 多版本

状态写入 pipeline/_series_queue_state.json，重启自动跳过已完成、续跑半成品。
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
_LIB = os.path.join(_ROOT, "lib")
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, _HERE)
sys.path.insert(0, _LIB)

from utils.notifier import toast  # noqa: E402
from export_config import export_mp4_path, get_export_dir  # noqa: E402
import export_config as pc  # noqa: E402
import job_manifest as jm  # noqa: E402
import subtitle_guard  # noqa: E402
import export_guard as eg  # noqa: E402  # 导出链守卫（与 watcher / 直调同口径）
import export_lock  # noqa: E402  # 全局导出锁：脚本导出与 watcher 互斥
import pipeline as jp  # noqa: E402  # 导出失效标记 / 草稿目录解析
import freshness  # noqa: E402  # E2 成品新鲜度（sidecar + 版本槽）
from no_window import NO_WINDOW, hidden_child_env  # noqa: E402

STATE_PATH = os.path.join(_HERE, "_series_queue_state.json")
BATCH_RUNNER = os.path.join(_HERE, "batch_runner.py")
AUTO_EXPORTER = os.path.join(
    _ROOT, ".trae", "skills", "jianying-editor", "examples", "robust_auto_export.py"
)
VERIFY_SCRIPT = os.path.join(_HERE, "verify_export.py")
JY_DRAFT_ROOT = pc.get_projects_root()
# 各模式部间默认间隔（秒）：generate 不碰剪映零等待；export 留 60s 回首页；full 120s
_MODE_DEFAULT_RESET = {"full": 120, "generate": 0, "export": 60}


def _load_state():
    if os.path.isfile(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)[:24]


def _find_draft(series_name: str, since: float = 0) -> str | None:
    """定位串流草稿：精确匹配「系列名_第NN[-NN]集串流」，取 mtime 最新；since=0 不限时间。

    不用 `系列名*串流` 前缀 glob：剧名互为前缀时会串剧，`_b` 克隆也可能被算进来。
    """
    pattern = re.compile(rf"{re.escape(series_name)}_第\d+(?:-\d+)?集串流")
    try:
        names = os.listdir(JY_DRAFT_ROOT)
    except OSError:
        return None
    cands = [
        os.path.join(JY_DRAFT_ROOT, n) for n in names
        if pattern.fullmatch(n) and os.path.isdir(os.path.join(JY_DRAFT_ROOT, n))
        and os.path.getmtime(os.path.join(JY_DRAFT_ROOT, n)) >= since
    ]
    if not cands:
        return None
    return os.path.basename(max(cands, key=os.path.getmtime))


def _all_stream_drafts() -> list[str]:
    """草稿库中所有名字含「串流」的草稿，按 mtime 升序（先旧后新）"""
    cands = [
        d for d in glob.glob(os.path.join(JY_DRAFT_ROOT, "*串流*")) if os.path.isdir(d)
    ]
    return [os.path.basename(d) for d in sorted(cands, key=os.path.getmtime)]


def _output_exists(draft_name: str) -> bool:
    """成品已存在且 >1MB 视为已导出，跳过（成品按剧名子目录归置）。

    草稿带 `_export_stale.json` 时一票否决：草稿重建/发布后旧 mp4 仍在，
    只看文件会把旧成品当成新成品跳过导出。
    """
    if jp.export_is_stale(os.path.join(JY_DRAFT_ROOT, draft_name)):
        return False
    out = export_mp4_path(draft_name)
    return os.path.isfile(out) and os.path.getsize(out) > 1 * 1024 * 1024


def _declared_profiles(job: dict) -> list[str]:
    """job 声明的版本（基础版 a 恒在）：workflow.preset_profile + 已有 outputs。"""
    procs = pc.parse_preset_profiles((job.get("workflow") or {}).get("preset_profile"))
    for ver in ((job.get("video") or {}).get("outputs") or {}):
        if ver not in procs:
            procs.append(ver)
    return procs or ["a"]


def _version_of_draft(draft_name: str, profiles: list[str]) -> str:
    """草稿名 → 版本：`<基础草稿>_<版本>` 命中声明版本时取该版本，否则为基础版 a。"""
    for prof in profiles:
        if prof != "a" and draft_name.endswith("_" + prof):
            return prof
    return "a"


def _account_draft_name(base_draft: str, prof: str) -> str:
    """附加版本草稿命名约定（与 preset_ops.clone_draft_with_presets 保持一致）。"""
    return f"{base_draft}_{prof}"


def _resolve_export_profiles(job_dir: str | None, explicit: list[str] | None) -> list[str]:
    """本轮要导出的版本：显式 --profile > prompts.json 组 > job 声明 > 仅基础版。

    基础版 a 恒在（草稿名就是原串流名，历史行为不变），其余版本按
    `<基础草稿>_<版本>` 找克隆草稿。
    """
    if explicit:
        procs = list(explicit)
    elif job_dir:
        procs = jm.prompt_groups(job_dir)
        if not procs:
            try:
                procs = _declared_profiles(jm.load_job(os.path.join(job_dir, "job.json")))
            except Exception:
                procs = []
    else:
        procs = []
    if "a" not in procs:
        procs.insert(0, "a")
    return list(dict.fromkeys(procs))


def _record_export_freshness(draft_name: str, out_mp4: str) -> None:
    """写 E2 sidecar + `edges.e2.versions[<版本>]`；失败只告警（best-effort）。"""
    try:
        if not (out_mp4 and os.path.isfile(out_mp4)):
            return
        job_dir = freshness.job_dir_for_draft(draft_name)
        if not job_dir:
            return
        freshness.record_export(job_dir, freshness.version_for_draft(job_dir, draft_name),
                                draft_name, out_mp4)
    except Exception as e:
        print(f"  [warn] 记录成品新鲜度失败（忽略）: {e}")


def _finalize_export(draft_name: str, out_mp4: str, job_dir: str | None,
                     profile: str | None = None) -> bool:
    """导出成功后的收尾，顺序固定：台账 → 清 stale → E2。

    台账按实际导出的草稿写 `video.outputs[<版本>]`（多版本各记各的）；
    台账失败时**不**清 stale、**不**写 E2，直接返回 False（fail-closed）：
    watcher 看到 stale 会重导，而不是把「成品在、台账缺」判成 blocked。
    后两步失败只告警：台账已成立，watcher 仍能靠 stale/progress 兜底。
    没有 job 可登记（脚本直接跑单条草稿）时跳过台账，仍清 stale、写 E2。
    """
    if job_dir:
        try:
            job = jm.load_job(os.path.join(job_dir, "job.json"))
            prof = profile or _version_of_draft(draft_name, _declared_profiles(job))
            jm.record_video(job_dir, export_dir=os.path.dirname(out_mp4) or None,
                            export_file=out_mp4, draft_name=draft_name, profile=prof)
            print(f"  🧾 台账已登记: outputs.{prof} ← {os.path.basename(out_mp4)}")
        except Exception as e:
            print(f"  ⚠️ 台账登记失败（保留导出失效标记，下次重跑）: {e}")
            return False
    jp.clear_export_stale(os.path.join(JY_DRAFT_ROOT, draft_name))
    _record_export_freshness(draft_name, out_mp4)
    return True


def _own_export_lock() -> bool:
    """当前进程是否已持有全局导出锁（watcher 在 run_export 外层已持锁）。"""
    try:
        data = export_lock.read_lock() or {}
        return int(data.get("pid") or 0) == os.getpid()
    except Exception:
        return False


def _parse_target(raw: str) -> tuple[str, dict]:
    """targets 元素支持 KV 覆盖语法：路径:cut=52（供 agent 一部剧一个参数串传参）。
    返回 (剧集目录, 覆盖参数 dict)。"""
    parts = raw.split(":")
    if len(parts) >= 3 and re.fullmatch(r"[A-Za-z]", parts[0]):
        path = ":".join(parts[:2])
        kvs = parts[2:]
    else:
        path, kvs = raw, []
    overrides = {}
    for kv in kvs:
        m = re.fullmatch(r"(cut)=(.+)", kv.strip())
        if m:
            overrides[m.group(1)] = float(m.group(2))
    return path, overrides


def _run_pipeline(series_dir: str, slug: str, overrides: dict | None = None) -> bool:
    cover = os.path.join(series_dir, "封面.png")
    cmd = [sys.executable, "-u", BATCH_RUNNER, series_dir,
           "--workdir", os.path.join("pipeline", f"_work_{slug}"), "--fresh",
           "--no-export"]  # 导出由 series_runner 统一编排，batch_runner 不自动导出
    if os.path.isfile(cover):
        cmd += ["--cover-image", cover]
    overrides = overrides or {}
    if overrides.get("cut"):
        cmd += ["--cut", str(overrides["cut"])]
    log_path = os.path.join(_HERE, f"_stream_{slug}.log")
    err_path = os.path.join(_HERE, f"_stream_{slug}.err.log")
    with open(log_path, "ab") as out, open(err_path, "ab") as err:
        proc = subprocess.run(cmd, cwd=_ROOT, stdout=out, stderr=err,
                              creationflags=NO_WINDOW)
    return proc.returncode == 0


def _preclean_bundles(draft_name: str, out_dir: str, keep_target: bool = False) -> None:
    """导出前清掉上轮残留：删除不含成品 mp4 的「剧名」目录和「剧名(1)/(2)…」去重目录。

    目标目录必须在导出前不存在，剪映才会直接写进「剧名」目录，而不是编号成「剧名(1)」。
    keep_target=True 时保留目标目录本身（失败收尾用，保留 stall 截图等诊断文件）。
    """
    out_dir = os.path.abspath(out_dir)
    safe = os.path.basename(out_dir)
    parent = os.path.dirname(out_dir)
    if not os.path.isdir(parent):
        return
    for name in os.listdir(parent):
        p = os.path.join(parent, name)
        if not os.path.isdir(p):
            continue
        if name != safe and not name.startswith(safe + "("):
            continue
        if keep_target and os.path.abspath(p) == out_dir:
            continue
        has_mp4 = any(f.lower().endswith(".mp4") for _r, _d, fs in os.walk(p) for f in fs)
        if has_mp4:
            print(f"  ⚠️ 保留含成品的目录: {p}")
            continue
        shutil.rmtree(p, ignore_errors=True)
        print(f"  🧹 清理上轮残留目录: {p}")


def _consolidate_bundle(draft_name: str, out_mp4: str) -> None:
    """导出后归拢：封面缺失时从兄弟目录抢救，清掉空壳去重目录，保证 mp4/封面/进度文件同目录。"""
    out_dir = os.path.dirname(os.path.abspath(out_mp4))
    safe = os.path.splitext(os.path.basename(out_mp4))[0]
    cover = os.path.join(out_dir, safe + "-封面.jpg")
    parent = os.path.dirname(out_dir)
    if not os.path.isdir(parent):
        return
    if os.path.isfile(out_mp4) and not os.path.isfile(cover):
        for name in os.listdir(parent):
            p = os.path.join(parent, name)
            if not os.path.isdir(p) or os.path.abspath(p) == out_dir:
                continue
            if not name.startswith(safe):
                continue
            found = None
            for f in os.listdir(p):
                if "封面" in f and f.lower().endswith((".jpg", ".jpeg", ".png")):
                    found = os.path.join(p, f)
                    break
            if found:
                shutil.move(found, cover)
                print(f"  🖼️ 封面已归位: {cover}")
                break
    for name in os.listdir(parent):
        p = os.path.join(parent, name)
        if not os.path.isdir(p) or os.path.abspath(p) == out_dir:
            continue
        if not name.startswith(safe):
            continue
        if any(f.lower().endswith(".mp4") for _r, _d, fs in os.walk(p) for f in fs):
            continue
        shutil.rmtree(p, ignore_errors=True)
        print(f"  🧹 清理残留目录: {p}")


def _progress_mtime(out_mp4: str) -> float:
    """导出进度文件 mtime（心跳）：运行中在剧名目录同级，完成后在 mp4 旁。"""
    out_dir = os.path.dirname(os.path.abspath(out_mp4))
    side = os.path.join(os.path.dirname(out_dir), os.path.basename(out_dir) + ".progress.json")
    ts = 0.0
    for p in (side, out_mp4 + ".progress.json"):
        try:
            ts = max(ts, os.path.getmtime(p))
        except OSError:
            pass
    return ts


def _job_dir_for(target: str, is_draft: bool) -> str | None:
    """解析 target 对应的 cover_job（含 job.json 才返回）。

    剧集目录：<target>/cover_job；草稿名：遍历 presets.json watch.roots，
    取剧名目录名与草稿名前缀匹配最长且唯一的一组；多命中不自动拦截。
    """
    if not is_draft:
        cand = os.path.join(target, "cover_job")
        return cand if os.path.isfile(os.path.join(cand, "job.json")) else None
    hits: list[tuple[int, str]] = []
    try:
        roots = pc.get_watch_roots()
    except Exception:
        roots = []
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for d in names:
            if not target.startswith(d):
                continue
            cand = os.path.join(root, d, "cover_job")
            if os.path.isfile(os.path.join(cand, "job.json")):
                hits.append((len(d), cand))
    if not hits:
        return None
    hits.sort(reverse=True)
    best = [c for n, c in hits if n == hits[0][0]]
    if len(best) > 1:
        print(f"  ⚠️ 草稿 {target} 匹配到多个 cover_job: {'、'.join(best)}，不自动拦截")
        return None
    return best[0]


def _export_console_path(job_dir: str | None) -> str:
    """导出子进程控制台日志落点：有 cover_job 写进 job，否则落 pipeline/_work。"""
    base = job_dir or os.path.join(_HERE, "_work")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "export_console.log")


def _close_jianying_after_batch(reason: str = "队列导出结束") -> None:
    """批内复用收尾：统一走 jianying_proc 的 WM_CLOSE（异常路径由 finally 保证调用）。"""
    try:
        import jianying_proc as jp
    except Exception:
        return
    if not jp.running():
        return
    print(f"🛑 {reason}：WM_CLOSE 关闭复用的剪映实例")
    if not jp.stop_jianying(grace_s=20.0, force=False,
                            log_fn=lambda m: print(f"  {m}")):
        print("  ⚠️ 剪映未在时限内正常退出（不影响已完成成品）")


def _drafts_root():
    try:
        from utils.formatters import get_default_drafts_root
        return get_default_drafts_root()
    except Exception:
        return JY_DRAFT_ROOT


def _draft_duration_s(draft_name: str):
    """草稿总时长（秒）。读不到返回 None：导出照常，只是进度没有估算。"""
    if not draft_name:
        return None
    path = os.path.join(_drafts_root(), draft_name, "draft_content.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            draft = json.load(f)
        us = 0
        for track in draft.get("tracks") or []:
            for seg in track.get("segments") or []:
                tr = seg.get("target_timerange") or {}
                us = max(us, int(tr.get("start", 0)) + int(tr.get("duration", 0)))
        return round(us / 1e6, 3) if us > 0 else None
    except Exception:
        return None


def _export_cmd(draft_name: str, out_mp4: str, cold_start: bool | None, keep_open: bool):
    cmd = [sys.executable, "-u", AUTO_EXPORTER, draft_name, out_mp4, "--move",
           "--fallback-dir", get_export_dir()]
    # 首版没有旧成品时，进度百分比只能靠草稿时长估算；watcher 路径也要传，
    # 否则和 pipeline_ui 路径的行为不一致（长时间显示"无估算"）。
    expect_s = _draft_duration_s(draft_name)
    if expect_s:
        cmd += ["--expect-duration", ("%g" % expect_s)]
    # cold_start 只表达「是否强制干净启动」的意图；None=交给实例管理器判定。
    if cold_start:
        cmd.append("--cold-start")
    if keep_open:
        cmd.append("--keep-open")
    return cmd


def _run_export(draft_name: str, timeout: float = 1800, *, cold_start: bool | None = None,
                job_dir: str | None = None, keep_open: bool = False) -> bool:
    """导出统一入口：外层持 `.export.lock`，再走 preclean + 子进程 + 归拢。

    watcher 调用本函数前已在 `run_export` 里持有同一把锁（同进程），
    这里识别到「本进程已持锁」就复用，不重复加锁、也不释放别人的锁。
    取不到锁时直接返回 False（不启动子进程、不动目录），由调用方记失败。
    """
    acquired = False
    if not _own_export_lock():
        if not export_lock.acquire(owner=f"series_runner {draft_name}"):
            holder = export_lock.read_lock() or {}
            print(f"  ⛔ 全局导出锁被占用（pid={holder.get('pid')} "
                  f"owner={holder.get('owner')}），本次不导出")
            return False
        acquired = True
    try:
        return _run_export_locked(draft_name, timeout, cold_start=cold_start,
                                  job_dir=job_dir, keep_open=keep_open)
    finally:
        if acquired:
            export_lock.release()


def _run_export_locked(draft_name: str, timeout: float = 1800, *,
                       cold_start: bool | None = None,
                       job_dir: str | None = None, keep_open: bool = False) -> bool:
    """robust_auto_export 位置参数直达导出落点，--move 搬运到默认目录的剧名子目录。

    timeout 按「无进展」计：进度 json 每 30s 刷新一次，超过 timeout 秒没有任何刷新
    才判定卡死并杀进程（导出慢但一直在写盘不会被误杀）。
    cold_start: 只表达意图——True 强制干净启动，None 交给实例管理器判定
    （可交互首页可复用，假死实例必须重启）。
    keep_open: 批内复用——导出后把实例留在首页，交给下一份接着用；
    队列结束时由调用方统一 WM_CLOSE 关闭。单份导出保持 False（用完就关）。
    job_dir: 显式 cover_job；为空时按草稿名解析。缺字幕清单非空时直接拦截，
    不建目录、不清理残留、不启动导出子进程。
    """
    job_dir = job_dir or _job_dir_for(draft_name, True)
    if job_dir:
        try:
            jm.require_export_allowed(job_dir)
        except jm.ExportBlocked as e:
            print(f"⛔ {e}")
            print("   请重剪这些集，确认字幕非空后重跑；未修复前不会导出。")
            return False
    out_mp4 = export_mp4_path(draft_name)
    out_dir = os.path.dirname(out_mp4)
    # 前置守卫（与 watcher 同源）：崩溃回收 → 配置覆盖 → 旧成品归档。
    # 直调（无 job 上下文）时这三项没有落点，必须显式声明跳过，不能静默略过。
    if job_dir:
        if eg.reap_stale(job_dir, progress_ts=eg.progress_mtime(out_mp4)):
            print("  ♻️ 真源回收：上次导出心跳超时（stale），转重试")
        try:
            eg.apply_job_config(job_dir, log=lambda m: print(f"  {m}"))
        except Exception as e:
            print(f"  ⛔ export_config 失败: {e}")
            return False
        if os.path.isfile(out_mp4) and jp.export_is_stale(os.path.join(_draft_root(), draft_name)):
            try:
                moved = eg.archive_stale_outputs(out_mp4)
            except Exception as e:
                print(f"  ⛔ 旧成品归档失败 {draft_name}: {e}")
                return False
            for s, d in moved:
                print(f"  🧹 旧成品归档: {os.path.basename(s)} → {os.path.basename(d)}")
    else:
        print("  ℹ️ 无 job 上下文，跳过 reap / 配置覆盖 / 归档旧产物")
    # 只建父目录：剧名目录留给剪映自己创建，否则剪映会把成品写进「剧名(1)」
    os.makedirs(os.path.dirname(out_dir), exist_ok=True)
    _preclean_bundles(draft_name, out_dir)
    cmd = _export_cmd(draft_name, out_mp4, cold_start, keep_open)

    # 子进程输出落盘：pythonw 链路下没有控制台，dprint 原本全部丢失（13:25、13:59
    # 两次冷启动失败零证据）。这里直接重定向 OS 句柄，并记录启动/pid/退出码。
    log_path = _export_console_path(job_dir)
    print(f"  📝 导出子进程日志: {log_path}")
    with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"\n===== [{time.strftime('%Y-%m-%d %H:%M:%S')}] 启动导出子进程 "
                   f"draft={draft_name} "
                   f"cold_start={'auto' if cold_start is None else int(bool(cold_start))} "
                   f"keep_open={int(keep_open)} =====\n")
        logf.write("cmd: " + " ".join(cmd) + "\n")
        logf.write(f"out: {out_mp4}\n")
        logf.flush()
        started = time.time()
        child_env = hidden_child_env()
        # 收尾守卫由本层统一执行，子进程不再重复跑一遍（避免二次质检）
        child_env["JY_GUARD_PARENT"] = "1"
        proc = subprocess.Popen(cmd, cwd=_ROOT, env=child_env,
                                creationflags=NO_WINDOW,
                                stdin=subprocess.DEVNULL,
                                stdout=logf, stderr=subprocess.STDOUT)
        logf.write(f"pid={proc.pid} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        logf.flush()
        last_beat = time.time()
        while proc.poll() is None:
            time.sleep(5)
            beat = _progress_mtime(out_mp4)
            if beat:
                last_beat = max(last_beat, beat)
            if time.time() - last_beat > timeout:
                print(f"  ⏱️ 导出 {timeout:.0f}s 无任何进展，判定卡死并终止子进程")
                proc.kill()
                proc.wait()
                break
        logf.write(f"exit: code={proc.returncode} "
                   f"duration_s={time.time() - started:.1f} "
                   f"at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        logf.flush()

    if proc.returncode == 0:
        _consolidate_bundle(draft_name, out_mp4)
        # 复用前提：上一份确实导出完成（子进程 rc=0 且成品已落盘）才切下一份。
        if not os.path.isfile(out_mp4):
            print(f"  ⚠️ 导出子进程成功但成品未落盘: {out_mp4}")
            return False
        # 收尾守卫（4/5/6）：三件套 → 质检 → 收尾证据。直调时 job_dir=None，
        # 只跑这三项，不碰 job.json（前置三项已在上面显式声明跳过）。
        res = eg.gate_after_export(draft_name, out_mp4, job_dir=job_dir,
                                   started_at=started, verify_script=VERIFY_SCRIPT,
                                   creationflags=NO_WINDOW,
                                   log=lambda m: print(f"  {m}"))
        if not res["ok"]:
            print(f"  ⛔ 导出后校验未通过（{res['failure_class']}）: {res['brief']}")
            return False
    else:
        # 失败也要清掉本轮重试产生的「剧名(1)/(2)…」，保证目录只剩一个
        _preclean_bundles(draft_name, out_dir, keep_target=True)
    return proc.returncode == 0


def _state_of(state, key):
    return state.setdefault(key, {"status": "pending"})


def main() -> int:
    ap = argparse.ArgumentParser(description="多剧集编排：串流生成 / 导出 / 联动，断点续跑")
    ap.add_argument("targets", nargs="*", help="剧集目录或草稿名")
    ap.add_argument("--mode", choices=["full", "generate", "export"], default="full")
    ap.add_argument("--all-stream", action="store_true",
                    help="export 模式：导出草稿库中所有含「串流」的草稿")
    ap.add_argument("--reset", type=float, default=None,
                    help="部间休息秒数（默认 full=120 / generate=0 / export=60）")
    ap.add_argument("--stage-gap", type=float, default=45,
                    help="full 模式中「生成→导出」的衔接秒数（默认 45）")
    ap.add_argument("--timeout", type=float, default=1800, help="导出超时秒数（默认 1800）")
    ap.add_argument("--cold-start", action="store_true",
                    help="导出前强制干净启动剪映（默认由实例管理器判定复用/重启）")
    ap.add_argument("--profile", action="append", metavar="VERSIONS",
                    help="额外导出的版本（可重复或逗号分隔，如 --profile b,tiktok）；"
                         "基础版 a 恒导出，其余版本按 <草稿>_<版本> 找克隆草稿。"
                         "缺省按 03_prompts/prompts.json 的 items[].group 判定")
    args = ap.parse_args()
    if args.profile:
        raw = ",".join(args.profile)
        procs: list[str] = []
        for p in (x.strip().lower() for x in raw.split(",")):
            if not p:
                continue
            if not re.fullmatch(r"[a-z][a-z0-9]{0,7}", p):
                ap.error(f"--profile 版本名不合法: {p}（小写字母开头，仅字母数字，≤8 位）")
            if p not in procs:
                procs.append(p)
        args.profile = procs or None

    if args.mode == "export" and args.all_stream:
        targets = _all_stream_drafts()
        if not targets:
            print("✗ 草稿库中没有含「串流」的草稿")
            return 1
        print(f"📋 全量导出清单（{len(targets)} 部）: {'、'.join(targets)}")
    else:
        if not args.targets:
            ap.error("必须提供剧集目录/草稿名，或使用 --all-stream")
        targets = [t.rstrip("\\/") for t in args.targets]

    # 解析 KV 覆盖语法（路径:cut=52），draft 名与 --all-stream 结果不做 KV 解析
    parsed = []  # [(target, overrides)]
    for t in targets:
        if os.path.isdir(t):
            # 真实存在的剧集目录（绝对路径），走 KV 覆盖解析
            path, ov = _parse_target(t)
            parsed.append((path, ov))
        elif os.path.isdir(os.path.join(JY_DRAFT_ROOT, t)) or args.all_stream:
            parsed.append((t, {}))
        else:
            path, ov = _parse_target(t)
            parsed.append((path, ov))
    overrides_of = {t: ov for t, ov in parsed}
    targets = [t for t, _ in parsed]

    reset = args.reset if args.reset is not None else _MODE_DEFAULT_RESET[args.mode]
    state = _load_state()
    failed = []

    # 批内复用：只有「导出阶段连续跑」的入口才复用一个剪映实例。
    # full 模式逐部"生成→导出"，生成阶段必须先关剪映（防内存旧副本回写草稿），
    # 复用跨不过去；输入全是现成草稿时没有生成阶段，可以整段复用。
    as_draft = {t: (not os.path.isdir(t)
                    and os.path.isdir(os.path.join(JY_DRAFT_ROOT, t)))
                for t in targets}
    reuse = (args.mode == "export"
             or (args.mode == "full" and bool(targets) and all(as_draft.values())))
    try:
        for i, target in enumerate(targets):
            # 输入本身是磁盘上的真实目录时视为剧集目录，而非草稿名
            # （os.path.join 遇到绝对路径会丢弃 JY_DRAFT_ROOT，需先排除）
            is_draft = as_draft[target]
            name = os.path.basename(target)
            st = _state_of(state, target)

            if st.get("status") == "done":
                # 已完成的剧默认跳过；显式 --profile 追加版本时不能整部跳过
                done_profs = {p for p, v in (st.get("profiles") or {}).items()
                              if (v or {}).get("status") == "done"}
                if not args.profile or set(args.profile) <= done_profs:
                    print(f"⏭️  跳过已完成: {name}")
                    continue

            # ---- 生成阶段（full/generate，且输入不是现成草稿名时）----
            if args.mode in ("full", "generate") and not is_draft:
                if not os.path.isdir(target):
                    print(f"✗ 目录不存在: {target}")
                    st["status"] = "failed"
                    failed.append(name)
                    continue
                draft = st.get("draft")
                if st.get("status") not in ("pipelined", "export_failed") or not draft:
                    print(f"\n{'='*60}\n🎬 [{i+1}/{len(targets)}] 串流生成: {name}\n{'='*60}")
                    toast("🎬 开始生成草稿", f"{name}（{i+1}/{len(targets)}）")
                    started = time.time()
                    st.update({"status": "pipelining", "started": started})
                    _save_state(state)
                    ok = _run_pipeline(target, _slug(name), overrides_of.get(target))
                    draft = _find_draft(name, started) if ok else None
                    if not draft:
                        st["status"] = "pipeline_failed"
                        _save_state(state)
                        toast("🎬 草稿生成失败", name)
                        failed.append(name)
                        continue
                    st.update({"status": "pipelined", "draft": draft})
                    _save_state(state)
                if args.mode == "generate":
                    print(f"✅ 草稿就绪（不导出）: {st['draft']}")
                    continue  # generate 模式零等待直接进入下一部
                time.sleep(args.stage_gap)  # full：生成→导出衔接缓冲
            elif args.mode in ("full", "generate") and is_draft:
                st.setdefault("draft", name)
                st["status"] = "pipelined"
                _save_state(state)
                if args.mode == "generate":
                    print(f"⏭️  已是现成草稿: {name}")
                    continue

            # ---- 导出阶段（full/export）----
            draft = st.get("draft") or (name if is_draft else _find_draft(name))
            if not draft:
                print(f"✗ 找不到对应串流草稿: {name}")
                st["status"] = "export_failed"
                _save_state(state)
                failed.append(name)
                continue
            st["draft"] = draft
            job_dir = _job_dir_for(target, is_draft)
            if job_dir:
                try:
                    jm.require_export_allowed(job_dir)
                except jm.ExportBlocked as e:
                    # 缺字幕未修复：该剧整轮跳过导出，只收尾通知一条，不进入失败重试计数
                    print(f"⛔ {name} {e}")
                    print("   请重剪这些集，确认字幕非空后重跑；未修复前不会导出。")
                    st["status"] = "blocked"
                    _save_state(state)
                    # full 模式的生成阶段已由 batch_runner 收尾通知；export 模式才在这里补一条
                    if args.mode == "export":
                        subtitle_guard.notify(job_dir)
                    failed.append(name)
                    continue
            # 多版本：基础版 a 用原串流草稿，其余用 `<基础草稿>_<版本>` 克隆草稿。
            # 版本真源见 lib/job_manifest.prompt_groups（prompts.json items[].group）。
            profiles = _resolve_export_profiles(job_dir, args.profile)
            prof_state = st.setdefault("profiles", {})
            todo: list[tuple[str, str]] = []
            job_failed = False
            for prof in profiles:
                dn = draft if prof == "a" else _account_draft_name(draft, prof)
                ps = prof_state.setdefault(prof, {})
                if _output_exists(dn):
                    print(f"⏭️  成品已存在，跳过导出: {dn}")
                    ps["status"] = "done"
                    continue
                if not os.path.isdir(os.path.join(JY_DRAFT_ROOT, dn)):
                    msg = f"缺版本草稿: {dn}"
                    if args.profile:
                        print(f"✗ {msg}（--profile 显式指定）")
                        ps.update({"status": "export_failed", "error": msg})
                        job_failed = True
                    else:
                        print(f"⏭️  {msg}（prompts.json 声明但尚未克隆，跳过）")
                    continue
                todo.append((prof, dn))
            if todo:
                st["status"] = "exporting"
                _save_state(state)
            for prof, dn in todo:
                ps = prof_state.setdefault(prof, {})
                print(f"\n📤 [{i+1}/{len(targets)}] 导出 {prof}: {dn}")
                if _run_export(dn, args.timeout, cold_start=args.cold_start or None,
                               job_dir=job_dir, keep_open=reuse):
                    out_mp4 = export_mp4_path(dn)
                    if _finalize_export(dn, out_mp4, job_dir, profile=prof):
                        ps.update({"status": "done", "output": os.path.basename(out_mp4)})
                        _save_state(state)
                        print(f"✅ 完成: {dn}")
                    else:
                        # 成品已落盘但台账没写成：保留 stale 标记，下次重跑而不是让 watcher 判冲突
                        ps["status"] = "export_failed"
                        job_failed = True
                        _save_state(state)
                        if reuse:
                            _close_jianying_after_batch(f"{name} 台账登记失败，收尾复用实例")
                else:
                    ps["status"] = "export_failed"
                    job_failed = True
                    _save_state(state)
                    if reuse:
                        # 失败后实例可能停在编辑页/导出视图：收干净再开下一部
                        _close_jianying_after_batch(f"{name} 导出失败，收尾复用实例")

            if job_failed:
                st["status"] = "export_failed"
                failed.append(name)
            else:
                st["status"] = "done"
                if todo:
                    print(f"✅ 完成: {name}")
            _save_state(state)

            if i < len(targets) - 1 and reset > 0:
                print(f"😴 休息 {reset:.0f}s 后继续下一部…")
                time.sleep(reset)
    finally:
        # 正常结束 / 报错 / 中断都统一收掉复用实例（单份导出本来就是子进程自己关）
        if reuse:
            _close_jianying_after_batch()

    ok = len(targets) - len(failed)
    summary = f"{args.mode} 模式成功 {ok}/{len(targets)}" + (f"，失败: {'、'.join(failed)}" if failed else "")
    toast("🏁 队列全部结束", summary)
    print(f"\n🏁 队列结束: {summary}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
