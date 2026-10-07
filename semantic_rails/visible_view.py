"""The caller's visible view: the package without the objects hidden from them.

Caller-facing work reads the view; enforcement reads the whole package. Hidden objects are
closed over compiler reads and declared references, including unbindable dependents. The
builder marks the view's provenance, filters rows and projects every field of every remaining
record by its class (:data:`ROW_FIELDS`), so no authored text names a hidden object, without
changing enforcement inputs. ``resource_access.run_authorized_operation`` pins the
view for the request; ``Runtime._config`` and ``Runtime.registry`` serve it.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Any

from .compiler import BoundQuery, bind_metadata_objects, bind_query
from .compiler_parts.indexes import ViewOf, VisiblePackageConfig, get_package_analysis
from .errors import SemanticLayerError
from .policy_rules import hidden_policy_ids, visible_only_listed
from .registry import Registry
from .request_context import RequestContext, context_from_policy_context
from .schema import (
    AccumulationConfig,
    AggregateRelationConfig,
    ConnectionSpec,
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MeasureExternalDiscontinuity,
    MeasureValidityWindow,
    MetricConfig,
    PackageConfig,
    PackageMeta,
    PathPolicyConfig,
    PathPreferenceConfig,
    PlannerConfig,
    RelationConfig,
    RelationPipelineStep,
    RelationshipConfig,
    SeedSpec,
    SegmentConfig,
    SemanticCaveatConfig,
    SemanticPolicyConfig,
    TemporalRoleConfig,
    ValueDomainConfig,
    ValueDomainValue,
)
from .schema import base_of as base_of
from .schema import require_base as require_base
from .segments import build_segment_query, normalize_segment

OBJECT_FIELDS = (
    "entities",
    "dimensions",
    "temporal_roles",
    "relationships",
    "value_domains",
    "measures",
    "metric_recipes",
    "segments",
)
# Every PackageConfig field: rows the view filters (and projects), records it projects, or
# values it keeps.
FIELDS = {
    "version": "kept",
    "package": "prose",
    **dict.fromkeys(OBJECT_FIELDS, "filtered"),
    "path_preferences": "filtered",
    "path_policy": "kept",
    "semantic_policies": "filtered",
    "semantic_caveats": "filtered",
    "aggregate_relations": "filtered",
    "relations": "prose",
    "operational_contract": "prose",
    "meta_contract": "prose",
}


def _classes(
    kept: str,
    *,
    text: str = "",
    texts: str = "",
    identity: str = "",
    typed: str = "",
    rows: str = "",
) -> dict[str, str]:
    groups = {"kept": kept, "text": text, "texts": texts, "identity": identity, "typed": typed}
    return {
        name: kind for kind, names in {**groups, "rows": rows}.items() for name in names.split()
    }


# How the view shows each field of each record, when it names a hidden object (a token):
# kept: ids, numbers, flags, enums and physical names (table, column, expr), never changed;
# text: "" instead; texts: without the items (list) or entries (mapping) that name one;
# identity: a name rebuilt from the record's id, or a list without the items that name one;
# typed: a mapping's engine keys (TYPED_KEYS) kept, its other entries as texts;
# rows: nested records, each projected by its own row. A field missing here is classed by
# its value (:func:`_class_of`), so authored text is never kept by omission.
ROW_FIELDS: dict[type, dict[str, str]] = {
    SeedSpec: _classes("kind source post_sql null_strings"),
    ConnectionSpec: _classes("kind name options"),
    PlannerConfig: _classes("disabled_patterns"),
    PackageMeta: _classes(
        "package_id warehouse default_db seed connection environments schema_strict planner "
        "observation_scope",
        text="description",
        identity="name",
    ),
    EntityConfig: _classes(
        "id table primary_key kind relation_id key identifiers foreign_keys calendar_id "
        "allowed_as_root freshness_sla_seconds",
        text="label description freshness_source freshness_as_of",
        texts="key_roles foreign_key_roles topics disallowed_names",
        identity="name aliases",
    ),
    DimensionConfig: _classes(
        "id entity column data_type filterable groupable value_domain",
        text="label description semantic_kind sample_values_strategy",
        texts="topics preferred_filter_ops",
        identity="name aliases",
    ),
    TemporalRoleConfig: _classes(
        "id dimension temporal_class supported_grains default_query_time_axis timezone "
        "column_timezone",
        text="label",
        identity="name aliases",
    ),
    RelationshipConfig: _classes(
        "id source_entity target_entity source_column target_column cardinality safety "
        "source_columns target_columns source_key_role target_key_role allowed_directions "
        "target_key_type rollup_safe_aggregations_reverse",
        text="label description join_semantics",
        identity="name aliases",
        typed="temporal_validity",
    ),
    ValueDomainValue: _classes("value", text="description", identity="label aliases"),
    ValueDomainConfig: _classes(
        "id dimensions", text="label description", identity="name", rows="values"
    ),
    AccumulationConfig: _classes("kind snapshot"),
    MeasureValidityWindow: _classes("from_ to", text="semantics"),
    MeasureExternalDiscontinuity: _classes("from_ to magnitude_estimate_pct", text="what"),
    MeasureConfig: _classes(
        "id entity row_grain expr default_aggregation allowed_aggregations source_relation "
        "invalid_aggregations measure_class accumulation compatible_temporal_roles value_type "
        "suggested_aggregations comparison_peers clock_variants preferred_companion_metrics "
        "default_temporal_role cross_window_policy additive lookup_from lookup_via publish",
        text="currency label description comparison_family comparison_mode",
        texts="topics example_entries authoring_warnings",
        identity="name aliases",
        typed="operational meta",
        rows="validity_windows external_discontinuities",
    ),
    MetricConfig: _classes(
        "id kind expression temporal_role compatible_temporal_roles filter_spec window_spec "
        "comparison_peers clock_variants preferred_companion_metrics value_type",
        text="label description comparison_family comparison_mode",
        texts="topics example_entries",
        identity="name aliases",
        typed="operational meta",
    ),
    SegmentConfig: _classes(
        "id entity basis_metric preview_dimensions where metric_filters time "
        "temporal_role_overrides",
        text="label description",
        texts="topics",
        identity="name aliases",
    ),
    PathPreferenceConfig: _classes("source_entity target_entity relationship_path", text="label"),
    PathPolicyConfig: _classes("max_hops"),
    # Policies and caveats are shown whole or in a generic form (:func:`display_policy`,
    # :func:`_display_caveat`); their classes say which fields decide that.
    SemanticPolicyConfig: _classes(
        "kind object_ids audiences environments roles action",
        text="rationale",
        identity="id",
        typed="config",
    ),
    SemanticCaveatConfig: _classes(
        "kind object_ids audiences environments severity",
        text="message owner",
        texts="entity_values references",
        identity="id",
        typed="time config",
    ),
    AggregateRelationConfig: _classes(
        "id relation source_entity measures dimensions temporal_role grain entity_grain "
        "freshness_sla_seconds model_id variant_id source time_column eligible_time_grains "
        "measure_columns measure_rollups measure_aggregations measure_holds dimension_columns "
        "dimension_paths excluded_entities excluded_dimensions selection_priority "
        "equivalence_kind",
        text="description freshness_source freshness_as_of",
    ),
    RelationPipelineStep: _classes("kind config"),
    RelationConfig: _classes(
        "id output_name columns",
        text="label description",
        identity="name",
        typed="meta",
        rows="steps",
    ),
}
# A typed mapping's keys the engine reads: the export hint (as the boolean the engine tests)
# and a historical join's physical columns.
TYPED_KEYS: dict[str, dict[str, Callable[[Any], Any]]] = {
    "meta": {"mnpi": bool},
    "operational": {"mnpi": bool},
    "temporal_validity": {"valid_from": str, "valid_to": str},
}
# A policy's own words, replaced by its action's engine text when they may name a hidden object.
POLICY_TEXT = ("rule", "rationale", "description")
ENGINE_TEXT = {
    "deny": "Access to this object is denied by policy.",
    "withhold_values": "Values of this object are withheld by policy.",
    "constrain": "Queries on this object are constrained by policy.",
    "protected": "This object is protected by policy.",
    "label": "The package's release label.",
}
VISIBILITY_UNRESOLVED = "visibility_unresolved"


def unresolved() -> SemanticLayerError:
    """The refusal when what this caller may see is uncertain; it names nothing."""
    return SemanticLayerError(
        "POLICY_DENIED",
        "What this request may see could not be resolved.",
        details={"reason": VISIBILITY_UNRESOLVED},
    )


def _rows(config: PackageConfig) -> list[Any]:
    return [row for name in OBJECT_FIELDS for row in getattr(config, name)]


def bound_object_ids(binding: BoundQuery) -> frozenset[str]:
    """Every object a bound query reads, including what each root leaf computes."""
    return binding.object_ids.union(*binding.leaf_objects.values())


def _object_reads(config: PackageConfig) -> dict[str, frozenset[str] | None]:
    analysis = get_package_analysis(config)
    if analysis.object_reads is None:
        # A fresh context, so an outer binding never records these reads as its own.
        analysis.object_reads = contextvars.Context().run(_bind_object_reads, config)
    return analysis.object_reads


def _bind_object_reads(config: PackageConfig) -> dict[str, frozenset[str] | None]:
    """What the compiler reads to answer each object: a recipe's default invocation, a
    segment's query, a value domain's dimensions, a relationship's entities."""
    reads: dict[str, frozenset[str] | None] = {}
    for row in _rows(config):
        try:
            if isinstance(row, SegmentConfig):
                segment = normalize_segment(config, row.id)
                query = build_segment_query(segment, include_preview_dimensions=True)
                reads[row.id] = bound_object_ids(bind_query(config, None, query))
                continue
            linked = (
                row.dimensions
                if isinstance(row, ValueDomainConfig)
                else [row.source_entity, row.target_entity]
                if isinstance(row, RelationshipConfig)
                else []
            )
            reads[row.id] = bind_metadata_objects(config, [row.id, *linked])
        except Exception:  # noqa: BLE001 — unknown reads cannot authorize disclosure
            reads[row.id] = None
    return reads


def _strings(value: Any) -> Iterator[str]:
    """Every string in ``value``: dataclass fields, mapping keys and values, list items."""
    if isinstance(value, str):
        yield value
    elif is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            yield from _strings(getattr(value, item.name))
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield from _strings(key)
            yield from _strings(child)
    elif isinstance(value, list | tuple | set | frozenset):
        for child in value:
            yield from _strings(child)


def _names(value: Any, ids: Iterable[str]) -> bool:
    known = set(ids)
    return bool(known) and any(text in known for text in _strings(value))


def _declared_references(config: PackageConfig) -> dict[str, frozenset[str]]:
    analysis = get_package_analysis(config)
    if analysis.declared_references is None:
        ids = {row.id for row in _rows(config)}
        analysis.declared_references = {
            row.id: frozenset(_strings(row)) & ids - {row.id} for row in _rows(config)
        }
    return analysis.declared_references


def hidden_object_ids(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> frozenset[str]:
    """The objects hidden from this context, one rule for ``hidden`` and ``visible_only``:
    each one listed, and every object whose reads or declared references reach one."""
    scope: dict[str, Any] = {"environment": environment, "audience": audience, "roles": roles}
    hidden = hidden_policy_ids(config, **scope) | visible_only_listed(config, **scope)
    if not hidden:
        return frozenset()
    reads, names = _object_reads(config), _declared_references(config)
    hidden |= {object_id for object_id, read in reads.items() if read is None}
    while more := {
        object_id
        for object_id, read in reads.items()
        if object_id not in hidden and ((read or frozenset()) & hidden or names[object_id] & hidden)
    }:
        hidden |= more
    return frozenset(hidden)


_EDGE = "(?![A-Za-z0-9_])"


def _word(text: str) -> re.Pattern[str]:
    return re.compile(f"(?<![A-Za-z0-9_]){re.escape(text)}{_EDGE}", re.IGNORECASE)


def _identities(row: Any) -> list[str]:
    names = [getattr(row, "name", ""), getattr(row, "label", ""), *getattr(row, "aliases", [])]
    return [row.id, *(text.strip() for text in names if text and text.strip())]


def token_pattern(base: PackageConfig, hidden: frozenset[str]) -> re.Pattern[str] | None:
    """A hidden object's tokens in text: its id, and each name, label or alias that no visible
    object's id, name, label or alias contains as a word (case-insensitive)."""
    rows = [row for row in _rows(base) if row.id in hidden]
    if not rows:
        return None
    visible = "\n".join(
        text for row in _rows(base) if row.id not in hidden for text in _identities(row)
    )
    ids = sorted({re.escape(row.id) for row in rows}, key=len, reverse=True)
    words = sorted(
        {
            re.escape(text)
            for row in rows
            for text in _identities(row)[1:]
            if len(text) > 1 and not _word(text).search(visible)
        },
        key=len,
        reverse=True,
    )
    pattern = f"(?:{'|'.join(ids)}){_EDGE}"
    if words:
        pattern += f"|(?i:(?<![A-Za-z0-9_])(?:{'|'.join(words)}){_EDGE})"
    return re.compile(pattern)


def _mentions(value: Any, tokens: re.Pattern[str] | None) -> bool:
    """Whether ``value`` names a hidden object: each string where :func:`_strings` looks, as
    authored (never serialized), and any other leaf but a number, flag or None as ``str``."""
    if tokens is None or not value:
        return False
    if isinstance(value, str):
        return bool(tokens.search(value))
    if is_dataclass(value) and not isinstance(value, type):
        return any(_mentions(getattr(value, item.name), tokens) for item in fields(value))
    if isinstance(value, Mapping):
        return any(
            _mentions(key, tokens) or _mentions(child, tokens) for key, child in value.items()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(_mentions(child, tokens) for child in value)
    return not isinstance(value, bool | int | float) and bool(tokens.search(str(value)))


def _class_of(value: Any) -> str:
    """The class of a field :data:`ROW_FIELDS` misses: nested records are projected, any
    other text, list or mapping is authored; only numbers and flags are kept."""
    items = value if isinstance(value, list) else [value]
    if items and all(is_dataclass(item) and not isinstance(item, type) for item in items):
        return "rows"
    if isinstance(value, str):
        return "text"
    if isinstance(value, Mapping):
        return "typed"
    return "texts" if isinstance(value, list | tuple) else "kept"


def _rebuilt_name(row: Any) -> str:
    """A name that names no hidden object: a value's own value, else the id's last segment."""
    if isinstance(row, ValueDomainValue):
        return str(row.value)
    return str(getattr(row, "id", "") or getattr(row, "package_id", "")).rsplit(".", 1)[-1]


def _field_shown(row: Any, name: str, kind: str, value: Any, tokens: re.Pattern[str] | None) -> Any:
    if kind == "kept" or not value:
        return value
    if kind == "rows":
        if isinstance(value, list):
            return [_projected(item, tokens) for item in value]
        return _projected(value, tokens)
    if kind == "typed" and isinstance(value, Mapping):
        engine = TYPED_KEYS.get(name, {})
        return {
            key: engine[key](item) if key in engine else item
            for key, item in value.items()
            if key in engine or not _mentions([key, item], tokens)
        }
    if not _mentions(value, tokens):
        return value
    if kind == "identity" and isinstance(value, str):
        return _rebuilt_name(row)
    if isinstance(value, Mapping):
        return {key: item for key, item in value.items() if not _mentions([key, item], tokens)}
    if isinstance(value, list | tuple) and kind in {"texts", "identity"}:
        return [item for item in value if not _mentions(item, tokens)]
    return "" if isinstance(value, str) else type(value)()


def _projected(row: Any, tokens: re.Pattern[str] | None) -> Any:
    """``row`` as a caller sees it: each field shown by its class in :data:`ROW_FIELDS`."""
    if tokens is None:
        return row
    classes = ROW_FIELDS.get(type(row), {})
    changes = {}
    for item in fields(row):
        value = getattr(row, item.name)
        kind = classes.get(item.name) or _class_of(value)
        shown = _field_shown(row, item.name, kind, value, tokens)
        if shown is not value and shown != value:
            changes[item.name] = shown
    return replace(row, **changes) if changes else row


def _authored_mentions(row: Any, tokens: re.Pattern[str] | None) -> bool:
    """Whether any field of ``row`` that the view may change names a hidden object."""
    classes = ROW_FIELDS.get(type(row), {})
    return _mentions(
        [
            getattr(row, item.name)
            for item in fields(row)
            if (classes.get(item.name) or _class_of(getattr(row, item.name))) != "kept"
        ],
        tokens,
    )


def _visible(value: Any, hidden: frozenset[str]) -> Any:
    """``value`` without list items that are, or name, a hidden id."""
    if isinstance(value, list):
        return [_visible(item, hidden) for item in value if not _names([item], hidden)]
    if isinstance(value, Mapping):
        return {key: _visible(item, hidden) for key, item in value.items()}
    return value


def display_policy(
    policy: SemanticPolicyConfig, hidden: frozenset[str], tokens: re.Pattern[str] | None
) -> SemanticPolicyConfig | None:
    """A policy as this caller may see it, or None. Visibility policies are never shown; a
    policy listing a hidden object, or whose words may name one, shows the generic form: no
    id, and its action's engine text."""
    if policy.kind == "object_visibility":
        return None
    listed = [object_id for object_id in policy.object_ids if object_id not in hidden]
    if policy.object_ids and not listed:
        return None
    settings = _visible(dict(policy.config), hidden)
    if policy.kind == "row_filter" and settings.get("dimension") in hidden:
        return None
    words = [policy.id, policy.rationale, *(settings.get(key) for key in POLICY_TEXT)]
    # Its other settings as a typed mapping: without the entries that may name one.
    shown = _field_shown(policy, "config", "typed", settings, tokens)
    if len(listed) == len(policy.object_ids) and not _mentions(words, tokens):
        return replace(policy, object_ids=listed, config=shown)
    action = str(policy.action or settings.get("action", "")).strip().lower()
    return replace(
        policy,
        id="",
        rationale=ENGINE_TEXT.get(action or "constrain", "Governed by policy."),
        object_ids=listed,
        config={key: value for key, value in shown.items() if key not in POLICY_TEXT},
    )


def _display_caveat(
    caveat: SemanticCaveatConfig, hidden: frozenset[str], tokens: re.Pattern[str] | None
) -> SemanticCaveatConfig | None:
    """A caveat is shown whole, without the hidden objects it lists, or not at all."""
    listed = [object_id for object_id in caveat.object_ids if object_id not in hidden]
    if caveat.object_ids and not listed:
        return None
    named = [caveat.entity_values, caveat.time, caveat.references, caveat.config]
    if _names(named, hidden) or _authored_mentions(caveat, tokens):
        return None
    return replace(caveat, object_ids=listed)


def build_view(base: PackageConfig, hidden: frozenset[str]) -> PackageConfig:
    """The package as a caller with ``hidden`` objects sees it."""
    if not hidden:
        return base
    tokens = token_pattern(base, hidden)

    changes: dict[str, Any] = {}
    for name, kind in FIELDS.items():
        value = getattr(base, name)
        if kind == "filtered":
            if name == "semantic_policies":
                value = [display_policy(row, hidden, tokens) for row in value]
            elif name == "semantic_caveats":
                value = [_display_caveat(row, hidden, tokens) for row in value]
            else:
                value = [
                    _projected(row, tokens)
                    for row in value
                    if (row.id not in hidden if name in OBJECT_FIELDS else not _names(row, hidden))
                ]
            value = [row for row in value if row is not None]
        elif kind == "prose":
            if isinstance(value, list):
                value = [_projected(row, tokens) for row in value]
            elif is_dataclass(value):
                value = _projected(value, tokens)
            elif _mentions(value, tokens):
                value = type(value)()
        changes[name] = value
    return VisiblePackageConfig(**changes, view=ViewOf(base, hidden, tokens))


def public_effects(private: list[dict[str, Any]], config: PackageConfig) -> list[dict[str, Any]]:
    """The policy effects a response may carry: each private effect as its display policy
    states it, with hidden ids left out. Execution consumes the private effects unchanged."""
    meta = get_package_analysis(config).view
    if meta is None:
        return private
    from .policies import _base_policy_effect

    policies = {policy.id: policy for policy in meta.base.semantic_policies}
    out = []
    for effect in private:
        if "policy_id" not in effect:  # already stated for the caller, in its generic form
            out.append(effect)
            continue
        policy = policies.get(str(effect["policy_id"]))
        shown = display_policy(policy, meta.hidden, meta.tokens) if policy is not None else None
        if shown is None:
            continue
        public = _base_policy_effect(shown, action=effect["action"])
        for key in ("violations", "withheld_objects", "withheld_column"):
            if key in effect:
                public[key] = _visible(effect[key], meta.hidden)
        out.append(public)
    return out


def public_error(exc: SemanticLayerError, config: PackageConfig) -> SemanticLayerError:
    """A policy refusal as this caller may see it (:func:`public_effects`)."""
    meta = get_package_analysis(config).view
    if meta is None or exc.code != "POLICY_DENIED" or "policy_effects" not in exc.details:
        return exc
    effects = public_effects(exc.details["policy_effects"], config)
    details = {
        **exc.details,
        "blocked_objects": _visible(list(exc.details.get("blocked_objects", [])), meta.hidden),
        "policy_effects": effects,
        "policy_violations": [row for effect in effects for row in effect.get("violations", [])],
    }
    return (
        exc if details == exc.details else SemanticLayerError(exc.code, str(exc), details=details)
    )


@dataclass
class ViewEntry:
    """One view of one runtime generation, and the caches computed from it."""

    config: PackageConfig
    registry: Registry
    hidden: frozenset[str] = frozenset()
    catalog: dict[str, Any] | None = None
    search_index: Any = None
    resolved: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestView:
    """The view one request reads, pinned for the whole operation."""

    runtime: Any
    generation: int
    # Environment, audience and lowered roles: what visibility is computed from.
    key: tuple[str, str, frozenset[str]]
    entry: ViewEntry
    base: PackageConfig
    # The caller as the request named them: attributes and allowlists are enforcement inputs.
    policy_context: Mapping[str, Any]


# The views pinned in this request, innermost last: one per runtime.
_pinned: contextvars.ContextVar[tuple[RequestView, ...]] = contextvars.ContextVar(
    "visible_view", default=()
)


def visibility_key(context: RequestContext) -> tuple[str, str, frozenset[str]]:
    roles = frozenset(role.strip().lower() for role in context.roles if role.strip())
    return (context.environment, context.audience, roles)


def hidden_on_its_own(config: PackageConfig, object_id: str) -> bool:
    """Whether ``config`` hides ``object_id`` in its own right, not for what it reads or names."""
    view = get_package_analysis(config).view
    if view is None or object_id not in view.hidden:
        return False
    read = _object_reads(view.base).get(object_id)
    if read is None:
        return False
    named = read | _declared_references(view.base)[object_id]
    return not (named - {object_id}) & view.hidden


def _hidden_from(
    base: PackageConfig, context: RequestContext | Mapping[str, Any] | None
) -> frozenset[str]:
    if not isinstance(context, RequestContext):
        context = context_from_policy_context(context)
    return hidden_object_ids(
        base, environment=context.environment, audience=context.audience, roles=context.roles
    )


def view_of(base: PackageConfig, context: RequestContext | Mapping[str, Any] | None) -> Any:
    """A caller's view of ``base``, uncached, for code that holds no runtime (the CLI)."""
    return build_view(base, _hidden_from(base, context))


def view_for(runtime: Any, context: RequestContext | Mapping[str, Any] | None) -> ViewEntry:
    """The runtime's cached view for this caller's hidden set, built on first use."""
    hidden = _hidden_from(runtime.package_config, context)
    with runtime._cache_lock:
        entry = runtime._views.get(hidden)
    if entry is None:
        config = build_view(runtime.package_config, hidden)
        with runtime._cache_lock:
            entry = runtime._views.setdefault(hidden, ViewEntry(config, Registry(config), hidden))
    return entry


def pinned() -> RequestView | None:
    """The innermost view pinned in the current request, if any."""
    views = _pinned.get()
    return views[-1] if views else None


def pinned_view(runtime: Any) -> RequestView | None:
    """The view pinned for this runtime in the current request, if any."""
    view = next((view for view in _pinned.get() if view.runtime is runtime), None)
    if view is not None and view.generation != runtime._generation:
        raise unresolved()
    return view


def request_view(
    runtime: Any, policy_context: Mapping[str, Any] | None
) -> AbstractContextManager[RequestView]:
    """Pin the caller's view; nested operations inherit it and refuse a visibility change.
    An uncertain view refuses, naming nothing, without serving the base."""
    context = context_from_policy_context(policy_context)
    current = pinned_view(runtime)
    if current is not None:
        if policy_context and visibility_key(context) != current.key:
            raise unresolved()
        return nullcontext(current)
    try:
        entry = view_for(runtime, context)
    except Exception as exc:  # noqa: BLE001 — uncertainty refuses; an invalid package says so
        if isinstance(exc, SemanticLayerError) and exc.code == "INVALID_CONFIG":
            raise
        raise unresolved() from None
    view = RequestView(
        runtime=runtime,
        generation=runtime._generation,
        key=visibility_key(context),
        entry=entry,
        base=runtime.package_config,
        policy_context=dict(policy_context or {}),
    )
    return _pinned_scope(view)


@contextmanager
def _pinned_scope(view: RequestView) -> Iterator[RequestView]:
    token = _pinned.set((*_pinned.get(), view))
    try:
        yield view
    finally:
        _pinned.reset(token)
