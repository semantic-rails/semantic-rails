"""Shared authored names, display labels and semantic SQL tokens.

Slug and title preserve Unicode names and let callers choose an empty-name fallback.
"""

from __future__ import annotations


def slug(value: str, *, fallback: str = "") -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    return "_".join(part for part in raw.split("_") if part) or fallback


def title(value: str, *, fallback: str = "") -> str:
    return (
        " ".join(part.capitalize() for part in str(value or "").replace("_", " ").split())
        or fallback
    )


def last_token(value: str) -> str:
    return str(value or "").split(".")[-1]


def semantic_token(value: str, *, fallback: str = "value") -> str:
    raw_value = str(value or "")
    token = last_token(value)
    if raw_value.startswith("entity.") and "_" in token:
        token = token.split("_", 1)[1]
    for prefix in ("jaffle_", "entity_", "metric_recipe_", "measure_"):
        if token.startswith(prefix):
            token = token[len(prefix) :]
    return slug(token, fallback=fallback)
