"""FastAPI entry point for the StorySpark backend.

Key design decisions:
- Uses pydantic-settings for strict env validation (Iter 2 #5 — server cannot
  start without required API keys being mounted from Secret Manager).
- Wraps root agent in ADK App class (Iter 2 #3 — App Containerization).
- Session pool with LiveRequestQueue + queue_size limit to return 503 gracefully
  on quota exhaustion (Iter 2 #1 — Live API Quota Exhaustion).
- min_instances cloud run warm ensured via Terraform — this app does NOT sleep.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import io
import json
import logging
import os
import re
import sys
import threading
import time
import wave
from collections import OrderedDict
from html import escape as html_escape
from typing import Any
from urllib.parse import urlparse

import uvicorn
import google.auth
import httpx
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from google.adk.artifacts import GcsArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google import genai as google_genai
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ensure the project root (google-prog/) is on sys.path so `agent` package resolves
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agent.storyteller_agent import app as storyteller_app, storyteller_agent  # noqa: E402
from shared.storybook_page_read_aloud_workflow import (  # noqa: E402
    PageReadAloudRequest,
    run_storybook_page_read_aloud_workflow,
)
from shared.storybook_movie_quality import (  # noqa: E402
    BURNED_CAPTIONS_DEFAULT,
    MUSIC_VOLUME_DEFAULT,
    NARRATION_REQUIRED_DEFAULT,
    PAGE_SECONDS_DEFAULT,
    SFX_MAX_DEFAULT,
    SFX_VOLUME_DEFAULT,
)
from .live_client_mode import (  # noqa: E402
    DEFAULT_LIVE_MODEL,
    DEFAULT_LIVE_TEMPERATURE,
    DEFAULT_LIVE_TOP_P,
    DEFAULT_LIVE_VOICE,
    build_live_ephemeral_token_config,
    clamp_live_temperature,
    clamp_live_top_p,
)
from .event_bus import set_main_loop
from .media_cache import get_media
from .ws_router import get_live_telemetry_snapshot, handle_storyteller_ws  # relative import within backend pkg

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Silence noisy third-party loggers (audio chunk spam, HTTP wire logs, etc.)
for _noisy in [
    "websockets",
    "websockets.client",
    "httpcore",
    "httpcore.http11",
    "httpx",
    "google_adk",
    "google.adk.flows.llm_flows.base_llm_flow",
    "google.adk.flows.llm_flows.audio_cache_manager",
    "google.adk.models.gemini_llm_connection",
    "google.adk.runners",
    "google.adk.agents",
]:
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ── Strict environment validation (Iter 2 #5) ─────────────────────────────────
class Settings(BaseSettings):
    """Application settings. Server literally cannot start without these."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_api_key: str
    google_cloud_project: str
    google_cloud_location: str = "us-central1"
    vertex_ai_location: str = "global"
    google_genai_use_vertexai: bool = True
    elevenlabs_api_key: str
    gcs_assets_bucket: str = "storyteller-session-assets"
    gcs_final_videos_bucket: str = "storyteller-final-videos"
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    home_assistant_mcp_url: str = ""
    ffmpeg_job_name: str = "storyteller-ffmpeg-assembler"
    firestore_database: str = "(default)"
    frontend_origin: str = "http://localhost:3000"
    prod_frontend_origin: str = ""
    # Maximum concurrent Live API sessions (prevents quota exhaustion)
    max_live_sessions: int = 50
    parent_gate_pin: str = ""
    # Local storybook (dev)
    local_storybook_mode: bool = False
    enable_fast_storybook_assembly: bool = False
    storybook_page_seconds: int = int(PAGE_SECONDS_DEFAULT)
    storybook_title: str = "auto"
    enable_storybook_tts: bool = NARRATION_REQUIRED_DEFAULT
    enable_storybook_captions: bool = BURNED_CAPTIONS_DEFAULT
    storybook_tts_lang: str = "en-US"
    storybook_tts_voice: str = "en-US-Neural2-F"
    storybook_tts_rate: float = 0.9
    storybook_tts_pitch: float = 0.0
    force_storybook_tts: bool = NARRATION_REQUIRED_DEFAULT
    enable_storybook_music: bool = True
    storybook_music_provider: str = "auto"
    storybook_music_volume: float = MUSIC_VOLUME_DEFAULT
    enable_storybook_sfx: bool = True
    storybook_sfx_provider: str = "auto"
    storybook_sfx_volume: float = SFX_VOLUME_DEFAULT
    storybook_sfx_max: int = SFX_MAX_DEFAULT
    storybook_sfx_min_score: int = 2
    storybook_sfx_cooldown: int = 1
    enable_storybook_audio_mastering: bool = True
    elevenlabs_music_endpoint: str = "https://api.elevenlabs.io/v1/music"
    elevenlabs_sound_endpoint: str = "https://api.elevenlabs.io/v1/sound-generation"
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    elevenlabs_tts_model: str = "eleven_multilingual_v2"
    elevenlabs_tts_endpoint: str = ""
    enable_client_direct_live: bool = False
    client_direct_live_model: str = DEFAULT_LIVE_MODEL
    client_direct_live_voice: str = DEFAULT_LIVE_VOICE
    client_direct_live_temperature: float = DEFAULT_LIVE_TEMPERATURE
    client_direct_live_top_p: float = DEFAULT_LIVE_TOP_P
    client_direct_live_expire_minutes: int = 30
    client_direct_live_new_session_minutes: int = 1
    page_read_aloud_provider: str = "auto"
    page_read_aloud_model: str = "gemini-2.5-flash-tts"
    page_read_aloud_voice: str = "Sulafat"
    page_read_aloud_language_code: str = "en-GB"
    page_read_aloud_elevenlabs_voice_id: str = ""
    page_read_aloud_elevenlabs_model: str = ""
    page_read_aloud_elevenlabs_endpoint: str = ""
    page_read_aloud_elevenlabs_timestamps_endpoint: str = ""


settings = Settings()

# Inject into environment for ADK and tool functions
os.environ["GOOGLE_API_KEY"] = settings.google_api_key
os.environ["GOOGLE_CLOUD_PROJECT"] = settings.google_cloud_project
os.environ["GOOGLE_CLOUD_LOCATION"] = settings.google_cloud_location
os.environ["VERTEX_AI_LOCATION"] = settings.vertex_ai_location
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE" if settings.google_genai_use_vertexai else "FALSE"
os.environ["ELEVENLABS_API_KEY"] = settings.elevenlabs_api_key
os.environ["GCS_ASSETS_BUCKET"] = settings.gcs_assets_bucket
os.environ["HOME_ASSISTANT_URL"] = settings.home_assistant_url
os.environ["HOME_ASSISTANT_TOKEN"] = settings.home_assistant_token
os.environ["HOME_ASSISTANT_MCP_URL"] = settings.home_assistant_mcp_url
os.environ["FFMPEG_JOB_NAME"] = settings.ffmpeg_job_name
os.environ["FIRESTORE_DATABASE"] = settings.firestore_database
os.environ["GCS_FINAL_VIDEOS_BUCKET"] = settings.gcs_final_videos_bucket
os.environ["FRONTEND_ORIGIN"] = settings.frontend_origin
os.environ["PROD_FRONTEND_ORIGIN"] = settings.prod_frontend_origin
os.environ["LOCAL_STORYBOOK_MODE"] = "1" if settings.local_storybook_mode else "0"
os.environ["ENABLE_FAST_STORYBOOK_ASSEMBLY"] = "1" if settings.enable_fast_storybook_assembly else "0"
os.environ["STORYBOOK_PAGE_SECONDS"] = str(settings.storybook_page_seconds)
os.environ["STORYBOOK_TITLE"] = settings.storybook_title
os.environ["ENABLE_STORYBOOK_TTS"] = "1" if settings.enable_storybook_tts else "0"
os.environ["ENABLE_STORYBOOK_CAPTIONS"] = "1" if settings.enable_storybook_captions else "0"
os.environ["STORYBOOK_TTS_LANG"] = settings.storybook_tts_lang
os.environ["STORYBOOK_TTS_VOICE"] = settings.storybook_tts_voice
os.environ["STORYBOOK_TTS_RATE"] = str(settings.storybook_tts_rate)
os.environ["STORYBOOK_TTS_PITCH"] = str(settings.storybook_tts_pitch)
os.environ["FORCE_STORYBOOK_TTS"] = "1" if settings.force_storybook_tts else "0"
os.environ["ENABLE_STORYBOOK_MUSIC"] = "1" if settings.enable_storybook_music else "0"
os.environ["STORYBOOK_MUSIC_PROVIDER"] = settings.storybook_music_provider
os.environ["STORYBOOK_MUSIC_VOLUME"] = str(settings.storybook_music_volume)
os.environ["ENABLE_STORYBOOK_SFX"] = "1" if settings.enable_storybook_sfx else "0"
os.environ["STORYBOOK_SFX_PROVIDER"] = settings.storybook_sfx_provider
os.environ["STORYBOOK_SFX_VOLUME"] = str(settings.storybook_sfx_volume)
os.environ["STORYBOOK_SFX_MAX"] = str(settings.storybook_sfx_max)
os.environ["STORYBOOK_SFX_MIN_SCORE"] = str(settings.storybook_sfx_min_score)
os.environ["STORYBOOK_SFX_COOLDOWN"] = str(settings.storybook_sfx_cooldown)
os.environ["ENABLE_STORYBOOK_AUDIO_MASTERING"] = "1" if settings.enable_storybook_audio_mastering else "0"
os.environ["ELEVENLABS_MUSIC_ENDPOINT"] = settings.elevenlabs_music_endpoint
os.environ["ELEVENLABS_SOUND_ENDPOINT"] = settings.elevenlabs_sound_endpoint
os.environ["ELEVENLABS_VOICE_ID"] = settings.elevenlabs_voice_id
os.environ["ELEVENLABS_TTS_MODEL"] = settings.elevenlabs_tts_model
os.environ["ELEVENLABS_TTS_ENDPOINT"] = settings.elevenlabs_tts_endpoint
os.environ["PAGE_READ_ALOUD_PROVIDER"] = settings.page_read_aloud_provider
os.environ["PAGE_READ_ALOUD_ELEVENLABS_VOICE_ID"] = settings.page_read_aloud_elevenlabs_voice_id
os.environ["PAGE_READ_ALOUD_ELEVENLABS_MODEL"] = settings.page_read_aloud_elevenlabs_model
os.environ["PAGE_READ_ALOUD_ELEVENLABS_ENDPOINT"] = settings.page_read_aloud_elevenlabs_endpoint
os.environ["PAGE_READ_ALOUD_ELEVENLABS_TIMESTAMPS_ENDPOINT"] = settings.page_read_aloud_elevenlabs_timestamps_endpoint


# ── Build the ADK Runner ───────────────────────────────────────────────────────
def _create_runner() -> Runner:
    """Builds the ADK Runner with Firestore sessions and GCS artifact storage."""
    session_service = InMemorySessionService()  # ADK in-process session state — no Redis needed
    artifact_service = GcsArtifactService(
        bucket_name=settings.gcs_assets_bucket
    )
    common_runner_kwargs = dict(
        session_service=session_service,
        artifact_service=artifact_service,
    )
    try:
        logger.info("Creating ADK Runner with resumable App container.")
        return Runner(
            app=storyteller_app,
            app_name="storyteller",
            **common_runner_kwargs,
        )
    except TypeError:
        try:
            logger.info("Creating ADK Runner with resumable App container (without explicit app_name).")
            return Runner(
                app=storyteller_app,
                **common_runner_kwargs,
            )
        except TypeError:
            logger.warning("ADK Runner does not accept app=...; falling back to agent-only runner without explicit App resumability.")
            return Runner(
                agent=storyteller_agent,
                app_name="storyteller",
                **common_runner_kwargs,
            )


# ── FastAPI App ────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Voxitale API",
    description="Real-time Bidi-streaming picture-storytelling agent for young children.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin for origin in [settings.frontend_origin, settings.prod_frontend_origin] if origin],
    allow_origin_regex=r"^https://storyteller-frontend-[a-z0-9-]+\.(?:[a-z0-9-]+\.)?run\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Runner is created once at startup and shared across all connections
_runner: Runner | None = None
_active_sessions: int = 0
_cleanup_task = None
_SESSION_TTL_SECONDS = 24 * 60 * 60
_CLEANUP_INTERVAL_SECONDS = 300
_PAGE_READ_ALOUD_CACHE_MAX_ITEMS = 24
_PAGE_READ_ALOUD_WORD_TIMESTAMPS_HEADER = "X-StorySpark-Word-Starts-Ms"
_HOME_ASSISTANT_RELAY_TIMEOUT_SECONDS = 5.0
_HOME_ASSISTANT_RESTORE_DELAY_SECONDS = 0.9
_FRONTEND_RUN_APP_ORIGIN_RE = re.compile(
    r"^https://storyteller-frontend-[a-z0-9-]+\.(?:[a-z0-9-]+\.)?run\.app$"
)
_page_read_aloud_cache: OrderedDict[str, tuple[bytes, str, tuple[int, ...], str, str]] = OrderedDict()
_page_read_aloud_cache_lock = threading.Lock()


class HomeAssistantRelayConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ha_url: str = ""
    ha_token: str = ""
    ha_entity: str = "light.living_room"


class HomeAssistantRelayCommand(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hex_color: str | None = None
    rgb_color: list[int | float] | None = None
    entity: str | None = None
    brightness: int | float | None = None
    transition: int | float | None = None
    scene_description: str | None = None


class HomeAssistantApplyRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    config: HomeAssistantRelayConfig
    command: HomeAssistantRelayCommand


class HomeAssistantTestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    config: HomeAssistantRelayConfig


def _allowed_frontend_origins() -> set[str]:
    return {
        origin.strip()
        for origin in [settings.frontend_origin, settings.prod_frontend_origin]
        if origin and origin.strip()
    }


def _is_allowed_frontend_origin(origin: str) -> bool:
    normalized = origin.strip()
    if not normalized:
        return False
    if normalized in _allowed_frontend_origins():
        return True
    return bool(_FRONTEND_RUN_APP_ORIGIN_RE.fullmatch(normalized))


def _normalize_home_assistant_url(raw_url: str) -> str:
    return str(raw_url or "").strip().rstrip("/")


def _normalize_home_assistant_entity(raw_entity: str) -> str:
    return str(raw_entity or "").strip()


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


def _map_home_assistant_http_reason(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "entity_not_found"
    return "http_error"


def _coerce_home_assistant_rgb_color(value: Any) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None

    rgb: list[int] = []
    for item in value:
        try:
            number = float(item)
        except Exception:
            return None
        if number < 0 or number > 255:
            return None
        rgb.append(int(round(number)))
    return rgb


def _rgb_from_hex(hex_color: str | None) -> list[int] | None:
    normalized = str(hex_color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", normalized):
        return None
    return [
        int(normalized[0:2], 16),
        int(normalized[2:4], 16),
        int(normalized[4:6], 16),
    ]


def _home_assistant_failure_response(
    reason: str,
    *,
    status_code: int = 200,
    message: str | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "reason": reason, "transport": "backend"}
    if message:
        payload["message"] = message
    return JSONResponse(payload, status_code=status_code)


def _validate_backend_home_assistant_config(
    config: HomeAssistantRelayConfig,
) -> tuple[dict[str, str], None] | tuple[None, str]:
    ha_url = _normalize_home_assistant_url(config.ha_url)
    ha_token = str(config.ha_token or "").strip()
    ha_entity = _normalize_home_assistant_entity(config.ha_entity) or "light.living_room"

    if not ha_url:
        return None, "missing_url"
    if not ha_token:
        return None, "missing_token"
    if not ha_entity:
        return None, "missing_entity"

    parsed = urlparse(ha_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "invalid_url"
    if _ha_url_is_private_or_local(ha_url):
        return None, "private_url"
    if parsed.scheme != "https":
        return None, "insecure_url"

    return {
        "ha_url": ha_url,
        "ha_token": ha_token,
        "ha_entity": ha_entity,
    }, None


def _normalize_home_assistant_state_rgb(value: Any) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None

    rgb: list[int] = []
    for item in value:
        try:
            number = int(item)
        except Exception:
            return None
        rgb.append(number)
    return rgb


async def _home_assistant_request(
    client: httpx.AsyncClient,
    config: dict[str, str],
    path: str,
    *,
    method: str,
    json_body: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {config['ha_token']}",
        "Content-Type": "application/json",
    }
    return await client.request(
        method,
        f"{config['ha_url']}{path}",
        headers=headers,
        json=json_body,
    )


async def _load_home_assistant_state(
    client: httpx.AsyncClient,
    config: dict[str, str],
) -> dict[str, Any]:
    entity_id = _normalize_home_assistant_entity(config.get("ha_entity", "")) or "light.living_room"
    try:
        response = await _home_assistant_request(
            client,
            config,
            f"/api/states/{entity_id}",
            method="GET",
        )
    except httpx.HTTPError:
        return {"reason": "network"}

    if not response.is_success:
        return {"reason": _map_home_assistant_http_reason(response.status_code)}

    try:
        payload = response.json()
    except Exception:
        return {"reason": "invalid_response"}

    if not isinstance(payload, dict):
        return {"reason": "invalid_response"}

    attributes = payload.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}

    brightness: int | None = None
    raw_brightness = attributes.get("brightness")
    try:
        parsed_brightness = int(raw_brightness)
    except Exception:
        parsed_brightness = None
    if parsed_brightness is not None:
        brightness = parsed_brightness

    return {
        "entity_id": entity_id,
        "friendly_name": (
            str(attributes.get("friendly_name")).strip()
            if isinstance(attributes.get("friendly_name"), str)
            else entity_id
        ),
        "was_on": str(payload.get("state") or "").strip().lower() == "on",
        "brightness": brightness,
        "rgb_color": _normalize_home_assistant_state_rgb(attributes.get("rgb_color")),
    }


async def _apply_home_assistant_turn_on(
    client: httpx.AsyncClient,
    config: dict[str, str],
    command: HomeAssistantRelayCommand,
) -> dict[str, Any]:
    rgb_color = _coerce_home_assistant_rgb_color(command.rgb_color)
    if rgb_color is None:
        rgb_color = _rgb_from_hex(command.hex_color)
    if rgb_color is None:
        return {"ok": False, "reason": "invalid_color", "transport": "backend"}

    brightness = 200
    if command.brightness is not None:
        try:
            brightness = int(round(float(command.brightness)))
        except Exception:
            brightness = 200

    transition = 2.0
    if command.transition is not None:
        try:
            transition = float(command.transition)
        except Exception:
            transition = 2.0

    payload = {
        "entity_id": _normalize_home_assistant_entity(command.entity or config.get("ha_entity", "")) or "light.living_room",
        "rgb_color": rgb_color,
        "brightness": brightness,
        "transition": transition,
    }

    try:
        response = await _home_assistant_request(
            client,
            config,
            "/api/services/light/turn_on",
            method="POST",
            json_body=payload,
        )
    except httpx.HTTPError:
        return {"ok": False, "reason": "network", "transport": "backend"}

    if not response.is_success:
        return {
            "ok": False,
            "reason": _map_home_assistant_http_reason(response.status_code),
            "transport": "backend",
        }
    return {"ok": True, "transport": "backend"}


async def _restore_home_assistant_state(
    client: httpx.AsyncClient,
    config: dict[str, str],
    snapshot: dict[str, Any],
) -> bool:
    entity_id = _normalize_home_assistant_entity(str(snapshot.get("entity_id") or "")) or config["ha_entity"]
    try:
        if not bool(snapshot.get("was_on")):
            response = await _home_assistant_request(
                client,
                config,
                "/api/services/light/turn_off",
                method="POST",
                json_body={
                    "entity_id": entity_id,
                    "transition": 0.6,
                },
            )
            return response.is_success

        payload: dict[str, Any] = {
            "entity_id": entity_id,
            "transition": 0.6,
        }
        brightness = snapshot.get("brightness")
        if isinstance(brightness, int):
            payload["brightness"] = brightness
        rgb_color = _normalize_home_assistant_state_rgb(snapshot.get("rgb_color"))
        if rgb_color is not None:
            payload["rgb_color"] = rgb_color

        response = await _home_assistant_request(
            client,
            config,
            "/api/services/light/turn_on",
            method="POST",
            json_body=payload,
        )
        return response.is_success
    except httpx.HTTPError:
        return False


def _coerce_child_age(value: Any) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return max(3, min(parsed, 12))


def _page_read_aloud_delivery_hint(
    child_age: int | None,
    storybook_movie_pacing: str | None,
) -> str:
    pacing = str(storybook_movie_pacing or "").strip().lower()
    if pacing == "read_to_me":
        return "read slowly, gently, and clearly"
    if pacing == "fast_movie":
        return "read warmly and clearly, but a little brisker"
    if child_age is not None and child_age <= 5:
        return "read slowly, clearly, and warmly for a preschooler"
    if child_age is not None and child_age <= 7:
        return "read clearly, warmly, and at an easy early-reader pace"
    return "read clearly, warmly, and naturally"


def _build_page_read_aloud_prompt(
    text: str,
    *,
    child_age: int | None,
    storybook_movie_pacing: str | None,
) -> str:
    delivery_hint = _page_read_aloud_delivery_hint(child_age, storybook_movie_pacing)
    language_code = str(settings.page_read_aloud_language_code or "").strip().lower()
    child_hint = (
        f" for a {child_age}-year-old child"
        if child_age is not None
        else " for a young child"
    )
    accent_hint = (
        "Use a natural British-English accent. "
        if language_code.startswith("en-gb")
        else ""
    )
    return (
        "Read the following picture-book page aloud exactly as written"
        f"{child_hint}. "
        "Sound like a warm, attentive story reader. "
        f"{accent_hint}"
        "Honor punctuation with small natural pauses, keep complete sentences flowing clearly, "
        "and gently emphasize comforting or magical story words without becoming theatrical. "
        "Do not add any extra words, introductions, explanations, or sound effects. "
        f"{delivery_hint.capitalize()}. "
        "Page text:\n"
        f"{text}"
    )


def _page_read_aloud_prosody_rate(
    child_age: int | None,
    storybook_movie_pacing: str | None,
) -> str:
    pacing = str(storybook_movie_pacing or "").strip().lower()
    if pacing == "read_to_me":
        return "88%"
    if pacing == "fast_movie":
        return "100%"
    if child_age is not None and child_age <= 5:
        return "90%"
    if child_age is not None and child_age <= 7:
        return "94%"
    return "97%"


def _page_read_aloud_requested_provider() -> str:
    normalized = str(settings.page_read_aloud_provider or "").strip().lower()
    if normalized in {"google", "google_cloud_tts", "cloud-tts"}:
        return "google-cloud-tts"
    if normalized in {"elevenlabs", "google-cloud-tts"}:
        return normalized
    return "auto"


def _normalize_storybook_elevenlabs_voice_id(raw: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "").strip())
    if 20 <= len(cleaned) <= 64:
        return cleaned
    return ""


def _page_read_aloud_elevenlabs_voice_id(voice_override: str | None = None) -> str:
    normalized_override = _normalize_storybook_elevenlabs_voice_id(voice_override)
    return (
        normalized_override
        or str(settings.page_read_aloud_elevenlabs_voice_id or "").strip()
        or str(settings.elevenlabs_voice_id or "").strip()
    )


def _page_read_aloud_elevenlabs_fallback_voice_id(voice_override: str | None = None) -> str:
    normalized_override = _normalize_storybook_elevenlabs_voice_id(voice_override)
    default_voice_id = _page_read_aloud_elevenlabs_voice_id(None)
    if default_voice_id and default_voice_id != normalized_override:
        return default_voice_id
    return ""


def _page_read_aloud_elevenlabs_model() -> str:
    return (
        str(settings.page_read_aloud_elevenlabs_model or "").strip()
        or str(settings.elevenlabs_tts_model or "").strip()
        or "eleven_multilingual_v2"
    )


def _page_read_aloud_log_model_name(provider: str) -> str:
    if provider == "google-cloud-tts":
        return str(settings.page_read_aloud_model or "").strip()
    return _page_read_aloud_elevenlabs_model()


def _page_read_aloud_log_voice_name(provider: str, *, elevenlabs_voice_id: str | None = None) -> str:
    if provider == "google-cloud-tts":
        return str(settings.page_read_aloud_voice or "").strip()
    return _page_read_aloud_elevenlabs_voice_id(elevenlabs_voice_id)


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


def _tokenize_page_read_aloud_words(text: str) -> list[str]:
    return re.findall(r"\S+", " ".join(str(text or "").split()).strip())


def _normalize_page_read_aloud_word_token(text: str) -> str:
    return re.sub(r"^[^A-Za-z0-9']+|[^A-Za-z0-9']+$", "", str(text or "")).lower()


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


def _extract_page_read_aloud_elevenlabs_word_timings_seconds(
    payload: dict[str, Any],
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
                        max(0.0, current_start),
                        max(0.0, current_end),
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
                max(0.0, current_start),
                max(0.0, current_end),
            )
        )
    return timings


def _exact_page_read_aloud_word_starts_ms(
    text: str,
    word_timings: list[tuple[str, float, float]] | None,
) -> list[int]:
    words = _tokenize_page_read_aloud_words(text)
    if not words or not word_timings or len(words) != len(word_timings):
        return []
    normalized_words = [_normalize_page_read_aloud_word_token(word) for word in words]
    normalized_timings = [_normalize_page_read_aloud_word_token(word) for word, _, _ in word_timings]
    if any(expected != actual for expected, actual in zip(normalized_words, normalized_timings)):
        return []
    starts_ms: list[int] = []
    last_start_ms = 0
    for _word, start_s, _end_s in word_timings:
        start_ms = max(0, int(round(max(0.0, start_s) * 1000)))
        start_ms = max(last_start_ms, start_ms)
        starts_ms.append(start_ms)
        last_start_ms = start_ms
    return starts_ms


def _build_page_read_aloud_ssml(
    text: str,
    *,
    child_age: int | None,
    storybook_movie_pacing: str | None,
) -> tuple[str, list[str]]:
    words = _tokenize_page_read_aloud_words(text)
    if not words:
        return "<speak></speak>", []

    parts = [
        "<speak>",
        f'<prosody rate="{_page_read_aloud_prosody_rate(child_age, storybook_movie_pacing)}">',
    ]
    for index, word in enumerate(words):
        if index:
            parts.append(" ")
        parts.append(f'<mark name="w{index}"/>')
        parts.append(html_escape(word, quote=False))
    parts.extend(["</prosody>", "</speak>"])
    return "".join(parts), words


def _extract_audio_inline_data(response: Any) -> tuple[bytes | None, str | None]:
    candidates = getattr(response, "candidates", None) or []
    parts: list[Any] = []
    if candidates:
        try:
            parts = list(candidates[0].content.parts)
        except Exception:
            parts = []
    else:
        parts = list(getattr(response, "parts", []) or [])

    for part in parts:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline else None
        if data:
            mime_type = getattr(inline, "mime_type", None) if inline else None
            return data, str(mime_type or "").strip() or None
    return None, None


def _wrap_pcm_as_wav(pcm_bytes: bytes, *, sample_rate_hz: int = 24000) -> bytes:
    if pcm_bytes.startswith(b"RIFF"):
        return pcm_bytes
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate_hz)
        wav_file.writeframes(pcm_bytes)
    return buffer.getvalue()

def _page_read_aloud_token() -> str:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    if not credentials.valid or not credentials.token:
        credentials.refresh(GoogleAuthRequest())
    if not credentials.token:
        raise RuntimeError("Google Cloud auth did not return an access token.")
    return credentials.token


def _page_read_aloud_cache_key(
    text: str,
    child_age: int | None,
    storybook_movie_pacing: str | None,
    elevenlabs_voice_id: str | None = None,
) -> str:
    provider = _page_read_aloud_requested_provider()
    normalized = {
        "text": " ".join(str(text or "").split()).strip(),
        "child_age": child_age,
        "storybook_movie_pacing": str(storybook_movie_pacing or "").strip().lower() or None,
        "provider": provider,
        "google_model": settings.page_read_aloud_model,
        "google_voice": settings.page_read_aloud_voice,
        "language_code": settings.page_read_aloud_language_code,
        "elevenlabs_model": _page_read_aloud_elevenlabs_model(),
        "elevenlabs_voice_id": _page_read_aloud_elevenlabs_voice_id(elevenlabs_voice_id),
    }
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _page_read_aloud_cache_get(
    cache_key: str,
) -> tuple[bytes, str, tuple[int, ...], str, str] | None:
    with _page_read_aloud_cache_lock:
        cached = _page_read_aloud_cache.get(cache_key)
        if cached is None:
            return None
        _page_read_aloud_cache.move_to_end(cache_key)
        return cached


def _page_read_aloud_cache_put(
    cache_key: str,
    audio_bytes: bytes,
    media_type: str,
    word_starts_ms: list[int] | tuple[int, ...],
    provider: str,
    timing_mode: str,
) -> None:
    with _page_read_aloud_cache_lock:
        _page_read_aloud_cache[cache_key] = (
            audio_bytes,
            media_type,
            tuple(word_starts_ms),
            str(provider or "google-cloud-tts"),
            str(timing_mode or "plain_text"),
        )
        _page_read_aloud_cache.move_to_end(cache_key)
        while len(_page_read_aloud_cache) > _PAGE_READ_ALOUD_CACHE_MAX_ITEMS:
            _page_read_aloud_cache.popitem(last=False)


def _page_read_aloud_request_body(
    text: str,
    *,
    child_age: int | None,
    storybook_movie_pacing: str | None,
    use_ssml_marks: bool = True,
) -> dict[str, Any]:
    speaking_rate = float(_page_read_aloud_prosody_rate(child_age, storybook_movie_pacing).rstrip("%")) / 100.0
    audio_config: dict[str, Any] = {
        "audioEncoding": "MP3",
    }
    if not use_ssml_marks:
        audio_config["speakingRate"] = speaking_rate

    payload: dict[str, Any] = {
        "voice": {
            "languageCode": settings.page_read_aloud_language_code,
            "name": settings.page_read_aloud_voice,
            "modelName": settings.page_read_aloud_model,
        },
        "audioConfig": audio_config,
    }
    if use_ssml_marks:
        ssml, _ = _build_page_read_aloud_ssml(
            text,
            child_age=child_age,
            storybook_movie_pacing=storybook_movie_pacing,
        )
        payload["input"] = {
            "ssml": ssml,
        }
        payload["enableTimePointing"] = ["SSML_MARK"]
        return payload

    payload["input"] = {
        "text": " ".join(str(text or "").split()).strip(),
    }
    return payload


def _page_read_aloud_is_ssml_unsupported_error(payload: Any) -> bool:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            payload = error.get("message") or error
    normalized = str(payload or "").strip().lower()
    return "does not support ssml input" in normalized and "text only input" in normalized


def _synthesize_page_read_aloud_elevenlabs_audio(
    text: str,
    *,
    child_age: int | None,
    storybook_movie_pacing: str | None,
    elevenlabs_voice_id: str | None = None,
) -> tuple[bytes, str, list[int], str]:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        raise RuntimeError("Page read-aloud text is empty.")

    api_key = str(settings.elevenlabs_api_key or "").strip()
    voice_id = _page_read_aloud_elevenlabs_voice_id(elevenlabs_voice_id)
    fallback_voice_id = _page_read_aloud_elevenlabs_fallback_voice_id(elevenlabs_voice_id)
    model_id = _page_read_aloud_elevenlabs_model()
    if not api_key or not voice_id:
        raise RuntimeError("ElevenLabs page read-aloud is not configured.")

    def _request_voice(request_voice_id: str) -> tuple[bytes, str, list[int], str]:
        base_endpoint = (
            str(settings.page_read_aloud_elevenlabs_timestamps_endpoint or "").strip()
            or str(settings.page_read_aloud_elevenlabs_endpoint or "").strip()
            or str(settings.elevenlabs_tts_endpoint or "").strip()
        )
        if base_endpoint:
            endpoint = base_endpoint.rstrip("/")
            if not endpoint.endswith("/with-timestamps"):
                endpoint = f"{endpoint}/with-timestamps"
        else:
            endpoint = f"https://api.elevenlabs.io/v1/text-to-speech/{request_voice_id}/with-timestamps"

        response = httpx.post(
            endpoint,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json={
                "text": cleaned,
                "model_id": model_id,
                "voice_settings": {
                    "stability": 0.4,
                    "similarity_boost": 0.75,
                },
            },
            timeout=30.0,
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            raise RuntimeError(f"ElevenLabs page read-aloud request failed ({response.status_code}): {payload}")

        payload = response.json()
        audio_base64 = str(payload.get("audio_base64") or payload.get("audioBase64") or "").strip()
        if not audio_base64:
            raise RuntimeError("ElevenLabs page read-aloud returned no audio content.")
        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as exc:
            raise RuntimeError(f"ElevenLabs page read-aloud returned invalid audio content: {exc}") from exc
        word_timings = _extract_page_read_aloud_elevenlabs_word_timings_seconds(payload)
        word_starts_ms = _exact_page_read_aloud_word_starts_ms(cleaned, word_timings)
        if not word_starts_ms:
            raise RuntimeError("ElevenLabs page read-aloud returned no usable word timestamps.")
        return audio_bytes, "audio/mpeg", word_starts_ms, "exact"

    try:
        return _request_voice(voice_id)
    except RuntimeError as exc:
        if fallback_voice_id and fallback_voice_id != voice_id and _elevenlabs_voice_not_found_error(str(exc)):
            logger.warning(
                "Selected ElevenLabs page voice %s was not found; retrying with fallback voice %s.",
                voice_id,
                fallback_voice_id,
            )
            return _request_voice(fallback_voice_id)
        raise


def _synthesize_page_read_aloud_google_audio(
    text: str,
    *,
    child_age: int | None,
    storybook_movie_pacing: str | None,
) -> tuple[bytes, str, list[int], str]:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        raise RuntimeError("Page read-aloud text is empty.")

    use_ssml_marks = True
    response = httpx.post(
        "https://texttospeech.googleapis.com/v1beta1/text:synthesize",
        headers={
            "Authorization": f"Bearer {_page_read_aloud_token()}",
            "x-goog-user-project": settings.google_cloud_project,
            "Content-Type": "application/json",
        },
        json=_page_read_aloud_request_body(
            cleaned,
            child_age=child_age,
            storybook_movie_pacing=storybook_movie_pacing,
            use_ssml_marks=use_ssml_marks,
        ),
        timeout=60.0,
    )
    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = response.text
        if response.status_code == 400 and _page_read_aloud_is_ssml_unsupported_error(payload):
            logger.warning(
                "Page read-aloud voice %s rejected SSML; retrying with plain text input.",
                settings.page_read_aloud_voice,
            )
            use_ssml_marks = False
            response = httpx.post(
                "https://texttospeech.googleapis.com/v1beta1/text:synthesize",
                headers={
                    "Authorization": f"Bearer {_page_read_aloud_token()}",
                    "x-goog-user-project": settings.google_cloud_project,
                    "Content-Type": "application/json",
                },
                json=_page_read_aloud_request_body(
                    cleaned,
                    child_age=child_age,
                    storybook_movie_pacing=storybook_movie_pacing,
                    use_ssml_marks=use_ssml_marks,
                ),
                timeout=60.0,
            )
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except Exception:
                    payload = response.text
                raise RuntimeError(f"Cloud TTS request failed ({response.status_code}): {payload}")
        else:
            raise RuntimeError(f"Cloud TTS request failed ({response.status_code}): {payload}")

    payload = response.json()
    encoded_audio = payload.get("audioContent") or payload.get("audio_content")
    if not encoded_audio:
        raise RuntimeError("Cloud TTS returned no audio content.")

    if not use_ssml_marks:
        return base64.b64decode(encoded_audio), "audio/mpeg", [], "plain_text"

    words = _tokenize_page_read_aloud_words(cleaned)
    word_starts_ms: list[int | None] = [None] * len(words)
    for raw_timepoint in payload.get("timepoints") or []:
        mark_name = str(
            _coerce_token_attr(raw_timepoint, "markName")
            or _coerce_token_attr(raw_timepoint, "mark_name")
            or ""
        ).strip()
        match = re.fullmatch(r"w(\d+)", mark_name)
        if not match:
            continue
        word_index = int(match.group(1))
        if word_index < 0 or word_index >= len(word_starts_ms):
            continue
        raw_seconds = _coerce_token_attr(raw_timepoint, "timeSeconds")
        if raw_seconds is None:
            raw_seconds = _coerce_token_attr(raw_timepoint, "time_seconds")
        try:
            time_seconds = float(raw_seconds)
        except Exception:
            continue
        if time_seconds < 0:
            continue
        word_starts_ms[word_index] = max(0, int(round(time_seconds * 1000)))

    normalized_word_starts_ms: list[int] = []
    last_start_ms = 0
    for index, maybe_start_ms in enumerate(word_starts_ms):
        if maybe_start_ms is None:
            maybe_start_ms = last_start_ms if index else 0
        if maybe_start_ms < last_start_ms:
            maybe_start_ms = last_start_ms
        normalized_word_starts_ms.append(maybe_start_ms)
        last_start_ms = maybe_start_ms

    return base64.b64decode(encoded_audio), "audio/mpeg", normalized_word_starts_ms, "ssml_marks"


def _synthesize_page_read_aloud_audio(
    text: str,
    child_age: int | None,
    storybook_movie_pacing: str | None,
    elevenlabs_voice_id: str | None = None,
) -> tuple[bytes, str, list[int], str, str]:
    requested_provider = _page_read_aloud_requested_provider()
    last_error: Exception | None = None

    if requested_provider in {"auto", "elevenlabs"}:
        try:
            audio_bytes, media_type, word_starts_ms, timing_mode = _synthesize_page_read_aloud_elevenlabs_audio(
                text,
                child_age=child_age,
                storybook_movie_pacing=storybook_movie_pacing,
                elevenlabs_voice_id=elevenlabs_voice_id,
            )
            return audio_bytes, media_type, word_starts_ms, "elevenlabs", timing_mode
        except Exception as exc:
            last_error = exc
            if requested_provider == "elevenlabs":
                raise
            logger.warning("ElevenLabs page read-aloud failed; falling back to Google TTS: %s", exc)

    if requested_provider in {"auto", "google-cloud-tts"}:
        try:
            audio_bytes, media_type, word_starts_ms, timing_mode = _synthesize_page_read_aloud_google_audio(
                text,
                child_age=child_age,
                storybook_movie_pacing=storybook_movie_pacing,
            )
            return audio_bytes, media_type, word_starts_ms, "google-cloud-tts", timing_mode
        except Exception as exc:
            last_error = exc
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Unsupported page read-aloud provider: {requested_provider}")


def _coerce_token_attr(value: Any, attr: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(attr, default)
    return getattr(value, attr, default)


def _serialize_ephemeral_token_response(token: Any) -> dict[str, Any]:
    expire_time = _coerce_token_attr(token, "expire_time")
    new_session_expire_time = _coerce_token_attr(token, "new_session_expire_time")
    config = build_live_ephemeral_token_config(
        model=settings.client_direct_live_model,
        temperature=settings.client_direct_live_temperature,
        top_p=settings.client_direct_live_top_p,
        voice_name=settings.client_direct_live_voice,
        expire_minutes=settings.client_direct_live_expire_minutes,
        new_session_minutes=settings.client_direct_live_new_session_minutes,
    )
    live_config = config.get("live_connect_constraints", {}).get("config", {})
    return {
        "status": "ok",
        "transport_mode": "client_direct_live_experimental",
        "story_control_mode": "backend_adk_websocket",
        "token": _coerce_token_attr(token, "name"),
        "model": settings.client_direct_live_model,
        "voice_name": settings.client_direct_live_voice,
        "temperature": clamp_live_temperature(settings.client_direct_live_temperature),
        "top_p": clamp_live_top_p(settings.client_direct_live_top_p),
        "expire_time": expire_time.isoformat() if hasattr(expire_time, "isoformat") else expire_time,
        "new_session_expire_time": (
            new_session_expire_time.isoformat()
            if hasattr(new_session_expire_time, "isoformat")
            else new_session_expire_time
        ),
        "http_options": {"apiVersion": "v1alpha"},
        "live_connect_config": {
            "responseModalities": list(live_config.get("response_modalities", ["AUDIO"])),
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {"disabled": True}
            },
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {
                        "voiceName": settings.client_direct_live_voice,
                    }
                }
            },
            "temperature": clamp_live_temperature(settings.client_direct_live_temperature),
            "topP": clamp_live_top_p(settings.client_direct_live_top_p),
            "sessionResumption": {},
        },
    }


@app.on_event("startup")
async def startup_event() -> None:
    global _runner, _cleanup_task
    logger.info("Initializing ADK Runner...")
    set_main_loop(asyncio.get_running_loop())
    _runner = _create_runner()
    _cleanup_task = asyncio.create_task(_session_ttl_cleanup_loop())
    logger.info(
        "ADK Runner ready. StorySpark API is live. backend_live_backend=%s storyteller_live_model=%s run_region=%s vertex_location=%s",
        "vertex_ai" if settings.google_genai_use_vertexai else "ai_studio",
        os.environ.get("STORYTELLER_LIVE_MODEL", "").strip() or "default",
        settings.google_cloud_location,
        settings.vertex_ai_location,
    )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None


async def _session_ttl_cleanup_loop() -> None:
    """Purges abandoned in-memory sessions older than 24 hours."""
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            if not _runner:
                continue
            service = _runner.session_service
            sessions_map = getattr(service, "sessions", None)
            if not isinstance(sessions_map, dict):
                continue

            now = time.time()
            stale: list[tuple[str, str]] = []
            app_sessions = sessions_map.get("storyteller", {})
            for user_id, by_session in app_sessions.items():
                for session_id, session in by_session.items():
                    last_update = float(getattr(session, "last_update_time", 0.0) or 0.0)
                    if last_update and now - last_update > _SESSION_TTL_SECONDS:
                        stale.append((user_id, session_id))

            for user_id, session_id in stale:
                try:
                    await service.delete_session(
                        app_name="storyteller",
                        user_id=user_id,
                        session_id=session_id,
                    )
                    logger.info("TTL-purged stale session: %s", session_id)
                except Exception as exc:
                    logger.warning("Failed to purge stale session %s: %s", session_id, exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Session cleanup loop error: %s", exc)


@app.get("/health")
@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "active_sessions": _active_sessions,
        "live_telemetry": get_live_telemetry_snapshot(),
        "client_direct_live": {
            "enabled": settings.enable_client_direct_live,
            "transport_mode": (
                "client_direct_live_experimental"
                if settings.enable_client_direct_live
                else "backend_adk_websocket"
            ),
            "model": settings.client_direct_live_model,
        },
        "backend_live": {
            "transport_mode": "backend_adk_websocket",
            "use_vertex_ai": settings.google_genai_use_vertexai,
            "model": os.environ.get("STORYTELLER_LIVE_MODEL", "").strip() or None,
            "location": settings.google_cloud_location,
            "vertex_location": settings.vertex_ai_location,
        },
    }


@app.get("/api/live-telemetry")
async def live_telemetry() -> dict:
    return {
        "status": "ok",
        "active_sessions": _active_sessions,
        "live_telemetry": get_live_telemetry_snapshot(),
    }


@app.post("/api/live-ephemeral-token")
async def create_live_ephemeral_token(request: Request) -> JSONResponse:
    """Mints an ephemeral Gemini Live token for experimental client-direct audio."""
    if not settings.enable_client_direct_live:
        return JSONResponse(
            {
                "error": "client_direct_live_disabled",
                "message": "Client-direct Gemini Live is disabled on this backend.",
            },
            status_code=503,
        )

    request_origin = (request.headers.get("origin") or "").strip()
    if not _is_allowed_frontend_origin(request_origin):
        return JSONResponse(
            {
                "error": "origin_not_allowed",
                "message": "This endpoint only serves configured frontend origins.",
            },
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    session_id = str(body.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    try:
        from google import genai as google_genai

        client = google_genai.Client(
            api_key=settings.google_api_key,
            http_options={"api_version": "v1alpha"},
        )
        token = client.auth_tokens.create(
            config=build_live_ephemeral_token_config(
                model=settings.client_direct_live_model,
                temperature=settings.client_direct_live_temperature,
                top_p=settings.client_direct_live_top_p,
                voice_name=settings.client_direct_live_voice,
                expire_minutes=settings.client_direct_live_expire_minutes,
                new_session_minutes=settings.client_direct_live_new_session_minutes,
            )
        )
        payload = _serialize_ephemeral_token_response(token)
        payload["session_id"] = session_id
        logger.info(
            "Minted experimental client-direct Live token for session %s using model %s",
            session_id,
            settings.client_direct_live_model,
        )
        return JSONResponse(payload)
    except Exception as exc:
        logger.exception("Failed to mint client-direct Live token: %s", exc)
        return JSONResponse(
            {
                "error": "token_mint_failed",
                "message": "Failed to mint a Gemini Live ephemeral token.",
            },
            status_code=502,
        )


@app.post("/api/page-read-aloud")
async def create_page_read_aloud(request: Request) -> Response:
    request_origin = (request.headers.get("origin") or "").strip()
    if request_origin and not _is_allowed_frontend_origin(request_origin):
        return JSONResponse(
            {
                "error": "origin_not_allowed",
                "message": "This endpoint only serves configured frontend origins.",
            },
            status_code=403,
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    text = " ".join(str(body.get("text") or "").split()).strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    if len(text) > 320:
        return JSONResponse({"error": "text too long"}, status_code=400)

    session_id = str(body.get("session_id") or "").strip()
    child_age = _coerce_child_age(body.get("child_age"))
    storybook_movie_pacing = str(body.get("storybook_movie_pacing") or "").strip() or None
    storybook_elevenlabs_voice_id = _normalize_storybook_elevenlabs_voice_id(
        body.get("storybook_elevenlabs_voice_id") or body.get("storybookElevenlabsVoiceId")
    )
    requested_provider = _page_read_aloud_requested_provider()
    logger.info(
        "Page read-aloud requested for session %s using provider=%s model=%s voice=%s chars=%d",
        session_id or "unknown",
        requested_provider,
        _page_read_aloud_log_model_name(requested_provider),
        _page_read_aloud_log_voice_name(requested_provider, elevenlabs_voice_id=storybook_elevenlabs_voice_id),
        len(text),
    )
    workflow_request = PageReadAloudRequest(
        text=text,
        session_id=session_id,
        child_age=child_age,
        storybook_movie_pacing=storybook_movie_pacing,
        elevenlabs_voice_id=storybook_elevenlabs_voice_id or None,
        provider=requested_provider,
        model_name=_page_read_aloud_log_model_name(requested_provider),
        voice_name=_page_read_aloud_log_voice_name(requested_provider, elevenlabs_voice_id=storybook_elevenlabs_voice_id),
        language_code=settings.page_read_aloud_language_code,
    )

    try:
        workflow_state = await run_storybook_page_read_aloud_workflow(
            request=workflow_request,
            cache_key_builder=_page_read_aloud_cache_key,
            cache_lookup=_page_read_aloud_cache_get,
            cache_store=_page_read_aloud_cache_put,
            synthesize_audio=_synthesize_page_read_aloud_audio,
        )
    except Exception as exc:
        logger.exception("Page read-aloud synthesis failed for session %s: %s", session_id or "unknown", exc)
        return JSONResponse(
            {
                "error": "page_read_aloud_failed",
                "message": "Voxitale could not create page audio right now.",
            },
            status_code=502,
        )

    result = dict(workflow_state.get("page_read_aloud_result_structured") or {})
    audio_b64 = str(result.get("audio_base64") or "").strip()
    if not audio_b64:
        logger.warning("Page read-aloud workflow returned no audio for session %s", session_id or "unknown")
        return JSONResponse(
            {
                "error": "page_read_aloud_unavailable",
                "message": "Voxitale could not create page audio right now.",
            },
            status_code=502,
        )

    try:
        audio_bytes = base64.b64decode(audio_b64)
    except Exception:
        logger.warning("Page read-aloud workflow returned invalid base64 audio for session %s", session_id or "unknown")
        return JSONResponse(
            {
                "error": "page_read_aloud_unavailable",
                "message": "Voxitale could not create page audio right now.",
            },
            status_code=502,
        )

    media_type = " ".join(str(result.get("media_type") or "").split()).strip() or "audio/mpeg"
    word_starts_ms = [
        max(0, int(value))
        for value in list(result.get("word_starts_ms") or [])
        if isinstance(value, (int, float)) or str(value).strip().isdigit()
    ]
    if not audio_bytes:
        logger.warning("Page read-aloud returned no audio for session %s", session_id or "unknown")
        return JSONResponse(
            {
                "error": "page_read_aloud_unavailable",
                "message": "Voxitale could not create page audio right now.",
            },
            status_code=502,
        )

    logger.info(
        "Page read-aloud ready for session %s (%d bytes)",
        session_id or "unknown",
        len(audio_bytes),
    )
    return Response(
        content=audio_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": "no-store",
            "X-StorySpark-Audio-Provider": str(result.get("provider") or "google-cloud-tts"),
            "X-StorySpark-Cache": "hit" if bool(result.get("cache_hit")) else "miss",
            "X-StorySpark-Workflow-Source": str(result.get("workflow_source") or "adk_page_read_aloud_service"),
            _PAGE_READ_ALOUD_WORD_TIMESTAMPS_HEADER: ",".join(str(value) for value in word_starts_ms),
        },
    )


@app.post("/api/home-assistant/apply-lighting")
async def apply_home_assistant_lighting_endpoint(
    request: Request,
    body: HomeAssistantApplyRequest,
) -> JSONResponse:
    request_origin = (request.headers.get("origin") or "").strip()
    if request_origin and not _is_allowed_frontend_origin(request_origin):
        return JSONResponse(
            {
                "error": "origin_not_allowed",
                "message": "This endpoint only serves configured frontend origins.",
            },
            status_code=403,
        )

    config, reason = _validate_backend_home_assistant_config(body.config)
    if reason:
        return _home_assistant_failure_response(reason, status_code=400)

    async with httpx.AsyncClient(timeout=_HOME_ASSISTANT_RELAY_TIMEOUT_SECONDS) as client:
        result = await _apply_home_assistant_turn_on(client, config, body.command)
    return JSONResponse(result)


@app.post("/api/home-assistant/test-light")
async def test_home_assistant_lighting_endpoint(
    request: Request,
    body: HomeAssistantTestRequest,
) -> JSONResponse:
    request_origin = (request.headers.get("origin") or "").strip()
    if request_origin and not _is_allowed_frontend_origin(request_origin):
        return JSONResponse(
            {
                "error": "origin_not_allowed",
                "message": "This endpoint only serves configured frontend origins.",
            },
            status_code=403,
        )

    config, reason = _validate_backend_home_assistant_config(body.config)
    if reason:
        return _home_assistant_failure_response(reason, status_code=400)

    async with httpx.AsyncClient(timeout=_HOME_ASSISTANT_RELAY_TIMEOUT_SECONDS) as client:
        snapshot = await _load_home_assistant_state(client, config)
        if snapshot.get("reason"):
            return JSONResponse(
                {
                    "ok": False,
                    "reason": snapshot["reason"],
                    "entityName": snapshot.get("friendly_name"),
                    "transport": "backend",
                }
            )

        test_result = await _apply_home_assistant_turn_on(
            client,
            config,
            HomeAssistantRelayCommand(
                entity=str(snapshot.get("entity_id") or config["ha_entity"]),
                rgb_color=[124, 92, 255],
                brightness=190,
                transition=0.6,
            ),
        )
        if not test_result.get("ok"):
            return JSONResponse(
                {
                    "ok": False,
                    "reason": test_result.get("reason"),
                    "entityName": snapshot.get("friendly_name"),
                    "transport": "backend",
                }
            )

        await asyncio.sleep(_HOME_ASSISTANT_RESTORE_DELAY_SECONDS)
        restored = await _restore_home_assistant_state(client, config, snapshot)
        return JSONResponse(
            {
                "ok": restored,
                "reason": None if restored else "restore_failed",
                "entityName": snapshot.get("friendly_name"),
                "restored": restored,
                "transport": "backend",
            }
        )


@app.get("/api/scene/{media_id}")
async def get_scene_media(media_id: str) -> Response:
    """Serves locally cached scene media bytes (dev fallback for large data URLs)."""
    item = get_media(media_id)
    if not item:
        raise HTTPException(status_code=404, detail="Scene media not found.")
    return Response(content=item.data, media_type=item.mime_type)


@app.delete("/api/purge-my-data")
async def purge_data(request: Request) -> JSONResponse:
    """Right-to-Be-Forgotten endpoint (Iter 5 #8 — COPPA compliance).

    Requires PIN authentication from the parental dashboard.
    """
    body = await request.json()
    session_id = body.get("session_id")
    user_id = body.get("user_id", "anonymous")
    if settings.parent_gate_pin:
        provided_pin = body.get("pin", "")
        if provided_pin != settings.parent_gate_pin:
            return JSONResponse({"error": "invalid PIN"}, status_code=403)
    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)
    if _runner:
        try:
            await _runner.session_service.delete_session(
                app_name="storyteller", user_id=user_id, session_id=session_id
            )
            # TODO: cascade delete from GCS via lifecycle rules or explicit delete
            logger.info(f"COPPA purge complete for session {session_id}")
            return JSONResponse({"status": "deleted", "session_id": session_id})
        except Exception as e:
            logger.error(f"Purge failed: {e}")
            return JSONResponse({"error": "Purge failed"}, status_code=500)
    return JSONResponse({"error": "Server not ready"}, status_code=503)


from fastapi import UploadFile, File, Form

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    session_id: str = Form(...)
) -> JSONResponse:
    """Uploads a spyglass image to GCS and returns its URI."""
    if not _runner:
        return JSONResponse({"error": "Server not ready"}, status_code=503)

    try:
        content = await file.read()
        mime_type = file.content_type or "image/jpeg"
        # GcsArtifactService.save_artifact expects filename= and artifact=Part(inline_data=Blob(...))
        artifact = genai_types.Part(
            inline_data=genai_types.Blob(data=content, mime_type=mime_type)
        )
        version = await _runner.artifact_service.save_artifact(
            app_name="storyteller",
            user_id="anonymous",
            session_id=session_id,
            filename=file.filename or "spyglass.jpg",
            artifact=artifact,
        )
        # GCS path: app_name/user_id/session_id/filename/version
        gcs_url = (
            f"gs://{settings.gcs_assets_bucket}/storyteller/anonymous/"
            f"{session_id}/{file.filename or 'spyglass.jpg'}/{version}"
        )
        return JSONResponse({"gcs_url": gcs_url})
    except Exception as e:
        logger.error(f"Failed to upload spyglass image: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.websocket("/ws/story")
async def story_websocket(websocket: WebSocket) -> None:
    """The main Bidi-streaming WebSocket endpoint."""
    global _active_sessions

    if _runner is None:
        await websocket.accept()
        await websocket.send_text(json.dumps({"type": "error", "payload": {"message": "Server warming up."}}))
        await websocket.close(code=1013)
        return

    # Flat-rate session cap — return 503 instead of crashing (Iter 2 #1)
    if _active_sessions >= settings.max_live_sessions:
        await websocket.accept()
        from .schemas import queue_position_event
        await websocket.send_text(
            queue_position_event(position=_active_sessions - settings.max_live_sessions + 1, eta_seconds=120).model_dump_json()
        )
        await websocket.close(code=1013)  # 1013 = Try Again Later
        return

    _active_sessions += 1
    try:
        await handle_storyteller_ws(websocket, _runner)
    finally:
        _active_sessions -= 1


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
