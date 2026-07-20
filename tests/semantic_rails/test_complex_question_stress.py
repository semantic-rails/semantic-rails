from __future__ import annotations

import pytest

from semantic_rails.compiler import compile_query
from semantic_rails.errors import SemanticLayerError
from semantic_rails.registry import Registry
from tests.plan_candidate_envelope import plan_candidate_envelope


def _snapshot_parent_metric_query() -> dict:
    return {
        "version": 2,
        "select": [
            {
                "as": "active_menu_count_from_high_activity_stores",
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": "measure.jaffle.active_menu_count_eop",
                    "aggregation": "sum",
                    "predicates": [
                        {
                            "measure": "measure.jaffle.order_count",
                            "entity": "entity.jaffle_store",
                            "scope_mode": "contextual",
                            "op": ">",
                            "value": 1,
                        },
                        {
                            "measure": "measure.jaffle.session_starts",
                            "entity": "entity.jaffle_store",
                            "scope_mode": "contextual",
                            "op": ">",
                            "value": 1,
                        },
                    ],
                },
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
        "order_by": [
            {"field": "time", "direction": "ASC"},
            {"field": "dimension.jaffle_store_name", "direction": "ASC"},
        ],
    }


def test_snapshot_parent_rollup_with_two_contextual_predicates_executes_once_per_parent_month(
    runtime_factory,
):
    runtime = runtime_factory("jaffle_shop")
    # Opts into the maximal envelope so `performance_plan` is present;
    # the default ('compact') drops it.
    query = {**_snapshot_parent_metric_query(), "verbosity": "full"}
    try:
        report = runtime.validate(query)
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]

        assert "qualified_stores_month_by_order_count" in rendered
        assert "qualified_stores_month_by_session_starts" in rendered
        assert "snapshot_complete AS (" in rendered
        assert "FULL OUTER JOIN" not in rendered
        performance = report["performance_plan"]
        assert performance["raw_fact_count"] == 3
        assert performance["risk_level"] == "high"
        assert {row["relation"] for row in performance["estimated_scan_relations"]} == {
            "jaffle_order",
            "jaffle_store_inventory_snapshot",
            "jaffle_storefront_session",
        }
        assert "unbounded_raw_primitive_scan" in performance["large_scan_risk"]["reasons"]

        actual = runtime.query(query)["rows"]
        oracle = runtime._get_adapter().query(
            """
            WITH order_qualified AS (
              SELECT
                store_id,
                DATE_TRUNC('month', CAST(ordered_at AS TIMESTAMP)) AS month_start
              FROM jaffle_order
              GROUP BY store_id, DATE_TRUNC('month', CAST(ordered_at AS TIMESTAMP))
              HAVING COUNT(DISTINCT order_id) > 1
            ),
            session_qualified AS (
              SELECT
                store_id,
                DATE_TRUNC('month', CAST(started_at AS TIMESTAMP)) AS month_start
              FROM jaffle_storefront_session
              GROUP BY store_id, DATE_TRUNC('month', CAST(started_at AS TIMESTAMP))
              HAVING COUNT(DISTINCT session_id) > 1
            ),
            snapshot_base AS (
              SELECT
                store.store_name,
                inventory.store_id,
                DATE_TRUNC('month', CAST(inventory.date_day AS TIMESTAMP)) AS month_start,
                inventory.date_day,
                inventory.active_menu_count
              FROM jaffle_store_inventory_snapshot AS inventory
              INNER JOIN jaffle_store AS store
                ON inventory.store_id = store.store_id
              INNER JOIN order_qualified AS orders
                ON inventory.store_id = orders.store_id
               AND DATE_TRUNC('month', CAST(inventory.date_day AS TIMESTAMP)) = orders.month_start
              INNER JOIN session_qualified AS sessions
                ON inventory.store_id = sessions.store_id
               AND DATE_TRUNC('month', CAST(inventory.date_day AS TIMESTAMP)) = sessions.month_start
            ),
            snapshot_complete AS (
              SELECT
                store_name,
                store_id,
                month_start,
                MAX(date_day) AS date_day
              FROM snapshot_base
              GROUP BY store_name, store_id, month_start
            )
            SELECT
              snapshot_base.store_name AS "dimension.jaffle_store_name",
              snapshot_base.month_start AS "temporal_role.jaffle_inventory_day__month",
              SUM(snapshot_base.active_menu_count) AS active_menu_count_from_high_activity_stores
            FROM snapshot_base
            INNER JOIN snapshot_complete
              ON snapshot_base.store_name IS NOT DISTINCT FROM snapshot_complete.store_name
             AND snapshot_base.store_id IS NOT DISTINCT FROM snapshot_complete.store_id
             AND snapshot_base.month_start IS NOT DISTINCT FROM snapshot_complete.month_start
             AND snapshot_base.date_day = snapshot_complete.date_day
            GROUP BY snapshot_base.store_name, snapshot_base.month_start
            ORDER BY snapshot_base.month_start ASC, snapshot_base.store_name ASC
            """
        )
        assert actual == oracle
    finally:
        runtime.close()


def test_monthly_snapshot_metric_for_session_qualified_stores_matches_oracle(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    query = {
        "version": 2,
        "select": [
            {
                "as": "active_menu_count_from_session_stores",
                "expression": {
                    "kind": "scoped_aggregate",
                    "measure": "measure.jaffle.active_menu_count_eop",
                    "aggregation": "sum",
                    "predicates": [
                        {
                            "input": {"measure": "measure.jaffle.session_starts"},
                            "entity": "entity.jaffle_store",
                            "scope_mode": "contextual",
                            "op": ">",
                            "value": 0,
                        }
                    ],
                },
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_inventory_day", "grain": "month"},
        "order_by": [
            {"field": "time", "direction": "ASC"},
            {"field": "dimension.jaffle_store_name", "direction": "ASC"},
        ],
    }
    try:
        report = runtime.validate(query)
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        assert "qualified_stores_month_by_session_starts" in rendered
        assert "snapshot_complete AS (" in rendered

        actual = runtime.query(query)["rows"]
        oracle = runtime._get_adapter().query(
            """
            WITH session_qualified AS (
              SELECT
                store_id,
                DATE_TRUNC('month', CAST(started_at AS TIMESTAMP)) AS month_start
              FROM jaffle_storefront_session
              GROUP BY store_id, DATE_TRUNC('month', CAST(started_at AS TIMESTAMP))
              HAVING COUNT(DISTINCT session_id) > 0
            ),
            snapshot_base AS (
              SELECT
                store.store_name,
                inventory.store_id,
                DATE_TRUNC('month', CAST(inventory.date_day AS TIMESTAMP)) AS month_start,
                inventory.date_day,
                inventory.active_menu_count
              FROM jaffle_store_inventory_snapshot AS inventory
              INNER JOIN jaffle_store AS store
                ON inventory.store_id = store.store_id
              INNER JOIN session_qualified AS sessions
                ON inventory.store_id = sessions.store_id
               AND DATE_TRUNC('month', CAST(inventory.date_day AS TIMESTAMP)) = sessions.month_start
            ),
            snapshot_complete AS (
              SELECT
                store_name,
                store_id,
                month_start,
                MAX(date_day) AS date_day
              FROM snapshot_base
              GROUP BY store_name, store_id, month_start
            )
            SELECT
              snapshot_base.store_name AS "dimension.jaffle_store_name",
              snapshot_base.month_start AS "temporal_role.jaffle_inventory_day__month",
              SUM(snapshot_base.active_menu_count) AS active_menu_count_from_session_stores
            FROM snapshot_base
            INNER JOIN snapshot_complete
              ON snapshot_base.store_name IS NOT DISTINCT FROM snapshot_complete.store_name
             AND snapshot_base.store_id IS NOT DISTINCT FROM snapshot_complete.store_id
             AND snapshot_base.month_start IS NOT DISTINCT FROM snapshot_complete.month_start
             AND snapshot_base.date_day = snapshot_complete.date_day
            GROUP BY snapshot_base.store_name, snapshot_base.month_start
            ORDER BY snapshot_base.month_start ASC, snapshot_base.store_name ASC
            """
        )
        assert actual == oracle
    finally:
        runtime.close()


def test_disallowed_child_entity_predicate_for_parent_snapshot_fails_closed(package_config_factory):
    config, _ = package_config_factory("jaffle_shop")
    query = _snapshot_parent_metric_query()
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


def test_28d_adoption_funnel_applies_order_rate_filter_inside_conversion_base(runtime_factory):
    unfiltered = {
        "version": 1,
        "select": [
            {
                "as": "signup_to_send_28d",
                "expression": {"metric": "metric.adoption.signup_to_send_conversion_rate_28d"},
            }
        ],
        "group_by": ["dimension.jaffle_store_name"],
        "time": {"temporal_role": "temporal_role.jaffle_session_started_at", "grain": "month"},
    }
    filtered = {
        **unfiltered,
        "metric_filters": [
            {
                "expression": {
                    "kind": "metric_predicate",
                    "entity": "entity.jaffle_store",
                    "scope_mode": "contextual",
                    "input": {
                        "metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"
                    },
                    "op": ">",
                    "value": 0.9,
                },
                "op": "=",
                "value": True,
            }
        ],
        "order_by": [
            {"field": "time", "direction": "ASC"},
            {"field": "dimension.jaffle_store_name", "direction": "ASC"},
        ],
    }

    runtime = runtime_factory("jaffle_shop")
    try:
        assert runtime.query(unfiltered)["row_count"] > 0

        # Opts into the maximal envelope so `performance_plan` is present;
        # the default ('compact') drops it.
        report = runtime.validate({**filtered, "verbosity": "full"})
        assert report["ok"] is True
        rendered = report["explain"]["rendered_sql"]
        # CTE name reflects the conversion metric ID; check by substring
        # rather than exact match since the alias scheme depends on internals.
        assert "session_to_order_conversion_rate_7d_same_store" in rendered
        assert "FROM conversion_leaf_1__conversion_base_1 AS base_events" in rendered
        assert "FULL OUTER JOIN" not in rendered
        assert report["performance_plan"]["full_outer_alignments"] == 0
        assert report["performance_plan"]["raw_fact_count"] == 4
        assert [
            row["relation"] for row in report["performance_plan"]["estimated_scan_relations"]
        ] == [
            "jaffle_storefront_session",
            "jaffle_order",
            "jaffle_storefront_session",
            "jaffle_order",
        ]
        assert report["performance_plan"]["risk_level"] == "high"
        assert (
            "unbounded_raw_primitive_scan"
            in report["performance_plan"]["large_scan_risk"]["reasons"]
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
               AND DATE_DIFF('day', CAST(base.started_at AS TIMESTAMP), CAST(converted.ordered_at AS TIMESTAMP)) <= 7
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
               AND DATE_DIFF('day', CAST(base.started_at AS TIMESTAMP), CAST(converted.ordered_at AS TIMESTAMP)) <= 28
            )
            SELECT
              store_name AS "dimension.jaffle_store_name",
              month_start AS "temporal_role.jaffle_session_started_at__month",
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
        assert result["rows"] == oracle
    finally:
        runtime.close()


def test_plan_composes_exact_complex_question_shapes(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        snapshot = plan_candidate_envelope(
            runtime,
            intent=(
                "Give me the sum of active menu snapshot at the store dimension for stores "
                "that have done more than one order and more than one session grouped by month"
            ),
            limit=1,
        )
        adoption = plan_candidate_envelope(
            runtime,
            intent="Give me the 28D adoption funnel from signup to Send for stores that have an order rate of over 90% grouped by month",
            limit=1,
        )

        assert snapshot["interpreted_intent"]["pattern"] == "qualified_metric_rollup"
        snapshot_query = snapshot["candidates"][0]["candidate_ir"]
        snapshot_expr = snapshot_query["select"][0]["expression"]
        assert snapshot["candidates"][0]["validation"]["ok"] is True
        assert snapshot_expr["measure"] == "measure.jaffle.active_menu_count_eop"
        assert snapshot_expr["aggregation"] == "sum"
        # Predicate inputs may be measures or metrics depending on which
        # scored higher in discovery — we assert the cohort fields match,
        # not the exact input ref.
        assert len(snapshot_expr["predicates"]) == 2
        for predicate in snapshot_expr["predicates"]:
            assert predicate["entity"] == "entity.jaffle_store"
            assert predicate["op"] == ">"
            assert predicate["value"] == 1
            assert "measure" in predicate or "metric" in predicate
        assert snapshot_query["group_by"] == ["dimension.jaffle_store_name"]
        assert snapshot_query["time"] == {
            "temporal_role": "temporal_role.jaffle_inventory_day",
            "grain": "month",
        }

        assert adoption["interpreted_intent"]["pattern"] == "filtered_adoption_funnel"
        adoption_query = adoption["candidates"][0]["candidate_ir"]
        adoption_filter = adoption_query["metric_filters"][0]
        assert adoption["candidates"][0]["validation"]["ok"] is True
        assert adoption_query["select"][0]["expression"] == {
            "metric": "metric.adoption.signup_to_send_conversion_rate_28d"
        }
        assert adoption_query["group_by"] == ["dimension.jaffle_store_name"]
        assert adoption_query["time"] == {
            "temporal_role": "temporal_role.jaffle_session_started_at",
            "grain": "month",
        }
        assert adoption_filter == {
            "expression": {
                "kind": "metric_predicate",
                "entity": "entity.jaffle_store",
                "scope_mode": "contextual",
                "input": {"metric": "metric.sales.session_to_order_conversion_rate_7d_same_store"},
                "op": ">",
                "value": 0.9,
            },
            "op": "=",
            "value": True,
        }
    finally:
        runtime.close()


def test_metric_predicate_filter_envelope_rejects_non_true_outer_filter(runtime_factory):
    runtime = runtime_factory("jaffle_shop")
    try:
        report = runtime.validate(
            {
                "version": 1,
                "select": [
                    {"as": "orders", "expression": {"measure": "measure.jaffle.order_count"}}
                ],
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
    finally:
        runtime.close()
