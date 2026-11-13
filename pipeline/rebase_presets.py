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
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from preset_ops import clone_draft_with_presets  # noqa: E402


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
    print(f"\n🎬 导出: {draft_name} → {out}")
    return subprocess.run(cmd).returncode == 0


def main():
    ap = argparse.ArgumentParser(description="复用已剪辑草稿，替换预设组后生成独立新草稿")
    ap.add_argument("source", help="源草稿名或草稿目录（A 已剪辑好的草稿）")
    ap.add_argument("--profile", default=None, metavar="b",
                    help="预设组名（配置里写 presets_b；这里写 b 或 presets_b 均可）；"
                         "不传则用 default_preset_profile / presets 默认组")
    ap.add_argument("--name", default=None, help="新草稿名（默认 源草稿名_预设组名）")
    ap.add_argument("--overwrite", action="store_true", help="新草稿已存在时覆盖")
    ap.add_argument("--export", action="store_true", help="生成后立即导出")
    args = ap.parse_args()

    try:
        r = clone_draft_with_presets(
            source=args.source,
            target_name=args.name,
            preset_profile=args.profile,
            overwrite=args.overwrite,
        )
    except Exception as e:
        print(f"❌ {e}")
        sys.exit(1)

    print(f"\n✅ 新草稿已生成: {r['target']}")
    print(f"   源草稿 : {r['source']}")
    print(f"   预设组 : {r['profile'] or '(无)'} → {r['presets'] or '未合并任何预设'}")
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
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
