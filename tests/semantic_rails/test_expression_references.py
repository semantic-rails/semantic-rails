"""Reference coverage follows the published Query IR expression definitions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest

from semantic_rails import caveats, expressions, resource_access, runtime
from semantic_rails.contracts import load_contract
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime_parts import responses
from semantic_rails.schema import SemanticPolicyConfig

SCHEMA = load_contract("query_ir.v1.json")
DEFS = SCHEMA["$defs"]
MEASURE = "measure.jaffle.revenue_usd"
METRIC = "metric.sales.aov_usd"
ENTITY = "entity.jaffle_order"
DIMENSION = "dimension.jaffle_customer_type"
ROLE = "temporal_role.jaffle_order_time"
REF = {"kind": "measure", "measure": MEASURE}
LITERAL = {"kind": "literal", "value": 1}
REFERENCE_FIELDS = {
    "measure": MEASURE,
    "metric": METRIC,
    "entity": ENTITY,
    "temporal_role": ROLE,
}


def _example(spec: dict[str, Any], key: str = "") -> Any:
    if spec.get("$ref") == "#/$defs/SelectExpression":
        return deepcopy(LITERAL)
    if "$ref" in spec:
        return _example(DEFS[spec["$ref"].rsplit("/", 1)[1]], key)
    if "oneOf" in spec:
        return _example(spec["oneOf"][0], key)
    if "const" in spec:
        return spec["const"]
    if "enum" in spec:
        return spec["enum"][0]
    if key in REFERENCE_FIELDS:
        return REFERENCE_FIELDS[key]
    kind = spec.get("type")
    if kind == "object":
        return {name: _example(spec["properties"][name], name) for name in spec.get("required", [])}
    if kind == "array":
        return [_example(spec["items"], key)]
    if kind in ("integer", "number"):
        return max(1, spec.get("minimum", 1))
    if kind == "boolean":
        return False
    if kind == "string":
        return {"unit": "day", "function": "median", "name": "abs", "op": ">"}.get(key, "day")
    return 1


def _table_column_expression_seed():
    return {
        "aggregation": "sum",
        "condition": {"kind": "literal", "value": True},
        "value": {"kind": "column", "table": "jaffle_order", "column": "order_id"},
    }


def _cases():
    """Every schema kind/alias, plus every child/ref property independently.

    Opaque scalar parameters are data, not expression-reference positions.
    """
    for name, definition in DEFS.items():
        props = definition.get("properties", {})
        if "kind" not in props and name not in (
            "MeasureRefKindShorthand",
            "MetricRefKindShorthand",
        ):
            continue
        kinds = props.get("kind", {}).get("enum", [props.get("kind", {}).get("const")])
        for kind in kinds:
            example = _example(definition)
            if kind is not None:
                example["kind"] = kind
            # Required arbitrary expression objects need a parseable seed.
            for key in ("condition", "expr", "low", "high"):
                if key in example:
                    example[key] = deepcopy(LITERAL)
            if kind == "call":
                example["args"] = [deepcopy(LITERAL)]
            if name == "PriorPeriodExprShorthand":
                example["offset"] = -1
            if (
                kind
                in {
                    "cumulative",
                    "rolling",
                    "prior_period",
                    "period_to_date",
                    "entity_value",
                    "metric_predicate",
                }
                and name != "PriorPeriodExprShorthand"
            ):
                example["input"] = deepcopy(REF)
            if kind == "aggregate_if":
                example.update(_table_column_expression_seed())
            if kind == "conversion":
                example.update(
                    {
                        "base": {"measure": "measure.jaffle.session_starts"},
                        "converted": {"measure": "measure.jaffle.order_count"},
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
                    probes.append(({"measure": measure_id}, measure_id, True))
                elif kind == "conversion" and key == "entity":
                    probes.append(("entity.jaffle_customer", "entity.jaffle_customer", True))
                elif kind == "entity_value" and key == "where":
                    continue  # value predicates are scalar data, not dimension references
                elif key in REFERENCE_FIELDS:
                    probes.append((REFERENCE_FIELDS[key], REFERENCE_FIELDS[key], True))
                elif spec.get("$ref") == "#/$defs/SelectExpression" or key in (
                    "condition",
                    "expr",
                    "low",
                    "high",
                ):
                    probes.append((deepcopy(REF), MEASURE, True))
                elif key == "over":
                    probes.append(
                        (
                            {"kind": "entity_value", "entity": ENTITY, "input": deepcopy(REF)},
                            MEASURE,
                            True,
                        )
                    )
                elif key == "args":
                    probes.append(([deepcopy(REF)], MEASURE, True))
                elif key == "whens":
                    for slot in ("when", "then"):
                        row = {"when": deepcopy(LITERAL), "then": deepcopy(LITERAL)}
                        row[slot] = deepcopy(REF)
                        probes.append(([row], MEASURE, True))
                elif key in ("partition_by", "constant_properties"):
                    probes.append(([DIMENSION], DIMENSION, True))
                elif key == "where":
                    probes.append(
                        ([{"field": DIMENSION, "op": "=", "value": "new"}], DIMENSION, True)
                    )
                elif key == "predicates":
                    probes.append(
                        (
                            [{"entity": ENTITY, "measure": MEASURE, "op": ">", "value": 0}],
                            MEASURE,
                            True,
                        )
                    )
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
                elif key == "value" and kind == "aggregate_if":
                    probes.append((deepcopy(REF), MEASURE, True))
                elif key == "value" and kind == "literal":
                    probes.append((MEASURE, "", False))
                elif (
                    key != "parameters"
                    and spec.get("type") == "object"
                    and spec.get("additionalProperties") is True
                ):
                    probes.append(({"extension": [{"field": DIMENSION}]}, DIMENSION, False))
                for index, (value, expected, normalize) in enumerate(probes):
                    payload = deepcopy(example)
                    payload[key] = value
                    if key == "anchor":
                        payload["window"] = {"unit": "day", "value": 1}
                    yield f"{name}-{kind}-{key}-{index}", payload, expected, normalize


CASES = list(_cases())
WALKERS = [
    runtime._collect_expr_object_ids,
    responses._collect_expr_object_ids,
    caveats._collect_expr_object_ids,
    resource_access._references,
]


@pytest.mark.parametrize("walker", WALKERS, ids=lambda f: f.__module__)
@pytest.mark.parametrize("case", CASES, ids=lambda c: c[0])
def test_schema_reference_positions(walker, case):
    _, expression, expected, _ = case
    assert expected in walker(expression) if expected else not walker(expression)
    nested = {
        "kind": "case",
        "whens": [{"when": {"kind": "literal", "value": False}, "then": deepcopy(LITERAL)}],
        "else": {"kind": "ratio", "numerator": expression, "denominator": deepcopy(LITERAL)},
    }
    assert expected in walker(nested) if expected else not walker(nested)


@pytest.fixture(scope="module")
def base_config():
    from semantic_rails.config import load_package_config

    return load_package_config("configs/semantic_rails/jaffle_shop")


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
    if kind == "entity_value":
        expression = {"kind": "distribution", "function": "avg", "over": expression}
    if kind == "metric_predicate":
        if nested:
            expression["input"] = {
                "kind": "ratio",
                "numerator": expression["input"],
                "denominator": {"measure": "measure.jaffle.order_count"},
            }
        query["select"] = [{"expression": {"measure": "measure.jaffle.order_count"}, "as": "value"}]
        query["metric_filters"] = [{"expression": expression, "op": "=", "value": True}]
    else:
        if nested and kind not in {"distribution", "entity_value"}:
            expression = {
                "kind": "case",
                "whens": [{"when": {"kind": "literal", "value": False}, "then": deepcopy(LITERAL)}],
                "else": {
                    "kind": "ratio",
                    "numerator": expression,
                    "denominator": deepcopy(LITERAL),
                },
            }
        query["select"] = [{"expression": expression, "as": "value"}]
        if nested and kind in {"distribution", "entity_value"}:
            query["select"].append({"expression": deepcopy(REF), "as": "sibling"})
    return query


@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["audiences", "roles"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("case", [case for case in CASES if case[3]], ids=lambda c: c[0])
def test_runtime_blocks_before_compile(base_config, monkeypatch, action, scope, nested, case):
    from semantic_rails import compiler

    _, _, protected, _ = case
    query = _schema_query(case, nested)
    unprotected = compiler.bind_query(base_config, None, query)
    assert protected in unprotected.object_ids
    assert compiler.compile_query(base_config, None, query, binding=unprotected)["sql"]
    config = replace(
        base_config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[protected],
                action=action,
                **{scope: ["restricted"]},
            )
        ],
    )
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_output(*args, **kwargs):
        pytest.fail("renderer/adapter reached before denial")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    query["policy_context"] = {"audience": "restricted", "roles": ["restricted"]}
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
            assert exc.value.details["policy_effects"][0]["action"] == action
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["future_expression", False, 0, None, {}, []])
def test_unknown_expression_fails_closed(kind):
    with pytest.raises(SemanticLayerError) as exc:
        expressions.collect_object_references({"kind": kind, "child": REF})
    assert exc.value.code == "INVALID_EXPRESSION_AST"


@pytest.mark.parametrize("walker", WALKERS, ids=lambda f: f.__module__)
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
def test_nested_config_and_spec_references(walker, spec):
    assert {MEASURE, ENTITY} & set(walker({"input": spec}))
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
    # Literal has separate permitted-data controls. Every other executable
    # schema tag must have a strict bind/compile/deny probe.
    assert {case[1].get("kind") for case in CASES if case[3]} - {None} == expected - {"literal"}


# Tags carried as data inside expression payloads (inline thresholds and
# value-filter rows); they are never parsed as expressions.
SHAPE_DATA_TAGS = {"percentile", "value_filter"}


def test_shape_vocabulary_follows_the_expression_parser():
    """Query, segment and config expressions share the parser's tag vocabulary.

    Parser aliases that the published schema does not list (such as ``nullif``
    and ``not_in``) must never be refused before normalization, and only data
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
    assert {"nullif", "not_in"} <= parser_kinds - schema_kinds
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
        "value": {"measure": MEASURE},
        "null_value": {"kind": "literal", "value": 0},
    },
    "not_in": {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": {"kind": "not_in", "expr": ORDER_COLUMN, "values": [0, 1]},
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
    if placement == "metric_filter":
        # The selected measure reads neither orders nor revenue, so only the
        # metric filter's alias slot can bind the protected object.
        return {
            "select": [{"expression": {"measure": "measure.jaffle.customer_count"}, "as": "n"}],
            "group_by": [DIMENSION],
            "metric_filters": [{"expression": expression, "op": ">", "value": 0}],
        }
    return {"select": [{"expression": expression, "as": "x"}]}


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
            "value": {"measure": "measure.jaffle.order_count"},
            "null_value": {"kind": "literal", "value": 0},
        }
        expression[slot] = deepcopy(REF) if protected else {"measure": "measure.jaffle.order_count"}
        return expression, MEASURE
    column = deepcopy(ORDER_COLUMN if protected else CUSTOMER_COLUMN)
    condition = {"kind": "not_in", "expr": deepcopy(LITERAL), "values": [column]}
    if slot == "expr":
        condition = {"kind": "not_in", "expr": column, "values": [0, 1]}
    expression = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "condition": condition,
        "value": deepcopy(LITERAL),
    }
    return expression, ENTITY


def _restricting_policy(protected, action, scope):
    return SemanticPolicyConfig(
        id="policy.test",
        kind="object_access",
        object_ids=[protected],
        action=action,
        **{scope: ["restricted"]},
    )


@pytest.mark.parametrize("placement", ["direct", "nested", "metric_filter", "segment"])
@pytest.mark.parametrize("alias", sorted(ALIAS_EXAMPLES))
def test_parser_aliases_validate_and_compile(base_config, alias, placement):
    expression = deepcopy(ALIAS_EXAMPLES[alias])
    config = replace(base_config, semantic_policies=[])
    segment = None
    if placement == "segment":
        segment, config = _alias_segment(config, expression)
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        if segment is not None:
            assert engine.segment_validate(segment.id)["ok"]
            sql = engine.segment_explain(segment.id)["rendered_sql"]
        else:
            query = _alias_query(expression, placement)
            assert engine.validate(query)["ok"]
            sql = engine.compile(query)["rendered_sql"]
        assert ALIAS_SQL[alias] in sql.upper()
    finally:
        engine.close()


@pytest.mark.parametrize("alias,slot", ALIAS_SLOTS)
@pytest.mark.parametrize("placement", ["direct", "nested", "metric_filter"])
@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
def test_parser_alias_dependencies_block_before_rendering(
    base_config, monkeypatch, alias, slot, placement, action, scope
):
    from semantic_rails import compiler

    expression, protected = _alias_dependency(alias, slot)
    query = _alias_query(expression, placement)
    control = _alias_query(_alias_dependency(alias, slot, protected=False)[0], placement)
    assert protected not in compiler.bind_query(base_config, None, control).object_ids
    unprotected = compiler.bind_query(base_config, None, query)
    assert protected in unprotected.object_ids
    assert compiler.compile_query(base_config, None, query, binding=unprotected)["sql"]
    config = replace(base_config, semantic_policies=[_restricting_policy(protected, action, scope)])
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_output(*args, **kwargs):
        pytest.fail("renderer/adapter reached before denial")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    query["policy_context"] = {"audience": "restricted", "roles": ["restricted"]}
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
            assert exc.value.details["policy_effects"][0]["action"] == action
    finally:
        engine.close()


@pytest.mark.parametrize("alias,slot", ALIAS_SLOTS)
@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
def test_parser_alias_segment_dependencies_block_before_rendering(
    base_config, monkeypatch, alias, slot, action, scope
):
    from semantic_rails import compiler

    expression, protected = _alias_dependency(alias, slot)
    segment, config = _alias_segment(base_config, expression)
    config = replace(config, semantic_policies=[_restricting_policy(protected, action, scope)])
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_output(*args, **kwargs):
        pytest.fail("renderer/adapter reached before denial")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(runtime, "compile_query", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    context = {"audience": "restricted", "roles": ["restricted"]}
    try:
        result = engine.segment_validate(segment.id, policy_context=context)
        assert result["errors"][0]["code"] == "POLICY_DENIED"
        for call in (engine.segment_explain, engine.segment_preview):
            with pytest.raises(SemanticLayerError) as exc:
                call(segment.id, policy_context=context)
            assert exc.value.code == "POLICY_DENIED"
    finally:
        engine.close()


def test_policy_context_metadata_is_not_an_expression_position(base_config):
    note = {"kind": "annotation", "detail": {"kind": "future_expression"}}
    count = {"measure": "measure.jaffle.order_count"}
    permitted = {"select": [{"expression": count, "as": "n"}]}
    refused = [
        {"select": [{"expression": {**note, "measure": MEASURE}, "as": "x"}]},
        {
            "select": [{"expression": count, "as": "n"}],
            "metric_filters": [
                {
                    "expression": {"kind": "ratio", "numerator": note, "denominator": LITERAL},
                    "op": ">",
                    "value": 0,
                }
            ],
        },
        {
            "select": [
                {
                    "expression": {
                        "kind": "scoped_aggregate",
                        "measure": MEASURE,
                        "predicates": [{"input": note, "entity": ENTITY, "op": ">", "value": 0}],
                    },
                    "as": "x",
                }
            ]
        },
    ]
    engine = runtime.Runtime.from_config(
        replace(base_config, semantic_policies=[]), source_path="configs/semantic_rails/jaffle_shop"
    )
    try:
        for query in (permitted, *refused):
            query["policy_context"] = {"audience": "finance", "note": deepcopy(note)}
        assert engine.validate(permitted)["ok"]
        assert engine.compile(permitted)["rendered_sql"]
        for query in refused:
            assert engine.validate(query)["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
            with pytest.raises(SemanticLayerError) as exc:
                engine.compile(query)
            assert exc.value.code == "INVALID_EXPRESSION_AST"
    finally:
        engine.close()


def _unknown_kind_request(placement):
    count = {"measure": "measure.jaffle.order_count"}
    unknown = {"kind": "future_expression"}
    query: dict[str, Any] = {"select": [{"expression": count, "as": "n"}]}
    if placement == "unrecognized_key":
        query["future_key"] = unknown
    elif placement == "note_key":
        query["_note"] = unknown
    elif placement == "limits":
        query["limits"] = {"note": unknown}
    elif placement == "entity_value_where":
        entity_value = {
            "kind": "entity_value",
            "entity": "entity.jaffle_customer",
            "input": count,
            "where": [{"kind": "future_filter", "op": ">", "value": 3}],
        }
        query["select"] = [
            {"expression": {"kind": "distribution", "function": "avg", "over": entity_value}}
        ]
    elif placement == "select_item_policy_context":
        query["select"][0]["policy_context"] = unknown
    elif placement == "expression_policy_context":
        query["select"][0]["expression"] = {**count, "policy_context": unknown}
    return query


def _assert_shape_refused_before_output(base_config, monkeypatch, query):
    from semantic_rails import compiler

    engine = runtime.Runtime.from_config(
        replace(base_config, semantic_policies=[]), source_path="configs/semantic_rails/jaffle_shop"
    )

    def no_output(*args, **kwargs):
        pytest.fail("request shape reached rendering or the adapter")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    monkeypatch.setattr(engine, "_compile", no_output)
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "INVALID_EXPRESSION_AST"
    finally:
        engine.close()


@pytest.mark.parametrize(
    "placement", ["unrecognized_key", "note_key", "limits", "entity_value_where"]
)
def test_request_shapes_are_checked_before_rendering(base_config, monkeypatch, placement):
    """Binding shape-checks every request key, including ones the IR ignores."""
    _assert_shape_refused_before_output(base_config, monkeypatch, _unknown_kind_request(placement))


@pytest.mark.parametrize("placement", ["select_item_policy_context", "expression_policy_context"])
def test_policy_context_is_skipped_only_at_the_top_level(base_config, monkeypatch, placement):
    """A ``policy_context`` key below the request's top level is still shape-checked."""
    _assert_shape_refused_before_output(base_config, monkeypatch, _unknown_kind_request(placement))


@pytest.mark.parametrize("action", ["deny", "redact"])
def test_sibling_paths_and_recipe_closure(base_config, monkeypatch, action):
    from semantic_rails.metadata_parts.valid_values import valid_values_payload

    expression = {"kind": "ratio", "numerator": deepcopy(REF), "denominator": LITERAL}
    nested_filter = {"expression": expression, "op": ">", "value": 0}
    segment = replace(base_config.segments[0], metric_filters=[nested_filter])
    policy = SemanticPolicyConfig(
        id="policy.test",
        kind="object_access",
        object_ids=[MEASURE],
        audiences=["restricted"],
        action=action,
    )
    recipe = replace(
        base_config.metric_recipes[0],
        expression=expressions.parse_semantic_expression(expression, context="query"),
    )
    config = replace(
        base_config,
        semantic_policies=[policy],
        segments=[segment],
        metric_recipes=[recipe, *base_config.metric_recipes[1:]],
    )
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    context = {"audience": "restricted"}

    def no_compile(*args, **kwargs):
        pytest.fail("compiler/warehouse reached before denial")

    monkeypatch.setattr(engine, "_compile", no_compile)
    monkeypatch.setattr(runtime, "compile_query", no_compile)
    monkeypatch.setattr(engine, "_get_adapter", no_compile)
    try:
        assert (
            engine.segment_validate(segment.id, policy_context=context)["errors"][0]["code"]
            == "POLICY_DENIED"
        )
        for call in (
            lambda: engine.segment_explain(segment.id, policy_context=context),
            # This also guards the independent membership/count SQL path.
            lambda: engine.segment_preview(segment.id, policy_context=context),
            lambda: engine.compile(
                {
                    "select": [{"expression": {"metric": recipe.id}, "as": "x"}],
                    "policy_context": context,
                }
            ),
            lambda: valid_values_payload(
                engine,
                dimension_id=DIMENSION,
                allow_live_query=True,
                include_counts=True,
                query={"metric_filters": [nested_filter], "policy_context": context},
            ),
        ):
            with pytest.raises(SemanticLayerError) as exc:
                call()
            assert exc.value.code == "POLICY_DENIED"
        from semantic_rails.request_context import RequestContext

        access = resource_access.ResourceAccess(
            config,
            RequestContext(
                audience="restricted",
                metric_allowlist=(recipe.id,),
                dimension_allowlist=(),
            ),
        )
        for expr in (expression, {"metric": recipe.id}):
            with pytest.raises(SemanticLayerError) as exc:
                access.enforce_query({"select": [{"expression": expr, "as": "x"}]})
            assert exc.value.code == "RESOURCE_ACCESS_DENIED"
    finally:
        engine.close()


def test_allowed_nested_expression_executes(runtime_factory):
    engine = runtime_factory("jaffle_shop")
    expression = {
        "kind": "case",
        "whens": [{"when": {"kind": "literal", "value": False}, "then": deepcopy(LITERAL)}],
        "else": {
            "kind": "ratio",
            "numerator": deepcopy(REF),
            "denominator": LITERAL,
        },
    }
    query = {"select": [{"expression": expression, "as": "x"}]}
    try:
        assert engine.validate(query)["ok"]
        assert engine.compile(query)["rendered_sql"]
        assert engine.query(query)["rows"]
    finally:
        engine.close()


SCALAR_FIELDS = {"number": 123, "boolean": False, "null": None}
SPELLINGS = ["canonical", "padded", "name", "label", "alias", "padded_alias", *SCALAR_FIELDS]
PREDICATES = ["measure", "metric", "input"]


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
        expression["where"] = [{"field": field, "op": "=", "value": "new"}]
        protected = DIMENSION
    else:
        reference = {
            "measure": {"measure": f" {MEASURE} "},
            "metric": {"metric": f" {METRIC} "},
            "input": {"input": {"measure": f" {MEASURE} "}},
        }[spelling]
        expression["predicates"] = [{"entity": ENTITY, **reference, "op": ">", "value": 0}]
        protected = MEASURE
    return config, expression, protected


@pytest.mark.parametrize(
    "spelling,placement",
    [
        (spelling, placement)
        for spelling in [*SPELLINGS, *PREDICATES]
        for placement in ["direct", "nested", "recipe"]
    ]
    + [
        (spelling, placement)
        for spelling in SPELLINGS
        for placement in ["conversion", "conversion_recipe"]
    ],
)
@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
def test_reference_spellings_denied_before_compile(
    base_config, monkeypatch, spelling, placement, action, scope
):
    from semantic_rails.request_context import RequestContext

    config, expression, protected = _spelling_case(base_config, spelling)
    if placement.startswith("conversion"):
        expression = _conversion_filter(expression)
    recipe = replace(
        config.metric_recipes[0],
        id="metric.test.spelling",
        expression=expressions.parse_semantic_expression(expression, context="config"),
    )
    config = replace(
        config,
        metric_recipes=[*config.metric_recipes, recipe],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[protected],
                action=action,
                **{scope: ["restricted"]},
            )
        ],
    )
    if placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
    elif placement.endswith("recipe"):
        expression = {"metric": recipe.id}
    context = {"audience": "restricted", "roles": ["restricted"]}
    query = {"select": [{"expression": expression, "as": "x"}], "policy_context": context}
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_compile(*args, **kwargs):
        pytest.fail("compiler/warehouse reached before denial")

    monkeypatch.setattr(engine, "_compile", no_compile)
    monkeypatch.setattr(engine, "_get_adapter", no_compile)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
        if placement.endswith("recipe"):
            access = resource_access.ResourceAccess(
                config,
                RequestContext(
                    audience="restricted",
                    roles=("restricted",),
                    metric_allowlist=(recipe.id,),
                    dimension_allowlist=(),
                ),
            )
            with pytest.raises(SemanticLayerError) as exc:
                access.enforce_query(query)
            assert exc.value.code == "RESOURCE_ACCESS_DENIED"
    finally:
        engine.close()


@pytest.mark.parametrize("spelling", [*SPELLINGS, *PREDICATES])
def test_permitted_reference_spellings_compile(base_config, spelling):
    config, expression, _ = _spelling_case(base_config, spelling)
    engine = runtime.Runtime.from_config(
        replace(config, semantic_policies=[]), source_path="configs/semantic_rails/jaffle_shop"
    )
    try:
        assert engine.compile({"select": [{"expression": expression, "as": "x"}]})["rendered_sql"]
    finally:
        engine.close()


@pytest.mark.parametrize("placement", ["direct", "nested", "predicate"])
def test_unknown_kind_with_measure_fails_before_normalization(base_config, monkeypatch, placement):
    expression = {"kind": "future_expression", "measure": MEASURE, "child": REF}
    if placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
    elif placement == "predicate":
        expression = {
            "kind": "scoped_aggregate",
            "measure": MEASURE,
            "predicates": [{"input": expression, "entity": ENTITY, "op": ">", "value": 0}],
        }
    engine = runtime.Runtime.from_config(
        base_config, source_path="configs/semantic_rails/jaffle_shop"
    )
    monkeypatch.setattr(engine, "_compile", lambda *a, **k: pytest.fail("unknown kind compiled"))
    query = {"select": [{"expression": expression, "as": "x"}]}
    try:
        assert engine.validate(query)["errors"][0]["code"] == "INVALID_EXPRESSION_AST"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "INVALID_EXPRESSION_AST"
    finally:
        engine.close()


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("action", ["deny", "redact"])
def test_recipe_filter_spec_spellings(base_config, spelling, action):
    from semantic_rails.request_context import RequestContext

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
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[protected],
                audiences=["restricted"],
                action=action,
            )
        ],
    )
    query = {"select": [{"expression": {"metric": recipe.id}, "as": "x"}]}
    assert protected in runtime._query_object_ids(query, config)
    access = resource_access.ResourceAccess(
        config, RequestContext(audience="restricted", metric_allowlist=(recipe.id,))
    )
    with pytest.raises(SemanticLayerError) as exc:
        access.enforce_query(query)
    assert exc.value.code == "RESOURCE_ACCESS_DENIED"


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
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    monkeypatch.setattr(engine, "_compile", lambda *a, **k: pytest.fail("ambiguous alias compiled"))
    try:
        result = engine.validate({"select": [{"expression": expression, "as": "x"}]})
        assert result["errors"][0]["code"] == "AMBIGUOUS_ALIAS"
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


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_permitted_conversion_dimension_spellings_compile(base_config, spelling):
    config, expression, _ = _spelling_case(base_config, spelling)
    engine = runtime.Runtime.from_config(
        replace(config, semantic_policies=[]), source_path="configs/semantic_rails/jaffle_shop"
    )
    try:
        assert engine.compile(
            {"select": [{"expression": _conversion_filter(expression), "as": "x"}]}
        )["rendered_sql"]
    finally:
        engine.close()


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


@pytest.mark.parametrize("slot", ["condition", "value"])
@pytest.mark.parametrize("placement", ["direct", "nested", "recipe"])
@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
def test_table_column_denied_before_compile(
    base_config, monkeypatch, slot, placement, action, scope
):
    from semantic_rails.request_context import RequestContext

    expression = _table_column_expression(slot)
    recipe = replace(
        base_config.metric_recipes[0],
        id="metric.test.column",
        expression=expressions.parse_semantic_expression(expression, context="config"),
    )
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
    recipe = replace(recipe, expression=expressions.MeasureRefExpr(authored.id))
    config = replace(
        base_config,
        measures=[*base_config.measures, authored],
        metric_recipes=[*base_config.metric_recipes, recipe],
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[ENTITY],
                action=action,
                **{scope: ["restricted"]},
            )
        ],
    )
    if placement == "nested":
        expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
    elif placement == "recipe":
        expression = {"metric": recipe.id}
    query = {
        "select": [{"expression": expression, "as": "x"}],
        "policy_context": {"roles": ["restricted"], "audience": "restricted"},
    }
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    monkeypatch.setattr(engine, "_compile", lambda *a, **k: pytest.fail("table column compiled"))
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
        if placement == "recipe":
            access = resource_access.ResourceAccess(
                config,
                RequestContext(
                    roles=("restricted",), audience="restricted", metric_allowlist=(recipe.id,)
                ),
            )
            with pytest.raises(SemanticLayerError) as exc:
                access.enforce_query(query)
            assert exc.value.code == "RESOURCE_ACCESS_DENIED"
    finally:
        engine.close()


@pytest.mark.parametrize("slot", ["condition", "value"])
def test_permitted_table_column_compiles(base_config, slot):
    engine = runtime.Runtime.from_config(
        base_config, source_path="configs/semantic_rails/jaffle_shop"
    )
    try:
        assert engine.compile(
            {"select": [{"expression": _table_column_expression(slot), "as": "x"}]}
        )["rendered_sql"]
    finally:
        engine.close()


DEPENDENCY_FORMS = ["measure", "metric", "nested_metric", "group", "where", "time"]


def _dependency_case(base_config, form):
    measure = next(row for row in base_config.measures if row.id == MEASURE)
    dimension = next(row for row in base_config.dimensions if row.id == DIMENSION)
    role = next(
        row for row in base_config.temporal_roles if row.id == "temporal_role.jaffle_order_time"
    )
    role_dimension = next(row for row in base_config.dimensions if row.id == role.dimension)
    recipe = replace(
        base_config.metric_recipes[0],
        id="metric.test.dependency",
        expression=expressions.MeasureRefExpr(MEASURE),
    )
    config = replace(base_config, metric_recipes=[*base_config.metric_recipes, recipe])
    query = {"select": [{"expression": deepcopy(LITERAL), "as": "x"}]}
    protected = measure.entity
    if form in {"measure", "metric", "nested_metric"}:
        expression = deepcopy(REF) if form == "measure" else {"metric": recipe.id}
        if form == "nested_metric":
            expression = {"kind": "ratio", "numerator": expression, "denominator": LITERAL}
        query["select"][0]["expression"] = expression
    elif form == "group":
        query["group_by"] = [dimension.id]
        protected = dimension.entity
    elif form == "where":
        query["where"] = [{"field": dimension.id, "op": "=", "value": "new"}]
        protected = dimension.entity
    else:
        query["time"] = {"temporal_role": role.id, "grain": "day"}
        protected = role_dimension.entity
    return config, query, protected


@pytest.mark.parametrize("form", DEPENDENCY_FORMS)
@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
def test_authored_dependencies_denied_before_compile(base_config, monkeypatch, form, action, scope):
    from semantic_rails.request_context import RequestContext

    config, query, protected = _dependency_case(base_config, form)
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[protected],
                action=action,
                **{scope: ["restricted"]},
            )
        ],
    )
    query["policy_context"] = {"roles": ["restricted"], "audience": "restricted"}
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_compile(*args, **kwargs):
        pytest.fail("authored dependency reached compiler/adapter")

    monkeypatch.setattr(engine, "_compile", no_compile)
    monkeypatch.setattr(engine, "_get_adapter", no_compile)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
        if form == "metric":
            access = resource_access.ResourceAccess(
                config,
                RequestContext(
                    roles=("restricted",),
                    audience="restricted",
                    metric_allowlist=("metric.test.dependency",),
                ),
            )
            with pytest.raises(SemanticLayerError) as exc:
                access.enforce_query(query)
            assert exc.value.code == "RESOURCE_ACCESS_DENIED"
    finally:
        engine.close()


@pytest.mark.parametrize("form", DEPENDENCY_FORMS)
def test_permitted_authored_dependencies_compile(base_config, form):
    config, query, _ = _dependency_case(base_config, form)
    engine = runtime.Runtime.from_config(
        replace(config, semantic_policies=[]), source_path="configs/semantic_rails/jaffle_shop"
    )
    try:
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


def test_authored_dependency_cycles_fail_closed(base_config):
    from semantic_rails.compiler import bind_query

    recipe = replace(
        base_config.metric_recipes[0],
        id="metric.test.cycle",
        expression=expressions.MetricRecipeRefExpr("metric.test.cycle"),
    )
    config = replace(base_config, metric_recipes=[*base_config.metric_recipes, recipe])
    with pytest.raises(SemanticLayerError) as exc:
        bind_query(config, None, {"select": [{"expression": {"metric": recipe.id}, "as": "x"}]})
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
    dependencies = runtime._query_object_ids(
        {"select": [{"expression": {"measure": measure.id}, "as": "x"}]}, config
    )
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
    elif override == "query":
        query["temporal_role_overrides"] = {measure_id: safe_role.id}
    elif override == "time":
        query["time"] = {"temporal_role": safe_role.id}
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
        expression = {"metric": outer.id}
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


@pytest.mark.parametrize("kind", ["conversion", "first_value", "last_value"])
@pytest.mark.parametrize("placement", ["direct", "nested", "recipe"])
@pytest.mark.parametrize("protected_kind", ["role", "dimension"])
@pytest.mark.parametrize("action", ["deny", "redact"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
def test_implicit_temporal_dependencies_block_before_compile(
    base_config, monkeypatch, kind, placement, protected_kind, action, scope
):
    from semantic_rails import compiler
    from semantic_rails.request_context import RequestContext

    config, query, role, _ = _implicit_time_case(base_config, kind, placement)
    protected = role.id if protected_kind == "role" else role.dimension
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[protected],
                action=action,
                **{scope: ["restricted"]},
            )
        ],
    )
    query["policy_context"] = {"roles": ["restricted"], "audience": "restricted"}
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")

    def no_compile(*args, **kwargs):
        pytest.fail("implicit temporal dependency reached compiler/adapter")

    monkeypatch.setattr(engine, "_compile", no_compile)
    monkeypatch.setattr(engine, "_get_adapter", no_compile)
    monkeypatch.setattr(compiler, "render_select_for_profile", no_compile)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
        if placement == "recipe":
            access = resource_access.ResourceAccess(
                config,
                RequestContext(
                    roles=("restricted",),
                    audience="restricted",
                    metric_allowlist=("metric.test.outer_time",),
                ),
            )
            with pytest.raises(SemanticLayerError) as exc:
                access.enforce_query(query)
            assert exc.value.code == "RESOURCE_ACCESS_DENIED"
    finally:
        engine.close()


@pytest.mark.parametrize("kind", ["conversion", "first_value", "last_value"])
@pytest.mark.parametrize("placement", ["direct", "nested", "recipe"])
@pytest.mark.parametrize("override", ["", "expression", "query", "time"])
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
    query["policy_context"] = {"roles": ["restricted" if override else "permitted"]}
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    try:
        sql = engine.compile(query)["rendered_sql"]
        assert sql
        if override:
            assert "safe_time" in sql
            assert role.id not in runtime._query_object_ids(query, config)
        if placement == "recipe" and override != "query":
            access = resource_access.ResourceAccess(
                config,
                RequestContext(
                    roles=tuple(query["policy_context"]["roles"]),
                    metric_allowlist=("metric.test.outer_time",),
                    dimension_allowlist=(safe_role.id,),
                ),
            )
            access.enforce_query(query)
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
@pytest.mark.parametrize("literal_kind", ["literal", " literal "])
def test_literal_containers_remain_data(base_config, value, literal_kind):
    literal = {"kind": literal_kind, "value": value}
    engine = runtime.Runtime.from_config(
        replace(base_config, semantic_policies=[]), source_path="configs/semantic_rails/jaffle_shop"
    )
    try:
        assert engine.compile(
            {"select": [{"expression": REF, "as": "total"}, {"expression": literal, "as": "label"}]}
        )["rendered_sql"]
        # Literal data, including reference-shaped strings, never selects objects.
        refs = expressions.collect_object_references(literal, base_config)
        assert refs == []
    finally:
        engine.close()


def test_binding_prepares_without_rendering_and_compile_reuses_it(base_config, monkeypatch):
    from semantic_rails import compiler

    query = {"select": [{"expression": REF, "as": "x"}]}
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
        compiler, "plan_query", lambda *a, **k: pytest.fail("replanned authorized binding")
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
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    query = {
        "select": [
            {"expression": {"measure": "measure.jaffle.order_count"}, "as": "count"},
            {
                "expression": {"kind": "literal", "value": {"kind": "future", "field": MEASURE}},
                "as": "label",
            },
        ]
    }
    try:
        assert MEASURE not in runtime._query_object_ids(query, config)
        assert engine.compile(query)["rendered_sql"]
    finally:
        engine.close()


def test_parallel_binding_dependency_sets_are_isolated(base_config):
    from concurrent.futures import ThreadPoolExecutor

    from semantic_rails.compiler import bind_query

    measure_ids = [MEASURE, "measure.jaffle.order_count"] * 12

    def bind(measure_id):
        return bind_query(
            base_config, None, {"select": [{"expression": {"measure": measure_id}, "as": "x"}]}
        ).object_ids

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
        compiler.bind_query(base_config, None, _schema_query(case))
    assert exc.value.code == "INVALID_ANCHOR_ROLE"


@pytest.mark.parametrize("branch", ["distribution", "mixed"])
@pytest.mark.parametrize("scope", ["roles", "audiences"])
@pytest.mark.parametrize("action", ["deny", "redact"])
def test_distribution_branch_dependencies_precede_rendering(
    base_config, monkeypatch, branch, scope, action
):
    from semantic_rails import compiler

    filtered = {
        "kind": "scoped_aggregate",
        "measure": MEASURE,
        "where": [{"field": DIMENSION, "op": "=", "value": "new"}],
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

    def no_output(*args, **kwargs):
        pytest.fail("rendering/adapter access before branch authorization")

    monkeypatch.setattr(compiler, "render_select_for_profile", no_output)
    assert DIMENSION in compiler.bind_query(base_config, None, query).object_ids
    config = replace(
        base_config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.test",
                kind="object_access",
                object_ids=[DIMENSION],
                action=action,
                **{scope: ["restricted"]},
            )
        ],
    )
    query["policy_context"] = {"roles": ["restricted"], "audience": "restricted"}
    engine = runtime.Runtime.from_config(config, source_path="configs/semantic_rails/jaffle_shop")
    monkeypatch.setattr(engine, "_get_adapter", no_output)
    try:
        assert engine.validate(query)["errors"][0]["code"] == "POLICY_DENIED"
        for operation in (engine.compile, engine.query):
            with pytest.raises(SemanticLayerError) as exc:
                operation(query)
            assert exc.value.code == "POLICY_DENIED"
    finally:
        engine.close()
