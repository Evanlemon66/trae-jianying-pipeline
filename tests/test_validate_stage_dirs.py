# -*- coding: utf-8 -*-
"""回归用例：目录校验按阶段分层（A′：发布目录不阻断前序阶段）。

覆盖：
  ① covers_ready 缺 06_publish/receipts → 校验通过
  ② publish_ready / published 缺 06_publish/receipts → 校验失败，错误串仍是
     「缺少目录: 06_publish/receipts」
  ③ 其余 6 个基础目录缺任意一个 → 任意 stage 都失败

运行：python tests\\test_validate_stage_dirs.py（无第三方依赖，不真导出）
"""
import json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import job_manifest as jm  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "validate_stage_dirs_test")
shutil.rmtree(TMP, ignore_errors=True)


def make_fixture(name, *, stage, skip_dirs=()):
    jd = os.path.join(TMP, name, "cover_job")
    os.makedirs(jd, exist_ok=True)
    for rel in jm.BASE_DIRS + jm.PUBLISH_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(name)
    job["stage"] = stage
    jm.save_job(job, jd)
    # 先建齐再删，保证「简介文件」等其他校验项不干扰目录断言
    for rel in skip_dirs:
        shutil.rmtree(os.path.join(jd, *rel.split("/")), ignore_errors=True)
    return jd


def errors_of(jd):
    return jm.validate_job(jm.load_job(os.path.join(jd, "job.json")), jd)


print("\n== ① covers_ready 缺 receipts → 通过 ==")
jd = make_fixture("covers_ready", stage="covers_ready", skip_dirs=("06_publish/receipts",))
check("无 06_publish/receipts 目录", not os.path.isdir(os.path.join(jd, "06_publish", "receipts")))
check("校验通过", errors_of(jd) == [], errors_of(jd))

print("\n== ② 发布阶段缺 receipts → 失败且错误串不变 ==")
for stage in ("publish_ready", "published"):
    jd = make_fixture(stage, stage=stage, skip_dirs=("06_publish/receipts",))
    errs = errors_of(jd)
    check(f"{stage} 校验失败", errs != [], errs)
    check(f"{stage} 错误串", "缺少目录: 06_publish/receipts" in errs, errs)

print("\n== ③ 基础目录缺失 → 任意 stage 都失败 ==")
for rel in jm.BASE_DIRS:
    for stage in ("covers_ready", "published"):
        jd = make_fixture(f"miss_{rel}_{stage}".replace("/", "_"),
                         stage=stage, skip_dirs=(rel,))
        errs = errors_of(jd)
        check(f"{stage} 缺 {rel}", f"缺少目录: {rel}" in errs, errs)

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
