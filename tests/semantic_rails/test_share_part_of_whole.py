"""A ratio of one population under entity filters divides its operands' own numbers.

A ratio whose numerator keeps every one of the denominator's entity filters and adds its own
is a part of a whole, read in one pass over the snapshot rows. Any other pair is not: read
that way, the numerator took both operands' filters, so the answer read 0 where each operand
read 1, and a group only the numerator had was left out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest

from semantic_rails.runtime import Runtime

ROWS = "('X', TIMESTAMP '2020-01-05', 100), ('Y', TIMESTAMP '2020-01-20', 200)"
MEMBERS = {
    "kind": "scoped_aggregate",
    "measure": "measure.x.members",
    "aggregation": "count_distinct",
}
JANUARY = {"temporal_role": "temporal_role.x_snap_ts", "start": "2020-01-01", "end": "2020-02-01"}
BY_ACCOUNT = ["dimension.x_snap_id"]


@pytest.fixture()
def runtime(tmp_path: Path):
    (tmp_path / "models").mkdir()
    (tmp_path / "package.yml").write_text(
        "schema_version: 1\n"
        "package: {id: x, namespace: x, name: x, warehouse: duckdb, default_db: x.duckdb,\n"
        "  seed: {kind: external}, environments: [development]}\n"
    )
    (tmp_path / "graph.yml").write_text(
        "graph:\n  entities:\n"
        "    snap: {key: [id, ts], model: snap}\n"
        "    account: {key: [id], model: accounts}\n"
    )
    (tmp_path / "models" / "snap.yml").write_text(
        "model:\n  id: snap\n  relation: snap\n  entities: {snap: {}, account: {}}\n"
        "  dimensions: {id: {kind: categorical}}\n"
        "  times:\n    ts: {column: ts, kind: timestamp, class: as_of_time, default: true}\n"
        "  measures:\n"
        "    flow: {kind: aggregate, expr: value, default_agg: sum,\n"
        "      accumulation: {kind: flow}, value_type: number}\n"
        "    members: {kind: entity_count, entity_key: id, accumulation: {kind: population}}\n"
    )
    (tmp_path / "models" / "accounts.yml").write_text(
        "model:\n  id: accounts\n  relation: accounts\n  entities: {account: {}}\n"
    )
    with duckdb.connect(str(tmp_path / "x.duckdb")) as con:
        con.execute("CREATE TABLE snap (id VARCHAR, ts TIMESTAMP, value INTEGER); "
                    f"INSERT INTO snap VALUES {ROWS}; "
                    "CREATE TABLE accounts AS SELECT DISTINCT id FROM snap")  # fmt: skip
    runtime = Runtime.from_path(str(tmp_path))
    yield runtime
    runtime.close()


def _flow(op: str, value: int) -> dict[str, Any]:
    return {"entity": "entity.x_account", "input": "measure.x.flow", "op": op, "value": value,
            "over": "all_time"}  # fmt: skip


def _members(*predicates: dict[str, Any]) -> dict[str, Any]:
    return {**MEMBERS, "predicates": list(predicates)} if predicates else dict(MEMBERS)


def _call(expression: dict[str, Any], **parts: Any) -> dict[str, Any]:
    return {"select": [{"expression": expression, "as": "v"}], "time": JANUARY, **parts}


def _answer(runtime: Runtime, expression: dict[str, Any], **parts: Any) -> list[tuple[Any, ...]]:
    rows = runtime.query(_call(expression, **parts))["rows"]
    return sorted(tuple(row.get(key) for key in ("dimension.x_snap_id", "v")) for row in rows)


def _anchored(runtime: Runtime, expression: dict[str, Any], **parts: Any) -> bool:
    optimizations = runtime.compile(_call(expression, **parts))["explain"]["physical_plan"]
    return any(row["kind"] == "anchored_entity_set" for row in optimizations["optimizations"])


def test_a_numerator_without_the_denominators_filter_reads_its_own_members(runtime: Runtime):
    part = _members(_flow("is", 200), _flow("gt", 0))
    whole = _members(_flow("is", 100))
    ratio = {"kind": "ratio", "numerator": part, "denominator": whole}
    assert _answer(runtime, part) == [(None, 1)]
    assert _answer(runtime, whole) == [(None, 1)]
    assert _answer(runtime, ratio) == [(None, 1.0)]
    assert not _anchored(runtime, ratio)


def test_a_group_only_the_numerator_has_stays_with_a_null_denominator(runtime: Runtime):
    part = _members(_flow("is", 100))
    whole = _members(_flow("is", 200))
    ratio = {"kind": "ratio", "numerator": part, "denominator": whole}
    assert _answer(runtime, part, group_by=BY_ACCOUNT) == [("X", 1)]
    assert _answer(runtime, whole, group_by=BY_ACCOUNT) == [("Y", 1)]
    assert _answer(runtime, ratio, group_by=BY_ACCOUNT) == [("X", None), ("Y", None)]


@pytest.mark.parametrize(
    ("whole", "total", "by_account"),
    [
        ([], 0.5, [("X", 0.0), ("Y", 1.0)]),
        ([_flow("gt", 0)], 0.5, [("X", 0.0), ("Y", 1.0)]),
        ([_flow("gt", 150)], 1.0, [("Y", 1.0)]),
    ],
)
def test_a_part_of_the_whole_keeps_its_one_pass_share(
    runtime: Runtime, whole: list[dict[str, Any]], total: float, by_account: list[Any]
):
    ratio = {
        "kind": "ratio",
        "numerator": _members(*whole, _flow("is", 200)),
        "denominator": _members(*whole),
    }
    assert _anchored(runtime, ratio)
    assert _answer(runtime, ratio) == [(None, total)]
    assert _answer(runtime, ratio, group_by=BY_ACCOUNT) == by_account
