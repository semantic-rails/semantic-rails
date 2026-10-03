"""observation_scope: where a sum or count is judged to have data, so an empty group reads 0.

``dataset`` (the default) judges it in the measure's own rows under its authored conditions
and the caller's row filters; ``query`` inside the query's filters too. Gold values come from
plain SQL over the seed, written without the engine.

Seed: store s5 sold pears and no apples; kale sold only in the west region, which the
regional caller can't see; February has no sale; one west apple sale has an unknown quantity.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_config_expression
from semantic_rails.registry import Registry
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime

SEED = """
CREATE TABLE sales (sale_id INTEGER, store_id VARCHAR, region VARCHAR, product VARCHAR,
                    qty INTEGER, sold_at TIMESTAMP);
INSERT INTO sales VALUES
  (1, 's1', 'east', 'apple', 3, TIMESTAMP '2025-01-05 10:00:00'),
  (2, 's2', 'east', 'apple', 2, TIMESTAMP '2025-01-20 10:00:00'),
  (3, 's5', 'east', 'pear', 4, TIMESTAMP '2025-03-02 10:00:00'),
  (4, 's4', 'west', 'kale', 6, TIMESTAMP '2025-01-11 10:00:00'),
  (5, 's4', 'west', 'apple', NULL, TIMESTAMP '2025-03-15 10:00:00');
CREATE TABLE inventory (store_id VARCHAR, as_of DATE, on_hand INTEGER);
INSERT INTO inventory VALUES
  ('s1', DATE '2025-01-31', 10), ('s2', DATE '2025-01-31', 5), ('s1', DATE '2025-03-31', 8);
"""

STORE = "dimension.obs_sale_store_id"
PRODUCT = "dimension.obs_sale_product"
SNAPSHOT_STORE = "dimension.obs_snapshot_store_id"
SOLD = "temporal_role.obs_sale_sold_at"
AS_OF = "temporal_role.obs_snapshot_as_of"
QTY = {"measure": "measure.obs.qty"}
SALES = {"measure": "measure.obs.sale_count"}
REGIONAL = RequestContext(actor="end-user", audience="regional", attributes={"region": "east"})


def _package(root: Path, defaults: dict[str, Any]) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "obs", "namespace": "obs", "name": "obs", "description": "Observation",
                    "warehouse": "duckdb", "default_db": "obs.duckdb", "seed": {"kind": "external"}},
        "defaults": {"time": {"timezone": "UTC"}, **defaults},
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "sale": {"key": ["sale_id"], "model": "sales"},
        "snapshot": {"key": ["store_id", "as_of"], "model": "inventory"},
    }}})  # fmt: skip
    put("models/sales.yml", {"model": {
        "id": "sales", "relation": "sales", "entities": {"sale": {}},
        "times": {"sold_at": {"column": "sold_at", "kind": "timestamp", "class": "event_time",
                              "default": True}},
        "dimensions": {name: {"kind": "categorical"} for name in ("store_id", "region", "product")},
        "measures": {
            "qty": {"kind": "aggregate", "expr": "qty", "value_type": "count"},
            "sale_count": {"kind": "entity_count", "entity_key": "sale_id"},
        },
    }})  # fmt: skip
    put("models/inventory.yml", {"model": {
        "id": "inventory", "relation": "inventory", "entities": {"snapshot": {}},
        "times": {"as_of": {"column": "as_of", "kind": "date", "class": "as_of_time",
                            "default": True}},
        "dimensions": {"store_id": {"kind": "categorical"}},
        "measures": {"on_hand": {"kind": "aggregate", "expr": "on_hand", "value_type": "count",
                                 "accumulation": {"kind": "stock", "snapshot": "end_of_period"}}},
    }})  # fmt: skip
    put("policies.yml", {"semantic_policies": [{
        "id": "policy.obs.own_region", "kind": "row_filter", "dimension": "dimension.obs_sale_region",
        "attribute": "region", "audiences": ["regional"],
    }]})  # fmt: skip
    with duckdb.connect(str(root / "obs.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module")
def package(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _package(tmp_path_factory.mktemp("observation") / "obs", {})


@pytest.fixture(scope="module")
def runtime(package: Path):
    engine = Runtime.from_path(str(package))
    try:
        yield engine
    finally:
        engine.close()


def _gold(sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return connection.execute(sql).fetchall()


def _ask(runtime: Runtime, scope: str | None, **query: Any) -> dict[str, Any]:
    payload = {"version": 1, **query, **({"observation_scope": scope} if scope else {})}
    return runtime.query(payload)


def _codes(response: dict[str, Any]) -> list[str]:
    return [item["code"] for item in response["warnings"]]


def _where(field: str, value: Any, op: str = "=") -> list[dict[str, Any]]:
    return [{"field": field, "op": op, "value": value}]


APPLES_AT_S5 = [*_where(PRODUCT, "apple"), *_where(STORE, "s5")]
# A group with no row reads 0 where the measure has a value anywhere (dataset), else NULL.
DATASET_QTY = """
    SELECT CASE WHEN COUNT(*) > 0 THEN SUM(qty)
                WHEN EXISTS (SELECT 1 FROM sales WHERE qty IS NOT NULL) THEN 0 END
    FROM sales WHERE {where}
"""


@pytest.mark.parametrize("scope", [None, "dataset", "query"])
def test_apples_at_a_store_that_sold_none(runtime: Runtime, scope: str | None) -> None:
    """Store s5 sold no apples: 0 across the dataset (as its store breakdown says), NULL with a
    warning when judged inside the query's filters, as plain SQL's SUM is."""
    where = "product = 'apple' AND store_id = 's5'"
    sql = f"SELECT SUM(qty), NULLIF(COUNT(*), 0) FROM sales WHERE {where}"
    if scope != "query":
        sql = f"SELECT ({DATASET_QTY.format(where=where)}), COUNT(*) FROM sales WHERE {where}"
    response = _ask(runtime, scope, select=_select(qty=QTY, sales=SALES), where=APPLES_AT_S5)
    assert [tuple(row.values()) for row in response["rows"]] == _gold(sql)
    assert _gold(sql) == ([(None, None)] if scope == "query" else [(0, 0)])
    assert ("NO_DATA_IN_SCOPE" in _codes(response)) is (scope == "query")
    assert "FILTER_VALUE_NOT_FOUND" not in _codes(response)


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_a_misspelled_filter_value(runtime: Runtime, scope: str) -> None:
    """'appel' matches no row: a confident 0 across the dataset comes with the value named."""
    where = "product = 'appel'"
    sql = f"SELECT SUM(qty) FROM sales WHERE {where}"
    if scope == "dataset":
        sql = DATASET_QTY.format(where=where)
    response = _ask(runtime, scope, select=_select(qty=QTY), where=_where(PRODUCT, "appel"))
    assert [tuple(row.values()) for row in response["rows"]] == _gold(sql)
    typos = [item for item in response["warnings"] if item["code"] == "FILTER_VALUE_NOT_FOUND"]
    if scope == "query":
        assert typos == [] and "NO_DATA_IN_SCOPE" in _codes(response)
        return
    assert "NO_DATA_IN_SCOPE" not in _codes(response)
    (typo,) = typos
    assert typo["details"]["filters"] == [
        {"dimension": PRODUCT, "value": "appel", "suggestion": "apple"}
    ]
    assert "'appel'" in typo["message"] and "did you mean 'apple'?" in typo["message"]


def test_one_warning_names_every_value_that_matched_nothing(runtime: Runtime) -> None:
    where = [*_where(PRODUCT, ["apple", "peer"], "IN"), *_where(STORE, "s9")]
    response = _ask(runtime, None, select=_select(qty=QTY), where=where)
    (typo,) = [item for item in response["warnings"] if item["code"] == "FILTER_VALUE_NOT_FOUND"]
    assert [(row["dimension"], row["value"]) for row in typo["details"]["filters"]] == [
        (PRODUCT, "peer"),
        (STORE, "s9"),
    ]


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_an_authored_condition_that_never_matched_reads_null(runtime: Runtime, scope: str) -> None:
    """No durian was ever sold: the filtered measure has no data anywhere, in either scope."""
    durian = {**QTY, "kind": "aggregate", "filter": {"all": _where(PRODUCT, "durian")}}
    (expected,) = _gold("SELECT SUM(qty) FROM sales WHERE product = 'durian'")
    assert expected == (None,)
    response = _ask(runtime, scope, select=_select(durian=durian), where=_where(STORE, "s1"))
    assert [tuple(row.values()) for row in response["rows"]] == [expected]
    assert "NO_DATA_IN_SCOPE" in _codes(response)


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_a_window_with_no_rows_reads_zero_while_other_dates_have_data(
    runtime: Runtime, scope: str
) -> None:
    """February has no sale, but quantities exist in other months: untimed, as before."""
    time = {"temporal_role": SOLD, "grain": "month", "fill": True}
    time.update(start="2025-02-01", end="2025-03-01")
    (expected,) = _gold(
        "SELECT CASE WHEN COUNT(*) > 0 THEN SUM(qty) WHEN EXISTS (SELECT 1 FROM sales "
        "WHERE qty IS NOT NULL) THEN 0 END FROM sales "
        "WHERE sold_at >= TIMESTAMP '2025-02-01' AND sold_at < TIMESTAMP '2025-03-01'"
    )
    response = _ask(runtime, scope, select=_select(qty=QTY), time=time)
    assert [row["qty"] for row in response["rows"]] == [expected[0]] == [0]


def test_a_regional_caller_never_observes_a_hidden_region(runtime: Runtime) -> None:
    """Kale sold only in the west: for an east caller it has no data anywhere, so it reads
    NULL, never 0, and the value probe neither finds kale nor suggests it."""
    kale = {**QTY, "kind": "aggregate", "filter": {"all": _where(PRODUCT, "kale")}}
    context = {"policy_context": REGIONAL.to_policy_context()}
    response = _ask(runtime, None, select=_select(kale=kale), where=_where(STORE, "s1"), **context)
    assert response["rows"] == [{"kale": None}]
    assert "NO_DATA_IN_SCOPE" in _codes(response)
    everyone = _ask(runtime, None, select=_select(kale=kale), where=_where(STORE, "s1"))
    assert everyone["rows"] == [{"kale": 0}]
    for value in ("kale", "kalee"):
        filtered = _ask(
            runtime, None, select=_select(qty=QTY), where=_where(PRODUCT, value), **context
        )
        (typo,) = [w for w in filtered["warnings"] if w["code"] == "FILTER_VALUE_NOT_FOUND"]
        (miss,) = typo["details"]["filters"]
        assert miss["value"] == value and miss["suggestion"] in {None, "apple", "pear"}


def test_a_stock_summed_across_entities_leaves_an_empty_group_null(runtime: Runtime) -> None:
    """No snapshot in February and none for s2 in March: NULL, never carried forward or 0."""
    on_hand = {"measure": "measure.obs.on_hand"}
    time = {"temporal_role": AS_OF, "grain": "month", "fill": True}
    time.update(start="2025-01-01", end="2025-04-01")
    for scope in ("dataset", "query"):
        response = _ask(
            runtime,
            scope,
            select=_select(on_hand=on_hand),
            group_by=[SNAPSHOT_STORE],
            where=_where(SNAPSHOT_STORE, ["s1", "s2"], "IN"),
            time=time,
        )
        got = {
            (row[SNAPSHOT_STORE], str(row[f"{AS_OF}__month"])[:7]): row["on_hand"]
            for row in response["rows"]
        }
        assert got == {
            ("s1", "2025-01"): 10,
            ("s1", "2025-02"): None,
            ("s1", "2025-03"): 8,
            ("s2", "2025-01"): 5,
            ("s2", "2025-02"): None,
            ("s2", "2025-03"): None,
        }


def test_the_package_default_switches_the_scope_and_a_query_overrides_it(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path / "obs", {"observation_scope": "query"})
    engine = Runtime.from_path(str(package))
    try:
        query = {"select": _select(qty=QTY), "where": APPLES_AT_S5}
        assert _ask(engine, None, **query)["rows"] == [{"qty": None}]
        assert _ask(engine, "dataset", **query)["rows"] == [{"qty": 0}]
    finally:
        engine.close()
    config = load_package_config(str(package))
    assert config.package.observation_scope == "query"
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(_package(tmp_path / "bad", {"observation_scope": "network"})))
    assert raised.value.code == "INVALID_CONFIG"
    assert "defaults.observation_scope" in str(raised.value)


def test_an_unknown_scope_is_an_invalid_query(package: Path) -> None:
    config = load_package_config(str(package))
    query = {"version": 1, "select": _select(qty=QTY), "observation_scope": "everything"}
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), query)
    assert raised.value.code == "INVALID_QUERY"
    assert raised.value.details["path"] == "observation_scope"


def test_a_shape_that_cannot_judge_outside_its_filters_refuses(package: Path) -> None:
    """Beside a distribution the combined outputs have no probe of their own: a filtered query
    refuses under the dataset scope and names the fix, and answers under the query scope."""
    config = load_package_config(str(package))
    median = {
        "kind": "distribution",
        "function": "median",
        "over": {"kind": "entity_value", "entity": "entity.obs_sale", "input": QTY},
    }
    query = {"version": 1, "select": _select(qty=QTY, median=median), "where": APPLES_AT_S5}
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), query)
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    assert raised.value.details["observation_scope"] == "dataset"
    compile_query(config, Registry(config), {**query, "observation_scope": "query"})
    # Unfiltered, the answer itself shows whether the measure has data: no probe, no refusal.
    compile_query(config, Registry(config), {**query, "where": []})


def test_a_nested_case_that_cannot_tell_unknown_from_no_rows_refuses(package: Path) -> None:
    """A CASE under arithmetic keeps the earlier settlement, which reads unknown amounts as 0
    where the measure has data: judged across the dataset that would hide them, so it refuses."""
    config = load_package_config(str(package))
    sales = next(row for row in config.measures if row.id == "measure.obs.qty")
    expr = {"kind": "arithmetic", "op": "multiply", "left": {"kind": "literal", "value": 2},
            "right": {"kind": "case", "whens": [{"when": {"kind": "comparison", "op": "=",
                "left": {"kind": "column", "column": "region"},
                "right": {"kind": "literal", "value": "east"}},
                "then": {"kind": "column", "column": "qty"}}]}}  # fmt: skip
    doubled = replace(sales, id="measure.obs.doubled_east", expr=parse_config_expression(expr))
    config = replace(config, measures=[*config.measures, doubled])
    query = {"version": 1, "select": _select(d={"measure": doubled.id}), "where": APPLES_AT_S5}
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, Registry(config), query)
    assert raised.value.code == "EMPTY_GROUPS_UNSETTLED"
    compile_query(config, Registry(config), {**query, "observation_scope": "query"})


def _select(**expressions: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"expression": expression, "as": alias} for alias, expression in expressions.items()]
