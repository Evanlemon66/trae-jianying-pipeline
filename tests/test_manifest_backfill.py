# -*- coding: utf-8 -*-
"""record-video 路径登记 + manifest 回写 + clean 兜底测试。

覆盖（对应「导出成功却重复导出 / 清理静默漏删」两个 bug）：
  A. manifest 缺 paths.export_file 时不传参 → 登记失败（旧行为，防止回退）
  B. 显式传 export_dir/export_file/draft_name → 登记成功、export_state=exported、
     build_plan 不再判为需要导出；manifest 自动补写 export_file（其余字段不动）
  C. clean 在 manifest 缺字段时回落到 job.json.video.export_file，删掉成品并回写 cleaned
  D. 两处都无成品路径：无「已导出」证据 → 明确告警；有证据 → 报错返回 2

运行：python tests\\test_manifest_backfill.py（纯临时目录）
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import stream_manifest as sm  # noqa: E402
import cover_watch as cw  # noqa: E402

# 不弹真实桌面通知、不看本机配置
cw.toast = lambda *a, **k: None
cw.pc.get_watch_config = lambda: {"edit_pause_after": 3}

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "manifest_backfill_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
MANIFESTS = os.path.join(TMP, "manifests")
sm.MANIFEST_DIR = MANIFESTS          # clean 侧读目录
jm.STREAM_MANIFEST_DIR = MANIFESTS   # record_video 探测/回写目录


def make(name, *, video=None, manifest_export=False):
    """源目录 + cover_job + manifest（可缺 export_file）+ 成品 bundle。"""
    root = os.path.join(TMP, name)
    src = os.path.join(root, "系列源目录")
    jd = os.path.join(src, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(name)
    job["stage"] = "covers_ready"
    if video:
        job["video"].update(video)
    jm.save_job(job, jd)

    jy = os.path.join(src, "__jycache__")
    os.makedirs(jy, exist_ok=True)
    work = os.path.join(TMP, f"pipeline/_work_{name}")
    os.makedirs(work, exist_ok=True)
    draft = os.path.join(TMP, "drafts", name)
    os.makedirs(draft, exist_ok=True)
    bundle = os.path.join(TMP, "L", name)
    os.makedirs(bundle, exist_ok=True)
    mp4 = os.path.join(bundle, f"{name}.mp4")
    with open(mp4, "wb") as f:
        f.write(b"m" * 8192)
    kw = dict(source_dir=src, jycache_dir=jy, workdir=work, draft_dir=draft)
    if manifest_export:
        kw["export_file"] = mp4
    sm.record(name, **kw)
    return {"jd": jd, "bundle": bundle, "mp4": mp4, "draft": draft,
            "manifest": sm._manifest_path(name)}


def read_job(jd):
    return jm.load_job(os.path.join(jd, "job.json"))


def run_clean(name):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sm.clean(name)
    return rc, buf.getvalue()


# ── A：manifest 缺字段 + 不传参 → 仍失败（旧坑不复现为静默成功）──────
print("\n== A 缺 manifest 字段时不传参应失败 ==")
p = make("mbA")
try:
    jm.record_video(p["jd"])
    check("A1 缺 manifest 且无参数应抛 ValueError", False, "no raise")
except ValueError as e:
    check("A1 缺 manifest 且无参数应抛 ValueError", True)
    check("A2 错误信息提示补 export 路径", "export" in str(e), str(e))

# ── B：显式传参 → 登记成功 + 回写 manifest + build_plan 不再导出 ──────
print("\n== B 显式传参登记并回写 manifest ==")
p = make("mbB")
job = jm.record_video(p["jd"], export_dir=p["bundle"], export_file=p["mp4"],
                      draft_name="mbB")
check("B1 export_state=exported", job["video"]["export_state"] == "exported", job["video"])
check("B2 job.export_dir 已记录",
      (job["video"]["export_dir"] or "").endswith("/L/mbB"), job["video"])
check("B3 job.export_file 已记录",
      (job["video"]["export_file"] or "").endswith("mbB.mp4"), job["video"])
with open(p["manifest"], encoding="utf-8") as f:
    mani = json.load(f)
paths = mani.get("paths", {})
check("B4 manifest 已回写 export_file",
      (paths.get("export_file") or "").replace("\\", "/").endswith("mbB.mp4"), paths)
check("B5 manifest 不写 export_dir（避免 show_all 重复计体积）",
      "export_dir" not in paths, paths)
check("B6 manifest 其余字段未被破坏",
      paths.get("source_dir") and paths.get("draft_dir") and paths.get("workdir"),
      paths)
plan = cw.build_plan(p["jd"], read_job(p["jd"]), {"export_policy": "immediate"}, 0,
                     notify=False, write_log=False)
check("B7 build_plan 不再判定需要导出", plan.get("action") != "export", plan)
check("B8 build_plan 原因为已导出", "已导出" in plan.get("reason", ""), plan)

# ── C：clean 回退 job.json 并真正删成品 / 回写 cleaned ───────────────
print("\n== C clean 回落 job.json ==")
p = make("mbC", video={"export_file": "", "export_dir": None,
                       "status": "done", "export_state": None})
# 模拟旧 bug 现场：job.json 有路径，manifest 没有
job = read_job(p["jd"])
job["video"]["export_file"] = p["mp4"].replace(os.sep, "/")
job["video"]["export_dir"] = p["bundle"].replace(os.sep, "/")
jm.save_job(job, p["jd"])
rc, out = run_clean("mbC")
check("C1 clean 返回 0", rc == 0, rc)
check("C2 输出含 job.json 回退提示", "[回退]" in out, out)
check("C3 成品目录已删除", not os.path.exists(p["bundle"]), p["bundle"])
check("C4 export_state=cleaned", read_job(p["jd"])["video"]["export_state"] == "cleaned",
      read_job(p["jd"])["video"])

# ── D：两处都无路径 ─────────────────────────────────────────────────
print("\n== D 无任何成品路径 ==")
p = make("mbD")
rc, out = run_clean("mbD")
check("D1 无证据时明确告警且返回 0", rc == 0 and "[warn] 找不到成品路径" in out, (rc, out))
check("D2 成品仍在（无路径可删）", os.path.exists(p["bundle"]))

p = make("mbE", video={"status": "done", "export_state": None,
                       "export_file": None, "export_dir": None})
rc, out = run_clean("mbE")
check("D3 有已导出证据时返回 2", rc == 2, rc)
check("D4 输出含错误说明", "[错误] 找不到任何成品路径" in out, out)
check("D5 成品仍在（等待人工补记录）", os.path.exists(p["bundle"]))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
