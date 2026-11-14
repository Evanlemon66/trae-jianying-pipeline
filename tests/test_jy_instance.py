# -*- coding: utf-8 -*-
"""回归用例：第 2 项剪映实例生命周期管理器（jy_instance.py）。

覆盖：
  ① 状态机转移 stopped→launching→home（launch）
  ② detect 四态判定
  ③ 就绪超时：wait_ready 到 deadline 返回 False（墙钟，不无限等）
  ④ ensure：不达 deadline 只 restart 一次，再失败抛 JyInstanceError（kill 恰 1 次）
  ⑤ kill 唯一入口：只有管理器 kill() 计数；launch 不杀进程
  ⑥ go_home 软关编辑页优先（不杀进程）

用假后端 + 假时钟，无需真实剪映 / UIA。运行：python tests\\test_jy_instance.py
"""
import os, sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EX = os.path.join(_ROOT, ".trae", "skills", "jianying-editor", "examples")
sys.path.insert(0, _EX)
# 假后端也会走 ensure_jianying → 必须关掉台账，否则假记录会写进生产台账。
os.environ.setdefault('JY_LAUNCH_LEDGER', 'off')
import jy_instance as ji  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeBackend(ji.Backend):
    def __init__(self):
        self.running = False
        self.window = False
        self.home = False
        self.edit = False
        self.kill_count = 0
        self.launch_count = 0
        self.close_called = 0
        self.launch_effect = None

    def process_running(self):
        return self.running

    def window_exists(self):
        return self.window

    def is_home(self):
        return self.home

    def is_edit(self):
        return self.edit

    def launch(self, exe):
        self.launch_count += 1
        if self.launch_effect:
            self.launch_effect()

    def kill(self):
        self.kill_count += 1
        self.running = False
        self.window = False
        self.home = False
        self.edit = False

    def close_edit_to_home(self):
        self.close_called += 1
        self.edit = False
        self.home = True
        return True

    def rebind_window(self):
        return object()


def make(backend, poll=1.0, probe=2.0):
    clk = FakeClock()
    mgr = ji.JyInstance(backend=backend, find_exe=lambda: "fake.exe",
                        time_fn=clk.time, sleep_fn=clk.sleep,
                        poll_interval=poll, probe_timeout=probe,
                        cleanup_fn=lambda _log: [], detect_detector_stall=False,
                        log=lambda *_a: None)
    return mgr, clk


print("\n== ① launch：stopped → launching → home ==")
b = FakeBackend()
b.launch_effect = lambda: setattr(b, "running", True) or setattr(b, "window", True) or setattr(b, "home", True)
mgr, clk = make(b)
ok = mgr.launch(timeout=30)
check("launch 到达 home", ok is True and mgr.state == "home", mgr.state)
check("launch 只拉起一次", b.launch_count == 1)
check("launch 不杀进程", b.kill_count == 0)

print("\n== ② detect 四态 ==")
b = FakeBackend()
mgr, _ = make(b)
check("无进程 → stopped", mgr.detect() == "stopped")
b.running = True
check("有进程无窗口 → launching", mgr.detect() == "launching")
b.window = True; b.home = True
check("首页 → home", mgr.detect() == "home")
b.home = False; b.edit = True
check("编辑页 → editing", mgr.detect() == "editing")

print("\n== ③ wait_ready 就绪超时（墙钟，不无限等）==")
b = FakeBackend()
b.running = True; b.window = True; b.home = False  # 永远到不了 home
mgr, clk = make(b)
t0 = clk.t
res = mgr.wait_ready("home", clk.t + 5)
check("到 deadline 返回 False", res is False)
check("墙钟推进≈5s（非无限）", 5 <= (clk.t - t0) <= 8, clk.t - t0)

print("\n== ④ ensure：只 restart 一次，再失败抛异常 ==")
b = FakeBackend()
# launch 后进程在、窗口在，但永远不到 home（模拟 UI 假死）
b.launch_effect = lambda: setattr(b, "running", True) or setattr(b, "window", True)
mgr, clk = make(b)
raised = False
try:
    mgr.ensure("home", timeout=10)
except ji.JyInstanceError as e:
    raised = True
    msg = str(e)
check("两次未就绪抛 JyInstanceError", raised)
check("异常含可分类关键词", ("未就绪" in msg) or ("冷启动失败" in msg), msg)
check("恰好重启一次（kill==1）", b.kill_count == 1, b.kill_count)
check("拉起两次（首试+重启）", b.launch_count == 2, b.launch_count)

print("\n== ⑤ kill 唯一入口 ==")
b = FakeBackend()
b.running = True
mgr, _ = make(b)
mgr.kill()
check("kill 计数 +1 且进程置 stopped", b.kill_count == 1 and mgr.state == "stopped")

print("\n== ⑥ go_home 软关编辑页优先（不杀进程）==")
b = FakeBackend()
b.running = True; b.window = True; b.edit = True  # 在编辑页
mgr, clk = make(b)
ok = mgr.go_home(timeout=30, allow_restart=True)
check("软关后回到 home", ok is True and mgr.state == "home", mgr.state)
check("软关走了关闭按钮", b.close_called == 1)
check("软关未杀进程", b.kill_count == 0)

print("\n== ⑦ 冷启动墙钟放宽（第 1 条稳定性修复）==")
import inspect  # noqa: E402

check("launch 默认墙钟 ≥120s",
      inspect.signature(ji.JyInstance.launch).parameters["timeout"].default >= 120.0,
      inspect.signature(ji.JyInstance.launch).parameters["timeout"].default)
check("restart 默认墙钟 ≥120s",
      inspect.signature(ji.JyInstance.restart).parameters["timeout"].default >= 120.0)
check("go_home 的 restart_timeout 默认 ≥120s",
      inspect.signature(ji.JyInstance.go_home).parameters["restart_timeout"].default >= 120.0)


class DelayedHomeBackend(FakeBackend):
    """拉起后 100s 才渲染出 HomePage（窗口一直在，但页面未就绪）。"""

    def __init__(self, clk, delay=100.0, editable=False):
        super().__init__()
        self.clk = clk
        self.delay = delay
        self.editable = editable
        self._t0 = None

    def launch(self, exe):
        super().launch(exe)
        self.running = True
        self.window = True
        self.edit = self.editable
        self._t0 = self.clk.time()

    def is_home(self):
        return self._t0 is not None and (self.clk.time() - self._t0) >= self.delay


def make_with(backend, clk):
    return ji.JyInstance(backend=backend, find_exe=lambda: "fake.exe",
                         time_fn=clk.time, sleep_fn=clk.sleep,
                         poll_interval=1.0, probe_timeout=2.0,
                         cleanup_fn=lambda _log: [], detect_detector_stall=False,
                         log=lambda *_a: None)


clk7 = FakeClock()
b7 = DelayedHomeBackend(clk7, delay=100.0)
mgr7 = make_with(b7, clk7)
check("首页 100s 才就绪：默认 launch 能等到", mgr7.launch() is True, mgr7.state)

clk7b = FakeClock()
b7b = DelayedHomeBackend(clk7b, delay=100.0)
mgr7b = make_with(b7b, clk7b)
check("同一场景按旧 60s 会失败（对照）", mgr7b.launch(timeout=60) is False)

print("\n== ⑧ go_home：软关无效时，restart 用 180s 而不是软关的 60s ==")
clk8 = FakeClock()
b8 = DelayedHomeBackend(clk8, delay=120.0, editable=True)


def _soft_close_noop():
    b8.edit = False          # 软关点空：没回到首页
    return False


b8.close_edit_to_home = _soft_close_noop
mgr8 = make_with(b8, clk8)
ok8 = mgr8.go_home()  # 默认 soft 60 + restart 180
check("软关 60s 后重启，120s 才就绪仍能回到首页", ok8 is True, mgr8.state)
check("确实走了强杀重启", b8.kill_count == 1, b8.kill_count)

print("\n== ⑨ 启动即死（20s 内消失）→ 结构化日志 + 立即重试一次成功 ==")
clk9 = FakeClock()
b9 = FakeBackend()
logs9 = []
die_at = {}


def _eff9():
    if b9.launch_count == 1:          # 首次：起来了但 5s 后就没了（13:25/13:59 现象）
        b9.running, b9.window, b9.home = True, False, False
        die_at["t"] = clk9.t + 5
    else:                              # 重试：正常到首页（不再复用首试的死亡时刻）
        die_at["t"] = None
        b9.running, b9.window, b9.home = True, True, True


b9.launch_effect = _eff9
b9.process_running = lambda: b9.running and (die_at.get("t") is None or clk9.t < die_at["t"])
mgr9 = ji.JyInstance(backend=b9, find_exe=lambda: "fake.exe", time_fn=clk9.time,
                     sleep_fn=clk9.sleep, poll_interval=1.0, probe_timeout=2.0,
                     cleanup_fn=lambda _log: [], detect_detector_stall=False,
                     log=logs9.append)
t9 = clk9.t
ok9 = mgr9.launch(timeout=30)
check("重试后到达 home", ok9 is True and mgr9.state == "home", mgr9.state)
check("拉起两次（首试+重试）", b9.launch_count == 2, b9.launch_count)
died9 = [l for l in logs9 if l.startswith("[startup-died]")]
check("记了一条 [startup-died]", len(died9) == 1, died9)
check("结构化字段齐全（pid/started/alive_s/app_log_bytes）",
      bool(died9) and all(k in died9[0] for k in
                          ("pid=", "started=", "alive_s=", "app_log=", "app_log_bytes=")),
      died9[0] if died9 else "")
check("标记为可重试 retry=1", bool(died9) and "retry=1" in died9[0], died9[0] if died9 else "")
check("秒退分类与退出时刻已记录",
      bool(died9) and "kind=instant" in died9[0] and "exit_at=" in died9[0],
      died9[0] if died9 else "")
check("重拉前清残留并静置 2s（首试5s + 2s 间隔 + 重试 1s + 观察窗 3s）",
      10.0 <= (clk9.t - t9) <= 12.0, clk9.t - t9)

print("\n== ⑩ 重试上限写死 2 次：三次都启动即死 → 失败，不再拉第四次 ==")
clk10 = FakeClock()
b10 = FakeBackend()
logs10 = []


def _eff10():
    b10.running, b10.window, b10.home = True, False, False
    b10._die_at = clk10.t + 5


b10._die_at = None
b10.launch_effect = _eff10
b10.process_running = lambda: b10.running and (b10._die_at is None or clk10.t < b10._die_at)
mgr10 = ji.JyInstance(backend=b10, find_exe=lambda: "fake.exe", time_fn=clk10.time,
                      sleep_fn=clk10.sleep, poll_interval=1.0, probe_timeout=2.0,
                      cleanup_fn=lambda _log: [], detect_detector_stall=False,
                      log=logs10.append)
ok10 = mgr10.launch(timeout=30)
check("返回 False（按原逻辑 fail）", ok10 is False)
check("总拉起次数 == 3（首试 + 2 次重拉，不无限重试）", b10.launch_count == 3, b10.launch_count)
died10 = [l for l in logs10 if l.startswith("[startup-died]")]
check("三次即死各记一条，末条 retry=0",
      len(died10) == 3 and "retry=0" in died10[-1], died10)
check("预算耗尽时给出终态错误（次数/退出时刻/类型）",
      "launch_exhausted" in (getattr(mgr10.last_outcome, "error", "") or ""),
      getattr(mgr10.last_outcome, "error", ""))
# 预算按管理器计：导出层再次 attempt 重试调 launch 时不会再额外拉起
ok10b = mgr10.launch(timeout=30)
check("预算耗尽后再 launch 只拉一次（不随 attempt 倍增）",
      ok10b is False and b10.launch_count == 4, b10.launch_count)

print("\n== ⑪ 晚死（>20s）且从未建出窗口 → 按新口径重拉 ==")
clk11 = FakeClock()
b11 = FakeBackend()
logs11 = []


def _eff11():
    b11.running, b11.window, b11.home = True, False, False
    b11._die_at = clk11.t + 25


b11._die_at = None
b11.launch_effect = _eff11
b11.process_running = lambda: b11.running and (b11._die_at is None or clk11.t < b11._die_at)
mgr11 = ji.JyInstance(backend=b11, find_exe=lambda: "fake.exe", time_fn=clk11.time,
                      sleep_fn=clk11.sleep, poll_interval=1.0, probe_timeout=2.0,
                      cleanup_fn=lambda _log: [], detect_detector_stall=False,
                      log=logs11.append)
_orig_seen11 = ji.jl._default_window_seen
ji.jl._default_window_seen = lambda: False   # 固定"从未建出窗口"
try:
    ok11 = mgr11.launch(timeout=60)
finally:
    ji.jl._default_window_seen = _orig_seen11
check("返回 False", ok11 is False)
check("无窗口晚死重拉到预算上限（3 次尝试）", b11.launch_count == 3, b11.launch_count)
check("结构化日志标记 late=1", any("late=1" in l for l in logs11), logs11)

print("\n== ⑪b 晚死但已建出窗口 → 沿用原口径，不重拉 ==")
clk11b = FakeClock()
b11b = FakeBackend()


def _eff11b():
    b11b.running, b11b.window, b11b.home = True, True, False
    b11b._die_at = clk11b.t + 25


b11b._die_at = None
b11b.launch_effect = _eff11b
b11b.process_running = lambda: b11b.running and (b11b._die_at is None or clk11b.t < b11b._die_at)
mgr11b = ji.JyInstance(backend=b11b, find_exe=lambda: "fake.exe", time_fn=clk11b.time,
                       sleep_fn=clk11b.sleep, poll_interval=1.0, probe_timeout=2.0,
                       cleanup_fn=lambda _log: [], detect_detector_stall=False,
                       log=lambda _m: None)
_orig_seen11b = ji.jl._default_window_seen
ji.jl._default_window_seen = lambda: True    # 固定"建出过窗口"
try:
    ok11b = mgr11b.launch(timeout=60)
finally:
    ji.jl._default_window_seen = _orig_seen11b
check("返回 False", ok11b is False)
check("窗口已建出后的晚死不重拉", b11b.launch_count == 1, b11b.launch_count)

print("\n== ⑫ restart：kill 与新进程之间留 2–5s 静默期 ==")
clk12 = FakeClock()
b12 = FakeBackend()
b12.running = True
marks12 = []
_orig_kill12 = b12.kill


def _kill12():
    marks12.append(("kill", clk12.t))
    _orig_kill12()


def _eff12():
    marks12.append(("launch", clk12.t))
    b12.running, b12.window, b12.home = True, True, True


b12.kill = _kill12
b12.launch_effect = _eff12
mgr12 = ji.JyInstance(backend=b12, find_exe=lambda: "fake.exe", time_fn=clk12.time,
                      sleep_fn=clk12.sleep, poll_interval=1.0, probe_timeout=2.0,
                      cleanup_fn=lambda _log: [], detect_detector_stall=False,
                      log=lambda *_a: None)
ok12 = mgr12.restart(timeout=30)
gap12 = marks12[1][1] - marks12[0][1]
check("重启后到达 home", ok12 is True and mgr12.state == "home", mgr12.state)
# B4 对照观测（2026-11-14，6 轮）：进程确认消失后立即拉起 6/6 成功，
# 含残留检测器仍在时的立即重拉 3/3 → 不再留静默期。
check("kill→launch 无静默期", gap12 == 0.0, gap12)

print("\n== ⑬ kill 后旧进程仍在：返回 False 并记一行 ==")
clk13 = FakeClock()
b13 = FakeBackend()
b13.running = True
logs13 = []


def _immortal_kill():
    b13.kill_count += 1          # taskkill 打了，但进程杀不掉


b13.kill = _immortal_kill
b13.launch_effect = lambda: (setattr(b13, "running", True) or
                             setattr(b13, "window", True) or setattr(b13, "home", True))
mgr13 = ji.JyInstance(backend=b13, find_exe=lambda: "fake.exe", time_fn=clk13.time,
                      sleep_fn=clk13.sleep, poll_interval=1.0, probe_timeout=2.0,
                      cleanup_fn=lambda _log: [], detect_detector_stall=False,
                      log=logs13.append)
check("kill() 返回 False", mgr13.kill() is False)
ok13 = mgr13.restart(timeout=30)
# died_late 止血层：旧进程杀不掉时绝不再 spawn 第二个（防双开），只重连/等待；
# 旧进程从未到 home → restart 失败，且 launch 一次都没发生。
check("restart 不双开：只重连不 spawn，返回 False", ok13 is False, mgr13.state)
check("旧进程存活时 launch_count == 0", b13.launch_count == 0, b13.launch_count)
check("记了『旧进程仍在运行』", any("旧进程仍在运行" in l for l in logs13), logs13)

print("\n== ⑭ _main_window：找不到主窗口返回 None（不再把搜索条件当窗口）==")


class _FakeWin:
    def __init__(self, name, exists, class_name=""):
        self.Name = name
        self._exists = exists
        self.ClassName = class_name

    def Exists(self, timeout=0):
        return self._exists

    def SetActive(self):
        pass

    def SetTopmost(self, on):
        pass


class _FakeUia:
    """最小 UIA 桩：只回答按 Name 的顶层窗口查询。"""

    def __init__(self, windows=None):
        self.windows = windows or {}
        self.queries = []

    def WindowControl(self, searchDepth=1, Name=""):
        self.queries.append(Name)
        return self.windows.get(Name, _FakeWin(Name, False))


def _backend_with(windows):
    b = ji.UIABackend()
    b._uia = _FakeUia(windows)
    b.log = lambda *_a: None
    return b


b14 = _backend_with({})
check("两个窗口都不存在 → _main_window() is None", b14._main_window() is None)
check("此时 is_home 为 False（不抛错）", b14.is_home() is False)
check("此时 is_edit 为 False（不抛错）", b14.is_edit() is False)

b14b = _backend_with({"导出": _FakeWin("导出", True, "ExportWindow")})
w14b = b14b._main_window()
check("只有导出弹窗时仍可回退（有意保留）",
      w14b is not None and w14b.Name == "导出", getattr(w14b, "Name", None))

print("\n== ⑮ rebind_window：拿不到主窗口返回 None 且不覆盖旧绑定 ==")
b15 = _backend_with({})
old15 = _FakeWin("旧主窗口", True, "HomePage")
b15.window = old15
check("rebind_window() 返回 None", b15.rebind_window() is None)
check("旧绑定未被覆盖", b15.window is old15)

b15b = _backend_with({"剪映专业版": _FakeWin("剪映专业版", True, "HomePage")})
w15b = b15b.rebind_window()
check("主窗口存在时正常绑定", w15b is not None and b15b.window is w15b
      and w15b.ClassName == "HomePage")

print("\n== ⑯ 就绪后置前：launch 成功时 rebind_window 恰好调用一次 ==")


class _CountBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.rebind_count = 0
        self.last_activation = {"hwnd": 1, "setactive": "ok", "setforeground": True,
                                "topmost": True, "in_front": True,
                                "before": {"fg_title": "Agent"},
                                "after": {"fg_title": "剪映专业版"}}

    def rebind_window(self):
        self.rebind_count += 1
        return object()


def _ready_effect(b):
    def eff():
        b.running = b.window = b.home = True
    return eff


b16 = _CountBackend()
b16.launch_effect = _ready_effect(b16)
mgr16, _ = make(b16)
ok16 = mgr16.launch(timeout=30)
check("launch 到达 home", ok16 is True and mgr16.state == "home", mgr16.state)
check("就绪后置前只调用一次", b16.rebind_count == 1, b16.rebind_count)

print("\n== ⑰ 置前失败不影响启动结果 ==")


class _BadRebindBackend(_CountBackend):
    def rebind_window(self):
        self.rebind_count += 1
        raise RuntimeError("SetActive refused")


b17 = _BadRebindBackend()
b17.launch_effect = _ready_effect(b17)
mgr17, _ = make(b17)
ok17 = mgr17.launch(timeout=30)
check("置前抛异常仍判启动成功", ok17 is True and mgr17.state == "home", mgr17.state)
check("outcome 仍为 ok（不进失败分类）",
      mgr17.last_outcome is not None and mgr17.last_outcome.ok is True)
check("置前确实被尝试过", b17.rebind_count == 1, b17.rebind_count)

print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
