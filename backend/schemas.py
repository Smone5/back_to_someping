"""Pydantic schemas for all WebSocket messages (both inbound and outbound).

All schemas use model_config = ConfigDict(extra='ignore') so the server never
crashes when the frontend or ADK sends unexpected extra fields.

The WebSocket protocol multiplexes two types of messages on a single connection:
  1. Audio frames — raw binary (PCM bytes)
  2. JSON control messages — typed using these schemas

Inbound (Frontend -> Backend):
  - ClientAudioChunk: raw 16kHz PCM binary frames
  - ClientCommand: JSON commands (rewind, optional camera image URL, session resume)

Outbound (Backend -> Frontend):
  - ServerAudioChunk: raw 24kHz PCM binary frames (ElevenLabs TTS audio)
  - ServerEvent: JSON control events (video_ready, music_command, theater_mode, etc.)
"""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ── Inbound ───────────────────────────────────────────────────────────────────

class ClientCommandType(StrEnum):
    REWIND = "rewind"
    SPYGLASS_IMAGE = "spyglass_image"   # GCS URL of an optional shared item photo
    RESUME_SESSION = "resume_session"   # For reconnect rehydration
    HEARTBEAT = "heartbeat"
    THEATER_CLOSE = "theater_close"     # Child closed the theater — terminate stream
    END_STORY = "end_story"             # Child/parent requested story ending now
    IOT_CONFIG = "iot_config"           # Parent provided HA url/token override
    CLIENT_READY = "client_ready"       # Gate approved, playback ready — send greeting
    ACTIVITY_START = "activity_start"   # User started speaking (tap orb)
    ACTIVITY_END = "activity_end"       # User finished speaking (tap orb again) — model can respond


class ClientCommand(BaseModel):
    """JSON command from the React frontend."""
    model_config = ConfigDict(extra="ignore")

    type: ClientCommandType
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Outbound ──────────────────────────────────────────────────────────────────

class ServerEventType(StrEnum):
    VIDEO_READY = "video_ready"
    VIDEO_GENERATION_STARTED = "video_generation_started"
    TRADING_CARD_READY = "trading_card_ready"
    MUSIC_COMMAND = "music_command"
    SFX_COMMAND = "sfx_command"
    THEATER_MODE = "theater_mode"
    QUEUE_POSITION = "queue_position"   # Graceful "you are #3 in line" UX
    SESSION_REHYDRATED = "session_rehydrated"
    REWIND_COMPLETE = "rewind_complete"  # Rewind completed — session rolled back
    ERROR = "error"
    LIGHTING_COMMAND = "lighting_command"
    USER_TRANSCRIPTION = "user_transcription"
    AGENT_TRANSCRIPTION = "agent_transcription"


class ServerEvent(BaseModel):
    """JSON control event from the backend to the React frontend."""
    model_config = ConfigDict(extra="ignore")

    type: ServerEventType
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Convenience factory methods ───────────────────────────────────────────────

def video_ready_event(mp4_url: str) -> ServerEvent:
    return ServerEvent(type=ServerEventType.VIDEO_READY, payload={"url": mp4_url})


def theater_mode_event(
    mp4_url: str,
    trading_card_url: str | None = None,
    narration_lines: list[str] | None = None,
    audio_available: bool | None = None,
) -> ServerEvent:
    payload: dict[str, Any] = {"mp4_url": mp4_url, "trading_card_url": trading_card_url}
    if narration_lines is not None:
        payload["narration_lines"] = narration_lines
    if audio_available is not None:
        payload["audio_available"] = audio_available
    return ServerEvent(type=ServerEventType.THEATER_MODE, payload=payload)


def music_command_event(mood: str, intensity: int) -> ServerEvent:
    return ServerEvent(
        type=ServerEventType.MUSIC_COMMAND, payload={"mood": mood, "intensity": intensity}
    )


def queue_position_event(position: int, eta_seconds: int) -> ServerEvent:
    return ServerEvent(
        type=ServerEventType.QUEUE_POSITION,
        payload={"position": position, "eta_seconds": eta_seconds},
    )
