"""Visibility / access policy enforcement.

Evaluates each declared ``semantic_policy`` against the resolved
request context (``environment``, ``audience``) and returns either a
list of "applied" effects (for HTTP/MCP response payloads) or the set
of hidden object ids that ``catalog`` / ``discover`` / ``inspect``
should filter out. :func:`enforce_query_policies` is the runtime's gate
on ``validate`` / ``compile`` / ``execute``.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields, is_dataclass, replace
from functools import cache
from typing import Any

from .ast import NormalizedQuery, every_filter, normalize_query, plain_filters
from .compiler import BoundQuery, bind_metadata_objects, bind_query
from .compiler_parts.indexes import get_package_analysis
from .errors import SemanticLayerError
from .expressions import (
    AggregateExpr,
    ConditionalAggregateExpr,
    ConversionExpr,
    MetricPredicateExpr,
    ScopedAggregateExpr,
    _opaque_expression_data,
    collect_column_refs,
    parse_semantic_expression,
)
from .policy_rules import (
    MAX_RANK,
    check_request_environment,
    hidden_policy_ids,
    visible_only_listed,
    withheld_max_rank,
)
from .policy_rules import context_scope_matches as context_scope_matches
from .policy_rules import policy_action as _policy_action
from .policy_rules import policy_config as _policy_config
from .policy_rules import policy_matches as _policy_matches
from .policy_rules import role_scope_matches as role_scope_matches
from .request_context import context_from_policy_context
from .row_filters import RowFilter, is_row_filter, row_filter
from .schema import (
    PackageConfig,
    RelationshipConfig,
    SegmentConfig,
    SemanticPolicyConfig,
    ValueDomainConfig,
)
from .segments import build_segment_query, normalize_segment
from .sql_ast import SqlCase, SqlCaseWhen, SqlIdentifier, SqlIsNull, SqlLiteral, SqlOrder
from .sql_preparation import checked_slot_value

WITHHOLD = "withhold_values"
# A conditional aggregate's condition reads columns, not fields: no allowed_where lists it.
INLINE_CONDITION = "aggregate_if.condition"
WITHHELD_SHAPE = (
    "Select the withheld metric directly, put it first in order_by, then every group key in "
    "the same direction (added for you when order_by names only the metric), with a limit of "
    "at most max_rank. Do not select, filter, threshold, compare or export it anywhere else."
)
# What the outermost operation's caller named; the operations it runs keep it.
_caller_names: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "caller_names", default=None
)


def _renamed(value: Any, rename: Callable[[str], str]) -> Any:
    """``value`` with every string that could name an object passed through ``rename``:
    never literal data or the caller's policy context."""
    if isinstance(value, str):
        return rename(value)
    if isinstance(value, Mapping):
        return {
            _renamed(key, rename): child
            if key == "policy_context" or _opaque_expression_data(value, key)
            else _renamed(child, rename)
            for key, child in value.items()
        }
    if isinstance(value, list | tuple):
        return [_renamed(child, rename) for child in value]
    return value


def _names(value: Any) -> set[str]:
    found: set[str] = set()

    def record(name: str) -> str:
        found.add(name)
        return name

    _renamed(value, record)
    return found


@contextmanager
def caller_request(request: Any) -> Iterator[None]:
    """Record what an operation's caller named, unless an outer operation already did."""
    token = _caller_names.set(frozenset(_names(request))) if _caller_names.get() is None else None
    try:
        yield
    finally:
        if token is not None:
            _caller_names.reset(token)


def refuse_as_unknown(
    request: Any, hidden: Collection[str] | None, resolve: Callable[[Any], object]
) -> None:
    """Refuse an object hidden from the caller exactly as if it did not exist.

    Counts each id the caller named that ``hidden`` holds (``None``, uncertain visibility:
    every named id). ``resolve`` runs again with those ids swapped for ids no package declares,
    and its own unknown-id refusal is raised with the caller's ids restored; a request that
    resolves anyway goes on as sent. An object read only through a visible one is not named.
    """
    named = _names(request)
    caller = _caller_names.get()
    named = named if caller is None else named & caller
    named = named if hidden is None else named & set(hidden)
    if not named:
        return
    swap = {name: f"{name}{uuid.uuid4().hex}" for name in named}
    try:
        resolve(_renamed(request, lambda name: swap.get(name, name)))
    except SemanticLayerError as exc:
        raise _restored(exc, swap) from None


def _restored(exc: SemanticLayerError, swap: Mapping[str, str]) -> SemanticLayerError:
    def restore(value: Any) -> Any:
        if isinstance(value, str):
            for name, swapped in swap.items():
                value = value.replace(swapped, name)
            return value
        if isinstance(value, Mapping):
            return {restore(key): restore(child) for key, child in value.items()}
        if isinstance(value, list | tuple):
            return type(value)(restore(child) for child in value)
        return value

    restored = type(exc).__new__(type(exc))  # the same refusal, subclass fields included
    restored.args = restore(exc.args)
    vars(restored).update(restore(vars(exc)))
    return restored


def _without(value: Any, hidden: Collection[str]) -> Any:
    """``value`` with no string that names a hidden object."""

    def shown(child: Any) -> bool:
        return not (isinstance(child, str) and child in hidden)

    if isinstance(value, Mapping):
        return {key: _without(child, hidden) for key, child in value.items() if shown(child)}
    if isinstance(value, list):
        return [_without(child, hidden) for child in value if shown(child)]
    return value


def policy_effects_for_object(
    config: PackageConfig,
    object_id: str,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    check_request_environment(config, environment)
    hidden = hidden_object_ids(config, environment=environment, audience=audience, roles=roles)
    effects: list[dict[str, Any]] = []
    for policy in config.semantic_policies:
        if not _policy_matches(
            policy,
            object_id=object_id,
            environment=environment,
            audience=audience,
            roles=roles,
        ):
            continue
        action = _policy_action(policy)
        if not action:
            continue
        effects.append(_without(_base_policy_effect(policy, action=action), hidden))
    return effects


def hidden_object_ids(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> set[str]:
    """The one visibility set catalog, discovery, inspect, valid values, planning and
    diagnostics read: ``hidden`` policies and :func:`restricted_object_ids`."""
    scope: dict[str, Any] = {"environment": environment, "audience": audience, "roles": roles}
    return hidden_policy_ids(config, **scope) | restricted_object_ids(config, **scope)


def restricted_object_ids(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> frozenset[str]:
    """What ``visible_only`` policies keep from this context: each listed object it is not
    eligible for, and every object whose reads reach one or cannot be bound."""
    listed = visible_only_listed(config, environment=environment, audience=audience, roles=roles)
    if not listed:
        return frozenset()
    return frozenset(listed).union(
        object_id
        for object_id, reads in _object_reads(config).items()
        if reads is None or reads & listed
    )


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
    rows: list[Any] = [
        *config.entities,
        *config.dimensions,
        *config.temporal_roles,
        *config.relationships,
        *config.value_domains,
        *config.measures,
        *config.metric_recipes,
        *config.segments,
    ]
    for row in rows:
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


def diagnostic_hidden_object_ids(
    config: PackageConfig, policy_context: Mapping[str, Any] | None
) -> frozenset[str] | None:
    """Use discovery's visibility check; uncertainty cannot authorize disclosure."""
    if policy_context is None:
        return None
    try:
        context = context_from_policy_context(policy_context)
        return frozenset(
            hidden_object_ids(
                config,
                environment=context.environment,
                audience=context.audience,
                roles=context.roles,
            )
        )
    except Exception:  # noqa: BLE001 — diagnostics must fail closed on uncertain visibility
        return None


def query_policy_effects(
    config: PackageConfig,
    object_ids: Iterable[str],
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
    query: Mapping[str, Any] | None = None,
    binding: BoundQuery | None = None,
) -> list[dict[str, Any]]:
    check_request_environment(config, environment)
    effects: list[dict[str, Any]] = []
    # Bound at most once per request, and only for a constraint that needs it.
    bound = cache(
        lambda: binding if binding is not None else bind_query(config, None, dict(query or {}))
    )
    metric_filter_refs = cache(lambda object_id: _metric_filter_refs(config, bound(), object_id))
    for object_id in list(dict.fromkeys(str(item) for item in object_ids if str(item).strip())):
        for policy in config.semantic_policies:
            if not _policy_matches(
                policy,
                object_id=object_id,
                environment=environment,
                audience=audience,
                roles=roles,
            ):
                continue
            action = _policy_action(policy)
            if not action:
                continue
            if policy.kind == "metric_constraint" and query is not None:
                effects.append(
                    _metric_constraint_effect(policy, query, metric_filter_refs, bound, object_id)
                )
            else:
                effects.append(_base_policy_effect(policy, action=action))
    # Dedupe by (policy_id, kind, action). Package-wide policies — e.g.
    # release-label — attach to every referenced object and would
    # otherwise return N copies of the same effect for an N-object
    # query. Merge object_ids across duplicates so callers still see
    # which objects the policy bound. Caught by the blind-agent
    # benchmark, which saw `policy.jaffle.release_label` repeated 6x.
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for effect in effects:
        key = (
            str(effect.get("policy_id", "")),
            str(effect.get("kind", "")),
            str(effect.get("action", "")),
        )
        if key not in deduped:
            deduped[key] = dict(effect)
            deduped[key]["object_ids"] = list(effect.get("object_ids", []) or [])
            continue
        existing_ids = list(deduped[key].get("object_ids", []) or [])
        for object_id in list(effect.get("object_ids", []) or []):
            if object_id not in existing_ids:
                existing_ids.append(object_id)
        deduped[key]["object_ids"] = existing_ids
        existing_violations = list(deduped[key].get("violations", []) or [])
        for violation in list(effect.get("violations", []) or []):
            if violation not in existing_violations:
                existing_violations.append(violation)
        if existing_violations:
            deduped[key]["violations"] = existing_violations
    hidden = hidden_object_ids(config, environment=environment, audience=audience, roles=roles)
    return [_without(effect, hidden) for effect in deduped.values()]


def enforce_query_policies(
    config: PackageConfig,
    object_ids: Iterable[str],
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
    query: Mapping[str, Any] | None = None,
    binding: BoundQuery | None = None,
) -> list[dict[str, Any]]:
    object_ids = list(object_ids)  # read twice: the effects, then the withheld objects
    # visible_only is checked against everything the query reads, never policy by policy.
    restricted = restricted_object_ids(
        config, environment=environment, audience=audience, roles=roles
    )
    hidden = restricted | hidden_policy_ids(
        config, environment=environment, audience=audience, roles=roles
    )
    if query is not None:
        refuse_as_unknown(query, hidden, lambda masked: bind_query(config, None, dict(masked)))
    if restricted and binding is None and query is not None:
        binding = bind_query(config, None, dict(query))
    blocked = restricted & {
        *object_ids,
        *(bound_object_ids(binding) if binding is not None else ()),
    }
    # Caller-created measures have no authored object id to govern their raw columns.
    raw_aggregate = bool(
        restricted
        and binding is not None
        and any(collect_column_refs(row.expr) for row in binding.plan.synthetic_measures.values())
    )
    if blocked or raw_aggregate:
        raise SemanticLayerError(
            "POLICY_DENIED",
            "Query references a semantic object blocked by policy.",
            # Every restricted object is hidden from this caller, so none is named.
            details={"blocked_objects": [], "policy_effects": [], "policy_violations": []},
        )
    effects = query_policy_effects(
        config,
        object_ids,
        environment=environment,
        audience=audience,
        roles=roles,
        query=query,
        binding=binding,
    )
    blocking = [row for row in effects if row["action"] in {"deny", "redact", "hidden"}]
    if blocking:
        # A hidden object read through a visible one: no policy hiding it is named.
        shown = [row for row in blocking if row["action"] != "hidden"]
        raise SemanticLayerError(
            "POLICY_DENIED",
            "Query references a semantic object blocked by policy.",
            details={
                "blocked_objects": sorted(
                    {object_id for row in shown for object_id in row.get("object_ids", [])}
                ),
                "policy_effects": shown,
                "policy_violations": [
                    violation
                    for row in shown
                    for violation in list(row.get("violations", []) or [])
                ],
            },
        )
    withheld = withheld_object_ids(
        config,
        [*object_ids, *(binding.object_ids if binding is not None else ())],
        environment=environment,
        audience=audience,
        roles=roles,
    )
    if withheld:
        if binding is None and query is not None:
            binding = bind_query(config, None, dict(query))
        refusal = withheld_shape(config, binding, withheld)
        if refusal is not None:
            raise refusal
        assert binding is not None  # withheld_shape refuses an unbound query
        column = binding.plan.query["order_by"][0]["field"]
        for row in effects:
            if row["action"] == WITHHOLD:
                row["withheld_objects"] = sorted(withheld)
                row["withheld_column"] = column
    return effects


def withheld_object_ids(
    config: PackageConfig,
    object_ids: Iterable[str],
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> dict[str, int]:
    """Each object whose values a matching ``withhold_values`` policy keeps from this caller,
    with the smallest ``max_rank`` among those policies."""
    check_request_environment(config, environment)
    ranks: dict[str, int] = {}
    for policy in config.semantic_policies:
        if _policy_action(policy) != WITHHOLD:
            continue
        for object_id in dict.fromkeys(str(item) for item in object_ids):
            if _policy_matches(
                policy, object_id=object_id, environment=environment, audience=audience, roles=roles
            ):
                ranks[object_id] = min(ranks.get(object_id, MAX_RANK), withheld_max_rank(policy))
    return ranks


def withheld_measure_ids(
    config: PackageConfig,
    *,
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> set[str]:
    """Measures whose values are withheld from this caller, directly or as a withheld
    metric's input."""
    measures = {row.id for row in config.measures}
    withheld = withheld_object_ids(
        config,
        [*measures, *(row.id for row in config.metric_recipes)],
        environment=environment,
        audience=audience,
        roles=roles,
    )
    recipes = set(withheld) - measures
    reads = set(withheld) | (bind_metadata_objects(config, recipes) if recipes else frozenset())
    return reads & measures


def withheld_shape(
    config: PackageConfig, binding: BoundQuery | None, withheld: Mapping[str, int]
) -> SemanticLayerError | None:
    """The one guard on withheld values: ``None`` when the bound query only ranks by one.

    Accepted: a grouped query whose first ``order_by`` field is a select item naming one
    withheld metric or measure directly, ordered then by every group key in the same direction,
    with a ``limit`` of at most ``max_rank``, no ``export``, and no other part of the query
    reading a withheld object. Dependencies come from the compiler: the cuts the binding
    records, then a binding of the query without that item, so a filter, threshold, segment,
    derived metric or comparison reading it is caught. Anything else is ``POLICY_DENIED``.
    """

    def refuse(reason: str, message: str) -> SemanticLayerError:
        return SemanticLayerError(
            "POLICY_DENIED",
            f"Values of {', '.join(sorted(withheld))} are withheld by policy: {message}",
            details={
                "reason": reason,
                "withheld_objects": sorted(withheld),
                "max_rank": max_rank,
                "accepted_shape": WITHHELD_SHAPE,
            },
        )

    max_rank = min(withheld.values())
    if binding is None:
        return refuse("withheld_unbound", "only a query can rank by them.")
    query = binding.plan.query
    if query.get("export"):
        return refuse("withheld_export", "a query that reads them cannot be exported.")
    order = list(query.get("order_by") or [])
    ranked = next(
        (item for item in query["select"] if order and item["as"] == order[0]["field"]), None
    )
    if ranked is None or _direct_reference(ranked["expression"]) not in withheld:
        return refuse(
            "withheld_not_ranked", "the first order_by field must select one of them directly."
        )
    keys = rank_keys(query)
    if not keys:
        return refuse("withheld_ungrouped", "a rank needs group_by keys.")
    direction = order[0]["direction"]
    if [(_order_field(query, item["field"]), item["direction"]) for item in order[1:]] != [
        (key, direction) for key in keys
    ]:
        return refuse(
            "withheld_tie_order",
            f"ties are ordered by every group key, {direction}, and nothing else.",
        )
    limit = query.get("limit")
    if limit is None or limit > max_rank:
        return refuse("withheld_rank_limit", f"limit must be at most {max_rank}.")
    if binding.unresolved_cuts:
        return refuse("withheld_unproven", "a filter or threshold could not be fully bound.")
    filtered = set().union(*binding.cuts) & set(withheld)
    if filtered:
        return refuse(
            "withheld_value_dependency",
            f"{', '.join(sorted(filtered))} is read by a filter or threshold.",
        )
    # Without the ranked item; a query selecting nothing ignores metric filters, which the
    # cuts above have covered.
    witness = {
        **query,
        "select": [item for item in query["select"] if item is not ranked],
        "order_by": [],
        "limit": None,
    }
    try:
        read = bind_query(config, None, witness).object_ids
    except SemanticLayerError:
        return refuse("withheld_unproven", "the rest of the query could not be bound without it.")
    if read & set(withheld):
        return refuse(
            "withheld_value_dependency",
            f"{', '.join(sorted(read & set(withheld)))} is read outside the ranked order_by field "
            "(a selected expression, filter, threshold, segment or comparison).",
        )
    return None


def rank_keys(query: Mapping[str, Any]) -> list[str]:
    """A normalized query's output group keys: its group_by, then a time bucket."""
    time = query.get("time") or {}
    bucket = [f"{time['temporal_role']}__{time['grain']}"] if time.get("grain") else []
    return [*query.get("group_by", []), *bucket]


def withheld_rank_order(
    config: PackageConfig,
    binding: BoundQuery,
    *,
    rebind: Callable[[dict[str, Any]], BoundQuery],
    environment: str = "",
    audience: str = "",
    roles: Iterable[str] | None = None,
) -> BoundQuery:
    """Bind missing tie keys and give each withheld order key a portable NULL indicator.

    Only the accepted rank gets this SQL order. The indicators follow the rank direction,
    so NULLs sort first on ASC and last on DESC, including in nullable tie keys.
    """
    query = binding.plan.query
    order = list(query.get("order_by") or [])
    ranked = next(
        (item for item in query["select"] if order and item["as"] == order[0]["field"]), None
    )
    if ranked is None:
        return binding
    target = _direct_reference(ranked["expression"])
    withheld = (
        withheld_object_ids(
            config, [target], environment=environment, audience=audience, roles=roles
        )
        if target
        else {}
    )
    if not withheld:
        return binding
    if len(order) == 1:
        binding = rebind(
            {
                **query,
                "order_by": [
                    order[0],
                    *(
                        {"field": key, "direction": order[0]["direction"]}
                        for key in rank_keys(query)
                    ),
                ],
            }
        )
    # The shared policy gate will report the refusal; never transform an unproven shape.
    if withheld_shape(config, binding, withheld) is not None:
        return binding
    fields = {field.alias: field.expression for field in binding.sql_ast.select}
    sql_order = []
    for term in binding.sql_ast.order_by:
        expression = term.expression
        # Postgres accepts a select alias as an order key, but not inside CASE. Use its
        # projected expression for the indicator, keeping qualified source keys as-is.
        if isinstance(expression, SqlIdentifier) and len(expression.parts) == 1:
            expression = fields.get(expression.parts[0], expression)
        sql_order.extend(
            [
                SqlOrder(
                    SqlCase([SqlCaseWhen(SqlIsNull(expression), SqlLiteral(0))], SqlLiteral(1)),
                    term.direction,
                ),
                term,
            ]
        )
    return replace(binding, sql_ast=replace(binding.sql_ast, order_by=sql_order))


def _direct_reference(expression: Mapping[str, Any]) -> str:
    """The metric or measure a select expression names with no wrapper or override."""
    if set(expression) == {"kind", "metric"} and expression["kind"] == "metric":
        return str(expression["metric"])
    if (
        set(expression) == {"kind", "measure", "aggregation", "temporal_role"}
        and expression["kind"] == "measure"
        and not expression["aggregation"]
        and not expression["temporal_role"]
    ):
        return str(expression["measure"])
    return ""


def _order_field(query: Mapping[str, Any], field: str) -> str:
    """``time`` names the query's time bucket."""
    bucket = rank_keys({**query, "group_by": []})
    return bucket[0] if field == "time" and bucket else field


def row_filters_for_context(
    config: PackageConfig, policy_context: Mapping[str, Any]
) -> tuple[RowFilter, ...]:
    """The row filters that apply to this request; each needs its attribute, correctly typed.

    Checked before binding, so every surface (validate, compile, plan, execute,
    segment preview) denies a missing attribute instead of compiling unfiltered.
    """
    context = context_from_policy_context(policy_context)
    check_request_environment(config, context.environment)
    filters = []
    for policy in config.semantic_policies:
        row_policy = is_row_filter(policy)
        _policy_action(policy)  # guard direct configs before binding or cache lookup
        if not row_policy:
            continue
        row = row_filter(config, policy)  # checked first: an unenforceable one is never skipped
        if _policy_matches(
            policy,
            object_id="",
            environment=context.environment,
            audience=context.audience,
            roles=context.roles,
        ):
            checked_slot_value(row.slot, context.attributes.get(row.slot.attribute))
            filters.append(row)
    return tuple(filters)


def package_release_labels(config: PackageConfig) -> list[str]:
    labels = []
    for policy in config.semantic_policies:
        if policy.kind == "package_release":
            policy_config = _policy_config(policy)
            label = str(policy_config.get("label", "") or policy.action or "").strip()
            if label:
                labels.append(label)
    return list(dict.fromkeys(labels))


def _policy_rationale(policy: SemanticPolicyConfig) -> str:
    policy_config = _policy_config(policy)
    return str(
        policy.rationale
        or policy_config.get("rule", "")
        or policy_config.get("rationale", "")
        or policy_config.get("description", "")
    )


def _base_policy_effect(policy: SemanticPolicyConfig, *, action: str) -> dict[str, Any]:
    effect: dict[str, Any] = {
        "policy_id": policy.id,
        "kind": policy.kind,
        "action": action,
        "rationale": _policy_rationale(policy),
        "object_ids": list(policy.object_ids),
        "audiences": list(policy.audiences),
        "environments": list(policy.environments),
        "roles": list(policy.roles),
    }
    if policy.kind == "metric_constraint":
        effect["constraints"] = _metric_constraint_summary(policy)
    return effect


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _config_str_list(policy_config: Mapping[str, Any], key: str) -> list[str]:
    return [text for item in _as_list(policy_config.get(key)) if (text := str(item).strip())]


def _metric_constraint_summary(policy: SemanticPolicyConfig) -> dict[str, Any]:
    policy_config = _policy_config(policy)
    summary_keys = (
        "required_group_by",
        "allowed_group_by",
        "required_where",
        "allowed_where",
        "allow_metric_filters",
        "allowed_metric_filter_entities",
        "allowed_metric_filter_metrics",
        "allowed_temporal_roles",
    )
    return {key: policy_config[key] for key in summary_keys if key in policy_config}


def _metric_constraint_effect(
    policy: SemanticPolicyConfig,
    query_payload: Mapping[str, Any],
    metric_filter_refs: Callable[[str], dict[str, list[str]]],
    bound: Callable[[], BoundQuery],
    object_id: str,
) -> dict[str, Any]:
    violations = _metric_constraint_violations(
        policy, query_payload, metric_filter_refs, bound, object_id
    )
    effect = _base_policy_effect(policy, action="deny" if violations else "constrain")
    if violations:
        effect["violations"] = violations
    return effect


def _metric_constraint_violations(
    policy: SemanticPolicyConfig,
    query_payload: Mapping[str, Any],
    metric_filter_refs: Callable[[str], dict[str, list[str]]],
    bound: Callable[[], BoundQuery],
    object_id: str,
) -> list[dict[str, Any]]:
    policy_config = _policy_config(policy)
    query = normalize_query(dict(query_payload or {}))
    violations: list[dict[str, Any]] = []

    if "required_group_by" in policy_config:
        required = _config_str_list(policy_config, "required_group_by")
        missing = [field for field in required if field not in set(query.group_by)]
        if missing:
            violations.append(
                {
                    "kind": "missing_required_group_by",
                    "missing": missing,
                    "present_group_by": list(query.group_by),
                }
            )

    if "allowed_group_by" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_group_by"))
        disallowed = [field for field in query.group_by if field not in allowed]
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_group_by",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                }
            )

    # A required filter must cut the query's own rows; a child group's condition cuts child
    # rows, so it never meets one. Every condition, a group's included, must be allowed.
    # An expression filter never meets required_where; it must be allowed when it counts
    # for the governed object, including whole-query attribution for nested filters.
    inline = cache(
        lambda: _governed_inline_fields(
            _inline_filters(query), bound, object_id, package_wide=not policy.object_ids
        )
    )
    where_rows = [
        {"field": item.field, "op": item.op, "value": item.value}
        for item in plain_filters(query.where)
    ]
    condition_fields = [item.field for item in every_filter(query.where)]
    if "required_where" in policy_config:
        required_filters = _normalize_where_specs(policy_config.get("required_where"))
        missing_filters = [
            spec
            for spec in required_filters
            if not any(_where_spec_matches(row, spec) for row in where_rows)
        ]
        if missing_filters:
            violations.append(
                {
                    "kind": "missing_required_where",
                    "missing": missing_filters,
                    "present_where": where_rows,
                }
            )

    if "allowed_where" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_where"))
        disallowed = [field for field in condition_fields if field not in allowed]
        if disallowed:
            violations.append(
                {"kind": "disallowed_where", "disallowed": disallowed, "allowed": sorted(allowed)}
            )
        disallowed = [field for field in inline() if field not in allowed]
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_where",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                    "source": "inline_expression",
                }
            )

    if "allowed_temporal_roles" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_temporal_roles"))
        effective = set(bound().temporal_roles.get(object_id, ()))
        if query.time is not None and query.time.temporal_role:
            effective.add(query.time.temporal_role)
        for role in sorted(effective - allowed):
            violations.append(
                {
                    "kind": "disallowed_temporal_role",
                    "temporal_role": role,
                    "allowed": sorted(allowed),
                }
            )

    filters_denied = policy_config.get("allow_metric_filters") is False
    allowlists = [
        key
        for key in ("allowed_metric_filter_entities", "allowed_metric_filter_metrics")
        if key in policy_config
    ]
    refs = metric_filter_refs(object_id) if filters_denied or allowlists else {}
    if filters_denied and bound().object_cuts(object_id):
        violations.append({"kind": "metric_filters_not_allowed", "metric_filter_refs": refs})
    elif filters_denied and inline():
        # Every inline filter is a cut, whether or not the compiler recorded one.
        violations.append(
            {
                "kind": "metric_filters_not_allowed",
                "metric_filter_refs": refs,
                "source": "inline_expression",
            }
        )
    if allowlists and refs.get("unresolved"):
        violations.append({"kind": "unresolved_metric_filter", "unresolved": refs["unresolved"]})
    if "allowed_metric_filter_entities" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_metric_filter_entities"))
        disallowed = sorted(set(refs.get("entities", [])) - allowed)
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_metric_filter_entity",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                }
            )
    if "allowed_metric_filter_metrics" in policy_config:
        allowed = set(_config_str_list(policy_config, "allowed_metric_filter_metrics"))
        disallowed = sorted(set(refs.get("metrics", [])) - allowed)
        if disallowed:
            violations.append(
                {
                    "kind": "disallowed_metric_filter_metric",
                    "disallowed": disallowed,
                    "allowed": sorted(allowed),
                }
            )

    return violations


def _normalize_where_specs(value: Any) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            spec = dict(item)
            field = str(spec.get("field", spec.get("dimension", "")) or "").strip()
            if field:
                spec["field"] = field
                specs.append(spec)
            continue
        field = str(item or "").strip()
        if field:
            specs.append({"field": field})
    return specs


def _where_spec_matches(row: Mapping[str, Any], spec: Mapping[str, Any]) -> bool:
    if str(row.get("field", "")) != str(spec.get("field", "")):
        return False
    if "op" in spec and str(row.get("op", "")).upper() != str(spec.get("op", "")).upper():
        return False
    return "value" not in spec or row.get("value") == spec.get("value")


def _inline_filters(query: NormalizedQuery) -> list[tuple[str | None, str]]:
    """``(measure, field)`` for each filter the caller wrote inside a select or metric-filter
    expression: an aggregate's ``filter`` and a scoped aggregate's ``where``, at any depth,
    predicate inputs included. A conditional aggregate's condition has no declared measure and
    is ``("", INLINE_CONDITION)``. ``measure=None`` marks whole-query attribution under
    predicates, metric filters and conversion operands. Recipes are not read here.
    """
    found: list[tuple[str | None, str]] = []

    def visit(expr: Any, *, whole_query: bool = False) -> None:
        whole_query = whole_query or isinstance(expr, (MetricPredicateExpr, ConversionExpr))
        nested: list[Any] = []
        if isinstance(expr, AggregateExpr):
            for clause in expr.filter.get("all", []):
                if "expression" in clause:
                    nested.append(dict(clause["expression"]))
                else:
                    found.append((None if whole_query else expr.measure, str(clause["field"])))
        elif isinstance(expr, ScopedAggregateExpr):
            found.extend(
                (None if whole_query else expr.measure, str(item.get("field", "")))
                for item in expr.where
            )
            nested.extend(
                dict(item["input"])
                for item in expr.predicates
                if isinstance(item.get("input"), dict)
            )
        elif isinstance(expr, ConditionalAggregateExpr):
            found.append((None if whole_query else "", INLINE_CONDITION))
        for payload in nested:
            visit(parse_semantic_expression(payload, context="query"), whole_query=True)
        for item in fields(expr) if is_dataclass(expr) else ():
            value = getattr(expr, item.name)
            for child in value if isinstance(value, list) else [value]:
                if is_dataclass(child):
                    visit(child, whole_query=whole_query)

    for row in query.select:
        visit(row.expression)
    for metric_filter in query.metric_filters:
        visit(metric_filter.expression, whole_query=True)
    return found


def _governed_inline_fields(
    filters: list[tuple[str | None, str]],
    bound: Callable[[], BoundQuery],
    object_id: str,
    *,
    package_wide: bool,
) -> list[str]:
    """The fields of the inline filters that cut ``object_id``.

    A filter is owned by the query's root leaves over its measure, and counts by the rule
    that attributes the compiler's own cuts (:meth:`BoundQuery.cut_counts`). A filter on the
    governed measure itself, under a package-wide constraint, or marked whole-query
    (``measure=None``) always counts.
    """

    def owners(measure: str) -> frozenset[str]:
        return frozenset(
            row.alias for row in bound().plan.bound_measures if row.measure_id == measure
        )

    out = [
        field
        for measure, field in filters
        if package_wide
        or measure is None
        or measure == object_id
        or bound().cut_counts(object_id, owners(measure))
    ]
    return list(dict.fromkeys(out))


def _metric_filter_refs(
    config: PackageConfig, binding: BoundQuery, object_id: str
) -> dict[str, list[str]]:
    """Dependencies of the cuts that apply to one governed object in the actual compilation."""
    kinds = {
        "entities": {row.id for row in config.entities},
        "metrics": {row.id for row in config.metric_recipes},
        "measures": {row.id for row in config.measures},
    }
    ids = set().union(*binding.object_cuts(object_id))
    refs = {key: sorted(ids & members) for key, members in kinds.items() if ids & members}
    if binding.unresolved_cuts:
        refs["unresolved"] = list(binding.unresolved_cuts)
    return refs
