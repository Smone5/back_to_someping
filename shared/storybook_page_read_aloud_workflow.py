from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Callable, Literal

from google.adk.agents import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

PageReadAloudProvider = Literal["auto", "elevenlabs", "google-cloud-tts"]
ResolvedPageReadAloudProvider = Literal["elevenlabs", "google-cloud-tts"]
PageReadAloudTimingMode = Literal["exact", "ssml_marks", "plain_text"]

CacheKeyBuilder = Callable[[str, int | None, str | None, str | None], str]
CacheLookup = Callable[[str], tuple[bytes, str, tuple[int, ...], ResolvedPageReadAloudProvider, PageReadAloudTimingMode] | None]
CacheStore = Callable[[str, bytes, str, list[int], ResolvedPageReadAloudProvider, PageReadAloudTimingMode], None]
SynthesizeAudio = Callable[[str, int | None, str | None, str | None], tuple[bytes, str, list[int], ResolvedPageReadAloudProvider, PageReadAloudTimingMode]]


class PageReadAloudRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    session_id: str = ""
    child_age: int | None = None
    storybook_movie_pacing: str | None = None
    elevenlabs_voice_id: str | None = None
    provider: PageReadAloudProvider = "auto"
    model_name: str
    voice_name: str
    language_code: str


class PageReadAloudPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: PageReadAloudProvider = "auto"
    model_name: str
    voice_name: str
    language_code: str
    cache_key: str
    timing_preference: Literal["exact_word_timestamps_if_supported"] = "exact_word_timestamps_if_supported"


class PageReadAloudResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: ResolvedPageReadAloudProvider = "google-cloud-tts"
    cache_key: str
    cache_hit: bool
    media_type: str
    timing_mode: PageReadAloudTimingMode
    word_starts_ms: list[int] = Field(default_factory=list)
    audio_base64: str
    workflow_source: Literal["adk_page_read_aloud_service"] = "adk_page_read_aloud_service"


def _clean_text(value: Any, limit: int = 320) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit] if text else ""


def _build_state_delta_event(
    ctx: InvocationContext,
    *,
    author: str,
    updates: dict[str, Any],
) -> Event | None:
    if not updates:
        return None
    callback_context = CallbackContext(ctx)
    for key, value in updates.items():
        callback_context.state[key] = value
    if not callback_context.state.has_delta():
        return None
    return Event(
        invocation_id=ctx.invocation_id,
        author=author,
        branch=ctx.branch,
        actions=callback_context.actions,
    )


def build_page_read_aloud_plan(
    request: PageReadAloudRequest,
    *,
    cache_key: str,
) -> dict[str, Any]:
    plan = PageReadAloudPlan(
        provider=request.provider,
        model_name=request.model_name,
        voice_name=request.voice_name,
        language_code=request.language_code,
        cache_key=cache_key,
    )
    return plan.model_dump(exclude_none=True)


def build_page_read_aloud_result(
    *,
    cache_key: str,
    cache_hit: bool,
    provider: ResolvedPageReadAloudProvider,
    media_type: str,
    timing_mode: PageReadAloudTimingMode,
    word_starts_ms: list[int] | tuple[int, ...],
    audio_bytes: bytes,
) -> dict[str, Any]:
    normalized_word_starts = [max(0, int(value)) for value in list(word_starts_ms or [])]
    result = PageReadAloudResult(
        provider=provider,
        cache_key=_clean_text(cache_key, limit=128),
        cache_hit=bool(cache_hit),
        media_type=_clean_text(media_type, limit=64) or "audio/mpeg",
        timing_mode=timing_mode,
        word_starts_ms=normalized_word_starts,
        audio_base64=base64.b64encode(audio_bytes).decode("ascii"),
    )
    return result.model_dump(exclude_none=True)


class StorybookPageReadAloudWorkflowAgent(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    cache_key_builder: Any
    cache_lookup: Any
    cache_store: Any
    synthesize_audio: Any

    def __init__(
        self,
        *,
        cache_key_builder: CacheKeyBuilder,
        cache_lookup: CacheLookup,
        cache_store: CacheStore,
        synthesize_audio: SynthesizeAudio,
    ) -> None:
        super().__init__(
            name="storybook_page_read_aloud_service",
            description="Typed ADK workflow for page narration audio generation and caching.",
            cache_key_builder=cache_key_builder,
            cache_lookup=cache_lookup,
            cache_store=cache_store,
            synthesize_audio=synthesize_audio,
        )

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        request = PageReadAloudRequest.model_validate(dict(state.get("page_read_aloud_request_structured") or {}))
        cache_key = self.cache_key_builder(
            request.text,
            request.child_age,
            request.storybook_movie_pacing,
            request.elevenlabs_voice_id,
        )
        plan = build_page_read_aloud_plan(request, cache_key=cache_key)
        plan_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "page_read_aloud_plan": json.dumps(plan, ensure_ascii=True),
                "page_read_aloud_plan_structured": plan,
            },
        )
        if plan_event is not None:
            yield plan_event

        cached_audio = self.cache_lookup(cache_key)
        cache_hit = cached_audio is not None
        if cached_audio is not None:
            audio_bytes, media_type, word_starts_ms, provider, timing_mode = cached_audio
        else:
            audio_bytes, media_type, word_starts_ms, provider, timing_mode = await asyncio.to_thread(
                self.synthesize_audio,
                request.text,
                request.child_age,
                request.storybook_movie_pacing,
                request.elevenlabs_voice_id,
            )
            self.cache_store(cache_key, audio_bytes, media_type, list(word_starts_ms), provider, timing_mode)

        result = build_page_read_aloud_result(
            cache_key=cache_key,
            cache_hit=cache_hit,
            provider=provider,
            media_type=media_type,
            timing_mode=timing_mode,
            word_starts_ms=word_starts_ms,
            audio_bytes=audio_bytes,
        )
        result_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "page_read_aloud_result": json.dumps(result, ensure_ascii=True),
                "page_read_aloud_result_structured": result,
            },
        )
        if result_event is not None:
            yield result_event


async def run_storybook_page_read_aloud_workflow(
    *,
    request: PageReadAloudRequest,
    cache_key_builder: CacheKeyBuilder,
    cache_lookup: CacheLookup,
    cache_store: CacheStore,
    synthesize_audio: SynthesizeAudio,
) -> dict[str, Any]:
    app_name = "storybook_page_read_aloud_service"
    user_id = _clean_text(request.session_id, limit=128) or "storybook-page-read-aloud"
    workflow_session_id = f"{user_id}-page-read-aloud-{uuid.uuid4().hex[:8]}"

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=workflow_session_id,
        state={
            "page_read_aloud_request": request.model_dump_json(exclude_none=True),
            "page_read_aloud_request_structured": request.model_dump(exclude_none=True),
        },
    )

    runner = Runner(
        agent=StorybookPageReadAloudWorkflowAgent(
            cache_key_builder=cache_key_builder,
            cache_lookup=cache_lookup,
            cache_store=cache_store,
            synthesize_audio=synthesize_audio,
        ),
        app_name=app_name,
        session_service=session_service,
    )

    kickoff = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text="Create page read aloud audio now.")],
    )
    async for _event in runner.run_async(
        user_id=user_id,
        session_id=workflow_session_id,
        new_message=kickoff,
    ):
        pass

    final_session = await session_service.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=workflow_session_id,
    )
    if final_session is None:
        logger.warning("Page read aloud workflow produced no final session for %s.", request.session_id)
        return {}
    return dict(final_session.state or {})
