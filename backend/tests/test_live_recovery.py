from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent import tools as agent_tools
from backend import ws_router


class LiveRecoveryTests(unittest.TestCase):
    def tearDown(self) -> None:
        ws_router._awaiting_greeting_sessions.clear()
        ws_router._opening_phase_sessions.clear()
        ws_router._ending_story_sessions.clear()
        ws_router._watching_final_video_sessions.clear()
        ws_router._assistant_speaking_sessions.clear()
        ws_router._interrupted_turn_sessions.clear()
        ws_router._page_read_aloud_active_sessions.clear()
        ws_router._page_read_aloud_suppress_until.clear()

    def test_clean_close_resumes_during_opening_phase(self) -> None:
        ws_router._opening_phase_sessions.add("session-a")
        self.assertTrue(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {},
                meaningful_pending_turn=False,
            )
        )

    def test_clean_close_does_not_resume_idle_opening_phase_after_greeting(self) -> None:
        ws_router._opening_phase_sessions.add("session-a")
        self.assertFalse(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {
                    "response_turn_number": 2,
                    "story_summary": "Hi there, friend! What's your name?",
                },
                meaningful_pending_turn=False,
            )
        )

    def test_clean_close_resumes_with_pending_response_state(self) -> None:
        self.assertTrue(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {"pending_response": True},
                meaningful_pending_turn=False,
            )
        )

    def test_clean_close_does_not_resume_idle_session(self) -> None:
        self.assertFalse(
            ws_router._should_attempt_clean_live_resume(
                "session-a",
                {},
                meaningful_pending_turn=False,
            )
        )

    def test_page_read_aloud_suppression_is_active_while_marked_active(self) -> None:
        ws_router._set_page_read_aloud_suppression(
            "session-a",
            active=True,
            suppress_for_ms=2500,
        )
        self.assertTrue(ws_router._page_read_aloud_suppression_active("session-a"))

    def test_page_read_aloud_suppression_persists_through_cooldown(self) -> None:
        with mock.patch.object(ws_router.time, "monotonic", side_effect=[100.0, 100.5, 103.0]):
            ws_router._set_page_read_aloud_suppression(
                "session-a",
                active=False,
                suppress_for_ms=2000,
            )
            self.assertTrue(ws_router._page_read_aloud_suppression_active("session-a"))
            self.assertFalse(ws_router._page_read_aloud_suppression_active("session-a"))

    def test_pending_scene_replacement_always_forces_live_reset(self) -> None:
        ws_router._assistant_speaking_sessions.discard("session-a")
        ws_router._interrupted_turn_sessions.discard("session-a")
        self.assertTrue(ws_router._should_reset_live_for_pending_scene_replacement("session-a"))

    def test_pending_scene_replacement_forces_reset_when_amelia_is_still_speaking(self) -> None:
        ws_router._assistant_speaking_sessions.add("session-a")
        try:
            self.assertTrue(ws_router._should_reset_live_for_pending_scene_replacement("session-a"))
        finally:
            ws_router._assistant_speaking_sessions.discard("session-a")

    def test_pending_scene_replacement_forces_reset_when_interrupted_turn_is_still_hot(self) -> None:
        ws_router._interrupted_turn_sessions.add("session-a")
        try:
            self.assertTrue(ws_router._should_reset_live_for_pending_scene_replacement("session-a"))
        finally:
            ws_router._interrupted_turn_sessions.discard("session-a")

    def test_pending_scene_replacement_only_barge_ins_when_amelia_is_active(self) -> None:
        self.assertFalse(ws_router._should_activate_barge_in_for_pending_scene_replacement("session-a"))
        ws_router._assistant_speaking_sessions.add("session-a")
        try:
            self.assertTrue(ws_router._should_activate_barge_in_for_pending_scene_replacement("session-a"))
        finally:
            ws_router._assistant_speaking_sessions.discard("session-a")

    def test_pending_scene_replacement_allows_barge_in_during_greeting(self) -> None:
        ws_router._awaiting_greeting_sessions.add("session-a")
        try:
            self.assertTrue(ws_router._should_activate_barge_in_for_pending_scene_replacement("session-a"))
        finally:
            ws_router._awaiting_greeting_sessions.discard("session-a")

    def test_pending_scene_replacement_follow_up_keeps_live_output_open(self) -> None:
        self.assertTrue(
            ws_router._should_keep_live_open_for_pending_scene_replacement_follow_up(
                {"pending_scene_replacement_text": "Wait no, Bubble Land."}
            )
        )
        self.assertFalse(
            ws_router._should_keep_live_open_for_pending_scene_replacement_follow_up(
                {"pending_scene_replacement_text": ""}
            )
        )

    def test_toy_share_story_request_auto_resumes_story(self) -> None:
        self.assertTrue(
            ws_router._should_resume_story_from_toy_share(
                "Can we go to the dark castle now?",
                {"toy_share_active": True},
            )
        )

    def test_toy_share_request_does_not_auto_resume_story(self) -> None:
        self.assertFalse(
            ws_router._should_resume_story_from_toy_share(
                "Can I show you my toy?",
                {"toy_share_active": True},
            )
        )

    def test_pending_scene_replacement_ack_turn_detects_ack_phase(self) -> None:
        self.assertTrue(
            ws_router._is_pending_scene_replacement_ack_turn(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                },
                scene_visuals_called_this_turn=False,
            )
        )
        self.assertFalse(
            ws_router._is_pending_scene_replacement_ack_turn(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_render",
                },
                scene_visuals_called_this_turn=False,
            )
        )

    def test_pending_scene_replacement_follow_up_turn_allows_render_ready_phase(self) -> None:
        self.assertTrue(
            ws_router._is_pending_scene_replacement_follow_up_turn(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_render",
                },
                scene_visuals_called_this_turn=False,
            )
        )
        self.assertFalse(
            ws_router._is_pending_scene_replacement_follow_up_turn(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "",
                },
                scene_visuals_called_this_turn=False,
            )
        )

    def test_pending_scene_replacement_ack_retries_only_for_silent_ack_turn(self) -> None:
        self.assertTrue(
            ws_router._should_retry_pending_scene_replacement_ack(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                },
                had_child_input_this_turn=True,
                model_emitted_meaningful_output=False,
                scene_visuals_called_this_turn=False,
            )
        )
        self.assertFalse(
            ws_router._should_retry_pending_scene_replacement_ack(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                },
                had_child_input_this_turn=True,
                model_emitted_meaningful_output=True,
                scene_visuals_called_this_turn=False,
            )
        )

    def test_pending_scene_replacement_ack_holds_scene_tools(self) -> None:
        self.assertTrue(
            ws_router._should_hold_scene_tools_for_pending_replacement_ack(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                }
            )
        )
        self.assertFalse(
            ws_router._should_hold_scene_tools_for_pending_replacement_ack(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_render",
                }
            )
        )

    def test_pending_scene_replacement_ack_ignores_stale_turn_complete_without_speech(self) -> None:
        self.assertTrue(
            ws_router._should_ignore_turn_complete_while_waiting_for_pending_replacement_ack(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                    "pending_scene_replacement_armed_at_epoch_ms": 1_000,
                },
                last_finished_assistant_output_at_epoch_ms=0,
            )
        )
        self.assertFalse(
            ws_router._should_ignore_turn_complete_while_waiting_for_pending_replacement_ack(
                {
                    "pending_scene_replacement_text": "Wait no, Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                    "pending_scene_replacement_armed_at_epoch_ms": 1_000,
                },
                last_finished_assistant_output_at_epoch_ms=1_250,
            )
        )

    def test_pending_scene_replacement_ack_ignores_turn_complete_until_finished_output_is_newer_than_interrupt(self) -> None:
        state = {
            "pending_scene_replacement_text": "Wait no, Bubble Land.",
            "pending_scene_replacement_phase": "awaiting_ack",
            "pending_scene_replacement_armed_at_epoch_ms": 2_000,
        }
        self.assertTrue(
            ws_router._should_ignore_turn_complete_while_waiting_for_pending_replacement_ack(
                state,
                last_finished_assistant_output_at_epoch_ms=1_999,
            )
        )
        self.assertFalse(
            ws_router._should_ignore_turn_complete_while_waiting_for_pending_replacement_ack(
                state,
                last_finished_assistant_output_at_epoch_ms=2_001,
            )
        )

    def test_named_destination_scene_descriptions_stay_location_focused(self) -> None:
        prompt_description, base_description = ws_router._named_destination_scene_descriptions(
            "Bubble Land"
        )
        self.assertIn("Bubble Land", prompt_description)
        self.assertIn("Bubble Land", base_description)
        self.assertNotIn("what kind of adventure", prompt_description.lower())
        self.assertNotIn("what kind of adventure", base_description.lower())

    def test_pending_scene_replacement_ack_text_requires_short_switch_ack(self) -> None:
        state = {
            "pending_scene_replacement_text": "No, wait, I want to go to Bubble Land.",
            "continuity_world_state": {"pending_location_label": "Bubble Land"},
        }
        self.assertTrue(
            ws_router._is_valid_pending_scene_replacement_ack_text(
                state,
                "Got it, we'll switch to Bubble Land.",
            )
        )
        self.assertFalse(
            ws_router._is_valid_pending_scene_replacement_ack_text(
                state,
                "AARON, I love that idea! We are going to a magical Candy Land! Is that right?",
            )
        )

    def test_pending_scene_replacement_partial_ack_aborts_old_location_narration(self) -> None:
        state = {
            "pending_scene_replacement_text": "No, wait, I want to go to Bubble Land.",
            "continuity_world_state": {
                "pending_location_label": "Bubble Land",
                "current_location_label": "Candy Land",
            },
        }
        self.assertTrue(
            ws_router._should_abort_partial_pending_scene_replacement_ack(
                state,
                "Candy Land is just for you, Aaron.",
            )
        )
        self.assertFalse(
            ws_router._should_abort_partial_pending_scene_replacement_ack(
                state,
                "Okay, let's switch to Bubble Land.",
            )
        )

    def test_scene_refresh_detects_explicit_visual_request(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_refresh(
                "Can you show me the Christmas tree?"
            )
        )

    def test_scene_refresh_detects_same_room_focus_shift(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_refresh(
                "Can I get closer to Santa's chair?"
            )
        )

    def test_scene_refresh_does_not_treat_detail_question_as_new_picture(self) -> None:
        self.assertFalse(
            ws_router._child_requested_scene_refresh(
                "What are the cupcakes like on this page?"
            )
        )

    def test_scene_chat_detects_current_page_detail_question(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_chat(
                "What are the cupcakes like on this page?"
            )
        )

    def test_scene_chat_detects_what_does_it_look_like_question(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_chat(
                "What do the cupcakes look like?"
            )
        )

    def test_scene_chat_detects_service_connection_question(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_chat(
                "Can you hear me right now?"
            )
        )

    def test_scene_chat_detects_simple_math_chatter(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_chat(
                "3 + 10 = 13"
            )
        )

    def test_fallback_scene_prompt_prefers_child_scene_request(self) -> None:
        prompt = ws_router._fallback_scene_prompt(
            "Let's tiptoe through the workshop together.",
            "Can we go to the Christmas tree?",
            {
                "story_summary": "We are inside Santa's workshop with warm lights and toys.",
                "current_scene_description": "Inside Santa's workshop by the fireplace.",
            },
        )

        self.assertIn("Christmas tree", prompt)
        self.assertNotIn("tiptoe through the workshop together", prompt)

    def test_read_page_request_is_treated_as_scene_chat(self) -> None:
        self.assertTrue(
            ws_router._child_requested_scene_chat(
                "Can you read the page to me right now? What's that say?"
            )
        )

    def test_read_page_request_does_not_trigger_fallback_scene(self) -> None:
        self.assertFalse(
            ws_router._should_trigger_fallback_scene(
                assistant_text="This page has busy elves polishing toys by the warm fireplace.",
                child_text="Can you read the page to me right now? What's that say?",
                state={
                    "scene_asset_urls": ["https://example.com/page1.jpg"],
                    "current_scene_description": "Inside Santa's workshop by the fireplace.",
                },
            )
        )

    def test_service_connection_question_does_not_trigger_fallback_scene_before_first_image(self) -> None:
        self.assertFalse(
            ws_router._should_trigger_fallback_scene(
                assistant_text="Yes, I can hear you just fine, friend.",
                child_text="Can you hear me?",
                state={
                    "scene_asset_urls": [],
                    "current_scene_description": "No image yet.",
                },
            )
        )

    def test_math_chatter_does_not_trigger_fallback_scene_before_first_image(self) -> None:
        self.assertFalse(
            ws_router._should_trigger_fallback_scene(
                assistant_text="Yes, that's thirteen!",
                child_text="3 + 10 = 13",
                state={
                    "scene_asset_urls": [],
                    "current_scene_description": "No image yet.",
                },
            )
        )

    def test_low_signal_single_word_is_not_actionable_once_story_is_underway(self) -> None:
        self.assertFalse(
            ws_router._is_actionable_child_text(
                "alle",
                {
                    "story_started": True,
                    "name_confirmed": True,
                },
            )
        )

    def test_single_word_name_like_story_seed_stays_actionable_during_onboarding(self) -> None:
        self.assertTrue(
            ws_router._is_actionable_child_text(
                "Candyland",
                {
                    "story_started": False,
                    "name_confirmed": False,
                },
            )
        )

    def test_pending_render_scene_chat_does_not_trigger_fallback_scene(self) -> None:
        self.assertFalse(
            ws_router._should_trigger_fallback_scene(
                assistant_text="The castle sounds fun! We can go look inside the big, shimmery bubble castle.",
                child_text="alle",
                state={
                    "scene_render_pending": True,
                    "pending_scene_description": "Bubble Land shimmered with playful bubble details.",
                    "scene_asset_urls": ["https://example.com/page1.jpg"],
                    "current_scene_description": "Bubble Land shimmered with playful bubble details.",
                    "story_started": True,
                    "name_confirmed": True,
                },
            )
        )

    def test_low_signal_single_word_does_not_trigger_fallback_scene_after_first_image(self) -> None:
        self.assertFalse(
            ws_router._should_trigger_fallback_scene(
                assistant_text="The castle sounds fun! We can go look inside the big, shimmery bubble castle.",
                child_text="Dharana",
                state={
                    "scene_asset_urls": ["https://example.com/page1.jpg"],
                    "current_scene_description": "Bubble Land shimmered with playful bubble details.",
                    "story_started": True,
                    "name_confirmed": True,
                },
            )
        )

    def test_resume_pending_child_turn_replays_replacement_prompt_after_reset(self) -> None:
        runner = mock.Mock()
        runner.session_service.get_session = mock.AsyncMock(
            return_value=mock.Mock(
                state={
                    "pending_response": True,
                    "pending_response_interrupted": False,
                    "last_child_utterance": "Oh, wait, no, I want to go to Bubble Land.",
                    "pending_scene_replacement_text": "Oh, wait, no, I want to go to Bubble Land.",
                    "pending_scene_replacement_phase": "awaiting_ack",
                    "scene_render_pending": True,
                }
            )
        )
        live_queue = mock.Mock()

        with mock.patch.object(ws_router, "_mutate_state", new=mock.AsyncMock()) as mutate_state:
            with mock.patch.object(ws_router, "_send_pending_scene_replacement_prompt") as send_replacement_prompt:
                with mock.patch.object(ws_router, "_send_live_content") as send_live_content:
                    resumed = asyncio.run(
                        ws_router._resume_pending_child_turn(
                            runner,
                            "user-a",
                            "session-a",
                            live_queue,
                            recovery_reason="hard_reset_1",
                        )
                    )

        self.assertTrue(resumed)
        mutate_state.assert_awaited()
        send_replacement_prompt.assert_called_once_with(
            "session-a",
            live_queue,
            "Oh, wait, no, I want to go to Bubble Land.",
        )
        send_live_content.assert_not_called()

    def test_storybook_scene_state_payload_uses_public_branch_points(self) -> None:
        payload = ws_router._storybook_scene_state_payload(
            {
                "scene_branch_points": [
                    {
                        "scene_number": 1,
                        "request_id": "req-1",
                        "scene_description": "Bubble forest.",
                        "storybeat_text": "Bubbles drift through the trees.",
                        "image_url": "https://example.com/scene-1.jpg",
                        "state_snapshot": {"story_summary": "internal only"},
                    }
                ],
                "scene_asset_urls": ["https://example.com/scene-1.jpg"],
                "scene_descriptions": ["Bubble forest."],
                "scene_storybeat_texts": ["Bubbles drift through the trees."],
                "current_scene_description": "Bubble forest.",
                "current_scene_storybeat_text": "Bubbles drift through the trees.",
                "story_summary": "We are in a bubble forest.",
                "story_phase": "waiting_for_child",
            }
        )

        self.assertEqual(payload["scene_branch_points_public"][0]["scene_number"], 1)
        self.assertEqual(payload["scene_branch_points"][0]["image_url"], "https://example.com/scene-1.jpg")
        self.assertNotIn("state_snapshot", payload["scene_branch_points"][0])

    def test_prepare_branch_state_restores_active_request_id(self) -> None:
        restored = ws_router._prepare_branch_state(
            {
                "scene_number": 2,
                "request_id": "req-2",
                "state_snapshot": {
                    "story_summary": "Scene two summary.",
                    "current_scene_description": "Secret bubble path.",
                    "current_scene_storybeat_text": "The bubble guides us onward.",
                },
            },
            [
                {
                    "scene_number": 1,
                    "request_id": "req-1",
                    "scene_description": "Bubble forest.",
                    "storybeat_text": "Bubbles drift through the trees.",
                    "image_url": "https://example.com/scene-1.jpg",
                },
                {
                    "scene_number": 2,
                    "request_id": "req-2",
                    "scene_description": "Secret bubble path.",
                    "storybeat_text": "The bubble guides us onward.",
                    "image_url": "https://example.com/scene-2.jpg",
                },
            ],
        )

        self.assertEqual(restored["active_scene_request_id"], "req-2")
        self.assertEqual(restored["current_scene_description"], "Secret bubble path.")
        self.assertEqual(restored["scene_asset_urls"][-1], "https://example.com/scene-2.jpg")

    def test_apply_shared_toy_story_state_promotes_toy_to_recurring_sidekick(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)

        ws_router._apply_shared_toy_story_state(
            state,
            summary_text="A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
            toy_thumb=("thumb-b64", "image/jpeg"),
        )

        self.assertTrue(state["camera_received"])
        self.assertEqual(state["camera_stage"], "done")
        self.assertEqual(state["toy_reference_thumbnail_b64"], "thumb-b64")
        self.assertEqual(state["toy_reference_thumbnail_mime"], "image/jpeg")
        self.assertEqual(
            state["toy_reference_visual_summary"],
            "A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
        )
        self.assertEqual(
            state["sidekick_description"],
            "A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
        )
        self.assertIn(
            {
                "character_name": "shared toy companion",
                "fact": "A small blue bunny toy with floppy ears and a shiny silver star on its belly.",
            },
            state["character_facts_list"],
        )
        self.assertIn("shared toy companion", state["character_facts"])
        registry = state["continuity_entity_registry"]["characters"]
        world = state["continuity_world_state"]
        self.assertIn("shared_toy_companion", registry)
        self.assertIn("shared_toy_companion", list(world.get("pending_character_keys", []) or []))
        self.assertIn("shared_toy_companion", list(world.get("active_character_keys", []) or []))

    def test_ensure_final_video_watch_task_restarts_watch_after_reconnect(self) -> None:
        async def fake_watch(_session_id: str) -> None:
            return None

        created_tasks: list[object] = []

        def fake_create_task(coro: object) -> object:
            created_tasks.append(coro)
            try:
                coro.close()  # type: ignore[attr-defined]
            except Exception:
                pass
            return object()

        with mock.patch.object(ws_router, "_watch_for_final_video", side_effect=fake_watch):
            with mock.patch.object(asyncio, "create_task", side_effect=fake_create_task):
                ws_router._ensure_final_video_watch_task("session-a")
                ws_router._ensure_final_video_watch_task("session-a")

        self.assertEqual(len(created_tasks), 1)
        self.assertIn("session-a", ws_router._watching_final_video_sessions)

    def test_extract_shared_toy_name_hint_prefers_explicit_name_phrases(self) -> None:
        self.assertEqual(
            ws_router._extract_shared_toy_name_hint("This is Lion-O."),
            "Lion-O",
        )
        self.assertEqual(
            ws_router._extract_shared_toy_name_hint("His name is Thundercats Lion-O!"),
            "Thundercats Lion-O",
        )
        self.assertEqual(
            ws_router._extract_shared_toy_name_hint("Let's go back to the castle now."),
            "",
        )

    def test_apply_shared_toy_story_state_preserves_named_toy_identity(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)

        ws_router._apply_shared_toy_story_state(
            state,
            summary_text="An orange cat-like hero toy with a red mane, blue suit, and silver sword.",
            toy_name_hint="Lion-O",
        )

        self.assertEqual(state["toy_reference_name_hint"], "Lion-O")
        self.assertEqual(
            state["sidekick_description"],
            "Lion-O, an orange cat-like hero toy with a red mane, blue suit, and silver sword.",
        )
        self.assertIn(
            {
                "character_name": "Lion-O",
                "fact": "shared toy helper; An orange cat-like hero toy with a red mane, blue suit, and silver sword.",
            },
            state["character_facts_list"],
        )
        registry = state["continuity_entity_registry"]["characters"]
        world = state["continuity_world_state"]
        self.assertEqual(registry["lion_o"]["label"], "Lion-O")
        self.assertIn("lion_o", list(world.get("pending_character_keys", []) or []))
        self.assertIn("lion_o", list(world.get("active_character_keys", []) or []))

    def test_capture_child_story_continuity_promotes_named_voice_toy_without_fake_prop(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)

        ws_router._capture_child_story_continuity(
            state,
            "Can my toy come with us along the journey? His name's Lion-O from Thundercats. Can he come, too?",
        )

        self.assertEqual(state["toy_reference_name_hint"], "Lion-O")
        self.assertEqual(
            state["sidekick_description"],
            "Lion-O, the child's toy companion",
        )
        self.assertIn(
            {
                "character_name": "Lion-O",
                "fact": "shared toy helper and recurring companion in the story",
            },
            state["character_facts_list"],
        )
        self.assertNotIn("Can my toy", state["continuity_registry_text"])
        self.assertEqual(
            list(state["continuity_world_state"].get("pending_prop_keys", []) or []),
            [],
        )

    def test_capture_child_story_continuity_does_not_promote_name_only_without_toy_context(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)

        ws_router._capture_child_story_continuity(state, "Lion-O")

        self.assertEqual(state["toy_reference_name_hint"], "")
        self.assertEqual(state["sidekick_description"], "a magical companion")
        self.assertEqual(list(state["character_facts_list"]), [])

    def test_capture_child_story_continuity_keeps_established_toy_name_on_friend_request(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)
        ws_router._apply_shared_toy_story_state(
            state,
            summary_text="An orange cat-like hero toy with a red mane, blue suit, and silver sword.",
            toy_name_hint="Lion-O",
        )

        ws_router._capture_child_story_continuity(
            state,
            "Can my friend, Lainey, come with us?",
        )

        self.assertEqual(state["toy_reference_name_hint"], "Lion-O")
        self.assertEqual(
            state["sidekick_description"],
            "Lion-O, an orange cat-like hero toy with a red mane, blue suit, and silver sword.",
        )
        self.assertNotIn("Lainey", state["character_facts"])

    def test_apply_shared_toy_story_state_prunes_conflicting_toy_aliases(self) -> None:
        state: dict[str, object] = {}
        ws_router._ensure_session_state_defaults(state)
        state["character_facts_list"] = [
            {"character_name": "Lino", "fact": "shared toy helper and recurring companion in the story"},
            {"character_name": "shared toy companion", "fact": "the child's recurring toy companion"},
        ]
        state["character_facts"] = (
            "- Lino: shared toy helper and recurring companion in the story\n"
            "- shared toy companion: the child's recurring toy companion"
        )
        state["continuity_entity_registry"]["characters"] = {
            "lino": {"label": "Lino"},
            "lion_o": {"label": "Lion-O"},
        }
        state["continuity_world_state"]["active_character_keys"] = ["lino", "lion_o"]
        state["continuity_world_state"]["pending_character_keys"] = ["lino"]
        state["character_bible"] = {
            "lino": {"label": "Lino"},
            "lion_o": {"label": "Lion-O"},
        }

        ws_router._apply_shared_toy_story_state(
            state,
            summary_text="An orange cat-like hero toy with a red mane, blue suit, and silver sword.",
            toy_name_hint="Lion-O",
        )

        fact_names = [entry["character_name"] for entry in state["character_facts_list"]]
        self.assertNotIn("Lino", fact_names)
        self.assertIn("Lion-O", fact_names)
        self.assertNotIn("lino", state["continuity_world_state"]["active_character_keys"])
        self.assertNotIn("lino", state["continuity_world_state"]["pending_character_keys"])
        self.assertNotIn("lino", state["character_bible"])

    def test_save_character_fact_ignores_low_confidence_friend_name_when_toy_is_established(self) -> None:
        state = {
            "camera_received": True,
            "toy_reference_name_hint": "Lion-O",
            "toy_reference_visual_summary": "An orange cat-like hero toy with a red mane, blue suit, and silver sword.",
            "last_child_utterance": "Can my friend, Lainey, come with us?",
            "character_facts_list": [],
            "character_facts": "",
        }

        result = asyncio.run(
            agent_tools.save_character_fact(
                "Lino",
                "child's friend",
                SimpleNamespace(state=state),
            )
        )

        self.assertEqual(result, "System: Ignored low-confidence character fact.")
        self.assertEqual(state["character_facts_list"], [])
        self.assertEqual(state["character_facts"], "")

    def test_save_character_fact_allows_explicit_shared_toy_name(self) -> None:
        state = {
            "camera_received": True,
            "toy_reference_name_hint": "",
            "toy_reference_visual_summary": "An orange cat-like hero toy with a red mane, blue suit, and silver sword.",
            "last_child_utterance": "His name is Lion-O from Thundercats.",
            "character_facts_list": [],
            "character_facts": "",
        }

        result = asyncio.run(
            agent_tools.save_character_fact(
                "Lion-O",
                "shared toy helper and recurring companion in the story",
                SimpleNamespace(state=state),
            )
        )

        self.assertTrue(result.startswith("SAVE_FACT:Lion-O|"))
        self.assertEqual(
            state["character_facts_list"],
            [
                {
                    "character_name": "Lion-O",
                    "fact": "shared toy helper and recurring companion in the story",
                }
            ],
        )

    def test_scene_render_helpers_ignore_skipped_render_state(self) -> None:
        session_id = "session-skipped-render"
        state: dict[str, object] = {
            "scene_render_skipped": True,
            "scene_render_pending": False,
        }
        ws_router._pending_image_events[session_id] = asyncio.Event()
        try:
            self.assertFalse(ws_router._scene_render_in_progress(session_id, state))
            self.assertFalse(
                ws_router._scene_render_still_in_flight_after_tool_call(session_id, state)
            )
        finally:
            ws_router._pending_image_events.pop(session_id, None)

    def test_detect_shared_item_subject_thumbnail_returns_isolated_crop(self) -> None:
        response = mock.Mock()
        response.candidates = [
            mock.Mock(
                content=mock.Mock(
                    parts=[
                        mock.Mock(
                            text='{"visible": true, "x": 140, "y": 120, "width": 520, "height": 610, "notes": "main toy"}'
                        )
                    ]
                )
            )
        ]
        client = mock.Mock()
        client.models.generate_content.return_value = response

        with mock.patch.object(ws_router, "_build_google_genai_client", return_value=client):
            with mock.patch.object(
                ws_router,
                "_crop_image_to_thumbnail_b64",
                return_value=("isolated-toy-b64", "image/jpeg"),
            ) as crop_mock:
                result = asyncio.run(
                    ws_router._detect_shared_item_subject_thumbnail(
                        b"fake-jpeg-bytes",
                        toy_name_hint="Lion-O",
                        summary_text="An orange cat-like hero toy with a red mane.",
                    )
                )

        self.assertEqual(result, ("isolated-toy-b64", "image/jpeg"))
        crop_mock.assert_called_once()
        crop_box = crop_mock.call_args.kwargs["crop_box"]
        self.assertEqual(crop_box["x"], 140)
        self.assertEqual(crop_box["width"], 520)


if __name__ == "__main__":
    unittest.main()
