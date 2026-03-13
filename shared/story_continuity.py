"""Structured continuity helpers for live story scenes and final assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import re

from .storybook_pages import story_pages_from_state_data

_MAX_ENTITY_TEXT = 120
_MAX_HISTORY = 40

_DEFAULT_REGISTRY: dict[str, dict[str, dict[str, Any]]] = {
    "characters": {},
    "locations": {},
    "props": {},
}

_DEFAULT_WORLD_STATE: dict[str, Any] = {
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

_DEFAULT_REGISTRY_TEXT = "No recurring entities tracked yet."
_DEFAULT_WORLD_TEXT = "No scene-to-scene world state established yet."

_STOPWORDS = {
    "a", "an", "and", "around", "at", "be", "big", "bright", "by", "can", "cozy",
    "for", "from", "giant", "go", "happy", "hello", "i", "im", "inside", "into", "is",
    "it", "its", "just", "little", "look", "me", "my", "of", "on", "outside", "see",
    "soft", "super", "that", "the", "their", "there", "they", "this", "to", "warm",
    "we", "what", "with", "you",
}

_LOCATION_TERMS = [
    "santa's workshop",
    "santa claus workshop",
    "throne room",
    "workshop",
    "castle",
    "space",
    "library",
    "forest",
    "garden",
    "kitchen",
    "tower",
    "room",
    "kingdom",
    "window",
    "pond",
    "fireplace",
    "cave",
    "palace",
    "bridge",
    "path",
    "trail",
    "road",
    "boat",
    "ship",
    "slide",
    "treehouse",
    "village",
    "beach",
    "moon",
]

_ANIMATE_TERMS = [
    "santa claus",
    "santa",
    "elves",
    "elf",
    "bubble",
    "dragon",
    "ghost",
    "witch",
    "wizard",
    "fox king",
    "king",
    "queen",
    "princess",
    "prince",
    "hero",
    "sidekick",
    "reindeer",
    "train",
]

_PROP_TERMS = [
    "treasure chest",
    "chest",
    "bubble",
    "bubbles",
    "fireplace",
    "window",
    "throne",
    "book",
    "books",
    "gift",
    "gifts",
    "toy",
    "toys",
    "bench",
    "worktable",
    "worktables",
    "lantern",
    "door",
    "bridge",
    "cauldron",
    "chair",
    "couch",
]

_TRANSITION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("inside", re.compile(r"\b(?:inside|go inside|enter|step inside)\b", flags=re.IGNORECASE)),
    ("outside", re.compile(r"\b(?:outside|go outside|walk around the outside)\b", flags=re.IGNORECASE)),
    ("window", re.compile(r"\b(?:window|look out|look at a window|peek out)\b", flags=re.IGNORECASE)),
    ("door", re.compile(r"\b(?:door|big door|knock)\b", flags=re.IGNORECASE)),
    ("tower", re.compile(r"\b(?:tower|go up|go upstairs|climb)\b", flags=re.IGNORECASE)),
]

_LOCATION_SIGNATURE_HINTS = {
    "workshop": "show worktables, toy parts, wrapped gifts, shelves, and warm workshop light",
    "castle": "show unmistakable towers, halls, banners, doors, or stonework",
    "space": "show stars, planets, nebula colors, and a clear zero-gravity or outer-space setting",
    "library": "show bookshelves, reading chairs, lamps, and books",
    "forest": "show trees, paths, leaves, and clear woodland depth",
    "garden": "show flowers, winding paths, plants, and cozy outdoor details",
    "kitchen": "show counters, cooking tools, warm lights, and food details",
    "window": "show the view clearly framed by the window and the room it belongs to",
    "moon": "show the moon surface, craters, soft stardust, and a clear view of outer space or Earth",
}

_INSIDE_HINTS = (
    "inside",
    "indoors",
    "interior",
    "room",
    "workshop",
    "library",
    "kitchen",
    "hall",
    "throne room",
    "fireplace",
)
_OUTSIDE_HINTS = (
    "outside",
    "outdoors",
    "sky",
    "garden",
    "forest",
    "yard",
    "beach",
    "moonlit",
    "snowy field",
)

_EXPLICIT_VISUAL_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bshow (?:me|us)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:draw|picture|image|illustration)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:with|in) an image\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:can|could) (?:i|we) see (?:it|that|the picture|the image|a picture|an image)\b", flags=re.IGNORECASE),
    re.compile(r"\bwhat does .* look like\b", flags=re.IGNORECASE),
    re.compile(r"\btake me there\b", flags=re.IGNORECASE),
)

_NAVIGATION_PAGE_TURN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:follow|explore|head|travel|venture|go|walk|run)\b.*\b(?:path|trail|road|way|direction)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:down|along|through|toward|towards|away from)\s+the\s+(?:path|trail|road|way)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:opposite direction|other direction|another way)\b",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"\bwhere does\b.*\b(?:path|trail|road|way)\b.*\bgo\b",
        flags=re.IGNORECASE,
    ),
)

_ROUTE_FOCUS_TERMS = (
    "path",
    "trail",
    "road",
    "bridge",
)

_MAJOR_VISUAL_CHANGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:transform|transformation|turn into|turns into|become|becomes|became)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:reveal|reveals|discovery|discover|suddenly)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:open|opens|opening)\b.*\b(?:door|portal|chest|gate)\b", flags=re.IGNORECASE),
    re.compile(r"\b(?:portal|secret room|hidden room|hidden door|magic door)\b", flags=re.IGNORECASE),
)


@dataclass(frozen=True)
class ContinuityValidationResult:
    resolved_description: str
    prompt_suffix: str
    issues: list[str]
    location_key: str
    location_label: str
    character_keys: list[str]
    prop_keys: list[str]


@dataclass(frozen=True)
class SceneRenderDecision:
    should_render: bool
    reason: str


def _clean_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:500]


def _safe_words(text: str) -> list[str]:
    return [token for token in re.findall(r"[a-zA-Z][a-zA-Z'\-]+", text.lower()) if token not in _STOPWORDS]


def _stable_key(label: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return cleaned[:64] or "unknown"


def _sentence_case(text: str) -> str:
    cleaned = _clean_text(text)
    if not cleaned:
        return cleaned
    return cleaned[0].upper() + cleaned[1:]


def _canonical_label(text: str) -> str:
    cleaned = _clean_text(text)
    cleaned = re.sub(
        r"^(?:let'?s|can we|could we|should we|do we|where does|where do|what(?:'s| is)|show me|take me)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:go|walk|run|head|travel|venture|explore|follow|see|look|step|enter|fly)\s+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"^(?:(?:inside|outside|at|in|into|through|from|to|toward|towards|along|down|up)\s+)+",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
    return cleaned[:_MAX_ENTITY_TEXT]


def _extract_phrase_candidates(text: str, terms: list[str]) -> list[str]:
    cleaned = _clean_text(text)
    lowered = cleaned.lower()
    matches: list[str] = []
    for term in sorted(terms, key=len, reverse=True):
        if term not in lowered:
            continue
        pattern = re.compile(
            rf"\b((?:[A-Za-z][A-Za-z'\-]+\s+){{0,3}}{re.escape(term)})\b",
            flags=re.IGNORECASE,
        )
        for match in pattern.finditer(cleaned):
            phrase = _canonical_label(match.group(1))
            if phrase and phrase.lower() not in {item.lower() for item in matches}:
                matches.append(phrase)
    return matches


def _extract_location_candidates(text: str) -> list[str]:
    return _extract_phrase_candidates(text, _LOCATION_TERMS)


def _extract_character_candidates(text: str, state: Mapping[str, Any]) -> list[str]:
    matches = _extract_phrase_candidates(text, _ANIMATE_TERMS)
    facts = state.get("character_facts_list", [])
    if isinstance(facts, list):
        for entry in facts:
            if not isinstance(entry, Mapping):
                continue
            name = _clean_text(entry.get("character_name", ""))
            if name and re.search(rf"\b{re.escape(name)}\b", text, flags=re.IGNORECASE):
                canonical = _canonical_label(name)
                if canonical.lower() not in {item.lower() for item in matches}:
                    matches.append(canonical)
    sidekick = _clean_text(state.get("sidekick_description", ""))
    if sidekick:
        for candidate in _extract_phrase_candidates(sidekick, _ANIMATE_TERMS):
            if candidate.lower() not in {item.lower() for item in matches}:
                matches.append(candidate)
    return matches


def _extract_prop_candidates(text: str) -> list[str]:
    return _extract_phrase_candidates(text, _PROP_TERMS)


def _detect_transition(text: str) -> str:
    for name, pattern in _TRANSITION_PATTERNS:
        if pattern.search(text or ""):
            return name
    return ""


def _matching_entity_keys_for_fragment(
    state: Mapping[str, Any],
    bucket: str,
    fragment: str,
    *,
    candidate_keys: list[str] | None = None,
) -> list[str]:
    fragment_clean = _clean_text(fragment).lower()
    if not fragment_clean:
        return []
    registry = state.get("continuity_entity_registry", {})
    if not isinstance(registry, Mapping):
        return []
    bucket_map = registry.get(bucket, {})
    if not isinstance(bucket_map, Mapping):
        return []

    allowed_keys = {str(key).strip() for key in list(candidate_keys or []) if str(key).strip()}
    matches: list[str] = []
    for raw_key, raw_entity in bucket_map.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if allowed_keys and key not in allowed_keys:
            continue
        if not isinstance(raw_entity, Mapping):
            continue
        label = _clean_text(raw_entity.get("label", "")).lower()
        aliases = [
            _clean_text(item).lower()
            for item in list(raw_entity.get("aliases", []) or [])
            if _clean_text(item)
        ]
        haystacks = [label, *aliases]
        if any(fragment_clean in candidate for candidate in haystacks if candidate):
            matches.append(key)
    return matches


def _is_same_place_reveal_request(state: Mapping[str, Any], text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return False
    lowered = cleaned.lower()
    if _extract_location_candidates(cleaned) or _is_navigation_page_turn_request(cleaned):
        return False
    reveal_action = bool(
        re.search(
            r"\b(?:open|unlock|peek|look|see|show|lift|what(?:'s| is))\b",
            cleaned,
            flags=re.IGNORECASE,
        )
    )
    if not reveal_action:
        return False
    if re.search(r"\b(?:treasure chest|chest)\b", cleaned, flags=re.IGNORECASE):
        return True
    if not re.search(r"\b(?:it|inside|in|into)\b", lowered):
        return False
    world = state.get("continuity_world_state", {})
    active_prop_keys = []
    if isinstance(world, Mapping):
        active_prop_keys = [str(item).strip() for item in list(world.get("active_prop_keys", []) or []) if str(item).strip()]
    if _matching_entity_keys_for_fragment(state, "props", "chest", candidate_keys=active_prop_keys):
        return True
    current_scene_bits = " ".join(
        _clean_text(state.get(key, ""))
        for key in ("current_scene_description", "current_scene_visual_summary", "previous_scene_visual_summary")
    ).lower()
    return "treasure chest" in current_scene_bits or re.search(r"\bchest\b", current_scene_bits) is not None


def _registry_bucket(state: dict[str, Any], bucket: str) -> dict[str, dict[str, Any]]:
    registry = state.setdefault("continuity_entity_registry", {})
    current = registry.get(bucket)
    if not isinstance(current, dict):
        current = {}
        registry[bucket] = current
    return current


def _register_entity(
    state: dict[str, Any],
    *,
    bucket: str,
    label: str,
    source: str,
    scene_number: int | None = None,
    description: str = "",
) -> str:
    label = _canonical_label(label)
    if not label:
        return ""
    key = _stable_key(label)
    bucket_map = _registry_bucket(state, bucket)
    entity = dict(bucket_map.get(key) or {})
    entity["key"] = key
    entity["label"] = entity.get("label") or label
    aliases = [str(item).strip() for item in list(entity.get("aliases", []) or []) if str(item).strip()]
    if label.lower() not in {item.lower() for item in aliases}:
        aliases.append(label)
    entity["aliases"] = aliases[-6:]
    entity["source"] = entity.get("source") or source
    entity["last_source"] = source
    if scene_number and scene_number > 0:
        entity["first_scene"] = int(entity.get("first_scene") or scene_number)
        entity["last_scene"] = scene_number
    if description:
        entity["last_description"] = _clean_text(description)[:220]
    bucket_map[key] = entity
    return key


def _entity_label(state: Mapping[str, Any], bucket: str, key: str) -> str:
    registry = state.get("continuity_entity_registry", {})
    if not isinstance(registry, Mapping):
        return key
    bucket_map = registry.get(bucket, {})
    if not isinstance(bucket_map, Mapping):
        return key
    entity = bucket_map.get(key, {})
    if not isinstance(entity, Mapping):
        return key
    label = _clean_text(entity.get("label", key))
    return label or key


def _list_entity_labels(state: Mapping[str, Any], bucket: str, keys: list[str]) -> list[str]:
    labels: list[str] = []
    for key in keys:
        label = _entity_label(state, bucket, key)
        if label and label not in labels:
            labels.append(label)
    return labels


def _location_hint(label: str) -> str:
    lowered = label.lower()
    for token, hint in _LOCATION_SIGNATURE_HINTS.items():
        if token in lowered:
            return hint
    return ""


def _mentions_label(text: str, label: str) -> bool:
    if not text or not label:
        return False
    return bool(re.search(rf"\b{re.escape(label)}\b", text, flags=re.IGNORECASE))


def _locations_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    a_words = set(_safe_words(a))
    b_words = set(_safe_words(b))
    return bool(a_words and b_words and (a_words == b_words or len(a_words & b_words) >= min(2, len(a_words), len(b_words))))


def _scene_space_mode(text: str) -> str:
    lowered = _clean_text(text).lower()
    if not lowered:
        return ""
    if lowered.startswith("inside ") or " looking out through a window" in lowered:
        return "inside"
    if re.search(r"\b(?:outside|outdoors)\b", lowered):
        return "outside"
    if any(token in lowered for token in _INSIDE_HINTS):
        return "inside"
    if any(token in lowered for token in _OUTSIDE_HINTS):
        return "outside"
    return ""


def _is_explicit_visual_request(text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in _EXPLICIT_VISUAL_REQUEST_PATTERNS)


def _is_navigation_page_turn_request(text: str) -> bool:
    cleaned = _clean_text(text)
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in _NAVIGATION_PAGE_TURN_PATTERNS)


def _is_route_focus_location(label: str) -> bool:
    lowered = _clean_text(label).lower()
    return any(term in lowered for term in _ROUTE_FOCUS_TERMS)


def _select_location_candidate(
    text: str,
    locations: list[str],
    *,
    current_location_label: str = "",
) -> str:
    if not locations:
        return ""
    if _is_navigation_page_turn_request(text):
        for location in locations:
            if _is_route_focus_location(location):
                return location
        for location in locations:
            if current_location_label and not _locations_match(location, current_location_label):
                return location
    return locations[0]


def _has_major_visual_change_signal(*texts: str) -> bool:
    combined = _clean_text(" ".join(text for text in texts if text))
    if not combined:
        return False
    return any(pattern.search(combined) for pattern in _MAJOR_VISUAL_CHANGE_PATTERNS)


def should_render_new_scene_page(
    state: dict[str, Any],
    description: str,
    *,
    target_location_label: str = "",
    render_in_flight: bool = False,
) -> SceneRenderDecision:
    ensure_story_continuity_state(state)
    cleaned_description = _clean_text(description)
    world = state["continuity_world_state"]
    current_location_label = _clean_text(world.get("current_location_label", ""))
    pending_location_label = _clean_text(world.get("pending_location_label", ""))
    pending_transition = _clean_text(world.get("pending_transition", ""))
    child_text = _clean_text(state.get("last_child_utterance", "") or world.get("pending_request", ""))
    proposed_location_label = _clean_text(target_location_label)
    navigation_request = _is_navigation_page_turn_request(child_text)
    child_age_band = _clean_text(state.get("child_age_band", ""))
    younger_child_mode = child_age_band == "4-5"

    if not proposed_location_label:
        proposed_locations = _extract_location_candidates(cleaned_description)
        proposed_location_label = _select_location_candidate(
            cleaned_description,
            proposed_locations,
            current_location_label=current_location_label,
        )

    if not current_location_label and not str(state.get("current_scene_description", "") or "").strip():
        return SceneRenderDecision(True, "first_page")

    if _is_explicit_visual_request(child_text):
        return SceneRenderDecision(True, "explicit_visual_request")

    if pending_transition:
        return SceneRenderDecision(True, "structural_transition")

    if (
        pending_location_label
        and current_location_label
        and not _locations_match(pending_location_label, current_location_label)
    ):
        return SceneRenderDecision(True, "pending_location_change")

    if (
        proposed_location_label
        and current_location_label
        and not _locations_match(proposed_location_label, current_location_label)
    ):
        return SceneRenderDecision(True, "location_change")

    same_location = bool(
        current_location_label
        and proposed_location_label
        and _locations_match(current_location_label, proposed_location_label)
    )
    if same_location:
        if navigation_request:
            return SceneRenderDecision(True, "navigation_request_same_location")
        if render_in_flight:
            return SceneRenderDecision(False, "same_location_while_rendering")
        if younger_child_mode:
            return SceneRenderDecision(False, "same_location_minor_beat_young_child")
        if _has_major_visual_change_signal(child_text, cleaned_description):
            return SceneRenderDecision(True, "major_visual_change_same_location")
        return SceneRenderDecision(False, "same_location_minor_beat")

    if _has_major_visual_change_signal(child_text, cleaned_description):
        return SceneRenderDecision(True, "major_visual_change")

    return SceneRenderDecision(True, "default_allow")


def _prepend_location_anchor(description: str, label: str, transition: str) -> str:
    core = _clean_text(description).rstrip(".")
    anchor = _sentence_case(label)
    if not core:
        return anchor
    if transition == "inside":
        return f"Inside {anchor}, {core[0].lower() + core[1:]}"
    if transition == "outside":
        return f"Outside {anchor}, {core[0].lower() + core[1:]}"
    if transition == "window":
        return f"Inside {anchor}, looking out through a window, {core[0].lower() + core[1:]}"
    if transition == "door":
        return f"At the door of {anchor}, {core[0].lower() + core[1:]}"
    if transition == "tower":
        return f"Inside the tower of {anchor}, {core[0].lower() + core[1:]}"
    return f"{anchor}. {core}"


def _scene_number_hint(state: Mapping[str, Any]) -> int:
    history = state.get("continuity_scene_history", [])
    if isinstance(history, list):
        return len(history) + 1
    return max(1, int(state.get("turn_number", 1) or 1))


def refresh_continuity_text_fields(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        return
    world = state.get("continuity_world_state", {})
    if not isinstance(world, dict):
        world = {}
    registry = state.get("continuity_entity_registry", {})
    if not isinstance(registry, dict):
        registry = {}

    current_location = _clean_text(world.get("current_location_label", ""))
    previous_location = _clean_text(world.get("previous_location_label", ""))
    active_characters = _list_entity_labels(state, "characters", list(world.get("active_character_keys", []) or []))
    active_props = _list_entity_labels(state, "props", list(world.get("active_prop_keys", []) or []))
    pending_request = _clean_text(world.get("pending_request", ""))
    last_transition = _clean_text(world.get("last_transition", ""))
    current_visual_summary = _clean_text(state.get("current_scene_visual_summary", ""))

    world_bits: list[str] = []
    if current_location:
        world_bits.append(f"Current place: {current_location}")
    if previous_location:
        world_bits.append(f"Previous place: {previous_location}")
    if active_characters:
        world_bits.append(f"Active characters: {', '.join(active_characters[:4])}")
    if active_props:
        world_bits.append(f"Recurring props: {', '.join(active_props[:4])}")
    if last_transition:
        world_bits.append(f"Last transition: {last_transition}")
    if current_visual_summary:
        world_bits.append(f"Visible scene anchor: {current_visual_summary[:140]}")
    if pending_request:
        world_bits.append(f"Pending child wish: {pending_request[:120]}")
    state["continuity_world_state_text"] = " | ".join(world_bits) if world_bits else _DEFAULT_WORLD_TEXT

    registry_bits: list[str] = []
    characters = registry.get("characters", {})
    if isinstance(characters, Mapping) and characters:
        labels = [_clean_text(item.get("label", "")) for item in characters.values() if isinstance(item, Mapping)]
        labels = [label for label in labels if label][:5]
        if labels:
            registry_bits.append(f"Characters: {', '.join(labels)}")
    locations = registry.get("locations", {})
    if isinstance(locations, Mapping) and locations:
        labels = [_clean_text(item.get("label", "")) for item in locations.values() if isinstance(item, Mapping)]
        labels = [label for label in labels if label][:5]
        if labels:
            registry_bits.append(f"Locations: {', '.join(labels)}")
    props = registry.get("props", {})
    if isinstance(props, Mapping) and props:
        labels = [_clean_text(item.get("label", "")) for item in props.values() if isinstance(item, Mapping)]
        labels = [label for label in labels if label][:5]
        if labels:
            registry_bits.append(f"Props: {', '.join(labels)}")
    state["continuity_registry_text"] = " | ".join(registry_bits) if registry_bits else _DEFAULT_REGISTRY_TEXT


def ensure_story_continuity_state(
    state: dict[str, Any] | None,
    *,
    bootstrap: bool = True,
) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}

    registry = state.get("continuity_entity_registry")
    if not isinstance(registry, dict):
        registry = {}
        state["continuity_entity_registry"] = registry
    for bucket, default_value in _DEFAULT_REGISTRY.items():
        if not isinstance(registry.get(bucket), dict):
            registry[bucket] = dict(default_value)

    world = state.get("continuity_world_state")
    if not isinstance(world, dict):
        world = {}
        state["continuity_world_state"] = world
    for key, value in _DEFAULT_WORLD_STATE.items():
        if key not in world:
            world[key] = value if not isinstance(value, list) else list(value)

    history = state.get("continuity_scene_history")
    if not isinstance(history, list):
        history = []
        state["continuity_scene_history"] = history

    if bootstrap:
        _bootstrap_continuity_from_state(state)

    refresh_continuity_text_fields(state)
    return state


def _bootstrap_continuity_from_state(state: dict[str, Any]) -> None:
    history = state.get("continuity_scene_history", [])
    if isinstance(history, list) and history:
        return

    scene_number = 0
    facts = state.get("character_facts_list", [])
    if isinstance(facts, list):
        for entry in facts:
            if not isinstance(entry, Mapping):
                continue
            label = _clean_text(entry.get("character_name", ""))
            fact = _clean_text(entry.get("fact", ""))
            if label:
                _register_entity(state, bucket="characters", label=label, source="character_fact", description=fact)

    sidekick = _clean_text(state.get("sidekick_description", ""))
    if sidekick and sidekick.lower() not in {"a brave sidekick", "a magical companion"}:
        for label in _extract_character_candidates(sidekick, state):
            _register_entity(state, bucket="characters", label=label, source="sidekick", description=sidekick)

    for page in story_pages_from_state_data(state):
        scene_number += 1
        record_continuity_scene(
            state,
            description=_clean_text(page.get("scene_description", "")),
            storybeat_text=_clean_text(page.get("storybeat_text", "")),
            request_id=_clean_text(page.get("request_id", "")),
            scene_number=scene_number,
        )


def update_continuity_from_child_utterance(state: dict[str, Any], child_text: str) -> None:
    cleaned = _clean_text(child_text)
    if not cleaned:
        return

    ensure_story_continuity_state(state)
    world = state["continuity_world_state"]
    current_location_key = _clean_text(world.get("current_location_key", ""))
    current_location_label = _clean_text(world.get("current_location_label", ""))
    world["pending_request"] = cleaned[:160]
    world["goal"] = cleaned[:160]
    world["pending_transition"] = _detect_transition(cleaned)

    scene_number = _scene_number_hint(state)
    same_place_reveal = _is_same_place_reveal_request(state, cleaned)
    locations = _extract_location_candidates(cleaned)
    if locations:
        label = _select_location_candidate(
            cleaned,
            locations,
            current_location_label=current_location_label,
        )
        key = _register_entity(
            state,
            bucket="locations",
            label=label,
            source="child_request",
            scene_number=scene_number,
            description=cleaned,
        )
        world["pending_location_key"] = key
        world["pending_location_label"] = label
    elif same_place_reveal and current_location_label:
        world["pending_location_key"] = current_location_key
        world["pending_location_label"] = current_location_label
        world["pending_transition"] = world.get("pending_transition") or "same_room"

    characters = _extract_character_candidates(cleaned, state)
    pending_character_keys: list[str] = []
    for label in characters:
        key = _register_entity(
            state,
            bucket="characters",
            label=label,
            source="child_request",
            scene_number=scene_number,
            description=cleaned,
        )
        if key:
            pending_character_keys.append(key)
    if pending_character_keys:
        world["pending_character_keys"] = pending_character_keys[-6:]

    props = _extract_prop_candidates(cleaned)
    pending_prop_keys: list[str] = []
    for label in props:
        key = _register_entity(
            state,
            bucket="props",
            label=label,
            source="child_request",
            scene_number=scene_number,
            description=cleaned,
        )
        if key:
            pending_prop_keys.append(key)
    if same_place_reveal:
        carried_chest_keys = _matching_entity_keys_for_fragment(
            state,
            "props",
            "chest",
            candidate_keys=[str(item).strip() for item in list(world.get("active_prop_keys", []) or []) if str(item).strip()],
        )
        for key in carried_chest_keys:
            if key and key not in pending_prop_keys:
                pending_prop_keys.append(key)
    if pending_prop_keys:
        world["pending_prop_keys"] = pending_prop_keys[-6:]

    refresh_continuity_text_fields(state)


def validate_live_scene_request(
    state: dict[str, Any],
    description: str,
) -> ContinuityValidationResult:
    ensure_story_continuity_state(state)
    cleaned = _clean_text(description)
    world = state["continuity_world_state"]
    issues: list[str] = []

    pending_location_label = _clean_text(world.get("pending_location_label", ""))
    current_location_label = _clean_text(world.get("current_location_label", ""))
    current_visual_summary = _clean_text(state.get("current_scene_visual_summary", ""))
    previous_visual_summary = _clean_text(state.get("previous_scene_visual_summary", ""))
    pending_transition = _clean_text(world.get("pending_transition", ""))
    child_text = _clean_text(state.get("last_child_utterance", "") or world.get("pending_request", ""))
    navigation_request = _is_navigation_page_turn_request(child_text)
    same_place_reveal = _is_same_place_reveal_request(state, child_text)
    proposed_locations = _extract_location_candidates(cleaned)
    proposed_location_label = _select_location_candidate(
        cleaned,
        proposed_locations,
        current_location_label=current_location_label,
    )

    target_location_label = ""
    if pending_location_label:
        target_location_label = pending_location_label
    elif pending_transition and current_location_label:
        target_location_label = current_location_label
    elif proposed_location_label:
        target_location_label = proposed_location_label
    elif current_location_label:
        target_location_label = current_location_label

    resolved_description = cleaned
    if target_location_label and not _mentions_label(resolved_description, target_location_label):
        issues.append("missing_location_anchor")
        resolved_description = _prepend_location_anchor(resolved_description, target_location_label, pending_transition)

    if pending_location_label and proposed_location_label and not _locations_match(pending_location_label, proposed_location_label):
        issues.append("requested_location_mismatch")

    current_space_mode = _scene_space_mode(current_visual_summary or current_location_label)
    proposed_space_mode = _scene_space_mode(resolved_description)
    if (
        not pending_location_label
        and current_location_label
        and current_space_mode
        and proposed_space_mode
        and current_space_mode != proposed_space_mode
    ):
        issues.append("interior_exterior_drift")
        resolved_description = _prepend_location_anchor(
            resolved_description,
            current_location_label,
            current_space_mode,
        )

    active_character_keys = list(world.get("active_character_keys", []) or [])
    pending_character_keys = list(world.get("pending_character_keys", []) or [])
    carry_character_keys = pending_character_keys or active_character_keys
    carry_character_labels = _list_entity_labels(state, "characters", carry_character_keys[:3])
    current_prop_keys = list(world.get("active_prop_keys", []) or [])
    pending_prop_keys = list(world.get("pending_prop_keys", []) or [])
    carry_prop_keys = pending_prop_keys or current_prop_keys
    carry_prop_labels = _list_entity_labels(state, "props", carry_prop_keys[:2])

    prompt_notes: list[str] = []
    if carry_character_labels and not any(_mentions_label(resolved_description, label) for label in carry_character_labels):
        issues.append("missing_character_carryover")
        resolved_description = (
            resolved_description.rstrip(". ")
            + ". Keep these same characters in view: "
            + ", ".join(carry_character_labels)
            + "."
        )
        prompt_notes.append(
            "Keep these recurring characters visible and consistent: "
            + ", ".join(carry_character_labels)
            + "."
        )
    if carry_character_labels and not pending_character_keys:
        prompt_notes.append(
            "Do not invent new named characters, creatures, animals, or vehicles. "
            "Stay with the already established story cast unless the child explicitly asked for someone new."
        )
        prompt_notes.append(
            "Do not replace the current helper, creature, or guide with a different one. "
            "Carry the same established cast forward into the new beat."
        )
    if (
        carry_prop_labels
        and target_location_label
        and current_location_label
        and _locations_match(target_location_label, current_location_label)
        and not any(_mentions_label(resolved_description, label) for label in carry_prop_labels)
    ):
        issues.append("missing_prop_carryover")
        resolved_description = (
            resolved_description.rstrip(". ")
            + ". Keep a familiar prop from this same place visible, like "
            + ", ".join(carry_prop_labels)
            + "."
        )
        prompt_notes.append(
            "Retain at least one familiar prop from this place: "
            + ", ".join(carry_prop_labels)
            + "."
        )

    if target_location_label:
        signature_hint = _location_hint(target_location_label)
        if signature_hint:
            prompt_notes.append(
                "Make the destination unmistakable with these setting cues: "
                + signature_hint
                + "."
            )

    if (
        pending_transition in {"door", "inside", "tower", "window"}
        and current_location_label
        and target_location_label
        and _locations_match(current_location_label, target_location_label)
    ):
        prompt_notes.append(
            "Treat the next scene as a directly connected space in the same world, like the next room, hidden passage, tower level, or view from the same building."
        )
        prompt_notes.append(
            "Keep the architecture, materials, palette, and lighting family coherent with the current place. Do not jump to an unrelated forest, field, or outdoor biome unless the child explicitly asked to leave."
        )

    if (
        current_location_label
        and target_location_label
        and _locations_match(current_location_label, target_location_label)
        and not navigation_request
    ):
        if same_place_reveal:
            prompt_notes.append(
                "Treat this as a same-place reveal: stay in the exact current setting and focus on what is being opened or discovered there."
            )
            prompt_notes.append(
                "Do not move outdoors or to a different biome just because something new is revealed. Keep the reveal inside the existing cave, room, hall, or hidden nook."
            )
        prompt_notes.append(
            "Preserve the same room/layout identity from the current scene unless the child clearly changed locations."
        )
        prompt_notes.append(
            "Treat this as the same place from a new angle or focal point. Do not introduce a vehicle ride, train, road trip, portal trip, or any travel montage."
        )
        if current_visual_summary:
            prompt_notes.append(
                "Stay in this exact visible space from the current image: "
                + current_visual_summary[:220]
                + "."
            )
    elif navigation_request:
        prompt_notes.append(
            "Make this feel like moving onward to the next stretch of the journey, not the exact same camera view."
        )
        prompt_notes.append(
            "Shift the composition forward along the route or into the newly explored direction so the child clearly sees progress."
        )
    elif current_visual_summary and not pending_location_label:
        prompt_notes.append(
            "Use the current image as a continuity anchor: "
            + current_visual_summary[:220]
            + "."
        )
    elif previous_visual_summary and not pending_location_label:
        prompt_notes.append(
            "Use the previous image as a continuity anchor: "
            + previous_visual_summary[:220]
            + "."
        )

    scene_number = _scene_number_hint(state)
    character_keys = [
        _register_entity(
            state,
            bucket="characters",
            label=label,
            source="scene_request",
            scene_number=scene_number,
            description=resolved_description,
        )
        for label in _extract_character_candidates(resolved_description, state)
    ]
    prop_keys = [
        _register_entity(
            state,
            bucket="props",
            label=label,
            source="scene_request",
            scene_number=scene_number,
            description=resolved_description,
        )
        for label in _extract_prop_candidates(resolved_description)
    ]
    location_key = ""
    if target_location_label:
        location_key = _register_entity(
            state,
            bucket="locations",
            label=target_location_label,
            source="scene_request",
            scene_number=scene_number,
            description=resolved_description,
        )

    prompt_suffix = " ".join(prompt_notes).strip()
    refresh_continuity_text_fields(state)
    return ContinuityValidationResult(
        resolved_description=resolved_description,
        prompt_suffix=prompt_suffix,
        issues=issues,
        location_key=location_key,
        location_label=target_location_label,
        character_keys=[key for key in character_keys if key],
        prop_keys=[key for key in prop_keys if key],
    )


def record_continuity_scene(
    state: dict[str, Any],
    *,
    description: str,
    storybeat_text: str = "",
    visual_summary: str = "",
    request_id: str = "",
    scene_number: int | None = None,
) -> None:
    # Avoid re-entering bootstrap when continuity history is being built from
    # existing story pages. Bootstrapping itself calls record_continuity_scene.
    ensure_story_continuity_state(state, bootstrap=False)
    combined = _clean_text(" ".join(part for part in [description, storybeat_text, visual_summary] if part))
    if not combined:
        return

    world = state["continuity_world_state"]
    registry = state["continuity_entity_registry"]
    scene_number = int(scene_number or _scene_number_hint(state) or 1)

    location_label = ""
    location_candidates = _extract_location_candidates(combined)
    if location_candidates:
        location_label = location_candidates[0]
    elif _clean_text(world.get("pending_location_label", "")):
        location_label = _clean_text(world.get("pending_location_label", ""))
    elif _clean_text(world.get("current_location_label", "")):
        location_label = _clean_text(world.get("current_location_label", ""))

    character_labels = _extract_character_candidates(combined, state)
    if not character_labels:
        character_labels = _list_entity_labels(state, "characters", list(world.get("pending_character_keys", []) or []))
    if not character_labels and location_label and _locations_match(location_label, _clean_text(world.get("current_location_label", ""))):
        character_labels = _list_entity_labels(state, "characters", list(world.get("active_character_keys", []) or []))

    prop_labels = _extract_prop_candidates(combined)
    if not prop_labels:
        prop_labels = _list_entity_labels(state, "props", list(world.get("pending_prop_keys", []) or []))
    if not prop_labels and location_label and _locations_match(location_label, _clean_text(world.get("current_location_label", ""))):
        prop_labels = _list_entity_labels(state, "props", list(world.get("active_prop_keys", []) or []))

    location_key = ""
    if location_label:
        location_key = _register_entity(
            state,
            bucket="locations",
            label=location_label,
            source="scene_final",
            scene_number=scene_number,
            description=combined,
        )
    character_keys = [
        _register_entity(
            state,
            bucket="characters",
            label=label,
            source="scene_final",
            scene_number=scene_number,
            description=combined,
        )
        for label in character_labels
    ]
    prop_keys = [
        _register_entity(
            state,
            bucket="props",
            label=label,
            source="scene_final",
            scene_number=scene_number,
            description=combined,
        )
        for label in prop_labels
    ]

    world["scene_index"] = scene_number
    world["previous_location_key"] = _clean_text(world.get("current_location_key", ""))
    world["previous_location_label"] = _clean_text(world.get("current_location_label", ""))
    if location_key:
        world["current_location_key"] = location_key
        world["current_location_label"] = location_label
    if character_keys:
        world["active_character_keys"] = [key for key in character_keys if key][-6:]
    if prop_keys:
        world["active_prop_keys"] = [key for key in prop_keys if key][-6:]

    pending_transition = _clean_text(world.get("pending_transition", ""))
    if pending_transition and world.get("current_location_label"):
        world["last_transition"] = f"{pending_transition} -> {world['current_location_label']}"
    elif location_label and world.get("previous_location_label") and not _locations_match(location_label, _clean_text(world.get("previous_location_label", ""))):
        world["last_transition"] = f"{world['previous_location_label']} -> {location_label}"
    elif location_label:
        world["last_transition"] = f"stay in {location_label}"

    history = state.setdefault("continuity_scene_history", [])
    if not isinstance(history, list):
        history = []
        state["continuity_scene_history"] = history
    history.append(
        {
            "scene_number": scene_number,
            "request_id": request_id,
            "location_key": location_key,
            "location_label": location_label,
            "character_keys": [key for key in character_keys if key],
            "prop_keys": [key for key in prop_keys if key],
            "summary": _clean_text(storybeat_text or description)[:220],
            "visual_summary": _clean_text(visual_summary)[:220],
        }
    )
    state["continuity_scene_history"] = history[-_MAX_HISTORY:]

    world["pending_request"] = ""
    world["pending_location_key"] = ""
    world["pending_location_label"] = ""
    world["pending_transition"] = ""
    world["pending_character_keys"] = []
    world["pending_prop_keys"] = []

    refresh_continuity_text_fields(state)
