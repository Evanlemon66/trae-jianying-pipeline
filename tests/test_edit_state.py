# -*- coding: utf-8 -*-
"""回归用例：edit 阶段失败退避/暂停（lib/edit_state.py + cover_watch 集成）。

覆盖：
  ① 失败计数与退避窗口递增（1/2/4…分钟）
  ② 第 3 次置 paused 且 toast
  ③ 不可重试类直接 failed、不计数
  ④ 缺目录自动补齐、不占额度、立即重试（二次仍缺 → 普通失败）
  ⑤ 退避窗口内被闸门挡且原因可读；editing 不拦截
  ⑥ --phase edit 放行不清零
  ⑦ 成功清零

运行：python tests\\test_edit_state.py（无第三方依赖，不真导出）
"""
import json, os, shutil, sys, tempfile, time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import edit_state as eds  # noqa: E402
import cover_watch as cw  # noqa: E402

fails = []
toasts = []
cw.toast = lambda title, msg, icon="info": toasts.append((title, msg))


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "edit_state_test")
shutil.rmtree(TMP, ignore_errors=True)


def fresh(name):
    d = os.path.join(TMP, name, "cover_job")
    os.makedirs(d, exist_ok=True)
    return d


print("\n== ① 失败计数与退避窗口递增 ==")
jd = fresh("退避")
now = time.time()
st = eds.fail(jd, "e1", now=now)
check("第 1 次 → retrying", st["status"] == "retrying", st)
check("第 1 次计数=1", st["consecutive_failures"] == 1)
check("第 1 次退避≈60s", abs((st["next_retry_at"] - now) - 60) <= 2, st["next_retry_at"] - now)
st = eds.fail(jd, "e2", now=now)
check("第 2 次 → retrying 且计数=2", st["status"] == "retrying" and st["consecutive_failures"] == 2)
check("第 2 次退避≈120s", abs((st["next_retry_at"] - now) - 120) <= 2, st["next_retry_at"] - now)

print("\n== ② 第 3 次置 paused 且 toast ==")
toasts.clear()
cw.note_edit_result(jd, "退避", False, error="e3", failure_class="unknown", retryable=True,
                    pause_after=eds.DEFAULT_PAUSE_AFTER)
st = eds.load(jd)
check("第 3 次触发 paused", st["status"] == "paused", st)
check("paused 后 next_retry_at=0", st["next_retry_at"] == 0)
check("弹了暂停 toast", any("剪辑连续失败已暂停" in t[0] for t in toasts), toasts)

print("\n== ③ 不可重试类直接 failed、不计数 ==")
jd = fresh("硬失败")
toasts.clear()
cw.note_edit_result(jd, "硬失败", False, error="素材预检不通过：源文件损坏",
                    failure_class="asset_missing", retryable=False)
st = eds.load(jd)
check("status=failed", st["status"] == "failed", st)
check("不递增计数", st["consecutive_failures"] == 0, st)
check("记录 failure_class", st["failure_class"] == "asset_missing")
check("弹了硬失败 toast", any("剪辑硬失败" in t[0] for t in toasts), toasts)
check("闸门拦截硬失败", "硬失败" in (eds.gate(jd) or ""), eds.gate(jd))

print("\n== ④ 缺目录自动补齐、不占额度、立即重试 ==")
jd = fresh("自愈")
eds.begin(jd)  # 模拟一次 edit 尝试
err = "ingest 后校验失败: 缺少目录: 05_video"
check("解析出缺失目录", cw._parse_missing_dirs(err) == ["05_video"])
check("越界路径被过滤", cw._parse_missing_dirs("缺少目录: ../evil") == [])
check("首次自愈成功", cw._try_heal_missing_dirs(jd, err, "自愈") is True)
check("目录已补齐", os.path.isdir(os.path.join(jd, "05_video")))
st = eds.load(jd)
check("不递增失败计数", st["consecutive_failures"] == 0, st)
check("状态回 pending（立即可重试）", st["status"] == "pending", st)
check("healed_dirs 已记录", st["healed_dirs"] == ["05_video"], st)
check("自愈日志已写", "已自动补齐缺失目录" in
      open(os.path.join(jd, "watcher.log"), encoding="utf-8").read())
check("闸门放行（立即重试）", eds.gate(jd) is None, eds.gate(jd))
# 补过仍缺 → 不再自愈，按普通失败
check("二次同错不再自愈", cw._try_heal_missing_dirs(jd, err, "自愈") is False)
cw.note_edit_result(jd, "自愈", False, error=err, failure_class="unknown", retryable=True)
check("二次失败正常计数=1", eds.load(jd)["consecutive_failures"] == 1)

print("\n== ⑤ 退避窗口内被闸门挡且原因可读；editing 不拦截 ==")
jd = fresh("闸门")
now = time.time()
eds.fail(jd, "boom", now=now)
g = eds.gate(jd, now=now + 5)
check("退避中被挡", g is not None, g)
check("原因含退避与剩余秒", ("剪辑失败退避中" in g and "s 后重试" in g), g)
check("原因含失败次数", "已失败 1 次" in g, g)
check("窗口过后放行", eds.gate(jd, now=now + 61) is None)
eds.begin(jd)
check("editing 不拦截（允许崩溃后直接重试）", eds.gate(jd, now=now + 61) is None)

print("\n== ⑥ --phase edit 放行不清零 ==")
jd = fresh("放行")
eds.fail(jd, "a", now=time.time())
eds.fail(jd, "b", now=time.time())
check("放行前闸门拦截", eds.gate(jd) is not None)
check("--phase edit 强制放行", eds.gate(jd, phase_override="edit") is None)
check("放行不清零计数", eds.load(jd)["consecutive_failures"] == 2)

print("\n== ⑦ 成功清零 ==")
cw.note_edit_result(jd, "放行", True)
st = eds.load(jd)
check("status=done", st["status"] == "done", st)
check("计数清零", st["consecutive_failures"] == 0 and st["attempts"] == 0)
check("成功后闸门放行", eds.gate(jd) is None)
check("succeed 幂等", eds.succeed(jd)["status"] == "done")

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
