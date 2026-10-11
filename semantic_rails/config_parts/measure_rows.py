"""Authored measure accumulation and aggregation semantics."""

from __future__ import annotations

from typing import Any

from ..errors import SemanticLayerError
from ..schema import AccumulationConfig


def _normalize_accumulation(spec: dict[str, Any]) -> AccumulationConfig:
    """Normalize the nested accumulation block or a kind-only scalar."""
    raw = spec.get("accumulation", "")
    if isinstance(raw, dict):
        return AccumulationConfig(
            kind=str(raw.get("kind", "") or "").strip().lower(),
            snapshot=str(raw.get("snapshot", "") or "").strip().lower(),
        )
    return AccumulationConfig(kind=str(raw or "").strip().lower())


def _authored_additive(spec: dict[str, Any], kind: str, where: str) -> bool:
    """A measure's ``additive:`` flag: a boolean, and ``false`` only on ``kind: aggregate``."""
    additive = spec.get("additive", True)
    if not isinstance(additive, bool) or (not additive and kind != "aggregate"):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{where} additive must be true or false, and false only on kind: aggregate "
            "(an entity_count is a distinct count the engine computes itself)",
        )
    return additive


def _derive_measure_semantics(spec: dict[str, Any]) -> tuple[str, list[str], list[str], str]:
    accumulation = _normalize_accumulation(spec)
    kind = str(spec.get("kind", "")).strip().lower()
    if kind == "entity_count":
        measure_class = (
            "distinct_population" if accumulation.kind == "population" else "event_count"
        )
        return (
            "count_distinct",
            ["count_distinct"],
            ["sum", "avg", "min", "max", "median", "percentile", "first_value", "last_value"],
            measure_class,
        )
    if accumulation.kind == "stock":
        default_aggregation = (
            "first_value" if accumulation.snapshot == "start_of_period" else "last_value"
        )
        return (
            default_aggregation,
            ["last_value", "first_value", "sum", "avg", "min", "max", "median", "percentile"],
            ["count_distinct"],
            "semi_additive",
        )
    return (
        "sum",
        ["sum", "avg", "min", "max", "median", "percentile"],
        ["count_distinct", "first_value", "last_value"],
        "additive",
    )


def _suggested_aggregations(
    default_aggregation: str, allowed_aggregations: list[str], measure_class: str
) -> list[str]:
    preferred_orders = {
        "semi_additive": [
            "last_value",
            "first_value",
            "sum",
            "avg",
            "percentile",
            "median",
            "min",
            "max",
        ],
        "event_count": ["count_distinct"],
        "distinct_population": ["count_distinct"],
        "additive": ["sum", "avg", "median", "percentile", "min", "max"],
    }
    order = preferred_orders.get(measure_class, [default_aggregation, *allowed_aggregations])
    out: list[str] = []
    for value in [default_aggregation, *order, *allowed_aggregations]:
        text = str(value).strip()
        if text and text in allowed_aggregations and text not in out:
            out.append(text)
    return out


def _default_topics(name: str, fallback: str = "analytics") -> list[str]:
    parts = [
        part
        for part in str(name or "").replace("metric.", "").replace("measure.", "").split(".")
        if part
    ]
    return parts[:2] or [fallback]
