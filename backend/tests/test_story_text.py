from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.story_text import clean_story_text
from shared.story_text import normalize_storybeat_text
from shared.story_text import story_sentence_needs_revision
from shared.story_text import story_sentence_quality_score
from shared.story_text import truncate_story_sentence


class StoryTextTests(unittest.TestCase):
    def test_normalize_storybeat_text_keeps_real_story_caption(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("Santa gently tucks Aaron into bed beneath the twinkling stars."),
            "Santa gently tucks Aaron into bed beneath the twinkling stars.",
        )

    def test_normalize_storybeat_text_rejects_placeholder_illustration_line(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("Here is the illustration for your story."),
            "",
        )

    def test_normalize_storybeat_text_rejects_adjectival_placeholder_illustration_line(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("Here is a whimsical children's storybook illustration."),
            "",
        )

    def test_normalize_storybeat_text_rejects_meta_page_language(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("This illustration shows Santa tucking Aaron into bed."),
            "",
        )

    def test_normalize_storybeat_text_rejects_you_described_placeholder(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("Here is the Candyland you described."),
            "",
        )

    def test_normalize_storybeat_text_strips_meta_illustration_prefix_with_story_payload(self) -> None:
        self.assertEqual(
            normalize_storybeat_text(
                "Here is a whimsical children's storybook illustration: Santa Claus relaxes in a cozy secret room by a warm stone fireplace."
            ),
            "Santa Claus relaxes in a cozy secret room by a warm stone fireplace.",
        )

    def test_normalize_storybeat_text_strips_bare_illustration_of_prefix_with_story_payload(self) -> None:
        self.assertEqual(
            normalize_storybeat_text(
                "A whimsical children's storybook illustration of: Candy Land shimmered with playful candy details."
            ),
            "Candy Land shimmered with playful candy details.",
        )

    def test_normalize_storybeat_text_strips_you_described_prefix_with_story_payload(self) -> None:
        self.assertEqual(
            normalize_storybeat_text(
                "Here is the Candyland you described: A chocolate river curls past lollipop trees toward a candy castle."
            ),
            "A chocolate river curls past lollipop trees toward a candy castle.",
        )

    def test_normalize_storybeat_text_rejects_conversational_feedback(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("Oh, I love that idea!"),
            "",
        )

    def test_normalize_storybeat_text_rejects_conversational_scene_lead_in(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("Let's go see who lives in the tree house!"),
            "",
        )

    def test_normalize_storybeat_text_prefers_descriptive_sentence_over_branch_label(self) -> None:
        self.assertEqual(
            normalize_storybeat_text(
                "Go inside the treehouse. Inside the cozy treehouse, warm lanterns glow beside tiny bunks."
            ),
            "Inside the cozy treehouse, warm lanterns glow beside tiny bunks.",
        )

    def test_normalize_storybeat_text_strips_promptish_prefixes(self) -> None:
        self.assertEqual(
            normalize_storybeat_text(
                "Inside Go inside the castle, outside Go inside the castle, outside the spooky rainbow castle at night. "
                "A large, friendly dragon is curled up asleep on the grass under the glowing moon and rainbow path. "
                "Keep these same characters in view: silly ghost."
            ),
            "A large, friendly dragon is curled up asleep on the grass under the glowing moon and rainbow path.",
        )

    def test_normalize_storybeat_text_prefers_first_real_scene_sentence_over_prompt_tail(self) -> None:
        self.assertEqual(
            normalize_storybeat_text(
                "A swirling rainbow path glowing under a big spooky moon, leading towards a playful purple castle. "
                "Friendly ghost shapes peek out from behind puffy trees. "
                "Make the destination unmistakable with these setting cues: show unmistakable towers."
            ),
            "A swirling rainbow path glowing under a big spooky moon, leading towards a playful purple castle.",
        )

    def test_clean_story_text_strips_embedded_prompt_suffixes(self) -> None:
        self.assertEqual(
            clean_story_text(
                "At the top of the staircase, a secret door glows softly. Keep these same characters in view: shared toy companion."
            ),
            "At the top of the staircase, a secret door glows softly",
        )

    def test_clean_story_text_strips_story_continuity_suffixes(self) -> None:
        self.assertEqual(
            clean_story_text(
                "Candy Land shimmered with playful candy details. Story continuity target: keep this page in or directly connected to Candy Land. Transition type: route progress."
            ),
            "Candy Land shimmered with playful candy details",
        )

    def test_clean_story_text_strips_box_glyphs_and_variation_selectors(self) -> None:
        self.assertEqual(
            clean_story_text("There are funny ghosts\u25a1 holding lanterns\ufe0f by the chair."),
            "There are funny ghosts holding lanterns by the chair.",
        )

    def test_clean_story_text_strips_html_tags_and_entities(self) -> None:
        self.assertEqual(
            clean_story_text("&lt;p&gt;The friendly toy companion peeks around the candy hall.&lt;/p&gt;"),
            "The friendly toy companion peeks around the candy hall.",
        )

    def test_normalize_storybeat_text_strips_html_tags(self) -> None:
        self.assertEqual(
            normalize_storybeat_text("<p>The friendly toy companion peeks around the candy hall.</p>"),
            "The friendly toy companion peeks around the candy hall.",
        )

    def test_truncate_story_sentence_drops_dangling_descriptor_endings(self) -> None:
        self.assertEqual(
            truncate_story_sentence(
                "Inside the cozy castle, we find soft toys and a big key!",
                max_words=11,
            ),
            "Inside the cozy castle, we find soft toys.",
        )

    def test_truncate_story_sentence_drops_dangling_fragment_endings(self) -> None:
        self.assertEqual(
            truncate_story_sentence(
                "Lion-O is very excited to explore the cozy, colorful castle made of candy.",
                max_words=11,
            ),
            "Lion-O is very excited to explore the cozy, colorful castle.",
        )

    def test_story_sentence_quality_prefers_complete_story_line(self) -> None:
        self.assertGreaterEqual(
            story_sentence_quality_score("A rainbow path shines under the spooky moon."),
            5,
        )

    def test_story_sentence_needs_revision_flags_duplicates_and_dangling_endings(self) -> None:
        self.assertTrue(
            story_sentence_needs_revision("The path is brightly is is lit with many colors but.")
        )
        self.assertTrue(
            story_sentence_needs_revision("The path is brightly is lit with many colors but the surroundings.")
        )
        self.assertTrue(
            story_sentence_needs_revision("inside a cozy, slightly spooky castle hallway with purple stone walls appears.")
        )
        self.assertTrue(
            story_sentence_needs_revision("Lion-O is very excited to explore the cozy, colorful castle made.")
        )
        self.assertFalse(
            story_sentence_needs_revision("A rainbow path shines under the spooky moon.")
        )


if __name__ == "__main__":
    unittest.main()
