from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.story_text import story_sentence_needs_revision

_ASSEMBLE_SPEC = importlib.util.spec_from_file_location(
    "assemble",
    _PROJECT_ROOT / "backend" / "ffmpeg_worker" / "assemble.py",
)
if _ASSEMBLE_SPEC is None or _ASSEMBLE_SPEC.loader is None:
    raise RuntimeError("Could not load FFmpeg assemble module for tests.")
_ASSEMBLE_MODULE = importlib.util.module_from_spec(_ASSEMBLE_SPEC)
_ASSEMBLE_SPEC.loader.exec_module(_ASSEMBLE_MODULE)

_GCS_FINAL_BUCKET = _ASSEMBLE_MODULE.GCS_FINAL_BUCKET
_build_storybook_movie_readalong_segments = _ASSEMBLE_MODULE._build_storybook_movie_readalong_segments
_build_storybook_theater_lighting_cues = _ASSEMBLE_MODULE._build_storybook_theater_lighting_cues
_build_storybook_readalong_ass_text = _ASSEMBLE_MODULE._build_storybook_readalong_ass_text
_build_video_assembly_render_report = _ASSEMBLE_MODULE._build_video_assembly_render_report
_build_public_story_video_url = _ASSEMBLE_MODULE._build_public_story_video_url
_build_narration_segments = _ASSEMBLE_MODULE._build_narration_segments
_build_page_narration_source_texts = _ASSEMBLE_MODULE._build_page_narration_source_texts
_build_storybook_padded_audio_track = _ASSEMBLE_MODULE._build_storybook_padded_audio_track
_ensure_end_card_music_cue = _ASSEMBLE_MODULE._ensure_end_card_music_cue
_elevenlabs_generate_music = _ASSEMBLE_MODULE._elevenlabs_generate_music
_elevenlabs_generate_sfx = _ASSEMBLE_MODULE._elevenlabs_generate_sfx
_ffmpeg_nonsilent_seconds = _ASSEMBLE_MODULE._ffmpeg_nonsilent_seconds
_ffprobe_duration = _ASSEMBLE_MODULE._ffprobe_duration
_generate_storybook_music_bytes = _ASSEMBLE_MODULE._generate_storybook_music_bytes
_narration_mentions_unsupported_detail = _ASSEMBLE_MODULE._narration_mentions_unsupported_detail
_resolve_storybook_word_starts_ms = _ASSEMBLE_MODULE._resolve_storybook_word_starts_ms
_recommended_audio_boost_gain_db = _ASSEMBLE_MODULE._recommended_audio_boost_gain_db
_run_storybook_studio_async = _ASSEMBLE_MODULE._run_storybook_studio_async
_simplify_for_readalong = _ASSEMBLE_MODULE._simplify_for_readalong
_scene_sources_from_state_doc = _ASSEMBLE_MODULE._scene_sources_from_state_doc
_resolve_story_page_sources_for_assembly = _ASSEMBLE_MODULE._resolve_story_page_sources_for_assembly
_story_page_fallback_data_url = _ASSEMBLE_MODULE._story_page_fallback_data_url
_backfill_missing_still_paths_with_story_cards = _ASSEMBLE_MODULE._backfill_missing_still_paths_with_story_cards
_merge_scene_sources_with_recovered_stills = _ASSEMBLE_MODULE._merge_scene_sources_with_recovered_stills
_build_storybook_cover_filtergraph = _ASSEMBLE_MODULE._build_storybook_cover_filtergraph
_storybook_burned_caption_filtergraph = _ASSEMBLE_MODULE._storybook_burned_caption_filtergraph
_storybook_cover_logo_path = _ASSEMBLE_MODULE._storybook_cover_logo_path
_storybook_motion_vf = _ASSEMBLE_MODULE._storybook_motion_vf
_synthesize_tts_with_provider = _ASSEMBLE_MODULE._synthesize_tts_with_provider


class StorybookAssembleSourceTests(unittest.TestCase):
    def test_storybook_cover_filtergraph_can_overlay_logo(self) -> None:
        filtergraph = _build_storybook_cover_filtergraph(
            title_text="Aaron and the Dark Castle",
            subtitle_text="by Aaron",
            duration=3.5,
            logo_path=Path("/tmp/voxitale arch.png"),
        )

        self.assertIn("movie='/tmp/voxitale arch.png'", filtergraph)
        self.assertIn("overlay=", filtergraph)
        self.assertIn("[outv]", filtergraph)

    def test_storybook_cover_logo_path_finds_repo_asset(self) -> None:
        logo_path = _storybook_cover_logo_path()

        self.assertIsNotNone(logo_path)
        assert logo_path is not None
        self.assertEqual(logo_path.name, "voxitale_arch.png")

    @mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "eleven-123"}, clear=False)
    def test_storybook_elevenlabs_voice_override_changes_tts_endpoint(self) -> None:
        client = mock.Mock()
        response = mock.Mock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.json.return_value = {
            "audio_base64": "UklGRg==",
            "alignment": {
                "characters": list("Hi."),
                "character_start_times_seconds": [0.0, 0.1, 0.2],
                "character_end_times_seconds": [0.1, 0.2, 0.3],
            },
        }
        client.post.return_value = response
        with mock.patch.object(_ASSEMBLE_MODULE.httpx, "Client") as mock_client_cls:
            with mock.patch.object(_ASSEMBLE_MODULE, "_slow_storybook_tts_audio", return_value=b"voice-bytes"):
                mock_client_cls.return_value.__enter__.return_value = client
                provider, _audio, _timings = _synthesize_tts_with_provider(
                    "Hi.",
                    child_age=4,
                    storybook_movie_pacing="read_with_me",
                    voice_id="EXAVITQu4vr4xnSDxMaL",
                )

        self.assertEqual(provider, "elevenlabs")
        self.assertIn(
            "/text-to-speech/EXAVITQu4vr4xnSDxMaL/with-timestamps",
            client.post.call_args.args[0],
        )

    def test_storybook_selected_voice_does_not_fallback_to_robotic_provider(self) -> None:
        with mock.patch.object(
            _ASSEMBLE_MODULE,
            "_synthesize_tts_elevenlabs_with_timing",
            return_value=(None, None),
        ):
            with mock.patch.object(_ASSEMBLE_MODULE, "_synthesize_tts_gemini_only", return_value=b"robotic"):
                provider, audio, timings = _synthesize_tts_with_provider(
                    "Hi.",
                    child_age=4,
                    storybook_movie_pacing="read_with_me",
                    voice_id="EXAVITQu4vr4xnSDxMaL",
                    allow_non_elevenlabs_fallback=False,
                )

        self.assertIsNone(provider)
        self.assertIsNone(audio)
        self.assertIsNone(timings)

    @mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "eleven-123", "ELEVENLABS_VOICE_ID": "EXAVITQu4vr4xnSDxMaL"}, clear=False)
    def test_storybook_selected_voice_retries_with_default_elevenlabs_voice_when_missing(self) -> None:
        client = mock.Mock()
        not_found_response = mock.Mock()
        not_found_response.status_code = 404
        not_found_response.headers = {"content-type": "application/json"}
        not_found_response.json.return_value = {
            "detail": {
                "type": "not_found",
                "code": "voice_not_found",
                "message": "A voice with voice_id 'S9EY1FKT0mCZ06GOW6V4' was not found.",
            }
        }
        not_found_response.text = '{"detail":{"code":"voice_not_found"}}'

        success_response = mock.Mock()
        success_response.status_code = 200
        success_response.headers = {"content-type": "application/json"}
        success_response.json.return_value = {
            "audio_base64": "UklGRg==",
            "alignment": {
                "characters": list("Hi."),
                "character_start_times_seconds": [0.0, 0.1, 0.2],
                "character_end_times_seconds": [0.1, 0.2, 0.3],
            },
        }
        client.post.side_effect = [not_found_response, success_response]

        with mock.patch.object(_ASSEMBLE_MODULE.httpx, "Client") as mock_client_cls:
            with mock.patch.object(_ASSEMBLE_MODULE, "_slow_storybook_tts_audio", return_value=b"voice-bytes"):
                with mock.patch.dict(_ASSEMBLE_MODULE._ELEVENLABS_VOICE_FALLBACK_OVERRIDES, {}, clear=True):
                    mock_client_cls.return_value.__enter__.return_value = client
                    provider, audio, timings = _synthesize_tts_with_provider(
                        "Hi.",
                        child_age=4,
                        storybook_movie_pacing="read_with_me",
                        voice_id="S9EY1FKT0mCZ06GOW6V4",
                        allow_non_elevenlabs_fallback=False,
                    )

        self.assertEqual(provider, "elevenlabs")
        self.assertEqual(audio, b"voice-bytes")
        self.assertIsNotNone(timings)
        self.assertIn(
            "/text-to-speech/S9EY1FKT0mCZ06GOW6V4/with-timestamps",
            client.post.call_args_list[0].args[0],
        )
        self.assertIn(
            "/text-to-speech/EXAVITQu4vr4xnSDxMaL/with-timestamps",
            client.post.call_args_list[1].args[0],
        )

    @mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "eleven-123", "ELEVENLABS_VOICE_ID": "EXAVITQu4vr4xnSDxMaL"}, clear=False)
    def test_storybook_selected_voice_caches_fallback_after_first_missing_voice(self) -> None:
        client = mock.Mock()
        not_found_response = mock.Mock()
        not_found_response.status_code = 404
        not_found_response.headers = {"content-type": "application/json"}
        not_found_response.json.return_value = {
            "detail": {
                "type": "not_found",
                "code": "voice_not_found",
                "message": "A voice with voice_id 'S9EY1FKT0mCZ06GOW6V4' was not found.",
            }
        }
        not_found_response.text = '{"detail":{"code":"voice_not_found"}}'

        success_response = mock.Mock()
        success_response.status_code = 200
        success_response.headers = {"content-type": "application/json"}
        success_response.json.return_value = {
            "audio_base64": "UklGRg==",
            "alignment": {
                "characters": list("Hi."),
                "character_start_times_seconds": [0.0, 0.1, 0.2],
                "character_end_times_seconds": [0.1, 0.2, 0.3],
            },
        }
        client.post.side_effect = [not_found_response, success_response, success_response]

        with mock.patch.object(_ASSEMBLE_MODULE.httpx, "Client") as mock_client_cls:
            with mock.patch.object(_ASSEMBLE_MODULE, "_slow_storybook_tts_audio", return_value=b"voice-bytes"):
                with mock.patch.dict(_ASSEMBLE_MODULE._ELEVENLABS_VOICE_FALLBACK_OVERRIDES, {}, clear=True):
                    mock_client_cls.return_value.__enter__.return_value = client
                    first_provider, first_audio, _ = _synthesize_tts_with_provider(
                        "Hi.",
                        child_age=4,
                        storybook_movie_pacing="read_with_me",
                        voice_id="S9EY1FKT0mCZ06GOW6V4",
                        allow_non_elevenlabs_fallback=False,
                    )
                    second_provider, second_audio, _ = _synthesize_tts_with_provider(
                        "Hi again.",
                        child_age=4,
                        storybook_movie_pacing="read_with_me",
                        voice_id="S9EY1FKT0mCZ06GOW6V4",
                        allow_non_elevenlabs_fallback=False,
                    )

        self.assertEqual(first_provider, "elevenlabs")
        self.assertEqual(second_provider, "elevenlabs")
        self.assertEqual(first_audio, b"voice-bytes")
        self.assertEqual(second_audio, b"voice-bytes")
        self.assertEqual(len(client.post.call_args_list), 3)
        self.assertIn(
            "/text-to-speech/S9EY1FKT0mCZ06GOW6V4/with-timestamps",
            client.post.call_args_list[0].args[0],
        )
        self.assertIn(
            "/text-to-speech/EXAVITQu4vr4xnSDxMaL/with-timestamps",
            client.post.call_args_list[1].args[0],
        )
        self.assertIn(
            "/text-to-speech/EXAVITQu4vr4xnSDxMaL/with-timestamps",
            client.post.call_args_list[2].args[0],
        )

    def test_video_assembly_render_report_does_not_retry_release_gate_quality_failures(self) -> None:
        report = _build_video_assembly_render_report(
            "session-123",
            data={
                "assembly_status": "failed",
                "assembly_error": (
                    "The final storybook movie failed the release gate. "
                    "duration=12.00s audio_expected=True audio_available=True "
                    "final_has_audio_stream=True issues=shot variety 1 is below required 3"
                ),
                "final_video_url": "https://example.com/final.mp4",
                "audio_expected": True,
                "audio_available": True,
                "final_has_audio_stream": True,
                "theater_release_ready": False,
                "expected_narration_count": 5,
                "rendered_narration_count": 5,
            },
            runtime_overrides={},
            error_text="The final storybook movie failed the release gate.",
        )

        self.assertFalse(report["ready_to_publish"])
        self.assertFalse(report["retryable"])
        self.assertIn(
            "release_gate_failed",
            {issue["code"] for issue in report["issues"]},
        )

    def test_video_assembly_render_report_retries_transient_worker_failures(self) -> None:
        report = _build_video_assembly_render_report(
            "session-123",
            data={
                "assembly_status": "failed",
                "assembly_error": "429 RESOURCE_EXHAUSTED while rendering final movie.",
                "theater_release_ready": False,
            },
            runtime_overrides={},
            error_text="429 RESOURCE_EXHAUSTED while rendering final movie.",
        )

        self.assertFalse(report["ready_to_publish"])
        self.assertTrue(report["retryable"])
        self.assertIn(
            "assembly_failed",
            {issue["code"] for issue in report["issues"]},
        )

    def test_scene_sources_skip_placeholder_story_pages(self) -> None:
        sources = _scene_sources_from_state_doc(
            {
                "story_pages": [
                    {
                        "scene_number": 1,
                        "storybeat_text": "A cozy treehouse glows in the woods.",
                    }
                ]
            }
        )

        self.assertEqual(sources, [])

    def test_scene_sources_recover_real_array_url_when_page_image_is_placeholder(self) -> None:
        sources = _scene_sources_from_state_doc(
            {
                "story_pages": [
                    {
                        "scene_number": 1,
                        "image_url": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
                        "storybeat_text": "A cozy treehouse glows in the woods.",
                    }
                ],
                "scene_asset_urls": ["https://example.com/treehouse.png"],
            }
        )

        self.assertEqual(sources, ["https://example.com/treehouse.png"])

    def test_story_page_fallback_data_url_prefers_png_for_ffmpeg_compatibility(self) -> None:
        data_url = _story_page_fallback_data_url("A cozy treehouse glows at the end of the path.")

        self.assertTrue(data_url.startswith("data:image/png;base64,"))

    def test_resolve_story_page_sources_backfills_missing_page_with_story_card(self) -> None:
        sources, descriptions, fallback_count = _resolve_story_page_sources_for_assembly(
            {
                "story_pages": [
                    {
                        "scene_number": 1,
                        "image_url": "https://example.com/castle-1.png",
                        "storybeat_text": "Aaron and Lion-O reach the licorice castle.",
                    },
                    {
                        "scene_number": 2,
                        "storybeat_text": "Inside the castle, they climb toward a secret room.",
                    },
                ],
            },
            [
                "Aaron and Lion-O reach the licorice castle.",
                "Inside the castle, they climb toward a secret room.",
            ],
            "Aaron and Lion-O reach the licorice castle. Inside the castle, they climb toward a secret room.",
        )

        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0], "https://example.com/castle-1.png")
        self.assertTrue(sources[1].startswith("data:image/png;base64,"))
        self.assertEqual(fallback_count, 1)
        self.assertEqual(len(descriptions), 2)
        self.assertIn("secret room", descriptions[1].lower())

    def test_backfill_missing_still_paths_with_story_cards_recovers_trailing_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            existing = tmp / "scene_000.png"
            existing.write_bytes(b"\x89PNG\r\n\x1a\n")

            completed_paths, created = _backfill_missing_still_paths_with_story_cards(
                [existing],
                expected_scene_pages=3,
                scene_descriptions=[
                    "Aaron reaches the licorice castle.",
                    "Aaron climbs the staircase to the secret room.",
                    "Santa waits inside with presents and candy.",
                ],
                story_summary="Aaron reaches the licorice castle, climbs the staircase, and finds Santa waiting inside.",
                tmp=tmp,
            )

            self.assertEqual(created, 2)
            self.assertEqual(len(completed_paths), 3)
            self.assertEqual(completed_paths[0], existing)
            self.assertTrue(all(path.exists() for path in completed_paths))
            self.assertEqual(completed_paths[1].suffix, ".png")
            self.assertEqual(completed_paths[2].suffix, ".png")
            self.assertGreater(completed_paths[1].stat().st_size, 32)
            self.assertGreater(completed_paths[2].stat().st_size, 32)

    def test_merge_scene_sources_with_recovered_stills_prefers_real_pages_and_keeps_tail_fallback(self) -> None:
        merged_sources, replacement_count = _merge_scene_sources_with_recovered_stills(
            [
                "data:image/png;base64,page1",
                "data:image/png;base64,page2",
                "data:image/png;base64,page3",
                "data:image/png;base64,page4",
                "data:image/png;base64,page5",
                "data:image/png;base64,page6",
                "data:image/png;base64,page7",
            ],
            [
                "gs://storybook/session/scene_000.jpg",
                "gs://storybook/session/scene_001.jpg",
                "gs://storybook/session/scene_002.jpg",
                "gs://storybook/session/scene_003.jpg",
                "gs://storybook/session/scene_004.jpg",
                "gs://storybook/session/scene_005.jpg",
            ],
            expected_scene_pages=7,
        )

        self.assertEqual(replacement_count, 6)
        self.assertEqual(len(merged_sources), 7)
        self.assertEqual(merged_sources[:6], [
            "gs://storybook/session/scene_000.jpg",
            "gs://storybook/session/scene_001.jpg",
            "gs://storybook/session/scene_002.jpg",
            "gs://storybook/session/scene_003.jpg",
            "gs://storybook/session/scene_004.jpg",
            "gs://storybook/session/scene_005.jpg",
        ])
        self.assertTrue(merged_sources[6].startswith("data:image/png;base64,"))

    def test_narration_sources_preserve_scene_alignment_when_page_storybeat_missing(self) -> None:
        sources = _build_page_narration_source_texts(
            [
                {"scene_number": 1, "storybeat_text": "Bubbles shimmer around the trees."},
                {"scene_number": 2, "scene_description": ""},
                {"scene_number": 3, "scene_description": "A cozy treehouse glows at the end of the path."},
            ],
            [
                "Bubbles shimmer around the trees.",
                "The bubble leads to a secret path.",
                "A cozy treehouse glows at the end of the path.",
            ],
            "Bubbles shimmer around the trees. The bubble leads to a secret path. A cozy treehouse glows at the end of the path.",
            3,
        )

        self.assertEqual(len(sources), 3)
        self.assertIn("Bubbles shimmer", sources[0])
        self.assertIn("secret path", sources[1])
        self.assertIn("treehouse", sources[2])

    def test_narration_sources_fall_back_when_page_storybeat_is_meta_placeholder(self) -> None:
        sources = _build_page_narration_source_texts(
            [
                {
                    "scene_number": 1,
                    "storybeat_text": "Here is the Candyland you described.",
                    "scene_description": "A chocolate river curls past lollipop trees toward a candy castle.",
                }
            ],
            ["A chocolate river curls past lollipop trees toward a candy castle."],
            "",
            1,
        )

        self.assertEqual(len(sources), 1)
        self.assertIn("chocolate river", sources[0].lower())
        self.assertNotIn("you described", sources[0].lower())

    def test_storybook_studio_receives_story_context_from_live_session(self) -> None:
        captured_initial_state: dict[str, object] = {}

        async def fake_run_storybook_studio_workflow(*, session_id: str, initial_state: dict[str, object], max_revision_rounds: int = 1) -> dict[str, object]:
            self.assertEqual(session_id, "session-123")
            self.assertEqual(max_revision_rounds, 1)
            captured_initial_state.update(initial_state)
            return {"studio_narration_plan": {"narration_style": "one short sentence", "narration_lines": ["line one", "line two"]}}

        with mock.patch.object(_ASSEMBLE_MODULE, "run_storybook_studio_workflow", side_effect=fake_run_storybook_studio_workflow):
            with mock.patch.object(
                _ASSEMBLE_MODULE,
                "build_storybook_studio_plan_from_workflow_state",
                return_value={"status": "approved", "narration_lines": ["line one", "line two"]},
            ):
                result = asyncio.run(
                    _run_storybook_studio_async(
                        session_id="session-123",
                        title="Rainbow Adventure",
                        child_name="Aaron",
                        child_age=4,
                        story_summary="Aaron follows a rainbow path into the castle and finds a dragon.",
                        scene_descriptions=[
                            "A rainbow path glows toward the castle.",
                            "A friendly dragon naps under the moon.",
                        ],
                        story_pages=[
                            {
                                "scene_number": 1,
                                "label": "Rainbow path",
                                "storybeat_text": "The rainbow path curls toward the castle.",
                                "scene_description": "A rainbow path glows toward the castle.",
                            },
                            {
                                "scene_number": 2,
                                "label": "Moonlit hill",
                                "storybeat_text": "A friendly dragon naps under the moon.",
                                "scene_description": "A friendly dragon naps under the moon.",
                            },
                        ],
                        narration_source_texts=[
                            "The rainbow path curls toward the castle.",
                            "A friendly dragon naps under the moon.",
                        ],
                        child_delight_anchors_text="- Let's explore the rainbow path.\n- Can we pet a dragon?",
                        continuity_world_state_text="Current place: moonlit hill | Pending child wish: pet a dragon",
                        scene_count=2,
                        max_music_cues=2,
                        max_sfx_cues=1,
                        fallback_narration_lines=["line one", "line two"],
                    )
                )

        self.assertEqual(result["status"], "approved")
        self.assertIn("studio_story_pages_json", captured_initial_state)
        self.assertIn("Rainbow path", str(captured_initial_state["studio_story_pages_json"]))
        self.assertIn("pet a dragon", str(captured_initial_state["studio_child_delight_anchors_text"]))
        self.assertIn("moonlit hill", str(captured_initial_state["studio_continuity_world_state_text"]))
        self.assertIn("line one", str(captured_initial_state["studio_fallback_narration_json"]))

    def test_narration_segments_keep_one_line_per_scene(self) -> None:
        lines = _build_narration_segments(
            [
                "Bubbles shimmer around the trees.",
                "",
                "A cozy treehouse glows at the end of the path.",
            ],
            "Bubbles shimmer around the trees. The bubble leads to a secret path. A cozy treehouse glows at the end of the path.",
            3,
            child_age=4,
        )

        self.assertEqual(len(lines), 3)
        self.assertIn("Bubbles shimmer", lines[0])
        self.assertIn("secret path", lines[1].lower())
        self.assertIn("treehouse", lines[2].lower())

    def test_narration_segments_reject_prompt_labels_and_keep_story_copy(self) -> None:
        lines = _build_narration_segments(
            [
                (
                    "A swirling rainbow path glowing under a big spooky moon, leading towards a playful, slightly "
                    "creaky purple castle made of soft shadows. Friendly, silly ghost shapes peek out from behind "
                    "puffy purple trees. Make the destination unmistakable with these setting cues: show "
                    "unmistakable towers."
                ),
                (
                    "Inside Go inside the castle, outside Go inside the castle, outside the spooky rainbow castle "
                    "at night. A large, friendly dragon is curled up asleep on the grass under the glowing moon "
                    "and rainbow path. Keep these same characters in view: silly ghost."
                ),
            ],
            "",
            2,
            child_age=4,
        )

        self.assertEqual(len(lines), 2)
        self.assertIn("rainbow path", lines[0].lower())
        self.assertNotIn("go inside", lines[0].lower())
        self.assertNotIn("shiny under a big spooky", lines[0].lower())
        self.assertTrue(lines[0].endswith("."))
        self.assertIn("dragon", lines[1].lower())
        self.assertNotIn("go inside", lines[1].lower())
        self.assertTrue(lines[1].endswith("."))

    def test_narration_segments_repair_prompty_descriptions_into_complete_lines(self) -> None:
        lines = _build_narration_segments(
            [
                (
                    "A swirling rainbow path glowing under a big spooky moon, leading towards a playful, slightly "
                    "creaky purple castle made of soft shadows. Friendly, silly ghost shapes peek out from behind "
                    "puffy purple trees."
                ),
                (
                    "There are funny, smiling ghosts holding little flickering lanterns floating gently around a "
                    "big, plush, velvet armchair. Soft candlelight glows from the walls."
                ),
            ],
            "",
            2,
            child_age=4,
        )

        self.assertEqual(lines[0], "A swirling rainbow path is glowing under a big spooky moon.")
        self.assertEqual(lines[1], "Funny, smiling ghosts hold little flickering lanterns.")

    def test_narration_visual_grounding_flags_unseen_treehouse_detail(self) -> None:
        self.assertTrue(
            _narration_mentions_unsupported_detail(
                "A cozy treehouse waits at the end of the path.",
                source_line="A sparkly path winds through the moonlit woods.",
                scene_description="A sparkly path winds through the moonlit woods.",
                previous_scene_description="A dragon naps by the castle gate.",
                next_scene_description="Lanterns glow beside the path.",
            )
        )
        self.assertFalse(
            _narration_mentions_unsupported_detail(
                "A sparkly path winds through the moonlit woods.",
                source_line="A sparkly path winds through the moonlit woods.",
                scene_description="A sparkly path winds through the moonlit woods.",
                previous_scene_description="A dragon naps by the castle gate.",
                next_scene_description="Lanterns glow beside the path.",
            )
        )

    def test_storybook_motion_filter_uses_snapped_crop_motion_without_frame_eval(self) -> None:
        vf = _storybook_motion_vf(
            1,
            4.8,
        )

        self.assertIn("scale=w=", vf)
        self.assertIn("force_original_aspect_ratio=increase", vf)
        self.assertIn("crop=1280:720", vf)
        self.assertIn("floor(((iw-1280)", vf)
        self.assertIn("floor(((ih-720)", vf)
        self.assertIn("/2)*2", vf)
        self.assertNotIn("eval=frame", vf)
        self.assertIn("scale=w=1280:h=720:flags=lanczos", vf)

    def test_burned_caption_filtergraph_stays_in_bottom_caption_band(self) -> None:
        filters = _storybook_burned_caption_filtergraph(
            [(0.0, 4.0, "See the friendly dragon sleeping under the big moon!", 3)]
        )

        self.assertTrue(filters)
        self.assertIn("y=566", filters[0])
        self.assertIn("y=566", filters[1])
        self.assertIn("y=620", filters[2])

    def test_end_card_music_cue_is_added_when_missing(self) -> None:
        cues = _ensure_end_card_music_cue(
            {0: {"prompt": "opening cue"}},
            end_card_index=2,
            audio_scene_descriptions=[
                "Cover title card",
                "A dragon naps by the castle path.",
                "The End",
            ],
            scene_durations=[3.5, 4.0, 2.8],
            studio_music_arc_prompt="gentle magical ending music",
        )

        self.assertIn(2, cues)
        self.assertIn("gentle magical ending music", cues[2]["prompt"])
        self.assertGreaterEqual(float(cues[2]["duration_seconds"]), 2.0)

    @mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "eleven-123"}, clear=False)
    def test_short_music_cues_use_music_endpoint_not_sound_generation(self) -> None:
        client = mock.AsyncMock()
        response = mock.Mock()
        response.status_code = 200
        response.headers = {"content-type": "audio/mpeg"}
        response.content = b"music-bytes"
        client.post.return_value = response
        async_client = mock.AsyncMock()
        async_client.__aenter__.return_value = client
        async_client.__aexit__.return_value = False

        with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_MUSIC_DISABLED_REASON", None):
            with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_AUDIO_CONCURRENCY_SEMAPHORE", None):
                with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_AUDIO_CONCURRENCY_LIMIT", None):
                    with mock.patch.object(_ASSEMBLE_MODULE.httpx, "AsyncClient", return_value=async_client):
                        audio = asyncio.run(_elevenlabs_generate_music("gentle magical ending music", 4.0))

        self.assertEqual(audio, b"music-bytes")
        self.assertIn("/v1/music", client.post.call_args.args[0])
        self.assertNotIn("sound-generation", client.post.call_args.args[0])

    @mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "eleven-123"}, clear=False)
    def test_music_rate_limit_does_not_disable_sfx(self) -> None:
        music_client = mock.AsyncMock()
        music_response = mock.Mock()
        music_response.status_code = 429
        music_response.headers = {"content-type": "application/json"}
        music_response.text = '{"detail":{"code":"concurrent_limit_exceeded"}}'
        music_client.post.return_value = music_response
        music_async_client = mock.AsyncMock()
        music_async_client.__aenter__.return_value = music_client
        music_async_client.__aexit__.return_value = False

        sfx_client = mock.AsyncMock()
        sfx_response = mock.Mock()
        sfx_response.status_code = 200
        sfx_response.headers = {"content-type": "audio/mpeg"}
        sfx_response.content = b"sfx-bytes"
        sfx_client.post.return_value = sfx_response
        sfx_async_client = mock.AsyncMock()
        sfx_async_client.__aenter__.return_value = sfx_client
        sfx_async_client.__aexit__.return_value = False

        with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_MUSIC_DISABLED_REASON", None):
            with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_SFX_DISABLED_REASON", None):
                with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_AUDIO_CONCURRENCY_SEMAPHORE", None):
                    with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_AUDIO_CONCURRENCY_LIMIT", None):
                        with mock.patch.object(
                            _ASSEMBLE_MODULE.httpx,
                            "AsyncClient",
                            side_effect=[music_async_client, sfx_async_client],
                        ):
                            music_audio = asyncio.run(_elevenlabs_generate_music("gentle magical ending music", 4.0))
                            sfx_audio = asyncio.run(_elevenlabs_generate_sfx("sparkly pop", 1.4))

        self.assertIsNone(music_audio)
        self.assertEqual(sfx_audio, b"sfx-bytes")
        self.assertIn("/v1/music", music_client.post.call_args.args[0])
        self.assertIn("/v1/sound-generation", sfx_client.post.call_args.args[0])

    @mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "eleven-123"}, clear=False)
    def test_elevenlabs_audio_requests_share_concurrency_limit(self) -> None:
        in_flight = 0
        max_in_flight = 0

        class _FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *_args, **_kwargs):
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.01)
                in_flight -= 1
                response = mock.Mock()
                response.status_code = 200
                response.headers = {"content-type": "audio/mpeg"}
                response.content = b"audio"
                return response

        async def _run_calls() -> list[bytes | None]:
            return await asyncio.gather(
                _elevenlabs_generate_music("gentle magical ending music", 4.0),
                _elevenlabs_generate_sfx("sparkly pop", 1.4),
                _elevenlabs_generate_music("soft ending music", 4.0),
            )

        with mock.patch.dict("os.environ", {"STORYBOOK_ELEVENLABS_AUDIO_CONCURRENCY": "2"}, clear=False):
            with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_MUSIC_DISABLED_REASON", None):
                with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_SFX_DISABLED_REASON", None):
                    with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_AUDIO_CONCURRENCY_SEMAPHORE", None):
                        with mock.patch.object(_ASSEMBLE_MODULE, "_ELEVENLABS_AUDIO_CONCURRENCY_LIMIT", None):
                            with mock.patch.object(_ASSEMBLE_MODULE.httpx, "AsyncClient", return_value=_FakeAsyncClient()):
                                results = asyncio.run(_run_calls())

        self.assertEqual(results, [b"audio", b"audio", b"audio"])
        self.assertLessEqual(max_in_flight, 2)

    def test_storybook_music_auto_skips_lyria_when_not_enabled(self) -> None:
        async def _run() -> tuple[bytes, str] | None:
            with mock.patch.object(_ASSEMBLE_MODULE, "_elevenlabs_generate_music", return_value=None):
                with mock.patch.object(_ASSEMBLE_MODULE, "_lyria_generate_music", side_effect=AssertionError("lyria should not run")):
                    return await _generate_storybook_music_bytes("gentle magical ending music", 4.0, seed=7)

        with mock.patch.dict("os.environ", {"STORYBOOK_MUSIC_PROVIDER": "auto"}, clear=False):
            result = asyncio.run(_run())

        self.assertIsNone(result)

    def test_storybook_music_auto_uses_lyria_when_explicitly_enabled(self) -> None:
        async def _run() -> tuple[bytes, str] | None:
            with mock.patch.object(_ASSEMBLE_MODULE, "_elevenlabs_generate_music", return_value=None):
                with mock.patch.object(_ASSEMBLE_MODULE, "_lyria_generate_music", return_value=(b"lyria-audio", ".wav")) as mock_lyria:
                    result = await _generate_storybook_music_bytes("gentle magical ending music", 4.0, seed=7)
                    self.assertEqual(mock_lyria.call_count, 1)
                    return result

        with mock.patch.dict(
            "os.environ",
            {"STORYBOOK_MUSIC_PROVIDER": "auto", "ENABLE_STORYBOOK_LYRIA_MUSIC": "1"},
            clear=False,
        ):
            result = asyncio.run(_run())

        self.assertEqual(result, (b"lyria-audio", ".wav"))

    def test_explicit_lyria_provider_respects_enable_flag(self) -> None:
        async def _run() -> tuple[bytes, str] | None:
            with mock.patch.object(_ASSEMBLE_MODULE, "_lyria_generate_music", side_effect=AssertionError("lyria should stay disabled")):
                return await _generate_storybook_music_bytes("gentle magical ending music", 4.0, seed=7)

        with mock.patch.dict("os.environ", {"STORYBOOK_MUSIC_PROVIDER": "lyria"}, clear=False):
            result = asyncio.run(_run())

        self.assertIsNone(result)

    def test_theater_lighting_cues_follow_scene_timeline_and_live_scene_colors(self) -> None:
        cues = _build_storybook_theater_lighting_cues(
            scene_durations=[2.0, 3.5],
            story_pages=[
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "scene_description": "A leafy forest path glows in the morning.",
                },
                {
                    "scene_number": 2,
                    "request_id": "scene-2",
                    "scene_description": "A moonlit castle hallway sparkles with bubbles.",
                },
            ],
            scene_descriptions=[
                "A leafy forest path glows in the morning.",
                "A moonlit castle hallway sparkles with bubbles.",
            ],
            scene_lighting_cues=[
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "hex_color": "#55C26A",
                    "brightness": 176,
                    "transition": 1.0,
                },
                {
                    "scene_number": 2,
                    "request_id": "scene-2",
                    "hex_color": "#6FA8FF",
                    "brightness": 154,
                    "transition": 1.2,
                },
            ],
        )

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["start_seconds"], 0.0)
        self.assertEqual(cues[0]["end_seconds"], 2.0)
        self.assertEqual(cues[0]["hex_color"], "#55C26A")
        self.assertEqual(cues[0]["cue_source"], "live_story_scene")
        self.assertEqual(cues[1]["start_seconds"], 2.0)
        self.assertEqual(cues[1]["end_seconds"], 5.5)
        self.assertEqual(cues[1]["hex_color"], "#6FA8FF")

    def test_theater_lighting_cues_prefer_page_metadata_when_available(self) -> None:
        cues = _build_storybook_theater_lighting_cues(
            scene_durations=[3.0],
            story_pages=[
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "scene_description": "A dreamy bubble garden twinkles at dusk.",
                    "hex_color": "#B89CFF",
                    "rgb_color": [184, 156, 255],
                    "brightness": 166,
                    "transition": 0.9,
                    "cue_source": "heuristic_scene",
                }
            ],
            scene_descriptions=["A dreamy bubble garden twinkles at dusk."],
            scene_lighting_cues=[
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "hex_color": "#55C26A",
                    "rgb_color": [85, 194, 106],
                    "brightness": 176,
                    "transition": 1.3,
                    "cue_source": "live_story_scene",
                }
            ],
        )

        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["hex_color"], "#B89CFF")
        self.assertEqual(cues[0]["rgb_color"], [184, 156, 255])
        self.assertEqual(cues[0]["cue_source"], "heuristic_scene")

    def test_theater_lighting_cues_keep_scene_boundaries_when_effect_changes(self) -> None:
        cues = _build_storybook_theater_lighting_cues(
            scene_durations=[2.2, 2.8],
            story_pages=[
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "scene_description": "A moonlit castle hallway glows softly.",
                    "hex_color": "#6FA8FF",
                    "rgb_color": [111, 168, 255],
                    "brightness": 154,
                    "transition": 1.1,
                    "cue_source": "page_metadata",
                },
                {
                    "scene_number": 2,
                    "request_id": "scene-2",
                    "scene_description": "Lightning flashes through the same moonlit castle hallway.",
                    "hex_color": "#6FA8FF",
                    "rgb_color": [111, 168, 255],
                    "brightness": 154,
                    "transition": 1.1,
                    "cue_source": "page_metadata",
                },
            ],
            scene_descriptions=[
                "A moonlit castle hallway glows softly.",
                "Lightning flashes through the same moonlit castle hallway.",
            ],
        )

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0]["effect"], "steady")
        self.assertEqual(cues[1]["effect"], "flash")
        self.assertEqual(cues[1]["start_seconds"], 2.2)
        self.assertEqual(cues[1]["end_seconds"], 5.0)

    def test_narration_segments_do_not_ship_duplicate_words_or_dangling_but(self) -> None:
        lines = _build_narration_segments(
            [
                (
                    "A swirling rainbow path glowing under a big spooky moon, leading towards a playful, slightly "
                    "creaky purple castle made of soft shadows. Friendly, silly ghost shapes peek out from behind "
                    "puffy purple trees. The path is brightly lit with many colors but the surroundings are dark "
                    "and misty."
                )
            ],
            "",
            1,
            child_age=4,
        )

        self.assertEqual(len(lines), 1)
        self.assertFalse(story_sentence_needs_revision(lines[0]))
        self.assertNotIn(" is is ", lines[0].lower())
        self.assertFalse(lines[0].lower().endswith(" but."))

    def test_public_story_video_url_appends_cache_token(self) -> None:
        self.assertEqual(
            _build_public_story_video_url("session-123", "987654"),
            f"https://storage.googleapis.com/{_GCS_FINAL_BUCKET}/session-123/story_final.mp4?v=987654",
        )

    def test_short_end_card_line_stays_clean(self) -> None:
        self.assertEqual(_simplify_for_readalong("The end.", max_words=6), "The end.")

    def test_quiet_audio_mix_triggers_recovery_gain(self) -> None:
        self.assertGreaterEqual(
            _recommended_audio_boost_gain_db(mean_volume_db=-51.5, max_volume_db=-24.4),
            20.0,
        )
        self.assertEqual(
            _recommended_audio_boost_gain_db(mean_volume_db=-21.0, max_volume_db=-4.0),
            0.0,
        )

    def test_ffmpeg_nonsilent_seconds_measures_spoken_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            audio_path = tmp / "coverage.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=880:duration=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=660:duration=1",
                    "-filter_complex",
                    "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
                    "-c:a",
                    "pcm_s16le",
                    str(audio_path),
                ],
                check=True,
                capture_output=True,
            )

            nonsilent_seconds = _ffmpeg_nonsilent_seconds(audio_path)

        self.assertIsNotNone(nonsilent_seconds)
        self.assertGreater(float(nonsilent_seconds or 0.0), 1.7)
        self.assertLess(float(nonsilent_seconds or 0.0), 2.3)

    def test_storybook_padded_audio_tracks_use_distinct_temp_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            narration_src = tmp / "narration_src.wav"
            music_src = tmp / "music_src.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=0.8",
                    "-c:a",
                    "pcm_s16le",
                    str(narration_src),
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=880:duration=0.5",
                    "-c:a",
                    "pcm_s16le",
                    str(music_src),
                ],
                check=True,
                capture_output=True,
            )

            narration_track = _build_storybook_padded_audio_track(
                tmp=tmp,
                audio_paths=[narration_src],
                durations=[1.2],
                basename="narration_track",
            )
            music_track = _build_storybook_padded_audio_track(
                tmp=tmp,
                audio_paths=[music_src],
                durations=[1.2],
                basename="music_track",
                fade_in=0.1,
                fade_out=0.1,
            )

            self.assertIsNotNone(narration_track)
            self.assertIsNotNone(music_track)
            self.assertNotEqual(narration_track, music_track)
            self.assertTrue(narration_track.exists())
            self.assertTrue(music_track.exists())
            self.assertAlmostEqual(_ffprobe_duration(narration_track), 1.2, delta=0.1)
            self.assertAlmostEqual(_ffprobe_duration(music_track), 1.2, delta=0.1)
            self.assertGreater(float(_ffmpeg_nonsilent_seconds(narration_track) or 0.0), 0.7)
            self.assertGreater(float(_ffmpeg_nonsilent_seconds(music_track) or 0.0), 0.4)

    def test_exact_storybook_word_starts_preserve_alignment_when_available(self) -> None:
        word_starts_ms, timing_mode = _resolve_storybook_word_starts_ms(
            "Glow softly now.",
            1.2,
            word_timings=[
                ("Glow", 0.0, 0.32),
                ("softly", 0.32, 0.81),
                ("now.", 0.81, 1.2),
            ],
        )

        self.assertEqual(timing_mode, "exact")
        self.assertEqual(word_starts_ms, [0, 320, 810])

    def test_movie_readalong_segments_follow_scene_timeline(self) -> None:
        segments = _build_storybook_movie_readalong_segments(
            scene_durations=[3.0, 4.0, 5.0, 2.4],
            narration_lines=[
                "Our story begins.",
                "We follow the rainbow path.",
                "Friendly ghosts wave hello.",
                "And that is the happy end of our story.",
            ],
            narration_audio_durations=[1.0, 1.6, 1.8, 1.0],
            narration_word_starts_ms=[
                [0, 420, 760],
                [0, 260, 520, 780, 1120],
                [0, 300, 620, 940],
                [0, 220, 480, 760, 1040, 1280, 1540, 1840, 2160],
            ],
            narration_timing_modes=["estimated", "exact", "exact", "estimated"],
            story_pages=[
                {"storybeat_text": "We follow the rainbow path."},
                {"storybeat_text": "Friendly ghosts wave hello."},
            ],
            cover_offset=1,
            end_card_index=3,
        )

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["page_number"], 1)
        self.assertEqual(segments[0]["start_ms"], 3000)
        self.assertEqual(segments[0]["end_ms"], 7000)
        self.assertEqual(segments[0]["timing_mode"], "exact")
        self.assertEqual(segments[0]["word_starts_ms"], [0, 260, 520, 780, 1120])
        self.assertEqual(segments[1]["page_number"], 2)
        self.assertEqual(segments[1]["start_ms"], 7000)
        self.assertEqual(segments[1]["end_ms"], 12000)
        self.assertEqual(segments[1]["word_starts_ms"], [0, 300, 620, 940])

    def test_storybook_readalong_ass_text_highlights_current_word(self) -> None:
        ass_text = _build_storybook_readalong_ass_text(
            [
                {
                    "page_number": 1,
                    "text": "We follow the rainbow path.",
                    "start_ms": 3000,
                    "end_ms": 7000,
                    "audio_duration_ms": 1600,
                    "word_starts_ms": [0, 260, 520, 780, 1120],
                    "timing_mode": "exact",
                }
            ]
        )

        self.assertIn("Style: ReadAlong,Fredoka,27", ass_text)
        self.assertIn("Dialogue: 0,0:00:03.00,0:00:03.26,ReadAlong", ass_text)
        self.assertIn(r"{\1c&H00321C0F&\3c&H00C7E8FF&\bord2\shad0\b1}We{\rReadAlong}", ass_text)
        self.assertIn("rainbow", ass_text)


if __name__ == "__main__":
    unittest.main()
