# -*- coding: utf-8 -*-
"""同一条剧分账号发布：复用已剪辑好的草稿，只换预设组，生成独立新草稿。

典型流程：
  A 账号：正常跑 pipeline，得到「剧名_第01-20集串流」
  B 账号：不改视频/字幕/翻译/人声分离，只把自己的预设层换上去

  py -3.12 pipeline/rebase_presets.py "剧名_第01-20集串流" --profile B
  py -3.12 pipeline/rebase_presets.py "剧名_第01-20集串流" --profile B --export

默认新草稿名 = 源草稿名 + "_" + 预设组名（如 剧名_第01-20集串流_B）；
导出落点按草稿名分目录，两条草稿互不覆盖。
"""
import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from preset_ops import clone_draft_with_presets  # noqa: E402


def _infer_job_dir(source):
    """从源草稿引用的视频路径反推 cover_job（找不到返回 None）。"""
    from pipeline import DRAFT_ROOT
    d = source if os.path.isdir(source) else os.path.join(DRAFT_ROOT, source)
    cp = os.path.join(d, "draft_content.json")
    if not os.path.isfile(cp):
        return None
    try:
        with open(cp, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    for m in (data.get("materials") or {}).get("videos") or []:
        p = m.get("path")
        if not (p and os.path.isabs(p)):
            continue
        cur = os.path.dirname(p)
        for _ in range(4):
            jd = os.path.join(cur, "cover_job")
            if os.path.isdir(jd):
                return jd
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return None


def _draft_duration_s(draft_name):
    """草稿时间线总时长（秒）：换预设重导时没有旧成品，导出进度靠它估总量。

    与 series_runner / batch_runner 同口径；读不到返回 None（导出照常，仅无百分比）。
    """
    from pipeline import DRAFT_ROOT
    path = os.path.join(DRAFT_ROOT, draft_name, "draft_content.json")
    try:
        with open(path, encoding="utf-8-sig") as f:
            draft = json.load(f)
        us = 0
        for track in draft.get("tracks") or []:
            for seg in track.get("segments") or []:
                tr = seg.get("target_timerange") or {}
                us = max(us, int(tr.get("start", 0)) + int(tr.get("duration", 0)))
        return round(us / 1e6, 3) if us > 0 else None
    except Exception:
        return None


def _export(draft_name):
    import export_config as pc
    exporter = os.path.join(_REPO, ".trae", "skills", "jianying-editor",
                            "examples", "robust_auto_export.py")
    if not os.path.isfile(exporter):
        print(f"⚠️ 找不到导出脚本: {exporter}")
        return False
    out = pc.export_mp4_path(draft_name)
    cmd = [sys.executable, exporter, draft_name, out,
           "--move", "--fallback-dir", pc.get_export_dir()]
    expect_s = _draft_duration_s(draft_name)
    if expect_s:
        cmd += ["--expect-duration", ("%g" % expect_s)]
    print(f"\n🎬 导出: {draft_name} → {out}"
          + (f"（草稿 {expect_s}s，预估约 {expect_s * 1.05:.0f} MB）" if expect_s else ""))
    return subprocess.run(cmd).returncode == 0


def main():
    ap = argparse.ArgumentParser(description="复用已剪辑草稿，替换预设组后生成独立新草稿")
    ap.add_argument("source", help="源草稿名或草稿目录（A 已剪辑好的草稿）")
    ap.add_argument("--profile", default=None, metavar="b",
                    help="预设组名（配置里写 presets_b；这里写 b 或 presets_b 均可）；"
                         "不传则用 default_preset_profile / presets 默认组")
    ap.add_argument("--name", default=None, help="新草稿名（默认 源草稿名_预设组名）")
    ap.add_argument("--overwrite", action="store_true", help="新草稿已存在时覆盖")
    ap.add_argument("--job-dir", default=None,
                    help="cover_job 目录（用于取该预设组的竖版封面）；不传则从草稿视频路径反推")
    ap.add_argument("--export", action="store_true", help="生成后立即导出")
    ap.add_argument("--cover-fallback", choices=("abort", "source"), default="abort",
                    help="预设组缺少竖版封面时：abort=抽帧兜底（抽不到则中止）；"
                         "source=直接复用源草稿封面")
    args = ap.parse_args()

    job_dir = args.job_dir or _infer_job_dir(args.source)
    if job_dir:
        print(f"🗂️ 版本封面来源: {job_dir}")
    else:
        print("⚠️ 未定位到 cover_job：扩展版本将改用源草稿主轨视频抽帧兜底（A 版沿用源封面）")

    try:
        r = clone_draft_with_presets(
            source=args.source,
            target_name=args.name,
            preset_profile=args.profile,
            overwrite=args.overwrite,
            job_dir=job_dir,
            cover_fallback=args.cover_fallback,
        )
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"\n✅ 新草稿已生成: {r['target']}")
    print(f"   源草稿 : {r['source']}")
    print(f"   预设组 : {r['profile'] or '(无)'} → {r['presets'] or '未合并任何预设'}")
    print(f"   封面   : {r['cover'] or '(无)'}（来源: {r.get('cover_source') or 'source_draft'}）")
    print(f"   剥离   : 轨道 {r['stripped']['tracks']} / 素材 {r['stripped']['materials']}")
    print(f"   时长   : {r['duration_us'] / 1e6:.2f}s")

    if args.export:
        ok = _export(r["target"])
        if ok:
            try:
                from pipeline import clear_export_stale
                clear_export_stale(r["target"])
            except Exception:
                pass
            # 写时确权：同步记一份成品出身（sidecar + E2），失败不影响结果
            try:
                import freshness
                from pipeline import export_mp4_path
                _jd = _infer_job_dir(r["target"])
                _out = export_mp4_path(r["target"])
                if _jd and os.path.isfile(_out):
                    freshness.record_export(
                        _jd, freshness.version_for_draft(_jd, r["target"]),
                        r["target"], _out)
            except Exception:
                pass
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
