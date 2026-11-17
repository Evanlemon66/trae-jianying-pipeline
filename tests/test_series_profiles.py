# -*- coding: utf-8 -*-
"""回归用例：series_runner 多版本导出（--profile）。

覆盖：
  A 无 job 上下文 → 只导基础版 a
  B 显式 --profile → 基础版 a 恒在 + 指定版本
  C prompts.json items[].group 是版本真源
  D prompts 缺失时回落到 job.workflow.preset_profile
  E 附加版本草稿命名 / 版本回推
  F 旧状态文件（无 profiles 子键）不报错

运行：python tests\\test_series_profiles.py（纯临时目录）
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
import series_runner as sr  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "series_profiles_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)


def make_job(tag, *, groups=None, preset_profile=None):
    jd = os.path.join(TMP, tag, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    jm.save_job(jm.build_job(tag), jd)
    if groups:
        with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
            json.dump({"items": [{"group": g} for g in groups]}, f)
    if preset_profile:
        jm.update_job(jd, {"workflow": {"preset_profile": preset_profile}},
                      event="test", detail="profiles")
    return jd


print("\n== A 无 job 上下文 ==")
check("A1 默认只导基础版", sr._resolve_export_profiles(None, None) == ["a"],
      sr._resolve_export_profiles(None, None))

print("\n== B 显式 --profile ==")
check("B1 基础版恒在", sr._resolve_export_profiles(None, ["b"]) == ["a", "b"])
check("B2 多版本保序", sr._resolve_export_profiles(None, ["b", "tiktok"]) == ["a", "b", "tiktok"])
check("B3 重复版本去重", sr._resolve_export_profiles(None, ["a", "b", "b"]) == ["a", "b"])

print("\n== C prompts.json 是真源 ==")
jd_c = make_job("profC", groups=["b", "a"])
check("C1 按 items[].group 解析且 a 优先",
      sr._resolve_export_profiles(jd_c, None) == ["a", "b"],
      sr._resolve_export_profiles(jd_c, None))
jd_c2 = make_job("profC2", groups=["tiktok"])
check("C2 未声明 a 时仍补基础版",
      sr._resolve_export_profiles(jd_c2, None) == ["a", "tiktok"],
      sr._resolve_export_profiles(jd_c2, None))

print("\n== D 回落到 job 声明 ==")
jd_d = make_job("profD", preset_profile="b,tiktok")
check("D1 无 prompts 用 preset_profile",
      sr._resolve_export_profiles(jd_d, None) == ["a", "b", "tiktok"],
      sr._resolve_export_profiles(jd_d, None))
check("D2 显式参数优先于 job 声明",
      sr._resolve_export_profiles(jd_d, ["b"]) == ["a", "b"])

print("\n== E 命名 / 版本回推 ==")
base = "八零猎王_第01-142集串流"
check("E1 附加版本草稿名", sr._account_draft_name(base, "b") == base + "_b")
check("E2 反向识别版本", sr._version_of_draft(base + "_b", ["a", "b"]) == "b")
check("E3 基础版草稿名不含后缀", sr._version_of_draft(base, ["a", "b"]) == "a")

print("\n== F 旧状态兼容 ==")
state = {"old_target": {"status": "done", "draft": base}}
st = sr._state_of(state, "old_target")
check("F1 旧状态仍可读", st.get("status") == "done" and st.get("draft") == base)
check("F2 旧状态可补 profiles 子键", isinstance(st.setdefault("profiles", {}), dict))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
