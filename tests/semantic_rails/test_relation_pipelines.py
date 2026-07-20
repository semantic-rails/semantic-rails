from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from semantic_rails import config as config_module
from semantic_rails.config import load_package_config
from semantic_rails.errors import SemanticLayerError
from semantic_rails.relation_pipelines import lower_relation
from semantic_rails.renderer import render_select
from semantic_rails.runtime import Runtime
from semantic_rails.sql_ast import SqlField, SqlLiteral, SqlSelect, SqlSetQuery


@pytest.fixture(autouse=True)
def _allow_external_package_paths(monkeypatch):
    # _write_relation_demo points default_db/seed at pytest tmp dirs outside
    # the package root — the trusted local flow the containment check gates.
    monkeypatch.setenv("SEMANTIC_RAILS_ALLOW_EXTERNAL_PACKAGE_PATHS", "1")


def _write_relation_demo(tmp_path: Path) -> Path:
    package_dir = tmp_path / "relation_demo"
    package_dir.mkdir()
    seed = package_dir / "seed.sql"
    seed.write_text(
        """
        CREATE TABLE account_state (
          account_id INTEGER,
          valid_from DATE,
          valid_to DATE,
          tags VARCHAR,
          settings VARCHAR
        );
        INSERT INTO account_state VALUES
          (1, DATE '2024-01-01', DATE '2024-01-04', 'email,sms', '{"sms":{"enabled":true}}'),
          (2, DATE '2024-01-02', DATE '2024-01-04', 'email', '{"sms":{"enabled":false}}');
        CREATE TABLE excluded_accounts (account_id INTEGER);
        INSERT INTO excluded_accounts VALUES (2);

        CREATE TABLE sms_sends (account_id INTEGER, sent_at DATE, send_id VARCHAR);
        CREATE TABLE email_sends (account_id INTEGER, sent_at DATE, send_id VARCHAR);
        INSERT INTO sms_sends VALUES (1, DATE '2024-01-01', 's1'), (1, DATE '2024-01-02', 's2');
        INSERT INTO email_sends VALUES (1, DATE '2024-01-01', 'e1'), (2, DATE '2024-01-02', 'e2');

        CREATE TABLE signup_events (signup_id VARCHAR, account_id INTEGER, signed_at DATE);
        CREATE TABLE send_events (send_id VARCHAR, account_id INTEGER, sent_at DATE);
        INSERT INTO signup_events VALUES
          ('u1', 1, DATE '2024-01-01'),
          ('u2', 2, DATE '2024-01-01'),
          ('u3', 3, DATE '2024-01-01');
        INSERT INTO send_events VALUES
          ('m1', 1, DATE '2024-01-03'),
          ('m2', 1, DATE '2024-01-05'),
          ('m3', 2, DATE '2024-02-20');
        """.strip(),
        encoding="utf-8",
    )
    package = {
        "schema_version": 1,
        "package": {
            "id": "relation_demo",
            "namespace": "reldemo",
            "warehouse": "duckdb",
            "default_db": str(tmp_path / "relation_demo.duckdb"),
            "seed": {"kind": "sql_script", "source": str(seed)},
        },
        "graph": {
            "entities": {
                "account_state": {
                    "key": ["account_id", "valid_from"],
                    "model": "account_state_raw",
                },
                "account_tag_day": {
                    "key": ["account_id", "tag", "date_day"],
                    "model": "account_tag_daily",
                },
                "eligible_account_state": {
                    "key": ["account_id", "next_valid_from"],
                    "model": "eligible_account_states",
                },
                "send": {"key": ["send_id"], "model": "send_feed"},
                "signup_match": {"key": ["signup_id"], "model": "signup_send_matches"},
            }
        },
        "relations": {
            "date_spine": {
                "date_spine": {"start": "2024-01-01", "end": "2024-01-03", "column": "date_day"}
            },
            "account_tag_daily": {
                "source": "account_state",
                "columns": ["account_id", "valid_from", "valid_to", "tags", "settings"],
                "steps": [
                    {
                        "json_extract": {
                            "column": "settings",
                            "path": ["sms", "enabled"],
                            "as": "sms_enabled",
                        }
                    },
                    {"explode": {"column": "tags", "delimiter": ",", "as": "tag"}},
                    {
                        "state_as_of": {
                            "spine": "rel_reldemo_date_spine",
                            "date_column": "date_day",
                            "valid_from": "valid_from",
                            "valid_to": "valid_to",
                            "select": {
                                "account_id": {
                                    "kind": "column",
                                    "table": "src",
                                    "column": "account_id",
                                },
                                "date_day": {
                                    "kind": "column",
                                    "table": "spine",
                                    "column": "date_day",
                                },
                                "tag": {"kind": "column", "table": "src", "column": "tag"},
                                "sms_enabled": {
                                    "kind": "column",
                                    "table": "src",
                                    "column": "sms_enabled",
                                },
                            },
                        }
                    },
                ],
            },
            "send_feed": {
                "steps": [
                    {
                        "union_all": {
                            "branches": [
                                {
                                    "source": "sms_sends",
                                    "columns": {
                                        "account_id": "account_id",
                                        "sent_at": "sent_at",
                                        "send_id": "send_id",
                                        "channel": {"kind": "literal", "value": "sms"},
                                    },
                                },
                                {
                                    "source": "email_sends",
                                    "columns": {
                                        "account_id": "account_id",
                                        "sent_at": "sent_at",
                                        "send_id": "send_id",
                                        "channel": {"kind": "literal", "value": "email"},
                                    },
                                },
                            ]
                        }
                    }
                ]
            },
            "eligible_account_states": {
                "source": "account_state",
                "columns": ["account_id", "valid_from", "valid_to", "tags"],
                "steps": [
                    {
                        "anti_join": {
                            "relation": "excluded_accounts",
                            "on": [{"left": "account_id", "right": "account_id"}],
                        }
                    },
                    {
                        "select": {
                            "columns": {
                                "account_id": "account_id",
                                "next_valid_from": {
                                    "kind": "date_add",
                                    "unit": "day",
                                    "value": {"kind": "literal", "value": 1},
                                    "date": {"kind": "column", "column": "valid_from"},
                                },
                                "status": {"kind": "literal", "value": "eligible"},
                            }
                        }
                    },
                ],
            },
            "signup_send_matches": {
                "steps": [
                    {
                        "attribution_join": {
                            "base": "signup_events",
                            "attributed": "send_events",
                            "keys": ["account_id"],
                            "base_time": "signed_at",
                            "attributed_time": "sent_at",
                            "lookback": {"unit": "day", "value": 28},
                            "select": {
                                "signup_id": {
                                    "kind": "column",
                                    "table": "base",
                                    "column": "signup_id",
                                },
                                "account_id": {
                                    "kind": "column",
                                    "table": "base",
                                    "column": "account_id",
                                },
                                "signed_at": {
                                    "kind": "column",
                                    "table": "base",
                                    "column": "signed_at",
                                },
                                "send_id": {
                                    "kind": "column",
                                    "table": "attributed",
                                    "column": "send_id",
                                },
                                "sent_at": {
                                    "kind": "column",
                                    "table": "attributed",
                                    "column": "sent_at",
                                },
                            },
                        }
                    },
                    {
                        "window": {
                            "match_rank": {
                                "function": "row_number",
                                "partition_by": ["signup_id"],
                                "order_by": [{"column": "sent_at", "direction": "ASC"}],
                            }
                        }
                    },
                    {"where": [{"field": "match_rank", "op": "=", "value": 1}]},
                ]
            },
        },
        "models": {
            "account_state_raw": {
                "relation": "account_state",
                "grain": ["account_id", "valid_from"],
                "keys": {"primary": ["account_id", "valid_from"]},
                "times": {
                    "valid_from": {
                        "label": "Valid from",
                        "kind": "date",
                        "class": "snapshot_time",
                        "default_query_axis": True,
                    }
                },
                "default_time": "valid_from",
                "measures": {
                    "raw_account_rows": {
                        "label": "Raw account rows",
                        "description": "Rows in the physical account state table.",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["account_id"],
                        "time": "valid_from",
                    }
                },
            },
            "account_tag_daily": {
                "relation": "account_tag_daily",
                "grain": ["account_id", "tag", "date_day"],
                "keys": {"primary": ["account_id", "tag", "date_day"]},
                "dimensions": {
                    "tag": {"label": "Tag", "kind": "categorical"},
                    "sms_enabled": {"label": "SMS enabled", "kind": "categorical"},
                },
                "times": {
                    "date_day": {
                        "label": "Date day",
                        "kind": "date",
                        "class": "snapshot_time",
                        "default_query_axis": True,
                    }
                },
                "default_time": "date_day",
                "measures": {
                    "account_tag_rows": {
                        "label": "Account tag rows",
                        "description": "Rows in the derived account tag daily relation.",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["account_id"],
                        "time": "date_day",
                        "topics": ["relations"],
                    }
                },
            },
            "eligible_account_states": {
                "relation": "eligible_account_states",
                "grain": ["account_id", "next_valid_from"],
                "keys": {"primary": ["account_id", "next_valid_from"]},
                "dimensions": {"status": {"label": "Status", "kind": "categorical"}},
                "times": {
                    "next_valid_from": {
                        "label": "Next valid from",
                        "kind": "date",
                        "class": "snapshot_time",
                        "default_query_axis": True,
                    }
                },
                "default_time": "next_valid_from",
                "measures": {
                    "eligible_account_rows": {
                        "label": "Eligible account rows",
                        "description": "Rows remaining after the anti-join exclusion relation.",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["account_id"],
                        "time": "next_valid_from",
                    }
                },
            },
            "send_feed": {
                "relation": "send_feed",
                "grain": ["send_id"],
                "keys": {"primary": ["send_id"]},
                "dimensions": {"channel": {"label": "Channel", "kind": "categorical"}},
                "times": {
                    "sent_at": {
                        "label": "Sent at",
                        "kind": "date",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "default_time": "sent_at",
                "measures": {
                    "sends": {
                        "label": "Sends",
                        "description": "Sends from the unioned feed.",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["send_id"],
                        "time": "sent_at",
                        "topics": ["relations"],
                    }
                },
            },
            "signup_send_matches": {
                "relation": "signup_send_matches",
                "grain": ["signup_id"],
                "keys": {"primary": ["signup_id"]},
                "dimensions": {"account_id": {"label": "Account ID", "kind": "categorical"}},
                "times": {
                    "signed_at": {
                        "label": "Signed at",
                        "kind": "date",
                        "class": "event_time",
                        "default_query_axis": True,
                    }
                },
                "default_time": "signed_at",
                "measures": {
                    "matched_signups": {
                        "label": "Matched signups",
                        "description": "Signup rows after the first-send attribution relation pipeline.",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["signup_id"],
                        "time": "signed_at",
                        "topics": ["relations"],
                    }
                },
            },
        },
    }
    (package_dir / "package.yml").write_text(
        yaml.safe_dump(package, sort_keys=False), encoding="utf-8"
    )
    return package_dir


def test_relation_pipeline_yaml_loads_and_models_reference_derived_relations(tmp_path: Path):
    package_dir = _write_relation_demo(tmp_path)
    config = load_package_config(str(package_dir))

    relations = {row.id: row for row in config.relations}
    assert "relation.reldemo.account_tag_daily" in relations
    assert "relation.reldemo.send_feed" in relations
    entities = {row.id: row for row in config.entities}
    assert (
        entities["entity.reldemo_account_tag_day"].relation_id
        == "relation.reldemo.account_tag_daily"
    )
    assert entities["entity.reldemo_account_tag_day"].table == "rel_reldemo_account_tag_daily"


def test_relation_pipeline_executes_json_explode_date_spine_and_union(tmp_path: Path, monkeypatch):
    package_dir = _write_relation_demo(tmp_path)
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"relation_demo": str(package_dir)}
    )
    runtime = Runtime("relation_demo")
    try:
        tag_rows = runtime.query(
            {
                "version": 1,
                "select": [
                    {"as": "rows", "expression": {"metric": "metric.reldemo.account_tag_rows"}}
                ],
                "group_by": ["dimension.reldemo_account_tag_day_tag"],
                "time": {
                    "temporal_role": "temporal_role.reldemo_account_tag_day_date_day",
                    "grain": "day",
                },
                "order_by": [
                    {"field": "dimension.reldemo_account_tag_day_tag", "direction": "ASC"}
                ],
            }
        )
        assert tag_rows["row_count"] == 6
        rendered = tag_rows["explain"]["rendered_sql"]
        assert "rel_reldemo_account_tag_daily AS (" in rendered
        assert "JSON_EXTRACT_STRING" in rendered
        assert "UNNEST(STRING_SPLIT" in rendered

        sends = runtime.query(
            {
                "version": 1,
                "select": [{"as": "sends", "expression": {"metric": "metric.reldemo.sends"}}],
                "group_by": ["dimension.reldemo_send_channel"],
                "time": {"temporal_role": "temporal_role.reldemo_send_sent_at", "grain": "day"},
            }
        )
        assert sends["row_count"] == 4
        send_sql = sends["explain"]["rendered_sql"]
        assert "UNION ALL" in send_sql
        assert "rel_reldemo_send_feed AS (" in send_sql
        assert "rel_reldemo_account_tag_daily" not in send_sql
        assert "rel_reldemo_signup_send_matches" not in send_sql

        matches = runtime.query(
            {
                "version": 1,
                "select": [
                    {"as": "matched", "expression": {"metric": "metric.reldemo.matched_signups"}}
                ],
                "group_by": ["dimension.reldemo_signup_match_account_id"],
                "time": {
                    "temporal_role": "temporal_role.reldemo_signup_match_signed_at",
                    "grain": "day",
                },
            }
        )
        assert matches["row_count"] == 3
        match_sql = matches["explain"]["rendered_sql"]
        assert "rel_reldemo_signup_send_matches__1_attribution_join AS (" in match_sql
        assert "ROW_NUMBER() OVER" in match_sql
        assert "DATE_DIFF('day'" in match_sql
    finally:
        runtime.close()


def test_relation_ctes_are_dependency_pruned_and_ordered(tmp_path: Path, monkeypatch):
    package_dir = _write_relation_demo(tmp_path)
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"relation_demo": str(package_dir)}
    )
    runtime = Runtime("relation_demo")
    try:
        physical = runtime.compile(
            {
                "version": 1,
                "select": [
                    {"as": "rows", "expression": {"metric": "metric.reldemo.raw_account_rows"}}
                ],
                "time": {
                    "temporal_role": "temporal_role.reldemo_account_state_valid_from",
                    "grain": "day",
                },
            }
        )
        physical_sql = physical["explain"]["rendered_sql"]
        assert "rel_reldemo_" not in physical_sql

        tag_daily = runtime.compile(
            {
                "version": 1,
                "select": [
                    {"as": "rows", "expression": {"metric": "metric.reldemo.account_tag_rows"}}
                ],
                "group_by": ["dimension.reldemo_account_tag_day_tag"],
                "time": {
                    "temporal_role": "temporal_role.reldemo_account_tag_day_date_day",
                    "grain": "day",
                },
            }
        )
        tag_sql = tag_daily["explain"]["rendered_sql"]
        assert tag_sql.index("rel_reldemo_date_spine AS (") < tag_sql.index(
            "rel_reldemo_account_tag_daily AS ("
        )
        assert "rel_reldemo_send_feed" not in tag_sql
        assert "rel_reldemo_signup_send_matches" not in tag_sql
        assert "rel_reldemo_eligible_account_states" not in tag_sql
    finally:
        runtime.close()


def test_relation_anti_join_and_date_add_execute_with_typed_sql(tmp_path: Path, monkeypatch):
    package_dir = _write_relation_demo(tmp_path)
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"relation_demo": str(package_dir)}
    )
    runtime = Runtime("relation_demo")
    try:
        result = runtime.query(
            {
                "version": 1,
                "select": [
                    {"as": "rows", "expression": {"metric": "metric.reldemo.eligible_account_rows"}}
                ],
                "time": {
                    "temporal_role": "temporal_role.reldemo_eligible_account_state_next_valid_from",
                    "grain": "day",
                },
            }
        )
        assert result["row_count"] == 1
        assert result["rows"][0]["rows"] == 1
        assert str(
            result["rows"][0]["temporal_role.reldemo_eligible_account_state_next_valid_from__day"]
        ).startswith("2024-01-02")
        rendered = result["explain"]["rendered_sql"]
        assert "NOT EXISTS" in rendered
        assert "DATE_ADD(src.valid_from, INTERVAL (1) DAY)" in rendered
    finally:
        runtime.close()


def test_relation_dependency_cycle_fails_only_when_required(tmp_path: Path, monkeypatch):
    package_dir = tmp_path / "cycle_demo"
    package_dir.mkdir()
    seed = package_dir / "seed.sql"
    seed.write_text(
        "CREATE TABLE physical_rows (id INTEGER); INSERT INTO physical_rows VALUES (1);",
        encoding="utf-8",
    )
    package = {
        "schema_version": 1,
        "package": {
            "id": "cycle_demo",
            "namespace": "cycle",
            "warehouse": "duckdb",
            "default_db": str(tmp_path / "cycle.duckdb"),
            "seed": {"kind": "sql_script", "source": str(seed)},
        },
        "graph": {
            "entities": {
                "cycle_a": {"key": ["id"], "model": "cycle_a"},
                "physical": {"key": ["id"], "model": "physical_rows"},
            }
        },
        "relations": {
            "a": {"output_name": "rel_cycle_a", "source": "rel_cycle_b", "columns": ["id"]},
            "b": {"output_name": "rel_cycle_b", "source": "rel_cycle_a", "columns": ["id"]},
        },
        "models": {
            "cycle_a": {
                "relation": "a",
                "grain": ["id"],
                "keys": {"primary": ["id"]},
                "measures": {
                    "cycle_rows": {
                        "label": "Cycle rows",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["id"],
                    }
                },
            },
            "physical_rows": {
                "relation": "physical_rows",
                "grain": ["id"],
                "keys": {"primary": ["id"]},
                "measures": {
                    "physical_rows": {
                        "label": "Physical rows",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["id"],
                    }
                },
            },
        },
    }
    (package_dir / "package.yml").write_text(
        yaml.safe_dump(package, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setattr(
        config_module, "list_package_paths", lambda: {"cycle_demo": str(package_dir)}
    )
    runtime = Runtime("cycle_demo")
    try:
        physical = runtime.query(
            {
                "version": 1,
                "select": [{"as": "rows", "expression": {"metric": "metric.cycle.physical_rows"}}],
            }
        )
        assert physical["rows"] == [{"rows": 1}]

        with pytest.raises(SemanticLayerError) as exc:
            runtime.compile(
                {
                    "version": 1,
                    "select": [{"as": "rows", "expression": {"metric": "metric.cycle.cycle_rows"}}],
                }
            )
        assert exc.value.code == "INVALID_CONFIG"
        assert "cycle" in str(exc.value).lower()
    finally:
        runtime.close()


def test_relation_join_pre_aggregate_controls_render_boundaries(tmp_path: Path):
    package_dir = tmp_path / "preagg_demo"
    package_dir.mkdir()
    package = {
        "schema_version": 1,
        "package": {
            "id": "preagg_demo",
            "namespace": "preagg",
            "warehouse": "duckdb",
            "default_db": str(tmp_path / "preagg.duckdb"),
            "seed": {"kind": "sql_script", "source": str(package_dir / "seed.sql")},
        },
        "graph": {"entities": {"aligned": {"key": ["account_id"], "model": "aligned"}}},
        "relations": {
            "aligned": {
                "source": "left_events",
                "columns": ["account_id", "value"],
                "steps": [
                    {
                        "join": {
                            "type": "full outer",
                            "relation": "right_events",
                            "on": [{"left": "account_id", "right": "account_id"}],
                            "require_pre_aggregate": True,
                            "pre_aggregate": {
                                "left": {
                                    "dimensions": {"account_id": "account_id"},
                                    "aggregates": {
                                        "left_value": {"function": "sum", "expr": "value"}
                                    },
                                },
                                "right": {
                                    "dimensions": {"account_id": "account_id"},
                                    "aggregates": {
                                        "right_value": {"function": "sum", "expr": "value"}
                                    },
                                },
                            },
                            "select": {
                                "account_id": {
                                    "kind": "column",
                                    "table": "left",
                                    "column": "account_id",
                                },
                                "left_value": {
                                    "kind": "column",
                                    "table": "left",
                                    "column": "left_value",
                                },
                                "right_value": {
                                    "kind": "column",
                                    "table": "right",
                                    "column": "right_value",
                                },
                            },
                        }
                    }
                ],
            }
        },
        "models": {
            "aligned": {
                "relation": "aligned",
                "grain": ["account_id"],
                "keys": {"primary": ["account_id"]},
                "measures": {
                    "aligned_rows": {
                        "label": "Aligned rows",
                        "kind": "entity_count",
                        "accumulation": "event",
                        "value_type": "count",
                        "entity_key": ["account_id"],
                    }
                },
            }
        },
    }
    (package_dir / "seed.sql").write_text(
        "CREATE TABLE left_events (account_id INTEGER, value INTEGER); CREATE TABLE right_events (account_id INTEGER, value INTEGER);",
        encoding="utf-8",
    )
    (package_dir / "package.yml").write_text(
        yaml.safe_dump(package, sort_keys=False), encoding="utf-8"
    )
    config = load_package_config(str(package_dir))
    relation = next(row for row in config.relations if row.id == "relation.preagg.aligned")
    ctes, _ = lower_relation(relation, warehouse=config.package.warehouse)
    rendered = "\n".join(
        render_select(SqlSelect(select=[SqlField(SqlLiteral(1), "one")], ctes=ctes)).splitlines()
    )
    assert "rel_preagg_aligned__left_preaggregate AS (" in rendered
    assert "rel_preagg_aligned__right_preaggregate AS (" in rendered
    assert "FULL OUTER JOIN" in rendered

    bad = yaml.safe_load(yaml.safe_dump(package))
    bad["relations"]["aligned"]["steps"][0]["join"].pop("pre_aggregate")
    (package_dir / "package.yml").write_text(yaml.safe_dump(bad, sort_keys=False), encoding="utf-8")
    bad_config = load_package_config(str(package_dir))
    bad_relation = next(row for row in bad_config.relations if row.id == "relation.preagg.aligned")
    with pytest.raises(SemanticLayerError) as exc:
        lower_relation(bad_relation, warehouse=bad_config.package.warehouse)
    assert "pre-aggregation boundary" in str(exc.value)


def test_sql_set_query_renders_union_all_without_from_clause():
    query = SqlSetQuery(
        "UNION ALL",
        [
            SqlSelect(select=[SqlField(SqlLiteral(1), "value")]),
            SqlSelect(select=[SqlField(SqlLiteral(2), "value")]),
        ],
    )

    assert (
        render_select(SqlSelect(select=[SqlField(SqlLiteral(1), "one")], ctes=[]))
        == "SELECT\n  1 AS one"
    )
    rendered = "\nUNION ALL\n".join(render_select(item) for item in query.queries)
    assert rendered == "SELECT\n  1 AS value\nUNION ALL\nSELECT\n  2 AS value"
