"""``temporal_validity.as_of: end_of_day``: a hop into a history reads the version in force at
the end of each row's day, on the query's clock, instead of at the row's own time.

Fixture: billing writes a team's first version a moment after its signup. Team 1 signs up
2024-01-10 03:00 UTC, which is still January 9 in New York; its first version (Pro) starts
5 ms later and ends at 06:00 UTC, after New York's January 9 ends (05:00 UTC) but before
UTC's January 10 does, when Builder takes over. Team 2 signs up 2024-01-10 10:00 UTC on Pro
(5 ms later) and upgrades to Builder at 15:00 the same day. The gold values come from SQL
written without the engine, read with scalar subqueries at each signup's day end.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from semantic_rails.runtime import Runtime

SEED_SQL = """
CREATE TABLE signups (signup_id INTEGER, team_id INTEGER, signed_up_at TIMESTAMP,
  signed_up_on DATE);
INSERT INTO signups VALUES
  (1, 1, TIMESTAMP '2024-01-10 03:00:00', DATE '2024-01-10'),
  (2, 2, TIMESTAMP '2024-01-10 10:00:00', DATE '2024-01-10');
CREATE TABLE billing (team_id INTEGER, plan VARCHAR, valid_from TIMESTAMP, valid_to TIMESTAMP);
INSERT INTO billing VALUES
  (1, 'Pro', TIMESTAMP '2024-01-10 03:00:00.005', TIMESTAMP '2024-01-10 06:00:00'),
  (1, 'Builder', TIMESTAMP '2024-01-10 06:00:00', NULL),
  (2, 'Pro', TIMESTAMP '2024-01-10 10:00:00.005', TIMESTAMP '2024-01-10 15:00:00'),
  (2, 'Builder', TIMESTAMP '2024-01-10 15:00:00', NULL)
"""

FILES = {
    "package.yml": """
        schema_version: 1
        package:
          id: eod
          namespace: eod
          warehouse: duckdb
          default_db: data/eod.duckdb
          seed: {kind: sql_script, source: data/seed.sql}
        """,
    "graph.yml": """
        graph:
          entities:
            signup: {label: Signup, key: [signup_id], model: signups}
            billing_version: {label: Billing version, key: [team_id, valid_from],
              model: billing}
          relationships:
            signup_billing:
              entities: [signup, billing_version]
              cardinality: many_to_one
              via: [team_id]
              target: [team_id]
              temporal_validity:
                valid_from: billing.valid_from
                valid_to: billing.valid_to
                as_of: {as_of}
        """,
    "models/signups.yml": """
        model:
          id: signups
          relation: signups
          entities: {signup: {}}
          times:
            utc: {label: Signed up (UTC), column: signed_up_at, kind: timestamp,
              class: event_time, default: true}
            new_york: {label: Signed up (New York), column: signed_up_at, kind: timestamp,
              class: event_time, timezone: America/New_York, column_timezone: UTC}
            day: {label: Signup day, column: signed_up_on, kind: date, class: event_time}
          measures:
            signups: {label: Signups, kind: entity_count, entity_key: signup_id,
              accumulation: {kind: flow}, times: [utc, new_york, day]}
        """,
    "models/billing.yml": """
        model:
          id: billing
          relation: billing
          entities: {billing_version: {}}
          dimensions:
            plan: {label: Plan, kind: categorical}
        """,
}

# The plan in force at each signup's day end: a version starting before it and not ended by it.
GOLD = """
SELECT (SELECT b.plan FROM billing AS b WHERE b.team_id = s.team_id
          AND b.valid_from < {end} AND (b.valid_to >= {end} OR b.valid_to IS NULL)) AS plan,
       COUNT(*)
FROM signups AS s GROUP BY 1
"""
UTC_END = "date_trunc('day', s.signed_up_at) + INTERVAL 1 DAY"
NEW_YORK_END = (
    "timezone('UTC', timezone('America/New_York', date_trunc('day', "
    "timezone('America/New_York', timezone('UTC', s.signed_up_at))) + INTERVAL 1 DAY))"
)


def _runtime(root: Path, as_of: str) -> Runtime:
    package = root / as_of / "eod"
    (package / "data").mkdir(parents=True)
    (package / "data" / "seed.sql").write_text(SEED_SQL)
    for name, body in FILES.items():
        (package / name).parent.mkdir(exist_ok=True)
        (package / name).write_text(textwrap.dedent(body).replace("{as_of}", as_of))
    return Runtime.from_path(str(package))


def _plans(runtime: Runtime, clock: str) -> list[tuple[Any, ...]]:
    query = {
        "select": [{"expression": "measure.eod.signups", "as": "signups"}],
        "group_by": ["dimension.eod_billing_version_plan"],
        "time": {"temporal_role": f"temporal_role.eod_signup_{clock}", "end": "2024-02-01"},
    }
    return sorted((tuple(row.values()) for row in runtime.query(query)["rows"]), key=repr)


def _gold(runtime: Runtime, end: str) -> list[tuple[Any, ...]]:
    rows = runtime._get_adapter().query(GOLD.format(end=end))
    return sorted((tuple(row.values()) for row in rows), key=repr)


@pytest.mark.parametrize(
    ("clock", "end", "expected"),
    [
        # UTC's January 10 ends after team 1's Builder and team 2's Builder start.
        ("utc", UTC_END, [("Builder", 2)]),
        # New York's January 9 ends at 05:00 UTC, while team 1 is still on Pro.
        ("new_york", NEW_YORK_END, [("Builder", 1), ("Pro", 1)]),
        # A DATE clock's day is the date itself.
        ("day", "CAST(s.signed_up_on AS TIMESTAMP) + INTERVAL 1 DAY", [("Builder", 2)]),
    ],
)
def test_the_version_in_force_when_the_rows_day_ends_on_the_querys_clock(
    tmp_path: Path, clock: str, end: str, expected: list[tuple[Any, ...]]
) -> None:
    runtime = _runtime(tmp_path, "end_of_day")
    try:
        assert _plans(runtime, clock) == _gold(runtime, end) == expected
    finally:
        runtime.close()


def test_at_the_rows_time_a_version_written_after_it_reads_none(tmp_path: Path) -> None:
    """The default, ``as_of: time``: both signups precede their first version by 5 ms."""
    runtime = _runtime(tmp_path, "time")
    try:
        assert _plans(runtime, "utc") == [(None, 2)]
    finally:
        runtime.close()
