"""Binding: a query's plan and SQL AST with every object, cut, route and row filter it reads,
for the policy check and the answer's disclosures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from typing import Any

from ..acceleration.routing import (
    aggregate_routing,
    recording_rollup_scans,
)
from ..ast import (
    child_groups,
    every_filter,
    normalize_query,
    route_decisions_from_payload,
)
from ..clock import parse_now
from ..config_parts.route_rows import (
    RouteRowError,
    UnknownRelationshipError,
    check_route_row,
    entity_references,
    walk_entities,
)
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ConversionExpr,
    MetricRecipeRefExpr,
    ScopedAggregateExpr,
    validate_expression_shapes,
)
from ..fanout import (
    RouteChoice,
    package_hop_limit,
    pair_routes,
    query_route_decisions,
    recording_route_choices,
    route_decision_basis,
)
from ..ir import (
    LogicalPlan,
)
from ..query_call import engine_where, refusal
from ..registry import Registry
from ..relation_pipelines import attach_relation_ctes
from ..row_filter_visibility import Visibility, hold_unowned_reads, visibility_for
from ..row_filters import RowFilter, apply_row_filters
from ..schema import (
    PackageConfig,
    PathPreferenceConfig,
    base_of,
    require_base,
)
from ..sql_ast import (
    SqlCte,
    SqlParameter,
    SqlSelect,
    sql_nodes,
)
from .bind import (
    _expression_alias,
    refusing_fallible_calls,
)
from .dependencies import (
    binding_dependencies,
    capture_objects,
    plan_bindings,
    record_temporal_role,
)
from .empty_groups import (
    capping_at,
    observing,
    recording_balance_edges,
    recording_zero_outputs,
)
from .indexes import (
    _dimension_index,
    _entity_index,
    _measure_index,
    _recipe_index,
    _relationship_index,
    _temporal_role_index,
    get_package_analysis,
)
from .lowering_common import recording_stock_key_gaps
from .paths import (
    _leaf_time_role,
    display_key,
)
from .plan_checks import _object_default_query_temporal_role
from .planning import plan_query, resolve_compile_config
from .snapshot_reads import closing_reads_by_output, recording_closing_reads
from .sql_lowering import lower_to_sql
from .temporal import (
    _requires_query_time,
    _source_filter_refusals,
    _validate_restrictive_time_semantics,
)


def _record_bound_plan(
    plan: LogicalPlan, config: PackageConfig, leaves: dict[str, set[str]]
) -> None:
    _entity_index(config).get(plan.root_entity)
    dimensions = _dimension_index(config)
    for dimension in plan.group_by:
        dimensions.get(dimension)
    for clause in every_filter(plan.query.get("where")):
        dimensions.get(str(clause.get("field", "")))
    for group in child_groups(plan.query.get("where")):
        _entity_index(config).get(str(group.get("child", "")))
    _temporal_role_index(config).get(str(plan.time.get("temporal_role", "")))
    for bound in plan.bound_measures:
        with capture_objects(leaves[bound.alias]):
            _measure_index(config).get(bound.measure_id)
            effective_role = _leaf_time_role(bound, normalize_query(plan.query), config)
            roles = {effective_role} if effective_role else set()
            if (
                bound.aggregation in {"first_value", "last_value"}
                or _measure_index(config)[bound.measure_id].measure_class == "semi_additive"
            ):
                roles.add(bound.temporal_role)
            for role in roles:
                _temporal_role_index(config).get(role)
                record_temporal_role(bound.measure_id, role, leaf_alias=bound.alias)
        # Preserve the complete object-access binding independently of cuts.
        _temporal_role_index(config).get(bound.temporal_role)
    for measure_plan in plan.measure_plans:
        for entity in measure_plan.required_entities:
            _entity_index(config).get(entity)
        for selection in measure_plan.path_selections:
            for relationship in selection.chosen_path:
                _relationship_index(config).get(relationship)


@dataclass(frozen=True)
class BoundQuery:
    plan: LogicalPlan
    config: PackageConfig
    sql_ast: SqlSelect
    object_ids: frozenset[str]
    cuts: tuple[frozenset[str], ...]
    temporal_roles: dict[str, frozenset[str]]
    unresolved_cuts: tuple[str, ...]
    # Per cut: the root leaves it filters, or None when it filters the whole query.
    cut_owners: tuple[frozenset[str] | None, ...]
    # Root leaf alias -> the measures and recipes whose values it computes.
    leaf_objects: dict[str, frozenset[str]]
    # Every rollup the SQL reads, including separately compiled branches.
    rollup_scans: frozenset[str] = frozenset()
    # What the request's row filters let it read; every statement it runs is held to it.
    visibility: Visibility | None = None
    # The key gap of every event/state-clock stock the SQL reads, nested compiles included.
    stock_key_gaps: tuple[dict[str, Any], ...] = ()
    # Every output that reads 0 or NULL for an empty group, with the measures behind it.
    zero_outputs: tuple[dict[str, Any], ...] = ()
    # Root leaf output aliases whose observed buckets lowering retained.
    retained_filtered_series: tuple[str, ...] = ()
    # Every route the SQL reads, nested compiles included (the plan's own root and leaf paths
    # are in the plan).
    route_choices: tuple[RouteChoice, ...] = ()
    # Each query route_decisions row the SQL reads, with the basis it replaced.
    route_decisions: tuple[dict[str, Any], ...] = ()
    # The request's ``now`` the SQL's loaded coverage caps at ("" when it reads none).
    coverage_now: str = ""
    # Each output that reads only balances, with the coverage CTEs (kept apart from the
    # query's SQL, row-filtered like it) that say where their data ends.
    balance_edges: tuple[dict[str, Any], ...] = ()
    balance_coverage: tuple[SqlCte, ...] = ()
    # Each balance read on its closing day (nested reads included), the outputs it counts for,
    # and the probe (row-filtered like the query) that names the day and counts the series
    # with no snapshot on it.
    closing_reads: tuple[dict[str, Any], ...] = ()
    # Root leaves only package metrics' own definitions compute (:func:`_authored_leaves`).
    authored_leaves: frozenset[str] = frozenset()

    def object_cuts(self, object_id: str) -> tuple[frozenset[str], ...]:
        """The cuts the caller added that count for ``object_id``: whole-query cuts plus the
        cuts of leaves computing it. A package metric's own filter is its author's definition,
        so a cut of authored leaves alone is not one."""
        return tuple(
            cut
            for cut, owners in zip(self.cuts, self.cut_owners, strict=True)
            if not (owners and owners <= self.authored_leaves)
            and self.cut_counts(object_id, owners)
        )

    def cut_counts(self, object_id: str, owners: frozenset[str] | None) -> bool:
        """Whether a cut of the root leaves ``owners`` (None: the whole query) counts for
        ``object_id``.

        An object read inside a cut, or computed by no root leaf (an entity,
        dimension or nested-only read), has no single owner and sees every cut.
        """
        leaves = {alias for alias, ids in self.leaf_objects.items() if object_id in ids}
        if not leaves or any(object_id in cut for cut in self.cuts):
            return True
        return owners is None or bool(owners & leaves)


def bind_metadata_objects(config: PackageConfig, object_ids: Iterable[str]) -> frozenset[str]:
    """Resolve metadata ownership and a valid default invocation for recipes."""
    references: set[str] = set()
    for object_id in object_ids:
        recipe = _recipe_index(config).get(object_id)
        if recipe is None:
            with binding_dependencies() as dependencies:
                for index in (
                    _dimension_index,
                    _temporal_role_index,
                    _entity_index,
                    _measure_index,
                ):
                    index(config).get(object_id)
            references.update(dependencies.object_ids)
            continue
        query: dict[str, Any] = {"select": [{"expression": {"metric": object_id}, "as": "value"}]}
        if not _requires_query_time(recipe.expression, config):
            references.update(bind_query(config, None, query).object_ids)
            continue
        role_id = _object_default_query_temporal_role(config, object_id)
        role = _temporal_role_index(config).get(role_id)
        grains = list(role.supported_grains) if role is not None else []
        # Window contracts need different grains. Metadata has no caller time
        # axis, so use a supported invocation rather than treating missing
        # context as a policy refusal. Execution binds the actual query again.
        last_error = SemanticLayerError(
            "INVALID_QUERY", "No default temporal invocation is available."
        )
        for grain in grains:
            query["time"] = {"temporal_role": role_id, "grain": grain}
            try:
                references.update(bind_query(config, None, query).object_ids)
                break
            except SemanticLayerError as exc:
                last_error = exc
        else:
            raise last_error
    return frozenset(references)


def _keyed_by_entity(config: PackageConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """A dimension that names an entity's rows (its ``display``) groups by the entity's key
    too, just before the name, so same-named entities never merge."""
    group_by = list(payload.get("group_by") or [])
    named = {row.get("as"): index for index, row in enumerate(payload.get("select") or [])}
    keyed = list(group_by)
    for dimension in group_by:
        key = display_key(config, str(dimension))
        if key and key not in keyed:
            if key in named:
                raise refusal(
                    f"select[{named[key]}]",
                    f"its column '{key}' is the key the answer carries beside '{dimension}'",
                    fix='Give it another name with "as".',
                    code="DUPLICATE_OUTPUT_ALIAS",
                    alias=key,
                )
            keyed.insert(keyed.index(dimension), key)
    return payload if keyed == group_by else {**payload, "group_by": keyed}


def bind_query(
    config: PackageConfig,
    registry: Registry | None,
    payload: dict[str, Any],
    *,
    row_filters: Sequence[RowFilter] = (),
    check_policies: Callable[[dict[str, Any], BoundQuery], object] | None = None,
) -> BoundQuery:
    """Prepare all bound branches without rendering SQL or accessing an adapter.

    ``row_filters`` are the filters the request's context applies; with any, the
    statement must read one filtered relation, and rollups are not routed to.
    The query's ``route_decisions`` answer their exact pairs for this query only.

    The one guard every child-scope clarification passes: it is offered only when every
    reading answers for this caller. Each option's where is bound once, with the same row
    filters and without asking (it holds child groups, so it cannot ask again), and then
    passes ``check_policies``, the caller's semantic policy gate, when given. If any option
    raises, the query is refused MIXED_GRAIN_INVALID, naming that option and its error code.
    """

    def bind(query: dict[str, Any]) -> BoundQuery:
        # Finish compiler dependency binding before authorizing a source diagnostic.
        refusals: list[SemanticLayerError] = []
        token = _source_filter_refusals.set(refusals)
        try:
            binding = _bind_with_row_filters(config, registry, query, row_filters)
            if refusals:
                if check_policies is not None:
                    check_policies(query, binding)
                raise refusals[0]
            return binding
        finally:
            _source_filter_refusals.reset(token)

    payload = _keyed_by_entity(config, payload)
    try:
        return bind(payload)
    except SemanticLayerError as exc:
        if exc.code != "AMBIGUOUS_CHILD_SCOPE":
            raise
        ambiguity = exc
    for option in ambiguity.details["clarification"]["options"]:
        # An option is the call's where; entity filters re-enter as the engine's predicates.
        where, entity_filters = engine_where(
            option["where"], grain=str((payload.get("time") or {}).get("grain") or "")
        )
        result_filters = [
            item
            for item in payload.get("metric_filters") or []
            if (item.get("expression") or {}).get("kind") != "metric_predicate"
        ]
        resent = {**payload, "where": where, "metric_filters": [*entity_filters, *result_filters]}
        try:
            binding = bind(resent)
            if check_policies is not None:
                check_policies(resent, binding)
        except Exception as exc:  # any failure withdraws the whole question
            code = exc.code if isinstance(exc, SemanticLayerError) else type(exc).__name__
            raise SemanticLayerError(
                "MIXED_GRAIN_INVALID",
                f"{ambiguity} The '{option['id']}' reading, a child group, is refused here "
                f"({code}), so no reading is offered.",
            ) from None
    raise ambiguity


def _bind_with_row_filters(
    config: PackageConfig,
    registry: Registry | None,
    payload: dict[str, Any],
    row_filters: Sequence[RowFilter],
) -> BoundQuery:
    decided = query_route_rows(config, payload, row_filters=row_filters)
    # Enforcement reads the whole package; a refusal names only policies the caller sees (a
    # hidden one by its kind), whichever statement it refuses.
    shown = {row.id for row in config.semantic_policies}
    shown_filters = [
        row if row.policy_id in shown else replace(row, policy_id="row_filter")
        for row in row_filters
    ]
    try:
        # A rollup may not hold the filter column, so a row-filtered query reads its base relation.
        with (
            aggregate_routing(not row_filters),
            refusing_fallible_calls(tuple(row.policy_id for row in shown_filters)),
            query_route_decisions(
                {pair: row.relationship_path for pair, (_, row) in decided.items()}
            ),
        ):
            bound = _bind_query(config, registry, payload)
    except SemanticLayerError as exc:
        if row_filters and exc.code == "AMBIGUOUS_PATH":
            # Offered only if every option answers under the caller's filters.
            visibility = visibility_for(config, shown_filters)
            for option in (exc.details.get("clarification") or {}).get("options") or []:
                route = (exc.details["start"], exc.details["target"], option["relationship_path"])
                hold_unowned_reads(config, visibility, roots=(), routes=[route])
        raise
    except RecursionError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG", "Expression dependencies are cyclic or too deep."
        ) from exc
    view = get_package_analysis(config).view
    if view is not None and view.hidden & bound.object_ids.union(*bound.leaf_objects.values()):
        # A caller's view holds nothing hidden from them; if it ever did, no SQL reads it.
        raise SemanticLayerError("OBJECT_NOT_FOUND", "The requested object was not found.")
    read = {(start, target) for start, target, _ in read_routes(bound.plan, bound.route_choices)}
    for (start, target), (index, _) in decided.items():
        if (start, target) not in read:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"routes[{index}] decides the route from '{start}' to '{target}', "
                "which this query never walks",
                details={
                    "path": f"routes[{index}]",
                    "reason": "route_decision_unused",
                    "source_entity": start,
                    "target_entity": target,
                    "hint": "Send only the decision of an option the query's AMBIGUOUS_PATH "
                    "refusal offered, with the same source_entity and target_entity.",
                },
            )
    row_filters = shown_filters
    visibility = visibility_for(config, row_filters)
    sql_ast = apply_row_filters(bound.sql_ast, visibility)
    if row_filters:
        # Holds only: a refusal here never authorizes anything the pass doesn't prove.
        hold_unowned_reads(
            config,
            visibility,
            roots=_row_roots(bound),
            routes=read_routes(bound.plan, bound.route_choices),
        )

    def filtered(statement: Any) -> SqlSelect:
        return apply_row_filters(statement, visibility)

    # A closing-day probe reads the stock's rows as its leaf does, under the same filters, and
    # so does a balance's coverage (its data_through); one that can't be filtered is refused.
    closing_reads = tuple(
        {**read, "probe": filtered(read["probe"])} for read in bound.closing_reads
    )
    balance_coverage = tuple(
        replace(cte, query=filtered(cte.query)) for cte in bound.balance_coverage
    )
    if row_filters:
        bound_slots = [node.slot for node in sql_nodes(sql_ast) if isinstance(node, SqlParameter)]
        _validate_restrictive_time_semantics(
            normalize_query(bound.plan.query, config=bound.config),
            base_of(bound.config),
            # Original slots identify applied policies; missing slots keep the guard conservative.
            row_filters=[
                row
                for row in row_filters
                if not bound_slots or any(slot is row.slot for slot in bound_slots)
            ],
        )
    return replace(
        bound,
        sql_ast=sql_ast,
        visibility=visibility if row_filters else None,
        balance_coverage=balance_coverage,
        closing_reads=closing_reads,
        route_decisions=tuple(
            {
                "source_entity": row.source_entity,
                "target_entity": row.target_entity,
                "relationship_path": list(row.relationship_path),
                "replaced": route_decision_basis(config, *pair),
            }
            for pair, (_, row) in decided.items()
        ),
    )


def _row_roots(bound: BoundQuery) -> set[str]:
    """The models whose rows a value starts from: the root, every measure's model (nested
    reads and predicates included) and the root of each route the SQL reads."""
    measures = get_package_analysis(bound.config).measures
    return {
        bound.plan.root_entity,
        *(plan.source_entity for plan in bound.plan.measure_plans),
        *(measures[oid].entity for oid in bound.object_ids if oid in measures),
    } - {""}


def query_route_rows(
    config: PackageConfig, payload: dict[str, Any], *, row_filters: Sequence[RowFilter] = ()
) -> dict[tuple[str, str], tuple[int, PathPreferenceConfig]]:
    """The query's ``route_decisions`` rows by exact pair, with each row's index, checked by
    the loader's rules (``INVALID_QUERY``: a bad row, or two rows for one pair) and taking one
    of the pair's routes within the hop ceiling (``fanout.pair_routes``, the resolver's own
    enumeration; else ``route_not_offered``).

    A route choice must never step around a row filter, so a row is refused
    (``POLICY_DENIED``) when a filter in ``row_filters`` reads any entity on any of those
    routes; a reviewed ``graph.path_preferences`` row changes routes there instead.
    """
    rows = route_decisions_from_payload(payload)
    if not rows:
        return {}
    entities = entity_references(config.entities)
    base = base_of(config)
    decided: dict[tuple[str, str], tuple[int, PathPreferenceConfig]] = {}
    for index, raw in enumerate(rows):
        where = f"routes[{index}]"
        try:
            row = check_route_row(
                asdict(raw), entities=entities, relationships=config.relationships
            )
        except RouteRowError as exc:
            details: dict[str, Any] = {"path": where, "reason": "invalid_route_decision"}
            if isinstance(exc, UnknownRelationshipError):
                details.update(_via_candidates(config, *exc.pair))
            raise SemanticLayerError("INVALID_QUERY", f"{where} {exc}", details=details) from None
        pair = (row.source_entity, row.target_entity)
        if pair in decided:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"{where} decides the route from '{pair[0]}' to '{pair[1]}' again "
                f"(routes[{decided[pair][0]}]); send one row per pair",
                details={"path": where, "reason": "duplicate_route_decision"},
            )
        routes = pair_routes(config, *pair)
        if list(row.relationship_path) not in routes:
            raise SemanticLayerError(
                "INVALID_QUERY",
                f"{where} is not one of the routes from '{pair[0]}' to '{pair[1]}' within "
                f"{package_hop_limit(config)} hops",
                details={
                    "path": where,
                    "reason": "route_not_offered",
                    "hint": "Send the decision of an option the query's AMBIGUOUS_PATH "
                    "refusal offered.",
                },
            )
        if row_filters:
            _hold_decided_route(config, pair, list(row.relationship_path), row_filters, where)
        _guard_route_row_filters(base, pair, row_filters, where, config)
        decided[pair] = (index, row)
    return decided


def _via_candidates(config: PackageConfig, source: str, target: str) -> dict[str, Any]:
    """What a route's ``via`` can name: the relationships walking from its source to its target
    in one hop (``closest_matches``); else the first three of the pair's routes, each ready to
    send (``other_routes``, the rest counted in ``more_routes``); else, when no route joins the
    pair, the relationships touching either entity (``closest_matches``)."""
    routes = pair_routes(config, source, target)
    joining = [path[0] for path in routes if len(path) == 1]
    found: dict[str, Any] = {}
    if joining:
        found["closest_matches"] = joining[:10]
        listed = "closest_matches lists the relationships that join `from` to `to` in one hop"
    elif routes:
        found["other_routes"] = [
            {"from": source, "to": target, "via": list(path)} for path in routes[:3]
        ]
        if len(routes) > 3:
            found["more_routes"] = len(routes) - 3
        listed = "other_routes lists the pair's routes, each ready to send in `routes`"
    else:
        found["closest_matches"] = sorted(
            rel.id
            for rel in config.relationships
            if {source, target} & {rel.source_entity, rel.target_entity}
        )[:10]
        listed = "no route joins the pair; closest_matches lists the relationships at either one"
    message = (
        '`via` lists relationship ids ("relationship.<id>"), one per hop from `from` to `to`, '
        f"not column names or entity ids; {listed}."
    )
    return {**found, "recovery_hints": [{"code": "FIX_QUERY_CALL", "message": message}]}


def _hold_decided_route(
    config: PackageConfig,
    pair: tuple[str, str],
    route: list[str],
    row_filters: Sequence[RowFilter],
    where: str,
) -> None:
    """A decided route the plan hold refuses is refused at its row (``where``), naming only
    the policies the caller's view shows."""
    shown = {row.id for row in config.semantic_policies}
    filters = [
        row if row.policy_id in shown else replace(row, policy_id="row_filter")
        for row in row_filters
    ]
    try:
        hold_unowned_reads(
            config, visibility_for(config, filters), roots=(), routes=[(*pair, route)]
        )
    except SemanticLayerError as exc:
        raise SemanticLayerError(
            exc.code, f"{where}: {exc}", details={**exc.details, "path": where}
        ) from None


def _guard_route_row_filters(
    config: PackageConfig,
    pair: tuple[str, str],
    row_filters: Sequence[RowFilter],
    where: str,
    display_config: PackageConfig,
) -> None:
    require_base(config)
    analysis = get_package_analysis(config)
    on_routes = {
        entity
        for path in pair_routes(config, *pair)
        for entity in walk_entities(analysis.relationships, pair[0], path)
    }
    tables = {analysis.entities[entity].table for entity in on_routes}
    policies = {row.policy_id for row in row_filters if row.table in tables}
    if policies:
        shown = {row.id for row in display_config.semantic_policies}
        raise SemanticLayerError(
            "POLICY_DENIED",
            f"{where} chooses a route from '{pair[0]}' to '{pair[1]}' under a row filter; "
            "a query cannot choose a route under a row filter",
            details={
                "reason": "route_override_under_row_policy",
                "path": where,
                "policy_ids": sorted(policies & shown),
                "hint": "Under a row filter only a reviewed graph.path_preferences row "
                "(record_route_decision) changes which route the package means.",
            },
        )


def read_routes(plan: LogicalPlan, route_choices: Sequence[RouteChoice]) -> list[RouteChoice]:
    """Each (start, target, route) the compiled SQL reads, once: the plan's root and leaf paths
    and the paths lowering read (predicates, conversions, a rewrite anchor's own lookups,
    nested compiles, direct key reads)."""
    choices: list[RouteChoice] = [
        (plan.root_entity, target, tuple(path))
        for target, path in sorted((plan.selected_paths or {}).items())
    ]
    for measure_plan in plan.measure_plans:
        choices.extend(
            (measure_plan.source_entity, selection.target_entity, tuple(selection.chosen_path))
            for selection in measure_plan.path_selections
        )
    choices.extend((start, target, tuple(path)) for start, target, path in route_choices)
    return list(dict.fromkeys(choices))


def _authored_leaves(plan: LogicalPlan, config: PackageConfig) -> frozenset[str]:
    """The filtered leaves only package metrics' own definitions compute.

    A leaf's alias names its filter, so a leaf the caller's own expressions also compute is
    the caller's, whatever a package metric says.
    """
    query = normalize_query(plan.query, config=config)
    recipes = _recipe_index(config)
    leaves: dict[bool, set[str]] = {False: set(), True: set()}

    def visit(expr: Any, authored: bool) -> None:
        if isinstance(expr, MetricRecipeRefExpr):
            if (recipe := recipes.get(expr.metric_recipe)) is not None:
                visit(recipe.expression, True)
            return
        if isinstance(expr, (AggregateExpr, ScopedAggregateExpr, ConversionExpr)):
            leaves[authored].add(_expression_alias(expr, config))
        for item in fields(expr) if is_dataclass(expr) else ():
            value = getattr(expr, item.name)
            for child in value if isinstance(value, list) else [value]:
                visit(child, authored)

    for expr in [
        *(row.expression for row in query.select),
        *(row.expression for row in query.metric_filters),
    ]:
        visit(expr, False)
    return frozenset(leaves[True] - leaves[False])


def _bind_query(
    config: PackageConfig, registry: Registry | None, payload: dict[str, Any]
) -> BoundQuery:
    # policy_context carries caller metadata and is never read as expressions;
    # every other request key, including unrecognized ones, is shape-checked.
    validate_expression_shapes(
        {key: value for key, value in payload.items() if key != "policy_context"}, path="query"
    )
    plan = plan_query(config, registry, payload)
    config = resolve_compile_config(plan, config)
    # Record selected plans and their real lowering, before SQL rendering.
    with (
        binding_dependencies() as dependencies,
        plan_bindings(plan) as leaves,
        recording_rollup_scans() as rollup_scans,
        recording_stock_key_gaps() as stock_key_gaps,
        recording_closing_reads() as closing_reads,
        recording_zero_outputs() as zero_outputs,
        recording_balance_edges() as balance_edges,
        recording_route_choices() as route_choices,
        observing(plan.query, config),
        capping_at(parse_now((payload.get("policy_context") or {}).get("now"))) as clock,
    ):
        _record_bound_plan(plan, config, leaves.leaves)
        sql_ast = attach_relation_ctes(config, lower_to_sql(plan, config))
    cuts = dependencies.cuts
    # A leaf read by a whole-query cut (a metric filter) filters the whole query too.
    shared = set().union(*(cut.leaves for cut in cuts if cut.owners is None))
    governed = {row.id for row in config.measures} | {row.id for row in config.metric_recipes}
    return BoundQuery(
        plan,
        config,
        sql_ast,
        frozenset(dependencies.object_ids),
        tuple(frozenset(cut.objects) for cut in cuts),
        {key: frozenset(roles) for key, roles in dependencies.temporal_roles.items()},
        tuple(sorted(dependencies.unresolved)),
        tuple(None if cut.owners is None or cut.owners & shared else cut.owners for cut in cuts),
        {
            alias: frozenset((ids | leaves.recipe_owners.get(alias, set())) & governed)
            for alias, ids in leaves.leaves.items()
        },
        frozenset(rollup_scans),
        stock_key_gaps=tuple(stock_key_gaps),
        zero_outputs=tuple(zero_outputs),
        retained_filtered_series=tuple(sorted(leaves.retained_filtered_series)),
        route_choices=tuple(route_choices),
        coverage_now=clock.literal if clock.read else "",
        balance_edges=tuple(balance_edges["outputs"]),
        balance_coverage=tuple(balance_edges["ctes"].values()),
        closing_reads=closing_reads_by_output(closing_reads),
        authored_leaves=_authored_leaves(plan, config),
    )
