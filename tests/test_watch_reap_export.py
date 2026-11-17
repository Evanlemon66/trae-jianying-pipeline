"""R11：watcher 每轮巡检回收 exporting 死记录（不依赖进入导出链）。"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _sub in ("lib", "pipeline"):
    sys.path.insert(0, os.path.join(ROOT, _sub))

import cover_watch as cw  # noqa: E402
import export_state as es  # noqa: E402
import job_manifest as jm  # noqa: E402


def _make_job(tmp_path, name):
    jd = tmp_path / name / "cover_job"
    os.makedirs(jd, exist_ok=True)
    job = jm.build_job("自检剧")
    job["video"]["draft_name"] = "自检剧"
    jm.save_job(job, str(jd))
    return str(jd)


def _write_state(job_dir, heartbeat_ts):
    st = es._default()
    st["status"] = "exporting"
    st["heartbeat_ts"] = heartbeat_ts
    st["updated_at"] = es._iso(heartbeat_ts)
    es._atomic_write(job_dir, st)


def test_stale_exporting_reaped_by_watch_round(tmp_path):
    jd = _make_job(tmp_path, "stale")
    _write_state(jd, time.time() - es.STALE_AFTER_S - 60)

    cw._reap_stale_export(jd)

    assert es._read_raw(jd)["status"] == "retrying"


def test_live_exporting_not_reaped(tmp_path):
    jd = _make_job(tmp_path, "live")
    _write_state(jd, time.time())

    cw._reap_stale_export(jd)

    assert es._read_raw(jd)["status"] == "exporting"


def test_progress_write_keeps_export_alive(tmp_path, monkeypatch):
    jd = _make_job(tmp_path, "progress")
    _write_state(jd, time.time() - es.STALE_AFTER_S - 60)

    # 子进程还在写 progress：巡检必须把 progress mtime 算进心跳
    out_mp4 = str(tmp_path / "out" / "自检剧" / "自检剧.mp4")
    monkeypatch.setattr(cw.pc, "export_mp4_path", lambda name: out_mp4)
    os.makedirs(os.path.dirname(out_mp4), exist_ok=True)
    with open(out_mp4 + ".progress.json", "w", encoding="utf-8") as f:
        f.write('{"status": "running", "percent": "约 40%"}')

    cw._reap_stale_export(jd)

    # 磁盘状态仍是 exporting（未回收）；不能只看游标，因为 load() 在缺 progress 参数时会
    # 在内存里推导 stale，不代表巡检真的落盘回收了。
    assert es._read_raw(jd)["status"] == "exporting"
