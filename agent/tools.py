"""All native ADK async tools for the Interactive Storyteller.

Every tool is an async Python function available to the agent.
"""

from __future__ import annotations

import asyncio
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
import json
import logging
import os
import ipaddress
import re
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
import uuid
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import tenacity
from google import genai as google_genai
from google.adk.tools import ToolContext
from google.cloud import firestore, storage
from pydantic import BaseModel, ConfigDict, Field
from shared.meta_learning import build_principles_injection_text, record_prompt_feedback
from shared.storybook_assembly_workflow import (
    build_storyboard_report_from_workflow_state,
    run_storybook_director_workflow,
)
from shared.storybook_movie_quality import (
    BURNED_CAPTIONS_DEFAULT,
    MUSIC_VOLUME_MAX,
    PAGE_SECONDS_DEFAULT,
    PAGE_SECONDS_NARRATION_BUFFER,
    SFX_VOLUME_MAX,
    StoryboardShotPlan,
    child_age_band,
    clamp_music_volume,
    clamp_narration_volume,
    clamp_page_seconds,
    clamp_sfx_cooldown_pages,
    clamp_sfx_max,
    clamp_sfx_volume,
    motion_profile,
    motion_timing,
    narration_required_default,
    narration_max_words_for_age,
    plan_storyboard_shots,
    storybook_release_gate,
)
from shared.story_continuity import (
    ensure_story_continuity_state,
    record_continuity_scene,
    should_render_new_scene_page,
    validate_live_scene_request,
)
from shared.storybook_pages import story_pages_from_state_data
from shared.storybook_studio_workflow import (
    build_storybook_studio_plan_from_workflow_state,
    build_storybook_studio_summary,
    run_storybook_studio_workflow,
)

from backend.event_bus import get_session_iot_config, publish_session_event, schedule_background_task
from backend.media_cache import store_media
from backend.schemas import theater_mode_event

logger = logging.getLogger(__name__)

_still_semaphore = asyncio.Semaphore(1)
_veo_semaphore = asyncio.Semaphore(1)
_last_light_call_by_session: dict[str, float] = {}
_LIGHT_COOLDOWN_SECONDS = 5.0
_assembled_sessions: set[str] = set()
_card_generating: set[str] = set()
_session_generating: set[str] = set()
_storybook_state_cache: dict[str, dict[str, Any]] = {}
# Per-session latest pending request: if a new scene arrives while an image is
# generating, queue the latest and suppress publishing the stale image.
_session_pending: dict[str, "VisualArgs"] = {}
_session_cancel_current: set[str] = set()
_session_last_gen_at: dict[str, float] = {}
_session_last_gen_text: dict[str, str] = {}
_session_image_backoff_until: dict[str, float] = {}
_CTRL_TOKEN_RE = re.compile(r"<ctrl\\d+>", flags=re.IGNORECASE)
_DEFAULT_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
_ALLOWED_STORY_TONES = {"cozy", "gentle_spooky", "adventure_spooky"}
_DEFAULT_VERTEX_IMAGE_MODEL = "gemini-2.5-flash-image"
_DEFAULT_API_KEY_IMAGE_MODEL = "gemini-3.1-flash-image-preview"


class _SupersededSceneRequest(RuntimeError):
    """Raised when a newer scene request should replace the current render immediately."""


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
    if normalized in _ALLOWED_STORY_TONES:
        return normalized
    return "cozy"


def _story_tone_from_state(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return "cozy"
    return _normalize_story_tone(state.get("story_tone"))


def _visual_negative_prompt_for_tone(story_tone: str) -> str:
    tone = _normalize_story_tone(story_tone)
    base = (
        "premium children's storybook illustration, age 4 safe, no text, no words, no letters, "
        "no labels, no signatures, no watermarks, no captions, no writing"
    )
    if tone == "gentle_spooky":
        return (
            f"{base}, gentle spooky but safe, no horror, no realistic menace, no jump scares, "
            "no violence, no injury, no blood, no gore, no panic, no grotesque faces, "
            "no sharp threatening monster closeups, no overwhelming darkness"
        )
    if tone == "adventure_spooky":
        return (
            f"{base}, preschool fantasy adventure only, no horror, no realistic menace, no jump scares, "
            "no violence, no injury, no blood, no gore, no screaming panic, no nightmare imagery, "
            "no grotesque monsters, no hopeless darkness"
        )
    return (
        f"{base}, U-rated Disney/Pixar warmth, strictly non-threatening, no monsters, "
        "no scary creatures, no dark themes, nothing frightening to a 4-year-old"
    )


def _visual_tone_guidance(story_tone: str) -> str:
    tone = _normalize_story_tone(story_tone)
    if tone == "gentle_spooky":
        return (
            "Tone: gentle spooky preschool adventure with moonlit wonder, cozy mystery, "
            "friendly or silly spooky details, and clear visible safety."
        )
    if tone == "adventure_spooky":
        return (
            "Tone: brave preschool fantasy adventure with dramatic mystery, magical suspense, "
            "bold but safe discovery, and an overall hopeful feeling."
        )
    return "Tone: fully cozy, bright, warm, and never spooky."


def _merge_visual_negative_prompt(story_tone: str, extra_negative_prompt: str) -> str:
    base = _visual_negative_prompt_for_tone(story_tone)
    extra = str(extra_negative_prompt or "").strip()
    if not extra:
        return base
    return f"{base}; {extra}"


class VisualArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str = Field(..., description="Positive scene description for image and video generation.")
    base_description: str | None = Field(
        default=None,
        description="Short, continuity-free scene description for fast first-pass generation.",
    )
    negative_prompt: str = Field(
        default="aesthetic: U-rated Disney/Pixar, STRICTLY non-threatening, no monsters, no scary creatures, no dark themes, nothing frightening to a 4-year-old, no text, no words, no letters, no labels, no signatures, no watermarks, no quotes, no captions, no writing",
        description="Single high-weight safety style instruction.",
    )
    aspect_ratio: str = Field(default="16:9", description="Desired image aspect ratio.")
    image_size: str = Field(default="512px", description="Image resolution preset.")
    image_model: str = Field(default="gemini-3.1-flash-image-preview", description="Image model id.")
    reference_images: list[dict[str, str]] = Field(
        default_factory=list,
        description="Ordered reference images with role metadata for continuity.",
    )
    illustration_style: str | None = Field(default=None, description="The chosen illustration style for this session.")
    request_id: str | None = Field(default=None, description="Stable id joining placeholder and final image for one scene request.")
    delivery_format: str = Field(default="jpeg", description="Client transport format for the first still.")
    delivery_quality: int = Field(default=72, description="Client transport encoding quality.")
    delivery_max_side: int | None = Field(default=None, description="Optional max image side for the first still delivered to the browser.")
    quota_retry_count: int = Field(default=0, description="How many quota/backpressure relaunches have already been attempted.")

CLASSIC_STORYBOOK_STYLES = [
    "Ultra-consistent, high-quality children's storybook illustration. Watercolor and ink, reminiscent of Beatrix Potter or E.H. Shepard. Soft pastels, gentle lighting, timeless and nostalgic.",
    "Ultra-consistent, high-quality children's storybook illustration. Dreamy atmospheric watercolor, reminiscent of Jon Klassen. Muted earthy tones, soft textures, minimalist and elegant.",
    "Ultra-consistent, high-quality children's storybook illustration. Classic Golden Books style, vintage 1950s children's book aesthetic. Gouache, warm bright colors, whimsical and cozy.",
    "Ultra-consistent, high-quality children's storybook illustration. Textured paper collage aesthetic, reminiscent of Eric Carle. Vibrant saturated colors, bold playful shapes, highly textured.",
    "Ultra-consistent, high-quality children's storybook illustration. Whimsical pen and wash, reminiscent of Quentin Blake. Energetic loose line work, expressive and joyful, colorful loose watercolor washes."
]


class MusicArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mood: str = Field(..., description="happy, suspenseful, magical, triumphant")
    intensity: int = Field(default=5, ge=1, le=10)


class CharacterFactArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    character_name: str
    fact: str


class ChildNameArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(..., description="Child's preferred first name")


_NAME_TOOL_CTRL_TOKEN_RE = re.compile(r"<ctrl\d+>", flags=re.IGNORECASE)
_NAME_TOOL_PHRASE_RE = re.compile(
    r"(?:\bmy name(?: is|'s|’s)\b|\bi am\b|\bi['’]m\b)\s+(?:(?:uh|um|erm|uhh|umm)\s+)?([A-Za-z][A-Za-z'\-]{1,23})\b",
    flags=re.IGNORECASE,
)
_NAME_TOOL_REJECT_WORDS = {
    "a", "an", "and", "are", "at", "be", "can", "cool", "dad", "do", "does", "done",
    "friend", "go", "good", "hello", "hey", "hi", "i", "im", "i'm", "is", "it",
    "its", "it's", "let", "me", "mom", "my", "name", "no", "not", "now", "ok",
    "okay", "one", "please", "right", "say", "she", "start", "stop", "sure", "that",
    "the", "there", "this", "uh", "um", "we", "well", "what", "who", "why", "will",
    "yes", "you", "your", "here", "their", "they", "them", "he", "her", "him",
}


def _extract_recent_child_name_candidate(text: Any) -> str | None:
    cleaned = _NAME_TOOL_CTRL_TOKEN_RE.sub("", str(text or "")).strip()
    if not cleaned:
        return None

    match = _NAME_TOOL_PHRASE_RE.search(cleaned)
    if match:
        candidate = match.group(1).strip(" .,!?:;").title()
        if candidate.lower() in _NAME_TOOL_REJECT_WORDS:
            return None
        return candidate

    token_match = re.fullmatch(r"[A-Za-z][A-Za-z'\-]{1,23}", cleaned)
    if token_match:
        candidate = token_match.group(0).title()
        if candidate.lower() in _NAME_TOOL_REJECT_WORDS:
            return None
        return candidate

    return None


class AssembleArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: str


class LightArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hex_color: str
    scene_description: str = ""


def _session_id_from_context(tool_context: ToolContext | None) -> str | None:
    if tool_context is None:
        return None
    try:
        return tool_context.session.id
    except Exception:
        return None


def _story_end_progress(tool_context: ToolContext | None) -> tuple[bool, int, int, int]:
    """Return whether final-story tools are allowed plus simple turn progress."""
    state = _load_tool_state(tool_context)

    try:
        max_turns = int(state.get("max_story_turns", os.environ.get("MAX_STORY_TURNS", "20")) or 20)
    except (TypeError, ValueError):
        max_turns = 20
    max_turns = max(1, min(max_turns, 20))

    try:
        current_turn = int(state.get("turn_number", 1) or 1)
    except (TypeError, ValueError):
        current_turn = 1
    current_turn = max(1, min(current_turn, max_turns))

    limit_reached = bool(state.get("story_turn_limit_reached", False)) or current_turn >= max_turns
    turns_remaining = max(max_turns - current_turn, 0)
    return limit_reached, current_turn, max_turns, turns_remaining


def _normalize_hex_color(raw_hex: str) -> str:
    candidate = raw_hex.strip()
    if not candidate.startswith("#"):
        candidate = f"#{candidate}"
    hex_clean = candidate[1:]
    if not re.fullmatch(r"[0-9a-fA-F]{6}", hex_clean):
        raise ValueError("Invalid color format. Use #RRGGBB.")
    return f"#{hex_clean.upper()}"


def _rgb_from_hex(hex_color: str) -> tuple[int, int, int]:
    hex_clean = hex_color.lstrip("#")
    return (
        int(hex_clean[0:2], 16),
        int(hex_clean[2:4], 16),
        int(hex_clean[4:6], 16),
    )


def _session_light_cooldown_key(session_id: str | None) -> str:
    return session_id or "__global__"


def _ha_url_is_private_or_local(ha_url: str) -> bool:
    parsed = urlparse(ha_url.strip())
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False

    if (
        host == "localhost"
        or host == "127.0.0.1"
        or host == "::1"
        or host.endswith(".local")
        or host.endswith(".lan")
        or host.endswith(".home")
        or host.endswith(".internal")
        or "." not in host
    ):
        return True

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False

    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved


def _remember_last_light_color(tool_context: ToolContext | None, session_id: str | None, hex_color: str) -> None:
    if tool_context is not None:
        try:
            tool_context.state["last_active_hex_color"] = hex_color
        except Exception:
            pass

    if session_id:
        cached = dict(_storybook_state_cache.get(session_id) or {})
        cached["last_active_hex_color"] = hex_color
        cache_storybook_state(session_id, cached)


def _publish_lighting_command(session_id: str, payload: dict[str, Any]) -> None:
    publish_session_event(
        session_id,
        {
            "type": "lighting_command",
            "payload": payload,
        },
    )


def _load_tool_state(tool_context: ToolContext | None) -> dict[str, Any]:
    raw = getattr(tool_context, "state", None) if tool_context else None
    if raw is None:
        return {}
    if hasattr(raw, "to_dict"):
        try:
            state = dict(raw.to_dict() or {})
            ensure_story_continuity_state(state)
            return state
        except Exception:
            return {}
    if hasattr(raw, "_value"):
        try:
            state = dict(getattr(raw, "_value", {}) or {})
            ensure_story_continuity_state(state)
            return state
        except Exception:
            return {}
    if isinstance(raw, dict):
        state = dict(raw)
        ensure_story_continuity_state(state)
        return state
    try:
        state = dict(raw)
        ensure_story_continuity_state(state)
        return state
    except Exception:
        return {}


def _scene_tool_turn_is_open(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    return bool(
        state.get(
            "scene_tool_turn_open",
            state.get("pending_response", False),
        )
    )


def _pending_response_token(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return ""
    return str(state.get("pending_response_token", "") or "").strip()


def _pending_child_utterance(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return ""
    return str(
        state.get("last_child_utterance")
        or state.get("partial_child_utterance")
        or ""
    ).strip()


def _stale_turn_tool_call(session_id: str, tool_context: ToolContext | None) -> bool:
    if not session_id:
        return False
    live_state = _load_tool_state(tool_context)
    latest_state = load_storybook_resume_state(session_id)
    if _scene_tool_turn_is_open(latest_state):
        return False
    if _scene_tool_turn_is_open(live_state):
        if not latest_state:
            return False
        live_token = _pending_response_token(live_state)
        latest_token = _pending_response_token(latest_state)
        if live_token and latest_token and live_token != latest_token:
            return False
        live_child = _pending_child_utterance(live_state)
        latest_child = _pending_child_utterance(latest_state)
        if live_child and latest_child and live_child != latest_child:
            return False
        return True
    if not latest_state and not live_state:
        return False
    return True


def _is_resource_exhausted_error(exc: Exception) -> bool:
    text = str(exc or "").upper()
    return "RESOURCE_EXHAUSTED" in text or " 429 " in f" {text} "


def _queued_newer_scene_request(session_id: str | None, current_request_id: str | None) -> bool:
    if not session_id:
        return False
    pending = _session_pending.get(session_id)
    if pending is None:
        return False
    current = str(current_request_id or "").strip()
    pending_request_id = str(getattr(pending, "request_id", "") or "").strip()
    if not current:
        return True
    if not pending_request_id:
        return True
    return pending_request_id != current


def cache_storybook_state(session_id: str, state: dict[str, Any]) -> None:
    if not session_id:
        return
    cached = dict(state or {})
    ensure_story_continuity_state(cached)
    _storybook_state_cache[session_id] = cached


def load_storybook_resume_state(session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    merged: dict[str, Any] = {}
    firestore_state = _load_storybook_firestore_state(session_id)
    if firestore_state:
        merged.update(firestore_state)
    cached_state = _storybook_state_cache.get(session_id) or {}
    if cached_state:
        merged.update(cached_state)
    ensure_story_continuity_state(merged)
    return merged


def _story_pages_from_state(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    return story_pages_from_state_data(state if isinstance(state, dict) else {})


def _sync_story_pages_in_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    pages = _story_pages_from_state(state)
    state["story_pages"] = pages
    state["scene_asset_urls"] = [
        str(page.get("image_url", "") or "").strip()
        for page in pages
        if str(page.get("image_url", "") or "").strip()
    ][-40:]
    state["scene_asset_gcs_uris"] = [
        str(page.get("gcs_uri", "") or "").strip()
        for page in pages
        if str(page.get("gcs_uri", "") or "").strip()
    ][-40:]
    state["scene_descriptions"] = [
        str(page.get("scene_description", "") or "").strip()
        for page in pages
        if str(page.get("scene_description", "") or "").strip()
    ][-40:]
    state["scene_storybeat_texts"] = [
        str(page.get("storybeat_text", "") or "").strip()
        for page in pages
        if str(page.get("storybeat_text", "") or "").strip()
    ][-40:]
    return pages


def _storybook_scene_descriptions(state: dict[str, Any]) -> list[str]:
    pages = _story_pages_from_state(state)
    if pages:
        return [
            str(page.get("scene_description", "") or "").strip()
            or str(page.get("storybeat_text", "") or "").strip()
            for page in pages
            if str(page.get("scene_description", "") or "").strip()
            or str(page.get("storybeat_text", "") or "").strip()
        ]
    return [
        str(item).strip()
        for item in list(state.get("scene_descriptions", []) or [])
        if str(item).strip()
    ]


def reset_storybook_assembly_lock(session_id: str) -> None:
    if not session_id:
        return
    _assembled_sessions.discard(session_id)


def _should_use_fast_storybook_assembly() -> bool:
    if not _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False):
        return False
    if _env_enabled("ENABLE_VEO_FINAL", default=False):
        return False
    return shutil.which("ffmpeg") is not None


def _continuity_anchor_text(tool_context: ToolContext | None) -> str:
    """Builds compact continuity anchors to keep visuals temporally consistent."""
    if tool_context is None:
        return ""
    try:
        state = dict(getattr(tool_context, "state", {}) or {})
    except Exception:
        return ""

    anchors: list[str] = []
    child_name = str(state.get("child_name", "")).strip()
    story_summary = str(state.get("story_summary", "")).strip()
    character_facts = str(state.get("character_facts", "")).strip()
    canonical_scene_description = str(state.get("canonical_scene_description", "")).strip()
    canonical_scene_visual_summary = str(state.get("canonical_scene_visual_summary", "")).strip()
    previous_scene_description = str(state.get("previous_scene_base_description", "")).strip()
    if not previous_scene_description:
        previous_scene_description = str(state.get("previous_scene_description", "")).strip()
    previous_scene_visual_summary = str(state.get("previous_scene_visual_summary", "")).strip()
    current_scene_base_description = str(state.get("current_scene_base_description", "")).strip()
    current_scene_visual_summary = str(state.get("current_scene_visual_summary", "")).strip()
    scene_descriptions_raw = list(state.get("scene_descriptions", []) or [])
    recent_scene_descriptions = [
        str(item).strip()
        for item in scene_descriptions_raw[-3:]
        if str(item).strip()
    ]

    # Intentionally omit child name from visual anchors to avoid image model refusals.
    if character_facts:
        anchors.append(
            "canonical character facts: "
            + character_facts.replace("\n", "; ").strip()[:260]
        )
    if canonical_scene_description:
        anchors.append(
            "canonical recurring setting look: "
            + canonical_scene_description.replace("\n", " ").strip()[-260:]
        )
    if canonical_scene_visual_summary:
        anchors.append(
            "canonical visual continuity anchor: "
            + canonical_scene_visual_summary.replace("\n", " ").strip()[-220:]
        )
    if story_summary:
        anchors.append(
            "recent story context: " + story_summary.replace("\n", " ").strip()[-340:]
        )
    if previous_scene_description:
        anchors.append(
            "carry-over from last scene: "
            + previous_scene_description.replace("\n", " ").strip()[-220:]
        )
    if previous_scene_visual_summary:
        anchors.append(
            "last image actually showed: "
            + previous_scene_visual_summary.replace("\n", " ").strip()[-220:]
        )
    if current_scene_base_description:
        anchors.append(
            "current visible scene anchor: "
            + current_scene_base_description.replace("\n", " ").strip()[-220:]
        )
    if current_scene_visual_summary:
        anchors.append(
            "current image actually shows: "
            + current_scene_visual_summary.replace("\n", " ").strip()[-220:]
        )
    if recent_scene_descriptions:
        anchors.append(
            "recent visual history: "
            + " | ".join(recent_scene_descriptions).replace("\n", " ").strip()[-360:]
        )

    return "; ".join(anchors)


_ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
}
_ALLOWED_IMAGE_SIZES = {"512px", "1K", "2K", "4K"}


def _resolve_image_preferences(tool_context: ToolContext | None) -> tuple[str, str, str]:
    state = {}
    if tool_context:
        try:
            state = dict(getattr(tool_context, "state", {}) or {})
        except Exception:
            state = {}

    aspect_ratio = str(state.get("preferred_aspect_ratio", "16:9"))
    if aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
        aspect_ratio = "16:9"

    image_size = os.environ.get("IMAGE_SIZE", "").strip() or str(
        state.get("preferred_image_size", "512px")
    )
    if image_size not in _ALLOWED_IMAGE_SIZES:
        image_size = "512px"

    image_model = os.environ.get("IMAGE_MODEL", "").strip() or _default_live_image_model()
    if not _is_supported_image_generation_model(image_model):
        image_model = _default_live_image_model()

    return aspect_ratio, image_size, image_model


def _using_vertex_ai_backend() -> bool:
    return os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().upper() == "TRUE"


def _default_live_image_model() -> str:
    return _DEFAULT_VERTEX_IMAGE_MODEL if _using_vertex_ai_backend() else _DEFAULT_API_KEY_IMAGE_MODEL


def _is_supported_image_generation_model(model_name: str) -> bool:
    normalized = str(model_name or "").strip().lower()
    return any(token in normalized for token in ("flash-image", "image-generation", "pro-image"))


def _build_google_genai_client() -> google_genai.Client:
    if _using_vertex_ai_backend():
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip() or "us-central1"
        if not project:
            raise ValueError("Missing GOOGLE_CLOUD_PROJECT for Vertex AI image generation.")
        return google_genai.Client(vertexai=True, project=project, location=location)

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Missing GOOGLE_API_KEY for image generation.")
    return google_genai.Client(api_key=api_key)


def _resolve_delivery_preferences(state: dict[str, Any], image_size: str) -> tuple[str, int, int | None]:
    device_profile = state.get("device_profile", {})
    network_profile = state.get("network_profile", {})
    if not isinstance(device_profile, dict):
        device_profile = {}
    if not isinstance(network_profile, dict):
        network_profile = {}

    is_compact = bool(device_profile.get("is_compact", False))
    effective_type = str(network_profile.get("effective_type", "") or "").strip().lower()
    save_data = bool(network_profile.get("save_data", False))
    try:
        downlink_mbps = float(network_profile.get("downlink_mbps", 0) or 0)
    except Exception:
        downlink_mbps = 0.0

    very_slow = save_data or effective_type in {"slow-2g", "2g"} or (downlink_mbps > 0 and downlink_mbps < 1.2)
    constrained = effective_type == "3g" or (downlink_mbps > 0 and downlink_mbps < 2.5)

    if is_compact and very_slow:
        return "webp", 42, 320
    if is_compact and constrained:
        return "webp", 48, 384
    if is_compact:
        return "webp", 56, 448
    if very_slow:
        return "webp", 50, 448
    if constrained or image_size == "512px":
        return "webp", 58, 576
    return "jpeg", 72, None


def _resolve_continuity_mode() -> str:
    raw = os.environ.get("IMAGE_CONTINUITY_MODE", "thumbnail").strip().lower()
    if raw in {"thumbnail", "text", "off"}:
        return "text" if raw == "off" else raw
    return "thumbnail"


def _make_thumbnail_b64(image_bytes: bytes, max_side: int = 384) -> tuple[str, str] | None:
    try:
        from PIL import Image
    except Exception:
        return None


def _scene_continuity_vision_model() -> str:
    return (
        os.environ.get("SCENE_CONTINUITY_VISION_MODEL", "").strip()
        or os.environ.get("STORYBOOK_SCENE_REVIEW_MODEL", "").strip()
        or os.environ.get("STORYBOOK_POST_MOVIE_REVIEW_MODEL", "").strip()
        or "gemini-2.5-flash"
    )


async def _describe_scene_image_for_continuity(image_bytes: bytes) -> str:
    if not image_bytes:
        return ""

    vision_bytes = image_bytes
    vision_mime = _sniff_mime_type(image_bytes)
    thumb = _make_thumbnail_b64(image_bytes, max_side=768)
    if thumb:
        try:
            vision_bytes = base64.b64decode(thumb[0])
            vision_mime = thumb[1] or "image/jpeg"
        except Exception:
            vision_bytes = image_bytes
            vision_mime = _sniff_mime_type(image_bytes)

    prompt = (
        "Describe only the visible continuity anchors in this story image. "
        "Use one short sentence under 40 words. "
        "Say whether it looks indoors or outdoors if obvious, name the main place or room, "
        "and mention 2 to 4 clearly visible objects or focal details. "
        "Do not guess unseen story details."
    )

    def _run() -> str:
        client = _build_google_genai_client()
        response = client.models.generate_content(
            model=_scene_continuity_vision_model(),
            contents=[
                prompt,
                google_genai.types.Part.from_bytes(
                    data=vision_bytes,
                    mime_type=vision_mime,
                ),
            ],
            config=google_genai.types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=90,
            ),
        )
        return _extract_response_text(response).strip().strip("\"'")[:220]

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warning("Scene continuity vision summary failed: %s", exc)
        return ""

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = BytesIO()
            img.save(out, format="JPEG", quality=70, optimize=True, progressive=True)
            b64 = base64.b64encode(out.getvalue()).decode("ascii")
            return b64, "image/jpeg"
    except Exception:
        return None


def _encode_transport_image(
    image_bytes: bytes,
    *,
    fmt: str,
    quality: int,
    max_side: int | None,
) -> tuple[bytes, str]:
    try:
        from PIL import Image
    except Exception:
        return image_bytes, _sniff_mime_type(image_bytes)

    normalized_format = str(fmt or "jpeg").strip().lower()
    target_quality = max(35, min(int(quality or 72), 90))
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            if max_side and max(img.size) > max_side:
                img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = BytesIO()
            if normalized_format == "webp":
                img.save(out, format="WEBP", quality=target_quality, method=6)
                return out.getvalue(), "image/webp"
            img.save(out, format="JPEG", quality=target_quality, optimize=True, progressive=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, _sniff_mime_type(image_bytes)


def _encode_storage_image(image_bytes: bytes) -> tuple[bytes, str]:
    """Prepare a storage-friendly still without blocking first paint on the browser."""
    try:
        from PIL import Image
    except Exception:
        return image_bytes, _sniff_mime_type(image_bytes)

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            out = BytesIO()
            img.save(out, format="JPEG", quality=80, optimize=True, progressive=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return image_bytes, _sniff_mime_type(image_bytes)


def _persist_uploaded_scene_asset(
    *,
    session_id: str,
    description: str,
    storybeat_text: str,
    scene_visual_summary: str,
    cloud_still_url: str | None,
    gcs_uri: str | None,
    thumbnail_b64: str | None,
    thumbnail_mime: str | None,
    request_id: str | None = None,
    preview_image_url: str | None = None,
) -> None:
    if not session_id:
        return

    state = load_storybook_resume_state(session_id)
    ensure_story_continuity_state(state)
    pages = _story_pages_from_state(state)
    normalized_request_id = str(request_id or "").strip()
    target_index = -1
    if normalized_request_id:
        for idx, page in enumerate(pages):
            if str(page.get("request_id", "") or "").strip() == normalized_request_id:
                target_index = idx
                break
    if target_index < 0 and normalized_request_id:
        target_index = len(pages)
        pages.append(
            {
                "scene_number": target_index + 1,
                "request_id": normalized_request_id,
                "scene_description": "",
                "storybeat_text": "",
                "image_url": "",
                "gcs_uri": "",
            }
        )
    elif target_index < 0 and pages:
        target_index = len(pages) - 1
    if target_index < 0:
        target_index = len(pages)
        pages.append(
            {
                "scene_number": target_index + 1,
                "request_id": normalized_request_id,
                "scene_description": "",
                "storybeat_text": "",
                "image_url": "",
                "gcs_uri": "",
            }
        )

    target_page = dict(pages[target_index])
    if normalized_request_id:
        target_page["request_id"] = normalized_request_id
    if description:
        target_page["scene_description"] = description
    if storybeat_text:
        target_page["storybeat_text"] = storybeat_text
    if gcs_uri:
        target_page["gcs_uri"] = gcs_uri
    if cloud_still_url:
        target_page["image_url"] = cloud_still_url
    elif preview_image_url and not str(target_page.get("image_url", "") or "").strip():
        target_page["image_url"] = preview_image_url
    pages[target_index] = target_page

    branch_points = list(state.get("scene_branch_points", []) or [])
    branch_target_index = -1
    if normalized_request_id:
        for idx, point in enumerate(branch_points):
            if str(point.get("request_id", "") or "").strip() == normalized_request_id:
                branch_target_index = idx
                break
    if branch_target_index < 0 and normalized_request_id:
        scene_number = max(1, int(target_page.get("scene_number", target_index + 1) or (target_index + 1)))
        branch_points.append(
            {
                "scene_number": scene_number,
                "request_id": normalized_request_id,
                "label": storybeat_text or description or f"Scene {scene_number}",
                "scene_description": description,
                "storybeat_text": storybeat_text,
                "image_url": cloud_still_url or preview_image_url or "",
                "gcs_uri": gcs_uri or "",
            }
        )
        branch_target_index = len(branch_points) - 1
    elif branch_target_index < 0 and branch_points:
        branch_target_index = min(target_index, len(branch_points) - 1)
    if branch_target_index >= 0:
        branch_point = dict(branch_points[branch_target_index])
        if description:
            branch_point["scene_description"] = description
        if storybeat_text:
            branch_point["storybeat_text"] = storybeat_text
            branch_point["label"] = storybeat_text
        elif description and not str(branch_point.get("label", "") or "").strip():
            branch_point["label"] = description
        if normalized_request_id:
            branch_point["request_id"] = normalized_request_id
        if gcs_uri:
            branch_point["gcs_uri"] = gcs_uri
        if cloud_still_url:
            branch_point["image_url"] = cloud_still_url
        elif preview_image_url and not str(branch_point.get("image_url", "") or "").strip():
            branch_point["image_url"] = preview_image_url
        branch_points[branch_target_index] = branch_point

    updated_state = {
        **state,
        "story_pages": pages[-40:],
        "scene_branch_points": branch_points[-20:] if branch_points else branch_points,
    }
    _sync_story_pages_in_state(updated_state)

    active_request_id = str(updated_state.get("active_scene_request_id", "") or "").strip()
    is_current_scene_request = not normalized_request_id or not active_request_id or normalized_request_id == active_request_id
    if description and is_current_scene_request:
        updated_state["current_scene_description"] = description
        if not str(updated_state.get("canonical_scene_description", "") or "").strip():
            updated_state["canonical_scene_description"] = description
    if storybeat_text and is_current_scene_request:
        updated_state["current_scene_storybeat_text"] = storybeat_text
        if not str(updated_state.get("canonical_scene_storybeat_text", "") or "").strip():
            updated_state["canonical_scene_storybeat_text"] = storybeat_text
    if scene_visual_summary and is_current_scene_request:
        updated_state["current_scene_visual_summary"] = scene_visual_summary
    if scene_visual_summary:
        updated_state["previous_scene_visual_summary"] = scene_visual_summary
        if not str(updated_state.get("canonical_scene_visual_summary", "") or "").strip():
            updated_state["canonical_scene_visual_summary"] = scene_visual_summary
    if thumbnail_b64:
        updated_state["previous_scene_thumbnail_b64"] = thumbnail_b64
        updated_state["previous_scene_thumbnail_mime"] = thumbnail_mime or "image/jpeg"
        if not str(updated_state.get("canonical_scene_thumbnail_b64", "") or "").strip():
            updated_state["canonical_scene_thumbnail_b64"] = thumbnail_b64
            updated_state["canonical_scene_thumbnail_mime"] = thumbnail_mime or "image/jpeg"

    record_continuity_scene(
        updated_state,
        description=description,
        storybeat_text=storybeat_text,
        visual_summary=scene_visual_summary,
        request_id=normalized_request_id,
        scene_number=max(1, int(target_page.get("scene_number", target_index + 1) or (target_index + 1))),
    )

    cache_storybook_state(session_id, updated_state)
    _update_storybook_firestore(
        session_id,
        {
            "story_pages": list(updated_state.get("story_pages", []) or []),
            "scene_asset_urls": list(updated_state.get("scene_asset_urls", []) or []),
            "scene_asset_gcs_uris": list(updated_state.get("scene_asset_gcs_uris", []) or []),
            "scene_descriptions": list(updated_state.get("scene_descriptions", []) or []),
            "scene_storybeat_texts": list(updated_state.get("scene_storybeat_texts", []) or []),
            "current_scene_description": str(updated_state.get("current_scene_description", "") or "").strip(),
            "current_scene_storybeat_text": str(updated_state.get("current_scene_storybeat_text", "") or "").strip(),
            "current_scene_visual_summary": str(updated_state.get("current_scene_visual_summary", "") or "").strip(),
            "canonical_scene_description": str(updated_state.get("canonical_scene_description", "") or "").strip(),
            "canonical_scene_storybeat_text": str(updated_state.get("canonical_scene_storybeat_text", "") or "").strip(),
            "canonical_scene_visual_summary": str(updated_state.get("canonical_scene_visual_summary", "") or "").strip(),
            "canonical_scene_thumbnail_b64": str(updated_state.get("canonical_scene_thumbnail_b64", "") or "").strip(),
            "canonical_scene_thumbnail_mime": str(updated_state.get("canonical_scene_thumbnail_mime", "") or "").strip(),
        },
    )


def _response_parts(response: Any) -> list[Any]:
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        try:
            return list(candidates[0].content.parts)
        except Exception:
            return []
    return list(getattr(response, "parts", []) or [])


def _extract_image_blob(image_response: Any) -> tuple[bytes, str] | None:
    parts = _response_parts(image_response)

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data, getattr(inline, "mime_type", "image/png")

    # Log what we got for debugging
    prompt_feedback = getattr(image_response, "prompt_feedback", None)
    if prompt_feedback:
        logger.warning("Image prompt_feedback: %s", prompt_feedback)
    logger.warning(
        "_extract_image_blob: no inline_data found. Parts count=%d, part types=%s",
        len(parts),
        [type(p).__name__ for p in parts],
    )
    for i, part in enumerate(parts):
        attrs = [a for a in dir(part) if not a.startswith("_")]
        logger.warning("  Part[%d] attrs: %s", i, attrs)
        # Check for text part (model may have responded with text instead of image)
        if getattr(part, "text", None):
            logger.warning("  Part[%d] text: %s", i, str(part.text)[:200])
    return None


def _extract_storybeat_text(response: Any) -> str:
    parts = _response_parts(response)
    segments: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str) and text.strip():
            segments.append(text.strip())

    if not segments:
        return ""

    merged = re.sub(r"\s+", " ", " ".join(segments)).strip()
    merged = re.sub(r"^(caption|storybeat|scene)\s*:\s*", "", merged, flags=re.IGNORECASE)
    merged = merged.strip(" \"'")
    return merged[:220]


def _fallback_storybeat_text(description: str) -> str:
    text = re.sub(r"\s+", " ", description).strip()
    if not text:
        return "A magical new page appears in the storybook."
    text = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    text = text.strip(" \"'")
    if len(text) > 220:
        text = text[:217].rstrip(" ,;:") + "..."
    return text or "A magical new page appears in the storybook."


def _extract_first_uri(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in ("uri", "gcsUri", "videoUri", "video_url", "url"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
        for value in obj.values():
            found = _extract_first_uri(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _extract_first_uri(item)
            if found:
                return found
    return None


def _gcs_signed_url(gs_uri: str, project: str) -> str | None:
    # gs://bucket/path -> signed https URL
    from google.cloud import storage

    path = gs_uri[5:]
    bucket_name, blob_path = path.split("/", 1)
    client = storage.Client(project=project)
    blob = client.bucket(bucket_name).blob(blob_path)
    try:
        # On Cloud Run, we must provide the service_account_email to sign.
        # We can try to get it from the client or fallback to environment.
        sa_email = getattr(client, "service_account_email", None)
        if not sa_email:
             # Typical for appspot/compute default
             sa_email = f"{project}@appspot.gserviceaccount.com"
             
        return blob.generate_signed_url(
            version="v4", 
            expiration=timedelta(hours=2), 
            method="GET",
            service_account_email=sa_email
        )
    except Exception as exc:
        # Local/user ADC often cannot sign URLs. Return None so callers can use
        # a deterministic inline-data fallback rather than a broken 403 URL.
        logger.warning("Signed URL generation failed for gs://%s/%s: %s", bucket_name, blob_path, exc)
        return None


def _normalize_media_url(url_or_uri: str, project: str) -> str | None:
    if url_or_uri.startswith("gs://"):
        return _gcs_signed_url(url_or_uri, project)
    return url_or_uri


def _veo_enabled() -> bool:
    # Live story should stay fast. Gate Veo during live turns separately.
    return _env_enabled("ENABLE_VEO_LIVE", default=False)


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_fallback_scene_svg_data_url(description: str) -> str:
    safe_text = html.escape(description[:220] or "A magical story scene")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#1b0b3b"/>
      <stop offset="55%" stop-color="#3f1c73"/>
      <stop offset="100%" stop-color="#0b2d5b"/>
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="35%" r="45%">
      <stop offset="0%" stop-color="#ffd166" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="#ffd166" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <rect width="1280" height="720" fill="url(#glow)"/>
  <circle cx="260" cy="560" r="180" fill="#ff7fbe" opacity="0.5"/>
  <circle cx="630" cy="590" r="220" fill="#7cf8cf" opacity="0.45"/>
  <circle cx="1020" cy="550" r="170" fill="#7ec9ff" opacity="0.5"/>
</svg>"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _upload_scene_still(
    image_bytes: bytes,
    mime_type: str,
    project: str,
    session_id: str | None,
) -> tuple[str | None, str | None]:
    bucket_name = os.environ.get("GCS_ASSETS_BUCKET", "").strip()
    if not bucket_name:
        return None, None

    from google.cloud import storage

    scene_id = session_id or "anonymous"
    normalized_mime = str(mime_type or "image/jpeg").strip().lower()
    object_ext = ".webp" if normalized_mime == "image/webp" else ".jpg"
    object_path = f"{scene_id}/scene_stills/{int(time.time() * 1000)}{object_ext}"
    client = storage.Client(project=project)
    blob = client.bucket(bucket_name).blob(object_path)
    blob.upload_from_string(image_bytes, content_type=normalized_mime or "image/jpeg")
    gcs_uri = f"gs://{bucket_name}/{object_path}"
    display_url = _normalize_media_url(gcs_uri, project=project)
    return display_url, gcs_uri


def _generate_scene_still(
    description: str,
    negative_prompt: str,
    aspect_ratio: str,
    image_size: str,
    image_model: str,
    reference_images: list[dict[str, str]] | None = None,
    style_prompt: str | None = None,
) -> tuple[bytes, str, str]:
    client = _build_google_genai_client()
    meta_guidance = build_principles_injection_text("interactive_scene_visual")
    prompt = f"{description}\n\n"
    if meta_guidance:
        prompt += f"{meta_guidance}\n\n"
    if negative_prompt:
        prompt += f"Safety style: {negative_prompt}\n"

    if style_prompt:
        prompt += f"Style: {style_prompt} NO 3D rendering. NO Pixar style. Completely uniform art style, no text overlays."
    else:
        prompt += "Style: Ultra-consistent, high-quality children's storybook illustration. Rich, warm, hand-drawn 2D aesthetic (like watercolor or classic storybooks). NO 3D rendering. NO Pixar style. Completely uniform art style, no text overlays."
    prompt += (
        "\nComposition: make the scene feel magical, memorable, and special for a young child. "
        "Use a clear focal subject, layered foreground/midground/background, warm storybook lighting, "
        "and several distinctive props or landmarks that make the setting unmistakable."
    )
    prompt += (
        "\nStorybeat text: In the same response, include exactly one short child-friendly storybook caption "
        "that matches the illustration. Use one vivid present-tense sentence, maximum 22 words, with no markdown, labels, or quotation marks."
    )

    reference_images = list(reference_images or [])
    reference_roles = [str(item.get("role", "")).strip().lower() for item in reference_images if isinstance(item, dict)]
    has_setting_ref = "canonical_setting" in reference_roles
    has_recent_scene_ref = "previous_scene" in reference_roles
    has_toy_ref = "toy" in reference_roles

    if has_setting_ref or has_recent_scene_ref:
        prompt += (
            "\nContinuity rules: keep recurring characters, wardrobe, props, architecture, palette, "
            "lighting, and room/layout details consistent with the reference images. "
            "If the story is still in the same place, do not redesign the setting."
        )
    if has_setting_ref:
        prompt += "\nReference priority: the canonical-setting image is the stable world/look anchor when revisiting the same place."
    if has_recent_scene_ref:
        prompt += "\nReference priority: the recent-scene image is the immediate carry-over anchor for poses, props, and camera-world continuity."
    if has_toy_ref:
        prompt += "\nIf a toy reference is present, use it only for the sidekick's appearance and colors, not for replacing the whole setting."

    contents: list[Any] = [prompt]
    for ref in reference_images:
        try:
            ref_b64 = str(ref.get("b64", "") or "").strip()
            if not ref_b64:
                continue
            ref_bytes = base64.b64decode(ref_b64)
            contents.append(
                google_genai.types.Part.from_bytes(
                    data=ref_bytes,
                    mime_type=str(ref.get("mime", "") or "image/jpeg").strip() or "image/jpeg",
                    media_resolution=google_genai.types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW,
                )
            )
        except Exception:
            logger.warning("Failed to decode reference image for continuity; continuing without it.")

    image_config_kwargs: dict[str, Any] = {
        "aspect_ratio": aspect_ratio,
    }
    if not _using_vertex_ai_backend():
        image_config_kwargs["image_size"] = image_size

    response = client.models.generate_content(
        model=image_model,
        contents=contents,
        config=google_genai.types.GenerateContentConfig(
            response_modalities=[
                google_genai.types.Modality.TEXT,
                google_genai.types.Modality.IMAGE,
            ],
            # 16:9 reduces pixels vs 1:1 and is the main latency lever on Vertex here.
            image_config=google_genai.types.ImageConfig(**image_config_kwargs),
        ),
    )
    blob = _extract_image_blob(response)
    if not blob:
        # Log more detail about what the response actually contained
        logger.warning("Image response had no image blob. Response: %s", str(response)[:500])
        raise RuntimeError("Image model returned no image bytes.")
    storybeat_text = _extract_storybeat_text(response) or _fallback_storybeat_text(description)
    return blob[0], blob[1], storybeat_text


def _poll_veo_operation(project: str, location: str, operation_name: str, timeout_seconds: float = 120.0) -> str:
    import google.auth
    import google.auth.transport.requests

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
                uri = _extract_first_uri(payload.get("response") or payload)
                if uri:
                    return uri
                raise RuntimeError("Veo operation completed without a media URL.")
            time.sleep(2.0)

    raise TimeoutError("Veo operation timed out.")


async def generate_scene_visuals(
    description: str,
    negative_prompt: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """
    Generates a scene image and a short video clip in the background.

    Args:
        description: A purely visual description of the scene to draw. MUST NOT contain conversational text like 'I want to go to' or the child's name. Just visually describe the magical setting and characters.
        negative_prompt: Visual elements to avoid in the generation.
    """
    try:
        session_id = _session_id_from_context(tool_context)
        if _stale_turn_tool_call(session_id, tool_context):
            logger.info(
                "generate_scene_visuals ignored after turn completion for session %s",
                session_id,
            )
            return "System: Ignore this stale scene request because the turn already finished."
        _tool_entry_t = time.monotonic()
        _desc_preview = repr(description)[:120]
        _neg_preview = repr(negative_prompt)[:60]
        logger.info(f"⏱️ TIMING [generate_scene_visuals] ENTRY at t=0ms | desc={_desc_preview}, negative={_neg_preview}")
        continuity = _continuity_anchor_text(tool_context)
        state = _load_tool_state(tool_context)
        latest_state = load_storybook_resume_state(session_id) if session_id else {}
        if latest_state:
            merged_state = dict(state)
            merged_state.update(latest_state)
            state = merged_state
        ensure_story_continuity_state(state)
        story_tone = _story_tone_from_state(state)
        base_description = description.strip()
        continuity_validation = validate_live_scene_request(state, base_description)
        base_description = continuity_validation.resolved_description
        render_decision = should_render_new_scene_page(
            state,
            base_description,
            target_location_label=continuity_validation.location_label,
            render_in_flight=bool(session_id and session_id in _session_generating),
        )
        if not render_decision.should_render:
            logger.info(
                "[generate_scene_visuals] Staying on current page for %s | reason=%s | location=%s",
                session_id,
                render_decision.reason,
                continuity_validation.location_label,
            )
            return (
                "System: Stay on the current storybook page. The child is still exploring the same scene, "
                "so narrate within the current picture instead of drawing a new one."
            )
        # Remove explicit child name from prompts to reduce image model refusals.
        if state:
            child_name = str(state.get("child_name", "")).strip()
            if child_name and child_name.lower() != "friend":
                base_description = re.sub(rf"\\b{re.escape(child_name)}\\b", "the young hero", base_description, flags=re.IGNORECASE)
        visual_description = base_description
        if continuity_validation.prompt_suffix:
            visual_description = f"{visual_description}. {continuity_validation.prompt_suffix}"
        if continuity:
            visual_description = (
                f"{visual_description}. Keep temporal/character continuity with: {continuity}."
            )
        tone_guidance = _visual_tone_guidance(story_tone)
        if tone_guidance:
            visual_description = f"{visual_description}. {tone_guidance}"

        aspect_ratio, image_size, image_model = _resolve_image_preferences(tool_context)
        delivery_format, delivery_quality, delivery_max_side = _resolve_delivery_preferences(state, image_size)
        continuity_mode = _resolve_continuity_mode()
        reference_images: list[dict[str, str]] = []
        using_toy_reference = False

        def _append_reference_image(b64: str, mime: str, role: str) -> None:
            cleaned_b64 = str(b64 or "").strip()
            if not cleaned_b64:
                return
            for existing in reference_images:
                if existing.get("b64") == cleaned_b64:
                    return
            reference_images.append(
                {
                    "b64": cleaned_b64,
                    "mime": str(mime or "image/jpeg").strip() or "image/jpeg",
                    "role": role,
                }
            )

        if state:
            if continuity_mode == "thumbnail":
                canonical_b64 = str(state.get("canonical_scene_thumbnail_b64", "") or "").strip()
                canonical_mime = str(state.get("canonical_scene_thumbnail_mime", "") or "").strip()
                if canonical_b64:
                    _append_reference_image(canonical_b64, canonical_mime, "canonical_setting")
                candidate_b64 = str(state.get("previous_scene_thumbnail_b64", "") or "").strip()
                candidate_mime = str(state.get("previous_scene_thumbnail_mime", "") or "").strip()
                if candidate_b64:
                    _append_reference_image(candidate_b64, candidate_mime, "previous_scene")
            toy_b64 = str(state.get("toy_reference_thumbnail_b64", "") or "").strip()
            toy_mime = str(state.get("toy_reference_thumbnail_mime", "") or "").strip()
            if toy_b64:
                _append_reference_image(toy_b64, toy_mime, "toy")
                using_toy_reference = True

        if using_toy_reference:
            visual_description = (
                f"{visual_description} The sidekick should match the toy reference image "
                "and feel like a soft, kid-friendly toy (no brand logos)."
            )

        request_id = uuid.uuid4().hex

        def _publish_placeholder(payload_description: str) -> None:
            if not session_id:
                return
            publish_session_event(
                session_id,
                {
                    "type": "video_ready",
                    "payload": {
                        "url": _build_fallback_scene_svg_data_url(payload_description),
                        "description": payload_description,
                        "media_type": "image",
                        "is_placeholder": True,
                        "request_id": request_id,
                    },
                },
            )

        # Pick or retrieve a persistent illustration style for this session.
        session_style: str | None = None
        if tool_context:
            try:
                # ADK ToolContext.state is usually a dict-like, but we ensure string keys.
                state = tool_context.state
                session_style = str(state.get("illustration_style") or "").strip()
                if not session_style:
                    import random
                    session_style = random.choice(CLASSIC_STORYBOOK_STYLES)
                    state["illustration_style"] = session_style
                    logger.info("Assigned new random illustration style for session %s: %s", session_id, session_style[:40])
            except Exception as e:
                logger.warning("Could not manage illustration_style state: %s", str(e))

        try:
            args = VisualArgs(
                description=visual_description,
                base_description=base_description,
                negative_prompt=_merge_visual_negative_prompt(story_tone, negative_prompt),
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                image_model=image_model,
                reference_images=reference_images,
                illustration_style=session_style,
                request_id=request_id,
                delivery_format=delivery_format,
                delivery_quality=delivery_quality,
                delivery_max_side=delivery_max_side,
            )
        except Exception as ve:
            logger.error(f"VisualArgs validation failed: {ve}", exc_info=True)
            raise

        # Hard gate: do NOT generate scene images during onboarding turns (name
        # collection, name confirmation, camera question). The agent sometimes
        # calls this when the child gives their name and story idea in the same
        # utterance, producing a generic "wrong image" before the story starts.
        if tool_context:
            try:
                state_snapshot = getattr(tool_context, "state", {}) or {}
                current_turn = int(state_snapshot.get("turn_number", 1) or 1)
                assembly_status = str(state_snapshot.get("assembly_status", "") or "").strip().lower()
            except (ValueError, TypeError):
                current_turn = 1
                assembly_status = ""
            if assembly_status in {"assembling", "reviewing_storyboard"}:
                logger.info(
                    "generate_scene_visuals ignored during movie assembly for session %s",
                    session_id,
                )
                return (
                    "System: The movie is already being made. Stay in playful waiting-room chat "
                    "and do not create a new picture."
                )
            # The skip logic used to be here, but was removed because it relied on
            # stale `tool_context.state` snapshots which incorrectly blocked
            # valid shortcircuited story generations on Turn 1.
        # Persist scene description in state so the model knows what's displayed on screen
        if tool_context:
            tool_context.state["previous_scene_description"] = tool_context.state.get(
                "current_scene_description", ""
            )
            tool_context.state["previous_scene_base_description"] = tool_context.state.get(
                "current_scene_base_description", ""
            )
            tool_context.state["current_scene_description"] = visual_description
            tool_context.state["current_scene_base_description"] = base_description
            tool_context.state["active_scene_request_id"] = request_id
            ensure_story_continuity_state(tool_context.state)

        _elapsed = int((time.monotonic() - _tool_entry_t) * 1000)
        logger.info(f"⏱️ TIMING [generate_scene_visuals] entry processing complete | elapsed={_elapsed}ms | session={session_id}")
        if continuity_validation.issues:
            logger.info(
                "[generate_scene_visuals] continuity validator adjusted scene for %s | issues=%s | location=%s",
                session_id,
                continuity_validation.issues,
                continuity_validation.location_label,
            )

        # Guard: if a generation is already running for this session, queue the latest
        # request if it's DIFFERENT. If it's the SAME description, just return success.
        if session_id and session_id in _session_generating:
            # Check if this precise description is already in the pipeline
            if _session_last_gen_text.get(session_id) == visual_description:
                logger.info("[generate_scene_visuals] Same description already in progress for %s; skipping duplicate.", session_id)
                return "Drawing already in progress. If you haven't given choices yet, do so now. If you already gave choices, stop talking immediately."
            
            _session_pending[session_id] = args
            _session_cancel_current.add(session_id)
            _publish_placeholder(base_description or visual_description)
            logger.info(
                "[generate_scene_visuals] Queued NEW scene description — generation already in progress "
                "for session %s.",
                session_id,
            )
            return "Drawing started. If you haven't given choices yet, do so now. If you already gave choices, stop talking immediately."

        # Mark as generating IMMEDIATELY (synchronously) to prevent rapid-fire duplicates.
        if session_id:
            _session_generating.add(session_id)

        _publish_placeholder(base_description or visual_description)
        schedule_background_task(
            _run_visual_pipeline(
                args=args,
                session_id=session_id,
                tool_context=tool_context,
            )
        )
        return "Drawing started. If you haven't given choices yet, do so now. If you already gave choices, stop talking immediately."
    except Exception as exc:
        logger.error(f"generate_scene_visuals failed synchronously: {exc}", exc_info=True)
        return f"Error starting visual generation: {exc}"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=5),
    stop=tenacity.stop_after_attempt(2),
    retry=tenacity.retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: logger.warning("Veo/Image gen retry #%s", retry_state.attempt_number),
)
async def _run_visual_pipeline(
    args: VisualArgs,
    session_id: str | None,
    tool_context: ToolContext | None = None,
) -> None:
    """Internal async pipeline: fast still image first, optional Veo clip second."""
    _pipeline_t0 = time.monotonic()
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    image_bytes: bytes | None = None
    gcs_uri: str | None = None
    storybeat_text = _fallback_storybeat_text(args.base_description or args.description)
    superseded_render = False

    # Track timing and description for the session.
    if session_id:
        _session_last_gen_at[session_id] = time.monotonic()
        _session_last_gen_text[session_id] = args.description

    try:
        if session_id:
            backoff_until = _session_image_backoff_until.get(session_id, 0.0)
            now = time.monotonic()
            if backoff_until > now:
                wait_ms = int((backoff_until - now) * 1000)
                logger.info(
                    "⏱️ TIMING [pipeline] BACKPRESSURE WAIT | wait_ms=%d | session=%s",
                    wait_ms,
                    session_id,
                )
                await asyncio.sleep(backoff_until - now)
        logger.info(f"⏱️ TIMING [pipeline] START | session={session_id} | waiting for semaphore")
        async with _still_semaphore:
            _sem_elapsed = int((time.monotonic() - _pipeline_t0) * 1000)
            logger.info(f"⏱️ TIMING [pipeline] SEMAPHORE ACQUIRED | elapsed={_sem_elapsed}ms | session={session_id}")
            # Attempt 1: short prompt, no negative prompt (fast & reliable).
            # Attempt 2: full prompt, no negative prompt.
            base_desc = args.base_description or args.description
            simple_desc = re.sub(r"\s+", " ", base_desc).strip()
            simple_desc = simple_desc[:220] if simple_desc else (base_desc[:220] or base_desc)
            prefixed_simple = f"A whimsical children's storybook illustration of: {simple_desc}"
            retry_plans = [
                (prefixed_simple, args.negative_prompt),
                (base_desc, args.negative_prompt),
            ]
            for attempt, (desc, neg) in enumerate(retry_plans, start=1):
                _attempt_t0 = time.monotonic()
                effective_size = args.image_size
                try:
                    image_bytes, _, storybeat_text = await asyncio.wait_for(
                        asyncio.to_thread(
                            _generate_scene_still,
                            desc,
                            neg,
                            args.aspect_ratio,
                            effective_size,
                            args.image_model,
                            args.reference_images,
                            args.illustration_style,
                        ),
                        timeout=45.0,
                    )
                    if image_bytes:
                        _attempt_elapsed = int((time.monotonic() - _attempt_t0) * 1000)
                        _total_elapsed = int((time.monotonic() - _pipeline_t0) * 1000)
                        logger.info(f"⏱️ TIMING [pipeline] IMAGE GEN attempt {attempt} SUCCESS | attempt_ms={_attempt_elapsed} | total_ms={_total_elapsed} | bytes={len(image_bytes)} | session={session_id}")
                        args.image_size = effective_size
                        if session_id and session_id in _session_cancel_current:
                            superseded_render = True
                        break  # success
                except Exception as exc:
                    if session_id and session_id in _session_cancel_current:
                        logger.info(
                            "Abandoning superseded scene render for session %s after attempt %d failure: %s",
                            session_id,
                            attempt,
                            exc,
                        )
                        raise _SupersededSceneRequest("newer scene queued during retry window") from exc
                    if attempt < len(retry_plans):
                        wait = 0.4 if attempt == 1 else 0.6
                        logger.warning(
                            "Image generation attempt %d/%d failed (size=%s): %s — "
                            "waiting %.1fs before retry.",
                            attempt, len(retry_plans), effective_size, exc, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise  # propagate after final attempt
        _gen_total = int((time.monotonic() - _pipeline_t0) * 1000)
        logger.info(f"⏱️ TIMING [pipeline] IMAGE GEN COMPLETE | total_ms={_gen_total} | raw_bytes={len(image_bytes)} | session={session_id}")
        if session_id:
            _session_image_backoff_until.pop(session_id, None)
        if session_id and session_id in _session_cancel_current:
            raise _SupersededSceneRequest("newer scene queued before delivery encode")
        
        _delivery_t0 = time.monotonic()
        transport_bytes, transport_mime = _encode_transport_image(
            image_bytes,
            fmt=args.delivery_format,
            quality=args.delivery_quality,
            max_side=args.delivery_max_side,
        )
        _delivery_ms = int((time.monotonic() - _delivery_t0) * 1000)
        logger.info(
            "⏱️ TIMING [pipeline] DELIVERY ENCODE | encode_ms=%d | transport_bytes=%d | transport_mime=%s | session=%s",
            _delivery_ms,
            len(transport_bytes),
            transport_mime,
            session_id,
        )

        # 1. Immediate browser delivery: keep first paint on the critical path, and
        # move heavier thumbnail/storage work after the first image is already visible.
        still_url = None
        try:
            still_b64 = base64.b64encode(transport_bytes).decode("ascii")
            still_url = f"data:{transport_mime};base64,{still_b64}"
            
            if session_id:
                if superseded_render or session_id in _session_cancel_current:
                    logger.info("Skipping Base64 publish for session %s — newer scene queued.", session_id)
                else:
                    publish_session_event(
                        session_id,
                        {
                            "type": "video_ready",
                            "payload": {
                                "url": still_url,
                                "media_type": "image",
                                "description": args.description,
                                "storybeat_text": storybeat_text,
                                "is_placeholder": False,
                                # The inline preview is for immediate paint only. Persist the
                                # durable cloud URL after upload instead of writing a large
                                # data URL into live session state first.
                                "persist_asset": False,
                                "request_id": args.request_id,
                            },
                        },
                    )
                    _gen_publish_total = int((time.monotonic() - _pipeline_t0) * 1000)
                    logger.info("⏱️ TIMING [pipeline] PUBLISHED (Base64) | total_ms=%d | session=%s", _gen_publish_total, session_id)
        except Exception as b64_exc:
            logger.warning("Base64 immediate publish failed: %s", b64_exc)

        if session_id and session_id in _session_cancel_current:
            superseded_render = True

        scene_visual_summary = await _describe_scene_image_for_continuity(image_bytes)
        if scene_visual_summary:
            logger.info(
                "Scene continuity anchor captured for session %s: %s",
                session_id,
                scene_visual_summary[:160],
            )
            if tool_context and isinstance(getattr(tool_context, "state", None), dict):
                tool_context.state["current_scene_visual_summary"] = scene_visual_summary
                tool_context.state["previous_scene_visual_summary"] = scene_visual_summary
                if not str(tool_context.state.get("canonical_scene_visual_summary", "") or "").strip():
                    tool_context.state["canonical_scene_visual_summary"] = scene_visual_summary

        # 2. Background asset prep/upload: persistence matters for theater mode and
        # remakes, but it should not delay first paint on slower phones.
        _upload_t0 = time.monotonic()
        try:
            _compress_t0 = time.monotonic()
            storage_bytes, storage_mime = _encode_storage_image(image_bytes)
            _compress_ms = int((time.monotonic() - _compress_t0) * 1000)
            _total_ms = int((time.monotonic() - _pipeline_t0) * 1000)
            logger.info(
                "⏱️ TIMING [pipeline] STORAGE ENCODE | encode_ms=%d | total_ms=%d | storage_bytes=%d | session=%s",
                _compress_ms,
                _total_ms,
                len(storage_bytes),
                session_id,
            )

            thumbnail_b64: str | None = None
            thumbnail_mime: str | None = None
            thumb = _make_thumbnail_b64(storage_bytes)
            if thumb:
                thumbnail_b64, thumbnail_mime = thumb

            cloud_still_url, gcs_uri = await asyncio.wait_for(
                asyncio.to_thread(_upload_scene_still, storage_bytes, storage_mime, project, session_id),
                timeout=12.0,
            )
            if cloud_still_url:
                still_url = cloud_still_url
            if session_id and (cloud_still_url or gcs_uri):
                _persist_uploaded_scene_asset(
                    session_id=session_id,
                    description=args.description,
                    storybeat_text=storybeat_text,
                    scene_visual_summary=scene_visual_summary,
                    cloud_still_url=cloud_still_url,
                    gcs_uri=gcs_uri,
                    thumbnail_b64=thumbnail_b64,
                    thumbnail_mime=thumbnail_mime,
                    request_id=args.request_id,
                    preview_image_url=still_url,
                )
            _upload_ms = int((time.monotonic() - _upload_t0) * 1000)
            logger.info("⏱️ TIMING [pipeline] GCS UPLOAD COMPLETE | upload_ms=%d | session=%s", _upload_ms, session_id)
        except Exception as exc:
            _upload_ms = int((time.monotonic() - _upload_t0) * 1000)
            logger.warning("⏱️ TIMING [pipeline] GCS UPLOAD FAILED after %dms: %s", _upload_ms, exc)
            if session_id and still_url:
                _persist_uploaded_scene_asset(
                    session_id=session_id,
                    description=args.description,
                    storybeat_text=storybeat_text,
                    scene_visual_summary=scene_visual_summary,
                    cloud_still_url=None,
                    gcs_uri=None,
                    thumbnail_b64=None,
                    thumbnail_mime=None,
                    request_id=args.request_id,
                    preview_image_url=still_url,
                )

        if not still_url:
            # Tertiary fallback if everything else failed (local cache)
            backend_base = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
            try:
                media_id = store_media(image_bytes, "image/jpeg")
                still_url = f"{backend_base}/api/scene/{media_id}"
                logger.debug("Falling back to local media cache for session state: %s", still_url)
            except Exception:
                pass

        if session_id:
            if superseded_render or session_id in _session_cancel_current:
                logger.info(
                    "Skipping final live scene publish for session %s — newer scene queued during generation, but the page asset was still persisted for movie assembly.",
                    session_id,
                )
            else:
                _publish_ms = int((time.monotonic() - _pipeline_t0) * 1000)
                logger.debug("Background stage complete: total_ms=%d | session=%s", _publish_ms, session_id)

    except _SupersededSceneRequest as exc:
        logger.info("Skipping stale scene publish for session %s: %s", session_id, exc)
    except Exception as exc:
        retry_scheduled = False
        if session_id and _is_resource_exhausted_error(exc):
            cooldown_seconds = 2.5
            _session_image_backoff_until[session_id] = time.monotonic() + cooldown_seconds
            if _queued_newer_scene_request(session_id, args.request_id):
                logger.warning(
                    "Quota backpressure hit for superseded scene in session %s; preserving newer queued scene instead of retrying request %s",
                    session_id,
                    args.request_id,
                )
            elif args.quota_retry_count < 1:
                retry_scheduled = True
                _session_pending[session_id] = args.model_copy(
                    update={"quota_retry_count": args.quota_retry_count + 1}
                )
                logger.warning(
                    "Deferring scene generation after quota backpressure for session %s; retry #%d scheduled after %.1fs",
                    session_id,
                    args.quota_retry_count + 1,
                    cooldown_seconds,
                )
        logger.warning("Still image generation failed gracefully: %s", exc, exc_info=True)
        schedule_background_task(
            asyncio.to_thread(
                record_prompt_feedback,
                "interactive_scene_visual",
                outcome="fallback",
                issues=[str(exc)],
                prompt_text=args.description,
                session_id=session_id or "",
                metadata={
                    "image_model": args.image_model,
                    "image_size": args.image_size,
                    "aspect_ratio": args.aspect_ratio,
                    "reference_image_count": len(args.reference_images),
                },
            )
        )
        if session_id and session_id not in _session_cancel_current and not retry_scheduled:
            payload = {
                "url": _build_fallback_scene_svg_data_url(args.description),
                "description": args.description,
                "storybeat_text": storybeat_text,
                "media_type": "image",
                "is_placeholder": False,
                "is_fallback": True,
                "persist_asset": False,
                "request_id": args.request_id,
            }
            if gcs_uri:
                payload["gcs_uri"] = gcs_uri
            publish_session_event(
                session_id,
                {
                    "type": "video_ready",
                    "payload": payload,
                },
            )
    else:
        schedule_background_task(
            asyncio.to_thread(
                record_prompt_feedback,
                "interactive_scene_visual",
                outcome="success",
                issues=[],
                prompt_text=args.description,
                session_id=session_id or "",
                metadata={
                    "image_model": args.image_model,
                    "image_size": args.image_size,
                    "aspect_ratio": args.aspect_ratio,
                    "reference_image_count": len(args.reference_images),
                },
            )
        )
        # Success path: optionally kick off Veo clip in the background.
        if _veo_enabled() and image_bytes:
            asyncio.create_task(
                _run_veo_clip(
                    project=project,
                    location=location,
                    image_bytes=image_bytes,
                    args=args,
                    session_id=session_id,
                )
            )
        else:
            logger.info("Skipping Veo clip generation because Veo is disabled or image_bytes is None.")
    finally:
        # Always release the in-flight lock so future calls for this session can proceed.
        if session_id:
            _session_generating.discard(session_id)
            _session_cancel_current.discard(session_id)
            pending = _session_pending.pop(session_id, None)
            if pending is not None:
                logger.info(
                    "Launching queued scene generation for session %s after prior completion.",
                    session_id,
                )
                schedule_background_task(_run_visual_pipeline(args=pending, session_id=session_id))


async def _run_veo_clip(
    project: str,
    location: str,
    image_bytes: bytes,
    args: VisualArgs,
    session_id: str | None,
) -> None:
    """Runs slower Veo clip generation without blocking still-image cadence."""
    async with _veo_semaphore:
        try:
            operation_name = await asyncio.wait_for(
                asyncio.to_thread(
                    _call_veo_api,
                    project=project,
                    location=location,
                    image_bytes=image_bytes,
                    description=args.description,
                    negative_prompt=args.negative_prompt,
                ),
                timeout=45.0,
            )
            raw_url = await asyncio.wait_for(
                asyncio.to_thread(_poll_veo_operation, project, location, operation_name),
                timeout=140.0,
            )
            ready_url = _normalize_media_url(raw_url, project=project)
            if ready_url and session_id:
                publish_session_event(
                    session_id,
                    {
                        "type": "video_ready",
                        "payload": {
                            "url": ready_url,
                            "description": args.description,
                            "media_type": "video",
                        },
                    },
                )
            elif not ready_url:
                logger.warning("Veo output URL could not be normalized to a client-accessible URL.")
        except Exception as exc:
            logger.warning("Optional Veo clip generation failed gracefully: %s", exc)


def _call_veo_api(
    project: str,
    location: str,
    image_bytes: bytes,
    description: str,
    negative_prompt: str,
) -> str:
    """Starts a Veo long-running operation and returns the operation name."""
    import google.auth
    import google.auth.transport.requests

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


async def generate_background_music(
    mood: str,
    intensity: int = 5,
    tool_context: ToolContext | None = None,
) -> str:
    """Triggers background music command for the frontend."""
    args = MusicArgs(mood=mood, intensity=intensity)
    session_id = _session_id_from_context(tool_context)
    if _stale_turn_tool_call(session_id, tool_context):
        logger.info(
            "generate_background_music ignored after turn completion for session %s",
            session_id,
        )
        return "System: Ignore this stale music change because the turn already finished."
    if session_id:
        publish_session_event(
            session_id,
            {
                "type": "music_command",
                "payload": {"mood": args.mood, "intensity": args.intensity},
            },
        )
    logger.info("Music command: mood=%s intensity=%s", args.mood, args.intensity)
    return f"System: Background music changing to '{args.mood}' (intensity {args.intensity})."


async def save_character_fact(
    character_name: str,
    fact: str,
    tool_context: ToolContext,
) -> str:
    """Persists canonical character facts in session state."""
    args = CharacterFactArgs(character_name=character_name.strip(), fact=fact.strip())
    if not args.character_name or not args.fact:
        return "System: Character fact was empty."

    facts_list = list(tool_context.state.get("character_facts_list", []))
    facts_list.append({"character_name": args.character_name, "fact": args.fact})
    facts_list = facts_list[-40:]
    tool_context.state["character_facts_list"] = facts_list
    tool_context.state["character_facts"] = "\n".join(
        f"- {entry['character_name']}: {entry['fact']}" for entry in facts_list
    )

    logger.info("Saved character fact: %s -> %s", args.character_name, args.fact)
    return f"SAVE_FACT:{args.character_name}|{args.fact}"


async def save_child_name(name: str, tool_context: ToolContext) -> str:
    """Saves the child's preferred name in session state."""
    args = ChildNameArgs(name=name.strip())
    if not args.name:
        return "System: No name provided; ask the child for their name again."
    pending = str(tool_context.state.get("pending_child_name", "")).strip()
    name_confirmed = bool(tool_context.state.get("name_confirmed", False))
    recent_utterance = (
        str(tool_context.state.get("partial_child_utterance", "")).strip()
        or str(tool_context.state.get("last_child_utterance", "")).strip()
    )
    recent_detected = _extract_recent_child_name_candidate(recent_utterance)
    if not name_confirmed:
        # Only allow immediate lock-in when the tool name matches either the
        # backend-captured pending name or a fresh name candidate from the
        # child's most recent utterance. This prevents the model from saving a
        # hallucinated name that was not actually spoken.
        if pending and pending.lower() == args.name.lower():
            tool_context.state["child_name"] = args.name
            tool_context.state["pending_child_name"] = ""
            tool_context.state["name_confirmed"] = True
            tool_context.state["name_confirmation_prompted"] = False
            if tool_context.state.get("camera_stage", "none") == "none":
                tool_context.state["camera_stage"] = "done"
                tool_context.state["camera_skipped"] = True
            logger.info("Saving child name: %s", args.name)
            return "System: Name saved. If you already asked a question, stop talking immediately."

        if recent_detected and recent_detected.lower() == args.name.lower():
            tool_context.state["child_name"] = args.name
            tool_context.state["pending_child_name"] = ""
            tool_context.state["name_confirmed"] = True
            tool_context.state["name_confirmation_prompted"] = False
            if tool_context.state.get("camera_stage", "none") == "none":
                tool_context.state["camera_stage"] = "done"
                tool_context.state["camera_skipped"] = True
            logger.info("Saving child name: %s", args.name)
            return "System: Name saved. If you already asked a question, stop talking immediately."

        if pending:
            logger.info(
                "Refusing to save mismatched child name. pending=%s tool=%s recent=%s",
                pending,
                args.name,
                recent_detected or "",
            )
            return "System: Name not confirmed yet; ask the child to confirm their name."

        if recent_detected:
            tool_context.state["pending_child_name"] = recent_detected
            logger.info(
                "Refusing to save unverifiable child name. tool=%s recent=%s",
                args.name,
                recent_detected,
            )
        else:
            tool_context.state["pending_child_name"] = args.name
            logger.info(
                "Refusing to save unverifiable child name with no recent match. tool=%s",
                args.name,
            )
        return "System: Name not confirmed yet; ask the child to confirm their name."
    tool_context.state["child_name"] = args.name
    tool_context.state["pending_child_name"] = ""
    tool_context.state["name_confirmed"] = True
    tool_context.state["name_confirmation_prompted"] = False
    if tool_context.state.get("camera_stage", "none") == "none":
        tool_context.state["camera_stage"] = "done"
        tool_context.state["camera_skipped"] = True
    logger.info("Saving child name: %s", args.name)
    return f"System: Name saved. If you already asked a question, stop talking immediately."


async def assemble_story_video(
    session_id: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """Triggers the FFmpeg Cloud Run Job once per session."""
    resolved_session_id = session_id.strip() or (_session_id_from_context(tool_context) or "")
    if not resolved_session_id:
        return "System: Missing session id for movie assembly."

    args = AssembleArgs(session_id=resolved_session_id)

    if tool_context is not None:
        allowed_to_finish, current_turn, max_turns, turns_remaining = _story_end_progress(tool_context)
        if not allowed_to_finish:
            logger.info(
                "assemble_story_video blocked early for %s at turn %s/%s",
                args.session_id,
                current_turn,
                max_turns,
            )
            if turns_remaining <= 1:
                return (
                    "System: Not movie time yet. Tell the child there is one more magic page, "
                    "then give one final choice and keep the story going."
                )
            return (
                f"System: Not movie time yet. There are still {turns_remaining} story turns left. "
                "Keep the adventure going and give the child one fun next choice."
            )

    if args.session_id in _assembled_sessions:
        logger.warning("assemble_story_video duplicate ignored for %s", args.session_id)
        return "System: Movie assembly already in progress."

    _assembled_sessions.add(args.session_id)

    use_local_storybook = _env_enabled("LOCAL_STORYBOOK_MODE", default=False)
    if not use_local_storybook and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        logger.warning("assemble_story_video: GOOGLE_CLOUD_PROJECT not set; cannot trigger Cloud Run job.")
        return "System: Movie assembly is unavailable. Missing GOOGLE_CLOUD_PROJECT."

    if use_local_storybook:
        logger.info("assemble_story_video route for %s: local_storybook", args.session_id)
        schedule_background_task(_run_local_storybook(args.session_id, tool_context))
        return f"ASSEMBLE_JOB_STARTED|LOCAL_STORYBOOK|SESSION:{args.session_id}"

    if _should_use_fast_storybook_assembly():
        logger.info("assemble_story_video route for %s: fast_storybook", args.session_id)
        schedule_background_task(_run_fast_storybook_assembly(args.session_id, tool_context))
        if _trading_card_enabled():
            schedule_background_task(_generate_trading_card_background(args.session_id, tool_context))
        return f"ASSEMBLE_JOB_STARTED|FAST_STORYBOOK|SESSION:{args.session_id}"

    publish_session_event(
        args.session_id,
        {
            "type": "music_command",
            "payload": {"mood": "triumphant_celebration", "intensity": 8},
        },
    )

    logger.info("assemble_story_video route for %s: cloud_run_job", args.session_id)
    asyncio.create_task(_trigger_ffmpeg_job(args.session_id))
    if _trading_card_enabled():
        schedule_background_task(_generate_trading_card_background(args.session_id, tool_context))
    return f"TRIGGER_LYRIA_CELEBRATION|ASSEMBLE_JOB_STARTED|SESSION:{args.session_id}"


def _ffmpeg_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


_STORYBOOK_W = 1280
_STORYBOOK_H = 720
_STORYBOOK_FPS = 30


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
) -> str:
    sx, ex, sy, ey, zoom_start, zoom_end = _storybook_motion_profile(scene_index, shot_plan)
    duration = max(PAGE_SECONDS_DEFAULT, float(duration))
    settle_seconds, travel_seconds, motion_factor = motion_timing(duration)
    ex = sx + ((ex - sx) * motion_factor)
    ey = sy + ((ey - sy) * motion_factor)
    zoom_end = zoom_start + ((zoom_end - zoom_start) * motion_factor)
    progress = f"max(0,min(1,(t-{settle_seconds:.3f})/{travel_seconds:.3f}))"
    eased = f"(0.5-0.5*cos(PI*{progress}))"
    zoom = f"({zoom_start:.4f}+({zoom_end - zoom_start:.4f})*{eased})"
    return (
        f"scale=w='trunc({_STORYBOOK_W}*{zoom}/2)*2':"
        f"h='trunc({_STORYBOOK_H}*{zoom}/2)*2':"
        "force_original_aspect_ratio=increase:flags=lanczos:eval=frame,"
        f"crop={_STORYBOOK_W}:{_STORYBOOK_H}:"
        f"x='max(0,min(iw-{_STORYBOOK_W},(iw-{_STORYBOOK_W})*({sx:.4f}+({ex - sx:.4f})*{eased})))':"
        f"y='max(0,min(ih-{_STORYBOOK_H},(ih-{_STORYBOOK_H})*({sy:.4f}+({ey - sy:.4f})*{eased})))',"
        "eq=brightness=-0.01:saturation=1.05,unsharp=5:5:0.20:5:5:0.0,setsar=1"
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
    overlay_filters: list[str] | None = None,
) -> None:
    duration = max(1.8, float(duration))
    vf_parts = [_storybook_motion_vf(scene_index, duration, shot_plan)]
    if overlay_filters:
        vf_parts.extend(part for part in overlay_filters if part)
    _render_storybook_image_shot(
        image_path,
        output_path,
        duration,
        ",".join(vf_parts),
    )


def _clean_storybook_title(raw: str) -> str:
    title = (raw or "").strip()
    if not title or title.lower() == "auto":
        return ""
    if "sdk_http_response" in title.lower() or "candidates=[" in title.lower():
        return ""
    title = re.sub(r"^(title|story)\s*[:\-]\s*", "", title, flags=re.IGNORECASE)
    title = title.strip().strip("\"'`")
    title = re.sub(r"\s+", " ", title).strip()
    if re.search(r"reading\s+rainbow", title, re.IGNORECASE):
        return ""
    words = title.split()
    if len(words) > 8:
        title = " ".join(words[:8])
    return title


def _heuristic_storybook_title(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str,
) -> str:
    text = " ".join(scene_descriptions) + " " + (story_summary or "")
    words = re.findall(r"[A-Za-z']{4,}", text)
    stopwords = {
        "this", "that", "with", "from", "they", "them", "were", "where", "when", "then",
        "there", "their", "your", "have", "into", "over", "under", "across", "about",
        "story", "book", "books", "child", "little", "gentle", "glowing", "bright",
        "light", "magic", "magical", "soft", "warm", "night", "cloud", "clouds",
        "reading", "rainbow", "disney", "pixar", "friend",
    }
    counts = Counter(w.lower() for w in words if w.lower() not in stopwords)
    top = [w.title() for w, _ in counts.most_common(3)]
    if len(top) >= 2:
        return f"{top[0]} and the {top[1]}"
    if len(top) == 1:
        return f"The {top[0]} Story"
    clean_child_name = (child_name or "").strip()
    if clean_child_name and clean_child_name.lower() != "friend":
        suffix = "'" if clean_child_name.endswith(("s", "S")) else "'s"
        return f"{clean_child_name}{suffix} Story"
    return "A Storybook Adventure"


def _resolve_storybook_title(state: dict[str, Any]) -> str:
    raw_title = str(
        state.get("story_title")
        or state.get("title")
        or os.environ.get("STORYBOOK_TITLE", "")
    ).strip()
    title = _clean_storybook_title(raw_title)
    if title:
        return title
    return _heuristic_storybook_title(
        list(state.get("scene_descriptions", []) or []),
        str(state.get("story_summary", "")).strip(),
        str(state.get("child_name", "")).strip(),
    )


def _storybook_title_overlay_filters(title: str) -> list[str]:
    clean_title = _clean_storybook_title(title)
    if not clean_title:
        return []
    title_font = 54 if len(clean_title) <= 18 else 48 if len(clean_title) <= 30 else 42
    return [
        "drawbox=x=0:y=44:w=iw:h=96:color=black@0.24:t=fill",
        f"drawtext=text='{_ffmpeg_escape(clean_title)}':fontcolor=white:fontsize={title_font}:x=(w-text_w)/2:y=74:shadowcolor=black:shadowx=2:shadowy=2",
    ]


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


def _sniff_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _make_image_part(image_bytes: bytes) -> Any:
    return google_genai.types.Part.from_bytes(
        data=image_bytes,
        mime_type=_sniff_mime_type(image_bytes),
    )


def _scene_text_for_index(scene_descriptions: list[str], idx: int, story_summary: str) -> str:
    if idx < len(scene_descriptions):
        text = str(scene_descriptions[idx]).strip()
        if text:
            return text
    sentences = _split_sentences(_clean_story_text(story_summary))
    if sentences:
        return sentences[min(idx, len(sentences) - 1)]
    return f"Scene {idx + 1} of the story."


def _post_movie_review_model() -> str:
    return (
        os.environ.get("STORYBOOK_POST_MOVIE_REVIEW_MODEL", "").strip()
        or os.environ.get("STORYBOOK_SCENE_REVIEW_MODEL", "").strip()
        or "gemini-2.5-flash"
    )


def _review_storybook_sequence_for_learning(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str,
    still_paths: list[Path],
) -> dict[str, Any] | None:
    if not still_paths:
        return None

    review_cards = []
    for idx in range(len(still_paths)):
        review_cards.append(
            {
                "scene_index": idx + 1,
                "previous_scene": _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else "",
                "current_scene": _scene_text_for_index(scene_descriptions, idx, story_summary),
                "next_scene": _scene_text_for_index(scene_descriptions, idx + 1, story_summary)
                if idx + 1 < len(still_paths)
                else "",
            }
        )

    meta_guidance = build_principles_injection_text("storyboard_review")
    prompt = f"""
You are reviewing a completed children's read-aloud storybook movie to produce EVOLUTIONARY prompt-learning feedback.
The movie has already shipped. Your job is not to rewrite it now. Your job is to identify what should change in future story prompting and scene prompting.

Child hero name: {child_name or "young hero"}
Story summary:
{story_summary or "No summary available."}

{meta_guidance}

Focus on promptable issues only:
- continuity across adjacent scenes
- character consistency
- location/prop carry-over
- prompt grounding to the intended story beat
- readable overlay text, labels, signatures, or watermarks
- composition clarity for a read-aloud page
- preschool-safe requested tone

Ignore:
- tiny unreadable page texture
- diegetic letters or page markings in clearly book-centric scenes unless they look pasted on top
- subjective style preference nitpicks

Use `warnings` for minor observations.
Use `issues` only for problems that should influence future prompting.

Return JSON only in this schema:
{{
  "global_feedback": ["short note"],
  "warnings": ["minor observation"],
  "issues": [
    {{
      "scene_index": 2,
      "severity": "major",
      "issue": "clear short issue text"
    }}
  ]
}}

Scene cards:
{json.dumps(review_cards, indent=2)}
""".strip()

    contents: list[Any] = [prompt]
    for idx, still_path in enumerate(still_paths, start=1):
        try:
            image_bytes = still_path.read_bytes()
        except Exception:
            continue
        thumb = _make_thumbnail_b64(image_bytes, max_side=384)
        if thumb:
            image_bytes = base64.b64decode(thumb[0])
        contents.append(f"Scene image {idx}")
        contents.append(_make_image_part(image_bytes))

    try:
        client = _build_google_genai_client()
        response = client.models.generate_content(
            model=_post_movie_review_model(),
            contents=contents,
            config=google_genai.types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=2048,
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        logger.warning("Post-movie meta review failed: %s", exc)
        return None

    payload = _extract_json_block(_extract_response_text(response))
    if not isinstance(payload, dict):
        return None

    global_feedback = [
        str(item).strip()
        for item in (payload.get("global_feedback") or [])
        if str(item).strip()
    ][:5]
    warnings = [
        str(item).strip()
        for item in (payload.get("warnings") or [])
        if str(item).strip()
    ][:8]
    issues: list[dict[str, Any]] = []
    for row in payload.get("issues") or []:
        if not isinstance(row, dict):
            continue
        issue_text = str(row.get("issue") or "").strip()
        if not issue_text:
            continue
        try:
            scene_index = int(row.get("scene_index", 0) or 0)
        except Exception:
            scene_index = 0
        if scene_index < 1 or scene_index > len(still_paths):
            scene_index = 0
        severity = str(row.get("severity") or "major").strip().lower() or "major"
        if severity not in {"minor", "major", "critical"}:
            severity = "major"
        issues.append(
            {
                "scene_index": scene_index,
                "severity": severity,
                "issue": issue_text[:300],
            }
        )

    status = "fix_required" if issues else "passed_with_warnings" if warnings else "passed"
    return {
        "status": status,
        "source": "fast_path_post_movie_review",
        "global_feedback": global_feedback,
        "warnings": warnings,
        "issues": issues,
    }


def _run_post_movie_meta_review_sync(session_id: str, state: dict[str, Any]) -> None:
    if not _env_enabled("ENABLE_STORYBOOK_POST_MOVIE_REVIEW", default=True):
        return

    scene_sources = _storybook_scene_sources(state)
    if not scene_sources:
        return

    story_summary = str(state.get("story_summary", "")).strip()
    scene_descriptions = _storybook_scene_descriptions(state)
    child_name = str(state.get("child_name", "")).strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        still_paths: list[Path] = []
        for idx, source in enumerate(scene_sources):
            try:
                image_bytes = _fetch_media_bytes_sync(source)
            except Exception as exc:
                logger.warning("Post-movie review could not fetch scene %d for %s: %s", idx + 1, session_id, exc)
                continue
            suffix = Path(source).suffix if "://" not in source else ""
            still_path = tmp_path / f"review_scene_{idx:03d}{suffix or '.png'}"
            still_path.write_bytes(image_bytes)
            still_paths.append(still_path)

        review_report = _review_storybook_sequence_for_learning(
            scene_descriptions=scene_descriptions,
            story_summary=story_summary,
            child_name=child_name,
            still_paths=still_paths,
        )

    if review_report is None:
        review_report = {
            "status": "skipped",
            "source": "fast_path_post_movie_review",
            "reason": "review_unavailable",
            "global_feedback": [],
            "warnings": [],
            "issues": [],
        }

    issue_texts = [
        str(item.get("issue") or "").strip()
        for item in review_report.get("issues", [])
        if isinstance(item, dict) and str(item.get("issue") or "").strip()
    ]
    warning_texts = [
        str(item).strip()
        for item in review_report.get("warnings", [])
        if str(item).strip()
    ]
    feedback_issues = issue_texts or warning_texts
    review_status = str(review_report.get("status", "observed")).strip() or "observed"

    _update_storybook_firestore(
        session_id,
        {
            "post_movie_meta_review": review_report,
        },
    )
    cache_storybook_state(
        session_id,
        {
            **state,
            "post_movie_meta_review": review_report,
        },
    )

    record_prompt_feedback(
        "storyboard_review",
        outcome=review_status,
        issues=feedback_issues,
        prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
        session_id=session_id,
        metadata={
            "source": "fast_path_post_movie_review",
            "scene_count": len(scene_sources),
            "issue_count": len(issue_texts),
            "warning_count": len(warning_texts),
            "global_feedback": list(review_report.get("global_feedback", []))[:3],
        },
        force_log=True,
    )
    if issue_texts:
        record_prompt_feedback(
            "interactive_story",
            outcome="post_movie_fix_required",
            issues=issue_texts,
            prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
            session_id=session_id,
            metadata={
                "source": "fast_path_post_movie_review",
                "issue_count": len(issue_texts),
            },
        )
        record_prompt_feedback(
            "interactive_scene_visual",
            outcome="post_movie_fix_required",
            issues=issue_texts,
            prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
            session_id=session_id,
            metadata={
                "source": "fast_path_post_movie_review",
                "issue_count": len(issue_texts),
            },
        )


async def _generate_trading_card_background(
    session_id: str,
    tool_context: ToolContext | None,
) -> None:
    """Generates a hero trading card image and stores the URL in the session event bus."""
    if not _trading_card_enabled():
        return
    if session_id in _card_generating:
        logger.info("[_generate_trading_card_background] Card already generating for %s; skipping.", session_id)
        return
    _card_generating.add(session_id)
    try:
        state = _merge_storybook_state(session_id, tool_context)

        child_name = str(state.get("child_name", "our hero")).strip() or "our hero"
        sidekick = str(state.get("sidekick_description", "")).strip()
        story_summary = str(state.get("story_summary", "")).strip()
        facts_raw = list(state.get("character_facts_list", []) or [])
        key_facts = "; ".join(
            f"{e.get('character_name')}: {e.get('fact')}" for e in facts_raw[:3]
        ) if facts_raw else ""

        sidekick_clause = f" alongside their magical sidekick: {sidekick}" if sidekick else ""
        story_clause = f" Story: {story_summary[:120]}" if story_summary else ""
        facts_clause = f" Characters: {key_facts}" if key_facts else ""

        card_prompt = (
            f"A vibrant children's storybook trading card with a thick colorful border. "
            f"The hero is a brave, friendly child named {child_name}{sidekick_clause}. "
            f"{story_clause}{facts_clause} "
            f"Portrait orientation, 3:4 aspect ratio. Bright Pixar/Disney style illustration, "
            f"soft warm colors, magical sparkles, G-rated. "
            f"Bottom banner reads '{child_name}' in bold friendly font. No scary elements."
        )

        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

        # Attempt to generate via Imagen
        image_bytes: bytes | None = None
        image_mime = "image/png"
        try:
            generated = await asyncio.wait_for(
                asyncio.to_thread(
                    _generate_scene_still,
                    card_prompt,
                    "aesthetic: U-rated, safe for children, no text, no brand logos",
                    "3:4",
                    "512px",
                    os.environ.get("IMAGE_MODEL", "").strip() or _default_live_image_model(),
                    None,
                    None,
                ),
                timeout=50.0,
            )
            if isinstance(generated, tuple):
                if generated:
                    image_bytes = generated[0]
                if len(generated) >= 2 and isinstance(generated[1], str) and generated[1].strip():
                    image_mime = generated[1].strip()
            elif isinstance(generated, bytes):
                image_bytes = generated
        except Exception as exc:
            logger.warning("Trading card image generation failed: %s", exc)
            return

        if not image_bytes:
            logger.warning("Trading card generation returned no bytes.")
            return

        image_ext = ".png"
        if image_mime == "image/jpeg":
            image_ext = ".jpg"
        elif image_mime == "image/webp":
            image_ext = ".webp"

        card_url: str | None = None
        # Upload to GCS if project is configured
        if project:
            try:
                bucket_name = os.environ.get("GCS_ASSETS_BUCKET", "")
                if bucket_name:
                    from google.cloud import storage as gcs_storage
                    gcs_client = gcs_storage.Client(project=project)
                    bucket = gcs_client.bucket(bucket_name)
                    blob = bucket.blob(f"{session_id}/trading_card{image_ext}")
                    blob.content_type = image_mime
                    blob.upload_from_string(image_bytes, content_type=image_mime)
                    try:
                        from datetime import timedelta
                        sa_email = getattr(gcs_client, "service_account_email", None)
                        if not sa_email:
                            sa_email = f"{project}@appspot.gserviceaccount.com"

                        card_url = blob.generate_signed_url(
                            version="v4",
                            expiration=timedelta(hours=4),
                            method="GET",
                            service_account_email=sa_email
                        )
                    except Exception:
                        card_url = f"https://storage.googleapis.com/{bucket_name}/{session_id}/trading_card{image_ext}"
                    logger.info("Trading card uploaded to GCS: %s", card_url)
            except Exception as exc:
                logger.warning("Trading card GCS upload failed: %s", exc)

        # Fall back to backend media cache
        if not card_url:
            try:
                backend_base = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
                media_id = store_media(image_bytes, image_mime)
                card_url = f"{backend_base}/api/scene/{media_id}"
                logger.info("Trading card served from backend media cache: %s", card_url)
            except Exception as exc:
                logger.warning("Trading card media cache fallback failed: %s", exc)
                return

        if card_url and session_id:
            _update_storybook_firestore(session_id, {"trading_card_url": card_url})
            cache_storybook_state(
                session_id,
                {
                    **state,
                    "trading_card_url": card_url,
                },
            )
            publish_session_event(
                session_id,
                {
                    "type": "trading_card_ready",
                    "payload": {"trading_card_url": card_url},
                },
            )
            logger.info("Trading card ready event published for session %s", session_id)
    except Exception as exc:
        logger.warning("_generate_trading_card_background failed: %s", exc, exc_info=True)
    finally:
        _card_generating.discard(session_id)


async def generate_trading_card(
    tool_context: ToolContext | None = None,
) -> str:
    """Generates a personalised hero trading card for the child at the end of the story."""
    session_id = _session_id_from_context(tool_context) or ""
    logger.info("[TOOL START] generate_trading_card called for session %s", session_id)
    if not _trading_card_enabled():
        return "System: Hero trading cards are turned off right now."
    if tool_context is not None:
        allowed_to_finish, current_turn, max_turns, _ = _story_end_progress(tool_context)
        if not allowed_to_finish:
            logger.info(
                "generate_trading_card blocked early for %s at turn %s/%s",
                session_id,
                current_turn,
                max_turns,
            )
            return "System: The hero card comes at the very end. Finish the story first."
    if session_id:
        schedule_background_task(_generate_trading_card_background(session_id, tool_context))
        return f"TRADING_CARD_GENERATING|SESSION:{session_id}"
    return "System: Could not generate trading card — missing session id."


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _clean_story_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _CTRL_TOKEN_RE.sub("", text)
    cleaned = re.sub(r"🌟\s*What should we do\?.*?(\n|$)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _simplify_for_kids(text: str, max_words: int = 28) -> str:
    cleaned = _clean_story_text(text)
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


def _simplify_for_readalong(text: str, max_words: int = 8) -> str:
    cleaned = _clean_story_text(text)
    if not cleaned:
        return ""
    sentences = _split_sentences(cleaned)
    if not sentences:
        return ""
    first = sentences[0]
    first = re.split(r",|;|\\b(and|but|so)\\b", first, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if not first:
        return ""
    words = first.split()
    if len(words) > max_words:
        first = " ".join(words[:max_words]).rstrip(".,!?") + "."
    else:
        first = first.rstrip(".,!?") + "."
    return first


_TEMPORAL_START_RE = re.compile(r"^(then|next|while|as)\\b", flags=re.IGNORECASE)
_SPATIAL_START_RE = re.compile(r"^(here|there|in|on|at)\\b", flags=re.IGNORECASE)


def _decapitalize(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:] if text[0].isupper() else text


def _apply_readalong_connectors(line: str, idx: int) -> str:
    """Add spatial anchors or temporal connectors for cohesive narration."""
    if not line:
        return line
    stripped = line.lstrip()
    if idx <= 0:
        if _SPATIAL_START_RE.match(stripped) or _TEMPORAL_START_RE.match(stripped):
            return line
        return f"Here, {_decapitalize(stripped)}"
    if _TEMPORAL_START_RE.match(stripped):
        return line
    core = stripped
    if _SPATIAL_START_RE.match(stripped):
        core = re.sub(r"^(?i)(here|there|in|on|at)\\b[:,]?\\s*", "", stripped)
        if not core:
            core = stripped
    return f"Then, {_decapitalize(core)}"


def _build_narration_segments(
    scene_descriptions: list[str],
    story_summary: str,
    scene_count: int,
    *,
    child_age: int | str | None = None,
) -> list[str]:
    narration: list[str] = []
    max_words = narration_max_words_for_age(child_age)
    if scene_descriptions:
        for idx, desc in enumerate(scene_descriptions[:scene_count]):
            line = _simplify_for_readalong(desc, max_words=max_words)
            line = _apply_readalong_connectors(line, idx)
            if line:
                narration.append(line)
    if not narration and story_summary:
        sentences = _split_sentences(_clean_story_text(story_summary))
        if sentences:
            chunk_size = max(1, len(sentences) // max(1, scene_count))
            for i in range(scene_count):
                chunk = sentences[i * chunk_size:(i + 1) * chunk_size]
                if not chunk and sentences:
                    chunk = [sentences[-1]]
                line = _simplify_for_readalong(" ".join(chunk), max_words=max_words)
                line = _apply_readalong_connectors(line, i)
                narration.append(line)
    if len(narration) < scene_count:
        if narration:
            narration.extend([narration[-1]] * (scene_count - len(narration)))
        else:
            narration = [""] * scene_count
    if len(narration) > scene_count:
        narration = narration[:scene_count]
    return narration


def _normalize_storybook_narration_line(
    text: str,
    *,
    fallback: str = "",
    max_words: int = 10,
    scene_index: int = 0,
) -> str:
    line = _simplify_for_readalong(text, max_words=max_words)
    if not line:
        line = _simplify_for_readalong(fallback, max_words=max_words)
    return _apply_readalong_connectors(line, scene_index) if line else ""


def _normalize_storybook_narration_lines(
    narration_lines: list[str],
    *,
    scene_descriptions: list[str],
    child_age: int | str | None = None,
) -> list[str]:
    max_words = narration_max_words_for_age(child_age)
    normalized: list[str] = []
    for idx, line in enumerate(narration_lines):
        fallback_description = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
        normalized.append(
            _normalize_storybook_narration_line(
                str(line or ""),
                fallback=fallback_description,
                max_words=max_words,
                scene_index=idx,
            )
        )
    return normalized


def _storybook_studio_enabled() -> bool:
    return _env_enabled("ENABLE_STORYBOOK_STUDIO_WORKFLOW", default=True)


def _storybook_studio_max_revisions() -> int:
    try:
        value = int(os.environ.get("STORYBOOK_STUDIO_MAX_REVISIONS", "1"))
    except Exception:
        value = 1
    return max(0, min(value, 2))


def _build_storybook_studio_initial_state(
    *,
    title: str,
    child_name: str,
    child_age: int | str | None,
    story_summary: str,
    scene_descriptions: list[str],
    scene_count: int,
    max_music_cues: int,
    max_sfx_cues: int,
) -> dict[str, Any]:
    return {
        "story_title": title or "A Storybook Adventure",
        "child_name": child_name or "friend",
        "child_age": child_age or 4,
        "child_age_band": child_age_band(child_age),
        "story_summary": story_summary or "A warm, magical read-aloud adventure.",
        "scene_descriptions_json": json.dumps(scene_descriptions, ensure_ascii=True),
        "studio_scene_count": max(1, int(scene_count)),
        "studio_max_music_cues": max(0, int(max_music_cues)),
        "studio_max_sfx_cues": max(0, int(max_sfx_cues)),
    }


def _apply_storybook_mix_guidance(
    guidance: dict[str, Any] | None,
    *,
    narration_volume: float,
    music_volume: float,
    sfx_volume: float,
    enable_ducking: bool,
    music_max: float,
    sfx_max: float,
) -> tuple[float, float, float, bool]:
    guidance = dict(guidance or {})
    try:
        narration_volume = clamp_narration_volume(guidance.get("narration_volume", narration_volume))
    except Exception:
        pass
    try:
        music_volume = min(clamp_music_volume(guidance.get("music_volume", music_volume)), music_max)
    except Exception:
        pass
    try:
        sfx_volume = min(clamp_sfx_volume(guidance.get("sfx_volume", sfx_volume)), sfx_max)
    except Exception:
        pass
    ducking = str(guidance.get("ducking") or "").strip().lower()
    if ducking == "off":
        enable_ducking = False
    elif ducking in {"strong", "medium", "light"}:
        enable_ducking = True
    return narration_volume, music_volume, sfx_volume, enable_ducking


def _run_storybook_studio_sync(
    *,
    session_id: str,
    title: str,
    child_name: str,
    child_age: int | str | None,
    story_summary: str,
    scene_descriptions: list[str],
    scene_count: int,
    max_music_cues: int,
    max_sfx_cues: int,
    fallback_narration_lines: list[str],
) -> dict[str, Any]:
    if not _storybook_studio_enabled() or scene_count <= 0:
        return {
            "status": "disabled",
            "narration_lines": list(fallback_narration_lines),
            "music_cues": {},
            "sfx_cues": {},
            "mix_guidance": {},
        }
    workflow_state = asyncio.run(
        run_storybook_studio_workflow(
            session_id=session_id,
            initial_state=_build_storybook_studio_initial_state(
                title=title,
                child_name=child_name,
                child_age=child_age,
                story_summary=story_summary,
                scene_descriptions=scene_descriptions,
                scene_count=scene_count,
                max_music_cues=max_music_cues,
                max_sfx_cues=max_sfx_cues,
            ),
            max_revision_rounds=_storybook_studio_max_revisions(),
        )
    )
    return build_storybook_studio_plan_from_workflow_state(
        workflow_state,
        scene_count=scene_count,
        fallback_narration_lines=fallback_narration_lines,
    )


async def _run_storybook_studio_async(
    *,
    session_id: str,
    title: str,
    child_name: str,
    child_age: int | str | None,
    story_summary: str,
    scene_descriptions: list[str],
    scene_count: int,
    max_music_cues: int,
    max_sfx_cues: int,
    fallback_narration_lines: list[str],
) -> dict[str, Any]:
    if not _storybook_studio_enabled() or scene_count <= 0:
        return {
            "status": "disabled",
            "narration_lines": list(fallback_narration_lines),
            "music_cues": {},
            "sfx_cues": {},
            "mix_guidance": {},
        }
    workflow_state = await run_storybook_studio_workflow(
        session_id=session_id,
        initial_state=_build_storybook_studio_initial_state(
            title=title,
            child_name=child_name,
            child_age=child_age,
            story_summary=story_summary,
            scene_descriptions=scene_descriptions,
            scene_count=scene_count,
            max_music_cues=max_music_cues,
            max_sfx_cues=max_sfx_cues,
        ),
        max_revision_rounds=_storybook_studio_max_revisions(),
    )
    return build_storybook_studio_plan_from_workflow_state(
        workflow_state,
        scene_count=scene_count,
        fallback_narration_lines=fallback_narration_lines,
    )


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
    except Exception:
        return False


def _synthesize_tts_elevenlabs(text: str) -> bytes | None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip() or _DEFAULT_ELEVENLABS_VOICE_ID
    if not api_key or not voice_id or not text:
        if api_key and not voice_id:
            logger.warning(
                "ELEVENLABS_VOICE_ID is not set — ElevenLabs TTS disabled. "
                "Set ELEVENLABS_VOICE_ID in .env (find IDs at elevenlabs.io/voice-library)."
            )
        return None
    endpoint = os.environ.get("ELEVENLABS_TTS_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    model_id = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2").strip()
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
        },
    }
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
    except Exception:
        return None
    return None


def _synthesize_tts_local(text: str) -> bytes | None:
    """Best-effort local TTS fallback for dev (macOS say / Linux espeak)."""
    if not text:
        return None
    if not (_env_enabled("LOCAL_STORYBOOK_MODE", default=False) or _env_enabled("STORYBOOK_ALLOW_LOCAL_TTS", default=False)):
        return None

    def _to_mp3(source_path: Path) -> bytes | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                return source_path.read_bytes()
            except Exception:
                return None
        mp3_path = source_path.with_suffix(".mp3")
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source_path),
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "4",
                    str(mp3_path),
                ],
                check=True,
                capture_output=True,
            )
            return mp3_path.read_bytes()
        except Exception:
            return None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        if sys.platform == "darwin":
            say = shutil.which("say")
            if not say:
                return None
            aiff_path = tmp_path / "say.aiff"
            try:
                subprocess.run(
                    [
                        say,
                        "-o",
                        str(aiff_path),
                        "--data-format=LEI16@22050",
                        text,
                    ],
                    check=True,
                    capture_output=True,
                )
            except Exception:
                return None
            return _to_mp3(aiff_path)

        if sys.platform.startswith("linux"):
            espeak = shutil.which("espeak") or shutil.which("espeak-ng")
            if not espeak:
                return None
            wav_path = tmp_path / "espeak.wav"
            try:
                subprocess.run(
                    [espeak, "-w", str(wav_path), text],
                    check=True,
                    capture_output=True,
                )
            except Exception:
                return None
            return _to_mp3(wav_path)

    return None


def _synthesize_tts_google(text: str) -> bytes | None:
    if not text:
        return None
    # Prefer ElevenLabs when available; fall back to Google TTS and local TTS.
    audio = _synthesize_tts_elevenlabs(text)
    if audio:
        return audio
    try:
        from google.cloud import texttospeech
    except Exception:
        return _synthesize_tts_local(text)
    try:
        client = texttospeech.TextToSpeechClient()
        language_code = os.environ.get("STORYBOOK_TTS_LANG", "en-US")
        voice_name = os.environ.get("STORYBOOK_TTS_VOICE", "en-US-Neural2-F")
        try:
            speaking_rate = float(os.environ.get("STORYBOOK_TTS_RATE", "0.9"))
        except Exception:
            speaking_rate = 0.9
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
    except Exception:
        return _synthesize_tts_local(text)


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


def _storybook_burned_captions_enabled() -> bool:
    return _env_enabled("ENABLE_STORYBOOK_CAPTIONS", default=BURNED_CAPTIONS_DEFAULT)


def _trading_card_enabled() -> bool:
    return _env_enabled("ENABLE_TRADING_CARD", default=False)


def _lyria_negative_prompt() -> str:
    return (
        "vocals, lyrics, singing, chanting, speech, dialogue, harsh, abrasive, "
        "intense percussion, horror, distorted noise, jump scares"
    )


def _lyria_storybook_prompt(story_summary: str, scene_descriptions: list[str]) -> str:
    opening = scene_descriptions[0].strip() if scene_descriptions else ""
    closing = scene_descriptions[-1].strip() if scene_descriptions else ""
    anchors = " ".join(part for part in [story_summary.strip(), opening, closing] if part).strip()
    if not anchors:
        anchors = "a gentle magical bedtime story for a four-year-old"
    return (
        "Warm instrumental soundtrack for a children's read-aloud storybook. "
        f"{anchors}. "
        "Gentle celesta, felt piano, soft strings, light woodwinds, subtle marimba, "
        "cozy wonder, emotionally safe, no vocals, no lyrics."
    )


def _lyria_generate_music_sync(
    prompt: str,
    seed: int | None = None,
) -> tuple[bytes, str] | None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1").strip() or "us-central1"
    if not project or not prompt:
        return None
    try:
        import google.auth
        import google.auth.transport.requests
    except Exception:
        return None

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    endpoint = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/"
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


def _elevenlabs_generate_music_sync(prompt: str, duration_seconds: float) -> tuple[bytes, str] | None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(2.0, min(float(duration_seconds), 30.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    use_music_api = duration_seconds >= 10.0
    if use_music_api:
        music_endpoint = os.environ.get("ELEVENLABS_MUSIC_ENDPOINT", "").strip() or "https://api.elevenlabs.io/v1/music"
        payload = {
            "prompt": prompt,
            "music_length_ms": int(duration_seconds * 1000),
            "force_instrumental": True,
        }
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(music_endpoint, headers=headers, json=payload)
                if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                    return resp.content, ".mp3"
        except Exception:
            pass

    sound_endpoint = os.environ.get("ELEVENLABS_SOUND_ENDPOINT", "").strip() or "https://api.elevenlabs.io/v1/sound-generation"
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content, ".mp3"
    except Exception:
        return None
    return None


def _elevenlabs_generate_sfx_sync(prompt: str, duration_seconds: float) -> tuple[bytes, str] | None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(0.8, min(float(duration_seconds), 6.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    sound_endpoint = os.environ.get("ELEVENLABS_SOUND_ENDPOINT", "").strip() or "https://api.elevenlabs.io/v1/sound-generation"
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content, ".mp3"
    except Exception:
        return None
    return None


def _generate_storybook_music_sync(
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
            audio = _lyria_generate_music_sync(prompt=prompt, seed=seed)
        elif name == "elevenlabs":
            audio = _elevenlabs_generate_music_sync(prompt=prompt, duration_seconds=duration_seconds)
        else:
            audio = None
        if audio:
            if idx > 0:
                logger.info("Storybook music fallback succeeded with %s", name)
            return audio
    return None


def _generate_storybook_sfx_sync(prompt: str, duration_seconds: float) -> tuple[bytes, str] | None:
    provider = _storybook_sfx_provider()
    attempts = ["elevenlabs"] if provider == "auto" else [provider]
    for name in attempts:
        if name == "off":
            return None
        if name == "elevenlabs":
            audio = _elevenlabs_generate_sfx_sync(prompt=prompt, duration_seconds=duration_seconds)
        else:
            audio = None
        if audio:
            return audio
    return None


def _storybook_sfx_pan_gains(idx: int, total: int) -> tuple[float, float]:
    if total <= 1:
        return 1.0, 1.0
    pattern = (-0.35, 0.35, 0.0, -0.2, 0.2)
    balance = pattern[idx % len(pattern)]
    if balance >= 0:
        left = max(0.45, 1.0 - balance * 0.75)
        right = 1.0
    else:
        left = 1.0
        right = max(0.45, 1.0 + balance * 0.75)
    return left, right


async def _elevenlabs_generate_music(prompt: str, duration_seconds: float) -> bytes | None:
    result = await asyncio.to_thread(_elevenlabs_generate_music_sync, prompt, duration_seconds)
    return result[0] if result else None


async def _elevenlabs_generate_sfx(prompt: str, duration_seconds: float) -> bytes | None:
    result = await asyncio.to_thread(_elevenlabs_generate_sfx_sync, prompt, duration_seconds)
    return result[0] if result else None


async def _fetch_media_bytes(url: str) -> bytes:
    if url.startswith("data:"):
        header, data = url.split(",", 1)
        if ";base64" in header:
            return base64.b64decode(data)
        return data.encode("utf-8")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


def _fetch_media_bytes_sync(url: str) -> bytes:
    if url.startswith("data:"):
        header, data = url.split(",", 1)
        if ";base64" in header:
            return base64.b64decode(data)
        return data.encode("utf-8")
    if url.startswith("gs://"):
        without_scheme = url[5:]
        bucket_name, _, object_name = without_scheme.partition("/")
        if not bucket_name or not object_name:
            raise ValueError(f"Invalid GCS URI: {url}")
        client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        return client.bucket(bucket_name).blob(object_name).download_as_bytes()
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _fetch_storybook_frames_sync(scene_sources: list[str], tmp_path: Path) -> list[Path]:
    if not scene_sources:
        return []

    max_workers = max(1, min(len(scene_sources), 4))
    frames: list[Path | None] = [None] * len(scene_sources)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_fetch_media_bytes_sync, source): (idx, source)
            for idx, source in enumerate(scene_sources)
        }
        for future in as_completed(future_map):
            idx, source = future_map[future]
            data = future.result()
            suffix = Path(source).suffix if "://" not in source else ""
            frame_path = tmp_path / f"frame_{idx:03d}{suffix or '.png'}"
            frame_path.write_bytes(data)
            frames[idx] = frame_path
    return [frame for frame in frames if frame is not None]


def _get_storybook_firestore_client() -> firestore.Client | None:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        return None
    database = os.environ.get("FIRESTORE_DATABASE", "(default)").strip() or "(default)"
    try:
        return firestore.Client(project=project, database=database)
    except Exception as exc:
        logger.debug("Storybook Firestore unavailable: %s", exc)
        return None


def _load_storybook_firestore_state(session_id: str) -> dict[str, Any]:
    client = _get_storybook_firestore_client()
    if client is None or not session_id:
        return {}
    try:
        doc = client.collection("storyteller_sessions").document(session_id).get()
        if doc.exists:
            return dict(doc.to_dict() or {})
    except Exception as exc:
        logger.debug("Could not load storybook Firestore state for %s: %s", session_id, exc)
    return {}


def _merge_storybook_state(session_id: str, tool_context: ToolContext | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    firestore_state = _load_storybook_firestore_state(session_id)
    if firestore_state:
        merged.update(firestore_state)
    cached_state = _storybook_state_cache.get(session_id) or {}
    if cached_state:
        merged.update(cached_state)
    tool_state = _load_tool_state(tool_context)
    if tool_state:
        merged.update(tool_state)
        cache_storybook_state(session_id, tool_state)
    return merged


def _storybook_scene_sources(state: dict[str, Any]) -> list[str]:
    story_pages = _story_pages_from_state(state)
    if story_pages:
        scene_sources: list[str] = []
        for page in story_pages:
            gcs_uri = str(page.get("gcs_uri", "") or "").strip()
            image_url = str(page.get("image_url", "") or "").strip()
            chosen = gcs_uri or image_url
            if chosen.startswith("data:image/svg+xml"):
                chosen = ""
            if chosen:
                scene_sources.append(chosen)
        if scene_sources:
            return scene_sources

    scene_urls = list(state.get("scene_asset_urls", []) or [])
    scene_gcs_uris = list(state.get("scene_asset_gcs_uris", []) or [])
    scene_sources: list[str] = []
    max_scenes = max(len(scene_urls), len(scene_gcs_uris))
    for idx in range(max_scenes):
        gcs_uri = str(scene_gcs_uris[idx]).strip() if idx < len(scene_gcs_uris) and scene_gcs_uris[idx] else ""
        url = str(scene_urls[idx]).strip() if idx < len(scene_urls) and scene_urls[idx] else ""
        chosen = gcs_uri or url
        if chosen.startswith("data:image/svg+xml"):
            chosen = ""
        if chosen:
            scene_sources.append(chosen)
    return scene_sources


def _update_storybook_firestore(session_id: str, fields: dict[str, Any]) -> None:
    client = _get_storybook_firestore_client()
    if client is None or not session_id or not fields:
        return
    try:
        client.collection("storyteller_sessions").document(session_id).set(fields, merge=True)
    except Exception as exc:
        logger.debug("Could not update storybook Firestore state for %s: %s", session_id, exc)


def _run_fast_storybook_sync(session_id: str, state: dict[str, Any]) -> None:
    scene_sources = _storybook_scene_sources(state)
    if not scene_sources:
        raise RuntimeError(f"Fast storybook: no scene sources for session {session_id}")

    story_summary = str(state.get("story_summary", "")).strip()
    scene_descriptions = list(state.get("scene_descriptions", []) or [])
    child_name = str(state.get("child_name", "")).strip()
    child_age = state.get("child_age")
    title = _resolve_storybook_title(state)
    try:
        page_seconds = clamp_page_seconds(os.environ.get("STORYBOOK_PAGE_SECONDS", "4"))
    except Exception:
        page_seconds = clamp_page_seconds(None)

    _update_storybook_firestore(
        session_id,
        {
            "assembly_status": "reviewing_storyboard" if _env_enabled("ENABLE_STORYBOOK_DIRECTOR_WORKFLOW", default=True) else "assembling",
            "story_title": title,
            "storyboard_review": {
                "status": "pending_director_workflow" if _env_enabled("ENABLE_STORYBOOK_DIRECTOR_WORKFLOW", default=True) else "skipped_fast_path",
                "reason": "backend_fast_path_director_workflow" if _env_enabled("ENABLE_STORYBOOK_DIRECTOR_WORKFLOW", default=True) else "backend_still_only_fast_path",
                "passes": [],
                "repairs_applied": [],
            },
        },
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        frames = _fetch_storybook_frames_sync(scene_sources, tmp_path)

        storyboard_review_report: dict[str, Any] = {
            "status": "skipped_fast_path",
            "reason": "director_workflow_not_run",
            "passes": [],
            "repairs_applied": [],
        }
        if frames and _env_enabled("ENABLE_STORYBOOK_DIRECTOR_WORKFLOW", default=True):
            try:
                from backend.ffmpeg_worker.assemble import (
                    _generate_repaired_story_still as _worker_generate_repaired_story_still,
                    _merge_storyboard_reviews as _worker_merge_storyboard_reviews,
                    _review_storyboard_pass as _worker_review_storyboard_pass,
                    _unresolved_storyboard_status as _worker_unresolved_storyboard_status,
                )

                async def _director_review_callback(_workflow_state: dict[str, Any], iteration: int) -> dict[str, Any]:
                    forward_review = await asyncio.to_thread(
                        _worker_review_storyboard_pass,
                        scene_descriptions,
                        story_summary,
                        child_name,
                        frames,
                        "FORWARD",
                    )
                    backward_review = await asyncio.to_thread(
                        _worker_review_storyboard_pass,
                        scene_descriptions,
                        story_summary,
                        child_name,
                        frames,
                        "BACKWARD",
                    )
                    merged = _worker_merge_storyboard_reviews(
                        [("FORWARD", forward_review), ("BACKWARD", backward_review)],
                        total_scenes=len(frames),
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
                    repair_limit = max(1, min(repair_limit, 3))

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
                        if idx < 0 or idx >= len(frames):
                            continue

                        current_image_bytes: bytes | None
                        previous_image_bytes: bytes | None = None
                        hero_anchor_image_bytes: bytes | None = None
                        try:
                            current_image_bytes = frames[idx].read_bytes()
                        except Exception:
                            current_image_bytes = None
                        if idx > 0:
                            try:
                                previous_image_bytes = frames[idx - 1].read_bytes()
                            except Exception:
                                previous_image_bytes = None
                        hero_anchor_description = _scene_text_for_index(scene_descriptions, 0, story_summary)
                        if idx != 0 and frames:
                            try:
                                hero_anchor_image_bytes = frames[0].read_bytes()
                            except Exception:
                                hero_anchor_image_bytes = None

                        current_desc = _scene_text_for_index(scene_descriptions, idx, story_summary)
                        previous_desc = _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else ""
                        next_desc = _scene_text_for_index(scene_descriptions, idx + 1, story_summary) if idx + 1 < len(frames) else ""

                        repaired_bytes = await asyncio.to_thread(
                            _worker_generate_repaired_story_still,
                            current_desc,
                            str(fix.get("repair_prompt") or fix.get("issue") or "").strip(),
                            story_summary,
                            previous_desc,
                            next_desc,
                            current_image_bytes,
                            previous_image_bytes,
                            hero_anchor_description,
                            hero_anchor_image_bytes,
                        )
                        if not repaired_bytes:
                            continue

                        repaired_path = tmp_path / f"frame_{idx:03d}_repair_pass_{iteration}.png"
                        repaired_path.write_bytes(repaired_bytes)
                        frames[idx] = repaired_path
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

                    result_status = "passed_with_repairs" if repairs_applied else _worker_unresolved_storyboard_status(actionable_fixes)
                    return {
                        "status": result_status,
                        "repairs_applied": len(repairs_applied),
                        "repairs": repairs_applied,
                    }

                workflow_state = asyncio.run(
                    run_storybook_director_workflow(
                        session_id=session_id,
                        initial_state={
                            "story_title": title,
                            "child_name": child_name or "friend",
                            "child_age": child_age or 4,
                            "child_age_band": child_age_band(child_age),
                            "story_summary": story_summary or "No summary available.",
                            "scene_descriptions_json": json.dumps(scene_descriptions, ensure_ascii=True),
                        },
                        review_callback=_director_review_callback,
                        repair_callback=_director_repair_callback,
                        max_revision_passes=max(1, min(int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_PASSES", "2") or 2), 3)),
                        max_repairs_per_pass=max(1, min(int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_FIXES", "3") or 3), 4)),
                    )
                )
                storyboard_review_report = build_storyboard_report_from_workflow_state(workflow_state)
            except Exception as exc:
                logger.warning("Fast storybook director workflow failed for %s: %s", session_id, exc, exc_info=True)
                storyboard_review_report = {
                    "status": "skipped_fast_path",
                    "reason": f"director_workflow_failed:{type(exc).__name__}",
                    "passes": [],
                    "repairs_applied": [],
                }

        try:
            max_music_cues = int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"))
        except Exception:
            max_music_cues = 2
        max_music_cues = max(0, min(max_music_cues, 4))
        try:
            sfx_max = clamp_sfx_max(os.environ.get("STORYBOOK_SFX_MAX", "2"))
        except Exception:
            sfx_max = clamp_sfx_max(None)

        default_narration_lines = _build_narration_segments(
            scene_descriptions=scene_descriptions,
            story_summary=story_summary,
            scene_count=len(frames),
            child_age=child_age,
        )
        studio_plan: dict[str, Any] = {
            "status": "not_run",
            "narration_lines": list(default_narration_lines),
            "music_cues": {},
            "sfx_cues": {},
            "mix_guidance": {},
        }
        try:
            studio_plan = _run_storybook_studio_sync(
                session_id=session_id,
                title=title,
                child_name=child_name,
                child_age=child_age,
                story_summary=story_summary,
                scene_descriptions=scene_descriptions,
                scene_count=len(frames),
                max_music_cues=max_music_cues,
                max_sfx_cues=sfx_max,
                fallback_narration_lines=default_narration_lines,
            )
        except Exception as exc:
            logger.warning("Fast storybook studio workflow failed for %s: %s", session_id, exc, exc_info=True)
        studio_summary = build_storybook_studio_summary(studio_plan)
        studio_audio_locked = str(studio_plan.get("status") or "").strip().lower() not in {"", "not_run", "disabled"}

        _update_storybook_firestore(
            session_id,
            {
                "assembly_status": "assembling",
                "storyboard_review": storyboard_review_report,
                "storybook_studio": studio_summary,
            },
        )

        narration_lines = _normalize_storybook_narration_lines(
            list(studio_plan.get("narration_lines", []) or default_narration_lines),
            scene_descriptions=scene_descriptions,
            child_age=child_age,
        )
        expected_narration_count = sum(1 for line in narration_lines if str(line or "").strip())
        enable_tts = _env_enabled("ENABLE_STORYBOOK_TTS", default=True)
        enable_captions = _storybook_burned_captions_enabled()
        enable_music = _env_enabled("ENABLE_STORYBOOK_MUSIC", default=True)
        enable_sfx = _env_enabled("ENABLE_STORYBOOK_SFX", default=True)
        enable_ducking = _env_enabled("ENABLE_STORYBOOK_DUCKING", default=True)
        if studio_audio_locked:
            enable_music = enable_music and bool(studio_plan.get("music_enabled", True))
        enable_mastering = _env_enabled("ENABLE_STORYBOOK_AUDIO_MASTERING", default=True)
        try:
            narration_volume = clamp_narration_volume(os.environ.get("STORYBOOK_NARRATION_VOLUME", "1.6"))
        except Exception:
            narration_volume = clamp_narration_volume(None)
        try:
            music_volume = clamp_music_volume(os.environ.get("STORYBOOK_MUSIC_VOLUME", "0.10"))
        except Exception:
            music_volume = clamp_music_volume(None)
        try:
            sfx_volume = clamp_sfx_volume(os.environ.get("STORYBOOK_SFX_VOLUME", "0.22"))
        except Exception:
            sfx_volume = clamp_sfx_volume(None)
        try:
            sfx_min_score = int(os.environ.get("STORYBOOK_SFX_MIN_SCORE", "2"))
        except Exception:
            sfx_min_score = 2
        try:
            sfx_cooldown = clamp_sfx_cooldown_pages(os.environ.get("STORYBOOK_SFX_COOLDOWN", "1"))
        except Exception:
            sfx_cooldown = clamp_sfx_cooldown_pages(None)
        narration_volume, music_volume, sfx_volume, enable_ducking = _apply_storybook_mix_guidance(
            studio_plan.get("mix_guidance"),
            narration_volume=narration_volume,
            music_volume=music_volume,
            sfx_volume=sfx_volume,
            enable_ducking=enable_ducking,
            music_max=MUSIC_VOLUME_MAX,
            sfx_max=SFX_VOLUME_MAX,
        )

        narration_audio: list[Path | None] = [None] * len(narration_lines)
        scene_durations: list[float] = [float(page_seconds)] * len(narration_lines)
        if enable_tts and narration_lines:
            max_tts_workers = max(1, min(len(narration_lines), 4))
            with ThreadPoolExecutor(max_workers=max_tts_workers) as executor:
                future_map = {
                    executor.submit(_synthesize_tts_google, line): idx
                    for idx, line in enumerate(narration_lines)
                    if line
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        audio_bytes = future.result()
                    except Exception as exc:
                        logger.warning("Fast storybook narration TTS failed for %s[%d]: %s", session_id, idx, exc)
                        continue
                    if not audio_bytes:
                        continue
                    audio_path = tmp_path / f"narration_{idx:03d}.mp3"
                    audio_path.write_bytes(audio_bytes)
                    narration_audio[idx] = audio_path
                    audio_seconds = _ffprobe_duration(audio_path)
                    if audio_seconds > 0:
                        scene_durations[idx] = max(page_seconds, audio_seconds + PAGE_SECONDS_NARRATION_BUFFER)
        rendered_narration_count = (
            expected_narration_count if not enable_tts else sum(1 for path in narration_audio if path is not None)
        )

        segments: list[Path] = []
        scene_shot_plans = plan_storyboard_shots(
            [str(scene_descriptions[idx]).strip() if idx < len(scene_descriptions) else "" for idx in range(len(frames))]
        )
        for idx, (frame, duration) in enumerate(zip(frames, scene_durations)):
            segment_path = tmp_path / f"segment_{idx:03d}.mp4"
            overlay_filters = _storybook_title_overlay_filters(title) if idx == 0 else None
            _render_storybook_cinematic_segment(
                frame,
                segment_path,
                duration,
                idx,
                scene_shot_plans[idx] if idx < len(scene_shot_plans) else None,
                overlay_filters,
            )
            segments.append(segment_path)

        concat_list = tmp_path / "concat.txt"
        concat_list.write_text("\n".join(f"file '{segment.name}'" for segment in segments))

        base_story_path = tmp_path / "storybook_base.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(base_story_path),
            ],
            check=True,
            capture_output=True,
        )

        output_path = tmp_path / "storybook_video.mp4"
        vf_parts = [
            "setsar=1",
        ]

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(base_story_path),
                "-vf",
                ",".join(vf_parts),
                *_storybook_x264_args(),
                "-movflags",
                "+faststart",
                str(output_path),
            ],
            check=True,
            capture_output=True,
        )

        audio_available = False
        story_duration = max(1.0, sum(scene_durations))

        def _concat_audio_segments(segment_paths: list[Path], stem: str) -> Path | None:
            if not segment_paths:
                return None
            concat_path = tmp_path / f"{stem}_concat.txt"
            concat_path.write_text("\n".join(f"file '{p.name}'" for p in segment_paths))
            output_audio = tmp_path / f"{stem}_full.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_path),
                    "-c:a",
                    "pcm_s16le",
                    str(output_audio),
                ],
                check=True,
                capture_output=True,
            )
            return output_audio

        narration_full: Path | None = None
        if any(path is not None for path in narration_audio):
            padded_audio_paths: list[Path] = []
            for idx, duration in enumerate(scene_durations):
                src = narration_audio[idx] if idx < len(narration_audio) else None
                padded_path = tmp_path / f"narration_pad_{idx:03d}.wav"
                if src is None:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-f",
                            "lavfi",
                            "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=44100",
                            "-t",
                            f"{duration:.3f}",
                            str(padded_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                else:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(src),
                            "-af",
                            f"aformat=channel_layouts=stereo,apad=pad_dur={duration:.3f},atrim=0:{duration:.3f}",
                            "-ar",
                            "44100",
                            "-ac",
                            "2",
                            str(padded_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                padded_audio_paths.append(padded_path)
            narration_full = _concat_audio_segments(padded_audio_paths, "narration")
        narration_audio_available = bool(narration_full and narration_full.exists())
        if narration_audio_available:
            narration_audio_available = _ffprobe_duration(narration_full) > 0.1

        music_result: tuple[bytes, str] | None = None
        sfx_sources: list[Path | None] = [None] * len(scene_durations)
        sfx_requests: list[tuple[int, str, float]] = []
        if enable_sfx and scene_durations:
            studio_sfx_cues = dict(studio_plan.get("sfx_cues", {}) or {})
            if studio_audio_locked:
                for idx, cue in sorted(studio_sfx_cues.items()):
                    if idx < 0 or idx >= len(scene_durations):
                        continue
                    desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                    prompt = str(cue.get("prompt") or "").strip() or _build_sfx_prompt(desc)
                    cue_duration = cue.get("duration_seconds")
                    try:
                        cue_duration = float(cue_duration)
                    except Exception:
                        cue_duration = max(0.8, min(3.0, scene_durations[idx]))
                    cue_duration = max(0.8, min(float(cue_duration), min(3.0, scene_durations[idx])))
                    sfx_requests.append((idx, prompt, cue_duration))
            else:
                sfx_indices = _choose_sfx_indices(
                    total=len(scene_durations),
                    max_count=sfx_max,
                    descriptions=scene_descriptions,
                    min_score=sfx_min_score,
                    cooldown=sfx_cooldown,
                )
                for idx in sfx_indices:
                    desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                    prompt = _build_sfx_prompt(desc)
                    cue_duration = max(0.8, min(3.0, scene_durations[idx]))
                    sfx_requests.append((idx, prompt, cue_duration))

        if enable_music or sfx_requests:
            future_map: dict[Any, tuple[str, int | None]] = {}
            with ThreadPoolExecutor(max_workers=max(1, min(1 + len(sfx_requests), 4))) as executor:
                if enable_music:
                    music_prompt = str(studio_plan.get("music_arc_prompt") or "").strip() or _lyria_storybook_prompt(story_summary, scene_descriptions)
                    music_seed = sum(ord(ch) for ch in session_id[:12]) or None
                    future = executor.submit(
                        _generate_storybook_music_sync,
                        music_prompt,
                        min(max(story_duration, 12.0), 30.0),
                        music_seed,
                    )
                    future_map[future] = ("music", None)
                for idx, prompt, cue_duration in sfx_requests:
                    future = executor.submit(_generate_storybook_sfx_sync, prompt, cue_duration)
                    future_map[future] = ("sfx", idx)
                for future in as_completed(future_map):
                    kind, idx = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.warning("Storybook %s generation failed: %s", kind, exc)
                        continue
                    if not result:
                        continue
                    if kind == "music":
                        music_result = result
                    elif idx is not None:
                        sfx_bytes, sfx_suffix = result
                        sfx_source = tmp_path / f"sfx_src_{idx:03d}{sfx_suffix or '.mp3'}"
                        sfx_source.write_bytes(sfx_bytes)
                        sfx_sources[idx] = sfx_source

        logger.info(
            "Fast storybook audio plan for %s: narration=%d/%d music=%s sfx=%d",
            session_id,
            sum(1 for path in narration_audio if path is not None),
            len(narration_audio),
            bool(music_result),
            sum(1 for path in sfx_sources if path is not None),
        )

        music_full: Path | None = None
        if music_result:
            music_bytes, music_suffix = music_result
            music_source = tmp_path / f"music_source{music_suffix or '.wav'}"
            music_source.write_bytes(music_bytes)
            music_full = tmp_path / "music_full.wav"
            fade_out_start = max(0.0, story_duration - 1.5)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(music_source),
                    "-t",
                    f"{story_duration:.3f}",
                    "-af",
                    ",".join(
                        [
                            "aformat=channel_layouts=stereo",
                            "highpass=f=70",
                            "lowpass=f=12000",
                            "dynaudnorm=f=150:g=7",
                            "afade=t=in:st=0:d=0.8",
                            f"afade=t=out:st={fade_out_start:.3f}:d=1.5",
                        ]
                    ),
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(music_full),
                ],
                check=True,
                capture_output=True,
            )

        sfx_full: Path | None = None
        if enable_sfx and scene_durations and any(sfx_sources):
            sfx_segments: list[Path] = []
            for idx, duration in enumerate(scene_durations):
                src = sfx_sources[idx] if idx < len(sfx_sources) else None
                sfx_segment = tmp_path / f"sfx_pad_{idx:03d}.wav"
                if src is None:
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-f",
                            "lavfi",
                            "-i",
                            "anullsrc=channel_layout=stereo:sample_rate=44100",
                            "-t",
                            f"{duration:.3f}",
                            str(sfx_segment),
                        ],
                        check=True,
                        capture_output=True,
                    )
                else:
                    left_gain, right_gain = _storybook_sfx_pan_gains(idx, len(scene_durations))
                    fade_out_start = max(0.05, duration - 0.25)
                    subprocess.run(
                        [
                            "ffmpeg",
                            "-y",
                            "-i",
                            str(src),
                            "-af",
                            ",".join(
                                [
                                    "aformat=channel_layouts=stereo",
                                    "highpass=f=140",
                                    "lowpass=f=12000",
                                    f"pan=stereo|c0={left_gain:.3f}*c0|c1={right_gain:.3f}*c1",
                                    "aecho=0.8:0.4:25|60:0.10|0.05",
                                    "afade=t=in:st=0:d=0.04",
                                    f"afade=t=out:st={fade_out_start:.3f}:d=0.20",
                                    f"apad=pad_dur={duration:.3f}",
                                    f"atrim=0:{duration:.3f}",
                                ]
                            ),
                            "-ar",
                            "44100",
                            "-ac",
                            "2",
                            "-c:a",
                            "pcm_s16le",
                            str(sfx_segment),
                        ],
                        check=True,
                        capture_output=True,
                    )
                sfx_segments.append(sfx_segment)
            sfx_full = _concat_audio_segments(sfx_segments, "sfx")

        mixed_audio: Path | None = None
        input_paths: list[Path] = []
        filter_parts: list[str] = []
        mix_labels: list[str] = []

        if narration_full and narration_full.exists() and _ffprobe_duration(narration_full) > 0.1:
            voice_idx = len(input_paths)
            input_paths.append(narration_full)
            filter_parts.append(
                f"[{voice_idx}:a]aformat=channel_layouts=stereo,highpass=f=90,lowpass=f=15000,volume={narration_volume:.3f}[voice]"
            )
            mix_labels.append("voice")

        music_label = "music"
        if music_full and music_full.exists() and _ffprobe_duration(music_full) > 0.1:
            music_idx = len(input_paths)
            input_paths.append(music_full)
            filter_parts.append(
                f"[{music_idx}:a]aformat=channel_layouts=stereo,volume={music_volume:.3f}[music]"
            )
            if "voice" in mix_labels and enable_ducking:
                filter_parts.append(
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked]"
                )
                music_label = "ducked"
            mix_labels.append(music_label)

        if sfx_full and sfx_full.exists() and _ffprobe_duration(sfx_full) > 0.1:
            sfx_idx = len(input_paths)
            input_paths.append(sfx_full)
            filter_parts.append(
                f"[{sfx_idx}:a]aformat=channel_layouts=stereo,volume={sfx_volume:.3f}[sfx]"
            )
            mix_labels.append("sfx")

        if mix_labels:
            mixed_audio = tmp_path / "audio_mix.wav"
            mix_chain = "".join(f"[{label}]" for label in mix_labels)
            if len(mix_labels) == 1:
                mix_chain += "anull"
            else:
                mix_chain += f"amix=inputs={len(mix_labels)}:duration=shortest:dropout_transition=0"
            if enable_mastering:
                mix_chain += ",alimiter=limit=0.95,loudnorm=I=-17:TP=-2:LRA=6"
            mix_chain += "[mixout]"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    *sum((["-i", str(path)] for path in input_paths), []),
                    "-filter_complex",
                    ";".join(filter_parts + [mix_chain]),
                    "-map",
                    "[mixout]",
                    "-c:a",
                    "pcm_s16le",
                    str(mixed_audio),
                ],
                check=True,
                capture_output=True,
            )

        if mixed_audio and mixed_audio.exists() and _ffprobe_duration(mixed_audio) > 0.1:
            audio_available = narration_audio_available
            merged_path = tmp_path / "storybook_with_audio.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(output_path),
                    "-i",
                    str(mixed_audio),
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(merged_path),
                ],
                check=True,
                capture_output=True,
            )
            output_path = merged_path

        if enable_captions and any(narration_lines):
            drawtext_parts: list[str] = []
            cursor = 0.0
            for duration, line in zip(scene_durations, narration_lines):
                if not line:
                    cursor += duration
                    continue
                wrapped = _wrap_caption(line, width=30)
                escaped = _ffmpeg_escape(wrapped)
                drawtext_parts.append(
                    f"drawtext=text='{escaped}':fontcolor=white:fontsize=46:x=(w-text_w)/2:y=h*0.10:box=1:boxcolor=0x00000099:boxborderw=18:font=Sans:shadowcolor=black:shadowx=2:shadowy=2:line_spacing=8:enable='between(t,{cursor:.3f},{cursor + duration:.3f})'"
                )
                cursor += duration
            if drawtext_parts:
                captioned_path = tmp_path / "storybook_captioned.mp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(output_path),
                        "-vf",
                        ",".join(drawtext_parts),
                        "-c:a",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(captioned_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                output_path = captioned_path

        bucket_name = os.environ.get("GCS_FINAL_VIDEOS_BUCKET", "").strip()
        if not bucket_name:
            raise RuntimeError("GCS_FINAL_VIDEOS_BUCKET is not configured")
        storage_client = storage.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None)
        blob = storage_client.bucket(bucket_name).blob(f"{session_id}/story_final.mp4")
        blob.content_disposition = f'attachment; filename="{session_id}-story.mp4"'
        blob.upload_from_filename(str(output_path), content_type="video/mp4")
        final_url = f"https://storage.googleapis.com/{bucket_name}/{session_id}/story_final.mp4"
        final_gcs_uri = f"gs://{bucket_name}/{session_id}/story_final.mp4"
        final_has_audio_stream = _ffprobe_has_audio_stream(output_path)
        final_video_duration_sec = _ffprobe_duration(output_path)
        audio_expected = bool(enable_tts and narration_required_default())
        theater_release_ready, release_gate_issues = storybook_release_gate(
            scene_count=len(frames),
            final_video_duration_sec=final_video_duration_sec,
            expected_audio=audio_expected,
            audio_available=audio_available,
            final_has_audio_stream=final_has_audio_stream,
            expected_narration_count=expected_narration_count,
            rendered_narration_count=rendered_narration_count,
            shot_types=[plan.shot_type for plan in scene_shot_plans],
        )
        if not theater_release_ready:
            raise RuntimeError(
                "Fast storybook assembly failed the release gate: "
                f"duration={final_video_duration_sec:.2f}s "
                f"audio_expected={audio_expected} "
                f"audio_available={audio_available} "
                f"final_has_audio_stream={final_has_audio_stream} "
                f"issues={'; '.join(release_gate_issues)}"
            )

        _update_storybook_firestore(
            session_id,
            {
                "assembly_status": "complete",
                "final_video_url": final_url,
                "final_video_gcs_uri": final_gcs_uri,
                "story_title": title,
                "narration_lines": [line for line in narration_lines if line],
                "audio_expected": audio_expected,
                "audio_available": audio_available,
                "expected_narration_count": expected_narration_count,
                "rendered_narration_count": rendered_narration_count,
                "final_has_audio_stream": final_has_audio_stream,
                "final_video_duration_sec": final_video_duration_sec,
                "final_scene_count": len(frames),
                "final_shot_types": [plan.shot_type for plan in scene_shot_plans],
                "theater_release_ready": theater_release_ready,
                "story_phase": "theater",
                "storyboard_review": storyboard_review_report,
                "storybook_studio": studio_summary,
                "post_movie_meta_review": {
                    "status": "scheduled",
                    "source": "fast_path_post_movie_review",
                },
            },
        )
        cache_storybook_state(
            session_id,
            {
                **state,
                "assembly_status": "complete",
                "final_video_url": final_url,
                "final_video_gcs_uri": final_gcs_uri,
                "story_title": title,
                "narration_lines": [line for line in narration_lines if line],
                "audio_expected": audio_expected,
                "audio_available": audio_available,
                "expected_narration_count": expected_narration_count,
                "rendered_narration_count": rendered_narration_count,
                "final_has_audio_stream": final_has_audio_stream,
                "final_video_duration_sec": final_video_duration_sec,
                "final_scene_count": len(frames),
                "final_shot_types": [plan.shot_type for plan in scene_shot_plans],
                "theater_release_ready": theater_release_ready,
                "story_phase": "theater",
                "storyboard_review": storyboard_review_report,
                "storybook_studio": studio_summary,
                "post_movie_meta_review": {
                    "status": "scheduled",
                    "source": "fast_path_post_movie_review",
                },
            },
        )
        review_state = {
            **state,
            "final_video_url": final_url,
            "final_video_gcs_uri": final_gcs_uri,
            "story_title": title,
        }
        schedule_background_task(asyncio.to_thread(_run_post_movie_meta_review_sync, session_id, review_state))
        async def _publish_theater_ready() -> None:
            publish_session_event(
                session_id,
                theater_mode_event(
                    mp4_url=final_url,
                    trading_card_url=str(state.get("trading_card_url", "")).strip() or None,
                    narration_lines=[line for line in narration_lines if line],
                    audio_available=audio_available,
                    story_title=title,
                    child_name=str(state.get("child_name", "")).strip() or None,
                    story_phase="theater",
                ).model_dump(mode="json"),
            )

        schedule_background_task(_publish_theater_ready())
        logger.info("Fast storybook assembled for session %s", session_id)


async def _run_fast_storybook_assembly(session_id: str, tool_context: ToolContext | None) -> None:
    try:
        state = _merge_storybook_state(session_id, tool_context)
        await asyncio.to_thread(_run_fast_storybook_sync, session_id, state)
    except Exception as exc:
        logger.warning("Fast storybook assembly failed for %s, falling back to job: %s", session_id, exc, exc_info=True)
        await _trigger_ffmpeg_job(session_id)


async def _run_local_storybook(session_id: str, tool_context: ToolContext | None) -> None:
    """Creates a lightweight storybook video from scene images and emits theater_mode."""
    try:
        def _load_state() -> dict[str, Any]:
            raw = getattr(tool_context, "state", None) if tool_context else None
            if raw is None:
                return {}
            if hasattr(raw, "to_dict"):
                try:
                    return dict(raw.to_dict() or {})
                except Exception:
                    return {}
            if hasattr(raw, "_value"):
                try:
                    return dict(getattr(raw, "_value", {}) or {})
                except Exception:
                    return {}
            if isinstance(raw, dict):
                return dict(raw)
            try:
                return dict(raw)  # best-effort
            except Exception:
                return {}

        min_scenes = 2
        try:
            min_scenes = int(os.environ.get("STORYBOOK_MIN_SCENES", "2"))
        except Exception:
            min_scenes = 2
        min_scenes = max(1, min(min_scenes, 6))
        try:
            wait_seconds = float(os.environ.get("STORYBOOK_WAIT_FOR_SCENES_SECONDS", "12"))
        except Exception:
            wait_seconds = 12.0
        wait_seconds = max(0.0, min(wait_seconds, 30.0))

        state = _load_state()
        scene_urls = _storybook_scene_sources(state)
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            in_flight = session_id in _session_generating or session_id in _session_pending
            if len(scene_urls) >= min_scenes and not in_flight:
                break
            await asyncio.sleep(0.4)
            state = _load_state()
            scene_urls = _storybook_scene_sources(state)

        if len(scene_urls) < min_scenes:
            logger.warning(
                "Local storybook assembling with %d scene(s) (min=%d).",
                len(scene_urls),
                min_scenes,
            )
        if not scene_urls:
            logger.warning("Local storybook: no scene images for session %s", session_id)
            return

        story_summary = str(state.get("story_summary", "")).strip()
        scene_descriptions = _storybook_scene_descriptions(state)
        child_name = str(state.get("child_name", "")).strip()
        child_age = state.get("child_age")
        title = _resolve_storybook_title(state)
        try:
            page_seconds = clamp_page_seconds(os.environ.get("STORYBOOK_PAGE_SECONDS", "4"))
        except Exception:
            page_seconds = clamp_page_seconds(None)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            async with httpx.AsyncClient(timeout=30.0) as client:
                async def _download_frame(idx: int, url: str) -> Path:
                    if url.startswith("data:"):
                        data = await _fetch_media_bytes(url)
                    else:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        data = resp.content
                    frame_path = tmp_path / f"frame_{idx:03d}.png"
                    frame_path.write_bytes(data)
                    return frame_path

                frames = list(
                    await asyncio.gather(
                        *(_download_frame(idx, str(url)) for idx, url in enumerate(scene_urls))
                    )
                )

            default_narration_lines = _build_narration_segments(
                scene_descriptions=scene_descriptions,
                story_summary=story_summary,
                scene_count=len(frames),
                child_age=child_age,
            )
            enable_tts = _env_enabled("ENABLE_STORYBOOK_TTS", default=True)
            enable_captions = _storybook_burned_captions_enabled()
            enable_music = _env_enabled("ENABLE_STORYBOOK_MUSIC", default=True)
            enable_sfx = _env_enabled("ENABLE_STORYBOOK_SFX", default=True)
            enable_ducking = _env_enabled("ENABLE_STORYBOOK_DUCKING", default=True)
            try:
                max_music_cues = int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"))
            except Exception:
                max_music_cues = 2
            max_music_cues = max(0, min(max_music_cues, 4))
            try:
                narration_volume = clamp_narration_volume(os.environ.get("STORYBOOK_NARRATION_VOLUME", "1.6"))
            except Exception:
                narration_volume = clamp_narration_volume(None)
            try:
                music_volume = clamp_music_volume(os.environ.get("STORYBOOK_MUSIC_VOLUME", "0.10"))
            except Exception:
                music_volume = clamp_music_volume(None)
            try:
                sfx_volume = clamp_sfx_volume(os.environ.get("STORYBOOK_SFX_VOLUME", "0.22"))
            except Exception:
                sfx_volume = clamp_sfx_volume(None)
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
            studio_plan: dict[str, Any] = {
                "status": "not_run",
                "narration_lines": list(default_narration_lines),
                "music_cues": {},
                "sfx_cues": {},
                "mix_guidance": {},
            }
            try:
                studio_plan = await _run_storybook_studio_async(
                    session_id=session_id,
                    title=title,
                    child_name=child_name,
                    child_age=child_age,
                    story_summary=story_summary,
                    scene_descriptions=scene_descriptions,
                    scene_count=len(frames),
                    max_music_cues=max_music_cues,
                    max_sfx_cues=sfx_max,
                    fallback_narration_lines=default_narration_lines,
                )
            except Exception as exc:
                logger.warning("Local storybook studio workflow failed for %s: %s", session_id, exc, exc_info=True)
            narration_lines = _normalize_storybook_narration_lines(
                list(studio_plan.get("narration_lines", []) or default_narration_lines),
                scene_descriptions=scene_descriptions,
                child_age=child_age,
            )
            expected_narration_count = sum(1 for line in narration_lines if str(line or "").strip())
            studio_sfx_cues = dict(studio_plan.get("sfx_cues", {}) or {})
            studio_music_cues = dict(studio_plan.get("music_cues", {}) or {})
            studio_audio_locked = str(studio_plan.get("status") or "").strip().lower() not in {"", "not_run", "disabled"}
            if studio_audio_locked:
                enable_music = enable_music and bool(studio_plan.get("music_enabled", True))
                if enable_music and not studio_music_cues and frames:
                    arc_prompt = str(studio_plan.get("music_arc_prompt") or "").strip()
                    default_indices = [0]
                    if len(frames) > 1:
                        default_indices.append(len(frames) - 1)
                    for idx in dict.fromkeys(default_indices):
                        desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                        studio_music_cues[idx] = {
                            "prompt": arc_prompt or _build_music_prompt(desc, idx, len(frames))
                        }
            narration_volume, music_volume, sfx_volume, enable_ducking = _apply_storybook_mix_guidance(
                studio_plan.get("mix_guidance"),
                narration_volume=narration_volume,
                music_volume=music_volume,
                sfx_volume=sfx_volume,
                enable_ducking=enable_ducking,
                music_max=MUSIC_VOLUME_MAX,
                sfx_max=SFX_VOLUME_MAX,
            )

            narration_audio: list[Path | None] = [None] * len(narration_lines)
            scene_durations: list[float] = [float(page_seconds)] * len(narration_lines)
            if enable_tts and narration_lines:
                try:
                    local_tts_concurrency = int(os.environ.get("LOCAL_STORYBOOK_TTS_CONCURRENCY", "4"))
                except Exception:
                    local_tts_concurrency = 4
                tts_semaphore = asyncio.Semaphore(max(1, min(local_tts_concurrency, 6)))

                async def _render_local_tts(idx: int, line: str) -> tuple[int, Path | None, float]:
                    if not line:
                        return idx, None, 0.0
                    async with tts_semaphore:
                        audio_bytes = await asyncio.to_thread(_synthesize_tts_google, line)
                    if not audio_bytes:
                        return idx, None, 0.0
                    audio_path = tmp_path / f"narration_{idx:03d}.mp3"
                    audio_path.write_bytes(audio_bytes)
                    duration = await asyncio.to_thread(_ffprobe_duration, audio_path)
                    return idx, audio_path, duration

                tts_results = await asyncio.gather(
                    *(_render_local_tts(idx, line) for idx, line in enumerate(narration_lines)),
                    return_exceptions=True,
                )
                for result in tts_results:
                    if isinstance(result, Exception):
                        logger.warning("Local storybook narration TTS failed for %s: %s", session_id, result)
                        continue
                    idx, audio_path, duration = result
                    narration_audio[idx] = audio_path
                    if duration > 0:
                        scene_durations[idx] = max(page_seconds, duration + PAGE_SECONDS_NARRATION_BUFFER)
            rendered_narration_count = (
                expected_narration_count if not enable_tts else sum(1 for path in narration_audio if path is not None)
            )

            music_paths: list[Path | None] = []
            if enable_music and os.environ.get("ELEVENLABS_API_KEY"):
                music_paths = [None] * len(scene_durations)

                async def _render_music(idx: int, duration: float) -> tuple[int, Path | None]:
                    if studio_audio_locked and idx not in studio_music_cues:
                        return idx, None
                    desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                    cue = studio_music_cues.get(idx) or {}
                    prompt = str(cue.get("prompt") or "").strip() or _build_music_prompt(desc, idx, len(scene_durations))
                    audio_bytes = await _elevenlabs_generate_music(prompt, duration)
                    if not audio_bytes:
                        return idx, None
                    music_path = tmp_path / f"music_{idx:03d}.mp3"
                    music_path.write_bytes(audio_bytes)
                    return idx, music_path

                music_results = await asyncio.gather(
                    *(_render_music(idx, duration) for idx, duration in enumerate(scene_durations)),
                    return_exceptions=True,
                )
                for result in music_results:
                    if isinstance(result, Exception):
                        logger.warning("Local storybook music generation failed for %s: %s", session_id, result)
                        continue
                    idx, music_path = result
                    music_paths[idx] = music_path
            else:
                music_paths = [None] * len(scene_durations)

            sfx_paths: list[Path | None] = [None] * len(scene_durations)
            if enable_sfx and os.environ.get("ELEVENLABS_API_KEY") and scene_durations:
                selected_sfx_indices = sorted(studio_sfx_cues.keys())
                if not selected_sfx_indices and not studio_audio_locked:
                    selected_sfx_indices = _choose_sfx_indices(
                        total=len(scene_durations),
                        max_count=sfx_max,
                        descriptions=scene_descriptions,
                        min_score=sfx_min_score,
                        cooldown=sfx_cooldown,
                    )
                if selected_sfx_indices:
                    logger.info("Storybook SFX scenes selected: %s", selected_sfx_indices)
                async def _render_sfx(idx: int) -> tuple[int, Path | None]:
                    if idx < 0 or idx >= len(scene_durations):
                        return idx, None
                    desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                    cue = studio_sfx_cues.get(idx) or {}
                    prompt = str(cue.get("prompt") or "").strip() or _build_sfx_prompt(desc)
                    audio_bytes = await _elevenlabs_generate_sfx(prompt, 2.5)
                    if not audio_bytes:
                        return idx, None
                    sfx_path = tmp_path / f"sfx_{idx:03d}.mp3"
                    sfx_path.write_bytes(audio_bytes)
                    return idx, sfx_path

                sfx_results = await asyncio.gather(
                    *(_render_sfx(idx) for idx in selected_sfx_indices),
                    return_exceptions=True,
                )
                for result in sfx_results:
                    if isinstance(result, Exception):
                        logger.warning("Local storybook SFX generation failed for %s: %s", session_id, result)
                        continue
                    idx, sfx_path = result
                    if 0 <= idx < len(sfx_paths):
                        sfx_paths[idx] = sfx_path

            logger.info(
                "Local storybook audio plan for %s: narration=%d/%d music=%d/%d sfx=%d/%d",
                session_id,
                sum(1 for path in narration_audio if path is not None),
                len(narration_audio),
                sum(1 for path in music_paths if path is not None),
                len(music_paths),
                sum(1 for path in sfx_paths if path is not None),
                len(sfx_paths),
            )

            segments: list[Path] = []
            scene_shot_plans = plan_storyboard_shots(
                [str(scene_descriptions[idx]).strip() if idx < len(scene_descriptions) else "" for idx in range(len(frames))]
            )
            for idx, (frame, duration) in enumerate(zip(frames, scene_durations)):
                segment_path = tmp_path / f"segment_{idx:03d}.mp4"
                overlay_filters = _storybook_title_overlay_filters(title) if idx == 0 else None
                _render_storybook_cinematic_segment(
                    frame,
                    segment_path,
                    duration,
                    idx,
                    scene_shot_plans[idx] if idx < len(scene_shot_plans) else None,
                    overlay_filters,
                )
                segments.append(segment_path)

            concat_list = tmp_path / "concat.txt"
            concat_list.write_text("\n".join(f"file '{segment.name}'" for segment in segments))

            base_story_path = tmp_path / "storybook_base.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-c",
                    "copy",
                    str(base_story_path),
                ],
                check=True,
                capture_output=True,
            )

            output_path = tmp_path / "storybook_video.mp4"

            vf_parts = [
                "setsar=1",
            ]

            vf = ",".join(vf_parts)

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(base_story_path),
                    "-vf",
                    vf,
                    *_storybook_x264_args(),
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
            )

            def _build_padded_audio(paths: list[Path | None], durations: list[float], stem: str) -> Path | None:
                if not durations or not paths:
                    return None
                padded_paths: list[Path] = []
                for idx, duration in enumerate(durations):
                    src = paths[idx] if idx < len(paths) else None
                    padded_path = tmp_path / f"{stem}_pad_{idx:03d}.wav"
                    if src is None:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-f",
                                "lavfi",
                                "-i",
                                "anullsrc=channel_layout=stereo:sample_rate=44100",
                                "-t",
                                f"{duration:.3f}",
                                str(padded_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                    else:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(src),
                                "-filter_complex",
                                f"apad=pad_dur={duration:.3f},atrim=0:{duration:.3f}",
                                "-ar",
                                "44100",
                                "-ac",
                                "2",
                                str(padded_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                    padded_paths.append(padded_path)

                audio_concat_list = tmp_path / f"{stem}_concat.txt"
                audio_concat_list.write_text(
                    "\n".join(f"file '{p.name}'" for p in padded_paths)
                )
                audio_full = tmp_path / f"{stem}_full.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(audio_concat_list),
                        "-c:a",
                        "pcm_s16le",
                        str(audio_full),
                    ],
                    check=True,
                    capture_output=True,
                )
                return audio_full

            narration_full = (
                _build_padded_audio(narration_audio, scene_durations, "narration")
                if enable_tts and any(p for p in narration_audio)
                else None
            )
            music_full = (
                _build_padded_audio(music_paths, scene_durations, "music")
                if enable_music and any(p for p in music_paths)
                else None
            )
            sfx_full = (
                _build_padded_audio(sfx_paths, scene_durations, "sfx")
                if enable_sfx and any(p for p in sfx_paths)
                else None
            )

            audio_full_path: Path | None = None
            if narration_full and music_full and sfx_full:
                mixed_path = tmp_path / "audio_mix.wav"
                duck = (
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked];"
                    if enable_ducking
                    else ""
                )
                music_label = "ducked" if enable_ducking else "music"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(narration_full),
                        "-i",
                        str(music_full),
                        "-i",
                        str(sfx_full),
                        "-filter_complex",
                        (
                            f"[0:a]volume={narration_volume}[voice];"
                            f"[1:a]volume={music_volume}[music];"
                            f"[2:a]volume={sfx_volume}[sfx];"
                            f"{duck}"
                            f"[voice][{music_label}][sfx]amix=inputs=3:duration=shortest:dropout_transition=0"
                        ),
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif narration_full and music_full:
                mixed_path = tmp_path / "audio_mix.wav"
                duck = (
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked];"
                    if enable_ducking
                    else ""
                )
                music_label = "ducked" if enable_ducking else "music"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(narration_full),
                        "-i",
                        str(music_full),
                        "-filter_complex",
                        (
                            f"[0:a]volume={narration_volume}[voice];"
                            f"[1:a]volume={music_volume}[music];"
                            f"{duck}"
                            f"[voice][{music_label}]amix=inputs=2:duration=shortest:dropout_transition=0"
                        ),
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif narration_full and sfx_full:
                mixed_path = tmp_path / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(narration_full),
                        "-i",
                        str(sfx_full),
                        "-filter_complex",
                        f"[0:a]volume={narration_volume}[voice];[1:a]volume={sfx_volume}[sfx];[voice][sfx]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif narration_full:
                audio_full_path = narration_full
            elif music_full and sfx_full:
                mixed_path = tmp_path / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(music_full),
                        "-i",
                        str(sfx_full),
                        "-filter_complex",
                        f"[0:a]volume={music_volume}[music];[1:a]volume={sfx_volume}[sfx];[music][sfx]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif music_full:
                music_only = tmp_path / "music_only.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(music_full),
                        "-filter:a",
                        f"volume={music_volume}",
                        "-c:a",
                        "pcm_s16le",
                        str(music_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = music_only
            elif sfx_full:
                sfx_only = tmp_path / "sfx_only.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(sfx_full),
                        "-filter:a",
                        f"volume={sfx_volume}",
                        "-c:a",
                        "pcm_s16le",
                        str(sfx_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = sfx_only

            video_with_audio = output_path
            narration_audio_available = bool(narration_full and narration_full.exists())
            if narration_audio_available:
                narration_audio_available = _ffprobe_duration(narration_full) > 0.1
            audio_available = False
            if audio_full_path and audio_full_path.exists():
                audio_available = narration_audio_available
                merged_path = tmp_path / "storybook_with_audio.mp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(output_path),
                        "-i",
                        str(audio_full_path),
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "160k",
                        "-shortest",
                        "-movflags",
                        "+faststart",
                        str(merged_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                video_with_audio = merged_path

            final_path = video_with_audio
            if enable_captions and any(narration_lines):
                # Build upper-frame book-style drawtext (per-page, time-gated).
                drawtext_parts: list[str] = []
                t = 0.0
                for duration, line in zip(scene_durations, narration_lines):
                    if not line:
                        t += duration
                        continue
                    wrapped = _wrap_caption(line, width=30)
                    escaped = _ffmpeg_escape(wrapped)
                    draw = (
                        f"drawtext=text='{escaped}'"
                        f":fontcolor=white"
                        f":fontsize=46"
                        f":x=(w-text_w)/2"
                        f":y=h*0.10"
                        f":box=1"
                        f":boxcolor=0x00000099"
                        f":boxborderw=18"
                        f":font=Sans"
                        f":shadowcolor=black:shadowx=2:shadowy=2"
                        f":line_spacing=8"
                        f":enable='between(t,{t:.3f},{t+duration:.3f})'"
                    )
                    drawtext_parts.append(draw)
                    t += duration
                if drawtext_parts:
                    captioned_path = tmp_path / "storybook_captioned.mp4"
                    try:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(video_with_audio),
                                "-vf",
                                ",".join(drawtext_parts),
                                "-c:a",
                                "copy",
                                "-movflags",
                                "+faststart",
                                str(captioned_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                        final_path = captioned_path
                    except Exception:
                        final_path = video_with_audio

            video_bytes = final_path.read_bytes()
            final_has_audio_stream = _ffprobe_has_audio_stream(final_path)
            final_video_duration_sec = _ffprobe_duration(final_path)
            audio_expected = bool(enable_tts and narration_required_default())
            theater_release_ready, release_gate_issues = storybook_release_gate(
                scene_count=len(frames),
                final_video_duration_sec=final_video_duration_sec,
                expected_audio=audio_expected,
                audio_available=audio_available,
                final_has_audio_stream=final_has_audio_stream,
                expected_narration_count=expected_narration_count,
                rendered_narration_count=rendered_narration_count,
                shot_types=[plan.shot_type for plan in scene_shot_plans],
            )
            if not theater_release_ready:
                raise RuntimeError(
                    "Local storybook assembly failed the release gate: "
                    f"duration={final_video_duration_sec:.2f}s "
                    f"audio_expected={audio_expected} "
                    f"audio_available={audio_available} "
                    f"final_has_audio_stream={final_has_audio_stream} "
                    f"issues={'; '.join(release_gate_issues)}"
                )
            backend_base = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
            media_id = store_media(video_bytes, "video/mp4")
            video_url = f"{backend_base}/api/scene/{media_id}"
            trading_card_url = str(state.get("trading_card_url", "")).strip() or None
            publish_session_event(
                session_id,
                theater_mode_event(
                    mp4_url=video_url,
                    trading_card_url=trading_card_url,
                    narration_lines=[line for line in narration_lines if line],
                    audio_available=audio_available,
                    story_title=title,
                    child_name=str(state.get("child_name", "")).strip() or None,
                    story_phase="theater",
                ).model_dump(mode="json"),
            )
            logger.info("Local storybook assembled for session %s", session_id)
    except Exception as exc:
        logger.warning("Local storybook assembly failed: %s", exc, exc_info=True)


async def _trigger_ffmpeg_job(session_id: str) -> None:
    """Invokes the FFmpeg Cloud Run Job via Cloud Run Jobs API."""
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    job_name = os.environ.get("FFMPEG_JOB_NAME", "storyteller-ffmpeg-assembler")

    import google.auth
    import google.auth.transport.requests

    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)

        endpoint = (
            f"https://run.googleapis.com/v2/projects/{project}/locations/{location}"
            f"/jobs/{job_name}:run"
        )
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        }
        payload = {"overrides": {"containerOverrides": [{"args": [f"--session_id={session_id}"]}]}}

        async with httpx.AsyncClient() as client:
            resp = await client.post(endpoint, headers=headers, json=payload, timeout=30)
            if resp.status_code >= 400:
                logger.error(f"FFmpeg trigger failed (HTTP {resp.status_code}): {resp.text}")
                # Clear the lock so they can retry after fixing config/IAM
                _assembled_sessions.discard(session_id)
            resp.raise_for_status()
        logger.info("FFmpeg Cloud Run Job triggered for session %s", session_id)
    except Exception as exc:
        logger.warning("FFmpeg trigger failed gracefully for %s: %s", session_id, exc)
        _assembled_sessions.discard(session_id)
        publish_session_event(
            session_id,
            {
                "type": "error",
                "payload": {"message": "Oops, my movie wand is recharging. Let's keep telling the story!"},
            },
        )


async def sync_room_lights(
    hex_color: str,
    scene_description: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """Syncs smart-home lights with a per-session cooldown and client fallback."""
    args = LightArgs(hex_color=hex_color.strip(), scene_description=scene_description.strip())
    session_id = _session_id_from_context(tool_context)
    tool_state = _load_tool_state(tool_context)

    try:
        normalized_hex = _normalize_hex_color(args.hex_color)
    except ValueError as exc:
        return f"System: {exc}"

    last_active_hex = str(tool_state.get("last_active_hex_color", "")).strip().upper()
    if last_active_hex == normalized_hex.upper():
        return f"System: Room lights already match {normalized_hex}."

    cfg = get_session_iot_config(session_id) if session_id else {}
    session_ha_url = str(cfg.get("ha_url", "")).strip()
    ha_url = session_ha_url or os.environ.get("HOME_ASSISTANT_URL", "")
    ha_token = str(cfg.get("ha_token", "")).strip() or os.environ.get("HOME_ASSISTANT_TOKEN", "")
    ha_entity = str(cfg.get("ha_entity", "")).strip() or os.environ.get("HA_LIGHT_ENTITY", "light.living_room")

    if not ha_url or not ha_token:
        return f"System: IoT lights not configured, skipping sync for color {normalized_hex}."

    now = time.monotonic()
    cooldown_key = _session_light_cooldown_key(session_id)
    last_call = _last_light_call_by_session.get(cooldown_key, 0.0)
    if now - last_call < _LIGHT_COOLDOWN_SECONDS:
        remaining = _LIGHT_COOLDOWN_SECONDS - (now - last_call)
        return f"System: Lighting cooldown active. Next change available in {remaining:.1f}s."
    _last_light_call_by_session[cooldown_key] = now

    r, g, b = _rgb_from_hex(normalized_hex)
    brightness = 200
    transition = 2
    payload = {
        "entity_id": ha_entity,
        "rgb_color": [r, g, b],
        "brightness": brightness,
        "transition": transition,
    }
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }
    event_payload = {
        "hex_color": normalized_hex,
        "rgb_color": [r, g, b],
        "entity": ha_entity,
        "brightness": brightness,
        "transition": transition,
        "scene_description": args.scene_description,
        "backend_applied": False,
        "client_should_apply": False,
    }

    if session_id and session_ha_url and _ha_url_is_private_or_local(ha_url):
        event_payload["client_should_apply"] = True
        _publish_lighting_command(session_id, event_payload)
        return f"System: Room lights queued for browser-side sync to {normalized_hex}."

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ha_url.rstrip('/')}/api/services/light/turn_on",
                headers=headers,
                json=payload,
                timeout=3.0,
            )
            resp.raise_for_status()

        if session_id:
            event_payload["backend_applied"] = True
            _publish_lighting_command(session_id, event_payload)

        _remember_last_light_color(tool_context, session_id, normalized_hex)
        return f"System: Room lights synced to {normalized_hex}."
    except Exception as exc:
        logger.warning("IoT light sync failed gracefully: %s", exc)
        if session_id:
            event_payload["client_should_apply"] = True
            event_payload["backend_error"] = str(exc)[:240]
            _publish_lighting_command(session_id, event_payload)
        return "System: IoT light sync failed gracefully."
