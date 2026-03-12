from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

try:
    from google.cloud import firestore as _firestore  # type: ignore
    from google.cloud import storage as _storage  # type: ignore
except Exception:  # pragma: no cover - test env fallback
    import types

    google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud_pkg = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    firestore_stub = sys.modules.setdefault("google.cloud.firestore", types.ModuleType("google.cloud.firestore"))
    storage_stub = sys.modules.setdefault("google.cloud.storage", types.ModuleType("google.cloud.storage"))
    setattr(cloud_pkg, "firestore", firestore_stub)
    setattr(cloud_pkg, "storage", storage_stub)
    setattr(google_pkg, "cloud", cloud_pkg)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agent.tools import (
    VisualArgs,
    _build_visual_continuity_plan,
    _character_crop_references_from_detection,
    _character_reference_images_for_keys,
    _copy_state_mapping,
    _continuity_anchor_text,
    _repair_visual_args_from_audit,
    _recent_scene_reference_entries,
    _remember_character_visual_references,
    _remember_recent_scene_reference,
)


class _FakeToolContext:
    def __init__(self, state: dict[str, object]) -> None:
        self.state = state


class _FakeAdkState:
    def __init__(self, value: dict[str, object]) -> None:
        self._value = value

    def get(self, key: str, default: object = None) -> object:
        return self._value.get(key, default)

    def __getitem__(self, key: object) -> object:
        return self._value[key]

    def __setitem__(self, key: str, value: object) -> None:
        self._value[key] = value


class LiveSceneVisualContinuityTests(unittest.TestCase):
    def test_copy_state_mapping_handles_adk_state_wrapper(self) -> None:
        wrapped = _FakeAdkState({"illustration_style": "storybook", "turn_number": 3})

        copied = _copy_state_mapping(wrapped)

        self.assertEqual(copied["illustration_style"], "storybook")
        self.assertEqual(copied["turn_number"], 3)

    def test_recent_scene_reference_buffer_keeps_last_three_entries(self) -> None:
        state: dict[str, object] = {}

        for idx in range(4):
            _remember_recent_scene_reference(
                state,
                request_id=f"scene-{idx + 1}",
                description=f"Scene description {idx + 1}",
                storybeat_text=f"Story beat {idx + 1}",
                visual_summary=f"Visual summary {idx + 1}",
                thumbnail_b64=f"thumb-{idx + 1}",
                thumbnail_mime="image/jpeg",
            )

        refs = _recent_scene_reference_entries(state)

        self.assertEqual(len(refs), 3)
        self.assertEqual([ref["request_id"] for ref in refs], ["scene-2", "scene-3", "scene-4"])

    def test_continuity_anchor_text_includes_recent_image_trail(self) -> None:
        state: dict[str, object] = {
            "story_summary": "The child explores deeper into the castle.",
            "scene_descriptions": [
                "A purple castle hall glows with lanterns.",
                "A secret door opens in the wall.",
                "A hidden stair curls upward behind the door.",
            ],
        }
        _remember_recent_scene_reference(
            state,
            request_id="scene-1",
            description="Castle hall",
            storybeat_text="The castle hall glows softly.",
            visual_summary="Warm lanterns glow in the purple castle hall.",
            thumbnail_b64="thumb-1",
            thumbnail_mime="image/jpeg",
        )
        _remember_recent_scene_reference(
            state,
            request_id="scene-2",
            description="Secret door",
            storybeat_text="A secret door swings open in the stone wall.",
            visual_summary="A secret door opens beside the lanterns in the stone wall.",
            thumbnail_b64="thumb-2",
            thumbnail_mime="image/jpeg",
        )

        anchor_text = _continuity_anchor_text(_FakeToolContext(state))

        self.assertIn("recent image trail (oldest to newest)", anchor_text)
        self.assertIn("secret door opens", anchor_text.lower())

    def test_character_bible_keeps_latest_two_references_per_character(self) -> None:
        state: dict[str, object] = {
            "continuity_entity_registry": {
                "characters": {
                    "friendly_dragon": {"label": "friendly dragon"},
                },
                "locations": {},
                "props": {},
            },
            "continuity_world_state": {
                "active_character_keys": ["friendly_dragon"],
            },
            "continuity_scene_history": [],
        }

        for idx in range(3):
            state["continuity_scene_history"].append(
                {
                    "scene_number": idx + 1,
                    "request_id": f"scene-{idx + 1}",
                    "character_keys": ["friendly_dragon"],
                }
            )
            _remember_character_visual_references(
                state,
                request_id=f"scene-{idx + 1}",
                description=f"Scene {idx + 1}",
                storybeat_text=f"The friendly dragon appears in scene {idx + 1}.",
                visual_summary=f"The friendly dragon glows purple in scene {idx + 1}.",
                thumbnail_b64=f"dragon-thumb-{idx + 1}",
                thumbnail_mime="image/jpeg",
                scene_number=idx + 1,
            )

        dragon_entry = state["character_bible"]["friendly_dragon"]
        refs = dragon_entry["reference_images"]
        self.assertEqual(len(refs), 2)
        self.assertEqual([ref["request_id"] for ref in refs], ["scene-2", "scene-3"])
        self.assertIn("colors: purple", dragon_entry["canonical_visual_traits"])

        reference_images = _character_reference_images_for_keys(state, ["friendly_dragon"])
        self.assertEqual(len(reference_images), 2)
        self.assertEqual(reference_images[-1]["role"], "character_friendly_dragon_scene_thumbnail")

    def test_character_bible_prefers_focused_crop_reference_when_available(self) -> None:
        state: dict[str, object] = {
            "continuity_entity_registry": {
                "characters": {
                    "friendly_dragon": {"label": "friendly dragon"},
                },
                "locations": {},
                "props": {},
            },
            "continuity_world_state": {
                "active_character_keys": ["friendly_dragon"],
            },
            "continuity_scene_history": [
                {
                    "scene_number": 1,
                    "request_id": "scene-1",
                    "character_keys": ["friendly_dragon"],
                }
            ],
        }

        _remember_character_visual_references(
            state,
            request_id="scene-1",
            description="Inside the castle hall.",
            storybeat_text="The friendly dragon watches the secret door.",
            visual_summary="The friendly dragon glows purple beside the stone wall.",
            thumbnail_b64="scene-thumb",
            thumbnail_mime="image/jpeg",
            focused_reference_images=[
                {
                    "character_key": "friendly_dragon",
                    "focus_label": "friendly dragon",
                    "thumbnail_b64": "crop-thumb",
                    "thumbnail_mime": "image/jpeg",
                    "reference_kind": "character_crop",
                    "crop_box": {"x": 120, "y": 160, "width": 300, "height": 420},
                }
            ],
            scene_number=1,
        )

        dragon_entry = state["character_bible"]["friendly_dragon"]
        self.assertEqual(dragon_entry["reference_images"][-1]["reference_kind"], "character_crop")
        self.assertEqual(dragon_entry["reference_images"][-1]["thumbnail_b64"], "crop-thumb")

        reference_images = _character_reference_images_for_keys(state, ["friendly_dragon"])
        self.assertEqual(reference_images[-1]["role"], "character_friendly_dragon_character_crop")

    def test_character_crop_detection_payload_produces_cropped_thumbnail(self) -> None:
        try:
            from PIL import Image
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"Pillow unavailable: {exc}")

        image = Image.new("RGB", (200, 120), color=(120, 80, 200))
        out = BytesIO()
        image.save(out, format="JPEG")

        crop_refs = _character_crop_references_from_detection(
            image_bytes=out.getvalue(),
            detection_payload={
                "crops": [
                    {
                        "character_key": "friendly_dragon",
                        "label": "friendly dragon",
                        "visible": True,
                        "x": 250,
                        "y": 100,
                        "width": 300,
                        "height": 600,
                    }
                ]
            },
        )

        self.assertEqual(len(crop_refs), 1)
        self.assertEqual(crop_refs[0]["reference_kind"], "character_crop")
        self.assertEqual(crop_refs[0]["focus_label"], "friendly dragon")
        self.assertTrue(crop_refs[0]["thumbnail_b64"])

    def test_visual_continuity_plan_uses_active_roster_and_required_props(self) -> None:
        state: dict[str, object] = {
            "continuity_entity_registry": {
                "characters": {
                    "friendly_dragon": {"label": "friendly dragon"},
                },
                "locations": {},
                "props": {
                    "golden_key": {"label": "golden key"},
                },
            },
            "continuity_world_state": {
                "current_location_label": "purple castle",
                "active_character_keys": ["friendly_dragon"],
                "active_prop_keys": ["golden_key"],
                "pending_transition": "door",
            },
            "current_scene_visual_summary": "Inside the purple castle, a friendly dragon curls beside a golden key.",
            "character_bible": {
                "friendly_dragon": {
                    "character_key": "friendly_dragon",
                    "label": "friendly dragon",
                    "species": "dragon",
                    "role": "recurring_creature",
                    "canonical_visual_traits": ["colors: purple", "traits: friendly"],
                    "outfit_accessories": ["key"],
                    "latest_visual_summary": "The friendly dragon glows purple beside a golden key.",
                    "reference_images": [],
                }
            },
        }

        plan = _build_visual_continuity_plan(
            state,
            validation=SimpleNamespace(
                location_label="purple castle",
                character_keys=["friendly_dragon"],
                prop_keys=["golden_key"],
            ),
            request_description="Go through the secret door in the purple castle.",
        )

        self.assertEqual(plan["target_location"], "purple castle")
        self.assertEqual(plan["transition_type"], "door")
        self.assertEqual(plan["active_character_labels"], ["friendly dragon"])
        self.assertEqual(plan["required_prop_labels"], ["golden key"])
        self.assertTrue(any("friendly dragon" in item.lower() for item in plan["forbidden_drift"]))
        self.assertTrue(any("purple castle" in item.lower() for item in plan["forbidden_drift"]))

    def test_visual_audit_repair_updates_prompt_text(self) -> None:
        args = VisualArgs(
            description="Inside the purple castle, the friendly dragon sleeps beside a secret door.",
            base_description="Inside the purple castle, the friendly dragon sleeps beside a secret door.",
            negative_prompt="no text",
        )

        repaired = _repair_visual_args_from_audit(
            args,
            {
                "status": "repair",
                "should_retry": True,
                "repair_prompt_suffix": "Keep the dragon inside the castle hall and preserve the same purple colors.",
                "issues": [],
            },
        )

        self.assertIsNotNone(repaired)
        self.assertIn("Repair continuity", repaired.description)
        self.assertIn("preserve the same purple colors", repaired.base_description)

if __name__ == "__main__":
    unittest.main()
