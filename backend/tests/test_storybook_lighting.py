from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.tools import _storybook_state_cache, _sync_room_lights_impl, sync_room_lights
from shared.storybook_lighting import heuristic_storybook_lighting_command


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
        story_pages = tool_context.state.get("story_pages")
        self.assertIsInstance(story_pages, list)
        self.assertEqual(story_pages[0]["hex_color"], "#6FA8FF")
        self.assertEqual(story_pages[0]["rgb_color"], [111, 168, 255])
        self.assertEqual(story_pages[0]["cue_source"], "tool_sync")

    def test_same_color_updates_new_page_metadata_even_when_live_command_is_skipped(self) -> None:
        tool_context = _FakeToolContext(
            "session-lighting",
            {
                "active_scene_request_id": "scene-1",
                "story_pages": [
                    {
                        "scene_number": 1,
                        "request_id": "scene-1",
                        "scene_description": "A moonlit castle hallway sparkles with bubbles.",
                    },
                    {
                        "scene_number": 2,
                        "request_id": "scene-2",
                        "scene_description": "A second moonlit castle hallway glows softly.",
                    },
                ],
            },
        )

        http_client = mock.AsyncMock()
        response = mock.Mock()
        response.raise_for_status.return_value = None
        http_client.post.return_value = response
        async_client = mock.AsyncMock()
        async_client.__aenter__.return_value = http_client
        async_client.__aexit__.return_value = False

        with mock.patch("agent.tools._update_storybook_firestore"):
            with mock.patch(
                "agent.tools.get_session_iot_config",
                return_value={
                    "ha_url": "https://example.ui.nabu.casa",
                    "ha_token": "token-123",
                    "ha_entity": "light.story_room",
                },
            ):
                with mock.patch("agent.tools.httpx.AsyncClient", return_value=async_client):
                    first_message = asyncio.run(
                        _sync_room_lights_impl(
                            hex_color="#6FA8FF",
                            scene_description="A moonlit castle hallway sparkles with bubbles.",
                            tool_context=tool_context,
                            enforce_cooldown=False,
                            remember_last_color=True,
                            brightness=154,
                            transition=1.1,
                            cue_source="heuristic_scene",
                        )
                    )

                    tool_context.state["active_scene_request_id"] = "scene-2"
                    second_message = asyncio.run(
                        _sync_room_lights_impl(
                            hex_color="#6FA8FF",
                            scene_description="A second moonlit castle hallway glows softly.",
                            tool_context=tool_context,
                            enforce_cooldown=False,
                            remember_last_color=True,
                            brightness=154,
                            transition=1.1,
                            cue_source="heuristic_scene",
                        )
                    )

        self.assertIn("synced", first_message.lower())
        self.assertIn("already match", second_message.lower())
        story_pages = tool_context.state.get("story_pages")
        self.assertIsInstance(story_pages, list)
        self.assertEqual(story_pages[1]["request_id"], "scene-2")
        self.assertEqual(story_pages[1]["hex_color"], "#6FA8FF")
        self.assertEqual(story_pages[1]["cue_source"], "heuristic_scene")

    def test_candy_scene_maps_to_bright_warm_palette(self) -> None:
        cue = heuristic_storybook_lighting_command(
            "The candy land is filled with lollipop trees, a flowing chocolate river, and houses made of yummy gummy bears."
        )

        self.assertEqual(cue["hex_color"], "#FFC78A")
        self.assertEqual(cue["rgb_color"], [255, 199, 138])
        self.assertEqual(cue["cue_source"], "heuristic_scene")


if __name__ == "__main__":
    unittest.main()
