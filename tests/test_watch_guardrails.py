# -*- coding: utf-8 -*-
"""回归用例：watcher 调度侧四项改造（lib/cover_watch.py + lib/edit_state.py）。

覆盖：
  ① reopen：published/done → 退回 covers_ready、清空 video 标记、记 history、删 .export_state.json；
           archived / 非交互无 --yes / 不需要重导 / --to 不早于当前 / 活锁 / exporting 一律拒绝
  ② build_plan：export_state=cleaned 视为完成；exported 只等清理不重复导出；
                --phase 强制路径不受影响；after_edit_queue_drained 语义不变；archived 仍硬拦
  ③ 编辑看门狗：新鲜心跳不动；超时且 pid 不存在 → 记失败不 kill；
                超时 pid 存活但归属校验不过 → 不 kill、不记失败、不放行（保持 editing）；
                归属通过 → kill；自建子进程超时 → 直接 kill，不查归属文本；
                无心跳时用状态文件 mtime 兜底
  ④ 新人宽限期：新剧拦；job.json 被改放行；grace=0 关闭；有执行 history 放行；超期放行

运行：python tests\\test_watch_guardrails.py（纯临时目录，不碰真实剧、不 kill 真实进程）
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import edit_state as eds  # noqa: E402
import cover_watch as cw  # noqa: E402

fails = []
toasts = []
cw.toast = lambda title, msg, icon="info": toasts.append((title, msg))

TMP = os.path.join(tempfile.gettempdir(), "watch_guardrails_test")
shutil.rmtree(TMP, ignore_errors=True)
ROOT = os.path.join(TMP, "root", "短剧")
os.makedirs(ROOT, exist_ok=True)


def check(name, cond, extra=""):
    print(f"  {'OK' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def make_job(name, *, stage="covers_ready", status=None, export_state=None):
    jd = os.path.join(ROOT, name, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(name)
    job["stage"] = stage
    job["video"]["status"] = status
    job["video"]["export_state"] = export_state
    jm.save_job(job, jd)
    return jd


def read_job(jd):
    return jm.load_job(os.path.join(jd, "job.json"))


def reopen(jd, *, to="covers_ready", yes=True):
    return cw.cmd_reopen(argparse.Namespace(job=jd, to=to, yes=yes))


print("\n== ① reopen ==")
jd = make_job("重导成功", stage="published", status="done", export_state="exported")
with open(os.path.join(jd, ".export_state.json"), "w", encoding="utf-8") as f:
    json.dump({"status": "done", "attempts": 2}, f)
rc = reopen(jd)
job = read_job(jd)
check("published/done 允许重导（rc=0）", rc == 0, rc)
check("stage 退回 covers_ready", job["stage"] == "covers_ready", job["stage"])
check("video.status 已清空", job["video"]["status"] is None, job["video"]["status"])
check("video.export_state 已清空", job["video"]["export_state"] is None)
check("history 记 reopen 事件", (job["history"][-1].get("event") == "reopen"), job["history"][-1])
check("revision 已递增", job["revision"] == 2, job["revision"])
check(".export_state.json 已删除", not os.path.exists(os.path.join(jd, ".export_state.json")))

jd = make_job("重导归档拒绝", stage="archived", status="done", export_state="cleaned")
rc = reopen(jd)
check("archived 拒绝（rc=1）", rc == 1, rc)
check("archived stage 未变", read_job(jd)["stage"] == "archived")
check("archived 无 reopen history",
      not any(h.get("event") == "reopen" for h in read_job(jd)["history"]))

jd = make_job("重导非交互", stage="video_ready", status="done")
with mock.patch("sys.stdin") as m_in:
    m_in.isatty.return_value = False
    rc = reopen(jd, yes=False)
check("非交互无 --yes 拒绝（rc=1）", rc == 1, rc)
check("拒绝后 stage 未变", read_job(jd)["stage"] == "video_ready")
check("拒绝后无 reopen history",
      not any(h.get("event") == "reopen" for h in read_job(jd)["history"]))

jd = make_job("重导不需要", stage="covers_ready", status=None)
rc = reopen(jd)
check("不需要重导的状态拒绝（rc=1）", rc == 1, rc)

jd = make_job("重导目标非法", stage="published", status="done")
rc = reopen(jd, to="published")
check("--to 不早于当前拒绝（rc=1）", rc == 1, rc)
check("--to 非法后 stage 未变", read_job(jd)["stage"] == "published")

jd = make_job("重导活锁", stage="video_ready", status="done")
with open(os.path.join(jd, ".watcher.lock"), "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid()}, f)
rc = reopen(jd)
check("活锁拒绝（rc=1）", rc == 1, rc)
check("活锁拒绝后 stage 未变", read_job(jd)["stage"] == "video_ready")
os.remove(os.path.join(jd, ".watcher.lock"))

jd = make_job("重导导出中", stage="video_ready", status="done")
with open(os.path.join(jd, ".export_state.json"), "w", encoding="utf-8") as f:
    json.dump({"status": "exporting"}, f)
rc = reopen(jd)
check("导出进行中拒绝（rc=1）", rc == 1, rc)
check("导出中拒绝后 stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ② build_plan 接入 export_state ==")


def plan(jd, *, phase=None, pending_edit=0, draft=None, policy="immediate"):
    job = read_job(jd)
    with mock.patch.object(cw, "find_stream_draft", return_value=draft), \
            mock.patch.object(cw.pc, "export_mp4_path", return_value=os.path.join(TMP, "x.mp4")), \
            mock.patch.object(cw.pc, "get_export_dir", return_value=TMP):
        return cw.build_plan(jd, job, {"export_policy": policy}, pending_edit=pending_edit,
                             phase_override=phase, notify=False, write_log=False)


jd = make_job("计划已清理", export_state="cleaned")
p = plan(jd)
check("cleaned → 不调度", p["action"] == "blocked" and "cleaned" in p["reason"], p)
check("cleaned → 无可执行命令", not p.get("cmd"))

jd = make_job("计划已导出", export_state="exported")
p = plan(jd)
check("exported → 等清理不重复导出",
      p["action"] == "blocked" and "已导出" in p["reason"], p)
check("exported → 无可执行命令", not p.get("cmd"))

jd = make_job("计划强制编辑", export_state="cleaned")
p = plan(jd, phase="edit")
check("--phase edit 强制路径不受影响", p["action"] == "edit", p)

draft_dir = os.path.join(TMP, "drafts", "计划强制导出串流")
os.makedirs(draft_dir, exist_ok=True)
p = plan(jd, phase="export", draft=draft_dir)
check("--phase export 强制路径不受影响", p["action"] == "export", p)

jd = make_job("计划队列闸门", stage="covers_ready")
p = plan(jd, pending_edit=2, draft=draft_dir, policy="after_edit_queue_drained")
check("after_edit_queue_drained 语义不变（有队列则挡）",
      p["action"] == "blocked" and "2 部待剪辑" in p["reason"], p)
job = read_job(jd)
job["video"]["export_state"] = "exported"
jm.save_job(job, jd)
p = plan(jd, pending_edit=2, draft=draft_dir, policy="after_edit_queue_drained")
check("exported 优先于编辑队列闸门（不重复导出）",
      p["action"] == "blocked" and "已导出" in p["reason"], p)

jd_arch = make_job("计划归档硬拦", stage="archived", status="done")
ok, reasons = cw.check_trigger(jd_arch)
check("archived 仍最先硬拦", ok is False and any("archived" in r for r in reasons), reasons)

print("\n== ③ 编辑看门狗 ==")
CFG = {"edit_stall_timeout_s": 60}


def editing_job(name, pid, stale_s=0):
    jd = make_job(name)
    eds.begin(jd, pid=pid, now=time.time() - stale_s)
    return jd


with mock.patch.object(cw.pc, "get_watch_config", return_value={}), \
        mock.patch.object(cw, "_pid_alive", return_value=True), \
        mock.patch.object(cw, "_pid_owned_by_job", return_value=True), \
        mock.patch.object(cw, "_kill_tree") as kill:
    jd = editing_job("看门狗新鲜", pid=101, stale_s=0)
    cw._reap_stalled_edit(jd, CFG, name="看门狗新鲜")
    check("新鲜心跳不动", eds.peek(jd)["status"] == "editing", eds.peek(jd))
    check("新鲜心跳不 kill", not kill.called)

with mock.patch.object(cw.pc, "get_watch_config", return_value={}), \
        mock.patch.object(cw, "_pid_alive", return_value=False), \
        mock.patch.object(cw, "_kill_tree") as kill:
    jd = editing_job("看门狗进程已死", pid=202, stale_s=120)
    cw._reap_stalled_edit(jd, CFG, name="看门狗进程已死")
    st = eds.peek(jd)
    check("超时且 pid 不存在 → 记失败退避", st["status"] == "retrying", st)
    check("超时且 pid 不存在 → 不 kill", not kill.called)
    check("失败分类 edit_stalled", st["failure_class"] == "edit_stalled", st)

with mock.patch.object(cw.pc, "get_watch_config", return_value={}), \
        mock.patch.object(cw, "_pid_alive", return_value=True), \
        mock.patch.object(cw, "_pid_owned_by_job", return_value=False), \
        mock.patch.object(cw, "_kill_tree") as kill:
    jd = editing_job("看门狗归属不符", pid=303, stale_s=120)
    lock_fp = os.path.join(jd, ".watcher.lock")
    with open(lock_fp, "w", encoding="utf-8") as f:
        json.dump({"pid": 303}, f)
    rc = cw._reap_stalled_edit(jd, CFG, name="看门狗归属不符")
    st = eds.peek(jd)
    check("pid 存活但归属不符 → 不 kill", not kill.called)
    check("归属不符不记失败（仍 editing）", st["status"] == "editing", st)
    check("归属不符不动失败计数", st["consecutive_failures"] == 0, st)
    check("归属不符不放行（返回 None）", rc is None, rc)
    check("归属不符保留 .watcher.lock", os.path.isfile(lock_fp))
    check("归属不符写警告日志", "本轮不记失败" in
          open(os.path.join(jd, "watcher.log"), encoding="utf-8").read())

with mock.patch.object(cw.pc, "get_watch_config", return_value={}), \
        mock.patch.object(cw, "_pid_alive", return_value=True), \
        mock.patch.object(cw, "_pid_owned_by_job", return_value=True), \
        mock.patch.object(cw, "_kill_tree") as kill:
    jd = editing_job("看门狗归属通过", pid=404, stale_s=120)
    cw._reap_stalled_edit(jd, CFG, name="看门狗归属通过")
    check("归属校验通过 → kill 一次", kill.call_count == 1, kill.call_count)
    check("kill 目标为状态里的 pid", kill.call_args and kill.call_args[0][0] == 404)
    check("kill 后记失败", eds.peek(jd)["status"] == "retrying")

with mock.patch.object(cw.pc, "get_watch_config", return_value={}), \
        mock.patch.object(cw, "_pid_alive", return_value=False):
    jd = make_job("看门狗无心跳兜底")
    eds._atomic_write(jd, dict(eds._default(), status="editing", pid=505, heartbeat_at=0))
    os.utime(os.path.join(jd, eds.STATE_NAME), (time.time() - 120, time.time() - 120))
    cw._reap_stalled_edit(jd, CFG, name="看门狗无心跳兜底")
    check("无心跳用状态文件 mtime 兜底（判卡死）", eds.peek(jd)["status"] == "retrying",
          eds.peek(jd))

with mock.patch.object(cw.pc, "get_watch_config", return_value={}), \
        mock.patch.object(cw, "_pid_owned_by_job") as owned, \
        mock.patch.object(cw, "_kill_tree") as kill:
    jd = editing_job("看门狗超时窗口", pid=606, stale_s=0)
    proc = mock.MagicMock()
    proc.pid = 606
    proc.wait.side_effect = subprocess.TimeoutExpired("batch_runner", 10)
    os.makedirs(os.path.join(jd, "06_publish"), exist_ok=True)
    open(os.path.join(jd, "06_publish", "pipeline.log"), "a", encoding="utf-8").close()
    clock = {"t": time.time()}

    def fake_time():
        clock["t"] += 1.0
        return clock["t"]

    with mock.patch.object(cw, "_edit_stall_timeout", return_value=0.5), \
            mock.patch("time.time", side_effect=fake_time):
        rc = cw._wait_edit_with_watchdog(jd, proc, "看门狗超时窗口")
    check("看门狗超时返回 -999（避免调用方重复计数）", rc == -999, rc)
    check("自建子进程超时直接 kill 一次", kill.call_count == 1, kill.call_count)
    check("kill 目标为子进程 pid", kill.call_args and kill.call_args[0][0] == 606)
    check("不再走归属文本校验", not owned.called, owned.called)
    check("看门狗已记一次失败", eds.peek(jd)["status"] == "retrying", eds.peek(jd))

check("_pid_owned_by_job(0) 一律 False", cw._pid_owned_by_job(0, ROOT) is False)

print("\n== ④ 新人宽限期 ==")


def grace(jd, cfg, now=None):
    return cw._new_job_in_grace(jd, read_job(jd), cfg, now=now)


jd = make_job("宽限新剧")
job = read_job(jd)
created = cw._job_created_ts(jd, job)
os.utime(os.path.join(jd, "job.json"), (created, created))
check("新剧在宽限期内 → 跳过", grace(jd, {"new_job_grace_min": 10}, now=created + 60) is True)
check("just past 宽限 → 放行",
      grace(jd, {"new_job_grace_min": 10}, now=created + 11 * 60) is False)
check("grace=0 关闭", grace(jd, {"new_job_grace_min": 0}, now=created + 10) is False)

job = read_job(jd)
job["history"].append({"at": jm.now_iso(), "event": "ingest"})
jm.save_job(job, jd)
check("已有执行 history → 放行", grace(jd, {"new_job_grace_min": 10}, now=created + 60) is False)

jd = make_job("宽限被改过")
job = read_job(jd)
created = cw._job_created_ts(jd, job)
jm.save_job(job, jd)
os.utime(os.path.join(jd, "job.json"), (created + 30, created + 30))
check("job.json 被改过（mtime 变新）→ 放行",
      grace(jd, {"new_job_grace_min": 10}, now=created + 60) is False)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
