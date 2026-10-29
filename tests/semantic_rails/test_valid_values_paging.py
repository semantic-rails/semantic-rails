"""valid-values searches, pages and counts every value a caller may see, from any source.

A live lookup searches, skips and limits in the warehouse, over the values its row filters
leave, after grouping; a declared domain matches the same way in memory. A search the
warehouse can't run as the ``contains`` word is refused before any read.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.dialects import dialect_for_warehouse
from semantic_rails.errors import SemanticLayerError
from semantic_rails.mcp import SemanticLayerMCPAdapter
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.renderer import render_select_for_profile
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig
from semantic_rails.sql_ast import SqlField, SqlIdentifier, SqlOrder, SqlSelect, SqlTableRef

NAME = "dimension.accounts_account_name"
REGION = "dimension.accounts_account_region"


@pytest.fixture
def accounts(tmp_path: Path) -> tuple[Any, Path]:
    """236 accounts, Account 001 to Account 236: the east holds the odd ones, the west the even."""
    path = tmp_path / "package.yml"
    model = {
        "relation": "accounts",
        "entities": {"account": {}},
        "dimensions": {
            "name": {"kind": "categorical", "column": "account_name"},
            "region": {"kind": "categorical"},
        },
        "measures": {"account_count": {"kind": "entity_count", "entity_key": "account_id"}},
    }
    package = {
        "schema_version": 1,
        "package": {
            "id": "accounts",
            "namespace": "accounts",
            "warehouse": "duckdb",
            "default_db": "accounts.duckdb",
            "seed": {"kind": "external"},
        },
        "graph": {"entities": {"account": {"key": "account_id", "model": "accounts"}}},
        "models": {"accounts": model},
    }
    path.write_text(yaml.safe_dump(package, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(tmp_path / "accounts.duckdb")) as db:
        db.execute(
            "CREATE TABLE accounts AS SELECT i AS account_id, "
            "'Account ' || LPAD(CAST(i AS VARCHAR), 3, '0') AS account_name, "
            "CASE WHEN i % 2 = 1 THEN 'east' ELSE 'west' END AS region "
            "FROM generate_series(1, 236) AS g(i)"
        )
    row_filter = SemanticPolicyConfig(
        id="policy.accounts.region",
        kind="row_filter",
        config={"dimension": REGION, "attribute": "region"},
    )
    config = load_package_config(str(path))
    return replace(config, semantic_policies=[row_filter]), path


def _region_values(accounts: tuple[Any, Path], region: str, **arguments: Any) -> dict[str, Any]:
    config, path = accounts
    context = RequestContext(attributes={"region": region}).to_policy_context()
    runtime = Runtime.from_config(config, source_path=str(path))
    try:
        return valid_values_payload(
            runtime,
            dimension_id=NAME,
            query={"policy_context": context},
            allow_live_query=True,
            **arguments,
        )
    finally:
        runtime.close()


def _values(out: dict[str, Any]) -> list[Any]:
    return [row["value"] for row in out["values"]]


def test_a_row_filtered_value_never_matches_or_counts(accounts):
    # Account 235 is the east's 118th name, past a first hundred; Account 236 is the west's.
    found = _region_values(accounts, "east", search="account 235")
    assert (_values(found), found["total_count"], found["has_more"]) == (["Account 235"], 1, False)
    hidden = _region_values(accounts, "east", search="account 236")
    assert (_values(hidden), hidden["total_count"], hidden["has_more"]) == ([], 0, False)
    first, last = (_region_values(accounts, "east", offset=offset) for offset in (0, 100))
    assert _values(first) + _values(last) == [f"Account {i:03d}" for i in range(1, 237, 2)]
    assert (first["has_more"], first["total_count"]) == (True, None)
    assert (last["has_more"], last["total_count"]) == (False, 118)


def test_a_search_matches_each_listed_value_across_a_to_many_hop(runtime_factory):
    # Orders reach item product types across a one-to-many hop: the search keeps the matching
    # value, never every value of an order that holds one.
    runtime = runtime_factory("jaffle_shop")
    try:
        seed = runtime.engine_seed({"select": ["measure.jaffle.order_count"]})
        out = valid_values_payload(
            runtime,
            dimension_id="dimension.jaffle_item_product_type",
            query=seed,
            search="BEV",
            allow_live_query=True,
        )
    finally:
        runtime.close()
    assert out["anchor_measure"] == "measure.jaffle.order_count"
    assert (_values(out), out["total_count"], out["has_more"]) == (["beverage"], 1, False)


def test_a_declared_domain_matches_like_a_live_lookup(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        label = valid_values_payload(
            runtime, dimension_id="dimension.jaffle_item_product_type", search="DRINK"
        )
        wildcard = valid_values_payload(
            runtime, dimension_id="dimension.jaffle_item_product_type", search="_"
        )
    finally:
        runtime.close()
    assert label["source"] == "value_domain"
    assert (_values(label), label["total_count"]) == (["beverage"], 1)  # its label, any case
    # Text, as in the warehouse: no product type holds "_", so none matches.
    assert (_values(wildcard), wildcard["total_count"], wildcard["has_more"]) == ([], 0, False)


@pytest.mark.parametrize(
    ("dimension", "search", "message", "hint"),
    [
        (
            "dimension.jaffle_order_customer_order_number",
            "1",
            "search: matches text, and 'dimension.jaffle_order_customer_order_number' is integer",
            "Send the call without search; offset pages through every value.",
        ),
        (
            "dimension.jaffle_customer_name",
            "Straße",
            "search: ß folds to lower case differently on different warehouses (by the next "
            "letter, or into a letter of another length), so no text word reads it",
            'Match the exact values with {"op": "is", "value": [...]}, which valid-values lists.',
        ),
    ],
    ids=["number", "unstable_letter"],
)
def test_a_search_no_warehouse_reads_alike_is_refused_before_any_read(
    runtime_factory, monkeypatch, dimension, search, message, hint
):
    runtime = runtime_factory("jaffle_shop")
    monkeypatch.setattr(runtime, "_get_adapter", lambda: pytest.fail("the refused search ran"))
    adapter = SemanticLayerMCPAdapter(runtime)
    try:
        with pytest.raises(SemanticLayerError) as raised:
            valid_values_payload(
                runtime, dimension_id=dimension, search=search, allow_live_query=True
            )
        answer = adapter.call_tool(
            "valid-values", {"dimension_id": dimension, "search": search, "allow_live_query": True}
        )
    finally:
        runtime.close()
    assert (raised.value.code, str(raised.value)) == ("INVALID_QUERY", message)
    assert raised.value.details["path"] == "search"
    assert answer["error"] == {"code": "INVALID_QUERY", "message": message}
    assert answer["errors"][0]["recovery_hints"][0]["message"] == hint


@pytest.mark.parametrize(
    ("warehouse", "tail"),
    [
        ("duckdb", "LIMIT 3\nOFFSET 2"),
        ("postgres", "LIMIT 3\nOFFSET 2"),
        ("snowflake", "LIMIT 3\nOFFSET 2"),
        ("bigquery", "LIMIT 3\nOFFSET 2"),
        ("databricks", "LIMIT 3\nOFFSET 2"),
        ("clickhouse", "LIMIT 3\nOFFSET 2"),
        ("athena", "OFFSET 2\nLIMIT 3"),  # Trino reads OFFSET before LIMIT
    ],
)
def test_an_offset_renders_where_each_warehouse_reads_it(warehouse, tail):
    column = SqlIdentifier(parts=["t", "name"])
    select = SqlSelect(
        select=[SqlField(column, "name")],
        from_table=SqlTableRef("t"),
        order_by=[SqlOrder(column, "ASC", nulls_last=True)],
        limit=3,
        offset=2,
    )
    rendered = render_select_for_profile(select, "audit", dialect=dialect_for_warehouse(warehouse))
    assert rendered.endswith(tail)
