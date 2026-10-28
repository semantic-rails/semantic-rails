"""Error enrichment and structured-issue assembly.

Owns the canonical ``semantic_issue`` shape used everywhere in HTTP /
MCP / CLI responses, the :func:`recovery_hints_for_error` map that
populates ``recovery_hints`` per error code, and
:func:`enrich_object_not_found` which adds ``closest_matches`` to
typo'd object id errors. Also exposes
:func:`relationship_contract_payload` and :func:`provenance_summary`
used by ``explain`` output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from difflib import get_close_matches
from typing import Any

from .errors import SemanticLayerError
from .schema import PackageConfig, RelationshipConfig


def _shift_iso_date_backward(start: str, lookback: dict[str, Any]) -> str:
    """Compute ``start - lookback`` for an ISO ``YYYY-MM-DD`` date and a
    ``{unit, value}`` lookback. Returns the shifted ISO string, or ``""``
    if either input is malformed. Used by the
    ``WINDOWED_TIME_FILTER_UNSUPPORTED`` recovery hint to produce a
    concrete ``suggested_start``.
    """
    if not start or not isinstance(lookback, dict):
        return ""
    unit = str(lookback.get("unit", "") or "").lower()
    try:
        value = int(lookback.get("value", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0 or unit not in {"day", "week", "month", "quarter", "year"}:
        return ""
    try:
        anchor = date.fromisoformat(start[:10])
    except ValueError:
        return ""
    if unit == "day":
        shifted = anchor - timedelta(days=value)
    elif unit == "week":
        shifted = anchor - timedelta(weeks=value)
    elif unit == "month":
        total_months = anchor.year * 12 + (anchor.month - 1) - value
        shifted = date(total_months // 12, (total_months % 12) + 1, min(anchor.day, 28))
    elif unit == "quarter":
        total_months = anchor.year * 12 + (anchor.month - 1) - (value * 3)
        shifted = date(total_months // 12, (total_months % 12) + 1, min(anchor.day, 28))
    else:  # year
        shifted = date(anchor.year - value, anchor.month, min(anchor.day, 28))
    return shifted.isoformat()


def relationship_proof_class(rel: RelationshipConfig) -> str:
    if rel.temporal_validity:
        return "temporal_validity"
    target_key_type = str(rel.target_key_type or "").strip().lower()
    join_semantics = str(rel.join_semantics or "").strip().lower()
    card = str(rel.cardinality or "").strip().upper()
    if target_key_type == "custom":
        return "custom"
    if target_key_type == "unique":
        return "fk_to_unique"
    if join_semantics == "one_to_one" or card == "1:1":
        return "pk_to_pk"
    if target_key_type in {"", "primary"}:
        return "fk_to_pk"
    return "unsupported"


def relationship_contract_payload(rel: RelationshipConfig) -> dict[str, Any]:
    return {
        "relationship_id": rel.id,
        "proof_class": relationship_proof_class(rel),
        "join_semantics": rel.join_semantics or _infer_join_semantics(rel),
        "safety": rel.safety or "safe",
        "target_key_type": rel.target_key_type or "primary",
        "source_key_role": rel.source_key_role,
        "target_key_role": rel.target_key_role,
        "cardinality": rel.cardinality,
        "temporal_validity": bool(rel.temporal_validity),
    }


_EMPTY: tuple[Any, ...] = (None, "", [], {})


def lean_issue(issue: Any) -> Any:
    """An error or warning object as a caller reads it: no empty fields, its own or its
    recovery hints' (``code`` and ``message`` always stay). ``details`` keep their values: an
    empty value there is what the caller sent."""
    if not isinstance(issue, dict):
        return issue
    lean = {
        key: value
        for key, value in issue.items()
        if key in ("code", "message") or not _is_empty(value)
    }
    if isinstance(lean.get("recovery_hints"), list):
        lean["recovery_hints"] = [lean_issue(hint) for hint in lean["recovery_hints"]]
    return lean


def _is_empty(value: Any) -> bool:
    return isinstance(value, (str, list, dict, type(None))) and value in _EMPTY


def semantic_issue(
    *,
    code: str,
    message: str,
    severity: str,
    stage: str,
    details: dict[str, Any] | None = None,
    object_ids: Iterable[str] | None = None,
    path: str = "",
    recovery_hints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detail_payload = dict(details or {})
    hint_payloads = list(recovery_hints or [])
    if detail_payload.get("recovery_hints") == hint_payloads:
        detail_payload.pop("recovery_hints")
    closest_valid_query = dict(detail_payload.get("closest_valid_query", {}) or {})
    if not closest_valid_query:
        for hint in hint_payloads:
            if not isinstance(hint, dict):
                continue
            closest_valid_query = dict(hint.get("closest_valid_query", {}) or {})
            if closest_valid_query:
                break
    issue = {
        "code": code,
        "message": message,
        "severity": severity,
        "stage": stage,
        "details": detail_payload,
        "object_ids": list(
            dict.fromkeys(str(item) for item in (object_ids or []) if str(item).strip())
        ),
        "path": path,
        "recovery_hints": hint_payloads,
    }
    # Refusal-shaped envelope fields belong to errors. An advisory warning
    # (e.g. SEMANTIC_CAVEAT_APPLIED on a successful query) that says
    # "why_invalid" reads as a failed query to humans and agents alike, so
    # warnings only carry these keys when the producer set them explicitly.
    if severity == "error":
        issue.update(
            {
                "unsupported_construct": detail_payload.get("unsupported_construct", code),
                "why_invalid": detail_payload.get("why_invalid", message),
                "missing_metadata_or_capability": detail_payload.get(
                    "missing_metadata_or_capability", ""
                ),
                "closest_valid_query": closest_valid_query,
                "suggested_yaml_change": detail_payload.get("suggested_yaml_change", ""),
                "suggested_query_ir_change": detail_payload.get("suggested_query_ir_change", ""),
            }
        )
    else:
        for key in (
            "unsupported_construct",
            "why_invalid",
            "missing_metadata_or_capability",
            "closest_valid_query",
            "suggested_yaml_change",
            "suggested_query_ir_change",
        ):
            if key in detail_payload:
                issue[key] = detail_payload[key]
    return issue


_FILTER_VALUE_FORMS = {
    "boolean": "true or false, unquoted (YAML `value: true`)",
    "string": "text; in YAML, quote text that looks like a number or boolean (`value: 'true'`)",
    "integer": "a whole number",
    "number": "a number",
    "date": "an ISO date, such as 2017-09-04",
    "timestamp": "an ISO date or date-time, such as 2017-09-04T12:00:00",
    "datetime": "an ISO date or date-time, such as 2017-09-04T12:00:00",
}


def _time_axis_hints(details: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``use_time_grain`` hint for a refused calendar-date group_by, from the
    ``time_axis_recovery`` the compiler attached; none without a role that would answer."""
    time_axis = dict(details.get("time_axis_recovery", {}) or {})
    grain = str(time_axis.get("grain", "") or "")
    role = str(time_axis.get("temporal_role", "") or "")
    if not role:
        return []
    example = {"temporal_role": role, "grain": grain or "<grain>"}
    hint = {
        "kind": "use_time_grain",
        "message": (
            "Group by time via time.grain instead of calendar-date dimensions: "
            f"drop '{time_axis.get('calendar_dimension', '')}' from group_by and set "
            f"a time block such as {example!r}."
        ),
        "time": example,
    }
    closest_query = dict(time_axis.get("closest_valid_query", {}) or {})
    if closest_query:
        hint["closest_valid_query"] = closest_query
    return [hint]


def recovery_hints_for_error(
    code: str, details: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    details = dict(details or {})
    if code == "INVALID_EXPRESSION":
        position = str(details.get("expression_position", "") or "")
        missing = str(details.get("missing_key", "") or "")
        received = list(details.get("received_keys", []) or [])
        hints: list[dict[str, Any]] = []
        if position == "order_by":
            hints.append(
                {
                    "kind": "fix_order_by_shape",
                    "message": (
                        'Each order_by entry is {"field": "<select alias, group_by id or period>", '
                        '"direction": "asc"|"desc"} — not a select-style expression.'
                    ),
                    "shape": {"field": "<alias>", "direction": "asc"},
                    "missing_key": missing,
                    "received_keys": received,
                }
            )
        elif position == "metric_filters":
            hints.append(
                {
                    "kind": "fix_having_shape",
                    "message": (
                        'Each having entry is {"field": "<select alias, measure or metric id>", '
                        '"op": "gt", "value": ...}; to keep entities by an aggregate, write an '
                        'entity filter in where: {"entity", "having", "over"}.'
                    ),
                    "shape": {"field": "<alias>", "op": "gt", "value": 0},
                    "missing_key": missing,
                    "received_keys": received,
                }
            )
        else:
            hints.append(
                {
                    "kind": "fix_expression_shape",
                    "message": (
                        f"{position or 'expression'} entry is missing the required {missing!r} key. "
                        "Send select items as a measure or metric id or {expression, as}, "
                        "group_by as dimension ids, and where entries as {field, op, value}."
                    ),
                    "missing_key": missing,
                    "received_keys": received,
                }
            )
        return hints
    if code == "INVALID_EXPRESSION_AST":
        kind = str(details.get("expression_kind", "") or "")
        position = str(details.get("expression_position", "") or "")
        if position == "where" and "op" in details and "allowed" in details:
            return [
                {
                    "kind": "use_supported_filter_operator",
                    "message": (
                        f"Choose a where operator: {', '.join(details['allowed'])}. "
                        "To test for no value, use op 'missing' with value true or false."
                    ),
                    "allowed": list(details["allowed"]),
                }
            ]
        # ``received`` is sometimes a dict (the full malformed expr) and
        # sometimes a scalar (e.g. the bad p value). Coerce defensively
        # so a downstream hint loop doesn't crash on a float.
        received_raw = details.get("received")
        received_dict: dict[str, Any] = received_raw if isinstance(received_raw, dict) else {}
        closest = list(details.get("closest_matches", []) or [])
        if kind == "prior_period":
            hints = []
            received_input = received_dict.get("input")
            received_measure = str(
                received_dict.get("measure")
                or (received_input if isinstance(received_input, str) else "")
                or (received_input.get("measure") if isinstance(received_input, dict) else "")
                or ""
            )
            if "unknown_offset_keys" in details:
                hints.append(
                    {
                        "kind": "fix_prior_period_offset_shape",
                        "message": (
                            "Prior-period 'offset' must be exactly "
                            "{unit, value}. Replace any aliases (e.g. 'n', "
                            "'count', 'period') with 'value' and keep 'unit'."
                        ),
                        "canonical_offset": {"unit": "<day|week|month|quarter|year>", "value": 1},
                        "received_offset_keys": list(received_dict.get("offset", {}).keys())
                        if isinstance(received_dict.get("offset"), dict)
                        else [],
                        "unknown_offset_keys": list(details.get("unknown_offset_keys", []) or []),
                    }
                )
            hints.append(
                {
                    "kind": "use_prior_period_ir_shape",
                    "message": "prior_period takes an input and an offset {unit, value}.",
                    "shape": {
                        "kind": "prior_period",
                        "input": received_measure or "<measure or metric id>",
                        "offset": {"unit": "<day|week|month|quarter|year>", "value": 1},
                    },
                }
            )
            if closest:
                hints.insert(
                    0,
                    {
                        "kind": "use_authored_prior_period_metric",
                        "message": (
                            "An authored metric already defines this period shift over the same "
                            'measure. Select its id ("metric.<id>"); no inline shift needed.'
                        ),
                        "closest_matches": closest,
                    },
                )
            return hints
        if details.get("recovery_hints"):
            return list(details["recovery_hints"])
        from .query_call import KIND_REQUIRED

        where = f"the expression at {details['path']}" if details.get("path") else "the expression"
        if kind in KIND_REQUIRED:
            shape = {"kind": kind, **dict.fromkeys(KIND_REQUIRED[kind], "...")}
            message = f"Write {where} as {json.dumps(shape)}, with each key's value as the query schema lists it."
        else:
            message = (
                f"Write {where} as a measure or metric id, or an object with a kind the query "
                "schema lists."
            )
        return [{"kind": "fix_expression_shape", "message": message}]
    if code == "PATH_NOT_FOUND" and details.get("reason") == "excluded_by_decision":
        return [
            {
                "kind": "follow_recorded_routes",
                "message": str(details.get("hint", "")),
                "rows": list(details.get("rows", []) or []),
            }
        ]
    if code == "PATH_NOT_FOUND" and details.get("unknown_entity"):
        unknown_entity = _echo(str(details["unknown_entity"]))
        matches = list(details.get("closest_matches", []) or [])
        return [
            {
                "kind": "fix_entity_id",
                "audience": "caller",
                "message": f"'{unknown_entity}' is not an entity of this package"
                + (f"; did you mean {matches}?" if matches else "; discover lists entity ids."),
                "closest_matches": matches,
            }
        ]
    if code == "PATH_NOT_FOUND":
        start = str(details.get("start", "") or "")
        target = str(details.get("target", "") or "")
        reachable = list(details.get("reachable_targets", []) or [])
        items = list(details.get("unreachable_items", []) or [])
        row_grain = bool(details.get("target_row_grain"))
        hints = []
        if not reachable:
            blocked = {
                "direction": f"by direction, each relationship of '{start}' leads only to it",
                "hop_limit": "the hop limit stops every route",
                "route_choice": "the package's route choices drop every route",
            }.get(
                str(details.get("blocked_by", "")),
                f"no relationship joins '{start}' to another entity",
            )
            message = f"No entity is reachable from '{start}': {blocked}."
            if details.get("bridge_entities"):
                message += (
                    f" Entities related to both '{start}' and '{target}': "
                    f"{', '.join(repr(entity) for entity in details['bridge_entities'])}."
                )
            if details.get("shared_key_columns"):
                message += f" Key columns they share: {', '.join(details['shared_key_columns'])}."
            dropped = f" ({', '.join(items)})" if items else ""
            message += (
                f" Split the query, or drop what needs '{target}'{dropped}; or group by a "
                f"dimension on '{start}' itself."
            )
            hints.append(
                {
                    "kind": "isolated_source_entity",
                    "message": message,
                    "start": start,
                    "unreachable_target": target,
                }
            )
        else:
            hints.append(
                {
                    "kind": "use_reachable_target",
                    "message": (
                        f"No declared path from '{start}' to '{target}'. "
                        f"Pick a group_by dimension whose entity is reachable from '{start}'."
                    ),
                    "start": start,
                    "unreachable_target": target,
                    "reachable_targets": reachable[:10],
                }
            )
            hints.append(
                {
                    "kind": "switch_group_by",
                    "message": (f"Entities reachable from '{start}': {reachable[:10]}."),
                    "reachable_targets": reachable[:10],
                }
            )
        # Concrete dimension ids the agent can paste into ``group_by``
        # without a follow-up ``inspect`` call. Same hint fires for both
        # the isolated-source and reachable-target branches because in
        # either case the start entity may have local dimensions worth
        # naming.
        compatible_dims = list(details.get("compatible_group_by_dimensions", []) or [])
        closest = list(details.get("closest_dimensions", []) or [])
        if compatible_dims:
            message = f"Dimensions reachable from '{start}' (cap 15): {compatible_dims}."
            if closest:
                message = (
                    f"Closest to what was asked, reachable from '{start}': {closest}. Each is a "
                    "different dimension, so ask the user before grouping by one. " + message
                )
            hints.append(
                {
                    "kind": "switch_group_by_dimension",
                    "message": message,
                    "compatible_group_by_dimensions": compatible_dims,
                    **({"closest_dimensions": closest} if closest else {}),
                }
            )
        stand_ins = list(details.get("closest_measures", []) or [])
        if stand_ins:
            hints.append(
                {
                    "kind": "switch_measure",
                    "message": (
                        f"Measures that do reach '{target}': {stand_ins}. Each is a different "
                        "measure, so ask the user before answering with one."
                    ),
                    "closest_measures": stand_ins,
                }
            )
        if items and reachable:
            hints.append(
                {
                    "kind": "drop_or_split",
                    "message": (
                        f"{details.get('anchor') or ''} Drop {', '.join(items)}, or split the "
                        f"query: ask for {', '.join(items)} separately."
                    ).strip(),
                    "unreachable_items": items,
                }
            )
        if row_grain:
            hints.append(
                {
                    "kind": "row_grain_target",
                    "message": (
                        f"'{target}' is a row-grain model (no key): nothing can look up one of "
                        "its rows, so no relationship leads into it. Group its own measures by "
                        "its columns."
                    ),
                    "unreachable_target": target,
                }
            )
            hints.append(
                {
                    "kind": "declare_key",
                    "message": (
                        f"For the package author: declare a key on '{target}' if its rows have "
                        "one, so a relationship can lead into it."
                    ),
                    "unreachable_target": target,
                }
            )
        else:
            from .path_gap import missing_link_fix

            fix = missing_link_fix(dict(details.get("missing_link") or {}))
            hints.append(
                {
                    "kind": "author_relationship",
                    "message": "For the package author: "
                    + (
                        fix[:1].lower() + fix[1:]
                        or (
                            f"if '{target}' should be reachable, add a graph.relationships row "
                            f"connecting it to an entity '{start}' reaches."
                        )
                    ),
                    "start": start,
                    "unreachable_target": target,
                }
            )
        for hint in hints:
            author = hint["kind"] in {"author_relationship", "declare_key"}
            hint["audience"] = "author" if author else "caller"
        return hints
    if code == "INVALID_EXPRESSION_KEY":
        unknown = list(details.get("unknown_keys", []) or [])
        valid = list(details.get("valid_keys", []) or [])
        kind = str(details.get("expression_kind", "") or "")
        closest = list(details.get("closest_matches", []) or [])
        aggregation = str(details.get("aggregation", "") or "")
        hints = [
            {
                "kind": "remove_or_rename_expression_key",
                "message": (
                    f"Expression with kind={kind!r} does not support keys: {unknown}. "
                    "Drop them or rename to a supported key."
                ),
                "unknown_keys": unknown,
                "valid_keys": valid,
                "closest_matches": closest,
            }
        ]
        # Disclose the inner ``parameters`` shape when (a) the agent's
        # typo'd a key that hash-matches ``parameters`` (e.g.
        # ``aggregation_params``, ``params``), or (b) ``parameters`` is
        # in the valid set for this kind. Reviewer's complaint: the
        # error renamed the key but didn't reveal that ``parameters``
        # is ``{p: float}`` for percentile — two round-trips required.
        from .expressions import parameter_schema_for_aggregation

        schema = parameter_schema_for_aggregation(aggregation) if aggregation else {}
        if schema and (
            "parameters" in valid or any("parameter" in str(name).lower() for name in unknown)
        ):
            hints.append(
                {
                    "kind": "disclose_parameters_schema",
                    "message": (
                        f"The ``parameters`` key for aggregation={aggregation!r} takes: {schema}."
                    ),
                    "aggregation": aggregation,
                    "parameters_schema": schema,
                }
            )
        return hints
    if code == "UNSUPPORTED_AGGREGATION":
        aggregation = str(details.get("aggregation", "") or "")
        if "allowed" in details:
            allowed = list(details["allowed"])
            default = str(details.get("default_aggregation", "") or "")
            default_hint = (
                f"; omit `aggregation` for the default ({default})" if default in allowed else ""
            )
            return [
                {
                    "kind": "use_supported_aggregation",
                    "message": (
                        f"Use one of this measure's aggregations: "
                        f"{', '.join(allowed)}{default_hint}."
                        if allowed
                        else "This measure allows no aggregations; inspect another measure."
                    ),
                    "aggregation_received": aggregation,
                    "allowed": allowed,
                }
            ]
        from .expressions import parameter_schema_for_aggregation

        schema = parameter_schema_for_aggregation(aggregation) if aggregation else {}
        hints = []
        if schema:
            hints.append(
                {
                    "kind": "disclose_parameters_schema",
                    "message": (
                        f"Aggregation {aggregation!r} requires "
                        f"``parameters`` of shape: {schema}. Add or "
                        "correct the inner ``parameters`` block."
                    ),
                    "aggregation": aggregation,
                    "parameters_schema": schema,
                    "parameters_received": dict(details.get("parameters_received", {}) or {}),
                }
            )
        else:
            hints.append(
                {
                    "kind": "use_supported_aggregation",
                    "message": (
                        "Inspect the measure to find its allowed aggregations, "
                        "then choose one of those values."
                    ),
                    "aggregation_received": aggregation,
                }
            )
        return hints
    if code == "DUPLICATE_OUTPUT_ALIAS":
        alias = str(details.get("alias", "") or "")
        return [
            {
                "kind": "rename_output",
                "message": f'Give the repeated select item its own name with "as" ({alias!r} is taken).',
                "alias": alias,
            }
        ]
    if code == "INVALID_TEMPORAL_ROLE":
        if details.get("available_temporal_roles") == []:
            return [
                {
                    "kind": "remove_time_or_declare_role",
                    "message": "Ask without time, or declare a times: entry on a model before time analysis.",
                }
            ]
        compatible = list(details.get("compatible", []) or [])
        return [
            {
                "kind": "select_temporal_role",
                "message": "Choose a declared temporal role for the query.",
                "compatible_temporal_roles": compatible,
            }
        ]
    if code == "INCOMPATIBLE_TEMPORAL_ROLE":
        compatible = list(details.get("compatible", []) or [])
        if not compatible and details.get("source") == "aggregate_if":
            return []  # The message says what to do; there is no measure to point a hint at.
        if not compatible and details.get("measure") and details.get("requested"):
            return [
                {
                    "kind": "declare_measure_time_role",
                    "message": (
                        f"'{details['measure']}' has no time role. Mark one of its model's "
                        "`times:` entries `default: true`, or list `times:` on the measure, "
                        "then query it by that role. Or drop `time` from the query."
                    ),
                    "measure": details["measure"],
                    "requested_temporal_role": details["requested"],
                }
            ]
        return [
            {
                "kind": "select_compatible_temporal_role",
                "message": "Use a temporal role compatible with the selected measure or metric.",
                "compatible_temporal_roles": compatible,
            }
        ]
    if code == "INVALID_ANCHOR_ROLE":
        anchor_role = str(details.get("anchor_role", "") or "")
        available = list(details.get("available_temporal_roles", []) or [])
        feature_status = str(details.get("feature_status", "") or "")
        hints = []
        if feature_status == "ir_contract_only":
            # Stub-state error — IR validates but SQL doesn't compile.
            # Steer the caller at the available authored escape hatches.
            hints.append(
                {
                    "kind": "feature_pending_sql_lowering",
                    "message": (
                        "scoped_aggregate.anchor + scoped_aggregate.window "
                        "parse and validate against the published IR "
                        "contract, but the SQL lowering hasn't shipped "
                        "yet. Use an authored anchor-offset column and "
                        "a measure filter instead."
                    ),
                    "feature_status": feature_status,
                    "anchor": dict(details.get("anchor", {}) or {}),
                    "window": dict(details.get("window", {}) or {}),
                }
            )
            hints.append(
                {
                    "kind": "use_anchor_offset_column",
                    "message": (
                        "If you need the result today, expose the "
                        "offset from the anchor as a column on the "
                        "measure's model (for example days since the "
                        "first order) and filter the measure on it. A "
                        "metric recipe with the same anchor + window "
                        "is refused the same way, so it is not a "
                        "workaround."
                    ),
                }
            )
            return hints
        # Unknown anchor role — list the package's roles so the agent
        # can pick a valid one without an extra ``inspect`` call.
        hints.append(
            {
                "kind": "use_valid_anchor_role",
                "message": (
                    f"Anchor temporal role {anchor_role!r} isn't defined. "
                    "Pick a per-entity scalar temporal role from the "
                    "package's available roles."
                ),
                "anchor_role_received": anchor_role,
                "available_temporal_roles": available[:15],
            }
        )
        return hints
    if code == "INVALID_TEMPORAL_BINDING":
        allowed = list(details.get("allowed_temporal_roles", []) or [])
        anchor = str(details.get("anchor_temporal_role", "") or "")
        recipe_id = str(details.get("metric", "") or details.get("recipe_id", "") or "")
        hints = [
            {
                "kind": "filter_on_conversion_anchor",
                "message": (
                    f"Filter the conversion's anchor clock by setting "
                    f"`time.temporal_role` to '{anchor}'."
                    if anchor
                    else "Filter the conversion on one of its anchor-aligned temporal roles."
                ),
                "allowed_temporal_roles": allowed,
                "anchor_temporal_role": anchor,
                "metric": recipe_id,
            },
            {
                "kind": "move_filter_into_conversion_window",
                "message": (
                    "If you need to constrain by a non-anchor clock, push the "
                    "constraint into the conversion's window/match semantics "
                    "via an authored conversion metric rather than the "
                    "top-level time block."
                ),
            },
        ]
        return hints
    if code == "AMBIGUOUS_CHILD_SCOPE":
        return list(details.get("recovery_hints", []) or [])
    if code == "AMBIGUOUS_PATH":
        # A calendar-date group_by reached only through other facts: the time block first.
        return [
            *_time_axis_hints(details),
            {
                "kind": "narrow_query",
                "message": str(
                    details.get("hint")
                    or "Choose a more specific grouping, filter, or root entity to break the path ambiguity."
                ),
            },
        ]
    if code == "FANOUT_UNSAFE":
        return [
            {
                "kind": "change_breakdown",
                "message": str(
                    details.get("hint")
                    or "Use a safer breakdown or add a pre-aggregation boundary before traversing this path."
                ),
                "relationships": list(details.get("relationships", []) or []),
            }
        ]
    if code == "ROLLUP_UNSAFE":
        return list(details.get("recovery_hints", []) or [])
    if code == "CONVERSION_WINDOW_REQUIRED":
        return list(details.get("recovery_hints", []) or [])
    if code == "CONVERSION_MATCHING_MODE_REQUIRED" and details.get("expression"):
        return [
            {
                "kind": "set_conversion_matching_mode",
                "message": "Resend details.expression, or set matching_mode to another "
                "value from details.allowed_values.",
                "allowed_values": list(details.get("allowed_values", {}) or {}),
            }
        ]
    if code == "MEASURE_VALIDITY_BOUNDARY":
        return list(details.get("recovery_hints", []) or []) or [
            {
                "kind": "split_time_window",
                "message": "Run separate queries inside each declared measure-validity window.",
                "details": dict(details),
            }
        ]
    if code == "OUT_OF_SCOPE":
        return [
            {
                "kind": "route_request",
                "message": "Hand this request off to the recommended tool; the semantic layer compiles governed data queries only.",
                "recommended_handoff": str(details.get("recommended_handoff", "")),
            }
        ]
    if code == "MIXED_GRAIN_INVALID":
        # Recovery enrichment computed at raise time (see
        # compiler_parts.grain_recovery): name the concrete escape
        # routes first, generic advice last. Blind-agent evaluation
        # showed agents brute-forcing the registry when this error was
        # a dead end.
        compatible_measures = list(details.get("compatible_measures", []) or [])
        closest_measure = str(details.get("closest_compatible_measure", "") or "")
        offending_dims = list(details.get("offending_dimensions", []) or [])
        requested_measures = list(details.get("measures", []) or [])
        # When the offending dimension is a calendar-date dimension the
        # primary recovery is the time block — lead with it.
        mixed_grain_hints: list[dict[str, Any]] = _time_axis_hints(details)
        # A different measure or dimension answers a different question, so these two
        # hints name the candidates without a query to run in place of the question.
        if compatible_measures and closest_measure:
            dim_label = offending_dims[0] if offending_dims else "the requested dimension"
            mixed_grain_hints.append(
                {
                    "kind": "replace_measure",
                    "message": (
                        f"Measure '{closest_measure}' can use '{dim_label}', but it is a "
                        "different measure: use it only if it is what the question asks for."
                    ),
                    "compatible_measures": compatible_measures,
                }
            )
        compatible_dimensions = list(details.get("compatible_dimensions", []) or [])
        if compatible_dimensions:
            measure_label = requested_measures[0] if requested_measures else "the requested measure"
            mixed_grain_hints.append(
                {
                    "kind": "replace_dimension",
                    "message": (
                        f"'{measure_label}' can use these dimensions instead, which answers a "
                        "different question."
                    ),
                    "compatible_dimensions": compatible_dimensions,
                }
            )
        analysis = dict(details.get("analysis", {}) or {})
        if str(details.get("purpose", "") or "") == "group_by" and list(
            analysis.get("requires_rewrite_relationships", []) or []
        ):
            mixed_grain_hints.append(
                {
                    "kind": "requires_allocation_policy",
                    "message": (
                        "This pairs a measure with a dimension reached through a fan-out path. "
                        "The engine will not allocate measure amounts across child rows unless "
                        "the package authors an allocated measure, allocation table, or governed "
                        "allocation policy."
                    ),
                    "details": {
                        "measures": requested_measures,
                        "offending_dimensions": offending_dims,
                        "path": list(details.get("path", []) or []),
                        "requires_rewrite_relationships": list(
                            analysis.get("requires_rewrite_relationships", []) or []
                        ),
                    },
                }
            )
        mixed_grain_hints.append(
            {
                "kind": "split_query",
                "message": "Split the query by grain or use a supported rewritten shape.",
                "details": {
                    "target_entity": str(details.get("target_entity", "") or ""),
                    "purpose": str(details.get("purpose", "") or ""),
                    "path": list(details.get("path", []) or []),
                },
            }
        )
        return mixed_grain_hints
    if code == "REWRITE_NOT_SUPPORTED":
        return [
            {
                "kind": "legal_alternative",
                "message": "Try a simpler query shape or remove the unsupported rewrite requirement.",
                "details": dict(details),
            }
        ]
    if code.startswith("PREDICATE_"):
        return [
            {
                "kind": "adjust_predicate_scope",
                "message": "Change the predicate entity, time grain, or grouped context to a safe combination.",
                "details": dict(details),
            }
        ]
    if code == "POLICY_DENIED":
        if details.get("withheld_objects"):
            return [
                {
                    "kind": "rank_by_withheld_values",
                    "message": str(details.get("accepted_shape", "")),
                    "details": dict(details),
                }
            ]
        if details.get("recovery_hints"):
            return list(details["recovery_hints"])
        return [
            {
                "kind": "remove_denied_object",
                "message": "Remove or replace the denied semantic object.",
                "details": dict(details),
            }
        ]
    if code == "OBJECT_NOT_FOUND":
        suggestions = list(details.get("closest_matches", []) or [])
        # Derive a discover search term from the missing-id tail (the
        # last segment after the namespace prefix). Used as a handoff
        # when no fuzzy match meets the similarity floor — better
        # to send the agent to discover than to return three irrelevant
        # matches (the reviewer's complaint).
        missing = (
            str(details.get("object_id", "") or "")
            or str(details.get("id", "") or "")
            or str(details.get("measure", "") or "")
            or str(details.get("dimension", "") or "")
            or str(details.get("metric", "") or "")
        )
        discover_term = ""
        if missing:
            tail = str(missing).split(".")[-1]
            discover_term = tail.replace("_", " ").strip()
        hints = [
            {
                "kind": "inspect_or_discover",
                "message": "Resolve the object ID through discover or inspect before querying.",
                "closest_matches": suggestions,
                "details": dict(details),
            }
        ]
        if not suggestions and discover_term:
            hints.append(
                {
                    "kind": "call_discover_to_locate",
                    "message": (
                        "No close matches in the catalog. Call discover with "
                        f"terms={discover_term!r} to find candidates by topic, "
                        "or browse the full catalog (over MCP, discover with empty terms)."
                    ),
                    "recommended_tool": "discover",
                    "recommended_terms": discover_term,
                }
            )
        return hints
    if code == "WINDOWED_TIME_FILTER_UNSUPPORTED":
        # An authored filter or policy is not a caller-editable time boundary.
        if details.get("filter_source") in {"measure", "policy"}:
            return []
        if details.get("comparison_beside_window"):
            series = dict(details.get("time") or {})
            return [
                {
                    "kind": "ask_for_a_series",
                    "message": (
                        "Ask for the series: keep time.grain and leave out the window "
                        "(last, this, start and end), then read the periods you need."
                    ),
                    "patch": {"replace": {"time": series}},
                }
            ]
        # The error message already says what to do — agents need a
        # structured patch they can apply without parsing English.
        # Two patches: ``drop_time_start`` (remove the boundary entirely)
        # and ``widen_time_window`` (subtract the lookback from
        # query.time.start). Round-six reordering: drop is listed FIRST
        # because it's always idempotent — widen by exactly the lookback
        # can still fall short when the window function needs at least
        # one full prior bucket (e.g. prior_period at month grain needs
        # the bucket BEFORE the widened start). Agents that grab the
        # first hint should get the safe path.
        # A where filter on a date or calendar dimension (``where_path``) cuts the same rows.
        where_path = str(details.get("where_path", "") or "")
        start = str(details.get("start", "") or "")
        lookback = dict(details.get("lookback", {}) or {})
        hints = [
            {
                "kind": "drop_time_start",
                "message": (
                    f"Remove {where_path or 'time.start'} entirely — the window function "
                    "computes its own lookback. Keep time.end if you "
                    "need an upper bound. This is the safe default — apply "
                    "this first."
                ),
                "patch": {"remove": [where_path or "time.start"]},
            }
        ]
        suggested_start = _shift_iso_date_backward(start, lookback)
        if suggested_start:
            hints.append(
                {
                    "kind": "widen_time_window",
                    "message": (
                        f"Widen {where_path + '.value' if where_path else 'time.start'} "
                        "by the metric's lookback "
                        f"({lookback.get('value')} {lookback.get('unit')}) "
                        "so the leaf WHERE doesn't truncate the rows the "
                        "window function depends on. NOTE: for prior_period / "
                        "rolling at a discrete grain (month, quarter, ...), "
                        "the LAG/window also needs at least one full bucket "
                        "BEFORE the widened start — widen by lookback + one "
                        "grain unit if a single-bucket widen still rejects."
                    ),
                    "current_start": start,
                    "suggested_start": suggested_start,
                    "lookback": lookback,
                }
            )
        return hints
    if code == "INVALID_MCP_ARGUMENTS":
        # Argument shape errors at the MCP boundary. The whole point of the
        # structured envelope is teaching the agent how to retry; an empty
        # recovery_hints array forces freeform recovery.
        field = str(details.get("field", "") or "")
        argument_type = str(details.get("argument_type", "") or "")
        hints = []
        # Most common mistake: a sentence, or JSON that is not an object, in place of the
        # query call.
        if field == "query":
            hints.append(
                {
                    "kind": "wrap_query_as_object",
                    "message": (
                        "'query' is the query call, a JSON object: "
                        '{"query": {"select": ["<measure or metric id>"]}}.'
                    ),
                    "closest_valid_query": {"select": ["<measure or metric id>"]},
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        elif field == "kinds" and details.get("unknown_kinds"):
            valid_kinds = list(details.get("valid_kinds") or [])
            hints.append(
                {
                    "kind": "use_valid_kind",
                    "message": (
                        f"Unknown kinds {list(details['unknown_kinds'])}; use one of {valid_kinds}."
                    ),
                    "details": {
                        "field": field,
                        "unknown_kinds": list(details["unknown_kinds"]),
                        "valid_kinds": valid_kinds,
                    },
                }
            )
        elif field == "kinds":
            hints.append(
                {
                    "kind": "use_string_or_array",
                    "message": (
                        "'kinds' must be a comma-separated string "
                        '("measure,metric"), an array of strings (["measure", "metric"]), '
                        'or a JSON array in a string (\'["measure", "metric"]\').'
                    ),
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        elif field in {"limit", "offset"}:
            hints.append(
                {
                    "kind": "use_integer",
                    "message": (
                        f"'{field}' must be a positive integer, not a "
                        f"{argument_type or 'non-numeric'} value."
                    ),
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        elif field:
            hints.append(
                {
                    "kind": "fix_argument_shape",
                    "message": (
                        f"Argument '{field}' has type '{argument_type}' but must "
                        "match the tool's input_schema. Inspect tools/list to see "
                        "the expected shape, then retry."
                    ),
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        # Generic fallback so the array is never empty.
        if not hints:
            hints.append(
                {
                    "kind": "fix_argument_shape",
                    "message": (
                        "Inspect the failing tool in tools/list and resend the "
                        "arguments matching its input_schema."
                    ),
                    "details": dict(details),
                }
            )
        return hints
    if code == "UNKNOWN_MCP_TOOL":
        return [
            {
                "kind": "use_available_tool",
                "message": (
                    "Call tools/list first and pick a name from "
                    "available_tools — common entry points are 'discover', "
                    "'inspect', 'plan'."
                ),
                "available_tools": list(details.get("available_tools", []) or []),
                "details": dict(details),
            }
        ]
    if code == "UNKNOWN_MCP_PROMPT":
        return [
            {
                "kind": "use_available_prompt",
                "message": ("Call prompts/list first to see supported prompt names."),
                "available_prompts": list(details.get("available_prompts", []) or []),
                "details": dict(details),
            }
        ]
    if code == "INVALID_QUERY":
        # Grain-specific recovery: when validate rejects an unsupported
        # ``time.grain``, give the agent a structured patch with the
        # closest supported grain so it can retry without parsing English.
        path = str(details.get("path", "") or "")
        if path == "time.grain" and "supported_grains" in details:
            received_grain = str(details.get("received", "") or "")
            closest_matches = list(details.get("closest_matches", []) or [])
            supported_grains = list(details.get("supported_grains", []) or [])
            temporal_role = str(details.get("temporal_role", "") or "")
            grain_hints: list[dict[str, Any]] = []
            if closest_matches:
                grain_hints.append(
                    {
                        "kind": "replace_grain",
                        "message": (
                            f"time.grain {received_grain!r} is not supported by temporal role "
                            f"{temporal_role!r}. Replace with the closest supported grain "
                            f"{closest_matches[0]!r}, or pick another from "
                            f"{supported_grains}."
                        ),
                        "patch": {"replace": {"time.grain": closest_matches[0]}},
                        "received": received_grain,
                        "closest_matches": closest_matches,
                        "supported_grains": supported_grains,
                        "temporal_role": temporal_role,
                    }
                )
            else:
                grain_hints.append(
                    {
                        "kind": "list_supported_grains",
                        "message": (
                            f"time.grain {received_grain!r} is not supported by temporal role "
                            f"{temporal_role!r}. Pick one of {supported_grains}."
                        ),
                        "received": received_grain,
                        "supported_grains": supported_grains,
                        "temporal_role": temporal_role,
                    }
                )
            return grain_hints
        data_type = str(details.get("data_type", "") or "")
        if data_type and details.get("dimension") and "value" in details:
            # A where value of the wrong type for its dimension (see compiler).
            dimension = details["dimension"]
            message = (
                f"Filter {dimension} on {_FILTER_VALUE_FORMS.get(data_type, 'a ' + data_type)}. "
                "If its column holds another type, set the dimension's `kind` to match."
            )
            return [
                {
                    "kind": "fix_filter_value_type",
                    "message": message,
                    "dimension": dimension,
                    "data_type": data_type,
                }
            ]
        kind = str(details.get("expression_kind", "") or "")
        if kind and details.get("supported_kinds"):
            # A row-level kind (a raw column, ...) as a query value (see ast).
            message = (
                f"'{kind}' reads row values, and a query reads measures and metrics: write an id, "
                f"or one of the kinds {list(details['supported_kinds'])}."
            )
            if kind == "column":
                message += (
                    " To split or filter by a column, name a dimension on it in group_by or where."
                )
            return [{"code": "FIX_QUERY_CALL", "message": message}]
        return list(details.get("recovery_hints", []) or [])
    if code == "QUERY_EXECUTION_ERROR" and details.get("segment_id"):
        segment = details["segment_id"]
        message = (
            f"If the warehouse refused a membership value of {segment} (a type or conversion "
            "error), make each value fit its column, and each dimension's `kind` match it (a "
            "true/false column is `kind: boolean`, compared with `value: true`)."
        )
        return [
            {"kind": "check_segment_membership_values", "message": message, "segment_id": segment}
        ]
    if code == "UNKNOWN_MCP_RESOURCE":
        return [
            {
                "kind": "use_available_resource",
                "message": ("Call resources/list first to see supported MCP resource URIs."),
                "available_resources": list(details.get("available_resources", []) or []),
                "details": dict(details),
            }
        ]
    # A refusal that carries its own hint (``query_call.refusal``) is followed as is.
    return list(details.get("recovery_hints", []) or [])


def _object_catalog_ids(config: PackageConfig) -> list[str]:
    ids: list[str] = []
    for collection in (
        config.entities,
        config.dimensions,
        config.temporal_roles,
        config.relationships,
        config.value_domains,
        config.measures,
        config.metric_recipes,
        config.segments,
        config.aggregate_relations,
        config.relations,
    ):
        for row in collection:
            object_id = str(getattr(row, "id", "") or "").strip()
            if object_id:
                ids.append(object_id)
    return list(dict.fromkeys(ids))


def filter_value_miss(
    subject: str, dimension: str, literal: str, values: Iterable[str], data_type: str = ""
) -> tuple[str, str | None]:
    """The message for a filter literal that matches no value in the data, and the value it
    likely meant, for package validation and a query's own filters alike: the same text in
    another case, or else a close spelling. An id, code or hash (an id dimension, or a literal
    with a digit) differs by one character from another real one, so it gets no close
    spelling, and neither does a weak match."""
    folded = {value.casefold(): value for value in values}
    close = [literal.casefold()] if literal.casefold() in folded else []
    if not close and data_type != "id" and not any(char.isdigit() for char in literal):
        close = get_close_matches(literal.casefold(), list(folded), n=1, cutoff=0.7)
    suggestion = folded[close[0]] if close else None
    return (
        f"{subject} filters {dimension} on {literal!r}, which matches no value in the data"
        + (f"; did you mean {suggestion!r}?" if suggestion else "")
    ), suggestion


def object_id_suggestions(
    config: PackageConfig,
    missing_id: str,
    *,
    limit: int = 3,
) -> list[str]:
    """Suggest near-matching object ids for a missing reference.

    The reviewer's "three irrelevant suggestions are worse than zero"
    complaint surfaced two failure modes here:

    1. ``difflib.get_close_matches`` cutoff of 0.35 was permissive
       enough to admit superficially-similar strings of any meaning.
    2. The token-overlap fallback fired on *any* substring hit, so a
       single shared 1-2 char token (e.g. ``count`` ⊂ ``count_eop``)
       returned a candidate even when nothing else aligned.

    Tightening the floor: difflib cutoff is now 0.5; the fallback
    requires at least one shared token of length ≥ 4. When neither
    yields a candidate, return ``[]`` so the OBJECT_NOT_FOUND arm can
    emit a "call discover" handoff hint instead of three misleading
    matches.
    """
    missing = str(missing_id or "").strip()
    if not missing:
        return []
    candidates = _object_catalog_ids(config)
    if not candidates:
        return []
    lowered = {item.lower(): item for item in candidates}
    if missing.lower() in lowered:
        return [lowered[missing.lower()]]
    # Score the *tail* (segment after the last dot) rather than the
    # full id. Otherwise the shared ``measure.jaffle.`` prefix
    # dominates the difflib ratio and every candidate looks similar —
    # the reviewer's "open_store_count_eop matches nonexistent_measure"
    # bug. Tail-based scoring also keeps namespace prefixes from
    # crowding out real semantic neighbours. Prefer same-namespace
    # matches (typo on a metric returns metric ids; typo on a measure
    # returns measure ids) so the suggestions track the agent's intent.
    parts = missing.split(".")
    missing_tail = parts[-1]
    missing_prefix = ".".join(parts[:-1]) if len(parts) > 1 else ""
    same_namespace = [
        cid for cid in candidates if missing_prefix and cid.startswith(missing_prefix + ".")
    ]
    other_namespace = [
        cid for cid in candidates if not (missing_prefix and cid.startswith(missing_prefix + "."))
    ]
    matches: list[str] = []
    # An exact metric/measure counterpart is more useful than a same-kind typo.
    # Keep fuzzy cross-kind suggestions suppressed for unrelated missing ids.
    if len(parts) == 3 and parts[0] in {"metric", "measure"}:
        other_kind = "measure" if parts[0] == "metric" else "metric"
        counterpart = lowered.get(f"{other_kind}.{parts[1]}.{parts[2]}".lower())
        if counterpart:
            matches.append(counterpart)

    def _add_tail_matches(pool: list[str]) -> None:
        if not pool or len(matches) >= limit:
            return
        tail_to_id: dict[str, str] = {}
        candidate_tails: list[str] = []
        for cid in pool:
            tail = cid.split(".")[-1]
            candidate_tails.append(tail)
            tail_to_id.setdefault(tail, cid)
        remaining = max(0, limit - len(matches))
        tail_matches = get_close_matches(missing_tail, candidate_tails, n=remaining, cutoff=0.5)
        for tail in tail_matches:
            candidate = tail_to_id.get(tail)
            if candidate and candidate not in matches:
                matches.append(candidate)
            if len(matches) >= limit:
                return

    _add_tail_matches(same_namespace)
    # Only fall back to other-kind suggestions when the caller didn't
    # specify a kind prefix. A typo on `dimension.foo` returning
    # `measure.bar` confuses agents into substituting the wrong object
    # type and getting a second failure on the same query. See the
    # blind-agent benchmark: difflib happily matched
    # ``totally_made_up_dim`` to ``tax_paid_usd`` across namespaces.
    _TYPED_PREFIXES = ("measure", "metric", "dimension", "entity", "segment")
    typed_prefix = missing_prefix.split(".")[0] if missing_prefix else ""
    if typed_prefix not in _TYPED_PREFIXES:
        _add_tail_matches(other_namespace)

    if len(matches) < limit:
        missing_tokens_norm = missing_tail.replace("_", " ")
        tokens = [token for token in missing_tokens_norm.split() if len(token) >= 4]
        scored: list[tuple[int, str]] = []
        if tokens:
            # Same-namespace pool first, then everything else — preserves
            # the kind-affinity above when filling the limit. The other-
            # namespace pool is suppressed when the caller specified a
            # typed prefix (measure./metric./dimension./entity./segment.)
            # to avoid suggesting a measure when the agent typo'd a
            # dimension id and vice versa.
            pools: tuple[list[str], ...] = (same_namespace,)
            if typed_prefix not in _TYPED_PREFIXES:
                pools = (same_namespace, other_namespace)
            for pool in pools:
                for candidate in pool:
                    tail = candidate.split(".")[-1].replace("_", " ")
                    tail_tokens = set(tail.split())
                    score = sum(1 for token in tokens if token in tail_tokens)
                    if score:
                        scored.append((score, candidate))
        for _, candidate in sorted(scored, key=lambda item: (-item[0], item[1])):
            if candidate not in matches:
                matches.append(candidate)
            if len(matches) >= limit:
                break
    return matches[:limit]


def _authored_prior_period_metrics(config: PackageConfig, target_measure_id: str) -> list[str]:
    """Find metric recipes whose top-level expression is a prior_period
    over ``target_measure_id``. Returned ids let an agent skip the
    inline shift entirely by referencing the authored metric directly.
    """
    matches: list[str] = []
    target = str(target_measure_id or "").strip()
    if not target:
        # No specific measure to anchor on — return every authored
        # prior_period metric so the agent can browse.
        for recipe in config.metric_recipes:
            expression = getattr(recipe, "expression", None)
            if expression is None:
                continue
            if str(getattr(expression, "kind", "") or "") == "prior_period":
                matches.append(str(recipe.id))
        return matches
    for recipe in config.metric_recipes:
        expression = getattr(recipe, "expression", None)
        if expression is None or str(getattr(expression, "kind", "") or "") != "prior_period":
            continue
        inner = getattr(expression, "input", None)
        inner_measure = ""
        if inner is not None:
            inner_measure = str(getattr(inner, "measure", "") or "")
        if inner_measure == target:
            matches.append(str(recipe.id))
    return matches


def enrich_expression_ast_error(
    exc: SemanticLayerError, config: PackageConfig
) -> SemanticLayerError:
    """Attach ``closest_matches`` to ``INVALID_EXPRESSION_AST`` errors.

    For ``expression_kind == "prior_period"`` we walk the metric recipes
    looking for authored prior-period counterparts over the same source
    measure — the reviewer's "the path forward is the authored measure"
    case. The recovery_hints arm in :func:`recovery_hints_for_error`
    picks the matches up and renders a top-of-list hint.
    """
    if exc.code != "INVALID_EXPRESSION_AST":
        return exc
    details = dict(exc.details or {})
    kind = str(details.get("expression_kind", "") or "")
    if kind != "prior_period":
        return exc
    if details.get("closest_matches"):
        return exc
    received = dict(details.get("received", {}) or {})
    measure_id = str(received.get("measure", "") or "")
    if not measure_id:
        inner = received.get("input")
        if isinstance(inner, dict):
            measure_id = str(inner.get("measure", "") or "")
    matches = _authored_prior_period_metrics(config, measure_id)
    if not matches:
        return exc
    details["closest_matches"] = matches[:5]
    return SemanticLayerError(exc.code, str(exc), details=details)


def enrich_column_expression(exc: SemanticLayerError, config: PackageConfig) -> SemanticLayerError:
    """Attach ``closest_matches`` to a refused raw ``column`` expression: the dimensions defined
    on that column, those of its entity or table when it names one that has any."""
    details = dict(exc.details or {})
    column = str(details.get("column") or "").lower()
    if (
        exc.code != "INVALID_QUERY"
        or details.get("expression_kind") != "column"
        or not column
        or details.get("closest_matches")
    ):
        return exc
    owners = {
        entity.id
        for entity in config.entities
        if details.get("entity") in (entity.id, entity.name) or details.get("table") == entity.table
    }
    on_column = [row for row in config.dimensions if row.column.lower() == column]
    matches = sorted(
        row.id for row in [row for row in on_column if row.entity in owners] or on_column
    )
    if not matches:
        return exc
    details["closest_matches"] = matches[:10]
    return SemanticLayerError(exc.code, str(exc), details=details)


_GAP_KEYS = (
    "missing_link",
    "nearest_entity",
    "closest_dimensions",
    "closest_measures",
    "unknown_entity",
)
# A caller's own id is echoed at most this long (a refusal's own message names it whole).
_ECHO_CHARS = 120


def _echo(text: str) -> str:
    return text if len(text) <= _ECHO_CHARS else text[: _ECHO_CHARS - 1] + "…"


def _unknown_entity_fact(target: str) -> str:
    return f"'{_echo(target)}' is not an entity of this package"


def _closest_entity_ids(config: PackageConfig, missing: str) -> list[str]:
    """Entity ids near a misspelled one, scored on the part after the kind: an entity slot is
    offered entities only, never the dimension or measure id that was put there."""
    by_tail = {row.id.partition(".")[2] or row.id: row.id for row in config.entities}
    tail = missing.rpartition(".")[2] if "." in missing else missing
    matches = get_close_matches(tail[:_ECHO_CHARS], list(by_tail), n=3, cutoff=0.5)
    return [by_tail[match] for match in matches if by_tail[match] != missing]


def enrich_path_not_found(
    exc: SemanticLayerError,
    config: PackageConfig,
    *,
    runnable_measure: Callable[[str], bool] | None = None,
) -> SemanticLayerError:
    """Attach reachable-target context to ``PATH_NOT_FOUND``.

    Shared path eligibility approves alternatives: relationship
    directions, hop limits, and route decisions apply to hints too.
    ``runnable_measure`` says whether the caller's access policies let them run a measure;
    stand-in measures are offered only with it, and only ones it allows. Enriching again (under
    another view) rebuilds the gap from scratch, so nothing an earlier view saw survives.
    """
    if exc.code != "PATH_NOT_FOUND":
        return exc
    details = dict(exc.details or {})
    message = str(exc)
    # Undo an earlier enrichment: its facts were appended to the message, after the base text.
    prior = [details.pop(key, None) for key in _GAP_KEYS]
    from .path_gap import missing_link_fact

    for fact in (
        missing_link_fact(prior[0] or {}),
        _unknown_entity_fact(str(prior[4])) if prior[4] else "",
    ):
        if fact:
            message = message.replace(f": {fact}.", "")
    start = str(details.get("start", "") or "")
    target = str(details.get("target", "") or "")
    if not start or not target:
        match = re.search(r"from '([^']+)' to '([^']+)'", str(exc))
        if match:
            start = start or match.group(1)
            target = target or match.group(2)
    if not start:
        return exc
    from .fanout import eligible_path_targets

    reachable_sorted = list(eligible_path_targets(config, start=start))
    # Walk the dimension index for concrete ``dimension.<id>`` values
    # whose owning entity is ``start`` or any reachable entity. Listing
    # 15 ids keeps the envelope small; agents that need more browse
    # via ``inspect``. Same-entity dimensions come first so the agent
    # sees the cheapest alternatives at the top.
    compatible_dimensions: list[str] = []
    eligible_entities = {start, *reachable_sorted}
    dimensions = config.dimensions
    for dim in dimensions:
        dim_id = str(getattr(dim, "id", "") or "")
        dim_entity = str(getattr(dim, "entity", "") or "")
        if not dim_id or dim_entity not in eligible_entities:
            continue
        compatible_dimensions.append(dim_id)
    # Stable rank: same-entity first, then everything else; alpha within
    # each bucket so reviewers see a deterministic list.
    compatible_dimensions = sorted(
        d
        for d in compatible_dimensions
        if any(
            getattr(dim, "id", "") == d and getattr(dim, "entity", "") == start
            for dim in dimensions
        )
    ) + sorted(
        d
        for d in compatible_dimensions
        if any(
            getattr(dim, "id", "") == d and getattr(dim, "entity", "") != start
            for dim in dimensions
        )
    )
    details["start"] = start
    details["target"] = target
    details["reachable_targets"] = reachable_sorted
    details["compatible_group_by_dimensions"] = compatible_dimensions[:15]
    if any(row.id == target and row.is_row_grain for row in config.entities):
        details["target_row_grain"] = True
    if not reachable_sorted:
        details.update(_isolated_start(config, start, target, str(details.get("reason") or "")))
    reason = str(details.get("reason") or "")
    if reason == "excluded_by_decision":
        return SemanticLayerError(exc.code, message, details=details)
    from .path_gap import closest_dimensions, closest_measures, path_gap

    entity_ids = [row.id for row in config.entities]
    if target not in entity_ids:
        # A misspelled (or, for this caller, nonexistent) entity: there is no gap to name, and
        # a relationship into it can't be authored. Say so, with the ids it may have meant.
        for key in ("reachable_targets", "compatible_group_by_dimensions"):
            details.pop(key, None)
        details["unknown_entity"] = _echo(target)
        details["closest_matches"] = _closest_entity_ids(config, target)
        message = f"{message}: {_unknown_entity_fact(target)}."
        return SemanticLayerError(exc.code, message, details=details)
    # What one change would connect the pair (said in the message too, which an agent reads
    # first), and the closest things the caller could ask for instead. Read from the caller's
    # view; none of it changes a route or an answer.
    if reason == "no_relationship_chain" and "relationship" not in details:
        details.update(path_gap(config, start, target, reachable_sorted))
    fact = missing_link_fact(details.get("missing_link") or {})
    if fact:
        message = f"{message}: {fact}."
    items = list(details.get("unreachable_items") or [])
    closest = closest_dimensions(config, start, reachable_sorted, target, items)
    if closest:
        details["closest_dimensions"] = closest
    stand_ins = closest_measures(
        config, target, list(details.get("measures") or []), runnable_measure
    )
    if stand_ins:
        details["closest_measures"] = stand_ins
    return SemanticLayerError(exc.code, message, details=details)


def _isolated_start(config: PackageConfig, start: str, target: str, reason: str) -> dict[str, Any]:
    """What keeps ``start`` from every entity: the constraint (``blocked_by``), the entities
    related to both ends (``bridge_entities``), and the key columns of one end that the other
    end's dimensions read (``shared_key_columns``)."""

    def related(entity: str) -> set[str]:
        return {
            other
            for rel in config.relationships
            for one, other in (
                (rel.source_entity, rel.target_entity),
                (rel.target_entity, rel.source_entity),
            )
            if one == entity
        }

    walkable = any(
        (rel.source_entity == start and "forward" in rel.allowed_directions)
        or (rel.target_entity == start and "reverse" in rel.allowed_directions)
        for rel in config.relationships
    )
    blocked = {"hop_limit_exceeded": "hop_limit", "excluded_by_decision": "route_choice"}.get(
        reason
    ) or ("route_choice" if walkable else "direction" if related(start) else "no_relationship")
    keys = {row.id: list(row.key_columns) for row in config.entities}

    def columns(entity: str) -> set[str]:
        return {dim.column.lower() for dim in config.dimensions if dim.entity == entity}

    shared = {
        column
        for one, other in ((start, target), (target, start))
        for column in keys.get(other, [])
        if column.lower() in columns(one)
    }
    return {
        "blocked_by": blocked,
        "bridge_entities": sorted((related(start) & related(target)) - {start, target}),
        "shared_key_columns": sorted(shared),
    }


def enrich_object_not_found(exc: SemanticLayerError, config: PackageConfig) -> SemanticLayerError:
    # OBJECT_NOT_FOUND is the canonical "unknown ID" code, but
    # INVALID_TEMPORAL_ROLE is raised with the same shape when an unknown
    # temporal_role id slips through (compiler.py paths). Treat it the
    # same way — authors typo'ing a temporal role still benefit from
    # near-match suggestions.
    if exc.code not in {"OBJECT_NOT_FOUND", "INVALID_TEMPORAL_ROLE"}:
        return exc
    details = dict(exc.details or {})
    if details.get("closest_matches"):
        return exc
    missing = (
        details.get("object_id")
        or details.get("id")
        or details.get("dimension")
        or details.get("measure")
        or details.get("metric")
        or details.get("temporal_role")
        or details.get("segment")
        or ""
    )
    if not missing:
        match = re.search(r"'([^']+)'", str(exc))
        missing = match.group(1) if match else ""
    suggestions = object_id_suggestions(config, str(missing))
    if not suggestions:
        # Even with no fuzzy matches, persist the parsed object_id into
        # details so downstream recovery-hint logic (the
        # ``call_discover_to_locate`` handoff arm) can derive a
        # discover term from it. Without this, raises that lacked
        # ``details.object_id`` (e.g. compiler_parts/paths.py:118
        # raised it with no details) lost the id entirely after the
        # enrichment step returned ``exc`` unchanged.
        if missing and "object_id" not in details:
            details["object_id"] = str(missing)
            return SemanticLayerError(exc.code, str(exc), details=details)
        return exc
    details["closest_matches"] = suggestions
    if missing and "object_id" not in details:
        details["object_id"] = str(missing)
    return SemanticLayerError(exc.code, str(exc), details=details)


# Raw process/adapter output must never reach public error envelopes:
# stdout can carry query result rows, stderr and option values can carry
# connection metadata, and raw SQL bypasses the debug-gated redaction in
# _query_execution_error_details(). Adapters should not attach these keys
# at all; this scrub is the envelope-level backstop for the ones that do.
_RAW_OUTPUT_DETAIL_KEYS = frozenset({"stdout", "stderr", "options", "command"})


def _scrub_exception_details(details: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(details or {})
    removed = sorted(key for key in _RAW_OUTPUT_DETAIL_KEYS if key in out)
    for key in removed:
        out.pop(key)
    if "sql" in out and not out.get("sql_debug_authorized"):
        out.pop("sql")
        out["sql_redacted"] = True
        removed.append("sql")
    if removed:
        out["redacted_detail_keys"] = removed
    return out


# Details that echo an expression back to the caller, in the call's spelling.
_EXPRESSION_DETAILS = ("expression", "received", "predicate")


def _in_call_spelling(details: dict[str, Any]) -> dict[str, Any]:
    from .query_call import _call_expression

    return {
        key: _call_expression(value)
        if key in _EXPRESSION_DETAILS and isinstance(value, dict) and value.get("kind")
        else value
        for key, value in details.items()
    }


def exception_issue(
    exc: SemanticLayerError, *, stage: str, object_ids: Iterable[str] | None = None, path: str = ""
) -> dict[str, Any]:
    details = _in_call_spelling(_scrub_exception_details(exc.details))
    return semantic_issue(
        code=exc.code,
        message=str(exc),
        severity="error",
        stage=stage,
        details=details,
        object_ids=object_ids,
        path=path,
        recovery_hints=recovery_hints_for_error(exc.code, details),
    )


def rewrite_warning_payload(step: Any) -> dict[str, Any]:
    details = dict(getattr(step, "details", {}) or {})
    rewrite_kind = str(getattr(step, "kind", "") or "")
    if rewrite_kind:
        details.setdefault("rewrite_kind", rewrite_kind)
    message = str(getattr(step, "reason", "") or "rewrite applied")
    if rewrite_kind == "leaf_preaggregate_join":
        details.setdefault("rewrite_classification", "safe_independent_leaf_alignment")
        message = (
            "Safe cross-fact alignment: independent leaf aggregates were joined on final "
            "grain keys."
        )
    # A disclosure of how a correct answer was computed: a note, not a warning to act on.
    return semantic_issue(
        code="REWRITE_APPLIED",
        message=message,
        severity="info",
        stage="planning",
        details=details,
        object_ids=[str(getattr(step, "measure_id", "") or "")],
    )


def provenance_summary(
    config: PackageConfig,
    logical_plan: Any,
    *,
    policy_effects: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rel_index = {row.id: row for row in config.relationships}
    selected_paths = []
    for target_entity, relationship_ids in dict(
        getattr(logical_plan, "selected_paths", {}) or {}
    ).items():
        selected_paths.append(
            {
                "target_entity": target_entity,
                "relationship_ids": list(relationship_ids or []),
                "contracts": [
                    relationship_contract_payload(rel_index[rel_id])
                    for rel_id in list(relationship_ids or [])
                    if rel_id in rel_index
                ],
            }
        )
    return {
        "root_entity": str(getattr(logical_plan, "root_entity", "") or ""),
        "time": dict(getattr(logical_plan, "time", {}) or {}),
        "rewrite_status": "rewritten"
        if list(getattr(logical_plan, "rewrite_steps", []) or [])
        else "direct",
        "selected_paths": selected_paths,
        "policy_effects": list(policy_effects or []),
    }


def _infer_join_semantics(rel: RelationshipConfig) -> str:
    card = str(rel.cardinality or "").strip().upper()
    mapping = {
        "1:1": "one_to_one",
        "1:N": "one_to_many",
        "N:1": "many_to_one",
        "N:N": "many_to_many",
    }
    return mapping.get(card, "")
