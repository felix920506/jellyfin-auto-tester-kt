"""Stage 1 model blacklist.

Edit this file when a model needs to be temporarily blocked from Stage 1.
Patterns are matched after lowercasing and normalizing spaces/underscores to
hyphens. Provider prefixes such as ``openrouter/`` are ignored for matching.
"""

from __future__ import annotations

import fnmatch
import re


BLACKLISTED_STAGE1_MODEL_PATTERNS = (
    "gemini-3.1-*",
)


def is_stage1_model_blacklisted(model: str) -> bool:
    """Return True when ``model`` is known to break Stage 1."""

    candidates = _model_candidates(model)
    return any(
        fnmatch.fnmatchcase(candidate, pattern)
        for candidate in candidates
        for pattern in BLACKLISTED_STAGE1_MODEL_PATTERNS
    )


def _model_candidates(model: str) -> tuple[str, ...]:
    normalized = _normalize_model(model)
    if not normalized:
        return ()

    providerless = normalized.rsplit("/", 1)[-1]
    if providerless == normalized:
        return (normalized,)
    return (normalized, providerless)


def _normalize_model(model: str) -> str:
    return re.sub(r"[\s_]+", "-", str(model).strip().lower())
