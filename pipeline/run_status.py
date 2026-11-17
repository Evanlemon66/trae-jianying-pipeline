# -*- coding: utf-8 -*-
"""只读查看 batch_runner 的运行记录（_run.json + run-<id>.log）。

回答三个问题：这次跑的是哪条命令、什么时候开始/结束、退出码是什么、日志在哪。
本脚本只读，不改任何状态、不触发任何任务。

用法：
  python pipeline\\run_status.py                       # 列出最近若干次运行（含 Console 跑的）
  python pipeline\\run_status.py --series "<剧集目录>"  # 看该剧集最近一次
  python pipeline\\run_status.py --workdir "<工作目录>"  # 指定工作目录
  python pipeline\\run_status.py --json                # 机器可读
  python pipeline\\run_status.py --tail 40             # 附带日志尾部行数
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return None


def default_workdir(series_dir):
    """与 batch_runner.py 的默认工作目录规则保持一致。"""
    base = os.path.basename(series_dir.rstrip("\\/")) or "series"
    safe = re.sub(r'[<>:"/\\|?*\s]+', "", base)[:24] or "series"
    digest = hashlib.md5(base.encode("utf-8")).hexdigest()[:8]
    return os.path.join(_HERE, f"_work_{safe}_{digest}")


def read_run(workdir):
    data = _read_json(os.path.join(workdir, "_run.json")) if workdir else None
    return data if isinstance(data, dict) and data.get("run_id") else None


def _run_dirs():
    """所有可能存放 _run.json 的工作目录：手工/pipeline 默认目录 + Console 工作目录。"""
    dirs = glob.glob(os.path.join(_HERE, "_work*"))
    ui = os.path.join(_ROOT, "pipeline_ui", "_console", "workdir")
    if os.path.isdir(ui):
        try:
            dirs.extend(os.path.join(ui, n) for n in os.listdir(ui))
        except OSError:
            pass
    return dirs


def latest_runs(limit=20):
    out = []
    for workdir in _run_dirs():
        data = _read_json(os.path.join(workdir, "_run.json"))
        if isinstance(data, dict) and data.get("run_id"):
            out.append(data)
    out.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return out[:limit]


def tail_lines(path, count):
    if not (path and count and os.path.isfile(path)):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-count:]
    except OSError:
        return []


def _print_run(rec, tail=0):
    print(f"run_id   : {rec.get('run_id')}")
    print(f"命令     : {' '.join(rec.get('argv') or [])}")
    print(f"目录     : {rec.get('series_dir') or '-'}")
    print(f"工作目录 : {rec.get('workdir') or '-'}")
    print(f"时间     : {rec.get('started_at')} → {rec.get('ended_at') or '运行中'}")
    print(f"状态     : {rec.get('status')}（exit_code={rec.get('exit_code')}）")
    print(f"日志     : {rec.get('log_path') or '-'}")
    for line in tail_lines(rec.get("log_path"), tail):
        print("  | " + line)


def main(argv=None):
    ap = argparse.ArgumentParser(description="只读查看 batch_runner 运行记录")
    ap.add_argument("--workdir", help="工作目录（含 _run.json）")
    ap.add_argument("--series", help="剧集目录（按默认规则推导工作目录）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--tail", type=int, default=0, help="附带日志尾部行数")
    ap.add_argument("--limit", type=int, default=20, help="列出最近几次（默认 20）")
    args = ap.parse_args(argv)

    if args.workdir or args.series:
        workdir = args.workdir or default_workdir(os.path.abspath(args.series))
        rec = read_run(workdir)
        if not rec:
            print(f"未找到运行记录：{os.path.join(workdir, '_run.json')}")
            return 1
        if args.json:
            data = dict(rec)
            if args.tail:
                data["log_tail"] = tail_lines(rec.get("log_path"), args.tail)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            _print_run(rec, args.tail)
        return 0

    runs = latest_runs(args.limit)
    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=2))
        return 0
    if not runs:
        print("暂无运行记录（pipeline/_work*/_run.json）")
        return 0
    for rec in runs:
        print("-" * 60)
        _print_run(rec)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
