from __future__ import annotations

import base64
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend import main
from shared.storybook_page_read_aloud_workflow import (
    PageReadAloudRequest,
    run_storybook_page_read_aloud_workflow,
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict[str, object]:
        return self._payload


class PageReadAloudTests(unittest.TestCase):
    def test_request_body_uses_cloud_tts_model_and_ssml_marks(self) -> None:
        payload = main._page_read_aloud_request_body(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
        )

        self.assertNotIn("text", payload["input"])
        self.assertIn("<speak>", payload["input"]["ssml"])
        self.assertIn('<mark name="w0"/>The', payload["input"]["ssml"])
        self.assertIn('rate="90%"', payload["input"]["ssml"])
        self.assertEqual(payload["voice"]["modelName"], main.settings.page_read_aloud_model)
        self.assertEqual(payload["voice"]["name"], main.settings.page_read_aloud_voice)
        self.assertEqual(payload["voice"]["languageCode"], main.settings.page_read_aloud_language_code)
        self.assertEqual(payload["audioConfig"]["audioEncoding"], "MP3")
        self.assertEqual(payload["enableTimePointing"], ["SSML_MARK"])

    def test_request_body_uses_plain_text_when_ssml_marks_are_disabled(self) -> None:
        payload = main._page_read_aloud_request_body(
            " The elves are making toys. ",
            child_age=4,
            storybook_movie_pacing="read_with_me",
            use_ssml_marks=False,
        )

        self.assertNotIn("ssml", payload["input"])
        self.assertEqual(payload["input"]["text"], "The elves are making toys.")
        self.assertNotIn("enableTimePointing", payload)
        self.assertEqual(payload["audioConfig"]["audioEncoding"], "MP3")
        self.assertEqual(payload["audioConfig"]["speakingRate"], 0.9)

    def test_page_read_aloud_cache_key_is_stable(self) -> None:
        key_a = main._page_read_aloud_cache_key(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
            elevenlabs_voice_id="21m00Tcm4TlvDq8ikWAM",
        )
        key_b = main._page_read_aloud_cache_key(
            " The elves are making toys. ",
            child_age=4,
            storybook_movie_pacing="READ_WITH_ME",
            elevenlabs_voice_id="21m00Tcm4TlvDq8ikWAM",
        )
        key_c = main._page_read_aloud_cache_key(
            "The elves are making toys.",
            child_age=7,
            storybook_movie_pacing="read_with_me",
            elevenlabs_voice_id="21m00Tcm4TlvDq8ikWAM",
        )
        key_d = main._page_read_aloud_cache_key(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
            elevenlabs_voice_id="EXAVITQu4vr4xnSDxMaL",
        )

        self.assertEqual(key_a, key_b)
        self.assertNotEqual(key_a, key_c)
        self.assertNotEqual(key_a, key_d)

    @mock.patch("backend.main.httpx.post")
    @mock.patch("backend.main._page_read_aloud_token", return_value="token-123")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "google-cloud-tts")
    def test_synthesize_page_read_aloud_decodes_audio_content(
        self,
        _mock_token: mock.Mock,
        mock_post: mock.Mock,
    ) -> None:
        audio_bytes = b"fake-mp3-bytes"
        mock_post.return_value = _FakeResponse(
            200,
            payload={
                "audioContent": base64.b64encode(audio_bytes).decode("ascii"),
                "timepoints": [
                    {"markName": "w0", "timeSeconds": 0.11},
                    {"markName": "w1", "timeSeconds": 0.28},
                    {"markName": "w2", "timeSeconds": 0.44},
                    {"markName": "w3", "timeSeconds": 0.62},
                    {"markName": "w4", "timeSeconds": 0.79},
                ],
            },
        )

        rendered_bytes, media_type, word_starts_ms, provider, timing_mode = main._synthesize_page_read_aloud_audio(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
        )

        self.assertEqual(rendered_bytes, audio_bytes)
        self.assertEqual(media_type, "audio/mpeg")
        self.assertEqual(word_starts_ms, [110, 280, 440, 620, 790])
        self.assertEqual(provider, "google-cloud-tts")
        self.assertEqual(timing_mode, "ssml_marks")
        self.assertTrue(mock_post.called)

    @mock.patch("backend.main.httpx.post")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "elevenlabs")
    @mock.patch.object(main.settings, "elevenlabs_api_key", "eleven-123")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_voice_id", "voice-abc")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_model", "eleven_flash_v2_5")
    def test_synthesize_page_read_aloud_prefers_elevenlabs_exact_word_timestamps(
        self,
        mock_post: mock.Mock,
    ) -> None:
        audio_bytes = b"elevenlabs-mp3-bytes"
        mock_post.return_value = _FakeResponse(
            200,
            payload={
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "alignment": {
                    "characters": list("The elves are making toys."),
                    "character_start_times_seconds": [
                        0.00, 0.06, 0.11, 0.18,
                        0.25, 0.31, 0.37, 0.43, 0.49,
                        0.57, 0.63, 0.68, 0.72,
                        0.80, 0.86, 0.92, 0.98, 1.04, 1.10, 1.16,
                        1.24, 1.30, 1.36, 1.42, 1.48, 1.54,
                    ],
                    "character_end_times_seconds": [
                        0.05, 0.10, 0.17, 0.24,
                        0.30, 0.36, 0.42, 0.48, 0.56,
                        0.62, 0.67, 0.71, 0.78,
                        0.85, 0.91, 0.97, 1.03, 1.09, 1.15, 1.23,
                        1.29, 1.35, 1.41, 1.47, 1.53, 1.61,
                    ],
                },
            },
        )

        rendered_bytes, media_type, word_starts_ms, provider, timing_mode = main._synthesize_page_read_aloud_audio(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
        )

        self.assertEqual(rendered_bytes, audio_bytes)
        self.assertEqual(media_type, "audio/mpeg")
        self.assertEqual(word_starts_ms, [0, 250, 630, 860, 1300])
        self.assertEqual(provider, "elevenlabs")
        self.assertEqual(timing_mode, "exact")
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn("/with-timestamps", mock_post.call_args.args[0])

    @mock.patch("backend.main.httpx.post")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "elevenlabs")
    @mock.patch.object(main.settings, "elevenlabs_api_key", "eleven-123")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_voice_id", "voice-default")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_model", "eleven_flash_v2_5")
    def test_synthesize_page_read_aloud_uses_selected_elevenlabs_voice_override(
        self,
        mock_post: mock.Mock,
    ) -> None:
        audio_bytes = b"voice-override-mp3"
        mock_post.return_value = _FakeResponse(
            200,
            payload={
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "alignment": {
                    "characters": list("The elves are making toys."),
                    "character_start_times_seconds": [0.0] * 26,
                    "character_end_times_seconds": [0.1] * 26,
                },
            },
        )

        main._synthesize_page_read_aloud_audio(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
            elevenlabs_voice_id="EXAVITQu4vr4xnSDxMaL",
        )

        self.assertIn(
            "/text-to-speech/EXAVITQu4vr4xnSDxMaL/with-timestamps",
            mock_post.call_args.args[0],
        )

    @mock.patch("backend.main.httpx.post")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "elevenlabs")
    @mock.patch.object(main.settings, "elevenlabs_api_key", "eleven-123")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_voice_id", "EXAVITQu4vr4xnSDxMaL")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_model", "eleven_flash_v2_5")
    def test_synthesize_page_read_aloud_retries_with_default_elevenlabs_voice_when_selected_voice_is_missing(
        self,
        mock_post: mock.Mock,
    ) -> None:
        audio_bytes = b"fallback-elevenlabs-mp3"
        mock_post.side_effect = [
            _FakeResponse(
                404,
                payload={
                    "detail": {
                        "type": "not_found",
                        "code": "voice_not_found",
                        "message": "A voice with voice_id 'S9EY1FKT0mCZ06GOW6V4' was not found.",
                    }
                },
            ),
            _FakeResponse(
                200,
                payload={
                    "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                    "alignment": {
                        "characters": list("The elves are making toys."),
                        "character_start_times_seconds": [0.0] * 26,
                        "character_end_times_seconds": [0.1] * 26,
                    },
                },
            ),
        ]

        rendered_bytes, media_type, word_starts_ms, provider, timing_mode = main._synthesize_page_read_aloud_audio(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
            elevenlabs_voice_id="S9EY1FKT0mCZ06GOW6V4",
        )

        self.assertEqual(rendered_bytes, audio_bytes)
        self.assertEqual(media_type, "audio/mpeg")
        self.assertEqual(provider, "elevenlabs")
        self.assertEqual(timing_mode, "exact")
        self.assertGreaterEqual(len(word_starts_ms), 1)
        self.assertIn(
            "/text-to-speech/S9EY1FKT0mCZ06GOW6V4/with-timestamps",
            mock_post.call_args_list[0].args[0],
        )
        self.assertIn(
            "/text-to-speech/EXAVITQu4vr4xnSDxMaL/with-timestamps",
            mock_post.call_args_list[1].args[0],
        )

    @mock.patch("backend.main.httpx.post")
    @mock.patch("backend.main._page_read_aloud_token", return_value="token-123")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "google-cloud-tts")
    def test_synthesize_page_read_aloud_retries_with_plain_text_when_ssml_is_unsupported(
        self,
        _mock_token: mock.Mock,
        mock_post: mock.Mock,
    ) -> None:
        audio_bytes = b"fallback-mp3-bytes"
        mock_post.side_effect = [
            _FakeResponse(
                400,
                payload={
                    "error": {
                        "message": "This voice does not support SSML input. Please try again with text only input.",
                    }
                },
            ),
            _FakeResponse(
                200,
                payload={
                    "audioContent": base64.b64encode(audio_bytes).decode("ascii"),
                },
            ),
        ]

        rendered_bytes, media_type, word_starts_ms, provider, timing_mode = main._synthesize_page_read_aloud_audio(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
        )

        self.assertEqual(rendered_bytes, audio_bytes)
        self.assertEqual(media_type, "audio/mpeg")
        self.assertEqual(word_starts_ms, [])
        self.assertEqual(provider, "google-cloud-tts")
        self.assertEqual(timing_mode, "plain_text")
        self.assertEqual(mock_post.call_count, 2)

        first_payload = mock_post.call_args_list[0].kwargs["json"]
        second_payload = mock_post.call_args_list[1].kwargs["json"]
        self.assertIn("ssml", first_payload["input"])
        self.assertNotIn("text", first_payload["input"])
        self.assertEqual(first_payload["enableTimePointing"], ["SSML_MARK"])
        self.assertIn("text", second_payload["input"])
        self.assertNotIn("ssml", second_payload["input"])
        self.assertNotIn("enableTimePointing", second_payload)
        self.assertEqual(second_payload["audioConfig"]["speakingRate"], 0.9)

    @mock.patch("backend.main.httpx.post")
    @mock.patch("backend.main._page_read_aloud_token", return_value="token-123")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "google-cloud-tts")
    def test_synthesize_page_read_aloud_raises_for_http_error(
        self,
        _mock_token: mock.Mock,
        mock_post: mock.Mock,
    ) -> None:
        mock_post.return_value = _FakeResponse(
            403,
            payload={"error": {"message": "API disabled"}},
            text="API disabled",
        )

        with self.assertRaises(RuntimeError) as exc:
            main._synthesize_page_read_aloud_audio(
                "The elves are making toys.",
                child_age=4,
                storybook_movie_pacing="read_with_me",
            )

        self.assertIn("Cloud TTS request failed", str(exc.exception))

    @mock.patch("backend.main.httpx.post")
    @mock.patch("backend.main._page_read_aloud_token", return_value="token-123")
    @mock.patch.object(main.settings, "page_read_aloud_provider", "auto")
    @mock.patch.object(main.settings, "elevenlabs_api_key", "eleven-123")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_voice_id", "voice-abc")
    @mock.patch.object(main.settings, "page_read_aloud_elevenlabs_model", "eleven_flash_v2_5")
    def test_synthesize_page_read_aloud_falls_back_to_google_when_elevenlabs_timestamps_are_unusable(
        self,
        _mock_token: mock.Mock,
        mock_post: mock.Mock,
    ) -> None:
        audio_bytes = b"fallback-google-mp3"

        def side_effect(url: str, **kwargs: object) -> _FakeResponse:
            if "with-timestamps" in url:
                return _FakeResponse(
                    200,
                    payload={
                        "audio_base64": base64.b64encode(b"elevenlabs-audio").decode("ascii"),
                        "alignment": {
                            "characters": list("bad data"),
                            "character_start_times_seconds": [0.0] * 8,
                            "character_end_times_seconds": [0.1] * 8,
                        },
                    },
                )
            return _FakeResponse(
                200,
                payload={
                    "audioContent": base64.b64encode(audio_bytes).decode("ascii"),
                    "timepoints": [
                        {"markName": "w0", "timeSeconds": 0.11},
                        {"markName": "w1", "timeSeconds": 0.28},
                        {"markName": "w2", "timeSeconds": 0.44},
                        {"markName": "w3", "timeSeconds": 0.62},
                        {"markName": "w4", "timeSeconds": 0.79},
                    ],
                },
            )

        mock_post.side_effect = side_effect

        rendered_bytes, media_type, word_starts_ms, provider, timing_mode = main._synthesize_page_read_aloud_audio(
            "The elves are making toys.",
            child_age=4,
            storybook_movie_pacing="read_with_me",
        )

        self.assertEqual(rendered_bytes, audio_bytes)
        self.assertEqual(media_type, "audio/mpeg")
        self.assertEqual(word_starts_ms, [110, 280, 440, 620, 790])
        self.assertEqual(provider, "google-cloud-tts")
        self.assertEqual(timing_mode, "ssml_marks")
        self.assertEqual(mock_post.call_count, 2)

    def test_page_read_aloud_workflow_uses_adk_contract_for_cache_miss_and_hit(self) -> None:
        cache: dict[str, tuple[bytes, str, tuple[int, ...], str, str]] = {}
        synth_calls: list[tuple[str, int | None, str | None, str | None]] = []

        def cache_key_builder(text: str, child_age: int | None, pacing: str | None, voice_id: str | None) -> str:
            return f"{text}|{child_age}|{pacing}|{voice_id}"

        def cache_lookup(cache_key: str) -> tuple[bytes, str, tuple[int, ...], str, str] | None:
            return cache.get(cache_key)

        def cache_store(
            cache_key: str,
            audio_bytes: bytes,
            media_type: str,
            word_starts_ms: list[int],
            provider: str,
            timing_mode: str,
        ) -> None:
            cache[cache_key] = (audio_bytes, media_type, tuple(word_starts_ms), provider, timing_mode)

        def synthesize_audio(
            text: str,
            child_age: int | None,
            storybook_movie_pacing: str | None,
            elevenlabs_voice_id: str | None,
        ) -> tuple[bytes, str, list[int], str, str]:
            synth_calls.append((text, child_age, storybook_movie_pacing, elevenlabs_voice_id))
            return b"workflow-audio", "audio/mpeg", [0, 220, 510], "elevenlabs", "exact"

        request = PageReadAloudRequest(
            text="The elves are making toys.",
            session_id="session-123",
            child_age=4,
            storybook_movie_pacing="read_with_me",
            elevenlabs_voice_id="EXAVITQu4vr4xnSDxMaL",
            provider="auto",
            model_name=main.settings.page_read_aloud_model,
            voice_name="EXAVITQu4vr4xnSDxMaL",
            language_code=main.settings.page_read_aloud_language_code,
        )

        first_state = asyncio.run(
            run_storybook_page_read_aloud_workflow(
                request=request,
                cache_key_builder=cache_key_builder,
                cache_lookup=cache_lookup,
                cache_store=cache_store,
                synthesize_audio=synthesize_audio,
            )
        )
        second_state = asyncio.run(
            run_storybook_page_read_aloud_workflow(
                request=request,
                cache_key_builder=cache_key_builder,
                cache_lookup=cache_lookup,
                cache_store=cache_store,
                synthesize_audio=synthesize_audio,
            )
        )

        first_result = first_state["page_read_aloud_result_structured"]
        second_result = second_state["page_read_aloud_result_structured"]

        self.assertEqual(first_state["page_read_aloud_plan_structured"]["voice_name"], "EXAVITQu4vr4xnSDxMaL")
        self.assertFalse(first_result["cache_hit"])
        self.assertTrue(second_result["cache_hit"])
        self.assertEqual(first_result["provider"], "elevenlabs")
        self.assertEqual(first_result["timing_mode"], "exact")
        self.assertEqual(base64.b64decode(first_result["audio_base64"]), b"workflow-audio")
        self.assertEqual(second_result["word_starts_ms"], [0, 220, 510])
        self.assertEqual(synth_calls[0][3], "EXAVITQu4vr4xnSDxMaL")
        self.assertEqual(len(synth_calls), 1)


if __name__ == "__main__":
    unittest.main()
