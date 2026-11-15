"""手工归档入口（archive --manual）：只搬成品 bundle，源剧集目录原地不动。

与默认路径（pipeline/archive_job.py）的差异：
- 默认实现把**整个剧集目录** `<root>/<剧名>` 搬进归档池；手工这条只搬成品
  bundle，源视频/源目录留在原地，watcher 完全感知不到目录位置变化。
- 判据只有清单字段 + 磁盘事实：成品完整性 = 三件套（mp4/progress.done/封面）
  + 清单 `versions.<p>.state`。不读 .watcher.lock / .export_state.json /
  job.json / freshness。
- job 回写退为可选镜像（`mirror` 回调由 CLI 层在 `--mirror-job` 时注入），
  没有 job 或写失败只告警，不影响返回码。默认一次 job 侧文件都不碰。
- 目标已存在且状态一致 → skip（幂等）；不一致 → 报冲突，需人工处理。

CLI 由 stream_manifest.py 的 `archive --manual` 分流进入。
"""
import json
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (os.path.join(_REPO, "lib"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from export_bundle import audit_bundle  # noqa: E402  # 纯磁盘三件套校验
import export_config  # noqa: E402  # 归档根 / 草稿根（中立配置）
import manual_guard as mg  # noqa: E402  # 手工侧公共守卫 + 锁兜底
import stream_manifest as sm  # noqa: E402  # 清单读写 / 目录大小（中立工具）
from manual_clean import _group, _index  # noqa: E402  # 手工侧内部复用，与默认路径无关


def _drafts_root():
    try:
        return export_config.get_projects_root()
    except Exception:
        return ""


def _bundle_of(ent):
    """成品包目录（目录名 == 草稿名）；平铺布局返回空串。"""
    draft = ent["draft"]
    ef, ed = ent.get("export_file") or "", ent.get("export_dir") or ""
    if ed and os.path.basename(os.path.abspath(ed)) == draft:
        return os.path.abspath(ed)
    if ef:
        parent = os.path.dirname(os.path.abspath(ef))
        if os.path.basename(parent) == draft:
            return parent
    return ""


def _flat_files(ent):
    """平铺布局的成品与伴随文件（存在的才列）。"""
    ef = ent.get("export_file") or ""
    if not ef:
        return []
    ef = os.path.abspath(ef)
    stem = os.path.splitext(ef)[0]
    out = []
    for p in (ef, ef + ".progress.json", stem + "-封面.jpg"):
        if mg.isfile(p):
            out.append(p)
    return out


def _mark(ent, profile, *, new_file, new_dir, dry_run, manifest_name=None):
    """清单回写 versions.<p>.state=archived + 新路径；dry-run 不写。

    磁盘发现的克隆版可能没有自己的清单，此时把版本状态记到基础版清单里，
    保证「一部剧一份台账」——verify 才能按 versions 对账。
    """
    if dry_run:
        return
    name = ent["draft"]
    mp = sm._manifest_path(name)
    if not os.path.exists(mp):
        name = manifest_name or name
        mp = sm._manifest_path(name)
    try:
        with open(mp, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    old_file = ent.get("export_file") or ""
    old_dir = ent.get("export_dir") or ""

    def _same(a, b):
        return a and b and a.replace("\\", "/").lower() == b.replace("\\", "/").lower()

    ver = data.setdefault("versions", {}).setdefault(profile, {})
    ver.setdefault("draft", name)
    ver["state"] = "archived"
    if new_file:
        ver["export_file"] = new_file.replace(os.sep, "/")
    if new_dir:
        ver["export_dir"] = new_dir.replace(os.sep, "/")
        ver["archived_to"] = new_dir.replace(os.sep, "/")
    paths = data.setdefault("paths", {})
    if new_file and _same(paths.get("export_file"), old_file):
        paths["export_file"] = ver["export_file"]
    if new_dir and _same(paths.get("export_dir"), old_dir):
        paths["export_dir"] = ver["export_dir"]
    try:
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [warn] 回写清单 state=archived 失败: {e}")


def _mirror(mirror, draft, profile, target, source_dir):
    """可选 job 镜像：只在显式注入时执行，失败只告警。"""
    if not mirror or not source_dir:
        return
    try:
        mirror(os.path.join(source_dir, "cover_job"),
               {"video": {"outputs": {profile: {"draft": draft,
                                                "archived_to": target}}}},
               event="archived", detail=f"成品已归档到 {target}")
        print(f"  [镜像] job.json outputs.{profile}.archived_to 已记录（可选镜像）")
    except Exception as e:
        print(f"  [warn] job 镜像回写失败（不影响返回码）: {e}")


def archive(draft_name, archive_root=None, dry_run=False, profile=None, force=False,
            mirror=None, manifest_dir=None):
    """归档一部剧的成品；返回 0=完成/幂等跳过；1=有版本被跳过/阻断；2=路径错误。"""
    if manifest_dir:
        sm.MANIFEST_DIR = os.path.abspath(manifest_dir)
    profile = str(profile or "").strip().lower()
    manifests = sm._all_manifests()
    if draft_name not in manifests:
        print(f"无记录: {draft_name}")
        return 1
    group = _group(manifests, draft_name)
    base = min(group, key=len) or draft_name
    index = _index(manifests, group, base)
    if profile and profile not in index:
        print(f"  [错误] 该剧没有 profile={profile} 的版本；现有: "
              + "、".join(f"{p}={index[p]['draft']}" for p in sorted(index)))
        return 1

    source_dir = ""
    for n in group:
        source_dir = source_dir or ((manifests[n][1].get("paths") or {})
                                    .get("source_dir") or "")
    root = archive_root or (export_config.get_archive_root() or "")
    if not root:
        print("  [错误] 未配置归档根（archive_root），无法归档")
        return 2
    root = os.path.abspath(root)
    if source_dir:
        src = os.path.abspath(source_dir)
        if mg.real(root) == mg.real(src) or mg.real(root).startswith(mg.real(src) + os.sep):
            print(f"  [错误] 归档根不能等于或位于源剧集目录内: {root}")
            return 2
    print(f"清理归档 [{manifests[draft_name][1].get('draft_name', draft_name)}]"
          f"（手工判定层，profile={profile or '全部'}）:")
    print("  [模式] 只搬成品 bundle；源剧集目录原地不动；不读 job/锁/export_state")

    targets = [profile] if profile else sorted(index)
    ready, blocked, errors = [], [], []
    for prof in targets:
        ent = index[prof]
        draft = ent["draft"]
        bundle = _bundle_of(ent)
        flat = [] if bundle else _flat_files(ent)
        if not bundle and not flat:
            if ent.get("state") in ("exported", "archived"):
                print(f"  [错误] {draft}: 清单 state={ent['state']} 但成品路径缺失")
                errors.append(draft)
            else:
                print(f"  [skip] {draft}: 无成品路径（可能已清理），跳过")
            continue
        src_path = bundle or flat[0]
        if not mg.exists(src_path):
            if ent.get("state") in ("exported", "archived"):
                print(f"  [错误] {draft}: state={ent['state']} 但成品不在盘上: {src_path}")
                errors.append(draft)
            else:
                print(f"  [skip] {draft}: 成品不存在，跳过")
            continue
        why = mg.protected(src_path, source_dir)
        if why:
            print(f"  [受保护] {draft}: {src_path}（{why}）")
            blocked.append(draft)
            continue
        target = os.path.join(root, draft)
        if os.path.exists(target):
            if ent.get("state") == "archived":
                print(f"  [skip] {draft}: 已归档（state=archived，目标存在）")
                continue
            probe = audit_bundle(draft, os.path.join(target, draft + ".mp4"), target)
            if probe["ok"]:
                print(f"  [skip] {draft}: 目标已存在且三件套完整，按已归档处理（幂等）")
                _mark(ent, prof, new_file=os.path.join(target, draft + ".mp4"),
                      new_dir=target, dry_run=dry_run, manifest_name=base)
                continue
            print(f"  [冲突] {draft}: 归档目标已存在且状态不一致: {target}")
            blocked.append(draft)
            continue
        probe = audit_bundle(draft, ent.get("export_file") or None, bundle or None)
        if not probe["ok"]:
            if not force:
                print(f"  [冲突] {draft}: 成品三件套不完整，已跳过:")
                for m in probe["missing"]:
                    print(f"    - {m}")
                blocked.append(draft)
                continue
            print(f"  [提示] {draft}: --force 跳过三件套校验")
        ready.append({"profile": prof, "draft": draft, "bundle": bundle,
                      "files": flat, "target": target,
                      "size": sm._entry_size("export_bundle", bundle) if bundle else
                      sum(sm._entry_size("export_file", p) for p in flat)})

    total = sum(it["size"] for it in ready)
    for it in ready:
        print(f"  [{'计划' if dry_run else '待搬'}] {it['draft']}: "
              f"{it['bundle'] or '、'.join(it['files'])} -> {it['target']} "
              f"({sm._fmt(it['size'])})")
    print(f"  合计: {sm._fmt(total)}")
    if errors:
        print("  [中止] 清单与磁盘不一致，未执行任何移动")
        return 2
    if dry_run:
        print("  （dry-run：未移动任何文件，记录未改动）")
        print(f"  （源剧集目录原地不动: {source_dir or '-'}）")
        return 0 if not blocked else 1
    if not ready:
        print("  （无可搬运的成品）")
        if blocked:
            return 1
        return 0

    ok, lock_note = mg.acquire_export_lock(f"manual_archive {draft_name}")
    if not ok:
        print(f"  ⛔ {lock_note}")
        return 1
    if lock_note:
        print(f"  [锁] {lock_note}")
    try:
        rc = 0
        for it in ready:
            target = it["target"]
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if it["bundle"]:
                    if os.path.exists(target):
                        raise OSError("目标已存在")
                    shutil.move(it["bundle"], target)
                    new_file = os.path.join(target, it["draft"] + ".mp4")
                else:
                    os.makedirs(target, exist_ok=True)
                    for p in it["files"]:
                        shutil.move(p, os.path.join(target, os.path.basename(p)))
                    new_file = os.path.join(target, os.path.basename(it["files"][0]))
            except OSError as e:
                print(f"  [失败] {it['draft']}: {e}")
                rc = 2
                continue
            print(f"  [已搬] {it['draft']}: {target}")
            _mark(index[it["profile"]], it["profile"], new_file=new_file,
                  new_dir=target, dry_run=False, manifest_name=base)
            _mirror(mirror, it["draft"], it["profile"], target, source_dir)
        if blocked:
            rc = max(rc, 1)
    finally:
        mg.release_export_lock()
    print(f"  （源剧集目录原地不动: {source_dir or '-'}）")
    return rc


def main(opt, mirror=None):
    """CLI 分流入口：opt 来自 stream_manifest._parse_manual_args。"""
    if opt.get("manifest_dir"):
        sm.MANIFEST_DIR = os.path.abspath(opt["manifest_dir"])
    if not opt.get("all") and not opt.get("job"):
        print("[用法] clean/archive/verify --manual --job \"<草稿名>\" [--dry-run]")
        return 1
    if opt.get("all") and opt.get("profile"):
        print("[错误] --all 与 --profile 不能同时使用")
        return 1
    names = sm._manifest_names() if opt.get("all") else [opt["job"]]
    if not names:
        print("（无任何记录）")
        return 0
    rc_all = 0
    for n in names:
        rc = archive(n, archive_root=opt.get("archive_root"),
                     dry_run=opt.get("dry_run", False), profile=opt.get("profile"),
                     force=opt.get("force", False), mirror=mirror)
        rc_all = max(rc_all, rc)
        print()
    return rc_all
