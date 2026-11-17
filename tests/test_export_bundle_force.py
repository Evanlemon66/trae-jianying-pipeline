# -*- coding: utf-8 -*-
"""回归用例：豁免机制下线 + --force 显式放行留痕。

覆盖：
  A 三件套缺件默认拦住（audit_bundle 严格判定）
  B 旧 audit_exemption.json 不再被读取（机制真下线，不是换个入口）
  C --force 留痕：06_publish/audit_forced.log 字段完整（谁/何时/放过什么/命令）
  D archive_job 强制归档前仍先算出被放过的缺件
  E stream_manifest clean 的冲突收集（版本维度）与 cleaned 跳过

运行：python tests\\test_export_bundle_force.py（纯临时目录）
"""
import json
import os
import shutil
import struct
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
sys.path.insert(0, os.path.join(_ROOT, "pipeline"))

import export_bundle as eb  # noqa: E402
import archive_job as aj  # noqa: E402
import stream_manifest as sm  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "export_bundle_force_test")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

PNG_HEAD = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", 1080, 1920) + b"\x08\x06\x00\x00\x00")


def make_bundle(out_dir, stem, *, mp4=True, cover=True, progress=True, done=True):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, stem + ".mp4")
    if mp4:
        with open(p, "wb") as f:
            f.write(b"m" * 4096)
    if cover:
        with open(os.path.join(out_dir, stem + "-封面.jpg"), "wb") as f:
            f.write(PNG_HEAD)
    if progress:
        with open(p + ".progress.json", "w", encoding="utf-8") as f:
            json.dump({"done": done}, f)
    return p


print("\n== A 缺件默认拦住 ==")
out_a = os.path.join(TMP, "out_a")
mp4_a = os.path.join(out_a, "剧A.mp4")
res = eb.audit_bundle("剧A", mp4_a, out_a)
check("A1 三件全缺 → ok=False", res["ok"] is False, res)
check("A2 三项都报出来", len(res["missing"]) == 3, res["missing"])
mp4_a2 = make_bundle(os.path.join(TMP, "out_a2"), "剧A2", cover=False)
res2 = eb.audit_bundle("剧A2", mp4_a2, os.path.dirname(mp4_a2))
check("A3 只缺封面仍拦住", res2["ok"] is False and "封面" in res2["missing"][0], res2["missing"])
mp4_a3 = make_bundle(os.path.join(TMP, "out_a3"), "剧A3")
res3 = eb.audit_bundle("剧A3", mp4_a3, os.path.dirname(mp4_a3))
check("A4 三件齐 → ok=True", res3["ok"] is True, res3["missing"])

print("\n== B 旧豁免文件不再读取 ==")
jd_b = os.path.join(TMP, "job_b")
os.makedirs(os.path.join(jd_b, "06_publish"), exist_ok=True)
with open(os.path.join(jd_b, "audit_exemption.json"), "w", encoding="utf-8") as f:
    json.dump({"exemptions": [{"draft": "剧B", "profile": "a", "reason": "网络卡",
                               "who": "tester", "at": "2026-11-14", "scope": "manual_exemption"}]}, f)
mp4_b = os.path.join(jd_b, "06_publish", "剧B.mp4")
resb = eb.audit_bundle("剧B", mp4_b, os.path.dirname(mp4_b))
check("B1 有豁免文件也照样拦住", resb["ok"] is False, resb)
check("B2 豁免读取入口已删除",
      not hasattr(eb, "find_exemption") and not hasattr(eb, "log_exemption_use")
      and not hasattr(eb, "EXEMPTION_FILE"))

print("\n== C --force 留痕 ==")
jd_c = os.path.join(TMP, "job_c")
os.makedirs(jd_c, exist_ok=True)
eb.log_forced_use(jd_c, "archive", [("剧C_串流", "a", ["封面: /x/剧C-封面.jpg"])])
log_c = os.path.join(jd_c, "06_publish", "audit_forced.log")
text = open(log_c, encoding="utf-8").read() if os.path.isfile(log_c) else ""
check("C1 日志已落盘", bool(text), text)
for field in ("forced", "op=archive", "user=", "draft=剧C_串流", "profile=a",
              "missing=封面: /x/剧C-封面.jpg", "cmd="):
    check(f"C2 字段 {field}", field in text, text)
os.remove(log_c)
eb.log_forced_use(jd_c, "clean", [("剧C_串流", "a", ["progress"])], dry_run=True)
check("C3 dry-run 不落盘", not os.path.exists(log_c))
eb.log_forced_use(jd_c, "clean", [("剧C_串流", "b", ["封面"])])
check("C4 clean 单独留痕",
      "op=clean" in open(log_c, encoding="utf-8").read())

print("\n== D 归档：force 前先算缺件 ==")
job_d = {"video": {"outputs": {
    "a": {"state": "exported", "draft": "剧D", "export_file": os.path.join(TMP, "nope", "剧D.mp4")},
    "b": {"state": "exported", "draft": "剧D_b", "export_file": make_bundle(
        os.path.join(TMP, "out_d"), "剧D_b")},
}}}
conf_d = aj._audit_outputs(job_d, os.path.join(TMP, "job_d"))
check("D1 只报缺件版本", [p for p, _ in conf_d] == ["a"], conf_d)
check("D2 缺件内容可读", conf_d and "mp4" in conf_d[0][1][0], conf_d)
jd_d = os.path.join(TMP, "job_d")
os.makedirs(jd_d, exist_ok=True)
eb.log_forced_use(jd_d, "archive", [("剧D", p, ms) for p, ms in conf_d])
check("D3 force 归档留痕", os.path.isfile(os.path.join(jd_d, eb.FORCED_LOG)))

print("\n== E clean：冲突收集（版本维度）==")
work_e = [
    ("剧E_串流", "a", {"export_file": os.path.join(TMP, "nope", "剧E.mp4")}),
    ("剧E_串流_b", "b", {"export_file": make_bundle(os.path.join(TMP, "out_e"), "剧E_串流_b")}),
    ("剧E_串流", "c", {"export_file": os.path.join(TMP, "nope", "剧E_c.mp4")}),
]
conf_e = sm._audit_conflicts(work_e, {"c": {"state": "cleaned"}}, {})
check("E1 带版本号返回", [(n, p) for n, p, _ in conf_e] == [("剧E_串流", "a")], conf_e)
check("E2 cleaned 版本跳过", all(p != "c" for _n, p, _ in conf_e), conf_e)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
