from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.storybook_flow import derive_story_phase, theater_release_ready


class StorybookFlowTests(unittest.TestCase):
    def test_opening_phase_before_story_start(self) -> None:
        phase = derive_story_phase(
            {"story_started": False, "pending_response": False},
            opening_phase=True,
        )
        self.assertEqual(phase, "opening")

    def test_pending_scene_render_becomes_drawing_scene(self) -> None:
        phase = derive_story_phase(
            {"story_started": True, "scene_render_pending": True},
        )
        self.assertEqual(phase, "drawing_scene")

    def test_pending_response_becomes_chatting(self) -> None:
        phase = derive_story_phase(
            {"story_started": True, "pending_response": True},
        )
        self.assertEqual(phase, "chatting")

    def test_story_with_scene_waits_for_child(self) -> None:
        phase = derive_story_phase(
            {
                "story_started": True,
                "pending_response": False,
                "current_scene_description": "A cozy castle room.",
            },
        )
        self.assertEqual(phase, "waiting_for_child")

    def test_placeholder_scene_keeps_flow_in_chatting(self) -> None:
        phase = derive_story_phase(
            {
                "story_started": True,
                "pending_response": False,
                "current_scene_description": "No image yet — the story is just beginning!",
            },
        )
        self.assertEqual(phase, "chatting")

    def test_assembly_kind_remake_is_distinct_phase(self) -> None:
        phase = derive_story_phase(
            {
                "story_started": True,
                "assembly_status": "assembling",
                "assembly_kind": "remake",
            },
        )
        self.assertEqual(phase, "remake")

    def test_theater_release_requires_audio_when_narration_exists(self) -> None:
        ready = theater_release_ready(
            {
                "assembly_status": "complete",
                "final_video_url": "https://example.com/movie.mp4",
                "narration_lines": ["Once upon a time"],
                "audio_available": False,
                "final_has_audio_stream": False,
            }
        )
        self.assertFalse(ready)

    def test_theater_release_requires_audio_when_narration_policy_expects_it(self) -> None:
        ready = theater_release_ready(
            {
                "assembly_status": "complete",
                "final_video_url": "https://example.com/movie.mp4",
                "narration_lines": [],
                "audio_available": False,
                "final_has_audio_stream": False,
            }
        )
        self.assertFalse(ready)

    def test_theater_release_accepts_complete_voiced_movie(self) -> None:
        ready = theater_release_ready(
            {
                "assembly_status": "complete",
                "final_video_url": "https://example.com/movie.mp4",
                "narration_lines": ["Once upon a time"],
                "audio_available": True,
                "final_has_audio_stream": True,
                "final_video_duration_sec": 12.5,
                "theater_release_ready": True,
            }
        )
        self.assertTrue(ready)

    def test_theater_release_rejects_too_short_movie(self) -> None:
        ready = theater_release_ready(
            {
                "assembly_status": "complete",
                "final_video_url": "https://example.com/movie.mp4",
                "final_video_duration_sec": 0.4,
                "theater_release_ready": True,
            }
        )
        self.assertFalse(ready)


if __name__ == "__main__":
    unittest.main()
