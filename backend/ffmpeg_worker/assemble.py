#!/usr/bin/env python3
"""FFmpeg Cloud Run Job — assembles the final story movie.

This job is triggered by the `assemble_story_video` ADK tool via the Cloud Run
Jobs API. It receives the session_id as a CLI argument and:
1. Pulls all session asset URLs (Veo MP4 clips, ElevenLabs audio) from Firestore.
2. Downloads assets from GCS to /tmp.
3. Uses FFmpeg to concatenate Veo clips, overlay TTS audio, mix Lyria music.
4. Uploads the final .mp4 (with -movflags faststart for instant streaming).
5. Writes the final URL back to Firestore so the backend WebSocket can push it.

Key design from the expert audits:
- `memory = "2Gi"` provisioned in Terraform (Iter 7 #6 — OOM fix for 8 Veo clips)
- `-movflags faststart` enforced (Iter 5 #6 — moov atom to front for streaming)
- Concurrent download using asyncio.gather for speed
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import textwrap
from typing import Any
from pathlib import Path
from collections import Counter
import sys

import httpx
import google.auth
import google.auth.transport.requests
from google.cloud import firestore, storage

def _resolve_import_root() -> Path:
    file_path = Path(__file__).resolve()
    for candidate in [file_path.parent, *file_path.parents]:
        if (candidate / "shared").exists():
            return candidate
    return file_path.parent


_IMPORT_ROOT = _resolve_import_root()
if str(_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMPORT_ROOT))

from shared.meta_learning import build_principles_injection_text, record_prompt_feedback

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

GCS_FINAL_BUCKET = os.environ.get("GCS_FINAL_VIDEOS_BUCKET", "storyteller-final-videos")
GCS_ASSETS_BUCKET = os.environ.get("GCS_ASSETS_BUCKET", "storyteller-session-assets")
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "(default)")
DEFAULT_VERTEX_TEXT_MODEL = "gemini-2.5-flash"
DEFAULT_VERTEX_IMAGE_MODEL = "gemini-2.0-flash-preview-image-generation"
_ELEVENLABS_TTS_DISABLED_REASON: str | None = None
_ELEVENLABS_AUDIO_DISABLED_REASON: str | None = None


async def download_blob(session: httpx.AsyncClient, url: str, dest: Path) -> Path:
    """Downloads a GCS signed URL to a local tmp path."""
    resp = await session.get(url, timeout=60)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    return dest


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clamp_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


def _clamp_float(value: str, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    return max(minimum, min(parsed, maximum))


def _extract_json_block(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _extract_response_text(response: Any) -> str:
    text = ""
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = list(candidates[0].content.parts)
            for part in parts:
                if getattr(part, "text", None):
                    text += str(part.text)
        elif getattr(response, "text", None):
            text = str(response.text)
        else:
            text = str(response)
    except Exception:
        text = str(response)
    return text.strip()


def _sniff_mime_type(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _scene_text_for_index(scene_descriptions: list[str], idx: int, story_summary: str) -> str:
    if idx < len(scene_descriptions):
        text = str(scene_descriptions[idx]).strip()
        if text:
            return text
    sentences = _split_sentences(_clean_story_text(story_summary))
    if sentences:
        pick = min(idx, len(sentences) - 1)
        return sentences[pick]
    return f"Scene {idx + 1} of the story."


def _make_image_part(google_genai: Any, image_bytes: bytes) -> Any:
    mime_type = _sniff_mime_type(image_bytes)
    return google_genai.types.Part.from_bytes(
        data=image_bytes,
        mime_type=mime_type,
    )


def _severity_rank(severity: str) -> int:
    normalized = str(severity or "").strip().lower()
    if normalized == "critical":
        return 3
    if normalized == "major":
        return 2
    if normalized == "minor":
        return 1
    return 0


def _review_category_rank(category: str) -> int:
    normalized = str(category or "").strip().lower()
    if normalized == "artifact_text":
        return 5
    if normalized == "safety":
        return 4
    if normalized == "diegetic_text":
        return 3
    if normalized == "continuity":
        return 2
    if normalized == "grounding":
        return 1
    if normalized == "other":
        return 1
    return 0


def _contains_any(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def _classify_storyboard_fix(issue: str, repair_prompt: str, severity: str) -> tuple[str, bool, str]:
    text = f"{issue} {repair_prompt}".lower()
    artifact_text_markers = (
        "watermark",
        "logo",
        "label",
        "caption",
        "subtitle",
        "signature",
        "signed",
        "ui text",
        "overlay text",
    )
    readable_text_markers = (
        "readable text",
        "clearly readable",
        "clearly legible",
        "legible text",
        "visible word",
        "written on its cover",
        "book cover has",
        "book spine",
        "title on",
    )
    decorative_text_markers = (
        "illegible text",
        "gibberish",
        "text-like",
        "letters",
        "letter-like",
        "symbols",
        "glyphs",
        "runes",
        "book pages",
        "page texture",
        "swirling magic",
        "river made of letters",
    )
    continuity_markers = (
        "inconsistent",
        "continuity",
        "missing",
        "appearance",
        "clothing",
        "character design",
        "sidekick",
        "prop",
        "location",
        "contradict",
    )
    safety_markers = (
        "scary",
        "harsh",
        "violent",
        "creepy",
        "intense",
        "threatening",
    )
    grounding_markers = (
        "does not match",
        "doesn't match",
        "misses",
        "wrong beat",
        "wrong scene",
        "not the intended",
    )

    if _contains_any(text, artifact_text_markers):
        return "artifact_text", True, severity
    if _contains_any(text, readable_text_markers) or re.search(r"(word|text)\s*['\"][A-Za-z0-9][^'\"]*['\"]", text):
        return "diegetic_text", True, severity
    if _contains_any(text, safety_markers):
        return "safety", True, severity
    if _contains_any(text, continuity_markers):
        actionable = _severity_rank(severity) >= 2
        return "continuity", actionable, severity
    if _contains_any(text, grounding_markers):
        actionable = _severity_rank(severity) >= 2
        return "grounding", actionable, severity
    if _contains_any(text, decorative_text_markers):
        return "decorative_text", False, "minor"
    actionable = _severity_rank(severity) >= 2
    return "other", actionable, severity


def _unresolved_storyboard_status(fixes: list[dict[str, Any]]) -> str:
    if not fixes:
        return "passed"
    for fix in fixes:
        category = str(fix.get("category", "")).strip().lower()
        if category == "artifact_text" or _severity_rank(fix.get("severity", "")) >= 3:
            return "needs_manual_review"
    return "passed_with_warnings"


def _normalize_review_fix(fix: dict[str, Any], total_scenes: int, direction: str) -> dict[str, Any] | None:
    try:
        scene_index = int(fix.get("scene_index", 0))
    except Exception:
        return None
    if scene_index < 1 or scene_index > total_scenes:
        return None
    issue = str(fix.get("issue", "")).strip()
    repair_prompt = str(fix.get("repair_prompt", "")).strip()
    severity = str(fix.get("severity", "major")).strip().lower() or "major"
    if severity not in {"minor", "major", "critical"}:
        severity = "major"
    if not issue:
        return None
    category, actionable, normalized_severity = _classify_storyboard_fix(issue, repair_prompt, severity)
    return {
        "scene_index": scene_index,
        "severity": normalized_severity,
        "category": category,
        "actionable": actionable,
        "issue": issue[:300],
        "repair_prompt": repair_prompt[:500],
        "directions": [direction],
    }


def _merge_storyboard_reviews(
    reviews: list[tuple[str, dict[str, Any] | None]],
    total_scenes: int,
) -> dict[str, Any]:
    merged: dict[int, dict[str, Any]] = {}
    global_feedback: list[str] = []
    for direction, review in reviews:
        if not review:
            continue
        for note in review.get("global_feedback", []) or []:
            note_text = str(note).strip()
            if note_text:
                global_feedback.append(f"{direction}: {note_text[:240]}")
        for raw_fix in review.get("scene_fixes", []) or []:
            if not isinstance(raw_fix, dict):
                continue
            fix = _normalize_review_fix(raw_fix, total_scenes, direction)
            if not fix:
                continue
            idx = fix["scene_index"]
            existing = merged.get(idx)
            if not existing:
                merged[idx] = fix
                continue
            existing["directions"] = sorted(set(existing.get("directions", []) + fix["directions"]))
            if fix["issue"] not in existing["issue"]:
                existing["issue"] = f"{existing['issue']}; {fix['issue']}"[:300]
            new_prompt = fix["repair_prompt"]
            if new_prompt and new_prompt not in existing["repair_prompt"]:
                if existing["repair_prompt"]:
                    existing["repair_prompt"] = (
                        f"{existing['repair_prompt']} Also ensure: {new_prompt}"
                    )[:500]
                else:
                    existing["repair_prompt"] = new_prompt[:500]
            if _severity_rank(fix["severity"]) > _severity_rank(existing["severity"]):
                existing["severity"] = fix["severity"]
            if _review_category_rank(fix.get("category", "")) > _review_category_rank(existing.get("category", "")):
                existing["category"] = fix.get("category", "other")
            existing["actionable"] = bool(existing.get("actionable")) or bool(fix.get("actionable"))

    scene_fixes = sorted(
        merged.values(),
        key=lambda item: (-_severity_rank(item.get("severity", "")), item.get("scene_index", 0)),
    )
    return {
        "passed": len(scene_fixes) == 0,
        "scene_fixes": scene_fixes,
        "global_feedback": global_feedback[:10],
    }


def _review_scene_window(
    scene_descriptions: list[str],
    story_summary: str,
    idx: int,
) -> dict[str, Any]:
    previous_desc = _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else ""
    current_desc = _scene_text_for_index(scene_descriptions, idx, story_summary)
    next_desc = _scene_text_for_index(scene_descriptions, idx + 1, story_summary) if idx + 1 < len(scene_descriptions) else ""
    return {
        "scene_index": idx + 1,
        "previous_scene": previous_desc,
        "current_scene": current_desc,
        "next_scene": next_desc,
    }


def _review_storyboard_pass(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str,
    still_paths: list[Path],
    direction: str,
) -> dict[str, Any] | None:
    if not PROJECT or not still_paths:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Storyboard review unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_SCENE_REVIEW_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    ordered_indices = list(range(len(still_paths)))
    if direction == "BACKWARD":
        ordered_indices.reverse()

    review_payload = []
    for review_order, idx in enumerate(ordered_indices, start=1):
        window = _review_scene_window(scene_descriptions, story_summary, idx)
        review_payload.append(
            {
                "review_order": review_order,
                "scene_index": window["scene_index"],
                "previous_scene": window["previous_scene"],
                "current_scene": window["current_scene"],
                "next_scene": window["next_scene"],
            }
        )
    meta_guidance = build_principles_injection_text("storyboard_review")

    prompt = f"""
You are a children's storybook art director reviewing a READ-ALOUD movie storyboard.
This is a still-only adaptation: each still is a page-like scene and the narration sets the pacing.

Review direction: {direction}
Child hero name: {child_name or "young hero"}
Story summary:
{story_summary or "No summary available."}

{meta_guidance}

Review goals:
- Keep character appearance, props, sidekicks, and locations temporally coherent.
- Make sure each still matches its own scene description and does not contradict adjacent scenes.
- Keep the art gentle, G-rated, warm, and book-like for ages 4-5.
- Reject clearly readable overlay text, logos, labels, signatures, UI text, or watermarks.
- Ignore tiny unreadable book-page texture, abstract glyphs, and decorative letter-like motifs unless they are prominent and child-readable.
- Only flag scenes that truly need regeneration and are worth the repair cost.

Return JSON only in this schema:
{{
  "global_feedback": ["short note"],
  "scene_fixes": [
    {{
      "scene_index": 2,
      "severity": "major",
      "issue": "what is wrong",
      "repair_prompt": "positive prompt for regenerating only this scene while preserving story intent and continuity"
    }}
  ]
}}

Scene cards:
{json.dumps(review_payload, indent=2)}
""".strip()

    contents: list[Any] = [prompt]
    for review_order, idx in enumerate(ordered_indices, start=1):
        try:
            image_bytes = still_paths[idx].read_bytes()
        except Exception:
            continue
        contents.append(f"Scene image {review_order} (original scene_index={idx + 1})")
        contents.append(_make_image_part(google_genai, image_bytes))

    try:
        client = google_genai.Client(vertexai=True, project=PROJECT, location=location)
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=google_genai.types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
        )
    except Exception as exc:
        logger.warning("Storyboard %s review failed: %s", direction.lower(), exc)
        return None

    payload = _extract_json_block(_extract_response_text(response))
    if not isinstance(payload, dict):
        logger.warning("Storyboard %s review returned no parsable JSON.", direction.lower())
        return None
    return payload


def _generate_repaired_story_still(
    scene_description: str,
    repair_prompt: str,
    story_summary: str,
    previous_description: str,
    next_description: str,
    current_image_bytes: bytes | None,
    previous_image_bytes: bytes | None,
    hero_anchor_description: str,
    hero_anchor_image_bytes: bytes | None,
) -> bytes | None:
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Scene repair unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_SCENE_IMAGE_MODEL", DEFAULT_VERTEX_IMAGE_MODEL).strip() or DEFAULT_VERTEX_IMAGE_MODEL
    image_size = os.environ.get("STORYBOOK_SCENE_IMAGE_SIZE", "512px").strip() or "512px"
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    meta_guidance = build_principles_injection_text("storyboard_repair")
    prompt = f"""
Create a single 16:9 children's storybook illustration.

Base scene intent:
{scene_description}

Required fixes:
{repair_prompt}

Story summary:
{story_summary or "No summary available."}

{meta_guidance}

Continuity anchors:
- Previous scene: {previous_description or "None"}
- Next scene: {next_description or "None"}
- Opening hero anchor: {hero_anchor_description or "None"}

Rules:
- Hand-drawn, painterly 2D storybook look.
- Warm, gentle, child-safe, reading-rainbow-style read-aloud energy.
- Keep character identity, clothing, colors, props, and setting progression coherent.
- Preserve the scene's intended story beat.
- No readable text, logos, labels, captions, signatures, or watermarks.
- Decorative storybook marks or unreadable page texture are acceptable only if they stay subtle and non-dominant.
""".strip()

    contents: list[Any] = [prompt]
    if hero_anchor_image_bytes:
        contents.append("Opening hero anchor for design continuity:")
        contents.append(_make_image_part(google_genai, hero_anchor_image_bytes))
    if previous_image_bytes:
        contents.append("Previous approved scene for continuity:")
        contents.append(_make_image_part(google_genai, previous_image_bytes))
    if current_image_bytes:
        contents.append("Current scene to improve:")
        contents.append(_make_image_part(google_genai, current_image_bytes))

    try:
        client = google_genai.Client(vertexai=True, project=PROJECT, location=location)
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=google_genai.types.GenerateContentConfig(
                response_modalities=[google_genai.types.MediaModality.IMAGE],
                image_config=google_genai.types.ImageConfig(
                    aspect_ratio="16:9",
                    image_size=image_size,
                ),
            ),
        )
        return _extract_image_bytes(response)
    except Exception as exc:
        logger.warning("Scene repair generation failed: %s", exc)
        return None


async def _review_and_refine_storyboard(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str,
    still_paths: list[Path],
    tmp: Path,
) -> tuple[list[Path], dict[str, Any]]:
    report: dict[str, Any] = {
        "status": "skipped",
        "passes": [],
        "repairs_applied": [],
    }
    if not _env_enabled("ENABLE_STORYBOOK_SCENE_REVIEW", default=True):
        report["reason"] = "scene review disabled"
        return still_paths, report
    if not PROJECT or not still_paths:
        report["reason"] = "missing project or stills"
        return still_paths, report

    max_passes = _clamp_int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_PASSES", "2"), 2, 1, 3)
    max_fixes = _clamp_int(os.environ.get("STORYBOOK_SCENE_REVIEW_MAX_FIXES", "3"), 3, 0, 4)
    refined_paths = list(still_paths)

    for pass_index in range(1, max_passes + 1):
        forward_review = _review_storyboard_pass(
            scene_descriptions=scene_descriptions,
            story_summary=story_summary,
            child_name=child_name,
            still_paths=refined_paths,
            direction="FORWARD",
        )
        backward_review = _review_storyboard_pass(
            scene_descriptions=scene_descriptions,
            story_summary=story_summary,
            child_name=child_name,
            still_paths=refined_paths,
            direction="BACKWARD",
        )
        merged = _merge_storyboard_reviews(
            [("FORWARD", forward_review), ("BACKWARD", backward_review)],
            total_scenes=len(refined_paths),
        )
        fix_candidates = merged.get("scene_fixes", []) or []
        actionable_fixes = [fix for fix in fix_candidates if bool(fix.get("actionable"))]
        warning_fixes = [fix for fix in fix_candidates if not bool(fix.get("actionable"))]
        report["passes"].append(
            {
                "pass": pass_index,
                "fix_count": len(actionable_fixes),
                "warning_count": len(warning_fixes),
                "global_feedback": merged.get("global_feedback", []),
            }
        )
        feedback_issues = [
            str(fix.get("issue") or fix.get("repair_prompt") or "").strip()
            for fix in actionable_fixes
            if str(fix.get("issue") or fix.get("repair_prompt") or "").strip()
        ]
        record_prompt_feedback(
            "storyboard_review",
            outcome=(
                "fix_required"
                if actionable_fixes
                else "passed_with_warnings"
                if warning_fixes
                else "passed"
            ),
            issues=feedback_issues,
            prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
            metadata={
                "pass": pass_index,
                "fix_count": len(actionable_fixes),
                "warning_count": len(warning_fixes),
                "global_feedback": merged.get("global_feedback", [])[:3],
            },
        )
        if feedback_issues:
            record_prompt_feedback(
                "interactive_story",
                outcome="movie_review_fix_required",
                issues=feedback_issues,
                prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
                metadata={
                    "source": "storyboard_review",
                    "pass": pass_index,
                    "fix_count": len(actionable_fixes),
                },
            )
            record_prompt_feedback(
                "interactive_scene_visual",
                outcome="movie_review_fix_required",
                issues=feedback_issues,
                prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
                metadata={
                    "source": "storyboard_review",
                    "pass": pass_index,
                    "fix_count": len(actionable_fixes),
                },
            )

        if not actionable_fixes:
            report["status"] = "passed_with_warnings" if warning_fixes else "passed"
            report["final_review"] = merged
            return refined_paths, report

        if max_fixes <= 0 or pass_index >= max_passes:
            report["status"] = _unresolved_storyboard_status(actionable_fixes)
            report["final_review"] = merged
            return refined_paths, report

        repairs_this_pass = 0
        for fix in actionable_fixes[:max_fixes]:
            idx = int(fix["scene_index"]) - 1
            if idx < 0 or idx >= len(refined_paths):
                continue
            try:
                current_image_bytes = refined_paths[idx].read_bytes()
            except Exception:
                current_image_bytes = None
            previous_image_bytes = None
            if idx > 0:
                try:
                    previous_image_bytes = refined_paths[idx - 1].read_bytes()
                except Exception:
                    previous_image_bytes = None
            hero_anchor_image_bytes = None
            hero_anchor_description = ""
            if refined_paths:
                hero_anchor_description = _scene_text_for_index(scene_descriptions, 0, story_summary)
                if idx != 0:
                    try:
                        hero_anchor_image_bytes = refined_paths[0].read_bytes()
                    except Exception:
                        hero_anchor_image_bytes = None

            current_desc = _scene_text_for_index(scene_descriptions, idx, story_summary)
            previous_desc = _scene_text_for_index(scene_descriptions, idx - 1, story_summary) if idx > 0 else ""
            next_desc = _scene_text_for_index(scene_descriptions, idx + 1, story_summary) if idx + 1 < len(refined_paths) else ""

            repaired_bytes = _generate_repaired_story_still(
                scene_description=current_desc,
                repair_prompt=str(fix.get("repair_prompt") or fix.get("issue") or "").strip(),
                story_summary=story_summary,
                previous_description=previous_desc,
                next_description=next_desc,
                current_image_bytes=current_image_bytes,
                previous_image_bytes=previous_image_bytes,
                hero_anchor_description=hero_anchor_description,
                hero_anchor_image_bytes=hero_anchor_image_bytes,
            )
            if not repaired_bytes:
                record_prompt_feedback(
                    "storyboard_repair",
                    outcome="repair_failed",
                    issues=[str(fix.get("issue") or fix.get("repair_prompt") or "").strip()],
                    prompt_text=current_desc,
                    metadata={
                        "pass": pass_index,
                        "scene_index": idx + 1,
                        "severity": fix.get("severity", "major"),
                    },
                )
                continue

            repaired_path = tmp / f"scene_{idx:03d}_repair_pass_{pass_index}.png"
            repaired_path.write_bytes(repaired_bytes)
            refined_paths[idx] = repaired_path
            repairs_this_pass += 1
            report["repairs_applied"].append(
                {
                    "pass": pass_index,
                    "scene_index": idx + 1,
                    "severity": fix.get("severity", "major"),
                    "issue": str(fix.get("issue", ""))[:240],
                    "directions": fix.get("directions", []),
                }
            )
            record_prompt_feedback(
                "storyboard_repair",
                outcome="repair_applied",
                issues=[str(fix.get("issue") or fix.get("repair_prompt") or "").strip()],
                prompt_text=current_desc,
                metadata={
                    "pass": pass_index,
                    "scene_index": idx + 1,
                    "severity": fix.get("severity", "major"),
                    "directions": fix.get("directions", []),
                },
            )
            logger.info(
                "Storyboard repair applied for scene %d on pass %d (%s).",
                idx + 1,
                pass_index,
                fix.get("severity", "major"),
            )

        if repairs_this_pass == 0:
            record_prompt_feedback(
                "storyboard_repair",
                outcome=_unresolved_storyboard_status(actionable_fixes),
                issues=feedback_issues,
                prompt_text=story_summary or " | ".join(scene_descriptions[:4]),
                metadata={
                    "pass": pass_index,
                    "fix_count": len(actionable_fixes),
                },
            )
            report["status"] = _unresolved_storyboard_status(actionable_fixes)
            report["final_review"] = merged
            return refined_paths, report

    report["status"] = "passed_with_repairs"
    return refined_paths, report


def _llm_audio_cue_plan(
    scene_descriptions: list[str],
    story_summary: str,
    max_music: int,
    max_sfx: int,
    still_paths: list[Path] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]] | None:
    """Use Gemini to choose which scenes get music/SFX cues."""
    if not _env_enabled("ENABLE_STORYBOOK_AUDIO_LLM", default=False):
        return None
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Audio cue LLM unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_AUDIO_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    client = google_genai.Client(vertexai=True, project=PROJECT, location=location)

    scene_lines = []
    for idx, desc in enumerate(scene_descriptions, start=1):
        scene_lines.append(f"{idx}. {desc.strip() or 'Scene with no description.'}")

    prompt = f"""
You are a small panel of creative advisors:
- LeVar Burton (warm, clear storytelling)
- Fred Rogers (gentle emotional truth)
- Julie Andrews (musical phrasing and cadence)
- Alan Menken (theme cues for opening/closing)
- Ben Burtt (purposeful physical SFX only)

Task: Decide which scenes deserve short MUSIC cues and which deserve SFX cues.
Constraints:
- Music should be minimal: opening and closing, optional single midpoint lift.
- SFX only if a clear physical action is present.
- Max music cues: {max_music}. Max SFX cues: {max_sfx}.
- Use 1-based scene_index values.
- Output JSON ONLY with the schema:
{{"music_cues":[{{"scene_index":1,"prompt":"...","duration_seconds":6}}],"sfx_cues":[{{"scene_index":2,"prompt":"...","duration_seconds":2}}]}}
Use a brief internal tree-of-thought and do not output your reasoning.

Story summary:
{story_summary.strip() or "No summary available."}

Scene descriptions:
{chr(10).join(scene_lines)}
""".strip()

    contents: list[Any] = [prompt]
    if _env_enabled("ENABLE_STORYBOOK_AUDIO_LLM_IMAGES", default=False) and still_paths:
        for idx, path in enumerate(still_paths, start=1):
            try:
                img_bytes = path.read_bytes()
            except Exception:
                continue
            contents.append(f"Scene {idx} image:")
            try:
                contents.append(
                    google_genai.types.Part.from_bytes(
                        data=img_bytes,
                        mime_type="image/png",
                    )
                )
            except Exception:
                continue

    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
        )
    except Exception as exc:
        logger.warning("Audio cue LLM request failed: %s", exc)
        return None

    text = ""
    try:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            parts = list(candidates[0].content.parts)
            for part in parts:
                if getattr(part, "text", None):
                    text += str(part.text)
        else:
            text = str(response)
    except Exception:
        text = str(response)

    payload = _extract_json_block(text)
    if not payload:
        logger.warning("Audio cue LLM returned no parsable JSON.")
        return None

    music: dict[int, dict[str, Any]] = {}
    sfx: dict[int, dict[str, Any]] = {}

    for cue in payload.get("music_cues", []) or []:
        try:
            idx = int(cue.get("scene_index", 0)) - 1
        except Exception:
            continue
        if idx < 0 or idx >= len(scene_descriptions):
            continue
        prompt_text = str(cue.get("prompt", "")).strip()
        if not prompt_text:
            continue
        music[idx] = {"prompt": prompt_text, "duration_seconds": cue.get("duration_seconds")}

    for cue in payload.get("sfx_cues", []) or []:
        try:
            idx = int(cue.get("scene_index", 0)) - 1
        except Exception:
            continue
        if idx < 0 or idx >= len(scene_descriptions):
            continue
        prompt_text = str(cue.get("prompt", "")).strip()
        if not prompt_text:
            continue
        sfx[idx] = {"prompt": prompt_text, "duration_seconds": cue.get("duration_seconds")}

    if not music and not sfx:
        return None
    return music, sfx


def _heuristic_audio_cues(
    scene_descriptions: list[str],
    total: int,
    max_music: int,
    max_sfx: int,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    music: dict[int, dict[str, Any]] = {}
    sfx: dict[int, dict[str, Any]] = {}
    if total <= 0:
        return music, sfx
    # Music: opening + closing only by default.
    if max_music > 0:
        music_indices = [0]
        if total > 1:
            music_indices.append(total - 1)
        for idx in dict.fromkeys(music_indices):  # preserve order, unique
            desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
            music[idx] = {"prompt": _build_music_prompt(desc, idx, total)}

    if max_sfx > 0:
        min_score = _clamp_int(os.environ.get("STORYBOOK_SFX_MIN_SCORE", "2"), 2, 1, 6)
        cooldown = _clamp_int(os.environ.get("STORYBOOK_SFX_COOLDOWN", "1"), 1, 0, 3)
        sfx_indices = _choose_sfx_indices(
            total=total,
            max_count=max_sfx,
            descriptions=scene_descriptions,
            min_score=min_score,
            cooldown=cooldown,
        )
        for idx in sfx_indices:
            desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
            sfx[idx] = {"prompt": _build_sfx_prompt(desc)}

    return music, sfx


def _plan_audio_cues(
    scene_descriptions: list[str],
    story_summary: str,
    total: int,
    still_paths: list[Path] | None = None,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    max_music = _clamp_int(os.environ.get("STORYBOOK_MUSIC_MAX", "2"), 2, 0, 4)
    max_sfx = _clamp_int(os.environ.get("STORYBOOK_SFX_MAX", "3"), 3, 0, 6)
    plan = _llm_audio_cue_plan(scene_descriptions, story_summary, max_music, max_sfx, still_paths)
    if plan:
        return plan
    return _heuristic_audio_cues(scene_descriptions, total, max_music, max_sfx)


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _clean_story_text(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<ctrl\\d+>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"🌟\\s*What should we do\\?.*?(\\n|$)", " ", cleaned)
    cleaned = re.sub(r"\\s+", " ", cleaned)
    return cleaned.strip()


def _simplify_for_kids(text: str, max_words: int = 28) -> str:
    cleaned = _clean_story_text(text)
    if not cleaned:
        return ""
    sentences = _split_sentences(cleaned)
    if not sentences:
        return ""
    short = " ".join(sentences[:2])
    words = short.split()
    if len(words) > max_words:
        short = " ".join(words[:max_words]).rstrip(".,") + "."
    return short


def _simplify_for_readalong(text: str, max_words: int = 8) -> str:
    cleaned = _clean_story_text(text)
    if not cleaned:
        return ""
    sentences = _split_sentences(cleaned)
    if not sentences:
        return ""
    first = sentences[0]
    first = re.split(r",|;|\\b(and|but|so)\\b", first, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if not first:
        return ""
    words = first.split()
    if len(words) > max_words:
        first = " ".join(words[:max_words]).rstrip(".,!?") + "."
    else:
        first = first.rstrip(".,!?") + "."
    if not re.match(r"^[\"“'\\(]*?(we|i|you|a|an|the|it|there|look)\\b", first, flags=re.IGNORECASE):
        first = "Look! " + first[0].upper() + first[1:]
    return first


def _build_narration_segments(
    scene_descriptions: list[str],
    story_summary: str,
    scene_count: int,
) -> list[str]:
    narration: list[str] = []
    if scene_descriptions:
        for idx, desc in enumerate(scene_descriptions[:scene_count]):
            line = _simplify_for_readalong(desc)
            if line:
                narration.append(line)
    if not narration and story_summary:
        sentences = _split_sentences(_clean_story_text(story_summary))
        if sentences:
            chunk_size = max(1, len(sentences) // max(1, scene_count))
            for i in range(scene_count):
                chunk = sentences[i * chunk_size:(i + 1) * chunk_size]
                if not chunk and sentences:
                    chunk = [sentences[-1]]
                line = _simplify_for_readalong(" ".join(chunk))
                narration.append(line)
    if len(narration) < scene_count:
        if narration:
            narration.extend([narration[-1]] * (scene_count - len(narration)))
        else:
            narration = ["" for _ in range(scene_count)]
    if len(narration) > scene_count:
        narration = narration[:scene_count]
    return narration


def _build_music_prompt(description: str, idx: int, total: int) -> str:
    base = "gentle, whimsical, child-friendly instrumental music"
    desc = description.strip()
    if desc:
        return (
            f"{base} for a story scene. Scene details: {desc}. "
            "No vocals, no lyrics, soft bells, warm pads, light marimba."
        )
    position = "opening" if idx == 0 else "ending" if idx == total - 1 else "middle"
    return (
        f"{base} for the {position} of a children's adventure. "
        "No vocals, no lyrics, soft bells, warm pads, light marimba."
    )


def _score_sfx(description: str) -> int:
    text = (description or "").lower()
    if not text:
        return 0
    keywords = [
        "door", "castle", "sparkle", "sparkly", "twinkle", "magic", "magical",
        "jump", "bounce", "boing", "splash", "river", "wave", "wind",
        "whoosh", "glow", "pop", "candy", "bubble", "giggle",
    ]
    score = 0
    for kw in keywords:
        if kw in text:
            score += 1
    return score


def _choose_sfx_indices(
    total: int,
    max_count: int,
    descriptions: list[str],
    min_score: int,
    cooldown: int,
) -> list[int]:
    if total <= 0 or max_count <= 0:
        return []
    max_count = max(0, min(max_count, total))
    min_score = max(1, min(min_score, 6))
    cooldown = max(0, min(cooldown, 3))
    scored: list[tuple[int, int]] = []
    for idx in range(total):
        desc = descriptions[idx] if idx < len(descriptions) else ""
        score = _score_sfx(desc)
        if score >= min_score:
            scored.append((idx, score))
    scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
    picked: list[int] = []
    for idx, _score in scored:
        if any(abs(idx - p) <= cooldown for p in picked):
            continue
        picked.append(idx)
        if len(picked) >= max_count:
            break
    return sorted(picked)


def _build_sfx_prompt(description: str) -> str:
    text = (description or "").lower()
    if "door" in text:
        return "soft magical door creak with candy chimes, gentle"
    if "jump" in text or "bounce" in text:
        return "soft bouncy boing, cute and gentle"
    if "sparkle" in text or "twinkle" in text or "magic" in text:
        return "magical sparkle twinkle, light and shimmering"
    if "splash" in text or "river" in text or "wave" in text:
        return "tiny water splash, playful and light"
    if "wind" in text or "whoosh" in text:
        return "soft magical whoosh, warm and gentle"
    return "gentle magical chime sparkle, light and child-friendly"


def _wrap_caption(text: str, width: int = 26, max_lines: int = 2) -> str:
    if not text:
        return ""
    lines = textwrap.wrap(text, width=width)
    return "\n".join(lines[:max_lines])


def _ffmpeg_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _clean_title(raw: str) -> str:
    title = (raw or "").strip()
    if not title:
        return ""
    if "sdk_http_response" in title.lower() or "candidates=[" in title.lower():
        return ""
    title = re.sub(r"^(title|story)\\s*[:\\-]\\s*", "", title, flags=re.IGNORECASE)
    title = title.strip().strip("\"'`")
    title = re.sub(r"\\s+", " ", title).strip()
    words = title.split()
    if len(words) > 8:
        title = " ".join(words[:8])
    return title


def _heuristic_title(scene_descriptions: list[str], story_summary: str) -> str:
    text = " ".join(scene_descriptions) + " " + (story_summary or "")
    words = re.findall(r"[A-Za-z']{4,}", text)
    stopwords = {
        "this", "that", "with", "from", "they", "them", "were", "where", "when", "then",
        "there", "their", "your", "have", "into", "over", "under", "across", "about",
        "story", "book", "books", "child", "little", "gentle", "glowing", "bright",
        "light", "magic", "magical", "soft", "warm", "night", "cloud", "clouds",
        "reading", "disney", "pixar",
    }
    counts = Counter(w.lower() for w in words if w.lower() not in stopwords)
    if not counts:
        return "A Storybook Adventure"
    top = [w.title() for w, _ in counts.most_common(3)]
    if len(top) >= 2:
        title = f"{top[0]} and the {top[1]}"
    else:
        title = f"The {top[0]} Story"
    if re.search(r"reading\\s+rainbow", title, re.IGNORECASE):
        return "A Storybook Adventure"
    return title


def _generate_story_title(scene_descriptions: list[str], story_summary: str) -> str:
    if not _env_enabled("ENABLE_STORYBOOK_TITLE_LLM", default=True):
        return _heuristic_title(scene_descriptions, story_summary)
    if not PROJECT:
        return _heuristic_title(scene_descriptions, story_summary)
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Title LLM unavailable (google-genai not installed): %s", exc)
        return _heuristic_title(scene_descriptions, story_summary)

    model = os.environ.get("STORYBOOK_TITLE_MODEL", DEFAULT_VERTEX_TEXT_MODEL).strip() or DEFAULT_VERTEX_TEXT_MODEL
    scene_lines = []
    for idx, desc in enumerate(scene_descriptions, start=1):
        scene_lines.append(f"{idx}. {desc.strip() or 'Scene with no description.'}")
    prompt = (
        "Create a short, original children's storybook title based on the scenes below. "
        "2 to 6 words. No quotes. Avoid brand names (e.g., Reading Rainbow, Disney). "
        "Return only the title text.\n\n"
        f"Story summary: {story_summary.strip() or 'No summary available.'}\n\n"
        "Scenes:\n" + "\n".join(scene_lines)
    )
    try:
        location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
        client = google_genai.Client(vertexai=True, project=PROJECT, location=location)
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config=google_genai.types.GenerateContentConfig(
                temperature=0.7,
                max_output_tokens=16,
            ),
        )
    except Exception as exc:
        logger.warning("Title LLM request failed: %s", exc)
        return _heuristic_title(scene_descriptions, story_summary)

    text = _extract_response_text(response)
    title = _clean_title(text)
    if not title or re.search(r"reading\\s+rainbow", title, re.IGNORECASE):
        return _heuristic_title(scene_descriptions, story_summary)
    return title


def _extract_image_bytes(image_response: Any) -> bytes | None:
    candidates = getattr(image_response, "candidates", None) or []
    parts: list[Any] = []
    if candidates:
        try:
            parts = list(candidates[0].content.parts)
        except Exception:
            parts = []
    else:
        parts = list(getattr(image_response, "parts", []) or [])

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data
    return None


def _generate_cover_image(scene_descriptions: list[str], story_summary: str) -> bytes | None:
    if not _env_enabled("ENABLE_STORYBOOK_COVER_IMAGE", default=False):
        return None
    if not PROJECT:
        return None
    try:
        from google import genai as google_genai
    except Exception as exc:
        logger.warning("Cover image generation unavailable (google-genai not installed): %s", exc)
        return None

    model = os.environ.get("STORYBOOK_COVER_IMAGE_MODEL", DEFAULT_VERTEX_IMAGE_MODEL).strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    client = google_genai.Client(vertexai=True, project=PROJECT, location=location)

    focus = story_summary.strip() or (scene_descriptions[0].strip() if scene_descriptions else "")
    prompt = (
        "storybook book cover illustration, ornate frame, painterly texture, warm lighting, "
        f"centered on: {focus}. No text, no logos."
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config=google_genai.types.GenerateContentConfig(
                response_modalities=[google_genai.types.MediaModality.IMAGE],
                image_config=google_genai.types.ImageConfig(
                    aspect_ratio="16:9",
                ),
            ),
        )
        return _extract_image_bytes(response)
    except Exception as exc:
        logger.warning("Cover image generation failed: %s", exc)
        return None


def _format_srt(entries: list[tuple[float, float, str]]) -> str:
    def _ts(seconds: float) -> str:
        ms = int(max(0.0, seconds) * 1000)
        h, rem = divmod(ms, 3600000)
        m, rem = divmod(rem, 60000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    for idx, (start, end, text) in enumerate(entries, start=1):
        if not text.strip():
            continue
        lines.append(str(idx))
        lines.append(f"{_ts(start)} --> {_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _ffprobe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _synthesize_tts_elevenlabs(text: str) -> bytes | None:
    global _ELEVENLABS_TTS_DISABLED_REASON
    if _ELEVENLABS_TTS_DISABLED_REASON:
        return None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if not api_key or not voice_id or not text:
        if api_key and not voice_id:
            logger.warning(
                "ELEVENLABS_VOICE_ID is not set — ElevenLabs TTS disabled. "
                "Set ELEVENLABS_VOICE_ID in env (find IDs at elevenlabs.io/voice-library)."
            )
        return None
    endpoint = os.environ.get(
        "ELEVENLABS_TTS_ENDPOINT",
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
    ).strip()
    model_id = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2").strip()
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.4,
            "similarity_boost": 0.75,
        },
    }
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
            if resp.status_code in {401, 402, 403}:
                _ELEVENLABS_TTS_DISABLED_REASON = f"http_{resp.status_code}"
                logger.warning(
                    "Disabling ElevenLabs TTS for this run after HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:240],
                )
    except Exception:
        return None
    return None


def _synthesize_tts_google(text: str) -> bytes | None:
    if not text:
        return None
    # Prefer ElevenLabs when available; fall back to Google TTS.
    audio = _synthesize_tts_elevenlabs(text)
    if audio:
        return audio
    try:
        from google.cloud import texttospeech
    except Exception:
        return None
    try:
        client = texttospeech.TextToSpeechClient()
        language_code = os.environ.get("STORYBOOK_TTS_LANG", "en-US")
        voice_name = os.environ.get("STORYBOOK_TTS_VOICE", "en-US-Neural2-F")
        try:
            speaking_rate = float(os.environ.get("STORYBOOK_TTS_RATE", "0.9"))
        except Exception:
            speaking_rate = 0.9
        try:
            pitch = float(os.environ.get("STORYBOOK_TTS_PITCH", "0.0"))
        except Exception:
            pitch = 0.0
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=language_code,
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=max(0.7, min(speaking_rate, 1.2)),
            pitch=max(-4.0, min(pitch, 4.0)),
        )
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice,
            audio_config=audio_config,
        )
        return response.audio_content
    except Exception:
        return None


async def _elevenlabs_generate_music(prompt: str, duration_seconds: float) -> bytes | None:
    global _ELEVENLABS_AUDIO_DISABLED_REASON
    if _ELEVENLABS_AUDIO_DISABLED_REASON:
        return None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(2.0, min(float(duration_seconds), 30.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    use_music_api = duration_seconds >= 10.0
    if use_music_api:
        music_endpoint = os.environ.get(
            "ELEVENLABS_MUSIC_ENDPOINT",
            "https://api.elevenlabs.io/v1/music",
        ).strip()
        payload = {
            "prompt": prompt,
            "music_length_ms": int(duration_seconds * 1000),
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(music_endpoint, headers=headers, json=payload)
                if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                    return resp.content
                if resp.status_code in {401, 402, 403, 429}:
                    _ELEVENLABS_AUDIO_DISABLED_REASON = f"music_http_{resp.status_code}"
                    logger.warning(
                        "Disabling ElevenLabs music/SFX for this run after HTTP %s: %s",
                        resp.status_code,
                        (resp.text or "")[:240],
                    )
                    return None
        except Exception:
            pass

    sound_endpoint = os.environ.get(
        "ELEVENLABS_SOUND_ENDPOINT",
        "https://api.elevenlabs.io/v1/sound-generation",
    ).strip()
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
            if resp.status_code in {401, 402, 403, 429}:
                _ELEVENLABS_AUDIO_DISABLED_REASON = f"sound_http_{resp.status_code}"
                logger.warning(
                    "Disabling ElevenLabs music/SFX for this run after HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:240],
                )
    except Exception:
        return None
    return None


async def _elevenlabs_generate_sfx(prompt: str, duration_seconds: float) -> bytes | None:
    global _ELEVENLABS_AUDIO_DISABLED_REASON
    if _ELEVENLABS_AUDIO_DISABLED_REASON:
        return None
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(1.0, min(float(duration_seconds), 6.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    sound_endpoint = os.environ.get(
        "ELEVENLABS_SOUND_ENDPOINT",
        "https://api.elevenlabs.io/v1/sound-generation",
    ).strip()
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
            if resp.status_code in {401, 402, 403, 429}:
                _ELEVENLABS_AUDIO_DISABLED_REASON = f"sfx_http_{resp.status_code}"
                logger.warning(
                    "Disabling ElevenLabs music/SFX for this run after HTTP %s: %s",
                    resp.status_code,
                    (resp.text or "")[:240],
                )
    except Exception:
        return None
    return None


def _score_scene(description: str) -> int:
    text = (description or "").lower()
    if not text:
        return 0
    keywords = [
        "magic", "magical", "sparkle", "sparkly", "glow", "glowing", "twinkle",
        "shimmer", "glitter", "rainbow", "portal", "wand", "spell", "fairy",
        "unicorn", "dragon", "castle", "moon", "star", "stars", "floating",
        "flying", "cloud", "crystal", "aurora", "light", "wonder",
    ]
    score = 0
    for kw in keywords:
        if kw in text:
            score += 1
    return score


def _choose_veo_indices(
    total: int,
    desired: int,
    descriptions: list[str],
    min_score: int,
) -> list[int]:
    if total <= 0 or desired <= 0:
        return []
    desired = max(0, min(desired, 2))
    if desired <= 0:
        return []
    scored: list[tuple[int, int]] = []
    for idx in range(total):
        desc = descriptions[idx] if idx < len(descriptions) else ""
        score = _score_scene(desc)
        if score >= min_score:
            scored.append((idx, score))
    if not scored:
        return []
    # Prefer higher score, then later scenes (feels more climactic).
    scored.sort(key=lambda item: (item[1], item[0]), reverse=True)
    picked: list[int] = []
    for idx, _score in scored:
        if idx not in picked:
            picked.append(idx)
        if len(picked) >= desired:
            break
    return picked


async def _download_any(
    session: httpx.AsyncClient,
    gcs: storage.Client,
    url: str,
    dest: Path,
) -> Path:
    if url.startswith("gs://"):
        path = url[5:]
        bucket_name, blob_path = path.split("/", 1)
        blob = gcs.bucket(bucket_name).blob(blob_path)
        dest.write_bytes(blob.download_as_bytes())
        return dest
    return await download_blob(session, url, dest)


def _call_veo_api(
    project: str,
    location: str,
    image_bytes: bytes,
    description: str,
    negative_prompt: str,
) -> str:
    """Starts a Veo long-running operation and returns the operation name."""
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    endpoint = (
        f"https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/"
        "publishers/google/models/veo-3.1-generate-001:predictLongRunning"
    )
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "instances": [
            {
                "prompt": description,
                "negativePrompt": negative_prompt,
                "image": {
                    "bytesBase64Encoded": base64.b64encode(image_bytes).decode(),
                },
            }
        ],
        "parameters": {
            "aspectRatio": "16:9",
            "durationSeconds": 5,
            "sampleCount": 1,
        },
    }

    resp = httpx.post(endpoint, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    operation_name = resp.json().get("name")
    if not operation_name:
        raise RuntimeError("Veo call did not return an operation name.")
    return operation_name


def _poll_veo_operation(project: str, location: str, operation_name: str, timeout_seconds: float = 160.0) -> str:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    auth_req = google.auth.transport.requests.Request()
    creds.refresh(auth_req)

    op_path = operation_name
    if operation_name.startswith("projects/"):
        op_path = operation_name
    elif operation_name.startswith("operations/"):
        op_path = f"projects/{project}/locations/{location}/{operation_name}"

    endpoint = f"https://{location}-aiplatform.googleapis.com/v1/{op_path}"
    headers = {"Authorization": f"Bearer {creds.token}"}

    deadline = time.monotonic() + timeout_seconds
    with httpx.Client(timeout=30.0) as client:
        while time.monotonic() < deadline:
            resp = client.get(endpoint, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("done"):
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                for key in ("uri", "gcsUri", "videoUri", "video_url", "url"):
                    candidate = payload.get("response", {}).get(key) if isinstance(payload.get("response"), dict) else None
                    if candidate:
                        return candidate
                raise RuntimeError("Veo operation completed without a media URL.")
            time.sleep(2.0)

    raise TimeoutError("Veo operation timed out.")


async def assemble(session_id: str) -> None:
    db = firestore.AsyncClient(project=PROJECT, database=FIRESTORE_DATABASE)
    gcs = storage.Client(project=PROJECT)

    # Pull session data from Firestore
    doc_ref = db.collection("storyteller_sessions").document(session_id)
    doc = await doc_ref.get()
    if not doc.exists:
        logger.error(f"Session {session_id} not found in Firestore.")
        return

    data = doc.to_dict()
    video_urls: list[str] = data.get("generated_asset_urls", [])
    scene_urls: list[str] = data.get("scene_asset_urls", [])
    scene_gcs_uris: list[str] = data.get("scene_asset_gcs_uris", [])
    scene_descriptions: list[str] = data.get("scene_descriptions", [])
    audio_urls: list[str] = data.get("elevenlabs_audio_chunks", [])
    story_summary = str(data.get("story_summary", "")).strip()
    child_name = str(data.get("child_name", "")).strip()
    raw_title = str(
        data.get("story_title")
        or data.get("title")
        or os.environ.get("STORYBOOK_TITLE", "")
    ).strip()
    if not raw_title or raw_title.lower() == "auto":
        title = _generate_story_title(scene_descriptions, story_summary)
    else:
        title = raw_title

    if not scene_urls and not scene_gcs_uris and not video_urls:
        logger.error(f"No scene assets for session {session_id}.")
        return

    if not isinstance(scene_urls, list):
        scene_urls = []
    if not isinstance(scene_gcs_uris, list):
        scene_gcs_uris = []

    scene_source_urls: list[str] = []
    max_scenes = max(len(scene_urls), len(scene_gcs_uris))
    for i in range(max_scenes):
        gcs_uri = scene_gcs_uris[i] if i < len(scene_gcs_uris) else ""
        url = scene_urls[i] if i < len(scene_urls) else ""
        gcs_uri = str(gcs_uri).strip() if gcs_uri else ""
        url = str(url).strip() if url else ""
        scene_source_urls.append(gcs_uri or url)
    scene_source_urls = [u for u in scene_source_urls if u]

    storyboard_review_report: dict[str, Any] = {
        "status": "skipped",
        "passes": [],
        "repairs_applied": [],
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # Download all assets concurrently
        async with httpx.AsyncClient() as http:
            audio_tasks = [
                download_blob(http, url, tmp / f"audio_{i:03d}.mp3")
                for i, url in enumerate(audio_urls)
            ]
            downloaded_audios = await asyncio.gather(*audio_tasks, return_exceptions=True)

            # Download stills (if present)
            still_paths: list[Path] = []
            if scene_source_urls:
                still_tasks = []
                for i, url in enumerate(scene_source_urls):
                    still_tasks.append(_download_any(http, gcs, url, tmp / f"scene_{i:03d}.img"))
                still_paths = await asyncio.gather(*still_tasks)

            # If we already have video clips (legacy), download them as fallback.
            downloaded_videos: list[Path] = []
            if video_urls:
                video_tasks = [
                    download_blob(http, url, tmp / f"clip_{i:03d}.mp4")
                    for i, url in enumerate(video_urls)
                ]
                downloaded_videos = await asyncio.gather(*video_tasks)

        try:
            await doc_ref.update({"assembly_status": "reviewing_storyboard"})
        except Exception:
            pass

        if still_paths:
            still_paths, storyboard_review_report = await _review_and_refine_storyboard(
                scene_descriptions=scene_descriptions,
                story_summary=story_summary,
                child_name=child_name,
                still_paths=still_paths,
                tmp=tmp,
            )
        else:
            storyboard_review_report = {
                "status": "skipped",
                "reason": "no still paths present",
                "passes": [],
                "repairs_applied": [],
            }

        try:
            await doc_ref.update(
                {
                    "assembly_status": "assembling",
                    "storyboard_review": storyboard_review_report,
                }
            )
        except Exception:
            pass

        valid_audios = [a for a in downloaded_audios if isinstance(a, Path) and a.exists()]
        enable_tts = _env_enabled("ENABLE_STORYBOOK_TTS", default=True)
        enable_captions = _env_enabled("ENABLE_STORYBOOK_CAPTIONS", default=True)
        force_tts = _env_enabled("FORCE_STORYBOOK_TTS", default=False)
        enable_music = _env_enabled("ENABLE_STORYBOOK_MUSIC", default=False)
        enable_sfx = _env_enabled("ENABLE_STORYBOOK_SFX", default=False)
        try:
            music_volume = float(os.environ.get("STORYBOOK_MUSIC_VOLUME", "0.25"))
        except Exception:
            music_volume = 0.25
        music_volume = max(0.0, min(music_volume, 1.0))
        try:
            sfx_volume = float(os.environ.get("STORYBOOK_SFX_VOLUME", "0.6"))
        except Exception:
            sfx_volume = 0.6
        sfx_volume = max(0.0, min(sfx_volume, 1.0))
        try:
            narration_volume = float(os.environ.get("STORYBOOK_NARRATION_VOLUME", "1.6"))
        except Exception:
            narration_volume = 1.6
        narration_volume = max(0.5, min(narration_volume, 3.0))
        enable_ducking = _env_enabled("ENABLE_STORYBOOK_DUCKING", default=True)
        try:
            sfx_max = int(os.environ.get("STORYBOOK_SFX_MAX", "3"))
        except Exception:
            sfx_max = 3
        try:
            sfx_min_score = int(os.environ.get("STORYBOOK_SFX_MIN_SCORE", "2"))
        except Exception:
            sfx_min_score = 2
        try:
            sfx_cooldown = int(os.environ.get("STORYBOOK_SFX_COOLDOWN", "1"))
        except Exception:
            sfx_cooldown = 1
        use_existing_audio = bool(valid_audios) and not force_tts
        cover_enabled = _env_enabled("ENABLE_STORYBOOK_COVER", default=False)
        cover_seconds = _clamp_float(os.environ.get("STORYBOOK_COVER_SECONDS", "3.5"), 3.5, 2.0, 6.0)
        cover_narration = os.environ.get("STORYBOOK_COVER_NARRATION", "").strip()
        if cover_enabled and not cover_narration:
            if title and child_name:
                cover_narration = f"Our story is called {title}, by {child_name}."
            elif title:
                cover_narration = f"Our story is called {title}."
            elif child_name:
                cover_narration = f"This story is by {child_name}."
            else:
                cover_narration = "Our story is about to begin."
        cover_description = (
            f"Cover: storybook title card for '{title}'."
            if title
            else "Cover: storybook title card."
        )
        cover_author = f"by {child_name}" if child_name else ""
        cover_image_bytes = _generate_cover_image(scene_descriptions, story_summary) if cover_enabled else None

        scene_count = len(still_paths) if still_paths else len(downloaded_videos)
        narration_lines = (
            _build_narration_segments(scene_descriptions, story_summary, scene_count)
            if scene_count
            else []
        )
        if cover_enabled:
            narration_lines = [cover_narration or ""] + narration_lines

        tts_audio_paths: list[Path | None] = []
        tts_audio_durations: list[float] = []
        if enable_tts and not use_existing_audio and narration_lines:
            for idx, line in enumerate(narration_lines):
                audio_bytes = _synthesize_tts_google(line) if line else None
                if audio_bytes:
                    audio_path = tmp / f"tts_{idx:03d}.mp3"
                    audio_path.write_bytes(audio_bytes)
                    tts_audio_paths.append(audio_path)
                    tts_audio_durations.append(_ffprobe_duration(audio_path))
                else:
                    tts_audio_paths.append(None)
                    tts_audio_durations.append(0.0)
        cover_audio_path: Path | None = None
        cover_audio_duration = 0.0
        if cover_enabled and enable_tts and use_existing_audio and cover_narration:
            cover_bytes = _synthesize_tts_google(cover_narration)
            if cover_bytes:
                cover_audio_path = tmp / "tts_cover.mp3"
                cover_audio_path.write_bytes(cover_bytes)
                cover_audio_duration = _ffprobe_duration(cover_audio_path)
        # Build per-scene segments (stills with optional Veo clips)
        segments: list[Path] = []
        scene_durations: list[float] = []
        if cover_enabled:
            cover_duration = cover_seconds
            cover_audio_len = 0.0
            if tts_audio_durations:
                cover_audio_len = tts_audio_durations[0] if len(tts_audio_durations) > 0 else 0.0
            elif cover_audio_duration > 0.0:
                cover_audio_len = cover_audio_duration
            if cover_audio_len > 0.0:
                cover_duration = max(cover_duration, cover_audio_len + 0.4)

            title_font = 60 if len(title) <= 18 else 52 if len(title) <= 28 else 44
            title_text = _ffmpeg_escape(title or "A Storybook Adventure")
            subtitle_text = _ffmpeg_escape(cover_author) if cover_author else ""
            vf_parts = [
                "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos",
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b2d5b",
                "eq=brightness=-0.02:saturation=1.08",
                "drawbox=x=60:y=90:w=1160:h=560:color=#000000@0.18:t=fill",
                "drawbox=x=60:y=90:w=1160:h=560:color=#f8f1dc@0.6:t=2",
                "drawbox=x=130:y=250:w=1020:h=200:color=#000000@0.35:t=fill",
                f"drawtext=text='{title_text}':fontcolor=white:fontsize={title_font}:x=(w-text_w)/2:y=h*0.42:shadowcolor=black:shadowx=2:shadowy=2",
            ]
            if subtitle_text:
                vf_parts.append(
                    f"drawtext=text='{subtitle_text}':fontcolor=white:fontsize=28:x=(w-text_w)/2:y=h*0.56:shadowcolor=black:shadowx=2:shadowy=2"
                )
            fade_out_start = max(0.2, cover_duration - 0.4)
            vf_parts.append(f"fade=t=in:st=0:d=0.4,fade=t=out:st={fade_out_start:.3f}:d=0.4")
            cover_vf = ",".join(vf_parts)
            cover_path = tmp / "segment_cover.mp4"
            if cover_image_bytes:
                cover_image_path = tmp / "cover.png"
                cover_image_path.write_bytes(cover_image_bytes)
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-loop", "1",
                        "-i", str(cover_image_path),
                        "-t", f"{cover_duration:.3f}",
                        "-vf", cover_vf,
                        "-r", "30",
                        "-pix_fmt", "yuv420p",
                        "-c:v", "libx264",
                        str(cover_path),
                    ],
                    check=True,
                    capture_output=True,
                )
            else:
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-f", "lavfi",
                        "-i", f"color=c=#0b2d5b:s=1280x720:d={cover_duration:.3f}",
                        "-vf", cover_vf,
                        "-r", "30",
                        "-pix_fmt", "yuv420p",
                        "-c:v", "libx264",
                        str(cover_path),
                    ],
                    check=True,
                    capture_output=True,
                )
            segments.append(cover_path)
            scene_durations.append(cover_duration)
        if still_paths:
            try:
                still_seconds = int(os.environ.get("FINAL_SCENE_SECONDS", "3"))
            except Exception:
                still_seconds = 3
            still_seconds = max(2, min(still_seconds, 6))

            use_veo = _env_enabled("ENABLE_VEO_FINAL", default=False)
            try:
                veo_count = int(os.environ.get("VEO_FINAL_SCENE_COUNT", "1"))
            except Exception:
                veo_count = 1
            veo_count = max(0, min(veo_count, 2))
            try:
                min_score = int(os.environ.get("VEO_MAGIC_MIN_SCORE", "2"))
            except Exception:
                min_score = 2
            min_score = max(1, min(min_score, 6))
            veo_indices = (
                _choose_veo_indices(len(still_paths), veo_count, scene_descriptions, min_score)
                if use_veo
                else []
            )

            veo_clips: dict[int, Path] = {}
            if veo_indices:
                location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
                for idx in veo_indices:
                    try:
                        img_bytes = still_paths[idx].read_bytes()
                        desc = ""
                        if idx < len(scene_descriptions):
                            desc = str(scene_descriptions[idx]).strip()
                        prompt = desc or "A whimsical story scene"
                        prompt = (
                            f"{prompt}. Gently animate with magical twinkles, soft breeze, and warm light. "
                            "Keep it child-friendly and dreamy."
                        )
                        op_name = await asyncio.to_thread(
                            _call_veo_api,
                            PROJECT,
                            location,
                            img_bytes,
                            prompt,
                            "scary, dark, violent, creepy, text, logos",
                        )
                        raw_url = await asyncio.to_thread(
                            _poll_veo_operation, PROJECT, location, op_name
                        )
                        clip_path = tmp / f"veo_{idx:03d}.mp4"
                        async with httpx.AsyncClient() as http:
                            await _download_any(http, gcs, raw_url, clip_path)
                        veo_clips[idx] = clip_path
                    except Exception as exc:
                        logger.warning("Veo final clip failed for scene %s: %s", idx, exc)

            veo_duration = 5.0
            audio_offset = 1 if cover_enabled else 0
            for idx, still_path in enumerate(still_paths):
                segment_path = tmp / f"segment_{idx:03d}.mp4"
                base_duration = veo_duration if idx in veo_clips else float(still_seconds)
                audio_idx = idx + audio_offset
                if tts_audio_durations and audio_idx < len(tts_audio_durations):
                    audio_len = tts_audio_durations[audio_idx]
                    if audio_len > 0:
                        base_duration = max(base_duration, audio_len + 0.4)
                scene_durations.append(base_duration)
                if idx in veo_clips:
                    # Normalize Veo clip to common codec/size
                    normalized_path = tmp / f"veo_norm_{idx:03d}.mp4"
                    subprocess.run([
                        "ffmpeg", "-y",
                        "-i", str(veo_clips[idx]),
                        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                               "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b2d5b",
                        "-r", "30",
                        "-pix_fmt", "yuv420p",
                        "-c:v", "libx264",
                        str(normalized_path),
                    ], check=True, capture_output=True)
                    extra = max(0.0, base_duration - veo_duration)
                    if extra > 0.05:
                        hold_path = tmp / f"hold_{idx:03d}.mp4"
                        subprocess.run([
                            "ffmpeg", "-y",
                            "-loop", "1",
                            "-t", f"{extra:.3f}",
                            "-i", str(still_path),
                            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                                   "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b2d5b",
                            "-r", "30",
                            "-pix_fmt", "yuv420p",
                            "-c:v", "libx264",
                            str(hold_path),
                        ], check=True, capture_output=True)
                        concat_hold = tmp / f"concat_hold_{idx:03d}.txt"
                        concat_hold.write_text(
                            "\n".join([f"file '{normalized_path.name}'", f"file '{hold_path.name}'"])
                        )
                        subprocess.run([
                            "ffmpeg", "-y",
                            "-f", "concat", "-safe", "0",
                            "-i", str(concat_hold),
                            "-c", "copy",
                            str(segment_path),
                        ], check=True, capture_output=True)
                    else:
                        segment_path.write_bytes(normalized_path.read_bytes())
                else:
                    # Create a short video from still
                    subprocess.run([
                        "ffmpeg", "-y",
                        "-loop", "1",
                        "-t", f"{base_duration:.3f}",
                        "-i", str(still_path),
                        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
                               "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b2d5b",
                        "-r", "30",
                        "-pix_fmt", "yuv420p",
                        "-c:v", "libx264",
                        str(segment_path),
                    ], check=True, capture_output=True)
                segments.append(segment_path)

        if downloaded_videos and not still_paths:
            for clip in downloaded_videos:
                segments.append(clip)
                scene_durations.append(_ffprobe_duration(clip))

        # Fallback to existing video clips if no stills available
        if not segments and downloaded_videos:
            segments = downloaded_videos

        if not scene_durations and segments:
            scene_durations = [_ffprobe_duration(p) for p in segments]

        music_paths: list[Path | None] = []
        sfx_paths: list[Path | None] = [None] * len(scene_durations)
        audio_scene_descriptions = scene_descriptions
        still_paths_for_audio = still_paths
        if cover_enabled:
            audio_scene_descriptions = [cover_description] + scene_descriptions
            still_paths_for_audio = None
        music_cues, sfx_cues = _plan_audio_cues(
            audio_scene_descriptions,
            story_summary,
            len(scene_durations),
            still_paths=still_paths_for_audio,
        )
        if cover_enabled:
            sfx_cues.pop(0, None)

        music_cue_default = _clamp_float(
            os.environ.get("STORYBOOK_MUSIC_CUE_SECONDS", "10"),
            10.0,
            2.0,
            30.0,
        )
        sfx_cue_default = _clamp_float(
            os.environ.get("STORYBOOK_SFX_CUE_SECONDS", "2"),
            2.0,
            0.6,
            6.0,
        )

        if enable_music and os.environ.get("ELEVENLABS_API_KEY") and scene_durations:
            for idx, duration in enumerate(scene_durations):
                cue = music_cues.get(idx)
                if not cue:
                    music_paths.append(None)
                    continue
                desc = audio_scene_descriptions[idx] if idx < len(audio_scene_descriptions) else ""
                prompt = str(cue.get("prompt") or "").strip() or _build_music_prompt(desc, idx, len(scene_durations))
                cue_seconds = cue.get("duration_seconds")
                try:
                    cue_seconds = float(cue_seconds)
                except Exception:
                    cue_seconds = music_cue_default
                cue_seconds = max(2.0, min(float(cue_seconds), duration))
                audio_bytes = await _elevenlabs_generate_music(prompt, cue_seconds)
                if audio_bytes:
                    music_path = tmp / f"music_{idx:03d}.mp3"
                    music_path.write_bytes(audio_bytes)
                    music_paths.append(music_path)
                else:
                    music_paths.append(None)
        else:
            music_paths = [None] * len(scene_durations)

        if enable_sfx and os.environ.get("ELEVENLABS_API_KEY") and scene_durations:
            if sfx_cues:
                logger.info("Storybook SFX scenes selected: %s", sorted(sfx_cues.keys()))
            for idx, duration in enumerate(scene_durations):
                cue = sfx_cues.get(idx)
                if not cue:
                    continue
                desc = audio_scene_descriptions[idx] if idx < len(audio_scene_descriptions) else ""
                prompt = str(cue.get("prompt") or "").strip() or _build_sfx_prompt(desc)
                cue_seconds = cue.get("duration_seconds")
                try:
                    cue_seconds = float(cue_seconds)
                except Exception:
                    cue_seconds = sfx_cue_default
                cue_seconds = max(0.6, min(float(cue_seconds), duration))
                audio_bytes = await _elevenlabs_generate_sfx(prompt, cue_seconds)
                if audio_bytes:
                    sfx_path = tmp / f"sfx_{idx:03d}.mp3"
                    sfx_path.write_bytes(audio_bytes)
                    sfx_paths[idx] = sfx_path

        if not segments:
            logger.error(f"No renderable segments for session {session_id}.")
            return

        # Create FFmpeg concat list
        concat_list = tmp / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{v.name}'" for v in segments)
        )

        base_video = tmp / "story_base.mp4"
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy",
            "-movflags", "+faststart",
            str(base_video),
        ], check=True, capture_output=True)

        def _build_padded_audio(
            audio_paths: list[Path | None],
            durations: list[float],
            fade_in: float = 0.0,
            fade_out: float = 0.0,
        ) -> Path | None:
            if not durations:
                return None
            padded_paths: list[Path] = []
            for idx, duration in enumerate(durations):
                src = audio_paths[idx] if idx < len(audio_paths) else None
                padded_path = tmp / f"audio_pad_{idx:03d}.wav"
                if src and src.exists():
                    filters: list[str] = []
                    if fade_in > 0:
                        filters.append(f"afade=t=in:st=0:d={fade_in}")
                    if fade_out > 0:
                        fade_start = max(0.0, duration - fade_out)
                        filters.append(f"afade=t=out:st={fade_start:.3f}:d={fade_out}")
                    filters.append(f"apad=pad_dur={duration:.3f},atrim=0:{duration:.3f}")
                    subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-i", str(src),
                            "-filter_complex", ",".join(filters),
                            "-ar", "44100",
                            "-ac", "2",
                            str(padded_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                else:
                    subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-f", "lavfi",
                            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                            "-t", f"{duration:.3f}",
                            str(padded_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                padded_paths.append(padded_path)
            audio_concat_list = tmp / "audio_concat.txt"
            audio_concat_list.write_text(
                "\n".join(f"file '{p.name}'" for p in padded_paths)
            )
            audio_full = tmp / "merged_audio.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", str(audio_concat_list),
                    "-c:a", "pcm_s16le",
                    str(audio_full),
                ],
                check=True,
                capture_output=True,
            )
            return audio_full

        audio_track: Path | None = None
        if tts_audio_paths and len(tts_audio_paths) == len(scene_durations) and any(tts_audio_paths):
            audio_track = _build_padded_audio(tts_audio_paths, scene_durations)
        elif use_existing_audio and valid_audios:
            existing_paths: list[Path | None] = list(valid_audios)
            if cover_enabled:
                existing_paths = [cover_audio_path] + existing_paths
            audio_track = _build_padded_audio(existing_paths, scene_durations)

        music_track: Path | None = None
        if enable_music and music_paths and any(music_paths):
            music_track = _build_padded_audio(music_paths, scene_durations, fade_in=0.4, fade_out=0.6)

        sfx_track: Path | None = None
        if enable_sfx and sfx_paths and any(sfx_paths):
            sfx_track = _build_padded_audio(sfx_paths, scene_durations, fade_in=0.15, fade_out=0.25)

        if music_track and music_track.exists():
            if audio_track and audio_track.exists() and sfx_track and sfx_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                duck = (
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked];"
                    if enable_ducking
                    else ""
                )
                mix = (
                    "[voice][ducked][sfx]amix=inputs=3:duration=shortest:dropout_transition=0"
                    if enable_ducking
                    else "[voice][music][sfx]amix=inputs=3:duration=shortest:dropout_transition=0"
                )
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(audio_track),
                        "-i", str(music_track),
                        "-i", str(sfx_track),
                        "-filter_complex",
                        f"[0:a]volume={narration_volume}[voice];[1:a]volume={music_volume}[music];[2:a]volume={sfx_volume}[sfx];{duck}{mix}",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            elif audio_track and audio_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                duck = (
                    "[music][voice]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=400[ducked];"
                    if enable_ducking
                    else ""
                )
                mix = (
                    "[voice][ducked]amix=inputs=2:duration=shortest:dropout_transition=0"
                    if enable_ducking
                    else "[voice][music]amix=inputs=2:duration=shortest:dropout_transition=0"
                )
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(audio_track),
                        "-i", str(music_track),
                        "-filter_complex",
                        f"[0:a]volume={narration_volume}[voice];[1:a]volume={music_volume}[music];{duck}{mix}",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            elif sfx_track and sfx_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(music_track),
                        "-i", str(sfx_track),
                        "-filter_complex",
                        f"[0:a]volume={music_volume}[music];[1:a]volume={sfx_volume}[sfx];[music][sfx]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            else:
                music_only = tmp / "music_only.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(music_track),
                        "-filter:a", f"volume={music_volume}",
                        "-c:a", "pcm_s16le",
                        str(music_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = music_only
        elif sfx_track and sfx_track.exists():
            if audio_track and audio_track.exists():
                mixed_audio = tmp / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(audio_track),
                        "-i", str(sfx_track),
                        "-filter_complex",
                        f"[0:a]volume={narration_volume}[voice];[1:a]volume={sfx_volume}[sfx];[voice][sfx]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a", "pcm_s16le",
                        str(mixed_audio),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = mixed_audio
            else:
                sfx_only = tmp / "sfx_only.wav"
                subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-i", str(sfx_track),
                        "-filter:a", f"volume={sfx_volume}",
                        "-c:a", "pcm_s16le",
                        str(sfx_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_track = sfx_only

        # If only narration is present, still apply the narration volume boost.
        if (
            audio_track
            and audio_track.exists()
            and not (music_track and music_track.exists())
            and not (sfx_track and sfx_track.exists())
            and abs(narration_volume - 1.0) > 0.01
        ):
            boosted = tmp / "audio_narration_boosted.wav"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(audio_track),
                    "-filter:a", f"volume={narration_volume}",
                    "-c:a", "pcm_s16le",
                    str(boosted),
                ],
                check=True,
                capture_output=True,
            )
            audio_track = boosted

        output_path = tmp / "story_final.mp4"
        if audio_track and audio_track.exists():
            subprocess.run([
                "ffmpeg", "-y",
                "-i", str(base_video),
                "-i", str(audio_track),
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                "-movflags", "+faststart",
                str(output_path),
            ], check=True, capture_output=True)
        else:
            output_path = base_video

        if enable_captions and narration_lines and scene_durations:
            # Build a drawtext filtergraph with one entry per scene, each time-gated
            # via enable='between(t,start,end)'. Positioned in the upper frame area
            # to look like a storybook page rather than movie subtitles.
            drawtext_parts: list[str] = []
            t_cursor = 0.0
            for duration, line in zip(scene_durations, narration_lines):
                if not line:
                    t_cursor += duration
                    continue
                wrapped = _wrap_caption(line, width=30)
                escaped = _ffmpeg_escape(wrapped)
                t_start = t_cursor
                t_end = t_cursor + duration
                draw = (
                    f"drawtext=text='{escaped}'"
                    f":fontcolor=white"
                    f":fontsize=46"
                    f":x=(w-text_w)/2"
                    f":y=h*0.10"
                    f":box=1"
                    f":boxcolor=0x00000099"
                    f":boxborderw=18"
                    f":font=Sans"
                    f":shadowcolor=black:shadowx=2:shadowy=2"
                    f":line_spacing=8"
                    f":enable='between(t,{t_start:.3f},{t_end:.3f})'"
                )
                drawtext_parts.append(draw)
                t_cursor += duration
            if drawtext_parts:
                captioned_path = tmp / "story_captioned.mp4"
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-y",
                            "-i", str(output_path),
                            "-vf", ",".join(drawtext_parts),
                            "-c:a", "copy",
                            "-movflags", "+faststart",
                            str(captioned_path),
                        ],
                        check=True,
                        capture_output=True,
                    )
                    output_path = captioned_path
                except Exception as exc:
                    logger.warning("Book-style caption overlay failed: %s", exc)

        logger.info(f"FFmpeg complete. Final video: {output_path.stat().st_size} bytes.")

        # Upload to GCS final videos bucket
        bucket = gcs.bucket(GCS_FINAL_BUCKET)
        blob = bucket.blob(f"{session_id}/story_final.mp4")
        blob.content_disposition = f'attachment; filename="{session_id}-story.mp4"'
        blob.upload_from_filename(str(output_path), content_type="video/mp4")
        final_gcs_uri = f"gs://{GCS_FINAL_BUCKET}/{session_id}/story_final.mp4"
        # Final videos bucket is public-read via bucket IAM, so a stable public URL
        # is the simplest and most reliable handoff for Theater Mode.
        final_url = f"https://storage.googleapis.com/{GCS_FINAL_BUCKET}/{session_id}/story_final.mp4"

        logger.info(f"Uploaded final video: {final_url}")

        # Write final URL back to Firestore session doc
        await doc_ref.update({
            "final_video_url": final_url,
            "final_video_gcs_uri": final_gcs_uri,
            "assembly_status": "complete",
            "storyboard_review": storyboard_review_report,
        })

        logger.info(f"Session {session_id} assembly complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Assemble the final story movie.")
    parser.add_argument("--session_id", required=True, help="ADK session ID")
    args = parser.parse_args()
    asyncio.run(assemble(args.session_id))


if __name__ == "__main__":
    main()
