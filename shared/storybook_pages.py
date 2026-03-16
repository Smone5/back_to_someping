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

from .story_text import clean_story_text, normalize_storybeat_text


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _clean_int(value: Any, default: int) -> int:
    try:
        numeric = int(value)
    except Exception:
        numeric = default
    return max(1, numeric)


def _clean_rgb(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 3:
        return []
    try:
        return [int(value[0]), int(value[1]), int(value[2])]
    except Exception:
        return []


def _normalize_story_page(item: Mapping[str, Any], fallback_scene_number: int) -> dict[str, Any] | None:
    scene_description = clean_story_text(
        item.get("scene_description")
        or item.get("description")
        or item.get("current_scene_description")
    )
    storybeat_text = normalize_storybeat_text(
        item.get("storybeat_text")
        or item.get("current_scene_storybeat_text")
    )
    image_url = _clean_str(item.get("image_url") or item.get("url"))
    gcs_uri = _clean_str(item.get("gcs_uri") or item.get("image_gcs_uri"))
    request_id = _clean_str(item.get("request_id"))
    scene_number = _clean_int(item.get("scene_number"), fallback_scene_number)
    hex_color = _clean_str(item.get("hex_color")).upper()
    rgb_color = _clean_rgb(item.get("rgb_color"))
    cue_source = _clean_str(item.get("cue_source"))
    try:
        brightness = int(item.get("brightness")) if item.get("brightness") is not None else None
    except Exception:
        brightness = None
    try:
        transition = float(item.get("transition")) if item.get("transition") is not None else None
    except Exception:
        transition = None

    if not (scene_description or storybeat_text or image_url or gcs_uri or request_id):
        return None

    page = {
        "scene_number": scene_number,
        "request_id": request_id,
        "scene_description": scene_description,
        "storybeat_text": storybeat_text,
        "image_url": image_url,
        "gcs_uri": gcs_uri,
    }
    if hex_color:
        page["hex_color"] = hex_color
    if rgb_color:
        page["rgb_color"] = rgb_color
    if brightness is not None:
        page["brightness"] = brightness
    if transition is not None:
        page["transition"] = transition
    if cue_source:
        page["cue_source"] = cue_source
    return page


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


def _page_match_index(
    pages: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> int:
    candidate_request_id = _clean_str(candidate.get("request_id"))
    candidate_scene_number = _clean_int(candidate.get("scene_number"), len(pages) + 1)
    for index, page in enumerate(pages):
        page_request_id = _clean_str(page.get("request_id"))
        page_scene_number = _clean_int(page.get("scene_number"), index + 1)
        if candidate_request_id and page_request_id and candidate_request_id == page_request_id:
            return index
        if candidate_scene_number == page_scene_number:
            return index
    return -1


def _merge_story_page_lists(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for source in sources:
        for page in source:
            match_index = _page_match_index(merged, page)
            if match_index < 0:
                merged.append(dict(page))
                continue
            existing = dict(merged[match_index])
            for key in (
                "request_id",
                "scene_description",
                "storybeat_text",
                "image_url",
                "gcs_uri",
                "hex_color",
                "cue_source",
            ):
                candidate_value = _clean_str(page.get(key))
                if candidate_value and not _clean_str(existing.get(key)):
                    existing[key] = candidate_value
            candidate_rgb = _clean_rgb(page.get("rgb_color"))
            if candidate_rgb and not _clean_rgb(existing.get("rgb_color")):
                existing["rgb_color"] = candidate_rgb
            if page.get("brightness") is not None and existing.get("brightness") is None:
                existing["brightness"] = int(page.get("brightness"))
            if page.get("transition") is not None and existing.get("transition") is None:
                existing["transition"] = float(page.get("transition"))
            existing["scene_number"] = _clean_int(
                existing.get("scene_number") or page.get("scene_number"),
                match_index + 1,
            )
            merged[match_index] = existing
    merged.sort(key=lambda item: int(item.get("scene_number", 0) or 0))
    return merged


def story_pages_from_state_data(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, Mapping):
        return []

    explicit_pages = story_pages_from_raw(data.get("story_pages"))
    public_points = story_pages_from_raw(data.get("scene_branch_points_public"))
    scene_points = story_pages_from_raw(data.get("scene_branch_points"))
    merged = _merge_story_page_lists(explicit_pages, public_points, scene_points)
    return merged


def _has_real_story_page_asset(raw_value: Any) -> bool:
    asset = _clean_str(raw_value)
    if not asset:
        return False
    return not asset.startswith("data:image/svg+xml")


def count_rendered_story_pages(data: Mapping[str, Any] | None) -> int:
    if not isinstance(data, Mapping):
        return 0

    pages = story_pages_from_state_data(data)
    rendered_page_count = sum(
        1
        for page in pages
        if _has_real_story_page_asset(page.get("image_url")) or _has_real_story_page_asset(page.get("gcs_uri"))
    )
    if rendered_page_count > 0:
        return rendered_page_count

    fallback_url_count = sum(
        1 for value in (data.get("scene_asset_urls") or []) if _has_real_story_page_asset(value)
    )
    fallback_gcs_count = sum(
        1 for value in (data.get("scene_asset_gcs_uris") or []) if _has_real_story_page_asset(value)
    )
    return max(fallback_url_count, fallback_gcs_count)


def resolve_story_page_number(
    data: Mapping[str, Any] | None,
    *,
    request_id: str | None = None,
    replacement_mode: bool = False,
) -> int:
    if not isinstance(data, Mapping):
        return 1

    normalized_request_id = _clean_str(request_id)
    active_request_id = _clean_str(data.get("active_scene_request_id"))
    try:
        pending_scene_page_number = int(data.get("pending_scene_page_number") or 0)
    except Exception:
        pending_scene_page_number = 0
    pages = story_pages_from_state_data(data)

    if normalized_request_id:
        for page in reversed(pages):
            if _clean_str(page.get("request_id")) == normalized_request_id:
                return _clean_int(page.get("scene_number"), 1)
        if (
            pending_scene_page_number > 0
            and active_request_id
            and normalized_request_id == active_request_id
        ):
            return pending_scene_page_number

    highest_scene_number = 0
    for page in pages:
        highest_scene_number = max(
            highest_scene_number,
            _clean_int(page.get("scene_number"), highest_scene_number or 1),
        )

    if replacement_mode and pending_scene_page_number > 0:
        return pending_scene_page_number
    if replacement_mode:
        return highest_scene_number or 1
    if highest_scene_number <= 0:
        return 1
    return highest_scene_number + 1


def resolve_incoming_story_page_number(
    data: Mapping[str, Any] | None,
    *,
    request_id: str | None = None,
    proposed_scene_number: int | None = None,
) -> int:
    if not isinstance(data, Mapping):
        return max(_clean_int(proposed_scene_number, 1), 1)

    normalized_request_id = _clean_str(request_id)
    proposed_number = _clean_int(proposed_scene_number, 0)
    active_request_id = _clean_str(data.get("active_scene_request_id"))
    pending_scene_page_number = _clean_int(data.get("pending_scene_page_number"), 0)
    pages = story_pages_from_state_data(data)

    if normalized_request_id:
        for page in reversed(pages):
            if _clean_str(page.get("request_id")) == normalized_request_id:
                return _clean_int(page.get("scene_number"), 1)

        if active_request_id and normalized_request_id == active_request_id and pending_scene_page_number > 0:
            return pending_scene_page_number

    highest_scene_number = 0
    for page in pages:
        highest_scene_number = max(
            highest_scene_number,
            _clean_int(page.get("scene_number"), highest_scene_number or 1),
        )

    if highest_scene_number <= 0:
        return proposed_number if proposed_number > 0 else 1

    if normalized_request_id:
        return highest_scene_number + 1

    if proposed_number <= 0:
        return highest_scene_number + 1
    if proposed_number > highest_scene_number + 1:
        return highest_scene_number + 1
    return proposed_number
