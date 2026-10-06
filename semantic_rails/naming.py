"""Shared authored names and display labels through :func:`slug` and :func:`title`.

Both helpers preserve Unicode names and let callers choose an empty-name fallback.
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
