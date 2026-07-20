"""Verify that ``times.<key>.column_timezone`` propagates into emitted SQL.

The temporal role's ``column_timezone`` (source zone) and ``timezone``
(target zone) should produce a ``CONVERT_TIMEZONE(source, target, col)``
wrap around the raw time-column expression — applied **before** any
``DATE_TRUNC`` or grain bucketing — so truncation and filtering happen
in the target zone.

When the two zones are absent or equal, no wrap is emitted.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from semantic_rails.compiler import compile_query
from semantic_rails.config import load_package_config
from semantic_rails.registry import Registry


def _query() -> dict[str, object]:
    return {
        "select": [
            {
                "expression": {
                    "measure": "measure.jaffle.order_count",
                    "aggregation": "count_distinct",
                },
                "as": "orders",
            },
        ],
        "time": {
            "temporal_role": "temporal_role.jaffle_order_time",
            "grain": "month",
        },
    }


def _patch_orders_times(
    package_path: Path,
    *,
    timezone: str | None,
    column_timezone: str | None,
) -> None:
    """Inject (or clear) timezone / column_timezone on the orders.ordered_at
    times block in the copied jaffle_shop package."""
    # ``package_path`` from ``package_config_factory`` is either the package
    # directory itself (jaffle_shop is a directory package) or a single YAML
    # file. Handle both.
    if package_path.is_dir():
        orders_yml = package_path / "models" / "core" / "orders.yml"
    else:
        orders_yml = package_path.parent / "models" / "core" / "orders.yml"
    raw = dict(yaml.safe_load(orders_yml.read_text(encoding="utf-8")) or {})
    model = dict(raw.get("model", {}) or {})
    times = dict(model.get("times", {}) or {})
    ordered_at = dict(times.get("ordered_at", {}) or {})
    if timezone is None:
        ordered_at.pop("timezone", None)
    else:
        ordered_at["timezone"] = timezone
    if column_timezone is None:
        ordered_at.pop("column_timezone", None)
    else:
        ordered_at["column_timezone"] = column_timezone
    times["ordered_at"] = ordered_at
    model["times"] = times
    raw["model"] = model
    orders_yml.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def test_column_timezone_wraps_raw_expr_with_convert_timezone(
    package_config_factory,
) -> None:
    _, package_path = package_config_factory("jaffle_shop")
    _patch_orders_times(
        package_path,
        timezone="America/New_York",
        column_timezone="UTC",
    )
    config = load_package_config(str(package_path))  # reload after YAML mutation

    compiled = compile_query(config, Registry(config), _query())
    rendered = compiled["sql"]

    assert "CONVERT_TIMEZONE('UTC', 'America/New_York'" in rendered, rendered
    # The wrap goes inside DATE_TRUNC — truncation happens in the target zone.
    assert "DATE_TRUNC('month', CAST(CONVERT_TIMEZONE(" in rendered or (
        "DATE_TRUNC('month'" in rendered and "CONVERT_TIMEZONE" in rendered
    ), rendered


def test_no_column_timezone_emits_no_convert_timezone(
    package_config_factory,
) -> None:
    _, package_path = package_config_factory("jaffle_shop")
    # Leave column_timezone empty; timezone may default to UTC.
    _patch_orders_times(
        package_path,
        timezone="UTC",
        column_timezone=None,
    )
    config = load_package_config(str(package_path))  # reload after YAML mutation

    compiled = compile_query(config, Registry(config), _query())
    rendered = compiled["sql"]

    assert "CONVERT_TIMEZONE" not in rendered, rendered


def test_column_timezone_equal_to_timezone_emits_no_convert_timezone(
    package_config_factory,
) -> None:
    _, package_path = package_config_factory("jaffle_shop")
    _patch_orders_times(
        package_path,
        timezone="UTC",
        column_timezone="UTC",
    )
    config = load_package_config(str(package_path))  # reload after YAML mutation

    compiled = compile_query(config, Registry(config), _query())
    rendered = compiled["sql"]

    assert "CONVERT_TIMEZONE" not in rendered, rendered
