# -*- coding: utf-8 -*-
"""
分离音频（Ctrl+Shift+S）+ 字幕轨收尾
======================================
用户确认的流程（7 步）：
  1. 隐藏并锁定识别字幕产生的两条字幕轨（顶部成簇 text 轨）
  2. Home + Ctrl+A 选中主轨道（ui_core.select_main_track，高频操作统一封装）
  3. Ctrl+Shift+S 分离音频
  4. 主轨道下方多出一条音频轨道 = 成功（轮询验证）
  5. 解除所有轨道的锁定和隐藏（restore_all_tracks）
  6. 只隐藏中文字幕轨（两条字幕轨中从上到下第二条，用户确认）
  7. 把最终所有轨道元素和坐标写入 ui_calibration.json（final_layout）供后续步骤使用

自愈：不在编辑页时自动打开最近草稿；UIA 窗口句柄失效时自动重连。
"""

from __future__ import annotations

import time
import os
import sys

import pyautogui
pyautogui.FAILSAFE = False

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui_core import (
    dprint, connect_jianying, ensure_maximized, send_keys, select_main_track,
    op_mark, op_timeline, take_op_marks, op_stage,
)
from ui_track_ops import (
    scan_tracks, find_main_track, lock_track, restore_all_tracks,
    _set_row_hidden, is_row_locked, get_lock_x,
    _HDR_SCALE,
)
from ui_calibration import update_param


def _ensure_edit_page(win):
    """确保在编辑页：不在则打开最近修改的草稿。返回 (win, ok)"""
    win, page = connect_jianying()
    if not win:
        return None, False
    if page == 'edit':
        return win, True
    dprint(f"  当前不在编辑页（page={page}），尝试打开最近草稿...")
    from ui_nav import open_draft
    from ui_recognize_translate import _active_draft_name
    name = _active_draft_name()
    if not name:
        dprint("  ❌ 找不到任何草稿")
        return None, False
    try:
        open_draft(name)
    except Exception as e:
        dprint(f"  ❌ 打开草稿 {name} 失败: {e}")
        return None, False
    win, page = connect_jianying(retry=False)
    return (win, page == 'edit')


def _safe_scan(win):
    """
    scan_tracks 自愈版：UIA 窗口句柄失效（COMError）时自动重连重试。
    返回 (rows, img, WL, WT, WW, eye_x, win)。
    """
    for _ in range(3):
        try:
            rows, img, WL, WT, WW, eye_x = scan_tracks(win)
            return rows, img, WL, WT, WW, eye_x, win
        except Exception as e:
            dprint(f"  ⚠️ 窗口句柄失效（{type(e).__name__}），重连中...")
            time.sleep(1)
            win2, _ = connect_jianying(retry=False)
            if win2:
                win = win2
    return [], None, 0, 0, 0, 0, win


def _subs_from_hint(hint):
    """从 [4/6] 已扫过的 rows 推导字幕轨簇，避免再跑一次完整 scan_tracks。
    隐藏行采不到片段像素 → type 会是 unknown，因此这里只用 y 间距成簇：
    顶部连续两行且与第三行断开（> 40px 逻辑间距）即视为字幕轨对。
    形态不符（不足 2 行 / 第三条间距过近）返回 (None, None) 让调用方回退完整扫描。
    返回 (cluster_rows[:2], eye_x) 或 (None, None)。
    """
    if not hint or not hint[0]:
        return None, None
    rows, eye_x = hint[0], hint[5]
    gap = 40 * max(float(_HDR_SCALE), 0.5)
    ordered = sorted(rows, key=lambda r: r['ymid'])
    cluster = []
    for r in ordered:
        if not cluster or r['ymid'] - cluster[-1]['ymid'] <= gap:
            cluster.append(r)
        else:
            break
    if len(cluster) < 2:
        return None, None
    return cluster[:2], eye_x


def _top_text_cluster(win):
    """
    顶部连续成簇的 text 轨 = 识别字幕产生的字幕轨（中文+英文）。
    与下方误分类的旧轨用 40px 逻辑间距断开（按 _HDR_SCALE 换算：1920 下 40px，
    4K@150% 下 60px，否则 4K 实测字幕行间距 42px 会被断成单条）。
    返回 (cluster_rows 按 ymid 升序, eye_x, win)。
    """
    gap = 40 * max(float(_HDR_SCALE), 0.5)
    rows, _, _, _, _, eye_x, win = _safe_scan(win)
    text_rows = sorted([r for r in rows if r['type'] == 'text'], key=lambda r: r['ymid'])
    cluster = []
    for r in text_rows:
        if not cluster or r['ymid'] - cluster[-1]['ymid'] <= gap:
            cluster.append(r)
        else:
            break
    return cluster, eye_x, win


def _write_final_layout(win, audio_row, type_overrides=None):
    """Step 7: 最终轨道布局全量落盘（每行：类型/ymid/隐藏/锁定/眼睛/锁定图标坐标）
    type_overrides: {ymid: type} 隐藏行采不到片段像素会得 unknown，用隐藏前的已知类型补正"""
    rows, img, WL, WT, WW, eye_x, win = _safe_scan(win)
    if not rows:
        dprint("  ⚠️ 布局落盘失败：未扫到轨道")
        return []
    lock_x_abs = get_lock_x(WL, WW)
    ovr = type_overrides or {}
    layout = [{
        'index': i,
        'type': ovr.get(r['ymid'], r['type']),
        'ymid': r['ymid'],
        'hidden': bool(r['hidden']),
        'locked': bool(is_row_locked(img, WT, r['ymid'])),
        'eye':  [eye_x, r['ymid']],
        'lock': [lock_x_abs, r['ymid']],
    } for i, r in enumerate(rows)]
    update_param('final_layout', 'recorded_at', time.strftime('%Y-%m-%dT%H:%M:%S'))
    update_param('final_layout', 'tracks', layout)
    if audio_row:
        update_param('final_layout', 'separated_audio', {
            'ymid': audio_row['ymid'],
            'eye':  [eye_x, audio_row['ymid']],
            'lock': [lock_x_abs, audio_row['ymid']],
        })
    dprint(f"  ✅ 最终布局已落盘 final_layout（{len(layout)} 行）")
    for t in layout:
        dprint(f"     行{t['index']}: {t['type']:<6} ymid={t['ymid']} "
               f"hidden={t['hidden']} locked={t['locked']} eye={t['eye']} lock={t['lock']}")
    return layout


def run_extract_audio(workdir: str = ".", timeout_sec: int = 30) -> dict:
    """分离音频：把选中主轨视频片段的音轨拆成独立音频轨。快捷键 Ctrl+Shift+S。"""
    op_stage('5 分离音频')
    t0 = time.time()
    os.makedirs(os.path.abspath(workdir), exist_ok=True)

    dprint("=" * 60)
    dprint("[5] 分离音频 - 开始")
    dprint("=" * 60)

    win, ok = _ensure_edit_page(None)
    if not ok:
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': '未能进入编辑页'}
    win = ensure_maximized(win)

    # --- Step 1: 隐藏并锁定两条字幕轨 ---
    dprint("\n[1/6] 隐藏并锁定两条字幕轨")
    subs, eye_x, win = _top_text_cluster(win)
    if len(subs) < 2:
        dprint(f"  ⚠️ 顶部字幕轨簇只有 {len(subs)} 条（预期 2 条），继续执行")
    for r in subs:  # 先全部锁定（锁定不引起行位移）
        lock_track(win, r['ymid'], want_locked=True)
    for r in sorted(subs, key=lambda x: -x['ymid']):  # 从下往上隐藏（防行位移错位）
        _set_row_hidden(win, eye_x, r['ymid'], True, f"字幕轨 ymid={r['ymid']}")

    # --- Step 2: Home + Ctrl+A 选中主轨 ---
    dprint("\n[2/6] Home + Ctrl+A 选中主轨")
    rows0, _, _, _, _, _, win = _safe_scan(win)
    main0, _ = find_main_track(win, rows0)
    if not main0:
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': '未定位主轨（无 video 行）'}
    n0 = len(rows0)
    select_main_track()

    # --- Step 3: Ctrl+Shift+S 分离音频 ---
    dprint("\n[3/6] Ctrl+Shift+S 分离音频")
    # 物理按键：UIA SendKeys 对剪映组合快捷键不可靠（5.9 实测不触发）
    op_mark('组合键', 'Ctrl+Shift+S（分离音频）')
    pyautogui.hotkey('ctrl', 'shift', 's')
    time.sleep(1)

    # --- Step 4: 轮询验证：主轨下方多出一条音频轨 ---
    dprint(f"\n[4/6] 等待新音频轨出现（主轨 ymid={main0['ymid'] if main0 else '?'}，超时 {timeout_sec}s）")
    audio_row = None
    hint = None          # [4/6] 最后一次扫描结果，供 [5/6] 复用（省 ~8-17s UIA 枚举）
    t_deadline = time.time() + timeout_sec
    while time.time() < t_deadline:
        time.sleep(2)
        rows1, img1, WL1, WT1, WW1, eye_x1, win = _safe_scan(win)
        if len(rows1) > n0 and main0:
            below = [r for r in rows1 if r['ymid'] > main0['ymid']]
            if below:
                audio_row = below[-1]   # 最底的新行 = 分离出的音频轨
                hint = (rows1, img1, WL1, WT1, WW1, eye_x1)
                dprint(f"  ✅ 主轨下方出现新轨道: ymid={audio_row['ymid']} type={audio_row['type']}")
                break
    if not audio_row:
        elapsed = time.time() - t0
        dprint("  ❌ 超时未检测到新音频轨")
        try:  # 失败留证截图
            from ui_core import save_screenshot
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_extract_fail.png')
            save_screenshot(win, p)
            dprint(f"  📸 失败截图: {p}")
        except Exception:
            pass
        return {'ok': False, 'duration_s': round(elapsed, 1),
                'error': '分离音频失败：主轨下方未出现新轨道'}

    # --- Step 5: 解除所有锁定和隐藏（阶段 6 边界：分离已完成，进入最终形态布局）---
    op_stage('6 草稿最终形态(只隐藏中文轨,其余解除隐藏)')
    dprint("\n[5/6] 解除所有轨道的锁定和隐藏")
    restore_result = restore_all_tracks(win, scan_hint=hint)
    if not restore_result.get('ok'):
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': '恢复轨道失败：仍有轨道锁定或隐藏'}

    # --- Step 6: 只隐藏中文字幕轨（从上到下第二条字幕轨）---
    dprint("\n[6/6] 隐藏中文字幕轨（从上到下第二条）")
    # 字幕轨 y 坐标与 [4/6] 那次扫描一致（隐藏/解锁都不引起行位移，已在 5.9 实测）
    # → 优先复用，避免再跑一次 ~10s 的 scan_tracks；簇形态不符则回退完整扫描
    subs2, eye_x = _subs_from_hint(hint)
    if subs2 is None:
        dprint("  [fallback] 复用行形态不符（顶部簇≠2 行），回退完整扫描")
        subs2, eye_x, win = _top_text_cluster(win)
    type_overrides = {}
    if len(subs2) >= 2:
        zh = subs2[1]
        type_overrides[zh['ymid']] = 'text'   # 隐藏后采不到像素，落盘时补正类型
        _set_row_hidden(win, eye_x, zh['ymid'], True, f"中文字幕轨 ymid={zh['ymid']}")
    else:
        dprint(f"  ⚠️ 字幕轨簇只有 {len(subs2)} 条，无法定位中文轨，跳过")

    # --- Step 7: 最终布局落盘（全程唯一一次完整 scan_tracks）---
    dprint("\n[7/7] 最终轨道布局写入 ui_calibration.json")
    _write_final_layout(win, audio_row, type_overrides)

    # --- 操作时间线：每个鼠标/键盘操作逐条耗时（阶段分组待用户定义）---
    if op_timeline('[extract] 操作时间线'):
        take_op_marks()

    elapsed = time.time() - t0
    dprint(f"\n[5] 分离音频 ✅完成  耗时 {elapsed:.1f}s")
    return {'ok': True, 'duration_s': round(elapsed, 1), 'error': None}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', default='.')
    parser.add_argument('--timeout', type=int, default=30)
    args = parser.parse_args()
    result = run_extract_audio(workdir=args.workdir, timeout_sec=args.timeout)
    print(f"\n结果: {result}")
    sys.exit(0 if result['ok'] else 1)
