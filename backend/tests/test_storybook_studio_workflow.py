from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.storybook_studio_workflow import (  # noqa: E402
    _merge_narration_review_reports,
    build_storybook_studio_plan_from_workflow_state,
)


class StorybookStudioWorkflowTests(unittest.TestCase):
    def test_merge_narration_reviews_requests_revision_when_backward_pass_flags_ending(self) -> None:
        merged = _merge_narration_review_reports(
            [
                {
                    "direction": "forward",
                    "status": "approved",
                    "reason": "The opening and middle flow well.",
                    "narration_feedback": [],
                    "kid_delight_notes": ["Lion-O stays present in the adventure."],
                },
                {
                    "direction": "backward",
                    "status": "revise_narration",
                    "reason": "The ending feels abrupt and does not resolve the castle adventure.",
                    "narration_feedback": [
                        "Let the final line feel like a warm ending instead of a label.",
                    ],
                    "kid_delight_notes": ["Santa and the candy room should still feel magical."],
                },
            ]
        )

        self.assertEqual(merged["status"], "revise_narration")
        self.assertIn("ending feels abrupt", merged["reason"].lower())
        self.assertIn(
            "Let the final line feel like a warm ending instead of a label.",
            merged["narration_feedback"],
        )
        self.assertEqual(merged["directions_reviewed"], ["forward", "backward"])

    def test_merge_narration_reviews_dedupes_feedback(self) -> None:
        merged = _merge_narration_review_reports(
            [
                {
                    "direction": "forward",
                    "status": "revise_narration",
                    "reason": "The story reads like captions.",
                    "narration_feedback": [
                        "Open with a true story sentence, not a picture label.",
                        "Open with a true story sentence, not a picture label.",
                    ],
                },
                {
                    "direction": "backward",
                    "status": "revise_narration",
                    "reason": "The ending is too generic.",
                    "narration_feedback": [
                        "End with a satisfying story beat.",
                        "Open with a true story sentence, not a picture label.",
                    ],
                },
            ]
        )

        self.assertEqual(
            merged["narration_feedback"],
            [
                "Open with a true story sentence, not a picture label.",
                "End with a satisfying story beat.",
            ],
        )

    def test_plan_builder_preserves_narration_review_summary(self) -> None:
        plan = build_storybook_studio_plan_from_workflow_state(
            {
                "studio_narration_plan": {
                    "narration_style": "one short sentence",
                    "narration_lines": ["Aaron tiptoed into the licorice castle."],
                },
                "studio_narration_review_summary": {
                    "status": "approved",
                    "reason": "The story flows from beginning to end.",
                },
                "studio_quality_report": {
                    "status": "approved",
                    "reason": "Ready to render.",
                },
            },
            scene_count=1,
            fallback_narration_lines=["fallback"],
        )

        self.assertEqual(plan["narration_review"]["status"], "approved")
        self.assertIn("beginning to end", plan["narration_review"]["reason"].lower())

    def test_plan_builder_does_not_mark_studio_approved_when_directional_review_failed(self) -> None:
        plan = build_storybook_studio_plan_from_workflow_state(
            {
                "studio_narration_plan": {
                    "narration_style": "one short sentence",
                    "narration_lines": ["Aaron reached Santa's secret room."],
                },
                "studio_narration_review_summary": {
                    "status": "revise_narration",
                    "reason": "The ending still reads like a caption.",
                },
                "studio_quality_report": {
                    "status": "approved",
                    "reason": "Audio mix is ready.",
                },
            },
            scene_count=1,
            fallback_narration_lines=["fallback"],
        )

        self.assertEqual(plan["status"], "revise_narration")
        self.assertIn("ending still reads like a caption", plan["reason"].lower())


if __name__ == "__main__":
    unittest.main()
