from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools


class _FakeToolContext:
    def __init__(self, state: dict[str, object], session_id: str = "session-a") -> None:
        self.state = state
        self.session = SimpleNamespace(id=session_id)


class _FakeAdkState:
    def __init__(self, state: dict[str, object]) -> None:
        self._value = dict(state)

    def get(self, key: str, default: object = None) -> object:
        return self._value.get(key, default)

    def __getitem__(self, key: object) -> object:
        return self._value[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._value[key] = value


class StaleTurnGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._firestore_patcher = patch.object(tools, "_load_storybook_firestore_state", return_value={})
        self._firestore_patcher.start()
        tools._storybook_state_cache.clear()
        tools._session_generating.clear()
        tools._session_pending.clear()
        tools._session_cancel_current.clear()
        tools._session_last_gen_text.clear()

    def tearDown(self) -> None:
        self._firestore_patcher.stop()
        tools._storybook_state_cache.clear()
        tools._session_generating.clear()
        tools._session_pending.clear()
        tools._session_cancel_current.clear()
        tools._session_last_gen_text.clear()

    def test_accepts_live_tool_call_for_newer_pending_turn_token(self) -> None:
        tools.cache_storybook_state(
            "session-a",
            {
                "scene_tool_turn_open": False,
                "pending_response": False,
                "pending_response_token": "old-token",
                "last_child_utterance": "Look at the castle",
            },
        )
        tool_context = _FakeToolContext(
            {
                "scene_tool_turn_open": True,
                "pending_response": True,
                "pending_response_token": "new-token",
                "last_child_utterance": "Look at the Christmas tree",
            }
        )
        self.assertFalse(tools._stale_turn_tool_call("session-a", tool_context))

    def test_rejects_live_tool_call_for_same_closed_turn_token(self) -> None:
        tools.cache_storybook_state(
            "session-a",
            {
                "scene_tool_turn_open": False,
                "pending_response": False,
                "pending_response_token": "same-token",
                "last_child_utterance": "Look at the castle",
            },
        )
        tool_context = _FakeToolContext(
            {
                "scene_tool_turn_open": True,
                "pending_response": True,
                "pending_response_token": "same-token",
                "last_child_utterance": "Look at the castle",
            }
        )
        self.assertTrue(tools._stale_turn_tool_call("session-a", tool_context))

    def test_accepts_live_tool_call_when_no_persisted_state_exists(self) -> None:
        tool_context = _FakeToolContext(
            {
                "scene_tool_turn_open": True,
                "pending_response": True,
                "pending_response_token": "fresh-token",
                "last_child_utterance": "Look at the Christmas tree",
            }
        )
        self.assertFalse(tools._stale_turn_tool_call("session-a", tool_context))

    def test_generate_scene_visuals_accepts_adk_state_without_dict_cast_failure(self) -> None:
        tool_context = _FakeToolContext(
            _FakeAdkState(
                {
                    "session_id": "session-a",
                    "child_name": "Aaron",
                    "story_tone": "gentle_spooky",
                    "scene_tool_turn_open": True,
                    "pending_response": True,
                    "pending_response_token": "fresh-token",
                }
            )
        )
        scheduled: list[str] = []

        def _schedule(coro):
            scheduled.append(type(coro).__name__)
            coro.close()

        with (
            patch.object(tools, "_stale_turn_tool_call", return_value=False),
            patch.object(tools, "_continuity_anchor_text", return_value=""),
            patch.object(
                tools,
                "validate_live_scene_request",
                return_value=SimpleNamespace(
                    resolved_description="A sunny adventure path with smiling trees.",
                    location_label="sparkly path",
                    prompt_suffix="",
                    issues=[],
                ),
            ),
            patch.object(
                tools,
                "_build_visual_continuity_plan",
                return_value={"active_character_keys": []},
            ),
            patch.object(tools, "_character_bible_entries_for_keys", return_value=[]),
            patch.object(
                tools,
                "should_render_new_scene_page",
                return_value=SimpleNamespace(should_render=True, reason="new_scene"),
            ),
            patch.object(tools, "_resolve_image_preferences", return_value=("16:9", "512px", "test-model")),
            patch.object(tools, "_resolve_delivery_preferences", return_value=("jpeg", 72, None)),
            patch.object(tools, "publish_session_event"),
            patch.object(tools, "schedule_background_task", side_effect=_schedule),
        ):
            result = asyncio.run(
                tools.generate_scene_visuals(
                    "A sunny adventure path with smiling trees.",
                    tool_context=tool_context,
                )
            )

        self.assertIn("Drawing started.", result)
        self.assertEqual(len(scheduled), 1)
        self.assertTrue(str(tool_context.state.get("illustration_style", "")).strip())
        self.assertTrue(str(tool_context.state.get("active_scene_request_id", "")).strip())
        cached_state = tools.load_storybook_resume_state("session-a")
        self.assertEqual(
            cached_state.get("current_scene_base_description"),
            "A sunny adventure path with smiling trees.",
        )


if __name__ == "__main__":
    unittest.main()
