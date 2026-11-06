"""短剧任务目录与 job.json 的唯一实现（Handoff Contract v1.1）。

只依赖标准库。被 drama-cover-builder 的 `prepare` 子命令调用，也可被
发布 skill / pipeline 直接 import，避免每个阶段各写一套目录约定。

契约见 .trae/skills/drama-cover-builder/references/handoff-contract.md
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import struct
import sys
from datetime import datetime
from typing import Any, Optional

SCHEMA_VERSION = "1.1"

# 全阶段必需目录（prepared 起就必须存在）
BASE_DIRS = (
    "01_input",
    "02_titles",
    "03_prompts",
    "04_covers",
    "05_video",
    "06_publish",
)

# 发布阶段目录：prepare 仍预建作占位（契约 §1「一次建全」），
# 但校验只在 publish_ready / published 阶段要求存在——缺失不阻断前序阶段。
PUBLISH_DIRS = (
    "06_publish/receipts",
)

# prepare 建剧时预建的完整目录集（含发布阶段占位目录）
JOB_DIRS = BASE_DIRS + PUBLISH_DIRS

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
EXCLUDE_TOKENS = ("draft_cover", "best_frame", "监审", "monitor", "提示词", "prompt")
EXACT_COVER_STEMS = ("封面", "cover")

SYNOPSIS_NAMES = ("简介.txt", "synopsis.txt", "剧情简介.txt", "故事简介.txt", "详细简介.txt")

STAGES = (
    "prepared",
    "titles_done",
    "titles_selected",
    "prompts_ready",
    "covers_ready",
    "video_ready",
    "publish_ready",
    "published",
    "archived",
)

# 兼容第三方 agent 的别名写法（收口时统一映射到契约值）
STAGE_ALIASES = {
    "covers_done": "covers_ready",
    "covers_complete": "covers_ready",
    "videos_done": "video_ready",
    "video_done": "video_ready",
}


def canon_stage(stage: Any) -> Any:
    """把非契约 stage 别名映射成契约值；未知值原样返回。"""
    if isinstance(stage, str):
        key = stage.strip().lower()
        return STAGE_ALIASES.get(key, stage)
    return stage


# 调度意图契约：豆包写 dispatch.json 声明，Trae ingest 归并进 job.json.workflow
DISPATCH_NAME = "dispatch.json"
WORKFLOW_MODES = ("full", "edit_only")
WORKFLOW_DEFAULTS: dict[str, Any] = {
    "mode": "full",          # full=剪辑+导出；edit_only=只剪辑停在草稿
    "cut_s": None,           # 正数秒裁剪；None/0=不裁
    "derive": [],            # 派生平台列表；[]=不派生
    "priority": 0,           # 调度优先级（大者优先）
    "intent_revision": 0,    # 仅人工 +1，任何程序路径只读不写
    "intent_source": "default",  # default | dispatch | manual
    "merged_at": None,
}
DERIVE_ALIASES = {"ig": "instagram-reels", "facebook": "facebook-reels"}

COVER_SLOTS = (
    (1, "爆点仰拍", "cover_01_hit.png"),
    (2, "冷暖对比", "cover_02_contrast.png"),
    (3, "守护挡身", "cover_03_guard.png"),
)

VERTICAL_SLOT = ("vertical", "胸像逆光", "cover_04_vertical.png")

CLEANUP_KEEP = [
    "job.json",
    "01_input/synopsis.txt",
    "02_titles",
    "03_prompts",
    "04_covers",
    "05_video",
]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sanitize_name(name: str, fallback: str = "未命名剧集") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def rel_path(path: str, base: str) -> str:
    """相对 job 目录的路径，统一使用正斜杠。"""
    return os.path.relpath(path, base).replace(os.sep, "/")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def natural_key(text: str) -> list[Any]:
    """自然排序：第1集 < 第2集 < 第10集。"""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def _iter_images(series_dir: str) -> list[str]:
    names = []
    for name in os.listdir(series_dir):
        path = os.path.join(series_dir, name)
        if os.path.isfile(path) and os.path.splitext(name)[1].lower() in IMAGE_EXTS:
            names.append(name)
    return names


def _is_excluded(name: str) -> bool:
    low = name.lower()
    if low.startswith("cover_") and re.match(r"cover_0\d", low):
        return True
    return any(tok in low for tok in EXCLUDE_TOKENS)


def detect_reference_image(series_dir: str) -> dict[str, Optional[str]]:
    """按契约 §5 的优先级挑选参考图。"""
    if not series_dir or not os.path.isdir(series_dir):
        return {"path": None, "source": None, "reason": "剧集目录不存在"}

    names = _iter_images(series_dir)
    if not names:
        return {"path": None, "source": None, "reason": "目录内没有图片"}

    for name in names:
        stem, _ = os.path.splitext(name)
        if stem.strip().lower() in EXACT_COVER_STEMS and not _is_excluded(name):
            return {"path": os.path.join(series_dir, name), "source": name,
                    "reason": "文件名精确命中封面"}

    hits = [n for n in names
            if not _is_excluded(n) and ("封面" in n or "cover" in n.lower())]
    if hits:
        name = sorted(hits, key=natural_key)[0]
        return {"path": os.path.join(series_dir, name), "source": name,
                "reason": "文件名含「封面」"}

    rest = [n for n in names if not _is_excluded(n)]
    if rest:
        name = sorted(rest, key=natural_key)[0]
        return {"path": os.path.join(series_dir, name), "source": name,
                "reason": "自然排序第一张（无封面命名）"}

    return {"path": None, "source": None, "reason": "候选图全部被排除规则过滤"}


def detect_synopsis(series_dir: str) -> dict[str, Any]:
    """探测剧集目录内的简介文件。"""
    if not series_dir or not os.path.isdir(series_dir):
        return {"path": None, "candidates": []}

    found = [os.path.join(series_dir, n) for n in SYNOPSIS_NAMES
             if os.path.isfile(os.path.join(series_dir, n))]
    if not found:
        found = [os.path.join(series_dir, n) for n in sorted(os.listdir(series_dir))
                 if n.lower().endswith(".txt") and "简介" in n]
    return {"path": found[0] if found else None, "candidates": found}


def detect_video(series_dir: str) -> dict[str, Any]:
    """探测剧集目录顶层的 mp4；只有唯一一个时才定为 video.path。"""
    if not series_dir or not os.path.isdir(series_dir):
        return {"path": None, "count": 0, "pattern": None}

    mp4s = [n for n in os.listdir(series_dir)
            if n.lower().endswith(".mp4") and os.path.isfile(os.path.join(series_dir, n))]
    if not mp4s:
        return {"path": None, "count": 0, "pattern": None}

    pattern = None
    if all(re.search(r"第\d+集", n) for n in mp4s):
        pattern = "第N集.mp4"
    elif len(mp4s) > 1:
        pattern = "*.mp4"

    return {
        "path": os.path.join(series_dir, mp4s[0]) if len(mp4s) == 1 else None,
        "count": len(mp4s),
        "pattern": pattern,
    }


def default_cover_map() -> list[dict[str, Any]]:
    return [
        {
            "slot": slot,
            "kind": "horizontal",
            "composition": composition,
            "title_id": f"T{slot}",
            "title": None,
            "file": f"04_covers/{filename}",
            "prompt": None,
            "channel": None,
            "image_sha256": None,
            "status": "pending",
            "generated_at": None,
        }
        for slot, composition, filename in COVER_SLOTS
    ]


def default_vertical_cover() -> dict[str, Any]:
    slot, composition, filename = VERTICAL_SLOT
    return {
        "slot": slot,
        "kind": "vertical",
        "role": "video_cover",
        "composition": composition,
        "title_id": None,
        "title": None,
        "file": f"04_covers/{filename}",
        "prompt": None,
        "channel": None,
        "image_sha256": None,
        "status": "pending",
        "generated_at": None,
    }


def default_cleanup() -> dict[str, Any]:
    return {
        "status": "blocked",
        "eligible": False,
        "reason": "尚未完成发布",
        "requires_confirm": True,
        "targets": ["01_input/reference.*"],
        "keep": list(CLEANUP_KEEP),
        "external": [],
        "executed_at": None,
        "deleted": [],
    }


def build_job(series: str, *, host: str = "trae") -> dict[str, Any]:
    stamp = datetime.now().astimezone()
    slug = sanitize_name(series)
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": f"{slug}-{stamp.strftime('%Y%m%d-%H%M%S')}",
        "series": series,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "revision": 1,
        "host": host,
        "stage": "prepared",
        "input": {
            "synopsis": "01_input/synopsis.txt",
            "synopsis_source": None,
            "synopsis_candidates": [],
            "reference_image": None,
            "reference_original": None,
            "reference_source": None,
            "reference_reason": None,
            "reference_sha256": None,
        },
        "sizes": {"horizontal": [2848, 1600], "vertical": [1080, 1920]},
        "titles": {
            "file": "02_titles/titles.json",
            "count": 12,
            "gate": "pending",
            "prompt_file": None,
            "prompt_sha256": None,
            "prompt_version": "title-spec-v4.1",
            "selected": [],
            "selected_at": None,
        },
        "cover_map": default_cover_map(),
        "vertical_cover": default_vertical_cover(),
        "prompts": {"file": "03_prompts/prompts.json", "channel": None,
                    "count": 4, "items": []},
        "video": {"path": None, "source": None, "candidates_count": 0,
                  "name_pattern": None, "duration_s": None, "cut_s": None,
                  "cover": None,
                  "export_dir": None, "export_file": None, "draft_name": None,
                  "export_state": None,
                  "companions": []},
        "workflow": copy.deepcopy(WORKFLOW_DEFAULTS),
        "publish": {"status": "not_ready", "idempotency_key": None,
                    "platforms": [], "receipts": []},
        "cleanup": default_cleanup(),
        "history": [],
    }


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def migrate_job(job: dict[str, Any]) -> dict[str, Any]:
    """把旧版 job.json 补齐到当前 schema，不覆盖已有值。"""
    template = build_job(job.get("series") or "未命名剧集",
                         host=job.get("host") or "trae")
    for key, value in template.items():
        if key not in job:
            job[key] = value
        elif isinstance(value, dict) and isinstance(job[key], dict):
            for sub_key, sub_value in value.items():
                job[key].setdefault(sub_key, sub_value)
    job["schema_version"] = SCHEMA_VERSION
    job.setdefault("revision", 1)
    return job


def save_job(job: dict[str, Any], job_dir: str) -> str:
    path = os.path.join(job_dir, "job.json")
    job["updated_at"] = now_iso()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


def load_job(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return migrate_job(json.load(f))


def _stage_index(stage: str) -> int:
    return STAGES.index(stage) if stage in STAGES else -1


def validate_job(job: dict[str, Any], job_dir: Optional[str] = None) -> list[str]:
    errors: list[str] = []

    for key in ("schema_version", "job_id", "series", "stage", "input", "titles",
                "cover_map", "vertical_cover", "prompts", "video", "workflow",
                "publish", "cleanup", "history", "revision"):
        if key not in job:
            errors.append(f"缺少字段: {key}")

    if job.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version 应为 {SCHEMA_VERSION}")
    if job.get("stage") not in STAGES:
        errors.append(f"未知 stage: {job.get('stage')}")
    if not isinstance(job.get("revision"), int) or job.get("revision", 0) < 1:
        errors.append("revision 应为正整数")

    wf = job.get("workflow")
    if wf is not None:
        if not isinstance(wf, dict):
            errors.append("workflow 应为对象")
        else:
            if wf.get("mode") not in WORKFLOW_MODES:
                errors.append(f"workflow.mode 应为 {WORKFLOW_MODES}: {wf.get('mode')!r}")
            cut = wf.get("cut_s")
            if cut is not None and (isinstance(cut, bool)
                                     or not isinstance(cut, (int, float)) or cut <= 0):
                errors.append(f"workflow.cut_s 应为正数秒或 null: {cut!r}")
            dv = wf.get("derive")
            if not isinstance(dv, list) or any(p not in DERIVE_PRESETS for p in dv):
                errors.append(f"workflow.derive 应为已知平台列表: {dv!r}")
            if not isinstance(wf.get("intent_revision"), int) or wf.get("intent_revision") < 0:
                errors.append("workflow.intent_revision 应为非负整数（仅人工递增）")

    cover_map = job.get("cover_map") or []
    if len(cover_map) != 3:
        errors.append(f"cover_map 应有 3 项，实际 {len(cover_map)}")
    for item in cover_map:
        slot = item.get("slot")
        if slot not in (1, 2, 3):
            errors.append(f"非法 slot: {slot}")
        if item.get("kind") != "horizontal":
            errors.append(f"slot {slot} 的 kind 应为 horizontal")
        title_id = item.get("title_id")
        if title_id is not None and not re.fullmatch(r"T(?:[1-9]|1[0-2])", str(title_id)):
            errors.append(f"slot {slot} 的 title_id 非法: {title_id}")
        if not str(item.get("file") or "").startswith("04_covers/"):
            errors.append(f"slot {slot} 文件应位于 04_covers/")

    vertical = job.get("vertical_cover") or {}
    if vertical.get("slot") != "vertical":
        errors.append("vertical_cover.slot 应为 vertical")
    if vertical.get("kind") != "vertical" or vertical.get("role") != "video_cover":
        errors.append("vertical_cover 应为 kind=vertical / role=video_cover")
    if not str(vertical.get("file") or "").startswith("04_covers/"):
        errors.append("vertical_cover 文件应位于 04_covers/")

    titles = job.get("titles") or {}
    if titles.get("count") != 12:
        errors.append("titles.count 应为 12")
    if titles.get("gate") not in ("pending", "done"):
        errors.append(f"titles.gate 非法: {titles.get('gate')}")
    selected = titles.get("selected") or []
    if len(selected) not in (0, 3):
        errors.append("titles.selected 应为空或 3 项")
    for entry in selected:
        if not isinstance(entry, dict):
            errors.append(f"selected 条目应为对象 {{slot,title_id,title}}，实际: {entry!r}")
            continue
        if not re.fullmatch(r"T(?:[1-9]|1[0-2])", str(entry.get("title_id"))):
            errors.append(f"selected 中 title_id 非法: {entry.get('title_id')}")
        if entry.get("slot") not in (1, 2, 3):
            errors.append(f"selected 中 slot 非法: {entry.get('slot')}")
    if titles.get("selected_by") not in (None, "user", "auto"):
        errors.append(f"titles.selected_by 非法: {titles.get('selected_by')}")
    if titles.get("selected_by") == "auto" and titles.get("auto_mode") not in ("recommended", "score"):
        errors.append("selected_by=auto 时 auto_mode 应为 recommended/score")
    if titles.get("selected_by") == "user" and titles.get("auto_mode") is not None:
        errors.append("selected_by=user 时 auto_mode 应为 null")
    if titles.get("gate") == "done" and len(selected) != 3:
        errors.append("gate=done 时 selected 必须有 3 项")

    prompts = job.get("prompts") or {}
    if prompts.get("count") != 4:
        errors.append("prompts.count 应为 4")
    if not isinstance(prompts.get("items", []), list):
        errors.append("prompts.items 应为数组")

    video = job.get("video") or {}
    if video.get("path") and not str(video["path"]).endswith(".mp4"):
        errors.append("video.path 应为 mp4")
    if not isinstance(video.get("candidates_count", 0), int):
        errors.append("video.candidates_count 应为整数")
    if video.get("export_state") not in (None, "exported", "cleaned"):
        errors.append(f"video.export_state 非法: {video.get('export_state')}"
                      "（只允许 null / exported / cleaned）")
    cut = video.get("cut_s")
    if cut is not None:
        dur = video.get("duration_s")
        if isinstance(cut, bool) or not isinstance(cut, (int, float)) or cut <= 0:
            errors.append("video.cut_s 应为正数秒")
        elif isinstance(dur, (int, float)) and not isinstance(dur, bool) and dur > 0 and cut > dur:
            errors.append(f"video.cut_s({cut}) 超过 duration_s({dur})")

    publish = job.get("publish") or {}
    if not isinstance(publish.get("platforms", []), list):
        errors.append("publish.platforms 应为数组")
    for p in publish.get("platforms", []):
        if not p.get("name"):
            errors.append("publish.platforms 项缺少 name")
        if p.get("status") not in ("pending", "uploading", "succeeded", "failed", "blocked"):
            errors.append(f"平台状态非法: {p.get('status')}")

    cleanup = job.get("cleanup") or {}
    if cleanup.get("status") not in ("blocked", "eligible", "done", "skipped"):
        errors.append(f"cleanup.status 非法: {cleanup.get('status')}")
    if not isinstance(cleanup.get("targets", []), list):
        errors.append("cleanup.targets 应为数组")
    for ext in cleanup.get("external", []):
        if not isinstance(ext, dict) or not ext.get("tool") or not ext.get("draft_name"):
            errors.append("cleanup.external 项缺少 tool/draft_name")
        elif ext.get("status") not in ("pending", "done", "skipped"):
            errors.append(f"cleanup.external 状态非法: {ext.get('status')}")

    if job_dir:
        # 基础目录：任何阶段都必须存在
        for rel in BASE_DIRS:
            if not os.path.isdir(os.path.join(job_dir, rel)):
                errors.append(f"缺少目录: {rel}")
        # 发布目录：只有进入发布阶段才要求（covers_ready 等前序阶段允许缺失）
        if job.get("stage") in ("publish_ready", "published"):
            for rel in PUBLISH_DIRS:
                if not os.path.isdir(os.path.join(job_dir, rel)):
                    errors.append(f"缺少目录: {rel}")
        synopsis = (job.get("input") or {}).get("synopsis")
        if not synopsis or not os.path.isfile(os.path.join(job_dir, synopsis)):
            errors.append("简介文件不存在")
        ref = (job.get("input") or {}).get("reference_image")
        if ref and not os.path.isfile(os.path.join(job_dir, ref)):
            errors.append(f"参考图不存在: {ref}")
        prompt_file = (job.get("titles") or {}).get("prompt_file")
        if _stage_index(job.get("stage")) >= _stage_index("titles_done") and prompt_file:
            if not os.path.isfile(os.path.join(job_dir, prompt_file)):
                errors.append(f"标题提示词文件不存在: {prompt_file}")

    # 绝对路径白名单：video.export_dir / export_file 是 pipeline 的实际导出位置，
    # 位于任务目录之外，必须允许绝对路径。
    abs_ok = {("video", "export_dir"), ("video", "export_file")}
    for section in ("input", "titles", "prompts", "video", "cleanup"):
        for key, value in (job.get(section) or {}).items():
            if (section, key) in abs_ok:
                continue
            if isinstance(value, str) and os.path.isabs(value):
                errors.append(f"{section} 中出现绝对路径: {value}")

    return errors


def update_job(job_dir: str, patch: dict[str, Any], *,
               event: str = "update", detail: str = "") -> dict[str, Any]:
    """读-改-写 job.json：深合并、stage 只进不退、revision+1。"""
    path = os.path.join(job_dir, "job.json")
    job = load_job(path)
    # gate 守卫：titles.gate 禁止 done→pending 回退。
    # 历史教训（重回95 2026-11-02）：手工 update 补丁携带陈旧 titles 块，
    # 深合并把 select 置好的 done 打回 pending，watcher 永久不触发且无告警。
    # 重选请走 select 命令（它会成对重写 selected+gate），故此处直接拒绝。
    patch_titles = patch.get("titles")
    if isinstance(patch_titles, dict) and patch_titles.get("gate") == "pending" \
            and (job.get("titles") or {}).get("gate") == "done":
        raise ValueError("titles.gate 不能从 done 回退为 pending；"
                         "如需更换标题请用 select 命令重写 selected"
                         "（select 不会把 gate 打回 pending）")
    target_stage = patch.get("stage")
    if target_stage:
        if target_stage not in STAGES:
            raise ValueError(f"未知 stage: {target_stage}")
        current = _stage_index(job.get("stage"))
        if _stage_index(target_stage) < current:
            raise ValueError(f"stage 不能回退: {job.get('stage')} -> {target_stage}")
    _deep_merge(job, patch)
    job["revision"] = int(job.get("revision", 1)) + 1
    job["history"].append({"at": now_iso(), "event": event,
                           "detail": detail, "revision": job["revision"]})
    errors = validate_job(job, job_dir)
    if errors:
        raise ValueError("job.json 校验失败: " + "；".join(errors))
    save_job(job, job_dir)
    return job


# ── 调度意图：dispatch.json → job.json.workflow 归并 ──────────────────────

def read_dispatch(job_dir: str) -> dict[str, Any]:
    """读 cover_job/dispatch.json（豆包侧意图声明）；不存在返回 {}。"""
    p = os.path.join(job_dir, DISPATCH_NAME)
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8-sig") as f:  # 兼容 Windows 记事本 BOM
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{DISPATCH_NAME} 应为 JSON 对象")
    return data


def _normalize_dispatch(raw: dict[str, Any]) -> dict[str, Any]:
    """规范化 dispatch 声明（mode/cut_s/derive/priority）；非法值抛 ValueError。"""
    out: dict[str, Any] = {}
    mode = str(raw.get("mode") or "full").strip().lower()
    if mode not in WORKFLOW_MODES:
        raise ValueError(f"dispatch.mode 应为 {WORKFLOW_MODES}: {raw.get('mode')!r}")
    out["mode"] = mode
    cut = raw.get("cut_s")
    if cut is not None:
        if isinstance(cut, bool) or not isinstance(cut, (int, float)):
            raise ValueError(f"dispatch.cut_s 应为秒数: {cut!r}")
        if cut < 0:
            raise ValueError(f"dispatch.cut_s 不能为负: {cut!r}")
        cut = float(cut) if cut > 0 else None  # 0 = 不裁剪
    out["cut_s"] = cut
    derive = raw.get("derive")
    derive = [] if derive is None else ([derive] if isinstance(derive, str) else list(derive))
    names: list[str] = []
    if any(str(n).strip().lower() == "all" for n in derive):
        names = list(DERIVE_PRESETS)
    else:
        for n in derive:
            key = DERIVE_ALIASES.get(str(n).strip().lower(), str(n).strip().lower())
            if key not in DERIVE_PRESETS:
                raise ValueError(f"dispatch.derive 未知平台: {n}")
            if key not in names:
                names.append(key)
    out["derive"] = names
    pri = raw.get("priority", 0)
    if isinstance(pri, bool) or not isinstance(pri, (int, float)):
        raise ValueError(f"dispatch.priority 应为整数: {pri!r}")
    out["priority"] = int(pri)
    return out


def merge_workflow_core(job: dict[str, Any], job_dir: str, *, notify=None) -> dict[str, Any]:
    """把 dispatch.json 归并进 job['workflow']（只改内存，不落盘）。

    规则：
      - intent_revision==0：dispatch 提供全部值（幂等可重复）；无 dispatch 则默认值
      - intent_revision>0（人工已确认）：job.json 永远优先，dispatch 只补缺失、
        不覆盖；值冲突记入 report.conflicts 并可 notify
      - 任何程序路径都不写 intent_revision（只读）
    """
    wf = dict(job.get("workflow") or {})
    for k, v in WORKFLOW_DEFAULTS.items():
        wf.setdefault(k, copy.deepcopy(v))
    rev = int(wf.get("intent_revision") or 0)
    raw = read_dispatch(job_dir)
    conflicts: list[str] = []
    changed = False
    if rev == 0:
        merged = _normalize_dispatch(raw) if raw else {}
        src = "dispatch" if raw else (wf.get("intent_source") or "default")
        for k in ("mode", "cut_s", "derive", "priority"):
            if k in merged and wf.get(k) != merged[k]:
                wf[k] = merged[k]
                changed = True
        if wf.get("intent_source") != src:
            wf["intent_source"] = src
            changed = True
        if changed or wf.get("merged_at") is None:
            wf["merged_at"] = now_iso()
            changed = True
    else:
        if raw:
            disp = _normalize_dispatch(raw)
            for k, v in disp.items():
                cur = wf.get(k)
                cur_empty = cur is None or (isinstance(cur, list) and not cur)
                val_empty = v is None or (isinstance(v, list) and not v)
                if cur_empty and not val_empty:
                    wf[k] = v
                    wf["merged_at"] = now_iso()
                    changed = True
                elif cur != v:
                    conflicts.append(f"{k}: job.json={cur!r} dispatch={v!r}")
        if conflicts:
            if notify:
                try:
                    notify("⚠️ 调度意图冲突",
                           f"{os.path.basename(os.path.dirname(job_dir))}：人工意图优先，"
                           f"dispatch 未覆盖（{'；'.join(conflicts[:2])}）")
                except Exception:
                    pass
    job["workflow"] = wf
    return {"changed": changed, "intent_source": wf.get("intent_source"),
            "intent_revision": rev, "conflicts": conflicts, "workflow": dict(wf)}


def merge_workflow(job_dir: str, *, dry_run: bool = False, notify=None) -> dict[str, Any]:
    """独立归并入口（watcher 人工意图重归并）：读-归并-写，revision+1 记 history。"""
    job = load_job(os.path.join(job_dir, "job.json"))
    rep = merge_workflow_core(job, job_dir, notify=notify)
    if dry_run or not rep["changed"]:
        rep["dry_run"] = dry_run
        return rep
    patch = {k: v for k, v in rep["workflow"].items() if k != "intent_revision"}
    update_job(job_dir, {"workflow": patch}, event="workflow_merge",
               detail=f"intent_source={rep['intent_source']} "
                      f"conflicts={len(rep['conflicts'])}")
    return rep


def _load_titles_items(titles_path: str) -> list[dict[str, Any]]:
    with open(titles_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("titles") if isinstance(data, dict) else data


def record_titles(job_dir: str, titles_path: str, prompt_path: Optional[str] = None,
                  prompt_version: Optional[str] = None) -> dict[str, Any]:
    """Stage 1 落盘：登记 12 条标题与初始提示词，停在门禁前。"""
    items = _load_titles_items(titles_path)
    if not isinstance(items, list) or len(items) != 12:
        raise ValueError(f"titles.json 应有 12 条，实际 {len(items) if isinstance(items, list) else '非数组'}")
    ids = [str(item.get("id")) for item in items]
    if ids != [f"T{i}" for i in range(1, 13)]:
        raise ValueError("titles.json 的 id 必须依次为 T1—T12")
    for item in items:
        if not str(item.get("text") or "").strip():
            raise ValueError(f"{item.get('id')} 缺少 text")

    patch: dict[str, Any] = {
        "stage": "titles_done",
        "titles": {
            "file": rel_path(os.path.abspath(titles_path), job_dir),
            "gate": "pending",
            "selected": [],
            "selected_at": None,
        },
    }
    if prompt_path:
        prompt_abs = os.path.abspath(prompt_path)
        patch["titles"]["prompt_file"] = rel_path(prompt_abs, job_dir)
        patch["titles"]["prompt_sha256"] = sha256_file(prompt_abs)
    if prompt_version:
        patch["titles"]["prompt_version"] = prompt_version

    return update_job(job_dir, patch, event="stage1_titles",
                      detail=f"写入 {len(items)} 条标题，等待用户门禁")


def _score_tuple(item: dict[str, Any]) -> tuple:
    """排序键：总分 → 内容兑现 → 信息缺口 → 情绪峰值（与 scorecard 同序）。"""
    score = item.get("score") or {}
    total = item.get("total")
    if not isinstance(total, (int, float)):
        total = sum(v for v in score.values() if isinstance(v, (int, float)))
    return (total,
            score.get("內容兌現", 0), score.get("内容兑现", 0),
            score.get("信息缺口", 0), score.get("情緒峰值", 0), score.get("情绪峰值", 0))


def auto_pick_titles(titles_path: str, top_n: int = 3,
                     mode: str = "recommended") -> dict[str, Any]:
    """按评分自动选 top_n 条（Stage 2 直调兜底）。

    mode="recommended"（默认）：summary.recommended（每版推荐各 1 条，通常 4 条）
        优先按分入选；推荐池不足 top_n 时从全量按分补足，并记 fallback=True；
        推荐池缺失时整体退化为全量评分。
    mode="score"：全量按分取前 top_n。
    两种模式均为：总分相同时推荐条目优先；再平则按 scorecard 同序细分
    （内容兑现 → 信息缺口 → 情绪峰值）。
    仅在 titles.json 含 score 字段时可用；否则报错，不瞎选。

    返回 {"picks": [{slot,title_id,title,total,recommended}],
          "mode": mode, "fallback": bool, "pool": [推荐池 id]}。
    """
    if mode not in ("recommended", "score"):
        raise ValueError(f"未知兜底模式: {mode}")
    with open(titles_path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("titles") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise ValueError("titles.json 为空或格式非法，无法自动选择")
    for item in items:
        if not isinstance(item.get("score"), dict):
            raise ValueError(f"{item.get('id')} 缺少 score，无法自动选择；请先完成人工门禁")

    rec_pool: list[str] = []
    if isinstance(data, dict):
        rec_pool = [str(x) for x in ((data.get("summary") or {}).get("recommended") or [])]
    rec_set = set(rec_pool)

    def _key(it: dict[str, Any]) -> tuple:
        st = _score_tuple(it)
        return (st[0], str(it.get("id")) in rec_set, *st[1:])

    ranked = sorted(items, key=_key, reverse=True)

    fallback = False
    if mode == "recommended":
        if rec_set:
            rec_items = [it for it in ranked if str(it.get("id")) in rec_set]
            rest_items = [it for it in ranked if str(it.get("id")) not in rec_set]
            picked = (rec_items + rest_items)[:top_n]
            fallback = len(rec_items) < top_n
        else:  # 推荐池缺失，整体退化为全量评分
            picked = ranked[:top_n]
            fallback = True
    else:
        picked = ranked[:top_n]

    picks = [
        {"slot": i + 1, "title_id": str(p["id"]), "title": p["text"],
         "total": _score_tuple(p)[0], "recommended": str(p["id"]) in rec_set}
        for i, p in enumerate(picked)
    ]
    return {"picks": picks, "mode": mode, "fallback": fallback, "pool": rec_pool}


def _best_of_selected(job_dir: str, titles_file: Optional[str],
                      selected: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """从 selected 的 3 条里挑评分最高的一条，用于竖版绑定。

    默认取总分最高；总分相同则按 selected 中出现的先后取靠前的一条。
    vertical_cover.title_id 已由人工指定时由调用方保留、不覆盖。
    评分数据缺失（titles.json 不在 / 条目无 score）时返回 None，不绑定。
    """
    if len(selected) != 3:
        return None
    path = os.path.join(job_dir, titles_file or "02_titles/titles.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    items = data.get("titles") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    by_id = {str(it.get("id")): it for it in items if isinstance(it, dict)}
    best_entry, best_total = None, None
    for entry in selected:
        it = by_id.get(str(entry.get("title_id")))
        if not it or not isinstance(it.get("score"), dict):
            return None
        total = _score_tuple(it)[0]
        if best_total is None or total > best_total:
            best_entry, best_total = entry, total
    return best_entry


def _annotate_titles_file(job_dir: str, job: dict[str, Any],
                          picks: list[dict[str, Any]], by: str) -> None:
    """门禁后在 titles.json 原位标注：选中 3 条加 selected/slot，顶层记 gate。

    不复制标题文本，job.json 仍是唯一事实源；这里只是让 02_titles/titles.json
    自包含（豆包侧 Stage 2 只读这个文件也能看懂结果）。
    """
    titles_rel = (job.get("titles") or {}).get("file")
    if not titles_rel:
        return
    path = os.path.join(job_dir, titles_rel)
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("titles") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return
    by_id = {p["title_id"]: p for p in picks}
    for item in items:
        item.pop("selected", None)
        item.pop("slot", None)
        tid = str(item.get("id"))
        if tid in by_id:
            item["selected"] = True
            item["slot"] = by_id[tid]["slot"]
    if isinstance(data, dict):
        data["gate"] = "done"
        data["selected_at"] = now_iso()
        data["selected_by"] = by
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def select_titles(job_dir: str, picks: list[dict[str, Any]],
                  by: str = "user", auto_mode: Optional[str] = None) -> dict[str, Any]:
    """写入门禁结果：titles.selected + gate=done + cover_map 回填。

    picks: [{"slot":1,"title_id":"T6","title":"..."},...] 共 3 项。
    by: "user"（人工门禁）/ "auto"（兜底自动选分）。
    auto_mode: 兜底模式（"recommended"/"score"），仅 by="auto" 时记录，人工选置空。
    """
    if by not in ("user", "auto"):
        raise ValueError(f"by 非法: {by}")
    if len(picks) != 3:
        raise ValueError(f"selected 应为 3 项，实际 {len(picks)}")

    path = os.path.join(job_dir, "job.json")
    job = load_job(path)

    # 回填 cover_map：按 slot 对齐 title_id / title
    cover_map = job.get("cover_map") or []
    by_slot = {p["slot"]: p for p in picks}
    new_map = []
    for item in cover_map:
        slot = item.get("slot")
        if slot in by_slot:
            item = dict(item)
            item["title_id"] = by_slot[slot]["title_id"]
            item["title"] = by_slot[slot]["title"]
        new_map.append(item)

    current_stage = job.get("stage")
    selected_at = now_iso()
    patch: dict[str, Any] = {
        "titles": {
            "gate": "done",
            "selected": [{"slot": p["slot"], "title_id": p["title_id"],
                          "title": p["title"]} for p in picks],
            "selected_at": selected_at,
            "selected_by": by,
            "auto_mode": auto_mode if by == "auto" else None,
        },
        "cover_map": new_map,
    }
    # 只有当前阶段早于 titles_selected 时才推进 stage；已出封面后重选不能回退 stage。
    reselect_after_covers = _stage_index(current_stage) >= _stage_index("titles_selected")
    if not reselect_after_covers:
        patch["stage"] = "titles_selected"
    # 竖版绑定：selected 中评分最高的一条；人工已指定则保留，无评分数据保持 null
    best = _best_of_selected(job_dir, (job.get("titles") or {}).get("file"), picks)
    event = "gate_select" if by == "user" else "gate_auto_select"
    mode_label = {"recommended": "推荐模式（推荐池优先）",
                  "score": "评分模式（全量取前 3）"}.get(auto_mode, "评分")
    detail = ("用户门禁选定 3 条标题" if by == "user"
              else f"兜底机制：用户未选，{mode_label}自动选定前 3 条")
    explicit = (job.get("vertical_cover") or {}).get("title_id")
    if best and explicit in (None, ""):
        patch["vertical_cover"] = {"title_id": best["title_id"], "title": best["title"]}
        detail += f"；竖版绑定最高分 {best['title_id']}"
    elif explicit not in (None, ""):
        detail += f"；竖版保留人工指定 {explicit}"
    if reselect_after_covers:
        # stage 不回退，但已生成的封面与旧标题绑定，必须显式标记待重做，不能静默
        patch["derived"] = {
            "covers_stale": True,
            "covers_stale_at": selected_at,
            "reason": f"重选发生在 {current_stage} 之后，04_covers 需重新生成",
        }
        detail += (f"；重选（当前 {current_stage}）：stage 保持不变，"
                   "04_covers 需重新生成")
    job = update_job(job_dir, patch, event=event, detail=detail)
    _annotate_titles_file(job_dir, job, picks, by)
    return job


def resolve_reference(job: dict[str, Any], job_dir: str) -> dict[str, Any]:
    """参考图兜底链：reference_image → reference_original → 剧集目录探测 → 空。

    返回 {"path": 绝对路径|None, "via": 命中环节, "warnings": [...]}
    """
    warnings: list[str] = []
    inp = job.get("input") or {}

    for rel, via in ((inp.get("reference_image"), "input.reference_image"),
                     (inp.get("reference_original"), "input.reference_original")):
        if rel:
            p = os.path.normpath(os.path.join(job_dir, rel))
            if os.path.isfile(p):
                if via.endswith("reference_original"):
                    warnings.append("任务副本缺失，回退到原始参考图")
                return {"path": p, "via": via, "warnings": warnings}
            warnings.append(f"{via} 指向的文件不存在: {rel}")

    # 剧集目录探测（cover_job 的父目录）
    series_dir = os.path.dirname(os.path.normpath(job_dir))
    found = detect_reference_image(series_dir)
    if found.get("path"):
        warnings.append(f"回退到剧集目录探测: {found.get('reason')}")
        return {"path": found["path"], "via": "series_dir_detect",
                "warnings": warnings}

    warnings.append("参考图兜底链全部落空")
    return {"path": None, "via": None, "warnings": warnings}


def resolve_context(job_dir: str, *, auto: bool = True,
                    dry_run: bool = False,
                    mode: str = "recommended") -> dict[str, Any]:
    """Stage 2 一键取上下文：选定标题 + 参考图 + 提示词，带完整兜底链。

    - 标题：gate=done 直接用 selected；否则 auto=True 时按 mode 自动取前 3
      并写回 job.json（selected_by="auto"、auto_mode=mode），dry_run 只演算不落盘。
      mode="recommended"（默认）：summary.recommended 推荐池优先；
      mode="score"：全量评分取前 3。同分时推荐条目优先。
    - 参考图：resolve_reference 兜底链。
    返回 dict，含 warnings；调用方据此决定是否继续。
    """
    job = load_job(os.path.join(job_dir, "job.json"))
    warnings: list[str] = []
    titles_info: dict[str, Any]

    gate = (job.get("titles") or {}).get("gate")
    if gate == "done" and (job.get("titles") or {}).get("selected"):
        titles_info = {
            "selected": job["titles"]["selected"],
            "selected_by": (job["titles"] or {}).get("selected_by") or "user",
            "auto_applied": False,
        }
    else:
        titles_file = (job.get("titles") or {}).get("file") or "02_titles/titles.json"
        titles_path = os.path.join(job_dir, titles_file)
        if not os.path.isfile(titles_path):
            raise FileNotFoundError(
                f"门禁未过且找不到 {titles_file}；请先完成 Stage 1")
        if not auto:
            raise ValueError(
                "titles.gate != done 且未允许自动选择；请先完成人工门禁")
        picks_result = auto_pick_titles(titles_path, top_n=3, mode=mode)
        picks = picks_result["picks"]
        mode_desc = "推荐模式" if mode == "recommended" else "评分模式"
        fb = "（推荐池不足/缺失，已从全量补足）" if picks_result["fallback"] else ""
        warnings.append(
            f"用户未完成门禁，已按{mode_desc}{fb}自动选定: "
            + ",".join(p["title_id"] for p in picks))
        if not dry_run:
            job = select_titles(job_dir, picks, by="auto", auto_mode=mode)
        titles_info = {"selected": picks, "selected_by": "auto",
                       "auto_applied": True, "auto_mode": mode,
                       "fallback": picks_result["fallback"]}

    ref = resolve_reference(job, job_dir)
    warnings.extend(ref["warnings"])

    prompts = (job.get("prompts") or {}).get("items") or []

    return {
        "job_dir": job_dir,
        "revision": job.get("revision"),
        "stage": job.get("stage"),
        "titles": titles_info,
        "reference_image": {"path": ref["path"], "via": ref["via"]},
        "prompts": prompts,
        "cover_map": job.get("cover_map"),
        "dry_run": dry_run,
        "warnings": warnings,
    }


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def detect_stream_export(series_dir: Optional[str] = None,
                         draft_name: Optional[str] = None) -> Optional[dict[str, Any]]:
    """在 pipeline/_stream_manifests 里按 source_dir 或 draft_name 找导出记录。

    返回 {draft_name, manifest, export_file, export_dir, exists}；找不到返回 None。
    """
    mdir = os.path.join(_repo_root(), "pipeline", "_stream_manifests")
    if not os.path.isdir(mdir):
        return None
    target_src = os.path.normpath(series_dir) if series_dir else None
    for fn in sorted(os.listdir(mdir)):
        if not fn.endswith(".json"):
            continue
        mp = os.path.join(mdir, fn)
        try:
            with open(mp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        paths = data.get("paths") or {}
        if draft_name:
            if data.get("draft_name") != draft_name:
                continue
        elif target_src and os.path.normpath(paths.get("source_dir") or "") != target_src:
            continue
        export_file = paths.get("export_file")
        if not export_file:
            continue
        return {
            "draft_name": data.get("draft_name"),
            "manifest": rel_path(mp, _repo_root()),
            "export_file": export_file,
            "export_dir": os.path.dirname(export_file),
            "exists": os.path.isfile(export_file),
        }
    return None


def _export_companions(export_file: str) -> list[str]:
    """成品旁的脚本产物：<mp4>.progress.json、<名>-封面.jpg。"""
    base, _ = os.path.splitext(export_file)
    return [export_file + ".progress.json", base + "-封面.jpg"]


def record_video(job_dir: str, *, export_dir: Optional[str] = None,
                 export_file: Optional[str] = None,
                 draft_name: Optional[str] = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """登记 pipeline 导出目录：只记录位置，不复制 mp4。

    自动按 source_dir=cover_job 父目录探测 stream manifest；同时把
    stream_manifest.py 的清理联动挂到 cleanup.external（记录不接管）。
    仅当当前 stage 已过 covers_ready 时才推进到 video_ready。
    """
    job = load_job(os.path.join(job_dir, "job.json"))
    series_dir = os.path.dirname(os.path.normpath(job_dir))
    info = detect_stream_export(series_dir, draft_name)

    # 手工指定优先：用户显式给的值永远覆盖自动探测结果
    overridden: list[str] = []
    if info:
        for key, user_val, auto_val in (
                ("export_file", export_file, info["export_file"]),
                ("export_dir", export_dir, info["export_dir"]),
                ("draft_name", draft_name, info["draft_name"])):
            if user_val and auto_val and os.path.normpath(str(user_val)) != os.path.normpath(str(auto_val)):
                overridden.append(f"{key}: 手工={user_val}（覆盖探测 {auto_val}）")
        export_file = export_file or info["export_file"]
        export_dir = export_dir or info["export_dir"]
        draft_name = draft_name or info["draft_name"]
    if export_file and not export_dir:
        export_dir = os.path.dirname(export_file)
    if not export_dir and not export_file:
        raise ValueError("未在 pipeline/_stream_manifests 找到该剧导出记录；"
                         "请提供 --export-dir 或 --draft-name")
    if not export_file and export_dir and os.path.isdir(export_dir):
        mp4s = [n for n in os.listdir(export_dir) if n.lower().endswith(".mp4")]
        if len(mp4s) == 1:
            export_file = os.path.join(export_dir, mp4s[0])

    companions = [p for p in (_export_companions(export_file) if export_file else [])
                  if os.path.exists(p)]

    patch: dict[str, Any] = {"video": {
        "export_dir": (export_dir or "").replace(os.sep, "/") or None,
        "export_file": (export_file or "").replace(os.sep, "/") or None,
        "draft_name": draft_name,
        "export_state": "exported",
        "companions": [p.replace(os.sep, "/") for p in companions],
    }}

    if draft_name:
        ext_entry = {
            "tool": "pipeline/stream_manifest.py",
            "draft_name": draft_name,
            "manifest": info["manifest"] if info else None,
            "command": f'python pipeline/stream_manifest.py clean "{draft_name}"',
            "scope": ["export_bundle(mp4+封面+progress)", "draft_dir",
                      "jycache_dir", "workdir"],
            "deletes_final_mp4": True,
            "status": "pending",
        }
        external = [e for e in (job.get("cleanup") or {}).get("external", [])
                    if e.get("draft_name") != draft_name]
        external.append(ext_entry)
        patch["cleanup"] = {"external": external}

    if _stage_index(job.get("stage")) >= _stage_index("covers_ready"):
        patch["stage"] = "video_ready"

    if dry_run:
        return {"dry_run": True, "patch": patch, "overridden": overridden,
                "detected": info, "companions": companions}

    detail = f"导出目录: {patch['video']['export_dir']}"
    if overridden:
        detail += "；" + "；".join(overridden)
    return update_job(job_dir, patch, event="video_recorded", detail=detail)


def _backfill_hashes_inplace(job: dict[str, Any], job_dir: str) -> list[str]:
    """在内存 job 上原位补算 null hash，返回补算字段列表（不落盘）。"""
    filled: list[str] = []

    def _fill(container: dict[str, Any], path_key: str, hash_key: str, label: str) -> None:
        rel = container.get(path_key)
        if rel and container.get(hash_key) is None:
            p = os.path.normpath(os.path.join(job_dir, rel))
            if os.path.isfile(p):
                container[hash_key] = sha256_file(p)
                filled.append(label)

    _fill(job.setdefault("input", {}), "reference_image", "reference_sha256",
          "input.reference_sha256")
    _fill(job.setdefault("titles", {}), "prompt_file", "prompt_sha256",
          "titles.prompt_sha256")
    for item in job.get("cover_map") or []:
        _fill(item, "image", "image_sha256", f"cover_map[{item.get('slot')}].image_sha256")
    _fill(job.setdefault("vertical_cover", {}), "image", "image_sha256",
          "vertical_cover.image_sha256")
    return filled


def backfill_hashes(job_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """补算无执行环境宿主（豆包）写为 null 的 hash 字段。

    覆盖：input.reference_sha256、titles.prompt_sha256、cover_map[].image_sha256、
    vertical_cover.image_sha256。仅当字段为 None 且对应文件存在时补算。
    """
    job = load_job(os.path.join(job_dir, "job.json"))
    filled = _backfill_hashes_inplace(job, job_dir)

    if dry_run or not filled:
        return {"dry_run": dry_run, "filled": filled, "changed": bool(filled)}

    job["revision"] = job.get("revision", 0) + 1
    job.setdefault("history", []).append({
        "revision": job["revision"], "event": "hash_backfill",
        "detail": f"补算 {len(filled)} 个 hash: {', '.join(filled)}",
        "at": now_iso()})
    errors = validate_job(job, job_dir)
    if errors:
        raise ValueError("补算后校验失败: " + "; ".join(errors))
    save_job(job, job_dir)
    return {"dry_run": False, "filled": filled, "changed": True,
            "revision": job["revision"]}


GATE_ENUM = ("pending", "done")


def _selected_shape_desc(entries: list[Any]) -> str:
    """描述 selected 旧形态，用于修复报告（如：字符串数组 3 条 ['T3','T5','T12']）。"""
    if not entries:
        return "空数组"
    kinds: list[str] = []
    for e in entries:
        k = "对象" if isinstance(e, dict) else (
            "字符串" if isinstance(e, str) else (
                "数字" if isinstance(e, int) else type(e).__name__))
        if k not in kinds:
            kinds.append(k)
    desc = "/".join(kinds) + f"数组 {len(entries)} 条"
    if len(entries) <= 3 and all(isinstance(e, (str, int)) for e in entries):
        desc += f" {entries}"
    return desc


def fix_job(job_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """validate --fix：规范化常见不合规字段后校验写盘。

    修复范围（不动其它字段）：
      1. titles.selected：字符串/数字/纯文本/缺字段对象 → [{slot,title_id,title}]
         （复用 _normalize_selected；title 文本取不到则留 None 并告警）；
      2. titles.gate：非枚举值（如 "selected"）→ selected 齐 3 条归 done，
         不足 3 条归 pending（不完整 selected 与 pending 不自洽，一并清空）；
      3. hash 字段：null 且文件存在 → 补算（同 backfill_hashes）。

    单一写者纪律：所有修改经 validate_job 通过后由 save_job 一次写盘，
    revision+1 并记一条 history（event=validate_fix）；无可修复项则完全不写盘。
    注意：gate 为合法枚举但内容不自洽（如 gate=done 而 selected 不足 3 条）
    不在修复范围，由最终校验报出，人工处理。
    """
    job = load_job(os.path.join(job_dir, "job.json"))
    fixes: list[dict[str, Any]] = []
    warnings: list[str] = []

    titles = job.setdefault("titles", {})
    old_sel = list(titles.get("selected") or [])
    old_gate = titles.get("gate")

    if old_gate not in GATE_ENUM and len(old_sel) != 3:
        # 非枚举 gate + 不完整 selected → 归 pending；不完整 selected 与
        # pending 不自洽（validate 要求空或 3 项），一并清空
        if old_sel:
            fixes.append({"field": "titles.selected",
                          "old": _selected_shape_desc(old_sel), "new": "空数组",
                          "reason": f"仅 {len(old_sel)} 条，pending 门禁下不保留"})
            titles["selected"] = []
        fixes.append({"field": "titles.gate", "old": repr(old_gate), "new": "pending",
                      "reason": f"非枚举值；selected 不足 3 条（{len(old_sel)}）→ pending"})
        titles["gate"] = "pending"
    else:
        # 齐 3 条（或 gate 已是合法枚举）：只做 selected 形态规范化
        if old_sel:
            new_sel, notes = _normalize_selected(job_dir, titles.get("file"), old_sel)
            if new_sel != old_sel:
                fixes.append({
                    "field": "titles.selected",
                    "old": _selected_shape_desc(old_sel),
                    "new": f"{len(new_sel)} 个对象 {{slot,title_id,title}}",
                    "reason": "；".join(notes) if notes else "形态规范化"})
                titles["selected"] = new_sel
        if old_gate not in GATE_ENUM:  # 此处必然是齐 3 条
            fixes.append({"field": "titles.gate", "old": repr(old_gate), "new": "done",
                          "reason": "非枚举值；selected 齐 3 条 → done"})
            titles["gate"] = "done"

    for i, e in enumerate(titles.get("selected") or []):
        if isinstance(e, dict) and e.get("title") is None:
            warnings.append(f"selected[{i}]（{e.get('title_id')}）title 文本未取到，留空")

    # 3) hash 补算
    for label in _backfill_hashes_inplace(job, job_dir):
        fixes.append({"field": label, "old": "null", "new": "已补算",
                      "reason": "文件存在且 hash 为空"})

    if not fixes:
        return {"changed": False, "fixes": [], "warnings": warnings, "fixed": 0}
    if dry_run:
        return {"changed": True, "dry_run": True, "fixes": fixes,
                "warnings": warnings, "fixed": len(fixes)}

    job["revision"] = job.get("revision", 0) + 1
    detail = "；".join(f"{x['field']}: {x['old']} → {x['new']}" for x in fixes)
    job.setdefault("history", []).append({
        "revision": job["revision"], "event": "validate_fix",
        "detail": detail, "at": now_iso()})
    errors = validate_job(job, job_dir)
    if errors:
        raise ValueError("修复后校验失败: " + "; ".join(errors))
    save_job(job, job_dir)
    return {"changed": True, "fixes": fixes, "warnings": warnings,
            "fixed": len(fixes), "revision": job["revision"]}


# 平台派生规格（契约 §6：derived 放平台用图；尺寸为建议值，核实后更新）
DERIVE_PRESETS: dict[str, dict[str, Any]] = {
    "youtube":         {"source": "horizontal", "size": (1280, 720),
                        "max_bytes": 2 * 1024 * 1024},
    "tiktok":          {"source": "vertical",   "size": (1080, 1920),
                        "max_bytes": None},
    "instagram-reels": {"source": "vertical",   "size": (1080, 1920),
                        "max_bytes": None},
    "facebook-reels":  {"source": "vertical",   "size": (1080, 1920),
                        "max_bytes": None},
}


def derive_covers(job_dir: str, platforms: list[str], *, slot: int = 1) -> dict[str, Any]:
    """从 04_covers 契约原图派生平台尺寸到 04_covers/derived/（不写 job.json）。

    horizontal 源取 cover_map[slot]（默认封面1），vertical 源取 vertical_cover。
    输出 jpg，命名 <源stem>_<platform>_<W>x<H>.jpg；max_bytes 超限自动降 quality。
    derived 是易变产物：不参与 hash 校验，可随时删除重新生成。
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("derive 需要 Pillow：pip install Pillow")

    bad = [p for p in platforms if p not in DERIVE_PRESETS]
    if bad:
        raise ValueError(
            f"未知平台: {','.join(bad)}；可选 {','.join(DERIVE_PRESETS)} / all")

    job = load_job(os.path.join(job_dir, "job.json"))

    def _source_path(kind: str) -> str:
        if kind == "horizontal":
            cm = [c for c in (job.get("cover_map") or []) if c.get("slot") == slot]
            rel = cm[0].get("file") if cm else None
        else:
            rel = (job.get("vertical_cover") or {}).get("file")
        if not rel:
            raise FileNotFoundError(f"job.json 缺少 {kind} 源文件登记（slot={slot}）")
        p = os.path.join(job_dir, rel)
        if not os.path.isfile(p):
            raise FileNotFoundError(f"源文件不存在: {rel}（Stage 2 产物落盘后再派生）")
        return p

    out_dir = os.path.join(job_dir, "04_covers", "derived")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for plat in platforms:
        spec = DERIVE_PRESETS[plat]
        src = _source_path(spec["source"])
        w, h = spec["size"]
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(out_dir, f"{stem}_{plat}_{w}x{h}.jpg")
        img = Image.open(src).convert("RGB").resize((w, h), Image.LANCZOS)
        quality = 92
        img.save(dst, "JPEG", quality=quality)
        limit = spec["max_bytes"]
        while limit and os.path.getsize(dst) > limit and quality > 60:
            quality -= 8
            img.save(dst, "JPEG", quality=quality)
        results.append({"platform": plat,
                        "source": os.path.relpath(src, job_dir),
                        "file": os.path.relpath(dst, job_dir),
                        "size": [w, h], "bytes": os.path.getsize(dst),
                        "quality": quality})
    return {"derived": results}


def _image_size(path: str) -> Optional[tuple[int, int]]:
    """纯标准库读 PNG/JPEG 宽高；解析失败返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(33)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return struct.unpack(">II", head[16:24])
            if head[:2] != b"\xff\xd8":
                return None
            f.seek(2)
            while True:
                b = f.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue
                marker = f.read(1)
                if not marker or marker in (b"\xd8", b"\xd9"):
                    continue
                seg_len = struct.unpack(">H", f.read(2))[0]
                if marker[0] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                                 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    data = f.read(seg_len - 2)
                    h, w = struct.unpack(">HH", data[1:5])
                    return w, h
                f.seek(seg_len - 2, 1)
    except (OSError, struct.error):
        return None


CANONICAL_COVER_STEMS: dict[Any, str] = {
    1: "cover_01_hit", 2: "cover_02_contrast", 3: "cover_03_guard",
    "vertical": "cover_04_vertical",
}


def detect_cover_files(job_dir: str, *, apply: bool = True) -> tuple[dict[Any, str], list[str]]:
    """识别 04_covers 下的图并改名到契约名。

    规则：契约文件名优先；竖版按文件名（vertical/竖）或宽高比（竖图）判定；
    其余横版按文件名编号（1/2/3）或自然顺序映射到 slot 1/2/3。
    apply=False 只演算不改名。返回 ({slot: 绝对路径}, [改名记录])。
    """
    covers = os.path.join(job_dir, "04_covers")
    result: dict[Any, str] = {}
    renames: list[str] = []
    if not os.path.isdir(covers):
        return result, renames

    files = [os.path.join(covers, n) for n in sorted(os.listdir(covers), key=natural_key)
             if os.path.isfile(os.path.join(covers, n))
             and os.path.splitext(n)[1].lower() in IMAGE_EXTS]
    by_stem = {os.path.splitext(os.path.basename(p))[0].lower(): p for p in files}
    for slot, stem in CANONICAL_COVER_STEMS.items():
        if stem in by_stem:
            result[slot] = by_stem[stem]
    rest = [p for p in files if p not in set(result.values())]

    # 竖版：名称提示优先，其次宽高比
    if "vertical" not in result and rest:
        hint = [p for p in rest if re.search(r"vertical|竖", os.path.basename(p), re.I)]
        portrait = [p for p in rest
                    if (lambda s: bool(s) and s[1] > s[0])(_image_size(p))]
        pick = (hint or portrait or [None])[0]
        if pick:
            result["vertical"] = pick
            rest.remove(pick)

    # 横版：先按文件名里的 1/2/3，再自然顺序补位
    numbered: dict[int, str] = {}
    for p in rest:
        stem = os.path.splitext(os.path.basename(p))[0].lower()
        m = re.search(r"(?:cover|封面|图|t)?[ _\-]?(0?[1-3])(?!\d)", stem)
        if m:
            numbered.setdefault(int(m.group(1)), p)
    used = set(numbered.values())
    for slot in (1, 2, 3):
        if slot in result:
            continue
        pick = numbered.get(slot)
        if pick is None:
            pick = next((x for x in rest if x not in used), None)
            if pick:
                used.add(pick)
        if pick:
            result[slot] = pick

    # 改名到契约名（保留原扩展名）
    for slot, p in list(result.items()):
        stem = CANONICAL_COVER_STEMS[slot]
        ext = os.path.splitext(p)[1].lower()
        if os.path.splitext(os.path.basename(p))[0].lower() == stem:
            continue
        dst = os.path.join(covers, stem + ext)
        renames.append(f"{os.path.basename(p)} → {os.path.basename(dst)}")
        if apply:
            os.replace(p, dst)
            result[slot] = dst
    return result, renames


def _title_texts(job_dir: str, titles_file: Optional[str]) -> dict[str, str]:
    """titles.json 的 id → 标题文本映射（结构容错）。"""
    rel = titles_file or "02_titles/titles.json"
    path = os.path.join(job_dir, rel)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    items = data.get("items") or data.get("titles") or (data if isinstance(data, list) else [])
    texts: dict[str, str] = {}
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        tid = it.get("id") or it.get("title_id") or f"T{idx + 1}"
        if it.get("title"):
            texts[str(tid).upper()] = it["title"]
    return texts


def _normalize_selected(job_dir: str, titles_file: Optional[str],
                        entries: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """把第三方 agent 的各种 selected 写法统一成 [{slot,title_id,title}]。

    支持：对象 / "T9" / 9 / 纯标题文本；slot 支持 1 / "1" / "封面1"。
    """
    texts = _title_texts(job_dir, titles_file)
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    for idx, entry in enumerate(entries[:3]):
        slot: Any = idx + 1
        title_id: Optional[str] = None
        title: Optional[str] = None
        if isinstance(entry, dict):
            slot = entry.get("slot", slot)
            title_id = entry.get("title_id")
            title = entry.get("title")
        elif isinstance(entry, int):
            title_id = f"T{entry}"
        elif isinstance(entry, str):
            s = entry.strip()
            if re.fullmatch(r"T\d{1,2}", s, re.I):
                title_id = s.upper()
            elif s.isdigit():
                title_id = f"T{int(s)}"
            else:
                title = s
        if isinstance(slot, str):
            m = re.fullmatch(r"封面\s*([123])", slot.strip())
            slot = int(m.group(1)) if m else (int(slot) if slot.strip().isdigit() else idx + 1)
        if slot not in (1, 2, 3):
            slot = idx + 1
        if title_id is None and title:
            hit = [k for k, v in texts.items() if v == title]
            title_id = hit[0] if hit else None
        if title_id is None and isinstance(entry, dict) and entry.get("title"):
            title_id = None
        if title_id is None:
            title_id = f"T{idx + 1}"
            notes.append(f"selected[{idx}] 缺 title_id，按顺序补 {title_id}")
        title_id = str(title_id).upper()
        title = title or texts.get(title_id)
        out.append({"slot": int(slot), "title_id": title_id, "title": title})
    if [e["slot"] for e in out] != [1, 2, 3]:
        notes.append("selected slot 重排为 1/2/3")
        for i, e in enumerate(out, 1):
            e["slot"] = i
    return out, notes


def ingest_job(job_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Stage 2 收口：从落盘产物（prompts.json + 4 张契约图）重建 job.json。

    收登记、规范化（selected.slot 中文→数字）、补 hash、推进 stage（只进不退）。
    幂等：重复跑安全；同名换图重跑会刷新 hash 并在报告 normalized 中体现。
    """
    job = load_job(os.path.join(job_dir, "job.json"))
    # ── stage 别名映射（第三方 agent 可能写 covers_done 等非契约值）
    raw_stage = job.get("stage")
    stage = canon_stage(raw_stage)
    if stage != raw_stage:
        report_stage_alias = f"stage {raw_stage}→{stage}"
        job["stage"] = stage
    else:
        report_stage_alias = None
    if stage not in ("titles_selected", "prompts_ready", "covers_ready"):
        raise ValueError(
            f"ingest 要求 stage ∈ titles_selected/prompts_ready/covers_ready，当前 {stage!r}")

    titles = job.get("titles") or {}
    report: dict[str, Any] = {"stage_from": stage, "prompts": None, "covers": [],
                              "missing_covers": [], "hash_filled": [], "normalized": [],
                              "renamed": []}
    if report_stage_alias:
        report["normalized"].append(report_stage_alias)

    # ── 标题门禁：优先读 selection.json（agent 只需交编号清单，不写 job.json）
    sel_file = os.path.join(job_dir, "02_titles", "selection.json")
    if os.path.isfile(sel_file) and len(titles.get("selected") or []) != 3:
        with open(sel_file, encoding="utf-8-sig") as f:
            sdata = json.load(f)
        picks = sdata.get("picks") or sdata.get("selected") or []
        titles["selected"] = picks
        titles["selected_by"] = sdata.get("selected_by") or "user"
        titles["selected_at"] = titles.get("selected_at") or now_iso()
        titles["gate"] = "done"
        report["normalized"].append(f"读取 selection.json（{len(picks)} 条）")

    # ── 规范化 selected：对象 / "T9" / 9 / 纯文本 → canonical 对象
    if titles.get("selected"):
        norm, notes = _normalize_selected(job_dir, titles.get("file"),
                                          list(titles["selected"]))
        titles["selected"] = norm
        report["normalized"].extend(notes)
    if titles.get("selected_by") not in ("user", "auto"):
        titles["selected_by"] = "user" if titles.get("selected") else None
    if titles.get("selected_by") != "auto":
        titles["auto_mode"] = None
    if titles.get("gate") != "done" or len(titles.get("selected") or []) != 3:
        raise ValueError("titles.gate 未 done 或 selected 不足 3 条：先完成标题门禁再 ingest")

    # ── 竖版绑定：selected 中评分最高的一条（已绑过则不动）
    vc = job.get("vertical_cover") or {}
    if titles.get("selected") and vc.get("title_id") is None:
        best = _best_of_selected(job_dir, titles.get("file"), titles["selected"])
        if best:
            job.setdefault("vertical_cover", {})
            job["vertical_cover"]["title_id"] = best["title_id"]
            job["vertical_cover"]["title"] = best["title"]
            report["normalized"].append(f"vertical_cover 绑定最高分标题 {best['title_id']}")

    # ── 收 prompts
    prompts_path = os.path.join(job_dir, "03_prompts", "prompts.json")
    if os.path.isfile(prompts_path):
        with open(prompts_path, encoding="utf-8") as f:
            pdata = json.load(f)
        items = pdata.get("items") or []
        slots = {it.get("slot") for it in items}
        missing = sorted(({1, 2, 3, "vertical"} - slots), key=str)
        if missing:
            raise ValueError(f"prompts.json 槽位不齐，缺: {missing}")
        new_hash = sha256_file(prompts_path)
        psec = job.setdefault("prompts", {})
        if psec.get("prompt_sha256") is None:
            report["hash_filled"].append("prompts.prompt_sha256")
        elif psec["prompt_sha256"] != new_hash:
            report["normalized"].append("prompts.prompt_sha256 刷新")
        psec.update({"file": "03_prompts/prompts.json",
                     "channel": pdata.get("channel"), "items": items,
                     "count": len(items), "prompt_sha256": new_hash})
        report["prompts"] = {"items": len(items), "channel": pdata.get("channel")}

    # ── 收 covers：先识别并把任意命名的图改名到契约名（agent 不需记文件名）
    found, renames = detect_cover_files(job_dir, apply=not dry_run)
    report["renamed"] = renames

    def _fill(entry: dict[str, Any], path: str, tag: Any) -> None:
        rel = f"04_covers/{os.path.basename(path)}"
        new_hash = sha256_file(path)
        if entry.get("image_sha256") is None:
            report["hash_filled"].append(f"cover[{tag}].image_sha256")
        elif entry["image_sha256"] != new_hash:
            report["normalized"].append(f"cover[{tag}] hash 刷新（文件已更换）")
        entry.update({"file": rel, "image": rel,
                      "image_sha256": new_hash, "status": "done"})
        entry.setdefault("generated_at", now_iso())
        report["covers"].append(tag)

    for cm in job.get("cover_map") or []:
        p = found.get(cm.get("slot"))
        if p:
            _fill(cm, p, cm["slot"])
        else:
            report["missing_covers"].append(cm.get("slot"))
    vp = found.get("vertical")
    if vp:
        _fill(job.setdefault("vertical_cover", {}), vp, "vertical")
    else:
        report["missing_covers"].append("vertical")

    # ── 参考图 hash（null 且文件在则补）
    inp = job.get("input") or {}
    ref = inp.get("reference_image")
    if ref and inp.get("reference_sha256") is None:
        rp = os.path.join(job_dir, ref)
        if os.path.isfile(rp):
            inp["reference_sha256"] = sha256_file(rp)
            report["hash_filled"].append("input.reference_sha256")

    # ── stage 推进（只进不退）
    order = ["titles_selected", "prompts_ready", "covers_ready"]
    new_stage = stage
    horizontal = [c for c in report["covers"] if c in (1, 2, 3)]
    if report["prompts"]:
        new_stage = "prompts_ready"
        if len(horizontal) == 3 and "vertical" in report["covers"]:
            new_stage = "covers_ready"
    if order.index(new_stage) < order.index(stage):
        new_stage = stage
    job["stage"] = new_stage
    report["stage_to"] = new_stage

    # ── 重选标记清除：4 张契约封面（3 横 + 竖）已重建
    derived = job.get("derived")
    if isinstance(derived, dict) and derived.get("covers_stale") \
            and len(horizontal) == 3 and "vertical" in report["covers"]:
        derived["covers_stale"] = False
        derived["reason"] = None
        report["normalized"].append("derived.covers_stale 清除（封面已重建）")

    # ── dispatch.json → workflow 归并（意图收口；intent_revision 程序只读）
    try:
        report["workflow"] = merge_workflow_core(job, job_dir)
    except ValueError as e:
        report["workflow"] = {"error": str(e)}

    if dry_run:
        report["dry_run"] = True
        return report
    job["revision"] = int(job.get("revision") or 0) + 1
    job.setdefault("history", []).append({
        "at": now_iso(), "event": "ingest",
        "detail": f"covers={len(report['covers'])}/4 "
                  f"missing={report['missing_covers']} stage {stage}->{new_stage}",
        "revision": job["revision"]})
    errors = validate_job(job, job_dir)
    if errors:
        raise ValueError("ingest 后校验失败: " + "；".join(errors))
    save_job(job, job_dir)
    report["validate"] = "ok"
    return report


PUBLISH_PLATFORMS = ("youtube", "tiktok")
# job.json 里平台状态用校验允许的枚举（pending/uploading/succeeded/failed/blocked）；
# 06_publish/<platform>.json 与回执文件里按契约写 status=published。
PLATFORM_OK_STATUS = "succeeded"


def _publish_key(series: str, platform: str, video_id: Optional[str],
                 url: Optional[str]) -> str:
    raw = f"{series}|{platform}|{video_id or ''}|{url or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _write_json(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def publish_record(
    job_dir: str,
    platform: str,
    *,
    video_id: Optional[str] = None,
    url: Optional[str] = None,
    uploaded_at: Optional[str] = None,
    cover_file: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """手工登记一条发布回执（发布流程尚未自动化时的正式入口）。

    写 `06_publish/<platform>.json` + `06_publish/receipts/<platform>-<时间戳>.json`，
    并把 `job.json` 的 `publish.platforms` / `publish.receipts` 补齐；至少一个平台成功即
    `publish.status=published`。stage 不强推，仅当已处于 publish_ready/published 时才置 published。
    同平台同 video_id（无 id 时同 url）重复登记不重复追加，返回 duplicate=True、不改 revision。
    """
    platform = (platform or "").strip().lower()
    if not platform:
        raise ValueError("platform 不能为空")
    video_id = (video_id or "").strip() or None
    url = (url or "").strip() or None
    if not video_id and not url:
        raise ValueError("--video-id 与 --url 至少提供一个")

    job = load_job(os.path.join(job_dir, "job.json"))
    series = job.get("series") or ""
    stamp = uploaded_at or now_iso()
    key = _publish_key(series, platform, video_id, url)

    publish = job.get("publish") or {}
    platforms = list(publish.get("platforms") or [])
    for p in platforms:
        same_name = str(p.get("name") or "").lower() == platform
        if not same_name:
            continue
        if video_id and str(p.get("video_id") or "") == video_id:
            return {"duplicate": True, "platform": platform, "video_id": video_id,
                    "revision": job.get("revision"), "stage": job.get("stage"),
                    "files": []}
        if not video_id and url and str(p.get("url") or "") == url:
            return {"duplicate": True, "platform": platform, "video_id": None,
                    "revision": job.get("revision"), "stage": job.get("stage"),
                    "files": []}

    receipts_dir = os.path.join(job_dir, "06_publish", "receipts")
    os.makedirs(receipts_dir, exist_ok=True)
    base_name = f"{platform}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    receipt_name = f"{base_name}.json"
    n = 2
    while os.path.exists(os.path.join(receipts_dir, receipt_name)):
        receipt_name = f"{base_name}-{n}.json"
        n += 1
    receipt_rel = f"06_publish/receipts/{receipt_name}"
    platform_rel = f"06_publish/{platform}.json"

    entry = {
        "name": platform,
        "status": PLATFORM_OK_STATUS,
        "video_id": video_id,
        "url": url,
        "cover_file": rel_path(cover_file, job_dir) if cover_file else None,
        "attempts": 1,
        "error": None,
        "uploaded_at": stamp,
        "note": note,
    }
    platforms.append(entry)
    receipts = list(publish.get("receipts") or [])
    receipts.append({"platform": platform, "file": receipt_rel,
                     "video_id": video_id, "url": url,
                     "uploaded_at": stamp, "idempotency_key": key})

    patch: dict[str, Any] = {
        "publish": {"status": "published", "platforms": platforms,
                    "receipts": receipts}
    }
    cur_stage = job.get("stage")
    if cur_stage in ("publish_ready", "published"):
        patch["stage"] = "published"
    job = update_job(job_dir, patch, event="publish_record",
                     detail=f"{platform} video_id={video_id or '-'} url={url or '-'}")

    payload = {"version": 1, "name": platform, "status": "published",
               "video_id": video_id, "url": url,
               "cover_file": entry["cover_file"], "attempts": 1,
               "uploaded_at": stamp, "note": note,
               "idempotency_key": key, "recorded_at": now_iso()}
    _write_json(os.path.join(job_dir, platform_rel), payload)
    _write_json(os.path.join(job_dir, receipt_rel), payload)
    return {"duplicate": False, "platform": platform, "video_id": video_id,
            "url": url, "revision": job["revision"], "stage": job["stage"],
            "idempotency_key": key, "files": [platform_rel, receipt_rel]}


def create_job(
    *,
    series: Optional[str] = None,
    synopsis: Optional[str] = None,
    series_dir: Optional[str] = None,
    reference: Optional[str] = None,
    video: Optional[str] = None,
    out_dir: Optional[str] = None,
    host: str = "trae",
    force: bool = False,
) -> dict[str, Any]:
    """创建任务目录与 job.json。默认目录为 <剧集目录>/cover_job。"""
    if series is None and series_dir:
        series = os.path.basename(os.path.normpath(series_dir))
    series = sanitize_name(series or "未命名剧集")
    slug = sanitize_name(series)

    if out_dir:
        target = os.path.abspath(out_dir)
    elif series_dir:
        target = os.path.join(os.path.abspath(series_dir), "cover_job")
    else:
        target = os.path.abspath(os.path.join("jobs", slug))

    job_path = os.path.join(target, "job.json")
    if os.path.exists(job_path) and not force:
        raise FileExistsError(f"{job_path} 已存在；如需重建请加 --force")

    for rel in JOB_DIRS:
        os.makedirs(os.path.join(target, rel), exist_ok=True)

    synopsis_info = {"path": None, "candidates": []}
    if not synopsis and series_dir:
        synopsis_info = detect_synopsis(series_dir)
        if synopsis_info["path"]:
            with open(synopsis_info["path"], encoding="utf-8") as f:
                synopsis = f.read().strip()
    if not synopsis:
        raise ValueError("简介为空；请提供 --synopsis 或在剧集目录放置 简介.txt")

    with open(os.path.join(target, "01_input", "synopsis.txt"), "w", encoding="utf-8") as f:
        f.write(synopsis.strip() + "\n")

    if reference:
        ref_path = os.path.abspath(reference)
        if not os.path.isfile(ref_path):
            raise FileNotFoundError(f"参考图不存在: {reference}")
        ref_info = {"path": ref_path, "source": os.path.basename(ref_path),
                    "reason": "显式指定"}
    elif series_dir:
        ref_info = detect_reference_image(series_dir)
    else:
        ref_info = {"path": None, "source": None, "reason": "未提供剧集目录"}

    if video:
        video_path = os.path.abspath(video)
        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"视频不存在: {video}")
        video_info = {"path": video_path, "count": 1, "pattern": None}
    elif series_dir:
        video_info = detect_video(series_dir)
    else:
        video_info = {"path": None, "count": 0, "pattern": None}

    job = build_job(series, host=host)

    if synopsis_info["path"]:
        job["input"]["synopsis_source"] = rel_path(synopsis_info["path"], target)
    elif series_dir:
        job["input"]["synopsis_source"] = "cli"
    job["input"]["synopsis_candidates"] = [
        rel_path(p, target) for p in synopsis_info["candidates"]
    ]

    if ref_info.get("path"):
        suffix = os.path.splitext(ref_info["path"])[1].lower() or ".jpg"
        rel_ref = f"01_input/reference{suffix}"
        shutil.copy2(ref_info["path"], os.path.join(target, rel_ref))
        job["input"]["reference_image"] = rel_ref
        job["input"]["reference_original"] = rel_path(ref_info["path"], target)
        job["input"]["reference_sha256"] = sha256_file(ref_info["path"])
    job["input"]["reference_source"] = ref_info.get("source")
    job["input"]["reference_reason"] = ref_info.get("reason")

    if video_info.get("path"):
        job["video"]["path"] = rel_path(video_info["path"], target)
        job["video"]["source"] = os.path.basename(video_info["path"])
    job["video"]["candidates_count"] = video_info.get("count", 0)
    job["video"]["name_pattern"] = video_info.get("pattern")

    job["history"].append({
        "at": now_iso(),
        "event": "prepare",
        "detail": "创建目录，写入简介与参考图",
        "revision": 1,
    })

    errors = validate_job(job, target)
    if errors:
        raise ValueError("job.json 校验失败: " + "；".join(errors))

    save_job(job, target)
    return {
        "job_dir": target,
        "job_path": job_path,
        "job": job,
        "reference": ref_info,
        "synopsis": synopsis_info,
        "video": video_info,
    }


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="job_manifest", description="job.json 工具")
    sub = ap.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="校验现有 job.json")
    p_val.add_argument("--job", required=True)
    p_val.add_argument("--fix", action="store_true",
                       help="先修复常见不合规字段（gate/selected 规范化、补算 null hash）再校验")

    p_upd = sub.add_parser("update", help="深合并补丁并推进 stage")
    p_upd.add_argument("--job", required=True)
    p_upd.add_argument("--patch-file", required=True)
    p_upd.add_argument("--event", default="update")
    p_upd.add_argument("--detail", default="")

    p_tit = sub.add_parser("record-titles", help="登记 Stage 1 的 12 条标题")
    p_tit.add_argument("--job", required=True)
    p_tit.add_argument("--titles", required=True)
    p_tit.add_argument("--prompt", default=None)
    p_tit.add_argument("--prompt-version", default=None)

    p_sel = sub.add_parser("select", help="人工门禁：按 slot 顺序给 3 个编号")
    p_sel.add_argument("--job", required=True)
    p_sel.add_argument("--picks", required=True,
                       help="逗号分隔 3 个编号，依次为封面1/2/3，如 T6,T11,T1")

    p_res = sub.add_parser("resolve", help="Stage 2 取上下文（含兜底链）")
    p_res.add_argument("--job", required=True)
    p_res.add_argument("--no-auto", action="store_true",
                       help="禁止自动选分，门禁未过直接报错")
    p_res.add_argument("--mode", choices=["recommended", "score"],
                       default="recommended",
                       help="兜底模式：recommended（默认，推荐池优先）/ score（全量评分）")
    p_res.add_argument("--dry-run", action="store_true",
                       help="只演算不写回 job.json")

    p_vid = sub.add_parser("record-video", help="登记 pipeline 导出目录与清理联动")
    p_vid.add_argument("--job", required=True)
    p_vid.add_argument("--export-dir", default=None)
    p_vid.add_argument("--export-file", default=None)
    p_vid.add_argument("--draft-name", default=None)
    p_vid.add_argument("--dry-run", action="store_true")

    p_drv = sub.add_parser("derive", help="从 04_covers 派生平台尺寸到 derived/")
    p_drv.add_argument("--job", required=True)
    p_drv.add_argument("--platform", default=None,
                       help="逗号分隔：youtube,tiktok,ig,facebook 或 all；也可用下方的快捷标志")
    p_drv.add_argument("--youtube", action="store_true", help="= youtube（1280×720 <2MB）")
    p_drv.add_argument("--tiktok", action="store_true", help="= tiktok（1080×1920）")
    p_drv.add_argument("--ig", action="store_true", help="= instagram-reels（1080×1920）")
    p_drv.add_argument("--facebook", action="store_true", help="= facebook-reels（1080×1920）")
    p_drv.add_argument("--slot", type=int, default=1, choices=[1, 2, 3],
                       help="横版源用第几个封面位（默认 1）")

    p_ing = sub.add_parser("ingest", help="Stage 2 收口：收 prompts+covers、规范化、补 hash、推进 stage")
    p_ing.add_argument("--job", required=True)
    p_ing.add_argument("--dry-run", action="store_true")

    p_cut = sub.add_parser("set-cut", help="设置/清除导出裁剪秒数 video.cut_s（默认 null 不裁剪）")
    p_cut.add_argument("--job", required=True)
    p_cut.add_argument("--cut", required=True,
                       help="正数秒数（≤ duration_s）；传 0 或 null 清除为不裁剪")

    p_pub = sub.add_parser("publish-record",
                           help="手工登记发布回执（写 06_publish/<platform>.json + receipts/）")
    p_pub.add_argument("--job", required=True)
    p_pub.add_argument("--platform", required=True,
                       choices=list(PUBLISH_PLATFORMS))
    p_pub.add_argument("--video-id", default=None, help="平台视频 id")
    p_pub.add_argument("--url", default=None, help="平台视频链接")
    p_pub.add_argument("--uploaded-at", default=None,
                       help="ISO 时间，缺省为当前时间")
    p_pub.add_argument("--cover-file", default=None, help="该平台实际使用的封面")
    p_pub.add_argument("--note", default=None, help="备注")

    args = ap.parse_args(argv)
    job_dir = os.path.dirname(os.path.abspath(args.job))
    if os.path.isdir(args.job):
        job_dir = os.path.abspath(args.job)

    if args.command == "validate":
        if args.fix:
            try:
                result = fix_job(job_dir)
            except ValueError as e:
                print(f"[FAIL] {e}")
                return 1
            for item in result["fixes"]:
                reason = f"（{item['reason']}）" if item.get("reason") else ""
                print(f"[fix] {item['field']}: {item['old']} → {item['new']}{reason}")
            for w in result["warnings"]:
                print(f"[warn] {w}")
            print(f"fixed: {result['fixed']}")
        job = load_job(os.path.join(job_dir, "job.json"))
        errors = validate_job(job, job_dir)
        if errors:
            print("[FAIL] " + "；".join(errors))
            return 1
        print("[OK] job.json 通过校验")
        return 0

    if args.command == "update":
        with open(args.patch_file, encoding="utf-8") as f:
            patch = json.load(f)
        job = update_job(job_dir, patch, event=args.event, detail=args.detail)
        print(f"[OK] revision={job['revision']} stage={job['stage']}")
        return 0

    if args.command == "record-titles":
        job = record_titles(job_dir, args.titles, args.prompt, args.prompt_version)
        print(f"[OK] revision={job['revision']} stage={job['stage']} gate={job['titles']['gate']}")
        return 0

    if args.command == "select":
        ids = [s.strip() for s in args.picks.split(",") if s.strip()]
        if len(ids) != 3:
            print("[FAIL] --picks 需要恰好 3 个编号，如 T6,T11,T1")
            return 1
        titles_file = os.path.join(
            job_dir, (load_job(os.path.join(job_dir, "job.json"))
                      .get("titles") or {}).get("file") or "02_titles/titles.json")
        items = {str(t.get("id")): t for t in _load_titles_items(titles_file)}
        picks = []
        for slot, tid in enumerate(ids, start=1):
            if tid not in items:
                print(f"[FAIL] 编号不存在: {tid}")
                return 1
            picks.append({"slot": slot, "title_id": tid,
                          "title": items[tid]["text"]})
        job = select_titles(job_dir, picks, by="user")
        print(f"[OK] revision={job['revision']} stage={job['stage']} gate=done "
              f"selected={','.join(ids)}")
        return 0

    if args.command == "resolve":
        try:
            ctx = resolve_context(job_dir, auto=not args.no_auto,
                                  dry_run=args.dry_run, mode=args.mode)
        except (ValueError, FileNotFoundError) as e:
            print(f"[FAIL] {e}")
            return 1
        print(json.dumps(ctx, ensure_ascii=False, indent=2))
        return 0

    if args.command == "record-video":
        try:
            result = record_video(job_dir, export_dir=args.export_dir,
                                  export_file=args.export_file,
                                  draft_name=args.draft_name,
                                  dry_run=args.dry_run)
        except ValueError as e:
            print(f"[FAIL] {e}")
            return 1
        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[OK] revision={result['revision']} stage={result['stage']} "
                  f"export_dir={result['video']['export_dir']}")
        return 0

    if args.command == "derive":
        alias = {"youtube": "youtube", "tiktok": "tiktok",
                 "ig": "instagram-reels", "facebook": "facebook-reels"}
        plats = [v for k, v in alias.items() if getattr(args, k)]
        if args.platform:
            plats += [alias.get(p.strip(), p.strip())
                      for p in args.platform.split(",") if p.strip()]
        if "all" in plats:
            plats = list(DERIVE_PRESETS)
        plats = list(dict.fromkeys(plats))
        if not plats:
            print("[FAIL] 至少指定一个平台：--platform tiktok 或 --tiktok")
            return 1
        try:
            result = derive_covers(job_dir, plats, slot=args.slot)
        except (ValueError, FileNotFoundError, RuntimeError) as e:
            print(f"[FAIL] {e}")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ingest":
        try:
            result = ingest_job(job_dir, dry_run=args.dry_run)
        except (ValueError, FileNotFoundError, json.JSONDecodeError) as e:
            print(f"[FAIL] {e}")
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "set-cut":
        raw = str(args.cut).strip().lower()
        if raw in ("0", "null", "none"):
            cut: Optional[float] = None
        else:
            try:
                cut = float(raw)
            except ValueError:
                print(f"[FAIL] --cut 无法解析为数字: {args.cut}")
                return 1
            if cut <= 0:
                print("[FAIL] --cut 必须为正数秒数；要清除请传 0 或 null")
                return 1
        try:
            job = update_job(job_dir, {"video": {"cut_s": cut}},
                             event="set_cut",
                             detail=f"video.cut_s={cut}")
        except ValueError as e:
            print(f"[FAIL] {e}")
            return 1
        print(f"[OK] revision={job['revision']} video.cut_s={job['video']['cut_s']}")
        return 0

    if args.command == "publish-record":
        try:
            result = publish_record(job_dir, args.platform,
                                    video_id=args.video_id, url=args.url,
                                    uploaded_at=args.uploaded_at,
                                    cover_file=args.cover_file, note=args.note)
        except (ValueError, FileNotFoundError) as e:
            print(f"[FAIL] {e}")
            return 1
        if result["duplicate"]:
            print(f"[SKIP] 已登记过（{result['platform']} video_id={result['video_id']}"
                  f"），未重复追加，revision={result['revision']} 未变")
            return 0
        print(f"[OK] revision={result['revision']} stage={result['stage']} "
              f"publish.status=published idempotency_key={result['idempotency_key']}")
        for f in result["files"]:
            print(f"  {f}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_main())
