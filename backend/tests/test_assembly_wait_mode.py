from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend import ws_router


class AssemblyWaitModeTests(unittest.TestCase):
    def test_mark_pending_assembly_wait_response_keeps_story_state_untouched(self) -> None:
        state = {
            "pending_story_hint": "Let's go through the secret door.",
            "last_child_utterance": "Can we go behind the fireplace?",
            "continuity_world_state_text": "Current place: Secret room | Pending child wish: secret door",
            "assembly_recent_activities": ["favorite_part"],
            "assembly_wait_prompt_count": 2,
        }

        ws_router._mark_pending_assembly_wait_response(
            state,
            child_utterance="Let's go to the dragon cave next.",
            activity_key="tiny_joke",
        )

        self.assertTrue(state["pending_response"])
        self.assertFalse(state["pending_response_interrupted"])
        self.assertTrue(state["scene_tool_turn_open"])
        self.assertEqual(state["assembly_wait_last_child_utterance"], "Let's go to the dragon cave next.")
        self.assertEqual(state["pending_story_hint"], "Let's go through the secret door.")
        self.assertEqual(state["last_child_utterance"], "Can we go behind the fireplace?")
        self.assertEqual(
            state["continuity_world_state_text"],
            "Current place: Secret room | Pending child wish: secret door",
        )
        self.assertEqual(state["assembly_wait_prompt_count"], 3)
        self.assertEqual(state["assembly_recent_activities"], ["favorite_part", "tiny_joke"])
        self.assertTrue(str(state.get("pending_response_token", "")).strip())

    def test_assembly_wait_prompt_explicitly_forbids_plot_progress(self) -> None:
        prompt = ws_router._assembly_wait_prompt(
            {
                "current_scene_storybeat_text": "Santa smiles in the bubble room.",
                "story_title": "Bubble Santa",
                "child_name": "Aaron",
            },
            child_utterance="Let's go through the tunnel next.",
            activity="favorite_part",
        )

        self.assertIn("The story is locked and finished.", prompt)
        self.assertIn("favorite-story chatter only", prompt)
        self.assertIn("Do NOT continue the plot.", prompt)
        self.assertIn("Do NOT narrate any new action, movement, discovery, location change, or next page.", prompt)
        self.assertIn("Do NOT use phrases like 'then we', 'next we', 'let's go', 'we find', 'we open', or 'now we'.", prompt)
        self.assertIn("Do NOT ask what happens next in the story or adventure.", prompt)
        self.assertIn("Do NOT ask where to go next, what to do next, what happens next, what the next page is", prompt)
        self.assertIn("Do NOT ask the child to show you anything", prompt)
        self.assertIn("Keep the interaction audio-first, calm, quiet-friendly, and bedtime-safe.", prompt)
        self.assertIn("Do NOT say the movie is ready, done, finished, starting now, opening now, or ready to watch.", prompt)
        self.assertIn("Do NOT say phrases like 'enjoy the movie', 'enjoy the show', 'the movie is done'", prompt)
        self.assertIn("you may use the room-light tool once", prompt)

    def test_assembly_activity_prompt_stays_in_waiting_room_mode(self) -> None:
        prompt = ws_router._assembly_activity_prompt(
            {"current_scene_storybeat_text": "A dragon naps under the moon."},
            activity="counting_game",
            label="Count to 3",
        )

        self.assertIn("waiting room", prompt.lower())
        self.assertIn("The story is locked and finished.", prompt)
        self.assertIn("Do NOT continue the plot.", prompt)
        self.assertIn("Do NOT narrate any new action, movement, discovery, location change, or next page.", prompt)
        self.assertIn("Do NOT ask what happens next in the story or adventure.", prompt)
        self.assertIn("Do NOT ask the child to show you anything", prompt)
        self.assertIn("Do NOT say the movie is ready, done, finished, starting now, opening now, or ready to watch.", prompt)

    def test_assembly_recent_activity_aliases_normalize_old_movement_keys(self) -> None:
        state = {
            "assembly_recent_activities": ["sparkle_wiggle", "helper_pose", "tiny_joke"],
        }

        self.assertEqual(
            ws_router._assembly_recent_activity_keys(state),
            ["soft_echo", "cozy_breath", "tiny_joke"],
        )

    def test_assembly_intro_prefers_quiet_audio_first_activity(self) -> None:
        activity = ws_router._choose_assembly_wait_activity({}, intro=True)

        self.assertIn(activity, {"tiny_joke", "favorite_sound", "counting_game"})

    def test_assembly_wait_light_request_chooses_light_color_activity(self) -> None:
        activity = ws_router._choose_assembly_wait_activity(
            {},
            child_utterance="Can you turn the lights blue while we wait?",
        )

        self.assertEqual(activity, "light_color")


if __name__ == "__main__":
    unittest.main()
