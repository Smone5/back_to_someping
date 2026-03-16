from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.storybook_video_assembly_workflow import (
    StorybookVideoAssemblyRenderExecutorAgent,
    StorybookVideoAssemblyWorkflowAgent,
    _build_state_delta_event,
    build_video_assembly_render_request,
    build_storybook_video_assembly_summary,
    derive_default_video_assembly_decision,
    normalize_video_assembly_render_report,
    normalize_video_assembly_decision,
    run_storybook_video_assembly_workflow,
)


class StorybookVideoAssemblyWorkflowTests(unittest.TestCase):
    def test_default_decision_retries_audio_failures_with_narration_first_overrides(self) -> None:
        decision = derive_default_video_assembly_decision(
            {
                "status": "failed",
                "ready_to_publish": False,
                "retryable": True,
                "reason": "Narration was missing from the final movie.",
                "issues": [
                    {"code": "narration_missing", "detail": "Narration missing."},
                    {"code": "audio_stream_missing", "detail": "Audio stream missing."},
                ],
            },
            attempt=1,
            max_render_attempts=2,
        )

        self.assertFalse(decision["done"])
        self.assertEqual(decision["status"], "retry_render")
        self.assertTrue(decision["runtime_overrides"]["force_tts"])
        self.assertTrue(decision["runtime_overrides"]["disable_sfx"])
        self.assertNotIn("disable_music", decision["runtime_overrides"])

    def test_default_decision_escalates_missing_assets_without_retry(self) -> None:
        decision = derive_default_video_assembly_decision(
            {
                "status": "failed",
                "ready_to_publish": False,
                "retryable": True,
                "reason": "No scene assets found for the story.",
                "issues": [
                    {"code": "scene_assets_missing", "detail": "No scene assets found."},
                ],
            },
            attempt=1,
            max_render_attempts=2,
        )

        self.assertTrue(decision["done"])
        self.assertEqual(decision["status"], "needs_manual_review")
        self.assertEqual(decision["runtime_overrides"], {})

    def test_summary_uses_finalizer_output_and_keeps_render_history(self) -> None:
        summary = build_storybook_video_assembly_summary(
            {
                "video_assembly_render_history": [
                    {
                        "status": "complete",
                        "ready_to_publish": True,
                        "reason": "Final movie uploaded.",
                    }
                ],
                "video_assembly_decision_history": [
                    {
                        "status": "approved",
                        "reason": "Movie is ready.",
                        "runtime_overrides": {},
                    }
                ],
                "video_assembly_summary": json.dumps(
                    {
                        "status": "approved",
                        "ready_to_publish": True,
                        "attempts": 1,
                        "reason": "Movie is ready.",
                    }
                ),
            }
        )

        self.assertEqual(summary["source"], "adk_video_assembly_director")
        self.assertEqual(summary["status"], "approved")
        self.assertTrue(summary["ready_to_publish"])
        self.assertEqual(summary["attempts"], 1)
        self.assertEqual(len(summary["render_history"]), 1)

    def test_normalized_decision_rejects_hallucinated_approval_when_render_is_not_ready(self) -> None:
        decision = normalize_video_assembly_decision(
            {
                "done": True,
                "status": "approved",
                "reason": "Looks good.",
                "runtime_overrides": {"force_tts": False},
            },
            report={
                "status": "failed",
                "ready_to_publish": False,
                "retryable": True,
                "reason": "Narration missing.",
                "issues": [{"code": "narration_missing"}],
            },
            attempt=1,
            max_render_attempts=2,
        )

        self.assertFalse(decision["done"])
        self.assertEqual(decision["status"], "retry_render")
        self.assertTrue(decision["runtime_overrides"]["force_tts"])

    def test_normalized_decision_rejects_hallucinated_manual_review_when_render_is_ready(self) -> None:
        decision = normalize_video_assembly_decision(
            {
                "done": True,
                "status": "needs_manual_review",
                "reason": "Maximum render attempts exhausted; assembly consistently failing.",
                "runtime_overrides": {"force_captions": True},
            },
            report={
                "status": "complete",
                "ready_to_publish": True,
                "retryable": False,
                "reason": "Final storybook movie uploaded.",
                "issues": [],
            },
            attempt=1,
            max_render_attempts=2,
        )

        self.assertTrue(decision["done"])
        self.assertEqual(decision["status"], "approved")
        self.assertEqual(decision["runtime_overrides"], {})

    def test_summary_never_publishes_without_ready_report_and_approved_decision(self) -> None:
        summary = build_storybook_video_assembly_summary(
            {
                "video_assembly_render_history": [
                    {
                        "status": "failed",
                        "ready_to_publish": False,
                        "reason": "Narration missing.",
                    }
                ],
                "video_assembly_decision_history": [
                    {
                        "status": "retry_render",
                        "reason": "Retry with narration-first settings.",
                        "runtime_overrides": {"force_tts": True},
                    }
                ],
                "video_assembly_summary": json.dumps(
                    {
                        "status": "approved",
                        "ready_to_publish": True,
                        "attempts": 1,
                        "reason": "Looks good.",
                    }
                ),
            }
        )

        self.assertEqual(summary["status"], "needs_manual_review")
        self.assertFalse(summary["ready_to_publish"])

    def test_render_request_contract_is_typed_and_normalized(self) -> None:
        request = build_video_assembly_render_request(
            session_id=" session-123 ",
            attempt=0,
            runtime_overrides={
                "force_tts": True,
                "disable_music": "yes",
                "disable_sfx": False,
                "force_captions": True,
            },
        )

        self.assertEqual(request["session_id"], "session-123")
        self.assertEqual(request["attempt"], 1)
        self.assertEqual(
            request["runtime_overrides"],
            {
                "force_tts": True,
                "disable_music": False,
                "disable_sfx": False,
                "force_captions": True,
            },
        )

    def test_render_report_contract_rejects_invalid_payloads(self) -> None:
        report = normalize_video_assembly_render_report(
            "not-a-dict",
            runtime_overrides={"force_tts": True},
            default_reason="Render callback returned an invalid result.",
        )

        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["ready_to_publish"])
        self.assertEqual(
            report["runtime_overrides"],
            {
                "force_tts": True,
                "disable_music": False,
                "disable_sfx": False,
                "force_captions": False,
            },
        )
        self.assertEqual(report["reason"], "Render callback returned an invalid result.")


class _FakeWorkflowAgent(BaseAgent):
    render_callback: object
    max_render_attempts: int = 2

    async def _run_async_impl(self, ctx: InvocationContext):
        event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "video_assembly_plan": json.dumps({"assembly_goal": "Test final movie assembly."}),
                "video_assembly_render_history": [],
                "video_assembly_decision_history": [],
                "video_assembly_render_history_json": "[]",
                "video_assembly_decision_history_json": "[]",
                "video_assembly_runtime_overrides": {},
            },
        )
        if event is not None:
            yield event

        state = ctx.session.state
        for attempt in range(1, self.max_render_attempts + 1):
            request_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "video_assembly_attempt": attempt,
                    "video_assembly_render_request_structured": {
                        "session_id": "",
                        "attempt": attempt,
                        "runtime_overrides": dict(state.get("video_assembly_runtime_overrides") or {}),
                    },
                },
            )
            if request_event is not None:
                yield request_event
            report = await self.render_callback(state, attempt)
            render_history = list(state.get("video_assembly_render_history", []) or [])
            render_history.append(report)
            render_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "video_assembly_render_report_structured": report,
                    "video_assembly_render_history": render_history,
                    "video_assembly_render_history_json": json.dumps(render_history),
                },
            )
            if render_event is not None:
                yield render_event

            if attempt == 1:
                decision = {
                    "done": False,
                    "status": "retry_render",
                    "reason": "Retry with narration-first settings.",
                    "runtime_overrides": {"force_tts": True, "disable_sfx": True},
                }
            else:
                decision = {
                    "done": True,
                    "status": "approved",
                    "reason": "Movie is ready.",
                    "runtime_overrides": {},
                }

            decision_history = list(state.get("video_assembly_decision_history", []) or [])
            decision_history.append(decision)
            decision_event = _build_state_delta_event(
                ctx,
                author=self.name,
                updates={
                    "video_assembly_decision": json.dumps(decision),
                    "video_assembly_runtime_overrides": dict(decision.get("runtime_overrides") or {}),
                    "video_assembly_decision_history": decision_history,
                    "video_assembly_decision_history_json": json.dumps(decision_history),
                },
            )
            if decision_event is not None:
                yield decision_event

            if decision["done"]:
                break

        final_event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "video_assembly_summary": json.dumps(
                    {
                        "status": "approved",
                        "ready_to_publish": True,
                        "attempts": 2,
                        "reason": "Movie is ready.",
                    }
                )
            },
        )
        if final_event is not None:
            yield final_event


class _StaticPlannerAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext):
        event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "video_assembly_plan": json.dumps(
                    {
                        "assembly_goal": "Build the movie.",
                        "render_profile": "balanced",
                        "must_keep": ["narration"],
                        "audio_policy": {
                            "narration_required": True,
                            "music_optional": True,
                            "sfx_optional": True,
                        },
                        "retry_policy": "Retry once for missing narration.",
                    }
                )
            },
        )
        if event is not None:
            yield event


class _StaticAuditorAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        attempt = int(state.get("video_assembly_attempt") or 1)
        if attempt == 1:
            decision = {
                "done": False,
                "status": "retry_render",
                "reason": "Retry with narration-first settings.",
                "runtime_overrides": {"force_tts": True, "disable_sfx": True},
            }
        else:
            decision = {
                "done": True,
                "status": "approved",
                "reason": "Movie is ready.",
                "runtime_overrides": {},
            }
        event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={"video_assembly_decision": json.dumps(decision)},
        )
        if event is not None:
            yield event


class _StaticFinalizerAgent(BaseAgent):
    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        attempts = len(list(state.get("video_assembly_render_history", []) or []))
        event = _build_state_delta_event(
            ctx,
            author=self.name,
            updates={
                "video_assembly_summary": json.dumps(
                    {
                        "status": "approved",
                        "ready_to_publish": True,
                        "attempts": attempts,
                        "reason": "Movie is ready.",
                    }
                )
            },
        )
        if event is not None:
            yield event


class StorybookVideoAssemblyWorkflowRunnerTests(unittest.TestCase):
    def test_runner_preserves_state_across_attempts(self) -> None:
        seen_states: list[tuple[int, dict[str, object]]] = []

        async def render_callback(state: dict[str, object], attempt: int) -> dict[str, object]:
            seen_states.append((attempt, dict(state)))
            if attempt == 1:
                return {
                    "status": "failed",
                    "ready_to_publish": False,
                    "retryable": True,
                    "reason": "Narration missing.",
                    "issues": [{"code": "narration_missing"}],
                }
            return {
                "status": "complete",
                "ready_to_publish": True,
                "retryable": False,
                "reason": "Movie is ready.",
                "issues": [],
            }

        def fake_build(*, render_callback, max_render_attempts):
            return _FakeWorkflowAgent(
                name="fake_storybook_video_assembly_director",
                description="Test-only ADK workflow agent.",
                render_callback=render_callback,
                max_render_attempts=max_render_attempts,
            )

        with patch("shared.storybook_video_assembly_workflow._build_workflow_agent", fake_build):
            final_state = asyncio.run(
                run_storybook_video_assembly_workflow(
                    session_id="session-123",
                    initial_state={},
                    render_callback=render_callback,
                    max_render_attempts=2,
                )
            )

        self.assertEqual([attempt for attempt, _ in seen_states], [1, 2])
        self.assertEqual(
            seen_states[1][1].get("video_assembly_runtime_overrides"),
            {"force_tts": True, "disable_sfx": True},
        )
        self.assertEqual(
            final_state.get("video_assembly_render_request_structured"),
            {
                "session_id": "",
                "attempt": 2,
                "runtime_overrides": {"force_tts": True, "disable_sfx": True},
            },
        )
        self.assertEqual(
            final_state.get("video_assembly_render_report_structured", {}).get("status"),
            "complete",
        )
        self.assertEqual(final_state.get("video_assembly_attempt"), 2)
        self.assertEqual(len(final_state.get("video_assembly_render_history", [])), 2)

    def test_real_workflow_agent_uses_executor_report_state(self) -> None:
        async def render_callback(state: dict[str, object], attempt: int) -> dict[str, object]:
            if attempt == 1:
                return {
                    "status": "failed",
                    "ready_to_publish": False,
                    "retryable": True,
                    "reason": "Narration missing.",
                    "issues": [{"code": "narration_missing"}],
                }
            return {
                "status": "complete",
                "ready_to_publish": True,
                "retryable": False,
                "reason": "Movie is ready.",
                "issues": [],
            }

        async def _run() -> dict[str, object]:
            session_service = InMemorySessionService()
            await session_service.create_session(
                app_name="storybook_video_assembly_director_test",
                user_id="session-123",
                session_id="session-123-workflow",
                state={"session_id": "session-123", "video_assembly_max_attempts": 2},
            )
            runner = Runner(
                agent=StorybookVideoAssemblyWorkflowAgent(
                    planner_agent=_StaticPlannerAgent(name="planner", description="planner"),
                    executor_agent=StorybookVideoAssemblyRenderExecutorAgent(render_callback=render_callback),
                    auditor_agent=_StaticAuditorAgent(name="auditor", description="auditor"),
                    finalizer_agent=_StaticFinalizerAgent(name="finalizer", description="finalizer"),
                    render_callback=render_callback,
                    max_render_attempts=2,
                ),
                app_name="storybook_video_assembly_director_test",
                session_service=session_service,
            )
            kickoff = genai_types.Content(
                role="user",
                parts=[genai_types.Part(text="Run the final storybook video assembly workflow now.")],
            )
            async for _event in runner.run_async(
                user_id="session-123",
                session_id="session-123-workflow",
                new_message=kickoff,
            ):
                pass
            session = await session_service.get_session(
                app_name="storybook_video_assembly_director_test",
                user_id="session-123",
                session_id="session-123-workflow",
            )
            return dict(session.state or {})

        final_state = asyncio.run(_run())

        self.assertEqual(final_state["video_assembly_render_request_structured"]["attempt"], 2)
        self.assertEqual(final_state["video_assembly_render_report_structured"]["status"], "complete")
        self.assertEqual(final_state["video_assembly_decision_history"][-1]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
