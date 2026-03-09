from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.storybook_movie_quality import (
    BURNED_CAPTIONS_DEFAULT,
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
    motion_profile,
    motion_timing,
    narration_required_default,
    plan_storyboard_shots,
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
        self.assertGreaterEqual(settle_seconds, 0.10)
        self.assertLessEqual(settle_seconds, 0.24)
        self.assertGreaterEqual(travel_seconds, 1.1)
        self.assertLessEqual(travel_seconds, 3.8)
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
        self.assertFalse(BURNED_CAPTIONS_DEFAULT)
        self.assertFalse(captions_enabled_default())

    def test_release_gate_rejects_weak_movies(self) -> None:
        ready, issues = storybook_release_gate(
            scene_count=2,
            final_video_duration_sec=8.0,
            expected_audio=True,
            audio_available=True,
            final_has_audio_stream=True,
            expected_narration_count=2,
            rendered_narration_count=2,
            shot_types=["establishing", "pullback"],
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


if __name__ == "__main__":
    unittest.main()
