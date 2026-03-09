from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools


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


if __name__ == "__main__":
    unittest.main()
