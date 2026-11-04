# -*- coding: utf-8 -*-
"""回归用例：validate --fix（fix_job）门禁数据修复。

覆盖：
  ① 字符串数组 selected + gate="selected"（非法）→ 对象数组 + gate=done
  ② 只有 2 条 + 非法 gate → gate 归 pending、不完整 selected 清空
  ③ 已合规文件 → fixed: 0 且完全不写盘（字节级比对）
  ④ 纯读 validate（不带 --fix）不改文件
  ⑤ 修复后 cover_watch.check_trigger 通过
  ⑥ CLI --fix 输出 [fix] 明细与 fixed: N

运行：python tests\\test_validate_fix.py（无第三方依赖，不真导出）
"""
import json, os, shutil, subprocess, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402
import cover_watch as cw   # noqa: E402

cw.toast = lambda *a, **k: None

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25  # 契约文件名命中即可，无需真实尺寸


def make_fixture(name, *, gate, selected, stage="covers_ready"):
    jd = os.path.join(TMP, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    with open(os.path.join(jd, "02_titles", "titles.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"id": "T3", "title": "标题三"},
                              {"id": "T5", "title": "标题五"},
                              {"id": "T12", "title": "标题十二"}]},
                  f, ensure_ascii=False)
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"slot": 1}, {"slot": 2}, {"slot": 3}, {"slot": "vertical"}]}, f)
    for stem in ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"):
        with open(os.path.join(jd, "04_covers", stem + ".png"), "wb") as f:
            f.write(PNG)
    job = jm.build_job(name)
    job["stage"] = stage
    job["titles"]["gate"] = gate
    job["titles"]["selected"] = selected
    job["titles"]["selected_by"] = "user"
    jm.save_job(job, jd)
    return jd


def read_bytes(jd):
    with open(os.path.join(jd, "job.json"), "rb") as f:
        return f.read()


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, os.path.join(_ROOT, "lib", "job_manifest.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=60)


TMP = os.path.join(tempfile.gettempdir(), "validate_fix_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

print("\n== ① 字符串数组 + gate=selected → 对象数组 + done ==")
jd = make_fixture("修复A", gate="selected", selected=["T3", "T5", "T12"])
rev0 = jm.load_job(os.path.join(jd, "job.json"))["revision"]
res = jm.fix_job(jd)
job = jm.load_job(os.path.join(jd, "job.json"))
check("changed=True 且 fixed=2", res["changed"] and res["fixed"] == 2, res)
check("gate → done", job["titles"]["gate"] == "done", job["titles"]["gate"])
sel = job["titles"]["selected"]
check("selected → 3 个对象", len(sel) == 3 and all(isinstance(e, dict) for e in sel), sel)
check("title_id 保留 T3/T5/T12", [e["title_id"] for e in sel] == ["T3", "T5", "T12"], sel)
check("title 文本从 titles.json 回填", [e["title"] for e in sel] == ["标题三", "标题五", "标题十二"], sel)
check("slot 依次 1/2/3", [e["slot"] for e in sel] == [1, 2, 3], sel)
check("revision+1", job["revision"] == rev0 + 1, job["revision"])
check("history 末条 event=validate_fix", job["history"][-1]["event"] == "validate_fix",
      job["history"][-1])
check("修复后 validate 通过", jm.validate_job(job, jd) == [], jm.validate_job(job, jd))

print("\n== ② 只有 2 条 + 非法 gate → pending 且清空 ==")
jd = make_fixture("修复B", gate="selected", selected=["T3", "T5"])
res = jm.fix_job(jd)
job = jm.load_job(os.path.join(jd, "job.json"))
check("gate → pending", job["titles"]["gate"] == "pending", job["titles"]["gate"])
check("不完整 selected 清空", job["titles"]["selected"] == [], job["titles"]["selected"])
check("报告含 pending 理由", any("pending" in x["reason"] for x in res["fixes"]
                                 if x["field"] == "titles.gate"), res["fixes"])
check("修复后 validate 通过", jm.validate_job(job, jd) == [], jm.validate_job(job, jd))

print("\n== ③ 已合规 → fixed: 0 且不写盘 ==")
jd = make_fixture("合规C", gate="done", selected=[
    {"slot": 1, "title_id": "T3", "title": "标题三"},
    {"slot": 2, "title_id": "T5", "title": "标题五"},
    {"slot": 3, "title_id": "T12", "title": "标题十二"}])
before = read_bytes(jd)
res = jm.fix_job(jd)
check("changed=False 且 fixed=0", not res["changed"] and res["fixed"] == 0, res)
check("字节级未写盘（含 updated_at 不刷新）", read_bytes(jd) == before)

print("\n== ④ 纯读 validate 不改文件 ==")
jd = make_fixture("纯读D", gate="selected", selected=["T3", "T5", "T12"])
before = read_bytes(jd)
r = run_cli("validate", "--job", jd)
check("非法数据纯读校验报错（exit 1）", r.returncode == 1, r.stdout + r.stderr)
check("纯读不改文件", read_bytes(jd) == before)

print("\n== ⑤ 修复后 check_trigger 通过 ==")
ok, reasons = cw.check_trigger(jd)
check("修复前 check_trigger 不通过（gate 非法）", not ok
      and any("gate" in x for x in reasons), reasons)
r = run_cli("validate", "--fix", "--job", jd)
check("CLI --fix 退出码 0", r.returncode == 0, r.stdout + r.stderr)
ok, reasons = cw.check_trigger(jd)
check("修复后 check_trigger=True", ok, reasons)

print("\n== ⑥ CLI --fix 输出明细 ==")
check("输出含 [fix] 明细行", r.stdout.count("[fix]") == 2, r.stdout)
check("输出含 fixed: 2", "fixed: 2" in r.stdout, r.stdout)
check("输出含 [OK] 通过校验", "[OK]" in r.stdout, r.stdout)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'='*40}\n{'全部通过' if not fails else '失败: ' + '、'.join(fails)}")
sys.exit(1 if fails else 0)
