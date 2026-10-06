"""Row-based rewrites must not bypass a stock's choice of snapshots.

The public planner already refuses stock fan-out paths and the loader refuses a stock
as a lookup source. Direct SQL lowering must retain those boundaries too. Controls use
the same changing-plan snapshots as the closing-first reference and grouped breakdown.
"""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.compiler import compile_query, plan_query
from semantic_rails.compiler_parts.sql_lowering import lower_to_sql
from semantic_rails.errors import SemanticLayerError
from semantic_rails.runtime import Runtime
from tests.semantic_rails.test_stock_filtered_by_changing_attribute import (
    PLAN,
    ROLE,
    WEEK,
    _is,
    _package,
    _query,
    _reference,
)

NOTICE_KIND = "dimension.fees_notice_kind"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    root = _package(tmp_path)
    path = root / "models" / "account_days.yml"
    doc = yaml.safe_load(path.read_text())
    doc["model"]["measures"]["fee_flow"] = {"kind": "aggregate", "expr": "fee"}
    path.write_text(yaml.safe_dump(doc))
    path = root / "models" / "notices.yml"
    doc = yaml.safe_load(path.read_text())
    doc["model"]["dimensions"]["kind"] = {"kind": "categorical"}
    path.write_text(yaml.safe_dump(doc))
    with duckdb.connect(str(root / "data" / "fees.duckdb")) as connection:
        connection.execute("alter table notices add column kind varchar")
        # Two matching children per snapshot exercise both EXISTS and de-duplication.
        connection.execute(
            "insert into notices (notice_id, account_id, date_day, kind) "
            "select row_number() over (), account_id, date_day, 'renewal' "
            "from account_day cross join (values (1), (2)) copies(n)"
        )
    return root


@pytest.fixture
def runtime(package: Path) -> Iterator[Runtime]:
    engine = Runtime.from_path(str(package))
    try:
        yield engine
    finally:
        engine.close()


def _payload(measure: str = "fee", *, placement: str = "where") -> dict[str, Any]:
    attribute = _is(PLAN, "basic")
    child = _is(NOTICE_KIND, "renewal")
    expression: dict[str, Any] = {"measure": f"measure.fees.{measure}", "aggregation": "sum"}
    where = [attribute, child]
    if placement == "measure_filter":
        expression["kind"] = "aggregate"
        expression["filter"] = {"all": where}
        where = []
    elif placement == "child_group":
        where = [attribute, {"child": "entity.fees_notice", "match": "any", "where": [child]}]
    return {
        "version": 1,
        "select": [{"expression": expression, "as": "v"}],
        "where": where,
    }


@pytest.mark.parametrize("warehouse", ["duckdb", "clickhouse"], ids=["semijoin", "dedup"])
@pytest.mark.parametrize("placement", ["where", "measure_filter", "child_group"])
@pytest.mark.parametrize("measure", ["fee", "fee_sop"])
def test_a_stock_on_a_child_filter_path_is_refused(
    runtime: Runtime, warehouse: str, placement: str, measure: str
) -> None:
    config = replace(runtime.config, package=replace(runtime.config.package, warehouse=warehouse))
    query = {
        **_payload(measure, placement=placement),
        "time": {"temporal_role": ROLE, "grain": "week"},
    }
    with pytest.raises(SemanticLayerError) as raised:
        compile_query(config, None, query)
    assert raised.value.code == "MIXED_GRAIN_INVALID"

    # The same snapshots on the supported leaf read the basic breakdown row, not a's
    # earlier basic snapshot. Every snapshot has the requested child, so it adds no restriction.
    answer = _query(runtime, [_is(PLAN, "basic")], measure=measure, time={"grain": "week"})
    assert answer == _reference("plan = 'basic'", measure=measure)
    breakdown = _query(runtime, measure=measure, group_by=[PLAN], time={"grain": "week"})
    assert answer == [(period, fee) for period, plan, fee in breakdown if plan == "basic"]
    assert (WEEK, 99 if measure == "fee" else 297) in answer


@pytest.mark.parametrize("warehouse", ["duckdb", "clickhouse"], ids=["semijoin", "dedup"])
@pytest.mark.parametrize("placement", ["where", "measure_filter", "child_group"])
def test_a_flow_on_the_same_child_filter_path_keeps_its_answer(
    runtime: Runtime, package: Path, warehouse: str, placement: str
) -> None:
    config = replace(runtime.config, package=replace(runtime.config.package, warehouse=warehouse))
    query = _payload("fee_flow", placement=placement)
    compiled = compile_query(config, None, query)
    assert compiled["logical_plan"].measure_plans[0].rewrite_strategy == "fanout_dedup"
    sql = compiled["prepared_query"].sql.removesuffix("\nSETTINGS join_use_nulls = 1")
    assert ("EXISTS (" if warehouse == "duckdb" else "SELECT DISTINCT") in sql
    with duckdb.connect(str(package / "data" / "fees.duckdb"), read_only=True) as connection:
        reference = connection.execute(
            "select sum(fee) from account_day d where plan = 'basic' and exists "
            "(select 1 from notices n where n.account_id = d.account_id "
            "and n.date_day = d.date_day and n.kind = 'renewal')"
        ).fetchall()
        assert connection.execute(sql).fetchall() == reference


@pytest.mark.parametrize("source", ["fee", "fee_sop", "fee_flow"])
def test_a_parent_lookup_source_refuses_stock_and_keeps_flow(package: Path, source: str) -> None:
    path = package / "models" / "notices.yml"
    doc = yaml.safe_load(path.read_text())
    doc["model"]["entities"]["account"] = {}
    doc["model"]["measures"] = {"carried_fee": {"kind": "lookup", "from": source, "via": "account"}}
    path.write_text(yaml.safe_dump(doc))
    if source != "fee_flow":
        with pytest.raises(SemanticLayerError) as raised:
            Runtime.from_path(str(package))
        assert raised.value.code == "INVALID_CONFIG"
        assert raised.value.details == {"key": "from"}
        assert "stock" in str(raised.value)
        return
    engine = Runtime.from_path(str(package))
    try:
        query = {
            "version": 1,
            "select": [{"expression": {"measure": "measure.fees.carried_fee"}, "as": "v"}],
            "group_by": ["dimension.fees_notice_account_id"],
            "where": [_is(NOTICE_KIND, "renewal")],
        }
        assert (
            compile_query(engine.config, None, query)["logical_plan"]
            .measure_plans[0]
            .rewrite_strategy
            == "parent_lookup"
        )
        with duckdb.connect(str(package / "data" / "fees.duckdb"), read_only=True) as connection:
            reference = connection.execute(
                "select account_id, sum(fee) from account_day group by account_id order by account_id"
            ).fetchall()
        assert (
            _query(
                engine,
                query["where"],
                measure="carried_fee",
                group_by=query["group_by"],
            )
            == reference
        )
    finally:
        engine.close()


@pytest.mark.parametrize(
    "warehouse,rewrite",
    [("duckdb", "fanout_dedup"), ("clickhouse", "fanout_dedup"), ("duckdb", "parent_lookup")],
    ids=["semijoin", "dedup", "parent_lookup"],
)
@pytest.mark.parametrize("guard_empty", [True, False])
@pytest.mark.parametrize("measure", ["fee", "fee_sop"])
def test_bypassing_the_stock_rewrite_checks_refuses_at_sql_lowering(
    runtime: Runtime, warehouse: str, rewrite: str, guard_empty: bool, measure: str
) -> None:
    query = _payload(measure)
    query["time"] = {"temporal_role": ROLE, "grain": "week"}
    if rewrite == "parent_lookup":
        query["where"] = [_is(PLAN, "basic")]
        plan = plan_query(runtime.config, None, query)
    else:
        # Keep a real child path, but bypass the planner's stock check by replacing the
        # flow measure with a stock after planning.
        flow_query = {**_payload("fee_flow"), "time": query["time"]}
        plan = plan_query(runtime.config, None, flow_query)
        bound = replace(plan.bound_measures[0], measure_id=f"measure.fees.{measure}")
        plan = replace(
            plan,
            query=query,
            bound_measures=[bound],
            measure_plans=[replace(plan.measure_plans[0], bound_measure=bound)],
        )
    config = replace(runtime.config, package=replace(runtime.config.package, warehouse=warehouse))
    if rewrite == "parent_lookup":
        # Also bypass the loader's prohibition on a stock carrying a lookup value.
        config = replace(
            config,
            measures=[
                replace(row, lookup_from="measure.fees.fee_flow", lookup_via="entity.fees_account")
                if row.id == f"measure.fees.{measure}"
                else row
                for row in config.measures
            ],
        )
    plan = replace(plan, measure_plans=[replace(plan.measure_plans[0], rewrite_strategy=rewrite)])
    with pytest.raises(SemanticLayerError) as raised:
        lower_to_sql(plan, config, guard_empty=guard_empty)
    assert raised.value.code == "REWRITE_NOT_SUPPORTED"
    assert raised.value.details == {
        "reason": "stock_requires_snapshot_selection",
        "measure": f"measure.fees.{measure}",
        "rewrite_strategy": rewrite,
    }
