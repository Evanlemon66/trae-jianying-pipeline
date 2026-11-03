# -*- coding: utf-8 -*-
"""回归用例：第 3 项失败分类器 + 自愈策略表（lib/failure_class.py）。

覆盖：
  ① 五类各若干样本（disk_full / asset_missing / export_timeout / version_dialog / ui_hang）
  ② unknown 兜底且可重试
  ③ 分类顺序防误判（「未找到草稿」是 asset_missing 而非 ui_hang；「打开草稿超时」是 ui_hang）
  ④ 策略表：不可重试类（asset_missing/disk_full）不计数、置终态 failed；可重试类计数
  ⑤ cover_watch.note_export_result 集成：硬失败置 failed 不计数

运行：python tests\\test_failure_class.py（无第三方依赖，不真导出）
"""
import os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import failure_class as fc  # noqa: E402
import export_state as es   # noqa: E402
import cover_watch as cw    # noqa: E402

cw.toast = lambda *a, **k: None

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "failure_class_test")
shutil.rmtree(TMP, ignore_errors=True)
JD = os.path.join(TMP, "测试剧", "cover_job")
os.makedirs(JD, exist_ok=True)

print("\n== ① 五类样本 ==")
samples = {
    fc.DISK_FULL: ["磁盘空间不足: D: 剩余 0.5GB，预估需 2.0GB", "no space left on device"],
    fc.ASSET_MISSING: ["素材预检不通过：3 个源文件损坏", "未找到名为【八仙】的草稿。",
                       "文件不存在: D:/短剧/第1集.mp4"],
    fc.EXPORT_TIMEOUT: ["导出卡死：产物 180s 无增长（12.0MB）", "导出超时或未检测到完成状态。"],
    fc.VERSION_DIALOG: ["版本更新：以后再说", "检测到新版本，请升级"],
    fc.UI_HANG: ["打开草稿超时，未能进入编辑界面。请确认是否手动干预或程序卡死。",
                 "无法自动启动【剪映专业版】，请手动启动后重试。",
                 "重启后仍未回到首页，请手动检查剪映状态。"],
}
for cat, errs in samples.items():
    for e in errs:
        got, action = fc.classify(e)
        check(f"{cat}: {e[:22]}…", got == cat, f"got={got}")
        check(f"    动作={fc.strategy_of(cat)['action']}", action == fc.strategy_of(cat)["action"])

print("\n== ② unknown 兜底且可重试 ==")
got, _ = fc.classify("某个完全未知的错误 xyz")
check("未知错误 → unknown", got == fc.UNKNOWN, got)
check("unknown 可重试", fc.is_retryable(fc.UNKNOWN))

print("\n== ③ 分类顺序防误判 ==")
check("未找到草稿 = asset_missing（非 ui_hang）",
      fc.classify("未找到名为【A】的草稿。")[0] == fc.ASSET_MISSING)
check("打开草稿超时 = ui_hang（非 export_timeout）",
      fc.classify("打开草稿超时，未能进入编辑界面。")[0] == fc.UI_HANG)
check("磁盘不足优先于其它", fc.classify("磁盘空间不足，未找到草稿")[0] == fc.DISK_FULL)

print("\n== ④ 策略表：可重试性 / 计数 ==")
check("asset_missing 不可重试", not fc.is_retryable(fc.ASSET_MISSING))
check("disk_full 不可重试", not fc.is_retryable(fc.DISK_FULL))
check("asset_missing 不计数", not fc.counts_toward_failures(fc.ASSET_MISSING))
check("disk_full 不计数", not fc.counts_toward_failures(fc.DISK_FULL))
for c in (fc.UI_HANG, fc.EXPORT_TIMEOUT, fc.VERSION_DIALOG, fc.UNKNOWN):
    check(f"{c} 可重试且计数", fc.is_retryable(c) and fc.counts_toward_failures(c))
check("asset_missing 动作=fail_terminal",
      fc.strategy_of(fc.ASSET_MISSING)["action"] == fc.FAIL_TERMINAL)
check("disk_full 动作=refuse_launch",
      fc.strategy_of(fc.DISK_FULL)["action"] == fc.REFUSE_LAUNCH)

print("\n== ⑤ 状态机集成：不可重试置终态 failed 且不计数 ==")
st = es.fail(JD, "未找到名为【A】的草稿。", failure_class=fc.ASSET_MISSING, retryable=False)
check("硬失败 → status=failed", st["status"] == "failed", st)
check("硬失败不递增计数", st["consecutive_failures"] == 0, st)
check("记录 failure_class", st["failure_class"] == fc.ASSET_MISSING)
check("failed 被闸门拦截", "硬失败" in (es.gate(JD) or ""), es.gate(JD))
check("--phase export 仍可强制放行", es.gate(JD, phase_override="export") is None)
# 可重试对照：计数递增、进 retrying
st2 = es.fail(JD, "打开草稿超时", failure_class=fc.UI_HANG, retryable=True)
check("可重试 → retrying 且计数=1",
      st2["status"] == "retrying" and st2["consecutive_failures"] == 1, st2)

print("\n== ⑥ note_export_result 集成（硬失败不计数）==")
JD2 = os.path.join(TMP, "集成剧", "cover_job")
os.makedirs(JD2, exist_ok=True)
cw.note_export_result(JD2, "集成剧", False, error="磁盘空间不足",
                      failure_class=fc.DISK_FULL, retryable=False)
st3 = es.load(JD2)
check("note 硬失败 → failed", st3["status"] == "failed", st3)
check("note 硬失败不计数", st3["consecutive_failures"] == 0, st3)
cw.note_export_result(JD2, "集成剧", False, error="打开草稿超时",
                      failure_class=fc.UI_HANG, retryable=True)
# 注意：failed 之后再来一次可重试失败，会把状态带回 retrying 并计数 1
st4 = es.load(JD2)
check("note 可重试失败 → retrying 计数=1",
      st4["status"] == "retrying" and st4["consecutive_failures"] == 1, st4)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
