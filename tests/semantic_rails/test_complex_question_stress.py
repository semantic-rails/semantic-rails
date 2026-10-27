from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.planner import plan_payload
from semantic_rails.registry import Registry
from semantic_rails.runtime import Runtime
from tests.plan_candidate_envelope import plan_candidate_envelope
from tests.semantic_rails.conftest import copy_package_config, opened
from tests.semantic_rails.result_helpers import assert_plan_held


def _wire_oracle(rows):
    """Oracle rows as answered: ISO dates, and each series row's period_partial (the fixture's
    periods all ended long ago)."""
    wire = []
    for row in rows:
        out = {}
        for key, value in row.items():
            out[key] = value.isoformat() if isinstance(value, date) else value
            if key == "period":
                out["period_partial"] = False
        wire.append(out)
    return wire


# Stores whose same-store session-to-order rate is over 90% in the month.
STORES_CONVERTING_OVER_90 = {
    "entity": "entity.jaffle_store",
    "having": [
        {
            "field": "metric.sales.session_to_order_conversion_rate_7d_same_store",
            "op": "gt",
            "value": 0.9,
        }
    ],
    "over": "period",
}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runtime]:
    """One jaffle_shop runtime for the module: every call here only reads."""
    package_dir = copy_package_config(
        tmp_path_factory.mktemp("complex"), "jaffle_shop", preseed_db=True
    )
    rt = Runtime.from_path(str(package_dir))
    try:
        yield opened(rt)
    finally:
        rt.close()


# Each store qualifier: its table, clock and counted key.
QUALIFIERS = {
    "order_count": ("jaffle_order", "ordered_at", "order_id"),
    "session_starts": ("jaffle_storefront_session", "started_at", "session_id"),
}


def _snapshot_parent_metric_query(thresholds: dict[str, int], key: str = "measure") -> dict:
    """The end-of-month active menus summed over the stores whose counts pass ``thresholds``
    in the month, by store."""
    predicates = [
        {key: f"measure.jaffle.{name}", "entity": "entity.jaffle_store", "over": "period"}
        | {"op": "gt", "value": value}
        for name, value in thresholds.items()
    ]
    return {
        "select": [
            {
                "as": "active_menus",
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": "measure.jaffle.active_menu_count_eop",
                    "aggregation": "sum",
                    "predicates": predicates,
                },
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
        "order_by": [
            {"field": "period", "direction": "asc"},
            {"field": "dimension.jaffle_store_name", "direction": "asc"},
        ],
    }


def _snapshot_oracle(thresholds: dict[str, int]) -> str:
    month = "DATE_TRUNC('month', CAST({} AS TIMESTAMP))"
    qualified = ", ".join(
        f"q_{name} AS (SELECT store_id, {month.format(clock)} AS month_start FROM {table} "
        f"GROUP BY 1, 2 HAVING COUNT(DISTINCT {counted}) > {thresholds[name]})"
        for name, (table, clock, counted) in QUALIFIERS.items()
        if name in thresholds
    )
    joins = " ".join(
        f"INNER JOIN q_{name} ON inventory.store_id = q_{name}.store_id "
        f"AND {month.format('inventory.date_day')} = q_{name}.month_start"
        for name in QUALIFIERS
        if name in thresholds
    )
    return f"""
        WITH {qualified},
        snapshot_base AS (
          SELECT store.store_name, inventory.store_id,
            {month.format("inventory.date_day")} AS month_start,
            inventory.date_day, inventory.active_menu_count
          FROM jaffle_store_inventory_snapshot AS inventory
          INNER JOIN jaffle_store AS store ON inventory.store_id = store.store_id
          {joins}
        ),
        snapshot_complete AS (
          SELECT store_name, store_id, month_start, MAX(date_day) AS date_day
          FROM snapshot_base GROUP BY store_name, store_id, month_start
        )
        SELECT
          snapshot_base.store_name AS "dimension.jaffle_store_name",
          CAST(snapshot_base.month_start AS DATE) AS period,
          SUM(snapshot_base.active_menu_count) AS active_menus
        FROM snapshot_base
        INNER JOIN snapshot_complete
          ON snapshot_base.store_name IS NOT DISTINCT FROM snapshot_complete.store_name
         AND snapshot_base.store_id IS NOT DISTINCT FROM snapshot_complete.store_id
         AND snapshot_base.month_start IS NOT DISTINCT FROM snapshot_complete.month_start
         AND snapshot_base.date_day = snapshot_complete.date_day
        GROUP BY snapshot_base.store_name, snapshot_base.month_start
        ORDER BY snapshot_base.month_start ASC, snapshot_base.store_name ASC
    """


@pytest.mark.parametrize(
    ("thresholds", "key"),
    [
        # Two contextual predicates: each store-month is answered once, with no full join.
        ({"order_count": 1, "session_starts": 1}, "measure"),
        ({"session_starts": 0}, "input"),
    ],
    ids=["orders_and_sessions", "sessions"],
)
def test_snapshot_parent_rollup_for_qualified_stores_matches_oracle(
    runtime: Runtime, thresholds: dict[str, int], key: str
) -> None:
    query = _snapshot_parent_metric_query(thresholds, key)
    # The maximal envelope carries `performance_plan`; the default drops it.
    report = runtime.validate(query, verbosity="full")
    assert report["ok"] is True
    assert "FULL OUTER JOIN" not in report["explain"]["rendered_sql"]
    performance = report["performance_plan"]
    assert performance["raw_fact_count"] == len(thresholds) + 1
    assert {row["relation"] for row in performance["estimated_scan_relations"]} == {
        "jaffle_store_inventory_snapshot",
        *(QUALIFIERS[name][0] for name in thresholds),
    }
    if len(thresholds) > 1:
        assert performance["risk_level"] == "high"
        assert "unbounded_raw_primitive_scan" in performance["large_scan_risk"]["reasons"]
    oracle = runtime._get_adapter().query(_snapshot_oracle(thresholds))
    assert oracle
    assert runtime.query(query)["rows"] == _wire_oracle(oracle)


def test_disallowed_child_entity_predicate_for_parent_snapshot_fails_closed(runtime):
    config = runtime.config
    query = _snapshot_parent_metric_query({})
    query["select"][0]["expression"]["predicates"] = [
        {
            "measure": "measure.jaffle.order_count",
            "entity": "entity.jaffle_customer",
            "scope_mode": "contextual",
            "op": ">",
            "value": 1,
        }
    ]

    with pytest.raises(SemanticLayerError) as exc:
        compile_query(config, Registry(config), query)

    assert exc.value.code == "PATH_NOT_FOUND"


def test_28d_adoption_funnel_applies_order_rate_filter_inside_conversion_base(runtime):
    unfiltered = {
        "select": [
            {
                "as": "signup_to_send_28d",
                "expression": "metric.adoption.signup_to_send_conversion_rate_28d",
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "month"},
    }
    filtered = {
        **unfiltered,
        "where": [STORES_CONVERTING_OVER_90],
        "order_by": [
            {"field": "period", "direction": "asc"},
            {"field": "dimension.jaffle_store_name", "direction": "asc"},
        ],
    }

    assert runtime.query(unfiltered)["row_count"] > 0

    # Opts into the maximal envelope so `performance_plan` is present;
    # the default ('compact') drops it.
    report = runtime.validate(filtered, verbosity="full")
    assert report["ok"] is True
    # The store filter applies inside the conversion's base: the oracle below reads it there.
    assert "FULL OUTER JOIN" not in report["explain"]["rendered_sql"]
    assert report["performance_plan"]["full_outer_alignments"] == 0
    assert report["performance_plan"]["raw_fact_count"] == 4
    assert [row["relation"] for row in report["performance_plan"]["estimated_scan_relations"]] == [
        "jaffle_storefront_session",
        "jaffle_order",
        "jaffle_storefront_session",
        "jaffle_order",
    ]
    assert report["performance_plan"]["risk_level"] == "high"
    assert (
        "unbounded_raw_primitive_scan" in report["performance_plan"]["large_scan_risk"]["reasons"]
    )

    result = runtime.query(filtered)
    assert result["status"] == "ok"
    assert result["row_count"] > 0
    oracle = runtime._get_adapter().query(
        """
        WITH predicate_base AS (
          SELECT
            session.store_id,
            DATE_TRUNC('month', CAST(session.started_at AS TIMESTAMP)) AS month_start,
            session.session_id,
            session.customer_id,
            session.started_at
          FROM jaffle_storefront_session AS session
        ),
        predicate_converted AS (
          SELECT
            order_id,
            customer_id,
            store_id,
            ordered_at
          FROM jaffle_order
        ),
        predicate_matches AS (
          SELECT
            base.store_id,
            base.month_start,
            base.session_id,
            converted.order_id,
            ROW_NUMBER() OVER (
              PARTITION BY base.session_id
              ORDER BY converted.ordered_at ASC, converted.order_id ASC
            ) AS match_rank
          FROM predicate_base AS base
          LEFT JOIN predicate_converted AS converted
            ON converted.customer_id IS NOT DISTINCT FROM base.customer_id
           AND converted.store_id IS NOT DISTINCT FROM base.store_id
           AND converted.ordered_at >= base.started_at
           AND CAST(converted.ordered_at AS TIMESTAMP) < CAST(base.started_at AS TIMESTAMP) + INTERVAL 7 DAY
        ),
        qualified_stores AS (
          SELECT
            store_id,
            month_start
          FROM predicate_matches
          WHERE match_rank = 1
          GROUP BY store_id, month_start
          HAVING COUNT(DISTINCT CASE WHEN order_id IS NOT NULL THEN session_id ELSE NULL END)
            / NULLIF(COUNT(DISTINCT session_id), 0) > 0.9
        ),
        base_events AS (
          SELECT
            store.store_name,
            session.store_id,
            DATE_TRUNC('month', CAST(session.started_at AS TIMESTAMP)) AS month_start,
            session.session_id,
            session.customer_id,
            session.started_at
          FROM jaffle_storefront_session AS session
          INNER JOIN jaffle_store AS store
            ON session.store_id = store.store_id
          INNER JOIN qualified_stores AS qualified
            ON session.store_id IS NOT DISTINCT FROM qualified.store_id
           AND DATE_TRUNC('month', CAST(session.started_at AS TIMESTAMP)) IS NOT DISTINCT FROM qualified.month_start
        ),
        converted_events AS (
          SELECT
            order_id,
            customer_id,
            ordered_at
          FROM jaffle_order
        ),
        adoption_matches AS (
          SELECT
            base.store_name,
            base.month_start,
            base.session_id,
            converted.order_id,
            ROW_NUMBER() OVER (
              PARTITION BY base.session_id
              ORDER BY converted.ordered_at ASC, converted.order_id ASC
            ) AS match_rank
          FROM base_events AS base
          LEFT JOIN converted_events AS converted
            ON converted.customer_id IS NOT DISTINCT FROM base.customer_id
           AND converted.ordered_at >= base.started_at
           AND CAST(converted.ordered_at AS TIMESTAMP) < CAST(base.started_at AS TIMESTAMP) + INTERVAL 28 DAY
        )
        SELECT
          store_name AS "dimension.jaffle_store_name",
          CAST(month_start AS DATE) AS period,
          CASE WHEN COUNT(DISTINCT session_id) = 0 THEN NULL
            ELSE COUNT(DISTINCT CASE WHEN order_id IS NOT NULL THEN session_id ELSE NULL END)
              / NULLIF(COUNT(DISTINCT session_id), 0)
          END AS signup_to_send_28d
        FROM adoption_matches
        WHERE match_rank = 1
        GROUP BY store_name, month_start
        ORDER BY month_start ASC, store_name ASC
        """
    )
    assert result["rows"] == _wire_oracle(oracle)


def test_plan_composes_exact_complex_question_shapes(runtime):
    snapshot = plan_payload(
        runtime,
        intent=(
            "Give me the sum of active menu snapshot at the store dimension for stores "
            "that have done more than one order and more than one session grouped by month"
        ),
        detail="full",
        limit=1,
    )
    adoption = plan_candidate_envelope(
        runtime,
        intent="Give me the 28D adoption funnel from signup to Send for stores that have an order rate of over 90% grouped by month",
        limit=1,
        verbosity="full",
    )

    assert snapshot["best"]["pattern"] == "qualified_metric_rollup"
    # Over each period, every qualifier is read on its own clock in the snapshot's month, so
    # the draft validates; the plan still holds it because a balance summed over a month is
    # not the snapshot the question asks for.
    assert_plan_held(snapshot, "PLAN_INTENT_COVERAGE_GAP")
    assert "stock_as_of_unrealized" in {gap["kind"] for gap in snapshot["why"]["details"]["gaps"]}
    assert "ready_for" not in snapshot["next"]
    snapshot_query = snapshot["best"]["query"]
    snapshot_expr = snapshot_query["select"][0]["expression"]
    assert snapshot["best"]["validation_ok"] is True
    assert runtime.validate(snapshot_query)["ok"] is True
    assert snapshot_expr["measure"] == "measure.jaffle.active_menu_count_eop"
    assert snapshot_expr["aggregation"] == "sum"
    # The stores qualify through one entity filter. Its conditions may read measures or
    # metrics depending on which scored higher in discovery — we assert the cohort fields
    # match, not the exact refs. The question doesn't say over which rows, so the draft
    # reads every row of each store.
    [stores] = snapshot_query["where"]
    assert stores["entity"] == "entity.jaffle_store"
    assert stores["over"] == "all_time"
    assert len(stores["having"]) == 2
    for condition in stores["having"]:
        assert condition["op"] == "gt"
        assert condition["value"] == 1
    assert not snapshot_query.get("group_by")
    assert snapshot_query["time"] == {
        "temporal_role": "temporal_role.jaffle_inventory_day",
        "grain": "month",
    }

    assert adoption["interpreted_intent"]["pattern"] == "filtered_adoption_funnel"
    # "For stores that have ..." qualifies the stores, and the draft also splits the funnel
    # by store, which the question never asks for: the drafted shape is held, not offered.
    assert not adoption["candidates"]
    [blocked] = adoption["blocked"]
    assert blocked["why_blocked"]["code"] == "PLAN_UNASKED_GROUPING"
    assert blocked["why_blocked"]["details"]["unasked_groupings"] == ["Store name"]
    adoption_query = blocked["candidate_ir"]
    assert blocked["validation"]["ok"] is True
    assert adoption_query["select"] == ["metric.adoption.signup_to_send_conversion_rate_28d"]
    assert adoption_query["group_by"] == ["dimension.jaffle_store_name"]
    assert adoption_query["time"] == {
        "temporal_role": "temporal_role.jaffle_session_started_at",
        "grain": "month",
    }
    # The question names an order rate, not which one: the draft reads the first by label,
    # over every row of each store.
    assert adoption_query["where"] == [
        {
            "entity": "entity.jaffle_store",
            "having": [
                {
                    "field": "metric.sales.session_to_order_conversion_rate_7d",
                    "op": "gt",
                    "value": 0.9,
                }
            ],
            "over": "all_time",
        }
    ]


def test_conversion_metric_refuses_a_threshold_that_zero_satisfies(runtime):
    report = runtime.validate(
        {
            "select": [
                {
                    "as": "signup_to_send_28d",
                    "expression": "metric.adoption.signup_to_send_conversion_rate_28d",
                }
            ],
            "group_by": ["dimension.jaffle_store_name"],
            "time": {
                "temporal_role": "temporal_role.jaffle_session_started_at",
                "grain": "month",
            },
            "where": [
                {
                    "entity": "entity.jaffle_store",
                    "having": [{"field": "measure.jaffle.order_count", "op": "is", "value": 0}],
                    "over": "period",
                }
            ],
        }
    )
    assert report["ok"] is False
    error = report["errors"][0]
    assert error["code"] == "INVALID_METRIC_PREDICATE"
    assert error["details"]["entity"] == "entity.jaffle_store"
    assert (error["details"]["predicate"]["op"], error["details"]["predicate"]["value"]) == (
        "is",
        0,
    )


def test_metric_predicate_filter_envelope_rejects_non_true_outer_filter(runtime):
    # A call says an entity filter as {entity, having, over}; the compiler's own form wraps it in
    # an envelope that must keep the entities that pass, never the ones that fail.
    report = runtime.validate_internal(
        {
            "select": [{"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}}],
            "metric_filters": [
                {
                    "expression": {
                        "kind": "metric_predicate",
                        "entity": "entity.jaffle_customer",
                        "input": {"measure": "measure.jaffle.order_count"},
                        "op": ">",
                        "value": 1,
                    },
                    "op": "=",
                    "value": False,
                }
            ],
            "time": {"temporal_role": "temporal_role.jaffle_order_time", "grain": "month"},
        }
    )

    assert report["ok"] is False
    assert report["errors"][0]["code"] == "INVALID_METRIC_FILTER"
