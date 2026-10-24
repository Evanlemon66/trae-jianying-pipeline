#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理工作目录里未被任何草稿引用的产物。

草稿只记录素材绝对路径、不复制音频，所以清理必须先看引用关系：
本工具只删「没有任何 draft_content.json 的 audio 材质所指向」的文件，
被引用的干声/伴奏一律保留。

用法:
  python prune_workdirs.py                      # 只统计，不删除（默认）
  python prune_workdirs.py --apply              # 删除无引用文件
  python prune_workdirs.py --apply --older-than 30   # 只删 30 天前的无引用文件
"""
import argparse
import glob
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import pipeline as pl  # noqa: E402

# 状态机、报告、日志不参与清理
_KEEP_NAMES = ("_batch_state.json", "_subtitle_audit.json", "_timings.jsonl")


def _human(n):
    return f"{n / 1048576:.1f} MB" if n < 1048576 * 1024 else f"{n / 1073741824:.2f} GB"


def _is_protected_name(name):
    return (name in _KEEP_NAMES or name.startswith("_")
            or name.endswith((".log", ".jsonl")))


def main():
    ap = argparse.ArgumentParser(description="清理未被草稿引用的工作目录产物")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只列出）")
    ap.add_argument("--older-than", type=float, default=0,
                    help="只处理 N 天前的文件（默认全部）")
    ap.add_argument("--root", default=_HERE, help="工作目录所在目录（默认 pipeline/）")
    args = ap.parse_args()

    refs = pl.referenced_media_paths()
    if refs is None:
        print("✗ 有草稿无法解析，无法确认引用关系，已中止（未删除任何文件）")
        sys.exit(1)
    cutoff = time.time() - args.older_than * 86400 if args.older_than else None
    print(f"📚 草稿中仍被引用的音频文件: {len(refs)} 个\n")

    workdirs = sorted(d for d in glob.glob(os.path.join(args.root, "_work*"))
                      if os.path.isdir(d))
    if not workdirs:
        print("没有找到 _work* 目录")
        return

    total_kept = total_orphan = 0
    kept_n = orphan_n = 0
    plan = []
    for wd in workdirs:
        kept = orphan = 0
        kept_c = orphan_c = 0
        samples = []
        for dirpath, _dirnames, filenames in os.walk(wd):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if _is_protected_name(fn):
                    continue
                if pl._media_key(fp) in refs:
                    kept += st.st_size
                    kept_c += 1
                    continue
                if cutoff and st.st_mtime >= cutoff:
                    continue
                orphan += st.st_size
                orphan_c += 1
                plan.append((fp, st.st_size, st.st_mtime))
                if len(samples) < 3:
                    samples.append((fn, st.st_size))
        total_kept += kept
        total_orphan += orphan
        kept_n += kept_c
        orphan_n += orphan_c
        print(f"📂 {os.path.basename(wd)}")
        print(f"   引用保留 {_human(kept)}（{kept_c} 个） | 可清理 {_human(orphan)}（{orphan_c} 个）")
        for fn, sz in samples:
            print(f"     例: {fn}  {_human(sz)}")

    print(f"\n合计: 引用保留 {_human(total_kept)}（{kept_n} 个） | 可清理 {_human(total_orphan)}（{orphan_n} 个）")

    if not args.apply:
        print("（dry-run，未删除。确认后加 --apply 执行）")
        return

    freed = 0
    for fp, sz, _ in plan:
        try:
            os.remove(fp)
            freed += sz
        except OSError as e:
            print(f"  ⚠ 删除失败 {os.path.basename(fp)}: {e.strerror}")

    # 清掉空目录（保留 workdir 根）
    for wd in workdirs:
        for dirpath, dirnames, filenames in os.walk(wd, topdown=False):
            if os.path.abspath(dirpath) == os.path.abspath(wd):
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                pass
    print(f"\n🧹 已删除 {_human(freed)}，释放完成")


if __name__ == "__main__":
    main()
