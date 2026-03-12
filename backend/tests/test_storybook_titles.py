from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.tools import _clean_storybook_title, _heuristic_storybook_title
from backend.ffmpeg_worker.assemble import _clean_title, _heuristic_title


class StorybookTitleTests(unittest.TestCase):
    def test_live_title_cleaner_rejects_what_and_the_drawing(self) -> None:
        self.assertEqual(_clean_storybook_title("What and the Drawing"), "")

    def test_movie_title_cleaner_rejects_what_and_the_drawing(self) -> None:
        self.assertEqual(_clean_title("What and the Drawing"), "")

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


if __name__ == "__main__":
    unittest.main()
