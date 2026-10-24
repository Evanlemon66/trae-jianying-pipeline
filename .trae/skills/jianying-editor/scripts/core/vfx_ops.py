import os
import time
from typing import Optional, Union

import pyJianYingDraft as draft
from utils.formatters import safe_tim


class VfxOpsMixin:
    """
    JyProject 的特效与转场 Mixin。
    """

    def _sync_filter_material(self, filter_obj) -> None:
        if filter_obj not in self.script.materials:
            self.script.materials.filters.append(filter_obj)

    def _sync_transition_material(self, transition_obj) -> None:
        if transition_obj not in self.script.materials:
            self.script.materials.transitions.append(transition_obj)

    def _warn_missing_asset(self, asset_kind: str, asset_name: str) -> None:
        print(
            f"⚠️ Unable to resolve {asset_kind}: '{asset_name}'. "
            f"Use asset_search.py or a valid built-in {asset_kind} name first."
        )

    def _resolve_video_segment(
        self,
        video_segment: Optional[draft.VideoSegment] = None,
        track_name: Optional[str] = None,
        prefer_transition_anchor: bool = False,
    ) -> Optional[draft.VideoSegment]:
        if video_segment is not None:
            return video_segment
        if not track_name:
            return None

        track = self.script.tracks.get(track_name)
        if not track or not getattr(track, "segments", None):
            print(f"⚠️ Track '{track_name}' has no video segments to attach VFX.")
            return None

        if prefer_transition_anchor and len(track.segments) >= 2:
            candidate = track.segments[-2]
        else:
            candidate = track.segments[-1]
        if not isinstance(candidate, draft.VideoSegment):
            print(f"⚠️ Track '{track_name}' last segment is not a video segment.")
            return None
        return candidate

    def add_filter_simple(
        self,
        filter_name: str,
        video_segment: Optional[draft.VideoSegment] = None,
        intensity: float = 100.0,
        track_name: Optional[str] = None,
    ):
        video_segment = self._resolve_video_segment(video_segment, track_name)
        if video_segment is None:
            return None

        filter_type = self._resolve_enum(draft.FilterType, filter_name)
        if not filter_type:
            self._warn_missing_asset("filter", filter_name)
            return None

        video_segment.add_filter(filter_type, intensity)
        filter_obj = video_segment.filters[-1]
        self._sync_filter_material(filter_obj)
        return filter_obj

    def add_effect_simple(
        self,
        effect_name: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "3s",
        track_name: str = "EffectTrack",
    ):
        if start_time is None:
            start_time = self.get_track_duration(track_name)
        self._ensure_track(draft.TrackType.effect, track_name)

        eff_type = self._resolve_enum(draft.VideoSceneEffectType, effect_name)
        if not eff_type:
            self._warn_missing_asset("effect", effect_name)
            return None

        seg = draft.EffectSegment(
            draft.EffectMaterial(eff_type),
            draft.Timerange(safe_tim(start_time), safe_tim(duration)),
        )
        self.script.add_segment(seg, track_name)
        return seg

    def add_transition_simple(
        self,
        transition_name: str,
        video_segment: Optional[draft.VideoSegment] = None,
        duration: Union[str, int] = "1s",
        track_name: Optional[str] = None,
    ):
        # 兼容调用：如果未直接给 segment，则尝试从指定轨道获取最后一个视频片段
        video_segment = self._resolve_video_segment(
            video_segment,
            track_name,
            prefer_transition_anchor=True,
        )
        if video_segment is None:
            return None

        trans_type = self._resolve_enum(draft.TransitionType, transition_name)
        if not trans_type:
            self._warn_missing_asset("transition", transition_name)
            return None

        video_segment.add_transition(trans_type, duration=safe_tim(duration))
        self._sync_transition_material(video_segment.transition)
        return video_segment.transition

    def add_web_asset_safe(
        self,
        html_path: str,
        start_time: Union[str, int] = None,
        duration: Union[str, int] = "5s",
        track_name: str = "WebVfxTrack",
        output_dir: Optional[str] = None,
        wait_timeout: Optional[float] = None,
        wait_mode: str = "auto",
    ):
        from web_recorder import record_web_animation

        if start_time is None:
            start_time = self.get_track_duration(track_name)
        if output_dir is None:
            output_dir = os.path.join(self.root, self.name, "temp_assets")
        os.makedirs(output_dir, exist_ok=True)

        video_output = os.path.join(output_dir, f"web_vfx_{int(time.time())}.webm")
        duration_s = safe_tim(duration) / 1e6
        wait_budget_s = min(300, max(60, duration_s * 2, duration_s + 15))
        if record_web_animation(
            html_path,
            video_output,
            max_duration=duration_s,
            signal_grace=10,
            wait_timeout=wait_timeout or wait_budget_s,
            wait_mode=wait_mode,
        ):
            return self.add_media_safe(video_output, start_time, duration, track_name=track_name)
        return None
