# -*- coding: utf-8 -*-
"""pytest 全局隔离：任何测试都不弹真实桌面通知。

autouse fixture 会把 notifier.toast 及各模块里 `from notifier import toast`
留下的别名替换为内存记录器；用例可以声明 `toast_recorder` 参数拿记录列表，
断言「通知被调用过、标题/正文是什么」，桌面无任何动静。

需要真实验证通知外观时：ALLOW_TEST_TOAST=1（notifier.toast 内置门禁会自动
加 [测试] 前缀）。本文件仅被 pytest 加载，运行时不硬依赖 pytest。
"""
import ast
import os
import sys

try:
    import pytest
except Exception:  # pragma: no cover - pytest 是测试期依赖，不是运行时依赖
    pytest = None

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _sub in ("lib", "pipeline", os.path.join(".trae", "skills", "jianying-editor", "examples")):
    _p = os.path.join(_ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

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

    @pytest.fixture(autouse=True)
    def isolated_stream_manifests(tmp_path, monkeypatch):
        """所有用例的串流清单一律写进 tmp_path，绝不落真实
        pipeline/_stream_manifests（用例跑完真实目录条目数必须不变）。

        覆盖两条实际写入路径：
          - stream_manifest.MANIFEST_DIR：record / clean / list 的读写根
          - job_manifest.STREAM_MANIFEST_DIR：record_video 的清单回写根
        都是函数内按模块全局取值，运行时 monkeypatch 即可生效。
        """
        import job_manifest as jm
        import stream_manifest as sm

        d = str(tmp_path / "stream_manifests")
        monkeypatch.setattr(sm, "MANIFEST_DIR", d)
        monkeypatch.setattr(jm, "STREAM_MANIFEST_DIR", d)
        return d


# ── 脚本式测试收集 ────────────────────────────────────────────────
# 仓库里多数 test_*.py 是「脚本式」：模块级直接执行断言，末尾 sys.exit()。
# pytest 在收集阶段 import 它们会执行整个脚本并撞上 SystemExit（INTERNALERROR，
# 退出码 3），既拿不到用例结果，也会在收集期产生真实副作用。
#
# 做法：这类文件不交给 pytest import，改由 tests/test_script_suite.py 参数化
# 成「一个文件一个子进程用例」，用退出码判定成败。脚本自身断言完全保留。


def _has_pytest_cases(path):
    """文件里是否有 pytest 能收集的 test_ 函数 / Test 类。"""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("test") or node.name.startswith("Test"):
                return True
    return False


def is_script_test(path):
    """是否为脚本式测试（无 pytest 用例定义的 test_*.py）。"""
    path = os.fspath(path)
    return (
        path.endswith(".py")
        and os.path.basename(path).startswith("test_")
        and os.path.dirname(os.path.abspath(path)) == _TESTS_DIR
        and os.path.basename(path) != "test_script_suite.py"
        and not _has_pytest_cases(path)
    )


def pytest_ignore_collect(collection_path, config):
    """脚本式测试不参与默认收集，统一由 test_script_suite.py 以子进程执行。"""
    if is_script_test(collection_path):
        return True
    return None
