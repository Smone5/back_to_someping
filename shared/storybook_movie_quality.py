"""Product-quality policy for final storybook movies.

This module exists so the worker, fast/local assembly path, and docs all point
to the same concrete targets for pacing, motion, audio mix, and theater sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Final, Literal

PAGE_SECONDS_DEFAULT: Final[float] = 4.0
PAGE_SECONDS_MIN: Final[float] = 3.0
PAGE_SECONDS_MAX: Final[float] = 6.0
PAGE_SECONDS_NARRATION_BUFFER: Final[float] = 0.4

NARRATION_REQUIRED_DEFAULT: Final[bool] = True
BURNED_CAPTIONS_DEFAULT: Final[bool] = False

NARRATION_VOLUME_DEFAULT: Final[float] = 1.6
NARRATION_VOLUME_MIN: Final[float] = 1.1
NARRATION_VOLUME_MAX: Final[float] = 2.0

MUSIC_VOLUME_DEFAULT: Final[float] = 0.10
MUSIC_VOLUME_MIN: Final[float] = 0.05
MUSIC_VOLUME_MAX: Final[float] = 0.18

SFX_VOLUME_DEFAULT: Final[float] = 0.22
SFX_VOLUME_MIN: Final[float] = 0.08
SFX_VOLUME_MAX: Final[float] = 0.32

SFX_MAX_DEFAULT: Final[int] = 2
SFX_MAX_MAX: Final[int] = 3
SFX_COOLDOWN_PAGES_DEFAULT: Final[int] = 1

STORYBOOK_RELEASE_MIN_SCENES: Final[int] = 3
STORYBOOK_RELEASE_MIN_SHOT_VARIETY: Final[int] = 3
STORYBOOK_RELEASE_MIN_NARRATION_COVERAGE: Final[float] = 0.85

ShotType = Literal["establishing", "wide", "reveal", "closeup", "detail", "pullback"]


@dataclass(frozen=True)
class StoryboardShotPlan:
    shot_type: ShotType
    profile_index: int

KEN_BURNS_MOTION_PROFILES: Final[list[tuple[float, float, float, float, float, float]]] = [
    (0.08, 0.18, 0.14, 0.12, 1.03, 1.12),
    (0.22, 0.09, 0.18, 0.16, 1.11, 1.04),
    (0.10, 0.24, 0.08, 0.22, 1.04, 1.13),
    (0.24, 0.14, 0.20, 0.08, 1.12, 1.05),
    (0.06, 0.12, 0.22, 0.12, 1.03, 1.09),
    (0.20, 0.20, 0.12, 0.18, 1.09, 1.03),
    (0.14, 0.06, 0.24, 0.20, 1.05, 1.13),
    (0.18, 0.24, 0.10, 0.06, 1.13, 1.05),
    (0.12, 0.16, 0.12, 0.24, 1.04, 1.10),
    (0.24, 0.10, 0.18, 0.18, 1.10, 1.04),
]
KEN_BURNS_ZOOM_MIN: Final[float] = 1.03
KEN_BURNS_ZOOM_MAX: Final[float] = 1.13

SHOT_TYPE_PROFILE_INDICES: Final[dict[ShotType, tuple[int, ...]]] = {
    "establishing": (0, 2, 9),
    "wide": (0, 3, 9),
    "reveal": (2, 3, 6),
    "closeup": (1, 5, 8),
    "detail": (4, 7, 8),
    "pullback": (1, 5, 9),
}

_DETAIL_KEYWORDS: Final[tuple[str, ...]] = (
    "elf",
    "toy",
    "book",
    "window",
    "door",
    "fireplace",
    "cookie",
    "ornament",
    "gift",
    "throne",
    "lantern",
    "little",
    "tiny",
    "cozy",
)
_REVEAL_KEYWORDS: Final[tuple[str, ...]] = (
    "inside",
    "enter",
    "open",
    "peek",
    "reveal",
    "discover",
    "find",
    "suddenly",
    "look in",
)
_WIDE_KEYWORDS: Final[tuple[str, ...]] = (
    "castle",
    "kingdom",
    "workshop",
    "forest",
    "village",
    "room",
    "hall",
    "landscape",
    "tower",
    "outside",
    "sky",
    "garden",
)
_PULLBACK_KEYWORDS: Final[tuple[str, ...]] = (
    "window",
    "home",
    "goodnight",
    "fireplace",
    "stars",
    "moon",
    "outside",
    "ending",
)

KEN_BURNS_SETTLE_MIN_SECONDS: Final[float] = 0.10
KEN_BURNS_SETTLE_MAX_SECONDS: Final[float] = 0.24
KEN_BURNS_TRAVEL_MIN_SECONDS: Final[float] = 1.1
KEN_BURNS_TRAVEL_MAX_SECONDS: Final[float] = 3.8
KEN_BURNS_DURATION_FOR_FULL_TRAVEL_SECONDS: Final[float] = 4.8

THEATER_MIN_HEIGHT_DESKTOP_PX: Final[int] = 420
THEATER_MIN_HEIGHT_TABLET_PX: Final[int] = 300
THEATER_MIN_HEIGHT_MOBILE_PX: Final[int] = 260

CHILD_AGE_MIN: Final[int] = 4
CHILD_AGE_MAX: Final[int] = 10


def clamp_page_seconds(value: float | int | str | None) -> float:
    try:
        numeric = float(value or PAGE_SECONDS_DEFAULT)
    except Exception:
        numeric = PAGE_SECONDS_DEFAULT
    return max(PAGE_SECONDS_MIN, min(PAGE_SECONDS_MAX, numeric))


def clamp_narration_volume(value: float | int | str | None) -> float:
    try:
        numeric = float(value or NARRATION_VOLUME_DEFAULT)
    except Exception:
        numeric = NARRATION_VOLUME_DEFAULT
    return max(NARRATION_VOLUME_MIN, min(NARRATION_VOLUME_MAX, numeric))


def clamp_music_volume(value: float | int | str | None) -> float:
    try:
        numeric = float(value or MUSIC_VOLUME_DEFAULT)
    except Exception:
        numeric = MUSIC_VOLUME_DEFAULT
    return max(MUSIC_VOLUME_MIN, min(MUSIC_VOLUME_MAX, numeric))


def clamp_sfx_volume(value: float | int | str | None) -> float:
    try:
        numeric = float(value or SFX_VOLUME_DEFAULT)
    except Exception:
        numeric = SFX_VOLUME_DEFAULT
    return max(SFX_VOLUME_MIN, min(SFX_VOLUME_MAX, numeric))


def clamp_sfx_max(value: int | str | None) -> int:
    try:
        numeric = int(value or SFX_MAX_DEFAULT)
    except Exception:
        numeric = SFX_MAX_DEFAULT
    return max(0, min(SFX_MAX_MAX, numeric))


def clamp_sfx_cooldown_pages(value: int | str | None) -> int:
    try:
        numeric = int(value or SFX_COOLDOWN_PAGES_DEFAULT)
    except Exception:
        numeric = SFX_COOLDOWN_PAGES_DEFAULT
    return max(0, min(2, numeric))


def _contains_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _infer_shot_type(description: str, scene_index: int, scene_count: int) -> ShotType:
    text = str(description or "").lower()
    if scene_count <= 1:
        return "establishing"
    if scene_index == 0:
        return "establishing"
    if scene_index == scene_count - 1:
        if _contains_keyword(text, _DETAIL_KEYWORDS):
            return "detail"
        return "pullback"
    if _contains_keyword(text, _DETAIL_KEYWORDS):
        return "detail" if scene_index % 2 else "closeup"
    if _contains_keyword(text, _REVEAL_KEYWORDS):
        return "reveal"
    if _contains_keyword(text, _WIDE_KEYWORDS):
        return "wide"
    cycle: tuple[ShotType, ...] = ("reveal", "closeup", "detail", "wide")
    return cycle[(scene_index - 1) % len(cycle)]


def _shot_plan(shot_type: ShotType, scene_index: int) -> StoryboardShotPlan:
    profile_indices = SHOT_TYPE_PROFILE_INDICES[shot_type]
    return StoryboardShotPlan(
        shot_type=shot_type,
        profile_index=profile_indices[scene_index % len(profile_indices)],
    )


def plan_storyboard_shots(scene_descriptions: list[str]) -> list[StoryboardShotPlan]:
    if not scene_descriptions:
        return []
    scene_count = len(scene_descriptions)
    plans: list[StoryboardShotPlan] = []
    previous_type: ShotType | None = None
    for idx, description in enumerate(scene_descriptions):
        shot_type = _infer_shot_type(description, idx, scene_count)
        if idx == scene_count - 1 and scene_count > 1:
            shot_type = "pullback"
        if previous_type == shot_type and scene_count > 2 and idx < scene_count - 1:
            alternates: tuple[ShotType, ...] = ("reveal", "closeup", "detail", "wide")
            for alternate in alternates:
                if alternate != previous_type:
                    shot_type = alternate
                    break
        plans.append(_shot_plan(shot_type, idx))
        previous_type = shot_type

    unique_types = {plan.shot_type for plan in plans}
    required_variety = min(STORYBOOK_RELEASE_MIN_SHOT_VARIETY, scene_count)
    if scene_count >= 3 and len(unique_types) < required_variety:
        rebuilt: list[StoryboardShotPlan] = []
        middle_cycle: tuple[ShotType, ...] = ("reveal", "closeup", "detail", "wide")
        for idx in range(scene_count):
            if idx == 0:
                shot_type = "establishing"
            elif idx == scene_count - 1:
                shot_type = "pullback"
            else:
                shot_type = middle_cycle[(idx - 1) % len(middle_cycle)]
            rebuilt.append(_shot_plan(shot_type, idx))
        return rebuilt
    return plans


def motion_profile(
    scene_index: int,
    *,
    shot_type: ShotType | None = None,
    profile_index: int | None = None,
) -> tuple[float, float, float, float, float, float]:
    if profile_index is not None:
        return KEN_BURNS_MOTION_PROFILES[profile_index % len(KEN_BURNS_MOTION_PROFILES)]
    if shot_type is not None:
        profile_indices = SHOT_TYPE_PROFILE_INDICES.get(shot_type)
        if profile_indices:
            return KEN_BURNS_MOTION_PROFILES[profile_indices[scene_index % len(profile_indices)]]
    return KEN_BURNS_MOTION_PROFILES[scene_index % len(KEN_BURNS_MOTION_PROFILES)]


def captions_enabled_default() -> bool:
    return BURNED_CAPTIONS_DEFAULT


def narration_required_default() -> bool:
    return NARRATION_REQUIRED_DEFAULT


def motion_timing(duration: float) -> tuple[float, float, float]:
    safe_duration = max(PAGE_SECONDS_MIN, float(duration))
    settle_seconds = min(KEN_BURNS_SETTLE_MAX_SECONDS, max(KEN_BURNS_SETTLE_MIN_SECONDS, safe_duration * 0.08))
    travel_seconds = min(max(KEN_BURNS_TRAVEL_MIN_SECONDS, safe_duration * 0.60), KEN_BURNS_TRAVEL_MAX_SECONDS)
    motion_factor = min(1.0, KEN_BURNS_DURATION_FOR_FULL_TRAVEL_SECONDS / max(safe_duration, KEN_BURNS_DURATION_FOR_FULL_TRAVEL_SECONDS))
    return settle_seconds, travel_seconds, motion_factor


def clamp_child_age(value: int | str | None) -> int:
    try:
        numeric = int(value or CHILD_AGE_MIN)
    except Exception:
        numeric = CHILD_AGE_MIN
    return max(CHILD_AGE_MIN, min(CHILD_AGE_MAX, numeric))


def child_age_band(value: int | str | None) -> str:
    age = clamp_child_age(value)
    if age <= 5:
        return "4-5"
    if age <= 7:
        return "6-7"
    return "8-10"


def narration_max_words_for_age(value: int | str | None, *, cover: bool = False) -> int:
    age = clamp_child_age(value)
    if cover:
        return 12 if age <= 5 else 14 if age <= 7 else 16
    if age <= 5:
        return 9
    if age <= 7:
        return 11
    return 14


def storybook_release_gate(
    *,
    scene_count: int,
    final_video_duration_sec: float,
    expected_audio: bool,
    audio_available: bool | None,
    final_has_audio_stream: bool | None,
    expected_narration_count: int = 0,
    rendered_narration_count: int = 0,
    shot_types: list[str] | None = None,
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    safe_scene_count = max(0, int(scene_count))
    safe_duration = max(0.0, float(final_video_duration_sec or 0.0))
    safe_expected_narration = max(0, int(expected_narration_count or 0))
    safe_rendered_narration = max(0, int(rendered_narration_count or 0))
    cleaned_shot_types = [str(item or "").strip() for item in shot_types or [] if str(item or "").strip()]

    if safe_scene_count and safe_scene_count < STORYBOOK_RELEASE_MIN_SCENES:
        issues.append(f"scene_count={safe_scene_count} is below the minimum of {STORYBOOK_RELEASE_MIN_SCENES}")
    if safe_duration > 0.0 and safe_duration < 1.0:
        issues.append(f"video_duration={safe_duration:.2f}s is too short")
    if expected_audio and audio_available is not True:
        issues.append("narration audio was expected but not available")
    if expected_audio and final_has_audio_stream is False:
        issues.append("final movie is missing an audio stream")
    if expected_audio and safe_expected_narration > 0:
        min_required = max(1, ceil(safe_expected_narration * STORYBOOK_RELEASE_MIN_NARRATION_COVERAGE))
        if safe_rendered_narration < min_required:
            issues.append(
                f"narration coverage {safe_rendered_narration}/{safe_expected_narration} is below {min_required}"
            )
    required_variety = min(STORYBOOK_RELEASE_MIN_SHOT_VARIETY, safe_scene_count)
    if required_variety >= 2 and cleaned_shot_types:
        if len(set(cleaned_shot_types)) < required_variety:
            issues.append(
                f"shot variety {len(set(cleaned_shot_types))} is below required {required_variety}"
            )

    return (not issues), issues
