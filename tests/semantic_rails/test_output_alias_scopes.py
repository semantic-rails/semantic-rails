"""A warehouse table's column is read as the warehouse names it, whatever the answer's names.

The answer's own column names (the outermost SELECT's aliases) are quoted where a SELECT defines
them and wherever a reference reads that definition, so a keyword such as ``order`` is a name on
every warehouse. A warehouse table's column keeps the quoting every other column gets (only a
name that needs it), even when the table shares a name with a relation the engine names itself
(``base``, ``projected``) and the column shares one with an answer column: quoting it would
read another column, or none, where a quoted name keeps its case (Postgres reads an unquoted
``REGION`` as ``region``, Snowflake an unquoted ``amount`` as ``AMOUNT``). Each reference is
decided in its own SQL scope, from what its relation is there.

Fixture: orders live in a table named ``base`` and stores in one named ``projected``. Orders 1
(5, web) and 2 (7, shop) are store 1's (east), order 3 (100, web) store 2's (west), order 4
(2, web) store 3's (east) and order 5 (3, web) store 4's (north). The model names the stores'
region column ``REGION`` and the orders' channel column ``order``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.config import load_package_config
from semantic_rails.dialects import dialect_for_warehouse as _dialect
from semantic_rails.dialects import supported_warehouses
from semantic_rails.renderer import render_select_for_profile
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCte,
    SqlExists,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableFunction,
    SqlTableRef,
    SqlWindow,
)

SEED = """
CREATE TABLE base (order_id INTEGER, store_id INTEGER, amount INTEGER, "order" VARCHAR);
INSERT INTO base VALUES
  (1, 1, 5, 'web'), (2, 1, 7, 'shop'), (3, 2, 100, 'web'), (4, 3, 2, 'web'), (5, 4, 3, 'web');
CREATE TABLE projected (store_id INTEGER, region VARCHAR);
INSERT INTO projected VALUES (1, 'east'), (2, 'west'), (3, 'east'), (4, 'north');
"""
FILES = {
    "package.yml": """
schema_version: 1
package: {id: shop, namespace: shop, warehouse: duckdb, default_db: data/warehouse.duckdb,
  seed: {kind: sql_script, source: data/seed.sql}}
""",
    "data/seed.sql": SEED,
    "graph.yml": """
graph:
  entities:
    order: {label: Order, key: [order_id], model: orders}
    store: {label: Store, key: [store_id], model: stores}
  relationships:
    order_store: {id: relationship.order_store, entities: [order, store], via: [store_id]}
""",
    "models/orders.yml": """
model:
  id: orders
  relation: base
  entities: {order: {}}
  dimensions:
    channel: {label: Channel, kind: categorical, column: order}
  measures:
    revenue: {label: Revenue, kind: aggregate, expr: amount, accumulation: {kind: flow}}
    order_count: {label: Orders, kind: entity_count, entity_key: order_id,
      accumulation: {kind: flow}}
    largest_order: {label: Largest order, kind: aggregate, expr: amount, default_agg: max}
    smallest_order: {label: Smallest order, kind: aggregate, expr: amount, default_agg: min}
""",
    "models/stores.yml": """
model:
  id: stores
  relation: projected
  entities: {store: {}}
  dimensions:
    region: {label: Region, kind: categorical, column: REGION}
""",
}
REGION = "dimension.shop_store_region"
CHANNEL = "dimension.shop_order_channel"
# Each answer column is named like a column the query reads from a warehouse table.
SELECT = [
    {"expression": "measure.shop.revenue", "as": "amount"},
    {"expression": "measure.shop.order_count", "as": "REGION"},
    {"expression": "measure.shop.largest_order", "as": "store_id"},
    {"expression": "measure.shop.smallest_order", "as": "order"},
]
ORDERED = {"group_by": [REGION, CHANNEL], "order_by": [{"field": "store_id", "direction": "desc"}]}
# `having` adds a CTE named `projected`, after the leaf that reads the table of that name.
FILTERED = {**ORDERED, "having": [{"field": "amount", "op": "gte", "value": 6}]}

# The leaf reads the warehouse tables (no quoting but "order"'s); every later scope reads the
# CTEs, whose answer columns are quoted where they are defined and where they are read.
EXPECTED = """WITH leaf_1 AS (
SELECT
  projected.REGION AS g1,
  base.{q}order{q} AS g2,
  SUM(base.amount) AS m1,
  COUNT(1) AS m1_rows,
  COUNT(DISTINCT base.order_id) AS m2,
  MAX(base.amount) AS m3,
  MIN(base.amount) AS m4
FROM base
{join} JOIN projected ON base.store_id = projected.store_id
GROUP BY
  projected.REGION,
  base.{q}order{q}
),
guarded_base AS (
SELECT
  base.g1 AS g1,
  base.g2 AS g2,
  COALESCE(base.m1, CASE WHEN COUNT(base.m1) OVER () > 0 AND ((base.m1_rows IS NULL) OR base.m1_rows = 0) THEN 0 END) AS m1,
  CASE WHEN MAX(base.m2) OVER () > 0 THEN COALESCE(base.m2, 0) END AS m2,
  base.m3 AS m3,
  base.m4 AS m4
FROM leaf_1 AS base
),
projected AS (
SELECT
  base.g1 AS g1,
  base.g2 AS g2,
  base.m1 AS {q}amount{q},
  base.m2 AS {q}REGION{q},
  base.m3 AS {q}store_id{q},
  base.m4 AS {q}order{q},
  base.m1 AS p1
FROM guarded_base AS base
)
SELECT
  projected.g1 AS {region},
  projected.g2 AS {channel},
  projected.{q}amount{q} AS {q}amount{q},
  projected.{q}REGION{q} AS {q}REGION{q},
  projected.{q}store_id{q} AS {q}store_id{q},
  projected.{q}order{q} AS {q}order{q}
FROM projected
WHERE
  projected.p1 >= 6
ORDER BY
  projected.{q}store_id{q} DESC NULLS LAST{tail}"""
_STANDARD = {
    "q": '"',
    "join": "LEFT",
    "region": '"dimension.shop_store_region"',
    "channel": '"dimension.shop_order_channel"',
    "tail": "",
}
# Each warehouse's spelling: its identifier quote, the leaf's join, the group columns' names in
# its SQL, and a trailing setting.
DIALECT_FORMS: dict[str, dict[str, str]] = {
    "athena": _STANDARD,
    "bigquery": {
        **_STANDARD,
        "q": "`",
        "region": "`dimension_shop_store_region_cb49d1c461`",
        "channel": "`dimension_shop_order_channel_187a2eda36`",
    },
    "clickhouse": {**_STANDARD, "join": "INNER", "tail": "\nSETTINGS join_use_nulls = 1"},
    "databricks": {
        **_STANDARD,
        "q": "`",
        "region": "`dimension.shop_store_region`",
        "channel": "`dimension.shop_order_channel`",
    },
    "duckdb": _STANDARD,
    "ducklake": _STANDARD,
    "motherduck": _STANDARD,
    "postgres": _STANDARD,
    "snowflake": _STANDARD,
}


def _package(root: Path) -> Path:
    for name, text in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def test_every_warehouse_has_its_spelling() -> None:
    assert set(DIALECT_FORMS) == set(supported_warehouses())


@pytest.mark.parametrize("warehouse", sorted(DIALECT_FORMS))
def test_a_table_named_like_an_engine_relation_reads_its_own_columns(
    tmp_path: Path, warehouse: str
) -> None:
    config = load_package_config(str(_package(tmp_path)))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    runtime = Runtime.from_config(config, source_path=str(tmp_path))
    try:
        sql = runtime.compile({"select": SELECT, **FILTERED})["rendered_sql"]
    finally:
        runtime.close()
    assert sql == EXPECTED.format(**DIALECT_FORMS[warehouse])


@pytest.mark.parametrize(
    ("call", "rows"),
    [
        (
            FILTERED,
            [
                ("west", "web", 100, 1, 100, 100),
                ("east", "shop", 7, 1, 7, 7),
                ("east", "web", 7, 2, 5, 2),
            ],
        ),
        (
            ORDERED,
            [
                ("west", "web", 100, 1, 100, 100),
                ("east", "shop", 7, 1, 7, 7),
                ("east", "web", 7, 2, 5, 2),
                ("north", "web", 3, 1, 3, 3),
            ],
        ),
    ],
    ids=["filtered", "ordered"],
)
def test_duckdb_answers_from_the_tables_own_columns(
    tmp_path: Path, call: dict[str, Any], rows: list[tuple[Any, ...]]
) -> None:
    runtime = Runtime.from_path(str(_package(tmp_path)))
    try:
        result = runtime.query({"select": SELECT, **call})
    finally:
        runtime.close()
    columns = [REGION, CHANNEL, "amount", "REGION", "store_id", "order"]
    assert [tuple(row[column] for column in columns) for row in result["rows"]] == rows


def _ref(*parts: str) -> SqlIdentifier:
    return SqlIdentifier(parts=list(parts))


def _sum(*parts: str) -> SqlCall:
    return SqlCall("SUM", [_ref(*parts)])


def _hit(table: SqlTableRef, condition: SqlBinary) -> SqlExists:
    return SqlExists(
        SqlSelect(select=[SqlField(SqlLiteral(1), "hit")], from_table=table, where=[condition])
    )


# Each case: a query whose answer columns are named "amount" (or "store_id") and the SQL it
# renders. A column read through a warehouse table is never quoted for it; one read through a
# CTE in view, or an alias the statement (or an enclosing one) gives a CTE, is.
SCOPE_CASES = {
    # `base` is the warehouse table in the leaf and the leaf's alias in the outer query.
    "a_cte_alias_elsewhere": (
        SqlSelect(
            ctes=[
                SqlCte(
                    "leaf",
                    SqlSelect(
                        select=[SqlField(_sum("base", "amount"), "amount")],
                        from_table=SqlTableRef("base"),
                    ),
                )
            ],
            select=[SqlField(_ref("base", "amount"), "amount")],
            from_table=SqlTableRef("leaf", alias="base"),
        ),
        """WITH leaf AS (
SELECT
  SUM(base.amount) AS "amount"
FROM base
)
SELECT
  base."amount" AS "amount"
FROM leaf AS base""",
    ),
    # A CTE reads only the CTEs before it: the leaf's `projected` is the warehouse table.
    "a_later_cte_name": (
        SqlSelect(
            ctes=[
                SqlCte(
                    "leaf",
                    SqlSelect(
                        select=[SqlField(_sum("projected", "amount"), "amount")],
                        from_table=SqlTableRef("projected"),
                    ),
                ),
                SqlCte(
                    "projected",
                    SqlSelect(
                        select=[SqlField(_ref("leaf", "amount"), "amount")],
                        from_table=SqlTableRef("leaf"),
                    ),
                ),
            ],
            select=[SqlField(_ref("projected", "amount"), "amount")],
            from_table=SqlTableRef("projected"),
        ),
        """WITH leaf AS (
SELECT
  SUM(projected.amount) AS "amount"
FROM projected
),
projected AS (
SELECT
  leaf."amount" AS "amount"
FROM leaf
)
SELECT
  projected."amount" AS "amount"
FROM projected""",
    ),
    # `base` is a CTE alias in one CTE and the warehouse table in the outer query and its
    # correlated subquery.
    "a_correlated_subquery": (
        SqlSelect(
            ctes=[
                SqlCte(
                    "seen",
                    SqlSelect(
                        select=[SqlField(_ref("stores", "store_id"), "store_id")],
                        from_table=SqlTableRef("stores"),
                    ),
                ),
                SqlCte(
                    "leaf",
                    SqlSelect(
                        select=[SqlField(_ref("base", "store_id"), "store_id")],
                        from_table=SqlTableRef("seen", alias="base"),
                    ),
                ),
            ],
            select=[
                SqlField(SqlCall("MAX", [_ref("base", "store_id")]), "store_id"),
                SqlField(_sum("base", "amount"), "amount"),
            ],
            from_table=SqlTableRef("base"),
            where=[
                _hit(
                    SqlTableRef("leaf"),
                    SqlBinary(_ref("leaf", "store_id"), "=", _ref("base", "store_id")),
                )
            ],
        ),
        """WITH seen AS (
SELECT
  stores.store_id AS "store_id"
FROM stores
),
leaf AS (
SELECT
  base."store_id" AS "store_id"
FROM seen AS base
)
SELECT
  MAX(base.store_id) AS "store_id",
  SUM(base.amount) AS "amount"
FROM base
WHERE
  EXISTS (
SELECT
  1 AS hit
FROM leaf
WHERE
  leaf."store_id" = base.store_id
)""",
    ),
}


@pytest.mark.parametrize(("query", "expected"), SCOPE_CASES.values(), ids=SCOPE_CASES.keys())
def test_quoting_is_decided_in_each_statement(query: SqlSelect, expected: str) -> None:
    assert render_select_for_profile(query) == expected


def _orders(**clauses: Any) -> SqlSelect:
    return SqlSelect(from_table=SqlTableRef("orders"), **clauses)


# References whose quoting does not depend on a warehouse table's name: rendered as before. A
# bare name that is an answer column is always quoted, whatever it reads.
KEPT_CASES = {
    # Unquoted, Postgres reads `user` as the session user and DuckDB refuses `end`.
    "bare_keywords": (
        SqlSelect(
            select=[SqlField(_ref("user"), "user"), SqlField(_ref("end"), "end")],
            from_table=SqlTableRef("people"),
        ),
        """SELECT
  "user" AS "user",
  "end" AS "end"
FROM people""",
    ),
    # The subquery's bare name reads the enclosing CTE's "REGION", not its own table.
    "bare_correlated_name": (
        SqlSelect(
            ctes=[
                SqlCte(
                    "c",
                    SqlSelect(
                        select=[
                            SqlField(SqlLiteral(1), "REGION"),
                            SqlField(SqlLiteral(2), "region"),
                        ]
                    ),
                )
            ],
            select=[SqlField(_ref("c", "REGION"), "REGION")],
            from_table=SqlTableRef("c"),
            where=[_hit(SqlTableRef("orders"), SqlBinary(_ref("REGION"), "=", SqlLiteral(1)))],
        ),
        """WITH c AS (
SELECT
  1 AS "REGION",
  2 AS region
)
SELECT
  c."REGION" AS "REGION"
FROM c
WHERE
  EXISTS (
SELECT
  1 AS hit
FROM orders
WHERE
  "REGION" = 1
)""",
    ),
    # QUALIFY, HAVING and GROUP BY read the SELECT's own columns by name.
    "aliases_in_qualify": (
        _orders(
            select=[SqlField(SqlWindow(SqlCall("ROW_NUMBER", [])), "rank")],
            qualify=[SqlBinary(_ref("rank"), "=", SqlLiteral(1))],
        ),
        """SELECT
  ROW_NUMBER() OVER () AS "rank"
FROM orders
QUALIFY
  "rank" = 1""",
    ),
    "aliases_in_group_by_and_having": (
        _orders(
            select=[
                SqlField(_ref("orders", "region"), "area"),
                SqlField(_sum("orders", "amount"), "total"),
            ],
            group_by=[_ref("area")],
            having=[SqlBinary(_ref("total"), ">", SqlLiteral(10))],
        ),
        """SELECT
  orders.region AS "area",
  SUM(orders.amount) AS "total"
FROM orders
GROUP BY
  "area"
HAVING
  "total" > 10""",
    ),
    # A bare name through a CTE written in another case.
    "bare_name_through_a_cte_in_another_case": (
        SqlSelect(
            ctes=[SqlCte("leaf", SqlSelect(select=[SqlField(SqlLiteral(1), "REGION")]))],
            select=[SqlField(_ref("REGION"), "REGION")],
            from_table=SqlTableRef("LEAF"),
        ),
        """WITH leaf AS (
SELECT
  1 AS "REGION"
)
SELECT
  "REGION" AS "REGION"
FROM LEAF""",
    ),
    # A table-function argument reads the enclosing query's `base` (the CTE), which a later
    # join of the subquery cannot hide.
    "a_lateral_argument_before_a_join": (
        SqlSelect(
            ctes=[SqlCte("leaf", SqlSelect(select=[SqlField(SqlLiteral(3), "REGION")]))],
            select=[SqlField(_ref("base", "REGION"), "REGION")],
            from_table=SqlTableRef("leaf", alias="base"),
            where=[
                SqlExists(
                    SqlSelect(
                        select=[SqlField(SqlLiteral(1), "hit")],
                        from_table=SqlTableFunction(
                            "GENERATE_SERIES",
                            [SqlLiteral(1), _ref("base", "REGION")],
                            alias="g",
                            columns=["n"],
                            lateral=True,
                        ),
                        joins=[SqlJoin("CROSS", SqlTableRef("orders", alias="base"))],
                    )
                )
            ],
        ),
        """WITH leaf AS (
SELECT
  3 AS "REGION"
)
SELECT
  base."REGION" AS "REGION"
FROM leaf AS base
WHERE
  EXISTS (
SELECT
  1 AS hit
FROM LATERAL GENERATE_SERIES(1, base."REGION") AS g(n)
CROSS JOIN orders AS base
)""",
    ),
}


@pytest.mark.parametrize(("query", "expected"), KEPT_CASES.values(), ids=KEPT_CASES.keys())
def test_other_references_render_as_before(query: SqlSelect, expected: str) -> None:
    assert render_select_for_profile(query) == expected


def test_bare_keyword_columns_read_the_table() -> None:
    sql = render_select_for_profile(KEPT_CASES["bare_keywords"][0], dialect=_dialect("duckdb"))
    connection = duckdb.connect()
    connection.execute("""CREATE TABLE people ("user" VARCHAR, "end" INTEGER)""")
    connection.execute("INSERT INTO people VALUES ('ann', 7)")
    assert connection.execute(sql).fetchall() == [("ann", 7)]


@pytest.mark.parametrize(
    ("warehouse", "reference"),
    [("duckdb", 'LEAF."REGION"'), ("postgres", 'LEAF."REGION"'), ("clickhouse", "LEAF.REGION")],
)
def test_a_cte_is_found_in_another_case_where_names_fold(warehouse: str, reference: str) -> None:
    """Unquoted, LEAF names the CTE leaf, except where identifiers are case-sensitive."""
    query = SqlSelect(
        ctes=[SqlCte("leaf", SqlSelect(select=[SqlField(SqlLiteral(1), "REGION")]))],
        select=[SqlField(_ref("LEAF", "REGION"), "REGION")],
        from_table=SqlTableRef("LEAF"),
    )
    sql = render_select_for_profile(query, dialect=_dialect(warehouse))
    assert f"  {reference} AS " in sql
