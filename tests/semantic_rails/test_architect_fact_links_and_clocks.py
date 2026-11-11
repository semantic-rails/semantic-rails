"""A package built with the Architect answers by month and across two facts.

Two facts that share a business key of two columns (a return names the sale line it returns by
order and line number) are linked by ``suggest_links``, with what makes the link safe said in
numbers: the key is unique on the sale side, how many returns find their sale, and on which
columns. A fact that keeps its dates only as keys into a calendar gets its measures dated by
``suggest_model``: one date key, or of several the one named after the row itself; where none is
defensible the draft dates none and says the choice is the author's. A measure left without a
time role is refused by month with the package line that fixes it, and validation names it.

Every answer is checked against hand-written SQL over the same DuckDB file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_introspection import open_duckdb, suggest_links, suggest_model
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import promote_package_report
from semantic_rails.runtime import Runtime

# 60 orders of 1 to 3 lines; a line number restarts on every order, so an order alone names
# several lines. Returns name the line they return by (sale_order_id, sale_line_no); one line is
# returned twice, and one return names a line no sale holds.
SEED = """
CREATE TABLE calendar AS
SELECT i::INTEGER AS day_key, DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
CREATE TABLE customers (customer_key INTEGER, customer_name VARCHAR);
INSERT INTO customers VALUES (1, 'Acme'), (2, 'Birch'), (3, 'Cedar'), (4, 'Dover');
CREATE TABLE sales AS
SELECT o::INTEGER AS order_id, l::INTEGER AS line_no,
       (1 + (o * 7) % 85)::INTEGER AS sale_day_key,
       (1 + (o * 7) % 85 + o % 4)::INTEGER AS ship_day_key,
       (1 + o % 4)::INTEGER AS customer_key,
       round(((o * 13 + l * 7) % 50) + 10.5, 2)::DOUBLE AS amount
FROM range(1, 61) a(o), range(1, 4) b(l) WHERE l <= 1 + o % 3;
CREATE TABLE returns AS
SELECT (row_number() OVER (ORDER BY order_id, line_no))::INTEGER AS return_id,
       order_id AS sale_order_id, line_no AS sale_line_no,
       least(90, sale_day_key + 5)::INTEGER AS return_day_key,
       round(amount / 2, 2)::DOUBLE AS refund_amount,
       CASE WHEN order_id % 2 = 0 THEN 'damaged' ELSE 'late' END AS reason
FROM sales WHERE (order_id * 3 + line_no) % 5 = 0;
INSERT INTO returns SELECT 1000, sale_order_id, sale_line_no, return_day_key, 4.0, 'late'
  FROM returns WHERE return_id = 3;
INSERT INTO returns VALUES (1001, 60, 3, 90, 7.5, 'damaged');
-- A claim per sale line, keyed by the line it claims on.
CREATE TABLE claims AS
SELECT order_id AS claim_order_id, line_no AS claim_line_no, 1.0::DOUBLE AS claim_amount
FROM sales WHERE order_id % 7 = 0;
-- Shipments with two day keys, neither named after the shipment.
CREATE TABLE shipments AS
SELECT order_id AS shipment_id, sale_day_key AS booked_day_key,
       ship_day_key AS dispatched_day_key, amount AS charge
FROM sales WHERE line_no = 1;
-- Own dates: one named after the order, one not; and two of which neither is.
CREATE TABLE orders AS
SELECT order_id, DATE '2025-01-01' + order_id::INTEGER AS order_date,
       DATE '2025-01-03' + order_id::INTEGER AS shipped_date, 1.0::DOUBLE AS total
FROM sales WHERE line_no = 1;
CREATE TABLE parcels AS
SELECT order_id AS parcel_id, DATE '2025-01-01' + order_id::INTEGER AS booked_on,
       DATE '2025-01-02' + order_id::INTEGER AS dispatched_on, 1.0::DOUBLE AS weight
FROM sales WHERE line_no = 1;
"""
REFUNDS_BY_CUSTOMER = """
SELECT c.customer_name, sum(r.refund_amount) FROM returns r
LEFT JOIN sales s ON s.order_id = r.sale_order_id AND s.line_no = r.sale_line_no
LEFT JOIN customers c ON c.customer_key = s.customer_key GROUP BY 1
"""
AMOUNT_BY_MONTH = """
SELECT strftime(date_trunc('month', c.day), '%Y-%m-%d'), sum(s.amount) FROM sales s
JOIN calendar c ON c.day_key = s.sale_day_key GROUP BY 1
"""
ROLE = "temporal_role.shop_day_day"


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _gold(pkg: Path, sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(str(pkg / "data" / "shop.duckdb"), read_only=True) as conn:
        return sorted(map(tuple, conn.execute(sql).fetchall()), key=repr)


def _warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "shop.duckdb"
    if not path.exists():
        with duckdb.connect(str(path)) as conn:
            conn.execute(SEED)
    return path


class _Session:
    """Architect MCP calls in one in-memory session, each write with the current revision."""

    def __init__(self, root: Path, pkg: Path) -> None:
        self.server, self.pkg, self.writes = (
            create_architect_mcp_server(workspace_root=root),
            pkg,
            0,
        )

    def run(self, steps: Any) -> Any:
        async def go() -> Any:
            async with create_connected_server_and_client_session(self.server) as session:
                return await steps(self, session)

        return asyncio.run(go())

    async def call(self, session: Any, name: str, **arguments: Any) -> dict[str, Any]:
        result = await session.call_tool(name, arguments)
        return dict(result.structuredContent or {})

    async def write(self, session: Any, name: str, **arguments: Any) -> dict[str, Any]:
        self.writes += 1
        status = await self.call(session, "project_status", project_path=str(self.pkg))
        return await self.call(
            session,
            name,
            project_path=str(self.pkg),
            expected_revision=status["revision"],
            idempotency_key=f"write-{self.writes}",
            **arguments,
        )


def _build(tmp_path: Path, *, prefer: bool = True) -> tuple[Path, list[dict[str, Any]]]:
    """A blind Architect build: create, explore, suggest, write what was suggested (a refused
    link retried as its refusal says first), validate. Returns the package and the log."""
    pkg = tmp_path / "shop"
    log: list[dict[str, Any]] = []

    async def steps(s: _Session, session: Any) -> None:
        created = await s.call(
            session,
            "create_project",
            package_id="shop",
            project_path=str(pkg),
            expected_revision="absent",
            idempotency_key="create",
            data="external",
            default_db="data/shop.duckdb",
            first_entity="day",
            relation="calendar",
            primary_key="day_key",
            time_column="day",
        )
        assert created["ok"], created
        (pkg / "data").mkdir(exist_ok=True)
        with duckdb.connect(str(pkg / "data" / "shop.duckdb")) as conn:
            conn.execute(SEED)
        for relation in ("customers", "returns", "sales"):
            suggestion = await s.call(
                session,
                "suggest_model",
                project_path=str(pkg),
                relation=relation,
                full_report=True,
            )
            log.append({"suggest_model": relation, **suggestion})
            written = await s.write(session, "upsert_model", **suggestion["upsert_model"])
            assert written["ok"], written
        for relation in ("returns", "sales"):
            links = await s.call(session, "suggest_links", project_path=str(pkg), relation=relation)
            log.append({"suggest_links": relation, **links})
            for row in links["proposals"]:
                written = await s.write(
                    session, "upsert_relationship", **row["upsert_relationship"]
                )
                if not written["ok"]:
                    log.append({"refused": written["error"]})
                    message = written["error"]["message"]
                    retry = (
                        "prefer_this_link"
                        if prefer and "prefer_this_link=true" in message
                        else "keep_existing_routes"
                    )
                    written = await s.write(
                        session,
                        "upsert_relationship",
                        **row["upsert_relationship"],
                        **{retry: True},
                    )
                assert written["ok"], written
        written = await s.write(
            session,
            "upsert_metric",
            metric_key="total_amount",
            spec={
                "kind": "aggregate",
                "measure": "measure.shop.amount",
                "value_type": "number",
                "label": "Sales amount",
            },
        )
        assert written["ok"], written
        checked = await s.call(session, "validate_project", project_path=str(pkg), mode="runtime")
        assert checked["ok"], checked

    _Session(tmp_path, pkg).run(steps)
    return pkg, log


def _ask(pkg: Path, call: dict[str, Any]) -> list[tuple[Any, ...]]:
    runtime = Runtime.from_path(str(pkg))
    try:
        rows = runtime.query(call)["rows"]
    finally:
        runtime.close()
    return sorted((tuple(row.values()) for row in rows), key=repr)


def test_a_blind_build_answers_by_month_and_across_the_two_facts(tmp_path):
    """The sale is dated by its own day key and the return linked to its sale line: sales by
    month and refunds by the customer of the returned sale equal the SQL, the return no sale
    holds in the NULL group."""
    pkg, log = _build(tmp_path)
    sales = next(row for row in log if row.get("suggest_model") == "sales")
    assert sales["upsert_model"]["entities"] == {"day": {"expr": "sale_day_key"}}
    assert {key: spec["times"] for key, spec in sales["upsert_model"]["measures"].items()} == {
        "amount": [ROLE]
    }
    (refused,) = [row["refused"] for row in log if "refused" in row]
    assert refused["code"] == "ROUTE_DECISION_NOT_RECORDED"
    assert refused["message"].startswith(
        "return and sale reach each other today through other entities' rows, so this link "
        "moves their route; nothing was written. Retry with prefer_this_link=true"
    )
    monthly = _ask(pkg, {"select": ["metric.shop.total_amount"], "time": {"grain": "month"}})
    assert [(row[0], row[-1]) for row in monthly] == _gold(pkg, AMOUNT_BY_MONTH)
    by_customer = {
        "select": ["measure.shop.refund_amount"],
        "group_by": ["dimension.shop_customer_customer_name"],
    }
    assert _ask(pkg, by_customer) == _gold(pkg, REFUNDS_BY_CUSTOMER)


def test_keeping_every_route_keeps_the_route_through_the_shared_day(tmp_path):
    """keep_existing_routes alone keeps the return's route to its sale through their shared
    day, as it says, so refunds by customer stay refused: that route reads every sale of the
    day, never the one the return names."""
    pkg, _ = _build(tmp_path, prefer=False)
    runtime = Runtime.from_path(str(pkg))
    try:
        with pytest.raises(SemanticLayerError) as refused:
            runtime.query(
                {
                    "select": ["measure.shop.refund_amount"],
                    "group_by": ["dimension.shop_customer_customer_name"],
                }
            )
    finally:
        runtime.close()
    assert refused.value.code == "MIXED_GRAIN_INVALID"


def test_a_two_column_key_is_proposed_with_what_makes_it_safe(tmp_path):
    """The return's (sale_order_id, sale_line_no) is proposed against the sale's two-column key,
    in key order, many to one (one line is returned twice), with the uniqueness, the coverage
    and the columns in words; the order alone is never proposed."""
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        links = suggest_links(
            warehouse,
            "returns",
            targets=[{"relation": "sales", "key": ["order_id", "line_no"], "entity": "sale"}],
            from_entity="return",
        )
    (proposal,) = links["proposals"]
    assert proposal["columns"] == ["sale_order_id", "sale_line_no"]
    assert proposal["cardinality"] == "many_to_one"
    assert proposal["upsert_relationship"] == {
        "from_entity": "return",
        "to_entity": "sale",
        "columns": ["sale_order_id", "sale_line_no"],
        "cardinality": "many_to_one",
    }
    evidence = proposal["evidence"]
    assert (evidence["distinct_found"], evidence["distinct_values"]) == (24, 25)
    assert (evidence["rows_found"], evidence["rows_with_value"]) == (25, 26)
    assert evidence["missing_samples"] == [[60, 3]]
    assert proposal["reason"] == (
        "sales is keyed by (order_id, line_no), never null and never repeated on its 120 rows, "
        "so each row reads at most one of them; matched on sale_order_id = order_id and "
        "sale_line_no = line_no, 24 of 25 distinct (sale_order_id, sale_line_no) values "
        "(96.0%) are its keys, on 25 of 26 rows with a value in every column; not found: "
        "(60, 3)."
    )
    assert links["below_threshold"] == links["not_proposed"] == []


def test_a_key_that_is_the_reference_links_one_to_one_and_a_repeated_key_is_no_lookup(tmp_path):
    """A claim keyed by the sale line it claims on links one to one. Against a target whose two
    columns repeat (returns by sale line: one line returned twice), nothing is drafted: a
    many-to-one link there would count the claim once per return."""
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        claims = suggest_links(
            warehouse,
            "claims",
            targets=[{"relation": "sales", "key": ["order_id", "line_no"], "entity": "sale"}],
            from_entity="claim",
        )
        repeated = suggest_links(
            warehouse,
            "claims",
            ["claim_order_id", "claim_line_no"],
            targets=[
                {"relation": "returns", "key": ["sale_order_id", "sale_line_no"], "entity": "r"}
            ],
            from_entity="claim",
        )
    (proposal,) = claims["proposals"]
    assert (proposal["columns"], proposal["cardinality"]) == (
        ["claim_order_id", "claim_line_no"],
        "one_to_one",
    )
    assert repeated["proposals"] == repeated["below_threshold"] == []
    (refused,) = repeated["not_proposed"]
    assert "upsert_relationship" not in refused
    assert refused["reason"].startswith(
        "returns.(sale_order_id, sale_line_no) repeats or is missing on some rows"
    )


def test_two_day_keys_get_named_roles_and_the_choice_is_stated(tmp_path):
    """Linked by suggest_links alone, a sale's two day keys would each replace the other's link:
    each draft carries its own name, and each row says the choice is the author's."""
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        links = suggest_links(
            warehouse,
            "sales",
            targets=[{"relation": "calendar", "key": "day_key", "entity": "day"}],
            from_entity="sale",
        )
    names = {row["column"]: row["upsert_relationship"]["name"] for row in links["proposals"]}
    assert names == {"sale_day_key": "sale_sale_day", "ship_day_key": "sale_ship_day"}
    assert links["proposals"][0]["choice"] == (
        "2 column sets of sales each hold a day key (sale_day_key, ship_day_key). Each draft "
        "writes a named role, so neither replaces the other; which one a question by day means "
        "is a business choice: record it with record_route_decision, or such a question is "
        "refused until you do."
    )


CLOCK = [{"entity": "day", "relation": "calendar", "key": "day_key", "temporal_role": ROLE}]


def test_a_sale_is_dated_by_its_own_day_key_and_the_ship_day_is_a_named_role(tmp_path):
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        sales = suggest_model(warehouse, "sales", clocks=CLOCK)
    roles = sales["time_roles"]
    assert (roles["status"], roles["default"], roles["candidates"]) == (
        "default",
        "sale_day_key",
        ["sale_day_key", "ship_day_key"],
    )
    assert roles["message"] == (
        "sales has no date column of its own and 2 columns holding keys of day (sale_day_key, "
        "ship_day_key). The draft dates each measure by sale_day_key, named after the sale "
        "itself: it links sale_day_key (entities.day) and lists temporal_role.shop_day_day in "
        "each measure's `times:`. ship_day_key stays unlinked here: each of `other_keys` adds "
        "one as a named role and keeps sale_day_key the default route."
    )
    (other,) = roles["other_keys"]
    assert other["upsert_relationship"] == {
        "from_entity": "sale",
        "to_entity": "day",
        "columns": ["ship_day_key"],
        "cardinality": "many_to_one",
        "name": "sale_ship_day",
        "keep_existing_routes": True,
    }
    draft = sales["upsert_model"]
    assert draft["entities"] == {"day": {"expr": "sale_day_key"}}
    assert all(spec["times"] == [ROLE] for spec in draft["measures"].values())


def test_a_single_day_key_dates_the_measures_and_two_unnamed_ones_are_a_choice(tmp_path):
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        returns = suggest_model(warehouse, "returns", clocks=CLOCK)
        shipments = suggest_model(warehouse, "shipments", clocks=CLOCK)
        bare = suggest_model(warehouse, "shipments")
    assert returns["time_roles"]["message"] == (
        "returns has no date column of its own; return_day_key holds keys of day, so the draft "
        "links it (entities.day) and dates each measure by temporal_role.shop_day_day. 25 of 25 "
        "distinct return_day_key values (100.0%) are calendar.day_key keys, on 26 of 26 rows "
        "with a value."
    )
    roles = shipments["time_roles"]
    assert roles["status"] == "choose"
    assert roles["message"] == (
        "shipments has no date column of its own and 2 columns holding keys of day "
        "(booked_day_key, dispatched_day_key); none is named after the shipment itself, so "
        "which one dates its rows is a business choice, and the draft dates no measure. Choose "
        "one of `options`: add its `entities` to the draft and its `times` to each measure; "
        "then add each other one as a named role (upsert_relationship with a name)."
    )
    assert [option["entities"] for option in roles["options"]] == [
        {"day": {"expr": "booked_day_key"}},
        {"day": {"expr": "dispatched_day_key"}},
    ]
    draft = shipments["upsert_model"]
    assert "entities" not in draft
    assert all("times" not in spec for spec in draft["measures"].values())
    # Without a package there is no calendar to read: the draft is as before.
    assert "time_roles" not in bare and bare["upsert_model"] == draft


def test_of_several_own_dates_the_one_named_after_the_row_is_the_default(tmp_path):
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        orders = suggest_model(warehouse, "orders")
        parcels = suggest_model(warehouse, "parcels")
    assert orders["time_roles"]["default"] == "order_date"
    assert orders["upsert_model"]["times"]["order_date"]["default"] is True
    assert "default" not in orders["upsert_model"]["times"]["shipped_date"]
    assert orders["time_roles"]["message"] == (
        "orders has 2 date columns (order_date, shipped_date). The draft dates its measures by "
        "order_date, named after the order itself; the others stay times of the model, to "
        "group by their dates or to list in a measure's `times:`. If the business reads these "
        "rows by another, mark that one `default: true` instead."
    )
    assert parcels["time_roles"]["status"] == "choose"
    assert parcels["time_roles"]["message"] == (
        "parcels has 2 date columns (booked_on, dispatched_on) and none is named after the "
        "parcel itself, so which one dates its rows is a business choice: the draft marks none "
        "`default: true`, and its measures can't be asked by month until one is. Mark the one "
        "the business reads them by `default: true` in the draft's times."
    )
    assert all("default" not in spec for spec in parcels["upsert_model"]["times"].values())


def _untimed(tmp_path: Path) -> Path:
    """The blind build with the sale's measure undated, as an older draft left it."""
    pkg, _ = _build(tmp_path)
    path = pkg / "models" / "core" / "sales.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for spec in document["model"]["measures"].values():
        spec.pop("times", None)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return pkg


def test_an_undated_measure_is_refused_with_the_line_that_dates_it(tmp_path):
    pkg = _untimed(tmp_path)
    runtime = Runtime.from_path(str(pkg))
    try:
        with pytest.raises(SemanticLayerError) as refused:
            runtime.query(
                {
                    "select": ["measure.shop.amount"],
                    "time": {"temporal_role": ROLE, "grain": "month"},
                }
            )
    finally:
        runtime.close()
    error = refused.value
    assert error.code == "INCOMPATIBLE_TEMPORAL_ROLE"
    assert str(error) == (
        "'measure.shop.amount' has no time role declared, so it can't be placed on "
        "'temporal_role.shop_day_day'. Its rows reach that clock by to-one links: if it dates "
        "them, add `times: [temporal_role.shop_day_day]` to measure amount in model sales "
        "(Architect suggest_model with project_path drafts it); or drop `time` from the query."
    )
    declare = {"model": "sales", "measure": "amount", "times": [ROLE]}
    assert error.details == {
        "measure": "measure.shop.amount",
        "requested": ROLE,
        "compatible": [],
        "declare": declare,
    }
    (hint,) = recovery_hints_for_error(error.code, error.details)
    assert hint["declare"] == declare
    assert hint["message"].startswith(
        "'measure.shop.amount' has no time role. Add `times: [temporal_role.shop_day_day]` to "
        "measure amount in model sales, then query it by that role."
    )


def test_validation_and_the_promotion_check_name_the_undated_measure(tmp_path):
    pkg = _untimed(tmp_path)
    ref = PackageReference(source_path=str(pkg))
    report, _ = parse_config_report(ref)
    (warning,) = [row for row in report["warnings"] if row["code"] == "MEASURE_WITHOUT_TIME_ROLE"]
    assert warning["message"].endswith(
        "measure.shop.amount has no time role, so it cannot be asked by month or in a window, "
        "although its rows reach temporal_role.shop_day_day. If it dates its rows, add `times: "
        "[temporal_role.shop_day_day]` to measure amount in model sales."
    )
    assert warning["details"]["reachable_temporal_roles"] == [ROLE]
    promotion = promote_package_report(ref, environment="development")
    assert [
        row["details"]["measure"]
        for row in promotion["advisories"]
        if row["code"] == ("MEASURE_WITHOUT_TIME_ROLE")
    ] == ["measure.shop.amount"]
    # A dated package has none.
    dated, _ = _build(tmp_path / "dated")
    clean, _ = parse_config_report(PackageReference(source_path=str(dated)))
    assert not [row for row in clean["warnings"] if row["code"] == "MEASURE_WITHOUT_TIME_ROLE"]
