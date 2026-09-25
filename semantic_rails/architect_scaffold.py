"""The one Semantic Rails project scaffold.

Every surface that creates a package (the Architect MCP's ``create_project``
and the CLI's ``init``, ``project new`` and setup wizard) builds it from a
:class:`ProjectSpec` here, so each creates the same files and object ids.

The scaffold is warehouse-aware. A DuckDB package either gets a two-row starter
CSV seed, so it runs immediately, or reads a database another tool builds
(``seed.kind: external``, e.g. dbt). Other warehouses get a ``connection``
block whose secrets are named by environment variable only. Every package is
strict (``schema_strict: true``) and ships a ``.gitignore`` for build outputs.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from .config import SEED_KIND_EXTERNAL
from .config_parts.package_loader import _slug as _id_slug  # the loader's id slug
from .dialects import supported_warehouses, warehouse_connector
from .errors import SemanticLayerError

DataMode = Literal["starter", "external"]

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_GITIGNORE = """\
# Built by Semantic Rails or by your warehouse tooling; the YAML is the source.
*.duckdb
*.duckdb.wal
*.tmp
.compiled/
.architect/
"""
_META = {"owner_team": "analytics", "review_priority": "medium", "change_risk": "low"}


@dataclass(frozen=True)
class ProjectWarehouse:
    """Where the package's data lives."""

    kind: str = "duckdb"
    data: DataMode = "starter"
    """DuckDB only: ``starter`` seeds a two-row CSV; ``external`` reads a database
    another tool builds. Other warehouses always read existing data."""
    default_db: str = ""
    """DuckDB: the database path inside the package (default ``data/<package_id>.duckdb``)."""
    connection_kind: str = ""
    connection_name: str = ""
    connection_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FirstModel:
    """The first model; a strict package needs one model and one metric to validate."""

    entity: str = "event"
    relation: str = "raw_events"
    primary_key: str = "event_id"
    time_column: str = "occurred_at"
    amount_column: str = ""
    """A numeric column summed into a ``total_amount`` metric; empty for none
    (starter data always has an ``amount`` column)."""
    dimension_column: str = ""
    """A categorical column; the starter data always has ``event_type``."""


@dataclass(frozen=True)
class ProjectSpec:
    package_id: str
    description: str = ""
    warehouse: ProjectWarehouse = field(default_factory=ProjectWarehouse)
    first_model: FirstModel = field(default_factory=FirstModel)
    environments: tuple[str, ...] = ("development", "staging", "production")


def slug(value: str, *, fallback: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "")).strip("_")
    while "__" in out:
        out = out.replace("__", "_")
    return out or fallback


def _title(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", " ").split()) or value


def _identifier(value: str, *, field_name: str, dotted: bool = False) -> str:
    text = str(value or "").strip()
    parts = text.split(".") if dotted else [text]
    # A relation is a dotted sequence of names, not a SQL fragment or path.
    # The SQL renderer quotes each component (including names with hyphens)
    # rather than requiring the warehouse's unquoted-identifier spelling.
    valid_parts = (
        all(part and part.isprintable() and not any(ch in "/\\" for ch in part) for part in parts)
        if dotted
        else all(_IDENTIFIER.fullmatch(part) for part in parts)
    )
    if not text or len(parts) > 3 or not valid_parts:
        shape = "a SQL identifier, optionally schema-qualified" if dotted else "a SQL identifier"
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{field_name} must be {shape} that exists in the warehouse (got {text!r})",
            details={"field": field_name, "value": text},
        )
    return text


def project_warehouse_options() -> list[dict[str, Any]]:
    """What a warehouse picker can offer, from the dialect registry."""
    options: list[dict[str, Any]] = []
    for kind in supported_warehouses():
        connector = warehouse_connector(kind)
        if connector is None:
            continue
        options.append(
            {
                "kind": kind,
                "executable": bool(connector.adapter),
                "data_modes": ["starter", "external"] if connector.requires_seed else ["external"],
                "default_db": connector.requires_default_db,
                "connection_kinds": list(connector.connection_kinds),
                "connection_options": list(connector.connection_options),
                "connection_name_required": connector.requires_connection_name,
            }
        )
    return options


def project_setup_questions(spec: ProjectSpec | None = None) -> list[dict[str, Any]]:
    """The ordered setup questions, with defaults from ``spec``.

    ``when`` names the answers a question depends on, so a client asks it only
    when they match (``connection_kind`` only for a non-DuckDB warehouse, say).
    """
    spec = spec or ProjectSpec(package_id="my_semantic_package")
    model = spec.first_model
    warehouses = [row["kind"] for row in project_warehouse_options() if row["executable"]]
    return [
        {"id": "package_id", "prompt": "Package directory name?", "default": spec.package_id},
        {
            "id": "description",
            "prompt": "What business domain does this package govern?",
            "default": spec.description or "Semantic Rails package managed through Architect MCP.",
        },
        {
            "id": "warehouse",
            "prompt": "Which warehouse holds the data?",
            "default": spec.warehouse.kind,
            "choices": warehouses,
        },
        {
            "id": "data",
            "prompt": "Start from a two-row starter CSV, or read a database another tool "
            "(such as dbt) builds?",
            "default": spec.warehouse.data,
            "choices": ["starter", "external"],
            "when": {"warehouse": ["duckdb"]},
        },
        {
            "id": "default_db",
            "prompt": "DuckDB database path inside the package (the file dbt writes, for "
            "external data)?",
            "default": spec.warehouse.default_db or f"data/{spec.package_id}.duckdb",
            "when": {"warehouse": ["duckdb"]},
        },
        {
            "id": "connection_kind",
            "prompt": "How does Semantic Rails connect to the warehouse?",
            "default": spec.warehouse.connection_kind,
            "choices_by_warehouse": {
                row["kind"]: row["connection_kinds"]
                for row in project_warehouse_options()
                if row["connection_kinds"]
            },
            "when": {"warehouse": [kind for kind in warehouses if kind != "duckdb"]},
        },
        {
            "id": "connection_name",
            "prompt": "Named Snowflake connection or profile?",
            "default": spec.warehouse.connection_name,
            "when": {"warehouse": ["snowflake"]},
        },
        {
            "id": "connection_options",
            "prompt": "Connection options as a JSON object; name credentials with *_env keys, "
            'never literal secrets (for example {"host_env":"PGHOST"})?',
            "default": "{}",
            "options_by_warehouse": {
                row["kind"]: row["connection_options"]
                for row in project_warehouse_options()
                if row["connection_options"]
            },
            "when": {"warehouse": [kind for kind in warehouses if kind != "duckdb"]},
        },
        {
            "id": "first_entity",
            "prompt": "First business entity to model?",
            "default": model.entity,
        },
        {
            "id": "relation",
            "prompt": "Which table or view backs it (schema-qualified if needed)?",
            "default": model.relation,
        },
        {
            "id": "primary_key",
            "prompt": "Which column identifies one row?",
            "default": model.primary_key,
        },
        {
            "id": "time_column",
            "prompt": "Which timestamp or date column anchors the first metric?",
            "default": model.time_column,
        },
        {
            "id": "amount_column",
            "prompt": "Which numeric column should be summed into a first metric (blank for none)?",
            "default": model.amount_column,
        },
        {
            "id": "dimension_column",
            "prompt": "A categorical column to group by (blank for none)?",
            "default": model.dimension_column,
        },
    ]


@dataclass(frozen=True)
class _Plan:
    package_id: str
    description: str
    warehouse: ProjectWarehouse
    entity: str
    model_id: str
    relation: str
    primary_key: str
    time_column: str
    amount_column: str
    dimension_column: str
    environments: tuple[str, ...]


def _plan(spec: ProjectSpec) -> _Plan:
    package_id = slug(spec.package_id, fallback="semantic_project")
    warehouse = spec.warehouse
    kind = str(warehouse.kind or "duckdb").strip().lower()
    connector = warehouse_connector(kind)
    if connector is None:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"unsupported warehouse {kind!r} (supported: {', '.join(supported_warehouses())})",
            details={"warehouse": kind},
        )
    is_duckdb = connector.requires_seed
    if warehouse.data not in ("starter", "external"):
        raise SemanticLayerError(
            "INVALID_CONFIG", f"data must be 'starter' or 'external' (got {warehouse.data!r})"
        )
    if not is_duckdb and warehouse.data == "starter":
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"starter data is DuckDB-only; a {kind} package reads existing tables (data: external)",
            details={"warehouse": kind},
        )
    if is_duckdb and (warehouse.connection_kind or warehouse.connection_options):
        raise SemanticLayerError(
            "INVALID_CONFIG", "duckdb packages take no connection; set default_db instead"
        )
    if connector.connection_kinds and warehouse.connection_kind not in connector.connection_kinds:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{kind} packages need connection_kind in [{', '.join(connector.connection_kinds)}]",
            details={"warehouse": kind, "connection_kind": warehouse.connection_kind},
        )
    model = spec.first_model
    entity = slug(model.entity, fallback="event")
    starter = is_duckdb and warehouse.data == "starter"
    if starter:
        # Starter names describe a CSV this scaffold writes, so they are made safe.
        relation = slug(model.relation, fallback="raw_events")
        primary_key = slug(model.primary_key, fallback="event_id")
        time_column = slug(model.time_column, fallback="occurred_at")
        amount_column = slug(model.amount_column or "amount", fallback="amount")
        dimension_column = slug(model.dimension_column or "event_type", fallback="event_type")
    else:
        # Everything else must name what the warehouse already holds.
        relation = _identifier(model.relation, field_name="relation", dotted=True)
        primary_key = _identifier(model.primary_key, field_name="primary_key")
        time_column = _identifier(model.time_column, field_name="time_column")
        amount_column = (
            _identifier(model.amount_column, field_name="amount_column")
            if model.amount_column
            else ""
        )
        dimension_column = (
            _identifier(model.dimension_column, field_name="dimension_column")
            if model.dimension_column
            else ""
        )
    return _Plan(
        package_id=package_id,
        description=spec.description or f"{_title(package_id)} Semantic Rails package.",
        warehouse=ProjectWarehouse(
            kind=kind,
            data="starter" if starter else "external",
            default_db=(warehouse.default_db or f"data/{package_id}.duckdb") if is_duckdb else "",
            connection_kind=warehouse.connection_kind,
            connection_name=warehouse.connection_name,
            connection_options=dict(warehouse.connection_options),
        ),
        entity=entity,
        model_id=entity if entity.endswith("s") else f"{entity}s",
        relation=relation,
        primary_key=primary_key,
        time_column=time_column,
        amount_column=amount_column,
        dimension_column=dimension_column,
        environments=tuple(spec.environments),
    )


def _package_document(plan: _Plan) -> dict[str, Any]:
    package: dict[str, Any] = {
        "id": plan.package_id,
        "namespace": plan.package_id,
        "name": plan.package_id,
        "description": plan.description,
        "warehouse": plan.warehouse.kind,
    }
    if plan.warehouse.default_db:
        package["default_db"] = plan.warehouse.default_db
        package["seed"] = (
            {"kind": "csv_dir_duckdb", "source": f"data/{plan.package_id}_csv"}
            if plan.warehouse.data == "starter"
            else {"kind": SEED_KIND_EXTERNAL}
        )
    else:
        connection: dict[str, Any] = {"kind": plan.warehouse.connection_kind}
        if plan.warehouse.connection_name:
            connection["name"] = plan.warehouse.connection_name
        if plan.warehouse.connection_options:
            connection["options"] = dict(plan.warehouse.connection_options)
        package["connection"] = connection
    package["schema_strict"] = True
    package["environments"] = list(plan.environments)
    return {
        "schema_version": 1,
        "package": package,
        "defaults": {
            "dimension": {"groupable": True, "filterable": True},
            "time": {
                "timezone": "UTC",
                "default_query_axis": False,
                "supported_grains": ["day", "week", "month", "quarter", "year"],
            },
            "measure": {"subject_entity": "self", "aggregation_entity": "self"},
            "relationship": {"traversal": ["forward", "reverse"]},
        },
    }


def _model_document(plan: _Plan) -> dict[str, Any]:
    entity_title = _title(plan.entity)
    rows = plan.entity.replace("_", " ")
    measures: dict[str, Any] = {
        f"{plan.entity}_count": {
            "label": f"{entity_title} count",
            "description": f"Count of unique {rows} rows.",
            "kind": "entity_count",
            "entity_key": plan.primary_key,
            "accumulation": {"kind": "event"},
            "value_type": "count",
            "meta": dict(_META),
        }
    }
    if plan.amount_column:
        measures["total_amount"] = {
            "label": "Total amount",
            "description": f"Sum of {plan.amount_column} over {rows} rows.",
            "kind": "aggregate",
            "expr": plan.amount_column,
            "default_agg": "sum",
            "accumulation": {"kind": "flow"},
            "value_type": "number",
            "meta": dict(_META),
        }
    model: dict[str, Any] = {
        "id": plan.model_id,
        "label": f"{entity_title}s" if not entity_title.endswith("s") else entity_title,
        "description": f"One row per {rows}.",
        "relation": plan.relation,
        "entities": {plan.entity: {}},
        "times": {
            plan.time_column: {
                "label": _title(plan.time_column),
                "column": plan.time_column,
                "kind": "timestamp",
                "class": "event_time",
                "default": True,
            }
        },
    }
    if plan.dimension_column:
        model["dimensions"] = {
            plan.dimension_column: {"label": _title(plan.dimension_column), "kind": "categorical"}
        }
    model["measures"] = measures
    return {"model": model}


def _metrics_document(plan: _Plan) -> dict[str, Any]:
    rows = plan.entity.replace("_", " ")
    metrics: dict[str, Any] = {
        f"{plan.entity}_count": {
            "label": f"{_title(plan.entity)} count",
            "description": f"Count of unique {rows} rows.",
            "kind": "aggregate",
            "measure": f"{plan.entity}_count",
            "value_type": "count",
            "meta": dict(_META),
        }
    }
    if plan.amount_column:
        metrics["total_amount"] = {
            "label": "Total amount",
            "description": f"Governed total of {plan.amount_column} for {plan.package_id}.",
            "kind": "aggregate",
            "measure": "total_amount",
            "value_type": "number",
            "meta": dict(_META),
        }
    return {"metrics": metrics}


def _examples_and_tests(plan: _Plan) -> tuple[dict[str, Any], dict[str, Any]]:
    ns = plan.package_id
    temporal_role = f"temporal_role.{ns}_{plan.entity}_{_id_slug(plan.time_column)}"
    metric_key = "total_amount" if plan.amount_column else f"{plan.entity}_count"
    query: dict[str, Any] = {
        "version": 1,
        "select": [{"expression": {"metric": f"metric.{ns}.{metric_key}"}, "as": metric_key}],
        "time": {"temporal_role": temporal_role, "grain": "day"},
        "order_by": [{"field": "time", "direction": "ASC"}],
        "limit": 10,
    }
    if plan.dimension_column:
        query["group_by"] = [f"dimension.{ns}_{plan.entity}_{_id_slug(plan.dimension_column)}"]
    examples = {
        "examples": {
            f"starter_{metric_key}_by_day": {
                "question": f"{_title(metric_key)} by day",
                "query": query,
                "expected_shape": {"min_rows": 1},
            }
        }
    }
    tests = {
        "tests": {
            "starter_count_returns_rows": {
                "kind": "query_row_count_bounds",
                "query": {
                    "version": 1,
                    "select": [
                        {
                            "expression": {"measure": f"measure.{ns}.{plan.entity}_count"},
                            "as": "row_count",
                        }
                    ],
                    "time": {"temporal_role": temporal_role, "grain": "day"},
                    "limit": 10,
                },
                "min_rows": 1,
            }
        }
    }
    return examples, tests


def _starter_csv(plan: _Plan) -> str:
    header = [plan.primary_key, plan.time_column, plan.dimension_column]
    rows = [["1", "2026-01-01T09:00:00", "starter"], ["2", "2026-01-02T09:00:00", "follow_up"]]
    if plan.amount_column:
        header.append(plan.amount_column)
        rows[0].append("100.0")
        rows[1].append("75.5")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def project_scaffold_files(spec: ProjectSpec) -> dict[str, bytes]:
    """The files a new package consists of, keyed by path inside the package."""
    plan = _plan(spec)
    examples, tests = _examples_and_tests(plan)

    def dump(document: dict[str, Any]) -> bytes:
        return yaml.safe_dump(document, sort_keys=False, allow_unicode=False).encode("utf-8")

    files = {
        "package.yml": dump(_package_document(plan)),
        "graph.yml": dump(
            {
                "graph": {
                    "entities": {
                        plan.entity: {
                            "label": _title(plan.entity),
                            "key": [plan.primary_key],
                            "model": plan.model_id,
                            "allowed_as_root": True,
                        }
                    }
                }
            }
        ),
        f"models/core/{plan.model_id}.yml": dump(_model_document(plan)),
        "metrics/core.yml": dump(_metrics_document(plan)),
        "examples/core.yml": dump(examples),
        "tests/core.yml": dump(tests),
        ".gitignore": _GITIGNORE.encode("utf-8"),
    }
    if plan.warehouse.data == "starter":
        files[f"data/{plan.package_id}_csv/{plan.relation}.csv"] = _starter_csv(plan).encode(
            "utf-8"
        )
    return files


def normalized_package_id(spec: ProjectSpec) -> str:
    return slug(spec.package_id, fallback="semantic_project")
