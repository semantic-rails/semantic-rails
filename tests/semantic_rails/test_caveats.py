from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.config_validation import resolve_package_reference
from semantic_rails.errors import SemanticLayerError
from semantic_rails.package_tools import diff_package_report, package_manifest
from semantic_rails.runtime import Runtime
from tests.semantic_rails.conftest import copy_package_config

ORDER_TIME = "temporal_role.jaffle_order_time"
REVENUE = "measure.jaffle.revenue_usd"
STORE_NAME = "dimension.jaffle_store_name"


def _write_caveats(package_dir: Path, rows: list[dict]) -> None:
    (package_dir / "caveats.yml").write_text(
        yaml.safe_dump({"semantic_caveats": rows}, sort_keys=False),
        encoding="utf-8",
    )


def _runtime_with_caveats(tmp_path: Path, rows: list[dict]) -> Runtime:
    package_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    _write_caveats(package_dir, rows)
    return Runtime.from_path(str(package_dir))


def _revenue_query(**overrides):
    payload = {
        "version": 1,
        "select": [{"expression": {"measure": REVENUE}, "as": "revenue"}],
        "time": {
            "temporal_role": ORDER_TIME,
            "grain": "month",
            "start": "2017-03-01",
            "end": "2017-04-01",
        },
    }
    payload.update(overrides)
    return payload


def _revenue_caveat(**overrides) -> dict:
    row = {
        "id": "caveat.test.revenue_march",
        "kind": "business_event",
        "message": "March pricing event affects revenue interpretation.",
        "object_ids": [REVENUE],
        "time": {"from": "2017-03-01", "to": "2017-04-01"},
        "severity": "warning",
    }
    row.update(overrides)
    return row


def _caveat_codes(payload: dict) -> list[str]:
    return [str(row.get("code", "")) for row in payload.get("warnings", []) or []]


def _applied_caveats(payload: dict) -> list[dict]:
    return [
        row
        for row in payload.get("warnings", []) or []
        if row.get("code") == "SEMANTIC_CAVEAT_APPLIED"
    ]


def _rounded_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        normalized = {}
        for key, value in row.items():
            normalized[key] = round(value, 6) if isinstance(value, float) else value
        out.append(normalized)
    return out


def test_jaffle_caveat_fixture_loads() -> None:
    config = load_package_config("configs/semantic_rails/jaffle_shop")

    assert len(config.semantic_caveats) == 1
    caveat = config.semantic_caveats[0]
    assert caveat.kind == "business_event"
    assert caveat.entity_values[0]["value"] == "Brooklyn"


def test_invalid_caveat_kind_time_and_object_refs_fail(tmp_path: Path) -> None:
    package_dir = copy_package_config(tmp_path, "jaffle_shop")
    _write_caveats(package_dir, [_revenue_caveat(kind="pricing_change")])
    with pytest.raises(SemanticLayerError, match="kind must be one of"):
        load_package_config(str(package_dir))

    _write_caveats(package_dir, [_revenue_caveat(time={"at": "2017-03-01", "from": "2017-03-01"})])
    with pytest.raises(SemanticLayerError, match="either at or from/to"):
        load_package_config(str(package_dir))

    _write_caveats(package_dir, [_revenue_caveat(object_ids=["measure.jaffle.nope"])])
    with pytest.raises(SemanticLayerError, match="unknown object_id"):
        load_package_config(str(package_dir))


def test_relevant_caveat_surfaces_on_validate_compile_and_query(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(tmp_path, [_revenue_caveat()])
    try:
        query = _revenue_query()

        validated = runtime.validate(query)
        compiled = runtime.compile(query)
        queried = runtime.query(query)

        for payload in (validated, compiled, queried):
            assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(payload)
            warning = _applied_caveats(payload)[0]
            assert warning["details"]["caveat_id"] == "caveat.test.revenue_march"
            assert warning["details"]["kind"] == "business_event"
    finally:
        runtime.close()


def test_caveats_do_not_change_sql_or_rows(tmp_path: Path) -> None:
    with_caveat = _runtime_with_caveats(tmp_path, [_revenue_caveat()])
    without_caveat_dir = copy_package_config(tmp_path, "jaffle_shop", preseed_db=True)
    (without_caveat_dir / "caveats.yml").unlink()
    without_caveat = Runtime.from_path(str(without_caveat_dir))
    try:
        query = _revenue_query()
        with_compiled = with_caveat.compile(query)
        without_compiled = without_caveat.compile(query)
        with_queried = with_caveat.query(query)
        without_queried = without_caveat.query(query)

        assert with_compiled["rendered_sql"] == without_compiled["rendered_sql"]
        assert _rounded_rows(with_queried["rows"]) == _rounded_rows(without_queried["rows"])
    finally:
        with_caveat.close()
        without_caveat.close()


def test_time_bound_caveat_does_not_fire_without_query_time(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(tmp_path, [_revenue_caveat()])
    try:
        result = runtime.validate(
            {"version": 1, "select": [{"expression": {"measure": REVENUE}, "as": "revenue"}]}
        )

        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(result)
    finally:
        runtime.close()


def test_entity_value_caveat_avoids_generic_revenue_spam(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(
        tmp_path,
        [
            {
                "id": "caveat.test.brooklyn",
                "kind": "business_event",
                "message": "Brooklyn had a partial operating period.",
                "object_ids": [STORE_NAME],
                "entity_values": [
                    {
                        "entity": "entity.jaffle_store",
                        "dimension": STORE_NAME,
                        "value": "Brooklyn",
                    }
                ],
                "time": {"from": "2017-03-01", "to": "2017-04-01"},
            }
        ],
    )
    try:
        generic = runtime.validate(_revenue_query())
        grouped = runtime.validate(_revenue_query(group_by=[STORE_NAME]))
        filtered = runtime.validate(
            _revenue_query(where=[{"field": STORE_NAME, "op": "=", "value": "Brooklyn"}])
        )
        other_store = runtime.validate(
            _revenue_query(where=[{"field": STORE_NAME, "op": "=", "value": "Philadelphia"}])
        )

        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(generic)
        assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(grouped)
        assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(filtered)
        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(other_store)
    finally:
        runtime.close()


def test_audience_and_environment_scope_suppress_caveats(tmp_path: Path) -> None:
    caveat = _revenue_caveat(audiences=["internal"], environments=["production"])
    runtime = _runtime_with_caveats(tmp_path, [caveat])
    try:
        missing_context = runtime.validate(_revenue_query())
        wrong_context = runtime.validate(
            _revenue_query(policy_context={"audience": "external", "environment": "production"})
        )
        matching_context = runtime.validate(
            _revenue_query(policy_context={"audience": "internal", "environment": "production"})
        )

        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(missing_context)
        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(wrong_context)
        assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(matching_context)
    finally:
        runtime.close()


def test_half_open_range_endpoints_do_not_false_positive(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(tmp_path, [_revenue_caveat()])
    try:
        before = runtime.validate(
            _revenue_query(
                time={"temporal_role": ORDER_TIME, "start": "2017-02-01", "end": "2017-03-01"}
            )
        )
        after = runtime.validate(
            _revenue_query(
                time={"temporal_role": ORDER_TIME, "start": "2017-04-01", "end": "2017-05-01"}
            )
        )

        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(before)
        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(after)
    finally:
        runtime.close()


def test_point_caveat_triggers_on_containment_and_comparison_crossing(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(
        tmp_path,
        [_revenue_caveat(id="caveat.test.point", time={"at": "2017-03-01"})],
    )
    try:
        contained = runtime.validate(_revenue_query())
        crossed = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": REVENUE}, "as": "current"},
                    {
                        "expression": {
                            "kind": "prior_period",
                            "measure": REVENUE,
                            "offset": -1,
                            "grain": "month",
                        },
                        "as": "prior",
                    },
                ],
                "time": {"temporal_role": ORDER_TIME, "grain": "month", "end": "2017-04-01"},
            }
        )

        assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(contained)
        assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(crossed)
    finally:
        runtime.close()


def test_comparison_windows_bookending_caveat_range_trigger(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(tmp_path, [_revenue_caveat()])
    try:
        bookended = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": REVENUE}, "as": "current"},
                    {
                        "expression": {
                            "kind": "prior_period",
                            "measure": REVENUE,
                            "offset": -2,
                            "grain": "month",
                        },
                        "as": "prior",
                    },
                ],
                "time": {"temporal_role": ORDER_TIME, "grain": "month", "end": "2017-05-01"},
            }
        )
        same_side = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"expression": {"measure": REVENUE}, "as": "current"},
                    {
                        "expression": {
                            "kind": "prior_period",
                            "measure": REVENUE,
                            "offset": -1,
                            "grain": "month",
                        },
                        "as": "prior",
                    },
                ],
                "time": {"temporal_role": ORDER_TIME, "grain": "month", "end": "2017-07-01"},
            }
        )

        assert "SEMANTIC_CAVEAT_APPLIED" in _caveat_codes(bookended)
        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(same_side)
    finally:
        runtime.close()


def test_broad_scalar_window_suppresses_short_contained_caveat(tmp_path: Path) -> None:
    runtime = _runtime_with_caveats(tmp_path, [_revenue_caveat()])
    try:
        result = runtime.validate(
            _revenue_query(
                time={
                    "temporal_role": ORDER_TIME,
                    "start": "2017-01-01",
                    "end": "2017-12-31",
                }
            )
        )

        assert "SEMANTIC_CAVEAT_APPLIED" not in _caveat_codes(result)
    finally:
        runtime.close()


def test_caveat_warning_cap_emits_truncation_summary(tmp_path: Path) -> None:
    rows = [_revenue_caveat(id=f"caveat.test.cap_{index}") for index in range(6)]
    runtime = _runtime_with_caveats(tmp_path, rows)
    try:
        result = runtime.validate(_revenue_query(verbosity="compact"))

        assert len(_applied_caveats(result)) == 5
        assert "SEMANTIC_CAVEATS_TRUNCATED" in _caveat_codes(result)
    finally:
        runtime.close()


def test_manifest_counts_and_diff_tracks_caveats_as_metadata_only(tmp_path: Path) -> None:
    base_dir = copy_package_config(tmp_path, "jaffle_shop")
    changed_dir = copy_package_config(tmp_path, "jaffle_shop")
    _write_caveats(
        changed_dir,
        [
            _revenue_caveat(),
            _revenue_caveat(id="caveat.test.second_row", message="Second advisory row."),
        ],
    )

    manifest = package_manifest(resolve_package_reference(path=str(changed_dir)))
    diff = diff_package_report(
        resolve_package_reference(path=str(changed_dir)), compare_path=str(base_dir)
    )

    assert manifest["summary"]["semantic_caveats"] == 2
    caveat_changes = [row for row in diff["changes"] if row["kind"] == "caveats"]
    assert {(row["object_id"], row["change_type"]) for row in caveat_changes} == {
        ("caveat.jaffle.brooklyn_soft_launch_mar_2017", "removed"),
        ("caveat.test.revenue_march", "added"),
        ("caveat.test.second_row", "added"),
    }
    assert all(row["behavior_change"] is False for row in caveat_changes)
    assert diff["summary"]["behavior_changes"] == 0
