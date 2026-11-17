# -*- coding: utf-8 -*-
"""产物新鲜度：三条边（指令→草稿、草稿→成片、封面→草稿）的写时确权与消费判定。

设计见 specs/artifact-freshness/spec.md。所有函数都是 best-effort：
任何异常都降级为 unknown / legacy，绝不把失败抛回调用方。
"""
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
_PIPELINE_DIR = os.path.join(_REPO_ROOT, "pipeline")


def _ensure_import_paths():
    """本模块可能被 pipeline/、lib/、scripts/ 三处 import，统一补齐搜索路径。"""
    for p in (_HERE, _PIPELINE_DIR, os.path.join(_REPO_ROOT, "scripts")):
        if p and p not in sys.path:
            sys.path.insert(0, p)


_ensure_import_paths()

CANON = "c1"
# 轮询期草稿摘要缓存：(path, mtime_ns, size) → (draft, hash)，上限 8 条
_DRAFT_CACHE: dict = {}
# 进程内已告警过的 (job_dir, version)：watcher 每 5 秒轮询也只响一次
_WARNED: set = set()


def _warn_once(key, message):
    """同类告警每进程只打一次，避免 watcher 轮询刷屏；测试/CLI 单次运行不受影响。"""
    if key in _WARNED:
        return
    if len(_WARNED) > 512:
        _WARNED.clear()
    _WARNED.add(key)
    print(message, file=sys.stderr)


EDGE_E1 = "prompts_to_draft"
EDGE_E2 = "draft_to_video"
EDGE_E3 = "cover_to_draft"
EDGE_ALIAS = {
    "e1": EDGE_E1, "drafts": EDGE_E1, "draft": EDGE_E1, "prompts": EDGE_E1,
    "e2": EDGE_E2, "video": EDGE_E2, "export": EDGE_E2,
    "e3": EDGE_E3, "covers": EDGE_E3, "cover": EDGE_E3,
}
DROP_KEYS = {
    "id", "uuid", "create_time", "update_time", "tm_draft_create",
    "tm_draft_modified", "tm_draft_removed", "mtime", "at", "generated_at",
    "updated_at", "recorded_at", "path", "draft_fold_path", "draft_json_file",
    "draft_root_path", "draft_cover", "built_at", "extra_info", "name",
}
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


# ━━━━━━━━━━━━━━━━ 基础工具 ━━━━━━━━━━━━━━━━

def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def _write_json_atomic(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _sha256_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=4 * 1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return "sha256:" + h.hexdigest()


def _drafts_root():
    from export_config import get_projects_root
    return get_projects_root()


def _preset_root():
    from export_config import get_presets_root
    return get_presets_root()


def _norm_version(version):
    v = str(version or "a").strip().lower()
    if v.startswith("presets_"):
        v = v[len("presets_"):]
    for suf in ("_group", "-group", "组"):
        if v.endswith(suf):
            v = v[: -len(suf)].strip("_-")
    return v or "a"


def _job_json(job_dir):
    return os.path.join(job_dir, "job.json")


def _publish_dir(job_dir):
    return os.path.join(job_dir, "06_publish")


# ━━━━━━━━━━━━━━━━ 规范化 ━━━━━━━━━━━━━━━━

def _clean(obj, round_digits=6):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                continue
            if k in DROP_KEYS or k.endswith("_time") or k.endswith("_id"):
                continue
            if _UUID_RE.match(str(k)):
                continue
            out[k] = _clean(v, round_digits)
        return out
    if isinstance(obj, list):
        return [_clean(v, round_digits) for v in obj]
    if isinstance(obj, float):
        if abs(obj - round(obj)) < 1e-9:
            return int(round(obj))
        return round(obj, round_digits)
    if isinstance(obj, str) and _UUID_RE.match(obj):
        return "<uuid>"
    return obj


def _canonical_hash(obj, round_digits=6):
    text = json.dumps(_clean(obj, round_digits), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))
    return _sha256_text(text)


def _material_view(m, level=0):
    if not isinstance(m, dict):
        return _clean(m)
    view = _clean(m)
    # 复合片段：内层时间线递归；原始内层结构不再重复进摘要
    inner = m.get("draft")
    view.pop("draft", None)
    if isinstance(inner, dict) and level < 4:
        view["draft_inner"] = canon_draft(inner, level + 1)
    return view


def canon_draft(draft, level=0):
    """草稿的语义视图：素材按签名排序，segment 用下标引用素材。"""
    if not isinstance(draft, dict):
        return {}
    mats = draft.get("materials") or {}
    sigs = []
    id_index = {}
    if isinstance(mats, dict):
        for _cat, items in mats.items():
            if not isinstance(items, list):
                continue
            for m in items:
                if not isinstance(m, dict):
                    continue
                sig = _material_view(m, level)
                sigs.append(sig)
                if m.get("id"):
                    id_index[m["id"]] = sig
    sig_texts = [json.dumps(_clean(s), ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")) for s in sigs]
    ordered = sorted(set(sig_texts))
    sig_pos = {s: i for i, s in enumerate(ordered)}

    tracks_out = []
    for t in (draft.get("tracks") or []):
        if not isinstance(t, dict):
            continue
        segs = []
        for s in (t.get("segments") or []):
            if not isinstance(s, dict):
                continue
            seg = {k: v for k, v in s.items() if k != "material_id"}
            mid = s.get("material_id")
            if mid and mid in id_index:
                key = json.dumps(_clean(id_index[mid]), ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
                seg["material_sig"] = sig_pos.get(key, -1)
            else:
                seg["material_sig"] = None
            segs.append(_clean(seg))
        track = {
            "type": t.get("type"),
            "segments": segs,
        }
        tracks_out.append(track)
    tracks_out.sort(key=lambda x: json.dumps(x, ensure_ascii=False,
                                             sort_keys=True, separators=(",", ":")))
    return {
        "duration": _clean(draft.get("duration")),
        "canvas": _clean(draft.get("canvas_config")),
        "materials": ordered,
        "tracks": tracks_out,
    }


def weak_draft(draft):
    """弱视图：仅在数值精度上更宽松，用于识别剪映的序列化噪音。"""
    return canon_draft(draft)


def draft_hash(draft):
    return _canonical_hash(canon_draft(draft), 6)


def draft_weak_hash(draft):
    return _canonical_hash(weak_draft(draft), 3)


def file_pixel_hash(path):
    """图片像素归一 hash；PIL 不可用时退回文件字节 hash。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB").resize((64, 64))
            return _sha256_bytes(im.tobytes())
    except Exception:
        try:
            return sha256_file(path)
        except Exception:
            return None


# ━━━━━━━━━━━━━━━━ job.json 读写（不走 validate） ━━━━━━━━━━━━━━━━

def default_freshness():
    return {
        "schema": 1,
        "canon": CANON,
        "enabled": True,
        "ignore": [],
        "edges": {
            EDGE_E1: {"state": "unknown", "input_hash": None,
                      "output_hash": None, "weak_hash": None, "draft": None,
                      "recorded_at": None, "last_writer": None,
                      "reason": None, "detail": None, "accepted_as_is": None},
            EDGE_E2: {"state": "unknown", "input_hash": None, "output": None,
                      "recorded_at": None, "reason": None,
                      "provenance_file": None, "version": None,
                      "versions": {}},
            EDGE_E3: {"state": "unknown", "input_hash": None,
                      "output_hash": None, "recorded_at": None,
                      "reason": None, "version": None, "versions": {}},
        },
        "manual_confirmed": {},
    }


def _ensure(job):
    if not isinstance(job.get("freshness"), dict):
        job["freshness"] = default_freshness()
    fr = job["freshness"]
    fr.setdefault("schema", 1)
    fr.setdefault("canon", CANON)
    fr.setdefault("enabled", True)
    fr.setdefault("ignore", [])
    fr.setdefault("manual_confirmed", {})
    edges = fr.setdefault("edges", {})
    template = default_freshness()["edges"]
    for e, val in template.items():
        if not isinstance(edges.get(e), dict):
            edges[e] = deepcopy(val)
        else:
            for k, v in val.items():
                edges[e].setdefault(k, v)
    return fr


def _enabled(job):
    if str(os.environ.get("JY_FRESHNESS", "")).strip().lower() in ("0", "off", "false", "no"):
        return False
    try:
        return bool((job.get("freshness") or {}).get("enabled", True))
    except Exception:
        return True


def _record(job_dir, mutate, event=None, detail=None):
    """读 → 改 freshness → revision+1 → 原子写。失败只返回 False。"""
    try:
        path = _job_json(job_dir)
        if not os.path.isfile(path):
            return False
        job = _read_json(path)
        fr = _ensure(job)
        mutate(job, fr)
        job["revision"] = int(job.get("revision") or 1) + 1
        job["updated_at"] = _now_iso()
        if event:
            job.setdefault("history", []).append(
                {"event": event, "at": _now_iso(), "detail": detail})
            job["history"] = job["history"][-200:]
        _write_json_atomic(path, job)
        return True
    except Exception as e:
        print(f"  [warn] 新鲜度写回失败（不影响主流程）: {e}")
        return False


def _append_log(job_dir, line):
    try:
        path = os.path.join(_publish_dir(job_dir), "freshness_writes.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        # 简单轮转：超过 2000 行保留最近 2000 行
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 2000:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(lines[-2000:])
            os.replace(tmp, path)
    except Exception:
        pass


def _read_log(job_dir, limit=2000):
    path = os.path.join(_publish_dir(job_dir), "freshness_writes.jsonl")
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f.readlines()[-limit:]:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def _log_has(job_dir, edge, hash_value, field="output_hash"):
    if not hash_value:
        return False
    for rec in _read_log(job_dir):
        if rec.get("edge") == edge and rec.get(field) == hash_value:
            return True
    return False


# ━━━━━━━━━━━━━━━━ 输入指纹 ━━━━━━━━━━━━━━━━

def _text_hash(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        return _sha256_text("\n".join(lines))
    except Exception:
        return None


def _titles_view(path):
    try:
        d = _read_json(path)
    except Exception:
        return None
    picked = {
        "gate": d.get("gate"),
        "selected": [{"slot": x.get("slot"), "title_id": x.get("title_id"),
                      "title": x.get("title")}
                     for x in (d.get("selected") or []) if isinstance(x, dict)],
        "selected_groups": [{"group": x.get("group"), "picks": x.get("picks")}
                            for x in (d.get("selected_groups") or [])
                            if isinstance(x, dict)],
        "prompt_version": d.get("prompt_version"),
    }
    return _canonical_hash(picked)


def _prompts_view(path):
    try:
        d = _read_json(path)
    except Exception:
        return None
    items = []
    for x in (d.get("items") or []):
        if not isinstance(x, dict):
            continue
        items.append({k: x.get(k) for k in
                      ("group", "slot", "title_id", "title", "composition",
                       "size", "prompt")})
    return _canonical_hash({"spec_version": d.get("spec_version"),
                            "channel": d.get("channel"),
                            "count": d.get("count"),
                            "items": items})


def _profiles(job):
    raw = ((job.get("workflow") or {}).get("preset_profile")
           or "default")
    if str(raw).strip().lower() == "default":
        raw = ""
    profiles = [p.strip() for p in str(raw).split(",") if p.strip()]
    return profiles or ["a"]


def _preset_plan(job):
    """返回 {profile: [(预设名, alpha), ...]} 与对应预设内容摘要。"""
    try:
        cfg = _read_json(os.path.join(_PIPELINE_DIR, "presets.json"))
    except Exception:
        return {}, {}
    default = cfg.get("default_preset_profile") or "presets_a"
    plans, digests = {}, {}
    for prof in _profiles(job):
        key = "presets_" + prof.lower()
        items = cfg.get(key)
        if not isinstance(items, list):
            items = cfg.get(default) if isinstance(cfg.get(default), list) else []
        pairs = []
        for it in items:
            if isinstance(it, str):
                pairs.append((it, None))
            elif isinstance(it, dict) and it.get("name"):
                pairs.append((it["name"], it.get("alpha")))
        plans[prof] = pairs
        for name, _a in pairs:
            if name in digests:
                continue
            p = os.path.join(_preset_root(), name, "preset_draft", "draft_content.json")
            try:
                digests[name] = draft_hash(_read_json(p))
            except Exception:
                digests[name] = None
    return plans, digests


def _media_from_draft(draft):
    """主轨媒体身份：basename + duration + size（不含路径/mtime）。"""
    out = []
    try:
        mats = (draft.get("materials") or {}).get("videos") or []
    except Exception:
        mats = []
    for m in mats:
        if not isinstance(m, dict):
            continue
        p = m.get("path")
        if not isinstance(p, str) or not p.strip():
            continue
        name = os.path.basename(p.replace("\\", "/"))
        size = None
        try:
            if os.path.isfile(p):
                size = os.path.getsize(p)
        except OSError:
            size = None
        out.append({"name": name, "duration": m.get("duration"), "size": size})
    out.sort(key=lambda x: (x["name"], str(x["duration"]), str(x["size"])))
    return out


def _media_view(job_dir, draft=None):
    """E1 的媒体清单：优先扫 05_video（与版本无关），退化到草稿主轨素材。

    05_video 是 job 级输入，用它可避免「重建 B 版导致 A 版 E1 变 stale」的跨版本串扰。
    """
    root = os.path.join(job_dir, "05_video")
    out = []
    if os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            for n in files:
                try:
                    out.append({"name": n,
                                "size": os.path.getsize(os.path.join(dirpath, n))})
                except OSError:
                    continue
    if out:
        out.sort(key=lambda x: (x["name"], str(x["size"])))
        return out
    return _media_from_draft(draft) if draft is not None else []


def compute_input_hash(job_dir, edge, version=None, draft=None):
    try:
        job = _read_json(_job_json(job_dir))
    except Exception:
        return None
    edge = EDGE_ALIAS.get(str(edge).lower(), edge)
    if edge == EDGE_E3:
        return _cover_input_hash(job_dir, version or job.get("_version") or "a")
    if edge != EDGE_E1:
        return None

    serial = {"series": job.get("series"), "sizes": job.get("sizes"),
              "workflow": {k: (job.get("workflow") or {}).get(k) for k in
                           ("mode", "cut_s", "preset_profile", "derive")}}
    payload = {"job": serial}
    # 提示词 / 标题 / 简介
    payload["titles"] = _titles_view(os.path.join(job_dir, "02_titles", "titles.json"))
    payload["titles_prompt"] = _text_hash(os.path.join(job_dir, "02_titles", "prompt.txt"))
    payload["prompts"] = _prompts_view(os.path.join(job_dir, "03_prompts", "prompts.json"))
    payload["prompts_md"] = _text_hash(os.path.join(job_dir, "03_prompts", "prompts.md"))
    payload["synopsis"] = _text_hash(os.path.join(job_dir, "01_input", "synopsis.txt"))
    # 预设
    plans, digests = _preset_plan(job)
    payload["presets"] = {p: [{"name": n, "alpha": a, "digest": digests.get(n)}
                              for n, a in pairs] for p, pairs in plans.items()}
    # 媒体（05_video 清单；目录为空时退化为主轨素材代理）
    payload["media"] = _media_view(job_dir, draft)
    return _canonical_hash(payload)


def _cover_input_hash(job_dir, version):
    try:
        import job_manifest as jm
        p = jm.resolve_profile_cover(job_dir, version)
    except Exception:
        p = None
    if not p or not os.path.isfile(p):
        return None
    return file_pixel_hash(p)


# ━━━━━━━━━━━━━━━━ 输出指纹 ━━━━━━━━━━━━━━━━

def _draft_dir(name):
    if not name:
        return None
    p = os.path.join(_drafts_root(), name)
    return p if os.path.isdir(p) else None


def _load_draft_from_dir(d):
    try:
        return _read_json(os.path.join(d, "draft_content.json"))
    except Exception:
        return None


def _cover_output_hash(draft_dir, draft):
    """草稿封面输出：cover 非空 + Resources/cover 下的图片像素 hash。"""
    if not isinstance(draft, dict) or not draft.get("cover"):
        return None
    rdir = os.path.join(draft_dir, "Resources", "cover")
    if not os.path.isdir(rdir):
        return None
    files = [os.path.join(rdir, n) for n in os.listdir(rdir)
             if os.path.isfile(os.path.join(rdir, n))]
    if len(files) != 1:
        return None
    return file_pixel_hash(files[0])


def _draft_dir_for_version(job, version):
    out = ((job.get("video") or {}).get("outputs") or {}).get(version) or {}
    name = out.get("draft") or (
        (job.get("video") or {}).get("draft_name") if version == "a" else None)
    return _draft_dir(name), name


def version_for_draft(job_dir, draft_name):
    """草稿名 → 版本名：按 job.video.outputs 反查，查不到按 a。"""
    if not job_dir or not draft_name:
        return "a"
    try:
        job = _read_json(_job_json(job_dir))
    except Exception:
        return "a"
    target = os.path.basename(os.path.normpath(str(draft_name)))
    for ver, out in (((job.get("video") or {}).get("outputs") or {}).items()):
        if os.path.basename(os.path.normpath(str((out or {}).get("draft") or ""))) == target:
            return _norm_version(ver)
    return "a"


def job_dir_for_draft(draft_path_or_name):
    """从草稿路径/草稿名反推 cover_job，找不到返回 None。

    只用于「脚本自己知道草稿、但不知道 job」的写入点（framefix / verify_fix 等）：
    先按 flow manifest 的 paths.source_dir，再从草稿主轨视频路径向上找 cover_job。
    """
    if not draft_path_or_name:
        return None
    raw = str(draft_path_or_name)
    ddir = raw if os.path.isdir(raw) else os.path.join(_drafts_root(), raw)
    if not os.path.isfile(os.path.join(ddir, "draft_content.json")):
        ddir = os.path.dirname(os.path.abspath(raw))
    name = os.path.basename(os.path.normpath(ddir))
    try:
        mf = os.path.join(_PIPELINE_DIR, "_stream_manifests", name + ".json")
        if os.path.isfile(mf):
            src = ((_read_json(mf).get("paths") or {}).get("source_dir") or "")
            jd = os.path.join(src, "cover_job") if src else None
            if jd and os.path.isfile(os.path.join(jd, "job.json")):
                return jd
    except Exception:
        pass
    try:
        draft = _read_json(os.path.join(ddir, "draft_content.json"))
        for m in ((draft.get("materials") or {}).get("videos") or []):
            p = m.get("path") if isinstance(m, dict) else None
            if not (p and os.path.isabs(str(p))):
                continue
            cur = os.path.dirname(str(p))
            for _ in range(4):
                jd = os.path.join(cur, "cover_job")
                if os.path.isfile(os.path.join(jd, "job.json")):
                    return jd
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
    except Exception:
        pass
    return None


# ━━━━━━━━━━━━━━━━ 记录写入 ━━━━━━━━━━━━━━━━

def _e3_record_for_version(fr, version):
    """按版本取 E3 证据槽（模式同 E2）。

    封面的输入/输出本身按版本区分；单槽会让"给 B 补封面基线"把 A 误判成
    cover_changed。旧 job.json 无 versions 时仍按平铺字段只读兼容。
    """
    e3 = fr["edges"][EDGE_E3]
    versions = e3.get("versions") if isinstance(e3.get("versions"), dict) else {}
    slot = versions.get(version)
    flat_version = e3.get("version")
    if isinstance(slot, dict) and slot.get("state") not in (None, "unknown"):
        return slot
    if flat_version in (None, "") or _norm_version(flat_version) == version:
        return e3
    return slot if isinstance(slot, dict) else {"state": "unknown"}


def _mark_e3_stale(job, fr, reason):
    """E3 置 stale。封面/意图变化对所有版本都成立，所以分槽与平铺一起标。"""
    e3 = fr["edges"][EDGE_E3]
    versions = e3.setdefault("versions", {})
    targets = set(((job.get("video") or {}).get("outputs") or {})) | set(versions)
    slot = {"state": "stale", "reason": reason, "recorded_at": _now_iso(),
            "input_hash": None, "output_hash": None}
    for v in targets:
        versions.setdefault(v, {}).update(dict(slot, version=v))
    flat_version = e3.get("version")
    if not targets or flat_version in (None, "") or \
            _norm_version(flat_version) in targets:
        e3.update(slot)


def _e2_record_for_version(fr, version):
    """按版本取 E2 证据槽。

    新版 job.json 在 `edges.e2.versions[<版本>]` 按版本分槽，避免 A/B 互相覆盖；
    平铺字段保留为最近一次写的兼容副本。旧 job.json 没有分槽（也没有 version
    字段）时仍按平铺字段判，保持原有行为。
    """
    e2 = fr["edges"][EDGE_E2]
    versions = e2.get("versions") if isinstance(e2.get("versions"), dict) else {}
    slot = versions.get(version)
    flat_version = e2.get("version")
    if isinstance(slot, dict) and slot.get("state") not in (None, "unknown"):
        return slot
    if flat_version in (None, "") or _norm_version(flat_version) == version:
        return e2
    return slot if isinstance(slot, dict) else {"state": "unknown"}


def _versions_for_draft_path(job, draft_path):
    """草稿路径 → 对应版本列表（按 job.video.outputs[*].draft 的目录名反查）。"""
    try:
        target = os.path.basename(os.path.normpath(
            os.path.dirname(os.path.abspath(draft_path))))
    except Exception:
        return []
    out = []
    for ver, ent in (((job.get("video") or {}).get("outputs") or {}).items()):
        name = (ent or {}).get("draft")
        if not name:
            continue
        if os.path.basename(os.path.normpath(str(name))) == target:
            out.append(_norm_version(ver))
    return out


def _mark_e2_stale(job, fr, version, reason, detail, draft_path=None):
    """只把受影响版本的 E2 槽置 stale；版本无法判定时保守地全部置 stale。"""
    e2 = fr["edges"][EDGE_E2]
    versions = e2.setdefault("versions", {})
    slot = {"state": "stale", "reason": reason, "detail": detail,
            "recorded_at": _now_iso(), "output": None, "provenance_file": None}
    targets = [version] if version else _versions_for_draft_path(job, draft_path)
    if not targets:
        targets = list(versions)  # 无法反查版本：全部分槽 + 平铺一起置 stale
    for v in targets:
        versions.setdefault(v, {}).update(dict(slot, version=v))
    flat_version = e2.get("version")
    if not targets or flat_version in (None, "") or \
            _norm_version(flat_version) in targets:
        e2.update(slot)


def record_draft_write(job_dir, draft_path, writer, adopted_external=False):
    """每次写 draft_content.json 后调用：记 E1 指纹，并把 E2 置 stale。

    job_dir 可为 None：按草稿名/主轨路径反推（framefix、verify_fix 这类
    只知道草稿的写入点用），仍找不到就静默 no-op。
    """
    try:
        if not job_dir:
            job_dir = job_dir_for_draft(draft_path)
        if not job_dir or not os.path.isfile(_job_json(job_dir)):
            return False
        draft = _read_json(draft_path)
        out_hash = draft_hash(draft)
        weak = draft_weak_hash(draft)
        in_hash = compute_input_hash(job_dir, EDGE_E1, draft=draft)
        dname = os.path.basename(os.path.dirname(os.path.abspath(draft_path)))
        _append_log(job_dir, {
            "at": _now_iso(), "edge": EDGE_E1, "writer": writer,
            "draft": dname, "input_hash": in_hash, "output_hash": out_hash,
            "weak_hash": weak, "adopted_external": bool(adopted_external),
        })

        def mutate(job, fr):
            e1 = fr["edges"][EDGE_E1]
            e1.update({"state": "valid", "input_hash": in_hash,
                       "output_hash": out_hash, "weak_hash": weak,
                       "draft": dname, "recorded_at": _now_iso(),
                       "last_writer": writer, "reason": None, "detail": None})
            _mark_e2_stale(job, fr, None, "draft_changed", f"writer={writer}",
                           draft_path=draft_path)
        _record(job_dir, mutate, event=None)
        return True
    except Exception as e:
        print(f"  [warn] 记录草稿写入失败: {e}")
        return False


def _ffprobe_info(path):
    try:
        ff = shutil.which("ffprobe")
        if not ff:
            return {}
        cp = subprocess.run(
            [ff, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,avg_frame_rate,width,height",
             "-show_entries", "format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        d = json.loads(cp.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        dur = (d.get("format") or {}).get("duration")
        return {
            "duration_us": int(float(dur) * 1_000_000) if dur else None,
            "nb_frames": int(st["nb_frames"]) if str(st.get("nb_frames", "")).isdigit() else None,
            "avg_frame_rate": st.get("avg_frame_rate"),
            "width": st.get("width"),
            "height": st.get("height"),
        }
    except Exception:
        return {}


def sidecar_path(export_file):
    return export_file + ".freshness.json"


def read_sidecar(export_file):
    p = sidecar_path(export_file) if export_file else None
    if not p or not os.path.isfile(p):
        return None
    try:
        return _read_json(p)
    except Exception:
        return None


def _write_sidecar(export_file, payload):
    p = sidecar_path(export_file)
    _write_json_atomic(p, payload)
    return p


def record_export(job_dir, version, draft_name, export_file, verify_rc=None,
                  compute_hash=True):
    """导出成功后调用：写 mp4 旁 sidecar + job E2 基线。"""
    try:
        version = _norm_version(version)
        export_file = os.path.abspath(export_file) if export_file else None
        if not export_file or not os.path.isfile(export_file):
            # 成品不存在：绝不写 valid 边（那会指向空文件），显式降级为
            # stale(output_missing)，让下游按"缺成品"处理。
            def mark_missing(job, fr):
                slot = {"state": "stale", "reason": "output_missing",
                        "recorded_at": _now_iso(), "output": None,
                        "version": version}
                fr["edges"][EDGE_E2].update(slot)
                fr["edges"][EDGE_E2].setdefault("versions", {})[version] = deepcopy(slot)
            _record(job_dir, mark_missing)
            return False
        ddir = _draft_dir(draft_name)
        draft = _load_draft_from_dir(ddir) if ddir else None
        dhash = draft_hash(draft) if draft else None
        ihash = compute_input_hash(job_dir, EDGE_E1, draft=draft) if draft else None
        chash = _cover_output_hash(ddir, draft) if draft else None
        size = os.path.getsize(export_file)
        vhash = None
        if compute_hash:
            try:
                print("  [freshness] 计算成品 sha256（一次性，可能较慢）...")
                vhash = sha256_file(export_file)
            except Exception as e:
                print(f"  [warn] 成品 hash 计算失败: {e}")
        info = _ffprobe_info(export_file)
        evidence = {
            "schema": 1, "canon": CANON, "version": version,
            "draft": draft_name, "draft_hash": dhash, "input_hash": ihash,
            "cover_hash": chash, "exported_at": _now_iso(),
            "video": {"file": os.path.basename(export_file), "size_bytes": size,
                      "sha256": vhash, **info},
            "state": "valid", "manual_confirmed": None,
            "verify_rc": verify_rc,
        }
        sc = None
        if _enabled_safe(job_dir):
            try:
                sc = _write_sidecar(export_file, evidence)
            except Exception as e:
                print(f"  [warn] sidecar 写入失败（不影响导出）: {e}")

        def mutate(job, fr):
            slot = {
                "state": "valid", "input_hash": dhash, "output": {
                    k: evidence["video"].get(k) for k in
                    ("size_bytes", "sha256", "duration_us", "nb_frames",
                     "avg_frame_rate", "width", "height")},
                "recorded_at": _now_iso(), "reason": None,
                "provenance_file": sc, "version": version,
            }
            e2 = fr["edges"][EDGE_E2]
            e2.update(slot)
            e2.setdefault("versions", {})[version] = deepcopy(slot)
        _record(job_dir, mutate, event=None)
        return True
    except Exception as e:
        print(f"  [warn] 记录导出失败: {e}")
        return False


def _enabled_safe(job_dir):
    try:
        return _enabled(_read_json(_job_json(job_dir)))
    except Exception:
        return True


def record_cover(job_dir, version, draft_dir, cover_image=None):
    """封面挂载后调用：记 E3 基线。"""
    try:
        version = _norm_version(version)
        if not job_dir or not os.path.isfile(_job_json(job_dir)):
            return False
        if cover_image is None:
            try:
                import job_manifest as jm
                cover_image = jm.resolve_profile_cover(job_dir, version)
            except Exception:
                cover_image = None
        in_hash = file_pixel_hash(cover_image) if cover_image and os.path.isfile(cover_image) else None
        draft = _load_draft_from_dir(draft_dir) if draft_dir else None
        out_hash = _cover_output_hash(draft_dir, draft) if draft else None

        def mutate(job, fr):
            slot = {
                "state": "valid", "input_hash": in_hash,
                "output_hash": out_hash, "recorded_at": _now_iso(),
                "reason": None, "version": version,
            }
            e3 = fr["edges"][EDGE_E3]
            e3.update(slot)
            e3.setdefault("versions", {})[version] = deepcopy(slot)
        _record(job_dir, mutate)
        return True
    except Exception as e:
        print(f"  [warn] 记录封面失败: {e}")
        return False


def record_cover_write(job_dir, draft_path, writer="cover_only"):
    """纯封面绑定后的写入留痕：E1 更新，E2 保持不动。

    与 record_draft_write 的唯一区别：封面绑定只改 `cover` 字段与封面 composition
    素材，时间线没变、渲染帧不变，因此**不得**把 E2 置 stale。E1 侧记一条
    `timeline_unchanged=true` 的写入日志，供 freshness_of(E2) 自愈识别。
    """
    try:
        if not job_dir:
            job_dir = job_dir_for_draft(draft_path)
        if not job_dir or not os.path.isfile(_job_json(job_dir)):
            return False
        draft = _read_json(draft_path)
        out_hash = draft_hash(draft)
        weak = draft_weak_hash(draft)
        in_hash = compute_input_hash(job_dir, EDGE_E1, draft=draft)
        dname = os.path.basename(os.path.dirname(os.path.abspath(draft_path)))
        _append_log(job_dir, {
            "at": _now_iso(), "edge": EDGE_E1, "writer": writer,
            "draft": dname, "input_hash": in_hash, "output_hash": out_hash,
            "weak_hash": weak, "timeline_unchanged": True,
        })

        def mutate(job, fr):
            fr["edges"][EDGE_E1].update({
                "state": "valid", "input_hash": in_hash,
                "output_hash": out_hash, "weak_hash": weak,
                "draft": dname, "recorded_at": _now_iso(),
                "last_writer": writer, "reason": None,
                "detail": "timeline_unchanged",
            })
            # E2 的 state/input_hash/output 原样保留，只记"封面在导出后改过"
            fr["edges"][EDGE_E2]["cover_only_since_export"] = _now_iso()
        _record(job_dir, mutate)
        return True
    except Exception as e:
        print(f"  [warn] 记录封面写入失败: {e}")
        return False


def mark_intent_changed(job_dir):
    """titles/prompts 重跑后：意图可能变了 → E1 stale(intent)，E3 等封面重生成。

    只标下游，不重建任何产物；E2 不动（时间线没变，已导出的成片仍有效）。
    """
    try:
        def mutate(job, fr):
            fr["edges"][EDGE_E1].update({
                "state": "stale", "reason": "intent", "recorded_at": _now_iso()})
            _mark_e3_stale(job, fr, "covers_pending_regeneration")
        return _record(job_dir, mutate)
    except Exception as e:
        print(f"  [warn] 标记意图变更失败: {e}")
        return False


def mark_covers_pending(job_dir, reason="covers_changed"):
    """covers 重跑后：E3 等 cover_bind，E1/E2 不动。"""
    try:
        def mutate(job, fr):
            _mark_e3_stale(job, fr, reason)
        return _record(job_dir, mutate)
    except Exception as e:
        print(f"  [warn] 标记封面待绑定失败: {e}")
        return False


def _iso_newer(a, b):
    """a 是否晚于 b（ISO 时间串；解析失败退化为字符串比较）。"""
    if not a:
        return False
    if not b:
        return True
    try:
        return datetime.fromisoformat(str(a)) > datetime.fromisoformat(str(b))
    except (TypeError, ValueError):
        return str(a) > str(b)


# ━━━━━━━━━━━━━━━━ 判定 ━━━━━━━━━━━━━━━━

def _load_draft_with_hash(ddir):
    """按 (路径, mtime_ns, size) 记忆化的 draft+强摘要。

    只为避免 watcher 每轮轮询都把整份草稿重算一遍；内容变了 mtime 必变，
    命中缓存不会改变判定结果（指纹仍只吃内容）。
    """
    if not ddir:
        return None, None
    p = os.path.join(ddir, "draft_content.json")
    try:
        st = os.stat(p)
        key = (p, st.st_mtime_ns, st.st_size)
        hit = _DRAFT_CACHE.get(key)
        if hit is not None:
            return hit
    except OSError:
        return None, None
    draft = _read_json(p)
    digest = draft_hash(draft)
    if len(_DRAFT_CACHE) > 8:
        _DRAFT_CACHE.clear()
    _DRAFT_CACHE[key] = (draft, digest)
    return draft, digest


def check(job_dir, edge, version=None):
    """单边判定，返回 state + evidence + reason。"""
    try:
        job = _read_json(_job_json(job_dir))
    except Exception:
        return {"state": "unknown", "reason": "no_job", "evidence": {}}
    if not _enabled(job):
        return {"state": "unknown", "reason": "disabled", "evidence": {}}
    fr = _ensure(job)
    if fr.get("canon") != CANON:
        return {"state": "unknown", "reason": "canon_mismatch", "evidence": {}}
    edge = EDGE_ALIAS.get(str(edge).lower(), edge)
    version = _norm_version(version)
    ddir, dname = _draft_dir_for_version(job, version)
    draft, _dhash = _load_draft_with_hash(ddir)

    if edge == EDGE_E1:
        # 人工确认优先（与 freshness_of 同口径）：确认过的版本不再按草稿 hash
        # 重算判 externally_modified；但产物 size 变了 / 成品缺失仍然失效。
        mc = (fr.get("manual_confirmed") or {}).get(version)
        if mc:
            out = ((job.get("video") or {}).get("outputs") or {}).get(version) or {}
            export_file = out.get("export_file")
            if export_file and not os.path.isabs(str(export_file)):
                export_file = os.path.join(
                    os.path.dirname(os.path.normpath(job_dir)), str(export_file))
            actual_size = None
            if export_file and os.path.isfile(export_file):
                try:
                    actual_size = os.path.getsize(export_file)
                except OSError:
                    actual_size = None
            ev = {"manual_confirmed": mc, "draft": dname,
                  "video": {"path": export_file, "size_bytes": actual_size}}
            if actual_size is not None and mc.get("size_bytes") in (None, actual_size):
                return {"state": "manual_confirmed", "reason": "manual_confirmed",
                        "evidence": ev}
            return {"state": "stale",
                    "reason": "manual_confirmed_artifact_changed", "evidence": ev}
        cur_in = compute_input_hash(job_dir, EDGE_E1, draft=draft)
        rec = fr["edges"][EDGE_E1]
        if cur_in is None or not rec.get("input_hash"):
            return {"state": "unknown", "reason": "no_input", "evidence": rec}
        if cur_in != rec.get("input_hash"):
            if _log_has(job_dir, EDGE_E1, cur_in, "input_hash"):
                return {"state": "valid", "reason": "write_log_hit", "evidence": rec}
            return {"state": "stale", "reason": "input_changed", "evidence": rec}
        if draft is None:
            return {"state": "stale", "reason": "draft_missing", "evidence": rec}
        cur_out = _dhash
        if cur_out == rec.get("output_hash"):
            return {"state": "valid", "reason": None, "evidence": rec}
        if _log_has(job_dir, EDGE_E1, cur_out):
            return {"state": "valid", "reason": "write_log_hit", "evidence": rec}
        if rec.get("weak_hash") and draft_weak_hash(draft) == rec.get("weak_hash"):
            return {"state": "valid", "reason": "serialization_only", "evidence": rec}
        return {"state": "stale", "reason": "externally_modified", "evidence": rec}

    if edge == EDGE_E3:
        cur_in = _cover_input_hash(job_dir, version)
        rec = _e3_record_for_version(fr, version)
        if cur_in is None or not rec.get("input_hash"):
            return {"state": "unknown", "reason": "no_cover", "evidence": rec}
        if cur_in != rec.get("input_hash"):
            return {"state": "stale", "reason": "cover_changed", "evidence": rec}
        cur_out = _cover_output_hash(ddir, draft) if draft else None
        if rec.get("output_hash") and cur_out and cur_out != rec.get("output_hash"):
            return {"state": "stale", "reason": "cover_not_attached", "evidence": rec}
        return {"state": "valid", "reason": None, "evidence": rec}

    return freshness_of(job_dir, version, consumer="scheduler")


def _export_recorded(job, version):
    """job.json 的导出成功台账里，这一版是否登记过 exported。

    只认 record_video 写的 video.outputs[<版本>]。老台账（没有 outputs，
    只有顶层 export_state/export_file）故意不算——那属于本系统上线前的产物，
    缺 sidecar 是正常的，按 legacy 处理。
    """
    outs = (job.get("video") or {}).get("outputs") or {}
    entry = outs.get(version)
    if not isinstance(entry, dict):
        return False
    return str(entry.get("state") or "").lower() == "exported"


# ━━━━━━━━━━━━━━━━ 一次性上线基线（legacy(baseline)） ━━━━━━━━━━━━━━━━
#
# sidecar 机制上线前已导出的产物没有 sidecar，但它们确实是我们导的。为了不把
# 这批历史产物误报成 evidence_missing，上线时冻结一份清单：命中 → legacy(baseline)，
# 只标注不告警；清单之外才报 evidence_missing。
# 清单只由 scripts/freshness_baseline.py 显式写入；运行期只读，绝不自动扩充。

BASELINE_ENV = "JY_FRESHNESS_BASELINE"
BASELINE_KIND = "freshness_legacy_baseline"
_BASELINE_CACHE = {"key": None, "data": None}


def baseline_path():
    env = str(os.environ.get(BASELINE_ENV) or "").strip()
    if env:
        return os.path.abspath(env)
    return os.path.join(_REPO_ROOT, "data", f"{BASELINE_KIND}.json")


def baseline_entries_digest(entries):
    return _sha256_text(json.dumps(entries, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")))


def load_baseline():
    """只读加载冻结清单。缺失/损坏/摘要不符一律 valid=False（不放行任何人）。"""
    path = baseline_path()
    try:
        st = os.stat(path)
    except OSError:
        return {"path": path, "valid": False, "reason": "baseline_missing",
                "entries": []}
    key = (path, st.st_mtime_ns, st.st_size)
    if _BASELINE_CACHE.get("key") == key:
        return _BASELINE_CACHE["data"]
    out = {"path": path, "valid": False, "reason": None, "entries": []}
    try:
        data = _read_json(path)
        entries = data.get("entries")
        if data.get("kind") != BASELINE_KIND:
            out["reason"] = "kind_mismatch"
        elif int(data.get("schema") or 0) != 1:
            out["reason"] = "schema_mismatch"
        elif not isinstance(entries, list):
            out["reason"] = "entries_not_list"
        elif baseline_entries_digest(entries) != data.get("entries_digest"):
            # 手改清单却没重新生成 → 整份失效。宁可误报，也不静默放行。
            out["reason"] = "digest_mismatch"
        else:
            out.update({"valid": True, "generated_at": data.get("generated_at"),
                        "command": data.get("command"),
                        "scanned_roots": data.get("scanned_roots"),
                        "entries": entries})
    except Exception as e:
        out["reason"] = f"read_error:{e}"
    _BASELINE_CACHE.update({"key": key, "data": out})
    return out


def _baseline_job_key(value):
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))
    except Exception:
        return str(value or "")


def baseline_lookup(job, job_dir, version, size_bytes=None):
    """命中冻结清单返回 entry，否则 None。只认 job 身份 + 版本 + 成品 size。"""
    if size_bytes is None:
        return None
    b = load_baseline()
    if not b.get("valid"):
        return None
    version = _norm_version(version)
    job_id = str(job.get("job_id") or "")
    jkey = _baseline_job_key(job_dir)
    for e in b.get("entries") or []:
        if not isinstance(e, dict) or not e.get("version"):
            continue
        if _norm_version(e.get("version")) != version:
            continue
        try:
            if e.get("size_bytes") is None or int(e["size_bytes"]) != int(size_bytes):
                continue
        except (TypeError, ValueError):
            continue
        if job_id and e.get("job_id"):
            if str(e["job_id"]) == job_id:
                return e
            continue  # 明确是另一个 job，不靠路径兜底
        if _baseline_job_key(e.get("job")) == jkey:
            return e
    return None


def freshness_of(job_dir, version, consumer="scheduler"):
    """唯一消费入口：三态 + 证据 + 消费策略结论。"""
    version = _norm_version(version)
    res = {"version": version, "state": "unknown", "allowed": None,
           "reason": None, "evidence": {}, "disabled": False}
    try:
        job = _read_json(_job_json(job_dir))
    except Exception:
        res["reason"] = "no_job"
        res["allowed"] = None if consumer == "scheduler" else True
        res["state"] = "unknown" if consumer == "scheduler" else "legacy"
        return res
    if not _enabled(job):
        res["disabled"] = True
        res["reason"] = "disabled"
        return res
    fr = _ensure(job)
    if fr.get("canon") != CANON:
        res["reason"] = "canon_mismatch"
        return res

    out = ((job.get("video") or {}).get("outputs") or {}).get(version) or {}
    export_file = out.get("export_file")
    if export_file and not os.path.isabs(str(export_file)):
        export_file = os.path.join(os.path.dirname(os.path.normpath(job_dir)),
                                   str(export_file))
    ddir, dname = _draft_dir_for_version(job, version)
    actual_size = None
    if export_file and os.path.isfile(export_file):
        try:
            actual_size = os.path.getsize(export_file)
        except OSError:
            actual_size = None

    # 人工确认优先：覆盖出身
    mc = (fr.get("manual_confirmed") or {}).get(version)
    if mc:
        same = actual_size is not None and mc.get("size_bytes") in (None, actual_size)
        if same:
            res.update({"state": "manual_confirmed", "allowed": True,
                        "reason": "manual_confirmed",
                        "evidence": {"manual_confirmed": mc, "draft": dname,
                                     "video": {"path": export_file,
                                               "size_bytes": actual_size}}})
            return res
        res.update({"state": "stale", "reason": "manual_confirmed_artifact_changed",
                    "evidence": {"manual_confirmed": mc, "draft": dname,
                                 "video": {"path": export_file,
                                           "size_bytes": actual_size}}})
        res["allowed"] = True if consumer in ("archive", "clean") else None
        return res

    # 草稿在导出后又被改过：_export_stale.json 是硬信号
    marker = os.path.join(ddir, "_export_stale.json") if ddir else None
    marker_hit = bool(marker and os.path.isfile(marker))

    sc = read_sidecar(export_file) if export_file else None
    rec = _e2_record_for_version(fr, version)

    # 封面专用写入自愈：草稿 hash 变了，但变的是 timeline_unchanged=true 的
    # 封面绑定（且晚于本次导出）→ 时间线未变，E2 仍成立，不判 draft_changed。
    cover_only_at = None
    if ddir:
        _draft_obj, _dh = _load_draft_with_hash(ddir)
        if _dh:
            for _w in reversed(_read_log(job_dir)):
                if (_w.get("edge") == EDGE_E1
                        and _w.get("output_hash") == _dh
                        and _w.get("timeline_unchanged") is True):
                    cover_only_at = _w.get("at")
                    break
    cover_only = _iso_newer(
        cover_only_at,
        (sc.get("exported_at") if sc else None) or rec.get("recorded_at"))
    if sc:
        video_ev = dict(sc.get("video") or {})
        video_ev.setdefault("path", export_file)
        ev = {"provenance_file": sidecar_path(export_file), "sidecar": sc,
              "draft": sc.get("draft") or dname,
              "draft_hash": sc.get("draft_hash"),
              "input_hash": sc.get("input_hash"),
              "cover_hash": sc.get("cover_hash"),
              "exported_at": sc.get("exported_at"),
              "manual_confirmed": sc.get("manual_confirmed"),
              "video": video_ev}
        if not (export_file and os.path.isfile(export_file)):
            res.update({"state": "stale", "reason": "output_missing", "evidence": ev})
        elif marker_hit:
            res.update({"state": "stale", "reason": "draft_changed", "evidence": ev})
        elif str(sc.get("version")) not in ("", version):
            res.update({"state": "stale", "reason": "version_mismatch", "evidence": ev})
        elif actual_size is not None and sc.get("video", {}).get("size_bytes") != actual_size:
            res.update({"state": "stale", "reason": "output_changed", "evidence": ev})
        elif sc.get("state") != "valid":
            res.update({"state": str(sc.get("state")), "reason": "sidecar_state",
                        "evidence": ev})
        elif cover_only:
            res.update({"state": "valid", "reason": "cover_only", "evidence": ev})
        else:
            res.update({"state": "valid", "evidence": ev})
    elif rec.get("state") == "valid" and actual_size is not None and \
            (rec.get("output") or {}).get("size_bytes") in (None, actual_size):
        ev = {"record": rec, "draft": dname,
              "video": dict(rec.get("output") or {}, path=export_file)}
        if marker_hit:
            res.update({"state": "stale", "reason": "draft_changed", "evidence": ev})
        elif cover_only:
            res.update({"state": "valid", "reason": "cover_only", "evidence": ev})
        else:
            res.update({"state": "valid", "evidence": ev})
    elif rec.get("state") == "stale":
        if cover_only:
            res.update({"state": "valid", "reason": "cover_only",
                        "evidence": {"record": rec, "draft": dname,
                                     "cover_only": cover_only_at}})
        else:
            res.update({"state": "stale", "reason": rec.get("reason") or "stale",
                        "evidence": {"record": rec, "draft": dname}})
    elif rec.get("state") == "valid":
        res.update({"state": "stale", "reason": "output_missing" if actual_size is None
                    else "output_changed",
                    "evidence": {"record": rec, "draft": dname,
                                 "video": dict(rec.get("output") or {},
                                               path=export_file)}})
    else:
        if _export_recorded(job, version):
            if actual_size is None:
                # 登记过导出，但成品文件已不在：先按缺失处理（可被重导）
                res.update({"state": "stale", "reason": "output_missing",
                            "evidence": {"draft": dname,
                                         "video": {"path": export_file}}})
            else:
                base = baseline_lookup(job, job_dir, version, actual_size)
                if base is not None:
                    # 上线前导出的历史产物：命中冻结清单 → legacy(baseline)。
                    # 只标注、不告警；清单只由显式命令维护，不随目录变化扩充。
                    res.update({"state": "legacy", "reason": "baseline",
                                "evidence": {"draft": dname,
                                             "video": {"path": export_file,
                                                       "size_bytes": actual_size},
                                             "baseline": base}})
                else:
                    # 关键区分：登记过导出成功却没有 sidecar —— 是我们漏写，不是老产物。
                    # 不降级成 legacy，必须让日志/快照/墓碑都能看到 evidence_missing。
                    res.update({"state": "evidence_missing",
                                "reason": "export_recorded_no_sidecar",
                                "evidence": {"draft": dname,
                                             "video": {"path": export_file,
                                                       "size_bytes": actual_size}}})
                    _warn_once(
                        ("evidence_missing", job_dir, version),
                        f"[freshness][evidence_missing] {job_dir} 版本 {version}: "
                        f"job 已登记导出成功，但成品旁缺少 {sidecar_path(export_file)}；"
                        f"不按 legacy 放行（归档/clean 会标注 evidence_missing）。")
        else:
            res.update({"state": "unknown", "reason": "no_evidence",
                        "evidence": {"draft": dname}})

    if res["state"] == "valid":
        res["allowed"] = True
    elif res["state"] in ("stale", "evidence_missing"):
        res["allowed"] = True if consumer in ("archive", "clean") else None
    else:
        if consumer in ("archive", "clean"):
            res["state"] = "legacy"
            res["allowed"] = True
            res["reason"] = res.get("reason") or "no_evidence"
        else:
            res["allowed"] = None
    return res


# ━━━━━━━━━━━━━━━━ 人工兜底 ━━━━━━━━━━━━━━━━

def _edge_list(edges):
    if edges in (None, "all"):
        return [EDGE_E1, EDGE_E2, EDGE_E3]
    out = []
    for e in str(edges).split(","):
        key = EDGE_ALIAS.get(e.strip().lower(), e.strip())
        if key in (EDGE_E1, EDGE_E2, EDGE_E3):
            out.append(key)
    return out or [EDGE_E1, EDGE_E2, EDGE_E3]


def mark_stale(job_dir, edges="all", reason=""):
    def mutate(job, fr):
        for e in _edge_list(edges):
            fr["edges"][e].update({"state": "stale", "reason": "manual_mark",
                                   "detail": reason or "manual"})
    return _record(job_dir, mutate, event="freshness_mark_stale",
                   detail={"edges": _edge_list(edges), "reason": reason})


def reuse_as_is(job_dir, edges="all", version=None, why=""):
    """人工确认：覆盖出身，记 manual_confirmed（谁/何时/为什么）。"""
    try:
        job = _read_json(_job_json(job_dir))
    except Exception:
        return False
    version = _norm_version(version)
    out = ((job.get("video") or {}).get("outputs") or {}).get(version) or {}
    export_file = out.get("export_file")
    if export_file and not os.path.isabs(str(export_file)):
        export_file = os.path.join(os.path.dirname(os.path.normpath(job_dir)),
                                   str(export_file))
    size = None
    vhash = None
    if export_file and os.path.isfile(export_file):
        try:
            size = os.path.getsize(export_file)
            vhash = sha256_file(export_file)
        except Exception:
            pass
    by = os.environ.get("USERNAME") or getpass.getuser()
    mc = {"by": by, "at": _now_iso(), "why": why or "人工确认这版可用",
          "size_bytes": size, "video_sha256": vhash}

    def mutate(j, fr):
        fr.setdefault("manual_confirmed", {})[version] = mc
        for e in _edge_list(edges):
            fr["edges"][e].update({"state": "valid", "reason": None,
                                   "detail": "manual_confirmed"})
    ok = _record(job_dir, mutate, event="freshness_reuse_as_is",
                 detail={"version": version, "why": mc["why"]})
    if ok and export_file:
        sc = read_sidecar(export_file) or {}
        sc.update({"schema": 1, "canon": CANON, "version": version,
                   "state": "manual_confirmed", "manual_confirmed": mc})
        try:
            _write_sidecar(export_file, sc)
        except Exception as e:
            print(f"  [warn] 更新 sidecar 失败: {e}")
    return ok


# ━━━━━━━━━━━━━━━━ 归档 / clean 支撑 ━━━━━━━━━━━━━━━━

def archive_snapshot(job_dir, versions=None, write=True, allow_stale_reason=None):
    """归档搬运前的判定快照：记录判定，不在这里阻断（阻断由 archive_job 做）。"""
    try:
        job = _read_json(_job_json(job_dir))
    except Exception:
        return {}
    if not _enabled(job):
        return {}
    outs = ((job.get("video") or {}).get("outputs") or {})
    versions = versions or list(outs) or ["a"]
    snap = {"at": _now_iso(), "versions": {}}
    for v in versions:
        snap["versions"][v] = freshness_of(job_dir, v, consumer="archive")
    # 反向检测汇总：登记过导出却没有 sidecar 的版本单独列出，归档时一眼可见
    snap["evidence_missing"] = [v for v, d in snap["versions"].items()
                                if d.get("state") == "evidence_missing"]
    snap["stale"] = [v for v, d in snap["versions"].items()
                     if d.get("state") == "stale"]
    # 人工放行 stale/evidence_missing 时把理由一起固化，便于事后追溯
    if allow_stale_reason:
        snap["allow_stale_reason"] = allow_stale_reason
    # 命中上线基线的历史产物：只标注，不算问题
    snap["legacy_baseline"] = [v for v, d in snap["versions"].items()
                               if d.get("reason") == "baseline"]
    if write:
        try:
            p = os.path.join(_publish_dir(job_dir), "freshness_archive_snapshot.json")
            _write_json_atomic(p, snap)
        except Exception as e:
            print(f"  [warn] 归档新鲜度快照写入失败: {e}")
    return snap


def write_tombstone(job_dir, version, status="deleting"):
    """删成品前写墓碑；返回墓碑路径（失败 None）。"""
    try:
        version = _norm_version(version)
        res = freshness_of(job_dir, version, consumer="clean")
        if res.get("disabled"):
            return None
        ev = res.get("evidence") or {}
        if res.get("state") == "evidence_missing":
            print(f"  [warn] 版本 {version} 已登记导出但缺 sidecar，"
                  f"墓碑标注 evidence_missing")
        video = ev.get("video") or (ev.get("sidecar") or {}).get("video") or {}
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        d = os.path.join(_publish_dir(job_dir), "freshness_tombstones")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{version}_{ts}.json")
        job = _read_json(_job_json(job_dir)) if os.path.isfile(_job_json(job_dir)) else {}
        rec = {
            "schema": 1, "canon": CANON, "version": version,
            "draft": ev.get("draft") or
                     (((job.get("video") or {}).get("outputs") or {}).get(version) or {})
                     .get("draft"),
            "draft_hash": ev.get("draft_hash"),
            "input_hash": ev.get("input_hash"),
            "cover_hash": ev.get("cover_hash"),
            "video": {"path": video.get("path") or video.get("file"),
                      "size_bytes": video.get("size_bytes"),
                      "sha256": video.get("sha256"),
                      "duration_us": video.get("duration_us")},
            "state": res.get("state"), "reason": res.get("reason"),
            "manual_confirmed": ev.get("manual_confirmed"),
            "status": status, "at": _now_iso(), "deleted_at": None,
        }
        _write_json_atomic(path, rec)
        return path
    except Exception as e:
        print(f"  [warn] 墓碑写入失败: {e}")
        return None


def update_tombstone(path, status):
    if not path or not os.path.isfile(path):
        return False
    try:
        rec = _read_json(path)
        rec["status"] = status
        rec["deleted_at"] = _now_iso() if status == "deleted" else None
        _write_json_atomic(path, rec)
        return True
    except Exception:
        return False


# ━━━━━━━━━━━━━━━━ CLI ━━━━━━━━━━━━━━━━

def _parse(argv):
    opt = {"job": None, "edge": "all", "mark": False, "reason": "",
           "reuse": False, "check": False, "json": False, "version": None,
           "explain": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--job", "-j") and i + 1 < len(argv):
            i += 1
            opt["job"] = argv[i]
        elif a == "--edge" and i + 1 < len(argv):
            i += 1
            opt["edge"] = argv[i]
        elif a == "--version" and i + 1 < len(argv):
            i += 1
            opt["version"] = argv[i]
        elif a in ("--mark-stale", "--mark_stale"):
            opt["mark"] = True
        elif a == "--reason" and i + 1 < len(argv):
            i += 1
            opt["reason"] = argv[i]
        elif a == "--reuse-as-is":
            opt["reuse"] = True
        elif a == "--check":
            opt["check"] = True
        elif a == "--explain":
            opt["check"] = True
            opt["explain"] = True
        elif a == "--json":
            opt["json"] = True
        i += 1
    return opt


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    opt = _parse(argv)
    job = opt["job"]
    if not job:
        print("用法: python lib/freshness.py --job <cover_job 目录> "
              "[--check [--json]] | [--mark-stale --reason ...] | [--reuse-as-is]")
        return 2
    job = os.path.abspath(job)
    if opt["mark"]:
        ok = mark_stale(job, opt["edge"], opt["reason"])
        print("已标记 stale" if ok else "标记失败")
        return 0 if ok else 1
    if opt["reuse"]:
        ok = reuse_as_is(job, opt["edge"], opt["version"], opt["reason"])
        print("已记为 manual_confirmed" if ok else "确认失败")
        return 0 if ok else 1
    versions = [opt["version"]] if opt["version"] else None
    if versions is None:
        try:
            versions = list(((_read_json(_job_json(job)).get("video") or {})
                             .get("outputs") or {})) or ["a"]
        except Exception:
            versions = ["a"]
    data = {v: freshness_of(job, v, consumer="scheduler") for v in versions}
    data["_edges"] = {e: check(job, e) for e in (EDGE_E1, EDGE_E2, EDGE_E3)}
    if opt["json"]:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for v, r in data.items():
            if v.startswith("_"):
                continue
            print(f"[{v}] {r['state']}  allowed={r['allowed']}  reason={r['reason']}")
        print("三边:", {e: d["state"] for e, d in data["_edges"].items()})
    if opt["explain"]:
        print(json.dumps({e: d.get("evidence") for e, d in data["_edges"].items()},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
