from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend import ws_router


class LiveRecoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        ws_router._awaiting_greeting_sessions.clear()
        ws_router._opening_phase_sessions.clear()
        ws_router._ending_story_sessions.clear()

    def test_clean_close_resumes_during_opening_phase(self) -> None:
        ws_router._opening_phase_sessions.add("session-a")
        self.assertTrue(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {},
                meaningful_pending_turn=False,
            )
        )

    def test_clean_close_does_not_resume_idle_opening_phase_after_greeting(self) -> None:
        ws_router._opening_phase_sessions.add("session-a")
        self.assertFalse(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {
                    "response_turn_number": 2,
                    "story_summary": "Hi there, friend! What's your name?",
                },
                meaningful_pending_turn=False,
            )
        )

    def test_clean_close_resumes_with_pending_response_state(self) -> None:
        self.assertTrue(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {"pending_response": True},
                meaningful_pending_turn=False,
            )
        )

    def test_clean_close_does_not_resume_idle_session(self) -> None:
        self.assertFalse(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {},
                meaningful_pending_turn=False,
            )
        )

    def test_scene_refresh_detects_explicit_visual_request(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_refresh(
                "Can you show me the Christmas tree?"
            )
        )

    def test_scene_refresh_detects_same_room_focus_shift(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_refresh(
                "Can I get closer to Santa's chair?"
            )
        )

    def test_fallback_scene_prompt_prefers_child_scene_request(self) -> None:
        prompt = ws_router._fallback_scene_prompt(
            "Let's tiptoe through the workshop together.",
            "Can we go to the Christmas tree?",
            {
                "story_summary": "We are inside Santa's workshop with warm lights and toys.",
                "current_scene_description": "Inside Santa's workshop by the fireplace.",
            },
        )

        self.assertIn("Christmas tree", prompt)
        self.assertNotIn("tiptoe through the workshop together", prompt)

    def test_read_page_request_is_treated_as_scene_chat(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_chat(
                "Can you read the page to me right now? What's that say?"
            )
        )

    def test_read_page_request_does_not_trigger_fallback_scene(self) -> None:
        self.assertFalse(
            ws_router._should_trigger_fallback_scene(
                assistant_text="This page has busy elves polishing toys by the warm fireplace.",
                child_text="Can you read the page to me right now? What's that say?",
                state={
                    "scene_asset_urls": ["https://example.com/page1.jpg"],
                    "current_scene_description": "Inside Santa's workshop by the fireplace.",
                },
            )
        )

    def test_storybook_scene_state_payload_uses_public_branch_points(self) -> None:
        payload = ws_router._storybook_scene_state_payload(
            {
                "scene_branch_points": [
                    {
                        "scene_number": 1,
                        "request_id": "req-1",
                        "scene_description": "Bubble forest.",
                        "storybeat_text": "Bubbles drift through the trees.",
                        "image_url": "https://example.com/scene-1.jpg",
                        "state_snapshot": {"story_summary": "internal only"},
                    }
                ],
                "scene_asset_urls": ["https://example.com/scene-1.jpg"],
                "scene_descriptions": ["Bubble forest."],
                "scene_storybeat_texts": ["Bubbles drift through the trees."],
                "current_scene_description": "Bubble forest.",
                "current_scene_storybeat_text": "Bubbles drift through the trees.",
                "story_summary": "We are in a bubble forest.",
                "story_phase": "waiting_for_child",
            }
        )

        self.assertEqual(payload["scene_branch_points_public"][0]["scene_number"], 1)
        self.assertEqual(payload["scene_branch_points"][0]["image_url"], "https://example.com/scene-1.jpg")
        self.assertNotIn("state_snapshot", payload["scene_branch_points"][0])

    def test_prepare_branch_state_restores_active_request_id(self) -> None:
        restored = ws_router._prepare_branch_state(
            {
                "scene_number": 2,
                "request_id": "req-2",
                "state_snapshot": {
                    "story_summary": "Scene two summary.",
                    "current_scene_description": "Secret bubble path.",
                    "current_scene_storybeat_text": "The bubble guides us onward.",
                },
            },
            [
                {
                    "scene_number": 1,
                    "request_id": "req-1",
                    "scene_description": "Bubble forest.",
                    "storybeat_text": "Bubbles drift through the trees.",
                    "image_url": "https://example.com/scene-1.jpg",
                },
                {
                    "scene_number": 2,
                    "request_id": "req-2",
                    "scene_description": "Secret bubble path.",
                    "storybeat_text": "The bubble guides us onward.",
                    "image_url": "https://example.com/scene-2.jpg",
                },
            ],
        )

        self.assertEqual(restored["active_scene_request_id"], "req-2")
        self.assertEqual(restored["current_scene_description"], "Secret bubble path.")
        self.assertEqual(restored["scene_asset_urls"][-1], "https://example.com/scene-2.jpg")

    def test_apply_shared_toy_story_state_promotes_toy_to_recurring_sidekick(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)

        ws_router._apply_shared_toy_story_state(
            state,
            summary_text="A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
            toy_thumb=("thumb-b64", "image/jpeg"),
        )

        self.assertTrue(state["camera_received"])
        self.assertEqual(state["camera_stage"], "done")
        self.assertEqual(state["toy_reference_thumbnail_b64"], "thumb-b64")
        self.assertEqual(state["toy_reference_thumbnail_mime"], "image/jpeg")
        self.assertEqual(
            state["toy_reference_visual_summary"],
            "A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
        )
        self.assertEqual(
            state["sidekick_description"],
            "A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
        )
        self.assertIn(
            {
                "character_name": "shared toy companion",
                "fact": "A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
            },
            state["character_facts_list"],
        )
        self.assertIn("shared toy companion", state["character_facts"])


if __name__ == "__main__":
    unittest.main()
