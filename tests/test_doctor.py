# -*- coding: utf-8 -*-
"""doctor 只读自检：缺件时要有可读的"缺什么 / 怎么补"。"""
import json
from types import SimpleNamespace

import doctor


def _make_repo(tmp_path, complete=True):
    files = [
        "lib/jianying_launch.py",
        "pipeline/export_config.py",
        "pipeline/presets.example.json",
        ".trae/skills/jianying-editor/SKILL.md",
        ".trae/skills/jianying-editor/examples/robust_auto_export.py",
    ]
    for rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    if complete:
        (tmp_path / "pipeline" / "presets.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_repo_layout_missing_explains_required_layout(tmp_path):
    r = doctor.check_repo_layout(str(tmp_path))
    assert r.status == "fail"
    assert "lib/jianying_launch.py" in r.detail
    assert "<repo>/.trae/skills/" in r.fix


def test_repo_layout_complete_passes(tmp_path):
    r = doctor.check_repo_layout(str(_make_repo(tmp_path)))
    assert r.status == "pass"


def test_repo_layout_without_personal_config_warns(tmp_path):
    r = doctor.check_repo_layout(str(_make_repo(tmp_path, complete=False)))
    assert r.status == "warn"
    assert "presets.json" in r.detail
    assert "presets.example.json" in r.fix


def test_python_version_gate():
    assert doctor.check_python((3, 9, 18)).status == "fail"
    assert "3.10" in doctor.check_python((3, 9, 18)).detail
    assert doctor.check_python((3, 10, 0)).status == "warn"
    assert doctor.check_python((3, 12, 4)).status == "pass"


def test_dependencies_missing_core_is_fail_and_says_how_to_fix():
    r = doctor.check_dependencies(find_spec=lambda _name: None)
    assert r.status == "fail"
    assert "pillow" in r.detail
    assert "pip install -r requirements.txt" in r.fix


def test_dependencies_only_ai_missing_is_warn():
    def finder(name):
        return object() if name in doctor.CORE_MODULES else None

    r = doctor.check_dependencies(find_spec=finder)
    assert r.status == "warn"
    assert "demucs" in r.detail


def test_dependencies_all_present_passes():
    r = doctor.check_dependencies(find_spec=lambda _name: object())
    assert r.status == "pass"


def test_ffmpeg_missing_reports_both_and_fix():
    r = doctor.check_ffmpeg(ffmpeg_path="", ffprobe_path="", which=lambda _n: None)
    assert r.status == "fail"
    assert "ffmpeg" in r.detail and "ffprobe" in r.detail
    assert "install_deps.ps1" in r.fix


def test_ffmpeg_present_passes(tmp_path):
    ff = tmp_path / "ffmpeg.exe"
    fp = tmp_path / "ffprobe.exe"
    ff.write_bytes(b"")
    fp.write_bytes(b"")
    r = doctor.check_ffmpeg(ffmpeg_path=str(ff), ffprobe_path=str(fp), which=lambda _n: None)
    assert r.status == "pass"
    assert str(ff) in r.detail and str(fp) in r.detail


def test_test_deps_missing_is_warn(monkeypatch):
    import doctor_core
    monkeypatch.setattr(doctor_core.importlib.util, "find_spec", lambda name: None)
    r = doctor.check_test_deps()
    assert r.status == "warn"
    assert "requirements-dev.txt" in r.fix
    assert doctor.DoctorReport("X", [r]).failed == []


def test_config_path_missing_input_is_fail_with_env_hint(tmp_path):
    results = doctor.check_config_paths([
        ("projects_root", "草稿根", str(tmp_path / "missing"), False, "JY_PROJECTS_ROOT"),
    ])
    assert len(results) == 1
    assert results[0].status == "fail"
    assert "JY_PROJECTS_ROOT" in results[0].fix


def test_config_path_missing_output_is_warn(tmp_path):
    results = doctor.check_config_paths([
        ("export_dir", "导出根", str(tmp_path / "new-out"), True, "JY_EXPORT_DIR"),
    ])
    assert results[0].status == "warn"
    assert "首次导出时创建" in results[0].detail


def test_config_path_existing_is_pass(tmp_path):
    target = tmp_path / "exists"
    target.mkdir()
    results = doctor.check_config_paths([
        ("projects_root", "草稿根", str(target), False, "JY_PROJECTS_ROOT"),
    ])
    assert results[0].status == "pass"
    assert "可写" in results[0].detail


def test_draft_index_missing_meta_warns(tmp_path):
    root = tmp_path / "drafts"
    root.mkdir()
    r = doctor.check_draft_index(str(root))
    assert r.status == "warn"
    assert "root_meta_info.json" in r.detail


def test_draft_index_mismatch_warns(tmp_path):
    root = tmp_path / "drafts"
    root.mkdir()
    (root / "root_meta_info.json").write_text(
        json.dumps({"draft_root_path": str(tmp_path / "other")}), encoding="utf-8")
    r = doctor.check_draft_index(str(root))
    assert r.status == "warn"
    assert "不一致" in r.detail


def test_draft_index_matched_passes(tmp_path):
    root = tmp_path / "drafts"
    draft = root / "demo"
    draft.mkdir(parents=True)
    (draft / "draft_content.json").write_text("{}", encoding="utf-8")
    (root / "root_meta_info.json").write_text(
        json.dumps({"draft_root_path": str(root)}), encoding="utf-8")
    r = doctor.check_draft_index(str(root))
    assert r.status == "pass"
    assert "1 个" in r.detail


def test_jianying_install_version_gate_message():
    r = doctor.check_jianying_install(inspect_fn=lambda: {
        "ok": False, "message": "仅支持剪映 5.9.0.11632，检测到: 6.0.0.1",
        "problems": [],
    })
    assert r.status == "fail"
    assert "5.9.0.11632" in r.detail
    assert "JY_JIANYING_DIR" in r.fix


def test_jianying_install_ok():
    r = doctor.check_jianying_install(inspect_fn=lambda: {
        "ok": True, "version": "5.9.0.11632", "exe": r"D:\JianyingPro.exe",
    })
    assert r.status == "pass"
    assert "5.9.0.11632" in r.detail


def test_report_format_includes_fix_and_summary():
    report = doctor.DoctorReport("X", [
        doctor.CheckResult("a", "A", "fail", "缺 A", "补 A"),
        doctor.CheckResult("b", "B", "pass", "有 B"),
    ])
    text = doctor.format_report(report)
    assert "[缺失] A: 缺 A" in text
    assert "修复: 补 A" in text
    assert "1 项缺失" in text


def test_jianying_session_never_starts_process():
    r = doctor.check_jianying_session(process_running_fn=lambda: False)
    assert "剪映进程未运行" in r.detail
    assert r.status in ("pass", "warn", "fail")


def _task_runner(returncode, stdout=""):
    return lambda _script: SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def test_watcher_task_unregistered_warns_without_blocking(tmp_path):
    r = doctor.check_watcher_task(runner=_task_runner(1),
                                  pause_path=str(tmp_path / "no.pause"))
    assert r.status == "warn"
    assert "未注册" in r.detail
    assert "本机运维信息" in r.detail
    assert doctor.DoctorReport("X", [r]).failed == []


def test_watcher_task_registered_and_enabled_passes(tmp_path):
    r = doctor.check_watcher_task(runner=_task_runner(0, "Ready|True"),
                                  pause_path=str(tmp_path / "no.pause"))
    assert r.status == "pass"
    assert "已注册并启用" in r.detail and "本机运维信息" in r.detail


def test_watcher_task_notes_pause_sentinel(tmp_path):
    pause = tmp_path / "watcher.pause"
    pause.write_text("paused", encoding="utf-8")
    r = doctor.check_watcher_task(runner=_task_runner(0, "Ready|True"),
                                  pause_path=str(pause))
    assert r.status == "pass"
    assert "自动拉起已暂停" in r.detail


def test_watcher_task_disabled_warns(tmp_path):
    r = doctor.check_watcher_task(runner=_task_runner(0, "Disabled|False"),
                                  pause_path=str(tmp_path / "no.pause"))
    assert r.status == "warn"
    assert "已禁用" in r.detail


def test_watcher_runtime_fresh_heartbeat_passes(tmp_path):
    hb = tmp_path / "watcher_heartbeat.json"
    hb.write_text(json.dumps({"pid": 123, "updated_ts": 1000.0}), encoding="utf-8")
    r = doctor.check_watcher_runtime(heartbeat_path=str(hb), lock_paths=[],
                                     pause_path=str(tmp_path / "no.pause"),
                                     stop_path=str(tmp_path / "no.stop"),
                                     now=1030.0, pid_alive=lambda pid: pid == 123,
                                     stall_seconds=900)
    assert r.status == "pass"
    assert "pid=123" in r.detail


def test_watcher_runtime_stale_heartbeat_warns(tmp_path):
    hb = tmp_path / "watcher_heartbeat.json"
    hb.write_text(json.dumps({"pid": 123, "updated_ts": 1000.0}), encoding="utf-8")
    r = doctor.check_watcher_runtime(heartbeat_path=str(hb), lock_paths=[],
                                     pause_path=str(tmp_path / "no.pause"),
                                     stop_path=str(tmp_path / "no.stop"),
                                     now=5000.0, pid_alive=lambda _pid: True,
                                     stall_seconds=900)
    assert r.status == "warn"
    assert "可能卡死" in r.detail


def test_watcher_runtime_missing_heartbeat_falls_back_to_lock(tmp_path):
    lock = tmp_path / ".watcher.lock"
    lock.write_text(json.dumps({"pid": 456, "phase": "export", "job": str(tmp_path)}),
                    encoding="utf-8")
    r = doctor.check_watcher_runtime(heartbeat_path=str(tmp_path / "missing.json"),
                                     lock_paths=[str(lock)],
                                     pause_path=str(tmp_path / "no.pause"),
                                     stop_path=str(tmp_path / "no.stop"),
                                     now=5000.0,
                                     pid_alive=lambda pid: pid == 456, stall_seconds=900)
    assert r.status == "warn"
    assert "pid=456" in r.detail
    assert ".watcher.lock" in r.detail


def test_watcher_runtime_not_running_is_warn_only(tmp_path):
    r = doctor.check_watcher_runtime(heartbeat_path=str(tmp_path / "missing.json"),
                                     lock_paths=[],
                                     pause_path=str(tmp_path / "no.pause"),
                                     stop_path=str(tmp_path / "no.stop"),
                                     now=5000.0,
                                     pid_alive=lambda _pid: False, stall_seconds=900)
    assert r.status == "warn"
    assert "未检测到运行中的 watcher" in r.detail
    assert doctor.DoctorReport("X", [r]).failed == []


def test_watcher_runtime_paused_but_running_warns(tmp_path):
    hb = tmp_path / "watcher_heartbeat.json"
    hb.write_text(json.dumps({"pid": 123, "updated_ts": 1000.0}), encoding="utf-8")
    pause = tmp_path / "watcher.pause"
    pause.write_text("paused", encoding="utf-8")
    r = doctor.check_watcher_runtime(heartbeat_path=str(hb), lock_paths=[],
                                     pause_path=str(pause), now=1030.0,
                                     pid_alive=lambda pid: pid == 123, stall_seconds=900)
    assert r.status == "warn"
    assert "自动拉起已暂停" in r.detail
    assert "不会被重新拉起" in r.detail


def test_watcher_runtime_stop_requested_warns(tmp_path):
    hb = tmp_path / "watcher_heartbeat.json"
    hb.write_text(json.dumps({"pid": 123, "updated_ts": 1000.0}), encoding="utf-8")
    stop = tmp_path / "watcher.stop"
    stop.write_text("stop", encoding="utf-8")
    r = doctor.check_watcher_runtime(heartbeat_path=str(hb), lock_paths=[],
                                     pause_path=str(tmp_path / "no.pause"),
                                     stop_path=str(stop), now=1030.0,
                                     pid_alive=lambda pid: pid == 123, stall_seconds=900)
    assert r.status == "warn"
    assert "已请求优雅停止" in r.detail
    assert "撤销停止请求" in r.fix


def test_watcher_runtime_paused_and_stopped_points_to_resume(tmp_path):
    pause = tmp_path / "watcher.pause"
    pause.write_text("paused", encoding="utf-8")
    r = doctor.check_watcher_runtime(heartbeat_path=str(tmp_path / "missing.json"),
                                     lock_paths=[], pause_path=str(pause),
                                     stop_path=str(tmp_path / "no.stop"), now=5000.0,
                                     pid_alive=lambda _pid: False, stall_seconds=900)
    assert r.status == "warn"
    assert "watcher.pause" in r.detail
    assert "恢复自动拉起" in r.fix
