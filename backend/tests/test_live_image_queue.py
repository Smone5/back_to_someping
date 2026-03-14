from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    import google  # type: ignore
except Exception:  # pragma: no cover - only for stripped local test envs
    google = types.ModuleType("google")
    sys.modules["google"] = google

try:
    import google.cloud as google_cloud  # type: ignore
except Exception:  # pragma: no cover - only for stripped local test envs
    google_cloud = types.ModuleType("google.cloud")
    sys.modules["google.cloud"] = google_cloud
    setattr(google, "cloud", google_cloud)

try:
    import google.adk as google_adk  # type: ignore
except Exception:  # pragma: no cover - only for stripped local test envs
    google_adk = None

if "google.cloud.firestore" not in sys.modules:
    firestore_stub = types.ModuleType("google.cloud.firestore")
    firestore_stub.Client = object
    firestore_stub.AsyncClient = object
    sys.modules["google.cloud.firestore"] = firestore_stub
    setattr(google_cloud, "firestore", firestore_stub)

if "google.cloud.storage" not in sys.modules:
    storage_stub = types.ModuleType("google.cloud.storage")

    class _StorageBucket:
        pass

    class _StorageBlob:
        pass

    class _StorageClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def bucket(self, *args, **kwargs):
            return _StorageBucket()

    storage_stub.Bucket = _StorageBucket
    storage_stub.Blob = _StorageBlob
    storage_stub.Client = _StorageClient
    sys.modules["google.cloud.storage"] = storage_stub
    setattr(google_cloud, "storage", storage_stub)

if google_adk is not None and "google.adk.utils" not in sys.modules:
    adk_utils_stub = types.ModuleType("google.adk.utils")
    instructions_utils_stub = types.ModuleType("google.adk.utils.instructions_utils")
    adk_utils_stub.instructions_utils = instructions_utils_stub
    sys.modules["google.adk.utils"] = adk_utils_stub
    sys.modules["google.adk.utils.instructions_utils"] = instructions_utils_stub
    setattr(google_adk, "utils", adk_utils_stub)

from agent import tools
from backend import ws_router


class LiveImageQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._session_pending.clear()
        tools._session_generating.clear()
        tools._session_cancel_current.clear()
        tools._session_image_backoff_until.clear()
        ws_router._pending_image_events.clear()

    def tearDown(self) -> None:
        tools._session_pending.clear()
        tools._session_generating.clear()
        tools._session_cancel_current.clear()
        tools._session_image_backoff_until.clear()
        ws_router._pending_image_events.clear()

    def test_detects_newer_pending_scene_request(self) -> None:
        tools._session_pending["session-a"] = tools.VisualArgs(
            description="newest castle",
            request_id="new-request",
        )
        self.assertTrue(tools._queued_newer_scene_request("session-a", "old-request"))

    def test_same_pending_request_is_not_treated_as_newer(self) -> None:
        tools._session_pending["session-a"] = tools.VisualArgs(
            description="same castle",
            request_id="same-request",
        )
        self.assertFalse(tools._queued_newer_scene_request("session-a", "same-request"))

    def test_missing_current_request_id_treats_pending_as_newer(self) -> None:
        tools._session_pending["session-a"] = tools.VisualArgs(
            description="queued scene",
            request_id="queued-request",
        )
        self.assertTrue(tools._queued_newer_scene_request("session-a", ""))

    def test_unshown_superseded_scene_does_not_persist_to_movie(self) -> None:
        tools._session_cancel_current.add("session-a")

        self.assertFalse(
            tools._should_persist_uploaded_scene_asset(
                session_id="session-a",
                superseded_render=True,
                preview_published=False,
            )
        )

    def test_visible_scene_can_still_persist_after_follow_up_queue(self) -> None:
        tools._session_cancel_current.add("session-a")

        self.assertTrue(
            tools._should_persist_uploaded_scene_asset(
                session_id="session-a",
                superseded_render=True,
                preview_published=True,
            )
        )

    def test_queue_latest_scene_follow_up_request_keeps_latest_child_wish(self) -> None:
        state = {}

        ws_router._queue_latest_scene_follow_up_request(state, "Let's go to the crystal bridge next.")
        first_timestamp = int(state.get("queued_scene_child_utterance_at_epoch_ms", 0) or 0)

        ws_router._queue_latest_scene_follow_up_request(state, "No, go behind the waterfall instead.")

        self.assertEqual(state["queued_scene_child_utterance"], "No, go behind the waterfall instead.")
        self.assertGreaterEqual(int(state.get("queued_scene_child_utterance_at_epoch_ms", 0) or 0), first_timestamp)
        self.assertEqual(state["partial_child_utterance"], "No, go behind the waterfall instead.")
        self.assertTrue(state["partial_child_utterance_finished"])

    def test_arm_queued_scene_follow_up_after_render_promotes_latest_request(self) -> None:
        state = {
            "queued_scene_child_utterance": "Let's go inside the glowing tunnel.",
            "queued_scene_child_utterance_at_epoch_ms": 123,
            "pending_response": False,
            "scene_tool_turn_open": False,
            "assembly_status": "",
        }

        queued = ws_router._arm_queued_scene_follow_up_after_render(state)

        self.assertEqual(queued, "Let's go inside the glowing tunnel.")
        self.assertEqual(state["queued_scene_child_utterance"], "")
        self.assertEqual(state["queued_scene_child_utterance_at_epoch_ms"], 0)
        self.assertTrue(state["pending_response"])
        self.assertFalse(state["pending_response_interrupted"])
        self.assertTrue(state["scene_tool_turn_open"])
        self.assertEqual(state["last_child_utterance"], "Let's go inside the glowing tunnel.")
        self.assertTrue(str(state.get("pending_response_token", "")).strip())

    def test_arm_queued_scene_follow_up_after_render_skips_when_response_already_pending(self) -> None:
        state = {
            "queued_scene_child_utterance": "Let's climb up to the dragon.",
            "queued_scene_child_utterance_at_epoch_ms": 456,
            "pending_response": True,
            "scene_tool_turn_open": False,
            "assembly_status": "",
        }

        queued = ws_router._arm_queued_scene_follow_up_after_render(state)

        self.assertEqual(queued, "")
        self.assertEqual(state["queued_scene_child_utterance"], "Let's climb up to the dragon.")

    def test_scene_render_still_in_flight_after_tool_call_tracks_event_state(self) -> None:
        state = {
            "pending_scene_description": "A dark licorice castle rises above the candy path.",
            "pending_scene_base_description": "A dark licorice castle rises above the candy path.",
        }
        ws_router._pending_image_events["session-a"] = asyncio.Event()

        self.assertTrue(
            ws_router._scene_render_still_in_flight_after_tool_call("session-a", state)
        )

        ws_router._pending_image_events["session-a"].set()

        self.assertFalse(
            ws_router._scene_render_still_in_flight_after_tool_call("session-a", state)
        )

    def test_scene_render_still_in_flight_after_tool_call_falls_back_to_pending_scene_metadata(self) -> None:
        state = {
            "pending_scene_description": "A dark licorice castle rises above the candy path.",
            "pending_scene_base_description": "",
        }

        self.assertTrue(
            ws_router._scene_render_still_in_flight_after_tool_call("session-a", state)
        )

        state["pending_scene_description"] = ""

        self.assertFalse(
            ws_router._scene_render_still_in_flight_after_tool_call("session-a", state)
        )

    def test_promote_pending_scene_request_to_current_updates_visible_scene(self) -> None:
        state = {
            "active_scene_request_id": "req-2",
            "current_scene_description": "At the top of the stairs by a glowing wooden door.",
            "current_scene_base_description": "At the top of the stairs by a glowing wooden door.",
            "pending_scene_description": "A cozy castle library with towering bookshelves.",
            "pending_scene_base_description": "A cozy castle library with towering bookshelves.",
            "current_scene_storybeat_text": "",
        }

        promoted = ws_router._promote_pending_scene_request_to_current(
            state,
            request_id="req-2",
            description="A cozy castle library with towering bookshelves.",
            storybeat_text="A cozy castle library appears.",
        )

        self.assertTrue(promoted)
        self.assertEqual(state["current_scene_description"], "A cozy castle library with towering bookshelves.")
        self.assertEqual(state["current_scene_base_description"], "A cozy castle library with towering bookshelves.")
        self.assertEqual(state["current_scene_storybeat_text"], "A cozy castle library appears.")
        self.assertEqual(state["pending_scene_description"], "")
        self.assertEqual(state["pending_scene_base_description"], "")

    def test_apply_scene_asset_uses_pending_scene_base_description(self) -> None:
        state = {
            "active_scene_request_id": "req-2",
            "current_scene_description": "At the top of the stairs by a glowing wooden door.",
            "current_scene_base_description": "At the top of the stairs by a glowing wooden door.",
            "pending_scene_description": "A cozy castle library with towering bookshelves.",
            "pending_scene_base_description": "A cozy castle library with towering bookshelves.",
            "story_pages": [],
            "scene_branch_points": [],
            "continuity_entity_registry": {"characters": {}, "locations": {}, "props": {}},
            "continuity_world_state": {
                "scene_index": 0,
                "current_location_key": "",
                "current_location_label": "",
                "previous_location_key": "",
                "previous_location_label": "",
                "active_character_keys": [],
                "active_prop_keys": [],
                "goal": "",
                "last_transition": "",
                "pending_request": "",
                "pending_location_key": "",
                "pending_location_label": "",
                "pending_transition": "",
                "pending_character_keys": [],
                "pending_prop_keys": [],
            },
            "continuity_scene_history": [],
        }

        ws_router._apply_scene_asset_to_story_state(
            state,
            request_id="req-2",
            image_url="https://example.com/library.png",
            description="A cozy castle library with towering bookshelves.",
            storybeat_text="A cozy castle library appears.",
            gcs_uri="gs://storybook/library.png",
        )

        self.assertEqual(state["current_scene_description"], "A cozy castle library with towering bookshelves.")
        self.assertEqual(state["current_scene_base_description"], "A cozy castle library with towering bookshelves.")
        self.assertEqual(state["pending_scene_description"], "")
        self.assertEqual(state["pending_scene_base_description"], "")
        self.assertEqual(state["scene_asset_urls"], ["https://example.com/library.png"])

    def test_apply_nonpersistent_scene_ready_preserves_newer_pending_request(self) -> None:
        state = {
            "active_scene_request_id": "req-new",
            "current_scene_description": "Candy Land with lollipop trees.",
            "current_scene_base_description": "Candy Land with lollipop trees.",
            "pending_scene_description": "A child flying toward the moon.",
            "pending_scene_base_description": "A child flying toward the moon.",
            "scene_render_pending": True,
        }

        ws_router._apply_nonpersistent_scene_ready_to_state(
            state,
            request_id="req-old",
            looks_like_image=True,
            is_fallback=False,
            description="An older stale scene finishes late.",
            storybeat_text="The stale scene should be ignored.",
        )

        self.assertEqual(state["current_scene_description"], "Candy Land with lollipop trees.")
        self.assertEqual(state["pending_scene_description"], "A child flying toward the moon.")
        self.assertTrue(state["scene_render_pending"])

    def test_apply_nonpersistent_scene_ready_fallback_keeps_current_scene(self) -> None:
        state = {
            "active_scene_request_id": "req-moon",
            "current_scene_description": "Candy Land with lollipop trees.",
            "current_scene_base_description": "Candy Land with lollipop trees.",
            "pending_scene_description": "A child flying toward the moon.",
            "pending_scene_base_description": "A child flying toward the moon.",
            "scene_render_pending": True,
        }

        ws_router._apply_nonpersistent_scene_ready_to_state(
            state,
            request_id="req-moon",
            looks_like_image=True,
            is_fallback=True,
            description="A fallback moon scene that never rendered.",
            storybeat_text="The fallback should not replace the visible page.",
        )

        self.assertEqual(state["current_scene_description"], "Candy Land with lollipop trees.")
        self.assertEqual(state["current_scene_base_description"], "Candy Land with lollipop trees.")
        self.assertEqual(state["pending_scene_description"], "")
        self.assertEqual(state["pending_scene_base_description"], "")
        self.assertFalse(state["scene_render_pending"])

    def test_fallback_scene_request_can_promote_finished_image_to_current_scene(self) -> None:
        state = {
            "current_scene_description": "Candy Land with lollipop trees.",
            "current_scene_base_description": "Candy Land with lollipop trees.",
            "current_scene_storybeat_text": "We walk along the candy path.",
            "pending_scene_description": "",
            "pending_scene_base_description": "",
            "scene_render_pending": True,
        }

        ws_router._prime_pending_scene_request(
            state,
            request_id="req-castle",
            description="A dark castle with lightning above the hills.",
        )
        ws_router._apply_nonpersistent_scene_ready_to_state(
            state,
            request_id="req-castle",
            looks_like_image=True,
            is_fallback=False,
            description="A dark castle with lightning above the hills.",
            storybeat_text="A dark castle crackles ahead in the stormy sky.",
        )

        self.assertEqual(state["active_scene_request_id"], "req-castle")
        self.assertEqual(state["current_scene_description"], "A dark castle with lightning above the hills.")
        self.assertEqual(state["current_scene_base_description"], "A dark castle with lightning above the hills.")
        self.assertEqual(state["current_scene_storybeat_text"], "A dark castle crackles ahead in the stormy sky.")
        self.assertEqual(state["pending_scene_description"], "")
        self.assertEqual(state["pending_scene_base_description"], "")
        self.assertFalse(state["scene_render_pending"])

    def test_apply_nonpersistent_scene_ready_appends_preview_page_history(self) -> None:
        state = {
            "active_scene_request_id": "req-castle",
            "current_scene_description": "Candy path winding toward a licorice castle.",
            "current_scene_base_description": "Candy path winding toward a licorice castle.",
            "current_scene_storybeat_text": "We follow the candy path toward the spooky castle.",
            "pending_scene_description": "Inside the licorice castle, a glowing candy hall.",
            "pending_scene_base_description": "Inside the licorice castle, a glowing candy hall.",
            "story_pages": [
                {
                    "scene_number": 1,
                    "request_id": "req-path",
                    "scene_description": "Candy path winding toward a licorice castle.",
                    "storybeat_text": "We follow the candy path toward the spooky castle.",
                    "image_url": "https://example.com/candy-path.png",
                    "gcs_uri": "gs://storybook/candy-path.png",
                },
            ],
            "scene_branch_points": [
                {
                    "scene_number": 1,
                    "request_id": "req-path",
                    "label": "We follow the candy path toward the spooky castle.",
                    "scene_description": "Candy path winding toward a licorice castle.",
                    "storybeat_text": "We follow the candy path toward the spooky castle.",
                    "image_url": "https://example.com/candy-path.png",
                    "gcs_uri": "gs://storybook/candy-path.png",
                },
            ],
            "scene_render_pending": True,
        }

        ws_router._apply_nonpersistent_scene_ready_to_state(
            state,
            request_id="req-castle",
            looks_like_image=True,
            is_fallback=False,
            description="Inside the licorice castle, a glowing candy hall.",
            storybeat_text="Inside the licorice castle, the candy hall glows with warm lanterns.",
        )

        self.assertEqual(len(state["story_pages"]), 2)
        self.assertEqual(state["story_pages"][-1]["scene_number"], 2)
        self.assertEqual(state["story_pages"][-1]["request_id"], "req-castle")
        self.assertEqual(state["story_pages"][-1]["image_url"], "")
        self.assertEqual(
            state["story_pages"][-1]["storybeat_text"],
            "Inside the licorice castle, the candy hall glows with warm lanterns.",
        )
        self.assertEqual(len(state["scene_branch_points"]), 2)
        self.assertEqual(state["scene_branch_points"][-1]["scene_number"], 2)
        self.assertEqual(state["scene_branch_points"][-1]["request_id"], "req-castle")
        self.assertEqual(state["current_scene_description"], "Inside the licorice castle, a glowing candy hall.")
        self.assertEqual(
            state["current_scene_storybeat_text"],
            "Inside the licorice castle, the candy hall glows with warm lanterns.",
        )
        self.assertFalse(state["scene_render_pending"])

    def test_public_scene_description_prefers_base_description(self) -> None:
        args = tools.VisualArgs(
            description=(
                "Moon. A glowing moon scene. Story continuity target: keep this page in or directly connected "
                "to moon. Tone: brave preschool fantasy adventure."
            ),
            base_description="Moon. A glowing moon scene.",
        )

        self.assertEqual(tools._public_scene_description(args), "Moon. A glowing moon scene.")

    def test_run_visual_pipeline_keeps_successful_first_still_when_repair_retry_fails(self) -> None:
        published_events: list[dict[str, object]] = []

        render_outputs: list[object] = [
            (b"good-image", "image/jpeg", "Aaron stands on the moon."),
            RuntimeError("429 RESOURCE_EXHAUSTED"),
            RuntimeError("429 RESOURCE_EXHAUSTED"),
        ]

        def _fake_generate_scene_still(*args, **kwargs):
            outcome = render_outputs.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def _schedule_background_task(coro):
            try:
                coro.close()
            except Exception:
                pass

        async def _fast_sleep(*args, **kwargs) -> None:
            return None

        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-project"}, clear=False),
            patch.object(tools, "_generate_scene_still", side_effect=_fake_generate_scene_still),
            patch.object(
                tools,
                "_audit_scene_visual_continuity",
                new=AsyncMock(
                    return_value={
                        "should_retry": True,
                        "repair_prompt_suffix": "Show more moon surface and craters.",
                    }
                ),
            ),
            patch.object(tools, "_encode_transport_image", return_value=(b"encoded-image", "image/jpeg")),
            patch.object(tools, "_describe_scene_image_for_continuity", new=AsyncMock(return_value="Moon surface with craters.")),
            patch.object(tools, "_detect_character_reference_crops", new=AsyncMock(return_value=[])),
            patch.object(tools, "_upload_scene_still", return_value=("https://example.com/moon.jpg", "gs://storybook/moon.jpg")),
            patch.object(tools, "_persist_uploaded_scene_asset"),
            patch.object(tools, "_make_thumbnail_b64", return_value=None),
            patch.object(tools, "build_scene_visual_audit_feedback_signal", return_value=None),
            patch.object(tools, "record_prompt_feedback"),
            patch.object(tools, "publish_session_event", side_effect=lambda session_id, event: published_events.append(event)),
            patch.object(tools, "schedule_background_task", side_effect=_schedule_background_task),
            patch.object(tools, "_veo_enabled", return_value=False),
            patch.object(tools.asyncio, "sleep", new=AsyncMock(side_effect=_fast_sleep)),
        ):
            asyncio.run(
                tools._run_visual_pipeline(
                    args=tools.VisualArgs(
                        description=(
                            "Moon. Aaron stands on a glowing sugar-cookie moon. Story continuity target: "
                            "keep this page in or directly connected to moon."
                        ),
                        base_description="Moon. Aaron stands on a glowing sugar-cookie moon.",
                        request_id="req-moon",
                    ),
                    session_id="session-a",
                )
            )

        self.assertTrue(published_events)
        self.assertEqual(published_events[0]["type"], "video_ready")
        payload = published_events[0]["payload"]
        self.assertEqual(payload["description"], "Moon. Aaron stands on a glowing sugar-cookie moon.")
        self.assertFalse(bool(payload.get("is_fallback")))

    def test_run_visual_pipeline_skips_blocking_retry_for_noncritical_live_audit(self) -> None:
        published_events: list[dict[str, object]] = []
        generate_call_count = 0

        def _fake_generate_scene_still(*args, **kwargs):
            nonlocal generate_call_count
            generate_call_count += 1
            return (b"good-image", "image/jpeg", "Aaron stands by the licorice castle.")

        def _schedule_background_task(coro):
            try:
                coro.close()
            except Exception:
                pass

        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-project"}, clear=False),
            patch.object(tools, "_generate_scene_still", side_effect=_fake_generate_scene_still),
            patch.object(
                tools,
                "_audit_scene_visual_continuity",
                new=AsyncMock(
                    return_value={
                        "status": "repair",
                        "should_retry": True,
                        "repair_prompt_suffix": "Make the black licorice towers and candy sky more obvious.",
                        "issues": [
                            {
                                "severity": "major",
                                "kind": "character_palette_drift",
                                "issue": "The colors drifted a little from the established palette.",
                            }
                        ],
                    }
                ),
            ),
            patch.object(tools, "_encode_transport_image", return_value=(b"encoded-image", "image/jpeg")),
            patch.object(tools, "_describe_scene_image_for_continuity", new=AsyncMock(return_value="Licorice castle under a candy sky.")),
            patch.object(tools, "_detect_character_reference_crops", new=AsyncMock(return_value=[])),
            patch.object(tools, "_upload_scene_still", return_value=("https://example.com/castle.jpg", "gs://storybook/castle.jpg")),
            patch.object(tools, "_persist_uploaded_scene_asset"),
            patch.object(tools, "_make_thumbnail_b64", return_value=None),
            patch.object(tools, "build_scene_visual_audit_feedback_signal", return_value=None),
            patch.object(tools, "record_prompt_feedback"),
            patch.object(tools, "publish_session_event", side_effect=lambda session_id, event: published_events.append(event)),
            patch.object(tools, "schedule_background_task", side_effect=_schedule_background_task),
            patch.object(tools, "_veo_enabled", return_value=False),
        ):
            asyncio.run(
                tools._run_visual_pipeline(
                    args=tools.VisualArgs(
                        description=(
                            "Licorice castle. Aaron stands by a big black licorice castle under a swirling candy sky. "
                            "Story continuity target: keep this page in or directly connected to licorice castle."
                        ),
                        base_description="Licorice castle. Aaron stands by a big black licorice castle under a swirling candy sky.",
                        request_id="req-castle",
                    ),
                    session_id="session-a",
                )
            )

        self.assertEqual(generate_call_count, 1)
        self.assertTrue(published_events)
        self.assertEqual(published_events[0]["type"], "video_ready")

    def test_run_visual_pipeline_republishes_placeholder_when_quota_retry_is_scheduled(self) -> None:
        published_events: list[dict[str, object]] = []
        scheduled_coroutines: list[object] = []

        def _schedule_background_task(coro):
            scheduled_coroutines.append(coro)
            try:
                coro.close()
            except Exception:
                pass

        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-project"}, clear=False),
            patch.object(tools, "_generate_scene_still", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")),
            patch.object(tools, "record_prompt_feedback"),
            patch.object(tools, "publish_session_event", side_effect=lambda session_id, event: published_events.append(event)),
            patch.object(tools, "schedule_background_task", side_effect=_schedule_background_task),
        ):
            asyncio.run(
                tools._run_visual_pipeline(
                    args=tools.VisualArgs(
                        description="A dark licorice castle under a swirling candy sky.",
                        base_description="A dark licorice castle under a swirling candy sky.",
                        request_id="req-backpressure",
                    ),
                    session_id="session-a",
                )
            )

        self.assertTrue(published_events)
        self.assertEqual(published_events[0]["type"], "video_ready")
        self.assertTrue(bool(published_events[0]["payload"].get("is_placeholder")))
        self.assertEqual(published_events[0]["payload"].get("request_id"), "req-backpressure")
        self.assertIn("session-a", tools._session_image_backoff_until)
        self.assertTrue(scheduled_coroutines)

    def test_run_visual_pipeline_publishes_raster_fallback_after_terminal_quota_failure(self) -> None:
        published_events: list[dict[str, object]] = []

        with (
            patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "test-project"}, clear=False),
            patch.object(tools, "_generate_scene_still", side_effect=RuntimeError("429 RESOURCE_EXHAUSTED")),
            patch.object(tools, "_build_fallback_scene_card_data_url", return_value="data:image/png;base64,abc123"),
            patch.object(tools, "record_prompt_feedback"),
            patch.object(tools, "publish_session_event", side_effect=lambda session_id, event: published_events.append(event)),
            patch.object(tools, "schedule_background_task"),
        ):
            asyncio.run(
                tools._run_visual_pipeline(
                    args=tools.VisualArgs(
                        description="Inside the castle door, Santa waits with presents and candy.",
                        base_description="Inside the castle door, Santa waits with presents and candy.",
                        request_id="req-santa-room",
                        quota_retry_count=1,
                    ),
                    session_id="session-a",
                )
            )

        self.assertTrue(published_events)
        self.assertEqual(published_events[0]["type"], "video_ready")
        payload = published_events[0]["payload"]
        self.assertTrue(bool(payload.get("is_fallback")))
        self.assertFalse(bool(payload.get("is_placeholder")))
        self.assertEqual(payload.get("request_id"), "req-santa-room")
        self.assertEqual(payload.get("url"), "data:image/png;base64,abc123")


if __name__ == "__main__":
    unittest.main()
