"""Audio utilities for the FastAPI backend.

Responsibilities:
1. PII scrubbing — runs in asyncio.to_thread() to prevent event loop starvation
   (Iteration 10, #4 — Audio Starvation fix).
2. Noise gate — simple energy-based VAD gate to block environmental hum from
   triggering the AI (Iteration 4, #7 — Empty Room Audio Bleed fix).
3. ElevenLabs audio track archival — saves per-turn PCM chunks to GCS for
   the final FFmpeg assembly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import struct

logger = logging.getLogger(__name__)

# ── PII patterns — lightweight regex, no heavy NLP libs on the event loop ─────
_PII_PATTERNS = [
    # US phone numbers
    (re.compile(r"\b(\d{3})[-.\s](\d{3})[-.\s](\d{4})\b"), "[phone number]"),
    # Email addresses
    (re.compile(r"\b[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z.]+\b"), "[email]"),
    # Possible school name patterns
    (re.compile(r"\b(I go to|my school is|at)\s+[A-Z][a-z]+ ?(Elementary|Middle|School|Academy)\b", re.I),
     r"\1 my magical learning castle"),
    # Street addresses
    (re.compile(r"\b\d{1,5}\s+\w+\s+(Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln)\b", re.I),
     "[magic address]"),
]

# Keep the backend gate slightly conservative so accidental bumps/background hum
# do not become full Gemini turns after the frontend VAD already fired.
_NOISE_GATE_RMS_THRESHOLD = max(1, int(os.environ.get("NOISE_GATE_RMS_THRESHOLD", "110")))


def _scrub_pii_sync(text: str) -> str:
    """Synchronous PII scrubber — called via asyncio.to_thread(), never on main loop."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


async def scrub_pii(text: str) -> str:
    """Async PII scrubber — offloads regex to thread pool to avoid event loop starvation."""
    return await asyncio.to_thread(_scrub_pii_sync, text)


def passes_noise_gate(pcm_bytes: bytes) -> bool:
    """Returns True if the PCM frame contains enough energy to be real speech.

    Blocks near-silence frames from being sent to Gemini, preventing the empty-room
    ambient hum from constantly triggering the VAD (Iteration 4, Expert Audit #7).

    Args:
        pcm_bytes: Raw 16kHz 16-bit mono PCM bytes.
    """
    if len(pcm_bytes) < 2:
        return False
    # Interpret as signed 16-bit PCM samples
    num_samples = len(pcm_bytes) // 2
    samples = struct.unpack(f"<{num_samples}h", pcm_bytes[:num_samples * 2])
    rms = (sum(s * s for s in samples) / num_samples) ** 0.5
    return rms > _NOISE_GATE_RMS_THRESHOLD
