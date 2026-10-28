"""Listings through one-to-many routes: a dimension-only call answered as distinct rows.

A call with no measure, metric or time reads distinct rows of its dimensions, starting at an
entity that reaches every entity it reads by lookups (``leaf_paths``). When none does, the call
is bound once more as a listing (``bind_listing``): on the package with every relationship
walkable both ways (``listing_config``), starting at the first entity it reads, through any
one-to-many hop. Nothing is aggregated, so nothing can count twice. The route rules are the
package's (the call's own ``routes``, a recorded row, the start's own key, inherited rows, the
only route, a lookup default, else ``AMBIGUOUS_PATH``), and the caller's view and row filters
hold as for any call, since the listing binds through the same path. When the listing can't
answer either, the call keeps its own refusal, except a question of which route
(``AMBIGUOUS_PATH``, when it fits the response budget every transport answers within) or a row
filter on the route (``POLICY_DENIED``). A start row lists a blank row only when it reaches
nothing (``listing_rows``).
"""

from __future__ import annotations

import json
import weakref
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, TypeVar

from ..errors import SemanticLayerError
from ..query_call import route_options
from ..request_payload import MIN_RESULT_CHARS, max_result_chars
from ..schema import PackageConfig, VisiblePackageConfig
from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCte,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
    SqlWindow,
)
from .indexes import ViewOf, _dimension_index, _entity_index
from .paths import _column_ref

BOTH_WAYS = ["forward", "reverse"]
# The refusals a call's own bind gives a listing no lookup route answers: no start reaches every
# entity it reads (``PATH_NOT_FOUND``), one does only through a one-to-many hop
# (``MIXED_GRAIN_INVALID``), or its ``routes`` names a walk the relationships' directions don't
# allow.
_RETRIED = frozenset({"PATH_NOT_FOUND", "MIXED_GRAIN_INVALID"})
_RETRIED_ROUTE_ROWS = frozenset({"invalid_route_decision", "route_not_offered"})
# What a listing's own refusal says instead of the call's: which route, or a row filter on it.
_LISTING_REFUSALS = frozenset({"AMBIGUOUS_PATH", "POLICY_DENIED"})

_listing: ContextVar[bool] = ContextVar("listing", default=False)
_CONFIGS: dict[int, tuple[Any, PackageConfig]] = {}
T = TypeVar("T")


def listing_call(payload: Mapping[str, Any]) -> bool:
    """Whether ``payload`` (an engine-form call) asks for dimensions alone: a ``group_by``, and
    no measure, metric, time window or metric filter."""
    return (
        bool(payload.get("group_by"))
        and not payload.get("select")
        and not payload.get("time")
        and not payload.get("metric_filters")
    )


def listing_active() -> bool:
    """Whether a listing is being bound (``bind_listing``)."""
    return _listing.get()


@contextmanager
def _binding_listing() -> Iterator[None]:
    token = _listing.set(True)
    try:
        yield
    finally:
        _listing.reset(token)


def listing_config(config: PackageConfig) -> PackageConfig:
    """``config`` with every relationship walkable both ways, cached while ``config`` lives. A
    caller's view is cut from the listing form of its base, so the view's routes are still
    the whole package's (``fanout._visible_route``)."""
    key = id(config)
    cached = _CONFIGS.get(key)
    if cached is not None and cached[0]() is config:
        return cached[1]
    relationships = [
        rel
        if set(rel.allowed_directions) >= set(BOTH_WAYS)
        else replace(rel, allowed_directions=BOTH_WAYS)
        for rel in config.relationships
    ]
    listing = replace(config, relationships=relationships)
    if isinstance(config, VisiblePackageConfig):
        view = config.view
        base = ViewOf(listing_config(view.base), view.hidden, view.tokens)
        listing = replace(config, relationships=relationships, view=base)

    def _discard(_ref: Any, *, cache_key: int = key) -> None:
        _CONFIGS.pop(cache_key, None)

    _CONFIGS[key] = (weakref.ref(config, _discard), listing)
    return listing


def bind_listing(
    config: PackageConfig,
    payload: Mapping[str, Any],
    refusal: SemanticLayerError,
    bind: Callable[[PackageConfig], T],
) -> T | None:
    """The call bound as a listing (``bind`` on ``listing_config``), when ``refusal`` is its own
    bind's refusal of a dimension-only call; None when it isn't, or when the listing can't
    answer either and its refusal is not one of ``_LISTING_REFUSALS`` (the call's own stands)."""
    retried = refusal.code in _RETRIED or (
        refusal.code == "INVALID_QUERY" and refusal.details.get("reason") in _RETRIED_ROUTE_ROWS
    )
    if not retried or not listing_call(payload):
        return None
    with _binding_listing():
        try:
            return bind(listing_config(config))
        except SemanticLayerError as exc:
            if exc.code in _LISTING_REFUSALS and _shown_whole(exc, payload):
                raise
    return None


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _shown_whole(refusal: SemanticLayerError, payload: Mapping[str, Any]) -> bool:
    """Whether a listing's ``AMBIGUOUS_PATH`` fits the response budget whole: its message (an
    answer states it twice) and its details with each option's ready call
    (``query_call.route_options``), with room for the envelope. Where a dense package offers
    more routes than fit, the call's own refusal, which names what would connect it, is the
    useful answer. Any other listing refusal is shown."""
    if refusal.code != "AMBIGUOUS_PATH":
        return True
    details = dict(refusal.details)
    details["clarification"] = {
        **dict(details.get("clarification") or {}),
        "options": [dict(row) for row in (details.get("clarification") or {}).get("options") or []],
    }
    route_options(
        {key: value for key, value in payload.items() if key != "policy_context"}, details
    )
    size = 2 * len(_compact(str(refusal))) + len(_compact(details))
    return size + MIN_RESULT_CHARS <= max_result_chars()


def is_listing_plan(plan: Any) -> bool:
    """Whether ``plan`` reads distinct rows through a one-to-many hop: a listing."""
    if plan.measure_plans or plan.bound_measures:
        return False
    paths = (plan.fanout_strategy or {}).get("root_paths") or {}
    return any(row.get("status") == "rewrite_required" for row in paths.values())


LISTING_ROWS = "listing_rows"
_REACHED = "listing_reached"


def _known(exprs: list[SqlExpr]) -> SqlExpr:
    """Whether any of ``exprs`` holds a value."""
    tests = [SqlBinary(expr, "IS NOT", SqlLiteral(None)) for expr in exprs]
    out = tests[0]
    for test in tests[1:]:
        out = SqlBinary(out, "OR", test)
    return out


def listing_rows(select: SqlSelect, plan: Any, config: PackageConfig) -> SqlSelect:
    """A listing's distinct rows (``select``) without a start row's blank row beside its real
    ones: a row whose requested columns beyond the start's own entity all read NULL is kept
    only for a start row (by its key) that has no other row. A department with no rooms lists
    once, with NULL; one with rooms in two cities and a unit with no room lists its two
    cities; a room the call lists with no building keeps its row."""
    dimensions, root = _dimension_index(config), _entity_index(config)[plan.root_entity]
    pairs = list(zip(select.select, plan.group_by, strict=True))
    far = [field for field, dim_id in pairs if dimensions[dim_id].entity != plan.root_entity]
    if not far:
        return select
    own = [field.expression for field, dim_id in pairs if field not in far]
    keys: list[SqlExpr] = [_column_ref(root.table, column) for column in root.key_columns] or own
    known = SqlCase(
        [SqlCaseWhen(_known([field.expression for field in far]), SqlLiteral(1))], SqlLiteral(0)
    )
    rows = replace(
        select,
        select=[*select.select, SqlField(SqlWindow(SqlCall("MAX", [known]), keys), _REACHED)],
        group_by=[*select.group_by, *(key for key in keys if key not in select.group_by)],
    )
    column = {
        field.alias: SqlIdentifier(parts=[LISTING_ROWS, field.alias]) for field in select.select
    }
    reaches_nothing = SqlBinary(SqlIdentifier(parts=[LISTING_ROWS, _REACHED]), "=", SqlLiteral(0))
    return SqlSelect(
        ctes=[SqlCte(name=LISTING_ROWS, query=rows)],
        select=[SqlField(column[field.alias], field.alias) for field in select.select],
        from_table=SqlTableRef(name=LISTING_ROWS),
        where=[SqlBinary(_known([column[field.alias] for field in far]), "OR", reaches_nothing)],
        group_by=[column[field.alias] for field in select.select],
    )
