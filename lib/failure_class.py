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
WINDOW_MISSING = "window_missing"  # 未找到剪映窗口（含拉起后窗口始终没出现）
WINDOW_NOT_CREATED = "window_not_created"  # 进程活着但从未建出任何窗口（启动期卡住）
WINDOW_ACTIVATE_TIMEOUT = "window_activate_timeout"  # 找到窗口但激活失败（不可交互）
LAUNCH_FAILED = "launch_failed"  # 拉起剪映失败（早退重试后仍 died / 启动超时）
ASSET_MISSING = "asset_missing"  # 草稿或素材不存在/损坏
DISK_FULL = "disk_full"          # 目标盘余量不足
EXPORT_TIMEOUT = "export_timeout"  # 导出无进展超时（看门狗卡死）
VERSION_DIALOG = "version_dialog"  # 版本更新/公告弹窗挡路
ARTIFACTS_INCOMPLETE = "artifacts_incomplete"  # 导出返回成功但 mp4/封面/progress 不全
VERIFY_FAILED = "verify_failed"    # 导出后质检（帧对齐）未通过
TRACK_RESTORE_FAILED = "track_restore_failed"  # 轨道恢复失败（轨道在可视区外/点后仍脏）
UI_STEP_FAILED = "ui_step_failed"  # 人声分离链路业务失败（音频面板/开关/等待分离完成）
VERSION_UNSUPPORTED = "version_unsupported"  # 剪映版本不在支持范围（预检拒绝）
UNKNOWN = "unknown"

CATEGORIES = (UI_HANG, WINDOW_MISSING, WINDOW_NOT_CREATED, WINDOW_ACTIVATE_TIMEOUT,
              LAUNCH_FAILED, ASSET_MISSING, DISK_FULL, EXPORT_TIMEOUT, VERSION_DIALOG,
              ARTIFACTS_INCOMPLETE, VERIFY_FAILED, TRACK_RESTORE_FAILED, UI_STEP_FAILED,
              VERSION_UNSUPPORTED, UNKNOWN)

# 跨轮重拉只对"拉起本身失败"生效：这类失败与草稿/素材无关，重新拉起有实质意义。
# 由 robust_auto_export 在实例管理器 launch() 返回 died/timeout 时打 `[launch_failed:`
# 标记——没有标记的 ui_hang（打开草稿超时等）仍走原有退避，不进跨轮重拉。
RELAUNCH_CLASSES = (LAUNCH_FAILED,)

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
    # 窗口没出现 ≠ UI 假死：重启一次通常能带回窗口；日志里能与 ui_hang 一眼区分。
    WINDOW_MISSING: {"retryable": True,  "action": RESTART,       "counts": True},
    # 进程活着但从未建窗（启动期卡在 GPU 枚举/环境检测等）：独立一类，不并入 launch_failed；
    # 处理动作与「窗口没出现」一致（重启），只是日志里能一眼看出它根本没进到建窗阶段。
    WINDOW_NOT_CREATED: {"retryable": True, "action": RESTART,    "counts": True},
    # 找到窗口但激活超时 = 窗口在却不可交互（常是假死）：杀进程重建，别在旧实例上重试。
    WINDOW_ACTIVATE_TIMEOUT: {"retryable": True, "action": KILL_RETRY, "counts": True},
    # 拉起失败：本轮不追加同轮重试（EARLY_DEATH_RETRIES=2 不动），交给 cover_watch
    # 的跨轮兜底——下一轮扫描优先重拉，连续多轮失败由 RELAUNCH_MAX_ROUNDS 截断。
    LAUNCH_FAILED:  {"retryable": True,  "action": RESTART,       "counts": True},
    ASSET_MISSING:  {"retryable": False, "action": FAIL_TERMINAL, "counts": False},
    DISK_FULL:      {"retryable": False, "action": REFUSE_LAUNCH, "counts": False},
    EXPORT_TIMEOUT: {"retryable": True,  "action": KILL_RETRY,    "counts": True},
    VERSION_DIALOG: {"retryable": True,  "action": DISMISS_RETRY, "counts": True},
    # 导出成功但产物不全 / 质检（帧对齐）不过：可重试，按退避重导；连续失败会暂停
    ARTIFACTS_INCOMPLETE: {"retryable": True, "action": RESTART, "counts": True},
    VERIFY_FAILED: {"retryable": True, "action": RESTART, "counts": True},
    # 轨道恢复失败（可视区外/解锁或恢复可见后复检仍脏）：不是导出本身的问题，
    # 准入可重跑；重启回首页后重新定位轨道再试，不能落 unknown 静默放弃。
    TRACK_RESTORE_FAILED: {"retryable": True, "action": RESTART, "counts": True},
    # 人声分离链路的业务失败（音频面板没切过去 / 开关没勾上 / 分离没跑完）：
    # 原因明确、可重跑，不能落 unknown（unknown 的语义是"未知错误保守重启"）。
    UI_STEP_FAILED: {"retryable": True, "action": RESTART, "counts": True},
    # 版本不符是环境问题：重试不会改变版本，直接拒绝并提示支持版本
    VERSION_UNSUPPORTED: {"retryable": False, "action": REFUSE_LAUNCH, "counts": False},
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

    # 0. 拉起失败（robust_auto_export 打的显式标记）：必须先于 ui_hang 判定，
    #    因为它的文案里同样含"无法自动启动"，不能混进普通 UI 假死。
    if "[launch_failed:" in e or e.startswith("launch_failed"):
        return LAUNCH_FAILED, STRATEGY[LAUNCH_FAILED]["action"]

    # 0.2 窗口级失败：窗口没出现 / 找到但激活超时，各自独立上报，不再统一报 ui_hang。
    if "[window_activate_timeout]" in e or _has(e, "窗口激活失败", "窗口激活超时"):
        return WINDOW_ACTIVATE_TIMEOUT, STRATEGY[WINDOW_ACTIVATE_TIMEOUT]["action"]
    if "[window_missing]" in e:
        return WINDOW_MISSING, STRATEGY[WINDOW_MISSING]["action"]
    # 进程活着但从未建窗：标记形如 [window_not_created alive=… pid=…]，故不能带闭合括号
    if "[window_not_created" in e:
        return WINDOW_NOT_CREATED, STRATEGY[WINDOW_NOT_CREATED]["action"]

    # 0.5 剪映版本不在支持范围（预检拒绝）：重试无意义，直接终态并提示支持版本。
    #     必须先于「未找到 / 超时」等宽泛关键词，否则会被误判成可重试的 ui_hang。
    if _has(e, "版本预检不通过", "不会静默拉起其它版本", "仅支持剪映"):
        return VERSION_UNSUPPORTED, STRATEGY[VERSION_UNSUPPORTED]["action"]

    # 0.6 轨道恢复失败（轨道在时间线可视区外 / 解锁或恢复可见后复检仍脏）：
    #     准入轨道整理阶段的专属失败，必须在「超时/未找到/窗口」等宽泛规则前命中，
    #     否则会落到 unknown 或误判成 ui_hang。
    if _has(e, "恢复轨道失败", "可视区外", "无法判定是锁定还是不可见",
            "轨道锁定或隐藏"):
        return TRACK_RESTORE_FAILED, STRATEGY[TRACK_RESTORE_FAILED]["action"]

    # 0.65 人声分离链路的业务失败：音频面板没切过去 / 人声分离开关没勾上 /
    #      等待分离完成超时 / 分离前复检不通过（字幕轨未锁定+隐藏、选中范围不对）。
    #      这几处文案稳定，单列一类，不落 unknown。
    if _has(e, "人声分离开关", "人声分离未完成", "看到「人声分离」", "音频面板",
            "滚动 20 轮", "分离前复检", "拒绝发送 Ctrl+Shift+S",
            "未保持锁定+隐藏", "主轨之外仍有可被选中的轨道", "选中主轨失败",
            "时间码未回到 00:00:00", "未全部白描边", "分离音频已中止",
            "坐标已失配"):
        return UI_STEP_FAILED, STRATEGY[UI_STEP_FAILED]["action"]

    # 0.7 导出后门禁（子进程 rc=3 回传的 brief）：先判质检，再判产物不全。
    #     两者都必须早于「文件不存在 / 超时」等宽泛规则，否则会被 asset_missing 抢走。
    if _has(e, "verify rc=", "质检未通过"):
        return VERIFY_FAILED, STRATEGY[VERIFY_FAILED]["action"]
    if _has(e, "产物不全", "缺产物证据", "导出后校验未通过"):
        return ARTIFACTS_INCOMPLETE, STRATEGY[ARTIFACTS_INCOMPLETE]["action"]

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
    #    英文 UIA 报错（"Find Control Timeout(10s): {Name: '剪映专业版'...}"）与
    #    COMError 都不含中文关键词，旧规则会落 unknown；这里显式归入 ui_hang。
    if _has(e, "打开草稿超时", "未能进入编辑界面", "程序卡死",
            "无法自动启动", "未找到【剪映专业版】", "重启后仍未回到首页",
            "冷启动失败", "未就绪",
            "Find Control Timeout", "Timeout", "COMError"):
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
