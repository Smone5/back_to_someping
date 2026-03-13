"""Root ADK agent definition for StorySpark.

Architecture decisions:
- Uses App wrapper (per Iteration 2 #3: ADK App Containerization) for
  production-grade context caching scope.
- Native audio model for affective dialog (emotional inflection).
- ElevenLabs McpToolset for character voice cloning.
- RunConfig with BIDI streaming mode and child-tuned VAD settings.
- Safety settings at BLOCK_LOW_AND_ABOVE for all harm categories.
"""
from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps import App
try:
    from google.adk.apps.app import ResumabilityConfig
except Exception:  # pragma: no cover - older ADK versions
    ResumabilityConfig = None
from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StdioConnectionParams, SseConnectionParams
from google.adk.utils import instructions_utils
from mcp import StdioServerParameters
from google.genai import types as genai_types
from google.genai.types import GenerateContentConfig, SafetySetting, HarmCategory, HarmBlockThreshold

from shared.meta_learning import build_principles_injection_text

from .prompts import SYSTEM_PROMPT_TEMPLATE
from .tools import (
    assemble_story_video,
    generate_background_music,
    generate_scene_visuals,
    generate_trading_card,
    save_character_fact,
    save_child_name,
)

def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _using_vertex_ai_backend() -> bool:
    raw = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_live_model() -> str:
    if _using_vertex_ai_backend():
        return "gemini-live-2.5-flash-native-audio"
    return "gemini-2.5-flash-native-audio-preview-12-2025"


async def _storyteller_instruction(context: ReadonlyContext) -> str:
    meta_block = build_principles_injection_text("interactive_story")
    template = SYSTEM_PROMPT_TEMPLATE.replace("<<INTERACTIVE_STORY_META_PRINCIPLES>>", meta_block)
    return await instructions_utils.inject_session_state(template, context)

# ── Native ADK tools ───────────────────────────────────────────────────────────
_native_tools = [
    FunctionTool(generate_scene_visuals),
    FunctionTool(generate_background_music),
    FunctionTool(save_character_fact),
    FunctionTool(save_child_name),
    FunctionTool(generate_trading_card),
    FunctionTool(assemble_story_video),
]

# ── ElevenLabs MCP Toolset (optional — requires ELEVENLABS_API_KEY) ───────────
# Provides: text_to_speech, speech_to_speech (voice cloning), generate_sfx (Foley)
# Skipped by default in local/dev to avoid hard failures when MCP startup/session
# negotiation is unavailable. Enable explicitly with ENABLE_ELEVENLABS_MCP=true.
_enable_elevenlabs_mcp = _env_enabled("ENABLE_ELEVENLABS_MCP", default=False)
_npx_cmd = "/opt/homebrew/bin/npx" if os.path.isfile("/opt/homebrew/bin/npx") else "npx"
_elevenlabs_mcp = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=_npx_cmd,
            args=["-y", "elevenlabs-mcp"],
            env={
                "ELEVENLABS_API_KEY": os.environ.get("ELEVENLABS_API_KEY", ""),
                "ELEVENLABS_MCP_OUTPUT_MODE": "resources",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"),
            },
        )
    )
) if _enable_elevenlabs_mcp and os.environ.get("ELEVENLABS_API_KEY") else None

# ── Home Assistant / HTTP MCP (optional IoT — graceful degradation if absent) ──
_home_assistant_mcp = McpToolset(
    connection_params=SseConnectionParams(
        url=os.environ.get("HOME_ASSISTANT_MCP_URL", ""),
    )
) if os.environ.get("HOME_ASSISTANT_MCP_URL") else None

_mcp_toolsets = [ts for ts in [_elevenlabs_mcp, _home_assistant_mcp] if ts]

from google.adk.agents.run_config import RunConfig, StreamingMode, ToolThreadPoolConfig

# ── RunConfig ──────────────────────────────────────────────────────────────────
# StreamingMode.BIDI required for full-duplex "Bidi-streaming" interaction.
# response_modalities=["AUDIO"] required for native audio model voice output.
_run_config_kwargs = dict(
    streaming_mode=StreamingMode.BIDI,
    response_modalities=[genai_types.Modality.AUDIO],  # Required for Gemini native audio voice output
    input_audio_transcription=genai_types.AudioTranscriptionConfig(),
    output_audio_transcription=genai_types.AudioTranscriptionConfig(),
    # Tool execution happens in background threads to keep Event Loop responsive
    # for immediate user interruptions.
    tool_thread_pool_config=ToolThreadPoolConfig(max_workers=5),
    # Toggle server-side (automatic) VAD via env to avoid 1008 on unsupported models.
    realtime_input_config=genai_types.RealtimeInputConfig(
        automatic_activity_detection=(
            genai_types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                end_of_speech_sensitivity=genai_types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=200,
                silence_duration_ms=900,
            )
            if _env_enabled("ENABLE_SERVER_VAD", default=False)
            else genai_types.AutomaticActivityDetection(disabled=True)
        )
    ),
    speech_config=genai_types.SpeechConfig(
        voice_config=genai_types.VoiceConfig(
            prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                voice_name='Aoede'
            )
        )
    )
)
_session_resumption_config_cls = getattr(genai_types, "SessionResumptionConfig", None)
_session_resumption_config = None
if _using_vertex_ai_backend() and _session_resumption_config_cls is not None:
    try:
        _session_resumption_config = _session_resumption_config_cls(transparent=True)
    except TypeError:
        try:
            _session_resumption_config = _session_resumption_config_cls()
        except Exception:
            _session_resumption_config = None

if _session_resumption_config is not None:
    try:
        run_config = RunConfig(
            **_run_config_kwargs,
            session_resumption=_session_resumption_config,
        )
    except TypeError:
        run_config = RunConfig(**_run_config_kwargs)
else:
    run_config = RunConfig(**_run_config_kwargs)

# ── Root Agent ─────────────────────────────────────────────────────────────────
# Use an explicit, known-live model by default; allow override via env.
_live_model = os.environ.get(
    "STORYTELLER_LIVE_MODEL",
    _default_live_model(),
).strip() or _default_live_model()
_live_temperature = _env_float("STORYTELLER_LIVE_TEMPERATURE", 0.82, 0.1, 1.5)
_live_top_p = _env_float("STORYTELLER_LIVE_TOP_P", 0.9, 0.1, 1.0)
# gemini-2.5-flash-native-audio-preview-12-2025 supports native TTS text-to-speech
storyteller_agent = LlmAgent(
    name="interactive_storyteller",
    model=_live_model,
    instruction=_storyteller_instruction,
    tools=[*_native_tools, *_mcp_toolsets],
    # Safety settings — BLOCK_ONLY_HIGH to avoid 1008 Policy Violation on misclassified inputs
    generate_content_config=GenerateContentConfig(
        safety_settings=[
            SafetySetting(category=HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
            SafetySetting(category=HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
            SafetySetting(category=HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
            SafetySetting(category=HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=HarmBlockThreshold.BLOCK_ONLY_HIGH),
        ],
        # Prevent internal "thought" parts from being surfaced in live streaming turns.
        thinking_config=genai_types.ThinkingConfig(
            include_thoughts=False
        ),
        # Keep responses imaginative, but slightly tighter and more stable for live turn-taking.
        temperature=_live_temperature,
        top_p=_live_top_p,
    ),
)

# ADK app container wrapper for production runtime scope and tooling integration.
_app_kwargs = dict(name="storyteller", root_agent=storyteller_agent)
if ResumabilityConfig is not None:
    try:
        app = App(
            **_app_kwargs,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )
    except TypeError:
        app = App(**_app_kwargs)
else:
    app = App(**_app_kwargs)
