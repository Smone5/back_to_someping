from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.storybook_movie_quality import (
    BURNED_CAPTIONS_DEFAULT,
    KEN_BURNS_SETTLE_MAX_SECONDS,
    KEN_BURNS_SETTLE_MIN_SECONDS,
    KEN_BURNS_TRAVEL_MAX_SECONDS,
    KEN_BURNS_TRAVEL_MIN_SECONDS,
    KEN_BURNS_ZOOM_MAX,
    KEN_BURNS_ZOOM_MIN,
    MUSIC_VOLUME_DEFAULT,
    NARRATION_REQUIRED_DEFAULT,
    PAGE_SECONDS_DEFAULT,
    PAGE_SECONDS_MAX,
    PAGE_SECONDS_MIN,
    SFX_COOLDOWN_PAGES_DEFAULT,
    SFX_MAX_DEFAULT,
    THEATER_MIN_HEIGHT_DESKTOP_PX,
    THEATER_MIN_HEIGHT_MOBILE_PX,
    THEATER_MIN_HEIGHT_TABLET_PX,
    clamp_music_volume,
    clamp_narration_volume,
    clamp_page_seconds,
    clamp_sfx_cooldown_pages,
    clamp_sfx_max,
    clamp_sfx_volume,
    captions_enabled_default,
    choose_readalong_text,
    motion_profile,
    motion_timing,
    normalize_storybook_movie_pacing,
    narration_required_default,
    plan_storyboard_shots,
    readalong_seconds_for_age,
    storybook_movie_pacing_default,
    storybook_tts_speaking_rate,
    storybook_tts_tempo_factor,
    storybook_page_duration_seconds,
    storybook_release_gate,
)


class StorybookMovieQualityTests(unittest.TestCase):
    def test_page_seconds_default_and_bounds(self) -> None:
        self.assertEqual(clamp_page_seconds(None), PAGE_SECONDS_DEFAULT)
        self.assertEqual(clamp_page_seconds(1), PAGE_SECONDS_MIN)
        self.assertEqual(clamp_page_seconds(10), PAGE_SECONDS_MAX)

    def test_mix_defaults_are_preschool_safe(self) -> None:
        self.assertEqual(clamp_music_volume(None), MUSIC_VOLUME_DEFAULT)
        self.assertGreater(clamp_narration_volume(None), clamp_music_volume(None))
        self.assertGreater(clamp_narration_volume(None), clamp_sfx_volume(None))

    def test_sfx_density_defaults_and_caps(self) -> None:
        self.assertEqual(clamp_sfx_max(None), SFX_MAX_DEFAULT)
        self.assertEqual(clamp_sfx_max(10), 3)
        self.assertEqual(clamp_sfx_cooldown_pages(None), SFX_COOLDOWN_PAGES_DEFAULT)
        self.assertEqual(clamp_sfx_cooldown_pages(9), 2)

    def test_motion_profile_and_timing_are_bounded(self) -> None:
        self.assertEqual(len(motion_profile(0)), 6)
        zoom_in_profiles = 0
        zoom_out_profiles = 0
        for idx in range(12):
            _, _, _, _, zoom_start, zoom_end = motion_profile(idx)
            self.assertGreaterEqual(zoom_start, KEN_BURNS_ZOOM_MIN)
            self.assertLessEqual(zoom_start, KEN_BURNS_ZOOM_MAX)
            self.assertGreaterEqual(zoom_end, KEN_BURNS_ZOOM_MIN)
            self.assertLessEqual(zoom_end, KEN_BURNS_ZOOM_MAX)
            if zoom_end > zoom_start:
                zoom_in_profiles += 1
            if zoom_start > zoom_end:
                zoom_out_profiles += 1
        settle_seconds, travel_seconds, motion_factor = motion_timing(4.0)
        self.assertGreaterEqual(settle_seconds, KEN_BURNS_SETTLE_MIN_SECONDS)
        self.assertLessEqual(settle_seconds, KEN_BURNS_SETTLE_MAX_SECONDS)
        self.assertGreaterEqual(travel_seconds, KEN_BURNS_TRAVEL_MIN_SECONDS)
        self.assertLessEqual(travel_seconds, KEN_BURNS_TRAVEL_MAX_SECONDS)
        self.assertGreaterEqual(motion_factor, 0.0)
        self.assertLessEqual(motion_factor, 1.0)
        self.assertGreaterEqual(zoom_in_profiles, 3)
        self.assertGreaterEqual(zoom_out_profiles, 3)

    def test_storyboard_shot_planner_enforces_variety(self) -> None:
        descriptions = [
            "A magical castle appears under the moon with wide snowy hills around it.",
            "Inside the castle, a tiny elf paints a bright toy train beside a cozy fireplace.",
            "They open a secret workshop door and discover a glowing room full of tools.",
            "From a high window, the whole winter kingdom sparkles outside.",
        ]
        plans = plan_storyboard_shots(descriptions)
        self.assertEqual(len(plans), len(descriptions))
        self.assertEqual(plans[0].shot_type, "establishing")
        self.assertEqual(plans[-1].shot_type, "pullback")
        self.assertGreaterEqual(len({plan.shot_type for plan in plans}), 3)

    def test_theater_stage_min_sizes_are_explicit(self) -> None:
        self.assertEqual(THEATER_MIN_HEIGHT_DESKTOP_PX, 420)
        self.assertEqual(THEATER_MIN_HEIGHT_TABLET_PX, 300)
        self.assertEqual(THEATER_MIN_HEIGHT_MOBILE_PX, 260)

    def test_narration_and_caption_defaults_match_product_spec(self) -> None:
        self.assertTrue(NARRATION_REQUIRED_DEFAULT)
        self.assertTrue(narration_required_default())
        self.assertTrue(BURNED_CAPTIONS_DEFAULT)
        self.assertTrue(captions_enabled_default())
        self.assertEqual(storybook_movie_pacing_default(), "read_with_me")
        self.assertEqual(normalize_storybook_movie_pacing("voice-first"), "read_to_me")
        self.assertEqual(normalize_storybook_movie_pacing("replay"), "fast_movie")
        self.assertEqual(normalize_storybook_movie_pacing(""), "read_with_me")

    def test_readalong_text_prefers_age_sized_copy(self) -> None:
        primary = "Erin tiptoes past the sparkling candy canes as Santa's glowing castle windows twinkle with a cozy secret tonight."
        fallback = "Erin tiptoes by Santa's glowing castle."
        self.assertEqual(
            choose_readalong_text(primary, fallback, 4),
            fallback,
        )
        self.assertEqual(
            choose_readalong_text("Santa smiles warmly.", fallback, 4),
            "Santa smiles warmly.",
        )
        self.assertEqual(
            choose_readalong_text("Erin tiptoes by Santa's glowing castle.", fallback, 4, "read_to_me"),
            "Erin tiptoes by Santa's glowing castle.",
        )
        self.assertLessEqual(
            len(choose_readalong_text(primary, fallback, 4, "read_to_me").split()),
            len(choose_readalong_text(primary, fallback, 4, "read_with_me").split()),
        )
        self.assertEqual(
            choose_readalong_text(
                "Then, go down and slide.",
                "The toy friends giggle as they zoom down the slide.",
                5,
            ),
            "The toy friends giggle as they zoom down the slide.",
        )
        self.assertEqual(
            choose_readalong_text(
                "Gnome village.",
                "A cozy gnome village twinkles beneath the tree house.",
                5,
            ),
            "A cozy gnome village twinkles beneath the tree house.",
        )

    def test_readalong_seconds_and_page_duration_scale_by_age(self) -> None:
        line = "Erin tiptoes by Santa's glowing castle."
        self.assertGreater(readalong_seconds_for_age(line, 4), readalong_seconds_for_age(line, 9))
        self.assertGreaterEqual(
            storybook_page_duration_seconds(
                child_age=4,
                base_page_seconds=4.0,
                narration_seconds=3.2,
                readalong_text=line,
            ),
            5.2,
        )
        self.assertGreaterEqual(
            storybook_page_duration_seconds(
                child_age=9,
                base_page_seconds=4.0,
                narration_seconds=5.0,
                readalong_text=line,
            ),
            5.4,
        )
        self.assertLess(
            readalong_seconds_for_age(line, 4, "read_to_me"),
            readalong_seconds_for_age(line, 4, "read_with_me"),
        )
        self.assertLess(
            storybook_page_duration_seconds(
                child_age=4,
                base_page_seconds=4.0,
                narration_seconds=3.2,
                readalong_text=line,
                movie_pacing="fast_movie",
            ),
            storybook_page_duration_seconds(
                child_age=4,
                base_page_seconds=4.0,
                narration_seconds=3.2,
                readalong_text=line,
                movie_pacing="read_with_me",
            ),
        )

    def test_tts_pacing_is_slightly_slower_for_preschoolers(self) -> None:
        self.assertAlmostEqual(storybook_tts_speaking_rate(0.9, 4), 0.84)
        self.assertAlmostEqual(storybook_tts_speaking_rate(0.9, 8), 0.9)
        self.assertAlmostEqual(storybook_tts_tempo_factor(4), 0.94)
        self.assertAlmostEqual(storybook_tts_tempo_factor(8), 1.0)
        self.assertAlmostEqual(storybook_tts_speaking_rate(0.9, 4, "read_to_me"), 0.86)
        self.assertAlmostEqual(storybook_tts_speaking_rate(0.9, 4, "fast_movie"), 0.89)
        self.assertAlmostEqual(storybook_tts_tempo_factor(4, "read_to_me"), 0.97)
        self.assertAlmostEqual(storybook_tts_tempo_factor(4, "fast_movie"), 1.0)

    def test_release_gate_allows_single_scene_movie_when_media_is_valid(self) -> None:
        ready, issues = storybook_release_gate(
            scene_count=1,
            final_video_duration_sec=8.0,
            expected_audio=True,
            audio_available=True,
            final_has_audio_stream=True,
            expected_narration_count=1,
            rendered_narration_count=1,
            shot_types=["establishing"],
        )
        self.assertTrue(ready)
        self.assertEqual(issues, [])

    def test_release_gate_rejects_weak_movies(self) -> None:
        ready, issues = storybook_release_gate(
            scene_count=0,
            final_video_duration_sec=8.0,
            expected_audio=True,
            audio_available=True,
            final_has_audio_stream=True,
            expected_narration_count=1,
            rendered_narration_count=1,
            shot_types=[],
        )
        self.assertFalse(ready)
        self.assertTrue(any("scene_count" in issue for issue in issues))

        ready, issues = storybook_release_gate(
            scene_count=4,
            final_video_duration_sec=16.0,
            expected_audio=True,
            audio_available=True,
            final_has_audio_stream=True,
            expected_narration_count=4,
            rendered_narration_count=2,
            shot_types=["establishing", "reveal", "closeup", "pullback"],
        )
        self.assertFalse(ready)
        self.assertTrue(any("narration coverage" in issue for issue in issues))

    def test_release_gate_requires_narration_per_scene_when_audio_policy_is_on(self) -> None:
        ready, issues = storybook_release_gate(
            scene_count=3,
            final_video_duration_sec=12.0,
            expected_audio=True,
            audio_available=True,
            final_has_audio_stream=True,
            expected_narration_count=0,
            rendered_narration_count=1,
            shot_types=["establishing", "reveal", "pullback"],
        )
        self.assertFalse(ready)
        self.assertTrue(any("narration coverage" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
