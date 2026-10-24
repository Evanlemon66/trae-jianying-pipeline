# -*- coding: utf-8 -*-
"""多剧集串行编排器：串流生成草稿 / 导出 / 联动，状态机断点续跑。

三种模式:
    full     （默认）生成草稿 → 导出，逐部联动
    generate 只生成草稿不导出（部间零等待），供稍后统一导出
    export   只导出不生成，输入支持三种：
               - 剧集目录（按 系列名*串流 + 最新 mtime 找草稿）
               - 草稿名（com.lveditor.draft 下存在优先按草稿名）
               - --all-stream：草稿库中所有名字含「串流」的草稿全部导出

用法:
    python pipeline\\series_runner.py "D:\\短剧\\剧A" "D:\\短剧\\剧B"
    python pipeline\\series_runner.py A B C --mode generate
    python pipeline\\series_runner.py A B --mode export
    python pipeline\\series_runner.py --mode export --all-stream

状态写入 pipeline/_series_queue_state.json，重启自动跳过已完成、续跑半成品。
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, _SCRIPTS)

from utils.notifier import toast  # noqa: E402

STATE_PATH = os.path.join(_HERE, "_series_queue_state.json")
BATCH_RUNNER = os.path.join(_HERE, "batch_runner.py")
AUTO_EXPORTER = os.path.join(
    _ROOT, ".trae", "skills", "jianying-editor", "scripts", "auto_exporter.py"
)
JY_DRAFT_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "JianyingPro", "User Data", "Projects", "com.lveditor.draft"
)
DEFAULT_OUTPUT_DIR = r"D:\油管打猎\L"
# 各模式部间默认间隔（秒）：generate 不碰剪映零等待；export 留 60s 回首页；full 120s
_MODE_DEFAULT_RESET = {"full": 120, "generate": 0, "export": 60}


def _load_state():
    if os.path.isfile(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)[:24]


def _find_draft(series_name: str, since: float = 0) -> str | None:
    """定位串流草稿：名称匹配「系列名*串流」，取 mtime 最新；since=0 表示不限时间"""
    cands = [
        d for d in glob.glob(os.path.join(JY_DRAFT_ROOT, f"{series_name}*串流"))
        if os.path.isdir(d) and os.path.getmtime(d) >= since
    ]
    if not cands:
        return None
    return os.path.basename(max(cands, key=os.path.getmtime))


def _all_stream_drafts() -> list[str]:
    """草稿库中所有名字含「串流」的草稿，按 mtime 升序（先旧后新）"""
    cands = [
        d for d in glob.glob(os.path.join(JY_DRAFT_ROOT, "*串流*")) if os.path.isdir(d)
    ]
    return [os.path.basename(d) for d in sorted(cands, key=os.path.getmtime)]


def _output_exists(draft_name: str) -> bool:
    """成品已存在且 >1MB 视为已导出，跳过"""
    out = os.path.join(DEFAULT_OUTPUT_DIR, f"{draft_name}.mp4")
    return os.path.isfile(out) and os.path.getsize(out) > 1 * 1024 * 1024


def _run_pipeline(series_dir: str, slug: str) -> bool:
    cover = os.path.join(series_dir, "封面.png")
    cmd = [sys.executable, "-u", BATCH_RUNNER, series_dir,
           "--workdir", os.path.join("pipeline", f"_work_{slug}"), "--fresh"]
    if os.path.isfile(cover):
        cmd += ["--cover-image", cover]
    log_path = os.path.join(_HERE, f"_stream_{slug}.log")
    err_path = os.path.join(_HERE, f"_stream_{slug}.err.log")
    with open(log_path, "ab") as out, open(err_path, "ab") as err:
        proc = subprocess.run(cmd, cwd=_ROOT, stdout=out, stderr=err)
    return proc.returncode == 0


def _run_export(draft_name: str, timeout: float) -> bool:
    cmd = [sys.executable, "-u", AUTO_EXPORTER, draft_name, "--timeout", str(timeout)]
    proc = subprocess.run(cmd, cwd=_ROOT)
    return proc.returncode == 0


def _state_of(state, key):
    return state.setdefault(key, {"status": "pending"})


def main() -> int:
    ap = argparse.ArgumentParser(description="多剧集编排：串流生成 / 导出 / 联动，断点续跑")
    ap.add_argument("targets", nargs="*", help="剧集目录或草稿名")
    ap.add_argument("--mode", choices=["full", "generate", "export"], default="full")
    ap.add_argument("--all-stream", action="store_true",
                    help="export 模式：导出草稿库中所有含「串流」的草稿")
    ap.add_argument("--reset", type=float, default=None,
                    help="部间休息秒数（默认 full=120 / generate=0 / export=60）")
    ap.add_argument("--stage-gap", type=float, default=45,
                    help="full 模式中「生成→导出」的衔接秒数（默认 45）")
    ap.add_argument("--timeout", type=float, default=1800, help="导出超时秒数（默认 1800）")
    args = ap.parse_args()

    if args.mode == "export" and args.all_stream:
        targets = _all_stream_drafts()
        if not targets:
            print("✗ 草稿库中没有含「串流」的草稿")
            return 1
        print(f"📋 全量导出清单（{len(targets)} 部）: {'、'.join(targets)}")
    else:
        if not args.targets:
            ap.error("必须提供剧集目录/草稿名，或使用 --all-stream")
        targets = [t.rstrip("\\/") for t in args.targets]

    reset = args.reset if args.reset is not None else _MODE_DEFAULT_RESET[args.mode]
    state = _load_state()
    failed = []

    for i, target in enumerate(targets):
        is_draft = os.path.isdir(os.path.join(JY_DRAFT_ROOT, target))
        name = os.path.basename(target)
        st = _state_of(state, target)

        if st.get("status") == "done":
            print(f"⏭️  跳过已完成: {name}")
            continue

        # ---- 生成阶段（full/generate，且输入不是现成草稿名时）----
        if args.mode in ("full", "generate") and not is_draft:
            if not os.path.isdir(target):
                print(f"✗ 目录不存在: {target}")
                st["status"] = "failed"
                failed.append(name)
                continue
            draft = st.get("draft")
            if st.get("status") not in ("pipelined", "export_failed") or not draft:
                print(f"\n{'='*60}\n🎬 [{i+1}/{len(targets)}] 串流生成: {name}\n{'='*60}")
                toast("🎬 开始生成草稿", f"{name}（{i+1}/{len(targets)}）")
                started = time.time()
                st.update({"status": "pipelining", "started": started})
                _save_state(state)
                ok = _run_pipeline(target, _slug(name))
                draft = _find_draft(name, started) if ok else None
                if not draft:
                    st["status"] = "pipeline_failed"
                    _save_state(state)
                    toast("🎬 草稿生成失败", name)
                    failed.append(name)
                    continue
                st.update({"status": "pipelined", "draft": draft})
                _save_state(state)
            if args.mode == "generate":
                print(f"✅ 草稿就绪（不导出）: {st['draft']}")
                continue  # generate 模式零等待直接进入下一部
            time.sleep(args.stage_gap)  # full：生成→导出衔接缓冲
        elif args.mode in ("full", "generate") and is_draft:
            st.setdefault("draft", name)
            st["status"] = "pipelined"
            _save_state(state)
            if args.mode == "generate":
                print(f"⏭️  已是现成草稿: {name}")
                continue

        # ---- 导出阶段（full/export）----
        draft = st.get("draft") or (name if is_draft else _find_draft(name))
        if not draft:
            print(f"✗ 找不到对应串流草稿: {name}")
            st["status"] = "export_failed"
            _save_state(state)
            failed.append(name)
            continue
        st["draft"] = draft
        if _output_exists(draft):
            print(f"⏭️  成品已存在，跳过导出: {draft}")
            st["status"] = "done"
            _save_state(state)
            continue
        print(f"\n📤 [{i+1}/{len(targets)}] 导出: {draft}")
        st["status"] = "exporting"
        _save_state(state)
        if _run_export(draft, args.timeout):
            st["status"] = "done"
            _save_state(state)
            print(f"✅ 完成: {name}")
        else:
            st["status"] = "export_failed"
            _save_state(state)
            failed.append(name)

        if i < len(targets) - 1 and reset > 0:
            print(f"😴 休息 {reset:.0f}s 后继续下一部…")
            time.sleep(reset)

    ok = len(targets) - len(failed)
    summary = f"{args.mode} 模式成功 {ok}/{len(targets)}" + (f"，失败: {'、'.join(failed)}" if failed else "")
    toast("🏁 队列全部结束", summary)
    print(f"\n🏁 队列结束: {summary}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
