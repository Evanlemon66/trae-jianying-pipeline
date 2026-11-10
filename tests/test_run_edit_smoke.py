# -*- coding: utf-8 -*-
"""端到端冒烟：run_edit 主流程真实走完（防「单测全过、pipeline start 后 NameError」）。

背景（2026-11-07）：`_write_lock` 被误删，现有测试无一真正进入 run_edit，
直到 watcher 跑到 pipeline start 之后才抛 NameError，空转 9 轮、写坏 6 集缓存。

本用例只替身「跑批量子进程」那一层（Popen / 看门狗），其余
backfill_hashes → validate → ingest → _write_lock → 成功记账 全部真实执行；
几秒内跑完，不依赖 GPU、网络、真实视频，也不碰真实剧目录。

运行：python tests\\test_run_edit_smoke.py（纯临时目录）
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
import edit_state as eds  # noqa: E402

# 回归测试不弹真实桌面通知；避免受本机 edit_pause_after 配置影响
cw.toast = lambda *a, **k: None
cw.pc.get_watch_config = lambda: {"edit_pause_after": 3}

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25


def make_fixture(name):
    """最小可 ingest 的 cover_job：4 张契约封面 + prompts + 3 条已选标题。"""
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
    for stem in ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"):
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
        c["image_sha256"] = jm.sha256_file(os.path.join(jd, c["file"]))
    job["vertical_cover"]["status"] = "done"
    job["vertical_cover"]["image_sha256"] = jm.sha256_file(
        os.path.join(jd, job["vertical_cover"]["file"]))
    jm.save_job(job, jd)
    return jd


TMP = os.path.join(tempfile.gettempdir(), "run_edit_smoke_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)


class _FakeProc:
    pid = 424242


popped = []


def _fake_popen(cmd, **kw):
    popped.append((cmd, kw))
    return _FakeProc()


_real_popen = cw.subprocess.Popen
_real_wait = cw._wait_edit_with_watchdog
cw.subprocess.Popen = _fake_popen
# 只替身「等子进程」这一层：不真起 batch_runner，也不等真实进程
cw._wait_edit_with_watchdog = lambda job_dir, proc, name: 0
try:
    jd = make_fixture("冒烟剧")
    plan = {"name": "冒烟剧", "cmd": [sys.executable, "-c", "print('stub')"]}
    cw.acquire_lock(jd, phase="edit")
    ok = cw.run_edit(jd, plan, dry_run=False)
    lock = cw.read_lock(jd)  # 释放前读取，验证 _write_lock 真的执行了
finally:
    cw.subprocess.Popen = _real_popen
    cw._wait_edit_with_watchdog = _real_wait
    cw.release_lock(jd)

check("run_edit 返回 True（主流程走完）", ok is True, ok)
check("只起了一次批量进程", len(popped) == 1, popped)
check("Popen 使用 plan['cmd']", bool(popped) and popped[0][0] == plan["cmd"], popped)
check("Popen cwd 为仓库根", bool(popped) and popped[0][1].get("cwd") == cw._REPO, popped)
check("_write_lock 写入 pipeline_pid",
      bool(lock) and lock.get("pipeline_pid") == _FakeProc.pid, lock)
check("_write_lock 写入 phase=edit", bool(lock) and lock.get("phase") == "edit", lock)

st = eds.load(jd)
check("edit 状态置 done", st.get("status") == "done", st)
check("成功后 attempts 清零", int(st.get("attempts") or 0) == 0, st)

with open(os.path.join(jd, "watcher.log"), encoding="utf-8") as f:
    log = f.read()
check("日志有 validate ok", "validate ok" in log)
check("日志有 ingest ok（主流程已穿过 ingest）", "ingest ok stage=" in log)
check("日志有 pipeline start（已穿过 _write_lock 调用点）", "pipeline start" in log)
check("日志有 edit done", "edit done" in log)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
