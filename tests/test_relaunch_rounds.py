"""跨轮重拉：状态机、优先级、分类与台账串号。"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'lib'))

import cover_watch as cw  # noqa: E402
import failure_class as fc  # noqa: E402
import launch_ledger as ll  # noqa: E402


def test_launch_failed_classification():
    cls, act = fc.classify("[launch_failed:died alive=1.2s attempt=2 env_check=0] "
                           "无法自动启动【剪映专业版】，请手动启动后重试。")
    assert cls == fc.LAUNCH_FAILED
    assert fc.strategy_of(cls)["retryable"] is True
    # 没有标记的同类文案仍走 UI_HANG，不会被误判成拉起失败
    cls2, _ = fc.classify("无法自动启动【剪映专业版】，请手动启动后重试。")
    assert cls2 == fc.UI_HANG


def test_relaunch_rounds_until_exhausted(tmp_path):
    job_dir = str(tmp_path / 'job')
    os.makedirs(job_dir)
    cfg = {'relaunch_max_rounds': 3}
    seen = []
    for _ in range(4):
        st = cw.relaunch_note_failure(job_dir, cfg, error='died', failure_class=fc.LAUNCH_FAILED)
        seen.append((st['status'], st['rounds']))
    assert seen == [('pending', 1), ('pending', 2), ('pending', 3), ('exhausted', 4)]
    # 到顶后状态保留在磁盘上（供人工查看），但 build_plan 不会再放行
    assert cw.relaunch_state(job_dir)['status'] == 'exhausted'
    cw._relaunch_clear(job_dir, '人工处理后重跑')
    assert cw.relaunch_state(job_dir) is None


def test_relaunch_state_tolerates_corrupt_file(tmp_path):
    job_dir = str(tmp_path / 'job')
    os.makedirs(job_dir)
    with open(os.path.join(job_dir, cw.RELAUNCH_STATE_NAME), 'w', encoding='utf-8') as f:
        f.write('{not json')
    assert cw.relaunch_state(job_dir) is None


def test_relaunch_job_ranks_first():
    def cand(name, action, relaunch=False, prio=0):
        p = {'action': action, 'intent': {'priority': prio}, 'job_dir': name}
        if relaunch:
            p['relaunch'] = {'status': 'pending', 'rounds': 1, 'max_rounds': 3}
        return p
    picked = cw._pick([cand('b_job', 'export', prio=9),
                       cand('a_job', 'export', relaunch=True)])
    assert picked['job_dir'] == 'a_job'


def test_ledger_job_round_summary(tmp_path):
    p = str(tmp_path / 'ledger.jsonl')
    ll.append_record({'ts': 't1', 'launch_id': 'x1', 'attempt': 1, 'result': 'died',
                      'died_at_s': 1.25, 'job': '剧A', 'job_round': 0, 'entry': 'export'}, path=p)
    ll.append_record({'ts': 't2', 'launch_id': 'x2', 'attempt': 1, 'result': 'died',
                      'died_at_s': 1.22, 'job': '剧A', 'job_round': 1, 'entry': 'export'}, path=p)
    ll.append_record({'ts': 't3', 'launch_id': 'x3', 'attempt': 1, 'result': 'ready',
                      'died_at_s': None, 'job': '剧A', 'job_round': 2, 'entry': 'export'}, path=p)
    s = ll.summarize(50, p)
    assert len(s['relaunch_jobs']) == 1
    g = s['relaunch_jobs'][0]
    assert g['job'] == '剧A' and g['relaunch_rounds'] == 2
    assert g['final'] == 'ready' and g['succeeded'] is True
    assert s['relaunch_jobs_succeeded'] == 1
    assert '跨轮重拉任务' in ll.format_summary(s)
