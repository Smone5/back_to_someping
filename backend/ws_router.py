"""WebSocket router for the Interactive Storyteller live session."""

from __future__ import annotations

import base64
import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import timedelta
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect
from google.adk.agents import LiveRequestQueue
from google.adk.runners import Runner
from google.cloud import storage
from google.genai import types as genai_types

from .audio import passes_noise_gate, scrub_pii
from agent.tools import assemble_story_video, VisualArgs, _run_visual_pipeline
from .event_bus import (
    clear_session,
    get_session_queue,
    publish_session_event,
    replace_session_queue,
    schedule_background_task,
    set_session_iot_config,
)
from .schemas import (
    ClientCommand,
    ClientCommandType,
    ServerEvent,
    ServerEventType,
    music_command_event,
    theater_mode_event,
)

logger = logging.getLogger(__name__)

_rewind_locks: dict[str, asyncio.Lock] = {}
_active_websockets: dict[str, WebSocket] = {}
_active_connection_ids: dict[str, str] = {}
_greeting_sent_sessions: set[str] = set()
_awaiting_greeting_sessions: set[str] = set()
_opening_phase_sessions: set[str] = set()
_assistant_speaking_sessions: set[str] = set()
_assistant_speaking_since: dict[str, float] = {}
_ending_story_sessions: set[str] = set()
_activity_active_sessions: set[str] = set()
_activity_last_change: dict[str, float] = {}
_live_request_debug: dict[str, deque[dict[str, Any]]] = {}
_audio_seen_this_turn: set[str] = set()
# Coordination: downstream loop sets an Event when waiting for image,
# _forward_session_events signals it when image arrives.
_pending_image_events: dict[str, asyncio.Event] = {}
# Track when image generation was requested for timing analysis.
_scene_gen_requested_at: dict[str, float] = {}
# Track sessions where an early fallback image was already started this turn
# so we don't double-generate if the model also calls the tool.
_early_fallback_started: set[str] = set()
# Track if a session has successfully received at least one image.
_session_has_any_image: set[str] = set()
_ALLOWED_ORIGINS = {
    origin
    for origin in {
        os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000"),
        os.environ.get("PROD_FRONTEND_ORIGIN", ""),
    }
    if origin
}
_HEARTBEAT_INTERVAL = 10
_MIN_STORY_TURNS = 6
_MAX_STORY_TURNS_HARD = 20
_ASPECT_RATIO_OPTIONS: list[tuple[str, float]] = [
    ("1:1", 1.0),
    ("4:5", 4 / 5),
    ("3:4", 3 / 4),
    ("2:3", 2 / 3),
    ("9:16", 9 / 16),
    ("4:3", 4 / 3),
    ("3:2", 3 / 2),
    ("16:9", 16 / 9),
    ("21:9", 21 / 9),
]

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


def _record_live_request(session_id: str, kind: str, info: dict[str, Any]) -> None:
    """Keep a short ring buffer of outbound live requests for debugging."""
    try:
        buf = _live_request_debug.get(session_id)
        if buf is None:
            buf = deque(maxlen=20)
            _live_request_debug[session_id] = buf
        payload = dict(info)
        payload["kind"] = kind
        payload["ts"] = time.monotonic()
        buf.append(payload)
    except Exception:
        pass


def _dump_live_request_debug(session_id: str) -> None:
    try:
        buf = _live_request_debug.get(session_id)
        if not buf:
            return
        logger.warning("Live request debug buffer for %s: %s", session_id, list(buf))
    except Exception:
        pass


def _connection_is_current(
    session_id: str,
    connection_id: str,
    websocket: WebSocket | None = None,
) -> bool:
    """Returns True only for the currently active websocket owner of a session."""
    if _active_connection_ids.get(session_id) != connection_id:
        return False
    if websocket is not None and _active_websockets.get(session_id) is not websocket:
        return False
    return True


def _send_live_content(session_id: str, live_queue: LiveRequestQueue, text: str) -> None:
    if not text:
        return
    _record_live_request(session_id, "content", {"text_len": len(text), "preview": text[:140]})
    live_queue.send_content(genai_types.Content(parts=[genai_types.Part(text=text)]))


def _send_live_realtime(session_id: str, live_queue: LiveRequestQueue, blob: genai_types.Blob) -> None:
    size = 0
    try:
        size = len(blob.data) if blob and blob.data else 0
    except Exception:
        size = 0
    _record_live_request(session_id, "audio", {"bytes": size, "mime": getattr(blob, "mime_type", "")})
    live_queue.send_realtime(blob)


def _resolve_image_prefs_from_state(state: dict[str, Any]) -> tuple[str, str, str]:
    aspect_ratio = str(state.get("preferred_aspect_ratio", "16:9"))
    if aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
        aspect_ratio = "16:9"
    image_size = os.environ.get("IMAGE_SIZE", "").strip() or str(state.get("preferred_image_size", "512px"))
    if image_size not in _ALLOWED_IMAGE_SIZES:
        image_size = "512px"
    image_model = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image-preview").strip()
    if not image_model:
        image_model = "gemini-3.1-flash-image-preview"
    return aspect_ratio, image_size, image_model


def _fallback_scene_prompt(assistant_text: str, child_text: str, state: dict[str, Any]) -> str:
    text = (assistant_text or "").strip()
    if not text:
        text = (child_text or "").strip()
    if not text:
        text = str(state.get("story_summary", "")).strip()
    if not text:
        text = str(state.get("current_scene_description", "A magical story scene")).strip()
    # Remove choice line to keep visuals focused on the scene.
    text = re.split(r"🌟\s*What should we do\?", text, maxsplit=1)[0].strip()
    text = _CTRL_TOKEN_RE.sub("", text).strip()
    if not text:
        text = "A magical story scene"
    continuity = str(state.get("story_summary", "")).strip()
    if continuity:
        continuity = re.sub(r"\s+", " ", continuity).strip()
        tail = continuity[-260:]
        if tail and tail not in text:
            text = f"{text}. Keep temporal/character continuity with: {tail}."
    return text[:600]


async def _trigger_fallback_scene(
    session_id: str,
    assistant_text: str,
    child_text: str,
    runner: Runner,
    websocket: WebSocket,
    user_id: str,
) -> None:
    try:
        session = await runner.session_service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        session = None
    state = session.state if session else {}
    description = _fallback_scene_prompt(assistant_text, child_text, state)
    aspect_ratio, image_size, image_model = _resolve_image_prefs_from_state(state)
    try:
        args = VisualArgs(
            description=description,
            negative_prompt=VisualArgs.model_fields["negative_prompt"].default,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            image_model=image_model,
        )
    except Exception:
        return
    # Always send placeholder signal so frontend can show loading indicator.
    should_send_placeholder = True
    if should_send_placeholder:
        placeholder_svg = _build_scene_svg_data_url(description)
        try:
            await websocket.send_text(
                json.dumps({
                    "type": "video_ready",
                    "payload": {
                        "url": placeholder_svg,
                        "description": description,
                        "media_type": "image",
                        "is_placeholder": True,
                    },
                })
            )
        except Exception:
            pass
    logger.info("Triggering fallback scene generation for session %s: %s", session_id, description[:100])
    _scene_gen_requested_at[session_id] = time.monotonic()
    _record_live_request(session_id, "fallback_scene", {"desc": description[:140]})
    schedule_background_task(_run_visual_pipeline(args=args, session_id=session_id))


def _closest_aspect_ratio(width: float, height: float) -> str:
    if width <= 0 or height <= 0:
        return "16:9"
    ratio = width / height
    return min(_ASPECT_RATIO_OPTIONS, key=lambda item: abs(item[1] - ratio))[0]


def _preferred_image_size(width: float, height: float, is_compact: bool = False) -> str:
    max_dim = max(width, height)
    if is_compact:
        if max_dim <= 720:
            return "512px"
        if max_dim <= 1100:
            return "512px"
        if max_dim <= 1600:
            return "1K"
        return "2K"
    if max_dim <= 900:
        return "512px"
    if max_dim <= 1400:
        return "1K"
    return "2K"


def _make_thumbnail_b64(image_bytes: bytes, max_side: int = 384) -> tuple[str, str] | None:
    try:
        from PIL import Image
    except Exception:
        return None

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


def _read_story_turn_limit() -> int:
    raw = os.environ.get("MAX_STORY_TURNS", "10")
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 10
    return max(_MIN_STORY_TURNS, min(parsed, _MAX_STORY_TURNS_HARD))


_MAX_STORY_TURNS = _read_story_turn_limit()
_story_turn_limit_sessions: set[str] = set()
_story_turn_limit_notified_sessions: set[str] = set()


def _is_allowed_origin(origin: str) -> bool:
    if not origin:
        return True
    if origin in _ALLOWED_ORIGINS:
        return True
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    if not host.startswith("storyteller-frontend-"):
        return False
    return host.endswith(".run.app") or host.endswith(".a.run.app")


def _extract_json_objects(buffer: str) -> tuple[list[dict[str, Any]], str]:
    """Parses complete JSON objects from a potentially fragmented stream buffer."""
    decoder = json.JSONDecoder()
    index = 0
    size = len(buffer)
    objects: list[dict[str, Any]] = []

    while index < size:
        while index < size and buffer[index].isspace():
            index += 1
        if index >= size:
            return objects, ""
        try:
            obj, end = decoder.raw_decode(buffer, index)
        except json.JSONDecodeError:
            return objects, buffer[index:]
        if isinstance(obj, dict):
            objects.append(obj)
        index = end

    return objects, ""


async def _mutate_state(
    runner: Runner,
    user_id: str,
    session_id: str,
    mutator,
) -> None:
    """Best-effort state mutation on in-memory sessions for live UX updates."""
    service = runner.session_service
    try:
        # InMemorySessionService stores canonical sessions here.
        storage_session = service.sessions["storyteller"][user_id][session_id]  # type: ignore[attr-defined]
        before_state = dict(storage_session.state)
        mutator(storage_session.state)
        
        # Log every state mutation for deep observability.
        changes = {
            k: v for k, v in storage_session.state.items() 
            if k not in before_state or before_state[k] != v
        }
        if changes and "state_snapshots" not in changes:  # don't spam the console with massive snapshots
            logger.info(f"🔍 STATE MUTATION [{session_id[:8]}]: {changes}")
        return
    except Exception:
        pass

    # Fallback for other services (may not persist if service returns copies).
    try:
        session = await service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
        )
        if session:
            mutator(session.state)
    except Exception as exc:
        logger.debug("State mutation fallback failed: %s", exc)

async def _prune_session_history(runner: Runner, user_id: str, session_id: str) -> None:
    """Aggressively clears session events to recover from Live API 1007/1011 loops."""
    try:
        service = runner.session_service
        # InMemorySessionService stores canonical sessions here.
        if hasattr(service, "sessions") and "storyteller" in service.sessions: # type: ignore[attr-defined]
            if user_id in service.sessions["storyteller"] and session_id in service.sessions["storyteller"][user_id]: # type: ignore[attr-defined]
                storage_session = service.sessions["storyteller"][user_id][session_id] # type: ignore[attr-defined]
                logger.warning("PRUNING session events for %s to recover from error.", session_id)
                storage_session.events = []
    except Exception as exc:
        logger.error("Failed to prune session history: %s", exc)


def _take_snapshot(state: dict[str, Any]) -> None:
    snapshots = list(state.get("state_snapshots", []))
    snapshot = {k: v for k, v in state.items() if k != "state_snapshots"}
    snapshots.append(snapshot)
    state["state_snapshots"] = snapshots[-40:]


def _rollback_snapshot(state: dict[str, Any]) -> None:
    snapshots = list(state.get("state_snapshots", []))
    if len(snapshots) < 2:
        return
    previous = dict(snapshots[-2])
    previous["state_snapshots"] = snapshots[:-1]
    
    # Prevent rollback from erasing images that finished in the background AFTER the snapshot was taken.
    # CRITICAL: We only preserve the *actual completed URLs*. We MUST NOT preserve 
    # "current_scene_description" or "current_scene_base_description", otherwise when the agent 
    # retries the turn after a rollback, it sees the description already exists in state and 
    # skips calling generate_scene_visuals!
    preserve_keys = [
        "scene_asset_urls", "scene_asset_gcs_uris", "generated_asset_urls"
    ]
    preserved_data = {k: state[k] for k in preserve_keys if k in state}
    
    state.clear()
    state.update(previous)
    state.update(preserved_data)


def _append_story_summary(state: dict[str, Any], assistant_text: str) -> None:
    existing = str(state.get("story_summary", "")).strip()
    assistant_text = assistant_text.strip()
    # Strip any hidden control tokens that can leak from live transcription.
    if existing:
        existing = _CTRL_TOKEN_RE.sub("", existing).strip()
        existing = re.sub(r"\s+", " ", existing).strip()
    if assistant_text:
        assistant_text = _CTRL_TOKEN_RE.sub("", assistant_text).strip()
        assistant_text = re.sub(r"\s+", " ", assistant_text).strip()
    if not assistant_text:
        return
    if existing and existing.endswith(assistant_text):
        return
    if existing and assistant_text in existing[-240:]:
        return
    combined = f"{existing} {assistant_text}".strip()
    state["story_summary"] = combined[-1200:]


_CTRL_TOKEN_RE = re.compile(r"<ctrl\d+>", flags=re.IGNORECASE)
_NAME_PHRASE_RE = re.compile(
    r"(?:\bmy name(?: is|'s|’s)\b|\bi am\b|\bi['’]m\b)\s+(?:(?:uh|um|erm|uhh|umm)\s+)?([A-Za-z][A-Za-z'\-]{1,23})\b",
    flags=re.IGNORECASE,
)
_NAME_REJECT_WORDS = {
    "a", "an", "and", "are", "at", "be", "can", "cool", "dad", "do", "does", "done",
    "friend", "go", "good", "hello", "hey", "hi", "i", "im", "i'm", "is", "it",
    "its", "it's", "let", "me", "mom", "my", "name", "no", "not", "now", "ok",
    "okay", "one", "please", "right", "say", "she", "start", "stop", "sure", "that",
    "the", "there", "this", "uh", "um", "we", "well", "what", "who", "why", "will",
    "yes", "you", "your", "here", "there", "their", "they", "them", "he", "her", "him",
}
_NAME_NEGATION_RE = re.compile(
    r"\b(nope|nah|not|isn't|is not|that's not|that is not|wrong)\b",
    flags=re.IGNORECASE,
)
_NAME_AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|right|correct|that's right|that is right|sure|ok|okay|uh-huh|mhm)\b",
    flags=re.IGNORECASE,
)
_CAMERA_PROMPT_RE = re.compile(
    r"\b(show|share|picture|photo|camera|toy|stuffed|drawing|book|character)\b",
    flags=re.IGNORECASE,
)
_CAMERA_YES_RE = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|please|i do|i want|i wanna|let's|lets)\b",
    flags=re.IGNORECASE,
)
_CAMERA_SKIP_RE = re.compile(
    r"\b(skip|no thanks|no thank you|not now|don't want|do not want|nothing to show)\b",
    flags=re.IGNORECASE,
)
_STORY_INTENT_RE = re.compile(
    r"\b(story|adventure|let's|lets|go to|go into|make|create|tell me|tell a story|i want|i wanna|can we)\b",
    flags=re.IGNORECASE,
)
_STORY_PROMPT_RE = re.compile(
    r"\b(what kind of story|what story|what adventure|what should we do|what do you want to do)\b",
    flags=re.IGNORECASE,
)


def _should_shortcircuit_story(text: str, state: dict[str, Any]) -> bool:
    # Legacy rigid override disabled: Amelia's system prompt now natively handles
    # complex logic like skipping name confirmation or jumping straight to the story.
    return False


def _should_skip_camera_for_story(text: str, state: dict[str, Any]) -> bool:
    # Legacy rigid override disabled for the same reason.
    return False


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _is_meaningful_text(text: str | None) -> bool:
    if not text:
        return False
    cleaned = _CTRL_TOKEN_RE.sub("", text).strip()
    if not cleaned:
        return False
    lowered = cleaned.lower()
    noise_tokens = {
        "[noise]",
        "noise",
        "[silence]",
        "silence",
        "static",
        "background",
        "uh",
        "um",
        "hmm",
        "mmm",
        "ah",
        "ahh",
    }
    if lowered in noise_tokens:
        return False
    # Filter ultra-short non-ASCII noise (e.g. "うん", "มัด") that often comes
    # from background audio or TTS bleed during loading.
    if len(cleaned) <= 3 and not any(ch.isascii() and ch.isalnum() for ch in cleaned):
        return False
    # Treat punctuation-only transcripts (e.g. "."/"...") as non-meaningful.
    alpha_num = any(ch.isalnum() for ch in cleaned)
    return alpha_num


def _is_retryable_live_error(exc: Exception) -> bool:
    """Returns True when a live-stream failure looks transient/retryable."""
    status_code = getattr(exc, "status_code", None)
    if status_code in {1007, 1011, 500, 502, 503, 504}:
        return True

    reason = str(getattr(exc, "reason", "")).lower()
    message = str(exc).lower()
    retryable_markers = (
        "1007",
        "1011",
        "invalid argument",
        "internal error occurred",
        "connectionclosederror",
        "temporarily unavailable",
        "service unavailable",
        "operation is not implemented",
        "not supported",
        "not enabled",
    )
    if any(marker in reason for marker in retryable_markers):
        return True
    if any(marker in message for marker in retryable_markers):
        return True
    return False


def _build_scene_svg_data_url(seed_text: str) -> str:
    safe_text = html.escape((seed_text or "A magical story scene")[:220])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#12042c"/>
      <stop offset="55%" stop-color="#2d1461"/>
      <stop offset="100%" stop-color="#0f3a63"/>
    </linearGradient>
    <radialGradient id="halo" cx="55%" cy="40%" r="48%">
      <stop offset="0%" stop-color="#ffd166" stop-opacity="0.42"/>
      <stop offset="100%" stop-color="#ffd166" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <rect width="1280" height="720" fill="url(#halo)"/>
  <circle cx="260" cy="560" r="170" fill="#ff79c6" opacity="0.5"/>
  <circle cx="640" cy="600" r="230" fill="#68f7cf" opacity="0.4"/>
  <circle cx="1020" cy="560" r="180" fill="#6fc8ff" opacity="0.48"/>
  <text x="640" y="340" text-anchor="middle" font-family="sans-serif" font-size="36" fill="#ffd166" opacity="0.85">✨ Creating magic... ✨</text>
  <circle cx="300" cy="200" r="4" fill="#ffd166" opacity="0">
    <animate attributeName="opacity" values="0;1;0" dur="2s" repeatCount="indefinite"/>
  </circle>
  <circle cx="640" cy="150" r="5" fill="#ff79c6" opacity="0">
    <animate attributeName="opacity" values="0;1;0" dur="1.5s" begin="0.5s" repeatCount="indefinite"/>
  </circle>
  <circle cx="980" cy="220" r="4" fill="#6fc8ff" opacity="0">
    <animate attributeName="opacity" values="0;1;0" dur="2.2s" begin="1s" repeatCount="indefinite"/>
  </circle>
  <circle cx="450" cy="100" r="3" fill="#68f7cf" opacity="0">
    <animate attributeName="opacity" values="0;1;0" dur="1.8s" begin="0.3s" repeatCount="indefinite"/>
  </circle>
  <circle cx="830" cy="130" r="4" fill="#ffd166" opacity="0">
    <animate attributeName="opacity" values="0;1;0" dur="2.5s" begin="0.8s" repeatCount="indefinite"/>
  </circle>
</svg>"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _infer_music_mood(text: str) -> tuple[str, int]:
    t = text.lower()
    if any(k in t for k in ("secret", "mystery", "dark", "cave", "whisper", "hidden")):
        return "suspenseful", 6
    if any(k in t for k in ("win", "victory", "celebrate", "hooray", "triumph")):
        return "triumphant_celebration", 7
    if any(k in t for k in ("magic", "sparkly", "glow", "wizard", "enchanted")):
        return "magical", 5
    return "playful", 5


def _extract_first_media_url(value: Any) -> str | None:
    """Best-effort extraction of a media URL from nested tool response payloads."""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith(("http://", "https://", "gs://", "data:audio", "data:video")):
            return candidate
        return None

    if isinstance(value, dict):
        preferred_keys = (
            "url",
            "audio_url",
            "audioUrl",
            "signed_url",
            "signedUrl",
            "gcs_url",
            "gcsUri",
            "uri",
            "file_url",
            "sfx_url",
            "file_path",
            "file",
            "resource_uri",
        )
        for key in preferred_keys:
            found = _extract_first_media_url(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = _extract_first_media_url(nested)
            if found:
                return found
        return None

    if isinstance(value, list):
        for item in value:
            found = _extract_first_media_url(item)
            if found:
                return found
        return None

    return None


def _normalize_client_media_url(url: str) -> str:
    """Converts gs:// URLs to signed URLs when possible so the browser can fetch them."""
    if not url.startswith("gs://"):
        return url
    path = url[5:]
    parts = path.split("/", 1)
    if len(parts) != 2:
        return url
    bucket_name, blob_path = parts
    try:
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_path)
        return blob.generate_signed_url(version="v4", expiration=timedelta(hours=2), method="GET")
    except Exception:
        return f"https://storage.googleapis.com/{bucket_name}/{blob_path}"


def _extract_child_name(text: str | None) -> str | None:
    if not text:
        return None

    cleaned = _CTRL_TOKEN_RE.sub("", text).strip()
    if not cleaned:
        return None

    match = _NAME_PHRASE_RE.search(cleaned)
    if match:
        candidate = match.group(1).strip(" .,!?:;").title()
        if candidate.lower() in _NAME_REJECT_WORDS:
            return None
        return candidate

    # If the child only says a single token (e.g. "Aaron"), treat it as a name.
    token_match = re.fullmatch(r"[A-Za-z][A-Za-z'\-]{1,23}", cleaned)
    if token_match:
        candidate = token_match.group(0).title()
        if candidate.lower() in {
            "no", "nope", "nah", "yes", "yeah", "yep", "ok", "okay", "sure", "right",
        }:
            return None
        if candidate.lower() in _NAME_REJECT_WORDS:
            return None
        return candidate

    return None


async def _update_child_name_state(
    runner: Runner,
    user_id: str,
    session_id: str,
    utterance_text: str | None,
    input_finished: bool,
) -> None:
    """Tracks pending name + confirmation without letting wrong names stick."""
    if not utterance_text:
        return
    session = await runner.session_service.get_session(
        app_name="storyteller",
        user_id=user_id,
        session_id=session_id,
    )
    if not session:
        return

    if not input_finished:
        return

    state = session.state
    current = str(state.get("child_name", "friend")).strip()
    if current and current.lower() != "friend":
        if not bool(state.get("name_confirmed", False)):
            state["name_confirmed"] = True
            state["pending_child_name"] = ""
            state["name_confirmation_prompted"] = False
        return

    if bool(state.get("name_confirmed", False)):
        return

    pending = str(state.get("pending_child_name", "")).strip()
    confirmation_prompted = bool(state.get("name_confirmation_prompted", False))
    cleaned = _CTRL_TOKEN_RE.sub("", utterance_text).strip().lower()
    detected = _extract_child_name(utterance_text)
    negated = bool(_NAME_NEGATION_RE.search(cleaned))
    affirmed = bool(_NAME_AFFIRM_RE.search(cleaned))
    try:
        turn_number = int(state.get("turn_number", 1))
    except Exception:
        turn_number = 1

    def _apply_update(
        pending_name: str | None,
        confirmed: bool,
    ) -> None:
        def _mutator(s: dict[str, Any]) -> None:
            if pending_name is not None:
                s["pending_child_name"] = pending_name
            if confirmed:
                s["child_name"] = pending_name or s.get("child_name", "friend")
                s["name_confirmed"] = True
                s["pending_child_name"] = ""
                s["name_confirmation_prompted"] = False
                if s.get("camera_stage", "none") == "none":
                    s["camera_stage"] = "done"
                    s["camera_skipped"] = True
            else:
                s["name_confirmed"] = False
                s["name_confirmation_prompted"] = False
                if s.get("camera_stage", "none") == "pending":
                    s["camera_stage"] = "done"
                    s["camera_skipped"] = True

        return _mutator

    if pending:
        # Child corrected the name.
        if detected and detected.lower() != pending.lower():
            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_apply_update(detected, False),
            )
            return
        # Explicit "no" without a new name.
        if negated and not affirmed:
            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_apply_update(detected or "", False),
            )
            return
        # Confirmation via "yes" or repeating the name (even if confirmation prompt tracking missed).
        if affirmed or (detected and detected.lower() == pending.lower()):
            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_apply_update(pending, True),
            )
            return
    else:
        if detected:
            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_apply_update(detected, False),
            )
            return

        # If the child says anything else on the early turns, stop blocking on the name.
        if (
            turn_number <= 2
            and _is_meaningful_text(utterance_text)
            and not affirmed
            and not negated
        ):
            def _skip_name_block(s: dict[str, Any]) -> None:
                s["name_confirmed"] = True
                s["pending_child_name"] = ""
                s["name_confirmation_prompted"] = False
                # Skip optional camera prompt to keep flow smooth for young kids.
                s["camera_stage"] = "done"
                s["camera_skipped"] = True

            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_skip_name_block,
            )


async def _notify_story_limit_once(websocket: WebSocket, session_id: str, max_turns: int) -> None:
    if session_id in _story_turn_limit_notified_sessions:
        return
    _story_turn_limit_notified_sessions.add(session_id)
    await websocket.send_text(
        ServerEvent(
            type=ServerEventType.ERROR,
            payload={
                "message": (
                    f"We finished this story after {max_turns} magical turns. "
                    "You can start a new adventure anytime."
                )
            },
        ).model_dump_json()
    )


async def handle_storyteller_ws(websocket: WebSocket, runner: Runner) -> None:
    """Main WebSocket handler for a single storytelling session."""
    origin = websocket.headers.get("origin", "")
    if origin and not _is_allowed_origin(origin):
        logger.warning("Rejected WebSocket from disallowed origin: %s", origin)
        await websocket.close(code=4003)
        return

    await websocket.accept()

    session_id = websocket.query_params.get("session_id") or str(uuid.uuid4())
    user_id = websocket.query_params.get("user_id", "anonymous")
    connection_id = uuid.uuid4().hex

    # Singleton WebSocket Guard: Forcibly close any ghost connection for this session ID.
    # This prevents multiple connections from splitting events (e.g. video_ready vs turn_complete).
    old_ws = _active_websockets.get(session_id)
    _active_websockets[session_id] = websocket
    _active_connection_ids[session_id] = connection_id
    replace_session_queue(session_id)
    if old_ws is not None and old_ws is not websocket:
        logger.warning("Session %s reconnected. Closing old ghost connection.", session_id)
        try:
            await old_ws.close(code=1000, reason="Replaced by new connection")
        except Exception:
            pass

    session = await runner.session_service.get_session(
        app_name="storyteller",
        user_id=user_id,
        session_id=session_id,
    )
    if session is None:
        session = await runner.session_service.create_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
            state={
                "child_name": "friend",
                "pending_child_name": "",
                "name_confirmed": False,
                "name_confirmation_prompted": False,
                "camera_stage": "done",
                "camera_received": False,
                "camera_skipped": True,
                "camera_prompt_nudged": False,
                "camera_prompt_forced": False,
                "story_started": False,
                "awaiting_story_choice": False,
                "pending_story_hint": "",
                "story_summary": "",
                "sidekick_description": "a magical companion",
                "character_facts": "",
                "character_facts_list": [],
                "generated_asset_urls": [],
                "scene_asset_urls": [],
                "turn_number": 1,
                "max_story_turns": _MAX_STORY_TURNS,
                "max_story_turns_minus_one": max(3, _MAX_STORY_TURNS - 1),
                "story_turn_limit_reached": False,
                "state_snapshots": [],
                "current_scene_description": "No image yet — the story is just beginning!",
            },
        )
        _opening_phase_sessions.add(session_id)
    else:
        defaults = {
            "child_name": "friend",
            "pending_child_name": "",
            "name_confirmed": False,
            "name_confirmation_prompted": False,
            "camera_stage": "done",
            "camera_received": False,
            "camera_skipped": True,
            "camera_prompt_nudged": False,
            "camera_prompt_forced": False,
            "camera_prompt_count": 0,
            "story_started": False,
            "awaiting_story_choice": False,
            "pending_story_hint": "",
            "story_summary": "",
            "sidekick_description": "a magical companion",
            "character_facts": "",
            "character_facts_list": [],
            "generated_asset_urls": [],
            "scene_asset_urls": [],
            "turn_number": 1,
            "max_story_turns": _MAX_STORY_TURNS,
            "max_story_turns_minus_one": max(3, _MAX_STORY_TURNS - 1),
            "story_turn_limit_reached": False,
            "state_snapshots": [],
            "current_scene_description": "No image yet — the story is just beginning!",
        }
        for key, value in defaults.items():
            if key not in session.state:
                session.state[key] = value
        # Camera feature is disabled for now — force it off even for existing sessions.
        session.state["camera_stage"] = "done"
        session.state["camera_skipped"] = True
        session.state["camera_received"] = False
        session.state["camera_prompt_nudged"] = False
        session.state["camera_prompt_forced"] = False
        if "story_started" not in session.state:
            session.state["story_started"] = False
        if "awaiting_story_choice" not in session.state:
            session.state["awaiting_story_choice"] = False
    session.state["max_story_turns"] = _MAX_STORY_TURNS
    session.state["max_story_turns_minus_one"] = max(3, _MAX_STORY_TURNS - 1)
    try:
        session.state["turn_number"] = min(int(session.state.get("turn_number", 1)), _MAX_STORY_TURNS)
    except Exception:
        session.state["turn_number"] = 1
    try:
        current_name = str(session.state.get("child_name", "friend")).strip().lower()
        if current_name and current_name != "friend":
            session.state["name_confirmed"] = True
            session.state["camera_stage"] = "done"
            session.state["camera_skipped"] = True
    except Exception:
        pass
    if session.state.get("name_confirmed") or int(session.state.get("turn_number", 1)) >= 3:
        _opening_phase_sessions.discard(session_id)
    else:
        _opening_phase_sessions.add(session_id)
    if bool(session.state.get("story_turn_limit_reached", False)):
        _story_turn_limit_sessions.add(session_id)
    else:
        _story_turn_limit_sessions.discard(session_id)

    if session.state.get("camera_stage") != "prompted" and session.state.get("camera_prompt_nudged"):
        session.state["camera_prompt_nudged"] = False
    # Clean any leaked control tokens from prior sessions.
    try:
        existing_summary = str(session.state.get("story_summary", "")).strip()
        if existing_summary:
            cleaned_summary = _CTRL_TOKEN_RE.sub("", existing_summary).strip()
            cleaned_summary = re.sub(r"\s+", " ", cleaned_summary).strip()
            session.state["story_summary"] = cleaned_summary
    except Exception:
        pass

    _rewind_locks.setdefault(session_id, asyncio.Lock())

    await websocket.send_text(
        ServerEvent(
            type=ServerEventType.SESSION_REHYDRATED,
            payload={
                "session_id": session_id,
                "turn_number": session.state.get("turn_number", 1),
                "max_story_turns": session.state.get("max_story_turns", _MAX_STORY_TURNS),
                "child_name": session.state.get("child_name", "friend"),
                "story_summary": session.state.get("story_summary", ""),
                "server_vad_enabled": _env_enabled("ENABLE_SERVER_VAD", default=False),
                # Re-sync information to help frontend recover UI state
                "current_scene_image_url": session.state.get("scene_asset_urls", [""])[-1] if session.state.get("scene_asset_urls") else None,
                "current_scene_description": session.state.get("current_scene_description", ""),
                "story_started": bool(session.state.get("story_started", False)),
                "pending_response": bool(session.state.get("pending_response", False)),
                "assistant_speaking": session_id in _assistant_speaking_sessions,
                "ending_story": session_id in _ending_story_sessions,
            },
        ).model_dump_json()
    )

    live_queue = LiveRequestQueue()

    from agent.storyteller_agent import run_config

    agent_task: asyncio.Task | None = None
    session_event_task: asyncio.Task | None = None
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))

    async def _ensure_agent_started() -> None:
        nonlocal agent_task, session_event_task
        if agent_task is None or agent_task.done():
            agent_task = asyncio.create_task(
                _run_agent(
                    runner,
                    user_id,
                    session_id,
                    connection_id,
                    live_queue,
                    run_config,
                    websocket,
                )
            )
        if session_event_task is None or session_event_task.done():
            session_event_task = asyncio.create_task(
                _forward_session_events(
                    websocket,
                    runner,
                    user_id,
                    session_id,
                    connection_id,
                )
            )

    json_buffer = ""

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=120.0)
            except asyncio.TimeoutError:
                logger.warning("Session %s timed out waiting for websocket activity.", session_id)
                break
            except WebSocketDisconnect:
                logger.info("Client disconnected: session %s", session_id)
                break
            except RuntimeError as exc:
                if "disconnect message" in str(exc).lower():
                    logger.info("Client disconnected via proxy: session %s", session_id)
                    break
                raise

            if "bytes" in raw and raw["bytes"]:
                if agent_task is None:
                    # Drop early audio until the live agent is started (setup-first).
                    continue
                if session_id in _awaiting_greeting_sessions:
                    # If the child starts speaking before the greeting lands,
                    # we drop the audio if barge-in is disabled so the greeting isn't interrupted.
                    if _env_enabled("DISABLE_BARGE_IN", default=True):
                        continue
                    _awaiting_greeting_sessions.discard(session_id)
                if _env_enabled("DISABLE_BARGE_IN", default=True) and session_id in _assistant_speaking_sessions:
                    # Don't allow background noise to interrupt Amelia while she's speaking,
                    # but release the lock after a short timeout so we don't ignore the child's reply.
                    suppress_for = _env_float("BARGE_IN_SUPPRESS_SECONDS", 1.2)
                    last_spoke = _assistant_speaking_since.get(session_id)
                    if last_spoke and (time.monotonic() - last_spoke) < suppress_for:
                        continue
                    _assistant_speaking_sessions.discard(session_id)
                    _assistant_speaking_since.pop(session_id, None)
                if session_id in _ending_story_sessions:
                    continue
                if session_id in _story_turn_limit_sessions:
                    await _notify_story_limit_once(websocket, session_id, _MAX_STORY_TURNS)
                    continue
                pcm = raw["bytes"]
                # When manual activity signaling is enabled, only stream audio while active.
                if not _env_enabled("ENABLE_SERVER_VAD", default=False):
                    if session_id not in _activity_active_sessions:
                        continue
                # Basic sanity checks on PCM framing to avoid invalid payloads.
                if not pcm or len(pcm) < 2 or (len(pcm) % 2) != 0:
                    continue
                if len(pcm) > 64000:  # ~2s of 16kHz int16 mono — drop oversized chunks
                    continue
                if _env_enabled("ENABLE_NOISE_GATE", default=False):
                    if not passes_noise_gate(pcm):
                        continue
                _audio_seen_this_turn.add(session_id)
                _send_live_realtime(
                    session_id,
                    live_queue,
                    genai_types.Blob(data=pcm, mime_type="audio/pcm;rate=16000")
                )
                continue

            if "text" in raw and raw["text"]:
                json_buffer += raw["text"]
                objects, json_buffer = _extract_json_objects(json_buffer)
                for obj in objects:
                    await asyncio.sleep(0)
                    try:
                        cmd = ClientCommand.model_validate(obj)
                        if session_id in _ending_story_sessions and cmd.type not in {ClientCommandType.HEARTBEAT, ClientCommandType.THEATER_CLOSE}:
                            continue
                        if cmd.type == ClientCommandType.CLIENT_READY:
                            await _ensure_agent_started()
                        if (
                            session_id in _story_turn_limit_sessions
                            and cmd.type
                            in {
                                ClientCommandType.CLIENT_READY,
                                ClientCommandType.ACTIVITY_START,
                                ClientCommandType.ACTIVITY_END,
                                ClientCommandType.SPYGLASS_IMAGE,
                            }
                        ):
                            await _notify_story_limit_once(websocket, session_id, _MAX_STORY_TURNS)
                            continue
                        await _handle_command(
                            cmd=cmd,
                            websocket=websocket,
                            session_id=session_id,
                            user_id=user_id,
                            runner=runner,
                            live_queue=live_queue,
                        )
                    except Exception as exc:
                        logger.error("Command handling error: %s", exc)

    except WebSocketDisconnect:
        logger.info("Client disconnected: session %s", session_id)
    finally:
        for task in (agent_task, heartbeat_task, session_event_task):
            if task:
                task.cancel()
        live_queue.close()
        if _connection_is_current(session_id, connection_id, websocket):
            _story_turn_limit_sessions.discard(session_id)
            _story_turn_limit_notified_sessions.discard(session_id)
            _awaiting_greeting_sessions.discard(session_id)
            _opening_phase_sessions.discard(session_id)
            _assistant_speaking_sessions.discard(session_id)
            _assistant_speaking_since.pop(session_id, None)
            _ending_story_sessions.discard(session_id)
            _active_websockets.pop(session_id, None)
            _active_connection_ids.pop(session_id, None)
            _activity_active_sessions.discard(session_id)
            _activity_last_change.pop(session_id, None)
            _live_request_debug.pop(session_id, None)
            # NOTE: _audio_seen_this_turn is now cleared only on turn completion
            # to ensure it persists across fast reconnections during a turn.
            clear_session(session_id)
            logger.info("Session %s cleaned up.", session_id)
        else:
            logger.info("Session %s stale connection cleanup skipped.", session_id)


async def _run_agent(
    runner: Runner,
    user_id: str,
    session_id: str,
    connection_id: str,
    live_queue: LiveRequestQueue,
    run_config,
    websocket: WebSocket,
) -> None:
    """Consumes ADK events and multiplexes audio + control messages to websocket."""
    assistant_parts: list[str] = []
    last_child_utterance: str = ""
    child_utterance_this_turn: str = ""
    model_emitted_meaningful_output = False
    scene_visuals_called_this_turn = False
    _turn_start_t: float = time.monotonic()
    completed_turn_number = 1
    completed_name_confirmed = False
    completed_camera_stage = "none"
    completed_story_turn_limit = False
    completed_story_started = False
    silent_recovery_attempts = 0
    last_output_transcription: str = ""
    reconnect_attempt = 0
    hard_reset_attempts = 0

    # Resume turn logic: if we reconnected mid-turn, check if we need to poke the model.
    try:
        session = await runner.session_service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
        )
        if session and session.state.get("pending_response") and session.state.get("last_child_utterance"):
            resume_text = session.state["last_child_utterance"]
            logger.info("RESUMING interrupted turn for %s: %s", session_id, resume_text)
            
            def _clear_pending_on_resume(s: dict[str, Any]) -> None:
                s["pending_response"] = False
            await _mutate_state(runner, user_id, session_id, _clear_pending_on_resume)
            
            _send_live_content(session_id, live_queue, resume_text)
    except Exception as exc:
        logger.debug("Turn recovery check failed: %s", exc)

    try:
        max_reconnect_attempts = int(os.environ.get("MAX_LIVE_RECONNECTS", "6"))
    except Exception:
        max_reconnect_attempts = 6

    try:
        while True:
            try:
                async for event in runner.run_live(
                    user_id=user_id,
                    session_id=session_id,
                    live_request_queue=live_queue,
                    run_config=run_config,
                ):
                    if not _connection_is_current(session_id, connection_id, websocket):
                        logger.info("Stopping stale live agent stream for session %s", session_id)
                        return

                    reconnect_attempt = 0

                    if event.error_code:
                        logger.error("Agent error: %s - %s", event.error_code, event.error_message)
                        err_event = ServerEvent(
                            type=ServerEventType.ERROR,
                            payload={"message": event.error_message or "Story magic had a blip!"},
                        )
                        await websocket.send_text(err_event.model_dump_json())
                        continue

                    if event.content and event.content.parts:
                        for part in event.content.parts:
                            if getattr(part, "thought", False):
                                # Never surface or count model-internal thought parts.
                                continue

                            if part.inline_data and part.inline_data.data:
                                model_emitted_meaningful_output = True
                                _assistant_speaking_sessions.add(session_id)
                                _assistant_speaking_since[session_id] = time.monotonic()
                                # If the greeting is in flight, any audible output counts as delivered.
                                if session_id in _awaiting_greeting_sessions:
                                    _awaiting_greeting_sessions.discard(session_id)
                                await websocket.send_bytes(part.inline_data.data)

                            if part.text:
                                # In AUDIO modality, model text parts may contain internal planning.
                                # Prefer spoken output transcription for user-facing content/state.
                                logger.debug("Ignoring model text part in audio stream.")

                            # ── Instant placeholder: intercept function_call BEFORE execution ──
                            if getattr(part, "function_call", None):
                                model_emitted_meaningful_output = True
                                fc = part.function_call
                                fc_name = getattr(fc, "name", "")
                                if fc_name == "generate_scene_visuals":
                                    fc_args = getattr(fc, "args", {}) or {}
                                    scene_desc = fc_args.get("description", "A magical story scene")
                                    # Send instant SVG placeholder so child sees something immediately,
                                    # but skip during onboarding turns to avoid a stuck placeholder.
                                    should_send_placeholder = True
                                    try:
                                        session = await runner.session_service.get_session(
                                            app_name="storyteller",
                                            user_id=user_id,
                                            session_id=session_id,
                                        )
                                        state = session.state if session else {}
                                        story_started = bool(state.get("story_started", False))
                                        pending_story_hint = str(state.get("pending_story_hint", "")).strip()
                                    except Exception:
                                        pass
                                    # Always send placeholder signal so frontend can show loading indicator.
                                    should_send_placeholder = True
                                    scene_visuals_called_this_turn = True
                                    if should_send_placeholder:
                                        placeholder_svg = _build_scene_svg_data_url(scene_desc)
                                        await websocket.send_text(
                                            json.dumps({
                                                "type": "video_ready",
                                                "payload": {
                                                    "url": placeholder_svg,
                                                    "description": scene_desc,
                                                    "media_type": "image",
                                                    "is_placeholder": True,
                                                },
                                            })
                                        )
                                        logger.info("Sent instant placeholder for scene: %s", scene_desc[:80])
                                    _scene_gen_requested_at[session_id] = time.monotonic()
                                    logger.info("⏱️ TIMING [ws] generate_scene_visuals function_call intercepted | session=%s", session_id)
                                    # Set up sync event EARLY so _forward_session_events can signal
                                    # it even if video_ready arrives before turn_complete.
                                    if session_id not in _pending_image_events:
                                        _pending_image_events[session_id] = asyncio.Event()
                                        logger.info("⏱️ SYNC [ws] image sync event created at function_call time | session=%s", session_id)

                            if part.function_response:
                                result_str = ""
                                response_data = (
                                    part.function_response.response
                                    if isinstance(part.function_response.response, dict)
                                    else None
                                )
                                if response_data:
                                    result_str = response_data.get("result", str(response_data))
                                # Track when scene visuals were requested this turn
                                fn_name = getattr(part.function_response, "name", "")
                                if fn_name == "generate_scene_visuals":
                                    scene_visuals_called_this_turn = True
                                await _handle_tool_response(
                                    result=result_str,
                                    websocket=websocket,
                                    session_id=session_id,
                                    user_id=user_id,
                                    runner=runner,
                                    function_name=getattr(part.function_response, "name", ""),
                                    response_data=response_data,
                                )

                    if getattr(event, "input_transcription", None) and hasattr(event.input_transcription, "text"):
                        raw_text = event.input_transcription.text
                        if raw_text:
                            cleaned_text = _CTRL_TOKEN_RE.sub("", raw_text).strip()
                            scrubbed = await scrub_pii(cleaned_text or raw_text)
                            logger.info("Child said (scrubbed): %s", scrubbed[:120])
                            input_finished = bool(getattr(event.input_transcription, "finished", False))
                            name_story_shortcircuit = False
                            if _is_meaningful_text(cleaned_text):
                                child_utterance_this_turn = cleaned_text
                                last_child_utterance = child_utterance_this_turn
                                # ── NEW: reset trackers at the start of a meaningful interaction ──
                                _early_fallback_started.discard(session_id)
                                _audio_seen_this_turn.add(session_id)

                                # Broadcast transcription to frontend for native rendering (Iter 11)
                                await websocket.send_text(
                                    ServerEvent(
                                        type=ServerEventType.USER_TRANSCRIPTION,
                                        payload={"text": scrubbed, "finished": input_finished}
                                    ).model_dump_json()
                                )
                            # If the child provides both a name and a story idea in one utterance,
                            # short-circuit the name confirmation and start the story immediately.
                            # ── STORY START & SHORT-CIRCUIT CONSOLIDATION ──
                            if input_finished:
                                session = await runner.session_service.get_session(
                                    app_name="storyteller",
                                    user_id=user_id,
                                    session_id=session_id,
                                )
                                state = session.state if session else {}
                                story_started = bool(state.get("story_started", False))
                                
                                prompt_sent = False
                                
                                # Priority 1: Full Name + Story Intent in one go
                                detected_name = _extract_child_name(cleaned_text or raw_text)
                                if (not story_started and detected_name 
                                    and _STORY_INTENT_RE.search(cleaned_text or "")
                                    and not bool(state.get("story_shortcircuit", False))):
                                    
                                    def _mark_name_story_sc(s: dict[str, Any]) -> None:
                                        s["child_name"] = detected_name
                                        s["name_confirmed"] = True
                                        s["story_started"] = True
                                        s["story_shortcircuit"] = True
                                        s["camera_stage"] = "done"
                                        s["camera_skipped"] = True

                                    await _mutate_state(runner, user_id, session_id, _mark_name_story_sc)
                                    if session_id not in _audio_seen_this_turn:
                                        _send_live_content(
                                            session_id, live_queue,
                                            f"The child said their name and story idea: \"{cleaned_text}\". "
                                            "Confirm the name once, then start the story immediately."
                                        )
                                        prompt_sent = True

                                # Priority 2: Generic Story Intent (Short-circuit name flow)
                                elif (not story_started and _env_enabled("ENABLE_STORY_SHORTCIRCUIT", default=True)
                                      and _should_shortcircuit_story(cleaned_text or raw_text, state)
                                      and not bool(state.get("story_shortcircuit", False))):
                                    
                                    def _mark_story_sc(s: dict[str, Any]) -> None:
                                        s["name_confirmed"] = True
                                        s["story_started"] = True
                                        s["story_shortcircuit"] = True
                                        s["camera_stage"] = "done"
                                        s["camera_skipped"] = True

                                    await _mutate_state(runner, user_id, session_id, _mark_story_sc)
                                    if session_id not in _audio_seen_this_turn:
                                        _send_live_content(
                                            session_id, live_queue,
                                            f"The child wants to jump straight into the story: \"{cleaned_text}\". "
                                            "Start the story now using 'friend' as the name."
                                        )
                                        prompt_sent = True

                                # Priority 3: Story Intent during Camera flow
                                elif (not story_started and _env_enabled("ENABLE_STORY_SHORTCIRCUIT", default=True)
                                      and _should_skip_camera_for_story(cleaned_text or raw_text, state)
                                      and not bool(state.get("camera_story_shortcircuit", False))):
                                    
                                    def _mark_camera_sc(s: dict[str, Any]) -> None:
                                        s["camera_stage"] = "done"
                                        s["camera_skipped"] = True
                                        s["camera_story_shortcircuit"] = True

                                    await _mutate_state(runner, user_id, session_id, _mark_camera_sc)
                                    if session_id not in _audio_seen_this_turn:
                                        _send_live_content(
                                            session_id, live_queue,
                                            f"The child is skipping the camera for the story: \"{cleaned_text}\". "
                                            "Start the story now."
                                        )
                                        prompt_sent = True

                                # If no story-start prompt was sent, try normal onboarding logic
                                elif not story_started:
                                    # Normal name capture (if enabled)
                                    if _env_enabled("ENABLE_BACKEND_NAME_CAPTURE", default=True):
                                        await _update_child_name_state(
                                            runner=runner,
                                            user_id=user_id,
                                            session_id=session_id,
                                            utterance_text=cleaned_text or raw_text,
                                            input_finished=input_finished,
                                        )

                                # Mark turn as pending so we can resume if disconnected
                                if _is_meaningful_text(cleaned_text):
                                    def _mark_pending(s: dict[str, Any]) -> None:
                                        s["pending_response"] = True
                                        s["last_child_utterance"] = cleaned_text or raw_text
                                    await _mutate_state(runner, user_id, session_id, _mark_pending)

                            if input_finished:
                                try:
                                    session = await runner.session_service.get_session(
                                        app_name="storyteller",
                                        user_id=user_id,
                                        session_id=session_id,
                                    )
                                except Exception:
                                    session = None
                                if session:
                                    state = session.state
                                    stage = str(state.get("camera_stage", "none"))
                                    if (
                                        stage in {"pending", "prompted"}
                                        and not bool(state.get("camera_received", False))
                                        and not bool(state.get("camera_skipped", False))
                                        and not bool(state.get("camera_prompt_nudged", False))
                                    ):
                                        if _CAMERA_YES_RE.search(cleaned_text or raw_text) or _CAMERA_PROMPT_RE.search(cleaned_text or raw_text):
                                            def _mark_camera_prompted(s: dict[str, Any]) -> None:
                                                if str(s.get("camera_stage", "none")) in {"pending", "prompted"}:
                                                    s["camera_stage"] = "prompted"
                                                s["camera_prompt_nudged"] = True

                                            await _mutate_state(
                                                runner=runner,
                                                user_id=user_id,
                                                session_id=session_id,
                                                mutator=_mark_camera_prompted,
                                            )

                    if getattr(event, "output_transcription", None) and hasattr(event.output_transcription, "text"):
                        out_text = event.output_transcription.text
                        cleaned_out = _CTRL_TOKEN_RE.sub("", out_text or "").strip()
                        if _is_meaningful_text(cleaned_out):
                            model_emitted_meaningful_output = True
                            last_output_transcription = cleaned_out
                            out_finished = bool(getattr(event.output_transcription, "finished", False))
                            if out_finished:
                                _assistant_speaking_sessions.discard(session_id)
                                _assistant_speaking_since.pop(session_id, None)
                            else:
                                _assistant_speaking_sessions.add(session_id)
                                _assistant_speaking_since[session_id] = time.monotonic()
                            if out_finished:
                                _awaiting_greeting_sessions.discard(session_id)
                            if out_finished:
                                scrubbed_out = await scrub_pii(cleaned_out)
                                logger.info("Agent said (scrubbed): %s", scrubbed_out)
                            # Broadcast agent transcription (Iter 11)
                            await websocket.send_text(
                                ServerEvent(
                                    type=ServerEventType.AGENT_TRANSCRIPTION,
                                    payload={"text": last_output_transcription, "finished": out_finished}
                                ).model_dump_json()
                            )
                            if (
                                session_id not in _early_fallback_started
                                and not scene_visuals_called_this_turn
                                and len(cleaned_out.split()) >= 8
                            ):
                                try:
                                    speculative_session = await runner.session_service.get_session(
                                        app_name="storyteller",
                                        user_id=user_id,
                                        session_id=session_id,
                                    )
                                except Exception:
                                    speculative_session = None
                                speculative_state = speculative_session.state if speculative_session else {}
                                speculative_story_started = bool(speculative_state.get("story_started", False))
                                speculative_camera_stage = str(speculative_state.get("camera_stage", "none"))
                                speculative_name_confirmed = bool(speculative_state.get("name_confirmed", False))
                                try:
                                    speculative_turn = int(speculative_state.get("turn_number", 1) or 1)
                                except Exception:
                                    speculative_turn = 1
                                if (
                                    speculative_story_started
                                    and speculative_camera_stage not in {"pending", "prompted"}
                                    and (speculative_name_confirmed or speculative_turn >= 3)
                                ):
                                    if session_id not in _pending_image_events:
                                        _pending_image_events[session_id] = asyncio.Event()
                                    await _trigger_fallback_scene(
                                        session_id=session_id,
                                        assistant_text=cleaned_out,
                                        child_text=child_utterance_this_turn or last_child_utterance,
                                        runner=runner,
                                        websocket=websocket,
                                        user_id=user_id,
                                    )
                                    _early_fallback_started.add(session_id)
                                    scene_visuals_called_this_turn = True
                            if out_finished:
                                assistant_parts.append(cleaned_out)

                    if getattr(event, "turn_complete", False):
                        _tc_t = time.monotonic()
                        _tc_delta = int((_tc_t - _turn_start_t) * 1000)

                        # Turn is done, no longer pending a response.
                        def _clear_pending(s: dict[str, Any]) -> None:
                            s["pending_response"] = False
                        await _mutate_state(runner, user_id, session_id, _clear_pending)
                        assistant_text = " ".join(assistant_parts).strip()
                        assistant_parts = []
                        if not assistant_text and _is_meaningful_text(last_output_transcription):
                            assistant_text = last_output_transcription
                        last_output_transcription = ""

                        if model_emitted_meaningful_output:
                            silent_recovery_attempts = 0
                            turn_limit_reached = False

                            def _on_turn_complete(state: dict[str, Any]) -> None:
                                nonlocal turn_limit_reached
                                nonlocal completed_turn_number
                                nonlocal completed_name_confirmed
                                nonlocal completed_camera_stage
                                nonlocal completed_story_turn_limit
                                nonlocal completed_story_started
                                _take_snapshot(state)
                                if state.get("name_confirmed") or int(state.get("turn_number", 1)) >= 3:
                                    _opening_phase_sessions.discard(session_id)
                                else:
                                    _opening_phase_sessions.add(session_id)
                                _append_story_summary(state, assistant_text)
                                # If a pending name exists and the child just affirmed or repeated it,
                                # lock in the name to prevent repeated confirmations.
                                pending_name = str(state.get("pending_child_name", "")).strip()
                                if pending_name:
                                    utter = (child_utterance_this_turn or "").strip()
                                    if utter:
                                        if (
                                            _NAME_AFFIRM_RE.search(utter)
                                            or re.search(rf"\\b{re.escape(pending_name)}\\b", utter, flags=re.IGNORECASE)
                                        ):
                                            state["child_name"] = pending_name
                                            state["name_confirmed"] = True
                                            state["pending_child_name"] = ""
                                            state["name_confirmation_prompted"] = False
                                            if state.get("camera_stage", "none") == "none":
                                                state["camera_stage"] = "done"
                                                state["camera_skipped"] = True
                                # If name is confirmed but child_name never got set, backfill from pending.
                                if state.get("name_confirmed") and str(state.get("child_name", "friend")).strip().lower() == "friend":
                                    if pending_name:
                                        state["child_name"] = pending_name
                                        state["pending_child_name"] = ""
                                        state["name_confirmation_prompted"] = False
                                
                                # Log any tool calls made this turn to help debug duplicate generation issues
                                if getattr(event, "tool_call", None) and hasattr(event.tool_call, "function_calls"):
                                    for fc in event.tool_call.function_calls:
                                        logger.info(f"🛠️ TOOL INVOCATION [{session_id[:8]}]: {fc.name}")
                                        logger.debug(f"   Args: {fc.args}")

                                if (
                                    assistant_text
                                    and str(state.get("pending_child_name", "")).strip()
                                    and not bool(state.get("name_confirmed", False))
                                ):
                                    pending_name = str(state.get("pending_child_name", "")).strip()
                                    prompt_match = re.search(
                                        r"(did i hear (your name is|you say)|is your name|your name is|did i get your name|your magical name|is it)",
                                        assistant_text,
                                        flags=re.IGNORECASE,
                                    )
                                    name_in_question = (
                                        "?" in assistant_text
                                        and pending_name
                                        and re.search(rf"\\b{re.escape(pending_name)}\\b", assistant_text, flags=re.IGNORECASE)
                                    )
                                    if prompt_match or name_in_question:
                                        state["name_confirmation_prompted"] = True
                                # If Amelia asks what kind of story to tell, mark that we're awaiting a story choice.
                                if (
                                    assistant_text
                                    and bool(state.get("name_confirmed", False))
                                    and not bool(state.get("story_started", False))
                                    and _STORY_PROMPT_RE.search(assistant_text)
                                ):
                                    state["awaiting_story_choice"] = True
                                if (
                                    str(state.get("camera_stage", "none")) == "pending"
                                    and _CAMERA_PROMPT_RE.search(assistant_text or "")
                                ):
                                    state["camera_stage"] = "prompted"
                                if _CAMERA_PROMPT_RE.search(assistant_text or ""):
                                    try:
                                        state["camera_prompt_count"] = int(state.get("camera_prompt_count", 0)) + 1
                                    except Exception:
                                        state["camera_prompt_count"] = 1
                                max_turns = _MAX_STORY_TURNS
                                try:
                                    max_turns = int(state.get("max_story_turns", _MAX_STORY_TURNS))
                                except Exception:
                                    max_turns = _MAX_STORY_TURNS
                                max_turns = max(_MIN_STORY_TURNS, min(max_turns, _MAX_STORY_TURNS_HARD))
                                state["max_story_turns"] = max_turns
                                state["max_story_turns_minus_one"] = max(3, max_turns - 1)
                                current_turn = int(state.get("turn_number", 1))
                                turn_limit_reached = current_turn >= max_turns
                                state["turn_number"] = min(current_turn + 1, max_turns)
                                state["story_turn_limit_reached"] = turn_limit_reached
                                completed_turn_number = current_turn
                                completed_name_confirmed = bool(state.get("name_confirmed", False))
                                completed_camera_stage = str(state.get("camera_stage", "none"))
                                completed_story_turn_limit = bool(state.get("story_turn_limit_reached", False))
                                completed_story_started = bool(state.get("story_started", False))
                                if (
                                    state.get("turn_number", 1) >= 3
                                    and str(state.get("child_name", "friend")).strip().lower() == "friend"
                                    and not bool(state.get("name_confirmed", False))
                                ):
                                    state["name_confirmed"] = True
                                    state["pending_child_name"] = ""
                                    state["name_confirmation_prompted"] = False
                                # If we've already asked about the camera and got no meaningful input,
                                # skip the camera to avoid repeating the same question.
                                try:
                                    prompt_count = int(state.get("camera_prompt_count", 0))
                                except Exception:
                                    prompt_count = 0
                                if prompt_count >= 1 and str(state.get("camera_stage", "none")) in {"pending", "prompted"}:
                                    utter = (child_utterance_this_turn or "").strip()
                                    if utter and _CAMERA_YES_RE.search(utter):
                                        state["camera_stage"] = "prompted"
                                        state["camera_prompt_nudged"] = False
                                    else:
                                        state["camera_stage"] = "done"
                                        state["camera_skipped"] = True
                                        state["camera_prompt_nudged"] = False
                                        state["camera_prompt_forced"] = False

                            await _mutate_state(
                                runner=runner,
                                user_id=user_id,
                                session_id=session_id,
                                mutator=_on_turn_complete,
                            )

                            # ── IMAGE SYNC: log whether image arrived before or after turn_complete ──
                            if scene_visuals_called_this_turn:
                                _img_wait_evt = _pending_image_events.get(session_id)
                                _image_already_arrived = _img_wait_evt is not None and _img_wait_evt.is_set()
                                _pending_image_events.pop(session_id, None)
                                if _image_already_arrived:
                                    _sync_ms = int((time.monotonic() - _tc_t) * 1000)
                                    logger.info("⏱️ SYNC [ws] image already arrived before turn_complete | sync_ms=%d | session=%s", _sync_ms, session_id)
                                else:
                                    # Image is still generating — it will arrive shortly and be sent
                                    # to the frontend automatically by _forward_session_events.
                                    # Do NOT send keep-narrating — it creates a new model turn which
                                    # resets scene_visuals_called_this_turn and causes a second image gen.
                                    logger.info("⏱️ SYNC [ws] image still generating at turn_complete — will arrive shortly | session=%s", session_id)

                            # ── TURN COMPLETE FALLBACK: if no tool call seen at ALL ──
                            if (
                                not scene_visuals_called_this_turn
                                and session_id not in _early_fallback_started
                                and session_id not in _pending_image_events
                            ):
                                if (
                                    completed_story_started
                                    and completed_camera_stage not in {"pending", "prompted"}
                                    and (completed_name_confirmed or completed_turn_number >= 3)
                                ):
                                    await _trigger_fallback_scene(
                                        session_id=session_id,
                                        assistant_text=assistant_text,
                                        child_text=last_child_utterance,
                                        runner=runner,
                                        websocket=websocket,
                                        user_id=user_id,
                                    )
                                    scene_visuals_called_this_turn = True
                                    _early_fallback_started.add(session_id)

                            if turn_limit_reached:
                                _story_turn_limit_sessions.add(session_id)
                                # Safety fallback: if the model fails to call assemble_story_video,
                                # trigger it once we hit the final turn (cloud mode only).
                                if not _env_enabled("LOCAL_STORYBOOK_MODE", default=False):
                                    try:
                                        await websocket.send_text(
                                            ServerEvent(
                                                type=ServerEventType.VIDEO_GENERATION_STARTED,
                                                payload={
                                                    "stage": "storybook",
                                                    "message": "Making your storybook movie…",
                                                    "eta_seconds": 90,
                                                },
                                            ).model_dump_json()
                                        )
                                        asyncio.create_task(assemble_story_video(session_id=session_id))
                                    except Exception as exc:
                                        logger.warning("Auto assemble_story_video failed: %s", exc)

                            # Deterministic multimodal pacing: always refresh scene + music each turn,
                            # even if the model skipped tool calls.
                            seed = (
                                assistant_text
                                or child_utterance_this_turn
                                or last_child_utterance
                                or "A magical story scene"
                            )
                            # Only refresh background music if mood changed — do NOT
                            # send new SVG placeholders here. The displayed image should
                            # persist across Q&A turns. New images only arrive when the
                            # model calls generate_scene_visuals (handled by the
                            # function_call interceptor above).
                            mood, intensity = _infer_music_mood(seed)
                            await websocket.send_text(
                                music_command_event(mood=mood, intensity=intensity).model_dump_json()
                            )
                            scene_visuals_called_this_turn = False
                            _audio_seen_this_turn.discard(session_id)
                            _turn_start_t = time.monotonic()  # reset turn timer
                        else:
                            had_child_input_this_turn = _is_meaningful_text(child_utterance_this_turn)
                            logger.warning(
                                "Silent model turn detected for session %s (child_input=%s)",
                                session_id,
                                had_child_input_this_turn,
                            )

                            if session_id in _awaiting_greeting_sessions:
                                # If the greeting didn't land, resend it instead of asking the child to repeat.
                                if silent_recovery_attempts < 2:
                                    silent_recovery_attempts += 1
                                    greeting_prompt = (
                                        "A child just joined. Greet them with very short, simple sentences for a 4-year-old. "
                                        "Ask their name, and say they can also tell you what story they want."
                                    )
                                    _send_live_content(session_id, live_queue, greeting_prompt)
                                else:
                                    _awaiting_greeting_sessions.discard(session_id)
                            elif session_id in _opening_phase_sessions:
                                # During the opening, ignore noise and re-offer the greeting instead of
                                # triggering a generic "repeat yourself" message.
                                if silent_recovery_attempts < 2:
                                    silent_recovery_attempts += 1
                                    greeting_prompt = (
                                        "You're still in the opening. Repeat the greeting in very short, simple sentences. "
                                        "Ask their name, and say they can also tell you what story they want."
                                    )
                                    _send_live_content(session_id, live_queue, greeting_prompt)
                            elif had_child_input_this_turn:
                                # Avoid injecting contradictory "repeat" instructions when we already
                                # captured clear child speech (e.g. "My name is Aaron").
                                await websocket.send_text(
                                    ServerEvent(
                                        type=ServerEventType.ERROR,
                                        payload={"message": "I heard you. Give me one second..."},
                                    ).model_dump_json()
                                )
                                if silent_recovery_attempts < 2 and last_child_utterance:
                                    silent_recovery_attempts += 1
                                    repair_prompt = (
                                        "The child just said: "
                                        f"\"{last_child_utterance}\". "
                                        "Please respond now in Amelia's voice, following all system rules. "
                                        "If this was a story choice, continue the story and call generate_scene_visuals."
                                    )
                                    _send_live_content(session_id, live_queue, repair_prompt)
                            else:
                                await websocket.send_text(
                                    ServerEvent(
                                        type=ServerEventType.ERROR,
                                        payload={"message": "I didn't catch that. Can you say it again?"},
                                    ).model_dump_json()
                                )

                                if silent_recovery_attempts < 2:
                                    silent_recovery_attempts += 1
                                    repair_prompt = (
                                        "The child's audio was unclear or empty this turn. "
                                        "Respond with one short child-friendly sentence asking them to repeat. "
                                        "Do not call tools or output silence."
                                    )
                                    _send_live_content(session_id, live_queue, repair_prompt)

                        await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
                        model_emitted_meaningful_output = False
                        child_utterance_this_turn = ""
                        _audio_seen_this_turn.discard(session_id)

                # Stream exited cleanly.
                break

            except asyncio.CancelledError:
                raise
            except Exception as stream_exc:
                _dump_live_request_debug(session_id)
                if _is_retryable_live_error(stream_exc) and reconnect_attempt < max_reconnect_attempts:
                    reconnect_attempt += 1
                    backoff_seconds = min(2.0, 0.5 * reconnect_attempt)
                    logger.warning(
                        "Transient live stream error for session %s (attempt %d/%d): %s",
                        session_id,
                        reconnect_attempt,
                        max_reconnect_attempts,
                        stream_exc,
                    )
                    try:
                        await websocket.send_text(
                            ServerEvent(
                                type=ServerEventType.ERROR,
                                payload={"message": "Magic hiccup. Reconnecting now..."},
                            ).model_dump_json()
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(backoff_seconds)
                    continue
                if hard_reset_attempts < 2:
                    hard_reset_attempts += 1
                    reconnect_attempt = 0
                    logger.warning(
                        "Live stream hard reset for session %s after error: %s",
                        session_id,
                        stream_exc,
                    )
                    # Aggressively prune history to clear 1007/1011 context corruption.
                    await _prune_session_history(runner, user_id, session_id)
                    try:
                        await websocket.send_text(
                            ServerEvent(
                                type=ServerEventType.ERROR,
                                payload={
                                    "message": "Cleaning up and starting fresh. One moment!",
                                    "auto_resume": True
                                },
                            ).model_dump_json()
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)
                    continue
                raise

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Agent runner error: %s", exc, exc_info=True)
        status_code = getattr(exc, "status_code", None)
        msg = str(exc).lower()
        if status_code == 1008 or "operation is not implemented" in msg:
            err_event = ServerEvent(
                type=ServerEventType.ERROR,
                payload={
                    "message": (
                        "Live audio config not supported for this model. "
                        "Disable server VAD or switch to a supported Live model, then retry."
                    )
                },
            )
            try:
                await websocket.send_text(err_event.model_dump_json())
            except Exception:
                pass
            return
        err_event = ServerEvent(
            type=ServerEventType.ERROR,
            payload={"message": "Story magic had a blip! Please try again."},
        )
        try:
            await websocket.send_text(err_event.model_dump_json())
        except Exception:
            pass


async def _download_gcs_to_bytes(gcs_url: str) -> bytes | None:
    if not gcs_url.startswith("gs://"):
        return None
    path = gcs_url[5:]
    parts = path.split("/", 1)
    if len(parts) != 2:
        return None
    bucket_name, blob_path = parts

    def _download() -> bytes:
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_path)
        return blob.download_as_bytes()

    try:
        return await asyncio.to_thread(_download)
    except Exception as exc:
        logger.warning("GCS download failed for %s: %s", gcs_url[:80], exc)
        return None


async def _handle_tool_response(
    result: str,
    websocket: WebSocket,
    session_id: str,
    user_id: str,
    runner: Runner,
    function_name: str = "",
    response_data: dict[str, Any] | None = None,
) -> None:
    normalized_name = function_name.strip().lower()

    if normalized_name in {
        "generate_sfx", "text_to_speech", "speech_to_speech",
        "text_to_sound_effects", "compose_music",
    } and response_data:
        media_url = _extract_first_media_url(response_data)
        if media_url:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "sfx_command",
                        "payload": {
                            "url": _normalize_client_media_url(media_url),
                            "label": normalized_name,
                        },
                    }
                )
            )

    if not result:
        return

    if result.startswith("SAVE_FACT:"):
        _, payload = result.split(":", 1)
        if "|" in payload:
            name, fact = payload.split("|", 1)

            def _save_fact(state: dict[str, Any]) -> None:
                facts = list(state.get("character_facts_list", []))
                facts.append({"character_name": name, "fact": fact})
                facts = facts[-40:]
                state["character_facts_list"] = facts
                state["character_facts"] = "\n".join(
                    f"- {entry['character_name']}: {entry['fact']}" for entry in facts
                )

            await _mutate_state(runner, user_id, session_id, _save_fact)

    if result.startswith("TRIGGER_LYRIA_CELEBRATION"):
        await websocket.send_text(music_command_event(mood="triumphant_celebration", intensity=8).model_dump_json())

    if "ASSEMBLE_JOB_STARTED" in result:
        observed_session_id = session_id
        if "|SESSION:" in result:
            observed_session_id = result.split("|SESSION:", 1)[1].strip() or session_id
        await websocket.send_text(
            ServerEvent(
                type=ServerEventType.VIDEO_GENERATION_STARTED,
                payload={
                    "stage": "storybook",
                    "message": "Making your storybook movie…",
                    "eta_seconds": 90,
                },
            ).model_dump_json()
        )
        asyncio.create_task(_watch_for_final_video(observed_session_id))

    if "TRADING_CARD_GENERATING" in result:
        # Trading card is generating in the background; the trading_card_ready
        # session event will be forwarded to the client via _forward_session_events.
        logger.info("Trading card generation started for session %s", session_id)

    if result.startswith("System: Background music"):
        import re

        mood_match = re.search(r"'([^']+)'", result)
        intensity_match = re.search(r"intensity (\d+)", result)
        if mood_match:
            event = music_command_event(
                mood=mood_match.group(1),
                intensity=int(intensity_match.group(1)) if intensity_match else 5,
            )
            await websocket.send_text(event.model_dump_json())


async def _watch_for_final_video(session_id: str) -> None:
    """Polls GCS for final assembled movie and emits theater_mode when ready."""
    bucket_name = os.environ.get("GCS_FINAL_VIDEOS_BUCKET", "")
    if not bucket_name:
        return

    object_path = f"{session_id}/story_final.mp4"
    deadline = asyncio.get_running_loop().time() + 600

    while asyncio.get_running_loop().time() < deadline:
        try:
            client = storage.Client()
            blob = client.bucket(bucket_name).blob(object_path)
            exists = await asyncio.to_thread(blob.exists)
            if exists:
                try:
                    url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=2), method="GET")
                except Exception:
                    url = f"https://storage.googleapis.com/{bucket_name}/{object_path}"

                # Try to read trading card URL from Firestore session doc
                trading_card_url: str | None = None
                try:
                    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
                    db_name = os.environ.get("FIRESTORE_DATABASE", "(default)")
                    if project:
                        from google.cloud import firestore as _fs
                        _db = _fs.AsyncClient(project=project, database=db_name)
                        _doc = await _db.collection("sessions").document(session_id).get()
                        if _doc.exists:
                            trading_card_url = (_doc.to_dict() or {}).get("trading_card_url") or None
                except Exception as exc:
                    logger.debug("Could not read trading_card_url from Firestore: %s", exc)

                publish_session_event(
                    session_id,
                    theater_mode_event(
                        mp4_url=url,
                        trading_card_url=trading_card_url,
                    ).model_dump(mode="json"),
                )
                return
        except Exception as exc:
            logger.debug("Final video poll error for %s: %s", session_id, exc)

        await asyncio.sleep(4)


async def _handle_command(
    cmd: ClientCommand,
    websocket: WebSocket,
    session_id: str,
    user_id: str,
    runner: Runner,
    live_queue: LiveRequestQueue,
) -> None:
    if cmd.type == ClientCommandType.CLIENT_READY:
        viewport = cmd.payload.get("viewport", {}) if isinstance(cmd.payload, dict) else {}
        panel = cmd.payload.get("panel", {}) if isinstance(cmd.payload, dict) else {}
        try:
            width = float(panel.get("width") or viewport.get("width") or 0)
            height = float(panel.get("height") or viewport.get("height") or 0)
            dpr = float(viewport.get("devicePixelRatio") or 1)
            is_compact = bool(viewport.get("isCompact"))
        except Exception:
            width = 0
            height = 0
            dpr = 1
            is_compact = False

        if width and height:
            def _save_device_profile(state: dict[str, Any]) -> None:
                state["device_profile"] = {
                    "width": width,
                    "height": height,
                    "device_pixel_ratio": dpr,
                    "is_compact": is_compact,
                }
                state["preferred_aspect_ratio"] = _closest_aspect_ratio(width, height)
                state["preferred_image_size"] = _preferred_image_size(width, height, is_compact)

            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_save_device_profile,
            )

        if session_id not in _greeting_sent_sessions:
            _greeting_sent_sessions.add(session_id)
            _awaiting_greeting_sessions.add(session_id)
            _send_live_content(
                session_id,
                live_queue,
                (
                    "A child just joined. Greet them with very short, simple sentences for a 4-year-old. "
                    "Ask their name, and say they can also tell you what story they want."
                ),
            )

    elif cmd.type == ClientCommandType.ACTIVITY_START:
        if session_id in _awaiting_greeting_sessions and _env_enabled("DISABLE_BARGE_IN", default=True):
            # Ignore manual VAD start if we are still waiting for the greeting to finish
            # and barge-in is disabled. The audio bytes are also dropped above.
            pass
        else:
            if not _env_enabled("ENABLE_SERVER_VAD", default=False):
                try:
                    live_queue.send_activity_start()
                except Exception:
                    pass
                _activity_active_sessions.add(session_id)
            _activity_last_change[session_id] = time.monotonic()
        return

    elif cmd.type == ClientCommandType.ACTIVITY_END:
        if session_id in _awaiting_greeting_sessions and _env_enabled("DISABLE_BARGE_IN", default=True):
            # Ignore manual VAD end if we ignored the start.
            pass
        else:
            if not _env_enabled("ENABLE_SERVER_VAD", default=False):
                try:
                    live_queue.send_activity_end()
                except Exception:
                    pass
                _activity_active_sessions.discard(session_id)
            _activity_last_change[session_id] = time.monotonic()
        return

    elif cmd.type == ClientCommandType.REWIND:
        lock = _rewind_locks.get(cmd.session_id)
        if lock and not lock.locked():
            async with lock:
                session = await runner.session_service.get_session(
                    app_name="storyteller",
                    user_id=user_id,
                    session_id=cmd.session_id,
                )
                if session and session.events:
                    last_invocation_id = session.events[-1].invocation_id
                    try:
                        await runner.rewind_async(
                            user_id=user_id,
                            session_id=cmd.session_id,
                            rewind_before_invocation_id=last_invocation_id,
                        )
                        await _mutate_state(
                            runner,
                            user_id,
                            cmd.session_id,
                            _rollback_snapshot,
                        )
                        refreshed = await runner.session_service.get_session(
                            app_name="storyteller",
                            user_id=user_id,
                            session_id=cmd.session_id,
                        )
                        if refreshed and not bool(refreshed.state.get("story_turn_limit_reached", False)):
                            _story_turn_limit_sessions.discard(cmd.session_id)
                            _story_turn_limit_notified_sessions.discard(cmd.session_id)
                        if refreshed:
                            scene_urls = refreshed.state.get("scene_asset_urls", [])
                            if isinstance(scene_urls, list) and scene_urls:
                                last_scene_url = scene_urls[-1]
                                await websocket.send_text(
                                    json.dumps(
                                        {
                                            "type": "video_ready",
                                            "payload": {
                                                "url": last_scene_url,
                                                "media_type": "image",
                                            },
                                        }
                                    )
                                )
                        await websocket.send_text(
                            ServerEvent(type=ServerEventType.REWIND_COMPLETE, payload={}).model_dump_json()
                        )
                    except ValueError as exc:
                        logger.warning("Rewind failed: %s", exc)

    elif cmd.type == ClientCommandType.END_STORY:
        def _force_story_end(state: dict[str, Any]) -> None:
            try:
                max_turns = int(state.get("max_story_turns", _MAX_STORY_TURNS))
            except Exception:
                max_turns = _MAX_STORY_TURNS
            max_turns = max(_MIN_STORY_TURNS, min(max_turns, _MAX_STORY_TURNS_HARD))
            state["turn_number"] = max_turns
            state["story_turn_limit_reached"] = True

        await _mutate_state(
            runner=runner,
            user_id=user_id,
            session_id=cmd.session_id,
            mutator=_force_story_end,
        )
        _ending_story_sessions.add(cmd.session_id)
        _send_live_content(
            session_id,
            live_queue,
            (
                "The child asked to end the story now. "
                "Give a warm, brief ending in 2-3 sentences, then call assemble_story_video."
            ),
        )

    elif cmd.type == ClientCommandType.SPYGLASS_IMAGE:
        gcs_url = cmd.payload.get("gcs_url", "")
        if isinstance(gcs_url, str) and gcs_url:
            image_bytes = await _download_gcs_to_bytes(gcs_url)
            if image_bytes:
                toy_thumb = _make_thumbnail_b64(image_bytes)
                _record_live_request(
                    session_id,
                    "content_image",
                    {"bytes": len(image_bytes), "mime": "image/jpeg"},
                )
                live_queue.send_content(
                    genai_types.Content(
                        role="user",
                        parts=[
                            genai_types.Part(
                                inline_data=genai_types.Blob(
                                    data=image_bytes,
                                    mime_type="image/jpeg",
                                )
                            )
                        ],
                    )
                )
                def _save_spyglass(state: dict[str, Any]) -> None:
                    state["camera_stage"] = "done"
                    state["camera_received"] = True
                    if toy_thumb:
                        state["toy_reference_thumbnail_b64"] = toy_thumb[0]
                        state["toy_reference_thumbnail_mime"] = toy_thumb[1]
                    state["sidekick_description"] = state.get("sidekick_description") or "their special toy companion"

                await _mutate_state(
                    runner=runner,
                    user_id=user_id,
                    session_id=cmd.session_id,
                    mutator=_save_spyglass,
                )

    elif cmd.type == ClientCommandType.THEATER_CLOSE:
        live_queue.close()

    elif cmd.type == ClientCommandType.IOT_CONFIG:
        config = cmd.payload.get("config", {})
        if isinstance(config, dict):
            set_session_iot_config(cmd.session_id, config)

    elif cmd.type == ClientCommandType.HEARTBEAT:
        try:
            await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
        except Exception as exc:
            logger.debug("Failed to send heartbeat ack: %s", exc)


async def _heartbeat(websocket: WebSocket) -> None:
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            await websocket.send_text(json.dumps({"type": "heartbeat", "payload": {"ping": True}}))
        except asyncio.CancelledError:
            break
        except WebSocketDisconnect:
            break
        except Exception as exc:
            logger.debug("Background heartbeat ping failed: %s", exc)



async def _forward_session_events(
    websocket: WebSocket,
    runner: Runner,
    user_id: str,
    session_id: str,
    connection_id: str,
) -> None:
    """Forwards custom async events (tool completions) to the websocket."""
    queue = get_session_queue(session_id)
    try:
        while True:
            if not _connection_is_current(session_id, connection_id, websocket):
                logger.info("Stopping stale session event forwarder for %s", session_id)
                return
            event = await queue.get()
            if not _connection_is_current(session_id, connection_id, websocket):
                logger.info("Dropping queued event for stale session forwarder %s", session_id)
                return
            event_type = event.get("type")
            payload = event.get("payload", {})

            if event_type == "video_ready" and isinstance(payload, dict):
                logger.debug("Popped video_ready event from queue for session %s. URL starts with: %s", session_id, str(payload.get("url", ""))[:30])
                url = str(payload.get("url", ""))
                if url:
                    # Mark that this session has received at least one real image.
                    if not payload.get("is_placeholder"):
                        _session_has_any_image.add(session_id)
                    media_type = str(payload.get("media_type", "")).lower().strip()
                    looks_like_image = (
                        media_type == "image"
                        or url.startswith("data:image")
                        or bool(re.search(r"\.(png|jpe?g|webp|gif|svg)(\?|$)", url, flags=re.IGNORECASE))
                    )
                    asset_key = "scene_asset_urls" if looks_like_image else "generated_asset_urls"

                    def _append_asset(state: dict[str, Any]) -> None:
                        urls = state.get(asset_key, [])
                        if not isinstance(urls, list):
                            urls = []
                        if not urls or urls[-1] != url:
                            urls.append(url)
                        state[asset_key] = urls[-40:]
                        if looks_like_image:
                            descs = state.get("scene_descriptions", [])
                            if not isinstance(descs, list):
                                descs = []
                            description = str(payload.get("description", "")).strip()
                            if description and (not descs or descs[-1] != description):
                                descs.append(description)
                                state["scene_descriptions"] = descs[-40:]
                            gcs_uri = str(payload.get("gcs_uri", "") or "").strip()
                            gcs_list = state.get("scene_asset_gcs_uris", [])
                            if not isinstance(gcs_list, list):
                                gcs_list = []
                            if gcs_uri and (not gcs_list or gcs_list[-1] != gcs_uri):
                                gcs_list.append(gcs_uri)
                            state["scene_asset_gcs_uris"] = gcs_list[-40:]
                        thumb_b64 = payload.get("thumbnail_b64")
                        thumb_mime = payload.get("thumbnail_mime")
                        if isinstance(thumb_b64, str) and thumb_b64:
                            state["previous_scene_thumbnail_b64"] = thumb_b64
                            state["previous_scene_thumbnail_mime"] = (
                                str(thumb_mime) if isinstance(thumb_mime, str) else "image/jpeg"
                            )

                    await _mutate_state(
                        runner,
                        user_id,
                        session_id,
                        _append_asset,
                    )
                    outbound_payload = dict(payload)
                    outbound_payload.pop("thumbnail_b64", None)
                    outbound_payload.pop("thumbnail_mime", None)
                    outbound_payload.pop("gcs_uri", None)
                    outbound_payload["url"] = url
                    logger.debug("Sending video_ready event to websocket loop for session %s. Media type: %s", session_id, media_type)
                    await websocket.send_text(
                        json.dumps({"type": "video_ready", "payload": outbound_payload})
                    )
                    # Signal the downstream loop that the image has arrived.
                    _img_evt = _pending_image_events.get(session_id)
                    if _img_evt:
                        _img_evt.set()
                        _req_at = _scene_gen_requested_at.pop(session_id, None)
                        _e2e_ms = int((time.monotonic() - _req_at) * 1000) if _req_at else -1
                        logger.info("⏱️ SYNC [ws] signaled image arrival | e2e_ms=%d | session=%s", _e2e_ms, session_id)
                    else:
                        _req_at = _scene_gen_requested_at.pop(session_id, None)
                        _e2e_ms = int((time.monotonic() - _req_at) * 1000) if _req_at else -1
                        logger.info("⏱️ TIMING [ws] video_ready sent (no sync wait) | e2e_ms=%d | session=%s", _e2e_ms, session_id)
                else:
                    logger.debug("video_ready event for session %s had an empty URL?!", session_id)
                continue

            await websocket.send_text(json.dumps(event))
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.debug("Session event forwarder exited: %s", exc)
