import argparse
import json
import os
import shutil
import subprocess
import threading
import time

from utils.cli_protocol import emit_result, make_result
from utils.env_setup import setup_env
from utils.errors import UserInputError
from utils.logging_utils import setup_logger

setup_env()
logger = setup_logger("auto_exporter")
from utils.notifier import toast as _toast  # noqa: E402

import pyJianYingDraft as draft  # noqa: E402

# 剪映默认草稿根目录（用于自动读取 draft_content.json 的预期时长）
JY_DRAFT_ROOT = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "JianyingPro", "User Data", "Projects", "com.lveditor.draft"
)
# ffprobe 候选：PATH 优先，回退到项目统一依赖目录
_FFPROBE_CANDIDATES = ("ffprobe", r"D:\AI_Deps\tools\ffmpeg\bin\ffprobe.exe")
# 默认成品输出目录（未显式指定 output / --output-dir 时使用）
DEFAULT_OUTPUT_DIR = r"D:\油管打猎\L"
# 剪映 5.9 启动程序（UI 自动化必须用此版本，可用环境变量 JIANYING_EXE 覆盖）
JIANYING_EXE = os.environ.get(
    "JIANYING_EXE",
    r"D:\剪映5.9\剪映5.9版+jianying-skill\剪映5.9(win+mac)\剪映5.9Windows\JianyingPro\5.9.0.11632\JianyingPro.exe",
)
# 可通过「重启剪映」补救的 UI 类失败关键词
_UI_RETRY_KEYWORDS = ("剪映窗口未找到", "导出按钮", "编辑窗口", "草稿", "未找到", "卡死", "超时")
# 看门狗：导出文件多少秒无增长判定为卡死
_STALL_SECONDS = float(os.environ.get("EXPORT_STALL_SECONDS", "180"))
# 导出码率经验值（字节/秒）：实测 7.79GB / 7638s ≈ 1.045MB/s，预留 20% 余量做磁盘预检
_EST_BYTES_PER_SEC = 1.05 * 1024 * 1024
# 进度通知间隔（秒）
_PROGRESS_INTERVAL = 300


def _check_disk_space(output_path: str, expected_duration: float | None) -> None:
    """磁盘余量预检：目标盘剩余空间不足预估成品的 1.2 倍时直接拒绝启动（不占用重试）"""
    if not expected_duration or expected_duration <= 0:
        return
    est_bytes = expected_duration * _EST_BYTES_PER_SEC * 1.2
    drive = os.path.splitdrive(os.path.abspath(output_path))[0] + "\\"
    free = shutil.disk_usage(drive).free
    if free < est_bytes:
        raise UserInputError(
            f"磁盘空间不足: {drive} 剩余 {free / 2**30:.1f}GB，预估需 {est_bytes / 2**30:.1f}GB"
        )
    logger.info("磁盘预检通过: %s 剩余 %.1fGB，预估需 %.1fGB", drive, free / 2**30, est_bytes / 2**30)


def _screenshot(tag: str) -> None:
    """UI 失败时截屏存证（TEMP/jy_export_shots），便于事后定位剪映卡在哪个界面"""
    try:
        shot_dir = os.path.join(os.environ.get("TEMP", "."), "jy_export_shots")
        os.makedirs(shot_dir, exist_ok=True)
        path = os.path.join(shot_dir, f"{time.strftime('%Y%m%d_%H%M%S')}_{tag}.png")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=New-Object System.Drawing.Bitmap([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width,"
            "[System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height);"
            "$g=[System.Drawing.Graphics]::FromImage($b);"
            "$g.CopyFromScreen(0,0,0,0,$b.Size);"
            f"$b.Save('{path}');$g.Dispose();$b.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        logger.info("失败快照已保存: %s", path)
    except Exception as e:  # noqa: BLE001
        logger.warning("失败快照保存失败: %s", e)


def _find_jianying_window():
    """返回剪映主窗口控件（首页或编辑窗），找不到返回 None"""
    try:
        import uiautomation as uia

        def _cmp(control, _depth):
            if control.Name != "剪映专业版":
                return False
            cls = (control.ClassName or "").lower()
            return "homepage" in cls or "mainwindow" in cls

        win = uia.WindowControl(searchDepth=1, Compare=_cmp)
        return win if win.Exists(0) else None
    except Exception:
        return None


def _jianying_window_exists() -> bool:
    return _find_jianying_window() is not None


def _wait_home_ready(max_wait: float = 30) -> bool:
    """就绪探针：首页需等草稿列表出现 ListItem 才可交互；编辑窗只需短暂稳定"""
    import uiautomation as uia  # noqa: F401

    deadline = time.time() + max_wait
    while time.time() < deadline:
        win = _find_jianying_window()
        if win is None:
            return False
        cls = (win.ClassName or "").lower()
        if "mainwindow" in cls:  # 编辑窗口没有草稿列表，稳定 3s 即可
            time.sleep(3)
            return True
        try:
            if win.ListItemControl(searchDepth=8).Exists(1):
                logger.info("首页草稿列表已就绪")
                return True
        except Exception:
            pass
        time.sleep(2)
    logger.warning("等待草稿列表就绪超时（%ss），继续尝试", max_wait)
    return True  # 超时不硬失败，交给导出流程自己报错进重试


def _bring_foreground() -> None:
    """把剪映窗口置前，排除遮挡/焦点干扰（UIA 个别点击依赖焦点）"""
    try:
        win = _find_jianying_window()
        if win is not None:
            win.SetActive()
            time.sleep(0.5)
    except Exception as e:  # noqa: BLE001
        logger.debug("置前失败（可忽略）: %s", e)


def _ensure_jianying(max_wait: float = 150) -> bool:
    """确保剪映 5.9 已启动、主窗口出现且首页草稿列表就绪；未运行则自动拉起"""
    if _jianying_window_exists():
        _bring_foreground()
        return _wait_home_ready()
    if os.path.exists(JIANYING_EXE):
        logger.info("剪映窗口未就绪，自动启动: %s", JIANYING_EXE)
        subprocess.Popen([JIANYING_EXE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(5)
        if _jianying_window_exists():
            logger.info("剪映主窗口已出现，等待草稿列表就绪…")
            _bring_foreground()
            return _wait_home_ready()
    logger.error("等待剪映窗口超时（%ss）", max_wait)
    return False


def _kill_jianying() -> None:
    """杀掉 5.9 版剪映进程树：按可执行路径过滤主进程，附带清理其辅助子进程"""
    ps = (
        "$killed = @();"
        "Get-CimInstance Win32_Process | Where-Object {"
        "  $_.ExecutablePath -like '*5.9.0.11632*' -or"
        "  (($_.Name -match '^(VEHelper|VEDetector|vebun|parfait)') -and"
        "   ($_.ExecutablePath -like '*JianyingPro*5.9*' -or $_.ExecutablePath -like '*5.9.0.11632*'))"
        "} | ForEach-Object { $killed += \"$($_.Name)#$($_.ProcessId)\";"
        "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue };"
        "if ($killed) { Write-Output ($killed -join ',') }"
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=30,
    )
    killed = (r.stdout or "").strip()
    logger.info("已清理剪映进程: %s", killed or "（无残留）")


def _restart_jianying(max_wait: float = 150) -> bool:
    """硬重启：杀掉残留剪映进程（含半成品弹窗状态），重新拉起到就绪首页"""
    logger.info("硬重启剪映以清理残留状态…")
    _kill_jianying()
    time.sleep(5)
    return _ensure_jianying(max_wait)


def _soft_recover_jianying() -> bool:
    """软恢复（不杀进程）：Esc 关残留弹窗 → 回首页 → 置前 → 等草稿列表就绪"""
    try:
        import uiautomation as uia

        win = _find_jianying_window()
        if win is None:
            logger.info("软恢复：窗口不存在，转硬重启")
            return _restart_jianying()
        win.SetActive()
        time.sleep(0.5)
        for _ in range(2):  # 连按 Esc 关闭可能的导出弹窗/对话框
            uia.SendKeys("{Esc}")
            time.sleep(0.8)
        # 首页按钮回首页（在编辑窗时有效；已在首页则无害）
        try:
            home = win.ButtonControl(searchDepth=6, Name="首页")
            if home.Exists(1):
                home.Click()
                time.sleep(2)
        except Exception:
            pass
        return _wait_home_ready()
    except Exception as e:  # noqa: BLE001
        logger.warning("软恢复异常（%s），转硬重启", e)
        return _restart_jianying()


def _export_size(output_path: str) -> int:
    """估算导出产物当前体积：output_path 为文件取自身大小，为同名文件夹取内部合计，不存在则 0"""
    try:
        if os.path.isfile(output_path):
            return os.path.getsize(output_path)
        if os.path.isdir(output_path):
            total = 0
            for root, _dirs, files in os.walk(output_path):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            return total
    except OSError:
        pass
    return 0


def _run_export_with_watchdog(ctrl, draft_name, output_path, target_res, target_fr, timeout,
                              expected_duration=None):
    """在子线程执行导出，主线程看门狗监控产物增长；卡死则杀剪映并抛错（可触发重试）"""
    result = {}

    def _run():
        try:
            ctrl.export_draft(
                draft_name, output_path, resolution=target_res, framerate=target_fr, timeout=timeout
            )
        except Exception as e:  # noqa: BLE001
            result["err"] = e

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.time() + timeout + 120  # 外层兜底宽限 120s，正常超时由 export_draft 内部抛出
    last_size, last_growth = -1, time.time()
    est_bytes = (expected_duration or 0) * _EST_BYTES_PER_SEC
    next_progress = time.time() + _PROGRESS_INTERVAL
    while t.is_alive():
        t.join(timeout=10)
        if not t.is_alive():
            break
        size = _export_size(output_path)
        if size != last_size:
            last_size, last_growth = size, time.time()
        elif last_size > 0 and time.time() - last_growth > _STALL_SECONDS:
            logger.error("看门狗判定导出卡死：%ss 内产物体积无增长（%d bytes）", _STALL_SECONDS, last_size)
            _kill_jianying()
            raise RuntimeError(f"导出卡死：{_STALL_SECONDS:.0f}s 无进度")
        if time.time() > deadline:
            logger.error("看门狗外层超时（%ss），终止剪映", timeout + 120)
            _kill_jianying()
            raise RuntimeError(f"导出卡死：超过 {timeout + 120:.0f}s 未完成")
        if time.time() >= next_progress:
            next_progress = time.time() + _PROGRESS_INTERVAL
            if last_size > 0:
                pct = f"，约 {min(99, last_size / est_bytes * 100):.0f}%" if est_bytes else ""
                _toast("📤 导出进行中", f"{draft_name}：已产出 {last_size / 2**30:.2f}GB{pct}")
    if "err" in result:
        raise result["err"]


def resolve_output(draft_name: str, output: str | None, output_dir: str | None) -> str:
    """解析成品输出路径：显式 output > --output-dir > 默认目录；文件名缺省为 <草稿名>.mp4。"""
    if output:
        if os.path.isdir(output) or output.endswith(("\\", "/")):
            return os.path.join(output.rstrip("\\/"), f"{draft_name}.mp4")
        return output
    return os.path.join(output_dir or DEFAULT_OUTPUT_DIR, f"{draft_name}.mp4")


def _find_ffprobe() -> str | None:
    for cand in _FFPROBE_CANDIDATES:
        found = shutil.which(cand)
        if found:
            return found
    return None


def _probe_duration(ffprobe: str, path: str) -> float | None:
    """用 ffprobe 读取成品时长（秒），失败返回 None。"""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60,
        )
        return float(r.stdout.strip().splitlines()[0])
    except Exception as e:
        logger.warning("ffprobe 读取时长失败: %s", e)
        return None


def _draft_expected_duration(draft_name: str) -> float | None:
    """从草稿目录 draft_content.json 顶层 duration（微秒）读取预期时长（秒）。"""
    path = os.path.join(JY_DRAFT_ROOT, draft_name, "draft_content.json")
    try:
        with open(path, encoding="utf-8") as f:
            duration_us = json.load(f).get("duration")
        return float(duration_us) / 1e6 if duration_us else None
    except Exception as e:
        logger.warning("读取草稿预期时长失败（%s）: %s", path, e)
        return None


def verify_export(output_path: str, expected_s: float | None) -> tuple[bool, dict]:
    """导出成品加固校验：文件可读 + 视频流存在 + 时长比对 + 体积阈值。

    返回 (是否通过, 校验详情)。ffprobe 不可用时降级为仅文件大小检查并在详情中标注。
    """
    info: dict = {"output": output_path, "checks": {}}
    if not os.path.isfile(output_path):
        info["reason"] = "成品文件不存在"
        return False, info

    size = os.path.getsize(output_path)
    info["checks"]["size_mb"] = round(size / (1 << 20), 2)
    if size <= (1 << 20):
        info["reason"] = f"文件过小（{size} 字节），疑似导出不完整"
        return False, info

    ffprobe = _find_ffprobe()
    if not ffprobe:
        logger.warning("未找到 ffprobe，跳过时长/视频流校验（仅完成文件大小检查）")
        info["checks"]["ffprobe"] = "skipped"
        return True, info

    # 视频流存在性
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_path],
            capture_output=True, text=True, timeout=60,
        )
        has_video = bool(r.stdout.strip())
        info["checks"]["video_codec"] = r.stdout.strip() or "none"
    except Exception as e:
        logger.warning("ffprobe 视频流检查失败: %s", e)
        has_video = False
    if not has_video:
        info["reason"] = "未检测到视频流，成品损坏"
        return False, info

    # 时长比对（误差容忍：max(5s, 1%)）
    duration = _probe_duration(ffprobe, output_path)
    info["checks"]["duration_s"] = round(duration, 2) if duration else None
    if duration is None:
        info["reason"] = "ffprobe 读取时长失败"
        return False, info
    if expected_s:
        info["checks"]["expected_s"] = round(expected_s, 2)
        tol = max(5.0, expected_s * 0.01)
        info["checks"]["tolerance_s"] = round(tol, 2)
        if abs(duration - expected_s) > tol:
            info["reason"] = (
                f"时长不符：成品 {duration:.1f}s vs 预期 {expected_s:.1f}s（容忍 {tol:.1f}s）"
            )
            return False, info

    return True, info


def _salvage_existing_output(output_path: str, expected_s: float | None) -> bool:
    """导出报错后的成品抢救：剪映可能在报错前已写出成品（含同名文件夹/(1)后缀形态），
    找到且四层校验通过则归位为最终文件并视为成功。"""
    out_dir = os.path.dirname(os.path.abspath(output_path))
    stem = os.path.splitext(os.path.basename(output_path))[0]
    candidates = []
    try:
        for entry in os.listdir(out_dir):
            if not entry.startswith(stem):
                continue
            full = os.path.join(out_dir, entry)
            if os.path.isfile(full) and entry.lower().endswith(".mp4"):
                candidates.append(full)
            elif os.path.isdir(full):
                for f in os.listdir(full):
                    if f.lower().endswith(".mp4"):
                        candidates.append(os.path.join(full, f))
    except OSError:
        return False
    candidates.sort(key=os.path.getmtime, reverse=True)
    for cand in candidates:
        ok, info = verify_export(cand, expected_s)
        if not ok:
            continue
        logger.info("抢救到已完成成品: %s（校验通过）", cand)
        shutil.move(cand, output_path)
        cover_src = os.path.join(os.path.dirname(cand),
                                 os.path.splitext(os.path.basename(cand))[0] + "-封面.jpg")
        if os.path.isfile(cover_src):
            shutil.move(cover_src, os.path.join(out_dir, stem + "-封面.jpg"))
        leftover = os.path.dirname(cand)
        if leftover != out_dir and os.path.isdir(leftover) and not os.listdir(leftover):
            os.rmdir(leftover)
        _ = info
        return True
    return False


def auto_export(
    draft_name: str,
    output_path: str,
    resolution: str = None,
    framerate: str = None,
    timeout: float = 1800,
    expected_duration: float = None,
) -> tuple[int, dict]:
    res_map = {
        "480": draft.ExportResolution.RES_480P,
        "720": draft.ExportResolution.RES_720P,
        "1080": draft.ExportResolution.RES_1080P,
        "2K": draft.ExportResolution.RES_2K,
        "4K": draft.ExportResolution.RES_4K,
        "8K": draft.ExportResolution.RES_8K,
    }
    fr_map = {
        "24": draft.ExportFramerate.FR_24,
        "25": draft.ExportFramerate.FR_25,
        "30": draft.ExportFramerate.FR_30,
        "50": draft.ExportFramerate.FR_50,
        "60": draft.ExportFramerate.FR_60,
    }

    target_res = res_map.get(str(resolution).upper() if resolution else "")
    target_fr = fr_map.get(str(framerate) if framerate else "")

    if resolution and target_res is None:
        raise UserInputError(
            f"Unsupported resolution: {resolution} (allowed: {', '.join(res_map.keys())})"
        )
    if framerate and target_fr is None:
        raise UserInputError(
            f"Unsupported framerate: {framerate} (allowed: {', '.join(fr_map.keys())})"
        )

    try:
        output_dir = os.path.dirname(os.path.abspath(output_path))
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        logger.info("Preparing export: draft=%s", draft_name)
        est_duration = expected_duration or _draft_expected_duration(draft_name)
        _check_disk_space(output_path, est_duration)
        ctrl = draft.JianyingController()
        _run_export_with_watchdog(ctrl, draft_name, output_path, target_res, target_fr, timeout,
                                  expected_duration=est_duration)
        logger.info("Export succeeded: %s", output_path)

        # 成品加固校验：预期时长优先用 CLI 显式值，否则自动读草稿 draft_content.json
        expected_s = est_duration
        ok, verify_info = verify_export(output_path, expected_s)
        payload = {
            "draft": draft_name,
            "output": output_path,
            "resolution": resolution,
            "fps": framerate,
            "verify": verify_info,
        }
        if not ok:
            logger.error("成品校验未通过: %s", verify_info.get("reason"))
            return 3, make_result(False, "verify_failed", verify_info.get("reason", ""), payload)

        return 0, make_result(True, "ok", "", payload)
    except Exception as e:
        logger.error("Export failed: %s", e)
        # 抢救：报错前成品可能已写出（如导出完成后 get_window 才失败），校验通过则视为成功
        est = expected_duration or _draft_expected_duration(draft_name)
        if _salvage_existing_output(output_path, est):
            logger.info("成品已抢救归位: %s", output_path)
            return 0, make_result(
                True, "ok_salvaged", str(e), {"draft": draft_name, "output": output_path}
            )
        logger.error("Hint: restart JianYing and keep it on Home/Edit page before retry.")
        return 1, make_result(
            False,
            "export_failed",
            str(e),
            {"draft": draft_name, "output": output_path},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless draft exporter")
    parser.add_argument(
        "name", nargs="?", default=None, help="Draft name"
    )
    parser.add_argument(
        "output", nargs="?", default=None,
        help="Output mp4 path or directory (default: %s\\<draft>.mp4)" % DEFAULT_OUTPUT_DIR,
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory only; filename auto-set to <draft>.mp4 (overrides default)",
    )
    parser.add_argument("--res", help="Resolution: 480/720/1080/2K/4K/8K")
    parser.add_argument("--fps", help="Framerate: 24/25/30/50/60")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help="Export timeout in seconds (default: 1800; override for very long drafts)",
    )
    parser.add_argument(
        "--expect-duration",
        type=float,
        default=None,
        help="Expected output duration in seconds; auto-read from draft_content.json if omitted",
    )
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="UI-failure retries after auto-restarting JianYing (default: 2, i.e. 3 attempts total)",
    )
    args = parser.parse_args()
    try:
        if not args.name:
            raise UserInputError("必须提供草稿名")
        draft_json = os.path.join(JY_DRAFT_ROOT, args.name, "draft_content.json")
        if not os.path.isfile(draft_json):
            raise UserInputError(f"草稿不存在（请核对名称）: {args.name}")
        output_path = resolve_output(args.name, args.output, args.output_dir)
        logger.info("输出路径: %s", output_path)

        _ensure_jianying()  # 预检：剪映未运行则先拉起，避免首轮即失败

        attempts = 1 + max(0, args.retries)
        code, summary = 1, {}
        for attempt in range(1, attempts + 1):
            code, summary = auto_export(
                args.name, output_path, args.res, args.fps, args.timeout, args.expect_duration
            )
            reason = str(summary.get("reason", ""))
            ui_fail = code == 1 and any(k in reason for k in _UI_RETRY_KEYWORDS)
            if code == 0 or not ui_fail or attempt >= attempts:
                break
            _screenshot(f"{args.name}_attempt{attempt}")
            if attempt == 1:
                # 第一级：软恢复，不杀进程（窗口大概率是好的，只是弹窗/列表未就绪）
                logger.warning("导出 UI 失败（%s），软恢复后重试（%d/%d）", reason, attempt, attempts - 1)
                _toast("📤 导出失败，软恢复重试", f"{args.name}: {reason}")
                if not _soft_recover_jianying():
                    break
            else:
                # 第二级：硬重启，清残局后重来
                logger.warning("导出 UI 失败（%s），硬重启剪映后重试（%d/%d）", reason, attempt, attempts - 1)
                _toast("📤 导出失败，重启剪映重试", f"{args.name}: {reason}")
                if not _restart_jianying():
                    break
            time.sleep(5)
            _bring_foreground()
        if code == 0:
            _toast("📤 导出成功", str(output_path))
        else:
            _toast("📤 导出失败", f"退出码 {code}: {summary.get('reason', '')}")
    except UserInputError as e:
        logger.error(str(e))
        summary = make_result(
            False, "invalid_input", str(e), {"draft": args.name, "output": args.output}
        )
        code = 2
        _toast("📤 导出失败", str(e))
    except Exception as e:
        logger.error("未预期的异常: %s", e)
        summary = make_result(
            False, "unexpected_error", str(e), {"draft": args.name, "output": args.output}
        )
        code = 4
        _toast("📤 导出失败", str(e))
    emit_result(summary, args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
