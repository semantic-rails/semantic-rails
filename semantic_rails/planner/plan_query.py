"""The caller's partial query: merge, validate, and trim validation errors."""

from __future__ import annotations

from typing import Any

from ..ast import is_child_group, rewrite_select_shorthand
from ..errors import SemanticLayerError


def _merge_partial_query(
    config: Any,
    draft_query: dict[str, Any],
    partial_query: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge a generated draft with caller-provided Query IR.

    Partial-query preservation is a hard planner invariant: no top-level
    Query IR field supplied by the caller may silently disappear. For
    additive list fields we append generated entries after existing
    caller entries. For scalar/dict fields the caller wins, with ``time``
    merged shallowly so generated temporal roles can still fill missing
    fields. The question's values on one field form one generated filter
    for one total or combined ranking, without adding grouping. Caller
    rows stay as written, with only string predicate fields stripped.
    """

    partial = dict(partial_query or {})
    # Context is validation authority, not portable Query IR. _validate_query
    # receives it separately so every draft remains governed without asking
    # callers to replay trusted claims in a later compile/execute request.
    partial.pop("policy_context", None)
    partial.pop("request_context", None)
    partial.pop("request_id", None)
    merged = dict(draft_query or {})
    if partial.get("select"):
        merged, partial["select"] = _without_caller_selects(config, merged, partial["select"])
    for key, value in partial.items():
        if value in (None, "", [], {}):
            continue
        if key == "where":
            value = [
                {**row, "field": row["field"].strip()}
                if isinstance(row, dict) and isinstance(row.get("field"), str)
                else row
                for row in list(value or [])
            ]
        if key in {"select", "where", "metric_filters", "order_by"}:
            merged[key] = _append_unique_dicts(list(value or []), list(merged.get(key, []) or []))
        elif key == "group_by":
            merged[key] = list(
                dict.fromkeys([*list(value or []), *list(merged.get(key, []) or [])])
            )
        elif key == "time" and isinstance(value, dict):
            generated = dict(merged.get("time", {}) or {})
            merged[key] = {**generated, **value}
        else:
            merged[key] = value
    return merged


def _checked_partial_query(partial_query: dict[str, Any] | None) -> dict[str, Any] | None:
    """The caller's partial query with ``group_by`` as dimension ids.

    The merge reads its list fields as lists, so a shape it can't read
    (``group_by: [["dimension.x"]]``) fails here as the validation error the
    engine gives, not as an internal error.
    """

    if not partial_query:
        return partial_query
    for key in ("select", "where", "metric_filters", "order_by", "group_by"):
        value = partial_query.get(key)
        if value in (None, "", [], {}) or isinstance(value, list):
            continue
        raise _invalid_partial(
            f"query.{key}",
            type(value).__name__,
            f"query.{key} must be a list; got {type(value).__name__}.",
            f"Pass query.{key} as a list.",
        )
    group_by: list[str] = []
    for index, item in enumerate(partial_query.get("group_by") or []):
        dimension = item.get("dimension", item.get("field")) if isinstance(item, dict) else item
        if not isinstance(dimension, str):
            raise _invalid_partial(
                f"query.group_by[{index}]",
                type(item).__name__,
                f"query.group_by[{index}] must be a dimension id string; "
                f"got {type(item).__name__}.",
                "Pass group_by as a flat list of dimension ids, e.g. "
                '["dimension.store_name"], not [["dimension.store_name"]].',
            )
        group_by.append(dimension)
    checked = {**partial_query, "group_by": group_by} if group_by else partial_query
    # The same rewrite validate, compile and execute apply, so plan accepts what they accept.
    return rewrite_select_shorthand(checked, partial=True)[0]


def _invalid_partial(path: str, received: str, message: str, hint: str) -> SemanticLayerError:
    return SemanticLayerError(
        "INVALID_QUERY",
        message,
        details={
            "path": path,
            "received_type": received,
            "recovery_hints": [{"kind": "fix_query_shape", "message": hint}],
        },
    )


def _without_caller_selects(
    config: Any, query: dict[str, Any], caller_select: list[Any]
) -> tuple[dict[str, Any], list[Any]]:
    """Drop generated select items that compute one of the caller's.

    The caller's alias names the column: the generated ``order_by`` follows
    it, and a caller item without an alias takes the generated one.
    """

    caller = list(caller_select)
    positions = {_select_key(config, item): index for index, item in enumerate(caller)}
    kept: list[Any] = []
    renamed: dict[str, str] = {}
    for item in query.get("select") or []:
        index = positions.get(_select_key(config, item))
        if index is None:
            kept.append(item)
            continue
        alias, mine = item.get("as") if isinstance(item, dict) else None, caller[index]
        if alias and isinstance(mine, dict) and mine.get("as"):
            renamed[str(alias)] = str(mine["as"])
        elif alias and isinstance(mine, dict):
            caller[index] = {**mine, "as": alias}
    out = {**query, "select": kept}
    if renamed and query.get("order_by"):
        out["order_by"] = [
            {**row, "field": renamed.get(str(row.get("field")), row.get("field"))}
            if isinstance(row, dict)
            else row
            for row in query["order_by"]
        ]
    return out, caller


def _select_key(config: Any, item: Any) -> str:
    """What a select item computes: its expression, with a measure's default
    aggregation spelled out or left implicit alike."""

    import json

    expression = item
    if isinstance(item, dict):
        expression = item.get("expression", {k: v for k, v in item.items() if k != "as"})
    if isinstance(expression, dict) and set(expression) <= {"measure", "aggregation"}:
        measure = next(
            (row for row in config.measures if row.id == expression.get("measure")), None
        )
        if measure is not None:
            aggregation = expression.get("aggregation") or measure.default_aggregation
            expression = {"measure": measure.id, "aggregation": aggregation}
    return json.dumps(expression, sort_keys=True, default=str)


def _append_unique_dicts(
    existing: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    import json

    out = list(existing)
    seen = {json.dumps(row, sort_keys=True, default=str) for row in out}
    for row in additions:
        key = json.dumps(row, sort_keys=True, default=str)
        if key in seen:
            continue
        out.append(row)
        seen.add(key)
    return out


def _where_filters(query: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list((query or {}).get("where") or []):
        if not isinstance(row, dict):
            continue
        if is_child_group(row):
            out.append(
                {
                    "child": str(row.get("child", "")),
                    "match": str(row.get("match", "")),
                    "where": _where_filters({"where": list(row.get("where") or [])}),
                }
            )
            continue
        field = row.get("field") or row.get("dimension")
        if not field:
            continue
        out.append(
            {
                "field": str(field),
                "op": str(row.get("op", "") or ""),
                "value": row.get("value"),
            }
        )
    return out


def _validate_query(
    runtime: Any,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None,
) -> dict[str, Any]:
    """Call ``runtime.validate`` against a pattern draft.

    Returns ``{"ok": bool, "errors": [...], "recovery_hints": [...]}``
    — a small projection of the full validate response that plan needs.
    Runs inline so plan's status field is trustworthy. If
    validate raises (an unexpected runtime error rather than a
    structured failure), we return ``ok=False`` with the exception
    rendered as an error so the agent still gets a structured signal.
    """

    # Cheap structural pre-check — skip the full validate (which
    # compiles the IR) when the draft is obviously malformed. Catches
    # the common pattern bugs without paying compile cost. The full
    # validator still runs on draft IRs that pass these gates.
    structural = _structural_precheck(query)
    if structural is not None:
        return {
            "ok": False,
            "errors": [structural],
            "recovery_hints": [],
        }

    payload = dict(query)
    if partial_query and partial_query.get("policy_context"):
        payload["policy_context"] = partial_query["policy_context"]
    try:
        report = runtime.validate(payload)
    except Exception as exc:  # noqa: BLE001 — surface any unexpected failure as a validation error
        return {
            "ok": False,
            "errors": [{"code": "VALIDATE_EXCEPTION", "message": str(exc)}],
            "recovery_hints": [],
        }
    return {
        "ok": bool(report.get("ok", False)),
        "errors": list(report.get("errors") or []),
        "recovery_hints": list(report.get("recovery_hints") or []),
    }


def _structural_precheck(query: dict[str, Any]) -> dict[str, Any] | None:
    """Return a structured error if the draft IR is obviously bad.

    Catches the cheap failure modes without paying ``runtime.validate``'s
    compile cost — a missing ``select``, empty selects, a select item
    without an ``expression``. Returning ``None`` means the IR is
    plausible and the full validator should run.

    Deliberately narrow: only catches structural shape errors that no
    legitimate pattern would produce. Catalog-membership checks
    (does this measure id exist?) stay in the full validator where
    they have config access.
    """

    if not isinstance(query, dict):
        return {
            "code": "STRUCTURAL_NOT_A_QUERY",
            "message": "Pattern emitted something that is not a Query IR dict.",
        }
    select = query.get("select")
    if not isinstance(select, list) or not select:
        return {
            "code": "STRUCTURAL_EMPTY_SELECT",
            "message": "Pattern emitted a draft with no select columns.",
        }
    for index, item in enumerate(select):
        if not isinstance(item, dict):
            return {
                "code": "STRUCTURAL_BAD_SELECT_ITEM",
                "message": f"select[{index}] is not a dict.",
            }
        if not item.get("expression"):
            return {
                "code": "STRUCTURAL_MISSING_EXPRESSION",
                "message": f"select[{index}] missing expression.",
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
                f"+{overflow} additional validation errors; validate best.query_ir "
                "(over MCP, execute with mode 'validate') for the full list."
            ),
        }
    return why


def _slim_validation_error(error: dict[str, Any]) -> dict[str, Any]:
    """Keep the inline plan failure compact.

    Full validator errors can contain path analyses, relationship
    payloads, and suggested patches. ``plan`` only needs the branching
    signal; callers can forward ``best.query_ir`` to ``validate`` for
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
