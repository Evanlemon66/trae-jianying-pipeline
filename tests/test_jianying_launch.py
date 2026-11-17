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
    # 单测注入 no-op 清残留：真实清理会 taskkill 残留进程，测试环境不该碰真实进程。
    kw.setdefault('cleanup_fn', lambda _log: [])
    # 检测器日志停滞判死会 stat 真实日志目录（假时钟 1000.0 会把真实日志都算进来），
    # 纯逻辑用例一律关掉；需要该行为的用例显式打开并注入日志探测。
    kw.setdefault('detect_detector_stall', False)
    return jl.ensure_jianying(
        exe_path='fake.exe', require_exists=False, validate_version=False,
        alive_fn=fake.alive, spawn_fn=fake.spawn,
        time_fn=clock.time, sleep_fn=clock.sleep, **kw)


# ── 1. 常量口径（必须保持不变的行为参数）──────────────────────────
def test_constants_pinned():
    assert jl.REQUIRED_JY_VERSION == '5.9.0.11632'
    assert jl.HOME_READY_TIMEOUT_S == 120.0
    assert jl.EARLY_DEATH_WINDOW_S == 20.0
    # 显式重试策略：最多重拉 2 次（共 3 次尝试），固定间隔 2s，不做指数退避。
    assert jl.EARLY_DEATH_RETRIES == 2
    assert jl.EARLY_DEATH_SETTLE_S == 2.0
    # 秒退单独一档：≤10s 自己退出 = 启动即退（确定性崩溃），与软失败分开。
    assert jl.INSTANT_DEATH_MAX_S == 10.0
    assert jl.RESIDUAL_CLEAN_WAIT_S == 5.0
    assert jl.RESTART_SETTLE_S == 0.0
    assert jl.POST_READY_WATCH_S == 3.0
    # 检测器停滞判死阈值：成功样本检测器日志最大行间隔 1.0s，取 5s 留余量
    assert jl.DETECTOR_STALL_S == 5.0
    # 崩溃框独立预算：本机 nvapi 已连续崩 3 次，不能被早死预算吃掉
    assert jl.CRASH_RETRIES == 2
    assert jl.CRASH_SETTLE_S == 3.0
    # 导出链通过别名引用同一口径
    assert ji.HOME_READY_TIMEOUT_S == 120.0
    assert ji.EARLY_DEATH_RETRIES == jl.EARLY_DEATH_RETRIES
    assert ji.EARLY_DEATH_WINDOW_S == 20.0
    assert ji.EARLY_DEATH_SETTLE_S == jl.EARLY_DEATH_SETTLE_S
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


# ── 3. 启动即死重拉：预算 2，不随 attempt 放大 ───────────────────
def test_early_death_retry_budget_bounded():
    clock = Clock()
    fake = FakeJianying(clock, [4.5, 4.5, 4.5])
    out = _run(clock, fake, timeout=180.0, retries=jl.EARLY_DEATH_RETRIES)
    assert out.ok is False
    assert out.outcome == 'died'
    assert out.death_kind == 'instant'
    assert out.exit_at, '必须记录退出时刻'
    assert len(fake.spawns) == 3, '重拉 2 次 = 最多 3 次尝试（不无限重试）'
    # 三次死亡各 ~5s + 两次 2s 固定间隔
    assert clock.t == 1019.0, clock.t
    # 预算耗尽时给出明确的终态错误（次数 / 退出时刻 / 类型）
    assert 'launch_exhausted' in out.error and 'instant' in out.error, out.error


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


# ── 4. 重拉前：先清残留，再按显式间隔（2s）拉起 ──────────────────
def test_cleanup_and_settle_before_respawn():
    clock = Clock()
    fake = FakeJianying(clock, [4.5, None])
    cleaned = []
    _run(clock, fake, retries=1, cleanup_fn=lambda _log: cleaned.append(1) or [])
    assert len(fake.spawns) == 2
    assert cleaned == [1], '重拉前必须执行一次残留清理'
    assert jl.EARLY_DEATH_SETTLE_S in clock.sleeps, '重拉前必须按显式间隔静置'
    assert fake.spawns[1] - fake.spawns[0] == 5.0 + jl.EARLY_DEATH_SETTLE_S


# ── 4b. ready 后又死：必须可见并进重试（B5）────────────────────
def test_post_ready_death_is_retried():
    """就绪后 2s 死亡 → 判 died_after_ready，消耗预算重拉，重拉后 ready。"""
    clock = Clock()
    fake = FakeJianying(clock, [2.0, 1e9])
    out = _run(clock, fake, ready_fn=lambda: True, retries=1)
    assert out.ok is True and out.outcome == 'ready'
    assert out.attempt == 2 and len(fake.spawns) == 2


def test_post_ready_death_without_budget_is_not_ready():
    clock = Clock()
    fake = FakeJianying(clock, [2.0])
    out = _run(clock, fake, ready_fn=lambda: True, retries=0)
    assert out.ok is False and out.outcome == 'died_after_ready'
    assert out.ready_at_s is not None
    assert 2.0 <= out.post_ready_died_at_s <= 3.0, out.post_ready_died_at_s
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


def test_detector_stall_kills_and_retries():
    """检测器日志停滞 + 主日志 0 字节 + 从未建窗 + 进程存活 → 立即判死重拉。

    本机失败 A1：检测器日志停在 +6s、主日志 0 字节、主进程到 +32s 才退出。
    这里断言判死发生在 ~+5s（不是 32s），且第二次尝试照常就绪。
    """
    clock = Clock()
    fake = FakeJianying(clock, [1e9, 1e9])
    logs = []

    def _stat(started):
        # 首次尝试：日志写到 21426 后不再增长；重拉后：每轮都增长（健康）
        if len(fake.spawns) <= 1:
            return 'VeDetector.log', 21426
        return 'VeDetector.log', 21426 + int((clock.t - started) * 1000)

    _orig = (jl.latest_detector_log_stat, jl.jianying_log_snapshot,
             jl._stop_stalled_instance)
    killed = []
    jl.latest_detector_log_stat = _stat
    jl.jianying_log_snapshot = lambda _s: ('JianyingPro.log', 0)
    jl._stop_stalled_instance = lambda _log, grace_s=1.5: killed.append(1) or True
    try:
        out = _run(clock, fake, ready_fn=lambda: len(fake.spawns) >= 2,
                   log_fn=logs.append, detect_detector_stall=True)
    finally:
        (jl.latest_detector_log_stat, jl.jianying_log_snapshot,
         jl._stop_stalled_instance) = _orig
    assert out.ok is True and out.attempt == 2, out.attempt
    assert len(fake.spawns) == 2
    assert killed, '停滞实例必须被关闭后再重拉'
    stalled = [l for l in logs if l.startswith('[detector-stalled]')]
    assert stalled, logs
    assert '零增长=5.0s' in stalled[0] or '零增长=6.0s' in stalled[0], stalled[0]
    # 首尝试判死时刻 ≈ 5s 停滞，远早于原来的 32s 晚死
    died = [l for l in logs if l.startswith('[startup-died]')]
    assert died and 'kind=detector_stalled' in died[0], died
    assert 'alive_s=5.0' in died[0] or 'alive_s=6.0' in died[0], died[0]


def test_detector_stall_ignored_when_app_log_has_content():
    """主日志已有内容（应用层在推进）→ 检测器日志静默也不算死路。

    健康实例在检测器收尾后到建窗之间也有 ~7s 静默，只靠"零增长"会误杀。
    """
    clock = Clock()
    fake = FakeJianying(clock, [1e9])
    logs = []
    _orig = (jl.latest_detector_log_stat, jl.jianying_log_snapshot,
             jl._stop_stalled_instance)
    killed = []
    jl.latest_detector_log_stat = lambda _s: ('VeDetector.log', 46683)
    jl.jianying_log_snapshot = lambda _s: ('JianyingPro.log', 19795)
    jl._stop_stalled_instance = lambda _log, grace_s=1.5: killed.append(1) or True
    try:
        out = _run(clock, fake, timeout=20.0, window_seen_fn=lambda: False,
                   log_fn=logs.append, detect_detector_stall=True)
    finally:
        (jl.latest_detector_log_stat, jl.jianying_log_snapshot,
         jl._stop_stalled_instance) = _orig
    assert out.outcome == 'window_not_created', out.outcome
    assert not killed, '主日志有进度时不允许判死'
    assert not [l for l in logs if l.startswith('[detector-stalled]')], logs



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


# ── 9. 第三层：进程活着但从未建窗 = window_not_created（独立一类）────
def test_timeout_without_any_window_is_window_not_created():
    clock = Clock()
    fake = FakeJianying(clock, [1e9])           # 进程一直活着
    out = _run(clock, fake, timeout=30.0, window_seen_fn=lambda: False)
    assert out.ok is False
    assert out.outcome == 'window_not_created', out.outcome
    assert out.death_kind == 'no_window'
    assert out.window_seen_at_s is None
    assert 'window_not_created' in out.error
    assert '从未创建' in out.error


def test_timeout_with_window_seen_stays_timeout():
    """窗口出现过但页面没渲染好 → 仍是 timeout，不冤枉成未建窗。"""
    clock = Clock()
    fake = FakeJianying(clock, [1e9])
    out = _run(clock, fake, timeout=10.0, window_seen_fn=lambda: True)
    assert out.outcome == 'timeout', out.outcome
    assert out.window_seen_at_s == 0.0


def test_ready_records_window_seen_at():
    clock = Clock()
    fake = FakeJianying(clock, [1e9])
    out = _run(clock, fake, ready_fn=lambda: True, retries=0,
               window_seen_fn=lambda: True)
    assert out.ok is True and out.window_seen_at_s is not None


def test_ledger_marks_window_not_created(monkeypatch):
    rows = []
    monkeypatch.setattr(jl, '_ledger_append', lambda rec: rows.append(rec))
    out = jl.LaunchOutcome(outcome='window_not_created', death_kind='no_window',
                           alive_s=120.0, attempt=1)
    jl._record_launch(out, launch_id='x', entry='selftest', readiness='home',
                      started_iso='2026-11-15T00:00:00+08:00')
    assert rows[0]['result'] == 'window_not_created'
    assert rows[0]['window_seen_at_s'] is None
    assert rows[0]['death_kind'] == 'no_window'


def test_failure_class_has_window_not_created():
    import failure_class as fc
    assert fc.WINDOW_NOT_CREATED in fc.CATEGORIES
    cls, action = fc.classify('[window_not_created alive=120.0s pid=123 attempt=1] ')
    assert cls == fc.WINDOW_NOT_CREATED and action == fc.RESTART
    # 不能混进 launch_failed
    cls2, _ = fc.classify('[launch_failed:instant alive=1.2s attempt=2]')
    assert cls2 == fc.LAUNCH_FAILED


def test_poll_thread_com_is_sta_initialized():
    """探测线程必须已做 STA 初始化：线程内再 CoInitializeEx(STA) 应返回 S_FALSE。"""
    import ctypes

    def probe():
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        if hr == 0:  # S_OK = 本线程此前没初始化过 → 探针环境不合格
            ctypes.windll.ole32.CoUninitialize()
            return 'not_initialized'
        return 'initialized'

    ok, val = jl.poll_with_timeout(probe, 5.0)
    assert ok is True and val == 'initialized', val


def test_poll_records_exception_type_and_message():
    """异常仍返回 False，但类型与消息要落到 PROBE_ERRORS，便于回溯超时原因。"""
    jl.PROBE_ERRORS.clear()

    def boom():
        raise OSError(-2147221008, '尚未调用 CoInitialize。')

    ok, val = jl.poll_with_timeout(boom, 5.0)
    assert ok is False and val is None
    assert jl.PROBE_ERRORS, '异常未记录'
    assert jl.PROBE_ERRORS[-1].startswith('OSError:')
    assert 'CoInitialize' in jl.PROBE_ERRORS[-1]


def test_late_death_without_window_is_retried():
    """>20s 才死但从未建出窗口 → 属于启动链没走完，按新口径照样重拉。"""
    clock = Clock()
    fake = FakeJianying(clock, [25.0, 25.0, 25.0])
    out = _run(clock, fake, timeout=180.0, retries=jl.EARLY_DEATH_RETRIES,
               window_seen_fn=lambda: False)
    assert out.outcome == 'died_late', out.outcome
    assert out.window_seen_at_s is None
    assert len(fake.spawns) == 3, '无窗口晚死也必须重拉到预算上限'
    assert '没有建出窗口' in out.error or '从未建出窗口' in out.error, out.error


def test_late_death_after_window_seen_keeps_old_policy():
    """已建出窗口后再晚死（>20s）→ 沿用原口径，不重拉。"""
    clock = Clock()
    fake = FakeJianying(clock, [25.0])
    out = _run(clock, fake, timeout=180.0, retries=jl.EARLY_DEATH_RETRIES,
               window_seen_fn=lambda: True)
    assert out.outcome == 'died_late', out.outcome
    assert out.window_seen_at_s is not None
    assert len(fake.spawns) == 1, '窗口建出后的晚死不重拉'
    assert 'launch_exhausted' in out.error, out.error


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in fns:
        fn()
        print(f'✓ {fn.__name__}')
    print(f'\n{len(fns)} passed')
