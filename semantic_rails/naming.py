"""Shared authored names, display labels and semantic SQL tokens.

Slug and title preserve Unicode names and let callers choose an empty-name fallback.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any


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


def governed_names(config: Any) -> frozenset[str]:
    """Every name a package's metrics and measures answer to: id, name and the id's last
    segment (the column a select of it answers under)."""
    names: set[str] = set()
    for row in [*config.metric_recipes, *config.measures]:
        names.update(value for value in (row.id, row.name, last_token(row.id)) if value)
    return frozenset(names)


def unclaimed_name(candidates: Sequence[str], taken: Collection[str]) -> str:
    """The first of ``candidates`` that ``taken`` doesn't hold, else the first one numbered: a
    generated column never answers under a name a governed object answers to."""
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    number = 2
    while f"{candidates[0]}_{number}" in taken:
        number += 1
    return f"{candidates[0]}_{number}"
