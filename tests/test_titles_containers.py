"""回归用例：titles.json 唯一候选容器 + 规范外顶层数组的保守合并。

覆盖：
  A. {"titles":12, "placement":12}（真实历史形态）→ 24，N2/N6 可解析、校验通过
  B. {"items":24, "selected":4}（selected 与 items 重叠）→ 仍 24，不并入、不破 12 倍数
  C. {"items":24} → 24，无告警
  D. 裸数组 24 → 24，无告警
  E. 未知兄弟数组 12 条（规范外容器名）→ 合并 24 并告警
  F. 未知兄弟数组非 12 整数倍 → 跳过并告警，主集合不变
  G. 读取不改写：已规范文件字节不变
  H. record-titles 对 placement 形态登记 count=24

运行：python tests\\test_titles_containers.py（无第三方依赖，不真导出）
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))
import job_manifest as jm  # noqa: E402

TMP = tempfile.mkdtemp(prefix="titles_containers_")
fails = []
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 25


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def entries(prefix, n=12):
    return [{"id": f"{prefix}{i}", "text": f"{prefix}标题{i}"} for i in range(1, n + 1)]


def make_job(name, data, *, selected=("T4", "N2", "N6"), count=24):
    jd = os.path.join(TMP, name, "cover_job")
    for d in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, d), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    tp = os.path.join(jd, "02_titles", "titles.json")
    with open(tp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(os.path.join(jd, "03_prompts", "prompts.json"), "w", encoding="utf-8") as f:
        json.dump({"items": [{"slot": 1}, {"slot": 2}, {"slot": 3}, {"slot": "vertical"}]}, f)
    for stem in ("cover_01_hit", "cover_02_contrast", "cover_03_guard", "cover_04_vertical"):
        with open(os.path.join(jd, "04_covers", stem + ".png"), "wb") as f:
            f.write(PNG)
    job = jm.build_job(name)
    job["stage"] = "covers_ready"
    job["titles"]["count"] = count
    job["titles"]["gate"] = "done"
    job["titles"]["selected_by"] = "user"
    job["titles"]["selected"] = [
        {"slot": i + 1, "title_id": t} for i, t in enumerate(selected)
    ]
    for i, t in enumerate(selected):
        job["cover_map"][i]["title_id"] = t
    job["vertical_cover"]["title_id"] = selected[0]
    jm.save_job(job, jd)
    return jd, tp


def read_capture(tp):
    """跑一次解析，返回 (候选, 捕获的 stdout)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        items = jm._load_titles_items(tp)
    return items, buf.getvalue()


try:
    # ── A. titles:12 + placement:12 → 24
    data = {"titles": entries("T"), "placement": entries("N"),
            "selected": ["T4", "N2", "N6"], "gate": "done", "spec": "title-spec-v4.2"}
    jd, tp = make_job("placement", data)
    items, out = read_capture(tp)
    check("A1 placement 并入后 24 条", len(items) == 24, len(items))
    idx = jm.title_candidate_index(jd, None)
    check("A2 候选索引含 N2/N6", {"N2", "N6"} <= set(idx), sorted(idx))
    errs = jm.validate_job(jm.load_job(os.path.join(jd, "job.json")), jd)
    check("A3 placement 形态通过校验", errs == [], errs)
    check("A4 并入时给出告警", "placement" in out and "已并入候选" in out, out)
    texts = jm._title_texts(jd, None)
    check("A5 _title_texts 能取到 N2 文本", texts.get("N2") == "N标题2", texts.get("N2"))

    # ── B. 兄弟数组与主集合重叠 → 按重复副本忽略
    data = {"items": entries("T") + entries("N"), "placement": entries("N")}
    jd, tp = make_job("overlap", data)
    items, out = read_capture(tp)
    check("B1 重叠兄弟数组不并入（仍 24）", len(items) == 24, len(items))
    check("B2 重叠时按重复副本忽略并告警", "重复副本忽略" in out, out)
    check("B3 重叠不破校验", jm.validate_job(jm.load_job(os.path.join(jd, "job.json")), jd) == [])
    # 业务键 selected（真实数据顶层形态）属 denylist，永不并入
    data = {"items": entries("T") + entries("N"), "selected": ["T1", "T2", "T3", "N1"]}
    jd, tp = make_job("denylist", data)
    items, _out = read_capture(tp)
    check("B4 顶层 selected 不并入（仍 24）", len(items) == 24, len(items))

    # ── C. items:24 → 24，无告警
    jd, tp = make_job("items24", {"items": entries("T") + entries("N")})
    items, out = read_capture(tp)
    check("C1 items:24 保持 24", len(items) == 24, len(items))
    check("C2 规范文件无告警", out.strip() == "", out)

    # ── D. 裸数组 → 24，无告警
    jd, tp = make_job("bare", entries("T") + entries("N"))
    items, out = read_capture(tp)
    check("D1 裸数组保持 24", len(items) == 24, len(items))
    check("D2 裸数组无告警", out.strip() == "", out)

    # ── E. 未知兄弟数组 12 条 → 合并并告警
    data = {"items": entries("T"), "extra_titles": entries("N")}
    jd, tp = make_job("extra", data)
    items, out = read_capture(tp)
    check("E1 未知兄弟数组 12 条并入（24）", len(items) == 24, len(items))
    check("E2 并入时点名容器名", "extra_titles" in out, out)

    # ── F. 未知兄弟数组非 12 整数倍 → 跳过并告警
    data = {"items": entries("T"), "draft": entries("N", 7)}
    jd, tp = make_job("odd", data)
    items, out = read_capture(tp)
    check("F1 非 12 倍数兄弟数组不并入（12）", len(items) == 12, len(items))
    check("F2 跳过时告警", "不是 12 的整数倍" in out, out)

    # ── G. 读取不改写（字节不变）
    jd, tp = make_job("nowrite", {"items": entries("T") + entries("N")})
    before = open(tp, "rb").read()
    read_capture(tp)
    jm.title_candidate_index(jd, None)
    check("G1 解析不写回文件", open(tp, "rb").read() == before)

    # ── H. record-titles 对 placement 形态登记 24 条
    jd, tp = make_job("rec", {})
    with open(tp, "w", encoding="utf-8") as f:
        json.dump({"titles": entries("T"), "placement": entries("N")},
                  f, ensure_ascii=False, indent=2)
    jm.save_job(jm.build_job("rec"), jd)
    job = jm.record_titles(jd, tp)
    check("H1 record-titles 登记 24 条", job["titles"]["count"] == 24, job["titles"]["count"])

    # ── I. 条目级校验（record_titles / validate_job / ingest_job 共用）
    def fresh(name):
        jd = os.path.join(TMP, name, "cover_job")
        for d in jm.JOB_DIRS:
            os.makedirs(os.path.join(jd, d), exist_ok=True)
        with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
            f.write("测试简介")
        jm.save_job(jm.build_job(name), jd)
        return jd

    def write_t(jd, data):
        tp = os.path.join(jd, "02_titles", "titles.json")
        with open(tp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return tp

    def raises(fn, needle):
        try:
            fn()
        except ValueError as e:
            return needle in str(e), str(e)
        except Exception as e:  # noqa: BLE001
            return False, f"非 ValueError: {e!r}"
        return False, "未抛错"

    jd = fresh("entry_missing_text")
    tp = write_t(jd, {"items": entries("T")[:11] + [{"id": "T12"}]})
    ok, msg = raises(lambda: jm.record_titles(jd, tp), "缺少正文")
    check("I1 record-titles 缺 text 阻断", ok, msg)

    jd = fresh("entry_bad_id")
    tp = write_t(jd, {"items": entries("T")[:11] + [{"id": "第一条", "text": "x"}]})
    ok, msg = raises(lambda: jm.record_titles(jd, tp), "id 形态非法")
    check("I2 record-titles id 形态非法阻断", ok, msg)

    jd = fresh("entry_dup")
    dup_items = entries("T")[:11] + [{"id": "T1", "text": "重复"}]
    tp = write_t(jd, {"items": dup_items})
    ok, msg = raises(lambda: jm.record_titles(jd, tp), "编号重复")
    check("I3 record-titles 重复编号阻断", ok, msg)

    # 旧字段名只告警、不阻断（存量兼容）
    jd = fresh("entry_legacy_names")
    legacy = [{"title_id": f"T{i}", "title": f"旧标题{i}"} for i in range(1, 13)]
    tp = write_t(jd, {"items": legacy})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        job = jm.record_titles(jd, tp)
    out = buf.getvalue()
    check("I4 title_id/title 旧名放行（12 条）", job["titles"]["count"] == 12, job["titles"]["count"])
    check("I5 旧名给出告警", "旧字段名" in out, out)

    # 绕过 record-titles 直接落盘 → validate_job 也能拦
    jd, tp = make_job("bypass_validate", {"items": entries("T") + entries("N")})
    with open(tp, "w", encoding="utf-8") as f:
        json.dump({"items": entries("T")[:11] + [{"id": "N12"}]},
                  f, ensure_ascii=False, indent=2)
    errs = jm.validate_job(jm.load_job(os.path.join(jd, "job.json")), jd)
    check("I6 validate_job 拦住绕过落盘（缺正文）",
          any("缺少正文" in e for e in errs), errs)

    # ingest_job 同源校验（直接落盘坏条 → raise）
    jd, tp = make_job("bypass_ingest", {"items": entries("T") + entries("N")})
    with open(tp, "w", encoding="utf-8") as f:
        json.dump({"items": entries("T")[:11] + [{"id": "N12"}]},
                  f, ensure_ascii=False, indent=2)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok, msg = raises(lambda: jm.ingest_job(jd), "缺少正文")
    check("I7 ingest_job 拦住绕过落盘（缺正文）", ok, msg)

    # ── J. spec → spec_version 旧名兼容并透传
    jd = fresh("spec_alias")
    tp = write_t(jd, {"items": entries("T"), "spec": "title-spec-v4.2"})
    job = jm.record_titles(jd, tp)
    check("J1 spec 透传为 spec_version",
          job["titles"]["spec_version"] == "title-spec-v4.2",
          job["titles"].get("spec_version"))
    job2 = jm.load_job(os.path.join(jd, "job.json"))
    job2["titles"]["spec_version"] = None
    notes = jm.normalize_job_shape(job2, jd)
    check("J2 normalize 认 spec 旧名透传",
          job2["titles"]["spec_version"] == "title-spec-v4.2"
          and any("spec_version" in n for n in notes),
          notes)

    # ── K. auto 兜底认 scores（7 元素数组）
    jd = fresh("scores_alias")
    scores = [3, 4, 5, 5, 4, 3, 5]
    items = [{"id": f"T{i}", "text": f"标题{i}", "scores": list(scores),
              "total": sum(scores)} for i in range(1, 13)]
    tp = write_t(jd, {"items": items, "summary": {"recommended": ["T1", "T2", "T3"]}})
    res = jm.auto_pick_titles(tp, top_n=3)
    check("K1 scores 数组可自动选", len(res["picks"]) == 3
          and res["picks"][0]["total"] == sum(scores), res)
    bad = [{"id": f"T{i}", "text": f"标题{i}", "scores": [1, 2]} for i in range(1, 13)]
    tp_bad = write_t(fresh("scores_bad"), {"items": bad})
    ok, msg = raises(lambda: jm.auto_pick_titles(tp_bad), "缺少七维评分")
    check("K2 scores 非 7 元素且无 score → 阻断", ok, msg)

    # ── L. 三种评分形态同一 total 口径（数组 / scores dict / score dict）
    dims = [5, 4, 4, 5, 5, 3, 5]
    trad = dict(zip(("第一眼", "與我有關", "信息缺口", "情緒峰值",
                     "具體畫面", "新鮮程度", "內容兌現"), dims))
    plain = dict(zip(jm.SCORE_DIMS, dims))
    forms = {
        "scores 数组": {"id": "T1", "text": "x", "scores": list(dims)},
        "scores dict(繁体)": {"id": "T1", "text": "x", "scores": dict(trad)},
        "scores dict(简体)": {"id": "T1", "text": "x", "scores": dict(plain)},
        "score dict(旧别名)": {"id": "T1", "text": "x", "score": dict(plain)},
    }
    views = {k: jm._score_view(v) for k, v in forms.items()}
    check("L1 四种写法都被认作有效评分", all(v is not None for v in views.values()), views)
    totals = {k: jm._score_tuple(v)[0] for k, v in forms.items()}
    check("L2 四种写法的 total 完全一致", len(set(totals.values())) == 1, totals)
    check("L3 total 等于分项合计 31", set(totals.values()) == {31}, totals)
    check("L4 繁体键也能对上细分维度（内容兑现=5）",
          jm._score_items(forms["scores dict(繁体)"])[6] == 5,
          jm._score_items(forms["scores dict(繁体)"]))
    # 键名只作审计：换一批任意键名，只要 7 个 0—5 数字就照样读
    alien = {"a": 5, "b": 4, "c": 4, "d": 5, "e": 5, "f": 3, "g": 5}
    check("L5 未知键名的 scores dict 仍可读",
          jm._score_tuple({"scores": alien})[0] == 31,
          jm._score_tuple({"scores": alien}))
    check("L6 scores dict 值越界 → 判无评分",
          not jm._has_score({"scores": dict(plain, 第一眼=9)}))
    check("L7 scores dict 键数不足 → 判无评分",
          not jm._has_score({"scores": {"第一眼": 5}}))
    check("L8 bool 不算数字",
          not jm._has_score({"scores": {k: True for k in plain}}))

    # auto 兜底对 scores dict 也可用
    jd = fresh("scores_dict")
    items = [{"id": f"T{i}", "text": f"标题{i}", "scores": dict(trad)} for i in range(1, 13)]
    tp = write_t(jd, {"items": items, "summary": {"recommended": ["T1", "T2", "T3"]}})
    res = jm.auto_pick_titles(tp, top_n=3)
    check("L9 scores dict 可自动选（total=31）", len(res["picks"]) == 3
          and res["picks"][0]["total"] == 31, res["picks"][:1])

    # dict 形态只告警、不阻断
    jd = fresh("scores_dict_warn")
    legacy_items = [{"title_id": f"T{i}", "title": f"旧标题{i}", "scores": dict(trad)}
                    for i in range(1, 13)]
    tp = write_t(jd, {"items": legacy_items})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        job = jm.record_titles(jd, tp)
    out = buf.getvalue()
    check("L10 scores dict 放行（12 条）", job["titles"]["count"] == 12, job["titles"]["count"])
    check("L11 scores dict 告警且点明禁止写入",
          "scores 是 dict" in out and "禁止产出 dict" in out, out)
    jd = fresh("score_alias_warn")
    legacy_items = [{"id": f"T{i}", "text": f"标题{i}", "score": dict(plain)}
                    for i in range(1, 13)]
    tp = write_t(jd, {"items": legacy_items})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        jm.record_titles(jd, tp)
    check("L12 score 旧别名只告警", "旧别名 score" in buf.getvalue(), buf.getvalue())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print(f"❌ {len(fails)} 项失败: " + "; ".join(fails))
    sys.exit(1)
print("✅ 全部通过")
