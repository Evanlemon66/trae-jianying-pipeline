# -*- coding: utf-8 -*-
"""run_edit 前置归一回归：写入方不规范（封面裸文件名）不再卡死在闸门。

覆盖：
  A. cover_map / vertical_cover 只有裸文件名 → 一次 run_edit 内自愈并通过 validate
  B. 归一后文件真实缺失 → 仍然失败，且错误信息指向「文件不存在」
  C. 已规范的数据：normalize_job_shape 不改动、job.json 字节不变（纯内存、不写盘）
  D. 归一不替写入方改业务：标题内容 / 选中项保持原样

运行：python tests\\test_run_edit_normalize.py（纯临时目录，不真起剪辑）
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
import cover_watch as cw  # noqa: E402

cw.toast = lambda *a, **k: None
cw.pc.get_watch_config = lambda: {"edit_pause_after": 3}

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25
TMP = os.path.join(tempfile.gettempdir(), "run_edit_normalize_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)


def make_fixture(name, *, bare=False, drop=None):
    jd = os.path.join(TMP, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    with open(os.path.join(jd, "02_titles", "titles.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"id": f"T{i}", "title": f"标题{i}"} for i in range(1, 13)]},
                  f, ensure_ascii=False)
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"slot": 1}, {"slot": 2}, {"slot": 3}, {"slot": "vertical"}]}, f)
    stems = ["cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"]
    for stem in stems:
        if stem == drop:
            continue
        with open(os.path.join(jd, "04_covers", stem + ".png"), "wb") as f:
            f.write(PNG)

    job = jm.build_job(name)
    job["stage"] = "covers_ready"
    job["titles"]["gate"] = "done"
    job["titles"]["selected"] = [
        {"slot": 1, "title_id": "T1", "title": "标题1"},
        {"slot": 2, "title_id": "T2", "title": "标题2"},
        {"slot": 3, "title_id": "T3", "title": "标题3"},
    ]
    job["titles"]["selected_by"] = "user"
    job["cleanup"]["status"] = "blocked"
    for c in job["cover_map"]:
        c["status"] = "done"
        if not (bare and drop and os.path.basename(c["file"]) == drop + ".png"):
            c["image_sha256"] = jm.sha256_file(os.path.join(jd, c["file"]))
    job["vertical_cover"]["status"] = "done"
    if not (bare and drop and os.path.basename(job["vertical_cover"]["file"]) == drop + ".png"):
        job["vertical_cover"]["image_sha256"] = jm.sha256_file(
            os.path.join(jd, job["vertical_cover"]["file"]))
    if bare:  # 模拟第三方写入方：只写裸文件名
        for c in job["cover_map"]:
            c["file"] = os.path.basename(c["file"])
            c["image"] = c["file"]
        v = job["vertical_cover"]
        v["file"] = os.path.basename(v["file"])
        v["image"] = v["file"]
    jm.save_job(job, jd)
    return jd


class _FakeProc:
    pid = 424242


popped = []


def _fake_popen(cmd, **kw):
    popped.append((cmd, kw))
    return _FakeProc()


_real_popen = cw.subprocess.Popen
_real_wait = cw._wait_edit_with_watchdog
cw.subprocess.Popen = _fake_popen
cw._wait_edit_with_watchdog = lambda job_dir, proc, name: 0

try:
    # ── A. 裸文件名 → 一次 run_edit 自愈
    jd = make_fixture("裸名剧", bare=True)
    popped.clear()
    cw.acquire_lock(jd, phase="edit")
    ok = cw.run_edit(jd, {"name": "裸名剧", "cmd": [sys.executable, "-c", "print('stub')"]},
                     dry_run=False)
    cw.release_lock(jd)
    with open(os.path.join(jd, "watcher.log"), encoding="utf-8") as f:
        log = f.read()
    job = jm.load_job(os.path.join(jd, "job.json"))
    check("A1 裸文件名一次 run_edit 自愈成功", ok is True, log[-500:])
    check("A2 日志记录归一动作", "归一: cover_map[1].file → 04_covers/" in log)
    check("A3 日志记录 vertical 归一", "归一: vertical_cover.file → 04_covers/" in log)
    check("A4 归一后通过 validate", "validate ok" in log)
    check("A5 落盘已是 04_covers/ 前缀",
          all(str(c.get("file")).startswith("04_covers/") for c in job["cover_map"] + [job["vertical_cover"]]),
          [c.get("file") for c in job["cover_map"] + [job["vertical_cover"]]])
    check("A6 只起了一次批量进程", len(popped) == 1, popped)

    # ── D. 归一不改标题内容 / 选中项
    check("D1 选中项未被改动",
          [(s["slot"], s["title_id"], s["title"]) for s in job["titles"]["selected"]]
          == [(1, "T1", "标题1"), (2, "T2", "标题2"), (3, "T3", "标题3")],
          job["titles"]["selected"])

    # ── B. 真缺失文件 → 仍失败，且报「文件不存在」
    jd = make_fixture("缺图剧", bare=True, drop="cover_02_contrast")
    popped.clear()
    cw.acquire_lock(jd, phase="edit")
    ok = cw.run_edit(jd, {"name": "缺图剧", "cmd": [sys.executable, "-c", "print('stub')"]},
                     dry_run=False)
    cw.release_lock(jd)
    with open(os.path.join(jd, "watcher.log"), encoding="utf-8") as f:
        log = f.read()
    check("B1 真缺失文件仍失败", ok is False, ok)
    check("B2 错误指向文件不存在", "文件不存在: 04_covers/cover_02_contrast.png" in log, log[-600:])
    check("B3 不再报「应位于 04_covers/」", "应位于 04_covers/" not in log)
    check("B4 失败时不起批量进程", len(popped) == 0, popped)

    # ── C. 已规范数据：纯内存归一不改字节
    jd = make_fixture("规范剧")
    path = os.path.join(jd, "job.json")
    with open(path, "rb") as f:
        before = f.read()
    job = jm.load_job(path)
    notes = jm.normalize_job_shape(job, jd)
    with open(path, "rb") as f:
        after = f.read()
    check("C1 已规范数据无归一动作", notes == [], notes)
    check("C2 normalize_job_shape 不写盘（字节不变）", before == after)
    check("C3 内存对象封面路径保持规范",
          [c["file"] for c in job["cover_map"]] ==
          ["04_covers/cover_01_hit.png", "04_covers/cover_02_contrast.png",
           "04_covers/cover_03_guard.png"],
          [c["file"] for c in job["cover_map"]])
finally:
    cw.subprocess.Popen = _real_popen
    cw._wait_edit_with_watchdog = _real_wait

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
