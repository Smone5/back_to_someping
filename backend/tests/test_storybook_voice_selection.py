from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend import ws_router
from backend.schemas import ClientCommand, ClientCommandType


class StorybookVoiceSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_story_persists_selected_story_reader_voice_before_assembly(self) -> None:
        cmd = ClientCommand(
            type=ClientCommandType.END_STORY,
            session_id="session-123",
            payload={"storybook_elevenlabs_voice_id": "S9EY1FKT0mCZ06GOW6V4"},
        )
        runner = mock.Mock()
        live_queue = mock.Mock()
        websocket = mock.AsyncMock()

        with (
            mock.patch(
                "backend.ws_router._persist_storybook_elevenlabs_voice_id",
                new=mock.AsyncMock(),
            ) as persist_voice,
            mock.patch(
                "backend.ws_router._trigger_story_end",
                new=mock.AsyncMock(),
            ) as trigger_story_end,
        ):
            await ws_router._handle_command(
                cmd=cmd,
                websocket=websocket,
                session_id=cmd.session_id,
                user_id="anonymous",
                runner=runner,
                live_queue=live_queue,
            )

        persist_voice.assert_awaited_once_with(
            runner=runner,
            user_id="anonymous",
            session_id="session-123",
            voice_id="S9EY1FKT0mCZ06GOW6V4",
        )
        trigger_story_end.assert_awaited_once()

    def test_payload_storybook_elevenlabs_voice_id_accepts_snake_and_camel_case(self) -> None:
        self.assertEqual(
            ws_router._payload_storybook_elevenlabs_voice_id(
                {"storybook_elevenlabs_voice_id": "S9EY1FKT0mCZ06GOW6V4"}
            ),
            "S9EY1FKT0mCZ06GOW6V4",
        )
        self.assertEqual(
            ws_router._payload_storybook_elevenlabs_voice_id(
                {"storybookElevenlabsVoiceId": "4u5cJuSmHP9d6YRolsOu"}
            ),
            "4u5cJuSmHP9d6YRolsOu",
        )
        self.assertIsNone(
            ws_router._payload_storybook_elevenlabs_voice_id(
                {"storybook_elevenlabs_voice_id": "bad"}
            )
        )


if __name__ == "__main__":
    unittest.main()
