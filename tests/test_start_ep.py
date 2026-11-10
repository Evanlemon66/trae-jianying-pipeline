# -*- coding: utf-8 -*-
"""start_ep → watcher 编辑命令 --only 的回归测试。

运行：python tests\\test_start_ep.py（无第三方依赖、不弹通知、不启动子进程）
"""
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))

import job_manifest as jm  # noqa: E402
import cover_watch as cw  # noqa: E402
import batch_runner as br  # noqa: E402

cw.toast = lambda *a, **k: None  # 不弹真实通知

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def make_job_dir(tmp, name="start_ep 测试剧"):
    jd = os.path.join(tmp, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试")
    job = jm.build_job(name)
    job["titles"]["gate"] = "done"
    jm.save_job(job, jd)
    return jd


def write_ec(jd, payload=None, raw=None):
    p = os.path.join(jd, "export_config.json")
    with open(p, "w", encoding="utf-8") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump(payload, f, ensure_ascii=False)


def edit_cmd(jd):
    with open(os.path.join(jd, "job.json"), encoding="utf-8") as f:
        job = json.load(f)
    plan = cw.build_plan(jd, job, {}, 0, phase_override="edit",
                        notify=False, write_log=False)
    return json.loads(json.dumps(plan["cmd"], ensure_ascii=False))


def only_args(cmd):
    return [cmd[i + 1] for i, v in enumerate(cmd) if v == "--only"]


TMP = os.path.join(tempfile.gettempdir(), "start_ep_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

print("── A. start_ep 接入编辑命令 ──")
jd = make_job_dir(TMP)

write_ec(jd, {"start_ep": 2})
cmd = edit_cmd(jd)
check("A1 start_ep=2 → 含 --only 2..", only_args(cmd) == ["2.."], str(cmd))
check("A2 --no-export 仍在", "--no-export" in cmd, str(cmd))

write_ec(jd, {})
cmd = edit_cmd(jd)
check("A3 字段缺失 → 不含 --only", "--only" not in cmd, str(cmd))

os.remove(os.path.join(jd, "export_config.json"))
cmd = edit_cmd(jd)
check("A4 文件不存在 → 不含 --only", "--only" not in cmd, str(cmd))

write_ec(jd, {"start_ep": 1})
cmd = edit_cmd(jd)
check("A5 start_ep=1 → 不含 --only", "--only" not in cmd, str(cmd))

print("\n── B. 非法值走默认且不影响其它参数 ──")
for label, payload, raw in [
    ("字符串", {"start_ep": "3"}, None),
    ("浮点", {"start_ep": 2.5}, None),
    ("bool", {"start_ep": True}, None),
    ("0", {"start_ep": 0}, None),
    ("负数", {"start_ep": -5}, None),
    ("坏 JSON", None, "{not json"),
]:
    shutil.rmtree(jd, ignore_errors=True)
    jd = make_job_dir(TMP, name=f"bad_{label}")
    if raw is None:
        write_ec(jd, payload)
    else:
        write_ec(jd, raw=raw)
    cmd = edit_cmd(jd)
    check(f"B {label} → 无 --only", "--only" not in cmd, str(cmd))
    check(f"B {label} → 其它参数不变", "--force" in cmd and "--no-export" in cmd, str(cmd))

print("\n── C. start_ep 与 cut 共存 ──")
shutil.rmtree(jd, ignore_errors=True)
jd = make_job_dir(TMP, name="with_cut")
write_ec(jd, {"start_ep": 20})
job = jm.load_job(os.path.join(jd, "job.json"))
job["workflow"]["cut_s"] = 5
jm.save_job(job, jd)
cmd = edit_cmd(jd)
check("C1 --only 20.. 与 --cut 5 同时存在",
      only_args(cmd) == ["20.."] and float(cmd[cmd.index("--cut") + 1]) == 5.0, str(cmd))

print("\n── D. batch_runner 开区间解析 ──")
eps, open_min = br._parse_only(["2.."])
check("D1 '2..' → 开区间下限 2", eps == set() and open_min == 2, f"{eps},{open_min}")
eps, open_min = br._parse_only(["20..30"])
check("D2 '20..30' 闭区间不变", eps == set(range(20, 31)) and open_min is None)
eps, open_min = br._parse_only(["3"])
check("D3 单集不变", eps == {3} and open_min is None)
check("D4 开区间命中第20集", br._only_match(20, eps, 2) is True)
check("D5 开区间不命中第1集", br._only_match(1, set(), 2) is False)
check("D6 空选择全选", br._only_match(99, set(), None) is True)

print()
if fails:
    print(f"FAILED {len(fails)}: {fails}")
    sys.exit(1)
print("ALL OK: test_start_ep")
