# -*- coding: utf-8 -*-
"""回归用例：本地执行链未捕获异常必须登记失败（防 30 秒一轮的永久空转）。

背景（2026-11-06）：watcher.log 出现 9 次 `chain 异常: name '_write_lock' is not
defined`，间隔约 30 秒，.edit_state.json 的 attempts 仍为 1、consecutive_failures
不变——异常在 note_edit_result 之前掀翻整轮，计数/退避/阈值暂停全部被跳过。

覆盖：
  ① chain 异常后 attempts 递增、状态 retrying、consecutive_failures=1；
  ② next_retry_at 按 60s → 120s 退避推进；
  ③ 达到阈值（3 次）置 paused；
  ④ 退避窗口内下一轮不再立即重试（cmd_watch --once 第二轮不发起执行）；
  ⑤ 本次执行已登记过结果时不重复计数；
  ⑥ run_export 吞掉 export_config 异常时同样登记失败（不再 5s 空转）。

运行：python tests\\test_chain_exception.py（临时目录，不真导出）
"""
import json
import os
import shutil
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import cover_watch as cw   # noqa: E402
import edit_state as eds   # noqa: E402
import export_state as es  # noqa: E402

cw.toast = lambda *a, **k: None
cw.pc.get_watch_config = lambda: {}   # 默认 edit_pause_after=3，避免受本机 presets 影响

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _status(jd):
    with open(os.path.join(jd, eds.STATE_NAME), "r", encoding="utf-8") as f:
        return json.load(f)


def _expire_backoff(jd):
    """把退避窗口拨到过去，模拟「时间到了、下一轮又执行一次」。"""
    st = _status(jd)
    st["next_retry_at"] = 0
    with open(os.path.join(jd, eds.STATE_NAME), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def _simulate_round(jd, err):
    """模拟 watcher 一轮：执行前快照 → begin（attempts+1）→ 链内抛异常 → 登记失败。"""
    before = cw._round_state_brief(jd, "edit")
    eds.begin(jd, phase="edit")
    cw._record_chain_failure(jd, "测试剧", "edit", {}, RuntimeError(err), before)
    return _status(jd)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25


def make_fixture(name):
    jd = os.path.join(TMP, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    with open(os.path.join(jd, "02_titles", "titles.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"id": "T3", "title": "标题三"}]}, f, ensure_ascii=False)
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"slot": 1}, {"slot": 2}, {"slot": 3}, {"slot": "vertical"}]}, f)
    for stem in ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"):
        with open(os.path.join(jd, "04_covers", stem + ".png"), "wb") as f:
            f.write(PNG)
    job = jm.build_job(name)
    job["stage"] = "covers_ready"
    job["titles"]["gate"] = "done"
    job["titles"]["selected"] = [
        {"slot": 1, "title_id": "T3", "title": "标题三"},
        {"slot": 2, "title_id": "T3", "title": "标题三"},
        {"slot": 3, "title_id": "T3", "title": "标题三"},
    ]
    job["titles"]["selected_by"] = "user"
    job["cleanup"]["status"] = "blocked"
    for c in job["cover_map"]:
        c["status"] = "done"
        c["image_sha256"] = jm.sha256_file(os.path.join(jd, c["file"]))
    job["vertical_cover"]["status"] = "done"
    job["vertical_cover"]["image_sha256"] = jm.sha256_file(
        os.path.join(jd, job["vertical_cover"]["file"]))
    jm.save_job(job, jd)
    return jd


TMP = os.path.join(tempfile.gettempdir(), "chain_exception_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

print("\n== ①②③ chain 异常按退避推进并在阈值暂停 ==")
jd = make_fixture("退避")
st1 = _simulate_round(jd, "name '_write_lock' is not defined")
check("attempts=1（begin 已递增）", st1["attempts"] == 1, st1["attempts"])
check("状态 retrying", st1["status"] == "retrying", st1["status"])
check("consecutive_failures=1", st1["consecutive_failures"] == 1, st1["consecutive_failures"])
check("failure_class=chain_exception", st1["failure_class"] == "chain_exception",
      st1["failure_class"])
check("last_error 记录 chain 异常", "chain 异常" in (st1["last_error"] or ""), st1["last_error"])
delay1 = float(st1["next_retry_at"]) - time.time()
check("首轮退避约 60s", 55 <= delay1 <= 65, round(delay1, 1))
check("退避窗口内 gate 拦截", eds.gate(jd) is not None, eds.gate(jd))

_expire_backoff(jd)
st2 = _simulate_round(jd, "boom2")
check("attempts=2", st2["attempts"] == 2, st2["attempts"])
check("consecutive_failures=2", st2["consecutive_failures"] == 2, st2["consecutive_failures"])
delay2 = float(st2["next_retry_at"]) - time.time()
check("第二轮退避约 120s", 115 <= delay2 <= 125, round(delay2, 1))

_expire_backoff(jd)
st3 = _simulate_round(jd, "boom3")
check("第 3 次达到阈值 paused", st3["status"] == "paused", st3["status"])
check("consecutive_failures=3", st3["consecutive_failures"] == 3, st3["consecutive_failures"])
check("paused 后 next_retry_at=0", float(st3["next_retry_at"]) == 0, st3["next_retry_at"])
check("paused 后 gate 拦截", eds.gate(jd) is not None, eds.gate(jd))

print("\n== ⑤ 本次执行已登记结果 → 不重复计数 ==")
jd5 = make_fixture("幂等")
before = cw._round_state_brief(jd5, "edit")
eds.begin(jd5, phase="edit")
cw.note_edit_result(jd5, "测试剧", False, error="validate 失败", failure_class="unknown")
cw._record_chain_failure(jd5, "测试剧", "edit", {}, RuntimeError("链内再抛"), before)
st5 = _status(jd5)
check("已登记的失败不重复计数", st5["consecutive_failures"] == 1, st5["consecutive_failures"])
check("次数仍为 1", int(st5["consecutive_failures"]) == 1, st5["consecutive_failures"])

print("\n== ⑥ run_export 吞异常时登记失败 ==")
jd6 = make_fixture("导出配置")
with open(os.path.join(jd6, "export_config.json"), "w", encoding="utf-8") as f:
    json.dump({"cut_s": -1}, f)
ok6 = cw.run_export(jd6, {"name": "测试剧", "draft": "测试草稿"}, {
    "retry_pause_after": 5, "export_stall_timeout": 1800, "cold_start": False,
}, dry_run=False, verify=False, no_derive=True)
st6 = es.load(jd6)
check("run_export 返回 False", ok6 is False, ok6)
check("导出状态 retrying", st6["status"] == "retrying", st6["status"])
check("导出失败计数=1", st6["consecutive_failures"] == 1, st6["consecutive_failures"])
check("导出 failure_class 记录", st6["failure_class"] == "export_config_error",
      st6["failure_class"])

print("\n== ④ watcher 一轮内 chain 异常 → 计数；下一轮被退避拦住 ==")
jd4 = make_fixture("watcher链路")
cfg_path = os.path.join(TMP, "watch.json")
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump({"roots": [], "jobs": [], "settle": 0, "interval": 0,
               "new_job_grace_min": 0, "edit_pause_after": 3,
               "export_policy": "immediate", "hold": False}, f)


def _boom_run_edit(job_dir, plan, *, dry_run):
    eds.begin(job_dir, phase="edit")
    raise RuntimeError("模拟 pipeline start 之后抛异常: name '_write_lock' is not defined")


_real_run_edit = cw.run_edit
cw.run_edit = _boom_run_edit
try:
    rc1 = cw.main(["--job", jd4, "--config", cfg_path, "--once"])
finally:
    cw.run_edit = _real_run_edit

st4 = _status(jd4)
check("watcher 返回 0", rc1 == 0, rc1)
check("attempts=1", st4["attempts"] == 1, st4["attempts"])
check("consecutive_failures=1", st4["consecutive_failures"] == 1, st4["consecutive_failures"])
check("状态 retrying", st4["status"] == "retrying", st4["status"])
with open(os.path.join(jd4, "watcher.log"), "r", encoding="utf-8") as f:
    log4 = f.read()
check("日志有 chain 异常", "chain 异常" in log4, log4)
check("日志有失败计数行", "edit 失败第 1 次" in log4, log4)

# 退避窗口内再跑一轮：不应再次发起执行（attempts / 失败数都不涨）
cw.run_edit = _boom_run_edit
try:
    rc2 = cw.main(["--job", jd4, "--config", cfg_path, "--once"])
finally:
    cw.run_edit = _real_run_edit

st4b = _status(jd4)
check("第二轮返回 0", rc2 == 0, rc2)
check("第二轮未执行（attempts 不变）", st4b["attempts"] == 1, st4b["attempts"])
check("第二轮未重复计数", st4b["consecutive_failures"] == 1, st4b["consecutive_failures"])

print("\n== ⑦ last_error 改结构化异常（不再记录日志尾部）==")


def _boom_here():
    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")


try:
    _boom_here()
except RuntimeError as e:
    brief = cw._exception_brief(e, where="测试链路")
check("含异常类型", "RuntimeError" in brief, brief)
check("含首行 message", "cublas64_12.dll" in brief, brief)
check("含发生位置（函数:行号）", "_boom_here:" in brief, brief)
check("含调用位置说明", "测试链路" in brief, brief)


class _MultiLine(Exception):
    pass


brief_ml = cw._exception_brief(_MultiLine("第一行\n第二行"), where="多行")
check("多条 message 只取首行", "第二行" not in brief_ml, brief_ml)
check("未抛出的异常也能生成摘要", brief_ml.startswith("_MultiLine: 第一行"), brief_ml)

# _pipeline_failure_brief：过滤线程噪音，保留真实错误与「缺少目录」自愈线索
print("\n== ⑧ batch_runner rc!=0 取有效错误行，剔除解码噪音 ==")
jd8 = make_fixture("结构化日志")
logp = os.path.join(jd8, "06_publish", "pipeline.log")
with open(logp, "w", encoding="utf-8") as f:
    f.write("\n".join([
        "===== 2026-11-07T00:00:00 cover_watch edit 阶段 =====",
        "Exception in thread Thread-3 (_readerthread):",
        "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbc in position 124",
        "  ✗ ffmpeg failed: Conversion failed!",
        "Traceback (most recent call last):",
        "RuntimeError: Library cublas64_12.dll is not found or cannot be loaded",
        "Exception in thread Thread-4 (_readerthread):",
        "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xa8 in position 266",
    ]) + "\n")
pf = cw._pipeline_failure_brief(jd8, 1)
check("以退出码开头", pf.startswith("batch_runner rc=1"), pf)
check("保留真实错误行", "cublas64_12.dll" in pf or "Conversion failed" in pf, pf)
check("剔除解码噪音", "UnicodeDecodeError" not in pf and "_readerthread" not in pf, pf)

with open(logp, "w", encoding="utf-8") as f:
    f.write("  ✗ 缺少目录: 06_publish/receipts\n")
    f.write("UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbc\n")
pf2 = cw._pipeline_failure_brief(jd8, 2)
check("缺目录自愈线索仍保留", "缺少目录: 06_publish/receipts" in pf2, pf2)

print("\n== ⑨ chain 异常登记 last_error 不含日志噪音 ==")
jd9 = make_fixture("结构化状态")
with open(os.path.join(jd9, "06_publish", "pipeline.log"), "w", encoding="utf-8") as f:
    f.write("UnicodeDecodeError: 'utf-8' codec can't decode byte 0xbc\n" * 20)
before9 = cw._round_state_brief(jd9, "edit")
eds.begin(jd9, phase="edit")
try:
    _boom_here()
except RuntimeError as e:
    cw._record_chain_failure(jd9, "测试剧", "edit", {}, e, before9)
st9 = _status(jd9)
check("last_error 含异常类型", "RuntimeError" in st9["last_error"], st9["last_error"])
check("last_error 含发生位置", "_boom_here:" in st9["last_error"], st9["last_error"])
check("last_error 不含解码噪音", "UnicodeDecodeError" not in st9["last_error"],
      st9["last_error"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
