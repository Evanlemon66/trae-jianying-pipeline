# -*- coding: utf-8 -*-
"""回归用例：裁剪分歧通知的三项修复（预览不发 / 写日志 / 去重落盘且值变才再弹）。

覆盖：
  ① plan/预览（notify=False）不触发 toast
  ② 真实轮次（notify=True）冲突触发一次
  ③ 同值重复运行不再触发（跨进程模拟：reload 模块清内存态后仍不弹，去重靠落盘）
  ④ 值变化后再次触发
  ⑤ 冲突行确实写进 watcher.log

运行：python tests\\test_cut_conflict.py（无第三方依赖，不真导出）
"""
import importlib, json, os, shutil, sys, tempfile

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


TMP = os.path.join(tempfile.gettempdir(), "cut_conflict_test")
shutil.rmtree(TMP, ignore_errors=True)
JD = os.path.join(TMP, "测试剧", "cover_job")
os.makedirs(JD, exist_ok=True)
with open(os.path.join(JD, "export_config.json"), "w", encoding="utf-8") as f:
    json.dump({"cut_s": 60}, f)

wf = {"cut_s": 91.0, "intent_revision": 0}   # 与 export_config 60 分歧
job = {"video": {}}

print("\n== ① 预览/只读（notify=False）不触发 toast ==")
toasts.clear()
cut = cw._effective_cut(wf, job, JD, notify=False)
check("preview 不弹 toast", len(toasts) == 0, toasts)
check("preview 不写去重文件", not os.path.isfile(os.path.join(JD, cw.CUT_CONFLICT_NAME)))
check("cut 值仍正确解析", cut == 60.0, cut)

print("\n== ② 真实轮次（notify=True）冲突触发一次 ==")
toasts.clear()
cw._effective_cut(wf, job, JD, notify=True)
check("真实轮次弹一次 toast", len(toasts) == 1, toasts)
check("去重文件已落盘", os.path.isfile(os.path.join(JD, cw.CUT_CONFLICT_NAME)))
ded = json.load(open(os.path.join(JD, cw.CUT_CONFLICT_NAME), encoding="utf-8"))
check("去重记录两来源值", ded.get("wf_cut") == 91.0 and ded.get("ec_cut") == 60.0, ded)

print("\n== ③ 同值重复不再触发（含跨进程模拟）==")
toasts.clear()
cw._effective_cut(wf, job, JD, notify=True)
check("同进程同值重复不弹", len(toasts) == 0, toasts)
# 跨进程模拟：reload 清空模块级内存态，去重仅靠落盘文件 → 仍不应弹
importlib.reload(cw)
toasts.clear()
cw.toast = lambda title, msg, icon="info": toasts.append((title, msg))
cw._effective_cut(wf, job, JD, notify=True)
check("reload 清内存态后同值仍不弹（落盘去重生效）", len(toasts) == 0, toasts)

print("\n== ④ 值变化后再次触发 ==")
toasts.clear()
wf2 = {"cut_s": 120.0, "intent_revision": 0}   # wf_cut 变了
cw._effective_cut(wf2, job, JD, notify=True)
check("值变化重新弹", len(toasts) == 1, toasts)
ded = json.load(open(os.path.join(JD, cw.CUT_CONFLICT_NAME), encoding="utf-8"))
check("去重记录已更新为新值", ded.get("wf_cut") == 120.0, ded)

print("\n== ⑤ 冲突行写进 watcher.log ==")
log_path = os.path.join(JD, "watcher.log")
check("watcher.log 存在", os.path.isfile(log_path))
logtxt = open(log_path, encoding="utf-8").read() if os.path.isfile(log_path) else ""
check("日志含冲突行", "cut 分歧" in logtxt and "workflow.cut_s" in logtxt, logtxt[-200:])
check("日志含采用值与优先级分支", "采用" in logtxt and ("export_config 优先" in logtxt or "workflow 优先" in logtxt))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
