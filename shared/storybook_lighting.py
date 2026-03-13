from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from typing import Any


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


def _clamp_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


_STORYBOOK_LIGHTING_PALETTES: dict[str, dict[str, Any]] = {
    "candy_peach": {
        "hex_color": "#FFC78A",
        "rgb_color": [255, 199, 138],
        "brightness": 196,
    },
    "dreamy_lavender": {
        "hex_color": "#B89CFF",
        "rgb_color": [184, 156, 255],
        "brightness": 166,
    },
    "moonlit_blue": {
        "hex_color": "#6FA8FF",
        "rgb_color": [111, 168, 255],
        "brightness": 154,
    },
    "forest_green": {
        "hex_color": "#55C26A",
        "rgb_color": [85, 194, 106],
        "brightness": 176,
    },
    "warm_gold": {
        "hex_color": "#FFB347",
        "rgb_color": [255, 179, 71],
        "brightness": 188,
    },
}

_STORYBOOK_LIGHTING_KEYWORDS: dict[str, set[str]] = {
    "candy_peach": {
        "candy", "candies", "caramel", "chocolate", "cookie", "cookies", "cupcake", "cupcakes",
        "dessert", "donut", "doughnut", "gummy", "gumdrop", "gumdrops", "honey", "lollipop",
        "lollipops", "marshmallow", "marshmallows", "peppermint", "sprinkle", "sprinkles",
        "sugar", "sugary", "sweet", "sweets", "taffy", "treat", "treats",
    },
    "dreamy_lavender": {
        "bubble", "bubbles", "dream", "dreamy", "fairy", "glitter", "magic", "magical",
        "pastel", "princess", "rainbow", "sparkle", "sparkly", "twinkle", "whimsical",
    },
    "moonlit_blue": {
        "blue", "castle", "ghost", "moon", "moonlit", "night", "secret", "sky", "spooky",
        "star", "stars", "water", "window", "wizard",
    },
    "forest_green": {
        "dragon", "forest", "garden", "grass", "green", "hill", "leaf", "leafy", "meadow",
        "mountain", "path", "tree", "trees", "woods",
    },
    "warm_gold": {
        "candle", "cozy", "fire", "firelight", "fireplace", "gift", "gold", "golden", "lantern",
        "morning", "santa", "sun", "sunny", "sunrise", "sunset", "warm",
    },
}

_STORYBOOK_LIGHTING_PRIORITY: dict[str, int] = {
    "candy_peach": 4,
    "dreamy_lavender": 3,
    "warm_gold": 2,
    "moonlit_blue": 1,
}


def storybook_rgb_from_hex(hex_color: str) -> list[int]:
    clean = str(hex_color or "").strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", clean):
        return []
    return [
        int(clean[0:2], 16),
        int(clean[2:4], 16),
        int(clean[4:6], 16),
    ]


def normalize_storybook_lighting_cues(raw_cues: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_cues, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_cues:
        if not isinstance(item, dict):
            continue
        hex_color = str(item.get("hex_color") or "").strip().upper()
        rgb_color = item.get("rgb_color")
        if isinstance(rgb_color, list) and len(rgb_color) == 3:
            try:
                rgb_triplet = [int(rgb_color[0]), int(rgb_color[1]), int(rgb_color[2])]
            except Exception:
                rgb_triplet = []
        else:
            rgb_triplet = storybook_rgb_from_hex(hex_color)
        if len(rgb_triplet) != 3:
            continue
        try:
            scene_number = int(item.get("scene_number") or 0)
        except Exception:
            scene_number = 0
        normalized.append(
            {
                "scene_number": max(0, scene_number),
                "request_id": str(item.get("request_id") or "").strip(),
                "hex_color": hex_color or f"#{rgb_triplet[0]:02X}{rgb_triplet[1]:02X}{rgb_triplet[2]:02X}",
                "rgb_color": rgb_triplet,
                "brightness": _clamp_int(item.get("brightness"), 180, 25, 255),
                "transition": _clamp_float(item.get("transition"), 1.2, 0.4, 3.0),
                "scene_description": str(item.get("scene_description") or "").strip(),
                "storybeat_text": str(item.get("storybeat_text") or "").strip(),
                "cue_source": str(item.get("cue_source") or "").strip(),
            }
        )
    return normalized


def heuristic_storybook_lighting_command(
    scene_text: str,
    *,
    is_cover: bool = False,
    is_end_card: bool = False,
    duration_seconds: float = 4.0,
) -> dict[str, Any]:
    if is_end_card:
        palette = _STORYBOOK_LIGHTING_PALETTES["warm_gold"]
        return {
            **palette,
            "transition": _clamp_float(min(duration_seconds * 0.3, 1.4), 1.2, 0.6, 1.6),
            "cue_source": "heuristic_end_card",
        }

    tokens = set(re.findall(r"[a-z]+", str(scene_text or "").lower()))
    scores: Counter[str] = Counter()
    for palette_name, keywords in _STORYBOOK_LIGHTING_KEYWORDS.items():
        scores[palette_name] = sum(1 for keyword in keywords if keyword in tokens)

    if is_cover and not any(scores.values()):
        palette_name = "dreamy_lavender"
    elif scores:
        palette_name = max(
            scores.items(),
            key=lambda item: (item[1], _STORYBOOK_LIGHTING_PRIORITY.get(item[0], 0)),
        )[0]
        if scores[palette_name] <= 0:
            palette_name = "moonlit_blue" if not is_cover else "dreamy_lavender"
    else:
        palette_name = "moonlit_blue"

    palette = _STORYBOOK_LIGHTING_PALETTES[palette_name]
    return {
        **palette,
        "transition": _clamp_float(min(duration_seconds * 0.28, 1.5), 1.1, 0.6, 1.8),
        "cue_source": "heuristic_scene",
    }


def lighting_cue_from_story_page(
    page: Mapping[str, Any] | None,
    *,
    duration_seconds: float = 4.0,
) -> dict[str, Any] | None:
    if not isinstance(page, Mapping):
        return None

    normalized = normalize_storybook_lighting_cues(
        [
            {
                "scene_number": page.get("scene_number"),
                "request_id": page.get("request_id"),
                "hex_color": page.get("hex_color"),
                "rgb_color": page.get("rgb_color"),
                "brightness": page.get("brightness"),
                "transition": page.get("transition") or min(duration_seconds * 0.28, 1.5),
                "scene_description": page.get("scene_description"),
                "storybeat_text": page.get("storybeat_text"),
                "cue_source": page.get("cue_source") or "page_metadata",
            }
        ]
    )
    return normalized[0] if normalized else None
