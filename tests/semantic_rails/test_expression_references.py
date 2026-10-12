"""Reference coverage follows the published query schema's expression definitions."""

from __future__ import annotations

import itertools
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails import caveats, expressions, query_call, resource_access, runtime
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime_parts import responses
from semantic_rails.schema import SemanticPolicyConfig

SCHEMA = load_contract("query.v2.json")
DEFS = SCHEMA["$defs"]
EXPRESSION = "#/$defs/Expression"
MEASURE = "measure.jaffle.revenue_usd"
METRIC = "metric.sales.aov_usd"
ENTITY = "entity.jaffle_order"
DIMENSION = "dimension.jaffle_customer_type"
ROLE = "temporal_role.jaffle_order_time"
REF = {"kind": "measure", "measure": MEASURE}
LITERAL = {"kind": "literal", "value": 1}
RESTRICTED = {"audience": "restricted", "roles": ["restricted"]}
REFERENCE_FIELDS = {
    "measure": MEASURE,
    "metric": METRIC,
    "entity": ENTITY,
    "temporal_role": ROLE,
}
# The schema leaves these keys open (any JSON); a value each kind accepts.
OPEN_VALUES: dict[str, Any] = {
    "op": ">",
    "aggregation": "count",
    "function": "median",
    "name": "abs",
    "unit": "day",
    "value": 1,
    "column": "order_id",
    "period": "day",
    "offset": {"unit": "day", "value": 1},
    "window": {"unit": "day", "value": 1},
    "matching_mode": "first_converted_after_base",
}
KIND_OPEN_VALUES: dict[tuple[str, str], Any] = {
    ("arithmetic", "op"): "add",
    ("binary", "op"): "add",
    ("boolean", "op"): "and",
    ("date_add", "value"): LITERAL,
}
# Kinds a call reads only inside another expression (a condition, a value, a date): their
# references are walked like any other, and they bind as part of their parent.
NESTED_ONLY = frozenset({"column", "date_add", "in", "not_in"})


def _example(spec: dict[str, Any], key: str = "", kind: str = "") -> Any:
    if spec.get("$ref") == EXPRESSION:
        return deepcopy(LITERAL)
    if "$ref" in spec:
        return _example(DEFS[spec["$ref"].rsplit("/", 1)[1]], key, kind)
    if "const" in spec:
        return spec["const"]
    if "enum" in spec:
        return spec["enum"][0]
    if key in REFERENCE_FIELDS:
        return REFERENCE_FIELDS[key]
    if spec.get("type") == "object":
        return {
            name: _example(spec["properties"][name], name, kind)
            for name in spec.get("required", [])
        }
    if spec.get("type") == "array":
        return [_example(spec["items"], key, kind)]
    return deepcopy(KIND_OPEN_VALUES.get((kind, key), OPEN_VALUES.get(key, "day")))


def _table_column_expression_seed():
    return {
        "aggregation": "sum",
        "condition": {"kind": "literal", "value": True},
        "value": {"kind": "column", "table": "jaffle_order", "column": "order_id"},
    }


def _cases():
    """Every schema kind, plus every child/ref property independently.

    Opaque scalar parameters are data, not expression-reference positions.
    """
    for name, definition in DEFS.items():
        props = definition.get("properties", {})
        if "kind" not in props:
            continue
        kind = props["kind"]["const"]
        example = _example(definition, kind=kind)
        # Required arbitrary expression objects need a parseable seed.
        for key in ("condition", "expr", "low", "high"):
            if key in example:
                example[key] = deepcopy(LITERAL)
        if kind == "call":
            example["args"] = [deepcopy(LITERAL)]
        if kind in {
            "cumulative",
            "rolling",
            "prior_period",
            "period_to_date",
            "entity_value",
            "metric_predicate",
        }:
            example["input"] = deepcopy(REF)
        if kind == "aggregate_if":
            example.update(_table_column_expression_seed())
        if kind == "conversion":
            example.update(
                {
                    "base": {"kind": "measure", "measure": "measure.jaffle.session_starts"},
                    "converted": {"kind": "measure", "measure": "measure.jaffle.order_count"},
                    "entity": "entity.jaffle_customer",
                    "window": {"unit": "day", "value": 28},
                    "matching_mode": "first_converted_after_base",
                }
            )
        if kind == "distribution":
            example.update(
                {
                    "function": "avg",
                    "over": {"kind": "entity_value", "entity": ENTITY, "input": deepcopy(REF)},
                }
            )
        if kind == "period_to_date":
            example["period"] = "month"
        if kind == "metric_predicate":
            example.update({"op": ">", "value": 0})
        if kind == "share":
            example.update({"input": MEASURE, "over": [DIMENSION]})
        for key, spec in props.items():
            probes: list[tuple[Any, str, bool]] = []
            if kind == "aggregate_if" and key in {"condition", "value"}:
                column = {"kind": "column", "entity": ENTITY, "column": "order_id"}
                value = (
                    column
                    if key == "value"
                    else {
                        "kind": "comparison",
                        "op": ">",
                        "left": column,
                        "right": deepcopy(LITERAL),
                    }
                )
                probes.append((value, ENTITY, True))
            elif kind == "conversion" and key in {"base", "converted"}:
                measure_id = (
                    "measure.jaffle.session_starts"
                    if key == "base"
                    else "measure.jaffle.order_count"
                )
                probes.append(({"kind": "measure", "measure": measure_id}, measure_id, True))
            elif kind == "conversion" and key == "entity":
                probes.append(("entity.jaffle_customer", "entity.jaffle_customer", True))
            elif kind == "share" and key == "input":
                # A call names the id; the engine's form, which the walkers read, is a ref.
                probes.append((deepcopy(REF), MEASURE, False))
            elif kind == "share" and key == "over":
                probes.append(([DIMENSION], DIMENSION, True))
            elif kind == "entity_value" and key == "where":
                continue  # value predicates are scalar data, not dimension references
            elif kind == "distribution" and key == "over":
                probes.append(
                    (
                        {"kind": "entity_value", "entity": ENTITY, "input": deepcopy(REF)},
                        MEASURE,
                        True,
                    )
                )
            elif key in REFERENCE_FIELDS:
                probes.append((REFERENCE_FIELDS[key], REFERENCE_FIELDS[key], True))
            elif spec.get("$ref") == EXPRESSION:
                probes.append((deepcopy(REF), MEASURE, True))
            elif key == "args":
                args = [deepcopy(REF)]
                if kind == "boolean" and example["op"] in {"and", "or"}:
                    args.append({"kind": "literal", "value": True})
                probes.append((args, MEASURE, True))
            elif key == "whens":
                for slot in ("when", "then"):
                    row = {"when": deepcopy(LITERAL), "then": deepcopy(LITERAL)}
                    row[slot] = deepcopy(REF)
                    probes.append(([row], MEASURE, True))
            elif key in ("partition_by", "constant_properties"):
                probes.append(([DIMENSION], DIMENSION, True))
            elif key == "where":
                probes.append(([{"field": DIMENSION, "op": "is", "value": "new"}], DIMENSION, True))
            elif key == "predicates":
                predicate = {"entity": ENTITY, "measure": MEASURE, "op": "gt", "value": 0}
                probes.append(([{**predicate, "over": "all_time"}], MEASURE, True))
            elif key == "dimension_bindings":
                probes.append(({DIMENSION: {"side": "base"}}, DIMENSION, True))
            elif key == "anchor":
                probes.append(
                    (
                        {"temporal_role": "temporal_role.jaffle_customer_first_order_at"},
                        "temporal_role.jaffle_customer_first_order_at",
                        False,
                    )
                )
            elif key == "value" and kind == "literal":
                probes.append((MEASURE, "", False))
            elif key in {"filter", "window"} and kind in {"aggregate", "semi_additive"}:
                probes.append(({"extension": [{"field": DIMENSION}]}, DIMENSION, False))
            for index, (value, expected, normalize) in enumerate(probes):
                payload = deepcopy(example)
                payload[key] = value
                if key == "anchor":
                    payload["window"] = {"unit": "day", "value": 1}
                bound = normalize and kind not in NESTED_ONLY
                yield f"{name}-{kind}-{key}-{index}", payload, expected, bound


CASES = list(_cases())
BOUND = [case for case in CASES if case[3]]
WALKERS = [
    responses._collect_expr_object_ids,
    caveats._collect_expr_object_ids,
    resource_access._references,
]
SOURCE = "configs/semantic_rails/jaffle_shop"
SCOPES = ["roles", "audiences"]


def _in_case_else(expression):
    return {
        "kind": "case",
        "whens": [{"when": {"kind": "literal", "value": False}, "then": deepcopy(LITERAL)}],
        "else": {"kind": "ratio", "numerator": expression, "denominator": deepcopy(LITERAL)},
    }


def _select(expression, **item):
    return {"select": [{"expression": expression, "as": "x", **item}]}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
def test_schema_reference_positions(case):
    _, expression, expected, _ = case
    for walker in WALKERS:
        for candidate in (expression, _in_case_else(expression)):
            found = walker(candidate)
            assert expected in found if expected else not found, walker.__module__


def _rotate_scope(rows):
    """Pair each row with one policy scope, so each scope runs on half of them.

    The scope only changes how a policy matches, never how an expression is walked, so
    crossing it with every row repeats the same walk. The shift after each full turn keeps a
    short trailing axis from always meeting the same scope.
    """
    return [(*row, SCOPES[(i + i // len(SCOPES)) % len(SCOPES)]) for i, row in enumerate(rows)]


@pytest.fixture(scope="module")
def base_config():
    from semantic_rails.config import load_package_config

    return load_package_config(SOURCE)


def _runtime(config):
    return runtime.Runtime.from_config(config, source_path=SOURCE)


def _deny(protected, scope="audiences"):
    return SemanticPolicyConfig(
        id="policy.test",
        kind="object_access",
        object_ids=[protected],
        action="deny",
        **{scope: ["restricted"]},
    )


@pytest.fixture(scope="module")
def denying(base_config):
    """The package with one policy denying an object to the restricted caller, per scope."""
    engines = {}

    def build(protected, scope):
        if (protected, scope) not in engines:
            config = replace(base_config, semantic_policies=[_deny(protected, scope)])
            engines[protected, scope] = _runtime(config)
        return engines[protected, scope]

    yield build
    for engine in engines.values():
        engine.close()


@pytest.fixture(scope="module")
def open_engine(base_config):
    """The package with no object policies, plus the alias and metrics the permitted calls name."""
    config = _with_dependency_recipe(_spelling_case(base_config, "alias")[0])
    engine = _runtime(replace(config, semantic_policies=[]))
    yield engine
    engine.close()


def _internal(config, query):
    """The compiler's own form of a call."""
    return query_call.read_call(query, config=config)[0]


def _guard(monkeypatch, engine):
    """Fail the test if the request reaches rendering, the compiler or the warehouse."""
    from semantic_rails import compiler

    def reached(*args, **kwargs):
        pytest.fail("reached rendering, the compiler or the warehouse before the refusal")

    monkeypatch.setattr(compiler, "render_select_for_profile", reached)
    monkeypatch.setattr(runtime, "compile_query", reached)
    monkeypatch.setattr(engine, "_compile", reached)
    monkeypatch.setattr(engine, "_get_adapter", reached)


def _assert_refused(engine, query, code="POLICY_DENIED", context=RESTRICTED):
    assert engine.validate(query, context=context)["errors"][0]["code"] == code
    for operation in (engine.compile, engine.query):
        with pytest.raises(SemanticLayerError) as exc:
            operation(query, context=context)
        assert exc.value.code == code
        if code == "POLICY_DENIED":
            assert exc.value.details["policy_effects"][0]["action"] == "deny"


def _assert_segment_refused(engine, segment_id):
    result = engine.segment_validate(segment_id, policy_context=RESTRICTED)
    assert result["errors"][0]["code"] == "POLICY_DENIED"
    # The preview also guards the segment's own membership/count SQL path.
    for call in (engine.segment_explain, engine.segment_preview):
        with pytest.raises(SemanticLayerError) as exc:
            call(segment_id, policy_context=RESTRICTED)
        assert exc.value.code == "POLICY_DENIED"


def _assert_grant_refuses(config, query, metric_id):
    """A metric grant never authorizes an object the granted metric depends on."""
    from semantic_rails.request_context import RequestContext

    context = RequestContext(
        audience="restricted", roles=("restricted",), metric_allowlist=(metric_id,)
    )
    with pytest.raises(SemanticLayerError) as exc:
        resource_access.ResourceAccess(config, context).enforce_query(query)
    assert exc.value.code == "RESOURCE_ACCESS_DENIED"


def _schema_query(case, nested=False):
    name, raw, _, _ = case
    expression = deepcopy(raw)
    kind = expression.get("kind", "")
    query = {}
    if kind in {"rolling", "prior_period", "cumulative", "period_to_date"}:
        query["time"] = {"temporal_role": ROLE, "grain": "day"}
        if expression.get("partition_by"):
            query["group_by"] = list(expression["partition_by"])
    if kind == "conversion" and expression.get("dimension_bindings"):
        query["group_by"] = list(expression["dimension_bindings"])
    if kind == "share":
        query["group_by"] = list(expression["over"])
    if kind == "entity_value":
        expression = {"kind": "distribution", "function": "avg", "over": expression}
    if kind == "metric_predicate":
        if nested:
            expression["input"] = {
                "kind": "ratio",
                "numerator": expression["input"],
                "denominator": {"kind": "measure", "measure": "measure.jaffle.order_count"},
            }
        # A call filters by an inline predicate as a predicate of a scoped aggregate.
        expression.pop("kind")
        scoped = {
            "kind": "scoped_aggregate",
            "measure": "measure.jaffle.order_count",
            "predicates": [expression],
        }
        query["select"] = [{"expression": scoped, "as": "value"}]
    else:
        if nested and kind not in {"distribution", "entity_value"}:
            expression = _in_case_else(expression)
        query["select"] = [{"expression": expression, "as": "value"}]
        if nested and kind in {"distribution", "entity_value"}:
            query["select"].append({"expression": deepcopy(REF), "as": "sibling"})
    return query


@pytest.mark.parametrize(
    "case,scope",
    [(case, SCOPES[i % 2]) for i, case in enumerate(BOUND)],
    ids=[case[0] for case in BOUND],
)
def test_runtime_blocks_before_compile(base_config, denying, monkeypatch, case, scope):
    """Each bound position is denied before rendering: directly under ``scope``, and nested in
    a case/ratio under the other scope."""
    from semantic_rails import compiler

    protected = case[2]
    runs = []
    for nested, policy_scope in ((False, scope), (True, SCOPES[1 - SCOPES.index(scope)])):
        query = _schema_query(case, nested)
        internal = _internal(base_config, query)
        unprotected = compiler.bind_query(base_config, None, internal)
        assert protected in unprotected.object_ids
        assert compiler.compile_query(base_config, None, internal, binding=unprotected)["sql"]
        runs.append((denying(protected, policy_scope), query))
    for engine, query in runs:
        _guard(monkeypatch, engine)
        _assert_refused(engine, query)


@pytest.mark.parametrize("kind", ["future_expression", False, 0, None, {}, []])
def test_unknown_expression_fails_closed(kind):
    with pytest.raises(SemanticLayerError) as exc:
        expressions.collect_object_references({"kind": kind, "child": REF})
    assert exc.value.code == "INVALID_EXPRESSION_AST"


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "column", "column": "amount", "entity": ENTITY},
        {"kind": "date_add", "unit": "day", "value": LITERAL, "date": REF},
        {"kind": "in", "expr": LITERAL, "values": [REF]},
        {"kind": "not_in", "expr": LITERAL, "values": [REF]},
        {"kind": "nullif", "value": REF, "null_value": LITERAL},
        {"kind": "percentile", "p": 0.9, "measure": MEASURE},
    ],
)
def test_nested_config_and_spec_references(spec):
    for walker in WALKERS:
        assert {MEASURE, ENTITY} & set(walker({"input": spec})), walker.__module__
        with pytest.raises(SemanticLayerError):
            walker({"input": {"kind": "future_expression", "child": REF}})


def test_schema_kinds_are_all_exercised():
    expected = {
        kind
        for definition in DEFS.values()
        for kind in definition.get("properties", {})
        .get("kind", {})
        .get("enum", [definition.get("properties", {}).get("kind", {}).get("const")])
        if kind is not None
    }
    assert {case[1].get("kind") for case in CASES} - {None} == expected
    # Literal has separate permitted-data controls. Every other schema tag a call may select
    # must have a strict bind/compile/deny probe.
    assert {case[1].get("kind") for case in BOUND} - {None} == (
        expected - {"literal"} - NESTED_ONLY
    )


# Tags carried as data inside expression payloads (inline thresholds and
# value-filter rows); they are never parsed as expressions.
SHAPE_DATA_TAGS = {"percentile", "value_filter"}


def test_shape_vocabulary_follows_the_expression_parser():
    """Query, segment and config expressions share the parser's tag vocabulary.

    Every kind the parser reads is accepted before normalization, and only data
    tags may extend the shape vocabulary beyond what the parser dispatches.
    """
    parser_kinds = set(expressions._VALID_KEYS_BY_KIND)
    schema_kinds = {
        kind
        for definition in DEFS.values()
        for kind in definition.get("properties", {})
        .get("kind", {})
        .get("enum", [definition.get("properties", {}).get("kind", {}).get("const")])
        if kind is not None
    }
    assert schema_kinds <= parser_kinds
    # The schema lists every kind a call may use: all the parser's but the retired ones.
    assert parser_kinds - schema_kinds == query_call.RETIRED_KINDS
    assert expressions._reference_expression_kinds() == parser_kinds | SHAPE_DATA_TAGS
    for kind in parser_kinds | SHAPE_DATA_TAGS:
        for spelling in (kind, f" {kind} "):
            expressions.validate_expression_shapes({"kind": spelling, "child": LITERAL})
    for kind in ("future_expression", "NULLIF", "notin"):
        for check in (
            expressions.validate_expression_shapes,
            lambda raw: expressions.parse_semantic_expression(raw, context="query"),
        ):
            with pytest.raises(SemanticLayerError) as exc:
                check({"kind": kind, "measure": MEASURE})
            assert exc.value.code == "INVALID_EXPRESSION_AST"


ORDER_COLUMN = {"kind": "column", "entity": ENTITY, "column": "order_id"}
ALIAS_EXAMPLES = {
    "nullif": {
        "kind": "nullif",
        "value": deepcopy(REF),
        "null_value": {"kind": "literal", "value": 0},
    },
    "not_in": {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": {
            "kind": "not_in",
            "expr": ORDER_COLUMN,
            "values": [{"kind": "literal", "value": 0}, {"kind": "literal", "value": 1}],
        },
        "value": ORDER_COLUMN,
    },
}
ALIAS_SQL = {"nullif": "NULLIF(", "not_in": "NOT IN"}
ALIAS_SLOTS = [
    ("nullif", "value"),
    ("nullif", "null_value"),
    ("not_in", "expr"),
    ("not_in", "values"),
]


def _alias_query(expression, placement):
    if placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": deepcopy(LITERAL)}
    return _select(expression)


def _alias_segment(config, expression):
    segment = replace(
        config.segments[0], metric_filters=[{"expression": expression, "op": ">", "value": 0}]
    )
    return segment, replace(config, segments=[segment, *config.segments[1:]])


CUSTOMER_COLUMN = {"kind": "column", "entity": "entity.jaffle_customer", "column": "customer_id"}


def _alias_dependency(alias, slot, *, protected=True):
    """An alias whose ``slot`` holds its only reference to the protected object.

    With ``protected=False`` the slot holds an unprotected reference instead:
    the control showing that the slot alone decides whether it is bound.
    """
    if alias == "nullif":
        expression = {
            "kind": "nullif",
            "value": {"kind": "measure", "measure": "measure.jaffle.order_count"},
            "null_value": {"kind": "literal", "value": 0},
        }
        unprotected = {"kind": "measure", "measure": "measure.jaffle.order_count"}
        expression[slot] = deepcopy(REF) if protected else unprotected
        return expression, MEASURE
    column = deepcopy(ORDER_COLUMN if protected else CUSTOMER_COLUMN)
    condition = {"kind": "not_in", "expr": deepcopy(LITERAL), "values": [column]}
    if slot == "expr":
        values = [{"kind": "literal", "value": 0}, {"kind": "literal", "value": 1}]
        condition = {"kind": "not_in", "expr": column, "values": values}
    expression = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": condition,
        "value": deepcopy(LITERAL),
    }
    return expression, ENTITY


# A package's metric filter (a segment's) is the one filter slot an inline expression has.
@pytest.mark.parametrize("alias", sorted(ALIAS_EXAMPLES))
def test_parser_aliases_validate_and_compile_in_a_segment(base_config, alias):
    expression = deepcopy(ALIAS_EXAMPLES[alias])
    segment, config = _alias_segment(replace(base_config, semantic_policies=[]), expression)
    engine = _runtime(config)
    try:
        assert engine.segment_validate(segment.id)["ok"]
        assert ALIAS_SQL[alias] in engine.segment_explain(segment.id)["rendered_sql"].upper()
    finally:
        engine.close()


@pytest.mark.parametrize(
    "alias,slot,placement,scope",
    _rotate_scope((*slot, placement) for slot in ALIAS_SLOTS for placement in ["direct", "nested"]),
)
def test_parser_alias_dependencies_block_before_rendering(
    base_config, denying, monkeypatch, alias, slot, placement, scope
):
    from semantic_rails import compiler

    expression, protected = _alias_dependency(alias, slot)
    query = _alias_query(expression, placement)
    control = _alias_query(_alias_dependency(alias, slot, protected=False)[0], placement)
    control_binding = compiler.bind_query(base_config, None, _internal(base_config, control))
    assert protected not in control_binding.object_ids
    internal = _internal(base_config, query)
    unprotected = compiler.bind_query(base_config, None, internal)
    assert protected in unprotected.object_ids
    assert compiler.compile_query(base_config, None, internal, binding=unprotected)["sql"]
    engine = denying(protected, scope)
    _guard(monkeypatch, engine)
    _assert_refused(engine, query)


@pytest.mark.parametrize("alias,slot,scope", _rotate_scope(ALIAS_SLOTS))
def test_parser_alias_segment_dependencies_block_before_rendering(
    base_config, monkeypatch, alias, slot, scope
):
    expression, protected = _alias_dependency(alias, slot)
    segment, config = _alias_segment(base_config, expression)
    engine = _runtime(replace(config, semantic_policies=[_deny(protected, scope)]))
    _guard(monkeypatch, engine)
    try:
        _assert_segment_refused(engine, segment.id)
    finally:
        engine.close()


def test_policy_context_metadata_is_not_an_expression_position(open_engine):
    note = {"kind": "annotation", "detail": {"kind": "future_expression"}}
    permitted = _select("measure.jaffle.order_count")
    context = {"audience": "finance", "note": note}
    assert open_engine.validate(permitted, context=context)["ok"]
    assert open_engine.compile(permitted, context=context)["rendered_sql"]


COUNT = {"kind": "measure", "measure": "measure.jaffle.order_count"}
UNKNOWN = {"kind": "future_expression"}
UNKNOWN_WITH_MEASURE = {"kind": "future_expression", "measure": MEASURE, "child": REF}
# A call checks every key it reads, the expressions it holds included, and refuses an unknown
# kind wherever it reads one (a predicate's input included). Only a top-level
# ``policy_context`` is skipped.
REFUSED_SHAPES = {
    "unrecognized_key": ({**_select(COUNT), "future_key": UNKNOWN}, "INVALID_QUERY"),
    "limits": ({**_select(COUNT), "limits": {"note": UNKNOWN}}, "INVALID_QUERY"),
    "entity_value_where": (
        {
            "select": [
                {
                    "expression": {
                        "kind": "distribution",
                        "function": "avg",
                        "over": {
                            "kind": "entity_value",
                            "entity": "entity.jaffle_customer",
                            "input": COUNT,
                            "where": [{"kind": "future_filter", "op": ">", "value": 3}],
                        },
                    }
                }
            ]
        },
        "INVALID_EXPRESSION_AST",
    ),
    "select_item_policy_context": (_select(COUNT, policy_context=UNKNOWN), "INVALID_QUERY"),
    "expression_policy_context": (_select({**COUNT, "policy_context": UNKNOWN}), "INVALID_QUERY"),
    "unknown_kind": (_select(UNKNOWN_WITH_MEASURE), "INVALID_QUERY"),
    "nested_unknown_kind": (
        _select({"kind": "ratio", "numerator": UNKNOWN_WITH_MEASURE, "denominator": LITERAL}),
        "INVALID_QUERY",
    ),
    "predicate_input_unknown_kind": (
        _select(
            {
                "kind": "scoped_aggregate",
                "measure": MEASURE,
                "predicates": [
                    {
                        "input": UNKNOWN_WITH_MEASURE,
                        "entity": ENTITY,
                        "op": "gt",
                        "value": 0,
                        "over": "all_time",
                    }
                ],
            }
        ),
        "INVALID_QUERY",
    ),
}


@pytest.mark.parametrize("query,code", REFUSED_SHAPES.values(), ids=list(REFUSED_SHAPES))
def test_request_shapes_are_checked_before_rendering(open_engine, monkeypatch, query, code):
    _guard(monkeypatch, open_engine)
    _assert_refused(open_engine, deepcopy(query), code, context=None)


def test_a_note_key_never_reaches_the_compiler(open_engine):
    """A key starting with ``_`` is a note for people: the call ignores it."""
    query = {**_select(COUNT), "_note": UNKNOWN}
    assert open_engine.validate(query)["ok"]
    assert "_note" not in open_engine.engine_payload(query)


def test_sibling_paths_and_recipe_closure(base_config, monkeypatch):
    from semantic_rails.metadata_parts.valid_values import valid_values_payload

    expression = {"kind": "ratio", "numerator": deepcopy(REF), "denominator": LITERAL}
    nested_filter = {"expression": expression, "op": ">", "value": 0}
    segment = replace(base_config.segments[0], metric_filters=[nested_filter])
    recipe = replace(
        base_config.metric_recipes[0],
        expression=expressions.parse_semantic_expression(expression, context="query"),
    )
    config = replace(
        base_config,
        semantic_policies=[_deny(MEASURE)],
        segments=[segment],
        metric_recipes=[recipe, *base_config.metric_recipes[1:]],
    )
    engine = _runtime(config)
    _guard(monkeypatch, engine)
    try:
        _assert_segment_refused(engine, segment.id)
        for call in (
            lambda: engine.compile(_select(recipe.id), context=RESTRICTED),
            # The metadata helper reads the engine's own form of a seed query.
            lambda: valid_values_payload(
                engine,
                dimension_id=DIMENSION,
                allow_live_query=True,
                include_counts=True,
                query={"metric_filters": [nested_filter], "policy_context": RESTRICTED},
            ),
        ):
            with pytest.raises(SemanticLayerError) as exc:
                call()
            assert exc.value.code == "POLICY_DENIED"
        for expr in (expression, {"metric": recipe.id}):
            _assert_grant_refuses(config, _select(expr), recipe.id)
    finally:
        engine.close()


def test_allowed_nested_expression_executes(runtime_factory):
    engine = runtime_factory("jaffle_shop")
    query = _select(_in_case_else(deepcopy(REF)))
    try:
        assert engine.validate(query)["ok"]
        assert engine.compile(query)["rendered_sql"]
        assert engine.query(query)["rows"]
    finally:
        engine.close()


SCALAR_FIELDS = {"number": 123, "boolean": False, "null": None}
SPELLINGS = ["canonical", "padded", "name", "label", "alias", "padded_alias", *SCALAR_FIELDS]
PREDICATES = ["measure", "metric", "input"]


def _spelling_expression(dimension, spelling):
    """A scoped aggregate naming the dimension (a where field) or the measure (a predicate)."""
    expression = {"kind": "scoped_aggregate", "measure": "measure.jaffle.order_count"}
    if spelling in SPELLINGS:
        field = {
            "canonical": dimension.id,
            "padded": f" {dimension.id} ",
            "name": dimension.name,
            "label": dimension.label,
            "alias": "customer_kind",
            "padded_alias": " customer_kind ",
            **SCALAR_FIELDS,
        }[spelling]
        expression["where"] = [{"field": field, "op": "is", "value": "new"}]
        return expression
    reference = {
        "measure": {"measure": f" {MEASURE} "},
        "metric": {"metric": f" {METRIC} "},
        "input": {"input": {"kind": "measure", "measure": f" {MEASURE} "}},
    }[spelling]
    expression["predicates"] = [
        {"entity": ENTITY, **reference, "op": "gt", "value": 0, "over": "all_time"}
    ]
    return expression


def _spelling_case(base_config, spelling):
    dimension = replace(
        next(row for row in base_config.dimensions if row.id == DIMENSION),
        aliases=["customer_kind"],
        **({"label": str(SCALAR_FIELDS[spelling])} if spelling in SCALAR_FIELDS else {}),
    )
    config = replace(
        base_config,
        dimensions=[dimension if row.id == DIMENSION else row for row in base_config.dimensions],
        metric_recipes=[
            *base_config.metric_recipes,
            replace(
                base_config.metric_recipes[0],
                id=METRIC,
                kind="simple",
                expression=expressions.parse_semantic_expression(REF, context="config"),
            ),
        ],
    )
    protected = DIMENSION if spelling in SPELLINGS else MEASURE
    return config, _spelling_expression(dimension, spelling), protected


def _config_form(expression):
    """A call's expression as a package metric writes it: a predicate states no ``over``."""
    if "predicates" not in expression:
        return expression
    predicates = [
        {**{key: value for key, value in row.items() if key != "over"}, "op": ">"}
        for row in expression["predicates"]
    ]
    return {**expression, "predicates": predicates}


@pytest.mark.parametrize(
    "spelling,placement,scope",
    _rotate_scope(
        [
            (spelling, placement)
            for spelling in [*SPELLINGS, *PREDICATES]
            for placement in ["direct", "nested", "recipe"]
        ]
        + [
            (spelling, placement)
            for spelling in SPELLINGS
            for placement in ["conversion", "conversion_recipe"]
        ]
    ),
)
def test_reference_spellings_denied_before_compile(
    base_config, monkeypatch, spelling, placement, scope
):
    config, expression, protected = _spelling_case(base_config, spelling)
    if placement.startswith("conversion"):
        expression = _conversion_filter(expression)
    recipe = replace(
        config.metric_recipes[0],
        id="metric.test.spelling",
        expression=expressions.parse_semantic_expression(
            _config_form(expression), context="config"
        ),
    )
    config = replace(
        config,
        metric_recipes=[*config.metric_recipes, recipe],
        semantic_policies=[_deny(protected, scope)],
    )
    if placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
    elif placement.endswith("recipe"):
        expression = recipe.id
    query = _select(expression)
    engine = _runtime(config)
    _guard(monkeypatch, engine)
    try:
        _assert_refused(engine, query)
        if placement.endswith("recipe"):
            _assert_grant_refuses(config, _internal(config, query), recipe.id)
    finally:
        engine.close()


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_recipe_filter_spec_spellings(base_config, spelling):
    config, expression, protected = _spelling_case(base_config, spelling)
    recipe = replace(
        config.metric_recipes[0],
        id="metric.test.filter",
        expression=expressions.AggregateExpr(MEASURE, filter={"all": expression["where"]}),
        filter_spec={"all": expression["where"]},
    )
    config = replace(
        config,
        metric_recipes=[*config.metric_recipes, recipe],
        semantic_policies=[_deny(protected)],
    )
    query = _internal(config, _select(recipe.id))
    assert protected in runtime._query_object_ids(query, config)
    _assert_grant_refuses(config, query, recipe.id)


def test_ambiguous_filter_alias_fails_closed(base_config, monkeypatch):
    config, expression, _ = _spelling_case(base_config, "alias")
    other = next(row for row in config.dimensions if row.id != DIMENSION)
    config = replace(
        config,
        dimensions=[
            replace(row, aliases=["customer_kind"]) if row.id == other.id else row
            for row in config.dimensions
        ],
    )
    engine = _runtime(config)
    monkeypatch.setattr(engine, "_compile", lambda *a, **k: pytest.fail("ambiguous alias compiled"))
    try:
        assert engine.validate(_select(expression))["errors"][0]["code"] == "AMBIGUOUS_ALIAS"
    finally:
        engine.close()


def _conversion_filter(expression):
    return {
        "kind": "conversion",
        "base": {
            "kind": "aggregate",
            "measure": "measure.jaffle.session_starts",
            "filter": {
                "all": [
                    {"dimension": row["field"], "op": row["op"], "value": row["value"]}
                    for row in expression["where"]
                ]
            },
        },
        "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
        "entity": "entity.jaffle_customer",
        "window": {"unit": "day", "value": 28},
        "matching_mode": "first_converted_after_base",
    }


def _table_column_expression(slot):
    column = {"kind": "column", "column": "order_id", "table": "jaffle_order"}
    return {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": (
            {"kind": "comparison", "op": ">", "left": column, "right": LITERAL}
            if slot == "condition"
            else {"kind": "literal", "value": True}
        ),
        "value": column if slot == "value" else LITERAL,
    }


@pytest.mark.parametrize(
    "slot,placement,scope",
    _rotate_scope(itertools.product(["condition", "value"], ["direct", "nested", "recipe"])),
)
def test_table_column_denied_before_compile(base_config, monkeypatch, slot, placement, scope):
    expression = _table_column_expression(slot)
    authored = replace(
        next(row for row in base_config.measures if row.id == MEASURE),
        id="measure.test.column",
        entity=ENTITY,
        default_aggregation="sum",
        allowed_aggregations=["sum"],
        expr=expressions.CaseExpr(
            whens=[
                expressions.CaseWhenExpr(
                    when=expressions.parse_semantic_expression(
                        expression["condition"], context="config"
                    ),
                    then=expressions.parse_semantic_expression(
                        expression["value"], context="config"
                    ),
                )
            ]
        ),
    )
    recipe = replace(
        base_config.metric_recipes[0],
        id="metric.test.column",
        expression=expressions.MeasureRefExpr(authored.id),
    )
    config = replace(
        base_config,
        measures=[*base_config.measures, authored],
        metric_recipes=[*base_config.metric_recipes, recipe],
        semantic_policies=[_deny(ENTITY, scope)],
    )
    if placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
    elif placement == "recipe":
        expression = recipe.id
    query = _select(expression)
    engine = _runtime(config)
    _guard(monkeypatch, engine)
    try:
        _assert_refused(engine, query)
        if placement == "recipe":
            _assert_grant_refuses(config, _internal(config, query), recipe.id)
    finally:
        engine.close()


DEPENDENCY_FORMS = ["measure", "metric", "nested_metric", "group", "where", "time"]
DEPENDENCY_RECIPE = "metric.test.dependency"


def _with_dependency_recipe(config):
    recipe = replace(
        config.metric_recipes[0],
        id=DEPENDENCY_RECIPE,
        expression=expressions.MeasureRefExpr(MEASURE),
    )
    return replace(config, metric_recipes=[*config.metric_recipes, recipe])


def _dependency_query(form):
    """A call reading an authored object through one of its forms."""
    if form in {"measure", "metric", "nested_metric"}:
        expression = deepcopy(REF) if form == "measure" else DEPENDENCY_RECIPE
        if form == "nested_metric":
            expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
        return _select(expression)
    query = _select(deepcopy(LITERAL))
    if form == "group":
        query["group_by"] = [DIMENSION]
    elif form == "where":
        query["where"] = [{"field": DIMENSION, "op": "is", "value": "new"}]
    else:
        query["time"] = {"temporal_role": ROLE, "grain": "day"}
    return query


def _dependency_owner(config, form):
    """The entity whose table a dependency form reads."""
    if form in {"measure", "metric", "nested_metric"}:
        return next(row.entity for row in config.measures if row.id == MEASURE)
    dimension = DIMENSION
    if form == "time":
        dimension = next(row.dimension for row in config.temporal_roles if row.id == ROLE)
    return next(row.entity for row in config.dimensions if row.id == dimension)


@pytest.mark.parametrize("form,scope", _rotate_scope((form,) for form in DEPENDENCY_FORMS))
def test_authored_dependencies_denied_before_compile(base_config, monkeypatch, form, scope):
    config = _with_dependency_recipe(base_config)
    config = replace(config, semantic_policies=[_deny(_dependency_owner(config, form), scope)])
    query = _dependency_query(form)
    engine = _runtime(config)
    _guard(monkeypatch, engine)
    try:
        _assert_refused(engine, query)
        if form == "metric":
            _assert_grant_refuses(config, _internal(config, query), DEPENDENCY_RECIPE)
    finally:
        engine.close()


# One call per reference shape a permitted caller may write, and a SQL fragment it renders.
PERMITTED = {
    "where_padded_alias": (lambda d: _select(_spelling_expression(d, "padded_alias")), ""),
    "predicate_metric": (lambda d: _select(_spelling_expression(d, "metric")), ""),
    "conversion_label": (
        lambda d: _select(_conversion_filter(_spelling_expression(d, "label"))),
        "",
    ),
    "table_column_condition": (lambda d: _select(_table_column_expression("condition")), ""),
    "table_column_value": (lambda d: _select(_table_column_expression("value")), ""),
    "nullif": (lambda d: _alias_query(ALIAS_EXAMPLES["nullif"], "direct"), "NULLIF("),
    "nested_not_in": (lambda d: _alias_query(ALIAS_EXAMPLES["not_in"], "nested"), "NOT IN"),
    **{
        f"authored_{form}": (lambda d, form=form: _dependency_query(form), "")
        for form in DEPENDENCY_FORMS
    },
}


@pytest.mark.parametrize("build,fragment", PERMITTED.values(), ids=list(PERMITTED))
def test_permitted_references_compile(base_config, open_engine, build, fragment):
    query = build(next(row for row in base_config.dimensions if row.id == DIMENSION))
    assert open_engine.validate(query)["ok"]
    assert fragment in open_engine.compile(query)["rendered_sql"].upper()


def test_authored_dependency_cycles_fail_closed(base_config):
    from semantic_rails.compiler import bind_query

    recipe = replace(
        base_config.metric_recipes[0],
        id="metric.test.cycle",
        expression=expressions.MetricRecipeRefExpr("metric.test.cycle"),
    )
    config = replace(base_config, metric_recipes=[*base_config.metric_recipes, recipe])
    with pytest.raises(SemanticLayerError) as exc:
        bind_query(config, None, _select({"metric": recipe.id}))
    assert exc.value.code == "INVALID_CONFIG"


def test_authored_measure_columns_keep_their_owner_for_shared_tables(base_config):
    measure = next(row for row in base_config.measures if row.id == MEASURE)
    owner = next(row for row in base_config.entities if row.id == measure.entity)
    other = replace(owner, id="entity.test.other_owner")
    measure = replace(
        measure, expr=expressions.ColumnRefExpr(column="order_total_cents", table=owner.table)
    )
    config = replace(
        base_config,
        entities=[*base_config.entities, other],
        measures=[measure if row.id == measure.id else row for row in base_config.measures],
    )
    dependencies = runtime._query_object_ids(_select({"measure": measure.id}), config)
    assert owner.id in dependencies
    assert other.id not in dependencies


def _implicit_time_case(base_config, kind, placement, override=""):
    measure_id = "measure.jaffle.session_starts" if kind == "conversion" else MEASURE
    measure = next(row for row in base_config.measures if row.id == measure_id)
    role = next(
        row for row in base_config.temporal_roles if row.id == measure.compatible_temporal_roles[0]
    )
    dimension = next(row for row in base_config.dimensions if row.id == role.dimension)
    safe_dimension = replace(dimension, id="dimension.test.safe_time", column="safe_time")
    safe_role = replace(role, id="temporal_role.test.safe_time", dimension=safe_dimension.id)
    measure = replace(measure, compatible_temporal_roles=[role.id, safe_role.id])
    if kind != "conversion":
        measure = replace(measure, default_aggregation=kind, allowed_aggregations=[kind])
    leaf = {"kind": "aggregate", "measure": measure_id}
    query = {}
    if override == "expression":
        leaf["temporal_role"] = safe_role.id
    elif override == "time":
        query["time"] = {"temporal_role": safe_role.id, "grain": "day"}
    expression = leaf
    if kind == "conversion":
        expression = {
            "kind": "conversion",
            "base": leaf,
            "converted": {"kind": "aggregate", "measure": "measure.jaffle.order_count"},
            "entity": "entity.jaffle_customer",
            "window": {"unit": "day", "value": 28},
            "matching_mode": "first_converted_after_base",
        }
    recipe = replace(
        base_config.metric_recipes[0],
        id="metric.test.implicit_time",
        expression=expressions.parse_semantic_expression(expression, context="query"),
        temporal_role="",
        compatible_temporal_roles=[role.id, safe_role.id],
    )
    outer = replace(
        recipe, id="metric.test.outer_time", expression=expressions.MetricRecipeRefExpr(recipe.id)
    )
    if placement == "recipe":
        expression = outer.id
    elif placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": deepcopy(LITERAL)}
    query["select"] = [{"expression": expression, "as": "x"}]
    config = replace(
        base_config,
        measures=[measure if row.id == measure.id else row for row in base_config.measures],
        dimensions=[*base_config.dimensions, safe_dimension],
        temporal_roles=[*base_config.temporal_roles, safe_role],
        metric_recipes=[*base_config.metric_recipes, recipe, outer],
    )
    return config, query, role, safe_role


TEMPORAL_KINDS = ["conversion", "first_value", "last_value"]
PLACEMENTS = ["direct", "nested", "recipe"]


@pytest.mark.parametrize(
    "kind,placement,protected_kind,scope",
    _rotate_scope(itertools.product(TEMPORAL_KINDS, PLACEMENTS, ["role", "dimension"])),
)
def test_implicit_temporal_dependencies_block_before_compile(
    base_config, monkeypatch, kind, placement, protected_kind, scope
):
    config, query, role, _ = _implicit_time_case(base_config, kind, placement)
    protected = role.id if protected_kind == "role" else role.dimension
    config = replace(config, semantic_policies=[_deny(protected, scope)])
    engine = _runtime(config)
    _guard(monkeypatch, engine)
    try:
        _assert_refused(engine, query)
        if placement == "recipe":
            _assert_grant_refuses(config, _internal(config, query), "metric.test.outer_time")
    finally:
        engine.close()


# Each kind meets each placement and each override once (a Latin square).
@pytest.mark.parametrize(
    "kind,placement,override",
    [
        (kind, placement, ["", "expression", "time"][(k + p) % 3])
        for k, kind in enumerate(TEMPORAL_KINDS)
        for p, placement in enumerate(PLACEMENTS)
    ],
)
def test_permitted_effective_temporal_dependencies_compile(base_config, kind, placement, override):
    from semantic_rails.request_context import RequestContext

    config, query, role, safe_role = _implicit_time_case(base_config, kind, placement, override)
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[role.id, role.dimension],
                action="deny",
                roles=["restricted"],
            )
        ],
    )
    roles = ["restricted" if override else "permitted"]
    engine = _runtime(config)
    try:
        sql = engine.compile(query, context={"roles": roles})["rendered_sql"]
        assert sql
        if override:
            assert "safe_time" in sql
            assert role.id not in runtime._query_object_ids(_internal(config, query), config)
        if placement == "recipe":
            access = resource_access.ResourceAccess(
                config,
                RequestContext(
                    roles=tuple(roles),
                    metric_allowlist=("metric.test.outer_time",),
                    dimension_allowlist=(safe_role.id,),
                ),
            )
            access.enforce_query(_internal(config, query))
    finally:
        engine.close()


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "purchase"},
        {"field": "unrelated_value"},
        {"dimension": "unrelated_value"},
        [{"kind": False, "child": {"field": "unrelated_value", "dimension": "unrelated_value"}}],
        {"kind": "aggregate", "measure": MEASURE},
        {"kind": "column", "table": "jaffle_order", "column": "order_id"},
    ],
)
def test_literal_containers_remain_data(base_config, open_engine, value):
    for kind in ("literal", " literal "):
        literal = {"kind": kind, "value": value}
        assert open_engine.compile(
            {"select": [{"expression": REF, "as": "total"}, {"expression": literal, "as": "label"}]}
        )["rendered_sql"]
        # Literal data, including reference-shaped strings, never selects objects.
        assert expressions.collect_object_references(literal, base_config) == []


def test_binding_prepares_without_rendering_and_compile_reuses_it(base_config, monkeypatch):
    from semantic_rails import compiler
    from semantic_rails.compiler_parts import binding as binding_module

    query = _select(REF)
    render = compiler.render_select_for_profile
    monkeypatch.setattr(
        compiler,
        "render_select_for_profile",
        lambda *a, **k: pytest.fail("rendered during binding"),
    )
    binding = compiler.bind_query(base_config, None, query)
    assert MEASURE in binding.object_ids
    monkeypatch.setattr(compiler, "render_select_for_profile", render)
    monkeypatch.setattr(
        binding_module, "plan_query", lambda *a, **k: pytest.fail("replanned authorized binding")
    )
    assert compiler.compile_query(base_config, None, query, binding=binding)["sql"]


def test_literal_reference_strings_do_not_authorize_objects(base_config):
    config = replace(
        base_config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test", kind="object_access", object_ids=[MEASURE], action="deny"
            )
        ],
    )
    engine = _runtime(config)
    query = {
        "select": [
            {"expression": "measure.jaffle.order_count", "as": "count"},
            {
                "expression": {"kind": "literal", "value": {"kind": "future", "field": MEASURE}},
                "as": "label",
            },
        ]
    }
    try:
        assert MEASURE not in runtime._query_object_ids(_internal(config, query), config)
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


def test_parallel_binding_dependency_sets_are_isolated(base_config):
    from concurrent.futures import ThreadPoolExecutor

    from semantic_rails.compiler import bind_query

    measure_ids = [MEASURE, "measure.jaffle.order_count"] * 12

    def bind(measure_id):
        return bind_query(base_config, None, _select({"measure": measure_id})).object_ids

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(bind, measure_ids))
    for measure_id, result in zip(measure_ids, results, strict=True):
        assert set(measure_ids) & result == {measure_id}


def test_unimplemented_schema_anchor_has_fixed_refusal(base_config, monkeypatch):
    from semantic_rails import compiler

    case = next(case for case in CASES if "scoped_aggregate-anchor-0" in case[0])
    monkeypatch.setattr(
        compiler,
        "render_select_for_profile",
        lambda *a, **k: pytest.fail("rendered unsupported anchor"),
    )
    with pytest.raises(SemanticLayerError) as exc:
        compiler.bind_query(base_config, None, _internal(base_config, _schema_query(case)))
    assert exc.value.code == "INVALID_ANCHOR_ROLE"


@pytest.mark.parametrize("branch", ["distribution", "mixed"])
@pytest.mark.parametrize("scope", SCOPES)
def test_distribution_branch_dependencies_precede_rendering(
    base_config, denying, monkeypatch, branch, scope
):
    from semantic_rails import compiler

    filtered = {
        "kind": "scoped_aggregate",
        "measure": MEASURE,
        "where": [{"field": DIMENSION, "op": "is", "value": "new"}],
    }
    distribution = {
        "kind": "distribution",
        "function": "avg",
        "over": {
            "kind": "entity_value",
            "entity": ENTITY,
            "input": filtered if branch == "distribution" else deepcopy(REF),
        },
    }
    query = {"select": [{"expression": distribution, "as": "distributed"}]}
    if branch == "mixed":
        query["select"].append({"expression": filtered, "as": "sibling"})
    assert compiler.compile_query(base_config, None, query)["sql"]
    engine = denying(DIMENSION, scope)
    _guard(monkeypatch, engine)
    assert DIMENSION in compiler.bind_query(base_config, None, query).object_ids
    _assert_refused(engine, query)
