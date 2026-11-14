# -*- coding: utf-8 -*-
"""draft_content.json 的唯一写出原语 + 时长不变量（全仓共用）。

所有写 draft_content.json 的地方都必须经由 write_draft_content：
断言校验 → 备份 → 原子写 → 新鲜度留痕，四步顺序固定，不允许各脚本各写一份。

时间口径统一为「帧」：任何时长比较先 ceil 到整帧，允许 1 帧取整误差
（1µs 也可能跨过 ceil 边界），超出 1 帧才判「不足」。
"""
import json
import math
import os
import shutil
import sys
import time

DEFAULT_FPS = 30.0
_FRAME_SLACK = 1   # 允许的帧数取整误差


class DraftInvariantError(RuntimeError):
    """草稿落盘前不变量校验失败：宁可拒绝落盘，也不产出剪映会截断/漂移的草稿。"""


# ━━━━━━━━━━━━━━━━ 时长/帧 ━━━━━━━━━━━━━━━━

def _fps_of(draft):
    try:
        fps = float((draft or {}).get("fps") or DEFAULT_FPS)
    except (TypeError, ValueError):
        fps = DEFAULT_FPS
    return fps if fps > 0 else DEFAULT_FPS


def frame_count(duration_us, fps=DEFAULT_FPS):
    """时长 → 帧数（向上取整）。全部时长比较共用这一种口径。"""
    try:
        fps = float(fps or DEFAULT_FPS)
    except (TypeError, ValueError):
        fps = DEFAULT_FPS
    if fps <= 0:
        fps = DEFAULT_FPS
    return math.ceil(int(duration_us) * fps / 1_000_000)


def ceil_to_frame_us(duration_us, fps=DEFAULT_FPS):
    """微秒时长向上取整到整帧（写回 JSON 用）。"""
    try:
        fps = float(fps or 0)
    except (TypeError, ValueError):
        fps = 0.0
    if duration_us <= 0 or fps <= 0:
        return int(duration_us)
    frames = math.ceil(int(duration_us) * fps / 1_000_000)
    return int(round(frames * 1_000_000 / fps))


# ━━━━━━━━━━━━━━━━ 素材时长索引 ━━━━━━━━━━━━━━━━

def material_durations(draft):
    """素材 ID → 真实时长(us)。组合片段取内层草稿 duration。"""
    idx = {}
    mats = draft.get("materials") or {}
    for key, lst in mats.items():
        if key == "drafts" or not isinstance(lst, list):
            continue
        for m in lst:
            if not isinstance(m, dict):
                continue
            mid, dur = m.get("id"), m.get("duration")
            if mid and isinstance(dur, (int, float)) and dur > 0:
                idx[mid] = int(dur)
    idx.update(inner_draft_durations(draft))
    return idx


def inner_draft_durations(draft):
    """组合片段包装 ID → 内层草稿 duration(us)。"""
    out = {}
    for m in (draft.get("materials") or {}).get("drafts") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        inner = m.get("draft")
        if isinstance(inner, dict) and isinstance(inner.get("duration"), (int, float)):
            out[m["id"]] = int(inner["duration"])
    return out


# ━━━━━━━━━━━━━━━━ 落盘前不变量 ━━━━━━━━━━━━━━━━

def _collect_violations(draft, where, out, depth=0):
    if not isinstance(draft, dict) or depth > 5:
        return
    fps = _fps_of(draft)
    durations = material_durations(draft)
    inner_durations = inner_draft_durations(draft)
    for ti, track in enumerate(draft.get("tracks") or []):
        for si, seg in enumerate(track.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            pos = f"{where}: tracks[{ti}].segments[{si}]"
            src = seg.get("source_timerange") or {}
            src_dur = int(src.get("duration") or 0)
            src_frames = frame_count(src_dur, fps)
            mid = seg.get("material_id")
            mat_dur = durations.get(mid)
            if mat_dur:
                mat_frames = frame_count(mat_dur, fps)
                if src_frames - mat_frames > _FRAME_SLACK:
                    out.append(
                        f"{pos} source {src_dur/1e6:.3f}s = {src_frames} 帧 > 素材 "
                        f"{str(mid)[:8]} {mat_dur/1e6:.3f}s = {mat_frames} 帧"
                        f"（超出 {src_frames - mat_frames} 帧）")
            tgt = seg.get("target_timerange") or {}
            seg_frames = frame_count(max(int(tgt.get("duration") or 0), src_dur), fps)
            for ref in seg.get("extra_material_refs") or []:
                inner_dur = inner_durations.get(ref)
                if not inner_dur:
                    continue
                inner_frames = frame_count(inner_dur, fps)
                if seg_frames - inner_frames > _FRAME_SLACK:
                    out.append(
                        f"{pos} 复合段 {seg_frames} 帧 > 内层草稿 {str(ref)[:8]} "
                        f"{inner_frames} 帧（超出 {seg_frames - inner_frames} 帧）")
    for i, mat in enumerate((draft.get("materials") or {}).get("drafts") or []):
        if isinstance(mat, dict) and isinstance(mat.get("draft"), dict):
            _collect_violations(
                mat["draft"], f"{where} > materials.drafts[{i}]", out, depth + 1)


def assert_draft_invariants(draft, where="draft_content.json"):
    """落盘前不变量校验（所有 draft_content.json 写盘点的统一门禁）。

    ① 段 source_timerange 不得超过其引用素材的真实 duration；
    ② 组合段（引用 materials.drafts）的段时长不得超过内层草稿 duration。
    两边按帧比较（允许 1 帧取整误差）；递归校验内层草稿；超标抛 DraftInvariantError。
    """
    violations = []
    _collect_violations(draft, where, violations)
    if violations:
        shown = "\n".join(f"  - {v}" for v in violations[:10])
        more = f"\n  ... 共 {len(violations)} 条" if len(violations) > 10 else ""
        raise DraftInvariantError(
            f"草稿不变量校验失败，拒绝落盘（{where}）：\n{shown}{more}")


# ━━━━━━━━━━━━━━━━ 唯一写出实现 ━━━━━━━━━━━━━━━━

def _record_freshness(job_dir, draft_path, writer):
    """写时确权（best-effort：job 找不到时内部 no-op，异常不外抛）。"""
    try:
        lib = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        import freshness
        if writer == "cover_only":
            # 只换封面、时间线未变：E2 不得置 stale，走封面专用留痕
            freshness.record_cover_write(job_dir, draft_path, writer)
        else:
            freshness.record_draft_write(job_dir, draft_path, writer)
    except Exception:
        pass


def write_draft_content(draft, path, *, job_dir=None, writer=None,
                        backup_suffix=None, keep_existing_backup=False,
                        indent=None, validate=True):
    """全仓唯一的 draft_content.json 写出实现。

    顺序固定：断言校验 → 备份（可选）→ 原子替换 → 新鲜度留痕（可选）。

    backup_suffix: 备份文件名后缀模板，可含 {ts}，如 ".repairbak_{ts}"；
                   为空表示不备份。
    keep_existing_backup: 备份文件已存在时不覆盖（framefix 的 .pre_*.json 语义）。
    indent: json 缩进（None = 不缩进，保持各调用点原有格式）。
    validate: 是否做不变量校验（只有 mock/云端副本这类非正式草稿可关闭）。
    返回实际生成的备份路径；未备份返回 None。
    """
    if validate:
        assert_draft_invariants(draft, where=path)
    bak = None
    if backup_suffix:
        bak_path = path + backup_suffix.format(ts=int(time.time()))
        if keep_existing_backup and os.path.exists(bak_path):
            bak_path = None
        elif os.path.exists(path):
            shutil.copy2(path, bak_path)
            bak = bak_path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if indent is None:
                json.dump(draft, f, ensure_ascii=False)
            else:
                json.dump(draft, f, ensure_ascii=False, indent=indent)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if writer:
        _record_freshness(job_dir, path, writer)
    return bak
