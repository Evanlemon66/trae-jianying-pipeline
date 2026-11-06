# -*- coding: utf-8 -*-
"""回归用例：plan / 闸门评估不许创建 .export_state.json。

运行：python tests\\test_plan_nowrite.py（纯临时目录）
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import cover_watch as cw  # noqa: E402
import export_state as es  # noqa: E402
import job_manifest as jm  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def make_job(tmp):
    jd = jm.create_job(series="plan测试剧", synopsis="简介",
                       out_dir=os.path.join(tmp, "plan测试剧"))["job_dir"]
    titles = [{"id": f"T{i}", "title": f"标题{i}", "score": {"情绪峰值": 10 - i}}
              for i in range(1, 4)]
    tpath = os.path.join(jd, "02_titles", "titles.json")
    with open(tpath, "w", encoding="utf-8") as f:
        json.dump({"titles": titles, "summary": {"recommended": []}}, f,
                  ensure_ascii=False)
    jm.select_titles(jd, [{"slot": i + 1, "title_id": f"T{i + 1}",
                           "title": f"标题{i + 1}"} for i in range(3)])
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w",
              encoding="utf-8") as f:
        json.dump({"channel": "test",
                   "items": [{"slot": 1}, {"slot": 2}, {"slot": 3},
                             {"slot": "vertical"}]}, f, ensure_ascii=False)
    covers = os.path.join(jd, "04_covers")
    for name in ("cover_01_hit.png", "cover_02_contrast.png",
                 "cover_03_guard.png", "cover_04_vertical.png"):
        with open(os.path.join(covers, name), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
    jm.update_job(jd, {"stage": "covers_ready"}, event="test_prepare")
    return jd


def main():
    tmp = tempfile.mkdtemp(prefix="plan_nowrite_")
    try:
        jd = make_job(tmp)
        state = os.path.join(jd, ".export_state.json")
        draft = os.path.join(tmp, "draft_folder")
        mp4 = os.path.join(tmp, "out.mp4")
        cfg = os.path.join(tmp, "watch.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"watch": {"new_job_grace_min": 0, "settle": 0,
                                 "export_policy": "immediate",
                                 "cold_start": False}}, f)

        # ① 直接调用闸门：缺失文件时纯内存推导，不落盘
        check("① 检查前无 .export_state.json", not os.path.exists(state))
        g = es.gate(jd, migrate_ctx={"video_status": None, "stage": "covers_ready"})
        check("① gate 可推导（返回 None）", g is None, g)
        check("① gate 后仍无 .export_state.json", not os.path.exists(state))

        # ② 跑 plan：有草稿 + 可导出，但 plan 只读
        with mock.patch.object(cw, "find_stream_draft", return_value=draft), \
                mock.patch.object(cw.pc, "export_mp4_path", return_value=mp4), \
                mock.patch.object(cw.pc, "get_export_dir", return_value=tmp):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cw.main(["plan", "--job", jd, "--config", cfg])
        check("② plan 正常返回", rc == 0, rc)
        check("② plan 后仍无 .export_state.json", not os.path.exists(state))
        check("② plan 输出含导出计划", "export" in buf.getvalue(), buf.getvalue()[:200])

        # ③ 对照：真实导出开始 begin() 才创建
        es.begin(jd, phase="export")
        check("③ begin 后创建 .export_state.json", os.path.exists(state))
        with open(state, encoding="utf-8") as f:
            st = json.load(f)
        check("③ begin 后 status=exporting", st.get("status") == "exporting",
              st.get("status"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print(f"\nFAILED: {len(fails)} 项 → {fails}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
