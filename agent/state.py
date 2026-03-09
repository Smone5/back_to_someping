"""Versioned Pydantic state models for ADK session.state and Firestore persistence.

CRITICAL: All models use @model_validator(mode='before') to handle schema
evolution gracefully. This prevents ValidationError crashes when reading old
Firestore sessions after future schema updates (Iteration 10, Data Integrity Audit).
"""

from __future__ import annotations

from typing import Any
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CharacterFact(BaseModel):
    """A single canonical fact about a story character.

    Stored in session.state['character_facts'] as a list.
    Versioned for backward-compatible Firestore reads.
    """

    model_config = ConfigDict(extra="ignore")  # Ignore hallucinated LLM kwargs

    schema_version: int = Field(default=1)
    character_name: str
    fact: str

    @model_validator(mode="before")
    @classmethod
    def migrate_schema(cls, data: Any) -> Any:
        """Handle old schema versions from Firestore reads."""
        if isinstance(data, dict):
            # v0 -> v1 migration: older records may not have schema_version
            if "schema_version" not in data:
                data["schema_version"] = 1
            # Future v1->v2 migrations go here
        return data


class StorySession(BaseModel):
    """The complete, versioned state of a single storytelling session.

    This is the structure stored in ADK session.state AND mirrored to Firestore
    for cross-session Lore persistence (Iteration 8, Contest Winner Audit).
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: int = Field(default=1)
    child_name: str = Field(default="friend")
    child_age: int = Field(default=4)
    child_age_band: str = Field(default="4-5")
    sidekick_description: str = Field(default="a brave sidekick")
    toy_share_active: bool = Field(default=False)
    toy_share_turns_remaining: int = Field(default=0)
    toy_reference_visual_summary: str = Field(default="")
    story_summary: str = Field(default="The adventure is just beginning...")
    story_tone: str = Field(default="cozy")
    story_phase: str = Field(default="opening")
    assembly_kind: str = Field(default="initial")
    scene_render_pending: bool = Field(default=False)
    theater_release_ready: bool = Field(default=False)
    child_delight_anchors: list[str] = Field(default_factory=list)
    child_delight_anchors_text: str = Field(default="None saved yet.")
    continuity_entity_registry: dict[str, Any] = Field(
        default_factory=lambda: {"characters": {}, "locations": {}, "props": {}}
    )
    continuity_world_state: dict[str, Any] = Field(
        default_factory=lambda: {
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
        }
    )
    continuity_scene_history: list[dict[str, Any]] = Field(default_factory=list)
    continuity_registry_text: str = Field(default="No recurring entities tracked yet.")
    continuity_world_state_text: str = Field(default="No scene-to-scene world state established yet.")
    current_scene_visual_summary: str = Field(default="")
    previous_scene_visual_summary: str = Field(default="")
    canonical_scene_visual_summary: str = Field(default="")
    character_facts: list[CharacterFact] = Field(default_factory=list)
    turn_number: int = Field(default=0)
    response_turn_number: int = Field(default=0)
    generated_asset_urls: list[str] = Field(
        default_factory=list,
        description="GCS URLs of all Veo 3.1 / Nano Banana 2 generated assets this session.",
    )
    elevenlabs_audio_chunks: list[str] = Field(
        default_factory=list,
        description="GCS URLs of per-turn ElevenLabs audio tracks for final FFmpeg assembly.",
    )
    last_active_hex_color: str = Field(
        default="#FFE5B4",
        description="Last IoT lighting color, used to prevent re-triggering identical scenes.",
    )
    # Snapshot stack for rewind state desync fix (Iteration 6, Logic Bomb Audit)
    state_snapshots: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Turn-indexed snapshots of state for perfect rewind reconciliation.",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_schema(cls, data: Any) -> Any:
        """Handle Firestore reads from older schema versions."""
        if isinstance(data, dict):
            if "schema_version" not in data:
                data["schema_version"] = 1
            # v1 didn't have state_snapshots — backfill with empty list
            if "state_snapshots" not in data:
                data["state_snapshots"] = []
            # v1 didn't have elevenlabs_audio_chunks
            if "elevenlabs_audio_chunks" not in data:
                data["elevenlabs_audio_chunks"] = []
            if "story_tone" not in data:
                data["story_tone"] = "cozy"
            if "child_age" not in data:
                data["child_age"] = 4
            if "child_age_band" not in data:
                data["child_age_band"] = "4-5"
            if "story_phase" not in data:
                data["story_phase"] = "opening"
            if "assembly_kind" not in data:
                data["assembly_kind"] = "initial"
            if "scene_render_pending" not in data:
                data["scene_render_pending"] = False
            if "theater_release_ready" not in data:
                data["theater_release_ready"] = False
            if "child_delight_anchors" not in data:
                data["child_delight_anchors"] = []
            if "child_delight_anchors_text" not in data:
                data["child_delight_anchors_text"] = "None saved yet."
            if "continuity_entity_registry" not in data:
                data["continuity_entity_registry"] = {"characters": {}, "locations": {}, "props": {}}
            if "continuity_world_state" not in data:
                data["continuity_world_state"] = {
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
                }
            if "continuity_scene_history" not in data:
                data["continuity_scene_history"] = []
            if "continuity_registry_text" not in data:
                data["continuity_registry_text"] = "No recurring entities tracked yet."
            if "continuity_world_state_text" not in data:
                data["continuity_world_state_text"] = "No scene-to-scene world state established yet."
            if "current_scene_visual_summary" not in data:
                data["current_scene_visual_summary"] = ""
            if "previous_scene_visual_summary" not in data:
                data["previous_scene_visual_summary"] = ""
            if "canonical_scene_visual_summary" not in data:
                data["canonical_scene_visual_summary"] = ""
            if "response_turn_number" not in data:
                data["response_turn_number"] = data.get("turn_number", 0)
        return data

    def take_snapshot(self) -> None:
        """Save current state as a turn-indexed snapshot before any mutation.

        Called at the start of every new turn to support clean rewind rollback.
        """
        snapshot = self.model_dump(exclude={"state_snapshots"})
        self.state_snapshots.append(snapshot)

    def rollback_to_turn(self, turn_index: int) -> "StorySession":
        """Restore a prior snapshot for the given turn index."""
        if turn_index < len(self.state_snapshots):
            prior = self.state_snapshots[turn_index]
            # Preserve snapshots up to this turn
            prior["state_snapshots"] = self.state_snapshots[: turn_index + 1]
            return StorySession.model_validate(prior)
        return self

    def formatted_character_facts(self) -> str:
        """Returns character facts as a formatted string for the system prompt."""
        if not self.character_facts:
            return "None established yet."
        return "\n".join(
            f"- {cf.character_name}: {cf.fact}" for cf in self.character_facts
        )
