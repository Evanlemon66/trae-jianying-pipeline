# -*- coding: utf-8 -*-
"""缺字幕清单（derived.subtitle_missing）记录 / 清除 / 校验 / 兼容性测试。

独立脚本运行（不依赖 pytest）：
    python tests/test_subtitle_missing.py
"""
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LIB = os.path.join(_ROOT, "lib")
sys.path.insert(0, _LIB)

import job_manifest as jm  # noqa: E402

_PIPELINE = os.path.join(_ROOT, "pipeline")
sys.path.insert(0, _PIPELINE)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))


def _new_job_dir(tmp, missing=None):
    """建一个最小可校验 job（含 validate 要求的目录与简介），返回目录。"""
    for d in ("01_input", "02_titles", "03_prompts", "04_covers", "05_video", "06_publish"):
        os.makedirs(os.path.join(tmp, d), exist_ok=True)
    with open(os.path.join(tmp, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job("测试剧")
    if missing is not None:
        job["derived"]["subtitle_missing"] = missing
    jm.save_job(job, tmp)
    return tmp


def _read(job_dir):
    with open(os.path.join(job_dir, "job.json"), encoding="utf-8") as f:
        return json.load(f)


def test_build_and_migrate():
    job = jm.build_job("测试剧")
    assert job["derived"]["subtitle_missing"] == [], job

    legacy = jm.build_job("旧任务")
    legacy.pop("derived")
    migrated = jm.migrate_job(legacy)
    assert migrated.get("derived", {}).get("subtitle_missing") == [], migrated
    print("OK build_job/migrate_job 预置空清单")


def test_record_and_cross_process_read():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp)
        rev0 = _read(tmp)["revision"]
        out = jm.sync_subtitle_missing(tmp, failed_eps={8, 3, 3})
        assert out == [3, 8], out
        job = _read(tmp)
        assert job["derived"]["subtitle_missing"] == [3, 8], job
        assert job["revision"] == rev0 + 1, (job["revision"], rev0)
        assert any(h.get("event") == "subtitle_missing" for h in job["history"]), job["history"]

        # 跨进程可读：另一个 Python 进程读 job.json
        code = (
            "import json,sys;"
            f"d=json.load(open(r'{os.path.join(tmp, 'job.json')}',encoding='utf-8'));"
            "print(','.join(map(str,d['derived']['subtitle_missing'])))"
        )
        got = subprocess.check_output([sys.executable, "-c", code],
                                      cwd=_ROOT, text=True).strip()
        assert got == "3,8", got
        print("OK 记录：升序去重、revision/history、跨进程可读")


def test_clear_only_when_repaired():
    with tempfile.TemporaryDirectory() as tmp:
        _new_job_dir(tmp, missing=[3, 8])
        assert jm.sync_subtitle_missing(tmp, repaired_eps={8}) == [3]
        print("OK 清除：重剪且字幕非空 -> 移除")

        # 反例：只重跑、仍是零字幕 -> 继续记录，不消除
        assert jm.sync_subtitle_missing(tmp, failed_eps={3}, repaired_eps=()) == [3]
        assert jm.subtitle_missing(tmp) == [3]
        print("OK 反例：只重跑但无字幕 -> 清单保留")


def test_validate_rejects_bad_shapes():
    bad = [[3, 3], [8, 3], [0], ["3"], [True], 5, "3"]
    for value in bad:
        with tempfile.TemporaryDirectory() as tmp:
            _new_job_dir(tmp)
            job = _read(tmp)
            job["derived"]["subtitle_missing"] = value
            jm.save_job(job, tmp)
            try:
                jm.update_job(tmp, {"stage": "titles_done"})
            except ValueError:
                pass
            else:
                raise AssertionError(f"非法形态未被拒绝: {value!r}")
    print("OK validate 拒绝重复/降序/0/字符串/bool/非数组")


def test_missing_job_file():
    with tempfile.TemporaryDirectory() as tmp:
        assert jm.subtitle_missing(tmp) == []
        out = jm.sync_subtitle_missing(tmp, failed_eps={5})
        assert out == [5], out
        assert not os.path.isfile(os.path.join(tmp, "job.json")), "不应创建 job.json"
        assert jm.require_export_allowed(tmp) == []  # 文件不存在时闸门放行（由调用方内存兜底）
    print("OK job.json 缺失：只返回内存结论、不创建文件")


def test_classify_outcome():
    import pipeline as pl
    assert pl._classify_subtitle_outcome(3, asr_empty=True, appended=0) == "failed"
    assert pl._classify_subtitle_outcome(3, asr_empty=False, appended=0) == "failed"
    assert pl._classify_subtitle_outcome(3, asr_empty=False, appended=2) == "repaired"
    assert pl._classify_subtitle_outcome(None, asr_empty=False, appended=2) is None
    assert pl._classify_subtitle_outcome(0, asr_empty=False, appended=2) is None
    print("OK 单集判定：ASR 空/cut 后零条 -> failed，有产出 -> repaired")


def main():
    test_build_and_migrate()
    test_record_and_cross_process_read()
    test_clear_only_when_repaired()
    test_validate_rejects_bad_shapes()
    test_missing_job_file()
    test_classify_outcome()
    print("\nALL OK: test_subtitle_missing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
