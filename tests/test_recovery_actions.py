# -*- coding: utf-8 -*-
"""回归用例：自愈动作分派（第 1 项补充：kill_retry 真正强杀重启）。

证明三条动作路径分别走到预期调用（用桩替换管理器与 Exporter）：
  - kill_retry（export_timeout）→ 强制经管理器 restart（kill+launch+wait home），不走软关
  - restart / unknown（ui_hang）→ 软关优先（switch_to_home），不走管理器 kill
  - dismiss_retry（version_dialog）→ 先关弹窗，再软关
并验证：export_timeout 重试必走 kill 分支；ui_hang 仍软关优先；实际动作写入 _TASK。

运行：python tests\\test_recovery_actions.py（桩替换，不真导出、不真杀进程）
"""
import os, sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, ".trae", "skills", "jianying-editor", "examples"))
import failure_class as fc        # noqa: E402
import robust_auto_export as rae  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


class _FakeBackend:
    def rebind_window(self):
        return None


class FakeMgr:
    def __init__(self, ready=True):
        self.backend = _FakeBackend()
        self.restart_calls = 0
        self.go_home_calls = 0
        self.ready = ready

    def restart(self, timeout=90):
        self.restart_calls += 1
        return True

    def go_home(self, timeout=60, allow_restart=True):
        self.go_home_calls += 1
        return self.ready


class FakeExporter:
    def __init__(self):
        self.switch_calls = 0
        self.dismiss_calls = 0
        self._manager = None

    def switch_to_home(self):
        self.switch_calls += 1

    def dismiss_blocking_dialogs(self):
        self.dismiss_calls += 1


_orig_get_manager = rae._get_manager

print("\n== kill_retry → 强制管理器 kill+launch（不走软关）==")
fm, fe = FakeMgr(), FakeExporter()
rae._get_manager = lambda ex: fm
ret = rae._do_recovery("kill_retry", exporter=fe)
check("返回 kill_restart", ret == "kill_restart", ret)
check("走了管理器 restart(kill+launch)", fm.restart_calls == 1, fm.restart_calls)
check("未走软关 switch_to_home", fe.switch_calls == 0, fe.switch_calls)
check("未关弹窗", fe.dismiss_calls == 0)
check("_TASK.recovery_action=kill_restart", rae._TASK["recovery_action"] == "kill_restart")

print("\n== restart（ui_hang）→ 软关优先，不杀进程 ==")
fm2, fe2 = FakeMgr(), FakeExporter()
rae._get_manager = lambda ex: fm2
ret = rae._do_recovery("restart", exporter=fe2)
check("返回 soft_home", ret == "soft_home", ret)
check("走了软关 switch_to_home", fe2.switch_calls == 1, fe2.switch_calls)
check("未走管理器 kill", fm2.restart_calls == 0, fm2.restart_calls)

print("\n== dismiss_retry：弹窗关掉且就绪 → 只软回（dismiss）==")
fm3, fe3 = FakeMgr(ready=True), FakeExporter()
rae._get_manager = lambda ex: fm3
ret = rae._do_recovery("dismiss_retry", exporter=fe3)
check("返回 dismiss", ret == "dismiss", ret)
check("关了弹窗", fe3.dismiss_calls == 1, fe3.dismiss_calls)
check("做了就绪判断 go_home", fm3.go_home_calls == 1, fm3.go_home_calls)
check("就绪则不强杀（restart=0）", fm3.restart_calls == 0, fm3.restart_calls)
check("不走 switch_to_home", fe3.switch_calls == 0, fe3.switch_calls)
check("_TASK.recovery_action=dismiss", rae._TASK["recovery_action"] == "dismiss")

print("\n== dismiss_retry：弹窗关掉但仍未就绪 → 当轮升级强杀（dismiss_then_kill）==")
fm3b, fe3b = FakeMgr(ready=False), FakeExporter()
rae._get_manager = lambda ex: fm3b
ret = rae._do_recovery("dismiss_retry", exporter=fe3b)
check("返回 dismiss_then_kill", ret == "dismiss_then_kill", ret)
check("关了弹窗", fe3b.dismiss_calls == 1, fe3b.dismiss_calls)
check("就绪判断未就绪", fm3b.go_home_calls == 1, fm3b.go_home_calls)
check("当轮升级强杀（restart=1）", fm3b.restart_calls == 1, fm3b.restart_calls)
check("_TASK.recovery_action=dismiss_then_kill",
      rae._TASK["recovery_action"] == "dismiss_then_kill")

print("\n== 分类 → 动作 → 执行分支 端到端 ==")
c, a = fc.classify("导出卡死：产物 180s 无增长（12.0MB）")
check("export_timeout → 动作 kill_retry", c == fc.EXPORT_TIMEOUT and a == "kill_retry", (c, a))
c2, a2 = fc.classify("打开草稿超时，未能进入编辑界面。")
check("ui_hang → 动作 restart", c2 == fc.UI_HANG and a2 == "restart", (c2, a2))

fe4, fm4 = FakeExporter(), FakeMgr()
rae._get_manager = lambda ex: fm4
rae._do_recovery(a, exporter=fe4)  # export_timeout 的动作
check("export_timeout 重试必走 kill 分支", fm4.restart_calls == 1 and fe4.switch_calls == 0)

fe5, fm5 = FakeExporter(), FakeMgr()
rae._get_manager = lambda ex: fm5
rae._do_recovery(a2, exporter=fe5)  # ui_hang 的动作
check("ui_hang 重试仍软关优先", fe5.switch_calls == 1 and fm5.restart_calls == 0)

rae._get_manager = _orig_get_manager

print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
