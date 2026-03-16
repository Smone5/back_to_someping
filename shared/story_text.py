"""Shared story text cleanup helpers for live pages and final storybook exports."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

_CTRL_TOKEN_RE = re.compile(r"<ctrl\d+>", flags=re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_LABEL_RE = re.compile(r"^(caption|storybeat|scene)\s*:\s*", flags=re.IGNORECASE)
_DECORATIVE_RE = re.compile(r"[✨🌟💫🎵🎶🪄🔊⏹️▶️]+")
_CHOICE_PROMPT_RE = re.compile(
    r"(?:\s+)?(?:what should we do(?: next)?|should we|do you want to|or maybe)\b.*$",
    flags=re.IGNORECASE,
)
_PLACEHOLDER_STORYBEAT_RE = re.compile(
    r"^(?:"
    r"(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?here(?:'s| is)\s+(?:the\s+)?(?:illustration|image|picture|page|caption)\b.*"
    r"|(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?here(?:'s| is)\s+(?:(?:a|an|the)\s+)?(?:(?:[\w']+(?:-[\w']+)?\s+){0,6})?(?:illustration|image|picture|page|caption)\s*[.!?]?"
    r"|(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?here(?:'s| is)\s+(?:(?:a|an|the)\s+)?(?:(?:[\w']+(?:-[\w']+)?\s+){0,8})?(?:you|we)\s+(?:described|imagined|asked(?:\s+for)?|requested|wanted)\b.*"
    r"|(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?here(?:'s| is)\s+what\s+you\s+(?:described|imagined|asked(?:\s+for)?|requested|wanted)\b.*"
    r"|this\s+(?:illustration|image|picture|page)\s+(?:shows|has|is)\b.*"
    r"|.*\bfor\s+your\s+story\b.*"
    r")$",
    flags=re.IGNORECASE,
)
_META_STORY_PREFIX_RE = re.compile(
    r"^(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?"
    r"here(?:'s| is)\s+"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:(?:[\w']+(?:-[\w']+)?\s+){0,6})?"
    r"(?:illustration|image|picture|page|caption)(?:\s+of)?\s*(?::|-)\s*",
    flags=re.IGNORECASE,
)
_BARE_META_STORY_PREFIX_RE = re.compile(
    r"^(?:(?:a|an|the)\s+)?"
    r"(?:(?:[\w']+(?:-[\w']+)?\s+){0,6})?"
    r"(?:illustration|image|picture|page|caption)(?:\s+of)?\s*(?::|-)\s*",
    flags=re.IGNORECASE,
)
_META_REQUEST_PREFIX_RE = re.compile(
    r"^(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?"
    r"here(?:'s| is)\s+"
    r"(?:(?:a|an|the)\s+)?"
    r"(?:(?:[\w']+(?:-[\w']+)?\s+){0,8})?"
    r"(?:you|we)\s+(?:described|imagined|asked(?:\s+for)?|requested|wanted)\s*(?::|-)\s*",
    flags=re.IGNORECASE,
)
_META_WHAT_YOU_PREFIX_RE = re.compile(
    r"^(?:(?:sure|okay|ok|great|wonderful)[!,. ]+\s*)?"
    r"here(?:'s| is)\s+what\s+you\s+(?:described|imagined|asked(?:\s+for)?|requested|wanted)\s*(?::|-)\s*",
    flags=re.IGNORECASE,
)
_EMBEDDED_META_STORY_SUFFIX_RE = re.compile(
    r"(?:\s+|[.?!]\s+)"
    r"(?:"
    r"story\s+continuity\s+target\s*:.*"
    r"|transition\s+type\s*:.*"
    r"|active\s+recurring\s+cast\s*:.*"
    r"|carry\s+over\s+these\s+props\s+if\s+they\s+belong\s+in\s+the\s+connected\s+space\s*:.*"
    r"|keep\s+these\s+same\s+characters\s+in\s+view\s*:.*"
    r"|keep\s+these\s+recurring\s+characters\s+visible\s+and\s+consistent\s*:.*"
    r"|do\s+not\s+invent\s+new\s+named\s+characters.*"
    r"|do\s+not\s+replace\s+the\s+current\s+helper.*"
    r"|stay\s+with\s+the\s+already\s+established\s+story\s+cast.*"
    r"|carry\s+the\s+same\s+established\s+cast\s+forward.*"
    r"|make\s+the\s+destination\s+unmistakable.*"
    r"|show\s+unmistakable\b.*"
    r"|tone\s*:.*"
    r")$",
    flags=re.IGNORECASE,
)
_CONVERSATIONAL_STORYBEAT_RE = re.compile(
    r"^(?:"
    r"(?:oh|wow|yay|great|okay|ok|sure)[!,. ]*\s*i\s+(?:love|like)\s+that\s+idea\b.*"
    r"|i\s+(?:love|like)\s+that\s+idea\b.*"
    r"|let'?s\b.*"
    r"|close\s+your\s+eyes\b.*"
    r"|look[,! ]+.*"
    r"|come\s+on\b.*"
    r"|we(?:'re| are)\s+going\s+to\b.*"
    r"|what(?:'s| is)\s+your\s+favorite\s+part\b.*"
    r")$",
    flags=re.IGNORECASE,
)
_PROMPTY_STORY_START_RE = re.compile(
    r"^(?:then|next|now|here)\s*,?\s*(?:go|look|see|pick|choose|find|follow|slide|swim|dance|turn|climb|open|peek|come)\b",
    flags=re.IGNORECASE,
)
_BROKEN_FILLER_RE = re.compile(
    r"\b(?:then,\s*so|so,\s*what|then,\s*what)\b",
    flags=re.IGNORECASE,
)
_LEADING_BRANCH_LABEL_RE = re.compile(
    r"^(?:(?:inside|outside)\s+go\s+(?:inside|outside)\s+[^,.!?]+,\s*)+",
    flags=re.IGNORECASE,
)
_INSTRUCTIONAL_STORYBEAT_RE = re.compile(
    r"^(?:"
    r"keep(?:\s+these|\s+this)?\b.*"
    r"|make\s+the\b.*"
    r"|preserve\s+the\b.*"
    r"|retain\b.*"
    r"|stay\s+with\b.*"
    r"|carry\s+the\s+same\b.*"
    r"|do\s+not\b.*"
    r"|treat\s+this\s+as\b.*"
    r"|tone\s*:\b.*"
    r"|show\s+unmistakable\b.*"
    r")$",
    flags=re.IGNORECASE,
)
_LIKELY_STORY_VERB_RE = re.compile(
    r"\b(?:"
    r"is|are|was|were|am|be|being|been|"
    r"has|have|had|"
    r"can|could|will|would|did|do|does|"
    r"go(?:es|ing)?|went|"
    r"come(?:s|ing)?|came|"
    r"see(?:s|ing)?|saw|seen|"
    r"look(?:s|ed|ing)?|"
    r"slide(?:s|d|ing)?|slid|"
    r"float(?:s|ed|ing)?|"
    r"shimmer(?:s|ed|ing)?|"
    r"glow(?:s|ed|ing)?|"
    r"sparkle(?:s|d|ing)?|"
    r"whisper(?:s|ed|ing)?|"
    r"laugh(?:s|ed|ing)?|"
    r"smile(?:s|d|ing)?|"
    r"sleep(?:s|ing)?|slept|"
    r"dream(?:s|ed|ing)?|"
    r"walk(?:s|ed|ing)?|"
    r"run(?:s|ning)?|ran|"
    r"curl(?:s|ed|ing)?|"
    r"dance(?:s|d|ing)?|"
    r"play(?:s|ed|ing)?|"
    r"hold(?:s|ing)?|held|"
    r"wave(?:s|d|ing)?|"
    r"guide(?:s|d|ing)?|"
    r"lead(?:s|ing)?|led|"
    r"live(?:s|d|ing)?|"
    r"wait(?:s|ed|ing)?|"
    r"tuck(?:s|ed|ing)?|"
    r"snuggle(?:s|d|ing)?|"
    r"rest(?:s|ed|ing)?|"
    r"ride(?:s|ing)?|rode|"
    r"open(?:s|ed|ing)?|"
    r"close(?:s|d|ing)?|"
    r"bounce(?:s|d|ing)?|"
    r"pop(?:s|ped|ping)?|"
    r"climb(?:s|ed|ing)?|"
    r"discover(?:s|ed|ing)?|"
    r"find(?:s|ing)?|found|"
    r"meet(?:s|ing)?|met|"
    r"peek(?:s|ed|ing)?|"
    r"appear(?:s|ed|ing)?|"
    r"drift(?:s|ed|ing)?|"
    r"sit(?:s|ting)?|sat|"
    r"nap(?:s|ped|ping)?|"
    r"whirl(?:s|ed|ing)?|"
    r"twirl(?:s|ed|ing)?|"
    r"twinkle(?:s|d|ing)?|"
    r"shine(?:s|d|ing)?|shone"
    r")\b",
    flags=re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_TRAILING_WEAK_WORDS = {
    "a",
    "an",
    "and",
    "around",
    "at",
    "behind",
    "beside",
    "by",
    "for",
    "from",
    "in",
    "into",
    "leading",
    "near",
    "of",
    "on",
    "or",
    "out",
    "over",
    "past",
    "right",
    "so",
    "the",
    "to",
    "toward",
    "towards",
    "under",
    "very",
    "with",
}
_TRAILING_DESCRIPTOR_WORDS = {
    "big",
    "bright",
    "cozy",
    "friendly",
    "gentle",
    "glowing",
    "golden",
    "happy",
    "little",
    "playful",
    "purple",
    "rainbow",
    "silly",
    "small",
    "soft",
    "sparkly",
    "spooky",
    "tiny",
    "warm",
}
_TRAILING_FRAGMENT_WORDS = {
    "made",
    "filled",
    "covered",
    "decorated",
    "lined",
    "stacked",
    "wrapped",
    "topped",
}
_PUNCT_TRANSLATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
)
_ODD_GLYPHS = {
    "\ufffd",  # replacement character
    "\u25a1",  # white square
    "\u25ad",  # white rectangle
    "\u25af",  # white vertical rectangle
    "\u25fb",  # white medium square
    "\u25fc",  # black medium square
    "\u25fd",  # white medium small square
    "\u25fe",  # black medium small square
}
_SPATIAL_START_WORDS = {
    "around",
    "at",
    "behind",
    "beside",
    "in",
    "inside",
    "near",
    "on",
    "outside",
    "over",
    "under",
}
_BROKEN_LINKING_PHRASE_RE = re.compile(r"\b\w+ly\s+is\s+\w+(?:ed|ing)\b", flags=re.IGNORECASE)
_TRUNCATED_CONJUNCTION_TAIL_RE = re.compile(
    r"\b(?:but|or|so)\b(?:\s+\w+){0,2}[.!?]$",
    flags=re.IGNORECASE,
)


def _strip_invisible_and_odd_chars(text: str) -> str:
    cleaned: list[str] = []
    for ch in text:
        codepoint = ord(ch)
        if ch in _ODD_GLYPHS:
            continue
        if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
            continue
        category = unicodedata.category(ch)
        if category in {"Cc", "Cf", "Cs", "Co", "Cn"}:
            cleaned.append(" ")
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def _normalized_story_tokens(sentence: str) -> list[str]:
    return [
        token.rstrip(".,!?;:").lower()
        for token in sentence.split()
        if token.rstrip(".,!?;:")
    ]


def _has_adjacent_repeated_story_word(sentence: str) -> bool:
    tokens = _normalized_story_tokens(sentence)
    previous = ""
    for token in tokens:
        if token and token == previous:
            return True
        previous = token
    return False


def _ends_with_weak_story_word(sentence: str) -> bool:
    tokens = _normalized_story_tokens(sentence)
    return bool(tokens) and tokens[-1] in _TRAILING_WEAK_WORDS


def _has_placeholder_story_ending(sentence: str) -> bool:
    tokens = _normalized_story_tokens(sentence)
    if not tokens or tokens[-1] != "appears":
        return False
    return len(tokens) >= 5 or tokens[0] in _SPATIAL_START_WORDS


def _ends_with_dangling_story_fragment(sentence: str) -> bool:
    tokens = _normalized_story_tokens(sentence)
    return bool(tokens) and tokens[-1] in _TRAILING_FRAGMENT_WORDS


def _has_broken_story_linking(sentence: str) -> bool:
    return bool(
        _BROKEN_LINKING_PHRASE_RE.search(sentence)
        or _TRUNCATED_CONJUNCTION_TAIL_RE.search(sentence)
    )


def _score_normalized_story_sentence(sentence: str) -> int:
    words = [token for token in sentence.split() if token]
    has_verb = bool(_LIKELY_STORY_VERB_RE.search(sentence))
    score = 0

    if len(words) >= 5:
        score += 3
    elif len(words) >= 3:
        score += 1
    else:
        score -= 1

    score += 3 if has_verb else -2

    if _PROMPTY_STORY_START_RE.match(sentence):
        score -= 4
    if _BROKEN_FILLER_RE.search(sentence):
        score -= 3
    if _has_adjacent_repeated_story_word(sentence):
        score -= 6
    if _ends_with_weak_story_word(sentence):
        score -= 5
    if _ends_with_dangling_story_fragment(sentence):
        score -= 6
    if _has_placeholder_story_ending(sentence):
        score -= 4
    if _has_broken_story_linking(sentence):
        score -= 5
    if len(words) <= 3 and not has_verb:
        score -= 3
    if sentence.endswith("..."):
        score -= 1

    return score


def _finalize_storybeat_candidate(candidate: str, *, max_chars: int) -> str:
    candidate = candidate.strip(" \"'")
    if not candidate:
        return ""
    candidate = _META_STORY_PREFIX_RE.sub("", candidate).strip(" \"'")
    candidate = _BARE_META_STORY_PREFIX_RE.sub("", candidate).strip(" \"'")
    candidate = _EMBEDDED_META_STORY_SUFFIX_RE.sub("", candidate).strip(" \"'")
    if not candidate:
        return ""
    candidate = _LEADING_BRANCH_LABEL_RE.sub("", candidate).strip(" ,;:-")
    if not candidate:
        return ""
    if _CONVERSATIONAL_STORYBEAT_RE.match(candidate):
        return ""
    if _INSTRUCTIONAL_STORYBEAT_RE.match(candidate):
        return ""
    if len(candidate) > max_chars:
        truncated = candidate[:max_chars].rsplit(" ", 1)[0].strip()
        candidate = truncated or candidate[:max_chars].strip()
    candidate = candidate.rstrip(" ,;:-")
    if candidate and candidate[-1] not in ".!?":
        candidate += "."
    return candidate


def clean_story_text(text: Any) -> str:
    normalized = html.unescape(
        unicodedata.normalize("NFKC", str(text or "")).translate(_PUNCT_TRANSLATION)
    )
    normalized = _strip_invisible_and_odd_chars(normalized)
    if not normalized:
        return ""
    normalized = _CTRL_TOKEN_RE.sub(" ", normalized)
    normalized = _HTML_TAG_RE.sub(" ", normalized)
    normalized = _LABEL_RE.sub("", normalized.strip())
    normalized = _META_STORY_PREFIX_RE.sub("", normalized)
    normalized = _BARE_META_STORY_PREFIX_RE.sub("", normalized)
    normalized = _META_REQUEST_PREFIX_RE.sub("", normalized)
    normalized = _META_WHAT_YOU_PREFIX_RE.sub("", normalized)
    normalized = _EMBEDDED_META_STORY_SUFFIX_RE.sub("", normalized)
    normalized = _DECORATIVE_RE.sub(" ", normalized)
    normalized = _CHOICE_PROMPT_RE.sub("", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip(" \"'")


def split_story_sentences(text: Any) -> list[str]:
    cleaned = clean_story_text(text)
    if not cleaned:
        return []
    return [part.strip() for part in _SENTENCE_SPLIT_RE.split(cleaned) if part.strip()]


def best_story_sentence(text: Any, *, max_chars: int = 220) -> str:
    cleaned = clean_story_text(text)
    if not cleaned:
        return ""
    if _PLACEHOLDER_STORYBEAT_RE.match(cleaned):
        return ""

    raw_candidates = split_story_sentences(cleaned) or [cleaned]
    best = ""
    best_score = -999
    for raw_candidate in raw_candidates:
        candidate = _finalize_storybeat_candidate(raw_candidate, max_chars=max_chars)
        if not candidate:
            continue
        score = _score_normalized_story_sentence(candidate)
        if score > best_score:
            best = candidate
            best_score = score

    return best


def normalize_storybeat_text(text: Any, *, max_chars: int = 220) -> str:
    return best_story_sentence(text, max_chars=max_chars)


def story_sentence_needs_revision(text: Any) -> bool:
    sentence = normalize_storybeat_text(text, max_chars=400)
    if not sentence:
        return True
    return (
        _has_adjacent_repeated_story_word(sentence)
        or _ends_with_weak_story_word(sentence)
        or _ends_with_dangling_story_fragment(sentence)
        or _has_placeholder_story_ending(sentence)
        or _has_broken_story_linking(sentence)
    )


def truncate_story_sentence(text: Any, *, max_words: int) -> str:
    sentence = normalize_storybeat_text(text, max_chars=400)
    if not sentence:
        return ""
    words = sentence.split()
    if len(words) <= max_words:
        return sentence
    clipped = words[:max_words]
    while (
        len(clipped) > 4
        and clipped[-1].rstrip(".,!?;:").lower() in _TRAILING_DESCRIPTOR_WORDS
        and clipped[-2].rstrip(".,!?;:").lower() in (_TRAILING_WEAK_WORDS | {"a", "an", "the"})
    ):
        clipped.pop()
    while len(clipped) > 4 and clipped[-1].rstrip(".,!?;:").lower() in _TRAILING_FRAGMENT_WORDS:
        clipped.pop()
    while len(clipped) > 4 and clipped[-1].rstrip(".,!?;:").lower() in _TRAILING_WEAK_WORDS:
        clipped.pop()
    compact = " ".join(clipped).rstrip(" ,;:-.!?")
    if compact and compact[-1] not in ".!?":
        compact += "."
    return compact


def story_sentence_quality_score(text: Any) -> int:
    sentence = normalize_storybeat_text(text, max_chars=400)
    if not sentence:
        return 0
    return _score_normalized_story_sentence(sentence)
