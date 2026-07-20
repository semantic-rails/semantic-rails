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

import re
from collections.abc import Iterable
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
        "rollup_safe_aggregations": list(rel.rollup_safe_aggregations),
    }


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


def recovery_hints_for_error(
    code: str, details: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    details = dict(details or {})
    if code == "INVALID_EXPRESSION":
        position = str(details.get("expression_position", "") or "")
        missing = str(details.get("missing_key", "") or "")
        received = list(details.get("received_keys", []) or [])
        hints: list[dict[str, Any]] = []
        # Cross-position key suggestion: the reviewer hit asymmetry —
        # order_by uses ``field``, where uses ``dimension``, metric_filters
        # uses ``expression``. When the agent's received_keys include the
        # canonical key from a different position, tell them where it
        # belongs before showing the generic fix-shape hint.
        _POSITION_CANONICAL_KEY: dict[str, str] = {
            "order_by": "field",
            "where": "dimension",
            "metric_filters": "expression",
        }
        canonical_here = _POSITION_CANONICAL_KEY.get(position, "")
        cross_positions = [
            (pos, key)
            for pos, key in _POSITION_CANONICAL_KEY.items()
            if pos != position and key in received and key != canonical_here
        ]
        if cross_positions:
            other_pos, other_key = cross_positions[0]
            hints.append(
                {
                    "kind": "use_canonical_key_for_position",
                    "message": (
                        f"{position!r} entries use the key {canonical_here!r}, "
                        f"but you passed {other_key!r} which belongs in {other_pos!r}. "
                        f"Move the value under {canonical_here!r} (or move the entry "
                        f"to the {other_pos!r} clause if that's what you meant)."
                    ),
                    "expected_key_here": canonical_here,
                    "received_canonical_key": other_key,
                    "received_canonical_key_belongs_to": other_pos,
                }
            )
        if position == "order_by":
            hints.append(
                {
                    "kind": "fix_order_by_shape",
                    "message": (
                        'Each order_by entry must be {"field": "<select_alias_or_dimension_id_or_time>", '
                        '"direction": "ASC"|"DESC"} — not a select-style expression. Use the alias '
                        "you set on a select item, the group_by dimension id, or 'time'."
                    ),
                    "shape": {"field": "<alias>", "direction": "ASC"},
                    "missing_key": missing,
                    "received_keys": received,
                }
            )
        elif position == "metric_filters":
            hints.append(
                {
                    "kind": "fix_metric_filters_shape",
                    "message": (
                        "Each metric_filters entry must be "
                        '{"expression": {...}, "op": "=|>|<|>=|<=|!=", "value": ...}. '
                        "The 'expression' is typically a kind='metric_predicate' object."
                    ),
                    "shape": {"expression": {"kind": "metric_predicate"}, "op": "=", "value": True},
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
                        "Consult docs/QUERY_IR_SCHEMA.md for the per-position shape."
                    ),
                    "missing_key": missing,
                    "received_keys": received,
                }
            )
        return hints
    if code == "INVALID_EXPRESSION_AST":
        kind = str(details.get("expression_kind", "") or "")
        position = str(details.get("expression_position", "") or "")
        # ``received`` is sometimes a dict (the full malformed expr) and
        # sometimes a scalar (e.g. the bad p value). Coerce defensively
        # so a downstream hint loop doesn't crash on a float.
        received_raw = details.get("received")
        received_dict: dict[str, Any] = received_raw if isinstance(received_raw, dict) else {}
        closest = list(details.get("closest_matches", []) or [])
        if kind == "prior_period":
            hints = []
            received_measure = str(received_dict.get("measure", "") or "")
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
            shorthand_shape: dict[str, Any] = {
                "kind": "prior_period",
                "measure": received_measure or "<measure_id>",
                "offset": -1,
                "grain": "<day|week|month|quarter|year>",
            }
            ir_shape: dict[str, Any] = {
                "kind": "prior_period",
                "input": {"kind": "measure", "measure": received_measure or "<measure_id>"},
                "offset": {"unit": "<day|week|month|quarter|year>", "value": 1},
            }
            hints.append(
                {
                    "kind": "use_prior_period_shorthand",
                    "message": (
                        "For an inline period shift in a select, use the shorthand: "
                        "{kind: 'prior_period', measure: '<id>', offset: <signed int>, grain: '<unit>'}."
                    ),
                    "shape": shorthand_shape,
                }
            )
            hints.append(
                {
                    "kind": "use_prior_period_ir_shape",
                    "message": (
                        "Canonical IR shape: input is required and offset must be {unit, value}."
                    ),
                    "shape": ir_shape,
                }
            )
            if closest:
                hints.insert(
                    0,
                    {
                        "kind": "use_authored_prior_period_metric",
                        "message": (
                            "An authored metric already defines this period shift over the same measure. "
                            "Reference it directly via {kind: 'metric', metric: '<id>'} — no inline shift needed."
                        ),
                        "closest_matches": closest,
                    },
                )
            return hints
        return [
            {
                "kind": "fix_expression_shape",
                "message": (
                    f"Expression with kind={kind!r} in position {position!r} is malformed. "
                    "Consult docs/QUERY_IR_SCHEMA.md for the per-position shape."
                ),
                "details": dict(details),
            }
        ]
    if code == "PATH_NOT_FOUND":
        start = str(details.get("start", "") or "")
        target = str(details.get("target", "") or "")
        reachable = list(details.get("reachable_targets", []) or [])
        hints = []
        if not reachable:
            hints.append(
                {
                    "kind": "isolated_source_entity",
                    "message": (
                        f"'{start}' has no declared relationships in this package — no "
                        f"entity is reachable from it. This usually means the measure is "
                        f"sourced from a roll-up table that wasn't connected to the rest "
                        f"of the graph. Group by a dimension defined on '{start}' itself, "
                        f"or author a relationship from '{start}' to '{target}'."
                    ),
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
        if compatible_dims:
            hints.append(
                {
                    "kind": "switch_group_by_dimension",
                    "message": (
                        f"Dimensions reachable from '{start}' (cap 15): {compatible_dims}."
                    ),
                    "compatible_group_by_dimensions": compatible_dims,
                }
            )
        hints.append(
            {
                "kind": "author_relationship",
                "message": (
                    f"If '{target}' should be reachable, author a relationship in "
                    "configs/.../relationships/ connecting it to a reachable entity."
                ),
                "start": start,
                "unreachable_target": target,
            }
        )
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
                        "Choose a supported aggregation — sum, count, "
                        "count_distinct, avg, min, max, median, "
                        "percentile, first_value, or last_value."
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
                "message": "Rename projected outputs so each result column is unique.",
                "alias": alias,
            }
        ]
    if code == "INVALID_TEMPORAL_ROLE":
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
                        "yet. Track the rollout in docs/CAPABILITIES.md."
                    ),
                    "feature_status": feature_status,
                    "anchor": dict(details.get("anchor", {}) or {}),
                    "window": dict(details.get("window", {}) or {}),
                }
            )
            hints.append(
                {
                    "kind": "use_authored_windowed_measure",
                    "message": (
                        "If you need the result today, author the "
                        "windowed measure inside a metric recipe "
                        "(kind: scoped_aggregate) so the per-row "
                        "window is fixed at package time — query-time "
                        "anchor + window awaits the next round."
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
    if code == "AMBIGUOUS_PATH":
        return [
            {
                "kind": "narrow_query",
                "message": "Choose a more specific grouping, filter, or root entity to break the path ambiguity.",
                "candidates": list(details.get("candidates", []) or []),
            }
        ]
    if code == "FANOUT_UNSAFE":
        return [
            {
                "kind": "change_breakdown",
                "message": "Use a safer breakdown or add a pre-aggregation boundary before traversing this path.",
                "relationships": list(details.get("relationships", []) or []),
            }
        ]
    if code == "ROLLUP_UNSAFE":
        return [
            {
                "kind": "change_aggregation",
                "message": "Use an additive primitive, provide sketch metadata, or query at the declared aggregation entity.",
                "details": dict(details),
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
        mixed_grain_hints: list[dict[str, Any]] = []
        compatible_measures = list(details.get("compatible_measures", []) or [])
        closest_measure = str(details.get("closest_compatible_measure", "") or "")
        offending_dims = list(details.get("offending_dimensions", []) or [])
        requested_measures = list(details.get("measures", []) or [])
        # When the offending dimension is a calendar-date dimension the
        # primary recovery is the time block — lead with it.
        time_axis = dict(details.get("time_axis_recovery", {}) or {})
        if time_axis:
            grain = str(time_axis.get("grain", "") or "")
            role = str(time_axis.get("temporal_role", "") or "")
            example = {"temporal_role": role or "<temporal_role>", "grain": grain or "<grain>"}
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
            mixed_grain_hints.append(hint)
        if compatible_measures and closest_measure:
            dim_label = offending_dims[0] if offending_dims else "the requested dimension"
            hint = {
                "kind": "replace_measure",
                "message": (
                    f"Measure '{closest_measure}' aggregates at a grain that can group by "
                    f"'{dim_label}' — query it instead."
                ),
                "compatible_measures": compatible_measures,
            }
            closest_query = dict(details.get("closest_compatible_measure_query", {}) or {})
            if closest_query:
                hint["closest_valid_query"] = closest_query
            mixed_grain_hints.append(hint)
        compatible_dimensions = list(details.get("compatible_dimensions", []) or [])
        if compatible_dimensions:
            measure_label = requested_measures[0] if requested_measures else "the requested measure"
            hint = {
                "kind": "replace_dimension",
                "message": (
                    f"Keep '{measure_label}' and group by a dimension at a compatible grain."
                ),
                "compatible_dimensions": compatible_dimensions,
            }
            closest_query = dict(details.get("closest_compatible_dimension_query", {}) or {})
            if closest_query:
                hint["closest_valid_query"] = closest_query
            mixed_grain_hints.append(hint)
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
        effects = list(details.get("policy_effects", []) or [])
        if any(str(effect.get("kind", "")) == "metric_constraint" for effect in effects):
            return [
                {
                    "kind": "adjust_metric_cuts",
                    "message": (
                        "Use only the group_by, where, temporal, or metric-filter cuts "
                        "allowed by the metric constraint policy."
                    ),
                    "details": dict(details),
                }
            ]
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
                        "or catalog to browse the full inventory."
                    ),
                    "recommended_tool": "discover",
                    "recommended_terms": discover_term,
                }
            )
        return hints
    if code == "WINDOWED_TIME_FILTER_UNSUPPORTED":
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
        start = str(details.get("start", "") or "")
        lookback = dict(details.get("lookback", {}) or {})
        hints = [
            {
                "kind": "drop_time_start",
                "message": (
                    "Remove query.time.start entirely — the window function "
                    "computes its own lookback. Keep query.time.end if you "
                    "need an upper bound. This is the safe default — apply "
                    "this first."
                ),
                "patch": {"remove": ["time.start"]},
            }
        ]
        suggested_start = _shift_iso_date_backward(start, lookback)
        if suggested_start:
            hints.append(
                {
                    "kind": "widen_time_window",
                    "message": (
                        "Widen query.time.start by the metric's lookback "
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
        # Most common mistake: passing a Query IR as a stringified JSON
        # blob or a freeform sentence instead of a JSON object.
        if field == "query" and argument_type in {"str", "string"}:
            hints.append(
                {
                    "kind": "wrap_query_as_object",
                    "message": 'Wrap the query in an object: {"query": {"select": [...], "version": 1}}.',
                    "closest_valid_query": {
                        "version": 1,
                        "select": [{"object_id": "<measure_or_metric_id>"}],
                    },
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        elif field == "query":
            hints.append(
                {
                    "kind": "wrap_query_as_object",
                    "message": (
                        "'query' must be a JSON object containing the Query IR — "
                        'e.g. {"query": {"version": 1, "select": [...]}}.'
                    ),
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        elif field == "policy_context":
            hints.append(
                {
                    "kind": "wrap_policy_context_as_object",
                    "message": (
                        "'policy_context' must be a JSON object with optional "
                        'keys like {"environment": "prod", "audience": "internal"}.'
                    ),
                    "details": {"field": field, "argument_type": argument_type},
                }
            )
        elif field == "kinds":
            hints.append(
                {
                    "kind": "use_string_or_array",
                    "message": (
                        "'kinds' must be either a comma-separated string "
                        '("measure,metric") or an array of strings (["measure", "metric"]).'
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
                    "'inspect', 'plan', 'validate', 'compile'."
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
        if path == "time.grain":
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
        return []
    if code == "UNKNOWN_MCP_RESOURCE":
        return [
            {
                "kind": "use_available_resource",
                "message": ("Call resources/list first to see supported MCP resource URIs."),
                "available_resources": list(details.get("available_resources", []) or []),
                "details": dict(details),
            }
        ]
    return []


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


def object_id_suggestions(config: PackageConfig, missing_id: str, *, limit: int = 3) -> list[str]:
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


def enrich_path_not_found(exc: SemanticLayerError, config: PackageConfig) -> SemanticLayerError:
    """Attach reachable-target context to ``PATH_NOT_FOUND``.

    The raw error is the most actionable signal we have, but the empty
    ``recovery_hints`` made it the laggard of the structured-envelope
    rollout. Enrich the details with a BFS of entities reachable from
    the source so :func:`recovery_hints_for_error` can list concrete
    alternatives.
    """
    if exc.code != "PATH_NOT_FOUND":
        return exc
    details = dict(exc.details or {})
    if details.get("reachable_targets"):
        return exc
    start = str(details.get("start", "") or "")
    target = str(details.get("target", "") or "")
    if not start or not target:
        match = re.search(r"from '([^']+)' to '([^']+)'", str(exc))
        if match:
            start = start or match.group(1)
            target = target or match.group(2)
    if not start:
        return exc
    # BFS the relationship graph from ``start``. Kept local to avoid a
    # cycle with ``fanout.build_graph`` (diagnostics is imported widely).
    graph: dict[str, set[str]] = {}
    for rel in getattr(config, "relationships", []) or []:
        source = str(getattr(rel, "source_entity", "") or "")
        target_entity = str(getattr(rel, "target_entity", "") or "")
        if not source or not target_entity:
            continue
        graph.setdefault(source, set()).add(target_entity)
        graph.setdefault(target_entity, set()).add(source)
    visited: set[str] = {start}
    frontier: list[str] = [start]
    while frontier:
        next_frontier: list[str] = []
        for node in frontier:
            for neighbour in graph.get(node, set()):
                if neighbour in visited:
                    continue
                visited.add(neighbour)
                next_frontier.append(neighbour)
        frontier = next_frontier
    visited.discard(start)
    reachable_sorted = sorted(visited)
    # Walk the dimension index for concrete ``dimension.<id>`` values
    # whose owning entity is ``start`` or any reachable entity. Listing
    # 15 ids keeps the envelope small; agents that need more browse
    # via ``inspect``. Same-entity dimensions come first so the agent
    # sees the cheapest alternatives at the top.
    compatible_dimensions: list[str] = []
    eligible_entities = {start, *reachable_sorted}
    for dim in getattr(config, "dimensions", []) or []:
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
            for dim in getattr(config, "dimensions", []) or []
        )
    ) + sorted(
        d
        for d in compatible_dimensions
        if any(
            getattr(dim, "id", "") == d and getattr(dim, "entity", "") != start
            for dim in getattr(config, "dimensions", []) or []
        )
    )
    details["start"] = start
    details["target"] = target
    details["reachable_targets"] = reachable_sorted
    details["compatible_group_by_dimensions"] = compatible_dimensions[:15]
    return SemanticLayerError(exc.code, str(exc), details=details)


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


def exception_issue(
    exc: SemanticLayerError, *, stage: str, object_ids: Iterable[str] | None = None, path: str = ""
) -> dict[str, Any]:
    details = _scrub_exception_details(exc.details)
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
    return semantic_issue(
        code="REWRITE_APPLIED",
        message=message,
        severity="warning",
        stage="planning",
        details=details,
        object_ids=[str(getattr(step, "measure_id", "") or "")],
    )


def history_warning_payload(*, paths: list[dict[str, Any]]) -> dict[str, Any]:
    return semantic_issue(
        code="NULL_PRESERVING_HISTORY",
        message="History-backed breakdowns preserve NULL when no valid history row exists at the query time anchor.",
        severity="warning",
        stage="planning",
        details={"paths": list(paths)},
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
