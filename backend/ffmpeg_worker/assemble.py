#!/usr/bin/env python3
"""FFmpeg Cloud Run Job — assembles the final story movie.

This job is triggered by the `assemble_story_video` ADK tool via the Cloud Run
Jobs API. The worker now runs an ADK video-assembly workflow inside the job:
1. ADK plans the movie assembly and render priorities.
2. The render callback pulls session assets and runs FFmpeg / audio generation.
3. ADK audits the render report and can request one focused retry.
4. The approved final .mp4 is uploaded and written back to Firestore.

Key design from the expert audits:
- `memory = "2Gi"` provisioned in Terraform (Iter 7 #6 — OOM fix for 8 Veo clips)
- `-movflags faststart` enforced (Iter 5 #6 — moov atom to front for streaming)
- Concurrent download using asyncio.gather for speed
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import textwrap
import struct
import zlib
from typing import Any, Literal
from pathlib import Path
from collections import Counter
import sys
import wave

import httpx
import google.auth
import google.auth.transport.requests
from google.cloud import firestore, storage
from pydantic import BaseModel, ConfigDict, Field

def _resolve_import_root() -> Path:
    file_path = Path(__file__).resolve()
    for candidate in [file_path.parent, *file_path.parents]:
        if (candidate / "shared").exists():
            return candidate
    return file_path.parent


_IMPORT_ROOT = _resolve_import_root()
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from shared.meta_learning import build_principles_injection_text, record_prompt_feedback
from shared.storybook_assembly_workflow import (
    build_storyboard_report_from_workflow_state,
    run_storybook_director_workflow,
)
from shared.storybook_movie_quality import (
    BURNED_CAPTIONS_DEFAULT,
    PAGE_SECONDS_DEFAULT,
    PAGE_SECONDS_NARRATION_BUFFER,
    StoryboardShotPlan,
    child_age_band,
    choose_readalong_text,
    clamp_music_volume,
    clamp_narration_volume,
    clamp_page_seconds,
    clamp_sfx_cooldown_pages,
    clamp_sfx_max,
    clamp_sfx_volume,
    motion_profile,
    motion_timing,
    narration_max_words_for_age,
    narration_required_default,
    normalize_storybook_movie_pacing,
    plan_storyboard_shots,
    storybook_tts_speaking_rate,
    storybook_tts_tempo_factor,
    storybook_page_duration_seconds,
    storybook_release_gate,
)
from shared.storybook_lighting import (
    heuristic_storybook_lighting_command,
    heuristic_storybook_lighting_effect,
    lighting_cue_from_story_page,
    normalize_storybook_lighting_cues,
)
from shared.storybook_pages import story_pages_from_state_data
from shared.story_text import (
    clean_story_text as shared_clean_story_text,
    normalize_storybeat_text as shared_normalize_storybeat_text,
    split_story_sentences as shared_split_story_sentences,
    story_sentence_needs_revision,
    story_sentence_quality_score,
    truncate_story_sentence as shared_truncate_story_sentence,
)
from shared.storybook_studio_workflow import (
    build_storybook_studio_plan_from_workflow_state,
    build_storybook_studio_summary,
    run_storybook_studio_workflow,
)
from shared.storybook_video_assembly_workflow import (
    build_storybook_video_assembly_summary,
    run_storybook_video_assembly_workflow,
)
from shared.storybook_titles import (
    build_storybook_title_prompt as shared_build_storybook_title_prompt,
    clean_storybook_title as shared_clean_storybook_title,
    heuristic_storybook_title as shared_heuristic_storybook_title,
    validate_storybook_title as shared_validate_storybook_title,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

GCS_FINAL_BUCKET = os.environ.get("GCS_FINAL_VIDEOS_BUCKET", "storyteller-final-videos")
GCS_ASSETS_BUCKET = os.environ.get("GCS_ASSETS_BUCKET", "storyteller-session-assets")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
DEFAULT_VERTEX_TEXT_MODEL = "gemini-2.5-flash"
DEFAULT_VERTEX_IMAGE_MODEL = "gemini-2.0-flash-preview-image-generation"
DEFAULT_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
_ELEVENLABS_TTS_DISABLED_REASON: str | None = None
_ELEVENLABS_AUDIO_DISABLED_REASON: str | None = None
_VISUAL_GROUNDING_STOPWORDS = {
    "about",
    "after",
    "around",
    "away",
    "back",
    "before",
    "beside",
    "bright",
    "castle",
    "child",
    "cozy",
    "down",
    "each",
    "every",
    "finds",
    "first",
    "following",
    "friendly",
    "from",
    "gentle",
    "glows",
    "happy",
    "into",
    "little",
    "magic",
    "magical",
    "moon",
    "moonlit",
    "near",
    "next",
    "page",
    "path",
    "picture",
    "playful",
    "scene",
    "shiny",
    "soft",
    "sparkly",
    "spooky",
    "story",
    "their",
    "there",
    "these",
    "this",
    "through",
    "tiny",
    "toward",
    "towards",
    "under",
    "warm",
    "with",
}


def _normalize_storybook_elevenlabs_voice_id(raw: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip())
    if 20 <= len(cleaned) <= 64:
        return cleaned
    return ""


def _storybook_default_elevenlabs_voice_id() -> str:
    return _normalize_storybook_elevenlabs_voice_id(
        os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    ) or DEFAULT_ELEVENLABS_VOICE_ID


def _storybook_fallback_elevenlabs_voice_id(voice_id: str | None) -> str:
    normalized = _normalize_storybook_elevenlabs_voice_id(voice_id)
    fallback = _storybook_default_elevenlabs_voice_id()
    if fallback and fallback != normalized:
        return fallback
    return ""


def _elevenlabs_voice_not_found_error(payload: Any) -> bool:
    if isinstance(payload, (dict, list)):
        try:
            payload = json.dumps(payload, ensure_ascii=True)
        except Exception:
            payload = str(payload)
    normalized = str(payload or "").strip().lower()
    return "voice_not_found" in normalized or (
        "voice with voice_id" in normalized and "was not found" in normalized
    )


async def download_blob(session: httpx.AsyncClient, url: str, dest: Path) -> Path:
    """Downloads a GCS signed URL to a local tmp path."""
    resp = await session.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _storybook_studio_enabled() -> bool:
    return _env_enabled("ENABLE_STORYBOOK_STUDIO_WORKFLOW", default=True)


def _fast_storybook_assembly_enabled() -> bool:
    return _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False)


def _storybook_scene_render_concurrency() -> int:
    default = "3" if _fast_storybook_assembly_enabled() else "2"
    return _clamp_int(os.environ.get("STORYBOOK_SCENE_RENDER_CONCURRENCY", default), int(default), 1, 4)


def _storybook_studio_max_revisions() -> int:
    return _clamp_int(os.environ.get("STORYBOOK_STUDIO_MAX_REVISIONS", "1"), 1, 0, 2)


def _build_storybook_studio_story_context(
    story_pages: list[dict[str, Any]],
    narration_source_texts: list[str],
    *,
    scene_count: int,
) -> list[dict[str, Any]]:
    context_rows: list[dict[str, Any]] = []
    target = max(0, int(scene_count or 0))
    for idx in range(target):
        page = story_pages[idx] if idx < len(story_pages) and isinstance(story_pages[idx], dict) else {}
        row = {
            "scene_number": idx + 1,
            "page_label": str(page.get("label", "") or "").strip(),
            "storybeat_text": str(page.get("storybeat_text", "") or "").strip(),
            "scene_description": str(page.get("scene_description", "") or "").strip(),
            "narration_source_text": (
                str(narration_source_texts[idx]).strip()
                if idx < len(narration_source_texts)
                else ""
            ),
        }
        compact = {key: value for key, value in row.items() if value not in {"", None}}
        if compact:
            context_rows.append(compact)
    return context_rows


async def _run_storybook_studio_async(
    *,
    session_id: str,
    title: str,
    child_name: str,
    child_age: int | str | None,
    story_summary: str,
    scene_descriptions: list[str],
    story_pages: list[dict[str, Any]] | None,
    narration_source_texts: list[str] | None,
    child_delight_anchors_text: str = "",
    continuity_world_state_text: str = "",
    scene_count: int,
    max_music_cues: int,
    max_sfx_cues: int,
    fallback_narration_lines: list[str] | None = None,
) -> dict[str, Any]:
    story_context_rows = _build_storybook_studio_story_context(
        list(story_pages or []),
        list(narration_source_texts or []),
        scene_count=scene_count,
    )
    workflow_state = await run_storybook_studio_workflow(
        session_id=session_id,
        initial_state={
            "story_title": title,
            "child_name": child_name or "friend",
            "child_age": child_age or 4,
            "child_age_band": child_age_band(child_age),
            "story_summary": story_summary or "No summary available.",
            "studio_scene_count": max(0, int(scene_count)),
            "scene_descriptions_json": json.dumps(scene_descriptions, ensure_ascii=True),
            "studio_story_pages_json": json.dumps(story_context_rows, ensure_ascii=True),
            "studio_child_delight_anchors_text": str(child_delight_anchors_text or "").strip() or "No child delight anchors recorded.",
            "studio_continuity_world_state_text": str(continuity_world_state_text or "").strip() or "No continuity state recorded.",
            "studio_fallback_narration_json": json.dumps(list(fallback_narration_lines or []), ensure_ascii=True),
            "studio_scene_max_words": narration_max_words_for_age(child_age),
            "studio_cover_max_words": narration_max_words_for_age(child_age, cover=True),
            "studio_max_music_cues": max(0, int(max_music_cues)),
            "studio_max_sfx_cues": max(0, int(max_sfx_cues)),
        },
        max_revision_rounds=_storybook_studio_max_revisions(),
    )
    return build_storybook_studio_plan_from_workflow_state(
        workflow_state,
        scene_count=scene_count,
        fallback_narration_lines=fallback_narration_lines,
    )


def _apply_storybook_mix_guidance(
    guidance: dict[str, Any] | None,
    *,
    narration_volume: float,
    music_volume: float,
    sfx_volume: float,
    enable_ducking: bool,
) -> tuple[float, float, float, bool]:
    guidance = dict(guidance or {})
    try:
        narration_volume = clamp_narration_volume(guidance.get("narration_volume", narration_volume))
    except Exception:
        pass
    try:
        music_volume = clamp_music_volume(guidance.get("music_volume", music_volume))
    except Exception:
        pass
    try:
        sfx_volume = clamp_sfx_volume(guidance.get("sfx_volume", sfx_volume))
    except Exception:
        pass
    ducking = str(guidance.get("ducking") or "").strip().lower()
    if ducking == "off":
        enable_ducking = False
    elif ducking in {"strong", "medium", "light"}:
        enable_ducking = True
    return narration_volume, music_volume, sfx_volume, enable_ducking


def _storybook_burned_captions_enabled() -> bool:
    return _env_enabled("ENABLE_STORYBOOK_CAPTIONS", default=BURNED_CAPTIONS_DEFAULT)


def _storybook_music_provider() -> str:
    raw = os.environ.get("STORYBOOK_MUSIC_PROVIDER", "auto").strip().lower()
    if raw in {"auto", "lyria", "elevenlabs", "off"}:
        return raw
    return "auto"


def _storybook_sfx_provider() -> str:
    raw = os.environ.get("STORYBOOK_SFX_PROVIDER", "auto").strip().lower()
    if raw in {"auto", "elevenlabs", "off"}:
        return raw
    return "auto"


def _normalize_story_tone(raw: Any) -> str:
    text = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    alias_map = {
        "": "cozy",
        "cozy": "cozy",
        "gentle": "gentle_spooky",
        "gentle_spooky": "gentle_spooky",
        "soft_spooky": "gentle_spooky",
        "spooky": "gentle_spooky",
        "adventure": "adventure_spooky",
        "adventure_spooky": "adventure_spooky",
        "brave_spooky": "adventure_spooky",
    }
    normalized = alias_map.get(text, text)
    if normalized in {"cozy", "gentle_spooky", "adventure_spooky"}:
        return normalized
    return "cozy"


def _story_tone_art_guidance(story_tone: str) -> str:
    tone = _normalize_story_tone(story_tone)
    if tone == "gentle_spooky":
        return (
            "Allow gentle spooky preschool mystery like moonlit forests, creaky towers, silly goblins, "
            "friendly dragons, glowing caves, and magical shadows, but keep visible safety, warmth, and quick reassurance."
        )
    if tone == "adventure_spooky":
        return (
            "Allow a slightly bolder preschool fantasy adventure with brave castle towers, magical storms, "
            "shadowy paths, and suspenseful discoveries, but keep it hopeful, age 4 safe, and never horrifying."
        )
    return "Keep the art fully cozy, bright, warm, and never spooky."


def _story_tone_veo_negative_prompt(story_tone: str) -> str:
    tone = _normalize_story_tone(story_tone)
    if tone == "cozy":
        return "scary, horror, violent, creepy, threatening monsters, dark menace, text, logos"
    return "horror, jump scares, violent, gore, nightmare imagery, realistic menace, text, logos"


def _lyria_negative_prompt() -> str:
    return (
        "vocals, lyrics, singing, speech, chanting, harsh, abrasive, "
        "distorted noise, horror, jump scares, aggressive percussion"
    )


def _lyria_generate_music_sync(
    prompt: str,
    seed: int | None = None,
) -> tuple[bytes, str] | None:
    if not PROJECT or not prompt:
        return None
    location = _vertex_ai_location()
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    endpoint = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{location}/"
        "publishers/google/models/lyria-002:predict"
    )
    instance: dict[str, Any] = {
        "prompt": prompt[:1200],
        "negative_prompt": _lyria_negative_prompt(),
    }
    if seed is not None:
        instance["seed"] = int(seed)
    payload = {"instances": [instance], "parameters": {}}
    try:
        resp = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=90,
        )
        resp.raise_for_status()
        predictions = list(resp.json().get("predictions", []) or [])
        if not predictions:
            return None
        first = dict(predictions[0] or {})
        audio_b64 = str(first.get("audioContent") or first.get("bytesBase64Encoded") or "").strip()
        if not audio_b64:
            return None
        mime_type = str(first.get("mimeType", "audio/wav")).strip().lower()
        suffix = ".wav" if "wav" in mime_type else ".bin"
        return base64.b64decode(audio_b64), suffix
    except Exception as exc:
        logger.warning("Lyria music generation failed: %s", exc)
        return None


def _clamp_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


def _clamp_float(value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


def _build_storybook_theater_lighting_cues(
    *,
    scene_durations: list[float],
    story_pages: list[dict[str, Any]],
    scene_descriptions: list[str],
    scene_lighting_cues: list[dict[str, Any]] | None = None,
    cover_enabled: bool = False,
    cover_description: str = "",
    end_card_index: int | None = None,
    end_card_title: str = "",
) -> list[dict[str, Any]]:
    if not scene_durations:
        return []

    live_cues = normalize_storybook_lighting_cues(scene_lighting_cues)
    cues_by_request_id = {
        str(item.get("request_id") or "").strip(): item
        for item in live_cues
        if str(item.get("request_id") or "").strip()
    }
    cues_by_scene_number = {
        int(item.get("scene_number") or 0): item
        for item in live_cues
        if int(item.get("scene_number") or 0) > 0
    }

    built: list[dict[str, Any]] = []
    start_seconds = 0.0
    page_offset = 1 if cover_enabled else 0

    for scene_index, raw_duration in enumerate(scene_durations):
        duration = max(0.1, float(raw_duration or 0.0))
        is_cover = cover_enabled and scene_index == 0
        is_end_card = end_card_index is not None and scene_index == end_card_index
        page_index = scene_index - page_offset
        page = story_pages[page_index] if 0 <= page_index < len(story_pages) else {}
        scene_number = 0
        try:
            scene_number = int(page.get("scene_number") or (page_index + 1 if page_index >= 0 else 0))
        except Exception:
            scene_number = page_index + 1 if page_index >= 0 else 0
        request_id = str(page.get("request_id") or "").strip()
        scene_text = ""
        if is_cover:
            scene_text = str(cover_description or "").strip()
        elif is_end_card:
            scene_text = str(end_card_title or "").strip()
        else:
            scene_text = (
                str(page.get("storybeat_text") or "").strip()
                or str(page.get("scene_description") or "").strip()
                or (
                    str(scene_descriptions[page_index]).strip()
                    if 0 <= page_index < len(scene_descriptions)
                    else ""
                )
            )

        effect_defaults = heuristic_storybook_lighting_effect(
            scene_text,
            is_cover=is_cover,
            is_end_card=is_end_card,
            duration_seconds=duration,
        )
        cue = None
        if request_id:
            cue = cues_by_request_id.get(request_id)
        if cue is None and scene_number > 0:
            cue = cues_by_scene_number.get(scene_number)
        page_cue = None if is_cover or is_end_card else lighting_cue_from_story_page(page, duration_seconds=duration)
        if page_cue is not None:
            cue = dict(page_cue)
        if cue is None:
            cue = heuristic_storybook_lighting_command(
                scene_text,
                is_cover=is_cover,
                is_end_card=is_end_card,
                duration_seconds=duration,
            )
        else:
            cue = dict(cue)
            if page_cue is None:
                cue["cue_source"] = str(cue.get("cue_source") or "").strip() or "live_story_scene"
            cue["transition"] = _clamp_float(
                str(cue.get("transition") or min(duration * 0.28, 1.5)),
                1.1,
                0.6,
                1.8,
            )
            if not str(cue.get("effect") or "").strip():
                cue["effect"] = effect_defaults.get("effect", "steady")
            if not cue.get("effect_interval_ms"):
                if effect_defaults.get("effect_interval_ms") is not None:
                    cue["effect_interval_ms"] = effect_defaults["effect_interval_ms"]

        entry = {
            "start_seconds": round(start_seconds, 3),
            "end_seconds": round(start_seconds + duration, 3),
            "scene_index": scene_index,
            "scene_number": max(0, scene_number),
            "scene_description": scene_text,
            "hex_color": cue.get("hex_color"),
            "rgb_color": cue.get("rgb_color"),
            "brightness": cue.get("brightness"),
            "transition": cue.get("transition"),
            "cue_source": cue.get("cue_source"),
            "effect": cue.get("effect"),
            "effect_interval_ms": cue.get("effect_interval_ms"),
        }
        if built:
            previous = built[-1]
            if (
                previous.get("hex_color") == entry.get("hex_color")
                and previous.get("rgb_color") == entry.get("rgb_color")
                and previous.get("brightness") == entry.get("brightness")
                and previous.get("transition") == entry.get("transition")
                and previous.get("effect") == entry.get("effect")
                and previous.get("effect_interval_ms") == entry.get("effect_interval_ms")
            ):
                previous["end_seconds"] = entry["end_seconds"]
                start_seconds += duration
                continue
        built.append(entry)
        start_seconds += duration

    return built


def _extract_json_block(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _extract_response_text(response: Any) -> str:
    text = ""
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = list(candidates[0].content.parts)
            for part in parts:
                if getattr(part, "text", None):
                    text += str(part.text)
        elif getattr(response, "text", None):
            text = str(response.text)
        else:
            text = str(response)
    except Exception:
        text = str(response)
    return text.strip()


class StoryboardReviewFix(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene_index: int = Field(ge=1)
    severity: Literal["minor", "major", "critical"]
    issue: str
    repair_prompt: str


class StoryboardReviewResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    global_feedback: list[str] = Field(
        default_factory=list,
        max_length=2,
    )
    scene_fixes: list[StoryboardReviewFix] = Field(default_factory=list, max_length=4)


class NarrationLineRewriteResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    line: str


def _require_typed_model_response(
    response: Any,
    schema_type: type[BaseModel],
    *,
    label: str,
) -> dict[str, Any] | None:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema_type):
        return parsed.model_dump(exclude_none=True)
    if isinstance(parsed, dict):
        try:
            return schema_type.model_validate(parsed).model_dump(exclude_none=True)
        except Exception as exc:
            logger.warning(
                "%s returned parsed dict that failed %s validation: %s",
                label,
                schema_type.__name__,
                exc,
            )
            return None
    logger.warning(
        "%s returned no typed parsed response for schema %s (parsed_type=%s).",
        label,
        schema_type.__name__,
        type(parsed).__name__ if parsed is not None else "None",
    )
    return None


def _sniff_mime_type(image_bytes: bytes) -> str:
    stripped = image_bytes.lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _is_placeholder_scene_source(source: str) -> bool:
    return str(source or "").strip().lower().startswith("data:image/svg+xml")


def _vertex_ai_location() -> str:
    return (
        os.environ.get("VERTEX_AI_LOCATION")
        or os.environ.get("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    ).strip() or "us-central1"


def _is_reviewable_raster_image(image_bytes: bytes) -> bool:
    return _sniff_mime_type(image_bytes) in {"image/png", "image/jpeg", "image/webp"}


def _scene_text_for_index(scene_descriptions: list[str], idx: int, story_summary: str) -> str:
    if idx < len(scene_descriptions):
        text = str(scene_descriptions[idx]).strip()
        if text:
            return text
    sentences = _split_sentences(_clean_story_text(story_summary))
    if sentences:
        pick = min(idx, len(sentences) - 1)
        return sentences[pick]
    return f"Scene {idx + 1} of the story."


def _normalize_grounding_token(token: str) -> str:
    lowered = re.sub(r"[^a-z0-9']+", "", str(token or "").lower())
    if len(lowered) > 5 and lowered.endswith("ing"):
        lowered = lowered[:-3]
    elif len(lowered) > 4 and lowered.endswith("ied"):
        lowered = f"{lowered[:-3]}y"
    elif len(lowered) > 4 and lowered.endswith("ed"):
        lowered = lowered[:-2]
    elif len(lowered) > 4 and lowered.endswith("es"):
        lowered = lowered[:-2]
    elif len(lowered) > 3 and lowered.endswith("s") and not lowered.endswith("ss"):
        lowered = lowered[:-1]
    return lowered


def _significant_grounding_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw_token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", str(text or "")):
        token = _normalize_grounding_token(raw_token)
        if len(token) < 4:
            continue
        if token in _VISUAL_GROUNDING_STOPWORDS:
            continue
        tokens.add(token)
    return tokens


def _narration_mentions_unsupported_detail(
    candidate: str,
    *,
    source_line: str = "",
    scene_description: str = "",
    previous_scene_description: str = "",
    next_scene_description: str = "",
    previous_line: str = "",
    next_line: str = "",
) -> bool:
    candidate_tokens = _significant_grounding_tokens(candidate)
    if not candidate_tokens:
        return False
    allowed_tokens = set()
    for text in (
        source_line,
        scene_description,
        previous_scene_description,
        next_scene_description,
        previous_line,
        next_line,
    ):
        allowed_tokens.update(_significant_grounding_tokens(text))
    new_tokens = sorted(token for token in candidate_tokens if token not in allowed_tokens)
    return len(new_tokens) >= 2 or (len(new_tokens) == 1 and len(new_tokens[0]) >= 6)


def _page_narration_source_text(
    story_pages: list[dict[str, Any]],
    scene_descriptions: list[str],
    story_summary: str,
    idx: int,
) -> str:
    candidates: list[tuple[str, int]] = []
    if 0 <= idx < len(story_pages):
        page = story_pages[idx]
        candidates.extend(
            [
                (str(page.get("storybeat_text", "") or "").strip(), 3),
                (str(page.get("scene_description", "") or "").strip(), 2),
            ]
        )
    if 0 <= idx < len(scene_descriptions):
        candidates.append((str(scene_descriptions[idx] or "").strip(), 1))
    candidates.append((_scene_text_for_index(scene_descriptions, idx, story_summary), 0))

    best = ""
    best_score = -999
    for candidate, source_bonus in candidates:
        cleaned = _clean_story_text(candidate)
        if not cleaned:
            continue
        raw_sentences = _split_sentences(cleaned) or [cleaned]
        for raw_sentence in raw_sentences:
            normalized = shared_normalize_storybeat_text(raw_sentence, max_chars=400)
            if not normalized:
                continue
            repaired = _repair_storybook_sentence_fragment(normalized) or normalized
            words = len(repaired.split())
            score = story_sentence_quality_score(repaired) + source_bonus
            if repaired != normalized:
                score += 1
            if story_sentence_needs_revision(repaired):
                score -= 6
            if 4 <= words <= 16:
                score += 1
            elif words > 20:
                score -= 1
            if score > best_score or (score == best_score and len(repaired) > len(best)):
                best = repaired
                best_score = score
    return best


def _build_page_narration_source_texts(
    story_pages: list[dict[str, Any]],
    scene_descriptions: list[str],
    story_summary: str,
    scene_count: int,
) -> list[str]:
    target_count = max(0, int(scene_count or 0))
    return [
        _page_narration_source_text(story_pages, scene_descriptions, story_summary, idx)
        for idx in range(target_count)
    ]


def _make_image_part(google_genai: Any, image_bytes: bytes) -> Any:
    mime_type = _sniff_mime_type(image_bytes)
    return google_genai.types.Part.from_bytes(
        data=image_bytes,
        mime_type=mime_type,
    )


def _severity_rank(severity: str) -> int:
    normalized = str(severity or "").strip().lower()
    if normalized == "critical":
        return 3
    if normalized == "major":
        return 2
    if normalized == "minor":
        return 1
    return 0


def _review_category_rank(category: str) -> int:
    normalized = str(category or "").strip().lower()
    if normalized == "artifact_text":
        return 5
    if normalized == "safety":
        return 4
    if normalized == "diegetic_text":
        return 3
    if normalized == "continuity":
        return 2
    if normalized == "grounding":
        return 1
    if normalized == "other":
        return 1
    return 0


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _classify_storyboard_fix(issue: str, repair_prompt: str, severity: str) -> tuple[str, bool, str]:
    text = f"{issue} {repair_prompt}".lower()
    artifact_text_markers = (
        "watermark",
        "logo",
        "label",
        "caption",
        "subtitle",
        "signature",
        "signed",
        "ui text",
        "overlay text",
    )
    readable_text_markers = (
        "readable text",
        "clearly readable",
        "clearly legible",
        "legible text",
        "visible word",
        "written on its cover",
        "book cover has",
        "book spine",
        "title on",
    )
    decorative_text_markers = (
        "illegible text",
        "gibberish",
        "text-like",
        "letters",
        "letter-like",
        "symbols",
        "glyphs",
        "runes",
        "book pages",
        "page texture",
        "swirling magic",
        "river made of letters",
    )
    continuity_markers = (
        "inconsistent",
        "continuity",
        "missing",
        "appearance",
        "clothing",
        "character design",
        "sidekick",
        "prop",
        "location",
        "contradict",
    )
    safety_markers = (
        "scary",
        "harsh",
        "violent",
        "creepy",
        "intense",
        "threatening",
    )
    grounding_markers = (
        "does not match",
        "doesn't match",
        "misses",
        "wrong beat",
        "wrong scene",
        "not the intended",
    )

    if _contains_any(text, artifact_text_markers):
        return "artifact_text", True, severity
    if _contains_any(text, readable_text_markers) or re.search(r"(word|text)\s*['\"][A-Za-z0-9][^'\"]*['\"]", text):
        return "diegetic_text", True, severity
    if _contains_any(text, safety_markers):
        return "safety", True, severity
    if _contains_any(text, continuity_markers):
        actionable = _severity_rank(severity) >= 2
        return "continuity", actionable, severity
    if _contains_any(text, grounding_markers):
        actionable = _severity_rank(severity) >= 2
        return "grounding", actionable, severity
    if _contains_any(text, decorative_text_markers):
        return "decorative_text", False, "minor"
    actionable = _severity_rank(severity) >= 2
    return "other", actionable, severity


def _unresolved_storyboard_status(fixes: list[dict[str, Any]]) -> str:
    if not fixes:
        return "passed"
    for fix in fixes:
        category = str(fix.get("category", "")).strip().lower()
        if category == "artifact_text" or _severity_rank(fix.get("severity", "")) >= 3:
            return "needs_manual_review"
    return "passed_with_warnings"


def _normalize_review_fix(fix: dict[str, Any], total_scenes: int, direction: str) -> dict[str, Any] | None:
    try:
        scene_index = int(fix.get("scene_index", 0))
    except Exception:
        return None
    if scene_index < 1 or scene_index > total_scenes:
        return None
    issue = str(fix.get("issue", "")).strip()
    repair_prompt = str(fix.get("repair_prompt", "")).strip()
    severity = str(fix.get("severity", "major")).strip().lower() or "major"
    if severity not in {"minor", "major", "critical"}:
        severity = "major"
    if not issue:
        return None
    category, actionable, normalized_severity = _classify_storyboard_fix(issue, repair_prompt, severity)
    return {
        "scene_index": scene_index,
        "severity": normalized_severity,
        "category": category,
        "actionable": actionable,
        "issue": issue[:300],
        "repair_prompt": repair_prompt[:500],
        "directions": [direction],
    }


def _merge_storyboard_reviews(
    reviews: list[tuple[str, dict[str, Any] | None]],
    total_scenes: int,
) -> dict[str, Any]:
    merged: dict[int, dict[str, Any]] = {}
    global_feedback: list[str] = []
    for direction, review in reviews:
        if not review:
            continue
        for note in review.get("global_feedback", []) or []:
            note_text = str(note).strip()
            if note_text:
                global_feedback.append(f"{direction}: {note_text[:240]}")
        for raw_fix in review.get("scene_fixes", []) or []:
            if not isinstance(raw_fix, dict):
                continue
            fix = _normalize_review_fix(raw_fix, total_scenes, direction)
            if not fix:
                continue
            idx = fix["scene_index"]
            existing = merged.get(idx)
            if not existing:
                merged[idx] = fix
                continue
            existing["directions"] = sorted(set(existing.get("directions", []) + fix["directions"]))
            if fix["issue"] not in existing["issue"]:
                existing["issue"] = f"{existing['issue']}; {fix['issue']}"[:300]
            new_prompt = fix["repair_prompt"]
            if new_prompt and new_prompt not in existing["repair_prompt"]:
                if existing["repair_prompt"]:
                    existing["repair_prompt"] = (
                        f"{existing['repair_prompt']} Also ensure: {new_prompt}"
                    )[:500]
                else:
                    existing["repair_prompt"] = new_prompt[:500]
            if _severity_rank(fix["severity"]) > _severity_rank(existing["severity"]):
                existing["severity"] = fix["severity"]
            if _review_category_rank(fix.get("category", "")) > _review_category_rank(existing.get("category", "")):
                existing["category"] = fix.get("category", "other")
            existing["actionable"] = bool(existing.get("actionable")) or bool(fix.get("actionable"))

    scene_fixes = sorted(
        merged.values(),
        key=lambda item: (-_severity_rank(item.get("severity", "")), item.get("scene_index", 0)),
    )
    return {
        "passed": len(scene_fixes) == 0,
        "scene_fixes": scene_fixes,
        "global_feedback": global_feedback[:10],
    }


def _review_scene_window(
    scene_descriptions: list[str],
    story_summary: str,
    idx: int,
) -> dict[str, Any]:
    previous_desc = _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else ""
    current_desc = _scene_text_for_index(scene_descriptions, idx, story_summary)
    next_desc = _scene_text_for_index(scene_descriptions, idx + 1, story_summary) if idx + 1 < len(scene_descriptions) else ""
    return {
        "scene_index": idx + 1,
        "previous_scene": previous_desc,
        "current_scene": current_desc,
        "next_scene": next_desc,
    }


def _review_storyboard_pass(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str,
    still_paths: list[Path],
    direction: str,
    story_tone: str = "cozy",
) -> dict[str, Any] | None:
    if not PROJECT or not still_paths:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Storyboard review unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_SCENE_REVIEW_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    location = _vertex_ai_location()
    ordered_indices = list(range(len(still_paths)))
    if direction == "BACKWARD":
        ordered_indices.reverse()

    review_payload = []
    for review_order, idx in enumerate(ordered_indices, start=1):
        window = _review_scene_window(scene_descriptions, story_summary, idx)
        review_payload.append(
            {
                "review_order": review_order,
                "scene_index": window["scene_index"],
                "previous_scene": window["previous_scene"],
                "current_scene": window["current_scene"],
                "next_scene": window["next_scene"],
            }
        )
    meta_guidance = build_principles_injection_text("storyboard_review")

    prompt = f"""
You are a children's storybook art director reviewing a READ-ALOUD movie storyboard.
This is a still-only adaptation: each still is a page-like scene and the narration sets the pacing.

Review direction: {direction}
Child hero name: {child_name or "young hero"}
Story summary:
{story_summary or "No summary available."}

{meta_guidance}

Review goals:
- Keep character appearance, props, sidekicks, and locations temporally coherent.
- Make sure each still matches its own scene description and does not contradict adjacent scenes.
- Prefer what the actual stills show over any stray detail from the broad story summary.
- Flag repairs when a recurring character changes colors, outfit, shape, or species, or when an interior/exterior transition feels like a teleport instead of the next connected beat.
- Keep the art G-rated and book-like for ages 4-5. {_story_tone_art_guidance(story_tone)}
- Reject clearly readable overlay text, logos, labels, signatures, UI text, or watermarks.
- Ignore tiny unreadable book-page texture, abstract glyphs, and decorative letter-like motifs unless they are prominent and clearly pasted on top of the art.
- If a scene is intentionally book-centric (libraries, pages, alphabet rivers, floating words, magical letters, book spines), treat isolated letters, short alphabet clusters like "A B C", spine markings, and page callouts as valid diegetic story elements unless they look like subtitles, UI, modern branding, signatures, or watermark text layered over the illustration.
- Do not fail a strong storybook scene only because a few readable letters are physically embedded in books, pages, tiles, stars, or magical language effects.
- Only flag scenes that truly need regeneration and are worth the repair cost.
- Return compact JSON only.
- Keep every string under 320 characters.
- Use at most 2 `global_feedback` items and 4 `scene_fixes`.
- Keep each `repair_prompt` to one concise sentence.
- Do not include markdown, prose outside JSON, or extra keys.

Return JSON only in this schema:
{{
  "global_feedback": ["short note"],
  "scene_fixes": [
    {{
      "scene_index": 2,
      "severity": "major",
      "issue": "what is wrong",
      "repair_prompt": "positive prompt for regenerating only this scene while preserving story intent and continuity"
    }}
  ]
}}

Scene cards:
{json.dumps(review_payload, indent=2)}
""".strip()

    image_contents: list[Any] = []
    for review_order, idx in enumerate(ordered_indices, start=1):
        try:
            image_bytes = still_paths[idx].read_bytes()
        except Exception:
            continue
        if not _is_reviewable_raster_image(image_bytes):
            logger.warning(
                "Skipping non-raster storyboard review image for %s pass scene %s.",
                direction.lower(),
                idx + 1,
            )
            continue
        image_contents.append(f"Scene image {review_order} (original scene_index={idx + 1})")
        image_contents.append(_make_image_part(google_genai, image_bytes))

    try:
        client = google_genai.Client(vertexai=True, project=PROJECT, location=location)
    except Exception as exc:
        logger.warning("Storyboard %s review failed: %s", direction.lower(), exc)
        return None
    payload: dict[str, Any] | None = None
    for attempt in range(1, 3):
        retry_suffix = ""
        if attempt > 1:
            retry_suffix = (
                "\n\nRetry requirements: the previous response was rejected because it was not valid JSON for "
                "the required schema. Return compact JSON only, with no markdown, no commentary, and no extra keys."
            )
        contents: list[Any] = [f"{prompt}{retry_suffix}", *image_contents]
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=google_genai.types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=4096,
                    thinking_config=google_genai.types.ThinkingConfig(thinking_budget=0),
                    response_mime_type="application/json",
                    response_json_schema=StoryboardReviewResponse.model_json_schema(),
                ),
            )
        except Exception as exc:
            logger.warning(
                "Storyboard %s review attempt %d failed: %s",
                direction.lower(),
                attempt,
                exc,
            )
            continue
        payload = _require_typed_model_response(
            response,
            StoryboardReviewResponse,
            label=f"Storyboard {direction.lower()} review",
        )
        if isinstance(payload, dict):
            break
        logger.warning(
            "Storyboard %s review attempt %d returned no typed schema payload; retrying.",
            direction.lower(),
            attempt,
        )
    if not isinstance(payload, dict):
        logger.warning("Storyboard %s review returned no typed schema payload.", direction.lower())
        return None
    return payload


def _generate_repaired_story_still(
    scene_description: str,
    repair_prompt: str,
    story_summary: str,
    previous_description: str,
    next_description: str,
    current_image_bytes: bytes | None,
    previous_image_bytes: bytes | None,
    hero_anchor_description: str,
    hero_anchor_image_bytes: bytes | None,
    story_tone: str = "cozy",
) -> bytes | None:
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Scene repair unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_SCENE_IMAGE_MODEL", DEFAULT_VERTEX_IMAGE_MODEL).strip() or DEFAULT_VERTEX_IMAGE_MODEL
    image_size = os.environ.get("STORYBOOK_SCENE_IMAGE_SIZE", "512px").strip() or "512px"
    location = _vertex_ai_location()
    meta_guidance = build_principles_injection_text("storyboard_repair")
    prompt = f"""
Create a single 16:9 children's storybook illustration.

Base scene intent:
{scene_description}

Required fixes:
{repair_prompt}

Story summary:
{story_summary or "No summary available."}

{meta_guidance}

Continuity anchors:
- Previous scene: {previous_description or "None"}
- Next scene: {next_description or "None"}
- Opening hero anchor: {hero_anchor_description or "None"}

Rules:
- Hand-drawn, painterly 2D storybook look.
- Preschool-safe, classic read-aloud storybook energy. {_story_tone_art_guidance(story_tone)}
- Keep character identity, clothing, colors, props, and setting progression coherent.
- Preserve the scene's intended story beat.
- Do not introduce rainbow arches, rainbow color bands, or rainbow effects unless the scene description explicitly calls for them.
- No readable text, logos, labels, captions, signatures, or watermarks.
- Decorative storybook marks or unreadable page texture are acceptable only if they stay subtle and non-dominant.
- Diegetic letters, page markings, and magical word motifs are acceptable when the scene is explicitly about books or language.
- Preserve isolated letters, short alphabet clusters, book-spine marks, and page details when they are physically part of the scene; only remove text that reads like overlays, captions, UI, signatures, branding, or watermarks pasted on top of the art.
""".strip()

    contents: list[Any] = [prompt]
    if hero_anchor_image_bytes:
        contents.append("Opening hero anchor for design continuity:")
        contents.append(_make_image_part(google_genai, hero_anchor_image_bytes))
    if previous_image_bytes:
        contents.append("Previous approved scene for continuity:")
        contents.append(_make_image_part(google_genai, previous_image_bytes))
    if current_image_bytes:
        contents.append("Current scene to improve:")
        contents.append(_make_image_part(google_genai, current_image_bytes))

    try:
        client = google_genai.Client(vertexai=True, project=PROJECT, location=location)
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=google_genai.types.GenerateContentConfig(
                response_modalities=[google_genai.types.MediaModality.IMAGE],
                image_config=google_genai.types.ImageConfig(
                    aspect_ratio="16:9",
                    image_size=image_size,
                ),
            ),
        )
        return _extract_image_bytes(response)
    except Exception as exc:
        logger.warning("Scene repair generation failed: %s", exc)
        return None


async def _review_and_refine_storyboard(
    session_id: str,
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str,
    still_paths: list[Path],
    tmp: Path,
    story_tone: str = "cozy",
) -> tuple[list[Path], dict[str, Any]]:
    report: dict[str, Any] = {
        "status": "skipped",
        "passes": [],
        "repairs_applied": [],
    }
    if not _env_enabled("ENABLE_STORYBOOK_SCENE_REVIEW", default=True):
        report["reason"] = "scene review disabled"
        return still_paths, report
    if not PROJECT or not still_paths:
        report["reason"] = "missing project or stills"
        return still_paths, report

    max_passes = _clamp_int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_PASSES", "2"), 2, 1, 3)
    max_fixes = _clamp_int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_FIXES", "3"), 3, 0, 4)
    refined_paths = list(still_paths)

    for pass_index in range(1, max_passes + 1):
        forward_review = _review_storyboard_pass(
            scene_descriptions=scene_descriptions,
            story_summary=story_summary,
            child_name=child_name,
            still_paths=refined_paths,
            direction="FORWARD",
            story_tone=story_tone,
        )
        backward_review = _review_storyboard_pass(
            scene_descriptions=scene_descriptions,
            story_summary=story_summary,
            child_name=child_name,
            still_paths=refined_paths,
            direction="BACKWARD",
            story_tone=story_tone,
        )
        merged = _merge_storyboard_reviews(
            [("FORWARD", forward_review), ("BACKWARD", backward_review)],
            total_scenes=len(refined_paths),
        )
        fix_candidates = merged.get("scene_fixes", []) or []
        actionable_fixes = [fix for fix in fix_candidates if bool(fix.get("actionable"))]
        warning_fixes = [fix for fix in fix_candidates if not bool(fix.get("actionable"))]
        report["passes"].append(
            {
                "pass": pass_index,
                "fix_count": len(actionable_fixes),
                "warning_count": len(warning_fixes),
                "global_feedback": merged.get("global_feedback", []),
            }
        )
        feedback_issues = [
            str(fix.get("issue") or fix.get("repair_prompt") or "").strip()
            for fix in actionable_fixes
            if str(fix.get("issue") or fix.get("repair_prompt") or "").strip()
        ]
        record_prompt_feedback(
            "storyboard_review",
            outcome=(
                "fix_required"
                if actionable_fixes
                else "passed_with_warnings"
                if warning_fixes
                else "passed"
            ),
            issues=feedback_issues,
            prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
            session_id=session_id,
            metadata={
                "pass": pass_index,
                "fix_count": len(actionable_fixes),
                "warning_count": len(warning_fixes),
                "global_feedback": merged.get("global_feedback", [])[:3],
            },
            force_log=True,
        )
        if feedback_issues:
            record_prompt_feedback(
                "interactive_story",
                outcome="movie_review_fix_required",
                issues=feedback_issues,
                prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
                session_id=session_id,
                metadata={
                    "source": "storyboard_review",
                    "pass": pass_index,
                    "fix_count": len(actionable_fixes),
                },
            )
            record_prompt_feedback(
                "interactive_scene_visual",
                outcome="movie_review_fix_required",
                issues=feedback_issues,
                prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
                session_id=session_id,
                metadata={
                    "source": "storyboard_review",
                    "pass": pass_index,
                    "fix_count": len(actionable_fixes),
                },
            )

        if not actionable_fixes:
            report["status"] = "passed_with_warnings" if warning_fixes else "passed"
            report["final_review"] = merged
            return refined_paths, report

        if max_fixes <= 0 or pass_index >= max_passes:
            report["status"] = _unresolved_storyboard_status(actionable_fixes)
            report["final_review"] = merged
            return refined_paths, report

        repairs_this_pass = 0
        for fix in actionable_fixes[:max_fixes]:
            idx = int(fix["scene_index"]) - 1
            if idx < 0 or idx >= len(refined_paths):
                continue
            try:
                current_image_bytes = refined_paths[idx].read_bytes()
            except Exception:
                current_image_bytes = None
            previous_image_bytes = None
            if idx > 0:
                try:
                    previous_image_bytes = refined_paths[idx - 1].read_bytes()
                except Exception:
                    previous_image_bytes = None
            hero_anchor_image_bytes = None
            hero_anchor_description = ""
            if refined_paths:
                hero_anchor_description = _scene_text_for_index(scene_descriptions, 0, story_summary)
                if idx != 0:
                    try:
                        hero_anchor_image_bytes = refined_paths[0].read_bytes()
                    except Exception:
                        hero_anchor_image_bytes = None

            current_desc = _scene_text_for_index(scene_descriptions, idx, story_summary)
            previous_desc = _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else ""
            next_desc = _scene_text_for_index(scene_descriptions, idx + 1, story_summary) if idx + 1 < len(refined_paths) else ""

            repaired_bytes = _generate_repaired_story_still(
                scene_description=current_desc,
                repair_prompt=str(fix.get("repair_prompt") or fix.get("issue") or "").strip(),
                story_summary=story_summary,
                previous_description=previous_desc,
                next_description=next_desc,
                current_image_bytes=current_image_bytes,
                previous_image_bytes=previous_image_bytes,
                hero_anchor_description=hero_anchor_description,
                hero_anchor_image_bytes=hero_anchor_image_bytes,
                story_tone=story_tone,
            )
            if not repaired_bytes:
                record_prompt_feedback(
                    "storyboard_repair",
                    outcome="repair_failed",
                    issues=[str(fix.get("issue") or fix.get("repair_prompt") or "").strip()],
                    prompt_text=current_desc,
                    session_id=session_id,
                    metadata={
                        "pass": pass_index,
                        "scene_index": idx + 1,
                        "severity": fix.get("severity", "major"),
                    },
                )
                continue

            repaired_path = tmp / f"scene_{idx:03d}_repair_pass_{pass_index}.png"
            repaired_path.write_bytes(repaired_bytes)
            refined_paths[idx] = repaired_path
            repairs_this_pass += 1
            report["repairs_applied"].append(
                {
                    "pass": pass_index,
                    "scene_index": idx + 1,
                    "severity": fix.get("severity", "major"),
                    "issue": str(fix.get("issue", ""))[:240],
                    "directions": fix.get("directions", []),
                }
            )
            record_prompt_feedback(
                "storyboard_repair",
                outcome="repair_applied",
                issues=[str(fix.get("issue") or fix.get("repair_prompt") or "").strip()],
                prompt_text=current_desc,
                session_id=session_id,
                metadata={
                    "pass": pass_index,
                    "scene_index": idx + 1,
                    "severity": fix.get("severity", "major"),
                    "directions": fix.get("directions", []),
                },
            )
            logger.info(
                "Storyboard repair applied for scene %d on pass %d (%s).",
                idx + 1,
                pass_index,
                fix.get("severity", "major"),
            )

        if repairs_this_pass == 0:
            record_prompt_feedback(
                "storyboard_repair",
                outcome=_unresolved_storyboard_status(actionable_fixes),
                issues=feedback_issues,
                prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
                session_id=session_id,
                metadata={
                    "pass": pass_index,
                    "fix_count": len(actionable_fixes),
                },
            )
            report["status"] = _unresolved_storyboard_status(actionable_fixes)
            report["final_review"] = merged
            return refined_paths, report

    report["status"] = "passed_with_repairs"
    return refined_paths, report


def _llm_audio_cue_plan(
    scene_descriptions: list[str],
    story_summary: str,
    max_music: int,
    max_sfx: int,
    still_paths: list[Path] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]] | None:
    """Use Gemini to choose which scenes get music/SFX cues."""
    if not _env_enabled("ENABLE_STORYBOOK_AUDIO_LLM", default=False):
        return None
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Audio cue LLM unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_AUDIO_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    client = google_genai.Client(vertexai=True, project=PROJECT, location=_vertex_ai_location())

    scene_lines = []
    for idx, desc in enumerate(scene_descriptions, start=1):
        scene_lines.append(f"{idx}. {desc.strip() or 'Scene with no description.'}")

    prompt = f"""
You are a small panel of creative advisors:
- LeVar Burton (warm, clear storytelling)
- Fred Rogers (gentle emotional truth)
- Julie Andrews (musical phrasing and cadence)
- Alan Menken (theme cues for opening/closing)
- Ben Burtt (purposeful physical SFX only)

Task: Decide which scenes deserve short MUSIC cues and which deserve SFX cues.
Constraints:
- Music should be minimal: opening and closing, optional single midpoint lift.
- SFX only if a clear physical action is present.
- Max music cues: {max_music}. Max SFX cues: {max_sfx}.
- Use 1-based scene_index values.
- Output JSON ONLY with the schema:
{{"music_cues":[{{"scene_index":1,"prompt":"...","duration_seconds":6}}],"sfx_cues":[{{"scene_index":2,"prompt":"...","duration_seconds":2}}]}}
Use a brief internal tree-of-thought and do not output your reasoning.

Story summary:
{story_summary.strip() or "No summary available."}

Scene descriptions:
{chr(10).join(scene_lines)}
""".strip()

    contents: list[Any] = [prompt]
    if _env_enabled("ENABLE_STORYBOOK_AUDIO_LLM_IMAGES", default=False) and still_paths:
        for idx, path in enumerate(still_paths, start=1):
            try:
                img_bytes = path.read_bytes()
            except Exception:
                continue
            contents.append(f"Scene {idx} image:")
            try:
                contents.append(
                    google_genai.types.Part.from_bytes(
                        data=img_bytes,
                        mime_type="image/png",
                    )
                )
            except Exception:
                continue

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
        )
    except Exception as exc:
        logger.warning("Audio cue LLM request failed: %s", exc)
        return None

    text = ""
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = list(candidates[0].content.parts)
            for part in parts:
                if getattr(part, "text", None):
                    text += str(part.text)
        else:
            text = str(response)
    except Exception:
        text = str(response)

    payload = _extract_json_block(text)
    if not payload:
        logger.warning("Audio cue LLM returned no parsable JSON.")
        return None

    music: dict[int, dict[str, Any]] = {}
    sfx: dict[int, dict[str, Any]] = {}

    for cue in payload.get("music_cues", []) or []:
        try:
            idx = int(cue.get("scene_index", 0)) - 1
        except Exception:
            continue
        if idx < 0 or idx >= len(scene_descriptions):
            continue
        prompt_text = str(cue.get("prompt", "")).strip()
        if not prompt_text:
            continue
        music[idx] = {"prompt": prompt_text, "duration_seconds": cue.get("duration_seconds")}

    for cue in payload.get("sfx_cues", []) or []:
        try:
            idx = int(cue.get("scene_index", 0)) - 1
        except Exception:
            continue
        if idx < 0 or idx >= len(scene_descriptions):
            continue
        prompt_text = str(cue.get("prompt", "")).strip()
        if not prompt_text:
            continue
        sfx[idx] = {"prompt": prompt_text, "duration_seconds": cue.get("duration_seconds")}

    if not music and not sfx:
        return None
    return music, sfx


def _heuristic_audio_cues(
    scene_descriptions: list[str],
    total: int,
    max_music: int,
    max_sfx: int,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    music: dict[int, dict[str, Any]] = {}
    sfx: dict[int, dict[str, Any]] = {}
    if total <= 0:
        return music, sfx
    # Music: opening + closing only by default.
    if max_music > 0:
        music_indices = [0]
        if total > 1:
            music_indices.append(total - 1)
        for idx in dict.fromkeys(music_indices):  # preserve order, unique
            desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
            music[idx] = {"prompt": _build_music_prompt(desc, idx, total)}

    if max_sfx > 0:
        min_score = _clamp_int(os.environ.get("STORYBOOK_SFX_MIN_SCORE", "2"), 2, 1, 6)
        cooldown = clamp_sfx_cooldown_pages(os.environ.get("STORYBOOK_SFX_COOLDOWN", "1"))
        sfx_indices = _choose_sfx_indices(
            total=total,
            max_count=max_sfx,
            descriptions=scene_descriptions,
            min_score=min_score,
            cooldown=cooldown,
        )
        for idx in sfx_indices:
            desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
            sfx[idx] = {"prompt": _build_sfx_prompt(desc)}

    return music, sfx


def _plan_audio_cues(
    scene_descriptions: list[str],
    story_summary: str,
    total: int,
    still_paths: list[Path] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    max_music = _clamp_int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"), 2, 0, 4)
    max_sfx = clamp_sfx_max(os.environ.get("STORYBOOK_SFX_MAX", "2"))
    plan = _llm_audio_cue_plan(scene_descriptions, story_summary, max_music, max_sfx, still_paths)
    if plan:
        return plan
    return _heuristic_audio_cues(scene_descriptions, total, max_music, max_sfx)


def _llm_rewrite_storybook_narration_line(
    line: str,
    *,
    scene_description: str,
    story_summary: str,
    child_age: int | str | None,
    max_words: int,
    previous_scene_description: str = "",
    next_scene_description: str = "",
    previous_line: str = "",
    next_line: str = "",
) -> str | None:
    source_line = shared_normalize_storybeat_text(line, max_chars=320)
    if not source_line:
        return None
    source_line_has_unsupported_detail = _narration_mentions_unsupported_detail(
        source_line,
        source_line=line,
        scene_description=scene_description,
        previous_scene_description=previous_scene_description,
        next_scene_description=next_scene_description,
        previous_line=previous_line,
        next_line=next_line,
    )
    if (
        len(source_line.split()) <= max_words
        and story_sentence_quality_score(source_line) >= 2
        and not story_sentence_needs_revision(source_line)
        and not source_line_has_unsupported_detail
    ):
        return source_line
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Narration rewrite unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_STUDIO_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    location = _vertex_ai_location()
    prompt = f"""
You rewrite one preschool narration line so it stays complete, gentle, and short.

Child age: {child_age or 4}
Max words: {max_words}
Story summary: {story_summary or "No summary available."}
Scene description: {scene_description or "No scene description available."}
Current line: {source_line}
Previous scene description: {previous_scene_description or "None"}
Next scene description: {next_scene_description or "None"}
Previous approved narration line: {previous_line or "None"}
Next raw narration line: {next_line or "None"}

Rules:
- Keep the same story meaning and stay grounded in what the current page actually shows.
- Treat the scene description and current line as the source of truth for the visible details.
- Use the story summary only for broad continuity, never for adding a new place, object, prop, or character.
- If helpful, make the line flow naturally from the previous beat toward the next one without inventing a brand-new location.
- Do not introduce any destination, landmark, animal, prop, or room unless it is already supported by the current line, the scene description, or the adjacent scene descriptions.
- Return one complete sentence only.
- Use at most {max_words} words.
- Fix duplicated words like "is is".
- Do not end on dangling words like "a big" or "the soft".
- Do not end on weak words like "and", "or", or "but".
- Do not use filler like "The end appears."
- Do not use a generic placeholder ending like "appears."
- Keep it warm, clear, and natural for a 4-year-old.

Return JSON only:
{{
  "line": "one complete short sentence"
}}
""".strip()
    try:
        client = google_genai.Client(vertexai=True, project=PROJECT, location=location)
    except Exception as exc:
        logger.warning("Narration rewrite client setup failed: %s", exc)
        return None

    for attempt in range(1, 3):
        retry_suffix = ""
        if attempt > 1:
            retry_suffix = (
                f"\n\nRetry requirements: the previous response was invalid. Return one complete sentence, "
                f"at most {max_words} words, inside the required JSON schema."
            )
        try:
            response = client.models.generate_content(
                model=model,
                contents=[f"{prompt}{retry_suffix}"],
                config=google_genai.types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=256,
                    thinking_config=google_genai.types.ThinkingConfig(thinking_budget=0),
                    response_mime_type="application/json",
                    response_json_schema=NarrationLineRewriteResponse.model_json_schema(),
                ),
            )
        except Exception as exc:
            logger.warning("Narration rewrite attempt %d failed: %s", attempt, exc)
            continue
        payload = _require_typed_model_response(
            response,
            NarrationLineRewriteResponse,
            label="Narration line rewrite",
        )
        candidate = shared_normalize_storybeat_text((payload or {}).get("line"), max_chars=220)
        if not candidate:
            continue
        if _narration_mentions_unsupported_detail(
            candidate,
            source_line=source_line,
            scene_description=scene_description,
            previous_scene_description=previous_scene_description,
            next_scene_description=next_scene_description,
            previous_line=previous_line,
            next_line=next_line,
        ):
            logger.info("Rejected narration rewrite that added unsupported visual detail: %s", candidate)
            continue
        if len(candidate.split()) > max_words:
            continue
        if story_sentence_quality_score(candidate) < 2:
            continue
        if story_sentence_needs_revision(candidate):
            continue
        return candidate
    return None


def _split_sentences(text: str) -> list[str]:
    return shared_split_story_sentences(text)


def _clean_story_text(text: str) -> str:
    cleaned = shared_clean_story_text(text)
    if not cleaned:
        return ""
    cleaned = re.sub(r"🌟\\s*What should we do\\?.*?(\\n|$)", " ", cleaned)
    cleaned = re.sub(r"(?:Should we|Do you want to|Or maybe)\\b.*$", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\\s+", " ", cleaned)
    return cleaned.strip()


def _apply_preschool_wording(text: str) -> str:
    if not text:
        return ""
    replacements = [
        (r"\bmajestic\b", "very tall"),
        (r"\bglittering\b", "sparkly"),
        (r"\bshimmering\b", "sparkly"),
        (r"\bnestled\b", "on"),
        (r"\bflutter(?:ing)?\b", "flying"),
        (r"\bsprites\b", "little friends"),
        (r"\btowering\b", "tall"),
        (r"\bgentle\b", "soft"),
        (r"\bwhimsical\b", "magical"),
        (r"\bmajesty\b", "magic"),
    ]
    normalized = text
    for pattern, replacement in replacements:
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _simplify_for_kids(text: str, max_words: int = 28) -> str:
    cleaned = _apply_preschool_wording(_clean_story_text(text))
    if not cleaned:
        return ""
    sentences = _split_sentences(cleaned)
    if not sentences:
        return ""
    short = " ".join(sentences[:2])
    words = short.split()
    if len(words) > max_words:
        short = " ".join(words[:max_words]).rstrip(".,") + "."
    return short


def _simplify_for_readalong(text: str, max_words: int = 12) -> str:
    cleaned = _apply_preschool_wording(_clean_story_text(text))
    if not cleaned:
        return ""
    repaired = _repair_storybook_sentence_fragment(cleaned) or cleaned
    first = shared_truncate_story_sentence(repaired, max_words=max_words)
    first = re.sub(r"^(look|see|wow|hey|oh|oh wow)\b[!,.:\s]*", "", first, flags=re.IGNORECASE).strip()
    if not first:
        return ""
    first = first.rstrip(".,!?") + "."
    return first


_TEMPORAL_START_RE = re.compile(r"^(then|next|while|as)\b", flags=re.IGNORECASE)
_SPATIAL_START_RE = re.compile(r"^(here|there|in|on|at)\b", flags=re.IGNORECASE)


def _decapitalize(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:] if text[0].isupper() else text


def _apply_readalong_connectors(line: str, idx: int) -> str:
    return line.lstrip()


_WEAK_NARRATION_START_RE = re.compile(
    r"^(?:then|next|now|here)\s*,?\s*(?:go|look|see|pick|choose|find|follow|slide|swim|dance|turn|climb|open|peek|come)\b",
    flags=re.IGNORECASE,
)
_DESCRIPTIVE_FRAGMENT_RE = re.compile(
    r"^(?P<prefix>(?:A|An|The)\s+.+?)\s+(?P<participle>filled|covered|made|decorated|lined|wrapped|tucked|lit)\b",
    flags=re.IGNORECASE,
)
_SUBJECT_GERUND_RE = re.compile(
    r"^(?P<subject>(?:A|An|The)\s+[^,.!?]+?)\s+(?P<gerund>glowing|sparkling|shimmering|twinkling|floating|winding|curling|sleeping|resting)\b",
    flags=re.IGNORECASE,
)
_LEADING_ACTION_RE = re.compile(
    r",?\s+leading\s+(?P<prep>towards?|to)\b",
    flags=re.IGNORECASE,
)
def _repair_storybook_sentence_fragment(text: str) -> str:
    sentence = shared_normalize_storybeat_text(text, max_chars=320)
    if not sentence:
        return ""
    if _WEAK_NARRATION_START_RE.match(sentence):
        return ""

    repaired = sentence
    repaired = _SUBJECT_GERUND_RE.sub(r"\g<subject> is \g<gerund>", repaired, count=1)
    repaired = _LEADING_ACTION_RE.sub(r" and leads \g<prep>", repaired, count=1)
    repaired = _DESCRIPTIVE_FRAGMENT_RE.sub(r"\g<prefix> is \g<participle>", repaired, count=1)
    if repaired.startswith("There are ") and " holding " in repaired:
        subject, _, obj = repaired[len("There are "):].partition(" holding ")
        subject = subject.strip(" ,")
        obj = re.split(r"\b(?:floating|around|near|beside|by|under)\b", obj, maxsplit=1, flags=re.IGNORECASE)[0]
        obj = obj.strip(" ,.")
        if subject and obj:
            verb = "hold" if subject.lower().endswith("s") else "holds"
            repaired = f"{subject[:1].upper()}{subject[1:]} {verb} {obj}."

    sentence_score = story_sentence_quality_score(sentence)
    repaired_score = story_sentence_quality_score(repaired)
    if repaired != sentence and (
        repaired_score >= sentence_score
        or (repaired_score >= 2 and len(repaired.split()) < len(sentence.split()))
    ):
        sentence = repaired
        sentence_score = repaired_score

    if sentence_score >= 2:
        return sentence

    bare = sentence.rstrip(".!?").strip()
    if not bare:
        return ""
    if re.fullmatch(r"the end", bare, flags=re.IGNORECASE):
        return "The end."
    if sentence_score < 2:
        repaired = f"{bare} appears."
        if story_sentence_quality_score(repaired) >= sentence_score:
            return repaired
    return sentence


def _normalize_storybook_narration_line(
    text: str,
    *,
    fallback: str = "",
    max_words: int = 12,
    scene_index: int = 0,
) -> str:
    primary = _simplify_for_readalong(text, max_words=max_words)
    fallback_line = _simplify_for_readalong(fallback, max_words=max_words)
    primary_score = story_sentence_quality_score(primary)
    fallback_score = story_sentence_quality_score(fallback_line)
    if fallback_line and (fallback_score >= primary_score + 1 or primary_score < 2):
        return _apply_readalong_connectors(fallback_line, scene_index)
    if primary:
        return _apply_readalong_connectors(primary, scene_index)
    return _apply_readalong_connectors(fallback_line, scene_index) if fallback_line else ""


def _build_narration_segments(
    scene_descriptions: list[str],
    story_summary: str,
    scene_count: int,
    *,
    child_age: int | str | None = None,
) -> list[str]:
    narration: list[str] = []
    max_words = narration_max_words_for_age(child_age)
    summary_sentences = _split_sentences(_clean_story_text(story_summary))
    chunk_size = max(1, len(summary_sentences) // max(1, scene_count)) if summary_sentences else 0

    for idx in range(scene_count):
        desc = str(scene_descriptions[idx]).strip() if idx < len(scene_descriptions) else ""
        fallback_line = ""
        if not desc and summary_sentences:
            chunk = summary_sentences[idx * chunk_size:(idx + 1) * chunk_size]
            if not chunk and summary_sentences:
                chunk = [summary_sentences[min(idx, len(summary_sentences) - 1)]]
            fallback_line = " ".join(chunk)
        line = _normalize_storybook_narration_line(
            desc,
            fallback=fallback_line,
            max_words=max_words,
            scene_index=idx,
        )
        narration.append(line if line else "")
    return narration


def _build_music_prompt(description: str, idx: int, total: int) -> str:
    base = "gentle, whimsical, child-friendly instrumental music"
    desc = description.strip()
    if desc:
        return (
            f"{base} for a story scene. Scene details: {desc}. "
            "No vocals, no lyrics, soft bells, warm pads, light marimba."
        )
    position = "opening" if idx == 0 else "ending" if idx == total - 1 else "middle"
    return (
        f"{base} for the {position} of a children's adventure. "
        "No vocals, no lyrics, soft bells, warm pads, light marimba."
    )


def _ensure_end_card_music_cue(
    music_cues: dict[int, dict[str, Any]] | None,
    *,
    end_card_index: int | None,
    audio_scene_descriptions: list[str],
    scene_durations: list[float],
    studio_music_arc_prompt: str = "",
) -> dict[int, dict[str, Any]]:
    normalized = {int(idx): dict(cue or {}) for idx, cue in dict(music_cues or {}).items()}
    if end_card_index is None or end_card_index < 0 or end_card_index >= len(scene_durations):
        return normalized
    if normalized.get(end_card_index):
        return normalized
    desc = audio_scene_descriptions[end_card_index] if end_card_index < len(audio_scene_descriptions) else ""
    prompt = str(studio_music_arc_prompt or "").strip() or _build_music_prompt(desc, end_card_index, len(scene_durations))
    normalized[end_card_index] = {
        "prompt": prompt,
        "duration_seconds": min(10.0, max(2.0, float(scene_durations[end_card_index]))),
    }
    return normalized


def _storybook_voice_filter_chain(narration_volume: float) -> str:
    return (
        "highpass=f=100,"
        "lowpass=f=7200,"
        "dynaudnorm=f=180:g=13:p=0.92,"
        "acompressor=threshold=0.08:ratio=3.6:attack=16:release=220:makeup=2.1,"
        f"volume={narration_volume}"
    )


def _score_sfx(description: str) -> int:
    text = (description or "").lower()
    if not text:
        return 0
    keywords = [
        "door", "castle", "sparkle", "sparkly", "twinkle", "magic", "magical",
        "jump", "bounce", "boing", "splash", "river", "wave", "wind",
        "whoosh", "glow", "pop", "candy", "bubble", "giggle",
    ]
    score = 0
    for kw in keywords:
        if kw in text:
            score += 1
    return score


def _choose_sfx_indices(
    total: int,
    max_count: int,
    descriptions: list[str],
    min_score: int,
    cooldown: int,
) -> list[int]:
    if total <= 0 or max_count <= 0:
        return []
    max_count = max(0, min(max_count, total))
    min_score = max(1, min(min_score, 6))
    cooldown = max(0, min(cooldown, 3))
    scored: list[tuple[int, int]] = []
    for idx in range(total):
        desc = descriptions[idx] if idx < len(descriptions) else ""
        score = _score_sfx(desc)
        if score >= min_score:
            scored.append((idx, score))
    scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
    picked: list[int] = []
    for idx, _score in scored:
        if any(abs(idx - p) <= cooldown for p in picked):
            continue
        picked.append(idx)
        if len(picked) >= max_count:
            break
    return sorted(picked)


def _build_sfx_prompt(description: str) -> str:
    text = (description or "").lower()
    if "door" in text:
        return "soft magical door creak with candy chimes, gentle"
    if "jump" in text or "bounce" in text:
        return "soft bouncy boing, cute and gentle"
    if "sparkle" in text or "twinkle" in text or "magic" in text:
        return "magical sparkle twinkle, light and shimmering"
    if "splash" in text or "river" in text or "wave" in text:
        return "tiny water splash, playful and light"
    if "wind" in text or "whoosh" in text:
        return "soft magical whoosh, warm and gentle"
    return "gentle magical chime sparkle, light and child-friendly"


def _wrap_caption(text: str, width: int = 26, max_lines: int = 2) -> str:
    if not text:
        return ""
    lines = textwrap.wrap(text, width=width)
    return "\n".join(lines[:max_lines])


def _ffmpeg_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _ffmpeg_filter_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", r"\'")


def _tokenize_storybook_readalong_words(text: str) -> list[str]:
    return re.findall(r"\S+", str(text or ""))


def _normalize_storybook_word_token(text: str) -> str:
    return re.sub(r"^[^A-Za-z0-9']+|[^A-Za-z0-9']+$", "", str(text or "")).lower()


def _estimate_storybook_word_weight(raw_word: str) -> float:
    bare_word = re.sub(r"^[^A-Za-z0-9']+|[^A-Za-z0-9'!?.,;:]+$", "", str(raw_word or ""))
    lowered = bare_word.lower()
    vowel_groups = re.findall(r"[aeiouy]+", lowered)
    syllables = len(vowel_groups) or 1
    if syllables > 1 and re.search(r"(?:e|es|ed)$", lowered) and not re.search(r"(?:le|ue|ee)$", lowered):
        syllables -= 1

    weight = 0.92 + syllables * 0.23 + min(max(len(bare_word), 1), 12) * 0.014
    if re.search(r"[,:;]", raw_word):
        weight += 0.18
    if re.search(r"[-–—]", raw_word):
        weight += 0.12
    if re.search(r"[.!?]", raw_word):
        weight += 0.3
    return weight


def _approximate_storybook_word_starts_ms(text: str, duration_s: float) -> list[int]:
    words = _tokenize_storybook_readalong_words(text)
    if not words or duration_s <= 0:
        return []
    total_ms = max(1, int(round(float(duration_s) * 1000)))
    weights = [_estimate_storybook_word_weight(word) for word in words]
    total_weight = sum(weights) or float(len(words))
    starts_ms: list[int] = []
    elapsed_weight = 0.0
    last_start_ms = 0
    for weight in weights:
        start_ms = int(round((elapsed_weight / total_weight) * total_ms))
        start_ms = max(last_start_ms, min(start_ms, total_ms))
        starts_ms.append(start_ms)
        last_start_ms = start_ms
        elapsed_weight += weight
    return starts_ms


def _coerce_float_sequence(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    coerced: list[float] = []
    for value in values:
        try:
            coerced.append(float(value))
        except Exception:
            return []
    return coerced


def _extract_elevenlabs_word_timings_seconds(
    payload: dict[str, Any],
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
) -> list[tuple[str, float, float]]:
    alignment = (
        payload.get("alignment")
        or payload.get("normalized_alignment")
        or payload.get("normalizedAlignment")
        or {}
    )
    if not isinstance(alignment, dict):
        return []
    chars_raw = alignment.get("characters")
    if not isinstance(chars_raw, list):
        return []
    start_values = (
        alignment.get("character_start_times_seconds")
        or alignment.get("characterStartTimesSeconds")
        or []
    )
    end_values = (
        alignment.get("character_end_times_seconds")
        or alignment.get("characterEndTimesSeconds")
        or []
    )
    starts = _coerce_float_sequence(start_values)
    ends = _coerce_float_sequence(end_values)
    chars = [str(ch or "") for ch in chars_raw]
    if not chars or len(chars) != len(starts) or len(chars) != len(ends):
        return []

    tempo = storybook_tts_tempo_factor(child_age, storybook_movie_pacing)
    stretch_factor = 1.0 / tempo if abs(tempo) > 0.01 else 1.0
    timings: list[tuple[str, float, float]] = []
    current_chars: list[str] = []
    current_start: float | None = None
    current_end: float | None = None
    for ch, start_s, end_s in zip(chars, starts, ends):
        if ch.isspace():
            if current_chars and current_start is not None and current_end is not None:
                timings.append(
                    (
                        "".join(current_chars),
                        max(0.0, current_start) * stretch_factor,
                        max(0.0, current_end) * stretch_factor,
                    )
                )
            current_chars = []
            current_start = None
            current_end = None
            continue
        if current_start is None:
            current_start = start_s
        current_end = end_s
        current_chars.append(ch)
    if current_chars and current_start is not None and current_end is not None:
        timings.append(
            (
                "".join(current_chars),
                max(0.0, current_start) * stretch_factor,
                max(0.0, current_end) * stretch_factor,
            )
        )
    return timings


def _exact_storybook_word_starts_ms(
    text: str,
    word_timings: list[tuple[str, float, float]] | None,
    duration_s: float,
) -> list[int]:
    words = _tokenize_storybook_readalong_words(text)
    if not words or not word_timings or duration_s <= 0:
        return []
    if len(words) != len(word_timings):
        return []
    normalized_words = [_normalize_storybook_word_token(word) for word in words]
    normalized_timings = [_normalize_storybook_word_token(word) for word, _, _ in word_timings]
    if any(expected != actual for expected, actual in zip(normalized_words, normalized_timings)):
        return []
    last_end_s = max((max(0.0, end_s) for _, _, end_s in word_timings), default=0.0)
    if last_end_s <= 0.01:
        return []
    total_ms = max(1, int(round(float(duration_s) * 1000)))
    scale = float(duration_s) / last_end_s
    starts_ms: list[int] = []
    last_start_ms = 0
    for _word, start_s, _end_s in word_timings:
        start_ms = int(round(max(0.0, start_s) * scale * 1000))
        start_ms = max(last_start_ms, min(start_ms, total_ms))
        starts_ms.append(start_ms)
        last_start_ms = start_ms
    return starts_ms


def _resolve_storybook_word_starts_ms(
    text: str,
    duration_s: float,
    *,
    word_timings: list[tuple[str, float, float]] | None = None,
) -> tuple[list[int], str]:
    exact = _exact_storybook_word_starts_ms(text, word_timings, duration_s)
    if exact:
        return exact, "exact"
    estimated = _approximate_storybook_word_starts_ms(text, duration_s)
    if estimated:
        return estimated, "estimated"
    return [], "none"


_STORYBOOK_CAPTION_FONT_CANDIDATES: tuple[Path, ...] = (
    _IMPORT_ROOT / "shared" / "assets" / "fonts" / "Fredoka.ttf",
    Path("/app/shared/assets/fonts/Fredoka.ttf"),
)
_STORYBOOK_COVER_LOGO_CANDIDATES: tuple[Path, ...] = (
    _IMPORT_ROOT / "frontend" / "public" / "voxitale_arch.png",
    Path("/app/frontend/public/voxitale_arch.png"),
)


def _storybook_caption_font_option() -> str:
    for candidate in _STORYBOOK_CAPTION_FONT_CANDIDATES:
        if candidate.exists():
            return f"fontfile={candidate}"
    return "font=Sans"


def _storybook_cover_logo_path() -> Path | None:
    for candidate in _STORYBOOK_COVER_LOGO_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _storybook_caption_fonts_dir() -> Path | None:
    for candidate in _STORYBOOK_CAPTION_FONT_CANDIDATES:
        if candidate.exists():
            return candidate.parent
    return None


def _storybook_burned_caption_segments(
    scene_durations: list[float],
    fallback_lines: list[str],
    story_pages: list[dict[str, Any]] | None = None,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    cover_offset: int = 0,
    skip_indices: set[int] | None = None,
) -> list[tuple[float, float, str, int]]:
    segments: list[tuple[float, float, str, int]] = []
    cursor = 0.0
    pages = list(story_pages or [])
    skipped = set(skip_indices or set())
    for idx, duration in enumerate(scene_durations):
        page_idx = idx - cover_offset
        text = ""
        page_number = page_idx + 1
        if page_idx < 0 or idx in skipped:
            cursor += float(duration)
            continue
        primary_text = str(fallback_lines[idx] or "").strip() if idx < len(fallback_lines) else ""
        fallback_text = ""
        if page_idx < len(pages):
            fallback_text = str(pages[page_idx].get("storybeat_text", "") or "").strip()
        text = choose_readalong_text(primary_text, fallback_text, child_age, storybook_movie_pacing)
        if text:
            segments.append((cursor, cursor + float(duration), text, page_number))
        cursor += float(duration)
    return segments


def _storybook_burned_caption_filtergraph(
    segments: list[tuple[float, float, str, int]],
) -> list[str]:
    filters: list[str] = []
    font_option = _storybook_caption_font_option()
    for start_s, end_s, text, page_number in segments:
        wrapped = _wrap_caption(text, width=46)
        if not wrapped:
            continue
        escaped_text = _ffmpeg_escape(wrapped.replace("'", "’"))
        escaped_page = _ffmpeg_escape(f"Page {page_number}")
        enable_expr = f"between(t\\,{start_s:.3f}\\,{end_s:.3f})"
        filters.extend([
            f"drawbox=x={_STORYBOOK_CAPTION_BOX_X}:y={_STORYBOOK_CAPTION_BOX_Y}:w={_STORYBOOK_CAPTION_BOX_W}:h={_STORYBOOK_CAPTION_BOX_H}:color=0xF8EEDC@0.965:t=fill:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_CAPTION_BOX_X}:y={_STORYBOOK_CAPTION_BOX_Y}:w={_STORYBOOK_CAPTION_BOX_W}:h={_STORYBOOK_CAPTION_BOX_H}:color=0xC5AA79@0.26:t=2:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_CAPTION_RULE_X}:y={_STORYBOOK_CAPTION_RULE_Y}:w={_STORYBOOK_CAPTION_BOX_W - 120}:h=1:color=0xC5AA79@0.30:t=fill:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_PAGE_CHIP_X}:y={_STORYBOOK_PAGE_CHIP_Y}:w={_STORYBOOK_PAGE_CHIP_W}:h={_STORYBOOK_PAGE_CHIP_H}:color=0xEBDDC6@0.985:t=fill:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_PAGE_CHIP_X}:y={_STORYBOOK_PAGE_CHIP_Y}:w={_STORYBOOK_PAGE_CHIP_W}:h={_STORYBOOK_PAGE_CHIP_H}:color=0xC5AA79@0.32:t=2:enable='{enable_expr}'",
            f"drawtext=text='{escaped_page}':fontcolor=0x8A6233:fontsize=15:x={_STORYBOOK_PAGE_CHIP_X + 14}:y={_STORYBOOK_PAGE_CHIP_Y + 6}:{font_option}:enable='{enable_expr}'",
            f"drawtext=text='{escaped_text}':fontcolor=0x2A1842:fontsize=25:x={_STORYBOOK_CAPTION_TEXT_X}:y={_STORYBOOK_CAPTION_TEXT_Y}:{font_option}:line_spacing=8:enable='{enable_expr}'",
        ])
    return filters


def _storybook_burned_caption_chrome_filtergraph(
    segments: list[dict[str, Any]],
) -> list[str]:
    filters: list[str] = []
    font_option = _storybook_caption_font_option()
    for segment in segments:
        start_ms = max(0, int(segment.get("start_ms") or 0))
        end_ms = max(start_ms, int(segment.get("end_ms") or start_ms))
        page_number = max(1, int(segment.get("page_number") or 1))
        enable_expr = f"between(t\\,{start_ms / 1000.0:.3f}\\,{end_ms / 1000.0:.3f})"
        escaped_page = _ffmpeg_escape(f"Page {page_number}")
        filters.extend([
            f"drawbox=x={_STORYBOOK_CAPTION_BOX_X}:y={_STORYBOOK_CAPTION_BOX_Y}:w={_STORYBOOK_CAPTION_BOX_W}:h={_STORYBOOK_CAPTION_BOX_H}:color=0xF8EEDC@0.965:t=fill:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_CAPTION_BOX_X}:y={_STORYBOOK_CAPTION_BOX_Y}:w={_STORYBOOK_CAPTION_BOX_W}:h={_STORYBOOK_CAPTION_BOX_H}:color=0xC5AA79@0.26:t=2:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_CAPTION_RULE_X}:y={_STORYBOOK_CAPTION_RULE_Y}:w={_STORYBOOK_CAPTION_BOX_W - 120}:h=1:color=0xC5AA79@0.30:t=fill:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_PAGE_CHIP_X}:y={_STORYBOOK_PAGE_CHIP_Y}:w={_STORYBOOK_PAGE_CHIP_W}:h={_STORYBOOK_PAGE_CHIP_H}:color=0xEBDDC6@0.985:t=fill:enable='{enable_expr}'",
            f"drawbox=x={_STORYBOOK_PAGE_CHIP_X}:y={_STORYBOOK_PAGE_CHIP_Y}:w={_STORYBOOK_PAGE_CHIP_W}:h={_STORYBOOK_PAGE_CHIP_H}:color=0xC5AA79@0.32:t=2:enable='{enable_expr}'",
            f"drawtext=text='{escaped_page}':fontcolor=0x8A6233:fontsize=15:x={_STORYBOOK_PAGE_CHIP_X + 14}:y={_STORYBOOK_PAGE_CHIP_Y + 6}:{font_option}:enable='{enable_expr}'",
        ])
    return filters


def _wrap_storybook_ass_words(words: list[str], *, width: int = 46, max_lines: int = 2) -> list[list[str]]:
    lines: list[list[str]] = []
    current_line: list[str] = []
    current_width = 0
    for word in words:
        word_width = len(word)
        next_width = current_width + (1 if current_line else 0) + word_width
        if current_line and next_width > width and len(lines) < max_lines - 1:
            lines.append(current_line)
            current_line = [word]
            current_width = word_width
            continue
        current_line.append(word)
        current_width = next_width
    if current_line:
        lines.append(current_line)
    return lines[:max_lines]


def _ass_escape_text(text: str) -> str:
    return (
        str(text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def _format_storybook_ass_timestamp(milliseconds: int) -> str:
    total_cs = max(0, int(round(milliseconds / 10)))
    hours, rem = divmod(total_cs, 360000)
    minutes, rem = divmod(rem, 6000)
    seconds, centiseconds = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _build_storybook_readalong_ass_text(
    segments: list[dict[str, Any]],
) -> str:
    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {_STORYBOOK_W}",
            f"PlayResY: {_STORYBOOK_H}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: ReadAlong,Fredoka,27,&H0042182A,&H0042182A,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,1,112,112,56,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]
    )
    dialogue_lines: list[str] = []
    highlight_prefix = r"{\1c&H00321C0F&\3c&H00C7E8FF&\bord2\shad0\b1}"
    highlight_reset = r"{\rReadAlong}"
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        words = _tokenize_storybook_readalong_words(text)
        word_starts_ms = [max(0, int(value)) for value in list(segment.get("word_starts_ms") or [])]
        segment_start_ms = max(0, int(segment.get("start_ms") or 0))
        segment_end_ms = max(segment_start_ms, int(segment.get("end_ms") or segment_start_ms))
        audio_duration_ms = max(0, int(segment.get("audio_duration_ms") or 0))
        if not words or len(word_starts_ms) != len(words):
            continue
        wrapped_lines = _wrap_storybook_ass_words(words)
        if not wrapped_lines:
            continue
        wrapped_word_indices: list[list[int]] = []
        next_word_index = 0
        for line_words in wrapped_lines:
            line_indices = list(range(next_word_index, next_word_index + len(line_words)))
            wrapped_word_indices.append(line_indices)
            next_word_index += len(line_words)

        def render_text(current_word_index: int | None) -> str:
            rendered_lines: list[str] = []
            for line_words, line_indices in zip(wrapped_lines, wrapped_word_indices):
                rendered_words: list[str] = []
                for word, word_index in zip(line_words, line_indices):
                    escaped = _ass_escape_text(word)
                    if current_word_index is not None and word_index == current_word_index:
                        rendered_words.append(f"{highlight_prefix}{escaped}{highlight_reset}")
                    else:
                        rendered_words.append(escaped)
                rendered_lines.append(" ".join(rendered_words))
            return r"\N".join(rendered_lines)

        first_start_ms = word_starts_ms[0]
        if first_start_ms > 0:
            dialogue_lines.append(
                "Dialogue: 0,{start},{end},ReadAlong,,0,0,0,,{text}".format(
                    start=_format_storybook_ass_timestamp(segment_start_ms),
                    end=_format_storybook_ass_timestamp(min(segment_end_ms, segment_start_ms + first_start_ms)),
                    text=render_text(None),
                )
            )
        for word_index, start_offset_ms in enumerate(word_starts_ms):
            next_offset_ms = (
                word_starts_ms[word_index + 1]
                if word_index + 1 < len(word_starts_ms)
                else max(start_offset_ms + 120, audio_duration_ms or 0, start_offset_ms)
            )
            absolute_start_ms = segment_start_ms + start_offset_ms
            absolute_end_ms = min(segment_end_ms, segment_start_ms + next_offset_ms)
            if absolute_end_ms <= absolute_start_ms:
                continue
            dialogue_lines.append(
                "Dialogue: 0,{start},{end},ReadAlong,,0,0,0,,{text}".format(
                    start=_format_storybook_ass_timestamp(absolute_start_ms),
                    end=_format_storybook_ass_timestamp(absolute_end_ms),
                    text=render_text(word_index),
                )
            )
        settled_end_ms = segment_start_ms + max(audio_duration_ms, word_starts_ms[-1])
        if settled_end_ms < segment_end_ms:
            dialogue_lines.append(
                "Dialogue: 0,{start},{end},ReadAlong,,0,0,0,,{text}".format(
                    start=_format_storybook_ass_timestamp(settled_end_ms),
                    end=_format_storybook_ass_timestamp(segment_end_ms),
                    text=render_text(None),
                )
            )
    return f"{header}\n" + "\n".join(dialogue_lines) + "\n"


def _build_storybook_movie_readalong_segments(
    scene_durations: list[float],
    narration_lines: list[str],
    narration_audio_durations: list[float],
    *,
    narration_word_starts_ms: list[list[int]] | None = None,
    narration_timing_modes: list[str | None] | None = None,
    story_pages: list[dict[str, Any]] | None = None,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    cover_offset: int = 0,
    end_card_index: int | None = None,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    pages = list(story_pages or [])
    word_starts_by_line = list(narration_word_starts_ms or [])
    timing_modes = list(narration_timing_modes or [])
    cursor_s = 0.0
    for scene_index, duration_s in enumerate(scene_durations):
        scene_start_ms = max(0, int(round(cursor_s * 1000)))
        scene_end_ms = max(scene_start_ms, int(round((cursor_s + float(duration_s)) * 1000)))
        cursor_s += float(duration_s)
        if scene_index < cover_offset or (end_card_index is not None and scene_index == end_card_index):
            continue
        page_index = scene_index - cover_offset
        audio_index = page_index + cover_offset
        spoken_text = str(narration_lines[audio_index] or "").strip() if 0 <= audio_index < len(narration_lines) else ""
        fallback_text = ""
        if 0 <= page_index < len(pages):
            fallback_text = str(pages[page_index].get("storybeat_text", "") or "").strip()
        text = spoken_text or choose_readalong_text("", fallback_text, child_age, storybook_movie_pacing)
        if not text:
            continue
        audio_duration_s = (
            float(narration_audio_durations[audio_index])
            if 0 <= audio_index < len(narration_audio_durations)
            else 0.0
        )
        words = _tokenize_storybook_readalong_words(text)
        if not words:
            continue
        word_starts_ms: list[int] = []
        timing_mode = "estimated"
        if 0 <= audio_index < len(word_starts_by_line):
            candidate = [max(0, int(value)) for value in list(word_starts_by_line[audio_index] or [])]
            if len(candidate) == len(words):
                word_starts_ms = candidate
                timing_mode = str(timing_modes[audio_index] or "exact")
        if not word_starts_ms:
            word_starts_ms = _approximate_storybook_word_starts_ms(text, audio_duration_s or duration_s)
            timing_mode = "estimated" if word_starts_ms else "none"
        segments.append(
            {
                "page_number": page_index + 1,
                "text": text,
                "start_ms": scene_start_ms,
                "end_ms": scene_end_ms,
                "audio_duration_ms": max(0, int(round((audio_duration_s or duration_s) * 1000))),
                "word_starts_ms": word_starts_ms,
                "timing_mode": timing_mode,
            }
        )
    return segments


_STORYBOOK_W = 1280
_STORYBOOK_H = 720
_STORYBOOK_FPS = 30
_STORYBOOK_SCENE_PICTURE_W = 960
_STORYBOOK_SCENE_PICTURE_H = 540
_STORYBOOK_SCENE_PICTURE_X = (_STORYBOOK_W - _STORYBOOK_SCENE_PICTURE_W) // 2
_STORYBOOK_SCENE_PICTURE_Y = 12
_STORYBOOK_CAPTION_BOX_X = 52
_STORYBOOK_CAPTION_BOX_Y = 566
_STORYBOOK_CAPTION_BOX_W = _STORYBOOK_W - (_STORYBOOK_CAPTION_BOX_X * 2)
_STORYBOOK_CAPTION_BOX_H = 118
_STORYBOOK_CAPTION_RULE_X = 112
_STORYBOOK_CAPTION_RULE_Y = 620
_STORYBOOK_CAPTION_TEXT_X = 112
_STORYBOOK_CAPTION_TEXT_Y = 632
_STORYBOOK_PAGE_CHIP_X = 78
_STORYBOOK_PAGE_CHIP_Y = 580
_STORYBOOK_PAGE_CHIP_W = 104
_STORYBOOK_PAGE_CHIP_H = 30


def _storybook_x264_args() -> list[str]:
    return [
        "-r",
        str(_STORYBOOK_FPS),
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
    ]


def _storybook_motion_profile(
    scene_index: int,
    shot_plan: StoryboardShotPlan | None = None,
) -> tuple[float, float, float, float, float, float]:
    if shot_plan is not None:
        return motion_profile(
            scene_index,
            shot_type=shot_plan.shot_type,
            profile_index=shot_plan.profile_index,
        )
    return motion_profile(scene_index)


def _storybook_motion_vf(
    scene_index: int,
    duration: float,
    shot_plan: StoryboardShotPlan | None = None,
    *,
    output_w: int = _STORYBOOK_W,
    output_h: int = _STORYBOOK_H,
    terminal: bool = True,
) -> str:
    sx, ex, sy, ey, zoom_start, zoom_end = _storybook_motion_profile(scene_index, shot_plan)
    duration = max(PAGE_SECONDS_DEFAULT, float(duration))
    settle_seconds, travel_seconds, motion_factor = motion_timing(duration)
    ex = sx + ((ex - sx) * motion_factor * 0.88)
    ey = sy + ((ey - sy) * motion_factor * 0.88)
    progress = f"max(0,min(1,(t-{settle_seconds:.3f})/{travel_seconds:.3f}))"
    eased = f"(0.5-0.5*cos(PI*{progress}))"
    max_zoom = max(1.045, zoom_start, zoom_end)
    scaled_w = int(((int(output_w * max_zoom) + 1) // 2) * 2)
    scaled_h = int(((int(output_h * max_zoom) + 1) // 2) * 2)
    x_expr = f"max(0,min(iw-{output_w},floor(((iw-{output_w})*({sx:.4f}+({ex - sx:.4f})*{eased}))/2)*2))"
    y_expr = f"max(0,min(ih-{output_h},floor(((ih-{output_h})*({sy:.4f}+({ey - sy:.4f})*{eased}))/2)*2))"
    vf_parts = [
        f"scale=w={scaled_w}:h={scaled_h}:force_original_aspect_ratio=increase:flags=lanczos",
        f"crop={output_w}:{output_h}:x='{x_expr}':y='{y_expr}'",
        f"scale=w={output_w}:h={output_h}:flags=lanczos",
    ]
    if terminal:
        vf_parts.append("format=yuv420p")
        vf_parts.append("setsar=1")
    return ",".join(vf_parts)


def _storybook_scene_canvas_vf(
    scene_index: int,
    duration: float,
    shot_plan: StoryboardShotPlan | None = None,
) -> str:
    background_vf = _storybook_motion_vf(
        scene_index,
        duration,
        shot_plan,
        output_w=_STORYBOOK_W,
        output_h=_STORYBOOK_H,
        terminal=False,
    )
    foreground_vf = _storybook_motion_vf(
        scene_index,
        duration,
        shot_plan,
        output_w=_STORYBOOK_SCENE_PICTURE_W,
        output_h=_STORYBOOK_SCENE_PICTURE_H,
        terminal=False,
    )
    return (
        "[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]{background_vf},boxblur=18:8,eq=brightness=-0.06:saturation=0.84[bg];"
        f"[fgsrc]{foreground_vf},drawbox=x=0:y=0:w=iw:h=ih:color=0xF7EED9@0.18:t=8,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=0x5C4424@0.28:t=2[fg];"
        f"[bg][fg]overlay=x={_STORYBOOK_SCENE_PICTURE_X}:y={_STORYBOOK_SCENE_PICTURE_Y},"
        "format=yuv420p,setsar=1"
    )


def _storybook_video_scene_canvas_vf() -> str:
    return (
        "[0:v]split=2[bgsrc][fgsrc];"
        "[bgsrc]scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1280:720,boxblur=18:8,eq=brightness=-0.06:saturation=0.84[bg];"
        f"[fgsrc]scale={_STORYBOOK_SCENE_PICTURE_W}:{_STORYBOOK_SCENE_PICTURE_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={_STORYBOOK_SCENE_PICTURE_W}:{_STORYBOOK_SCENE_PICTURE_H}:(ow-iw)/2:(oh-ih)/2:color=#0b1020,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=0xF7EED9@0.18:t=8,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=0x5C4424@0.28:t=2[fg];"
        f"[bg][fg]overlay=x={_STORYBOOK_SCENE_PICTURE_X}:y={_STORYBOOK_SCENE_PICTURE_Y},"
        "format=yuv420p,setsar=1"
    )


def _render_storybook_image_shot(
    image_path: Path,
    output_path: Path,
    duration: float,
    vf: str,
) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(_STORYBOOK_FPS),
            "-t",
            f"{duration:.3f}",
            "-i",
            str(image_path),
            "-vf",
            vf,
            *_storybook_x264_args(),
            str(output_path),
        ],
        check=True,
        capture_output=True,
    )


def _render_storybook_cinematic_segment(
    image_path: Path,
    output_path: Path,
    duration: float,
    scene_index: int,
    shot_plan: StoryboardShotPlan | None = None,
) -> None:
    duration = max(1.8, float(duration))
    _render_storybook_image_shot(
        image_path,
        output_path,
        duration,
        _storybook_scene_canvas_vf(scene_index, duration, shot_plan),
    )


def _render_storybook_end_card_segment(
    output_path: Path,
    *,
    duration: float,
    title_text: str,
    subtitle_text: str = "",
    background_image_path: Path | None = None,
) -> None:
    duration = max(2.2, float(duration))
    escaped_title = _ffmpeg_escape(title_text)
    escaped_subtitle = _ffmpeg_escape(subtitle_text) if subtitle_text else ""
    fade_out_start = max(0.4, duration - 0.45)
    font_option = _storybook_caption_font_option()
    vf_parts = [
        "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos",
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b1230",
        "boxblur=6:2",
        "eq=brightness=-0.08:saturation=0.78",
        "drawbox=x=150:y=150:w=980:h=410:color=0x120428@0.58:t=fill",
        "drawbox=x=150:y=150:w=980:h=410:color=0xE5C87B@0.28:t=2",
        f"drawtext=text='{escaped_title}':fontcolor=0xFFF4DA:fontsize=84:x=(w-text_w)/2:y=290:{font_option}:shadowcolor=0x150D24@0.9:shadowx=2:shadowy=2",
    ]
    if escaped_subtitle:
        vf_parts.append(
            f"drawtext=text='{escaped_subtitle}':fontcolor=0xF7E4BF:fontsize=28:x=(w-text_w)/2:y=392:{font_option}:shadowcolor=0x150D24@0.8:shadowx=2:shadowy=2"
        )
    vf_parts.append(f"fade=t=in:st=0:d=0.35,fade=t=out:st={fade_out_start:.3f}:d=0.45,setsar=1")
    vf = ",".join(vf_parts)
    if background_image_path and background_image_path.exists():
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-loop", "1",
                "-framerate", str(_STORYBOOK_FPS),
                "-t", f"{duration:.3f}",
                "-i", str(background_image_path),
                "-vf", vf,
                "-r", "30",
                "-pix_fmt", "yuv420p",
                "-c:v", "libx264",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )
        return
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"color=c=#11032c:s=1280x720:d={duration:.3f}",
            "-vf", vf,
            "-r", "30",
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264",
            str(output_path),
        ],
        check=True,
        capture_output=True,
    )


def _build_storybook_cover_filtergraph(
    *,
    title_text: str,
    subtitle_text: str = "",
    duration: float,
    logo_path: Path | None = None,
) -> str:
    title_font = 60 if len(title_text) <= 18 else 52 if len(title_text) <= 28 else 44
    escaped_title = _ffmpeg_escape(title_text or "A Storybook Adventure")
    escaped_subtitle = _ffmpeg_escape(subtitle_text) if subtitle_text else ""
    fade_out_start = max(0.2, float(duration) - 0.4)
    chains = [
        "[0:v]scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b2d5b,"
        "eq=brightness=-0.02:saturation=1.08,"
        "drawbox=x=60:y=90:w=1160:h=560:color=#000000@0.18:t=fill,"
        "drawbox=x=60:y=90:w=1160:h=560:color=#f8f1dc@0.6:t=2,"
        "drawbox=x=130:y=250:w=1020:h=200:color=#000000@0.35:t=fill,"
        f"drawtext=text='{escaped_title}':fontcolor=white:fontsize={title_font}:x=(w-text_w)/2:y=h*0.42:shadowcolor=black:shadowx=2:shadowy=2"
        + (
            f",drawtext=text='{escaped_subtitle}':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=h*0.56:shadowcolor=black:shadowx=2:shadowy=2"
            if escaped_subtitle
            else ""
        )
        + "[coverbase]"
    ]
    final_source = "coverbase"
    if logo_path is not None:
        chains.append(f"movie='{_ffmpeg_filter_path(logo_path)}',scale=230:-1[coverlogo]")
        chains.append("[coverbase][coverlogo]overlay=x=W-w-84:y=108:format=auto[coverwithlogo]")
        final_source = "coverwithlogo"
    chains.append(
        f"[{final_source}]fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out_start:.3f}:d=0.4,setsar=1[outv]"
    )
    return ";".join(chains)


def _clean_title(raw: str) -> str:
    return shared_clean_storybook_title(raw)


def _heuristic_title(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str = "",
) -> str:
    return shared_heuristic_storybook_title(scene_descriptions, story_summary, child_name)


def _generate_story_title(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str = "",
) -> str:
    if not _env_enabled("ENABLE_STORYBOOK_TITLE_LLM", default=True):
        return _heuristic_title(scene_descriptions, story_summary, child_name)
    if not PROJECT:
        return _heuristic_title(scene_descriptions, story_summary, child_name)
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Title LLM unavailable (google-genai not installed): %s", exc)
        return _heuristic_title(scene_descriptions, story_summary, child_name)

    model = os.environ.get("STORYBOOK_TITLE_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    prompt = shared_build_storybook_title_prompt(scene_descriptions, story_summary, child_name)
    try:
        client = google_genai.Client(vertexai=True, project=PROJECT, location=_vertex_ai_location())
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config=google_genai.types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=16,
            ),
        )
    except Exception as exc:
        logger.warning("Title LLM request failed: %s", exc)
        return _heuristic_title(scene_descriptions, story_summary, child_name)

    text = _extract_response_text(response)
    title = shared_validate_storybook_title(text, scene_descriptions, story_summary, child_name)
    if not title:
        return _heuristic_title(scene_descriptions, story_summary, child_name)
    return title


def _extract_image_bytes(image_response: Any) -> bytes | None:
    candidates = getattr(image_response, "candidates", None) or []
    parts: list[Any] = []
    if candidates:
        try:
            parts = list(candidates[0].content.parts)
        except Exception:
            parts = []
    else:
        parts = list(getattr(image_response, "parts", []) or [])

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data
    return None


def _generate_cover_image(scene_descriptions: list[str], story_summary: str) -> bytes | None:
    if not _env_enabled("ENABLE_STORYBOOK_COVER_IMAGE", default=False):
        return None
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Cover image generation unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_COVER_IMAGE_MODEL", DEFAULT_VERTEX_IMAGE_MODEL).strip()
    client = google_genai.Client(vertexai=True, project=PROJECT, location=_vertex_ai_location())

    focus = story_summary.strip() or (scene_descriptions[0].strip() if scene_descriptions else "")
    prompt = (
        "storybook book cover illustration, ornate frame, painterly texture, warm lighting, "
        f"centered on: {focus}. No text, no logos."
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config=google_genai.types.GenerateContentConfig(
                response_modalities=[google_genai.types.MediaModality.IMAGE],
                image_config=google_genai.types.ImageConfig(
                    aspect_ratio="16:9",
                ),
            ),
        )
        return _extract_image_bytes(response)
    except Exception as exc:
        logger.warning("Cover image generation failed: %s", exc)
        return None


def _format_srt(entries: list[tuple[float, float, str]]) -> str:
    def _ts(seconds: float) -> str:
        ms = int(max(0.0, seconds) * 1000)
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    for idx, (start, end, text) in enumerate(entries, start=1):
        if not text.strip():
            continue
        lines.append(str(idx))
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _ffprobe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _ffprobe_has_audio_stream(path: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams") or []
        return any(
            isinstance(stream, dict) and str(stream.get("codec_type") or "").strip().lower() == "audio"
            for stream in streams
        )
    except Exception as exc:
        logger.warning("Unable to inspect audio streams for %s: %s", path, exc)
        return False


def _parse_volumedetect_levels(stderr_text: str) -> tuple[float | None, float | None]:
    if not stderr_text:
        return None, None
    mean_match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr_text)
    max_match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr_text)
    mean_volume_db = float(mean_match.group(1)) if mean_match else None
    max_volume_db = float(max_match.group(1)) if max_match else None
    return mean_volume_db, max_volume_db


def _ffmpeg_audio_levels(path: Path) -> tuple[float | None, float | None]:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(path),
                "-vn",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        return _parse_volumedetect_levels((result.stderr or "") + "\n" + (result.stdout or ""))
    except Exception as exc:
        logger.warning("Unable to inspect audio levels for %s: %s", path, exc)
        return None, None


def _ffmpeg_nonsilent_seconds(
    path: Path,
    *,
    noise_db: float = -45.0,
    min_silence_duration: float = 0.3,
) -> float | None:
    total_duration = _ffprobe_duration(path)
    if total_duration <= 0.0:
        return 0.0
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(path),
                "-vn",
                "-af",
                f"silencedetect=noise={noise_db:.1f}dB:d={min_silence_duration:.3f}",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        output = (result.stderr or "") + "\n" + (result.stdout or "")
        silence_total = 0.0
        for match in re.finditer(r"silence_duration:\s*(\d+(?:\.\d+)?)", output):
            silence_total += float(match.group(1))
        return max(0.0, total_duration - silence_total)
    except Exception as exc:
        logger.warning("Unable to inspect non-silent coverage for %s: %s", path, exc)
        return None


def _recommended_audio_boost_gain_db(
    *,
    mean_volume_db: float | None,
    max_volume_db: float | None,
) -> float:
    if max_volume_db is None:
        return 0.0
    if max_volume_db >= -10.0 and (mean_volume_db is None or mean_volume_db >= -30.0):
        return 0.0
    desired_gain_db = max(0.0, -3.0 - max_volume_db)
    if mean_volume_db is not None and mean_volume_db < -34.0:
        desired_gain_db = max(desired_gain_db, min(24.0, -26.0 - mean_volume_db))
    return max(0.0, min(24.0, desired_gain_db))


def _boost_quiet_audio_track(
    audio_track: Path,
    *,
    tmp: Path,
) -> tuple[Path, dict[str, float | bool | None]]:
    mean_volume_db, max_volume_db = _ffmpeg_audio_levels(audio_track)
    gain_db = _recommended_audio_boost_gain_db(
        mean_volume_db=mean_volume_db,
        max_volume_db=max_volume_db,
    )
    info: dict[str, float | bool | None] = {
        "applied": False,
        "before_mean_volume_db": mean_volume_db,
        "before_max_volume_db": max_volume_db,
        "gain_db": gain_db,
        "after_mean_volume_db": mean_volume_db,
        "after_max_volume_db": max_volume_db,
    }
    if gain_db < 0.5:
        return audio_track, info

    boosted_track = tmp / "audio_mix_boosted.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(audio_track),
            "-filter:a",
            f"volume={gain_db:.1f}dB,alimiter=limit=0.98",
            "-c:a",
            "pcm_s16le",
            str(boosted_track),
        ],
        check=True,
        capture_output=True,
    )
    after_mean_volume_db, after_max_volume_db = _ffmpeg_audio_levels(boosted_track)
    info.update(
        {
            "applied": True,
            "after_mean_volume_db": after_mean_volume_db,
            "after_max_volume_db": after_max_volume_db,
        }
    )
    return boosted_track, info


def _master_storybook_narration_track(
    narration_track: Path,
    *,
    tmp: Path,
    voice_filter: str,
    master_chain: str,
    filename: str,
) -> Path:
    mastered_track = tmp / filename
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(narration_track),
            "-filter:a",
            f"{voice_filter}{master_chain}",
            "-c:a",
            "pcm_s16le",
            str(mastered_track),
        ],
        check=True,
        capture_output=True,
    )
    return mastered_track


def _build_storybook_padded_audio_track(
    *,
    tmp: Path,
    audio_paths: list[Path | None],
    durations: list[float],
    basename: str,
    fade_in: float = 0.0,
    fade_out: float = 0.0,
) -> Path | None:
    if not durations:
        return None
    safe_basename = re.sub(r"[^A-Za-z0-9_.-]+", "_", basename or "audio")
    padded_paths: list[Path] = []
    for idx, duration in enumerate(durations):
        src = audio_paths[idx] if idx < len(audio_paths) else None
        padded_path = tmp / f"{safe_basename}_pad_{idx:03d}.wav"
        if src and src.exists():
            filters: list[str] = []
            if fade_in > 0:
                filters.append(f"afade=t=in:st=0:d={fade_in}")
            if fade_out > 0:
                fade_start = max(0.0, duration - fade_out)
                filters.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out}")
            filters.append(f"apad=pad_dur={duration:.3f},atrim=0:{duration:.3f}")
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(src),
                    "-filter_complex", ",".join(filters),
                    "-ar", "44100",
                    "-ac", "2",
                    str(padded_path),
                ],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "lavfi",
                    "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-t", f"{duration:.3f}",
                    str(padded_path),
                ],
                check=True,
                capture_output=True,
            )
        padded_paths.append(padded_path)
    audio_concat_list = tmp / f"{safe_basename}_concat.txt"
    audio_concat_list.write_text(
        "\n".join(f"file '{p.name}'" for p in padded_paths)
    )
    audio_full = tmp / f"{safe_basename}.wav"
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(audio_concat_list),
            "-c:a", "pcm_s16le",
            str(audio_full),
        ],
        check=True,
        capture_output=True,
    )
    return audio_full


def _mux_story_video_with_audio(
    base_video: Path,
    audio_track: Path,
    output_path: Path,
    *,
    reencode_video: bool = False,
) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(base_video),
        "-i",
        str(audio_track),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
    ]
    if reencode_video:
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                os.environ.get("STORYBOOK_MUX_FALLBACK_PRESET", "veryfast").strip() or "veryfast",
                "-crf",
                os.environ.get("STORYBOOK_MUX_FALLBACK_CRF", "20").strip() or "20",
                "-pix_fmt",
                "yuv420p",
            ]
        )
    else:
        command.extend(["-c:v", "copy"])
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    subprocess.run(command, check=True, capture_output=True)


def _build_public_story_video_url(session_id: str, cache_token: str) -> str:
    base_url = f"https://storage.googleapis.com/{GCS_FINAL_BUCKET}/{session_id}/story_final.mp4"
    token = str(cache_token or "").strip()
    if not token:
        return base_url
    return f"{base_url}?v={token}"


def _slow_storybook_tts_audio(
    audio_bytes: bytes | None,
    *,
    child_age: int | str | None,
    storybook_movie_pacing: str | None,
) -> bytes | None:
    if not audio_bytes:
        return audio_bytes
    tempo = storybook_tts_tempo_factor(child_age, storybook_movie_pacing)
    if abs(tempo - 1.0) < 0.01:
        return audio_bytes
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_suffix = ".wav" if audio_bytes.startswith(b"RIFF") else ".mp3"
        input_path = tmp / f"tts_input{input_suffix}"
        output_path = tmp / "tts_output.mp3"
        input_path.write_bytes(audio_bytes)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(input_path),
                    "-filter:a",
                    f"atempo={tempo:.3f}",
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "3",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
            )
            return output_path.read_bytes()
        except Exception as exc:
            logger.warning("Storybook TTS tempo adjustment failed: %s", exc)
            return audio_bytes


def _wrap_pcm_as_wav(pcm_bytes: bytes, *, sample_rate_hz: int = 24000) -> bytes:
    if not pcm_bytes or pcm_bytes.startswith(b"RIFF"):
        return pcm_bytes
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()


def _convert_audio_bytes_to_mp3(audio_bytes: bytes | None, *, source_suffix: str = ".wav") -> bytes | None:
    if not audio_bytes:
        return None
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        input_path = tmp / f"tts_input{source_suffix}"
        output_path = tmp / "tts_output.mp3"
        input_path.write_bytes(audio_bytes)
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(input_path),
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "3",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
            )
            return output_path.read_bytes()
        except Exception as exc:
            logger.warning("Storybook TTS audio conversion failed: %s", exc)
            return audio_bytes


def _extract_inline_audio_data(response: Any) -> tuple[bytes | None, str | None]:
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if not inline:
                continue
            data = getattr(inline, "data", None)
            if not data:
                continue
            mime_type = getattr(inline, "mime_type", None)
            return bytes(data), str(mime_type or "").strip() or None
    return None, None


def _storybook_gemini_tts_model() -> str:
    return os.environ.get("STORYBOOK_GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts").strip()


def _storybook_gemini_tts_voice() -> str:
    return os.environ.get("STORYBOOK_GEMINI_TTS_VOICE", "Kore").strip()


def _build_gemini_tts_prompt(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
) -> str:
    age_band = child_age_band(child_age)
    pacing = normalize_storybook_movie_pacing(storybook_movie_pacing)
    pacing_note = {
        "read_to_me": "Read it clearly and a little more narratively, with gentle warmth.",
        "fast_movie": "Read it clearly with a touch more momentum, but stay calm and child-friendly.",
    }.get(pacing, "Read it clearly, warmly, and at an easy read-along pace for a child.")
    return (
        f"Read this one-sentence storybook page aloud for a child age band {age_band}. "
        f"{pacing_note} Keep the delivery expressive, cozy, and easy to follow. "
        "Do not add extra words. Read exactly this line: "
        f"{text}"
    )


def _synthesize_tts_gemini_only(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
) -> bytes | None:
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not text or not api_key:
        return None
    try:
        from google import genai as google_genai

        client = google_genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=_storybook_gemini_tts_model(),
            contents=_build_gemini_tts_prompt(
                text,
                child_age=child_age,
                storybook_movie_pacing=storybook_movie_pacing,
            ),
            config=google_genai.types.GenerateContentConfig(
                response_modalities=[google_genai.types.Modality.AUDIO],
                speech_config=google_genai.types.SpeechConfig(
                    voice_config=google_genai.types.VoiceConfig(
                        prebuilt_voice_config=google_genai.types.PrebuiltVoiceConfig(
                            voice_name=_storybook_gemini_tts_voice(),
                        )
                    )
                ),
            ),
        )
        audio_bytes, mime_type = _extract_inline_audio_data(response)
        if not audio_bytes:
            return None
        mime_lower = str(mime_type or "").lower()
        if "l16" in mime_lower or "pcm" in mime_lower:
            audio_bytes = _wrap_pcm_as_wav(audio_bytes)
            audio_bytes = _convert_audio_bytes_to_mp3(audio_bytes, source_suffix=".wav") or audio_bytes
        elif audio_bytes.startswith(b"RIFF"):
            audio_bytes = _convert_audio_bytes_to_mp3(audio_bytes, source_suffix=".wav") or audio_bytes
        return _slow_storybook_tts_audio(
            audio_bytes,
            child_age=child_age,
            storybook_movie_pacing=storybook_movie_pacing,
        )
    except Exception as exc:
        logger.warning("Gemini TTS request failed: %s", exc)
        return None


def _synthesize_tts_elevenlabs_audio_only(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    voice_id: str | None = None,
) -> bytes | None:
    global _ELEVENLABS_TTS_DISABLED_REASON
    if _ELEVENLABS_TTS_DISABLED_REASON:
        return None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    selected_voice_id = _normalize_storybook_elevenlabs_voice_id(voice_id) or _storybook_default_elevenlabs_voice_id()
    fallback_voice_id = _storybook_fallback_elevenlabs_voice_id(voice_id)
    if not api_key or not selected_voice_id or not text:
        if api_key and not voice_id:
            logger.warning(
                "ELEVENLABS_VOICE_ID is not set — ElevenLabs TTS disabled. "
                "Set ELEVENLABS_VOICE_ID in env (find IDs at elevenlabs.io/voice-library)."
            )
        return None
    model_id = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2").strip()
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
        },
    }
    candidate_voice_ids = [selected_voice_id]
    if fallback_voice_id:
        candidate_voice_ids.append(fallback_voice_id)
    try:
        with httpx.Client(timeout=30.0) as client:
            for idx, request_voice_id in enumerate(candidate_voice_ids):
                endpoint = (
                    os.environ.get("ELEVENLABS_TTS_ENDPOINT", "").strip()
                    or f"https://api.elevenlabs.io/v1/text-to-speech/{request_voice_id}"
                )
                resp = client.post(endpoint, headers=headers, json=payload)
                content_type = resp.headers.get("content-type", "")
                if resp.status_code < 300 and content_type.startswith("audio"):
                    return _slow_storybook_tts_audio(
                        resp.content,
                        child_age=child_age,
                        storybook_movie_pacing=storybook_movie_pacing,
                    )
                if resp.status_code in {401, 402, 403}:
                    _ELEVENLABS_TTS_DISABLED_REASON = f"http_{resp.status_code}"
                    logger.warning(
                        "Disabling ElevenLabs TTS for this run after HTTP %s: %s",
                        resp.status_code,
                        (resp.text or "")[:240],
                    )
                    return None
                try:
                    payload_preview = resp.json()
                except Exception:
                    payload_preview = resp.text
                if idx == 0 and fallback_voice_id and _elevenlabs_voice_not_found_error(payload_preview):
                    logger.warning(
                        "Selected ElevenLabs narrator %s was not found; retrying with fallback narrator %s.",
                        request_voice_id,
                        fallback_voice_id,
                    )
                    continue
                logger.warning(
                    "ElevenLabs TTS returned no audio (status=%s, content-type=%s) for voice=%s model=%s. Body preview: %s",
                    resp.status_code,
                    content_type or "unknown",
                    request_voice_id,
                    model_id,
                    str(payload_preview)[:240],
                )
                break
    except Exception as exc:
        logger.warning("ElevenLabs TTS request failed: %s", exc)
        return None
    return None


def _synthesize_tts_elevenlabs_with_timing(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    voice_id: str | None = None,
) -> tuple[bytes | None, list[tuple[str, float, float]] | None]:
    global _ELEVENLABS_TTS_DISABLED_REASON
    if _ELEVENLABS_TTS_DISABLED_REASON:
        return None, None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    selected_voice_id = _normalize_storybook_elevenlabs_voice_id(voice_id) or _storybook_default_elevenlabs_voice_id()
    fallback_voice_id = _storybook_fallback_elevenlabs_voice_id(voice_id)
    if not api_key or not selected_voice_id or not text:
        return None, None
    model_id = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2").strip()
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
        },
    }
    candidate_voice_ids = [selected_voice_id]
    if fallback_voice_id:
        candidate_voice_ids.append(fallback_voice_id)
    final_voice_id = selected_voice_id
    try:
        with httpx.Client(timeout=30.0) as client:
            for idx, request_voice_id in enumerate(candidate_voice_ids):
                endpoint = (
                    os.environ.get("ELEVENLABS_TTS_TIMESTAMPS_ENDPOINT", "").strip()
                    or f"https://api.elevenlabs.io/v1/text-to-speech/{request_voice_id}/with-timestamps"
                )
                resp = client.post(endpoint, headers=headers, json=payload)
                content_type = resp.headers.get("content-type", "")
                if resp.status_code < 300 and "json" in content_type:
                    body = resp.json()
                    audio_base64 = str(body.get("audio_base64") or body.get("audioBase64") or "").strip()
                    if audio_base64:
                        audio_bytes = base64.b64decode(audio_base64)
                        audio_bytes = _slow_storybook_tts_audio(
                            audio_bytes,
                            child_age=child_age,
                            storybook_movie_pacing=storybook_movie_pacing,
                        )
                        word_timings = _extract_elevenlabs_word_timings_seconds(
                            body,
                            child_age=child_age,
                            storybook_movie_pacing=storybook_movie_pacing,
                        )
                        return audio_bytes, (word_timings or None)
                if resp.status_code in {401, 402, 403}:
                    _ELEVENLABS_TTS_DISABLED_REASON = f"http_{resp.status_code}"
                    logger.warning(
                        "Disabling ElevenLabs TTS for this run after HTTP %s: %s",
                        resp.status_code,
                        (resp.text or "")[:240],
                    )
                    return None, None
                try:
                    payload_preview = resp.json()
                except Exception:
                    payload_preview = resp.text
                if idx == 0 and fallback_voice_id and _elevenlabs_voice_not_found_error(payload_preview):
                    logger.warning(
                        "Selected ElevenLabs timestamp narrator %s was not found; retrying with fallback narrator %s.",
                        request_voice_id,
                        fallback_voice_id,
                    )
                    final_voice_id = fallback_voice_id
                    continue
                logger.warning(
                    "ElevenLabs timestamp TTS returned no audio (status=%s, content-type=%s) for voice=%s model=%s. Body preview: %s",
                    resp.status_code,
                    content_type or "unknown",
                    request_voice_id,
                    model_id,
                    str(payload_preview)[:240],
                )
                final_voice_id = request_voice_id
                break
    except Exception as exc:
        logger.warning("ElevenLabs timestamp TTS request failed: %s", exc)

    return _synthesize_tts_elevenlabs_audio_only(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
        voice_id=final_voice_id,
    ), None


def _synthesize_tts_elevenlabs(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    voice_id: str | None = None,
) -> bytes | None:
    audio_bytes, _word_timings = _synthesize_tts_elevenlabs_with_timing(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
        voice_id=voice_id,
    )
    return audio_bytes


def _synthesize_tts_google_only(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
) -> bytes | None:
    if not text:
        return None
    try:
        from google.cloud import texttospeech
    except Exception:
        return None
    try:
        client = texttospeech.TextToSpeechClient()
        language_code = os.environ.get("STORYBOOK_TTS_LANG", "en-US")
        voice_name = os.environ.get("STORYBOOK_TTS_VOICE", "en-US-Neural2-F")
        try:
            speaking_rate = float(os.environ.get("STORYBOOK_TTS_RATE", "0.9"))
        except Exception:
            speaking_rate = 0.9
        speaking_rate = storybook_tts_speaking_rate(speaking_rate, child_age, storybook_movie_pacing)
        try:
            pitch = float(os.environ.get("STORYBOOK_TTS_PITCH", "0.0"))
        except Exception:
            pitch = 0.0
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=language_code,
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=max(0.7, min(speaking_rate, 1.2)),
            pitch=max(-4.0, min(pitch, 4.0)),
        )
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
        )
        return response.audio_content
    except Exception as exc:
        logger.warning("Google TTS request failed: %s", exc)
        return None


def _synthesize_tts_google(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    voice_id: str | None = None,
) -> bytes | None:
    if not text:
        return None
    audio = _synthesize_tts_elevenlabs(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
        voice_id=voice_id,
    )
    if audio:
        return audio
    audio = _synthesize_tts_gemini_only(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
    )
    if audio:
        return audio
    return _synthesize_tts_google_only(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
    )


def _synthesize_tts_with_provider(
    text: str,
    *,
    child_age: int | str | None = None,
    storybook_movie_pacing: str | None = None,
    voice_id: str | None = None,
    allow_non_elevenlabs_fallback: bool = True,
) -> tuple[str | None, bytes | None, list[tuple[str, float, float]] | None]:
    if not text:
        return None, None, None
    audio, word_timings = _synthesize_tts_elevenlabs_with_timing(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
        voice_id=voice_id,
    )
    if audio:
        return "elevenlabs", audio, word_timings
    if not allow_non_elevenlabs_fallback:
        return None, None, None
    audio = _synthesize_tts_gemini_only(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
    )
    if audio:
        return "gemini", audio, None
    audio = _synthesize_tts_google_only(
        text,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
    )
    if audio:
        return "google", audio, None
    return None, None, None


async def _elevenlabs_generate_music(prompt: str, duration_seconds: float) -> bytes | None:
    global _ELEVENLABS_AUDIO_DISABLED_REASON
    if _ELEVENLABS_AUDIO_DISABLED_REASON:
        return None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(2.0, min(float(duration_seconds), 30.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    music_endpoint = (
        os.environ.get("ELEVENLABS_MUSIC_ENDPOINT", "").strip()
        or "https://api.elevenlabs.io/v1/music"
    )
    minimum_duration_seconds = _clamp_float(
        os.environ.get("ELEVENLABS_MUSIC_MIN_SECONDS", "8"),
        8.0,
        4.0,
        15.0,
    )
    payload = {
        "prompt": prompt,
        "music_length_ms": int(max(duration_seconds, minimum_duration_seconds) * 1000),
        "force_instrumental": True,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(music_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
            if resp.status_code in {401, 402, 403, 429}:
                _ELEVENLABS_AUDIO_DISABLED_REASON = f"music_http_{resp.status_code}"
                logger.warning(
                    "Disabling ElevenLabs music/SFX for this run after music HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:240],
                )
    except Exception:
        return None
    return None


async def _elevenlabs_generate_sfx(prompt: str, duration_seconds: float) -> bytes | None:
    global _ELEVENLABS_AUDIO_DISABLED_REASON
    if _ELEVENLABS_AUDIO_DISABLED_REASON:
        return None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(1.0, min(float(duration_seconds), 6.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    sound_endpoint = (
        os.environ.get("ELEVENLABS_SOUND_ENDPOINT", "").strip()
        or "https://api.elevenlabs.io/v1/sound-generation"
    )
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
            if resp.status_code in {401, 402, 403, 429}:
                _ELEVENLABS_AUDIO_DISABLED_REASON = f"sfx_http_{resp.status_code}"
                logger.warning(
                    "Disabling ElevenLabs music/SFX for this run after HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:240],
                )
    except Exception:
        return None
    return None


async def _lyria_generate_music(prompt: str, seed: int | None = None) -> tuple[bytes, str] | None:
    return await asyncio.to_thread(_lyria_generate_music_sync, prompt, seed)


async def _generate_storybook_music_bytes(
    prompt: str,
    duration_seconds: float,
    seed: int | None = None,
) -> tuple[bytes, str] | None:
    provider = _storybook_music_provider()
    attempts = ["elevenlabs", "lyria"] if provider == "auto" else [provider]
    for idx, name in enumerate(attempts):
        if name == "off":
            return None
        if name == "lyria":
            result = await _lyria_generate_music(prompt, seed=seed)
        elif name == "elevenlabs":
            audio = await _elevenlabs_generate_music(prompt, duration_seconds)
            result = (audio, ".mp3") if audio else None
        else:
            result = None
        if result and result[0]:
            if idx > 0:
                logger.info("Storybook music fallback succeeded with %s", name)
            return result
    return None


async def _generate_storybook_sfx_bytes(prompt: str, duration_seconds: float) -> tuple[bytes, str] | None:
    provider = _storybook_sfx_provider()
    attempts = ["elevenlabs"] if provider == "auto" else [provider]
    for name in attempts:
        if name == "off":
            return None
        if name == "elevenlabs":
            audio = await _elevenlabs_generate_sfx(prompt, duration_seconds)
            result = (audio, ".mp3") if audio else None
        else:
            result = None
        if result and result[0]:
            return result
    return None


def _score_scene(description: str) -> int:
    text = (description or "").lower()
    if not text:
        return 0
    keywords = [
        "magic", "magical", "sparkle", "sparkly", "glow", "glowing", "twinkle",
        "shimmer", "glitter", "rainbow", "portal", "wand", "spell", "fairy",
        "unicorn", "dragon", "castle", "moon", "star", "stars", "floating",
        "flying", "cloud", "crystal", "aurora", "light", "wonder",
    ]
    score = 0
    for kw in keywords:
        if kw in text:
            score += 1
    return score


def _choose_veo_indices(
    total: int,
    desired: int,
    descriptions: list[str],
    min_score: int,
) -> list[int]:
    if total <= 0 or desired <= 0:
        return []
    desired = max(0, min(desired, 2))
    if desired <= 0:
        return []
    scored: list[tuple[int, int]] = []
    for idx in range(total):
        desc = descriptions[idx] if idx < len(descriptions) else ""
        score = _score_scene(desc)
        if score >= min_score:
            scored.append((idx, score))
    if not scored:
        return []
    # Prefer higher score, then later scenes (feels more climactic).
    scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
    picked: list[int] = []
    for idx, _score in scored:
        if idx not in picked:
            picked.append(idx)
        if len(picked) >= desired:
            break
    return picked


async def _download_any(
    session: httpx.AsyncClient,
    gcs: storage.Client,
    url: str,
    dest: Path,
) -> Path:
    if url.startswith("data:"):
        header, data = url.split(",", 1)
        payload = base64.b64decode(data) if ";base64" in header else data.encode("utf-8")
        dest.write_bytes(payload)
        return dest
    if url.startswith("gs://"):
        path = url[5:]
        bucket_name, blob_path = path.split("/", 1)
        blob = gcs.bucket(bucket_name).blob(blob_path)
        dest.write_bytes(blob.download_as_bytes())
        return dest
    return await download_blob(session, url, dest)


def _scene_sources_from_state_doc(data: dict[str, Any]) -> list[str]:
    story_pages = story_pages_from_state_data(data)
    if story_pages:
        raw_scene_urls = list(data.get("scene_asset_urls", []) or [])
        raw_scene_gcs_uris = list(data.get("scene_asset_gcs_uris", []) or [])
        scene_sources: list[str] = []
        for idx, page in enumerate(story_pages):
            gcs_uri = str(page.get("gcs_uri", "") or "").strip()
            image_url = str(page.get("image_url", "") or "").strip()
            if _is_placeholder_scene_source(gcs_uri):
                gcs_uri = ""
            if _is_placeholder_scene_source(image_url):
                image_url = ""
            if not gcs_uri and idx < len(raw_scene_gcs_uris):
                gcs_uri = str(raw_scene_gcs_uris[idx] or "").strip()
            if not image_url and idx < len(raw_scene_urls):
                image_url = str(raw_scene_urls[idx] or "").strip()
            chosen = gcs_uri or image_url
            if chosen and not _is_placeholder_scene_source(chosen):
                scene_sources.append(chosen)
        if scene_sources:
            return scene_sources

    raw_scene_urls = data.get("scene_asset_urls", [])
    raw_scene_gcs_uris = data.get("scene_asset_gcs_uris", [])
    scene_urls = list(raw_scene_urls) if isinstance(raw_scene_urls, list) else []
    scene_gcs_uris = list(raw_scene_gcs_uris) if isinstance(raw_scene_gcs_uris, list) else []
    scene_sources: list[str] = []
    max_scenes = max(len(scene_urls), len(scene_gcs_uris))
    for idx in range(max_scenes):
        gcs_uri = str(scene_gcs_uris[idx]).strip() if idx < len(scene_gcs_uris) and scene_gcs_uris[idx] else ""
        url = str(scene_urls[idx]).strip() if idx < len(scene_urls) and scene_urls[idx] else ""
        if url.startswith("data:"):
            url = ""
        chosen = gcs_uri or url
        if chosen and not _is_placeholder_scene_source(chosen):
            scene_sources.append(chosen)
    return scene_sources


def _story_page_fallback_data_url(text: str) -> str:
    caption = str(text or "A magical story page").strip()[:180] or "A magical story page"
    try:
        from PIL import Image, ImageDraw, ImageFont

        width, height = 1280, 720
        image = Image.new("RGB", (width, height), "#1a0d40")
        draw = ImageDraw.Draw(image, "RGBA")

        top_color = (26, 13, 64)
        mid_color = (60, 31, 114)
        bottom_color = (16, 59, 98)
        for y in range(height):
            progress = y / max(1, height - 1)
            if progress < 0.55:
                blend = progress / 0.55
                color = tuple(
                    int(top_color[idx] + (mid_color[idx] - top_color[idx]) * blend)
                    for idx in range(3)
                )
            else:
                blend = (progress - 0.55) / 0.45
                color = tuple(
                    int(mid_color[idx] + (bottom_color[idx] - mid_color[idx]) * blend)
                    for idx in range(3)
                )
            draw.line((0, y, width, y), fill=color, width=1)

        draw.ellipse((120, 430, 470, 780), fill=(255, 127, 190, 92))
        draw.ellipse((430, 380, 930, 880), fill=(104, 247, 207, 72))
        draw.ellipse((860, 420, 1180, 740), fill=(111, 200, 255, 84))
        draw.ellipse((440, 80, 840, 470), fill=(255, 209, 102, 44))
        draw.rounded_rectangle(
            (180, 230, 1100, 470),
            radius=42,
            fill=(17, 8, 44, 148),
            outline=(255, 247, 214, 70),
            width=4,
        )

        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 42)
        except Exception:
            font = ImageFont.load_default()

        wrapped_lines: list[str] = []
        current_line = ""
        for word in caption.split():
            candidate = f"{current_line} {word}".strip()
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if current_line and (bbox[2] - bbox[0]) > 820:
                wrapped_lines.append(current_line)
                current_line = word
            else:
                current_line = candidate
        if current_line:
            wrapped_lines.append(current_line)
        wrapped_lines = wrapped_lines[:3] or [caption]

        line_gap = 18
        line_heights: list[int] = []
        for line in wrapped_lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_heights.append(max(1, bbox[3] - bbox[1]))
        total_height = sum(line_heights) + line_gap * max(0, len(line_heights) - 1)
        current_y = 350 - total_height // 2
        for line, line_height in zip(wrapped_lines, line_heights):
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            x = (width - line_width) // 2
            draw.text((x + 2, current_y + 2), line, font=font, fill=(17, 8, 44, 180))
            draw.text((x, current_y), line, font=font, fill=(255, 247, 214, 245))
            current_y += line_height + line_gap

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        png_bytes = output.getvalue()
    except Exception:
        width, height = 1280, 720

        def _png_chunk(tag: bytes, payload: bytes) -> bytes:
            encoded = tag + payload
            return (
                len(payload).to_bytes(4, "big")
                + encoded
                + (zlib.crc32(encoded) & 0xFFFFFFFF).to_bytes(4, "big")
            )

        top_color = (26, 13, 64)
        mid_color = (60, 31, 114)
        bottom_color = (16, 59, 98)
        scanlines = bytearray()
        for y in range(height):
            progress = y / max(1, height - 1)
            if progress < 0.55:
                blend = progress / 0.55
                color = tuple(
                    int(top_color[idx] + (mid_color[idx] - top_color[idx]) * blend)
                    for idx in range(3)
                )
            else:
                blend = (progress - 0.55) / 0.45
                color = tuple(
                    int(mid_color[idx] + (bottom_color[idx] - mid_color[idx]) * blend)
                    for idx in range(3)
                )
            scanlines.append(0)
            scanlines.extend(bytes(color) * width)

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        png_bytes = b"".join(
            [
                b"\x89PNG\r\n\x1a\n",
                _png_chunk(b"IHDR", ihdr),
                _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9)),
                _png_chunk(b"IEND", b""),
            ]
        )

    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _backfill_missing_still_paths_with_story_cards(
    still_paths: list[Path],
    *,
    expected_scene_pages: int,
    scene_descriptions: list[str],
    story_summary: str,
    tmp: Path,
) -> tuple[list[Path], int]:
    if expected_scene_pages <= len(still_paths):
        return list(still_paths), 0

    completed_paths = list(still_paths)
    created = 0
    for idx in range(len(completed_paths), expected_scene_pages):
        fallback_text = _scene_text_for_index(scene_descriptions, idx, story_summary)
        fallback_url = _story_page_fallback_data_url(fallback_text)
        _, data = fallback_url.split(",", 1)
        fallback_path = tmp / f"scene_{idx:03d}_fallback.png"
        fallback_path.write_bytes(base64.b64decode(data))
        completed_paths.append(fallback_path)
        created += 1
    return completed_paths, created


def _merge_scene_sources_with_recovered_stills(
    scene_source_urls: list[str],
    recovered_scene_sources: list[str],
    *,
    expected_scene_pages: int,
) -> tuple[list[str], int]:
    if not recovered_scene_sources:
        return list(scene_source_urls), 0

    target_len = max(expected_scene_pages, len(scene_source_urls))
    merged_sources = list(scene_source_urls[:target_len])
    if len(merged_sources) < target_len:
        merged_sources.extend([""] * (target_len - len(merged_sources)))

    replacements = 0
    recovered_index = 0
    for idx in range(target_len):
        current = str(merged_sources[idx] or "").strip()
        current_is_real = bool(current) and not current.startswith("data:")
        if current_is_real:
            if recovered_index < len(recovered_scene_sources):
                recovered_index += 1
            continue
        if recovered_index >= len(recovered_scene_sources):
            break
        merged_sources[idx] = recovered_scene_sources[recovered_index]
        recovered_index += 1
        replacements += 1

    merged_sources = [str(source or "").strip() for source in merged_sources if str(source or "").strip()]
    return merged_sources, replacements


def _resolve_story_page_sources_for_assembly(
    data: dict[str, Any],
    scene_descriptions: list[str],
    story_summary: str,
) -> tuple[list[str], list[str], int]:
    story_pages = story_pages_from_state_data(data)
    if not story_pages:
        return _scene_sources_from_state_doc(data), list(scene_descriptions), 0

    raw_scene_urls = list(data.get("scene_asset_urls", []) or [])
    raw_scene_gcs_uris = list(data.get("scene_asset_gcs_uris", []) or [])
    resolved_page_sources: list[str] = []
    resolved_page_descriptions: list[str] = []
    fallback_page_count = 0

    for idx, page in enumerate(story_pages):
        page_description = (
            str(page.get("scene_description", "") or "").strip()
            or str(page.get("storybeat_text", "") or "").strip()
            or _scene_text_for_index(scene_descriptions, idx, story_summary)
        )
        page_gcs_uri = str(page.get("gcs_uri", "") or "").strip()
        page_image_url = str(page.get("image_url", "") or "").strip()
        if _is_placeholder_scene_source(page_gcs_uri):
            page_gcs_uri = ""
        if _is_placeholder_scene_source(page_image_url):
            page_image_url = ""
        source = page_gcs_uri or page_image_url
        if not source:
            array_gcs = str(raw_scene_gcs_uris[idx]).strip() if idx < len(raw_scene_gcs_uris) and raw_scene_gcs_uris[idx] else ""
            array_url = str(raw_scene_urls[idx]).strip() if idx < len(raw_scene_urls) and raw_scene_urls[idx] else ""
            if array_url.startswith("data:"):
                source = array_url
            else:
                source = array_gcs or array_url
            if _is_placeholder_scene_source(source):
                source = ""
        if not source:
            source = _story_page_fallback_data_url(page_description)
            fallback_page_count += 1
        if source:
            resolved_page_sources.append(source)
            resolved_page_descriptions.append(page_description)

    return resolved_page_sources, resolved_page_descriptions, fallback_page_count


def _discover_scene_stills_from_bucket(gcs: storage.Client, session_id: str) -> list[str]:
    if not GCS_ASSETS_BUCKET or not session_id:
        return []
    prefix = f"{session_id}/scene_stills/"
    blobs = list(gcs.bucket(GCS_ASSETS_BUCKET).list_blobs(prefix=prefix))
    still_paths: list[str] = []
    for blob in sorted(blobs, key=lambda candidate: candidate.name):
        name = str(getattr(blob, "name", "") or "")
        if not name or name.endswith("/"):
            continue
        lower_name = name.lower()
        if not lower_name.endswith((".png", ".jpg", ".jpeg", ".webp")):
            continue
        still_paths.append(f"gs://{GCS_ASSETS_BUCKET}/{name}")
    return still_paths


async def _mark_assembly_failed(
    doc_ref: firestore.AsyncDocumentReference,
    session_id: str,
    reason: str,
) -> None:
    clean_reason = str(reason or "Unknown assembly failure.").strip()
    logger.error("%s", clean_reason)
    try:
        await doc_ref.update(
            {
                "assembly_status": "failed",
                "assembly_error": clean_reason[:500],
                "theater_release_ready": False,
            }
        )
    except Exception:
        pass
    raise RuntimeError(clean_reason)


async def _load_storyteller_session_document(
    db: firestore.AsyncClient,
    session_id: str,
) -> tuple[firestore.AsyncDocumentReference, dict[str, Any]]:
    doc_ref = db.collection("storyteller_sessions").document(session_id)
    doc = await doc_ref.get()
    if not doc.exists:
        raise RuntimeError(f"Session {session_id} not found in Firestore.")
    return doc_ref, dict(doc.to_dict() or {})


def _build_video_assembly_initial_state(session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    story_pages = story_pages_from_state_data(data)
    scene_sources = _scene_sources_from_state_doc(data)
    generated_asset_urls = list(data.get("generated_asset_urls", []) or [])
    child_age = data.get("child_age")
    scene_count = max(len(story_pages), len(scene_sources), len(generated_asset_urls))
    return {
        "video_assembly_session_id": session_id,
        "story_title": str(data.get("story_title") or data.get("title") or "").strip() or "Untitled Storybook",
        "child_name": str(data.get("child_name") or "").strip() or "friend",
        "child_age": child_age or 4,
        "child_age_band": child_age_band(child_age),
        "story_summary": str(data.get("story_summary") or "").strip() or "No summary available.",
        "scene_count": scene_count,
        "story_tone": _normalize_story_tone(data.get("story_tone")),
        "storybook_movie_pacing": normalize_storybook_movie_pacing(data.get("storybook_movie_pacing")),
        "storybook_elevenlabs_voice_id": _normalize_storybook_elevenlabs_voice_id(data.get("storybook_elevenlabs_voice_id")),
    }


def _build_video_assembly_issue_entries(
    data: dict[str, Any],
    *,
    error_text: str = "",
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    def _add(code: str, detail: str) -> None:
        clean_code = str(code or "").strip().lower()
        clean_detail = " ".join(str(detail or "").split()).strip()[:320]
        if not clean_code or clean_code in seen_codes:
            return
        seen_codes.add(clean_code)
        issues.append({"code": clean_code, "detail": clean_detail or clean_code})

    if error_text:
        _add("assembly_failed", error_text)
    combined_error_text = " ".join(
        [
            str(error_text or ""),
            str(data.get("assembly_error") or ""),
        ]
    ).lower()
    if "no scene assets found" in combined_error_text or "dropped pages" in combined_error_text:
        _add("scene_assets_missing", combined_error_text)

    final_video_url = str(data.get("final_video_url") or "").strip()
    expected_audio = bool(data.get("audio_expected"))
    audio_available = bool(data.get("audio_available"))
    final_has_audio_stream = bool(data.get("final_has_audio_stream"))
    theater_ready = bool(data.get("theater_release_ready"))
    try:
        expected_narration_count = int(data.get("expected_narration_count", 0) or 0)
    except Exception:
        expected_narration_count = 0
    try:
        rendered_narration_count = int(data.get("rendered_narration_count", 0) or 0)
    except Exception:
        rendered_narration_count = 0

    if not final_video_url:
        _add("final_video_missing", "Final video URL is missing.")
    if expected_audio and not audio_available:
        _add("narration_missing", "Narration was expected but was not available in the assembled movie.")
    if expected_audio and not final_has_audio_stream:
        _add("audio_stream_missing", "Final MP4 is missing an audio stream.")
    if expected_narration_count > rendered_narration_count:
        _add(
            "narration_incomplete",
            f"Rendered narration count {rendered_narration_count} is below expected {expected_narration_count}.",
        )
    if not theater_ready:
        _add(
            "release_gate_failed",
            str(data.get("assembly_error") or "The movie did not pass the release gate."),
        )

    return issues


def _build_video_assembly_render_report(
    session_id: str,
    *,
    data: dict[str, Any],
    runtime_overrides: dict[str, Any] | None = None,
    error_text: str = "",
) -> dict[str, Any]:
    runtime_overrides = dict(runtime_overrides or {})
    final_video_url = str(data.get("final_video_url") or "").strip()
    expected_audio = bool(data.get("audio_expected"))
    audio_available = bool(data.get("audio_available"))
    final_has_audio_stream = bool(data.get("final_has_audio_stream"))
    theater_ready = bool(data.get("theater_release_ready"))
    status = str(data.get("assembly_status") or "").strip() or ("failed" if error_text else "unknown")
    try:
        final_video_duration_sec = float(data.get("final_video_duration_sec") or 0.0)
    except Exception:
        final_video_duration_sec = 0.0
    try:
        expected_narration_count = int(data.get("expected_narration_count", 0) or 0)
    except Exception:
        expected_narration_count = 0
    try:
        rendered_narration_count = int(data.get("rendered_narration_count", 0) or 0)
    except Exception:
        rendered_narration_count = 0

    issues = _build_video_assembly_issue_entries(data, error_text=error_text)
    issue_codes = {issue["code"] for issue in issues}
    retryable_codes = {
        "assembly_failed",
        "final_video_missing",
        "narration_missing",
        "audio_stream_missing",
        "narration_incomplete",
        "release_gate_failed",
    }
    ready_to_publish = bool(
        status == "complete"
        and final_video_url
        and theater_ready
        and (not expected_audio or (audio_available and final_has_audio_stream))
    )
    reason = " ".join(
        str(
            data.get("video_assembly_reason")
            or data.get("assembly_error")
            or error_text
            or "Final storybook movie uploaded."
        ).split()
    ).strip()[:320]
    return {
        "session_id": session_id,
        "status": status,
        "ready_to_publish": ready_to_publish,
        "retryable": bool(issue_codes.intersection(retryable_codes)),
        "reason": reason,
        "issues": issues,
        "runtime_overrides": runtime_overrides,
        "final_video_url": final_video_url,
        "audio_expected": expected_audio,
        "audio_available": audio_available,
        "final_has_audio_stream": final_has_audio_stream,
        "theater_release_ready": theater_ready,
        "final_video_duration_sec": final_video_duration_sec,
        "expected_narration_count": expected_narration_count,
        "rendered_narration_count": rendered_narration_count,
    }


def _call_veo_api(
    project: str,
    location: str,
    image_bytes: bytes,
    description: str,
    negative_prompt: str,
) -> str:
    """Starts a Veo long-running operation and returns the operation name."""
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    endpoint = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/"
        "publishers/google/models/veo-3.1-generate-001:predictLongRunning"
    )
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "instances": [
            {
                "prompt": description,
                "negativePrompt": negative_prompt,
                "image": {
                    "bytesBase64Encoded": base64.b64encode(image_bytes).decode(),
                },
            }
        ],
        "parameters": {
            "aspectRatio": "16:9",
            "durationSeconds": 5,
            "sampleCount": 1,
        },
    }

    resp = httpx.post(endpoint, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    operation_name = resp.json().get("name")
    if not operation_name:
        raise RuntimeError("Veo call did not return an operation name.")
    return operation_name


def _poll_veo_operation(project: str, location: str, operation_name: str, timeout_seconds: float = 160.0) -> str:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    op_path = operation_name
    if operation_name.startswith("projects/"):
        op_path = operation_name
    elif operation_name.startswith("operations/"):
        op_path = f"projects/{project}/locations/{location}/{operation_name}"

    endpoint = f"https://{location}-aiplatform.googleapis.com/v1/{op_path}"
    headers = {"Authorization": f"Bearer {creds.token}"}

    deadline = time.monotonic() + timeout_seconds
    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() < deadline:
            resp = client.get(endpoint, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("done"):
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                for key in ("uri", "gcsUri", "videoUri", "video_url", "url"):
                    candidate = payload.get("response", {}).get(key) if isinstance(payload.get("response"), dict) else None
                    if candidate:
                        return candidate
                raise RuntimeError("Veo operation completed without a media URL.")
            time.sleep(2.0)

    raise TimeoutError("Veo operation timed out.")


async def _assemble_pipeline(
    session_id: str,
    *,
    runtime_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_overrides = dict(runtime_overrides or {})
    db = firestore.AsyncClient(project=PROJECT, database=FIRESTORE_DATABASE)
    gcs = storage.Client(project=PROJECT)

    # Pull session data from Firestore
    doc_ref, data = await _load_storyteller_session_document(db, session_id)
    video_urls: list[str] = data.get("generated_asset_urls", [])
    scene_descriptions: list[str] = list(data.get("scene_descriptions", []) or [])
    audio_urls: list[str] = data.get("elevenlabs_audio_chunks", [])
    story_summary = str(data.get("story_summary", "")).strip()
    child_name = str(data.get("child_name", "")).strip()
    child_age = data.get("child_age")
    story_tone = _normalize_story_tone(data.get("story_tone"))
    storybook_movie_pacing = normalize_storybook_movie_pacing(data.get("storybook_movie_pacing"))
    storybook_elevenlabs_voice_id = _normalize_storybook_elevenlabs_voice_id(data.get("storybook_elevenlabs_voice_id"))
    child_delight_anchors_text = str(data.get("child_delight_anchors_text", "") or "").strip()
    continuity_world_state_text = str(data.get("continuity_world_state_text", "") or "").strip()
    raw_title = str(
        data.get("story_title")
        or data.get("title")
        or os.environ.get("STORYBOOK_TITLE", "")
    ).strip()
    title = shared_validate_storybook_title(raw_title, scene_descriptions, story_summary, child_name)
    if not title:
        title = _generate_story_title(scene_descriptions, story_summary, child_name)

    story_pages = story_pages_from_state_data(data)
    narration_source_scene_count = max(len(story_pages), len(scene_descriptions), len(video_urls))
    narration_source_texts = _build_page_narration_source_texts(
        story_pages,
        scene_descriptions,
        story_summary,
        narration_source_scene_count,
    )
    fast_storybook_assembly = _fast_storybook_assembly_enabled()
    fallback_page_count = 0
    if story_pages:
        scene_source_urls, resolved_page_descriptions, fallback_page_count = _resolve_story_page_sources_for_assembly(
            data,
            scene_descriptions,
            story_summary,
        )
        if resolved_page_descriptions:
            scene_descriptions = resolved_page_descriptions
        if fallback_page_count:
            logger.warning(
                "Storybook assembly recovered %d missing page image(s) for %s with generated fallback story cards.",
                fallback_page_count,
                session_id,
            )
            try:
                await doc_ref.set(
                    {
                        "assembly_asset_recovery": "page_fallback_cards",
                        "assembly_missing_page_count": fallback_page_count,
                    },
                    merge=True,
                )
            except Exception:
                pass
    else:
        scene_source_urls = _scene_sources_from_state_doc(data)
    scene_source_urls = [
        source
        for source in scene_source_urls
        if str(source or "").strip() and not _is_placeholder_scene_source(str(source))
    ]
    expected_scene_pages = max(len(story_pages), len(video_urls), len(scene_source_urls))
    if expected_scene_pages and (
        len(scene_source_urls) < expected_scene_pages
        or any(str(source or "").strip().startswith("data:") for source in scene_source_urls)
    ):
        recovered_scene_sources = await asyncio.to_thread(_discover_scene_stills_from_bucket, gcs, session_id)
        recovered_scene_sources = [
            source
            for source in recovered_scene_sources
            if str(source or "").strip() and not _is_placeholder_scene_source(str(source))
        ]
        merged_scene_sources, recovered_replacements = _merge_scene_sources_with_recovered_stills(
            scene_source_urls,
            recovered_scene_sources,
            expected_scene_pages=expected_scene_pages,
        )
        if recovered_replacements > 0 or len(merged_scene_sources) > len(scene_source_urls):
            scene_source_urls = merged_scene_sources
            logger.warning(
                "Recovered %d real scene stills for %s directly from gs://%s/%s/scene_stills/ because Firestore page assets were incomplete.",
                len(recovered_scene_sources),
                session_id,
                GCS_ASSETS_BUCKET,
                session_id,
            )
            try:
                await doc_ref.set(
                    {
                        "scene_asset_gcs_uris": scene_source_urls,
                        "assembly_asset_recovery": "scene_stills_prefix",
                    },
                    merge=True,
                )
            except Exception:
                pass

    if not scene_source_urls and not video_urls:
        if story_pages:
            logger.warning(
                "No persisted scene assets were available for %s; the worker will assemble from generated fallback story cards.",
                session_id,
            )
        else:
            await _mark_assembly_failed(
                doc_ref,
                session_id,
                (
                    f"No scene assets found for session {session_id}. "
                    "Firestore scene arrays were empty and no files were found under the scene_stills bucket prefix."
                ),
            )

    storyboard_review_report: dict[str, Any] = {
        "status": "skipped",
        "passes": [],
        "repairs_applied": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Download all assets concurrently
        async with httpx.AsyncClient() as http:
            audio_tasks = [
                download_blob(http, url, tmp / f"audio_{i:03d}.mp3")
                for i, url in enumerate(audio_urls)
            ]
            downloaded_audios = await asyncio.gather(*audio_tasks, return_exceptions=True)

            # Download stills (if present)
            still_paths: list[Path] = []
            if scene_source_urls:
                still_tasks = []
                for i, url in enumerate(scene_source_urls):
                    still_tasks.append(_download_any(http, gcs, url, tmp / f"scene_{i:03d}.img"))
                still_paths = await asyncio.gather(*still_tasks)

            # If we already have video clips (legacy), download them as fallback.
            downloaded_videos: list[Path] = []
            if video_urls:
                video_tasks = [
                    download_blob(http, url, tmp / f"clip_{i:03d}.mp4")
                    for i, url in enumerate(video_urls)
                ]
                downloaded_videos = await asyncio.gather(*video_tasks)

        expected_scene_pages = max(len(story_pages), len(scene_source_urls), len(downloaded_videos))
        late_fallback_count = 0
        if not downloaded_videos and expected_scene_pages and len(still_paths) < expected_scene_pages:
            still_paths, late_fallback_count = _backfill_missing_still_paths_with_story_cards(
                still_paths,
                expected_scene_pages=expected_scene_pages,
                scene_descriptions=scene_descriptions,
                story_summary=story_summary,
                tmp=tmp,
            )
            if late_fallback_count:
                logger.warning(
                    "Storybook assembly backfilled %d missing downloaded page image(s) for %s with local fallback story cards.",
                    late_fallback_count,
                    session_id,
                )
                try:
                    await doc_ref.set(
                        {
                            "assembly_asset_recovery": "local_page_fallback_cards",
                            "assembly_missing_page_count": late_fallback_count,
                        },
                        merge=True,
                    )
                except Exception:
                    pass

        scene_count = len(still_paths) if still_paths else len(downloaded_videos)
        try:
            max_music_cues = _clamp_int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"), 2, 0, 4)
        except Exception:
            max_music_cues = 2
        try:
            max_sfx_cues = clamp_sfx_max(os.environ.get("STORYBOOK_SFX_MAX", "2"))
        except Exception:
            max_sfx_cues = clamp_sfx_max(None)
        default_narration_lines = (
            _build_narration_segments(
                narration_source_texts or scene_descriptions,
                story_summary,
                scene_count,
                child_age=child_age,
            )
            if scene_count
            else []
        )
        studio_plan_task: asyncio.Task[dict[str, Any]] | None = None
        if scene_count and _storybook_studio_enabled():
            logger.info("Starting storybook studio planning in parallel for %s (%s scenes).", session_id, scene_count)
            studio_plan_task = asyncio.create_task(
                _run_storybook_studio_async(
                    session_id=session_id,
                    title=title,
                    child_name=child_name,
                    child_age=child_age,
                    story_summary=story_summary,
                    scene_descriptions=scene_descriptions,
                    story_pages=story_pages,
                    narration_source_texts=narration_source_texts,
                    child_delight_anchors_text=child_delight_anchors_text,
                    continuity_world_state_text=continuity_world_state_text,
                    scene_count=scene_count,
                    max_music_cues=max_music_cues,
                    max_sfx_cues=max_sfx_cues,
                    fallback_narration_lines=default_narration_lines,
                )
            )

        try:
            await doc_ref.update({"assembly_status": "reviewing_storyboard"})
        except Exception:
            pass

        if still_paths and _env_enabled("ENABLE_STORYBOOK_DIRECTOR_WORKFLOW", default=True) and not fast_storybook_assembly:
            async def _director_review_callback(_workflow_state: dict[str, Any], iteration: int) -> dict[str, Any]:
                forward_review, backward_review = await asyncio.gather(
                    asyncio.to_thread(
                        _review_storyboard_pass,
                        scene_descriptions,
                        story_summary,
                        child_name,
                        still_paths,
                        "FORWARD",
                        story_tone,
                    ),
                    asyncio.to_thread(
                        _review_storyboard_pass,
                        scene_descriptions,
                        story_summary,
                        child_name,
                        still_paths,
                        "BACKWARD",
                        story_tone,
                    ),
                )
                merged = _merge_storyboard_reviews(
                    [("FORWARD", forward_review), ("BACKWARD", backward_review)],
                    total_scenes=len(still_paths),
                )
                actionable_fixes = [
                    fix
                    for fix in (merged.get("scene_fixes", []) or [])
                    if isinstance(fix, dict) and bool(fix.get("actionable"))
                ]
                warning_fixes = [
                    fix
                    for fix in (merged.get("scene_fixes", []) or [])
                    if isinstance(fix, dict) and not bool(fix.get("actionable"))
                ]
                review_status = (
                    "passed_with_warnings"
                    if warning_fixes and not actionable_fixes
                    else "passed"
                    if not actionable_fixes
                    else "fix_required"
                )
                return {
                    "pass": iteration,
                    "status": review_status,
                    "fix_count": len(actionable_fixes),
                    "warning_count": len(warning_fixes),
                    "global_feedback": list(merged.get("global_feedback", []) or []),
                    "scene_fixes": list(merged.get("scene_fixes", []) or []),
                }

            async def _director_repair_callback(
                review: dict[str, Any],
                decision: dict[str, Any],
                iteration: int,
            ) -> dict[str, Any]:
                selected_indices: list[int] = []
                for raw in decision.get("selected_scene_indices", []) or []:
                    try:
                        idx = int(raw)
                    except Exception:
                        continue
                    if idx > 0 and idx not in selected_indices:
                        selected_indices.append(idx)
                if not selected_indices:
                    for fix in (review.get("scene_fixes", []) or []):
                        if not isinstance(fix, dict) or not bool(fix.get("actionable")):
                            continue
                        try:
                            selected_indices.append(int(fix.get("scene_index", 0) or 0))
                        except Exception:
                            continue

                try:
                    repair_limit = int(decision.get("selected_repair_limit", 2) or 2)
                except Exception:
                    repair_limit = 2
                repair_limit = max(1, min(repair_limit, 4))

                repairs_applied: list[dict[str, Any]] = []
                selected_set = {idx for idx in selected_indices if idx > 0}
                actionable_fixes = [
                    fix
                    for fix in (review.get("scene_fixes", []) or [])
                    if isinstance(fix, dict) and bool(fix.get("actionable"))
                ]

                for fix in actionable_fixes:
                    try:
                        scene_index = int(fix.get("scene_index", 0) or 0)
                    except Exception:
                        scene_index = 0
                    if scene_index <= 0:
                        continue
                    if selected_set and scene_index not in selected_set:
                        continue
                    idx = scene_index - 1
                    if idx < 0 or idx >= len(still_paths):
                        continue

                    current_image_bytes: bytes | None
                    previous_image_bytes: bytes | None = None
                    hero_anchor_image_bytes: bytes | None = None
                    try:
                        current_image_bytes = still_paths[idx].read_bytes()
                    except Exception:
                        current_image_bytes = None
                    if idx > 0:
                        try:
                            previous_image_bytes = still_paths[idx - 1].read_bytes()
                        except Exception:
                            previous_image_bytes = None
                    hero_anchor_description = _scene_text_for_index(scene_descriptions, 0, story_summary)
                    if idx != 0 and still_paths:
                        try:
                            hero_anchor_image_bytes = still_paths[0].read_bytes()
                        except Exception:
                            hero_anchor_image_bytes = None

                    current_desc = _scene_text_for_index(scene_descriptions, idx, story_summary)
                    previous_desc = _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else ""
                    next_desc = _scene_text_for_index(scene_descriptions, idx + 1, story_summary) if idx + 1 < len(still_paths) else ""

                    repaired_bytes = await asyncio.to_thread(
                        _generate_repaired_story_still,
                        current_desc,
                        str(fix.get("repair_prompt") or fix.get("issue") or "").strip(),
                        story_summary,
                        previous_desc,
                        next_desc,
                        current_image_bytes,
                        previous_image_bytes,
                        hero_anchor_description,
                        hero_anchor_image_bytes,
                        story_tone,
                    )
                    if not repaired_bytes:
                        continue

                    repaired_path = tmp / f"scene_{idx:03d}_repair_pass_{iteration}.png"
                    repaired_path.write_bytes(repaired_bytes)
                    still_paths[idx] = repaired_path
                    repairs_applied.append(
                        {
                            "pass": iteration,
                            "scene_index": scene_index,
                            "severity": str(fix.get("severity") or "major"),
                            "issue": str(fix.get("issue") or "")[:240],
                            "directions": list(fix.get("directions", []) or []),
                        }
                    )
                    if len(repairs_applied) >= repair_limit:
                        break

                result_status = "passed_with_repairs" if repairs_applied else _unresolved_storyboard_status(actionable_fixes)
                return {
                    "status": result_status,
                    "repairs_applied": len(repairs_applied),
                    "repairs": repairs_applied,
                }

            workflow_state = await run_storybook_director_workflow(
                session_id=session_id,
                initial_state={
                    "story_title": title,
                    "child_name": child_name or "friend",
                    "child_age": child_age or 4,
                    "child_age_band": child_age_band(child_age),
                    "story_tone": story_tone,
                    "story_summary": story_summary or "No summary available.",
                    "scene_descriptions_json": json.dumps(scene_descriptions, ensure_ascii=True),
                },
                review_callback=_director_review_callback,
                repair_callback=_director_repair_callback,
                max_revision_passes=_clamp_int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_PASSES", "2"), 2, 1, 3),
                max_repairs_per_pass=_clamp_int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_FIXES", "3"), 3, 1, 4),
            )
            storyboard_review_report = build_storyboard_report_from_workflow_state(workflow_state)
        elif still_paths:
            still_paths, storyboard_review_report = await _review_and_refine_storyboard(
                session_id=session_id,
                scene_descriptions=scene_descriptions,
                story_summary=story_summary,
                child_name=child_name,
                still_paths=still_paths,
                tmp=tmp,
                story_tone=story_tone,
            )
        else:
            storyboard_review_report = {
                "status": "skipped",
                "reason": "no still paths present",
                "passes": [],
                "repairs_applied": [],
            }

        studio_plan: dict[str, Any] = {
            "status": "not_run",
            "narration_lines": list(default_narration_lines),
            "music_cues": {},
            "sfx_cues": {},
            "mix_guidance": {},
        }
        if studio_plan_task is not None:
            try:
                studio_plan = await studio_plan_task
            except Exception as exc:
                logger.warning("Storybook studio workflow failed for %s: %s", session_id, exc, exc_info=True)
        studio_summary = build_storybook_studio_summary(studio_plan)
        studio_audio_locked = str(studio_plan.get("status") or "").strip().lower() not in {"", "not_run", "disabled"}

        try:
            await doc_ref.update(
                {
                    "assembly_status": "assembling",
                    "storyboard_review": storyboard_review_report,
                    "storybook_studio": studio_summary,
                }
            )
        except Exception:
            pass

        valid_audios = [a for a in downloaded_audios if isinstance(a, Path) and a.exists()]
        if still_paths and expected_scene_pages and len(still_paths) < expected_scene_pages:
            await _mark_assembly_failed(
                doc_ref,
                session_id,
                (
                    f"Storybook assembly only recovered {len(still_paths)} page images for {expected_scene_pages} story pages. "
                    "The movie would have dropped pages, so the worker aborted."
                ),
            )
        enable_tts = _env_enabled("ENABLE_STORYBOOK_TTS", default=True)
        enable_captions = _storybook_burned_captions_enabled()
        force_tts = _env_enabled("FORCE_STORYBOOK_TTS", default=True)
        enable_music = _env_enabled("ENABLE_STORYBOOK_MUSIC", default=True)
        enable_sfx = _env_enabled("ENABLE_STORYBOOK_SFX", default=True)
        if bool(runtime_overrides.get("force_tts")):
            enable_tts = True
            force_tts = True
        if bool(runtime_overrides.get("disable_music")):
            enable_music = False
        if bool(runtime_overrides.get("disable_sfx")):
            enable_sfx = False
        if bool(runtime_overrides.get("force_captions")):
            enable_captions = True
        if studio_audio_locked:
            enable_music = enable_music and bool(studio_plan.get("music_enabled", True))
        try:
            music_volume = clamp_music_volume(os.environ.get("STORYBOOK_MUSIC_VOLUME"))
        except Exception:
            music_volume = clamp_music_volume(None)
        try:
            sfx_volume = clamp_sfx_volume(os.environ.get("STORYBOOK_SFX_VOLUME"))
        except Exception:
            sfx_volume = clamp_sfx_volume(None)
        try:
            narration_volume = clamp_narration_volume(os.environ.get("STORYBOOK_NARRATION_VOLUME"))
        except Exception:
            narration_volume = clamp_narration_volume(None)
        enable_ducking = _env_enabled("ENABLE_STORYBOOK_DUCKING", default=True)
        narration_volume, music_volume, sfx_volume, enable_ducking = _apply_storybook_mix_guidance(
            studio_plan.get("mix_guidance"),
            narration_volume=narration_volume,
            music_volume=music_volume,
            sfx_volume=sfx_volume,
            enable_ducking=enable_ducking,
        )
        try:
            sfx_max = clamp_sfx_max(os.environ.get("STORYBOOK_SFX_MAX", "2"))
        except Exception:
            sfx_max = clamp_sfx_max(None)
        try:
            sfx_min_score = int(os.environ.get("STORYBOOK_SFX_MIN_SCORE", "2"))
        except Exception:
            sfx_min_score = 2
        try:
            sfx_cooldown = clamp_sfx_cooldown_pages(os.environ.get("STORYBOOK_SFX_COOLDOWN", "1"))
        except Exception:
            sfx_cooldown = clamp_sfx_cooldown_pages(None)
        use_existing_audio = bool(valid_audios) and not force_tts
        cover_enabled = _env_enabled("ENABLE_STORYBOOK_COVER", default=False)
        cover_seconds = _clamp_float(os.environ.get("STORYBOOK_COVER_SECONDS", "3.5"), 3.5, 2.0, 6.0)
        cover_narration = os.environ.get("STORYBOOK_COVER_NARRATION", "").strip()
        if cover_enabled and not cover_narration:
            if title and child_name:
                cover_narration = f"Our story is called {title}, by {child_name}."
            elif title:
                cover_narration = f"Our story is called {title}."
            elif child_name:
                cover_narration = f"This story is by {child_name}."
            else:
                cover_narration = "Our story is about to begin."
        cover_description = (
            f"Cover: storybook title card for '{title}'."
            if title
            else "Cover: storybook title card."
        )
        cover_author = f"by {child_name}" if child_name else ""
        end_card_enabled = _env_enabled("ENABLE_STORYBOOK_END_CARD", default=True)
        end_card_seconds = _clamp_float(os.environ.get("STORYBOOK_END_CARD_SECONDS", "2.8"), 2.8, 2.0, 5.0)
        end_card_title = os.environ.get("STORYBOOK_END_CARD_TITLE", "The End").strip() or "The End"
        end_card_narration = (
            os.environ.get(
                "STORYBOOK_END_CARD_NARRATION",
                "And that is the happy end of our story.",
            ).strip()
            or "And that is the happy end of our story."
        )
        cover_image_bytes = (
            _generate_cover_image(scene_descriptions, story_summary)
            if cover_enabled and not fast_storybook_assembly
            else None
        )

        narration_lines = list(studio_plan.get("narration_lines", []) or default_narration_lines)
        studio_plan_status = str(studio_plan.get("status") or "").strip().lower()
        studio_plan_trusted = bool(studio_plan.get("narration_lines")) and studio_plan_status == "approved"
        if cover_enabled:
            narration_lines = [cover_narration or ""] + narration_lines
        if end_card_enabled:
            narration_lines = narration_lines + [end_card_narration]
        normalized_narration_lines: list[str] = []
        scene_max_words = narration_max_words_for_age(child_age)
        cover_max_words = narration_max_words_for_age(child_age, cover=True)
        for idx, line in enumerate(narration_lines):
            if cover_enabled and idx == 0:
                normalized_narration_lines.append(
                    _simplify_for_readalong(
                        str(line or "") or cover_narration or "Our story is about to begin.",
                        max_words=cover_max_words,
                    )
                )
                continue
            if end_card_enabled and idx == len(narration_lines) - 1:
                normalized_narration_lines.append(
                    _simplify_for_readalong(
                        str(line or "") or end_card_narration,
                        max_words=cover_max_words,
                    )
                )
                continue
            scene_idx = idx - 1 if cover_enabled else idx
            fallback_description = (
                (narration_source_texts or scene_descriptions)[scene_idx]
                if 0 <= scene_idx < len(narration_source_texts or scene_descriptions)
                else ""
            )
            previous_scene_description = (
                (narration_source_texts or scene_descriptions)[scene_idx - 1]
                if scene_idx > 0 and scene_idx - 1 < len(narration_source_texts or scene_descriptions)
                else ""
            )
            next_scene_description = (
                (narration_source_texts or scene_descriptions)[scene_idx + 1]
                if scene_idx >= 0 and scene_idx + 1 < len(narration_source_texts or scene_descriptions)
                else ""
            )
            previous_line = normalized_narration_lines[-1] if normalized_narration_lines else ""
            next_line = (
                str(narration_lines[idx + 1] or "").strip()
                if idx + 1 < len(narration_lines)
                else ""
            )
            llm_line = shared_normalize_storybeat_text(str(line or ""), max_chars=220)
            llm_line_needs_revision = bool(llm_line) and story_sentence_needs_revision(llm_line)
            llm_line_has_unsupported_detail = bool(llm_line) and _narration_mentions_unsupported_detail(
                llm_line,
                source_line=str(line or ""),
                scene_description=fallback_description,
                previous_scene_description=previous_scene_description,
                next_scene_description=next_scene_description,
                previous_line=previous_line,
                next_line=next_line,
            )
            if llm_line_has_unsupported_detail:
                llm_line_needs_revision = True
            if (
                llm_line
                and len(llm_line.split()) <= scene_max_words
                and story_sentence_quality_score(llm_line) >= 2
                and not llm_line_needs_revision
                ):
                normalized_narration_lines.append(llm_line)
                continue
            if llm_line and (
                studio_plan_trusted
                or story_sentence_quality_score(llm_line) >= 2
                or llm_line_needs_revision
            ):
                rewritten_line = _llm_rewrite_storybook_narration_line(
                    llm_line,
                    scene_description=fallback_description,
                    story_summary=story_summary,
                    child_age=child_age,
                    max_words=scene_max_words,
                    previous_scene_description=previous_scene_description,
                    next_scene_description=next_scene_description,
                    previous_line=previous_line,
                    next_line=next_line,
                )
                if rewritten_line:
                    normalized_narration_lines.append(rewritten_line)
                    continue
            fallback_line = _normalize_storybook_narration_line(
                str(line or ""),
                fallback=fallback_description,
                max_words=scene_max_words,
                scene_index=max(0, scene_idx),
            )
            if fallback_line and _narration_mentions_unsupported_detail(
                fallback_line,
                source_line=fallback_description or str(line or ""),
                scene_description=fallback_description,
                previous_scene_description=previous_scene_description,
                next_scene_description=next_scene_description,
                previous_line=previous_line,
                next_line=next_line,
            ):
                fallback_line = ""
            if fallback_line and not story_sentence_needs_revision(fallback_line):
                normalized_narration_lines.append(fallback_line)
                continue
            fallback_rewrite = None
            if studio_plan_trusted or not fallback_line:
                fallback_rewrite = _llm_rewrite_storybook_narration_line(
                    fallback_description or str(line or "") or fallback_line,
                    scene_description=fallback_description,
                    story_summary=story_summary,
                    child_age=child_age,
                    max_words=scene_max_words,
                    previous_scene_description=previous_scene_description,
                    next_scene_description=next_scene_description,
                    previous_line=previous_line,
                    next_line=next_line,
                )
            elif not studio_plan_trusted:
                fallback_rewrite = _llm_rewrite_storybook_narration_line(
                    fallback_description or str(line or "") or fallback_line,
                    scene_description=fallback_description,
                    story_summary=story_summary,
                    child_age=child_age,
                    max_words=scene_max_words,
                    previous_scene_description=previous_scene_description,
                    next_scene_description=next_scene_description,
                    previous_line=previous_line,
                    next_line=next_line,
                )
            if fallback_rewrite or not studio_plan_trusted:
                normalized_narration_lines.append(fallback_rewrite or fallback_line or llm_line)
                continue
            normalized_narration_lines.append(fallback_line or llm_line)
        narration_lines = normalized_narration_lines
        minimum_required_narration_count = scene_count if enable_tts and narration_required_default() else 0
        expected_narration_count = max(
            sum(1 for line in narration_lines if str(line or "").strip()),
            minimum_required_narration_count,
        )
        rendered_narration_count = 0
        require_selected_elevenlabs_voice = bool(storybook_elevenlabs_voice_id)

        cover_audio_path: Path | None = None
        cover_audio_duration = 0.0
        end_card_audio_path: Path | None = None
        end_card_audio_duration = 0.0
        tts_audio_paths: list[Path | None] = []
        tts_audio_durations: list[float] = []
        tts_word_starts_ms: list[list[int]] = []
        tts_timing_modes: list[str | None] = []
        existing_audio_durations: list[float] = [0.0] * len(narration_lines)
        if enable_tts and not use_existing_audio and narration_lines:
            tts_audio_paths = [None] * len(narration_lines)
            tts_audio_durations = [0.0] * len(narration_lines)
            tts_word_starts_ms = [[] for _ in narration_lines]
            tts_providers: list[str | None] = [None] * len(narration_lines)
            tts_timing_modes = [None] * len(narration_lines)
            try:
                default_tts_concurrency = "6" if fast_storybook_assembly else "4"
                tts_concurrency = int(os.environ.get("STORYBOOK_TTS_CONCURRENCY", default_tts_concurrency))
            except Exception:
                tts_concurrency = 6 if fast_storybook_assembly else 4
            tts_semaphore = asyncio.Semaphore(max(1, min(tts_concurrency, 6)))

            async def _render_tts_line(idx: int, line: str) -> tuple[int, Path | None, float, str | None, list[int], str | None]:
                if not line:
                    return idx, None, 0.0, None, [], None
                async with tts_semaphore:
                    provider, audio_bytes, raw_word_timings = await asyncio.to_thread(
                        _synthesize_tts_with_provider,
                        line,
                        child_age=child_age,
                        storybook_movie_pacing=storybook_movie_pacing,
                        voice_id=storybook_elevenlabs_voice_id,
                        allow_non_elevenlabs_fallback=not require_selected_elevenlabs_voice,
                    )
                if not audio_bytes:
                    return idx, None, 0.0, provider, [], None
                audio_path = tmp / f"tts_{idx:03d}.mp3"
                audio_path.write_bytes(audio_bytes)
                duration = await asyncio.to_thread(_ffprobe_duration, audio_path)
                word_starts_ms, timing_mode = _resolve_storybook_word_starts_ms(
                    line,
                    duration,
                    word_timings=raw_word_timings,
                )
                return idx, audio_path, duration, provider, word_starts_ms, timing_mode

            async def _render_fallback_tts_bundle(
                prefix: str,
            ) -> tuple[list[Path | None], list[float], list[str | None], list[list[int]], list[str | None]]:
                bundle_paths: list[Path | None] = [None] * len(narration_lines)
                bundle_durations: list[float] = [0.0] * len(narration_lines)
                bundle_providers: list[str | None] = [None] * len(narration_lines)
                bundle_word_starts_ms: list[list[int]] = [[] for _ in narration_lines]
                bundle_timing_modes: list[str | None] = [None] * len(narration_lines)
                async def _render_bundle_line(
                    idx: int,
                    line: str,
                ) -> tuple[int, Path | None, float, str | None, list[int], str | None]:
                    if not line:
                        return idx, None, 0.0, None, [], None
                    provider: str | None = None
                    async with tts_semaphore:
                        audio_bytes = await asyncio.to_thread(
                            _synthesize_tts_gemini_only,
                            line,
                            child_age=child_age,
                            storybook_movie_pacing=storybook_movie_pacing,
                        )
                        if audio_bytes:
                            provider = "gemini"
                        else:
                            audio_bytes = await asyncio.to_thread(
                                _synthesize_tts_google_only,
                                line,
                                child_age=child_age,
                                storybook_movie_pacing=storybook_movie_pacing,
                            )
                            if audio_bytes:
                                provider = "google"
                    if not audio_bytes:
                        return idx, None, 0.0, provider, [], None
                    audio_path = tmp / f"{prefix}_{idx:03d}.mp3"
                    audio_path.write_bytes(audio_bytes)
                    duration = await asyncio.to_thread(_ffprobe_duration, audio_path)
                    word_starts_ms, timing_mode = _resolve_storybook_word_starts_ms(line, duration)
                    return idx, audio_path, duration, provider, word_starts_ms, timing_mode

                bundle_results = await asyncio.gather(
                    *(
                        _render_bundle_line(idx, line)
                        for idx, line in enumerate(narration_lines)
                        if line
                    ),
                    return_exceptions=True,
                )
                for result in bundle_results:
                    if isinstance(result, Exception):
                        logger.warning("Deterministic fallback TTS failed for %s: %s", session_id, result)
                        continue
                    idx, audio_path, duration, provider, word_starts_ms, timing_mode = result
                    bundle_paths[idx] = audio_path
                    bundle_durations[idx] = duration
                    bundle_providers[idx] = provider
                    bundle_word_starts_ms[idx] = word_starts_ms
                    bundle_timing_modes[idx] = timing_mode
                return bundle_paths, bundle_durations, bundle_providers, bundle_word_starts_ms, bundle_timing_modes

            tts_results = await asyncio.gather(
                *(_render_tts_line(idx, line) for idx, line in enumerate(narration_lines)),
                return_exceptions=True,
            )
            for result in tts_results:
                if isinstance(result, Exception):
                    logger.warning("Storybook narration TTS failed for %s: %s", session_id, result)
                    continue
                idx, audio_path, duration, provider, word_starts_ms, timing_mode = result
                if 0 <= idx < len(tts_audio_paths):
                    tts_audio_paths[idx] = audio_path
                    tts_audio_durations[idx] = duration
                    tts_providers[idx] = provider
                    tts_word_starts_ms[idx] = word_starts_ms
                    tts_timing_modes[idx] = timing_mode
            missing_tts_indexes = [
                idx
                for idx, line in enumerate(narration_lines)
                if line and idx < len(tts_audio_paths) and tts_audio_paths[idx] is None
            ]
            if missing_tts_indexes:
                if require_selected_elevenlabs_voice:
                    logger.warning(
                        "Selected ElevenLabs narrator %s did not render %d movie narration lines for %s; "
                        "skipping Gemini/Google fallback to avoid mixed voices.",
                        storybook_elevenlabs_voice_id,
                        len(missing_tts_indexes),
                        session_id,
                    )
                else:
                    logger.info(
                        "Retrying %d narration lines with Gemini/Google TTS fallback for %s.",
                        len(missing_tts_indexes),
                        session_id,
                    )
            if not require_selected_elevenlabs_voice:
                async def _retry_missing_tts_line(
                    idx: int,
                ) -> tuple[int, Path | None, float, str | None, list[int], str | None]:
                    provider: str | None = None
                    async with tts_semaphore:
                        audio_bytes = await asyncio.to_thread(
                            _synthesize_tts_gemini_only,
                            narration_lines[idx],
                            child_age=child_age,
                            storybook_movie_pacing=storybook_movie_pacing,
                        )
                        if audio_bytes:
                            provider = "gemini"
                        else:
                            audio_bytes = await asyncio.to_thread(
                                _synthesize_tts_google_only,
                                narration_lines[idx],
                                child_age=child_age,
                                storybook_movie_pacing=storybook_movie_pacing,
                            )
                            if audio_bytes:
                                provider = "google"
                    if not audio_bytes:
                        return idx, None, 0.0, provider, [], None
                    audio_path = tmp / f"tts_{idx:03d}.mp3"
                    audio_path.write_bytes(audio_bytes)
                    duration = await asyncio.to_thread(_ffprobe_duration, audio_path)
                    word_starts_ms, timing_mode = _resolve_storybook_word_starts_ms(
                        narration_lines[idx],
                        duration,
                    )
                    return idx, audio_path, duration, provider, word_starts_ms, timing_mode

                retry_results = await asyncio.gather(
                    *(_retry_missing_tts_line(idx) for idx in missing_tts_indexes),
                    return_exceptions=True,
                )
                for result in retry_results:
                    if isinstance(result, Exception):
                        logger.warning("Gemini/Google retry narration failed for %s: %s", session_id, result)
                        continue
                    idx, audio_path, duration, provider, word_starts_ms, timing_mode = result
                    if not audio_path:
                        continue
                    tts_audio_paths[idx] = audio_path
                    tts_audio_durations[idx] = duration
                    tts_providers[idx] = provider
                    tts_word_starts_ms[idx] = word_starts_ms
                    tts_timing_modes[idx] = timing_mode
            rendered_narration_count = sum(1 for path in tts_audio_paths if path is not None)
            provider_counts = Counter(provider for provider in tts_providers if provider)
            logger.info(
                "Storybook narration plan for %s: %d/%d lines rendered | providers=%s",
                session_id,
                rendered_narration_count,
                len(tts_audio_paths),
                dict(provider_counts),
            )
            if expected_narration_count and rendered_narration_count < expected_narration_count:
                logger.warning(
                    "Incomplete narration rendered for %s: %d/%d lines.",
                    session_id,
                    rendered_narration_count,
                    expected_narration_count,
                )
                if not require_selected_elevenlabs_voice:
                    fallback_paths, fallback_durations, fallback_providers, fallback_word_starts_ms, fallback_timing_modes = await _render_fallback_tts_bundle("tts_fallback")
                    fallback_rendered_narration_count = sum(1 for path in fallback_paths if path is not None)
                    if fallback_rendered_narration_count > rendered_narration_count:
                        logger.info(
                            "Deterministic Gemini/Google narration fallback improved %s to %d/%d lines.",
                            session_id,
                            fallback_rendered_narration_count,
                            expected_narration_count,
                        )
                        tts_audio_paths = fallback_paths
                        tts_audio_durations = fallback_durations
                        tts_providers = fallback_providers
                        tts_word_starts_ms = fallback_word_starts_ms
                        tts_timing_modes = fallback_timing_modes
                        rendered_narration_count = fallback_rendered_narration_count
                if force_tts or narration_required_default():
                    await _mark_assembly_failed(
                        doc_ref,
                        session_id,
                        (
                            "Storybook narration failed before final assembly. "
                            f"Rendered {rendered_narration_count}/{expected_narration_count} narration lines, "
                            "so the worker aborted instead of publishing a partially silent movie."
                        ),
                    )
        cover_audio_path: Path | None = None
        cover_audio_duration = 0.0
        if cover_enabled and enable_tts and use_existing_audio and cover_narration:
            cover_bytes = _synthesize_tts_google(
                cover_narration,
                child_age=child_age,
                storybook_movie_pacing=storybook_movie_pacing,
            )
            if cover_bytes:
                cover_audio_path = tmp / "tts_cover.mp3"
                cover_audio_path.write_bytes(cover_bytes)
                cover_audio_duration = _ffprobe_duration(cover_audio_path)
        if end_card_enabled and enable_tts and use_existing_audio and end_card_narration:
            end_card_bytes = _synthesize_tts_google(
                end_card_narration,
                child_age=child_age,
                storybook_movie_pacing=storybook_movie_pacing,
            )
            if end_card_bytes:
                end_card_audio_path = tmp / "tts_end_card.mp3"
                end_card_audio_path.write_bytes(end_card_bytes)
                end_card_audio_duration = _ffprobe_duration(end_card_audio_path)
        if use_existing_audio and valid_audios:
            audio_offset = 1 if cover_enabled else 0
            if cover_enabled and cover_audio_duration > 0.0 and existing_audio_durations:
                existing_audio_durations[0] = cover_audio_duration
            for idx, audio_path in enumerate(valid_audios):
                target_idx = idx + audio_offset
                if target_idx >= len(existing_audio_durations):
                    break
                try:
                    existing_audio_durations[target_idx] = _ffprobe_duration(audio_path)
                except Exception:
                    existing_audio_durations[target_idx] = 0.0
            if end_card_enabled and end_card_audio_duration > 0.0 and existing_audio_durations:
                existing_audio_durations[-1] = end_card_audio_duration
            rendered_narration_count = sum(
                1
                for idx, duration in enumerate(existing_audio_durations)
                if duration > 0.1 and (not cover_enabled or idx > 0)
            )
        # Build per-scene segments (stills with optional Veo clips)
        segments: list[Path] = []
        scene_durations: list[float] = []
        if cover_enabled:
            cover_duration = cover_seconds
            cover_audio_len = 0.0
            if tts_audio_durations:
                cover_audio_len = tts_audio_durations[0] if len(tts_audio_durations) > 0 else 0.0
            elif cover_audio_duration > 0.0:
                cover_audio_len = cover_audio_duration
            if cover_audio_len > 0.0:
                cover_duration = max(cover_duration, cover_audio_len + 0.4)

            cover_logo_path = _storybook_cover_logo_path()
            cover_filter = _build_storybook_cover_filtergraph(
                title_text=title or "A Storybook Adventure",
                subtitle_text=cover_author,
                duration=cover_duration,
                logo_path=cover_logo_path,
            )
            cover_path = tmp / "segment_cover.mp4"
            if cover_image_bytes:
                cover_image_path = tmp / "cover.png"
                cover_image_path.write_bytes(cover_image_bytes)
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-loop", "1",
                        "-framerate", str(_STORYBOOK_FPS),
                        "-i", str(cover_image_path),
                        "-t", f"{cover_duration:.3f}",
                        "-filter_complex", cover_filter,
                        "-map", "[outv]",
                        "-r", "30",
                        "-pix_fmt", "yuv420p",
                        "-c:v", "libx264",
                        str(cover_path),
                    ],
                    check=True,
                    capture_output=True,
                )
            else:
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-f", "lavfi",
                        "-i", f"color=c=#0b2d5b:s=1280x720:d={cover_duration:.3f}",
                        "-filter_complex", cover_filter,
                        "-map", "[outv]",
                        "-r", "30",
                        "-pix_fmt", "yuv420p",
                        "-c:v", "libx264",
                        str(cover_path),
                    ],
                    check=True,
                    capture_output=True,
                )
            segments.append(cover_path)
            scene_durations.append(cover_duration)
        if still_paths:
            render_scene_descriptions = [
                str(scene_descriptions[idx]).strip() if idx < len(scene_descriptions) else ""
                for idx in range(len(still_paths))
            ]
            scene_shot_plans = plan_storyboard_shots(render_scene_descriptions)
            try:
                still_seconds = clamp_page_seconds(os.environ.get("FINAL_SCENE_SECONDS", "4"))
            except Exception:
                still_seconds = clamp_page_seconds(None)

            use_veo = _env_enabled("ENABLE_VEO_FINAL", default=False)
            try:
                veo_count = int(os.environ.get("VEO_FINAL_SCENE_COUNT", "1"))
            except Exception:
                veo_count = 1
            veo_count = max(0, min(veo_count, 2))
            try:
                min_score = int(os.environ.get("VEO_MAGIC_MIN_SCORE", "2"))
            except Exception:
                min_score = 2
            min_score = max(1, min(min_score, 6))
            veo_indices = (
                _choose_veo_indices(len(still_paths), veo_count, scene_descriptions, min_score)
                if use_veo
                else []
            )

            veo_clips: dict[int, Path] = {}
            if veo_indices:
                location = _vertex_ai_location()
                for idx in veo_indices:
                    try:
                        img_bytes = still_paths[idx].read_bytes()
                        desc = ""
                        if idx < len(scene_descriptions):
                            desc = str(scene_descriptions[idx]).strip()
                        prompt = desc or "A whimsical story scene"
                        prompt = (
                            f"{prompt}. Gently animate with magical twinkles, soft breeze, and warm light. "
                            "Keep it child-friendly and dreamy."
                        )
                        op_name = await asyncio.to_thread(
                            _call_veo_api,
                            PROJECT,
                            location,
                            img_bytes,
                            prompt,
                            _story_tone_veo_negative_prompt(story_tone),
                        )
                        raw_url = await asyncio.to_thread(
                            _poll_veo_operation, PROJECT, location, op_name
                        )
                        clip_path = tmp / f"veo_{idx:03d}.mp4"
                        async with httpx.AsyncClient() as http:
                            await _download_any(http, gcs, raw_url, clip_path)
                        veo_clips[idx] = clip_path
                    except Exception as exc:
                        logger.warning("Veo final clip failed for scene %s: %s", idx, exc)

            veo_duration = 5.0
            audio_offset = 1 if cover_enabled else 0
            segment_duration_plan: list[float] = []
            for idx, _still_path in enumerate(still_paths):
                base_duration = veo_duration if idx in veo_clips else float(still_seconds)
                audio_idx = idx + audio_offset
                audio_len = 0.0
                if tts_audio_durations and audio_idx < len(tts_audio_durations):
                    audio_len = tts_audio_durations[audio_idx]
                elif existing_audio_durations and audio_idx < len(existing_audio_durations):
                    audio_len = existing_audio_durations[audio_idx]
                primary_text = str(story_pages[idx].get("storybeat_text", "") or "").strip() if idx < len(story_pages) else ""
                fallback_text = str(narration_lines[audio_idx] or "").strip() if audio_idx < len(narration_lines) else ""
                readalong_text = choose_readalong_text(
                    primary_text,
                    fallback_text,
                    child_age,
                    storybook_movie_pacing,
                )
                segment_duration_plan.append(
                    storybook_page_duration_seconds(
                        child_age=child_age,
                        base_page_seconds=base_duration,
                        narration_seconds=audio_len,
                        readalong_text=readalong_text,
                        movie_pacing=storybook_movie_pacing,
                    )
                )
            scene_durations.extend(segment_duration_plan)

            render_scene_semaphore = asyncio.Semaphore(_storybook_scene_render_concurrency())

            async def _render_scene_segment(
                idx: int,
                still_path: Path,
                base_duration: float,
            ) -> tuple[int, Path]:
                segment_path = tmp / f"segment_{idx:03d}.mp4"
                shot_plan = scene_shot_plans[idx] if idx < len(scene_shot_plans) else None
                async with render_scene_semaphore:
                    if idx in veo_clips:
                        normalized_path = tmp / f"veo_norm_{idx:03d}.mp4"
                        await asyncio.to_thread(
                            subprocess.run,
                            [
                                "ffmpeg", "-y",
                                "-i", str(veo_clips[idx]),
                                "-vf", _storybook_video_scene_canvas_vf(),
                                "-r", "30",
                                "-pix_fmt", "yuv420p",
                                "-c:v", "libx264",
                                str(normalized_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                        extra = max(0.0, base_duration - veo_duration)
                        if extra > 0.05:
                            hold_path = tmp / f"hold_{idx:03d}.mp4"
                            await asyncio.to_thread(
                                _render_storybook_cinematic_segment,
                                still_path,
                                hold_path,
                                extra,
                                idx,
                                shot_plan,
                            )
                            concat_hold = tmp / f"concat_hold_{idx:03d}.txt"
                            concat_hold.write_text(
                                "\n".join([f"file '{normalized_path.name}'", f"file '{hold_path.name}'"])
                            )
                            await asyncio.to_thread(
                                subprocess.run,
                                [
                                    "ffmpeg", "-y",
                                    "-f", "concat", "-safe", "0",
                                    "-i", str(concat_hold),
                                    "-an",
                                    "-vf", "fps=30,format=yuv420p,setsar=1",
                                    "-c:v", "libx264",
                                    "-preset", "veryfast",
                                    "-crf", "18",
                                    "-pix_fmt", "yuv420p",
                                    str(segment_path),
                                ],
                                check=True,
                                capture_output=True,
                            )
                        else:
                            await asyncio.to_thread(segment_path.write_bytes, normalized_path.read_bytes())
                    else:
                        await asyncio.to_thread(
                            _render_storybook_cinematic_segment,
                            still_path,
                            segment_path,
                            base_duration,
                            idx,
                            shot_plan,
                        )
                return idx, segment_path

            rendered_scene_segments = await asyncio.gather(
                *(
                    _render_scene_segment(idx, still_path, segment_duration_plan[idx])
                    for idx, still_path in enumerate(still_paths)
                ),
                return_exceptions=True,
            )
            ordered_segments: list[tuple[int, Path]] = []
            for result in rendered_scene_segments:
                if isinstance(result, Exception):
                    raise result
                ordered_segments.append(result)
            ordered_segments.sort(key=lambda item: item[0])
            segments.extend(path for _, path in ordered_segments)
        else:
            scene_shot_plans = []

        if downloaded_videos and not still_paths:
            for clip in downloaded_videos:
                segments.append(clip)
                scene_durations.append(_ffprobe_duration(clip))

        # Fallback to existing video clips if no stills available
        if not segments and downloaded_videos:
            segments = downloaded_videos

        if not scene_durations and segments:
            scene_durations = [_ffprobe_duration(p) for p in segments]

        end_card_index: int | None = None
        if end_card_enabled and segments:
            end_audio_idx = len(narration_lines) - 1
            end_audio_len = 0.0
            if tts_audio_durations and 0 <= end_audio_idx < len(tts_audio_durations):
                end_audio_len = tts_audio_durations[end_audio_idx]
            elif end_card_audio_duration > 0.0:
                end_audio_len = end_card_audio_duration
            elif existing_audio_durations and 0 <= end_audio_idx < len(existing_audio_durations):
                end_audio_len = existing_audio_durations[end_audio_idx]
            end_duration = max(end_card_seconds, end_audio_len + 0.35 if end_audio_len > 0.0 else end_card_seconds)
            end_background = still_paths[-1] if still_paths else None
            end_card_path = tmp / "segment_end_card.mp4"
            _render_storybook_end_card_segment(
                end_card_path,
                duration=end_duration,
                title_text=end_card_title,
                subtitle_text=cover_author,
                background_image_path=end_background,
            )
            segments.append(end_card_path)
            scene_durations.append(end_duration)
            end_card_index = len(scene_durations) - 1

        music_paths: list[Path | None] = []
        sfx_paths: list[Path | None] = [None] * len(scene_durations)
        audio_scene_descriptions = scene_descriptions
        still_paths_for_audio = still_paths
        if cover_enabled:
            audio_scene_descriptions = [cover_description] + scene_descriptions
            still_paths_for_audio = None
        if end_card_enabled:
            audio_scene_descriptions = list(audio_scene_descriptions) + [end_card_title]
        cover_offset = 1 if cover_enabled else 0
        studio_music_cues = {
            idx + cover_offset: cue
            for idx, cue in dict(studio_plan.get("music_cues", {}) or {}).items()
            if isinstance(idx, int)
        }
        studio_sfx_cues = {
            idx + cover_offset: cue
            for idx, cue in dict(studio_plan.get("sfx_cues", {}) or {}).items()
            if isinstance(idx, int)
        }
        if studio_audio_locked:
            music_cues, sfx_cues = studio_music_cues, studio_sfx_cues
            if enable_music and not music_cues and bool(studio_plan.get("music_enabled", True)) and scene_durations:
                arc_prompt = str(studio_plan.get("music_arc_prompt") or "").strip()
                default_indices = [0]
                if len(scene_durations) > 1:
                    default_indices.append(len(scene_durations) - 1)
                for idx in dict.fromkeys(default_indices):
                    desc = audio_scene_descriptions[idx] if idx < len(audio_scene_descriptions) else ""
                    music_cues[idx] = {
                        "prompt": arc_prompt or _build_music_prompt(desc, idx, len(scene_durations)),
                        "duration_seconds": min(10.0, max(2.0, scene_durations[idx])),
                    }
        else:
            if fast_storybook_assembly:
                music_cues, sfx_cues = _heuristic_audio_cues(
                    audio_scene_descriptions,
                    len(scene_durations),
                    _clamp_int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"), 2, 0, 4),
                    clamp_sfx_max(os.environ.get("STORYBOOK_SFX_MAX", "2")),
                )
            else:
                music_cues, sfx_cues = _plan_audio_cues(
                    audio_scene_descriptions,
                    story_summary,
                    len(scene_durations),
                    still_paths=still_paths_for_audio,
                )
            if cover_enabled:
                sfx_cues.pop(0, None)
        if enable_music and scene_durations and len(music_cues) < min(2, len(scene_durations)):
            heuristic_music, _ = _heuristic_audio_cues(
                audio_scene_descriptions,
                len(scene_durations),
                _clamp_int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"), 2, 0, 4),
                0,
            )
            for cue_idx, cue in heuristic_music.items():
                music_cues.setdefault(cue_idx, cue)
        if end_card_index is not None:
            music_cues = _ensure_end_card_music_cue(
                music_cues,
                end_card_index=end_card_index,
                audio_scene_descriptions=audio_scene_descriptions,
                scene_durations=scene_durations,
                studio_music_arc_prompt=str(studio_plan.get("music_arc_prompt") or "").strip(),
            )
            sfx_cues.pop(end_card_index, None)

        music_cue_default = _clamp_float(
            os.environ.get("STORYBOOK_MUSIC_CUE_SECONDS", "10"),
            10.0,
            2.0,
            30.0,
        )
        sfx_cue_default = _clamp_float(
            os.environ.get("STORYBOOK_SFX_CUE_SECONDS", "2"),
            2.0,
            0.6,
            6.0,
        )

        async def _generate_music_path(
            idx: int,
            duration: float,
            cue: dict[str, Any],
        ) -> tuple[int, Path | None]:
            desc = audio_scene_descriptions[idx] if idx < len(audio_scene_descriptions) else ""
            prompt = str(cue.get("prompt") or "").strip() or _build_music_prompt(desc, idx, len(scene_durations))
            cue_seconds = cue.get("duration_seconds")
            try:
                cue_seconds = float(cue_seconds)
            except Exception:
                cue_seconds = music_cue_default
            cue_seconds = max(2.0, min(float(cue_seconds), duration))
            music_seed = sum(ord(ch) for ch in f"{session_id}:{idx}") or None
            music_result = await _generate_storybook_music_bytes(prompt, cue_seconds, seed=music_seed)
            if not music_result:
                return idx, None
            audio_bytes, suffix = music_result
            music_path = tmp / f"music_{idx:03d}{suffix or '.wav'}"
            music_path.write_bytes(audio_bytes)
            return idx, music_path

        async def _generate_sfx_path(
            idx: int,
            duration: float,
            cue: dict[str, Any],
        ) -> tuple[int, Path | None]:
            desc = audio_scene_descriptions[idx] if idx < len(audio_scene_descriptions) else ""
            prompt = str(cue.get("prompt") or "").strip() or _build_sfx_prompt(desc)
            cue_seconds = cue.get("duration_seconds")
            try:
                cue_seconds = float(cue_seconds)
            except Exception:
                cue_seconds = sfx_cue_default
            cue_seconds = max(0.6, min(float(cue_seconds), duration))
            sfx_result = await _generate_storybook_sfx_bytes(prompt, cue_seconds)
            if not sfx_result:
                return idx, None
            audio_bytes, suffix = sfx_result
            sfx_path = tmp / f"sfx_{idx:03d}{suffix or '.mp3'}"
            sfx_path.write_bytes(audio_bytes)
            return idx, sfx_path

        music_paths = [None] * len(scene_durations)
        music_tasks = (
            [
                _generate_music_path(idx, duration, cue)
                for idx, duration in enumerate(scene_durations)
                if (cue := music_cues.get(idx))
            ]
            if enable_music and scene_durations
            else []
        )
        sfx_tasks: list[Any] = []
        if enable_sfx and scene_durations:
            if sfx_cues:
                logger.info("Storybook SFX scenes selected: %s", sorted(sfx_cues.keys()))
            sfx_tasks = [
                _generate_sfx_path(idx, duration, cue)
                for idx, duration in enumerate(scene_durations)
                if (cue := sfx_cues.get(idx))
            ]

        music_results: list[Any] = []
        sfx_results: list[Any] = []
        if music_tasks and sfx_tasks:
            music_results, sfx_results = await asyncio.gather(
                asyncio.gather(*music_tasks, return_exceptions=True),
                asyncio.gather(*sfx_tasks, return_exceptions=True),
            )
        elif music_tasks:
            music_results = await asyncio.gather(*music_tasks, return_exceptions=True)
        elif sfx_tasks:
            sfx_results = await asyncio.gather(*sfx_tasks, return_exceptions=True)

        for result in music_results:
            if isinstance(result, Exception):
                logger.warning("Storybook music cue generation failed: %s", result)
                continue
            idx, music_path = result
            music_paths[idx] = music_path

        for result in sfx_results:
            if isinstance(result, Exception):
                logger.warning("Storybook SFX cue generation failed: %s", result)
                continue
            idx, sfx_path = result
            sfx_paths[idx] = sfx_path

        logger.info(
            "FFmpeg storybook audio plan for %s: narration=%d/%d music=%d/%d sfx=%d/%d force_tts=%s reused_audio=%s",
            session_id,
            sum(1 for path in tts_audio_paths if path is not None),
            len(tts_audio_paths),
            sum(1 for path in music_paths if path is not None),
            len(music_paths),
            sum(1 for path in sfx_paths if path is not None),
            len(sfx_paths),
            force_tts,
            use_existing_audio,
        )

        if not segments:
            await _mark_assembly_failed(
                doc_ref,
                session_id,
                f"No renderable segments could be built for session {session_id}.",
            )

        # Create FFmpeg concat list
        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{v.name}'" for v in segments)
        )

        base_video = tmp / "story_base.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-an",
            "-vf", "fps=30,format=yuv420p,setsar=1",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(base_video),
        ], check=True, capture_output=True)

        audio_track: Path | None = None
        narration_reference_track: Path | None = None
        if tts_audio_paths and scene_durations and len(tts_audio_paths) != len(scene_durations):
            logger.warning(
                "Aligning narration tracks for %s: tts_paths=%d scene_durations=%d",
                session_id,
                len(tts_audio_paths),
                len(scene_durations),
            )
            if len(tts_audio_paths) < len(scene_durations):
                tts_audio_paths = tts_audio_paths + [None] * (len(scene_durations) - len(tts_audio_paths))
            else:
                tts_audio_paths = tts_audio_paths[:len(scene_durations)]
        if tts_audio_paths and len(tts_audio_paths) == len(scene_durations) and any(tts_audio_paths):
            audio_track = _build_storybook_padded_audio_track(
                tmp=tmp,
                audio_paths=tts_audio_paths,
                durations=scene_durations,
                basename="narration_track",
            )
            narration_reference_track = audio_track
        elif use_existing_audio and valid_audios:
            existing_paths: list[Path | None] = list(valid_audios)
            if cover_enabled:
                existing_paths = [cover_audio_path] + existing_paths
            if end_card_enabled:
                existing_paths = existing_paths + [end_card_audio_path]
            audio_track = _build_storybook_padded_audio_track(
                tmp=tmp,
                audio_paths=existing_paths,
                durations=scene_durations,
                basename="existing_narration_track",
            )
            narration_reference_track = audio_track
        narration_audio_available = bool(audio_track and audio_track.exists())
        if narration_audio_available:
            narration_audio_available = _ffprobe_duration(audio_track) > 0.1

        music_track: Path | None = None
        if enable_music and music_paths and any(music_paths):
            music_track = _build_storybook_padded_audio_track(
                tmp=tmp,
                audio_paths=music_paths,
                durations=scene_durations,
                basename="music_track",
                fade_in=0.4,
                fade_out=0.6,
            )

        sfx_track: Path | None = None
        if enable_sfx and sfx_paths and any(sfx_paths):
            sfx_track = _build_storybook_padded_audio_track(
                tmp=tmp,
                audio_paths=sfx_paths,
                durations=scene_durations,
                basename="sfx_track",
                fade_in=0.15,
                fade_out=0.25,
            )

        master_chain = ",alimiter=limit=0.97,loudnorm=I=-14:TP=-1.5:LRA=7" if _env_enabled("ENABLE_STORYBOOK_AUDIO_MASTERING", default=True) else ""
        voice_filter = _storybook_voice_filter_chain(narration_volume)

        if music_track and music_track.exists():
            if audio_track and audio_track.exists() and sfx_track and sfx_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                duck = (
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked];"
                    if enable_ducking
                    else ""
                )
                mix = (
                    "[voice][ducked][sfx]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0"
                    if enable_ducking
                    else "[voice][music][sfx]amix=inputs=3:duration=longest:dropout_transition=0:normalize=0"
                )
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(audio_track),
                        "-i", str(music_track),
                        "-i", str(sfx_track),
                        "-filter_complex",
                        f"[0:a]{voice_filter}[voice];[1:a]volume={music_volume}[music];[2:a]volume={sfx_volume}[sfx];{duck}{mix}{master_chain}",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            elif audio_track and audio_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                duck = (
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked];"
                    if enable_ducking
                    else ""
                )
                mix = (
                    "[voice][ducked]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0"
                    if enable_ducking
                    else "[voice][music]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0"
                )
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(audio_track),
                        "-i", str(music_track),
                        "-filter_complex",
                        f"[0:a]{voice_filter}[voice];[1:a]volume={music_volume}[music];{duck}{mix}{master_chain}",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            elif sfx_track and sfx_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(music_track),
                        "-i", str(sfx_track),
                        "-filter_complex",
                        f"[0:a]volume={music_volume}[music];[1:a]volume={sfx_volume}[sfx];[music][sfx]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0{master_chain}",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            else:
                music_only = tmp / "music_only.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(music_track),
                        "-filter:a", f"volume={music_volume}{master_chain}",
                        "-c:a", "pcm_s16le",
                        str(music_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = music_only
        elif sfx_track and sfx_track.exists():
            if audio_track and audio_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(audio_track),
                        "-i", str(sfx_track),
                        "-filter_complex",
                        f"[0:a]{voice_filter}[voice];[1:a]volume={sfx_volume}[sfx];[voice][sfx]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0{master_chain}",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            else:
                sfx_only = tmp / "sfx_only.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(sfx_track),
                        "-filter:a", f"volume={sfx_volume}{master_chain}",
                        "-c:a", "pcm_s16le",
                        str(sfx_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = sfx_only

        # If only narration is present, still apply the narration volume boost.
        if (
            audio_track
            and audio_track.exists()
            and not (music_track and music_track.exists())
            and not (sfx_track and sfx_track.exists())
            and abs(narration_volume - 1.0) > 0.01
        ):
            boosted = tmp / "audio_narration_boosted.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(audio_track),
                    "-filter:a", f"{voice_filter}{master_chain}",
                    "-c:a", "pcm_s16le",
                    str(boosted),
                ],
                check=True,
                capture_output=True,
            )
            audio_track = boosted

        narration_mix_guard_applied = False
        narration_reference_nonsilent_seconds = None
        mixed_nonsilent_seconds = None
        if (
            narration_reference_track
            and narration_reference_track.exists()
            and audio_track
            and audio_track.exists()
            and audio_track != narration_reference_track
        ):
            narration_reference_nonsilent_seconds = _ffmpeg_nonsilent_seconds(narration_reference_track)
            mixed_nonsilent_seconds = _ffmpeg_nonsilent_seconds(audio_track)
            if (
                narration_reference_nonsilent_seconds is not None
                and mixed_nonsilent_seconds is not None
                and narration_reference_nonsilent_seconds >= 2.0
                and mixed_nonsilent_seconds < max(1.5, narration_reference_nonsilent_seconds * 0.65)
            ):
                logger.warning(
                    "Storybook audio mix for %s dropped spoken coverage from %.2fs to %.2fs; "
                    "falling back to narration-priority master.",
                    session_id,
                    narration_reference_nonsilent_seconds,
                    mixed_nonsilent_seconds,
                )
                audio_track = _master_storybook_narration_track(
                    narration_reference_track,
                    tmp=tmp,
                    voice_filter=voice_filter,
                    master_chain=master_chain,
                    filename="audio_narration_priority.wav",
                )
                narration_mix_guard_applied = True
                mixed_nonsilent_seconds = _ffmpeg_nonsilent_seconds(audio_track)

        expected_narration_audio = bool(enable_tts and narration_required_default())
        audio_available = narration_audio_available
        audio_level_info: dict[str, float | bool | None] = {
            "applied": False,
            "before_mean_volume_db": None,
            "before_max_volume_db": None,
            "gain_db": 0.0,
            "after_mean_volume_db": None,
            "after_max_volume_db": None,
        }
        if audio_track and audio_track.exists():
            try:
                audio_track, audio_level_info = _boost_quiet_audio_track(audio_track, tmp=tmp)
                if audio_level_info.get("applied"):
                    logger.warning(
                        "Storybook audio mix for %s was too quiet; applied %.1fdB recovery gain "
                        "(max %.1fdB -> %.1fdB).",
                        session_id,
                        float(audio_level_info.get("gain_db") or 0.0),
                        float(audio_level_info.get("before_max_volume_db") or 0.0),
                        float(audio_level_info.get("after_max_volume_db") or 0.0),
                    )
            except Exception as exc:
                logger.warning("Storybook quiet-audio recovery failed for %s: %s", session_id, exc)

        output_path = tmp / "story_final.mp4"
        if audio_track and audio_track.exists():
            _mux_story_video_with_audio(base_video, audio_track, output_path, reencode_video=False)
        else:
            output_path = base_video

        final_has_audio_stream = _ffprobe_has_audio_stream(output_path)
        if audio_track and audio_track.exists() and audio_available and not final_has_audio_stream:
            logger.warning(
                "Final video for %s is missing an audio stream after the fast mux path. "
                "Retrying with defensive video re-encode.",
                session_id,
            )
            remux_output_path = tmp / "story_final_remux.mp4"
            _mux_story_video_with_audio(base_video, audio_track, remux_output_path, reencode_video=True)
            remux_has_audio_stream = _ffprobe_has_audio_stream(remux_output_path)
            if remux_has_audio_stream:
                output_path = remux_output_path
                final_has_audio_stream = True
            else:
                logger.warning(
                    "Defensive remux also failed to produce an audio stream for %s.",
                    session_id,
                )
        if audio_available and not final_has_audio_stream:
            audio_available = False
            if expected_narration_audio:
                await _mark_assembly_failed(
                    doc_ref,
                    session_id,
                    (
                        "Storybook narration was generated, but the final MP4 still had no audio stream after muxing. "
                        "The worker aborted instead of publishing a silent movie."
                    ),
                )
        final_audio_mean_volume_db, final_audio_max_volume_db = (None, None)
        if final_has_audio_stream:
            final_audio_mean_volume_db, final_audio_max_volume_db = _ffmpeg_audio_levels(output_path)
        final_video_duration_sec = _ffprobe_duration(output_path)
        theater_release_ready, release_gate_issues = storybook_release_gate(
            scene_count=len(still_paths) or len(downloaded_videos),
            final_video_duration_sec=final_video_duration_sec,
            expected_audio=expected_narration_audio,
            audio_available=audio_available,
            final_has_audio_stream=final_has_audio_stream,
            expected_narration_count=expected_narration_count,
            rendered_narration_count=rendered_narration_count,
            shot_types=[plan.shot_type for plan in scene_shot_plans],
        )
        logger.info(
            "FFmpeg storybook final media audit for %s: narration_audio_available=%s final_has_audio_stream=%s narration_lines=%d release_issues=%s",
            session_id,
            narration_audio_available,
            final_has_audio_stream,
            sum(1 for line in narration_lines if line),
            release_gate_issues,
        )
        if not theater_release_ready:
            await _mark_assembly_failed(
                doc_ref,
                session_id,
                (
                    "The final storybook movie failed the release gate. "
                    f"duration={final_video_duration_sec:.2f}s "
                    f"audio_expected={expected_narration_audio} "
                    f"audio_available={audio_available} "
                    f"final_has_audio_stream={final_has_audio_stream} "
                    f"issues={'; '.join(release_gate_issues)}"
                ),
            )

        narration_audio_durations_for_readalong: list[float] = []
        for idx, _line in enumerate(narration_lines):
            tts_duration = tts_audio_durations[idx] if idx < len(tts_audio_durations) else 0.0
            existing_duration = existing_audio_durations[idx] if idx < len(existing_audio_durations) else 0.0
            narration_audio_durations_for_readalong.append(tts_duration if tts_duration > 0.0 else existing_duration)

        movie_readalong_segments = _build_storybook_movie_readalong_segments(
            scene_durations=scene_durations,
            narration_lines=narration_lines,
            narration_audio_durations=narration_audio_durations_for_readalong,
            narration_word_starts_ms=tts_word_starts_ms,
            narration_timing_modes=tts_timing_modes,
            story_pages=story_pages,
            child_age=child_age,
            storybook_movie_pacing=storybook_movie_pacing,
            cover_offset=1 if cover_enabled else 0,
            end_card_index=end_card_index,
        )
        theater_lighting_cues = _build_storybook_theater_lighting_cues(
            scene_durations=scene_durations,
            story_pages=story_pages,
            scene_descriptions=scene_descriptions,
            scene_lighting_cues=data.get("scene_lighting_cues"),
            cover_enabled=cover_enabled,
            cover_description=cover_description,
            end_card_index=end_card_index,
            end_card_title=end_card_title,
        )

        if enable_captions and scene_durations:
            captioned_path = tmp / "story_captioned.mp4"
            caption_overlay_applied = False
            if movie_readalong_segments:
                ass_path = tmp / "story_readalong.ass"
                ass_path.write_text(
                    _build_storybook_readalong_ass_text(movie_readalong_segments),
                    encoding="utf-8",
                )
                readalong_filters = _storybook_burned_caption_chrome_filtergraph(movie_readalong_segments)
                fonts_dir = _storybook_caption_fonts_dir()
                subtitle_filter = f"subtitles='{_ffmpeg_filter_path(ass_path)}'"
                if fonts_dir and fonts_dir.exists():
                    subtitle_filter += f":fontsdir='{_ffmpeg_filter_path(fonts_dir)}'"
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-i", str(output_path),
                            "-vf", ",".join([*readalong_filters, subtitle_filter]),
                            "-c:a", "copy",
                            "-movflags", "+faststart",
                            str(captioned_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    output_path = captioned_path
                    caption_overlay_applied = True
                except Exception as exc:
                    logger.warning("Timed read-along caption overlay failed, falling back to static captions: %s", exc)
            if not caption_overlay_applied:
                caption_segments = _storybook_burned_caption_segments(
                    scene_durations=scene_durations,
                    fallback_lines=narration_lines,
                    story_pages=story_pages,
                    child_age=child_age,
                    storybook_movie_pacing=storybook_movie_pacing,
                    cover_offset=1 if cover_enabled else 0,
                    skip_indices={end_card_index} if end_card_index is not None else None,
                )
                caption_filters = _storybook_burned_caption_filtergraph(caption_segments)
                if caption_filters:
                    try:
                        subprocess.run(
                            [
                                "ffmpeg", "-y",
                                "-i", str(output_path),
                                "-vf", ",".join(caption_filters),
                                "-c:a", "copy",
                                "-movflags", "+faststart",
                                str(captioned_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                        output_path = captioned_path
                    except Exception as exc:
                        logger.warning("Book-style caption overlay failed: %s", exc)

        logger.info(f"FFmpeg complete. Final video: {output_path.stat().st_size} bytes.")

        # Upload to GCS final videos bucket
        bucket = gcs.bucket(GCS_FINAL_BUCKET)
        blob = bucket.blob(f"{session_id}/story_final.mp4")
        blob.content_disposition = f'attachment; filename="{session_id}-story.mp4"'
        blob.cache_control = "no-store, max-age=0, must-revalidate"
        blob.upload_from_filename(str(output_path), content_type="video/mp4")
        final_gcs_uri = f"gs://{GCS_FINAL_BUCKET}/{session_id}/story_final.mp4"
        cache_token = str(blob.generation or int(time.time() * 1000))
        final_url = _build_public_story_video_url(session_id, cache_token)

        logger.info(f"Uploaded final video: {final_url}")

        # Write final URL back to Firestore session doc
        await doc_ref.update({
            "final_video_url": final_url,
            "final_video_cache_token": cache_token,
            "final_video_gcs_uri": final_gcs_uri,
            "storybook_movie_pacing": storybook_movie_pacing,
            "narration_lines": [line for line in narration_lines if line] or [line for line in default_narration_lines if line],
            "audio_expected": expected_narration_audio,
            "audio_available": audio_available,
            "expected_narration_count": expected_narration_count,
            "rendered_narration_count": rendered_narration_count,
            "final_has_audio_stream": final_has_audio_stream,
            "final_video_duration_sec": final_video_duration_sec,
            "theater_lighting_cues": theater_lighting_cues,
            "final_scene_count": len(still_paths) or len(downloaded_videos),
            "final_shot_types": [plan.shot_type for plan in scene_shot_plans],
            "theater_release_ready": theater_release_ready,
            "audio_recovery_applied": bool(audio_level_info.get("applied")),
            "audio_recovery_gain_db": float(audio_level_info.get("gain_db") or 0.0),
            "audio_mix_before_mean_volume_db": audio_level_info.get("before_mean_volume_db"),
            "audio_mix_before_max_volume_db": audio_level_info.get("before_max_volume_db"),
            "audio_mix_after_mean_volume_db": audio_level_info.get("after_mean_volume_db"),
            "audio_mix_after_max_volume_db": audio_level_info.get("after_max_volume_db"),
            "final_audio_mean_volume_db": final_audio_mean_volume_db,
            "final_audio_max_volume_db": final_audio_max_volume_db,
            "narration_reference_nonsilent_seconds": narration_reference_nonsilent_seconds,
            "mixed_audio_nonsilent_seconds": mixed_nonsilent_seconds,
            "narration_mix_guard_applied": narration_mix_guard_applied,
            "video_assembly_runtime_overrides": runtime_overrides,
            "video_assembly_reason": "Final storybook movie uploaded.",
            "assembly_status": "complete",
            "storyboard_review": storyboard_review_report,
            "storybook_studio": studio_summary,
            "post_movie_meta_review": {
                **storyboard_review_report,
                "source": "assembly_review",
            },
        })

        logger.info(f"Session {session_id} assembly complete.")
        return {
            "session_id": session_id,
            "status": "complete",
            "ready_to_publish": theater_release_ready,
            "retryable": False,
            "reason": "Final storybook movie uploaded.",
            "issues": [],
            "runtime_overrides": runtime_overrides,
            "final_video_url": final_url,
            "final_video_cache_token": cache_token,
            "audio_expected": expected_narration_audio,
            "audio_available": audio_available,
            "final_has_audio_stream": final_has_audio_stream,
            "theater_release_ready": theater_release_ready,
            "final_video_duration_sec": final_video_duration_sec,
            "expected_narration_count": expected_narration_count,
            "rendered_narration_count": rendered_narration_count,
            "audio_recovery_applied": bool(audio_level_info.get("applied")),
            "audio_recovery_gain_db": float(audio_level_info.get("gain_db") or 0.0),
            "final_audio_mean_volume_db": final_audio_mean_volume_db,
            "final_audio_max_volume_db": final_audio_max_volume_db,
            "narration_reference_nonsilent_seconds": narration_reference_nonsilent_seconds,
            "mixed_audio_nonsilent_seconds": mixed_nonsilent_seconds,
            "narration_mix_guard_applied": narration_mix_guard_applied,
        }


async def assemble(session_id: str) -> None:
    db = firestore.AsyncClient(project=PROJECT, database=FIRESTORE_DATABASE)
    doc_ref, initial_data = await _load_storyteller_session_document(db, session_id)

    async def _render_callback(workflow_state: dict[str, Any], _attempt: int) -> dict[str, Any]:
        runtime_overrides = dict(workflow_state.get("video_assembly_runtime_overrides") or {})
        try:
            report = await _assemble_pipeline(session_id, runtime_overrides=runtime_overrides)
        except Exception as exc:
            _latest_doc_ref, latest_data = await _load_storyteller_session_document(db, session_id)
            return _build_video_assembly_render_report(
                session_id,
                data=latest_data,
                runtime_overrides=runtime_overrides,
                error_text=str(exc),
            )
        _latest_doc_ref, latest_data = await _load_storyteller_session_document(db, session_id)
        persisted_report = _build_video_assembly_render_report(
            session_id,
            data=latest_data,
            runtime_overrides=runtime_overrides,
        )
        persisted_report.update(
            {
                key: value
                for key, value in report.items()
                if key not in {"issues", "reason", "retryable", "ready_to_publish"}
            }
        )
        if report.get("issues"):
            persisted_report["issues"] = list(report["issues"])
        if report.get("reason"):
            persisted_report["reason"] = str(report["reason"])
        if "retryable" in report:
            persisted_report["retryable"] = bool(report["retryable"])
        if "ready_to_publish" in report:
            persisted_report["ready_to_publish"] = bool(report["ready_to_publish"])
        return persisted_report

    workflow_state = await run_storybook_video_assembly_workflow(
        session_id=session_id,
        initial_state=_build_video_assembly_initial_state(session_id, initial_data),
        render_callback=_render_callback,
        max_render_attempts=max(1, min(int(os.environ.get("STORYBOOK_VIDEO_ASSEMBLY_MAX_ATTEMPTS", "2") or 2), 3)),
    )
    summary = build_storybook_video_assembly_summary(workflow_state)
    await doc_ref.set(
        {
            "video_assembly_director": summary,
            "assembly_workflow_source": summary.get("source"),
        },
        merge=True,
    )
    if not bool(summary.get("ready_to_publish")):
        raise RuntimeError(
            str(summary.get("reason") or "The ADK video assembly workflow did not approve the final movie.")
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble the final story movie.")
    parser.add_argument("--session_id", required=True, help="ADK session ID")
    args = parser.parse_args()
    asyncio.run(assemble(args.session_id))


if __name__ == "__main__":
    main()
