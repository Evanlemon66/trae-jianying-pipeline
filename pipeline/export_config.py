# -*- coding: utf-8 -*-
"""pipeline 公共配置：统一从 pipeline/presets.json 读，不再在各脚本里硬编码。

优先级：presets.json 对应字段 > 内置默认值。
新机器/新用户：复制 pipeline/presets.example.json 为 pipeline/presets.json，改成自己的路径。

可配置项：
  export_dir    成品默认导出根目录。成品按「一部剧一个文件夹」归置：
                  <export_dir>/<草稿名>/<草稿名>.mp4（封面/进度文件同目录）
  deps_root     统一依赖根目录（ffmpeg/ffprobe/模型缓存都在其下）
  jianying_dir  剪映安装目录（字幕字体等内置资源从这儿取）
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_HERE, "presets.json")

# 内置兜底值（presets.json 缺失或字段为空时使用）
DEFAULT_EXPORT_DIR = r"D:\油管打猎\L"
DEFAULT_DEPS_ROOT = r"D:\AI_Deps"
DEFAULT_JIANYING_DIR = r"D:\剪映5.9\剪映5.9版+jianying-skill\剪映5.9(win+mac)\剪映5.9Windows\JianyingPro\5.9.0.11632"


def _cfg():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def get_export_dir():
    """成品导出根目录：presets.json 的 export_dir > 内置默认值。"""
    return (_cfg().get("export_dir") or "").strip() or DEFAULT_EXPORT_DIR


def get_deps_root():
    """统一依赖根目录：presets.json 的 deps_root > 内置默认值。"""
    return (_cfg().get("deps_root") or "").strip() or DEFAULT_DEPS_ROOT


def get_jianying_dir():
    """剪映安装目录：presets.json 的 jianying_dir > 内置默认值。"""
    return (_cfg().get("jianying_dir") or "").strip() or DEFAULT_JIANYING_DIR


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
