# -*- coding: utf-8 -*-
"""pipeline 公共配置：统一从 pipeline/presets.json 读，不再在各脚本里硬编码。

路径统一解析顺序（所有 *路径* 配置都遵守）：
  环境变量 > presets.json 对应字段 > 自动探测 > 内置默认值（最后一层保持仓库现状）

新机器/新用户：复制 pipeline/presets.example.json 为 pipeline/presets.json，改成自己的路径。
不想建配置文件时，也可以只设环境变量（键名见各函数 docstring）。

可配置项：
  export_dir       成品默认导出根目录。成品按「一部剧一个文件夹」归置：
                     <export_dir>/<草稿名>/<草稿名>.mp4（封面/进度文件同目录）
  deps_root        统一依赖根目录（ffmpeg/ffprobe/模型缓存都在其下）
  jianying_dir     剪映安装目录（字幕字体等内置资源从这儿取）
  projects_root    剪映草稿根目录（默认 %LOCALAPPDATA%\\JianyingPro\\User Data\\Projects\\com.lveditor.draft）
  presets_root     剪映组合预设库（默认 %LOCALAPPDATA%\\JianyingPro\\User Data\\Presets\\Combination\\Presets）
  jianying_log_dir 剪映日志目录（启动取证用）
  series_root      短剧素材根目录（默认取 watch.roots[0]，再退回 D:\\短剧）
  archive_root     归档/备份池根目录（默认取 watch.archive_root，再退回 D:\\短剧_存档）
  ui_pylibs        pipeline_ui UI 自动化 OCR 依赖目录（默认 <依赖根>\\ui_pylibs）
  watch            cover_watch 落盘监听的默认参数（roots/jobs/exclude/settle/interval/derive）
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "presets.json")

# 内置兜底值（presets.json 缺失或字段为空时使用；这是本机现有默认，保持不变）
DEFAULT_EXPORT_DIR = r"D:\油管打猎\L"
DEFAULT_DEPS_ROOT = r"D:\AI_Deps"
DEFAULT_JIANYING_DIR = r"D:\剪映5.9\剪映5.9版+jianying-skill\剪映5.9(win+mac)\剪映5.9Windows\JianyingPro\5.9.0.11632"
DEFAULT_SERIES_ROOT = r"D:\短剧"
DEFAULT_ARCHIVE_ROOT = r"D:\短剧_存档"
# UI 自动化 OCR 依赖（rapidocr 等）与依赖根同层，源码目录不再自带 pylibs
DEFAULT_UI_PYLIBS = os.path.join(DEFAULT_DEPS_ROOT, "ui_pylibs")

# 同一条提示每进程只打一次，避免 watcher 每轮轮询刷屏
_WARNED = set()


def _warn(message):
    if message in _WARNED:
        return
    if len(_WARNED) > 256:
        _WARNED.clear()
    _WARNED.add(message)
    print(f"[配置提示] {message}", file=sys.stderr)


def _expand(value):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(value))))


def _cfg():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _cfg_text(key):
    value = _cfg().get(key)
    return value.strip() if isinstance(value, str) else ""


def _resolve_dir(config_key, env_name, default, candidates=(), label="目录"):
    """输入型路径解析：env > presets.json > 自动探测 > 默认。

    显式配置但目录不可用时打一句可读提示并继续下一级，绝不静默使用错误路径。
    自动探测只在候选目录真实存在时命中；最后一层永远返回 default（保持现状）。
    """
    env_value = (os.environ.get(env_name) or "").strip()
    if env_value:
        path = _expand(env_value)
        if os.path.isdir(path):
            return path
        _warn(f"{env_name}={env_value} 不是可用的{label}，已回退到下一级")
    cfg_value = _cfg_text(config_key)
    if cfg_value:
        path = _expand(cfg_value)
        if os.path.isdir(path):
            return path
        _warn(f"pipeline/presets.json 的 {config_key}={cfg_value} 不是可用的{label}，已回退到下一级")
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return _expand(candidate)
    return default


def _resolve_target(config_key, env_name, default, label="目录"):
    """输出型路径解析：env > presets.json > 默认（不要求已存在，缺失只提示不丢弃）。"""
    env_value = (os.environ.get(env_name) or "").strip()
    if env_value:
        path = _expand(env_value)
        if not os.path.isdir(path):
            _warn(f"{env_name}={env_value} 指向的{label}尚不存在，将在首次使用时创建")
        return path
    cfg_value = _cfg_text(config_key)
    if cfg_value:
        path = _expand(cfg_value)
        if not os.path.isdir(path):
            _warn(f"pipeline/presets.json 的 {config_key}={cfg_value} 指向的{label}尚不存在，将在首次使用时创建")
        return path
    return default


def _local_appdata():
    return os.environ.get("LOCALAPPDATA", "")


def _user_profile():
    return os.environ.get("USERPROFILE") or os.path.expanduser("~")


def _default_projects_root():
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Movies", "JianyingPro",
                            "User Data", "Projects", "com.lveditor.draft")
    return os.path.join(_local_appdata(), "JianyingPro",
                        "User Data", "Projects", "com.lveditor.draft")


def _projects_candidates():
    if sys.platform == "darwin":
        home = os.path.expanduser("~")
        return [
            os.path.join(home, "Movies", "JianyingPro", "User Data", "Projects", "com.lveditor.draft"),
            os.path.join(home, "Movies", "JianyingPro Drafts"),
            os.path.join(home, "Library", "Containers", "com.lemon.lvpro", "Data", "Library",
                         "Application Support", "JianyingPro", "User Data", "Projects",
                         "com.lveditor.draft"),
            os.path.join(home, "Library", "Containers", "com.bytedance.JianyingPro", "Data",
                         "Library", "Application Support", "JianyingPro", "User Data",
                         "Projects", "com.lveditor.draft"),
        ]
    local = _local_appdata()
    return [
        os.path.join(local, "JianyingPro", "User Data", "Projects", "com.lveditor.draft"),
        os.path.join(local, "CapCut", "User Data", "Projects", "com.lveditor.draft"),
        os.path.join(_user_profile(), "AppData", "Local", "JianyingPro",
                     "User Data", "Projects", "com.lveditor.draft"),
    ]


def _presets_candidates():
    local = _local_appdata()
    return [
        os.path.join(local, "JianyingPro", "User Data", "Presets", "Combination", "Presets"),
        os.path.join(local, "CapCut", "User Data", "Presets", "Combination", "Presets"),
    ]


def _log_candidates():
    local = _local_appdata()
    return [
        os.path.join(local, "JianyingPro", "User Data", "Log"),
        os.path.join(local, "CapCut", "User Data", "Log"),
    ]


# 默认值在模块加载时定一次：测试与其他调用方直接引用常量，不依赖函数内私有计算。
DEFAULT_PROJECTS_ROOT = _default_projects_root()
DEFAULT_PRESETS_ROOT = _presets_candidates()[0]
DEFAULT_JIANYING_LOG_DIR = _log_candidates()[0]


def _autodetect_jianying_dirs():
    """自动探测剪映安装目录（只读：常见安装位置 + 卸载注册表）。"""
    out = []
    for root in (os.environ.get("PROGRAMFILES"), os.environ.get("ProgramFiles(x86)")):
        if root:
            out.append(os.path.join(root, "JianyingPro"))
    try:
        import winreg
        sub = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | view) as key:
                        for i in range(winreg.QueryInfoKey(key)[0]):
                            try:
                                with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                                    name = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                                    if "剪映专业版" not in name:
                                        continue
                                    try:
                                        icon = (winreg.QueryValueEx(sk, "DisplayIcon")[0] or "").strip('"')
                                        if icon:
                                            out.append(os.path.dirname(icon))
                                    except OSError:
                                        pass
                                    try:
                                        loc = (winreg.QueryValueEx(sk, "InstallLocation")[0] or "").strip()
                                        if loc:
                                            out.append(loc)
                                    except OSError:
                                        pass
                            except OSError:
                                continue
                except OSError:
                    continue
    except Exception:
        pass
    # 安装器目录下一层通常才是版本目录（含 JianyingPro.exe）
    for base in list(out):
        if not os.path.isdir(base):
            continue
        try:
            for name in os.listdir(base):
                candidate = os.path.join(base, name)
                if os.path.isfile(os.path.join(candidate, "JianyingPro.exe")):
                    out.append(candidate)
        except OSError:
            continue
    return out


def get_export_dir():
    """成品导出根目录：JY_EXPORT_DIR > presets.json export_dir > 内置默认值。"""
    return _resolve_target("export_dir", "JY_EXPORT_DIR", DEFAULT_EXPORT_DIR, "成品导出目录")


def get_deps_root():
    """统一依赖根目录：JY_DEPS_ROOT > presets.json deps_root > 内置默认值。"""
    return _resolve_dir("deps_root", "JY_DEPS_ROOT", DEFAULT_DEPS_ROOT,
                        candidates=(DEFAULT_DEPS_ROOT,), label="依赖根目录")


def _ui_pylibs_candidates():
    """UI OCR 依赖目录的自动探测候选：旧仓库内自包含目录、依赖根同层目录。"""
    return [
        os.path.join(os.path.dirname(_HERE), "pipeline_ui", "pylibs"),
        os.path.join(get_deps_root(), "ui_pylibs"),
    ]


def get_ui_pylibs_dir():
    """UI 自动化 OCR 依赖目录：JY_UI_PYLIBS > presets.json ui_pylibs > 自动探测 > 内置默认。

    与其它输入型目录不同：环境变量/presets.json 显式指定的路径即使尚不存在也原样返回，
    便于 install_deps.ps1 首次安装到该位置；doctor 会单独报告"缺失"。
    """
    env_value = (os.environ.get("JY_UI_PYLIBS") or "").strip()
    if env_value:
        return _expand(env_value)
    cfg_value = _cfg_text("ui_pylibs")
    if cfg_value:
        return _expand(cfg_value)
    for candidate in _ui_pylibs_candidates():
        if candidate and os.path.isdir(candidate):
            return _expand(candidate)
    return DEFAULT_UI_PYLIBS


def get_jianying_dir():
    """剪映安装目录：JY_JIANYING_DIR > presets.json jianying_dir > 自动探测 > 内置默认值。"""
    return _resolve_dir("jianying_dir", "JY_JIANYING_DIR", DEFAULT_JIANYING_DIR,
                        candidates=_autodetect_jianying_dirs(), label="剪映安装目录")


def get_projects_root():
    """剪映草稿根目录：JY_PROJECTS_ROOT > presets.json projects_root > 自动探测 > 默认。"""
    return _resolve_dir("projects_root", "JY_PROJECTS_ROOT", DEFAULT_PROJECTS_ROOT,
                        candidates=_projects_candidates(), label="剪映草稿根目录")


def get_presets_root():
    """剪映组合预设库：JY_PRESETS_ROOT > presets.json presets_root > 自动探测 > 默认。"""
    return _resolve_dir("presets_root", "JY_PRESETS_ROOT", DEFAULT_PRESETS_ROOT,
                        candidates=_presets_candidates(), label="剪映预设库")


def get_jianying_log_dir():
    """剪映日志目录：JY_JIANYING_LOG_DIR > presets.json jianying_log_dir > 自动探测 > 默认。"""
    return _resolve_dir("jianying_log_dir", "JY_JIANYING_LOG_DIR", DEFAULT_JIANYING_LOG_DIR,
                        candidates=_log_candidates(), label="剪映日志目录")


def get_series_root():
    """短剧素材根目录：JY_SERIES_ROOT > presets.json series_root > watch.roots[0] > 默认。"""
    roots = []
    try:
        raw = (_cfg().get("watch") or {}).get("roots") or []
        if isinstance(raw, list):
            roots = [str(r).strip() for r in raw if isinstance(r, str) and str(r).strip()]
    except Exception:
        roots = []
    return _resolve_dir("series_root", "JY_SERIES_ROOT", DEFAULT_SERIES_ROOT,
                        candidates=roots, label="短剧素材根目录")


def get_archive_root():
    """归档/备份池根目录：JY_ARCHIVE_ROOT > presets.json archive_root > watch.archive_root > 默认。"""
    watch = get_watch_config()
    candidates = []
    if isinstance(watch.get("archive_root"), str) and watch["archive_root"].strip():
        candidates.append(watch["archive_root"].strip())
    return _resolve_dir("archive_root", "JY_ARCHIVE_ROOT", DEFAULT_ARCHIVE_ROOT,
                        candidates=candidates, label="归档/备份根目录")


def get_watch_config():
    """cover_watch 监听配置：presets.json 的 watch 段（缺失返回 {}）。

    roots 用目录自动发现（扫 <root>/*/cover_job），不必逐个登记任务；
    与 export_dir/deps_root 同在本机配置，脚本内不写死任何路径。
    """
    watch = _cfg().get("watch") or {}
    return watch if isinstance(watch, dict) else {}


def get_admission_config():
    """准入测试草稿配置：presets.json 的 admission 段（缺失返回 {}）。

    草稿名只认这里的 draft_name，代码里不写死；缺失时由调用方直接失败。
    """
    admission = _cfg().get("admission") or {}
    return admission if isinstance(admission, dict) else {}


def preset_profile_name(value):
    """预设组名归一化：presets_a / presets_A / A / a_group → a。

    统一小写：watcher 的版本名与草稿后缀一律 `[a-z][a-z0-9]{0,7}`，
    否则 `--profile B` 会建出 `_B`，和 watcher 期望的 `_b` 形成两套口径。
    """
    s = str(value or "").strip()
    if s.lower().startswith("presets_"):
        s = s[len("presets_"):]
    s = s.strip()
    for suf in ("_group", "-group", "组"):
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
            break
    return s.lower()


def parse_preset_profiles(value):
    """解析多预设组参数，返回去重保序的组名列表。

    支持：'a' / 'a,b' / 'a，b' / 'presets_a,presets_b' / ['a', 'b']
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;，；\s]+", str(value))
    out = []
    for item in items:
        name = preset_profile_name(item)
        if name and name not in out:
            out.append(name)
    return out


def get_ffmpeg():
    return os.path.join(get_deps_root(), "tools", "ffmpeg", "bin", "ffmpeg.exe")


def get_ffprobe():
    return os.path.join(get_deps_root(), "tools", "ffmpeg", "bin", "ffprobe.exe")


def get_font_path():
    """字幕内置字体路径（草稿 JSON 用正斜杠格式）。"""
    return os.path.join(
        get_jianying_dir(), "Resources", "Font", "SystemFont", "zh-hans.ttf"
    ).replace("\\", "/")


def export_bundle_dir(draft_name, export_dir=None):
    """一部剧的成品归置目录：<export_dir>/<草稿名>"""
    return os.path.join(export_dir or get_export_dir(), draft_name)


def export_mp4_path(draft_name, export_dir=None):
    """成品 mp4 目标路径：<export_dir>/<草稿名>/<草稿名>.mp4"""
    return os.path.join(export_bundle_dir(draft_name, export_dir), f"{draft_name}.mp4")
