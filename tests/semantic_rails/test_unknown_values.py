"""A dimension's declared unknown spellings read as NULL everywhere the dimension is read.

The source writes an unknown country as '' or 'XX' as well as NULL. Declaring
``unknown_values: ['', 'XX']`` makes grouping, ``missing``, ``not`` and the unknown-rows note
treat all three alike. Gold values come from plain SQL over the seed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.expressions import AggregateExpr
from semantic_rails.runtime import Runtime
from semantic_rails.schema import (
    AggregateRelationConfig,
    EntityConfig,
    MetricConfig,
    RelationshipConfig,
    SemanticPolicyConfig,
    TemporalRoleConfig,
)

SEED = """
CREATE TABLE downloads (download_id INTEGER, country VARCHAR, installs INTEGER);
INSERT INTO downloads VALUES
  (1, 'US', 10), (2, 'DE', 4), (3, '', 2), (4, 'XX', 1), (5, NULL, 3), (6, 'US', 5);
"""
COUNTRY = "dimension.dl_download_country"
INSTALLS = "measure.dl.installs"
KNOWN = "CASE WHEN country IN ('', 'XX') THEN NULL ELSE country END"


def _package(
    root: Path,
    country: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    others: dict[str, Any] | None = None,
) -> Path:
    def put(name: str, doc: dict[str, Any]) -> None:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    put("package.yml", {
        "schema_version": 1,
        "package": {"id": "dl", "namespace": "dl", "name": "dl", "description": "Downloads",
                    "warehouse": "duckdb", "default_db": "dl.duckdb", "seed": {"kind": "external"}},
        **({"defaults": defaults} if defaults else {}),
    })  # fmt: skip
    put("graph.yml", {"graph": {"entities": {
        "download": {"key": ["download_id"], "model": "downloads"},
    }}})  # fmt: skip
    put("models/downloads.yml", {"model": {
        "id": "downloads", "relation": "downloads", "entities": {"download": {}},
        "dimensions": {"country": {"kind": "categorical", **country}, **(others or {})},
        "measures": {"installs": {"kind": "aggregate", "expr": "installs", "value_type": "count"}},
    }})  # fmt: skip
    with duckdb.connect(str(root / "dl.duckdb")) as connection:
        connection.execute(SEED)
    return root


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory):
    root = _package(tmp_path_factory.mktemp("dl"), {"unknown_values": ["", "XX"]})
    engine = Runtime.from_path(str(root))
    try:
        engine._get_adapter()  # the module fixture owns its connection across tests
        yield engine
    finally:
        engine.close()


def _gold(sql: str) -> list[tuple[Any, ...]]:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED)
        return connection.execute(sql).fetchall()


def _rows(result: dict[str, Any]) -> set[tuple[Any, ...]]:
    return {(row.get(COUNTRY), int(row["installs"])) for row in result["rows"]}


def test_grouping_puts_every_unknown_spelling_in_one_unknown_group(runtime):
    result = runtime.query({"select": [INSTALLS], "group_by": [COUNTRY]})
    gold = _gold(f"SELECT {KNOWN}, SUM(installs) FROM downloads GROUP BY 1")
    assert _rows(result) == set(gold) == {("US", 15), ("DE", 4), (None, 6)}


@pytest.mark.parametrize(("missing", "test"), [(True, "IS NULL"), (False, "IS NOT NULL")])
def test_missing_reads_the_declared_spellings_as_unknown(runtime, missing, test):
    result = runtime.query(
        {"select": [INSTALLS], "where": [{"field": COUNTRY, "op": "missing", "value": missing}]}
    )
    [gold] = _gold(f"SELECT SUM(installs) FROM downloads WHERE {KNOWN} {test}")
    assert [int(row["installs"]) for row in result["rows"]] == [gold[0]]


def test_not_keeps_every_unknown_row_and_counts_them(runtime):
    result = runtime.query(
        {"select": [INSTALLS], "where": [{"field": COUNTRY, "op": "not", "value": "US"}]}
    )
    [gold] = _gold(
        f"SELECT SUM(installs) FROM downloads WHERE ({KNOWN}) <> 'US' OR ({KNOWN}) IS NULL"
    )
    assert [int(row["installs"]) for row in result["rows"]] == [gold[0]] == [10]
    [warning] = [row for row in result["warnings"] if row["code"] == "UNKNOWN_ROWS_KEPT"]
    [[kept]] = _gold(f"SELECT COUNT(*) FROM downloads WHERE {KNOWN} IS NULL")
    assert warning["details"] == {"field": COUNTRY, "rows": kept, "values": [None, "", "XX"]}


@pytest.mark.parametrize(("op", "missing"), [("is", "true"), ("not", "false")])
def test_an_unknown_spelling_in_is_or_not_is_refused_with_missing(runtime, op, missing):
    with pytest.raises(SemanticLayerError) as raised:
        runtime.query(
            {"select": [INSTALLS], "where": [{"field": COUNTRY, "op": op, "value": "XX"}]}
        )
    error = raised.value
    assert error.code == "INVALID_QUERY"
    assert error.details["path"] == "where[0].value"
    assert f'"op": "missing", "value": {missing}' in error.details["recovery_hints"][0]["message"]


@pytest.mark.parametrize(
    ("country", "defaults", "message"),
    [
        ({"unknown_values": ""}, None, "non-empty list"),
        ({"unknown_values": []}, None, "non-empty list"),
        ({"unknown_values": ["", None]}, None, "non-empty list"),
        ({}, {"dimension": {"unknown_values": [""]}}, "never as a package default"),
    ],
)
def test_unknown_values_is_a_list_declared_per_dimension(tmp_path, country, defaults, message):
    _package(tmp_path, country, defaults)
    with pytest.raises(SemanticLayerError) as raised:
        load_package_config(str(tmp_path))
    assert message in str(raised.value) + str(raised.value.details)


# A reference code in one column, "Ref Code": two downloads record an unknown code as 'UNKNOWN'
# and two leave it NULL. DuckDB reads "Ref Code" and "REF CODE" as that one column.
REFERENCES = """
ALTER TABLE downloads ADD COLUMN "Ref Code" VARCHAR;
UPDATE downloads SET "Ref Code" = CASE download_id
  WHEN 1 THEN 'R1' WHEN 2 THEN 'UNKNOWN' WHEN 4 THEN 'UNKNOWN' WHEN 6 THEN 'R6' END;
"""
SENTINEL = {"unknown_values": ["UNKNOWN"]}


def _references(
    root: Path, spellings: dict[str, dict[str, Any]], *, measure_column: str = "REF CODE"
) -> Runtime:
    """The downloads package with one dimension over each spelling of the reference column."""
    others = {
        f"ref_{index}": {"kind": "categorical", "column": column, **declared}
        for index, (column, declared) in enumerate(spellings.items())
    }
    _package(root, {}, others=others)
    model = root / "models" / "downloads.yml"
    doc = yaml.safe_load(model.read_text(encoding="utf-8"))
    # A declared measure that reads the reference column through a CASE.
    when = {
        "kind": "comparison",
        "op": "missing",
        "left": {"kind": "column", "column": measure_column},
        "right": {"kind": "literal", "value": True},
    }
    doc["model"]["measures"]["unreferenced_installs"] = {
        "kind": "aggregate", "expr": {"kind": "case", "whens": [
            {"when": when, "then": {"kind": "column", "column": "installs"}}],
            "else": {"kind": "literal", "value": 0}},
    }  # fmt: skip
    model.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with duckdb.connect(str(root / "dl.duckdb")) as connection:
        connection.execute(REFERENCES)
    return Runtime.from_path(str(root))


def _installs_where(column: str, op: str, value: Any) -> dict[str, Any]:
    ref = {"kind": "column", "entity": "entity.dl_download", "column": column}
    condition = {
        "kind": "comparison",
        "op": op,
        "left": ref,
        "right": {"kind": "literal", "value": value},
    }
    value_ref = {"kind": "column", "entity": "entity.dl_download", "column": "installs"}
    expression = {
        "kind": "aggregate_if",
        "aggregation": "sum",
        "value": value_ref,
        "condition": condition,
    }
    return {"select": [{"expression": expression, "as": "total"}]}


def _gold_references(where: str) -> int:
    with duckdb.connect(":memory:") as connection:
        connection.execute(SEED + REFERENCES)
        [[total]] = connection.execute(
            f"SELECT SUM(installs) FROM downloads WHERE {where}"
        ).fetchall()
    return int(total)


@pytest.mark.parametrize("spelling", ["Ref Code", "REF CODE", "ref code"])
@pytest.mark.parametrize(
    ("op", "value", "message"),
    [
        ("missing", True, "whose dimensions declare different unknown values"),
        ("is", "R1", "whose dimensions declare different unknown values"),
        ("contains", "R", "is the column of several dimensions"),
    ],
)
def test_a_column_read_whose_spellings_declare_different_unknown_values_is_refused(
    tmp_path, spelling, op, value, message
):
    # Without the refusal each spelling read only its own dimension: missing summed 10 in the
    # sentinel's spelling and 5 in the other, two answers for one column.
    runtime = _references(tmp_path, {"Ref Code": SENTINEL, "REF CODE": {}})
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.query(_installs_where(spelling, op, value))
    finally:
        runtime.close()
    error = raised.value
    assert error.code == "INVALID_QUERY"
    assert message in str(error)
    assert error.details["dimensions"] == [
        "dimension.dl_download_ref_0",
        "dimension.dl_download_ref_1",
    ]
    assert '"op": "' + op + '"' in error.details["recovery_hints"][0]["message"]


def test_a_declared_measure_reading_a_spelling_whose_dimensions_disagree_is_refused(tmp_path):
    # The call has no inline column to check: the compiler refuses the measure's own read.
    runtime = _references(tmp_path, {"Ref Code": SENTINEL, "REF CODE": {}})
    try:
        with pytest.raises(SemanticLayerError) as raised:
            runtime.query({"select": ["measure.dl.unreferenced_installs"]})
    finally:
        runtime.close()
    error = raised.value
    assert error.code == "INVALID_QUERY"
    assert str(error).startswith(
        "measure.dl.unreferenced_installs.left: 'missing' reads entity.dl_download.REF CODE, "
        "whose dimensions declare different unknown values"
    )
    assert error.details["dimensions"] == [
        "dimension.dl_download_ref_0",
        "dimension.dl_download_ref_1",
    ]


@pytest.mark.parametrize(
    ("declared", "where"),
    [
        (SENTINEL, '"Ref Code" IS NULL OR "Ref Code" = \'UNKNOWN\''),
        ({}, '"Ref Code" IS NULL'),
    ],
)
def test_spellings_whose_dimensions_agree_give_one_answer(tmp_path, declared, where):
    runtime = _references(tmp_path, {"Ref Code": declared, "REF CODE": declared})
    try:
        totals = {
            spelling: [
                int(row["total"])
                for row in runtime.query(_installs_where(spelling, "missing", True))["rows"]
            ]
            for spelling in ["Ref Code", "REF CODE"]
        }
        [declared_row] = runtime.query({"select": ["measure.dl.unreferenced_installs"]})["rows"]
    finally:
        runtime.close()
    gold = _gold_references(where)
    assert totals == {"Ref Code": [gold], "REF CODE": [gold]}
    assert int(declared_row["unreferenced_installs"]) == gold


def _assert_one_dimension_reads_its_unknown_values(tmp_path, spelling):
    runtime = _references(tmp_path, {"Ref Code": SENTINEL})
    try:
        missing = runtime.query(_installs_where(spelling, "missing", True))["rows"]
        contains = runtime.query(_installs_where(spelling, "contains", "R"))["rows"]
        [error] = runtime.validate(_installs_where(spelling, "is", "UNKNOWN"))["errors"]
    finally:
        runtime.close()
    assert (
        [int(row["total"]) for row in missing]
        == [_gold_references('"Ref Code" IS NULL OR "Ref Code" = \'UNKNOWN\'')]
        == [10]
    )
    assert (
        [int(row["total"]) for row in contains]
        == [_gold_references("\"Ref Code\" LIKE '%R%' AND \"Ref Code\" <> 'UNKNOWN'")]
        == [15]
    )
    assert error["code"] == "INVALID_QUERY"
    assert (
        "'UNKNOWN' is how dimension.dl_download_ref_0 records an unknown value" in error["message"]
    )


def test_one_dimension_over_its_exact_spelling_reads_its_unknown_values(tmp_path):
    _assert_one_dimension_reads_its_unknown_values(tmp_path, "Ref Code")


def test_one_dimension_over_an_undeclared_spelling_reads_its_unknown_values(tmp_path):
    _assert_one_dimension_reads_its_unknown_values(tmp_path, "ref code")


@pytest.mark.parametrize(("op", "value"), [("missing", True), ("contains", "R")])
def test_a_dimension_policy_governs_an_undeclared_spelling(tmp_path, op, value):
    dimension = "dimension.dl_download_ref"
    root = _package(
        tmp_path,
        {},
        others={
            "ref": {"kind": "categorical", "column": "Ref Code", **SENTINEL},
        },
    )
    config = load_package_config(str(root))
    policy = SemanticPolicyConfig(
        id="policy.dl.ref_denied",
        kind="object_access",
        object_ids=[dimension],
        action="deny",
    )
    runtime = Runtime.from_config(
        replace(config, semantic_policies=[policy]), source_path=str(root)
    )
    try:
        [error] = runtime.validate(_installs_where("ref code", op, value))["errors"]
    finally:
        runtime.close()
    assert error["code"] == "POLICY_DENIED"
    assert dimension in error["message"]
    assert error["details"]["blocked_objects"] == [dimension]


@pytest.mark.parametrize(
    ("kind", "action", "code"),
    [
        ("object_access", "deny", "POLICY_DENIED"),
        ("object_visibility", "hidden", "OBJECT_NOT_FOUND"),
    ],
)
def test_an_authored_comparison_records_the_undeclared_spellings_dimension(
    tmp_path, kind, action, code
):
    config = _references(tmp_path, {"Ref Code": SENTINEL})
    package = config.package_config
    config.close()
    dimension = "dimension.dl_download_ref_0"
    policy = SemanticPolicyConfig(
        id="policy.dl.restricted_ref",
        kind=kind,
        action=action,
        object_ids=[dimension],
        audiences=["restricted"],
    )
    runtime = Runtime.from_config(
        replace(package, semantic_policies=[policy]), source_path=str(tmp_path)
    )
    measure = "measure.dl.unreferenced_installs"
    query = {"select": [measure]}
    try:
        assert runtime.query(query)["rows"] == [{"unreferenced_installs": 10}]
        [error] = runtime.validate(query, context={"audience": "restricted"})["errors"]
    finally:
        runtime.close()
    assert error["code"] == code
    if action == "deny":
        assert f"{dimension} is denied" in error["message"]
    else:
        assert measure in error["message"]
        assert dimension not in repr(error)


@pytest.mark.parametrize("warehouse", ["duckdb", "postgres", "snowflake", "clickhouse", "bigquery"])
def test_a_declared_measure_and_its_metric_follow_the_compared_dimensions_policy(
    tmp_path, warehouse
):
    fixture = _references(tmp_path, {"Ref Code": SENTINEL}, measure_column="Ref Code")
    package = fixture.package_config
    fixture.close()
    dimension = "dimension.dl_download_ref_0"
    measure = "measure.dl.unreferenced_installs"
    metric = MetricConfig("metric.dl.unreferenced_installs", "aggregate", AggregateExpr(measure))
    policies = [
        SemanticPolicyConfig(
            id=f"policy.dl.{action}_ref",
            kind=kind,
            object_ids=[dimension],
            action=action,
            audiences=[action],
        )
        for kind, action in [("object_access", "deny"), ("object_visibility", "hidden")]
    ]
    config = replace(
        package,
        package=replace(package.package, warehouse=warehouse),
        metric_recipes=[metric],
        semantic_policies=policies,
    )
    runtime = Runtime.from_config(config, source_path=str(tmp_path))
    try:
        for object_id in (measure, metric.id):
            query = {"select": [object_id]}
            assert runtime.validate(query)["ok"]
            [denied] = runtime.validate(query, context={"audience": "deny"})["errors"]
            assert denied["code"] == "POLICY_DENIED"
            assert f"{dimension} is denied" in denied["message"]
            [hidden] = runtime.validate(query, context={"audience": "hidden"})["errors"]
            assert hidden["code"] == "OBJECT_NOT_FOUND"
            assert f"'{object_id}'" in hidden["message"]
            assert dimension not in repr(hidden)
    finally:
        runtime.close()


@pytest.mark.parametrize("case", ["object-policy", "conflict", "stored-before-upgrade"])
def test_a_rollup_respects_the_authored_comparison(tmp_path, case):
    declared = "REF CODE" if case == "object-policy" else "Ref Code"
    fixture = _references(tmp_path, {declared: SENTINEL})
    package = fixture.package_config
    fixture.close()
    ref_id = "dimension.dl_download_ref_0"
    day_id = "dimension.dl_download_day"
    role = TemporalRoleConfig(
        "temporal_role.dl_day", day_id, "event_time", supported_grains=["day"]
    )
    measure_id = "measure.dl.unreferenced_installs"
    date = replace(package.dimensions[0], id=day_id, column="day", data_type="date")
    measures = [
        replace(row, compatible_temporal_roles=[role.id], default_temporal_role=role.id)
        if row.id == measure_id
        else row
        for row in package.measures
    ]
    rollup = AggregateRelationConfig(
        "aggregate_relation.dl_daily",
        "downloads_daily",
        date.entity,
        temporal_role=role.id,
        grain="day",
        time_column="day",
        measure_columns={measure_id: "unreferenced_installs"},
        measure_holds={measure_id: "sum"},
    )
    policy = SemanticPolicyConfig(
        id="policy.dl.restricted_ref",
        kind="object_access",
        action="deny",
        object_ids=[ref_id],
        audiences=["restricted"],
    )
    dimensions = [*package.dimensions, date]
    if case == "conflict":
        ref = next(row for row in dimensions if row.id == ref_id)
        dimensions.append(replace(ref, id=f"{ref_id}_upper", column="Ref CODE", unknown_values=[]))
    if case == "stored-before-upgrade":
        with duckdb.connect(str(tmp_path / "dl.duckdb")) as connection:
            connection.execute("ALTER TABLE downloads ADD COLUMN day DATE DEFAULT '2025-01-01'")
            connection.execute(
                """
                CREATE TABLE downloads_daily AS
                SELECT day, SUM(CASE WHEN "Ref Code" IS NULL THEN installs ELSE 0 END)
                    AS unreferenced_installs
                FROM downloads GROUP BY day
                """
            )
            assert connection.execute(
                "SELECT unreferenced_installs FROM downloads_daily"
            ).fetchall() == [(5,)]
    config = replace(
        package,
        dimensions=dimensions,
        temporal_roles=[role],
        measures=measures,
        aggregate_relations=[rollup],
        semantic_policies=[policy],
    )
    runtime = Runtime.from_config(config, source_path=str(tmp_path))
    query = {"select": [measure_id], "time": {"temporal_role": role.id, "grain": "day"}}
    try:
        if case == "conflict":
            with pytest.raises(SemanticLayerError) as raised:
                runtime.compile(query)
            assert raised.value.code == "INVALID_QUERY"
            assert "whose dimensions declare different unknown values" in str(raised.value)
            assert raised.value.details["dimensions"] == [ref_id, f"{ref_id}_upper"]
            return
        if case == "stored-before-upgrade":
            assert runtime.query(query)["rows"][0]["unreferenced_installs"] == 10
            assert "downloads_daily" not in runtime.compile(query)["rendered_sql"]
            return
        assert "downloads_daily" in runtime.compile(query)["rendered_sql"]
        [error] = runtime.validate(query, context={"audience": "restricted"})["errors"]
    finally:
        runtime.close()
    assert error["code"] == "POLICY_DENIED"
    assert f"{ref_id} is denied" in error["message"]


@pytest.mark.parametrize("spelling", ["Ref Code", "REF CODE", "ref code"])
@pytest.mark.parametrize(
    "equivalent", [["", "UNKNOWN"], ["UNKNOWN", "UNKNOWN", ""]], ids=["reordered", "repeated"]
)
def test_unknown_values_agree_regardless_of_order_and_repetition(tmp_path, spelling, equivalent):
    runtime = _references(
        tmp_path,
        {
            "Ref Code": {"unknown_values": ["UNKNOWN", ""]},
            "REF CODE": {"unknown_values": equivalent},
        },
    )
    try:
        gold = _gold_references("(\"Ref Code\" IS NULL OR \"Ref Code\" IN ('UNKNOWN', ''))")
        assert runtime.query(_installs_where(spelling, "missing", True))["rows"] == [
            {"total": gold}
        ]
    finally:
        runtime.close()


def test_distinct_numeric_unknown_literals_conflict(tmp_path):
    runtime = _references(
        tmp_path,
        {"Ref Code": {"unknown_values": [1]}, "REF CODE": {"unknown_values": [1.0]}},
    )
    try:
        with duckdb.connect(str(tmp_path / "dl.duckdb")) as connection:
            connection.execute("DELETE FROM downloads")
            connection.execute(
                "INSERT INTO downloads VALUES (1, 'US', 10, '1.4'), (2, 'US', 5, '1.0')"
            )
        for spelling in ("Ref Code", "REF CODE", "ref code"):
            with pytest.raises(SemanticLayerError) as raised:
                runtime.query(_installs_where(spelling, "missing", True))
            assert raised.value.code == "INVALID_QUERY"
            assert "whose dimensions declare different unknown values" in str(raised.value)
            assert raised.value.details["dimensions"] == [
                "dimension.dl_download_ref_0",
                "dimension.dl_download_ref_1",
            ]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        ("Day Code", "\"Day Code\" = '2025-01-01'"),
        ("DAY CODE", "\"DAY CODE\" = TIMESTAMP '2025-01-01'"),
        ("day code", None),
    ],
)
def test_athena_comparison_types_follow_the_declared_column_spelling(tmp_path, spelling, expected):
    root = _package(
        tmp_path,
        {},
        others={
            "day_text": {"kind": "categorical", "column": "Day Code"},
            "day_date": {"kind": "date", "column": "DAY CODE"},
            "delivery_date": {"kind": "date", "column": "Delivery Day"},
        },
    )
    config = load_package_config(str(root))
    config = replace(config, package=replace(config.package, warehouse="athena"))
    runtime = Runtime.from_config(config, source_path=str(root))
    try:
        query = _installs_where(spelling, "is", "2025-01-01")
        if expected is None:
            with pytest.raises(SemanticLayerError) as raised:
                runtime.compile(query)
            assert raised.value.code == "INVALID_QUERY"
            assert "disagree on whether to compare text values as dates or timestamps" in str(
                raised.value
            )
            assert raised.value.details["dimensions"] == [
                "dimension.dl_download_day_date",
                "dimension.dl_download_day_text",
            ]
            assert (
                '"day code" IS NULL'
                in runtime.compile(_installs_where(spelling, "missing", True))["rendered_sql"]
            )
        else:
            assert expected in runtime.compile(query)["rendered_sql"]
        assert (
            "\"delivery day\" = TIMESTAMP '2025-01-01'"
            in runtime.compile(_installs_where("delivery day", "is", "2025-01-01"))["rendered_sql"]
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("warehouse", ["databricks", "athena", "bigquery"])
def test_folded_reserved_column_spellings_compile_with_the_same_unknown_values(tmp_path, warehouse):
    root = _package(
        tmp_path, {}, others={"ref": {"kind": "categorical", "column": "Select", **SENTINEL}}
    )
    config = load_package_config(str(root))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    runtime = Runtime.from_config(config, source_path=str(root))
    try:
        declared = runtime.compile(_installs_where("Select", "missing", True))["rendered_sql"]
        folded = runtime.compile(_installs_where("select", "missing", True))["rendered_sql"]
        assert "'UNKNOWN'" in declared
        assert folded == declared.replace("Select", "select")
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("spellings", "joined"),
    [
        pytest.param(("Customer Ref", "customer ref"), False, id="quoted"),
        pytest.param(("Select", "select"), False, id="reserved"),
        pytest.param(("Customer Ref", "customer ref"), True, id="joined-table"),
    ],
)
@pytest.mark.parametrize("conflict", [False, True], ids=["agree", "conflict"])
def test_undeclared_spelling_uses_the_columns_unknown_values(tmp_path, spellings, joined, conflict):
    declared, undeclared = spellings
    root = _package(tmp_path, {})
    config = load_package_config(str(root))
    download = "entity.dl_download"
    if joined:
        customer = "entity.dl_customer"
        config = replace(
            config,
            entities=[*config.entities, EntityConfig(customer, "customers", ["id"])],
            relationships=[
                *config.relationships,
                RelationshipConfig(
                    "relationship.dl_download_customer",
                    download,
                    customer,
                    "download_id",
                    "id",
                    "n:1",
                    "safe",
                ),
            ],
        )
    dimension = replace(
        config.dimensions[0],
        id="dimension.dl_ref",
        entity=customer if joined else download,
        column=declared,
        unknown_values=["UNKNOWN"],
    )
    alternate = replace(
        dimension,
        id="dimension.dl_ref_upper",
        column=declared.upper(),
        unknown_values=[] if conflict else ["UNKNOWN"],
    )
    config = replace(config, dimensions=[*config.dimensions, dimension, alternate])
    with duckdb.connect(str(root / "dl.duckdb")) as connection:
        connection.execute("DELETE FROM downloads")
        connection.execute(
            "INSERT INTO downloads VALUES (1, NULL, 5), (2, NULL, 10), (3, NULL, 20)"
        )
        if joined:
            connection.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO customers SELECT download_id FROM downloads")
            table, key = "customers", "id"
        else:
            table, key = "downloads", "download_id"
        connection.execute(f'ALTER TABLE {table} ADD COLUMN "{declared}" VARCHAR')
        connection.execute(
            f"UPDATE {table} SET \"{declared}\" = CASE {key} WHEN 2 THEN 'UNKNOWN' WHEN 3 THEN 'R1' END"
        )
        source = "downloads JOIN customers ON download_id = id" if joined else "downloads"
        predicate = (
            f'"{declared}" IS NOT NULL AND "{declared}" <> \'UNKNOWN\''
            if joined
            else f'("{declared}" IS NULL OR "{declared}" = \'UNKNOWN\')'
        )
        [(gold,)] = connection.execute(
            f"SELECT SUM(installs) FROM {source} WHERE {predicate}"
        ).fetchall()

    runtime = Runtime.from_config(config, source_path=str(root))
    try:
        for spelling in (declared, undeclared):
            query = _installs_where(spelling, "missing", not joined)
            if joined:
                query["select"][0]["expression"]["condition"]["left"] = {
                    "kind": "column",
                    "table": "customers",
                    "column": spelling,
                }
            if conflict:
                with pytest.raises(SemanticLayerError) as raised:
                    runtime.query(query)
                assert raised.value.code == "INVALID_QUERY"
                assert "whose dimensions declare different unknown values" in str(raised.value)
                assert raised.value.details["dimensions"] == [dimension.id, alternate.id]
            else:
                assert runtime.query(query)["rows"] == [{"total": gold}]
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("warehouse", "identifiers"),
    [
        *(
            (name, ('"Customer Ref"', '"customer ref"'))
            for name in ("postgres", "snowflake", "clickhouse")
        ),
        ("bigquery", ("`Customer_Ref_", "`customer_ref_")),
    ],
)
def test_quoted_spellings_for_case_sensitive_warehouses_read_distinct_columns(
    tmp_path, warehouse, identifiers
):
    root = _package(
        tmp_path,
        {},
        others={
            "ref": {"kind": "categorical", "column": "Customer Ref", **SENTINEL},
        },
    )
    config = load_package_config(str(root))
    config = replace(config, package=replace(config.package, warehouse=warehouse))
    runtime = Runtime.from_config(config, source_path=str(root))
    try:
        declared = runtime.compile(_installs_where("Customer Ref", "missing", True))["rendered_sql"]
        different = runtime.compile(_installs_where("customer ref", "missing", True))[
            "rendered_sql"
        ]
    finally:
        runtime.close()
    assert identifiers[0] in declared
    assert identifiers[1] in different
    assert "'UNKNOWN'" in declared
    assert "'UNKNOWN'" not in different
