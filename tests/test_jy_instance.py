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

print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
