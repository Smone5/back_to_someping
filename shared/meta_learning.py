"""Lightweight ADAI-style prompt meta-learning for google-prog.

This keeps the base prompts stable and injects a small scoped bundle of
learned principles at runtime. Repeated failures can promote issue-specific
rules from feedback rollups into Firestore-backed candidate/active principles.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_SEED_PATH = Path(__file__).with_name("meta_learning_principles.json")
_CACHE: dict[str, tuple[float, Any]] = {}

_SCOPE_FRAMING: dict[str, str] = {
    "interactive_story": "When guiding the live story, apply these learned principles.",
    "interactive_scene_visual": "When writing still-image prompts for the live story, apply these learned principles.",
    "storyboard_review": "When reviewing end-movie stills, use these learned principles as quality criteria.",
    "storyboard_repair": "When repairing end-movie stills, apply these learned principles while preserving the intended story beat.",
}

_ISSUE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("continuity", ("continuity", "coherence", "carry-over", "carry over", "temporal", "adjacent", "drift", "abrupt")),
    ("character_consistency", ("character", "appearance", "hair", "face", "clothing", "costume", "sidekick", "hero design")),
    ("setting_consistency", ("setting", "background", "location", "environment", "place", "room", "forest", "kingdom")),
    ("props", ("prop", "object", "item", "boat", "ship", "wand", "hat", "backpack", "toy")),
    ("prompt_grounding", ("does not match", "mismatch", "wrong beat", "wrong scene", "off prompt", "scene description", "not matching")),
    ("no_text", ("text", "logo", "label", "watermark", "caption", "letters", "words", "sign")),
    ("safety_tone", ("scary", "frightening", "dark", "threat", "monster", "villain", "dangerous", "creepy")),
    ("composition_clarity", ("busy", "cluttered", "confusing", "unclear", "focal", "readability", "crowded")),
    ("style_consistency", ("style", "3d", "pixar", "render", "photoreal", "art style")),
    ("model_reliability", ("blocked", "refusal", "failed", "timeout", "no image", "fallback", "error")),
)

_PROMOTED_RULES: dict[str, dict[str, str]] = {
    "interactive_story": {
        "continuity": "Carry one named object, place, goal, or companion from the prior beat into the next beat so the world never feels reset.",
        "character_consistency": "Keep the hero and sidekick identity stable across turns; do not rename, redesign, or swap traits unless the child explicitly changes them.",
        "prompt_grounding": "Make each story beat clearly pay off the child's most recent choice instead of drifting to a different scene idea.",
        "safety_tone": "Keep even adventurous moments soft, cozy, and reassuring for ages 4-5.",
    },
    "interactive_scene_visual": {
        "continuity": "Keep the same hero, sidekick, anchor prop, and location progression from the prior approved scene unless the story explicitly changes them.",
        "character_consistency": "Preserve consistent character silhouette, clothing colors, and sidekick design from scene to scene.",
        "prompt_grounding": "Show the exact story beat requested, with one clear focal action and no unrelated set pieces.",
        "no_text": "Do not generate any words, labels, captions, signs, or watermarks inside the illustration.",
        "style_consistency": "Stay in a warm hand-drawn 2D storybook style; avoid photoreal, glossy 3D, or Pixar-like rendering.",
    },
    "storyboard_review": {
        "continuity": "Flag only meaningful continuity breaks across adjacent stills, especially character identity, props, and location progression.",
        "prompt_grounding": "Check whether each still matches its intended scene description, not just whether it looks nice in isolation.",
        "no_text": "Treat visible text, labels, logos, and watermarks as real defects.",
        "safety_tone": "Treat scary or age-inappropriate imagery as a defect even if the composition is otherwise strong.",
    },
    "storyboard_repair": {
        "continuity": "Repair the flagged continuity break while keeping the original scene purpose and adjacent-scene flow intact.",
        "character_consistency": "Preserve the approved character look from neighboring scenes while fixing only the broken details.",
        "prompt_grounding": "Repair toward the intended scene description, not toward a newly invented moment.",
        "no_text": "Repair away any visible text, logos, labels, or watermarks.",
        "style_consistency": "Keep the repaired still in the same gentle hand-drawn storybook style as the rest of the movie.",
    },
}


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_scope(scope: str) -> str:
    return str(scope or "").strip().lower() or "interactive_story"


def _cache_ttl_seconds() -> int:
    raw = os.environ.get("PROMPT_META_LEARNING_CACHE_SECONDS", "120")
    try:
        return max(10, min(int(raw), 900))
    except Exception:
        return 120


def _cache_get(key: str) -> Any | None:
    entry = _CACHE.get(key)
    if not entry:
        return None
    expires_at, value = entry
    if time.time() >= expires_at:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any, ttl_seconds: int | None = None) -> Any:
    ttl = ttl_seconds if ttl_seconds is not None else _cache_ttl_seconds()
    _CACHE[key] = (time.time() + ttl, value)
    return value


@functools.lru_cache(maxsize=1)
def _load_seed_principles() -> list[dict[str, Any]]:
    try:
        payload = json.loads(_SEED_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Meta-learning seed load failed: %s", exc)
        return []
    principles = payload.get("principles", [])
    if not isinstance(principles, list):
        return []
    clean: list[dict[str, Any]] = []
    for row in principles:
        if not isinstance(row, dict):
            continue
        scope = _normalize_scope(str(row.get("scope", "all")))
        rule_text = str(row.get("rule_text", "")).strip()
        if not rule_text:
            continue
        clean.append(
            {
                "id": str(row.get("id", f"seed-{len(clean) + 1}")).strip(),
                "scope": scope,
                "category": str(row.get("category", "general")).strip() or "general",
                "status": str(row.get("status", "active")).strip().lower() or "active",
                "priority": int(row.get("priority", 50) or 50),
                "rule_text": rule_text,
                "source": str(row.get("source", "seed")).strip() or "seed",
            }
        )
    return clean


@functools.lru_cache(maxsize=1)
def _firestore_client() -> Any | None:
    if not _env_enabled("ENABLE_PROMPT_META_LEARNING", default=True):
        return None
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        return None
    try:
        from google.cloud import firestore
    except Exception as exc:
        logger.debug("Firestore unavailable for meta-learning: %s", exc)
        return None
    database = os.environ.get("FIRESTORE_DATABASE", "(default)").strip() or "(default)"
    try:
        return firestore.Client(project=project, database=database)
    except TypeError:
        return firestore.Client(project=project)
    except Exception as exc:
        logger.warning("Meta-learning Firestore client init failed: %s", exc)
        return None


def _principles_collection_name() -> str:
    return os.environ.get("PROMPT_META_LEARNING_PRINCIPLES_COLLECTION", "prompt_learning_principles").strip() or "prompt_learning_principles"


def _feedback_collection_name() -> str:
    return os.environ.get("PROMPT_META_LEARNING_FEEDBACK_COLLECTION", "prompt_learning_feedback").strip() or "prompt_learning_feedback"


def _rollups_collection_name() -> str:
    return os.environ.get("PROMPT_META_LEARNING_ROLLUPS_COLLECTION", "prompt_learning_issue_rollups").strip() or "prompt_learning_issue_rollups"


def _feedback_log_path() -> Path:
    return Path(os.environ.get("PROMPT_META_LEARNING_LOCAL_LOG", "/tmp/prompt_learning_feedback.jsonl"))


def _load_firestore_principles(scope: str) -> list[dict[str, Any]]:
    cache_key = f"firestore_principles:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = _firestore_client()
    if client is None:
        return _cache_set(cache_key, [])

    docs: list[dict[str, Any]] = []
    try:
        stream = client.collection(_principles_collection_name()).limit(100).stream()
        for snap in stream:
            row = snap.to_dict() or {}
            row["id"] = row.get("id") or snap.id
            docs.append(row)
    except Exception as exc:
        logger.warning("Meta-learning principles fetch failed: %s", exc)
        return _cache_set(cache_key, [])
    return _cache_set(cache_key, docs)


def _dedupe_principles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        rule_text = str(row.get("rule_text", "")).strip()
        if not rule_text:
            continue
        key = (str(row.get("category", "general")).strip() or "general", rule_text)
        existing = deduped.get(key)
        candidate = {
            "id": str(row.get("id", "")).strip(),
            "scope": _normalize_scope(str(row.get("scope", "all"))),
            "category": str(row.get("category", "general")).strip() or "general",
            "status": str(row.get("status", "active")).strip().lower() or "active",
            "priority": int(row.get("priority", 50) or 50),
            "rule_text": rule_text,
            "source": str(row.get("source", "seed")).strip() or "seed",
        }
        if existing is None:
            deduped[key] = candidate
            continue
        existing_rank = (0 if existing["status"] == "active" else 1, -existing["priority"])
        candidate_rank = (0 if candidate["status"] == "active" else 1, -candidate["priority"])
        if candidate_rank < existing_rank:
            deduped[key] = candidate
    ordered = list(deduped.values())
    ordered.sort(key=lambda item: (0 if item["status"] == "active" else 1, -item["priority"], item["category"]))
    return ordered


def get_scoped_principles(scope: str) -> list[dict[str, Any]]:
    normalized_scope = _normalize_scope(scope)
    max_principles_raw = os.environ.get("PROMPT_META_LEARNING_MAX_PRINCIPLES", "5")
    try:
        max_principles = max(1, min(int(max_principles_raw), 8))
    except Exception:
        max_principles = 5

    rows: list[dict[str, Any]] = []
    for row in _load_seed_principles():
        row_scope = _normalize_scope(str(row.get("scope", "all")))
        if row_scope in {"all", normalized_scope}:
            rows.append(row)

    for row in _load_firestore_principles(normalized_scope):
        row_scope = _normalize_scope(str(row.get("scope", "all")))
        status = str(row.get("status", "active")).strip().lower() or "active"
        if row_scope not in {"all", normalized_scope}:
            continue
        if status not in {"active", "candidate"}:
            continue
        rows.append(row)

    deduped = _dedupe_principles(rows)
    if not _env_enabled("PROMPT_META_LEARNING_INCLUDE_CANDIDATES", default=True):
        deduped = [row for row in deduped if row.get("status") == "active"]
    return deduped[:max_principles]


def build_principles_injection_text(scope: str) -> str:
    normalized_scope = _normalize_scope(scope)
    principles = get_scoped_principles(normalized_scope)
    if not principles:
        return ""

    scope_instruction = _SCOPE_FRAMING.get(
        normalized_scope,
        "Apply these learned principles to the current task.",
    )
    lines = [scope_instruction, "", f"<learned_story_principles scope=\"{normalized_scope}\" count=\"{len(principles)}\">"]
    for row in principles:
        status = str(row.get("status", "active")).strip().lower() or "active"
        status_tag = f" [{status}]" if status != "active" else ""
        category = str(row.get("category", "general")).strip() or "general"
        rule_text = str(row.get("rule_text", "")).strip()
        lines.append(f"- [{category}]{status_tag} {rule_text}")
    lines.append("</learned_story_principles>")
    return "\n".join(lines)


def _normalize_issue_tags(issues: list[str] | None) -> list[str]:
    if not issues:
        return []
    tags: list[str] = []
    for issue in issues:
        text = str(issue or "").strip().lower()
        if not text:
            continue
        direct = re.sub(r"[^a-z0-9_]+", "_", text).strip("_")
        if direct in {
            "continuity",
            "character_consistency",
            "setting_consistency",
            "props",
            "prompt_grounding",
            "no_text",
            "safety_tone",
            "composition_clarity",
            "style_consistency",
            "model_reliability",
        }:
            tags.append(direct)
            continue
        matched = False
        for tag, patterns in _ISSUE_PATTERNS:
            if any(pattern in text for pattern in patterns):
                tags.append(tag)
                matched = True
                break
        if not matched and text:
            tags.append("prompt_grounding")
    return sorted(set(tags))


def _promotion_rule_for(scope: str, issue_tag: str) -> str:
    normalized_scope = _normalize_scope(scope)
    scoped = _PROMOTED_RULES.get(normalized_scope, {})
    if issue_tag in scoped:
        return scoped[issue_tag]
    shared = _PROMOTED_RULES.get("interactive_scene_visual", {})
    if issue_tag in shared:
        return shared[issue_tag]
    return "Stay tightly grounded to the intended story beat and preserve continuity with adjacent scenes."


def _append_local_feedback(record: Mapping[str, Any]) -> None:
    try:
        path = _feedback_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
    except Exception as exc:
        logger.debug("Local prompt feedback write failed: %s", exc)


def _upsert_rollup_and_principle(client: Any, scope: str, issue_tag: str, sample_issue: str) -> None:
    try:
        from google.cloud import firestore
    except Exception:
        return

    normalized_scope = _normalize_scope(scope)
    rollup_id = f"{normalized_scope}--{issue_tag}"
    rollup_ref = client.collection(_rollups_collection_name()).document(rollup_id)
    existing_count = 0
    try:
        snap = rollup_ref.get()
        if snap.exists:
            payload = snap.to_dict() or {}
            existing_count = int(payload.get("count", 0) or 0)
    except Exception:
        existing_count = 0
    new_count = existing_count + 1
    rollup_ref.set(
        {
            "scope": normalized_scope,
            "issue_tag": issue_tag,
            "count": new_count,
            "sample_issue": sample_issue[:300],
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )

    candidate_threshold = 2
    active_threshold = 4
    if new_count < candidate_threshold:
        return

    status = "active" if new_count >= active_threshold else "candidate"
    priority = 85 if status == "active" else 60
    principle_ref = client.collection(_principles_collection_name()).document(rollup_id)
    principle_ref.set(
        {
            "id": rollup_id,
            "scope": normalized_scope,
            "category": issue_tag,
            "rule_text": _promotion_rule_for(normalized_scope, issue_tag),
            "status": status,
            "priority": priority,
            "evidence_count": new_count,
            "source": "feedback_rollup",
            "updated_at": firestore.SERVER_TIMESTAMP,
        },
        merge=True,
    )
    _CACHE.pop(f"firestore_principles:{normalized_scope}", None)


def record_prompt_feedback(
    scope: str,
    *,
    outcome: str,
    issues: list[str] | None = None,
    prompt_text: str = "",
    session_id: str = "",
    metadata: Mapping[str, Any] | None = None,
    force_log: bool = False,
) -> None:
    """Persists prompt feedback and promotes repeated issue patterns into rules."""
    if not _env_enabled("ENABLE_PROMPT_META_LEARNING", default=True):
        return

    issue_tags = _normalize_issue_tags(issues)
    normalized_scope = _normalize_scope(scope)
    normalized_outcome = str(outcome or "").strip().lower() or "observed"
    if not force_log and not issue_tags and normalized_outcome in {"success", "passed"} and not _env_enabled(
        "PROMPT_META_LEARNING_LOG_SUCCESSES",
        default=False,
    ):
        return

    record = {
        "id": str(uuid.uuid4()),
        "scope": normalized_scope,
        "outcome": normalized_outcome,
        "issue_tags": issue_tags,
        "issues": [str(item).strip()[:300] for item in (issues or []) if str(item).strip()],
        "prompt_excerpt": str(prompt_text or "").strip()[:600],
        "session_id": str(session_id or "").strip()[:120],
        "metadata": dict(metadata or {}),
        "created_at_epoch": int(time.time()),
    }
    _append_local_feedback(record)

    client = _firestore_client()
    if client is None:
        return

    try:
        from google.cloud import firestore
    except Exception:
        firestore = None

    try:
        firestore_record = dict(record)
        if firestore is not None:
            firestore_record["created_at"] = firestore.SERVER_TIMESTAMP
        client.collection(_feedback_collection_name()).document(record["id"]).set(firestore_record)
    except Exception as exc:
        logger.warning("Prompt feedback write failed: %s", exc)
        return

    sample_issue = record["issues"][0] if record["issues"] else normalized_outcome
    for tag in issue_tags:
        try:
            _upsert_rollup_and_principle(client, normalized_scope, tag, sample_issue)
        except Exception as exc:
            logger.warning("Prompt principle promotion failed for %s/%s: %s", normalized_scope, tag, exc)
