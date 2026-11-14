# -*- coding: utf-8 -*-
"""剪映导航：回首页 / 草稿存在性 / 打开草稿（UIA，从 _run_vocal_draft2 提升复用）"""
import time

import uiautomation as uia

from ui_core import (connect_jianying, dprint, jl, launch_and_connect, op_mark,
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
    # 探测统一走 lib/jianying_proc.py（唯一进程入口）
    import jianying_proc as jp
    return jp.running()


# 本轮是否由本进程拉起/重启过剪映：准入收尾据此决定"要不要替用户关掉"
JIANYING_LAUNCHED = False


def _graceful_close(graceful_timeout=20):
    """正常关闭剪映并等待退出：WM_CLOSE → 超时结束进程树兜底。

    返回是否已确认进程退出。只在需要换窗口/收尾时调用；调用方自行决定是否等 2s 静默。
    优雅关闭会让剪映自己删掉 .locked 并落盘 autosave，避免下次打开弹恢复提示。
    具体关闭动作统一走 lib/jianying_proc.py（唯一入口），本函数只负责收尾缓存。
    """
    import jianying_proc as jp
    t0 = time.time()
    exited = jp.stop_jianying(grace_s=graceful_timeout, force=False, log_fn=dprint)
    dprint(f'[剪映] 进程已退出（{time.time()-t0:.1f}s）' if exited
           else '[剪映] 进程仍在运行')
    try:
        from ui_core import invalidate_hwnd_cache
        invalidate_hwnd_cache()   # 进程已退出，清 HWND 缓存避免 attach 死句柄
    except Exception:
        pass
    return exited


def close_jianying(graceful_timeout=20):
    """收尾关闭剪映（策略统一走共用层 finish_run）。

    留窗 / 关闭 / 只关本轮拉起的实例三种策略由 finish_run 判定：
    非本轮拉起（JIANYING_LAUNCHED=False）时不会误杀用户自己的剪映。
    返回 True 表示已确认退出（或按策略本就不该关）；False 表示仍在运行。
    """
    global JIANYING_LAUNCHED
    ok = jl.finish_run(
        keep_window=False, launched_this_run=JIANYING_LAUNCHED,
        close_fn=lambda: _graceful_close(graceful_timeout),
        is_running_fn=_jianying_running, log_fn=dprint)
    if ok:
        JIANYING_LAUNCHED = False
    return ok


def _cold_restart():
    """关闭剪映并等其真正退出：先 WM_CLOSE 优雅关闭（正常退出可跳过崩溃恢复，
    下次冷启动显著更快），20s 未退再强杀。仅在首页状态调用（无草稿打开，无保存风险）。"""
    _graceful_close(20)
    time.sleep(2)


def _wait_draft_card(name, seconds):
    """轮询找同名草稿卡片；进程中途退出立即返回，避免对着死进程空等。
    返回 (ctrl|None, reason)，reason ∈ {'found','dead','timeout'}。"""
    deadline = time.time() + seconds
    while True:
        ctrl = _find_draft_ctrl_any(name)
        if ctrl:
            return ctrl, 'found'
        if not _jianying_running():
            return None, 'dead'
        if time.time() >= deadline:
            return None, 'timeout'
        time.sleep(2)


def _launch_until_card(name, cold_wait=90, max_launch=2, readiness='window'):
    """冷启动拉起并找卡片：覆盖『启动即死』重试、『启动后停在编辑页』两种情况。

    readiness（默认 'window'，与既有行为一致）决定等窗口就绪还是等首页就绪：
      - 'window'：拉起后若停在编辑页 → 先 switch_to_home 再找卡片；
      - 'home'  ：launch_and_connect 已等到首页才返回，下面的 edit 分支通常走不到；
                  若冷启动停在编辑页且一直没出现首页，会等满 420s 判本轮失败，
                  再由外层 max_launch 决定是否再拉一轮。
    """
    global JIANYING_LAUNCHED
    for attempt in range(1, max_launch + 1):
        try:
            # retries=0：重拉预算归本函数（max_launch），共用层不再叠加
            launch_and_connect(max_wait=420, retries=0, readiness=readiness)
            JIANYING_LAUNCHED = True   # 本轮确实拉起过剪映，收尾时由准入决定是否关闭
        except Exception as e:
            dprint(f'  [cold] 第{attempt}次拉起失败: {e}')
            time.sleep(3)
            continue
        # 'window' 口径下剪映冷启动可能直接恢复到上次的编辑页 → 先回首页，否则卡片永远扫不到
        # 'home' 口径下 launch_and_connect 已确认首页，这里的 edit 判定通常不成立
        _, pt = connect_jianying(retry=False)
        if pt == 'edit':
            dprint('  [cold] 启动后停在编辑页，先回首页再找卡片')
            switch_to_home(max_wait=20)
        ctrl, reason = _wait_draft_card(name, cold_wait)
        if ctrl:
            return ctrl
        if reason == 'dead':
            dprint(f'  [cold] 第{attempt}次拉起后进程退出（启动即死）')
            time.sleep(3)
            continue
        return None   # 进程存活但卡片始终未出现
    return None


def open_draft(name, timeout=60, hot_wait=30, cold_wait=90, max_launch=2,
               allow_cold_restart=True, readiness='window'):
    """打开指定草稿。

    hot_wait            热复用等待秒数（首页列表已在）；0 = 只扫一次就转冷启动
    cold_wait           冷启动后每轮找卡片秒数（默认 90，与旧行为一致）
    max_launch          冷启动重拉次数（默认 2，与旧行为一致）
    allow_cold_restart  False = 禁止杀进程/重开剪映；热态做不到直接报错
    readiness           冷启动就绪口径：'window'（默认，等窗口）/ 'home'（等首页）；
                        见 _launch_until_card 的两种分支说明
    """
    t_open = time.time()
    refreshed = False
    first_ready = None
    cold = False
    if not _jianying_running():
        if not allow_cold_restart:
            raise RuntimeError(
                f'剪映未运行，且已禁止冷启动（allow_cold_restart=false），无法打开草稿 {name}')
        dprint('[open_draft] 剪映未运行 → 冷启动拉起')
        cold = True
    deadline = t_open + max(0.0, float(hot_wait))
    while not cold:
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
        # 启动即死：进程已退出时不要再空等剩余时间，直接转冷启动重试
        if not _jianying_running():
            cold = True
            dprint(f'[open_draft] 剪映进程已退出（+{time.time()-t_open:.1f}s），转冷启动…')
            break
        # 热发布后列表可能延迟刷新：第一轮找不到就做一次主动刷新，其余轮询等待
        if not refreshed:
            refreshed = True
            refresh_home_list(win)
        # 热复用窗口（默认 30s）：剪映首页列表不会因外部改盘重载（磁盘草稿已在，
        # UIA 卡片停在旧列表，模板→首页 切换也不触发重载）→ 到点就冷启动强制重载
        if time.time() >= deadline:
            if not allow_cold_restart:
                raise RuntimeError(
                    f'热态 {float(hot_wait):g}s 内未在首页找到草稿「{name}」，'
                    f'且已禁止冷启动（allow_cold_restart=false）')
            cold = True
            dprint(f'热复用下首页未见「{name}」（已等 {time.time()-t_open:.0f}s），'
                   f'冷启动剪映强制重载草稿列表…')
            break
        time.sleep(2)
    if cold:
        _cold_restart()
        t_c = time.time()
        # 刚杀完进程，跳过「先找窗口」；内置『启动即死→重拉』与『启动后停在编辑页→回首页』
        ctrl = _launch_until_card(name, cold_wait=cold_wait, max_launch=max_launch,
                                  readiness=readiness)
        dprint(f'[open_draft] 冷启动窗口就绪 {time.time()-t_c:.1f}s')
        if ctrl:
            dprint(f'[open_draft] 冷启动后卡片出现于 +{time.time()-t_open:.1f}s → 点击')
            op_mark('点击', f'草稿卡片「{name}」（冷启动重载后）')
            ctrl.GetParentControl().Click(simulateMove=False)
            dprint(f'点击草稿: {name}（冷启动重载后）')
        else:
            raise RuntimeError(
                f'未找到草稿 {name}（冷启动重载后仍未出现；'
                f'窗口就绪 {time.time()-t_c:.1f}s，已重试拉起 {max_launch} 次）')
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
