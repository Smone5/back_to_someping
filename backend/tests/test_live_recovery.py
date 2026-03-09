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


if __name__ == "__main__":
    unittest.main()
