"""Conservative intent-to-Query-IR faithfulness diagnostics.

Validation proves that a Query IR is executable; it does not prove that the IR
answers every clause in the user's question.  ``intent_faithfulness_why`` runs a
deliberately small set of high-confidence structural checks (this module's subject
and metric checks, and ``time_checks``, ``ranking_checks`` and ``filter_checks``)
whose realization is observable in Query IR.  A detected gap downgrades a
validating plan instead of guessing how to repair it.

The checks are compositional rather than pattern-specific.  A newly added
planner pattern automatically passes once its Query IR contains the requested
structure.
"""

from __future__ import annotations

import re
from typing import Any

from ..config_parts.measure_governance import (
    governing_metrics,
    population_governors,
    published_measure,
    unoffered_measures,
    whole_aggregate,
)
from ..visible_view import base_of, hidden_on_its_own
from ._base import (
    _canonical_measure,
    _canonical_metric,
    _name_fit,
    _name_matches,
    _named_metric,
    _object_by_id,
    _said_name,
    _singular,
    _tied_top,
    _tokens,
)
from .coverage import (
    _PRIOR_PERIOD_RE,
    CoverageGap,
    _coverage_why,
    _dict_nodes,
    _projected_subject_ids,
    _query_contains_prior_period,
    _referenced_ids,
)
from .filter_checks import (
    _contradictory_filter_gaps,
    _excluded_value_spans,
    _exclusion_matches,
    _filter_value_gaps,
    _positive_filter_evidence,
    _query_has_negative_semantics,
    _where_clause_gaps,
)
from .generators import _target_focus_text
from .intent_ir import IntentIR
from .ranking_checks import _ranking_gaps
from .snapshot import snapshot_day_gaps, snapshot_read
from .time_checks import (
    _caller_window_gaps,
    _fiscal_calendar_gaps,
    _role_window_why,
    _stock_as_of_gaps,
    _subject_window_gaps,
    _time_window_gaps,
)
from .time_windows import _time_window
from .visibility import visible_object_ids


def _selectable_subjects(config: Any, candidate_ids: list[str] | None = None) -> list[Any]:
    """Visible published subjects, with equivalent authored answers collapsed."""
    rows = [
        *config.metric_recipes,
        *(row for row in config.measures if getattr(row, "publish", True)),
    ]
    visible = set(visible_object_ids(config, (row.id for row in rows)))
    rows = [row for row in rows if row.id in visible]
    # A generated plain mirror and its measure are the same authored answer.
    measures = {row.id: row for row in config.measures}
    mirrors = set()
    for metric in config.metric_recipes:
        wrapped = whole_aggregate(metric)
        other = measures.get(wrapped[0]) if wrapped is not None and not wrapped[2] else None
        if wrapped is not None and (
            candidate_ids is not None
            and metric.id in candidate_ids
            and metric.id in visible
            and other is not None
            and other.id in candidate_ids
            and (wrapped[1] or other.default_aggregation) == other.default_aggregation
        ):
            mirrors.add(other.id)
        elif (
            wrapped is not None
            and other is not None
            and other.label == metric.label
            and other.name == metric.name
            and (
                candidate_ids is None
                or (wrapped[1] or other.default_aggregation) == other.default_aggregation
            )
        ):
            mirrors.add(metric.id)
    return [
        row
        for row in rows
        if row.id not in mirrors and (candidate_ids is None or row.id in candidate_ids)
    ]


def _shared_subjects(config: Any, text: str) -> list[Any]:
    """Whole analytic names that remain indistinguishable, independent of ranking.

    A label without its parenthetical counts, so the shared base of two variants
    ("Conversion rate (7d)", "Conversion rate (7d, same store)") never picks one.
    """

    rows = _selectable_subjects(config)
    fits = {}
    for row in rows:
        spans = _name_matches(row, text)
        if spans:
            fits[row.id] = (row, _name_fit(text, spans), spans)
    contenders = [
        row
        for row, words, _ in fits.values()
        if not any(words < other_words for _, other_words, _ in fits.values())
    ]
    return (
        sorted(contenders, key=lambda row: row.id)
        if len(contenders) > 1
        and any(
            (start, end) == (other_start, other_end)
            for row in contenders
            for _, start, end in fits[row.id][2]
            for other in contenders
            if other.id != row.id
            and (
                row.id.split(".")[0] == other.id.split(".")[0]
                # Existing whole-name metric precedence settles measure/metric labels.
                # An authored synonym crossing that boundary must still clarify.
                or any(
                    _singular(text[start:end].lower()) == _singular(alias.lower())
                    for candidate in (row, other)
                    for alias in (candidate.aliases or [])
                )
            )
            for _, other_start, other_end in fits[other.id][2]
        )
        else []
    )


_PARTITIONED_RANK_RE = re.compile(
    r"\b(?:within|inside)\s+(?:each|every)\b|"
    r"\b(?:in|for)\s+(?:each|every)\b",
    re.IGNORECASE,
)
_RANK_RE = re.compile(
    r"\b(?:top|bottom)\s+\d+\b|\brank(?:ed|ing)?\b|"
    r"\b\d+\s+(?:highest|lowest|best|worst)\b",
    re.IGNORECASE,
)


_SUBJECT_CONJUNCTION_RE = re.compile(
    r"\s+(?:and|plus|as\s+well\s+as|along\s+with|together\s+with)\s+|\s*,\s*", re.IGNORECASE
)
# The preposition that opens a time clause, cut off with the clause.
_TIME_LEAD_RE = re.compile(
    r"\s+(?:in|for|during|from|between|on|over|of)(?:\s+the)?\s*$", re.IGNORECASE
)
_SUBJECT_BOUNDARY_RE = re.compile(
    r"\s+(?:by|where|during|over\s+time|for\s+(?:customers?|stores?|accounts?|users?)|"
    r"with\s+(?:at\s+least|more\s+than|over|under))\b",
    re.IGNORECASE,
)
_SUBJECT_FILLER = frozenset(
    {
        "a",
        "all",
        "average",
        "avg",
        "calculate",
        "count",
        "daily",
        "give",
        "historical",
        "how",
        "is",
        "list",
        "many",
        "me",
        "monthly",
        "of",
        "please",
        "quarterly",
        "show",
        "sum",
        "the",
        "total",
        "was",
        "weekly",
        "what",
        "yearly",
    }
)


def intent_faithfulness_why(
    runtime: Any,
    *,
    question: str,
    intent_ir: IntentIR,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a structured downgrade reason for high-confidence coverage gaps.

    A window in the caller's ``partial_query`` settles the question's time
    window, as it does for ``TIME_WINDOW_UNRESOLVED``.
    """

    gaps: list[CoverageGap] = []

    text = str(question or "")
    named = _named_metric(runtime._config, text)
    if named is not None and str(named[0].id) in _referenced_ids(query):
        # The chosen metric answers its own name ("revenue, trailing 7 days").
        text = named[1]
    elif named is not None:
        gaps.append(
            CoverageGap(
                kind="named_metric_unrealized",
                clause=str(named[0].label),
                message="The question names a governed metric, but the draft doesn't use it.",
                expected={"metric": str(named[0].id)},
                actual={"subjects": _projected_subject_ids(query)},
                recovery_hint={
                    "kind": "use_named_metric",
                    "message": "Select the named metric in Query IR, then validate.",
                },
            )
        )
    reported, subjects = (named[0].id if named else ""), [term.id for term in intent_ir.subjects]
    caller = partial_query or {}
    gaps.extend(_governed_metric_gaps(runtime._config, question, query, caller, reported, subjects))
    partition_match = _PARTITIONED_RANK_RE.search(text)
    if (
        partition_match
        and (intent_ir.is_top_intent or _RANK_RE.search(text))
        and not _query_has_partitioned_ranking(query)
    ):
        gaps.append(
            CoverageGap(
                kind="partitioned_ranking_unrealized",
                clause=partition_match.group(0),
                message=(
                    "The question requests ranking within each group, but the draft only proves "
                    "a global ordering/limit."
                ),
                expected={"ranking_scope": "partitioned"},
                actual={
                    "group_by": list(query.get("group_by") or []),
                    "order_by": list(query.get("order_by") or []),
                    "limit": query.get("limit"),
                    "partition_by": [],
                },
                recovery_hint={
                    "kind": "author_partitioned_ranking",
                    "message": (
                        "Do not execute this as within-group top-N. Author an explicitly "
                        "partitioned ranking through a supported Query IR/relation shape, or "
                        "run one governed query per parent group."
                    ),
                },
            )
        )

    period_match = _PRIOR_PERIOD_RE.search(text)
    if period_match and not _query_contains_prior_period(runtime, query):
        gaps.append(
            CoverageGap(
                kind="prior_period_comparison_unrealized",
                clause=period_match.group(0),
                message=(
                    "The question requests a prior-period comparison, but no prior-period "
                    "expression or governed prior-period metric is present."
                ),
                expected={"expression_kind": "prior_period"},
                actual={"selected_expression_kinds": _selected_expression_kinds(query)},
                recovery_hint={
                    "kind": "rephrase_or_author_prior_period",
                    "message": (
                        "Use a supported phrase such as 'revenue vs prior year', or author a "
                        "second select with kind 'prior_period' and validate it."
                    ),
                },
            )
        )

    # Named-value coverage suppresses a reversal when this check reports it,
    # so every exclusion clause must be inspected, not only the first one.
    negation_matches = _exclusion_matches(text)
    excluded_spans = _excluded_value_spans(text)
    for negation_match in negation_matches:
        excluded_span = next(
            (span for span in excluded_spans if span[0] == negation_match.start("value")),
            negation_match.span("value"),
        )
        excluded_text = text[excluded_span[0] : excluded_span[1]].strip()
        positive_filters = _positive_filter_evidence(runtime, query, excluded_text)
        reversed_clause = bool(positive_filters)
        negative_present = _query_has_negative_semantics(query)
        # A matching positive predicate is still a reversal when an unrelated
        # (or even contradictory) negative predicate also happens to exist.
        if reversed_clause or not negative_present:
            gaps.append(
                CoverageGap(
                    kind=("negation_reversed" if reversed_clause else "negation_unrealized"),
                    clause=negation_match.group(0).strip(),
                    message=(
                        "The excluded value is encoded by a positive filter, reversing the request."
                        if reversed_clause
                        else "The question contains an exclusion, but the draft has no negative predicate."
                    ),
                    expected={"filter_polarity": "negative", "excluded_text": excluded_text},
                    actual={
                        "where": list(query.get("where") or []),
                        "positive_matches": positive_filters,
                        "negative_predicate_present": negative_present,
                    },
                    recovery_hint={
                        "kind": "provide_negative_filter",
                        "message": (
                            "Pass an explicit Query IR/partial_query filter using != or NOT IN for "
                            "the excluded value, then validate before execution."
                        ),
                    },
                )
            )

    requested_subjects = _conjoined_subjects(runtime, text)
    if len(requested_subjects) >= 2:
        projected = set(_projected_subject_ids(query))
        missing = [row for row in requested_subjects if projected.isdisjoint(row["candidate_ids"])]
        if missing:
            gaps.append(
                CoverageGap(
                    kind="multiple_subjects_unrealized",
                    clause=" and ".join(row["phrase"] for row in requested_subjects),
                    message=(
                        "The question clearly requests multiple governed subjects, but one or "
                        "more are absent from the top-level select list."
                    ),
                    expected={"subjects": requested_subjects},
                    actual={"projected_subject_ids": sorted(projected), "missing": missing},
                    recovery_hint={
                        "kind": "provide_multiple_selects",
                        "message": (
                            "Use 'X vs Y' for a supported side-by-side comparison, or provide "
                            "one explicit Query IR select entry per requested subject."
                        ),
                    },
                )
            )

    role_window_why = _role_window_why(runtime, text, query)
    if role_window_why is not None:
        return role_window_why
    caller_time = (partial_query or {}).get("time")
    # A balance read on the closing day of the question's window answers that window.
    read = snapshot_read(runtime, question, query)
    if isinstance(caller_time, dict) and any(
        caller_time.get(key) for key in ("start", "end", "range")
    ):
        # The window must agree in the planning zone. Existing holds read it in UTC, and the
        # planning zone must not admit an explicit interval they held.
        gaps.extend(
            _caller_window_gaps(runtime, text, query)
            or _caller_window_gaps(runtime, text, query, timezone="UTC")
        )
    elif read is None:
        gaps.extend(_time_window_gaps(runtime, text, query))
    gaps.extend(_fiscal_calendar_gaps(runtime._config, text, query))
    gaps.extend(_subject_window_gaps(runtime._config, query))
    # Unconsumed as-of cues have their own TIME_WINDOW_UNRESOLVED hold; the balance's
    # independent completion/subject checks still apply, including to caller windows.
    stock_gaps = (
        _stock_as_of_gaps(runtime._config, query)
        if (
            read is not None
            or not _time_window(question, policy_context=query.get("policy_context")).as_of
        )
        else []
    )
    gaps.extend(stock_gaps or snapshot_day_gaps(runtime, question, query, partial_query))
    gaps.extend(_ranking_gaps(runtime, text, query))
    gaps.extend(_ambiguous_grouping_gaps(text, query, partial_query or {}))
    gaps.extend(_where_clause_gaps(runtime, text, query))
    contradictions = _contradictory_filter_gaps(query)
    if contradictions:
        # No row can satisfy the draft. Report that decisive failure once;
        # value-specific absences are consequences of the same contradiction.
        gaps.extend(contradictions)
    else:
        filter_text = text
        if len(requested_subjects) >= 2 and not missing:
            filter_text = _conjoined_filter_text(text, requested_subjects)
        gaps.extend(_filter_value_gaps(runtime, filter_text, query))

    why = _coverage_why(gaps)
    if why is not None and any(gap.kind == "multiple_subjects_unrealized" for gap in gaps):
        objects = {
            row.id: row for row in [*runtime._config.measures, *runtime._config.metric_recipes]
        }
        why["details"]["parts"] = [
            {
                **part,
                "temporal_roles": [
                    str(
                        getattr(objects[key], "temporal_role", "")
                        or getattr(objects[key], "default_temporal_role", "")
                    )
                    for key in part["candidate_ids"]
                ],
            }
            for part in requested_subjects
        ]
    return why


def _ambiguous_grouping_gaps(
    text: str, query: dict[str, Any], partial_query: dict[str, Any]
) -> list[CoverageGap]:
    """Refuse when the draft adds a grouping beside the caller's ``group_by``.

    Whether the question's grouping phrase restates a caller dimension or asks
    for another one is not decided by matching names: the caller confirms by
    passing every intended dimension ID in ``group_by``.
    """

    from .generators import _requested_grouping_terms  # noqa: WPS433

    authored = set(partial_query.get("group_by") or [])
    added = set(query.get("group_by") or []) - authored
    if not authored or not added:
        return []
    return [
        CoverageGap(
            kind="ambiguous_grouping",
            clause=", ".join(_requested_grouping_terms(text)) or text,
            message="The draft adds a grouping dimension the caller's group_by does not include.",
            actual={"dimension_ids": sorted(authored | added)},
            recovery_hint={
                "kind": "clarify_grouping",
                "message": "Pass every intended grouping dimension ID in group_by.",
            },
        )
    ]


def _governed_metric_gaps(
    config: Any,
    question: str,
    query: dict[str, Any],
    partial_query: dict[str, Any],
    reported: str,
    subjects: list[str],
) -> list[CoverageGap]:
    """Hold drafts over unoffered measures or measures governed by a fitting metric.

    The draft selects the measure itself, or the metric that is its plain aggregate, and does
    not select a metric that aggregates the measure through a filter while the question's
    whole question names that metric, or the package doesn't offer the measure. Otherwise a visible
    metric that narrows its rows holds it (``_population_hold``). A measure or metric the
    caller's ``partial_query.select`` names by id is the caller's choice; nothing else in the
    request names one. ``reported`` already has its own gap.
    """

    choices = partial_query.get("select")
    caller = {
        node[key]
        for item in (choices if isinstance(choices, list) else [])
        if isinstance(item, dict)
        for node in (item, item.get("expression"))
        if isinstance(node, dict)
        for key in ("measure", "metric")
        if isinstance(node.get(key), str)
    }
    selected = list(
        dict.fromkeys(
            node[key]
            for node in _dict_nodes(list(query.get("select") or []))
            for key in ("measure", "metric")
            if isinstance(node.get(key), str)
        )
    )
    # Governance is enforcement: it reads the whole package, not the caller's view.
    try:
        unoffered: frozenset[str] | None = unoffered_measures(base_of(config))
    except Exception:  # noqa: BLE001 — an unreadable offer cannot make a draft ready
        unoffered = None
    gaps: list[CoverageGap] = []
    for object_id in selected:
        plain = _object_by_id(config.metric_recipes, object_id)
        measure_id = published_measure(plain) if plain is not None else object_id
        if not measure_id or {object_id, measure_id} & caller:
            continue
        offered = unoffered is not None and measure_id not in unoffered
        governing = governing_metrics(config, measure_id) if unoffered is not None else []
        visible = set(visible_object_ids(config, (metric.id for metric in governing)))
        governing = [metric for metric in governing if metric.id in visible]
        metrics = [
            metric.id
            for metric in governing
            if metric.id not in selected
            and metric.id != reported
            and (not offered or _said_name(metric, question))
        ]
        expected: dict[str, Any] | None = {"metrics": metrics}
        if not metrics and (offered or governing):
            expected = _population_hold(config, measure_id, query, [*selected, reported], subjects)
        if expected is None:
            continue
        measure = _object_by_id(config.measures, measure_id)
        gaps.append(
            CoverageGap(
                kind="governed_metric_unrealized",
                clause=str(getattr(measure, "label", "") or measure_id),
                message=(
                    "The draft reads this measure without the filter of a governed metric "
                    "that fits the question."
                    if metrics
                    else "The package's governed metrics leave out some of this measure's rows, "
                    "and the draft counts all of them."
                    if expected.get("metrics")
                    else "A definition you can't see governs this measure, so it can't be "
                    "answered as a raw number."
                    if "narrowed_by" in expected
                    else "The package doesn't offer this measure."
                ),
                expected=expected,
                actual={"measure": measure_id},
                recovery_hint={
                    "kind": "use_governed_metric",
                    "message": (
                        "Select the governed metric in Query IR. Name the measure by id in "
                        "partial_query.select only when the question asks for every row it counts."
                        if expected["metrics"] or "narrowed_by" in expected
                        else "Name the measure by id in partial_query.select when the question asks "
                        "for every row it counts, or pick an offered metric."
                    ),
                },
            )
        )
    return gaps


def _population_hold(
    config: Any, measure_id: str, query: dict[str, Any], skipped: list[str], subjects: list[str]
) -> dict[str, Any] | None:
    """The gap's ``expected`` for whole-package metrics narrowing the measure's rows that the
    draft neither selects (``skipped``) nor filters or groups by, else ``None``. Fails closed.
    A metric hidden in its own right counts for nothing; ``expected`` names only the view."""

    filters = {key: query.get(key) for key in ("where", "group_by", "metric_filters")}
    drafted = set(_referenced_ids(filters))
    try:
        governors = population_governors(base_of(config), measure_id)
    except Exception:  # noqa: BLE001 — an unreadable metric cannot make a draft ready
        return {"metrics": []}
    found = {
        row.id: dims
        for row, dims in governors
        if row.id not in skipped and not hidden_on_its_own(config, row.id) and not dims & drafted
    }
    if not found:
        return None
    shown = {row.id for row in [*config.metric_recipes, *config.dimensions]}
    ranked = [*dict.fromkeys([*(key for key in subjects if key in found), *sorted(found)])]
    metrics = [key for key in ranked if key in shown][:5]
    return {"metrics": metrics, "narrowed_by": sorted(shown & set().union(*found.values()))}


def named_subject_why(
    runtime: Any, question: str, partial_query: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """A shared whole name cannot be settled by the ranking's label or score."""

    parts = _conjoined_subjects(runtime, question)
    projected = set(_projected_subject_ids(partial_query or {}))
    rows = []
    for part in parts:
        rows = _selectable_subjects(runtime._config, part["candidate_ids"])
        if len(rows) >= 2 and not any(row.id in projected for row in rows):
            rows = sorted(rows, key=lambda row: row.id)
            break
        rows = []
    if not parts:
        rows = _shared_subjects(runtime._config, question)
    if not rows or any(row.id in projected for row in rows):
        return None
    return _coverage_why(
        [
            CoverageGap(
                kind="subject_ambiguous",
                clause=_target_focus_text(question),
                message="The question names more than one selectable subject.",
                expected={"candidates": [row.id for row in rows], "candidate_count": len(rows)},
                actual={},
                recovery_hint={
                    "kind": "name_one_subject",
                    "message": "Ask again naming the one you mean: "
                    + " or ".join(f"{row.label} ({row.id})" for row in rows)
                    + ".",
                },
            )
        ]
    )


def intent_subject_why(
    runtime: Any,
    *,
    question: str,
    intent_ir: IntentIR,
    query: dict[str, Any],
    partial_query: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """A coverage gap when the ranking tied the draft's one subject with others
    and neither the question nor the caller's ``partial_query`` names that
    subject (see ``_base._tied_top``).

    ``plan`` reports it after every other reason, which says more.
    """

    config, text = runtime._config, str(question or "")
    collision = named_subject_why(runtime, text, partial_query)
    if collision is not None:
        return collision
    subjects = _projected_subject_ids(query)
    measure = bool(subjects) and subjects[0].startswith("measure.")
    terms = set(intent_ir.target_measure_terms)
    canonical = (_canonical_measure if measure else _canonical_metric)(config, terms)
    if (
        len(subjects) != 1
        or subjects[0] in _projected_subject_ids(partial_query or {})
        or getattr(canonical, "id", None) == subjects[0]
        or _named_metric(config, text)
    ):
        return None
    tied, named = _tied_top(
        config.measures if measure else config.metric_recipes,
        terms,
        set(_tokens(_target_focus_text(text))) or terms,
    )
    ids = [row.id for row in tied]
    if len(ids) < 2 or subjects[0] not in ids or getattr(named, "id", None) == subjects[0]:
        return None
    candidates = " or ".join(f"{row.label} ({row.id})" for row in tied[:5])
    gap = CoverageGap(
        kind="subject_ambiguous",
        clause=_target_focus_text(text),
        message="The question fits these equally well, and the draft picked one of them.",
        expected={"candidates": ids[:5], "candidate_count": len(ids)},
        actual={"subjects": subjects},
        recovery_hint={
            "kind": "name_one_subject",
            "message": f"Ask again naming the one you mean: {candidates}.",
        },
    )
    return _coverage_why([gap])


def _query_has_partitioned_ranking(query: dict[str, Any]) -> bool:
    for node in _dict_nodes(query):
        partition = node.get("partition_by")
        if partition not in (None, "", [], {}):
            kind = str(node.get("kind", "") or "").casefold()
            if kind in {"rank", "ranking", "top_n", "row_number"} or any(
                key in node for key in ("order_by", "limit", "rank")
            ):
                return True
    return False


def _selected_expression_kinds(query: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    for item in list(query.get("select") or []):
        expression = item.get("expression") if isinstance(item, dict) else None
        if not isinstance(expression, dict):
            continue
        kind = str(expression.get("kind", "") or "")
        if not kind:
            kind = "metric" if expression.get("metric") else "measure"
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def _conjoined_subjects(runtime: Any, text: str) -> list[dict[str, Any]]:
    """Return exact catalog subjects conjoined in the target phrase.

    This intentionally refuses fuzzy resolution.  Each side must exactly match
    an authored id suffix, name, label, or alias after harmless request words
    are removed.  Qualification clauses are cut off before splitting so
    supported "target for stores with orders and sessions" patterns do not
    become false multi-select requests.
    """

    target_text = _SUBJECT_BOUNDARY_RE.split(str(text or ""), maxsplit=1)[0]
    # The time clause is no part of the last subject: "revenue and orders in Q1 2017".
    for start, end in sorted(_time_window(target_text).spans, reverse=True):
        target_text = _TIME_LEAD_RE.sub("", target_text[:start]) + target_text[end:]
    pieces = [
        piece.strip() for piece in _SUBJECT_CONJUNCTION_RE.split(target_text) if piece.strip()
    ]
    if len(pieces) < 2:
        return []
    rows = [
        *getattr(runtime._config, "measures", []),
        *getattr(runtime._config, "metric_recipes", []),
    ]
    matches: list[dict[str, Any]] = []
    for piece in pieces:
        piece_tokens = _subject_tokens(piece)
        if not piece_tokens:
            return []
        candidate_ids: list[str] = []
        for row in rows:
            if _matches_exact_subject_field(row, piece_tokens):
                object_id = str(getattr(row, "id", "") or "")
                if object_id and object_id not in candidate_ids:
                    candidate_ids.append(object_id)
        if not candidate_ids:
            return []
        matches.append({"phrase": piece, "candidate_ids": candidate_ids})
    return matches


def _conjoined_filter_text(text: str, subjects: list[dict[str, Any]]) -> str:
    """Selected subject names consume only their first occurrence, never a later filter."""
    for part in subjects:
        text = text.replace(part["phrase"], "", 1)
    return text


def _matches_exact_subject_field(row: Any, piece_tokens: tuple[str, ...]) -> bool:
    object_id = str(getattr(row, "id", "") or "")
    id_suffix = object_id.rsplit(".", 1)[-1]
    label = str(getattr(row, "label", "") or "")
    fields = (
        object_id,
        id_suffix,
        str(getattr(row, "name", "") or ""),
        label,
        # "item revenue" names "Item revenue (USD)".
        re.sub(r"\s*\(.*?\)", "", label),
        *[str(value) for value in list(getattr(row, "aliases", []) or [])],
    )
    # Filler goes on both sides: "order count" names the "Order count" measure.
    return any(_subject_tokens(value) == piece_tokens for value in fields if value)


def _subject_tokens(text: str) -> tuple[str, ...]:
    return tuple(token for token in _tokens(text) if token not in _SUBJECT_FILLER)


__all__ = [
    "intent_faithfulness_why",
]
