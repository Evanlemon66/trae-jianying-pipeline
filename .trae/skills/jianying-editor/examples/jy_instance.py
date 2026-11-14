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


def _require_repo_layout():
    """上探仓库依赖层；找不到时给出布局说明，而不是裸 ModuleNotFoundError。"""
    missing = [rel for rel in ("lib/jianying_launch.py", "lib/jianying_proc.py")
               if not os.path.isfile(os.path.join(_REPO_ROOT, *rel.split("/")))]
    if missing:
        raise ImportError(
            "未找到仓库依赖层，jy_instance.py 不能脱离仓库单独使用。\n"
            "支持的唯一布局：<repo>/.trae/skills/jianying-editor/examples/jy_instance.py\n"
            f"本次按上探推算的仓库根：{_REPO_ROOT}\n"
            f"缺失：{', '.join(missing)}\n"
            "请把整个仓库（含 lib/ 与 pipeline/）一起 clone/解压后再运行。"
        )


_require_repo_layout()
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
import jianying_launch as jl  # noqa: E402
import jianying_proc as jp  # noqa: E402
import jy_page_detect as jpd  # noqa: E402  （窗口枚举/判页/可交互的唯一实现）

STATES = ("stopped", "launching", "home", "editing", "exporting")
PROCESS_NAME = "jianyingpro.exe"

# 分段耗时观测开关（只影响日志输出，不影响判据/超时/重试）
_PROBE_TRACE = os.environ.get("JY_PROBE_TRACE") == "1"


def _pick_main_window(children, pids):
    """从顶层窗口里挑剪映主窗口：进程名 + 类名（统一走 jy_page_detect 的可配置集合）。

    界面语言/版本改名时中文 Name 匹配会落空；类名匹配集涵盖
    HomePage_QMLTYPE_* / MainWindow_QMLTYPE_* / Qt*QWindowIcon，可作为稳定兜底。
    """
    for c in children:
        try:
            if pids and getattr(c, "ProcessId", None) not in pids:
                continue
            if jpd.window_class_ok(getattr(c, "ClassName", "") or ""):
                return c
        except Exception:
            continue
    return None


def _jianying_pids():
    try:
        import psutil
        return {p.pid for p in psutil.process_iter(["name"])
                if (p.info.get("name") or "").lower() == PROCESS_NAME}
    except Exception:
        return set()


def _window_by_process(uia):
    """按进程名定位主窗口；进程不在或 UIA 读取失败都返回 None。"""
    pids = _jianying_pids()
    if not pids:
        return None
    try:
        return _pick_main_window(uia.GetRootControl().GetChildren(), pids)
    except Exception:
        return None


def _is_draft_card(control, _depth=0):
    """首页草稿卡片（口径统一在 jy_page_detect，保留本名供既有引用）。"""
    return jpd._is_draft_card(control)

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

    def prime(self) -> None:  # pragma: no cover - 接口
        """在当前（主）线程初始化 UIA 客户端；默认无操作。"""
        return None

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
        # 分段计时观测：首次"判出页面"/"可交互通过"的 epoch 秒（只记录，不影响判据）
        self.timing: dict[str, float] = {}
        # 最近一次激活（rebind_window 内 activate_window 的原始返回，排障用）
        self.last_activation: dict = {}

    def _stamp(self, key: str, cond: bool) -> None:
        """计时观测（不改判据）：条件首次为真时记时刻，后续不覆盖。"""
        if cond and key not in self.timing:
            self.timing[key] = time.time()

    def _note_window(self, hwnd: int = 0) -> None:
        """计时观测：首次绑定到主窗口的 epoch 时刻（只记录，不参与任何判定）。"""
        if 'window_first' not in self.timing:
            self.timing['window_first'] = time.time()
            self.timing['window_hwnd'] = int(hwnd or 0)

    def _trace(self, stage: str, ts: float, ret: Any = '-', err: str = '-') -> None:
        """分段耗时观测（仅 JY_PROBE_TRACE=1 时输出；不改判据/超时/重试）。"""
        if not _PROBE_TRACE:
            return
        try:
            self.log(f"[probe-trace] {stage} start={ts:.3f} "
                     f"dur={time.time() - ts:.3f}s ret={ret} err={err}")
        except Exception:
            pass

    def _u(self):
        if self._uia is None:
            import uiautomation as uia  # noqa: 懒导入
            self._uia = uia
        return self._uia

    def prime(self) -> None:
        """在主线程创建 _AutomationClient 单例（探测线程随后可直接复用）。

        首次创建若发生在 poll_with_timeout 的探测线程，该线程未 CoInitialize，
        会报 "Can not load UIAutomationCore.dll"，就绪探测永远 False（表现为启动
        超时，实际实例已可用）。
        """
        try:
            self._u().GetRootControl().ClassName
        except Exception:
            pass

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
        """窗口是否存在：纯 Win32 枚举（毫秒级，与判页层同源）。

        原实现走 `_main_window()`：Win32 枚举为空时会退到 UIA 按名搜索，实测每次
        固定 ~20s。冷启动「还没建窗」阶段每轮探针都被拖 20s，顺带把启动层的检测器
        停滞判死饿死（探不到 5s 采样间隔）。窗口存在性本来就不需要 UIA。
        """
        try:
            return bool(jpd.any_jianying_window())
        except Exception:
            return False

    def _main_window(self):
        """剪映主窗口；找不到返回 None。

        首选 Win32 枚举（毫秒级）+ `ControlFromHandle` 绑定，不再对每个顶层窗口
        做 UIA 名字搜索：`WindowControl(Name=…).Exists()` 实测每次固定 ~20.1s，
        超过探针超时后会让"窗口明明在"被判成超时。
        Win32 拿不到时才退回按名搜索（兜底不得跳过存在性校验）。
        """
        _t = time.time()
        try:
            uia = self._u()
            # 1) Win32 枚举：标题含「剪映」+ 类名收口（qwindowicon 等），拿到 hwnd 直接绑定
            try:
                for hwnd, cls, title in jpd.enum_jianying_windows():
                    try:
                        ctrl = uia.ControlFromHandle(hwnd)
                    except Exception:
                        continue
                    if ctrl is not None:
                        self._note_window(hwnd)
                        self._trace('find-window', _t, ret=f'win32:{hwnd} {cls!r}')
                        return ctrl
            except Exception:
                pass
            # 2) 兜底：UIA 按窗口名搜索（仅 Win32 拿不到时走；调用方保证
            #    已存在剪映窗口，冷启动「还没建窗」阶段不会走到这里）
            w = uia.WindowControl(searchDepth=1, Name="剪映专业版")
            if w.Exists(0):
                self._note_window(getattr(w, 'NativeWindowHandle', 0))
                self._trace('find-window', _t, ret="fallback:title='剪映专业版'")
                return w
            export_win = uia.WindowControl(searchDepth=1, Name="导出")
            if export_win.Exists(0):
                self._note_window(getattr(export_win, 'NativeWindowHandle', 0))
                self._trace('find-window', _t, ret="fallback:title='导出'")
                return export_win
            found = _window_by_process(uia)
            if found is not None:
                self._note_window(getattr(found, 'NativeWindowHandle', 0))
            self._trace('find-window', _t, ret=f'fallback:by-process:{bool(found)}')
            return found
        except Exception as e:
            self._trace('find-window', _t, err=f'{type(e).__name__}: {e}')
            raise


    def _home_ready(self, w) -> bool:
        """首页「真能用」：草稿卡片/启用按钮/任意文本三级降级（口径见 jy_page_detect）。"""
        return jpd.page_interactive(w, 'home')

    def is_home(self) -> bool:
        """首页就绪：判页口径统一走 jy_page_detect（Description 特征优先）。"""
        _t0 = time.time()
        try:
            # 冷启动还没建窗：直接判否。否则 _main_window 的 UIA 按名兜底每次
            # 固定 ~20s（实测），会把探针周期拖成 20s 并饿死检测器停滞判死。
            if not jpd.any_jianying_window():
                return False
            w = self._main_window()
            if not w:
                return False
            _t = time.time()
            page = jpd.detect_page(w)
            self._trace('detect_page(home)', _t, ret=page)
            self._stamp('page_home', page == 'home')
            if page != 'home':
                return False
            _t = time.time()
            ok = jpd.page_interactive(w, 'home')
            self._trace('page_interactive(home)', _t, ret=ok)
            self._stamp('interactive_home', ok)
            return ok
        except Exception as e:
            self._trace('is_home', _t0, err=f'{type(e).__name__}: {e}')
            return False

    def is_edit(self) -> bool:
        """编辑页就绪：不再只认类名，必须有时间线根 + 可枚举的启用控件。"""
        _t0 = time.time()
        try:
            # 同 is_home：没建窗就不进 _main_window，避免 20s 的 UIA 按名兜底
            if not jpd.any_jianying_window():
                return False
            w = self._main_window()
            if not w:
                return False
            _t = time.time()
            page = jpd.detect_page(w)
            self._trace('detect_page(edit)', _t, ret=page)
            self._stamp('page_edit', page == 'edit')
            if page != 'edit':
                return False
            _t = time.time()
            ok = jpd.page_interactive(w, 'edit')
            self._trace('page_interactive(edit)', _t, ret=ok)
            self._stamp('interactive_edit', ok)
            return ok
        except Exception as e:
            self._trace('is_edit', _t0, err=f'{type(e).__name__}: {e}')
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
        # 激活是独立动作：记录真实结果（含前台锁），失败只告警，不改变就绪判定
        self.last_activation = jpd.activate_window(w, log=self.log)
        return w


# ── 管理器 ───────────────────────────────────────────────────────

class JyInstance:
    """剪映实例状态机管理器。`kill()` 是唯一杀进程入口。"""

    def __init__(self, backend: Optional[Backend] = None,
                 find_exe: Optional[Callable[[], Optional[str]]] = None,
                 time_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 poll_interval: float = 1.0,
                 probe_timeout: float = 30.0,
                 cleanup_fn: Optional[Callable[[Optional[Callable[[str], None]]], list]] = None,
                 detect_detector_stall: bool = True,
                 log: Callable[[str], None] = print):
        self.backend = backend or UIABackend()
        self._find_exe = find_exe
        self._time = time_fn
        self._sleep = sleep_fn
        # 重拉前清残留（默认委托启动共用层）；测试注入 no-op，避免碰真实进程。
        self._cleanup = cleanup_fn
        self.poll_interval = poll_interval
        self.probe_timeout = probe_timeout
        # 检测器日志停滞判死：真实启动默认开（失败 A1 可省 ~20s 白等）；
        # 单测（假时钟）必须关，否则会 stat 到真实日志目录。
        self.detect_detector_stall = detect_detector_stall
        self._log = log
        # 后端启动日志（「启动/启动方式」两行）绑定到管理器同一输出口，
        # 保证导出链真实打印，而不是被默认 print 吞掉或写到别处。
        if isinstance(self.backend, UIABackend):
            self.backend.log = self._log
        self.state = "stopped"
        # 启动即死重试预算：每个管理器（= 每次导出运行）只有 EARLY_DEATH_RETRIES 次，
        # 不会随导出层 attempt 重试倍增。
        self._budget = jl.LaunchBudget(EARLY_DEATH_RETRIES)
        # 最近一次 launch 的结果（died/timeout 等），供统一判定打 launch_failed 标记
        self.last_outcome = None
        # 就绪判定那一次探到的页面名（home/editing）；启动成功后直接复用它，
        # 不再额外重复判页（原先会多花 8s 做一次同样的确认）。
        self._last_ready_page = None
        # 台账分类：导出链 export / 准入链 admit；由 ensure_for_export 显式设置
        self.entry = "export"

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

    def _probe_ready_page(self):
        """启动就绪判据：主窗口在且处于首页或编辑页（= 可交互）。返回页面名或 None。

        5.9 冷启动会自动恢复上次草稿、直接进编辑页；只等 home 会白等到 180s 超时
        并误报 launch_failed。编辑页同样算"启动成功"，由 ensure_for_export 软关回首页。
        """
        _tr = getattr(self.backend, "_trace", None)
        if _tr is not None:  # 只做观测：UIA 连接/取根节点耗时（探测线程内）
            _t = time.time()
            try:
                _u = getattr(self.backend, "_u", None)
                _root = _u().GetRootControl() if _u is not None else None
                _tr("uia-root", _t, ret=f"ok:{bool(_root)}")
            except Exception as e:
                _tr("uia-root", _t, err=f"{type(e).__name__}: {e}")
        if self._probe("home"):
            self._last_ready_page = "home"
            return "home"
        if self._probe("editing"):
            self._last_ready_page = "editing"
            return "editing"
        return None

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
        """stopped → launching → 可交互页面。返回是否在 timeout 内到达首页**或编辑页**。

        就绪口径（2026-11-14 修正）：进程可交互 + 主窗口在 + 页面属于 {home, editing}。
        5.9 冷启动会自动恢复上次草稿直接进编辑页，只等 home 会白等 180s 并误报
        launch_failed；编辑页同样算启动成功，后续由 ensure_for_export 软关回首页。

        实际启动动作统一交给 lib/jianying_launch.ensure_jianying：
          - 版本强校验 / cwd=exe 目录 / breakaway + WMI 兜底；
          - 等待期间每轮判进程存活，进程消失立即判失败；
          - 启动即死按管理器级预算（EARLY_DEATH_RETRIES=1）重拉一次，不随 attempt 放大。
        timeout 默认 180s：窗口出现但 HomePage 未渲染完仍算"未就绪"，继续轮询。
        """
        # UIA 单例必须在调用线程先建好，否则下面的探测线程会因未 CoInitialize 全部失败。
        self.backend.prime()
        # 防双开（唯一收口）：进程已经在就只重连/等待，绝不再 spawn 第二个实例。
        # 与 robust_auto_export 的“进程退出即判拉起失败 → watcher 跨轮重拉”配合，
        # 两条路径都先过这里，保证同一时刻只有一个剪映进程。
        if self.backend.process_running():
            self._log("实例管理器：已有剪映进程存活 → 只重连，不重复拉起")
            self.state = "launching"
            deadline = self._time() + timeout
            while self._time() < deadline:
                st = self._probe_ready_page()
                if st:
                    self.state = st
                    self._log(f"实例管理器：已有进程可交互（页面={st}）→ 启动就绪")
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
        self._last_ready_page = None
        outcome = jl.ensure_jianying(
            exe_path=exe, timeout=timeout, readiness="home",
            require_exists=False, budget=self._budget,
            entry=self.entry,
            ready_fn=lambda: bool(self._probe_ready_page()),
            alive_fn=self.backend.process_running,
            spawn_fn=self.backend.launch,
            cleanup_fn=self._cleanup,
            time_fn=self._time, sleep_fn=self._sleep,
            poll_interval=self.poll_interval, probe_timeout=self.probe_timeout,
            detect_detector_stall=self.detect_detector_stall,
            log_fn=self._log)
        # 供上层（robust_auto_export）区分"拉起本身失败"与普通 UI 假死：
        # died/timeout 才进跨轮重拉，其他失败仍走原退避。
        self.last_outcome = outcome
        if outcome.ok:
            # 判页结论直接取就绪判定那一次的结果（同一次判定，不重复探测）
            st = self._last_ready_page or "home"
            self.state = st
            self._log(f"实例管理器：冷启动就绪，页面={st}"
                      f"{'（5.9 自动恢复草稿，交由 ensure_for_export 软关回首页）' if st == 'editing' else ''}")
            # 就绪后置前：独立动作，只做一次，失败只告警、不参与成败判定
            self._activate_after_ready()
            return True
        if outcome.outcome in ("died", "died_late", "died_after_ready"):
            self.state = "stopped"
        return False

    def _activate_after_ready(self) -> None:
        """就绪后把主窗口置前一次（复用 backend.rebind_window → activate_window）。

        独立动作：不含重试、不进轮询循环、不改 self.state / last_outcome；
        异常与"前台锁"只记录告警。
        """
        fn = getattr(self.backend, "rebind_window", None)
        if fn is None:
            return
        _stamps = getattr(self.backend, 'timing', None)
        if isinstance(_stamps, dict):
            _stamps['activate_start'] = time.time()   # 观测：激活开始 epoch
        try:
            fn()
        except Exception as e:
            self._log(f"[activate] 就绪后置前失败（仅告警）：{type(e).__name__}: {e}")
            return
        finally:
            if isinstance(_stamps, dict):
                _stamps['activate_done'] = time.time()
        rec = getattr(self.backend, "last_activation", None) or {}
        if rec:
            before = (rec.get("before") or {}).get("fg_title")
            after = (rec.get("after") or {}).get("fg_title")
            self._log(f"[activate] 就绪后置前 hwnd={rec.get('hwnd')} "
                      f"setactive={rec.get('setactive')} "
                      f"setforeground={rec.get('setforeground')} "
                      f"topmost={rec.get('topmost')} in_front={rec.get('in_front')} "
                      f"fg_before={before!r} -> fg_after={after!r}")

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

    # 进程在但首页没渲染完（正常冷启动尾声）的复用宽限；超过仍不可交互才判 UI 假死。
    REUSE_READY_GRACE_S = 30.0

    def _raise_unready(self, what: str) -> None:
        """拉起/重启未到达可用首页：把失败类型、存活时长、退出时刻带进异常。

        上层只需看异常消息就能区分：
          [launch_failed:instant]  启动即退（进程几秒内自己退出）
          [launch_failed:timeout]  软失败（进程在且窗口在，但等不到可交互页面）
          [window_not_created]     进程在但从未建出任何窗口（独立一类，不混进 launch_failed）
          [launch_exhausted:…]     重试预算耗尽（多带尝试次数/最后退出时刻）
        """
        out = self.last_outcome
        oc = getattr(out, "outcome", "") if out is not None else ""
        if oc == "window_not_created":
            raise JyInstanceError(
                f"[window_not_created alive={out.alive_s:.1f}s pid={out.pid or '-'} "
                f"attempt={out.attempt} "
                f"window_seen_at={'-' if getattr(out, 'window_seen_at_s', None) is None else f'+{out.window_seen_at_s:.1f}s'} "
                f"how={getattr(out, 'how', '') or '-'}] "
                f"{what}后进程活着但从未创建剪映窗口。"
                f"{getattr(out, 'error', '') or ''}")
        if oc in ("died", "died_late", "died_after_ready", "timeout"):
            kind = getattr(out, "death_kind", "") or oc
            raise JyInstanceError(
                f"[launch_failed:{kind} alive={out.alive_s:.1f}s attempt={out.attempt} "
                f"exit_at={getattr(out, 'exit_at', '') or '-'} "
                f"env_check={int(bool(out.env_check_seen))}] {what}后未到达可交互首页。"
                f"{getattr(out, 'error', '') or ''}")
        raise JyInstanceError(f"{what}后未到达可交互首页（窗口未就绪/UI 假死）")

    def ensure_for_export(self, force_clean: bool = False,
                          timeout: float = HOME_READY_TIMEOUT_S,
                          entry: str = "export") -> bool:
        """导出前实例判定（唯一口径，准入链同样走这里）：实例是否可用、是否需要干净重启。

        三个导出入口（cover_watch / series_runner / 直调 robust）与准入链
        （pipeline_ui.ui_core.launch_via_manager）只表达意图，复用还是干净重启全由
        这里判定，不再各自维护 cold_start 默认值：
          force_clean=True   → 无条件冷启动（调用方显式强制）
          进程不在           → launch
          首页且可交互       → 复用（唯一允许复用的条件）
          编辑页             → 软关回首页，失败/假死再重启
          进程在但不可用     → 宽限等待后仍不可交互 = UI 假死 → restart，绝不复用
        entry 只影响启动台账分类（export / admit）。
        返回是否已停在可交互首页；拉起失败抛含分类标记的 JyInstanceError。
        """
        # 先在本线程建 UIA 单例：detect/launch 的探测都在子线程里跑，首次创建若发生在
        # 子线程会因未 CoInitialize 全部失败，把已就绪的实例误判成启动超时。
        self.backend.prime()
        self.entry = entry or "export"

        if force_clean:
            self._log("实例判定：显式要求干净启动 → 重启剪映")
            if self.restart(timeout) and self._normalize_home_after_start(timeout):
                return True
            self._raise_unready("冷启动")

        if not self.backend.process_running():
            self._log("实例判定：剪映未运行 → 拉起")
            if self.launch(timeout) and self._normalize_home_after_start(timeout):
                return True
            self._raise_unready("拉起")

        state = self.detect()
        if state == "home":
            self._log("实例判定：首页可交互 → 复用现有实例")
            return True
        if state == "editing":
            self._log("实例判定：停在编辑页 → 软关回首页（失败再重启）")
            if self.go_home(timeout=SOFT_HOME_TIMEOUT_S, allow_restart=True,
                            restart_timeout=timeout):
                return True
            self._raise_unready("回首页")

        # 进程在但窗口/页面不可用：先给冷启动尾声留宽限，仍不可交互即判 UI 假死。
        self._log(f"实例判定：进程在但实例不可用（state={state}），等待首页可交互...")
        if self.wait_ready("home", self._time() + self.REUSE_READY_GRACE_S):
            self._log("实例判定：宽限内首页恢复可交互 → 复用")
            return True
        self._log("实例判定：宽限内首页仍不可交互（疑似 UI 假死）→ 干净重启")
        if self.restart(timeout) and self._normalize_home_after_start(timeout):
            return True
        self._raise_unready("重启")
        return False  # pragma: no cover - _raise_unready 必抛

    def _normalize_home_after_start(self, timeout: float) -> bool:
        """启动成功后的归位：5.9 冷启动自动恢复草稿会停在编辑页 → 软关回首页。

        只在 state == 'editing' 时动作；首页直接放行。软关失败按 go_home 的
        allow_restart 兜底（重启后若又落编辑页，再软关一次）。
        """
        if self.state != "editing":
            return True
        self._log("实例判定：启动后停在编辑页（自动恢复草稿）→ 软关回首页")
        if self.go_home(timeout=SOFT_HOME_TIMEOUT_S, allow_restart=True,
                        restart_timeout=timeout):
            return True
        return False

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
