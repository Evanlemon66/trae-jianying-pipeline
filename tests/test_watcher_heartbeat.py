# -*- coding: utf-8 -*-
"""回归用例：watcher 心跳（进程存活信号）原子写与字段。

运行：python tests\\test_watcher_heartbeat.py（纯临时目录，不碰真实剧）
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import cover_watch as cw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def read_hb(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    tmp = tempfile.mkdtemp(prefix="watcher_hb_")
    try:
        path = os.path.join(tmp, cw.HEARTBEAT_NAME)
        hb = cw._WatcherHeartbeat(work_dir=tmp, interval=0.1)
        hb.start()
        time.sleep(0.05)

        check("心跳文件已创建", os.path.isfile(path))
        data = read_hb(path)
        check("含 pid", data.get("pid") == os.getpid(), data.get("pid"))
        check("含 updated_ts", isinstance(data.get("updated_ts"), (int, float)))
        check("含 started_ts", isinstance(data.get("started_ts"), (int, float)))
        check("初始 stage=starting", data.get("stage") == "starting", data.get("stage"))
        check("state_io 补 updated_at", bool(data.get("updated_at")))
        check("state_io 补 version", data.get("version") == 1, data.get("version"))
        check("含 code_fingerprint 且与当前代码一致",
              data.get("code_fingerprint") == cw._code_fingerprint(),
              data.get("code_fingerprint"))

        hb.update(stage="edit", job="D:/剧/cover_job", phase="running", round=7)
        data = read_hb(path)
        check("update 后 stage=edit", data.get("stage") == "edit", data.get("stage"))
        check("update 后 job 已写入", data.get("job") == "D:/剧/cover_job", data.get("job"))
        check("update 后 phase=running", data.get("phase") == "running", data.get("phase"))
        check("update 后 round=7", data.get("round") == 7, data.get("round"))

        first = data.get("updated_ts")
        time.sleep(0.35)
        data = read_hb(path)
        check("后台线程持续刷新 updated_ts", data.get("updated_ts", 0) > first,
              (first, data.get("updated_ts")))

        hb.stop()
        check("stop 后线程退出", hb._thread is not None and not hb._thread.is_alive())

        print("\n== 优雅停止哨兵 ==")
        stop_path = os.path.join(tmp, cw.STOP_NAME)
        check("无哨兵 → take_stop_request 返回 False",
              cw.take_stop_request(tmp) is False)
        with open(stop_path, "w", encoding="utf-8") as f:
            f.write("stop\n")
        check("有哨兵 → 返回 True", cw.take_stop_request(tmp) is True)
        check("消费后哨兵被删除（下次启动不会立刻退出）", not os.path.isfile(stop_path))

        print("\n== 放行前代码指纹校验 ==")
        hb_path = os.path.join(tmp, "guard_heartbeat.json")

        def write_hb(**kw):
            payload = {"pid": os.getpid(), "updated_ts": time.time()}
            payload.update(kw)
            with open(hb_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)

        check("无心跳（watcher 没跑）→ 放行",
              cw.check_release_safe("edit", heartbeat_path=hb_path) is None)
        write_hb(updated_ts=time.time() - 600, code_fingerprint="deadbeef")
        check("心跳过期 → 按没在跑放行",
              cw.check_release_safe("edit", heartbeat_path=hb_path) is None)
        write_hb(pid=2 ** 31 - 1, code_fingerprint="deadbeef")
        check("pid 已退出 → 按没在跑放行",
              cw.check_release_safe("edit", heartbeat_path=hb_path) is None)
        write_hb(code_fingerprint="deadbeef")
        reason = cw.check_release_safe("edit", heartbeat_path=hb_path)
        check("在跑且指纹不一致 → 拒绝",
              reason is not None and "仍运行旧代码" in reason, reason)
        check("拒绝提示含「重启」", reason is not None and "重启" in reason, reason)
        write_hb()
        reason2 = cw.check_release_safe("edit", heartbeat_path=hb_path)
        check("旧版心跳无指纹 → 拒绝",
              reason2 is not None and "旧代码" in reason2, reason2)
        write_hb(code_fingerprint=cw._code_fingerprint())
        check("在跑且指纹一致 → 放行",
              cw.check_release_safe("edit", heartbeat_path=hb_path) is None)

        print("\n== 指纹文件集：导出链路模块必须纳入 ==")
        required = [
            "lib/cover_watch.py", "lib/job_manifest.py", "lib/edit_state.py",
            "lib/export_state.py", "lib/failure_class.py", "lib/export_lock.py",
            "lib/state_io.py", "lib/subtitle_guard.py",
            "pipeline/batch_runner.py", "pipeline/pipeline.py", "pipeline/preset_ops.py",
            "pipeline/series_runner.py", "pipeline/export_config.py",
            "pipeline/no_window.py", "pipeline/verify_export.py",
            ".trae/skills/jianying-editor/examples/robust_auto_export.py",
            ".trae/skills/jianying-editor/examples/jy_instance.py",
        ]
        missing = [r for r in required if r not in cw.CODE_FINGERPRINT_FILES]
        check("导出链路模块全部纳入指纹", not missing, missing)
        absent = [r for r in cw.CODE_FINGERPRINT_FILES
                  if not os.path.isfile(os.path.join(_ROOT, r.replace("/", os.sep)))]
        check("指纹文件都真实存在（不会退化成 <missing>）", not absent, absent)

        # 机制校验：被覆盖文件内容变化 → 指纹必须变化（临时目录内模拟，不碰真实文件）
        real_fp = cw._code_fingerprint()
        fp_root = os.path.join(tmp, "fp_repo")
        os.makedirs(fp_root)
        fake = os.path.join(fp_root, "series_runner.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("VALUE = 1\n")
        old_repo, old_files = cw._REPO, cw.CODE_FINGERPRINT_FILES
        try:
            cw._REPO = fp_root
            cw.CODE_FINGERPRINT_FILES = ("series_runner.py",)
            fp1 = cw._code_fingerprint()
            with open(fake, "w", encoding="utf-8") as f:
                f.write("VALUE = 2\n")
            fp2 = cw._code_fingerprint()
        finally:
            cw._REPO, cw.CODE_FINGERPRINT_FILES = old_repo, old_files
        check("被覆盖文件内容变化 → 指纹变化", fp1 != fp2 and len(fp1) == 12, (fp1, fp2))
        check("恢复文件集后指纹回到真实值", cw._code_fingerprint() == real_fp)

        print("\n== cmd_watch --phase 集成：旧代码拒绝放行 ==")
        cfg_path = os.path.join(tmp, "watch.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({}, f)
        main_hb = os.path.join(tmp, cw.HEARTBEAT_NAME)
        with open(main_hb, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "updated_ts": time.time(),
                       "code_fingerprint": "deadbeef"}, f)
        old_work = cw._WORK_DIR
        cw._WORK_DIR = tmp
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = cw.main(["--job", os.path.join(tmp, "cover_job"),
                              "--config", cfg_path, "--once", "--phase", "edit"])
        finally:
            cw._WORK_DIR = old_work
        check("--phase 被拒绝（返回 1）", rc == 1, rc)
        check("输出明确提示 watcher 仍在跑旧代码",
              "仍运行旧代码" in out.getvalue() and "重启" in out.getvalue(),
              out.getvalue())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nFAILED: {len(fails)} 项 → {fails}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
