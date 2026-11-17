# -*- coding: utf-8 -*-
"""环境初始化：分组清单（只读检查）+ 白名单修复动作。

安全边界：
  - 只写 pipeline/presets.json（含备份）、配置里的目录（mkdir）、依赖安装目标、
    Console 自己的 _console/env_jobs/；不删任何业务文件。
  - 动作走白名单，命令模板固定，参数只接受枚举值与已校验路径，不拼用户输入到 shell。
  - 计划任务只在显式动作时注册/启用，不静默。
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

# 在 paths/export_config 调整 sys.path 前锁定 Console 的 doctor，避免与 pipeline/doctor.py 同名冲突。
import doctor as runtime_doctor
import paths
import status

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PRESETS_PATH = os.path.join(paths.REPO, "pipeline", "presets.json")
PAUSE_PATH = os.path.join(paths.REPO, "pipeline", "_work", "watcher.pause")
STOP_PATH = os.path.join(paths.REPO, "pipeline", "_work", "watcher.stop")
PRESETS_EXAMPLE = os.path.join(paths.REPO, "pipeline", "presets.example.json")
REQUIRED_JY_VERSION = "5.9.0.11632"
WATCHER_TASK_NAME = "TraeDramaWatcher"

# presets.json 里程序认识的顶层键（其余给 warn 保留，不删）
KNOWN_TOP_KEYS = {
    "_说明", "export_dir", "deps_root", "jianying_dir",
    "projects_root", "presets_root", "jianying_log_dir", "series_root",
    "archive_root", "watch", "admission", "presets", "default_preset_profile",
    "translate",
}
# 路径类顶层键（绝对路径校验 + 可选目录检查）
PATH_KEYS = ("export_dir", "deps_root", "jianying_dir",
             "projects_root", "presets_root", "jianying_log_dir",
             "series_root", "archive_root")

# 依赖版本的唯一真源是 requirements.txt 的 `==` 定版：必须严格一致，
# 高了低了都算不通过（不允许「已装更高就放行」这种放水判定）。

# 核心依赖（与 console/doctor.py 同源；直接定义避免 import 到 pipeline/doctor.py）
CORE_MODULES = ["psutil", "uiautomation", "PIL", "comtypes", "requests"]


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_NO_WINDOW)
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out + err
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def _item(id_, name, st, detail, impact="", actions=()):
    return {"id": id_, "name": name, "status": st, "detail": detail,
            "impact": impact, "actions": list(actions)}


def _read_json(path):
    try:
        # utf-8-sig：兼容 PowerShell 5.1 Out-File 写出的带 BOM JSON（如 _env_profile.json）
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f), None
    except FileNotFoundError:
        return None, "文件不存在"
    except (OSError, ValueError) as e:
        return None, str(e)


def _cfg():
    cfg, _ = _read_json(PRESETS_PATH)
    return cfg if isinstance(cfg, dict) else {}


def _importable(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _installed_version(dist):
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return ""
    except Exception:
        return ""


# ── 依赖组 ───────────────────────────────────────────────────────────
def _chk_system():
    if os.name != "nt":
        return _item("system", "Windows / PowerShell", "fail",
                     f"当前系统: {platform.system()}（仅支持 Windows 10/11）")
    win = sys.getwindowsversion()
    windows = "Windows 11" if win.build >= 22000 else "Windows 10"
    windows_detail = f"{windows}（{win.major}.{win.minor}.{win.build}）"
    rc, out = _run(["powershell.exe", "-NoProfile", "-Command",
                    "$PSVersionTable.PSVersion.ToString()"], timeout=15)
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    if rc != 0 or not match:
        return _item("system", "Windows / PowerShell", "fail",
                     f"{windows_detail}；PowerShell 版本读取失败: {(out or '')[:160]}",
                     "install_deps.ps1 与 watcher 守护无法运行")
    ps_version = tuple(int(x or 0) for x in match.groups())
    ps_text = match.group(0)
    if win.major < 10 or ps_version < (5, 1, 0):
        return _item("system", "Windows / PowerShell", "fail",
                     f"{windows_detail}；PowerShell {ps_text}",
                     "要求 Windows 10/11 且 PowerShell ≥ 5.1")
    return _item("system", "Windows / PowerShell", "ok",
                 f"{windows_detail}；PowerShell {ps_text}")


def _chk_python():
    v = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] == (3, 12):
        return _item("python", "Python 解释器", "ok", f"{v}（{sys.executable}）")
    return _item("python", "Python 解释器", "fail",
                 f"{v}（{sys.executable}）",
                 "仅支持推荐的 Python 3.12（依赖版本按 3.12 锁定）")


def _chk_pip_core():
    missing = [m for m in CORE_MODULES if not _importable(m)]
    if not missing:
        return _item("pip_core", "pip 核心依赖", "ok",
                     "、".join(CORE_MODULES) + " 均可导入")
    return _item("pip_core", "pip 核心依赖", "fail",
                 "缺失: " + "、".join(missing),
                 "建稿/导出/预览会失败",
                 [("deps.install", "全量安装依赖")])


def _chk_pip_ai():
    mods = {"demucs": "demucs", "faster_whisper": "faster-whisper",
            "funasr_onnx": "funasr-onnx", "videocaptioner": "videocaptioner",
            "torch": "torch"}
    missing = [pip for mod, pip in mods.items() if not _importable(mod)]
    if not missing:
        return _item("pip_ai", "AI 依赖（字幕/人声分离）", "ok",
                     "、".join(mods.values()) + " 均可导入")
    return _item("pip_ai", "AI 依赖（字幕/人声分离）", "warn",
                 "缺失: " + "、".join(missing),
                 "无字幕/无人声分离，或退化为 CPU",
                 [("deps.install", "全量安装依赖")])


def _chk_env_profile():
    """校验画像结构及可影响运行的字段；不比较时间戳等无运行意义的字段。"""
    p = os.path.join(paths.REPO, "pipeline", "_env_profile.json")
    profile, err = _read_json(p)
    if err or not isinstance(profile, dict):
        return _item("env_profile", "机器画像 _env_profile.json", "warn",
                     f"{p}（{err or '内容不是 JSON 对象'}）", "运行时回落默认组件",
                     "运行 python pipeline/install_deps.ps1 -DetectOnly 重建机器画像")

    required = {
        "generated_at": str, "deps_root": str, "python": str, "gpu": dict,
        "ram_gb": (int, float), "torch_channel": str, "whisper": dict,
        "ollama_model": str, "paraformer": dict, "demucs": dict,
        "translate": dict, "tts": dict, "asr_chain": list, "translate_chain": list,
    }
    nested = {
        "gpu": ("name", "driver", "vram_gb"),
        "whisper": ("model", "device", "compute_type", "options"),
        "paraformer": ("quantize", "model_dir", "vad_dir"),
        "demucs": ("model", "fallback", "options"),
        "translate": ("glm_model", "xfyun_model", "ollama_model", "ollama_options"),
        "tts": ("backend", "fallback", "speaker", "note"),
    }
    problems = [k for k, typ in required.items() if not isinstance(profile.get(k), typ)]
    for section, keys in nested.items():
        value = profile.get(section)
        if isinstance(value, dict):
            problems += [f"{section}.{k}" for k in keys if k not in value]
    if problems:
        return _item("env_profile", "机器画像 _env_profile.json", "warn",
                     "画像结构缺字段或类型错误: " + "、".join(problems[:8]),
                     "运行 python pipeline/install_deps.ps1 -DetectOnly 重建完整画像")

    mismatches = []
    informational = []  # 当前值比画像记录更新：不是问题，只记录，不告警

    def same_path(a, b):
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b)))

    try:
        import export_config as ec
        deps = ec.get_deps_root()
        if not same_path(profile["deps_root"], deps):
            mismatches.append(f"deps_root 记录为 {profile['deps_root']}，当前为 {deps}")
        paraformer = profile["paraformer"]
        for key, rel in (("model_dir", ("models", "paraformer", "iic_onnx")),
                         ("vad_dir", ("models", "paraformer", "vad_onnx"))):
            expected_path = os.path.join(deps, *rel)
            if not same_path(paraformer[key], expected_path):
                mismatches.append(f"paraformer.{key} 与当前 deps_root 不一致")
    except Exception as e:
        mismatches.append(f"依赖路径复核失败: {e}")

    recorded_py = re.search(r"\d+\.\d+\.\d+", profile["python"])
    if recorded_py:
        rec_py, cur_py = recorded_py.group(0), platform.python_version()
        if rec_py != cur_py:
            rec_mm = tuple(map(int, rec_py.split(".")[:2]))
            cur_mm = tuple(map(int, cur_py.split(".")[:2]))
            if cur_mm < rec_mm:
                mismatches.append(f"Python 记录为 {rec_py}，当前为 {cur_py}（低于记录版本）")
            else:
                informational.append(f"Python {rec_py} → {cur_py}")
    try:
        import psutil
        ram = round(psutil.virtual_memory().total / (1024 ** 3), 1)
        rec_ram = float(profile["ram_gb"])
        if ram < rec_ram - 0.2:
            mismatches.append(f"内存记录为 {rec_ram} GiB，当前约 {ram} GiB（低于记录值）")
        elif ram > rec_ram + 0.2:
            informational.append(f"内存 {rec_ram} → {ram} GiB")
    except Exception:
        pass

    gpu = profile["gpu"]
    rc, out = _run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
                    "--format=csv,noheader,nounits"], timeout=15)
    current_gpu = ""
    if rc == 0 and out.strip():
        parts = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        if len(parts) >= 3:
            current_gpu = parts[0]
            if str(gpu["name"]) != current_gpu:
                mismatches.append(f"GPU 记录为 {gpu['name'] or '无'}，当前为 {current_gpu}")
            try:
                driver = float(parts[1])
                vram = round(float(parts[2]) / 1024, 1)
                rec_driver = float(gpu["driver"])
                if driver > rec_driver + 0.01:
                    informational.append(f"GPU 驱动 {gpu['driver']} → {driver}")
                elif driver < rec_driver - 0.01:
                    mismatches.append(f"GPU 驱动记录为 {gpu['driver']}，当前为 {driver}（低于记录值）")
                rec_vram = float(gpu["vram_gb"])
                if vram > rec_vram + 0.2:
                    informational.append(f"显存 {rec_vram} → {vram} GiB")
                elif vram < rec_vram - 0.2:
                    mismatches.append(f"显存记录为 {rec_vram} GiB，当前约 {vram} GiB（低于记录值）")
                expected_channel = "cu126" if driver >= 528 else "cu118" if driver >= 460 else "cpu"
                if profile["torch_channel"] != expected_channel:
                    mismatches.append(
                        f"torch_channel 记录为 {profile['torch_channel']}，当前硬件应为 {expected_channel}")
            except (TypeError, ValueError):
                mismatches.append("GPU 驱动或显存字段不是有效数字")
    if not current_gpu and str(gpu["name"]):
        mismatches.append("当前无法通过 nvidia-smi 复核画像中记录的 NVIDIA GPU")
    if not current_gpu and not str(gpu["name"]) and profile["torch_channel"] != "cpu":
        mismatches.append("画像记录无 NVIDIA GPU，但 torch_channel 不是 cpu")

    channel = profile["torch_channel"]
    whisper = profile["whisper"]
    expected_device = "cuda" if channel.startswith("cu") else "cpu"
    expected_compute = "float16" if expected_device == "cuda" else "int8"
    if whisper["device"] != expected_device or whisper["compute_type"] != expected_compute:
        mismatches.append(
            f"whisper 为 {whisper['device']}/{whisper['compute_type']}，"
            f"按 {channel} 应为 {expected_device}/{expected_compute}")
    if whisper["model"] not in whisper["options"]:
        mismatches.append("whisper.model 不在画像声明的可选模型中")
    demucs = profile["demucs"]
    if demucs["model"] not in demucs["options"] or demucs["fallback"] not in demucs["options"]:
        mismatches.append("demucs 当前模型或兜底模型不在可选列表中")

    translate = profile["translate"]
    expected_model = str(((_cfg().get("translate") or {}).get("ollama") or {}).get(
        "model") or "").strip()
    if profile["ollama_model"] != translate["ollama_model"]:
        mismatches.append("顶层 ollama_model 与 translate.ollama_model 不一致")
    if expected_model and profile["ollama_model"] != expected_model:
        mismatches.append(
            f"ollama_model 记录为 {profile['ollama_model']}，presets 配置为 {expected_model}")
    if profile["ollama_model"] != "none" and profile["ollama_model"] not in translate["ollama_options"]:
        mismatches.append("Ollama 当前模型不在画像声明的可选模型中")

    if mismatches:
        detail = "；".join(mismatches[:6])
        if len(mismatches) > 6:
            detail += f"；另有 {len(mismatches) - 6} 项不一致"
        return _item("env_profile", "机器画像 _env_profile.json", "warn", detail,
                     "画像与当前硬件/配置不一致：运行 python pipeline/install_deps.ps1 -DetectOnly 重建画像")
    note = ""
    if informational:
        note = "；记录值已被当前更新值覆盖（" + "、".join(informational[:3]) + "），无需处理"
    return _item("env_profile", "机器画像 _env_profile.json", "ok",
                 f"{p}（结构完整；硬件、依赖路径、torch / whisper / Ollama 配置一致{note}）")


# ── 目录组 ───────────────────────────────────────────────────────────
def _chk_dir(id_, name, path, *, required=True, can_create=True, sub="",
             count_dirs=False):
    if not path:
        return _item(id_, name, "fail" if required else "warn", "未配置")
    acts = [("dirs.ensure", "创建目录")] if can_create else []
    if not os.path.isdir(path):
        return _item(id_, name, "fail" if required else "warn",
                     f"不存在: {path}", "", acts)
    extra = ""
    if sub:
        missing = [s for s in sub.split(",") if not os.path.isdir(os.path.join(path, s))]
        if missing:
            return _item(id_, name, "warn",
                         f"{path}（缺子目录: {'、'.join(missing)}）", "",
                         [("deps.install", "全量安装依赖")])
        extra = "（子目录齐全）"
    if count_dirs:
        try:
            extra += f"；一级目录 {sum(1 for e in os.scandir(path) if e.is_dir())} 个"
        except OSError:
            pass
    if not os.access(path, os.W_OK):
        return _item(id_, name, "warn", f"{path}{extra}（当前用户无写权限）",
                     "换到有写权限的目录，或调整目录权限")
    u = status.disk_usage(path)
    cap = f"；剩余 {u['free_gib']} GiB" if u else ""
    return _item(id_, name, "ok", f"{path}{extra}（存在且可写）{cap}")


# ── 配置组 ───────────────────────────────────────────────────────────
def _chk_presets_exist():
    if not os.path.isfile(PRESETS_PATH):
        return _item("presets_exist", "presets.json", "fail",
                     f"缺少 {PRESETS_PATH}", "全链路不可用",
                     [("presets.init", "从模板创建")])
    return _item("presets_exist", "presets.json", "ok", PRESETS_PATH,
                 "", [("presets.edit", "编辑配置")])


def _chk_presets_schema():
    cfg, err = _read_json(PRESETS_PATH)
    if err:
        return _item("presets_schema", "配置内容校验", "fail", err)
    rep = validate_presets(cfg)
    if rep["errors"]:
        return _item("presets_schema", "配置内容校验", "fail",
                     "；".join(rep["errors"][:3]), "",
                     [("presets.edit", "编辑配置")])
    if rep["warnings"]:
        return _item("presets_schema", "配置内容校验", "warn",
                     "；".join(rep["warnings"][:3]), "",
                     [("presets.edit", "编辑配置")])
    return _item("presets_schema", "配置内容校验", "ok",
                 "必填键/路径/预设组/默认组均通过")


def _chk_watch_roots():
    try:
        import export_config as ec
        roots = ec.get_watch_roots()
        extras = ec._watch_extra_roots()
    except Exception as e:
        return _item("watch_roots", "watcher 扫描根", "fail", str(e),
                     "watcher 扫不到剧；主素材根请写在 series_root",
                     [("presets.edit", "编辑配置")])
    if not extras:
        return _item("watch_roots", "watcher 扫描根", "ok",
                     f"仅 series_root: {roots[0]}")
    return _item("watch_roots", "watcher 扫描根", "ok",
                 "series_root + 额外根: " + "、".join(roots))


def _chk_translate_key():
    tr = _cfg().get("translate") or {}
    providers = tr.get("providers") or []
    zhipu = next((p for p in providers if p.get("name") == "zhipu"), None)
    key = (zhipu or {}).get("api_key") or ""
    if key and not key.startswith("<"):
        return _item("translate_key", "智谱 api_key（英文字幕）", "ok",
                     "已配置（不显示明文）")
    return _item("translate_key", "智谱 api_key（英文字幕）", "warn",
                 "未配置", "英文字幕缺失或降级本地模型",
                 [("presets.edit", "编辑配置")])


# ── 剪映组 ───────────────────────────────────────────────────────────
def _chk_jy_install():
    try:
        import jianying_launch as jl
        rep = jl.inspect_jianying_install()
    except Exception as e:
        return _item("jy_install", "剪映安装 + 版本", "fail", f"检查失败: {e}")
    if rep.get("ok"):
        return _item("jy_install", "剪映安装 + 版本", "ok",
                     f"{rep.get('version')}（{rep.get('exe')}）")
    return _item("jy_install", "剪映安装 + 版本", "fail",
                 rep.get("message") or "未通过",
                 f"仅支持 {REQUIRED_JY_VERSION}，版本不符导出预检直接拒绝")


def _chk_jy_session():
    """与命令行 doctor 同口径：进程状态 + Session 0 + 可用显示器。"""
    try:
        import jianying_launch as jl
        running = jl.process_running()
    except Exception:
        running = None
    proc_text = "剪映进程运行中" if running else "剪映进程未运行（需要时冷启动）"
    if os.name != "nt":
        return _item("jy_session", "剪映进程 / 桌面会话", "fail",
                     f"{proc_text}；非 Windows 系统", "UI 自动化仅支持 Windows 交互桌面")
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        session_id = ctypes.c_ulong()
        has_session = bool(kernel32.ProcessIdToSessionId(
            os.getpid(), ctypes.byref(session_id)))
        monitors = int(user32.GetSystemMetrics(80))  # SM_CMONITORS
        if has_session and session_id.value == 0:
            return _item("jy_session", "剪映进程 / 桌面会话", "fail",
                         f"{proc_text}；当前处于 Session 0（无交互桌面）",
                         "请在已登录的桌面会话中运行，不要在服务或无桌面计划任务中执行")
        if monitors <= 0:
            return _item("jy_session", "剪映进程 / 桌面会话", "fail",
                         f"{proc_text}；未检测到可用显示器",
                         "连接显示器或开启远程桌面后重试")
        return _item("jy_session", "剪映进程 / 桌面会话", "ok",
                     f"{proc_text}；交互桌面可用"
                     f"（session={session_id.value if has_session else '未知'}，显示器 {monitors}）")
    except Exception as e:
        return _item("jy_session", "剪映进程 / 桌面会话", "warn",
                     f"{proc_text}；桌面会话探测失败: {e}",
                     "确认在 Windows 交互式桌面运行")


def _chk_draft_index():
    """与 pipeline/doctor.py 的 check_draft_index 同一依据：<草稿根>/root_meta_info.json。"""
    try:
        import export_config as ec
        projects = ec.get_projects_root()
    except Exception:
        projects = ""
    if not projects or not os.path.isdir(projects):
        return _item("draft_index", "草稿根索引一致性", "fail",
                     f"草稿根不存在: {projects or '(空)'}")
    meta = os.path.join(projects, "root_meta_info.json")
    if not os.path.isfile(meta):
        return _item("draft_index", "草稿根索引一致性", "warn",
                     f"未找到 root_meta_info.json（{projects}）",
                     "打开一次剪映首页或新建草稿，让剪映写入索引")
    cfg, err = _read_json(meta)
    if err:
        return _item("draft_index", "草稿根索引一致性", "warn", f"索引不可解析: {err}")
    indexed = str((cfg or {}).get("draft_root_path") or "").strip()
    if indexed and os.path.normcase(os.path.abspath(indexed)) != os.path.normcase(
            os.path.abspath(projects)):
        return _item("draft_index", "草稿根索引一致性", "warn",
                     f"索引记录的是 {indexed}，与当前配置 {projects} 不一致",
                     "把剪映草稿位置改到当前目录，或把 projects_root 指回索引目录")
    return _item("draft_index", "草稿根索引一致性", "ok", f"索引已指向 {projects}")


def _chk_template_draft():
    try:
        import export_config as ec
        projects = ec.get_projects_root()
    except Exception:
        projects = ""
    found = [n for n in ("default", "pipeline_test", "demo_test")
             if os.path.isdir(os.path.join(projects, n))]
    if found:
        return _item("template_draft", "模板草稿 default", "ok",
                     "找到模板: " + "、".join(found))
    return _item("template_draft", "模板草稿 default", "warn",
                 "未找到 default / pipeline_test / demo_test",
                 "建稿会失败：找不到模板（请在剪映里手工建一个空草稿命名为 default）")


def _chk_preset_names():
    cfg = _cfg()
    names = []
    for k, v in cfg.items():
        if str(k).lower().startswith("presets") and isinstance(v, list):
            names += [x.get("name") for x in v if isinstance(x, dict) and x.get("name")]
    if not names:
        return _item("preset_names", "组合预设名匹配", "warn", "配置里没有预设条目")
    try:
        import export_config as ec
        root = ec.get_presets_root()
    except Exception:
        root = ""
    if not os.path.isdir(root):
        return _item("preset_names", "组合预设名匹配", "warn",
                     f"预设库目录不存在: {root}")
    try:
        disk = os.listdir(root)
    except OSError:
        disk = []

    def _usable(name):
        # 与 pipeline.find_preset_by_name 同一判定：目录名含预设名 + preset_draft/draft_content.json
        for d in disk:
            if name in d and os.path.isfile(
                    os.path.join(root, d, "preset_draft", "draft_content.json")):
                return True
        return False

    missing = [n for n in sorted(set(names)) if not _usable(n)]
    if missing:
        return _item("preset_names", "组合预设名匹配", "warn",
                     "预设库里找不到: " + "、".join(missing[:5]),
                     "这些预设会被跳过，成片少素材（名字须与剪映「组合预设」逐字一致）")
    return _item("preset_names", "组合预设名匹配", "ok",
                 f"{len(set(names))} 个预设名均能在预设库找到")


# ── 托管组 ───────────────────────────────────────────────────────────
def task_status():
    rc, out = _run(["schtasks", "/Query", "/TN", WATCHER_TASK_NAME,
                    "/FO", "LIST", "/V"], timeout=20)
    if rc != 0:
        return {"registered": False, "enabled": False, "detail": "未注册"}
    enabled = "Disabled" not in out and "已禁用" not in out
    return {"registered": True, "enabled": enabled,
            "detail": "已注册并启用" if enabled else "已注册但被禁用"}


def _chk_watcher_task():
    t = task_status()
    if t["registered"] and t["enabled"]:
        return _item("watcher_task", "watcher 计划任务（可选）", "ok", t["detail"])
    return _item("watcher_task", "watcher 计划任务（可选）", "warn", t["detail"],
                 "可选配置：不注册只影响自动托管，不影响手动跑",
                 [("task.register", "注册并启用计划任务")])


def watcher_paused():
    """暂停哨兵是否存在（pipeline/_work/watcher.pause）。"""
    return os.path.isfile(PAUSE_PATH)


def pause_watcher(reason=""):
    """写暂停哨兵：supervisor 与 delayed_start 看到后不再拉起 watcher。

    不杀正在跑的 watcher（避免打断正在导出的任务），只是不再自动拉起；
    计划任务照常触发，但每次都会记一行“已暂停”。
    """
    d = os.path.dirname(PAUSE_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    text = (reason or "").strip()
    with open(PAUSE_PATH, "w", encoding="utf-8") as f:
        f.write((text + "\n") if text else
                (time.strftime("%Y-%m-%d %H:%M:%S") + " Console 手动暂停\n"))
    return {"paused": True, "path": PAUSE_PATH,
            "detail": "已暂停：supervisor / delayed_start 不会自动拉起 watcher"}


def resume_watcher():
    try:
        os.remove(PAUSE_PATH)
    except FileNotFoundError:
        pass
    return {"paused": False, "path": PAUSE_PATH,
            "detail": "已恢复：计划任务下次触发会重新拉起 watcher"}


def watcher_stop_requested():
    """优雅停止哨兵是否存在（pipeline/_work/watcher.stop）。"""
    return os.path.isfile(STOP_PATH)


def request_watcher_stop(reason=""):
    """写优雅停止哨兵：watcher 在下一轮开头消费并退出。

    当前轮任务会跑完再退出，不会砍断编辑/导出。watcher 未运行时直接拒绝，
    避免留下一个“下次一启动就自杀”的陈旧哨兵。
    """
    w = status.watcher_info()
    if w.get("state") != "running":
        return {"requested": False, "running": False, "path": STOP_PATH,
                "detail": "watcher 当前未运行，无需停止"}
    d = os.path.dirname(STOP_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    text = (reason or "").strip()
    with open(STOP_PATH, "w", encoding="utf-8") as f:
        f.write((text + "\n") if text else
                (time.strftime("%Y-%m-%d %H:%M:%S") + " Console 请求优雅停止\n"))
    return {"requested": True, "running": True, "path": STOP_PATH,
            "detail": "已请求停止：当前任务跑完后 watcher 自动退出，且不再接新任务"}


def cancel_watcher_stop():
    try:
        os.remove(STOP_PATH)
    except FileNotFoundError:
        pass
    return {"requested": False, "path": STOP_PATH, "detail": "已撤销停止请求"}


# ── Console 登录自启 ─────────────────────────────────────────────────
STARTUP_DIR = os.path.join(os.environ.get("APPDATA") or "",
                           "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
CONSOLE_LNK = os.path.join(STARTUP_DIR, "TraeJianyingConsole.lnk")
LAUNCHER_PS1 = os.path.join(paths.REPO, "scripts", "console_launcher.ps1")


def autostart_status():
    enabled = bool(STARTUP_DIR) and os.path.isfile(CONSOLE_LNK)
    return {"enabled": enabled, "shortcut": CONSOLE_LNK,
            "detail": "已开启：登录 Windows 时自动启动 Console" if enabled
                      else "未开启登录自启"}


def set_autostart(enabled):
    """开/关登录自启：复用 console_launcher.ps1，写 Startup 快捷方式。"""
    if not os.path.isfile(LAUNCHER_PS1):
        raise RuntimeError("缺少 scripts\\console_launcher.ps1，无法切换登录自启")
    action = "autostart-on" if enabled else "autostart-off"
    rc, out = _run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", LAUNCHER_PS1, "-Action", action], timeout=30)
    if rc != 0:
        raise RuntimeError((out or "").strip()[:300] or "切换登录自启失败")
    st = autostart_status()
    st["detail"] = "已开启：登录 Windows 时自动启动 Console" if st["enabled"] \
        else "已关闭登录自启"
    return st


def _chk_watcher_runtime():
    w = status.watcher_info()
    state = w.get("state")
    hb = w.get("heartbeat") or {}
    resumed = [("task.resume", "恢复自动拉起")]
    cancel_stop = [("task.stop-cancel", "撤销停止请求")]
    if watcher_stop_requested():
        return _item("watcher_runtime", "watcher 运行态", "warn",
                     f"已请求优雅停止（哨兵 {STOP_PATH}）；当前 state={state}",
                     "当前任务跑完后 watcher 自动退出，不再接新任务", cancel_stop)
    if watcher_paused():
        return _item("watcher_runtime", "watcher 运行态", "warn",
                     f"已暂停（哨兵 {PAUSE_PATH}）；当前 state={state}",
                     "暂停只阻止自动拉起，不杀正在跑的任务", resumed)
    if state == "running":
        return _item("watcher_runtime", "watcher 运行态", "ok",
                     f"在跑（心跳 {hb.get('age_text') or '-'}）",
                     "", [("task.stop", "优雅停止 watcher"),
                          ("task.pause", "暂停自动拉起")])
    return _item("watcher_runtime", "watcher 运行态", "warn",
                 f"未在跑（state={state}）", "不影响手动跑",
                 [("task.pause", "暂停自动拉起")])


def _chk_lock():
    d = status.lock_diag()
    if not d["exists"]:
        return _item("export_lock", "全局导出锁", "ok", "未占用")
    if d["pid_state"] == "alive":
        return _item("export_lock", "全局导出锁", "ok",
                     f"被占用 pid={d['pid']}（有任务在导）")
    return _item("export_lock", "全局导出锁", "warn",
                 f"陈旧锁 pid={d['pid']}（进程已不存在）",
                 "确认无任务后可删除 .export.lock")


def _chk_config_resolution():
    try:
        import export_config as ec
        rep = ec.validate_config("console")
    except Exception as e:
        return _item("config_resolution", "统一路径解析", "fail", f"检查失败: {e}")
    if rep["ok"]:
        # 一切正常时不显示：路径是否可用已由「路径」组逐项给出，避免重复刷两条一样的结论
        return None
    messages = [i["message"] for i in rep["issues"]]
    return _item("config_resolution", "统一路径解析", "fail",
                 "；".join(messages[:4]),
                 "按提示补齐 pipeline/presets.json；旧机器可运行 migrate_config.py --write",
                 [("config.migrate", "从 watch.roots 迁移 series_root"),
                  ("presets.edit", "编辑配置")])


def _ollama_executable():
    """定位本机 Ollama，不依赖 PATH 是否刷新到当前 Console 进程。"""
    candidates = [shutil.which("ollama") or shutil.which("ollama.exe")]
    deps = str(_cfg().get("deps_root") or "").strip()
    if deps:
        candidates.append(os.path.join(deps, "Ollama", "ollama.exe"))
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(os.path.join(local, "Programs", "Ollama", "ollama.exe"))
    program_files = os.environ.get("ProgramFiles", "")
    if program_files:
        candidates.append(os.path.join(program_files, "Ollama", "ollama.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return ""


def start_ollama(wait_s=20.0):
    """后台拉起本机 `ollama serve` 并等待 /api/tags 恢复。

    只启动进程，不拉取模型；已运行时直接返回，避免重复占用端口。
    """
    import urllib.parse
    import urllib.request

    oc = ((_cfg().get("translate") or {}).get("ollama") or {})
    host = str(oc.get("host") or "http://127.0.0.1:11434").rstrip("/")
    model = str(oc.get("model") or "").strip()

    def _probe():
        with urllib.request.urlopen(host + "/api/tags", timeout=2) as r:
            data = json.loads(r.read() or b"{}")
        return [str(m.get("name") or m.get("model") or "")
                for m in (data.get("models") or [])]

    try:
        _probe()
        return {"ok": True, "started": False,
                "detail": f"Ollama 已在运行（{host}），无需重复拉起"}
    except Exception:
        pass

    parsed = urllib.parse.urlsplit(host)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return {"ok": False, "started": False,
                "detail": f"{host} 不是本机地址，无法由这台电脑一键拉起"}
    exe = _ollama_executable()
    if not exe:
        return {"ok": False, "started": False,
                "detail": "未找到 ollama.exe；请先运行 install_deps.ps1 安装 Ollama"}

    env = os.environ.copy()
    if parsed.netloc:
        env["OLLAMA_HOST"] = parsed.netloc
    deps = str(_cfg().get("deps_root") or "").strip()
    models_dir = os.path.join(deps, "Ollama", "models") if deps else ""
    if models_dir and os.path.isdir(models_dir):
        env.setdefault("OLLAMA_MODELS", models_dir)
    flags = (_NO_WINDOW
             | getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        proc = subprocess.Popen(
            [exe, "serve"], cwd=os.path.dirname(exe), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    except OSError as e:
        return {"ok": False, "started": False,
                "detail": f"Ollama 启动失败：{e}"}

    deadline = time.time() + max(0.0, float(wait_s))
    while time.time() < deadline:
        try:
            names = [n for n in _probe() if n]
            detail = f"Ollama 已拉起（pid={proc.pid}，{host}）"
            if model and model not in names and not any(
                    n.split(":")[0] == model.split(":")[0] for n in names):
                detail += f"；但缺少模型 {model}，请再补齐依赖"
            return {"ok": True, "started": True, "pid": proc.pid,
                    "detail": detail}
        except Exception:
            time.sleep(0.5)
    return {"ok": False, "started": True, "pid": proc.pid,
            "detail": f"已发起启动（pid={proc.pid}），但 {wait_s:g} 秒内仍不可达；"
                      "请检查 Ollama 进程与端口占用"}


def _chk_ollama_service():
    """Ollama 本地翻译兜底：探针 /api/tags，不自动启动、不访问外网。"""
    oc = ((_cfg().get("translate") or {}).get("ollama") or {})
    if oc.get("enabled") is False:
        return _item("ollama_service", "Ollama 本地翻译服务", "warn",
                     "配置已禁用 translate.ollama；智谱/讯飞都失败时没有本地兜底")
    host = str(oc.get("host") or "http://127.0.0.1:11434").rstrip("/")
    model = str(oc.get("model") or "").strip()
    try:
        import urllib.request
        with urllib.request.urlopen(host + "/api/tags", timeout=3) as r:
            data = json.loads(r.read() or b"{}")
        names = [str(m.get("name") or m.get("model") or "")
                 for m in (data.get("models") or [])]
        names = [n for n in names if n]
    except Exception as e:
        return _item("ollama_service", "Ollama 本地翻译服务", "warn",
                     f"服务未运行或不可达（{host}）：{str(e)[:100]}；"
                     "英文字幕会少一层本地兜底",
                     "点「一键拉起 Ollama」，恢复后再点「完整检查」",
                     [("ollama.start", "一键拉起 Ollama")])
    if model and model not in names and not any(
            n.split(":")[0] == model.split(":")[0] for n in names):
        return _item("ollama_service", "Ollama 本地翻译服务", "warn",
                     f"已运行，但缺少配置模型 {model}；已装："
                     + ("、".join(names[:5]) or "无"),
                     f"ollama pull {model}",
                     [("deps.install", "安装/补齐依赖")])
    return _item("ollama_service", "Ollama 本地翻译服务", "ok",
                 f"已运行（{host}）"
                 + (f"；模型 {model} 可用" if model else f"；已装模型 {len(names)} 个"))


# ── 汇总清单 ─────────────────────────────────────────────────────────
def inventory():
    try:
        import export_config as ec
        deps = ec.resolve_paths("console")["paths"].get("deps_root", "")

        def _opt(fn):
            try:
                return fn() or ""
            except Exception:
                return ""
        jy_dir = _opt(ec.get_jianying_dir)
        jy_presets = _opt(ec.get_presets_root)
        jy_log = _opt(ec.get_jianying_log_dir)
    except Exception:
        deps = jy_dir = jy_presets = jy_log = ""
    roots = paths.known_roots()
    groups = [
        {"key": "deps", "title": "运行环境和状态", "items": [
            _chk_system(), _chk_python(), _chk_pip_core(), _chk_pip_ai(),
            _chk_env_profile(), _chk_ollama_service(),
        ]},
        # 所有路径只在这里出现一次（完整检查时就地替换状态，不再搬到「运行状态」）
        {"key": "dirs", "title": "配置路径", "items": [
            _chk_dir("deps_root", "依赖根 deps_root", deps, sub="tools,models"),
            _chk_dir("series_root", "短剧素材根", roots.get("series"), count_dirs=True),
            _chk_dir("export_dir", "成品导出根", roots.get("export")),
            _chk_dir("archive_root", "归档根", roots.get("archive"), required=False),
            _chk_dir("projects_root", "剪映草稿根", roots.get("projects"), can_create=False),
            _chk_dir("jianying_dir", "剪映安装目录", jy_dir, can_create=False),
            _chk_dir("presets_root", "剪映组合预设库", jy_presets, can_create=False),
            _chk_dir("jianying_log_dir", "剪映日志目录", jy_log, required=False,
                     can_create=False),
        ]},
        {"key": "config", "title": "预设配置检查", "items": [
            it for it in (_chk_presets_exist(), _chk_presets_schema(),
                          _chk_config_resolution(), _chk_watch_roots(),
                          _chk_translate_key())
            if it
        ]},
        {"key": "jianying", "title": "剪映前置", "items": [
            _chk_jy_install(), _chk_jy_session(), _chk_draft_index(),
            _chk_template_draft(), _chk_preset_names(),
        ]},
        {"key": "host", "title": "watcher 托管服务", "items": [
            _chk_watcher_task(), _chk_watcher_runtime(), _chk_lock(),
        ]},
    ]
    summary = {"ok": 0, "warn": 0, "fail": 0}
    for g in groups:
        for it in g["items"]:
            summary[it["status"] if it["status"] in summary else "warn"] += 1
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "groups": groups, "summary": summary}


# ── 全量依赖检查（按钮触发，含版本 pin / CUDA / 模型清单）─────────────
def _requirements():
    req = []
    path = os.path.join(paths.REPO, "requirements.txt")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if not s:
                    continue
                m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=)\s*([A-Za-z0-9_.\-+]+)$", s)
                if m:
                    req.append((m.group(1), m.group(2), m.group(3)))
    except OSError:
        pass
    return req


def _version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def deps_report():
    rows = []
    for dist, op, want in _requirements():
        got = _installed_version(dist)
        required = f"{op} {want}"
        if not got:
            st, note = "fail", "未安装"
        elif op == ">=":
            ok = _version_tuple(got) >= _version_tuple(want)
            st, note = ("ok", f"满足 >={want}") if ok else ("fail", f"要求 >={want}，实际 {got}")
        elif got == want:
            st, note = "ok", "版本一致"
        else:
            # requirements.txt 是唯一真源：`==` 定版必须严格一致，高了也不放过
            st, note = "fail", f"要求 {want}，实际 {got}"
        rows.append({"name": dist, "required": required, "installed": got or "-",
                     "status": st, "detail": note})
    # torch CUDA：必须子进程检查——在 HTTP 处理线程（非主线程）import torch 会 hang。
    # CUDA build 按机器画像校验：本机画像为 cu126 时就强制 +cu126，不能只看 CUDA=True。
    pyexe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.isfile(pyexe):
        pyexe = sys.executable
    profile, _ = _read_json(os.path.join(paths.REPO, "pipeline", "_env_profile.json"))
    channel = str((profile or {}).get("torch_channel") or "").strip().lower()
    required_cuda = f"{channel} + CUDA 可用" if channel.startswith("cu") else "CUDA 可用"
    rc, out = _run([pyexe, "-c",
                    "import torch;print(torch.__version__);print(torch.cuda.is_available())"],
                   timeout=120)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if rc == 0 and len(lines) >= 2:
        ver, cuda = lines[-2], lines[-1] == "True"
        installed = f"{ver}；CUDA={cuda}"
        if channel in ("cu126", "cu118"):
            build_ok = f"+{channel}" in ver
            problems = []
            if not build_ok:
                problems.append(f"版本构建应为 +{channel}")
            if not cuda:
                problems.append("torch.cuda.is_available()=False")
            rows.append({"name": "torch.cuda", "required": required_cuda,
                         "installed": installed, "status": "ok" if not problems else "fail",
                         "detail": "CUDA 构建与机器画像一致，GPU 加速可用" if not problems
                                   else "；".join(problems)})
        elif channel == "cpu":
            cpu_ok = not cuda and "+cu" not in ver
            rows.append({"name": "torch.cuda", "required": "CPU 构建（机器画像）",
                         "installed": installed, "status": "ok" if cpu_ok else "fail",
                         "detail": "与 CPU 机器画像一致" if cpu_ok else "与 CPU 机器画像不一致"})
        else:
            rows.append({"name": "torch.cuda", "required": required_cuda,
                         "installed": installed, "status": "ok" if cuda else "warn",
                         "detail": "GPU 加速可用" if cuda else
                                   "无 GPU 加速（demucs 走 CPU）；无 torch_channel 画像，未强制 CUDA build"})
    else:
        rows.append({"name": "torch.cuda", "required": required_cuda, "installed": "-",
                     "status": "warn", "detail": "子进程检查失败: " + (out or "")[:200]})
    # ffmpeg / ffprobe 都实际执行 -version，缺失项也列入全量结果
    try:
        import export_config as ec
        binaries = (("ffmpeg", ec.get_ffmpeg()), ("ffprobe", ec.get_ffprobe()))
    except Exception:
        binaries = (("ffmpeg", ""), ("ffprobe", ""))
    for name, exe in binaries:
        if not exe or not os.path.isfile(exe):
            rows.append({"name": name, "required": "可运行", "installed": "-",
                         "status": "fail", "detail": f"未找到: {exe or '(空)'}"})
            continue
        rc, out = _run([exe, "-version"], timeout=15)
        first = (out or "").strip().splitlines()[0] if out.strip() else ""
        rows.append({"name": name, "required": "可运行", "installed": first or "-",
                     "status": "ok" if rc == 0 else "fail", "detail": exe})
    # Playwright 不只找目录：子进程真实启动 Chromium 并创建本地页面，不访问网络。
    browser_code = (
        "from playwright.sync_api import sync_playwright\n"
        "p=sync_playwright().start()\n"
        "b=p.chromium.launch(headless=True)\n"
        "page=b.new_page()\n"
        "page.set_content('<title>console-check</title>')\n"
        "print(page.title())\n"
        "b.close()\n"
        "p.stop()\n"
    )
    rc, out = _run([pyexe, "-c", browser_code], timeout=60)
    browser_ok = rc == 0 and "console-check" in (out or "")
    rows.append({"name": "playwright.chromium", "required": "实际启动并创建页面",
                 "installed": "启动实测通过" if browser_ok else "-",
                 "status": "ok" if browser_ok else "warn",
                 "detail": "仅网页录制受影响；未访问网络" if browser_ok else
                           "Chromium 启动失败: " + (out or "")[:300]})
    # pytest：只用于开发/验证跑 tests/，缺失不判 fail，避免终端用户误以为环境坏了
    try:
        pv = importlib.metadata.version("pytest")
        rows.append({"name": "pytest", "required": ">=8.0（可选·仅测试）", "installed": pv,
                     "status": "ok", "detail": "开发/验证跑 tests/ 需要，终端用户可忽略"})
    except importlib.metadata.PackageNotFoundError:
        rows.append({"name": "pytest", "required": ">=8.0（可选·仅测试）", "installed": "-",
                     "status": "warn",
                     "detail": "未安装，不影响运行；跑 tests/ 时执行 pip install -r requirements-dev.txt 或 install_deps.ps1 -IncludeTest"})
    # VC++ Redistributable：onnxruntime / torch 的运行库，用当前 Python 真实加载 DLL
    rc, out = _run([pyexe, "-c",
                    "import ctypes;"
                    "ctypes.WinDLL('vcruntime140.dll');"
                    "ctypes.WinDLL('vcruntime140_1.dll');"
                    "print('VC_OK')"], timeout=20)
    vc_ok = rc == 0 and "VC_OK" in (out or "")
    rows.append({"name": "VC++ Redistributable x64", "required": "vcruntime140 / 140_1 可加载",
                 "installed": "可加载" if vc_ok else "-",
                 "status": "ok" if vc_ok else "warn",
                 "detail": "onnxruntime（UI OCR）/ torch 的运行库" if vc_ok else
                           "缺失或不可加载：AI/OCR 链路会起不来。运行 "
                           "pipeline\\install_deps_admin.ps1（Console 安装时会自动提权处理）"})
    # MediaInfo DLL：只有 vendor 的 local_materials 用 pymediainfo，且自带 ffprobe 回退
    rc, out = _run([pyexe, "-c",
                    "import pymediainfo;"
                    "print('MI_OK' if pymediainfo.MediaInfo.can_parse() else 'MI_NO_DLL')"],
                   timeout=20)
    text = out or ""
    if "MI_OK" in text:
        mi_status, mi_detail = "ok", "pymediainfo 可解析"
    elif "MI_NO_DLL" in text:
        mi_status, mi_detail = "warn", ("pymediainfo 可导入但找不到 MediaInfo DLL；"
                                        "vendor 脚本会自动回退 ffprobe，不影响主链路")
    else:
        mi_status, mi_detail = "warn", "pymediainfo 不可用；vendor 脚本回退 ffprobe（不影响主链路）"
    rows.append({"name": "MediaInfo DLL", "required": "可选（有 ffprobe 回退）",
                 "installed": "可解析" if mi_status == "ok" else "-",
                 "status": mi_status, "detail": mi_detail})
    # ollama：核对 presets 配置的兜底模型，而不是把任意模型视为通过
    ollama_cfg = (_cfg().get("translate") or {}).get("ollama") or {}
    expected_model = str(ollama_cfg.get("model") or "").strip()
    required_model = expected_model or "至少 1 个"
    exe = shutil.which("ollama") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    if os.path.isfile(exe):
        rc, out = _run([exe, "list"], timeout=20)
        models = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip()] if rc == 0 else []
        model_ok = expected_model in models if expected_model else bool(models)
        rows.append({"name": "ollama 模型", "required": required_model,
                     "installed": "、".join(models) or "无",
                     "status": "ok" if model_ok else "warn",
                     "detail": "智谱失败时的本地翻译兜底" if model_ok else
                               (out.strip()[:200] if rc != 0 else "未安装配置指定的兜底模型")})
    else:
        rows.append({"name": "ollama 模型", "required": required_model, "installed": "-",
                     "status": "warn", "detail": "ollama 未安装"})
    # 模型目录
    try:
        import export_config as ec
        models_dir = os.path.join(ec.get_deps_root(), "models")
        has = os.path.isdir(models_dir)
        rows.append({"name": "模型缓存目录", "required": "存在",
                     "installed": models_dir, "status": "ok" if has else "warn",
                     "detail": "ASR/分离模型缓存（首次使用会自动下载）"})
    except Exception:
        pass
    summary = {"ok": sum(1 for r in rows if r["status"] == "ok"),
               "warn": sum(1 for r in rows if r["status"] == "warn"),
               "fail": sum(1 for r in rows if r["status"] == "fail")}
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.executable, "rows": rows, "summary": summary}


def full_report(host="127.0.0.1", port=8765):
    """一次完成环境、依赖和运行检查；Console 只暴露这一个检查入口。"""
    t0 = time.time()
    inv = inventory()
    dependencies = deps_report()
    # console/doctor.py 是共享核心 doctor_core 的适配层；pipeline/doctor.py 是同一核心的 CLI。
    runtime = runtime_doctor.run_all(host, port)
    inv_actions = {it["id"]: it.get("actions") or []
                   for g in inv["groups"] for it in g["items"]}
    inv_ids = {it["id"] for g in inv["groups"] for it in g["items"]}
    # 盘点项 id 与共享核心 key 的对齐表（同一件事只显示一次，留在原分组）
    core_aliases = {"jy_install": "jianying_install", "jy_session": "jianying_session"}
    core_by_key = {}
    for c in runtime["checks"]:
        core_by_key[c["key"]] = c
    # 同 key 以共享核心为准（判定与 CLI 完全一致），但**留在原分组位置**：
    # 只替换该行，不把整项搬去「运行状态」，避免完整检查前后分组内容对不上。
    used_core = set()
    groups = []
    for g in inv["groups"]:
        items = []
        for it in g["items"]:
            c = core_by_key.get(it["id"]) or core_by_key.get(core_aliases.get(it["id"], ""))
            if c:
                used_core.add(c["key"])
                items.append(_item(c["key"], c["name"], c["status"], c["detail"],
                                   c.get("fix") or "", actions=it.get("actions") or ()))
            else:
                items.append(it)
        groups.append({"key": g["key"], "title": g["title"], "items": items})
    groups = [g for g in groups if g["items"]]
    # 盘点里没有对应项的共享检查，才单独归到「运行状态」；
    # path:* 与 projects/series/archive/export 已由「路径」组逐项给出（存在+可写+剩余空间），
    # Python 依赖由 pip_core/pip_ai 给出，这里不再重复。
    _dup_keys = {"deps", "projects", "series", "archive", "export"}
    extra_items = [
        _item(c["key"], c["name"], c["status"], c["detail"], c.get("fix") or "",
              actions=inv_actions.get(c["key"], ()))
        for c in runtime["checks"]
        if c["key"] not in used_core
        and c["key"] not in inv_ids and c["key"] not in _dup_keys
        and not c["key"].startswith("path:")
    ]
    if extra_items:
        # 运行态检查并进「运行环境和状态」同一张卡，胶囊名与卡片标题保持一致
        for g in groups:
            if g["key"] == "deps":
                g["items"].extend(extra_items)
                break
        else:
            groups.append({"key": "runtime", "title": "运行环境和状态",
                           "items": extra_items})
    summary = {"ok": 0, "warn": 0, "fail": 0}
    for group in groups:
        for item in group["items"]:
            summary[item["status"] if item["status"] in summary else "warn"] += 1
    for row in dependencies["rows"]:
        summary[row["status"] if row["status"] in summary else "warn"] += 1
    return {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": round(time.time() - t0, 2), "groups": groups,
            "dependencies": dependencies, "summary": summary}


# ── presets.json：校验 / 备份保存 / 从模板创建 ──────────────────────
def validate_presets(cfg):
    """schema 校验：有 error 一律不许写盘；warn 只提示。"""
    errors, warnings = [], []
    if not isinstance(cfg, dict):
        return {"errors": ["配置必须是 JSON 对象"], "warnings": []}

    for k in cfg:
        if k not in KNOWN_TOP_KEYS and not str(k).lower().startswith("presets_"):
            warnings.append(f"未知顶层键 {k}（已保留，但程序不会读取它）")

    for k in ("series_root", "export_dir", "deps_root"):
        v = cfg.get(k)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"必填项 {k} 为空")

    for k in PATH_KEYS:
        v = cfg.get(k)
        if v in (None, ""):
            continue
        if not isinstance(v, str):
            errors.append(f"{k} 必须是字符串")
            continue
        p = v.strip()
        if "<" in p or ">" in p or "\x00" in p:
            errors.append(f"{k} 含非法字符")
        if ".." in p:
            errors.append(f"{k} 不能包含 ..")
        if not re.match(r"^[A-Za-z]:[\\/]", p) and not p.startswith("\\\\"):
            errors.append(f"{k} 必须是绝对路径: {p}")
        elif not os.path.isdir(p):
            if k in ("series_root", "export_dir", "deps_root"):
                errors.append(f"{k} 目录不存在: {p}")
            else:
                warnings.append(f"{k} 目录不存在: {p}")

    groups = [k for k in cfg
              if str(k).lower().startswith("presets") and isinstance(cfg.get(k), list)]
    for g in groups:
        for i, entry in enumerate(cfg[g]):
            if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
                errors.append(f"{g}[{i}] 缺少 name")
                continue
            alpha = entry.get("alpha")
            if alpha is not None:
                try:
                    a = float(alpha)
                    if not (0 <= a <= 1):
                        errors.append(f"{g}[{i}].alpha 必须在 0~1")
                except (TypeError, ValueError):
                    errors.append(f"{g}[{i}].alpha 必须是数字")

    default = str(cfg.get("default_preset_profile") or "").strip()
    if default:
        key = default if default in cfg else f"presets_{default}"
        if key not in cfg:
            errors.append(f"default_preset_profile={default} 指向不存在的组")
    elif not groups:
        warnings.append("未配置任何预设组，成片将没有预设层")

    watch = cfg.get("watch")
    if watch is not None and not isinstance(watch, dict):
        errors.append("watch 必须是对象")
    if isinstance(watch, dict):
        roots = watch.get("roots")
        if roots is not None:
            if not isinstance(roots, list):
                errors.append("watch.roots 必须是数组（额外 watcher 扫描根）")
            else:
                seen = set()
                for r in roots:
                    if not isinstance(r, str) or not r.strip():
                        errors.append("watch.roots 含空项")
                        continue
                    p = r.strip()
                    key = os.path.normcase(os.path.abspath(p))
                    if key in seen:
                        warnings.append(f"watch.roots 重复项: {p}")
                        continue
                    seen.add(key)
                    if not os.path.isdir(p):
                        errors.append(f"watch.roots 目录不存在: {p}")
                        continue
                    try:
                        has_job = any(os.path.isdir(os.path.join(e.path, "cover_job"))
                                      for e in os.scandir(p) if e.is_dir())
                    except OSError as exc:
                        errors.append(f"watch.roots 无法读取: {p}（{exc}）")
                        continue
                    if not has_job:
                        errors.append(f"watch.roots 目录下没有 */cover_job: {p}")

    tr = cfg.get("translate")
    if tr is not None and not isinstance(tr, dict):
        errors.append("translate 必须是对象")
    if isinstance(tr, dict):
        providers = tr.get("providers")
        if providers is not None:
            if not isinstance(providers, list):
                errors.append("translate.providers 必须是数组")
            else:
                for i, p in enumerate(providers):
                    if not isinstance(p, dict) or not p.get("name"):
                        errors.append(f"translate.providers[{i}] 缺少 name")
                    elif p.get("name") == "zhipu":
                        key = str(p.get("api_key") or "")
                        if not key or key.startswith("<"):
                            warnings.append("智谱 api_key 未填写，英文字幕将降级到本地模型")

    jd = str(cfg.get("jianying_dir") or "").strip()
    if jd and REQUIRED_JY_VERSION not in jd:
        warnings.append(f"jianying_dir 未包含版本号 {REQUIRED_JY_VERSION}")

    ad = cfg.get("admission")
    if ad is not None and not isinstance(ad, dict):
        errors.append("admission 必须是对象")
    if isinstance(ad, dict) and ad and not str(ad.get("draft_name") or "").strip():
        warnings.append("admission.draft_name 为空，UI 自动剪辑默认草稿名推导会失败")

    return {"errors": errors, "warnings": warnings}


def _prune_backups(keep=10):
    d = os.path.dirname(PRESETS_PATH)
    try:
        baks = [os.path.join(d, n) for n in os.listdir(d)
                if n.startswith("presets.json.bak-")]
        baks.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in baks[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def save_presets(cfg):
    """校验通过才写：先备份（保留 10 份），再临时文件 + 原子替换。"""
    rep = validate_presets(cfg)
    if rep["errors"]:
        raise ValueError("；".join(rep["errors"]))
    backup = ""
    if os.path.isfile(PRESETS_PATH):
        # 同秒多次保存也不能互相覆盖：时间戳 + 短随机后缀
        backup = (PRESETS_PATH + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
                  + "-" + uuid.uuid4().hex[:4])
        shutil.copy2(PRESETS_PATH, backup)
    tmp = PRESETS_PATH + ".tmp-" + uuid.uuid4().hex[:6]
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, PRESETS_PATH)
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    _prune_backups(keep=10)
    return {"errors": [], "warnings": rep["warnings"], "backup": backup,
            "path": PRESETS_PATH}


def init_presets():
    if os.path.isfile(PRESETS_PATH):
        raise ValueError("presets.json 已存在，不会覆盖")
    if not os.path.isfile(PRESETS_EXAMPLE):
        raise ValueError("缺少模板 presets.example.json")
    shutil.copy2(PRESETS_EXAMPLE, PRESETS_PATH)
    return {"path": PRESETS_PATH}


def migrate_config(series_root="", write=False):
    """把旧配置的 watch.roots 一次性迁移为 series_root。

    只做确定性迁移：series_root 为空且 watch.roots 恰好一个有效目录时写入该目录；
    多个根必须显式给 series_root，不猜。export_dir/deps_root 缺失只报告，不猜。
    """
    cfg, err = _read_json(PRESETS_PATH)
    if err or not isinstance(cfg, dict):
        raise ValueError(f"无法读取 {PRESETS_PATH}: {err or '内容不是 JSON 对象'}")
    changes = {}
    current = str(cfg.get("series_root") or "").strip()
    if not current:
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
                             "请显式指定 series_root（多个根不能自动猜）")
    missing = [k for k in ("export_dir", "deps_root")
               if not str(cfg.get(k) or "").strip()]
    if not write:
        return {"written": False, "changes": changes, "missing": missing,
                "path": PRESETS_PATH}
    if missing:
        raise ValueError("迁移前还需要填写: " + "、".join(missing))
    merged = dict(cfg)
    merged.update(changes)
    rep = save_presets(merged)
    return {"written": True, "changes": changes, "missing": [],
            "path": PRESETS_PATH, "backup": rep.get("backup", ""),
            "warnings": rep.get("warnings", [])}


def read_presets():
    raw = ""
    if os.path.isfile(PRESETS_PATH):
        with open(PRESETS_PATH, encoding="utf-8") as f:
            raw = f.read()
    cfg, err = _read_json(PRESETS_PATH)
    return {"exists": os.path.isfile(PRESETS_PATH), "path": PRESETS_PATH,
            "raw": raw, "config": cfg if isinstance(cfg, dict) else {},
            "parse_error": err}


# ── 目录创建（只建配置里的目录，不建剪映草稿根）─────────────────────
def ensure_dirs():
    import export_config as ec
    targets = []
    snap = ec.resolve_paths("console")["paths"]
    # 只创建业务根；watch.roots 是额外扫描根，必须由用户放入含 cover_job 的剧集目录，
    # 这里绝不能创建空目录（否则下一轮校验会因“没有 */cover_job”报错）。
    for key in ("series_root", "export_dir", "archive_root", "deps_root"):
        value = snap.get(key)
        if isinstance(value, str) and value.strip():
            targets.append(value)
    created, skipped = [], []
    for t in dict.fromkeys(x for x in targets if isinstance(x, str) and x.strip()):
        p = os.path.abspath(t.strip())
        if not re.match(r"^[A-Za-z]:[\\/]", p):
            skipped.append({"path": p, "reason": "不是本机绝对路径"})
            continue
        if os.path.isdir(p):
            skipped.append({"path": p, "reason": "已存在"})
            continue
        try:
            os.makedirs(p, exist_ok=True)
            created.append(p)
        except OSError as e:
            skipped.append({"path": p, "reason": str(e)})
    return {"created": created, "skipped": skipped}


# ── 计划任务（显式动作才注册）────────────────────────────────────────
def register_task(enable=True):
    script = os.path.join(paths.REPO, "pipeline", "watcher_supervisor.ps1")
    if not os.path.isfile(script):
        raise ValueError(f"缺少 {script}")
    tr = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}"'
    rc, out = _run(["schtasks", "/Create", "/TN", WATCHER_TASK_NAME,
                    "/SC", "MINUTE", "/MO", "5", "/TR", tr, "/F"], timeout=30)
    if rc != 0:
        raise ValueError("注册计划任务失败: " + out.strip())
    if enable:
        _run(["schtasks", "/Change", "/TN", WATCHER_TASK_NAME, "/ENABLE"], timeout=20)
    return task_status()


def redetect_jianying():
    import jianying_launch as jl
    found = []
    for src, exe in jl.jianying_candidates():
        if exe and os.path.isfile(exe):
            found.append({"source": src, "exe": exe, "dir": os.path.dirname(exe)})
    return {"candidates": found, "required_version": REQUIRED_JY_VERSION}


# ── 全量安装 job（管理员 PowerShell，实时输出，可中断）───────────────
_JOBS = {}
_JOBS_LOCK = threading.Lock()

_WRAPPER = r"""$ErrorActionPreference = 'Continue'
# 日志编码统一为中文（系统 ANSI / GBK）：
# PowerShell 重定向输出本身就是 ANSI，Python 侧强制同编码，避免中英混写乱码。
$env:PYTHONUTF8 = '0'
$env:PYTHONIOENCODING = 'gbk:replace'
$repo = '__REPO__'
$jobDir = '__JOBDIR__'
$main = Join-Path $repo 'pipeline\install_deps.ps1'
$admin = Join-Path $repo 'pipeline\install_deps_admin.ps1'
$stdout = Join-Path $jobDir 'stdout.log'
$stderr = Join-Path $jobDir 'stderr.log'
$exitFile = Join-Path $jobDir 'exit'
$stopFlag = Join-Path $jobDir 'stop.flag'
$adminLog = Join-Path $jobDir 'admin.log'
$detectLog = Join-Path $jobDir 'detect.log'
function Invoke-Child([string]$file, [string[]]$extra, [string]$outFile) {
  $argLine = '-NoProfile -ExecutionPolicy Bypass -File "' + $file + '"'
  if ($extra) { $argLine = $argLine + ' ' + ($extra -join ' ') }
  $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $argLine `
      -WorkingDirectory $repo -RedirectStandardOutput $outFile `
      -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
  while ($true) {
    if (Test-Path $stopFlag) {
      & taskkill /PID $p.Id /T /F 2>$null | Out-Null
      return @{ stopped = $true; code = 0 }
    }
    if ($p.HasExited) { break }
    Start-Sleep -Seconds 2
    $p.Refresh()
  }
  # PowerShell 5.1 的 Start-Process -PassThru 在重定向输出时 ExitCode 常为 $null，
  # 直接 [string]$null 会写出空 exit 文件，前端就会一直判定「安装中」。
  # 拿不到退出码时用安装脚本末尾的收尾标记兜底。
  $code = $null
  try { $p.Refresh(); $code = $p.ExitCode } catch { $code = $null }
  if ($null -eq $code) {
    $tailTxt = ''
    if (Test-Path $outFile) {
      $tailTxt = Get-Content -Path $outFile -Raw -Encoding Default -ErrorAction SilentlyContinue
    }
    if ($tailTxt -match '\[OK\] Done!') { $code = 0 } else { $code = 1 }
  }
  return @{ stopped = $false; code = $code }
}
try {
  Write-Host '===== 1/3 环境探测（普通权限，-DetectOnly）====='
  $r1 = Invoke-Child $main @('-DetectOnly') $detectLog
  Write-Host ('探测退出码: ' + $r1.code)
  if (Test-Path $detectLog) { Get-Content -Path $detectLog -Encoding Default | Write-Host }
  if ($r1.stopped) { Set-Content -Path $exitFile -Value 'stopped' -Encoding ascii; exit 0 }

  Write-Host '===== 2/3 管理员前置项（长路径 / VC++ / ffmpeg）====='
  $probe = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $admin -DetectOnly 2>&1 | Out-String
  Write-Host $probe
  if ($probe -notmatch 'ADMIN_NEEDED=0') {
    Write-Host '检测到需要管理员的项，弹出一次 UAC …'
    $inner = "& '" + $admin + "' *> '" + $adminLog + "'"
    $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -PassThru -Wait `
        -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',$inner
    if (Test-Path $adminLog) { Get-Content -Path $adminLog | Write-Host }
    Write-Host ('管理员批处理退出码: ' + $p.ExitCode)
  } else {
    Write-Host '无管理员项，跳过提权'
  }

  Write-Host '===== 3/3 主安装（普通权限）====='
  $r3 = Invoke-Child $main @() $stdout
  if ($r3.stopped) { Set-Content -Path $exitFile -Value 'stopped' -Encoding ascii }
  else { Set-Content -Path $exitFile -Value ([string]$r3.code) -Encoding ascii }
} catch {
  Set-Content -Path $exitFile -Value ('wrapper-error: ' + $_.Exception.Message) -Encoding utf8
}
"""


def _tail(path, lines=400):
    """读日志尾部。子进程混写 UTF-8 / GBK（PowerShell 中文输出），
    逐行回退解码，避免整页乱码。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    out = []
    for raw in data.splitlines(True):
        for enc in ("utf-8", "gbk"):
            try:
                out.append(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        else:
            out.append(raw.decode("utf-8", "replace"))
    return "".join(out[-lines:])


def _job_view(job):
    d = job["dir"]
    exit_file = os.path.join(d, "exit")
    has_exit = os.path.isfile(exit_file)
    exit_val = ""
    if has_exit:
        try:
            with open(exit_file, encoding="utf-8", errors="replace") as f:
                exit_val = f.read().strip()
        except OSError:
            exit_val = ""
    log = (_tail(os.path.join(d, "detect.log")) +
           _tail(os.path.join(d, "admin.log")) +
           _tail(os.path.join(d, "stdout.log")) +
           _tail(os.path.join(d, "stderr.log")))
    if not has_exit:
        status = "running"
    elif not exit_val:
        # 历史 job：wrapper 已收尾但退出码没写进来（Start-Process 的 ExitCode
        # 已知缺陷），用安装脚本的收尾标记判定，避免永远显示「安装中」。
        status = "done" if "[OK] Done!" in log else "error"
    elif exit_val == "stopped":
        status = "stopped"
    elif exit_val == "0":
        status = "done"
    else:
        status = "error"
    return {"id": job["id"], "kind": job["kind"],
            "status": status,
            "exit": exit_val, "started_at": job["started_at"],
            "log": log, "dir": d}


def is_busy():
    with _JOBS_LOCK:
        job = _JOBS.get("current")
    if not job:
        return False
    return not os.path.isfile(os.path.join(job["dir"], "exit"))


def _latest_finished_job():
    """Console 重启后内存里的 _JOBS 会清空，回退展示最近一次已收尾的安装记录，
    避免刷新页面后看不到上一次安装的结果。"""
    try:
        names = [n for n in os.listdir(paths.ENV_JOB_DIR)
                 if os.path.isdir(os.path.join(paths.ENV_JOB_DIR, n))]
    except OSError:
        return None
    best = None
    for n in names:
        d = os.path.join(paths.ENV_JOB_DIR, n)
        if not os.path.isfile(os.path.join(d, "exit")):
            continue
        try:
            mt = os.path.getmtime(d)
        except OSError:
            continue
        if best is None or mt > best[0]:
            best = (mt, n, d)
    if not best:
        return None
    started = ""
    if len(best[1]) >= 15:
        started = (f"{best[1][0:4]}-{best[1][4:6]}-{best[1][6:8]} "
                   f"{best[1][9:11]}:{best[1][11:13]}:{best[1][13:15]}")
    return {"id": best[1], "kind": "full", "dir": best[2], "started_at": started}


def job_status():
    with _JOBS_LOCK:
        job = _JOBS.get("current")
    if not job:
        job = _latest_finished_job()
        if not job:
            return {"status": "idle"}
    return _job_view(job)


def start_install(kind="full"):
    """启动全量安装：普通权限探测 → 按需一次 UAC 提权 → 普通权限主安装。

    提权只发生在 install_deps_admin.ps1（长路径 / VC++ / 缺失 ffmpeg），
    其余 pip / 模型 / Chromium / Ollama 全部在普通权限下完成。
    """
    if kind != "full":
        raise ValueError("未知的安装类型")
    with _JOBS_LOCK:
        if _JOBS.get("current") and not os.path.isfile(
                os.path.join(_JOBS["current"]["dir"], "exit")):
            raise ValueError("已有安装任务在跑")
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = os.path.join(paths.ENV_JOB_DIR, job_id)
        os.makedirs(d, exist_ok=True)
        wrapper = os.path.join(d, "wrapper.ps1")
        content = (_WRAPPER.replace("__REPO__", paths.REPO.replace("'", "''"))
                           .replace("__JOBDIR__", d.replace("'", "''")))
        with open(wrapper, "w", encoding="utf-8-sig") as f:
            f.write(content)
        job = {"id": job_id, "kind": kind, "dir": d,
               "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _JOBS["current"] = job

    ps_wrapper = wrapper.replace("'", "''")
    launcher = (
        "$w = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden "
        "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\""
        + ps_wrapper + "\"' -PassThru; $w.Id"
    )
    rc, out = _run(["powershell", "-NoProfile", "-Command", launcher], timeout=40)
    if rc != 0:
        # 启动失败：写 exit，便于前端看到失败（提权失败会在 wrapper 的 2/3 阶段体现）
        with open(os.path.join(d, "exit"), "w", encoding="utf-8") as f:
            f.write("launch-failed")
        raise ValueError("安装进程启动失败: " + out.strip())
    return _job_view(job)


def stop_install():
    with _JOBS_LOCK:
        job = _JOBS.get("current")
    if not job:
        return {"stopped": False, "reason": "没有安装任务"}
    flag = os.path.join(job["dir"], "stop.flag")
    with open(flag, "w", encoding="utf-8") as f:
        f.write("stop")
    return {"stopped": True, "id": job["id"]}
