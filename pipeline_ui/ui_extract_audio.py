# -*- coding: utf-8 -*-
"""
分离音频（Ctrl+Shift+S）+ 字幕轨收尾
======================================
用户确认的流程（7 步）：
  1. 隐藏并锁定识别字幕产生的两条字幕轨（顶部成簇 text 轨）
  2. Home + Ctrl+A 选中主轨道（ui_core.select_main_track，高频操作统一封装）
  3. Ctrl+Shift+S 分离音频
  4. 对账 draft_content.json 判分离是否真的成功（先持续采样到稳定态，再三项硬校验，
     见 _wait_separation_applied / _check_separation）
  5. 解除所有轨道的锁定和隐藏（restore_all_tracks）
  6. 先把时间线滚回顶层，再只隐藏中文字幕轨（顶层两条 text 轨中从上到下第二条，用户确认）
  7. 把最终所有轨道元素和坐标写入 ui_calibration.json（final_layout）供后续步骤使用

快捷键结论（2026-11 核对 + 用户实测）：
  「分离音频」= Ctrl+Shift+S，与剪映默认方案的键位一致。
  ⚠️ 配置文件里的绑定 ≠ 生效绑定，别照单个预设文件下结论：
    - User Data\\Config\\keymapSettings 里唯一的激活标记是 currentKeymapIndex=0，
      且与出厂 Config.zip 内的值完全相同 → 用户从未切换过快捷键方案，
      生效的是内置默认方案（不落 Shortcut 目录的 json）。
    - Config\\Shortcut\\ 下 5 个文件（Final Cut Pro X / Premiere Pro / Custom1-3）都是
      **非默认预设**：其中 Final Cut Pro X.json 把 Ctrl+Shift+S 绑到 toggleVideoAudio
      （视频↔音频切换），照它推断会得出"Ctrl+Shift+S 不是分离音频"的错误结论。
    - 默认方案里没有可查的"分离音频"命令键名（5 个预设里也没有 Detach/Separate 类
      命令），所以只认实测键位，不在代码里做"命令名→按键"的反查。

为什么不再用"主轨下方多出一条轨道"判成功：
  上一轮主轨 5 条视频片段被原地转成音频（videos 7→2、主轨 segments=0），
  界面上下方确实"多出一条 audio 轨"，旧判据把破坏判成了成功。
  现在只认 draft_content.json 的前后对账，UI 现象仅用于落盘坐标。

自愈：不在编辑页时自动打开最近草稿；UIA 窗口句柄失效时自动重连。
"""

from __future__ import annotations

import json
import time
import os
import sys

import pyautogui
pyautogui.FAILSAFE = False
import uiautomation as uia

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui_core import (
    dprint, connect_jianying, ensure_maximized, send_keys, select_main_track,
    op_mark, op_timeline, take_op_marks, op_stage,
)
from ui_track_ops import (
    scan_tracks, find_main_track, lock_track, restore_all_tracks,
    _set_row_hidden, is_row_locked, get_lock_x,
    scroll_timeline_vertical,
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


def _cluster_text_rows(rows):
    """顶部连续成簇的 text 轨 = 识别字幕产生的字幕轨（中文+英文）。

    与下方误分类的旧轨用 40px 逻辑间距断开（按 _HDR_SCALE 换算：1920 下 40px，
    4K@150% 下 60px，否则 4K 实测字幕行间距 42px 会被断成单条）。
    """
    gap = 40 * max(float(_HDR_SCALE), 0.5)
    text_rows = sorted([r for r in rows if r['type'] == 'text'], key=lambda r: r['ymid'])
    cluster = []
    for r in text_rows:
        if not cluster or r['ymid'] - cluster[-1]['ymid'] <= gap:
            cluster.append(r)
        else:
            break
    return cluster


def _top_text_cluster(win):
    """扫一次并返回顶部 text 簇。返回 (cluster_rows 按 ymid 升序, eye_x, win)。"""
    rows, _, _, _, _, eye_x, win = _safe_scan(win)
    return _cluster_text_rows(rows), eye_x, win


def _scroll_to_top(win, max_steps=8):
    """朝上滚时间线，直到三模态均无位移（=已到顶）。返回 (ok, why)。

    复用 scroll_timeline_vertical 的多模态轮转：reason='all_modes_failed' 表示
    keyboard / scrollbar / wheel 全都没有位移 —— 这正是"已经在顶层"的判据。
    """
    for i in range(max_steps):
        r = scroll_timeline_vertical(win, direction='up')
        if r.get('ok'):
            continue
        if r.get('reason') == 'all_modes_failed':
            dprint(f"  ✅ 已到顶层：第 {i + 1} 次上滚三模态均无位移"
                   f"（tried={[t.get('mode') for t in (r.get('tried') or [])]}）")
            return True, f'第 {i + 1} 次上滚三模态均无位移'
        return False, f'滚动不可用（{r.get("reason")}）'
    return False, f'连续上滚 {max_steps} 次仍有位移，未确认到顶'


def _top_text_cluster_checked(win):
    """回顶后复扫校验：最上面那簇 text 必须是 2 行（中文+英文字幕轨）。

    "顶层"指 text 轨里的最上一簇，不是"整条时间线最顶行"——实测预设组里可能有
    video 行排在 text 簇上方（例如 ymid 971 的 video 在 1042/1084 的 text 之上），
    按"最顶行必须是 text"判会无条件误报。
    返回 (cluster, eye_x, win, why)；why 为空串表示校验通过。
    """
    rows, _, _, _, _, eye_x, win = _safe_scan(win)
    cluster = _cluster_text_rows(rows)
    if not cluster:
        return cluster, eye_x, win, '扫描未采到任何 text 行'
    if len(cluster) < 2:
        return cluster, eye_x, win, (f'最上层 text 簇只有 {len(cluster)} 行（预期 2 行，'
                                     f'ymid={[r["ymid"] for r in cluster]}）')
    if len(cluster) > 2:
        return cluster, eye_x, win, (f'最上层 text 簇有 {len(cluster)} 行（预期 2 行，'
                                     f'ymid={[r["ymid"] for r in cluster]}）')
    return cluster, eye_x, win, ''


def _verify_zh_hidden(win, ymid):
    """隐藏后复扫确认原中文字幕行确实变成 hidden。返回 (ok, why, win)。"""
    rows, _, _, _, _, _, win = _safe_scan(win)
    if not rows:
        return False, '隐藏后复扫未采到任何轨道行', win
    hit = min(rows, key=lambda r: abs(r['ymid'] - ymid))
    if abs(hit['ymid'] - ymid) > 40:
        return False, f'复扫找不到原中文字幕行（ymid={ymid}，最近行 ymid={hit["ymid"]}）', win
    if not hit['hidden']:
        return False, f'中文字幕轨 ymid={ymid} 复检仍可见', win
    return True, f'ymid={ymid} hidden=True', win


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


# ============ 分离结果硬校验（draft_content.json 前后对账）============
SEPARATE_AUDIO_HOTKEY = ('ctrl', 'shift', 's')

# 终态采样（取代旧的"等到有变化 → 再等 2s 取一次"单点读数）
# 上一轮主轨 5→0 的破坏性落盘发生在按键后约 50s，任何 T+5s 的单点读数都看不见。
TERMINAL_SAMPLE_INTERVAL_S = 0.5   # 采样间隔（秒）
TERMINAL_STABLE_HOLD_S = 40.0      # 连续多久无落盘变化才算进入稳定态
TERMINAL_TIMEOUT_S = 180.0         # 总采样超时（秒）
TERMINAL_NOCHANGE_S = 30.0         # 这么久连一次变化都没有 → 快捷键没有生效
_SEP_KEY_SENDS = 0                 # 本次运行累计发送 Ctrl+Shift+S 的次数

# 不同版本/素材来源写"视频原声"用的字段名不一，命中任一即视为显式标记
_ORIGINAL_SOUND_KEYS = ('video_original_sound', 'is_video_original_sound',
                        'original_sound')


def _draft_path():
    """当前草稿 draft_content.json 路径（活动草稿 = 最近修改的那个）。"""
    try:
        from ui_recognize_translate import DRAFT_ROOT, _active_draft_name
        name = _active_draft_name()
        if not name:
            return None
        return os.path.join(DRAFT_ROOT, name, 'draft_content.json')
    except Exception as e:
        dprint(f"  ⚠️ 解析草稿路径失败: {type(e).__name__}: {e}")
        return None


def _main_track(tracks):
    """主轨 = 草稿里第一条 video 轨（track 数组自下而上，track0 即时间线最底部的主轨）。"""
    for t in tracks:
        if t.get('type') == 'video':
            return t
    return None


def _draft_sig(path):
    """分离前后对账用的草稿指纹（读不到/解析失败由调用方归类为失败）。"""
    with open(path, encoding='utf-8') as f:
        d = json.load(f)
    tracks = d.get('tracks') or []
    mats = d.get('materials') or {}
    videos = mats.get('videos') or []
    audios = mats.get('audios') or []
    main = _main_track(tracks)
    return {
        'mtime': os.path.getmtime(path),
        'size': os.path.getsize(path),          # 终态采样用：任何落盘都会变
        'n_tracks': len(tracks),
        'main_id': (main or {}).get('id'),
        'main_segments': len((main or {}).get('segments') or []),
        # 按 id 回查每条轨：分离后不能重新"找最底 video 行"，否则会认到下方预设轨
        'track_index': {t.get('id'): {'type': t.get('type'),
                                      'n_segments': len(t.get('segments') or [])}
                        for t in tracks if t.get('id')},
        'n_videos': len(videos),
        'video_paths': {v.get('path') for v in videos if v.get('path')},
        'video_ids': {v.get('id') for v in videos if v.get('id')},
        'audio_tracks': [{'id': t.get('id'), 'segments': t.get('segments') or []}
                         for t in tracks if t.get('type') == 'audio'],
        # 每条 audio 轨的段数（采样日志要能一眼看出"新增 audio 轨且片段指向视频原声"）
        'audio_shape': tuple(len(t.get('segments') or [])
                             for t in tracks if t.get('type') == 'audio'),
        'audios_by_id': {a.get('id'): a for a in audios if a.get('id')},
    }


def _audio_from_video(seg, mat, sig):
    """该音频片段是否来自视频原声 → (ok, 证据说明)。

    证据按可靠度排序，命中任一即真：
      ① 显式标记字段（video_original_sound 等；存在但为 False 时直接判否）
      ② 片段/素材里有字段指向某条视频素材（分离音轨常复用视频素材 id，只取音频流）
      ③ 素材 type=extract（剪映"提取音频"写出的类型）
      ④ 素材 path 与某条视频素材同源
    """
    seen = []
    for holder_name, holder in (('segment', seg), ('material', mat)):
        for key in _ORIGINAL_SOUND_KEYS:
            if key in holder:
                seen.append(f'{holder_name}.{key}={holder.get(key)}')
                if holder.get(key):
                    return True, seen[-1]
    if seen:
        return False, '; '.join(seen)
    for holder_name, holder in (('segment', seg), ('material', mat)):
        for key, val in holder.items():
            if isinstance(val, str) and val in sig['video_ids']:
                return True, f'{holder_name}.{key} 指向视频素材'
    mtype = str(mat.get('type') or '').lower()
    if 'original_sound' in mtype:
        # 分离音频写出的素材类型就叫 video_original_sound（上一轮破坏性签名同此）
        return True, f'material.type={mat.get("type")}'
    if mtype in ('extract', 'video_extract'):
        return True, f'material.type={mat.get("type")}'
    if mat.get('path') and mat.get('path') in sig['video_paths']:
        return True, 'material.path 与视频素材同源'
    return False, '无原声标记'


def _check_separation(before, after):
    """分离结果严格对账 → (ok, detail)。三条同时成立才算成功，缺一即失败。

      ① 主轨 segments 没减少：按**主轨 id** 回查（不能重新按"最底 video 行"找——
         上一轮主轨片段被转成音频后，最底 video 行已换成下方预设轨，重新找会认错轨）；
      ② materials.videos 数量没减少（片段被转成音频时剪映会回收视频素材，
         上一轮实测 7→2，"分离"绝不会发生）；
      ③ 新增的确实是 audio 轨，且其片段对应的素材是视频原声（video_original_sound）。
    """
    main_id = before.get('main_id')
    now = after['track_index'].get(main_id) if main_id else None
    if not main_id or now is None:
        return False, f'主轨(id={main_id})在分离后消失'
    if now['type'] != 'video':
        return False, f'主轨被改成 {now["type"]}（应为 video）'
    if now['n_segments'] < before['main_segments']:
        return False, f'主轨 segments 减少 {before["main_segments"]}→{now["n_segments"]}'
    if after['n_videos'] < before['n_videos']:
        return False, (f'materials.videos 减少 {before["n_videos"]}→{after["n_videos"]}'
                       f'（视频片段被转成了音频）')
    old_ids = {t['id'] for t in before['audio_tracks']}
    new_tracks = [t for t in after['audio_tracks'] if t['id'] not in old_ids]
    if not new_tracks:
        return False, (f'未新增 audio 轨（audio 轨 '
                       f'{len(before["audio_tracks"])}→{len(after["audio_tracks"])}）')
    for t in new_tracks:
        for seg in t['segments']:
            mat = after['audios_by_id'].get(seg.get('material_id')) or {}
            ok, why = _audio_from_video(seg, mat, after)
            if ok:
                return True, f'新增 audio 轨 id={t["id"]}（{why}）'
    return False, (f'新增 audio 轨 {len(new_tracks)} 条，但没有任何片段对应 '
                   f'video_original_sound（新轨段数='
                   f'{[len(t["segments"]) for t in new_tracks]}，'
                   f'audios={len(after["audios_by_id"])}）')


def _state_key(sig):
    """终态采样用的可比指纹：草稿一有落盘就会变（字节数/轨道/主轨段/视频素材/音频段）。"""
    return (sig['size'], sig['n_tracks'], sig['main_segments'],
            sig['n_videos'], sig['audio_shape'])


def _fmt_ts(ts):
    return time.strftime('%H:%M:%S', time.localtime(ts)) + f'.{int((ts % 1) * 1000):03d}'


def _destroyed_reason(before, sig):
    """破坏性签名 → 原因串；健康返回 None。

    四条都是上一轮实测过的"视频被吃掉"形态，任一命中就不许再往下走：
      ① 主轨段数减少（按主轨 id 回查，不重新找最底 video 行）
      ② 主轨被清空
      ③ materials.videos 减少（视频片段被换成音频素材）
      ④ 新增 audio 段指向 video_original_sound，且原有视频素材 id 消失
    """
    main_id = before.get('main_id')
    now = sig['track_index'].get(main_id) if main_id else None
    if not main_id or now is None:
        return f'主轨(id={main_id})在分离后消失'
    if now['type'] != 'video':
        return f'主轨被改成 {now["type"]}（应为 video）'
    if now['n_segments'] == 0 and before['main_segments'] > 0:
        return f'主轨被清空（{before["main_segments"]}→0 段）'
    if now['n_segments'] < before['main_segments']:
        return f'主轨段数减少 {before["main_segments"]}→{now["n_segments"]}'
    if sig['n_videos'] < before['n_videos']:
        return (f'materials.videos 减少 {before["n_videos"]}→{sig["n_videos"]}'
                f'（视频片段被换成了音频素材）')
    old_ids = {t['id'] for t in before['audio_tracks']}
    new_tracks = [t for t in sig['audio_tracks'] if t['id'] not in old_ids]
    gone = set(before.get('video_ids') or ()) - set(sig['video_ids'])
    if new_tracks and gone:
        for t in new_tracks:
            for seg in t['segments']:
                mat = sig['audios_by_id'].get(seg.get('material_id')) or {}
                ok, why = _audio_from_video(seg, mat, sig)
                if ok:
                    return (f'新增 video_original_sound 音频段顶替视频（{why}），'
                            f'原视频素材减少 {len(gone)} 个')
    return None


def _send_separate_hotkey(win):
    """发送分离快捷键并计数（采样日志要能回答"这段时间里有没有第二次键事件"）。"""
    global _SEP_KEY_SENDS
    op_mark('组合键', 'Ctrl+Shift+S（分离音频）')
    _key_inject_evidence(win, SEPARATE_AUDIO_HOTKEY)
    pyautogui.hotkey(*SEPARATE_AUDIO_HOTKEY)
    _SEP_KEY_SENDS += 1
    dprint(f"  ⌨️ Ctrl+Shift+S 已发送（本次运行累计 {_SEP_KEY_SENDS} 次）")
    return _SEP_KEY_SENDS


def _wait_separation_applied(path, before, timeout_sec=None):
    """按键后**持续采样** draft_content.json，直到稳定态 / 破坏性签名 / 超时。

    旧实现是"等到有变化 → 再等 2s 取一次"，等于拿中间态读数判成功；上一轮主轨
    5→0 的落盘发生在按键后约 50s，单点读数永远看不见。现在逐拍留日志：
      [采样 NN +X.Xs] HH:MM:SS.mmm size=… main=…段 videos=… audio_tracks=… audio_segs=(…)
    返回 {'state','sig','samples','first_drop','stable_at','reason','key_sends'}：
      stable     连续 TERMINAL_STABLE_HOLD_S 无变化（且期间确有落盘变化）
      destroyed  命中破坏性签名（调用方必须立刻失败 + 回滚，不得 continue）
      timeout    总超时，或 TERMINAL_NOCHANGE_S 内连一次变化都没有（键没生效）
    """
    timeout = TERMINAL_TIMEOUT_S if timeout_sec is None else float(timeout_sec)
    t_start = time.time()
    deadline = t_start + timeout
    prev_key = _state_key(before)
    last_sig = before
    last_change_at = t_start
    changed_any = False
    first_drop = None
    n = 0
    dprint(f"  [终态采样] 间隔 {TERMINAL_SAMPLE_INTERVAL_S}s / 稳定判定 "
           f"{TERMINAL_STABLE_HOLD_S:.0f}s / 超时 {timeout:.0f}s（按键后立即开始）")
    while True:
        time.sleep(TERMINAL_SAMPLE_INTERVAL_S)
        now = time.time()
        n += 1
        try:
            sig = _draft_sig(path)
        except Exception:
            dprint(f"  [采样 {n:02d} +{now - t_start:5.1f}s] {_fmt_ts(now)} "
                   f"读盘失败（剪映正在写盘，下一拍重试）")
            if now >= deadline:
                break
            continue
        key = _state_key(sig)
        changed = key != prev_key
        shape = ' '.join(str(x) for x in sig['audio_shape']) or '-'
        dprint(f"  [采样 {n:02d} +{now - t_start:5.1f}s] {_fmt_ts(now)} "
               f"size={sig['size']} mtime={_fmt_ts(sig['mtime'])} "
               f"main={sig['main_segments']}段 videos={sig['n_videos']} "
               f"audio_tracks={len(sig['audio_shape'])} audio_segs=({shape})"
               f"{' ←变化' if changed else ''}")
        if sig['main_segments'] < before['main_segments'] and first_drop is None:
            first_drop = (now, now - t_start,
                          f'主轨 {before["main_segments"]}→{sig["main_segments"]} 段')
            dprint(f"  🧨 首次读到主轨段数下降 @ {_fmt_ts(now)}"
                   f"（按键后 +{now - t_start:.1f}s，Ctrl+Shift+S 累计 "
                   f"{_SEP_KEY_SENDS} 次）")
        if changed:
            changed_any = True
            last_change_at = now
        prev_key = key
        last_sig = sig

        bad = _destroyed_reason(before, sig)
        if bad:
            dprint(f"  🧨 破坏性签名 @ {_fmt_ts(now)}（按键后 +{now - t_start:.1f}s）：{bad}"
                   f"；共采样 {n} 次，Ctrl+Shift+S 累计 {_SEP_KEY_SENDS} 次")
            return {'state': 'destroyed', 'sig': sig, 'samples': n,
                    'first_drop': first_drop, 'stable_at': None, 'reason': bad,
                    'key_sends': _SEP_KEY_SENDS}
        if changed_any and now - last_change_at >= TERMINAL_STABLE_HOLD_S:
            dprint(f"  ✅ 终态稳定：连续 {TERMINAL_STABLE_HOLD_S:.0f}s 无落盘变化"
                   f"（末次变化 @ {_fmt_ts(last_change_at)}，共采样 {n} 次，"
                   f"Ctrl+Shift+S 累计 {_SEP_KEY_SENDS} 次）")
            return {'state': 'stable', 'sig': sig, 'samples': n,
                    'first_drop': first_drop, 'stable_at': now, 'reason': '',
                    'key_sends': _SEP_KEY_SENDS}
        if not changed_any and now - t_start >= TERMINAL_NOCHANGE_S:
            dprint(f"  ⚠️ 终态采样：{TERMINAL_NOCHANGE_S:.0f}s 内草稿无任何变化"
                   f"（共采样 {n} 次，Ctrl+Shift+S 累计 {_SEP_KEY_SENDS} 次）")
            return {'state': 'timeout', 'sig': last_sig, 'samples': n,
                    'first_drop': None, 'stable_at': None,
                    'reason': '草稿无任何变化（快捷键未生效或焦点不在时间线）',
                    'key_sends': _SEP_KEY_SENDS}
        if now >= deadline:
            break
    dprint(f"  ⚠️ 终态采样超时：{timeout:.0f}s 内未进入稳定态"
           f"（共采样 {n} 次，末次变化 @ {_fmt_ts(last_change_at)}，"
           f"Ctrl+Shift+S 累计 {_SEP_KEY_SENDS} 次）")
    return {'state': 'timeout', 'sig': last_sig, 'samples': n,
            'first_drop': first_drop, 'stable_at': None,
            'reason': f'{timeout:.0f}s 内草稿未进入稳定态',
            'key_sends': _SEP_KEY_SENDS}


def _key_inject_evidence(win, keys):
    """发送快捷键前的只读取证：前台窗口/进程、焦点控件、残留修饰键、实际按键序列。

    为什么要这条：默认方案下 Ctrl+Shift+S 的语义就是分离音频（用户实测），但上一轮
    自动化发出的同一键位却造成了"视频转音频"。这两种现象无法靠推理区分，只能靠现场
    读数判断是"键位理解错"还是"注入层面差异"（前台窗口不对 / 焦点在轨道头 / 修饰键卡住）。
    只读采集，不改任何状态。
    """
    lines = []
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        title = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            import psutil
            proc = psutil.Process(pid.value).name()
        except Exception:
            proc = f'(psutil 不可用, pid={pid.value})'
        jy_hwnd = int(getattr(win, 'NativeWindowHandle', 0) or 0)
        lines.append(f"前台窗口: hwnd={hwnd} title={title.value!r} class={cls.value!r} "
                     f"进程={proc} pid={pid.value} 是否剪映={hwnd == jy_hwnd}(剪映hwnd={jy_hwnd})")
    except Exception as e:
        lines.append(f"前台窗口取证失败: {type(e).__name__}: {e}")

    try:
        focused = uia.GetFocusedControl()
        if focused is not None:
            try:
                r = focused.BoundingRectangle
                rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
            except Exception:
                rect = None
            fd = ''
            try:
                fd = focused.GetPropertyValue(30159) or ''
            except Exception:
                pass
            lines.append(f"焦点控件: type={focused.ControlTypeName!r} name={focused.Name!r} "
                         f"desc={fd!r} rect={rect}")
        else:
            lines.append("焦点控件: (None)")
    except Exception as e:
        lines.append(f"焦点控件取证失败: {type(e).__name__}: {e}")

    try:
        import ctypes
        u = ctypes.windll.user32
        mods = ((0xA2, 'LCtrl'), (0xA3, 'RCtrl'), (0xA0, 'LShift'), (0xA1, 'RShift'),
                (0xA4, 'LAlt'), (0xA5, 'RAlt'))
        stuck = [nm for vk, nm in mods if u.GetAsyncKeyState(vk) & 0x8000]
        lines.append(f"按键前残留修饰键: {stuck if stuck else '无'}")
    except Exception as e:
        lines.append(f"修饰键取证失败: {type(e).__name__}: {e}")

    seq = [str(k) for k in keys]
    lines.append(f"实际发送键序列: pyautogui.hotkey({seq}) "
                 f"→ keyDown {'→'.join(seq)}，keyUp {'→'.join(reversed(seq))}")
    for ln in lines:
        dprint(f"  [key-evidence] {ln}")


def run_extract_audio(workdir: str = ".", timeout_sec: float = TERMINAL_TIMEOUT_S) -> dict:
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

    def _fail(msg):
        """失败退出：留证截图 + 统一返回结构（错误文案即 failure_class 的输入）。"""
        dprint(f"  ❌ {msg}")
        try:
            from ui_core import save_screenshot
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_extract_fail.png')
            save_screenshot(win, p)
            dprint(f"  📸 失败截图: {p}")
        except Exception:
            pass
        return {'ok': False, 'duration_s': round(time.time() - t0, 1), 'error': msg}

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
        return _fail('未定位主轨（无可用 video 行或 M2 封面校验未过），拒绝用下方预设轨顶替')
    select_main_track()

    # --- Step 3: Ctrl+Shift+S 分离音频 ---
    dprint("\n[3/6] Ctrl+Shift+S 分离音频")
    # 键位结论见模块 docstring：默认方案下 Ctrl+Shift+S = 分离音频（用户实测）。
    # 别被 Shortcut\Final Cut Pro X.json 里同键位绑 toggleVideoAudio 误导——那不是生效方案。
    # 物理按键：UIA SendKeys 对剪映组合快捷键不可靠（5.9 实测不触发）
    draft_path = _draft_path()
    try:
        before_sig = _draft_sig(draft_path) if draft_path else None
    except Exception as e:
        dprint(f"  ❌ 分离前读草稿失败: {type(e).__name__}: {e}")
        before_sig = None
    if not before_sig:
        return _fail('分离音频失败：读不到当前草稿 draft_content.json，无法做分离前后对账')
    dprint(f"  分离前草稿: 主轨(id={before_sig['main_id']}) "
           f"{before_sig['main_segments']} 段，videos={before_sig['n_videos']}，"
           f"audio 轨={len(before_sig['audio_tracks'])}，总轨={before_sig['n_tracks']}")
    _send_separate_hotkey(win)
    time.sleep(1)

    # --- Step 4: 终态校验：持续采样到稳定态后再对账（不看界面现象）---
    dprint(f"\n[4/6] 校验分离终态（持续采样 draft，稳定判定 "
           f"{TERMINAL_STABLE_HOLD_S:.0f}s / 超时 {timeout_sec:.0f}s）")
    res = _wait_separation_applied(draft_path, before_sig, timeout_sec)
    if res['state'] == 'destroyed':
        if res.get('first_drop'):
            when = (f'{_fmt_ts(res["first_drop"][0])}（按键后 +{res["first_drop"][1]:.1f}s，'
                    f'{res["first_drop"][2]}）')
        else:
            when = '未知'
        return _fail('分离音频失败：终态质检未通过 —— ' + res['reason'] +
                     f'；破坏性签名首次出现于 {when}，Ctrl+Shift+S 累计发送 '
                     f'{res["key_sends"]} 次；已停止，不进入 restore / 隐藏字幕'
                     f'（逐拍读数见上方 [采样] 行）')
    if res['state'] != 'stable':
        return _fail('分离音频失败：终态采样超时 —— ' + res['reason'] +
                     f'；共采样 {res["samples"]} 次，Ctrl+Shift+S 累计发送 '
                     f'{res["key_sends"]} 次（逐拍读数见上方 [采样] 行）')
    after_sig = res['sig']
    sep_ok, sep_detail = _check_separation(before_sig, after_sig)
    if not sep_ok:
        return _fail(f'分离音频失败：终态质检未通过 —— {sep_detail}'
                     f'（稳定落盘 @ {_fmt_ts(res["stable_at"])}，'
                     f'采样 {res["samples"]} 次）')
    dprint(f"  ✅ 分离校验通过: {sep_detail}"
           f"（videos {before_sig['n_videos']}→{after_sig['n_videos']}，"
           f"主轨 {before_sig['main_segments']}→"
           f"{after_sig['track_index'][before_sig['main_id']]['n_segments']} 段；"
           f"稳定落盘 @ {_fmt_ts(res['stable_at'])}，采样 {res['samples']} 次，"
           f"Ctrl+Shift+S 累计 {res['key_sends']} 次）")

    # 界面侧只用于定位"分离出的音频行"供落盘；定位不到不影响成功判定
    rows1, img1, WL1, WT1, WW1, eye_x1, win = _safe_scan(win)
    hint = (rows1, img1, WL1, WT1, WW1, eye_x1) if rows1 else None
    audio_row = None
    main_now, _ = find_main_track(win, rows1, img1) if rows1 else (None, False)
    if main_now:
        below = [r for r in rows1 if r['ymid'] > main_now['ymid']]
        audio_row = below[-1] if below else None
    if audio_row:
        dprint(f"  📍 分离出的音频行: ymid={audio_row['ymid']} type={audio_row['type']}")
    else:
        dprint("  ⚠️ 界面未定位到主轨下方的新音频行（草稿校验已通过，布局落盘时不记 separated_audio）")

    # --- Step 5: 解除所有锁定和隐藏（阶段 6 边界：分离已完成，进入最终形态布局）---
    op_stage('6 草稿最终形态(只隐藏中文轨,其余解除隐藏)')
    dprint("\n[5/6] 解除所有轨道的锁定和隐藏")
    # expected_tracks 由 restore_all_tracks 直接读 draft_content.json 的真实轨道数：
    # 旧口径 len(rows0)+1 受滚动视口影响，视口外的轨道会被当成不存在，永远差一条。
    restore_result = restore_all_tracks(win, scan_hint=hint)
    if not restore_result.get('ok'):
        reason = restore_result.get('reason')
        if reason == 'lock_unresponsive':
            detail = '; '.join(
                f"{u.get('kind')}@ymid={u.get('click_ymid')}"
                f"(type={(u.get('evidence') or {}).get('type')},"
                f"track={(u.get('evidence') or {}).get('track_guess')})"
                for u in (restore_result.get('unresponsive') or []))
            err = ('恢复轨道失败：点击前已重扫定位（排除坐标过期），点击后状态仍不翻转，'
                   f'判定该行不响应锁列/眼睛点击，已停止盲点。明细: {detail}')
        elif reason == 'coverage_insufficient':
            pre = restore_result.get('prescan') or {}
            err = (f"恢复轨道失败：恢复前覆盖预扫只扫到 {pre.get('count')}/"
                   f"{restore_result.get('expected_tracks')} 条轨道"
                   f"（steps={pre.get('steps')}，ambiguous={pre.get('ambiguous')}），"
                   '未达到全量覆盖，已按约定一次都不点（请人工滚动时间线后复跑）')
        elif reason == 'scroll_unavailable':
            err = ('恢复轨道失败：仍有轨道在时间线可视区外，滚轮/滚动条/键盘均无法滚动，'
                   '无法判定是锁定还是不可见（请人工滚动时间线后复跑）')
        else:
            err = '恢复轨道失败：仍有轨道锁定或隐藏'
            if restore_result.get('still_locked') or restore_result.get('still_hidden'):
                err += (f"（仍锁定={restore_result.get('still_locked')} "
                        f"仍隐藏={restore_result.get('still_hidden')}）")
        return {'ok': False, 'duration_s': round(time.time() - t0, 1),
                'error': err, 'restore': restore_result}

    # --- Step 6: 先回顶层，再只隐藏中文字幕轨（顶层两条 text 轨中从上到下第二条）---
    dprint("\n[6/6] 回顶层 → 隐藏中文字幕轨（从上到下第二条）")
    # 为什么必须先回顶：分离音频后时间线可能停在下方轨道，复用分离前的旧 ymid 会点到
    # 别的行（上一轮点到了 sticker 轨）。这里先滚到顶，再以**当前扫描**的坐标点，一次都不猜。
    top_ok, top_why = _scroll_to_top(win)
    if not top_ok:
        return _fail(f'隐藏字幕轨失败：top_not_reached —— {top_why}；一次都不点')
    subs2, eye_x, win, cluster_why = _top_text_cluster_checked(win)
    if cluster_why:
        return _fail(f'隐藏字幕轨失败：top_cluster_mismatch —— {cluster_why}；'
                     f'一次都不点')
    zh = subs2[1]
    type_overrides = {zh['ymid']: 'text'}   # 隐藏后采不到像素，落盘时补正类型
    dprint(f"  中文字幕轨候选: 顶层 text 簇 ymid={[r['ymid'] for r in subs2]} "
           f"→ 取第 2 行 ymid={zh['ymid']}")
    _set_row_hidden(win, eye_x, zh['ymid'], True, f"中文字幕轨 ymid={zh['ymid']}")
    hid_ok, hid_why, win = _verify_zh_hidden(win, zh['ymid'])
    if not hid_ok:
        return _fail(f'隐藏字幕轨失败：终态质检未通过 —— {hid_why}')
    dprint(f"  ✅ 中文字幕轨已隐藏（命中 ymid={zh['ymid']}，{hid_why}）")

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
    parser.add_argument('--timeout', type=float, default=TERMINAL_TIMEOUT_S)
    args = parser.parse_args()
    result = run_extract_audio(workdir=args.workdir, timeout_sec=args.timeout)
    print(f"\n结果: {result}")
    sys.exit(0 if result['ok'] else 1)
