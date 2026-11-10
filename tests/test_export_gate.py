# -*- coding: utf-8 -*-
"""导出前硬拦测试：job 闸门 / cover_watch / series_runner / export_state 复位 / 直调脚本。

独立脚本运行：
    python tests/test_export_gate.py
"""
import importlib.util
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import job_manifest as jm  # noqa: E402
import export_state as es  # noqa: E402
import cover_watch as cw  # noqa: E402
import series_runner as sr  # noqa: E402


def _new_job_dir(tmp, missing):
    for d in ("01_input", "02_titles", "03_prompts", "04_covers", "05_video", "06_publish"):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    with open(os.path.join(tmp, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job("测试剧")
    job["derived"]["subtitle_missing"] = list(missing)
    jm.save_job(job, tmp)
    return tmp


def _raise_on_popen(*_a, **_k):
    raise AssertionError("缺字幕未修复时不应启动导出子进程")


def test_require_gate():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp, [8, 3])
        try:
            jm.require_export_allowed(tmp)
        except jm.ExportBlocked as e:
            assert e.missing == [3, 8], e.missing
            assert "第3、8集" in str(e), str(e)
        else:
            raise AssertionError("清单非空未拦截")
    print("OK require_export_allowed 拦截并完整列出集号")


def test_cover_watch_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp, [3])
        old_popen = cw.subprocess.Popen
        cw.subprocess.Popen = _raise_on_popen
        try:
            ok = cw.run_export(tmp, {}, {}, dry_run=False)
        finally:
            cw.subprocess.Popen = old_popen
        assert ok is False, ok
        st = es.load(tmp)
        assert st["status"] == "failed", st
        assert st["failure_class"] == "subtitle_missing", st
        assert st["consecutive_failures"] == 0, st   # 硬拦不计入重试
        reason = es.gate(tmp)
        assert reason and "subtitle_missing" in reason, reason
    print("OK cover_watch.run_export 被拦：Popen 未调用、状态 failed/subtitle_missing")


def test_cover_watch_dry_run_no_state():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp, [3])
        ok = cw.run_export(tmp, {}, {}, dry_run=True)
        assert ok is False
        assert not os.path.isfile(os.path.join(tmp, ".export_state.json")), \
            "dry_run 不应写导出状态"
    print("OK dry_run 被拦且不写 .export_state.json")


def test_series_runner_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp, [5])
        old_popen = sr.subprocess.Popen
        sr.subprocess.Popen = _raise_on_popen
        try:
            ok = sr._run_export("测试剧串流", job_dir=tmp)
        finally:
            sr.subprocess.Popen = old_popen
        assert ok is False
    print("OK series_runner._run_export 被拦且不启动子进程")


def test_clear_block():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp, [3])
        es.fail(tmp, "缺字幕未修复：第3集", retryable=False,
                failure_class="subtitle_missing", phase="export")
        before = es.load(tmp)
        assert before["status"] == "failed"
        es.clear_block(tmp)
        st = es.load(tmp)
        assert st["status"] == "pending", st
        assert st["last_error"] is None, st
        assert st["failure_class"] == "unknown", st
        assert st["attempts"] == before["attempts"], st

        # failure_class 不匹配时不复位
        es.fail(tmp, "素材缺失", retryable=False, failure_class="asset_missing")
        es.clear_block(tmp)
        assert es.load(tmp)["status"] == "failed"

        # 清单清空后闸门放行
        jm.sync_subtitle_missing(tmp, repaired_eps={3})
        assert jm.require_export_allowed(tmp) == []
    print("OK clear_block：仅匹配的 subtitle_missing 复位，集号清理后放行")


def test_robust_auto_export_gate():
    path = os.path.join(_ROOT, ".trae", "skills", "jianying-editor",
                        "examples", "robust_auto_export.py")
    examples = os.path.dirname(path)
    if examples not in sys.path:
        sys.path.insert(0, examples)
    spec = importlib.util.spec_from_file_location("robust_auto_export_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    with tempfile.TemporaryDirectory() as root:
        jd = os.path.join(root, "测试剧", "cover_job")
        os.makedirs(jd)
        _new_job_dir(jd, [7])
        assert mod.subtitle_gate("测试剧串流", roots=[root]) == [7]

        assert mod.subtitle_gate("无此草稿", roots=[root]) == []
    print("OK robust_auto_export.subtitle_gate 直调入口可拦、找不到任务时放行")


def main():
    test_require_gate()
    test_cover_watch_blocked()
    test_cover_watch_dry_run_no_state()
    test_series_runner_blocked()
    test_clear_block()
    test_robust_auto_export_gate()
    print("\nALL OK: test_export_gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
