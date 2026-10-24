"""批量任务 Watcher 守护进程：轮询 _batch_state.json 和日志文件变化，触发桌面通知。

用法:
    python watcher.py --dir <workdir>                    # 常驻监控，直到所有集终态
    python watcher.py --dir <workdir> --interval 5       # 轮询间隔 5s（默认 3s）
    python watcher.py --dir <workdir> --once             # 只轮询一次（供 Agent 定时调用）
    python watcher.py --dir <workdir> --heartbeat 60     # 60s 无新事件时弹心跳进度（默认开启）
    python watcher.py --dir <workdir> --pid 12345        # 监控指定主进程：其死亡立即告警，
                                                         # 且全部终态时主进程活着则静默退出（去重）

设计:
    - 轻量（单线程单文件轮询，内存 <1MB），脚本崩了不影响 batch_runner 主流程
    - 只报增量变化：首次启动读快照，只通知新出现的事件
    - 终态自动退出：所有视频 status ∈ {done, failed, skipped} 时汇总通知后退出
      （--pid 指定主进程时：主进程存活则收尾 toast 由主进程发，watcher 静默退出避免重复）
    - 主进程监控：--pid 指定的进程中途死亡时立即告警（崩溃兜底核心路径）
    - 自愈：单轮异常不退出（连续 5 轮异常才告警退出），未捕获异常临终 toast
    - 两个信号源: _batch_state.json（结构化状态） + *.log 文件（关键字正则）
    - 通知走 notifier.toast()，失败静默，不影响主循环
    - 事件限速聚合：同轮询 2s 窗口内多个状态变化合并为一条 toast（防 30 条轰炸）
    - 日志关键字正则 + 误匹配排除词表（error_rate=0.0 / no error 等不误报）
    - 60s 心跳：长时间无新事件时弹进度更新
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

# 允许直接 `python watcher.py` 运行（不是 -m utils.watcher）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from notifier import toast  # noqa: E402

_STATE_FILENAME = "_batch_state.json"

# 关键字正则：不区分大小写，单词边界，排除已知误匹配词
_LOG_PATTERN = re.compile(
    r"\b(error|traceback|exception|fail(ed)?|export_failed)\b",
    re.IGNORECASE,
)
# 已知误匹配排除词（整段包含以下任意子串时跳过）
_FALSE_POSITIVE_SUBSTRINGS = (
    "error_rate",      # error_rate=0.0
    "error handling",  # Error handling complete
    "no error",        # 无错误的正常描述
    "error handling",
    "handle error",
)
# 日志告警 10s 去重窗口（同一 log 文件 + 同一关键字不重复弹）
_LOG_DEDUP_WINDOW = 10.0

# 状态事件限速聚合窗口（秒）
_STATE_AGG_WINDOW = 2.0

# 心跳间隔（秒）
_DEFAULT_HEARTBEAT = 60.0

# 主循环连续异常上限：超过则告警退出（防死循环刷异常）
_MAX_CONSECUTIVE_ERRORS = 5


def _pid_alive(pid: int) -> bool:
    """判断进程是否存在且仍在运行（ctypes 实现，零依赖）。"""
    if not pid or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import ctypes
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
                return True  # 查不到退出码，按存活处理避免误报
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return True  # 判断不了就当活着，避免误报死亡


def _read_state(workdir: str) -> dict:
    """读 _batch_state.json，不存在返回空 dict，损坏也返回空。"""
    p = Path(workdir) / _STATE_FILENAME
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def _collect_logs(workdir: str) -> list[Path]:
    """收集 workdir 下所有 .log 文件。"""
    return sorted(Path(workdir).glob("*.log"))


def _poll_state(workdir: str, prev_state: dict) -> tuple[list[tuple[str, str, str]], dict]:
    """对比前后 _batch_state.json，返回新事件列表 + 当前状态。

    返回 [(video_path, new_status, ep_str), ...]
    只关注 status 变化: running → done / failed / skipped
    """
    cur = _read_state(workdir)
    events = []

    for video, info in cur.items():
        cur_status = info.get("status", "")
        prev_info = prev_state.get(video, {})
        prev_status = prev_info.get("status", "")

        # 只在从 running 进入终态时触发（避免初始快照时一堆 done 集的假事件）
        if prev_status == "running" and cur_status in ("done", "failed", "skipped"):
            ep = info.get("ep", "?")
            events.append((video, cur_status, str(ep)))

    return events, cur


def _poll_logs(workdir: str, log_positions: dict[str, int],
               last_log_fire: dict[str, float]) -> list[tuple[str, str]]:
    """增量读 log 文件，返回匹配关键字且排除误匹配的新行。

    返回 [(log_name, matched_line), ...]
    同一 (log_name, matched_keyword) 在 _LOG_DEDUP_WINDOW 内只弹一次。
    """
    events = []
    now = time.time()
    for log_path in _collect_logs(workdir):
        path_str = str(log_path)
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            continue

        last_line = log_positions.get(path_str, 0)
        for i in range(last_line, len(lines)):
            line = lines[i].rstrip()

            # 误匹配排除
            lower = line.lower()
            if any(fp in lower for fp in _FALSE_POSITIVE_SUBSTRINGS):
                continue

            # 正则匹配（单词边界 + 不区分大小写）
            m = _LOG_PATTERN.search(line)
            if not m:
                continue

            # 去重：同一 log + 同一关键字 + 时间窗口
            kw = m.group(0).lower()
            dedup_key = f"{log_path.name}|{kw}"
            if now - last_log_fire.get(dedup_key, 0) < _LOG_DEDUP_WINDOW:
                continue
            last_log_fire[dedup_key] = now

            # 截短到 120 字符避免通知过长
            short = line if len(line) <= 120 else line[:117] + "..."
            events.append((log_path.name, short))

        log_positions[path_str] = len(lines)

    return events


def _aggregate_state_events(events: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """把一批状态事件聚合为更紧凑的 toast 消息。

    input: [(video, status, ep), ...]  e.g. 30 条
    output: [(toast_title, toast_body), ...]  e.g. 2-3 条（按 status 分组 + 集号范围）

    聚合规则:
      - 同一 status 合并: "👁️ 状态: done" → body="第1-10集 → done (10)"
      - failed 单独一条（更醒目）
    """
    # 按 status 分组收集集号
    by_status: dict[str, list[str]] = defaultdict(list)
    for _, status, ep in events:
        by_status[status].append(ep)

    results = []
    for status, eps in by_status.items():
        icon = "error" if status == "failed" else "info"
        if len(eps) == 1:
            body = f"第{eps[0]}集 → {status}"
        else:
            body = f"第{eps[0]}-{eps[-1]}集 → {status} ({len(eps)})"
        results.append((f"👁️ 状态: {status}", body, icon))

    return results


def _all_terminal(state: dict) -> bool:
    """所有视频都是终态（done/failed/skipped）且至少有一个视频，或者没有任何状态文件。"""
    if not state:
        return False  # 还没开始
    all_status = {info.get("status", "") for info in state.values()}
    terminal = {"done", "failed", "skipped"}
    return all_status.issubset(terminal) and bool(all_status)


def _summary(state: dict) -> str:
    """生成最终汇总文本。"""
    counts: dict[str, int] = {}
    for info in state.values():
        s = info.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1
    parts = []
    for k, v in sorted(counts.items()):
        parts.append(f"{k}={v}")
    return " | ".join(parts)


def _progress(state: dict) -> str:
    """生成进度心跳文本: 已完成 N/M 集, 运行中 R 集"""
    if not state:
        return "等待任务启动..."
    total = len(state)
    done = sum(1 for info in state.values() if info.get("status") == "done")
    failed = sum(1 for info in state.values() if info.get("status") == "failed")
    running = sum(1 for info in state.values() if info.get("status") == "running")
    return f"已完成 {done}/{total} 集, 运行中 {running} 集, 失败 {failed} 集"


def run_watcher(workdir: str, interval: float = 3.0, once: bool = False,
                heartbeat: float = _DEFAULT_HEARTBEAT, parent_pid: int = 0) -> None:
    """主循环。"""
    print(f"🔍 Watcher 启动: {workdir}")
    print(f"   轮询间隔 {interval}s, {'单次模式' if once else '常驻模式'}")
    if heartbeat and not once:
        print(f"   心跳间隔 {heartbeat}s")
    if parent_pid:
        print(f"   监控主进程 PID={parent_pid}（死亡立即告警；终态时其存活则静默退出）")

    # 首次读取快照（作为 prev_state，避免初始 done 集误报）
    prev_state = _read_state(workdir)
    log_positions: dict[str, int] = {}  # 每个 log 已读行数
    last_log_fire: dict[str, float] = {}  # 日志去重: {dedup_key: last_fire_time}

    # 预初始化 log_positions：扫一遍现有 log 到末尾，避免启动时误报历史关键字
    for log_path in _collect_logs(workdir):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                log_positions[str(log_path)] = len(f.readlines())
        except Exception:
            pass

    if not prev_state:
        print("   初始无 _batch_state.json，等待...")

    # 事件限速聚合缓冲器
    agg_buffer: list[tuple[str, str, str]] = []  # (video, status, ep)
    agg_buffer_start: float = 0.0

    # 心跳计时
    last_event_time: float = time.time()

    # 主进程死亡只告警一次
    parent_dead_notified = False

    # 自愈：连续异常计数
    consecutive_errors = 0

    def _flush_agg_buffer():
        """刷新聚合缓冲器，把缓冲的事件合并后弹 toast。"""
        nonlocal agg_buffer, agg_buffer_start
        if not agg_buffer:
            return
        aggregated = _aggregate_state_events(agg_buffer)
        for title, body, icon in aggregated:
            toast(title, body, icon=icon)
            print(f"   📢 {title}: {body}")
        agg_buffer = []
        agg_buffer_start = 0.0

    try:
        while True:
            try:
                now = time.time()
                events_state, prev_state = _poll_state(workdir, prev_state)
                events_log = _poll_logs(workdir, log_positions, last_log_fire)

                # 状态事件：进入聚合缓冲器（2s 窗口）
                if events_state:
                    if not agg_buffer:
                        agg_buffer_start = now
                    agg_buffer.extend(events_state)
                    last_event_time = now

                # 聚合窗口到了就刷新
                if agg_buffer and (now - agg_buffer_start) >= _STATE_AGG_WINDOW:
                    _flush_agg_buffer()

                # 日志事件：逐条直接弹（日志告警本身频率不高）
                for log_name, line in events_log:
                    toast(f"👁️ 日志告警: {log_name}", line, icon="warning")
                    print(f"   📢 日志告警 [{log_name}]: {line}")
                    last_event_time = now

                # 心跳：常驻模式 + 长时间无新事件 + 有进度
                if heartbeat and not once and (now - last_event_time) >= heartbeat and prev_state:
                    if not _all_terminal(prev_state):
                        progress = _progress(prev_state)
                        toast("👁️ 进度（仍在跑）", progress, icon="info")
                        print(f"   💓 心跳: {progress}")
                        last_event_time = now

                # 主进程死亡检测（崩溃兜底核心路径，只告警一次）
                if parent_pid and not parent_dead_notified and not _pid_alive(parent_pid):
                    parent_dead_notified = True
                    toast("👁️ 主进程已退出", f"PID {parent_pid} 消失，当前进度: {_progress(prev_state)}",
                          icon="warning")
                    print(f"   ⚠️ 主进程 PID={parent_pid} 已退出（继续监控至终态）")
                    last_event_time = now

                # 检查是否全部终态
                if _all_terminal(prev_state):
                    _flush_agg_buffer()  # 先刷新剩余缓冲
                    summary = _summary(prev_state)
                    if parent_pid and _pid_alive(parent_pid):
                        # 主进程活着：收尾通知由主进程（🎬）发出，watcher 静默退出避免重复
                        print(f"\n✅ 所有任务结束: {summary}（主进程存活，收尾通知由主进程发出）")
                    else:
                        toast("👁️ 全部终态", summary, icon="info")
                        print(f"\n✅ Watcher 检测到所有任务结束: {summary}")
                    break

                consecutive_errors = 0  # 本轮正常，清零

            except Exception as e:
                consecutive_errors += 1
                print(f"   ⚠️ 轮询异常（第 {consecutive_errors} 次，不退出）: {e}")
                if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    toast("👁️ Watcher 异常退出", f"连续 {consecutive_errors} 轮异常: {e}", icon="error")
                    return

            if once:
                break

            time.sleep(max(0.5, interval))

    except KeyboardInterrupt:
        _flush_agg_buffer()
        print("\n⏹️  Watcher 手动退出")
    except Exception as e:
        # 临终兜底：任何未捕获异常都尽力通知一声
        try:
            toast("👁️ Watcher 崩溃", str(e)[:100], icon="error")
        except Exception:
            pass
        print(f"\n❌ Watcher 未捕获异常退出: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(description="批量任务 Watcher 守护进程")
    parser.add_argument("--dir", required=True, help="batch_runner 的 --workdir 路径")
    parser.add_argument("--interval", type=float, default=3.0, help="轮询间隔秒数（默认 3）")
    parser.add_argument("--once", action="store_true", help="只轮询一次就退出（供 Agent 定时调用）")
    parser.add_argument("--heartbeat", type=float, default=_DEFAULT_HEARTBEAT,
                        help=f"心跳通知间隔秒数（默认 {_DEFAULT_HEARTBEAT}, 0 关闭）")
    parser.add_argument("--pid", type=int, default=0,
                        help="主进程 PID：其死亡立即告警；全部终态时若主进程存活则静默退出（去重）")
    args = parser.parse_args()

    if not os.path.isdir(args.dir):
        print(f"❌ 目录不存在: {args.dir}")
        return 1

    heartbeat_val = args.heartbeat if args.heartbeat > 0 else 0
    run_watcher(args.dir, interval=args.interval, once=args.once,
                heartbeat=heartbeat_val, parent_pid=args.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
