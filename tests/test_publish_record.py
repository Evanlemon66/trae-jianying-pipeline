# -*- coding: utf-8 -*-
"""回归用例：job_manifest publish-record 手工登记发布回执。

覆盖：
  ① 正常登记：写 06_publish/<platform>.json + receipts/<platform>-<时间戳>.json，
     job.json 补齐 platforms/receipts，publish.status=published，stage 不强推
  ② 幂等：同平台同 video_id 重复登记不重复追加、revision 不变
  ③ 缺 url 与 video-id → ValueError / CLI 返回 1
  ④ 多平台并存（youtube + tiktok）→ 两条平台记录，stage=publish_ready 时推到 published
  ⑤ 同平台不同 video_id → 追加一条并 revision+1

运行：python tests\\test_publish_record.py（纯临时目录，不碰真实剧）
"""
import contextlib, io, json, os, shutil, sys, tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "lib"))
import job_manifest as jm  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


TMP = os.path.join(tempfile.gettempdir(), "publish_record_test")
shutil.rmtree(TMP, ignore_errors=True)


def make_job(name, stage="video_ready"):
    jd = os.path.join(TMP, name, "cover_job")
    for rel in jm.JOB_DIRS:
        os.makedirs(os.path.join(jd, *rel.split("/")), exist_ok=True)
    with open(os.path.join(jd, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write("测试简介")
    job = jm.build_job(name)
    job["stage"] = stage
    jm.save_job(job, jd)
    return jd


def read_job(jd):
    return jm.load_job(os.path.join(jd, "job.json"))


def receipts_of(jd):
    d = os.path.join(jd, "06_publish", "receipts")
    return sorted(f for f in os.listdir(d) if f.endswith(".json"))


print("\n== ① 正常登记 ==")
jd = make_job("pub1")
r = jm.publish_record(jd, "youtube", video_id="yt001",
                      url="https://youtu.be/yt001", note="手工上传")
check("非重复", r["duplicate"] is False, r)
check("返回两个文件", len(r["files"]) == 2, r["files"])
check("平台 json 存在", os.path.isfile(os.path.join(jd, "06_publish", "youtube.json")))
check("回执文件存在", os.path.isfile(os.path.join(jd, *r["files"][1].split("/"))))
with open(os.path.join(jd, "06_publish", "youtube.json"), encoding="utf-8") as f:
    payload = json.load(f)
check("平台 json status=published", payload.get("status") == "published", payload)
check("平台 json 记 video_id", payload.get("video_id") == "yt001", payload)
check("平台 json 有幂等键", bool(payload.get("idempotency_key")), payload)
job = read_job(jd)
check("publish.status=published", job["publish"]["status"] == "published", job["publish"])
check("platforms 1 条", len(job["publish"]["platforms"]) == 1, job["publish"]["platforms"])
check("平台状态在合法枚举内", job["publish"]["platforms"][0]["status"] == "succeeded",
      job["publish"]["platforms"][0])
check("receipts 1 条", len(job["publish"]["receipts"]) == 1, job["publish"]["receipts"])
check("stage 不强推（仍 video_ready）", job["stage"] == "video_ready", job["stage"])
check("history 记 publish_record", job["history"][-1]["event"] == "publish_record")
check("job.json 校验通过", jm.validate_job(job, jd) == [], jm.validate_job(job, jd))

print("\n== ② 幂等：同平台同 video_id 不重复追加 ==")
rev_before = job["revision"]
r2 = jm.publish_record(jd, "youtube", video_id="yt001")
check("判定重复", r2["duplicate"] is True, r2)
check("无新文件", r2["files"] == [], r2["files"])
job2 = read_job(jd)
check("revision 不变", job2["revision"] == rev_before, (rev_before, job2["revision"]))
check("platforms 仍 1 条", len(job2["publish"]["platforms"]) == 1)
check("回执文件仍 1 个", len(receipts_of(jd)) == 1, receipts_of(jd))

print("\n== ③ 缺 url 与 video-id → 报错 ==")
try:
    jm.publish_record(jd, "youtube")
    check("应抛 ValueError", False)
except ValueError as e:
    check("抛出 ValueError", True)
    check("错误提示含两者至少其一", "--video-id 与 --url 至少提供一个" in str(e), e)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = jm._main(["publish-record", "--job", jd, "--platform", "youtube"])
check("CLI 返回 1", rc == 1, rc)
check("CLI 打印 FAIL", "[FAIL]" in buf.getvalue(), buf.getvalue())

print("\n== ④ 多平台并存 + publish_ready 推 stage ==")
jd = make_job("pub2", stage="publish_ready")
jm.publish_record(jd, "youtube", video_id="y1")
jm.publish_record(jd, "tiktok", url="https://www.tiktok.com/@x/video/1")
job = read_job(jd)
check("platforms 2 条", len(job["publish"]["platforms"]) == 2, job["publish"]["platforms"])
check("receipts 2 条", len(job["publish"]["receipts"]) == 2)
check("两个平台 json 都在",
      os.path.isfile(os.path.join(jd, "06_publish", "youtube.json"))
      and os.path.isfile(os.path.join(jd, "06_publish", "tiktok.json")))
check("publish.status=published", job["publish"]["status"] == "published")
check("stage 推到 published", job["stage"] == "published", job["stage"])
check("两个回执文件", len(receipts_of(jd)) == 2, receipts_of(jd))

print("\n== ⑤ 同平台不同 video_id → 追加并 revision+1 ==")
jd = make_job("pub3")
jm.publish_record(jd, "youtube", video_id="a1")
rev1 = read_job(jd)["revision"]
jm.publish_record(jd, "youtube", video_id="a2")
job = read_job(jd)
check("platforms 2 条", len(job["publish"]["platforms"]) == 2, job["publish"]["platforms"])
check("revision +1", job["revision"] == rev1 + 1, (rev1, job["revision"]))
check("回执文件 2 个", len(receipts_of(jd)) == 2, receipts_of(jd))

print("\n" + ("全部通过" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
