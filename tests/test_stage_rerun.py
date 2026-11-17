# -*- coding: utf-8 -*-
"""stage-rerun 验收（specs/stage-rerun/spec.md §11）。

覆盖：
  ① cover_bind 在 video_ready 成功，不动 video.outputs/export_state，不写 _export_stale
  ② cover_bind 后成片边（E2）仍是 valid(cover_only)
  ③ covers + cover_bind 全程不产生 ASR/翻译子进程
  ④ edit 在 video_ready 无 --force 拒绝；有 --force 执行且 stage 不变
  ⑤ export --versions a 只动 a，不碰 b
  ⑥ archived / 活锁 / exporting 拒绝且零写盘
  ⑦ cover_bind 写盘后失败 → 草稿/job 回滚到原字节
  ⑧ --resume 幂等；--rollback 恢复；--dry-run 零写盘

运行：python tests\\test_stage_rerun.py（纯临时目录）
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import job_manifest as jm  # noqa: E402
import cover_watch as cw  # noqa: E402
import freshness  # noqa: E402
import jy_cover  # noqa: E402
from bundle_fixture import png_cover  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "stage_rerun_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
DRAFTS = os.path.join(TMP, "drafts")
os.makedirs(DRAFTS)

NAME = "重跑测试剧"
DRAFT = NAME
DD = os.path.join(DRAFTS, DRAFT)
DRAFT_JSON = os.path.join(DD, "draft_content.json")


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def make_job(tag, *, stage="video_ready", outputs=None, export_state="exported"):
    jd = os.path.join(TMP, tag, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"prompts": []}, f)
    job = jm.build_job(NAME)
    job["stage"] = stage
    job["titles"] = {"gate": "done"}
    job["workflow"] = {"mode": "full"}
    job["video"] = {
        "status": "done" if stage in ("video_ready", "publish_ready", "published") else None,
        "export_state": export_state,
        "draft_name": DRAFT,
        "outputs": outputs or {"a": {"draft": DRAFT,
                                     "export_file": os.path.join(TMP, tag, f"{DRAFT}.mp4")}},
    }
    job["freshness"] = freshness.default_freshness()
    jm.save_job(job, jd)
    os.makedirs(DD, exist_ok=True)
    with open(DRAFT_JSON, "w", encoding="utf-8") as f:
        json.dump({"version": 360000, "fps": 30, "duration": 1000000,
                   "canvas_config": {"width": 1080, "height": 1920},
                   "materials": {}, "tracks": [], "cover": None}, f)
    cover = os.path.join(jd, "04_covers", "cover_04_vertical.png")
    for n in ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"):
        with open(os.path.join(jd, "04_covers", n + ".png"), "wb") as f:
            f.write(png_cover())
    return jd, cover


def set_e2_valid(jd, mp4):
    job = jm.load_job(os.path.join(jd, "job.json"))
    job["freshness"]["edges"][freshness.EDGE_E2] = {
        "state": "valid", "input_hash": "old", "output": {"path": mp4,
        "size_bytes": os.path.getsize(mp4) if os.path.isfile(mp4) else 0},
        "recorded_at": "2020-01-01T00:00:00+08:00"}
    jm.save_job(job, jd)


@contextlib.contextmanager
def isolated(jd, cover, *, set_cover=None, guard_popen=True):
    patches = [
        mock.patch.object(cw, "_draft_root", return_value=DRAFTS),
        mock.patch.object(freshness, "_drafts_root", return_value=DRAFTS),
        mock.patch.object(cw.jm, "resolve_profile_cover", return_value=cover),
        mock.patch.object(jy_cover, "set_draft_cover",
                          side_effect=set_cover or (lambda dd, cover_image=None: True)),
    ]
    if guard_popen:
        patches.append(mock.patch.object(cw.subprocess, "Popen",
                                         side_effect=AssertionError("不应拉起子进程")))
    with contextlib.ExitStack() as st:
        for p in patches:
            st.enter_context(p)
        yield


def run_cli(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cw.main(args)
    return rc, buf.getvalue()


def job_snapshot(jd):
    return (_sha(os.path.join(jd, "job.json")), _sha(DRAFT_JSON))


def main():
    print("== A cover_bind：video_ready 单版本重绑 ==")
    jd, cover = make_job("A")
    mp4 = os.path.join(TMP, "A", f"{DRAFT}.mp4")
    with open(mp4, "wb") as f:
        f.write(b"m" * 1024)
    set_e2_valid(jd, mp4)
    before = job_snapshot(jd)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "cover_bind",
                           "--versions", "a", "--yes"])
    job = jm.load_job(os.path.join(jd, "job.json"))
    check("A1 返回成功", rc == 0, out)
    check("A2 stage 不变", jm.canon_stage(job.get("stage")) == "video_ready", job.get("stage"))
    check("A3 未写 _export_stale", not os.path.isfile(os.path.join(DD, "_export_stale.json")))
    check("A4 export_state/outputs 未被改",
          job["video"].get("export_state") == "exported"
          and job["video"]["outputs"]["a"]["export_file"] == mp4, job["video"])
    check("A5 草稿确实写了封面", (job.get("freshness") or {}).get("edges") is not None
          and os.path.isfile(DRAFT_JSON))
    with isolated(jd, cover):
        e2 = freshness.freshness_of(jd, "a", "scheduler")
    check("A6 E2 保持 valid", e2.get("state") == "valid", e2)
    check("A7 E2 原因是 cover_only", e2.get("reason") == "cover_only", e2)
    check("A8 未动 stage/history 以外的 stage_before", before != job_snapshot(jd))

    print("\n== B covers 外部单元：只标 E3，不动 E1/E2 ==")
    jd, cover = make_job("B")
    job0 = jm.load_job(os.path.join(jd, "job.json"))
    e1_before = json.dumps(job0["freshness"]["edges"][freshness.EDGE_E1], sort_keys=True)
    e2_before = json.dumps(job0["freshness"]["edges"][freshness.EDGE_E2], sort_keys=True)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "covers", "--yes"])
    job = jm.load_job(os.path.join(jd, "job.json"))
    e3 = job["freshness"]["edges"][freshness.EDGE_E3]
    check("B1 返回成功", rc == 0, out)
    check("B2 E3=stale(covers_changed)", e3.get("state") == "stale"
          and e3.get("reason") == "covers_changed", e3)
    check("B3 E1 未变", json.dumps(job["freshness"]["edges"][freshness.EDGE_E1],
                                   sort_keys=True) == e1_before)
    check("B4 E2 未变", json.dumps(job["freshness"]["edges"][freshness.EDGE_E2],
                                   sort_keys=True) == e2_before)
    check("B5 文案标注外部生成单元", "外部生成单元" in out, out)

    print("\n== C/D edit：无 --force 拒绝；--force 执行 ==")
    jd, cover = make_job("C")
    snap = job_snapshot(jd)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "edit", "--yes"])
    check("C1 无 --force 返回失败", rc == 1, out)
    check("C2 零写盘", job_snapshot(jd) == snap)
    with isolated(jd, cover), mock.patch.object(cw, "_run_rerun_cmd", return_value=0) as cmd:
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "edit",
                           "--versions", "a", "--force", "--yes"])
    check("D1 --force 成功", rc == 0, out)
    check("D2 stage 仍为 video_ready",
          jm.canon_stage(jm.load_job(os.path.join(jd, "job.json"))["stage"]) == "video_ready")
    check("D3 写了失效标记", os.path.isfile(os.path.join(DD, "_export_stale.json")))
    check("D4 调用了 batch_runner", cmd.called and "batch_runner.py" in " ".join(cmd.call_args[0][0]), cmd.call_args)

    print("\n== E export：--versions a 只动 a ==")
    jd, cover = make_job("E", outputs={
        "a": {"draft": DRAFT, "export_file": os.path.join(TMP, "E", f"{DRAFT}.mp4")},
        "b": {"draft": DRAFT + "_b", "export_file": os.path.join(TMP, "E", f"{DRAFT}_b.mp4")},
    })
    os.makedirs(os.path.join(DRAFTS, DRAFT + "_b"), exist_ok=True)
    with open(os.path.join(DRAFTS, DRAFT + "_b", "draft_content.json"), "w",
              encoding="utf-8") as f:
        json.dump({"version": 360000, "fps": 30, "duration": 1, "materials": {},
                   "tracks": []}, f)
    os.makedirs(os.path.join(jd, "04_covers"), exist_ok=True)
    with open(dark := os.path.join(jd, "04_covers", "cover_04_vertical.png"), "wb") as f:
        f.write(png_cover())
    captured = {}
    def fake_export(job_dir, plan, cfg, **kw):
        captured.update(plan)
        return True
    with isolated(jd, dark), mock.patch.object(cw, "run_export", side_effect=fake_export):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "export",
                           "--versions", "a", "--yes"])
    check("E1 返回成功", rc == 0, out)
    check("E2 只导出 a", captured.get("export_targets") == [DRAFT], captured)
    check("E3 强制重导", captured.get("force_reexport") is True, captured)
    check("E4 profiles 只含 a", captured.get("profiles") == ["a"], captured)
    check("E5 b 草稿未被删", os.path.isfile(os.path.join(DRAFTS, DRAFT + "_b",
                                                         "draft_content.json")))

    print("\n== F 拒绝路径：archived / 活锁 / exporting ==")
    jd, cover = make_job("F")
    job = jm.load_job(os.path.join(jd, "job.json"))
    job["stage"] = "archived"
    jm.save_job(job, jd)
    snap = job_snapshot(jd)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "cover_bind",
                           "--versions", "a", "--yes"])
    check("F1 archived 拒绝", rc == 1 and "archived" in out, out)
    check("F2 archived 零写盘", job_snapshot(jd) == snap)

    jd, cover = make_job("F2")
    with open(os.path.join(jd, cw.LOCK_NAME), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "job": jd, "phase": "export"}, f)
    snap = job_snapshot(jd)
    with isolated(jd, cover, guard_popen=False):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "cover_bind",
                           "--versions", "a", "--yes"])
    check("F3 活锁拒绝", rc == 1 and "存活" in out, out)
    check("F4 活锁零写盘", job_snapshot(jd) == snap)

    jd, cover = make_job("F3")
    os.makedirs(os.path.join(jd, "06_publish"), exist_ok=True)
    with open(os.path.join(jd, ".export_state.json"), "w", encoding="utf-8") as f:
        json.dump({"status": "exporting"}, f)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "export",
                           "--versions", "a", "--yes"])
    check("F5 exporting 拒绝", rc == 1 and "exporting" in out, out)

    print("\n== G 失败回滚：写盘后抛错 ==")
    jd, cover = make_job("G")
    orig = _sha(DRAFT_JSON)
    def boom(dd, cover_image=None):
        raise RuntimeError("模拟 draft_cover 写入失败")
    with isolated(jd, cover, set_cover=boom):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "cover_bind",
                           "--versions", "a", "--yes"])
    job = jm.load_job(os.path.join(jd, "job.json"))
    hist = [h.get("event") for h in job.get("history") or []]
    check("G1 返回失败", rc == 1, out)
    check("G2 草稿回滚到原字节", _sha(DRAFT_JSON) == orig)
    check("G3 history 有 rolled_back", any(str(h).endswith("rolled_back") for h in hist), hist)
    check("G4 无 .tmp 残留", not os.path.isfile(DRAFT_JSON + ".tmp"))
    jdirs = os.listdir(os.path.join(jd, "06_publish", cw.RERUN_DIR))
    jdir = os.path.join(jd, "06_publish", cw.RERUN_DIR, jdirs[0])
    check("G5 journal 有 failed.json", os.path.isfile(os.path.join(jdir, "failed.json")))

    print("\n== H --rollback / --resume ==")
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--rollback", jdir])
    check("H1 rollback 成功", rc == 0, out)
    check("H2 草稿仍是原字节", _sha(DRAFT_JSON) == orig)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--resume", jdir, "--yes"])
    check("H3 resume 完成", rc == 0, out)
    check("H4 resume 后封面写入生效",
          _sha(DRAFT_JSON) != orig and os.path.isfile(DRAFT_JSON))
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--resume", jdir, "--yes"])
    check("H5 再次 resume 幂等（已完成直接返回）", rc == 0 and "已完成" in out, out)

    print("\n== I --dry-run 零写盘 ==")
    jd, cover = make_job("I")
    snap = job_snapshot(jd)
    with isolated(jd, cover):
        rc, out = run_cli(["rerun", "--job", jd, "--unit", "cover_bind",
                           "--versions", "a", "--dry-run"])
    check("I1 dry-run 成功", rc == 0, out)
    check("I2 零写盘", job_snapshot(jd) == snap)
    check("I3 未建 journal", not os.path.isdir(os.path.join(jd, "06_publish", cw.RERUN_DIR)))
    check("I4 未取锁", not os.path.isfile(os.path.join(jd, cw.LOCK_NAME)))

    print("\n" + ("=" * 52))
    if fails:
        print(f"❌ {len(fails)} 项失败: {fails}")
        sys.exit(1)
    print("✅ stage-rerun 全部通过")


if __name__ == "__main__":
    main()
