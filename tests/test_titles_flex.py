"""回归用例：标题候选放宽（12 或其整数倍、T/N 混合编号）+ titles.spec_version 元信息。

覆盖：
  A. 24 条（T1—T12 + N1—N12）+ selected T5/N1/N4 → validate 通过
  B. 24 条 + titles.spec_version 字符串 → 通过；非字符串 → 失败
  C. selected 引用不存在的 id / 重复 id → 失败
  D. 12 条旧格式（titles 数组 + text 字段）→ 行为不变，通过
  E. count=13（非 12 的整数倍）→ 失败且错误信息含实际值
  F. 编号不连续只告警不阻断（title_id_warnings + record_titles 仍成功）
  G. record-titles 接受 24 条并记录 count/spec_version，拒绝 13 条
  H. select CLI 支持 N 前缀编号
  I. ingest 透传 spec_version 与候选数

运行：python tests\\test_titles_flex.py（无第三方依赖，不真导出）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402

TMP = tempfile.mkdtemp(prefix="titles_flex_")
fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25


def write_titles(path, *, count=24, field="title", spec="title-spec-v4.2",
                 wrap="items", gap=False):
    """写 titles.json：count=24 时 T1—T12 + N1—N12；count=12 时只有 T 组。"""
    items = []
    old_n = 12
    for i in range(1, old_n + 1):
        items.append({"id": f"T{i}", field: f"旧规范标题{i}"})
    if count > old_n:
        for i in range(1, count - old_n + 1):
            items.append({"id": f"N{i}", field: f"平台标题{i}"})
    if gap:  # 去掉末号、补一个更大的号：数量不变但序号不连续（只缺 N12）
        last = count - old_n
        items = [it for it in items if it["id"] != f"N{last}"]
        items.append({"id": f"N{last + 1}", field: "平台标题补位"})
    data = {wrap: items}
    if spec is not None:
        data["spec_version"] = spec
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def make_fixture(name, *, count=24, selected=("T5", "N1", "N4"), spec="title-spec-v4.2",
                 field="title", wrap="items", count_field=None, gap=False,
                 stage="covers_ready", spec_in_job=None):
    jd = os.path.join(TMP, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    write_titles(os.path.join(jd, "02_titles", "titles.json"), count=count,
                 field=field, spec=spec, wrap=wrap, gap=gap)
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"slot": 1}, {"slot": 2}, {"slot": 3}, {"slot": "vertical"}]}, f)
    for stem in ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"):
        with open(os.path.join(jd, "04_covers", stem + ".png"), "wb") as f:
            f.write(PNG)

    job = jm.build_job(name)
    job["stage"] = stage
    job["titles"]["count"] = count if count_field is None else count_field
    job["titles"]["gate"] = "done" if selected else "pending"
    job["titles"]["selected_by"] = "user" if selected else None
    job["titles"]["selected"] = [
        {"slot": i + 1, "title_id": tid, "title": f"标题{tid}"}
        for i, tid in enumerate(selected)
    ]
    if spec_in_job is not None:
        job["titles"]["spec_version"] = spec_in_job
    for i, tid in enumerate(selected):
        job["cover_map"][i]["title_id"] = tid
        job["cover_map"][i]["title"] = f"标题{tid}"
    if selected:
        job["vertical_cover"]["title_id"] = selected[0]
        job["vertical_cover"]["title"] = f"标题{selected[0]}"
    jm.save_job(job, jd)
    return jd


def load_errors(jd):
    return jm.validate_job(jm.load_job(os.path.join(jd, "job.json")), jd)


def run_cli(*argv):
    return subprocess.run(
        [sys.executable, os.path.join(_ROOT, "lib", "job_manifest.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=120)


try:
    # ── A. 24 条 + 混合编号 → 通过
    jd = make_fixture("pass24")
    errs = load_errors(jd)
    check("A1 24 条 T/N 混合 selected 通过校验", errs == [], errs)
    job = jm.load_job(os.path.join(jd, "job.json"))
    check("A2 selected 保持 N 前缀原样",
          [e["title_id"] for e in job["titles"]["selected"]] == ["T5", "N1", "N4"])

    # ── B. spec_version：字符串通过、非字符串失败
    jd = make_fixture("spec_ok", spec_in_job="title-spec-v4.2")
    check("B1 spec_version 字符串通过", load_errors(jd) == [], load_errors(jd))
    jd = make_fixture("spec_missing", spec=None)
    check("B2 spec_version 缺失（legacy）通过", load_errors(jd) == [], load_errors(jd))
    jd = make_fixture("spec_bad", spec_in_job=20261108)
    errs = load_errors(jd)
    check("B3 spec_version 非字符串失败",
          any("spec_version" in e for e in errs), errs)

    # ── C. 引用不存在 / 重复 → 失败
    jd = make_fixture("bad_ref", selected=("T5", "N1", "N99"))
    errs = load_errors(jd)
    check("C1 selected 引用不存在的 id 失败",
          any("不存在" in e and "N99" in e for e in errs), errs)
    jd = make_fixture("dup_sel", selected=("T5", "N1", "N1"))
    errs = load_errors(jd)
    check("C2 selected 重复 id 失败", any("重复" in e for e in errs), errs)

    # ── D. 12 条旧格式（titles 数组 + text 字段）行为不变
    jd = make_fixture("legacy12", count=12, selected=("T1", "T2", "T3"),
                      field="text", wrap="titles", spec=None)
    check("D1 12 条旧格式通过", load_errors(jd) == [], load_errors(jd))

    # ── E. 非 12 的整数倍 → 失败并报实际值
    jd = make_fixture("bad_count", count_field=13)
    errs = load_errors(jd)
    check("E1 count=13 失败且说明实际值",
          any("整数倍" in e and "13" in e for e in errs), errs)

    # ── F. 编号不连续：只告警
    jd = make_fixture("gap", selected=("T5", "N1", "N4"), gap=True)
    job = jm.load_job(os.path.join(jd, "job.json"))
    warns = jm.title_id_warnings(job, jd)
    check("F1 不连续产生告警", any("不连续" in w for w in warns), warns)
    check("F2 不连续不阻断校验", load_errors(jd) == [], load_errors(jd))

    # ── G. record-titles：24 条通过、13 条拒绝、spec_version 透传
    jd = os.path.join(TMP, "rec24", "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    jm.save_job(jm.build_job("rec24"), jd)
    tp = os.path.join(jd, "02_titles", "titles.json")
    write_titles(tp, count=24)
    job = jm.record_titles(jd, tp)
    check("G1 record-titles 接受 24 条", job["titles"]["count"] == 24, job["titles"]["count"])
    check("G2 record-titles 透传 spec_version",
          job["titles"].get("spec_version") == "title-spec-v4.2", job["titles"].get("spec_version"))
    write_titles(tp, count=13, spec=None)
    try:
        jm.record_titles(jd, tp)
        check("G3 record-titles 拒绝 13 条", False)
    except ValueError as e:
        check("G3 record-titles 拒绝 13 条", "整数倍" in str(e) and "13" in str(e), e)
    write_titles(tp, count=24, spec=None, gap=True)
    try:
        job = jm.record_titles(jd, tp)
        check("G4 不连续仍可登记（只告警）", job["titles"]["count"] == 24)
    except ValueError as e:
        check("G4 不连续仍可登记（只告警）", False, e)
    jd_old = os.path.join(TMP, "rec_nospec", "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd_old, d), exist_ok=True)
    with open(os.path.join(jd_old, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    jm.save_job(jm.build_job("rec_nospec"), jd_old)
    tp_old = os.path.join(jd_old, "02_titles", "titles.json")
    write_titles(tp_old, count=12, spec=None)
    jm.record_titles(jd_old, tp_old)
    check("G5 旧数据不补 spec_version", "spec_version" not in
          jm.load_job(os.path.join(jd_old, "job.json"))["titles"])

    # ── H. select CLI 支持 N 前缀
    jd = make_fixture("cli_select", selected=(), stage="titles_done")
    r = run_cli("select", "--job", jd, "--picks", "T5,N1,N4")
    check("H1 select CLI 接受 N 编号", r.returncode == 0 and "[OK]" in r.stdout,
          (r.stdout + r.stderr)[-300:])
    job = jm.load_job(os.path.join(jd, "job.json"))
    check("H2 CLI 回填 N 编号与标题",
          [e["title_id"] for e in job["titles"]["selected"]] == ["T5", "N1", "N4"]
          and job["titles"]["selected"][1]["title"] == "平台标题1",
          job["titles"]["selected"])

    # ── I. ingest 透传 spec_version 与候选数
    jd = make_fixture("ingest24", spec_in_job=None, count_field=12)
    rep = jm.ingest_job(jd)
    job = jm.load_job(os.path.join(jd, "job.json"))
    check("I1 ingest 同步候选数为 24", job["titles"]["count"] == 24, job["titles"]["count"])
    check("I2 ingest 透传 spec_version",
          job["titles"].get("spec_version") == "title-spec-v4.2", job["titles"].get("spec_version"))
    check("I3 ingest 后仍通过校验", load_errors(jd) == [], load_errors(jd))
    check("I4 ingest 报告记录归一动作",
          any("spec_version" in n for n in rep["normalized"]), rep["normalized"])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print(f"❌ {len(fails)} 项失败: " + "; ".join(fails))
    sys.exit(1)
print("✅ 全部通过")
