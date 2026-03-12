"""Product-quality policy for final storybook movies.

This module exists so the worker, fast/local assembly path, and docs all point
to the same concrete targets for pacing, motion, audio mix, and theater sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Final, Literal

from .story_text import normalize_storybeat_text, story_sentence_quality_score, truncate_story_sentence

PAGE_SECONDS_DEFAULT: Final[float] = 4.0
PAGE_SECONDS_MIN: Final[float] = 3.0
PAGE_SECONDS_MAX: Final[float] = 6.0
PAGE_SECONDS_NARRATION_BUFFER: Final[float] = 0.4

NARRATION_REQUIRED_DEFAULT: Final[bool] = True
BURNED_CAPTIONS_DEFAULT: Final[bool] = True

NARRATION_VOLUME_DEFAULT: Final[float] = 2.0
NARRATION_VOLUME_MIN: Final[float] = 1.2
NARRATION_VOLUME_MAX: Final[float] = 2.2

MUSIC_VOLUME_DEFAULT: Final[float] = 0.14
MUSIC_VOLUME_MIN: Final[float] = 0.05
MUSIC_VOLUME_MAX: Final[float] = 0.20

SFX_VOLUME_DEFAULT: Final[float] = 0.22
SFX_VOLUME_MIN: Final[float] = 0.08
SFX_VOLUME_MAX: Final[float] = 0.32

SFX_MAX_DEFAULT: Final[int] = 2
SFX_MAX_MAX: Final[int] = 3
SFX_COOLDOWN_PAGES_DEFAULT: Final[int] = 1

# A single finished storybook page should still produce a watchable movie.
# Higher scene counts are better, but they are not a reason to block export.
STORYBOOK_RELEASE_MIN_SCENES: Final[int] = 1
STORYBOOK_RELEASE_MIN_SHOT_VARIETY: Final[int] = 3
STORYBOOK_RELEASE_MIN_NARRATION_COVERAGE: Final[float] = 0.85

ShotType = Literal["establishing", "wide", "reveal", "closeup", "detail", "pullback"]
StorybookMoviePacingMode = Literal["read_to_me", "read_with_me", "fast_movie"]

STORYBOOK_MOVIE_PACING_DEFAULT: Final[StorybookMoviePacingMode] = "read_with_me"


@dataclass(frozen=True)
class StoryboardShotPlan:
    shot_type: ShotType
    profile_index: int

KEN_BURNS_MOTION_PROFILES: Final[list[tuple[float, float, float, float, float, float]]] = [
    (0.10, 0.14, 0.12, 0.11, 1.018, 1.055),
    (0.16, 0.11, 0.14, 0.12, 1.062, 1.024),
    (0.11, 0.17, 0.10, 0.15, 1.022, 1.070),
    (0.17, 0.13, 0.15, 0.10, 1.068, 1.028),
    (0.09, 0.12, 0.16, 0.12, 1.018, 1.048),
    (0.15, 0.15, 0.11, 0.14, 1.050, 1.018),
    (0.12, 0.09, 0.17, 0.15, 1.026, 1.072),
    (0.14, 0.17, 0.10, 0.09, 1.070, 1.030),
    (0.11, 0.14, 0.11, 0.17, 1.020, 1.050),
    (0.17, 0.10, 0.14, 0.14, 1.054, 1.022),
]
KEN_BURNS_ZOOM_MIN: Final[float] = 1.018
KEN_BURNS_ZOOM_MAX: Final[float] = 1.072

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

KEN_BURNS_SETTLE_MIN_SECONDS: Final[float] = 0.18
KEN_BURNS_SETTLE_MAX_SECONDS: Final[float] = 0.38
KEN_BURNS_TRAVEL_MIN_SECONDS: Final[float] = 1.6
KEN_BURNS_TRAVEL_MAX_SECONDS: Final[float] = 4.6
KEN_BURNS_DURATION_FOR_FULL_TRAVEL_SECONDS: Final[float] = 6.2

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
        return 12
    if age <= 7:
        return 11
    return 14


def normalize_storybook_movie_pacing(raw: str | None) -> StorybookMoviePacingMode:
    normalized = str(raw or "").strip().lower()
    if normalized in {"read_to_me", "read-to-me", "readtome", "voice_first", "voice-first"}:
        return "read_to_me"
    if normalized in {"fast_movie", "fast-movie", "fast", "replay"}:
        return "fast_movie"
    return STORYBOOK_MOVIE_PACING_DEFAULT


def storybook_movie_pacing_default() -> StorybookMoviePacingMode:
    return STORYBOOK_MOVIE_PACING_DEFAULT


def readalong_max_words_for_age(
    value: int | str | None,
    movie_pacing: str | None = None,
) -> int:
    age = clamp_child_age(value)
    mode = normalize_storybook_movie_pacing(movie_pacing)
    if mode == "read_to_me":
        if age <= 5:
            return 6
        if age <= 7:
            return 8
        return 10
    if mode == "fast_movie":
        if age <= 5:
            return 7
        if age <= 7:
            return 9
        return 12
    if age <= 5:
        return 12
    if age <= 7:
        return 12
    return 16


def _storybook_word_count(text: str | None) -> int:
    normalized = normalize_storybeat_text(text, max_chars=400)
    return len([token for token in normalized.split() if token])


def _truncate_words(text: str, max_words: int) -> str:
    if max_words <= 0:
        return ""
    return truncate_story_sentence(text, max_words=max_words)


def choose_readalong_text(
    primary_text: str | None,
    fallback_text: str | None,
    child_age: int | str | None,
    movie_pacing: str | None = None,
) -> str:
    primary = normalize_storybeat_text(primary_text, max_chars=320)
    fallback = normalize_storybeat_text(fallback_text, max_chars=320)
    max_words = readalong_max_words_for_age(child_age, movie_pacing)
    primary_score = story_sentence_quality_score(primary)
    fallback_score = story_sentence_quality_score(fallback)

    if fallback and (fallback_score >= primary_score + 3 or primary_score < 2):
        if _storybook_word_count(fallback) <= max_words:
            return fallback
        return _truncate_words(fallback, max_words)

    if primary and _storybook_word_count(primary) <= max_words:
        return primary
    if fallback and _storybook_word_count(fallback) <= max_words:
        return fallback
    if fallback:
        return _truncate_words(fallback, max_words)
    if primary:
        return _truncate_words(primary, max_words)
    return ""


def readalong_seconds_for_age(
    text: str | None,
    child_age: int | str | None,
    movie_pacing: str | None = None,
) -> float:
    words = _storybook_word_count(text)
    if words <= 0:
        return 0.0

    age = clamp_child_age(child_age)
    mode = normalize_storybook_movie_pacing(movie_pacing)
    if mode == "read_to_me":
        if age <= 5:
            min_seconds = 4.4
            max_seconds = 5.8
            words_per_second = 2.4
            settle_seconds = 1.2
        elif age <= 7:
            min_seconds = 4.1
            max_seconds = 5.3
            words_per_second = 2.8
            settle_seconds = 0.95
        else:
            min_seconds = 3.8
            max_seconds = 4.8
            words_per_second = 3.3
            settle_seconds = 0.75
    elif mode == "fast_movie":
        if age <= 5:
            min_seconds = 4.0
            max_seconds = 5.0
            words_per_second = 2.8
            settle_seconds = 0.9
        elif age <= 7:
            min_seconds = 3.7
            max_seconds = 4.8
            words_per_second = 3.2
            settle_seconds = 0.8
        else:
            min_seconds = 3.4
            max_seconds = 4.5
            words_per_second = 3.8
            settle_seconds = 0.6
    else:
        if age <= 5:
            min_seconds = 5.2
            max_seconds = 7.2
            words_per_second = 2.0
            settle_seconds = 1.6
        elif age <= 7:
            min_seconds = 4.6
            max_seconds = 6.2
            words_per_second = 2.5
            settle_seconds = 1.3
        else:
            min_seconds = 4.0
            max_seconds = 5.6
            words_per_second = 3.0
            settle_seconds = 1.0

    estimated = settle_seconds + (words / words_per_second)
    return max(min_seconds, min(max_seconds, estimated))


def storybook_page_duration_seconds(
    *,
    child_age: int | str | None,
    base_page_seconds: float,
    narration_seconds: float = 0.0,
    readalong_text: str | None = None,
    movie_pacing: str | None = None,
) -> float:
    duration = max(
        clamp_page_seconds(base_page_seconds),
        readalong_seconds_for_age(readalong_text, child_age, movie_pacing),
    )
    if narration_seconds and narration_seconds > 0:
        duration = max(duration, float(narration_seconds) + PAGE_SECONDS_NARRATION_BUFFER)
    return duration


def storybook_tts_speaking_rate(
    base_rate: float,
    child_age: int | str | None,
    movie_pacing: str | None = None,
) -> float:
    rate = float(base_rate or 0.9)
    age = clamp_child_age(child_age)
    mode = normalize_storybook_movie_pacing(movie_pacing)
    if age <= 5:
        if mode == "read_to_me":
            rate -= 0.04
        elif mode == "fast_movie":
            rate -= 0.01
        else:
            rate -= 0.06
    return max(0.7, min(rate, 1.2))


def storybook_tts_tempo_factor(
    child_age: int | str | None,
    movie_pacing: str | None = None,
) -> float:
    age = clamp_child_age(child_age)
    if age <= 5:
        mode = normalize_storybook_movie_pacing(movie_pacing)
        if mode == "read_to_me":
            return 0.97
        if mode == "fast_movie":
            return 1.0
        return 0.94
    return 1.0


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
    if expected_audio and safe_scene_count > 0:
        safe_expected_narration = max(safe_expected_narration, safe_scene_count)
    cleaned_shot_types = [str(item or "").strip() for item in shot_types or [] if str(item or "").strip()]

    if safe_scene_count < STORYBOOK_RELEASE_MIN_SCENES:
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
