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
  ⑪ 盘符根 root 的分隔符守卫
  ⑫ roots 为空 fail-closed
  ⑬ job_dir 不是 <剧名>/cover_job → 拒绝（--force 也不行）
  ⑭ 剧集目录不是 watch.roots 的直接子级 → 拒绝
  ⑮ 剧集目录是盘符根 → 拒绝

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
ROOTS = [ROOT]


def run(jd, archive_root=None, *, dry_run=False, roots=None):
    """默认以临时 ROOT 作为 watch.roots，避免测试依赖本机 presets.json。"""
    return aj.archive(jd, archive_root or ARCH, MF, dry_run=dry_run,
                      roots=ROOTS if roots is None else roots)


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


def make_job_at(series, leaf, name, stage="video_ready", export_state="cleaned"):
    """在任意目录结构下建一部剧（用于路径守卫用例）。"""
    jd = os.path.join(series, leaf)
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(name)
    job["stage"] = stage
    job["video"]["export_state"] = export_state
    jm.save_job(job, jd)
    return jd


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
rc = run(jd, dry_run=True)
check("dry-run 返回 0", rc == 0, rc)
check("dry-run 未改 stage", read_job(jd)["stage"] == "video_ready", read_job(jd)["stage"])
check("dry-run 未移动目录", os.path.isdir(series) and not os.path.isdir(os.path.join(ARCH, "dry")))
check("dry-run 未改 manifest",
      json.load(open(os.path.join(MF, "dry.json"), encoding="utf-8"))["paths"]["source_dir"] == series)

print("\n== ② export_state=cleaned 归档 ==")
series, jd = make_job("cleaned1", export_state="cleaned")
write_manifest("cleaned1", series)
rc = run(jd)
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
rc = run(jd)
check("返回 0", rc == 0, rc)
check("已移动", os.path.isdir(os.path.join(ARCH, "pub1", "cover_job")))

print("\n== ④ 未满足准入 ==")
series, jd = make_job("notready", stage="covers_ready")
rc = run(jd)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "covers_ready")
check("目录原地", os.path.isdir(os.path.join(series, "cover_job")))

print("\n== ⑤ 归档目标已存在 ==")
series, jd = make_job("conflict", export_state="cleaned")
os.makedirs(os.path.join(ARCH, "conflict"), exist_ok=True)
rc = run(jd)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ⑥ 活锁 ==")
series, jd = make_job("locked", export_state="cleaned")
with open(os.path.join(jd, ".watcher.lock"), "w", encoding="utf-8") as f:
    json.dump({"pid": os.getpid()}, f)
rc = run(jd)
check("跳过返回 1", rc == 1, rc)
check("stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ⑦ 导出中 ==")
series, jd = make_job("exporting", export_state="cleaned")
with open(os.path.join(jd, ".export_state.json"), "w", encoding="utf-8") as f:
    json.dump({"status": "exporting"}, f)
rc = run(jd)
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
    rc = run(jd)
check("首次失败返回 2", rc == 2, rc)
check("stage 已置 archived", read_job(jd)["stage"] == "archived")
check("源目录未动", os.path.isdir(os.path.join(series, "cover_job")))
check("目标未生成", not os.path.exists(os.path.join(ARCH, "resume")))
rc = run(jd)
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

print("\n== ⑪ 盘符根 root 修正（不再双分隔符失效） ==")
check("root=D:/ 时 D:/短剧/存档 命中", aj._root_hit("D:/短剧/存档", ["D:/"]) == "D:/")
check("root=D:/ 时 D:/短剧_存档 也在 D: 盘内，命中", aj._root_hit("D:/短剧_存档", ["D:/"]) == "D:/")
check("root=D:/短剧 时 D:/短剧_存档 不命中（分隔符守卫）",
      aj._root_hit("D:/短剧_存档", ["D:/短剧"]) is None)
series, jd = make_job("drive_root", export_state="cleaned")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = aj.archive(jd, "D:/短剧/存档", MF, roots=["D:/"])
out = buf.getvalue()
check("盘符根下归档池 rc=2", rc == 2, rc)
check("拒绝文案指明命中 root", "命中 root" in out and "D:/" in out, out.strip())
check("stage 未变", read_job(jd)["stage"] == "video_ready")
check("目录原地", os.path.isdir(os.path.join(series, "cover_job")))

print("\n== ⑫ roots 为空 fail-closed ==")
series, jd = make_job("noroots", export_state="cleaned")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = aj.archive(jd, ARCH, MF, roots=[])
out = buf.getvalue()
check("roots 为空返回 2", rc == 2, rc)
check("文案说明未配置 watch.roots 且已拒绝",
      "未配置 watch.roots" in out and "已拒绝执行" in out, out.strip())
check("stage 未变", read_job(jd)["stage"] == "video_ready")
check("目录原地", os.path.isdir(os.path.join(series, "cover_job")))
check("归档目录未创建", not os.path.exists(os.path.join(ARCH, "noroots")))

print("\n== ⑬ job_dir 不是 <剧名>/cover_job → 拒绝 ==")
series = os.path.join(ROOT, "notcover")
jd = make_job_at(series, "jobs", "notcover")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = run(jd)
out = buf.getvalue()
check("非 cover_job 返回 2", rc == 2, rc)
check("报错说明必须是 cover_job", "cover_job" in out and "[FAIL]" in out, out.strip())
check("非 cover_job 源目录未移动", os.path.isdir(jd) and os.path.isdir(series))
check("非 cover_job 未生成归档目标", not os.path.exists(os.path.join(ARCH, "notcover")))
check("非 cover_job stage 未变", read_job(jd)["stage"] == "video_ready")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = aj.archive(jd, ARCH, MF, force=True, roots=ROOTS)
check("非 cover_job 加 --force 仍拒绝", rc == 2, rc)

print("\n== ⑭ 剧集目录不是 roots 直接子级 → 拒绝 ==")
series = os.path.join(ROOT, "sub", "deep")
jd = make_job_at(series, "cover_job", "deep")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = run(jd)
out = buf.getvalue()
check("更深层返回 2", rc == 2, rc)
check("报错说明必须是直接子级", "直接子级" in out, out.strip())
check("更深层源目录未移动", os.path.isdir(os.path.join(series, "cover_job")))
check("更深层未生成归档目标", not os.path.exists(os.path.join(ARCH, "deep")))
check("更深层 stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n== ⑮ 剧集目录是盘符根 → 拒绝 ==")
series, jd = make_job("driveroot", export_state="cleaned")
real_dirname = os.path.dirname


def fake_dirname(p):
    # 假装 series 是盘符根：dirname(root) == root
    if os.path.normcase(os.path.normpath(p)) == os.path.normcase(os.path.normpath(series)):
        return os.path.normpath(series)
    return real_dirname(p)


buf = io.StringIO()
with mock.patch.object(aj.os.path, "dirname", side_effect=fake_dirname), \
        contextlib.redirect_stdout(buf):
    rc = run(jd)
out = buf.getvalue()
check("盘符根返回 2", rc == 2, rc)
check("报错说明盘符根", "盘符根" in out, out.strip())
check("盘符根源目录未移动", os.path.isdir(os.path.join(series, "cover_job")))
check("盘符根 stage 未变", read_job(jd)["stage"] == "video_ready")

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(0 if not fails else 1)
