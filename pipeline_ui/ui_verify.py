# -*- coding: utf-8 -*-
"""
UI 终检：三态对账（PASS / WARN / FAIL）
======================================
读轨引擎：scan_tracks（UIA + 颜色兜底）——与 pipeline 其它所有环节同一口径；
不再使用裸 classify_track_ys（纯 UIA 前缀匹配，实测 5.9 下 text/audio 片段零暴露）。

预期来源：calibration final_layout.tracks（阶段 6 落盘的布局），逐行对账 类型/y/隐藏状态。

判定三态：
  PASS：逐行对上（类型一致 + |dy|<=3 + 隐藏状态一致），且类型判据均为 UIA 级
  WARN：两路读数冲突 / 某行类型只能靠颜色兜底 / 隐藏行类型不可验 / y 漂移 3~25px
        / 预期外多出行 —— 记录并继续，不阻断 pipeline
  FAIL：预期有、实际扫不到（|dy|>25 视为缺轨），或隐藏状态确定不符，
        或无通道冲突的 UIA 类型确定不符 —— 阻断

段数语义（校准旧口径「video: 3 段」实为行数）：
  segments_uia = 该行 ±25px 内 UIA 片段节点数。实测（2026-11-03 探针）：5.9 全窗
  MTLS* 节点 7 个清一色 MTLSVideoP（白场/复合/第1~5集）；text 与分离 audio 片段
  无 UIA 节点暴露。故 text/audio 行存在性只能由「行检测+颜色兜底」给出，
  segments_uia=0 不代表缺轨。

路二（UIA 树结构判型）未采纳：MainTimeLineRoot 在 UIA 中是叶子节点
（探针 total_nodes=1，无子元素），Qt Quick 时间线内部渲染不可达，无结构判型依据。

结果落盘 workdir/_verify_report.json（每行对账 + 判据来源 + 置信度），
截图 verify_timeline.png 保留。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui_core import (
    dprint, connect_jianying, save_screenshot, op_stage, op_event,
    CalibLayoutMismatchError, _detect_page, get_window_rect,
    _resolve_prop, PROPERTY_ID_FULLDESC,
)
from ui_track_ops import (
    scan_tracks, classify_track_ys, classify_row_color, get_timeline_rect,
)
import ui_track_ops as _tops
from ui_calibration import get_param

Y_PASS = 3     # |dy| <= 3 → y 一致
Y_MATCH = 25   # |dy| <= 25 → 视为同一行；否则缺轨
X_PASS = 3     # |dx| <= 3 → x 锚点一致
_SEG_PREFIXES = ('MTLSVideoP:', 'MTLSTextP:', 'MTLSText:')


def _collect_uia_segments(win):
    """全窗 UIA 片段节点 → [(cat, cy)]（cat 口径与 classify_track_ys 相同，但不去重聚类）"""
    segs = []

    def scan(ctrl, depth=0):
        if depth > 20:
            return
        fd = (_resolve_prop(ctrl, PROPERTY_ID_FULLDESC) or '').strip()
        if fd.startswith(_SEG_PREFIXES):
            try:
                r = ctrl.BoundingRectangle
                cy = (int(r.top) + int(r.bottom)) / 2
            except Exception:
                cy = None
            if cy:
                label = fd.split(':', 1)[1] if ':' in fd else fd
                if fd.startswith('MTLSText'):
                    cat = 'text'
                elif 'pipeline_sx' in label.lower() or 'audio' in label.lower():
                    cat = 'audio'
                else:
                    cat = 'video'
                segs.append((cat, cy))
        try:
            for c in ctrl.GetChildren():
                scan(c, depth + 1)
        except Exception:
            pass

    scan(win)
    return segs


def _scan_with_evidence(win):
    """scan_tracks + 逐行双通道取证（终检/布局自检共用）。
    返回 (rows, img, WL, WT, WW, eye_x, tr, row_reports)。"""
    rows, img, WL, WT, WW, eye_x = scan_tracks(win)
    tr = get_timeline_rect(win)
    segs = _collect_uia_segments(win)
    uia_ys = classify_track_ys(win)

    row_reports = []
    for r in rows:
        ymid = r['ymid']
        uia_t, best = None, 30
        for t, ty in uia_ys:
            d = abs(ty - ymid)
            if d < best:
                best, uia_t = d, t
        color_t = classify_row_color(img, WL, WT, ymid, tr[0]) if tr else None
        seg_n = sum(1 for _c, cy in segs if abs(cy - ymid) <= Y_MATCH)
        if uia_t:
            src, conf = 'uia', 'high'
        elif r['hidden']:
            src, conf = 'none', 'unverifiable_hidden'
        elif r['type'] != 'unknown':
            src, conf = 'color', 'color-only'
        else:
            src, conf = 'none', 'none'
        row_reports.append({
            'ymid': ymid, 'type': r['type'], 'hidden': bool(r['hidden']),
            'uia_type': uia_t, 'color_type': color_t,
            'type_source': src, 'confidence': conf,
            'segments_uia': seg_n,
            'channel_conflict': bool(
                uia_t and color_t and (
                    (color_t == 'nontext' and uia_t == 'text')
                    or (color_t != 'nontext' and uia_t != color_t)
                )),
        })
    return rows, img, WL, WT, WW, eye_x, tr, row_reports


def _x_anchor_live(WL, WW, eye_x):
    """当次实测 x 锚点（屏幕绝对 px）：眼睛列/锁定列/图标列左右缘。"""
    sc = max(float(_tops._HDR_SCALE), 0.5)
    il, ir = _tops._icon_offsets()
    return {
        'wl': int(WL), 'ww': int(WW), 'scale': round(sc, 3),
        'eye_x': int(eye_x),
        'lock_x': int(_tops.get_lock_x(WL, WW)),
        'icon_l_offset': int(_tops.header_abs_x(WL, il)),
        'icon_r_offset': int(_tops.header_abs_x(WL, ir)),
    }


def run_verify(workdir: str = ".") -> dict:
    """UI 终检（三态对账）。ok = (state != 'FAIL')。"""
    op_stage('7 终检（UI 自动化检查）')
    t0 = time.time()
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)

    dprint("=" * 60)
    dprint("[6] UI 终检（三态对账）- 开始")
    dprint("=" * 60)

    win, _ = connect_jianying()

    # --- 预期布局（阶段 6 落盘） ---
    expected = get_param('final_layout', 'tracks', []) or []
    recorded_at = get_param('final_layout', 'recorded_at', None)

    # --- 实际扫描：scan_tracks（UIA+颜色兜底），另取两路原始读数交叉校验 ---
    rows, img, WL, WT, WW, eye_x, tr, row_reports = _scan_with_evidence(win)

    # --- 行级对账（预期 → 实际） ---
    diffs = []
    recon = []
    state = 'PASS'
    if not expected:
        state = 'WARN'
        diffs.append({'kind': 'no_baseline',
                      'detail': 'calibration 无 final_layout.tracks，退化为在位性粗查'})
    for exp in expected:
        ey = exp.get('ymid')
        cand = [rr for rr in row_reports if abs(rr['ymid'] - ey) <= Y_MATCH]
        if not cand:
            recon.append({'expected': exp, 'actual': None, 'verdict': 'fail',
                          'reasons': ['missing_track']})
            diffs.append({'kind': 'missing_track', 'expected': exp})
            state = 'FAIL'
            continue
        act = min(cand, key=lambda rr: abs(rr['ymid'] - ey))
        dy = act['ymid'] - ey
        reasons = []
        row_fail = False
        # 隐藏行无像素可测，type=unknown 时不判类型不符（另行标注不可验）
        if act['type'] != exp.get('type') and not (
                act['hidden'] and exp.get('hidden') and act['type'] == 'unknown'):
            reasons.append(f"type_mismatch(exp={exp.get('type')},act={act['type']})")
            if act['confidence'] == 'high' and not act['channel_conflict']:
                row_fail = True
        if bool(act['hidden']) != bool(exp.get('hidden')):
            reasons.append(f"hidden_mismatch(exp={exp.get('hidden')},act={act['hidden']})")
            row_fail = True
        if abs(dy) > Y_PASS:
            reasons.append(f'y_drift(dy={dy})')
        if act['confidence'] == 'color-only':
            reasons.append('type_color_only')
        if act['confidence'] == 'unverifiable_hidden':
            reasons.append('hidden_row_type_unverifiable')
        if act['channel_conflict']:
            reasons.append(f"channel_conflict(uia={act['uia_type']},color={act['color_type']})")
        # 确定的隐藏不符及无通道冲突的 UIA 类型不符 FAIL；低置信读数仍 WARN。
        verdict = 'fail' if row_fail else ('warn' if reasons else 'pass')
        if reasons:
            diffs.append({'kind': 'row_diff', 'ymid_exp': ey, 'reasons': reasons})
            if row_fail:
                state = 'FAIL'
            elif state == 'PASS':
                state = 'WARN'
        recon.append({'expected': exp, 'actual': act, 'dy': dy,
                      'verdict': verdict, 'reasons': reasons})

    # --- 反向：实际多出预期外的行 → WARN ---
    extra = [rr for rr in row_reports
             if not any(abs(rr['ymid'] - e.get('ymid', -999)) <= Y_MATCH
                        for e in expected)]
    if extra:
        diffs.append({'kind': 'extra_rows',
                      'detail': [f"{rr['type']}@{rr['ymid']}" for rr in extra]})
        if state == 'PASS':
            state = 'WARN'

    # --- 汇总（语义校准：行≠片段） ---
    by_type = {}
    for rr in row_reports:
        bt = by_type.setdefault(rr['type'], {'rows': 0, 'hidden': 0, 'segs_uia': 0})
        bt['rows'] += 1
        bt['hidden'] += 1 if rr['hidden'] else 0
        bt['segs_uia'] += rr['segments_uia']

    dprint("\n轨道对账结果（行级）:")
    for rc in recon:
        exp, act = rc['expected'], rc['actual']
        if act is None:
            dprint(f"  ❌ 缺轨: 预期 {exp.get('type')}@{exp.get('ymid')} 实际扫不到")
            continue
        mark = {'pass': '✅', 'warn': '⚠️', 'fail': '❌'}[rc['verdict']]
        dprint(f"  {mark} 行@{act['ymid']} type={act['type']}(src={act['type_source']}) "
               f"hidden={act['hidden']} dy={rc['dy']} 片段(UIA)={act['segments_uia']}"
               f"{' [' + ';'.join(rc['reasons']) + ']' if rc['reasons'] else ''}")
    for t, bt in sorted(by_type.items()):
        dprint(f"  汇总 {t}: {bt['rows']} 行（{bt['hidden']} 隐藏）/ UIA片段 {bt['segs_uia']} 个")

    # 兼容字段（真实读数，不再硬编码）
    results = {
        'video_track': by_type.get('video', {}).get('rows', 0) >= 1,
        'audio_track': by_type.get('audio', {}).get('rows', 0) >= 1,
        'text_track': (by_type.get('text', {}).get('rows', 0)
                       + sum(1 for rc in recon
                             if rc['expected'].get('type') == 'text'
                             and rc['actual'] and rc['actual']['type'] == 'unknown'
                             and rc['actual']['hidden'])) >= 1,
        'all_tracks_visible': not any(
            bool(rc['actual']['hidden']) != bool(rc['expected'].get('hidden'))
            for rc in recon if rc['actual']),
    }

    # --- 截图存档 ---
    scr_path = os.path.join(workdir, 'verify_timeline.png')
    scr = save_screenshot(win, scr_path)
    if scr:
        dprint(f"\n📸 终检时间线截图存档: {scr}")

    ok = state != 'FAIL'
    elapsed = time.time() - t0

    # S1-6：颜色阈值来源（json=calibration track_classify.*；hardcoded_default=内置实测值）
    color_thresh_src = ('json' if get_param('track_classify', 'text_r_over_gb', None)
                        is not None else 'hardcoded_default')

    # --- 结构化报告落盘 ---
    report = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'state': state,
        'expected_source': 'calibration:final_layout.tracks' if expected else None,
        'expected_recorded_at': recorded_at,
        'tolerance_px': {'y_pass': Y_PASS, 'y_match': Y_MATCH},
        'rows_expected': len(expected),
        'rows_scanned': len(row_reports),
        'summary_by_type': by_type,
        'recon': recon,
        'diffs': diffs,
        'results': results,
        'color_thresholds_source': color_thresh_src,
        'screenshot': scr_path if scr else None,
        'notes': [
            'segments_uia 语义: 该行±25px 内 UIA 片段节点数（5.9 仅 video 片段暴露 '
            'MTLSVideoP；text/分离 audio 片段无 UIA 节点，0 不代表缺轨）',
            '路二未采纳: MainTimeLineRoot 为 UIA 叶子节点(total_nodes=1)，'
            'timeline 内部结构不可达',
        ],
    }
    report_path = os.path.join(workdir, '_verify_report.json')
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    dprint(f"  📄 终检报告 → {report_path}")

    # --- 人类可读一行摘要 ---
    n_pass = sum(1 for rc in recon if rc['verdict'] == 'pass')
    n_warn = sum(1 for rc in recon if rc['verdict'] == 'warn')
    n_fail = sum(1 for rc in recon if rc['verdict'] == 'fail')
    icon = {'PASS': '✅', 'WARN': '⚠️', 'FAIL': '❌'}[state]
    dprint(f"\n[6] UI 终检 {icon}{state}  行对账 pass={n_pass} warn={n_warn} fail={n_fail}"
           f"（{len(row_reports)}/{len(expected)} 行在位）  耗时 {elapsed:.1f}s")
    dprint(f"  结果: {results}")

    return {
        'ok': ok,
        'state': state,
        'duration_s': round(elapsed, 1),
        'results': results,
        'report': report_path,
        'screenshot': scr_path if scr else None,
    }


# ================= 命中即自检（进入编辑页后、动 UI 前） =================

def _layout_write(workdir, payload):
    path = os.path.join(workdir, '_layout_check.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _layout_skip(workdir, reason, page, t0):
    """无法对账（判不出 edit 页/无基线/未渲染）→ WARN 跳过，不阻断。"""
    elapsed = time.time() - t0
    summary = f"[布局自检] ⚠️ 跳过（{reason}，耗时 {elapsed:.1f}s）"
    print(summary, flush=True)
    path = _layout_write(workdir, {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'state': 'SKIP', 'page': page, 'reason': reason,
        'elapsed_s': round(elapsed, 2), 'summary': summary,
    })
    op_event('layout_check', '命中即自检', status='warn',
             duration_ms=elapsed * 1000, state='SKIP', reason=reason,
             page=page, report=path)
    return {'ok': True, 'state': 'SKIP', 'reason': reason,
            'summary': summary, 'report': path,
            'elapsed_s': round(elapsed, 2)}


def check_layout(workdir: str = '.', win=None, page=None) -> dict:
    """命中即自检：进入编辑页后、动任何 UI 之前，实测时间线 vs calibration initial_layout.rows。

    判定口径（严格）：行数一致 + 每行 type 一致 + 每行 |dy|<=3 + hidden 一致
    + x 锚点（eye_x/lock_x/icon_l_offset/icon_r_offset）|dx|<=3 → 放行。
    actual type=unknown：期望该行 hidden=true 时允许（隐藏行本不可验）；否则 WARN 点名不阻断。
    dy=1~3 放行但记账（摘要标出偏差）。不符 → 抛 CalibLayoutMismatchError(CALIB_LAYOUT_MISMATCH)，
    消息带逐行差异（期望 type/ymid/hidden vs 实测 + x_anchor_drift）。
    判不出 edit 页 / 无 initial_layout / 扫不到行 → WARN 跳过（不硬失败）。
    """
    t0 = time.time()
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)

    if win is None:
        win, page = connect_jianying(retry=False)
    if not win:
        return _layout_skip(workdir, 'window_not_found（剪映窗口不可用）', page, t0)
    if page is None:
        page = _detect_page(win)
    if page != 'edit':
        return _layout_skip(workdir, f'not_edit_page(page={page})，无法对账', page, t0)

    expected = get_param('initial_layout', 'rows', []) or []
    if not expected:
        return _layout_skip(
            workdir,
            '无 initial_layout（本 calibration 无 initial_layout，跳过布局校验，请重跑准入补字段）',
            page, t0)

    scan_t0 = time.time()
    rows, img, WL, WT, WW, eye_x, tr, row_reports = _scan_with_evidence(win)
    scan_elapsed = time.time() - scan_t0
    if not row_reports:
        return _layout_skip(workdir, 'no_rows_scanned（时间线未渲染或扫描失败）', page, t0)

    # --- x 锚点校验（期望 = initial_layout 缓存的 eye/icon offset 绝对 px） ---
    recon, diffs, fails, warns, notes = [], [], [], [], []
    live_x = _x_anchor_live(WL, WW, eye_x)
    exp_x = {}
    for k in ('eye_x', 'lock_x', 'icon_l_offset', 'icon_r_offset'):
        v = get_param('initial_layout', k, None)
        exp_x[k] = v
        if v is None or live_x.get(k) is None:
            continue
        dx = int(live_x[k]) - int(v)
        if abs(dx) > X_PASS:
            fails.append(f"x 锚点漂移 {k}: 期望 {v} 实测 {live_x[k]} (dx={dx})")
            diffs.append({'kind': 'x_anchor_drift', 'field': k,
                          'expected': int(v), 'actual': int(live_x[k]), 'dx': int(dx)})

    used = set()
    for exp in expected:
        ey = exp.get('ymid')
        cand = [(i, rr) for i, rr in enumerate(row_reports)
                if i not in used and abs(rr['ymid'] - ey) <= Y_MATCH]
        if not cand:
            fails.append(f"缺失 {exp.get('type')}@{ey}")
            recon.append({'expected': exp, 'actual': None, 'dy': None,
                          'verdict': 'fail', 'reasons': ['row_missing'], 'notes': []})
            continue
        idx, act = min(cand, key=lambda t: abs(t[1]['ymid'] - ey))
        used.add(idx)
        dy = act['ymid'] - ey
        reasons, row_notes, row_fail = [], [], False
        exp_t, act_t = exp.get('type'), act['type']
        if act_t != exp_t:
            if act_t == 'unknown' and exp.get('hidden'):
                row_notes.append('type_unknown_allowed(hidden)')
            elif act_t == 'unknown':
                reasons.append(f'type_unknown(exp={exp_t})')
                warns.append(f"unknown@{act['ymid']}(exp={exp_t})")
            else:
                reasons.append(f'type_mismatch(exp={exp_t},act={act_t})')
                fails.append(f"type 不符 {exp_t}@{ey}→{act_t}@{act['ymid']}")
                row_fail = True
        if bool(act['hidden']) != bool(exp.get('hidden')):
            reasons.append(f"hidden_mismatch(exp={exp.get('hidden')},act={act['hidden']})")
            fails.append(f"hidden 不符 {exp_t}@{ey}: 期望 {exp.get('hidden')} "
                         f"实测 {act['hidden']}")
            row_fail = True
        if abs(dy) > Y_PASS:
            reasons.append(f'y_drift(dy={dy})')
            fails.append(f"dy 超差 {exp_t}@{ey}: {dy}")
            row_fail = True
        elif dy != 0:
            row_notes.append(f'dy={dy}')
            notes.append(f"@{act['ymid']} dy={dy}")
        recon.append({'expected': exp, 'actual': act, 'dy': dy,
                      'verdict': ('fail' if row_fail else ('warn' if reasons else 'pass')),
                      'reasons': reasons, 'notes': row_notes})

    extra = [rr for i, rr in enumerate(row_reports) if i not in used]
    if extra:
        fails.append(f"多出 {len(extra)} 行: "
                     + '、'.join(f"{r['type']}@{r['ymid']}" for r in extra))
        diffs.append({'kind': 'row_extra',
                      'detail': [f"{r['type']}@{r['ymid']}" for r in extra]})

    for rc in recon:
        if rc['actual'] is None:
            diffs.append({'kind': 'row_missing',
                          'expected': {'type': rc['expected'].get('type'),
                                       'ymid': rc['expected'].get('ymid'),
                                       'hidden': rc['expected'].get('hidden')}})
        elif rc['verdict'] != 'pass' or rc['notes']:
            a = rc['actual']
            diffs.append({'kind': 'row_diff',
                          'index': rc['expected'].get('idx', rc['expected'].get('index')),
                          'expected': {'type': rc['expected'].get('type'),
                                       'ymid': rc['expected'].get('ymid'),
                                       'hidden': rc['expected'].get('hidden')},
                          'actual': {'type': a['type'], 'ymid': a['ymid'],
                                     'hidden': a['hidden']},
                          'dy': rc['dy'], 'verdict': rc['verdict'],
                          'reasons': rc['reasons'], 'notes': rc['notes']})

    max_dy = max((abs(rc['dy']) for rc in recon if rc['actual'] is not None), default=0)
    elapsed = time.time() - t0
    drift = ('，偏差 ' + '、'.join(notes[:3])) if notes else ''
    payload = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'state': None, 'page': page,
        'expected_source': 'calibration:initial_layout.rows',
        'expected_recorded_at': get_param('initial_layout', 'recorded_at', None),
        'tolerance': {'y_pass': Y_PASS, 'y_match': Y_MATCH, 'x_pass': X_PASS},
        'rows_expected': len(expected),
        'rows_scanned': len(row_reports),
        'scan_elapsed_s': round(scan_elapsed, 2),
        'elapsed_s': round(elapsed, 2),
        'expected': expected,
        'expected_x': exp_x,
        'actual_x': live_x,
        'actual': [{'index': i, 'type': rr['type'], 'ymid': rr['ymid'],
                    'hidden': rr['hidden'], 'type_source': rr['type_source'],
                    'confidence': rr['confidence']}
                   for i, rr in enumerate(row_reports)],
        'recon': recon, 'diffs': diffs, 'summary': None,
    }

    if fails:
        state = 'FAIL'
        summary = (f"[布局自检] ❌ {len(expected)}→{len(row_reports)} 行，"
                   + '；'.join(fails))
        detail = '；'.join(
            f"行{rc['expected'].get('idx', rc['expected'].get('index'))}: 期望 "
            f"{rc['expected'].get('type')}@{rc['expected'].get('ymid')}"
            f"/hidden={rc['expected'].get('hidden')} vs "
            + ("实测 缺失" if rc['actual'] is None else
               f"实测 {rc['actual']['type']}@{rc['actual']['ymid']}"
               f"/hidden={rc['actual']['hidden']}")
            + f" 差异 {';'.join(rc['reasons']) if rc['reasons'] else '-'}"
            for rc in recon if rc['actual'] is None or rc['verdict'] != 'pass')
        payload.update(state=state, summary=summary)
        path = _layout_write(workdir, payload)
        print(summary, flush=True)
        op_event('layout_check', '命中即自检', status='fail',
                 duration_ms=elapsed * 1000, state=state,
                 rows_expected=len(expected), rows_scanned=len(row_reports),
                 max_dy=max_dy, diffs=fails[:10], report=path)
        raise CalibLayoutMismatchError(summary + ('；' + detail if detail else ''))

    if warns:
        state = 'WARN'
        summary = (f"[布局自检] ⚠️ {len(expected)}/{len(expected)} 行一致"
                   f"（最大dy={max_dy}{drift}，耗时 {elapsed:.1f}s；"
                   f"WARN: {'、'.join(warns)}）")
    else:
        state = 'PASS'
        summary = (f"[布局自检] ✅ {len(expected)}/{len(expected)} 行一致"
                   f"（最大dy={max_dy}{drift}，耗时 {elapsed:.1f}s）")
    payload.update(state=state, summary=summary)
    path = _layout_write(workdir, payload)
    print(summary, flush=True)
    op_event('layout_check', '命中即自检',
             status=('warn' if state == 'WARN' else 'ok'),
             duration_ms=elapsed * 1000, state=state,
             rows_expected=len(expected), rows_scanned=len(row_reports),
             max_dy=max_dy, warns=warns[:10], report=path)
    return {'ok': True, 'state': state, 'summary': summary,
            'report': path, 'elapsed_s': round(elapsed, 2),
            'max_dy': max_dy, 'recon': recon}


# ================= 面板锚点闸门（recognize / vocal） =================
#
# 为什么要单开一套参照：时间线闸门（check_layout）只在"进入编辑页、动 UI 之前"
# 可跑（比 initial_layout）；recognize / vocal 的锚点只有在对应面板打开时才存在，
# 起点态根本测不到，所以参照单独落在 calibration.panel_anchors（带 verified_at，
# 本轮实测写入），分别在"字幕面板已激活"与"音频面板已滚到人声分离"两个时刻校验。
# 关键约束（S4 教训）：参照值只能是实测键，禁止拿未验证常量当参照。
ANCHOR_TOL = 20   # 坐标容差：与 S5 运行期坐标写回守卫 ±20px 保持一致

# key → (类型, 比较策略)
#   point/abs  : x、y 都比，±ANCHOR_TOL
#   rect/abs   : 四条边都比，±ANCHOR_TOL
#   point/x    : 只比 x（面板滚动行的 y 会随滚动档位漂移，实测两次差 35px，比 y 会假拦）
#   point/x_rel: x 绝对比；y 转成"相对 vocal_switch_center 的偏移"再比（同滚不漂）
_ANCHOR_SPECS = {
    'recognize': (
        ('subtitle_tab_center',     'point', 'abs'),
        ('translate_switch_center', 'point', 'abs'),
        ('translate_dropdown_row',  'rect',  'abs'),
        ('start_recog_btn_center',  'point', 'abs'),
    ),
    'vocal': (
        ('audio_tab_center',        'point', 'abs'),
        ('vocal_switch_center',     'point', 'x'),
        ('vocal_mode_row_center',   'point', 'x_rel'),
    ),
}

_TRANSLATE_SWITCH_DX = -35   # 运行期 ensure_switch 的首选偏移（实测生效）
_TOAST_KW = ('处理', '等待', '加载')


def _ctrl_rect(_ctrl):
    try:
        r = _ctrl.BoundingRectangle
        return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    except Exception:
        return None


def _combo_rects(win, max_depth=25):
    """UIA 树里的 VEComboBox 行矩形（剪映下拉框类名固定，S5 单点实测同一取值）。"""
    out = []

    def walk(ctrl, depth=0):
        if depth > max_depth:
            return
        try:
            cls = ctrl.ClassName or ''
        except Exception:
            cls = ''
        if 'VEComboBox' in cls:
            r = _ctrl_rect(ctrl)
            if r:
                out.append(r)
        try:
            for ch in ctrl.GetChildren():
                walk(ch, depth + 1)
        except Exception:
            pass

    walk(win)
    return out


def _scaled_region(win, region):
    """窗口相对 OCR region（100% 口径）→ 当前窗口物理像素。

    换算基准 = 窗口物理尺寸/逻辑尺寸的**实测比值**（ui_core.ui_content_scale），
    不用 monitor dpi_scale（DPI 虚拟化下屏幕尺寸会被读成逻辑值，历史上踩过坑）。
    返回 (region|None, 原因)；拿不到比值返回 None，调用方必须按"不可测"处理，不许猜。
    """
    try:
        from ui_core import ui_content_scale
        s, why = ui_content_scale(win)
    except Exception as e:
        s, why = None, f'exception {e}'
    if not s:
        return None, why or 'unknown'
    return tuple(int(round(v * s)) for v in region), None


def _first_ocr_box(win, keyword, region, exclude_kw=()):
    """区域 OCR → 命中 keyword 且不含排除词的第一个文字框（绝对坐标）。"""
    from ocr_locate import _ocr_window
    for text, _c, _s, box in _ocr_window(win, region=region):
        if keyword not in text:
            continue
        if any(k in text for k in exclude_kw):
            continue
        return [int(v) for v in box], text
    return None, None


def measure_anchors(win, section, ref_section=None, preset=None):
    """实时测量某 section 的面板锚点。

    只返回真正测到的键——测不到的一律不出现在 values 里（绝不填兜底/历史值，
    否则闸门会拿兜底数据自证通过）。
    preset：调用方在「同一物理锚点处于可测量状态」时先测到的值（如字幕 Tab 只在
    面板未激活时露出文字，激活后标签隐藏），有值就不再重复 OCR。
    返回 (values, notes, raw)。
    """
    values, notes, raw = {}, [], {}
    if not win:
        return values, ['窗口不可用'], raw
    for _k, _v in (preset or {}).items():
        if _v:
            values[_k] = [int(_v[0]), int(_v[1])] if len(_v) == 2 else list(_v)
    if section == 'recognize':
        from ocr_locate import find_text_box, find_text_in_window, _ocr_window
        from ui_core import panel_edges
        # region 口径（B：4K 多栏布局）：
        #   字幕 Tab = 全宽带（宽=窗口宽、高≈150×s；该 Tab 随布局跳动，不能按原点×s 推）
        #   翻译语言标签 = 左面板 [左缘, 右缘] + y 150~240×s
        #   开始识别 = 左面板 [左缘, 右缘] + y 时间线顶 −150×s ~ 时间线顶
        pe = panel_edges(win)
        for w in pe.get('warn') or []:
            notes.append(f'面板边缘降级: {w}')
        s_f = None
        try:
            from ui_core import ui_content_scale
            s_f, why = ui_content_scale(win)
        except Exception as e:
            why = f'exception {e}'
        wr = get_window_rect(win)
        if not wr or not s_f:
            notes.append(f'窗口/比值不可测（{why}）→ recognize 锚点无法换算')
            return values, notes, raw
        WL, WT, WW, WH = wr[0], wr[1], wr[2] - wr[0], wr[3] - wr[1]
        left = pe.get('left_left')
        right = pe.get('left_right')
        if left is None or right is None:
            notes.append('面板左/右缘不可测 → 3 个 OCR 锚点 region 无法锚定')
        # 字幕 Tab：全宽带
        reg_tab = (0, int(round(20 * s_f)), WW, int(round(170 * s_f)))
        # 翻译语言标签：左面板上部
        reg_label = ((int(left) - WL, int(round(150 * s_f)),
                      int(right) - WL, int(round(240 * s_f))) if left is not None
                     and right is not None else None)
        # 开始识别：左面板下部、紧贴时间线上方
        tr = get_timeline_rect(win)
        tl = int(tr[1]) if tr else WT + int(round(700 * s_f))
        reg_btn = ((int(left) - WL, max(0, tl - int(round(150 * s_f))),
                    int(right) - WL, tl) if left is not None and right is not None else None)
        # 该带同时覆盖左面板首行操作项（「新建字幕」也含「字幕」，会抢在 Tab 前命中），
        # 故先取文本恰为「字幕」的候选（= 顶部 Tab），没有才退回包含匹配。
        # 面板激活后顶部 Tab 文字会隐藏（实测只剩左面板「新建字幕/识别字幕」），
        # 此时不能用包含匹配冒充 Tab —— 没测到就是不可测。
        if 'subtitle_tab_center' in values:
            notes.append('subtitle_tab_center 复用激活前实测值（面板激活后 Tab 文字隐藏）')
        else:
            cands = []
            for text, c, score, _b in _ocr_window(win, region=reg_tab):
                t = text.replace(' ', '').replace('\n', '')
                if '字幕' in t and not any(k in t for k in ('识别', '添加', '翻译', '智能', '新建')) \
                        and score >= 0.4:
                    cands.append((t, int(c[0]), int(c[1])))
            hit = None
            for t, x, y in cands:
                if t == '字幕':
                    hit = (x, y)
                    break
            if hit:
                values['subtitle_tab_center'] = [int(hit[0]), int(hit[1])]
            else:
                notes.append('subtitle_tab_center 全宽带 OCR 未命中「字幕」')
        box = find_text_box(win, '翻译语言', region=reg_label) if reg_label else None
        if box:
            cx = int((box[0] + box[2]) // 2)
            cy = int((box[1] + box[3]) // 2)
            values['translate_switch_center'] = [cx + _TRANSLATE_SWITCH_DX, cy]
            raw['translate_label_box'] = [int(v) for v in box]
        else:
            notes.append('translate_switch_center OCR 未命中「翻译语言」')
        # 下拉行 rect：只取左面板内的 VEComboBox（条件 = 右缘贴面板右缘）；
        # 有参照时按「标签框下沿 → 行顶」最近锁定同一物理行（y 不能拿 1920 参照直接比，
        # 4K 下行距 ×s 拉开：1920 行 cy=224 / 4K 行 cy=335）。
        combos = list(_combo_rects(win))
        if right is not None:
            combos = [r for r in combos if abs(int(r[2]) - int(right)) <= 8]
        else:
            combos = [r for r in combos if r[0] < WL + WW * 0.6]
        ref_row = (ref_section or {}).get('translate_dropdown_row')
        row = None
        if combos:
            if box:
                lb = int(box[3])
                row = min(combos, key=lambda r: abs(int(r[1]) - lb))
            elif ref_row:
                ref_cy = (int(ref_row[1]) + int(ref_row[3])) // 2 * float(s_f)
                row = min(combos, key=lambda r: abs((r[1] + r[3]) // 2 - ref_cy))
            else:
                cand = [r for r in combos if 180 <= (r[1] + r[3]) // 2 <= 400]
                row = max(cand, key=lambda r: r[2] - r[0]) if cand else None
        if row:
            values['translate_dropdown_row'] = list(row)
        else:
            notes.append('translate_dropdown_row 未找到左面板 VEComboBox 行')
        hit = find_text_in_window(win, '开始识别', region=reg_btn) if reg_btn else None
        if hit:
            values['start_recog_btn_center'] = [int(hit[0]), int(hit[1])]
        else:
            notes.append('start_recog_btn_center OCR 未命中「开始识别」')
        return values, notes, raw

    if section == 'vocal':
        from ocr_locate import find_text_box
        from ui_core import panel_edges
        wr = get_window_rect(win)
        if not wr:
            return values, ['窗口矩形不可用'], raw
        # get_window_rect 返回 (left, top, right, bottom) —— 这里显式算宽高，
        # 避免沿用其它模块 "WL,WT,WW,WH = get_window_rect()" 在非全屏窗口下的错位
        WL, WT = wr[0], wr[1]
        WW, WH = wr[2] - wr[0], wr[3] - wr[1]
        # region 里的设计常量同样按窗口物理/逻辑比值换算（WW/WH 已是实际窗口像素）
        reg_audio, why = _scaled_region(win, (0, 0, 0, 120))
        reg_switch, _w2 = _scaled_region(win, (0, 240, 0, 0))
        reg_mode, _w3 = _scaled_region(win, (0, 250, 0, 700))
        if reg_audio is None:
            notes.append(f'窗口物理/逻辑比值不可测（{why}）→ 3 个 OCR 锚点无法换算')
        # 右面板内容左缘锚定：左界 = 内容左缘 −20（4K=2999 / 1920=1374），
        # 替代 WW//2（4K=1920 → 1920×180 极端长宽比，实测偶发漏检）；
        # 拿不到内容左缘 → 退 WW//2 并显式记 note（不可测，不许静默）
        pe = panel_edges(win)
        for w in pe.get('warn') or []:
            notes.append(f'面板边缘降级: {w}')
        rl = pe.get('right_left')
        if rl is not None:
            vx0 = max(0, int(rl) - WL - 20)
            raw['vocal_region_left'] = f'uia:right_left-20 -> {vx0}'
        else:
            vx0 = WW // 2
            notes.append('右面板内容左缘不可测 → 音频锚点 region 退 WW//2')
            raw['vocal_region_left'] = f'fallback:WW//2 -> {vx0}'
        box = find_text_box(win, '音频',
                            region=(vx0, 0, WW, reg_audio[3])) if reg_audio else None
        if box:
            values['audio_tab_center'] = [int((box[0] + box[2]) // 2),
                                          int((box[1] + box[3]) // 2)]
        else:
            notes.append('audio_tab_center OCR 未命中「音频」')
        # 面板行 y 随滚动漂移 → 收紧到面板行区域（y>=240），并排除处理提示 toast
        box, txt = _first_ocr_box(win, '人声分离',
                                  (vx0, reg_switch[1], WW, WH) if reg_switch else None,
                                  exclude_kw=_TOAST_KW) if reg_switch else (None, None)
        if box:
            dx, dy = get_param('vocal_switch', 'checkbox_offset', [-3, 0])
            values['vocal_switch_center'] = [int(box[0] + int(dx)),
                                             int((box[1] + box[3]) // 2 + int(dy))]
            raw['vocal_label'] = txt
        else:
            notes.append('vocal_switch_center OCR 未命中「人声分离」')
        box = None
        for kw in ('仅保留人声', '仅保留背景声'):
            box, txt = _first_ocr_box(win, kw,
                                      (vx0, reg_mode[1], WW, min(WH, reg_mode[3]))
                                      if reg_mode else None,
                                      exclude_kw=_TOAST_KW) if reg_mode else (None, None)
            if box:
                values['vocal_mode_row_center'] = [int((box[0] + box[2]) // 2),
                                                   int((box[1] + box[3]) // 2)]
                raw['vocal_mode_label'] = txt
                break
        if box is None:
            notes.append('vocal_mode_row_center OCR 未命中模式选项')
        return values, notes, raw

    return values, [f'未知 section: {section}'], raw


def _layout_merge(workdir, payload):
    """把面板锚点闸门结果并进 _layout_check.json：
    只追加 anchor_checks，不改时间线闸门已落盘的顶层 state；
    仅当本次 FAIL 时才把顶层 state/summary 提升为 FAIL（闸门拦下时以失败为准）。"""
    path = os.path.join(workdir, '_layout_check.json')
    cur = {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cur = json.load(f) or {}
    except Exception:
        cur = {}
    cur.setdefault('anchor_checks', []).append(payload)
    if payload.get('state') == 'FAIL':
        cur['state'] = 'FAIL'
        cur['summary'] = payload.get('summary')
        cur['anchor_fail'] = payload
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    return path


def check_anchors(workdir: str = '.', section: str = 'recognize', win=None,
                  tol: int = ANCHOR_TOL, preset=None) -> dict:
    """面板锚点闸门：本次实测 vs calibration.panel_anchors.<section>（须带 verified_at）。

    返回 {ok, state, summary, diffs, values}；FAIL → 落盘 _layout_check.json（追加
    anchor_checks 并提升顶层 state）+ 打印 [布局自检] + op_event，再抛
    CalibLayoutMismatchError 阻断（与 check_layout 同一口径）。
    参照缺失（无 panel_anchors / 无 verified_at）→ SKIP(WARN) 不阻断（沿用"无基线不硬失败"）。
    """
    t0 = time.time()
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    specs = _ANCHOR_SPECS.get(section)
    if not specs:
        return {'ok': True, 'state': 'SKIP', 'summary': f'[布局自检] ⚠️ 未知 section={section}'}

    ref_section = get_param('panel_anchors', section, None)
    verified_at = get_param('panel_anchors', 'verified_at', None)
    if not isinstance(ref_section, dict) or not verified_at:
        summary = (f'[布局自检] ⚠️ 跳过 面板锚点（{section}）：'
                   f'calibration.panel_anchors.{section} 缺失或无 verified_at（无实测基线不硬失败）')
        print(summary, flush=True)
        op_event('layout_check', f'面板锚点（{section}）', status='warn',
                 state='SKIP', section=section, reason='no_verified_baseline')
        return {'ok': True, 'state': 'SKIP', 'summary': summary}

    values, notes, raw = measure_anchors(win, section, ref_section=ref_section, preset=preset)
    # 覆盖率口径：不可测 = 本次没测到（OCR 未命中 / 比值不可测），不是"参照缺失"
    keys = [k for k, _t, _m in specs]
    meas_keys = [k for k in keys if values.get(k) is not None]
    unmeas_keys = [k for k in keys if values.get(k) is None]
    noref_keys = [k for k in keys if ref_section.get(k) is None]
    comparable = [k for k in keys
                  if values.get(k) is not None and ref_section.get(k) is not None]
    coverage = (f'{section} {len(meas_keys)}/{len(keys)} 键'
                f'（实测 {len(meas_keys)} / 不可测 {len(unmeas_keys)}）')
    fails, diffs, skipped = [], [], []
    for key, kind, mode in specs:
        exp = ref_section.get(key)
        act = values.get(key)
        if exp is None:
            skipped.append(f'{key}(参照缺失)')
            continue
        if act is None:
            skipped.append(f'{key}(实测不可得)')
            continue
        if kind == 'rect':
            names = ('left', 'top', 'right', 'bottom')
            for i, nm in enumerate(names):
                d = int(act[i]) - int(exp[i])
                diffs.append({'kind': 'anchor_delta', 'key': key, 'axis': nm,
                              'expected': int(exp[i]), 'actual': int(act[i]), 'delta': d})
                if abs(d) > tol:
                    fails.append(f'{key}.{nm} 期望 {int(exp[i])} 实测 {int(act[i])} '
                                 f'(d{nm}={d:+d}, 阈值±{tol})')
        else:
            dx = int(act[0]) - int(exp[0])
            diffs.append({'kind': 'anchor_delta', 'key': key, 'axis': 'x',
                          'expected': int(exp[0]), 'actual': int(act[0]), 'delta': dx})
            if abs(dx) > tol:
                fails.append(f'{key}.x 期望 {int(exp[0])} 实测 {int(act[0])} '
                             f'(dx={dx:+d}, 阈值±{tol})')
            if mode == 'x_rel':
                # y 转相对偏移：两者同处滚动面板，滚动时一起位移，差值不漂
                sw_ref = ref_section.get('vocal_switch_center')
                if ('vocal_switch_center' in values
                        and isinstance(sw_ref, (list, tuple)) and len(sw_ref) >= 2):
                    dy = int(act[1]) - int(values['vocal_switch_center'][1])
                    dy_ref = int(exp[1]) - int(sw_ref[1])
                    diffs.append({'kind': 'anchor_delta', 'key': key, 'axis': 'y_rel',
                                  'expected': dy_ref, 'actual': dy,
                                  'delta': dy - dy_ref})
                    if abs(dy - dy_ref) > tol:
                        fails.append(f'{key}.y(相对开关行) 期望 {dy_ref} 实测 {dy} '
                                     f'(dy={dy - dy_ref:+d}, 阈值±{tol})')
                else:
                    skipped.append(f'{key}.y_rel(开关行不可得)')
            elif mode == 'abs':
                dy = int(act[1]) - int(exp[1])
                diffs.append({'kind': 'anchor_delta', 'key': key, 'axis': 'y',
                              'expected': int(exp[1]), 'actual': int(act[1]), 'delta': dy})
                if abs(dy) > tol:
                    fails.append(f'{key}.y 期望 {int(exp[1])} 实测 {int(act[1])} '
                                 f'(dy={dy:+d}, 阈值±{tol})')

    elapsed = time.time() - t0
    max_dev = max((abs(d.get('delta', 0)) for d in diffs), default=0)
    payload = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'section': section, 'state': None, 'summary': None,
        'expected_source': 'calibration:panel_anchors.%s' % section,
        'verified_at': verified_at, 'tolerance_px': tol,
        'expected': {k: ref_section.get(k) for k, _k, _m in specs},
        'actual': values, 'raw': raw, 'diffs': diffs,
        'unmeasurable': skipped, 'notes': notes,
        'coverage': {'total': len(keys), 'measured': len(meas_keys),
                     'unmeasurable': len(unmeas_keys), 'no_reference': len(noref_keys),
                     'unmeasurable_keys': unmeas_keys, 'no_reference_keys': noref_keys,
                     'comparable': len(comparable), 'text': coverage},
        'elapsed_s': round(elapsed, 2),
    }

    # 判绿规则（防"假绿"）：
    #   有超差 → FAIL
    #   没有任何可比对项（全部不可测/无参照）→ FAIL（零证据不给绿）
    #   部分不可测/无参照 → WARN，摘要必须点名是哪几个键没测到
    #   全部实测且全部比对 → PASS
    if fails or not comparable:
        state = 'FAIL'
        if fails:
            summary = (f'[布局自检] ❌ 面板锚点（{section}）{coverage}：'
                       + '；'.join(fails))
        else:
            summary = (f'[布局自检] ❌ 面板锚点（{section}）{coverage}：'
                       f'无可比对项（不可测 {len(unmeas_keys)} / 无参照 {len(noref_keys)}），'
                       f'零实测证据不判绿' +
                       (f'；未测到: {"、".join(unmeas_keys)}' if unmeas_keys else ''))
        payload.update(state=state, summary=summary)
        path = _layout_merge(workdir, payload)
        print(summary, flush=True)
        op_event('layout_check', f'面板锚点（{section}）', status='fail',
                 duration_ms=elapsed * 1000, state=state, section=section,
                 tolerance_px=tol, max_deviation=max_dev, diffs=fails[:10],
                 unmeasurable=unmeas_keys[:5], no_reference=noref_keys[:5], report=path)
        raise CalibLayoutMismatchError(summary)

    if unmeas_keys or noref_keys:
        state = 'WARN'
        parts = []
        if unmeas_keys:
            parts.append(f'不可测: {"、".join(unmeas_keys)}')
        if noref_keys:
            parts.append(f'无参照: {"、".join(noref_keys)}')
        summary = (f'[布局自检] ⚠️ 面板锚点（{section}）{coverage}；'
                   + '；'.join(parts)
                   + f'；其余 {len(comparable)} 项实测一致（最大偏差 {max_dev}px，'
                     f'耗时 {elapsed:.1f}s）')
    else:
        state = 'PASS'
        summary = (f'[布局自检] ✅ 面板锚点（{section}）{coverage} 全部一致'
                   f'（最大偏差 {max_dev}px，耗时 {elapsed:.1f}s）')
    payload.update(state=state, summary=summary)
    path = _layout_merge(workdir, payload)
    print(summary, flush=True)
    op_event('layout_check', f'面板锚点（{section}）',
             status=('warn' if state == 'WARN' else 'ok'),
             duration_ms=elapsed * 1000, state=state, section=section,
             tolerance_px=tol, max_deviation=max_dev,
             unmeasurable=skipped[:5], report=path)
    return {'ok': True, 'state': state, 'summary': summary, 'report': path,
            'diffs': diffs, 'values': values, 'unmeasurable': skipped,
            'elapsed_s': round(elapsed, 2)}

