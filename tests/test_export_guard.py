# -*- coding: utf-8 -*-
"""回归用例：export 阶段"跳过不得变成功"与"收尾必须有产物证据"。

覆盖：
  A 强制重导（--phase export）遇到已有成品：必须真跑，不得被 _draft_exported 跳过
  B 强制重导但 progress 未 done：不得走完成收尾，按失败报清楚缺什么
  C 非强制 + 已有成品：只跳过——不登记、不 record_video、不弹完成
  D freshness.record_export 成品不存在：写 stale(output_missing)，不写 valid
  E 旧正式名产物冒充本轮：不得算完成
  F 跳过分支不变量（R01）：走了 skip 就只留日志，不 record_video / 不 succeed /
    不 record_export / 不清标记 / 不 note_export_result / 不弹完成
  G 复用判定 fail-closed（R02）：_export_stale / freshness / progress 证据不足
    一律判「未导出」；progress.done 或 freshness valid/legacy 才判已导出
  H legacy 冻结基线（R02）：无 sidecar 的历史产物走白名单放行，不误伤

运行：python tests\\test_export_guard.py（纯临时目录；不碰导出锁/真实剧/真实草稿）
"""
import contextlib
import json
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


def run(jd, plan, run_export, toasts, record_video, notes, *,
        succeeds=None, rec_exports=None, clears=None):
    """统一打桩：不碰真实导出锁/剪映/真实草稿目录，只跑 run_export 的编排逻辑。

    succeeds/rec_exports/clears 可选：收集 es.succeed / freshness.record_export /
    jp.clear_export_stale 的调用，供"跳过分支不变量"断言。
    """
    succeeds = [] if succeeds is None else succeeds
    rec_exports = [] if rec_exports is None else rec_exports
    clears = [] if clears is None else clears
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
            mock.patch.object(cw.jp, "clear_export_stale",
                              side_effect=lambda *a: clears.append(a)), \
            mock.patch.object(cw.jm, "record_video",
                              return_value={"video": {"export_dir": OUT}}), \
            mock.patch.object(cw, "note_export_result") as note, \
            mock.patch.object(cw.es, "succeed",
                              side_effect=lambda *a, **k: succeeds.append((a, k))), \
            mock.patch.object(cw.sr, "_run_export", side_effect=run_export), \
            mock.patch.object(freshness, "record_export",
                              side_effect=lambda *a, **k: rec_exports.append(a)):
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

# ── F：跳过分支不变量（R01）──────────────────────────────────────────
# 只要走了 skip 分支，就只允许留日志；任何"登记成功/完成通知"都算破坏不变量。
print("\n== F 跳过分支不变量：只跳过，不登记不通知 ==")
jd = make_job("guardF")
calls, toasts, recs, notes, succ, rexp, clr = [], [], [], [], [], [], []


def never_run(dn, **kw):
    calls.append(dn)
    return True


rc = run(jd, plan_for(force=False), never_run, toasts, recs, notes,
         succeeds=succ, rec_exports=rexp, clears=clr)
wlog = os.path.join(jd, "watcher.log")
wtext = open(wlog, encoding="utf-8").read() if os.path.isfile(wlog) else ""
job = jm.load_job(os.path.join(jd, "job.json"))
outs = (job.get("video") or {}).get("outputs") or {}
check("F1 返回 True（跳过不是失败）", rc is True, rc)
check("F2 未触发导出", not calls, calls)
check("F3 未 record_video", not recs, recs)
check("F4 未 note_export_result（含 ok=True）", not notes, notes)
check("F5 未 es.succeed", not succ, succ)
check("F6 未 freshness.record_export", not rexp, rexp)
check("F7 未清 _export_stale 标记", not clr, clr)
check("F8 未弹完成通知", not has_done_toast(toasts), toasts)
check("F9 留下 skipped 痕迹", "export skipped" in wtext, wtext[-400:])
check("F10 未 append 进 outputs", not any(
    isinstance(v, dict) and v.get("export_file") for v in outs.values()), outs)

# ── G：复用判定 fail-closed（R02）────────────────────────────────────
print("\n== G 复用判定 fail-closed：证据不足判未导出 ==")


def reset_draft(dn):
    ddir = os.path.join(DRAFTS, dn)
    os.makedirs(ddir, exist_ok=True)
    marker = os.path.join(ddir, "_export_stale.json")
    if os.path.isfile(marker):
        os.remove(marker)
    out = os.path.join(OUT, dn + ".mp4")
    for suf in ("", ".progress.json", ".freshness.json"):
        try:
            os.remove(out + suf)
        except OSError:
            pass
    return out, marker


def exported_raw(dn, *, jd=None, fresh_state="__real__"):
    """只打桩不造文件：直接调 _draft_exported。"""
    stack = [mock.patch.object(cw.pc, "export_mp4_path",
                               side_effect=lambda d: os.path.join(OUT, d + ".mp4")),
             mock.patch.object(cw, "_draft_root", return_value=DRAFTS),
             mock.patch.object(freshness, "job_dir_for_draft", return_value=jd)]
    if fresh_state != "__real__":
        stack.append(mock.patch.object(freshness, "freshness_of",
                                       return_value={"state": fresh_state}))
    with contextlib.ExitStack() as stk:
        for p in stack:
            stk.enter_context(p)
        return cw._draft_exported(dn)


def export_state(dn, *, mp4=True, progress=None, stale=False,
                 fresh_state="__real__", jd=None):
    """造三层证据组合，直接调 _draft_exported（不跑 run_export）。

    fresh_state="__real__" 表示不 patch freshness_of，走真实判定（legacy 用）；
    其余值表示伪造 freshness 三态之一（valid/unknown/...）。
    """
    out, marker = reset_draft(dn)
    if mp4:
        with open(out, "wb") as f:
            f.write(b"m" * 1024)
    if progress is not None:
        with open(out + ".progress.json", "w", encoding="utf-8") as f:
            json.dump({"progress": 100, "done": progress}, f)
    if stale:
        with open(marker, "w", encoding="utf-8") as f:
            json.dump({"at": "测试"}, f)
    return exported_raw(dn, jd=jd, fresh_state=fresh_state)


dn = "剧G复用_第01-3集串流"
check("G1 有 mp4 但无 progress/无 freshness → 未导出（原 fail-open 回归点）",
      export_state(dn) is False)
check("G2 progress.done=true → 已导出",
      export_state(dn, progress=True) is True)
check("G3 progress.done=false → 未导出",
      export_state(dn, progress=False) is False)
check("G4 _export_stale 硬否定优先于 progress.done → 未导出",
      export_state(dn, progress=True, stale=True) is False)
check("G5 mp4 不存在（progress 在）→ 未导出",
      export_state(dn, mp4=False, progress=True) is False)
check("G6 freshness=valid 且无 progress → 已导出",
      export_state(dn, fresh_state="valid", jd=os.path.join(TMP, "fakejob")) is True)

# ── H：legacy 冻结基线（R02）─────────────────────────────────────────
# 无 sidecar 的历史产物必须走冻结基线白名单放行，不能一刀切成未导出。
print("\n== H legacy 冻结基线：无 sidecar 也放行 ==")
dn = "剧H历史_第01-3集串流"
out, _ = reset_draft(dn)
with open(out, "wb") as f:
    f.write(b"m" * 2048)
size = os.path.getsize(out)
draft_dir = os.path.join(DRAFTS, dn)
with open(os.path.join(draft_dir, "draft_content.json"), "w", encoding="utf-8") as f:
    json.dump({"fps": 30, "tracks": [], "materials": {}}, f)
jd = os.path.join(TMP, "guardH", "cover_job")
os.makedirs(jd, exist_ok=True)
with open(os.path.join(jd, "job.json"), "w", encoding="utf-8") as f:
    json.dump({"job_id": "guardH-legacy", "revision": 1,
               "video": {"outputs": {"a": {
                   "state": "exported", "draft": draft_dir,
                   "export_file": out, "size_bytes": size}}}}, f)
entries = [{"job": jd, "job_id": "guardH-legacy", "version": "a", "size_bytes": size}]
base = {"schema": 1, "kind": freshness.BASELINE_KIND, "entries": entries,
        "entries_digest": freshness.baseline_entries_digest(entries)}
base_path = os.path.join(TMP, "baseline_H.json")
with open(base_path, "w", encoding="utf-8") as f:
    json.dump(base, f)
nonexistent = os.path.join(TMP, "baseline_missing.json")
with mock.patch.dict(os.environ, {freshness.BASELINE_ENV: base_path}):
    st = freshness.freshness_of(jd, "a", consumer="scheduler")
    got = exported_raw(dn, jd=jd)          # 无 sidecar、无 progress
check("H1 命中基线 → freshness=legacy", st.get("state") == "legacy", st)
check("H2 无 sidecar/无 progress 仍判已导出（白名单）", got is True)
with mock.patch.dict(os.environ, {freshness.BASELINE_ENV: nonexistent}):
    st2 = freshness.freshness_of(jd, "a", consumer="scheduler")
    got2 = exported_raw(dn, jd=jd)
check("H3 基线缺失 → evidence_missing", st2.get("state") == "evidence_missing", st2)
check("H4 基线缺失且无 progress → 未导出（不误放行）", got2 is False)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
