# -*- coding: utf-8 -*-
"""回归用例：lib/export_guard.py —— 三入口共用的导出链守卫。

覆盖：
  A 三件套缺件：默认拦住（gate 判 artifacts_incomplete，且不进入质检）
  B 收尾证据：正式名 mp4 是旧文件 / progress.done=false → 判失败
  C 质检：rc!=0 → verify_failed，brief 带明细结论
  D 全部通过 → ok=True
  E watcher 路径（cw.apply_export_config）：cut_s 写 job、derive 覆盖生效，
    且 watcher.log 文案与抽离前一致
  F 崩溃回收：exporting 心跳超时 → retrying（eg.reap_stale）

运行：python tests\\test_export_guard_lib.py（纯临时目录；不碰真实剧/真实草稿）
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

import cover_watch as cw  # noqa: E402
import export_guard as eg  # noqa: E402
import export_state as es  # noqa: E402
import job_manifest as jm  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "export_guard_lib_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)


def make_job(tag):
    jd = os.path.join(TMP, tag, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    jm.save_job(jm.build_job(tag), jd)
    return jd


def make_bundle(out_dir, stem, *, done=True, age=0.0):
    """造一份合法三件套（mp4 / -封面.jpg / .progress.json）。"""
    os.makedirs(out_dir, exist_ok=True)
    mp4 = os.path.join(out_dir, stem + ".mp4")
    with open(mp4, "wb") as f:
        f.write(b"m" * 4096)
    with open(os.path.join(out_dir, stem + "-封面.jpg"), "wb") as f:
        f.write(b"j" * 64)
    with open(mp4 + ".progress.json", "w", encoding="utf-8") as f:
        json.dump({"done": done}, f)
    if age:
        for p in (mp4, mp4 + ".progress.json"):
            os.utime(p, (time.time() - age, time.time() - age))
    return mp4


def write_script(name, body):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


VERIFY_OK = write_script("verify_ok.py", "import sys\nsys.exit(0)\n")
VERIFY_BAD = write_script("verify_bad.py",
                          "print('帧级漂移: 3.2 帧（阈值 1.0）')\nimport sys\nsys.exit(1)\n")

# ── A：三件套缺件默认拦住 ────────────────────────────────────────
print("\n== A 缺件默认拦住 ==")
out_dir = os.path.join(TMP, "out_a")
mp4 = os.path.join(out_dir, "剧A.mp4")
miss = eg.missing_artifacts(mp4_path=mp4, strict_progress=True)
check("A1 三件全缺", miss == ["mp4", "封面", "progress"], miss)
res = eg.gate_after_export("剧A", mp4, verify_script=VERIFY_BAD)
check("A2 gate 判失败", res["ok"] is False, res)
check("A3 分类 artifacts_incomplete", res["failure_class"] == "artifacts_incomplete", res)
check("A4 未进入质检（无 verify_log）", res["verify_log"] is None, res)
check("A5 brief 说明缺什么", "缺" in res["brief"] and "mp4" in res["brief"], res["brief"])
os.makedirs(out_dir, exist_ok=True)
open(mp4, "wb").close()
check("A6 只有空 mp4 仍缺两件",
      eg.missing_artifacts(mp4_path=mp4, strict_progress=True) == ["封面", "progress"])

# ── B：收尾证据不足判失败 ────────────────────────────────────────
print("\n== B 收尾证据不足 ==")
mp4_b = make_bundle(os.path.join(TMP, "out_b"), "剧B", done=True, age=3600)
miss_b = eg.success_evidence_missing(mp4_b, time.time())
check("B1 旧产物冒充本轮", miss_b == ["mp4(旧文件：正式名本轮未更新)"], miss_b)
mp4_b2 = make_bundle(os.path.join(TMP, "out_b2"), "剧B2", done=False)
miss_b2 = eg.success_evidence_missing(mp4_b2)
check("B2 progress.done=false", miss_b2 == ["progress.done!=true"], miss_b2)
res_b = eg.gate_after_export("剧B2", mp4_b2, verify_script=VERIFY_OK)
check("B3 gate 分类 artifacts_missing", res_b["failure_class"] == "artifacts_missing", res_b)

# ── C：质检不过 → verify_failed ──────────────────────────────────
print("\n== C 质检不过 ==")
mp4_c = make_bundle(os.path.join(TMP, "out_c"), "剧C")
jd_c = make_job("guardC")
res_c = eg.gate_after_export("剧C", mp4_c, job_dir=jd_c,
                             verify_script=VERIFY_BAD, creationflags=0)
check("C1 gate 判失败", res_c["ok"] is False, res_c)
check("C2 分类 verify_failed", res_c["failure_class"] == "verify_failed", res_c)
check("C3 明细落 06_publish/verify.log",
      res_c["verify_log"] == os.path.join(jd_c, "06_publish", "verify.log"), res_c["verify_log"])
check("C4 brief 带实测数值", "帧级漂移" in res_c["brief"], res_c["brief"])

# ── D：全部通过 ─────────────────────────────────────────────────
print("\n== D 全部通过 ==")
mp4_d = make_bundle(os.path.join(TMP, "out_d"), "剧D")
jd_d = make_job("guardD")
res_d = eg.gate_after_export("剧D", mp4_d, job_dir=jd_d, started_at=time.time() - 5,
                             verify_script=VERIFY_OK, creationflags=0)
check("D1 ok=True", res_d["ok"] is True, res_d)
check("D2 stage=ok", res_d["stage"] == "ok", res_d)
check("D3 质检明细存在", os.path.isfile(res_d["verify_log"]), res_d["verify_log"])

# ── E：watcher 路径 apply_export_config（抽离后文案/行为不变）───────
print("\n== E watcher 路径 apply_export_config ==")
jd_e = make_job("guardE")
with open(os.path.join(jd_e, "export_config.json"), "w", encoding="utf-8") as f:
    json.dump({"cut_s": 52, "derive": ["tiktok"]}, f)
derive = cw.apply_export_config(jd_e)
job_e = jm.load_job(os.path.join(jd_e, "job.json"))
check("E1 cut_s 写进 job.video.cut_s", (job_e.get("video") or {}).get("cut_s") == 52, job_e)
check("E2 derive 覆盖返回 tiktok", derive == ["tiktok"], derive)
wlog = os.path.join(jd_e, "watcher.log")
text = open(wlog, encoding="utf-8").read() if os.path.isfile(wlog) else ""
check("E3 watcher.log 文案与抽离前一致",
      "export_config: cut_s=52, derive=['tiktok']" in text, text[-300:])
check("E4 无 export_config.json 时返回 None",
      cw.apply_export_config(make_job("guardE2")) is None)

# ── F：崩溃回收 ─────────────────────────────────────────────────
print("\n== F 崩溃回收 ==")
jd_f = make_job("guardF")
st = es._default()
st["status"] = "exporting"
st["heartbeat_ts"] = time.time() - es.STALE_AFTER_S - 60
st["updated_at"] = es._iso(st["heartbeat_ts"])
es._atomic_write(jd_f, st)
check("F1 心跳超时回收为 retrying", eg.reap_stale(jd_f) is True)
check("F2 磁盘状态已落盘", es._read_raw(jd_f)["status"] == "retrying")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
