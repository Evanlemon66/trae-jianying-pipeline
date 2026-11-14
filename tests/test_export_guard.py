# -*- coding: utf-8 -*-
"""回归用例：export 阶段"跳过不得变成功"与"收尾必须有产物证据"。

覆盖：
  A 强制重导（--phase export）遇到已有成品：必须真跑，不得被 _draft_exported 跳过
  B 强制重导但 progress 未 done：不得走完成收尾，按失败报清楚缺什么
  C 非强制 + 已有成品：只跳过——不登记、不 record_video、不弹完成
  D freshness.record_export 成品不存在：写 stale(output_missing)，不写 valid

运行：python tests\\test_export_guard.py（纯临时目录；不碰导出锁/真实剧/真实草稿）
"""
import os
import shutil
import sys
import tempfile
import time
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job_manifest as jm  # noqa: E402
import cover_watch as cw  # noqa: E402
import freshness  # noqa: E402
from bundle_fixture import make_bundle  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "export_guard_test")
shutil.rmtree(TMP, ignore_errors=True)
OUT = os.path.join(TMP, "exports")
DRAFTS = os.path.join(TMP, "drafts")
os.makedirs(OUT, exist_ok=True)
os.makedirs(DRAFTS, exist_ok=True)
DRAFT_NAME = "剧G_第01-3集串流"


def make_job(tag):
    jd = os.path.join(TMP, tag, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(tag)
    job["stage"] = "video_ready"
    jm.save_job(job, jd)
    return jd


def plan_for(force):
    p = {"action": "export", "name": DRAFT_NAME,
         "draft": os.path.join(DRAFTS, DRAFT_NAME),
         "profiles": ["a"], "export_targets": [DRAFT_NAME]}
    if force:
        p["force_reexport"] = True
    return p


def run(jd, plan, run_export, toasts, record_video, notes):
    """统一打桩：不碰真实导出锁/剪映/真实草稿目录，只跑 run_export 的编排逻辑。"""
    with mock.patch.object(cw.pc, "export_mp4_path",
                           side_effect=lambda dn: os.path.join(OUT, dn + ".mp4")), \
            mock.patch.object(cw, "_draft_root", return_value=DRAFTS), \
            mock.patch.object(cw, "_draft_exported", return_value=True), \
            mock.patch.object(cw, "toast", side_effect=lambda *a, **k: toasts.append(a)), \
            mock.patch.object(cw.jm, "require_export_allowed", return_value=None), \
            mock.patch.object(cw.export_lock, "acquire", return_value=True), \
            mock.patch.object(cw.export_lock, "release", return_value=None), \
            mock.patch.object(cw, "_write_lock", return_value=None), \
            mock.patch.object(cw, "_relaunch_clear", return_value=None), \
            mock.patch.object(cw.jp, "export_is_stale", return_value=False), \
            mock.patch.object(cw.jp, "clear_export_stale", return_value=None), \
            mock.patch.object(cw.jm, "record_video",
                              return_value={"video": {"export_dir": OUT}}), \
            mock.patch.object(cw, "note_export_result") as note, \
            mock.patch.object(cw.sr, "_run_export", side_effect=run_export), \
            mock.patch.object(freshness, "record_export", return_value=True):
        rc = cw.run_export(jd, plan, {"retry_pause_after": 3},
                          dry_run=False, verify=False, no_derive=True)
        record_video.extend(cw.jm.record_video.call_args_list)
        notes.extend(note.call_args_list)
    return rc


def has_done_toast(toasts):
    return any("mp4 导出完成" in str(t[0]) for t in toasts)


# ── A：强制重导 + 已有成品 → 必须真跑并正常收尾 ──────────────────────
print("\n== A 强制重导遇已有成品：真跑，不跳过 ==")
jd = make_job("guardA")
calls, toasts, recs, notes = [], [], [], []


def full_export(dn, **kw):
    calls.append(dn)
    make_bundle(OUT, dn)
    return True


rc = run(jd, plan_for(force=True), full_export, toasts, recs, notes)
check("A1 进入真实导出（未被跳过）", len(calls) == 1, calls)
check("A2 返回成功", rc is True, rc)
check("A3 弹了完成通知", has_done_toast(toasts), toasts)
check("A4 登记了成品", len(recs) == 1, len(recs))
check("A5 无失败登记", all(n.args[2] is not False for n in notes), notes)
done_text = " ".join(str(t[1]) for t in toasts if "mp4 导出完成" in str(t[0]))
check("A6 通知带正式名路径", os.path.join(OUT, DRAFT_NAME + ".mp4") in done_text, done_text)
check("A7 通知带字节数", "（10240 字节）" in done_text, done_text)

# ── B：强制重导但 progress 未 done → 不得收尾成功 ────────────────────
print("\n== B 缺 progress.done：按失败处理 ==")
jd = make_job("guardB")
calls, toasts, recs, notes = [], [], [], []


def half_export(dn, **kw):
    calls.append(dn)
    make_bundle(OUT, dn, progress_done=False)   # mp4/封面在，done=false
    return True


rc = run(jd, plan_for(force=True), half_export, toasts, recs, notes)
check("B1 返回失败", rc is False, rc)
check("B2 未弹完成通知", not has_done_toast(toasts), toasts)
check("B3 未登记成品", not recs, recs)
check("B4 失败分类 artifacts_missing",
      any((n.kwargs or {}).get("failure_class") == "artifacts_missing" for n in notes), notes)
check("B5 失败原因说明缺什么",
      any("done" in str((n.kwargs or {}).get("error", "")) for n in notes), notes)

# ── C：非强制 + 已有成品 → 只跳过 ────────────────────────────────────
print("\n== C 非强制已有成品：只跳过 ==")
jd = make_job("guardC")
calls, toasts, recs, notes = [], [], [], []


def should_not_run(dn, **kw):
    calls.append(dn)
    return True


rc = run(jd, plan_for(force=False), should_not_run, toasts, recs, notes)
check("C1 未触发导出", not calls, calls)
check("C2 未登记成品", not recs, recs)
check("C3 未弹完成通知", not has_done_toast(toasts), toasts)
check("C4 未记成功", not notes, notes)

# ── D：freshness 成品缺失 → stale(output_missing) ────────────────────
print("\n== D 成品不存在时写 stale 边 ==")
jd = make_job("guardD")
missing = os.path.join(TMP, "never", "不存在.mp4")
ok = freshness.record_export(jd, "a", DRAFT_NAME, missing)
job = jm.load_job(os.path.join(jd, "job.json"))
e2 = ((job.get("freshness") or {}).get("edges") or {}).get(freshness.EDGE_E2) or {}
check("D1 返回 False", ok is False, ok)
check("D2 边状态 stale", e2.get("state") == "stale", e2)
check("D3 原因为 output_missing", e2.get("reason") == "output_missing", e2)
check("D4 未写出空 output", not e2.get("output"), e2)

# ── E：旧正式名产物冒充本轮结果 → 不得算完成 ─────────────────────────
print("\n== E 旧成品冒充本轮：按失败处理 ==")
jd = make_job("guardE")
calls, toasts, recs, notes = [], [], [], []
here = os.path.join(OUT, DRAFT_NAME + ".mp4")
make_bundle(OUT, DRAFT_NAME)                 # 旧成品已在，且 progress.done=true
old = time.time() - 3600
os.utime(here, (old, old))
os.utime(here + ".progress.json", (old, old))


def noop_export(dn, **kw):
    calls.append(dn)
    return True                               # 返回成功但没有产出/没有 --move


rc = run(jd, plan_for(force=True), noop_export, toasts, recs, notes)
check("E1 返回失败", rc is False, rc)
check("E2 未弹完成通知", not has_done_toast(toasts), toasts)
check("E3 未登记成品", not recs, recs)
check("E4 指出正式名本轮未更新",
      any("正式名" in str((n.kwargs or {}).get("error", "")) for n in notes), notes)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
