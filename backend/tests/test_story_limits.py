from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools
from backend import ws_router


class _FakeToolContext:
    def __init__(self, state: dict[str, object], session_id: str = "session-limit") -> None:
        self.state = state
        self.session = SimpleNamespace(id=session_id)


class StoryLimitTests(unittest.TestCase):
    def test_movie_phrase_requires_explicit_end_request(self) -> None:
        intent, payload = ws_router._detect_voice_ui_intent(
            "make a movie about dragons",
            {"story_started": True},
        )

        self.assertIsNone(intent)
        self.assertEqual(payload, {})

    def test_explicit_make_movie_request_still_ends_story(self) -> None:
        intent, payload = ws_router._detect_voice_ui_intent(
            "Let's make the movie now.",
            {"story_started": True},
        )

        self.assertEqual(intent, "end_story")
        self.assertEqual(payload, {})

    def test_session_defaults_sync_real_rendered_page_count(self) -> None:
        state = {
            "max_story_turns": 20,
            "story_pages": [
                {"scene_number": 1, "image_url": "https://example.com/page-1.jpg"},
                {"scene_number": 2, "image_url": "https://example.com/page-2.jpg"},
                {"scene_number": 3, "gcs_uri": "gs://bucket/page-3.jpg"},
                {"scene_number": 4, "image_url": ""},
                {"scene_number": 5, "image_url": "data:image/svg+xml;base64,placeholder"},
            ],
        }

        ws_router._ensure_session_state_defaults(state)

        self.assertEqual(state["story_page_count"], 3)
        self.assertEqual(state["story_pages_remaining"], 17)
        self.assertFalse(state["story_page_limit_reached"])

    def test_story_end_progress_uses_rendered_pages_not_turn_counter(self) -> None:
        tool_context = _FakeToolContext(
            {
                "max_story_turns": 20,
                "turn_number": 20,
                "story_turn_limit_reached": False,
                "story_pages": [
                    {"scene_number": 1, "image_url": "https://example.com/page-1.jpg"},
                    {"scene_number": 2, "image_url": "https://example.com/page-2.jpg"},
                    {"scene_number": 3, "image_url": "https://example.com/page-3.jpg"},
                    {"scene_number": 4, "image_url": "https://example.com/page-4.jpg"},
                ],
            }
        )

        allowed_to_finish, current_page_count, max_turns, turns_remaining = tools._story_end_progress(tool_context)

        self.assertFalse(allowed_to_finish)
        self.assertEqual(current_page_count, 4)
        self.assertEqual(max_turns, 20)
        self.assertEqual(turns_remaining, 16)


if __name__ == "__main__":
    unittest.main()
