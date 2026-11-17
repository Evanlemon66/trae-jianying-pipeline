"""手工清理入口（--manual）：独立判定 + 独立执行，只读清单字段与磁盘事实。

设计约束（与默认路径严格隔离）：
- 判据零复用：不复用 stream_manifest.clean / _job_video / _audit_conflicts /
  outputs 台账 / 墓碑 / 回执；也不 import cover_watch / job_manifest /
  freshness / export_state。只复用中立工具：清单读写、目录大小、成品三件套
  校验（纯磁盘）；锁兜底在 manual_guard 里自己做（不改 export_lock.py）。
- 零 watcher 依赖：job.json、cover_job/**、.watcher.lock、.export_state.json
  一律不读。所有文件访问都过 `_guard()`，误读即 AssertionError（运行期防呆）。
- 版本来源：清单 paths.export_file（旧格式）+ 清单 clones[] + 磁盘发现
  「有成品包的同名克隆草稿」。没有证据的克隆不碰，绝不按命名瞎删。

CLI 由 stream_manifest.py 的 `--manual` 分流进入，默认路径不受影响。
"""
import json
import os
import re
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (os.path.join(_REPO, "lib"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from export_bundle import audit_bundle  # noqa: E402  # 纯磁盘三件套校验
import export_config  # noqa: E402  # 草稿根 / 导出根（中立配置）
import manual_guard as mg  # noqa: E402  # 手工侧公共守卫 + 锁兜底
import stream_manifest as sm  # noqa: E402  # 清单读写 / 目录大小（中立工具）

WatcherReadError = mg.WatcherReadError
_PROTECTED_FILES = mg.PROTECTED_FILES
_WATCHER_NAMES = mg.WATCHER_NAMES
_guard = mg.guard
_exists = mg.exists
_isdir = mg.isdir
_isfile = mg.isfile
_real = mg.real
_protected = mg.protected
_referenced_owners = mg.referenced_owners
_referencing_drafts = mg.referencing_drafts


def _drafts_root():
    try:
        return export_config.get_projects_root()
    except Exception:
        return ""


def _profile_of(name, base):
    """`<base>_<suffix>` → suffix；base 本身 → a；不符合版本命名 → None。"""
    if name == base:
        return "a"
    if name.startswith(base + "_"):
        tail = name[len(base) + 1:]
        if re.fullmatch(r"[a-z][a-z0-9]{0,7}", tail):
            return tail
    return None


def _group(manifests, target):
    """与 target 同 source_dir 的清单名集合（含自身）。"""
    data = manifests.get(target)
    if not data:
        return [target]
    src = ((data[1].get("paths") or {}).get("source_dir") or "")
    key = os.path.normcase(os.path.normpath(src)) if src else ""
    if not key:
        return [target]
    out = [target]
    for name, (_fp, d) in manifests.items():
        s = ((d.get("paths") or {}).get("source_dir") or "")
        if s and os.path.normcase(os.path.normpath(s)) == key and name not in out:
            out.append(name)
    return out


def _state_of(data, profile):
    v = (data.get("versions") or {}).get(profile) or {}
    return str(v.get("state") or "").lower()


def _export_root(index):
    for ent in index.values():
        ef = ent.get("export_file")
        if not ef:
            continue
        parent = os.path.dirname(os.path.abspath(ef))
        return os.path.dirname(parent) if os.path.basename(parent) == ent["draft"] else parent
    try:
        return export_config.get_export_dir()
    except Exception:
        return ""


def _index(manifests, group, base):
    """{profile: {draft, draft_dir, export_file, export_dir, state, explicit}}。"""
    idx = {}
    for name in group:
        data = manifests[name][1]
        paths = data.get("paths") or {}
        prof = _profile_of(name, base)
        if prof is None:
            prof = "a"
        idx[prof] = {
            "draft": name,
            "draft_dir": paths.get("draft_dir") or "",
            "export_file": paths.get("export_file") or "",
            "state": _state_of(data, prof),
        }
    # 清单里显式登记的克隆（未来格式）
    for name in group:
        for c in manifests[name][1].get("clones") or []:
            cn = str(c.get("draft") or "")
            prof = _profile_of(cn, base) if cn else None
            if not prof or prof in idx:
                continue
            idx[prof] = {"draft": cn, "draft_dir": c.get("draft_dir") or "",
                         "export_file": "", "state": ""}
    # 磁盘发现：只认「已经存在成品包」的克隆草稿，避免误判无关同名草稿
    root = _export_root(idx)
    drafts_root = _drafts_root()
    if root and drafts_root and os.path.isdir(drafts_root):
        for dn in sorted(os.listdir(drafts_root)):
            prof = _profile_of(dn, base)
            if not prof or prof in idx:
                continue
            ddir = os.path.join(drafts_root, dn)
            if not os.path.isdir(ddir):
                continue
            for cand in (os.path.join(root, dn, dn + ".mp4"),
                         os.path.join(root, dn + ".mp4")):
                if os.path.isfile(cand):
                    idx[prof] = {"draft": dn, "draft_dir": ddir,
                                 "export_file": cand, "state": ""}
                    break
    for ent in idx.values():
        ef = ent.get("export_file")
        if ef and os.path.basename(os.path.dirname(os.path.abspath(ef))) == ent["draft"]:
            ent["export_dir"] = os.path.dirname(os.path.abspath(ef))
        else:
            ent["export_dir"] = ""
    return idx


def _missing(ent):
    try:
        _guard(ent.get("export_file"))
        _guard(ent.get("export_dir"))
        res = audit_bundle(ent["draft"], ent.get("export_file") or None,
                           ent.get("export_dir") or None)
    except WatcherReadError:
        raise
    except Exception as e:
        return [f"校验失败: {e}"]
    return [] if res["ok"] else list(res["missing"])


def _has_mp4(d):
    """目录（含子目录）里还有 mp4 就不算空壳——可能是尚未完成的成品，不自动删。"""
    for _root, _dirs, files in os.walk(d):
        if any(f.lower().endswith(".mp4") for f in files):
            return True
    return False


def _known_drafts(index, manifests, base):
    """本次清理涉及的所有版本草稿名：清单/索引 + 磁盘上与 base 同名的克隆。"""
    known = {ent["draft"] for ent in index.values() if ent.get("draft")}
    root = _drafts_root()
    if root and os.path.isdir(root):
        for dn in os.listdir(root):
            if _profile_of(dn, base) and os.path.isdir(os.path.join(root, dn)):
                known.add(dn)
    return known


def _orphan_export_residue(index, manifests, group, profile=None):
    """导出中途被杀留下的残留：清单里没有成品记录，磁盘却留着空目录/同级心跳。

    `_index` 的磁盘发现只认「已有 mp4」的克隆，所以这类版本永远不会进计划：
    `<导出根>/<草稿>.progress.json`（status=running 的心跳）和空的
    `<导出根>/<草稿>/`、`<草稿>(n)/` 就成了清不掉的遗漏。
    这里按「草稿同名」这一条硬证据补齐，含 mp4 的目录一律不碰。
    """
    root = _export_root(index)
    if not root or not os.path.isdir(root) or not index:
        return []
    base = manifests.get(group[0], (None, {}))[1].get("draft_name") or group[0]
    out = []
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    for dn in sorted(_known_drafts(index, manifests, base)):
        prof = _profile_of(dn, base) or "a"
        if profile and prof != profile:
            continue
        for label, p in ((f"{dn}.progress.json", os.path.join(root, dn + ".progress.json")),
                         (f"{dn}.mp4.progress.json",
                          os.path.join(root, dn + ".mp4.progress.json"))):
            if os.path.isfile(p):
                out.append(("delete", "progress_side", "export_file", p,
                            "导出中断残留的心跳", prof))
        bundle = os.path.join(root, dn)
        if os.path.isdir(bundle) and not _has_mp4(bundle):
            out.append(("delete", "export_bundle", "export_bundle", bundle,
                        "导出中断留下的空目录", prof))
        dedup = re.compile(re.escape(dn) + r"\(\d+\)$")
        for name in entries:
            p = os.path.join(root, name)
            if dedup.match(name) and os.path.isdir(p) and not _has_mp4(p):
                out.append(("delete", "export_bundle", "export_bundle", p,
                            "导出中断留下的空壳去重目录", prof))
    return out


def build_plan(index, group, manifests, *, include_drafts=False, profile=None,
               intermediates_only=False):
    """纯判定：只读清单字段 + 磁盘事实，返回 (items, conflicts, errors)。

    items: {action, label, kind, path, reason, version, size}
    action ∈ delete / keep / protected / warn / error
    """
    items, conflicts, errors = [], [], []
    group = list(dict.fromkeys(group))
    source_dir = ""
    for name in group:
        source_dir = source_dir or ((manifests[name][1].get("paths") or {})
                                    .get("source_dir") or "")

    def add(action, label, kind, path, reason="", version=None):
        items.append({"action": action, "label": label, "kind": kind, "path": path,
                      "reason": reason, "version": version, "size": 0})

    if not profile:
        seen = set()
        for name in group:
            paths = manifests[name][1].get("paths") or {}
            for kind in ("jycache_dir", "workdir"):
                p = paths.get(kind)
                if not p or _real(p) in seen:
                    continue
                seen.add(_real(p))
                if not _exists(p):
                    continue
                why = _protected(p, source_dir)
                if why:
                    add("protected", kind, kind, p, why)
                    continue
                owners = _referenced_owners(_drafts_root())
                if owners is None:
                    add("keep", kind, kind, p, "无法确认草稿引用关系，保守保留")
                    continue
                refs = _referencing_drafts(p, owners)
                if refs:
                    add("keep", kind, kind, p,
                        "仍被草稿引用: " + "、".join(refs[:3])
                        + ("…" if len(refs) > 3 else ""))
                    continue
                add("delete", kind, kind, p)

    if not intermediates_only:
        targets = [profile] if profile else sorted(index)
        for prof in targets:
            ent = index[prof]
            draft = ent["draft"]
            ef, ed = ent.get("export_file") or "", ent.get("export_dir") or ""
            if not ef and not ed:
                if ent.get("state") == "exported":
                    add("error", draft, "export_bundle", "",
                        "清单显示已导出但没有任何成品路径（export_file/export_dir 均缺）",
                        prof)
                    errors.append(draft)
                elif ent.get("state") != "cleaned":
                    add("warn", draft, "export_bundle", "",
                        "找不到成品路径，本次只清中间产物", prof)
                continue
            if ent.get("state") != "cleaned":
                missing = _missing(ent)
                if missing:
                    conflicts.append((draft, prof, missing))
            parent = os.path.dirname(os.path.abspath(ef)) if ef else ""
            bundle = ""
            if ed and os.path.basename(os.path.abspath(ed)) == draft:
                bundle = os.path.abspath(ed)
            elif ef and os.path.basename(parent) == draft:
                bundle = parent
            if bundle:
                if not _isdir(bundle):
                    continue
                why = _protected(bundle, source_dir)
                if why:
                    add("protected", "export_bundle", "export_bundle", bundle, why)
                    continue
                add("delete", "export_bundle", "export_bundle", bundle, "", prof)
                side = os.path.join(os.path.dirname(bundle), draft + ".progress.json")
                if _exists(side):
                    add("delete", "progress_side", "export_file", side, "", prof)
            else:
                stem = os.path.splitext(os.path.abspath(ef))[0]
                for label, kind, p in (
                        ("export_file", "export_file", os.path.abspath(ef)),
                        (f"{os.path.basename(stem)}.progress.json", "export_file",
                         stem + ".mp4.progress.json"),
                        (f"{os.path.basename(stem)}-封面.jpg", "export_file",
                         stem + "-封面.jpg")):
                    if not _exists(p):
                        continue
                    why = _protected(p, source_dir)
                    if why:
                        add("protected", label, kind, p, why)
                        continue
                    add("delete", label, kind, p, "", prof)

        # 清单里没有成品的版本（导出中途被杀）：补齐空目录 / 同级心跳的清理
        planned = {_real(it["path"]) for it in items if it.get("path")}
        for t in _orphan_export_residue(index, manifests, group, profile):
            if _real(t[3]) in planned:
                continue
            add(*t)

    if include_drafts and not profile and not intermediates_only:
        seen = set()
        for prof in sorted(index):
            p = index[prof].get("draft_dir") or ""
            if not p or _real(p) in seen:
                continue
            seen.add(_real(p))
            if not _exists(p):
                continue
            why = _protected(p, source_dir)
            if why:
                add("protected", "draft_dir", "draft_dir", p, why)
                continue
            add("delete", "draft_dir", "draft_dir", p)

    return items, conflicts, errors


def _mark_cleaned(ent, profile):
    """把该版本清单标 cleaned（幂等重跑不再判缺件）；只写清单，不读 job。"""
    name = ent["draft"]
    mp = sm._manifest_path(name)
    try:
        with open(mp, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    ver = data.setdefault("versions", {}).setdefault(profile, {})
    ver.setdefault("draft", name)
    if ent.get("export_file"):
        ver.setdefault("export_file", ent["export_file"])
    if ent.get("export_dir"):
        ver.setdefault("export_dir", ent["export_dir"])
    ver["state"] = "cleaned"
    try:
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"  [warn] 回写清单 state=cleaned 失败: {e}")


def run(draft_name, dry_run=False, include_drafts=False, require_receipts=False,
        intermediates_only=False, profile=None, force=False, manifest_dir=None):
    """手工清理单部剧；返回 0/1/2（与默认 clean 的 rc 语义一致）。"""
    if manifest_dir:
        sm.MANIFEST_DIR = os.path.abspath(manifest_dir)
    if require_receipts:
        print("  [错误] --require-receipts 依赖 cover_job 回执，手工模式不支持")
        return 1
    profile = str(profile or "").strip().lower()
    if profile and intermediates_only:
        print("  [错误] --intermediates-only 不能与 --profile 同用")
        return 1
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
    title = manifests[draft_name][1].get("draft_name", draft_name)
    print(f"清理 [{title}]（手工判定层，profile={profile or '全部'}）:")
    print("  [模式] 只读清单 + 磁盘事实；不读 job.json / cover_job / 锁状态文件")
    if intermediates_only:
        print("  [模式] intermediates-only：只清 __jycache__ / _work_*")
    elif profile:
        print(f"  [模式] 单版本清理：共享中间产物保留（共 {len(index)} 个版本）")

    items, conflicts, errors = build_plan(
        index, group, manifests, include_drafts=include_drafts, profile=profile,
        intermediates_only=intermediates_only)

    total = 0
    ready = []
    seen = set()
    for it in items:
        act, label, p, reason = it["action"], it["label"], it["path"], it["reason"]
        if p:
            rp = _real(p)
            if rp in seen:
                continue
            seen.add(rp)
        if act == "error":
            print(f"  [错误] {label}: {reason}")
            continue
        if act == "protected":
            print(f"  [受保护] {label}: {p}（{reason}）")
            continue
        if act == "warn":
            print(f"  [warn] {label}: {reason}")
            continue
        if act == "keep":
            print(f"  [保留] {label}: {p}（{reason}）")
            continue
        it["size"] = sm._entry_size(it["kind"], p)
        total += it["size"]
        ready.append(it)
        print(f"  [{'计划' if dry_run else '待删'}] {label}: {p} ({sm._fmt(it['size'])})")
    print(f"  合计: {sm._fmt(total)}")
    if errors:
        print("  [中止] 清单存在无法定位的成品，未执行任何删除")
        return 2
    if conflicts:
        if force:
            print("  [提示] --force：跳过成品三件套校验（手工层不写强制留痕）")
        elif dry_run:
            print("  [告警] 成品产物校验未通过（dry-run 仅提示）：")
            for n, _p, missing in conflicts:
                for m in missing:
                    print(f"    - {n}: {m}")
        else:
            print("  [冲突] 成品产物校验未通过（mp4 / progress.done / 封面），已中止:")
            for n, _p, missing in conflicts:
                for m in missing:
                    print(f"    - {n}: {m}")
            print("  补齐缺件后重跑，或加 --force")
            return 1
    if dry_run:
        print("  （dry-run：未执行任何删除，记录未改动）")
        if source_dir:
            print(f"  （源视频目录保留不动: {source_dir}）")
        return 0

    ok, lock_note = mg.acquire_export_lock(f"manual_clean {draft_name}")
    if not ok:
        print(f"  ⛔ {lock_note}")
        return 1
    if lock_note:
        print(f"  [锁] {lock_note}")
    try:
        deleted = []
        for it in ready:
            label, p = it["label"], it["path"]
            if label == "progress_side":
                print(f"  [心跳] {p}（残留，随成品目录一并清理）")
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"  [fail] {label}: {e}")
                    continue
            if not os.path.exists(p):
                print(f"  [已删] {label}: {p} ({sm._fmt(it['size'])})")
                deleted.append(it)
            else:
                print(f"  [失败] {label}: {p}")
        marks = [it for it in deleted
                 if it["kind"] == "export_bundle"
                 or (it["kind"] == "export_file" and it["label"] == "export_file")]
        for prof in sorted({it["version"] for it in marks if it["version"]}):
            _mark_cleaned(index[prof], prof)
    finally:
        mg.release_export_lock()
    if source_dir:
        print(f"  （源视频目录保留不动: {source_dir}）")
    return 0


def main(opt):
    """CLI 分流入口：opt 来自 stream_manifest._parse_clean_args。"""
    if opt.get("manifest_dir"):
        sm.MANIFEST_DIR = os.path.abspath(opt["manifest_dir"])
    if not opt.get("all") and not opt.get("job"):
        print(sm.__doc__)
        return 1
    if opt.get("all") and opt.get("profile"):
        print("[错误] --all 与 --profile 不能同时使用")
        return 1
    names = sm._manifest_names() if opt.get("all") else [opt["job"]]
    if not names:
        print("（无任何记录）")
        return 0
    skipped = []
    for n in names:
        rc = run(n, dry_run=opt.get("dry_run", False),
                 include_drafts=opt.get("include_drafts", False),
                 require_receipts=opt.get("require_receipts", False),
                 intermediates_only=opt.get("intermediates_only", False),
                 profile=opt.get("profile"), force=opt.get("force", False))
        if rc != 0:
            skipped.append(n)
        print()
    if skipped:
        print("跳过（严格模式/无记录/产物缺件）:")
        for n in skipped:
            print(f"  - {n}")
        return 1
    return 0
