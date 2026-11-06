# -*- coding: utf-8 -*-
"""回归用例：不裁剪剧的 Step 6 与构建头文案不再有容易误读的表达。

背景（2026-11-07）：一部 workflow.cut_s=null 的剧，日志里出现
`━ [6] 跳过裁剪 (cut_us=None)` 与 `✂️ cut_us : None  (None (no cut)s)`，
被读成「裁剪报错」。现改为明确的无裁剪表述。

验证方式：静态断言源码中新文案存在、旧文案不再存在。
运行：python tests\\test_cut_wording.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPE = os.path.join(_ROOT, "pipeline", "pipeline.py")

with open(PIPE, encoding="utf-8") as f:
    src = f.read()

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


print("\n== 不裁剪文案 ==")
check("Step 6 新文案存在", '━ [6] 无裁剪参数，跳过' in src)
check("Step 6 旧文案已移除", "跳过裁剪 (cut_us=None)" not in src)
check("构建头新文案存在", "✂️ cut_us     : 未指定（整片保留）" in src)
check("构建头旧文案已移除", "None (no cut)" not in src)
check("有裁剪时仍打印秒数", "✂️ cut_us     : {cut_us}  ({cut_us/1_000_000}s)" in src)

print(f"\n{'=' * 40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
