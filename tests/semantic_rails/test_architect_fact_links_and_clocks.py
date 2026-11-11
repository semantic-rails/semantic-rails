"""A package built with the Architect answers by month and across two facts.

Two facts that share a business key of two columns (a return names the sale line it returns by
order and line number) are linked by ``suggest_links``, with what makes the link safe said in
numbers: the key is unique on the sale side, how many returns find their sale, and on which
columns. A fact that keeps its date only as one key into a calendar gets its measures dated by
``suggest_model``; with several such keys, or one beside a date of its own, the draft dates none
and says the choice is the author's, and a table's own dates are drafted as before. A measure
left without a time role is refused by month with the package line that fixes it, and validation
names it.

Every answer is checked against hand-written SQL over the same DuckDB file.
"""

from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import duckdb
import pytest
import yaml
from mcp.shared.memory import create_connected_server_and_client_session

from semantic_rails.architect_introspection import (
    _composite_candidates,
    open_duckdb,
    suggest_links,
    suggest_model,
)
from semantic_rails.architect_mcp import create_architect_mcp_server
from semantic_rails.architect_service import ArchitectProject, _reads_one_row
from semantic_rails.cli.scaffold import create_project_report
from semantic_rails.config_validation import PackageReference, parse_config_report
from semantic_rails.diagnostics import recovery_hints_for_error
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import promote_package_report
from semantic_rails.route_census import resolve_pairs
from semantic_rails.runtime import Runtime
from semantic_rails.untimed_measures import _to_one_reach

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


def _through_the_link(
    refused: dict[str, Any], beyond: list[dict[str, Any]], relation: str, link: dict[str, Any]
) -> list[dict[str, Any]]:
    """What the author records once ``prefer_this_link`` was refused for routes beyond its pair
    and every route was kept: the pair both ways by the link, and each of those routes with the
    pair's earlier route on its way replaced by the link."""
    rel = f"relationship.{relation}_{link['to_entity']}"
    ends = (f"entity.shop_{link['from_entity']}", f"entity.shop_{link['to_entity']}")
    earlier = {
        (row["source_entity"], row["target_entity"]): row["base"].get("relationship_path")
        for row in refused["details"]["route_changes"]
    }
    legs = [earlier[pair] for pair in (ends, ends[::-1]) if earlier.get(pair)]

    def through(path: list[str]) -> list[str]:
        for leg in legs:
            for at in range(len(path) - len(leg) + 1):
                if path[at : at + len(leg)] == leg:
                    return [*path[:at], rel, *path[at + len(leg) :]]
        return path

    pair = [
        {"source_entity": source, "target_entity": target, "relationship_path": [rel]}
        for source, target in (ends, ends[::-1])
    ]
    return pair + [
        {
            "source_entity": row["source_entity"],
            "target_entity": row["target_entity"],
            "relationship_path": through(row["base"]["relationship_path"]),
        }
        for row in beyond
    ]


def _build(
    tmp_path: Path,
    *,
    prefer: bool = True,
    seed: str = SEED,
    models: tuple[str, ...] = ("customers", "returns", "sales"),
    links: tuple[str, ...] = ("returns", "sales"),
    dates: dict[str, str] | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    """A blind Architect build: create, explore, suggest, write what was suggested, validate.
    Where ``suggest_model`` states a choice of date, the author picks ``dates[relation]`` (a sale
    is dated by its sale day). A refused link is retried as its refusal says first; where
    ``prefer_this_link`` is refused for routes beyond the pair, the author keeps every route and
    records the pair and those routes through the link, as that refusal says. Returns the
    package and the log."""
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
                link = row["upsert_relationship"]
                written = first = await s.write(session, "upsert_relationship", **link)
                if not written["ok"]:
                    log.append({"refused": written["error"]})
                    message = written["error"]["message"]
                    retry = (
                        "prefer_this_link"
                        if prefer and "prefer_this_link=true" in message
                        else "keep_existing_routes"
                    )
                    written = await s.write(session, "upsert_relationship", **link, **{retry: True})
                if not written["ok"]:
                    log.append({"refused": written["error"]})
                    beyond = written["error"]["details"]["route_changes"]
                    written = await s.write(
                        session, "upsert_relationship", **link, keep_existing_routes=True
                    )
                    assert written["ok"], written
                    written = await s.write(
                        session,
                        "record_route_decision",
                        decisions=_through_the_link(first["error"], beyond, relation, link),
                    )
                    log.append({"recorded": written})
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


def test_a_blind_build_answers_by_month_and_across_the_two_facts(tmp_path):
    """The sale's two day keys are a choice its draft states; dated by its sale day as the
    author picks, and the return linked to its sale line, sales by month and refunds by the
    customer of the returned sale equal the SQL, the return no sale holds in the NULL group."""
    pkg, log = _build(tmp_path)
    sales = next(row for row in log if row.get("suggest_model") == "sales")
    roles = sales["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["sale_day_key", "ship_day_key"])
    assert "entities" not in sales["upsert_model"]
    assert all("times" not in spec for spec in sales["upsert_model"]["measures"].values())
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
        "so each returns row matches at most one sales row; matched on sale_order_id = order_id and "
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
            source_key=["claim_order_id", "claim_line_no"],
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


def test_two_keys_whose_names_read_alike_get_names_of_their_own(tmp_path):
    """day_id and day_key both read as the day: named after their columns instead, neither
    draft replaces the other."""
    path = tmp_path / "events.duckdb"
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            "CREATE TABLE calendar AS SELECT * FROM (VALUES (1), (2)) t(id);"
            "CREATE TABLE events AS SELECT * FROM (VALUES (1, 1, 2, 10), (2, 2, 1, 20)) "
            "t(event_id, day_id, day_key, amount);"
        )
    with open_duckdb(path) as warehouse:
        links = suggest_links(
            warehouse,
            "events",
            targets=[{"relation": "calendar", "key": "id", "entity": "day"}],
            from_entity="event",
        )
    names = {row["column"]: row["upsert_relationship"]["name"] for row in links["proposals"]}
    assert names == {"day_id": "event_day_id", "day_key": "event_day_key"}


def test_a_wide_key_no_columns_can_fill_is_given_up_at_once():
    """Ten columns can't fill an eleven-column key with each used once: no candidates, and no
    walk through every combination to find that out."""
    columns = [f"c{i}" for i in range(10)]
    keys = [f"k{i}" for i in range(11)]
    types = dict.fromkeys(columns, "INTEGER")
    found = _composite_candidates(
        types, keys, dict.fromkeys(keys, "INTEGER"), columns, time.monotonic() + 1
    )
    assert found == []


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


def _chain(tmp_path: Path, *, with_d: bool) -> ArchitectProject:
    """Entities (d ->) a -> x -> b, each by a many-to-one link."""
    report = create_project_report(package_id="p", workspace_root=str(tmp_path), run_checks=False)
    project = ArchitectProject(report["project_path"], workspace_root=tmp_path)
    for name in ("d", "a", "x", "b") if with_d else ("a", "x", "b"):
        project.upsert_model(
            model_id=f"{name}s", entity_key=name, relation=f"raw_{name}", primary_key=[f"{name}_id"]
        )
    if with_d:
        project.upsert_relationship(from_entity="d", to_entity="a", columns=["a_id"])
    project.upsert_relationship(from_entity="a", to_entity="x", columns=["x_id"])
    project.upsert_relationship(from_entity="x", to_entity="b", columns=["b_id"])
    return project


def _route(project: ArchitectProject, source: str, target: str) -> list[str]:
    _, config = parse_config_report(PackageReference(source_path=str(project.project_path)))
    pair = (f"entity.p_{source}", f"entity.p_{target}")
    return list(resolve_pairs(config, [pair])[pair].shape()["relationship_path"])


@pytest.mark.parametrize("column", ["direct_b_id", "b_id"])
def test_preferring_a_link_never_moves_a_route_that_answers_beyond_its_pair(tmp_path, column):
    """d reaches b through a and x by to-one links and answers today. Preferring a new a -> b
    link (on its own column, or on one the x -> b link also names) would move that route too:
    refused, naming it, and nothing written."""
    project = _chain(tmp_path, with_d=True)
    with pytest.raises(SemanticLayerError) as refused:
        project.upsert_relationship(
            from_entity="a", to_entity="b", columns=[column], prefer_this_link=True
        )
    assert refused.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert str(refused.value).startswith(
        "prefer_this_link would also move routes beyond the two entities that answer today: "
        "entity.p_b -> entity.p_d (relationship.xs_b, relationship.as_x, relationship.ds_a); "
        "entity.p_d -> entity.p_b (relationship.ds_a, relationship.as_x, relationship.xs_b); "
        "nothing was written."
    )
    assert _route(project, "d", "b") == [
        "relationship.ds_a",
        "relationship.as_x",
        "relationship.xs_b",
    ]
    assert _route(project, "a", "b") == ["relationship.as_x", "relationship.xs_b"]


def test_a_preferred_link_replaces_a_recorded_route_for_its_pair(tmp_path):
    """An a -> b route recorded through x gives way to the preferred direct link."""
    project = _chain(tmp_path, with_d=False)
    project.record_route_decision(
        source_entity="a",
        target_entity="b",
        relationship_path=["relationship.as_x", "relationship.xs_b"],
    )
    project.upsert_relationship(
        from_entity="a", to_entity="b", columns=["direct_b_id"], prefer_this_link=True
    )
    assert _route(project, "a", "b") == ["relationship.as_b"]


# The shop again, each return carrying the customer of the sale line it returns; no line is
# returned twice unless ``RETURNED_TWICE`` adds one.
RETURN_CUSTOMERS = (
    SEED
    + """
DELETE FROM returns WHERE return_id = 1000;
ALTER TABLE returns ADD COLUMN customer_key INTEGER;
UPDATE returns SET customer_key = s.customer_key FROM sales s
  WHERE s.order_id = returns.sale_order_id AND s.line_no = returns.sale_line_no;
"""
)
RETURNED_TWICE = """
INSERT INTO returns SELECT 1000, sale_order_id, sale_line_no, return_day_key, 4.0, 'late',
  customer_key FROM returns WHERE return_id = 3;
"""
SALES_BY_CUSTOMER = """
SELECT c.customer_name, sum(s.amount) FROM sales s
LEFT JOIN customers c ON c.customer_key = s.customer_key GROUP BY 1
"""


@pytest.mark.parametrize(
    "order", [("returns", "sales"), ("sales", "returns")], ids=["returns-first", "sales-first"]
)
@pytest.mark.parametrize("twice", [False, True], ids=["once", "twice"])
@pytest.mark.parametrize("day", [True, False], ids=["shared-day", "undated-returns"])
def test_a_build_in_either_table_order_reads_a_sale_s_customer_from_the_sale(
    tmp_path, order, twice, day
):
    """Linked in either table order, the return reaches its customer and its sale line, many to
    one even where no line is returned twice yet, and the sale its own customer. Where a link
    is refused, its retries move no route beyond its pair unless the author records it. Sales
    by customer and refunds by the customer of the returned sale equal the SQL, as on main,
    which links no return to its sale line."""
    seed = RETURN_CUSTOMERS + (RETURNED_TWICE if twice else "")
    if not day:
        seed += "ALTER TABLE returns DROP COLUMN return_day_key;"
    pkg, log = _build(tmp_path, seed=seed, links=order)
    returns = next(row for row in log if row.get("suggest_links") == "returns")
    (to_sale,) = [row for row in returns["proposals"] if row["target"]["entity"] == "sale"]
    assert to_sale["cardinality"] == "many_to_one"
    by_customer = {
        "select": ["measure.shop.amount"],
        "group_by": ["dimension.shop_customer_customer_name"],
    }
    assert _ask(pkg, by_customer) == _gold(pkg, SALES_BY_CUSTOMER)
    refunds = {**by_customer, "select": ["measure.shop.refund_amount"]}
    assert _ask(pkg, refunds) == _gold(pkg, REFUNDS_BY_CUSTOMER)


def test_a_link_beyond_its_pair_is_refused_and_named_until_the_author_records_it(tmp_path):
    """Returns with no day linked first: a sale reaches its customer and its day's customers
    through its returns. Preferring the sale's own customer link would move those routes too,
    so it is refused, naming them, and nothing is written; kept, and recorded through the link
    by the author, sales by customer equal the SQL."""
    seed = RETURN_CUSTOMERS + "ALTER TABLE returns DROP COLUMN return_day_key;"
    pkg, log = _build(tmp_path, seed=seed)
    refused = [row["refused"] for row in log if "refused" in row]
    assert [row["code"] for row in refused] == ["ROUTE_DECISION_NOT_RECORDED"] * 2
    assert refused[0]["message"].startswith(
        "sale and customer reach each other today through other entities' rows"
    )
    assert refused[1]["message"] == (
        "prefer_this_link would also move routes beyond the two entities that answer today: "
        "entity.shop_customer -> entity.shop_day (relationship.returns_customer, "
        "relationship.returns_sale, relationship.sales_day); entity.shop_day -> "
        "entity.shop_customer (relationship.sales_day, relationship.returns_sale, "
        "relationship.returns_customer); nothing was written. Retry with "
        "keep_existing_routes=true to keep every route as it is; then record_route_decision can "
        "take the routes you want through the link."
    )
    (recorded,) = [row["recorded"] for row in log if "recorded" in row]
    assert recorded["ok"]
    by_customer = {
        "select": ["measure.shop.amount"],
        "group_by": ["dimension.shop_customer_customer_name"],
    }
    assert _ask(pkg, by_customer) == _gold(pkg, SALES_BY_CUSTOMER)


def test_a_link_a_recorded_route_holds_off_its_pair_is_refused(tmp_path):
    """a and b each name one x; a route from a to b through x reads every b of an a's x. Kept
    on that route, a plain a -> b link would change no answer: refused with what to do, nothing
    written; preferred, it is the route."""
    report = create_project_report(package_id="p", workspace_root=str(tmp_path), run_checks=False)
    project = ArchitectProject(report["project_path"], workspace_root=tmp_path)
    for name in ("a", "x", "b"):
        project.upsert_model(
            model_id=f"{name}s", entity_key=name, relation=f"raw_{name}", primary_key=[f"{name}_id"]
        )
    project.upsert_relationship(from_entity="a", to_entity="x", columns=["x_id"])
    project.upsert_relationship(from_entity="b", to_entity="x", columns=["x_id"])
    project.record_route_decision(
        source_entity="a",
        target_entity="b",
        relationship_path=["relationship.as_x", "relationship.bs_x"],
    )
    with pytest.raises(SemanticLayerError) as refused:
        project.upsert_relationship(from_entity="a", to_entity="b", columns=["b_id"])
    assert refused.value.code == "ROUTE_DECISION_NOT_RECORDED"
    assert str(refused.value) == (
        "A recorded route holds entity.p_a -> entity.p_b on relationship.as_x, "
        "relationship.bs_x, which reads several rows on its way, so this link would change no "
        "answer between a and b; nothing was written. Retry with prefer_this_link=true to make "
        "the link the route between a and b and keep every other route, or with "
        "keep_existing_routes=true to keep the recorded route."
    )
    assert refused.value.details["recorded_route"] == {
        "source_entity": "entity.p_a",
        "target_entity": "entity.p_b",
        "relationship_path": ["relationship.as_x", "relationship.bs_x"],
    }
    assert _route(project, "a", "b") == ["relationship.as_x", "relationship.bs_x"]
    project.upsert_relationship(
        from_entity="a", to_entity="b", columns=["b_id"], prefer_this_link=True
    )
    assert _route(project, "a", "b") == ["relationship.as_b"]


def test_a_composite_reference_unique_today_is_many_to_one_unless_it_is_the_row_s_key(tmp_path):
    """No sale line is returned twice yet, but a return is not keyed by the line it returns:
    many to one. A claim keyed by its line stays one to one (its own key, as the package says)."""
    path = _warehouse(tmp_path)
    with duckdb.connect(str(path)) as conn:
        conn.execute("CREATE TABLE returns_once AS SELECT * FROM returns WHERE return_id < 1000")
    target = [{"relation": "sales", "key": ["order_id", "line_no"], "entity": "sale"}]
    with open_duckdb(path) as warehouse:
        once = suggest_links(warehouse, "returns_once", targets=target, from_entity="return")
        claims = suggest_links(
            warehouse,
            "claims",
            targets=target,
            from_entity="claim",
            source_key=["claim_order_id", "claim_line_no"],
        )
    (proposal,) = once["proposals"]
    assert proposal["evidence"]["column_unique"]
    assert (proposal["cardinality"], proposal["upsert_relationship"]["cardinality"]) == (
        "many_to_one",
        "many_to_one",
    )
    assert claims["proposals"][0]["cardinality"] == "one_to_one"


def test_a_key_part_is_never_a_column_that_only_starts_like_it(tmp_path):
    """A tuple is proposed only when its columns are named as the key's columns, or all with
    one prefix or suffix around them. A return's line quantity (1 or 2) finds line 1 or 2 of
    every order, a return line's own line number finds a line of the order it names, and a sale
    id of 1 to 3 finds lines 1 to 3: none is proposed as the sale line."""
    path = _warehouse(tmp_path)
    with duckdb.connect(str(path)) as conn:
        conn.execute(
            """
            CREATE TABLE quantity_returns AS
            SELECT return_id, sale_order_id, 'SKU' || sale_line_no AS sku,
                   (1 + return_id % 2)::INTEGER AS line_qty, refund_amount
            FROM returns WHERE return_id < 1000;
            CREATE TABLE return_lines AS
            SELECT sale_order_id AS return_no,
                   (row_number() OVER (PARTITION BY sale_order_id ORDER BY sale_line_no DESC))::INTEGER
                       AS line_no,
                   sale_order_id, sale_line_no, refund_amount
            FROM returns WHERE return_id < 1000;
            ALTER TABLE return_lines ADD PRIMARY KEY (return_no, line_no);
            CREATE TABLE surrogate_lines AS
            SELECT return_id AS return_line_id, return_id, line_no, sale_order_id, sku,
                   refund_amount FROM (
              SELECT return_id, sale_order_id, 'SKU' || sale_line_no AS sku, refund_amount,
                     (row_number() OVER (PARTITION BY sale_order_id ORDER BY return_id))::INTEGER
                         AS line_no
              FROM returns WHERE return_id < 1000);
            ALTER TABLE surrogate_lines ADD PRIMARY KEY (return_line_id);
            CREATE TABLE id_returns AS
            SELECT return_id, sale_order_id, sale_line_no AS sale_id, refund_amount
            FROM returns WHERE return_id < 1000;
            CREATE TABLE line_discounts AS
            SELECT order_id, line_no, 1.0::DOUBLE AS discount FROM sales WHERE order_id % 3 = 0;
            CREATE TABLE line_notes AS
            SELECT order_id AS order_id_noted, line_no AS line_no_noted, 'n' AS note
            FROM sales WHERE order_id % 4 = 0;
            """
        )
    target = [{"relation": "sales", "key": ["order_id", "line_no"], "entity": "sale"}]
    with open_duckdb(path) as warehouse:
        found = {
            relation: suggest_links(warehouse, relation, targets=target, from_entity="row")
            for relation in (
                "quantity_returns",
                "return_lines",
                "surrogate_lines",
                "id_returns",
                "line_discounts",
                "line_notes",
            )
        }
    # A prefixed order beside a bare line number, under a surrogate key or a key of its own.
    for relation in ("quantity_returns", "surrogate_lines", "id_returns"):
        assert found[relation]["proposals"] == found[relation]["below_threshold"] == []
    columns = {
        relation: [row["columns"] for row in found[relation]["proposals"]]
        for relation in ("return_lines", "line_discounts", "line_notes")
    }
    assert columns == {
        "return_lines": [["sale_order_id", "sale_line_no"]],
        "line_discounts": [["order_id", "line_no"]],
        "line_notes": [["order_id_noted", "line_no_noted"]],
    }
    (proposal,) = found["return_lines"]["proposals"]
    assert "choice" not in proposal and "name" not in proposal["upsert_relationship"]


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


def test_a_second_table_of_one_row_per_day_makes_the_date_a_choice(tmp_path):
    """Invoices hold a day key and the key of a daily exchange rate, whose table also reads one
    row per day: no name settles which dates an invoice, so the draft dates none."""
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
    roles = invoices["time_roles"]
    assert (roles["status"], roles["candidates"]) == ("choose", ["invoice_day_key", "fx_rate_key"])
    assert all("times" not in spec for spec in invoices["upsert_model"]["measures"].values())


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


def test_the_reverse_of_a_one_to_many_link_reads_one_row():
    """An invoice reads its one order through the order's one-to-many link to its invoices; an
    order reads several invoices."""
    rel = SimpleNamespace(
        id="relationship.orders_invoices",
        source_entity="entity.order",
        target_entity="entity.invoice",
        cardinality="1:N",
    )
    config = SimpleNamespace(relationships=[rel])
    assert _reads_one_row(config, "entity.invoice", [rel.id])
    assert not _reads_one_row(config, "entity.order", [rel.id])
