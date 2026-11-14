# -*- coding: utf-8 -*-
"""jy_instance.py — 剪映实例生命周期「统一管理器」（第 2 项）。

现状问题：`connect` / `switch_to_home` / `_restart_jianying_to_home` /
`_cold_start_jianying` 四套拉起/回首页逻辑并存，各自判「就绪」的标准不一致，
正是「窗口存在但未就绪」假死的根源。本模块把它们收敛为**一个实例管理器**，
对外只暴露少数动作：`ensure / launch / wait_ready / restart / kill / go_home`。

显式状态机
    stopped → launching → home → editing → exporting → (home | stopped)
  - 每个转移用**墙钟 deadline**（不是"循环 sleep 计数"的假超时）；
  - 每次 UIA 探测都带**独立线程超时**（沿用 F 项做法，防 UIA 无限阻塞）；
  - `ensure` 不达 deadline 就 `restart()` **一次**，再失败抛 `JyInstanceError`，
    交给第 1 项状态机记 `failure_class`（异常消息含可分类关键词）。

杀进程权限（唯一入口）
    所有关闭/结束剪映统一走 `lib/jianying_proc.py`（stop_jianying）：
    `kill()` = 强制语义（taskkill /F），`shutdown()` = 正常关闭（WM_CLOSE 优先）。
    robust_auto_export / batch_runner / ui_nav 都不得再各自写裸 taskkill。

可测性：backend / time_fn / sleep_fn / poll_interval 均可注入，单测用假后端 + 假时钟，
无需真实剪映或 UIA。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Optional

# 启动共用层：exe 定位/版本校验/拉起/等待就绪/早死重拉统一走 lib/jianying_launch.py
_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(_HERE_DIR))))
_LIB_DIR = os.path.join(_REPO_ROOT, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import jianying_launch as jl  # noqa: E402
import jianying_proc as jp  # noqa: E402

STATES = ("stopped", "launching", "home", "editing", "exporting")
PROCESS_NAME = "jianyingpro.exe"

# ── 启动即死检测（口径统一在 jianying_launch，模块内保留同名别名供调用方引用）──
EARLY_DEATH_WINDOW_S = jl.EARLY_DEATH_WINDOW_S
EARLY_DEATH_RETRIES = jl.EARLY_DEATH_RETRIES
EARLY_DEATH_SETTLE_S = jl.EARLY_DEATH_SETTLE_S
# restart() 的 kill 与新进程之间的静默间隔（口径见 jianying_launch：B4 观测后归零）
RESTART_SETTLE_S = jl.RESTART_SETTLE_S


class JyInstanceError(Exception):
    """实例生命周期错误。消息含可被第 3 项分类器识别的关键词（未就绪/冷启动失败等）。"""


# 兼容旧引用：探测/取证实现已移到共用层（lib/jianying_launch.py）
_poll_with_timeout = jl.poll_with_timeout
_jianying_log_snapshot = jl.jianying_log_snapshot


# ── 就绪墙钟（第 1 条稳定性修复）─────────────────────────────────
# 大草稿 / 冷盘启动时，剪映进程与窗口常在，但 HomePage 还没渲染出来。
# 旧默认（launch 60s / restart 90s）会把"窗口存在但页面未就绪"直接判成失败，
# 于是首轮必挂、靠下一轮重试才成功。这里统一放宽到 180s；wait_ready 本身
# 会在 deadline 前持续轮询，因此"存在但未就绪"= 继续等待，而不是提前失败。
HOME_READY_TIMEOUT_S = jl.HOME_READY_TIMEOUT_S
SOFT_HOME_TIMEOUT_S = 60.0    # 软关编辑页（点关闭按钮）不重启的墙钟上限


# ── 后端：把所有真实系统调用收口到这里（可在测试中整体替换）──────────

class Backend:
    """后端接口。真实实现见 UIABackend；测试注入假后端。"""

    def process_running(self) -> bool:  # pragma: no cover - 接口
        raise NotImplementedError

    def window_exists(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def is_home(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def is_edit(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def launch(self, exe: str) -> Any:  # pragma: no cover
        """返回 pid 或 (pid, 真实启动方式)。"""
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def shutdown(self) -> bool:  # pragma: no cover
        """正常关闭（WM_CLOSE 优先）。返回是否确认退出。"""
        raise NotImplementedError

    def close_edit_to_home(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    def rebind_window(self) -> Any:  # pragma: no cover
        raise NotImplementedError


class UIABackend(Backend):
    """真实 UIA + psutil 后端。uiautomation 懒导入，保证本模块可被无 UIA 环境导入。"""

    def __init__(self):
        self._uia = None
        self.window = None
        # 启动日志出口：JyInstance 会把它绑到管理器同一输出口（导出链的 dprint）
        self.log: Callable[[str], None] = print

    def _u(self):
        if self._uia is None:
            import uiautomation as uia  # noqa: 懒导入
            self._uia = uia
        return self._uia

    def process_running(self) -> bool:
        try:
            import psutil
            for p in psutil.process_iter(["name"]):
                if (p.info.get("name") or "").lower() == PROCESS_NAME:
                    return True
        except Exception:
            pass
        return False

    def window_exists(self) -> bool:
        try:
            uia = self._u()
            if uia.WindowControl(searchDepth=1, Name="剪映专业版").Exists(0):
                return True
            return uia.WindowControl(searchDepth=1, Name="导出").Exists(0)
        except Exception:
            return False

    def _main_window(self):
        """剪映主窗口；找不到返回 None。

        教训：回退用的搜索条件不是窗口。Name='导出' 只是一个查询描述，
        未做 Exists 校验就把它当主窗口交出去，后续读 ClassName 会白等 10s
        搜索超时（看起来像剪映假死）。任何 rebind 路径都不能跳过存在性校验。
        """
        uia = self._u()
        w = uia.WindowControl(searchDepth=1, Name="剪映专业版")
        if w.Exists(0):
            return w
        export_win = uia.WindowControl(searchDepth=1, Name="导出")
        if export_win.Exists(0):
            return export_win
        return None

    def is_home(self) -> bool:
        try:
            w = self._main_window()
            return bool(w) and "HomePage" in (w.ClassName or "")
        except Exception:
            return False

    def is_edit(self) -> bool:
        try:
            w = self._main_window()
            return bool(w) and "MainWindow" in (w.ClassName or "")
        except Exception:
            return False

    def launch(self, exe: str) -> Any:
        # 返回 (pid, 真实启动方式)：共用层据此判活并写入台账；
        # 传 log_fn 让「启动: 路径」/「启动方式: createprocess|wmi」两行落到导出链输出。
        return jl.spawn_jianying(exe, log_fn=self.log)

    def kill(self) -> None:
        # 杀进程统一走 lib/jianying_proc.py（唯一入口），本模块不再直接 taskkill
        jp.stop_jianying(force=True, log_fn=self.log)

    def shutdown(self) -> bool:
        """正常关闭：WM_CLOSE 优先，超时才结束进程树。返回是否确认退出。"""
        return jp.stop_jianying(grace_s=20.0, force=False, log_fn=self.log)

    def close_edit_to_home(self) -> bool:
        """编辑页右上角关闭按钮（TitleBarButton foundIndex=3）→ 回首页（不杀进程）。"""
        try:
            w = self._main_window()
            btn = w.GroupControl(searchDepth=1, ClassName="TitleBarButton", foundIndex=3)
            if btn.Exists(1):
                btn.Click(simulateMove=False)
                return True
        except Exception:
            pass
        return False

    def rebind_window(self) -> Any:
        """重新绑定主窗口；拿不到主窗口时**不改动** self.window，返回 None。

        调用方据此报错或保留旧绑定，绝不把一个不存在的搜索条件留在绑定位上。
        """
        w = self._main_window()
        if w is None:
            return None
        self.window = w
        try:
            w.SetActive()
            w.SetTopmost(True)
            time.sleep(0.2)
            w.SetTopmost(False)
        except Exception:
            pass
        return w


# ── 管理器 ───────────────────────────────────────────────────────

class JyInstance:
    """剪映实例状态机管理器。`kill()` 是唯一杀进程入口。"""

    def __init__(self, backend: Optional[Backend] = None,
                 find_exe: Optional[Callable[[], Optional[str]]] = None,
                 time_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 poll_interval: float = 1.0,
                 probe_timeout: float = 8.0,
                 log: Callable[[str], None] = print):
        self.backend = backend or UIABackend()
        self._find_exe = find_exe
        self._time = time_fn
        self._sleep = sleep_fn
        self.poll_interval = poll_interval
        self.probe_timeout = probe_timeout
        self._log = log
        # 后端启动日志（「启动/启动方式」两行）绑定到管理器同一输出口，
        # 保证导出链真实打印，而不是被默认 print 吞掉或写到别处。
        if isinstance(self.backend, UIABackend):
            self.backend.log = self._log
        self.state = "stopped"
        # 启动即死重试预算：每个管理器（= 每次导出运行）只有 EARLY_DEATH_RETRIES 次，
        # 不会随导出层 attempt 重试倍增。
        self._budget = jl.LaunchBudget(EARLY_DEATH_RETRIES)

    # ── 探测 ──
    def _probe(self, state: str) -> bool:
        b = self.backend
        if state == "stopped":
            ok, v = _poll_with_timeout(lambda: not b.process_running(), self.probe_timeout)
            return ok and bool(v)
        if state == "launching":
            ok, v = _poll_with_timeout(b.process_running, self.probe_timeout)
            return ok and bool(v)
        if state == "home":
            ok, v = _poll_with_timeout(
                lambda: b.window_exists() and b.is_home(), self.probe_timeout)
            return ok and bool(v)
        if state == "editing":
            ok, v = _poll_with_timeout(
                lambda: b.window_exists() and b.is_edit(), self.probe_timeout)
            return ok and bool(v)
        if state == "exporting":
            ok, v = _poll_with_timeout(b.window_exists, self.probe_timeout)
            return ok and bool(v)
        return False

    def detect(self) -> str:
        """探测当前真实状态并同步 self.state。"""
        if not self._probe("launching"):  # 进程不在
            self.state = "stopped"
            return self.state
        if self._probe("editing"):
            self.state = "editing"
        elif self._probe("home"):
            self.state = "home"
        else:
            self.state = "launching"  # 进程在但窗口/页面未就绪
        return self.state

    # ── 等待 ──
    def wait_ready(self, state: str, deadline: float) -> bool:
        """轮询直到达到 state 或超过 deadline（墙钟）。到不了返回 False，绝不无限等。"""
        if state not in STATES:
            raise JyInstanceError(f"未知实例状态: {state}")
        while True:
            if self._probe(state):
                self.state = state
                return True
            if self._time() >= deadline:
                return False
            self._sleep(self.poll_interval)

    # ── 动作 ──
    def kill(self) -> bool:
        """强制结束（经 backend.kill → lib/jianying_proc.py）。返回旧进程是否确已消失。"""
        self.backend.kill()
        deadline = self._time() + 15
        while self._time() < deadline and self.backend.process_running():
            self._sleep(1)
        self.state = "stopped"
        return not self.backend.process_running()

    def shutdown(self) -> bool:
        """正常关闭剪映（WM_CLOSE 优先，超时才结束进程树）。返回是否确认退出。

        与 kill() 的区别：给剪映自己删锁、落盘 autosave 的机会；导出收尾默认走这条，
        避免把实例留成空转进程。后端未实现 shutdown 时退回 kill()。
        """
        self._log("实例管理器：正常关闭剪映（WM_CLOSE → 超时兜底）")
        try:
            gone = bool(self.backend.shutdown())
        except NotImplementedError:
            return self.kill()
        if not self.backend.process_running():
            self.state = "stopped"
        return gone and not self.backend.process_running()

    def launch(self, timeout: float = HOME_READY_TIMEOUT_S) -> bool:
        """stopped → launching → home。返回是否在 timeout 内到达 home。

        实际启动动作统一交给 lib/jianying_launch.ensure_jianying：
          - 版本强校验 / cwd=exe 目录 / breakaway + WMI 兜底；
          - 等待期间每轮判进程存活，进程消失立即判失败；
          - 启动即死按管理器级预算（EARLY_DEATH_RETRIES=1）重拉一次，不随 attempt 放大。
        timeout 默认 180s：窗口出现但 HomePage 未渲染完仍算"未就绪"，继续轮询。
        """
        # 防双开（唯一收口）：进程已经在就只重连/等待，绝不再 spawn 第二个实例。
        # 与 robust_auto_export 的“进程退出即判拉起失败 → watcher 跨轮重拉”配合，
        # 两条路径都先过这里，保证同一时刻只有一个剪映进程。
        if self.backend.process_running():
            self._log("实例管理器：已有剪映进程存活 → 只重连，不重复拉起")
            self.state = "launching"
            deadline = self._time() + timeout
            while self._time() < deadline:
                if self._probe("home"):
                    self.state = "home"
                    self.last_outcome = None
                    return True
                if not self.backend.process_running():
                    self._log("实例管理器：等待期间进程退出（未 spawn，交由上层重拉）")
                    self.state = "stopped"
                    self.last_outcome = None
                    return False
                self._sleep(self.poll_interval)
            self._log("实例管理器：存活进程未到达 home（不 spawn，交由上层处理）")
            self.last_outcome = None
            return False

        exe = self._find_exe() if self._find_exe else None
        if not exe:
            raise JyInstanceError("无法定位剪映安装路径，无法启动（冷启动失败）")
        self.state = "launching"
        outcome = jl.ensure_jianying(
            exe_path=exe, timeout=timeout, readiness="home",
            require_exists=False, budget=self._budget,
            entry="export",
            ready_fn=lambda: self._probe("home"),
            alive_fn=self.backend.process_running,
            spawn_fn=self.backend.launch,
            time_fn=self._time, sleep_fn=self._sleep,
            poll_interval=self.poll_interval, probe_timeout=self.probe_timeout,
            log_fn=self._log)
        # 供上层（robust_auto_export）区分"拉起本身失败"与普通 UI 假死：
        # died/timeout 才进跨轮重拉，其他失败仍走原退避。
        self.last_outcome = outcome
        if outcome.ok:
            self.state = "home"
            return True
        if outcome.outcome in ("died", "died_late", "died_after_ready"):
            self.state = "stopped"
        return False

    def restart(self, timeout: float = HOME_READY_TIMEOUT_S) -> bool:
        """kill → 静默 RESTART_SETTLE_S → launch → 等 home。返回是否到达 home。

        原实现假设"旧进程可能还占着单实例互斥体"，留 3s 静默期；B4 对照观测
        （2026-11-14，6 轮）未复现该机理：进程确认消失后立即拉起 6/6 成功，
        含残留检测器仍在时的立即重拉 3/3。故 RESTART_SETTLE_S 已归零，仍保留
        这一步是为了让调用方能显式覆盖间隔（而不是写死 sleep）。
        """
        self._log("实例管理器：重启剪映（唯一 kill 入口）")
        gone = self.kill()
        if not gone:
            self._log("实例管理器：kill 后旧进程仍在运行（15s 未退出），静默期后仍尝试拉起")
        self._sleep(RESTART_SETTLE_S)
        return self.launch(timeout)

    def _reach(self, state: str, deadline: float) -> bool:
        cur = self.detect()
        if cur == state:
            return True
        if state == "stopped":
            self.kill()
            return True
        if cur == "stopped":
            # 进程未起：先拉起再等目标状态（首次尝试就要真正启动，而不是干等）
            try:
                self.launch(max(1.0, deadline - self._time()))
            except JyInstanceError:
                return False
            if self._probe(state):
                self.state = state
                return True
            return self.wait_ready(state, deadline)
        if state == "home" and cur == "editing":
            # 软关编辑页（不杀进程），再等首页
            self.backend.close_edit_to_home()
        return self.wait_ready(state, deadline)

    def ensure(self, state: str, timeout: float = HOME_READY_TIMEOUT_S) -> bool:
        """把实例带到 state。不达 deadline 就 restart 一次；再失败抛 JyInstanceError。

        绝不无限等待；异常消息含「未就绪/冷启动失败」关键词，供第 3 项分类为 ui_hang。
        """
        if self._reach(state, self._time() + timeout):
            return True
        self._log(f"实例管理器：首次未达到 {state}，重启一次")
        restarted = False
        try:
            restarted = self.restart(timeout)
        except JyInstanceError:
            raise
        if restarted and self._reach(state, self._time() + timeout):
            return True
        raise JyInstanceError(
            f"剪映两次尝试均未达到状态 {state}（窗口未就绪/冷启动失败），已重启一次仍失败")

    def go_home(self, timeout: float = SOFT_HOME_TIMEOUT_S, allow_restart: bool = True,
                restart_timeout: float = HOME_READY_TIMEOUT_S) -> bool:
        """回首页：软关编辑页优先，失败按 allow_restart 决定是否重启兜底。

        restart_timeout 与软关墙钟分开（默认 180s）：旧实现把软关的 60s 直接
        透传给 restart，导致冷启动根本等不到 HomePage，表现为"重启后仍未回到首页"。
        """
        if self._reach("home", self._time() + timeout):
            self.backend.rebind_window()
            return True
        if not allow_restart:
            return False
        ok = self.restart(restart_timeout)
        if ok:
            self.backend.rebind_window()
        return ok
