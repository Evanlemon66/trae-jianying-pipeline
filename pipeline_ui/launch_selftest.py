# -*- coding: utf-8 -*-
"""launch_selftest.py — 剪映冷启动稳定性自测（唯一拉起入口：ensure_for_export）。

判据三级（只有第三级达成才算成功）：
  1) 进程起来（ensure_for_export 内部 spawn 成功且进程存活）
  2) 主窗口可交互（ensure_for_export 返回 True = 首页真能枚举出可交互控件）
  3) 草稿确实进入编辑页（点击草稿卡片后 connect_jianying 判定 edit）
前两级达成、第三级没成，按「秒退 / 等不到窗口 / 进不去编辑页」分别归类。

每次尝试都是真冷启动：先优雅关闭剪映并确认退出，再走统一入口拉起。
只读草稿：记录 draft_content.json 的 sha256/大小/轨道数，不写回任何内容。

用法（在 pipeline_ui 目录或仓库根均可）:
    python pipeline_ui/launch_selftest.py --runs 5 --draft ui_pipeline_ac
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_HERE, os.path.join(_ROOT, 'lib'), os.path.join(_ROOT, 'pipeline'),
           os.path.join(_ROOT, '.trae', 'skills', 'jianying-editor', 'examples')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ui_core  # noqa: E402
import ui_nav  # noqa: E402
import jy_instance  # noqa: E402
import jy_page_detect as jpd  # noqa: E402
import jianying_proc as jp  # noqa: E402
from export_config import get_projects_root  # noqa: E402

EVIDENCE_DIR = os.path.join(_HERE, '_evidence_20261115')
LEDGER_PATH = os.path.join(_ROOT, 'pipeline', '_work', 'launch_ledger.jsonl')


def _clock(epoch) -> str:
    """epoch 秒 → HH:MM:SS.mmm（台账/trace 同源时钟）。"""
    if epoch is None:
        return '-'
    return datetime.fromtimestamp(float(epoch)).strftime('%H:%M:%S.%f')[:12]


def _ledger_offset() -> int:
    try:
        return os.path.getsize(LEDGER_PATH)
    except OSError:
        return 0


def _ledger_new(offset: int) -> list[dict]:
    """读台账 offset 之后新增的行（按字节偏移，避免把上一轮的行算进来）。"""
    rows: list[dict] = []
    try:
        with open(LEDGER_PATH, 'rb') as f:
            f.seek(offset)
            for line in f.read().decode('utf-8', 'ignore').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except OSError:
        pass
    return rows


def _draft_path(name: str) -> str:
    return os.path.join(get_projects_root(), name, 'draft_content.json')


def _draft_snapshot(name: str) -> dict:
    path = _draft_path(name)
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        data = json.loads(raw.decode('utf-8'))
        tracks = data.get('tracks') or []
        main = next((t for t in tracks if t.get('type') == 'video'), {})
        return {
            'path': path,
            'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()[:16],
            'tracks': len(tracks),
            'main_segments': len(main.get('segments') or []),
            'videos': len((data.get('materials') or {}).get('videos') or []),
        }
    except Exception as e:
        return {'path': path, 'error': f'{type(e).__name__}: {e}'}


def _classify(exc: Exception) -> str:
    msg = str(exc)
    if 'launch_failed:instant' in msg or 'launch_exhausted:instant' in msg:
        return '启动即退'
    if 'launch_failed:early' in msg or 'launch_exhausted:early' in msg:
        return '早死(≤20s)'
    if 'launch_failed:late' in msg or 'launch_exhausted:late' in msg:
        return '晚死(>20s)'
    if 'launch_failed:post_ready' in msg:
        return '就绪后死'
    if 'window_not_created' in msg:
        return '未建窗口(进程活着)'
    if 'launch_failed:timeout' in msg or 'window_missing' in msg:
        return '等不到窗口'
    if 'window_activate_timeout' in msg:
        return '窗口激活超时'
    return '拉起失败'


def _close_all(log) -> None:
    """尝试之间确保剪映完全退出（下次才是真冷启动）。"""
    if not jp.running():
        return
    jp.stop_jianying(grace_s=20.0, force=False, log_fn=log)
    if jp.running():
        log('  [selftest] 优雅关闭未退出 → 强制结束')
        jp.stop_jianying(grace_s=5.0, force=True, log_fn=log)
    for _ in range(20):
        if not jp.running():
            return
        time.sleep(0.5)


def _to_edit_page(draft: str, timeout: float = 120.0, log=print,
                  rec: dict | None = None) -> tuple[bool, str]:
    """点击草稿卡片并等编辑页；返回 (是否进入编辑页, 失败类型或空串)。

    rec 只用于记录时刻（点击 epoch / 判出编辑页 epoch），不参与判定。
    """
    def _mark(key, epoch):
        if rec is not None:
            rec[key] = epoch

    win, pt = ui_core.connect_jianying(retry=False)
    if pt == 'edit':
        _mark('click_epoch', time.time())
        _mark('edit_epoch', time.time())
        return True, ''
    ctrl = ui_nav._find_draft_ctrl_any(draft)
    if ctrl is None:
        try:
            ui_nav.refresh_home_list(win)
        except Exception:
            pass
        ctrl, _reason = ui_nav._wait_draft_card(draft, 20)
    if ctrl is None:
        return False, '首页找不到草稿卡片'
    log(f'  [selftest] 点击草稿卡片「{draft}」')
    _mark('click_epoch', time.time())
    ctrl.GetParentControl().Click(simulateMove=False)
    deadline = time.time() + timeout
    poll = 0
    while time.time() < deadline:
        time.sleep(1)
        poll += 1
        if not jp.running():
            return False, '打开草稿后进程退出'
        _t = time.time()
        _wins = jpd.enum_jianying_windows()
        _, pt = ui_core.connect_jianying(retry=False)
        _dt = time.time() - _t
        log(f'  [selftest][poll {poll}] +{_t - (rec or {}).get("click_epoch", _t):.1f}s '
            f'page={pt} 连接耗时={_dt:.2f}s 可见剪映窗口={len(_wins)}'
            + (' ' + '; '.join(f'{h}:{c}:{t[:20]!r}' for h, c, t in _wins) if _wins else ''))
        if pt == 'edit':
            _mark('edit_epoch', time.time())
            return True, ''
    return False, '进不去编辑页'


def _stage_timing(mgr, t0: float, t_end: float) -> dict:
    """把一轮启动拆成分段耗时（秒，全部相对本轮起点 t0 换算成 epoch 后求差）。

    数据来源：启动层 outcome 的 spawn_epoch + process_seen_at_s /
    window_seen_at_s（相对该次 spawn），以及后端在那次 spawn 之后打下的
    window_first / page_home|page_edit / interactive_home|interactive_edit
    epoch 戳。只读取，不改判据、不改启动流程。
    """
    out = getattr(mgr, 'last_outcome', None)
    stamps = dict(getattr(getattr(mgr, 'backend', None), 'timing', {}) or {})
    spawn = getattr(out, 'spawn_epoch', None) if out is not None else None

    def _pick(*keys):
        """取本次 spawn 之后最早的 epoch 戳（丢弃上一轮尝试留下的旧戳）。"""
        cand = [stamps[k] for k in keys
                if stamps.get(k) is not None
                and (spawn is None or stamps[k] >= spawn)]
        return min(cand) if cand else None

    def _rel(ep):
        return None if ep is None else round(float(ep) - t0, 1)

    def _off(since=None):
        return None if since is None else round(float(since), 1)

    t_spawn_e = spawn
    t_proc_e = (spawn + out.process_seen_at_s
                if spawn is not None and getattr(out, 'process_seen_at_s', None) is not None
                else None)
    t_win_e = (spawn + out.window_seen_at_s
               if spawn is not None and getattr(out, 'window_seen_at_s', None) is not None
               else None)
    if t_win_e is None:
        t_win_e = _pick('window_first')
    t_page_e = _pick('page_edit', 'page_home')
    t_int_e = _pick('interactive_edit', 'interactive_home')
    t_act_e = _pick('activate_start')

    t_spawn, t_proc = _rel(t_spawn_e), _rel(t_proc_e)
    t_win, t_page = _rel(t_win_e), _rel(t_page_e)
    t_int, t_act = _rel(t_int_e), _rel(t_act_e)
    return {
        't_spawn_s': t_spawn,
        't_process_s': t_proc,
        't_window_s': t_win,
        't_page_s': t_page,
        't_interactive_s': t_int,
        't_activate_s': t_act,
        'epochs': {'spawn': t_spawn_e, 'process': t_proc_e, 'window': t_win_e,
                   'page': t_page_e, 'interactive': t_int_e, 'activate': t_act_e},
        'seg_spawn_process': _off(None if t_proc is None else t_proc - (t_spawn or 0.0)),
        'seg_process_window': _off(None if (t_proc is None or t_win is None) else t_win - t_proc),
        'seg_window_page': _off(None if (t_win is None or t_page is None) else t_page - t_win),
        'seg_page_interactive': _off(None if (t_page is None or t_int is None) else t_int - t_page),
        'total_s': round(t_end - t0, 1),
    }


def _failure_stage(rec: dict, mgr) -> tuple[str, float]:
    """失败轮次：停在哪个阶段、在该阶段停留多久（秒）。"""
    out = getattr(mgr, 'last_outcome', None)
    oc = getattr(out, 'outcome', '') if out is not None else ''
    total = rec.get('alive_s') or rec.get('total_s') or 0.0
    t_proc = rec.get('t_process_s')
    t_win = rec.get('t_window_s')
    t_int = rec.get('t_interactive_s')
    if oc == 'died_after_ready':
        return '可交互后稳定性', round(total - (t_int or 0.0), 1)
    if oc in ('died', 'died_late'):
        return '进程存活期', round(total - (t_proc or 0.0), 1)
    if oc == 'window_not_created':
        return '建窗等待', round(total - (t_proc or 0.0), 1)
    if oc == 'timeout':
        if t_win is None:
            return '建窗等待', round(total - (t_proc or 0.0), 1)
        return '页面渲染/可交互', round(total - t_win, 1)
    return '拉起', total


def _fmt(v) -> str:
    return '-' if v is None else f'{v:.1f}'


def _print_table(results: list[dict]) -> None:
    print('\n===== 分段耗时（秒）=====')
    print(f"{'#':<2} {'结果':<4} {'spawn→进程':>10} {'进程→窗口':>9} "
          f"{'窗口→判页':>9} {'判页→可交互':>11} {'可交互→编辑':>11} "
          f"{'总耗时':>7}  失败步骤(停留)")
    for r in results:
        stage = r.get('stage_reached') or ''
        wait = r.get('stage_wait_s')
        fail = f'{stage}({wait}s)' if stage else '-'
        print(f"{r['attempt']:<2} {'成功' if r['ok'] else '失败':<4} "
              f"{_fmt(r.get('seg_spawn_process')):>10} "
              f"{_fmt(r.get('seg_process_window')):>9} "
              f"{_fmt(r.get('seg_window_page')):>9} "
              f"{_fmt(r.get('seg_page_interactive')):>11} "
              f"{_fmt(r.get('seg_interactive_edit')):>11} "
              f"{_fmt(r.get('total_s')):>7}  {fail}")


def _print_dist(results: list[dict]) -> None:
    import statistics
    print('\n===== 各步骤达成轮次与耗时分布 =====')
    steps = (('spawn→进程', 'seg_spawn_process'),
             ('进程→窗口', 'seg_process_window'),
             ('窗口→判页', 'seg_window_page'),
             ('判页→可交互', 'seg_page_interactive'),
             ('可交互→编辑', 'seg_interactive_edit'),
             ('总耗时', 'total_s'))
    for label, key in steps:
        vals = [r[key] for r in results if r.get(key) is not None]
        if not vals:
            print(f'{label}: 0/{len(results)} 轮达成')
            continue
        print(f'{label}: {len(vals)}/{len(results)} 轮达成  '
              f'最小 {min(vals):.1f}s  中位 {statistics.median(vals):.1f}s  '
              f'最大 {max(vals):.1f}s')


def _activation_slim(mgr) -> dict:
    """后端 last_activation 的摘要（含每步耗时），证据与时间轴都用它。"""
    act = getattr(getattr(mgr, 'backend', None), 'last_activation', None) or {}
    if not act:
        return {}
    return {
        'hwnd': act.get('hwnd'),
        'alt': act.get('alt'), 'minimize': act.get('minimize'),
        'restored': act.get('restored'), 'setactive': act.get('setactive'),
        'setforeground': act.get('setforeground'), 'topmost': act.get('topmost'),
        'in_front': act.get('in_front'),
        'steps_s': act.get('steps_s'), 'total_s': act.get('total_s'),
        'fg_before': (act.get('before') or {}).get('fg_title'),
        'fg_after': (act.get('after') or {}).get('fg_title'),
    }


def _attempts_of(rows: list[dict]) -> list[dict]:
    """台账行 → 每次尝试的重拉明细（尝试号/退出原因/存活/是否建过窗口）。"""
    out = []
    for r in rows:
        if r.get('entry') != 'selftest':
            continue
        out.append({
            'attempt': r.get('attempt'),
            'spawn_at': r.get('ts'),
            'result': r.get('result'),
            'death_kind': r.get('death_kind'),
            'alive_s': r.get('alive_s'),
            'window_seen_at_s': r.get('window_seen_at_s'),
            'process_seen_at_s': r.get('process_seen_at_s'),
            'ready_at_s': r.get('ready_at_s'),
            'exit_at': r.get('exit_at'),
            'how': r.get('how'),
            'pid': r.get('pid'),
            'error': (r.get('error') or '')[:160],
        })
    return out


def run_once(idx: int, draft: str, log=print) -> dict:
    rec = {'attempt': idx, 'started_at': datetime.now().strftime('%H:%M:%S.%f')[:12]}
    before = _draft_snapshot(draft)
    _close_all(log)
    time.sleep(1.5)
    rec['draft_before'] = {k: before.get(k) for k in
                           ('bytes', 'sha256', 'tracks', 'main_segments', 'videos')}

    ledger_off = _ledger_offset()
    t0 = time.time()
    rec['t0_epoch'] = round(t0, 3)
    mgr = jy_instance.JyInstance(find_exe=ui_core.resolve_jianying_exe, log=log)
    rec['spawn_at'] = _clock(t0)
    try:
        mgr.ensure_for_export(force_clean=False, timeout=120.0, entry='selftest')
    except Exception as e:
        rec.update(ok=False, level=1, failure_type=_classify(e),
                   error=str(e)[:300], alive_s=round(time.time() - t0, 1))
        rec.update(_stage_timing(mgr, t0, time.time()))
        rec['activation'] = _activation_slim(mgr)
        rec['attempts'] = _attempts_of(_ledger_new(ledger_off))
        rec['stage_reached'], rec['stage_wait_s'] = _failure_stage(rec, mgr)
        rec['draft_after'] = {k: _draft_snapshot(draft).get(k) for k in
                              ('bytes', 'sha256', 'tracks', 'main_segments', 'videos')}
        return rec

    rec['t_home_s'] = round(time.time() - t0, 1)
    rec.update(_stage_timing(mgr, t0, time.time()))
    rec['activation'] = _activation_slim(mgr)
    rec['level'] = 2
    ok_edit, why = _to_edit_page(draft, timeout=120.0, log=log, rec=rec)
    rec['t_edit_s'] = round(time.time() - t0, 1)
    if rec.get('click_epoch'):
        rec['t_click_s'] = round(rec['click_epoch'] - t0, 1)
    if not ok_edit:
        rec.update(ok=False, failure_type=why,
                   stage_reached='草稿进编辑页',
                   stage_wait_s=round(time.time() - t0 - rec['t_home_s'], 1))
    else:
        rec.update(ok=True, failure_type='')
        if rec.get('t_interactive_s') is not None:
            rec['seg_interactive_edit'] = round(rec['t_edit_s'] - rec['t_interactive_s'], 1)
        elif rec.get('t_click_s') is not None:
            rec['seg_interactive_edit'] = round(rec['t_edit_s'] - rec['t_click_s'], 1)
    rec['attempts'] = _attempts_of(_ledger_new(ledger_off))
    time.sleep(2)
    after = _draft_snapshot(draft)
    rec['draft_after'] = {k: after.get(k) for k in
                          ('bytes', 'sha256', 'tracks', 'main_segments', 'videos')}
    rec['draft_unchanged'] = (before.get('sha256') == after.get('sha256')
                              and before.get('bytes') == after.get('bytes'))
    rec['alive_s'] = round(time.time() - t0, 1)
    rec['still_alive'] = jp.running()
    return rec


def _print_timeline(rec: dict) -> None:
    """每轮一行时间轴：绝对时刻 + 相对 spawn 偏移 + 该段耗时。"""
    t0 = rec.get('t0_epoch')

    def _abs(off):
        return None if (t0 is None or off is None) else t0 + off

    def row(label, off, extra=''):
        rel = f'+{off:.1f}s' if off is not None else '-'
        print(f'  {label:<10} {_clock(_abs(off)):<13} {rel:>9}  {extra}')

    print(f"\n----- 第 {rec['attempt']} 轮时间轴（拉起 {rec.get('spawn_at')}）-----")
    row('拉起', 0.0)
    row('进程出现', rec.get('t_process_s'))
    row('窗口出现', rec.get('t_window_s'))
    row('首次判页', rec.get('t_page_s'))
    row('可交互通过', rec.get('t_interactive_s'))
    act = rec.get('activation') or {}
    steps = act.get('steps_s') or {}
    seg = ' '.join(f'{k}={steps[k]:.3f}s' for k in
                   ('alt', 'minimize', 'restore', 'setactive', 'setforeground', 'topmost')
                   if k in steps)
    row('激活', rec.get('t_activate_s'),
        f"total={act.get('total_s')}s {seg} in_front={act.get('in_front')} "
        f"fg={act.get('fg_before')!r}->{act.get('fg_after')!r}" if act else '未采集')
    row('点击草稿', rec.get('t_click_s'))
    if rec.get('ok'):
        row('进编辑页', rec.get('t_edit_s'))
    else:
        row(f"失败({rec.get('stage_reached') or rec['failure_type']})", rec.get('alive_s'),
            f"停留={rec.get('stage_wait_s')}s 原因={rec['failure_type'] or '-'}")
    for a in rec.get('attempts') or []:
        seen = (f"+{a['window_seen_at_s']:.1f}s"
                if a.get('window_seen_at_s') is not None else '从未建窗')
        print(f"   尝试#{a['attempt']} spawn={a['spawn_at']} 结果={a['result']} "
              f"类型={a.get('death_kind') or '-'} 存活={a.get('alive_s')}s "
              f"窗口={seen} pid={a.get('pid')} 退出={a.get('exit_at') or '-'}")


def _print_compare(results: list[dict]) -> None:
    """横向对比：同一环节 5 轮并排，标出最快/最慢。"""
    segs = (('拉起→进程', 'seg_spawn_process'),
            ('进程→窗口', 'seg_process_window'),
            ('窗口→判页', 'seg_window_page'),
            ('判页→可交互', 'seg_page_interactive'),
            ('可交互→编辑', 'seg_interactive_edit'),
            ('总耗时', 'total_s'))
    print('\n===== 横向对比（秒；- = 该轮未采集到）=====')
    head = '环节'.ljust(12) + ''.join(f"R{r['attempt']}".ljust(8) for r in results)
    print(head + '最快 / 最慢')
    for label, key in segs:
        cells, vals = [], []
        for r in results:
            v = r.get(key)
            cells.append('-' if v is None else f'{v:.1f}')
            if v is not None:
                vals.append((v, r['attempt']))
        if vals:
            fast, slow = min(vals), max(vals)
            tail = f"R{fast[1]} {fast[0]:.1f}s / R{slow[1]} {slow[0]:.1f}s"
        else:
            tail = '-'
        print(label.ljust(12) + ''.join(c.ljust(8) for c in cells) + tail)

    print('\n===== 激活子步骤横向对比（秒）=====')
    print('轮次'.ljust(6) + ''.join(k.ljust(13) for k in
                                    ('alt', 'minimize', 'restore', 'setactive',
                                     'setforeground', 'topmost', '合计')) + 'in_front')
    for r in results:
        act = r.get('activation') or {}
        steps = act.get('steps_s') or {}
        if not steps:
            print(f"R{r['attempt']}".ljust(6) + '未采集')
            continue
        cells = ''.join(f"{steps.get(k, '-')}".ljust(13) for k in
                        ('alt', 'minimize', 'restore', 'setactive',
                         'setforeground', 'topmost'))
        print(f"R{r['attempt']}".ljust(6) + cells +
              f"{act.get('total_s', '-')}".ljust(9) + f"{act.get('in_front')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--runs', type=int, default=5)
    ap.add_argument('--draft', default='ui_pipeline_ac')
    args = ap.parse_args()

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = os.path.join(EVIDENCE_DIR, f'launch_selftest_{ts}.json')
    print(f'[selftest] 草稿={args.draft} 次数={args.runs} 证据={out_path}')

    results = []
    for i in range(1, args.runs + 1):
        print(f'\n===== 第 {i}/{args.runs} 次冷启动 =====')
        rec = run_once(i, args.draft)
        results.append(rec)
        print(f"[selftest] #{i} ok={rec['ok']} level={rec.get('level')} "
              f"type={rec['failure_type'] or '-'} home={rec.get('t_home_s')}s "
              f"edit={rec.get('t_edit_s')}s alive={rec.get('alive_s')}s")
        _print_timeline(rec)
        try:
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f'  [selftest] 证据写盘失败: {e}')
        _close_all(print)

    ok = [r for r in results if r['ok']]
    fails = [r for r in results if not r['ok']]
    print('\n===== 汇总 =====')
    for r in results:
        print(f"#{r['attempt']:<2} {r['started_at']}  "
              f"{'成功' if r['ok'] else '失败':<4} "
              f"级别={r.get('level', '-')} 失败类型={r['failure_type'] or '-':<10} "
              f"到首页={r.get('t_home_s', '-')}s 进编辑页={r.get('t_edit_s', '-')}s "
              f"存活={r.get('alive_s', '-')}s 草稿未变={r.get('draft_unchanged', '-')}")
    print(f'\n成功率 {len(ok)}/{len(results)}')
    dist: dict[str, int] = {}
    for r in fails:
        dist[r['failure_type']] = dist.get(r['failure_type'], 0) + 1
    print('失败分布 ' + (', '.join(f'{k}×{v}' for k, v in dist.items()) if dist else '无'))
    _print_table(results)
    _print_dist(results)
    _print_compare(results)
    print(f'证据文件 {out_path}')
    return 0 if len(ok) == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
