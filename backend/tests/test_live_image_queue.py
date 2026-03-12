from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools
from backend import ws_router


class LiveImageQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._session_pending.clear()

    def tearDown(self) -> None:
        tools._session_pending.clear()

    def test_detects_newer_pending_scene_request(self) -> None:
        tools._session_pending["session-a"] = tools.VisualArgs(
            description="newest castle",
            request_id="new-request",
        )
        self.assertTrue(tools._queued_newer_scene_request("session-a", "old-request"))

    def test_same_pending_request_is_not_treated_as_newer(self) -> None:
        tools._session_pending["session-a"] = tools.VisualArgs(
            description="same castle",
            request_id="same-request",
        )
        self.assertFalse(tools._queued_newer_scene_request("session-a", "same-request"))

    def test_missing_current_request_id_treats_pending_as_newer(self) -> None:
        tools._session_pending["session-a"] = tools.VisualArgs(
            description="queued scene",
            request_id="queued-request",
        )
        self.assertTrue(tools._queued_newer_scene_request("session-a", ""))

    def test_queue_latest_scene_follow_up_request_keeps_latest_child_wish(self) -> None:
        state = {}

        ws_router._queue_latest_scene_follow_up_request(state, "Let's go to the crystal bridge next.")
        first_timestamp = int(state.get("queued_scene_child_utterance_at_epoch_ms", 0) or 0)

        ws_router._queue_latest_scene_follow_up_request(state, "No, go behind the waterfall instead.")

        self.assertEqual(state["queued_scene_child_utterance"], "No, go behind the waterfall instead.")
        self.assertGreaterEqual(int(state.get("queued_scene_child_utterance_at_epoch_ms", 0) or 0), first_timestamp)
        self.assertEqual(state["partial_child_utterance"], "No, go behind the waterfall instead.")
        self.assertTrue(state["partial_child_utterance_finished"])

    def test_arm_queued_scene_follow_up_after_render_promotes_latest_request(self) -> None:
        state = {
            "queued_scene_child_utterance": "Let's go inside the glowing tunnel.",
            "queued_scene_child_utterance_at_epoch_ms": 123,
            "pending_response": False,
            "scene_tool_turn_open": False,
            "assembly_status": "",
        }

        queued = ws_router._arm_queued_scene_follow_up_after_render(state)

        self.assertEqual(queued, "Let's go inside the glowing tunnel.")
        self.assertEqual(state["queued_scene_child_utterance"], "")
        self.assertEqual(state["queued_scene_child_utterance_at_epoch_ms"], 0)
        self.assertTrue(state["pending_response"])
        self.assertFalse(state["pending_response_interrupted"])
        self.assertTrue(state["scene_tool_turn_open"])
        self.assertEqual(state["last_child_utterance"], "Let's go inside the glowing tunnel.")
        self.assertTrue(str(state.get("pending_response_token", "")).strip())

    def test_arm_queued_scene_follow_up_after_render_skips_when_response_already_pending(self) -> None:
        state = {
            "queued_scene_child_utterance": "Let's climb up to the dragon.",
            "queued_scene_child_utterance_at_epoch_ms": 456,
            "pending_response": True,
            "scene_tool_turn_open": False,
            "assembly_status": "",
        }

        queued = ws_router._arm_queued_scene_follow_up_after_render(state)

        self.assertEqual(queued, "")
        self.assertEqual(state["queued_scene_child_utterance"], "Let's climb up to the dragon.")


if __name__ == "__main__":
    unittest.main()
