"""Live values choose a policy-eligible anchor without disclosing skipped measures."""

from dataclasses import replace

import duckdb
import pytest
import yaml

from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.metadata_parts.valid_values import valid_values_payload
from semantic_rails.request_context import RequestContext
from semantic_rails.runtime import Runtime
from semantic_rails.schema import SemanticPolicyConfig

REVENUE = "measure.values.revenue"
USAGE = "measure.values.usage"
CATEGORY = "dimension.values_event_category"
TENANT = "dimension.values_event_tenant"
CONTEXT = RequestContext(roles=("support",)).to_policy_context()


@pytest.fixture
def package(tmp_path):
    path = tmp_path / "package.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "package": {
                    "id": "values",
                    "namespace": "values",
                    "warehouse": "duckdb",
                    "default_db": "values.duckdb",
                    "seed": {"kind": "external"},
                },
                "graph": {"entities": {"event": {"key": "event_id"}}},
                "models": {
                    "events": {
                        "relation": "events",
                        "grain": ["event_id"],
                        "entities": {"event": {}},
                        "dimensions": {
                            "category": {"kind": "categorical"},
                            "tenant": {"kind": "categorical"},
                        },
                        "measures": {
                            "revenue": {"kind": "aggregate", "expr": "amount"},
                            "usage": {"kind": "entity_count", "entity_key": "event_id"},
                        },
                    }
                },
            },
            sort_keys=False,
        )
    )
    with duckdb.connect(str(tmp_path / "values.duckdb")) as db:
        db.execute(
            "CREATE TABLE events AS SELECT * FROM (VALUES "
            "(1, 'a', 'web', 10), (2, 'a', 'web', 20), "
            "(3, 'a', 'app', 30), (4, 'b', 'private', 400)) "
            "t(event_id, tenant, category, amount)"
        )
    config = load_package_config(str(path))
    assert [row.id for row in config.measures] == [REVENUE, USAGE]
    return config, path


def _policy(action, objects=(REVENUE,)):
    return SemanticPolicyConfig(
        id="policy.values.restricted",
        kind="object_visibility" if action in {"hidden", "visible_only"} else "object_access",
        action=action,
        object_ids=list(objects),
        roles=["finance"] if action == "visible_only" else ["support"],
    )


def _values(config, path, context=CONTEXT, **query):
    runtime = Runtime.from_config(config, source_path=str(path))
    try:
        return valid_values_payload(
            runtime,
            dimension_id=CATEGORY,
            query={"policy_context": context, **query},
            allow_live_query=True,
            include_counts=True,
        )
    finally:
        runtime.close()


def _reference(path, tenant=None):
    with duckdb.connect(str(path.parent / "values.duckdb"), read_only=True) as db:
        rows = db.execute(
            "SELECT category, COUNT(DISTINCT event_id) FROM events "
            + ("WHERE tenant = ? " if tenant else "")
            + "GROUP BY category ORDER BY category",
            [tenant] if tenant else [],
        ).fetchall()
    return [{"value": category, "label": category, "count": count} for category, count in rows]


@pytest.mark.parametrize("action", ["deny", "hidden", "visible_only"])
@pytest.mark.parametrize("prefer_revenue", [False, True])
def test_live_values_skip_blocked_first_measure(package, action, prefer_revenue):
    config, path = package
    config = replace(config, semantic_policies=[_policy(action)])
    query = {"select": [{"expression": {"measure": REVENUE}}]} if prefer_revenue else {}
    out = _values(config, path, **query)
    assert out["anchor_measure"] == USAGE
    assert out["values"] == _reference(path)
    assert REVENUE not in str(out)


@pytest.mark.parametrize(
    "action,has_fallback",
    [
        ("deny", True),
        ("hidden", False),
        ("hidden", True),
        ("visible_only", False),
        ("visible_only", True),
    ],
)
@pytest.mark.parametrize("invalid_anchor", [False, True])
def test_skipped_measure_matches_absent_measure(package, action, has_fallback, invalid_anchor):
    config, path = package
    if invalid_anchor:
        config = replace(
            config,
            measures=[
                replace(config.measures[0], default_aggregation="unsupported"),
                *config.measures[1:],
            ],
        )
    config = replace(
        config,
        measures=config.measures if has_fallback else config.measures[:1],
        semantic_policies=[_policy(action)],
    )
    absent = replace(config, measures=[row for row in config.measures if row.id != REVENUE])

    def response(config):
        try:
            out = _values(config, path)
            # Different authored packages retain their own opaque provenance identities.
            for key in ("semantic_fingerprint", "source_fingerprint"):
                out["provenance"].pop(key)
            return out
        except SemanticLayerError as exc:
            return {"code": exc.code, "message": str(exc), "details": exc.details}

    assert response(config) == response(absent)


@pytest.mark.parametrize("deny_all", [False, True])
def test_no_permitted_anchor_refuses_without_executing(package, monkeypatch, deny_all):
    config, path = package
    config = replace(
        config,
        measures=(
            config.measures
            if deny_all
            else [
                config.measures[0],
                replace(config.measures[1], default_aggregation="unsupported"),
            ]
        ),
        semantic_policies=[_policy("deny", (REVENUE, USAGE) if deny_all else (REVENUE,))],
    )
    monkeypatch.setattr(Runtime, "_get_adapter", lambda _: pytest.fail("denied anchor ran SQL"))
    with pytest.raises(SemanticLayerError) as exc:
        _values(config, path)
    assert exc.value.code == "POLICY_DENIED"
    assert exc.value.details == {}


@pytest.mark.parametrize("allowed", [False, True])
def test_candidate_query_constraints_control_eligibility(package, allowed):
    config, path = package
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.values.grouping",
                kind="metric_constraint",
                object_ids=[REVENUE],
                config={"allowed_group_by": [CATEGORY] if allowed else []},
            )
        ],
    )
    out = _values(config, path)
    assert out["anchor_measure"] == (REVENUE if allowed else USAGE)
    with duckdb.connect(str(path.parent / "values.duckdb"), read_only=True) as db:
        aggregate = "SUM(amount)" if allowed else "COUNT(DISTINCT event_id)"
        expected = db.execute(
            f"SELECT category, {aggregate} FROM events GROUP BY category ORDER BY category"
        ).fetchall()
    assert out["values"] == [
        {"value": category, "label": category, "count": count} for category, count in expected
    ]


@pytest.mark.parametrize("tenant", ["a", "b"])
def test_row_filters_apply_to_fallback_values(package, tenant):
    config, path = package
    row_filter = SemanticPolicyConfig(
        id="policy.values.tenant",
        kind="row_filter",
        config={"dimension": TENANT, "attribute": "tenant"},
    )
    config = replace(config, semantic_policies=[_policy("deny"), row_filter])
    context = RequestContext(roles=("support",), attributes={"tenant": tenant}).to_policy_context()
    out = _values(config, path, context)
    assert out["anchor_measure"] == USAGE
    assert out["values"] == _reference(path, tenant)


def test_missing_row_filter_attribute_never_falls_back_unfiltered(package, monkeypatch):
    config, path = package
    config = replace(
        config,
        semantic_policies=[
            SemanticPolicyConfig(
                id="policy.values.tenant",
                kind="row_filter",
                config={"dimension": TENANT, "attribute": "tenant"},
            )
        ],
    )
    monkeypatch.setattr(Runtime, "_get_adapter", lambda _: pytest.fail("unfiltered anchor ran SQL"))
    with pytest.raises(SemanticLayerError):
        _values(config, path)
