from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any, Literal

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


class StudioMixGuidance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    music_volume: float | None = None
    sfx_volume: float | None = None
    narration_volume: float | None = None
    ducking: Literal["strong", "medium", "light", "off"] | None = None


class StudioNarrationPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    narration_style: str
    narration_lines: list[str] = Field(default_factory=list)


class StudioNarrationReviewReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    direction: Literal["forward", "backward"]
    status: Literal["approved", "revise_narration"]
    reason: str
    narration_feedback: list[str] = Field(default_factory=list)
    kid_delight_notes: list[str] = Field(default_factory=list)


class StudioAudioCue(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scene_index: int
    prompt: str
    duration_seconds: float | None = None


class StudioAudioCuePlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    music_enabled: bool
    music_arc_prompt: str
    music_cues: list[StudioAudioCue] = Field(default_factory=list)
    sfx_cues: list[StudioAudioCue] = Field(default_factory=list)
    mix_guidance: StudioMixGuidance = Field(default_factory=StudioMixGuidance)


class StudioQualityReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["approved", "revise_narration", "revise_audio", "revise_both"]
    reason: str
    narration_feedback: list[str] = Field(default_factory=list)
    audio_feedback: list[str] = Field(default_factory=list)
    kid_delight_notes: list[str] = Field(default_factory=list)
    mix_guidance: StudioMixGuidance = Field(default_factory=StudioMixGuidance)


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
    if isinstance(raw, BaseModel):
        return raw.model_dump(exclude_none=True)
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


def _dedupe_clean_texts(raw_items: Any, *, limit: int | None = None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in list(raw_items or []):
        text = _clean_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
        if limit is not None and len(cleaned) >= limit:
            break
    return cleaned


def _merge_narration_review_reports(reports: list[dict[str, Any]] | None) -> dict[str, Any]:
    normalized_reports: list[dict[str, Any]] = []
    for raw in list(reports or []):
        if not isinstance(raw, dict):
            continue
        direction = _clean_text(raw.get("direction")).lower()
        if direction not in {"forward", "backward"}:
            continue
        status = _clean_text(raw.get("status")).lower()
        if status not in {"approved", "revise_narration"}:
            status = "approved"
        normalized_reports.append(
            {
                "direction": direction,
                "status": status,
                "reason": _clean_text(raw.get("reason")),
                "narration_feedback": _dedupe_clean_texts(raw.get("narration_feedback"), limit=6),
                "kid_delight_notes": _dedupe_clean_texts(raw.get("kid_delight_notes"), limit=6),
            }
        )

    needs_revision = any(report["status"] == "revise_narration" for report in normalized_reports)
    merged_feedback = _dedupe_clean_texts(
        [
            item
            for report in normalized_reports
            for item in list(report.get("narration_feedback", []) or [])
        ],
        limit=8,
    )
    merged_delight = _dedupe_clean_texts(
        [
            item
            for report in normalized_reports
            for item in list(report.get("kid_delight_notes", []) or [])
        ],
        limit=6,
    )
    merged_reasons = _dedupe_clean_texts(
        [report.get("reason", "") for report in normalized_reports],
        limit=3,
    )
    if merged_reasons:
        merged_reason = " ".join(merged_reasons)
    elif needs_revision:
        merged_reason = "Narration needs another pass to feel like one complete story."
    else:
        merged_reason = "Narration reads like one connected story from beginning to end."

    return {
        "status": "revise_narration" if needs_revision else "approved",
        "reason": merged_reason,
        "narration_feedback": merged_feedback,
        "kid_delight_notes": merged_delight,
        "directions_reviewed": [
            report["direction"]
            for report in normalized_reports
        ],
    }


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
    narration_review = parse_studio_json(state.get("studio_narration_review_summary")) or {}
    audio_plan = parse_studio_json(state.get("studio_audio_cue_plan")) or {}
    quality_report = parse_studio_json(state.get("studio_quality_report")) or {}
    plan_status = _clean_text(quality_report.get("status")) or "observed"
    plan_reason = _clean_text(quality_report.get("reason"))
    narration_review_status = _clean_text(narration_review.get("status")).lower()
    narration_review_reason = _clean_text(narration_review.get("reason"))

    if narration_review_status == "revise_narration":
        plan_status = "revise_narration"
        if narration_review_reason:
            plan_reason = narration_review_reason

    mix_guidance = _normalize_mix_guidance(audio_plan.get("mix_guidance"))
    mix_guidance.update(_normalize_mix_guidance(quality_report.get("mix_guidance")))

    plan = {
        "status": plan_status,
        "reason": plan_reason,
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
        "narration_review": narration_review,
        "revision_history": [
            item
            for item in list(state.get("studio_revision_history", []) or [])
            if isinstance(item, dict)
        ],
        "narration_revision_history": [
            item
            for item in list(state.get("studio_narration_revision_history", []) or [])
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
        "narration_review_status": _clean_text(
            (plan.get("narration_review") or {}).get("status")
            if isinstance(plan.get("narration_review"), dict)
            else ""
        ),
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
Story pages JSON: {studio_story_pages_json}
Continuity world-state notes: {studio_continuity_world_state_text}
Child delight anchors:
{studio_child_delight_anchors_text}
Fallback narration draft JSON: {studio_fallback_narration_json}
Max spoken words per scene line: {studio_scene_max_words}

Revision notes:
{studio_feedback_notes}

Task:
- Write one short read-aloud narration line for each scene.
- Read the whole adventure first so you understand how it begins, how it changes, and how it ends before writing line one.
- Silently plan a clear beginning, middle, and ending arc across all scenes, then write the page lines to match that arc.
- Treat the scene descriptions and story pages as raw production notes from a child's live adventure, not as final prose.
- Your job is to turn those notes into a coherent storybook retelling that makes the whole adventure feel intentional and connected.
- You are not transcribing the child literally. You are writing the polished storybook version of what happened.
- Make the lines feel like one connected story from page to page, not isolated captions.
- The opening line should feel like the true start of a story, not a label for a picture.
- Let each line clearly follow the previous scene's action, place, or feeling.
- Middle lines should feel like the adventure is moving forward.
- Use the continuity notes and child delight anchors to carry recurring characters, props, wishes, and destinations through the story.
- If the child jumps suddenly to a new place, bridge it in story form so the move feels like the next beat of the same adventure.
- Prefer concrete details that are actually present in the story pages over generic labels from the raw scene descriptions.
- Treat the story pages as the visual ground truth for what is on screen.
- Use the story summary only to connect scenes, never to add a new object, building, room, destination, or character that is missing from the current page notes.
- Never mention a landmark or prop unless it is supported by the current page's story page notes or the neighboring scene notes.
- Rewrite command-like fragments such as "go inside the castle" or "let's go the other way" into natural story narration instead of repeating them literally.
- If the raw notes are messy, contradictory, or incomplete, resolve them into the clearest gentle adventure beat that still matches the pictures and continuity notes.
- Write the final scene with full awareness of how the story began, so the ending feels earned and complete.
- Land the final scene on a satisfying story beat, not a generic label or meta ending.
- Every scene line must stay at or under {studio_scene_max_words} spoken words.
- Match the child's age band:
  - `4-5`: very short, concrete, gentle, magical, emotionally safe.
  - `6-7`: still warm and simple, but slightly richer and more adventurous.
  - `8-10`: still child-safe, but do not sound babyish.
- Use simple spoken language with natural rhythm.
- Keep each line to one short sentence.
- Avoid repeated sentence openings.
- Do not sound like a prompt, caption, or summary.
- Never use filler lines like "The end.", "The end appears.", or "This is page three."
- No scary words, no moralizing, no exposition dump.

Return JSON only:
{
  "narration_style": "one short sentence",
  "narration_lines": ["short line", "short line"]
}
"""


_NARRATION_FORWARD_REVIEW_TEMPLATE = """\
You are the Forward Story Editor for a preschool story studio.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene count: {studio_scene_count}
Story pages JSON: {studio_story_pages_json}
Continuity world-state notes: {studio_continuity_world_state_text}
Child delight anchors:
{studio_child_delight_anchors_text}
Narration plan JSON: {studio_narration_plan}
Scene line word limit: {studio_scene_max_words}

Task:
- Review the narration from the first scene to the final scene.
- Check that the opening line truly starts the story, the middle lines move the adventure forward, and the final line feels like a satisfying ending.
- Request revision if the narration feels like disconnected captions instead of one continuous story.
- Request revision if the story jumps abruptly without a natural bridge, repeats the same opening pattern, or ends on a weak label.
- Request revision if the narration repeats command-like raw notes instead of turning them into story prose.
- Request revision if any line sounds incomplete, clipped, or awkwardly unfinished.
- Request revision if a line mentions a place, prop, building, room, character, or landmark that is not supported by the relevant story page notes.
- Keep feedback short and specific.

Return JSON only:
{
  "direction": "forward",
  "status": "approved|revise_narration",
  "reason": "one short sentence",
  "narration_feedback": ["short note"],
  "kid_delight_notes": ["short note"]
}
"""


_NARRATION_BACKWARD_REVIEW_TEMPLATE = """\
You are the Backward Story Editor for a preschool story studio.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene count: {studio_scene_count}
Story pages JSON: {studio_story_pages_json}
Continuity world-state notes: {studio_continuity_world_state_text}
Child delight anchors:
{studio_child_delight_anchors_text}
Narration plan JSON: {studio_narration_plan}
Scene line word limit: {studio_scene_max_words}

Task:
- Review the narration from the final scene back to the first scene.
- Check that the ending feels earned, that it resolves or warmly lands the adventure, and that the earlier lines set up that ending naturally.
- Request revision if the last line feels generic, abrupt, or disconnected from the story that came before it.
- Request revision if reading backward reveals missing setup, dropped continuity, or a middle section that does not actually lead to the ending.
- Request revision if the narration sounds like isolated picture labels instead of one remembered adventure.
- Request revision if any line sounds incomplete, clipped, or awkwardly unfinished.
- Request revision if a line mentions a place, prop, building, room, character, or landmark that is not supported by the relevant story page notes.
- Keep feedback short and specific.

Return JSON only:
{
  "direction": "backward",
  "status": "approved|revise_narration",
  "reason": "one short sentence",
  "narration_feedback": ["short note"],
  "kid_delight_notes": ["short note"]
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
Story pages JSON: {studio_story_pages_json}
Continuity world-state notes: {studio_continuity_world_state_text}
Child delight anchors:
{studio_child_delight_anchors_text}
Narration plan JSON: {studio_narration_plan}
Audio cue plan JSON: {studio_audio_cue_plan}
Scene line word limit: {studio_scene_max_words}

Task:
- Approve if this would feel warm, clear, and magical for the child's age band.
- Request revision if narration is too long, repetitive, disconnected between scenes, merely labels the picture, or ends on a generic filler line.
- Request revision if the narration ignores strong continuity clues or leaves the child's adventure feeling random instead of connected.
- Request revision if the narration repeats command-like fragments from the raw notes instead of turning them into natural story sentences.
- Request revision if the narration mentions a place, prop, building, room, character, or landmark that is not supported by the relevant story page notes.
- Request revision if audio feels too busy or would bury the narration.
- Request revision if any narration line exceeds {studio_scene_max_words} words or trails off awkwardly.
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


async def _narration_forward_review_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_NARRATION_FORWARD_REVIEW_TEMPLATE, context)


async def _narration_backward_review_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_NARRATION_BACKWARD_REVIEW_TEMPLATE, context)


async def _qc_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_QC_TEMPLATE, context)


class StorybookStudioWorkflowAgent(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    narration_agent: LlmAgent
    narration_forward_review_agent: LlmAgent
    narration_backward_review_agent: LlmAgent
    audio_agent: LlmAgent
    qa_agent: LlmAgent
    max_revision_rounds: int = 1

    def __init__(
        self,
        *,
        narration_agent: LlmAgent,
        narration_forward_review_agent: LlmAgent,
        narration_backward_review_agent: LlmAgent,
        audio_agent: LlmAgent,
        qa_agent: LlmAgent,
        max_revision_rounds: int,
    ) -> None:
        super().__init__(
            name="storybook_studio_workflow",
            description="Agentic studio workflow for preschool narration, soundtrack planning, and pre-render QA.",
            sub_agents=[
                narration_agent,
                narration_forward_review_agent,
                narration_backward_review_agent,
                audio_agent,
                qa_agent,
            ],
            narration_agent=narration_agent,
            narration_forward_review_agent=narration_forward_review_agent,
            narration_backward_review_agent=narration_backward_review_agent,
            audio_agent=audio_agent,
            qa_agent=qa_agent,
            max_revision_rounds=max_revision_rounds,
        )

    async def _run_narration_phase(self, ctx: InvocationContext):
        state = ctx.session.state

        async for event in self.narration_agent.run_async(ctx):
            yield event

        for revision_round in range(self.max_revision_rounds + 1):
            async for event in self.narration_forward_review_agent.run_async(ctx):
                yield event
            async for event in self.narration_backward_review_agent.run_async(ctx):
                yield event

            forward_review = parse_studio_json(state.get("studio_narration_forward_review")) or {}
            backward_review = parse_studio_json(state.get("studio_narration_backward_review")) or {}
            merged_review = _merge_narration_review_reports([forward_review, backward_review])

            narration_revision_history = list(state.get("studio_narration_revision_history", []) or [])
            narration_revision_history.append(
                {
                    "round": revision_round + 1,
                    "status": _clean_text(merged_review.get("status")),
                    "reason": _clean_text(merged_review.get("reason")),
                }
            )
            merged_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "studio_narration_review_summary": merged_review,
                    "studio_narration_revision_history": narration_revision_history,
                },
            )
            if merged_event is not None:
                yield merged_event

            if merged_review.get("status") == "approved" or revision_round >= self.max_revision_rounds:
                break

            feedback_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={"studio_feedback_notes": _format_revision_feedback(merged_review)},
            )
            if feedback_event is not None:
                yield feedback_event

            async for event in self.narration_agent.run_async(ctx):
                yield event

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
                    ("studio_narration_revision_history", []),
                    ("studio_narration_review_summary", {}),
                )
                if key not in state
            },
        )
        if initial_state_event is not None:
            yield initial_state_event

        async for event in self._run_narration_phase(ctx):
            yield event
        feedback_reset_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={"studio_feedback_notes": "No revision notes yet."},
        )
        if feedback_reset_event is not None:
            yield feedback_reset_event
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
                async for event in self._run_narration_phase(ctx):
                    yield event
                feedback_reset_event = _build_state_delta_event(
                    ctx,
                    author=self.name,
                    updates={"studio_feedback_notes": "No revision notes yet."},
                )
                if feedback_reset_event is not None:
                    yield feedback_reset_event
                async for event in self.audio_agent.run_async(ctx):
                    yield event
                continue
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
        output_schema=StudioNarrationPlan,
    )
    narration_forward_review_agent = LlmAgent(
        name="studio_narration_forward_review",
        model=model,
        instruction=_narration_forward_review_instruction,
        output_key="studio_narration_forward_review",
        output_schema=StudioNarrationReviewReport,
    )
    narration_backward_review_agent = LlmAgent(
        name="studio_narration_backward_review",
        model=model,
        instruction=_narration_backward_review_instruction,
        output_key="studio_narration_backward_review",
        output_schema=StudioNarrationReviewReport,
    )
    audio_agent = LlmAgent(
        name="studio_audio_director",
        model=model,
        instruction=_audio_instruction,
        output_key="studio_audio_cue_plan",
        output_schema=StudioAudioCuePlan,
    )
    qa_agent = LlmAgent(
        name="studio_preschool_qa",
        model=model,
        instruction=_qc_instruction,
        output_key="studio_quality_report",
        output_schema=StudioQualityReport,
    )
    return StorybookStudioWorkflowAgent(
        narration_agent=narration_agent,
        narration_forward_review_agent=narration_forward_review_agent,
        narration_backward_review_agent=narration_backward_review_agent,
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
            "studio_story_pages_json": dict(initial_state or {}).get("studio_story_pages_json", "[]"),
            "studio_child_delight_anchors_text": dict(initial_state or {}).get(
                "studio_child_delight_anchors_text",
                "No child delight anchors recorded.",
            ),
            "studio_narration_revision_history": [],
            "studio_narration_review_summary": {},
            "studio_continuity_world_state_text": dict(initial_state or {}).get(
                "studio_continuity_world_state_text",
                "No continuity state recorded.",
            ),
            "studio_fallback_narration_json": dict(initial_state or {}).get(
                "studio_fallback_narration_json",
                "[]",
            ),
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
