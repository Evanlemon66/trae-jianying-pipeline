"""启动层台账（lib/launch_ledger.py + ensure_jianying 埋点）单测。

不启动剪映：台账部分只写临时文件，集成部分用假时钟/假进程。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'lib'))

import jianying_launch as jl  # noqa: E402
import launch_ledger as ll  # noqa: E402


def _rec(lid, attempt, result, died_at=None, entry='admit', **kw):
    r = {
        'ts': '2026-11-14T12:00:0%d+0800' % attempt,
        'launch_id': lid, 'entry': entry, 'attempt': attempt,
        'retry_count': attempt - 1, 'result': result,
        'died_at_s': died_at, 'alive_s': died_at or 2.4,
    }
    r.update(kw)
    return r


def test_append_and_summarize(tmp_path):
    p = str(tmp_path / 'ledger.jsonl')
    assert ll.append_record(_rec('a1', 1, 'ready'), path=p)
    assert ll.append_record(_rec('a2', 1, 'ready'), path=p)
    assert ll.append_record(_rec('a3', 1, 'died', 1.25), path=p)
    s = ll.summarize(50, p)
    assert s['n'] == 3 and s['ready'] == 2 and s['died'] == 1
    assert s['early_exits'] == 1
    assert abs(s['early_exit_rate'] - 1 / 3) < 1e-4
    assert s['exceeds_baseline'] is True
    assert s['retry_still_early'] == 0
    assert s['by_entry'] == {'admit': 3}


def test_retry_still_early(tmp_path):
    p = str(tmp_path / 'ledger.jsonl')
    ll.append_record(_rec('L1', 1, 'died', 1.22), path=p)
    ll.append_record(_rec('L1', 2, 'died', 1.28), path=p)
    ll.append_record(_rec('L2', 1, 'died', 1.24), path=p)
    ll.append_record(_rec('L2', 2, 'ready'), path=p)
    s = ll.summarize(50, p)
    assert s['retry_still_early'] == 1
    assert s['retry_still_early_ids'][0]['launch_id'] == 'L1'
    assert s['retry_still_early_ids'][0]['attempts'] == [1, 2]


def test_died_late_not_counted_as_early(tmp_path):
    p = str(tmp_path / 'ledger.jsonl')
    ll.append_record(_rec('b1', 1, 'died', 8.0), path=p)
    s = ll.summarize(50, p)
    assert s['early_exits'] == 0 and s['died'] == 1
    assert s['exceeds_baseline'] is False


def test_died_after_ready_is_visible_but_not_early(tmp_path):
    p = str(tmp_path / 'ledger.jsonl')
    ll.append_record(_rec('e1', 1, 'died_after_ready', 11.0,
                          ready_at_s=3.0, post_ready_died_at_s=8.0), path=p)
    s = ll.summarize(50, p)
    assert s['died_after_ready'] == 1
    assert s['early_exits'] == 0
    assert 'ready 后死 1' in ll.format_summary(s)


def test_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv('JY_LAUNCH_LEDGER', 'off')
    assert ll.append_record(_rec('c1', 1, 'ready')) is False
    s = ll.summarize(50)
    assert s['n'] == 0 and s['early_exit_rate'] == 0.0


def test_summary_text_has_triggers(tmp_path):
    p = str(tmp_path / 'ledger.jsonl')
    ll.append_record(_rec('d1', 1, 'ready'), path=p)
    txt = ll.format_summary(ll.summarize(50, p))
    assert '升级触发条件' in txt and '基线 29%' in txt


class Clock:
    def __init__(self, start=1000.0):
        self.t = start

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Fake:
    """第一次拉起 1.2s 后消失，第二次永不消失。"""

    def __init__(self, clock):
        self.clock = clock
        self.spawns = []
        self.birth = None
        self.current = None

    def spawn(self, exe):
        self.spawns.append(self.clock.time())
        self.birth = self.clock.time()
        self.current = 1.2 if len(self.spawns) == 1 else None
        return 777

    def alive(self):
        return self.current is None or (self.clock.time() - self.birth) <= self.current


def test_ensure_jianying_writes_ledger(tmp_path, monkeypatch):
    p = str(tmp_path / 'ledger.jsonl')
    monkeypatch.setenv('JY_LAUNCH_LEDGER', p)
    clock, fake = Clock(), Fake(Clock())
    fake.clock = clock
    out = jl.ensure_jianying(
        exe_path='fake.exe', require_exists=False, validate_version=False,
        timeout=30.0, poll_interval=1.0, retries=1, entry='export',
        ready_fn=lambda: fake.spawns and len(fake.spawns) >= 2,
        alive_fn=fake.alive, spawn_fn=fake.spawn,
        time_fn=clock.time, sleep_fn=clock.sleep)
    assert out.ok is True and out.attempt == 2
    rows = list(ll.iter_records(p))
    assert len(rows) == 2
    assert rows[0]['result'] == 'died' and rows[0]['died_at_s'] <= 3.0
    assert rows[0]['retry_count'] == 0 and rows[1]['retry_count'] == 1
    assert rows[0]['launch_id'] == rows[1]['launch_id']
    assert rows[0]['entry'] == 'export' and rows[1]['result'] == 'ready'
    s = ll.summarize(50, p)
    assert s['early_exits'] == 1 and s['retry_still_early'] == 0
