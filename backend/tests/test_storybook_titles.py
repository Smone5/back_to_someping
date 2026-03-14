from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.tools import _clean_storybook_title, _heuristic_storybook_title
from backend.ffmpeg_worker.assemble import _clean_title, _heuristic_title
from shared.storybook_titles import validate_storybook_title


class StorybookTitleTests(unittest.TestCase):
    def test_live_title_cleaner_rejects_what_and_the_drawing(self) -> None:
        self.assertEqual(_clean_storybook_title("What and the Drawing"), "")

    def test_movie_title_cleaner_rejects_what_and_the_drawing(self) -> None:
        self.assertEqual(_clean_title("What and the Drawing"), "")

    def test_live_title_cleaner_rejects_should_and_the_look(self) -> None:
        self.assertEqual(_clean_storybook_title("Should and the Look"), "")

    def test_movie_title_cleaner_rejects_should_and_the_look(self) -> None:
        self.assertEqual(_clean_title("Should and the Look"), "")

    def test_live_heuristic_ignores_question_and_drawing_words(self) -> None:
        title = _heuristic_storybook_title(
            [
                "What does the drawing show? A spooky castle glows under the moon.",
                "A friendly dragon sleeps beside the castle path.",
            ],
            "What happens next in the castle adventure?",
            "Aaron",
        )

        self.assertNotIn("What", title)
        self.assertNotIn("Drawing", title)
        self.assertTrue("Castle" in title or "Dragon" in title)

    def test_live_heuristic_prefers_story_specific_places(self) -> None:
        title = _heuristic_storybook_title(
            [
                "We sailed down a chocolate river into Candy Land.",
                "A moonlit dark castle waited beyond the gummy bear hills.",
            ],
            "Erin bravely followed the chocolate river toward the dark castle in Candy Land.",
            "Erin",
        )

        self.assertNotIn("Should", title)
        self.assertNotIn("Look", title)
        self.assertTrue(
            any(
                phrase in title
                for phrase in ("Chocolate River", "Candy Land", "Dark Castle", "Gummy Bear Hills")
            ),
            title,
        )

    def test_movie_heuristic_ignores_question_and_drawing_words(self) -> None:
        title = _heuristic_title(
            [
                "What does the drawing show? A spooky castle glows under the moon.",
                "A friendly dragon sleeps beside the castle path.",
            ],
            "What happens next in the castle adventure?",
        )

        self.assertNotIn("What", title)
        self.assertNotIn("Drawing", title)
        self.assertTrue("Castle" in title or "Dragon" in title)

    def test_validator_rejects_unrelated_or_fragment_titles(self) -> None:
        scene_descriptions = [
            "We followed the chocolate river through Candy Land.",
            "A dark castle glowed under the moon.",
        ]
        story_summary = "Erin followed the chocolate river to a dark castle in Candy Land."

        self.assertEqual(
            validate_storybook_title("Should and the Look", scene_descriptions, story_summary, "Erin"),
            "",
        )
        self.assertEqual(
            validate_storybook_title("Golden Train", scene_descriptions, story_summary, "Erin"),
            "",
        )
        self.assertEqual(
            validate_storybook_title("Erin and the Dark Castle", scene_descriptions, story_summary, "Erin"),
            "Erin and the Dark Castle",
        )


if __name__ == "__main__":
    unittest.main()
