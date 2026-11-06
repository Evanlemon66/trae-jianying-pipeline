# -*- coding: utf-8 -*-
"""回归用例：ingest 归一 cleanup.status + watcher 校验日志反映真实结果。

覆盖：
  ① cleanup.status=pending → ingest 后归一为 blocked、通过校验、revision+1、
    history 记归一；
  ② 校验不通过时 watcher 日志不得出现 "validate ok"，且不得进入 ingest。

运行：python tests\\test_ingest_cleanup_status.py（纯临时目录，不真导出）
"""
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import cover_watch as cw   # noqa: E402

cw.toast = lambda *a, **k: None

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25


def make_fixture(name, cleanup_status):
    jd = os.path.join(TMP, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    with open(os.path.join(jd, "02_titles", "titles.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"id": "T3", "title": "标题三"},
                              {"id": "T5", "title": "标题五"},
                              {"id": "T12", "title": "标题十二"}]},
                  f, ensure_ascii=False)
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
        {"slot": 2, "title_id": "T5", "title": "标题五"},
        {"slot": 3, "title_id": "T12", "title": "标题十二"},
    ]
    job["titles"]["selected_by"] = "user"
    job["cleanup"]["status"] = cleanup_status
    # hash 预填，确保 backfill_hashes 无事可补、不会抢先抛异常
    for c in job["cover_map"]:
        c["status"] = "done"
        c["image_sha256"] = jm.sha256_file(os.path.join(jd, c["file"]))
    job["vertical_cover"]["status"] = "done"
    job["vertical_cover"]["image_sha256"] = jm.sha256_file(
        os.path.join(jd, job["vertical_cover"]["file"]))
    jm.save_job(job, jd)
    return jd


TMP = os.path.join(tempfile.gettempdir(), "ingest_cleanup_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

print("\n== ① cleanup.status=pending → ingest 归一 blocked ==")
jd = make_fixture("归一A", "pending")
rev0 = jm.load_job(os.path.join(jd, "job.json"))["revision"]
rep = jm.ingest_job(jd)
job = jm.load_job(os.path.join(jd, "job.json"))
check("cleanup.status → blocked", job["cleanup"]["status"] == "blocked",
      job["cleanup"]["status"])
check("report.normalized 含归一记录",
      any("cleanup.status" in x for x in rep.get("normalized", [])), rep.get("normalized"))
check("归一后 validate 通过", jm.validate_job(job, jd) == [], jm.validate_job(job, jd))
check("revision+1", job["revision"] == rev0 + 1, job["revision"])
check("history 含归一记录",
      "cleanup.status" in (job.get("history") or [{}])[-1].get("detail", ""),
      (job.get("history") or [{}])[-1])

print("\n== ② 校验不通过：日志无 validate ok、且不进 ingest ==")
jd2 = make_fixture("校验失败B", "bogus")
ingest_calls = []
_orig_ingest = cw.jm.ingest_job


def _spy_ingest(*a, **k):
    ingest_calls.append(1)
    raise AssertionError("校验失败时不应进入 ingest")


cw.jm.ingest_job = _spy_ingest
try:
    ok = cw.run_edit(jd2, {"name": "测试剧", "cmd": []}, dry_run=False)
finally:
    cw.jm.ingest_job = _orig_ingest

with open(os.path.join(jd2, "watcher.log"), "r", encoding="utf-8") as f:
    log = f.read()
check("run_edit 返回 False", ok is False, ok)
check("日志无 validate ok", "validate ok" not in log, log)
check("日志写真实错误", "cleanup.status" in log, log)
check("未进入 ingest", ingest_calls == [], ingest_calls)

print("\n== ③ watcher 路径：pending 在校验前归一，正常进 ingest ==")
jd3 = make_fixture("watcher归一C", "pending")
ok_calls = []
_orig_ingest2 = cw.jm.ingest_job


def _spy_ingest_ok(job_dir, **k):
    ok_calls.append(job_dir)
    return _orig_ingest2(job_dir, **k)


cw.jm.ingest_job = _spy_ingest_ok
try:
    ok3 = cw.run_edit(jd3, {
        "name": "测试剧",
        "cmd": [sys.executable, "-c", "import sys; sys.exit(0)"],
    }, dry_run=False)
finally:
    cw.jm.ingest_job = _orig_ingest2

with open(os.path.join(jd3, "watcher.log"), "r", encoding="utf-8") as f:
    log3 = f.read()
check("run_edit 返回 True", ok3 is True, ok3)
check("日志出现 validate ok", "validate ok" in log3, log3)
check("日志无 validate 失败", "validate 失败" not in log3, log3)
check("正常进入 ingest", ok_calls == [jd3], ok_calls)
job3 = jm.load_job(os.path.join(jd3, "job.json"))
check("落盘 cleanup.status=blocked", job3["cleanup"]["status"] == "blocked",
      job3["cleanup"]["status"])
check("落盘 history 含归一记录",
      any("cleanup.status" in (h.get("detail") or "") for h in job3.get("history", [])),
      job3.get("history"))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
