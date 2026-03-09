from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools


class _FakeToolContext:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state


class StaleTurnGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._storybook_state_cache.clear()

    def tearDown(self) -> None:
        tools._storybook_state_cache.clear()

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


if __name__ == "__main__":
    unittest.main()
