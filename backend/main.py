"""FastAPI entry point for the Interactive Storyteller backend.

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
import json
import logging
import os
import sys
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from google.adk.artifacts import GcsArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from pydantic_settings import BaseSettings, SettingsConfigDict

# Ensure the project root (google-prog/) is on sys.path so `agent` package resolves
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from agent.storyteller_agent import app as storyteller_app, storyteller_agent  # noqa: E402
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


settings = Settings()

# Inject into environment for ADK and tool functions
os.environ["GOOGLE_API_KEY"] = settings.google_api_key
os.environ["GOOGLE_CLOUD_PROJECT"] = settings.google_cloud_project
os.environ["GOOGLE_CLOUD_LOCATION"] = settings.google_cloud_location
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
    title="Interactive Storyteller API",
    description="Real-time Bidi-streaming storytelling agent for young children.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin for origin in [settings.frontend_origin, settings.prod_frontend_origin] if origin],
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


def _allowed_frontend_origins() -> set[str]:
    return {
        origin.strip()
        for origin in [settings.frontend_origin, settings.prod_frontend_origin]
        if origin and origin.strip()
    }


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
        "ADK Runner ready. Interactive Storyteller API is live. backend_live_backend=%s storyteller_live_model=%s location=%s",
        "vertex_ai" if settings.google_genai_use_vertexai else "ai_studio",
        os.environ.get("STORYTELLER_LIVE_MODEL", "").strip() or "default",
        settings.google_cloud_location,
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

    allowed_origins = _allowed_frontend_origins()
    request_origin = (request.headers.get("origin") or "").strip()
    if not request_origin or request_origin not in allowed_origins:
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
