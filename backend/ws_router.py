"""WebSocket router for the StorySpark live session."""

from __future__ import annotations

import base64
import copy
import asyncio
import html
import json
import logging
import os
import re
import time
import uuid
from collections import Counter, deque
from datetime import timedelta
from io import BytesIO
from typing import Any, Mapping
from urllib.parse import urlparse

import resource

from fastapi import WebSocket, WebSocketDisconnect
from google import genai as google_genai
from google.adk.agents import LiveRequestQueue
from google.adk.agents.live_request_queue import LiveRequest
from google.adk.runners import Runner
from google.cloud import storage
from google.genai import types as genai_types

from .audio import passes_noise_gate, scrub_pii
from .storybook_flow import derive_story_phase, theater_release_ready
from shared.story_continuity import (
    ensure_story_continuity_state,
    prime_character_carryover,
    record_continuity_scene,
    update_continuity_from_child_utterance,
    validate_live_scene_request,
)
from shared.storybook_movie_quality import (
    child_age_band,
    clamp_child_age,
    normalize_storybook_movie_pacing,
)
from shared.storybook_pages import count_rendered_story_pages, story_pages_from_state_data
from agent.tools import (
    assemble_story_video,
    cache_storybook_state,
    load_storybook_resume_state,
    reset_storybook_assembly_lock,
    supersede_scene_render,
    VisualArgs,
    _build_google_genai_client,
    _crop_image_to_thumbnail_b64,
    _default_live_image_model,
    _is_supported_image_generation_model,
    _load_storybook_firestore_state,
    _resolve_delivery_preferences,
    _resolve_storybook_title,
    _run_visual_pipeline,
    _update_storybook_firestore,
    record_prompt_feedback,
)
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
_last_client_transport_at: dict[str, float] = {}
_last_client_heartbeat_at: dict[str, float] = {}
_forced_disconnect_reasons: dict[str, str] = {}
_greeting_sent_sessions: set[str] = set()
_awaiting_greeting_sessions: set[str] = set()
_opening_phase_sessions: set[str] = set()
_assistant_speaking_sessions: set[str] = set()
_assistant_speaking_since: dict[str, float] = {}
_interrupted_turn_sessions: set[str] = set()
_ending_story_sessions: set[str] = set()
_ending_story_flush_sessions: set[str] = set()
_assembly_intro_sent_sessions: set[str] = set()
_video_generation_started_sessions: set[str] = set()
_watching_final_video_sessions: set[str] = set()
_final_video_watch_not_before_epoch: dict[str, float] = {}
_activity_active_sessions: set[str] = set()
_activity_last_change: dict[str, float] = {}
_live_request_debug: dict[str, deque[dict[str, Any]]] = {}
_audio_seen_this_turn: set[str] = set()
_clean_live_reconnect_sessions: set[str] = set()
_recent_finished_child_transcripts: dict[str, tuple[str, float]] = {}
_page_read_aloud_active_sessions: set[str] = set()
_page_read_aloud_suppress_until: dict[str, float] = {}
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
_live_telemetry_counters: Counter[str] = Counter()
_PENDING_SCENE_REPLACEMENT_PHASE_ACK = "awaiting_ack"
_PENDING_SCENE_REPLACEMENT_PHASE_RENDER = "awaiting_render"
_ALLOWED_ORIGINS = {
    origin
    for origin in {
        os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000"),
        os.environ.get("PROD_FRONTEND_ORIGIN", ""),
    }
    if origin
}
_HEARTBEAT_INTERVAL = 10
_CLIENT_HEARTBEAT_STALE_SECONDS = 35
_CLIENT_TRANSPORT_STALE_SECONDS = 55
_MIN_STORY_TURNS = 6
_MAX_STORY_TURNS = int(os.environ.get("MAX_STORY_TURNS", "20"))
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
_MOVIE_FEEDBACK_REASON_MAP: dict[str, str] = {
    "didnt_match_story": "The finished movie did not match the intended story beats closely enough.",
    "characters_changed": "Character appearance drifted across scenes in the finished movie.",
    "wrong_place_or_props": "Important locations or props changed unexpectedly across scenes.",
    "too_much_text": "There was too much visible text, lettering, labels, or page clutter in the artwork.",
    "too_busy": "Some scenes felt too busy or unclear for a calm read-aloud storybook.",
    "too_scary": "Some imagery felt too intense or scary for a cozy 4-year-old story.",
    "pacing_off": "The movie pacing or page timing felt off during the final storybook playback.",
    "camera_motion": "The camera motion, pans, or zooms felt choppy instead of smooth and story-led.",
}

_INITIAL_SCENE_PLACEHOLDER = "No image yet — the story is just beginning!"
_DEFAULT_STORYBOOK_ELEVENLABS_VOICE_ID = (
    str(os.environ.get("PAGE_READ_ALOUD_ELEVENLABS_VOICE_ID") or "").strip()
    or str(os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    or "21m00Tcm4TlvDq8ikWAM"
)
_SHARED_TOY_COMPANION_NAME = "shared toy companion"
_TOY_NAME_HINT_PATTERNS = (
    re.compile(
        r"\b(?:his|her|their)\s+name\s+is\s+([A-Za-z0-9][A-Za-z0-9' -]{0,40}?)(?=[,.!?]|$|\s+(?:and|with|who|that|because|but)\b)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:this|it|he|she|they)\s+(?:is|are|'s)\s+([A-Za-z0-9][A-Za-z0-9' -]{0,40}?)(?=[,.!?]|$|\s+(?:and|with|who|that|because|but)\b)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\bthat(?:'s| is)\s+([A-Za-z0-9][A-Za-z0-9' -]{0,40}?)(?=[,.!?]|$|\s+(?:and|with|who|that|because|but)\b)",
        flags=re.IGNORECASE,
    ),
)
_GENERIC_TOY_NAME_HINTS = {
    "toy",
    "my toy",
    "the toy",
    "this toy",
    "that toy",
    "stuffie",
    "stuffed animal",
    "photo",
    "picture",
    "camera",
}
_VOICE_TOY_COMPANION_RE = re.compile(
    r"\b(?:toy|stuffie|stuffed animal|plush|companion|sidekick)\b",
    flags=re.IGNORECASE,
)
_VOICE_TOY_BRING_ALONG_RE = re.compile(
    r"\b(?:come(?:\s+with\s+us|\s+too|\s+along)?|join us|bring(?:\s+(?:him|her|it|them))?(?:\s+with\s+us|\s+along)?|along the journey)\b",
    flags=re.IGNORECASE,
)

_SESSION_STATE_DEFAULTS: dict[str, Any] = {
    "child_name": "friend",
    "child_age": 4,
    "child_age_band": "4-5",
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
    "story_phase": "opening",
    "toy_share_active": False,
    "toy_share_turns_remaining": 0,
    "toy_reference_visual_summary": "",
    "toy_reference_name_hint": "",
    "toy_share_resume_story_summary": "",
    "toy_share_resume_scene_description": "",
    "toy_share_resume_storybeat_text": "",
    "scene_branch_points": [],
    "story_pages": [],
    "pending_scene_branch_number": 0,
    "pending_scene_branch_label": "",
    "awaiting_story_choice": False,
    "pending_story_hint": "",
    "assembly_wait_last_child_utterance": "",
    "story_tone": "cozy",
    "storybook_movie_pacing": "read_with_me",
    "storybook_elevenlabs_voice_id": _DEFAULT_STORYBOOK_ELEVENLABS_VOICE_ID,
    "assembly_kind": "initial",
    "assembly_status": "",
    "scene_render_pending": False,
    "scene_render_skipped": False,
    "pending_scene_replacement_text": "",
    "pending_scene_replacement_phase": "",
    "pending_scene_replacement_armed_at_epoch_ms": 0,
    "queued_scene_child_utterance": "",
    "queued_scene_child_utterance_at_epoch_ms": 0,
    "theater_release_ready": False,
    "story_summary": "",
    "sidekick_description": "a magical companion",
    "character_facts": "",
    "character_facts_list": [],
    "generated_asset_urls": [],
    "scene_asset_urls": [],
    "scene_asset_gcs_uris": [],
    "scene_descriptions": [],
    "scene_storybeat_texts": [],
    "scene_lighting_cues": [],
    "theater_lighting_cues": [],
    "canonical_scene_description": "",
    "canonical_scene_storybeat_text": "",
    "canonical_scene_thumbnail_b64": "",
    "canonical_scene_thumbnail_mime": "",
    "current_scene_visual_summary": "",
    "previous_scene_visual_summary": "",
    "canonical_scene_visual_summary": "",
    "character_bible": {},
    "current_visual_continuity_plan": {
        "previous_location": "",
        "target_location": "",
        "transition_type": "",
        "active_character_keys": [],
        "active_character_labels": [],
        "required_prop_keys": [],
        "required_prop_labels": [],
        "forbidden_drift": [],
        "continuity_notes": [],
    },
    "last_scene_visual_audit": {
        "status": "pass",
        "should_retry": False,
        "repair_prompt_suffix": "",
        "notes": [],
        "issues": [],
    },
    "child_delight_anchors": [],
    "child_delight_anchors_text": "None saved yet.",
    "continuity_entity_registry": {
        "characters": {},
        "locations": {},
        "props": {},
    },
    "continuity_world_state": {
        "scene_index": 0,
        "current_location_key": "",
        "current_location_label": "",
        "previous_location_key": "",
        "previous_location_label": "",
        "active_character_keys": [],
        "active_prop_keys": [],
        "goal": "",
        "last_transition": "",
        "pending_request": "",
        "pending_location_key": "",
        "pending_location_label": "",
        "pending_transition": "",
        "pending_character_keys": [],
        "pending_prop_keys": [],
    },
    "continuity_scene_history": [],
    "recent_scene_references": [],
    "continuity_registry_text": "No recurring entities tracked yet.",
    "continuity_world_state_text": "No scene-to-scene world state established yet.",
    "turn_number": 1,
    "response_turn_number": 1,
    "max_story_turns": _MAX_STORY_TURNS,
    "max_story_turns_minus_one": max(3, _MAX_STORY_TURNS - 1),
    "story_turn_limit_reached": False,
    "story_page_count": 0,
    "story_pages_remaining": _MAX_STORY_TURNS,
    "story_page_limit_reached": False,
    "state_snapshots": [],
    "current_scene_description": _INITIAL_SCENE_PLACEHOLDER,
    "pending_scene_description": "",
    "pending_scene_base_description": "",
    "current_scene_storybeat_text": "",
    "pending_response": False,
    "pending_response_interrupted": False,
    "pending_response_token": "",
    "last_child_utterance": "",
    "partial_child_utterance": "",
    "partial_child_utterance_finished": False,
    "scene_tool_turn_open": False,
}


def _sync_story_page_progress_fields(state: dict[str, Any] | None) -> None:
    if not isinstance(state, dict):
        return
    try:
        max_turns = int(state.get("max_story_turns", _MAX_STORY_TURNS) or _MAX_STORY_TURNS)
    except Exception:
        max_turns = _MAX_STORY_TURNS
    max_turns = max(_MIN_STORY_TURNS, min(max_turns, _MAX_STORY_TURNS_HARD))
    rendered_page_count = count_rendered_story_pages(state)
    state["story_page_count"] = rendered_page_count
    state["story_pages_remaining"] = max(max_turns - rendered_page_count, 0)
    state["story_page_limit_reached"] = rendered_page_count >= max_turns


def _bump_live_telemetry(metric: str, amount: int = 1) -> None:
    try:
        _live_telemetry_counters[metric] += amount
    except Exception:
        pass


def _ensure_session_state_defaults(state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    for key, value in _SESSION_STATE_DEFAULTS.items():
        if key not in state:
            state[key] = copy.deepcopy(value)
    ensure_story_continuity_state(state)
    _sync_story_page_progress_fields(state)
    return state


def _state_has_prior_story_context(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict) or not state:
        return False
    if bool(state.get("story_started", False)) or bool(state.get("name_confirmed", False)):
        return True
    child_name = str(state.get("child_name", "") or "").strip().lower()
    if child_name and child_name != "friend":
        return True
    if str(state.get("pending_child_name", "") or "").strip():
        return True
    if str(state.get("pending_story_hint", "") or "").strip():
        return True
    if str(state.get("last_child_utterance", "") or "").strip():
        return True
    if str(state.get("assembly_status", "") or "").strip():
        return True
    if str(state.get("story_summary", "") or "").strip():
        return True
    try:
        if int(state.get("turn_number", 1) or 1) > 1:
            return True
    except Exception:
        pass
    try:
        if int(state.get("response_turn_number", 1) or 1) > 1:
            return True
    except Exception:
        pass
    current_scene_description = str(state.get("current_scene_description", "") or "").strip()
    if current_scene_description and current_scene_description != _INITIAL_SCENE_PLACEHOLDER:
        return True
    scene_urls = state.get("scene_asset_urls")
    if isinstance(scene_urls, list) and any(str(item or "").strip() for item in scene_urls):
        return True
    branch_points = state.get("scene_branch_points")
    if isinstance(branch_points, list) and len(branch_points) > 0:
        return True
    return False


def _opening_phase_needs_first_greeting(state: dict[str, Any] | None) -> bool:
    """Returns True only before the first real greeting has landed."""
    if not isinstance(state, dict):
        return True
    if bool(state.get("story_started", False)) or bool(state.get("name_confirmed", False)):
        return False
    if str(state.get("story_summary", "") or "").strip():
        return False
    if str(state.get("last_child_utterance", "") or "").strip():
        return False
    if str(state.get("pending_story_hint", "") or "").strip():
        return False
    if str(state.get("pending_child_name", "") or "").strip():
        return False
    try:
        response_turn_number = int(state.get("response_turn_number", state.get("turn_number", 1)) or 1)
    except Exception:
        response_turn_number = 1
    return response_turn_number <= 1


def _current_rss_mb() -> float | None:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return round(int(parts[1]) / 1024.0, 2)
    except Exception:
        pass
    return None


def _runtime_pressure_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "active_ws_sessions": len(_active_connection_ids),
        "assistant_speaking_sessions": len(_assistant_speaking_sessions),
        "active_activity_sessions": len(_activity_active_sessions),
        "awaiting_greeting_sessions": len(_awaiting_greeting_sessions),
        "ending_story_sessions": len(_ending_story_sessions),
    }
    rss_mb = _current_rss_mb()
    if rss_mb is not None:
        snapshot["rss_mb"] = rss_mb
    try:
        maxrss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        snapshot["maxrss_mb"] = round(maxrss_kb / 1024.0, 2)
    except Exception:
        pass
    try:
        load1, load5, load15 = os.getloadavg()
        snapshot["loadavg_1m"] = round(load1, 2)
        snapshot["loadavg_5m"] = round(load5, 2)
        snapshot["loadavg_15m"] = round(load15, 2)
    except Exception:
        pass
    return snapshot


def _child_turn_loss_rate() -> float:
    attempted = int(_live_telemetry_counters.get("child_turn.attempted", 0))
    if attempted <= 0:
        return 0.0
    lost = int(_live_telemetry_counters.get("child_turn.lost", 0))
    return round(lost / attempted, 4)


def _emit_live_telemetry(
    event: str,
    *,
    session_id: str | None = None,
    include_runtime: bool = False,
    **fields: Any,
) -> None:
    payload: dict[str, Any] = {
        "event": event,
        "child_turn_loss_rate": _child_turn_loss_rate(),
        "counters": {
            "child_turn_attempted": int(_live_telemetry_counters.get("child_turn.attempted", 0)),
            "child_turn_answered": int(_live_telemetry_counters.get("child_turn.answered", 0)),
            "child_turn_lost": int(_live_telemetry_counters.get("child_turn.lost", 0)),
            "child_turn_interrupted": int(_live_telemetry_counters.get("child_turn.interrupted", 0)),
            "child_turn_recovered": int(_live_telemetry_counters.get("child_turn.recovered", 0)),
            "live_hard_resets": int(_live_telemetry_counters.get("live_reset.hard", 0)),
            "live_retry_resets": int(_live_telemetry_counters.get("live_reset.retryable", 0)),
            "disconnect_proxy": int(_live_telemetry_counters.get("disconnect.proxy", 0)),
            "disconnect_websocket": int(_live_telemetry_counters.get("disconnect.websocket", 0)),
            "disconnect_timeout": int(_live_telemetry_counters.get("disconnect.timeout", 0)),
            "disconnect_transport_stale": int(_live_telemetry_counters.get("disconnect.transport_stale", 0)),
            "disconnect_heartbeat_stale": int(_live_telemetry_counters.get("disconnect.heartbeat_stale", 0)),
        },
    }
    if session_id:
        payload["session_id"] = session_id
    if include_runtime:
        payload["runtime"] = _runtime_pressure_snapshot()
    payload.update(fields)
    try:
        logger.info("LIVE_TELEMETRY %s", json.dumps(payload, sort_keys=True, default=str))
    except Exception:
        logger.info("LIVE_TELEMETRY %s", payload)


def get_live_telemetry_snapshot() -> dict[str, Any]:
    counters = dict(_live_telemetry_counters)
    return {
        "runtime": _runtime_pressure_snapshot(),
        "counters": counters,
        "child_turn_loss_rate": _child_turn_loss_rate(),
    }


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


def _barge_in_enabled() -> bool:
    """Barge-in is on unless the deploy explicitly disables it."""
    return not _env_enabled("DISABLE_BARGE_IN", default=False)


def _activate_barge_in(session_id: str) -> None:
    """Marks the current assistant turn as interrupted and drops residual output."""
    _interrupted_turn_sessions.add(session_id)
    _assistant_speaking_sessions.discard(session_id)
    _assistant_speaking_since.pop(session_id, None)
    _awaiting_greeting_sessions.discard(session_id)


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


def _mark_client_transport_activity(session_id: str, *, heartbeat: bool = False) -> None:
    now = time.monotonic()
    _last_client_transport_at[session_id] = now
    if heartbeat:
        _last_client_heartbeat_at[session_id] = now


def _set_page_read_aloud_suppression(
    session_id: str,
    *,
    active: bool,
    suppress_for_ms: int = 0,
) -> None:
    now = time.monotonic()
    if active:
        _page_read_aloud_active_sessions.add(session_id)
    else:
        _page_read_aloud_active_sessions.discard(session_id)
    if suppress_for_ms > 0:
        _page_read_aloud_suppress_until[session_id] = now + (suppress_for_ms / 1000.0)
    elif not active:
        _page_read_aloud_suppress_until.pop(session_id, None)


def _page_read_aloud_suppression_active(session_id: str) -> bool:
    if session_id in _page_read_aloud_active_sessions:
        return True
    deadline = _page_read_aloud_suppress_until.get(session_id)
    if not deadline:
        return False
    if time.monotonic() < deadline:
        return True
    _page_read_aloud_suppress_until.pop(session_id, None)
    return False


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


class ResettableLiveRequestQueue(LiveRequestQueue):
    """A resettable ADK queue that still passes InvocationContext validation.

    Gemini Live can get stuck in 1007/1011 loops if a stale activity/content
    sequence survives a transport error. We keep the public type as a real
    LiveRequestQueue so Pydantic accepts it, but swap the underlying asyncio
    queue to discard stale frames.
    """

    def __init__(self) -> None:
        super().__init__()

    def reset(self, session_id: str, reason: str) -> None:
        old_queue = self._queue
        self._queue = asyncio.Queue()
        _record_live_request(session_id, "queue_reset", {"reason": reason})
        if reason.startswith("retryable_error_"):
            _bump_live_telemetry("live_reset.retryable")
        elif "hard_reset" in reason:
            _bump_live_telemetry("live_reset.hard")
        _bump_live_telemetry(f"live_reset.reason.{reason}")
        _emit_live_telemetry(
            "live_queue_reset",
            session_id=session_id,
            reason=reason,
            include_runtime=True,
        )
        try:
            old_queue.put_nowait(LiveRequest(close=True))
        except Exception:
            pass


async def _prepare_clean_live_reconnect(
    runner: Runner,
    user_id: str,
    session_id: str,
) -> None:
    """Clears stale live transport state after a websocket died mid-turn."""
    if session_id not in _clean_live_reconnect_sessions:
        return
    _clean_live_reconnect_sessions.discard(session_id)
    logger.warning("Preparing clean live reconnect for session %s after mid-turn disconnect.", session_id)
    _emit_live_telemetry(
        "clean_live_reconnect",
        session_id=session_id,
        include_runtime=True,
    )
    await _prune_session_history(runner, user_id, session_id)
    _audio_seen_this_turn.discard(session_id)
    _activity_active_sessions.discard(session_id)
    _activity_last_change.pop(session_id, None)


async def _promote_partial_child_utterance_to_pending(
    runner: Runner,
    user_id: str,
    session_id: str,
) -> None:
    """Carries a strong partial child utterance across a transport drop."""
    promoted = False

    def _promote(state: dict[str, Any]) -> None:
        nonlocal promoted
        if bool(state.get("pending_response", False)):
            return
        partial = str(state.get("partial_child_utterance", "") or "").strip()
        if not _partial_child_utterance_is_resumable(partial):
            return
        state["pending_response"] = True
        state["pending_response_interrupted"] = True
        state["scene_tool_turn_open"] = True
        state["pending_response_token"] = uuid.uuid4().hex
        state["last_child_utterance"] = partial
        promoted = True
        _bump_live_telemetry("child_turn.interrupted")

    await _mutate_state(
        runner=runner,
        user_id=user_id,
        session_id=session_id,
        mutator=_promote,
    )
    if promoted:
        _emit_live_telemetry(
            "promote_partial_child_utterance",
            session_id=session_id,
            include_runtime=False,
        )


async def _resume_pending_child_turn(
    runner: Runner,
    user_id: str,
    session_id: str,
    live_queue: LiveRequestQueue,
    *,
    recovery_reason: str,
) -> bool:
    """Last-resort replay of a pending child turn into a fresh Gemini Live session."""
    try:
        session = await runner.session_service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as exc:
        logger.debug("Pending turn resume lookup failed for %s: %s", session_id, exc)
        return False

    if not session or not session.state:
        return False

    state = session.state
    if not bool(state.get("pending_response")):
        return False

    resume_text = str(state.get("last_child_utterance", "") or "").strip()
    replacement_resume_text = str(state.get("pending_scene_replacement_text", "") or "").strip()
    if not resume_text:
        return False

    interrupted_resume = bool(state.get("pending_response_interrupted", False))

    def _clear_pending_on_resume(s: dict[str, Any]) -> None:
        s["pending_response"] = False
        s["pending_response_interrupted"] = False
        s["scene_tool_turn_open"] = False

    await _mutate_state(runner, user_id, session_id, _clear_pending_on_resume)

    if (
        replacement_resume_text
        and _pending_scene_replacement_phase(state) == _PENDING_SCENE_REPLACEMENT_PHASE_ACK
        and _should_preserve_pending_scene_render(session_id, state)
    ):
        _send_pending_scene_replacement_prompt(
            session_id,
            live_queue,
            replacement_resume_text,
        )
        return True

    if interrupted_resume:
        _bump_live_telemetry("child_turn.recovered")
        _emit_live_telemetry(
            "child_turn_recovered",
            session_id=session_id,
            include_runtime=False,
            reason=recovery_reason,
        )
        _send_live_content(
            session_id,
            live_queue,
            (
                "The child was cut off by a connection blip while saying: "
                f"\"{resume_text}\". Resume naturally from that likely idea. "
                "Do not ask them to repeat unless the meaning is truly impossible to infer."
            ),
        )
    else:
        _send_live_content(session_id, live_queue, resume_text)
    return True


def _resolve_image_prefs_from_state(state: dict[str, Any]) -> tuple[str, str, str]:
    aspect_ratio = str(state.get("preferred_aspect_ratio", "16:9"))
    if aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
        aspect_ratio = "16:9"
    image_size = os.environ.get("IMAGE_SIZE", "").strip() or str(state.get("preferred_image_size", "512px"))
    if image_size not in _ALLOWED_IMAGE_SIZES:
        image_size = "512px"
    image_model = os.environ.get("IMAGE_MODEL", "").strip() or _default_live_image_model()
    if not _is_supported_image_generation_model(image_model):
        image_model = _default_live_image_model()
    return aspect_ratio, image_size, image_model


_LOCATION_THEME_STOPWORDS = {
    "land",
    "world",
    "kingdom",
    "castle",
    "forest",
    "garden",
    "park",
    "town",
    "village",
    "city",
    "island",
    "planet",
    "moon",
    "beach",
    "mountain",
    "mountains",
}

_FALLBACK_DESTINATION_REQUEST_RE = re.compile(
    r"\b(?:go|going|take|head|travel|visit|fly|walk)\s+to\b"
    r"|\b(?:want|wanna)\s+to\s+go\s+to\b"
    r"|\blet'?s\s+go\s+to\b",
    flags=re.IGNORECASE,
)


def _location_theme_fragment(label: str) -> str:
    words = [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z'\-]+", str(label or ""))
        if token.lower() not in _LOCATION_THEME_STOPWORDS
    ]
    if not words:
        return ""
    return " ".join(words[:3])


def _named_destination_scene_descriptions(location_label: str, prompt_suffix: str = "") -> tuple[str, str]:
    label = re.sub(r"\s+", " ", str(location_label or "")).strip()
    if not label:
        return "", ""
    theme = _location_theme_fragment(label)
    if theme:
        base_description = (
            f"{label} shimmered with playful {theme} details, welcoming paths, and cozy magical landmarks."
        )
        prompt_description = (
            f"{label}. A bright magical children's storybook setting themed around {theme}, "
            f"with playful landmarks, welcoming paths, and cozy details that make {label} unmistakable."
        )
    else:
        base_description = f"{label} felt bright, magical, and full of playful landmarks and welcoming paths."
        prompt_description = (
            f"{label}. A bright magical children's storybook setting with playful landmarks, "
            f"welcoming paths, and cozy details that make {label} unmistakable."
        )
    cleaned_suffix = re.sub(r"\s+", " ", str(prompt_suffix or "")).strip().strip(".")
    if cleaned_suffix:
        prompt_description = f"{prompt_description} {cleaned_suffix}."
    return prompt_description[:600], base_description[:320]


def _fallback_scene_prompt_parts(
    assistant_text: str,
    child_text: str,
    state: dict[str, Any],
) -> tuple[str, str]:
    cleaned_child = _CTRL_TOKEN_RE.sub("", child_text or "").strip()
    cleaned_assistant = _CTRL_TOKEN_RE.sub("", assistant_text or "").strip()
    child_refresh = bool(cleaned_child and _child_requested_scene_refresh(cleaned_child))
    if child_refresh and _FALLBACK_DESTINATION_REQUEST_RE.search(cleaned_child):
        try:
            ensure_story_continuity_state(state)
            continuity_validation = validate_live_scene_request(state, cleaned_child)
        except Exception:
            continuity_validation = None
        location_label = ""
        prompt_suffix = ""
        if continuity_validation is not None:
            location_label = str(getattr(continuity_validation, "location_label", "") or "").strip()
            prompt_suffix = str(getattr(continuity_validation, "prompt_suffix", "") or "").strip()
        if location_label:
            prompt_description, base_description = _named_destination_scene_descriptions(
                location_label,
                prompt_suffix=prompt_suffix,
            )
            if prompt_description:
                return prompt_description, base_description or prompt_description
    text = cleaned_child if child_refresh else cleaned_assistant
    if not text:
        text = cleaned_child
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
    if continuity and not child_refresh:
        continuity = re.sub(r"\s+", " ", continuity).strip()
        tail = continuity[-260:]
        if tail and tail not in text:
            text = f"{text}. Keep temporal/character continuity with: {tail}."
    text = text[:600]
    return text, text


_IMAGE_CHAT_RE = re.compile(
    r"\b(what(?:'s| is| are)|why|who(?:'s| is| are)|how(?:'s| is| are| do| does)|tell me about|i see|look at|look!|that's|that is|it's|it is|so pretty|sparkly|cute|funny|what color|where is|do you see|wow|cool|silly)\b",
    flags=re.IGNORECASE,
)
_OFFTOPIC_CHAT_RE = re.compile(
    r"\b(?:can|could|do)\s+you\s+(?:still\s+)?(?:hear|see|understand|remember)\s+me\b"
    r"|\bare\s+you\s+there\b"
    r"|\bare\s+you\s+listening\b"
    r"|\btell\s+me\s+(?:a\s+)?joke\b"
    r"|\b(?:what(?:'s| is)|how(?:'s| is))\s+the\s+weather\b"
    r"|\b(?:stock|stocks|stock\s+price|market)\b",
    flags=re.IGNORECASE,
)
_MATH_CHAT_RE = re.compile(
    r"^\s*\d+(?:\s*[+\-*/xX]\s*\d+)+(?:\s*=\s*\d+)?\s*$",
    flags=re.IGNORECASE,
)
_LAUGH_CHAT_RE = re.compile(
    r"\b(?:ha(?:ha)+|he(?:he)+|giggl(?:e|ing|y)|laugh(?:ing)?|so silly)\b",
    flags=re.IGNORECASE,
)
_SCENE_ACTION_RE = re.compile(
    r"\b(go|going|walk|run|fly|swim|sail|climb|crawl|hop|jump|open|enter|step|follow|ride|explore|peek|look inside|look behind|through|into|across|under|over|behind|find|search|discover|reach|arrive|visit|choose|pick|take|move|head|turn into|become|transform)\b"
    r"|\b(?:get|move|come|walk|go|step|head)\s+(?:closer|near|next to|over to|toward|towards|up to)\b"
    r"|\bcloser\s+to\b",
    flags=re.IGNORECASE,
)
_SCENE_EDIT_RE = re.compile(
    r"\b(add|change|make|turn|give|put)\b",
    flags=re.IGNORECASE,
)
_EXPLICIT_VISUAL_REQUEST_RE = re.compile(
    r"\bshow (?:me|us)\b"
    r"|\b(?:draw|picture|image|illustration)\b"
    r"|\b(?:with|in) an image\b"
    r"|\b(?:can|could) (?:i|we) see (?:it|that|the picture|the image|a picture|an image|the [a-z][a-z'\- ]{0,40})\b"
    r"|\btake me there\b",
    flags=re.IGNORECASE,
)
_READ_PAGE_REQUEST_RE = re.compile(
    r"\b(?:read|say|tell)\s+(?:the|this)?\s*page\b"
    r"|\bwhat(?:'s| does)\s+(?:this|the)\s+page\s+(?:say|says)\b"
    r"|\bwhat's\s+that\s+say\b"
    r"|\bread\s+(?:it|that)\s+to\s+me\b",
    flags=re.IGNORECASE,
)
_SCENE_DETAIL_LOOK_QUESTION_RE = re.compile(
    r"\bwhat\s+(?:do|does)\b.*\blook like\b",
    flags=re.IGNORECASE,
)
_PENDING_SCENE_REPLACE_RE = re.compile(
    r"^(?:no(?:,)?\s*)?wait\b"
    r"|^actually\b"
    r"|^not\b"
    r"|\binstead\b"
    r"|\bchange\s+(?:it|that|the\s+(?:picture|page|scene))\b"
    r"|\bdifferent\s+(?:one|picture|page|scene)\b",
    flags=re.IGNORECASE,
)
_PENDING_SCENE_SEQUENCE_RE = re.compile(
    r"\bafter\s+(?:that|this|we)\b"
    r"|\band\s+then\b"
    r"|\bthen\b"
    r"|\bnext(?:\s+page)?\b"
    r"|\bafterwards\b",
    flags=re.IGNORECASE,
)
_ASSISTANT_NEW_SCENE_RE = re.compile(
    r"\b(arrive|reach|step into|inside|through the|suddenly|now you're|now you are|ahead of you|in front of you|at the edge of|deep in|high above|beneath|beyond|opens into)\b",
    flags=re.IGNORECASE,
)


def _has_rendered_scene(state: dict[str, Any]) -> bool:
    scene_urls = list(state.get("scene_asset_urls", []) or [])
    current_scene = str(state.get("current_scene_description", "")).strip().lower()
    return bool(scene_urls) and not current_scene.startswith("no image yet")


def _child_requested_scene_refresh(text: str) -> bool:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip().lower()
    if not cleaned:
        return False
    has_action = bool(
        _SCENE_ACTION_RE.search(cleaned)
        or _SCENE_EDIT_RE.search(cleaned)
        or _EXPLICIT_VISUAL_REQUEST_RE.search(cleaned)
    )
    if _IMAGE_CHAT_RE.search(cleaned) and not has_action:
        return False
    return has_action


def _child_requested_scene_chat(text: str) -> bool:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip().lower()
    if not cleaned:
        return False
    if (
        _READ_PAGE_REQUEST_RE.search(cleaned)
        or _SCENE_DETAIL_LOOK_QUESTION_RE.search(cleaned)
        or _OFFTOPIC_CHAT_RE.search(cleaned)
        or _MATH_CHAT_RE.fullmatch(cleaned)
    ):
        return True
    if (
        _SCENE_ACTION_RE.search(cleaned)
        or _SCENE_EDIT_RE.search(cleaned)
        or _EXPLICIT_VISUAL_REQUEST_RE.search(cleaned)
    ):
        return False
    return bool(_IMAGE_CHAT_RE.search(cleaned) or _LAUGH_CHAT_RE.search(cleaned))


def _scene_render_in_progress(session_id: str, state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return session_id in _pending_image_events
    if bool(state.get("scene_render_skipped", False)):
        return False
    return bool(state.get("scene_render_pending", False)) or session_id in _pending_image_events


def _should_preserve_pending_scene_render(
    session_id: str,
    state: dict[str, Any] | None,
) -> bool:
    if not isinstance(state, dict):
        return session_id in _pending_image_events
    if bool(state.get("scene_render_skipped", False)):
        return False
    return bool(
        state.get("scene_render_pending", False)
        or str(state.get("pending_scene_description", "") or "").strip()
        or str(state.get("pending_scene_base_description", "") or "").strip()
        or session_id in _pending_image_events
    )


def _can_interact_with_pending_scene_render(
    session_id: str,
    state: dict[str, Any] | None,
) -> bool:
    if _storybook_assembly_in_progress(state):
        return False
    return _scene_render_in_progress(session_id, state)


def _should_reset_live_for_pending_scene_replacement(session_id: str) -> bool:
    # Replacement turns are safer when they begin from a fresh Live request queue.
    # Reusing the current queue lets stale narration/name-confirmation spill into
    # the acknowledgment turn before the new destination prompt takes effect.
    return True


def _should_activate_barge_in_for_pending_scene_replacement(session_id: str) -> bool:
    return (
        session_id in _assistant_speaking_sessions
        or session_id in _awaiting_greeting_sessions
    )


def _should_keep_live_open_for_pending_scene_replacement_follow_up(
    state: dict[str, Any] | None,
) -> bool:
    if not isinstance(state, dict):
        return False
    return bool(str(state.get("pending_scene_replacement_text", "") or "").strip())


def _pending_scene_replacement_phase(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return ""
    return str(state.get("pending_scene_replacement_phase", "") or "").strip()


def _should_ignore_nonpersistent_scene_ready(
    state: dict[str, Any] | None,
    *,
    request_id: str | None,
) -> bool:
    if not isinstance(state, dict):
        return False
    normalized_request_id = str(request_id or "").strip()
    pending_replacement_text = str(state.get("pending_scene_replacement_text", "") or "").strip()
    pending_replacement_phase = _pending_scene_replacement_phase(state)
    if pending_replacement_text and pending_replacement_phase in {
        _PENDING_SCENE_REPLACEMENT_PHASE_ACK,
        _PENDING_SCENE_REPLACEMENT_PHASE_RENDER,
    }:
        return True
    active_request_id = str(state.get("active_scene_request_id", "") or "").strip()
    if active_request_id and normalized_request_id and normalized_request_id != active_request_id:
        return True
    if (
        active_request_id
        and not normalized_request_id
        and (
            bool(state.get("scene_render_pending", False))
            or bool(str(state.get("pending_scene_description", "") or "").strip())
            or bool(str(state.get("pending_scene_base_description", "") or "").strip())
        )
    ):
        return True
    return False


def _pending_scene_replacement_armed_at_epoch_ms(state: dict[str, Any] | None) -> int:
    if not isinstance(state, dict):
        return 0
    try:
        return int(state.get("pending_scene_replacement_armed_at_epoch_ms", 0) or 0)
    except Exception:
        return 0


def _has_pending_scene_replacement(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    return bool(str(state.get("pending_scene_replacement_text", "") or "").strip())


def _is_pending_scene_replacement_ack_turn(
    state: dict[str, Any] | None,
    *,
    scene_visuals_called_this_turn: bool,
) -> bool:
    return (
        not scene_visuals_called_this_turn
        and _pending_scene_replacement_phase(state) == _PENDING_SCENE_REPLACEMENT_PHASE_ACK
        and _has_pending_scene_replacement(state)
    )


def _is_pending_scene_replacement_follow_up_turn(
    state: dict[str, Any] | None,
    *,
    scene_visuals_called_this_turn: bool,
) -> bool:
    if scene_visuals_called_this_turn:
        return False
    phase = _pending_scene_replacement_phase(state)
    return phase in {
        _PENDING_SCENE_REPLACEMENT_PHASE_ACK,
        _PENDING_SCENE_REPLACEMENT_PHASE_RENDER,
    } and _has_pending_scene_replacement(state)


def _should_retry_pending_scene_replacement_ack(
    state: dict[str, Any] | None,
    *,
    had_child_input_this_turn: bool,
    model_emitted_meaningful_output: bool,
    scene_visuals_called_this_turn: bool,
) -> bool:
    return (
        had_child_input_this_turn
        and not model_emitted_meaningful_output
        and _is_pending_scene_replacement_ack_turn(
            state,
            scene_visuals_called_this_turn=scene_visuals_called_this_turn,
        )
    )


def _should_hold_scene_tools_for_pending_replacement_ack(
    state: dict[str, Any] | None,
) -> bool:
    return _pending_scene_replacement_phase(state) == _PENDING_SCENE_REPLACEMENT_PHASE_ACK and _has_pending_scene_replacement(state)


_PENDING_SCENE_REPLACEMENT_ACK_SIGNAL_RE = re.compile(
    r"\b(?:okay|ok|got it|sure|alright|all right|switch|change|instead|we(?:'ll| will)|let'?s|hear you|heard you)\b",
    flags=re.IGNORECASE,
)
_PENDING_SCENE_REPLACEMENT_ACK_INVALID_RE = re.compile(
    r"\b(?:close your eyes|imagine|what kind of story|what story|what adventure|what should we do|what do you want|what would you like|is your name|did i hear|did i get your name|your magical name|is that right)\b",
    flags=re.IGNORECASE,
)


def _pending_scene_replacement_target_label(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return ""
    world = state.get("continuity_world_state", {})
    if isinstance(world, Mapping):
        pending_label = str(world.get("pending_location_label", "") or "").strip()
        if pending_label:
            return pending_label
    return ""


def _current_scene_location_label(state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return ""
    world = state.get("continuity_world_state", {})
    if isinstance(world, Mapping):
        current_label = str(world.get("current_location_label", "") or "").strip()
        if current_label:
            return current_label
    return ""


def _is_valid_pending_scene_replacement_ack_text(
    state: dict[str, Any] | None,
    text: str,
) -> bool:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip()
    if not _is_meaningful_text(cleaned):
        return False
    if _PENDING_SCENE_REPLACEMENT_ACK_INVALID_RE.search(cleaned):
        return False
    if _STORY_PROMPT_RE.search(cleaned):
        return False
    if _CAMERA_PROMPT_RE.search(cleaned):
        return False
    target_label = _pending_scene_replacement_target_label(state)
    mentions_target = bool(
        target_label
        and re.search(rf"\b{re.escape(target_label)}\b", cleaned, flags=re.IGNORECASE)
    )
    ack_signal = bool(_PENDING_SCENE_REPLACEMENT_ACK_SIGNAL_RE.search(cleaned))
    word_count = len(re.findall(r"[A-Za-z][A-Za-z'\-]+", cleaned))
    if word_count > 18 and not (ack_signal and mentions_target):
        return False
    if mentions_target:
        return ack_signal or word_count <= 12
    return ack_signal and word_count <= 12


def _should_abort_partial_pending_scene_replacement_ack(
    state: dict[str, Any] | None,
    text: str,
) -> bool:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip()
    if not _is_meaningful_text(cleaned):
        return False
    if _PENDING_SCENE_REPLACEMENT_ACK_INVALID_RE.search(cleaned):
        return True
    target_label = _pending_scene_replacement_target_label(state)
    current_label = _current_scene_location_label(state)
    mentions_target = bool(
        target_label
        and re.search(rf"\b{re.escape(target_label)}\b", cleaned, flags=re.IGNORECASE)
    )
    mentions_current = bool(
        current_label
        and re.search(rf"\b{re.escape(current_label)}\b", cleaned, flags=re.IGNORECASE)
    )
    ack_signal = bool(_PENDING_SCENE_REPLACEMENT_ACK_SIGNAL_RE.search(cleaned))
    word_count = len(re.findall(r"[A-Za-z][A-Za-z'\-]+", cleaned))
    if mentions_current and not mentions_target:
        return True
    if word_count >= 5 and not ack_signal and not mentions_target:
        return True
    return False


def _should_ignore_turn_complete_while_waiting_for_pending_replacement_ack(
    state: dict[str, Any] | None,
    *,
    last_finished_assistant_output_at_epoch_ms: int,
) -> bool:
    if not _should_hold_scene_tools_for_pending_replacement_ack(state):
        return False
    armed_at = _pending_scene_replacement_armed_at_epoch_ms(state)
    if armed_at > 0:
        return int(last_finished_assistant_output_at_epoch_ms or 0) <= armed_at
    return int(last_finished_assistant_output_at_epoch_ms or 0) <= 0


def _queue_latest_scene_follow_up_request(state: dict[str, Any], child_utterance: str) -> None:
    queued_text = str(child_utterance or "").strip()
    if not queued_text:
        return
    state["queued_scene_child_utterance"] = queued_text
    state["queued_scene_child_utterance_at_epoch_ms"] = int(time.time() * 1000)
    state["partial_child_utterance"] = queued_text
    state["partial_child_utterance_finished"] = True
    _capture_child_story_continuity(state, queued_text)


def _classify_pending_scene_interrupt(text: str) -> str:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip().lower()
    if not cleaned or not _child_requested_scene_refresh(cleaned):
        return ""
    if _PENDING_SCENE_REPLACE_RE.search(cleaned):
        return "replace"
    if _PENDING_SCENE_SEQUENCE_RE.search(cleaned):
        return "queue"
    return "replace"


def _discard_unshown_pending_scene_slot(state: dict[str, Any]) -> None:
    active_request_id = str(state.get("active_scene_request_id", "") or "").strip()
    if not active_request_id:
        return

    points = _scene_branch_points(state)
    kept_points: list[dict[str, Any]] = []
    removed_point = False
    for point in points:
        point_request_id = str(point.get("request_id", "") or "").strip()
        image_url = str(point.get("image_url", "") or "").strip()
        gcs_uri = str(point.get("gcs_uri", "") or "").strip()
        if point_request_id == active_request_id and not image_url and not gcs_uri:
            removed_point = True
            continue
        kept_points.append(point)
    if removed_point:
        state["scene_branch_points"] = kept_points[-20:]

    raw_pages = state.get("story_pages", [])
    if not isinstance(raw_pages, list):
        return
    kept_pages: list[dict[str, Any]] = []
    removed_page = False
    for page in raw_pages:
        if not isinstance(page, dict):
            continue
        page_request_id = str(page.get("request_id", "") or "").strip()
        image_url = str(page.get("image_url", "") or "").strip()
        gcs_uri = str(page.get("gcs_uri", "") or "").strip()
        if page_request_id == active_request_id and not image_url and not gcs_uri:
            removed_page = True
            continue
        kept_pages.append(dict(page))
    if removed_page:
        state["story_pages"] = kept_pages[-40:]


def _arm_pending_scene_replacement(state: dict[str, Any], child_utterance: str) -> str:
    replacement_text = str(child_utterance or "").strip()
    if not replacement_text:
        return ""
    _discard_unshown_pending_scene_slot(state)
    state["active_scene_request_id"] = ""
    state["pending_scene_description"] = ""
    state["pending_scene_base_description"] = ""
    state["pending_scene_replacement_text"] = replacement_text
    state["pending_scene_replacement_phase"] = _PENDING_SCENE_REPLACEMENT_PHASE_ACK
    state["pending_scene_replacement_armed_at_epoch_ms"] = int(time.time() * 1000)
    state["queued_scene_child_utterance"] = ""
    state["queued_scene_child_utterance_at_epoch_ms"] = 0
    state["pending_response"] = True
    state["pending_response_interrupted"] = False
    state["scene_tool_turn_open"] = True
    state["pending_response_token"] = uuid.uuid4().hex
    state["last_child_utterance"] = replacement_text
    state["partial_child_utterance"] = replacement_text
    state["partial_child_utterance_finished"] = True
    _capture_child_story_continuity(state, replacement_text)
    return replacement_text


def _mark_pending_scene_wait_response(
    state: dict[str, Any],
    *,
    child_utterance: str,
) -> None:
    # Keep render-wait chatter out of story continuity so casual small-talk
    # does not become a new canonical story wish.
    utterance = str(child_utterance or "").strip()
    state["pending_scene_replacement_text"] = ""
    state["pending_scene_replacement_phase"] = ""
    state["pending_scene_replacement_armed_at_epoch_ms"] = 0
    state["pending_response"] = True
    state["pending_response_interrupted"] = False
    state["scene_tool_turn_open"] = True
    state["pending_response_token"] = uuid.uuid4().hex
    state["last_child_utterance"] = utterance
    state["partial_child_utterance"] = utterance
    state["partial_child_utterance_finished"] = True


def _arm_queued_scene_follow_up_after_render(state: dict[str, Any]) -> str:
    queued_text = str(state.get("queued_scene_child_utterance", "") or "").strip()
    if not queued_text:
        return ""
    if bool(state.get("pending_response", False)) or bool(state.get("scene_tool_turn_open", False)):
        return ""
    if _storybook_assembly_in_progress(state):
        return ""
    state["queued_scene_child_utterance"] = ""
    state["queued_scene_child_utterance_at_epoch_ms"] = 0
    state["pending_scene_replacement_text"] = ""
    state["pending_scene_replacement_phase"] = ""
    state["pending_scene_replacement_armed_at_epoch_ms"] = 0
    state["pending_response"] = True
    state["pending_response_interrupted"] = False
    state["scene_tool_turn_open"] = True
    state["pending_response_token"] = uuid.uuid4().hex
    state["last_child_utterance"] = queued_text
    state["partial_child_utterance"] = queued_text
    state["partial_child_utterance_finished"] = True
    return queued_text


def _send_queued_scene_follow_up_prompt(
    session_id: str,
    live_queue: LiveRequestQueue,
    queued_text: str,
) -> None:
    cleaned = str(queued_text or "").strip()
    if not cleaned:
        return
    _send_live_content(
        session_id,
        live_queue,
        (
            "The child already asked for the next page while the last picture was still drawing: "
            f"\"{cleaned}\". "
            "Continue immediately from that newest request. "
            "Give at most one short acknowledgment if needed, then make the next page. "
            "Do not ask the child to repeat it."
        ),
    )


def _send_pending_scene_replacement_prompt(
    session_id: str,
    live_queue: LiveRequestQueue,
    replacement_text: str,
) -> None:
    cleaned = str(replacement_text or "").strip()
    if not cleaned:
        return
    _send_live_content(
        session_id,
        live_queue,
        (
            "The child changed their mind before the new page appeared. "
            "You are in an acknowledgment-only turn. "
            "Say exactly one short warm out-loud acknowledgment, like "
            "\"Okay, let's change it\" or \"Got it, we'll switch there.\" "
            f"The new destination is: \"{cleaned}\". "
            "Do NOT call any tools in this response. Do NOT describe the old page. "
            "Do NOT continue the story yet. The system will start the new picture right after you finish this short spoken acknowledgment. "
            "Do not stay silent, and do not ask the child to repeat it."
        ),
    )


def _publish_quick_ack(
    session_id: str,
    *,
    text: str = "Okay, let's change it.",
    interrupt_audio: bool = True,
) -> None:
    if not session_id:
        return
    publish_session_event(
        session_id,
        {
            "type": "quick_ack",
            "payload": {
                "text": str(text or "Okay, let's change it.").strip() or "Okay, let's change it.",
                "interrupt_audio": bool(interrupt_audio),
            },
        },
    )


def _should_trigger_fallback_scene(
    assistant_text: str,
    child_text: str,
    state: dict[str, Any],
) -> bool:
    cleaned_child = _CTRL_TOKEN_RE.sub("", child_text or "").strip()
    cleaned_assistant = _CTRL_TOKEN_RE.sub("", assistant_text or "").strip()
    if cleaned_child and _child_requested_scene_chat(cleaned_child):
        return False
    if (
        cleaned_child
        and _is_low_signal_single_word_child_utterance(cleaned_child, state)
        and not _child_requested_scene_refresh(cleaned_child)
    ):
        return False
    if (
        (
            bool(state.get("scene_render_pending", False))
            or bool(str(state.get("pending_scene_description", "") or "").strip())
            or bool(str(state.get("pending_scene_base_description", "") or "").strip())
        )
        and cleaned_child
        and not _child_requested_scene_refresh(cleaned_child)
    ):
        return False
    if not _has_rendered_scene(state):
        return bool(cleaned_child and cleaned_assistant)
    if _READ_PAGE_REQUEST_RE.search(cleaned_child.lower()):
        return False
    if _child_requested_scene_refresh(child_text):
        return True
    return bool(cleaned_assistant and _ASSISTANT_NEW_SCENE_RE.search(cleaned_assistant))


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
    description, base_description = _fallback_scene_prompt_parts(assistant_text, child_text, state)
    aspect_ratio, image_size, image_model = _resolve_image_prefs_from_state(state)
    delivery_format, delivery_quality, delivery_max_side = _resolve_delivery_preferences(state, image_size)
    request_id = uuid.uuid4().hex
    try:
        args = VisualArgs(
            description=description,
            base_description=base_description,
            negative_prompt=VisualArgs.model_fields["negative_prompt"].default,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            image_model=image_model,
            delivery_format=delivery_format,
            delivery_quality=delivery_quality,
            delivery_max_side=delivery_max_side,
            request_id=request_id,
        )
    except Exception:
        return
    try:
        await _mutate_state(
            runner,
            user_id,
            session_id,
            lambda state: _prime_pending_scene_request(
                state,
                request_id=request_id,
                description=description,
                base_description=base_description,
            ),
        )
    except Exception:
        logger.debug("Could not prime fallback scene request state for session %s", session_id, exc_info=True)
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
                        "request_id": request_id,
                    },
                })
            )
        except Exception:
            pass
    logger.info("Triggering fallback scene generation for session %s: %s", session_id, description[:100])
    _scene_gen_requested_at[session_id] = time.monotonic()
    _record_live_request(session_id, "fallback_scene", {"desc": description[:140]})
    schedule_background_task(_run_visual_pipeline(args=args, session_id=session_id))


async def _trigger_pending_scene_replacement_scene(
    session_id: str,
    replacement_text: str,
    runner: Runner,
    websocket: WebSocket,
    user_id: str,
) -> None:
    cleaned = _CTRL_TOKEN_RE.sub("", replacement_text or "").strip()
    if not cleaned:
        return
    logger.info(
        "Triggering replacement scene generation for session %s: %s",
        session_id,
        cleaned[:100],
    )
    await _trigger_fallback_scene(
        session_id=session_id,
        assistant_text="",
        child_text=cleaned,
        runner=runner,
        websocket=websocket,
        user_id=user_id,
    )


def _should_auto_confirm_name_on_story_shortcircuit(
    utterance_text: str | None,
    detected_name: str | None,
    child_age: int,
) -> bool:
    if not str(detected_name or "").strip():
        return False
    if int(child_age or 0) > 5:
        return True
    cleaned = _CTRL_TOKEN_RE.sub("", utterance_text or "").strip()
    if not cleaned:
        return False
    return bool(_NAME_PHRASE_RE.search(cleaned))


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


def _sniff_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


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
    return re.sub(r"\s+", " ", text).strip()


def _shared_item_vision_model() -> str:
    return (
        os.environ.get("SHARED_ITEM_VISION_MODEL", "").strip()
        or os.environ.get("STORYBOOK_POST_MOVIE_REVIEW_MODEL", "").strip()
        or "gemini-2.5-flash"
    )


async def _describe_shared_item_image(image_bytes: bytes) -> str:
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
        "Describe only the visible details of this child's toy or special-item photo. "
        "Use one short concrete sentence under 35 words. Mention 2-4 obvious traits like color, shape, face, wheels, outfit, sparkles, or pose. "
        "Do not guess brand names, logos, or anything not clearly visible."
    )

    def _run() -> str:
        client = _build_google_genai_client()
        response = client.models.generate_content(
            model=_shared_item_vision_model(),
            contents=[
                prompt,
                google_genai.types.Part.from_bytes(
                    data=vision_bytes,
                    mime_type=vision_mime,
                ),
            ],
            config=google_genai.types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=80,
            ),
        )
        return _extract_response_text(response).strip().strip("\"'")[:220]

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warning("Shared-item vision summary failed: %s", exc)
        return ""


async def _detect_shared_item_subject_thumbnail(
    image_bytes: bytes,
    *,
    toy_name_hint: str = "",
    summary_text: str = "",
) -> tuple[str, str] | None:
    if not image_bytes:
        return None

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

    clue_bits: list[str] = []
    normalized_name = _clean_shared_toy_name_hint(toy_name_hint)
    if normalized_name:
        clue_bits.append(f"The child calls it {normalized_name}.")
    if summary_text:
        clue_bits.append(f"Visible details: {str(summary_text).strip()[:220]}")
    prompt = (
        "Find the main toy or special item the child is sharing in this photo. "
        "Return JSON only with keys visible, x, y, width, height, and notes. "
        "Use normalized coordinates from 0 to 1000, with x/y as the top-left corner. "
        "The box should tightly frame the toy or special item itself, not the whole room or the child's hand unless needed to include the toy. "
        "If no single toy or item is clearly visible, return {\"visible\": false}."
    )
    if clue_bits:
        prompt += " " + " ".join(clue_bits)

    def _run() -> tuple[str, str] | None:
        client = _build_google_genai_client()
        response = client.models.generate_content(
            model=_shared_item_vision_model(),
            contents=[
                prompt,
                google_genai.types.Part.from_bytes(
                    data=vision_bytes,
                    mime_type=vision_mime,
                ),
            ],
            config=google_genai.types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=160,
                response_mime_type="application/json",
            ),
        )
        raw_text = _extract_response_text(response)
        if not raw_text:
            return None
        try:
            payload = json.loads(raw_text)
        except Exception:
            logger.warning("Shared-item subject crop returned non-JSON payload: %s", raw_text[:200])
            return None
        if not isinstance(payload, dict) or not bool(payload.get("visible", False)):
            return None
        crop_box = {
            "x": int(payload.get("x", 0) or 0),
            "y": int(payload.get("y", 0) or 0),
            "width": int(payload.get("width", 0) or 0),
            "height": int(payload.get("height", 0) or 0),
        }
        if crop_box["width"] <= 0 or crop_box["height"] <= 0:
            return None
        cropped = _crop_image_to_thumbnail_b64(image_bytes, crop_box=crop_box, max_side=384)
        if not cropped:
            return None
        return cropped

    try:
        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.warning("Shared-item subject crop failed: %s", exc)
        return None


def _clean_shared_toy_name_hint(candidate: Any) -> str:
    cleaned = re.sub(r"\s+", " ", str(candidate or "")).strip(" \"'.,!?")
    cleaned = re.sub(r"^(?:my|the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+from\s+[A-Za-z0-9][A-Za-z0-9' -]{1,40}$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return ""
    if len(cleaned) > 48 or len(cleaned.split()) > 4:
        return ""
    if cleaned.lower() in _GENERIC_TOY_NAME_HINTS:
        return ""
    return cleaned


def _extract_shared_toy_name_hint(text: Any) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return ""

    for pattern in _TOY_NAME_HINT_PATTERNS:
        match = pattern.search(compact)
        if match:
            name_hint = _clean_shared_toy_name_hint(match.group(1))
            if name_hint:
                return name_hint

    fallback = _clean_shared_toy_name_hint(compact)
    if fallback and 1 <= len(fallback.split()) <= 3:
        lowered = fallback.lower()
        if not re.search(r"\b(?:go|look|see|share|show|want|like|have|there|here|is|are|looks|has|with|and|but)\b", lowered):
            return fallback
    return ""


def _shared_toy_sidekick_description(summary_text: str, toy_name_hint: str) -> str:
    normalized_summary = str(summary_text or "").strip()
    normalized_name = _clean_shared_toy_name_hint(toy_name_hint)
    if not normalized_name or not normalized_summary:
        return normalized_summary
    if normalized_name.lower() in normalized_summary.lower():
        return normalized_summary
    lowered_summary = normalized_summary[:1].lower() + normalized_summary[1:]
    return f"{normalized_name}, {lowered_summary}"


def _promote_voice_toy_companion_from_utterance(
    state: dict[str, Any],
    utterance_text: Any,
) -> None:
    cleaned = re.sub(r"\s+", " ", str(utterance_text or "")).strip()
    if not cleaned:
        return
    toy_signal = bool(_VOICE_TOY_COMPANION_RE.search(cleaned))
    companion_signal = bool(_VOICE_TOY_BRING_ALONG_RE.search(cleaned))
    existing_name_hint = _clean_shared_toy_name_hint(state.get("toy_reference_name_hint", ""))
    existing_sidekick_description = str(state.get("sidekick_description", "") or "").strip()
    extracted_name_hint = _extract_shared_toy_name_hint(cleaned)
    toy_name_hint = _clean_shared_toy_name_hint(extracted_name_hint or existing_name_hint)
    if not toy_signal and not companion_signal and not toy_name_hint:
        return
    existing_toy_context = bool(
        existing_name_hint
        or str(state.get("toy_reference_visual_summary", "") or "").strip()
        or str(state.get("toy_reference_thumbnail_b64", "") or "").strip()
        or bool(state.get("camera_received", False))
        or bool(state.get("toy_share_active", False))
        or (
            existing_sidekick_description
            and existing_sidekick_description != "a magical companion"
            and any(
                token in existing_sidekick_description.lower()
                for token in ("toy companion", "shared toy", "plush", "stuffed animal", "stuffie")
            )
        )
    )
    if not toy_signal and not companion_signal and not (existing_toy_context and extracted_name_hint):
        return

    if toy_name_hint and not existing_name_hint:
        state["toy_reference_name_hint"] = toy_name_hint

    if not existing_sidekick_description or existing_sidekick_description == "a magical companion":
        state["sidekick_description"] = (
            f"{toy_name_hint}, the child's toy companion"
            if toy_name_hint else
            "the child's toy companion"
        )

    continuity_label = toy_name_hint or _SHARED_TOY_COMPANION_NAME
    continuity_fact = (
        f"{toy_name_hint}, the child's recurring toy companion"
        if toy_name_hint else
        "the child's recurring toy companion"
    )
    _upsert_character_fact(
        state,
        character_name=_SHARED_TOY_COMPANION_NAME,
        fact=continuity_fact,
    )
    if toy_name_hint:
        _upsert_character_fact(
            state,
            character_name=toy_name_hint,
            fact="shared toy helper and recurring companion in the story",
        )
    prime_character_carryover(
        state,
        [continuity_label],
        source="voice_toy",
        description=str(state.get("sidekick_description", "") or "").strip() or continuity_fact,
    )


def _capture_child_story_continuity(state: dict[str, Any], utterance_text: str) -> None:
    update_continuity_from_child_utterance(state, utterance_text)
    _promote_voice_toy_companion_from_utterance(state, utterance_text)


def _read_story_turn_limit() -> int:
    raw = os.environ.get("MAX_STORY_TURNS", "20")
    try:
        parsed = int(raw)
    except ValueError:
        parsed = 20
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
        _ensure_session_state_defaults(storage_session.state)
        before_state = dict(storage_session.state)
        mutator(storage_session.state)
        _ensure_session_state_defaults(storage_session.state)
        
        # Log every state mutation for deep observability.
        changes = {
            k: v for k, v in storage_session.state.items() 
            if k not in before_state or before_state[k] != v
        }
        if changes and "state_snapshots" not in changes:  # don't spam the console with massive snapshots
            logger.info(f"🔍 STATE MUTATION [{session_id[:8]}]: {changes}")
        try:
            cache_storybook_state(session_id, dict(storage_session.state))
        except Exception:
            pass
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
            _ensure_session_state_defaults(session.state)
            mutator(session.state)
            _ensure_session_state_defaults(session.state)
            try:
                cache_storybook_state(session_id, dict(session.state))
            except Exception:
                pass
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
    if not snapshots:
        return
    previous = dict(snapshots[-1])
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


def _append_child_delight_anchor(state: dict[str, Any], child_text: str) -> None:
    cleaned = _normalize_transcript_text(child_text)
    if not cleaned:
        return
    lowered = cleaned.lower()
    tokens = _speech_tokens(cleaned)
    if (
        not tokens
        or (len(tokens) == 1 and len(tokens[0]) < 6)
        or _NAME_PHRASE_RE.search(cleaned)
        or _TOY_SHARE_REQUEST_RE.search(cleaned)
        or _TOY_SHARE_CLOSE_RE.search(cleaned)
        or _RESTART_STORY_RE.search(cleaned)
        or _END_STORY_RE.search(cleaned)
        or _MIC_OFF_RE.search(cleaned)
        or _MIC_ON_RE.search(cleaned)
        or lowered in {"ready", "okay", "ok", "yes please", "no thank you"}
    ):
        return

    anchors_raw = state.get("child_delight_anchors", [])
    anchors = [str(item).strip() for item in anchors_raw if str(item).strip()] if isinstance(anchors_raw, list) else []
    canonical = _canonicalize_finished_child_transcript(cleaned)
    if not canonical:
        return
    if any(_canonicalize_finished_child_transcript(existing) == canonical for existing in anchors):
        return
    anchors.append(cleaned[:96])
    anchors = anchors[-6:]
    state["child_delight_anchors"] = anchors
    state["child_delight_anchors_text"] = "\n".join(f"- {anchor}" for anchor in anchors) if anchors else "None saved yet."


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
_SELF_INTRO_NAME_PREFIX_RE = re.compile(r"^(?:i am|i['’]m)\b", flags=re.IGNORECASE)
_SELF_INTRO_NAME_TRAILING_OK_RE = re.compile(
    r"^(?:"
    r"[.!?,;:]+"
    r"|\band\b"
    r"|\bbut\b"
    r"|\bso\b"
    r"|\bcan\b"
    r"|\bcould\b"
    r"|\bwill\b"
    r"|\bwould\b"
    r"|\bshould\b"
    r"|\bmay\b"
    r"|\bdo\b"
    r"|\bdid\b"
    r"|\bwe\b"
    r"|\bwhat\b"
    r"|\bwhere\b"
    r"|\bwhen\b"
    r"|\bwhy\b"
    r"|\bhow\b"
    r"|\blet\b"
    r"|\blet['’]s\b"
    r"|\bplease\b"
    r")",
    flags=re.IGNORECASE,
)
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
    r"\b(what kind of story|what story|what adventure|what should we do|what do you want to do|what do you want to see|what would you like to see|where should we go|where do you want to go)\b",
    flags=re.IGNORECASE,
)
_TOY_SHARE_REQUEST_RE = re.compile(
    r"\b("
    r"(?:can i|could i|may i|let me|i want to|i wanna)\s+(?:share|show|hare)\b"
    r"|(?:show|share|hare)\s+(?:you\s+)?(?:my\s+|this\s+|a\s+)?(?:toy|stuffie|stuffed animal|picture|pic|photo)\b"
    r"|(?:share|show|hare)\s+something\s+with\s+you\b"
    r"|(?:can i|could i|may i)\s+(?:share|hare)\s+something\s+with\s+you\b"
    r")",
    flags=re.IGNORECASE,
)
_TOY_SHARE_CLOSE_RE = re.compile(
    r"\b(back to (?:the )?story|let'?s go back|all done(?: with (?:the )?(?:toy|show and tell))?|done sharing|close (?:the )?(?:toy|camera|picture)|that's all)\b",
    flags=re.IGNORECASE,
)
_RESTART_STORY_RE = re.compile(
    r"^\s*(?:restart|start over|new story|another story|do over)\b",
    flags=re.IGNORECASE,
)
_END_STORY_RE = re.compile(
    r"^\s*(?:"
    r"the end"
    r"|end (?:the|this)? story(?: now)?"
    r"|finish (?:the )?story(?: now)?"
    r"|(?:let'?s|lets|please|can we|could we|i want to|we want to)\s+(?:make|start)\s+(?:the|this|our|my)\s+movie(?: now)?"
    r"|(?:make|start)\s+(?:the|this|our|my)\s+movie(?: now)?"
    r"|we(?:'re| are) done(?: with (?:the )?story)?"
    r"|story is over"
    r")\s*[.!?]*\s*$",
    flags=re.IGNORECASE,
)
_MIC_OFF_RE = re.compile(
    r"\b(?:mute|turn|switch)\s+(?:the\s+)?(?:mic|microphone)\s+(?:off|down)\b|\bstop listening\b",
    flags=re.IGNORECASE,
)
_MIC_ON_RE = re.compile(
    r"\b(?:unmute|turn|switch)\s+(?:the\s+)?(?:mic|microphone)\s+(?:on|back on)\b|\bstart listening\b",
    flags=re.IGNORECASE,
)
_SCENE_BRANCH_REQUEST_RE = re.compile(
    r"\b(?:go|jump|take me|bring me|can we|could we|let'?s|i want(?: to)?|rewind)\s+back\b"
    r"|\b(?:rewind|go back|back to)\s+(?:to\s+)?(?:scene|page|part)\b"
    r"|\bscene\s+\d+(?:st|nd|rd|th)?\b",
    flags=re.IGNORECASE,
)
_SCENE_BRANCH_CONFIRM_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|okay|ok|sure|do it|go back|let'?s do it|take us there|that's right)\b",
    flags=re.IGNORECASE,
)
_SCENE_BRANCH_CANCEL_RE = re.compile(
    r"^\s*(?:no|nope|nah|cancel|never mind|dont|don't|stay here|keep going|keep this one)\b",
    flags=re.IGNORECASE,
)
_SCENE_NUMBER_REF_RE = re.compile(
    r"\b(?:scene|page)\s+(\d{1,2}(?:st|nd|rd|th)?|first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\b"
    r"|\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth)\s+(?:scene|page)\b",
    flags=re.IGNORECASE,
)
_SPEECH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_TRANSCRIPTION_META_TOKEN_RE = re.compile(
    r"(?i)(?:<|\[)\s*(?:noise|silence|inaudible|unclear|background|music|cough|breath|breathing|laugh|laughter|giggle|static)\s*(?:>|\])"
)
_SHORT_ACTIONABLE_UTTERANCES = {
    "yes",
    "no",
    "ok",
    "okay",
    "hi",
    "hey",
    "wow",
    "look",
    "stop",
    "again",
    "more",
    "help",
    "toy",
    "pic",
    "photo",
    "camera",
    "scene",
    "page",
    "rewind",
    "story",
    "restart",
    "end",
    "mic",
    "on",
    "off",
}
_COMMON_ENGLISH_HINTS = {
    "i",
    "im",
    "i'm",
    "my",
    "me",
    "we",
    "you",
    "your",
    "can",
    "could",
    "may",
    "please",
    "share",
    "show",
    "with",
    "toy",
    "picture",
    "photo",
    "camera",
    "scene",
    "page",
    "story",
    "adventure",
    "restart",
    "start",
    "again",
    "rewind",
    "end",
    "movie",
    "mic",
    "microphone",
    "turn",
    "mute",
    "unmute",
    "stop",
    "back",
}
_SCENE_NUMBER_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
    "eleventh": 11,
    "twelfth": 12,
}
_SCENE_BRANCH_QUERY_STOPWORDS = {
    "a",
    "an",
    "and",
    "back",
    "can",
    "could",
    "go",
    "happened",
    "i",
    "it",
    "jump",
    "let",
    "lets",
    "me",
    "my",
    "page",
    "part",
    "please",
    "scene",
    "take",
    "that",
    "the",
    "there",
    "this",
    "to",
    "us",
    "want",
    "we",
    "where",
}


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
    cleaned = _normalize_transcript_text(text)
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


def _normalize_transcript_text(text: str | None) -> str:
    if not text:
        return ""
    cleaned = _CTRL_TOKEN_RE.sub("", text)
    cleaned = _TRANSCRIPTION_META_TOKEN_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _canonicalize_finished_child_transcript(text: str | None) -> str:
    cleaned = _normalize_transcript_text(text).lower()
    if not cleaned:
        return ""
    return re.sub(r"[^\w\s']", "", cleaned).strip()


def _is_duplicate_finished_child_transcript(session_id: str, text: str | None) -> bool:
    canonical = _canonicalize_finished_child_transcript(text)
    if not canonical:
        return False
    now = time.monotonic()
    previous = _recent_finished_child_transcripts.get(session_id)
    _recent_finished_child_transcripts[session_id] = (canonical, now)
    if not previous:
        return False
    previous_text, previous_ts = previous
    if canonical != previous_text:
        return False
    return (now - previous_ts) <= 3.0


def _merge_streaming_transcript(previous: str | None, incoming_raw: str) -> str:
    incoming = (incoming_raw or "").strip()
    if not incoming:
        return previous.strip() if isinstance(previous, str) else ""

    prev = previous.strip() if isinstance(previous, str) else ""
    if not prev:
        return incoming

    if incoming.startswith(prev):
        return incoming
    if prev.startswith(incoming) or prev.endswith(incoming):
        return prev
    if len(incoming) >= 12 and incoming in prev:
        return prev

    max_overlap = min(len(prev), len(incoming))
    overlap = 0
    for i in range(max_overlap, 0, -1):
        if prev[-i:].lower() == incoming[:i].lower():
            overlap = i
            break

    merged = (
        f"{prev}{incoming[overlap:]}"
        if overlap > 0
        else (f"{prev}{incoming}" if re.search(r"""[([{'"-]$""", prev) or re.search(r"""^[,.;:!?)}\]'"]""", incoming) else f"{prev} {incoming}")
    )

    if len(merged) % 2 == 0:
        half = merged[: len(merged) // 2]
        if half and f"{half}{half}" == merged:
            return half.strip()
    return merged.strip()


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


def _normalize_child_age(raw: Any) -> int:
    return clamp_child_age(raw)


def _normalize_storybook_movie_pacing(raw: Any) -> str:
    return normalize_storybook_movie_pacing(raw)


def _normalize_storybook_elevenlabs_voice_id(raw: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip())
    if 20 <= len(cleaned) <= 64:
        return cleaned
    return _DEFAULT_STORYBOOK_ELEVENLABS_VOICE_ID


def _payload_storybook_elevenlabs_voice_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    raw = payload.get("storybook_elevenlabs_voice_id")
    if raw is None:
        raw = payload.get("storybookElevenlabsVoiceId")
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip())
    if 20 <= len(cleaned) <= 64:
        return cleaned
    return None


async def _persist_storybook_elevenlabs_voice_id(
    *,
    runner: Runner,
    user_id: str,
    session_id: str,
    voice_id: str | None,
) -> None:
    normalized = _payload_storybook_elevenlabs_voice_id(
        {"storybook_elevenlabs_voice_id": voice_id}
    ) if voice_id else None
    if not normalized:
        return

    await _mutate_state(
        runner=runner,
        user_id=user_id,
        session_id=session_id,
        mutator=lambda state: state.__setitem__("storybook_elevenlabs_voice_id", normalized),
    )
    _update_storybook_firestore(
        session_id,
        {
            "storybook_elevenlabs_voice_id": normalized,
        },
    )


def _is_meta_only_transcription(text: str | None) -> bool:
    if not text:
        return False
    raw = _CTRL_TOKEN_RE.sub("", text).strip()
    if not raw:
        return False
    return _normalize_transcript_text(raw) == ""


def _speech_tokens(text: str | None) -> list[str]:
    cleaned = _normalize_transcript_text(text).lower()
    if not cleaned:
        return []
    return [token for token in _SPEECH_WORD_RE.findall(cleaned) if token]


def _looks_like_child_speech(text: str | None) -> bool:
    cleaned = _normalize_transcript_text(text)
    if not cleaned:
        return False
    lowered = cleaned.lower()

    if (
        _NAME_PHRASE_RE.search(cleaned)
        or _TOY_SHARE_REQUEST_RE.search(cleaned)
        or _TOY_SHARE_CLOSE_RE.search(cleaned)
        or _SCENE_BRANCH_REQUEST_RE.search(cleaned)
        or _RESTART_STORY_RE.search(cleaned)
        or _END_STORY_RE.search(cleaned)
        or _MIC_OFF_RE.search(cleaned)
        or _MIC_ON_RE.search(cleaned)
    ):
        return True

    tokens = _speech_tokens(cleaned)
    if not tokens:
        return False

    if len(tokens) == 1:
        token = tokens[0]
        if token in _SHORT_ACTIONABLE_UTTERANCES:
            return True
        if len(token) >= 3 and re.search(r"[aeiouy]", token) and not re.fullmatch(r"(?:[bcdfghjklmnpqrstvwxyz]){4,}", token):
            return True
        return False

    english_hits = sum(1 for token in tokens if token in _COMMON_ENGLISH_HINTS)
    vowelish_tokens = sum(1 for token in tokens if re.search(r"[aeiouy]", token))
    if english_hits >= 1 and vowelish_tokens >= 1:
        return True
    return vowelish_tokens >= 2


def _allows_freeform_single_word_child_turn(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    return (
        not bool(state.get("story_started", False))
        or not bool(state.get("name_confirmed", False))
    )


def _is_low_signal_single_word_child_utterance(
    text: str | None,
    state: dict[str, Any] | None,
) -> bool:
    tokens = _speech_tokens(text)
    if len(tokens) != 1:
        return False
    token = tokens[0].lower()
    if token in _SHORT_ACTIONABLE_UTTERANCES:
        return False
    return not _allows_freeform_single_word_child_turn(state)


def _is_actionable_child_text(
    text: str | None,
    state: dict[str, Any] | None = None,
) -> bool:
    if not _is_meaningful_text(text):
        return False
    if not _looks_like_child_speech(text):
        return False
    if _is_low_signal_single_word_child_utterance(text, state):
        return False
    return True


def _partial_child_utterance_is_resumable(text: str | None) -> bool:
    cleaned = _normalize_transcript_text(text)
    if not cleaned:
        return False
    tokens = _speech_tokens(cleaned)
    if len(tokens) >= 4:
        return True
    return len(tokens) >= 3 and len(cleaned) >= 18


def _scene_branch_points(state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("scene_branch_points", [])
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            cleaned.append(item)
    return cleaned


def _scene_branch_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(state)
    snapshot.pop("state_snapshots", None)
    snapshot.pop("scene_branch_points", None)
    snapshot["pending_scene_branch_number"] = 0
    snapshot["pending_scene_branch_label"] = ""
    snapshot["pending_response"] = False
    snapshot["pending_scene_replacement_phase"] = ""
    return snapshot


def _scene_branch_label(point: dict[str, Any]) -> str:
    for key in ("storybeat_text", "scene_description"):
        value = str(point.get(key, "") or "").strip()
        if value:
            return value[:140]
    scene_number = int(point.get("scene_number", 0) or 0)
    if scene_number > 0:
        return f"Scene {scene_number}"
    return "Earlier story scene"


def _scene_branch_public_payload(
    state: dict[str, Any],
    *,
    selected_scene_number: int | None = None,
) -> list[dict[str, Any]]:
    current_scene_number = 0
    points = _scene_branch_points(state)
    if points:
        current_scene_number = int(points[-1].get("scene_number", 0) or 0)
    payload: list[dict[str, Any]] = []
    for point in points:
        scene_number = int(point.get("scene_number", 0) or 0)
        payload.append(
            {
                "scene_number": scene_number,
                "label": _scene_branch_label(point),
                "scene_description": str(point.get("scene_description", "") or "").strip(),
                "storybeat_text": str(point.get("storybeat_text", "") or "").strip(),
                "image_url": str(point.get("image_url", "") or "").strip() or None,
                "is_current": scene_number == current_scene_number,
                "is_selected": scene_number == int(selected_scene_number or 0),
            }
        )
    return payload


def _story_pages_payload(state: dict[str, Any]) -> list[dict[str, Any]]:
    return story_pages_from_state_data(
        {
            "scene_branch_points": _scene_branch_points(state),
            "story_pages": state.get("story_pages", []),
        }
    )


def _storybook_scene_state_payload(state: dict[str, Any]) -> dict[str, Any]:
    public_points = _scene_branch_public_payload(state)
    return {
        "story_pages": _story_pages_payload(state),
        "scene_branch_points": public_points,
        "scene_branch_points_public": public_points,
        "scene_asset_urls": list(state.get("scene_asset_urls", []) or []),
        "scene_asset_gcs_uris": list(state.get("scene_asset_gcs_uris", []) or []),
        "scene_descriptions": list(state.get("scene_descriptions", []) or []),
        "scene_storybeat_texts": list(state.get("scene_storybeat_texts", []) or []),
        "current_scene_description": str(state.get("current_scene_description", "") or "").strip(),
        "current_scene_storybeat_text": str(state.get("current_scene_storybeat_text", "") or "").strip(),
        "story_summary": str(state.get("story_summary", "") or "").strip(),
        "story_phase": str(state.get("story_phase", "") or "").strip(),
    }


def _clear_pending_scene_request_metadata(state: dict[str, Any]) -> None:
    state["pending_scene_description"] = ""
    state["pending_scene_base_description"] = ""
    state["pending_scene_replacement_text"] = ""
    state["pending_scene_replacement_phase"] = ""
    state["pending_scene_replacement_armed_at_epoch_ms"] = 0
    state["scene_render_skipped"] = False


def _prime_pending_scene_request(
    state: dict[str, Any],
    *,
    request_id: str | None,
    description: str = "",
    base_description: str = "",
) -> None:
    normalized_request_id = str(request_id or "").strip()
    normalized_description = str(description or "").strip()
    normalized_base_description = str(base_description or "").strip()
    if normalized_request_id:
        state["active_scene_request_id"] = normalized_request_id
    if normalized_description:
        state["pending_scene_description"] = normalized_description
        state["pending_scene_base_description"] = normalized_base_description or normalized_description
    state["pending_scene_replacement_text"] = ""
    state["pending_scene_replacement_phase"] = ""
    state["pending_scene_replacement_armed_at_epoch_ms"] = 0


def _scene_request_matches_active_request(
    state: dict[str, Any],
    *,
    request_id: str | None,
) -> bool:
    normalized_request_id = str(request_id or "").strip()
    active_request_id = str(state.get("active_scene_request_id", "") or "").strip()
    return (
        not normalized_request_id
        or not active_request_id
        or normalized_request_id == active_request_id
    )


def _promote_pending_scene_request_to_current(
    state: dict[str, Any],
    *,
    request_id: str | None,
    description: str = "",
    storybeat_text: str = "",
) -> bool:
    if not _scene_request_matches_active_request(state, request_id=request_id):
        return False

    if description:
        state["current_scene_description"] = description
        if not str(state.get("canonical_scene_description", "") or "").strip():
            state["canonical_scene_description"] = description

    pending_base_description = str(state.get("pending_scene_base_description", "") or "").strip()
    if pending_base_description:
        state["current_scene_base_description"] = pending_base_description
    elif description and not str(state.get("current_scene_base_description", "") or "").strip():
        state["current_scene_base_description"] = description

    if storybeat_text:
        state["current_scene_storybeat_text"] = storybeat_text
        if not str(state.get("canonical_scene_storybeat_text", "") or "").strip():
            state["canonical_scene_storybeat_text"] = storybeat_text

    _clear_pending_scene_request_metadata(state)
    return True


def _apply_nonpersistent_scene_ready_to_state(
    state: dict[str, Any],
    *,
    request_id: str | None,
    looks_like_image: bool,
    is_fallback: bool,
    description: str = "",
    storybeat_text: str = "",
) -> None:
    if _should_ignore_nonpersistent_scene_ready(state, request_id=request_id):
        state["scene_render_pending"] = bool(
            str(state.get("pending_scene_description", "") or "").strip()
            or str(state.get("pending_scene_base_description", "") or "").strip()
            or (
                str(state.get("pending_scene_replacement_text", "") or "").strip()
                and _pending_scene_replacement_phase(state)
                in {
                    _PENDING_SCENE_REPLACEMENT_PHASE_ACK,
                    _PENDING_SCENE_REPLACEMENT_PHASE_RENDER,
                }
            )
        )
        return
    if looks_like_image:
        if not _scene_request_matches_active_request(state, request_id=request_id):
            state["scene_render_pending"] = bool(
                str(state.get("pending_scene_description", "") or "").strip()
                or str(state.get("pending_scene_base_description", "") or "").strip()
            )
            return
        if is_fallback:
            _clear_pending_scene_request_metadata(state)
        else:
            # Preview stills arrive before the durable cloud asset is uploaded.
            # Record the next page immediately so the frontend can advance page
            # history and captions in lockstep with the first visible preview.
            _apply_scene_asset_to_story_state(
                state,
                request_id=request_id,
                image_url="",
                description=description,
                storybeat_text=storybeat_text,
                gcs_uri="",
            )
    else:
        _clear_pending_scene_request_metadata(state)
    state["scene_render_pending"] = False


def _apply_scene_asset_to_story_state(
    state: dict[str, Any],
    *,
    request_id: str | None,
    image_url: str,
    description: str = "",
    storybeat_text: str = "",
    gcs_uri: str = "",
) -> None:
    ensure_story_continuity_state(state)
    normalized_request_id = str(request_id or "").strip()
    pages = _story_pages_payload(state)
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
                "scene_number": len(pages) + 1,
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
        target_index = 0
        pages.append(
            {
                "scene_number": 1,
                "request_id": normalized_request_id,
                "scene_description": "",
                "storybeat_text": "",
                "image_url": "",
                "gcs_uri": "",
            }
        )

    target_page = dict(pages[target_index])
    target_page["scene_number"] = max(1, int(target_page.get("scene_number", target_index + 1) or (target_index + 1)))
    if normalized_request_id:
        target_page["request_id"] = normalized_request_id
    if description:
        target_page["scene_description"] = description
    if storybeat_text:
        target_page["storybeat_text"] = storybeat_text
    if image_url:
        target_page["image_url"] = image_url
    if gcs_uri:
        target_page["gcs_uri"] = gcs_uri
    pages[target_index] = target_page
    state["story_pages"] = pages[-40:]

    points = _scene_branch_points(state)
    branch_target_index = -1
    if normalized_request_id:
        for idx, point in enumerate(points):
            if str(point.get("request_id", "") or "").strip() == normalized_request_id:
                branch_target_index = idx
                break
    if branch_target_index < 0 and normalized_request_id:
        scene_number = max(1, int(target_page.get("scene_number", target_index + 1) or (target_index + 1)))
        points.append(
            {
                "scene_number": scene_number,
                "request_id": normalized_request_id,
                "label": storybeat_text or description or f"Scene {scene_number}",
                "scene_description": description,
                "storybeat_text": storybeat_text,
                "image_url": image_url,
                "gcs_uri": gcs_uri,
            }
        )
        branch_target_index = len(points) - 1
    elif branch_target_index < 0 and points:
        branch_target_index = min(target_index, len(points) - 1)
    if branch_target_index >= 0:
        branch_point = dict(points[branch_target_index])
        if normalized_request_id:
            branch_point["request_id"] = normalized_request_id
        if description:
            branch_point["scene_description"] = description
        if storybeat_text:
            branch_point["storybeat_text"] = storybeat_text
            branch_point["label"] = storybeat_text
        elif description and not str(branch_point.get("label", "") or "").strip():
            branch_point["label"] = description
        if image_url:
            branch_point["image_url"] = image_url
        if gcs_uri:
            branch_point["gcs_uri"] = gcs_uri
        points[branch_target_index] = branch_point
        state["scene_branch_points"] = points[-20:]

    page_image_urls = [
        str(page.get("image_url", "") or "").strip()
        for page in state["story_pages"]
        if str(page.get("image_url", "") or "").strip()
    ]
    page_gcs_uris = [
        str(page.get("gcs_uri", "") or "").strip()
        for page in state["story_pages"]
        if str(page.get("gcs_uri", "") or "").strip()
    ]
    page_descriptions = [
        str(page.get("scene_description", "") or "").strip()
        for page in state["story_pages"]
        if str(page.get("scene_description", "") or "").strip()
    ]
    page_storybeats = [
        str(page.get("storybeat_text", "") or "").strip()
        for page in state["story_pages"]
        if str(page.get("storybeat_text", "") or "").strip()
    ]
    state["scene_asset_urls"] = page_image_urls[-40:]
    state["scene_asset_gcs_uris"] = page_gcs_uris[-40:]
    state["scene_descriptions"] = page_descriptions[-40:]
    state["scene_storybeat_texts"] = page_storybeats[-40:]

    is_current_scene_request = _promote_pending_scene_request_to_current(
        state,
        request_id=normalized_request_id,
        description=description,
        storybeat_text=storybeat_text,
    )
    if description and is_current_scene_request and not str(state.get("canonical_scene_description", "") or "").strip():
        state["canonical_scene_description"] = description
    if storybeat_text and is_current_scene_request and not str(state.get("canonical_scene_storybeat_text", "") or "").strip():
        state["canonical_scene_storybeat_text"] = storybeat_text
    _sync_story_page_progress_fields(state)
    record_continuity_scene(
        state,
        description=description,
        storybeat_text=storybeat_text,
        request_id=normalized_request_id,
        scene_number=max(1, int(target_page.get("scene_number", target_index + 1) or (target_index + 1))),
    )


def _parse_scene_number_token(raw: str) -> int | None:
    candidate = str(raw or "").strip().lower()
    if not candidate:
        return None
    if candidate in _SCENE_NUMBER_WORDS:
        return _SCENE_NUMBER_WORDS[candidate]
    candidate = re.sub(r"(st|nd|rd|th)$", "", candidate)
    if candidate.isdigit():
        try:
            value = int(candidate)
        except Exception:
            return None
        return value if value > 0 else None
    return None


def _extract_scene_number_from_text(text: str | None) -> int | None:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip().lower()
    if not cleaned:
        return None
    match = _SCENE_NUMBER_REF_RE.search(cleaned)
    if not match:
        return None
    return _parse_scene_number_token(match.group(1) or match.group(2) or "")


def _scene_branch_query_tokens(text: str | None) -> list[str]:
    tokens = _speech_tokens(text)
    return [
        token
        for token in tokens
        if token not in _SCENE_BRANCH_QUERY_STOPWORDS and len(token) >= 3
    ]


def _score_scene_branch_point(point: dict[str, Any], query_tokens: list[str]) -> int:
    if not query_tokens:
        return 0
    haystack = " ".join(
        [
            str(point.get("scene_description", "") or ""),
            str(point.get("storybeat_text", "") or ""),
            str(point.get("label", "") or ""),
        ]
    ).lower()
    if not haystack:
        return 0
    score = 0
    for token in query_tokens:
        if re.search(rf"\b{re.escape(token)}\b", haystack):
            score += 2
        elif token in haystack:
            score += 1
    return score


def _resolve_scene_branch_target(text: str | None, state: dict[str, Any]) -> dict[str, Any] | None:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip()
    if not cleaned:
        return None
    points = _scene_branch_points(state)
    if not points:
        return None

    explicit_number = _extract_scene_number_from_text(cleaned)
    if explicit_number is not None:
        for point in points:
            if int(point.get("scene_number", 0) or 0) == explicit_number:
                return point
        return None

    query_tokens = _scene_branch_query_tokens(cleaned)
    best_point: dict[str, Any] | None = None
    best_score = 0
    for point in points:
        score = _score_scene_branch_point(point, query_tokens)
        if score > best_score:
            best_score = score
            best_point = point
    if best_point and best_score >= 2:
        return best_point

    if _SCENE_BRANCH_REQUEST_RE.search(cleaned) and len(points) >= 2:
        return points[-2]
    return None


def _detect_voice_ui_intent(text: str | None, state: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip()
    if not cleaned:
        return None, {}

    # Scene rewind is intentionally button-only. Do not open or confirm the
    # rewind picker from voice, even if the transcript happens to match.

    if bool(state.get("toy_share_active", False)) and _TOY_SHARE_CLOSE_RE.search(cleaned):
        return "close_toy_share", {}
    if _TOY_SHARE_REQUEST_RE.search(cleaned):
        return "open_toy_share", {}
    if _RESTART_STORY_RE.search(cleaned):
        return "restart_story", {}
    if bool(state.get("story_started", False)) and _END_STORY_RE.search(cleaned):
        return "end_story", {}
    if _MIC_OFF_RE.search(cleaned):
        return "set_mic_enabled", {"enabled": False}
    if _MIC_ON_RE.search(cleaned):
        return "set_mic_enabled", {"enabled": True}
    return None, {}


def _should_resume_story_from_toy_share(text: str | None, state: dict[str, Any]) -> bool:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip()
    if not cleaned or not bool(state.get("toy_share_active", False)):
        return False
    if _TOY_SHARE_REQUEST_RE.search(cleaned) or _TOY_SHARE_CLOSE_RE.search(cleaned):
        return False
    if _child_requested_scene_refresh(cleaned):
        return True
    if _is_navigation_page_turn_request(cleaned):
        return True
    if _is_explicit_visual_request(cleaned):
        return True
    return False


_ASSISTANT_TURN_COMPLETE_RE = re.compile(
    r"(?:\?\s*$|\b(?:should we|do you want|which one|what should we do|what do you want to do|where should we go)\b.*\bor\b.*[.?!]*$)",
    flags=re.IGNORECASE,
)


def _assistant_turn_soft_closes(text: str | None) -> bool:
    cleaned = _CTRL_TOKEN_RE.sub("", text or "").strip()
    if not cleaned:
        return False
    return bool(_ASSISTANT_TURN_COMPLETE_RE.search(cleaned))


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
    )
    if any(marker in reason for marker in retryable_markers):
        return True
    if any(marker in message for marker in retryable_markers):
        return True
    return False


def _is_invalid_argument_live_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 1007:
        return True

    reason = str(getattr(exc, "reason", "")).lower()
    message = str(exc).lower()
    markers = (
        "1007",
        "invalid argument",
        "invalid frame payload data",
        "request contains an invalid argument",
    )
    return any(marker in reason for marker in markers) or any(marker in message for marker in markers)


class _LiveTurnRecoveryRequested(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _is_capability_live_error(exc: Exception) -> bool:
    """Returns True when the service rejected an unsupported live capability/config."""
    status_code = getattr(exc, "status_code", None)
    if status_code == 1008:
        return True

    reason = str(getattr(exc, "reason", "")).lower()
    message = str(exc).lower()
    markers = (
        "1008",
        "operation is not implemented",
        "not supported",
        "not enabled",
        "policy violation",
    )
    return any(marker in reason for marker in markers) or any(marker in message for marker in markers)


def _is_clean_live_close(exc: Exception) -> bool:
    """Returns True for a normal WebSocket close that should not be treated as an error."""
    status_code = getattr(exc, "status_code", None)
    if status_code == 1000:
        return True

    reason = str(getattr(exc, "reason", "")).lower()
    message = str(exc).lower()
    markers = (
        "1000",
        "connectionclosedok",
        "sent 1000 (ok)",
        "received 1000 (ok)",
    )
    return any(marker in reason for marker in markers) or any(marker in message for marker in markers)


def _should_attempt_clean_live_resume(
    session_id: str,
    state: dict[str, Any] | None,
    *,
    meaningful_pending_turn: bool,
) -> bool:
    """Returns True when a clean Live close should transparently restart the stream."""
    if session_id in _awaiting_greeting_sessions:
        return True
    if session_id in _opening_phase_sessions and _opening_phase_needs_first_greeting(state):
        return True
    if session_id in _ending_story_sessions:
        return True
    if meaningful_pending_turn:
        return True
    if not isinstance(state, dict):
        return False
    return bool(
        state.get("pending_response")
        or state.get("pending_response_interrupted")
        or state.get("scene_tool_turn_open")
    )


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
        matched_prefix = match.group(0).strip()
        trailing_text = cleaned[match.end():].lstrip()
        if _SELF_INTRO_NAME_PREFIX_RE.match(matched_prefix):
            if trailing_text and not _SELF_INTRO_NAME_TRAILING_OK_RE.match(trailing_text):
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
        turn_number = int(state.get("response_turn_number", state.get("turn_number", 1)))
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
                    "We reached the last magic page, so now it is movie time. "
                    "After this one, we can start a brand-new adventure."
                )
            },
        ).model_dump_json()
    )


def _publish_ui_command(session_id: str, action: str, **payload: Any) -> None:
    if not session_id or not action:
        return
    publish_session_event(
        session_id,
        ServerEvent(
            type=ServerEventType.UI_COMMAND,
            payload={"action": action, **payload},
        ).model_dump(),
    )


def _get_storage_session(runner: Runner, user_id: str, session_id: str) -> Any | None:
    try:
        return runner.session_service.sessions["storyteller"][user_id][session_id]  # type: ignore[attr-defined]
    except Exception:
        return None


def _clear_pending_scene_branch(state: dict[str, Any]) -> None:
    state["pending_scene_branch_number"] = 0
    state["pending_scene_branch_label"] = ""


def _record_scene_branch_point(
    state: dict[str, Any],
    *,
    snapshot_state: dict[str, Any],
    session_event_count: int,
) -> None:
    request_id = str(snapshot_state.get("active_scene_request_id", "") or "").strip()
    points = _scene_branch_points(state)
    existing_index = -1
    if request_id:
        for idx, point in enumerate(points):
            if str(point.get("request_id", "") or "").strip() == request_id:
                existing_index = idx
                break

    matching_page: dict[str, Any] | None = None
    if request_id:
        for page in _story_pages_payload(snapshot_state):
            if str(page.get("request_id", "") or "").strip() == request_id:
                matching_page = dict(page)
                break

    if existing_index >= 0:
        scene_number = max(1, int(points[existing_index].get("scene_number", existing_index + 1) or (existing_index + 1)))
    elif matching_page is not None:
        scene_number = max(1, int(matching_page.get("scene_number", len(points) + 1) or (len(points) + 1)))
    elif points:
        scene_number = max(max(int(point.get("scene_number", 0) or 0) for point in points) + 1, 1)
    else:
        scene_number = 1
    scene_description = str(
        snapshot_state.get("pending_scene_description")
        or snapshot_state.get("current_scene_description")
        or ""
    ).strip()
    storybeat_text = str(snapshot_state.get("current_scene_storybeat_text", "") or "").strip()
    image_url = str((matching_page or {}).get("image_url", "") or "").strip()
    gcs_uri = str((matching_page or {}).get("gcs_uri", "") or "").strip()
    point = {
        "scene_number": scene_number,
        "request_id": request_id,
        "label": storybeat_text or scene_description or f"Scene {scene_number}",
        "scene_description": scene_description,
        "storybeat_text": storybeat_text,
        "image_url": image_url,
        "gcs_uri": gcs_uri,
        "session_event_count": max(0, int(session_event_count or 0)),
        "state_snapshot": _scene_branch_state_snapshot(snapshot_state),
    }
    if existing_index >= 0:
        merged_point = dict(points[existing_index])
        merged_point.update(point)
        points[existing_index] = merged_point
    else:
        points.append(point)
    state["scene_branch_points"] = points[-20:]
    state["story_pages"] = _story_pages_payload(state)
    _clear_pending_scene_branch(state)


def _sync_latest_scene_branch_point_from_state(state: dict[str, Any]) -> None:
    points = _scene_branch_points(state)
    if not points:
        return
    active_request_id = str(state.get("active_scene_request_id", "") or "").strip()
    target_index = len(points) - 1
    matching_page: dict[str, Any] | None = None
    if active_request_id:
        for idx, point in enumerate(points):
            if str(point.get("request_id", "") or "").strip() == active_request_id:
                target_index = idx
                break
        for page in _story_pages_payload(state):
            if str(page.get("request_id", "") or "").strip() == active_request_id:
                matching_page = dict(page)
                break
    latest = dict(points[target_index])
    scene_description = str(state.get("current_scene_description", "") or "").strip()
    storybeat_text = str(state.get("current_scene_storybeat_text", "") or "").strip()
    image_url = str((matching_page or {}).get("image_url", "") or "").strip()
    gcs_uri = str((matching_page or {}).get("gcs_uri", "") or "").strip()
    if scene_description:
        latest["scene_description"] = scene_description
    if storybeat_text:
        latest["storybeat_text"] = storybeat_text
        latest["label"] = storybeat_text
    elif scene_description:
        latest["label"] = scene_description
    if image_url:
        latest["image_url"] = image_url
    if gcs_uri:
        latest["gcs_uri"] = gcs_uri
    points[target_index] = latest
    state["scene_branch_points"] = points
    state["story_pages"] = _story_pages_payload(state)


def _restore_branch_scene_lists(state: dict[str, Any], points: list[dict[str, Any]]) -> None:
    scene_urls = [str(point.get("image_url", "") or "").strip() for point in points if str(point.get("image_url", "") or "").strip()]
    scene_descriptions = [str(point.get("scene_description", "") or "").strip() for point in points if str(point.get("scene_description", "") or "").strip()]
    storybeats = [str(point.get("storybeat_text", "") or "").strip() for point in points if str(point.get("storybeat_text", "") or "").strip()]
    state["scene_asset_urls"] = scene_urls[-40:]
    state["scene_descriptions"] = scene_descriptions[-40:]
    state["scene_storybeat_texts"] = storybeats[-40:]
    if storybeats:
        state["current_scene_storybeat_text"] = storybeats[-1]
    elif not str(state.get("current_scene_storybeat_text", "") or "").strip():
        state["current_scene_storybeat_text"] = ""
    if scene_descriptions:
        state["current_scene_description"] = scene_descriptions[-1]
        state["current_scene_base_description"] = scene_descriptions[-1]


def _prepare_branch_state(
    target_point: dict[str, Any],
    kept_points: list[dict[str, Any]],
) -> dict[str, Any]:
    restored = copy.deepcopy(target_point.get("state_snapshot", {}) or {})
    restored["story_started"] = True
    restored["toy_share_active"] = False
    restored["toy_share_turns_remaining"] = 0
    restored["pending_response"] = False
    restored["story_turn_limit_reached"] = False
    restored["camera_stage"] = str(restored.get("camera_stage", "done") or "done")
    restored["scene_render_pending"] = False
    restored["assembly_kind"] = "initial"
    restored["theater_release_ready"] = False
    restored["active_scene_request_id"] = str(target_point.get("request_id", "") or "").strip()
    restored["pending_scene_description"] = ""
    restored["pending_scene_base_description"] = ""
    restored["scene_branch_points"] = copy.deepcopy(kept_points)
    restored["story_pages"] = story_pages_from_state_data({"scene_branch_points": kept_points})
    restored["state_snapshots"] = []
    _clear_pending_scene_branch(restored)
    _restore_branch_scene_lists(restored, kept_points)
    return restored


def _reset_storybook_progress_after_branch(session_id: str, state: dict[str, Any]) -> None:
    state.pop("assembly_status", None)
    state.pop("assembly_kind", None)
    state.pop("final_video_url", None)
    state.pop("final_video_gcs_uri", None)
    state.pop("trading_card_url", None)
    state.pop("narration_lines", None)
    state.pop("theater_lighting_cues", None)
    state.pop("audio_available", None)
    state.pop("final_has_audio_stream", None)
    state.pop("final_video_duration_sec", None)
    state.pop("audio_expected", None)
    state.pop("expected_narration_count", None)
    state.pop("rendered_narration_count", None)
    state.pop("final_scene_count", None)
    state.pop("final_shot_types", None)
    state.pop("storyboard_review", None)
    state.pop("storybook_studio", None)
    state.pop("assembly_started_at_epoch_ms", None)
    state["assembly_recent_activities"] = []
    state["assembly_wait_prompt_count"] = 0
    state["scene_render_pending"] = False
    _clear_pending_scene_request_metadata(state)
    state["theater_release_ready"] = False
    _sync_story_phase(session_id, state)
    state.pop("movie_feedback_latest", None)
    state.pop("post_movie_meta_review", None)
    cache_storybook_state(
        session_id,
        dict(state),
    )
    _update_storybook_firestore(
        session_id,
        {
            "assembly_status": "",
            "assembly_kind": "",
            "final_video_url": "",
            "final_video_gcs_uri": "",
            "trading_card_url": "",
            "narration_lines": [],
            "theater_lighting_cues": [],
            "audio_expected": None,
            "audio_available": None,
            "expected_narration_count": None,
            "rendered_narration_count": None,
            "final_has_audio_stream": None,
            "final_video_duration_sec": None,
            "final_scene_count": None,
            "final_shot_types": [],
            "storyboard_review": None,
            "storybook_studio": None,
            "assembly_started_at_epoch_ms": None,
            "assembly_recent_activities": [],
            "assembly_wait_prompt_count": 0,
            "theater_release_ready": False,
            "movie_feedback_latest": None,
            "post_movie_meta_review": None,
            **_storybook_scene_state_payload(state),
        },
    )


async def _branch_story_to_scene(
    *,
    runner: Runner,
    websocket: WebSocket,
    session_id: str,
    user_id: str,
    scene_number: int,
    source: str,
) -> bool:
    storage_session = _get_storage_session(runner, user_id, session_id)
    if storage_session is None:
        return False
    points = _scene_branch_points(storage_session.state)
    target_point: dict[str, Any] | None = None
    for point in points:
        if int(point.get("scene_number", 0) or 0) == scene_number:
            target_point = point
            break
    if target_point is None:
        return False

    kept_points = [copy.deepcopy(point) for point in points if int(point.get("scene_number", 0) or 0) <= scene_number]
    restored_state = _prepare_branch_state(target_point, kept_points)
    target_event_count = max(0, int(target_point.get("session_event_count", 0) or 0))

    storage_session.state.clear()
    storage_session.state.update(restored_state)
    if target_event_count > 0:
        storage_session.events = list(storage_session.events[:target_event_count])
    storage_session.last_update_time = time.time()

    _story_turn_limit_sessions.discard(session_id)
    _story_turn_limit_notified_sessions.discard(session_id)
    _ending_story_sessions.discard(session_id)
    _video_generation_started_sessions.discard(session_id)
    _watching_final_video_sessions.discard(session_id)
    _final_video_watch_not_before_epoch.pop(session_id, None)
    if storage_session.state.get("name_confirmed") or int(storage_session.state.get("response_turn_number", 1)) >= 3:
        _opening_phase_sessions.discard(session_id)
    else:
        _opening_phase_sessions.add(session_id)

    _reset_storybook_progress_after_branch(session_id, storage_session.state)

    target_storybeat_text = (
        str(target_point.get("storybeat_text", "") or "").strip()
        or str(storage_session.state.get("current_scene_storybeat_text", "") or "").strip()
    )
    target_scene_description = (
        str(target_point.get("scene_description", "") or "").strip()
        or str(storage_session.state.get("current_scene_description", "") or "").strip()
    )
    target_image_url = str(target_point.get("image_url", "") or "").strip()
    if not target_image_url:
        story_pages = _story_pages_payload(storage_session.state)
        if story_pages:
            target_image_url = str(story_pages[-1].get("image_url", "") or "").strip()
    if not target_image_url:
        scene_asset_urls = list(storage_session.state.get("scene_asset_urls", []) or [])
        if scene_asset_urls:
            target_image_url = str(scene_asset_urls[-1] or "").strip()
    if target_image_url:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "video_ready",
                    "payload": {
                        "url": target_image_url,
                        "media_type": "image",
                        "storybeat_text": target_storybeat_text,
                        "scene_history": _scene_branch_public_payload(storage_session.state, selected_scene_number=scene_number),
                    },
                }
            )
        )

    await websocket.send_text(
        ServerEvent(
            type=ServerEventType.REWIND_COMPLETE,
            payload={
                "scene_number": scene_number,
                "scene_history": _scene_branch_public_payload(storage_session.state, selected_scene_number=scene_number),
                "current_scene_image_url": target_image_url or None,
                "current_scene_storybeat_text": target_storybeat_text,
                "current_scene_description": target_scene_description,
                "source": source,
            },
        ).model_dump_json()
    )
    return True


async def _request_scene_branch_confirmation(
    *,
    runner: Runner,
    session_id: str,
    user_id: str,
    scene_number: int,
) -> dict[str, Any] | None:
    storage_session = _get_storage_session(runner, user_id, session_id)
    if storage_session is None:
        return None
    points = _scene_branch_points(storage_session.state)
    target_point: dict[str, Any] | None = None
    for point in points:
        if int(point.get("scene_number", 0) or 0) == scene_number:
            target_point = point
            break
    if target_point is None:
        return None
    storage_session.state["pending_scene_branch_number"] = scene_number
    storage_session.state["pending_scene_branch_label"] = _scene_branch_label(target_point)
    return {
        "scene_number": scene_number,
        "label": _scene_branch_label(target_point),
        "scene_history": _scene_branch_public_payload(storage_session.state, selected_scene_number=scene_number),
    }


def _capture_toy_share_resume_state(state: dict[str, Any]) -> None:
    state["toy_share_resume_story_summary"] = str(state.get("story_summary", "") or "").strip()
    state["toy_share_resume_scene_description"] = str(state.get("current_scene_description", "") or "").strip()
    state["toy_share_resume_storybeat_text"] = str(state.get("current_scene_storybeat_text", "") or "").strip()


def _clear_toy_share_resume_state(state: dict[str, Any]) -> None:
    state["toy_share_resume_story_summary"] = ""
    state["toy_share_resume_scene_description"] = ""
    state["toy_share_resume_storybeat_text"] = ""


def _upsert_character_fact(state: dict[str, Any], *, character_name: str, fact: str) -> None:
    name = str(character_name or "").strip()
    cleaned_fact = str(fact or "").strip()
    if not name or not cleaned_fact:
        return
    facts = list(state.get("character_facts_list", []) or [])
    normalized_name = name.lower()
    updated = False
    for entry in facts:
        if not isinstance(entry, dict):
            continue
        existing_name = str(entry.get("character_name", "") or "").strip().lower()
        if existing_name != normalized_name:
            continue
        entry["character_name"] = name
        entry["fact"] = cleaned_fact
        updated = True
        break
    if not updated:
        facts.append({"character_name": name, "fact": cleaned_fact})
    facts = facts[-40:]
    state["character_facts_list"] = facts
    state["character_facts"] = "\n".join(
        f"- {str(entry.get('character_name', '')).strip()}: {str(entry.get('fact', '')).strip()}"
        for entry in facts
        if isinstance(entry, dict)
        and str(entry.get("character_name", "")).strip()
        and str(entry.get("fact", "")).strip()
    )


def _apply_shared_toy_story_state(
    state: dict[str, Any],
    *,
    summary_text: str,
    toy_name_hint: str = "",
    toy_thumb: tuple[str, str] | None = None,
) -> None:
    normalized_summary = str(summary_text or "").strip() or (
        "The photo looks a little fuzzy, but it seems like a very special toy."
    )
    normalized_name_hint = _clean_shared_toy_name_hint(toy_name_hint)
    named_sidekick_description = _shared_toy_sidekick_description(
        normalized_summary,
        normalized_name_hint,
    )
    state["camera_stage"] = "done"
    state["camera_received"] = True
    if toy_thumb:
        state["toy_reference_thumbnail_b64"] = str(toy_thumb[0] or "").strip()
        state["toy_reference_thumbnail_mime"] = str(toy_thumb[1] or "").strip()
    state["toy_reference_visual_summary"] = normalized_summary
    state["toy_reference_name_hint"] = normalized_name_hint
    state["sidekick_description"] = named_sidekick_description or normalized_summary
    _upsert_character_fact(
        state,
        character_name=_SHARED_TOY_COMPANION_NAME,
        fact=named_sidekick_description or normalized_summary,
    )
    if normalized_name_hint:
        _upsert_character_fact(
            state,
            character_name=normalized_name_hint,
            fact=f"shared toy helper; {normalized_summary}",
        )
    continuity_label = normalized_name_hint or _SHARED_TOY_COMPANION_NAME
    prime_character_carryover(
        state,
        [continuity_label],
        source="shared_toy",
        description=named_sidekick_description or normalized_summary,
    )


def _scene_render_still_in_flight_after_tool_call(
    session_id: str,
    state: dict[str, Any],
) -> bool:
    if bool(state.get("scene_render_skipped", False)):
        return False
    image_event = _pending_image_events.get(session_id)
    if image_event is not None:
        return not image_event.is_set()
    return bool(
        str(state.get("pending_scene_description", "") or "").strip()
        or str(state.get("pending_scene_base_description", "") or "").strip()
    )


def _begin_toy_share_state(state: dict[str, Any], turns: int = 3) -> None:
    if not bool(state.get("toy_share_active", False)):
        _capture_toy_share_resume_state(state)
    state["toy_share_active"] = True
    state["toy_share_turns_remaining"] = max(1, turns)


def _finish_toy_share_state(state: dict[str, Any]) -> None:
    state["toy_share_active"] = False
    state["toy_share_turns_remaining"] = 0


async def _trigger_story_end(
    *,
    runner: Runner,
    websocket: WebSocket,
    session_id: str,
    user_id: str,
    live_queue: LiveRequestQueue,
    notify_frontend: bool = False,
) -> None:
    started_at_epoch_ms = int(time.time() * 1000)

    def _force_story_end(state: dict[str, Any]) -> None:
        try:
            max_turns = int(state.get("max_story_turns", _MAX_STORY_TURNS))
        except Exception:
            max_turns = _MAX_STORY_TURNS
        max_turns = max(_MIN_STORY_TURNS, min(max_turns, _MAX_STORY_TURNS_HARD))
        state["turn_number"] = max_turns
        state["story_turn_limit_reached"] = True
        state["pending_response"] = False
        state["awaiting_story_choice"] = False
        state["pending_story_hint"] = ""
        state["assembly_kind"] = "initial"
        state["assembly_status"] = "assembling"
        state["assembly_started_at_epoch_ms"] = started_at_epoch_ms
        state["assembly_recent_activities"] = []
        state["assembly_wait_prompt_count"] = 0
        state["scene_render_pending"] = False
        _clear_pending_scene_request_metadata(state)
        state["theater_release_ready"] = False
        _finish_toy_share_state(state)
        _sync_story_phase(session_id, state)

    await _mutate_state(
        runner=runner,
        user_id=user_id,
        session_id=session_id,
        mutator=_force_story_end,
    )
    _ending_story_sessions.add(session_id)
    _ending_story_flush_sessions.add(session_id)
    _assembly_intro_sent_sessions.discard(session_id)
    _story_turn_limit_sessions.discard(session_id)
    if notify_frontend:
        _publish_ui_command(
            session_id,
            "story_ending",
            message="Making your storybook movie…",
            eta_seconds=90,
            source="voice",
        )
    session = await runner.session_service.get_session(
        app_name="storyteller",
        user_id=user_id,
        session_id=session_id,
    )
    live_state: dict[str, Any] = {}
    if session and session.state:
        try:
            live_state = dict(session.state)
        except Exception:
            live_state = {}
    story_title, child_name = _storybook_identity_from_state(live_state)
    storybook_progress: dict[str, Any] = {
        "assembly_kind": "initial",
        "assembly_status": "assembling",
        "assembly_started_at_epoch_ms": started_at_epoch_ms,
        "assembly_recent_activities": [],
        "assembly_wait_prompt_count": 0,
        "scene_render_pending": False,
        "theater_release_ready": False,
    }
    if story_title:
        storybook_progress["story_title"] = story_title
    if child_name:
        storybook_progress["child_name"] = child_name
    if live_state:
        storybook_progress.update(
            {
                "story_pages": _story_pages_payload(live_state),
                "scene_asset_urls": list(live_state.get("scene_asset_urls", []) or []),
                "scene_asset_gcs_uris": list(live_state.get("scene_asset_gcs_uris", []) or []),
                "scene_descriptions": list(live_state.get("scene_descriptions", []) or []),
                "scene_storybeat_texts": list(live_state.get("scene_storybeat_texts", []) or []),
                "current_scene_description": str(live_state.get("current_scene_description", "") or "").strip(),
                "current_scene_storybeat_text": str(live_state.get("current_scene_storybeat_text", "") or "").strip(),
                "story_summary": str(live_state.get("story_summary", "") or "").strip(),
            }
        )
    progress_snapshot = {**live_state, **storybook_progress}
    storybook_progress["story_phase"] = _sync_story_phase(session_id, progress_snapshot)
    _update_storybook_firestore(session_id, storybook_progress)
    cache_storybook_state(session_id, progress_snapshot)
    logger.info("Story ending started for session %s; requesting movie assembly.", session_id)
    reset_storybook_assembly_lock(session_id)
    await _announce_storybook_assembly_started(
        websocket=websocket,
        session_id=session_id,
        eta_seconds=25 if _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False) else 90,
        story_title=story_title,
        child_name=child_name,
        started_at_epoch_ms=started_at_epoch_ms,
    )
    asyncio.create_task(assemble_story_video(session_id=session_id))
    asyncio.create_task(
        _release_end_story_flush_after_delay(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            live_queue=live_queue,
        )
    )


async def _trigger_toy_share_start(
    *,
    runner: Runner,
    session_id: str,
    user_id: str,
    live_queue: LiveRequestQueue,
    open_overlay: bool,
    source: str,
    send_prompt: bool = True,
) -> None:
    await _mutate_state(
        runner=runner,
        user_id=user_id,
        session_id=session_id,
        mutator=lambda state: _begin_toy_share_state(state, turns=3),
    )
    if open_overlay:
        _publish_ui_command(session_id, "open_toy_share", source=source)
    if send_prompt:
        _send_live_content(
            session_id,
            live_queue,
            (
                "The child wants a special little show-and-tell moment. "
                "In Amelia's warm voice, invite them to hold their toy up so you can peek at it, "
                "make them feel proud for sharing, and ask exactly one short question about it. "
                "Do not advance the story yet."
            ),
        )


async def _trigger_toy_share_end(
    *,
    runner: Runner,
    session_id: str,
    user_id: str,
    live_queue: LiveRequestQueue,
    close_overlay: bool,
    source: str,
    send_resume_prompt: bool,
) -> None:
    await _mutate_state(
        runner=runner,
        user_id=user_id,
        session_id=session_id,
        mutator=_finish_toy_share_state,
    )
    if close_overlay:
        _publish_ui_command(session_id, "close_toy_share", reason="resume_story", source=source)
    if send_resume_prompt:
        _send_live_content(
            session_id,
            live_queue,
            (
                "The child wants to go back to the story now. "
                "Give one short, warm bridge sentence that carries their toy into the adventure and resume the story. "
                "Do not ask more toy-sharing setup questions."
            ),
        )


async def _handle_voice_ui_intent(
    *,
    intent: str,
    payload: dict[str, Any],
    runner: Runner,
    websocket: WebSocket,
    session_id: str,
    user_id: str,
    live_queue: LiveRequestQueue,
) -> tuple[bool, bool]:
    if intent == "request_scene_branch":
        scene_number = int(payload.get("scene_number", 0) or 0)
        if scene_number <= 0:
            return False, False
        branch_request = await _request_scene_branch_confirmation(
            runner=runner,
            session_id=session_id,
            user_id=user_id,
            scene_number=scene_number,
        )
        if branch_request is None:
            if session_id not in _audio_seen_this_turn:
                _send_live_content(
                    session_id,
                    live_queue,
                    "Tell the child you could not find that earlier scene yet, and ask them to pick a different scene or keep going.",
                )
            return True, True
        _publish_ui_command(
            session_id,
            "open_scene_branch_picker",
            scene_number=scene_number,
            warning=(
                f"Going back to scene {scene_number} will remove the pages after it."
            ),
            scene_history=branch_request["scene_history"],
            source="voice",
        )
        if session_id not in _audio_seen_this_turn:
            _send_live_content(
                session_id,
                live_queue,
                (
                    f"The child wants to go back to scene {scene_number}. "
                    "Warn them in one short sentence that the pages after that scene will disappear, "
                    "then ask one yes-or-no confirmation question."
                ),
            )
        return True, True

    if intent == "confirm_scene_branch":
        scene_number = int(payload.get("scene_number", 0) or 0)
        if scene_number <= 0:
            return False, False
        branched = await _branch_story_to_scene(
            runner=runner,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
            scene_number=scene_number,
            source="voice",
        )
        if not branched:
            return True, False
        if session_id not in _audio_seen_this_turn:
            _send_live_content(
                session_id,
                live_queue,
                (
                    f"You just jumped back to scene {scene_number}. "
                    "Say one short magical sentence that you are back in that scene, then pause."
                ),
            )
        return True, True

    if intent == "cancel_scene_branch":
        await _mutate_state(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            mutator=_clear_pending_scene_branch,
        )
        _publish_ui_command(session_id, "close_scene_branch_picker", source="voice")
        if session_id not in _audio_seen_this_turn:
            _send_live_content(
                session_id,
                live_queue,
                "The child decided not to go back. Say one short sentence that you will stay in the current story scene.",
            )
        return True, True

    if intent == "open_toy_share":
        await _trigger_toy_share_start(
            runner=runner,
            session_id=session_id,
            user_id=user_id,
            live_queue=live_queue,
            open_overlay=True,
            source="voice",
            send_prompt=session_id not in _audio_seen_this_turn,
        )
        return True, True

    if intent == "close_toy_share":
        await _trigger_toy_share_end(
            runner=runner,
            session_id=session_id,
            user_id=user_id,
            live_queue=live_queue,
            close_overlay=True,
            source="voice",
            send_resume_prompt=session_id not in _audio_seen_this_turn,
        )
        return True, True

    if intent == "restart_story":
        _publish_ui_command(session_id, "restart_story", source="voice")
        return True, False

    if intent == "set_mic_enabled":
        enabled = bool(payload.get("enabled", True))
        _publish_ui_command(session_id, "set_mic_enabled", enabled=enabled, source="voice")
        if session_id not in _audio_seen_this_turn:
            _send_live_content(
                session_id,
                live_queue,
                (
                    "The app just changed the microphone button. "
                    f"In Amelia's voice, say one very short sentence that the mic is {'on' if enabled else 'off'} now."
                ),
            )
        return True, True

    if intent == "end_story":
        await _trigger_story_end(
            runner=runner,
            websocket=websocket,
            session_id=session_id,
            user_id=user_id,
            live_queue=live_queue,
            notify_frontend=True,
        )
        return True, True

    return False, False


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
    _mark_client_transport_activity(session_id, heartbeat=True)
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
    session_was_created = False
    if session is None:
        session = await runner.session_service.create_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
            state=copy.deepcopy(_SESSION_STATE_DEFAULTS),
        )
        session_was_created = True
        _opening_phase_sessions.add(session_id)
    else:
        _ensure_session_state_defaults(session.state)
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
        session.state["response_turn_number"] = max(int(session.state.get("response_turn_number", session.state.get("turn_number", 1))), 1)
    except Exception:
        session.state["response_turn_number"] = session.state.get("turn_number", 1)
    _sync_story_page_progress_fields(session.state)
    storybook_resume_state = load_storybook_resume_state(session_id)
    if session_was_created and _state_has_prior_story_context(storybook_resume_state):
        session.state.update(copy.deepcopy(storybook_resume_state))
        _ensure_session_state_defaults(session.state)
        _sync_story_page_progress_fields(session.state)
        logger.info("Rehydrated live session state from cached storybook state for session %s", session_id)
    try:
        current_name = str(session.state.get("child_name", "friend")).strip().lower()
        if current_name and current_name != "friend":
            session.state["name_confirmed"] = True
            session.state["camera_stage"] = "done"
            session.state["camera_skipped"] = True
    except Exception:
        pass
    if session.state.get("name_confirmed") or int(session.state.get("response_turn_number", 1)) >= 3:
        _opening_phase_sessions.discard(session_id)
    else:
        _opening_phase_sessions.add(session_id)
    if bool(session.state.get("story_turn_limit_reached", False) or session.state.get("story_page_limit_reached", False)):
        _story_turn_limit_sessions.add(session_id)
    else:
        _story_turn_limit_sessions.discard(session_id)
    _sync_story_phase(session_id, session.state)

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

    if _state_has_prior_story_context(session.state) or _state_has_prior_story_context(storybook_resume_state):
        _greeting_sent_sessions.add(session_id)
        _awaiting_greeting_sessions.discard(session_id)
    assembly_resuming = (
        _storybook_assembly_in_progress(session.state)
        or _storybook_assembly_in_progress(storybook_resume_state)
    )
    if assembly_resuming:
        _ending_story_sessions.add(session_id)
        _story_turn_limit_sessions.discard(session_id)
    else:
        _ending_story_sessions.discard(session_id)
        _ending_story_flush_sessions.discard(session_id)
        _assembly_intro_sent_sessions.discard(session_id)
    _sync_story_phase(session_id, session.state)

    _rewind_locks.setdefault(session_id, asyncio.Lock())
    rehydrated_state_snapshot = dict(session.state)
    if isinstance(storybook_resume_state, dict) and storybook_resume_state:
        rehydrated_state_snapshot.update(storybook_resume_state)
    rehydrated_story_phase = derive_story_phase(
        rehydrated_state_snapshot,
        opening_phase=session_id in _opening_phase_sessions,
        ending_story=session_id in _ending_story_sessions or assembly_resuming,
        assistant_speaking=(
            session_id in _assistant_speaking_sessions
            or session_id in _awaiting_greeting_sessions
        ),
        pending_scene_render=(
            bool(rehydrated_state_snapshot.get("scene_render_pending", False))
            or session_id in _pending_image_events
        ),
    )
    rehydrated_scene_urls = list(rehydrated_state_snapshot.get("scene_asset_urls", []) or [])

    await websocket.send_text(
        ServerEvent(
            type=ServerEventType.SESSION_REHYDRATED,
            payload={
                "session_id": session_id,
                "turn_number": session.state.get("turn_number", 1),
                "max_story_turns": session.state.get("max_story_turns", _MAX_STORY_TURNS),
                "child_name": session.state.get("child_name", "friend"),
                "story_title": (
                    (storybook_resume_state.get("story_title") if isinstance(storybook_resume_state, dict) else None)
                    or _storybook_identity_from_state(session.state)[0]
                ),
                "story_summary": session.state.get("story_summary", ""),
                "server_vad_enabled": _env_enabled("ENABLE_SERVER_VAD", default=False),
                # Re-sync information to help frontend recover UI state
                "current_scene_image_url": rehydrated_scene_urls[-1] if rehydrated_scene_urls else None,
                "current_scene_description": rehydrated_state_snapshot.get("current_scene_description", ""),
                "current_scene_storybeat_text": rehydrated_state_snapshot.get("current_scene_storybeat_text", ""),
                "scene_history": _scene_branch_public_payload(rehydrated_state_snapshot),
                "story_started": bool(session.state.get("story_started", False)),
                "story_phase": rehydrated_story_phase,
                "toy_share_active": bool(session.state.get("toy_share_active", False)),
                "pending_response": bool(session.state.get("pending_response", False)),
                "assistant_speaking": session_id in _assistant_speaking_sessions,
                "ending_story": session_id in _ending_story_sessions or assembly_resuming,
                "assembly_status": str(rehydrated_state_snapshot.get("assembly_status", "") or "").strip(),
                "assembly_error": str(rehydrated_state_snapshot.get("assembly_error", "") or "").strip(),
                "assembly_started_at_epoch_ms": (
                    _assembly_started_at_epoch_ms_from_state(storybook_resume_state)
                    or _assembly_started_at_epoch_ms_from_state(session.state)
                ),
                "assembly_eta_seconds": 25 if _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False) else 90,
            },
        ).model_dump_json()
    )
    if assembly_resuming:
        _ensure_final_video_watch_task(session_id)
    if storybook_resume_state:
        await _restore_storybook_ui_after_reconnect(
            websocket=websocket,
            session_id=session_id,
            state=storybook_resume_state,
        )

    live_queue = ResettableLiveRequestQueue()

    from agent.storyteller_agent import run_config

    agent_task: asyncio.Task | None = None
    session_event_task: asyncio.Task | None = None
    heartbeat_task = asyncio.create_task(_heartbeat(websocket))
    heartbeat_watchdog_task = asyncio.create_task(_heartbeat_watchdog(websocket, session_id, connection_id))

    early_audio_drop_logged = False

    async def _ensure_agent_started() -> None:
        nonlocal agent_task, session_event_task, early_audio_drop_logged
        if agent_task is None or agent_task.done():
            await _prepare_clean_live_reconnect(runner, user_id, session_id)
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
                    live_queue,
                )
            )
        early_audio_drop_logged = False

    async def _should_autostart_live_agent() -> bool:
        if session_id not in _greeting_sent_sessions or session_id in _awaiting_greeting_sessions:
            return True
        try:
            session = await runner.session_service.get_session(
                app_name="storyteller",
                user_id=user_id,
                session_id=session_id,
            )
        except Exception:
            session = None
        state = session.state if session else {}
        pending_response = bool(state.get("pending_response", False))
        last_child = str(state.get("last_child_utterance", "") or "").strip()
        return pending_response and bool(last_child)

    json_buffer = ""
    disconnect_reason = "clean"

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive(), timeout=120.0)
            except asyncio.TimeoutError:
                disconnect_reason = "timeout"
                _bump_live_telemetry("disconnect.timeout")
                _emit_live_telemetry(
                    "websocket_disconnect",
                    session_id=session_id,
                    include_runtime=True,
                    reason=disconnect_reason,
                )
                logger.warning("Session %s timed out waiting for websocket activity.", session_id)
                break
            except WebSocketDisconnect:
                disconnect_reason = _forced_disconnect_reasons.pop(session_id, "websocket")
                if disconnect_reason == "websocket":
                    _bump_live_telemetry("disconnect.websocket")
                    _emit_live_telemetry(
                        "websocket_disconnect",
                        session_id=session_id,
                        include_runtime=True,
                        reason=disconnect_reason,
                    )
                logger.info("Client disconnected: session %s (%s)", session_id, disconnect_reason)
                break
            except RuntimeError as exc:
                if "disconnect message" in str(exc).lower():
                    disconnect_reason = "proxy"
                    _bump_live_telemetry("disconnect.proxy")
                    _emit_live_telemetry(
                        "websocket_disconnect",
                        session_id=session_id,
                        include_runtime=True,
                        reason=disconnect_reason,
                    )
                    logger.info("Client disconnected via proxy: session %s", session_id)
                    break
                raise

            _mark_client_transport_activity(session_id)

            if "bytes" in raw and raw["bytes"]:
                if _page_read_aloud_suppression_active(session_id):
                    continue
                if agent_task is None or agent_task.done():
                    if agent_task is not None and agent_task.done():
                        await _ensure_agent_started()
                    elif _env_enabled("ENABLE_SERVER_VAD", default=False):
                        await _ensure_agent_started()
                    if agent_task is not None:
                        early_audio_drop_logged = False
                    else:
                        # Drop early audio until the live agent is started (setup-first).
                        if not early_audio_drop_logged:
                            logger.info(
                                "Dropping early audio before live agent start for session %s; waiting for client_ready.",
                                session_id,
                            )
                            early_audio_drop_logged = True
                        continue
                if session_id in _awaiting_greeting_sessions:
                    # If the child starts speaking before the greeting lands,
                    # we drop the audio if barge-in is disabled so the greeting isn't interrupted.
                    if not _barge_in_enabled():
                        continue
                    _awaiting_greeting_sessions.discard(session_id)
                if not _barge_in_enabled() and session_id in _assistant_speaking_sessions:
                    # Don't allow background noise to interrupt Amelia while she's speaking,
                    # but release the lock after a short timeout so we don't ignore the child's reply.
                    suppress_for = _env_float("BARGE_IN_SUPPRESS_SECONDS", 1.2)
                    last_spoke = _assistant_speaking_since.get(session_id)
                    if last_spoke and (time.monotonic() - last_spoke) < suppress_for:
                        continue
                    _assistant_speaking_sessions.discard(session_id)
                    _assistant_speaking_since.pop(session_id, None)
                if session_id in _ending_story_flush_sessions:
                    continue
                if session_id in _story_turn_limit_sessions and session_id not in _ending_story_sessions:
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
                        if session_id in _ending_story_sessions and cmd.type not in {
                            ClientCommandType.HEARTBEAT,
                            ClientCommandType.THEATER_CLOSE,
                            ClientCommandType.MOVIE_FEEDBACK,
                            ClientCommandType.MOVIE_REMAKE,
                            ClientCommandType.ASSEMBLY_PLAY_PROMPT,
                            ClientCommandType.CLIENT_READY,
                            ClientCommandType.ACTIVITY_START,
                            ClientCommandType.ACTIVITY_END,
                            ClientCommandType.PAGE_READ_ALOUD,
                        }:
                            continue
                        if cmd.type == ClientCommandType.ACTIVITY_START and (agent_task is None or agent_task.done()):
                            await _ensure_agent_started()
                        if (
                            session_id in _story_turn_limit_sessions
                            and session_id not in _ending_story_sessions
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
                        if cmd.type == ClientCommandType.CLIENT_READY and await _should_autostart_live_agent():
                            await _ensure_agent_started()
                    except Exception as exc:
                        logger.error("Command handling error: %s", exc)

    except WebSocketDisconnect:
        disconnect_reason = _forced_disconnect_reasons.pop(session_id, "websocket")
        if disconnect_reason == "websocket":
            _bump_live_telemetry("disconnect.websocket")
            _emit_live_telemetry(
                "websocket_disconnect",
                session_id=session_id,
                include_runtime=True,
                reason=disconnect_reason,
            )
        logger.info("Client disconnected: session %s (%s)", session_id, disconnect_reason)
    finally:
        for task in (agent_task, heartbeat_task, heartbeat_watchdog_task, session_event_task):
            if task:
                task.cancel()
        live_queue.close()
        if _connection_is_current(session_id, connection_id, websocket):
            storybook_resume_state = load_storybook_resume_state(session_id)
            preserve_storybook_events = _storybook_assembly_in_progress(storybook_resume_state)
            had_mid_turn_audio = session_id in _activity_active_sessions or session_id in _audio_seen_this_turn
            _story_turn_limit_sessions.discard(session_id)
            _story_turn_limit_notified_sessions.discard(session_id)
            _awaiting_greeting_sessions.discard(session_id)
            _opening_phase_sessions.discard(session_id)
            _assistant_speaking_sessions.discard(session_id)
            _assistant_speaking_since.pop(session_id, None)
            _interrupted_turn_sessions.discard(session_id)
            _page_read_aloud_active_sessions.discard(session_id)
            _page_read_aloud_suppress_until.pop(session_id, None)
            _ending_story_sessions.discard(session_id)
            _ending_story_flush_sessions.discard(session_id)
            _video_generation_started_sessions.discard(session_id)
            _watching_final_video_sessions.discard(session_id)
            _final_video_watch_not_before_epoch.pop(session_id, None)
            _active_websockets.pop(session_id, None)
            _active_connection_ids.pop(session_id, None)
            _last_client_transport_at.pop(session_id, None)
            _last_client_heartbeat_at.pop(session_id, None)
            _forced_disconnect_reasons.pop(session_id, None)
            _activity_active_sessions.discard(session_id)
            _activity_last_change.pop(session_id, None)
            _live_request_debug.pop(session_id, None)
            _recent_finished_child_transcripts.pop(session_id, None)
            # NOTE: _audio_seen_this_turn is now cleared only on turn completion
            # to ensure it persists across fast reconnections during a turn.
            if had_mid_turn_audio:
                await _promote_partial_child_utterance_to_pending(runner, user_id, session_id)
                _clean_live_reconnect_sessions.add(session_id)
                _emit_live_telemetry(
                    "mid_turn_disconnect",
                    session_id=session_id,
                    include_runtime=True,
                    reason=disconnect_reason,
                )
                logger.warning(
                    "Session %s disconnected with an active voice turn; next live reconnect will start clean.",
                    session_id,
                )
            if preserve_storybook_events:
                logger.info(
                    "Preserving queued storybook events for session %s while movie assembly is in progress.",
                    session_id,
                )
            else:
                _assembly_intro_sent_sessions.discard(session_id)
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
    completed_response_turn_number = 1
    completed_name_confirmed = False
    completed_camera_stage = "none"
    completed_story_turn_limit = False
    completed_story_started = False
    completed_toy_share_active = False
    completed_scene_chat_turn = False
    completed_story_page_turn = False
    completed_scene_render_skipped = False
    toy_share_finished_now = False
    silent_recovery_attempts = 0
    last_output_transcription: str = ""
    assistant_finished_utterances_this_turn = 0
    last_finished_assistant_output_at_epoch_ms = 0
    heard_humanish_speech_this_turn = False
    heard_noise_only_this_turn = False
    reconnect_attempt = 0
    hard_reset_attempts = 0
    last_persisted_partial_child_utterance = ""
    last_persisted_partial_child_utterance_finished = False
    child_turn_attempted_this_turn = False
    child_turn_answered_this_turn = False
    assistant_output_soft_closed = False
    assistant_output_hard_closed = False
    assistant_audio_closed_after_finish = False
    scene_narration_guard_active = False
    turn_output_sealed_until_child_input = False
    deferred_opening_turn_complete = False
    deferred_opening_finalize_task: asyncio.Task | None = None
    turn_output_seal_task: asyncio.Task | None = None

    try:
        max_reconnect_attempts = int(os.environ.get("MAX_LIVE_RECONNECTS", "6"))
    except Exception:
        max_reconnect_attempts = 6

    def _cancel_deferred_opening_finalize() -> None:
        nonlocal deferred_opening_finalize_task
        task = deferred_opening_finalize_task
        deferred_opening_finalize_task = None
        if task and not task.done():
            task.cancel()

    def _cancel_turn_output_seal() -> None:
        nonlocal turn_output_seal_task
        task = turn_output_seal_task
        turn_output_seal_task = None
        if task and not task.done():
            task.cancel()

    def _schedule_turn_output_seal(delay_seconds: float = 0.0) -> None:
        nonlocal turn_output_sealed_until_child_input
        nonlocal turn_output_seal_task
        _cancel_turn_output_seal()
        if delay_seconds <= 0:
            turn_output_sealed_until_child_input = True
            return

        async def _seal_later() -> None:
            nonlocal turn_output_sealed_until_child_input
            nonlocal turn_output_seal_task
            try:
                await asyncio.sleep(delay_seconds)
                turn_output_sealed_until_child_input = True
            except asyncio.CancelledError:
                return
            finally:
                turn_output_seal_task = None

        turn_output_seal_task = asyncio.create_task(_seal_later())

    async def _finalize_deferred_opening_turn() -> bool:
        nonlocal assistant_parts
        nonlocal last_output_transcription
        nonlocal child_utterance_this_turn
        nonlocal last_persisted_partial_child_utterance
        nonlocal last_persisted_partial_child_utterance_finished
        nonlocal child_turn_attempted_this_turn
        nonlocal child_turn_answered_this_turn
        nonlocal model_emitted_meaningful_output
        nonlocal assistant_output_soft_closed
        nonlocal assistant_output_hard_closed
        nonlocal assistant_audio_closed_after_finish
        nonlocal assistant_finished_utterances_this_turn
        nonlocal heard_humanish_speech_this_turn
        nonlocal heard_noise_only_this_turn
        nonlocal turn_output_sealed_until_child_input
        nonlocal deferred_opening_turn_complete
        nonlocal silent_recovery_attempts
        nonlocal _turn_start_t

        if not deferred_opening_turn_complete:
            return False

        assistant_text = " ".join(assistant_parts).strip()
        if not assistant_text and _is_meaningful_text(last_output_transcription):
            assistant_text = last_output_transcription
        if not _is_meaningful_text(assistant_text):
            return False

        completed_state_snapshot: dict[str, Any] = {}

        def _on_late_opening_turn_complete(state: dict[str, Any]) -> None:
            nonlocal completed_state_snapshot
            _take_snapshot(state)
            state["pending_response"] = False
            state["pending_response_interrupted"] = False
            state["scene_tool_turn_open"] = False
            state["assembly_wait_last_child_utterance"] = ""
            state["partial_child_utterance"] = ""
            state["partial_child_utterance_finished"] = False
            if state.get("name_confirmed") or int(state.get("response_turn_number", state.get("turn_number", 1))) >= 3:
                _opening_phase_sessions.discard(session_id)
            else:
                _opening_phase_sessions.add(session_id)
            _append_story_summary(state, assistant_text)

            pending_name = str(state.get("pending_child_name", "")).strip()
            if (
                assistant_text
                and pending_name
                and not bool(state.get("name_confirmed", False))
            ):
                prompt_match = re.search(
                    r"(did i hear (your name is|you say)|is your name|your name is|did i get your name|your magical name|is it)",
                    assistant_text,
                    flags=re.IGNORECASE,
                )
                name_in_question = (
                    "?" in assistant_text
                    and re.search(rf"\\b{re.escape(pending_name)}\\b", assistant_text, flags=re.IGNORECASE)
                )
                if prompt_match or name_in_question:
                    state["name_confirmation_prompted"] = True

            if (
                assistant_text
                and bool(state.get("name_confirmed", False))
                and not bool(state.get("story_started", False))
                and _STORY_PROMPT_RE.search(assistant_text)
            ):
                state["awaiting_story_choice"] = True
            elif bool(state.get("story_started", False)):
                state["awaiting_story_choice"] = False

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

            try:
                current_response_turn = int(state.get("response_turn_number", state.get("turn_number", 1)) or 1)
            except Exception:
                current_response_turn = 1
            current_response_turn = max(current_response_turn, 1)
            state["response_turn_number"] = current_response_turn + 1

            if (
                state.get("response_turn_number", state.get("turn_number", 1)) >= 3
                and str(state.get("child_name", "friend")).strip().lower() == "friend"
                and not bool(state.get("name_confirmed", False))
            ):
                state["name_confirmed"] = True
                state["pending_child_name"] = ""
                state["name_confirmation_prompted"] = False

            _sync_story_phase(session_id, state)
            completed_state_snapshot = dict(state)

        await _mutate_state(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            mutator=_on_late_opening_turn_complete,
        )
        cache_storybook_state(session_id, completed_state_snapshot)

        try:
            await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
        except Exception:
            return False

        logger.info(
            "Finalized deferred opening turn after late narration for session %s",
            session_id,
        )
        _cancel_deferred_opening_finalize()
        deferred_opening_turn_complete = False
        silent_recovery_attempts = 0
        turn_output_sealed_until_child_input = True
        assistant_parts = []
        last_output_transcription = ""
        child_utterance_this_turn = ""
        last_persisted_partial_child_utterance = ""
        last_persisted_partial_child_utterance_finished = False
        child_turn_attempted_this_turn = False
        child_turn_answered_this_turn = False
        model_emitted_meaningful_output = False
        assistant_output_soft_closed = False
        assistant_output_hard_closed = False
        assistant_audio_closed_after_finish = False
        assistant_finished_utterances_this_turn = 0
        heard_humanish_speech_this_turn = False
        heard_noise_only_this_turn = False
        _audio_seen_this_turn.discard(session_id)
        _assistant_speaking_sessions.discard(session_id)
        _assistant_speaking_since.pop(session_id, None)
        _turn_start_t = time.monotonic()
        return True

    async def _retry_deferred_opening_turn() -> bool:
        nonlocal assistant_parts
        nonlocal last_output_transcription
        nonlocal child_utterance_this_turn
        nonlocal last_persisted_partial_child_utterance
        nonlocal last_persisted_partial_child_utterance_finished
        nonlocal child_turn_attempted_this_turn
        nonlocal child_turn_answered_this_turn
        nonlocal scene_visuals_called_this_turn
        nonlocal model_emitted_meaningful_output
        nonlocal assistant_output_soft_closed
        nonlocal assistant_output_hard_closed
        nonlocal assistant_audio_closed_after_finish
        nonlocal assistant_finished_utterances_this_turn
        nonlocal heard_humanish_speech_this_turn
        nonlocal heard_noise_only_this_turn
        nonlocal turn_output_sealed_until_child_input
        nonlocal deferred_opening_turn_complete
        nonlocal silent_recovery_attempts
        nonlocal _turn_start_t

        if not deferred_opening_turn_complete:
            return False

        deferred_opening_turn_complete = False
        recovery_reason = ""
        storage_session = _get_storage_session(runner, user_id, session_id)
        opening_state = (
            dict(storage_session.state)
            if storage_session is not None and isinstance(storage_session.state, dict)
            else {}
        )

        if session_id in _awaiting_greeting_sessions:
            if silent_recovery_attempts < 2:
                silent_recovery_attempts += 1
                greeting_prompt = (
                    "A child just joined. Greet them with very short, simple sentences for a 4-year-old. "
                    "Ask their name, and say they can also tell you what story they want."
                )
                recovery_reason = "silent_greeting_retry"
                live_queue.reset(session_id, recovery_reason)
                _send_live_content(session_id, live_queue, greeting_prompt)
            else:
                _awaiting_greeting_sessions.discard(session_id)
        elif session_id in _opening_phase_sessions and _opening_phase_needs_first_greeting(opening_state):
            if silent_recovery_attempts < 2:
                silent_recovery_attempts += 1
                greeting_prompt = (
                    "You're still in the opening. Repeat the greeting in very short, simple sentences. "
                    "Ask their name, and say they can also tell you what story they want."
                )
                recovery_reason = "opening_greeting_retry"
                live_queue.reset(session_id, recovery_reason)
                _send_live_content(session_id, live_queue, greeting_prompt)
        else:
            return False

        logger.warning(
            "Opening-phase turn still had no finalized narration after grace period for session %s; %s.",
            session_id,
            "retrying greeting" if recovery_reason else "ending defer without retry",
        )

        try:
            await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
        except Exception:
            return False

        turn_output_sealed_until_child_input = True
        assistant_parts = []
        last_output_transcription = ""
        child_utterance_this_turn = ""
        last_persisted_partial_child_utterance = ""
        last_persisted_partial_child_utterance_finished = False
        child_turn_attempted_this_turn = False
        child_turn_answered_this_turn = False
        scene_visuals_called_this_turn = False
        model_emitted_meaningful_output = False
        assistant_output_soft_closed = False
        assistant_output_hard_closed = False
        assistant_audio_closed_after_finish = False
        assistant_finished_utterances_this_turn = 0
        heard_humanish_speech_this_turn = False
        heard_noise_only_this_turn = False
        _audio_seen_this_turn.discard(session_id)
        _assistant_speaking_sessions.discard(session_id)
        _assistant_speaking_since.pop(session_id, None)
        _turn_start_t = time.monotonic()
        return True

    def _schedule_deferred_opening_finalize() -> None:
        nonlocal deferred_opening_finalize_task
        _cancel_deferred_opening_finalize()

        async def _runner() -> None:
            try:
                await asyncio.sleep(0.35)
                finalized = await _finalize_deferred_opening_turn()
                if not finalized:
                    await _retry_deferred_opening_turn()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.debug(
                    "Deferred opening finalize failed for session %s: %s",
                    session_id,
                    exc,
                )

        deferred_opening_finalize_task = asyncio.create_task(_runner())

    async def _perform_hard_live_reset(*, reason: str, user_message: str) -> None:
        nonlocal hard_reset_attempts
        nonlocal reconnect_attempt
        nonlocal assistant_output_soft_closed
        nonlocal assistant_output_hard_closed
        nonlocal assistant_audio_closed_after_finish
        nonlocal assistant_finished_utterances_this_turn
        nonlocal scene_narration_guard_active
        nonlocal turn_output_sealed_until_child_input
        nonlocal deferred_opening_turn_complete
        hard_reset_attempts += 1
        reconnect_attempt = 0
        assistant_output_soft_closed = False
        assistant_output_hard_closed = False
        assistant_audio_closed_after_finish = False
        assistant_finished_utterances_this_turn = 0
        scene_narration_guard_active = False
        turn_output_sealed_until_child_input = False
        deferred_opening_turn_complete = False
        _cancel_deferred_opening_finalize()
        _emit_live_telemetry(
            "live_hard_reset",
            session_id=session_id,
            include_runtime=True,
            hard_reset_attempt=hard_reset_attempts,
            error=reason,
        )
        logger.warning("Live stream hard reset for session %s after error: %s", session_id, reason)
        await _promote_partial_child_utterance_to_pending(runner, user_id, session_id)
        await _prune_session_history(runner, user_id, session_id)
        live_queue.reset(session_id, f"hard_reset_{hard_reset_attempts}")
        try:
            await websocket.send_text(
                ServerEvent(
                    type=ServerEventType.ERROR,
                    payload={
                        "message": user_message,
                        "auto_resume": True,
                    },
                ).model_dump_json()
            )
        except Exception:
            pass
        resumed = await _resume_pending_child_turn(
            runner,
            user_id,
            session_id,
            live_queue,
            recovery_reason=f"hard_reset_{hard_reset_attempts}",
        )
        await asyncio.sleep(1.1 if resumed else 2.0)

    try:
        while True:
            try:
                try:
                    service = runner.session_service
                    storage_session = service.sessions["storyteller"][user_id][session_id]  # type: ignore[attr-defined]
                    _ensure_session_state_defaults(storage_session.state)
                except Exception:
                    pass
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
                            if session_id in _ending_story_flush_sessions:
                                logger.debug(
                                    "Dropping residual assistant live content after end-story for session %s",
                                    session_id,
                                )
                                continue
                            if assistant_output_soft_closed or assistant_output_hard_closed:
                                logger.debug(
                                    "Dropping assistant content after Amelia already completed the turn for session %s",
                                    session_id,
                                )
                                continue

                            if part.inline_data and part.inline_data.data:
                                if turn_output_sealed_until_child_input and session_id not in _ending_story_sessions:
                                    logger.debug(
                                        "Dropping stale assistant audio after turn completion for session %s",
                                        session_id,
                                    )
                                    continue
                                if assistant_audio_closed_after_finish and session_id not in _ending_story_sessions:
                                    logger.debug(
                                        "Dropping assistant audio after first finished utterance for session %s",
                                        session_id,
                                    )
                                    continue
                                if session_id in _interrupted_turn_sessions:
                                    logger.debug(
                                        "Dropping residual assistant audio after barge-in for session %s",
                                        session_id,
                                    )
                                    continue
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
                                if session_id in _ending_story_sessions:
                                    logger.info(
                                        "Dropping tool call during movie assembly for session %s",
                                        session_id,
                                    )
                                    continue
                                storage_session = _get_storage_session(runner, user_id, session_id)
                                current_tool_state = (
                                    dict(storage_session.state)
                                    if storage_session is not None and isinstance(storage_session.state, dict)
                                    else {}
                                )
                                if _should_hold_scene_tools_for_pending_replacement_ack(current_tool_state):
                                    logger.info(
                                        "Ignoring function call while waiting for pending replacement acknowledgment in session %s",
                                        session_id,
                                    )
                                    continue
                                if turn_output_sealed_until_child_input and session_id not in _ending_story_sessions:
                                    logger.info(
                                        "Dropping stale function call after turn completion for session %s",
                                        session_id,
                                    )
                                    continue
                                if session_id in _interrupted_turn_sessions:
                                    logger.info(
                                        "Ignoring function call emitted after barge-in for session %s",
                                        session_id,
                                    )
                                    continue
                                model_emitted_meaningful_output = True
                                fc = part.function_call
                                fc_name = getattr(fc, "name", "")
                                if fc_name == "generate_scene_visuals":
                                    scene_visuals_called_this_turn = True
                                    _scene_gen_requested_at[session_id] = time.monotonic()
                                    logger.info("⏱️ TIMING [ws] generate_scene_visuals function_call intercepted | session=%s", session_id)
                                    # Set up sync event EARLY so _forward_session_events can signal
                                    # it even if video_ready arrives before turn_complete.
                                    if session_id not in _pending_image_events:
                                        _pending_image_events[session_id] = asyncio.Event()
                                        logger.info("⏱️ SYNC [ws] image sync event created at function_call time | session=%s", session_id)

                            if part.function_response:
                                if session_id in _ending_story_sessions:
                                    logger.info(
                                        "Dropping tool response during movie assembly for session %s",
                                        session_id,
                                    )
                                    continue
                                storage_session = _get_storage_session(runner, user_id, session_id)
                                current_tool_state = (
                                    dict(storage_session.state)
                                    if storage_session is not None and isinstance(storage_session.state, dict)
                                    else {}
                                )
                                if _should_hold_scene_tools_for_pending_replacement_ack(current_tool_state):
                                    logger.info(
                                        "Ignoring function response while waiting for pending replacement acknowledgment in session %s",
                                        session_id,
                                    )
                                    continue
                                if turn_output_sealed_until_child_input and session_id not in _ending_story_sessions:
                                    logger.info(
                                        "Dropping stale function response after turn completion for session %s",
                                        session_id,
                                    )
                                    continue
                                if session_id in _interrupted_turn_sessions:
                                    logger.info(
                                        "Ignoring function response emitted after barge-in for session %s",
                                        session_id,
                                    )
                                    continue
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
                            cleaned_text = _normalize_transcript_text(raw_text)
                            scrubbed = await scrub_pii(cleaned_text or raw_text)
                            if _page_read_aloud_suppression_active(session_id):
                                logger.info(
                                    "Ignoring child transcription during page read-aloud suppression for session %s: %s",
                                    session_id,
                                    scrubbed[:120],
                                )
                                continue
                            logger.info("Child said (scrubbed): %s", scrubbed[:120])
                            input_finished = bool(getattr(event.input_transcription, "finished", False))
                            meta_only_finished_transcription = input_finished and _is_meta_only_transcription(raw_text)
                            punctuation_only_finished_transcription = (
                                input_finished
                                and bool(cleaned_text)
                                and not any(ch.isalnum() for ch in cleaned_text)
                            )
                            ignored_finished_transcription = (
                                meta_only_finished_transcription or punctuation_only_finished_transcription
                            )
                            if ignored_finished_transcription:
                                logger.info(
                                    "Ignoring finished non-speech transcription for session %s: %s",
                                    session_id,
                                    scrubbed[:120],
                                )
                                # If the live model started a turn from pure placeholder/noise input,
                                # mark it interrupted so any stray follow-up text is discarded.
                                if (
                                    session_id in _assistant_speaking_sessions
                                    or session_id in _awaiting_greeting_sessions
                                ):
                                    _activate_barge_in(session_id)
                            humanish_child_text = _looks_like_child_speech(cleaned_text)
                            actionable_child_text = _is_actionable_child_text(cleaned_text)
                            duplicate_finished_child_transcript = (
                                input_finished
                                and actionable_child_text
                                and not ignored_finished_transcription
                                and _is_duplicate_finished_child_transcript(session_id, cleaned_text)
                            )
                            if duplicate_finished_child_transcript:
                                logger.info(
                                    "Ignoring duplicate finished child transcription for session %s: %s",
                                    session_id,
                                    scrubbed[:120],
                                )
                                continue
                            voice_ui_consumed = False
                            voice_ui_marks_pending = False
                            if input_finished and cleaned_text and not ignored_finished_transcription:
                                if humanish_child_text:
                                    heard_humanish_speech_this_turn = True
                                    heard_noise_only_this_turn = False
                                elif not heard_humanish_speech_this_turn:
                                    heard_noise_only_this_turn = True
                            if actionable_child_text:
                                _cancel_turn_output_seal()
                                scene_narration_guard_active = False
                                assistant_output_soft_closed = False
                                assistant_output_hard_closed = False
                                assistant_audio_closed_after_finish = False
                                assistant_finished_utterances_this_turn = 0
                                turn_output_sealed_until_child_input = False
                                child_utterance_this_turn = _merge_streaming_transcript(
                                    child_utterance_this_turn,
                                    cleaned_text,
                                )
                                last_child_utterance = child_utterance_this_turn
                                if (
                                    not child_turn_attempted_this_turn
                                    and (
                                        input_finished
                                        or _partial_child_utterance_is_resumable(child_utterance_this_turn)
                                    )
                                ):
                                    child_turn_attempted_this_turn = True
                                    _bump_live_telemetry("child_turn.attempted")
                                # ── NEW: reset trackers at the start of a meaningful interaction ──
                                _early_fallback_started.discard(session_id)
                                _audio_seen_this_turn.add(session_id)

                                should_persist_partial = (
                                    input_finished
                                    or _partial_child_utterance_is_resumable(child_utterance_this_turn)
                                )
                                partial_text_changed = (
                                    child_utterance_this_turn != last_persisted_partial_child_utterance
                                )
                                partial_finish_state_changed = (
                                    input_finished and not last_persisted_partial_child_utterance_finished
                                )
                                if should_persist_partial and (
                                    partial_text_changed or partial_finish_state_changed
                                ):
                                    def _store_partial_child_utterance(state: dict[str, Any]) -> None:
                                        state["partial_child_utterance"] = child_utterance_this_turn
                                        state["partial_child_utterance_finished"] = input_finished
                                        if input_finished:
                                            state["pending_response_interrupted"] = False
                                            _append_child_delight_anchor(state, child_utterance_this_turn)
                                            _capture_child_story_continuity(state, child_utterance_this_turn)

                                    await _mutate_state(
                                        runner=runner,
                                        user_id=user_id,
                                        session_id=session_id,
                                        mutator=_store_partial_child_utterance,
                                    )
                                    last_persisted_partial_child_utterance = child_utterance_this_turn
                                    last_persisted_partial_child_utterance_finished = input_finished

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
                            if input_finished and not ignored_finished_transcription:
                                session = await runner.session_service.get_session(
                                    app_name="storyteller",
                                    user_id=user_id,
                                    session_id=session_id,
                                )
                                state = session.state if session else {}
                                if actionable_child_text:
                                    actionable_child_text = _is_actionable_child_text(
                                        cleaned_text or raw_text,
                                        state,
                                    )
                                    if not actionable_child_text:
                                        logger.info(
                                            "Ignoring low-signal finished child transcription for session %s: %s",
                                            session_id,
                                            scrubbed[:120],
                                        )
                                if (
                                    actionable_child_text
                                    and _should_resume_story_from_toy_share(cleaned_text or raw_text, state)
                                ):
                                    await _mutate_state(
                                        runner=runner,
                                        user_id=user_id,
                                        session_id=session_id,
                                        mutator=_finish_toy_share_state,
                                    )
                                    state["toy_share_active"] = False
                                    state["toy_share_turns_remaining"] = 0
                                    _publish_ui_command(
                                        session_id,
                                        "close_toy_share",
                                        reason="resume_story",
                                        source="system",
                                    )
                                    logger.info(
                                        "Auto-resuming story from toy share for session %s after child request: %s",
                                        session_id,
                                        (cleaned_text or raw_text)[:160],
                                    )
                                story_started = bool(state.get("story_started", False))
                                assembly_wait_mode = (
                                    session_id in _ending_story_sessions
                                    and session_id not in _ending_story_flush_sessions
                                    and _storybook_assembly_in_progress(state)
                                )
                                pending_scene_render_mode = (
                                    actionable_child_text
                                    and _can_interact_with_pending_scene_render(session_id, state)
                                )
                                scene_render_wait_mode = (
                                    pending_scene_render_mode
                                    and not _child_requested_scene_refresh(cleaned_text or raw_text)
                                )
                                if actionable_child_text and assembly_wait_mode:
                                    wait_activity: dict[str, str] = {"key": ""}

                                    def _mark_pending_assembly(s: dict[str, Any]) -> None:
                                        wait_activity["key"] = _choose_assembly_wait_activity(
                                            s,
                                            child_utterance=cleaned_text or raw_text,
                                        )
                                        _mark_pending_assembly_wait_response(
                                            s,
                                            child_utterance=cleaned_text or raw_text,
                                            activity_key=wait_activity["key"],
                                        )

                                    await _mutate_state(
                                        runner=runner,
                                        user_id=user_id,
                                        session_id=session_id,
                                        mutator=_mark_pending_assembly,
                                    )
                                    voice_ui_consumed = True
                                elif actionable_child_text and scene_render_wait_mode:
                                    await _mutate_state(
                                        runner=runner,
                                        user_id=user_id,
                                        session_id=session_id,
                                        mutator=lambda s: _mark_pending_scene_wait_response(
                                            s,
                                            child_utterance=cleaned_text or raw_text,
                                        ),
                                    )
                                    voice_ui_consumed = True
                                if actionable_child_text and not voice_ui_consumed:
                                    intent, intent_payload = _detect_voice_ui_intent(cleaned_text or raw_text, state)
                                    if intent:
                                        voice_ui_consumed, voice_ui_marks_pending = await _handle_voice_ui_intent(
                                            intent=intent,
                                            payload=intent_payload,
                                            runner=runner,
                                            websocket=websocket,
                                            session_id=session_id,
                                            user_id=user_id,
                                            live_queue=live_queue,
                                        )

                                queued_scene_follow_up = False
                                immediate_scene_replacement = False
                                if (
                                    actionable_child_text
                                    and input_finished
                                    and not voice_ui_consumed
                                    and pending_scene_render_mode
                                    and _child_requested_scene_refresh(cleaned_text or raw_text)
                                ):
                                    interrupt_mode = _classify_pending_scene_interrupt(cleaned_text or raw_text)
                                    if interrupt_mode == "queue":
                                        queued_scene_follow_up = True

                                        def _queue_scene_follow_up(s: dict[str, Any]) -> None:
                                            _queue_latest_scene_follow_up_request(s, cleaned_text or raw_text)

                                        await _mutate_state(runner, user_id, session_id, _queue_scene_follow_up)
                                        logger.info(
                                            "Queued latest child scene request after the current pending render for session %s: %s",
                                            session_id,
                                            (cleaned_text or raw_text)[:160],
                                        )
                                        voice_ui_consumed = True
                                    elif interrupt_mode == "replace":
                                        immediate_scene_replacement = True

                                        def _mark_scene_replacement_pending(s: dict[str, Any]) -> None:
                                            _arm_pending_scene_replacement(s, cleaned_text or raw_text)

                                        await _mutate_state(
                                            runner,
                                            user_id,
                                            session_id,
                                            _mark_scene_replacement_pending,
                                        )
                                        supersede_scene_render(session_id)
                                        if (
                                            _barge_in_enabled()
                                            and _should_activate_barge_in_for_pending_scene_replacement(session_id)
                                        ):
                                            _activate_barge_in(session_id)
                                        _publish_quick_ack(
                                            session_id,
                                            text="Okay, let's change it.",
                                            interrupt_audio=True,
                                        )
                                        if _should_reset_live_for_pending_scene_replacement(session_id):
                                            live_queue.reset(session_id, "pending_scene_replace")
                                        _send_pending_scene_replacement_prompt(
                                            session_id,
                                            live_queue,
                                            cleaned_text or raw_text,
                                        )
                                        # Start a fresh live turn for the spoken acknowledgment instead of
                                        # carrying forward the old scene turn's tool/output flags.
                                        child_utterance_this_turn = cleaned_text or raw_text
                                        last_child_utterance = cleaned_text or raw_text
                                        last_persisted_partial_child_utterance = cleaned_text or raw_text
                                        last_persisted_partial_child_utterance_finished = True
                                        scene_visuals_called_this_turn = False
                                        model_emitted_meaningful_output = False
                                        assistant_parts = []
                                        last_output_transcription = ""
                                        assistant_output_soft_closed = False
                                        assistant_output_hard_closed = False
                                        assistant_audio_closed_after_finish = False
                                        assistant_finished_utterances_this_turn = 0
                                        last_finished_assistant_output_at_epoch_ms = 0
                                        heard_humanish_speech_this_turn = True
                                        heard_noise_only_this_turn = False
                                        _audio_seen_this_turn.discard(session_id)
                                        deferred_opening_turn_complete = False
                                        _cancel_deferred_opening_finalize()
                                        _cancel_turn_output_seal()
                                        turn_output_sealed_until_child_input = False
                                        _turn_start_t = time.monotonic()
                                        logger.info(
                                            "Replacing still-pending scene render for session %s with newest child request: %s",
                                            session_id,
                                            (cleaned_text or raw_text)[:160],
                                        )
                                        voice_ui_consumed = True

                                if (
                                    actionable_child_text
                                    and not voice_ui_consumed
                                    and not story_started
                                    and bool(state.get("awaiting_story_choice", False))
                                ):
                                    def _mark_first_scene_choice(s: dict[str, Any]) -> None:
                                        s["story_started"] = True
                                        s["awaiting_story_choice"] = False
                                        s["pending_story_hint"] = cleaned_text or raw_text
                                        if str(s.get("camera_stage", "none")) in {"none", "pending", "prompted"}:
                                            s["camera_stage"] = "done"
                                            s["camera_skipped"] = True

                                    await _mutate_state(runner, user_id, session_id, _mark_first_scene_choice)
                                    story_started = True

                                if actionable_child_text and not voice_ui_consumed:
                                    # Priority 1: Full Name + Story Intent in one go
                                    detected_name = _extract_child_name(cleaned_text or raw_text)
                                    try:
                                        current_child_age = int(state.get("child_age", 4) or 4)
                                    except Exception:
                                        current_child_age = 4
                                    auto_confirm_name_story_shortcircuit = _should_auto_confirm_name_on_story_shortcircuit(
                                        cleaned_text or raw_text,
                                        detected_name,
                                        current_child_age,
                                    )
                                    if (not story_started and detected_name
                                        and _STORY_INTENT_RE.search(cleaned_text or "")
                                        and not bool(state.get("story_shortcircuit", False))):

                                        def _mark_name_story_sc(s: dict[str, Any]) -> None:
                                            # For younger children, be conservative unless the
                                            # child used an explicit self-introduction such as
                                            # "my name is Aaron" alongside the story request.
                                            if not auto_confirm_name_story_shortcircuit:
                                                s["pending_child_name"] = detected_name
                                                s["name_confirmed"] = False
                                                s["name_confirmation_prompted"] = False
                                                s["pending_story_hint"] = cleaned_text or raw_text
                                                s["camera_stage"] = "done"
                                                s["camera_skipped"] = True
                                                return
                                            s["child_name"] = detected_name
                                            s["pending_child_name"] = ""
                                            s["name_confirmed"] = True
                                            s["name_confirmation_prompted"] = False
                                            s["story_started"] = True
                                            s["story_shortcircuit"] = True
                                            s["pending_story_hint"] = cleaned_text or raw_text
                                            s["camera_stage"] = "done"
                                            s["camera_skipped"] = True

                                        await _mutate_state(runner, user_id, session_id, _mark_name_story_sc)

                                    # Priority 2: Generic Story Intent (Short-circuit name flow)
                                    elif (not story_started and _env_enabled("ENABLE_STORY_SHORTCIRCUIT", default=True)
                                          and _should_shortcircuit_story(cleaned_text or raw_text, state)
                                          and not bool(state.get("story_shortcircuit", False))):

                                        def _mark_story_sc(s: dict[str, Any]) -> None:
                                            s["name_confirmed"] = True
                                            s["story_started"] = True
                                            s["story_shortcircuit"] = True
                                            s["pending_story_hint"] = cleaned_text or raw_text
                                            s["camera_stage"] = "done"
                                            s["camera_skipped"] = True

                                        await _mutate_state(runner, user_id, session_id, _mark_story_sc)

                                    # Priority 3: Story Intent during Camera flow
                                    elif (not story_started and _env_enabled("ENABLE_STORY_SHORTCIRCUIT", default=True)
                                          and _should_skip_camera_for_story(cleaned_text or raw_text, state)
                                          and not bool(state.get("camera_story_shortcircuit", False))):

                                        def _mark_camera_sc(s: dict[str, Any]) -> None:
                                            s["camera_stage"] = "done"
                                            s["camera_skipped"] = True
                                            s["camera_story_shortcircuit"] = True
                                            s["pending_story_hint"] = cleaned_text or raw_text

                                        await _mutate_state(runner, user_id, session_id, _mark_camera_sc)

                                    # If no short-circuit matched, fall back to normal onboarding logic
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
                                if (
                                    actionable_child_text
                                    and not queued_scene_follow_up
                                    and not immediate_scene_replacement
                                    and (voice_ui_marks_pending or not voice_ui_consumed)
                                ):
                                    def _mark_pending(s: dict[str, Any]) -> None:
                                        s["pending_response"] = True
                                        s["pending_response_interrupted"] = False
                                        s["scene_tool_turn_open"] = True
                                        s["pending_response_token"] = uuid.uuid4().hex
                                        s["last_child_utterance"] = cleaned_text or raw_text
                                        s["partial_child_utterance"] = cleaned_text or raw_text
                                        s["partial_child_utterance_finished"] = True
                                        _capture_child_story_continuity(s, cleaned_text or raw_text)
                                    await _mutate_state(runner, user_id, session_id, _mark_pending)

                            if input_finished and not voice_ui_consumed and not ignored_finished_transcription:
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
                        if session_id in _ending_story_flush_sessions:
                            continue
                        if assistant_output_soft_closed or assistant_output_hard_closed:
                            if bool(getattr(event.output_transcription, "finished", False)):
                                _assistant_speaking_sessions.discard(session_id)
                                _assistant_speaking_since.pop(session_id, None)
                            continue
                        out_text = event.output_transcription.text
                        cleaned_out = _CTRL_TOKEN_RE.sub("", out_text or "").strip()
                        out_finished = bool(getattr(event.output_transcription, "finished", False))
                        if _is_meaningful_text(cleaned_out):
                                pending_ack_state = {}
                                storage_session = _get_storage_session(runner, user_id, session_id)
                                if storage_session is not None and isinstance(storage_session.state, dict):
                                    pending_ack_state = dict(storage_session.state)
                                if (
                                    _should_hold_scene_tools_for_pending_replacement_ack(pending_ack_state)
                                    and _should_abort_partial_pending_scene_replacement_ack(
                                        pending_ack_state,
                                        cleaned_out,
                                    )
                                ):
                                    logger.info(
                                        "Ignoring invalid partial assistant utterance while waiting for pending replacement acknowledgment in session %s",
                                        session_id,
                                    )
                                    replacement_retry_text = str(
                                        pending_ack_state.get("pending_scene_replacement_text", "") or ""
                                    ).strip()
                                    if replacement_retry_text:
                                        live_queue.reset(session_id, "pending_scene_replacement_invalid_ack")
                                        _send_pending_scene_replacement_prompt(
                                            session_id,
                                            live_queue,
                                            replacement_retry_text,
                                        )
                                    _assistant_speaking_sessions.discard(session_id)
                                    _assistant_speaking_since.pop(session_id, None)
                                    continue
                                if out_finished:
                                    if (
                                        _should_hold_scene_tools_for_pending_replacement_ack(pending_ack_state)
                                        and not _is_valid_pending_scene_replacement_ack_text(
                                            pending_ack_state,
                                            cleaned_out,
                                        )
                                    ):
                                        logger.info(
                                            "Ignoring non-ack assistant utterance while waiting for pending replacement acknowledgment in session %s",
                                            session_id,
                                        )
                                        replacement_retry_text = str(
                                            pending_ack_state.get("pending_scene_replacement_text", "") or ""
                                        ).strip()
                                        if replacement_retry_text:
                                            live_queue.reset(session_id, "pending_scene_replacement_invalid_ack")
                                            _send_pending_scene_replacement_prompt(
                                                session_id,
                                                live_queue,
                                                replacement_retry_text,
                                        )
                                        _assistant_speaking_sessions.discard(session_id)
                                        _assistant_speaking_since.pop(session_id, None)
                                        continue
                                    if _should_hold_scene_tools_for_pending_replacement_ack(pending_ack_state):
                                        replacement_ack_text = str(
                                            pending_ack_state.get("pending_scene_replacement_text", "") or ""
                                        ).strip()
                                        if replacement_ack_text:
                                            def _mark_pending_scene_replacement_ack_heard(s: dict[str, Any]) -> None:
                                                if (
                                                    str(s.get("pending_scene_replacement_text", "") or "").strip()
                                                    == replacement_ack_text
                                                    and _pending_scene_replacement_phase(s)
                                                    == _PENDING_SCENE_REPLACEMENT_PHASE_ACK
                                                ):
                                                    s["pending_scene_replacement_phase"] = (
                                                        _PENDING_SCENE_REPLACEMENT_PHASE_RENDER
                                                    )

                                            await _mutate_state(
                                                runner=runner,
                                                user_id=user_id,
                                                session_id=session_id,
                                                mutator=_mark_pending_scene_replacement_ack_heard,
                                            )
                                            pending_ack_state["pending_scene_replacement_phase"] = (
                                                _PENDING_SCENE_REPLACEMENT_PHASE_RENDER
                                            )
                                            logger.info(
                                                "Accepted pending replacement acknowledgment for session %s",
                                                session_id,
                                            )
                                if turn_output_sealed_until_child_input and session_id not in _ending_story_sessions:
                                    if out_finished:
                                        _assistant_speaking_sessions.discard(session_id)
                                        _assistant_speaking_since.pop(session_id, None)
                                    logger.info(
                                        "Dropping assistant narration after turn completion for session %s",
                                        session_id,
                                    )
                                    continue
                                if assistant_audio_closed_after_finish and session_id not in _ending_story_sessions:
                                    if out_finished:
                                        _assistant_speaking_sessions.discard(session_id)
                                        _assistant_speaking_since.pop(session_id, None)
                                    logger.info(
                                        "Dropping assistant transcription after the first finished utterance for session %s",
                                        session_id,
                                    )
                                    continue
                                if scene_narration_guard_active and session_id not in _ending_story_sessions:
                                    if out_finished:
                                        _assistant_speaking_sessions.discard(session_id)
                                        _assistant_speaking_since.pop(session_id, None)
                                    logger.info(
                                        "Dropping assistant narration after the scene turn already resolved for session %s",
                                        session_id,
                                    )
                                    continue
                                if out_finished and assistant_finished_utterances_this_turn >= 1:
                                    logger.info(
                                        "Dropping extra finished assistant utterance in the same turn for session %s",
                                        session_id,
                                    )
                                    assistant_output_soft_closed = True
                                    assistant_output_hard_closed = True
                                    _assistant_speaking_sessions.discard(session_id)
                                    _assistant_speaking_since.pop(session_id, None)
                                    continue
                                model_emitted_meaningful_output = True
                                last_output_transcription = cleaned_out
                                if session_id in _interrupted_turn_sessions:
                                    if out_finished:
                                        _assistant_speaking_sessions.discard(session_id)
                                        _assistant_speaking_since.pop(session_id, None)
                                    continue
                                if out_finished:
                                    if scene_visuals_called_this_turn and assistant_finished_utterances_this_turn >= 1:
                                        logger.info(
                                            "Dropping duplicate assistant scene narration in the same turn for session %s",
                                            session_id,
                                        )
                                        assistant_output_soft_closed = True
                                        _assistant_speaking_sessions.discard(session_id)
                                        _assistant_speaking_since.pop(session_id, None)
                                        continue
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
                                    last_finished_assistant_output_at_epoch_ms = int(time.time() * 1000)
                                # Broadcast agent transcription (Iter 11)
                                await websocket.send_text(
                                    ServerEvent(
                                        type=ServerEventType.AGENT_TRANSCRIPTION,
                                        payload={"text": last_output_transcription, "finished": out_finished}
                                    ).model_dump_json()
                                )
                                if out_finished:
                                    assistant_finished_utterances_this_turn += 1
                                    assistant_parts.append(cleaned_out)
                                    if _assistant_turn_soft_closes(cleaned_out):
                                        assistant_output_soft_closed = True
                                    assistant_audio_closed_after_finish = True
                                    if scene_visuals_called_this_turn:
                                        # Scene turns should resolve to one Amelia answer.
                                        # Any later continuation in the same Live turn is stale.
                                        scene_narration_guard_active = True
                                    if deferred_opening_turn_complete:
                                        _schedule_deferred_opening_finalize()

                    if getattr(event, "turn_complete", False):
                        if session_id in _ending_story_flush_sessions:
                            def _clear_pending_after_end(s: dict[str, Any]) -> None:
                                s["pending_response"] = False
                                s["pending_response_interrupted"] = False
                                s["scene_tool_turn_open"] = False
                                s["assembly_wait_last_child_utterance"] = ""
                                s["partial_child_utterance"] = ""
                                s["partial_child_utterance_finished"] = False
                            await _mutate_state(runner, user_id, session_id, _clear_pending_after_end)
                            _ending_story_flush_sessions.discard(session_id)
                            if session_id not in _assembly_intro_sent_sessions:
                                intro_activity: dict[str, str] = {"key": ""}

                                def _mark_intro_activity(s: dict[str, Any]) -> None:
                                    intro_activity["key"] = _select_and_record_assembly_wait_activity(
                                        s,
                                        intro=True,
                                    )

                                await _mutate_state(runner, user_id, session_id, _mark_intro_activity)
                                storage_session = _get_storage_session(runner, user_id, session_id)
                                _send_assembly_wait_prompt(
                                    session_id=session_id,
                                    live_queue=live_queue,
                                    state=dict(storage_session.state) if storage_session and storage_session.state else {},
                                    activity=intro_activity["key"],
                                    intro=True,
                                )
                                _assembly_intro_sent_sessions.add(session_id)
                            assistant_parts = []
                            last_output_transcription = ""
                            child_utterance_this_turn = ""
                            last_persisted_partial_child_utterance = ""
                            last_persisted_partial_child_utterance_finished = False
                            child_turn_attempted_this_turn = False
                            child_turn_answered_this_turn = False
                            scene_visuals_called_this_turn = False
                            model_emitted_meaningful_output = False
                            assistant_output_soft_closed = False
                            assistant_output_hard_closed = False
                            assistant_audio_closed_after_finish = False
                            assistant_finished_utterances_this_turn = 0
                            heard_humanish_speech_this_turn = False
                            heard_noise_only_this_turn = False
                            _audio_seen_this_turn.discard(session_id)
                            _assistant_speaking_sessions.discard(session_id)
                            _assistant_speaking_since.pop(session_id, None)
                            turn_output_sealed_until_child_input = True
                            await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
                            _turn_start_t = time.monotonic()
                            continue
                        if session_id in _interrupted_turn_sessions:
                            storage_session = _get_storage_session(runner, user_id, session_id)
                            interrupted_state = (
                                dict(storage_session.state)
                                if storage_session is not None and isinstance(storage_session.state, dict)
                                else {}
                            )
                            if _should_keep_live_open_for_pending_scene_replacement_follow_up(interrupted_state):
                                logger.info(
                                    "Clearing interrupted assistant turn but keeping live output open for pending scene replacement in session %s",
                                    session_id,
                                )
                                _interrupted_turn_sessions.discard(session_id)
                                assistant_parts = []
                                last_output_transcription = ""
                                scene_visuals_called_this_turn = False
                                model_emitted_meaningful_output = False
                                assistant_output_soft_closed = False
                                assistant_output_hard_closed = False
                                assistant_audio_closed_after_finish = False
                                assistant_finished_utterances_this_turn = 0
                                silent_recovery_attempts = 0
                                _assistant_speaking_sessions.discard(session_id)
                                _assistant_speaking_since.pop(session_id, None)
                                _awaiting_greeting_sessions.discard(session_id)
                                _cancel_turn_output_seal()
                                turn_output_sealed_until_child_input = False
                                _turn_start_t = time.monotonic()
                                if not _is_meaningful_text(child_utterance_this_turn):
                                    child_utterance_this_turn = ""
                                last_persisted_partial_child_utterance = ""
                                last_persisted_partial_child_utterance_finished = False
                                child_turn_attempted_this_turn = False
                                child_turn_answered_this_turn = False
                                heard_humanish_speech_this_turn = False
                                heard_noise_only_this_turn = False
                                _audio_seen_this_turn.discard(session_id)
                                continue

                            logger.info("Discarding interrupted assistant turn for session %s", session_id)
                            _interrupted_turn_sessions.discard(session_id)
                            assistant_parts = []
                            last_output_transcription = ""
                            scene_visuals_called_this_turn = False
                            model_emitted_meaningful_output = False
                            assistant_output_soft_closed = False
                            assistant_output_hard_closed = False
                            assistant_audio_closed_after_finish = False
                            assistant_finished_utterances_this_turn = 0
                            silent_recovery_attempts = 0
                            _assistant_speaking_sessions.discard(session_id)
                            _assistant_speaking_since.pop(session_id, None)
                            _awaiting_greeting_sessions.discard(session_id)
                            _turn_start_t = time.monotonic()
                            turn_output_sealed_until_child_input = True
                            await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
                            if not _is_meaningful_text(child_utterance_this_turn):
                                child_utterance_this_turn = ""
                            last_persisted_partial_child_utterance = ""
                            last_persisted_partial_child_utterance_finished = False
                            child_turn_attempted_this_turn = False
                            child_turn_answered_this_turn = False
                            heard_humanish_speech_this_turn = False
                            heard_noise_only_this_turn = False
                            _audio_seen_this_turn.discard(session_id)
                            continue

                        _tc_t = time.monotonic()
                        _tc_delta = int((_tc_t - _turn_start_t) * 1000)

                        storage_session = _get_storage_session(runner, user_id, session_id)
                        turn_complete_state = (
                            dict(storage_session.state)
                            if storage_session is not None and isinstance(storage_session.state, dict)
                            else {}
                        )
                        if _should_ignore_turn_complete_while_waiting_for_pending_replacement_ack(
                            turn_complete_state,
                            last_finished_assistant_output_at_epoch_ms=last_finished_assistant_output_at_epoch_ms,
                        ):
                            logger.info(
                                "Ignoring stale turn_complete while waiting for pending replacement acknowledgment in session %s",
                                session_id,
                            )
                            assistant_parts = []
                            last_output_transcription = ""
                            scene_visuals_called_this_turn = False
                            model_emitted_meaningful_output = False
                            assistant_output_soft_closed = False
                            assistant_output_hard_closed = False
                            assistant_audio_closed_after_finish = False
                            assistant_finished_utterances_this_turn = 0
                            silent_recovery_attempts = 0
                            _assistant_speaking_sessions.discard(session_id)
                            _assistant_speaking_since.pop(session_id, None)
                            _cancel_turn_output_seal()
                            turn_output_sealed_until_child_input = False
                            _turn_start_t = time.monotonic()
                            continue

                        # Turn is done, no longer pending a response.
                        def _clear_pending(s: dict[str, Any]) -> None:
                            s["pending_response"] = False
                            s["pending_response_interrupted"] = False
                            s["scene_tool_turn_open"] = False
                            s["assembly_wait_last_child_utterance"] = ""
                            s["partial_child_utterance"] = ""
                            s["partial_child_utterance_finished"] = False
                        await _mutate_state(runner, user_id, session_id, _clear_pending)
                        assistant_text = " ".join(assistant_parts).strip()
                        assistant_parts = []
                        if not assistant_text and _is_meaningful_text(last_output_transcription):
                            assistant_text = last_output_transcription
                        last_output_transcription = ""

                        had_child_input_this_turn = _is_meaningful_text(child_utterance_this_turn)
                        if (
                            not had_child_input_this_turn
                            and not _is_meaningful_text(assistant_text)
                            and not scene_visuals_called_this_turn
                            and (session_id in _awaiting_greeting_sessions or session_id in _opening_phase_sessions)
                        ):
                            if not deferred_opening_turn_complete:
                                logger.info(
                                    "Deferring opening-phase turn completion briefly for session %s to allow late greeting narration.",
                                    session_id,
                                )
                            deferred_opening_turn_complete = True
                            _schedule_deferred_opening_finalize()
                            continue

                        if session_id in _ending_story_sessions:
                            assembly_state_snapshot: dict[str, Any] = {}

                            def _capture_assembly_state(state: dict[str, Any]) -> None:
                                nonlocal assembly_state_snapshot
                                state["pending_response"] = False
                                state["pending_response_interrupted"] = False
                                state["scene_tool_turn_open"] = False
                                state["assembly_wait_last_child_utterance"] = ""
                                state["partial_child_utterance"] = ""
                                state["partial_child_utterance_finished"] = False
                                assembly_state_snapshot = dict(state)

                            await _mutate_state(
                                runner=runner,
                                user_id=user_id,
                                session_id=session_id,
                                mutator=_capture_assembly_state,
                            )
                            cache_storybook_state(session_id, assembly_state_snapshot)
                            if model_emitted_meaningful_output:
                                silent_recovery_attempts = 0
                            else:
                                had_child_input_this_turn = _is_meaningful_text(child_utterance_this_turn)
                                if had_child_input_this_turn and silent_recovery_attempts < 2:
                                    silent_recovery_attempts += 1
                                    retry_activity: dict[str, str] = {"key": ""}

                                    def _mark_retry_activity(s: dict[str, Any]) -> None:
                                        retry_activity["key"] = _select_and_record_assembly_wait_activity(
                                            s,
                                            child_utterance=last_child_utterance or child_utterance_this_turn,
                                            retry=True,
                                        )

                                    await _mutate_state(
                                        runner=runner,
                                        user_id=user_id,
                                        session_id=session_id,
                                        mutator=_mark_retry_activity,
                                    )
                                    raise _LiveTurnRecoveryRequested("assembly_wait_silent_turn")
                            assistant_parts = []
                            last_output_transcription = ""
                            child_utterance_this_turn = ""
                            last_persisted_partial_child_utterance = ""
                            last_persisted_partial_child_utterance_finished = False
                            child_turn_attempted_this_turn = False
                            child_turn_answered_this_turn = False
                            scene_visuals_called_this_turn = False
                            model_emitted_meaningful_output = False
                            assistant_output_soft_closed = False
                            assistant_output_hard_closed = False
                            assistant_audio_closed_after_finish = False
                            heard_humanish_speech_this_turn = False
                            heard_noise_only_this_turn = False
                            _audio_seen_this_turn.discard(session_id)
                            _assistant_speaking_sessions.discard(session_id)
                            _assistant_speaking_since.pop(session_id, None)
                            turn_output_sealed_until_child_input = True
                            await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
                            _turn_start_t = time.monotonic()
                            continue

                        completed_pending_scene_replacement_ack_turn = False
                        if model_emitted_meaningful_output or scene_visuals_called_this_turn:
                            deferred_opening_turn_complete = False
                            _cancel_deferred_opening_finalize()
                            silent_recovery_attempts = 0
                            if child_turn_attempted_this_turn and not child_turn_answered_this_turn:
                                child_turn_answered_this_turn = True
                                _bump_live_telemetry("child_turn.answered")
                            turn_limit_reached = False
                            completed_state_snapshot: dict[str, Any] = {}

                            def _on_turn_complete(state: dict[str, Any]) -> None:
                                nonlocal turn_limit_reached
                                nonlocal completed_turn_number
                                nonlocal completed_response_turn_number
                                nonlocal completed_name_confirmed
                                nonlocal completed_camera_stage
                                nonlocal completed_story_turn_limit
                                nonlocal completed_story_started
                                nonlocal completed_toy_share_active
                                nonlocal completed_scene_chat_turn
                                nonlocal completed_story_page_turn
                                nonlocal completed_scene_render_skipped
                                nonlocal completed_state_snapshot
                                nonlocal completed_pending_scene_replacement_ack_turn
                                nonlocal toy_share_finished_now
                                toy_share_finished_now = False
                                _take_snapshot(state)
                                state["pending_response"] = False
                                state["pending_response_interrupted"] = False
                                state["assembly_wait_last_child_utterance"] = ""
                                state["partial_child_utterance"] = ""
                                state["partial_child_utterance_finished"] = False
                                pending_replacement_ack_turn = _is_pending_scene_replacement_follow_up_turn(
                                    state,
                                    scene_visuals_called_this_turn=scene_visuals_called_this_turn,
                                )
                                if state.get("name_confirmed") or int(state.get("response_turn_number", state.get("turn_number", 1))) >= 3:
                                    _opening_phase_sessions.discard(session_id)
                                else:
                                    _opening_phase_sessions.add(session_id)
                                if not pending_replacement_ack_turn:
                                    _append_story_summary(state, assistant_text)
                                # If a pending name exists and the child just affirmed or repeated it,
                                # lock in the name to prevent repeated confirmations.
                                pending_name = str(state.get("pending_child_name", "")).strip()
                                if not pending_replacement_ack_turn and pending_name:
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
                                if (
                                    not pending_replacement_ack_turn
                                    and state.get("name_confirmed")
                                    and str(state.get("child_name", "friend")).strip().lower() == "friend"
                                ):
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
                                    not pending_replacement_ack_turn
                                    and assistant_text
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
                                    not pending_replacement_ack_turn
                                    and assistant_text
                                    and bool(state.get("name_confirmed", False))
                                    and not bool(state.get("story_started", False))
                                    and _STORY_PROMPT_RE.search(assistant_text)
                                ):
                                    state["awaiting_story_choice"] = True
                                elif not pending_replacement_ack_turn and bool(state.get("story_started", False)):
                                    state["awaiting_story_choice"] = False
                                if (
                                    not pending_replacement_ack_turn
                                    and
                                    str(state.get("camera_stage", "none")) == "pending"
                                    and _CAMERA_PROMPT_RE.search(assistant_text or "")
                                ):
                                    state["camera_stage"] = "prompted"
                                if not pending_replacement_ack_turn and _CAMERA_PROMPT_RE.search(assistant_text or ""):
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
                                _sync_story_page_progress_fields(state)
                                try:
                                    current_response_turn = int(state.get("response_turn_number", state.get("turn_number", 1)) or 1)
                                except Exception:
                                    current_response_turn = 1
                                current_response_turn = max(current_response_turn, 1)
                                try:
                                    toy_share_turns_remaining = int(state.get("toy_share_turns_remaining", 0) or 0)
                                except Exception:
                                    toy_share_turns_remaining = 0
                                toy_share_active_before = bool(state.get("toy_share_active", False)) and toy_share_turns_remaining > 0
                                fallback_scene_turn = (
                                    not pending_replacement_ack_turn
                                    and
                                    not scene_visuals_called_this_turn
                                    and bool(state.get("story_started", False))
                                    and str(state.get("camera_stage", "none")) not in {"pending", "prompted"}
                                    and (bool(state.get("name_confirmed", False)) or current_response_turn >= 3)
                                    and not toy_share_active_before
                                    and _should_trigger_fallback_scene(
                                        assistant_text=assistant_text,
                                        child_text=last_child_utterance,
                                        state=state,
                                    )
                                )
                                scene_render_skipped_turn = bool(
                                    scene_visuals_called_this_turn and state.get("scene_render_skipped", False)
                                )
                                scene_chat_turn = (
                                    not pending_replacement_ack_turn
                                    and
                                    bool(state.get("story_started", False))
                                    and _has_rendered_scene(state)
                                    and (
                                        not scene_visuals_called_this_turn
                                        or scene_render_skipped_turn
                                    )
                                    and not fallback_scene_turn
                                    and _child_requested_scene_chat(child_utterance_this_turn)
                                )
                                story_page_turn = bool(
                                    not pending_replacement_ack_turn
                                    and (
                                        fallback_scene_turn
                                        or (scene_visuals_called_this_turn and not scene_render_skipped_turn)
                                    )
                                )
                                current_turn = int(state.get("turn_number", 1))
                                try:
                                    current_story_page_count = int(state.get("story_page_count", 0) or 0)
                                except Exception:
                                    current_story_page_count = 0
                                turn_limit_reached = False if (toy_share_active_before or not story_page_turn) else current_story_page_count >= max_turns
                                state["response_turn_number"] = current_response_turn + 1
                                completed_pending_scene_replacement_ack_turn = pending_replacement_ack_turn
                                if toy_share_active_before:
                                    state["toy_share_turns_remaining"] = max(toy_share_turns_remaining - 1, 0)
                                    state["toy_share_active"] = bool(state["toy_share_turns_remaining"])
                                    if not state["toy_share_active"]:
                                        toy_share_finished_now = True
                                    state["story_turn_limit_reached"] = False
                                    state["scene_render_pending"] = False
                                    _clear_pending_scene_request_metadata(state)
                                elif not story_page_turn:
                                    state["story_turn_limit_reached"] = False
                                    if pending_replacement_ack_turn:
                                        state["scene_render_pending"] = True
                                        state["pending_scene_replacement_phase"] = _PENDING_SCENE_REPLACEMENT_PHASE_RENDER
                                    elif _should_preserve_pending_scene_render(session_id, state):
                                        state["scene_render_pending"] = True
                                    else:
                                        state["scene_render_pending"] = False
                                        _clear_pending_scene_request_metadata(state)
                                else:
                                    state["story_started"] = True
                                    state["awaiting_story_choice"] = False
                                    state["pending_story_hint"] = ""
                                    state["turn_number"] = min(current_turn + 1, max_turns)
                                    state["story_turn_limit_reached"] = turn_limit_reached
                                    state["scene_render_pending"] = bool(
                                        fallback_scene_turn
                                        or (
                                            scene_visuals_called_this_turn
                                            and _scene_render_still_in_flight_after_tool_call(session_id, state)
                                        )
                                    )
                                    if not bool(state.get("scene_render_pending", False)):
                                        _clear_pending_scene_request_metadata(state)
                                    _clear_toy_share_resume_state(state)
                                _sync_story_page_progress_fields(state)
                                completed_turn_number = current_turn
                                completed_response_turn_number = current_response_turn
                                completed_name_confirmed = bool(state.get("name_confirmed", False))
                                completed_camera_stage = str(state.get("camera_stage", "none"))
                                completed_story_turn_limit = bool(
                                    state.get("story_turn_limit_reached", False)
                                    or state.get("story_page_limit_reached", False)
                                )
                                completed_story_started = bool(state.get("story_started", False))
                                completed_toy_share_active = toy_share_active_before
                                completed_scene_chat_turn = scene_chat_turn
                                completed_story_page_turn = story_page_turn
                                completed_scene_render_skipped = scene_render_skipped_turn
                                if (
                                    state.get("response_turn_number", state.get("turn_number", 1)) >= 3
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
                                _sync_story_phase(session_id, state)
                                completed_state_snapshot = dict(state)

                            await _mutate_state(
                                runner=runner,
                                user_id=user_id,
                                session_id=session_id,
                                mutator=_on_turn_complete,
                            )
                            if completed_story_page_turn:
                                storage_session = _get_storage_session(runner, user_id, session_id)
                                session_event_count = 0
                                if storage_session is not None:
                                    try:
                                        session_event_count = len(storage_session.events)
                                    except Exception:
                                        session_event_count = 0
                                await _mutate_state(
                                    runner=runner,
                                    user_id=user_id,
                                    session_id=session_id,
                                    mutator=lambda state: _record_scene_branch_point(
                                        state,
                                        snapshot_state=completed_state_snapshot,
                                        session_event_count=session_event_count,
                                    ),
                                )
                                refreshed_session = _get_storage_session(runner, user_id, session_id)
                                if refreshed_session is not None:
                                    completed_state_snapshot = dict(refreshed_session.state)
                            cache_storybook_state(session_id, completed_state_snapshot)
                            _update_storybook_firestore(
                                session_id,
                                {
                                    "story_pages": _story_pages_payload(completed_state_snapshot),
                                },
                            )
                            if toy_share_finished_now:
                                _publish_ui_command(
                                    session_id,
                                    "close_toy_share",
                                    reason="toy_share_complete",
                                    source="system",
                                )

                            # ── IMAGE SYNC: log whether image arrived before or after turn_complete ──
                            if scene_visuals_called_this_turn:
                                _img_wait_evt = _pending_image_events.get(session_id)
                                _image_already_arrived = _img_wait_evt is not None and _img_wait_evt.is_set()
                                _pending_image_events.pop(session_id, None)
                                if completed_scene_render_skipped:
                                    logger.info(
                                        "⏱️ SYNC [ws] scene render skipped — no image expected | session=%s",
                                        session_id,
                                    )
                                elif _image_already_arrived:
                                    _sync_ms = int((time.monotonic() - _tc_t) * 1000)
                                    logger.info("⏱️ SYNC [ws] image already arrived before turn_complete | sync_ms=%d | session=%s", _sync_ms, session_id)
                                else:
                                    # Image is still generating — it will arrive shortly and be sent
                                    # to the frontend automatically by _forward_session_events.
                                    # Do NOT send keep-narrating — it creates a new model turn which
                                    # resets scene_visuals_called_this_turn and causes a second image gen.
                                    logger.info("⏱️ SYNC [ws] image still generating at turn_complete — will arrive shortly | session=%s", session_id)

                            queued_follow_up_after_turn = ""
                            if not completed_story_turn_limit:
                                def _arm_follow_up_after_turn(state: dict[str, Any]) -> None:
                                    nonlocal queued_follow_up_after_turn
                                    if bool(state.get("scene_render_pending", False)):
                                        return
                                    queued_follow_up_after_turn = _arm_queued_scene_follow_up_after_render(state)

                                await _mutate_state(
                                    runner=runner,
                                    user_id=user_id,
                                    session_id=session_id,
                                    mutator=_arm_follow_up_after_turn,
                                )
                            if queued_follow_up_after_turn:
                                logger.info(
                                    "Replaying queued child scene request after turn complete for session %s: %s",
                                    session_id,
                                    queued_follow_up_after_turn[:160],
                                )
                                _send_queued_scene_follow_up_prompt(
                                    session_id,
                                    live_queue,
                                    queued_follow_up_after_turn,
                                )

                            pending_replacement_after_turn = str(
                                completed_state_snapshot.get("pending_scene_replacement_text", "") or ""
                            ).strip()
                            pending_replacement_phase_after_turn = _pending_scene_replacement_phase(
                                completed_state_snapshot
                            )
                            if (
                                pending_replacement_after_turn
                                and pending_replacement_phase_after_turn == _PENDING_SCENE_REPLACEMENT_PHASE_RENDER
                                and not scene_visuals_called_this_turn
                                and session_id not in _early_fallback_started
                            ):
                                await _trigger_pending_scene_replacement_scene(
                                    session_id=session_id,
                                    replacement_text=pending_replacement_after_turn,
                                    runner=runner,
                                    websocket=websocket,
                                    user_id=user_id,
                                )
                                scene_visuals_called_this_turn = True
                                _early_fallback_started.add(session_id)

                            # ── TURN COMPLETE FALLBACK: if no tool call seen at ALL ──
                            if (
                                not scene_visuals_called_this_turn
                                and session_id not in _early_fallback_started
                                and session_id not in _pending_image_events
                                and not pending_replacement_after_turn
                            ):
                                if (
                                    completed_story_started
                                    and completed_camera_stage not in {"pending", "prompted"}
                                    and (completed_name_confirmed or completed_response_turn_number >= 3)
                                    and not completed_toy_share_active
                                    and _should_trigger_fallback_scene(
                                        assistant_text=assistant_text,
                                        child_text=last_child_utterance,
                                        state=completed_state_snapshot,
                                    )
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

                            if completed_story_turn_limit:
                                _story_turn_limit_sessions.add(session_id)
                                # Safety fallback: if the model fails to call assemble_story_video,
                                # trigger it once we hit the final turn (cloud mode only).
                                if not _env_enabled("LOCAL_STORYBOOK_MODE", default=False):
                                    try:
                                        story_title, child_name = _storybook_identity_from_state(completed_state_snapshot)
                                        await _announce_storybook_assembly_started(
                                            websocket=websocket,
                                            session_id=session_id,
                                            eta_seconds=25 if _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False) else 90,
                                            story_title=story_title,
                                            child_name=child_name,
                                            started_at_epoch_ms=_assembly_started_at_epoch_ms_from_state(completed_state_snapshot),
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
                            heard_humanish_speech_this_turn = False
                            heard_noise_only_this_turn = False
                            _turn_start_t = time.monotonic()  # reset turn timer
                        else:
                            logger.warning(
                                "Silent model turn detected for session %s (child_input=%s)",
                                session_id,
                                had_child_input_this_turn,
                            )
                            if had_child_input_this_turn and child_turn_attempted_this_turn and not child_turn_answered_this_turn:
                                _bump_live_telemetry("child_turn.lost")
                                _emit_live_telemetry(
                                    "child_turn_lost",
                                    session_id=session_id,
                                    include_runtime=False,
                                    reason="silent_model_turn",
                                )

                            storage_session = _get_storage_session(runner, user_id, session_id)
                            silent_turn_state = (
                                dict(storage_session.state)
                                if storage_session is not None and isinstance(storage_session.state, dict)
                                else {}
                            )
                            if _should_retry_pending_scene_replacement_ack(
                                silent_turn_state,
                                had_child_input_this_turn=had_child_input_this_turn,
                                model_emitted_meaningful_output=model_emitted_meaningful_output,
                                scene_visuals_called_this_turn=scene_visuals_called_this_turn,
                            ):
                                replacement_retry_text = str(
                                    silent_turn_state.get("pending_scene_replacement_text", "") or ""
                                ).strip()
                                if silent_recovery_attempts < 2 and replacement_retry_text:
                                    silent_recovery_attempts += 1
                                    live_queue.reset(session_id, "pending_scene_replacement_ack_retry")
                                    _send_pending_scene_replacement_prompt(
                                        session_id,
                                        live_queue,
                                        replacement_retry_text,
                                    )
                                    assistant_parts = []
                                    last_output_transcription = ""
                                    child_utterance_this_turn = ""
                                    last_persisted_partial_child_utterance = ""
                                    last_persisted_partial_child_utterance_finished = False
                                    child_turn_attempted_this_turn = False
                                    child_turn_answered_this_turn = False
                                    scene_visuals_called_this_turn = False
                                    model_emitted_meaningful_output = False
                                    assistant_output_soft_closed = False
                                    assistant_output_hard_closed = False
                                    assistant_audio_closed_after_finish = False
                                    assistant_finished_utterances_this_turn = 0
                                    heard_humanish_speech_this_turn = False
                                    heard_noise_only_this_turn = False
                                    _audio_seen_this_turn.discard(session_id)
                                    _cancel_turn_output_seal()
                                    turn_output_sealed_until_child_input = False
                                    _turn_start_t = time.monotonic()
                                    continue

                            if session_id in _awaiting_greeting_sessions:
                                # If the greeting didn't land, resend it instead of asking the child to repeat.
                                if silent_recovery_attempts < 2:
                                    silent_recovery_attempts += 1
                                    greeting_prompt = (
                                        "A child just joined. Greet them with very short, simple sentences for a 4-year-old. "
                                        "Ask their name, and say they can also tell you what story they want."
                                    )
                                    live_queue.reset(session_id, "silent_greeting_retry")
                                    _send_live_content(session_id, live_queue, greeting_prompt)
                                else:
                                    _awaiting_greeting_sessions.discard(session_id)
                            elif session_id in _opening_phase_sessions:
                                # During the opening, ignore noise and re-offer the greeting instead of
                                # triggering a generic "repeat yourself" message.
                                storage_session = _get_storage_session(runner, user_id, session_id)
                                opening_state = (
                                    dict(storage_session.state)
                                    if storage_session is not None and isinstance(storage_session.state, dict)
                                    else {}
                                )
                                if (
                                    _opening_phase_needs_first_greeting(opening_state)
                                    and silent_recovery_attempts < 2
                                ):
                                    silent_recovery_attempts += 1
                                    greeting_prompt = (
                                        "You're still in the opening. Repeat the greeting in very short, simple sentences. "
                                        "Ask their name, and say they can also tell you what story they want."
                                    )
                                    live_queue.reset(session_id, "opening_greeting_retry")
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
                                    def _re_mark_pending_after_silent_turn(s: dict[str, Any]) -> None:
                                        s["pending_response"] = True
                                        s["pending_response_interrupted"] = False
                                        s["scene_tool_turn_open"] = True
                                        s["pending_response_token"] = uuid.uuid4().hex
                                        s["last_child_utterance"] = last_child_utterance
                                        s["partial_child_utterance"] = last_child_utterance
                                        s["partial_child_utterance_finished"] = True

                                    await _mutate_state(
                                        runner=runner,
                                        user_id=user_id,
                                        session_id=session_id,
                                        mutator=_re_mark_pending_after_silent_turn,
                                    )
                                    raise _LiveTurnRecoveryRequested("silent_turn_repair")
                            elif heard_humanish_speech_this_turn:
                                await websocket.send_text(
                                    ServerEvent(
                                        type=ServerEventType.ERROR,
                                        payload={"message": "I almost heard you. Can you tell me again?"},
                                    ).model_dump_json()
                                )

                                if silent_recovery_attempts < 2:
                                    silent_recovery_attempts += 1
                                    repair_prompt = (
                                        "The child likely tried to speak, but the words were too unclear to trust. "
                                        "Respond with one short, warm sentence asking them to say it again. "
                                        "Do not invent meaning and do not call tools."
                                    )
                                    live_queue.reset(session_id, "unclear_speech_repair")
                                    _send_live_content(session_id, live_queue, repair_prompt)
                            else:
                                if heard_noise_only_this_turn:
                                    logger.info(
                                        "Ignoring likely non-speech audio for session %s instead of prompting a repeat.",
                                        session_id,
                                    )

                        await websocket.send_text(json.dumps({"type": "TURN_COMPLETE"}))
                        seal_delay_seconds = (
                            0.8
                            if completed_pending_scene_replacement_ack_turn
                            and session_id not in _ending_story_sessions
                            else (
                                0.45
                                if had_child_input_this_turn
                                and assistant_finished_utterances_this_turn <= 0
                                and session_id not in _ending_story_sessions
                                else 0.0
                            )
                        )
                        _schedule_turn_output_seal(seal_delay_seconds)
                        model_emitted_meaningful_output = False
                        child_utterance_this_turn = ""
                        last_persisted_partial_child_utterance = ""
                        last_persisted_partial_child_utterance_finished = False
                        child_turn_attempted_this_turn = False
                        child_turn_answered_this_turn = False
                        assistant_output_soft_closed = False
                        assistant_output_hard_closed = False
                        assistant_finished_utterances_this_turn = 0
                        heard_humanish_speech_this_turn = False
                        heard_noise_only_this_turn = False
                        _audio_seen_this_turn.discard(session_id)

                # Stream exited cleanly.
                break

            except asyncio.CancelledError:
                raise
            except Exception as stream_exc:
                meaningful_pending_turn = (
                    _is_meaningful_text(child_utterance_this_turn)
                    or _is_meaningful_text(last_child_utterance)
                    or _partial_child_utterance_is_resumable(last_persisted_partial_child_utterance)
                    or session_id in _audio_seen_this_turn
                )
                if _is_clean_live_close(stream_exc):
                    reconnect_state: dict[str, Any] | None = None
                    if reconnect_attempt < max_reconnect_attempts:
                        try:
                            reconnect_session = await runner.session_service.get_session(
                                app_name="storyteller",
                                user_id=user_id,
                                session_id=session_id,
                            )
                        except Exception:
                            reconnect_session = None
                        reconnect_state = (
                            dict(reconnect_session.state)
                            if reconnect_session and isinstance(reconnect_session.state, dict)
                            else None
                        )
                    if (
                        reconnect_attempt < max_reconnect_attempts
                        and _should_attempt_clean_live_resume(
                            session_id,
                            reconnect_state,
                            meaningful_pending_turn=meaningful_pending_turn,
                        )
                    ):
                        reconnect_attempt += 1
                        assistant_output_soft_closed = False
                        assistant_output_hard_closed = False
                        assistant_audio_closed_after_finish = False
                        assistant_finished_utterances_this_turn = 0
                        turn_output_sealed_until_child_input = False
                        deferred_opening_turn_complete = False
                        _cancel_deferred_opening_finalize()
                        _emit_live_telemetry(
                            "live_clean_close_recovered",
                            session_id=session_id,
                            include_runtime=True,
                            attempt=reconnect_attempt,
                            max_attempts=max_reconnect_attempts,
                            error=str(stream_exc),
                        )
                        logger.warning(
                            "Live stream closed cleanly for session %s but conversation is still active; restarting (%d/%d).",
                            session_id,
                            reconnect_attempt,
                            max_reconnect_attempts,
                        )
                        await asyncio.sleep(0.15)
                        continue
                    logger.info(
                        "Live stream closed cleanly for session %s: %s",
                        session_id,
                        stream_exc,
                    )
                    break
                _dump_live_request_debug(session_id)
                assistant_output_soft_closed = False
                assistant_output_hard_closed = False
                assistant_audio_closed_after_finish = False
                assistant_finished_utterances_this_turn = 0
                turn_output_sealed_until_child_input = False
                deferred_opening_turn_complete = False
                _cancel_deferred_opening_finalize()
                forced_turn_recovery = isinstance(stream_exc, _LiveTurnRecoveryRequested)
                invalid_argument_error = _is_invalid_argument_live_error(stream_exc)
                internal_error_after_child_turn = (
                    getattr(stream_exc, "status_code", None) == 1011 and meaningful_pending_turn
                )
                recoverable_capability_error = _is_capability_live_error(stream_exc) and (
                    session_id in _ending_story_sessions
                    or meaningful_pending_turn
                )
                if forced_turn_recovery and hard_reset_attempts < 2:
                    await _perform_hard_live_reset(
                        reason=str(stream_exc),
                        user_message="I heard you. Give me one second...",
                    )
                    continue
                if recoverable_capability_error and hard_reset_attempts < 2:
                    hard_reset_attempts += 1
                    reconnect_attempt = 0
                    _emit_live_telemetry(
                        "live_capability_error_recovered",
                        session_id=session_id,
                        include_runtime=True,
                        hard_reset_attempt=hard_reset_attempts,
                        error=str(stream_exc),
                    )
                    logger.warning(
                        "Recoverable live capability error for session %s (hard reset %d/2): %s",
                        session_id,
                        hard_reset_attempts,
                        stream_exc,
                    )
                    try:
                        await websocket.send_text(
                            ServerEvent(
                                type=ServerEventType.ERROR,
                                payload={
                                    "message": "Magic hiccup. Reopening Amelia now...",
                                    "auto_resume": True,
                                },
                            ).model_dump_json()
                        )
                    except Exception:
                        pass
                    await _promote_partial_child_utterance_to_pending(runner, user_id, session_id)
                    await _prune_session_history(runner, user_id, session_id)
                    live_queue.reset(session_id, f"capability_hard_reset_{hard_reset_attempts}")
                    await _resume_pending_child_turn(
                        runner,
                        user_id,
                        session_id,
                        live_queue,
                        recovery_reason=f"capability_hard_reset_{hard_reset_attempts}",
                    )
                    await asyncio.sleep(1.5)
                    continue
                if _is_capability_live_error(stream_exc):
                    _emit_live_telemetry(
                        "live_capability_error_fatal",
                        session_id=session_id,
                        include_runtime=True,
                        error=str(stream_exc),
                    )
                    logger.error(
                        "Non-retryable live capability/config error for session %s: %s",
                        session_id,
                        stream_exc,
                    )
                    raise
                if (invalid_argument_error or internal_error_after_child_turn) and hard_reset_attempts < 2:
                    await _perform_hard_live_reset(
                        reason=str(stream_exc),
                        user_message="Amelia is picking the story right back up...",
                    )
                    continue
                if _is_retryable_live_error(stream_exc) and reconnect_attempt < max_reconnect_attempts:
                    reconnect_attempt += 1
                    retry_backoff_schedule = (0.35, 0.75, 1.15, 1.5, 1.75, 2.0)
                    backoff_seconds = retry_backoff_schedule[
                        min(reconnect_attempt - 1, len(retry_backoff_schedule) - 1)
                    ]
                    _emit_live_telemetry(
                        "live_retryable_error",
                        session_id=session_id,
                        include_runtime=True,
                        attempt=reconnect_attempt,
                        max_attempts=max_reconnect_attempts,
                        error=str(stream_exc),
                    )
                    logger.warning(
                        "Transient live stream error for session %s (attempt %d/%d): %s",
                        session_id,
                        reconnect_attempt,
                        max_reconnect_attempts,
                        stream_exc,
                    )
                    if reconnect_attempt >= 3:
                        try:
                            await websocket.send_text(
                                ServerEvent(
                                    type=ServerEventType.ERROR,
                                    payload={
                                        "message": "Amelia is smoothing out a tiny hiccup. Keep talking.",
                                        "auto_resume": True,
                                    },
                                ).model_dump_json()
                            )
                        except Exception:
                            pass
                    live_queue.reset(session_id, f"retryable_error_{reconnect_attempt}")
                    await asyncio.sleep(backoff_seconds)
                    continue
                if hard_reset_attempts < 2:
                    await _perform_hard_live_reset(
                        reason=str(stream_exc),
                        user_message="Cleaning up and starting fresh. One moment!",
                    )
                    continue
                raise

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error("Agent runner error: %s", exc, exc_info=True)
        status_code = getattr(exc, "status_code", None)
        msg = str(exc).lower()
        if _is_capability_live_error(exc):
            err_event = ServerEvent(
                type=ServerEventType.ERROR,
                payload={
                    "message": (
                        "Gemini Live rejected this stream configuration. "
                        "Please start a fresh session after adjusting the model or live features."
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
    finally:
        _cancel_deferred_opening_finalize()


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


def _eta_seconds_for_storybook_result(result: str) -> int:
    if "FAST_STORYBOOK" in result:
        return 25
    if "LOCAL_STORYBOOK" in result:
        return 12
    return 90


def _state_has_storyboard_assets(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    scene_urls = state.get("scene_asset_urls", [])
    if isinstance(scene_urls, list) and any(str(url).strip() for url in scene_urls):
        return True
    scene_gcs_uris = state.get("scene_asset_gcs_uris", [])
    if isinstance(scene_gcs_uris, list) and any(str(uri).strip() for uri in scene_gcs_uris):
        return True
    return False


def _session_has_storyboard_assets(session_id: str) -> bool:
    if session_id in _session_has_any_image:
        return True
    try:
        return _state_has_storyboard_assets(_load_storybook_firestore_state(session_id))
    except Exception:
        return False


def _storybook_identity_from_state(state: dict[str, Any]) -> tuple[str | None, str | None]:
    if not isinstance(state, dict):
        return None, None
    title = _resolve_storybook_title(state).strip() or None
    child_name = str(state.get("child_name", "")).strip() or None
    if child_name and child_name.lower() == "friend":
        child_name = None
    return title, child_name


def _assembly_started_at_epoch_ms_from_state(state: dict[str, Any] | None) -> int | None:
    if not isinstance(state, dict):
        return None
    try:
        started_at = int(state.get("assembly_started_at_epoch_ms") or 0)
    except Exception:
        return None
    return started_at if started_at > 0 else None


def _storybook_assembly_in_progress(state: dict[str, Any] | None) -> bool:
    if not isinstance(state, dict):
        return False
    if str(state.get("final_video_url", "") or "").strip():
        return False
    assembly_status = str(state.get("assembly_status", "") or "").strip().lower()
    return assembly_status in {"assembling", "reviewing_storyboard"}


def _sync_story_phase(session_id: str, state: dict[str, Any] | None) -> str:
    if not isinstance(state, dict):
        return "opening"
    phase = derive_story_phase(
        state,
        opening_phase=session_id in _opening_phase_sessions,
        ending_story=session_id in _ending_story_sessions,
        assistant_speaking=(
            session_id in _assistant_speaking_sessions
            or session_id in _awaiting_greeting_sessions
        ),
        pending_scene_render=(
            bool(state.get("scene_render_pending", False))
            or session_id in _pending_image_events
        ),
    )
    state["story_phase"] = phase
    return phase


def _storybook_release_ready(state: dict[str, Any] | None) -> bool:
    return theater_release_ready(state)


def _assembly_wait_context_text(state: dict[str, Any] | None) -> str:
    story_title, child_name = _storybook_identity_from_state(state or {})
    scene_hint = str((state or {}).get("current_scene_storybeat_text") or (state or {}).get("current_scene_description") or "").strip()
    story_summary = str((state or {}).get("story_summary", "") or "").strip()
    context_bits: list[str] = []
    if story_title:
        context_bits.append(f"Story title: {story_title}.")
    if child_name:
        context_bits.append(f"Child name: {child_name}.")
    if scene_hint:
        context_bits.append(f"Last page on screen: {scene_hint[:220]}.")
    elif story_summary:
        context_bits.append(f"Story summary: {story_summary[:320]}.")
    return " ".join(context_bits).strip()


def _assembly_recent_activity_keys(state: dict[str, Any] | None) -> list[str]:
    if not isinstance(state, dict):
        return []
    raw = state.get("assembly_recent_activities")
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    for item in raw:
        key = _normalize_assembly_activity_key(item)
        if key and key not in cleaned:
            cleaned.append(key)
    return cleaned[:6]


def _remember_assembly_activity(state: dict[str, Any], activity: str) -> None:
    key = _normalize_assembly_activity_key(activity)
    if not key:
        return
    recent = [item for item in _assembly_recent_activity_keys(state) if item != key]
    recent.append(key)
    state["assembly_recent_activities"] = recent[-6:]


_ASSEMBLY_ACTIVITY_ALIASES: dict[str, str] = {
    "sparkle_wiggle": "soft_echo",
    "helper_pose": "cozy_breath",
}


def _normalize_assembly_activity_key(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    return _ASSEMBLY_ACTIVITY_ALIASES.get(key, key)


_ASSEMBLY_ACTIVITY_INSTRUCTIONS: dict[str, str] = {
    "favorite_part": (
        "Run a tiny favorite-part memory game. "
        "Ask exactly one short question about the child's favorite, funniest, biggest, or coziest part of the finished story."
    ),
    "favorite_sound": (
        "Run a tiny sound-memory game. "
        "Ask the child what sound they remember best, then invite one silly echo or copycat sound."
    ),
    "soft_echo": (
        "Run a tiny soft-echo game. "
        "Invite one quiet copycat sound, whisper, or silly word from the finished story."
    ),
    "cozy_breath": (
        "Run one tiny calm-down game. "
        "Lead one slow star breath, candle breath, or whisper-count to three together."
    ),
    "tiny_joke": (
        "Tell one tiny preschool-friendly joke, rhyme, or playful sound joke, then pause."
    ),
    "counting_game": (
        "Run a tiny soft counting game with stars, bells, moonbeams, or sleepy toy helpers."
    ),
    "light_color": (
        "If and only if the child explicitly asked to change the room lights, help with one calm room-light color change. "
        "Briefly echo their color wish, use the room-light tool once, and avoid flashing or rapid color cycling."
    ),
}

_ASSEMBLY_WAIT_ACTIVITY_ORDER = [
    "favorite_part",
    "favorite_sound",
    "tiny_joke",
    "counting_game",
    "soft_echo",
    "cozy_breath",
]

_ASSEMBLY_WAIT_NO_FALSE_COMPLETION_TEXT = (
    "Do NOT say the movie is ready, done, finished, starting now, opening now, or ready to watch. "
    "Do NOT say phrases like 'enjoy the movie', 'enjoy the show', 'the movie is done', "
    "'it is time to watch', 'here comes the movie', or 'the curtain is opening'. "
    "Until the real release happens, talk as if the movie is still being made right now."
)

_ASSEMBLY_WAIT_NO_FUTURE_STORY_TEXT = (
    "Do NOT ask what happens next in the story or adventure. "
    "Do NOT ask where to go next, what to do next, what happens next, what the next page is, "
    "or what the child wants next on the adventure. "
    "Do NOT invite another choice that could continue, reopen, or extend the finished story."
)

_ASSEMBLY_WAIT_NO_VISION_OR_MOVEMENT_TEXT = (
    "Do NOT ask the child to show you anything, let you see anything, look at the camera, "
    "make a face, hold up an object, stand up, dance, jump, wiggle, pose, or do any activity "
    "that depends on vision, camera input, or big movement. "
    "Keep the interaction audio-first, calm, quiet-friendly, and bedtime-safe."
)

_ASSEMBLY_WAIT_LIGHT_TOOL_TEXT = (
    "If the child explicitly asks to change the room lights to one simple calm color, "
    "you may use the room-light tool once to help with that exact request. "
    "Do not invent a light change on your own, and do not turn it into flashing or rapid color cycling."
)


def _assembly_child_requested_light_change(child_utterance: str) -> bool:
    cleaned = str(child_utterance or "").strip().lower()
    if not cleaned:
        return False
    color_markers = (
        "blue", "pink", "purple", "violet", "green", "mint", "teal", "aqua", "cyan",
        "gold", "yellow", "orange", "red", "white", "cozy gold", "warm white", "soft white",
    )
    if any(marker in cleaned for marker in ("turn", "make", "change", "set", "switch")) and any(
        marker in cleaned for marker in ("light", "lights", "lamp", "color", "colour")
    ):
        return True
    if any(marker in cleaned for marker in color_markers) and any(
        marker in cleaned for marker in ("light", "lights", "color", "colour")
    ):
        return True
    return False


def _assembly_activity_instruction(activity: str) -> str:
    normalized = _normalize_assembly_activity_key(activity)
    return _ASSEMBLY_ACTIVITY_INSTRUCTIONS.get(
        normalized,
        "Start one tiny waiting-room game right away. Keep it playful, simple, and easy for a 4-year-old to answer.",
    )


def _choose_assembly_wait_activity(
    state: dict[str, Any] | None,
    *,
    child_utterance: str = "",
    intro: bool = False,
    retry: bool = False,
) -> str:
    recent = set(_assembly_recent_activity_keys(state))
    cleaned = str(child_utterance or "").strip().lower()
    if _assembly_child_requested_light_change(cleaned):
        return "light_color"
    preferred: list[str] = []
    if intro:
        preferred.extend(["tiny_joke", "favorite_sound", "counting_game"])
    if retry:
        preferred.extend(["tiny_joke", "favorite_part", "counting_game"])
    if any(marker in cleaned for marker in ("sound", "music", "boing", "buzz", "pop", "giggle", "silly")):
        preferred.extend(["favorite_sound", "soft_echo"])
    if any(marker in cleaned for marker in ("quiet", "calm", "cozy", "sleep", "sleepy", "bed", "night", "whisper")):
        preferred.extend(["cozy_breath", "tiny_joke"])
    if any(marker in cleaned for marker in ("friend", "helper", "elf", "buddy", "wave", "hello")):
        preferred.extend(["favorite_part", "tiny_joke"])
    if any(marker in cleaned for marker in ("favorite", "best", "liked", "love", "part")):
        preferred.extend(["favorite_part", "favorite_sound"])
    prompt_count = 0
    if isinstance(state, dict):
        try:
            prompt_count = int(state.get("assembly_wait_prompt_count", 0) or 0)
        except Exception:
            prompt_count = 0
    rotated = [
        _ASSEMBLY_WAIT_ACTIVITY_ORDER[(prompt_count + offset) % len(_ASSEMBLY_WAIT_ACTIVITY_ORDER)]
        for offset in range(len(_ASSEMBLY_WAIT_ACTIVITY_ORDER))
    ]
    ordered: list[str] = []
    for key in preferred + rotated:
        if key not in ordered:
            ordered.append(key)
    for key in ordered:
        if key not in recent:
            return key
    return ordered[0] if ordered else "favorite_part"


def _select_and_record_assembly_wait_activity(
    state: dict[str, Any],
    *,
    child_utterance: str = "",
    intro: bool = False,
    retry: bool = False,
) -> str:
    activity = _choose_assembly_wait_activity(
        state,
        child_utterance=child_utterance,
        intro=intro,
        retry=retry,
    )
    _remember_assembly_activity(state, activity)
    try:
        prompt_count = int(state.get("assembly_wait_prompt_count", 0) or 0)
    except Exception:
        prompt_count = 0
    state["assembly_wait_prompt_count"] = prompt_count + 1
    return activity


def _mark_pending_assembly_wait_response(
    state: dict[str, Any],
    *,
    child_utterance: str,
    activity_key: str,
) -> None:
    # Keep waiting-room chatter isolated from live story continuity.
    state["pending_response"] = True
    state["pending_response_interrupted"] = False
    state["scene_tool_turn_open"] = True
    state["pending_response_token"] = uuid.uuid4().hex
    state["assembly_wait_last_child_utterance"] = str(child_utterance or "").strip()
    _remember_assembly_activity(state, activity_key)
    try:
        prompt_count = int(state.get("assembly_wait_prompt_count", 0) or 0)
    except Exception:
        prompt_count = 0
    state["assembly_wait_prompt_count"] = prompt_count + 1


def _assembly_wait_prompt(
    state: dict[str, Any] | None,
    *,
    child_utterance: str = "",
    activity: str = "",
    intro: bool = False,
    retry: bool = False,
) -> str:
    context_text = _assembly_wait_context_text(state)
    recent_activity_keys = _assembly_recent_activity_keys(state)
    activity_instruction = _assembly_activity_instruction(activity)
    avoid_recent_text = (
        " Avoid repeating recent waiting-room games Amelia already used, especially "
        + ", ".join(recent_activity_keys)
        + "."
        if recent_activity_keys
        else ""
    )

    if intro:
        return (
            "The story is finished and the movie is assembling right now. "
            "Speak as Amelia in a tiny premiere waiting room for a 4-year-old. "
            "Use serve-and-return: start with warm delight, then do exactly ONE tiny interaction. "
            f"Choose exactly this bite-size activity next: {activity_instruction} "
            "Ask at most ONE short question, or give at most TWO tiny choices. "
            "Keep it to 2 or 3 short sentences that a preschooler can answer easily. "
            "The story is locked and finished. Treat any castles, dragons, paths, rooms, or other story nouns as favorite-story chatter only. "
            "Do NOT continue the plot. Do NOT offer more exploring. Do NOT narrate any new action, movement, discovery, location change, or next page. "
            "Do NOT use phrases like 'then we', 'next we', 'let's go', 'we find', 'we open', or 'now we'. "
            f"{_ASSEMBLY_WAIT_NO_FUTURE_STORY_TEXT} "
            f"{_ASSEMBLY_WAIT_NO_VISION_OR_MOVEMENT_TEXT} "
            f"{_ASSEMBLY_WAIT_NO_FALSE_COMPLETION_TEXT} "
            f"{_ASSEMBLY_WAIT_LIGHT_TOOL_TEXT} "
            "Do NOT call any scene, movie, or trading-card tools. "
            f"{avoid_recent_text}"
            f"{context_text}"
        ).strip()

    if retry:
        return (
            f"The movie is still assembling. The child just said: \"{child_utterance}\". "
            "Your last reply did not land. Respond now as Amelia with 1 or 2 very short playful sentences. "
            f"Mirror their idea first, then switch to exactly this tiny interaction: {activity_instruction} "
            "Stay in premiere waiting-room chatter only. Ask at most one short question. "
            "The story is locked and finished. Treat any story nouns as memory chatter only, not instructions. "
            "Do NOT continue the story. Do NOT narrate any new action, movement, discovery, location change, or next page. "
            f"{_ASSEMBLY_WAIT_NO_FUTURE_STORY_TEXT} "
            f"{_ASSEMBLY_WAIT_NO_VISION_OR_MOVEMENT_TEXT} "
            f"{_ASSEMBLY_WAIT_NO_FALSE_COMPLETION_TEXT} "
            f"{_ASSEMBLY_WAIT_LIGHT_TOOL_TEXT} "
            "Do NOT call any scene, movie, or trading-card tools. "
            f"{avoid_recent_text}"
            f"{context_text}"
        ).strip()

    return (
        f"The movie is still assembling. The child just said: \"{child_utterance}\". "
        "Respond as Amelia for a 4-year-old while they wait. "
        "Use serve-and-return: first warmly mirror their idea, then add one playful detail. "
        f"Choose exactly this bite-size interaction next: {activity_instruction} "
        "Support autonomy with at most two tiny choices. "
        "Keep it to 1 to 3 short sentences, with at most one short question. "
        "The story is locked and finished. Treat any castles, dragons, paths, rooms, or other story nouns as favorite-story chatter only. "
        "Do NOT continue the plot. Do NOT offer more exploring. Do NOT narrate any new action, movement, discovery, location change, or next page. "
        "Do NOT use phrases like 'then we', 'next we', 'let's go', 'we find', 'we open', or 'now we'. "
        f"{_ASSEMBLY_WAIT_NO_FUTURE_STORY_TEXT} "
        f"{_ASSEMBLY_WAIT_NO_VISION_OR_MOVEMENT_TEXT} "
        f"{_ASSEMBLY_WAIT_NO_FALSE_COMPLETION_TEXT} "
        f"{_ASSEMBLY_WAIT_LIGHT_TOOL_TEXT} "
        "Do NOT call any scene, movie, or trading-card tools. "
        f"{avoid_recent_text}"
        f"{context_text}"
    ).strip()


def _assembly_activity_prompt(
    state: dict[str, Any] | None,
    *,
    activity: str,
    label: str = "",
) -> str:
    normalized = str(activity or "").strip().lower()
    activity_instruction = _assembly_activity_instruction(normalized)
    button_hint = f" The child tapped the on-screen choice labeled '{label}'." if label else ""
    context_text = _assembly_wait_context_text(state)
    recent_activity_keys = [item for item in _assembly_recent_activity_keys(state) if item != normalized]
    avoid_recent_text = (
        " Do not repeat recent waiting-room games Amelia already used, especially "
        + ", ".join(recent_activity_keys)
        + "."
        if recent_activity_keys
        else ""
    )
    return (
        "The story is finished and the movie is still assembling. "
        "Speak as Amelia in a tiny premiere waiting room for a 4-year-old. "
        f"{activity_instruction}{button_hint} "
        "Use serve-and-return: start warmly, name the game in simple words, then do exactly ONE tiny interaction. "
        "Keep it to 1 or 2 short sentences, with at most one short question. "
        "The story is locked and finished. Keep this in waiting-room chatter only. "
        "Do NOT continue the plot. Do NOT offer more exploring. Do NOT narrate any new action, movement, discovery, location change, or next page. "
        f"{_ASSEMBLY_WAIT_NO_FUTURE_STORY_TEXT} "
        f"{_ASSEMBLY_WAIT_NO_VISION_OR_MOVEMENT_TEXT} "
        f"{_ASSEMBLY_WAIT_NO_FALSE_COMPLETION_TEXT} "
        f"{_ASSEMBLY_WAIT_LIGHT_TOOL_TEXT} "
        "Do NOT call any scene, movie, or trading-card tools. "
        f"{avoid_recent_text}"
        f"{context_text}"
    ).strip()


def _send_assembly_wait_prompt(
    *,
    session_id: str,
    live_queue: LiveRequestQueue,
    state: dict[str, Any] | None,
    child_utterance: str = "",
    activity: str = "",
    intro: bool = False,
    retry: bool = False,
) -> None:
    prompt = _assembly_wait_prompt(
        state,
        child_utterance=child_utterance,
        activity=activity,
        intro=intro,
        retry=retry,
    )
    _send_live_content(session_id, live_queue, prompt)


def _send_assembly_activity_prompt(
    *,
    session_id: str,
    live_queue: LiveRequestQueue,
    state: dict[str, Any] | None,
    activity: str,
    label: str = "",
) -> None:
    prompt = _assembly_activity_prompt(
        state,
        activity=activity,
        label=label,
    )
    _send_live_content(session_id, live_queue, prompt)


async def _release_end_story_flush_after_delay(
    *,
    runner: Runner,
    user_id: str,
    session_id: str,
    live_queue: LiveRequestQueue,
    delay_seconds: float = 0.8,
) -> None:
    await asyncio.sleep(delay_seconds)
    if session_id not in _ending_story_flush_sessions:
        return
    _ending_story_flush_sessions.discard(session_id)
    if session_id in _assembly_intro_sent_sessions or session_id not in _ending_story_sessions:
        return
    try:
        session = await runner.session_service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        session = None
    intro_activity = {"key": ""}
    if session and session.state:
        def _mark_intro_activity(state: dict[str, Any]) -> None:
            intro_activity["key"] = _select_and_record_assembly_wait_activity(state, intro=True)

        await _mutate_state(
            runner=runner,
            user_id=user_id,
            session_id=session_id,
            mutator=_mark_intro_activity,
        )
    state = dict(session.state) if session and session.state else {}
    _send_assembly_wait_prompt(
        session_id=session_id,
        live_queue=live_queue,
        state=state,
        activity=intro_activity["key"],
        intro=True,
    )
    _assembly_intro_sent_sessions.add(session_id)


async def _restore_storybook_ui_after_reconnect(
    websocket: WebSocket,
    session_id: str,
    state: dict[str, Any] | None,
) -> None:
    if not isinstance(state, dict):
        return

    final_video_url = str(state.get("final_video_url", "") or "").strip()
    trading_card_url = str(state.get("trading_card_url", "") or "").strip() or None
    story_title, child_name = _storybook_identity_from_state(state)
    narration_raw = state.get("narration_lines")
    narration_lines = (
        [str(line).strip() for line in narration_raw if isinstance(line, str) and str(line).strip()]
        if isinstance(narration_raw, list)
        else None
    )
    lighting_cues_raw = state.get("theater_lighting_cues")
    lighting_cues = (
        [dict(item) for item in lighting_cues_raw if isinstance(item, dict)]
        if isinstance(lighting_cues_raw, list)
        else None
    )
    audio_available = state.get("audio_available")
    if not isinstance(audio_available, bool):
        audio_available = None
    final_has_audio_stream = state.get("final_has_audio_stream")
    if not isinstance(final_has_audio_stream, bool):
        final_has_audio_stream = None
    try:
        final_video_duration_sec = float(state.get("final_video_duration_sec") or 0.0)
    except Exception:
        final_video_duration_sec = 0.0
    explicit_theater_release_ready = state.get("theater_release_ready")
    if not isinstance(explicit_theater_release_ready, bool):
        explicit_theater_release_ready = None

    assembly_status = str(state.get("assembly_status", "") or "").strip().lower()

    if assembly_status == "failed":
        error_message = str(state.get("assembly_error", "") or "").strip()
        await websocket.send_text(
            ServerEvent(
                type=ServerEventType.ERROR,
                payload={
                    "message": error_message or "Movie assembly failed before the final video was created.",
                    "assembly_failed": True,
                },
            ).model_dump_json()
        )
        return

    if _storybook_release_ready(state):
        await websocket.send_text(
            theater_mode_event(
                mp4_url=final_video_url,
                trading_card_url=trading_card_url,
                narration_lines=narration_lines,
                lighting_cues=lighting_cues,
                audio_available=audio_available,
                final_has_audio_stream=final_has_audio_stream,
                final_video_duration_sec=final_video_duration_sec if final_video_duration_sec > 0 else None,
                theater_release_ready=explicit_theater_release_ready if explicit_theater_release_ready is not None else True,
                story_title=story_title,
                child_name=child_name,
                story_phase="theater",
            ).model_dump_json()
        )
        return

    if assembly_status in {"assembling", "reviewing_storyboard"}:
        await _announce_storybook_assembly_started(
            websocket=websocket,
            session_id=session_id,
            eta_seconds=25 if _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False) else 90,
            story_title=story_title,
            child_name=child_name,
            started_at_epoch_ms=_assembly_started_at_epoch_ms_from_state(state),
        )


def _normalize_movie_feedback_rating(raw: Any) -> str:
    candidate = str(raw or "").strip().lower()
    if candidate in {"loved_it", "pretty_good", "needs_fixing"}:
        return candidate
    return "pretty_good"


def _normalize_movie_feedback_reasons(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        reason_id = str(item or "").strip().lower()
        if not reason_id or reason_id not in _MOVIE_FEEDBACK_REASON_MAP or reason_id in seen:
            continue
        seen.add(reason_id)
        cleaned.append(reason_id)
    return cleaned[:6]


def _movie_feedback_issue_texts(rating: str, reasons: list[str], note: str) -> list[str]:
    issues = [_MOVIE_FEEDBACK_REASON_MAP[reason_id] for reason_id in reasons if reason_id in _MOVIE_FEEDBACK_REASON_MAP]
    if note and rating != "loved_it":
        issues.append(f"Parent note: {note[:300]}")
    return issues


def _movie_feedback_prompt_text(state: dict[str, Any]) -> str:
    story_summary = str(state.get("story_summary", "")).strip()
    if story_summary:
        return story_summary[:600]
    scene_descriptions = [
        str(item).strip()
        for item in (state.get("scene_descriptions") or [])
        if str(item).strip()
    ]
    if scene_descriptions:
        return " | ".join(scene_descriptions[:4])[:600]
    current_scene = str(state.get("current_scene_description", "")).strip()
    return current_scene[:600]


def _movie_feedback_scope_outcome(rating: str) -> str:
    return {
        "loved_it": "parent_loved_it",
        "pretty_good": "parent_pretty_good",
        "needs_fixing": "parent_needs_fixing",
    }.get(rating, "parent_observed")


def _record_movie_feedback_sync(
    session_id: str,
    state: dict[str, Any],
    *,
    rating: str,
    reasons: list[str],
    note: str,
) -> None:
    submitted_at_epoch = int(time.time())
    feedback_record = {
        "rating": rating,
        "reasons": reasons,
        "note": note,
        "source": "parent_post_video_feedback",
        "submitted_at_epoch": submitted_at_epoch,
    }
    issue_texts = _movie_feedback_issue_texts(rating, reasons, note)
    prompt_text = _movie_feedback_prompt_text(state)
    scope_outcome = _movie_feedback_scope_outcome(rating)

    existing_review = state.get("post_movie_meta_review")
    post_movie_meta_review = dict(existing_review) if isinstance(existing_review, dict) else {}
    post_movie_meta_review["parent_feedback"] = feedback_record
    storybook_update = {
        "movie_feedback_latest": feedback_record,
        "post_movie_meta_review": post_movie_meta_review,
    }
    _update_storybook_firestore(session_id, storybook_update)
    cache_storybook_state(
        session_id,
        {
            **state,
            **storybook_update,
        },
    )

    metadata = {
        "source": "parent_post_video_feedback",
        "rating": rating,
        "reasons": reasons,
        "note_present": bool(note),
        "note_excerpt": note[:200],
        "issue_count": len(issue_texts),
    }
    record_prompt_feedback(
        "storyboard_review",
        outcome=scope_outcome,
        issues=issue_texts,
        prompt_text=prompt_text,
        session_id=session_id,
        metadata=metadata,
        force_log=True,
    )
    if issue_texts:
        record_prompt_feedback(
            "interactive_story",
            outcome=scope_outcome,
            issues=issue_texts,
            prompt_text=prompt_text,
            session_id=session_id,
            metadata=metadata,
        )
        record_prompt_feedback(
            "interactive_scene_visual",
            outcome=scope_outcome,
            issues=issue_texts,
            prompt_text=prompt_text,
            session_id=session_id,
            metadata=metadata,
        )


def _ensure_final_video_watch_task(session_id: str) -> None:
    if session_id not in _watching_final_video_sessions:
        _watching_final_video_sessions.add(session_id)
        asyncio.create_task(_watch_for_final_video(session_id))


async def _announce_storybook_assembly_started(
    websocket: WebSocket,
    session_id: str,
    eta_seconds: int,
    *,
    story_title: str | None = None,
    child_name: str | None = None,
    kind: str = "initial",
    started_at_epoch_ms: int | None = None,
) -> None:

    should_emit = session_id not in _video_generation_started_sessions or kind == "remake"
    if kind == "remake":
        _final_video_watch_not_before_epoch[session_id] = time.time()
    if should_emit:
        payload: dict[str, Any] = {
            "stage": "storybook",
            "message": "Making your storybook movie…" if kind != "remake" else "Polishing a better version of your movie…",
            "eta_seconds": eta_seconds,
            "kind": kind,
            "story_phase": "remake" if kind == "remake" else "assembling_movie",
        }
        if story_title:
            payload["story_title"] = story_title
        if child_name:
            payload["child_name"] = child_name
        if started_at_epoch_ms and started_at_epoch_ms > 0:
            payload["started_at_epoch_ms"] = started_at_epoch_ms
        await websocket.send_text(
            ServerEvent(
                type=ServerEventType.VIDEO_GENERATION_STARTED,
                payload=payload,
            ).model_dump_json()
        )
        _video_generation_started_sessions.add(session_id)
    _ensure_final_video_watch_task(session_id)


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
        resume_state = load_storybook_resume_state(observed_session_id)
        await _announce_storybook_assembly_started(
            websocket=websocket,
            session_id=observed_session_id,
            eta_seconds=_eta_seconds_for_storybook_result(result),
            started_at_epoch_ms=_assembly_started_at_epoch_ms_from_state(resume_state),
        )

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
        _watching_final_video_sessions.discard(session_id)
        return

    object_path = f"{session_id}/story_final.mp4"
    deadline = asyncio.get_running_loop().time() + 600
    try:
        poll_seconds = float(os.environ.get("STORYBOOK_FINAL_VIDEO_POLL_SECONDS", "1.0"))
    except Exception:
        poll_seconds = 1.0
    poll_seconds = max(0.5, min(poll_seconds, 4.0))
    firestore_client = None
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    db_name = os.environ.get("FIRESTORE_DATABASE", "(default)")
    if project:
        try:
            from google.cloud import firestore as _fs

            firestore_client = _fs.AsyncClient(project=project, database=db_name)
        except Exception as exc:
            logger.debug("Could not initialize Firestore client for final video watch: %s", exc)

    try:
        while asyncio.get_running_loop().time() < deadline:
            try:
                resume_state = load_storybook_resume_state(session_id)
                merged_state = dict(resume_state or {})
                if firestore_client is not None:
                    try:
                        doc = await firestore_client.collection("storyteller_sessions").document(session_id).get()
                        if doc.exists:
                            merged_state.update(doc.to_dict() or {})
                            if merged_state:
                                cache_storybook_state(session_id, merged_state)
                    except Exception as exc:
                        logger.debug("Could not read final movie metadata from Firestore: %s", exc)
                assembly_status = str((merged_state or {}).get("assembly_status", "") or "").strip().lower()
                if assembly_status == "failed":
                    error_message = str((merged_state or {}).get("assembly_error", "") or "").strip()
                    publish_session_event(
                        session_id,
                        {
                            "type": "error",
                            "payload": {
                                "message": error_message or "Movie assembly failed before the final video was created.",
                                "assembly_failed": True,
                            },
                        },
                    )
                    return
                client = storage.Client()
                blob = client.bucket(bucket_name).blob(object_path)
                exists = await asyncio.to_thread(blob.exists)
                if exists:
                    not_before_epoch = _final_video_watch_not_before_epoch.get(session_id, 0.0)
                    if not_before_epoch:
                        try:
                            await asyncio.to_thread(blob.reload)
                            updated = getattr(blob, "updated", None)
                            updated_epoch = updated.timestamp() if updated else 0.0
                        except Exception:
                            updated_epoch = 0.0
                        if updated_epoch <= 0.0 or updated_epoch + 0.25 < not_before_epoch:
                            await asyncio.sleep(1)
                            continue
                    story_title, child_name = _storybook_identity_from_state(merged_state)
                    narration_raw = merged_state.get("narration_lines")
                    narration_lines = (
                        [str(line).strip() for line in narration_raw if isinstance(line, str) and str(line).strip()]
                        if isinstance(narration_raw, list)
                        else None
                    )
                    lighting_cues_raw = merged_state.get("theater_lighting_cues")
                    lighting_cues = (
                        [dict(item) for item in lighting_cues_raw if isinstance(item, dict)]
                        if isinstance(lighting_cues_raw, list)
                        else None
                    )
                    audio_available = merged_state.get("audio_available")
                    if not isinstance(audio_available, bool):
                        audio_available = None
                    final_has_audio_stream = merged_state.get("final_has_audio_stream")
                    if not isinstance(final_has_audio_stream, bool):
                        final_has_audio_stream = None
                    try:
                        final_video_duration_sec = float(merged_state.get("final_video_duration_sec") or 0.0)
                    except Exception:
                        final_video_duration_sec = 0.0
                    explicit_theater_release_ready = merged_state.get("theater_release_ready")
                    if not isinstance(explicit_theater_release_ready, bool):
                        explicit_theater_release_ready = None
                    final_url: str | None = str(merged_state.get("final_video_url", "") or "").strip() or None
                    trading_card_url: str | None = str((merged_state or {}).get("trading_card_url", "") or "").strip() or None
                    publish_ready = _storybook_release_ready(merged_state)
                    if not publish_ready:
                        await asyncio.sleep(1)
                        continue
                    url = final_url
                    if not url:
                        try:
                            url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=2), method="GET")
                        except Exception:
                            url = f"https://storage.googleapis.com/{bucket_name}/{object_path}"

                    publish_session_event(
                        session_id,
                        theater_mode_event(
                            mp4_url=url,
                            trading_card_url=trading_card_url,
                            narration_lines=narration_lines,
                            lighting_cues=lighting_cues,
                            audio_available=audio_available,
                            final_has_audio_stream=final_has_audio_stream,
                            final_video_duration_sec=final_video_duration_sec if final_video_duration_sec > 0 else None,
                            theater_release_ready=explicit_theater_release_ready if explicit_theater_release_ready is not None else publish_ready,
                            story_title=story_title,
                            child_name=child_name,
                            story_phase="theater",
                        ).model_dump(mode="json"),
                    )
                    return
            except Exception as exc:
                logger.debug("Final video poll error for %s: %s", session_id, exc)

            await asyncio.sleep(poll_seconds)
    finally:
        _watching_final_video_sessions.discard(session_id)
        _final_video_watch_not_before_epoch.pop(session_id, None)


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
        network = cmd.payload.get("network", {}) if isinstance(cmd.payload, dict) else {}
        story_tone = _normalize_story_tone(
            (
                cmd.payload.get("story_tone")
                or cmd.payload.get("storyTone")
            ) if isinstance(cmd.payload, dict) else ""
        )
        child_age = _normalize_child_age(
            (
                cmd.payload.get("child_age")
                or cmd.payload.get("childAge")
            ) if isinstance(cmd.payload, dict) else None
        )
        storybook_movie_pacing = _normalize_storybook_movie_pacing(
            (
                cmd.payload.get("storybook_movie_pacing")
                or cmd.payload.get("storybookMoviePacing")
            ) if isinstance(cmd.payload, dict) else None
        )
        storybook_elevenlabs_voice_id = _normalize_storybook_elevenlabs_voice_id(
            (
                cmd.payload.get("storybook_elevenlabs_voice_id")
                or cmd.payload.get("storybookElevenlabsVoiceId")
            ) if isinstance(cmd.payload, dict) else None
        )
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
        try:
            effective_type = str(network.get("effectiveType") or network.get("effective_type") or "").strip().lower()
            save_data = bool(network.get("saveData") or network.get("save_data"))
            downlink_mbps = float(network.get("downlink") or network.get("downlink_mbps") or 0)
            rtt_ms = float(network.get("rtt") or network.get("rtt_ms") or 0)
        except Exception:
            effective_type = ""
            save_data = False
            downlink_mbps = 0.0
            rtt_ms = 0.0

        if width and height:
            def _save_device_profile(state: dict[str, Any]) -> None:
                state["device_profile"] = {
                    "width": width,
                    "height": height,
                    "device_pixel_ratio": dpr,
                    "is_compact": is_compact,
                }
                state["network_profile"] = {
                    "effective_type": effective_type,
                    "save_data": save_data,
                    "downlink_mbps": downlink_mbps,
                    "rtt_ms": rtt_ms,
                }
                state["preferred_aspect_ratio"] = _closest_aspect_ratio(width, height)
                state["preferred_image_size"] = _preferred_image_size(width, height, is_compact)
                state["story_tone"] = story_tone
                state["child_age"] = child_age
                state["child_age_band"] = child_age_band(child_age)
                state["storybook_movie_pacing"] = storybook_movie_pacing
                state["storybook_elevenlabs_voice_id"] = storybook_elevenlabs_voice_id

            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=_save_device_profile,
            )
        else:
            await _mutate_state(
                runner=runner,
                user_id=user_id,
                session_id=session_id,
                mutator=lambda state: (
                    state.__setitem__("story_tone", story_tone),
                    state.__setitem__("child_age", child_age),
                    state.__setitem__("child_age_band", child_age_band(child_age)),
                    state.__setitem__("storybook_movie_pacing", storybook_movie_pacing),
                    state.__setitem__("storybook_elevenlabs_voice_id", storybook_elevenlabs_voice_id),
                ),
            )

        _update_storybook_firestore(
            session_id,
            {
                "story_tone": story_tone,
                "child_age": child_age,
                "child_age_band": child_age_band(child_age),
                "storybook_movie_pacing": storybook_movie_pacing,
                "storybook_elevenlabs_voice_id": storybook_elevenlabs_voice_id,
            },
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

    elif cmd.type == ClientCommandType.PAGE_READ_ALOUD:
        payload = cmd.payload if isinstance(cmd.payload, dict) else {}
        active = bool(payload.get("active", False))
        try:
            suppress_for_ms = int(payload.get("suppress_for_ms", payload.get("suppressForMs", 0)) or 0)
        except Exception:
            suppress_for_ms = 0
        suppress_for_ms = max(0, min(suppress_for_ms, 10_000))
        _set_page_read_aloud_suppression(
            cmd.session_id,
            active=active,
            suppress_for_ms=suppress_for_ms,
        )
        if active:
            _activity_active_sessions.discard(cmd.session_id)
        logger.info(
            "Page read-aloud suppression updated for session %s: active=%s suppress_for_ms=%d",
            cmd.session_id,
            active,
            suppress_for_ms,
        )
        return

    elif cmd.type == ClientCommandType.ACTIVITY_START:
        if _page_read_aloud_suppression_active(session_id):
            _activity_last_change[session_id] = time.monotonic()
            logger.info("Ignoring activity_start during page read-aloud suppression for session %s", session_id)
            return
        if session_id in _awaiting_greeting_sessions and not _barge_in_enabled():
            # Ignore manual VAD start if we are still waiting for the greeting to finish
            # and barge-in is disabled. The audio bytes are also dropped above.
            pass
        else:
            if session_id in _activity_active_sessions:
                _activity_last_change[session_id] = time.monotonic()
                return
            if _barge_in_enabled() and (
                session_id in _assistant_speaking_sessions or session_id in _awaiting_greeting_sessions
            ):
                logger.info("Barge-in activated for session %s", session_id)
                _activate_barge_in(session_id)
            if not _env_enabled("ENABLE_SERVER_VAD", default=False):
                try:
                    _record_live_request(session_id, "activity_start", {})
                    live_queue.send_activity_start()
                except Exception:
                    pass
                _activity_active_sessions.add(session_id)
            _activity_last_change[session_id] = time.monotonic()
        return

    elif cmd.type == ClientCommandType.ACTIVITY_END:
        if _page_read_aloud_suppression_active(session_id):
            _activity_active_sessions.discard(session_id)
            _activity_last_change[session_id] = time.monotonic()
            logger.info("Ignoring activity_end during page read-aloud suppression for session %s", session_id)
            return
        if session_id in _awaiting_greeting_sessions and not _barge_in_enabled():
            # Ignore manual VAD end if we ignored the start.
            pass
        else:
            if not _env_enabled("ENABLE_SERVER_VAD", default=False):
                if session_id not in _activity_active_sessions:
                    logger.info(
                        "Ignoring stray activity_end on inactive live turn for session %s",
                        session_id,
                    )
                    _activity_last_change[session_id] = time.monotonic()
                    return
                try:
                    _record_live_request(session_id, "activity_end", {})
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
                storage_session = _get_storage_session(runner, user_id, cmd.session_id)
                if storage_session is None:
                    return
                points = _scene_branch_points(storage_session.state)
                if len(points) >= 2:
                    await _branch_story_to_scene(
                        runner=runner,
                        websocket=websocket,
                        session_id=cmd.session_id,
                        user_id=user_id,
                        scene_number=int(points[-2].get("scene_number", 0) or 0),
                        source="button",
                    )
                elif storage_session.state.get("state_snapshots"):
                    _rollback_snapshot(storage_session.state)
                    cache_storybook_state(cmd.session_id, dict(storage_session.state))
                    await websocket.send_text(
                        ServerEvent(
                            type=ServerEventType.REWIND_COMPLETE,
                            payload={
                                "scene_history": _scene_branch_public_payload(storage_session.state),
                                "current_scene_image_url": storage_session.state.get("scene_asset_urls", [""])[-1] if storage_session.state.get("scene_asset_urls") else None,
                                "current_scene_storybeat_text": str(storage_session.state.get("current_scene_storybeat_text", "") or "").strip(),
                                "current_scene_description": str(storage_session.state.get("current_scene_description", "") or "").strip(),
                                "source": "button",
                            },
                        ).model_dump_json()
                    )

    elif cmd.type == ClientCommandType.BRANCH_TO_SCENE:
        scene_number = int(cmd.payload.get("scene_number", 0) or 0)
        if scene_number > 0:
            await _branch_story_to_scene(
                runner=runner,
                websocket=websocket,
                session_id=cmd.session_id,
                user_id=user_id,
                scene_number=scene_number,
                source=str(cmd.payload.get("source", "button") or "button"),
            )

    elif cmd.type == ClientCommandType.END_STORY:
        await _persist_storybook_elevenlabs_voice_id(
            runner=runner,
            user_id=user_id,
            session_id=cmd.session_id,
            voice_id=_payload_storybook_elevenlabs_voice_id(cmd.payload),
        )
        await _trigger_story_end(
            runner=runner,
            session_id=cmd.session_id,
            live_queue=live_queue,
            websocket=websocket,
            user_id=user_id,
            notify_frontend=False,
        )

    elif cmd.type == ClientCommandType.ASSEMBLY_PLAY_PROMPT:
        try:
            current_session = await runner.session_service.get_session(
                app_name="storyteller",
                user_id=user_id,
                session_id=cmd.session_id,
            )
        except Exception:
            current_session = None
        state = dict(current_session.state) if current_session and current_session.state else {}
        if not _storybook_assembly_in_progress(state):
            return
        activity = str(cmd.payload.get("activity", "") or "").strip().lower()
        label = str(cmd.payload.get("label", "") or "").strip()

        def _mark_pending_assembly_play(s: dict[str, Any]) -> None:
            s["pending_response"] = True
            if label:
                s["last_child_utterance"] = label
            _remember_assembly_activity(s, activity)

        await _mutate_state(
            runner=runner,
            user_id=user_id,
            session_id=cmd.session_id,
            mutator=_mark_pending_assembly_play,
        )
        _send_assembly_activity_prompt(
            session_id=cmd.session_id,
            live_queue=live_queue,
            state=state,
            activity=activity,
            label=label,
        )

    elif cmd.type == ClientCommandType.TOY_SHARE_START:
        await _trigger_toy_share_start(
            runner=runner,
            session_id=cmd.session_id,
            user_id=user_id,
            live_queue=live_queue,
            open_overlay=False,
            source="button",
            send_prompt=True,
        )

    elif cmd.type == ClientCommandType.TOY_SHARE_END:
        await _trigger_toy_share_end(
            runner=runner,
            session_id=cmd.session_id,
            user_id=user_id,
            live_queue=live_queue,
            close_overlay=False,
            source="button",
            send_resume_prompt=False,
        )

    elif cmd.type == ClientCommandType.SPYGLASS_IMAGE:
        gcs_url = cmd.payload.get("gcs_url", "")
        if isinstance(gcs_url, str) and gcs_url:
            image_bytes = await _download_gcs_to_bytes(gcs_url)
            if image_bytes:
                toy_visual_summary = await _describe_shared_item_image(image_bytes)
                current_session = await runner.session_service.get_session(
                    app_name="storyteller",
                    user_id=user_id,
                    session_id=cmd.session_id,
                )
                toy_share_active = False
                toy_name_hint = ""
                if current_session and current_session.state:
                    try:
                        toy_share_active = bool(current_session.state.get("toy_share_active", False))
                        toy_name_hint = _extract_shared_toy_name_hint(
                            current_session.state.get("partial_child_utterance")
                            or current_session.state.get("last_child_utterance")
                            or ""
                        )
                    except Exception:
                        toy_share_active = False
                        toy_name_hint = ""
                toy_thumb = await _detect_shared_item_subject_thumbnail(
                    image_bytes,
                    toy_name_hint=toy_name_hint,
                    summary_text=toy_visual_summary,
                )
                if toy_thumb is None:
                    toy_thumb = _make_thumbnail_b64(image_bytes)
                summary_text = toy_visual_summary or "The photo looks a little fuzzy, but it seems like a very special toy."
                _send_live_content(
                    session_id,
                    live_queue,
                    (
                        "The child just shared a photo of a toy or special item. "
                        + (
                            f"The child calls it {toy_name_hint}. "
                            if toy_name_hint else
                            ""
                        )
                        + f"Visible details: {summary_text} "
                        + (
                            "This is an active toy show-and-tell moment. "
                            "Warmly notice one or two of those details, make the child feel proud for sharing it, "
                            "and ask exactly one simple follow-up question about the toy. "
                            "Do not advance the story yet."
                            if toy_share_active else
                            "Warmly acknowledge it in one short sentence, describe what you can see in simple kid-friendly words, "
                            "and weave it into the ongoing story as their helper or sidekick. "
                            "Do not restart onboarding or ask camera questions."
                        )
                    ),
                )
                def _save_spyglass(state: dict[str, Any]) -> None:
                    _apply_shared_toy_story_state(
                        state,
                        summary_text=summary_text,
                        toy_name_hint=toy_name_hint,
                        toy_thumb=toy_thumb,
                    )

                await _mutate_state(
                    runner=runner,
                    user_id=user_id,
                    session_id=cmd.session_id,
                    mutator=_save_spyglass,
                )

    elif cmd.type == ClientCommandType.MOVIE_FEEDBACK:
        rating = _normalize_movie_feedback_rating(cmd.payload.get("rating"))
        reasons = _normalize_movie_feedback_reasons(cmd.payload.get("reasons"))
        note = str(cmd.payload.get("note", "") or "").strip()[:500]
        firestore_state = _load_storybook_firestore_state(cmd.session_id)
        session = await runner.session_service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=cmd.session_id,
        )
        merged_state = dict(firestore_state or {})
        if session and session.state:
            try:
                merged_state.update(dict(session.state))
            except Exception:
                pass
        schedule_background_task(
            asyncio.to_thread(
                _record_movie_feedback_sync,
                cmd.session_id,
                merged_state,
                rating=rating,
                reasons=reasons,
                note=note,
            )
        )

    elif cmd.type == ClientCommandType.MOVIE_REMAKE:
        rating = _normalize_movie_feedback_rating(cmd.payload.get("rating"))
        reasons = _normalize_movie_feedback_reasons(cmd.payload.get("reasons"))
        note = str(cmd.payload.get("note", "") or "").strip()[:500]
        storybook_elevenlabs_voice_id = _payload_storybook_elevenlabs_voice_id(cmd.payload)
        await _persist_storybook_elevenlabs_voice_id(
            runner=runner,
            user_id=user_id,
            session_id=cmd.session_id,
            voice_id=storybook_elevenlabs_voice_id,
        )
        firestore_state = _load_storybook_firestore_state(cmd.session_id)
        session = await runner.session_service.get_session(
            app_name="storyteller",
            user_id=user_id,
            session_id=cmd.session_id,
        )
        merged_state = dict(firestore_state or {})
        if session and session.state:
            try:
                merged_state.update(dict(session.state))
            except Exception:
                pass
        if storybook_elevenlabs_voice_id:
            merged_state["storybook_elevenlabs_voice_id"] = storybook_elevenlabs_voice_id
        if rating or reasons or note:
            schedule_background_task(
                asyncio.to_thread(
                    _record_movie_feedback_sync,
                    cmd.session_id,
                    merged_state,
                    rating=rating,
                    reasons=reasons,
                    note=note,
                )
            )
        story_title, child_name = _storybook_identity_from_state(merged_state)
        started_at_epoch_ms = int(time.time() * 1000)
        _update_storybook_firestore(
            cmd.session_id,
            {
                "assembly_kind": "remake",
                "assembly_status": "assembling",
                "assembly_started_at_epoch_ms": started_at_epoch_ms,
                "assembly_recent_activities": [],
                "assembly_wait_prompt_count": 0,
                "scene_render_pending": False,
                "theater_release_ready": False,
                "story_phase": "remake",
                "movie_remake_requested_at_epoch": int(time.time()),
                "movie_remake_feedback": {
                    "rating": rating,
                    "reasons": reasons,
                    "note": note,
                },
            },
        )
        cache_storybook_state(
            cmd.session_id,
            {
                **merged_state,
                "assembly_kind": "remake",
                "assembly_status": "assembling",
                "assembly_started_at_epoch_ms": started_at_epoch_ms,
                "assembly_recent_activities": [],
                "assembly_wait_prompt_count": 0,
                "scene_render_pending": False,
                "theater_release_ready": False,
                "story_phase": "remake",
            },
        )
        reset_storybook_assembly_lock(cmd.session_id)
        await _announce_storybook_assembly_started(
            websocket=websocket,
            session_id=cmd.session_id,
            eta_seconds=45 if _env_enabled("ENABLE_FAST_STORYBOOK_ASSEMBLY", default=False) else 90,
            story_title=story_title,
            child_name=child_name,
            kind="remake",
            started_at_epoch_ms=started_at_epoch_ms,
        )
        asyncio.create_task(assemble_story_video(session_id=cmd.session_id))

    elif cmd.type == ClientCommandType.THEATER_CLOSE:
        live_queue.close()

    elif cmd.type == ClientCommandType.IOT_CONFIG:
        config = cmd.payload.get("config", {})
        if isinstance(config, dict):
            set_session_iot_config(cmd.session_id, config)

    elif cmd.type == ClientCommandType.HEARTBEAT:
        _mark_client_transport_activity(cmd.session_id, heartbeat=True)
        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "heartbeat_ack",
                        "payload": {"server_ts_ms": int(time.time() * 1000)},
                    }
                )
            )
        except Exception as exc:
            logger.debug("Failed to send heartbeat ack: %s", exc)


async def _heartbeat(websocket: WebSocket) -> None:
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "heartbeat",
                        "payload": {
                            "ping": True,
                            "server_ts_ms": int(time.time() * 1000),
                        },
                    }
                )
            )
        except asyncio.CancelledError:
            break
        except WebSocketDisconnect:
            break
        except Exception as exc:
            logger.debug("Background heartbeat ping failed: %s", exc)


async def _heartbeat_watchdog(
    websocket: WebSocket,
    session_id: str,
    connection_id: str,
) -> None:
    while True:
        try:
            await asyncio.sleep(max(5, _HEARTBEAT_INTERVAL // 2))
            if not _connection_is_current(session_id, connection_id, websocket):
                return
            now = time.monotonic()
            last_transport = _last_client_transport_at.get(session_id, now)
            last_heartbeat = _last_client_heartbeat_at.get(session_id, last_transport)
            transport_idle = now - last_transport
            heartbeat_idle = now - last_heartbeat
            if transport_idle > _CLIENT_TRANSPORT_STALE_SECONDS:
                _forced_disconnect_reasons[session_id] = "transport_stale"
                _bump_live_telemetry("disconnect.transport_stale")
                _emit_live_telemetry(
                    "websocket_disconnect",
                    session_id=session_id,
                    include_runtime=True,
                    reason="transport_stale",
                    idle_seconds=round(transport_idle, 2),
                )
                logger.warning(
                    "Closing stale websocket for session %s after %.2fs without client transport activity.",
                    session_id,
                    transport_idle,
                )
                await websocket.close(code=4005, reason="client transport stale")
                return
            if (
                heartbeat_idle > _CLIENT_HEARTBEAT_STALE_SECONDS
                and transport_idle > (_HEARTBEAT_INTERVAL * 2)
            ):
                _forced_disconnect_reasons[session_id] = "heartbeat_stale"
                _bump_live_telemetry("disconnect.heartbeat_stale")
                _emit_live_telemetry(
                    "websocket_disconnect",
                    session_id=session_id,
                    include_runtime=True,
                    reason="heartbeat_stale",
                    idle_seconds=round(heartbeat_idle, 2),
                )
                logger.warning(
                    "Closing websocket for session %s after %.2fs without heartbeat.",
                    session_id,
                    heartbeat_idle,
                )
                await websocket.close(code=4004, reason="heartbeat stale")
                return
        except asyncio.CancelledError:
            return
        except WebSocketDisconnect:
            return
        except Exception as exc:
            logger.debug("Heartbeat watchdog failed for %s: %s", session_id, exc)
            return


async def _forward_session_events(
    websocket: WebSocket,
    runner: Runner,
    user_id: str,
    session_id: str,
    connection_id: str,
    live_queue: LiveRequestQueue,
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
                    is_placeholder = bool(payload.get("is_placeholder"))
                    is_fallback = bool(payload.get("is_fallback"))
                    persist_asset = payload.get("persist_asset", True) is not False
                    request_id = str(payload.get("request_id", "") or "").strip()
                    if not persist_asset:
                        storage_session = _get_storage_session(runner, user_id, session_id)
                        storage_state = storage_session.state if storage_session is not None else {}
                        if _should_ignore_nonpersistent_scene_ready(
                            storage_state,
                            request_id=request_id,
                        ):
                            logger.info(
                                "Dropping stale nonpersistent scene_ready event for session %s while a newer replacement/page request is active.",
                                session_id,
                            )
                            continue
                    queued_follow_up_text = ""
                    # Mark that this session has received at least one real image.
                    if not is_placeholder:
                        _session_has_any_image.add(session_id)
                    media_type = str(payload.get("media_type", "")).lower().strip()
                    looks_like_image = (
                        media_type == "image"
                        or url.startswith("data:image")
                        or bool(re.search(r"\.(png|jpe?g|webp|gif|svg)(\?|$)", url, flags=re.IGNORECASE))
                    )
                    asset_key = "scene_asset_urls" if looks_like_image else "generated_asset_urls"

                    def _append_asset(state: dict[str, Any]) -> None:
                        nonlocal queued_follow_up_text
                        urls = state.get(asset_key, [])
                        if not isinstance(urls, list):
                            urls = []
                        if looks_like_image:
                            description = str(payload.get("description", "")).strip()
                            gcs_uri = str(payload.get("gcs_uri", "") or "").strip()
                            storybeat_text = str(payload.get("storybeat_text", "") or "").strip()
                            _apply_scene_asset_to_story_state(
                                state,
                                request_id=request_id,
                                image_url=url,
                                description=description,
                                storybeat_text=storybeat_text,
                                gcs_uri=gcs_uri,
                            )
                            state["scene_render_pending"] = False
                            queued_follow_up_text = _arm_queued_scene_follow_up_after_render(state)
                        else:
                            if not urls or urls[-1] != url:
                                urls.append(url)
                            state[asset_key] = urls[-40:]
                        thumb_b64 = payload.get("thumbnail_b64")
                        thumb_mime = payload.get("thumbnail_mime")
                        if isinstance(thumb_b64, str) and thumb_b64:
                            state["previous_scene_thumbnail_b64"] = thumb_b64
                            state["previous_scene_thumbnail_mime"] = (
                                str(thumb_mime) if isinstance(thumb_mime, str) else "image/jpeg"
                            )
                            if not str(state.get("canonical_scene_thumbnail_b64", "") or "").strip():
                                state["canonical_scene_thumbnail_b64"] = thumb_b64
                                state["canonical_scene_thumbnail_mime"] = (
                                    str(thumb_mime) if isinstance(thumb_mime, str) else "image/jpeg"
                                )
                        _sync_story_phase(session_id, state)

                    if not is_placeholder and persist_asset:
                        await _mutate_state(
                            runner,
                            user_id,
                            session_id,
                            _append_asset,
                        )
                        storage_session = _get_storage_session(runner, user_id, session_id)
                        if storage_session is not None and storage_session.state:
                            _sync_story_phase(session_id, storage_session.state)
                            persisted_state = dict(storage_session.state)
                            persisted_scene_urls = [
                                str(candidate or "").strip()
                                for candidate in list(persisted_state.get("scene_asset_urls", []) or [])
                                if str(candidate or "").strip() and not str(candidate or "").strip().startswith("data:")
                            ]
                            cache_storybook_state(session_id, persisted_state)
                            _update_storybook_firestore(
                                session_id,
                                {
                                    "scene_asset_urls": persisted_scene_urls,
                                    "scene_asset_gcs_uris": list(persisted_state.get("scene_asset_gcs_uris", []) or []),
                                    "scene_descriptions": list(persisted_state.get("scene_descriptions", []) or []),
                                    "scene_storybeat_texts": list(persisted_state.get("scene_storybeat_texts", []) or []),
                                    "current_scene_description": str(persisted_state.get("current_scene_description", "") or "").strip(),
                                    "current_scene_storybeat_text": str(persisted_state.get("current_scene_storybeat_text", "") or "").strip(),
                                    "story_pages": _story_pages_payload(persisted_state),
                                    "story_phase": str(persisted_state.get("story_phase", "") or "").strip(),
                                },
                            )
                    elif is_placeholder:
                        def _mark_scene_render_pending(state: dict[str, Any]) -> None:
                            state["scene_render_pending"] = True
                            _sync_story_phase(session_id, state)

                        await _mutate_state(
                            runner,
                            user_id,
                            session_id,
                            _mark_scene_render_pending,
                        )
                    else:
                        def _clear_scene_render_pending(state: dict[str, Any]) -> None:
                            _apply_nonpersistent_scene_ready_to_state(
                                state,
                                request_id=request_id,
                                looks_like_image=looks_like_image,
                                is_fallback=is_fallback,
                                description=str(payload.get("description", "") or "").strip(),
                                storybeat_text=str(payload.get("storybeat_text", "") or "").strip(),
                            )
                            _sync_story_phase(session_id, state)

                        await _mutate_state(
                            runner,
                            user_id,
                            session_id,
                            _clear_scene_render_pending,
                        )
                    outbound_payload = dict(payload)
                    if url.startswith("data:image"):
                        outbound_payload.pop("thumbnail_b64", None)
                        outbound_payload.pop("thumbnail_mime", None)
                    outbound_payload.pop("gcs_uri", None)
                    outbound_payload["url"] = url
                    storage_session = _get_storage_session(runner, user_id, session_id)
                    if storage_session is not None:
                        outbound_payload["scene_history"] = _scene_branch_public_payload(storage_session.state)
                    logger.debug("Sending video_ready event to websocket loop for session %s. Media type: %s", session_id, media_type)
                    await websocket.send_text(
                        json.dumps({"type": "video_ready", "payload": outbound_payload})
                    )
                    # Signal the downstream loop that the image has arrived.
                    if not is_placeholder:
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
                        if queued_follow_up_text:
                            logger.info(
                                "Replaying queued child scene request after render complete for session %s: %s",
                                session_id,
                                queued_follow_up_text[:160],
                            )
                            _send_queued_scene_follow_up_prompt(
                                session_id,
                                live_queue,
                                queued_follow_up_text,
                            )
                else:
                    logger.debug("video_ready event for session %s had an empty URL?!", session_id)
                continue

            if event_type == "theater_mode":
                if isinstance(payload, dict):
                    final_video_url = str(payload.get("mp4_url", "") or "").strip()
                    trading_card_url = str(payload.get("trading_card_url", "") or "").strip()
                    narration_raw = payload.get("narration_lines")
                    narration_lines = (
                        [str(line).strip() for line in narration_raw if isinstance(line, str) and str(line).strip()]
                        if isinstance(narration_raw, list)
                        else None
                    )
                    lighting_cues_raw = payload.get("lighting_cues")
                    lighting_cues = (
                        [dict(item) for item in lighting_cues_raw if isinstance(item, dict)]
                        if isinstance(lighting_cues_raw, list)
                        else None
                    )
                    audio_available = payload.get("audio_available")
                    final_has_audio_stream = payload.get("final_has_audio_stream")
                    final_video_duration_sec = payload.get("final_video_duration_sec")
                    theater_release_ready = payload.get("theater_release_ready")

                    def _mark_theater_ready(state: dict[str, Any]) -> None:
                        state["assembly_status"] = "complete"
                        state["scene_render_pending"] = False
                        _clear_pending_scene_request_metadata(state)
                        if final_video_url:
                            state["final_video_url"] = final_video_url
                        if trading_card_url:
                            state["trading_card_url"] = trading_card_url
                        if narration_lines is not None:
                            state["narration_lines"] = narration_lines
                        if lighting_cues is not None:
                            state["theater_lighting_cues"] = lighting_cues
                        if isinstance(audio_available, bool):
                            state["audio_available"] = audio_available
                        if isinstance(final_has_audio_stream, bool):
                            state["final_has_audio_stream"] = final_has_audio_stream
                        try:
                            duration_seconds = float(final_video_duration_sec or 0.0)
                        except Exception:
                            duration_seconds = 0.0
                        if duration_seconds > 0.0:
                            state["final_video_duration_sec"] = duration_seconds
                        if isinstance(theater_release_ready, bool):
                            state["theater_release_ready"] = theater_release_ready
                        _sync_story_phase(session_id, state)

                    await _mutate_state(
                        runner=runner,
                        user_id=user_id,
                        session_id=session_id,
                        mutator=_mark_theater_ready,
                    )
                    storage_session = _get_storage_session(runner, user_id, session_id)
                    if storage_session is not None and storage_session.state:
                        persisted_state = dict(storage_session.state)
                        cache_storybook_state(session_id, persisted_state)
                        _update_storybook_firestore(
                            session_id,
                            {
                                "assembly_status": str(persisted_state.get("assembly_status", "") or "").strip(),
                                "final_video_url": str(persisted_state.get("final_video_url", "") or "").strip(),
                                "trading_card_url": str(persisted_state.get("trading_card_url", "") or "").strip(),
                                "narration_lines": list(persisted_state.get("narration_lines", []) or []),
                                "theater_lighting_cues": list(persisted_state.get("theater_lighting_cues", []) or []),
                                "audio_available": persisted_state.get("audio_available"),
                                "final_has_audio_stream": persisted_state.get("final_has_audio_stream"),
                                "final_video_duration_sec": persisted_state.get("final_video_duration_sec"),
                                "theater_release_ready": persisted_state.get("theater_release_ready"),
                                "story_phase": str(persisted_state.get("story_phase", "") or "").strip(),
                            },
                        )
                _ending_story_sessions.discard(session_id)
                _ending_story_flush_sessions.discard(session_id)
                _assembly_intro_sent_sessions.discard(session_id)

            await websocket.send_text(json.dumps(event))
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.debug("Session event forwarder exited: %s", exc)
