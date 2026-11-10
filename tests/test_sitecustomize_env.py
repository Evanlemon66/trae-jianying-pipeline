# -*- coding: utf-8 -*-
"""sitecustomize 跨进程隐藏窗口 + 子进程环境注入测试。

覆盖：
  A. JY_HIDE_WINDOW=1 时 sitecustomize 强制安装补丁（即使有控制台）
  B. 未设 JY_HIDE_WINDOW 时完全无副作用（不安装、不改 Popen）
  C. hidden_child_env 只影响子进程，父进程 os.environ 不变
  D. PYTHONPATH 前置 pipeline 且不破坏原有值 / 重复调用不重复前置
  E. sitecustomize 内部异常被吞掉，不影响启动

运行：python tests\\test_sitecustomize_env.py
"""
import importlib.util
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PIPELINE = os.path.join(_ROOT, "pipeline")
sys.path.insert(0, _PIPELINE)
import no_window as nw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def load_sitecustomize(tag):
    path = os.path.join(_PIPELINE, "sitecustomize.py")
    spec = importlib.util.spec_from_file_location(f"_sc_test_{tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # 异常应按约定被吞掉，不向外抛
    return mod


_real_popen = subprocess.Popen
_real_console = nw._console_available
_orig_env = dict(os.environ)
try:
    # ── B：未设开关 → 无副作用 ──────────────────────────────────
    os.environ.pop("JY_HIDE_WINDOW", None)
    nw._installed = False
    before_init = subprocess.Popen.__init__
    load_sitecustomize("off")
    check("B1 未设 JY_HIDE_WINDOW 时不安装", nw._installed is False, nw._installed)
    check("B2 未设 JY_HIDE_WINDOW 时 Popen.__init__ 不变",
          subprocess.Popen.__init__ is before_init)

    # ── A：设开关 → 强制安装（即使判定「有控制台」）─────────────
    os.environ["JY_HIDE_WINDOW"] = "1"
    nw._installed = False
    nw._console_available = lambda: True  # 模拟启发式判断为「有控制台」
    load_sitecustomize("on")
    patched = getattr(subprocess.Popen.__init__, "_no_window_patched", False)
    check("A1 JY_HIDE_WINDOW=1 时 sitecustomize 强制安装", patched is True, patched)

    # ── E：安装过程抛异常 → 被吞掉，不向外传播 ──────────────────
    nw._installed = False
    real_install = nw.install_default_hidden
    nw.install_default_hidden = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    ok = True
    try:
        load_sitecustomize("boom")
    except Exception as e:  # 不应发生
        ok = False
        err = e
    finally:
        nw.install_default_hidden = real_install
    check("E1 补丁抛异常时 sitecustomize 静默跳过", ok, "" if ok else err)

    # ── C：hidden_child_env 不影响父进程 ─────────────────────────
    parent_pythonpath = os.environ.get("PYTHONPATH")
    parent_hide = os.environ.get("JY_HIDE_WINDOW")
    child = nw.hidden_child_env()
    check("C1 子进程环境带 JY_HIDE_WINDOW=1", child.get("JY_HIDE_WINDOW") == "1")
    check("C2 父进程 JY_HIDE_WINDOW 未被改写",
          os.environ.get("JY_HIDE_WINDOW") == parent_hide)
    check("C3 父进程 PYTHONPATH 未被改写",
          os.environ.get("PYTHONPATH") == parent_pythonpath)

    # ── D：PYTHONPATH 合并规则 ──────────────────────────────────
    old = os.pathsep.join(["C:/keep_a", "C:/keep_b"])
    child = nw.hidden_child_env({"PYTHONPATH": old})
    parts = child["PYTHONPATH"].split(os.pathsep)
    check("D1 pipeline 目录被前置", parts[0] == _PIPELINE, parts[:2])
    check("D2 原有 PYTHONPATH 值完整保留",
          parts[1:] == ["C:/keep_a", "C:/keep_b"], parts)
    child2 = nw.hidden_child_env(child)
    check("D3 重复注入不重复前置",
          child2["PYTHONPATH"].split(os.pathsep).count(_PIPELINE) == 1,
          child2["PYTHONPATH"])
    check("D4 其余环境变量原样继承",
          nw.hidden_child_env({"PATH": "X"}).get("PATH") == "X")
finally:
    subprocess.Popen = _real_popen
    nw._console_available = _real_console
    nw._installed = False
    os.environ.clear()
    os.environ.update(_orig_env)

print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
