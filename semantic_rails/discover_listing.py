"""The id listing MCP ``discover`` returns for empty terms.

The listing reads the caller's catalog (``catalog`` at verbosity ``summary``), so every id and
count in it is one the caller may see; the caller's view of the package says which model a
listed id belongs to, and gives each model's label. With no ``kinds`` it lists the caller's models, each with the ids of its measures,
metrics, segments and dimensions: the ids an agent picks a starting point from. When the caller
sees more dimensions than one page holds, each model gives its ``dimension_count`` instead, and
``kinds: ["dimension"]`` pages their ids. ``limit`` and ``offset`` page the models, and a page
also ends before the first model that doesn't fit ``max_chars``. With ``kinds``, or for a caller
who sees no model (a resource grant lists metrics, dimensions and temporal roles only), it lists
each kind's ids, a page per kind. Detail (aggregations, descriptions, time roles) is on each id's
inspect card, and the package's capability flags are on the ``catalog/index`` resource.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Any

from .errors import SemanticLayerError
from .expressions import AggregateExpr, MeasureRefExpr, MetricRecipeRefExpr, ScopedAggregateExpr
from .metadata import _first_root_entity
from .schema import MetricConfig, PackageConfig

# Ids per kind (models, in the listing by model) on one page, and the most dimensions the
# listing by model names by id.
ID_PAGE = 100
# What a model lists, as (kind, key).
_MODEL_KINDS = (
    ("measure", "measures"),
    ("metric", "metrics"),
    ("segment", "segments"),
    ("dimension", "dimensions"),
)
_WORDS = re.compile(r"[^\W_]+")


def _chars(payload: Any) -> int:
    """``payload``'s length as the MCP text channel renders it (``mcp.json_text``)."""
    return len(json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str))


def label_restates_id(label: Any, object_id: Any) -> bool:
    """Whether every word of ``label`` is already a word of ``object_id`` past its kind.

    "Store name" restates ``dimension.store_name``; "Orders" doesn't restate
    ``measure.order_count``, and "Food" doesn't restate ``dimension.product_type``.
    """

    if not isinstance(label, str) or not isinstance(object_id, str):
        return False
    words = set(_WORDS.findall(label.casefold()))
    return bool(words) and words <= set(_WORDS.findall(object_id.partition(".")[2].casefold()))


def _first_measure(
    expr: Any, recipes: Mapping[str, MetricConfig], seen: frozenset[str] = frozenset()
) -> str:
    """The first measure an expression reads, through the metrics it names, or ``""``."""
    if isinstance(expr, (MeasureRefExpr, AggregateExpr, ScopedAggregateExpr)):
        return expr.measure
    if isinstance(expr, MetricRecipeRefExpr):
        recipe = recipes.get(expr.metric_recipe)
        if recipe is None or recipe.id in seen:
            return ""
        return _first_measure(recipe.expression, recipes, seen | {recipe.id})
    if is_dataclass(expr):
        for item in fields(expr):
            value = getattr(expr, item.name)
            for child in value if isinstance(value, (list, tuple)) else (value,):
                if found := _first_measure(child, recipes, seen):
                    return found
    return ""


def _owners(config: PackageConfig) -> dict[str, str]:
    """Each measure, metric, segment and dimension id, with its model's entity id.

    A metric's model is the one whose rows it counts: its discover card's ``root_entity`` (a
    predicate or conversion metric's entity, else its first measure's model), else, for a metric
    whose card names none (a windowed one), the model of the first measure it reads.
    """
    measures = {row.id: row.entity for row in config.measures}
    owner = {**measures, **{row.id: row.entity for row in config.segments}}
    owner.update({row.id: row.entity for row in config.dimensions})
    recipes = {row.id: row for row in config.metric_recipes}
    for recipe in config.metric_recipes:
        root = ""
        with contextlib.suppress(SemanticLayerError):
            root = _first_root_entity(config, recipe.expression)
        owner[recipe.id] = root or measures.get(_first_measure(recipe.expression, recipes), "")
    return owner


def _truncated(message: str, remaining: dict[str, int], next_offset: int) -> dict[str, Any]:
    return {
        "code": "DISCOVER_IDS_TRUNCATED",
        "severity": "warning",
        "message": message,
        "details": {"remaining": remaining, "next_offset": next_offset},
    }


def _by_kind(
    catalog: Mapping[str, Any], kinds: Sequence[str], *, offset: int, limit: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Each listed kind's ids, ``limit`` from ``offset``, and a warning while more remain."""
    page: dict[str, Any] = {"counts": catalog["counts"]}
    more: dict[str, int] = {}
    for key, value in catalog.items():
        kind = key.removesuffix("_ids")
        if key != kind and (not kinds or kind in kinds):
            page[key] = list(value)[offset : offset + limit]
            if len(value) > offset + limit:
                more[kind] = len(value) - offset - limit
    if not more:
        return page, []
    message = (
        f"Listed {limit} ids per kind from offset {offset}; more remain for {sorted(more)}. "
        f"Pass offset={offset + limit} for the next page, kinds to list fewer kinds, or terms "
        "to rank candidates."
    )
    return page, [_truncated(message, more, offset + limit)]


def _by_model(
    catalog: Mapping[str, Any],
    config: PackageConfig,
    *,
    offset: int,
    limit: int,
    max_chars: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The caller's models from ``offset``, each with its ids, and the notes that say what is
    left out."""
    models = [str(model) for model in catalog["entity_ids"]]
    labels = {row.id: row.label for row in config.entities}
    entries: dict[str, dict[str, Any]] = {
        model: (
            {"label": labels[model]}
            if labels.get(model) and not label_restates_id(labels[model], model)
            else {}
        )
        for model in models
    }
    owners = _owners(config)
    # An id whose model the caller doesn't see is listed by kind, never under a model.
    loose: dict[str, list[str]] = {}
    for kind, key in _MODEL_KINDS:
        for object_id in map(str, catalog.get(f"{kind}_ids") or []):
            model = owners.get(object_id, "")
            if model in entries:
                entries[model].setdefault(key, []).append(object_id)
            else:
                loose.setdefault(f"{kind}_ids", []).append(object_id)
    notes: list[dict[str, Any]] = []
    dimensions = len(catalog.get("dimension_ids") or [])
    if dimensions > ID_PAGE:
        for entry in entries.values():
            if listed := entry.pop("dimensions", None):
                entry["dimension_count"] = len(listed)
        notes.append(
            {
                "code": "DISCOVER_DIMENSIONS_COUNTED",
                "severity": "info",
                "message": (
                    f"{dimensions:,} dimensions are more than one page ({ID_PAGE}), so each "
                    'model gives its dimension_count. Pass kinds ["dimension"] to page their '
                    "ids, or terms to rank them."
                ),
                "details": {"dimensions": dimensions},
            }
        )
    description = str(((catalog.get("meta") or {}).get("package") or {}).get("description") or "")
    page: dict[str, Any] = {
        "counts": catalog["counts"],
        **({"description": description} if description else {}),
        **loose,
        "models": {},
    }
    # A page holds at least one model. It ends at ``limit`` models, or before the first model
    # that doesn't fit ``max_chars``.
    used = _chars(page) + _chars(notes)
    for model in models[offset : offset + limit]:
        cost = _chars({model: entries[model]})
        if page["models"] and used + cost > max_chars:
            break
        page["models"][model] = entries[model]
        used += cost
    next_offset = offset + len(page["models"])
    if next_offset < len(models):
        message = (
            f"Listed {len(page['models'])} of {len(models)} models from offset {offset}. Pass "
            f"offset={next_offset} for the next page, kinds to list one kind's ids, or terms to "
            "rank candidates."
        )
        notes.insert(0, _truncated(message, {"models": len(models) - next_offset}, next_offset))
    return page, notes


def id_listing(
    catalog: Mapping[str, Any],
    config: PackageConfig,
    *,
    kinds: Sequence[str],
    offset: int,
    limit: int,
    max_chars: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The empty-terms listing of the caller's ``catalog``, and the warnings that go with it.

    ``config`` is the caller's view (``Runtime.view_for``). The listing reads from it which model
    each listed id belongs to and the model's label, and lists an id under a model only when the
    catalog lists that model too.
    """
    if kinds or not catalog.get("entity_ids"):
        return _by_kind(catalog, kinds, offset=offset, limit=limit)
    return _by_model(catalog, config, offset=offset, limit=limit, max_chars=max_chars)
