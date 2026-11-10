# -*- coding: utf-8 -*-
"""pytest 全局隔离：任何测试都不弹真实桌面通知。

autouse fixture 会把 notifier.toast 及各模块里 `from notifier import toast`
留下的别名替换为内存记录器；用例可以声明 `toast_recorder` 参数拿记录列表，
断言「通知被调用过、标题/正文是什么」，桌面无任何动静。

需要真实验证通知外观时：ALLOW_TEST_TOAST=1（notifier.toast 内置门禁会自动
加 [测试] 前缀）。本文件仅被 pytest 加载，运行时不硬依赖 pytest。
"""
import sys

try:
    import pytest
except Exception:  # pragma: no cover - pytest 是测试期依赖，不是运行时依赖
    pytest = None

# 直接 `from notifier import toast` 的模块，其别名不会随 notifier 更新
_ALIAS_MODULES = ("cover_watch", "series_runner", "batch_runner", "auto_exporter")


def install_toast_recorder(calls, set_attr):
    """把 notifier 及已导入模块的 toast 别名替换为记录器，返回记录器本身。"""
    def recorder(title, message="", icon="info"):
        calls.append({"title": title, "message": message, "icon": icon})

    # notifier 可能同时以 notifier / utils.notifier 两种名字存在
    for name, mod in list(sys.modules.items()):
        if name == "notifier" or name.endswith(".notifier"):
            set_attr(mod, "toast", recorder, raising=False)
    for name in _ALIAS_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for attr in ("toast", "_toast"):
            if hasattr(mod, attr):
                set_attr(mod, attr, recorder, raising=False)
    return recorder


if pytest is not None:
    @pytest.fixture(autouse=True)
    def toast_recorder(monkeypatch):
        calls = []
        install_toast_recorder(calls, monkeypatch.setattr)
        return calls
