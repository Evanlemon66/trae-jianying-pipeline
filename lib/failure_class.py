# -*- coding: utf-8 -*-
"""failure_class.py — 导出失败分类器 + 自愈策略表（第 3 项）。

把旧的 `_UI_RETRY_KEYWORDS` 子串匹配升级为「分类器 + 策略表」：
  - 分类器 `classify(error, phase)`：输入错误串 + 所处阶段，输出类别与动作；纯函数可单测。
  - 策略表 `STRATEGY`：每类写明 是否可重试 / 自愈动作 / 是否计入 consecutive_failures。

分类结果由 cover_watch 写入第 1 项真源 `.export_state.json` 的 `failure_class` 与
`phase`；策略动作（restart / 关弹窗 / 杀进程重试）由第 2 项实例管理器执行。

硬约束（对应需求）：
  - `asset_missing` 与 `disk_full` 必须**不重试**、**不计入失败计数**，直接置状态机
    终态 `failed` 并 toast——避免像重回95 那样空转 38 分钟。
  - `unknown` 视为可重试（保守：未知错误走重启重试，而非直接放弃）。
  - `retry_pause_after`、退避序列、`--phase export` 强制放行语义不变（在 cover_watch/
    export_state 侧，本模块只给分类与动作）。

自愈动作（供管理器/调度器执行）：
  restart        重启剪映后重试（冷启动回首页）
  kill_retry     杀进程后重试（导出卡死，进程可能假死）
  dismiss_retry  关闭阻挡弹窗后重试（版本更新/公告）
  fail_terminal  直接失败不重试，置终态 + toast（素材缺失）
  refuse_launch  拒绝启动不重试，置终态 + toast（磁盘不足）
"""
from __future__ import annotations

from typing import Optional

# ── 类别 ─────────────────────────────────────────────────────────
UI_HANG = "ui_hang"              # 窗口存在但无响应 / 就绪或打开草稿超时
ASSET_MISSING = "asset_missing"  # 草稿或素材不存在/损坏
DISK_FULL = "disk_full"          # 目标盘余量不足
EXPORT_TIMEOUT = "export_timeout"  # 导出无进展超时（看门狗卡死）
VERSION_DIALOG = "version_dialog"  # 版本更新/公告弹窗挡路
UNKNOWN = "unknown"

CATEGORIES = (UI_HANG, ASSET_MISSING, DISK_FULL, EXPORT_TIMEOUT,
              VERSION_DIALOG, UNKNOWN)

# ── 动作 ─────────────────────────────────────────────────────────
RESTART = "restart"
KILL_RETRY = "kill_retry"
DISMISS_RETRY = "dismiss_retry"
FAIL_TERMINAL = "fail_terminal"
REFUSE_LAUNCH = "refuse_launch"

# ── 策略表：类别 → {可重试 / 动作 / 是否计入连续失败计数} ──────────
# counts=True 才会推进 consecutive_failures（退避/暂停）；
# 不可重试类（asset_missing/disk_full）counts=False，直接置终态 failed。
STRATEGY = {
    UI_HANG:        {"retryable": True,  "action": RESTART,       "counts": True},
    ASSET_MISSING:  {"retryable": False, "action": FAIL_TERMINAL, "counts": False},
    DISK_FULL:      {"retryable": False, "action": REFUSE_LAUNCH, "counts": False},
    EXPORT_TIMEOUT: {"retryable": True,  "action": KILL_RETRY,    "counts": True},
    VERSION_DIALOG: {"retryable": True,  "action": DISMISS_RETRY, "counts": True},
    UNKNOWN:        {"retryable": True,  "action": RESTART,       "counts": True},
}


def strategy_of(category: str) -> dict:
    return STRATEGY.get(category, STRATEGY[UNKNOWN])


def _has(error: str, *keywords: str) -> bool:
    return any(k in error for k in keywords)


def classify(error: Optional[str], phase: Optional[str] = None) -> tuple[str, str]:
    """把错误串 + 阶段映射为 (类别, 动作)。规则自上而下，先命中先返回。

    顺序刻意安排：先判「不可重试」与「明确可辨」的硬错误（磁盘/素材/导出卡死/弹窗），
    再判 UI 假死，最后兜底 unknown——避免宽泛关键词（如「未找到」「超时」）抢先误分类。
    """
    e = str(error or "")

    # 1. 磁盘不足：不可重试，拒绝启动
    if _has(e, "磁盘空间不足", "磁盘不足", "空间不足", "no space", "ENOSPC"):
        return DISK_FULL, STRATEGY[DISK_FULL]["action"]

    # 2. 素材/草稿缺失或损坏：不可重试，直接终态
    if _has(e, "素材预检不通过", "源文件损坏", "文件不存在", "文件过小",
            "未找到名为", "链接媒体", "找不到对应串流草稿", "无法读取内容文件",
            "文件缺失", "10011"):
        return ASSET_MISSING, STRATEGY[ASSET_MISSING]["action"]

    # 3. 导出无进展超时（看门狗）：可重试，杀进程重试
    if _has(e, "无增长", "导出卡死", "看门狗", "导出超时", "未检测到完成状态"):
        return EXPORT_TIMEOUT, STRATEGY[EXPORT_TIMEOUT]["action"]

    # 4. 版本更新/公告弹窗挡路：可重试，关弹窗重试
    if _has(e, "以后再说", "版本更新", "检测到新版本", "升级", "公告"):
        return VERSION_DIALOG, STRATEGY[VERSION_DIALOG]["action"]

    # 5. UI 假死 / 就绪或打开草稿超时（含启动失败）：可重试，重启
    if _has(e, "打开草稿超时", "未能进入编辑界面", "程序卡死",
            "无法自动启动", "未找到【剪映专业版】", "重启后仍未回到首页",
            "冷启动失败", "未就绪"):
        return UI_HANG, STRATEGY[UI_HANG]["action"]

    # 6. 其余「超时 / 未找到 / 卡死 / 窗口」类（阶段辅助判断）→ UI 假死
    if _has(e, "超时", "卡死", "未找到", "未响应"):
        return UI_HANG, STRATEGY[UI_HANG]["action"]
    if phase in ("cold_start", "open_draft") and _has(e, "窗口", "首页", "编辑页"):
        return UI_HANG, STRATEGY[UI_HANG]["action"]

    return UNKNOWN, STRATEGY[UNKNOWN]["action"]


def is_retryable(category: str) -> bool:
    return bool(strategy_of(category).get("retryable"))


def counts_toward_failures(category: str) -> bool:
    return bool(strategy_of(category).get("counts"))
