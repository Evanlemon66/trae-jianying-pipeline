# -*- coding: utf-8 -*-
"""一次性路径配置迁移：把旧 watch.roots 收敛为 series_root。

用法：
  python pipeline/migrate_config.py --dry-run
  python pipeline/migrate_config.py --write
  python pipeline/migrate_config.py --write --series-root "E:\\短剧"

只做确定性迁移，不猜路径：
  - series_root 为空且 watch.roots 恰好一个有效目录 → 写入该目录；
  - series_root 为空且 watch.roots 有多个 → 必须显式 --series-root；
  - export_dir / deps_root 缺失只报告，不自动填。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export_config as ec  # noqa: E402


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("presets.json 内容不是 JSON 对象")
    return data


def _backup(path):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = f"{path}.bak-{stamp}"
    shutil.copy2(path, target)
    return target


def _write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def plan(cfg, series_root=""):
    changes = {}
    current = str(cfg.get("series_root") or "").strip()
    if current:
        return changes
    roots = ((cfg.get("watch") or {}).get("roots") or [])
    valid = [r for r in roots if isinstance(r, str) and os.path.isdir(r)]
    if len(valid) == 1:
        changes["series_root"] = os.path.abspath(valid[0])
    elif str(series_root or "").strip():
        root = os.path.abspath(str(series_root).strip())
        if not os.path.isdir(root):
            raise ValueError(f"指定的 series_root 不存在: {root}")
        changes["series_root"] = root
    else:
        raise ValueError("series_root 为空，且 watch.roots 不是恰好一个有效目录；"
                         "请用 --series-root 显式指定（多个根不能自动猜）")
    return changes


def main(argv=None):
    ap = argparse.ArgumentParser(description="把旧配置迁移为 series_root 单一素材根")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只打印将要做的事（默认）")
    mode.add_argument("--write", action="store_true", help="备份后写回 presets.json")
    ap.add_argument("--series-root", default="", help="watch.roots 多个根时显式指定素材根")
    ap.add_argument("--config", default=ec.CONFIG_PATH, help="presets.json 路径")
    args = ap.parse_args(argv)

    path = os.path.abspath(args.config)
    if not os.path.isfile(path):
        print(f"找不到配置文件: {path}", file=sys.stderr)
        return 2
    ec.CONFIG_PATH = path
    try:
        cfg = _read(path)
        changes = plan(cfg, args.series_root)
    except (OSError, ValueError) as exc:
        print(f"迁移失败: {exc}", file=sys.stderr)
        return 2

    missing = [k for k in ("export_dir", "deps_root")
               if not str(cfg.get(k) or "").strip()]
    print(f"配置文件: {path}")
    print("计划修改: " + (json.dumps(changes, ensure_ascii=False) if changes else "(无需修改)"))
    print("仍需手工填写: " + ("、".join(missing) if missing else "(无)"))
    if not args.write:
        print("当前为 dry-run；确认后加 --write 写回。")
        return 0
    if missing:
        print("缺少必需项，未写回。请先补齐: " + "、".join(missing), file=sys.stderr)
        return 2
    if not changes:
        print("没有需要写入的变更。")
        return 0
    try:
        backup = _backup(path)
        merged = dict(cfg)
        merged.update(changes)
        _write_atomic(path, merged)
    except OSError as exc:
        print(f"写回失败: {exc}", file=sys.stderr)
        return 2

    print(f"已备份: {backup}")
    print(f"已写回: {path}")
    report = ec.validate_config("run")
    if report["issues"]:
        print("写回后仍有配置问题:")
        for item in report["issues"]:
            print(f"  - {item['message']}")
        return 2
    print("统一路径解析通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
