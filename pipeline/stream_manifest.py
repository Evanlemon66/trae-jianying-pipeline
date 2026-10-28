"""串流产物清单（manifest）：记录每次串流生成的关联文件，支持一键清理。

每次串流 pipeline 发布草稿时自动记录；导出成功后补充成品路径。
用户不需要某部剧的残留时，用 clean 一键清理所有关联文件并删除记录。

用法（在项目根目录执行）：
  python pipeline/stream_manifest.py list              # 列出所有记录及占用
  python pipeline/stream_manifest.py show <草稿名>      # 查看单条记录详情
  python pipeline/stream_manifest.py clean <草稿名>     # 清理单部剧的所有残留
  python pipeline/stream_manifest.py clean all         # 清理所有记录的残留

安全约束：绝不清理 source_dir（用户源视频目录本体）。
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
    print(f"\n清理: python pipeline/stream_manifest.py clean <草稿名|all>")


def show_one(draft_name):
    mp = _manifest_path(draft_name)
    if not os.path.exists(mp):
        print(f"无记录: {draft_name}")
        return
    with open(mp, encoding="utf-8") as f:
        d = json.load(f)
    print(json.dumps(d, ensure_ascii=False, indent=2))


def clean(draft_name):
    """清理一部剧的所有串流残留，成功后删除记录。"""
    mp = _manifest_path(draft_name)
    if not os.path.exists(mp):
        print(f"无记录: {draft_name}")
        return 1
    with open(mp, encoding="utf-8") as f:
        d = json.load(f)
    paths = d.get("paths", {})
    source_dir = paths.get("source_dir", "")
    print(f"清理 [{d['draft_name']}]:")
    for kind in ("export_file", "draft_dir", "jycache_dir", "workdir"):
        p = paths.get(kind)
        if not p:
            continue
        # 成品旁的伴随文件一并清理：<mp4>.progress.json 进度快照、<名>-封面.jpg 封面
        targets = [(p, kind)]
        if kind == "export_file":
            parent = os.path.dirname(os.path.abspath(p))
            if os.path.basename(parent) == d["draft_name"]:
                # 新布局：成品/封面/进度文件同处「剧名子目录」，整目录一次清理
                targets = [(parent, "export_bundle")]
            else:
                # 旧平铺布局兼容：逐个删除成品与伴随文件
                targets.append((os.path.abspath(p) + ".progress.json", f"{kind}旁:{os.path.basename(p)}.progress.json"))
                targets.append((os.path.splitext(p)[0] + "-封面.jpg", f"{kind}旁:{os.path.splitext(os.path.basename(p))[0]}-封面.jpg"))
        for tp, label in targets:
            if not os.path.exists(tp):
                if tp == p:
                    print(f"  [skip] {kind}: 不存在 {tp}")
                continue
            size = _entry_size(kind, tp)
            if os.path.isdir(tp):
                shutil.rmtree(tp, ignore_errors=True)
            else:
                try:
                    os.remove(tp)
                except OSError as e:
                    print(f"  [fail] {label}: {e}")
                    continue
            gone = not os.path.exists(tp)
            print(f"  [{'已删' if gone else '失败'}] {label}: {tp} ({_fmt(size)})")
    os.remove(mp)
    print(f"  记录已删除: {mp}")
    # source_dir 只提示，永不清理
    if source_dir:
        print(f"  （源视频目录保留不动: {source_dir}）")
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        show_all()
    elif cmd == "show" and len(sys.argv) >= 3:
        show_one(" ".join(sys.argv[2:]))
    elif cmd == "clean" and len(sys.argv) >= 3:
        if sys.argv[2].lower() == "all":
            if not os.path.isdir(MANIFEST_DIR):
                print("（无任何记录）")
                return
            names = []
            for fn in os.listdir(MANIFEST_DIR):
                if fn.endswith(".json"):
                    with open(os.path.join(MANIFEST_DIR, fn), encoding="utf-8") as f:
                        names.append(json.load(f).get("draft_name", fn[:-5]))
            for n in names:
                clean(n)
                print()
        else:
            clean(" ".join(sys.argv[2:]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
