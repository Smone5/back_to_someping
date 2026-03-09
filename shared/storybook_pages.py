"""Helpers for page-level storybook scene history.

The live storyteller can move faster than image generation. A child may advance
the story before an earlier scene render fully finishes. Flat arrays like
`scene_asset_urls` lose the relationship between a spoken story page and the
image request that belongs to it. These helpers preserve that page-level
identity so movie assembly can use the actual story pages, not just the latest
images that happened to finish.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _clean_int(value: Any, default: int) -> int:
    try:
        numeric = int(value)
    except Exception:
        numeric = default
    return max(1, numeric)


def _normalize_story_page(item: Mapping[str, Any], fallback_scene_number: int) -> dict[str, Any] | None:
    scene_description = _clean_str(
        item.get("scene_description")
        or item.get("description")
        or item.get("current_scene_description")
    )
    storybeat_text = _clean_str(
        item.get("storybeat_text")
        or item.get("label")
        or item.get("current_scene_storybeat_text")
    )
    image_url = _clean_str(item.get("image_url") or item.get("url"))
    gcs_uri = _clean_str(item.get("gcs_uri") or item.get("image_gcs_uri"))
    request_id = _clean_str(item.get("request_id"))
    scene_number = _clean_int(item.get("scene_number"), fallback_scene_number)

    if not (scene_description or storybeat_text or image_url or gcs_uri or request_id):
        return None

    return {
        "scene_number": scene_number,
        "request_id": request_id,
        "scene_description": scene_description,
        "storybeat_text": storybeat_text,
        "image_url": image_url,
        "gcs_uri": gcs_uri,
    }


def story_pages_from_raw(raw_pages: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_pages, list):
        return []

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_pages, start=1):
        if not isinstance(item, Mapping):
            continue
        page = _normalize_story_page(item, index)
        if page is not None:
            normalized.append(page)

    normalized.sort(key=lambda item: int(item.get("scene_number", 0) or 0))
    return normalized


def story_pages_from_state_data(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []

    explicit_pages = story_pages_from_raw(data.get("story_pages"))
    if explicit_pages:
        return explicit_pages

    public_points = story_pages_from_raw(data.get("scene_branch_points_public"))
    if public_points:
        return public_points

    return story_pages_from_raw(data.get("scene_branch_points"))
