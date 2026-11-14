# -*- coding: utf-8 -*-
"""启动共用层（lib/jianying_launch.py）单测。

只跑纯逻辑：注入假时钟 / 假存活 / 假探测，不启动剪映、不碰 UIA、不跑导出。
"""
import inspect
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'lib'))
sys.path.insert(0, os.path.join(_ROOT, 'pipeline'))
sys.path.insert(0, os.path.join(_ROOT, 'pipeline_ui'))
sys.path.insert(0, os.path.join(_ROOT, '.trae', 'skills', 'jianying-editor', 'examples'))

import jianying_launch as jl  # noqa: E402
import jy_instance as ji  # noqa: E402

# 单测不写生产台账（launch_ledger 的集成用例自己用 monkeypatch 指定临时文件）。
os.environ.setdefault('JY_LAUNCH_LEDGER', 'off')


class Clock:
    def __init__(self, start=1000.0):
        self.t = start
        self.sleeps = []

    def time(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class FakeJianying:
    """按 lifetimes 模拟每次拉起的存活时长（None=永不退出）。"""

    def __init__(self, clock, lifetimes):
        self.clock = clock
        self.lifetimes = list(lifetimes)
        self.spawns = []
        self.birth = None
        self.current = None

    def spawn(self, exe):
        self.spawns.append(self.clock.time())
        self.birth = self.clock.time()
        idx = min(len(self.spawns) - 1, len(self.lifetimes) - 1)
        self.current = self.lifetimes[idx]
        return 4242

    def alive(self):
        if self.current is None:
            return False
        return (self.clock.time() - self.birth) <= self.current


def _run(clock, fake, **kw):
    kw.setdefault('timeout', 180.0)
    kw.setdefault('poll_interval', 1.0)
    kw.setdefault('ready_fn', lambda: False)
    return jl.ensure_jianying(
        exe_path='fake.exe', require_exists=False, validate_version=False,
        alive_fn=fake.alive, spawn_fn=fake.spawn,
        time_fn=clock.time, sleep_fn=clock.sleep, **kw)


# ── 1. 常量口径（必须保持不变的行为参数）──────────────────────────
def test_constants_pinned():
    assert jl.REQUIRED_JY_VERSION == '5.9.0.11632'
    assert jl.HOME_READY_TIMEOUT_S == 120.0
    assert jl.EARLY_DEATH_WINDOW_S == 20.0
    assert jl.EARLY_DEATH_RETRIES == 1
    assert jl.EARLY_DEATH_SETTLE_S == 0.0
    assert jl.RESTART_SETTLE_S == 0.0
    assert jl.POST_READY_WATCH_S == 8.0
    # 崩溃框独立预算：本机 nvapi 已连续崩 3 次，不能被早死预算（1）吃掉
    assert jl.CRASH_RETRIES == 2
    assert jl.CRASH_SETTLE_S == 3.0
    # 导出链通过别名引用同一口径
    assert ji.HOME_READY_TIMEOUT_S == 120.0
    assert ji.EARLY_DEATH_RETRIES == 1
    assert ji.EARLY_DEATH_WINDOW_S == 20.0
    assert ji.EARLY_DEATH_SETTLE_S == 0.0
    assert ji.RESTART_SETTLE_S == 0.0


# ── 2. 进程消失早退：不等满 180s ─────────────────────────────────
def test_process_died_returns_immediately():
    clock = Clock()
    fake = FakeJianying(clock, [4.5])
    out = _run(clock, fake, timeout=180.0, retries=0)
    assert out.ok is False
    assert out.outcome == 'died'
    assert len(fake.spawns) == 1
    assert out.alive_s <= 6.0, out.alive_s
    assert clock.t - 1000.0 <= 6.0, '进程已死却仍在盲等超时'


# ── 3. 启动即死重拉：预算 1，不随 attempt 放大 ───────────────────
def test_early_death_retry_budget_bounded():
    clock = Clock()
    fake = FakeJianying(clock, [4.5, 4.5, 4.5])
    out = _run(clock, fake, timeout=180.0, retries=jl.EARLY_DEATH_RETRIES)
    assert out.ok is False
    assert out.outcome == 'died'
    assert len(fake.spawns) == 2, '一次运行最多重拉 1 次'
    # 两次死亡各 ~5s，重拉前不静默（SETTLE=0）
    assert clock.t == 1010.0, clock.t


def test_retry_budget_is_per_run_not_per_call():
    clock = Clock()
    fake = FakeJianying(clock, [4.5, 4.5, 4.5, 4.5])
    budget = jl.LaunchBudget(1)
    _run(clock, fake, budget=budget)
    assert len(fake.spawns) == 2
    _run(clock, fake, budget=budget)   # 同一运行的第二次 launch
    assert len(fake.spawns) == 3, '预算耗尽后不得再重拉'
    assert budget.remaining == 0


def test_crash_retry_has_its_own_budget():
    """崩溃框不与早死挤占预算：进程不退出也能连拉 1+CRASH_RETRIES 次。"""
    clock = Clock()
    fake = FakeJianying(clock, [None])
    orig_crash, orig_dismiss = jl.crash_dialogs, jl.dismiss_window
    jl.crash_dialogs = lambda: [{'hwnd': 1, 'title': 'VEDetector has stopped working',
                                 'text': 'nvapi', 'pid': 99}]
    jl.dismiss_window = lambda hwnd: True
    try:
        out = _run(clock, fake)
    finally:
        jl.crash_dialogs, jl.dismiss_window = orig_crash, orig_dismiss
    assert out.ok is False
    assert out.crash_retry is False, '预算耗尽那次不应再标记为崩溃重拉'
    assert len(fake.spawns) == 1 + jl.CRASH_RETRIES, len(fake.spawns)
    assert clock.sleeps.count(jl.CRASH_SETTLE_S) == jl.CRASH_RETRIES


# ── 4. 重拉前不得静默：早退实例未进应用层，等 2s 无收益（实测 6 轮无差别）──
def test_no_settle_before_respawn():
    clock = Clock()
    fake = FakeJianying(clock, [4.5, None])
    _run(clock, fake, retries=1)
    assert len(fake.spawns) == 2
    assert 2.0 not in clock.sleeps, '不允许出现 2s 静置'
    assert 0.0 in clock.sleeps, 'SETTLE=0 仍应走一次零等待的 settle 调用'
    assert fake.spawns[1] - fake.spawns[0] <= 6.0


# ── 4b. ready 后又死：必须可见并进重试（B5）────────────────────
def test_post_ready_death_is_retried():
    """就绪后 3s 死亡 → 判 died_after_ready，消耗预算重拉，重拉后 ready。"""
    clock = Clock()
    fake = FakeJianying(clock, [3.0, 1e9])
    out = _run(clock, fake, ready_fn=lambda: True, retries=1)
    assert out.ok is True and out.outcome == 'ready'
    assert out.attempt == 2 and len(fake.spawns) == 2


def test_post_ready_death_without_budget_is_not_ready():
    clock = Clock()
    fake = FakeJianying(clock, [3.0])
    out = _run(clock, fake, ready_fn=lambda: True, retries=0)
    assert out.ok is False and out.outcome == 'died_after_ready'
    assert out.ready_at_s is not None
    assert 3.0 <= out.post_ready_died_at_s <= 5.0, out.post_ready_died_at_s
    assert 'ready 后死' in out.error


def test_post_ready_watch_passes_when_alive():
    """观察窗内不死 → 正常 ready；观察窗确实走满，不是立即返回。"""
    clock = Clock()
    fake = FakeJianying(clock, [1e9])
    out = _run(clock, fake, ready_fn=lambda: True, retries=0)
    assert out.ok is True and out.outcome == 'ready'
    assert clock.t - fake.spawns[0] >= jl.POST_READY_WATCH_S, clock.t


def test_post_ready_watch_can_be_disabled():
    clock = Clock()
    fake = FakeJianying(clock, [1e9])
    out = _run(clock, fake, ready_fn=lambda: True, retries=0,
               post_ready_watch_s=0.0)
    assert out.ok is True
    assert clock.t - fake.spawns[0] < 1.0, clock.t


# ── 5. 版本门两档：读到不符→拒绝；读不到→告警继续 ──────────────
def test_version_mismatch_raises(monkeypatch=None):
    tmp = tempfile.NamedTemporaryFile(suffix='.exe', delete=False)
    tmp.close()
    orig = jl.exe_file_version
    jl.exe_file_version = lambda _p: '8.9.0.0'
    try:
        try:
            jl.ensure_jianying(exe_path=tmp.name, timeout=1.0,
                               ready_fn=lambda: True, alive_fn=lambda: True)
        except jl.JianyingLaunchError as e:
            assert '8.9.0.0' in str(e) and '5.9.0.11632' in str(e)
        else:
            raise AssertionError('版本不符必须抛 JianyingLaunchError')
    finally:
        jl.exe_file_version = orig
        os.unlink(tmp.name)


def test_version_unreadable_warns_and_proceeds():
    """读不到版本信息 → 不拒绝，打告警后继续（用目录名作佐证）。"""
    d = tempfile.mkdtemp(prefix='JianyingPro 5.9.0.11632_')
    exe = os.path.join(d, 'JianyingPro.exe')
    open(exe, 'wb').close()
    logs = []
    orig = jl.exe_file_version
    jl.exe_file_version = lambda _p: ''
    try:
        c = Clock()
        out = jl.ensure_jianying(
            exe_path=exe, timeout=1.0, ready_fn=lambda: True,
            alive_fn=lambda: True, spawn_fn=lambda _e: 1,
            time_fn=c.time, sleep_fn=c.sleep, log_fn=logs.append)
        assert out.ok is True, '读不到版本不应拒绝启动'
        joined = '\n'.join(logs)
        assert '读不到版本信息' in joined and '目录名佐证' in joined
    finally:
        jl.exe_file_version = orig
        os.unlink(exe)
        os.rmdir(d)


def test_missing_exe_raises():
    try:
        jl.ensure_jianying(exe_path='Z:/no/such/JianyingPro.exe', timeout=1.0)
    except jl.JianyingLaunchError as e:
        assert '不存在' in str(e)
    else:
        raise AssertionError('require_exists=True 时路径不存在必须抛错')


# ── 5b. 就绪口径：readiness='home' 走默认首页判定，'window' 需注入 ready_fn ──
def test_readiness_variants():
    import ui_core
    # 默认仍是等窗口（与既有准入行为一致）
    assert inspect.signature(ui_core.launch_and_connect).parameters['readiness'].default == 'window'
    import ui_nav
    # 准入已统一到实例管理器：不再暴露 readiness（就绪口径固定 home）
    assert 'readiness' not in inspect.signature(ui_nav.open_draft).parameters
    assert 'readiness' not in inspect.signature(ui_nav._launch_until_card).parameters
    assert inspect.signature(ui_nav.open_draft).parameters['ready_timeout'].default == 120.0
    # 'window' 缺 ready_fn → 明确报错（不得静默当 home）
    try:
        jl.ensure_jianying(exe_path='fake.exe', readiness='window',
                           require_exists=False, validate_version=False,
                           timeout=1.0)
    except jl.JianyingLaunchError as e:
        assert '未知就绪口径' in str(e)
    else:
        raise AssertionError("readiness='window' 且未注入 ready_fn 必须报错")


# ── 5c. 检测器日志：按 mtime 取最新，排除主日志，返回文件名/字节数 ──
def test_latest_detector_log_by_mtime():
    d = tempfile.mkdtemp(prefix='jylog_')
    try:
        def make(name, mtime, body=''):
            p = os.path.join(d, name)
            with open(p, 'w', encoding='utf-8') as f:
                f.write(body)
            os.utime(p, (mtime, mtime))
            return p

        make('JianyingPro_20261114.log', 1005.0, 'main log')     # 主日志，最新但要排除
        make('vedetector_a.log', 1001.0, 'old detector')
        target = make('whatever_b.log', 1003.0, 'line1\nline2 crash boom\n')
        orig = jl._JY_LOG_DIR
        jl._JY_LOG_DIR = d
        try:
            name, size, tail = jl.latest_detector_log(1000.0)
        finally:
            jl._JY_LOG_DIR = orig
        assert name == 'whatever_b.log', name
        assert size == os.path.getsize(target), size
        assert 'crash boom' in tail, tail
    finally:
        for n in os.listdir(d):
            os.unlink(os.path.join(d, n))
        os.rmdir(d)


# ── 6. 拉起顺序：breakaway → WMI → 普通 Popen ────────────────────
def test_spawn_wmi_fallback_then_plain():
    calls = {'popen': 0, 'wmi': 0}

    def bad_popen(*_a, **_kw):
        calls['popen'] += 1
        raise OSError('WinError 5')

    orig_popen, orig_wmi = jl.subprocess.Popen, jl.wmi_launch
    jl.subprocess.Popen = bad_popen
    try:
        jl.wmi_launch = lambda _e, log_fn=None: (calls.__setitem__('wmi', calls['wmi'] + 1) or True)
        assert jl.spawn_jianying('fake.exe') == (None, 'wmi')
        assert calls['popen'] == 1 and calls['wmi'] == 1

        jl.wmi_launch = lambda _e, log_fn=None: False
        try:
            jl.spawn_jianying('fake.exe')
        except jl.JianyingLaunchError:
            pass
        else:
            raise AssertionError('WMI 也失败时应抛 JianyingLaunchError')
        assert calls['popen'] == 3, '应经历 breakaway → plain 两次 Popen'
    finally:
        jl.subprocess.Popen, jl.wmi_launch = orig_popen, orig_wmi


# ── 7. 收尾策略矩阵：留窗 / 只关本轮 / 关闭 ──────────────────────
def test_finish_run_matrix():
    calls = {'close': 0}

    def close():
        calls['close'] += 1
        return True

    assert jl.finish_run(True, True, close_fn=close, is_running_fn=lambda: True) is True
    assert calls['close'] == 0, '留窗不得关闭'

    assert jl.finish_run(False, False, close_fn=close, is_running_fn=lambda: True) is True
    assert calls['close'] == 0, '非本轮拉起不得关闭'

    assert jl.finish_run(False, True, close_fn=close, is_running_fn=lambda: True) is True
    assert calls['close'] == 1, '本轮拉起且允许关闭时必须关闭'

    # 进程已不在：不再调用 close，直接视为已收尾
    assert jl.finish_run(False, True, close_fn=close, is_running_fn=lambda: False) is True
    assert calls['close'] == 1


# ── 8. 准入/导出参数未被改动（回归钉）────────────────────────────
def test_call_sites_params_unchanged():
    import ui_nav
    sig = inspect.signature(ui_nav.open_draft)
    d = {k: v.default for k, v in sig.parameters.items()}
    assert d['hot_wait'] == 30
    assert d['cold_wait'] == 90
    assert d['max_launch'] == 2
    assert d['allow_cold_restart'] is True
    # 冷启动后不额外热等（维护期参数）
    from export_config import get_admission_config
    cfg = get_admission_config() or {}
    assert (cfg.get('open') or {}).get('hot_wait_after_build') == 0
    # 就绪口径：导出与准入都等首页（准入经 ui_core.launch_via_manager → ensure_for_export）
    assert inspect.signature(jl.ensure_jianying).parameters['readiness'].default == 'home'


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f'✓ {fn.__name__}')
    print(f'\n{len(fns)} passed')
