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
from semantic_rails.dialects import _WAREHOUSE_CONNECTORS
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import parse_config_expression
from semantic_rails.registry import Registry
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from tests.semantic_rails.empty_groups_invariant import assert_settled_in_one_place

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
QTY_METRIC = "metric.obs.qty"
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
        engine._get_adapter()  # the module fixture owns its connection across tests
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
def test_apples_whose_quantity_is_unknown_stay_unknown(runtime: Runtime, scope: str) -> None:
    """Store s4's one apple sale has no quantity: rows exist, so its sum is unknown (NULL) in
    either scope, never 0, and its count counts the row."""
    where = "product = 'apple' AND store_id = 's4'"
    sql = f"SELECT SUM(qty), COUNT(*) FROM sales WHERE {where}"
    where_items = [*_where(PRODUCT, "apple"), *_where(STORE, "s4")]
    response = _ask(runtime, scope, select=_select(qty=QTY, sales=SALES), where=where_items)
    assert [tuple(row.values()) for row in response["rows"]] == _gold(sql) == [(None, 1)]
    assert ("NO_DATA_IN_SCOPE" in _codes(response)) is (scope == "query")
    # The same rule holds when every output in the response is NULL.
    quantity = _ask(runtime, scope, select=_select(qty=QTY), where=where_items)
    assert quantity["rows"] == [{"qty": None}]
    assert ("NO_DATA_IN_SCOPE" in _codes(quantity)) is (scope == "query")


@pytest.mark.parametrize("warehouse", sorted(_WAREHOUSE_CONNECTORS))
def test_every_warehouse_probes_with_plain_ctes(package: Path, warehouse: str) -> None:
    """The probe is a first-row CTE counted and cross-joined, which every warehouse runs."""
    config = load_package_config(str(package))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    query = {"version": 1, "select": _select(qty=QTY), "where": APPLES_AT_S5}
    sql = compile_query(config, Registry(config), query)["sql"]
    assert "observed_1_rows AS" in sql and "LIMIT 1" in sql and "CROSS JOIN observed_1" in sql
    assert "EXISTS" not in sql


@pytest.mark.parametrize("condition_index", [0, 1])
def test_the_invariant_detects_each_lost_authored_condition(
    package: Path, condition_index: int
) -> None:
    config = load_package_config(str(package))
    regional_apples = {
        **QTY,
        "kind": "aggregate",
        "filter": {
            "all": [
                *_where(PRODUCT, "apple"),
                *_where("dimension.obs_sale_region", "east"),
            ]
        },
    }
    compiled = compile_query(
        config,
        Registry(config),
        {
            "version": 1,
            "select": _select(qty=regional_apples),
            "where": _where(STORE, "s5"),
        },
    )
    assert_settled_in_one_place(compiled, config)
    probe = next(cte.query for cte in compiled["sql_ast"].ctes if cte.name == "observed_1_rows")
    probe.where.pop(condition_index)
    with pytest.raises(AssertionError, match="dataset probe lost an authored leaf condition"):
        assert_settled_in_one_place(compiled, config)


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


@pytest.mark.parametrize("verbosity", ["minimal", "compact", "full"])
def test_a_granted_metric_keeps_the_unverified_filter_warning(
    runtime: Runtime, verbosity: str
) -> None:
    context = RequestContext(metric_allowlist=(QTY_METRIC,), dimension_allowlist=(PRODUCT,))
    response = _ask(
        runtime,
        None,
        select=_select(qty={"metric": QTY_METRIC}),
        where=_where(PRODUCT, "appel"),
        policy_context=context.to_policy_context(),
        verbosity=verbosity,
    )
    assert response["ok"]
    if verbosity != "minimal":
        assert response["status"] == "ok"
    assert (
        [tuple(row.values()) for row in response["rows"]]
        == _gold(DATASET_QTY.format(where="product = 'appel'"))
        == [(0,)]
    )
    (warning,) = response["warnings"]
    # The dimension-only existence probe remains forbidden by the resource grant.
    assert warning["code"] == "FILTER_VALUE_UNVERIFIED"
    assert "'appel'" in warning["message"]
    assert warning["object_ids"] == [PRODUCT]
    assert warning["details"]["filters"] == [{"dimension": PRODUCT, "value": "appel"}]


def test_a_granted_metric_still_refuses_an_ungranted_filter(runtime: Runtime) -> None:
    context = RequestContext(metric_allowlist=(QTY_METRIC,), dimension_allowlist=(PRODUCT,))
    with pytest.raises(SemanticLayerError) as exc:
        _ask(
            runtime,
            None,
            select=_select(qty={"metric": QTY_METRIC}),
            where=_where(STORE, "s5"),
            policy_context=context.to_policy_context(),
        )
    assert exc.value.code == "RESOURCE_ACCESS_DENIED"


def test_one_warning_names_every_value_that_matched_nothing(runtime: Runtime) -> None:
    where = [*_where(PRODUCT, ["apple", "peer"], "IN"), *_where(STORE, "s9")]
    response = _ask(runtime, None, select=_select(qty=QTY), where=where)
    (typo,) = [item for item in response["warnings"] if item["code"] == "FILTER_VALUE_NOT_FOUND"]
    assert [(row["dimension"], row["value"]) for row in typo["details"]["filters"]] == [
        (PRODUCT, "peer"),
        (STORE, "s9"),
    ]


@pytest.mark.parametrize("failed_execution", [2, 3])
def test_a_failed_value_probe_never_silences_the_guard(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, failed_execution: int
) -> None:
    adapter = runtime._get_adapter()
    original = adapter.query_prepared
    calls = 0

    def query(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == failed_execution:
            raise SemanticLayerError("QUERY_EXECUTION_ERROR", "Value read failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "query_prepared", query)
    response = _ask(runtime, "dataset", select=_select(qty=QTY), where=_where(PRODUCT, "appel"))
    assert response["rows"] == [{"qty": 0}]
    code = "FILTER_VALUE_UNVERIFIED" if failed_execution == 2 else "FILTER_VALUE_NOT_FOUND"
    (warning,) = [item for item in response["warnings"] if item["code"] == code]
    (value,) = warning["details"]["filters"]
    assert value["dimension"] == PRODUCT and value["value"] == "appel"
    if failed_execution == 2:
        assert "could not be verified" in warning["message"]
        assert PRODUCT in warning["message"] and "'appel'" in warning["message"]
    else:
        assert value["suggestion"] is None


def test_a_filter_only_dimension_reports_unverified_values(package: Path) -> None:
    config = load_package_config(str(package))
    config = replace(
        config,
        dimensions=[
            replace(row, groupable=False) if row.id == PRODUCT else row for row in config.dimensions
        ],
    )
    engine = Runtime.from_config(config, source_path=str(package))
    try:
        response = _ask(
            engine,
            "dataset",
            select=_select(qty=QTY),
            where=_where(PRODUCT, ["apple", "appel"], "IN"),
        )
        assert response["rows"] == [{"qty": 5}]
        (warning,) = [w for w in response["warnings"] if w["code"] == "FILTER_VALUE_UNVERIFIED"]
        assert [(v["dimension"], v["value"]) for v in warning["details"]["filters"]] == [
            (PRODUCT, "apple"),
            (PRODUCT, "appel"),
        ]
    finally:
        engine.close()


@pytest.mark.parametrize("data_type", ["string", "id"])
def test_each_literal_uses_warehouse_equality(package: Path, data_type: str) -> None:
    config = load_package_config(str(package))
    config = replace(
        config,
        dimensions=[
            replace(row, data_type=data_type) if row.id == PRODUCT else row
            for row in config.dimensions
        ],
    )
    engine = Runtime.from_config(config, source_path=str(package))
    try:
        response = _ask(
            engine,
            "dataset",
            select=_select(qty=QTY),
            where=_where(PRODUCT, ["apple", "Apple"], "IN"),
        )
    finally:
        engine.close()
    assert response["rows"] == [{"qty": 5}]
    (warning,) = [w for w in response["warnings"] if w["code"] == "FILTER_VALUE_NOT_FOUND"]
    assert warning["details"]["filters"] == [
        {"dimension": PRODUCT, "value": "Apple", "suggestion": "apple"},
    ]


def test_every_value_probe_keeps_request_limits_and_identity(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Runtime.query
    calls: list[dict[str, Any]] = []

    def query(engine: Runtime, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return original(engine, payload)

    monkeypatch.setattr(Runtime, "query", query)
    limits = {"statement_timeout_ms": 1000}
    context = REGIONAL.to_policy_context()
    response = _ask(
        runtime,
        "dataset",
        select=_select(qty=QTY),
        where=_where(PRODUCT, "appel"),
        limits=limits,
        request_id="value-probe",
        policy_context=context,
    )
    assert "FILTER_VALUE_NOT_FOUND" in _codes(response)
    assert len(calls) == 3  # answer, existence, optional suggestion
    for probe in calls[1:]:
        assert probe["limits"] == limits
        assert probe["request_id"] == "value-probe"
        assert probe["policy_context"] == context


def test_the_settlement_read_keeps_policy_parameters_and_timeout(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = runtime._get_adapter()
    original = adapter.query_prepared
    calls: list[dict[str, Any]] = []

    def query(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "query_prepared", query)
    context = replace(REGIONAL, attributes={"region": "west"}).to_policy_context()
    response = _ask(
        runtime,
        "dataset",
        select=_select(qty=QTY),
        where=[*_where(PRODUCT, "apple"), *_where(STORE, "s4")],
        policy_context=context,
        limits={"statement_timeout_ms": 1000},
    )
    assert response["rows"] == [{"qty": None}]
    assert "NO_DATA_IN_SCOPE" not in _codes(response)  # the west has known kale quantities
    assert len(calls) == 4  # answer, settlement observation, two existence reads
    for call in calls:
        assert call["limits"]["statement_timeout_ms"] == 1000
        assert call["parameters"] and set(call["parameters"]) == {"west"}


def test_the_settlement_read_retains_derived_relation_dependencies(tmp_path: Path) -> None:
    package = _package(tmp_path / "obs", {})
    (package / "policies.yml").unlink()  # row filters require a plain physical relation
    relations = {
        "relations": {
            "all_sales": {
                "source": "sales",
                "columns": ["sale_id", "store_id", "region", "product", "qty", "sold_at"],
                "steps": [],
            }
        }
    }
    (package / "relations.yml").write_text(yaml.safe_dump(relations))
    model = yaml.safe_load((package / "models/sales.yml").read_text())
    model["model"]["relation"] = "all_sales"
    (package / "models/sales.yml").write_text(yaml.safe_dump(model))
    engine = Runtime.from_path(str(package))
    try:
        response = _ask(
            engine,
            "dataset",
            select=_select(qty=QTY),
            where=[*_where(PRODUCT, "apple"), *_where(STORE, "s4")],
        )
        assert response["rows"] == [{"qty": None}]
        assert "NO_DATA_IN_SCOPE" not in _codes(response)
    finally:
        engine.close()


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


@pytest.mark.parametrize("scope", ["dataset", "query"])
def test_a_filtered_empty_month_observes_the_selected_scope(runtime: Runtime, scope: str) -> None:
    """February has no pears at s1; dataset pears elsewhere permit 0, query data does not."""
    observation = "" if scope == "dataset" else "AND store_id = 's1'"
    (expected,) = _gold(
        "SELECT CASE WHEN COUNT(*) > 0 THEN SUM(qty) WHEN EXISTS "
        "(SELECT 1 FROM sales WHERE product = 'pear' AND qty IS NOT NULL "
        f"{observation}) THEN 0 END FROM sales WHERE product = 'pear' AND store_id = 's1' "
        "AND sold_at >= TIMESTAMP '2025-02-01' AND sold_at < TIMESTAMP '2025-03-01'"
    )
    pears = {**QTY, "kind": "aggregate", "filter": {"all": _where(PRODUCT, "pear")}}
    response = _ask(
        runtime,
        scope,
        select=_select(qty=pears),
        where=_where(STORE, "s1"),
        time={
            "temporal_role": SOLD,
            "grain": "month",
            "fill": True,
            "start": "2025-02-01",
            "end": "2025-03-01",
        },
    )
    assert [row["qty"] for row in response["rows"]] == [expected[0]]
    assert expected == ((0,) if scope == "dataset" else (None,))


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
