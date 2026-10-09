"""The caller's partial call: merge, validate, and trim validation errors."""

from __future__ import annotations

import json
from typing import Any

from ..errors import SemanticLayerError
from .call import Call, Order, QueryBuilder, Select, Time
from .time_reference import time_policy_context


def _merge_partial_query(config: Any, draft_query: Call, partial_query: Call | None) -> Call:
    """Merge a generated draft with the caller's partial call.

    Partial-call preservation is a hard planner invariant: no field the caller supplied may
    silently disappear. List fields put the caller's entries first and the generated ones
    after; a scalar the caller sets wins; ``time`` merges field by field, the caller's window
    replacing the draft's whole. A caller's group_by holding one dimension that stands for an
    entity's rows (``_entity_stand_ins``) settles how they show: the draft's other stand-ins of
    that entity, and their order_by entries, are left out. A generated select that computes one
    of the caller's, or takes the column name of one, gives way to it (``_without_caller_selects``).
    """

    from .groupings import _entity_stand_ins  # noqa: WPS433

    if partial_query is None:
        return draft_query
    partial = partial_query
    chosen = set(partial.group_by)
    settled: set[str] = set()
    for entity in config.entities if chosen else []:
        keys, shown = _entity_stand_ins(config, entity, str(entity.label or ""))
        if chosen & {*keys, *shown}:
            settled |= {*keys, *shown} - chosen
    generated, renamed = _without_caller_selects(config, draft_query.select, partial.select)
    orders = [
        Order(renamed.get(row.field, row.field), row.direction)
        for row in draft_query.order_by
        if row.field not in settled
    ]
    mine = QueryBuilder(config)
    for item in partial.select:
        mine.select(item.expression, item.alias)
    builder = QueryBuilder(config, draft_query).selects([*mine.build().select, *generated])
    builder.groups([*partial.group_by, *(g for g in draft_query.group_by if g not in settled)])
    builder.filters([*partial.where, *draft_query.where])
    builder.having(
        [*partial.having, *(row for row in draft_query.having if row not in partial.having)]
    )
    builder.time(_merged_time(draft_query.time, partial.time))
    builder.orders([*partial.order_by, *orders])
    if partial.limit is not None:
        builder.limit(partial.limit)
    if partial.routes:
        builder.routes(partial.routes)
    if partial.observation_scope:
        builder.observation_scope(partial.observation_scope)
    return builder.build()


def _merged_time(draft: Time | None, caller: Time | None) -> Time | None:
    """The caller's time fields over the draft's; the caller's window replaces the draft's."""

    if caller is None or draft is None:
        return caller or draft
    merged = Time(
        temporal_role=caller.temporal_role or draft.temporal_role,
        grain=caller.grain or draft.grain,
        fill=caller.fill or draft.fill,
    )
    return merged.with_window(caller.window or draft.window)


def _without_caller_selects(
    config: Any, generated: tuple[Select, ...], caller: tuple[Select, ...]
) -> tuple[list[Select], dict[str, str]]:
    """The generated selects that compute none of the caller's, and the renames of the generated
    order_by entries: the caller's alias names a column both compute. A generated select of a
    caller's column name gives way too."""

    keys = {_select_key(config, item): item for item in caller}
    names = {item.alias for item in caller}
    kept: list[Select] = []
    renamed: dict[str, str] = {}
    for item in generated:
        mine = keys.get(_select_key(config, item))
        if mine is not None:
            renamed[item.alias] = mine.alias
        elif item.alias not in names:
            kept.append(item)
    return kept, renamed


def _select_key(config: Any, item: Select) -> str:
    """What a select item computes: a measure at its package aggregation is its id."""

    expression = item.expression
    if item.ref and item.aggregation:
        measure = next((row for row in config.measures if row.id == item.ref), None)
        if measure is not None and item.aggregation == measure.default_aggregation:
            expression = item.ref
    return json.dumps(expression, sort_keys=True, default=str)


def _append_unique_dicts(
    existing: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = list(existing)
    seen = {json.dumps(row, sort_keys=True, default=str) for row in out}
    for row in additions:
        key = json.dumps(row, sort_keys=True, default=str)
        if key in seen:
            continue
        out.append(row)
        seen.add(key)
    return out


def _validate_query(runtime: Any, query: Call, partial_query: Call | None = None) -> dict[str, Any]:
    """Validate a draft as the call the plan hands back, on the request's clock and identity.

    Returns ``{"ok": bool, "errors": [...], "warnings": [...], "recovery_hints": [...]}``
    — a small projection of the full validate response that plan needs.
    Runs inline so plan's status field is trustworthy. If
    validate raises (an unexpected runtime error rather than a
    structured failure), we return ``ok=False`` with the exception
    rendered as an error so the agent still gets a structured signal.
    """

    # Cheap structural pre-check — skip the full validate (which
    # compiles the call) when the draft is obviously malformed.
    structural = _structural_precheck(query)
    if structural is not None:
        return {
            "ok": False,
            "errors": [structural],
            "recovery_hints": [],
        }

    context = dict(time_policy_context() or {})
    now = context.pop("now", None)
    try:
        report = runtime.validate(query.to_call(), now=now, context=context or None)
    except SemanticLayerError as exc:
        return {
            "ok": False,
            "errors": [{"code": exc.code, "message": str(exc), "details": exc.details}],
            "recovery_hints": [],
        }
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure as a validation error
        return {
            "ok": False,
            "errors": [{"code": "VALIDATE_EXCEPTION", "message": str(exc)}],
            "recovery_hints": [],
        }
    return {
        "ok": bool(report.get("ok", False)),
        "errors": list(report.get("errors") or []),
        "warnings": list(report.get("warnings") or []),
        "recovery_hints": list(report.get("recovery_hints") or []),
    }


def _structural_precheck(query: Call) -> dict[str, Any] | None:
    """Return a structured error if the draft is obviously bad: it selects nothing.

    Returning ``None`` means the call is plausible and the full validator should run.
    """

    if not query.select:
        return {
            "code": "STRUCTURAL_EMPTY_SELECT",
            "message": "Pattern emitted a draft with no select columns.",
        }
    return None


# Maximum number of validation errors surfaced inline under ``why``.
# Validation can emit many errors per IR (one per offending key, one
# per missing dimension, one per impossible JOIN); a full envelope can
# bloat the low_confidence response. Trim to the top few; validating the
# draft (over MCP, ``execute`` with mode ``validate``) returns the full set.
_WHY_ERROR_BUDGET = 3


def _trim_why_errors(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Cap ``why.errors`` to ``_WHY_ERROR_BUDGET`` entries.

    Adds a ``truncated`` marker with the dropped count so the agent
    knows validating the draft returns more detail.
    """

    why: dict[str, Any] = {
        "code": "VALIDATION_FAILED",
        "message": "Pattern-realized IR did not pass validate.",
        "errors": [_slim_validation_error(error) for error in errors[:_WHY_ERROR_BUDGET]],
    }
    overflow = len(errors) - _WHY_ERROR_BUDGET
    if overflow > 0:
        why["truncated"] = {
            "dropped": overflow,
            "hint": (
                f"+{overflow} additional validation errors; validate best.query "
                "(over MCP, execute with mode 'validate') for the full list."
            ),
        }
    return why


def _slim_validation_error(error: dict[str, Any]) -> dict[str, Any]:
    """Keep the inline plan failure compact.

    Full validator errors can contain path analyses, relationship
    payloads, and suggested patches. ``plan`` only needs the branching
    signal; callers can forward ``best.query`` to ``validate`` for
    the complete diagnostic envelope.
    """

    out: dict[str, Any] = {}
    for key in (
        "code",
        "message",
        "severity",
        "stage",
        "path",
        "unsupported_construct",
        "why_invalid",
        "missing_metadata_or_capability",
    ):
        value = error.get(key)
        if value not in (None, "", [], {}):
            out[key] = value
    object_ids = error.get("object_ids")
    if object_ids:
        out["object_ids"] = list(object_ids)[:8]
    recovery_hints = _slim_recovery_hints(list(error.get("recovery_hints") or []))
    if recovery_hints:
        out["recovery_hints"] = recovery_hints
    return out or {"code": "VALIDATION_ERROR", "message": str(error)[:500]}


def _slim_recovery_hints(hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for hint in hints[:3]:
        if not isinstance(hint, dict):
            continue
        slim = {
            key: hint[key] for key in ("kind", "message") if hint.get(key) not in (None, "", [], {})
        }
        if slim:
            out.append(slim)
    return out
