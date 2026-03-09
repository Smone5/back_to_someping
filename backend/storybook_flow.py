"""Shared storybook flow helpers for backend session state and release gating."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from shared.storybook_movie_quality import narration_required_default, storybook_release_gate

StoryPhase = Literal[
    "opening",
    "chatting",
    "drawing_scene",
    "waiting_for_child",
    "ending_story",
    "assembling_movie",
    "theater",
    "remake",
]


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _clean_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        text = _clean_str(item)
        if text:
            cleaned.append(text)
    return cleaned


def _state_has_scene_assets(state: Mapping[str, Any]) -> bool:
    scene_urls = _clean_list(state.get("scene_asset_urls"))
    if any(not url.startswith("data:") for url in scene_urls):
        return True
    current_scene_description = _clean_str(state.get("current_scene_description"))
    if current_scene_description and not current_scene_description.lower().startswith("no image yet"):
        return True
    if _clean_str(state.get("current_scene_storybeat_text")):
        return True
    return False


def theater_release_ready(state: Mapping[str, Any] | None) -> bool:
    if not isinstance(state, Mapping):
        return False
    assembly_status = _clean_str(state.get("assembly_status")).lower()
    if assembly_status != "complete":
        return False
    if not _clean_str(state.get("final_video_url")):
        return False

    explicit_ready = state.get("theater_release_ready")
    if isinstance(explicit_ready, bool):
        if not explicit_ready:
            return False
    narration_lines = _clean_list(state.get("narration_lines"))
    explicit_audio_expected = state.get("audio_expected")
    if isinstance(explicit_audio_expected, bool):
        expects_audio = explicit_audio_expected
    else:
        expects_audio = narration_required_default() or bool(narration_lines)
    audio_available = state.get("audio_available")
    final_has_audio_stream = state.get("final_has_audio_stream")

    try:
        duration_seconds = float(state.get("final_video_duration_sec") or 0.0)
    except Exception:
        duration_seconds = 0.0

    try:
        scene_count = int(state.get("final_scene_count") or len(_clean_list(state.get("scene_asset_urls"))))
    except Exception:
        scene_count = len(_clean_list(state.get("scene_asset_urls")))
    try:
        rendered_narration_count = int(state.get("rendered_narration_count") or 0)
    except Exception:
        rendered_narration_count = 0
    if rendered_narration_count <= 0 and narration_lines:
        # Older completed sessions persisted narration_lines but not an
        # explicit rendered_narration_count. Treat those lines as rendered so
        # theater restore stays backward-compatible.
        rendered_narration_count = len(narration_lines)

    release_ready, _ = storybook_release_gate(
        scene_count=scene_count,
        final_video_duration_sec=duration_seconds,
        expected_audio=expects_audio,
        audio_available=audio_available if isinstance(audio_available, bool) else None,
        final_has_audio_stream=final_has_audio_stream if isinstance(final_has_audio_stream, bool) else None,
        expected_narration_count=len(narration_lines),
        rendered_narration_count=rendered_narration_count,
        shot_types=_clean_list(state.get("final_shot_types")),
    )
    if not release_ready:
        return False

    return explicit_ready is not False


def derive_story_phase(
    state: Mapping[str, Any] | None,
    *,
    opening_phase: bool = False,
    ending_story: bool = False,
    assistant_speaking: bool = False,
    pending_scene_render: bool = False,
) -> StoryPhase:
    if not isinstance(state, Mapping):
        return "opening"

    if theater_release_ready(state):
        return "theater"

    assembly_status = _clean_str(state.get("assembly_status")).lower()
    assembly_kind = _clean_str(state.get("assembly_kind")).lower()
    if assembly_status in {"assembling", "reviewing_storyboard"}:
        return "remake" if assembly_kind == "remake" else "assembling_movie"
    if ending_story:
        return "ending_story"

    story_started = bool(state.get("story_started", False))
    if opening_phase or not story_started:
        return "opening"

    if pending_scene_render or bool(state.get("scene_render_pending", False)):
        return "drawing_scene"

    if assistant_speaking or bool(state.get("pending_response", False)):
        return "chatting"

    if bool(state.get("awaiting_story_choice", False)) or _state_has_scene_assets(state):
        return "waiting_for_child"

    return "chatting"
