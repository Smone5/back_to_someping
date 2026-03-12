from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.tools import (
    PostMovieMetaReview,
    _require_typed_model_response as require_post_movie_typed_response,
)
from backend.ffmpeg_worker.assemble import (
    StoryboardReviewResponse,
    _require_typed_model_response as require_storyboard_typed_response,
)


class _FakeResponse:
    def __init__(self, parsed):
        self.parsed = parsed


class StructuredModelResponseTests(unittest.TestCase):
    def test_post_movie_helper_accepts_typed_schema_instances(self) -> None:
        response = _FakeResponse(
            PostMovieMetaReview(
                global_feedback=["Looks good."],
                warnings=[],
                issues=[],
            )
        )

        payload = require_post_movie_typed_response(
            response,
            PostMovieMetaReview,
            label="post-movie",
        )

        self.assertEqual(payload, {"global_feedback": ["Looks good."], "warnings": [], "issues": []})

    def test_post_movie_helper_accepts_valid_parsed_dicts(self) -> None:
        response = _FakeResponse({"global_feedback": ["Looks good."], "warnings": [], "issues": []})

        payload = require_post_movie_typed_response(
            response,
            PostMovieMetaReview,
            label="post-movie",
        )

        self.assertEqual(payload, {"global_feedback": ["Looks good."], "warnings": [], "issues": []})

    def test_post_movie_helper_rejects_invalid_parsed_dicts(self) -> None:
        response = _FakeResponse({"global_feedback": "wrong type"})

        payload = require_post_movie_typed_response(
            response,
            PostMovieMetaReview,
            label="post-movie",
        )

        self.assertIsNone(payload)

    def test_storyboard_helper_accepts_typed_schema_instances(self) -> None:
        response = _FakeResponse(
            StoryboardReviewResponse(
                global_feedback=["Check continuity."],
                scene_fixes=[],
            )
        )

        payload = require_storyboard_typed_response(
            response,
            StoryboardReviewResponse,
            label="storyboard",
        )

        self.assertEqual(payload, {"global_feedback": ["Check continuity."], "scene_fixes": []})

    def test_storyboard_helper_accepts_valid_parsed_dicts(self) -> None:
        response = _FakeResponse({"global_feedback": ["Check continuity."], "scene_fixes": []})

        payload = require_storyboard_typed_response(
            response,
            StoryboardReviewResponse,
            label="storyboard",
        )

        self.assertEqual(payload, {"global_feedback": ["Check continuity."], "scene_fixes": []})

    def test_storyboard_helper_rejects_missing_parsed_output(self) -> None:
        response = _FakeResponse(None)

        payload = require_storyboard_typed_response(
            response,
            StoryboardReviewResponse,
            label="storyboard",
        )

        self.assertIsNone(payload)

    def test_storyboard_helper_rejects_invalid_parsed_dicts(self) -> None:
        response = _FakeResponse({"global_feedback": "wrong type", "scene_fixes": []})

        payload = require_storyboard_typed_response(
            response,
            StoryboardReviewResponse,
            label="storyboard",
        )

        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
