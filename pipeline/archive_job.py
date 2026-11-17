"""archive_job.py — 已完成剧的归档：置 stage=archived 并搬入独立归档池。

背景：watcher（lib/cover_watch.py）靠 check_trigger 的 stage 白名单 +
video.status=done 挡住已导出剧，但剧目录仍留在公共 roots；本脚本负责
"显式归档"：写 stage=archived（check_trigger 硬拦），再把整剧目录移出 roots。

用法（项目根目录）：
  python pipeline/archive_job.py --job "<cover_job>" --archive-root "<归档根>" --dry-run
  python pipeline/archive_job.py --job "<cover_job>" --archive-root "<归档根>"

准入（默认满足其一即可）：
  - publish.status == "published"（已有成功回执）
  - video.export_state == "cleaned"（成品已清理，视为发布后的收尾）
其余情况跳过；确需归档用 --force。
拒绝条件：.watcher.lock 的 pid 仍存活；.export_state.json status=exporting；
成品三件套校验未通过（exported 版本缺 mp4 / progress.done / 封面，或封面非竖版 9:16）；
新鲜度判定 stale/evidence_missing（已 clean 的版本除外：export_state=cleaned 或
outputs[*].state=cleaned 视作合法已清理状态，不参与阻断）；
归档目标已存在；archive_root 等于或位于任一有效扫描根之下（规范化真实路径比较）；
有效扫描根为空（fail-closed，无法校验位置时拒绝执行；series_root 必填后正常不会为空）；
job_dir 不是 <剧名>/cover_job；剧集目录是盘符根；剧集目录不是有效扫描根的直接子级。
有效扫描根 = pipeline/export_config.get_watch_roots()（series_root + watch.roots 额外根）。
以上路径相关拒绝不受 --force 影响（搬错目录不可逆）。

断点可重入：stage 已是 archived 时，只要源目录仍在、归档目标不存在，就继续移动；
只有"源目录已不在"或"目标已存在"才跳过。

行为（非 dry-run）：
  1. job.json 置 stage="archived"（job_manifest.update_job，revision+1，history 记 archived）
  2. 整个剧集目录 <root>/<剧名> 移动到 <archive_root>/<剧名>（同盘为改名）
  3. pipeline/_stream_manifests/*.json 中 paths.source_dir 指向旧目录的记录改写为新路径
  --dry-run 只打印计划，不写盘、不移动。
archive_root 未传时读 export_config.get_archive_root()
（JY_ARCHIVE_ROOT > presets.json archive_root > watch.archive_root 兼容）。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "lib"))
sys.path.insert(0, os.path.join(_REPO, "pipeline"))

import job_manifest as jm  # noqa: E402
import export_config as pc  # noqa: E402
from export_bundle import audit_bundle, log_forced_use  # noqa: E402
import export_lock  # noqa: E402  # 与导出侧同一把 .export.lock，防并发互踩
from no_window import NO_WINDOW  # noqa: E402

LOCK_NAME = ".watcher.lock"
STATE_NAME = ".export_state.json"


def _norm_job_dir(path: str) -> str:
    path = os.path.abspath(path)
    if os.path.isfile(path) and os.path.basename(path) == "job.json":
        path = os.path.dirname(path)
    return path


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.strip()
        for line in out.splitlines():
            cols = [c.strip('"') for c in line.split('","')]
            if len(cols) > 1 and cols[1] == str(pid):
                return True
    except Exception:
        pass
    return False


def _lock_alive(job_dir: str) -> bool:
    try:
        with open(os.path.join(job_dir, LOCK_NAME), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return _pid_alive(int((data or {}).get("pid") or 0))


def _exporting(job_dir: str) -> bool:
    try:
        with open(os.path.join(job_dir, STATE_NAME), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return False
    return (st or {}).get("status") == "exporting"


def _real(path: str) -> str:
    """规范化真实路径（Windows 下大小写不敏感）。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _watch_roots() -> list[str]:
    """有效扫描根：series_root + watch.roots 额外根（统一走 export_config）。"""
    return pc.get_watch_roots()


def _root_hit(archive_root: str, roots: list[str]) -> str | None:
    """archive_root 等于或位于某个 root 之下时返回该 root，否则 None。"""
    ar = _real(archive_root)
    for root in roots:
        rr = _real(root).rstrip(os.sep)
        if ar == rr or ar.startswith(rr + os.sep):
            return root
    return None


def _series_dir_guard(job_dir: str, series_dir: str, roots: list[str]) -> str | None:
    """移动前的路径断言；返回拒绝原因，None=通过。不受 --force 影响。

    - job_dir 必须是 <剧集目录>/cover_job（out_dir 覆盖后仍必须是这个名字）
    - 剧集目录不能是盘符根/文件系统根（dirname(root) == root）
    - 剧集目录必须是某个有效扫描根的直接子级（不是更深层，也不是 root 本身）

    缺这层校验时，out_dir 一旦指到共享父目录，归档会把整层目录（甚至 D:\\）
    搬进归档池，而 shutil.move 不可逆。
    """
    base = os.path.basename(os.path.normpath(job_dir)).lower()
    if base != "cover_job":
        return f"job_dir 必须是 <剧名>/cover_job（实际: {job_dir}）"
    sd = os.path.normpath(series_dir)
    if os.path.normcase(os.path.dirname(sd)) == os.path.normcase(sd):
        return f"剧集目录不能是盘符根/文件系统根: {series_dir}"
    parent = os.path.dirname(os.path.realpath(sd)).rstrip("\\/")
    for root in roots:
        rr = os.path.realpath(root).rstrip("\\/")
        if os.path.normcase(parent) == os.path.normcase(rr):
            return None
    return (f"剧集目录必须是某个有效扫描根的直接子级"
            f"（实际: {series_dir}；roots: {', '.join(roots)}）")


def _eligible(job: dict) -> bool:
    pub = str((job.get("publish") or {}).get("status") or "")
    exp = (job.get("video") or {}).get("export_state")
    return pub == "published" or exp == "cleaned"


def _declared_profiles(job: dict, job_dir: str | None = None) -> list[str]:
    """待归档版本：与 watcher 同口径（job 优先、prompts 兜底）。

    1. job.json: titles.selected_groups[].group → prompts.items[].group
       → workflow.preset_profile / preset_profile
    2. job 没有或不完整时才回落 03_prompts/prompts.json 的 items[].group
    """
    def _norm(values) -> list[str]:
        out: list[str] = []
        for v in values or []:
            g = str(v or "").strip().lower()
            if g.startswith("presets_"):
                g = g[len("presets_"):]
            if re.fullmatch(r"[a-z][a-z0-9]{0,7}", g) and g not in out:
                out.append(g)
        return out

    titles = job.get("titles") or {}
    declared = _norm([(g or {}).get("group") for g in (titles.get("selected_groups") or [])])
    if not declared:
        declared = _norm([(it or {}).get("group")
                          for it in ((job.get("prompts") or {}).get("items") or [])])
    if not declared:
        wf = job.get("workflow") or {}
        declared = _norm(pc.parse_preset_profiles(
            wf.get("preset_profile") or job.get("preset_profile")))
    if job_dir and not declared:
        # 只有 job 完全没有组声明时才回落 prompts.json（严格互斥，不取并集）
        pp = os.path.join(job_dir, "03_prompts", "prompts.json")
        try:
            with open(pp, encoding="utf-8-sig") as f:
                declared = _norm([(it or {}).get("group")
                                  for it in (json.load(f).get("items") or [])])
        except (OSError, ValueError):
            declared = []
    return sorted(declared, key=lambda g: (g != "a", g))


def _pending_outputs(job: dict, job_dir: str | None = None) -> list[str]:
    """所有声明版本里仍未导出的（缺 outputs 条目 / state 不是 exported|cleaned）。

    归档会把整剧目录搬出 watch.roots，watcher 再也补不了未导出的版本；
    因此必须要求声明版本全部完成，--force 才可跳过。
    """
    outputs = (job.get("video") or {}).get("outputs")
    outputs = outputs if isinstance(outputs, dict) else {}
    declared = _declared_profiles(job, job_dir)
    names = list(dict.fromkeys(declared + list(outputs)))
    pending = []
    for p in names:
        v = outputs.get(p) or {}
        st = str(v.get("state") or "").lower()
        if st not in ("exported", "cleaned"):
            pending.append(f"{p}({st or '未导出'})")
    return pending


def _blocked_versions(job: dict) -> list[dict]:
    """blocked 版本单列（仍计为 pending 挡归档，但不再糊成"未完成"）。"""
    outputs = (job.get("video") or {}).get("outputs")
    outputs = outputs if isinstance(outputs, dict) else {}
    out = []
    for p, v in outputs.items():
        v = v or {}
        if str(v.get("state") or "").lower() != "blocked":
            continue
        out.append({
            "profile": p,
            "missing": str(v.get("missing") or f"presets_{p}"),
            "reason": str(v.get("reason") or "missing_presets"),
        })
    return sorted(out, key=lambda x: (x["profile"] != "a", x["profile"]))


def _audit_outputs(job: dict, job_dir: str) -> list[tuple[str, list[str]]]:
    """按导出侧同口径校验每个 exported 版本的成品三件套。严格判定，缺件即冲突。"""
    outputs = (job.get("video") or {}).get("outputs")
    outputs = outputs if isinstance(outputs, dict) else {}
    bad = []
    for p, v in outputs.items():
        v = v or {}
        if str(v.get("state") or "").lower() != "exported":
            continue
        draft = str(v.get("draft") or p)
        res = audit_bundle(draft, v.get("export_file"), v.get("export_dir"))
        if not res["ok"]:
            bad.append((p, res["missing"]))
    return bad


def _log_block(job_dir: str, name: str, blocked: list[dict],
               conflicts: list[tuple[str, list[str]]], dry_run: bool = False) -> None:
    """清单落一份日志：06_publish/archive_blocked.log（追加，不覆盖历史）。"""
    lines = [f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] archive blocked: {name}"]
    for b in blocked:
        lines.append(f"  blocked  version={b['profile']}  missing={b['missing']}  reason={b['reason']}")
    for p, missing in conflicts:
        for m in missing:
            lines.append(f"  conflict version={p}  missing={m}")
    path = os.path.join(job_dir, "06_publish", "archive_blocked.log")
    if dry_run:
        print(f"  [日志] dry-run 不写盘（预计追加 {len(lines) - 1} 行 → {path}）")
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"  [日志] {path}")
    except OSError as e:
        print(f"  [warn] 写 blocked 日志失败: {e}")


def _rewrite_manifests(manifest_dir: str, old_dir: str, new_dir: str,
                       dry_run: bool) -> int:
    """把 paths.source_dir=旧剧集目录 的记录改写为新路径；返回命中记录数。"""
    if not os.path.isdir(manifest_dir):
        return 0
    old_key = os.path.normcase(os.path.normpath(old_dir))
    hits = []
    for fn in sorted(os.listdir(manifest_dir)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(manifest_dir, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        src = (data.get("paths") or {}).get("source_dir")
        if not src or os.path.normcase(os.path.normpath(src)) != old_key:
            continue
        hits.append((fp, data))
    for fp, data in hits:
        print(f"  [manifest] {os.path.basename(fp)}: source_dir → {new_dir}")
        if not dry_run:
            data["paths"]["source_dir"] = os.path.normpath(new_dir)
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, fp)
    return len(hits)


def archive(job_dir: str, archive_root: str, manifest_dir: str, *,
            dry_run: bool = False, force: bool = False,
            roots: list[str] | None = None,
            allow_stale_reason: str | None = None) -> int:
    """归档一部剧。返回 0=成功/计划完成；1=跳过；2=失败。"""
    job_dir = _norm_job_dir(job_dir)
    if not os.path.isfile(os.path.join(job_dir, "job.json")):
        print(f"[FAIL] 无 job.json: {job_dir}")
        return 2
    series_dir = os.path.dirname(os.path.abspath(job_dir.rstrip("\\/")))
    name = os.path.basename(series_dir)
    job = jm.load_job(os.path.join(job_dir, "job.json"))
    print(f"归档 [{name}] stage={job.get('stage')}")

    archive_root = os.path.abspath(archive_root)
    if roots is None:
        try:
            roots_list = _watch_roots()
        except pc.ConfigError as exc:
            print(f"  [FAIL] 无法解析有效扫描根（{exc.key}）：{exc}")
            if exc.hint:
                print(f"          {exc.hint}")
            return 2
    else:
        roots_list = roots
    if not roots_list:
        # fail-closed：没有扫描根就无法判断归档池是否落在公共区，宁可不搬
        print("  [FAIL] 有效扫描根为空，无法校验归档池位置，已拒绝执行；"
              "请检查 pipeline/presets.json 的 series_root")
        return 2
    hit = _root_hit(archive_root, roots_list)
    if hit:
        print(f"  [FAIL] archive_root 不能等于或位于有效扫描根之下"
              f"（命中 root: {hit}）: {archive_root}")
        return 2

    # 路径断言先于 --force / dry-run：搬错的目录不可逆，必须硬拦
    guard_err = _series_dir_guard(job_dir, series_dir, roots_list)
    if guard_err:
        print(f"  [FAIL] {guard_err}")
        return 2

    target = os.path.join(archive_root, name)
    archived = jm.canon_stage(job.get("stage")) == "archived"

    if archived:
        # 断点可重入：上次可能已置 archived 但移动失败，只要源还在、目标不存在就续做
        if not os.path.isdir(series_dir):
            if os.path.exists(target):
                print(f"  [skip] 已归档完成（源已不在，目标存在）: {target}")
                return 1
            print(f"  [FAIL] stage=archived 但源目录与归档目标都不存在: {series_dir} -> {target}")
            return 2
        if os.path.exists(target):
            print(f"  [skip] 归档目标已存在（源目录仍在，需人工处理冲突）: {target}")
            return 1
        print("  [resume] 已是 archived，源目录仍在且目标不存在，继续移动")
    else:
        blocked = _blocked_versions(job)
        if blocked:
            print("  [blocked] 以下版本单列（仍挡归档）:")
            for b in blocked:
                print(f"    - 版本 {b['profile']}：缺 {b['missing']}"
                      f"（reason={b['reason']}）")
        # 成品缺件先展示，避免被"未满足准入"这类理由遮住（A 版只缺封面的场景）。
        # --force 也先算一遍：放过了什么必须留痕，不能凭开关直接跳过校验。
        conflicts = _audit_outputs(job, job_dir)
        if conflicts:
            print("  [conflict] 成品产物校验未通过（mp4 / progress.done / 封面三件套）:")
            for p, missing in conflicts:
                for m in missing:
                    print(f"    - 版本 {p}: {m}")
        if force and conflicts:
            outs = (job.get("video") or {}).get("outputs") or {}
            log_forced_use(job_dir, "archive",
                           [(str((outs.get(p) or {}).get("draft") or p), p, ms)
                            for p, ms in conflicts], dry_run=dry_run)
        if not force and not _eligible(job):
            if blocked or conflicts:
                _log_block(job_dir, name, blocked, conflicts, dry_run=dry_run)
            print("  [skip] 未满足准入（publish.status!=published 且 video.export_state!=cleaned）；"
                  "确需归档请加 --force")
            return 1
        pending = _pending_outputs(job, job_dir)
        if not force and pending:
            _log_block(job_dir, name, blocked, conflicts, dry_run=dry_run)
            print("  [skip] 仍有声明版本未完成导出: " + "、".join(pending)
                  + "；归档后 watcher 无法再补，先补齐或加 --force")
            return 1
        if _lock_alive(job_dir):
            print("  [skip] 存在活锁（.watcher.lock pid 仍存活），请先停止该剧的调度")
            return 1
        if _exporting(job_dir):
            print("  [skip] .export_state.json status=exporting（导出进行中）")
            return 1
        if os.path.normcase(target) == os.path.normcase(series_dir):
            print("  [skip] archive_root 与公共 roots 相同，移动等于原地不动")
            return 1
        if os.path.exists(target):
            print(f"  [skip] 归档目标已存在: {target}")
            return 1
        if conflicts and not force:
            _log_block(job_dir, name, blocked, conflicts, dry_run=dry_run)
            print("  已拒绝归档；补齐缺件后重跑即可自动通过（无需额外登记）；"
                  "确需强推加 --force")
            return 1

    # 新鲜度默认阻断：stale / evidence_missing 的成品不能静默搬进归档池。
    # 人工确认后必须用 --allow-stale "<理由>" 放行，理由会写进归档快照。
    # 例外：clean 已删成品的版本（outputs[*].state=cleaned / export_state=cleaned）
    # 在磁盘上必然查不到 mp4，freshness 会判 output_missing；这是合法收尾，
    # 不能反过来把"已经清理干净"的剧挡在归档之外（clean → archive 死锁）。
    allow_stale_reason = (allow_stale_reason or "").strip() or None
    stale_hit: list[str] = []
    if not archived:
        try:
            import freshness
            probe = freshness.archive_snapshot(job_dir, write=False)
            stale_hit = list(probe.get("stale") or [])
            stale_hit += [v for v in (probe.get("evidence_missing") or [])
                          if v not in stale_hit]
        except Exception as e:
            # 判定本身不可用时沿用原 best-effort 语义，但必须显式提示
            print(f"  [warn] 归档前新鲜度判定失败，按原行为继续: {e}")
        video = job.get("video") or {}
        outputs = video.get("outputs") if isinstance(video.get("outputs"), dict) else {}
        cleaned_versions = {str(p) for p, v in outputs.items()
                            if str((v or {}).get("state") or "").lower() == "cleaned"}
        job_cleaned = str(video.get("export_state") or "").lower() == "cleaned"
        skipped_cleaned = [v for v in stale_hit
                           if job_cleaned or v in cleaned_versions]
        if skipped_cleaned:
            print("  [cleaned] 版本 " + "、".join(skipped_cleaned)
                  + " 已 clean（成品已删），视作合法已清理状态，不参与 stale 阻断")
        stale_hit = [v for v in stale_hit
                     if not job_cleaned and v not in cleaned_versions]
        if stale_hit and not allow_stale_reason:
            print("  [blocked] 新鲜度判定为 stale/evidence_missing: "
                  + "、".join(stale_hit))
            print('  归档默认阻断过期/无证据成品；确认要归档请加 '
                  '--allow-stale "理由"（理由会写入快照）')
            return 1
        if stale_hit:
            print(f"  [allow-stale] 版本 {'、'.join(stale_hit)} 已人工放行："
                  f"{allow_stale_reason}")

    print(f"  剧集目录: {series_dir}")
    print(f"  归档目标: {target}")
    if not archived:
        # 取锁前只打印计划，不能用结果口径（锁可能拿不到，届时 stage 并未变）
        print("  拟 stage → archived（取锁成功后才写入）")
    if dry_run:
        hits = _rewrite_manifests(manifest_dir, series_dir, target, dry_run=True)
        print(f"  manifest 待更新: {hits} 条")
        print("  （dry-run：未写 job.json、未移动目录）")
        return 0

    # 与导出侧同一把 .export.lock：真正动文件（写 job.json / 搬目录 / 改 manifest）
    # 前先取锁，避免和 watcher / series_runner 的导出并发互踩；拿不到按导出侧策略退出。
    if not export_lock.acquire(owner=f"archive_job {name}"):
        holder = export_lock.read_lock() or {}
        print(f"  ⛔ 全局导出锁被占用（pid={holder.get('pid')} "
              f"owner={holder.get('owner')}），本次不归档；"
              "等导出结束或确认无人在跑后重试")
        return 1
    try:
        if not archived:
            try:
                jm.update_job(job_dir, {"stage": "archived"}, event="archived",
                              detail=f"归档到 {target}")
            except (ValueError, OSError) as e:
                print(f"  [FAIL] 置 stage=archived 失败: {e}")
                return 2
            print("  stage → archived")
        os.makedirs(archive_root, exist_ok=True)
        # 归档 = 产物连同出身一起搬走：搬之前把各版本的新鲜度判定固化成快照，
        # 供归档后追溯（stale/evidence_missing 已在上面做过默认阻断与人工放行）。
        try:
            import freshness
            snap = freshness.archive_snapshot(
                job_dir, write=True, allow_stale_reason=allow_stale_reason)
            if snap:
                brief = ", ".join(f"{k}={v.get('state')}"
                                  for k, v in (snap.get("versions") or {}).items())
                print(f"  新鲜度快照: 06_publish/freshness_archive_snapshot.json ({brief})")
                miss = snap.get("evidence_missing") or []
                if miss:
                    print(f"  [warn] evidence_missing（已登记导出但缺 sidecar）: {', '.join(miss)}")
        except Exception as e:
            print(f"  [warn] 新鲜度快照写入失败（不影响归档）: {e}")
        try:
            shutil.move(series_dir, target)
        except OSError as e:
            print(f"  [FAIL] 移动目录失败（stage 已置 archived，调度已阻断；修复后可重跑续做）: {e}")
            return 2
        hits = _rewrite_manifests(manifest_dir, series_dir, target, dry_run=False)
        print(f"  [ok] 已归档: {target}（manifest 更新 {hits} 条）")
        return 0
    finally:
        export_lock.release()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", required=True, help="cover_job 目录或其中的 job.json")
    ap.add_argument("--archive-root", default=None,
                    help="归档池根目录（默认读 export_config.get_archive_root()）")
    ap.add_argument("--manifest-dir", default=os.path.join(_REPO, "pipeline", "_stream_manifests"),
                    help="串流产物清单位置（默认 pipeline/_stream_manifests）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘、不移动")
    ap.add_argument("--force", action="store_true",
                    help="跳过发布/清理准入与成品三件套校验（路径断言不可跳过）")
    ap.add_argument("--allow-stale", default=None, metavar="理由",
                    help="人工放行 stale/evidence_missing 成品归档；理由写入快照")
    args = ap.parse_args(argv)

    root = args.archive_root or pc.get_archive_root()
    if not root:
        print("[FAIL] 未指定 --archive-root，且配置中没有 archive_root"
              "（JY_ARCHIVE_ROOT / presets.json archive_root / watch.archive_root）")
        return 2
    return archive(args.job, root, args.manifest_dir,
                   dry_run=args.dry_run, force=args.force,
                   allow_stale_reason=args.allow_stale)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
