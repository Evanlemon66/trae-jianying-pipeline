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
    **只有本管理器的 `kill()`（经 backend.kill）允许调用 taskkill**。
    robust_auto_export 的 `_restart_jianying_to_home` / `_cold_start_jianying`
    已改为薄封装转调本管理器，不再各自直接 taskkill。其它模块一律不得直接杀进程。

可测性：backend / time_fn / sleep_fn / poll_interval 均可注入，单测用假后端 + 假时钟，
无需真实剪映或 UIA。
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Callable, Optional

STATES = ("stopped", "launching", "home", "editing", "exporting")
PROCESS_NAME = "jianyingpro.exe"


class JyInstanceError(Exception):
    """实例生命周期错误。消息含可被第 3 项分类器识别的关键词（未就绪/冷启动失败等）。"""


def _poll_with_timeout(fn: Callable[[], Any], timeout: float) -> tuple[bool, Any]:
    """在 daemon 线程里执行一次探测，超时判失败。

    剪映假死时 UIA 调用会无限阻塞，所以每次探测必须有独立墙钟上限；
    daemon 线程不阻塞进程退出。
    """
    result: dict[str, Any] = {}

    def _runner():
        try:
            result["value"] = fn()
        except Exception:
            pass

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive() or "value" not in result:
        return False, None
    return True, result["value"]


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

    def launch(self, exe: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover
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
        uia = self._u()
        w = uia.WindowControl(searchDepth=1, Name="剪映专业版")
        if not w.Exists(0):
            w = uia.WindowControl(searchDepth=1, Name="导出")
        return w

    def is_home(self) -> bool:
        try:
            return "HomePage" in (self._main_window().ClassName or "")
        except Exception:
            return False

    def is_edit(self) -> bool:
        try:
            return "MainWindow" in (self._main_window().ClassName or "")
        except Exception:
            return False

    def launch(self, exe: str) -> None:
        subprocess.Popen([exe])

    def kill(self) -> None:
        # 唯一允许 taskkill 的地方（经管理器 kill() 调用）
        subprocess.run(["taskkill", "/F", "/IM", "JianyingPro.exe"],
                       capture_output=True)

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
        try:
            self.window = self._main_window()
            try:
                self.window.SetActive()
                self.window.SetTopmost(True)
                time.sleep(0.2)
                self.window.SetTopmost(False)
            except Exception:
                pass
            return self.window
        except Exception:
            return None


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
        self.state = "stopped"

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
    def kill(self) -> None:
        """唯一杀进程入口。"""
        self.backend.kill()
        deadline = self._time() + 15
        while self._time() < deadline and self.backend.process_running():
            self._sleep(1)
        self.state = "stopped"

    def launch(self, timeout: float = 60) -> bool:
        """stopped → launching → home。返回是否在 timeout 内到达 home。"""
        exe = self._find_exe() if self._find_exe else None
        if not exe:
            raise JyInstanceError("无法定位剪映安装路径，无法启动（冷启动失败）")
        self.backend.launch(exe)
        self.state = "launching"
        return self.wait_ready("home", self._time() + timeout)

    def restart(self, timeout: float = 90) -> bool:
        """kill → launch → 等 home。返回是否到达 home。"""
        self._log("实例管理器：重启剪映（唯一 kill 入口）")
        self.kill()
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

    def ensure(self, state: str, timeout: float = 90) -> bool:
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

    def go_home(self, timeout: float = 60, allow_restart: bool = True) -> bool:
        """回首页：软关编辑页优先，失败按 allow_restart 决定是否重启兜底。"""
        if self._reach("home", self._time() + timeout):
            self.backend.rebind_window()
            return True
        if not allow_restart:
            return False
        ok = self.restart(timeout)
        if ok:
            self.backend.rebind_window()
        return ok
