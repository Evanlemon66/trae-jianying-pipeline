"""archive_job.py — 已完成剧的归档：置 stage=archived 并搬入独立归档池。

背景：watcher（lib/cover_watch.py）靠 check_trigger 的 stage 白名单 +
video.status=done 挡住已导出剧，但剧目录仍留在公共 roots；本脚本负责
"显式归档"：写 stage=archived（check_trigger 硬拦），再把整剧目录移出 roots。

用法（项目根目录）：
  python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档" --dry-run
  python pipeline/archive_job.py --job "<cover_job>" --archive-root "D:/短剧_存档"

准入（默认满足其一即可）：
  - publish.status == "published"（已有成功回执）
  - video.export_state == "cleaned"（成品已清理，视为发布后的收尾）
其余情况跳过；确需归档用 --force。
拒绝条件：.watcher.lock 的 pid 仍存活；.export_state.json status=exporting；
归档目标已存在；archive_root 等于或位于任一 watch.roots 之下（规范化真实路径比较）；
watch.roots 未配置（fail-closed，无法校验位置时拒绝执行）；
job_dir 不是 <剧名>/cover_job；剧集目录是盘符根；剧集目录不是 watch.roots 的直接子级。
以上路径相关拒绝不受 --force 影响（搬错目录不可逆）。

断点可重入：stage 已是 archived 时，只要源目录仍在、归档目标不存在，就继续移动；
只有"源目录已不在"或"目标已存在"才跳过。

行为（非 dry-run）：
  1. job.json 置 stage="archived"（job_manifest.update_job，revision+1，history 记 archived）
  2. 整个剧集目录 <root>/<剧名> 移动到 <archive_root>/<剧名>（同盘为改名）
  3. pipeline/_stream_manifests/*.json 中 paths.source_dir 指向旧目录的记录改写为新路径
  --dry-run 只打印计划，不写盘、不移动。
archive_root 未传时读 pipeline/presets.json 的 watch.archive_root。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
sys.path.insert(0, os.path.join(_REPO, "pipeline"))

import job_manifest as jm  # noqa: E402
import export_config as pc  # noqa: E402

LOCK_NAME = ".watcher.lock"
STATE_NAME = ".export_state.json"


def _norm_job_dir(path: str) -> str:
    path = os.path.abspath(path)
    if os.path.isfile(path) and os.path.basename(path) == "job.json":
        path = os.path.dirname(path)
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        for line in out.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) > 1 and cols[1] == str(pid):
                return True
    except Exception:
        pass
    return False


def _lock_alive(job_dir: str) -> bool:
    try:
        with open(os.path.join(job_dir, LOCK_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return _pid_alive(int((data or {}).get("pid") or 0))


def _exporting(job_dir: str) -> bool:
    try:
        with open(os.path.join(job_dir, STATE_NAME), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return False
    return (st or {}).get("status") == "exporting"


def _real(path: str) -> str:
    """规范化真实路径（Windows 下大小写不敏感）。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _watch_roots() -> list[str]:
    roots = (pc.get_watch_config() or {}).get("roots") or []
    return [r for r in roots if isinstance(r, str) and r.strip()]


def _root_hit(archive_root: str, roots: list[str]) -> str | None:
    """archive_root 等于或位于某个 root 之下时返回该 root，否则 None。"""
    ar = _real(archive_root)
    for root in roots:
        rr = _real(root).rstrip(os.sep)
        if ar == rr or ar.startswith(rr + os.sep):
            return root
    return None


def _series_dir_guard(job_dir: str, series_dir: str, roots: list[str]) -> str | None:
    """移动前的路径断言；返回拒绝原因，None=通过。不受 --force 影响。

    - job_dir 必须是 <剧集目录>/cover_job（out_dir 覆盖后仍必须是这个名字）
    - 剧集目录不能是盘符根/文件系统根（dirname(root) == root）
    - 剧集目录必须是某个 watch.roots 的直接子级（不是更深层，也不是 root 本身）

    缺这层校验时，out_dir 一旦指到共享父目录，归档会把整层目录（甚至 D:\\）
    搬进归档池，而 shutil.move 不可逆。
    """
    base = os.path.basename(os.path.normpath(job_dir)).lower()
    if base != "cover_job":
        return f"job_dir 必须是 <剧名>/cover_job（实际: {job_dir}）"
    sd = os.path.normpath(series_dir)
    if os.path.normcase(os.path.dirname(sd)) == os.path.normcase(sd):
        return f"剧集目录不能是盘符根/文件系统根: {series_dir}"
    parent = os.path.dirname(os.path.realpath(sd)).rstrip("\\/")
    for root in roots:
        rr = os.path.realpath(root).rstrip("\\/")
        if os.path.normcase(parent) == os.path.normcase(rr):
            return None
    return (f"剧集目录必须是某个 watch.roots 的直接子级"
            f"（实际: {series_dir}；roots: {', '.join(roots)}）")


def _eligible(job: dict) -> bool:
    pub = str((job.get("publish") or {}).get("status") or "")
    exp = (job.get("video") or {}).get("export_state")
    return pub == "published" or exp == "cleaned"


def _rewrite_manifests(manifest_dir: str, old_dir: str, new_dir: str,
                       dry_run: bool) -> int:
    """把 paths.source_dir=旧剧集目录 的记录改写为新路径；返回命中记录数。"""
    if not os.path.isdir(manifest_dir):
        return 0
    old_key = os.path.normcase(os.path.normpath(old_dir))
    hits = []
    for fn in sorted(os.listdir(manifest_dir)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(manifest_dir, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        src = (data.get("paths") or {}).get("source_dir")
        if not src or os.path.normcase(os.path.normpath(src)) != old_key:
            continue
        hits.append((fp, data))
    for fp, data in hits:
        print(f"  [manifest] {os.path.basename(fp)}: source_dir → {new_dir}")
        if not dry_run:
            data["paths"]["source_dir"] = os.path.normpath(new_dir)
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, fp)
    return len(hits)


def archive(job_dir: str, archive_root: str, manifest_dir: str, *,
            dry_run: bool = False, force: bool = False,
            roots: list[str] | None = None) -> int:
    """归档一部剧。返回 0=成功/计划完成；1=跳过；2=失败。"""
    job_dir = _norm_job_dir(job_dir)
    if not os.path.isfile(os.path.join(job_dir, "job.json")):
        print(f"[FAIL] 无 job.json: {job_dir}")
        return 2
    series_dir = os.path.dirname(os.path.abspath(job_dir.rstrip("\\/")))
    name = os.path.basename(series_dir)
    job = jm.load_job(os.path.join(job_dir, "job.json"))
    print(f"归档 [{name}] stage={job.get('stage')}")

    archive_root = os.path.abspath(archive_root)
    roots_list = _watch_roots() if roots is None else roots
    if not roots_list:
        # fail-closed：没有 roots 就无法判断归档池是否落在公共区，宁可不搬
        print("  [FAIL] 未配置 watch.roots，无法校验归档池位置，已拒绝执行；"
              "请检查 pipeline/presets.json 的 watch 段 roots")
        return 2
    hit = _root_hit(archive_root, roots_list)
    if hit:
        print(f"  [FAIL] archive_root 不能等于或位于 watch.roots 之下"
              f"（命中 root: {hit}）: {archive_root}")
        return 2

    # 路径断言先于 --force / dry-run：搬错的目录不可逆，必须硬拦
    guard_err = _series_dir_guard(job_dir, series_dir, roots_list)
    if guard_err:
        print(f"  [FAIL] {guard_err}")
        return 2

    target = os.path.join(archive_root, name)
    archived = jm.canon_stage(job.get("stage")) == "archived"

    if archived:
        # 断点可重入：上次可能已置 archived 但移动失败，只要源还在、目标不存在就续做
        if not os.path.isdir(series_dir):
            if os.path.exists(target):
                print(f"  [skip] 已归档完成（源已不在，目标存在）: {target}")
                return 1
            print(f"  [FAIL] stage=archived 但源目录与归档目标都不存在: {series_dir} -> {target}")
            return 2
        if os.path.exists(target):
            print(f"  [skip] 归档目标已存在（源目录仍在，需人工处理冲突）: {target}")
            return 1
        print("  [resume] 已是 archived，源目录仍在且目标不存在，继续移动")
    else:
        if not force and not _eligible(job):
            print("  [skip] 未满足准入（publish.status!=published 且 video.export_state!=cleaned）；"
                  "确需归档请加 --force")
            return 1
        if _lock_alive(job_dir):
            print("  [skip] 存在活锁（.watcher.lock pid 仍存活），请先停止该剧的调度")
            return 1
        if _exporting(job_dir):
            print("  [skip] .export_state.json status=exporting（导出进行中）")
            return 1
        if os.path.normcase(target) == os.path.normcase(series_dir):
            print("  [skip] archive_root 与公共 roots 相同，移动等于原地不动")
            return 1
        if os.path.exists(target):
            print(f"  [skip] 归档目标已存在: {target}")
            return 1

    print(f"  剧集目录: {series_dir}")
    print(f"  归档目标: {target}")
    if not archived:
        print("  stage → archived")
    if dry_run:
        hits = _rewrite_manifests(manifest_dir, series_dir, target, dry_run=True)
        print(f"  manifest 待更新: {hits} 条")
        print("  （dry-run：未写 job.json、未移动目录）")
        return 0

    if not archived:
        try:
            jm.update_job(job_dir, {"stage": "archived"}, event="archived",
                          detail=f"归档到 {target}")
        except (ValueError, OSError) as e:
            print(f"  [FAIL] 置 stage=archived 失败: {e}")
            return 2
    os.makedirs(archive_root, exist_ok=True)
    try:
        shutil.move(series_dir, target)
    except OSError as e:
        print(f"  [FAIL] 移动目录失败（stage 已置 archived，调度已阻断；修复后可重跑续做）: {e}")
        return 2
    hits = _rewrite_manifests(manifest_dir, series_dir, target, dry_run=False)
    print(f"  [ok] 已归档: {target}（manifest 更新 {hits} 条）")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", required=True, help="cover_job 目录或其中的 job.json")
    ap.add_argument("--archive-root", default=None,
                    help="归档池根目录（默认读 presets.json 的 watch.archive_root）")
    ap.add_argument("--manifest-dir", default=os.path.join(_REPO, "pipeline", "_stream_manifests"),
                    help="串流产物清单位置（默认 pipeline/_stream_manifests）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘、不移动")
    ap.add_argument("--force", action="store_true", help="跳过发布/清理准入校验")
    args = ap.parse_args(argv)

    root = args.archive_root or (pc.get_watch_config() or {}).get("archive_root")
    if not root:
        print("[FAIL] 未指定 --archive-root，且 presets.json watch 段无 archive_root")
        return 2
    return archive(args.job, root, args.manifest_dir,
                   dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
