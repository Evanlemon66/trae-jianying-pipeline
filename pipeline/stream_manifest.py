"""串流产物清单（manifest）：记录每次串流生成的关联文件，支持一键清理。

每次串流 pipeline 发布草稿时自动记录；导出成功后补充成品路径。
用户不需要某部剧的残留时，用 clean 一键清理所有关联文件并删除记录。

用法（在项目根目录执行）：
  python pipeline/stream_manifest.py list              # 列出所有记录及占用
  python pipeline/stream_manifest.py show <草稿名>      # 查看单条记录详情
  python pipeline/stream_manifest.py clean --job "<草稿名>" [--dry-run]      # 清理整部剧（所有版本）
  python pipeline/stream_manifest.py clean --all [--dry-run]                # 清理所有记录
  python pipeline/stream_manifest.py clean --job "<草稿名>" --require-receipts   # 严格模式
  python pipeline/stream_manifest.py clean --job "<草稿名>" --include-drafts     # 连 C 盘草稿一起删
  python pipeline/stream_manifest.py clean --job "<草稿名>" --intermediates-only # 只清中间产物
  python pipeline/stream_manifest.py clean --job "<草稿名>" --profile b [--dry-run]  # 只清 b 版

默认行为（信任"用户让清理＝已发布"，发布流程尚未自动化）：
  `clean --job` 默认清**整部剧的所有版本**（基础版 + 各 `_<版本>` 克隆版）：
  清理 ① __jycache__ ② _work_* 工作目录 ③ 各版本导出成品目录（mp4+封面+progress.json）；
  **默认不删 C 盘剪映草稿**，需显式 --include-drafts。
  只清某一个版本必须显式 `--profile <p>`：此时只删该版本的成品目录，
  __jycache__ / _work_* / C 盘草稿等共享产物一律保留，并提示仍被多少个版本引用。
  --require-receipts 严格模式：必须有成功回执（06_publish/receipts 或 platform json 标 published+url/id），否则整剧跳过。
  --intermediates-only 只清 ① __jycache__ ② _work_*，成品目录与草稿一律不动（不受 include_drafts 影响）；
  该模式只对整部剧生效，不能与 --profile 同用。
  --dry-run 只打印计划与大小，不做任何删除、不改 manifest。
  真正删除导出成品后，回写该剧 job.json 的 video.outputs[版本].state（单版本 clean）或
  video.export_state="cleaned"（整剧 clean）；dry-run / --intermediates-only 不写。
  体积统计按 realpath 去重：多版本共享的 __jycache__ / _work_* 只计一次。

安全约束：绝不清理 source_dir（用户源视频目录本体）；保护区（_broken / cover_job / s2·s3·ss.mp4 /
pipeline/_work 旧状态文件）命中即跳过；manifest 记录本身永不删除。
"""
import json, os, sys, time, shutil

sys.stdout.reconfigure(encoding="utf-8")

MANIFEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_stream_manifests")


def _manifest_path(draft_name):
    safe = draft_name.replace("/", "_").replace("\\", "_").replace(":", "_")
    return os.path.join(MANIFEST_DIR, f"{safe}.json")


def _all_manifests():
    """{draft_name: (path, data)}；坏文件跳过。draft_name 缺失时退回文件名。"""
    out = {}
    if not os.path.isdir(MANIFEST_DIR):
        return out
    for fn in sorted(os.listdir(MANIFEST_DIR)):
        if not fn.endswith(".json"):
            continue
        fp = os.path.join(MANIFEST_DIR, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        out[str(data.get("draft_name") or fn[:-5])] = (fp, data)
    return out


def _sync_shared_with():
    """重算所有 manifest 的 shared_with：某路径被多个版本引用时记下其它版本名。

    目的：多版本共存时 __jycache__ / _work_* 是同一份，清理与体积统计必须
    知道"还有谁在用"，避免单版本清理把别人的中间产物删掉、或重复计体积。
    """
    mans = _all_manifests()
    owners = {}  # realpath(normcase) -> [draft_name]
    for name, (_fp, d) in mans.items():
        for key, p in (d.get("paths") or {}).items():
            if key == "source_dir" or not p:
                continue
            try:
                rp = os.path.normcase(os.path.realpath(p))
            except OSError:
                rp = os.path.normcase(os.path.abspath(p))
            owners.setdefault(rp, []).append(name)
    for name, (fp, d) in mans.items():
        shared = {}
        for key, p in (d.get("paths") or {}).items():
            if key == "source_dir" or not p:
                continue
            try:
                rp = os.path.normcase(os.path.realpath(p))
            except OSError:
                rp = os.path.normcase(os.path.abspath(p))
            others = sorted(x for x in owners.get(rp, []) if x != name)
            if others:
                shared[key] = others
        new_val = shared or None
        if d.get("shared_with") == new_val:
            continue
        if new_val:
            d["shared_with"] = new_val
        else:
            d.pop("shared_with", None)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)


def _base_draft_name(names):
    """从一组版本草稿名里找出基础版：其它名字都以 `<基础>_` 开头的最短名字。"""
    for n in sorted(names, key=len):
        others = [m for m in names if m != n]
        if others and all(m.startswith(n + "_") for m in others):
            return n
    return sorted(names, key=len)[0] if names else None


def version_profile(draft_name, group_names):
    """草稿名 → 版本名：基础版返回 'a'，克隆版返回后缀（b/c/d…）。"""
    base = _base_draft_name(list(group_names) or [draft_name])
    if not base or draft_name == base:
        return "a"
    prefix = base + "_"
    return draft_name[len(prefix):] if draft_name.startswith(prefix) else "a"


def _same_job_group(target_name, manifests):
    """与 target 指向同一部剧（同 source_dir）的全部 manifest 名（含自身）。"""
    target = manifests.get(target_name)
    if not target:
        return [target_name]
    src = ((target[1].get("paths") or {}).get("source_dir") or "")
    key = os.path.normcase(os.path.normpath(src)) if src else ""
    if not key:
        return [target_name]
    names = [target_name]
    for name, (_fp, d) in manifests.items():
        s = ((d.get("paths") or {}).get("source_dir") or "")
        if s and os.path.normcase(os.path.normpath(s)) == key and name not in names:
            names.append(name)
    return names


def record(draft_name, source_dir=None, draft_dir=None, jycache_dir=None,
           workdir=None, export_file=None):
    """记录/更新一条串流产物清单（同草稿名多次调用做字段合并）。"""
    os.makedirs(MANIFEST_DIR, exist_ok=True)
    mp = _manifest_path(draft_name)
    data = {}
    if os.path.exists(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["draft_name"] = draft_name
    data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data.setdefault("recorded_at", data["updated_at"])
    for k, v in (("source_dir", source_dir), ("draft_dir", draft_dir),
                 ("jycache_dir", jycache_dir), ("workdir", workdir),
                 ("export_file", export_file)):
        if v:
            data.setdefault("paths", {})[k] = v
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        _sync_shared_with()
    except OSError:
        pass
    return mp


def _dir_size(p):
    total = 0
    for root, _, files in os.walk(p):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _fmt(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _entry_size(kind, p):
    if not p or not os.path.exists(p):
        return 0
    if kind == "export_file" or os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return 0
    return _dir_size(p)


def show_all():
    if not os.path.isdir(MANIFEST_DIR):
        print("（无任何记录）")
        return
    files = sorted(f for f in os.listdir(MANIFEST_DIR) if f.endswith(".json"))
    if not files:
        print("（无任何记录）")
        return
    # 体积按 realpath 去重：多版本共享的 __jycache__ / _work_* 只算在第一个版本名下
    owners: dict[str, list[str]] = {}
    loaded = []
    for fn in files:
        with open(os.path.join(MANIFEST_DIR, fn), encoding="utf-8") as f:
            d = json.load(f)
        loaded.append(d)
        for k, p in (d.get("paths") or {}).items():
            if k == "source_dir" or not p:
                continue
            owners.setdefault(os.path.normcase(os.path.realpath(p)), []).append(
                str(d.get("draft_name") or fn[:-5]))
    print(f"{'草稿名':<44} {'残留大小':>10}  状态")
    for d in loaded:
        paths = d.get("paths", {})
        name = str(d.get("draft_name") or "")
        total = 0
        for k, p in paths.items():
            if k == "source_dir" or not p:
                continue
            own = sorted(owners.get(os.path.normcase(os.path.realpath(p)), [name]))
            if not own or own[0] != name:
                continue  # 共享路径记在第一个版本名下，这里不重复计
            total += _entry_size(k, p)
        missing = [k for k, p in paths.items()
                   if k != "source_dir" and p and not os.path.exists(p)]
        status = "已清理" if len(missing) == len([k for k in paths if k != "source_dir"]) and paths else \
                 (f"缺 {len(missing)} 项" if missing else "完整")
        if d.get("shared_with"):
            status += f"（共享 {len(d['shared_with'])} 项）"
        print(f"{name:<44} {_fmt(total):>10}  {status}")
    print(f"\n清理: python pipeline/stream_manifest.py clean --job \"<草稿名>\" [--dry-run]")


def show_one(draft_name):
    mp = _manifest_path(draft_name)
    if not os.path.exists(mp):
        print(f"无记录: {draft_name}")
        return
    with open(mp, encoding="utf-8") as f:
        d = json.load(f)
    print(json.dumps(d, ensure_ascii=False, indent=2))


PROTECTED_FILES = ("s2.mp4", "s3.mp4", "ss.mp4")
SUCCESS_WORDS = ("success", "published", "uploaded", "succeeded", "成功", "已发布")


def _blocked_reason(tp, source_dir):
    """保护区断言：返回不可删原因；空串表示可以删。"""
    a = os.path.abspath(tp)
    s = os.path.abspath(source_dir) if source_dir else ""
    if s and a == s:
        return "等于源视频目录"
    if s and s.startswith(a + os.sep):
        return "包含源视频目录"
    parts = a.split(os.sep)
    if "_broken" in parts:
        return "位于 _broken 保留区"
    if "cover_job" in parts:
        return "位于 cover_job 正式产物区"
    if os.path.basename(a) in PROTECTED_FILES:
        return "散落成品文件（s2/s3/ss.mp4）"
    if os.path.basename(a) == "_work":
        return "pipeline/_work 旧状态目录"
    return ""


def _text_has_success(path):
    """判定一个回执/平台 json 是否表示"成功发布"。"""
    try:
        if os.path.getsize(path) > 2 * 1024 * 1024:
            return False
        with open(path, encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return False
    try:
        d = json.loads(txt)
    except ValueError:
        d = None
    if isinstance(d, dict):
        st = str(d.get("status") or d.get("state") or "").strip().lower()
        if st in ("ok", "success", "succeeded", "published", "uploaded"):
            return True
        if d.get("url") or d.get("video_id") or d.get("videoId"):
            return True
    low = txt.lower()
    return any(w in low for w in SUCCESS_WORDS)


def receipts_status(source_dir):
    """严格模式：06_publish/receipts 内有成功回执，或 platform json 标 published 且有 url/id。"""
    if not source_dir:
        return False, "记录无 source_dir，无法定位 cover_job"
    pub = os.path.join(source_dir, "cover_job", "06_publish")
    if not os.path.isdir(pub):
        return False, "无 06_publish 目录"
    rec = os.path.join(pub, "receipts")
    if os.path.isdir(rec):
        for fn in sorted(os.listdir(rec)):
            fp = os.path.join(rec, fn)
            if os.path.isfile(fp) and _text_has_success(fp):
                return True, f"回执 {fn}"
    for fn in sorted(os.listdir(pub)):
        if fn.lower().endswith(".json") and os.path.isfile(os.path.join(pub, fn)):
            fp = os.path.join(pub, fn)
            if _text_has_success(fp):
                try:
                    with open(fp, encoding="utf-8") as f:
                        d = json.load(f)
                except ValueError:
                    continue
                st = str(d.get("status") or "").strip().lower()
                if st in ("published", "uploaded") and (d.get("url") or d.get("video_id")):
                    return True, f"{fn} status={st}"
    return False, "无成功回执（receipts 为空、platform json 未标 published/url）"


def _plan(paths, draft_name, include_drafts, intermediates_only=False,
          only_export=False):
    """清理计划：中间产物 → 成品 →（可选）草稿。返回 [(label, kind, path)]。

    only_export=True（按版本清）：只列该版本的成品目录，共享中间产物与草稿一律不动。
    """
    items = []
    if not only_export:
        for kind in ("jycache_dir", "workdir"):
            if paths.get(kind):
                items.append((kind, kind, paths[kind]))
    if intermediates_only:
        return items
    ef = paths.get("export_file")
    if ef:
        parent = os.path.dirname(os.path.abspath(ef))
        if os.path.basename(parent) == draft_name:
            # 新布局：成品/封面/进度同处「剧名子目录」，整目录一次清理
            items.append(("export_bundle", "export_bundle", parent))
        else:
            # 旧平铺布局兼容：逐个列成品与伴随文件
            stem = os.path.splitext(os.path.abspath(ef))[0]
            items.append(("export_file", "export_file", os.path.abspath(ef)))
            items.append((f"{os.path.basename(stem)}.progress.json", "export_file", stem + ".mp4.progress.json"))
            items.append((f"{os.path.basename(stem)}-封面.jpg", "export_file", stem + "-封面.jpg"))
    if include_drafts and not only_export and paths.get("draft_dir"):
        items.append(("draft_dir", "draft_dir", paths["draft_dir"]))
    return items


def _mark_export_cleaned(source_dir, deleted, profile=None):
    """成品目录真正删除后回写 job.json（失败只告警）。

    profile=None（整剧清）：video.export_state=cleaned，且所有 outputs 版本置 cleaned。
    profile=<p>（单版本清）：只置 outputs[p].state=cleaned；所有声明版本都清完才
    把顶层 export_state 置 cleaned，否则保持 exported（其余版本仍占盘）。
    """
    if not source_dir:
        print("  [warn] 记录无 source_dir，未回写 job.json")
        return
    job_dir = os.path.join(source_dir, "cover_job")
    if not os.path.isfile(os.path.join(job_dir, "job.json")):
        print(f"  [skip] 无 job.json，未回写: {job_dir}")
        return
    total = sum(s for _, s in deleted)
    detail = "成品目录已被 clean 删除，路径：" + "；".join(p for p, _ in deleted) \
             + f"（释放 {_fmt(total)}）"
    if profile:
        detail += f"（单版本 profile={profile}）"
    try:
        lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import job_manifest as jm
        job = jm.load_job(os.path.join(job_dir, "job.json"))
        outputs = dict(((job.get("video") or {}).get("outputs") or {}))
        patch_video = {}
        if profile:
            entry = dict(outputs.get(profile) or {})
            entry["state"] = "cleaned"
            outputs[profile] = entry
            patch_video["outputs"] = outputs
            if outputs and all((v or {}).get("state") == "cleaned" for v in outputs.values()):
                patch_video["export_state"] = "cleaned"
        else:
            for k, v in outputs.items():
                outputs[k] = dict(v or {}, state="cleaned")
            patch_video = {"export_state": "cleaned", "outputs": outputs}
        job = jm.update_job(job_dir, {"video": patch_video},
                            event="export_cleaned", detail=detail)
        scope = f"profile={profile}" if profile else "整剧"
        print(f"  [回写] job.json export_cleaned（{scope}）revision={job['revision']}")
    except (ValueError, OSError, ImportError) as e:
        print(f"  [warn] 回写 job.json 失败（不影响已完成的删除）: {e}")


def _job_video(source_dir):
    """读 job.json 的 video 段（clean 的兜底来源；读不到返回 {}）。"""
    if not source_dir:
        return {}
    jp = os.path.join(source_dir, "cover_job", "job.json")
    try:
        with open(jp, encoding="utf-8-sig") as f:
            job = json.load(f)
    except (OSError, ValueError):
        return {}
    video = job.get("video")
    return video if isinstance(video, dict) else {}


def _norm_profile(value):
    s = str(value or "").strip().lower()
    return s[len("presets_"):] if s.startswith("presets_") else s


def clean(draft_name, dry_run=False, include_drafts=False, require_receipts=False,
          intermediates_only=False, profile=None):
    """清理一部剧的串流残留。默认信任"已发布"，不删草稿（除非 include_drafts）。

    默认（profile=None）：清整部剧**所有版本**（基础版 + 各 `_<版本>` 克隆版）。
    profile=<p>：只清该版本的成品目录；__jycache__ / _work_* / C 盘草稿等共享
    产物一律保留，并提示仍被多少个版本引用。与 intermediates_only 互斥。

    返回 0=已执行或计划完成；1=记录不存在/被严格模式跳过/参数冲突；2=路径错误。
    """
    manifests = _all_manifests()
    if draft_name not in manifests:
        print(f"无记录: {draft_name}")
        return 1
    group = _same_job_group(draft_name, manifests)
    profile = _norm_profile(profile)
    if profile and intermediates_only:
        print("  [错误] --intermediates-only 只能整剧清理，不能与 --profile 同用")
        return 1
    if profile:
        targets = [n for n in group if version_profile(n, group) == profile]
        if not targets:
            avail = "、".join(f"{version_profile(n, group)}={n}" for n in sorted(group))
            print(f"  [错误] 该剧没有 profile={profile} 的版本；现有: {avail}")
            return 1
    else:
        targets = sorted(group)
    base = _base_draft_name(group) or draft_name
    title = manifests[draft_name][1].get("draft_name", draft_name)
    source_dir = ((manifests[targets[0]][1].get("paths") or {}).get("source_dir") or "")
    video = _job_video(source_dir)
    if profile:
        print(f"清理 [{title}]（只清版本 profile={profile}）:")
    else:
        print(f"清理 [{title}]（整部剧，共 {len(targets)} 个版本）:")
    if require_receipts:
        ok, why = receipts_status(source_dir)
        if not ok:
            print(f"  [跳过] 严格模式未通过：{why}")
            return 1
        print(f"  [通过] 回执校验：{why}")
    if intermediates_only:
        print("  [模式] intermediates-only：只清 __jycache__ / _work_*，成品与草稿一律不动")
    elif profile:
        others = max(0, len(group) - len(targets))
        print(f"  [模式] 单版本清理：共享中间产物（__jycache__/_work_*/C 盘草稿）保留，"
              f"仍有 {others} 个版本引用")

    total = 0
    ready = []
    seen = set()
    for n in targets:
        paths = dict((manifests[n][1].get("paths") or {}))
        # 兜底：旧 manifest 可能缺 export_file（record 时未写），整剧清时用 job.json
        # 的 video.export_file，避免"成品还在盘上、clean 却静默跳过"的漏删。
        if not profile and n == base and not paths.get("export_file") and video.get("export_file"):
            paths["export_file"] = video["export_file"]
            print(f"  [回退] {n}: manifest 缺 export_file，改用 job.json: {paths['export_file']}")
        if (not intermediates_only) and not paths.get("export_file"):
            pref = f"{n}: " if len(targets) > 1 else ""
            exported_evidence = (str(video.get("status") or "").lower() == "done"
                                 or str(video.get("export_state") or "").lower()
                                 in ("exported", "cleaned"))
            if exported_evidence:
                print(f"  [错误] {pref}找不到任何成品路径：manifest 无 export_file，"
                      "job.json 也无可用路径，但记录显示已导出——成品可能仍占盘。"
                      "请先用 stream_manifest.py record 补写 export_file 再清理。")
                return 2
            print(f"  [warn] {pref}找不到成品路径（manifest 与 job.json 均无 export_file），"
                  "本次只清中间产物；若成品已导出请先补写记录")
        for label, kind, p in _plan(paths, n, include_drafts, intermediates_only,
                                    only_export=bool(profile)):
            rp = os.path.normcase(os.path.realpath(p))
            if rp in seen:
                continue  # 多版本共享的同一路径只处理/计数一次
            seen.add(rp)
            why = _blocked_reason(p, source_dir)
            if why:
                print(f"  [受保护] {label}: {p}（{why}），跳过")
                continue
            if not os.path.exists(p):
                print(f"  [skip] {label}: 不存在 {p}")
                continue
            size = _entry_size(kind, p)
            total += size
            ready.append((n, label, kind, p, size))
            print(f"  [{'计划' if dry_run else '待删'}] {label}: {p} ({_fmt(size)})")
    print(f"  合计: {_fmt(total)}")
    if dry_run:
        print("  （dry-run：未执行任何删除，记录未改动）")
    else:
        print("  （非 dry-run：即将实际删除，可先用 --dry-run 复核）")
        cleaned = []
        for n, label, kind, p, size in ready:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"  [fail] {label}: {e}")
                    continue
            if not os.path.exists(p):
                print(f"  [已删] {label}: {p} ({_fmt(size)})")
                if kind == "export_bundle" or (kind == "export_file" and label == "export_file"):
                    cleaned.append((p, size))
            else:
                print(f"  [失败] {label}: {p}")
        if cleaned:
            _mark_export_cleaned(source_dir, cleaned, profile=profile or None)
    # source_dir 只提示，永不清理；manifest 记录本身也永不删除
    if source_dir:
        print(f"  （源视频目录保留不动: {source_dir}）")
    return 0


def _manifest_names():
    if not os.path.isdir(MANIFEST_DIR):
        return []
    names = []
    for fn in sorted(os.listdir(MANIFEST_DIR)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(MANIFEST_DIR, fn), encoding="utf-8") as f:
                    names.append(json.load(f).get("draft_name", fn[:-5]))
            except ValueError:
                continue
    return names


def _parse_clean_args(args):
    """解析 clean 子命令：--job/--all/--dry-run/--require-receipts/--include-drafts/
    --intermediates-only/--profile。"""
    opt = {"job": None, "all": False, "dry_run": False,
           "require_receipts": False, "include_drafts": False,
           "intermediates_only": False, "profile": None}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--all":
            opt["all"] = True
        elif a == "--dry-run":
            opt["dry_run"] = True
        elif a == "--require-receipts":
            opt["require_receipts"] = True
        elif a == "--include-drafts":
            opt["include_drafts"] = True
        elif a == "--intermediates-only":
            opt["intermediates_only"] = True
        elif a == "--profile" and i + 1 < len(args):
            i += 1
            opt["profile"] = args[i]
        elif a in ("--job", "-j") and i + 1 < len(args):
            i += 1
            opt["job"] = args[i]
        elif a.lower() == "all" and not opt["job"]:
            opt["all"] = True
        elif not a.startswith("-") and not opt["job"]:
            opt["job"] = a
        i += 1
    return opt


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "list":
        show_all()
    elif cmd == "show" and len(sys.argv) >= 3:
        show_one(" ".join(sys.argv[2:]))
    elif cmd == "clean":
        opt = _parse_clean_args(sys.argv[2:])
        if not opt["all"] and not opt["job"]:
            print(__doc__)
            return
        if opt["all"] and opt["profile"]:
            print("[错误] --all 与 --profile 不能同时使用（--all 语义是每部剧整剧清理）")
            return
        names = _manifest_names() if opt["all"] else [opt["job"]]
        if not names:
            print("（无任何记录）")
            return
        skipped = []
        for n in names:
            rc = clean(n, dry_run=opt["dry_run"],
                       include_drafts=opt["include_drafts"],
                       require_receipts=opt["require_receipts"],
                       intermediates_only=opt["intermediates_only"],
                       profile=opt["profile"])
            if rc != 0:
                skipped.append(n)
            print()
        if skipped:
            print("跳过（严格模式/无记录）:")
            for n in skipped:
                print(f"  - {n}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
