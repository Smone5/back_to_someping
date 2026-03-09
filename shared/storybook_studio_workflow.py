from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.utils import instructions_utils
from google.genai import types as genai_types
from pydantic import ConfigDict

logger = logging.getLogger(__name__)


def _studio_model() -> str:
    return (
        os.environ.get("STORYBOOK_STUDIO_MODEL", "").strip()
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


def parse_studio_json(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    return _extract_json_block(raw)


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


def _clean_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:240] if text else ""


def _normalize_narration_lines(
    raw_lines: Any,
    scene_count: int,
    fallback_lines: list[str] | None = None,
) -> list[str]:
    cleaned: list[str] = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            text = _clean_text(item)
            if text:
                cleaned.append(text)
    fallback = [_clean_text(line) for line in list(fallback_lines or []) if _clean_text(line)]
    target = max(0, int(scene_count))
    if target <= 0:
        return []
    if not cleaned and fallback:
        cleaned = list(fallback)
    if len(cleaned) > target:
        cleaned = cleaned[:target]
    if len(cleaned) < target:
        seed = cleaned[-1] if cleaned else (fallback[-1] if fallback else "")
        cleaned.extend([seed] * (target - len(cleaned)))
    if len(cleaned) < target:
        cleaned.extend([""] * (target - len(cleaned)))
    return cleaned[:target]


def _normalize_mix_guidance(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key, lower, upper in (
        ("music_volume", 0.0, 1.0),
        ("sfx_volume", 0.0, 1.0),
        ("narration_volume", 0.5, 3.0),
    ):
        value = raw.get(key)
        try:
            if value is not None:
                result[key] = max(lower, min(float(value), upper))
        except Exception:
            continue
    ducking = str(raw.get("ducking") or "").strip().lower()
    if ducking in {"strong", "medium", "light", "off"}:
        result["ducking"] = ducking
    return result


def _normalize_cue_map(raw_cues: Any, scene_count: int) -> dict[int, dict[str, Any]]:
    cues: dict[int, dict[str, Any]] = {}
    if not isinstance(raw_cues, list):
        return cues
    for item in raw_cues:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("scene_index", 0)) - 1
        except Exception:
            continue
        if idx < 0 or idx >= scene_count:
            continue
        prompt = _clean_text(item.get("prompt"))
        if not prompt:
            continue
        cue: dict[str, Any] = {"prompt": prompt}
        try:
            duration_seconds = item.get("duration_seconds")
            if duration_seconds is not None:
                cue["duration_seconds"] = max(0.6, min(float(duration_seconds), 30.0))
        except Exception:
            pass
        cues[idx] = cue
    return cues


def build_storybook_studio_plan_from_workflow_state(
    state: dict[str, Any],
    *,
    scene_count: int,
    fallback_narration_lines: list[str] | None = None,
) -> dict[str, Any]:
    narration_plan = parse_studio_json(state.get("studio_narration_plan")) or {}
    audio_plan = parse_studio_json(state.get("studio_audio_cue_plan")) or {}
    quality_report = parse_studio_json(state.get("studio_quality_report")) or {}

    mix_guidance = _normalize_mix_guidance(audio_plan.get("mix_guidance"))
    mix_guidance.update(_normalize_mix_guidance(quality_report.get("mix_guidance")))

    plan = {
        "status": _clean_text(quality_report.get("status")) or "observed",
        "reason": _clean_text(quality_report.get("reason")),
        "narration_style": _clean_text(narration_plan.get("narration_style")),
        "narration_lines": _normalize_narration_lines(
            narration_plan.get("narration_lines"),
            scene_count=scene_count,
            fallback_lines=fallback_narration_lines,
        ),
        "music_arc_prompt": _clean_text(audio_plan.get("music_arc_prompt")),
        "music_cues": _normalize_cue_map(audio_plan.get("music_cues"), scene_count),
        "sfx_cues": _normalize_cue_map(audio_plan.get("sfx_cues"), scene_count),
        "mix_guidance": mix_guidance,
        "kid_delight_notes": [
            _clean_text(note)
            for note in list(quality_report.get("kid_delight_notes", []) or [])
            if _clean_text(note)
        ][:6],
        "revision_history": [
            item
            for item in list(state.get("studio_revision_history", []) or [])
            if isinstance(item, dict)
        ],
        "quality_report": quality_report,
        "narration_plan": narration_plan,
        "audio_plan": audio_plan,
    }
    raw_music_enabled = audio_plan.get("music_enabled")
    if isinstance(raw_music_enabled, bool):
        plan["music_enabled"] = raw_music_enabled
    else:
        plan["music_enabled"] = bool(plan["music_arc_prompt"] or plan["music_cues"])
    return plan


def build_storybook_studio_summary(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": _clean_text(plan.get("status")) or "observed",
        "reason": _clean_text(plan.get("reason")),
        "narration_style": _clean_text(plan.get("narration_style")),
        "music_cue_count": len(plan.get("music_cues", {}) or {}),
        "sfx_cue_count": len(plan.get("sfx_cues", {}) or {}),
        "kid_delight_notes": list(plan.get("kid_delight_notes", []) or [])[:4],
        "revision_history": list(plan.get("revision_history", []) or [])[:4],
        "narration_preview": list(plan.get("narration_lines", []) or [])[:3],
        "mix_guidance": dict(plan.get("mix_guidance", {}) or {}),
    }


def _format_revision_feedback(report: dict[str, Any]) -> str:
    parts: list[str] = []
    narration_feedback = [
        _clean_text(item)
        for item in list(report.get("narration_feedback", []) or [])
        if _clean_text(item)
    ]
    audio_feedback = [
        _clean_text(item)
        for item in list(report.get("audio_feedback", []) or [])
        if _clean_text(item)
    ]
    if narration_feedback:
        parts.append("Narration feedback:")
        parts.extend(f"- {item}" for item in narration_feedback[:4])
    if audio_feedback:
        parts.append("Audio feedback:")
        parts.extend(f"- {item}" for item in audio_feedback[:4])
    if not parts:
        parts.append("- Keep the movie gentle, magical, clear, and simple for a 4-year-old.")
    return "\n".join(parts)


_NARRATION_TEMPLATE = """\
You are the Narration Director for a preschool story studio.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene count: {studio_scene_count}
Scene descriptions JSON: {scene_descriptions_json}

Revision notes:
{studio_feedback_notes}

Task:
- Write one short read-aloud narration line for each scene.
- Match the child's age band:
  - `4-5`: very short, concrete, gentle, magical, emotionally safe.
  - `6-7`: still warm and simple, but slightly richer and more adventurous.
  - `8-10`: still child-safe, but do not sound babyish.
- Use simple spoken language with natural rhythm.
- Keep each line to one short sentence.
- Avoid repeated sentence openings.
- Do not sound like a prompt, caption, or summary.
- No scary words, no moralizing, no exposition dump.

Return JSON only:
{
  "narration_style": "one short sentence",
  "narration_lines": ["short line", "short line"]
}
"""


_AUDIO_TEMPLATE = """\
You are the Music and Sound Director for a preschool story studio.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene count: {studio_scene_count}
Maximum music cues: {studio_max_music_cues}
Maximum SFX cues: {studio_max_sfx_cues}
Scene descriptions JSON: {scene_descriptions_json}
Narration plan JSON: {studio_narration_plan}

Revision notes:
{studio_feedback_notes}

Task:
- Plan a tiny, tasteful soundtrack that fits the child's age band without ever feeling scary or overwhelming.
- Music should feel warm and magical, never busy or overwhelming.
- SFX should be sparse and only for clear physical moments.
- Give a full-story music arc prompt for one continuous score.
- Also choose a few optional per-scene music and SFX cues.
- Prefer clarity of narration over flashy audio.

Return JSON only:
{
  "music_enabled": true,
  "music_arc_prompt": "one short production prompt",
  "music_cues": [{"scene_index": 1, "prompt": "cue prompt", "duration_seconds": 6}],
  "sfx_cues": [{"scene_index": 2, "prompt": "cue prompt", "duration_seconds": 2}],
  "mix_guidance": {
    "music_volume": 0.22,
    "sfx_volume": 0.28,
    "narration_volume": 1.7,
    "ducking": "strong"
  }
}
"""


_QC_TEMPLATE = """\
You are the Preschool Screening Lead for a children's story studio.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene count: {studio_scene_count}
Narration plan JSON: {studio_narration_plan}
Audio cue plan JSON: {studio_audio_cue_plan}

Task:
- Approve if this would feel warm, clear, and magical for the child's age band.
- Request revision only if narration is too long/repetitive or audio feels too busy.
- Keep fixes minimal and fast.

Return JSON only:
{
  "status": "approved|revise_narration|revise_audio|revise_both",
  "reason": "one short sentence",
  "narration_feedback": ["short note"],
  "audio_feedback": ["short note"],
  "kid_delight_notes": ["short note"],
  "mix_guidance": {
    "music_volume": 0.2,
    "sfx_volume": 0.24,
    "narration_volume": 1.7,
    "ducking": "strong"
  }
}
"""


async def _narration_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_NARRATION_TEMPLATE, context)


async def _audio_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_AUDIO_TEMPLATE, context)


async def _qc_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_QC_TEMPLATE, context)


class StorybookStudioWorkflowAgent(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    narration_agent: LlmAgent
    audio_agent: LlmAgent
    qa_agent: LlmAgent
    max_revision_rounds: int = 1

    def __init__(
        self,
        *,
        narration_agent: LlmAgent,
        audio_agent: LlmAgent,
        qa_agent: LlmAgent,
        max_revision_rounds: int,
    ) -> None:
        super().__init__(
            name="storybook_studio_workflow",
            description="Agentic studio workflow for preschool narration, soundtrack planning, and pre-render QA.",
            sub_agents=[narration_agent, audio_agent, qa_agent],
            narration_agent=narration_agent,
            audio_agent=audio_agent,
            qa_agent=qa_agent,
            max_revision_rounds=max_revision_rounds,
        )

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        initial_state_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                key: default
                for key, default in (
                    ("studio_feedback_notes", "No revision notes yet."),
                    ("studio_revision_history", []),
                )
                if key not in state
            },
        )
        if initial_state_event is not None:
            yield initial_state_event

        async for event in self.narration_agent.run_async(ctx):
            yield event
        async for event in self.audio_agent.run_async(ctx):
            yield event

        for revision_round in range(self.max_revision_rounds + 1):
            async for event in self.qa_agent.run_async(ctx):
                yield event

            report = parse_studio_json(state.get("studio_quality_report")) or {}
            status = str(report.get("status") or "").strip().lower() or "approved"
            revision_history = list(state.get("studio_revision_history", []) or [])
            revision_history.append(
                {
                    "round": revision_round + 1,
                    "status": status,
                    "reason": _clean_text(report.get("reason")),
                }
            )
            history_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={"studio_revision_history": revision_history},
            )
            if history_event is not None:
                yield history_event

            if status == "approved" or revision_round >= self.max_revision_rounds:
                break

            feedback_notes = _format_revision_feedback(report)
            feedback_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={"studio_feedback_notes": feedback_notes},
            )
            if feedback_event is not None:
                yield feedback_event

            if status in {"revise_narration", "revise_both"}:
                async for event in self.narration_agent.run_async(ctx):
                    yield event
            if status in {"revise_audio", "revise_both"}:
                async for event in self.audio_agent.run_async(ctx):
                    yield event


def _build_workflow_agent(
    *,
    max_revision_rounds: int,
) -> StorybookStudioWorkflowAgent:
    model = _studio_model()
    narration_agent = LlmAgent(
        name="studio_narration_director",
        model=model,
        instruction=_narration_instruction,
        output_key="studio_narration_plan",
    )
    audio_agent = LlmAgent(
        name="studio_audio_director",
        model=model,
        instruction=_audio_instruction,
        output_key="studio_audio_cue_plan",
    )
    qa_agent = LlmAgent(
        name="studio_preschool_qa",
        model=model,
        instruction=_qc_instruction,
        output_key="studio_quality_report",
    )
    return StorybookStudioWorkflowAgent(
        narration_agent=narration_agent,
        audio_agent=audio_agent,
        qa_agent=qa_agent,
        max_revision_rounds=max_revision_rounds,
    )


async def run_storybook_studio_workflow(
    *,
    session_id: str,
    initial_state: dict[str, Any],
    max_revision_rounds: int = 1,
) -> dict[str, Any]:
    user_id = session_id or "storybook-studio"
    workflow_session_id = f"{user_id}-studio-{uuid.uuid4().hex[:8]}"
    app_name = "storybook_studio_workflow"

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=workflow_session_id,
        state={
            **dict(initial_state or {}),
            "studio_max_revisions": max(0, int(max_revision_rounds)),
            "studio_feedback_notes": "No revision notes yet.",
        },
    )

    runner = Runner(
        agent=_build_workflow_agent(max_revision_rounds=max_revision_rounds),
        app_name=app_name,
        session_service=session_service,
    )

    kickoff = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text="Run the preschool story studio workflow now.")],
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
        logger.warning("Storybook studio workflow produced no final session for %s.", session_id)
        return dict(initial_state or {})
    return dict(final_session.state or {})
