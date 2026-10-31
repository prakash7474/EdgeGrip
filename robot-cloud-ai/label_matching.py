"""Label comparison helpers.

Kept separate so both the inference pipeline (scoring a labelled request) and
the reporting layer can use it without a circular import.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")


def labels_match(predicted: str, expected: str) -> bool:
    """Compare a model label with an expected label.

    Handles ImageNet-style alias lists ("tabby, tabby cat") and taxonomy-width
    differences ("cat" vs "tiger cat") by comparing comma-separated variants and
    allowing one variant's tokens to contain the other's. This is a deliberate,
    documented heuristic, not a semantic match.
    """

    predicted = (predicted or "").strip()
    expected = (expected or "").strip()
    if not predicted or not expected:
        return False

    expected_variants = [tokens for tokens in (_tokens(v) for v in _variants(expected)) if tokens]
    if not expected_variants:
        return False

    for predicted_variant in _variants(predicted):
        predicted_tokens = _tokens(predicted_variant)
        if not predicted_tokens:
            continue
        for expected_tokens in expected_variants:
            if predicted_tokens <= expected_tokens or expected_tokens <= predicted_tokens:
                return True
    return False


def _variants(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"[,/|]", text) if part.strip()]
    return parts or [text.strip()]


def _tokens(text: str) -> set[str]:
    cleaned = text.lower().replace("_", " ").replace("-", " ")
    return set(_WORD.findall(cleaned))
