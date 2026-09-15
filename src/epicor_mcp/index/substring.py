"""Literal Unicode case-insensitive matching; no optional retrieval dependency."""
from __future__ import annotations


def substring_score(query: str, *values: str) -> float:
    """Prefer an exact phrase, then count matching whitespace-delimited terms.

    Percent signs, underscores and quotes are literal characters, never SQL or
    FTS operators. Stable callers break equal-score ties by name/document id.
    """
    phrase = query.strip().casefold()
    if not phrase:
        return 0.0
    haystack = " ".join(str(value or "") for value in values).casefold()
    terms = tuple(dict.fromkeys(phrase.split()))
    return float((100 if phrase in haystack else 0) + sum(term in haystack for term in terms))
