# -*- coding: utf-8 -*-
"""回归用例：lib/state_io.py 原子写的失败语义。

覆盖：
  ① 正常写入：文件落盘、补齐 version/updated_at/writer、无残留 .tmp
  ② os.replace 一直失败 → OSError 必须向上传播；.tmp 被清理；原文件未被破坏
  ③ save_if_changed 变更落盘失败 → 异常同样冒到调用方（不静默）
  ④ 内容未变（忽略 updated_at/writer）→ 幂等跳过，不刷 mtime

运行：python tests\\test_state_io.py（纯临时目录）
"""
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import state_io as sio  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "state_io_test")
shutil.rmtree(TMP, ignore_errors=True)
JD = os.path.join(TMP, "cover_job")
os.makedirs(JD, exist_ok=True)
NAME = ".export_state.json"
FP = os.path.join(JD, NAME)
TMP_FP = FP + ".tmp"


def content():
    with open(FP, encoding="utf-8") as f:
        return json.load(f)


print("\n== ① 正常写入 ==")
sio.atomic_write(JD, NAME, {"status": "pending", "attempts": 0})
check("文件已落盘", os.path.isfile(FP))
check("version 已补齐", content()["version"] == sio.STATE_VERSION, content())
check("updated_at/writer 已补齐", content()["updated_at"] and content()["writer"], content())
check("无残留 .tmp", not os.path.exists(TMP_FP))

print("\n== ② os.replace 持续失败 → 抛 OSError + 清理 tmp + 原文件不变 ==")
old = content()
with mock.patch.object(sio.os, "replace", side_effect=OSError("模拟占用")):
    try:
        sio.atomic_write(JD, NAME, {"status": "done", "attempts": 9, "sentinel": "new"})
        raised = None
    except OSError as e:
        raised = e
check("最终 OSError 向上传播", raised is not None, raised)
check("失败后 .tmp 已清理", not os.path.exists(TMP_FP))
check("原文件未被破坏（仍可解析）", isinstance(content(), dict))
check("原文件内容未被替换", content().get("status") == old.get("status") and
      content().get("attempts") == old.get("attempts"), content())
check("未把半成品写进目标文件", "sentinel" not in content())

print("\n== ③ save_if_changed 落盘失败 → 异常冒泡 ==")
with mock.patch.object(sio.os, "replace", side_effect=OSError("模拟占用")):
    try:
        sio.save_if_changed(JD, NAME, {"status": "retrying", "attempts": 9})
        raised = None
    except OSError as e:
        raised = e
check("save_if_changed 不吞异常", raised is not None, raised)
check("失败后 .tmp 已清理", not os.path.exists(TMP_FP))
check("原文件仍可解析", isinstance(content(), dict))

print("\n== ④ 幂等跳过 ==")
before_mtime = os.path.getmtime(FP)
sio.save_if_changed(JD, NAME, dict(content()))
check("内容未变不落盘（mtime 不变）", os.path.getmtime(FP) == before_mtime, os.path.getmtime(FP))

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(0 if not fails else 1)
