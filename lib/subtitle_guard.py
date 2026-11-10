# -*- coding: utf-8 -*-
"""subtitle_guard.py — 缺字幕收尾通知（唯一实现）。

整轮剪辑结束后，若 job.json 的 derived.subtitle_missing 非空，只弹一条需人工处理
的通知，列出全部集号；不做逐集通知（避免刷屏）。进程内按 job_dir 去重，重复调用
同一轮不会重弹。watcher 不调用本模块（无「整轮」概念，只写日志）。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import job_manifest as jm  # noqa: E402
from utils.notifier import toast  # noqa: E402

_notified = set()


def notify(job_dir: str) -> bool:
    """清单非空 -> 弹一条通知并返回 True；空清单/无 job_dir -> False。"""
    if not job_dir:
        return False
    key = os.path.abspath(job_dir)
    missing = jm.subtitle_missing(job_dir)
    if not missing:
        return False
    if key in _notified:
        return False
    _notified.add(key)
    toast("⚠️ 缺字幕需人工处理",
          f"缺字幕未修复：{jm.format_episodes(missing)}（共 {len(missing)} 集）；"
          f"请重剪这些集，字幕非空后才会移出清单；未修复前不会导出。")
    return True
