# -*- coding: utf-8 -*-
"""回归用例：video.export_state（null / exported / cleaned）写入与回写。

覆盖：
  ① clean 真正删除成品后回写 cleaned（revision+1、history=export_cleaned）
  ② --dry-run 不回写
  ③ --intermediates-only 不回写（成品未删）
  ④ record_video 置 exported
  ⑤ validate 接受三值、拒绝非法值
  ⑥ 无 job.json 时 clean 照常删除，回写链接跳过并记录

运行：python tests\\test_export_state.py（纯临时目录，不碰真实剧）
"""
import contextlib, io, json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import stream_manifest as sm  # noqa: E402
import export_state as es  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def run_clean(name, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sm.clean(name, **kw)
    return rc, buf.getvalue()


TMP = os.path.join(tempfile.gettempdir(), "export_state_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
sm.MANIFEST_DIR = os.path.join(TMP, "manifests")


def make_series(name, *, with_job=True):
    """源目录 + cover_job(+job.json) + jycache + workdir + 成品 bundle + 草稿。"""
    src = os.path.join(TMP, name, "系列源目录")
    os.makedirs(os.path.join(src, "cover_job"), exist_ok=True)
    if with_job:
        jd = os.path.join(src, "cover_job")
        for rel in jm.JOB_DIRS:
            os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
        with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
            f.write("测试简介")
        job = jm.build_job(name)
        job["stage"] = "covers_ready"
        jm.save_job(job, jd)
    jy = os.path.join(src, "__jycache__")
    os.makedirs(jy, exist_ok=True)
    with open(os.path.join(jy, "a.bin"), "wb") as f:
        f.write(b"x" * 4096)
    work = os.path.join(TMP, f"pipeline/_work_{name}_deadbeef")
    os.makedirs(work, exist_ok=True)
    bundle = os.path.join(TMP, "L", name)
    os.makedirs(bundle, exist_ok=True)
    with open(os.path.join(bundle, f"{name}.mp4"), "wb") as f:
        f.write(b"m" * 10240)
    with open(os.path.join(bundle, f"{name}-封面.jpg"), "wb") as f:
        f.write(b"c" * 1024)
    with open(os.path.join(bundle, f"{name}.mp4.progress.json"), "w", encoding="utf-8") as f:
        json.dump({"progress": 100}, f)
    draft = os.path.join(TMP, "drafts", name)
    os.makedirs(draft, exist_ok=True)
    sm.record(name, source_dir=src, jycache_dir=jy, workdir=work,
              export_file=os.path.join(bundle, f"{name}.mp4"), draft_dir=draft)
    return {"src": src, "jd": os.path.join(src, "cover_job"), "jy": jy, "work": work,
            "bundle": bundle, "draft": draft}


def read_job(jd):
    return jm.load_job(os.path.join(jd, "job.json"))


print("\n== ① clean 删成品后回写 cleaned ==")
p = make_series("esA")
rev0 = read_job(p["jd"])["revision"]
check("初始 export_state=null", read_job(p["jd"])["video"]["export_state"] is None)
rc, out = run_clean("esA")
check("返回 0", rc == 0, rc)
check("成品已删", not os.path.exists(p["bundle"]))
job = read_job(p["jd"])
check("export_state=cleaned", job["video"]["export_state"] == "cleaned", job["video"])
check("revision +1", job["revision"] == rev0 + 1, (rev0, job["revision"]))
check("history 事件名", job["history"][-1]["event"] == "export_cleaned", job["history"][-1])
check("detail 含路径与释放大小",
      p["bundle"] in job["history"][-1]["detail"] and "释放" in job["history"][-1]["detail"],
      job["history"][-1]["detail"])
check("输出含回写行", "[回写]" in out, out)
check("stage 未被改动", job["stage"] == "covers_ready", job["stage"])

print("\n== ② dry-run 不回写 ==")
p = make_series("esB")
rev0 = read_job(p["jd"])["revision"]
rc, out = run_clean("esB", dry_run=True)
check("返回 0", rc == 0, rc)
check("成品仍在", os.path.exists(p["bundle"]))
job = read_job(p["jd"])
check("export_state 仍 null", job["video"]["export_state"] is None, job["video"])
check("revision 不变", job["revision"] == rev0, (rev0, job["revision"]))
check("输出无回写行", "[回写]" not in out, out)

print("\n== ③ --intermediates-only 不回写 ==")
p = make_series("esC")
rev0 = read_job(p["jd"])["revision"]
rc, out = run_clean("esC", intermediates_only=True)
check("返回 0", rc == 0, rc)
check("jycache 已删", not os.path.exists(p["jy"]))
check("workdir 已删", not os.path.exists(p["work"]))
check("成品仍在", os.path.exists(p["bundle"]))
job = read_job(p["jd"])
check("export_state 仍 null", job["video"]["export_state"] is None, job["video"])
check("revision 不变", job["revision"] == rev0, (rev0, job["revision"]))
check("输出无回写行", "[回写]" not in out, out)

print("\n== ④ record_video 置 exported ==")
p = make_series("esD")
job = jm.record_video(p["jd"], export_dir=p["bundle"],
                      export_file=os.path.join(p["bundle"], "esD.mp4"))
check("export_state=exported", job["video"]["export_state"] == "exported", job["video"])
check("video_ready", job["stage"] == "video_ready", job["stage"])
check("export_file 已记录", job["video"]["export_file"].endswith("esD.mp4"), job["video"])
check("history 事件名", job["history"][-1]["event"] == "video_recorded")

print("\n== ⑤ validate 三值 ==")
jd = os.path.join(TMP, "esE", "系列源目录", "cover_job")
for rel in jm.JOB_DIRS:
    os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
    f.write("测试简介")
for value in (None, "exported", "cleaned"):
    job = jm.build_job("esE")
    job["stage"] = "video_ready"
    job["video"]["export_state"] = value
    check(f"接受 {value}", jm.validate_job(job, jd) == [], jm.validate_job(job, jd))
job = jm.build_job("esE")
job["stage"] = "video_ready"
job["video"]["export_state"] = "bogus"
errs = jm.validate_job(job, jd)
check("拒绝非法值", any("video.export_state" in e for e in errs), errs)

print("\n== ⑥ 无 job.json：照常删除、回写跳过并记录 ==")
p = make_series("esF", with_job=False)
rc, out = run_clean("esF")
check("返回 0", rc == 0, rc)
check("成品已删", not os.path.exists(p["bundle"]))
check("输出含跳过原因", "无 job.json" in out, out)
check("无回写行", "[回写]" not in out, out)

print("\n== ⑦ record_video 版本台账 + 顶层聚合 ==")
p = make_series("esG")
bt = os.path.join(TMP, "L", "esG_b")
os.makedirs(bt, exist_ok=True)
mp4b = os.path.join(bt, "esG_b.mp4")
with open(mp4b, "wb") as f:
    f.write(b"n" * 2048)
jm.record_video(p["jd"], export_dir=p["bundle"],
                export_file=os.path.join(p["bundle"], "esG.mp4"),
                draft_name="esG", profile="a")
job = jm.record_video(p["jd"], export_dir=bt, export_file=mp4b,
                      draft_name="esG_b", profile="b")
outs = job["video"]["outputs"]
check("outputs 有两版", set(outs) == {"a", "b"}, outs)
check("a 版 draft/state", outs["a"]["draft"] == "esG" and outs["a"]["state"] == "exported", outs)
check("b 版记录 size", outs["b"]["size_bytes"] == 2048, outs["b"])
check("b 版 history 有记录", len(outs["b"]["history"]) == 1, outs["b"]["history"])
check("顶层 export_file 仍是基础版", job["video"]["export_file"].endswith("esG.mp4"),
      job["video"]["export_file"])
check("顶层 draft_name 仍是基础版", job["video"]["draft_name"] == "esG", job["video"]["draft_name"])
ext = job["cleanup"]["external"]
check("cleanup 命令带 --profile b",
      any(e.get("profile") == "b" and "--profile b" in (e.get("command") or "") for e in ext), ext)
check("基础版 cleanup 仍是整剧命令（无 --profile）",
      any(e.get("draft_name") == "esG" and "--profile" not in (e.get("command") or "") for e in ext), ext)

print("\n== ⑧ 版本级退避独立 + 导出全局串行 + 聚合 ==")
p = make_series("esH")
jd = p["jd"]
es.register_profiles(jd, ["a", "b"])
es.begin(jd, profile="a")
check("导出中全局 status=exporting", es.load(jd)["status"] == "exporting", es.load(jd))
check("active_profile=a", es.load(jd)["active_profile"] == "a", es.load(jd))
why = es.gate(jd, profile="b")
check("b 被全局串行挡住", why and "全局串行" in why, why)
es.succeed(jd, profile="a")
st = es.load(jd)
check("a done 后全局不是 done（b 未导）", st["status"] != "done", st)
check("b 未被 a 的完成影响，可导", es.gate(jd, profile="b") is None)
es.fail(jd, "boom", profile="b", now=es._now())
st = es.load(jd)
p_b = st["profiles"]["b"]
check("b 进入 retrying", p_b["status"] == "retrying", p_b)
check("b 有独立 next_retry_at", float(p_b["next_retry_at"] or 0) > 0, p_b)
check("b 退避中 gate 被挡", (es.gate(jd, profile="b") or "").find("退避") >= 0,
      es.gate(jd, profile="b"))
check("a 已 done 不受 b 失败影响", es.gate(jd, profile="a") is None)
es.begin(jd, profile="b")
es.succeed(jd, profile="b")
st = es.load(jd)
check("两版都 done → 全局 done", st["status"] == "done", st)
check("done 后 gate 放行", es.gate(jd, profile="b") is None)

print("\n== ⑨ 同版本重跑 .v<n> 三件套改名（含失败回滚） ==")
import importlib.util
from unittest import mock
_spec = importlib.util.spec_from_file_location(
    "pl_pipeline", os.path.join(_ROOT, "pipeline", "pipeline.py"))
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)

d = os.path.join(TMP, "stale_out")
os.makedirs(d, exist_ok=True)
mp4 = os.path.join(d, "剧A.mp4")
cover = os.path.join(d, "剧A-封面.jpg")
prog = mp4 + ".progress.json"
for f in (mp4, cover, prog):
    with open(f, "wb") as fh:
        fh.write(b"x")
moved = pl.archive_stale_outputs(mp4)
check("返回三件套", len(moved) == 3, moved)
check("v1 mp4 存在", os.path.isfile(os.path.join(d, "剧A.v1.mp4")))
check("v1 封面存在", os.path.isfile(os.path.join(d, "剧A.v1-封面.jpg")))
check("v1 progress 存在", os.path.isfile(os.path.join(d, "剧A.v1.mp4.progress.json")))
check("原三件套已移走", not any(os.path.exists(f) for f in (mp4, cover, prog)))
for f in (mp4, cover, prog):
    with open(f, "wb") as fh:
        fh.write(b"y")
moved = pl.archive_stale_outputs(mp4)
check("第二次用 v2", len(moved) == 3 and os.path.isfile(os.path.join(d, "剧A.v2.mp4")), moved)
for f in (mp4, cover, prog):
    with open(f, "wb") as fh:
        fh.write(b"z")
_real_replace = os.replace
_calls = {"n": 0}


def _fail_second(src, dst):
    _calls["n"] += 1
    if _calls["n"] == 2:
        raise OSError("模拟封面改名失败")
    return _real_replace(src, dst)


try:
    with mock.patch.object(pl.os, "replace", side_effect=_fail_second):
        pl.archive_stale_outputs(mp4)
    check("改名失败应抛错", False, "未抛 RuntimeError")
except RuntimeError as e:
    check("改名失败抛 RuntimeError", "回滚" in str(e), e)
check("失败后原 mp4 已回滚", os.path.isfile(mp4))
check("失败后封面仍在原位", os.path.isfile(cover))
check("失败后未留下 v3 半套", not os.path.exists(os.path.join(d, "剧A.v3.mp4")))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
