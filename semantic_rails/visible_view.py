"""The caller's visible view: the package without the objects hidden from them.

Every caller-facing computation reads the caller's view; every enforcement decision reads the
whole package (the base); visibility never removes or changes a deny, withhold, constraint or
row filter. A hidden id is then the binder's own unknown id, in every position it reads.

The hidden set is what a matching ``hidden`` policy or an ineligible ``visible_only`` policy
lists, closed over every object whose compiler reads (``_object_reads``) or declared references
(a field of its row equal to an id) reach it; while anything is hidden, an object that cannot be
bound is hidden too. The view removes those rows and whatever names them (route rows, rollups,
caveats, policies, which it keeps for display only), and omits whole any authored prose that
names a hidden object (``FIELDS`` classifies every package field).

``resource_access.run_authorized_operation`` pins one :class:`RequestView` per request
(:func:`request_view`); ``Runtime._config`` and ``Runtime.registry`` serve it.
"""

from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace
from typing import Any

from .compiler import BoundQuery, bind_metadata_objects, bind_query
from .compiler_parts.indexes import ViewOf, get_package_analysis
from .errors import SemanticLayerError
from .policy_rules import hidden_policy_ids, policy_config, visible_only_listed
from .registry import Registry
from .request_context import RequestContext, context_from_policy_context
from .schema import (
    PackageConfig,
    RelationshipConfig,
    SegmentConfig,
    SemanticCaveatConfig,
    SemanticPolicyConfig,
    ValueDomainConfig,
)
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
# Every PackageConfig field: rows the view filters (and omits prose of), text it omits when it
# names a hidden object, or values it keeps.
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
# Authored text on a row: omitted whole when it names a hidden object.
PROSE = frozenset(
    {
        "description",
        "topics",
        "example_entries",
        "meta",
        "operational",
        "authoring_warnings",
        "validity_windows",
        "external_discontinuities",
    }
)
# A policy's own words, replaced by its action's engine text when they may name a hidden object.
POLICY_TEXT = ("rule", "rationale", "description")
ENGINE_TEXT = {
    "deny": "Access to this object is denied by policy.",
    "redact": "Access to this object is denied by policy.",
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
    if tokens is None or not value:
        return False
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return bool(tokens.search(text))


def _empty(item: Any) -> Any:
    return item.default_factory() if item.default is MISSING else item.default


def _without_prose(row: Any, tokens: re.Pattern[str] | None) -> Any:
    """``row`` with each authored text field that names a hidden object omitted, whole."""
    if tokens is None:
        return row
    changes = {
        item.name: _empty(item)
        for item in fields(row)
        if item.name in PROSE and _mentions(getattr(row, item.name), tokens)
    }
    if isinstance(row, ValueDomainConfig) and _mentions(
        [v.description for v in row.values], tokens
    ):
        changes["values"] = [
            replace(value, description="") if _mentions(value.description, tokens) else value
            for value in row.values
        ]
    if hasattr(row, "relationship_path") and _mentions(row.label, tokens):
        changes["label"] = ""
    return replace(row, **changes) if changes else row


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
    settings = _visible(policy_config(policy), hidden)
    if policy.kind == "row_filter" and settings.get("dimension") in hidden:
        return None
    words = [policy.id, policy.rationale, *(settings.get(key) for key in POLICY_TEXT)]
    if len(listed) == len(policy.object_ids) and not _mentions(words, tokens):
        return replace(policy, object_ids=listed, config=settings)
    action = str(policy.action or settings.get("action", "")).strip().lower()
    return replace(
        policy,
        id="",
        rationale=ENGINE_TEXT.get(action or "constrain", "Governed by policy."),
        object_ids=listed,
        config={key: value for key, value in settings.items() if key not in POLICY_TEXT},
    )


def _display_caveat(
    caveat: SemanticCaveatConfig, hidden: frozenset[str], tokens: re.Pattern[str] | None
) -> SemanticCaveatConfig | None:
    listed = [object_id for object_id in caveat.object_ids if object_id not in hidden]
    if caveat.object_ids and not listed:
        return None
    named = [caveat.entity_values, caveat.time, caveat.references, caveat.config]
    if _names(named, hidden) or _mentions([caveat.id, caveat.message, caveat.owner, named], tokens):
        return None
    return replace(caveat, object_ids=listed)


def build_view(base: PackageConfig, hidden: frozenset[str]) -> PackageConfig:
    """The package as a caller with ``hidden`` objects sees it."""
    tokens = token_pattern(base, hidden)

    def filtered(name: str) -> list[Any]:
        rows = getattr(base, name)
        shown: Iterable[Any]
        if name in OBJECT_FIELDS:
            return [_without_prose(row, tokens) for row in rows if row.id not in hidden]
        if name == "semantic_policies":
            shown = (display_policy(row, hidden, tokens) for row in rows)
        elif name == "semantic_caveats":
            shown = (_display_caveat(row, hidden, tokens) for row in rows)
        else:  # route rows and rollups: routes come from the base, rollups give the same numbers
            shown = (_without_prose(row, tokens) for row in rows if not _names(row, hidden))
        return [row for row in shown if row is not None]

    def prose(name: str) -> Any:
        value: Any = getattr(base, name)
        if isinstance(value, list):
            return [_without_prose(row, tokens) for row in value]
        if name == "package":
            return replace(value, description="") if _mentions(value.description, tokens) else value
        return type(value)() if _mentions(value, tokens) else value

    changes: dict[str, Any] = {
        name: filtered(name) if kind == "filtered" else prose(name)
        for name, kind in FIELDS.items()
        if kind != "kept"
    }
    return replace(base, **changes)


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
    hidden: frozenset[str]
    entry: ViewEntry
    base: PackageConfig
    # The caller as the request named them (attributes and allowlists are enforcement inputs).
    context: RequestContext
    policy_context: Mapping[str, Any]


# The views pinned in this request, innermost last: one per runtime.
_pinned: contextvars.ContextVar[tuple[RequestView, ...]] = contextvars.ContextVar(
    "visible_view", default=()
)


def visibility_key(context: RequestContext) -> tuple[str, str, frozenset[str]]:
    roles = frozenset(role.strip().lower() for role in context.roles if role.strip())
    return (context.environment, context.audience, roles)


def base_of(config: PackageConfig) -> PackageConfig:
    """The whole package ``config`` was cut from, or ``config`` itself when it is no view."""
    view = get_package_analysis(config).view
    return config if view is None else view.base


def _cut(base: PackageConfig, hidden: frozenset[str]) -> PackageConfig:
    """``build_view``, marked with what it was cut from (routes, the binding guard and public
    effects read the mark). Nothing hidden: the whole package itself."""
    if not hidden:
        return base
    config = build_view(base, hidden)
    get_package_analysis(config).view = ViewOf(base, hidden, token_pattern(base, hidden))
    return config


def _hidden_for(base: PackageConfig, context: RequestContext) -> frozenset[str]:
    return hidden_object_ids(
        base, environment=context.environment, audience=context.audience, roles=context.roles
    )


def _caller(context: RequestContext | Mapping[str, Any] | None) -> RequestContext:
    return context if isinstance(context, RequestContext) else context_from_policy_context(context)


def view_of(
    base: PackageConfig, context: RequestContext | Mapping[str, Any] | None
) -> PackageConfig:
    """A caller's view of ``base``, uncached, for code that holds no runtime (the CLI)."""
    return _cut(base, _hidden_for(base, _caller(context)))


def view_entry(runtime: Any, hidden: frozenset[str]) -> ViewEntry:
    """The runtime's cached view for ``hidden``, built on first use."""
    with runtime._cache_lock:
        entry = runtime._views.get(hidden)
    if entry is None:
        config = _cut(runtime.package_config, hidden)
        with runtime._cache_lock:
            entry = runtime._views.setdefault(hidden, ViewEntry(config, Registry(config), hidden))
    return entry


def view_for(runtime: Any, context: RequestContext | Mapping[str, Any] | None) -> ViewEntry:
    return view_entry(runtime, _hidden_for(runtime.package_config, _caller(context)))


def pinned() -> RequestView | None:
    """The innermost view pinned in the current request, if any."""
    views = _pinned.get()
    return views[-1] if views else None


def pinned_view(runtime: Any) -> RequestView | None:
    """The view pinned for this runtime in the current request, if any."""
    view = next((view for view in _pinned.get() if view.runtime is runtime), None)
    if view is None:
        return None
    if view.generation != runtime._generation:
        raise unresolved()
    return view


def request_view(
    runtime: Any, policy_context: Mapping[str, Any] | None
) -> AbstractContextManager[RequestView]:
    """Resolve the caller's view for one operation; entering the result pins it.

    A nested operation inherits the pinned view; one naming a caller who sees otherwise is
    refused, so a request never widens what it sees. An uncertain view refuses, naming
    nothing, and is never served from the base.
    """
    current = pinned_view(runtime)
    if current is not None:
        if policy_context and (
            visibility_key(context_from_policy_context(policy_context)) != current.key
        ):
            raise unresolved()
        return nullcontext(current)
    context = context_from_policy_context(policy_context)
    try:
        entry = view_for(runtime, context)
    except Exception:  # noqa: BLE001 — uncertainty refuses
        raise unresolved() from None
    return _pinned_scope(
        RequestView(
            runtime=runtime,
            generation=runtime._generation,
            key=visibility_key(context),
            hidden=entry.hidden,
            entry=entry,
            base=runtime.package_config,
            context=context,
            policy_context=dict(policy_context or {}),
        )
    )


@contextmanager
def _pinned_scope(view: RequestView) -> Iterator[RequestView]:
    token = _pinned.set((*_pinned.get(), view))
    try:
        yield view
    finally:
        _pinned.reset(token)
