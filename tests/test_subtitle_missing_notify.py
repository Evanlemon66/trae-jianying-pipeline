# -*- coding: utf-8 -*-
"""整轮缺字幕通知测试：一条、含全部集号、进程内去重、空清单不发。

独立脚本运行：
    python tests/test_subtitle_missing_notify.py
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import job_manifest as jm  # noqa: E402
import subtitle_guard  # noqa: E402


def _new_job_dir(tmp, missing):
    for d in ("01_input", "02_titles", "03_prompts", "04_covers", "05_video", "06_publish"):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    with open(os.path.join(tmp, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job("测试剧")
    job["derived"]["subtitle_missing"] = list(missing)
    jm.save_job(job, tmp)
    return tmp


def _with_recorder(fn):
    calls = []
    old_toast = subtitle_guard.toast
    old_notified = set(subtitle_guard._notified)
    subtitle_guard.toast = lambda title, body=None, *a, **k: calls.append((title, body))
    subtitle_guard._notified.clear()
    try:
        fn(calls)
    finally:
        subtitle_guard.toast = old_toast
        subtitle_guard._notified.clear()
        subtitle_guard._notified.update(old_notified)


def test_one_toast_with_all_episodes():
    def run(calls):
        with tempfile.TemporaryDirectory() as tmp:
            _new_job_dir(tmp, [8, 1, 3])
            assert subtitle_guard.notify(tmp) is True
            assert len(calls) == 1, calls
            title, body = calls[0]
            assert "缺字幕需人工处理" in title, title
            assert "第1、3、8集" in body, body
            assert "共 3 集" in body, body
            assert "不会导出" in body, body
            # 同一轮重复调用不重弹
            assert subtitle_guard.notify(tmp) is False
            assert len(calls) == 1, calls
    _with_recorder(run)
    print("OK 清单非空只弹一条，含全部集号，重复调用去重")


def test_empty_or_missing_no_toast():
    def run(calls):
        with tempfile.TemporaryDirectory() as tmp:
            _new_job_dir(tmp, [])
            assert subtitle_guard.notify(tmp) is False
        with tempfile.TemporaryDirectory() as tmp:
            assert subtitle_guard.notify(tmp) is False   # job.json 不存在
        assert calls == [], calls
    _with_recorder(run)
    print("OK 空清单 / 无 job.json 不发通知")


def test_wiring_guard():
    with open(os.path.join(_ROOT, "pipeline", "batch_runner.py"), encoding="utf-8") as f:
        batch = f.read()
    with open(os.path.join(_ROOT, "pipeline", "series_runner.py"), encoding="utf-8") as f:
        series = f.read()

    assert batch.count("subtitle_guard.notify(job_dir)") == 2, \
        "batch_runner 应在串流/逐集两条分支各收尾通知一次"
    assert series.count("subtitle_guard.notify(job_dir)") == 1, \
        "series_runner 被拦分支应只通知一次"
    assert batch.index("jm.require_export_allowed(job_dir)") < batch.index("if args.auto_export"), \
        "batch_runner 的闸门必须在启动自动导出之前"
    assert "require_export_allowed(job_dir)" in series
    print("OK 结构守卫：两条整轮分支各一次通知、导出前先过闸门")


def main():
    test_one_toast_with_all_episodes()
    test_empty_or_missing_no_toast()
    test_wiring_guard()
    print("\nALL OK: test_subtitle_missing_notify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
