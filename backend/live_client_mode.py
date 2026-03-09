from __future__ import annotations

import datetime as dt
from typing import Any


DEFAULT_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_LIVE_TEMPERATURE = 0.82
DEFAULT_LIVE_TOP_P = 0.9
DEFAULT_LIVE_VOICE = "Aoede"


def clamp_live_temperature(value: float | int | str | None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = DEFAULT_LIVE_TEMPERATURE
    return max(0.1, min(1.5, parsed))


def clamp_live_top_p(value: float | int | str | None) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = DEFAULT_LIVE_TOP_P
    return max(0.1, min(1.0, parsed))


def build_live_ephemeral_token_config(
    *,
    model: str,
    temperature: float | int | str | None = DEFAULT_LIVE_TEMPERATURE,
    top_p: float | int | str | None = DEFAULT_LIVE_TOP_P,
    voice_name: str = DEFAULT_LIVE_VOICE,
    expire_minutes: int = 30,
    new_session_minutes: int = 1,
) -> dict[str, Any]:
    now = dt.datetime.now(tz=dt.timezone.utc)
    live_model = (model or DEFAULT_LIVE_MODEL).strip() or DEFAULT_LIVE_MODEL
    temp = clamp_live_temperature(temperature)
    top_p_value = clamp_live_top_p(top_p)
    live_voice = (voice_name or DEFAULT_LIVE_VOICE).strip() or DEFAULT_LIVE_VOICE
    expire_delta = max(5, int(expire_minutes))
    new_session_delta = max(1, min(int(new_session_minutes), expire_delta))
    return {
        "uses": 1,
        "expire_time": now + dt.timedelta(minutes=expire_delta),
        "new_session_expire_time": now + dt.timedelta(minutes=new_session_delta),
        "live_connect_constraints": {
            "model": live_model,
            "config": {
                "session_resumption": {},
                "response_modalities": ["AUDIO"],
                "input_audio_transcription": {},
                "output_audio_transcription": {},
                "realtime_input_config": {
                    "automatic_activity_detection": {
                        "disabled": True,
                    }
                },
                "speech_config": {
                    "voice_config": {
                        "prebuilt_voice_config": {
                            "voice_name": live_voice,
                        }
                    }
                },
                "temperature": temp,
                "top_p": top_p_value,
            },
        },
        "http_options": {"api_version": "v1alpha"},
    }
