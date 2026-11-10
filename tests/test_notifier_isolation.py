# -*- coding: utf-8 -*-
"""测试期通知隔离：第一层（notifier 内置门禁）、第二层（conftest autouse）、第三层（显式放行）。

覆盖：
  1) pytest 环境且未放行 → 不触碰任何桌面通道，只写日志；
  2) pytest 环境 + ALLOW_TEST_TOAST=1 → 真弹（此处用 stub 捕获），标题带 [测试]；
  3) 非测试运行 → 正常通知，不加 [测试]（生产行为不变）；
  4) conftest 的记录器能替换 notifier.toast 及各模块别名，供测试断言。

运行：python tests\\test_notifier_isolation.py（也可在 pytest 下运行）
"""
import contextlib
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "utils"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notifier  # noqa: E402
import cover_watch as cw  # noqa: E402
import conftest  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


# 用 stub 捕获真实通知通道；直接拿 toast 的原始引用，避免被 conftest fixture 替换干扰
_real_toast = notifier.toast
_channel_calls = []
_orig_channels = {}
for _n in ("_win_toast", "_mac_toast", "_linux_toast"):
    _orig_channels[_n] = getattr(notifier, _n)
    setattr(notifier, _n, lambda *a, **k: _channel_calls.append(a))

_orig_env = {k: os.environ.get(k) for k in ("PYTEST_CURRENT_TEST", notifier.TEST_ALLOW_ENV)}


def _set_env(pytest_mode, allow):
    if pytest_mode:
        os.environ["PYTEST_CURRENT_TEST"] = "tests/test_notifier_isolation.py::case"
    else:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
    if allow:
        os.environ[notifier.TEST_ALLOW_ENV] = "1"
    else:
        os.environ.pop(notifier.TEST_ALLOW_ENV, None)


try:
    print("== 第一层：pytest 下默认不弹 ==")
    _set_env(True, False)
    _channel_calls.clear()
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        _real_toast("默认抑制", "正文", "warning")
    check("未触碰桌面通道", len(_channel_calls) == 0, _channel_calls)
    check("降级为日志", "toast suppressed in test" in err.getvalue(), err.getvalue())

    print("\n== 第三层：显式放行才真弹，且带 [测试] 前缀 ==")
    _set_env(True, True)
    _channel_calls.clear()
    _real_toast("放行标题", "放行正文", "success")
    check("放行后真弹一次", len(_channel_calls) == 1, _channel_calls)
    _title = _channel_calls[0][0] if _channel_calls else ""
    check("标题含 [测试] 前缀", notifier.TEST_PREFIX.strip() in _title, _title)
    check("正文未被污染", _channel_calls and _channel_calls[0][1] == "放行正文",
          _channel_calls)
    check("图标保留", _channel_calls and _channel_calls[0][2] == "success", _channel_calls)

    print("\n== 生产行为不变：非测试运行正常弹、无 [测试] ==")
    _set_env(False, False)
    _channel_calls.clear()
    _real_toast("生产标题", "生产正文", "error")
    check("生产下真弹一次", len(_channel_calls) == 1, _channel_calls)
    check("生产标题不带 [测试]",
          _channel_calls and notifier.TEST_PREFIX.strip() not in _channel_calls[0][0],
          _channel_calls)
    # ALLOW_TEST_TOAST 只在测试态生效，生产态即使误设也不应加前缀
    _set_env(False, True)
    _channel_calls.clear()
    _real_toast("生产标题2", "正文", "info")
    check("非测试态忽略 ALLOW 标记", len(_channel_calls) == 1 and
          notifier.TEST_PREFIX.strip() not in _channel_calls[0][0], _channel_calls)

    print("\n== 第二层：conftest autouse 记录器 ==")

    class _FakeMonkeypatch:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, value, raising=True):
            self._undo.append((obj, name, getattr(obj, name, None)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, old in self._undo:
                setattr(obj, name, old)

    mp = _FakeMonkeypatch()
    recorded = []
    recorder = conftest.install_toast_recorder(recorded, mp.setattr)
    check("notifier.toast 被替换为记录器", notifier.toast is recorder)
    check("cover_watch.toast 别名被替换", cw.toast is recorder)
    cw.toast("测试标题", "测试正文")
    check("记录器可断言标题/正文",
          recorded == [{"title": "测试标题", "message": "测试正文", "icon": "info"}],
          recorded)
    mp.undo()
    check("记录器撤销后 notifier.toast 复原", notifier.toast is _real_toast)
finally:
    for _n, _f in _orig_channels.items():
        setattr(notifier, _n, _f)
    for _k, _v in _orig_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v

print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
