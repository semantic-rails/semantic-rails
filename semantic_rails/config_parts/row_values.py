"""Shared normalization of authored package row values."""

from __future__ import annotations

from typing import Any

from ..schema import require_boolean_mnpi


def _unique_aliases(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for value in group:
            text = str(value or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(text)
    return out


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value)).strip("_")


def _titleize(value: str) -> str:
    text = str(value or "").replace("_", " ").replace(".", " ").strip()
    return " ".join(part.capitalize() for part in text.split()) or value


def _ensure_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        raw = value.get("columns", value.get("column"))
        if raw is None:
            return []
        return [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else ([] if value is None else [value])
    return [dict(row or {}) for row in rows if isinstance(row, dict)]


def _normalize_meta(value: Any, where: str) -> dict[str, Any]:
    meta = dict(value or {}) if isinstance(value, dict) else {}
    require_boolean_mnpi(meta, where)
    return meta


def _merge_meta(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Shallow key-level merge of model-level meta into a child's meta.

    Child wins per key — authors override the model default at the
    measure/dimension/entity-binding level.
    """
    merged = dict(parent or {})
    merged.update(dict(child or {}))
    return merged


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _normalize_examples(value: Any) -> tuple[list[str], list[dict[str, Any]]]:
    text_examples: list[str] = []
    entries: list[dict[str, Any]] = []
    raw_items = value if isinstance(value, list) else ([] if value is None else [value])
    for raw in raw_items:
        if isinstance(raw, dict):
            entry = dict(raw)
            label = str(entry.get("question") or entry.get("name") or entry.get("id") or "").strip()
            if label:
                text_examples.append(label)
            entries.append(entry)
        else:
            text = str(raw or "").strip()
            if text:
                text_examples.append(text)
    return text_examples, entries
