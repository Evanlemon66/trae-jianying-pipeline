# -*- coding: utf-8 -*-
"""pipeline 公共配置：唯一真源是 pipeline/presets.json，代码里不写死业务路径。

路径解析规则（三根统一后）：
  环境变量（显式覆盖）> presets.json 对应字段 > 系统派生根自动探测 > 报错

关键约束：
  - 业务根 series_root / export_dir / deps_root 必须显式配置；缺失或无效直接抛 ConfigError。
  - archive_root 可留空表示禁用归档；配置了但无效同样抛 ConfigError。
  - watch.roots 只是 watcher 的“额外扫描根”，不参与 series_root 解析；
    有效扫描根 = [series_root] + watch.roots（去重），额外根必须存在且含 */cover_job。
  - 系统派生根（剪映安装/草稿根/预设库/日志）允许自动探测，但必须显示来源；探测失败报错。

新机器/新用户：复制 pipeline/presets.example.json 为 pipeline/presets.json，填好自己的路径。
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "presets.json")

# 系统派生根只允许来自 LOCALAPPDATA / 注册表探测，不再保留任何开发机字面量。
# UI 自动化 OCR 依赖（rapidocr 等）与依赖根同层，源码目录不再自带 pylibs

# 同一条提示每进程只打一次，避免 watcher 每轮轮询刷屏
_WARNED = set()


class ConfigError(ValueError):
    """配置缺失或无效：带 key、当前值与修复提示。"""

    def __init__(self, key, message, hint=""):
        self.key = key
        self.hint = hint
        text = f"{message}"
        if hint:
            text += f"；{hint}"
        super().__init__(text)


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


def _hint_for(config_key, env_name):
    return (f"请在 pipeline/presets.json 填写 {config_key}，"
            f"或设置环境变量 {env_name}（必须指向有效目录）")


def _resolve_required_dir(config_key, env_name, label):
    """业务输入根：env > presets.json > 报错；无效配置不再回退。"""
    env_value = (os.environ.get(env_name) or "").strip()
    if env_value:
        path = _expand(env_value)
        if os.path.isdir(path):
            return path, "env"
        raise ConfigError(config_key, f"{env_name}={env_value} 不是可用的{label}",
                          _hint_for(config_key, env_name))
    cfg_value = _cfg_text(config_key)
    if cfg_value:
        path = _expand(cfg_value)
        if os.path.isdir(path):
            return path, "presets.json"
        raise ConfigError(config_key,
                          f"pipeline/presets.json 的 {config_key}={cfg_value} 不是可用的{label}",
                          _hint_for(config_key, env_name))
    raise ConfigError(config_key, f"未配置{label}",
                      _hint_for(config_key, env_name))


def _resolve_output_root(config_key, env_name, label):
    """业务输出根：env > presets.json > 报错；允许尚不存在（首次使用时创建）。"""
    env_value = (os.environ.get(env_name) or "").strip()
    if env_value:
        path = _expand(env_value)
        if not os.path.isdir(path):
            _warn(f"{env_name}={env_value} 指向的{label}尚不存在，将在首次使用时创建")
        return path, "env"
    cfg_value = _cfg_text(config_key)
    if cfg_value:
        path = _expand(cfg_value)
        if not os.path.isdir(path):
            _warn(f"pipeline/presets.json 的 {config_key}={cfg_value} 指向的{label}尚不存在，将在首次使用时创建")
        return path, "presets.json"
    raise ConfigError(config_key, f"未配置{label}",
                      _hint_for(config_key, env_name))


def _resolve_dir(config_key, env_name, default, candidates=(), label="目录", allow_missing=False):
    """系统派生根：env > presets.json > 自动探测 > 派生默认；显式配置无效直接报错。"""
    env_value = (os.environ.get(env_name) or "").strip()
    if env_value:
        path = _expand(env_value)
        if os.path.isdir(path) or allow_missing:
            return path, "env"
        raise ConfigError(config_key, f"{env_name}={env_value} 不是可用的{label}",
                          _hint_for(config_key, env_name))
    cfg_value = _cfg_text(config_key)
    if cfg_value:
        path = _expand(cfg_value)
        if os.path.isdir(path) or allow_missing:
            return path, "presets.json"
        raise ConfigError(config_key,
                          f"pipeline/presets.json 的 {config_key}={cfg_value} 不是可用的{label}",
                          _hint_for(config_key, env_name))
    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return _expand(candidate), "autodetect"
    if default:
        return _expand(default), "system"
    raise ConfigError(config_key, f"未能定位{label}",
                      _hint_for(config_key, env_name))


def _resolve_target(config_key, env_name, label="目录"):
    """输出型路径：env > presets.json > 报错（不要求已存在）。"""
    return _resolve_output_root(config_key, env_name, label)[0]


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
    """成品导出根目录：JY_EXPORT_DIR > presets.json export_dir；未配置报错。"""
    return _resolve_target("export_dir", "JY_EXPORT_DIR", "成品导出目录")


def get_deps_root():
    """统一依赖根目录：JY_DEPS_ROOT > presets.json deps_root；未配置报错。"""
    return _resolve_dir("deps_root", "JY_DEPS_ROOT", None, label="依赖根目录")[0]


def get_jianying_dir():
    """剪映安装目录：JY_JIANYING_DIR > presets.json jianying_dir > 注册表探测；都失败报错。"""
    return _resolve_dir("jianying_dir", "JY_JIANYING_DIR", None,
                        candidates=_autodetect_jianying_dirs(), label="剪映安装目录")[0]


def get_projects_root():
    """剪映草稿根目录：JY_PROJECTS_ROOT > presets.json projects_root > 系统派生根。"""
    return _resolve_dir("projects_root", "JY_PROJECTS_ROOT", DEFAULT_PROJECTS_ROOT,
                        candidates=_projects_candidates(), label="剪映草稿根目录",
                        allow_missing=True)[0]


def get_presets_root():
    """剪映组合预设库：JY_PRESETS_ROOT > presets.json presets_root > 系统派生根。"""
    return _resolve_dir("presets_root", "JY_PRESETS_ROOT", DEFAULT_PRESETS_ROOT,
                        candidates=_presets_candidates(), label="剪映预设库",
                        allow_missing=True)[0]


def get_jianying_log_dir():
    """剪映日志目录：JY_JIANYING_LOG_DIR > presets.json jianying_log_dir > 系统派生根。"""
    return _resolve_dir("jianying_log_dir", "JY_JIANYING_LOG_DIR", DEFAULT_JIANYING_LOG_DIR,
                        candidates=_log_candidates(), label="剪映日志目录",
                        allow_missing=True)[0]


def _series_root_detail():
    return _resolve_required_dir("series_root", "JY_SERIES_ROOT", "短剧素材根目录")


def get_series_root():
    """短剧素材根目录：JY_SERIES_ROOT > presets.json series_root；不再读 watch.roots。"""
    return _series_root_detail()[0]


def _watch_extra_roots():
    """watch.roots：仅 watcher 额外扫描根；每项必须存在且下面有 */cover_job。"""
    raw = get_watch_config().get("roots")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ConfigError("watch.roots", "watch.roots 必须是目录列表",
                          "请把 presets.json 的 watch.roots 改成 [] 或 [\"<额外素材根>\"]")
    out = []
    seen = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError("watch.roots", f"watch.roots 第 {index} 项为空或不是字符串",
                              "请删除空项，或填写有效的额外素材根")
        path = _expand(item)
        key = os.path.normcase(path)
        if key in seen:
            continue
        if not os.path.isdir(path):
            raise ConfigError("watch.roots", f"watch.roots 第 {index} 项不存在: {path}",
                              "请修正路径，或从 watch.roots 中删除；主素材根请写在 series_root")
        try:
            has_job = any(os.path.isdir(os.path.join(entry.path, "cover_job"))
                          for entry in os.scandir(path) if entry.is_dir())
        except OSError as exc:
            raise ConfigError("watch.roots", f"watch.roots 第 {index} 项无法读取: {path}（{exc}）",
                              "请检查目录权限，或从 watch.roots 中删除")
        if not has_job:
            raise ConfigError("watch.roots",
                              f"watch.roots 第 {index} 项目录下没有 */cover_job: {path}",
                              "watcher 扫不到任务；请放入剧集目录，或从 watch.roots 中删除")
        seen.add(key)
        out.append(path)
    return out


def get_watch_roots():
    """watcher 有效扫描根：[series_root] + watch.roots（去重）。"""
    roots = [get_series_root()]
    seen = {os.path.normcase(roots[0])}
    for path in _watch_extra_roots():
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            roots.append(path)
    return roots


def get_archive_root():
    """归档根：JY_ARCHIVE_ROOT > presets.json archive_root > watch.archive_root（兼容）。

    未配置返回空串，表示禁用归档；显式配置但无效的路径由输出根规则处理（允许尚未创建）。
    """
    env_value = (os.environ.get("JY_ARCHIVE_ROOT") or "").strip()
    if env_value:
        return _expand(env_value)
    cfg_value = _cfg_text("archive_root")
    if cfg_value:
        return _expand(cfg_value)
    legacy = get_watch_config().get("archive_root")
    if isinstance(legacy, str) and legacy.strip():
        return _expand(legacy)
    return ""


def _archive_root_detail():
    """归档根 + 真实来源（Console 展示用）：env > presets.json > watch.archive_root。"""
    env_value = (os.environ.get("JY_ARCHIVE_ROOT") or "").strip()
    if env_value:
        return _expand(env_value), "env:JY_ARCHIVE_ROOT"
    cfg_value = _cfg_text("archive_root")
    if cfg_value:
        return _expand(cfg_value), "presets.json"
    legacy = get_watch_config().get("archive_root")
    if isinstance(legacy, str) and legacy.strip():
        return _expand(legacy), "presets.json:watch.archive_root"
    return "", "未配置"


def resolve_paths(mode="run"):
    """统一路径快照：所有调用方只读这里，不再自行拼路径。

    mode 目前用于标记调用场景（run/export/install/console），不改变解析优先级；
    缺失项不抛出，而是进入 issues，由 doctor/Console 统一展示。
    """
    paths = {}
    sources = {}
    issues = []

    def take(key, getter, source_key=None):
        try:
            value = getter()
            paths[key] = value
            sources[key] = source_key or "derived"
            return value
        except ConfigError as exc:
            paths[key] = ""
            sources[key] = "error"
            issues.append({"key": exc.key, "message": str(exc), "hint": exc.hint})
            return ""

    def take_detail(key, detail):
        try:
            value, source = detail()
            paths[key] = value
            sources[key] = source
            return value
        except ConfigError as exc:
            paths[key] = ""
            sources[key] = "error"
            issues.append({"key": exc.key, "message": str(exc), "hint": exc.hint})
            return ""

    take_detail("series_root", _series_root_detail)
    take_detail("export_dir", lambda: _resolve_output_root(
        "export_dir", "JY_EXPORT_DIR", "成品导出目录"))
    take_detail("deps_root", lambda: _resolve_dir(
        "deps_root", "JY_DEPS_ROOT", None, label="依赖根目录"))
    take_detail("archive_root", _archive_root_detail)
    try:
        paths["watch_extra_roots"] = _watch_extra_roots()
        sources["watch_extra_roots"] = "presets.json" if paths["watch_extra_roots"] else "empty"
    except ConfigError as exc:
        paths["watch_extra_roots"] = []
        sources["watch_extra_roots"] = "error"
        issues.append({"key": exc.key, "message": str(exc), "hint": exc.hint})
    try:
        paths["watch_roots"] = get_watch_roots()
        sources["watch_roots"] = "derived"
    except ConfigError:
        paths["watch_roots"] = [paths.get("series_root")] if paths.get("series_root") else []
    take("jianying_dir", get_jianying_dir)
    take("projects_root", get_projects_root)
    take("presets_root", get_presets_root)
    take("jianying_log_dir", get_jianying_log_dir)
    paths["mode"] = mode
    return {"paths": paths, "sources": sources, "issues": issues}


def validate_config(mode="run"):
    """只读校验：返回 {ok, paths, sources, issues}，不抛异常。"""
    snapshot = resolve_paths(mode)
    return {
        "ok": not snapshot["issues"],
        "paths": snapshot["paths"],
        "sources": snapshot["sources"],
        "issues": snapshot["issues"],
    }


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
