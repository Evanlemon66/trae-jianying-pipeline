# -*- coding: utf-8 -*-
"""
初始化测试草稿（复用 pipeline 标准流程）—— 供 ui_pipeline [0] 步与 admission_test 直接调用

流程：
  1. create_empty_draft(staging=True)  → 暂存区(__jybuild__)构建干净草稿，剪映不可见
  2. inject_videos_into_draft          → 注入指定目录的视频到主轨
  3. merge_preset_into_draft           → 合并预设（拉伸到总时长）
  4. save_draft                        → 保存到暂存区
  5. publish_staged_draft              → shutil.move 原子发布到 com.lveditor.draft + 注册 root_meta_info
  6. ui_nav.open_draft                 → 在剪映中打开草稿

函数调用：
  from init_draft import init_draft
  init_draft()                                  # 全默认（测试草稿）
  init_draft(draft_name='X', video_dir=r'D:\path\to\videos') # 自定义

CLI：
  py -3.12 pipeline_ui/init_draft.py
  py -3.12 pipeline_ui/init_draft.py --draft-name X --video-dir "D:\\..." --limit 5 --no-open
"""
import os, sys, glob, time, unicodedata, re

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, '..', 'pipeline')))

from pipeline import (
    create_empty_draft,
    inject_videos_into_draft,
    merge_preset_into_draft,
    publish_staged_draft,
    find_preset_by_name,
    resolve_presets,
    save_draft,
    write_preset_manifest,
    DRAFT_ROOT,
)

# ====== 默认配置（草稿名 = 视频目录名=剧名）======
VIDEO_DIR = r"D:\剪映5.9\test\赶山救下小狐狸，所有猛兽都听我的"
DRAFT_NAME = os.path.basename(VIDEO_DIR.rstrip('\\/'))
# 预设默认从 pipeline/presets.json 读取（可用 profile 选 presets_<组名>）；
# 传 presets=[("名", alpha), ...] 可显式覆盖
VIDEO_GLOB = "第*集.mp4"
VIDEO_LIMIT = 5                          # 取前 N 集，None 表示全部
# ================================


def natural_sort_key(s):
    import re
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', s)]


def _probe_duration_sec(path):
    """ffprobe 时长（秒），失败返回 0。"""
    try:
        from utils.formatters import get_duration_ffprobe_cached
        return float(get_duration_ffprobe_cached(path) or 0)
    except Exception as e:
        print(f"  [warn] ffprobe 失败 {os.path.basename(path)}: {e}")
        return 0.0


def _main_track_segments(draft):
    for tr in draft.get('tracks') or []:
        if tr.get('type') == 'video':
            return tr.get('segments') or []
    return []


def _validate_built_draft(draft, episodes, expected_us):
    """构建后校验：主轨段数=视频数、轨道数>1、总时长与源文件一致。返回错误列表。"""
    errs = []
    segs = _main_track_segments(draft)
    if len(segs) != len(episodes):
        errs.append(f'主轨段数 {len(segs)} != 视频数 {len(episodes)}')
    n_tracks = len(draft.get('tracks') or [])
    if n_tracks <= 1:
        errs.append(f'轨道数 {n_tracks} <= 1（预设未合并成功？）')
    got_us = int(draft.get('duration') or 0)
    if expected_us > 0:
        diff_us = abs(got_us - expected_us)
        tol_us = max(2_000_000, int(expected_us * 0.01))
        if diff_us > tol_us:
            errs.append(f'总时长 {got_us/1e6:.1f}s 与源文件 {expected_us/1e6:.1f}s '
                        f'偏差 {diff_us/1e6:.1f}s（容差 {tol_us/1e6:.1f}s）')
    return errs


def _wait_target_quiesce(content_path, stable_rounds=2, probe_gap=1.0, max_wait=12.0):
    """发布前等待剪映 autosave 落盘：目标 draft_content.json 的 mtime 连续 stable_rounds 次探测不变。
    目标不存在（首次发布）直接返回，零开销。"""
    if not os.path.exists(content_path):
        return
    t0 = time.time()
    last = os.path.getmtime(content_path)
    stable = 0
    while stable < stable_rounds and time.time() - t0 < max_wait:
        time.sleep(probe_gap)
        try:
            m = os.path.getmtime(content_path)
        except OSError:
            return
        if m == last:
            stable += 1
        else:
            stable, last = 0, m
    print(f"   静默检查通过: 目标草稿 mtime 已稳定（{time.time()-t0:.1f}s）")


def parse_video_range(spec, total):
    """把区间字符串解析为 1-based 集号列表（升序去重）。
    支持：'5'（单集）、'5-15'（第5到15集，含端点）、'5-'（第5集到末尾）、
    '-10'（前10集）、'1,3,5-8'（逗号组合）。越界集号自动忽略并提示。
    非法格式抛 ValueError。"""
    picked = set()
    for raw in str(spec).split(','):
        part = raw.strip()
        if not part:
            continue
        m = re.fullmatch(r'(\d*)-(\d*)', part)   # 同时覆盖 '5-15' / '5-' / '-10'
        if m:
            lo = int(m.group(1)) if m.group(1) else 1
            hi = int(m.group(2)) if m.group(2) else total
            if lo < 1 or hi < lo:
                raise ValueError(f'非法区间: {part!r}')
            picked.update(range(lo, hi + 1))
        elif part.isdigit():
            picked.add(int(part))
        else:
            raise ValueError(f'非法集号: {part!r}')
    out = sorted(i for i in picked if 1 <= i <= total)
    if not out:
        raise ValueError(f'区间 {spec!r} 未命中任何集（共 {total} 集）')
    if len(out) < len(picked):
        print(f"  ⚠️ 区间 {spec!r} 有 {len(picked) - len(out)} 个集号超出范围（共 {total} 集），已忽略")
    return out


def init_draft(draft_name: str = DRAFT_NAME,
               video_dir: str = VIDEO_DIR,
               presets=None,
               preset_profile: str = None,
               video_glob: str = VIDEO_GLOB,
               video_limit=VIDEO_LIMIT,
               video_range: str = None,
               cut_s: float = None,
               normalize: bool = False,
               open_after: bool = True) -> dict:
    """构建并（可选）打开测试草稿。返回 {'ok': bool, 'episodes': int, 'duration_s': float, 'error': str|None}
    normalize=False 时直接引用原始视频（HEVC/非常规尺寸也不重编码，剪映原生支持），省去逐集编码。"""
    if presets is None:
        try:
            presets, _src = resolve_presets(preset_profile)
        except RuntimeError as e:
            print(f"❌ {e}")
            return {'ok': False, 'episodes': 0, 'duration_s': 0, 'error': str(e)}
        if _src:
            print(f"🧩 预设来源: {_src}")
    from ui_core import op_stage, op_stage_end
    op_stage('1 生成草稿（包含预设+时间线裁剪）')

    # ── 分段计时：本步骤几乎全是非 UI 工作（ffprobe/文件发布/等编辑器），
    #    op_mark 只有 1 个点击，光看操作时间线会误以为不耗时 ──
    _phases = []
    _t_phase = [time.time()]

    def _phase(name):
        now = time.time()
        _phases.append((name, now - _t_phase[0]))
        print(f"   ⏱ {name}: {now - _t_phase[0]:.1f}s")
        _t_phase[0] = now

    # ── Step 1: 扫视频 ──
    episodes = sorted(
        glob.glob(os.path.join(video_dir, video_glob)),
        key=lambda p: natural_sort_key(os.path.basename(p)),
    )
    if not episodes:
        return {'ok': False, 'episodes': 0, 'duration_s': 0,
                'error': f'没找到视频: {video_dir}/{video_glob}'}
    if video_range:
        idxs = parse_video_range(video_range, len(episodes))   # 非法区间直接抛错终止
        episodes = [episodes[i - 1] for i in idxs]
        print(f"📼 video_range={video_range} → 取第 {idxs[0]}~{idxs[-1]} 集共 {len(episodes)} 个")
    if video_limit:
        episodes = episodes[:video_limit]
    print(f"📼 取 {len(episodes)} 个视频")
    _phase('扫视频')

    # ── Step 2: 暂存区构建干净草稿（剪映不可见）──
    print(f"\n📝 暂存区构建草稿: {draft_name}")
    stage_dir, jp_new, draft = create_empty_draft(draft_name, staging=True)
    _phase('构建草稿骨架')
    if not stage_dir:
        return {'ok': False, 'episodes': 0, 'duration_s': 0,
                'error': '创建草稿失败（未找到模板草稿 default/pipeline_test/demo_test）'}

    # ── Step 3: 注入视频 + 合并预设 ──
    print(f"\n🎬 注入视频")
    episodes_info = inject_videos_into_draft(draft, episodes, normalize=normalize)
    _phase(f'注入视频（ffprobe {len(episodes)} 集）')
    if not normalize:
        # 直接引用原始视频时素材尺寸混杂（如 540×960 与 1080×1920），
        # 与手动导入对齐：画布固定 1080×1920，剪映对 scale=1.0 的素材自动适配。
        draft['canvas_config'] = {'height': 1920, 'ratio': 'original', 'width': 1080}
        print(f"   画布: 1080x1920（对齐手动导入）")
    total_us = draft.get("duration", 0)
    print(f"   总时长: {total_us/1e6:.1f}s")

    preset_manifest = {"profile": preset_profile, "presets": [], "tracks": [], "materials": {},
                       "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    for pn, alpha in presets:
        pp = find_preset_by_name(pn)
        if not pp:
            print(f"  ⚠️ 未找到预设: {pn}，跳过")
            continue
        print(f"\n🎨 合并预设: {pn}" + (f" (clip.alpha={alpha})" if alpha is not None else ""))
        one = {}
        merge_preset_into_draft(draft, pp, stretch_to_duration_us=total_us,
                                clip_alpha=alpha, out_manifest=one)
        preset_manifest["presets"].append({"name": pn, "alpha": alpha})
        preset_manifest["tracks"].extend(one.get("tracks") or [])
        for _mk, _ids in (one.get("materials") or {}).items():
            preset_manifest["materials"].setdefault(_mk, []).extend(_ids)
    _phase('合并预设')

    # ── Step 3.5: 构建期校验（段数 / 轨道数 / 总时长）──
    if not normalize:
        # 不编码时注入的就是原文件，直接复用注入阶段已探测的时长（省 80 次 ffprobe，~1-2 分钟）
        src_durations = [e['duration_us'] / 1e6 for e in episodes_info]
    else:
        src_durations = [_probe_duration_sec(p) for p in episodes]
    expected_us = int(round(sum(src_durations) * 1e6))
    print(f"   源文件总时长: {expected_us/1e6:.1f}s（{', '.join(f'{d:.1f}' for d in src_durations)}）")
    errs = _validate_built_draft(draft, episodes, expected_us)
    for msg in errs:
        print(f"  ❌ 校验失败: {msg}")
    if errs:
        return {'ok': False, 'episodes': len(episodes_info),
                'duration_s': round(total_us / 1e6, 1), 'error': '; '.join(errs)}
    print(f"  ✅ 校验通过: 主轨 {len(episodes_info)} 段 / 轨道 {len(draft.get('tracks') or [])} 条 / "
          f"总时长 {total_us/1e6:.1f}s")
    _phase('构建期校验')

    # ── Step 3.6: 时间线裁剪（发布前执行——剪映尚未打开本稿，写入不会被覆盖；
    #    旧方案在 [2] 改盘上 JSON，会被打开中草稿的后续保存覆盖回未裁剪版）──
    if cut_s and float(cut_s) > 0:
        from pipeline import crop_before_us, rebase_timeline, get_main_video_duration
        cut_us = int(round(float(cut_s) * 1e6))   # 新稿主轨从 0 起，裁点 = cut_s
        cut_segs = crop_before_us(draft, cut_us)
        moved = rebase_timeline(draft, cut_us)
        after_us = int(get_main_video_duration(draft) or 0)
        main_after_us = 0
        for seg in _main_track_segments(draft):
            target = seg.get('target_timerange') or {}
            end = int(target.get('start', 0) or 0) + int(target.get('duration', 0) or 0)
            main_after_us = max(main_after_us, end)
        if main_after_us <= 0:
            error = (f'裁剪后主轨时长为 0（cut_s={float(cut_s):g}s，裁剪前总时长 '
                     f'{total_us/1e6:.1f}s），请确保 cut_s 小于总时长；已取消保存和发布')
            print(f"  ❌ {error}")
            return {'ok': False, 'episodes': len(episodes_info),
                    'duration_s': 0.0, 'error': error}
        draft['duration'] = after_us
        total_us = after_us
        print(f"  ✂️ 发布前裁剪 {float(cut_s):g}s：裁段 {len(cut_segs)} 条，左移 {moved} 段，"
              f"总时长 → {total_us/1e6:.1f}s")
        _phase('发布前时间线裁剪')

    # ── Step 4: 保存到暂存区 ──
    save_draft(draft, jp_new)
    write_preset_manifest(stage_dir, preset_manifest)
    print(f"\n💾 暂存区草稿已保存")
    _phase('保存暂存区')

    # ── Step 5: 原子发布到剪映目录 + 注册索引 ──
    # 发布前先回首页：剪映打开着同名草稿时目录被占用，rename 会 PermissionError。
    # 回首页还会触发剪映对该草稿的 autosave 异步写回，必须等目标文件 mtime 静默后再发布，
    # 否则写回会覆盖刚发布的新草稿（热复用路径的核心风险）。
    print(f"\n🚀 发布草稿")
    try:
        import ui_nav
        from ui_core import connect_jianying as _cj
        _w, _pt = _cj(retry=False)
        if _w is not None and _pt == 'edit':
            ui_nav.switch_to_home(max_wait=15)
            time.sleep(1)
    except Exception as e:
        print(f"  [warn] 回首页失败（若同名草稿未打开可忽略）: {e}")
    _wait_target_quiesce(os.path.join(DRAFT_ROOT, draft_name, 'draft_content.json'))
    target, content_path = publish_staged_draft(stage_dir, draft_name, duration_us=total_us)
    print(f"   发布目录: {target}")
    _phase('发布到剪映目录（含静默检查）')

    # ── Step 6: 用 UI 自动化打开草稿 ──
    if open_after:
        print(f"\n⏳ 切换到剪映首页 → 打开草稿...")
        time.sleep(2)
        import ui_nav
        ui_nav.switch_to_home()
        time.sleep(2)
        _phase('切换首页')
        ui_nav.open_draft(draft_name, timeout=60)
        print(f"\n✅ 草稿已在剪映中打开！")
        _phase('打开草稿（含进编辑页轮询）')

    _total = sum(d for _n, d in _phases)

    def _dw(s):
        # 中文/全角算 2 列，保证秒数落在同一列
        return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in s)

    _w = max(_dw(n) for n, _d in _phases)
    print(f"\n   ⏱ [0] 初始化草稿分段汇总（合计 {_total:.1f}s）:")
    for _n, _d in _phases:
        print(f"      {_n}{' ' * (_w - _dw(_n))}  {_d:6.1f}s")

    return {'ok': True, 'episodes': len(episodes_info),
            'duration_s': round(total_us / 1e6, 1), 'error': None}


def main():
    import argparse
    ap = argparse.ArgumentParser(description='初始化测试草稿')
    ap.add_argument('--draft-name', default=DRAFT_NAME)
    ap.add_argument('--video-dir', default=VIDEO_DIR)
    ap.add_argument('--limit', type=int, default=VIDEO_LIMIT)
    ap.add_argument('--profile', default=None, metavar='A',
                    help='预设组名（presets.json 里的 presets_<组名>），多账号各用各的预设')
    ap.add_argument('--no-open', action='store_true', help='只发布，不在剪映中打开')
    args = ap.parse_args()
    r = init_draft(draft_name=args.draft_name, video_dir=args.video_dir,
                   preset_profile=args.profile,
                   video_limit=args.limit, open_after=not args.no_open)
    if not r['ok']:
        print(f"❌ {r['error']}")
        sys.exit(1)
    print(f"   总时长: {r['duration_s']}s，视频: {r['episodes']} 集")


if __name__ == '__main__':
    main()
