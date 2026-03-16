from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Awaitable, Callable, Literal

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.utils import instructions_utils
from google.genai import types as genai_types
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

RenderCallback = Callable[[dict[str, Any], int], Awaitable[dict[str, Any]]]


class VideoAssemblyRuntimeOverrides(BaseModel):
    model_config = ConfigDict(extra="ignore")

    force_tts: bool = False
    disable_music: bool = False
    disable_sfx: bool = False
    force_captions: bool = False


class VideoAssemblyAudioPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    narration_required: bool
    music_optional: bool
    sfx_optional: bool


class VideoAssemblyPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assembly_goal: str
    render_profile: Literal["balanced", "narration_first", "quality_first"]
    must_keep: list[str] = Field(default_factory=list)
    audio_policy: VideoAssemblyAudioPolicy
    retry_policy: str


class VideoAssemblyDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    done: bool
    status: Literal["approved", "retry_render", "needs_manual_review"]
    reason: str
    runtime_overrides: VideoAssemblyRuntimeOverrides = Field(default_factory=VideoAssemblyRuntimeOverrides)


class VideoAssemblySummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["approved", "needs_manual_review"]
    ready_to_publish: bool
    attempts: int
    reason: str


class VideoAssemblyIssue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str
    detail: str = ""


class VideoAssemblyRenderRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str
    attempt: int
    runtime_overrides: VideoAssemblyRuntimeOverrides = Field(default_factory=VideoAssemblyRuntimeOverrides)


class VideoAssemblyRenderReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    ready_to_publish: bool = False
    retryable: bool = False
    reason: str
    issues: list[VideoAssemblyIssue] = Field(default_factory=list)
    runtime_overrides: VideoAssemblyRuntimeOverrides = Field(default_factory=VideoAssemblyRuntimeOverrides)
    final_video_url: str = ""
    final_video_cache_token: str = ""
    audio_expected: bool = False
    audio_available: bool = False
    final_has_audio_stream: bool = False
    theater_release_ready: bool = False
    final_video_duration_sec: float = 0.0
    expected_narration_count: int = 0
    rendered_narration_count: int = 0


def _assembly_model() -> str:
    return (
        os.environ.get("STORYBOOK_VIDEO_ASSEMBLY_MODEL", "").strip()
        or os.environ.get("STORYBOOK_DIRECTOR_MODEL", "").strip()
        or "gemini-2.5-flash"
    )


def _extract_json_block(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


def parse_video_assembly_json(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, BaseModel):
        return raw.model_dump(exclude_none=True)
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    return _extract_json_block(raw)


def _clean_text(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:320] if text else ""


def _coerce_non_negative_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _coerce_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _normalize_runtime_overrides(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, bool] = {}
    for key in ("force_tts", "disable_music", "disable_sfx", "force_captions"):
        value = raw.get(key)
        if isinstance(value, bool):
            normalized[key] = value
    return normalized


def _normalize_video_assembly_issue_list(raw: Any) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return issues
    for item in raw:
        if isinstance(item, dict):
            code = _clean_text(item.get("code"))
            detail = _clean_text(item.get("detail"))
        else:
            code = _clean_text(item)
            detail = ""
        if not code:
            continue
        issues.append({"code": code, "detail": detail})
    return issues


def build_video_assembly_render_request(
    *,
    session_id: str,
    attempt: int,
    runtime_overrides: Any,
) -> dict[str, Any]:
    request = VideoAssemblyRenderRequest(
        session_id=_clean_text(session_id),
        attempt=max(1, int(attempt)),
        runtime_overrides=VideoAssemblyRuntimeOverrides(**_normalize_runtime_overrides(runtime_overrides)),
    )
    return request.model_dump(exclude_none=True)


def normalize_video_assembly_render_report(
    raw: Any,
    *,
    runtime_overrides: Any,
    default_reason: str,
) -> dict[str, Any]:
    if isinstance(raw, BaseModel):
        parsed = raw.model_dump(exclude_none=True)
    elif isinstance(raw, dict):
        parsed = dict(raw)
    else:
        parsed = {}

    issues = _normalize_video_assembly_issue_list(parsed.get("issues"))
    report = VideoAssemblyRenderReport(
        status=_clean_text(parsed.get("status")) or "failed",
        ready_to_publish=bool(parsed.get("ready_to_publish")),
        retryable=bool(parsed.get("retryable")),
        reason=_clean_text(parsed.get("reason")) or _clean_text(default_reason) or "Render execution failed.",
        issues=[VideoAssemblyIssue(**issue) for issue in issues],
        runtime_overrides=VideoAssemblyRuntimeOverrides(
            **_normalize_runtime_overrides(parsed.get("runtime_overrides") or runtime_overrides)
        ),
        final_video_url=_clean_text(parsed.get("final_video_url")),
        final_video_cache_token=_clean_text(parsed.get("final_video_cache_token")),
        audio_expected=bool(parsed.get("audio_expected")),
        audio_available=bool(parsed.get("audio_available")),
        final_has_audio_stream=bool(parsed.get("final_has_audio_stream")),
        theater_release_ready=bool(parsed.get("theater_release_ready")),
        final_video_duration_sec=_coerce_non_negative_float(parsed.get("final_video_duration_sec")),
        expected_narration_count=_coerce_non_negative_int(parsed.get("expected_narration_count")),
        rendered_narration_count=_coerce_non_negative_int(parsed.get("rendered_narration_count")),
    )
    return report.model_dump(exclude_none=True)


def _normalize_issue_codes(raw: Any) -> list[str]:
    codes: list[str] = []
    if not isinstance(raw, list):
        return codes
    for item in raw:
        if isinstance(item, dict):
            code = _clean_text(item.get("code")).lower()
        else:
            code = _clean_text(item).lower()
        if code and code not in codes:
            codes.append(code)
    return codes


def derive_default_video_assembly_decision(
    report: dict[str, Any],
    *,
    attempt: int,
    max_render_attempts: int,
) -> dict[str, Any]:
    status = _clean_text(report.get("status")).lower() or "failed"
    ready = bool(report.get("ready_to_publish"))
    retryable = bool(report.get("retryable"))
    issue_codes = _normalize_issue_codes(report.get("issues"))
    reason = _clean_text(report.get("reason")) or "Render report did not include a reason."

    if ready and status in {"complete", "approved"}:
        return {
            "done": True,
            "status": "approved",
            "reason": reason,
            "runtime_overrides": {},
        }

    if "scene_assets_missing" in issue_codes:
        return {
            "done": True,
            "status": "needs_manual_review",
            "reason": reason,
            "runtime_overrides": {},
        }

    if attempt >= max_render_attempts or not retryable:
        return {
            "done": True,
            "status": "needs_manual_review",
            "reason": reason,
            "runtime_overrides": {},
        }

    runtime_overrides: dict[str, bool] = {}
    audio_issue_codes = {
        "narration_missing",
        "audio_stream_missing",
        "audio_missing",
        "narration_incomplete",
    }
    if any(code in audio_issue_codes for code in issue_codes):
        runtime_overrides = {
            "force_tts": True,
            "disable_sfx": True,
        }
    elif "release_gate_failed" in issue_codes:
        runtime_overrides = {"force_captions": True}

    return {
        "done": False,
        "status": "retry_render",
        "reason": reason,
        "runtime_overrides": runtime_overrides,
    }


def normalize_video_assembly_decision(
    raw: Any,
    *,
    report: dict[str, Any],
    attempt: int,
    max_render_attempts: int,
) -> dict[str, Any]:
    default_decision = derive_default_video_assembly_decision(
        report,
        attempt=attempt,
        max_render_attempts=max_render_attempts,
    )
    parsed = parse_video_assembly_json(raw)
    if not parsed:
        return default_decision

    status = _clean_text(parsed.get("status")).lower()
    reason = _clean_text(parsed.get("reason")) or default_decision["reason"]
    runtime_overrides = _normalize_runtime_overrides(parsed.get("runtime_overrides"))
    report_status = _clean_text(report.get("status")).lower()
    report_ready = bool(report.get("ready_to_publish")) and report_status in {"complete", "approved"}
    retryable = bool(report.get("retryable"))

    # The deterministic render report is the source of truth for publishability.
    # If the worker says the movie is ready, do not let a flaky auditor/finalizer
    # downgrade it to a retry or manual review.
    if report_ready and status in {"retry_render", "needs_manual_review"}:
        return default_decision

    if status == "approved":
        if report_ready:
            return {
                "done": True,
                "status": "approved",
                "reason": reason,
                "runtime_overrides": {},
            }
        return default_decision

    if status == "retry_render":
        if attempt < max_render_attempts and retryable:
            return {
                "done": False,
                "status": "retry_render",
                "reason": reason,
                "runtime_overrides": runtime_overrides,
            }
        return default_decision

    if status == "needs_manual_review":
        return {
            "done": True,
            "status": "needs_manual_review",
            "reason": reason,
            "runtime_overrides": {},
        }

    return default_decision


def build_storybook_video_assembly_summary(state: dict[str, Any]) -> dict[str, Any]:
    render_history = [
        item
        for item in list(state.get("video_assembly_render_history", []) or [])
        if isinstance(item, dict)
    ]
    decision_history = [
        item
        for item in list(state.get("video_assembly_decision_history", []) or [])
        if isinstance(item, dict)
    ]
    final_summary = parse_video_assembly_json(state.get("video_assembly_summary")) or {}
    last_report = render_history[-1] if render_history else {}
    last_decision = decision_history[-1] if decision_history else {}
    runtime_overrides = _normalize_runtime_overrides(
        state.get("video_assembly_runtime_overrides") or last_decision.get("runtime_overrides")
    )
    report_status = _clean_text(last_report.get("status")).lower()
    report_ready = bool(last_report.get("ready_to_publish")) and report_status in {"complete", "approved"}
    decision_status = _clean_text(last_decision.get("status")).lower()
    ready_to_publish = bool(report_ready and decision_status == "approved")
    status = (
        _clean_text(final_summary.get("status"))
        or _clean_text(last_decision.get("status"))
        or (_clean_text(last_report.get("status")) if ready_to_publish else "needs_manual_review")
    )
    if not status:
        status = "needs_manual_review"
    if not ready_to_publish and status.lower() == "approved":
        status = "needs_manual_review"

    return {
        "source": "adk_video_assembly_director",
        "status": status,
        "ready_to_publish": ready_to_publish,
        "attempts": len(render_history),
        "reason": _clean_text(final_summary.get("reason"))
        or _clean_text(last_decision.get("reason"))
        or _clean_text(last_report.get("reason")),
        "runtime_overrides": runtime_overrides,
        "render_history": render_history,
        "decision_history": decision_history,
        "final_report": last_report,
    }


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


_PLANNER_TEMPLATE = """\
You are the Video Assembly Director for a children's storybook movie.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene count: {scene_count}
Story tone: {story_tone}
Movie pacing: {storybook_movie_pacing}
Max render attempts: {video_assembly_max_attempts}

Return JSON only:
{
  "assembly_goal": "one short sentence",
  "render_profile": "balanced|narration_first|quality_first",
  "must_keep": ["short item", "short item"],
  "audio_policy": {
    "narration_required": true,
    "music_optional": true,
    "sfx_optional": true
  },
  "retry_policy": "one short sentence"
}

Keep it concise and practical for a young child-facing movie.
"""


_AUDITOR_TEMPLATE = """\
You are the Video Assembly Director auditing the latest movie render.

Director plan JSON:
{video_assembly_plan}

Latest render report JSON:
{video_assembly_render_report}

Render history JSON:
{video_assembly_render_history_json}

Decision history JSON:
{video_assembly_decision_history_json}

Current render attempt: {video_assembly_attempt}
Max render attempts: {video_assembly_max_attempts}

Rules:
- Approve only if the movie is ready to publish.
- Narration is required whenever the render report says audio is expected.
- If narration or the audio stream is missing, retry with narration-first settings.
- Keep retries minimal and practical. Do not ask for more than one focused retry at a time.
- If the report is not retryable or attempts are exhausted, escalate to manual review.

Return JSON only:
{
  "done": true,
  "status": "approved|retry_render|needs_manual_review",
  "reason": "one short sentence",
  "runtime_overrides": {
    "force_tts": true,
    "disable_music": false,
    "disable_sfx": false,
    "force_captions": false
  }
}
"""


_FINALIZER_TEMPLATE = """\
You are the Video Assembly Director closing the movie assembly workflow.

Director plan JSON:
{video_assembly_plan}

Render history JSON:
{video_assembly_render_history_json}

Decision history JSON:
{video_assembly_decision_history_json}

Return JSON only:
{
  "status": "approved|needs_manual_review",
  "ready_to_publish": true,
  "attempts": 1,
  "reason": "one short sentence"
}
"""


async def _planner_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_PLANNER_TEMPLATE, context)


async def _auditor_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_AUDITOR_TEMPLATE, context)


async def _finalizer_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_FINALIZER_TEMPLATE, context)


class StorybookVideoAssemblyWorkflowAgent(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    planner_agent: BaseAgent
    executor_agent: BaseAgent
    auditor_agent: BaseAgent
    finalizer_agent: BaseAgent
    render_callback: Any
    max_render_attempts: int = 2

    def __init__(
        self,
        *,
        planner_agent: BaseAgent,
        executor_agent: BaseAgent,
        auditor_agent: BaseAgent,
        finalizer_agent: BaseAgent,
        render_callback: RenderCallback,
        max_render_attempts: int,
    ) -> None:
        super().__init__(
            name="storybook_video_assembly_director",
            description="Agentic ADK workflow for planning, auditing, and approving final movie assembly.",
            sub_agents=[planner_agent, executor_agent, auditor_agent, finalizer_agent],
            planner_agent=planner_agent,
            executor_agent=executor_agent,
            auditor_agent=auditor_agent,
            finalizer_agent=finalizer_agent,
            render_callback=render_callback,
            max_render_attempts=max_render_attempts,
        )

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        initial_state_updates: dict[str, Any] = {}
        for key, default in (
            ("video_assembly_render_history", []),
            ("video_assembly_decision_history", []),
            ("video_assembly_render_history_json", "[]"),
            ("video_assembly_decision_history_json", "[]"),
            ("video_assembly_runtime_overrides", {}),
        ):
            if key not in state:
                initial_state_updates[key] = default
        initial_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates=initial_state_updates,
        )
        if initial_event is not None:
            yield initial_event

        async for event in self.planner_agent.run_async(ctx):
            yield event

        for attempt in range(1, self.max_render_attempts + 1):
            attempt_state_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "video_assembly_attempt": attempt,
                },
            )
            if attempt_state_event is not None:
                yield attempt_state_event

            async for event in self.executor_agent.run_async(ctx):
                yield event

            async for event in self.auditor_agent.run_async(ctx):
                yield event

            report = dict(state.get("video_assembly_render_report_structured") or {})
            if not report:
                report = parse_video_assembly_json(state.get("video_assembly_render_report")) or {}
            if not report:
                report = normalize_video_assembly_render_report(
                    {},
                    runtime_overrides=state.get("video_assembly_runtime_overrides"),
                    default_reason="Render executor did not publish a report.",
                )
            decision = normalize_video_assembly_decision(
                state.get("video_assembly_decision"),
                report=report,
                attempt=attempt,
                max_render_attempts=self.max_render_attempts,
            )
            decision["runtime_overrides"] = _normalize_runtime_overrides(decision.get("runtime_overrides"))
            decision_history = list(state.get("video_assembly_decision_history", []) or [])
            decision_history.append(decision)
            decision_state_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "video_assembly_decision": json.dumps(decision, ensure_ascii=True),
                    "video_assembly_runtime_overrides": decision["runtime_overrides"],
                    "video_assembly_decision_history": decision_history,
                    "video_assembly_decision_history_json": json.dumps(decision_history, ensure_ascii=True),
                },
            )
            if decision_state_event is not None:
                yield decision_state_event

            if bool(decision.get("done")):
                break

        async for event in self.finalizer_agent.run_async(ctx):
            yield event


class StorybookVideoAssemblyRenderExecutorAgent(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    render_callback: Any

    def __init__(self, *, render_callback: RenderCallback) -> None:
        super().__init__(
            name="storybook_video_render_executor",
            description="Deterministic ADK executor that performs one final movie render attempt.",
            render_callback=render_callback,
        )

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        attempt = max(1, int(state.get("video_assembly_attempt") or 1))
        runtime_overrides = _normalize_runtime_overrides(state.get("video_assembly_runtime_overrides"))
        request = build_video_assembly_render_request(
            session_id=_clean_text(state.get("session_id")),
            attempt=attempt,
            runtime_overrides=runtime_overrides,
        )
        request_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "video_assembly_render_request": json.dumps(request, ensure_ascii=True),
                "video_assembly_render_request_structured": request,
            },
        )
        if request_event is not None:
            yield request_event

        try:
            raw_report = await self.render_callback(state, attempt)
        except Exception as exc:
            raw_report = {
                "status": "failed",
                "ready_to_publish": False,
                "retryable": False,
                "reason": str(exc) or "Render execution raised an exception.",
                "issues": [{"code": "render_callback_error", "detail": str(exc)}],
            }

        report = normalize_video_assembly_render_report(
            raw_report,
            runtime_overrides=runtime_overrides,
            default_reason="Render callback returned an invalid result.",
        )
        render_history = list(state.get("video_assembly_render_history", []) or [])
        render_history.append(report)
        render_state_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "video_assembly_render_report": json.dumps(report, ensure_ascii=True),
                "video_assembly_render_report_structured": report,
                "video_assembly_render_history": render_history,
                "video_assembly_render_history_json": json.dumps(render_history, ensure_ascii=True),
            },
        )
        if render_state_event is not None:
            yield render_state_event


def _build_workflow_agent(
    *,
    render_callback: RenderCallback,
    max_render_attempts: int,
) -> StorybookVideoAssemblyWorkflowAgent:
    model = _assembly_model()
    planner_agent = LlmAgent(
        name="video_assembly_planner",
        model=model,
        instruction=_planner_instruction,
        output_key="video_assembly_plan",
        output_schema=VideoAssemblyPlan,
    )
    executor_agent = StorybookVideoAssemblyRenderExecutorAgent(
        render_callback=render_callback,
    )
    auditor_agent = LlmAgent(
        name="video_assembly_auditor",
        model=model,
        instruction=_auditor_instruction,
        output_key="video_assembly_decision",
        output_schema=VideoAssemblyDecision,
    )
    finalizer_agent = LlmAgent(
        name="video_assembly_finalizer",
        model=model,
        instruction=_finalizer_instruction,
        output_key="video_assembly_summary",
        output_schema=VideoAssemblySummary,
    )
    return StorybookVideoAssemblyWorkflowAgent(
        planner_agent=planner_agent,
        executor_agent=executor_agent,
        auditor_agent=auditor_agent,
        finalizer_agent=finalizer_agent,
        render_callback=render_callback,
        max_render_attempts=max_render_attempts,
    )


async def run_storybook_video_assembly_workflow(
    *,
    session_id: str,
    initial_state: dict[str, Any],
    render_callback: RenderCallback,
    max_render_attempts: int = 2,
) -> dict[str, Any]:
    user_id = session_id or "storybook-video-assembly"
    workflow_session_id = f"{user_id}-video-assembly-{uuid.uuid4().hex[:8]}"
    app_name = "storybook_video_assembly_director"

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=workflow_session_id,
        state={
            **dict(initial_state or {}),
            "video_assembly_max_attempts": max(1, int(max_render_attempts)),
        },
    )

    runner = Runner(
        agent=_build_workflow_agent(
            render_callback=render_callback,
            max_render_attempts=max_render_attempts,
        ),
        app_name=app_name,
        session_service=session_service,
    )

    kickoff = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text="Run the final storybook video assembly workflow now.")],
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
        logger.warning("Storybook video assembly workflow produced no final session for %s.", session_id)
        return dict(initial_state or {})
    return dict(final_session.state or {})
