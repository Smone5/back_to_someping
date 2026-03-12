from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.tools import _storybook_state_cache, sync_room_lights


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id


class _FakeToolContext:
    def __init__(self, session_id: str, state: dict[str, object]) -> None:
        self.session = _FakeSession(session_id)
        self.state = state


class StorybookLightingTests(unittest.TestCase):
    def tearDown(self) -> None:
        _storybook_state_cache.pop("session-lighting", None)

    def test_sync_room_lights_records_scene_lighting_even_without_home_assistant(self) -> None:
        tool_context = _FakeToolContext(
            "session-lighting",
            {
                "current_scene_description": "A moonlit castle hallway sparkles with bubbles.",
                "story_pages": [
                    {
                        "scene_number": 1,
                        "request_id": "scene-1",
                        "scene_description": "A moonlit castle hallway sparkles with bubbles.",
                    }
                ],
            },
        )

        with mock.patch("agent.tools._update_storybook_firestore"):
            message = asyncio.run(
                sync_room_lights(
                    "#6FA8FF",
                    "A moonlit castle hallway sparkles with bubbles.",
                    tool_context,
                )
            )

        self.assertIn("not configured", message.lower())
        cues = tool_context.state.get("scene_lighting_cues")
        self.assertIsInstance(cues, list)
        self.assertEqual(len(cues), 1)
        cue = cues[0]
        self.assertEqual(cue["scene_number"], 1)
        self.assertEqual(cue["request_id"], "scene-1")
        self.assertEqual(cue["hex_color"], "#6FA8FF")
        self.assertEqual(cue["rgb_color"], [111, 168, 255])


if __name__ == "__main__":
    unittest.main()
