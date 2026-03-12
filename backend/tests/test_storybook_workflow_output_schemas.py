from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.storybook_assembly_workflow import (
    AssemblyDirectorDecision,
    AssemblyDirectorPlan,
    AssemblyDirectorSummary,
    _build_workflow_agent as build_storyboard_workflow_agent,
)
from shared.storybook_studio_workflow import (
    StudioAudioCuePlan,
    StudioNarrationPlan,
    StudioQualityReport,
    _build_workflow_agent as build_studio_workflow_agent,
)
from shared.storybook_page_read_aloud_workflow import (
    PageReadAloudPlan,
    PageReadAloudRequest,
    PageReadAloudResult,
)
from shared.storybook_video_assembly_workflow import (
    VideoAssemblyDecision,
    VideoAssemblyRenderReport,
    VideoAssemblyRenderRequest,
    VideoAssemblyPlan,
    VideoAssemblySummary,
    _build_workflow_agent as build_video_workflow_agent,
)
from agent.tools import PostMovieMetaReview
from backend.ffmpeg_worker.assemble import StoryboardReviewResponse
from backend.ffmpeg_worker.assemble import NarrationLineRewriteResponse


async def _fake_render_callback(_state: dict[str, object], _attempt: int) -> dict[str, object]:
    return {}


async def _fake_review_callback(_state: dict[str, object], _iteration: int) -> dict[str, object]:
    return {}


async def _fake_repair_callback(
    _review: dict[str, object],
    _decision: dict[str, object],
    _iteration: int,
) -> dict[str, object]:
    return {}


class StorybookWorkflowOutputSchemaTests(unittest.TestCase):
    def _assert_no_additional_properties(self, value) -> None:
        if isinstance(value, dict):
            self.assertNotIn("additionalProperties", value)
            for nested in value.values():
                self._assert_no_additional_properties(nested)
        elif isinstance(value, list):
            for nested in value:
                self._assert_no_additional_properties(nested)

    def test_video_workflow_agents_use_output_schemas(self) -> None:
        agent = build_video_workflow_agent(
            render_callback=_fake_render_callback,
            max_render_attempts=2,
        )

        self.assertIs(agent.planner_agent.output_schema, VideoAssemblyPlan)
        self.assertIs(agent.auditor_agent.output_schema, VideoAssemblyDecision)
        self.assertIs(agent.finalizer_agent.output_schema, VideoAssemblySummary)

    def test_storyboard_workflow_agents_use_output_schemas(self) -> None:
        agent = build_storyboard_workflow_agent(
            review_callback=_fake_review_callback,
            repair_callback=_fake_repair_callback,
            max_revision_passes=2,
            max_repairs_per_pass=2,
        )

        self.assertIs(agent.planner_agent.output_schema, AssemblyDirectorPlan)
        self.assertIs(agent.critic_agent.output_schema, AssemblyDirectorDecision)
        self.assertIs(agent.finalizer_agent.output_schema, AssemblyDirectorSummary)

    def test_studio_workflow_agents_use_output_schemas(self) -> None:
        agent = build_studio_workflow_agent(max_revision_rounds=1)

        self.assertIs(agent.narration_agent.output_schema, StudioNarrationPlan)
        self.assertIs(agent.audio_agent.output_schema, StudioAudioCuePlan)
        self.assertIs(agent.qa_agent.output_schema, StudioQualityReport)

    def test_all_model_output_schemas_avoid_additional_properties(self) -> None:
        schema_types = (
            VideoAssemblyPlan,
            VideoAssemblyDecision,
            VideoAssemblySummary,
            VideoAssemblyRenderRequest,
            VideoAssemblyRenderReport,
            AssemblyDirectorPlan,
            AssemblyDirectorDecision,
            AssemblyDirectorSummary,
            StudioNarrationPlan,
            StudioAudioCuePlan,
            StudioQualityReport,
            PageReadAloudRequest,
            PageReadAloudPlan,
            PageReadAloudResult,
            PostMovieMetaReview,
            NarrationLineRewriteResponse,
            StoryboardReviewResponse,
        )

        for schema_type in schema_types:
            with self.subTest(schema_type=schema_type.__name__):
                self._assert_no_additional_properties(schema_type.model_json_schema())


if __name__ == "__main__":
    unittest.main()
