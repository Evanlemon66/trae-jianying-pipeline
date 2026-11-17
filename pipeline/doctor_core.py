# -*- coding: utf-8 -*-
"""环境自检的共享核心（只读）。

同一套检查函数供两个薄入口复用：
  - pipeline/doctor.py        命令行文本报告（退出码 0/1）
  - pipeline_ui/console/doctor.py  Console 页面（ok/warn/fail + JSON）

运行时另外补充 Console 专属的目录可写探针、磁盘、串流清单和监听地址，
那些不属于本文件；本文件只放两边都必须一致的检查。

只做读取与探测：不启动/关闭剪映、不启停 watcher、不触发导出、不写任何文件。
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_LIB_DIR = os.path.join(_REPO_ROOT, "lib")
for _p in (_HERE, _LIB_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import export_config as ec  # noqa: E402

MIN_PYTHON = (3, 10)
RECOMMENDED_PYTHON = (3, 12)

# 运行主链路必需（缺任一都建议先补齐再跑 pipeline）
CORE_MODULES = {
    "uiautomation": "uiautomation",
    "pynput": "pynput",
    "pymediainfo": "pymediainfo",
    "cv2": "opencv-python",
    "numpy": "numpy",
    "PIL": "pillow",
    "imageio": "imageio",
    "psutil": "psutil",
    "requests": "requests",
    "websockets": "websockets",
}
# AI/增强链路（缺了只影响对应功能，不阻塞基础剪辑/导出）
AI_MODULES = {
    "demucs": "demucs",
    "faster_whisper": "faster-whisper",
    "funasr_onnx": "funasr-onnx",
    "videocaptioner": "videocaptioner",
    "audio_separator": "audio-separator",
    "pyloudnorm": "pyloudnorm",
    "silero_vad": "silero-vad",
    "torch": "torch (CUDA 版)",
}


@dataclass
class CheckResult:
    key: str
    title: str
    status: str  # pass / warn / fail
    detail: str
    fix: str = ""
    blocking: bool = True  # fail 时是否阻断（UI OCR 依赖等可降级项为 False）


@dataclass
class DoctorReport:
    repo_root: str
    results: list = field(default_factory=list)

    @property
    def failed(self):
        """阻断项：status=fail 且 blocking=True。"""
        return [r for r in self.results if r.status == "fail" and r.blocking]

    @property
    def missing(self):
        """全部 fail（含不阻断项），用于汇总计数。"""
        return [r for r in self.results if r.status == "fail"]

    @property
    def warned(self):
        return [r for r in self.results if r.status == "warn"]


def _ok(key, title, detail):
    return CheckResult(key, title, "pass", detail)


def _warn(key, title, detail, fix=""):
    return CheckResult(key, title, "warn", detail, fix)


def _fail(key, title, detail, fix="", blocking=True):
    return CheckResult(key, title, "fail", detail, fix, blocking)


# ── 1. 仓库布局 ────────────────────────────────────────────────────
def check_repo_layout(repo_root: str) -> CheckResult:
    required = [
        ("lib/jianying_launch.py", "启动共用层"),
        ("pipeline/export_config.py", "统一配置解析"),
        ("pipeline/presets.example.json", "配置模板"),
        (".trae/skills/jianying-editor/SKILL.md", "剪映技能"),
        (".trae/skills/jianying-editor/examples/robust_auto_export.py", "导出脚本"),
    ]
    missing = [f"{rel}（{desc}）" for rel, desc in required
               if not os.path.exists(os.path.join(repo_root, *rel.split("/")))]
    if missing:
        return _fail(
            "repo_layout", "仓库布局",
            "缺少：" + "；".join(missing),
            "请从完整仓库运行；skill 必须位于 <repo>/.trae/skills/ 下，本文件不能单独拷走使用",
        )
    notes = []
    if not os.path.isfile(os.path.join(repo_root, "pipeline", "presets.json")):
        notes.append("未找到 pipeline/presets.json（个人配置）")
    if notes:
        return _warn(
            "repo_layout", "仓库布局",
            "核心目录齐全；" + "；".join(notes),
            "个人配置：复制 pipeline/presets.example.json 为 presets.json",
        )
    return _ok("repo_layout", "仓库布局", f"结构完整（{repo_root}）")


# ── 2. Python 与依赖 ───────────────────────────────────────────────
def check_python(version_info=None) -> CheckResult:
    v = version_info or sys.version_info
    if hasattr(v, "major"):
        major, minor, micro = v.major, v.minor, v.micro
    else:
        major, minor, micro = (tuple(v) + (0, 0, 0))[:3]
    text = f"{major}.{minor}.{micro}"
    if (major, minor) < MIN_PYTHON:
        return _fail("python", "Python 版本", f"当前 {text}，最低要求 3.10",
                     "安装 Python 3.12：winget install Python.Python.3.12")
    if (major, minor) < RECOMMENDED_PYTHON:
        return _warn("python", "Python 版本", f"当前 {text}，可用但推荐 3.12",
                     "安装 Python 3.12 后重跑 pipeline/install_deps.ps1")
    return _ok("python", "Python 版本", text)


def _requirements_pins():
    """requirements.txt 的版本约束（唯一真源）：`==` 严格相等，`>=` 不低于。"""
    pins = {}
    path = os.path.join(_REPO_ROOT, "requirements.txt")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=)\s*([A-Za-z0-9_.\-+]+)$", s)
                if m:
                    pins[m.group(1)] = (m.group(2), m.group(3))
    except OSError:
        pass
    return pins


def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def _installed_version(dist):
    try:
        return importlib.metadata.version(dist)
    except Exception:
        return ""


def _find_spec(name):
    try:
        return importlib.util.find_spec(name)
    except Exception:
        return None


def check_dependencies(find_spec=None) -> CheckResult:
    real = find_spec is None
    finder = find_spec or _find_spec
    missing_core = [pip for mod, pip in CORE_MODULES.items() if finder(mod) is None]
    missing_ai = [pip for mod, pip in AI_MODULES.items() if finder(mod) is None]
    if missing_core:
        return _fail(
            "deps", "Python 依赖",
            "缺少核心依赖：" + "、".join(missing_core),
            "运行 pipeline/install_deps.ps1，或 pip install -r requirements.txt",
        )
    if missing_ai:
        return _warn(
            "deps", "Python 依赖",
            "核心依赖齐全；AI 增强依赖缺失：" + "、".join(missing_ai),
            "需要字幕识别/人声分离时运行 pipeline/install_deps.ps1",
        )
    if real:
        # requirements.txt 是唯一真源：`==` 必须严格一致，`>=` 不低于
        bad = []
        for name, (op, want) in _requirements_pins().items():
            got = _installed_version(name)
            if not got:
                continue
            if op == ">=":
                if _version_tuple(got) < _version_tuple(want):
                    bad.append(f"{name} {got}<{want}")
            elif got != want:
                bad.append(f"{name} {got}≠{want}")
        if bad:
            return _fail(
                "deps", "Python 依赖",
                "版本与 requirements.txt 不一致：" + "、".join(bad[:6]) +
                ("等" if len(bad) > 6 else ""),
                "pip install -r requirements.txt（严格按 requirements.txt 的定版对齐）",
            )
    return _ok("deps", "Python 依赖", "核心与 AI 依赖均已安装，版本与 requirements.txt 一致")


# ── 2b. 测试依赖（仅开发/验证跑 tests/ 用，不阻断）─────────────────
def check_test_deps() -> CheckResult:
    """pytest 只用于跑 tests/；终端用户不需要，缺失只提示不阻断。"""
    note = "仅开发/验证跑 tests/ 需要，终端用户可忽略"
    try:
        spec = importlib.util.find_spec("pytest")
    except Exception:
        spec = None
    if spec is None:
        return _warn(
            "test_deps", "测试依赖",
            f"未安装 pytest；{note}",
            "py -3.12 -m pip install -r requirements-dev.txt（或 install_deps.ps1 -IncludeTest）",
        )
    try:
        ver = importlib.metadata.version("pytest")
    except importlib.metadata.PackageNotFoundError:
        ver = "(版本未知)"
    return _ok("test_deps", "测试依赖", f"pytest {ver}（{note}）")


# ── 3. ffmpeg / ffprobe ────────────────────────────────────────────
def _binary_version(path: str) -> str:
    try:
        r = subprocess.run(
            [path, "-version"], capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        line = (r.stdout or "").strip().splitlines()
        return line[0].strip() if line else ""
    except Exception:
        return ""


def check_ffmpeg(ffmpeg_path: Optional[str] = None, ffprobe_path: Optional[str] = None,
                 which=None) -> CheckResult:
    finder = which or shutil.which
    ff = ec.get_ffmpeg() if ffmpeg_path is None else ffmpeg_path
    fp = ec.get_ffprobe() if ffprobe_path is None else ffprobe_path
    if not os.path.isfile(ff):
        ff = finder("ffmpeg") or ""
    if not os.path.isfile(fp):
        fp = finder("ffprobe") or ""
    missing = [name for name, path in (("ffmpeg", ff), ("ffprobe", fp)) if not path]
    if missing:
        return _fail(
            "ffmpeg", "ffmpeg / ffprobe",
            "未找到：" + "、".join(missing) + f"（依赖根: {ec.get_deps_root()}）",
            "运行 pipeline/install_deps.ps1，或把 ffmpeg/ffprobe 放进 "
            "<deps_root>/tools/ffmpeg/bin/ 或加入 PATH",
        )
    versions = [f"{name}: {_binary_version(path) or path}"
                for name, path in (("ffmpeg", ff), ("ffprobe", fp))]
    return _ok("ffmpeg", "ffmpeg / ffprobe", "；".join(versions))


# ── 4. 剪映安装与版本 ──────────────────────────────────────────────
def check_jianying_install(inspect_fn=None) -> CheckResult:
    try:
        import jianying_launch as jl
        inspect = inspect_fn or jl.inspect_jianying_install
        info = inspect()
        required = jl.REQUIRED_JY_VERSION
    except Exception as exc:
        return _fail("jianying_install", "剪映安装与版本",
                     f"无法读取剪映安装信息：{exc}",
                     "确认从仓库根运行，且 Windows 上已安装剪映专业版")
    if info.get("ok"):
        return _ok("jianying_install", "剪映安装与版本",
                   f"{info['version']}（{info['exe']}）")
    detail = info.get("message") or "未找到受支持的剪映版本"
    problems = info.get("problems") or []
    if problems:
        detail += "；候选：" + "；".join(problems[:4])
    return _fail(
        "jianying_install", "剪映安装与版本", detail,
        f"安装剪映专业版 {required}，或在 pipeline/presets.json 设置 jianying_dir / "
        "设置环境变量 JY_JIANYING_DIR 指向安装目录",
    )


# ── 5. 剪映进程 / 桌面会话 ─────────────────────────────────────────
def check_jianying_session(process_running_fn=None) -> CheckResult:
    running = None
    try:
        import jianying_launch as jl
        running = (process_running_fn or jl.process_running)()
    except Exception:
        running = None
    proc_text = "剪映进程运行中" if running else "剪映进程未运行（导出时会冷启动）"

    if os.name != "nt":
        return _warn("jianying_session", "剪映进程 / 桌面会话",
                     f"{proc_text}；当前非 Windows，UI 自动化不可用",
                     "剪映自动化仅在 Windows 交互式桌面会话下可用")
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        session_id = ctypes.c_ulong()
        has_session = bool(kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(session_id)))
        monitors = int(user32.GetSystemMetrics(80))  # SM_CMONITORS
        if has_session and session_id.value == 0:
            return _fail("jianying_session", "剪映进程 / 桌面会话",
                         f"{proc_text}；当前处于 Session 0（无交互桌面）",
                         "请在已登录的桌面会话中运行，不要在服务/计划任务的无桌面环境执行")
        if monitors <= 0:
            return _fail("jianying_session", "剪映进程 / 桌面会话",
                         f"{proc_text}；未检测到可用显示器",
                         "连接显示器或开启远程桌面后重试")
        return _ok("jianying_session", "剪映进程 / 桌面会话",
                   f"{proc_text}；交互桌面可用（session={session_id.value}，显示器 {monitors}）")
    except Exception as exc:
        return _warn("jianying_session", "剪映进程 / 桌面会话",
                     f"{proc_text}；桌面会话探测失败：{exc}",
                     "确认在 Windows 交互式桌面运行")


# ── 6. 配置路径 ────────────────────────────────────────────────────
def config_paths():
    """返回 [(key, 标题, 路径, 是否输出目录, 环境变量名)]，全部来自统一解析层。"""
    paths = ec.resolve_paths("console")["paths"]
    return [
        ("export_dir", "成品导出根目录", paths.get("export_dir", ""), True, "JY_EXPORT_DIR"),
        ("deps_root", "依赖根目录", paths.get("deps_root", ""), False, "JY_DEPS_ROOT"),
        ("jianying_dir", "剪映安装目录", paths.get("jianying_dir", ""), False, "JY_JIANYING_DIR"),
        ("projects_root", "剪映草稿根目录", paths.get("projects_root", ""), False, "JY_PROJECTS_ROOT"),
        ("presets_root", "剪映组合预设库", paths.get("presets_root", ""), False, "JY_PRESETS_ROOT"),
        ("jianying_log_dir", "剪映日志目录", paths.get("jianying_log_dir", ""), False, "JY_JIANYING_LOG_DIR"),
        ("series_root", "短剧素材根目录", paths.get("series_root", ""), False, "JY_SERIES_ROOT"),
        ("archive_root", "归档/备份根目录", paths.get("archive_root", ""), True, "JY_ARCHIVE_ROOT"),
    ]


def _writable(path):
    try:
        return os.access(path, os.W_OK)
    except Exception:
        return False


def check_config_paths(paths: Optional[Iterable] = None) -> list:
    results = []
    if paths is None:
        entries = config_paths()
        issue_map = {item["key"]: item["message"]
                     for item in ec.validate_config("console")["issues"]}
    else:
        entries = paths
        issue_map = {}
    for key, title, path, is_output, env_name in entries:
        exists = bool(path) and os.path.isdir(path)
        if exists:
            if _writable(path):
                results.append(_ok("path:" + key, title, f"{path}（存在且可写）"))
            else:
                results.append(_warn("path:" + key, title, f"{path}（存在但当前用户不可写）",
                                     f"检查目录权限，或通过 {env_name} / presets.json 改到可写目录"))
            continue
        if is_output:
            parent = os.path.dirname(os.path.abspath(path)) if path else ""
            parent_ok = bool(parent) and os.path.isdir(parent) and _writable(parent)
            results.append(_warn(
                "path:" + key, title, f"{path}（尚不存在，首次导出时创建）",
                "" if parent_ok else f"父目录不可写；请先创建或通过 {env_name} / presets.json 指定",
            ))
        else:
            detail = issue_map.get(key) or f"{path or '(空)'}（不存在）"
            results.append(_fail(
                "path:" + key, title, detail,
                f"安装/初始化对应组件，或通过 {env_name} / pipeline/presets.json 指向正确目录",
            ))
    return results


# ── 7. 草稿根是否已被剪映索引 ─────────────────────────────────────
def check_draft_index(projects_root: Optional[str] = None) -> CheckResult:
    root = projects_root or ec.get_projects_root()
    if not root or not os.path.isdir(root):
        return _fail("draft_index", "草稿根索引", f"草稿根不存在：{root or '(空)'}",
                     "先让剪映创建草稿根，或设置 JY_PROJECTS_ROOT / presets.json projects_root")
    meta_path = os.path.join(root, "root_meta_info.json")
    if not os.path.isfile(meta_path):
        return _warn("draft_index", "草稿根索引",
                     f"未找到 root_meta_info.json（{root}）",
                     "打开一次剪映首页或新建一个草稿，让剪映写入索引后重试")
    try:
        with open(meta_path, encoding="utf-8-sig") as f:
            meta = json.load(f)
    except Exception as exc:
        return _warn("draft_index", "草稿根索引", f"root_meta_info.json 无法解析：{exc}",
                     "由剪映重新生成索引（打开首页/新建草稿）")
    indexed = str(meta.get("draft_root_path") or "").strip()
    if indexed and os.path.normcase(os.path.abspath(indexed)) != os.path.normcase(os.path.abspath(root)):
        return _warn("draft_index", "草稿根索引",
                     f"索引记录的草稿根是 {indexed}，与当前配置 {root} 不一致",
                     "把剪映的草稿位置改到当前目录，或把 projects_root 指回索引目录")
    try:
        drafts = [n for n in os.listdir(root)
                  if os.path.isdir(os.path.join(root, n))
                  and (os.path.isfile(os.path.join(root, n, "draft_content.json"))
                       or os.path.isfile(os.path.join(root, n, "draft_info.json")))]
    except OSError as exc:
        return _warn("draft_index", "草稿根索引", f"无法读取草稿目录：{exc}")
    return _ok("draft_index", "草稿根索引", f"已索引；可见草稿 {len(drafts)} 个")


# ── 8. watcher 计划任务 / 运行状态（本机运维信息，只读、不阻断）────
WATCHER_TASK_NAME = "TraeDramaWatcher"
_HEARTBEAT_NAME = "watcher_heartbeat.json"
_DEFAULT_STALL_SECONDS = 900


def _task_query_script(task_name):
    return (
        "$ErrorActionPreference='Stop';"
        "$t = Get-ScheduledTask -TaskName '" + task_name + "' -ErrorAction Stop;"
        "Write-Output ('{0}|{1}' -f [string]$t.State, [bool]$t.Settings.Enabled)"
    )


def _run_powershell(script):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def check_watcher_task(runner=None, task_name=WATCHER_TASK_NAME, pause_path=None) -> CheckResult:
    """Windows 计划任务的注册与启用状态（只读查询，不注册/不启停）。

    本机运维信息：未注册或已禁用的计划任务只说明 watcher 不会自动留守，
    别人机器上不注册属正常，因此不产生阻断项，也不影响退出码。
    """
    note = "本机运维信息，不影响导出；其他机器未注册计划任务属正常"
    pause = pause_path or os.path.join(_REPO_ROOT, "pipeline", "_work", "watcher.pause")
    if os.path.isfile(pause):
        note += "；自动拉起已暂停（watcher.pause），任务触发时不会拉起 watcher"
    if os.name != "nt":
        return _warn("watcher_task", "watcher 计划任务",
                     f"非 Windows，未查询 {task_name}；{note}",
                     "watcher 仅在 Windows 交互式桌面下由计划任务守护")
    run = runner or _run_powershell
    try:
        proc = run(_task_query_script(task_name))
    except Exception as exc:
        return _warn("watcher_task", "watcher 计划任务",
                     f"查询 {task_name} 失败：{exc}；{note}")
    if getattr(proc, "returncode", 1) != 0:
        return _warn(
            "watcher_task", "watcher 计划任务",
            f"未注册 {task_name}（或当前账户无权查询）；{note}",
            "需要登录后自动守护时再按文档注册该任务；不影响手动运行与导出",
        )
    raw = (getattr(proc, "stdout", "") or "").strip()
    state, _, enabled = raw.partition("|")
    state = state.strip() or "Unknown"
    enabled_flag = enabled.strip().lower() in ("true", "1")
    if not enabled_flag or state.lower() == "disabled":
        return _warn(
            "watcher_task", "watcher 计划任务",
            f"已注册但已禁用（状态 {state}）；{note}",
            "需要自动守护时在任务计划程序中启用，或重新注册该任务",
        )
    return _ok("watcher_task", "watcher 计划任务",
               f"已注册并启用（状态 {state}；{note}）")


def _read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pid_alive(pid) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        for line in out.splitlines():
            cols = [c.strip().strip('"') for c in line.split(",")]
            if len(cols) > 1 and cols[1] == str(pid):
                return True
    except Exception:
        pass
    return False


def _watcher_lock_paths(roots=None, jobs=None) -> list:
    """可能的 .watcher.lock 路径：有效扫描根（series_root + 额外根）下 <剧>/cover_job 与显式 jobs。"""
    if roots is None or jobs is None:
        watch = ec.get_watch_config()
        if roots is None:
            try:
                roots = ec.get_watch_roots()
            except Exception:
                roots = watch.get("roots") or []
        if jobs is None:
            jobs = watch.get("jobs")
    paths = []
    for root in roots or []:
        try:
            with os.scandir(str(root)) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            paths.extend([
                os.path.join(entry.path, ".watcher.lock"),
                os.path.join(entry.path, "cover_job", ".watcher.lock"),
            ])
    for job in jobs or []:
        base = str(job)
        if os.path.isfile(base) and os.path.basename(base) == "job.json":
            base = os.path.dirname(base)
        paths.extend([
            os.path.join(base, ".watcher.lock"),
            os.path.join(base, "cover_job", ".watcher.lock"),
        ])
    seen, out = set(), []
    for path in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            out.append(path)
    return out


def _alive_watcher_locks(lock_paths, pid_alive) -> list:
    out = []
    for path in lock_paths:
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        try:
            pid = int(data.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid_alive(pid):
            out.append({"pid": pid, "phase": data.get("phase"),
                        "job": data.get("job") or os.path.dirname(os.path.dirname(path))})
    return out


def check_watcher_runtime(repo_root=None, heartbeat_path=None, lock_paths=None,
                          now=None, pid_alive=None, stall_seconds=None,
                          pause_path=None, stop_path=None) -> CheckResult:
    """watcher 运行状态：心跳新鲜度 + 存活锁（只读，未运行只警告不阻断）。"""
    root = repo_root or _REPO_ROOT
    hb_path = heartbeat_path or os.path.join(root, "pipeline", "_work", _HEARTBEAT_NAME)
    pause = pause_path or os.path.join(root, "pipeline", "_work", "watcher.pause")
    stop = stop_path or os.path.join(root, "pipeline", "_work", "watcher.stop")
    paused = os.path.isfile(pause)
    stopping = os.path.isfile(stop)
    alive = pid_alive or _pid_alive
    now_ts = time.time() if now is None else now
    if stall_seconds is None:
        try:
            stall_seconds = int(ec.get_watch_config().get("stall_heartbeat_s")
                                or _DEFAULT_STALL_SECONDS)
        except (TypeError, ValueError):
            stall_seconds = _DEFAULT_STALL_SECONDS
    note = "本机运维信息，不影响导出"

    hb = _read_json(hb_path)
    hb_pid, hb_ts = 0, None
    if isinstance(hb, dict):
        try:
            hb_pid = int(hb.get("pid") or 0)
        except (TypeError, ValueError):
            hb_pid = 0
        try:
            hb_ts = float(hb.get("updated_ts"))
        except (TypeError, ValueError):
            hb_ts = None
    age = None if hb_ts is None else max(0.0, now_ts - hb_ts)
    fresh = age is not None and age <= stall_seconds

    pause_hint = "自动拉起已暂停（watcher.pause）"
    stop_hint = "已请求优雅停止（watcher.stop）"

    if hb_pid and alive(hb_pid):
        if fresh:
            if stopping:
                return _warn(
                    "watcher_runtime", "watcher 运行状态",
                    f"运行中 pid={hb_pid}，心跳 {int(age)} 秒前（阈值 {stall_seconds}s），"
                    f"但{stop_hint}；当前轮任务结束后退出；{note}",
                    "需要取消时在 Console 环境页点「撤销停止请求」",
                )
            if paused:
                return _warn(
                    "watcher_runtime", "watcher 运行状态",
                    f"运行中 pid={hb_pid}，心跳 {int(age)} 秒前（阈值 {stall_seconds}s），"
                    f"但{pause_hint}；该进程退出后不会被重新拉起；{note}",
                    "需要恢复后台托管时删除 pipeline/_work/watcher.pause，"
                    "或在 Console 环境页点「恢复自动拉起」",
                )
            return _ok("watcher_runtime", "watcher 运行状态",
                       f"运行中 pid={hb_pid}，心跳 {int(age)} 秒前（阈值 {stall_seconds}s）；{note}")
        return _warn(
            "watcher_runtime", "watcher 运行状态",
            f"进程在跑 pid={hb_pid}，但心跳已 {int(age)} 秒未更新（阈值 {stall_seconds}s），可能卡死；"
            f"{pause_hint + '，supervisor 不会自动重启它；' if paused else ''}"
            f"{stop_hint + '；' if stopping else ''}{note}",
            "查看 pipeline/_work/watcher_console.log"
            + ("；自动拉起已暂停，需手动重启 watcher" if paused else "；supervisor 下轮会自动判定并重启"),
        )
    if fresh:
        return _warn(
            "watcher_runtime", "watcher 运行状态",
            f"心跳 {int(age)} 秒前更新，但 pid={hb_pid or '(缺失)'} 已不存活（残留心跳）"
            f"{'；' + pause_hint if paused else ''}{'；' + stop_hint + '，下次启动会立即退出' if stopping else ''}"
            f"；{note}",
            "watcher 当前未运行；" + ("自动拉起已暂停，需要时手动启动"
                                     if paused else "计划任务下次触发时会重新拉起")
            + ("；先删除 pipeline/_work/watcher.stop 再启动" if stopping else ""),
        )

    locks = _alive_watcher_locks(
        _watcher_lock_paths() if lock_paths is None else lock_paths, alive)
    if locks:
        desc = "；".join(
            f"pid={item['pid']} phase={item['phase'] or '-'} job={item['job']}" for item in locks)
        return _warn(
            "watcher_runtime", "watcher 运行状态",
            f"心跳{'已过期' if hb_ts is not None else '缺失/不可解析'}，"
            f"但 .watcher.lock 显示有存活进程：{desc}"
            f"{'；' + pause_hint if paused else ''}{'；' + stop_hint if stopping else ''}；{note}")
    reason = ("心跳文件缺失" if not os.path.isfile(hb_path)
              else "心跳已过期" if hb_ts is not None
              else "心跳不可解析")
    return _warn(
        "watcher_runtime", "watcher 运行状态",
        f"未检测到运行中的 watcher（{reason}，且未发现存活的 .watcher.lock）"
        f"{'；' + pause_hint if paused else ''}{'；' + stop_hint if stopping else ''}；{note}",
        "需要后台守护时启动 watcher"
        + ("（先删除 pipeline/_work/watcher.pause 恢复自动拉起）" if paused else "")
        + ("（先删除 pipeline/_work/watcher.stop）" if stopping else "")
        + "；手动跑 doctor/导出不受影响",
    )


# ── 汇总 ──────────────────────────────────────────────────────────
# 注册表：key 与 check_* 返回的 CheckResult.key 保持一致，
# 供 Console 按 key 选取共享项、也便于两边核对是否漏项。
_CHECKS = (
    ("repo_layout", lambda ctx: check_repo_layout(ctx["repo_root"])),
    ("python", lambda ctx: check_python()),
    ("deps", lambda ctx: check_dependencies()),
    ("test_deps", lambda ctx: check_test_deps()),
    ("ffmpeg", lambda ctx: check_ffmpeg()),
    ("jianying_install", lambda ctx: check_jianying_install()),
    ("jianying_session", lambda ctx: check_jianying_session()),
    ("config_paths", lambda ctx: check_config_paths()),
    ("draft_index", lambda ctx: check_draft_index()),
    ("watcher_task", lambda ctx: check_watcher_task()),
    ("watcher_runtime", lambda ctx: check_watcher_runtime(repo_root=ctx["repo_root"])),
)


def check_keys():
    """共享检查的 key 顺序（config_paths 一项会产生多条 path:* 结果）。"""
    return [key for key, _fn in _CHECKS]


def run_checks(keys=None, context=None) -> DoctorReport:
    """跑共享检查。

    keys=None 跑全部；否则只跑指定 key。
    context 目前支持 repo_root（默认取本文件所在仓库）。
    """
    ctx = {"repo_root": _REPO_ROOT}
    ctx.update(context or {})
    selected = None if keys is None else set(keys)
    report = DoctorReport(repo_root=ctx["repo_root"])
    for key, fn in _CHECKS:
        if selected is not None and key not in selected:
            continue
        out = fn(ctx)
        if isinstance(out, list):
            report.results.extend(out)
        else:
            report.results.append(out)
    return report
