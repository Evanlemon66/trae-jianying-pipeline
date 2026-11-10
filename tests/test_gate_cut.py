"""回归用例：B. update_job gate 守卫；C. _effective_cut 优先级四组合。

运行：python tests\\test_gate_cut.py（无第三方依赖）
"""
import json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import job_manifest as jm  # noqa: E402
import cover_watch as cw  # noqa: E402

fails = []
# 回归测试不弹真实桌面通知：把 toast 换成内存捕获，仍保留 C1 覆盖通知路径
toasts = []
cw.toast = lambda title, msg, icon="info": toasts.append((title, msg))


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def make_job_dir(tmp, name="守卫测试剧"):
    jd = os.path.join(tmp, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试")
    job = jm.build_job(name)
    job["titles"]["gate"] = "done"
    job["titles"]["selected"] = [
        {"slot": 1, "title_id": "T1", "title": "一"},
        {"slot": 2, "title_id": "T2", "title": "二"},
        {"slot": 3, "title_id": "T3", "title": "三"},
    ]
    job["titles"]["selected_by"] = "user"
    jm.save_job(job, jd)
    return jd


TMP = os.path.join(tempfile.gettempdir(), "gate_cut_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

# ── B. gate 守卫 ──────────────────────────────────────────────
print("── B. gate 守卫 ──")
jd = make_job_dir(TMP)
try:
    jm.update_job(jd, {"titles": {"gate": "pending"}}, event="stale_patch")
    check("B1 stale titles(gate=pending) 被拒绝", False)
except ValueError as e:
    check("B1 stale titles(gate=pending) 被拒绝", "不能从 done 回退" in str(e), str(e))
job = jm.load_job(os.path.join(jd, "job.json"))
check("B2 拒绝后 gate 仍为 done", job["titles"]["gate"] == "done")
check("B3 拒绝后 revision 未变（无写入）", job["revision"] == 1)
jm.update_job(jd, {"titles": {"gate": "done"}}, event="noop_ok")
check("B4 done→done 合法补丁放行", jm.load_job(os.path.join(jd, "job.json"))["revision"] == 2)

# ── C. _effective_cut 优先级 ──────────────────────────────────
print("\n── C. _effective_cut 优先级 ──")
jd2 = os.path.join(TMP, "cut剧", "cover_job")
os.makedirs(jd2, exist_ok=True)


def set_ec(cut):
    with open(os.path.join(jd2, "export_config.json"), "w", encoding="utf-8") as f:
        json.dump({"cut_s": cut}, f)


def rm_ec():
    p = os.path.join(jd2, "export_config.json")
    if os.path.isfile(p):
        os.remove(p)


def wf_of(rev, cut):
    wf = dict(jm.WORKFLOW_DEFAULTS)
    wf["intent_revision"] = rev
    wf["cut_s"] = cut
    return wf


JOB_VID = {"video": {"cut_s": 30}}
set_ec(60)
# C1 rev=0 + export_config 存在 → export_config 优先（新人工动作）
# 保留通知路径覆盖：默认 notify=True，用上面替换的 toast 捕获，不弹真实桌面通知
toasts.clear()
check("C1 rev=0 有ec: ec(60) > wf(91)", cw._effective_cut(wf_of(0, 91), JOB_VID, jd2) == 60.0)
check("C1b 通知路径弹一次且标题正确",
      len(toasts) == 1 and "裁剪意图分歧" in toasts[0][0], toasts)
# C2 rev=0 无 export_config → workflow
rm_ec()
check("C2 rev=0 无ec: wf(91)",
      cw._effective_cut(wf_of(0, 91), JOB_VID, jd2, notify=False, write_log=False) == 91.0)
# C3 rev>0 + export_config 存在 → workflow 最高（人工已确认）
set_ec(60)
check("C3 rev>0 有ec: wf(91) > ec(60)",
      cw._effective_cut(wf_of(1, 91), JOB_VID, jd2, notify=False, write_log=False) == 91.0)
# C4 rev>0 无 export_config → workflow
rm_ec()
check("C4 rev>0 无ec: wf(91)",
      cw._effective_cut(wf_of(1, 91), JOB_VID, jd2, notify=False, write_log=False) == 91.0)
# C5 video.cut_s 最低：无 wf/ec 时兜底；有 wf 时被压过
check("C5a 无wf无ec: video(30) 兜底",
      cw._effective_cut(wf_of(0, None), JOB_VID, jd2, notify=False, write_log=False) == 30.0)
check("C5b 有wf: video 被压过",
      cw._effective_cut(wf_of(0, 91), JOB_VID, jd2, notify=False, write_log=False) == 91.0)
# C6 0/负值/布尔 不生效
set_ec(0)
check("C6 ec=0 视为不裁→落 video(30)",
      cw._effective_cut(wf_of(0, None), JOB_VID, jd2, notify=False, write_log=False) == 30.0)
rm_ec()
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'✅ 全部通过' if not fails else '❌ 失败: ' + str(fails)}")
sys.exit(1 if fails else 0)
