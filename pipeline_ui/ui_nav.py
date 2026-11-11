# -*- coding: utf-8 -*-
"""剪映导航：回首页 / 草稿存在性 / 打开草稿（UIA，从 _run_vocal_draft2 提升复用）"""
import time

import uiautomation as uia

from ui_core import (connect_jianying, dprint, launch_and_connect, op_mark,
                     enum_jianying_windows, op_stage)


def switch_to_home(max_wait=30):
    win, pt = connect_jianying()
    if pt == 'home':
        return True
    btn = win.GroupControl(searchDepth=1, ClassName='TitleBarButton', foundIndex=3)
    if btn.Exists(1):
        op_mark('点击', '标题栏「回首页」按钮')
        btn.Click(simulateMove=False)
        for _ in range(max_wait // 2):
            time.sleep(2)
            # 首页/编辑页标题都叫「剪映专业版」，不能用 ClassName 判页（Qt6 下恒不匹配）
            _, pt2 = connect_jianying(retry=False)
            if pt2 == 'home':
                dprint('已回首页')
                return True
    return False


def refresh_home_list(win=None, rounds=2):
    """主动刷新首页草稿列表（切「模板」→切回「首页」）。
    剪映热发布后列表不会立即出现新卡片，延迟刷新可能超过 open_draft 的轮询窗口。"""
    if win is None:
        win, pt = connect_jianying()
        if pt != 'home':
            return False
    for _ in range(rounds):
        try:
            tpl = win.TextControl(SubName='模板', searchDepth=15)
            if tpl.Exists(1):
                op_mark('点击', '模板Tab（刷新草稿列表）')
                tpl.Click(simulateMove=False)
                time.sleep(1.2)
                home = win.TextControl(SubName='首页', searchDepth=15)
                if home.Exists(1):
                    op_mark('点击', '首页Tab（刷新草稿列表）')
                    home.Click(simulateMove=False)
                    time.sleep(1.5)
                    dprint('已刷新首页草稿列表')
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _find_draft_ctrl(win, name):
    """在首页草稿列表找指定草稿控件，返回 (控件, 是否找到)"""
    target = f'HomePageDraftTitle:{name}'
    found = []

    def scan(ctrl, d=0):
        if d > 10 or found:
            return
        try:
            if (ctrl.GetPropertyValue(30159) or '') == target:
                found.append(ctrl)
                return
        except Exception:
            pass
        try:
            for c in ctrl.GetChildren():
                scan(c, d + 1)
        except Exception:
            pass

    scan(win)
    return found[0] if found else None


def _find_draft_ctrl_any(name):
    """跨所有剪映顶层窗口（首页+编辑页）找草稿卡片。
    Win32 枚举窗口句柄（毫秒级，不受 UIA 枚举卡顿影响）→ 逐个绑定后扫子树。
    修 bug：connect_jianying 可能返回编辑页窗口，其子树没有首页卡片，
    导致卡片明明在屏幕上却扫不到、白白等满 12 轮后误触发冷启动。"""
    for hwnd, cls, title in enum_jianying_windows():
        try:
            win = uia.ControlFromHandle(hwnd)
            if win is not None:
                ctrl = _find_draft_ctrl(win, name)
                if ctrl:
                    return ctrl
        except Exception:
            continue
    return None


def draft_exists(name):
    win, pt = connect_jianying()
    if pt != 'home':
        return False
    return _find_draft_ctrl(win, name) is not None


def _jianying_running():
    import subprocess
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq JianyingPro.exe'],
                           capture_output=True, text=True,
                           encoding='mbcs', errors='replace', timeout=10,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return 'JianyingPro.exe' in (r.stdout or '')
    except Exception:
        return False


def _cold_restart():
    """关闭剪映并等其真正退出：先 WM_CLOSE 优雅关闭（正常退出可跳过崩溃恢复，
    下次冷启动显著更快），20s 未退再强杀。仅在首页状态调用（无草稿打开，无保存风险）。"""
    import subprocess, ctypes
    graceful = False
    try:
        win = uia.WindowControl(searchDepth=1, ClassName='Qt51515QWindowIcon')
        if not win.Exists(2):
            win = uia.WindowControl(searchDepth=1, Name='剪映专业版')
        if win.Exists(2) and win.NativeWindowHandle:
            ctypes.windll.user32.PostMessageW(int(win.NativeWindowHandle), 0x0010, 0, 0)
            dprint('  已发送 WM_CLOSE 优雅关闭请求…')
            deadline = time.time() + 20
            while time.time() < deadline:
                if not _jianying_running():
                    graceful = True
                    break
                time.sleep(1)
    except Exception as e:
        dprint(f'  [warn] 优雅关闭失败: {e}')
    if not graceful:
        try:
            subprocess.run(['taskkill', '/F', '/IM', 'JianyingPro.exe', '/T'],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as e:
            dprint(f'  [warn] taskkill 失败: {e}')
    t_kill = time.time()
    for _ in range(30):
        if not _jianying_running():
            break
        time.sleep(1)
    dprint(f'[open_draft] 剪映进程已退出'
           f'（{"优雅关闭" if graceful else "强杀"}，{time.time()-t_kill:.1f}s）')
    try:
        from ui_core import invalidate_hwnd_cache
        invalidate_hwnd_cache()   # 进程已退出，清 HWND 缓存避免 attach 死句柄
    except Exception:
        pass
    time.sleep(2)


def open_draft(name, timeout=60):
    t_open = time.time()
    refreshed = False
    first_ready = None
    cold = False
    while True:
        t_c = time.time()
        win, _ = connect_jianying()
        if first_ready is None:
            first_ready = time.time() - t_c
            dprint(f'[open_draft] 窗口就绪 {first_ready:.1f}s')
        ctrl = _find_draft_ctrl_any(name)   # 跨窗口扫（connect 可能返回编辑页窗口）
        if ctrl:
            dprint(f'[open_draft] 卡片出现于 +{time.time()-t_open:.1f}s → 点击')
            op_mark('点击', f'草稿卡片「{name}」')
            ctrl.GetParentControl().Click(simulateMove=False)
            dprint(f'点击草稿: {name}')
            break
        # 热发布后列表可能延迟刷新：第一轮找不到就做一次主动刷新，其余轮询等待
        if not refreshed:
            refreshed = True
            refresh_home_list(win)
        # 热复用最多等 30s：剪映首页列表不会因外部改盘重载（磁盘草稿已在，
        # UIA 卡片停在旧列表，模板→首页 切换也不触发重载）→ 到点就冷启动强制重载
        if time.time() - t_open >= 30:
            cold = True
            dprint(f'热复用下首页未见「{name}」（已等 {time.time()-t_open:.0f}s），'
                   f'冷启动剪映强制重载草稿列表…')
            break
        time.sleep(2)
    if cold:
        _cold_restart()
        t_c = time.time()
        # 刚杀完进程，跳过「先找窗口」直接拉起（避免 UIA 枚举僵尸窗口阻塞）
        win, _ = launch_and_connect(max_wait=420)
        dprint(f'[open_draft] 冷启动窗口就绪 {time.time()-t_c:.1f}s')
        for _ in range(12):
            ctrl = _find_draft_ctrl_any(name)
            if ctrl:
                dprint(f'[open_draft] 冷启动后卡片出现于 +{time.time()-t_open:.1f}s → 点击')
                op_mark('点击', f'草稿卡片「{name}」（冷启动重载后）')
                ctrl.GetParentControl().Click(simulateMove=False)
                dprint(f'点击草稿: {name}（冷启动重载后）')
                break
            time.sleep(2)
        else:
            raise RuntimeError(f'未找到草稿 {name}（冷启动重载后仍未出现）')
    t_editor = time.time()
    for i in range(timeout):
        time.sleep(1)
        t_p = time.time()
        win, pt = connect_jianying(retry=False)
        dur = time.time() - t_p
        if pt == 'edit':
            import ui_calibration
            ui_calibration.set_current_win(win)
            dprint(f'进入编辑页 (轮询{i + 1}次，本次检测 {dur:.1f}s，'
                   f'自 open_draft 起 {time.time()-t_open:.1f}s)')
            time.sleep(3)
            return
        # 打开草稿会新建顶层窗口，旧首页窗口随即销毁；期间可能短暂无窗口。
        # 判页已改为 MainTimeLineRoot 特征（实测编辑页 4s 内可判出），
        # 不再「轮询 10 次就假定成功」——假定错误会让后续 scan_tracks 全盘失败。
        if pt is None and i % 5 == 0:
            dprint(f'  ...页面类型未判定（第{i + 1}轮，本次检测 {dur:.1f}s，'
                   f'自 open_draft 起 {time.time()-t_open:.1f}s）')
        if dur > 10:
            dprint(f'  ...页面检测耗时 {dur:.0f}s（草稿加载中，UIA 阻塞）')
    raise RuntimeError(f'打开草稿超时（{timeout}s 内未判定为编辑页）')
