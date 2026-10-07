"""mf2sr writes strict packages; --keep-schema preserves dbt relations."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from mf2sr import translate
from mf2sr.cli import main as cli_main
from semantic_rails.config import load_package_config
from semantic_rails.runtime import Runtime
from tests.semantic_rails.dbt_warehouse import build_dbt_warehouse
from tests.semantic_rails.result_helpers import typed_rows

NODE = {"database": "analytics", "schema_name": "main_marts", "alias": "fct_orders"}


def _manifest(tmp_path: Path, node_relation: dict[str, Any]) -> Path:
    """A dbt semantic_manifest.json with one semantic model over ``node_relation``."""
    orders = {
        "name": "orders",
        "node_relation": node_relation,
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": [{"name": "order", "type": "primary", "expr": "order_id"}],
        "dimensions": [
            {"name": "ordered_at", "type": "time", "type_params": {"time_granularity": "day"}},
            {"name": "status", "type": "categorical"},
        ],
        "measures": [{"name": "order_total", "expr": "order_total", "agg": "sum"}],
    }
    revenue = {
        "name": "revenue",
        "label": "Revenue",
        "type": "simple",
        "type_params": {"measure": {"name": "order_total"}},
    }
    path = tmp_path / "semantic_manifest.json"
    path.write_text(json.dumps({"semantic_models": [orders], "metrics": [revenue]}))
    return path


def _model(report: Any) -> dict[str, Any]:
    return yaml.safe_load((report.package_dir / "models" / "orders.yml").read_text())["model"]


NO_SCHEMA = ["semantic model `orders` names no schema"]
NO_CONNECTION = ["parse: ", "package.connection"]  # mf2sr writes one for Snowflake only


@pytest.mark.parametrize(
    ("warehouse", "node_relation", "relation", "warnings"),
    [
        ("duckdb", NODE, "main_marts.fct_orders", []),
        ("duckdb", {**NODE, "schema_name": "main"}, "fct_orders", []),  # dbt-duckdb's default
        ("snowflake", NODE, "main_marts.fct_orders", []),  # the usual database is implied
        ("postgres", NODE, "main_marts.fct_orders", [NO_CONNECTION, NO_CONNECTION]),
        ("duckdb", {"alias": "fct_orders"}, "fct_orders", [NO_SCHEMA]),  # a YAML directory
    ],
)
def test_relations_keep_their_schema(
    tmp_path: Path,
    warehouse: str,
    node_relation: dict[str, Any],
    relation: str,
    warnings: list[list[str]],
) -> None:
    report = translate(
        _manifest(tmp_path, node_relation),
        tmp_path / "out",
        package_id="shop",
        warehouse=warehouse,
        keep_schema=True,
    )

    assert _model(report)["relation"] == relation
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["schema_strict"] is True
    assert len(report.warnings) == len(warnings), report.warnings
    for warning, parts in zip(report.warnings, warnings, strict=True):
        assert all(part in warning for part in parts), warning
    if warehouse == "snowflake":  # the implied database is pinned, not left to the session
        assert package["connection"]["options"]["database"] == "analytics"


def test_a_relation_outside_the_usual_database_names_it(tmp_path: Path) -> None:
    """As import_dbt_project names dbt relations: the database only when it differs."""
    manifest = json.loads(_manifest(tmp_path, NODE).read_text())
    manifest["semantic_models"].append(
        {
            "name": "customers",
            "node_relation": {"database": "raw", "schema_name": "crm", "alias": "dim_customers"},
            "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        }
    )
    source = tmp_path / "two.json"
    source.write_text(json.dumps(manifest))

    report = translate(
        source, tmp_path / "out", package_id="shop", warehouse="snowflake", keep_schema=True
    )

    relations = {
        name: yaml.safe_load((report.package_dir / "models" / f"{name}.yml").read_text())["model"][
            "relation"
        ]
        for name in ("orders", "customers")
    }
    assert relations == {"orders": "main_marts.fct_orders", "customers": "raw.crm.dim_customers"}
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["connection"]["options"]["database"] == "analytics"


def test_a_strict_package_queries_the_dbt_built_marts(tmp_path: Path) -> None:
    report = translate(
        _manifest(tmp_path, NODE),
        tmp_path / "out",
        package_id="shop",
        default_db="data/warehouse.duckdb",
        keep_schema=True,
    )
    build_dbt_warehouse(report.package_dir / "data" / "warehouse.duckdb")

    assert report.warnings == []  # including no parse errors
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["seed"] == {"kind": "external"}  # dbt built it; never rebuilt
    engine = Runtime.from_path(str(report.package_dir))
    try:
        rows = typed_rows(
            engine.query(
                {
                    "version": 1,
                    "select": [{"expression": {"metric": "metric.shop.revenue"}, "as": "revenue"}],
                }
            )
        )
    finally:
        engine.close()
    assert rows and rows[0]["revenue"] > 0


@pytest.mark.parametrize("flags", [[], ["--keep-schema"]])
def test_parse_errors_fail_strict_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    """A Postgres package has no connection block yet, so it doesn't parse."""
    source = str(_manifest(tmp_path, NODE))
    arguments = ["--source", source, "--package-id", "shop", "--warehouse", "postgres"]

    assert cli_main([*arguments, "--output", str(tmp_path / "a"), *flags]) == 0
    assert cli_main([*arguments, "--output", str(tmp_path / "b"), *flags, "--strict"]) == 2
    assert "parse: " in capsys.readouterr().out


def test_semantic_rails_import_takes_keep_schema(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails.cli",
            "import",
            "--from",
            "metricflow",
            "--source",
            str(_manifest(tmp_path, NODE)),
            "--output",
            str(tmp_path / "out"),
            "--package-id",
            "shop",
            "--keep-schema",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )

    assert json.loads(result.stdout)["ok"] is True
    assert (
        yaml.safe_load((tmp_path / "out" / "shop" / "models" / "orders.yml").read_text())["model"][
            "relation"
        ]
        == "main_marts.fct_orders"
    )


@pytest.mark.parametrize("same_name", [False, True])
def test_default_output_exposes_only_explicit_metrics(tmp_path: Path, same_name: bool) -> None:
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    if same_name:
        raw["metrics"][0]["name"] = "order_total"
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop")
    cfg = load_package_config(report.package_dir)

    assert {metric.id for metric in cfg.metric_recipes} == {
        f"metric.shop.{metric['name']}" for metric in raw["metrics"]
    }
    assert _model(report)["relation"] == "fct_orders"
    package = yaml.safe_load((report.package_dir / "package.yml").read_text())["package"]
    assert package["schema_strict"] is True
    assert package["seed"] == {"kind": "sql_script", "source": "data/seed_shop.sql"}
    assert all("publish" not in measure for measure in _model(report)["measures"].values())
    assert report.warnings == []


@pytest.mark.parametrize("warehouse", ["duckdb", "snowflake"])
def test_keep_schema_preserves_previous_output_bytes(tmp_path: Path, warehouse: str) -> None:
    """Frozen --schema-strict output bytes, with only publish: false lines removed."""
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    raw["metrics"].append(
        {
            "name": "order_total",
            "type": "simple",
            "type_params": {"measure": {"name": "order_total"}},
        }
    )
    source.write_text(json.dumps(raw))
    assert (
        cli_main(
            [
                "--source",
                str(source),
                "--output",
                str(tmp_path / "out"),
                "--package-id",
                "shop",
                "--warehouse",
                warehouse,
                "--keep-schema",
            ]
        )
        == 0
    )
    package_dir = tmp_path / "out" / "shop"
    fixtures = Path(__file__).parent / "fixtures" / "keep_schema"
    expected = {
        path: (fixtures / path).read_text()
        for path in ("graph.yml", "metrics/orders.yml", "models/orders.yml")
    }
    expected["package.yml"] = (fixtures / f"package.{warehouse}.yml").read_text()
    assert {
        str(path.relative_to(package_dir)): path.read_text() for path in package_dir.rglob("*.yml")
    } == expected


@pytest.mark.parametrize("entrypoint", ["mf2sr", "import"])
def test_old_schema_strict_flag_is_refused(tmp_path: Path, entrypoint: str) -> None:
    command = [sys.executable, "-m", "mf2sr"]
    if entrypoint == "import":
        command = [sys.executable, "-m", "semantic_rails.cli", "import", "--from", "metricflow"]
    result = subprocess.run(
        [
            *command,
            "--source",
            str(_manifest(tmp_path, NODE)),
            "--output",
            str(tmp_path / "out"),
            "--package-id",
            "shop",
            "--schema-strict",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 2
    assert "unrecognized arguments: --schema-strict" in result.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("dimension_expr", "metric_type", "skipped_name", "problem"),
    [
        ("lower(status)", "simple", "delivered_revenue", "is not a dimension in this project"),
        (42, "simple", "delivered_revenue", "is not a dimension in this project"),
        (None, "conversion", "orders_conversion", "unsupported type `conversion`"),
    ],
)
def test_skipped_dimensions_and_conversions_drop_dependent_metrics(
    tmp_path: Path,
    dimension_expr: str | int | None,
    metric_type: str,
    skipped_name: str,
    problem: str,
) -> None:
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    skipped_metric: dict[str, Any] = {"name": skipped_name, "type": metric_type}
    if dimension_expr is not None:
        raw["semantic_models"][0]["dimensions"].append(
            {"name": "order_status", "type": "categorical", "expr": dimension_expr}
        )
        skipped_metric.update(
            type_params={"measure": {"name": "order_total"}},
            filter="{{ Dimension('order__order_status') }} = 'delivered'",
        )
    else:
        skipped_metric["type_params"] = {
            "conversion_type_params": {
                "base_measure": {"name": "order_total"},
                "conversion_measure": {"name": "order_total"},
                "entity": "order",
                "window": "7 days",
            }
        }
    dependent_name = f"{skipped_name}_twice"
    raw["metrics"].extend(
        [
            skipped_metric,
            {
                "name": dependent_name,
                "type": "derived",
                "type_params": {
                    "expr": "source * 2",
                    "metrics": [{"name": skipped_name, "alias": "source"}],
                },
            },
        ]
    )
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop", keep_schema=True)

    assert report.metrics_emitted == ["revenue"]
    assert any(
        warning.startswith(f"metric `{skipped_name}`:")
        and problem in warning
        and "skipped" in warning
        for warning in report.warnings
    )
    assert any(
        warning.startswith(f"metric `{dependent_name}`:") and "skipped too" in warning
        for warning in report.warnings
    )
    if dimension_expr is not None:
        assert "order_status" not in _model(report)["dimensions"]
        assert any("dimension `order_status` has a non-column expr" in w for w in report.warnings)
    assert not any(w.startswith("parse:") for w in report.warnings)
    cfg = load_package_config(report.package_dir)
    assert {metric.id for metric in cfg.metric_recipes} == {"metric.shop.revenue"}

    database = build_dbt_warehouse(report.package_dir / "data" / "shop.duckdb")
    query = {
        "version": 1,
        "select": [{"expression": {"metric": "metric.shop.revenue"}, "as": "revenue"}],
    }
    engine = Runtime.from_path(str(report.package_dir))
    try:
        assert engine.compile(query)["ok"] is True
        rows = typed_rows(engine.query(query))
    finally:
        engine.close()
    with duckdb.connect(str(database), read_only=True) as conn:
        expected = conn.execute("SELECT SUM(order_total) FROM main_marts.fct_orders").fetchone()
    assert expected is not None
    assert rows == [{"revenue": expected[0]}]


# Per status, `amount` sums to -30, 0 and 12, and `b` sums to 0, 2 and -4.
SIGNED_ORDERS_SQL = """
CREATE SCHEMA main_marts;
CREATE TABLE main_marts.fct_orders (
  order_id INTEGER, ordered_at DATE, status VARCHAR,
  order_total DOUBLE, amount DOUBLE, a DOUBLE, b DOUBLE
);
INSERT INTO main_marts.fct_orders VALUES
  (1, DATE '2024-01-01', 'returned', 10, -10, 6, 4),
  (2, DATE '2024-01-02', 'returned', 20, -20, -9, -4),
  (3, DATE '2024-01-03', 'placed', 30, 5, 0, 3),
  (4, DATE '2024-01-04', 'placed', 40, -5, 0, -1),
  (5, DATE '2024-01-05', 'delivered', 50, 12, 9, -6),
  (6, DATE '2024-01-06', 'delivered', 60, 0, 0, 2);
"""


def _signed_package(tmp_path: Path, derived: list[dict[str, Any]]) -> Any:
    """Translate measures and same-named metrics `source`, `a` and `b`, plus ``derived``.

    The same names mean a derived metric written as an aggregate over its
    first input would still load and run.
    """
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    raw["semantic_models"][0]["measures"] += [
        {"name": "source", "expr": "amount", "agg": "sum"},
        {"name": "a", "expr": "a", "agg": "sum"},
        {"name": "b", "expr": "b", "agg": "sum"},
    ]
    raw["metrics"] += [
        {"name": name, "type": "simple", "type_params": {"measure": {"name": name}}}
        for name in ("source", "a", "b")
    ]
    raw["metrics"] += derived
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop", keep_schema=True)
    (report.package_dir / "data").mkdir()
    with duckdb.connect(str(report.package_dir / "data" / "shop.duckdb")) as conn:
        conn.execute(SIGNED_ORDERS_SQL)
    return report


def _by_status(package_dir: Path, metrics: list[str]) -> dict[str, dict[str, Any]]:
    query = {
        "version": 1,
        "select": [
            {"expression": {"metric": f"metric.shop.{name}"}, "as": name} for name in metrics
        ],
        "group_by": ["dimension.shop_order_status"],
    }
    engine = Runtime.from_path(str(package_dir))
    try:
        assert engine.compile(query)["ok"] is True
        rows = typed_rows(engine.query(query))
    finally:
        engine.close()
    return {row.pop("dimension.shop_order_status"): row for row in rows}


def _reference(package_dir: Path, columns: dict[str, str]) -> dict[str, dict[str, Any]]:
    select = ", ".join(f"{sql} AS {name}" for name, sql in columns.items())
    with duckdb.connect(str(package_dir / "data" / "shop.duckdb"), read_only=True) as conn:
        rows = conn.execute(
            f"SELECT status, {select} FROM main_marts.fct_orders GROUP BY status"
        ).fetchall()
    return {status: dict(zip(columns, values, strict=True)) for status, *values in rows}


@pytest.mark.parametrize(
    "expr",
    [
        "ABS(source)",  # -30 for returned orders if written as SUM(amount), not 30
        "NULLIF(source, 0) + 1",  # 1 for placed orders if NULLIF is dropped, not NULL
        "NULLIF(a, 0) / b",  # 0 for placed orders if NULLIF is dropped, not NULL
        "a / (NULLIF(b, 0) + 1)",  # -3 for returned orders if NULLIF is dropped, not NULL
        # SQL reads what follows `--` or `#` as a comment (or `#` as XOR); Python
        # reads a - (-b) and a.
        "a--b",
        "a # b",
        # Python reads these as 1000, 16, 1000.0 and a complex number; SQL dialects
        # differ (`0x10` can be `0 AS x10`).
        "1_000 * a",
        "0x10 * a",
        "1e3 * a",
        "1j * a",
    ],
)
def test_derived_expressions_without_an_exact_translation_are_skipped(
    tmp_path: Path, expr: str
) -> None:
    report = _signed_package(
        tmp_path,
        [
            {
                "name": "inexact",
                "type": "derived",
                "type_params": {
                    "expr": expr,
                    "metrics": [{"name": "source"}, {"name": "a"}, {"name": "b"}],
                },
            },
            {
                "name": "inexact_twice",
                "type": "derived",
                "type_params": {
                    "expr": "base * 2",
                    "metrics": [{"name": "inexact", "alias": "base"}],
                },
            },
        ],
    )

    assert report.metrics_emitted == ["revenue", "source", "a", "b"]
    assert report.warnings == [
        f"metric `inexact`: could not parse derived expression `{expr}`; "
        "skipped rather than approximated",
        "metric `inexact_twice`: it uses `inexact`, which mf2sr skipped; skipped too",
    ]
    cfg = load_package_config(report.package_dir)
    assert {metric.id for metric in cfg.metric_recipes} == {
        f"metric.shop.{name}" for name in ("revenue", "source", "a", "b")
    }
    assert _by_status(report.package_dir, ["revenue", "source", "a", "b"]) == _reference(
        report.package_dir,
        {"revenue": "SUM(order_total)", "source": "SUM(amount)", "a": "SUM(a)", "b": "SUM(b)"},
    )


@pytest.mark.parametrize(
    ("expr", "reference"),
    [
        ("a / NULLIF(b, 0)", "SUM(a) / NULLIF(SUM(b), 0)"),
        ("a * 1.0 / nullif( b , 0 )", "SUM(a) * 1.0 / NULLIF(SUM(b), 0)"),
    ],
)
def test_a_denominator_nullif_matches_the_source_formula(
    tmp_path: Path, expr: str, reference: str
) -> None:
    report = _signed_package(
        tmp_path,
        [
            {
                "name": "a_per_b",
                "type": "derived",
                "type_params": {"expr": expr, "metrics": [{"name": "a"}, {"name": "b"}]},
            }
        ],
    )

    assert report.metrics_emitted == ["revenue", "source", "a", "b", "a_per_b"]
    assert report.warnings == []
    rows = _by_status(report.package_dir, ["a_per_b"])
    assert rows == _reference(report.package_dir, {"a_per_b": reference})
    # Returned orders' b sums to 0, so the source formula is NULL there.
    assert rows == {
        "returned": {"a_per_b": None},
        "placed": {"a_per_b": 0.0},
        "delivered": {"a_per_b": -2.25},
    }


# 8 orders in 4 statuses. customer_id repeats and is NULL twice; payer_id is
# another customer column. Per status, the row count, COUNT(customer_id),
# COUNT(DISTINCT customer_id) and COUNT(DISTINCT payer_id) differ somewhere.
COUNTED_ORDERS_SQL = """
CREATE SCHEMA main_marts;
CREATE TABLE main_marts.fct_orders (
  order_id INTEGER, ordered_at DATE, status VARCHAR, order_total DOUBLE,
  customer_id INTEGER, payer_id INTEGER
);
INSERT INTO main_marts.fct_orders VALUES
  (1, DATE '2024-01-01', 'placed', 10, 1, 1),
  (2, DATE '2024-01-02', 'placed', 20, 1, 2),
  (3, DATE '2024-02-03', 'shipped', 30, 2, 2),
  (4, DATE '2024-02-04', 'shipped', 40, NULL, 2),
  (5, DATE '2024-02-05', 'delivered', 50, 2, 3),
  (6, DATE '2024-03-06', 'delivered', 60, NULL, 3),
  (7, DATE '2024-03-07', 'returned', 70, 3, 3),
  (8, DATE '2024-03-08', 'returned', 80, 3, 3);
CREATE TABLE main_marts.dim_customers (customer_id INTEGER);
INSERT INTO main_marts.dim_customers VALUES (1), (2), (3);
"""


def _counted_package(
    tmp_path: Path, measure: dict[str, Any], customer_column: str, metrics: list[dict[str, Any]]
) -> Any:
    """Orders with ``measure``, a customer entity declared on ``customer_column``, and ``metrics``."""
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    orders = raw["semantic_models"][0]
    orders["entities"].append({"name": "customer", "type": "foreign", "expr": customer_column})
    orders["measures"].append(measure)
    raw["semantic_models"].append(
        {
            "name": "customers",
            "node_relation": {**NODE, "alias": "dim_customers"},
            "entities": [{"name": "customer", "type": "primary", "expr": "customer_id"}],
        }
    )
    raw["metrics"] += metrics
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop", keep_schema=True)
    (report.package_dir / "data").mkdir()
    with duckdb.connect(str(report.package_dir / "data" / "shop.duckdb")) as conn:
        conn.execute(COUNTED_ORDERS_SQL)
    return report


@pytest.mark.parametrize(
    ("measure", "customer_column", "reference"),
    [
        # No graph entity keys `status`: once a count of non-null rows, 8 not 4.
        ({"name": "statuses", "expr": "status", "agg": "count_distinct"}, "customer_id", None),
        # Without `expr`, the column named after the measure is counted, never the rows.
        ({"name": "payer_id", "agg": "count_distinct"}, "customer_id", None),
        ({"expr": "1", "agg": "count_distinct"}, "customer_id", None),  # MetricFlow's is 1
        ({"expr": "order_total", "agg": "bogus"}, "customer_id", None),  # once a sum
        # A repeated foreign key: once its distinct count.
        ({"expr": "customer_id", "agg": "count"}, "customer_id", "COUNT(customer_id)"),
        ({"name": "customer_id", "agg": "count"}, "customer_id", "COUNT(customer_id)"),
        (
            {"expr": "customer_id", "agg": "count_distinct"},
            "customer_id",
            "COUNT(DISTINCT customer_id)",
        ),
        # The customer entity is declared on payer_id: once COUNT(DISTINCT payer_id).
        (
            {"expr": "customer_id", "agg": "count_distinct"},
            "payer_id",
            "COUNT(DISTINCT customer_id)",
        ),
        (
            {"name": "customer_id", "agg": "count_distinct"},
            "customer_id",
            "COUNT(DISTINCT customer_id)",
        ),
    ],
)
def test_counts_translate_exactly_or_are_skipped(
    tmp_path: Path, measure: dict[str, Any], customer_column: str, reference: str | None
) -> None:
    measure = {"name": "counted", **measure}
    report = _counted_package(
        tmp_path,
        measure,
        customer_column,
        [
            {
                "name": "counted",
                "type": "simple",
                "type_params": {"measure": {"name": measure["name"]}},
            },
            {
                "name": "counted_twice",
                "type": "derived",
                "type_params": {"expr": "counted * 2", "metrics": [{"name": "counted"}]},
            },
        ],
    )

    columns = {"revenue": "SUM(order_total)"}
    if reference is None:
        assert report.metrics_emitted == ["revenue"]
        skipped, *dependents = report.warnings
        assert skipped.startswith(f"model `orders`: measure `{measure['name']}` ")
        assert skipped.endswith("skipped rather than approximated")
        assert dependents == [
            f"metric `counted`: measure `{measure['name']}` was not emitted; skipped",
            "metric `counted_twice`: it uses `counted`, which mf2sr skipped; skipped too",
        ]
    else:
        assert report.metrics_emitted == ["revenue", "counted", "counted_twice"]
        assert report.warnings == []
        columns.update(counted=reference, counted_twice=f"{reference} * 2")
    assert _by_status(report.package_dir, list(columns)) == _reference(report.package_dir, columns)


def test_a_running_count_of_a_column_adds_up_its_periods(tmp_path: Path) -> None:
    """COUNT(customer_id) adds up across months, so its running total is kept."""
    report = _counted_package(
        tmp_path,
        {"name": "customer_orders", "expr": "customer_id", "agg": "count"},
        "customer_id",
        [
            {
                "name": "running_customer_orders",
                "type": "cumulative",
                "type_params": {"measure": "customer_orders"},
            }
        ],
    )

    assert report.metrics_emitted == ["revenue", "running_customer_orders"]
    month = "temporal_role.shop_order_ordered_at__month"
    engine = Runtime.from_path(str(report.package_dir))
    try:
        rows = typed_rows(
            engine.query(
                {
                    "version": 1,
                    "select": [
                        {
                            "expression": {"metric": "metric.shop.running_customer_orders"},
                            "as": "running",
                        }
                    ],
                    "time": {
                        "temporal_role": "temporal_role.shop_order_ordered_at",
                        "grain": "month",
                    },
                    "order_by": [{"field": month}],
                }
            )
        )
    finally:
        engine.close()
    with duckdb.connect(str(report.package_dir / "data" / "shop.duckdb"), read_only=True) as conn:
        expected = conn.execute(
            "SELECT SUM(COUNT(customer_id)) OVER (ORDER BY date_trunc('month', ordered_at)) "
            "FROM main_marts.fct_orders GROUP BY date_trunc('month', ordered_at) "
            "ORDER BY date_trunc('month', ordered_at)"
        ).fetchall()
    assert [row["running"] for row in rows] == [value for (value,) in expected] == [2, 4, 6]


@pytest.mark.parametrize("dimension_expr", ["status", "lower(status)"])
def test_dimension_and_percentile_output_uses_loadable_keys(
    tmp_path: Path, dimension_expr: str
) -> None:
    source = _manifest(tmp_path, NODE)
    raw = json.loads(source.read_text())
    model = raw["semantic_models"][0]
    model["dimensions"].append(
        {"name": "order_status", "type": "categorical", "expr": dimension_expr}
    )
    model["measures"].append(
        {
            "name": "p90_total",
            "expr": "order_total",
            "agg": "percentile",
            "agg_params": {"percentile": 0.9},
        }
    )
    raw["metrics"].extend(
        [
            {
                "name": "p90",
                "type": "simple",
                "type_params": {"measure": {"name": "p90_total"}},
            },
            {"name": "p90_running", "type": "cumulative", "type_params": {"measure": "p90_total"}},
            {
                "name": "p90_share",
                "type": "ratio",
                "type_params": {
                    "numerator": "p90_total",
                    "denominator": "order_total",
                },
            },
            {
                "name": "p90_twice",
                "type": "derived",
                "type_params": {
                    "expr": "p90 * 2",
                    "metrics": [{"name": "p90"}],
                },
            },
        ]
    )
    source.write_text(json.dumps(raw))
    report = translate(source, tmp_path / "out", package_id="shop", keep_schema=True)
    build_dbt_warehouse(report.package_dir / "data" / "shop.duckdb")
    cfg = load_package_config(report.package_dir)

    assert {metric.id for metric in cfg.metric_recipes} == {"metric.shop.revenue"}
    assert "p90_total" not in _model(report)["measures"]
    assert any("unsupported percentile parameters" in warning for warning in report.warnings)
    if dimension_expr == "status":
        dimension = next(
            dim for dim in cfg.dimensions if dim.id == "dimension.shop_order_order_status"
        )
        assert dimension.column == "status"
        assert _model(report)["dimensions"]["order_status"]["column"] == "status"
        engine = Runtime.from_path(str(report.package_dir))
        try:
            rows = typed_rows(
                engine.query(
                    {
                        "version": 1,
                        "select": [
                            {"expression": {"metric": "metric.shop.revenue"}, "as": "revenue"}
                        ],
                        "where": [
                            {
                                "field": "dimension.shop_order_order_status",
                                "op": "=",
                                "value": "delivered",
                            }
                        ],
                    }
                )
            )
        finally:
            engine.close()
        with duckdb.connect(
            str(report.package_dir / "data" / "shop.duckdb"), read_only=True
        ) as conn:
            expected = conn.execute(
                "SELECT SUM(order_total) FROM main_marts.fct_orders WHERE status = 'delivered'"
            ).fetchone()
        assert expected is not None
        assert rows == [{"revenue": expected[0]}]
    else:
        assert "order_status" not in _model(report)["dimensions"]
        assert any("non-column expr" in warning for warning in report.warnings)
    assert not any(warning.startswith("parse:") for warning in report.warnings)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "semantic_rails.cli",
            "validate-config",
            "--path",
            str(report.package_dir),
            "--quiet",
            "--no-manifest",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["ok"] is True
