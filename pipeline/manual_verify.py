"""手工对账入口（verify --manual）：清单 ↔ 磁盘逐条对账 + 残留扫描。

判据只有清单字段 + 磁盘事实（不读 job.json / .watcher.lock / .export_state.json /
freshness）。按 scope 分三档，各自独立计数与返回码，互不稀释：
  - drama（默认）：本剧（或 --all 全部剧）的清单对账，rc 只看本剧；
  - staging：全局固定 staging 区（草稿根同级的 `__jybuild__`）残留扫描，
    单独给残留数量与总大小，rc 只看全局；
  - both：两段各出结论，合并 rc 取两者较大值。

本剧对账检查项：
  1. 清单里每个路径的存在性（源目录 / 草稿 / __jycache__ / workdir）；
  2. 每个版本的 `versions.<p>.state` 与磁盘是否一致（exported/archived/cleaned）；
  3. 成品三件套（mp4 非空 / progress.done / 竖版封面）；
  4. 中间产物是否仍被任一草稿引用（没人引用还在 = 可清未清的残留）。

任一 scope 有残留 → 该段 rc=1，并逐条打印路径与原因。
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
for _p in (os.path.join(_REPO, "lib"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from export_bundle import audit_bundle  # noqa: E402  # 纯磁盘三件套校验
import export_config  # noqa: E402  # 草稿根（中立配置）
import manual_guard as mg  # noqa: E402  # 手工侧公共守卫
import stream_manifest as sm  # noqa: E402  # 清单读写（中立工具）

SCOPES = ("drama", "staging", "both")


def _drafts_root():
    try:
        return export_config.get_projects_root()
    except Exception:
        return ""


def _staging_default(drafts_root):
    if not drafts_root:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(drafts_root)), "__jybuild__")


def _human_size(n):
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{int(val)} B" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024


def _tree_size(root):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total


def _entry_size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    return _tree_size(path)


def _three_piece(draft, ef, ed):
    res = audit_bundle(draft, ef or None, ed or None)
    return [] if res["ok"] else list(res["missing"])


def check_one(draft_name, data, *, drafts_root, owners):
    """对账单条清单记录。返回 (notes, residuals)。"""
    notes, residuals = [], []
    paths = data.get("paths") or {}
    src = paths.get("source_dir") or ""
    if src and not mg.exists(src):
        notes.append(("source_dir", src, "清单记录了但磁盘不存在"))
    for key in ("draft_dir", "jycache_dir", "workdir"):
        p = paths.get(key) or ""
        if p and not mg.exists(p):
            notes.append((key, p, "清单记录了但磁盘不存在"))

    for key in ("jycache_dir", "workdir"):
        p = paths.get(key) or ""
        if not p or not mg.exists(p):
            continue
        if owners is None:
            notes.append((key, p, "无法确认草稿引用关系，无法判定是否可清"))
            continue
        refs = mg.referencing_drafts(p, owners)
        if not refs:
            residuals.append((key, p, "中间产物仍在且无任何草稿引用（可清理）"))

    vers = data.get("versions") if isinstance(data.get("versions"), dict) else {}
    if vers:
        for prof in sorted(vers):
            v = vers[prof] or {}
            state = str(v.get("state") or "").lower()
            ef = v.get("export_file") or paths.get("export_file") or ""
            ed = v.get("export_dir") or ""
            label = f"versions.{prof}"
            if state == "archived":
                target = v.get("archived_to") or ed
                if not target or not mg.exists(target):
                    residuals.append((label, target or "-",
                                      "state=archived 但归档目标不存在"))
                    continue
                miss = _three_piece(draft_name, ef or os.path.join(target, draft_name + ".mp4"),
                                    target)
                if miss:
                    residuals.append((label, target,
                                      "归档成品三件套不完整：" + "；".join(miss)))
                continue
            if state == "cleaned":
                if ef and mg.exists(ef):
                    residuals.append((label, ef, "state=cleaned 但成品仍在盘上"))
                continue
            if not ef and not ed:
                residuals.append((label, "-", f"state={state or '空'} 但没有任何成品路径"))
                continue
            if ef and not mg.exists(ef):
                residuals.append((label, ef, f"state={state or '空'} 但成品不存在"))
                continue
            miss = _three_piece(draft_name, ef, ed)
            if miss:
                residuals.append((label, ef or ed, "成品三件套不完整：" + "；".join(miss)))
        return notes, residuals

    # 旧清单没有 versions：退化为顶层 paths 对账
    ef = paths.get("export_file") or ""
    if ef:
        if not mg.exists(ef):
            residuals.append(("paths.export_file", ef, "清单记录但成品不存在"))
        else:
            miss = _three_piece(draft_name, ef, paths.get("export_dir"))
            if miss:
                residuals.append(("paths.export_file", ef,
                                  "成品三件套不完整：" + "；".join(miss)))
    return notes, residuals


def verify_drama(draft_name=None, all=False, drafts_root=None):
    """本剧（或 --all 全部剧）清单对账；返回 (rc, 残留数, 缺失数)。

    rc 只由本剧自身的残留决定，不受全局 staging 影响。
    """
    owners = mg.referenced_owners(drafts_root)
    manifests = sm._all_manifests()
    names = sorted(manifests) if all else [draft_name]
    if not names or (not all and names[0] not in manifests):
        print(f"无记录: {draft_name}")
        print("本剧汇总: 残留 0 项，缺失 0 项 → rc=1（无记录）")
        return 1, 0, 0

    residual_total = 0
    note_total = 0
    for n in names:
        data = manifests[n][1]
        notes, residuals = check_one(n, data, drafts_root=drafts_root, owners=owners)
        vers = data.get("versions") if isinstance(data.get("versions"), dict) else {}
        summary = "、".join(f"{p}={((vers[p] or {}).get('state') or '-')}"
                           for p in sorted(vers)) or "-"
        print(f"对账 [{n}] state: {summary}")
        for kind, p, why in notes:
            print(f"  [缺] {kind}: {p} — {why}")
        for kind, p, why in residuals:
            print(f"  [残留] {kind}: {p} — {why}")
        if not notes and not residuals:
            print("  [OK] 清单与磁盘一致，无残留")
        note_total += len(notes)
        residual_total += len(residuals)

    rc = 1 if residual_total else 0
    label = f"全部 {len(names)} 部" if all else names[0]
    print(f"本剧汇总[{label}]: 残留 {residual_total} 项，缺失 {note_total} 项 → rc={rc}")
    return rc, residual_total, note_total


def verify_staging(staging_root=None, drafts_root=None):
    """全局 staging（__jybuild__）残留扫描；返回 (rc, 残留数, 总字节数)。

    rc 只由全局 staging 残留决定，与本剧对账互不影响。
    """
    explicit = bool(staging_root)
    st = os.path.abspath(staging_root) if explicit else _staging_default(drafts_root)
    if not st:
        print("[warn] 无法定位 staging 根，跳过全局扫描")
        print("全局 staging 汇总: 残留 0 项，合计 0 B → rc=0（未扫描）")
        return 0, 0, 0
    if not mg.exists(st) or not mg.isdir(st):
        if explicit:
            print(f"[错误] staging 根不存在或不是目录: {st}")
            print("全局 staging 汇总: 残留 0 项，合计 0 B → rc=1（根不可用）")
            return 1, 0, 0
        print(f"全局 staging [{st}]: 目录不存在，无残留 → rc=0")
        return 0, 0, 0

    left = sorted(os.listdir(st))
    if not left:
        print(f"全局 staging [{st}]: 无残留 → rc=0")
        return 0, 0, 0

    print(f"全局 staging 扫描 [{st}]")
    total = 0
    for name in left:
        p = os.path.join(st, name)
        size = _entry_size(p)
        total += size
        print(f"  [残留] staging: {p} — 固定临时构建区（__jybuild__）未清理"
              f"（{_human_size(size)}）")
    print(f"全局 staging 汇总: 残留 {len(left)} 项，合计 {_human_size(total)}"
          f"（{total} 字节） → rc=1")
    return 1, len(left), total


def verify(draft_name=None, all=False, manifest_dir=None, staging_root=None,
           scope="drama"):
    """按 scope 分流对账；返回合并 rc（各段 rc 取最大值）。

    scope=drama（默认）：只看本剧/全部剧，全局 staging 残留不会让本段变红；
    scope=staging：只看全局 staging，单独给数量与总大小；
    scope=both：两段各出结论，合并 rc = max(本剧 rc, 全局 rc)。
    """
    scope = (scope or "drama").lower()
    if scope not in SCOPES:
        print(f"[错误] --scope 只支持 {' / '.join(SCOPES)}: {scope}")
        return 1
    if manifest_dir:
        sm.MANIFEST_DIR = os.path.abspath(manifest_dir)
    drafts_root = _drafts_root()

    print(f"=== verify scope={scope} ===")
    drama_rc = staging_rc = 0
    if scope in ("drama", "both"):
        print("--- 本剧对账 ---")
        drama_rc, _r, _m = verify_drama(draft_name, all=all, drafts_root=drafts_root)
    if scope in ("staging", "both"):
        print("--- 全局 staging 扫描 ---")
        staging_rc, _n, _b = verify_staging(staging_root=staging_root,
                                            drafts_root=drafts_root)

    if scope == "both":
        print("--- 合并结论 ---")
        print(f"本剧 rc={drama_rc}，全局 staging rc={staging_rc}，"
              f"合并 rc={max(drama_rc, staging_rc)}")
    return max(drama_rc, staging_rc)


def main(opt):
    """CLI 分流入口：opt 来自 stream_manifest._parse_manual_args。"""
    if opt.get("manifest_dir"):
        sm.MANIFEST_DIR = os.path.abspath(opt["manifest_dir"])
    scope = str(opt.get("scope") or "drama").lower()
    if scope not in SCOPES:
        print(f"[错误] --scope 只支持 {' / '.join(SCOPES)}: {scope}")
        return 1
    need_drama = scope in ("drama", "both")
    if need_drama and not opt.get("all") and not opt.get("job"):
        print("[用法] verify --manual --job \"<草稿名>\" | --all "
              "[--scope drama|staging|both] [--staging-root <dir>]")
        return 1
    return verify(opt.get("job"), all=bool(opt.get("all")), scope=scope,
                  staging_root=opt.get("staging_root"))
