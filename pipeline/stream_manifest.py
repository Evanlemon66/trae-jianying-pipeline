"""串流产物清单（manifest）：记录每次串流生成的关联文件，支持一键清理。

每次串流 pipeline 发布草稿时自动记录；导出成功后补充成品路径。
用户不需要某部剧的残留时，用 clean 一键清理所有关联文件并删除记录。

用法（在项目根目录执行）：
  python pipeline/stream_manifest.py list              # 列出所有记录及占用
  python pipeline/stream_manifest.py show <草稿名>      # 查看单条记录详情
  python pipeline/stream_manifest.py clean --job "<草稿名>" [--dry-run]      # 清理单部剧
  python pipeline/stream_manifest.py clean --all [--dry-run]                # 清理所有记录
  python pipeline/stream_manifest.py clean --job "<草稿名>" --require-receipts   # 严格模式
  python pipeline/stream_manifest.py clean --job "<草稿名>" --include-drafts     # 连 C 盘草稿一起删

默认行为（信任"用户让清理＝已发布"，发布流程尚未自动化）：
  清理 ① __jycache__ ② _work_* 工作目录 ③ 导出成品目录（mp4+封面+progress.json 整体）；
  **默认不删 C 盘剪映草稿**，需显式 --include-drafts。
  --require-receipts 严格模式：必须有成功回执（06_publish/receipts 或 platform json 标 published+url/id），否则整剧跳过。
  --dry-run 只打印计划与大小，不做任何删除、不改 manifest。

安全约束：绝不清理 source_dir（用户源视频目录本体）；保护区（_broken / cover_job / s2·s3·ss.mp4 /
pipeline/_work 旧状态文件）命中即跳过；manifest 记录本身永不删除。
"""
import json, os, sys, time, shutil

sys.stdout.reconfigure(encoding="utf-8")

MANIFEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_stream_manifests")


def _manifest_path(draft_name):
    safe = draft_name.replace("/", "_").replace("\\", "_").replace(":", "_")
    return os.path.join(MANIFEST_DIR, f"{safe}.json")


def record(draft_name, source_dir=None, draft_dir=None, jycache_dir=None,
           workdir=None, export_file=None):
    """记录/更新一条串流产物清单（同草稿名多次调用做字段合并）。"""
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    mp = _manifest_path(draft_name)
    data = {}
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["draft_name"] = draft_name
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data.setdefault("recorded_at", data["updated_at"])
    for k, v in (("source_dir", source_dir), ("draft_dir", draft_dir),
                 ("jycache_dir", jycache_dir), ("workdir", workdir),
                 ("export_file", export_file)):
        if v:
            data.setdefault("paths", {})[k] = v
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return mp


def _dir_size(p):
    total = 0
    for root, _, files in os.walk(p):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _fmt(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _entry_size(kind, p):
    if not p or not os.path.exists(p):
        return 0
    if kind == "export_file" or os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    return _dir_size(p)


def show_all():
    if not os.path.isdir(MANIFEST_DIR):
        print("（无任何记录）")
        return
    files = sorted(f for f in os.listdir(MANIFEST_DIR) if f.endswith(".json"))
    if not files:
        print("（无任何记录）")
        return
    print(f"{'草稿名':<44} {'残留大小':>10}  状态")
    for fn in files:
        with open(os.path.join(MANIFEST_DIR, fn), encoding="utf-8") as f:
            d = json.load(f)
        paths = d.get("paths", {})
        total = sum(_entry_size(k, p) for k, p in paths.items() if k != "source_dir")
        missing = [k for k, p in paths.items()
                   if k != "source_dir" and p and not os.path.exists(p)]
        status = "已清理" if len(missing) == len([k for k in paths if k != "source_dir"]) and paths else \
                 (f"缺 {len(missing)} 项" if missing else "完整")
        print(f"{d['draft_name']:<44} {_fmt(total):>10}  {status}")
    print(f"\n清理: python pipeline/stream_manifest.py clean --job \"<草稿名>\" [--dry-run]")


def show_one(draft_name):
    mp = _manifest_path(draft_name)
    if not os.path.exists(mp):
        print(f"无记录: {draft_name}")
        return
    with open(mp, encoding="utf-8") as f:
        d = json.load(f)
    print(json.dumps(d, ensure_ascii=False, indent=2))


PROTECTED_FILES = ("s2.mp4", "s3.mp4", "ss.mp4")
SUCCESS_WORDS = ("success", "published", "uploaded", "succeeded", "成功", "已发布")


def _blocked_reason(tp, source_dir):
    """保护区断言：返回不可删原因；空串表示可以删。"""
    a = os.path.abspath(tp)
    s = os.path.abspath(source_dir) if source_dir else ""
    if s and a == s:
        return "等于源视频目录"
    if s and s.startswith(a + os.sep):
        return "包含源视频目录"
    parts = a.split(os.sep)
    if "_broken" in parts:
        return "位于 _broken 保留区"
    if "cover_job" in parts:
        return "位于 cover_job 正式产物区"
    if os.path.basename(a) in PROTECTED_FILES:
        return "散落成品文件（s2/s3/ss.mp4）"
    if os.path.basename(a) == "_work":
        return "pipeline/_work 旧状态目录"
    return ""


def _text_has_success(path):
    """判定一个回执/平台 json 是否表示"成功发布"。"""
    try:
        if os.path.getsize(path) > 2 * 1024 * 1024:
            return False
        with open(path, encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return False
    try:
        d = json.loads(txt)
    except ValueError:
        d = None
    if isinstance(d, dict):
        st = str(d.get("status") or d.get("state") or "").strip().lower()
        if st in ("ok", "success", "succeeded", "published", "uploaded"):
            return True
        if d.get("url") or d.get("video_id") or d.get("videoId"):
            return True
    low = txt.lower()
    return any(w in low for w in SUCCESS_WORDS)


def receipts_status(source_dir):
    """严格模式：06_publish/receipts 内有成功回执，或 platform json 标 published 且有 url/id。"""
    if not source_dir:
        return False, "记录无 source_dir，无法定位 cover_job"
    pub = os.path.join(source_dir, "cover_job", "06_publish")
    if not os.path.isdir(pub):
        return False, "无 06_publish 目录"
    rec = os.path.join(pub, "receipts")
    if os.path.isdir(rec):
        for fn in sorted(os.listdir(rec)):
            fp = os.path.join(rec, fn)
            if os.path.isfile(fp) and _text_has_success(fp):
                return True, f"回执 {fn}"
    for fn in sorted(os.listdir(pub)):
        if fn.lower().endswith(".json") and os.path.isfile(os.path.join(pub, fn)):
            fp = os.path.join(pub, fn)
            if _text_has_success(fp):
                try:
                    with open(fp, encoding="utf-8") as f:
                        d = json.load(f)
                except ValueError:
                    continue
                st = str(d.get("status") or "").strip().lower()
                if st in ("published", "uploaded") and (d.get("url") or d.get("video_id")):
                    return True, f"{fn} status={st}"
    return False, "无成功回执（receipts 为空、platform json 未标 published/url）"


def _plan(paths, draft_name, include_drafts):
    """清理计划：中间产物 → 成品 →（可选）草稿。返回 [(label, kind, path)]。"""
    items = []
    for kind in ("jycache_dir", "workdir"):
        if paths.get(kind):
            items.append((kind, kind, paths[kind]))
    ef = paths.get("export_file")
    if ef:
        parent = os.path.dirname(os.path.abspath(ef))
        if os.path.basename(parent) == draft_name:
            # 新布局：成品/封面/进度同处「剧名子目录」，整目录一次清理
            items.append(("export_bundle", "export_bundle", parent))
        else:
            # 旧平铺布局兼容：逐个列成品与伴随文件
            stem = os.path.splitext(os.path.abspath(ef))[0]
            items.append(("export_file", "export_file", os.path.abspath(ef)))
            items.append((f"{os.path.basename(stem)}.progress.json", "export_file", stem + ".mp4.progress.json"))
            items.append((f"{os.path.basename(stem)}-封面.jpg", "export_file", stem + "-封面.jpg"))
    if include_drafts and paths.get("draft_dir"):
        items.append(("draft_dir", "draft_dir", paths["draft_dir"]))
    return items


def clean(draft_name, dry_run=False, include_drafts=False, require_receipts=False):
    """清理一部剧的串流残留。默认信任"已发布"，不删草稿（除非 include_drafts）。

    返回 0=已执行或计划完成；1=记录不存在或被严格模式跳过。
    """
    mp = _manifest_path(draft_name)
    if not os.path.exists(mp):
        print(f"无记录: {draft_name}")
        return 1
    with open(mp, encoding="utf-8") as f:
        d = json.load(f)
    paths = d.get("paths", {})
    title = d.get("draft_name", draft_name)
    source_dir = paths.get("source_dir", "")
    print(f"清理 [{title}]:")
    if require_receipts:
        ok, why = receipts_status(source_dir)
        if not ok:
            print(f"  [跳过] 严格模式未通过：{why}")
            return 1
        print(f"  [通过] 回执校验：{why}")

    total = 0
    ready = []
    for label, kind, p in _plan(paths, title, include_drafts):
        why = _blocked_reason(p, source_dir)
        if why:
            print(f"  [受保护] {label}: {p}（{why}），跳过")
            continue
        if not os.path.exists(p):
            print(f"  [skip] {label}: 不存在 {p}")
            continue
        size = _entry_size(kind, p)
        total += size
        ready.append((label, kind, p, size))
        print(f"  [{'计划' if dry_run else '待删'}] {label}: {p} ({_fmt(size)})")
    print(f"  合计: {_fmt(total)}")
    if dry_run:
        print("  （dry-run：未执行任何删除，记录未改动）")
    else:
        print("  （非 dry-run：即将实际删除，可先用 --dry-run 复核）")
        for label, kind, p, size in ready:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"  [fail] {label}: {e}")
                    continue
            if not os.path.exists(p):
                print(f"  [已删] {label}: {p} ({_fmt(size)})")
            else:
                print(f"  [失败] {label}: {p}")
    # source_dir 只提示，永不清理；manifest 记录本身也永不删除
    if source_dir:
        print(f"  （源视频目录保留不动: {source_dir}）")
    return 0


def _manifest_names():
    if not os.path.isdir(MANIFEST_DIR):
        return []
    names = []
    for fn in sorted(os.listdir(MANIFEST_DIR)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(MANIFEST_DIR, fn), encoding="utf-8") as f:
                    names.append(json.load(f).get("draft_name", fn[:-5]))
            except ValueError:
                continue
    return names


def _parse_clean_args(args):
    """解析 clean 子命令：--job/--all/--dry-run/--require-receipts/--include-drafts，兼容裸名字与 all。"""
    opt = {"job": None, "all": False, "dry_run": False,
           "require_receipts": False, "include_drafts": False}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--all":
            opt["all"] = True
        elif a == "--dry-run":
            opt["dry_run"] = True
        elif a == "--require-receipts":
            opt["require_receipts"] = True
        elif a == "--include-drafts":
            opt["include_drafts"] = True
        elif a in ("--job", "-j") and i + 1 < len(args):
            i += 1
            opt["job"] = args[i]
        elif a.lower() == "all" and not opt["job"]:
            opt["all"] = True
        elif not a.startswith("-") and not opt["job"]:
            opt["job"] = a
        i += 1
    return opt


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        show_all()
    elif cmd == "show" and len(sys.argv) >= 3:
        show_one(" ".join(sys.argv[2:]))
    elif cmd == "clean":
        opt = _parse_clean_args(sys.argv[2:])
        if not opt["all"] and not opt["job"]:
            print(__doc__)
            return
        names = _manifest_names() if opt["all"] else [opt["job"]]
        if not names:
            print("（无任何记录）")
            return
        skipped = []
        for n in names:
            rc = clean(n, dry_run=opt["dry_run"],
                       include_drafts=opt["include_drafts"],
                       require_receipts=opt["require_receipts"])
            if rc != 0:
                skipped.append(n)
            print()
        if skipped:
            print("跳过（严格模式/无记录）:")
            for n in skipped:
                print(f"  - {n}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
