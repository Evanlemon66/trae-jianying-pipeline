# -*- coding: utf-8 -*-
"""no_window.py — 隐藏控制台的子进程常量与兜底补丁。

watcher 现由 pythonw.exe 托管（不创建控制台）。它拉起的所有控制台程序（ffmpeg、
demucs、python 脚本）如果照旧继承，会各自新建一个可见控制台窗口；必须显式带
CREATE_NO_WINDOW 才不会弹窗。

用法：
  subprocess.run(cmd, ..., creationflags=NO_WINDOW)

兜底（防再犯）：入口在 import 后调用一次 install_default_hidden()。当当前进程
没有可用控制台（pythonw 启动 / stdout 为 None / GetConsoleWindow()==0）时，给
subprocess.Popen.__init__ 打补丁：调用方未显式传 creationflags 时自动补上
CREATE_NO_WINDOW。显式传入的值（含 0、DETACHED_PROCESS、CREATE_NEW_CONSOLE）
一律原样尊重。有控制台（终端里手工 python 调试）时完全不打补丁。
"""
import os
import sys
import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_HERE = os.path.dirname(os.path.abspath(__file__))

_installed = False


def _console_available() -> bool:
    """当前进程是否有可用控制台。非 Windows 一律视为有（无需隐藏）。"""
    if os.name != "nt":
        return True
    if (sys.executable or "").lower().endswith("pythonw.exe"):
        return False
    if sys.stdout is None or sys.stderr is None:
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        # 判断不了就保守当作有控制台：宁可漏打补丁，也不要干扰调试
        return True


def install_default_hidden(force: bool = False) -> bool:
    """无控制台环境下给 Popen 兜底加 CREATE_NO_WINDOW；幂等，失败只告警。

    force=True 时跳过「当前是否有控制台」的启发式判断（供 sitecustomize 显式开关使用）。
    返回 True 表示本次调用完成了安装；已安装过 / 有控制台 / 非 Windows 返回 False。
    """
    global _installed
    if _installed or NO_WINDOW == 0 or (not force and _console_available()):
        return False
    try:
        orig_init = subprocess.Popen.__init__
        if getattr(orig_init, "_no_window_patched", False):
            _installed = True
            return False

        def _patched_init(self, *args, **kwargs):
            # 只在调用方完全没写 creationflags 时补默认值；显式值一律不覆盖
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = NO_WINDOW
            return orig_init(self, *args, **kwargs)

        _patched_init._no_window_patched = True
        subprocess.Popen.__init__ = _patched_init
        _installed = True
        return True
    except Exception as e:
        print(f"[warn] no_window 兜底安装失败（忽略）: {e}")
        return False


def hidden_child_env(env: dict | None = None) -> dict:
    """给独立 python 子进程准备环境：PYTHONPATH 前置 pipeline + JY_HIDE_WINDOW=1。

    只复制、不修改父进程 os.environ；其余变量原样继承。子进程启动时 site.py 会自动
    import 本目录的 sitecustomize.py，从而把补丁带进第三方包（如 pydub/videocaptioner）。
    """
    child = dict(os.environ if env is None else env)
    dirs = [p for p in child.get("PYTHONPATH", "").split(os.pathsep) if p]
    if _HERE not in dirs:
        dirs.insert(0, _HERE)
    child["PYTHONPATH"] = os.pathsep.join(dirs)
    child["JY_HIDE_WINDOW"] = "1"
    return child
