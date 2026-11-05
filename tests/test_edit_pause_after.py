# -*- coding: utf-8 -*-
"""回归用例：edit 阶段暂停阈值可由 presets.watch.edit_pause_after 覆盖。

背景（2026-11-04）：edit 侧原为 DEFAULT_PAUSE_AFTER=3，导出侧为 5。
临时把编辑阶段放宽到 5 次（不改导出侧语义），避免数据修复期反复进入 paused。

覆盖：
  ① watch.edit_pause_after=5 → 第 1~4 次失败仍 retrying，第 5 次才 paused
  ② 未配置该键 → 回落到 DEFAULT_PAUSE_AFTER(3)，第 3 次即 paused（旧语义不变）

运行：python tests\\test_edit_pause_after.py
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))

import cover_watch as cw  # noqa: E402
import edit_state as eds  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def _new_job():
    d = tempfile.mkdtemp(prefix="edit_pause_")
    return d


def _status(d):
    p = os.path.join(d, ".edit_state.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


print("① watch.edit_pause_after=5")
cw.pc.get_watch_config = lambda: {"edit_pause_after": 5}
d = _new_job()
seq = []
for i in range(1, 5):
    cw.note_edit_result(d, "测试剧", False, error=f"err{i}", failure_class="unknown")
    seq.append(_status(d)["status"])
check("第 1~4 次失败均 retrying", seq == ["retrying"] * 4, seq)
cw.note_edit_result(d, "测试剧", False, error="err5", failure_class="unknown")
check("第 5 次失败才 paused", _status(d)["status"] == "paused", _status(d)["status"])
check("failure 计数=5", _status(d)["consecutive_failures"] == 5, _status(d)["consecutive_failures"])

print("② 未配置时回落默认 3")
cw.pc.get_watch_config = lambda: {}
d2 = _new_job()
for i in range(1, 3):
    cw.note_edit_result(d2, "测试剧", False, error=f"e{i}", failure_class="unknown")
check("第 2 次仍 retrying", _status(d2)["status"] == "retrying")
cw.note_edit_result(d2, "测试剧", False, error="e3", failure_class="unknown")
check("第 3 次 paused（旧语义不变）", _status(d2)["status"] == "paused", _status(d2)["status"])
check("默认阈值常量仍为 3", eds.DEFAULT_PAUSE_AFTER == 3, eds.DEFAULT_PAUSE_AFTER)

print()
if fails:
    print(f"❌ {len(fails)} 项失败: {fails}")
    sys.exit(1)
print("✅ 全部通过")
