"""桌面通知模块：Windows 现代 Toast / macOS 原生 / Linux 通知。

使用:
    from utils.notifier import toast
    toast("任务完成", "剪映草稿已就绪")
    toast("导出失败", "超时 1200s", icon="error")

设计原则:
    - 异步弹出，绝不阻塞主流程
    - 通知失败静默吞掉（try/except pass），主流程退出码不受影响
    - Windows 优先走 plyer（底层 WinRT 现代 Toast，Win10/11 真生效），
      降级到 PowerShell NotifyIcon（Win7/8 或无 plyer 时）
    - 跨平台：macOS → osascript, Linux → notify-send
    - 状态图标通过 emoji 嵌入 title：✅ info / ⚠️ warning / ❌ error
"""

from __future__ import annotations

import os
import sys
import subprocess


# emoji 图标映射，嵌入 title 让 Windows Toast 更美观
_ICON_EMOJI = {
    "info": "\u2705",     # ✅
    "success": "\u2705",  # ✅
    "warning": "\u26a0\ufe0f",  # ⚠️
    "error": "\u274c",    # ❌
    "done": "\u2705",     # ✅
    "fail": "\u274c",     # ❌
}

_MAX_TITLE = 60
_MAX_MSG = 120


def _fit(text: str, limit: int) -> str:
    """截断到 limit 字符，末尾加省略号。"""
    s = str(text or "").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def toast(title: str, message: str = "", icon: str = "info") -> None:
    """弹一个桌面通知。

    Args:
        title: 通知标题（必填，自动带 emoji 图标前缀）
        message: 通知正文（可选）
        icon: "info" | "success" | "warning" | "error" | "done" | "fail"
    """
    try:
        # 组装美观的标题：emoji + 内容
        emoji = _ICON_EMOJI.get(icon, _ICON_EMOJI["info"])
        nice_title = _fit(f"{emoji} {title}".strip(), _MAX_TITLE)
        nice_msg = _fit(message, _MAX_MSG)

        if sys.platform == "win32":
            _win_toast(nice_title, nice_msg, icon)
        elif sys.platform == "darwin":
            _mac_toast(nice_title, nice_msg)
        else:
            _linux_toast(nice_title, nice_msg, icon)
    except Exception:
        # 通知失败绝不能影响主流程
        pass


def _win_toast(title: str, message: str, icon: str) -> None:
    """Windows：plyer 现代 Toast 优先，降级 NotifyIcon balloon。"""
    if _try_plyer(title, message):
        return
    # 降级：旧 NotifyIcon balloon（Win7/8 或无 plyer）
    _win_balloon(title, message, icon)


def _try_plyer(title: str, message: str) -> bool:
    """尝试 plyer（底层走 WinRT，Win11 真弹）。成功返回 True。"""
    try:
        from plyer import notification  # 延迟 import，不让无 plyer 的环境报错
        notification.notify(title=title, message=message or "", timeout=8)
        return True
    except Exception:
        return False


def _win_balloon(title: str, message: str, icon: str) -> None:
    """PowerShell + .NET NotifyIcon（旧版 Windows 降级）。"""
    esc = lambda s: str(s).replace("'", "''")
    icon_map = {"info": "Info", "warning": "Warning", "error": "Error"}
    ps_icon = icon_map.get(icon, "Info")
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        f"$n.ShowBalloonTip(10000,'{esc(title)}','{esc(message)}',"
        f"[System.Windows.Forms.ToolTipIcon]::{ps_icon});"
        "Start-Sleep -Seconds 12;$n.Dispose()"
    )
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    DETACHED_PROCESS = 0x00000008
    subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", ps],
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _mac_toast(title: str, message: str) -> None:
    script = f'display notification "{message}" with title "{title}"'
    subprocess.Popen(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _linux_toast(title: str, message: str, icon: str) -> None:
    try:
        icon_map = {"info": "dialog-information", "warning": "dialog-warning", "error": "dialog-error"}
        subprocess.Popen(
            ["notify-send", title, message or "", "-i", icon_map.get(icon, "dialog-information")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    # 自测: python -m utils.notifier
    toast("剪映通知模块 OK", "plyer 现代 Toast 链路验证成功")
    toast("测试 warning", "这是一条警告消息", icon="warning")
    toast("测试 error", "这是一条错误消息", icon="error")
    print("toast() 已触发，检查 Windows 通知中心")
