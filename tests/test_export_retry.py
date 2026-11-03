"""回归用例：导出失败退避 / 暂停闸门（C）+ 冷启动参数贯通（D/F）。

运行：python tests\\test_export_retry.py（无第三方依赖）
"""
import inspect, os, shutil, sys, tempfile, time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import cover_watch as cw  # noqa: E402
import series_runner as sr  # noqa: E402

cw.toast = lambda *a, **k: None  # 用例里不打桌面通知

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "export_retry_test")
shutil.rmtree(TMP, ignore_errors=True)
JD = os.path.join(TMP, "测试剧", "cover_job")
os.makedirs(JD, exist_ok=True)

print("\n== 1. 初始可导 ==")
check("初始无退避记录 → 可导", cw.retry_gate(JD) is None)

print("\n== 2. 失败 → 指数退避 ==")
expected = [60, 120, 240, 480, 960, 1800, 1800]
for i, delay in enumerate(expected, start=1):
    cw.note_export_result(JD, "测试剧", False, pause_after=99)  # 本节不触发暂停
    raw = cw.read_retry(JD)
    left = int(raw.get("next_ts") - time.time())
    check(f"第 {i} 次失败 fails={i}", raw.get("fails") == i, raw)
    check(f"第 {i} 次退避≈{delay}s", abs(left - delay) <= 5, f"left={left}")
    check(f"第 {i} 次失败后闸门拦截", (cw.retry_gate(JD) or "").startswith("导出失败退避中"))

print("\n== 3. 连续失败 → 暂停 + 人工放行 ==")
cw.note_export_result(JD, "测试剧", False, pause_after=4)
raw = cw.read_retry(JD)
check("超限后 paused=true", bool(raw.get("paused")) and raw.get("next_ts") == 0, raw)
check("暂停原因含人工处置提示", "已暂停" in (cw.retry_gate(JD) or ""))
check("--phase export 可强制放行", cw.retry_gate(JD, "export") is None)

print("\n== 4. 成功清零 ==")
cw.note_export_result(JD, "测试剧", True)
raw = cw.read_retry(JD)
check("成功后 fails=0", raw.get("fails") == 0, raw)
check("成功后闸门放行", cw.retry_gate(JD) is None)

print("\n== 5. 冷启动/超时参数贯通 ==")
sig_run = inspect.signature(sr._run_export)
check("_run_export 接受 cold_start", "cold_start" in sig_run.parameters)
check("_run_export 默认 timeout=1800", sig_run.parameters["timeout"].default == 1800)
check("进度心跳无文件时为 0",
      sr._progress_mtime(os.path.join(TMP, "L", "剧", "剧.mp4")) == 0.0)

cw.find_stream_draft = lambda _name: "测试剧串流"  # 不依赖本机真实草稿库
job = {"workflow": {"mode": "full", "cut_s": None, "derive": []}, "video": {}}
plan_on = cw.build_plan(JD, job, {"cold_start": True}, pending_edit=0)
plan_off = cw.build_plan(JD, job, {"cold_start": False}, pending_edit=0)
check("cold_start=true 时导出命令带 --cold-start",
      plan_on["action"] == "export" and "--cold-start" in plan_on["cmd"])
check("cold_start=false 时导出命令不带 --cold-start",
      plan_off["action"] == "export" and "--cold-start" not in plan_off["cmd"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
