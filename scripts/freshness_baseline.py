# -*- coding: utf-8 -*-
"""生成/追加「sidecar 机制上线前」的一次性冻结基线。

背景：sidecar 上线前导出的产物没有 `<mp4>.freshness.json`。为了不让它们被
误报成 evidence_missing，上线时冻结一份清单；命中清单的按 legacy(baseline)
处理，只标注不告警。清单不随目录变化自动扩充——新发现必须由人显式跑本命令。

用法：
  # 只读扫描，先看会写什么（默认 dry-run，不落盘）
  python scripts/freshness_baseline.py --root "<剧集根>"

  # 首次生成（文件已存在时必须二选一：--force 覆盖 / --append 追加）
  python scripts/freshness_baseline.py --root "<剧集根>" --write

  # 人工补充新发现的历史产物（只增不删）
  python scripts/freshness_baseline.py --root "<剧集根>" --append --write

  # 显式整份重算（会丢掉旧条目，谨慎）
  python scripts/freshness_baseline.py --root "<剧集根>" --force --write

约束：只读 job.json 与成品文件；只写基线文件本身，不改任何 job 数据。
      不启动/关闭剪映、不跑导出、不启停 watcher。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))

import freshness  # noqa: E402
from export_config import get_series_root  # noqa: E402

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache",
             "node_modules", "__jycache__", ".trae-html-share-packages"}


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iter_job_json(roots):
    """只找 <root>/**/cover_job/job.json，跳过缓存/隐藏目录。"""
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            print(f"  [skip] 目录不存在: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS and not d.startswith("_work")]
            if os.path.basename(dirpath) == "cover_job" and "job.json" in filenames:
                yield os.path.join(dirpath, "job.json")
                dirnames[:] = []


def _resolve_export_file(job_dir, raw):
    path = str(raw)
    if not os.path.isabs(path):
        # 与 freshness.freshness_of 一致：相对序列目录（cover_job 的上一级）
        path = os.path.join(os.path.dirname(os.path.normpath(job_dir)), path)
    return os.path.normpath(path)


def scan_roots(roots, want_sha=False):
    """只读扫描，返回 (entries, report)。不认识的结构进 report，不猜。"""
    entries, unrecognized, missing, conflicts = [], [], [], []
    stats = {"jobs": 0, "old_ledger": 0, "has_sidecar": 0}
    product_roots = set()

    for job_path in _iter_job_json(roots):
        job_dir = os.path.dirname(job_path)
        stats["jobs"] += 1
        try:
            with open(job_path, encoding="utf-8-sig") as fp:
                job = json.load(fp)
        except Exception as e:
            unrecognized.append((job_path, f"job.json 解析失败: {e}"))
            continue
        if not isinstance(job, dict):
            unrecognized.append((job_path, "job.json 顶层不是对象"))
            continue
        video = job.get("video")
        if video is None:
            continue
        if not isinstance(video, dict):
            unrecognized.append((job_path, "video 不是对象"))
            continue

        outs = video.get("outputs")
        if outs is None:
            # 老台账：只有顶层 export_state/export_file，legacy 已能处理，不需要基线
            if str(video.get("export_state") or "").lower() == "exported":
                stats["old_ledger"] += 1
            continue
        if not isinstance(outs, dict):
            unrecognized.append((job_path, "video.outputs 不是对象"))
            continue

        for ver, ent in outs.items():
            if not isinstance(ent, dict):
                unrecognized.append((job_path, f"outputs[{ver}] 不是对象"))
                continue
            state = str(ent.get("state") or "").lower()
            if state != "exported":
                if state and state != "cleaned":
                    unrecognized.append((job_path, f"outputs[{ver}].state={state!r} 不认识"))
                continue
            raw = ent.get("export_file")
            if not raw:
                unrecognized.append((job_path, f"outputs[{ver}] 已 exported 但缺 export_file"))
                continue
            export_file = _resolve_export_file(job_dir, raw)
            product_roots.add(os.path.dirname(export_file).replace(os.sep, "/"))
            if os.path.isfile(export_file + ".freshness.json"):
                stats["has_sidecar"] += 1
                continue
            if not os.path.isfile(export_file):
                missing.append({"job": job_dir.replace(os.sep, "/"), "version": ver,
                                "export_file": export_file.replace(os.sep, "/"),
                                "reason": "成品文件不存在（freshness 会报 output_missing）"})
                continue
            size = os.path.getsize(export_file)
            entry = {
                "job": job_dir.replace(os.sep, "/"),
                "job_id": job.get("job_id"),
                "series": job.get("series"),
                "version": freshness._norm_version(ver),
                "draft": ent.get("draft"),
                "export_file": export_file.replace(os.sep, "/"),
                "size_bytes": size,
                "ledger_size_bytes": ent.get("size_bytes"),
                "exported_at": ent.get("exported_at"),
                "job_revision": job.get("revision"),
                "job_updated_at": job.get("updated_at"),
            }
            if ent.get("size_bytes") not in (None, size):
                conflicts.append({"job": entry["job"], "version": entry["version"],
                                  "ledger_size_bytes": ent.get("size_bytes"),
                                  "actual_size_bytes": size})
                entry["size_mismatch"] = True
            if want_sha:
                entry["sha256"] = freshness.sha256_file(export_file)
            entries.append(entry)

    entries.sort(key=lambda e: (str(e.get("job") or ""), str(e.get("version") or "")))
    report = {"stats": stats, "unrecognized": unrecognized, "missing_output": missing,
              "size_mismatch": conflicts,
              "product_roots": sorted(product_roots)}
    return entries, report


def _entry_key(entry):
    ident = str(entry.get("job_id") or "")
    if not ident:
        ident = os.path.normcase(os.path.normpath(str(entry.get("job") or "")))
    return (ident, freshness._norm_version(entry.get("version")))


def _script_arg():
    """argv[0] 的展示形式：同盘用相对路径，跨盘保留绝对路径。

    Windows 上 os.path.relpath 跨盘符会抛 ValueError（path is on mount 'C:'）；
    脚本在 C: 盘、工作目录在 D: 盘时就是这种情况。退回绝对路径，保证
    command 字段始终是可读、可直接复用的真实命令。
    """
    raw = sys.argv[0] or __file__
    path = os.path.abspath(raw)
    try:
        path = os.path.relpath(path)
    except ValueError:
        pass  # 不同盘符无法求相对路径，保留绝对路径
    return path.replace(os.sep, "/")


def _cmdline():
    return "python " + subprocess.list2cmdline([_script_arg()] + sys.argv[1:])


def build_payload(entries, roots, report, command):
    entries = sorted(entries, key=lambda e: (str(e.get("job") or ""),
                                             str(e.get("version") or "")))
    return {
        "schema": 1,
        "kind": freshness.BASELINE_KIND,
        "generated_at": now_iso(),
        "generated_by": "scripts/freshness_baseline.py",
        "command": command,
        "policy": "frozen; runtime read-only; explicit command only; append never removes",
        "scanned_roots": [os.path.abspath(r).replace(os.sep, "/") for r in roots],
        "product_roots": report.get("product_roots") or [],
        "entries_count": len(entries),
        "entries_digest": freshness.baseline_entries_digest(entries),
        "entries": entries,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 sidecar 上线前的一次性冻结基线")
    ap.add_argument("--root", action="append", default=None,
                    help="只读扫描根目录，可重复；默认按统一配置解析（series_root → watch.roots[0] → D:\\短剧）")
    ap.add_argument("--out", default=None,
                    help="基线文件路径；默认 data/freshness_legacy_baseline.json")
    ap.add_argument("--write", action="store_true", help="真正落盘（默认 dry-run）")
    ap.add_argument("--append", action="store_true", help="只追加新条目，不删除旧条目")
    ap.add_argument("--force", action="store_true", help="整份重算并覆盖（会丢旧条目）")
    ap.add_argument("--sha", action="store_true", help="为每条记录计算成品 sha256（慢）")
    args = ap.parse_args(argv)

    if args.append and args.force:
        ap.error("--append 与 --force 只能二选一")
    roots = args.root or [get_series_root()]
    out = os.path.abspath(args.out or freshness.baseline_path())

    print(f"扫描根目录: {', '.join(os.path.abspath(r) for r in roots)}")
    entries, report = scan_roots(roots, want_sha=args.sha)
    st = report["stats"]
    print(f"job 数: {st['jobs']}  已带 sidecar: {st['has_sidecar']}  "
          f"老台账(无需基线): {st['old_ledger']}")
    print(f"待冻结条目: {len(entries)}")

    for item in report["unrecognized"]:
        print(f"  [unrecognized] {item[0]} :: {item[1]}")
    for item in report["missing_output"]:
        print(f"  [missing_output] {item['job']} 版本 {item['version']}")
    for item in report["size_mismatch"]:
        print(f"  [size_mismatch] {item['job']} 版本 {item['version']} "
              f"台账 {item['ledger_size_bytes']} / 实际 {item['actual_size_bytes']}")

    existing = None
    existing_broken = False
    if os.path.isfile(out):
        try:
            with open(out, encoding="utf-8-sig") as fp:
                existing = json.load(fp)
        except Exception as e:
            print(f"  [warn] 现有清单解析失败: {e}")
            existing = None
            existing_broken = True

    def _existing_valid(data):
        """与运行期 load_baseline 同口径：kind/schema/digest 任一不符即整份失效。"""
        try:
            ents = data.get("entries")
            return (data.get("kind") == freshness.BASELINE_KIND
                    and int(data.get("schema") or 0) == 1
                    and isinstance(ents, list)
                    and freshness.baseline_entries_digest(ents)
                    == data.get("entries_digest"))
        except Exception:
            return False

    if existing is not None and not _existing_valid(existing):
        # 手改过清单 / 摘要不符：追加会把篡改后的条目重新签名成"合法"，
        # 等于洗掉校验，必须拒绝。人工核对后用 --force 整份重算。
        if not args.force:
            print(f"\n拒绝写入：{out} 的 kind/schema/entries_digest 校验不通过。")
            print("  请先人工核对清单内容；确认无误后再显式 --force 整份重算。")
            return 5
        existing = None
    if existing_broken:
        if not args.force:
            print(f"\n拒绝写入：{out} 存在但无法解析。")
            print("  请先人工修复；确认要丢弃旧内容时再显式 --force 整份重算。")
            return 5
        existing = None

    if existing and not args.append and not args.force:
        print(f"\n拒绝写入：{out} 已存在。请显式选择：")
        print("  --append  只追加新条目（推荐，不删除历史）")
        print("  --force   整份重算并覆盖（会丢旧条目）")
        return 4

    merged = entries
    if existing and args.append:
        old = existing.get("entries") or []
        by_key = {_entry_key(e): e for e in old if isinstance(e, dict)}
        added = 0
        for e in entries:
            k = _entry_key(e)
            if k not in by_key:
                by_key[k] = e
                added += 1
            elif int(by_key[k].get("size_bytes") or -1) != int(e.get("size_bytes") or -2):
                print(f"  [conflict] {e.get('job')} 版本 {e.get('version')} "
                      f"size 与清单不一致，保留旧条目（不覆盖）")
        merged = sorted(by_key.values(), key=lambda e: (str(e.get("job") or ""),
                                                        str(e.get("version") or "")))
        print(f"追加模式：旧条目 {len(old)}，新增 {added}，合计 {len(merged)}")

    payload = build_payload(merged, roots, report, _cmdline())

    if not args.write:
        print(f"\n[dry-run] 未写盘。将写入: {out}")
        print(json.dumps({k: v for k, v in payload.items() if k != "entries"},
                         ensure_ascii=False, indent=2))
        print(f"条目示例: {json.dumps(payload['entries'][:2], ensure_ascii=False, indent=2)}")
        return 0

    freshness._write_json_atomic(out, payload)
    print(f"\n已写入: {out}")
    print(f"条目数: {payload['entries_count']}  digest: {payload['entries_digest']}")
    print("提示：运行期只读此文件；不要让任何自动流程改写它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
