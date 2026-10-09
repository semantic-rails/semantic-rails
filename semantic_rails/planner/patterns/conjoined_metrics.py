"""Exact conjoined subjects on one compatible clock, window and grouping."""

from __future__ import annotations

from typing import Any

from ...config_parts.measure_governance import whole_aggregate
from ...naming import semantic_token as _semantic_token
from .._base import (
    RuntimeCompositionDraft,
    _aggregation_from_text,
    _name_matches,
    _object_by_id,
    _resolved,
    _tokens,
)
from ..coverage import CoverageGap, _coverage_why
from ..faithfulness import _conjoined_filter_text, _conjoined_subjects, _selectable_subjects
from ..generators import _matched_value_rows, _normalize_value_filters
from ..groupings import _explicit_grain, _maybe_group_by, _time_spec
from ..qualifiers import _add_order
from ..time_windows import _time_window
from ._protocol import IntentPattern
from .metric_by_dimension_rollup import _TIME_SERIES_PHRASES, _governed_target


def _clock(config: Any, row: Any) -> str:
    # An object's own clock, or its plain aggregate's measure clock; never a model default.
    role = str(getattr(row, "temporal_role", "") or getattr(row, "default_temporal_role", ""))
    wrapped = whole_aggregate(row) if row.id.startswith("metric.") and not role else None
    measure = _object_by_id(config.measures, wrapped[0]) if wrapped and not wrapped[2] else None
    return role or str(getattr(measure, "default_temporal_role", ""))


def _part_query(
    runtime: Any, text: str, phrase: str, target: Any, filter_text: str
) -> tuple[Any, dict[str, Any]]:
    """Use the catch-all's time, grouping, value-filter and governance helpers."""
    config = runtime._config
    role = _clock(config, target)
    clock = _object_by_id(config.temporal_roles, role)
    clock_label = str(getattr(clock, "label", "") or "")
    group_by = _maybe_group_by(config, text, target_terms=_tokens(phrase), clock=clock_label)
    expression = (
        {
            "measure": target.id,
            "aggregation": _aggregation_from_text(phrase, set(_tokens(phrase)), target),
        }
        if target.id.startswith("measure.")
        else {"metric": target.id}
    )
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"as": _semantic_token(target.id, fallback="value"), "expression": expression}],
    }
    window = _time_window(text)
    if role and (
        _explicit_grain(text, clock_label)
        or window.bounds
        or window.unresolved
        or any(cue in text.lower() for cue in _TIME_SERIES_PHRASES)
    ):
        query["time"] = _time_spec(role, text, clock_label)
    if group_by:
        query["group_by"] = group_by
    query = _normalize_value_filters(
        query, _matched_value_rows(runtime, query, filter_text), text=filter_text
    )
    governed = _governed_target(config, phrase, query)
    if (
        governed is not None
        and (query.get("time") or {}).get("temporal_role", "") == governed.temporal_role
    ):
        target = governed
        query["select"] = [
            {
                "as": _semantic_token(target.id, fallback="value"),
                "expression": {"metric": target.id},
            }
        ]
    return target, query


def _match(runtime: Any, text: str, terms: set[str]) -> RuntimeCompositionDraft | None:
    subjects = _conjoined_subjects(runtime, text)
    config = runtime._config
    for part in subjects:
        part["candidate_ids"] = [
            row.id for row in _selectable_subjects(config, part["candidate_ids"])
        ]
    if len(subjects) < 2 or any(len(part["candidate_ids"]) != 1 for part in subjects):
        return None
    objects = {row.id: row for row in [*config.measures, *config.metric_recipes]}
    # Names consume their own words; another selected subject is never a filter value.
    filter_text = _conjoined_filter_text(text, subjects)
    targets_and_queries = [
        _part_query(runtime, text, part["phrase"], objects[part["candidate_ids"][0]], filter_text)
        for part in subjects
    ]
    targets = [target for target, _ in targets_and_queries]
    queries = [query for _, query in targets_and_queries]
    parts = []
    cursor = 0
    for part, target in zip(subjects, targets, strict=True):
        start = text.find(part["phrase"], cursor)
        if start < 0:
            return None
        cursor = start + len(part["phrase"])
        parts.append(
            {
                **part,
                "temporal_roles": [_clock(config, target)],
                "spans": [
                    [start + begin, start + end]
                    for _, begin, end in _name_matches(target, part["phrase"])
                ],
            }
        )
    # Compatibility is directional: every subject must allow the drafted clock.
    roles = list(dict.fromkeys(_clock(config, target) for target in targets))
    role = next(
        (
            candidate
            for candidate in roles
            if candidate
            and all(
                candidate == _clock(config, target) or candidate in target.compatible_temporal_roles
                for target in targets
            )
        ),
        "",
    )
    shared = bool(role) or roles == [""]
    first = queries[0]
    for _target, query in targets_and_queries:
        own_time = dict(query.get("time") or {})
        own_time.pop("temporal_role", None)
        first_time = dict(first.get("time") or {})
        first_time.pop("temporal_role", None)
        shared = (
            shared
            and own_time == first_time
            and query.get("group_by", []) == first.get("group_by", [])
            and query.get("where", []) == first.get("where", [])
        )
        if query.get("time") and role:
            query["time"] = {**query["time"], "temporal_role": role}
    query = {**first, "select": []}
    aliases: set[str] = set()
    for part_query in queries:
        item = dict(part_query["select"][0])
        alias = item["as"]
        suffix = 2
        while item["as"] in aliases:
            item["as"] = f"{alias}_{suffix}"
            suffix += 1
        aliases.add(item["as"])
        query["select"].append(item)
    _add_order(query)
    why: dict[str, Any] = {}
    if not shared:
        why = (
            _coverage_why(
                [
                    CoverageGap(
                        kind="multiple_subjects_unrealized",
                        clause=" and ".join(part["phrase"] for part in subjects),
                        message="The requested subjects do not share one supported clock, window and grouping.",
                        expected={"subjects": subjects},
                        recovery_hint={
                            "kind": "provide_multiple_selects",
                            "message": "Plan each named subject with its own clock, window and grouping.",
                        },
                    )
                ]
            )
            or {}
        )
        why["details"]["parts"] = parts
    return RuntimeCompositionDraft(
        query=query,
        resolved=[_resolved(target) for target in targets],
        rationale=["Selected every exact conjoined subject in question order on a shared scope."],
        interpreted_intent={"pattern": "conjoined_metrics", "source": "plan", "parts": parts},
        blocked_reason=why,
    )


PATTERN = IntentPattern(name="conjoined_metrics", match=_match)
