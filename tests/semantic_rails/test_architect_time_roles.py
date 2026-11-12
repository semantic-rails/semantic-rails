"""A package built with the Architect answers by month.

A fact that keeps its date only as one key into a calendar gets its measures dated by
``suggest_model``; with several such keys, or one beside a date of its own, the draft dates none
and says the choice is the author's, and a table's own dates are drafted as before. A measure
left without a time role is refused by month with the package line that fixes it, and validation
names it.

Every answer is checked against hand-written SQL over the same DuckDB file.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_introspection import open_duckdb, suggest_model
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import promote_package_report
from semantic_rails.runtime import Runtime
from semantic_rails.untimed_measures import _to_one_reach

# 60 orders of 1 to 3 lines, each sale with two day keys; returns with one day key; shipments
# with two day keys named after neither; orders and parcels with dates of their own.
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
AMOUNT_BY_MONTH = """
SELECT strftime(date_trunc('month', c.day), '%Y-%m-%d'), sum(s.amount) FROM sales s
JOIN calendar c ON c.day_key = s.sale_day_key GROUP BY 1
"""
REFUNDS_BY_MONTH = """
SELECT strftime(date_trunc('month', c.day), '%Y-%m-%d'), sum(r.refund_amount) FROM returns r
JOIN calendar c ON c.day_key = r.return_day_key GROUP BY 1
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


def _build(
    tmp_path: Path,
    *,
    seed: str = SEED,
    models: tuple[str, ...] = ("customers", "returns", "sales"),
    links: tuple[str, ...] = ("returns", "sales"),
    dates: dict[str, str] | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    """A blind Architect build: create, explore, suggest, write what was suggested, validate.
    Where ``suggest_model`` states a choice of date, the author picks ``dates[relation]`` (a sale
    is dated by its sale day). Returns the package and the log."""
    pkg = tmp_path / "shop"
    log: list[dict[str, Any]] = []
    picks = {"sales": "sale_day_key"} if dates is None else dates

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
            conn.execute(seed)
        for relation in models:
            suggestion = await s.call(
                session,
                "suggest_model",
                project_path=str(pkg),
                relation=relation,
                full_report=True,
            )
            log.append({"suggest_model": relation, **suggestion})
            draft = copy.deepcopy(suggestion["upsert_model"])
            roles = suggestion.get("time_roles") or {}
            if roles.get("status") == "choose" and relation in picks:
                (option,) = [row for row in roles["options"] if row["column"] == picks[relation]]
                draft = {**draft, "entities": {**draft.get("entities", {}), **option["entities"]}}
                for spec in draft["measures"].values():
                    spec["times"] = option["times"]
            written = await s.write(session, "upsert_model", **draft)
            assert written["ok"], written
        for relation in links:
            proposed = await s.call(
                session, "suggest_links", project_path=str(pkg), relation=relation
            )
            log.append({"suggest_links": relation, **proposed})
            for row in proposed["proposals"]:
                written = await s.write(
                    session, "upsert_relationship", **row["upsert_relationship"]
                )
                assert written["ok"], written
        if "sales" in models:
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


def _ask(pkg: Path, call: dict[str, Any]) -> list[tuple[Any, ...]]:
    runtime = Runtime.from_path(str(pkg))
    try:
        rows = runtime.query(call)["rows"]
    finally:
        runtime.close()
    return sorted((tuple(row.values()) for row in rows), key=repr)


def test_a_blind_build_answers_by_month(tmp_path):
    """The sale's two day keys are a choice its draft states; dated by its sale day as the
    author picks, sales by month equal the SQL. The return's one day key dates it with no
    choice to make."""
    pkg, log = _build(tmp_path)
    sales = next(row for row in log if row.get("suggest_model") == "sales")
    roles = sales["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["sale_day_key", "ship_day_key"])
    assert "entities" not in sales["upsert_model"]
    assert all("times" not in spec for spec in sales["upsert_model"]["measures"].values())
    returns = next(row for row in log if row.get("suggest_model") == "returns")
    assert (returns["time_roles"]["status"], returns["time_roles"]["default"]) == (
        "default",
        "return_day_key",
    )
    monthly = _ask(pkg, {"select": ["metric.shop.total_amount"], "time": {"grain": "month"}})
    assert [(row[0], row[-1]) for row in monthly] == _gold(pkg, AMOUNT_BY_MONTH)
    refunds = _ask(
        pkg,
        {
            "select": ["measure.shop.refund_amount"],
            "time": {"temporal_role": ROLE, "grain": "month"},
        },
    )
    assert [(row[0], row[-1]) for row in refunds] == _gold(pkg, REFUNDS_BY_MONTH)


CLOCK = [
    {
        "entity": "day",
        "relation": "calendar",
        "key": "day_key",
        "date_column": "day",
        "temporal_role": ROLE,
    }
]


def test_two_day_keys_are_a_choice_unless_the_package_links_one(tmp_path):
    """No name settles which of a sale's day keys dates it: the draft dates no measure and lists
    both. Where the package's model already links the sale day, that one dates the measures and
    the ship day is a named role to add."""
    with open_duckdb(_warehouse(tmp_path)) as warehouse:
        sales = suggest_model(warehouse, "sales", clocks=CLOCK)
        linked = suggest_model(
            warehouse, "sales", clocks=CLOCK, clock_links={"day": "sale_day_key"}
        )
    roles = sales["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["sale_day_key", "ship_day_key"])
    assert roles["message"] == (
        "sales has no date column of its own and 2 columns holding keys of day (sale_day_key, "
        "ship_day_key), so which one dates its rows is a business choice, and the draft dates no "
        "measure. Choose one of `options`: add its `entities` to the draft and its `times` to "
        "each measure; then add each other one as a named role (upsert_relationship with a name)."
    )
    assert roles["options"] == [
        {"column": "sale_day_key", "entities": {"day": {"expr": "sale_day_key"}}, "times": [ROLE]},
        {"column": "ship_day_key", "entities": {"day": {"expr": "ship_day_key"}}, "times": [ROLE]},
    ]
    assert "entities" not in sales["upsert_model"]
    assert all("times" not in spec for spec in sales["upsert_model"]["measures"].values())
    chosen = linked["time_roles"]
    assert (chosen["status"], chosen["default"]) == ("default", "sale_day_key")
    assert chosen["message"] == (
        "sales has no date column of its own and 2 columns holding keys of day (sale_day_key, "
        "ship_day_key). The draft dates each measure by sale_day_key, which the package's model "
        "already links: it links sale_day_key (entities.day) and lists temporal_role.shop_day_day "
        "in each measure's `times:`. ship_day_key stays unlinked here: each of `other_keys` adds "
        "one as a named role and keeps sale_day_key the default route."
    )
    (other,) = chosen["other_keys"]
    assert other["upsert_relationship"] == {
        "from_entity": "sale",
        "to_entity": "day",
        "columns": ["ship_day_key"],
        "cardinality": "many_to_one",
        "name": "sale_ship_day",
        "keep_existing_routes": True,
    }
    draft = linked["upsert_model"]
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
        "(booked_day_key, dispatched_day_key), so which one dates its rows is a business choice, "
        "and the draft dates no measure. Choose one of `options`: add its `entities` to the "
        "draft and its `times` to each measure; then add each other one as a named role "
        "(upsert_relationship with a name)."
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


def test_own_dates_are_drafted_as_before(tmp_path):
    """Of several own dates the first is the default, as it always was, whatever the names say:
    no rule of names picks a booking's cancellation, a shipment's delivery or a payment's due
    date over the event the row records, and there is no time_roles block."""
    path = _warehouse(tmp_path)
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE bookings AS SELECT i AS booking_id,
              TIMESTAMP '2025-01-01 10:00:00' + to_days(i::INTEGER) AS booked_at,
              TIMESTAMP '2025-02-01 10:00:00' + to_days(i::INTEGER) AS booking_cancelled_at,
              10.0 * i AS amount FROM range(1, 30) t(i);
            CREATE TABLE order_payments AS SELECT i AS payment_id,
              TIMESTAMP '2025-01-05 10:00:00' + to_days(i::INTEGER) AS paid_at,
              TIMESTAMP '2025-01-02 10:00:00' + to_days(i::INTEGER) AS payment_due_at,
              10.0 * i AS amount FROM range(1, 30) t(i);
            CREATE TABLE deliveries AS SELECT i AS shipment_id,
              TIMESTAMP '2025-01-03 10:00:00' + to_days(i::INTEGER) AS shipped_at,
              TIMESTAMP '2025-01-06 10:00:00' + to_days(i::INTEGER) AS shipment_delivered_at,
              10.0 * i AS charge FROM range(1, 30) t(i);
            ALTER TABLE bookings ADD PRIMARY KEY (booking_id);
            ALTER TABLE order_payments ADD PRIMARY KEY (payment_id);
            ALTER TABLE deliveries ADD PRIMARY KEY (shipment_id);
            """
        )
    defaults = {
        "orders": "order_date",
        "parcels": "booked_on",
        "bookings": "booked_at",
        "order_payments": "paid_at",
        "deliveries": "shipped_at",
    }
    with open_duckdb(path) as warehouse:
        drafted = {relation: suggest_model(warehouse, relation) for relation in defaults}
    for relation, column in defaults.items():
        assert "time_roles" not in drafted[relation]
        times = drafted[relation]["upsert_model"]["times"]
        assert [name for name, spec in times.items() if spec.get("default")] == [column]


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
        "them, add `times: [temporal_role.shop_day_day]` to measure.shop.amount in model sales; "
        "or drop `time` from the query."
    )
    declare = {"model": "sales", "measure": "measure.shop.amount", "times": [ROLE]}
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
        "measure.shop.amount in model sales, then query it by that role."
    )


def test_validation_and_the_promotion_check_name_the_undated_measure(tmp_path):
    pkg = _untimed(tmp_path)
    ref = PackageReference(source_path=str(pkg))
    report, _ = parse_config_report(ref)
    (warning,) = [row for row in report["warnings"] if row["code"] == "MEASURE_WITHOUT_TIME_ROLE"]
    assert warning["message"].endswith(
        "measure.shop.amount has no time role, so it cannot be asked by month or in a window, "
        "although its rows reach temporal_role.shop_day_day. If it dates its rows, add `times: "
        "[temporal_role.shop_day_day]` to measure.shop.amount in model sales."
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


def test_a_clock_reached_only_against_a_links_direction_is_no_reach():
    """A link a query may walk only in reverse takes a sale's rows to no day."""
    rel = SimpleNamespace(
        source_entity="entity.sale",
        target_entity="entity.day",
        cardinality="N:1",
        allowed_directions=["reverse"],
    )
    config = SimpleNamespace(relationships=[rel])
    assert _to_one_reach(config, "entity.sale") == {"entity.sale"}
    rel.allowed_directions = ["forward", "reverse"]
    assert _to_one_reach(config, "entity.sale") == {"entity.sale", "entity.day"}


def test_another_entity_s_own_date_dates_no_row_linked_to_it(tmp_path):
    """A store's opening day and a customer's signup day are clocks of those entities, not
    calendars: a clerk is not dated by their store's opening, an invoice not by its customer's
    signup, and validation suggests neither."""
    seed = """
    CREATE TABLE calendar AS SELECT i::INTEGER AS day_key, DATE '2025-01-01' + (i - 1)::INTEGER
      AS day FROM range(1, 91) t(i);
    CREATE TABLE stores (store_id INTEGER, store_name VARCHAR, opened_on DATE);
    INSERT INTO stores VALUES (1, 'North', DATE '2025-01-05'), (2, 'South', DATE '2025-02-10'),
      (3, 'East', DATE '2025-03-01');
    CREATE TABLE customers AS SELECT i::INTEGER AS customer_id, 'C' || i AS customer_name,
      TIMESTAMP '2025-01-01 09:00:00' + to_hours(i * 7) AS created_at FROM range(1, 31) t(i);
    CREATE TABLE clerks AS SELECT i::INTEGER AS clerk_id, 'K' || i AS clerk_name,
      (1 + i % 3)::INTEGER AS store_id FROM range(1, 13) t(i);
    CREATE TABLE invoices AS SELECT i::INTEGER AS invoice_id, (1 + i % 30)::INTEGER AS customer_id,
      (10.0 + i)::DOUBLE AS amount FROM range(1, 61) t(i);
    """
    pkg, log = _build(
        tmp_path,
        seed=seed,
        models=("stores", "customers", "clerks", "invoices"),
        links=("clerks", "invoices"),
    )
    for relation in ("clerks", "invoices"):
        drafted = next(row for row in log if row.get("suggest_model") == relation)
        assert "time_roles" not in drafted
        assert "entities" not in drafted["upsert_model"]
        assert all("times" not in spec for spec in drafted["upsert_model"]["measures"].values())
    links = [row for row in log if "suggest_links" in row]
    assert [len(row["proposals"]) for row in links] == [1, 1]
    report, _ = parse_config_report(PackageReference(source_path=str(pkg)))
    assert [row for row in report["warnings"] if row["code"] == "MEASURE_WITHOUT_TIME_ROLE"] == []


def test_a_versioned_lookup_s_date_is_no_calendar_beside_the_linked_day(tmp_path):
    """A consignment links its booked day and a cargo kind version whose model is dated by
    valid_from: only the calendar's key dates it, the one its model already links."""
    path = tmp_path / "freight.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE kinds (kind_key INTEGER, kind_code VARCHAR, valid_from DATE);
            INSERT INTO kinds VALUES (1, 'A', DATE '2024-01-01'), (2, 'A', DATE '2025-02-01'),
              (3, 'B', DATE '2024-06-01');
            CREATE TABLE consignments AS SELECT i::INTEGER AS consignment_id,
              (1 + i % 80)::INTEGER AS booked_day_key, (2 + i % 80)::INTEGER AS dispatched_day_key,
              (1 + i % 3)::INTEGER AS kind_key, (5.0 * i)::DOUBLE AS charge FROM range(1, 41) t(i);
            """
        )
    kind = {
        "entity": "kind",
        "relation": "kinds",
        "key": "kind_key",
        "date_column": "valid_from",
        "temporal_role": "temporal_role.shop_kind_valid_from",
    }
    with open_duckdb(path) as warehouse:
        drafted = suggest_model(
            warehouse,
            "consignments",
            clocks=[*CLOCK, kind],
            clock_links={"day": "booked_day_key", "kind": "kind_key"},
        )
    roles = drafted["time_roles"]
    assert (roles["status"], roles["default"]) == ("default", "booked_day_key")
    assert roles["candidates"] == ["booked_day_key", "dispatched_day_key"]
    assert all(spec["times"] == [ROLE] for spec in drafted["upsert_model"]["measures"].values())


def test_a_calendar_key_beside_an_own_date_is_a_choice(tmp_path):
    """No name settles whether an order is dated by its order day key or its shipped_at, nor a
    booking by its booking_date or its ship day key: each draft marks no date `default: true`,
    dates no measure and lists the options."""
    path = tmp_path / "orders.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE fct_orders AS SELECT i AS order_id, (1 + i % 60)::INTEGER AS order_day_key,
              TIMESTAMP '2025-02-01 10:00:00' + to_days(i::INTEGER) AS shipped_at,
              5.0 * i AS amount FROM range(1, 30) t(i);
            CREATE TABLE bookings AS SELECT order_id AS booking_id,
              DATE '2025-01-01' + order_id::INTEGER AS booking_date,
              order_day_key AS ship_day_key, amount FROM fct_orders;
            """
        )
    with open_duckdb(path) as warehouse:
        orders = suggest_model(warehouse, "fct_orders", clocks=CLOCK)
        bookings = suggest_model(warehouse, "bookings", clocks=CLOCK)
    roles = orders["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["shipped_at", "order_day_key"])
    assert roles["message"] == (
        "fct_orders has a date column of its own (shipped_at) and a column holding keys of day "
        "(order_day_key), so which one dates its rows is a business choice, and the draft dates "
        "no measure. Mark the date the business reads them by `default: true` in the draft's "
        "times, or choose one of `options`: add its `entities` to the draft and its `times` to "
        "each measure."
    )
    assert roles["options"] == [
        {"column": "order_day_key", "entities": {"day": {"expr": "order_day_key"}}, "times": [ROLE]}
    ]
    for drafted in (orders, bookings):
        draft = drafted["upsert_model"]
        assert drafted["time_roles"]["status"] == "choose"
        assert "entities" not in draft
        assert all("default" not in spec for spec in draft["times"].values())
        assert all("times" not in spec for spec in draft["measures"].values())


def _calendar_with(tmp_path: Path, unknown: str) -> Path:
    path = tmp_path / "dim.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(SEED)
        conn.execute(f"INSERT INTO calendar VALUES {unknown}")
    return path


@pytest.mark.parametrize(
    "unknown",
    ["(-1, NULL)", "(-1, DATE '1900-01-01')", "(0, DATE '2024-06-30')"],
    ids=["null-date", "date-1900", "key-zero"],
)
def test_a_calendar_with_an_unknown_member_row_still_dates_by_its_key(tmp_path, unknown):
    """A date dimension's unknown member (-1 with no date, or 1900-01-01) is no day: the
    calendar still reads one row per day, and a return is dated by its day key."""
    with open_duckdb(_calendar_with(tmp_path, unknown)) as warehouse:
        returns = suggest_model(warehouse, "returns", clocks=CLOCK)
    assert (returns["time_roles"]["status"], returns["time_roles"]["default"]) == (
        "default",
        "return_day_key",
    )
    assert all(spec["times"] == [ROLE] for spec in returns["upsert_model"]["measures"].values())


def test_a_daily_exchange_rate_is_no_calendar(tmp_path):
    """A daily exchange rate reads one row per day, but it is no calendar: an invoice that names
    the rate it was booked at is dated by its day key, and one with no day key by nothing."""
    path = tmp_path / "fx.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE fx_rates AS SELECT i::INTEGER AS fx_rate_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS rate_date, 1.1::DOUBLE AS rate
              FROM range(1, 91) t(i);
            CREATE TABLE invoices AS SELECT i AS invoice_id,
              (30 + i)::INTEGER AS invoice_day_key, i::INTEGER AS fx_rate_key,
              10.0 * i AS amount FROM range(1, 40) t(i);
            CREATE TABLE rated_invoices AS SELECT invoice_id, fx_rate_key, 'March' AS invoice_month,
              amount FROM invoices;
            """
        )
    fx = {
        "entity": "fx_rate",
        "relation": "fx_rates",
        "key": "fx_rate_key",
        "date_column": "rate_date",
        "temporal_role": "temporal_role.shop_fx_rate_rate_date",
    }
    with open_duckdb(path) as warehouse:
        invoices = suggest_model(warehouse, "invoices", clocks=[*CLOCK, fx])
        rated = suggest_model(warehouse, "rated_invoices", clocks=[*CLOCK, fx])
    roles = invoices["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("default", ["invoice_day_key"])
    assert all(spec["times"] == [ROLE] for spec in invoices["upsert_model"]["measures"].values())
    assert "time_roles" not in rated
    assert "entities" not in rated["upsert_model"]
    assert all("times" not in spec for spec in rated["upsert_model"]["measures"].values())


def test_a_key_the_calendar_lacks_or_a_snapshot_day_dates_nothing_alone(tmp_path):
    """A due day key that the calendar mostly lacks, or a stock snapshot's day key, dates no
    measure by itself: beside another key, the choice is stated; alone, the draft is as on
    main, with no time."""
    path = tmp_path / "doubt.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 60) t(i);
            CREATE TABLE invoices AS SELECT i AS invoice_id, 15 AS invoice_day_key,
              CASE WHEN i = 20 THEN 66 ELSE 59 END AS due_day_key, 10.0 AS amount
              FROM range(1, 21) t(i);
            CREATE TABLE inventory_snapshots AS SELECT i AS snapshot_id, 1 + i % 3 AS product_key,
              1 + i % 59 AS snapshot_day_key, 10.0 + i % 7 AS quantity FROM range(1, 60) t(i);
            CREATE TABLE stock_counts AS SELECT snapshot_id AS count_id, snapshot_day_key,
              1 + snapshot_day_key % 50 AS counted_day_key, quantity FROM inventory_snapshots;
            """
        )
    with open_duckdb(path) as warehouse:
        drafted = {
            relation: suggest_model(warehouse, relation, clocks=CLOCK)
            for relation in ("invoices", "inventory_snapshots", "stock_counts")
        }
    for relation, candidates in (
        ("invoices", ["invoice_day_key", "due_day_key"]),
        ("stock_counts", ["snapshot_day_key", "counted_day_key"]),
    ):
        roles = drafted[relation]["time_roles"]
        assert (roles["status"], roles["candidates"]) == ("choose", candidates)
    assert "time_roles" not in drafted["inventory_snapshots"]
    for relation in drafted:
        draft = drafted[relation]["upsert_model"]
        assert "entities" not in draft
        assert all("times" not in spec for spec in draft["measures"].values())


def test_a_date_that_is_also_a_calendar_key_is_one_candidate(tmp_path):
    """A calendar keyed by its date: an order's order_date is its own date and that key at once,
    so it stays the one default, with no choice to state."""
    path = tmp_path / "dates.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar_dates AS SELECT DATE '2025-01-01' + i::INTEGER AS date
            FROM range(0, 90) t(i);
            CREATE TABLE orders AS SELECT i AS order_id, DATE '2025-01-01' + i::INTEGER
              AS order_date, 1.0 * i AS amount FROM range(1, 40) t(i);
            ALTER TABLE orders ADD PRIMARY KEY (order_id);
            """
        )
    clock = {
        "entity": "day",
        "relation": "calendar_dates",
        "key": "date",
        "date_column": "date",
        "temporal_role": ROLE,
    }
    with open_duckdb(path) as warehouse:
        orders = suggest_model(warehouse, "orders", clocks=[clock])
    assert "time_roles" not in orders
    assert orders["upsert_model"]["times"]["order_date"]["default"] is True


def test_a_date_key_named_unlike_the_calendar_s_key_still_makes_the_choice(tmp_path):
    """A sale's ship_date_key is not named like the calendar's day_key, but it holds its keys:
    beside the order day key the choice is stated, and alone it dates nothing."""
    path = tmp_path / "named.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE sales AS SELECT i AS sale_id, (1 + i % 30)::INTEGER AS order_day_key,
              (32 + i % 30)::INTEGER AS ship_date_key, 10.0 * i AS amount FROM range(1, 40) t(i);
            CREATE TABLE shipped AS SELECT sale_id, ship_date_key, amount FROM sales;
            """
        )
    with open_duckdb(path) as warehouse:
        sales = suggest_model(warehouse, "sales", clocks=CLOCK)
        shipped = suggest_model(warehouse, "shipped", clocks=CLOCK)
    roles = sales["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["order_day_key", "ship_date_key"])
    for drafted in (sales, shipped):
        draft = drafted["upsert_model"]
        assert "entities" not in draft
        assert all("times" not in spec for spec in draft["measures"].values())
    assert "time_roles" not in shipped


def test_a_day_key_in_the_row_s_key_dates_nothing_and_states_the_choice(tmp_path):
    """A periodic snapshot keyed by day and product (or account), whatever its table is called:
    a sum of its stock by month would count every day, so its day key dates nothing and the
    choice is stated. An order's day key, which is no part of its key, still dates it."""
    path = tmp_path / "periodic.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE fct_inventory AS SELECT (1 + i // 3)::INTEGER AS day_key,
              1 + i % 3 AS product_key, 100.0 + i % 3 AS quantity_on_hand FROM range(0, 180) t(i);
            CREATE TABLE fct_daily_account_balances (day_key INTEGER, account_key INTEGER,
              balance DOUBLE, PRIMARY KEY (day_key, account_key));
            INSERT INTO fct_daily_account_balances SELECT 1 + i // 2, 1 + i % 2, 1000.0 + i % 2
              FROM range(0, 120) t(i);
            CREATE TABLE inventory_snapshots AS SELECT (1 + i)::INTEGER AS day_key,
              10.0 + i % 7 AS quantity FROM range(0, 59) t(i);
            CREATE TABLE fct_orders AS SELECT i AS order_id, (1 + i % 60)::INTEGER AS day_key,
              10.0 * i AS amount FROM range(1, 40) t(i);
            """
        )
    with open_duckdb(path) as warehouse:
        drafted = {
            relation: suggest_model(warehouse, relation, clocks=CLOCK)
            for relation in (
                "fct_inventory",
                "fct_daily_account_balances",
                "inventory_snapshots",
                "fct_orders",
            )
        }
    inventory = drafted["fct_inventory"]
    assert inventory["primary_key"]["columns"] == ["day_key", "product_key"]
    assert inventory["time_roles"] == {
        "status": "choose",
        "candidates": ["day_key"],
        "message": (
            "fct_inventory has no date column of its own; day_key holds keys of day and is part "
            "of its key (day_key, product_key), so it has one row per product_key and day. A "
            "measure of it is either a stock (on hand, a balance), which a sum over a longer "
            "period counts once per day, or a total per day; which one is a business choice, and "
            "the draft dates no measure. To date a total per day, choose one of `options`: add "
            "its `entities` to the draft and its `times` to each measure."
        ),
        "options": [{"column": "day_key", "entities": {"day": {}}, "times": [ROLE]}],
    }
    balances = drafted["fct_daily_account_balances"]["time_roles"]
    assert (balances["status"], balances["candidates"]) == ("choose", ["day_key"])
    assert "one row per account_key and day" in balances["message"]
    snapshots = drafted["inventory_snapshots"]["time_roles"]
    assert (snapshots["status"], snapshots["candidates"]) == ("choose", ["day_key"])
    assert "so it has one row per day. A measure" in snapshots["message"]
    for relation in ("fct_inventory", "fct_daily_account_balances", "inventory_snapshots"):
        draft = drafted[relation]["upsert_model"]
        assert "entities" not in draft
        assert all("times" not in spec for spec in draft["measures"].values())
    orders = drafted["fct_orders"]
    assert (orders["time_roles"]["status"], orders["time_roles"]["default"]) == (
        "default",
        "day_key",
    )
    assert all(spec["times"] == [ROLE] for spec in orders["upsert_model"]["measures"].values())


def test_a_day_key_in_the_row_s_key_wins_over_a_linked_second_key(tmp_path):
    """A daily inventory keyed by day and product with a second day key the package already
    links: the row is still one per product per day, so neither key dates its stock. A key in
    the row's key that the calendar doesn't hold (yyyymmdd against a 1..N calendar) is no key
    of it, and a monthly calendar's key in the row's key names months, not days."""
    path = tmp_path / "periodic.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE months AS SELECT i::INTEGER AS month_key,
              DATE '2025-01-01' + to_months((i - 1)::INTEGER) AS month_start FROM range(1, 13) t(i);
            CREATE TABLE fct_inventory (day_key INTEGER, product_key INTEGER,
              reported_day_key INTEGER, quantity_on_hand DOUBLE,
              PRIMARY KEY (day_key, product_key));
            INSERT INTO fct_inventory SELECT 1 + i // 2, 1 + i % 2, 1 + i // 2, 100.0
              FROM range(0, 62) t(i);
            CREATE TABLE fct_stock AS SELECT day_key AS date_key, product_key, reported_day_key,
              quantity_on_hand FROM fct_inventory;
            ALTER TABLE fct_stock ADD PRIMARY KEY (date_key, product_key);
            CREATE TABLE fct_balances (day_key INTEGER, account_key INTEGER, balance DOUBLE,
              PRIMARY KEY (day_key, account_key));
            INSERT INTO fct_balances SELECT 20250101 + i // 2, 1 + i % 2, 100.0
              FROM range(0, 62) t(i);
            CREATE TABLE fct_monthly_balances (month_key INTEGER, account_key INTEGER,
              balance DOUBLE, PRIMARY KEY (month_key, account_key));
            INSERT INTO fct_monthly_balances SELECT 1 + i // 2, 1 + i % 2, 100.0
              FROM range(0, 24) t(i);
            """
        )
    month = {
        "entity": "month",
        "relation": "months",
        "key": "month_key",
        "date_column": "month_start",
        "temporal_role": "temporal_role.shop_month_month_start",
        "calendar": True,
    }
    with open_duckdb(path) as warehouse:
        inventory = suggest_model(
            warehouse, "fct_inventory", clocks=CLOCK, clock_links={"day": "reported_day_key"}
        )
        # Named unlike the calendar's day_key, a date_key in the row's key holds its keys all the
        # same.
        stock = suggest_model(
            warehouse, "fct_stock", clocks=CLOCK, clock_links={"day": "reported_day_key"}
        )
        balances = suggest_model(warehouse, "fct_balances", clocks=CLOCK)
        monthly = suggest_model(warehouse, "fct_monthly_balances", clocks=[month])
    roles = inventory["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["day_key", "reported_day_key"])
    assert roles["message"].startswith(
        "fct_inventory has no date column of its own; day_key holds keys of day and is part of "
        "its key (day_key, product_key), so it has one row per product_key and day."
    )
    assert stock["time_roles"]["status"] == "choose"
    assert stock["time_roles"]["message"].startswith(
        "fct_stock has no date column of its own; date_key holds keys of day and is part of "
        "its key (date_key, product_key)"
    )
    for drafted in (inventory, stock, balances, monthly):
        draft = drafted["upsert_model"]
        assert "entities" not in draft
        assert all("times" not in spec for spec in draft["measures"].values())
    assert "time_roles" not in balances
    by_month = monthly["time_roles"]
    assert by_month["status"] == "choose"
    assert (
        "one row per account_key and month. A measure of it is either a stock"
        in (by_month["message"])
    )
    assert "counts once per month, or a total per month;" in by_month["message"]


def test_a_doubtful_key_beside_an_own_date_is_a_choice(tmp_path):
    """A stock snapshot with its own recorded_at and the snapshot's day key, and a booking with its
    own booked_on beside a snapshot day key: which one dates the rows is the author's choice,
    and neither draft marks a time `default: true`."""
    path = tmp_path / "counts.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar AS SELECT i::INTEGER AS day_key,
              DATE '2025-01-01' + (i - 1)::INTEGER AS day FROM range(1, 91) t(i);
            CREATE TABLE inventory_snapshots AS SELECT 1 + i % 2 AS product_id,
              (1 + i // 2)::INTEGER AS day_key,
              TIMESTAMP '2025-01-02 00:05:00' + to_minutes(i::INTEGER) AS recorded_at,
              10.0 + i % 7 AS quantity FROM range(0, 120) t(i);
            ALTER TABLE inventory_snapshots ADD PRIMARY KEY (product_id, day_key);
            CREATE TABLE bookings AS SELECT i AS booking_id,
              DATE '2025-01-01' + i::INTEGER AS booked_on,
              (1 + i % 60)::INTEGER AS snapshot_day_key, 10.0 * i AS amount FROM range(1, 40) t(i);
            """
        )
    with open_duckdb(path) as warehouse:
        drafted = {
            relation: suggest_model(warehouse, relation, clocks=CLOCK)
            for relation in ("inventory_snapshots", "bookings")
        }
    for relation, own in (("inventory_snapshots", "recorded_at"), ("bookings", "booked_on")):
        roles = drafted[relation]["time_roles"]
        assert roles["status"] == "choose"
        assert roles["candidates"][0] == own
        draft = drafted[relation]["upsert_model"]
        assert all("default" not in spec for spec in draft["times"].values())
        assert all("times" not in spec for spec in draft["measures"].values())


def test_a_day_named_foreign_key_of_another_table_is_no_date(tmp_path):
    """A sale's day_part_code is declared a key of day_parts: it is no second date, so the
    order day key alone dates the sale."""
    path = tmp_path / "parts.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE calendar (day_key INTEGER PRIMARY KEY, day DATE);
            INSERT INTO calendar SELECT i, DATE '2025-01-01' + (i - 1)::INTEGER
              FROM range(1, 91) t(i);
            CREATE TABLE day_parts (day_part_code INTEGER PRIMARY KEY, label VARCHAR);
            INSERT INTO day_parts VALUES (1, 'morning'), (2, 'evening');
            CREATE TABLE sales (sale_id INTEGER PRIMARY KEY,
              order_day_key INTEGER REFERENCES calendar (day_key),
              day_part_code INTEGER REFERENCES day_parts (day_part_code), amount DOUBLE);
            INSERT INTO sales SELECT i, 1 + i % 60, 1 + i % 2, 10.0 * i FROM range(1, 40) t(i);
            """
        )
    with open_duckdb(path) as warehouse:
        sales = suggest_model(warehouse, "sales", clocks=CLOCK)
    roles = sales["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("default", ["order_day_key"])
    assert all(spec["times"] == [ROLE] for spec in sales["upsert_model"]["measures"].values())
