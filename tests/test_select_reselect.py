# -*- coding: utf-8 -*-
"""回归用例：select 在 covers_ready 之后重选（stage 不回退 + derived 标记 + 下游清除）。

运行：python tests\\test_select_reselect.py（纯临时目录）
"""
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import job_manifest as jm  # noqa: E402
import cover_watch as cw  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def new_job(tmp, name):
    return jm.create_job(series=name, synopsis="测试简介",
                         out_dir=os.path.join(tmp, name))["job_dir"]


def write_titles(job_dir, items):
    """写候选：不足 12 条时补到 T1—T12（候选数须为 12 的整数倍）。"""
    have = {it["id"] for it in items}
    padded = list(items) + [{"id": f"T{i}", "title": f"标题{i}"}
                            for i in range(1, 13) if f"T{i}" not in have]
    padded.sort(key=lambda it: int(it["id"][1:]))
    path = os.path.join(job_dir, "02_titles", "titles.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"titles": padded, "summary": {"recommended": []}}, f,
                  ensure_ascii=False, indent=2)


def pick(job_dir, ids):
    picks = [{"slot": i + 1, "title_id": tid, "title": f"标题{tid}"}
             for i, tid in enumerate(ids)]
    return jm.select_titles(job_dir, picks, by="user")


def make_covers_and_prompts(job_dir):
    covers = os.path.join(job_dir, "04_covers")
    os.makedirs(covers, exist_ok=True)
    for name in ("cover_01_hit.png", "cover_02_contrast.png",
                 "cover_03_guard.png", "cover_04_vertical.png"):
        with open(os.path.join(covers, name), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
    prompts = os.path.join(job_dir, "03_prompts")
    os.makedirs(prompts, exist_ok=True)
    with open(os.path.join(prompts, "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"channel": "test",
                   "items": [{"slot": 1}, {"slot": 2}, {"slot": 3},
                             {"slot": "vertical"}]}, f, ensure_ascii=False)


def main():
    tmp = tempfile.mkdtemp(prefix="select_reselect_")
    try:
        # ① prepared 起选：行为不变，stage 推进到 titles_selected
        jd = new_job(tmp, "prepared选")
        write_titles(jd, [
            {"id": "T1", "title": "甲", "score": {"情绪峰值": 5}},
            {"id": "T2", "title": "乙", "score": {"情绪峰值": 12}},
            {"id": "T3", "title": "丙", "score": {"情绪峰值": 9}},
        ])
        job = pick(jd, ["T1", "T2", "T3"])
        check("① prepared → stage=titles_selected", job["stage"] == "titles_selected",
              job["stage"])
        check("① prepared → 无 covers_stale 标记",
              not (job.get("derived") or {}).get("covers_stale"),
              job.get("derived"))

        # ② covers_ready 后重选：不抛错、stage 不变、derived 标记
        jm.update_job(jd, {"stage": "covers_ready"}, event="test_prepare")
        make_covers_and_prompts(jd)
        write_titles(jd, [
            {"id": "T1", "title": "甲", "score": {"情绪峰值": 5}},
            {"id": "T2", "title": "乙", "score": {"情绪峰值": 12}},
            {"id": "T3", "title": "丙", "score": {"情绪峰值": 9}},
            {"id": "T4", "title": "丁", "score": {"情绪峰值": 7}},
            {"id": "T5", "title": "戊", "score": {"情绪峰值": 6}},
            {"id": "T6", "title": "己", "score": {"情绪峰值": 3}},
        ])
        job = pick(jd, ["T4", "T5", "T6"])
        check("② covers_ready 重选成功", job["stage"] == "covers_ready", job["stage"])
        check("② stage 未回退", job["stage"] != "titles_selected", job["stage"])
        check("② selected 已更新",
              [s["title_id"] for s in job["titles"]["selected"]] == ["T4", "T5", "T6"],
              job["titles"]["selected"])
        check("② derived.covers_stale=True",
              (job.get("derived") or {}).get("covers_stale") is True,
              job.get("derived"))
        check("② derived 带重做原因",
              "04_covers" in str((job.get("derived") or {}).get("reason")),
              job.get("derived"))
        last = (job.get("history") or [{}])[-1]
        check("② history detail 明示封面重做",
              "04_covers 需重新生成" in str(last.get("detail")),
              last.get("detail"))
        ok, reasons = cw.check_trigger(jd)
        check("② watcher 被 covers_stale 阻断",
              (not ok) and any("covers_stale" in r for r in reasons),
              reasons)

        # ③ gate 回退仍被拒绝，且文案不再自相矛盾
        try:
            jm.update_job(jd, {"titles": {"gate": "pending"}}, event="test")
            raised = False
            msg = ""
        except ValueError as e:
            raised = True
            msg = str(e)
        check("③ gate done→pending 仍拒绝", raised, msg)
        check("③ 文案指向 select 且不再说「重选请用 select 命令」",
              "select 命令" in msg and "重选请用 select 命令" not in msg, msg)

        # ④ 下游消费并清除：4 张封面重建后 ingest 清 derived.covers_stale
        report = jm.ingest_job(jd)
        check("④ ingest 后 covers_stale=False",
              (report.get("derived") or {}).get("covers_stale") is not True)
        reloaded = jm.load_job(os.path.join(jd, "job.json"))
        check("④ job.json 中 covers_stale=False",
              (reloaded.get("derived") or {}).get("covers_stale") is False,
              reloaded.get("derived"))
        check("④ ingest 报告记录清除动作",
              any("covers_stale" in s for s in report.get("normalized", [])),
              report.get("normalized"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nFAILED: {len(fails)} 项 → {fails}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
