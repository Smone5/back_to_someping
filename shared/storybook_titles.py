"""Shared helpers for storybook title generation and validation."""

from __future__ import annotations

from collections import Counter
import re

_TITLE_CONNECTORS = {"a", "an", "the", "and", "of", "in", "on", "to", "for", "with"}
_TITLE_WEAK_WORDS = {
    "what", "where", "when", "why", "how", "can", "could", "should", "would", "do", "did",
    "look", "show", "draw", "read", "tell", "lets", "let", "next", "page", "pages", "scene",
    "story", "book", "books", "drawing", "image", "picture", "illustration",
}
_TITLE_GENERIC_WORDS = {
    "adventure", "magic", "magical", "bright", "glowing", "little", "gentle", "fun", "funny",
    "pretty", "cool", "silly", "friend", "friends", "thing", "things", "some", "something",
}
_TITLE_DESCRIPTOR_WORDS = {
    "chocolate", "moonlit", "dark", "spooky", "friendly", "secret", "rainbow", "candy",
    "cupcake", "frosted", "floating", "glowing", "stormy", "sleepy", "twinkling", "cozy",
    "giant", "gummy", "marshmallow", "midnight", "golden", "sparkly", "swirly",
}
_TITLE_THEME_NOUN_WEIGHTS = {
    "castle": 10,
    "dragon": 10,
    "river": 9,
    "ocean": 9,
    "forest": 8,
    "workshop": 8,
    "cave": 8,
    "moon": 8,
    "boat": 8,
    "ghost": 8,
    "cupcake": 8,
    "cupcakes": 8,
    "path": 7,
    "treasure": 7,
    "tower": 7,
    "hills": 7,
    "hill": 7,
    "garden": 6,
    "library": 6,
    "kingdom": 6,
    "tree": 6,
    "train": 6,
    "door": 5,
    "window": 4,
    "windows": 4,
}
_SPECIAL_TITLE_PHRASES = {
    "candy land": 14,
    "santa's workshop": 14,
    "gummy bear hills": 13,
    "chocolate river": 13,
    "chocolate ocean": 13,
    "rainbow path": 12,
    "dark castle": 12,
    "spooky castle": 12,
    "moonlit castle": 12,
    "cupcake hills": 12,
    "floating candles": 11,
    "secret door": 10,
}
_PROMPTY_SOURCE_RE = re.compile(
    r"^(?:what|where|when|why|how|can|could|should|do|did|show|draw|look|read|tell|close your eyes|imagine)\b",
    flags=re.IGNORECASE,
)
_TITLE_TOKEN_RE = re.compile(r"[A-Za-z']+")
_SPECIAL_CASE_WORDS = {
    "candy land": "Candy Land",
    "gummy bear hills": "Gummy Bear Hills",
    "santa's workshop": "Santa's Workshop",
}


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _title_case_word(word: str) -> str:
    parts = [part for part in str(word).split("'")]
    if not parts:
        return ""
    return "'".join(part[:1].upper() + part[1:].lower() if part else "" for part in parts)


def _smart_title_case(text: str) -> str:
    cleaned = _clean_space(text)
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered in _SPECIAL_CASE_WORDS:
        return _SPECIAL_CASE_WORDS[lowered]
    words = cleaned.split()
    titled: list[str] = []
    for idx, word in enumerate(words):
        bare = re.sub(r"[^a-z']", "", word.lower())
        if idx > 0 and bare in {"and", "the", "of", "in", "on", "to"}:
            titled.append(bare)
        else:
            titled.append(_title_case_word(word))
    return " ".join(titled)


def _story_source_fragments(scene_descriptions: list[str], story_summary: str) -> list[str]:
    fragments: list[str] = []
    raw_sources = [*(scene_descriptions or []), story_summary or ""]
    for raw in raw_sources:
        for piece in re.split(r"[\n]+|(?<=[.!?])\s+", str(raw or "")):
            cleaned = _clean_space(piece).strip(" -")
            if not cleaned:
                continue
            if "?" in cleaned and _PROMPTY_SOURCE_RE.search(cleaned):
                continue
            if _PROMPTY_SOURCE_RE.search(cleaned):
                continue
            fragments.append(cleaned)
    return fragments


def _story_vocabulary(scene_descriptions: list[str], story_summary: str, child_name: str = "") -> set[str]:
    vocab = {
        token.lower()
        for fragment in _story_source_fragments(scene_descriptions, story_summary)
        for token in _TITLE_TOKEN_RE.findall(fragment)
        if token
    }
    clean_child_name = _clean_space(child_name).lower()
    if clean_child_name and clean_child_name != "friend":
        vocab.update(_TITLE_TOKEN_RE.findall(clean_child_name))
    return vocab


def clean_storybook_title(raw: str) -> str:
    title = _clean_space(raw)
    if not title or title.lower() == "auto":
        return ""
    if "sdk_http_response" in title.lower() or "candidates=[" in title.lower():
        return ""
    if "?" in title:
        return ""
    title = re.sub(r"^(title|story)\s*[:\-]\s*", "", title, flags=re.IGNORECASE)
    title = title.strip().strip("\"'`")
    title = _clean_space(title)
    tokens = _TITLE_TOKEN_RE.findall(title)
    if len(tokens) < 2:
        return ""
    if len(tokens) > 7:
        return ""

    lowered = [token.lower() for token in tokens]
    meaningful = [token for token in lowered if token not in _TITLE_CONNECTORS]
    if not meaningful:
        return ""
    if meaningful[0] in _TITLE_WEAK_WORDS:
        return ""
    if any(token in {"look", "should", "drawing", "picture", "image", "illustration"} for token in meaningful):
        return ""
    if all(token in _TITLE_WEAK_WORDS or token in _TITLE_GENERIC_WORDS for token in meaningful):
        return ""
    if re.search(r"reading\s+rainbow", title, re.IGNORECASE):
        return ""
    return _smart_title_case(title)


def validate_storybook_title(
    raw_title: str,
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str = "",
) -> str:
    title = clean_storybook_title(raw_title)
    if not title:
        return ""
    vocab = _story_vocabulary(scene_descriptions, story_summary, child_name)
    title_tokens = [
        token.lower()
        for token in _TITLE_TOKEN_RE.findall(title)
        if token.lower() not in _TITLE_CONNECTORS
    ]
    if not title_tokens:
        return ""
    clean_child_name = _clean_space(child_name).lower()
    meaningful = [
        token for token in title_tokens
        if token not in _TITLE_WEAK_WORDS and token not in _TITLE_GENERIC_WORDS and token != clean_child_name
    ]
    if not meaningful:
        return ""
    if not any(token in vocab for token in meaningful):
        return ""
    return title


def _normalize_candidate_phrase(raw_phrase: str, child_name: str = "") -> str:
    words = [word for word in _TITLE_TOKEN_RE.findall(raw_phrase) if word]
    if not words:
        return ""
    clean_child_name = _clean_space(child_name).lower()
    while words and words[0].lower() in _TITLE_CONNECTORS:
        words.pop(0)
    while words and words[-1].lower() in _TITLE_CONNECTORS:
        words.pop()
    if not words:
        return ""
    lowered = [word.lower() for word in words]
    if lowered[0] in _TITLE_WEAK_WORDS:
        return ""
    if clean_child_name and lowered[0] == clean_child_name:
        return ""
    phrase = _smart_title_case(" ".join(words[:3]))
    return clean_storybook_title(phrase) or ""


def _collect_title_phrase_counts(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str = "",
) -> Counter[str]:
    counts: Counter[str] = Counter()
    weighted_sources = (
        [(str(text or ""), 3) for text in (scene_descriptions or [])]
        + [(story_summary or "", 1)]
    )
    for raw_text, weight in weighted_sources:
        text = _clean_space(raw_text)
        if not text:
            continue
        lowered_text = text.lower()
        for phrase, score in _SPECIAL_TITLE_PHRASES.items():
            if phrase in lowered_text:
                counts[_smart_title_case(phrase)] += score * weight
        words = _TITLE_TOKEN_RE.findall(text)
        for idx, word in enumerate(words):
            lowered = word.lower()
            noun_score = _TITLE_THEME_NOUN_WEIGHTS.get(lowered)
            if noun_score is None:
                continue
            phrase_words = [word]
            scan = idx - 1
            while scan >= 0 and len(phrase_words) < 3:
                prev = words[scan]
                prev_lower = prev.lower()
                if prev_lower in _TITLE_CONNECTORS:
                    scan -= 1
                    continue
                if (
                    prev_lower in _TITLE_DESCRIPTOR_WORDS
                    or prev[0].isupper()
                    or "'" in prev
                ):
                    phrase_words.insert(0, prev)
                    scan -= 1
                    continue
                break
            candidate = _normalize_candidate_phrase(" ".join(phrase_words), child_name=child_name)
            if not candidate:
                continue
            counts[candidate] += (noun_score + len(phrase_words) * 2) * weight
    return counts


def _candidate_titles(primary: str, secondary: str, child_name: str) -> list[str]:
    clean_child_name = _smart_title_case(_clean_space(child_name)) if _clean_space(child_name).lower() != "friend" else ""
    titles: list[str] = []
    if clean_child_name and primary:
        if primary.lower() in {"candy land", "santa's workshop"} or "'" in primary:
            titles.append(f"{clean_child_name} in {primary}")
        titles.append(f"{clean_child_name} and the {primary}")
    if primary:
        if primary.lower() in {"candy land", "santa's workshop"} or "'" in primary:
            titles.append(f"A Visit to {primary}")
        titles.append(f"The {primary}")
    if primary and secondary:
        secondary_tokens = [token for token in _TITLE_TOKEN_RE.findall(secondary) if token]
        if secondary_tokens:
            titles.append(f"The {primary} and the {secondary_tokens[-1]}")
    if clean_child_name:
        suffix = "'" if clean_child_name.endswith(("s", "S")) else "'s"
        titles.append(f"{clean_child_name}{suffix} Story")
    titles.append("A Storybook Adventure")
    seen: set[str] = set()
    ordered: list[str] = []
    for title in titles:
        cleaned = _clean_space(title)
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            ordered.append(cleaned)
    return ordered


def heuristic_storybook_title(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str = "",
) -> str:
    counts = _collect_title_phrase_counts(scene_descriptions, story_summary, child_name)
    ranked = [phrase for phrase, _ in counts.most_common(6)]
    primary = ranked[0] if ranked else ""
    secondary = next((phrase for phrase in ranked[1:] if phrase.lower() != primary.lower()), "")

    for candidate in _candidate_titles(primary, secondary, child_name):
        validated = validate_storybook_title(candidate, scene_descriptions, story_summary, child_name)
        if validated:
            return validated

    clean_child_name = _smart_title_case(_clean_space(child_name))
    if clean_child_name and clean_child_name.lower() != "friend":
        suffix = "'" if clean_child_name.endswith(("s", "S")) else "'s"
        return f"{clean_child_name}{suffix} Story"
    return "A Storybook Adventure"


def build_storybook_title_prompt(
    scene_descriptions: list[str],
    story_summary: str,
    child_name: str = "",
) -> str:
    scene_lines = [
        f"{idx}. {_clean_space(desc) or 'Scene with no description.'}"
        for idx, desc in enumerate(scene_descriptions or [], start=1)
    ]
    child_line = _clean_space(child_name)
    return (
        "Write one strong children's storybook title for this finished story.\n"
        "Rules:\n"
        "- 3 to 6 words.\n"
        "- Sound like a real book title, not a question, prompt, or sentence fragment.\n"
        "- Use the story's most memorable place, creature, object, or twist.\n"
        "- Avoid generic words like story, adventure, drawing, page, look, show, should, what, where, when, why, or how.\n"
        "- No quotes.\n"
        "- Return only the title text.\n\n"
        f"Child name: {child_line or 'friend'}\n"
        f"Story summary: {_clean_space(story_summary) or 'No summary available.'}\n\n"
        "Scenes:\n"
        + ("\n".join(scene_lines) if scene_lines else "1. No scene descriptions available.")
    ).strip()
