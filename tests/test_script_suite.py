# -*- coding: utf-8 -*-
"""脚本式回归用例统一入口。

仓库里多数 tests/test_*.py 是脚本式：模块级直接跑断言、末尾 sys.exit()。
直接交给 pytest 会在收集期 import 并触发 SystemExit（INTERNALERROR，退出码 3）。
conftest 已把这类文件排除出默认收集，这里参数化成「一个文件一个子进程用例」，
用退出码判定成败；脚本自身的断言与输出原样保留，不导入 pytest 进程。

运行：pytest tests/test_script_suite.py -q
"""
import os
import subprocess
import sys

import pytest

from conftest import is_script_test

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS_DIR)
_TIMEOUT_S = 300


def _script_files():
    out = []
    for name in sorted(os.listdir(_TESTS_DIR)):
        path = os.path.join(_TESTS_DIR, name)
        if os.path.isfile(path) and is_script_test(path):
            out.append(path)
    return out


SCRIPTS = _script_files()


@pytest.mark.parametrize("script", SCRIPTS, ids=[os.path.basename(p) for p in SCRIPTS])
def test_script(script):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            [sys.executable, script], cwd=_ROOT, env=env,
            capture_output=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"脚本式测试超时（>{_TIMEOUT_S}s）：{script}")
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        tail = "\n".join((out + "\n" + err).strip().splitlines()[-40:])
        pytest.fail(f"脚本式测试失败，退出码 {proc.returncode}：{script}\n{tail}")
