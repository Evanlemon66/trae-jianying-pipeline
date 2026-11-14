# -*- coding: utf-8 -*-
"""jianying_proc.py — 剪映进程收尾的唯一入口。

全仓所有"关闭/结束剪映"都必须走 stop_jianying，禁止其它模块再写裸 taskkill：
  1. 正常关闭：给剪映所有顶层窗口发 WM_CLOSE，让它自己删锁、落盘 autosave；
  2. 超时兜底：taskkill /IM JianyingPro.exe /T（先温和，再 /F）。
只按固定镜像名处理剪映本体及其进程树，不按任意 PID 强杀别的进程。
"""
from __future__ import annotations

import ctypes
import subprocess
import time
from typing import Callable, Optional

EXE_NAME = 'JianyingPro.exe'
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
WM_CLOSE = 0x0010


def _pids() -> list[int]:
    """当前剪映进程 pid 列表（psutil 不可用时返回空，由 taskkill 兜底）。"""
    try:
        import psutil
        return [p.pid for p in psutil.process_iter(['name'])
                if (p.info.get('name') or '').lower() == EXE_NAME.lower()]
    except Exception:
        return []


def running() -> bool:
    """剪映是否在跑。优先 psutil，缺失时退 tasklist。"""
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            if (p.info.get('name') or '').lower() == EXE_NAME.lower():
                return True
        return False
    except Exception:
        pass
    try:
        r = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {EXE_NAME}'],
                           capture_output=True, text=True,
                           encoding='mbcs', errors='replace', timeout=10,
                           creationflags=_NO_WINDOW)
        return EXE_NAME in (r.stdout or '')
    except Exception:
        return False


def _post_wm_close() -> int:
    """给剪映所有顶层窗口发 WM_CLOSE，返回成功投递的窗口数。"""
    from ctypes import wintypes

    pids = set(_pids())
    if not pids:
        return 0
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        pid = wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid))
        if pid.value in pids and ctypes.windll.user32.PostMessageW(
                wintypes.HWND(hwnd), WM_CLOSE, wintypes.WPARAM(0), wintypes.LPARAM(0)):
            hits.append(int(hwnd))
        return True

    try:
        user32 = ctypes.windll.user32
        # 64 位防御：不设 argtypes 时回调指针会被截断，EnumWindows 静默不回调
        user32.EnumWindows.argtypes = [
            ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
            wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.EnumWindows(_enum, 0)
    except Exception:
        return 0
    return len(hits)


def _taskkill(force: bool) -> None:
    """按镜像名结束剪映进程树；只有本文件允许出现 taskkill。"""
    argv = ['taskkill', '/IM', EXE_NAME, '/T'] + (['/F'] if force else [])
    try:
        subprocess.run(argv, capture_output=True, timeout=15, creationflags=_NO_WINDOW)
    except Exception:
        pass


def stop_jianying(grace_s: float = 20.0, force: bool = False,
                  log_fn: Optional[Callable[[str], None]] = None,
                  allow_force: bool = True) -> bool:
    """关闭剪映并等它退出，返回是否确认已全部退出。

    grace_s     WM_CLOSE 后等待自然退出的秒数（force=True 时跳过）
    force       True = 强制语义（kill）：直接 taskkill /F，不发 WM_CLOSE
    allow_force False = 只发 WM_CLOSE，绝不 taskkill（有未保存草稿时用）
    """
    log = log_fn or (lambda _m: None)
    if not running():
        return True
    if force and allow_force:
        log('强制结束剪映进程树（taskkill /F）…')
        _taskkill(force=True)
    elif force:
        log('要求强制结束但 allow_force=False，改为只发 WM_CLOSE')
        force = False
    if not force:
        n = _post_wm_close()
        if n:
            log(f'已发送 WM_CLOSE 优雅关闭请求（{n} 个窗口）…')
        deadline = time.time() + max(1.0, float(grace_s))
        while time.time() < deadline:
            if not running():
                log('剪映已优雅退出')
                return True
            time.sleep(1.0)
        if not allow_force:
            log(f'剪映 {grace_s:g}s 内未退出，按调用方要求不做强杀')
            return not running()
        log(f'剪映 {grace_s:g}s 内未退出，结束进程树…')
        _taskkill(force=False)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not running():
            break
        time.sleep(1.0)
    if running() and not force:
        _taskkill(force=True)
        for _ in range(10):
            if not running():
                break
            time.sleep(1.0)
    ok = not running()
    log('剪映进程已退出' if ok else '剪映仍在运行（关闭失败）')
    return ok
