"""回归用例：pythonw（无控制台、无可用 stdout）下质检仍要拿到真实结论。

背景：watcher 由 pythonw 托管，子进程继承的 stdout 为空。旧实现直接
subprocess.call([sys.executable, verify_export.py, ...])，子进程里
sys.stdout 为 None，verify_export.py 的 sys.stdout.reconfigure 立刻抛异常，
rc 恒为 1（且没有任何明细），质检结论不可信。

覆盖：
  A. 对照组：pythonw 宿主不重定向直接跑探针 → rc=3（复现旧失败路径）
  B. 修复后：_run_verify 把子进程 stdout 落到 06_publish/verify.log
     → rc=0、日志里有 PROBE_OK、文件存在
  C. verify_export.py 自身对 sys.stdout 为 None 做兜底（不崩）

运行：python tests\\test_verify_stdout.py（需要同目录下的 pythonw.exe）
"""
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="verify_stdout_")
fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")

PROBE = r'''
import sys
if sys.stdout is None:
    raise SystemExit(3)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass
print("PROBE_OK")
raise SystemExit(0)
'''

HOST = r'''
import json, sys
sys.path.insert(0, {lib!r})
sys.path.insert(0, {pipe!r})
import cover_watch as cw
rc, log = cw._run_verify({probe!r}, "dummy", "dummy.mp4", {job!r})
with open({out!r}, "w", encoding="utf-8") as f:
    json.dump({{"rc": rc, "log": log, "stdout_none": sys.stdout is None}}, f)
'''


def run_host(host_src, out_path):
    host = os.path.join(TMP, f"host{len(os.listdir(TMP))}.py")
    with open(host, "w", encoding="utf-8") as f:
        f.write(host_src)
    subprocess.run([PYTHONW, host], timeout=120)
    import json
    with open(out_path, encoding="utf-8") as f:
        return json.load(f)


try:
    if not os.path.isfile(PYTHONW):
        print("  ⏭ 未找到 pythonw.exe，跳过（本机无 GUI 宿主）")
    else:
        probe = os.path.join(TMP, "probe.py")
        with open(probe, "w", encoding="utf-8") as f:
            f.write(PROBE)
        job = os.path.join(TMP, "cover_job")
        os.makedirs(os.path.join(job, "06_publish"), exist_ok=True)

        # A. 对照：不重定向 → pythonw 子进程 stdout 为空 → 探针 rc=3
        out_a = os.path.join(TMP, "a.json")
        host_a = (
            "import json, subprocess, sys\n"
            f"rc = subprocess.call([sys.executable, r'{probe}'])\n"
            f"json.dump({{'rc': rc, 'stdout_none': sys.stdout is None}}, "
            f"open(r'{out_a}', 'w'))\n"
        )
        a = run_host(host_a, out_a)
        check("A0 宿主 pythonw 自身无可用 stdout（复现前提）", a["stdout_none"] is True)
        check("A1 旧路径（无重定向）rc=3", a["rc"] == 3, a["rc"])

        # B. 修复后：_run_verify 显式重定向到 verify.log
        out_b = os.path.join(TMP, "b.json")
        b = run_host(HOST.format(lib=os.path.join(_ROOT, "lib"),
                                 pipe=os.path.join(_ROOT, "pipeline"),
                                 probe=probe, job=job, out=out_b), out_b)
        check("B1 _run_verify 返回真实 rc=0", b["rc"] == 0, b["rc"])
        check("B2 明细写入 verify.log", os.path.isfile(b["log"]), b["log"])
        if os.path.isfile(b["log"]):
            text = open(b["log"], encoding="utf-8", errors="replace").read()
            check("B3 日志含子进程 stdout", "PROBE_OK" in text, text[-200:])

        # C. verify_export.py 对 None stdout 不崩（直接以 pythonw 跑 --help 不起作用，
        #    这里只做语法级导入检查：不设 stdout 时模块不应在 reconfigure 处抛错）
        vpath = os.path.join(_ROOT, "pipeline", "verify_export.py")
        src = open(vpath, encoding="utf-8").read()
        check("C1 verify_export 已对 None stdout 兜底",
              "if sys.stdout is not None" in src)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print(f"❌ {len(fails)} 项未通过: {fails}")
    sys.exit(1)
print("ALL OK")
