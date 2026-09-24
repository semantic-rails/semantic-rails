"""Run with ``python -I`` in a fresh core-only wheel installation.

This checks the wheel dependency closure, not the developer environment's extras.
"""

from __future__ import annotations

import dataclasses
import zoneinfo
from importlib.metadata import requires, version
from zoneinfo import ZoneInfo, reset_tzpath

import duckdb

import semantic_rails
from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config, resolve_repo_path
from semantic_rails.registry import Registry


def main() -> None:
    assert "site-packages" in str(semantic_rails.__file__).lower()
    assert any(
        requirement.lower().startswith("tzdata") and ";" not in requirement
        for requirement in requires("semantic-rails") or []
    )
    reset_tzpath([])
    ZoneInfo.clear_cache()
    assert not zoneinfo.TZPATH

    config = load_package_config(resolve_repo_path("configs/semantic_rails/jaffle_shop"))
    config = dataclasses.replace(
        config,
        temporal_roles=[
            dataclasses.replace(role, timezone="America/New_York")
            if role.id == "temporal_role.jaffle_order_time"
            else role
            for role in config.temporal_roles
        ],
    )
    registry = Registry(config)
    windows = [
        ("2017-07-01", "2017-07-03", ["2017-07-01", "2017-07-02"]),
        (
            "2017-07-01T23:30:00-07:00",
            "2017-07-02T23:30:00-07:00",
            ["2017-07-02", "2017-07-03"],
        ),
    ]
    with duckdb.connect(":memory:") as connection:
        connection.execute("CREATE TABLE jaffle_order (order_id VARCHAR, ordered_at TIMESTAMP)")
        connection.execute(
            "CREATE TABLE jaffle_calendar (date_day DATE, week_start DATE, "
            "month_start DATE, quarter_start DATE, year_start DATE)"
        )
        connection.execute(
            "INSERT INTO jaffle_calendar (date_day) VALUES "
            "('2017-07-01'), ('2017-07-02'), ('2017-07-03')"
        )
        for start, end, expected in windows:
            query = {
                "version": 1,
                "select": [
                    {"expression": {"measure": "measure.jaffle.order_count"}, "as": "orders"}
                ],
                "time": {
                    "temporal_role": "temporal_role.jaffle_order_time",
                    "grain": "day",
                    "start": start,
                    "end": end,
                    "fill": True,
                },
            }
            sql = compile_query(config, registry, query)["sql"]
            rows = connection.execute(
                'SELECT CAST("temporal_role.jaffle_order_time__day" AS DATE), orders '
                f"FROM ({sql}) AS core_result ORDER BY 1"
            ).fetchall()
            assert [day.isoformat() for day, _ in rows] == expected, (start, end, rows)
            assert all(count == 0 for _, count in rows)

    print(f"core wheel: {semantic_rails.__file__}")
    print(f"tzdata: {version('tzdata')}")
    print("empty TZPATH: DATE-only and offset-bound New York windows passed")


if __name__ == "__main__":
    main()
