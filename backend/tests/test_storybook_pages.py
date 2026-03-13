from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.storybook_pages import story_pages_from_state_data


class StorybookPagesTests(unittest.TestCase):
    def test_explicit_story_pages_take_precedence(self) -> None:
        pages = story_pages_from_state_data(
            {
                "story_pages": [
                    {
                        "scene_number": 2,
                        "request_id": "req-b",
                        "scene_description": "Library",
                        "storybeat_text": "A cozy library appears.",
                        "image_url": "https://example.com/library.png",
                    },
                    {
                        "scene_number": 1,
                        "request_id": "req-a",
                        "scene_description": "Castle",
                        "storybeat_text": "A castle appears.",
                        "image_url": "https://example.com/castle.png",
                    },
                ],
                "scene_branch_points": [
                    {
                        "scene_number": 1,
                        "scene_description": "Wrong fallback",
                    }
                ],
            }
        )
        self.assertEqual([page["scene_number"] for page in pages], [1, 2])
        self.assertEqual(pages[0]["request_id"], "req-a")
        self.assertEqual(pages[1]["request_id"], "req-b")
        self.assertEqual(pages[0]["scene_description"], "Castle")

    def test_story_pages_merge_missing_fields_and_missing_pages_from_branch_points(self) -> None:
        pages = story_pages_from_state_data(
            {
                "story_pages": [
                    {
                        "scene_number": 1,
                        "request_id": "req-a",
                        "scene_description": "Bubble forest",
                        "storybeat_text": "Bubbles glow in the forest.",
                    }
                ],
                "scene_branch_points": [
                    {
                        "scene_number": 1,
                        "request_id": "req-a",
                        "image_url": "https://example.com/forest.png",
                    },
                    {
                        "scene_number": 2,
                        "request_id": "req-b",
                        "scene_description": "Secret bubble path",
                        "storybeat_text": "The bubble shows a secret path.",
                        "image_url": "https://example.com/path.png",
                    },
                ],
            }
        )

        self.assertEqual([page["scene_number"] for page in pages], [1, 2])
        self.assertEqual(pages[0]["image_url"], "https://example.com/forest.png")
        self.assertEqual(pages[1]["request_id"], "req-b")
        self.assertEqual(pages[1]["storybeat_text"], "The bubble shows a secret path.")

    def test_branch_points_are_normalized_when_explicit_pages_missing(self) -> None:
        pages = story_pages_from_state_data(
            {
                "scene_branch_points": [
                    {
                        "scene_number": 3,
                        "label": "Ghost chef cookies",
                        "image_url": "data:image/webp;base64,abc",
                    },
                    {
                        "scene_number": 2,
                        "scene_description": "Castle kitchen",
                        "gcs_uri": "gs://bucket/session/scene_002.jpg",
                    },
                ]
            }
        )
        self.assertEqual([page["scene_number"] for page in pages], [2, 3])
        self.assertEqual(pages[0]["gcs_uri"], "gs://bucket/session/scene_002.jpg")
        self.assertEqual(pages[1]["storybeat_text"], "")

    def test_branch_labels_do_not_become_storybeat_text_when_pages_missing_it(self) -> None:
        pages = story_pages_from_state_data(
            {
                "scene_branch_points": [
                    {
                        "scene_number": 2,
                        "label": "Go inside the treehouse",
                        "scene_description": "A cozy treehouse glows at the end of the path.",
                        "image_url": "https://example.com/treehouse.png",
                    }
                ]
            }
        )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["storybeat_text"], "")
        self.assertEqual(pages[0]["scene_description"], "A cozy treehouse glows at the end of the path.")

    def test_story_page_lighting_metadata_is_preserved(self) -> None:
        pages = story_pages_from_state_data(
            {
                "story_pages": [
                    {
                        "scene_number": 1,
                        "request_id": "req-a",
                        "scene_description": "A moonlit castle hallway sparkles with bubbles.",
                        "hex_color": "#6FA8FF",
                        "rgb_color": [111, 168, 255],
                        "brightness": 154,
                        "transition": 1.1,
                        "cue_source": "heuristic_scene",
                    }
                ]
            }
        )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["hex_color"], "#6FA8FF")
        self.assertEqual(pages[0]["rgb_color"], [111, 168, 255])
        self.assertEqual(pages[0]["brightness"], 154)
        self.assertEqual(pages[0]["transition"], 1.1)
        self.assertEqual(pages[0]["cue_source"], "heuristic_scene")


if __name__ == "__main__":
    unittest.main()
