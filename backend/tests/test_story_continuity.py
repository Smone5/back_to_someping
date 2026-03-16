from __future__ import annotations

import sys
import unittest
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.story_continuity import (
    ensure_story_continuity_state,
    prime_character_carryover,
    record_continuity_scene,
    should_render_new_scene_page,
    update_continuity_from_child_utterance,
    validate_live_scene_request,
)


class StoryContinuityTests(unittest.TestCase):
    def test_requested_location_wins_over_generic_scene_drift(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        update_continuity_from_child_utterance(
            state,
            "Can we go to Santa's workshop and see the elves?",
        )

        result = validate_live_scene_request(
            state,
            "A magical castle made of shimmering ice and snow glows softly.",
        )

        self.assertIn("Santa's workshop", result.resolved_description)
        self.assertIn("requested_location_mismatch", result.issues)
        self.assertIn("elves", result.prompt_suffix.lower())

    def test_relative_transition_keeps_current_place(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="A giant colorful bouncy castle sits on soft grass with bright flags.",
            storybeat_text="The bouncy castle waits for fun.",
            scene_number=1,
        )
        current_label = str(state["continuity_world_state"]["current_location_label"])
        self.assertTrue(current_label)

        update_continuity_from_child_utterance(state, "Can we go inside?")
        result = validate_live_scene_request(
            state,
            "A bright squishy throne room with playful flags.",
        )

        self.assertIn(current_label.lower(), result.resolved_description.lower())
        self.assertIn("missing_location_anchor", result.issues)

    def test_record_scene_updates_registry_and_world_state(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        update_continuity_from_child_utterance(
            state,
            "Can we go to Santa's workshop and see the elves?",
        )

        record_continuity_scene(
            state,
            description="Inside Santa's workshop, little elves paint toys beside a cozy fireplace.",
            storybeat_text="Warm elves work happily in Santa's workshop.",
            scene_number=1,
        )

        world = state["continuity_world_state"]
        self.assertEqual(world["current_location_label"], "Santa's workshop")
        self.assertTrue(world["active_character_keys"])
        self.assertTrue(world["active_prop_keys"])
        self.assertIn("Santa's workshop", state["continuity_registry_text"])
        self.assertIn("Current place: Santa's workshop", state["continuity_world_state_text"])

    def test_voice_toy_request_does_not_create_conversational_prop_label(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)

        update_continuity_from_child_utterance(
            state,
            "Can my toy come with us along the journey? His name's Lion-O from Thundercats. Can he come, too?",
        )

        world = state["continuity_world_state"]
        self.assertEqual(list(world.get("pending_prop_keys", []) or []), [])
        self.assertNotIn("Can my toy", state["continuity_registry_text"])
        self.assertNotIn("like Can my toy", state["continuity_world_state_text"])

    def test_focus_request_stays_inside_current_room(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "Inside Santa's workshop, a tall Christmas tree stands by warm worktables, "
                "wrapped toys, and glowing lights."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a tall Christmas tree sparkles beside toy worktables.",
            storybeat_text="The indoor tree glows warmly in Santa's workshop.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we see the star at the top?")
        result = validate_live_scene_request(
            state,
            "A glowing star on top of a Christmas tree outside under the snowy night sky.",
        )

        self.assertIn("inside santa's workshop", result.resolved_description.lower())
        self.assertIn("Stay in this exact visible space", result.prompt_suffix)
        self.assertIn("interior_exterior_drift", result.issues)

    def test_bootstrap_from_story_pages_populates_history_without_recursion(self) -> None:
        state: dict[str, object] = {
            "story_pages": [
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "scene_description": "Inside Santa's workshop, elves build toys by a fireplace.",
                    "storybeat_text": "The elves work happily in Santa's workshop.",
                    "image_url": "https://example.com/scene-1.jpg",
                }
            ]
        }

        ensure_story_continuity_state(state)

        history = state["continuity_scene_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["request_id"], "scene-1")
        self.assertEqual(
            state["continuity_world_state"]["current_location_label"],
            "Santa's workshop",
        )

    def test_same_location_minor_beat_stays_on_current_page(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a wooden toy train sits near wrapped gifts and warm worktables.",
            storybeat_text="The toy train waits in Santa's workshop.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we hug the teddy bear?")
        result = validate_live_scene_request(
            state,
            "A child's hand gently hugging a fluffy teddy bear beside the toy train in Santa's workshop.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertFalse(decision.should_render)
        self.assertEqual(decision.reason, "same_location_minor_beat")

    def test_initial_placeholder_scene_still_allows_first_page_render(self) -> None:
        state: dict[str, object] = {
            "current_scene_description": "No image yet — the story is just beginning!"
        }
        ensure_story_continuity_state(state)

        update_continuity_from_child_utterance(
            state,
            "Can we go on an adventure to Candyland?",
        )
        result = validate_live_scene_request(
            state,
            "A whimsical Candyland forest with lollipop trees, candy-cane paths, and a chocolate river.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "first_page")

    def test_named_location_suffix_beats_prop_or_character_parsing(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)

        update_continuity_from_child_utterance(
            state,
            "Oh, wait, no, I want to go to Bubble Land.",
        )

        world = state["continuity_world_state"]
        self.assertEqual(world["pending_location_label"], "Bubble Land")
        self.assertEqual(list(world.get("pending_character_keys", []) or []), [])
        self.assertEqual(list(world.get("pending_prop_keys", []) or []), [])

        result = validate_live_scene_request(
            state,
            "A magical world filled with giant, shimmering bubbles floating gently everywhere.",
        )

        self.assertEqual(result.location_label, "Bubble Land")
        self.assertIn("Bubble Land", result.resolved_description)
        self.assertIn("missing_location_anchor", result.issues)

    def test_same_location_new_character_and_presents_force_new_page(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "Inside the secret room of the licorice castle, a winding candy staircase ends beside "
                "a cozy white couch and swirly wallpaper."
            ),
            storybeat_text="The secret room waits at the top of the winding candy staircase.",
            scene_number=1,
        )
        world = state["continuity_world_state"]
        world["current_location_key"] = "secret_room"
        world["current_location_label"] = "secret room"

        update_continuity_from_child_utterance(
            state,
            "Can we see Santa in the secret room with presents?",
        )
        result = validate_live_scene_request(
            state,
            "Inside the secret room of the licorice castle, Santa smiles beside a pile of wrapped presents.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertIn("Santa", result.resolved_description)
        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "new_character_reveal_same_location")

    def test_current_scene_detail_question_stays_on_current_page(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "In Candy Land, giant frosted cupcakes sit beside the chocolate river while the rainbow path curls onward."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "In Candy Land, giant frosted cupcakes sit beside the chocolate river while the rainbow path curls onward."
            ),
            storybeat_text="Big frosted cupcakes rise beside the chocolate river.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "What are the cupcakes like on this page?")
        result = validate_live_scene_request(
            state,
            "A close-up of giant pink-frosted cupcakes with sprinkles beside the chocolate river in Candy Land.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertFalse(decision.should_render)
        self.assertEqual(decision.reason, "current_scene_detail_chat")

    def test_explicit_visual_request_allows_same_location_redraw(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a tall Christmas tree glows beside toy worktables.",
            storybeat_text="The tree sparkles warmly in Santa's workshop.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can you show me the star on the tree with an image?")
        result = validate_live_scene_request(
            state,
            "A close-up of the glowing star at the top of the Christmas tree in Santa's workshop.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "explicit_visual_request")
        self.assertEqual(decision.reason, "explicit_visual_request")

    def test_what_does_it_look_like_is_not_automatic_redraw_without_visual_ask(self) -> None:
        state: dict[str, object] = {
            "current_scene_description": (
                "Inside Candy Land, giant cupcakes sparkle under a sunny sky beside a chocolate river."
            ),
            "current_scene_visual_summary": (
                "Inside Candy Land, giant cupcakes sparkle under a sunny sky beside a chocolate river."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Candy Land, giant cupcakes sparkle under a sunny sky beside a chocolate river.",
            storybeat_text="The cupcake hills sparkle in Candy Land.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "What do the cupcakes look like?")
        result = validate_live_scene_request(
            state,
            "A close-up of the same giant cupcakes in Candy Land beside the chocolate river.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertFalse(decision.should_render)
        self.assertEqual(decision.reason, "current_scene_detail_chat")

    def test_same_room_get_closer_request_still_allows_new_page(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "Inside Santa's workshop, Santa's chair sits beside wrapped gifts and glowing lanterns."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, Santa's chair sits beside wrapped gifts and glowing lanterns.",
            storybeat_text="Santa's chair glows warmly in the workshop.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can I get closer to Santa's chair?")
        result = validate_live_scene_request(
            state,
            "A closer view of Santa's chair in the same warm workshop beside wrapped gifts and lanterns.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertTrue(decision.should_render)

    def test_structural_transition_allows_new_page_in_same_place(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Outside Santa's workshop, snow glows under the moonlight.",
            storybeat_text="The workshop glimmers in the snow.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we go inside the workshop?")
        result = validate_live_scene_request(
            state,
            "A cozy room full of toy benches and elves inside Santa's workshop.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "structural_transition")

    def test_young_child_mode_keeps_same_location_reveal_on_current_page(self) -> None:
        state: dict[str, object] = {"child_age_band": "4-5"}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside Santa's workshop, a toy train sits by glowing shelves and wrapped gifts.",
            storybeat_text="The toy train waits by the workshop shelves.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "The teddy bear turns sparkly!")
        result = validate_live_scene_request(
            state,
            "A teddy bear in Santa's workshop transforms with swirls of sparkly light beside the toy train.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertFalse(decision.should_render)
        self.assertEqual(decision.reason, "same_location_minor_beat_young_child")

    def test_path_exploration_is_treated_as_new_page_for_young_child(self) -> None:
        state: dict[str, object] = {"child_age_band": "4-5"}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "A swirling rainbow path glows under a big moon, leading toward a playful purple castle "
                "with friendly ghosts in the trees."
            ),
            storybeat_text="The rainbow path leads toward the castle.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Let's explore the rainbow path.")
        result = validate_live_scene_request(
            state,
            "A swirling rainbow path glowing under a big moon, curving deeper through the spooky forest toward the castle.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertEqual(state["continuity_world_state"]["pending_location_label"], "rainbow path")
        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "pending_location_change")
        self.assertNotIn("Treat this as the same place from a new angle", result.prompt_suffix)

    def test_generic_destination_phrase_captures_unlisted_fantasy_place(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)

        update_continuity_from_child_utterance(
            state,
            "Can we go to a Candy Land with a chocolate river?",
        )

        self.assertEqual(
            state["continuity_world_state"]["pending_location_label"],
            "Candy Land",
        )

    def test_generic_destination_phrase_forces_new_scene_for_chocolate_ocean(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        update_continuity_from_child_utterance(
            state,
            "Can we go to a Candy Land with a chocolate river?",
        )
        record_continuity_scene(
            state,
            description=(
                "In Candy Land, a little candy swirl boat floats along a sparkling chocolate river "
                "past lollipop trees and gummy bear bushes."
            ),
            storybeat_text="The candy swirl boat floats gently down the sparkling chocolate river.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Let's go to the chocolate ocean.")
        result = validate_live_scene_request(
            state,
            "At the edge of a wide chocolate ocean, candy waves shimmer under caramel clouds.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertEqual(
            state["continuity_world_state"]["pending_location_label"],
            "chocolate ocean",
        )
        self.assertEqual(result.location_label, "chocolate ocean")
        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "pending_location_change")

    def test_opposite_direction_request_forces_new_page_even_if_castle_is_still_mentioned(self) -> None:
        state: dict[str, object] = {"child_age_band": "4-5"}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "A swirling rainbow path glows under a big moon, leading toward a playful purple castle "
                "with friendly ghosts in the trees."
            ),
            storybeat_text="The rainbow path leads toward the castle.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(
            state,
            "Let's go the opposite direction of the castle. Where does the path go if we go the opposite direction of the castle?",
        )
        result = validate_live_scene_request(
            state,
            "A swirling rainbow path glowing under a big moon, curving away from the purple castle toward shadowy trees and magical flowers.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertEqual(state["continuity_world_state"]["pending_location_label"], "path")
        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "pending_location_change")
        self.assertIn("moving onward to the next stretch of the journey", result.prompt_suffix)

    def test_moon_destination_request_forces_new_scene_from_castle(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "Scary castle. A large, spooky castle silhouetted against a dark, cloudy sky "
                "with a crescent moon and a heavy wooden drawbridge over a deep moat."
            ),
            storybeat_text="A spooky castle stands tall with crooked spires against the dark, cloudy sky.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(
            state,
            "You know what? Instead, let's go fly up to the moon.",
        )
        world = state["continuity_world_state"]

        self.assertEqual(world["pending_location_label"], "moon")

        result = validate_live_scene_request(
            state,
            "A child flying gently toward a large, glowing moon in a dark but friendly starry sky.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertEqual(result.location_label, "moon")
        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "pending_location_change")
        self.assertIn("moon surface", result.prompt_suffix.lower())

    def test_hidden_door_request_keeps_next_scene_adjacent_to_current_world(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Inside the purple castle, a cozy hall glows with lanterns beside a secret little door.",
            storybeat_text="A secret little door waits in the castle hall.",
            visual_summary="Inside the purple castle hall, warm lanterns glow beside a secret little door in the stone wall.",
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we go through the secret door?")
        result = validate_live_scene_request(
            state,
            "A moonlit forest clearing with tall trees and soft grass.",
        )

        self.assertIn("purple castle", result.resolved_description.lower())
        self.assertIn("directly connected space in the same world", result.prompt_suffix)
        self.assertIn("Do not jump to an unrelated forest", result.prompt_suffix)

    def test_open_treasure_chest_keeps_reveal_inside_current_cave(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "Inside a crystal cave behind the waterfall, a treasure chest glows beside blue crystals "
                "and a friendly green blob."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "Inside a crystal cave behind the waterfall, a treasure chest glows beside blue crystals "
                "and a friendly green blob."
            ),
            storybeat_text="Behind the waterfall, the blob finds a glowing treasure chest in the cave.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Can we open the treasure chest?")
        world = state["continuity_world_state"]
        self.assertEqual(world["pending_transition"], "same_room")
        self.assertEqual(world["pending_location_label"], "crystal cave")

        result = validate_live_scene_request(
            state,
            "A sunny forest clearing with the treasure chest open beside moss and red mushrooms.",
        )
        decision = should_render_new_scene_page(
            state,
            result.resolved_description,
            target_location_label=result.location_label,
        )

        self.assertIn("crystal cave", result.resolved_description.lower())
        self.assertIn("requested_location_mismatch", result.issues)
        self.assertIn("same-place reveal", result.prompt_suffix.lower())
        self.assertIn("keep the reveal inside the existing cave", result.prompt_suffix.lower())
        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "structural_transition")

    def test_open_it_after_chest_scene_inherits_current_cave_location(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "Inside a crystal cave behind the waterfall, a treasure chest rests beside blue crystals."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description=(
                "Inside a crystal cave behind the waterfall, a treasure chest rests beside blue crystals "
                "and the happy blob."
            ),
            storybeat_text="The treasure chest waits inside the cave behind the waterfall.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Open it!")
        world = state["continuity_world_state"]

        self.assertEqual(world["pending_transition"], "same_room")
        self.assertEqual(world["pending_location_label"], "crystal cave")
        self.assertTrue(world["pending_prop_keys"])

    def test_persistent_shared_toy_companion_is_carried_into_new_scene(self) -> None:
        state: dict[str, object] = {
            "toy_reference_name_hint": "Lion-O",
            "sidekick_description": "Lion-O, the child's toy companion",
            "character_facts_list": [
                {
                    "character_name": "Lion-O",
                    "fact": "shared toy helper and recurring companion in the story",
                }
            ],
        }
        ensure_story_continuity_state(state)
        prime_character_carryover(
            state,
            ["Lion-O"],
            source="test",
            description="Lion-O, the child's toy companion",
        )
        record_continuity_scene(
            state,
            description="Outside the dark castle, tall stone towers rise under soft gray daylight.",
            storybeat_text="The dark castle waits at the end of the path.",
            scene_number=2,
        )

        active_labels = [
            state["continuity_entity_registry"]["characters"][key]["label"]
            for key in state["continuity_world_state"]["active_character_keys"]
        ]
        self.assertIn("Lion-O", active_labels)

    def test_inferred_structural_transition_allows_new_page_without_pending_state(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Outside the dark castle, crooked towers rise above the path in soft gray daylight.",
            storybeat_text="The dark castle waits ahead.",
            scene_number=1,
        )
        state["last_child_utterance"] = "Can we go inside the castle?"

        decision = should_render_new_scene_page(
            state,
            "A tall entrance hall inside the dark castle glows with lantern light.",
            target_location_label="dark castle",
            render_in_flight=True,
        )

        self.assertTrue(decision.should_render)
        self.assertEqual(decision.reason, "structural_transition")

    def test_structural_transition_prompt_preserves_same_castle_daylight(self) -> None:
        state: dict[str, object] = {
            "current_scene_visual_summary": (
                "Outside the dark castle, charcoal stone towers and purple banners sit under soft gray daylight."
            )
        }
        ensure_story_continuity_state(state)
        record_continuity_scene(
            state,
            description="Outside the dark castle, charcoal stone towers and purple banners stand over the gate.",
            storybeat_text="The dark castle looms quietly under gray daylight.",
            visual_summary=str(state["current_scene_visual_summary"]),
            scene_number=1,
        )

        update_continuity_from_child_utterance(state, "Let's go inside the dark castle.")
        result = validate_live_scene_request(
            state,
            "A bright golden ballroom with sunny windows and pale marble.",
        )

        self.assertIn("same time of day", result.prompt_suffix.lower())
        self.assertIn("architecture, materials, palette", result.prompt_suffix)
        self.assertIn("charcoal stone towers and purple banners", result.prompt_suffix)

    def test_record_scene_strips_leading_of_from_location_label(self) -> None:
        state: dict[str, object] = {}
        ensure_story_continuity_state(state)

        record_continuity_scene(
            state,
            description="Inside the secret room of the licorice castle, Santa smiles beside wrapped presents.",
            storybeat_text="Santa waits inside the licorice castle secret room.",
            scene_number=3,
        )

        world = state["continuity_world_state"]
        self.assertEqual(world["current_location_label"], "licorice castle")
        self.assertIn("Current place: licorice castle", state["continuity_world_state_text"])


if __name__ == "__main__":
    unittest.main()
