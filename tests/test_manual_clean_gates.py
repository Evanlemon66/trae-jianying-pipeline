# -*- coding: utf-8 -*-
"""`--manual` 手工侧（clean / archive / verify / 锁兜底）的四道闸 + 功能用例。

闸 1 代码隔离：AST 扫 4 个手工模块，禁止出现默认路径的判据符号。
闸 2 import 图：禁止 cover_watch / job_manifest / freshness / export_state /
              pipeline / pipeline_ui 等 watcher 侧依赖。
闸 3 变更面：watcher 侧文件零 diff；默认路径 8 个文件零 diff；stream_manifest.py
              以 `def _parse_clean_args` 为界，之前的部分与 HEAD 逐字节相同，且
              `clean()` 函数体整段原样保留。
闸 4 对照测试：HEAD 版 stream_manifest 与当前默认路径同一夹具逐项一致；运行期
              防呆（审计钩子记录所有 open，手工跑批期间禁止 job 侧文件）。

功能用例：锁兜底 6 例、clean（A/B + 引用保护 + 幂等）、archive（只搬成品 +
源目录不动 + 幂等 + 三件套/冲突）、verify（对账 0 残留 → 造残留 → 非 0）。

运行：python tests\\test_manual_clean_gates.py（纯临时目录，不跑准入）
"""
import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export_config  # noqa: E402
import export_lock  # noqa: E402
import job_manifest as jm  # noqa: E402
import manual_archive as ma  # noqa: E402
import manual_clean as mc  # noqa: E402
import manual_guard as mg  # noqa: E402
import manual_verify as mv  # noqa: E402
import stream_manifest as sm  # noqa: E402
from bundle_fixture import make_bundle  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def capture(fn):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn()
    return rc, buf.getvalue()


TMP = os.path.join(tempfile.gettempdir(), "manual_clean_gates")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

MODULES = {
    "manual_guard": os.path.join(_ROOT, "pipeline", "manual_guard.py"),
    "manual_clean": os.path.join(_ROOT, "pipeline", "manual_clean.py"),
    "manual_archive": os.path.join(_ROOT, "pipeline", "manual_archive.py"),
    "manual_verify": os.path.join(_ROOT, "pipeline", "manual_verify.py"),
}
TREES = {}
for _n, _p in MODULES.items():
    with open(_p, encoding="utf-8") as f:
        TREES[_n] = ast.parse(f.read())

# ── 闸 1：代码隔离（零判据交叠）──────────────────────────────────────
print("\n== 闸1 代码隔离：AST 零判据交叠 ==")
FORBIDDEN_SYMBOLS = {
    "clean", "_job_video", "_audit_conflicts", "_output_plan_items",
    "receipts_status", "_mark_export_cleaned", "_existing_job_dir",
    "_unregistered_outputs", "_plan", "_blocked_reason", "_same_job_group",
    "_base_draft_name", "_sync_shared_with", "version_profile", "record",
    "_norm_profile", "_progress_side_status", "archive_job", "_pid_alive",
    "read_lock", "_claim",
}
for name, tree in TREES.items():
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    hit = sorted((names | attrs) & FORBIDDEN_SYMBOLS)
    check(f"闸1 {name} 不引用默认路径/共享锁内部判据", not hit, hit)

mc_funcs = {n.name for n in TREES["manual_clean"].body if isinstance(n, ast.FunctionDef)}
ma_funcs = {n.name for n in TREES["manual_archive"].body if isinstance(n, ast.FunctionDef)}
mv_funcs = {n.name for n in TREES["manual_verify"].body if isinstance(n, ast.FunctionDef)}
mc_assigned = {t.id for n in TREES["manual_clean"].body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
check("闸1.2 clean 自带判定+执行入口",
      {"build_plan", "run", "main"} <= mc_funcs and {"_protected", "_guard"} <= mc_assigned,
      sorted(({"build_plan", "run", "main"} - mc_funcs) | ({"_protected", "_guard"} - mc_assigned)))
check("闸1.3 archive 自带判定+执行入口",
      {"archive", "main", "_bundle_of", "_mark"} <= ma_funcs,
      sorted({"archive", "main", "_bundle_of", "_mark"} - ma_funcs))
check("闸1.4 verify 自带对账入口",
      {"verify", "main", "check_one"} <= mv_funcs,
      sorted({"verify", "main", "check_one"} - mv_funcs))
mg_funcs = {n.name for n in TREES["manual_guard"].body if isinstance(n, ast.FunctionDef)}
check("闸1.5 锁兜底自建三态判活（不用 _pid_alive）",
      {"pid_state", "diagnose_lock", "_stale_reason", "acquire_export_lock"} <= mg_funcs,
      sorted(mg_funcs))

# ── 闸 2：import 图 ────────────────────────────────────────────────
print("\n== 闸2 import 图：禁止 watcher 侧依赖 ==")
FORBIDDEN_MODULES = {"cover_watch", "job_manifest", "freshness", "export_state",
                     "pipeline", "pipeline_ui", "archive_job"}
ALLOWED = {
    "manual_guard": {"json", "os", "subprocess", "sys", "time", "export_lock", "no_window"},
    "manual_clean": {"json", "os", "re", "shutil", "sys", "export_bundle", "export_config",
                     "manual_guard", "stream_manifest"},
    "manual_archive": {"json", "os", "shutil", "sys", "export_bundle", "export_config",
                       "manual_guard", "stream_manifest", "manual_clean"},
    "manual_verify": {"json", "os", "sys", "export_bundle", "export_config",
                      "manual_guard", "stream_manifest"},
}
for name, tree in TREES.items():
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                mods.add(n.module.split(".")[0])
    check(f"闸2 {name} 不 import watcher 侧模块", not (mods & FORBIDDEN_MODULES),
          sorted(mods & FORBIDDEN_MODULES))
    extra = sorted(mods - ALLOWED[name])
    check(f"闸2 {name} 只 import 白名单", not extra, extra)

# ── 闸 3：变更面（git diff）────────────────────────────────────────
print("\n== 闸3 变更面：watcher 侧零改动，默认路径保持 HEAD ==")


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=_ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


changed = {ln.strip().replace("\\", "/") for ln in git("diff", "--name-only", "HEAD").splitlines()
           if ln.strip()}
WATCHER_FILES = {"lib/cover_watch.py", "pipeline/archive_job.py", "lib/freshness.py",
                 "lib/export_state.py", "lib/job_manifest.py"}
RESTORED_FILES = {"lib/export_lock.py", "pipeline/pipeline.py", "pipeline/series_runner.py",
                  "pipeline/batch_runner.py", "pipeline/preset_ops.py",
                  "tests/test_stream_clean.py", "tests/test_manifest_backfill.py"}
# 本批（手工清理三件套 + UI 准入链路 + 测试隔离）确认的合法改动面。
# watcher 侧与默认路径仍由闸3.1–3.3 硬拦，这里的白名单只放行本批主题文件。
OTHER_LINE = {"lib/failure_class.py", "lib/jy_page_detect.py", "pipeline/verify_draft.py",
              "pipeline/jianying_ui.py",
              "pipeline_ui/admission_test.py", "pipeline_ui/jianying_ui.py",
              "pipeline_ui/ui_calibration.py", "pipeline_ui/ui_core.py",
              "pipeline_ui/ui_extract_audio.py", "pipeline_ui/ui_recognize_translate.py",
              "pipeline_ui/ui_track_ops.py", "pipeline_ui/ui_vocal_separate.py",
              "specs/ui-admission-launch/spec.md", "specs/ui-admission-launch/tasks.md",
              "tests/conftest.py", "tests/test_ui_pipeline_gates.py",
              ".gitignore"}

check("闸3.1 watcher 侧文件零改动", not (changed & WATCHER_FILES), sorted(changed & WATCHER_FILES))
check("闸3.2 export_lock.py 一个字节未动", "lib/export_lock.py" not in changed)
check("闸3.3 默认路径的 8 个文件仍是 HEAD 原样",
      not (changed & RESTORED_FILES), sorted(changed & RESTORED_FILES))
check("闸3.4 变更面仅限本批已确认的文件（watcher 侧零改动）",
      (changed - OTHER_LINE) == {"pipeline/stream_manifest.py"}, sorted(changed - OTHER_LINE))

head_src = git("show", "HEAD:pipeline/stream_manifest.py")
head_lines = head_src.splitlines()
cli_line = next(i + 1 for i, ln in enumerate(head_lines) if ln.startswith("def _parse_clean_args"))
with open(os.path.join(_ROOT, "pipeline", "stream_manifest.py"), encoding="utf-8") as f:
    cur_lines = f.read().splitlines()
check(f"闸3.5 _parse_clean_args 之前与 HEAD 逐字节相同（前 {cli_line - 1} 行）",
      cur_lines[:cli_line - 1] == head_lines[:cli_line - 1])
head_clean = head_src[head_src.index("def clean("):head_src.index("def _manifest_names(")]
check("闸3.6 clean() 函数体整段原样保留", head_clean in "\n".join(cur_lines))

diff = git("diff", "-U0", "--", "pipeline/stream_manifest.py")
starts = []
for ln in diff.splitlines():
    if ln.startswith("@@"):
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
        if m:
            starts.append(int(m.group(1)))
check(f"闸3.7 所有 hunk 都在 CLI 区（>= {cli_line}）",
      bool(starts) and min(starts) >= cli_line, (cli_line, starts))
removed = [ln for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
check("闸3.8 默认路径零删除（只增不删）", len(removed) <= 2, removed)


# ── 闸 4a：对照测试（HEAD 实现 vs 当前默认路径）──────────────────────
print("\n== 闸4a 对照测试：HEAD clean vs 当前默认 clean ==")


def load_head_module(dst):
    os.makedirs(dst, exist_ok=True)
    fp = os.path.join(dst, "stream_manifest_head.py")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(head_src)
    spec = importlib.util.spec_from_file_location("stream_manifest_head", fp)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_contrast_fixture(mod, root):
    man = os.path.join(root, "manifests")
    os.makedirs(man, exist_ok=True)
    mod.MANIFEST_DIR = man
    src = os.path.join(root, "series", "剧A")
    jd = os.path.join(src, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("简介")
    L = os.path.join(root, "L")
    ba, bb = os.path.join(L, "剧A"), os.path.join(L, "剧A_b")
    mpa = make_bundle(ba, "剧A")["mp4"]
    mpb = make_bundle(bb, "剧A_b")["mp4"]
    with open(os.path.join(L, "剧A.progress.json"), "w", encoding="utf-8") as f:
        json.dump({"status": "running"}, f)
    job = jm.build_job("剧A")
    job["stage"] = "video_ready"
    job["video"].update({"status": "done", "export_state": "exported",
                         "export_file": mpa, "export_dir": ba,
                         "outputs": {"a": {"draft": "剧A", "export_file": mpa,
                                           "export_dir": ba, "state": "exported"},
                                     "b": {"draft": "剧A_b", "export_file": mpb,
                                           "export_dir": bb, "state": "exported"}}})
    jm.save_job(job, jd)
    jy = os.path.join(src, "__jycache__")
    os.makedirs(jy)
    with open(os.path.join(jy, "a.bin"), "wb") as f:
        f.write(b"x" * 4096)
    work = os.path.join(root, "pipeline", "_work_x")
    os.makedirs(work)
    with open(os.path.join(work, "w.tmp"), "wb") as f:
        f.write(b"w" * 128)
    draft = os.path.join(root, "drafts", "剧A")
    os.makedirs(draft)
    with open(os.path.join(draft, "draft_content.json"), "w", encoding="utf-8") as f:
        json.dump({"materials": {}}, f)
    mod.record("剧A", source_dir=src, jycache_dir=jy, workdir=work,
               export_file=mpa, draft_dir=draft)
    return {"root": root, "src": src, "jd": jd, "man": man}


VOLATILE_KEYS = {"job_id", "created_at", "updated_at", "at", "ts", "time", "timestamp",
                 "recorded_at", "deleted_at", "started_at", "finished_at"}


def normalize_job(node, root):
    if isinstance(node, dict):
        return {k: normalize_job(v, root) for k, v in sorted(node.items())
                if k not in VOLATILE_KEYS}
    if isinstance(node, list):
        return [normalize_job(v, root) for v in node]
    if isinstance(node, str):
        return node.replace(root, "<ROOT>").replace(root.replace("\\", "/"), "<ROOT>")
    return node


def tree_of(root, skip_names):
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if os.path.relpath(os.path.join(dp, d), root) not in skip_names)
        out.append(("dir", os.path.relpath(dp, root).replace("\\", "/")))
        for fn in sorted(fns):
            if fn in skip_names:
                continue
            out.append(("file", os.path.relpath(os.path.join(dp, fn), root).replace("\\", "/"),
                        os.path.getsize(os.path.join(dp, fn))))
    return sorted(out)


def run_contrast():
    import freshness
    freshness.write_tombstone = lambda *a, **k: None  # 墓碑名含微秒，去掉以保证可比
    head = load_head_module(os.path.join(TMP, "headpkg"))
    hroot = os.path.join(TMP, "contrast_head")
    croot = os.path.join(TMP, "contrast_cur")
    build_contrast_fixture(head, hroot)
    build_contrast_fixture(sm, croot)
    rc_h, out_h = capture(lambda: head.clean("剧A"))
    rc_c, out_c = capture(lambda: sm.clean("剧A"))
    out_h = out_h.replace(hroot, "<ROOT>").replace(hroot.replace("\\", "/"), "<ROOT>")
    out_c = out_c.replace(croot, "<ROOT>").replace(croot.replace("\\", "/"), "<ROOT>")
    check("闸4a.1 返回码一致", rc_h == rc_c, (rc_h, rc_c))
    if out_h != out_c:
        import difflib
        d = "\n".join(difflib.unified_diff(out_h.splitlines(), out_c.splitlines(),
                                           "HEAD", "current", lineterm=""))
        check("闸4a.2 stdout 逐行一致", False, d[:1200])
    else:
        check("闸4a.2 stdout 逐行一致", True)
    skip = {"manifests", "job.json"}
    check("闸4a.3 剩余文件树一致", tree_of(hroot, skip) == tree_of(croot, skip))
    with open(os.path.join(hroot, "series", "剧A", "cover_job", "job.json"), encoding="utf-8") as f:
        jh = normalize_job(json.load(f), hroot)
    with open(os.path.join(croot, "series", "剧A", "cover_job", "job.json"), encoding="utf-8") as f:
        jc = normalize_job(json.load(f), croot)
    check("闸4a.4 job.json 逐字段一致（去时间戳）", jh == jc)


run_contrast()

# ── 环境：把手工侧的中立配置与锁路径钉到临时目录 ─────────────────────
DRAFTS = os.path.join(TMP, "manual", "drafts")
EXPORT = os.path.join(TMP, "manual", "L")
ARCH = os.path.join(TMP, "manual", "archive")
STAGING = os.path.join(TMP, "manual", "__jybuild__")
for _d in (DRAFTS, EXPORT, ARCH):
    os.makedirs(_d, exist_ok=True)
export_config.get_projects_root = lambda: DRAFTS
export_config.get_export_dir = lambda: EXPORT
export_config.get_archive_root = lambda: ARCH
export_lock._LOCK_PATH = os.path.join(TMP, "manual", ".export.lock")

opened = []
active = {"on": False}


def _hook(event, args):
    if active["on"] and event == "open" and args:
        opened.append(str(args[0]))


sys.addaudithook(_hook)

# ── 闸 4b1：运行期防呆（_guard）─────────────────────────────────────
print("\n== 闸4b 运行期防呆：manual 途中读到 job 侧文件即失败 ==")
for rel in ("cover_job/job.json", "cover_job/.watcher.lock", "cover_job/.export_state.json"):
    try:
        mg.guard(os.path.join("D:\\fake\\系列", rel))
        check(f"闸4b 拦截 {rel}", False, "未抛错")
    except mg.WatcherReadError:
        check(f"闸4b 拦截 {rel}", True)
check("闸4b 清单路径放行", mg.guard(os.path.join(TMP, "manifests", "剧A.json")) is None)

# ── 闸 4b2：锁兜底 6 例 ────────────────────────────────────────────
print("\n== 闸4b 锁兜底：手工侧接管陈旧锁，绝不抢活锁 ==")
LOCK = export_lock._LOCK_PATH


def write_lock(payload, age=0):
    if os.path.exists(LOCK):
        os.remove(LOCK)
    with open(LOCK, "wb") as f:
        f.write(payload)
    if age:
        t = time.time() - age
        os.utime(LOCK, (t, t))


def backup_watcher_state():
    snap = {}
    for dp, _, fns in os.walk(TMP):
        for fn in fns:
            if "cover_job" in dp.split(os.sep) or fn in mg.WATCHER_NAMES:
                p = os.path.join(dp, fn)
                snap[p] = os.path.getmtime(p)
    return snap


snap0 = backup_watcher_state()
try:
    os.remove(LOCK)
except OSError:
    pass
rc, out = capture(lambda: mg.acquire_export_lock("t-normal"))
check("L1 正常取锁成功且无接管", rc[0] and rc[1] == "" and os.path.exists(LOCK), (rc, out))
mg.release_export_lock()

write_lock(b"", age=120)
rc, out = capture(lambda: mg.acquire_export_lock("t-zero"))
check("L2 0 字节 + mtime>60s 可接管", rc[0] and "接管" in rc[1], (rc, out))
check("L2b 接管后锁里是本进程", json.load(open(LOCK, encoding="utf-8"))["pid"] == os.getpid())
mg.release_export_lock()

write_lock(b"", age=0)
rc, out = capture(lambda: mg.acquire_export_lock("t-fresh"))
check("L3 新鲜坏锁拒绝接管", not rc[0] and "未确认陈旧" in rc[1], (rc, out))

write_lock(json.dumps({"pid": os.getpid(), "owner": "alive",
                       "started": "x"}).encode("utf-8"))
rc, out = capture(lambda: mg.acquire_export_lock("t-alive"))
check("L4 活 pid 拒绝接管", not rc[0] and "绝不能抢" in rc[1], (rc, out))

write_lock(json.dumps({"pid": 999999, "owner": "dead", "started": "x"}).encode("utf-8"))
rc, out = capture(lambda: mg.acquire_export_lock("t-dead"))
check("L5 死 pid 接管", rc[0] and "接管" in rc[1], (rc, out))
mg.release_export_lock()

orig_state = mg.pid_state
mg.pid_state = lambda pid: "unknown"
write_lock(json.dumps({"pid": 1234, "owner": "x", "started": "x"}).encode("utf-8"))
rc, out = capture(lambda: mg.acquire_export_lock("t-unknown"))
check("L6 tasklist 不可用 → unknown 拒绝接管", not rc[0] and "宁停不抢" in rc[1], (rc, out))
mg.pid_state = orig_state

check("L7 锁兜底全程未新增 watcher 侧文件", backup_watcher_state() == snap0,
      backup_watcher_state())
try:
    os.remove(LOCK)
except OSError:
    pass

# ── 功能用例：clean（A+B、引用保护、幂等）─────────────────────────────
print("\n== 功能用例 clean：A+B 磁盘发现 / 引用保护 / 幂等 ==")
croot = os.path.join(TMP, "case_clean")
csrc = os.path.join(croot, "series", "剧C")
os.makedirs(csrc)
cjy = os.path.join(csrc, "__jycache__")
os.makedirs(cjy)
with open(os.path.join(cjy, "s1.mp4"), "wb") as f:
    f.write(b"j" * 512)
cwork = os.path.join(croot, "pipeline", "_work_c")
os.makedirs(cwork)
with open(os.path.join(cwork, "x.tmp"), "wb") as f:
    f.write(b"w" * 64)
cba, cbb = os.path.join(EXPORT, "剧C"), os.path.join(EXPORT, "剧C_b")
make_bundle(cba, "剧C")
make_bundle(cbb, "剧C_b")
with open(os.path.join(EXPORT, "剧C.progress.json"), "w", encoding="utf-8") as f:
    json.dump({"status": "running"}, f)
dca, dcb = os.path.join(DRAFTS, "剧C"), os.path.join(DRAFTS, "剧C_b")
os.makedirs(dca)
with open(os.path.join(dca, "draft_content.json"), "w", encoding="utf-8") as f:
    json.dump({"materials": {"videos": [{"path": os.path.join(cjy, "s1.mp4")}]}}, f)
os.makedirs(dcb)
with open(os.path.join(dcb, "draft_content.json"), "w", encoding="utf-8") as f:
    json.dump({"materials": {}}, f)

sm.MANIFEST_DIR = os.path.join(croot, "manifests")
os.makedirs(sm.MANIFEST_DIR, exist_ok=True)
sm.record("剧C", source_dir=csrc, jycache_dir=cjy, workdir=cwork,
          export_file=os.path.join(cba, "剧C.mp4"), draft_dir=dca)

old_argv = sys.argv
sys.argv = ["stream_manifest.py", "clean", "--job", "剧C", "--manual", "--dry-run",
            "--manifest-dir", sm.MANIFEST_DIR]
rc_cli, out_cli = capture(sm.main)
sys.argv = old_argv
check("F1 CLI clean --manual 分流返回 0", rc_cli == 0, rc_cli)
check("F2 dry-run 列出 A/B 两个成品包", cba in out_cli and cbb in out_cli, out_cli)
check("F3 dry-run 未删任何东西",
      os.path.isdir(cba) and os.path.isdir(cbb) and os.path.isdir(cwork))
check("F4 被引用 __jycache__ 保留并列出引用方",
      "[保留]" in out_cli and os.path.isdir(cjy), out_cli)

active["on"] = True
rc_run, out_run = capture(lambda: mc.run("剧C"))
active["on"] = False
check("F5 实际清理返回 0", rc_run == 0, rc_run)
check("F6 A/B 成品包均已删除", not os.path.exists(cba) and not os.path.exists(cbb))
check("F7 未引用 workdir 已删、被引用 __jycache__ 保留",
      not os.path.exists(cwork) and os.path.isdir(cjy))
check("F8 源目录保留且未创建 cover_job",
      os.path.isdir(csrc) and not os.path.exists(os.path.join(csrc, "cover_job")))
with open(sm._manifest_path("剧C"), encoding="utf-8") as f:
    man_c = json.load(f)
check("F9 清单回写 versions.a.state=cleaned",
      ((man_c.get("versions") or {}).get("a") or {}).get("state") == "cleaned",
      man_c.get("versions"))
rc_again, out_again = capture(lambda: mc.run("剧C"))
check("F10 幂等重跑返回 0 且无删除动作",
      rc_again == 0 and "[已删]" not in out_again, (rc_again, out_again))

# ── 功能用例：archive（只搬成品、源目录不动、幂等）────────────────────
print("\n== 功能用例 archive：只搬成品 bundle / 源目录原地不动 / 幂等 ==")
aroot = os.path.join(TMP, "case_archive")
asrc = os.path.join(aroot, "series", "剧D")
os.makedirs(asrc)
with open(os.path.join(asrc, "s1.mp4"), "wb") as f:
    f.write(b"s" * 256)  # 源视频本体必须原地保留
aba, abb = os.path.join(EXPORT, "剧D"), os.path.join(EXPORT, "剧D_b")
make_bundle(aba, "剧D")
make_bundle(abb, "剧D_b")
dda, ddb = os.path.join(DRAFTS, "剧D"), os.path.join(DRAFTS, "剧D_b")
os.makedirs(dda)
with open(os.path.join(dda, "draft_content.json"), "w", encoding="utf-8") as f:
    json.dump({"materials": {}}, f)
os.makedirs(ddb)
with open(os.path.join(ddb, "draft_content.json"), "w", encoding="utf-8") as f:
    json.dump({"materials": {}}, f)
sm.MANIFEST_DIR = os.path.join(aroot, "manifests")
os.makedirs(sm.MANIFEST_DIR, exist_ok=True)
sm.record("剧D", source_dir=asrc, export_file=os.path.join(aba, "剧D.mp4"), draft_dir=dda)

old_argv = sys.argv
sys.argv = ["stream_manifest.py", "archive", "--manual", "--job", "剧D", "--dry-run",
            "--manifest-dir", sm.MANIFEST_DIR, "--archive-root", ARCH]
rc_dry, out_dry = capture(sm.main)
sys.argv = old_argv
check("A1 CLI archive --manual --dry-run 返回 0", rc_dry == 0, (rc_dry, out_dry))
check("A2 dry-run 计划搬 A/B 且未动文件",
      os.path.join(ARCH, "剧D") in out_dry and os.path.isdir(aba) and os.path.isdir(abb), out_dry)
check("A3 dry-run 明示源剧集目录原地不动", "源剧集目录原地不动" in out_dry, out_dry)

active["on"] = True
rc_arc, out_arc = capture(lambda: ma.archive("剧D"))
active["on"] = False
check("A4 归档返回 0", rc_arc == 0, (rc_arc, out_arc))
check("A5 成品已搬到归档池", os.path.isdir(os.path.join(ARCH, "剧D"))
      and os.path.isdir(os.path.join(ARCH, "剧D_b")))
check("A6 导出根下成品已消失", not os.path.exists(aba) and not os.path.exists(abb))
check("A7 源剧集目录与源视频原地不动",
      os.path.isfile(os.path.join(asrc, "s1.mp4"))
      and not os.path.exists(os.path.join(asrc, "cover_job")))
check("A8 别处未新建 cover_job / job.json",
      not os.path.exists(os.path.join(asrc, "cover_job")))
with open(sm._manifest_path("剧D"), encoding="utf-8") as f:
    man_d = json.load(f)
check("A9 清单 versions.a/b=archived 且记了归档目标",
      all(((man_d.get("versions") or {}).get(p) or {}).get("state") == "archived"
          and ((man_d["versions"][p].get("archived_to") or "").replace("\\", "/")
               .startswith(ARCH.replace("\\", "/"))) for p in ("a", "b")),
      man_d.get("versions"))
check("A10 顶层 paths.export_file 已指向归档池",
      (man_d["paths"]["export_file"].replace("\\", "/")
       .startswith(ARCH.replace("\\", "/"))), man_d["paths"])

rc_arc2, out_arc2 = capture(lambda: ma.archive("剧D"))
check("A11 幂等重跑返回 0 且全部 skip",
      rc_arc2 == 0 and "[已搬]" not in out_arc2 and "已归档" in out_arc2, (rc_arc2, out_arc2))
check("A12 重复归档未破坏归档池", os.path.isdir(os.path.join(ARCH, "剧D")))

out_buf = io.StringIO()
with contextlib.redirect_stdout(out_buf):
    ma._mirror(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
               "剧D", "a", os.path.join(ARCH, "剧D"), asrc)
check("A13 可选 job 镜像失败只告警", "[warn] job 镜像回写失败" in out_buf.getvalue(),
      out_buf.getvalue())

# 半成品：三件套不全 → 冲突
bad = os.path.join(TMP, "case_archive", "series", "剧E")
os.makedirs(bad)
bba = os.path.join(EXPORT, "剧E")
os.makedirs(bba)
with open(os.path.join(bba, "剧E.mp4"), "wb") as f:
    f.write(b"x")
sm.record("剧E", source_dir=bad, export_file=os.path.join(bba, "剧E.mp4"))
rc_bad, out_bad = capture(lambda: ma.archive("剧E"))
check("A14 三件套不全 → 阻断且不搬", rc_bad == 1 and os.path.isdir(bba) and "三件套不完整" in out_bad,
      (rc_bad, out_bad))

# ── 功能用例：verify（对账 0 残留 → 造残留 → 非 0）───────────────────
print("\n== 功能用例 verify：对账 / staging / 中间产物引用 ==")
sm.MANIFEST_DIR = os.path.join(aroot, "manifests")
rc_v0, out_v0 = capture(lambda: mv.verify("剧D"))
check("V1 归档后对账 0 残留且返回 0", rc_v0 == 0 and "[残留]" not in out_v0, (rc_v0, out_v0))
check("V2 对账打印版本状态", "state: a=archived" in out_v0, out_v0)

os.makedirs(STAGING, exist_ok=True)
tmpd = os.path.join(STAGING, "剧D_tmp123")
os.makedirs(tmpd)
with open(os.path.join(tmpd, "blob.bin"), "wb") as f:
    f.write(b"z" * 2048)
rc_v1, out_v1 = capture(lambda: mv.verify("剧D"))  # 默认 scope=drama
check("V3 本剧干净 + 全局 staging 有残留 → 本剧 rc 仍为 0",
      rc_v1 == 0 and "本剧汇总" in out_v1 and "rc=0" in out_v1
      and tmpd not in out_v1, (rc_v1, out_v1))
rc_v1s, out_v1s = capture(lambda: mv.verify(scope="staging"))
check("V3b 全局 staging 单独扫描 → 非 0 且给数量与总大小",
      rc_v1s != 0 and tmpd in out_v1s and "残留 1 项" in out_v1s
      and "合计" in out_v1s and "2048 字节" in out_v1s, (rc_v1s, out_v1s))
rc_v1b, out_v1b = capture(lambda: mv.verify("剧D", scope="both"))
check("V3c scope=both 分段出结论且合并 rc=1",
      rc_v1b != 0 and "本剧 rc=0" in out_v1b and "全局 staging rc=1" in out_v1b,
      (rc_v1b, out_v1b))
shutil.rmtree(tmpd)

# 中间产物无草稿引用 → 残留
vwork = os.path.join(TMP, "case_archive", "pipeline", "_work_d")
os.makedirs(vwork, exist_ok=True)
with open(os.path.join(vwork, "x.tmp"), "wb") as f:
    f.write(b"w")
with open(sm._manifest_path("剧D"), encoding="utf-8") as f:
    raw = json.load(f)
raw["paths"]["workdir"] = vwork
with open(sm._manifest_path("剧D"), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False, indent=2)
rc_v2, out_v2 = capture(lambda: mv.verify("剧D"))
check("V4 中间产物无引用 → 残留并说明可清理",
      rc_v2 != 0 and vwork in out_v2 and "无任何草稿引用" in out_v2, (rc_v2, out_v2))
shutil.rmtree(vwork)

# state=cleaned 但成品仍在 → 残留
with open(sm._manifest_path("剧D"), encoding="utf-8") as f:
    raw = json.load(f)
raw["versions"]["a"]["state"] = "cleaned"
with open(sm._manifest_path("剧D"), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False, indent=2)
rc_v3, out_v3 = capture(lambda: mv.verify("剧D"))
check("V5 state=cleaned 但成品仍在 → 残留",
      rc_v3 != 0 and "state=cleaned 但成品仍在盘上" in out_v3, (rc_v3, out_v3))

# 三件套不全 → 残留（把 a 版还原成 archived，删掉封面）
with open(sm._manifest_path("剧D"), encoding="utf-8") as f:
    raw = json.load(f)
raw["versions"]["a"]["state"] = "archived"
with open(sm._manifest_path("剧D"), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False, indent=2)
cover = os.path.join(ARCH, "剧D", "剧D-封面.jpg")
if os.path.exists(cover):
    os.remove(cover)
rc_v4, out_v4 = capture(lambda: mv.verify("剧D"))
check("V6 三件套不完整 → 残留", rc_v4 != 0 and "三件套不完整" in out_v4, (rc_v4, out_v4))

old_argv = sys.argv
sys.argv = ["stream_manifest.py", "verify", "--manual", "--job", "剧D",
            "--manifest-dir", sm.MANIFEST_DIR]
rc_v5, out_v5 = capture(sm.main)
sys.argv = ["stream_manifest.py", "verify", "--job", "剧D"]
rc_v8, out_v8 = capture(sm.main)
sys.argv = ["stream_manifest.py", "verify", "--manual", "--scope", "staging",
            "--manifest-dir", sm.MANIFEST_DIR]
rc_v9, out_v9 = capture(sm.main)
sys.argv = ["stream_manifest.py", "verify", "--manual", "--scope", "bogus"]
rc_v10, out_v10 = capture(sm.main)
sys.argv = old_argv
check("V7 CLI verify --manual 分流可用（有残留 → 非 0）", rc_v5 != 0, (rc_v5, out_v5))
check("V8 CLI 无 --manual 时明确拒绝", rc_v8 == 1 and "请加 --manual" in out_v8, (rc_v8, out_v8))
check("V9 CLI --scope staging 无需 --job", rc_v9 == 0 and "scope=staging" in out_v9,
      (rc_v9, out_v9))
check("V10 CLI 非法 --scope 明确拒绝", rc_v10 == 1 and "只支持" in out_v10, (rc_v10, out_v10))

bad_open = [p for p in opened
            if "cover_job" in p.replace("/", "\\").split("\\")
            or os.path.basename(p) in mg.WATCHER_NAMES]
check("G1 手工跑批全程未 open 任何 job 侧文件", not bad_open, bad_open[:5])
check("G2 审计钩子确实观测到清单读取", any(p.endswith("剧C.json") for p in opened), len(opened))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'=' * 46}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
