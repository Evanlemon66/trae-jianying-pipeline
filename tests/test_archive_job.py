# -*- coding: utf-8 -*-
"""回归用例：pipeline/archive_job.py 归档（stage=archived + 移出公共 roots）。

覆盖：
  ① dry-run：只打印计划，不写 stage、不移动目录
  ② 已清理（video.export_state=cleaned）→ 归档成功，manifest source_dir 同步
  ③ 已发布（publish.status=published）→ 归档成功
  ④ 未满足准入（covers_ready）→ 跳过，目录原地不动
  ⑤ 归档目标已存在 → 跳过
  ⑥ 活锁（.watcher.lock pid 存活）→ 跳过
  ⑦ .export_state.json status=exporting → 跳过
  ⑧ stage=archived 被 cover_watch.check_trigger 硬拦
  ⑨ 移动失败后重跑：stage 已 archived，源仍在、目标不存在 → 续做成功，不重复记事件
  ⑩ archive_root 等于/嵌在 roots 下 → 拒绝，报错指明命中的 root

运行：python tests\\test_archive_job.py（纯临时目录，不碰真实剧）
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import archive_job as aj  # noqa: E402
import cover_watch as cw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "archive_job_test")
shutil.rmtree(TMP, ignore_errors=True)
ROOT = os.path.join(TMP, "root")
ARCH = os.path.join(TMP, "archive")
MF = os.path.join(TMP, "manifests")
os.makedirs(ROOT, exist_ok=True)
os.makedirs(MF, exist_ok=True)


def make_job(name, stage="video_ready", export_state=None, publish_status="not_ready"):
    series = os.path.join(ROOT, name)
    jd = os.path.join(series, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(name)
    job["stage"] = stage
    job["video"]["export_state"] = export_state
    job["publish"]["status"] = publish_status
    jm.save_job(job, jd)
    return series, jd


def read_job(jd):
    return jm.load_job(os.path.join(jd, "job.json"))


def write_manifest(name, series):
    fp = os.path.join(MF, f"{name}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"draft_name": name, "paths": {"source_dir": series}}, f, ensure_ascii=False)
    return fp


print("\n== ① dry-run ==")
series, jd = make_job("dry", export_state="cleaned")
write_manifest("dry", series)
rc = aj.archive(jd, ARCH, MF, dry_run=True)
check("dry-run 返回 0", rc == 0, rc)
check("dry-run 未改 stage", read_job(jd)["stage"] == "video_ready", read_job(jd)["stage"])
check("dry-run 未移动目录", os.path.isdir(series) and not os.path.isdir(os.path.join(ARCH, "dry")))
check("dry-run 未改 manifest",
      json.load(open(os.path.join(MF, "dry.json"), encoding="utf-8"))["paths"]["source_dir"] == series)

print("\n== ② export_state=cleaned 归档 ==")
series, jd = make_job("cleaned1", export_state="cleaned")
write_manifest("cleaned1", series)
rc = aj.archive(jd, ARCH, MF)
new_series = os.path.join(ARCH, "cleaned1")
check("返回 0", rc == 0, rc)
check("旧目录已移走", not os.path.isdir(series))
check("新目录存在", os.path.isdir(os.path.join(new_series, "cover_job")))
check("stage=archived", read_job(os.path.join(new_series, "cover_job"))["stage"] == "archived")
check("history 记 archived",
      read_job(os.path.join(new_series, "cover_job"))["history"][-1]["event"] == "archived")
check("manifest source_dir 同步",
      os.path.normcase(json.load(open(os.path.join(MF, "cleaned1.json"), encoding="utf-8"))
                       ["paths"]["source_dir"]) == os.path.normcase(new_series))

print("\n== ③ publish.status=published 归档 ==")
series, jd = make_job("pub1", stage="publish_ready", publish_status="published")
rc = aj.archive(jd, ARCH, MF)
check("返回 0", rc == 0, rc)
check("已移动", os.path.isdir(os.path.join(ARCH, "pub1", "cover_job")))

print("\n== ④ 未满足准入 ==")
series, jd = make_job("notready", stage="covers_ready")
rc = aj.archive(jd, ARCH, MF)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "covers_ready")
check("目录原地", os.path.isdir(os.path.join(series, "cover_job")))

print("\n== ⑤ 归档目标已存在 ==")
series, jd = make_job("conflict", export_state="cleaned")
os.makedirs(os.path.join(ARCH, "conflict"), exist_ok=True)
rc = aj.archive(jd, ARCH, MF)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ⑥ 活锁 ==")
series, jd = make_job("locked", export_state="cleaned")
with open(os.path.join(jd, ".watcher.lock"), "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid()}, f)
rc = aj.archive(jd, ARCH, MF)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ⑦ 导出中 ==")
series, jd = make_job("exporting", export_state="cleaned")
with open(os.path.join(jd, ".export_state.json"), "w", encoding="utf-8") as f:
    json.dump({"status": "exporting"}, f)
rc = aj.archive(jd, ARCH, MF)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ⑧ check_trigger 硬拦 archived ==")
ok, reasons = cw.check_trigger(os.path.join(ARCH, "cleaned1", "cover_job"))
check("不再触发", ok is False, (ok, reasons))
check("原因含 archived", any("archived" in r for r in reasons), reasons)

print("\n== ⑨ 移动失败后重跑续做 ==")
series, jd = make_job("resume", export_state="cleaned")
write_manifest("resume", series)
with mock.patch.object(aj.shutil, "move", side_effect=OSError("模拟移动失败")):
    rc = aj.archive(jd, ARCH, MF)
check("首次失败返回 2", rc == 2, rc)
check("stage 已置 archived", read_job(jd)["stage"] == "archived")
check("源目录未动", os.path.isdir(os.path.join(series, "cover_job")))
check("目标未生成", not os.path.exists(os.path.join(ARCH, "resume")))
rc = aj.archive(jd, ARCH, MF)
check("重跑续做返回 0", rc == 0, rc)
check("重跑后已移动", os.path.isdir(os.path.join(ARCH, "resume", "cover_job")))
check("源已不在", not os.path.isdir(series))
archived_events = [h for h in read_job(os.path.join(ARCH, "resume", "cover_job"))["history"]
                   if h.get("event") == "archived"]
check("archived 事件仅一条", len(archived_events) == 1, archived_events)

print("\n== ⑩ archive_root 等于/嵌在 roots 下拒绝 ==")
series, jd = make_job("nested", export_state="cleaned")
inside = os.path.join(ROOT, "archive_inside")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = aj.archive(jd, inside, MF, roots=[ROOT])
out = buf.getvalue()
check("嵌在 roots 下返回 2", rc == 2, rc)
check("报错指明命中 root", "命中 root" in out and ROOT in out, out.strip())
check("stage 未变", read_job(jd)["stage"] == "video_ready")
check("目录原地", os.path.isdir(os.path.join(series, "cover_job")))
check("未创建归档目录", not os.path.exists(inside))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = aj.archive(jd, ROOT, MF, roots=[ROOT])
out = buf.getvalue()
check("等于 roots 也拒绝", rc == 2, rc)
check("等于 roots 报错同样指明 root", "命中 root" in out and ROOT in out, out.strip())

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(0 if not fails else 1)
