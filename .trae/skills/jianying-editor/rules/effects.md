---
name: effects`
description: Searching for and applying effects, filters, and transitions.
metadata:
  tags: effects, filters, transitions, search, asset_id
---

# Effects, Filters & Transitions

Jianying uses unique string IDs (e.g., `734521...`) for local assets. These IDs change or vary by version.
**You MUST NOT guess IDs.** You MUST search for them first.

## 1. Search for Asset IDs

Use the `asset_search.py` script to find the correct ID for a request.

```bash
# Syntax
python <SKILL_ROOT>/scripts/asset_search.py "<Keyword>" -c <Category>

# Categories (-c):
# - filters (滤镜)
# - video_scene_effects (画面特效)
# - transitions (转场)
# - text_animations (文字动画)

# Example: Search for "Glitch" effects
python <SKILL_ROOT>/scripts/asset_search.py "故障" -c video_scene_effects
# Output: [Found] Name: 故障_I, ID: 1234567...
```

## 2. Apply in Code

Once you have the ID, apply it using the wrapper.

## 2A. Preferred High-Level APIs

For normal editing requests, prefer these wrapper methods instead of inventing new API names:

```python
seg1 = project.add_media_safe("clip1.mp4", "0s", "3s", track_name="V1")
seg2 = project.add_media_safe("clip2.mp4", "3s", "3s", track_name="V1")

project.add_filter_simple("哈苏蓝", video_segment=seg1, intensity=70)
project.add_effect_simple("复古DV", start_time="0s", duration="3s")
project.add_transition_simple("叠化", video_segment=seg1, duration="0.5s")
```

Rules:
- Filters should usually target a concrete `video_segment`.
- Transitions should usually target the previous clip. Prefer passing `video_segment=seg1` explicitly.
- Only use `track_name="V1"` auto-resolution for transitions when the track already contains at least two video clips.
- If any VFX method returns `None`, treat that as failure and report the exact reason to the user.

**(Common Pattern for Transitions)**
Transitions are applied between clips. Do not just say "done" after calling the API. You must verify the draft content.

**(Common Pattern for Global Effects)**
Effects often sit on their own track above the main video.
```python
project.add_effect_simple("故障", start_time="0s", duration="5s")
```

## 3. Mandatory Verification

After generating a draft with filters / effects / transitions, inspect the saved draft and verify:
- `materials.filters` is non-empty when you claim a filter was added.
- `materials.video_effects` is non-empty when you claim an effect was added.
- `materials.transitions` is non-empty when you claim a transition was added.

Use one of these:

```bash
python <SKILL_ROOT>/scripts/draft_inspector.py summary --name "DraftName"
python <SKILL_ROOT>/scripts/draft_inspector.py show --name "DraftName" --kind content --json
```

If the verification fails, do not report success. Explain whether the failure came from:
- unresolved asset name
- no valid video segment to attach
- no second clip for transition
- draft save / patch failure
