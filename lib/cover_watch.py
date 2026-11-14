"""cover_watch.py — cover_job 落盘监听 → 两阶段调度（剪辑 / 导出）。

触发条件（全部满足，契约见 references/handoff-contract.md）：
  1. 04_covers/ 有 4 张图（文件名可任意，ingest 自动识别改名；多账号可再加
     `<契约名>_<账号>` 或 `<账号>/<契约名>` 组，每个账号同样 4 张）
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
  python lib/cover_watch.py inspect --job "<cover_job>"   # 只读聚合视图（不写任何文件）
  python lib/cover_watch.py reopen --job "<cover_job>" --yes   # 人工重导（退回 covers_ready）
  python lib/cover_watch.py rerun --job "<cover_job>" --unit cover_bind --versions a --yes
  python lib/cover_watch.py rerun --job "<cover_job>" --unit edit --force --yes
  python lib/cover_watch.py rerun --rollback "<journal_dir>"   # 按 journal 回滚
  python lib/cover_watch.py rerun --resume "<journal_dir>" --yes  # 续跑未完成单元

配套文件：
  <repo>/pipeline/presets.json  watch 段（每轮热加载）：
      {"roots":[...], "jobs":[], "exclude":[], "settle":30, "interval":5,
       "export_policy":"immediate", "hold":false, "derive":[],
       "cold_start":true, "export_stall_timeout":1800, "retry_pause_after":5,
       "edit_stall_timeout_s":900, "new_job_grace_min":10}
      cold_start：每部剧导出前冷启动剪映（默认 true；手动用剪映时建议设 false）
      export_stall_timeout：导出无进展判定卡死的秒数（进度 json 每 30s 刷新一次）
      retry_pause_after：连续失败多少次后暂停该剧（默认 5，退避 1/2/4/8/16/30 分钟）
      edit_stall_timeout_s：edit 阶段心跳超时秒数（默认 900，0 关闭看门狗）
      new_job_grace_min：新剧首次发现的宽限分钟数（默认 10，0 关闭）
  <cover_job>/dispatch.json     豆包侧意图声明（ingest 归并进 job.json.workflow）
  <cover_job>/export_config.json 旧版一句话偏好（仍兼容：cut_s/derive）
  <cover_job>/.export_state.json 导出状态机单一真源（lib/export_state.py 唯一写者：
      status/attempts/consecutive_failures/next_retry_at/progress/phase/failure_class；
      旧的 .export_retry.json 首次读取时一次性迁入并备份，video.status 由它派生）
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job_manifest as jm  # noqa: E402
import export_lock  # noqa: E402
import export_state as es  # noqa: E402  # 导出状态机单一真源（.export_state.json）
import edit_state as eds  # noqa: E402  # edit 阶段失败退避/暂停（.edit_state.json）
import state_io as _sio  # noqa: E402  # 状态文件公共读写（只读查看用）
import failure_class as fc  # noqa: E402  # 第 3 项：失败分类器 + 自愈策略表

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BATCH_RUNNER = os.path.join(_REPO, "pipeline", "batch_runner.py")


def _ensure_streams_to_log() -> None:
    """托管启动（pythonw）时 stdout/stderr 为 None：把输出接到 pipeline/_work/watcher_console.log。

    隐藏控制台不等于丢日志：print 与未捕获 traceback（走 sys.stderr）都仍落盘。
    手工在终端里跑时 streams 正常，本函数不做事，行为不变。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    path = os.path.join(_REPO, "pipeline", "_work", "watcher_console.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stream = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_ensure_streams_to_log()

sys.path.insert(0, os.path.join(_REPO, "pipeline"))
import export_config as pc  # noqa: E402  # 本机配置（presets.json）唯一读取入口
from no_window import NO_WINDOW, install_default_hidden  # noqa: E402  # pythonw 托管下子进程一律不弹控制台
import series_runner as sr  # noqa: E402  # 复用 _run_export（preclean+导出+归拢）
import pipeline as jp  # noqa: E402  # 草稿目录/导出失效标记等公共能力
install_default_hidden()  # 无控制台托管时兜底隐藏所有子进程窗口（有控制台不生效）
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

# ── 跨轮重拉（拉起失败兜底）────────────────────────────────────
# 这一轮拉起剪映失败（早退重试后仍 died，或启动 timeout）→ 下一轮扫描优先重拉，
# 不等自然扫描顺序；连续多轮到顶后转成可见终态，绝不无限重试。
# 硬约束：不引入 sleep/退避，EARLY_DEATH_RETRIES=1 不变，同一次调用内不追加同轮重试。
RELAUNCH_STATE_NAME = ".relaunch_state.json"
RELAUNCH_MAX_ROUNDS = 3

_WORK_DIR = os.path.join(_REPO, "pipeline", "_work")
HEARTBEAT_NAME = "watcher_heartbeat.json"
HEARTBEAT_INTERVAL_S = 30.0
# 心跳超过 3 个周期未刷新 → 认为 watcher 已不在运行（放行语义按「没跑」处理）
HEARTBEAT_STALE_S = HEARTBEAT_INTERVAL_S * 3
# 人工放行前比对代码指纹的关键模块 = watcher 实际执行/拉起的代码（只覆盖调度/执行链，
# 避免无关文件误报）。导出链路上的模块也算：这些文件改了却不重启 watcher，
# --phase 放行后跑的仍是旧代码，所以必须一并纳入。
CODE_FINGERPRINT_FILES = (
    "lib/cover_watch.py",
    "lib/job_manifest.py",
    "lib/edit_state.py",
    "lib/export_state.py",
    "lib/failure_class.py",
    "lib/export_lock.py",        # 导出互斥锁/活锁判定
    "lib/state_io.py",           # 状态文件原子写（export_state/edit_state 共用）
    "lib/subtitle_guard.py",     # 导出前置的缺字幕通知
    "pipeline/batch_runner.py",
    "pipeline/pipeline.py",
    "pipeline/preset_ops.py",
    "pipeline/series_runner.py",  # 导出编排：preclean/子进程/超时/归拢
    "pipeline/export_config.py",  # 导出落点与导出根目录解析（presets.json）
    "pipeline/no_window.py",      # 导出子进程 creationflags/env
    "pipeline/verify_export.py",  # 导出后质检（watcher 以子进程调用）
    # 真正执行导出的外部脚本（series_runner 以子进程拉起）
    ".trae/skills/jianying-editor/examples/robust_auto_export.py",
    ".trae/skills/jianying-editor/examples/jy_instance.py",
)


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
    # 多账号：基础 4 张之外允许任意多张账号图（8/12 张都行），但每个出现过的
    # 账号必须 4 张齐全，避免只换一半封面就发出去。
    reasons.extend(jm.account_cover_errors(job_dir))
    if not os.path.isfile(os.path.join(job_dir, "03_prompts", "prompts.json")):
        reasons.append("缺 03_prompts/prompts.json")
    try:
        job = jm.load_job(os.path.join(job_dir, "job.json"))
    except Exception as e:
        reasons.append(f"job.json 不可解析: {e}")
        return False, reasons
    if (job.get("titles") or {}).get("gate") != "done":
        reasons.append("titles.gate != done")
    if (job.get("derived") or {}).get("covers_stale"):
        # 重选后旧封面与旧标题绑定：必须先重建 04_covers，否则剪辑会嵌入过期封面
        reasons.append("重选后 04_covers 需重新生成（derived.covers_stale=true）")
    stage = jm.canon_stage(job.get("stage"))
    if stage == "archived":
        # 归档剧显式硬拦：即使有人放宽 TRIGGER_STAGES 或误改 job.json，也不再进入调度
        reasons.append("stage=archived（已归档，不再调度）")
        return False, reasons
    video = job.get("video") or {}
    if str(video.get("export_state") or "").lower() == "cleaned":
        # 成品已清理的剧不重导：clean 之后草稿可能还在，但重导会产生"已清理又冒出新成品"
        reasons.append("video.export_state=cleaned（成品已清理，不再重导）")
        return False, reasons
    # 多账号：基础账号导完会把剧钉在 video_ready + status=done，是否还有事要做
    # 只能看"声明版本里谁还没成品"。算一次，stage 与 status 两处判定共用。
    pending_accounts = _pending_account_outputs(job, job_dir)
    if stage not in TRIGGER_STAGES:
        if stage == "video_ready" and pending_accounts:
            pass  # 已导完的剧补新版本：放行（其余前置条件仍已通过）
        elif stage == "video_ready":
            reasons.append("stage=video_ready 但无待补版本（所有声明版本均已导出）")
        else:
            reasons.append(
                f"stage={job.get('stage')} 不在可触发集合"
                f"（仅 {', '.join(TRIGGER_STAGES)} 及 video_ready+待补版本）")
    if video.get("status") == "done":
        # 多账号：基础账号导完就把整部剧钉成 done 会挡住后来新增的账号草稿/成品。
        # 只在 job 自己声明了多预设组且确有未完成版本时放行（避免全导完后反复评估）。
        if not pending_accounts:
            reasons.append("video 已导出（status=done）且无待补版本")
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
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.strip()
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


def _claim_lock(path: str, data: dict[str, Any]) -> bool:
    """原子抢占：O_CREAT|O_EXCL 创建成功才算拿到锁。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except OSError:
        # 写完失败：清掉自己刚创建的文件，避免留下不可读锁
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return True


def acquire_lock(job_dir: str, phase: str = "triggered") -> bool:
    """原子抢占 .watcher.lock：成功 True；活锁 False；陈旧锁接管一次。"""
    path = lock_path(job_dir)
    data = {"pid": os.getpid(), "job": job_dir, "phase": phase,
            "started": jm.now_iso(), "pipeline_pid": None}
    if _claim_lock(path, data):
        return True

    existing = read_lock(job_dir)
    if not existing:
        return False
    pid = int(existing.get("pid") or 0)
    if pid > 0 and not _pid_alive(pid):
        # 陈旧锁：再读一次确认 pid 未变，删掉后重试一次抢占
        again = read_lock(job_dir)
        if not again or int(again.get("pid") or 0) != pid:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        return _claim_lock(path, data)
    return False


def release_lock(job_dir: str) -> None:
    """只释放自己持有的锁：pid 不是本进程就不删。"""
    data = read_lock(job_dir)
    if not data or int(data.get("pid") or 0) != os.getpid():
        return
    try:
        os.remove(lock_path(job_dir))
    except OSError:
        pass


def _write_lock(job_dir: str, phase: str, **extra: Any) -> None:
    """更新本进程持有的 .watcher.lock 字段（phase / pipeline_pid 等）。

    锁的创建与陈旧接管由 acquire_lock() 原子完成；这里只做原地字段更新，
    且只在锁属于本进程时写，避免误改别人的锁。写失败向上抛，由调用方处理。
    """
    data = read_lock(job_dir)
    if not data or int(data.get("pid") or 0) != os.getpid():
        return
    data["phase"] = phase
    data.update(extra)
    data["updated_at"] = jm.now_iso()
    _sio.atomic_write(job_dir, LOCK_NAME, data)


LOG_MAX_BYTES = 10 * 1024 * 1024   # watcher.log 单文件上限（10 MB）
LOG_BACKUP_COUNT = 5               # 保留 watcher.log.1 ~ watcher.log.5


def _rotate_log(log_path: str) -> None:
    """按大小轮转 watcher.log：超限则删 .5 → .4→.5 → … → .1→.2 → log→.1。

    只在写入前调用；任一步失败（被占用/权限/跨盘）记一行 stderr 并放弃本次轮转，
    继续往当前文件追加，绝不抛异常打断调度。用 os.replace（Windows 覆盖语义更稳）。
    """
    try:
        if os.path.getsize(log_path) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    backups = [f"{log_path}.{i}" for i in range(1, LOG_BACKUP_COUNT + 1)]
    oldest = backups[-1]
    try:
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(LOG_BACKUP_COUNT - 1, 0, -1):
            src, dst = backups[i - 1], backups[i]
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(log_path, backups[0])
    except OSError as e:
        print(f"[warn] watcher.log 轮转失败，继续写当前文件: {e}", file=sys.stderr)


def _log(job_dir: str, msg: str) -> None:
    """唯一写入口：所有 watcher.log 写入都走这里（先按大小轮转，再追加）。"""
    line = f"{jm.now_iso()} {msg}"
    print(line)
    path = os.path.join(job_dir, "watcher.log")
    _rotate_log(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _code_fingerprint() -> str:
    """关键模块内容哈希（16 进制短串）：watcher 启动时写进心跳，供放行前比对。"""
    h = hashlib.sha256()
    for rel in CODE_FINGERPRINT_FILES:
        p = os.path.join(_REPO, rel.replace("/", os.sep))
        h.update(rel.encode("utf-8"))
        try:
            with open(p, "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:12]


class _WatcherHeartbeat:
    """进程心跳：后台 daemon 线程每 30s 原子写，主循环按轮次更新上下文。

    心跳文件放 pipeline/_work/watcher_heartbeat.json（已被 .gitignore 忽略），
    供 watcher_supervisor.ps1 判定「进程活着但卡死」。写失败只告警，不影响调度。
    """

    def __init__(self, work_dir: str = _WORK_DIR,
                 interval: float = HEARTBEAT_INTERVAL_S) -> None:
        self._work_dir = work_dir
        self._interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state: dict[str, Any] = {
            "stage": "starting", "job": None, "phase": None, "round": 0,
        }
        self._started_ts = time.time()
        self._code_fingerprint = _code_fingerprint()
        self._fail_streak = 0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._write()  # 先同步写一次，避免 supervisor 在启动初期读到旧心跳
        print(f"[hb] watcher 启动 pid={os.getpid()} 代码指纹={self._code_fingerprint}")
        self._thread = threading.Thread(
            target=self._loop, name="watcher-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)
        self._write()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._write()

    def _write(self) -> None:
        with self._lock:
            payload = {
                "pid": os.getpid(),
                "updated_ts": time.time(),
                "started_ts": self._started_ts,
                "code_fingerprint": self._code_fingerprint,
                "stage": self._state.get("stage"),
                "job": self._state.get("job"),
                "phase": self._state.get("phase"),
                "round": self._state.get("round"),
            }
        # 写失败不能把心跳永久停更：同一轮内带退避重试 3 次，仍失败则下一周期
        # （30s 后）继续写；只有连续失败才告警，避免每轮刷屏。
        last: Optional[OSError] = None
        for attempt in range(3):
            try:
                _sio.atomic_write(self._work_dir, HEARTBEAT_NAME, payload)
            except OSError as e:
                last = e
                time.sleep(0.1 * (attempt + 1))
                continue
            if self._fail_streak:
                print(f"[hb] 心跳写入恢复（此前连续失败 {self._fail_streak} 次）")
                self._fail_streak = 0
            return
        self._fail_streak += 1
        if self._fail_streak <= 3 or self._fail_streak % 10 == 0:
            print(f"[warn] 心跳写入失败（连续 {self._fail_streak} 次，下一周期重试）: {last}",
                  file=sys.stderr)


def check_release_safe(phase: str, *, heartbeat_path: Optional[str] = None,
                       now: Optional[float] = None,
                       fingerprint: Optional[str] = None) -> Optional[str]:
    """人工 --phase 放行（清 paused/强制阶段）前的代码一致性校验。

    返回拒绝原因；None 表示放行。语义区分三种情况：
      - 心跳缺失/不可解析/已过期（超过 3 个周期）或 pid 已退出 → watcher 没在跑，放行；
      - 心跳新鲜且进程存活、但指纹缺失（旧版心跳）或不一致 → 拒绝，提示先重启；
      - 心跳新鲜、指纹一致 → 放行。
    """
    path = heartbeat_path or os.path.join(_WORK_DIR, HEARTBEAT_NAME)
    try:
        with open(path, encoding="utf-8") as f:
            hb = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(hb, dict):
        return None
    now_ts = time.time() if now is None else now
    try:
        updated = float(hb.get("updated_ts") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    if now_ts - updated > HEARTBEAT_STALE_S:
        return None  # 心跳过期：watcher 已不在运行
    try:
        pid = int(hb.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0 or not _pid_alive(pid):
        return None  # pid 已退出：watcher 没在跑
    old_fp = str(hb.get("code_fingerprint") or "").strip()
    cur_fp = fingerprint or _code_fingerprint()
    if not old_fp:
        return (f"watcher（pid={pid}）心跳未携带代码指纹，运行的是旧代码；"
                f"拒绝 {phase} 放行。请先重启 watcher")
    if old_fp != cur_fp:
        return (f"watcher（pid={pid}）仍运行旧代码（心跳指纹 {old_fp} ≠ 当前 {cur_fp}）；"
                f"拒绝 {phase} 放行。请先重启 watcher")
    return None


# ── 导出失败退避 / 暂停（单一真源适配层）──────────────────────────
# 真源是 cover_job/.export_state.json（lib/export_state.py 唯一写者）。
# 旧的 .export_retry.json 只作为迁移来源，首次读取时一次性迁移并备份，之后不再写。
# 下面三个函数保留旧签名，全部改为读写真源，保证既有调用方与回归用例不变。

RETRY_NAME = es.LEGACY_RETRY_NAME          # ".export_retry.json"（仅迁移读取）
_BACKOFF_S = es.BACKOFF_S                  # 1 / 2 / 4 / 8 / 16 / 30 分钟
RETRY_PAUSE_AFTER = es.DEFAULT_PAUSE_AFTER


def read_retry(job_dir: str) -> dict[str, Any]:
    """兼容读：把真源映射成旧 {fails, next_ts, paused} 形状。"""
    st = es.load(job_dir)
    return {"fails": int(st.get("consecutive_failures") or 0),
            "next_ts": float(st.get("next_retry_at") or 0),
            "paused": st.get("status") == "paused"}


def retry_gate(job_dir: str, phase_override: Optional[str] = None,
               progress_mtime: float = 0.0,
               migrate_ctx: Optional[dict[str, Any]] = None,
               profile: Optional[str] = None) -> Optional[str]:
    """导出闸门（读真源）：返回等待/暂停原因；None 表示可导。

    profile 非空时按版本独立判断退避/暂停；exporting 仍是全局串行。
    --phase export 人工放行。
    """
    return es.gate(job_dir, phase_override=phase_override,
                   progress_mtime=progress_mtime, migrate_ctx=migrate_ctx,
                   profile=profile)


def note_export_result(job_dir: str, name: str, ok: bool, *,
                       pause_after: int = RETRY_PAUSE_AFTER,
                       error: Optional[str] = None,
                       failure_class: str = "unknown",
                       retryable: bool = True,
                       phase: Optional[str] = None,
                       profile: Optional[str] = None) -> None:
    """记录导出结果（写真源）：成功置 done 并清零；失败按分类走退避/暂停或终态。

    第 3 项：不可重试类（asset_missing/disk_full，retryable=False）直接置终态
    failed、不计数并 toast，避免空转；可重试类按 1/2/4/8/16/30 分钟退避/暂停。
    """
    if ok:
        es.succeed(job_dir, profile=profile)
        _log(job_dir, "export ok（真源置 done，计数清零）" if not profile
             else f"export ok（版本 {profile} 置 done）")
        return
    st = es.fail(job_dir, error, pause_after=pause_after,
                 failure_class=failure_class, retryable=retryable, phase=phase,
                 profile=profile)
    if st.get("status") == "failed":
        _log(job_dir, f"export 硬失败[{failure_class}]，置终态不重试：{error}")
        try:
            toast("⛔ 导出硬失败（不重试）",
                  f"{name}：{failure_class}，需人工处理\n{error or ''}", icon="error")
        except Exception:
            pass
        return
    fails = int(st.get("consecutive_failures") or 0)
    paused = st.get("status") == "paused"
    if paused:
        _log(job_dir, f"export 失败第 {fails} 次（已暂停该剧）")
    else:
        delay = es.BACKOFF_S[min(fails - 1, len(es.BACKOFF_S) - 1)]
        _log(job_dir, f"export 失败第 {fails} 次[{failure_class}]，{delay}s 后重试")
    try:
        if paused:
            toast("⛔ 导出连续失败已暂停", f"{name}：连续 {fails} 次失败，需人工排查", icon="error")
        else:
            delay = es.BACKOFF_S[min(fails - 1, len(es.BACKOFF_S) - 1)]
            toast("⚠️ 导出失败，将退避重试", f"{name}：第 {fails} 次失败，{delay}s 后重试",
                  icon="warning")
    except Exception:
        pass


# ── 意图与计划推导 ──────────────────────────────────────────────

def _draft_root() -> str:
    return (os.environ.get("JY_PROJECTS_ROOT", "").strip()
            or os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "JianyingPro", "User Data", "Projects", "com.lveditor.draft"))


def _is_stream_draft_name(name: str, series_name: str) -> bool:
    """是否该剧的串流草稿：`<剧名>_第NN集串流` / `<剧名>_第NN-NN集串流`（精确匹配）。

    不用 `<剧名>*串流` 前缀 glob：那会让「剧名 A」命中「剧名 A2」，
    也会让 `_b` 克隆在宽松规则下被误算进来。集数区间是唯一允许变化的部分。
    """
    return bool(re.fullmatch(
        rf"{re.escape(str(series_name))}_第\d+(?:-\d+)?集串流", str(name or "")))


def find_stream_draft(series_name: str) -> Optional[str]:
    """定位串流草稿目录（精确匹配剧名 + 集数区间，取 mtime 最新）。"""
    root = _draft_root()
    try:
        names = os.listdir(root)
    except OSError:
        return None
    cands = [os.path.join(root, n) for n in names
             if _is_stream_draft_name(n, series_name)
             and os.path.isdir(os.path.join(root, n))]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _draft_exported(draft_name: str) -> bool:
    """该草稿是否已有完成导出的成品（fail-closed：证据不足一律判「未导出」）。

    草稿目录带 _export_stale.json（重新构建/克隆发布后写入）时一律视为未导出：
    否则改完预设重建同名草稿，旧 mp4 会被当成新成品跳过，静默发布旧视频。

    正向证据只认三种，拿不到就判未导出（宁可重导一次，也不把来路不明的旧 mp4
    当成新成品跳过）：
      ① freshness=valid / manual_confirmed —— sidecar 或人工确认，明确新鲜；
      ② freshness=legacy —— 命中 data/freshness_legacy_baseline.json 冻结基线，
         sidecar 上线前导出的历史产物，无 sidecar 属正常（白名单只读、不自动扩充）；
      ③ 成品旁 progress.json 的 done 为真 —— 导出脚本写的完成标记。
    旧版成品若连 progress 都没有，不再按「有 mp4 即完成」放行。
    """
    if jp.export_is_stale(os.path.join(_draft_root(), draft_name)):
        return False
    out = pc.export_mp4_path(draft_name)
    if not os.path.isfile(out):
        return False
    try:
        import freshness
        jd = freshness.job_dir_for_draft(draft_name)
        if jd:
            ver = freshness.version_for_draft(jd, draft_name)
            state = freshness.freshness_of(jd, ver, consumer="scheduler").get("state")
            if state in ("valid", "manual_confirmed", "legacy"):
                return True
    except Exception:
        pass
    pj = out + ".progress.json"
    try:
        with open(pj, encoding="utf-8") as f:
            return json.load(f).get("done") is True
    except Exception:
        return False  # fail-closed：无 progress 证据不再视为已完成


def _account_draft_name(base_draft: str, prof: str) -> str:
    """附加账号草稿命名约定（与 preset_ops.clone_draft_with_presets 保持一致）。"""
    return f"{base_draft}_{prof}"


def _profile_of_draft(base_draft: str, draft_name: str) -> str:
    """草稿名 → 版本名：基础草稿是 a，克隆草稿取 `_<版本>` 后缀。

    后缀必须是合法版本名 `[a-z][a-z0-9]{0,7}`。`_bb` / `_b_2` / `_B` 这类非法后缀
    一律显式报错，绝不降级当成 `b`（否则不同目录会被当成同一版本互相覆盖）。
    """
    if not draft_name or draft_name == base_draft:
        return "a"
    prefix = base_draft + "_"
    if not draft_name.startswith(prefix):
        return "a"
    suffix = draft_name[len(prefix):]
    if not re.fullmatch(r"[a-z][a-z0-9]{0,7}", suffix):
        raise ValueError(
            f"草稿版本后缀非法：{draft_name}（基础草稿 {base_draft}，后缀 {suffix!r}）；"
            "版本名只允许 [a-z][a-z0-9]{0,7}，请改名或删除该目录后再重试")
    return suffix


def _artifact_paths(draft_name: str) -> dict[str, str]:
    """成品三件套路径（与 pipeline.archive_stale_outputs / series_runner 同口径）。"""
    out = pc.export_mp4_path(draft_name)
    out_dir = os.path.dirname(os.path.abspath(out))
    return {"mp4": out,
            "封面": os.path.join(out_dir, f"{draft_name}-封面.jpg"),
            "progress": out + ".progress.json"}


def _progress_side_path(draft_name: str) -> str:
    """导出运行中的进度侧写（成品目录同级），完成后会被 robust_auto_export 删除。"""
    out = pc.export_mp4_path(draft_name)
    out_dir = os.path.dirname(os.path.abspath(out))
    return os.path.join(os.path.dirname(out_dir), os.path.basename(out_dir) + ".progress.json")


def _missing_artifacts(draft_name: str, *, strict_progress: bool = False) -> list[str]:
    """成品三件套缺失项（mp4 / -封面.jpg / progress）。

    strict_progress=True 只认成品目录内那份（mp4.progress.json）：导出成功收尾一定
    会写它并删侧写，用于"导出后判成败"；冲突巡检放宽到侧写，避免历史成品误报。
    """
    paths = _artifact_paths(draft_name)
    missing = [k for k in ("mp4", "封面") if not os.path.isfile(paths[k])]
    progress_ok = os.path.isfile(paths["progress"]) or (
        not strict_progress and os.path.isfile(_progress_side_path(draft_name)))
    if not progress_ok:
        missing.append("progress")
    return missing


def _success_evidence_missing(mp4: str, started_at: float | None = None) -> list[str]:
    """完成收尾前的硬证据：正式名 mp4 真在（且是本轮产物）+ 旁 progress 的 done 为真。

    导出过程中文件是 `<UUID>.mp4`，收尾 `--move` 才改成草稿名。因此只认正式名
    路径，并额外用 mtime 排除"旧成品/旧 progress 冒充本轮结果"。
    """
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
    pj = mp4 + ".progress.json"
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


def _output_conflicts(job: dict[str, Any], base_draft: str,
                      profiles: list[str]) -> list[dict[str, Any]]:
    """video.outputs[<版本>].state ⇄ 磁盘三件套双向校验（不再默认信磁盘）。

    两类冲突：
      ledger_exported_disk_missing —— 台账 exported 但磁盘缺件：该版本不算已导出，
        计划 blocked 等人工确认（补齐文件 / 强制重导 / 修正台账）
      disk_present_ledger_missing —— 磁盘三件套齐全、台账却没有 exported 记录：
        同样 blocked，不默认信磁盘（补登记 record-video，或删旧成品重导）。
    基础版（a）在 outputs 缺失时用顶层 video.export_state/export_file 兜底，
    兼容 outputs 台账引入之前的老 job。
    """
    video = job.get("video") or {}
    outputs = video.get("outputs") or {}
    out: list[dict[str, Any]] = []
    for prof in profiles:
        dn = base_draft if prof == "a" else _account_draft_name(base_draft, prof)
        missing = _missing_artifacts(dn)
        ent = outputs.get(prof) if isinstance(outputs.get(prof), dict) else {}
        state = str((ent or {}).get("state") or "").strip().lower()
        if prof == "a" and not state:
            state = str(video.get("export_state") or "").strip().lower()
        if state == "exported" and missing:
            out.append({"profile": prof, "kind": "ledger_exported_disk_missing",
                        "draft": dn, "missing": missing,
                        "detail": f"台账 exported，磁盘缺 {', '.join(missing)}",
                        "hint": "补齐缺失文件；确认要重导用 --phase export，或修正台账后再跑"})
        elif state != "exported" and not missing:
            out.append({"profile": prof, "kind": "disk_present_ledger_missing",
                        "draft": dn, "missing": [],
                        "detail": f"磁盘三件套齐全但台账 state={state or 'null'}",
                        "hint": "确认成品有效后用 record-video 补登记；或删除旧成品后重导"})
    return out


_LOG_CONFLICTS: dict[str, str] = {}


def _log_output_conflicts(job_dir: str, conflicts: list[dict[str, Any]]) -> None:
    """同一组冲突只在变化时落日志，避免每轮刷屏。"""
    sig = "；".join(f"{c['profile']}:{c['kind']}:{','.join(c.get('missing') or [])}"
                    for c in conflicts)
    key = job_dir.lower()
    if _LOG_CONFLICTS.get(key) == sig:
        return
    _LOG_CONFLICTS[key] = sig
    for c in conflicts:
        _log(job_dir, f"⚠️ 成品台账冲突 [{c['profile']}] {c['detail']}（{c['hint']}）")


def _missing_account_drafts(base_draft: str, profiles: list[str]) -> list[str]:
    """多账号预设里尚未生成的克隆草稿（基础草稿不在此列）。"""
    return [d for d in (_account_draft_name(base_draft, p) for p in profiles[1:])
            if not os.path.isdir(os.path.join(_draft_root(), d))]



def _export_targets(base_draft: str, profiles: list[str],
                    base_profile: str = "a") -> list[str]:
    """多账号预设：按「版本 → 草稿名」直接映射。

    基础版本用基础草稿名，其余版本用 `<基础草稿>_<版本>` 克隆名；缺失的克隆跳过。
    base_profile 必须由调用方给（取 job 声明集合的首个），不能假定 profiles[0]
    就是基础版本——`--profile b` 过滤后 profiles 只剩 b。
    """
    root = _draft_root()
    targets = []
    for p in profiles:
        if p == base_profile:
            targets.append(base_draft)
            continue
        name = _account_draft_name(base_draft, p)
        if os.path.isdir(os.path.join(root, name)):
            targets.append(name)
    return targets


def _prompt_groups(job_dir: str) -> list[str]:
    """版本声明的唯一真源：03_prompts/prompts.json 的 items[].group（a 优先排序）。"""
    pp = os.path.join(job_dir, "03_prompts", "prompts.json")
    if not os.path.isfile(pp):
        return []
    try:
        with open(pp, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    groups: set[str] = set()
    for it in (data.get("items") or []):
        g = str((it or {}).get("group") or "").strip().lower()
        if re.fullmatch(r"[a-z][a-z0-9]{0,7}", g):
            groups.add(g)
    return sorted(groups, key=lambda g: (g != "a", g))


def _norm_group_name(value: Any) -> str:
    """组名归一：A / a_group / b组 / presets_b → a / a / b / b（非法返回空串）。"""
    g = str(value or "").strip().lower()
    for suf in ("_group", "-group", "组"):
        if g.endswith(suf):
            g = g[: -len(suf)].strip()
            break
    if g.startswith("presets_"):
        g = g[len("presets_"):].strip()
    return g if re.fullmatch(r"[a-z][a-z0-9]{0,7}", g) else ""


def _norm_groups(values: Any) -> list[str]:
    """组名列表归一 + 去重 + a 优先排序。"""
    out: list[str] = []
    for v in values or []:
        g = _norm_group_name(v)
        if g and g not in out:
            out.append(g)
    return sorted(out, key=lambda g: (g != "a", g))


def _job_groups(job: dict[str, Any]) -> list[str]:
    """job.json 自己声明的版本组（job 优先的取值顺序）。

    1. titles.selected_groups[].group（selection.json 归并进来的多组，最权威）
    2. prompts.items[].group（ingest 从 prompts.json 同步进 job 的副本）
    3. workflow.preset_profile（显式版本声明，支持 "a,b"）
    前一档取到组就不再往下一档补，避免陈旧字段虚增版本。
    """
    titles = job.get("titles") or {}
    sources = (
        [(g or {}).get("group") for g in (titles.get("selected_groups") or [])],
        [(it or {}).get("group") for it in ((job.get("prompts") or {}).get("items") or [])],
        pc.parse_preset_profiles((job.get("workflow") or {}).get("preset_profile")),
    )
    for raw in sources:
        groups = _norm_groups(raw)
        if groups:
            return groups
    return []


def _profile_groups(job: dict[str, Any],
                    job_dir: Optional[str] = None) -> tuple[list[str], str, list[str]]:
    """版本声明：job 优先、prompts 兜底（严格互斥，**不取并集**）。

    返回 (组列表, 判定来源, prompts 多出的组)。
    - job 任一档取到组 → 只用 job 的组集合；prompts 多出的组只作告警，不参与判定
      （重选标题后残留的旧 prompts 不能把版本虚增）。
    - job 完全没有组声明 → 整体回落 prompts.json；再没有 → default(a)。
    来源取值固定为 job / prompts / default。
    """
    job_groups = _job_groups(job)
    prompt_groups = _prompt_groups(job_dir) if job_dir else []
    if job_groups:
        extra = [g for g in prompt_groups if g not in job_groups]
        return job_groups, "job", extra
    if prompt_groups:
        return prompt_groups, "prompts", []
    return ["a"], "default", []


def _job_profiles(job: dict[str, Any], job_dir: Optional[str] = None) -> list[str]:
    """声明的版本组（含缺预设的组，供计划展示/台账登记）。"""
    return _profile_groups(job, job_dir)[0]


def _blocked_profiles(profiles: list[str]) -> list[dict[str, str]]:
    """声明了但缺 presets_<组> 的版本：标记 blocked，不阻断其余版本。"""
    available = _available_preset_profiles()
    return [{"profile": p, "reason": "missing_presets", "missing": f"presets_{p}"}
            for p in profiles if p not in available]


def _runnable_profiles(profiles: list[str]) -> list[str]:
    """可实际生成/导出的版本（剔除缺预设的组）。"""
    blocked = {b["profile"] for b in _blocked_profiles(profiles)}
    return [p for p in profiles if p not in blocked]


_LOG_PROFILE_SOURCE: dict[str, str] = {}


def _log_profile_source(job_dir: str, source: str) -> bool:
    """同一部剧的判定来源只在变化时落日志，避免每轮刷屏。"""
    key = job_dir.lower()
    if _LOG_PROFILE_SOURCE.get(key) == source:
        return False
    _LOG_PROFILE_SOURCE[key] = source
    return True


def _pending_account_outputs(job: dict[str, Any], job_dir: str) -> list[str]:
    """job 声明的版本里，仍缺草稿或缺成品的草稿名（含基础版台账↔磁盘冲突）。

    基础版也参与：台账说 exported、磁盘三件套却不全的剧，stage 往往已被钉在
    video_ready + status=done，若不在这里算成"待补"，check_trigger 会直接判
    「无待补版本」再也不看它（成品被删也永远不重导）。
    """
    profiles = _runnable_profiles(_job_profiles(job, job_dir))
    if not profiles:
        return []
    name = os.path.basename(os.path.dirname(job_dir.rstrip("\\/")))
    draft = find_stream_draft(name)
    if not draft:
        return []
    base = os.path.basename(draft.rstrip("\\/"))
    pending: list[str] = []
    # 两类台账冲突都算"仍有事要做"：否则 stage 会被钉在 video_ready/status=done，
    # check_trigger 判"无待补版本"后连 build_plan 都不进，冲突永远不会在计划里露出来。
    for c in _output_conflicts(job, base, profiles):
        dn = base if c["profile"] == "a" else _account_draft_name(base, c["profile"])
        if dn not in pending:
            pending.append(dn)
    if len(profiles) < 2:
        return pending
    pending += [d for d in _missing_account_drafts(base, profiles) if d not in pending]
    for p in profiles[1:]:
        dn = _account_draft_name(base, p)
        if dn not in pending and not _draft_exported(dn):
            pending.append(dn)
    return pending


def _intent_of(job: dict[str, Any]) -> dict[str, Any]:
    wf = dict(job.get("workflow") or {})
    for k, v in jm.WORKFLOW_DEFAULTS.items():
        wf.setdefault(k, v)
    return wf


def _progress_info(out_mp4: str) -> tuple[float, Optional[str], Optional[int]]:
    """读导出进度侧写 .progress.json：返回 (mtime, status, percent)。

    运行中在剧名目录同级（side），完成后在 mp4 旁（final）；取较新的一份。
    该文件是剪映导出子进程的运行时产物，只作真源的心跳/进度输入，不是真源本身。
    """
    out_dir = os.path.dirname(os.path.abspath(out_mp4))
    side = os.path.join(os.path.dirname(out_dir),
                        os.path.basename(out_dir) + ".progress.json")
    final = out_mp4 + ".progress.json"
    cands = [p for p in (final, side) if os.path.isfile(p)]
    if not cands:
        return 0.0, None, None
    p = max(cands, key=os.path.getmtime)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return 0.0, None, None
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return mtime, None, None
    return mtime, rec.get("status"), es.parse_percent(rec.get("percent"))


def _read_progress_failure(out_mp4: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """从 .progress.json 读回导出子进程写下的 (failure_class, phase, last_error)。

    robust_auto_export 已用同一分类器在失败时落盘 failure_class；这里读回作为权威来源。
    缺失时返回 (None, None, None)，由调用方退回对 last_error 重新分类。
    """
    out_dir = os.path.dirname(os.path.abspath(out_mp4))
    side = os.path.join(os.path.dirname(out_dir),
                        os.path.basename(out_dir) + ".progress.json")
    final = out_mp4 + ".progress.json"
    cands = [p for p in (final, side) if os.path.isfile(p)]
    if not cands:
        return None, None, None
    p = max(cands, key=os.path.getmtime)
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None, None, None
    errs = rec.get("errors") or []
    last = str(errs[-1]) if errs else None
    return rec.get("failure_class"), rec.get("phase"), last


def _read_progress_recovery(out_mp4: str) -> Optional[str]:
    """从 .progress.json 读回导出子进程实际执行的自愈动作 recovery_action。

    旧版本产物无该字段时返回 None（不报错、不写空值）。
    """
    out_dir = os.path.dirname(os.path.abspath(out_mp4))
    side = os.path.join(os.path.dirname(out_dir),
                        os.path.basename(out_dir) + ".progress.json")
    final = out_mp4 + ".progress.json"
    cands = [p for p in (final, side) if os.path.isfile(p)]
    if not cands:
        return None
    p = max(cands, key=os.path.getmtime)
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    return rec.get("recovery_action") or None


# ── 跨轮重拉：状态读写 ──────────────────────────────────────────
def relaunch_state(job_dir: str) -> Optional[dict[str, Any]]:
    """读 .relaunch_state.json；不存在/损坏返回 None（损坏按无状态处理，不阻断调度）。"""
    try:
        with open(os.path.join(job_dir, RELAUNCH_STATE_NAME), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) and st.get("status") in ("pending", "exhausted") else None


def _relaunch_save(job_dir: str, st: dict[str, Any]) -> None:
    path = os.path.join(job_dir, RELAUNCH_STATE_NAME)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        _log(job_dir, f"跨轮重拉状态写入失败（不影响本轮）: {e}")


def _relaunch_clear(job_dir: str, why: str = "") -> None:
    """清除跨轮状态：拉起成功、或本次失败不再是拉起失败时调用。"""
    path = os.path.join(job_dir, RELAUNCH_STATE_NAME)
    if not os.path.exists(path):
        return
    try:
        os.remove(path)
        _log(job_dir, "跨轮重拉状态已清除" + (f"：{why}" if why else ""))
    except OSError:
        pass


def relaunch_note_failure(job_dir: str, cfg: dict[str, Any], *,
                          error: str = "", failure_class: str = "") -> dict[str, Any]:
    """拉起失败 → 记一轮。未到顶返回 pending（下一轮重拉），到顶返回 exhausted。

    rounds = 已排定的跨轮重拉轮次（0 → 1 表示"下一轮重拉第 1 轮"）。
    到顶时 rounds 停在 max_rounds+1，便于汇总表达"重拉 N 轮仍失败"。
    """
    st = relaunch_state(job_dir) or {}
    max_rounds = max(1, int(cfg.get("relaunch_max_rounds", RELAUNCH_MAX_ROUNDS)))
    rounds = int(st.get("rounds") or 0) + 1
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    new = {
        "status": "pending" if rounds <= max_rounds else "exhausted",
        "rounds": rounds,
        "max_rounds": max_rounds,
        "job": os.path.basename(os.path.dirname(job_dir.rstrip("\\/"))),
        "first_at": st.get("first_at") or now,
        "last_at": now,
        "last_class": failure_class or st.get("last_class") or "",
        "last_error": (error or "")[-500:],
    }
    _relaunch_save(job_dir, new)
    return new


def _ledger_env(job_dir: str, name: str, round_no: int) -> None:
    """给导出子进程打台账标签：jy_instance → jianying_launch 读这两个变量写进台账。"""
    os.environ["JY_LEDGER_JOB"] = name
    os.environ["JY_LEDGER_JOB_ROUND"] = str(int(round_no or 0))


def _migrate_ctx(job: dict[str, Any]) -> dict[str, Any]:
    """真源缺失时的迁移上下文：从 job.json 提取已导/阶段信号（只读）。"""
    return {"video_status": (job.get("video") or {}).get("status"),
            "stage": job.get("stage")}


# 裁剪分歧去重：落盘到 cover_job/.cut_conflict.json（原子写），跨进程有效。
# 两个独立通道，各自按「值变化才再做一次」去重：
#   logged   —— 已写进 watcher.log（真实执行轮次：持续 watcher 与 --once）
#   notified —— 已弹过桌面通知（仅持续 watcher 真实轮次）
# 分开记：--once 记日志不吞掉首轮桌面通知，持久 watcher 也不会每轮刷日志。
CUT_CONFLICT_NAME = ".cut_conflict.json"


def _read_cut_conflict(job_dir: str) -> dict[str, Any]:
    try:
        with open(os.path.join(job_dir, CUT_CONFLICT_NAME), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cut_conflict_changed(job_dir: str, kind: str, wf_cut: float, ec_cut: float) -> bool:
    prev = _read_cut_conflict(job_dir).get(kind)
    if not isinstance(prev, dict) or not prev.get("done"):
        return True
    return not (prev.get("wf_cut") == wf_cut and prev.get("ec_cut") == ec_cut)


def _record_cut_conflict(job_dir: str, kind: str, wf_cut: float, ec_cut: float) -> None:
    data = _read_cut_conflict(job_dir)
    data[kind] = {"wf_cut": wf_cut, "ec_cut": ec_cut, "done": True, "at": jm.now_iso()}
    path = os.path.join(job_dir, CUT_CONFLICT_NAME)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _effective_cut(wf: dict[str, Any], job: dict[str, Any], job_dir: str,
                   notify: bool = True, write_log: bool = True) -> Optional[float]:
    """cut 解析（以 intent_revision 分界）：

    - rev==0（未经人工确认）：export_config.json（更新的人工动作）> workflow.cut_s（dispatch 归并）> video.cut_s
    - rev>0（人工已确认）：workflow.cut_s 最高 > export_config > video.cut_s
    workflow 与 export_config 分歧时禁止静默忽略，按路径分三档：
      - 预览/只读（notify=False, write_log=False：plan/--dry-run/--hold）只打印，不弹通知、不落日志；
      - --once 真实执行（notify=False, write_log=True）：写 watcher.log（同值不重复刷），不弹通知；
      - 持续 watcher 真实轮次（notify=True, write_log=True）：日志 + 桌面通知，各自「值变化才再做」。
    export_config 必须在 edit 阶段就被消费（裁剪是构建草稿时生效的）。
    """
    def _pos(v: Any) -> Optional[float]:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return float(v)
        return None

    rev = int(wf.get("intent_revision") or 0)
    wf_cut = _pos(wf.get("cut_s"))
    try:
        ec_cut = _pos(_read_export_config(job_dir).get("cut_s"))
    except Exception:
        ec_cut = None
    vid_cut = _pos((job.get("video") or {}).get("cut_s"))

    if rev > 0:
        cut = wf_cut if wf_cut is not None else (ec_cut if ec_cut is not None else vid_cut)
    else:
        cut = ec_cut if ec_cut is not None else (wf_cut if wf_cut is not None else vid_cut)

    if wf_cut is not None and ec_cut is not None and wf_cut != ec_cut:
        name = os.path.basename(os.path.dirname(job_dir).rstrip("\\/"))
        branch = "rev>0：workflow 优先" if rev > 0 else "rev=0：export_config 优先"
        msg = (f"cut 分歧: {name} workflow.cut_s={wf_cut} vs export_config.cut_s={ec_cut}"
               f" → 采用 {cut}（{branch}）")
        changed_log = _cut_conflict_changed(job_dir, "logged", wf_cut, ec_cut)
        changed_notify = _cut_conflict_changed(job_dir, "notified", wf_cut, ec_cut)
        did_log = False
        did_notify = False
        if write_log and changed_log:
            # 真实执行轮次：落 watcher.log；同一对值不重复刷
            _log(job_dir, "⚠️ " + msg)
            _record_cut_conflict(job_dir, "logged", wf_cut, ec_cut)
            did_log = True
        if notify and changed_notify:
            _record_cut_conflict(job_dir, "notified", wf_cut, ec_cut)
            try:
                toast("⚠️ 裁剪意图分歧",
                      f"{name}：workflow={wf_cut} export_config={ec_cut} → 采用 {cut}（{branch}）")
            except Exception:
                pass
            did_notify = True
        if not (did_log or did_notify):
            # 预览/只读路径，或与上次记录相同：只打印
            print("  ⚠️ " + msg)
    return cut


def _effective_derive(wf: dict[str, Any], cfg: dict[str, Any],
                      legacy_override: Optional[list[str]]) -> list[str]:
    """派生解析：workflow.derive > export_config 旧覆盖 > 全局 watch.derive（默认 []）。"""
    if wf.get("derive"):
        return list(wf["derive"])
    if legacy_override is not None:
        return legacy_override
    g = cfg.get("derive", [])
    if isinstance(g, bool):  # 旧配置兼容：derive:true=全平台 / false=不派生
        return list(jm.DERIVE_PRESETS) if g else []
    if isinstance(g, str):
        g = [g]
    return list(g) if g else []


def _available_preset_profiles() -> set[str]:
    """presets.json 里真实存在的预设组（a/b/…），用于过滤推导结果。

    兼容旧配置：未分组时基础版用裸键 `presets`，视同 `presets_a` 存在。
    """
    try:
        with open(pc.CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(data, dict):
        return set()
    keys = [str(k) for k in data]
    out = {k[len("presets_"):].lower() for k in keys if k.startswith("presets_")}
    if "presets" in keys and "a" not in out:
        out.add("a")
    return out


def _edit_gate(job_dir: str, phase_override: Optional[str] = None) -> Optional[str]:
    """edit 闸门（每轮无条件读，不再只在"草稿不存在"时读）。

    最近一次 edit 是 failed / paused / retrying（含退避窗口已过但还没重跑成功）
    → 返回原因，禁止进入导出（也禁止再起一轮 edit）。--phase edit 人工强制放行
    （eds.gate 内处理）。状态文件不存在时不调用 eds.gate（它会同写初始 pending），
    避免只读的 plan 预览产生副作用。
    """
    if phase_override == "edit":
        return None
    st = eds.peek(job_dir)
    if st is None:
        return None
    reason = eds.gate(job_dir, phase_override=phase_override)
    if reason:
        return reason
    if str(st.get("status") or "") == "retrying":
        # 窗口已到只代表"可以去重试剪辑"，不代表"可以跳过剪辑直接导出"
        return (f"最近一次剪辑失败（已失败 {st.get('consecutive_failures')} 次，"
                f"failure_class={st.get('failure_class')}），需重跑剪辑成功后才可导出")
    return None


def _edit_retry_due(job_dir: str, now: Optional[float] = None) -> bool:
    """retrying 且退避窗口已到（下一轮应重跑 edit，而不是导出）。"""
    st = eds.peek(job_dir) or {}
    if str(st.get("status") or "") != "retrying":
        return False
    nxt = float(st.get("next_retry_at") or 0)
    return nxt <= (now if now is not None else time.time())


def build_plan(job_dir: str, job: dict[str, Any], cfg: dict[str, Any],
               pending_edit: int, phase_override: Optional[str] = None,
               notify: bool = True, write_log: bool = True,
               profiles: Optional[list[str]] = None) -> dict[str, Any]:
    """推导单个剧的计划：{action: edit|export|blocked, reason, draft, intent...}

    notify/write_log 透传给 _effective_cut：预览传 (False, False)；
    --once 真实执行传 (False, True)；持续 watcher 传 (True, True)。
    """
    series_dir = os.path.dirname(job_dir)
    name = os.path.basename(series_dir.rstrip("\\/"))
    wf = _intent_of(job)
    draft = find_stream_draft(name)
    # 版本声明：job 优先（titles.selected_groups → prompts.items → workflow.preset_profile），
    # job 没有或不完整才回落到 03_prompts/prompts.json。缺 presets_<组> 只把该组标
    # blocked，不阻断其余版本（A 照常导出，B 在计划/日志/台账里可见地缺失）。
    policy = str(cfg.get("export_policy", "immediate")).strip().lower()
    if policy not in EXPORT_POLICIES:
        policy = "immediate"
    plan: dict[str, Any] = {"job_dir": job_dir, "name": name, "draft": draft,
                            "intent": wf, "policy": policy, "profiles": [],
                            "action": "blocked", "reason": "", "cmd": ""}
    # 闸门②（edit 失败闸门）：不论草稿在不在，都先看最近一次 edit 是否已恢复。
    # failed / paused / 退避中或窗口已过但未重跑成功的 retrying 一律不许进入导出；
    # 窗口已过且有草稿 → 本轮改为重跑剪辑（而不是直接用旧草稿导出）。
    edit_block = _edit_gate(job_dir, phase_override)
    edit_retry = bool(edit_block) and draft is not None \
        and phase_override is None and _edit_retry_due(job_dir)
    if edit_block and not edit_retry:
        plan.update(reason=f"edit 未恢复，暂停推进：{edit_block}")
        return plan
    declared, p_source, ignored_groups = _profile_groups(job, job_dir)
    declared_all = declared
    base_profile = (declared_all or ["a"])[0]
    plan["base_profile"] = base_profile
    if profiles:
        declared = [p for p in declared if p in profiles]
        if not declared:
            plan.update(reason=f"请求的版本 {'/'.join(profiles)} 不在本剧声明集合里")
            return plan
    blocked_profiles = _blocked_profiles(declared)
    blocked_set = {b["profile"] for b in blocked_profiles}
    runnable = [p for p in declared if p not in blocked_set]
    plan.update(profiles=runnable, profiles_declared=declared,
                profiles_source=p_source, profiles_ignored=ignored_groups,
                blocked_profiles=blocked_profiles)
    blocked_note = "；".join(f"{b['profile']} 缺 {b['missing']}（blocked）"
                             for b in blocked_profiles)
    if write_log:
        for p in jm.set_outputs_blocked(job_dir, blocked_profiles, all_profiles=declared_all):
            _log(job_dir, f"版本 {p} " + ("标记 blocked" if p in blocked_set else "解除 blocked")
                 + f"（判定来源: {p_source}）")
        if _log_profile_source(job_dir, f"{p_source}|ignored={','.join(ignored_groups)}"):
            _log(job_dir, f"版本判定来源: {p_source}（{'/'.join(declared)}）")
            if ignored_groups:
                _log(job_dir, f"prompts 含 {','.join(ignored_groups)} 组但 job 未声明，"
                              "已忽略（版本判定严格以 job 为准）")
    if not runnable:
        plan.update(reason=f"声明的版本 {'/'.join(declared)} 均缺 presets_<组>，无可导出版本")
        return plan
    # 闸门③（台账 ⇄ 磁盘双向校验）：先算清冲突，再判"是否已导出"——否则
    # video.export_state=exported 会把成品已被删的剧当成"已完成"直接跳过。
    conflicts: list[dict[str, Any]] = []
    if draft:
        conflicts = _output_conflicts(job, os.path.basename(draft.rstrip("\\/")), runnable)
    plan["output_conflicts"] = conflicts
    if conflicts and write_log:
        _log_output_conflicts(job_dir, conflicts)
    if conflicts and phase_override not in ("export", "edit"):
        # 两种冲突都停下等人工确认：既不默认信磁盘，也不擅自重导几个 G 的成品。
        plan.update(reason="成品台账与磁盘冲突：" + "；".join(
            f"{c['profile']} {c['detail']}" for c in conflicts)
            + f"；{conflicts[0]['hint']}")
        return plan
    # 闸门⑥（产物新鲜度）：草稿在上次记录之后被非本管线改过 —— 重建会覆盖人工编辑，
    # 必须先人工确认。unknown/valid/legacy 一律不拦（与改动前行为一致）。
    try:
        import freshness
        e1 = freshness.check(job_dir, "e1")
        if e1.get("state") == "stale" and e1.get("reason") == "externally_modified":
            plan.update(reason="草稿被非本管线修改（freshness: externally_modified），"
                              "重建会覆盖人工内容；确认这版可用后执行 "
                              f"python lib/freshness.py --job \"{job_dir}\" --reuse-as-is")
            return plan
    except Exception:
        pass
    cut = _effective_cut(wf, job, job_dir, notify=notify, write_log=write_log)

    def _edit_cmd() -> list[str]:
        # -u：stdout 必须无缓冲。watcher 用 pythonw 托管，子进程 stdout 被重定向到
        # 06_publish/pipeline.log 后默认是块缓冲（8KB），进度迟迟不落盘；而
        # _wait_edit_with_watchdog 只按该日志体积变化刷新 edit 心跳，缓冲期间会被
        # 误判成 edit_stalled 而杀掉正在跑的剪辑。
        cmd = [sys.executable, "-u", _BATCH_RUNNER, series_dir, "--force", "--no-export"]
        if runnable:
            cmd += ["--profile", ",".join(runnable)]
        start_ep = _read_start_ep(job_dir)
        if start_ep > 1:
            cmd += ["--only", f"{start_ep}.."]
        if cut:
            cmd += ["--cut", str(cut)]
        return cmd

    def _export_cmd() -> list[str]:
        dn = os.path.basename(draft.rstrip("\\/"))
        cmd = [sys.executable, os.path.join(_REPO, ".trae", "skills", "jianying-editor",
                                            "examples", "robust_auto_export.py"),
               dn, pc.export_mp4_path(dn), "--move", "--fallback-dir", pc.get_export_dir()]
        if cfg.get("cold_start", True):
            cmd.append("--cold-start")
        return cmd

    if phase_override == "edit":
        plan.update(action="edit", reason="--phase edit 强制", cmd=_edit_cmd())
        return plan
    if phase_override == "export":
        if not draft:
            plan.update(reason="--phase export 强制但无草稿，先跑 edit")
            return plan
        dn = os.path.basename(draft.rstrip("\\/"))
        # 人工放行同样导全多账号草稿（强制语义：已有成品也重导）
        plan["export_targets"] = _export_targets(dn, runnable, base_profile)
        # 显式标记"强制重导"：run_export 据此不得按 _draft_exported 跳过，
        # 否则会出现"跳过 + 登记 exported + 弹完成"的假成功。
        plan["force_reexport"] = True
        plan.update(action="export", reason="--phase export 强制", cmd=_export_cmd())
        return plan

    if edit_retry:
        # 剪辑失败退避窗口已过 → 重跑剪辑；不拿旧草稿去导出。
        # 这一支必须优先于终态判断：上一次剪辑失败说明草稿不可信，哪怕台账里
        # 还是 exported，也要先把剪辑补完，不能直接当"已导出"放过去。
        reason = "最近一次剪辑失败（退避窗口已到）→ 重跑剪辑，不进入导出"
        if blocked_note:
            reason += "；" + blocked_note
        plan.update(action="edit", reason=reason, cmd=_edit_cmd())
        return plan

    # 终态优先：已清理/已导出的剧不再进入剪辑，即使草稿缺失也不重建
    # （重建会产生"已清理又冒出新成品"）。放在 draft is None 之前。
    export_state = str(((job.get("video") or {})).get("export_state") or "").strip().lower()
    if export_state == "cleaned":
        plan.update(reason="video.export_state=cleaned：成品已清理，视为完成（不再调度）")
        return plan
    if export_state == "exported":
        # 多账号：已经 exported 但 job 声明的附加账号还有缺草稿/缺成品的，继续往下走
        # （下一步会命中 clone / export 分支）。cleaned 仍视为终态。
        pending_accounts = _pending_account_outputs(job, job_dir)
        if not pending_accounts:
            plan.update(reason="video.export_state=exported：已导出，等待清理（不重复导出）")
            return plan

    if draft is None:
        # 终态已排除（未清理/未导出）才允许重建草稿
        reason = "无串流草稿 → 剪辑阶段"
        if blocked_note:
            reason += "；" + blocked_note
        plan.update(action="edit", reason=reason, cmd=_edit_cmd())
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
    # 跨轮重拉：上一轮拉起失败 → 本轮直接放行（跳过退避闸门，不做任何 sleep）。
    rl = relaunch_state(job_dir)
    if rl and rl.get("status") == "pending":
        plan["relaunch"] = rl
        plan.update(action="export",
                    reason=(f"跨轮重拉 第 {rl['rounds']}/{rl['max_rounds']} 轮"
                            f"（上一轮拉起失败：{rl.get('last_class') or 'unknown'}）"),
                    cmd=_export_cmd())
        return plan
    if rl and rl.get("status") == "exhausted":
        plan.update(reason=(f"拉起连续失败 {rl['rounds'] - 1} 轮已到顶，"
                            f"状态已转 failed，等人工处理"))
        return plan
    # 导出闸门读单一真源：带上 .progress.json 心跳（判 exporting 是否 stale）与迁移上下文
    dn = os.path.basename(draft.rstrip("\\/"))
    pm, p_status, p_pct = _progress_info(pc.export_mp4_path(dn))
    ctx = _migrate_ctx(job)
    ctx.update({"progress_status": p_status, "progress_pct": p_pct})
    if len(runnable) <= 1:
        gate = retry_gate(job_dir, phase_override, progress_mtime=pm, migrate_ctx=ctx)
        if gate:
            plan.update(reason=gate)
            return plan
    missing = _missing_account_drafts(dn, runnable)
    if missing:
        # 少一份账号草稿就导出会漏发一个账号：先自动补克隆（只复制草稿换预设，
        # 几秒完成，不重跑 ASR/翻译/人声分离），失败才退回人工处理。
        clone_profiles = [p for p in runnable[1:]
                          if _account_draft_name(dn, p) in set(missing)]
        plan["draft_name"] = dn
        plan["clone_profiles"] = clone_profiles
        clone_cmd: list[str] = []
        for p in clone_profiles:
            if clone_cmd:
                clone_cmd.append("&&")
            clone_cmd += [sys.executable, os.path.join(_REPO, "pipeline", "rebase_presets.py"),
                          dn, "--profile", p, "--overwrite"]
        plan.update(action="clone",
                    reason=f"缺少账号预设草稿 {', '.join(missing)} → 自动复制基础草稿换预设",
                    cmd=clone_cmd)
        return plan
    targets = _export_targets(dn, runnable, base_profile)
    pending = [d for d in targets if not _draft_exported(d)]
    # 版本级退避：某版本暂停/退避只卡它自己（next_retry_at 按版本独立），
    # 其余版本照常导出；导出本身仍全局串行。
    blocked: list[str] = []
    if len(runnable) > 1:
        ready = []
        for d in pending:
            prof = _profile_of_draft(dn, d)
            _pm, _, _ = _progress_info(pc.export_mp4_path(d))
            why = retry_gate(job_dir, phase_override, progress_mtime=_pm,
                             migrate_ctx=ctx, profile=prof)
            if why:
                blocked.append(f"{prof}: {why}")
                _log(job_dir, f"版本 {prof} 暂缓导出：{why}")
            else:
                ready.append(d)
        pending = ready
    plan["export_targets"] = pending
    if blocked and not pending:
        plan.update(reason="待导版本均在退避/暂停 → 暂不导出；" + "；".join(blocked))
        return plan
    if len(targets) > 1:
        reason = f"草稿就绪 + mode=full + policy={policy}，待导出 {len(pending)}/{len(targets)} 份（多版本预设）"
        if blocked:
            reason += "；暂缓: " + "；".join(blocked)
    else:
        reason = f"草稿就绪 + mode=full + policy={policy}"
    if blocked_note:
        reason += "；" + blocked_note
    if conflicts:
        reason += "；台账冲突: " + "；".join(f"{c['profile']} {c['detail']}" for c in conflicts)
    plan.update(action="export", reason=reason, cmd=_export_cmd())
    return plan


def _plan_line(plan: dict[str, Any]) -> str:
    wf = plan["intent"]
    intent = (f"mode={wf.get('mode')} cut={wf.get('cut_s')} derive={wf.get('derive')} "
              f"来源={wf.get('intent_source')} rev={wf.get('intent_revision')} "
              f"优先级={wf.get('priority')}")
    head = f"[plan] {plan['name']}: {plan['action']}"
    line = f"{head}（{plan['reason']}） | 意图: {intent}"
    declared = plan.get("profiles_declared") or plan.get("profiles") or []
    if declared:
        line += f" | 版本: {'/'.join(declared)}（判定来源: {plan.get('profiles_source') or '?'}）"
    ignored = plan.get("profiles_ignored") or []
    if ignored:
        line += f" | prompts 多出未声明组已忽略: {','.join(ignored)}"
    for b in plan.get("blocked_profiles") or []:
        line += f"\n        blocked: {b['profile']} 缺 {b['missing']}（{b['reason']}）"
    for c in plan.get("output_conflicts") or []:
        line += f"\n        成品冲突 [{c['profile']}]: {c['detail']}（{c['hint']}）"
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


def _read_start_ep(job_dir: str) -> int:
    """export_config.json 的 start_ep：起始集号，缺省/非法一律按 1（只告警不抛异常）。

    start_ep > 1 时 watcher 拼编辑命令追加 --only <start_ep>..（从该集起重剪到最后一集）；
    缺省或 <=1 时不加，行为与旧版完全一致。
    """
    p = os.path.join(job_dir, EXPORT_CONFIG_NAME)
    if not os.path.isfile(p):
        return 1
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"  ⚠️ {EXPORT_CONFIG_NAME} 读取失败，start_ep 按 1 处理: {e}")
        return 1
    if not isinstance(data, dict) or "start_ep" not in data:
        return 1
    raw = data.get("start_ep")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        print(f"  ⚠️ export_config.start_ep 非法（{raw!r}），按 1 处理")
        return 1
    return raw


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

# edit 阶段可自愈失败：validate_job 报「缺少目录: 06_publish/receipts」这类环境缺陷
MISSING_DIR_RE = re.compile(r"缺少目录[:：]\s*([0-9A-Za-z_\-./\\]+)")


def _parse_missing_dirs(error_text: str) -> list[str]:
    """从错误文本提取「缺少目录: xxx」的相对目录列表（去重，过滤越界路径）。"""
    out = []
    for raw in MISSING_DIR_RE.findall(error_text or ""):
        rel = raw.strip().strip("，；;,")
        rel = rel.replace("\\", "/")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        out.append(rel)
    return sorted(set(out))


def _try_heal_missing_dirs(job_dir: str, error_text: str, name: str) -> bool:
    """缺目录失败的自愈：一次自动补齐标准目录，不占额度，下一轮立即重试。

    仅首次缺失可自愈；已补过仍缺（healed_dirs 已记录）说明是别的问题，
    返回 False 让调用方按普通失败计数。补齐操作本身失败也算普通失败。
    """
    rels = _parse_missing_dirs(error_text)
    if not rels or eds.already_healed(job_dir, rels):
        return False
    try:
        for rel in rels:
            os.makedirs(os.path.join(job_dir, *rel.split("/")), exist_ok=True)
    except OSError as e:
        _log(job_dir, f"⚠️ 自动补目录失败 {rels}: {e}（按普通失败计数）")
        return False
    eds.heal(job_dir, rels)
    _log(job_dir, f"🔧 已自动补齐缺失目录 {', '.join(rels)}（{name}），不计入重试，立即重试")
    return True


def _edit_retry_iso(ts: float) -> Optional[str]:
    """epoch → 本地 ISO 时间；0/负数表示"立即可重试"，返回 None。"""
    return (datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
            if ts and ts > 0 else None)


def _write_edit_job(job_dir: str, *, state: str, rc: Optional[int] = None,
                    error: Optional[str] = None,
                    failure_class: Optional[str] = None) -> None:
    """闸门①：把 edit 的 rc/状态/错误摘要落进 job.json（不只留 watcher.log）。

    成功后 .edit_state.json 已是 done、计数清零，这里读回同一份状态落账，保证
    job.json 里的 edit_state 与会话状态机永远一致；落账失败只告警，不影响剪辑。
    """
    st = eds.peek(job_dir) or {}
    try:
        jm.record_edit_state(
            job_dir, state=state, rc=rc, error=error, failure_class=failure_class,
            attempts=int(st.get("attempts") or 0),
            consecutive_failures=int(st.get("consecutive_failures") or 0),
            next_retry_at=_edit_retry_iso(float(st.get("next_retry_at") or 0)))
    except Exception as e:
        _log(job_dir, "⚠️ edit 状态落账失败（不影响剪辑本身）: "
                      + _exception_brief(e, where="record_edit_state"))


def note_edit_result(job_dir: str, name: str, ok: bool, *,
                     error: Optional[str] = None,
                     failure_class: str = "unknown",
                     retryable: bool = True,
                     pause_after: Optional[int] = None,
                     rc: Optional[int] = None) -> None:
    """记录 edit 结果（写 .edit_state.json + job.json 台账）：成功置 done 清零；
    失败按分类退避/暂停或终态。

    与导出侧 note_export_result 同构；差异：暂停阈值 3 次，仅 paused/硬失败弹
    toast，退避中的失败只写日志（edit 失败多为数据问题，避免通知轰炸）。
    第 1 道闸：rc / 状态 / 错误摘要同步写进 job.json.video.edit_state，
    下一轮/人工都能不读日志就看到 blocked 语义与重试时间。
    """
    if ok:
        eds.succeed(job_dir)
        _write_edit_job(job_dir, state="done", rc=rc)
        _log(job_dir, "edit 状态置 done（计数清零）")
        # 无字幕策略：只有本轮剪辑后清单已空（缺字幕集已真正修好）才解除导出终态拦截
        if not jm.subtitle_missing(job_dir):
            prev = _sio.read_raw(job_dir, es.STATE_NAME) or {}
            if prev.get("status") == "failed" \
                    and prev.get("failure_class") == "subtitle_missing":
                es.clear_block(job_dir, failure_class="subtitle_missing")
                _log(job_dir, "缺字幕已全部修复，导出闸门复位 pending")
        return
    if pause_after is None:
        # 阈值可由 presets.watch.edit_pause_after 覆盖（默认仍为 3，导出侧语义不受影响）
        try:
            pause_after = int(pc.get_watch_config().get("edit_pause_after",
                                                        eds.DEFAULT_PAUSE_AFTER))
        except Exception:
            pause_after = eds.DEFAULT_PAUSE_AFTER
    st = eds.fail(job_dir, error, pause_after=pause_after,
                  failure_class=failure_class, retryable=retryable, phase="edit")
    _write_edit_job(job_dir, state=str(st.get("status") or "retrying"),
                    rc=rc, error=error, failure_class=failure_class)
    fails = int(st.get("consecutive_failures") or 0)
    if st.get("status") == "failed":
        _log(job_dir, f"edit 硬失败[{failure_class}]，置终态不重试：{error}")
        try:
            toast("⛔ 剪辑硬失败（不重试）",
                  f"{name}：{failure_class}，需人工处理\n{error or ''}", icon="error")
        except Exception:
            pass
        return
    if st.get("status") == "paused":
        _log(job_dir, f"edit 失败第 {fails} 次（已暂停该剧）")
        try:
            toast("⛔ 剪辑连续失败已暂停",
                  f"{name}：连续 {fails} 次失败，需人工排查", icon="error")
        except Exception:
            pass
        return
    delay = eds.BACKOFF_S[min(fails - 1, len(eds.BACKOFF_S) - 1)]
    _log(job_dir, f"edit 失败第 {fails} 次[{failure_class}]，{delay}s 后重试")


# pipeline.log 尾部常被这些子进程解码线程噪音覆盖，结构化提取时必须剔除
_LOG_NOISE = ("UnicodeDecodeError", "_readerthread",
              "Exception in thread", "subprocess.py")
_LOG_ERR_RE = re.compile(
    r"(✗|Traceback|RuntimeError|Error|error:|失败|failed|"
    r"Conversion failed|Nothing was written|缺少目录)")


def _exception_brief(err: BaseException, where: str = "") -> str:
    """结构化异常摘要：异常类型 + 首行 message + 发生位置（函数:行号）。

    last_error 不再记录日志尾部 500 字符：2026-11-07 那轮 pipeline.log 尾部被子进程
    解码线程的 UnicodeDecodeError 噪音覆盖，按 last_error 误判了一轮根因。
    """
    msg = str(err).replace("\r", "").strip()
    first = msg.splitlines()[0].strip() if msg else ""
    if len(first) > 200:
        first = first[:199] + "…"
    tb = err.__traceback__
    loc = ""
    while tb is not None:
        code = tb.tb_frame.f_code
        loc = f"{code.co_name}:{tb.tb_lineno}"
        tb = tb.tb_next
    out = f"{type(err).__name__}: {first}" if first else type(err).__name__
    if loc:
        out += f" @ {loc}"
    if where:
        out += f" ({where})"
    return out


def _pipeline_failure_brief(job_dir: str, rc: int) -> str:
    """batch_runner 非 0 退出：退出码 + 本轮最后几条有效错误行（剔除解码噪音）。

    不再整段记录日志尾部；只保留最后 3 条去重错误行，同时保住「缺少目录」这类
    需要自愈匹配的关键行（_try_heal_missing_dirs 依赖它）。
    """
    path = os.path.join(job_dir, "06_publish", "pipeline.log")
    sig: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read()[-16384:]
        marker = data.rfind("cover_watch edit 阶段")
        if marker >= 0:
            data = data[marker:]
        for line in data.splitlines():
            s = line.strip()
            if not s or any(n in s for n in _LOG_NOISE):
                continue
            if not _LOG_ERR_RE.search(s):
                continue
            if s not in sig:
                sig.append(s)
    except OSError:
        pass
    base = f"batch_runner rc={rc}（run_edit）"
    if not sig:
        return base
    return base + "｜" + " ⏎ ".join(x[:160] for x in sig[-3:])


def _norm_cmp_path(s: str) -> str:
    return s.replace("\\", "/").rstrip("/").lower()


def _edit_stall_timeout(cfg: Optional[dict[str, Any]] = None) -> float:
    """watch.edit_stall_timeout_s，默认 900 秒；0 表示关闭看门狗。"""
    try:
        raw = (cfg or pc.get_watch_config() or {}).get("edit_stall_timeout_s", 900)
        return float(raw)
    except (TypeError, ValueError):
        return 900.0


def _kill_tree(pid: int) -> None:
    """杀整棵进程树（编辑子进程是 batch_runner，可能再拉子进程）。"""
    if pid <= 0:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=15,
                       creationflags=NO_WINDOW)
    except Exception as e:
        print(f"  [WARN] taskkill pid={pid} 失败: {e}")


def _pid_owned_by_job(pid: int, series_dir: str) -> bool:
    """pid 或其父链的命令行是否指向这部剧（防止杀错进程）。

    batch_runner 的启动命令带 series_dir；父链里 watcher 的命令行也可能带
    cover_job 路径，两者都包含 series_dir，因此按 series_dir 子串判定即可。
    查询失败一律返回 False（宁可不杀）。
    """
    if pid <= 0:
        return False
    series = _norm_cmp_path(os.path.abspath(series_dir))
    if not series:
        return False
    ps = (
        f"$p={int(pid)}; for($i=0; $i -lt 5 -and $p -ne 0; $i++) {{ "
        f"$o=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $p) "
        f"-ErrorAction SilentlyContinue; if(-not $o) {{ break }}; "
        f"Write-Output ($o.CommandLine + '|' + $o.ParentProcessId); "
        f"$p=[int]$o.ParentProcessId }}"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW).stdout
    except Exception:
        return False
    return series in _norm_cmp_path(out)


def _proc_tree_snapshot() -> dict[int, int]:
    """Win32 进程表快照（pid → parent pid）。失败返回 {}，调用方按「无可杀」处理。"""
    ps = ("Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
          "ForEach-Object { Write-Output ($_.ProcessId.ToString() + '|' + "
          "$_.ParentProcessId.ToString()) }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=20,
                             creationflags=NO_WINDOW).stdout
    except Exception as e:
        print(f"  [WARN] 进程表快照失败: {e}")
        return {}
    tree: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        tree[pid] = ppid
    return tree


def _descendant_pids(root_pid: int, tree: Optional[dict[int, int]] = None) -> list[int]:
    """返回 root_pid 的全部后代 pid（按 pid 树，不按进程名匹配）。"""
    tree = _proc_tree_snapshot() if tree is None else tree
    children: dict[int, list[int]] = {}
    for pid, ppid in tree.items():
        children.setdefault(ppid, []).append(pid)
    out: list[int] = []
    stack, seen = [root_pid], {root_pid}
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            stack.append(child)
    return out


def _kill_descendants(root_pid: int) -> list[int]:
    """终止 root_pid 派生的全部子进程（按 pid 树），返回实际尝试终止的 pid 列表。

    只用于 root_pid=watcher 自身：本地执行链异常掀翻本轮后，若 batch_runner /
    ffmpeg 还活着，下一轮会与它并发写同一份缓存/草稿（2026-11-07 六个坏缓存事故）。
    只遍历 pid 树、不按进程名匹配，因此不会误杀别的剧的进程。
    """
    pids = _descendant_pids(root_pid)
    killed: list[int] = []
    for pid in reversed(pids):
        if not _pid_alive(pid):
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=NO_WINDOW)
            killed.append(pid)
        except Exception as e:
            print(f"  [WARN] taskkill pid={pid} 失败: {e}")
    return killed


def _wait_edit_with_watchdog(job_dir: str, proc: subprocess.Popen, name: str) -> int:
    """等编辑子进程，按 pipeline.log 的进展刷新心跳；超时直接 kill 本进程的子进程。

    该 pid 来自本进程的 Popen 句柄，归属已确定，无需再做命令行文本校验
    （CIM 查询在中文路径/编码差异下会误判，反而导致"记失败但子进程还在写"）。
    """
    timeout = _edit_stall_timeout()
    log_path = os.path.join(job_dir, "06_publish", "pipeline.log")
    try:
        last_size = os.path.getsize(log_path)
    except OSError:
        last_size = 0
    last_beat = time.time()
    while True:
        try:
            return proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        try:
            size = os.path.getsize(log_path)
        except OSError:
            size = last_size
        if size != last_size:
            last_size = size
            last_beat = time.time()
            eds.heartbeat(job_dir, pid=proc.pid, now=last_beat)
        if timeout > 0 and time.time() - last_beat >= timeout:
            _log(job_dir, f"edit 卡死：{int(timeout)}s 无日志进展，pid={proc.pid}")
            # 自己 Popen 的子进程，归属无需外部校验，直接杀整棵树；
            # 否则一旦校验失败就会"记失败但不杀"，下一轮重启后两个 edit 同时写草稿。
            _kill_tree(proc.pid)
            _log(job_dir, f"edit 卡死：已终止 pid={proc.pid}（本进程子进程）")
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            note_edit_result(job_dir, name, False,
                             error=f"edit 心跳超时（{int(timeout)}s 无进展）",
                             failure_class="edit_stalled", retryable=True)
            toast("⛔ 剪辑卡死", f"{name}：{int(timeout)}s 无进展，已按失败退避", icon="error")
            return -999  # 已记过失败，调用方不要再走 _fail 重复计数


def _reap_stalled_edit(job_dir: str, cfg: dict[str, Any], *, name: str) -> None:
    """每轮巡检：editing 心跳超时 → 校验 pid 归属后 kill，并记一次失败退避。

    与 run_edit 内的看门狗互补：这里处理上一轮崩溃/遗留的 editing。
    归属无法证明时只告警并返回：不 kill、不记失败、不放行下一轮，避免在旧子进程
    仍在写同一草稿时又启动一个新的 edit。
    """
    timeout = _edit_stall_timeout(cfg)
    if timeout <= 0:
        return
    st = eds.peek(job_dir)
    if not st or st.get("status") != "editing":
        return
    hb = float(st.get("heartbeat_at") or 0)
    if hb <= 0:  # 旧版状态没有心跳：用状态文件 mtime 兜底
        try:
            hb = os.path.getmtime(os.path.join(job_dir, eds.STATE_NAME))
        except OSError:
            hb = 0
    if not hb or time.time() - hb < timeout:
        return
    pid = int(st.get("pid") or 0)
    series_dir = os.path.dirname(job_dir.rstrip("\\/"))
    alive = _pid_alive(pid)
    if not alive:
        _log(job_dir, f"edit 卡死：pid={pid} 已不存在，仅清理状态")
    elif _pid_owned_by_job(pid, series_dir):
        _kill_tree(pid)
        _log(job_dir, f"edit 卡死：pid={pid} 心跳 {int(time.time() - hb)}s 未更新，已终止")
    else:
        _log(job_dir, f"edit 卡死：pid={pid} 归属校验未通过，未 kill，"
                      f"本轮不记失败、不放行（等人工确认）")
        return
    try:
        note_edit_result(job_dir, name, False,
                         error=f"edit 心跳超时（{int(time.time() - hb)}s 未更新）",
                         failure_class="edit_stalled", retryable=True)
        toast("⛔ 剪辑卡死", f"{name}：心跳超时，已按失败退避", icon="error")
    except OSError as e:
        # 巡检轮次不因为它自己写状态失败就把 daemon 打崩；下一轮会再次巡检。
        _log(job_dir, f"edit 卡死：写 .edit_state.json 失败（{e}），下一轮重试")


def _job_created_ts(job_dir: str, job: dict[str, Any]) -> Optional[float]:
    """job.json 的创建时间：优先 job.created_at（原子重写后 ctime 会重置，
    created_at 才稳定），缺失/不可解析时退回文件 ctime。"""
    raw = str(job.get("created_at") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            pass
    try:
        return os.path.getctime(os.path.join(job_dir, "job.json"))
    except OSError:
        return None


def _new_job_in_grace(job_dir: str, job: dict[str, Any], cfg: dict[str, Any],
                      now: Optional[float] = None) -> bool:
    """新剧宽限期：从未执行过、job.json 未被改过、创建不足 N 分钟 → 本轮跳过。"""
    try:
        grace_min = float(cfg.get("new_job_grace_min", 10) or 0)
    except (TypeError, ValueError):
        grace_min = 10.0
    if grace_min <= 0:
        return False
    events = {str((h or {}).get("event") or "") for h in (job.get("history") or [])}
    if events & _EXEC_HISTORY_EVENTS:
        return False
    created = _job_created_ts(job_dir, job)
    if created is None:
        return False
    try:
        mtime = os.path.getmtime(os.path.join(job_dir, "job.json"))
    except OSError:
        return False
    if mtime > created + 1.0:  # 创建后被改过（有人在配裁剪）→ 放行
        return False
    return ((now if now is not None else time.time()) - created) < grace_min * 60


def run_edit(job_dir: str, plan: dict[str, Any], *, dry_run: bool) -> bool:
    """edit 阶段：backfill_hashes + 真实校验 → ingest（含 workflow 归并）→ batch_runner --no-export。

    失败处理（.edit_state.json 单一真源，lib/edit_state.py）：
      - 缺目录类失败一次自动补齐（不占额度、不退避，下一轮立即重试）；
      - 其余按失败分类：可重试 → 1/2/4/8/16/30 分钟退避，连续 3 次暂停；
        不可重试（asset_missing/disk_full）→ 终态 failed。
    stage 等契约字段仍由 job_manifest 写，本链路不碰。
    """
    name = plan["name"]
    if dry_run:
        print(_plan_line(plan))
        return True
    toast("🎬 剪辑阶段开始", f"{name}", icon="info")
    _log(job_dir, "edit start")
    eds.begin(job_dir, phase="edit")
    _write_edit_job(job_dir, state="editing")

    def _fail(err_text: str, rc: Optional[int] = None) -> bool:
        """失败统一出口：先试缺目录自愈（不占额度），否则分类写状态机。

        err_text 必须是结构化摘要（_exception_brief / _pipeline_failure_brief /
        validate 的明确错误串），不要再传日志尾部原文。rc 一并落进 job.json。
        """
        if _try_heal_missing_dirs(job_dir, err_text, name):
            return False
        category, _action = fc.classify(err_text, phase="edit")
        note_edit_result(job_dir, name, False, error=err_text,
                         failure_class=category,
                         retryable=fc.is_retryable(category), rc=rc)
        return False

    try:
        jm.backfill_hashes(job_dir)
        job = jm.load_job(os.path.join(job_dir, "job.json"))
        # 先把 ingest 会做的「形态归一」在内存里做掉，再校验：避免写入方不规范
        # （如 cover_map[].file 只有裸文件名、cleanup.status=pending）在闸门处被误判。
        # 归一不落盘，真正的持久化留给随后的 ingest_job；两者规则一致、均幂等。
        for note in jm.normalize_job_shape(job, job_dir):
            _log(job_dir, f"归一: {note}")
        errors = jm.validate_job(job, job_dir) + jm.cover_file_missing(job, job_dir)
    except Exception as e:
        _log(job_dir, f"validate 失败: {e}")
        toast("❌ 剪辑阶段失败", f"{name} validate: {e}", icon="error")
        return _fail(_exception_brief(e, where="run_edit.validate"))
    if errors:
        msg = "；".join(errors)
        _log(job_dir, f"validate 失败: {msg}")
        toast("❌ 剪辑阶段失败", f"{name} validate: {msg}", icon="error")
        return _fail(msg)
    for w in jm.title_id_warnings(job, job_dir):
        _log(job_dir, f"⚠️ {w}")
    _log(job_dir, "validate ok")
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
        return _fail(_exception_brief(e, where="run_edit.ingest"))
    # 旧版 export_config 落盘（cut_s → video.cut_s 记账；derive 覆盖留到 export 阶段用）
    try:
        apply_export_config(job_dir)
    except Exception as e:
        _log(job_dir, f"export_config 失败: {e}")
        toast("❌ 剪辑阶段失败", f"{name} export_config: {e}", icon="error")
        return _fail(_exception_brief(e, where="run_edit.export_config"))
    cmd = list(plan["cmd"])
    _log(job_dir, "pipeline start: " + " ".join(cmd))
    with open(os.path.join(job_dir, "06_publish", "pipeline.log"), "a",
              encoding="utf-8") as lf:
        lf.write(f"\n===== {jm.now_iso()} cover_watch edit 阶段 =====\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=_REPO,
                                creationflags=NO_WINDOW)
    _write_lock(job_dir, "edit", pipeline_pid=proc.pid)
    eds.heartbeat(job_dir, pid=proc.pid)
    rc = _wait_edit_with_watchdog(job_dir, proc, name)
    _log(job_dir, f"edit exit rc={rc}")
    if rc == -999:
        return False  # 看门狗已记一次 edit_stalled 失败，不再重复计数
    if rc != 0:
        toast("❌ 剪辑失败", f"{name} rc={rc}，见 06_publish/pipeline.log", icon="error")
        return _fail(_pipeline_failure_brief(job_dir, rc), rc=rc)
    note_edit_result(job_dir, name, True, rc=rc)
    toast("✅ 草稿就绪", f"{name}（等待导出阶段）", icon="success")
    _log(job_dir, "edit done")
    return True


def run_clone(job_dir: str, plan: dict[str, Any], cfg: dict[str, Any],
              *, dry_run: bool) -> bool:
    """多账号补克隆：只复制基础草稿换预设层，不重跑 ASR/翻译/人声分离。

    场景：基础草稿已存在（单账号时代建的），后来配置里加了第二个账号预设组，
    watcher 不会重跑整条 edit，这里几秒补齐缺失的账号草稿，下一轮正常导出。
    """
    name = plan["name"]
    if dry_run:
        print(_plan_line(plan))
        return True
    _write_lock(job_dir, "clone")
    toast("👥 账号预设克隆开始", name, icon="info")
    _log(job_dir, f"clone start: {plan.get('clone_profiles')}")
    try:
        from preset_ops import clone_draft_with_presets
        for prof in plan.get("clone_profiles") or []:
            rep = clone_draft_with_presets(
                plan["draft_name"], preset_profile=prof, overwrite=True,
                job_dir=job_dir)
            _log(job_dir, f"clone ok {rep['target']}（{prof}）")
        toast("✅ 账号预设草稿就绪", f"{name}（下一轮导出）", icon="success")
        return True
    except Exception as e:
        brief = _exception_brief(e, where="run_clone")
        _log(job_dir, f"clone 失败: {brief}")
        p_class, _act = fc.classify(brief, phase="export")
        strat = fc.strategy_of(p_class)
        note_export_result(job_dir, name, False,
                           pause_after=int(cfg.get("retry_pause_after", RETRY_PAUSE_AFTER)),
                           error=brief, failure_class=p_class,
                           retryable=bool(strat.get("retryable")), phase="clone")
        toast("❌ 账号预设克隆失败", f"{name}: {brief}", icon="error")
        return False


def _run_verify(script: str, draft_name: str, mp4_path: str, job_dir: str) -> tuple[int, str]:
    """调用导出后质检脚本，返回 (rc, 明细日志路径)。

    watcher 由 pythonw 托管、没有控制台：子进程若继承空 stdout，
    verify_export.py 里的 sys.stdout.reconfigure / print 会直接抛异常，
    导致质检恒 rc=1 且没有任何明细。这里把子进程 stdout/stderr 显式落到
    06_publish/verify.log，既是真实质检结论，也保留可回溯的明细。
    """
    log_dir = os.path.join(job_dir, "06_publish")
    log_path = os.path.join(log_dir, "verify.log")
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
            logf.write(f"\n===== {jm.now_iso()} {draft_name} =====\n")
            logf.flush()
            rc = subprocess.call([sys.executable, script, draft_name, mp4_path],
                                 cwd=os.path.dirname(script),
                                 stdout=logf, stderr=subprocess.STDOUT,
                                 creationflags=NO_WINDOW)
        return rc, log_path
    except OSError as e:
        _log(job_dir, f"verify 启动失败: {e}")
        return 2, log_path


def _verify_failure_brief(vlog: str, rc: int) -> str:
    """verify 未通过：退出码 + 结论行/漂移数值（给 job.video.last_edit_error 同级的
    失败原因用，必须带数值，不能只说"质检没过"）。"""
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


def run_export(job_dir: str, plan: dict[str, Any], cfg: dict[str, Any],
               *, dry_run: bool, verify: bool = True, no_derive: bool = False) -> bool:
    """export 阶段：reap(崩溃回收) → begin(exporting) → 导出 → verify → record-video
    → succeed(done) → 派生 video.status → derive。导出状态全程读写单一真源
    .export_state.json（lib/export_state.py），video.status 由真源派生。"""
    # 无字幕策略：导出前硬拦。必须最先执行——先于 dry_run 预览、状态机、子进程，
    # --phase export 人工放行也绕不过；清单为空才继续。
    try:
        jm.require_export_allowed(job_dir)
    except jm.ExportBlocked as e:
        prev = _sio.read_raw(job_dir, es.STATE_NAME) or {}
        repeat = (prev.get("status") == "failed"
                  and prev.get("failure_class") == "subtitle_missing")
        if repeat:
            _log(job_dir, f"export blocked（清单未变）: {e}")
        else:
            _log(job_dir, f"export blocked: {e}")
            print(f"⛔ {e}")
            print("   请重剪这些集，确认字幕非空后重跑；未修复前不会导出。")
        if not dry_run:
            es.fail(job_dir, str(e), retryable=False,
                    failure_class="subtitle_missing", phase="export")
        return False
    name = plan["name"]
    draft_name = os.path.basename(plan["draft"].rstrip("\\/"))
    out_mp4 = pc.export_mp4_path(draft_name)
    if dry_run:
        print(_plan_line(plan))
        print(f"        export: {draft_name} → {out_mp4}")
        return True
    toast("📤 导出阶段开始", f"{name}", icon="info")
    _log(job_dir, "export start")
    # 崩溃恢复（不假设优雅退出）：上次 exporting 且心跳超时 → 回收为 retrying 再走本轮
    pm, _, _ = _progress_info(out_mp4)
    if es.reap_stale(job_dir, progress_mtime=pm):
        _log(job_dir, "真源回收：上次导出心跳超时（stale），转重试")
    try:
        legacy_override = apply_export_config(job_dir)
    except Exception as e:
        _log(job_dir, f"export_config 失败: {e}")
        toast("❌ 导出阶段失败", f"{name} export_config: {e}", icon="error")
        # 异常被吞掉后若只 return False，本轮不计数 → 下一轮 5s 立即重试、无限空转；
        # 与 chain 异常同等对待：必须登记失败，让退避/阈值暂停生效。
        note_export_result(job_dir, name, False,
                           pause_after=int(cfg.get("retry_pause_after", RETRY_PAUSE_AFTER)),
                           error=_exception_brief(e, where="run_export.export_config"),
                           failure_class="export_config_error",
                           retryable=True, phase="export")
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
    # 多版本：导出仍全局串行；每次只把一个版本置 exporting，版本自己的退避/暂停
    # 记在 profiles[<版本>]，互不拖累。已完成的版本下一轮由 _draft_exported 跳过。
    # 版本集合与 build_plan 同源（job 优先、prompts 兜底），缺预设的组已在计划里
    # 标 blocked，这里只导出可运行的版本。
    profiles = list(plan.get("profiles") or []) or _runnable_profiles(_job_profiles(job, job_dir))
    base_profile = plan.get("base_profile") or (_job_profiles(job, job_dir) or ["a"])[0]
    es.register_profiles(job_dir, profiles)
    # 强制重导（--phase export）：目标即使已有成品也要真跑，不得被跳过。
    forced: set[str] = set()
    if "export_targets" in plan:
        targets = list(plan.get("export_targets") or [])
        if plan.get("force_reexport"):
            forced = set(targets)
    else:
        targets = [d for d in _export_targets(draft_name, profiles, base_profile)
                   if not _draft_exported(d)]
    if not targets:
        export_lock.release()
        remaining = [d for d in _export_targets(draft_name, profiles, base_profile)
                     if not _draft_exported(d)]
        _log(job_dir, "export 跳过：本轮无待导版本"
             + (f"（仍有未完成: {', '.join(remaining)}）" if remaining else "（全部已有成品）"))
        if not remaining:
            note_export_result(job_dir, name, True)
            _relaunch_clear(job_dir, "全部已有成品")
            _derive_video_status(job_dir)
        return True
    verify_script = os.path.join(_REPO, "pipeline", "verify_export.py")
    exported: list[tuple[str, str]] = []
    try:
        for idx, dn in enumerate(targets, 1):
            one_out = pc.export_mp4_path(dn)
            prof = _profile_of_draft(draft_name, dn)
            if dn not in forced and _draft_exported(dn):
                # 跳过就只是跳过：不登记 exported、不 record_video、不 succeed、
                # 不 record_export、不弹完成，只留一条 skipped 痕迹。
                _log(job_dir, f"export skipped profile={prof} draft={dn} "
                              f"reason=已有成品（非强制重导） {idx}/{len(targets)}")
                continue
            # 同版本重跑：旧成品三件套（mp4/封面/progress）改名 .v<n> 保留，
            # 不覆盖、也不与新成品混为一谈。任一改名失败整组回滚并硬失败。
            if os.path.isfile(one_out) and jp.export_is_stale(os.path.join(_draft_root(), dn)):
                try:
                    moved = jp.archive_stale_outputs(one_out)
                except Exception as e:
                    brief = _exception_brief(e, where="archive_stale")
                    _log(job_dir, f"旧成品归档失败 {dn}: {brief}")
                    note_export_result(job_dir, name, False,
                                       pause_after=int(cfg.get("retry_pause_after", RETRY_PAUSE_AFTER)),
                                       error=brief, failure_class="archive_failed",
                                       retryable=False, phase="export", profile=prof)
                    toast("❌ 旧成品归档失败", f"{name} {dn}: {brief}", icon="error")
                    return False
                for s, d in moved:
                    _log(job_dir, f"旧成品归档: {os.path.basename(s)} → {os.path.basename(d)}")
            _log(job_dir, f"export 开始 {idx}/{len(targets)}: {dn}（版本 {prof}）")
            es.begin(job_dir, phase="export", profile=prof)
            es.heartbeat(job_dir, phase="export", profile=prof)
            # 台账标签：rounds=0 是首次正常轮，>0 是第 N 次跨轮重拉。
            _ledger_env(job_dir, name, (relaunch_state(job_dir) or {}).get("rounds") or 0)
            export_started = time.time()
            ok = sr._run_export(dn,
                                timeout=float(cfg.get("export_stall_timeout", 1800)),
                                cold_start=bool(cfg.get("cold_start", True)),
                                job_dir=job_dir)
            _log(job_dir, f"export {'ok' if ok else '失败'} {idx}/{len(targets)}: {dn}")
            if not ok:
                # 第 3 项：读回子进程落盘的 failure_class（权威），缺失则对 last_error 重新分类
                p_class, p_phase, last_err = _read_progress_failure(one_out)
                if not p_class:
                    p_class, _act = fc.classify(last_err, p_phase)
                strat = fc.strategy_of(p_class)
                if p_class not in fc.RELAUNCH_CLASSES:
                    # 本次失败不是"拉起本身失败"（草稿/素材/导出超时等）→ 旧状态作废
                    _relaunch_clear(job_dir, "本次失败不是拉起失败")
                else:
                    st = relaunch_note_failure(job_dir, cfg, error=last_err or "",
                                               failure_class=p_class)
                    if st["status"] == "pending":
                        # 到顶前：不改重试预算、不sleep，下一轮扫描由 build_plan 直接放行。
                        _log(job_dir, f"拉起失败 → 待重拉 第 {st['rounds']}/{st['max_rounds']} 轮"
                                      f"（下一轮优先，不等待）")
                        toast("↻ 拉起失败，下一轮优先重拉",
                              f"{name}（第 {st['rounds']}/{st['max_rounds']} 轮）", icon="warning")
                        note_export_result(job_dir, name, False,
                                           pause_after=int(cfg.get("retry_pause_after",
                                                                   RETRY_PAUSE_AFTER)),
                                           error=last_err or "拉起剪映失败",
                                           failure_class=p_class, retryable=True,
                                           phase=p_phase, profile=prof)
                        es.record_recovery(job_dir, _read_progress_recovery(one_out))
                        return False
                    # 到顶：转成可见终态 failed（retryable=False → 不计数、不再自动跑），
                    # 绝不能记成完成；状态文件保留供人工查看。
                    _log(job_dir, f"拉起连续失败 {st['rounds'] - 1} 轮已到顶 → 转 failed，等人工处理")
                    toast("⛔ 拉起连续失败，已停手", f"{name}（{st['rounds'] - 1} 轮）", icon="error")
                    note_export_result(job_dir, name, False,
                                       error=(last_err or "拉起剪映失败")
                                             + f"（跨轮重拉 {st['rounds'] - 1} 轮到顶）",
                                       failure_class=fc.LAUNCH_FAILED,
                                       retryable=False, phase=p_phase, profile=prof)
                    es.record_recovery(job_dir, _read_progress_recovery(one_out))
                    return False
                note_export_result(job_dir, name, False,
                                   pause_after=int(cfg.get("retry_pause_after", RETRY_PAUSE_AFTER)),
                                   error=last_err or "robust_auto_export 返回非 0",
                                   failure_class=p_class,
                                   retryable=bool(strat.get("retryable")),
                                   phase=p_phase, profile=prof)
                # 从 sidecar 读回实际自愈动作写入真源（唯一写者仍是 export_state；无值不写）
                es.record_recovery(job_dir, _read_progress_recovery(one_out))
                toast("❌ 导出失败", f"{name} {dn}（{p_class}）", icon="error")
                return False
            # 闸门④（产物三件套）：导出返回成功但 mp4/封面/progress 缺任一件 →
            # 该版本判失败：不 verify、不 record、不 succeed、不写 exported。
            missing_art = _missing_artifacts(dn, strict_progress=True)
            if missing_art:
                brief = f"导出返回成功但产物不全：缺 {', '.join(missing_art)}（{one_out}）"
                _log(job_dir, f"export 判失败 {idx}/{len(targets)}: {dn}｜{brief}")
                note_export_result(job_dir, name, False,
                                   pause_after=int(cfg.get("retry_pause_after",
                                                           RETRY_PAUSE_AFTER)),
                                   error=brief, failure_class="artifacts_incomplete",
                                   retryable=True, phase="export", profile=prof)
                toast("❌ 导出产物不全（按失败处理）", f"{name} {dn}：缺 {', '.join(missing_art)}",
                      icon="error")
                return False
            # 逐份质检（与 batch_runner 默认行为一致；--no-verify 关闭）
            es.heartbeat(job_dir, phase="verify", profile=prof)
            if verify and os.path.isfile(verify_script):
                _log(job_dir, f"verify start {idx}/{len(targets)}: {dn}")
                rc, vlog = _run_verify(verify_script, dn, one_out, job_dir)
                _log(job_dir, f"verify rc={rc} {idx}/{len(targets)}: {dn}（明细 {vlog}）")
                if rc != 0:
                    # 闸门⑤（帧对齐 = 发布门槛）：verify 不过（如 B 版漂移）不进
                    # outputs、不算已导出；失败原因与实测数值写进状态机/日志。
                    brief = _verify_failure_brief(vlog, rc)
                    _log(job_dir, f"export 判失败（质检未通过，不登记成品）: {brief}")
                    note_export_result(job_dir, name, False,
                                       pause_after=int(cfg.get("retry_pause_after",
                                                               RETRY_PAUSE_AFTER)),
                                       error=brief, failure_class="verify_failed",
                                       retryable=True, phase="verify", profile=prof)
                    toast("⛔ 质检未通过（不登记成品）", f"{name} {dn}\n{brief}", icon="error")
                    return False
            # 完成收尾的硬门槛：mp4 真在 + progress.done 为真，缺一不算导出成功
            miss_ev = _success_evidence_missing(one_out, export_started)
            if miss_ev:
                brief = f"收尾缺产物证据：缺 {', '.join(miss_ev)}（{one_out}）"
                _log(job_dir, f"export 判失败 {idx}/{len(targets)}: {dn}｜{brief}")
                note_export_result(job_dir, name, False,
                                   pause_after=int(cfg.get("retry_pause_after",
                                                           RETRY_PAUSE_AFTER)),
                                   error=brief, failure_class="artifacts_missing",
                                   retryable=True, phase="export", profile=prof)
                toast("❌ 导出未完成（缺产物证据）", f"{name} {dn}：缺 {', '.join(miss_ev)}",
                      icon="error")
                return False
            exported.append((dn, one_out))
    finally:
        export_lock.release()
    # 走到这里说明本轮拉起/导出链路已跑通（无论是否还有版本待导）→ 清除跨轮状态
    _relaunch_clear(job_dir, "本轮拉起成功")
    if not exported:
        # 全部被跳过：本轮没有任何真实产物，不能走完成收尾（不登记、不通知）
        _log(job_dir, "export 本轮无实际产出（目标均已有成品、已跳过）→ 不做完成收尾")
        return True
    if len(exported) > 1:
        for dn, p in exported[1:]:
            _log(job_dir, f"附加版本成品: {dn} → {p}")
    # 按版本登记成品：video.outputs[<版本>]，顶层字段取基础版聚合。
    # 本轮导出目标路径是确定的（与 _run_export 同一个路径），显式传下去，避免
    # manifest 缺 paths.export_file 时登记失败、video.export_state 留 null。
    for dn, p in exported:
        prof = _profile_of_draft(draft_name, dn)
        try:
            rep = jm.record_video(job_dir,
                                  export_dir=os.path.dirname(p) or None,
                                  export_file=p if os.path.isfile(p) else None,
                                  draft_name=dn, profile=prof)
            _log(job_dir, f"record-video ok 版本={prof} draft={dn} "
                          f"export_dir={rep['video']['export_dir']}")
        except Exception as e:
            _log(job_dir, f"record-video 失败（{dn}）: {e}")
            toast("⚠️ 导出完成但登记失败", f"{name} record-video({dn}): {e}", icon="warning")
        es.succeed(job_dir, profile=prof)
        # 导出成功：清除该草稿的失效标记，下次轮询才认这份成品
        jp.clear_export_stale(os.path.join(_draft_root(), dn))
        # 写时确权：成品旁 sidecar + job E2 基线（best-effort，不影响导出结果）
        try:
            import freshness
            # 无条件交给 freshness 判：成品真在才写 valid；不在则写 stale(output_missing)
            freshness.record_export(job_dir, prof, dn, p)
        except Exception as e:
            _log(job_dir, f"freshness record_export 失败（忽略）: {e}")
    # 所有声明版本都导出后才把顶层置 done；否则保持聚合态，下一轮补未完成版本
    remaining = [d for d in _export_targets(draft_name, profiles, base_profile)
                 if not _draft_exported(d)]
    if remaining:
        _log(job_dir, "仍有版本未导出（等待下一轮）: " + ", ".join(remaining))
    else:
        note_export_result(job_dir, name, True)
        _derive_video_status(job_dir)
    # 从 sidecar 读回实际自愈动作写入真源（唯一写者仍是 export_state；无值不写）
    es.record_recovery(job_dir, _read_progress_recovery(out_mp4))
    if platforms:
        try:
            rep = jm.derive_covers(job_dir, platforms)
            _log(job_dir, f"derive ok: {[d['platform'] for d in rep['derived']]}")
        except Exception as e:
            _log(job_dir, f"derive 失败: {e}")
    else:
        _log(job_dir, "derive 跳过（意图/全局均为不派生）")
    # 通知必须能对上文件：逐个版本给出正式名路径与字节数，避免"无法核对的假完成"
    lines = []
    for dn, p in exported:
        try:
            size = os.path.getsize(p)
        except OSError:
            size = -1
        lines.append(f"{p}（{size} 字节）")
        _log(job_dir, f"导出完成产物: {dn} → {p}（{size} 字节）")
    extra = f"\n另 {len(exported) - 1} 份账号预设草稿" if len(exported) > 1 else ""
    toast("✅ mp4 导出完成", f"{name}\n" + "\n".join(lines) + extra, icon="success")
    _log(job_dir, "chain done")
    return True


def _derive_video_status(job_dir: str) -> None:
    """video.status 由真源派生（唯一写入口）：真源 done → job.json video.status=done。

    video.status 是展示/契约字段，任何地方不得绕过真源直接改它。
    """
    try:
        st = es.load(job_dir)
    except Exception:
        return
    if st.get("status") != "done":
        return
    try:
        job = jm.load_job(os.path.join(job_dir, "job.json"))
    except Exception:
        return
    if (job.get("video") or {}).get("status") == "done":
        return
    try:
        jm.update_job(job_dir, {"video": {"status": "done"}},
                      event="export_state", detail="video.status 由真源派生")
    except Exception as e:
        _log(job_dir, f"video.status 派生失败: {e}")


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

_REOPEN_SOURCE_STAGES = ("video_ready", "publish_ready", "published")
_EXEC_HISTORY_EVENTS = {"ingest", "edit", "export", "record_video",
                        "publish_record", "cleanup", "reopen"}


def _export_state_file_status(job_dir: str) -> Optional[str]:
    """只读 .export_state.json 的 status（不创建、不迁移、不改写）。"""
    try:
        with open(os.path.join(job_dir, ".export_state.json"), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("status") or "")
    except (OSError, ValueError):
        return None


def cmd_reopen(args: argparse.Namespace) -> int:
    """人工重导：stage 退回 covers_ready（或 --to），清空 video 导出标记。

    只允许在需要重导的状态下使用；archived / 活锁 / exporting 一律拒绝。
    故意不走 jm.update_job（它禁止 stage 回退），这里显式放宽并记 history；
    不自动触发任何调度，下一轮是否重跑仍由 watcher 按常规规则决定。
    """
    job_dir = _norm_job_dir(args.job)
    jf = os.path.join(job_dir, "job.json")
    if not os.path.isfile(jf):
        print(f"[FAIL] 找不到 job.json: {jf}")
        return 1
    try:
        job = jm.load_job(jf)
    except Exception as e:
        print(f"[FAIL] job.json 不可解析: {e}")
        return 1
    old = jm.canon_stage(job.get("stage")) or ""
    target = args.to or "covers_ready"
    if target not in jm.STAGES or target == "archived":
        print(f"[FAIL] 非法目标 stage: {target}")
        return 1
    if old == "archived":
        print("[FAIL] stage=archived：已归档剧不允许 reopen（需先人工移回归档区）")
        return 1
    video = job.get("video") or {}
    export_state = str(video.get("export_state") or "")
    needs = (old in _REOPEN_SOURCE_STAGES or video.get("status") == "done"
             or export_state in ("exported", "cleaned"))
    if not needs:
        print(f"[FAIL] 当前 stage={old} / video.status={video.get('status')} / "
              f"export_state={export_state or '-'}，不处于需要重导的状态")
        return 1
    if jm._stage_index(target) >= jm._stage_index(old):
        print(f"[FAIL] --to={target} 必须早于当前 stage={old}")
        return 1
    if lock_alive(job_dir):
        print("[FAIL] 剧正在跑（.watcher.lock pid 存活），请等本轮结束后再 reopen")
        return 1
    if _export_state_file_status(job_dir) == "exporting":
        print("[FAIL] .export_state.json status=exporting：导出进行中，拒绝 reopen")
        return 1
    print(f"reopen 计划（{job_dir}）：")
    print(f"  - stage: {old} → {target}")
    print(f"  - video.status: {video.get('status')} → None")
    print(f"  - video.export_state: {export_state or '-'} → None")
    print("  - 删除 cover_job/.export_state.json（若存在），避免下一轮派生回 done")
    if not args.yes:
        if not sys.stdin.isatty():
            print("[FAIL] 非交互场景必须显式加 --yes")
            return 1
        if input("确认执行? [y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消")
            return 1
    if not acquire_lock(job_dir, phase="reopen"):
        print("[FAIL] 获取 .watcher.lock 失败（可能刚被 watcher 占用），请重试")
        return 1
    try:
        job["stage"] = target
        video = job.setdefault("video", {})
        video["status"] = None
        video["export_state"] = None
        job["revision"] = int(job.get("revision") or 0) + 1
        job.setdefault("history", []).append({
            "at": jm.now_iso(), "event": "reopen",
            "detail": f"人工重导：{old} → {target}，清空 video.status/export_state"})
        jm.save_job(job, job_dir)
        sf = os.path.join(job_dir, ".export_state.json")
        if os.path.exists(sf):
            os.remove(sf)
            print("  已删除 .export_state.json")
        errs = jm.validate_job(jm.load_job(jf), job_dir)
        if errs:
            print(f"[WARN] reopen 后 validate 报 {len(errs)} 项问题（已写入，请人工复核）: "
                  f"{'；'.join(errs[:3])}")
        print(f"[OK] 已 reopen：stage={target}，video.status/export_state 已清空；"
              f"本次不触发调度")
        return 0
    finally:
        release_lock(job_dir)


# ━━━━━━━━━━━━━━━━ stage rerun（执行单元重跑，不回退 stage）━━━━━━━━━━━━━━━━
# 设计见 specs/stage-rerun/spec.md。核心：对外 stage 契约不动，重跑只复用已有
# 产物重做单个执行单元；titles/prompts/covers/publish 是外部生成单元，这里只
# 校验产物并更新新鲜度，不假装生成。

RERUN_UNITS = ("titles", "prompts", "covers", "cover_bind",
               "edit", "verify", "export", "publish")
RERUN_EXTERNAL_UNITS = ("titles", "prompts", "covers", "publish")
RERUN_ROLLBACK_UNITS = ("edit", "cover_bind", "verify")
_RERUN_MIN_STAGE = {
    "titles": "prepared",
    "prompts": "titles_selected",
    "covers": "titles_selected",
    "cover_bind": "covers_ready",
    "verify": "covers_ready",
    "export": "covers_ready",
    "publish": "video_ready",
}
RERUN_DIR = "rerun"


def _rerun_series_dir(job_dir: str) -> str:
    return os.path.dirname(os.path.abspath(job_dir))


def _rerun_label(job: dict[str, Any], job_dir: str) -> str:
    return (str(job.get("series") or job.get("name") or "").strip()
            or os.path.basename(_rerun_series_dir(job_dir)))


def _rerun_base_draft(job: dict[str, Any], job_dir: str) -> str:
    video = job.get("video") or {}
    dn = video.get("draft_name")
    if dn:
        return os.path.basename(str(dn).rstrip("\\/"))
    for ver, out in (video.get("outputs") or {}).items():
        if str(ver) == "a" and (out or {}).get("draft"):
            return os.path.basename(str(out["draft"]).rstrip("\\/"))
    draft = find_stream_draft(_rerun_label(job, job_dir))
    return os.path.basename(draft.rstrip("\\/")) if draft else ""


def _rerun_draft_name(base: str, prof: str) -> str:
    return base if prof == "a" else _account_draft_name(base, prof)


def _rerun_journal_root(job_dir: str) -> str:
    return os.path.join(job_dir, "06_publish", RERUN_DIR)


def _rerun_read_plan(jdir: str) -> dict[str, Any]:
    with open(os.path.join(jdir, "plan.json"), encoding="utf-8") as f:
        return json.load(f)


def _rerun_write_json(jdir: str, name: str, obj: dict[str, Any]) -> None:
    _sio.atomic_write(jdir, name, obj)


def _rerun_progress(jdir: str, backups: list[dict[str, Any]],
                    new_files: list[str], markers: Optional[list[dict[str, Any]]] = None) -> None:
    """执行中增量落盘：失败时即使 executor 没返回，也能按这份证据回滚。"""
    try:
        _rerun_write_json(jdir, "progress.json", {
            "backups": backups, "new_cover_files": new_files,
            "markers": markers or []})
    except Exception as e:
        print(f"[WARN] rerun 进度落盘失败: {e}")


def _rerun_progress_meta(jdir: str) -> dict[str, Any]:
    try:
        with open(os.path.join(jdir, "progress.json"), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _rerun_history(job_dir: str, event: str, detail: str = "") -> None:
    """只动 revision/history/updated_at，不碰 stage / video（回滚也要能写）。"""
    try:
        jf = os.path.join(job_dir, "job.json")
        job = jm.load_job(jf)
        job["revision"] = int(job.get("revision") or 0) + 1
        job.setdefault("history", []).append(
            {"at": jm.now_iso(), "event": event, "detail": detail})
        job["history"] = job["history"][-200:]
        job["updated_at"] = jm.now_iso()
        _sio.atomic_write(job_dir, "job.json", job)
    except Exception as e:
        print(f"[WARN] rerun history 写入失败（忽略）: {e}")


def _rerun_ensure_inputs(unit: str, job: dict[str, Any], job_dir: str,
                         versions: list[str], *, force: bool = False) -> list[str]:
    """只读输入校验：不推进/回退 stage，不调 ingest 硬闸门。"""
    errs: list[str] = []
    stage = jm.canon_stage(job.get("stage")) or ""
    if stage == "archived":
        return ["stage=archived（已归档，不再调度）"]
    if unit == "edit":
        if stage != "covers_ready" and not force:
            errs.append("edit 只允许在 stage=covers_ready 重跑；"
                        "video_ready 及之后请用 --force（或 reopen 重新走完整链路）")
    else:
        min_stage = _RERUN_MIN_STAGE[unit]
        if jm._stage_index(stage) < jm._stage_index(min_stage):
            errs.append(f"{unit} 需要 stage >= {min_stage}（当前 {stage or '空'}）")

    def _need(cond: bool, msg: str) -> None:
        if not cond:
            errs.append(msg)

    if unit in ("titles", "prompts"):
        inp = os.path.join(job_dir, "01_input")
        has_input = os.path.isdir(inp) and any(
            os.path.isfile(os.path.join(inp, n)) for n in os.listdir(inp))
        _need(has_input, "缺 01_input 输入文件")
    if unit in ("prompts", "covers"):
        _need((job.get("titles") or {}).get("gate") == "done",
              "titles.gate != done（prompts/covers 依赖已定稿标题）")
        _need(os.path.isfile(os.path.join(job_dir, "03_prompts", "prompts.json")),
              "缺 03_prompts/prompts.json")
    if unit == "covers":
        found, _ = jm.detect_cover_files(job_dir, apply=False)
        for slot in (1, 2, 3, "vertical"):
            _need(slot in found, f"缺封面 slot={slot}")
        errs.extend(jm.account_cover_errors(job_dir))

    if unit in ("cover_bind", "verify", "export"):
        base = _rerun_base_draft(job, job_dir)
        _need(bool(base), "无法确定基础串流草稿名（video.draft_name 为空）")
        for prof in versions:
            dn = _rerun_draft_name(base, prof)
            dd = os.path.join(_draft_root(), dn)
            _need(os.path.isfile(os.path.join(dd, "draft_content.json")),
                  f"版本 {prof} 缺草稿：{dd}")
            if unit == "cover_bind":
                _need(bool(jm.resolve_profile_cover(job_dir, prof)),
                      f"版本 {prof} 缺竖版封面（resolve_profile_cover=None）")
    if unit == "export":
        video = job.get("video") or {}
        if str(video.get("export_state") or "").lower() == "cleaned":
            errs.append("video.export_state=cleaned：成品已清理，拒绝重导")
        if _export_state_file_status(job_dir) == "exporting":
            errs.append("当前正在导出（.export_state.json status=exporting）")
    if unit == "publish":
        base = _rerun_base_draft(job, job_dir)
        _need(bool(base), "无法确定基础串流草稿名（video.draft_name 为空）")
        for prof in versions:
            dn = _rerun_draft_name(base, prof)
            p = pc.export_mp4_path(dn)
            _need(os.path.isfile(p), f"版本 {prof} 缺成片：{p}")
    return errs


def _rerun_new_cover_files(dd: str) -> set[str]:
    root = os.path.join(dd, "Resources", "cover")
    out: set[str] = set()
    try:
        for n in os.listdir(root):
            out.add(os.path.join(root, n))
    except OSError:
        pass
    return out


def _run_rerun_cmd(cmd: list[str], job_dir: str, log_name: str) -> int:
    """执行重跑子进程；stdout/stderr 全部落 06_publish/<log_name>。"""
    log_dir = os.path.join(job_dir, "06_publish")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_name)
    with open(log_path, "a", encoding="utf-8") as lf:
        lf.write(f"\n[rerun] {time.strftime('%Y-%m-%d %H:%M:%S')} "
                 f"{' '.join(str(c) for c in cmd)}\n")
        lf.flush()
        proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                cwd=_REPO, creationflags=NO_WINDOW)
        rc = proc.wait()
        lf.write(f"[rerun] rc={rc}\n")
    return rc


def _rerun_exec_external(unit: str, job_dir: str) -> dict[str, Any]:
    """外部生成单元：只校验产物 + 标记新鲜度，不生成内容。"""
    import freshness
    if unit in ("titles", "prompts"):
        freshness.mark_intent_changed(job_dir)
        return {"edges": [freshness.EDGE_E1, freshness.EDGE_E3], "generated": False}
    if unit == "covers":
        freshness.mark_covers_pending(job_dir)
        return {"edges": [freshness.EDGE_E3], "generated": False}
    return {"edges": [], "generated": False}


def _rerun_exec_cover_bind(job_dir: str, job: dict[str, Any],
                           versions: list[str], jdir: str) -> dict[str, Any]:
    import draft_io
    from jy_cover import set_cover_composition, set_draft_cover
    import freshness
    base = _rerun_base_draft(job, job_dir)
    backups: list[dict[str, str]] = []
    new_files: list[str] = []
    changed: list[dict[str, Any]] = []
    for prof in versions:
        dn = _rerun_draft_name(base, prof)
        dd = os.path.join(_draft_root(), dn)
        dp = os.path.join(dd, "draft_content.json")
        cover = jm.resolve_profile_cover(job_dir, prof)
        before_files = _rerun_new_cover_files(dd)
        with open(dp, encoding="utf-8") as f:
            draft = json.load(f)
        if not set_cover_composition(draft, dd, cover):
            raise RuntimeError(f"版本 {prof} 封面 composition 写入失败：{cover}")
        bak = draft_io.write_draft_content(
            draft, dp, job_dir=job_dir, writer="cover_only", indent=2,
            backup_suffix=".rerun_backup_{ts}.json")
        if bak:
            backups.append({"path": dp, "backup": bak})
        else:
            # 没拿到备份路径时补一份最小安全网
            safe = os.path.join(jdir, f"draft_{prof}.before.json")
            shutil.copy2(dp, safe)
            backups.append({"path": dp, "backup": safe})
        _rerun_progress(jdir, backups, new_files)
        if not set_draft_cover(dd, cover_image=cover):
            raise RuntimeError(f"版本 {prof} draft_cover.jpg 写入失败")
        new_files.extend(sorted(_rerun_new_cover_files(dd) - before_files))
        _rerun_progress(jdir, backups, new_files)
        freshness.record_cover(job_dir, prof, dd, cover)
        changed.append({"profile": prof, "draft": dn, "draft_path": dp,
                        "cover": cover})
    return {"changed": changed, "backups": backups,
            "new_cover_files": new_files,
            "edges": [freshness.EDGE_E1]}


def _rerun_exec_edit(job_dir: str, job: dict[str, Any],
                     versions: list[str], jdir: str) -> dict[str, Any]:
    base = _rerun_base_draft(job, job_dir)
    backups: list[dict[str, str]] = []
    markers: list[dict[str, Any]] = []
    for prof in versions:
        dp = os.path.join(_draft_root(), _rerun_draft_name(base, prof),
                          "draft_content.json")
        dd = os.path.dirname(dp)
        if os.path.isfile(dp):
            safe = os.path.join(jdir, f"draft_{prof}.before.json")
            shutil.copy2(dp, safe)
            backups.append({"path": dp, "backup": safe})
        marker = os.path.join(dd, jp.EXPORT_STALE_NAME)
        markers.append({"path": marker, "existed": os.path.isfile(marker)})
        # 先让旧成品失效：即使本轮中断，也不会把旧 mp4 当当前草稿的导出
        jp.mark_export_stale(dd, reason="rerun.edit")
        _rerun_progress(jdir, backups, [], markers)
    cmd = [sys.executable, "-u", _BATCH_RUNNER, _rerun_series_dir(job_dir),
           "--force", "--no-export"]
    if versions:
        cmd += ["--profile", ",".join(versions)]
    rc = _run_rerun_cmd(cmd, job_dir, "rerun_edit.log")
    if rc != 0:
        raise RuntimeError(f"batch_runner rc={rc}（详见 06_publish/rerun_edit.log）")
    return {"backups": backups, "markers": markers, "rc": rc,
            "edges": ["draft_to_video"]}


def _rerun_exec_verify(job_dir: str, job: dict[str, Any],
                       versions: list[str], jdir: str) -> dict[str, Any]:
    base = _rerun_base_draft(job, job_dir)
    script = os.path.join(_REPO, "pipeline", "verify_draft.py")
    results: list[dict[str, Any]] = []
    backups: list[dict[str, str]] = []
    markers: list[dict[str, Any]] = []
    for prof in versions:
        dp = os.path.join(_draft_root(), _rerun_draft_name(base, prof),
                          "draft_content.json")
        dd = os.path.dirname(dp)
        safe = os.path.join(jdir, f"draft_{prof}.before.json")
        shutil.copy2(dp, safe)
        backups.append({"path": dp, "backup": safe})
        marker = os.path.join(dd, jp.EXPORT_STALE_NAME)
        markers.append({"path": marker, "existed": os.path.isfile(marker)})
        _rerun_progress(jdir, backups, [], markers)
        with open(dp, "rb") as f:
            before = hashlib.sha256(f.read()).hexdigest()
        rc = _run_rerun_cmd([sys.executable, "-u", script, dd, "--fix"],
                            job_dir, "rerun_verify.log")
        if rc != 0:
            raise RuntimeError(f"verify_draft rc={rc}（详见 06_publish/rerun_verify.log）")
        with open(dp, "rb") as f:
            after = hashlib.sha256(f.read()).hexdigest()
        repaired = before != after
        if repaired:
            import freshness
            freshness.record_draft_write(job_dir, dp, "rerun_verify")
            jp.mark_export_stale(dd, reason="rerun.verify")
        results.append({"profile": prof, "repaired": repaired, "rc": rc})
    return {"results": results, "backups": backups, "markers": markers,
            "edges": ["draft_to_video"] if any(r["repaired"] for r in results) else []}


def _rerun_exec_export(job_dir: str, job: dict[str, Any],
                       versions: list[str], jdir: str) -> dict[str, Any]:
    base = _rerun_base_draft(job, job_dir)
    targets = [_rerun_draft_name(base, p) for p in versions]
    plan = {
        "action": "export",
        "name": _rerun_label(job, job_dir),
        "draft": os.path.join(_draft_root(), base),
        "profiles": list(versions),
        "export_targets": targets,
        "force_reexport": True,
    }
    ok = run_export(job_dir, plan,
                    {"retry_pause_after": RETRY_PAUSE_AFTER},
                    dry_run=False, verify=True, no_derive=True)
    if not ok:
        raise RuntimeError("export 失败（详见 06_publish/pipeline.log 与 .export_state.json）")
    return {"targets": targets, "edges": ["draft_to_video"]}


_RERUN_EXEC = {
    "titles": lambda jd, job, vers, jdir: _rerun_exec_external("titles", jd),
    "prompts": lambda jd, job, vers, jdir: _rerun_exec_external("prompts", jd),
    "covers": lambda jd, job, vers, jdir: _rerun_exec_external("covers", jd),
    "publish": lambda jd, job, vers, jdir: _rerun_exec_external("publish", jd),
    "cover_bind": _rerun_exec_cover_bind,
    "edit": _rerun_exec_edit,
    "verify": _rerun_exec_verify,
    "export": _rerun_exec_export,
}


def _rerun_will_write(unit: str) -> list[str]:
    return {
        "titles": ["job.freshness.E1/E3（不生成内容）"],
        "prompts": ["job.freshness.E1/E3（不生成内容）"],
        "covers": ["job.freshness.E3（不生成内容）"],
        "cover_bind": ["各版本 draft_content.json", "各版本 draft_cover.jpg",
                       "Resources/cover/*", "job.freshness.E1"],
        "edit": ["串流草稿（batch_runner 全链路）", "_export_stale.json",
                 "job.freshness.E2"],
        "verify": ["草稿（仅修复时）", "_export_stale.json（仅修复时）"],
        "export": ["mp4 成品", "video.outputs / export_state", "job.freshness.E2"],
        "publish": ["无（只校验）"],
    }[unit]


def _rerun_restore(jdir: str, unit: str, meta: dict[str, Any],
                   job_dir: str) -> None:
    """失败/回滚：恢复 job.before.json + 草稿备份 + 删除本轮新增封面文件。"""
    for ent in meta.get("backups") or []:
        path, bak = ent.get("path"), ent.get("backup")
        if path and bak and os.path.isfile(bak):
            tmp = path + ".rerun_restore.tmp"
            shutil.copy2(bak, tmp)
            os.replace(tmp, path)
    for p in meta.get("new_cover_files") or []:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass
    for ent in meta.get("markers") or []:
        # 本轮之前没有失效标记的，回滚时一并摘掉；原有的保持（语义仍是旧成品失效）
        if not ent.get("existed") and ent.get("path") and os.path.isfile(ent["path"]):
            try:
                os.remove(ent["path"])
            except OSError:
                pass
    snap = os.path.join(jdir, "job.before.json")
    if os.path.isfile(snap):
        try:
            old = jm.load_job(snap)
            cur = jm.load_job(os.path.join(job_dir, "job.json"))
            old["revision"] = max(int(cur.get("revision") or 0),
                                  int(old.get("revision") or 0)) + 1
            old.setdefault("history", []).append({
                "at": jm.now_iso(), "event": f"rerun.{unit}.rolled_back",
                "detail": "失败后按快照回滚（stage / video / freshness 恢复）"})
            old["history"] = old["history"][-200:]
            old["updated_at"] = jm.now_iso()
            _sio.atomic_write(job_dir, "job.json", old)
        except Exception as e:
            print(f"[WARN] rerun 回滚 job.json 失败: {e}")
    for p in (os.path.join(jdir, "draft_content.json.rerun_restore.tmp"),):
        if os.path.isfile(p):
            os.remove(p)


def _rerun_run(job_dir: str, unit: str, versions: list[str], *,
               force: bool, yes: bool, dry_run: bool,
               resume_jdir: Optional[str] = None) -> int:
    jf = os.path.join(job_dir, "job.json")
    if not os.path.isfile(jf):
        print(f"[FAIL] 找不到 job.json: {jf}")
        return 1
    try:
        job = jm.load_job(jf)
    except Exception as e:
        print(f"[FAIL] job.json 不可解析: {e}")
        return 1
    stage = jm.canon_stage(job.get("stage")) or ""
    base = _rerun_base_draft(job, job_dir)

    if not versions:
        versions = _runnable_profiles(_job_profiles(job, job_dir)) or ["a"]
    if not force and unit == "edit" and stage != "covers_ready":
        print("[FAIL] edit 在 video_ready 及之后重跑必须显式 --force"
              "（或改用 reopen 重新走完整链路）")
        return 1
    if lock_alive(job_dir):
        print("[FAIL] 剧正在跑（.watcher.lock pid 存活），请等本轮结束后再 rerun")
        return 1

    errs = _rerun_ensure_inputs(unit, job, job_dir, versions, force=force)
    if errs:
        print(f"[FAIL] rerun --unit {unit} 前置条件不满足：")
        for e in errs:
            print(f"  - {e}")
        return 1

    ext = unit in RERUN_EXTERNAL_UNITS
    print(f"rerun 计划（{job_dir}）：")
    print(f"  - unit: {unit}    stage: {stage}（不推进/不回退）")
    print(f"  - versions: {', '.join(versions)}（基础草稿 {base or '-'}）")
    print(f"  - 将写: {'；'.join(_rerun_will_write(unit))}")
    if ext:
        print("  - 外部生成单元：只校验产物并标记新鲜度，不执行生成")
    if dry_run:
        print("[DRY-RUN] 只校验，不写任何文件、不取锁")
        return 0
    if not yes:
        if not sys.stdin.isatty():
            print("[FAIL] 非交互场景必须显式加 --yes")
            return 1
        if input("确认执行? [y/N] ").strip().lower() not in ("y", "yes"):
            print("已取消")
            return 1

    if resume_jdir:
        jdir = resume_jdir
        meta_plan = _rerun_read_plan(jdir)
        if meta_plan.get("status") == "done":
            print(f"[OK] 该 journal 已完成：{jdir}")
            return 0
    else:
        jdir = os.path.join(_rerun_journal_root(job_dir),
                            time.strftime("%Y%m%d_%H%M%S") + "_" + unit)
        os.makedirs(jdir, exist_ok=True)
        shutil.copy2(jf, os.path.join(jdir, "job.before.json"))
        _rerun_write_json(jdir, "plan.json", {
            "unit": unit, "versions": versions, "stage_before": stage,
            "revision_before": job.get("revision"),
            "will_write": _rerun_will_write(unit),
            "status": "running", "pid": os.getpid(),
            "started_at": jm.now_iso(), "job_dir": job_dir,
        })

    if not acquire_lock(job_dir, phase=f"rerun:{unit}"):
        print("[FAIL] 获取 .watcher.lock 失败（可能刚被 watcher 占用），请重试")
        return 1
    _rerun_history(job_dir, f"rerun.{unit}.start",
                   f"versions={','.join(versions)} stage={stage}")
    try:
        meta = _RERUN_EXEC[unit](job_dir, job, versions, jdir) or {}
        _rerun_write_json(jdir, "done.json", {
            "unit": unit, "status": "done", "ended_at": jm.now_iso(), **meta})
        try:
            plan = _rerun_read_plan(jdir)
            plan.update({"status": "done", "ended_at": jm.now_iso()})
            _rerun_write_json(jdir, "plan.json", plan)
        except Exception:
            pass
        _rerun_history(job_dir, f"rerun.{unit}.done",
                       f"versions={','.join(versions)}")
        print(f"[OK] rerun --unit {unit} 完成：versions={','.join(versions)}")
        print(f"  journal: {jdir}")
        return 0
    except Exception as e:
        print(f"[FAIL] rerun --unit {unit} 失败: {e}")
        try:
            meta = _rerun_read_plan(jdir) or {}
        except Exception:
            meta = {}
        for k, v in _rerun_progress_meta(jdir).items():
            meta.setdefault(k, v)
        _rerun_write_json(jdir, "failed.json", {
            "unit": unit, "status": "failed", "error": str(e),
            "ended_at": jm.now_iso(), **meta})
        _rerun_history(job_dir, f"rerun.{unit}.failed", str(e))
        if unit in RERUN_ROLLBACK_UNITS:
            _rerun_restore(jdir, unit, meta, job_dir)
            print("  已按快照回滚（草稿/job.json）；journal 保留供 --rollback/--resume")
        try:
            plan = _rerun_read_plan(jdir)
            plan.update({"status": "failed", "error": str(e),
                         "ended_at": jm.now_iso()})
            _rerun_write_json(jdir, "plan.json", plan)
        except Exception:
            pass
        return 1
    finally:
        release_lock(job_dir)


def cmd_rerun(args: argparse.Namespace) -> int:
    if args.rollback:
        return _rerun_rollback(args.rollback)
    if args.resume:
        try:
            plan = _rerun_read_plan(args.resume)
        except Exception as e:
            print(f"[FAIL] journal 不可读: {e}")
            return 1
        return _rerun_run(_norm_job_dir(plan["job_dir"]), plan["unit"],
                          list(plan.get("versions") or []), force=True,
                          yes=args.yes, dry_run=False,
                          resume_jdir=args.resume)
    if not args.job:
        print("[FAIL] 缺少 --job（或 --resume/--rollback）")
        return 1
    versions = [v for v in (args.versions or "").split(",") if v.strip()]
    return _rerun_run(_norm_job_dir(args.job), args.unit, versions,
                      force=args.force, yes=args.yes, dry_run=args.dry_run)


def _rerun_rollback(jdir: str) -> int:
    jdir = os.path.abspath(jdir)
    try:
        plan = _rerun_read_plan(jdir)
    except Exception as e:
        print(f"[FAIL] journal 不可读: {e}")
        return 1
    job_dir = _norm_job_dir(plan.get("job_dir") or "")
    if not os.path.isfile(os.path.join(job_dir, "job.json")):
        print(f"[FAIL] journal 指向的 job 不存在: {job_dir}")
        return 1
    if lock_alive(job_dir):
        print("[FAIL] 剧正在跑（.watcher.lock pid 存活），拒绝回滚")
        return 1
    meta: dict[str, Any] = {}
    for n in ("done.json", "failed.json"):
        p = os.path.join(jdir, n)
        if os.path.isfile(p):
            try:
                meta = json.load(open(p, encoding="utf-8"))
            except Exception:
                meta = {}
    for k, v in _rerun_progress_meta(jdir).items():
        meta.setdefault(k, v)
    if not acquire_lock(job_dir, phase="rerun:rollback"):
        print("[FAIL] 获取 .watcher.lock 失败，请重试")
        return 1
    try:
        _rerun_restore(jdir, plan.get("unit") or "rerun", meta, job_dir)
        _rerun_write_json(jdir, "rolled_back.json",
                          {"status": "rolled_back", "at": jm.now_iso()})
        print(f"[OK] 已回滚: {jdir}")
        return 0
    finally:
        release_lock(job_dir)


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


def _read_json_readonly(path: str) -> Optional[dict[str, Any]]:
    """只读 JSON（缺失/损坏 → None）：inspect 专用，绝不创建或修复文件。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _fmt_val(v: Any) -> str:
    return "缺失" if v is None else str(v)


def _fmt_ts(v: Any, now: float) -> str:
    """epoch 秒 → ISO（相对 now 的可读提示）；缺失/0 分别标注。"""
    if v is None:
        return "缺失"
    try:
        ts = float(v)
    except (TypeError, ValueError):
        return _fmt_val(v)
    if ts <= 0:
        return "0（立即可重试）"
    iso = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))
    delta = int(ts - now)
    rel = f"{delta}s 后" if delta > 0 else f"已过期 {-delta}s"
    return f"{iso}（{rel}）"


def cmd_inspect(args: argparse.Namespace) -> int:
    """只读聚合视图：intent / edit / export 三个来源一次打印。

    纯只读：不读写锁、不落盘、不迁移、不 reap、不触发调度；缺文件显示「缺失」。
    """
    for j in args.job:
        job_dir = _norm_job_dir(j)
        now = time.time()
        print(f"{job_dir}")

        job = _read_json_readonly(os.path.join(job_dir, "job.json"))
        if job is None:
            print("  intent：缺失（job.json 不存在或不可读）")
        else:
            wf = dict(job.get("workflow") or {})
            ec = _read_json_readonly(os.path.join(job_dir, EXPORT_CONFIG_NAME)) or {}
            vid = job.get("video") or {}
            print("  intent（job.json）")
            print(f"    stage={_fmt_val(job.get('stage'))} "
                  f"mode={_fmt_val(wf.get('mode'))} "
                  f"export_policy={_fmt_val(wf.get('export_policy'))} "
                  f"intent_revision={_fmt_val(wf.get('intent_revision'))} "
                  f"intent_source={_fmt_val(wf.get('intent_source'))}")
            print(f"    cut_s: workflow={_fmt_val(wf.get('cut_s'))} "
                  f"export_config={_fmt_val(ec.get('cut_s'))} "
                  f"video={_fmt_val(vid.get('cut_s'))}")

        est = _sio.read_raw(job_dir, eds.STATE_NAME)
        if est is None:
            print(f"  edit（{eds.STATE_NAME}）：缺失")
        else:
            pause = "paused" if est.get("status") == "paused" else "未暂停"
            print(f"  edit（{eds.STATE_NAME}）")
            print(f"    version={_fmt_val(est.get('version'))} "
                  f"status={_fmt_val(est.get('status'))} "
                  f"attempts={_fmt_val(est.get('attempts'))} "
                  f"连续失败={_fmt_val(est.get('consecutive_failures'))}（{pause}）")
            print(f"    next_retry_at={_fmt_ts(est.get('next_retry_at'), now)} "
                  f"phase={_fmt_val(est.get('phase'))} "
                  f"failure_class={_fmt_val(est.get('failure_class'))}")
            print(f"    last_error={_fmt_val(est.get('last_error'))} "
                  f"healed_dirs={_fmt_val(est.get('healed_dirs'))} "
                  f"updated_at={_fmt_val(est.get('updated_at'))} "
                  f"writer={_fmt_val(est.get('writer'))}")

        xst = _sio.read_raw(job_dir, es.STATE_NAME)
        if xst is None:
            print(f"  export（{es.STATE_NAME}）：缺失")
        else:
            hb = xst.get("heartbeat_ts")
            try:
                hb_age = now - float(hb or 0)
            except (TypeError, ValueError):
                hb_age = None
            age_txt = "缺失" if hb_age is None else f"{int(hb_age)}s 前"
            print(f"  export（{es.STATE_NAME}）")
            print(f"    version={_fmt_val(xst.get('version'))} "
                  f"status={_fmt_val(xst.get('status'))} "
                  f"attempts={_fmt_val(xst.get('attempts'))} "
                  f"连续失败={_fmt_val(xst.get('consecutive_failures'))}")
            print(f"    next_retry_at={_fmt_ts(xst.get('next_retry_at'), now)} "
                  f"progress={_fmt_val(xst.get('progress'))} "
                  f"phase={_fmt_val(xst.get('phase'))} "
                  f"failure_class={_fmt_val(xst.get('failure_class'))}")
            print(f"    heartbeat_ts={_fmt_val(hb)}（{age_txt}） "
                  f"last_error={_fmt_val(xst.get('last_error'))}")
            print(f"    last_recovery_action={_fmt_val(xst.get('last_recovery_action'))} "
                  f"last_recovery_at={_fmt_val(xst.get('last_recovery_at'))} "
                  f"updated_at={_fmt_val(xst.get('updated_at'))} "
                  f"writer={_fmt_val(xst.get('writer'))}")
            if (xst.get("status") == "exporting" and age_txt != "缺失"
                    and hb_age is not None and hb_age > es.STALE_AFTER_S):
                print(f"    ⚠️ 心跳超过 {es.STALE_AFTER_S}s：只读推导为 stale"
                      f"（未回收；回收落盘由导出链 reap_stale 执行）")
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
                intent_states: dict[str, dict[str, Any]],
                notify: bool = True, write_log: bool = True) -> list[dict[str, Any]]:
    """一轮评估：意图同步 + 触发判定 + 计划推导。返回候选计划列表（未执行）。

    两遍推导：第一遍数出待剪辑队列（pending_edit），第二遍用它套策略门槛，
    保证 after_edit_queue_drained 基于本轮全局编辑队列生效。
    notify/write_log 透传给 build_plan/_effective_cut（见 _round_flags 的三档语义）。
    """
    qualified: list[str] = []
    for job_dir in jobs:
        if job_dir in done:
            continue
        name = os.path.basename(os.path.dirname(job_dir.rstrip("\\/")))
        if write_log:
            # 只在真正跑轮次时回收卡死的 editing；plan/dry-run 一律只读
            _reap_stalled_edit(job_dir, cfg, name=name)
        if lock_alive(job_dir):
            if args.verbose:
                print(f"[skip] {job_dir}: 活锁存在（链在跑）")
            continue
        ok, reasons = check_trigger(job_dir)
        if not ok:
            if getattr(args, "once", False) or args.verbose:
                print(f"[skip] {job_dir}: {'；'.join(reasons)}")
            continue
        job = jm.load_job(os.path.join(job_dir, "job.json"))
        if _new_job_in_grace(job_dir, job, cfg):
            grace = cfg.get("new_job_grace_min", 10)
            print(f"[wait] {job_dir}: 新剧宽限期内（<{grace}min 且 job.json 未被改动，本轮跳过）")
            continue
        _sync_intent(job_dir, name, intent_states.setdefault(job_dir.lower(), {}))
        idle = time.time() - last_change_ts(job_dir)
        if idle < args._settle:
            print(f"[wait] {job_dir}: 条件满足，静默确认中（{idle:.0f}/{args._settle:.0f}s）")
            continue
        qualified.append(job_dir)
    pending_edit = 0
    for j in qualified:
        job = jm.load_job(os.path.join(j, "job.json"))
        export_state = str(((job.get("video") or {})).get("export_state") or "").lower()
        if export_state in ("exported", "cleaned"):
            continue  # 已导出/已清理的剧不再计入待剪辑队列（否则会拖住导出闸门）
        if find_stream_draft(os.path.basename(os.path.dirname(j).rstrip("\\/"))) is None:
            pending_edit += 1
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
                                phase_override=getattr(args, "phase", None),
                                notify=notify, write_log=write_log,
                                profiles=getattr(args, "profile", None)))
    return plans


def _pick(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """每轮只推进一部：edit 优先（不占剪映），clone 次之（几秒），再按优先级/路径。"""
    actionable = [p for p in candidates if p["action"] in ("edit", "clone", "export")]
    if not actionable:
        return None
    action_rank = {"edit": 0, "clone": 1, "export": 2}
    # 同一档内"待重拉"排最前：上一轮拉起失败的任务下一轮优先重新拉起。
    actionable.sort(key=lambda p: (action_rank.get(p["action"], 9),
                                   0 if p.get("relaunch") else 1,
                                   -int(p["intent"].get("priority") or 0),
                                   p["job_dir"].lower()))
    return actionable[0]


def _round_flags(args: argparse.Namespace, hold_on: bool) -> tuple[bool, bool]:
    """本轮 (notify, write_log) 三档语义：
      预览（--dry-run / --hold / 配置 hold:true）：(False, False) 只打印；
      --once 真实执行：(False, True) 记日志、不弹通知；
      持续 watcher 真实轮次：(True, True) 日志 + 通知。
    """
    if getattr(args, "dry_run", False) or hold_on:
        return False, False
    return (not getattr(args, "once", False)), True


def _round_state_brief(job_dir: str, action: str) -> tuple:
    """本轮执行前的状态快照（status / 连续失败数 / last_error），用于判断
    chain 异常时失败是否已被本次执行登记，避免重复计数。"""
    st = (eds.peek(job_dir) if action == "edit"
          else _sio.read_raw(job_dir, es.STATE_NAME)) or {}
    return (st.get("status"), int(st.get("consecutive_failures") or 0), st.get("last_error"))


def _record_chain_failure(job_dir: str, name: str, action: str,
                          cfg: dict[str, Any], err: BaseException,
                          before: tuple) -> None:
    """本地执行链的未捕获异常 → 必须登记一次失败，否则每轮立即重试、无限空转。

    若本次执行已经写过状态机（状态已进入 done/retrying/paused/failed 且与执行前
    快照不同），只记日志、不重复计数；否则说明异常直接掀翻了这一轮（失败计数、
    退避、阈值暂停全被跳过），补记失败让它们照常生效。
    登记过程本身再异常也只记日志，绝不让 daemon 因一处状态写失败而退出。
    """
    text = f"chain 异常: {_exception_brief(err, where=action)}"
    try:
        after = _round_state_brief(job_dir, action)
        if after[0] in ("done", "retrying", "paused", "failed") and after != before:
            _log(job_dir, f"{text}（本次执行已登记为 {after[0]}，不重复计数）")
            return
        if action == "edit":
            note_edit_result(job_dir, name, False, error=text,
                             failure_class="chain_exception", retryable=True)
        else:
            note_export_result(job_dir, name, False,
                               pause_after=int(cfg.get("retry_pause_after", RETRY_PAUSE_AFTER)),
                               error=text, failure_class="chain_exception",
                               retryable=True, phase="export")
    except Exception as e2:
        _log(job_dir, f"{text}（登记失败时再次异常: {e2}）")


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
    candidates = _eval_round(args, cfg, jobs, set(), states, notify=False, write_log=False)
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
    # --phase 是人工放行 paused/强制阶段的入口：先确认在跑的 watcher 不是旧代码，
    # 否则旧进程会继续按旧逻辑空转、写坏缓存（2026-11-07 事故）。
    forced_phase = getattr(args, "phase", None)
    if forced_phase:
        reason = check_release_safe(forced_phase)
        if reason:
            print(f"[FAIL] {reason}")
            return 1
    jobs, roots = _collect_jobs(args, cfg)
    if not jobs and (args.once or args.dry_run or not roots):
        print("[FAIL] 没有可监听的任务：给 --job <cover_job>、--root <目录>，"
              "或在 pipeline/presets.json 的 watch 段配置 roots")
        return 1
    if not jobs:
        # 根目录存在但暂时没有 cover_job（如全部归档）：常驻空转、每轮重扫，
        # 等新剧落盘后自动接上；一次性/预览模式仍在上面 fail-fast。
        print(f"[idle] 本轮扫描无 cover_job（根目录: {', '.join(roots)}），持续监听中")
    settle = args.settle if args.settle is not None else float(cfg.get("settle", 30.0))
    interval = args.interval if args.interval is not None else float(cfg.get("interval", 5.0))
    args._settle = settle
    print(f"cover_watch 监听 {len(jobs)} 个任务（静默 {settle:.0f}s，轮询 {interval:.0f}s，"
          f"roots={roots or '无'}，export_policy={cfg.get('export_policy', 'immediate')}，"
          f"配置热加载）")
    done: set[str] = set()
    known: set[str] = set()
    intent_states: dict[str, dict[str, Any]] = {}
    hb = _WatcherHeartbeat()
    hb.start()
    round_no = 0
    empty_logged = False
    while True:
        round_no += 1
        hb.update(stage="scan", job=None, phase=None, round=round_no)
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
        # 空转态只在新旧切换时打一行，避免每轮 5s 刷屏（日志按 10MB×5 轮转）
        if not jobs and not empty_logged:
            print(f"[idle] 第 {round_no} 轮扫描：无 cover_job"
                  f"（根目录: {', '.join(roots) or '无'}），继续监听")
            empty_logged = True
        elif jobs and empty_logged:
            print(f"[idle] 第 {round_no} 轮扫描：发现待处理剧集，恢复调度")
            empty_logged = False
        fresh = [j for j in jobs if j.lower() not in known]
        for j in fresh:
            print(f"[new] 自动发现任务: {j}")
            known.add(j.lower())

        # 仅「持续 watcher 真实轮次」弹通知；--once 真实执行记日志不弹；预览两者都不做
        notify, write_log = _round_flags(args, hold_on)
        candidates = _eval_round(args, cfg, jobs, done, intent_states,
                                 notify=notify, write_log=write_log)
        if hold_on or args.dry_run:
            # 只列计划不执行：全部候选（含被挡原因）
            for p in candidates:
                print(_plan_line(p))
            if not candidates and (args.once or args.verbose):
                print("[plan] 本轮无可推进任务")
            if args.once:
                hb.stop()
                return 0
            time.sleep(interval)
            continue
        pick = _pick(candidates)
        if pick is None:
            if args.once:
                hb.stop()
                return 0
            time.sleep(interval)
            continue
        if args.verbose:
            for p in candidates:
                if p is not pick and p["action"] == "blocked":
                    print(_plan_line(p))
        if not acquire_lock(pick["job_dir"], phase=pick["action"]):
            hb.update(stage="wait-lock", job=pick["job_dir"], phase=None, round=round_no)
            time.sleep(interval)
            continue
        hb.update(stage=pick["action"], job=pick["job_dir"], phase="running",
                  round=round_no)
        before_state = _round_state_brief(pick["job_dir"], pick["action"])
        try:
            if pick["action"] == "edit":
                success = run_edit(pick["job_dir"], pick, dry_run=False)
            elif pick["action"] == "clone":
                success = run_clone(pick["job_dir"], pick, cfg, dry_run=False)
            else:
                success = run_export(pick["job_dir"], pick, cfg, dry_run=False,
                                     verify=not args.no_verify,
                                     no_derive=args.no_derive)
        except Exception as e:
            # 先终止本轮派生的子进程树，再登记失败：异常掀翻本轮时若 batch_runner /
            # ffmpeg 还活着，下一轮会与它并发写同一份缓存/草稿（2026-11-07 六个坏缓存
            # 事故）。按 pid 树只杀本 watcher 的后代，不按进程名，避免误杀别的剧。
            killed = _kill_descendants(os.getpid())
            brief = _exception_brief(e, where=f"cmd_watch.{pick['action']}")
            _log(pick["job_dir"], f"chain 异常: {brief}")
            if killed:
                _log(pick["job_dir"],
                     f"chain 异常：已按 pid 树终止本轮子进程 {len(killed)} 个 "
                     f"(pids={killed})")
            # 异常掀翻本轮的路径必须登记失败：否则计数/退避/阈值暂停全被跳过，
            # 坏代码或坏数据会变成 30 秒一轮的永久空转（本轮修复的历史事故）。
            _record_chain_failure(pick["job_dir"], pick["name"], pick["action"],
                                  cfg, e, before_state)
            toast("❌ 本地执行链异常", f"{pick['name']}: {brief}", icon="error")
            success = False
        finally:
            release_lock(pick["job_dir"])
            hb.update(stage="idle", job=None, phase=None, round=round_no)
        if success and pick["action"] == "export":
            done.add(pick["job_dir"])
        if args.once:
            hb.stop()
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
    ap.add_argument("--profile", action="append", default=None,
                    help="只处理指定版本（如 a；可逗号分隔或多次），其余版本不参与本轮")
    ap.add_argument("--no-derive", action="store_true", help="跳过平台派生图（保留兼容）")
    ap.add_argument("--no-verify", action="store_true",
                    help="（保留兼容；导出阶段质检随 batch_runner/verify_export 默认开启）")
    ap.add_argument("--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command")
    p_st = sub.add_parser("status", help="查看锁与进度")
    p_st.add_argument("--job", action="append", required=True)
    p_in = sub.add_parser("inspect", help="只读聚合视图：intent / edit / export（不写文件、不触发调度）")
    p_in.add_argument("--job", action="append", required=True,
                      help="cover_job 目录或其中的 job.json")
    p_pl = sub.add_parser("plan", help="查看所有剧的阶段/意图/下一步/被挡原因（不执行）")
    p_pl.add_argument("--job", action="append", default=None)
    p_pl.add_argument("--root", action="append", default=None)
    p_pl.add_argument("--config", default=None)
    p_pl.add_argument("--phase", choices=["edit", "export"], default=None)
    p_ro = sub.add_parser("reopen", help="人工重导：退回 covers_ready 并清空导出标记（不触发调度）")
    p_ro.add_argument("--job", required=True, help="cover_job 目录或其中的 job.json")
    p_ro.add_argument("--to", default="covers_ready",
                      help="目标 stage（默认 covers_ready，必须早于当前 stage）")
    p_ro.add_argument("--yes", action="store_true", help="非交互场景必填，跳过确认")
    p_rr = sub.add_parser(
        "rerun", help="重跑单个执行单元（不回退 stage；支持 --dry-run/--resume/--rollback）")
    p_rr.add_argument("--job", default=None, help="cover_job 目录或其中的 job.json")
    p_rr.add_argument("--unit", choices=list(RERUN_UNITS), default=None,
                      help="执行单元；titles/prompts/covers/publish 为外部生成单元"
                           "（只校验产物并标记新鲜度）")
    p_rr.add_argument("--versions", default=None,
                      help="只重跑指定版本（逗号分隔，如 a,b；默认全部可运行版本）")
    p_rr.add_argument("--dry-run", action="store_true", help="只校验输入，不写文件、不取锁")
    p_rr.add_argument("--yes", action="store_true", help="非交互场景必填，跳过确认")
    p_rr.add_argument("--force", action="store_true",
                      help="允许 edit 在 video_ready 及之后重跑")
    p_rr.add_argument("--resume", default=None, help="从 journal 目录继续一次未完成的 rerun")
    p_rr.add_argument("--rollback", default=None, help="按 journal 回滚一次 rerun")
    args = ap.parse_args(argv)
    if getattr(args, "profile", None):
        args.profile = pc.parse_preset_profiles(args.profile)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "reopen":
        return cmd_reopen(args)
    if args.command == "rerun":
        return cmd_rerun(args)
    return cmd_watch(args)


if __name__ == "__main__":
    sys.exit(main())
