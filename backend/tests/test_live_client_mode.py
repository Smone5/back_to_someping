from __future__ import annotations

import unittest

from backend.live_client_mode import (
    DEFAULT_LIVE_MODEL,
    DEFAULT_LIVE_TEMPERATURE,
    DEFAULT_LIVE_TOP_P,
    DEFAULT_LIVE_VOICE,
    build_live_ephemeral_token_config,
    clamp_live_temperature,
    clamp_live_top_p,
)


class LiveClientModeTests(unittest.TestCase):
    def test_clamp_live_temperature_uses_safe_bounds(self) -> None:
        self.assertEqual(clamp_live_temperature(None), DEFAULT_LIVE_TEMPERATURE)
        self.assertEqual(clamp_live_temperature("bad"), DEFAULT_LIVE_TEMPERATURE)
        self.assertEqual(clamp_live_temperature(-1), 0.1)
        self.assertEqual(clamp_live_temperature(9), 1.5)

    def test_clamp_live_top_p_uses_safe_bounds(self) -> None:
        self.assertEqual(clamp_live_top_p(None), DEFAULT_LIVE_TOP_P)
        self.assertEqual(clamp_live_top_p("bad"), DEFAULT_LIVE_TOP_P)
        self.assertEqual(clamp_live_top_p(-1), 0.1)
        self.assertEqual(clamp_live_top_p(9), 1.0)

    def test_build_live_ephemeral_token_config_enables_resumption_and_audio(self) -> None:
        config = build_live_ephemeral_token_config(model="")
        self.assertEqual(config["uses"], 1)
        self.assertEqual(config["http_options"]["api_version"], "v1alpha")
        self.assertEqual(config["live_connect_constraints"]["model"], DEFAULT_LIVE_MODEL)
        live_config = config["live_connect_constraints"]["config"]
        self.assertEqual(live_config["response_modalities"], ["AUDIO"])
        self.assertEqual(live_config["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"], DEFAULT_LIVE_VOICE)
        self.assertEqual(live_config["temperature"], DEFAULT_LIVE_TEMPERATURE)
        self.assertEqual(live_config["top_p"], DEFAULT_LIVE_TOP_P)
        self.assertEqual(live_config["session_resumption"], {})
        self.assertEqual(live_config["input_audio_transcription"], {})
        self.assertEqual(live_config["output_audio_transcription"], {})
        self.assertTrue(
            live_config["realtime_input_config"]["automatic_activity_detection"]["disabled"]
        )

    def test_build_live_ephemeral_token_config_clamps_expiration_and_session_window(self) -> None:
        config = build_live_ephemeral_token_config(
            model="gemini-custom-live",
            temperature=10,
            top_p=-2,
            voice_name="Puck",
            expire_minutes=2,
            new_session_minutes=99,
        )
        self.assertEqual(config["live_connect_constraints"]["model"], "gemini-custom-live")
        live_config = config["live_connect_constraints"]["config"]
        self.assertEqual(live_config["temperature"], 1.5)
        self.assertEqual(live_config["top_p"], 0.1)
        self.assertEqual(
            live_config["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"],
            "Puck",
        )
        self.assertGreaterEqual(config["expire_time"], config["new_session_expire_time"])


if __name__ == "__main__":
    unittest.main()
