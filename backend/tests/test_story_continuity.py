from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.story_continuity import (
    ensure_story_continuity_state,
    record_continuity_scene,
    should_render_new_scene_page,
    update_continuity_from_child_utterance,
    validate_live_scene_request,
)


class StoryContinuityTests(unittest.TestCase):
    def test_requested_location_wins_over_generic_scene_drift(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        update_continuity_from_child_utterance(
            state,
            "Can we go to Santa's workshop and see the elves?",
        )

        result = validate_live_scene_request(
            state,
            "A magical castle made of shimmering ice and snow glows softly.",
        )

        self.assertIn("Santa's workshop", result.resolved_description)
        self.assertIn("requested_location_mismatch", result.issues)
        self.assertIn("elves", result.prompt_suffix.lower())

    def test_relative_transition_keeps_current_place(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="A giant colorful bouncy castle sits on soft grass with bright flags.",
            storybeat_text="The bouncy castle waits for fun.",
            scene_number=1,
        )
        current_label = str(state["continuity_world_state"]["current_location_label"])
        self.assertTrue(current_label)

        update_continuity_from_child_utterance(state, "Can we go inside?")
        result = validate_live_scene_request(
            state,
            "A bright squishy throne room with playful flags.",
        )

        self.assertIn(current_label.lower(), result.resolved_description.lower())
        self.assertIn("missing_location_anchor", result.issues)

    def test_record_scene_updates_registry_and_world_state(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        update_continuity_from_child_utterance(
            state,
            "Can we go to Santa's workshop and see the elves?",
        )

        record_continuity_scene(
            state,
            description="Inside Santa's workshop, little elves paint toys beside a cozy fireplace.",
            storybeat_text="Warm elves work happily in Santa's workshop.",
            scene_number=1,
        )

        world = state["continuity_world_state"]
        self.assertEqual(world["current_location_label"], "Santa's workshop")
        self.assertTrue(world["active_character_keys"])
        self.assertTrue(world["active_prop_keys"])
        self.assertIn("Santa's workshop", state["continuity_registry_text"])
        self.assertIn("Current place: Santa's workshop", state["continuity_world_state_text"])

    def test_focus_request_stays_inside_current_room(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "Inside Santa's workshop, a tall Christmas tree stands by warm worktables, "
                "wrapped toys, and glowing lights."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a tall Christmas tree sparkles beside toy worktables.",
            storybeat_text="The indoor tree glows warmly in Santa's workshop.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we see the star at the top?")
        result = validate_live_scene_request(
            state,
            "A glowing star on top of a Christmas tree outside under the snowy night sky.",
        )

        self.assertIn("inside santa's workshop", result.resolved_description.lower())
        self.assertIn("Stay in this exact visible space", result.prompt_suffix)
        self.assertIn("interior_exterior_drift", result.issues)

    def test_bootstrap_from_story_pages_populates_history_without_recursion(self) -> None:
        state: dict[str, object] = {
            "story_pages": [
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "scene_description": "Inside Santa's workshop, elves build toys by a fireplace.",
                    "storybeat_text": "The elves work happily in Santa's workshop.",
                    "image_url": "https://example.com/scene-1.jpg",
                }
            ]
        }

        ensure_story_continuity_state(state)

        history = state["continuity_scene_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["request_id"], "scene-1")
        self.assertEqual(
            state["continuity_world_state"]["current_location_label"],
            "Santa's workshop",
        )

    def test_same_location_minor_beat_stays_on_current_page(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a wooden toy train sits near wrapped gifts and warm worktables.",
            storybeat_text="The toy train waits in Santa's workshop.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we hug the teddy bear?")
        result = validate_live_scene_request(
            state,
            "A child's hand gently hugging a fluffy teddy bear beside the toy train in Santa's workshop.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertFalse(decision.should_render)
        self.assertEqual(decision.reason, "same_location_minor_beat")

    def test_explicit_visual_request_allows_same_location_redraw(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a tall Christmas tree glows beside toy worktables.",
            storybeat_text="The tree sparkles warmly in Santa's workshop.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can you show me the star on the tree with an image?")
        result = validate_live_scene_request(
            state,
            "A close-up of the glowing star at the top of the Christmas tree in Santa's workshop.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "explicit_visual_request")

    def test_structural_transition_allows_new_page_in_same_place(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Outside Santa's workshop, snow glows under the moonlight.",
            storybeat_text="The workshop glimmers in the snow.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we go inside the workshop?")
        result = validate_live_scene_request(
            state,
            "A cozy room full of toy benches and elves inside Santa's workshop.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "structural_transition")

    def test_young_child_mode_keeps_same_location_reveal_on_current_page(self) -> None:
        state: dict[str, object] = {"child_age_band": "4-5"}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a toy train sits by glowing shelves and wrapped gifts.",
            storybeat_text="The toy train waits by the workshop shelves.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "The teddy bear turns sparkly!")
        result = validate_live_scene_request(
            state,
            "A teddy bear in Santa's workshop transforms with swirls of sparkly light beside the toy train.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertFalse(decision.should_render)
        self.assertEqual(decision.reason, "same_location_minor_beat_young_child")


if __name__ == "__main__":
    unittest.main()
