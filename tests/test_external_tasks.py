# -*- coding: utf-8 -*-
"""外部任务只读快照的纯函数测试（不依赖真实进程/窗口）。"""
import json
import os
import sys

_CONSOLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "pipeline_ui", "console")
# 只在本模块导入期间借用 console 目录，用完立刻移除，
# 避免 shadow 掉 pipeline/doctor.py（tests/test_doctor.py 依赖它）。
sys.path.insert(0, _CONSOLE)
try:
    import status  # noqa: E402
finally:
    sys.path.remove(_CONSOLE)


def test_cmd_series_dir_takes_first_positional():
    cmd = (r'"C:\Python312\python.exe" pipeline\batch_runner.py '
           r'"D:\短剧\甲剧" --draft-prefix 甲剧 --cut 2')
    assert status.cmd_series_dir(cmd, "batch_runner.py") == r"D:\短剧\甲剧"


def test_cmd_series_dir_skips_value_flags():
    cmd = (r'"py.exe" pipeline\batch_runner.py --draft-prefix 前缀 '
           r'--delay 5 "D:\短剧\乙剧"')
    assert status.cmd_series_dir(cmd, "batch_runner.py") == r"D:\短剧\乙剧"


def test_cmd_series_dir_ui_uses_video_dir():
    cmd = (r'"py.exe" pipeline_ui\ui_pipeline.py '
           r'--video-dir "D:\短剧\丙剧" --workdir D:\tmp\w1')
    assert status.cmd_series_dir(cmd, "ui_pipeline.py") == r"D:\短剧\丙剧"


def test_cmd_workdir_uses_flag_or_default():
    series = r"D:\短剧\甲剧"
    cmd = r'"py.exe" pipeline\batch_runner.py "D:\短剧\甲剧"'
    default = status.cmd_workdir(cmd, "batch_runner.py", series)
    assert os.path.basename(default).startswith("_work_")
    explicit = cmd + r" --workdir D:\tmp\w2"
    assert status.cmd_workdir(explicit, "batch_runner.py", series) == r"D:\tmp\w2"


def test_batch_state_summary_counts_progress(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    state = {
        "a.mp4": {"status": "done", "ep": 1},
        "b.mp4": {"status": "done", "ep": 2},
        "c.mp4": {"status": "running", "ep": 3},
        "d.mp4": {"status": "failed", "ep": 4},
    }
    (workdir / "_batch_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    summary = status.batch_state_summary(str(workdir))
    assert summary["total"] == 4
    assert summary["done"] == 2
    assert summary["failed"] == 1
    assert summary["running"] == 1
    assert summary["percent"] == 75
    assert "第3集处理中" in summary["stage"]
    assert "失败 1" in summary["detail"]


def test_batch_state_summary_missing_returns_none(tmp_path):
    assert status.batch_state_summary(str(tmp_path)) is None


def test_batch_state_summary_ignores_run_key(tmp_path):
    """_batch_state.json 里的 run 快照不是集，不能计入 total。"""
    workdir = tmp_path / "work"
    workdir.mkdir()
    state = {
        "run": {"run_id": "r1", "status": "ok", "exit_code": 0},
        "a.mp4": {"status": "done", "ep": 1},
        "b.mp4": {"status": "running", "ep": 2},
    }
    (workdir / "_batch_state.json").write_text(
        json.dumps(state, ensure_ascii=False), encoding="utf-8")
    summary = status.batch_state_summary(str(workdir))
    assert summary["total"] == 2
    assert summary["done"] == 1
    assert summary["run"]["run_id"] == "r1"


def test_run_record_roundtrip(tmp_path):
    """batch_runner 落 _run.json / run-<id>.log，收尾写状态与退出码。"""
    pipeline = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "pipeline")
    sys.path.insert(0, pipeline)
    try:
        import batch_runner as br
        workdir = str(tmp_path / "w")
        os.makedirs(workdir)
        br._start_run(workdir, series_dir="剧A")
        rec = json.loads(open(os.path.join(workdir, "_run.json"), encoding="utf-8").read())
        assert rec["status"] == "running" and rec["run_id"]
        assert rec["series_dir"] == "剧A" and rec["argv"]
        assert os.path.isfile(rec["log_path"])
        print("tee 测试行")
        br._finish_run(0)
        rec = json.loads(open(os.path.join(workdir, "_run.json"), encoding="utf-8").read())
        assert rec["status"] == "ok" and rec["exit_code"] == 0 and rec["ended_at"]
        log = open(rec["log_path"], encoding="utf-8").read()
        assert "tee 测试行" in log
        state = json.loads(open(os.path.join(workdir, "_batch_state.json"),
                                encoding="utf-8").read())
        assert state["run"]["run_id"] == rec["run_id"]
        # 恢复现场，避免影响后续测试的 stdout
        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
    finally:
        sys.path.remove(pipeline)
