from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared import meta_learning


class MetaLearningTests(unittest.TestCase):
    def test_normalize_issue_tags_recognizes_continuity_specific_patterns(self) -> None:
        tags = meta_learning._normalize_issue_tags(
            [
                "The image teleports to an unrelated forest.",
                "The sidekick is missing from the next scene.",
                "The dragon has the wrong color palette.",
                "The page is outside instead of inside the castle.",
                "missing_character_carryover",
            ]
        )

        self.assertIn("location_teleport", tags)
        self.assertIn("missing_sidekick", tags)
        self.assertIn("character_palette_drift", tags)
        self.assertIn("interior_exterior_drift", tags)
        self.assertIn("missing_character_carryover", tags)

    def test_record_prompt_feedback_merges_explicit_issue_tags(self) -> None:
        captured: list[dict[str, object]] = []

        with patch.object(meta_learning, "_append_local_feedback", side_effect=lambda record: captured.append(record)):
            with patch.object(meta_learning, "_firestore_client", return_value=None):
                meta_learning.record_prompt_feedback(
                    "interactive_scene_visual",
                    outcome="audit_repair_requested",
                    issues=["The image teleports to an unrelated forest."],
                    issue_tags=["location_teleport", "character_palette_drift"],
                    prompt_text="Inside the purple castle hall, the friendly dragon sleeps beside a secret door.",
                    session_id="session-123",
                    force_log=True,
                )

        self.assertEqual(len(captured), 1)
        self.assertEqual(
            set(captured[0]["issue_tags"]),
            {"location_teleport", "character_palette_drift"},
        )

    def test_get_scoped_principles_reserves_room_for_learned_rules(self) -> None:
        seed_principles = [
            {
                "id": f"seed-{idx}",
                "scope": "interactive_scene_visual",
                "category": f"seed_category_{idx}",
                "status": "active",
                "priority": 120 - idx,
                "rule_text": f"Seed rule {idx}",
                "source": "seed",
            }
            for idx in range(5)
        ]
        learned_principles = [
            {
                "id": "interactive_scene_visual--location_teleport",
                "scope": "interactive_scene_visual",
                "category": "location_teleport",
                "status": "active",
                "priority": 85,
                "rule_text": "Keep transitions spatially adjacent and do not jump to unrelated places.",
                "source": "feedback_rollup",
                "evidence_count": 6,
            }
        ]

        with patch.object(meta_learning, "_load_seed_principles", return_value=seed_principles):
            with patch.object(meta_learning, "_load_firestore_principles", return_value=learned_principles):
                with patch.dict(os.environ, {"PROMPT_META_LEARNING_MAX_PRINCIPLES": "5"}, clear=False):
                    selected = meta_learning.get_scoped_principles("interactive_scene_visual")

        self.assertEqual(len(selected), 5)
        self.assertTrue(any(row.get("source") == "feedback_rollup" for row in selected))
        self.assertTrue(any(row.get("category") == "location_teleport" for row in selected))

    def test_build_scene_visual_audit_feedback_signal_preserves_structured_kinds(self) -> None:
        feedback = meta_learning.build_scene_visual_audit_feedback_signal(
            [
                {
                    "status": "repair",
                    "should_retry": True,
                    "repair_prompt_suffix": "Keep the scene in the same castle hall and preserve the dragon's purple colors.",
                    "issues": [
                        {
                            "kind": "location_teleport",
                            "issue": "The image jumps to an unrelated moonlit forest instead of the castle hall.",
                        },
                        {
                            "kind": "character_palette_drift",
                            "issue": "The friendly dragon turns green instead of keeping the same purple palette.",
                        },
                    ],
                    "notes": ["The sidekick should still be visible."],
                },
                {
                    "status": "pass",
                    "should_retry": False,
                    "issues": [],
                    "notes": [],
                },
            ],
            original_prompt_text="Inside the purple castle hall, the friendly dragon sleeps beside a secret door.",
            final_prompt_config={
                "continuity_plan": {
                    "target_location": "purple castle hall",
                    "transition_type": "door",
                    "active_character_labels": ["friendly dragon", "bubble sidekick"],
                    "required_prop_labels": ["secret door"],
                },
                "reference_images": [{"role": "previous_scene", "b64": "thumb", "mime": "image/jpeg"}],
                "image_model": "gemini-3.1-flash-image-preview",
                "image_size": "512px",
                "aspect_ratio": "16:9",
            },
        )

        self.assertIsNotNone(feedback)
        self.assertEqual(feedback["outcome"], "audit_repair_requested")
        self.assertIn("location_teleport", feedback["issue_tags"])
        self.assertIn("character_palette_drift", feedback["issue_tags"])
        self.assertEqual(feedback["metadata"]["source"], "live_scene_visual_audit")
        self.assertEqual(feedback["metadata"]["target_location"], "purple castle hall")
        self.assertEqual(feedback["metadata"]["reference_image_count"], 1)


if __name__ == "__main__":
    unittest.main()
