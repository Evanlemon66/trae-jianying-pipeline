# -*- coding: utf-8 -*-
"""回归用例：stream_manifest clean 的开关与保护区（全部用临时目录，不碰真实剧）。

覆盖：
  ① 默认 clean：删 jycache + workdir + 成品 bundle，**不删草稿**
  ② --include-drafts 才删草稿
  ③ --require-receipts：缺回执整剧跳过；有回执才放行
  ④ --dry-run：零删除，清单与合计正确
  ⑤ 保护区断言：源目录 / _broken / cover_job 路径被跳过并告警
  ⑥ --intermediates-only：只清中间产物，成品与草稿都不动（成品存在/不存在两态）

运行：python tests\\test_stream_clean.py
"""
import contextlib, io, json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import stream_manifest as sm  # noqa: E402

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


TMP = os.path.join(tempfile.gettempdir(), "stream_clean_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
sm.MANIFEST_DIR = os.path.join(TMP, "manifests")


def make_series(name, *, with_receipt=False):
    """造一部剧：源目录 + jycache + cover_job + 成品 bundle + 草稿 + pipeline workdir。"""
    src = os.path.join(TMP, name, "系列源目录")
    os.makedirs(os.path.join(src, "cover_job", "06_publish", "receipts"), exist_ok=True)
    jy = os.path.join(src, "__jycache__")
    os.makedirs(jy, exist_ok=True)
    with open(os.path.join(jy, "a.bin"), "wb") as f:
        f.write(b"x" * 4096)
    work = os.path.join(TMP, f"pipeline/_work_{name}_deadbeef")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "b.bin"), "wb") as f:
        f.write(b"y" * 8192)
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
    with open(os.path.join(draft, "draft_content.json"), "wb") as f:
        f.write(b"d" * 2048)
    if with_receipt:
        with open(os.path.join(src, "cover_job", "06_publish", "receipts", "youtube.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"platform": "youtube", "status": "published",
                       "video_id": "abc123", "url": "https://youtu.be/abc123"}, f)
    sm.record(name, source_dir=src, jycache_dir=jy, workdir=work,
              export_file=os.path.join(bundle, f"{name}.mp4"), draft_dir=draft)
    return {"src": src, "jy": jy, "work": work, "bundle": bundle, "draft": draft}


print("\n== ① 默认 clean：删中间产物+成品，保留草稿 ==")
p = make_series("showA")
rc, out = run_clean("showA")
check("返回 0", rc == 0, rc)
check("jycache 已删", not os.path.exists(p["jy"]))
check("workdir 已删", not os.path.exists(p["work"]))
check("成品 bundle 已删", not os.path.exists(p["bundle"]))
check("草稿保留", os.path.exists(p["draft"]))
check("源目录保留", os.path.exists(p["src"]))
check("manifest 记录保留", os.path.exists(sm._manifest_path("showA")))
check("输出含已删行", "[已删] export_bundle" in out, out)
check("输出含 dry-run 提示", "--dry-run" in out, out)

print("\n== ② --include-drafts 才删草稿 ==")
p = make_series("showB")
rc, out = run_clean("showB", include_drafts=True)
check("返回 0", rc == 0, rc)
check("草稿已删", not os.path.exists(p["draft"]))
check("输出单独标注 draft_dir", "[已删] draft_dir" in out, out)

print("\n== ③ --require-receipts ==")
p = make_series("showC", with_receipt=False)
rc, out = run_clean("showC", require_receipts=True)
check("缺回执 → 整剧跳过（rc=1）", rc == 1, rc)
check("跳过后 jycache 未删", os.path.exists(p["jy"]))
check("跳过后 workdir 未删", os.path.exists(p["work"]))
check("跳过后成品未删", os.path.exists(p["bundle"]))
check("输出含跳过原因", "[跳过] 严格模式未通过" in out, out)
p = make_series("showD", with_receipt=True)
rc, out = run_clean("showD", require_receipts=True)
check("有回执 → 放行（rc=0）", rc == 0, rc)
check("放行后 jycache 已删", not os.path.exists(p["jy"]))
check("输出含回执校验通过", "[通过] 回执校验" in out, out)

print("\n== ④ --dry-run 零删除 ==")
p = make_series("showE")
rc, out = run_clean("showE", dry_run=True)
check("返回 0", rc == 0, rc)
check("jycache 仍在", os.path.exists(p["jy"]))
check("workdir 仍在", os.path.exists(p["work"]))
check("成品仍在", os.path.exists(p["bundle"]))
check("输出标记计划", "[计划]" in out, out)
check("输出含合计", "合计:" in out, out)
check("输出含 dry-run 说明", "未执行任何删除" in out, out)

print("\n== ⑤ 保护区断言 ==")
src = os.path.join(TMP, "showF", "系列源目录")
os.makedirs(os.path.join(src, "cover_job", "06_publish", "receipts"), exist_ok=True)
broken = os.path.join(TMP, "_broken")
os.makedirs(broken, exist_ok=True)
with open(os.path.join(broken, "bad.mp4"), "wb") as f:
    f.write(b"b" * 1024)
inside_job = os.path.join(src, "cover_job", "__jycache__")
os.makedirs(inside_job, exist_ok=True)
sm.record("showF", source_dir=src, workdir=src,
          export_file=os.path.join(broken, "bad.mp4"), jycache_dir=inside_job,
          draft_dir=os.path.join(TMP, "drafts", "showF"))
rc, out = run_clean("showF")
check("返回 0（仅跳过）", rc == 0, rc)
check("源目录被保护", os.path.exists(src))
check("_broken 被保护", os.path.exists(os.path.join(broken, "bad.mp4")))
check("cover_job 内路径被保护", os.path.exists(inside_job))
check("告警：等于源视频目录", "等于源视频目录" in out, out)
check("告警：_broken", "_broken" in out, out)
check("告警：cover_job", "cover_job 正式产物区" in out, out)
check("无任何 [已删]", "[已删]" not in out, out)

print("\n== ⑥ --intermediates-only：成品与草稿都不删 ==")
p = make_series("showG")
rc, out = run_clean("showG", intermediates_only=True)
check("返回 0", rc == 0, rc)
check("jycache 已删", not os.path.exists(p["jy"]))
check("workdir 已删", not os.path.exists(p["work"]))
check("成品保留", os.path.exists(p["bundle"]))
check("草稿保留", os.path.exists(p["draft"]))
check("输出含模式说明", "intermediates-only" in out, out)
check("无成品删除行", "[已删] export_bundle" not in out, out)
check("仅删 2 项", out.count("[已删]") == 2, out.count("[已删]"))

p = make_series("showH")
shutil.rmtree(p["bundle"])
rc, out = run_clean("showH", intermediates_only=True)
check("成品不存在仍返回 0", rc == 0, rc)
check("jycache 已删", not os.path.exists(p["jy"]))
check("workdir 已删", not os.path.exists(p["work"]))
check("草稿保留", os.path.exists(p["draft"]))
check("无成品相关行", "export_bundle" not in out, out)

p = make_series("showI")
rc, out = run_clean("showI", intermediates_only=True, include_drafts=True)
check("intermediates-only 压过 include-drafts：草稿保留", os.path.exists(p["draft"]))
check("成品保留", os.path.exists(p["bundle"]))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
