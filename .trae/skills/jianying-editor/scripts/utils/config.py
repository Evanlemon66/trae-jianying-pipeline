import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    log_level: str
    cloud_max_mb: float
    tts_insecure_ssl: bool
    projects_root_override: str


def load_config() -> RuntimeConfig:
    return RuntimeConfig(
        log_level=os.getenv("JY_LOG_LEVEL", "INFO"),
        cloud_max_mb=float(os.getenv("JY_CLOUD_MAX_MB", "512")),
        tts_insecure_ssl=os.getenv("JY_TTS_INSECURE_SSL", "0") == "1",
        projects_root_override=os.getenv("JY_PROJECTS_ROOT", "").strip(),
    )


def _presets_jianying_dir() -> str:
    """向上查找 pipeline/presets.json 里的 jianying_dir（可选配置，读不到返回空串）。"""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        d = os.path.dirname(d)
        if not d:
            break
        preset = os.path.join(d, "pipeline", "presets.json")
        if os.path.isfile(preset):
            try:
                with open(preset, encoding="utf-8") as f:
                    return (json.load(f).get("jianying_dir") or "").strip()
            except (OSError, ValueError):
                return ""
    return ""


def resolve_jianying_dir() -> str:
    """剪映安装目录：JY_JIANYING_DIR 环境变量 → presets.json → 注册表 → 常见安装位置 → 空串。"""
    env_dir = os.getenv("JY_JIANYING_DIR", "").strip()
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    cfg_dir = _presets_jianying_dir()
    if cfg_dir and os.path.isdir(cfg_dir):
        return cfg_dir
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        ) as key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                        if "剪映专业版" not in name:
                            continue
                        icon_dir = os.path.dirname(str(winreg.QueryValueEx(sub, "DisplayIcon")[0]))
                        if os.path.isdir(icon_dir):
                            return icon_dir
                except OSError:
                    continue
    except OSError:
        pass
    for root in (os.getenv("PROGRAMFILES"), os.getenv("ProgramFiles(x86)")):
        if root:
            candidate = os.path.join(root, "JianyingPro")
            if os.path.isdir(candidate):
                return candidate
    return ""


CONFIG = load_config()
