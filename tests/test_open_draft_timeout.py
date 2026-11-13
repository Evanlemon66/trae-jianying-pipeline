# -*- coding: utf-8 -*-
"""回归用例：打开草稿墙钟（第 3 条稳定性修复）。

旧实现写死 40s 找卡片 + 60s 等编辑页（合计 100s），84~183 集大草稿首轮必然超时。
现改为：基础 120s（40 + 80），90 集之后按 0.5s/集放大，上限 240s；
基础值可用 presets.json 的 watch.draft_open_timeout_s 覆盖。

覆盖：
  ① 草稿名解析集数（第01-146集串流 / 第50集 / 无法解析）
  ② 默认预算：84 集=120s、146 集=148s、183 集=166.5s，均严格大于旧的 100s
  ③ watch.draft_open_timeout_s 覆盖基础值
  ④ 上限 240s；找卡片始终 ≤40s

运行：python tests\\test_open_draft_timeout.py（纯函数，不真打开草稿）
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, ".trae", "skills", "jianying-editor", "examples"))
import robust_auto_export as rae  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def near(a, b, tol=0.01):
    return abs(a - b) <= tol


print("\n== ① 草稿名解析集数 ==")
check("第01-84集串流 → 84",
      rae._draft_episodes("奶爸赶山：虎王认我当兄弟_第01-84集串流") == 84)
check("第01-146集串流 → 146",
      rae._draft_episodes("八零猎王，风雪赶山养妻护女_第01-146集串流") == 146)
check("单集草稿 → 50", rae._draft_episodes("某剧_第50集") == 50)
check("无集号 → 0", rae._draft_episodes("无名草稿") == 0)
check("None 不崩", rae._draft_episodes(None) == 0)

print("\n== ② 默认预算（基础 120s，>90 集每集 +0.5s）==")
f84, e84 = rae._open_draft_timeouts("某剧_第01-84集串流", {})
check("84 集合计 120s", near(f84 + e84, 120.0), (f84, e84))
check("84 集严格大于旧 100s", (f84 + e84) > 100.0)

f146, e146 = rae._open_draft_timeouts("某剧_第01-146集串流", {})
check("146 集合计 148s", near(f146 + e146, 148.0), (f146, e146))

f183, e183 = rae._open_draft_timeouts("某剧_第01-183集串流", {})
check("183 集合计 166.5s", near(f183 + e183, 166.5), (f183, e183))
check("找卡片 ≤40s", f183 <= 40.0, f183)

print("\n== ③ presets watch.draft_open_timeout_s 覆盖基础值 ==")
fc_, ec_ = rae._open_draft_timeouts("某剧_第01-84集串流", {"draft_open_timeout_s": 180})
check("配置 180s 生效（84 集不再放大）", near(fc_ + ec_, 180.0), (fc_, ec_))
fc2, ec2 = rae._open_draft_timeouts("某剧_第01-146集串流", {"draft_open_timeout_s": 180})
check("配置 180s + 146 集 = 208s", near(fc2 + ec2, 208.0), (fc2, ec2))
fc3, ec3 = rae._open_draft_timeouts("某剧_第01-84集串流", {"draft_open_timeout_s": "bad"})
check("非法配置回落到默认 120s", near(fc3 + ec3, 120.0), (fc3, ec3))

print("\n== ④ 上限 240s ==")
fmax, emax = rae._open_draft_timeouts("某剧_第01-1000集串流", {"draft_open_timeout_s": 300})
check("总预算封顶 240s", near(fmax + emax, 240.0), (fmax, emax))
check("封顶后找卡片仍 ≤40s", fmax <= 40.0, fmax)

print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
