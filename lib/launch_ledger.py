"""launch_ledger.py — 启动层早退台账（追加式 JSONL，独立于会轮转的日志）。

为什么单独一份：`JianyingPro_*.log` 会被轮转/覆盖，而"早退率是否恶化"需要跨天累积，
所以台账只追加、不重写、不清理。

落盘位置：`pipeline/_work/launch_ledger.jsonl`
  - 可用环境变量 `JY_LAUNCH_LEDGER` 覆盖路径（单测/临时实验用）；
  - 该变量为 `off` 时完全关闭写入。

一条 = 一次拉起（重试各算一条），同一次 `ensure_jianying` 调用共用一个 `launch_id`。

汇总入口（回答三个升级触发条件）：
    python lib/launch_ledger.py --last 50
"""
from __future__ import annotations

import json
import os
import sys
from collections import OrderedDict, defaultdict

# "早退"口径：进程消失且存活 ≤ 3s。实测自发早退签名是 1.22–1.29s（exit 0、无窗口、
# 主日志 0 B）；取 3s 是为了容纳慢机，同时把"3s 后死亡"归到普通 died 而不是早退。
EARLY_EXIT_MAX_S = 3.0
# 基线：2026-11-14 受害实例 24 次拉起 / 7 次自发早退 ≈ 29%，超过即触发升级评估。
BASELINE_EARLY_EXIT_RATE = 0.29
# 样本太小时比率没有意义，只做提示，不改变判定输出。
MIN_SAMPLE = 5

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(_ROOT, 'pipeline', '_work', 'launch_ledger.jsonl')


def ledger_path() -> str:
    """台账路径：环境变量优先，其次 pipeline/_work/launch_ledger.jsonl。"""
    return os.environ.get('JY_LAUNCH_LEDGER') or DEFAULT_PATH


def append_record(rec: dict, path: str | None = None) -> bool:
    """追加一条记录。失败只返回 False，绝不让台账问题影响启动流程。"""
    p = path or ledger_path()
    if p == 'off':
        return False
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        return True
    except Exception:
        return False


def iter_records(path: str | None = None):
    """按写入顺序产出记录；坏行跳过。"""
    p = path or ledger_path()
    if p == 'off' or not os.path.exists(p):
        return
    with open(p, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get('result'):
                yield rec


def is_early_exit(rec: dict) -> bool:
    """died 且存活 ≤ EARLY_EXIT_MAX_S。"""
    if rec.get('result') != 'died':
        return False
    died_at = rec.get('died_at_s')
    return died_at is not None and float(died_at) <= EARLY_EXIT_MAX_S


def summarize(last_n: int = 50, path: str | None = None) -> dict:
    """最近 N 次的早退率、重试后仍早退、是否超基线（三条升级触发条件）。"""
    recs = list(iter_records(path))
    window = recs[-max(1, int(last_n)):]
    n = len(window)

    early = [r for r in window if is_early_exit(r)]
    rate = (len(early) / n) if n else 0.0

    groups: 'OrderedDict[str, list]' = OrderedDict()
    for r in window:
        groups.setdefault(str(r.get('launch_id') or ''), []).append(r)
    retry_early = []
    for lid, rs in groups.items():
        rs.sort(key=lambda r: int(r.get('attempt') or 0))
        early_rs = [r for r in rs if is_early_exit(r)]
        if len(early_rs) >= 2:
            retry_early.append({
                'launch_id': lid,
                'attempts': [e.get('attempt') for e in early_rs],
                'died_at_s': [e.get('died_at_s') for e in early_rs],
                'entry': early_rs[0].get('entry'),
                'ts': early_rs[0].get('ts'),
            })

    by_entry = defaultdict(int)
    for r in window:
        by_entry[str(r.get('entry') or 'unknown')] += 1

    # 同一部剧的跨轮重拉：job 由 cover_watch 透传，job_round=0 是首次正常轮。
    jobs: 'OrderedDict[str, dict]' = OrderedDict()
    for r in window:
        job = r.get('job')
        if not job:
            continue
        g = jobs.setdefault(str(job), {'job': str(job), 'records': 0, 'rounds': 0,
                                       'ready': 0, 'died': 0, 'timeout': 0,
                                       'died_after_ready': 0, 'final': None})
        g['records'] += 1
        g['rounds'] = max(g['rounds'], int(r.get('job_round') or 0))
        res = r.get('result')
        if res in ('ready', 'died', 'timeout', 'died_after_ready'):
            g[res] += 1
        g['final'] = res
    # 只保留发生过跨轮重拉（rounds>0）或拉起失败的 job，避免把全部任务刷进汇总
    relaunch_jobs = [g for g in jobs.values()
                     if g['rounds'] > 0 or g['died'] or g['timeout']
                     or g['died_after_ready']]
    for g in relaunch_jobs:
        g['relaunch_rounds'] = max(0, g['rounds'])
        g['succeeded'] = g['final'] == 'ready'

    return {
        'path': path or ledger_path(),
        'last_n': last_n,
        'n': n,
        'window_from': window[0].get('ts') if window else None,
        'window_to': window[-1].get('ts') if window else None,
        'ready': sum(1 for r in window if r.get('result') == 'ready'),
        'died': sum(1 for r in window if r.get('result') == 'died'),
        'timeout': sum(1 for r in window if r.get('result') == 'timeout'),
        'died_after_ready': sum(1 for r in window
                                if r.get('result') == 'died_after_ready'),
        'early_exits': len(early),
        'early_exit_rate': round(rate, 4),
        'baseline_rate': BASELINE_EARLY_EXIT_RATE,
        'exceeds_baseline': rate > BASELINE_EARLY_EXIT_RATE,
        'sample_ok': n >= MIN_SAMPLE,
        'retry_still_early': len(retry_early),
        'retry_still_early_ids': retry_early,
        'by_entry': dict(by_entry),
        'relaunch_jobs': relaunch_jobs,
        'relaunch_jobs_succeeded': sum(1 for g in relaunch_jobs if g['succeeded']),
        'total_records': len(recs),
    }


def format_summary(s: dict) -> str:
    """人读版汇总。"""
    if not s['n']:
        return (f'启动层早退台账：{s["path"]}\n'
                f'  还没有记录（累计 {s["total_records"]} 条，全部为空）')
    lines = [
        f'启动层早退台账：{s["path"]}',
        f'  窗口：最近 {s["last_n"]} 次（实际 {s["n"]} 次，累计 {s["total_records"]} 条）',
        f'  时间：{s["window_from"]} → {s["window_to"]}',
        f'  结果：ready {s["ready"]} / died {s["died"]} / timeout {s["timeout"]}'
        f' / ready 后死 {s["died_after_ready"]}',
        f'  早退（died 且 ≤{EARLY_EXIT_MAX_S:g}s）：{s["early_exits"]} 次，'
        f'早退率 {s["early_exit_rate"] * 100:.1f}%',
        f'  按入口：' + (' / '.join(f'{k} {v}' for k, v in s['by_entry'].items()) or '-'),
        '升级触发条件：',
    ]
    lines.append(f'  [{"x" if s["exceeds_baseline"] else " "}] 早退率 {s["early_exit_rate"] * 100:.1f}% '
                 f'> 基线 {s["baseline_rate"] * 100:.0f}%')
    lines.append(f'  [{"x" if s["retry_still_early"] else " "}] 出现"重试后仍然早退"：'
                 f'{s["retry_still_early"]} 次')
    for e in s['retry_still_early_ids'][:5]:
        lines.append(f'      launch_id={e["launch_id"]} entry={e["entry"]} '
                     f'attempts={e["attempts"]} died_at_s={e["died_at_s"]} ts={e["ts"]}')
    if s.get('relaunch_jobs'):
        lines.append(f'  跨轮重拉任务：{len(s["relaunch_jobs"])} 个，'
                     f'最后成功 {s["relaunch_jobs_succeeded"]} 个')
        for g in s['relaunch_jobs'][:8]:
            lines.append(f'      {g["job"]}: 跨轮 {g["relaunch_rounds"]} 轮 / '
                         f'{g["records"]} 条，最后 {g["final"]}'
                         f'（ready {g["ready"]} died {g["died"]} timeout {g["timeout"]} '
                         f'ready后死 {g["died_after_ready"]}）')
    if not s['sample_ok']:
        lines.append(f'  注意：样本 {s["n"]} < {MIN_SAMPLE}，比率仅供参考')
    return '\n'.join(lines)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    last_n, path, as_json = 50, None, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--last' and i + 1 < len(argv):
            last_n = int(argv[i + 1]); i += 2
        elif a == '--path' and i + 1 < len(argv):
            path = argv[i + 1]; i += 2
        elif a == '--json':
            as_json = True; i += 1
        elif a in ('-h', '--help'):
            print('用法: python lib/launch_ledger.py [--last N] [--path FILE] [--json]')
            return 0
        else:
            print(f'未知参数: {a}')
            return 2
    s = summarize(last_n, path)
    print(json.dumps(s, ensure_ascii=False, indent=2) if as_json else format_summary(s))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
