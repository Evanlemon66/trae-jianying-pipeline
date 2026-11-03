# -*- coding: utf-8 -*-
"""回归用例：导出状态机单一真源 cover_job/.export_state.json（lib/export_state.py）。

覆盖：
  ① 初始 pending
  ② 首次失败 → retrying + 退避时间
  ③ 连续失败到阈值 → paused
  ④ 成功 → done 且计数清零
  ⑤ exporting 心跳超时 → stale 回收
  ⑥ 旧文件迁移三种组合（paused / retrying / done）
  ⑦ 重复写幂等
  ⑧ 暂停状态下 plan 被拦、--phase export 放行

运行：python tests\\test_export_state.py（无第三方依赖，不真导出）
"""
import json, os, shutil, sys, tempfile, time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import export_state as es  # noqa: E402
import cover_watch as cw   # noqa: E402

cw.toast = lambda *a, **k: None  # 用例里不打桌面通知

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def fresh(name):
    d = os.path.join(TMP, name, "cover_job")
    os.makedirs(d, exist_ok=True)
    return d


TMP = os.path.join(tempfile.gettempdir(), "export_state_test")
shutil.rmtree(TMP, ignore_errors=True)

print("\n== ① 初始 pending ==")
jd = fresh("初始")
st = es.load(jd)
check("无真源无旧数据 → pending", st["status"] == "pending", st)
check("落盘了 .export_state.json", os.path.isfile(es.state_path(jd)))
check("初始计数为 0", st["attempts"] == 0 and st["consecutive_failures"] == 0)

print("\n== ② 首次失败 → retrying + 退避 ==")
jd = fresh("首败")
now = time.time()
st = es.fail(jd, "boom", pause_after=5, now=now)
check("status=retrying", st["status"] == "retrying", st)
check("consecutive_failures=1", st["consecutive_failures"] == 1)
check("退避≈60s", abs((st["next_retry_at"] - now) - 60) <= 2, st["next_retry_at"] - now)
check("记录 last_error", st["last_error"] == "boom")
check("闸门拦截（退避中）", (es.gate(jd) or "").startswith("导出失败退避中"))

print("\n== ③ 连续失败到阈值 → paused ==")
jd = fresh("暂停")
for i in range(4):
    st = es.fail(jd, f"e{i}", pause_after=5)
check("第 4 次仍 retrying", st["status"] == "retrying", st)
st = es.fail(jd, "e5", pause_after=5)
check("第 5 次触发 paused", st["status"] == "paused", st)
check("paused 后 next_retry_at=0", st["next_retry_at"] == 0)
check("闸门显示已暂停", "已暂停" in (es.gate(jd) or ""))

print("\n== ④ 成功 → done 且计数清零 ==")
st = es.succeed(jd)
check("status=done", st["status"] == "done", st)
check("计数清零", st["consecutive_failures"] == 0 and st["attempts"] == 0)
check("progress=100", st["progress"] == 100)
check("成功后闸门放行", es.gate(jd) is None)

print("\n== ⑤ exporting 心跳超时 → stale 回收 ==")
jd = fresh("崩溃回收")
now = time.time()
es.begin(jd, phase="export", now=now)
check("begin 后 status=exporting", es.load(jd)["status"] == "exporting")
# 心跳新鲜（刚 begin）→ 不回收
check("心跳新鲜不回收", es.reap_stale(jd, progress_mtime=0.0, now=now + 10) is False)
# 心跳超时（>180s）且无 progress → 回收
check("心跳超时回收", es.reap_stale(jd, progress_mtime=0.0, now=now + 400) is True)
check("回收后 status=retrying", es.load(jd)["status"] == "retrying")
# progress_mtime 新鲜 → 即便 heartbeat 旧也不回收（子进程仍在写盘）
jd2 = fresh("子进程仍活")
es.begin(jd2, phase="export", now=now)
check("progress 新鲜不回收",
      es.reap_stale(jd2, progress_mtime=now + 390, now=now + 400) is False)

print("\n== ⑥ 旧文件迁移三种组合 ==")
# A: .export_retry.json paused=true → paused（且备份）
jd = fresh("迁移A")
with open(os.path.join(jd, es.LEGACY_RETRY_NAME), "w", encoding="utf-8") as f:
    json.dump({"fails": 6, "paused": True, "next_ts": 0}, f)
st = es.load(jd, migrate_ctx={"video_status": None, "stage": "covers_ready"})
check("A 旧 paused → paused", st["status"] == "paused", st)
check("A 继承失败计数", st["consecutive_failures"] == 6)
check("A 备份 .bak 已生成", os.path.isfile(os.path.join(jd, es.LEGACY_RETRY_NAME + ".bak")))
# B: fails>0 未暂停 → retrying（带计数与退避点）
jd = fresh("迁移B")
future = time.time() + 500
with open(os.path.join(jd, es.LEGACY_RETRY_NAME), "w", encoding="utf-8") as f:
    json.dump({"fails": 3, "paused": False, "next_ts": future}, f)
st = es.load(jd, migrate_ctx={"video_status": None, "stage": "covers_ready"})
check("B 旧 fails → retrying", st["status"] == "retrying", st)
check("B 继承计数=3", st["consecutive_failures"] == 3)
check("B 继承退避点", abs(st["next_retry_at"] - future) <= 2)
# C: job video.status=done / stage=video_ready → done（最高优先，不回退）
jd = fresh("迁移C")
with open(os.path.join(jd, es.LEGACY_RETRY_NAME), "w", encoding="utf-8") as f:
    json.dump({"fails": 2, "paused": False, "next_ts": 0}, f)  # 即便有失败记录
st = es.load(jd, migrate_ctx={"video_status": "done", "stage": "video_ready"})
check("C 已导出 → done（压过失败记录）", st["status"] == "done", st)

print("\n== ⑦ 重复写幂等 ==")
jd = fresh("幂等")
es.begin(jd, phase="export")
a1 = es.load(jd)["attempts"]
es.begin(jd, phase="export")  # 已在 exporting，不应重复计次
a2 = es.load(jd)["attempts"]
check("重复 begin 不重复计次", a1 == a2 == 1, f"a1={a1} a2={a2}")
es.succeed(jd)
snap1 = open(es.state_path(jd), encoding="utf-8").read()
es.succeed(jd)  # 幂等
snap2 = open(es.state_path(jd), encoding="utf-8").read()
st = es.load(jd)
check("重复 succeed 仍 done 且计数 0", st["status"] == "done" and st["attempts"] == 0)
check("重复 succeed 内容不变", snap1 == snap2)

print("\n== ⑧ 暂停状态 plan 被拦 / --phase export 放行 ==")
jd = fresh("计划门禁")
for i in range(5):
    es.fail(jd, f"e{i}", pause_after=5)  # → paused
cw.find_stream_draft = lambda _name: os.path.join("D:", "fake", "测试剧串流")
job = {"workflow": {"mode": "full", "cut_s": None, "derive": []},
       "video": {}, "stage": "covers_ready"}
plan_blocked = cw.build_plan(jd, job, {"cold_start": False}, pending_edit=0)
check("paused 时 plan 被拦", plan_blocked["action"] == "blocked", plan_blocked["action"])
check("拦截原因含已暂停", "已暂停" in plan_blocked["reason"], plan_blocked["reason"])
plan_forced = cw.build_plan(jd, job, {"cold_start": False}, pending_edit=0,
                            phase_override="export")
check("--phase export 强制放行", plan_forced["action"] == "export", plan_forced["action"])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
