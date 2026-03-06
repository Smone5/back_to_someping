"""All native ADK async tools for the Interactive Storyteller.

Every tool is an async Python function available to the agent.
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import textwrap
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import tenacity
from google import genai as google_genai
from google.adk.tools import ToolContext
from pydantic import BaseModel, ConfigDict, Field
from shared.meta_learning import build_principles_injection_text, record_prompt_feedback

from backend.event_bus import get_session_iot_config, publish_session_event, schedule_background_task
from backend.media_cache import store_media
from backend.schemas import theater_mode_event

logger = logging.getLogger(__name__)

_still_semaphore = asyncio.Semaphore(1)
_veo_semaphore = asyncio.Semaphore(1)
_last_light_call: float = 0.0
_LIGHT_COOLDOWN_SECONDS = 5.0
_assembled_sessions: set[str] = set()
_card_generating: set[str] = set()
_session_generating: set[str] = set()
# Per-session latest pending request: if a new scene arrives while an image is
# generating, queue the latest and suppress publishing the stale image.
_session_pending: dict[str, "VisualArgs"] = {}
_session_cancel_current: set[str] = set()
_session_last_gen_at: dict[str, float] = {}
_session_last_gen_text: dict[str, str] = {}
_CTRL_TOKEN_RE = re.compile(r"<ctrl\\d+>", flags=re.IGNORECASE)


class VisualArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str = Field(..., description="Positive scene description for image and video generation.")
    base_description: str | None = Field(
        default=None,
        description="Short, continuity-free scene description for fast first-pass generation.",
    )
    negative_prompt: str = Field(
        default="aesthetic: U-rated Disney/Pixar, STRICTLY non-threatening, no monsters, no scary creatures, no dark themes, nothing frightening to a 4-year-old, no text, no words, no letters, no labels, no signatures, no watermarks, no quotes, no captions, no writing",
        description="Single high-weight safety style instruction.",
    )
    aspect_ratio: str = Field(default="16:9", description="Desired image aspect ratio.")
    image_size: str = Field(default="512px", description="Image resolution preset.")
    image_model: str = Field(default="gemini-3.1-flash-image-preview", description="Image model id.")
    reference_image_b64: str | None = Field(default=None, description="Base64-encoded reference image.")
    reference_image_mime: str | None = Field(default=None, description="Reference image mime type.")
    illustration_style: str | None = Field(default=None, description="The chosen illustration style for this session.")

CLASSIC_STORYBOOK_STYLES = [
    "Ultra-consistent, high-quality children's storybook illustration. Watercolor and ink, reminiscent of Beatrix Potter or E.H. Shepard. Soft pastels, gentle lighting, timeless and nostalgic.",
    "Ultra-consistent, high-quality children's storybook illustration. Dreamy atmospheric watercolor, reminiscent of Jon Klassen. Muted earthy tones, soft textures, minimalist and elegant.",
    "Ultra-consistent, high-quality children's storybook illustration. Classic Golden Books style, vintage 1950s children's book aesthetic. Gouache, warm bright colors, whimsical and cozy.",
    "Ultra-consistent, high-quality children's storybook illustration. Textured paper collage aesthetic, reminiscent of Eric Carle. Vibrant saturated colors, bold playful shapes, highly textured.",
    "Ultra-consistent, high-quality children's storybook illustration. Whimsical pen and wash, reminiscent of Quentin Blake. Energetic loose line work, expressive and joyful, colorful loose watercolor washes."
]


class MusicArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    mood: str = Field(..., description="happy, suspenseful, magical, triumphant")
    intensity: int = Field(default=5, ge=1, le=10)


class CharacterFactArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    character_name: str
    fact: str


class ChildNameArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(..., description="Child's preferred first name")


class AssembleArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: str


class LightArgs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    hex_color: str
    scene_description: str = ""


def _session_id_from_context(tool_context: ToolContext | None) -> str | None:
    if tool_context is None:
        return None
    try:
        return tool_context.session.id
    except Exception:
        return None


def _continuity_anchor_text(tool_context: ToolContext | None) -> str:
    """Builds compact continuity anchors to keep visuals temporally consistent."""
    if tool_context is None:
        return ""
    try:
        state = dict(getattr(tool_context, "state", {}) or {})
    except Exception:
        return ""

    anchors: list[str] = []
    child_name = str(state.get("child_name", "")).strip()
    story_summary = str(state.get("story_summary", "")).strip()
    character_facts = str(state.get("character_facts", "")).strip()
    previous_scene_description = str(state.get("previous_scene_base_description", "")).strip()
    if not previous_scene_description:
        previous_scene_description = str(state.get("previous_scene_description", "")).strip()

    # Intentionally omit child name from visual anchors to avoid image model refusals.
    if character_facts:
        anchors.append(
            "canonical character facts: "
            + character_facts.replace("\n", "; ").strip()[:260]
        )
    if story_summary:
        anchors.append(
            "recent story context: " + story_summary.replace("\n", " ").strip()[-340:]
        )
    if previous_scene_description:
        anchors.append(
            "carry-over from last scene: "
            + previous_scene_description.replace("\n", " ").strip()[-220:]
        )

    return "; ".join(anchors)


_ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
}
_ALLOWED_IMAGE_SIZES = {"512px", "1K", "2K", "4K"}


def _resolve_image_preferences(tool_context: ToolContext | None) -> tuple[str, str, str]:
    state = {}
    if tool_context:
        try:
            state = dict(getattr(tool_context, "state", {}) or {})
        except Exception:
            state = {}

    aspect_ratio = str(state.get("preferred_aspect_ratio", "16:9"))
    if aspect_ratio not in _ALLOWED_ASPECT_RATIOS:
        aspect_ratio = "16:9"

    image_size = os.environ.get("IMAGE_SIZE", "").strip() or str(
        state.get("preferred_image_size", "512px")
    )
    if image_size not in _ALLOWED_IMAGE_SIZES:
        image_size = "512px"

    image_model = os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image-preview").strip()
    # Enforce Gemini Flash image model only.
    if not image_model or "flash-image" not in image_model:
        image_model = "gemini-3.1-flash-image-preview"

    return aspect_ratio, image_size, image_model


def _resolve_continuity_mode() -> str:
    raw = os.environ.get("IMAGE_CONTINUITY_MODE", "thumbnail").strip().lower()
    if raw in {"thumbnail", "text", "off"}:
        return "text" if raw == "off" else raw
    return "thumbnail"


def _make_thumbnail_b64(image_bytes: bytes, max_side: int = 384) -> tuple[str, str] | None:
    try:
        from PIL import Image
    except Exception:
        return None

    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img = img.convert("RGB")
            img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            out = BytesIO()
            img.save(out, format="JPEG", quality=70, optimize=True, progressive=True)
            b64 = base64.b64encode(out.getvalue()).decode("ascii")
            return b64, "image/jpeg"
    except Exception:
        return None


def _extract_image_blob(image_response: Any) -> tuple[bytes, str] | None:
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
            return inline.data, getattr(inline, "mime_type", "image/png")

    # Log what we got for debugging
    prompt_feedback = getattr(image_response, "prompt_feedback", None)
    if prompt_feedback:
        logger.warning("Image prompt_feedback: %s", prompt_feedback)
    logger.warning(
        "_extract_image_blob: no inline_data found. Parts count=%d, part types=%s",
        len(parts),
        [type(p).__name__ for p in parts],
    )
    for i, part in enumerate(parts):
        attrs = [a for a in dir(part) if not a.startswith("_")]
        logger.warning("  Part[%d] attrs: %s", i, attrs)
        # Check for text part (model may have responded with text instead of image)
        if getattr(part, "text", None):
            logger.warning("  Part[%d] text: %s", i, str(part.text)[:200])
    return None


def _extract_first_uri(obj: Any) -> str | None:
    if isinstance(obj, dict):
        for key in ("uri", "gcsUri", "videoUri", "video_url", "url"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
        for value in obj.values():
            found = _extract_first_uri(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _extract_first_uri(item)
            if found:
                return found
    return None


def _gcs_signed_url(gs_uri: str, project: str) -> str | None:
    # gs://bucket/path -> signed https URL
    from google.cloud import storage

    path = gs_uri[5:]
    bucket_name, blob_path = path.split("/", 1)
    client = storage.Client(project=project)
    blob = client.bucket(bucket_name).blob(blob_path)
    try:
        # On Cloud Run, we must provide the service_account_email to sign.
        # We can try to get it from the client or fallback to environment.
        sa_email = getattr(client, "service_account_email", None)
        if not sa_email:
             # Typical for appspot/compute default
             sa_email = f"{project}@appspot.gserviceaccount.com"
             
        return blob.generate_signed_url(
            version="v4", 
            expiration=timedelta(hours=2), 
            method="GET",
            service_account_email=sa_email
        )
    except Exception as exc:
        # Local/user ADC often cannot sign URLs. Return None so callers can use
        # a deterministic inline-data fallback rather than a broken 403 URL.
        logger.warning("Signed URL generation failed for gs://%s/%s: %s", bucket_name, blob_path, exc)
        return None


def _normalize_media_url(url_or_uri: str, project: str) -> str | None:
    if url_or_uri.startswith("gs://"):
        return _gcs_signed_url(url_or_uri, project)
    return url_or_uri


def _veo_enabled() -> bool:
    # Live story should stay fast. Gate Veo during live turns separately.
    return _env_enabled("ENABLE_VEO_LIVE", default=False)


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _build_fallback_scene_svg_data_url(description: str) -> str:
    safe_text = html.escape(description[:220] or "A magical story scene")
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#1b0b3b"/>
      <stop offset="55%" stop-color="#3f1c73"/>
      <stop offset="100%" stop-color="#0b2d5b"/>
    </linearGradient>
    <radialGradient id="glow" cx="50%" cy="35%" r="45%">
      <stop offset="0%" stop-color="#ffd166" stop-opacity="0.55"/>
      <stop offset="100%" stop-color="#ffd166" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="1280" height="720" fill="url(#bg)"/>
  <rect width="1280" height="720" fill="url(#glow)"/>
  <circle cx="260" cy="560" r="180" fill="#ff7fbe" opacity="0.5"/>
  <circle cx="630" cy="590" r="220" fill="#7cf8cf" opacity="0.45"/>
  <circle cx="1020" cy="550" r="170" fill="#7ec9ff" opacity="0.5"/>
</svg>"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def _upload_scene_still(
    image_bytes: bytes,
    project: str,
    session_id: str | None,
) -> tuple[str | None, str | None]:
    bucket_name = os.environ.get("GCS_ASSETS_BUCKET", "").strip()
    if not bucket_name:
        return None, None

    from google.cloud import storage

    scene_id = session_id or "anonymous"
    object_path = f"{scene_id}/scene_stills/{int(time.time() * 1000)}.png"
    client = storage.Client(project=project)
    blob = client.bucket(bucket_name).blob(object_path)
    blob.upload_from_string(image_bytes, content_type="image/png")
    gcs_uri = f"gs://{bucket_name}/{object_path}"
    display_url = _normalize_media_url(gcs_uri, project=project)
    return display_url, gcs_uri


def _generate_scene_still(
    description: str,
    negative_prompt: str,
    aspect_ratio: str,
    image_size: str,
    image_model: str,
    reference_image_b64: str | None = None,
    reference_image_mime: str | None = None,
    style_prompt: str | None = None,
) -> tuple[bytes, str]:
    # API-key path keeps still-image generation available even when Vertex AI API is disabled.
    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        logger.warning("GOOGLE_API_KEY is not set. Still image generation depends on it.")
        raise ValueError("Missing GOOGLE_API_KEY for image generation.")
    client = google_genai.Client(api_key=api_key)
    meta_guidance = build_principles_injection_text("interactive_scene_visual")
    prompt = f"{description}\n\n"
    if meta_guidance:
        prompt += f"{meta_guidance}\n\n"
    if negative_prompt:
        prompt += f"Safety style: {negative_prompt}\n"
        
    if style_prompt:
        prompt += f"Style: {style_prompt} NO 3D rendering. NO Pixar style. Completely uniform art style, no text overlays."
    else:
        prompt += "Style: Ultra-consistent, high-quality children's storybook illustration. Rich, warm, hand-drawn 2D aesthetic (like watercolor or classic storybooks). NO 3D rendering. NO Pixar style. Completely uniform art style, no text overlays."

    if reference_image_b64:
        prompt = f"{prompt}\nUse the provided reference image for continuity in characters and setting."

    contents: list[Any] = [prompt]
    if reference_image_b64:
        try:
            ref_bytes = base64.b64decode(reference_image_b64)
            contents.append(
                google_genai.types.Part.from_bytes(
                    data=ref_bytes,
                    mime_type=reference_image_mime or "image/jpeg",
                    media_resolution=google_genai.types.PartMediaResolutionLevel.MEDIA_RESOLUTION_LOW,
                )
            )
        except Exception:
            logger.warning("Failed to decode reference image for continuity; continuing without it.")

    response = client.models.generate_content(
        model=image_model,
        contents=contents,
        config=google_genai.types.GenerateContentConfig(
            response_modalities=[google_genai.types.MediaModality.IMAGE],
            # 16:9 aspect ratio generates fewer pixels than default 1:1, usually speeding it up!
            image_config=google_genai.types.ImageConfig(
                aspect_ratio=aspect_ratio,
                image_size=image_size,
            ),
        ),
    )
    blob = _extract_image_blob(response)
    if not blob:
        # Log more detail about what the response actually contained
        logger.warning("Image response had no image blob. Response: %s", str(response)[:500])
        raise RuntimeError("Image model returned no image bytes.")
    return blob


def _poll_veo_operation(project: str, location: str, operation_name: str, timeout_seconds: float = 120.0) -> str:
    import google.auth
    import google.auth.transport.requests

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
                uri = _extract_first_uri(payload.get("response") or payload)
                if uri:
                    return uri
                raise RuntimeError("Veo operation completed without a media URL.")
            time.sleep(2.0)

    raise TimeoutError("Veo operation timed out.")


async def generate_scene_visuals(
    description: str,
    negative_prompt: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """
    Generates a scene image and a short video clip in the background.

    Args:
        description: A purely visual description of the scene to draw. MUST NOT contain conversational text like 'I want to go to' or the child's name. Just visually describe the magical setting and characters.
        negative_prompt: Visual elements to avoid in the generation.
    """
    try:
        _tool_entry_t = time.monotonic()
        _desc_preview = repr(description)[:120]
        _neg_preview = repr(negative_prompt)[:60]
        logger.info(f"⏱️ TIMING [generate_scene_visuals] ENTRY at t=0ms | desc={_desc_preview}, negative={_neg_preview}")
        continuity = _continuity_anchor_text(tool_context)
        base_description = description.strip()
        # Remove explicit child name from prompts to reduce image model refusals.
        if tool_context:
            try:
                state = dict(getattr(tool_context, "state", {}) or {})
            except Exception:
                state = {}
            child_name = str(state.get("child_name", "")).strip()
            if child_name and child_name.lower() != "friend":
                base_description = re.sub(rf"\\b{re.escape(child_name)}\\b", "the young hero", base_description, flags=re.IGNORECASE)
        visual_description = base_description
        if continuity:
            visual_description = (
                f"{visual_description}. Keep temporal/character continuity with: {continuity}."
            )

        aspect_ratio, image_size, image_model = _resolve_image_preferences(tool_context)
        continuity_mode = _resolve_continuity_mode()
        reference_image_b64: str | None = None
        reference_image_mime: str | None = None
        using_toy_reference = False
        if tool_context:
            try:
                state = dict(getattr(tool_context, "state", {}) or {})
            except Exception:
                state = {}
            toy_b64 = str(state.get("toy_reference_thumbnail_b64", "") or "").strip()
            toy_mime = str(state.get("toy_reference_thumbnail_mime", "") or "").strip()
            if toy_b64:
                reference_image_b64 = toy_b64
                reference_image_mime = toy_mime or "image/jpeg"
                using_toy_reference = True
            elif continuity_mode == "thumbnail":
                candidate_b64 = str(state.get("previous_scene_thumbnail_b64", "") or "").strip()
                candidate_mime = str(state.get("previous_scene_thumbnail_mime", "") or "").strip()
                if candidate_b64:
                    reference_image_b64 = candidate_b64
                    reference_image_mime = candidate_mime or "image/jpeg"

        if using_toy_reference:
            visual_description = (
                f"{visual_description} The sidekick should match the toy reference image "
                "and feel like a soft, kid-friendly toy (no brand logos)."
            )

        session_id = _session_id_from_context(tool_context)
        
        # Pick or retrieve a persistent illustration style for this session.
        session_style: str | None = None
        if tool_context:
            try:
                # ADK ToolContext.state is usually a dict-like, but we ensure string keys.
                state = tool_context.state
                session_style = str(state.get("illustration_style") or "").strip()
                if not session_style:
                    import random
                    session_style = random.choice(CLASSIC_STORYBOOK_STYLES)
                    state["illustration_style"] = session_style
                    logger.info("Assigned new random illustration style for session %s: %s", session_id, session_style[:40])
            except Exception as e:
                logger.warning("Could not manage illustration_style state: %s", str(e))

        try:
            args = VisualArgs(
                description=visual_description,
                base_description=base_description,
                negative_prompt=negative_prompt or VisualArgs.model_fields["negative_prompt"].default,
                aspect_ratio=aspect_ratio,
                image_size=image_size,
                image_model=image_model,
                reference_image_b64=reference_image_b64,
                reference_image_mime=reference_image_mime,
                illustration_style=session_style,
            )
        except Exception as ve:
            logger.error(f"VisualArgs validation failed: {ve}", exc_info=True)
            raise

        # Hard gate: do NOT generate scene images during onboarding turns (name
        # collection, name confirmation, camera question). The agent sometimes
        # calls this when the child gives their name and story idea in the same
        # utterance, producing a generic "wrong image" before the story starts.
        if tool_context:
            try:
                state_snapshot = getattr(tool_context, "state", {}) or {}
                current_turn = int(state_snapshot.get("turn_number", 1) or 1)
            except (ValueError, TypeError):
                current_turn = 1
            # The skip logic used to be here, but was removed because it relied on
            # stale `tool_context.state` snapshots which incorrectly blocked
            # valid shortcircuited story generations on Turn 1.
        # Persist scene description in state so the model knows what's displayed on screen
        if tool_context:
            tool_context.state["previous_scene_description"] = tool_context.state.get(
                "current_scene_description", ""
            )
            tool_context.state["previous_scene_base_description"] = tool_context.state.get(
                "current_scene_base_description", ""
            )
            tool_context.state["current_scene_description"] = visual_description
            tool_context.state["current_scene_base_description"] = base_description

        _elapsed = int((time.monotonic() - _tool_entry_t) * 1000)
        logger.info(f"⏱️ TIMING [generate_scene_visuals] entry processing complete | elapsed={_elapsed}ms | session={session_id}")

        # Guard: if a generation is already running for this session, queue the latest
        # request if it's DIFFERENT. If it's the SAME description, just return success.
        if session_id and session_id in _session_generating:
            # Check if this precise description is already in the pipeline
            if tool_context and tool_context.state.get("current_scene_description") == visual_description:
                logger.info("[generate_scene_visuals] Same description already in progress for %s; skipping duplicate.", session_id)
                return "Drawing already in progress. If you haven't given choices yet, do so now. If you already gave choices, stop talking immediately."
            
            _session_pending[session_id] = args
            _session_cancel_current.add(session_id)
            logger.info(
                "[generate_scene_visuals] Queued NEW scene description — generation already in progress "
                "for session %s.",
                session_id,
            )
            return "Drawing started. If you haven't given choices yet, do so now. If you already gave choices, stop talking immediately."

        # Mark as generating IMMEDIATELY (synchronously) to prevent rapid-fire duplicates.
        if session_id:
            _session_generating.add(session_id)

        schedule_background_task(_run_visual_pipeline(args=args, session_id=session_id))
        return "Drawing started. If you haven't given choices yet, do so now. If you already gave choices, stop talking immediately."
    except Exception as exc:
        logger.error(f"generate_scene_visuals failed synchronously: {exc}", exc_info=True)
        return f"Error starting visual generation: {exc}"


@tenacity.retry(
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=5),
    stop=tenacity.stop_after_attempt(2),
    retry=tenacity.retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: logger.warning("Veo/Image gen retry #%s", retry_state.attempt_number),
)
async def _run_visual_pipeline(args: VisualArgs, session_id: str | None) -> None:
    """Internal async pipeline: fast still image first, optional Veo clip second."""
    _pipeline_t0 = time.monotonic()
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    image_bytes: bytes | None = None

    # Track timing and description for the session.
    if session_id:
        _session_last_gen_at[session_id] = time.monotonic()
        _session_last_gen_text[session_id] = args.description

    try:
        logger.info(f"⏱️ TIMING [pipeline] START | session={session_id} | waiting for semaphore")
        async with _still_semaphore:
            _sem_elapsed = int((time.monotonic() - _pipeline_t0) * 1000)
            logger.info(f"⏱️ TIMING [pipeline] SEMAPHORE ACQUIRED | elapsed={_sem_elapsed}ms | session={session_id}")
            # Attempt 1: short prompt, no negative prompt (fast & reliable).
            # Attempt 2: full prompt, no negative prompt.
            base_desc = args.base_description or args.description
            simple_desc = re.sub(r"\s+", " ", base_desc).strip()
            simple_desc = simple_desc[:220] if simple_desc else (base_desc[:220] or base_desc)
            prefixed_simple = f"A whimsical children's storybook illustration of: {simple_desc}"
            retry_plans = [
                (prefixed_simple, args.negative_prompt),
                (base_desc, args.negative_prompt),
            ]
            for attempt, (desc, neg) in enumerate(retry_plans, start=1):
                _attempt_t0 = time.monotonic()
                effective_size = args.image_size
                try:
                    image_bytes, _ = await asyncio.wait_for(
                        asyncio.to_thread(
                            _generate_scene_still,
                            desc,
                            neg,
                            args.aspect_ratio,
                            effective_size,
                            args.image_model,
                            args.reference_image_b64,
                            args.reference_image_mime,
                            args.illustration_style,
                        ),
                        timeout=45.0,
                    )
                    if image_bytes:
                        _attempt_elapsed = int((time.monotonic() - _attempt_t0) * 1000)
                        _total_elapsed = int((time.monotonic() - _pipeline_t0) * 1000)
                        logger.info(f"⏱️ TIMING [pipeline] IMAGE GEN attempt {attempt} SUCCESS | attempt_ms={_attempt_elapsed} | total_ms={_total_elapsed} | bytes={len(image_bytes)} | session={session_id}")
                        args.image_size = effective_size
                        break  # success
                except Exception as exc:
                    if attempt < len(retry_plans):
                        wait = 0.4 if attempt == 1 else 0.6
                        logger.warning(
                            "Image generation attempt %d/%d failed (size=%s): %s — "
                            "waiting %.1fs before retry.",
                            attempt, len(retry_plans), effective_size, exc, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise  # propagate after final attempt
        _gen_total = int((time.monotonic() - _pipeline_t0) * 1000)
        logger.info(f"⏱️ TIMING [pipeline] IMAGE GEN COMPLETE | total_ms={_gen_total} | raw_bytes={len(image_bytes)} | session={session_id}")
        
        # Compress image to JPEG to drastically reduce network transfer times (1.6MB -> 100KB)
        _compress_t0 = time.monotonic()
        try:
            from PIL import Image
            with Image.open(BytesIO(image_bytes)) as img:
                img = img.convert("RGB")
                out = BytesIO()
                img.save(out, format="JPEG", quality=80, optimize=True, progressive=True)
                image_bytes = out.getvalue()
                _compress_ms = int((time.monotonic() - _compress_t0) * 1000)
                _total_ms = int((time.monotonic() - _pipeline_t0) * 1000)
                logger.info(f"⏱️ TIMING [pipeline] JPEG COMPRESS | compress_ms={_compress_ms} | total_ms={_total_ms} | compressed_bytes={len(image_bytes)} | session={session_id}")
        except Exception as e:
            logger.warning("Failed to compress image bytes to JPEG, proceeding with raw bytes: %s", e)

        thumbnail_b64: str | None = None
        thumbnail_mime: str | None = None
        thumb = _make_thumbnail_b64(image_bytes)
        if thumb:
            thumbnail_b64, thumbnail_mime = thumb

        # 1. Immediate Base64 Delivery: Send to client ASAP to bypass GCS latency.
        still_url = None
        try:
            still_b64 = base64.b64encode(image_bytes).decode("ascii")
            still_url = f"data:image/jpeg;base64,{still_b64}"
            
            if session_id:
                if session_id in _session_cancel_current:
                    logger.info("Skipping Base64 publish for session %s — newer scene queued.", session_id)
                else:
                    publish_session_event(
                        session_id,
                        {
                            "type": "video_ready",
                            "payload": {
                                "url": still_url,
                                "media_type": "image",
                                "description": args.description,
                                "is_placeholder": False,
                                "thumbnail_b64": thumbnail_b64,
                                "thumbnail_mime": thumbnail_mime,
                            },
                        },
                    )
                    _gen_publish_total = int((time.monotonic() - _pipeline_t0) * 1000)
                    logger.info("⏱️ TIMING [pipeline] PUBLISHED (Base64) | total_ms=%d | session=%s", _gen_publish_total, session_id)
        except Exception as b64_exc:
            logger.warning("Base64 immediate publish failed: %s", b64_exc)

        # 2. Background GCS Upload: Save to cloud for persistence/theater-mode, but don't block the user.
        gcs_uri: str | None = None
        _upload_t0 = time.monotonic()
        try:
            # We still want the signed URL for the session state even if we already sent Base64 to the UI.
            cloud_still_url, gcs_uri = await asyncio.wait_for(
                asyncio.to_thread(_upload_scene_still, image_bytes, project, session_id),
                timeout=12.0,
            )
            # If we got a real Cloud URL, we use it for internal state (preferred for long-term).
            if cloud_still_url:
                still_url = cloud_still_url
            _upload_ms = int((time.monotonic() - _upload_t0) * 1000)
            logger.info("⏱️ TIMING [pipeline] GCS UPLOAD COMPLETE | upload_ms=%d | session=%s", _upload_ms, session_id)
        except Exception as exc:
            _upload_ms = int((time.monotonic() - _upload_t0) * 1000)
            logger.warning("⏱️ TIMING [pipeline] GCS UPLOAD FAILED after %dms: %s", _upload_ms, exc)

        if not still_url:
            # Tertiary fallback if everything else failed (local cache)
            backend_base = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
            try:
                media_id = store_media(image_bytes, "image/jpeg")
                still_url = f"{backend_base}/api/scene/{media_id}"
                logger.debug("Falling back to local media cache for session state: %s", still_url)
            except Exception:
                pass

        if session_id:
            if session_id in _session_cancel_current:
                logger.info(
                    "Skipping publish for session %s — newer scene queued during generation.",
                    session_id,
                )
            else:
                _publish_ms = int((time.monotonic() - _pipeline_t0) * 1000)
                logger.debug("Background stage complete: total_ms=%d | session=%s", _publish_ms, session_id)

    except Exception as exc:
        logger.warning("Still image generation failed gracefully: %s", exc, exc_info=True)
        schedule_background_task(
            asyncio.to_thread(
                record_prompt_feedback,
                "interactive_scene_visual",
                outcome="fallback",
                issues=[str(exc)],
                prompt_text=args.description,
                session_id=session_id or "",
                metadata={
                    "image_model": args.image_model,
                    "image_size": args.image_size,
                    "aspect_ratio": args.aspect_ratio,
                    "has_reference_image": bool(args.reference_image_b64),
                },
            )
        )
        if session_id and session_id not in _session_cancel_current:
            payload = {
                "url": _build_fallback_scene_svg_data_url(args.description),
                "description": args.description,
                "media_type": "image",
                "is_placeholder": False,
                "is_fallback": True,
            }
            if gcs_uri:
                payload["gcs_uri"] = gcs_uri
            publish_session_event(
                session_id,
                {
                    "type": "video_ready",
                    "payload": payload,
                },
            )
    else:
        schedule_background_task(
            asyncio.to_thread(
                record_prompt_feedback,
                "interactive_scene_visual",
                outcome="success",
                issues=[],
                prompt_text=args.description,
                session_id=session_id or "",
                metadata={
                    "image_model": args.image_model,
                    "image_size": args.image_size,
                    "aspect_ratio": args.aspect_ratio,
                    "has_reference_image": bool(args.reference_image_b64),
                },
            )
        )
        # Success path: optionally kick off Veo clip in the background.
        if _veo_enabled() and image_bytes:
            asyncio.create_task(
                _run_veo_clip(
                    project=project,
                    location=location,
                    image_bytes=image_bytes,
                    args=args,
                    session_id=session_id,
                )
            )
        else:
            logger.info("Skipping Veo clip generation because Veo is disabled or image_bytes is None.")
    finally:
        # Always release the in-flight lock so future calls for this session can proceed.
        if session_id:
            _session_generating.discard(session_id)
            _session_cancel_current.discard(session_id)
            pending = _session_pending.pop(session_id, None)
            if pending is not None:
                logger.info(
                    "Launching queued scene generation for session %s after prior completion.",
                    session_id,
                )
                schedule_background_task(_run_visual_pipeline(args=pending, session_id=session_id))


async def _run_veo_clip(
    project: str,
    location: str,
    image_bytes: bytes,
    args: VisualArgs,
    session_id: str | None,
) -> None:
    """Runs slower Veo clip generation without blocking still-image cadence."""
    async with _veo_semaphore:
        try:
            operation_name = await asyncio.wait_for(
                asyncio.to_thread(
                    _call_veo_api,
                    project=project,
                    location=location,
                    image_bytes=image_bytes,
                    description=args.description,
                    negative_prompt=args.negative_prompt,
                ),
                timeout=45.0,
            )
            raw_url = await asyncio.wait_for(
                asyncio.to_thread(_poll_veo_operation, project, location, operation_name),
                timeout=140.0,
            )
            ready_url = _normalize_media_url(raw_url, project=project)
            if ready_url and session_id:
                publish_session_event(
                    session_id,
                    {
                        "type": "video_ready",
                        "payload": {
                            "url": ready_url,
                            "description": args.description,
                            "media_type": "video",
                        },
                    },
                )
            elif not ready_url:
                logger.warning("Veo output URL could not be normalized to a client-accessible URL.")
        except Exception as exc:
            logger.warning("Optional Veo clip generation failed gracefully: %s", exc)


def _call_veo_api(
    project: str,
    location: str,
    image_bytes: bytes,
    description: str,
    negative_prompt: str,
) -> str:
    """Starts a Veo long-running operation and returns the operation name."""
    import google.auth
    import google.auth.transport.requests

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


async def generate_background_music(
    mood: str,
    intensity: int = 5,
    tool_context: ToolContext | None = None,
) -> str:
    """Triggers background music command for the frontend."""
    args = MusicArgs(mood=mood, intensity=intensity)
    session_id = _session_id_from_context(tool_context)
    if session_id:
        publish_session_event(
            session_id,
            {
                "type": "music_command",
                "payload": {"mood": args.mood, "intensity": args.intensity},
            },
        )
    logger.info("Music command: mood=%s intensity=%s", args.mood, args.intensity)
    return f"System: Background music changing to '{args.mood}' (intensity {args.intensity})."


async def save_character_fact(
    character_name: str,
    fact: str,
    tool_context: ToolContext,
) -> str:
    """Persists canonical character facts in session state."""
    args = CharacterFactArgs(character_name=character_name.strip(), fact=fact.strip())
    if not args.character_name or not args.fact:
        return "System: Character fact was empty."

    facts_list = list(tool_context.state.get("character_facts_list", []))
    facts_list.append({"character_name": args.character_name, "fact": args.fact})
    facts_list = facts_list[-40:]
    tool_context.state["character_facts_list"] = facts_list
    tool_context.state["character_facts"] = "\n".join(
        f"- {entry['character_name']}: {entry['fact']}" for entry in facts_list
    )

    logger.info("Saved character fact: %s -> %s", args.character_name, args.fact)
    return f"SAVE_FACT:{args.character_name}|{args.fact}"


async def save_child_name(name: str, tool_context: ToolContext) -> str:
    """Saves the child's preferred name in session state."""
    args = ChildNameArgs(name=name.strip())
    if not args.name:
        return "System: No name provided; ask the child for their name again."
    pending = str(tool_context.state.get("pending_child_name", "")).strip()
    name_confirmed = bool(tool_context.state.get("name_confirmed", False))
    if not name_confirmed:
        if pending and pending.lower() != args.name.lower():
            tool_context.state["pending_child_name"] = args.name
        return "System: Name not confirmed yet; ask the child to confirm their name."
    tool_context.state["child_name"] = args.name
    tool_context.state["pending_child_name"] = ""
    tool_context.state["name_confirmed"] = True
    tool_context.state["name_confirmation_prompted"] = False
    if tool_context.state.get("camera_stage", "none") == "none":
        tool_context.state["camera_stage"] = "pending"
    logger.info("Saving child name: %s", args.name)
    return f"System: Name saved. If you already asked a question, stop talking immediately."


async def assemble_story_video(
    session_id: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """Triggers the FFmpeg Cloud Run Job once per session."""
    resolved_session_id = session_id.strip() or (_session_id_from_context(tool_context) or "")
    if not resolved_session_id:
        return "System: Missing session id for movie assembly."

    args = AssembleArgs(session_id=resolved_session_id)

    if args.session_id in _assembled_sessions:
        logger.warning("assemble_story_video duplicate ignored for %s", args.session_id)
        return "System: Movie assembly already in progress."

    _assembled_sessions.add(args.session_id)

    use_local_storybook = _env_enabled("LOCAL_STORYBOOK_MODE", default=False)
    if not use_local_storybook and not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        logger.warning("assemble_story_video: GOOGLE_CLOUD_PROJECT not set; cannot trigger Cloud Run job.")
        return "System: Movie assembly is unavailable. Missing GOOGLE_CLOUD_PROJECT."

    if use_local_storybook:
        schedule_background_task(_run_local_storybook(args.session_id, tool_context))
        return f"ASSEMBLE_JOB_STARTED|LOCAL_STORYBOOK|SESSION:{args.session_id}"

    publish_session_event(
        args.session_id,
        {
            "type": "music_command",
            "payload": {"mood": "triumphant_celebration", "intensity": 8},
        },
    )

    asyncio.create_task(_trigger_ffmpeg_job(args.session_id))
    # Also generate the trading card in the background so it's ready for theater mode.
    schedule_background_task(_generate_trading_card_background(args.session_id, tool_context))
    return f"TRIGGER_LYRIA_CELEBRATION|ASSEMBLE_JOB_STARTED|SESSION:{args.session_id}"


def _ffmpeg_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


async def _generate_trading_card_background(
    session_id: str,
    tool_context: ToolContext | None,
) -> None:
    """Generates a hero trading card image and stores the URL in the session event bus."""
    if session_id in _card_generating:
        logger.info("[_generate_trading_card_background] Card already generating for %s; skipping.", session_id)
        return
    _card_generating.add(session_id)
    try:
        state: dict[str, Any] = {}
        if tool_context:
            raw = getattr(tool_context, "state", None)
            if raw is not None:
                try:
                    state = dict(raw.to_dict() if hasattr(raw, "to_dict") else raw)
                except Exception:
                    state = {}

        child_name = str(state.get("child_name", "our hero")).strip() or "our hero"
        sidekick = str(state.get("sidekick_description", "")).strip()
        story_summary = str(state.get("story_summary", "")).strip()
        facts_raw = list(state.get("character_facts_list", []) or [])
        key_facts = "; ".join(
            f"{e.get('character_name')}: {e.get('fact')}" for e in facts_raw[:3]
        ) if facts_raw else ""

        sidekick_clause = f" alongside their magical sidekick: {sidekick}" if sidekick else ""
        story_clause = f" Story: {story_summary[:120]}" if story_summary else ""
        facts_clause = f" Characters: {key_facts}" if key_facts else ""

        card_prompt = (
            f"A vibrant children's storybook trading card with a thick colorful border. "
            f"The hero is a brave, friendly child named {child_name}{sidekick_clause}. "
            f"{story_clause}{facts_clause} "
            f"Portrait orientation, 3:4 aspect ratio. Bright Pixar/Disney style illustration, "
            f"soft warm colors, magical sparkles, G-rated. "
            f"Bottom banner reads '{child_name}' in bold friendly font. No scary elements."
        )

        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")

        # Attempt to generate via Imagen
        image_bytes: bytes | None = None
        try:
            image_bytes, _ = await asyncio.wait_for(
                asyncio.to_thread(
                    _generate_scene_still,
                    card_prompt,
                    "aesthetic: U-rated, safe for children, no text, no brand logos",
                    "3:4",
                    "512px",
                    os.environ.get("IMAGE_MODEL", "gemini-3.1-flash-image-preview"),
                    None,
                    None,
                ),
                timeout=50.0,
            )
        except Exception as exc:
            logger.warning("Trading card image generation failed: %s", exc)
            return

        if not image_bytes:
            logger.warning("Trading card generation returned no bytes.")
            return

        card_url: str | None = None
        # Upload to GCS if project is configured
        if project:
            try:
                bucket_name = os.environ.get("GCS_ASSETS_BUCKET", "")
                if bucket_name:
                    from google.cloud import storage as gcs_storage
                    gcs_client = gcs_storage.Client(project=project)
                    bucket = gcs_client.bucket(bucket_name)
                    blob = bucket.blob(f"{session_id}/trading_card.png")
                    blob.content_type = "image/png"
                    blob.upload_from_string(image_bytes, content_type="image/png")
                    try:
                        from datetime import timedelta
                        sa_email = getattr(gcs_client, "service_account_email", None)
                        if not sa_email:
                            sa_email = f"{project}@appspot.gserviceaccount.com"

                        card_url = blob.generate_signed_url(
                            version="v4",
                            expiration=timedelta(hours=4),
                            method="GET",
                            service_account_email=sa_email
                        )
                    except Exception:
                        card_url = f"https://storage.googleapis.com/{bucket_name}/{session_id}/trading_card.png"
                    logger.info("Trading card uploaded to GCS: %s", card_url)
            except Exception as exc:
                logger.warning("Trading card GCS upload failed: %s", exc)

        # Fall back to backend media cache
        if not card_url:
            try:
                backend_base = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
                media_id = store_media(image_bytes, "image/png")
                card_url = f"{backend_base}/api/scene/{media_id}"
                logger.info("Trading card served from backend media cache: %s", card_url)
            except Exception as exc:
                logger.warning("Trading card media cache fallback failed: %s", exc)
                return

        if card_url and session_id:
            publish_session_event(
                session_id,
                {
                    "type": "trading_card_ready",
                    "payload": {"trading_card_url": card_url},
                },
            )
            logger.info("Trading card ready event published for session %s", session_id)
    except Exception as exc:
        logger.warning("_generate_trading_card_background failed: %s", exc, exc_info=True)
    finally:
        _card_generating.discard(session_id)


async def generate_trading_card(
    tool_context: ToolContext | None = None,
) -> str:
    """Generates a personalised hero trading card for the child at the end of the story."""
    session_id = _session_id_from_context(tool_context) or ""
    logger.info("[TOOL START] generate_trading_card called for session %s", session_id)
    if session_id:
        schedule_background_task(_generate_trading_card_background(session_id, tool_context))
        return f"TRADING_CARD_GENERATING|SESSION:{session_id}"
    return "System: Could not generate trading card — missing session id."


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _clean_story_text(text: str) -> str:
    if not text:
        return ""
    cleaned = _CTRL_TOKEN_RE.sub("", text)
    cleaned = re.sub(r"🌟\s*What should we do\?.*?(\n|$)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
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
    return first


_TEMPORAL_START_RE = re.compile(r"^(then|next|while|as)\\b", flags=re.IGNORECASE)
_SPATIAL_START_RE = re.compile(r"^(here|there|in|on|at)\\b", flags=re.IGNORECASE)


def _decapitalize(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:] if text[0].isupper() else text


def _apply_readalong_connectors(line: str, idx: int) -> str:
    """Add spatial anchors or temporal connectors for cohesive narration."""
    if not line:
        return line
    stripped = line.lstrip()
    if idx <= 0:
        if _SPATIAL_START_RE.match(stripped) or _TEMPORAL_START_RE.match(stripped):
            return line
        return f"Here, {_decapitalize(stripped)}"
    if _TEMPORAL_START_RE.match(stripped):
        return line
    core = stripped
    if _SPATIAL_START_RE.match(stripped):
        core = re.sub(r"^(?i)(here|there|in|on|at)\\b[:,]?\\s*", "", stripped)
        if not core:
            core = stripped
    return f"Then, {_decapitalize(core)}"


def _build_narration_segments(
    scene_descriptions: list[str],
    story_summary: str,
    scene_count: int,
) -> list[str]:
    narration: list[str] = []
    if scene_descriptions:
        for idx, desc in enumerate(scene_descriptions[:scene_count]):
            line = _simplify_for_readalong(desc)
            line = _apply_readalong_connectors(line, idx)
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
                line = _apply_readalong_connectors(line, i)
                narration.append(line)
    if len(narration) < scene_count:
        if narration:
            narration.extend([narration[-1]] * (scene_count - len(narration)))
        else:
            narration = [""] * scene_count
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
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
    if not api_key or not voice_id or not text:
        if api_key and not voice_id:
            logger.warning(
                "ELEVENLABS_VOICE_ID is not set — ElevenLabs TTS disabled. "
                "Set ELEVENLABS_VOICE_ID in .env (find IDs at elevenlabs.io/voice-library)."
            )
        return None
    endpoint = os.environ.get("ELEVENLABS_TTS_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
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
    except Exception:
        return None
    return None


def _synthesize_tts_local(text: str) -> bytes | None:
    """Best-effort local TTS fallback for dev (macOS say / Linux espeak)."""
    if not text:
        return None
    if not (_env_enabled("LOCAL_STORYBOOK_MODE", default=False) or _env_enabled("STORYBOOK_ALLOW_LOCAL_TTS", default=False)):
        return None

    def _to_mp3(source_path: Path) -> bytes | None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            try:
                return source_path.read_bytes()
            except Exception:
                return None
        mp3_path = source_path.with_suffix(".mp3")
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(source_path),
                    "-codec:a",
                    "libmp3lame",
                    "-q:a",
                    "4",
                    str(mp3_path),
                ],
                check=True,
                capture_output=True,
            )
            return mp3_path.read_bytes()
        except Exception:
            return None

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        if sys.platform == "darwin":
            say = shutil.which("say")
            if not say:
                return None
            aiff_path = tmp_path / "say.aiff"
            try:
                subprocess.run(
                    [
                        say,
                        "-o",
                        str(aiff_path),
                        "--data-format=LEI16@22050",
                        text,
                    ],
                    check=True,
                    capture_output=True,
                )
            except Exception:
                return None
            return _to_mp3(aiff_path)

        if sys.platform.startswith("linux"):
            espeak = shutil.which("espeak") or shutil.which("espeak-ng")
            if not espeak:
                return None
            wav_path = tmp_path / "espeak.wav"
            try:
                subprocess.run(
                    [espeak, "-w", str(wav_path), text],
                    check=True,
                    capture_output=True,
                )
            except Exception:
                return None
            return _to_mp3(wav_path)

    return None


def _synthesize_tts_google(text: str) -> bytes | None:
    if not text:
        return None
    # Prefer ElevenLabs when available; fall back to Google TTS and local TTS.
    audio = _synthesize_tts_elevenlabs(text)
    if audio:
        return audio
    try:
        from google.cloud import texttospeech
    except Exception:
        return _synthesize_tts_local(text)
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
        return _synthesize_tts_local(text)


async def _elevenlabs_generate_music(prompt: str, duration_seconds: float) -> bytes | None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(2.0, min(float(duration_seconds), 30.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    # Prefer the music endpoint for longer durations; otherwise fall back to sound generation.
    use_music_api = duration_seconds >= 10.0
    if use_music_api:
        music_endpoint = os.environ.get("ELEVENLABS_MUSIC_ENDPOINT", "").strip()
        if not music_endpoint:
            music_endpoint = "https://api.elevenlabs.io/v1/music"
        payload = {
            "prompt": prompt,
            "music_length_ms": int(duration_seconds * 1000),
        }
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(music_endpoint, headers=headers, json=payload)
                if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                    return resp.content
        except Exception:
            pass

    sound_endpoint = os.environ.get("ELEVENLABS_SOUND_ENDPOINT", "").strip()
    if not sound_endpoint:
        sound_endpoint = "https://api.elevenlabs.io/v1/sound-generation"
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
    except Exception:
        return None
    return None


async def _elevenlabs_generate_sfx(prompt: str, duration_seconds: float) -> bytes | None:
    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key or not prompt:
        return None
    duration_seconds = max(1.0, min(float(duration_seconds), 6.0))
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    sound_endpoint = os.environ.get("ELEVENLABS_SOUND_ENDPOINT", "").strip()
    if not sound_endpoint:
        sound_endpoint = "https://api.elevenlabs.io/v1/sound-generation"
    payload = {
        "text": prompt,
        "duration_seconds": duration_seconds,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(sound_endpoint, headers=headers, json=payload)
            if resp.status_code < 300 and resp.headers.get("content-type", "").startswith("audio"):
                return resp.content
    except Exception:
        return None
    return None


async def _fetch_media_bytes(url: str) -> bytes:
    if url.startswith("data:"):
        header, data = url.split(",", 1)
        if ";base64" in header:
            return base64.b64decode(data)
        return data.encode("utf-8")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def _run_local_storybook(session_id: str, tool_context: ToolContext | None) -> None:
    """Creates a lightweight storybook video from scene images and emits theater_mode."""
    try:
        def _load_state() -> dict[str, Any]:
            raw = getattr(tool_context, "state", None) if tool_context else None
            if raw is None:
                return {}
            if hasattr(raw, "to_dict"):
                try:
                    return dict(raw.to_dict() or {})
                except Exception:
                    return {}
            if hasattr(raw, "_value"):
                try:
                    return dict(getattr(raw, "_value", {}) or {})
                except Exception:
                    return {}
            if isinstance(raw, dict):
                return dict(raw)
            try:
                return dict(raw)  # best-effort
            except Exception:
                return {}

        min_scenes = 2
        try:
            min_scenes = int(os.environ.get("STORYBOOK_MIN_SCENES", "2"))
        except Exception:
            min_scenes = 2
        min_scenes = max(1, min(min_scenes, 6))
        try:
            wait_seconds = float(os.environ.get("STORYBOOK_WAIT_FOR_SCENES_SECONDS", "12"))
        except Exception:
            wait_seconds = 12.0
        wait_seconds = max(0.0, min(wait_seconds, 30.0))

        state = _load_state()
        scene_urls = list(state.get("scene_asset_urls", []) or [])
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            in_flight = session_id in _session_generating or session_id in _session_pending
            if len(scene_urls) >= min_scenes and not in_flight:
                break
            await asyncio.sleep(0.4)
            state = _load_state()
            scene_urls = list(state.get("scene_asset_urls", []) or [])

        if len(scene_urls) < min_scenes:
            logger.warning(
                "Local storybook assembling with %d scene(s) (min=%d).",
                len(scene_urls),
                min_scenes,
            )
        if not scene_urls:
            logger.warning("Local storybook: no scene images for session %s", session_id)
            return

        child_name = str(state.get("child_name", "")).strip()
        story_summary = str(state.get("story_summary", "")).strip()
        scene_descriptions = list(state.get("scene_descriptions", []) or [])
        title = os.environ.get("STORYBOOK_TITLE", "Reading Rainbow Adventure").strip()
        try:
            page_seconds = int(os.environ.get("STORYBOOK_PAGE_SECONDS", "3"))
        except Exception:
            page_seconds = 3
        page_seconds = max(2, min(page_seconds, 6))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            frames: list[Path] = []
            for idx, url in enumerate(scene_urls):
                data = await _fetch_media_bytes(str(url))
                frame_path = tmp_path / f"frame_{idx:03d}.png"
                frame_path.write_bytes(data)
                frames.append(frame_path)

            narration_lines = _build_narration_segments(
                scene_descriptions=scene_descriptions,
                story_summary=story_summary,
                scene_count=len(frames),
            )
            enable_tts = _env_enabled("ENABLE_STORYBOOK_TTS", default=True)
            enable_captions = _env_enabled("ENABLE_STORYBOOK_CAPTIONS", default=True)
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

            narration_audio: list[Path | None] = []
            scene_durations: list[float] = []
            for idx, line in enumerate(narration_lines):
                audio_path: Path | None = None
                audio_seconds = 0.0
                if enable_tts and line:
                    audio_bytes = _synthesize_tts_google(line)
                    if audio_bytes:
                        audio_path = tmp_path / f"narration_{idx:03d}.mp3"
                        audio_path.write_bytes(audio_bytes)
                        audio_seconds = _ffprobe_duration(audio_path)
                narration_audio.append(audio_path)
                if audio_seconds > 0:
                    scene_durations.append(max(page_seconds, audio_seconds + 0.4))
                else:
                    scene_durations.append(float(page_seconds))

            music_paths: list[Path | None] = []
            if enable_music and os.environ.get("ELEVENLABS_API_KEY"):
                for idx, duration in enumerate(scene_durations):
                    desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                    prompt = _build_music_prompt(desc, idx, len(scene_durations))
                    audio_bytes = await _elevenlabs_generate_music(prompt, duration)
                    if audio_bytes:
                        music_path = tmp_path / f"music_{idx:03d}.mp3"
                        music_path.write_bytes(audio_bytes)
                        music_paths.append(music_path)
                    else:
                        music_paths.append(None)
            else:
                music_paths = [None] * len(scene_durations)

            sfx_paths: list[Path | None] = [None] * len(scene_durations)
            if enable_sfx and os.environ.get("ELEVENLABS_API_KEY") and scene_durations:
                sfx_indices = _choose_sfx_indices(
                    total=len(scene_durations),
                    max_count=sfx_max,
                    descriptions=scene_descriptions,
                    min_score=sfx_min_score,
                    cooldown=sfx_cooldown,
                )
                if sfx_indices:
                    logger.info("Storybook SFX scenes selected: %s", sfx_indices)
                for idx in sfx_indices:
                    desc = scene_descriptions[idx] if idx < len(scene_descriptions) else ""
                    prompt = _build_sfx_prompt(desc)
                    audio_bytes = await _elevenlabs_generate_sfx(prompt, 2.5)
                    if audio_bytes:
                        sfx_path = tmp_path / f"sfx_{idx:03d}.mp3"
                        sfx_path.write_bytes(audio_bytes)
                        sfx_paths[idx] = sfx_path

            concat_list = tmp_path / "concat.txt"
            lines = []
            for frame, duration in zip(frames, scene_durations):
                lines.append(f"file '{frame.name}'")
                lines.append(f"duration {duration:.3f}")
            lines.append(f"file '{frames[-1].name}'")
            concat_list.write_text("\n".join(lines))

            output_path = tmp_path / "storybook_video.mp4"

            vf_parts = [
                "scale=1280:720:force_original_aspect_ratio=decrease",
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=#0b2d5b",
            ]

            if title:
                vf_parts.extend([
                    "drawbox=x=0:y=0:w=1280:h=18:color=#ff595e@0.9:t=fill",
                    "drawbox=x=0:y=18:w=1280:h=18:color=#ffca3a@0.9:t=fill",
                    "drawbox=x=0:y=36:w=1280:h=18:color=#8ac926@0.9:t=fill",
                    "drawbox=x=0:y=54:w=1280:h=18:color=#1982c4@0.9:t=fill",
                    "drawbox=x=0:y=72:w=1280:h=18:color=#6a4c93@0.9:t=fill",
                    f"drawtext=text='{_ffmpeg_escape(title)}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=120:shadowcolor=black:shadowx=2:shadowy=2",
                ])
                if child_name:
                    vf_parts.append(
                        f"drawtext=text='Starring { _ffmpeg_escape(child_name)}':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=165:shadowcolor=black:shadowx=2:shadowy=2"
                    )

            vf = ",".join(vf_parts)

            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_list),
                    "-vf",
                    vf,
                    "-r",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ],
                check=True,
                capture_output=True,
            )

            def _build_padded_audio(paths: list[Path | None], durations: list[float], stem: str) -> Path | None:
                if not durations or not paths:
                    return None
                padded_paths: list[Path] = []
                for idx, duration in enumerate(durations):
                    src = paths[idx] if idx < len(paths) else None
                    padded_path = tmp_path / f"{stem}_pad_{idx:03d}.wav"
                    if src is None:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-f",
                                "lavfi",
                                "-i",
                                "anullsrc=channel_layout=stereo:sample_rate=44100",
                                "-t",
                                f"{duration:.3f}",
                                str(padded_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                    else:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(src),
                                "-filter_complex",
                                f"apad=pad_dur={duration:.3f},atrim=0:{duration:.3f}",
                                "-ar",
                                "44100",
                                "-ac",
                                "2",
                                str(padded_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                    padded_paths.append(padded_path)

                audio_concat_list = tmp_path / f"{stem}_concat.txt"
                audio_concat_list.write_text(
                    "\n".join(f"file '{p.name}'" for p in padded_paths)
                )
                audio_full = tmp_path / f"{stem}_full.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        str(audio_concat_list),
                        "-c:a",
                        "pcm_s16le",
                        str(audio_full),
                    ],
                    check=True,
                    capture_output=True,
                )
                return audio_full

            narration_full = (
                _build_padded_audio(narration_audio, scene_durations, "narration")
                if enable_tts and any(p for p in narration_audio)
                else None
            )
            music_full = (
                _build_padded_audio(music_paths, scene_durations, "music")
                if enable_music and any(p for p in music_paths)
                else None
            )
            sfx_full = (
                _build_padded_audio(sfx_paths, scene_durations, "sfx")
                if enable_sfx and any(p for p in sfx_paths)
                else None
            )

            audio_full_path: Path | None = None
            if narration_full and music_full and sfx_full:
                mixed_path = tmp_path / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(narration_full),
                        "-i",
                        str(music_full),
                        "-i",
                        str(sfx_full),
                        "-filter_complex",
                        f"[1:a]volume={music_volume}[music];[2:a]volume={sfx_volume}[sfx];[0:a][music][sfx]amix=inputs=3:duration=shortest:dropout_transition=0",
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif narration_full and music_full:
                mixed_path = tmp_path / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(narration_full),
                        "-i",
                        str(music_full),
                        "-filter_complex",
                        f"[1:a]volume={music_volume}[music];[0:a][music]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif narration_full and sfx_full:
                mixed_path = tmp_path / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(narration_full),
                        "-i",
                        str(sfx_full),
                        "-filter_complex",
                        f"[1:a]volume={sfx_volume}[sfx];[0:a][sfx]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif narration_full:
                audio_full_path = narration_full
            elif music_full and sfx_full:
                mixed_path = tmp_path / "audio_mix.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(music_full),
                        "-i",
                        str(sfx_full),
                        "-filter_complex",
                        f"[0:a]volume={music_volume}[music];[1:a]volume={sfx_volume}[sfx];[music][sfx]amix=inputs=2:duration=shortest:dropout_transition=0",
                        "-c:a",
                        "pcm_s16le",
                        str(mixed_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = mixed_path
            elif music_full:
                music_only = tmp_path / "music_only.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(music_full),
                        "-filter:a",
                        f"volume={music_volume}",
                        "-c:a",
                        "pcm_s16le",
                        str(music_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = music_only
            elif sfx_full:
                sfx_only = tmp_path / "sfx_only.wav"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(sfx_full),
                        "-filter:a",
                        f"volume={sfx_volume}",
                        "-c:a",
                        "pcm_s16le",
                        str(sfx_only),
                    ],
                    check=True,
                    capture_output=True,
                )
                audio_full_path = sfx_only

            video_with_audio = output_path
            audio_available = False
            if audio_full_path and audio_full_path.exists():
                audio_available = _ffprobe_duration(audio_full_path) > 0.1
                merged_path = tmp_path / "storybook_with_audio.mp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(output_path),
                        "-i",
                        str(audio_full_path),
                        "-c:v",
                        "copy",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "160k",
                        "-shortest",
                        "-movflags",
                        "+faststart",
                        str(merged_path),
                    ],
                    check=True,
                    capture_output=True,
                )
                video_with_audio = merged_path

            final_path = video_with_audio
            if enable_captions and any(narration_lines):
                # Build upper-frame book-style drawtext (per-page, time-gated).
                drawtext_parts: list[str] = []
                t = 0.0
                for duration, line in zip(scene_durations, narration_lines):
                    if not line:
                        t += duration
                        continue
                    wrapped = _wrap_caption(line, width=30)
                    escaped = _ffmpeg_escape(wrapped)
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
                        f":enable='between(t,{t:.3f},{t+duration:.3f})'"
                    )
                    drawtext_parts.append(draw)
                    t += duration
                if drawtext_parts:
                    captioned_path = tmp_path / "storybook_captioned.mp4"
                    try:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-i",
                                str(video_with_audio),
                                "-vf",
                                ",".join(drawtext_parts),
                                "-c:a",
                                "copy",
                                "-movflags",
                                "+faststart",
                                str(captioned_path),
                            ],
                            check=True,
                            capture_output=True,
                        )
                        final_path = captioned_path
                    except Exception:
                        final_path = video_with_audio

            video_bytes = final_path.read_bytes()
            backend_base = os.environ.get("BACKEND_PUBLIC_URL", "http://localhost:8000").rstrip("/")
            media_id = store_media(video_bytes, "video/mp4")
            video_url = f"{backend_base}/api/scene/{media_id}"
            trading_card_url = str(state.get("trading_card_url", "")).strip() or None
            publish_session_event(
                session_id,
                theater_mode_event(
                    mp4_url=video_url,
                    trading_card_url=trading_card_url,
                    narration_lines=[line for line in narration_lines if line],
                    audio_available=audio_available,
                ).model_dump(mode="json"),
            )
            logger.info("Local storybook assembled for session %s", session_id)
    except Exception as exc:
        logger.warning("Local storybook assembly failed: %s", exc, exc_info=True)


async def _trigger_ffmpeg_job(session_id: str) -> None:
    """Invokes the FFmpeg Cloud Run Job via Cloud Run Jobs API."""
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
    job_name = os.environ.get("FFMPEG_JOB_NAME", "storyteller-ffmpeg-assembler")

    import google.auth
    import google.auth.transport.requests

    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        auth_req = google.auth.transport.requests.Request()
        creds.refresh(auth_req)

        endpoint = (
            f"https://run.googleapis.com/v2/projects/{project}/locations/{location}"
            f"/jobs/{job_name}:run"
        )
        headers = {
            "Authorization": f"Bearer {creds.token}",
            "Content-Type": "application/json",
        }
        payload = {"overrides": {"containerOverrides": [{"args": [f"--session_id={session_id}"]}]}}

        async with httpx.AsyncClient() as client:
            resp = await client.post(endpoint, headers=headers, json=payload, timeout=30)
            if resp.status_code >= 400:
                logger.error(f"FFmpeg trigger failed (HTTP {resp.status_code}): {resp.text}")
                # Clear the lock so they can retry after fixing config/IAM
                _assembled_sessions.discard(session_id)
            resp.raise_for_status()
        logger.info("FFmpeg Cloud Run Job triggered for session %s", session_id)
    except Exception as exc:
        logger.warning("FFmpeg trigger failed gracefully for %s: %s", session_id, exc)
        _assembled_sessions.discard(session_id)
        publish_session_event(
            session_id,
            {
                "type": "error",
                "payload": {"message": "Oops, my movie wand is recharging. Let's keep telling the story!"},
            },
        )


async def sync_room_lights(
    hex_color: str,
    scene_description: str = "",
    tool_context: ToolContext | None = None,
) -> str:
    """Syncs smart-home lights with a 5-second hard cooldown."""
    global _last_light_call
    args = LightArgs(hex_color=hex_color.strip(), scene_description=scene_description.strip())
    session_id = _session_id_from_context(tool_context)

    now = time.monotonic()
    if now - _last_light_call < _LIGHT_COOLDOWN_SECONDS:
        remaining = _LIGHT_COOLDOWN_SECONDS - (now - _last_light_call)
        return f"System: Lighting cooldown active. Next change available in {remaining:.1f}s."
    _last_light_call = now

    cfg = get_session_iot_config(session_id) if session_id else {}
    ha_url = cfg.get("ha_url") or os.environ.get("HOME_ASSISTANT_URL", "")
    ha_token = cfg.get("ha_token") or os.environ.get("HOME_ASSISTANT_TOKEN", "")
    ha_entity = cfg.get("ha_entity") or os.environ.get("HA_LIGHT_ENTITY", "light.living_room")

    if not ha_url or not ha_token:
        return f"System: IoT lights not configured, skipping sync for color {args.hex_color}."

    hex_clean = args.hex_color.lstrip("#")
    if len(hex_clean) != 6:
        return "System: Invalid color format. Use #RRGGBB."

    r, g, b = int(hex_clean[0:2], 16), int(hex_clean[2:4], 16), int(hex_clean[4:6], 16)
    payload = {
        "entity_id": ha_entity,
        "rgb_color": [r, g, b],
        "brightness": 200,
        "transition": 2,
    }
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{ha_url.rstrip('/')}/api/services/light/turn_on",
                headers=headers,
                json=payload,
                timeout=3.0,
            )
            resp.raise_for_status()

        if session_id:
            publish_session_event(
                session_id,
                {
                    "type": "lighting_command",
                    "payload": {"hex_color": args.hex_color, "entity": ha_entity},
                },
            )

        return f"System: Room lights synced to {args.hex_color}."
    except Exception as exc:
        logger.warning("IoT light sync failed gracefully: %s", exc)
        return "System: IoT light sync failed gracefully."
