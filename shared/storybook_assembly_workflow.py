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

ReviewCallback = Callable[[dict[str, Any], int], Awaitable[dict[str, Any]]]
RepairCallback = Callable[[dict[str, Any], dict[str, Any], int], Awaitable[dict[str, Any]]]


class AssemblyDirectorPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quality_bar: str
    pacing_focus: list[str] = Field(default_factory=list)
    audio_focus: list[str] = Field(default_factory=list)
    motion_focus: list[str] = Field(default_factory=list)
    repair_strategy: str


class AssemblyDirectorDecision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    done: bool
    reason: str
    selected_scene_indices: list[int] = Field(default_factory=list)
    selected_repair_limit: int = 0


class AssemblyDirectorSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["passed", "passed_with_warnings", "passed_with_repairs", "needs_manual_review"]
    ready_to_render: bool
    iterations_completed: int
    repairs_applied: int
    reason: str


def _director_model() -> str:
    return (
        os.environ.get("STORYBOOK_DIRECTOR_MODEL", "").strip()
        or os.environ.get("STORYBOOK_POST_MOVIE_REVIEW_MODEL", "").strip()
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


def _flatten_review_feedback(review: dict[str, Any]) -> str:
    lines: list[str] = []
    for note in review.get("global_feedback", []) or []:
        text = str(note).strip()
        if text:
            lines.append(f"- {text}")
    for fix in review.get("scene_fixes", []) or []:
        if not isinstance(fix, dict):
            continue
        issue = str(fix.get("issue") or "").strip()
        if not issue:
            continue
        scene_index = fix.get("scene_index")
        lines.append(f"- Scene {scene_index}: {issue}")
    if not lines:
        lines.append("- No major issues found.")
    return "\n".join(lines[:12])


def _selected_scene_indices(decision: dict[str, Any]) -> list[int]:
    picked: list[int] = []
    for raw in decision.get("selected_scene_indices", []) or []:
        try:
            idx = int(raw)
        except Exception:
            continue
        if idx > 0 and idx not in picked:
            picked.append(idx)
    return picked


def _derive_default_decision(review: dict[str, Any], max_repairs_per_pass: int) -> dict[str, Any]:
    fixes = [
        fix
        for fix in (review.get("scene_fixes", []) or [])
        if isinstance(fix, dict) and bool(fix.get("actionable"))
    ]
    if not fixes:
        return {
            "done": True,
            "reason": "No actionable fixes remain.",
            "selected_scene_indices": [],
            "selected_repair_limit": 0,
        }
    selected_scene_indices: list[int] = []
    for fix in fixes[:max_repairs_per_pass]:
        try:
            scene_index = int(fix.get("scene_index", 0) or 0)
        except Exception:
            scene_index = 0
        if scene_index > 0 and scene_index not in selected_scene_indices:
            selected_scene_indices.append(scene_index)
    return {
        "done": False,
        "reason": "Actionable fixes remain.",
        "selected_scene_indices": selected_scene_indices,
        "selected_repair_limit": max(1, min(max_repairs_per_pass, len(selected_scene_indices) or 1)),
    }


def _workflow_status_from_state(state: dict[str, Any]) -> str:
    summary = parse_workflow_json(state.get("assembly_director_summary"))
    if isinstance(summary, dict):
        status = str(summary.get("status") or "").strip()
        if status:
            return status
    reports = list(state.get("assembly_pass_reports", []) or [])
    if reports:
        return str(reports[-1].get("status") or "observed").strip() or "observed"
    return "observed"


def parse_workflow_json(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, BaseModel):
        return raw.model_dump(exclude_none=True)
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return None
    return _extract_json_block(raw)


def build_storyboard_report_from_workflow_state(state: dict[str, Any]) -> dict[str, Any]:
    raw_pass_reports = list(state.get("assembly_pass_reports", []) or [])
    pass_reports: list[dict[str, Any]] = []
    for report in raw_pass_reports:
        if not isinstance(report, dict):
            continue
        fixes = [
            fix
            for fix in (report.get("scene_fixes", []) or [])
            if isinstance(fix, dict) and bool(fix.get("actionable"))
        ]
        warnings = [
            fix
            for fix in (report.get("scene_fixes", []) or [])
            if isinstance(fix, dict) and not bool(fix.get("actionable"))
        ]
        pass_reports.append(
            {
                "pass": report.get("pass"),
                "status": report.get("status"),
                "fix_count": int(report.get("fix_count", len(fixes)) or len(fixes)),
                "warning_count": int(report.get("warning_count", len(warnings)) or len(warnings)),
                "global_feedback": list(report.get("global_feedback", []) or [])[:5],
            }
        )
    decision_history = list(state.get("assembly_decision_history", []) or [])
    repair_results = list(state.get("assembly_repair_results", []) or [])
    repairs_applied: list[dict[str, Any]] = []
    for result in repair_results:
        if isinstance(result, dict):
            repairs_applied.extend(list(result.get("repairs", []) or []))

    report: dict[str, Any] = {
        "status": _workflow_status_from_state(state),
        "source": "adk_storybook_director",
        "passes": pass_reports,
        "repairs_applied": repairs_applied,
        "decision_history": decision_history,
    }

    plan = parse_workflow_json(state.get("assembly_director_plan"))
    if plan:
        report["director_plan"] = plan
    summary = parse_workflow_json(state.get("assembly_director_summary"))
    if summary:
        report["director_summary"] = summary
        status = str(summary.get("status") or "").strip()
        if status:
            report["status"] = status
    return report


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
You are the Storybook Director planning a final children's movie assembly.

Story title: {story_title}
Child name: {child_name}
Child age: {child_age}
Child age band: {child_age_band}
Story summary: {story_summary}
Scene descriptions JSON: {scene_descriptions_json}
Maximum storyboard refinement passes: {assembly_max_passes}

Return JSON only:
{
  "quality_bar": "one short sentence",
  "pacing_focus": ["short bullet", "short bullet"],
  "audio_focus": ["short bullet", "short bullet"],
  "motion_focus": ["short bullet"],
  "repair_strategy": "one short sentence"
}

Keep it practical, warm, and concise for the child's age band.
"""


_CRITIC_TEMPLATE = """\
You are the Storybook Director deciding whether the current storyboard is ready to render.

Director plan JSON:
{assembly_director_plan}

Storyboard review JSON:
{assembly_review_json}

Storyboard review summary:
{assembly_review_feedback}

Current refinement pass: {assembly_iteration}
Maximum passes: {assembly_max_passes}

Rules:
- Approve when the review is `passed` or `passed_with_warnings`.
- If major or critical actionable fixes remain, select only the smallest useful repair set.
- Prefer repairing at most {assembly_max_repairs_per_pass} scenes in one pass.
- Minor warnings alone should not block render.

Return JSON only:
{
  "done": true,
  "reason": "one short sentence",
  "selected_scene_indices": [2, 5],
  "selected_repair_limit": 2
}
"""


_FINALIZER_TEMPLATE = """\
You are the Storybook Director closing out the assembly workflow.

Director plan JSON:
{assembly_director_plan}

All pass reports JSON:
{assembly_pass_reports_json}

Decision history JSON:
{assembly_decision_history_json}

Repair results JSON:
{assembly_repair_results_json}

Return JSON only:
{
  "status": "passed|passed_with_warnings|passed_with_repairs|needs_manual_review",
  "ready_to_render": true,
  "iterations_completed": 1,
  "repairs_applied": 0,
  "reason": "one short sentence"
}
"""


async def _planner_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_PLANNER_TEMPLATE, context)


async def _critic_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_CRITIC_TEMPLATE, context)


async def _finalizer_instruction(context: ReadonlyContext) -> str:
    return await instructions_utils.inject_session_state(_FINALIZER_TEMPLATE, context)


class StorybookAssemblyWorkflowAgent(BaseAgent):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    planner_agent: LlmAgent
    critic_agent: LlmAgent
    finalizer_agent: LlmAgent
    review_callback: Any
    repair_callback: Any
    max_revision_passes: int = 2
    max_repairs_per_pass: int = 2

    def __init__(
        self,
        *,
        planner_agent: LlmAgent,
        critic_agent: LlmAgent,
        finalizer_agent: LlmAgent,
        review_callback: ReviewCallback,
        repair_callback: RepairCallback,
        max_revision_passes: int,
        max_repairs_per_pass: int,
    ) -> None:
        super().__init__(
            name="storybook_assembly_director",
            description="Agentic ADK workflow for storyboard critique, repair, and render readiness.",
            sub_agents=[planner_agent, critic_agent, finalizer_agent],
            planner_agent=planner_agent,
            critic_agent=critic_agent,
            finalizer_agent=finalizer_agent,
            review_callback=review_callback,
            repair_callback=repair_callback,
            max_revision_passes=max_revision_passes,
            max_repairs_per_pass=max_repairs_per_pass,
        )

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        initial_state_updates: dict[str, Any] = {}
        for key, default in (
            ("assembly_pass_reports", []),
            ("assembly_decision_history", []),
            ("assembly_repair_results", []),
            ("assembly_pass_reports_json", "[]"),
            ("assembly_decision_history_json", "[]"),
            ("assembly_repair_results_json", "[]"),
        ):
            if key not in state:
                initial_state_updates[key] = default
        initial_state_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates=initial_state_updates,
        )
        if initial_state_event is not None:
            yield initial_state_event

        async for event in self.planner_agent.run_async(ctx):
            yield event

        for iteration in range(1, self.max_revision_passes + 1):
            review = await self.review_callback(state, iteration)
            if not isinstance(review, dict):
                review = {
                    "status": "needs_manual_review",
                    "global_feedback": ["Storyboard review returned an invalid result."],
                    "scene_fixes": [],
                }
            pass_reports = list(state.get("assembly_pass_reports", []) or [])
            pass_reports.append(review)
            review_state_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "assembly_iteration": iteration,
                    "assembly_review_json": json.dumps(review, ensure_ascii=True),
                    "assembly_review_feedback": _flatten_review_feedback(review),
                    "assembly_pass_reports": pass_reports,
                    "assembly_pass_reports_json": json.dumps(pass_reports, ensure_ascii=True),
                },
            )
            if review_state_event is not None:
                yield review_state_event

            async for event in self.critic_agent.run_async(ctx):
                yield event

            decision = parse_workflow_json(state.get("assembly_director_decision")) or _derive_default_decision(
                review,
                self.max_repairs_per_pass,
            )
            decision_history = list(state.get("assembly_decision_history", []) or [])
            decision_history.append(decision)
            decision_state_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "assembly_director_decision": json.dumps(decision, ensure_ascii=True),
                    "assembly_decision_history": decision_history,
                    "assembly_decision_history_json": json.dumps(decision_history, ensure_ascii=True),
                },
            )
            if decision_state_event is not None:
                yield decision_state_event

            if bool(decision.get("done")):
                break

            repair_result = await self.repair_callback(review, decision, iteration)
            if not isinstance(repair_result, dict):
                repair_result = {"repairs_applied": 0, "reason": "Repair callback returned an invalid result."}

            repair_results = list(state.get("assembly_repair_results", []) or [])
            repair_results.append(repair_result)
            repair_state_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "assembly_repair_results": repair_results,
                    "assembly_repair_results_json": json.dumps(repair_results, ensure_ascii=True),
                },
            )
            if repair_state_event is not None:
                yield repair_state_event

            if int(repair_result.get("repairs_applied", 0) or 0) <= 0:
                break

        async for event in self.finalizer_agent.run_async(ctx):
            yield event


def _build_workflow_agent(
    *,
    review_callback: ReviewCallback,
    repair_callback: RepairCallback,
    max_revision_passes: int,
    max_repairs_per_pass: int,
) -> StorybookAssemblyWorkflowAgent:
    model = _director_model()
    planner_agent = LlmAgent(
        name="assembly_planner",
        model=model,
        instruction=_planner_instruction,
        output_key="assembly_director_plan",
        output_schema=AssemblyDirectorPlan,
    )
    critic_agent = LlmAgent(
        name="assembly_critic",
        model=model,
        instruction=_critic_instruction,
        output_key="assembly_director_decision",
        output_schema=AssemblyDirectorDecision,
    )
    finalizer_agent = LlmAgent(
        name="assembly_finalizer",
        model=model,
        instruction=_finalizer_instruction,
        output_key="assembly_director_summary",
        output_schema=AssemblyDirectorSummary,
    )
    return StorybookAssemblyWorkflowAgent(
        planner_agent=planner_agent,
        critic_agent=critic_agent,
        finalizer_agent=finalizer_agent,
        review_callback=review_callback,
        repair_callback=repair_callback,
        max_revision_passes=max_revision_passes,
        max_repairs_per_pass=max_repairs_per_pass,
    )


async def run_storybook_director_workflow(
    *,
    session_id: str,
    initial_state: dict[str, Any],
    review_callback: ReviewCallback,
    repair_callback: RepairCallback,
    max_revision_passes: int = 2,
    max_repairs_per_pass: int = 2,
) -> dict[str, Any]:
    user_id = session_id or "storybook-director"
    workflow_session_id = f"{user_id}-director-{uuid.uuid4().hex[:8]}"
    app_name = "storybook_assembly_director"

    session_service = InMemorySessionService()
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=workflow_session_id,
        state={
            **dict(initial_state or {}),
            "assembly_max_passes": max(1, int(max_revision_passes)),
            "assembly_max_repairs_per_pass": max(1, int(max_repairs_per_pass)),
        },
    )

    runner = Runner(
        agent=_build_workflow_agent(
            review_callback=review_callback,
            repair_callback=repair_callback,
            max_revision_passes=max_revision_passes,
            max_repairs_per_pass=max_repairs_per_pass,
        ),
        app_name=app_name,
        session_service=session_service,
    )

    kickoff = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text="Run the storybook assembly workflow now.")],
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
        logger.warning("Storybook director workflow produced no final session for %s.", session_id)
        return dict(initial_state or {})
    return dict(final_session.state or {})
