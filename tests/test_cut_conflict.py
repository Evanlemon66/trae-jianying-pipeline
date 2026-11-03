# -*- coding: utf-8 -*-
"""回归用例：裁剪分歧通知的三项修复（预览不发 / 写日志 / 去重落盘且值变才再弹）。

三档语义（见 cover_watch._round_flags）：
  预览（plan / --dry-run / --hold）       → (notify=False, write_log=False) 只打印
  --once 真实执行                        → (notify=False, write_log=True)  记日志、不弹窗
  持续 watcher 真实轮次                   → (notify=True,  write_log=True)  日志 + 弹窗

覆盖：
  ① 预览不弹 toast、不写日志、不写去重
  ② --once 真实执行：不弹 toast、写 watcher.log、只记 logged 通道
  ③ --once 之后持续 watcher 首轮：仍弹一次（notified 通道未被 --once 吞掉）
  ④ 同值重复（watcher / 跨进程 reload）：不再弹、不再刷日志
  ⑤ 值变化后再次触发（弹窗 + 新日志行）
  ⑥ _round_flags 三档映射

运行：python tests\\test_cut_conflict.py（无第三方依赖，不真导出）
"""
import argparse, importlib, json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import cover_watch as cw  # noqa: E402

fails = []
toasts = []
cw.toast = lambda title, msg, icon="info": toasts.append((title, msg))


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def fresh(name):
    d = os.path.join(TMP, name, "cover_job")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "export_config.json"), "w", encoding="utf-8") as f:
        json.dump({"cut_s": 60}, f)
    return d


def log_lines(jd, needle="cut 分歧"):
    p = os.path.join(jd, "watcher.log")
    if not os.path.isfile(p):
        return 0
    return sum(1 for line in open(p, encoding="utf-8") if needle in line)


def dedupe(jd):
    p = os.path.join(jd, cw.CUT_CONFLICT_NAME)
    return json.load(open(p, encoding="utf-8")) if os.path.isfile(p) else {}


TMP = os.path.join(tempfile.gettempdir(), "cut_conflict_test")
shutil.rmtree(TMP, ignore_errors=True)
wf = {"cut_s": 91.0, "intent_revision": 0}   # 与 export_config 60 分歧
job = {"video": {}}

print("\n== ① 预览：(False, False) 只打印 ==")
jd = fresh("预览")
toasts.clear()
cut = cw._effective_cut(wf, job, jd, notify=False, write_log=False)
check("preview 不弹 toast", len(toasts) == 0, toasts)
check("preview 不写 watcher.log", log_lines(jd) == 0)
check("preview 不写去重文件", not os.path.isfile(os.path.join(jd, cw.CUT_CONFLICT_NAME)))
check("cut 值仍正确解析", cut == 60.0, cut)

print("\n== ② --once 真实执行：(False, True) 记日志不弹窗 ==")
jd = fresh("once")
toasts.clear()
cw._effective_cut(wf, job, jd, notify=False, write_log=True)
check("--once 不弹 toast", len(toasts) == 0, toasts)
check("--once 写了 watcher.log", log_lines(jd) == 1, log_lines(jd))
check("日志含冲突行与采用值/分支",
      open(os.path.join(jd, "watcher.log"), encoding="utf-8").read().find("采用") >= 0)
ded = dedupe(jd)
check("只记 logged 通道", isinstance(ded.get("logged"), dict), ded)
check("logged 记录两来源值", ded["logged"].get("wf_cut") == 91.0
      and ded["logged"].get("ec_cut") == 60.0, ded)
check("notified 通道未写（未被 --once 吞掉）", "notified" not in ded, ded)

print("\n== ③ --once 之后持续 watcher 首轮：(True, True) 仍弹一次 ==")
toasts.clear()
cw._effective_cut(wf, job, jd, notify=True, write_log=True)
check("watcher 首轮弹一次", len(toasts) == 1, toasts)
check("日志不重复刷（仍 1 行）", log_lines(jd) == 1, log_lines(jd))
check("notified 通道落盘", isinstance(dedupe(jd).get("notified"), dict), dedupe(jd))

print("\n== ④ 同值重复不再弹、不再刷日志（含跨进程模拟）==")
toasts.clear()
cw._effective_cut(wf, job, jd, notify=True, write_log=True)
check("同进程同值重复不弹", len(toasts) == 0, toasts)
check("同值不刷日志", log_lines(jd) == 1, log_lines(jd))
# 跨进程模拟：reload 清空模块级内存态，去重仅靠落盘文件 → 仍不应弹
importlib.reload(cw)
toasts.clear()
cw.toast = lambda title, msg, icon="info": toasts.append((title, msg))
cw._effective_cut(wf, job, jd, notify=True, write_log=True)
check("reload 清内存态后同值仍不弹（落盘去重生效）", len(toasts) == 0, toasts)
check("reload 后同值仍不刷日志", log_lines(jd) == 1, log_lines(jd))

print("\n== ⑤ 值变化后再次触发 ==")
toasts.clear()
wf2 = {"cut_s": 120.0, "intent_revision": 0}   # wf_cut 变了
cw._effective_cut(wf2, job, jd, notify=True, write_log=True)
check("值变化重新弹", len(toasts) == 1, toasts)
check("值变化新写一行日志", log_lines(jd) == 2, log_lines(jd))
ded = dedupe(jd)
check("notified 已更新为新值", ded["notified"].get("wf_cut") == 120.0, ded)
check("logged 已更新为新值", ded["logged"].get("wf_cut") == 120.0, ded)

print("\n== ⑥ _round_flags 三档映射 ==")
check("持续 watcher → (True, True)", cw._round_flags(argparse.Namespace(once=False, dry_run=False), False) == (True, True))
check("--once 真实执行 → (False, True)", cw._round_flags(argparse.Namespace(once=True, dry_run=False), False) == (False, True))
check("--dry-run → (False, False)", cw._round_flags(argparse.Namespace(once=False, dry_run=True), False) == (False, False))
check("--once --dry-run → (False, False)", cw._round_flags(argparse.Namespace(once=True, dry_run=True), False) == (False, False))
check("配置 hold:true → (False, False)", cw._round_flags(argparse.Namespace(once=False, dry_run=False), True) == (False, False))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
