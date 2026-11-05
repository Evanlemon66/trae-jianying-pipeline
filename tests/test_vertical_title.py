# -*- coding: utf-8 -*-
"""竖版封面默认标题回归：最高分 / 同分取靠前 / 人工指定优先 / 评分缺失或非法回落。

运行：python tests\\test_vertical_title.py（无第三方依赖）
"""
import json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import job_manifest as jm  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def new_job(tmp, name):
    return jm.create_job(series=name, synopsis="测试简介",
                         out_dir=os.path.join(tmp, name))["job_dir"]


def write_titles(job_dir, items):
    path = os.path.join(job_dir, "02_titles", "titles.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"titles": items, "summary": {"recommended": []}}, f,
                  ensure_ascii=False, indent=2)
    return path


def pick(job_dir, ids):
    picks = [{"slot": i + 1, "title_id": tid, "title": f"标题{tid}"}
             for i, tid in enumerate(ids)]
    return jm.select_titles(job_dir, picks, by="user")


def main():
    tmp = tempfile.mkdtemp(prefix="vertical_title_")
    try:
        # ① 最高分不是第一个
        jd = new_job(tmp, "高分非首位")
        write_titles(jd, [
            {"id": "T1", "title": "甲", "score": {"情绪峰值": 5}},
            {"id": "T2", "title": "乙", "score": {"情绪峰值": 12}},
            {"id": "T3", "title": "丙", "score": {"情绪峰值": 9}},
        ])
        job = pick(jd, ["T1", "T2", "T3"])
        check("① 最高分非首个 → 选 T2",
              job["vertical_cover"]["title_id"] == "T2",
              job["vertical_cover"])

        # ② 同分取 selected 中出现靠前的
        jd = new_job(tmp, "同分取先")
        write_titles(jd, [
            {"id": "T1", "title": "甲", "score": {"情绪峰值": 10}},
            {"id": "T2", "title": "乙", "score": {"情绪峰值": 10}},
            {"id": "T3", "title": "丙", "score": {"情绪峰值": 4}},
        ])
        job = pick(jd, ["T1", "T2", "T3"])
        check("② 同分取靠前 → T1",
              job["vertical_cover"]["title_id"] == "T1",
              job["vertical_cover"])

        # ③ 人工显式指定优先，不被最高分覆盖
        jd = new_job(tmp, "人工优先")
        write_titles(jd, [
            {"id": "T1", "title": "甲", "score": {"情绪峰值": 12}},
            {"id": "T2", "title": "乙", "score": {"情绪峰值": 6}},
            {"id": "T3", "title": "丙", "score": {"情绪峰值": 4}},
        ])
        jm.update_job(jd, {"vertical_cover": {"title_id": "T3", "title": "人工指定"}},
                      event="manual_vertical")
        job = pick(jd, ["T1", "T2", "T3"])
        check("③ 人工指定优先 → 仍为 T3",
              job["vertical_cover"]["title_id"] == "T3",
              job["vertical_cover"])

        # ④ 条目缺 score → 不绑定，保持 null
        jd = new_job(tmp, "缺评分")
        write_titles(jd, [
            {"id": "T1", "title": "甲"},
            {"id": "T2", "title": "乙"},
            {"id": "T3", "title": "丙"},
        ])
        job = pick(jd, ["T1", "T2", "T3"])
        check("④ score 缺失 → title_id 保持 null",
              (job.get("vertical_cover") or {}).get("title_id") is None,
              job.get("vertical_cover"))

        # ⑤ score 非法（非对象）→ 不绑定，不报错
        jd = new_job(tmp, "非法评分")
        write_titles(jd, [
            {"id": "T1", "title": "甲", "score": "92"},
            {"id": "T2", "title": "乙", "score": {"情绪峰值": 8}},
            {"id": "T3", "title": "丙", "score": {"情绪峰值": 7}},
        ])
        job = pick(jd, ["T1", "T2", "T3"])
        check("⑤ score 非法 → title_id 保持 null",
              (job.get("vertical_cover") or {}).get("title_id") is None,
              job.get("vertical_cover"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nFAILED: {len(fails)} 项 → {fails}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
