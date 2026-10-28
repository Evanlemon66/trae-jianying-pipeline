"""剧集批量流水线：整季视频 → 跑 pipeline（剪映云ASR + 翻译 + 人声分离 + 草稿）。

用法:
  python batch_runner.py "D:\\短剧\\10-六零糙汉：我靠打猎养全家"              # 整季（默认单草稿串流）
  python batch_runner.py <剧集目录> --per-episode                            # 逐集独立草稿（需多草稿并合并时才用）
  python batch_runner.py <剧集目录> --pattern "__jycache__\\*.mp4"           # 指定匹配
  python batch_runner.py <剧集目录> --only 3 --only 7                        # 只跑第3、7集
  python batch_runner.py <剧集目录> --only 20..30                            # 只跑第20到30集（区间）
  python batch_runner.py <剧集目录> --limit 2                                # 先跑2集试试

特性:
- 默认单草稿串流：所有集按文件名 a→z 注入同一草稿主轨，一条中文字幕轨 + 一条英文字幕轨 + 每集一条 vocals 音轨
- --per-episode 逐集独立草稿：状态机 pending → running → done / failed，记录在 <workdir>/_batch_state.json，
  中断后重跑自动跳过 done，失败集可单独 --only 重试，跑完可 --auto-merge 合并
- 串行 + 每集间隔(--delay)，降低剪映云接口风控概率；单集失败不阻塞后续集
"""

import os
import re
import sys
import json
import time
import glob
import argparse
import traceback

sys.stdout.reconfigure(encoding="utf-8")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_HERE)                    # pipeline/ → 项目根
_SCRIPTS_DIR = os.path.join(_PROJECT_ROOT, "scripts")     # scripts/utils/notifier.py 在这里
sys.path.insert(0, _HERE)
sys.path.insert(0, _SCRIPTS_DIR)

import pipeline as pl  # noqa: E402
from utils.notifier import toast as _toast  # noqa: E402
from export_config import export_mp4_path  # noqa: E402


def _ep_number(path, idx):
    """从文件名提取集数；提不出来就用序号。"""
    m = re.search(r"[第E]\s*(\d{1,3})\s*集", os.path.basename(path))
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,3})", os.path.basename(path))
    if m:
        return int(m.group(1))
    return idx + 1


def _natural_key(path):
    """文件名自然排序（第2集 < 第10集）。"""
    s = os.path.basename(path)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _parse_only(values):
    """把 --only 参数解析成集数集合。支持单个集数与区间（20..30 / 20-30），可混用。"""
    eps = set()
    for v in values or []:
        m = re.fullmatch(r"(\d{1,4})\s*(?:\.\.|-)\s*(\d{1,4})", str(v))
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            eps.update(range(min(a, b), max(a, b) + 1))
        else:
            eps.add(int(v))
    return eps


def load_state(workdir):
    p = os.path.join(workdir, "_batch_state.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state(workdir, state):
    p = os.path.join(workdir, "_batch_state.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="整季批量流水线")
    ap.add_argument("series_dir", help="剧集目录")
    ap.add_argument("--pattern", default=None,
                    help="视频 glob（相对剧集目录），默认自动探测 *.mp4/*.mkv")
    ap.add_argument("--draft-prefix", default=None, help="草稿名前缀，默认取目录名")
    ap.add_argument("--only", action="append",
                    help="只处理指定集数（可多次），支持区间如 20..30 或 20-30")
    ap.add_argument("--limit", type=int, default=None, help="本次最多处理几集")
    ap.add_argument("--cover-image", default=None,
                    help="草稿封面图片路径（传入则直接使用该图，不从视频抽帧）")
    ap.add_argument("--delay", type=float, default=5.0, help="每集之间间隔秒数")
    ap.add_argument("--retries", type=int, default=2, help="单集失败重试次数")
    ap.add_argument("--workdir", default=None,
                    help="工作目录（默认按剧名自动派生，避免多部剧互相覆盖音频素材）")
    ap.add_argument("--fresh", action="store_true", help="忽略状态从头跑")
    ap.add_argument("--clean-workdir", action="store_true",
                    help="批量结束后清理中间产物（默认不清理；只删未被草稿引用的文件）")
    ap.add_argument("--auto-merge", action="store_true",
                    help="跑完所有集后自动合并为一个串集草稿（等价于 merge_drafts.py --state）")
    ap.add_argument("--stream", dest="stream", action="store_true",
                    help="(默认) 单草稿串流模式：所有视频按文件名排序一次注入同一草稿（一条主轨）")
    ap.add_argument("--per-episode", dest="stream", action="store_false",
                    help="逐集独立草稿模式：每集一个草稿（可配合 --auto-merge 合并），仅在明确需要多草稿并合并时使用")
    ap.add_argument("--cut", type=float, default=None,
                    help="裁掉开头 N 秒之前的内容（透传 pipeline --cut，单位秒）")
    ap.add_argument("--no-export", dest="auto_export", action="store_false",
                    help="串流草稿生成后不自动导出（默认自动导出到默认目录）")
    ap.set_defaults(auto_export=True)
    ap.add_argument("--export-dir", default=None,
                    help="自动导出落点根目录（默认读 pipeline/presets.json 的 export_dir）")
    ap.add_argument("--wait-export", action="store_true",
                    help="后台导出启动后轮询 progress.json 等待完成（默认立即 return）")
    ap.add_argument("--verify", action="store_true",
                    help="导出完成后自动跑 verify_export.py（隐含 --wait-export）")
    ap.add_argument("--export-timeout", type=float, default=60.0,
                    help="导出等待上限分钟数（默认 60；超时视为异常，不阻塞继续）")
    ap.add_argument("--presets", default=None, metavar="'名1,名2:0.03'",
                    help="显式指定组合预设（透传 pipeline；不指定则走 presets.json/前缀自动发现）")
    ap.add_argument("--watch", dest="watch", action="store_true", default=True,
                    help="(默认) 启动 watcher 守护进程（后台监控状态变化，桌面通知，脚本崩溃也能告知）")
    ap.add_argument("--no-watch", dest="watch", action="store_false",
                    help="关闭 watcher 守护进程")
    ap.set_defaults(stream=True)
    args = ap.parse_args()

    series_dir = os.path.abspath(args.series_dir)
    if not os.path.isdir(series_dir):
        print(f"✗ 目录不存在: {series_dir}")
        sys.exit(1)

    # 未指定 workdir 时按剧名派生独立目录：demucs 产物按 demucs_epNN 同名写入，
    # 共用 _work 会让后一次运行覆盖前一部剧草稿引用的音频素材。
    if not args.workdir:
        import hashlib
        base = os.path.basename(series_dir)
        safe = re.sub(r'[<>:"/\\|?*\s]+', "", base)[:24] or "series"
        args.workdir = os.path.join(
            _HERE, f"_work_{safe}_{hashlib.md5(base.encode('utf-8')).hexdigest()[:8]}")
    os.makedirs(args.workdir, exist_ok=True)  # 状态文件在首集开跑前就要写入

    # 自动启动 watcher 守护进程（默认开启，不阻塞主流程；运行中会反查重启）
    _watcher_proc = None

    def _start_watcher():
        nonlocal _watcher_proc
        import subprocess as _sp
        watcher_path = os.path.join(_SCRIPTS_DIR, "utils", "watcher.py")
        _watcher_proc = _sp.Popen(
            [sys.executable, watcher_path, "--dir", args.workdir, "--pid", str(os.getpid())],
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        print(f"👁️  Watcher 已后台启动: {args.workdir}")

    def _ensure_watcher():
        """互监：watcher 死了就重启（watcher 盯主进程，主进程盯 watcher）。"""
        if not args.watch:
            return
        try:
            if _watcher_proc is None or _watcher_proc.poll() is not None:
                _start_watcher()
        except Exception as e:
            print(f"  ⚠️ Watcher 启动失败（不影响主流程）: {e}")

    if args.watch:
        _ensure_watcher()

    # 收集视频
    if args.pattern:
        videos = sorted(glob.glob(os.path.join(series_dir, args.pattern)))
    else:
        videos = []
        for pat in ("*.mp4", "*.mkv", "*.mov"):
            videos += glob.glob(os.path.join(series_dir, pat))
        if not videos:  # 探测 __jycache__ 子目录
            for pat in ("__jycache__/*.mp4", "**/*.mp4"):
                videos += sorted(glob.glob(os.path.join(series_dir, pat), recursive=True))
                if videos:
                    break
    videos = sorted(set(videos))
    if not videos:
        print(f"✗ 目录里没找到视频: {series_dir}")
        sys.exit(1)

    # 封面自动探测（默认行为）：封面.png/jpg → 目录下任意图片兜底 → 都没有则抽帧
    if not args.cover_image:
        for name in ("封面.png", "封面.jpg", "封面.jpeg", "封面.webp"):
            p = os.path.join(series_dir, name)
            if os.path.isfile(p):
                args.cover_image = p
                break
        else:
            imgs = sorted(
                f for f in os.listdir(series_dir)
                if os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
                and os.path.isfile(os.path.join(series_dir, f))
            )
            if imgs:
                args.cover_image = os.path.join(series_dir, imgs[0])
        if args.cover_image:
            print(f"🖼️ 自动封面: {os.path.basename(args.cover_image)}")

    prefix = args.draft_prefix or os.path.basename(series_dir.rstrip("\\/"))
    state = {} if args.fresh else load_state(args.workdir)
    only_eps = _parse_only(args.only)

    # ── 单草稿串流模式：按文件名 a→z 全部注入同一草稿主轨，一次跑完 ──
    if args.stream:
        sel = []
        for i, v in enumerate(videos):
            ep = _ep_number(v, i)
            if only_eps and ep not in only_eps:
                continue
            sel.append((ep, v))
        sel.sort(key=lambda x: _natural_key(x[1]))
        if args.limit:
            sel = sel[:args.limit]
        if not sel:
            print("✗ 没有可处理的视频")
            sys.exit(1)

        eps = [ep for ep, _ in sel]
        if len(eps) > 1:
            draft_name = f"{prefix}_第{eps[0]:02d}-{eps[-1]:02d}集串流"
        else:
            draft_name = f"{prefix}_第{eps[0]:02d}集"

        print(f"\n🎬 单草稿串流: {len(sel)} 集 → {draft_name}")
        for ep, v in sel:
            print(f"   第{ep:02d}集  {os.path.basename(v)}")
        print()

        cover_kw = ({"cover_image": args.cover_image} if args.cover_image
                    else {"cover_extract_from_video": sel[0][1]})
        _ensure_watcher()  # 串流主流程开跑前反查 watcher
        pl.run_pipeline(
            draft_name_or_path=draft_name,
            video_paths=[v for _, v in sel],
            cut_us=int(args.cut * 1_000_000) if args.cut is not None else None,
            do_export=False,
            skip_local_ai=False,
            workdir=args.workdir,
            presets=pl.parse_presets_arg(args.presets) if args.presets else None,
            **cover_kw,
        )

        for ep, v in sel:
            state.setdefault(v, {})
            state[v].update({
                "status": "done",
                "ep": ep,
                "draft": draft_name,
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        save_state(args.workdir, state)
        print(f"\n🏁 串流草稿完成: {draft_name}")

        # ---- 帧对齐修复（必须在剪映关着的时候改磁盘草稿；pipeline 内部已关剪映）----
        try:
            import _framefix_draft as _ff
            ff_result = _ff.fix_draft(draft_name, run_tests=True)
            if ff_result["test_passed"] is False:
                print(f"  ⚠️ 帧对齐字幕测试失败（不阻塞导出）: {ff_result['test_details']}")
            print(f"  📐 帧对齐: {ff_result['ep_count']}集, 总长 {ff_result['old_total']:.1f}s → {ff_result['new_total']:.1f}s")
        except Exception as e:
            print(f"  ⚠️ 帧对齐跳过（不阻塞导出）: {e}")
            ff_result = None

        _toast("🎬 串流草稿已就绪", f"{draft_name}（{len(sel)} 集）")

        # ---- 导出（后台 WMI + pythonw + CREATE_NO_WINDOW，零弹窗、不阻塞）----
        if args.auto_export:
            import subprocess as _sp
            import tempfile as _tf
            exporter = os.path.join(
                _PROJECT_ROOT, ".trae", "skills", "jianying-editor",
                "examples", "robust_auto_export.py")
            # 成品按「一部剧一个文件夹」归置：<export_dir>/<草稿名>/<草稿名>.mp4
            out_mp4 = export_mp4_path(draft_name, args.export_dir)
            export_root = os.path.dirname(os.path.dirname(out_mp4))
            os.makedirs(os.path.dirname(out_mp4), exist_ok=True)
            print(f"\n📤 后台导出 → {out_mp4}")

            # 构造临时 pythonw 启动器（写一次，复用）
            launcher_dir = os.path.join(_tf.gettempdir(), "jy_logs")
            os.makedirs(launcher_dir, exist_ok=True)
            launcher_path = os.path.join(launcher_dir, f"_launch_export_{os.getpid()}.py")
            log_out = os.path.join(launcher_dir, f"export_{os.getpid()}.out.log")
            log_err = os.path.join(launcher_dir, f"export_{os.getpid()}.err.log")
            with open(launcher_path, "w", encoding="utf-8") as lf:
                lf.write(f'''"""WMI 启动器：后台导出 {draft_name}，零弹窗随即退出。"""
import os, subprocess, sys
LOG_DIR = r"{launcher_dir}"
PY = r"{sys.executable}"
os.makedirs(LOG_DIR, exist_ok=True)
env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
out = open(r"{log_out}", "w", encoding="utf-8", errors="replace")
err = open(r"{log_err}", "w", encoding="utf-8", errors="replace")
subprocess.Popen(
    [PY, "-u", r"{exporter}", r"{draft_name}", r"{out_mp4}", "--move",
     "--fallback-dir", r"{export_root}"],
    cwd=r"{_PROJECT_ROOT}", stdout=out, stderr=err, env=env,
    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    close_fds=True,
)
''')
            # WMI 方式启动（父进程 WmiPrvSE，脱离 job object；用 PowerShell Invoke-CimMethod 不依赖 pywin32）
            ps_cmd = (
                f'Invoke-CimMethod -ClassName Win32_Process -MethodName Create '
                f'-Arguments @{{ CommandLine = \'"{sys.executable.replace("python.exe", "pythonw.exe")}" "{launcher_path}"\' }} '
                f'| Out-Null'
            )
            try:
                rc = _sp.run(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    capture_output=True, timeout=30)
                if rc.returncode == 0:
                    print(f"  WMI 后台启动完成（pythonw，零弹窗）")
                else:
                    raise RuntimeError(f"CIM rc={rc.returncode}")
            except Exception:
                _sp.Popen(
                    [sys.executable, "-u", launcher_path],
                    creationflags=_sp.CREATE_NO_WINDOW | _sp.CREATE_NEW_PROCESS_GROUP,
                    close_fds=True,
                )
                print(f"  回退启动完成（CREATE_NO_WINDOW）")
            print(f"  进度文件: {out_mp4}.progress.json")
            print(f"  日志: {log_out}")
            _toast("📤 后台导出中", draft_name)

            # ---- 等待导出 + 自动验证 ----
            wait = args.wait_export or args.verify
            if wait:
                import time as _time, json as _json
                pjson = out_mp4 + ".progress.json"
                deadline = _time.time() + args.export_timeout * 60
                print(f"\n  ⏳ 等待导出完成（上限 {args.export_timeout}min，每 25s 轮询一次）...")
                last_pct = -1
                while _time.time() < deadline:
                    if os.path.exists(pjson):
                        try:
                            with open(pjson, encoding="utf-8") as f:
                                rec = _json.load(f)
                            cur_pct = rec.get("percent", "")
                            if cur_pct and cur_pct != last_pct:
                                print(f"    {rec.get('updated_at','')}  {cur_pct}  "
                                      f"{rec.get('exported_mb',0):.0f}MB  已跑 {rec.get('elapsed_s',0)/60:.1f}min")
                                last_pct = cur_pct
                            if rec.get("done"):
                                print(f"    ✅ 导出完成（耗时 {rec.get('elapsed_s',0)/60:.1f}min，"
                                      f"最终 {rec.get('final_mb',0):.0f}MB）")
                                break
                        except Exception:
                            pass
                    _time.sleep(25)
                else:
                    print(f"    ⚠️ 导出超时（>{args.export_timeout}min），不阻塞继续")
                    _toast("📤 导出超时", draft_name)

                if args.verify:
                    print(f"\n  🔍 自动验证帧级漂移 + 字幕帧位...")
                    verify_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "verify_export.py")
                    if os.path.exists(verify_script):
                        rc = _sp.call([sys.executable, verify_script, draft_name, out_mp4],
                                      cwd=os.path.dirname(verify_script))
                        if rc == 0:
                            _toast("✅ 验证全部通过", draft_name)
                        else:
                            _toast("❌ 验证未通过", draft_name)
                    else:
                        print(f"    verify_export.py 不存在，跳过")

        if args.clean_workdir:
            pl.clean_workdir(args.workdir)
        return

    todo = []
    for i, v in enumerate(videos):
        ep = _ep_number(v, i)
        if only_eps and ep not in only_eps:
            continue
        st = state.get(v, {}).get("status")
        if st == "done":
            print(f"⏭️  第{ep}集 已完成, 跳过")
            continue
        todo.append((ep, v))

    if args.limit:
        todo = todo[:args.limit]

    print(f"\n🎬 批量流水线: {os.path.basename(series_dir)}")
    print(f"   共 {len(videos)} 个视频, 待处理 {len(todo)} 集, 草稿前缀: {prefix}\n")

    ok, fail = 0, 0
    t0 = time.time()
    for n, (ep, video) in enumerate(todo, 1):
        _ensure_watcher()  # 每集开跑前反查 watcher
        draft_name = f"{prefix}_第{ep:02d}集"
        print(f"\n{'━'*60}")
        print(f"📺 [{n}/{len(todo)}] 第{ep}集  {os.path.basename(video)}")
        print(f"🎬 草稿: {draft_name}")
        print(f"{'━'*60}")

        state.setdefault(video, {})
        state[video]["status"] = "running"
        state[video]["ep"] = ep
        save_state(args.workdir, state)

        # 每集独立 workdir：单集调用时 pipeline 内部集序号恒为 1，
        # 共用目录会让所有逐集草稿都指向 demucs_ep01，互相覆盖。
        wd_ep = os.path.join(args.workdir, f"ep{ep:02d}")
        os.makedirs(wd_ep, exist_ok=True)

        done = False
        for attempt in range(1, args.retries + 2):
            try:
                pl.run_pipeline(
                    draft_name_or_path=draft_name,
                    video_paths=[video],
                    cut_us=int(args.cut * 1_000_000) if args.cut is not None else None,
                    do_export=False,
                    skip_local_ai=False,
                    cover_image=args.cover_image or None,
                    cover_extract_from_video=None if args.cover_image else video,
                    workdir=wd_ep,
                    presets=pl.parse_presets_arg(args.presets) if args.presets else None,
                )
                done = True
                break
            except Exception:
                print(f"⚠️ 第{ep}集 第{attempt}次尝试失败:")
                traceback.print_exc()
                time.sleep(3)

        if done:
            state[video]["status"] = "done"
            state[video]["draft"] = draft_name
            ok += 1
        else:
            state[video]["status"] = "failed"
            fail += 1
        state[video]["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(args.workdir, state)

        if n < len(todo):
            time.sleep(args.delay)

    mins = (time.time() - t0) / 60
    print(f"\n{'='*60}")
    print(f"🏁 批量完成: 成功 {ok}, 失败 {fail}, 耗时 {mins:.1f} 分钟")
    if fail:
        print("   失败集可重跑: python batch_runner.py <目录> --only <集数>")
    print(f"{'='*60}")
    _toast("🎬 批量制作完成", f"成功 {ok} 集, 失败 {fail} 集, 耗时 {mins:.0f} 分钟")

    if args.auto_merge and ok > 0:
        try:
            import subprocess as _sp
            merge_out = f"{prefix}_全季合并"
            state_path = os.path.join(args.workdir, "_batch_state.json")
            print(f"\n🧩 自动合并 → {merge_out}")
            _sp.check_call(
                [sys.executable, os.path.join(_HERE, "merge_drafts.py"),
                 "--state", state_path, "--out", merge_out],
                cwd=_HERE,
            )
        except Exception as e:
            print(f"  ✗ 合并失败: {e}")

    if args.clean_workdir:
        print()
        pl.clean_workdir(args.workdir)
        for d in sorted(glob.glob(os.path.join(args.workdir, "ep*"))):
            if os.path.isdir(d):
                pl.clean_workdir(d)


if __name__ == "__main__":
    main()
