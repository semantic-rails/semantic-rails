"""Guided authoring wizards for models, dimensions, times, measures,
metrics and segments.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Protocol

import yaml

from .. import architect_introspection as introspection
from ..architect_service import ArchitectMutation, ArchitectProject
from ..cli.common import _quote, _ref_label, _runtime_from_ref, _slug, _title
from ..cli.output import _authoring_error_messages, _authoring_warning_messages
from ..cli.reports import project_validation_report
from ..config import _derive_measure_semantics, load_package_config
from ..config_validation import PackageReference
from ..errors import SemanticLayerError
from ..expressions import (
    AggregateExpr,
    ArithmeticExpr,
    MetricRecipeRefExpr,
    OffsetWindowExpr,
    resolve_filter_dimension,
)
from ..schema import MeasureConfig, MetricConfig, PackageConfig
from ..segments import _metric_root_entity
from .backend import current_backend
from .prompts import (
    _author_choice,
    _author_confirm,
    _author_multi_choice,
    _author_prompt,
    _author_slug_prompt,
    _AuthoringCancelled,
)

_AUTHORING_KINDS = ("model", "dimension", "time", "measure", "metric", "segment", "calendar")


class Undoable(Protocol):
    """What the REPL's `undo` needs from an authoring change."""

    @property
    def report(self) -> dict[str, Any]: ...

    @property
    def project_path(self) -> Path: ...

    def undo(self) -> dict[str, Any]: ...


@dataclass
class _Mutations:
    """Changes one wizard run made (a measure, then the metric that uses it).

    ``undo`` restores both through one project transaction after checking both files.
    """

    parts: list[ArchitectMutation]

    @property
    def report(self) -> dict[str, Any]:
        return self.parts[-1].report

    @property
    def project_path(self) -> Path:
        return self.parts[-1].project_path

    def undo(self) -> dict[str, Any]:
        return ArchitectMutation.undo_together(self.parts)


_AUTHORING_ALIASES = {
    "entity": "model",
    "model/entity": "model",
    "time_dimension": "time",
    "times": "time",
    "dimensions": "dimension",
    "measures": "measure",
    "metrics": "metric",
    "segments": "segment",
    "calendars": "calendar",
}


def _run_authoring_flow(
    current_ref: PackageReference,
    *,
    requested_kind: str = "",
) -> Undoable | None:
    try:
        if not sys.stdin.isatty():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                (
                    "Guided authoring needs an interactive terminal. Re-run `semantic-rails repl "
                    f"--path {_quote(current_ref.source_path)}` in a terminal, then type `author`."
                ),
            )
        source = Path(current_ref.source_path).expanduser().resolve()
        if not source.is_dir() or not (source / "package.yml").is_file():
            raise SemanticLayerError(
                "INVALID_CONFIG",
                (
                    "Guided authoring supports split-layout package directories. "
                    "Create one with `semantic-rails init <name>` or manage this legacy "
                    "single-file package directly."
                ),
                details={"source_path": str(source)},
            )
        project = ArchitectProject(source, workspace_root=source.parent)
        initial = project_validation_report(current_ref, mode="parse")
        if not initial.get("ok"):
            print("Cannot author safely while the package has parse errors.")
            for message in _authoring_error_messages(initial)[:5]:
                print(f"  [error] {message}")
            print("Fix those errors, then run `author` again.")
            return None

        inventory = project.inventory()
        kind = _resolve_authoring_kind(requested_kind, inventory)
        print()
        print(f"Authoring {_ref_label(current_ref)} - {kind}")
        print(
            "Press Ctrl-C, or type `cancel` at a text prompt or list, to stop without writing "
            "(where a list option contains `cancel`, typing it picks that option)."
        )

        before_warnings = set(_authoring_warning_messages(initial))
        dispatch = {
            "model": _author_model,
            "dimension": _author_dimension,
            "time": _author_time,
            "measure": _author_measure,
            "metric": _author_metric,
            "segment": _author_segment,
            "calendar": _author_calendar,
        }
        mutation = dispatch[kind](project, inventory, current_ref, before_warnings)
        return mutation
    except _AuthoringCancelled:
        print("Authoring cancelled; no files changed.")
        return None
    except (EOFError, KeyboardInterrupt):
        print("\nAuthoring cancelled; no files changed.")
        return None


def _resolve_authoring_kind(requested: str, inventory: dict[str, Any]) -> str:
    raw = str(requested or "").strip().lower().replace(" ", "_")
    raw = _AUTHORING_ALIASES.get(raw, raw)
    if raw:
        if raw not in _AUTHORING_KINDS:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Usage: author [model|dimension|time|measure|metric|segment|calendar]",
            )
        return raw

    recommended = "model"
    if _inventory_items(inventory, "model"):
        recommended = "measure" if not _inventory_items(inventory, "measure") else "metric"
    options = [
        ("model", "Model/entity - connect a table and define its business grain"),
        ("dimension", "Dimension - something people group or filter by"),
        ("time", "Time - when an event or state occurred"),
        ("measure", "Measure - a primitive count, sum, or aggregatable fact"),
        ("metric", "Metric - a stable governed KPI built from measures or metrics"),
        ("segment", "Segment - a reusable entity cohort, such as high-value customers"),
        ("calendar", "Calendar - the date spine rolling, prior-period and growth metrics need"),
    ]
    return _author_choice("What do you want to create or update?", options, default=recommended)


def _author_model(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    if (source := _table_source(project, ref)) is not None:
        mutation = _author_model_from_table(project, inventory, ref, before_warnings, *source)
        if mutation is not None:
            return mutation
    key, label, existing = _author_identity(project, inventory, "model", "orders")
    spec = dict(existing.get("spec", {}) or {}) if existing else {}
    existing_entity = _model_entity_defaults(project, key)
    relation = _author_prompt(
        "Warehouse table or relation (for example raw_orders)",
        str(spec.get("relation", key)),
    )
    # A new name must be a table in the listed database or a relation pipeline; a saved
    # relation kept by pressing Enter stays as it was.
    if source is not None and not (existing and relation == spec.get("relation")):
        pipelines = load_package_config(ref.source_path).relations
        if not any(relation in (row.id, row.name, row.output_name) for row in pipelines):
            _relation_columns(project, ref, relation)
    entity_key = _author_slug_prompt(
        "Business entity at one row of this model",
        str(existing_entity["key"]),
    )
    _check_entity_is_free(inventory, entity_key, model=key)
    primary_default = ", ".join(existing_entity.get("primary_key", []) or []) or f"{entity_key}_id"
    primary_key = [
        _slug(part, fallback=f"{entity_key}_id")
        for part in _author_prompt("Primary key column(s), comma separated", primary_default).split(
            ","
        )
        if part.strip()
    ]
    description = _author_prompt(
        "Description",
        str(spec.get("description", f"One row per {entity_key.replace('_', ' ')}.")),
    )
    preview = {
        "model": {
            "id": key,
            "label": label,
            "relation": relation,
            "entities": {entity_key: {}},
            "description": description,
        },
        "graph": {
            "entities": {
                entity_key: {
                    "label": _title(entity_key),
                    "key": primary_key,
                    "model": key,
                    "allowed_as_root": True,
                }
            }
        },
    }
    target = str(existing.get("relative_path", "")) if existing else f"models/core/{key}.yml"
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="model",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview=preview,
        apply=lambda: project.upsert_model(
            model_id=key,
            entity_key=entity_key,
            relation=relation,
            primary_key=primary_key,
            description=description,
            label=label,
        ),
        next_action="Add a dimension with `author dimension`.",
    )


def _author_calendar(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    """The package calendar: a date spine, one row per `date_day`, that time.fill reads."""

    calendars = {
        str((row.get("spec") or {}).get("model", "")): str(row.get("key", ""))
        for row in _inventory_items(inventory, "entity")
        if (row.get("spec") or {}).get("kind") == "time"
    }
    key, label, existing = _author_identity(
        project, inventory, "model", next(iter(calendars), "calendar")
    )
    if existing and key not in calendars:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Model `{key}` is not a calendar; give the calendar its own key."
        )
    spec = dict(existing.get("spec", {}) or {}) if existing else {}
    relation = _author_prompt(
        "Warehouse table with one row per day in a `date_day` column",
        str(spec.get("relation", key)),
    )
    columns = _relation_columns(project, ref, relation)
    if columns is not None and "date_day" not in columns:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"`{relation}` has no `date_day` column, so it can't be a calendar."
        )
    entity_key = calendars.get(key) or key
    _check_entity_is_free(inventory, entity_key, model=key)
    saved_times, saved = dict(spec.get("times") or {}), dict(spec.get("dimensions") or {})
    starts = [column for unit, column in _CALENDAR_COLUMNS.items() if unit != "day"]
    chosen = _author_multi_choice(
        "Period-start columns on the table (each adds that unit)",
        [(column, column) for column in starts],
        defaults=[c for c in starts if c in (saved if columns is None else columns)],
    )
    day = {"label": "Calendar day", "column": "date_day", "kind": "date", "class": "calendar_time"}
    times = {"date_day": {**day, **dict(saved_times.get("date_day") or {})}}
    dimensions = {
        column: {"label": _title(column), "kind": "date", **dict(saved.get(column) or {})}
        for column in chosen
    }
    # upsert_model merges: saved columns stay whether or not they are ticked.
    preview = {
        "model": {
            "id": key,
            "relation": relation,
            "times": {**saved_times, **times},
            "dimensions": {**saved, **dimensions},
        },
        "graph": {"entities": {entity_key: {"kind": "time", "key": ["date_day"], "model": key}}},
    }
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="calendar",
        key=key,
        label=label,
        existing=existing,
        target=str(existing.get("relative_path", "")) if existing else f"models/core/{key}.yml",
        preview=preview,
        apply=lambda: project.upsert_model(
            model_id=key,
            entity_key=entity_key,
            relation=relation,
            primary_key=["date_day"],
            times=times,
            dimensions=dimensions,
            label=label,
            calendar=True,
        ),
        next_action="Rolling, prior-period and growth recipes now appear in `author metric`.",
    )


def _relation_columns(
    project: ArchitectProject, ref: PackageReference, relation: str
) -> set[str] | None:
    """The relation's columns in the package's DuckDB file; None when there is no file to read.

    A relation the file doesn't have is refused, not waved through unchecked.
    """

    if _authoring_warehouse(ref) != "duckdb":
        return None
    try:
        with introspection.open_duckdb(
            introspection.package_duckdb_path(project.project_path)
        ) as warehouse:
            described = introspection.describe_table(warehouse, relation)
    except SemanticLayerError as exc:
        if exc.code in {"OBJECT_NOT_FOUND", "INVALID_QUERY"}:
            raise
        print(f"Can't check the table's columns: {exc}")
        return None
    return {str(column["name"]) for column in described["columns"]}


_TYPE_IT = "__type__"
_DEFAULT_META = {"owner_team": "analytics", "review_priority": "medium", "change_risk": "medium"}
_MONEY_WORDS = "amount revenue sales price cost total tax fee profit margin discount spend paid"


def _table_source(
    project: ArchitectProject, ref: PackageReference
) -> tuple[str, list[dict[str, Any]]] | None:
    """The package's DuckDB file and its tables, when the warehouse is DuckDB and readable."""

    if _authoring_warehouse(ref) != "duckdb":
        return None
    try:
        path = introspection.package_duckdb_path(project.project_path)
        with introspection.open_duckdb(path) as warehouse:
            tables = introspection.list_tables(warehouse)
    except SemanticLayerError as exc:
        seed = dict(_package_block(ref).get("seed") or {})
        # `external`: dbt (or another tool) builds the file; its message names `dbt build`.
        if exc.details.get("reason") == "database_missing" and seed.get("kind") not in {
            None,
            "",
            "external",
        }:
            print(
                f"Can't list the warehouse tables: {exc.details['duckdb_path']} isn't built yet. "
                "Build it from the package's seed files with `validate runtime`, then run "
                "`author model` again, or enter the table by hand."
            )
        else:
            print(f"Can't list the warehouse tables: {exc}. Enter the table by hand.")
        return None
    if not tables:
        print(f"{path} has no tables yet. Enter the table by hand.")
        return None
    with contextlib.suppress(Exception):  # only a hint; the tables listed are real
        runtime = _runtime_from_ref(ref)
        try:  # notes `validate runtime`'s STALE_SEED_DATABASE rebuild hint
            runtime._get_adapter()
        finally:
            runtime.close()
        for warning in runtime._seed_warnings:
            print(f"[warning] {warning['message']}")
    return path, tables


def _author_model_from_table(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
    path: str,
    tables: list[dict[str, Any]],
) -> ArchitectMutation | None:
    """Pick a table, confirm the suggested model, and write it in one change."""

    models = _inventory_items(inventory, "model")
    modeled = {(row.get("spec") or {}).get("relation"): row["key"] for row in models}
    options = []
    for table in tables:
        relation, size = table["relation"], table["rows_estimate"]
        detail = f"{table['kind']}, {table['columns']} columns"
        detail += "" if size is None else f", about {size:,} rows"
        detail += f"; modeled by {modeled[relation]}" if relation in modeled else ""
        options.append((relation, f"{relation} ({detail})"))
    options.append((_TYPE_IT, "Type a table name instead"))
    # Facts are usually the largest table; a calendar or lookup sorting first isn't the pick.
    unmodeled = [table for table in tables if table["relation"] not in modeled]
    default = (
        max(unmodeled, key=lambda table: table["rows_estimate"] or 0)["relation"]
        if unmodeled
        else _TYPE_IT
    )
    relation = _author_choice(f"Table to model (from {path})", options, default=default)
    if relation == _TYPE_IT:
        return None
    try:
        with introspection.open_duckdb(path) as warehouse:
            suggestion = introspection.suggest_model(warehouse, relation)
            described = introspection.describe_table(warehouse, relation)
    except SemanticLayerError as exc:
        print(f"Can't suggest a model for {relation}: {exc}. Enter the table by hand.")
        return None
    columns = [column["name"] for column in described["columns"]]

    entity = _author_slug_prompt("Business entity at one row of this table", suggestion["entity"])
    _check_entity_is_free(inventory, entity, model=None)
    suggested_key = suggestion["primary_key"] or {"columns": []}
    if suggested_key["columns"]:
        print(
            f"  Suggested key: {', '.join(suggested_key['columns'])} "
            f"({suggested_key['confidence']}: {suggested_key['reason']})"
        )
    key_columns: list[str] = []
    while not key_columns:
        key_columns = _author_multi_choice(
            "Primary key column(s)",
            [(column, column) for column in columns],
            defaults=suggested_key["columns"],
        )
        if not key_columns:
            print("Choose at least one key column; a model needs one row per key.")
    times = _pick_suggestions(
        "Time columns (the first is the default clock)",
        [item for item in suggestion["times"] if item["column"] not in key_columns],
        name="column",
        detail="kind",
    )
    dimensions = _pick_suggestions(
        "Dimensions to group and filter by",
        [item for item in suggestion["dimensions"] if item["column"] not in key_columns],
        name="column",
    )
    measures = _pick_suggestions(
        "Measures",
        [
            item
            for item in suggestion["measures"]
            if item["kind"] != "entity_count" or len(key_columns) == 1
        ],
        name="key",
        detail="aggregation",
    )
    amounts = [item["key"] for item in measures if item["kind"] != "entity_count"]
    money: set[str] = set()
    if amounts:
        money = set(
            _author_multi_choice(
                "Which of these are money amounts?",
                [(key, key) for key in amounts],
                # A `_cents` column isn't pre-checked: as currency it would print cents as dollars.
                defaults=[
                    key
                    for key in amounts
                    if any(w in key.lower() for w in _MONEY_WORDS.split())
                    and "cents" not in key.lower()
                ],
            )
        )
    currency = _author_prompt("Currency code for those amounts", "USD").upper() if money else ""
    if suggestion["foreign_keys"]:
        print("  Links found (not added here; relationship authoring adds them):")
    for link in suggestion["foreign_keys"][:5]:
        local, target = ", ".join(introspection.link_columns(link)), link["references"]
        print(f"    {local} -> {target['relation']}({', '.join(target['columns'])})")

    draft = introspection.upsert_model_draft(
        entity=entity,
        relation=relation,
        key_columns=key_columns,
        times=times,
        dimensions=dimensions,
        measures=measures,
    )
    for measure_key, measure in draft["measures"].items():
        measure.setdefault("meta", dict(_DEFAULT_META))
        if measure_key in money:
            measure.update(value_type="currency", currency=currency)
    model_id = draft["model_id"]
    if any(row["key"] == model_id for row in models):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"Model `{model_id}` already exists. To update it, run `author model`, pick "
            '"Type a table name instead" and enter its key.',
        )
    label = _title(model_id)
    if not _distinct(project, "model", model_id, label):
        return None  # `author model` then asks for the key and label
    preview = {
        "model": {
            "id": model_id,
            "label": label,
            "relation": relation,
            "entities": {entity: {}},
            **{
                part: draft[part] for part in ("times", "dimensions", "measures") if draft.get(part)
            },
        },
        "graph": {"entities": {entity: {"key": key_columns, "model": model_id}}},
    }
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="model",
        key=model_id,
        label=label,
        existing=None,
        target=f"models/core/{model_id}.yml",
        preview=preview,
        apply=lambda: project.upsert_model(**draft, label=label),
        next_action="Publish a measure as a metric with `author metric`.",
    )


def _pick_suggestions(
    label: str, items: list[dict[str, Any]], *, name: str, detail: str = ""
) -> list[dict[str, Any]]:
    """Checkboxes over suggestions; confident ones start checked, and every pick is kept."""

    if not items:
        return []
    options = []
    for item in items:
        extra = f"{item[detail]}; " if detail else ""
        options.append(
            (item[name], f"{item[name]} ({extra}{item['confidence']}: {item['reason']})")
        )
    confident = [item[name] for item in items if item["confidence"] != "low"]
    chosen = set(_author_multi_choice(label, options, defaults=confident))
    # The draft builder drops low-confidence items; a person who ticked one meant it.
    return [
        {**item, "confidence": "chosen"} if item["confidence"] == "low" else item
        for item in items
        if item[name] in chosen
    ]


def _check_entity_is_free(inventory: dict[str, Any], entity: str, *, model: str | None) -> None:
    owner = next(
        (
            str((row.get("spec") or {}).get("model", ""))
            for row in _inventory_items(inventory, "entity")
            if str(row.get("key", "")) == entity
            and str((row.get("spec") or {}).get("model", "")) != model
        ),
        None,
    )
    if owner is not None:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            (
                f"Entity `{entity}` already belongs to model `{owner}`. "
                "Choose a different entity key or manage its existing model."
            ),
        )


def _author_dimension(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    model = _select_model(inventory)
    key, label, existing = _author_identity(
        project, inventory, "dimension", "status", parent=str(model.get("key", ""))
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    kind = _kept_choice(
        "How should values behave?",
        [
            ("categorical", "Categorical - labels such as placed, shipped, cancelled"),
            ("boolean", "Boolean - true/false"),
            ("integer", "Integer - whole numbers that can be grouped or filtered"),
            ("continuous", "Continuous - numeric values used as dimensions"),
        ],
        current.get("kind"),
        "categorical",
    )
    column = _author_slug_prompt("Backing column", str(current.get("expr", key)))
    description = _author_prompt(
        "Description", str(current.get("description", f"{label} used for grouping and filtering."))
    )
    dimension = {**current, "label": label, "description": description, "kind": kind}
    if column != key:
        dimension["expr"] = column
    else:
        dimension.pop("expr", None)
    target = str(model.get("relative_path", ""))
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="dimension",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview={"dimensions": {key: dimension}},
        apply=lambda: _upsert_nested_model(project, model, dimensions={key: dimension}),
        next_action=f"Try `ls dimension {key}` after validation.",
    )


def _author_time(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    model = _select_model(inventory)
    model_key = str(model.get("key", ""))
    key, label, existing = _author_identity(
        project, inventory, "time", "occurred_at", parent=model_key
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    column = _author_slug_prompt("Date or timestamp column", str(current.get("column", key)))
    kind = _kept_choice(
        "Column type",
        [("timestamp", "Timestamp - date and time"), ("date", "Date - calendar date only")],
        current.get("kind"),
        "timestamp",
    )
    clock_class = _kept_choice(
        "What does this clock mean?",
        [
            ("event_time", "Event time - when an event happened"),
            ("state_time", "State time - when a snapshot was observed"),
            ("as_of_time", "As-of time - effective point for temporal joins"),
            ("calendar_time", "Calendar time - a date-spine or calendar model"),
        ],
        current.get("class"),
        "event_time",
    )
    model_times = dict((model.get("spec", {}) or {}).get("times", {}) or {})
    should_default = bool(current.get("default", not model_times))
    make_default = _author_confirm(
        "Use this as the model's default query clock?", default=should_default
    )
    time_spec: dict[str, Any] = {
        **current,
        "label": label,
        "column": column,
        "kind": kind,
        "class": clock_class,
        "default": make_default,
        "default_query_axis": make_default,
    }
    updates: dict[str, Any] = {key: time_spec}
    if make_default:
        for other_key, other_spec in model_times.items():
            if other_key == key:
                continue
            updates[str(other_key)] = {
                **dict(other_spec or {}),
                "default": False,
                "default_query_axis": False,
            }
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="time",
        key=key,
        label=label,
        existing=existing,
        target=str(model.get("relative_path", "")),
        preview={"times": updates},
        apply=lambda: _upsert_nested_model(project, model, times=updates),
        next_action="Run `validate runtime` when you are ready to test warehouse data.",
    )


def _author_measure(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    return _measure_change(project, inventory, ref, before_warnings)[0]


def _measure_change(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
    *,
    on_commit: Callable[[ArchitectMutation, str], None] | None = None,
) -> tuple[ArchitectMutation, str, str]:
    """Run the measure wizard; returns the change, the measure key and its model."""

    model = _select_model(inventory)
    key, label, existing = _author_identity(
        project, inventory, "measure", "revenue", parent=str(model.get("key", ""))
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    measure_kind = _kept_choice(
        "What primitive fact is this?",
        [
            ("aggregate", "Aggregate - sum, average, median, minimum, or maximum"),
            ("entity_count", "Entity count - distinct count of business keys"),
        ],
        current.get("kind"),
        "aggregate",
    )
    # A saved accumulation (a stock, a population) stays while the kind does.
    same_kind = str(current.get("kind", "")).strip().casefold() == measure_kind
    accumulation = current.get("accumulation") if same_kind else None
    description = _author_prompt(
        "Description", str(current.get("description", f"Governed {label.lower()} primitive."))
    )
    if measure_kind == "entity_count":
        entity_defaults = _model_entity_defaults(project, str(model.get("key", "")))
        entity_key = _author_slug_prompt(
            "Entity key column",
            str(
                current.get("entity_key", next(iter(entity_defaults.get("primary_key", [])), "id"))
            ),
        )
        measure: dict[str, Any] = {
            **current,
            "label": label,
            "description": description,
            "kind": "entity_count",
            "entity_key": entity_key,
            "accumulation": accumulation or {"kind": "event"},
            "value_type": "count",
            "meta": {
                "owner_team": "analytics",
                "review_priority": "medium",
                "change_risk": "medium",
                **dict(current.get("meta", {}) or {}),
            },
        }
        if not same_kind:
            for stale_key in ("expr", "default_agg", "currency", "rollup"):
                measure.pop(stale_key, None)
    else:
        expression = _author_prompt(
            "Column or scalar expression (for example amount_cents / 100.0)",
            str(current.get("expr", key)),
        )
        # The loader's aggregations for this accumulation, less percentile: a
        # measure cannot give it the `p` it needs, so only a saved one is offered.
        default_agg, allowed, _, _ = _derive_measure_semantics(
            {**current, "kind": "aggregate", "accumulation": accumulation}
        )
        aggregation = _kept_choice(
            "Default aggregation",
            [
                (value, value.replace("_", " ").title())
                for value in allowed
                if value != "percentile"
            ],
            current.get("default_agg"),
            default_agg,
        )
        value_type = _kept_choice("Result type", _VALUE_TYPES, current.get("value_type"), "number")
        if existing:
            _warn_on_new_aggregation(
                load_package_config(ref.source_path),
                _row_id(existing),
                aggregation,
                authored={_row_id(row) for row in _inventory_items(inventory, "metric")},
            )
        measure = {
            **current,
            "label": label,
            "description": description,
            "kind": "aggregate",
            "expr": expression,
            "default_agg": aggregation,
            "accumulation": accumulation or {"kind": "flow"},
            "value_type": value_type,
            "meta": {
                "owner_team": "analytics",
                "review_priority": "medium",
                "change_risk": "medium",
                **dict(current.get("meta", {}) or {}),
            },
        }
        if not same_kind:
            measure.pop("entity_key", None)
        if value_type == "currency":
            measure["currency"] = _author_prompt(
                "Currency code", str(current.get("currency", "USD"))
            ).upper()
    mutation = _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="measure",
        key=key,
        label=label,
        existing=existing,
        target=str(model.get("relative_path", "")),
        preview={"measures": {key: measure}},
        apply=lambda: _upsert_nested_model(project, model, measures={key: measure}),
        next_action="Publish this primitive as a stable KPI with `author metric`.",
        on_commit=on_commit,
    )
    return mutation, key, str(model.get("key", ""))


def _author_metric(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> Undoable:
    committed: list[ArchitectMutation] = []
    kept: list[str] = []

    def commit(part: ArchitectMutation, name: str) -> None:
        committed.append(part)
        kept.append(name)

    try:
        _metric_change(project, inventory, ref, before_warnings, commit)
    except BaseException as stopped:
        # Cancellation restores every committed part before the caller reports
        # "no files changed". A file edited since is a conflict: nothing is
        # restored, and the parts stay on the undo stack like any other change.
        rollback = ArchitectMutation.undo_together(committed) if committed else {}
        if rollback.get("status") != "undo_conflict":
            raise
        if str(stopped):
            print(f"[error] {stopped}")
        print("[error] Authoring stopped, but nothing was restored: a file changed afterward.")
        for relative_path in rollback.get("conflicting_files", []) or []:
            print(f"  conflict {project.project_path / relative_path}")
        print(f"  kept    {', '.join(kept)}")
        print("  undo    Type `undo` to restore them once the conflicting file is resolved.")
    return _Mutations(committed) if len(committed) > 1 else committed[0]


def _metric_change(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
    on_commit: Callable[[ArchitectMutation, str], None],
) -> ArchitectMutation:
    config = load_package_config(ref.source_path)

    def new_measure() -> dict[str, Any]:
        nonlocal config
        mutation, key, model = _measure_change(
            project, inventory, ref, before_warnings, on_commit=on_commit
        )
        before_warnings.update(_authoring_warning_messages(mutation.report.get("parse", {})))
        inventory.clear()
        inventory.update(project.inventory())
        config = load_package_config(ref.source_path)
        print("Back to the metric.")
        return next(
            row
            for row in _inventory_items(inventory, "measure")
            if str(row.get("key", "")) == key and str(row.get("model_key", "")) == model
        )

    def pick(label: str, rows: list[dict[str, Any]], default: str | None) -> dict[str, Any]:
        return _select_inventory_item(
            label, rows, default_key=default, create=("Create a new measure first", new_measure)
        )

    measures = _inventory_items(inventory, "measure")
    metrics = _inventory_items(inventory, "metric")
    if not measures and not _inventory_items(inventory, "model"):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Add a model with `author model` first; a metric publishes one of its measures.",
        )
    published_measures = {str((row.get("spec", {}) or {}).get("measure", "")) for row in metrics}
    recommended_measure = next(
        (row for row in measures if str(row.get("key", "")) not in published_measures),
        measures[0] if measures else {},
    )
    recommended_key = str(recommended_measure.get("key", "metric")) if measures else "metric"
    key, label, existing = _author_identity(project, inventory, "metric", recommended_key)
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    metric_id = str(current.get("as") or current.get("id") or "") or (
        f"metric.{key}" if "." in key else f"metric.{_authoring_namespace(project)}.{key}"
    )
    saved = _saved_metric(current, config, metric_id) if existing else _Saved()
    units = _calendar_units(config)
    recipes = [
        (recipe, text)
        for recipe, text in _RECIPES.items()
        if units or recipe not in _CALENDAR_RECIPES
    ]
    if existing and saved.recipe not in dict(recipes):
        # A shape these recipes cannot write back stays intact unless the
        # person explicitly chooses a different recipe.
        what = "derived expression" if current.get("kind") == "derived" else "expression"
        recipes.insert(0, (_PRESERVE, f"Keep this {what} unchanged"))
    if not units:
        print(
            "  Rolling windows, prior periods and growth need a package calendar (a date spine); "
            "add it with `author calendar`, and they appear here."
        )
    recipe = _author_choice(
        "Metric recipe",
        recipes,
        default=saved.recipe if saved.recipe in dict(recipes) else recipes[0][0],
    )
    description = _author_prompt(
        "Business definition",
        str(current.get("description", f"Governed definition of {label.lower()}.")),
    )
    spec: dict[str, Any] = {
        **current,
        "as": metric_id,
        "label": label,
        "description": description,
        "kind": current.get("kind") if recipe == _PRESERVE else _RECIPE_KINDS.get(recipe, recipe),
        "meta": {
            "owner_team": "analytics",
            "review_priority": "medium",
            "change_risk": "medium",
            **dict(current.get("meta", {}) or {}),
        },
    }

    def write(next_action: str) -> ArchitectMutation:
        return _apply_authoring_change(
            project,
            ref,
            before_warnings,
            kind="metric",
            key=key,
            label=label,
            existing=existing,
            target=str(existing.get("relative_path", ""))
            if existing
            else f"metrics/core/{_slug(key, fallback='metric')}.yml",
            preview={"metrics": {key: spec}},
            apply=lambda: project.upsert_metric(
                metric_key=key, spec=spec, group="core", replace=True
            ),
            next_action=next_action,
            on_commit=on_commit,
        )

    if recipe == _PRESERVE:
        return write("Review this derived metric with `validate`.")

    # Inputs first: every other default depends on whether they changed.
    rows = measures
    if recipe == "ratio":
        # Measures and other metrics, once each; a metric does not divide itself.
        operands: dict[str, dict[str, Any]] = {}
        for row in [*measures, *metrics]:
            if _row_id(row) and not (row.get("kind") == "metric" and row.get("key") == key):
                operands.setdefault(_row_id(row), row)
        rows = list(operands.values())
    first_default = saved.inputs[0] if saved.inputs else None
    if first_default is None and any(row.get("key") == key for row in rows):
        first_default = key
    selector = "Measure" if recipe in _TIME_RECIPES else "Measure to publish"
    inputs = [pick("Numerator" if recipe == "ratio" else selector, rows, first_default)]
    if recipe == "ratio":
        others = [row for row in rows if _row_id(row) != _row_id(inputs[0])]
        second = saved.inputs[1] if len(saved.inputs) > 1 else ""
        if others and not any(second in {row.get("key"), row.get("id")} for row in others):
            second = _row_id(_suggested_denominator(config, inputs[0], others))
        inputs.append(pick("Denominator", others, second))
    ids = tuple(_row_id(row) for row in inputs)
    # A measure created or managed at the Denominator step can be the numerator again.
    if len(set(ids)) < len(ids):
        raise SemanticLayerError(
            "INVALID_CONFIG", "A ratio needs two distinct measures or metrics."
        )
    retain = recipe == saved.recipe and ids == saved.inputs
    aggregation = saved.aggregation if retain else ""
    loaded = _loaded_input(config, inputs[0])
    name = label.lower()
    if recipe == "ratio":
        fields: dict[str, Any] = {
            "numerator": ids[0],
            "denominator": ids[1],
            "null_behavior": (retain and saved.null_behavior) or "null_if_zero",
        }
        example = f"How does {name} trend by month?"
    elif recipe == "filtered":
        clauses = _row_filter(
            config,
            inventory,
            inputs[0],
            saved=saved.row_filter if retain else None,
            require_dimension=bool(existing) and not retain,
        )
        fields = {
            "expression": {
                "kind": "aggregate",
                "measure": ids[0],
                "aggregation": aggregation or getattr(loaded, "default_aggregation", ""),
                "filter": {"all": clauses},
            }
        }
        example = f"What is {name} by month?"
    elif recipe == "aggregate":
        fields = {"measure": ids[0]}
        example = f"What is {name} by month?"
    else:
        if not _time_roles_of(config, inputs[0]):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{_TIME_RECIPES[recipe].split(' - ')[0]} needs a time axis, and "
                f"`{inputs[0].get('model_key') or inputs[0].get('key')}` has no time column. "
                "Add one with `author time`, then try again.",
            )
        fields, example = _time_recipe(
            recipe,
            ids[0],
            saved.params,
            name,
            aggregation or getattr(loaded, "default_aggregation", ""),
            units,
        )
    if aggregation and "measure" in fields:
        fields["aggregation"] = aggregation
    for stale_key in _KIND_FIELDS - set(fields):
        spec.pop(stale_key, None)
    spec.update(fields)

    # Unchanged inputs keep the saved result type, currency and clock;
    # changed inputs propose theirs.
    value_options = _RATIO_VALUE_TYPES if recipe == "ratio" else _VALUE_TYPES
    value_default = str(current.get("value_type") or "") if retain else ""
    if not value_default:
        types = [getattr(_loaded_input(config, row), "value_type", "") for row in inputs]
        if recipe == "ratio":
            value_default = "currency" if types == ["currency", "count"] else "ratio"
        elif recipe == "growth":
            value_default = "percent"
        elif types[0] in dict(value_options):
            value_default = types[0]
    value_type = _kept_choice("Result type", value_options, value_default, value_options[0][0])
    spec["value_type"] = value_type
    if value_type == "currency":
        currency = str(
            getattr(loaded, "currency", "")
            or (inputs[0].get("spec", {}) or {}).get("currency")
            or "USD"
        )
        if retain and current.get("value_type") == "currency" and current.get("currency"):
            currency = str(current["currency"])
        spec["currency"] = _author_prompt("Currency code", currency).upper()
    else:
        spec.pop("currency", None)
    temporal = _metric_time_role(config, inventory, inputs, current=saved.clock if retain else "")
    # One canonical effective clock: `temporal_role` takes precedence over
    # the loader's legacy `time` alias, so do not retain both after editing.
    spec.pop("time", None)
    if temporal:
        spec["temporal_role"] = temporal
    else:
        spec.pop("temporal_role", None)
    if not spec.get("examples"):
        spec["examples"] = [example]
    return write(f"Try `ask {example}`.")


# The wizard's recipes over time; "growth" is written as a derived metric.
_TIME_RECIPES = {
    "cumulative": "Running total - the measure from the start of the data to each period",
    "rolling": "Rolling window - the measure over a trailing window, such as 7 days",
    "period_to_date": "Period to date - month-, quarter- or year-to-date",
    "prior_period": "Prior period - the measure one or more periods earlier",
    "growth": "Growth - the change against a prior period, as a percent",
}
_RECIPES = {
    "aggregate": "Aggregate - publish one measure as a stable KPI",
    "filtered": "Filtered aggregate - one measure over only some rows, such as completed orders",
    "ratio": "Ratio - divide one measure/metric by another",
    **_TIME_RECIPES,
}
# These read a date spine: the engine needs a calendar entity (`kind: time`) to fill gaps.
_CALENDAR_RECIPES = frozenset({"rolling", "prior_period", "growth"})
_RECIPE_KINDS = {"filtered": "aggregate", "growth": "derived"}
# Recipes written as an `expression`; the others write named fields.
_EXPRESSION_RECIPES = frozenset({"filtered", "growth"})
_PRESERVE = "preserve"
# Fields that belong to one recipe; switching recipes drops the others.
_KIND_FIELDS = frozenset(
    {
        "measure",
        "aggregation",
        "numerator",
        "denominator",
        "null_behavior",
        "expression",
        "window",
        "offset",
        "period",
    }
)
_UNITS = [
    ("day", "Days"),
    ("week", "Weeks"),
    ("month", "Months"),
    ("quarter", "Quarters"),
    ("year", "Years"),
]
# The calendar column that fills each unit's periods, as the engine's time.fill reads it.
_CALENDAR_COLUMNS = {
    "day": "date_day",
    "week": "week_start",
    "month": "month_start",
    "quarter": "quarter_start",
    "year": "year_start",
}
_VALUE_TYPES = [
    ("number", "Number"),
    ("currency", "Currency"),
    ("percent", "Percent"),
    ("count", "Count"),
]
_RATIO_VALUE_TYPES = [
    ("percent", "Percent or share"),
    ("ratio", "Dimensionless ratio"),
    ("currency", "Currency per unit"),
]


@dataclass(frozen=True)
class _Saved:
    """A saved metric as recipe answers. ``recipe`` is empty for a new metric."""

    recipe: str = ""
    inputs: tuple[str, ...] = ()
    aggregation: str = ""
    row_filter: dict[str, Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    null_behavior: str = ""
    clock: str = ""


def _saved_metric(current: dict[str, Any], config: PackageConfig, metric_id: str) -> _Saved:
    """Read a saved metric back into recipe answers from the loader's resolved expression.

    The loader has already resolved package-relative and named references to
    canonical IDs. Anything the recipes cannot write back unchanged is
    ``preserve``: extra filter clauses or parameters, other kinds, and an
    authored expression where the recipe writes named fields.
    """

    loaded = next((row for row in config.metric_recipes if row.id == metric_id), None)
    if loaded is None:
        return _Saved(_PRESERVE)
    saved = _read_recipe(str(current.get("kind", "")), loaded.expression, config)
    if saved is None or (current.get("expression") and saved.recipe not in _EXPRESSION_RECIPES):
        return _Saved(_PRESERVE)
    return replace(saved, clock=loaded.temporal_role)


def _read_recipe(kind: str, expr: Any, config: PackageConfig) -> _Saved | None:
    def plain(node: Any) -> AggregateExpr | None:
        """A measure with at most an aggregation: all the recipe prompts can write."""

        simple = isinstance(node, AggregateExpr) and node == AggregateExpr(
            node.measure, node.aggregation
        )
        return node if simple else None

    if kind == "ratio" and isinstance(expr, ArithmeticExpr) and expr.op == "divide":
        ids = tuple(_operand_id(side) for side in (expr.left, expr.right))
        return _Saved("ratio", ids, null_behavior=expr.null_behavior) if all(ids) else None
    if kind == "derived" and isinstance(expr, ArithmeticExpr):
        # Growth: (now - prior) / prior, exactly as `_time_recipe` writes it.
        now = plain(expr.left.left) if isinstance(expr.left, ArithmeticExpr) else None
        prior = expr.right
        if (
            now is not None
            and isinstance(prior, OffsetWindowExpr)
            and prior.kind == "prior_period"
            and prior.input == now
            and expr
            == ArithmeticExpr(
                "divide", ArithmeticExpr("subtract", now, prior), prior, "null_if_zero"
            )
        ):
            offset = {"unit": prior.unit, "value": prior.value}
            return _Saved("growth", (now.measure,), now.aggregation, params={"offset": offset})
        return None
    inner = expr.input if isinstance(expr, OffsetWindowExpr) else expr
    if not isinstance(inner, AggregateExpr) or plain(replace(inner, filter={})) is None:
        return None
    saved = _Saved(kind, (inner.measure,), inner.aggregation)
    if kind == "aggregate" and expr is inner:
        if not inner.filter:
            return saved
        clause = _single_value_filter(inner.filter)
        if clause is None:
            return None
        field_id = resolve_filter_dimension(clause["field"], config)
        return replace(saved, recipe="filtered", row_filter={**clause, "field": field_id})
    if inner.filter or not isinstance(expr, OffsetWindowExpr) or expr.kind != kind:
        return None
    params: dict[str, dict[str, Any]] = {
        "cumulative": {},
        "rolling": {"window": {"unit": expr.unit, "value": expr.value}},
        "prior_period": {"offset": {"unit": expr.unit, "value": expr.value}},
        "period_to_date": {"period": expr.period},
    }
    return replace(saved, params=params[kind]) if kind in params else None


def _operand_id(node: Any) -> str:
    """A ratio operand the prompts can write: a metric, or a measure at its default aggregation."""

    if isinstance(node, MetricRecipeRefExpr):
        return node.metric_recipe
    if isinstance(node, AggregateExpr) and node == AggregateExpr(node.measure):
        return node.measure
    return ""


def _single_value_filter(predicate: dict[str, Any]) -> dict[str, Any] | None:
    """The one `field IN/NOT IN [values]` clause this wizard writes, if that is all there is."""

    clauses = predicate.get("all")
    clause = clauses[0] if isinstance(clauses, list) and len(clauses) == 1 else None
    if (
        isinstance(clause, dict)
        and set(clause) == {"field", "op", "value"}
        and isinstance(clause["field"], str)
        and clause["op"] in {"IN", "NOT IN"}
        and isinstance(clause["value"], list)
        and clause["value"]
        and all(type(value) in {str, int, float, bool} for value in clause["value"])
    ):
        return clause
    return None


def _time_recipe(
    recipe: str,
    measure_id: str,
    saved: dict[str, Any],
    name: str,
    aggregation: str,
    units: list[tuple[str, str]],
) -> tuple[dict[str, Any], str]:
    """Prompt for one recipe over time; returns its fields and an example question."""

    if recipe == "cumulative":
        return {"measure": measure_id}, f"What is {name} by month?"
    if recipe == "rolling":
        window = dict(saved.get("window", {}))
        unit = _unit_choice("Window unit", units, window.get("unit"), "day")
        length = _author_count(
            "Window length", default=int(window.get("value") or {"day": 7, "week": 4}.get(unit, 3))
        )
        return {"measure": measure_id, "window": {"unit": unit, "value": length}}, (
            f"What is {name} by {unit}?"
        )
    if recipe == "period_to_date":
        periods = [
            ("month", "Month to date"),
            ("quarter", "Quarter to date"),
            ("year", "Year to date"),
        ]
        period = _saved_choice("Period", periods, saved.get("period"), "month")
        return {"measure": measure_id, "period": period}, f"What is {name} by day?"
    offset = dict(saved.get("offset", {}))
    unit = _unit_choice("Compare with how far back", units, offset.get("unit"), "month")
    step = {
        "unit": unit,
        "value": _author_count(f"How many {unit}s back", default=int(offset.get("value") or 1)),
    }
    if recipe == "prior_period":
        return {"measure": measure_id, "offset": step}, f"What was {name} by {unit}?"
    now = {"kind": "aggregate", "measure": measure_id, "aggregation": aggregation}
    then = {"kind": "prior_period", "input": dict(now), "offset": step}
    growth = {
        "kind": "binary",
        "op": "divide",
        "null_behavior": "null_if_zero",
        "left": {"kind": "binary", "op": "subtract", "left": now, "right": then},
        "right": dict(then),
    }
    return {"expression": growth}, f"What is {name} by {unit}?"


def _calendar_units(config: PackageConfig) -> list[tuple[str, str]]:
    """The units whose periods the package calendar can fill.

    Like the engine's time.fill, this reads the default calendar, else the first one.
    """

    calendars = [row for row in config.entities if row.kind == "time"]
    calendar = next((row for row in calendars if (row.calendar_id or "default") == "default"), None)
    calendar = calendar or next(iter(calendars), None)
    columns = {row.column for row in config.dimensions if calendar and row.entity == calendar.id}
    return [(unit, text) for unit, text in _UNITS if _CALENDAR_COLUMNS[unit] in columns]


def _unit_choice(label: str, units: list[tuple[str, str]], saved: Any, fallback: str) -> str:
    """A unit for a calendar recipe, naming the calendar columns the others need.

    A saved unit stays on offer even when the calendar cannot fill it, so Enter keeps it.
    """

    missing = [f"`{_CALENDAR_COLUMNS[unit]}`" for unit, _ in _UNITS if unit not in dict(units)]
    if missing:
        print(
            f"  More units need these columns on the calendar model: {', '.join(missing)}. "
            "Add them with `author calendar`."
        )
    offered = [(unit, text) for unit, text in _UNITS if unit in dict(units) or unit == saved]
    return _saved_choice(
        label, offered, saved, fallback if fallback in dict(units) else units[0][0]
    )


def _saved_choice(label: str, options: list[tuple[str, str]], saved: Any, fallback: str) -> str:
    """Offer the saved value by default; never substitute one the prompt cannot offer."""

    default = str(saved or fallback)
    if default not in dict(options):
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{label}: {default!r} is not supported by this wizard; "
            "the authored metric was left unchanged.",
        )
    return _author_choice(label, options, default=default)


def _kept_choice(label: str, options: list[tuple[str, str]], saved: Any, fallback: str) -> str:
    """Preselect the saved value so Enter keeps it, offering it when the menu lacks it.

    A saved value that differs from an option only in case selects that option.
    """

    saved = str(saved or fallback).strip()
    listed = next((value for value, _ in options if value.casefold() == saved.casefold()), "")
    if not listed:
        options = [*options, (saved, f"Keep {saved} (the saved value)")]
    return _author_choice(label, options, default=listed or saved)


def _warn_on_new_aggregation(
    config: PackageConfig, measure_id: str, aggregation: str, *, authored: set[str]
) -> None:
    """Warn when an edit changes a measure's default aggregation, naming the metrics it changes.

    A loaded metric that isn't ``authored`` was published from a measure by the loader,
    which spells out the measure's default aggregation.
    """

    measure = next((row for row in config.measures if row.id == measure_id), None)
    if measure is None or measure.default_aggregation == aggregation.lower():
        return
    changed: set[str] = set()

    def uses(node: Any, published: bool) -> bool:
        """The node aggregates the measure by its default, or reads a metric that does."""
        if isinstance(node, MetricRecipeRefExpr):
            return node.metric_recipe in changed
        if getattr(node, "measure", None) == measure_id:
            return published or not getattr(node, "aggregation", "")
        if isinstance(node, list | tuple):
            return any(uses(item, published) for item in node)
        return is_dataclass(node) and any(
            uses(getattr(node, part.name), published) for part in fields(node)
        )

    rows = config.metric_recipes
    while more := {
        row.id
        for row in rows
        if row.id not in changed and uses(row.expression, row.id not in authored)
    }:
        changed |= more
    print(
        f"[warning] This changes the default aggregation from "
        f"{measure.default_aggregation} to {aggregation}."
    )
    if changed:
        print("  These metrics will return different numbers: " + ", ".join(sorted(changed)))


def _row_filter(
    config: PackageConfig,
    inventory: dict[str, Any],
    measure: dict[str, Any],
    *,
    saved: dict[str, Any] | None,
    require_dimension: bool,
) -> list[dict[str, Any]]:
    """The rows a filtered metric counts, such as orders whose status is completed.

    The filter uses a dimension of the measure's own model. Values come from the
    dimension's declared domain when it has one, else they are typed in. The
    saved operator and values are offered only while the dimension stays the same.
    """

    model = str(measure.get("model_key", "") or "")
    dimensions = [
        row
        for row in _inventory_items(inventory, "dimension")
        if model and str(row.get("model_key", "")) == model
    ]
    if not dimensions:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"`{model or measure.get('key')}` has no dimension to filter by. "
            "Add one with `author dimension`, then try again.",
        )
    dimension = _select_inventory_item(
        "Filter by",
        dimensions,
        default_key=str(saved["field"]) if saved else None if require_dimension else "",
    )
    field_id = _row_id(dimension)
    loaded = next((item for item in config.dimensions if item.id == field_id), None)
    if loaded is None:
        raise SemanticLayerError("INVALID_CONFIG", f"Filter dimension {field_id!r} is not loaded")
    previous = list(saved["value"]) if saved and field_id == saved["field"] else []
    op = _author_choice(
        "Keep the rows where it",
        [("IN", "is one of"), ("NOT IN", "is not one of")],
        default=str(saved["op"]) if saved and previous else "IN",
    )
    domain = [
        (value.value, value.label)
        for item in config.value_domains
        if item.id == loaded.value_domain
        for value in item.values
    ] or ([(True, "true"), (False, "false")] if loaded.data_type == "boolean" else [])

    def same(left: Any, right: Any) -> bool:
        return type(left) is type(right) and left == right

    while True:
        if domain:
            choices = list(domain)
            choices += [
                (old, f"Existing value {old!r}")
                for old in previous
                if not any(same(value, old) for value, _ in choices)
            ]
            options = [
                (str(index), text if text == _shown(value) else f"{text} ({_shown(value)})")
                for index, (value, text) in enumerate(choices)
            ]
            defaults = [
                str(next(index for index, (value, _) in enumerate(choices) if same(value, old)))
                for old in previous
            ]
            picked = _author_multi_choice("Values", options, defaults=defaults)
            values = (
                previous
                if previous and set(picked) == set(defaults)
                else [choices[int(index)][0] for index in picked]
            )
        else:
            default_text = ", ".join(_shown(value) for value in previous)
            raw = _author_prompt("Values, comma separated", default_text)
            try:
                values = (
                    previous
                    if previous and raw == default_text
                    else [
                        _filter_value(part.strip(), loaded.data_type)
                        for part in raw.split(",")
                        if part.strip()
                    ]
                )
            except SemanticLayerError as exc:
                print(f"{exc}. Enter other values, or type cancel.")
                continue
        if values:
            return [{"field": field_id, "op": op, "value": values}]
        print("Choose at least one value, or type cancel.")


def _shown(value: Any) -> str:
    """A filter value as the person types it: booleans as true/false, as in YAML."""

    return str(value).lower() if isinstance(value, bool) else str(value)


def _filter_value(value: str, data_type: str) -> Any:
    """Parse free-text filters only when the loaded dimension type calls for it."""

    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]  # `'true'` means the text true, not a value with quotes in it
    if data_type == "boolean":
        if value.lower() not in {"true", "false"}:
            raise SemanticLayerError(
                "INVALID_CONFIG", "Boolean filter values must be true or false"
            )
        return value.lower() == "true"
    if data_type in {"integer", "number"}:
        try:
            return int(value) if data_type == "integer" else float(value)
        except ValueError as exc:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"{data_type.title()} filter value {value!r} is not numeric"
            ) from exc
    return value


def _author_count(label: str, *, default: int) -> int:
    while True:
        raw = _author_prompt(label, str(default))
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        print("Enter a whole number greater than 0.")


def _row_id(row: dict[str, Any]) -> str:
    return str(row.get("id") or row.get("key", ""))


def _loaded_input(
    config: PackageConfig, row: dict[str, Any]
) -> MeasureConfig | MetricConfig | None:
    """The loader's measure or metric for an inventory row."""

    items: list[Any] = config.metric_recipes if row.get("kind") == "metric" else config.measures
    return next((item for item in items if item.id == _row_id(row)), None)


def _time_roles_of(config: PackageConfig, row: dict[str, Any]) -> list[str]:
    """An input's compatible clocks, default first; a model's other clocks may not time it."""

    loaded = _loaded_input(config, row)
    if isinstance(loaded, MetricConfig):
        return [loaded.temporal_role] if loaded.temporal_role else []
    if loaded is None:
        return []
    default = loaded.default_temporal_role
    return sorted(loaded.compatible_temporal_roles, key=lambda role: role != default)


def _suggested_denominator(
    config: PackageConfig, numerator: dict[str, Any], options: list[dict[str, Any]]
) -> dict[str, Any]:
    """Prefer a count on the numerator's own model, then anything on that model or clock."""

    model = str(numerator.get("model_key", "") or "")
    clocks = set(_time_roles_of(config, numerator))
    related = [
        row
        for row in options
        if (model and str(row.get("model_key", "")) == model)
        or (clocks & set(_time_roles_of(config, row)))
    ]
    counts = [
        row for row in related if getattr(_loaded_input(config, row), "value_type", "") == "count"
    ]
    return (counts or related or options)[0]


def _metric_time_role(
    config: PackageConfig,
    inventory: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    current: str = "",
) -> str:
    """Choose among the inputs' compatible clocks, preferring the first input's default."""

    candidates: list[str] = []
    for row in inputs:
        for role in _time_roles_of(config, row):
            if role and role not in candidates:
                candidates.append(role)
    if len(candidates) <= 1:
        return candidates[0] if candidates else ""
    labels = {
        _row_id(time): f"{time.get('label') or time.get('key')} ({time.get('model_key')})"
        for time in _inventory_items(inventory, "time")
    }
    return _author_choice(
        "Time axis for this metric",
        [(role, labels.get(role, role)) for role in candidates],
        default=current if current in candidates else candidates[0],
    )


def _author_segment(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    config = load_package_config(ref.source_path)
    # The loader requires a basis metric rooted on the segment's entity; offer only those.
    roots: dict[str, str] = {}
    for recipe in config.metric_recipes:
        with contextlib.suppress(SemanticLayerError):
            roots[recipe.id] = _metric_root_entity(config, recipe.expression)
    rooted = {entity.id for entity in config.entities if entity.allowed_as_root}
    entities = [
        row
        for row in _inventory_items(inventory, "entity")
        if _row_id(row) in rooted and _row_id(row) in roots.values()
    ]
    dimensions = _inventory_items(inventory, "dimension")
    if not entities or not dimensions:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Segments need an entity, one of its dimensions, and a metric over its rows. "
            "Add those abstractions first.",
        )
    key, label, existing = _author_identity(project, inventory, "segment", "active_customers")
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    entity = _select_inventory_item(
        "Entity whose members this segment contains",
        entities,
        default_key=str(current.get("entity", "")),
    )
    entity_model = str((entity.get("spec", {}) or {}).get("model", ""))
    entity_dimensions = [row for row in dimensions if str(row.get("parent", "")) == entity_model]
    if not entity_dimensions:
        entity_dimensions = dimensions
    current_preview = list(current.get("preview_dimensions", []) or [])
    dimension = _select_inventory_item(
        "Membership dimension",
        entity_dimensions,
        default_key=str(current_preview[0]) if current_preview else "",
    )
    basis = _select_inventory_item(
        "Metric used when previewing segment size",
        [
            row
            for row in _inventory_items(inventory, "metric")
            if roots.get(_row_id(row)) == _row_id(entity)
        ],
        default_key=str(current.get("basis_metric", "")),
    )
    loaded = next((item for item in config.dimensions if item.id == _row_id(dimension)), None)
    data_type = loaded.data_type if loaded else "string"
    comparison_options = [("=", "Equals"), ("!=", "Does not equal")]
    if data_type in {"integer", "number", "date", "timestamp"}:
        comparison_options.extend(
            [(">=", "At least"), (">", "Greater than"), ("<=", "At most"), ("<", "Less than")]
        )
    current_where = list((current.get("membership", {}) or {}).get("where", []) or [])
    current_filter = dict(current_where[0] or {}) if current_where else {}
    current_op = str(current_filter.get("op", "="))
    op = _author_choice(
        "Membership comparison",
        comparison_options,
        default=current_op if current_op in {value for value, _ in comparison_options} else "=",
    )
    domain = list((dimension.get("spec", {}) or {}).get("domain", []) or [])
    domain_default: Any = ""
    if domain:
        first_domain = domain[0]
        domain_default = (
            first_domain.get("value", "") if isinstance(first_domain, dict) else first_domain
        )
    value_default = str(current_filter.get("value", domain_default))
    if data_type == "boolean":
        value: Any = (
            _author_choice(
                "Comparison value",
                [("true", "true"), ("false", "false")],
                default="false" if value_default.lower() == "false" else "true",
            )
            == "true"
        )
    else:
        while True:
            raw = _author_prompt(
                "Comparison value (use a value from the authored domain when available)",
                value_default,
            )
            if "value" in current_filter and raw == value_default:
                value = current_filter["value"]  # Enter keeps it as saved
                break
            try:
                value = _filter_value(raw, data_type)
            except SemanticLayerError as exc:
                print(f"{exc}. Enter another value, or type cancel.")
                continue
            if value != "":
                break
            print("Enter a value, or type cancel.")
    description = _author_prompt(
        "Description",
        str(
            current.get(
                "description",
                f"{label} where {dimension.get('label') or dimension.get('key')} {op} "
                f"{_shown(value)}.",
            )
        ),
    )
    namespace = _authoring_namespace(project)
    entity_ref = str(entity.get("id") or entity.get("key", ""))
    dimension_ref = str(dimension.get("id") or dimension.get("key", ""))
    basis_ref = str(basis.get("id") or basis.get("key", ""))
    segment_id = str(current.get("id") or "")
    if not segment_id:
        segment_id = f"segment.{key}" if "." in key else f"segment.{namespace}.{key}"
    spec = {
        **current,
        "id": segment_id,
        "label": label,
        "description": description,
        "entity": entity_ref,
        "basis_metric": basis_ref,
        "preview_dimensions": [dimension_ref],
        "membership": {"where": [{"field": dimension_ref, "op": op, "value": value}]},
    }
    target = str(existing.get("relative_path", "")) if existing else "segments/core.yml"
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="segment",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview={"segments": {key: spec}},
        apply=lambda: project.upsert_segment(segment_key=key, spec=spec, file_name="core.yml"),
        next_action=f"Inspect it with `ls segment {key}`.",
    )


def _upsert_nested_model(
    project: ArchitectProject, model: dict[str, Any], **updates: Any
) -> ArchitectMutation:
    model_key = str(model.get("key", ""))
    spec = dict(model.get("spec", {}) or {})
    entity_defaults = _model_entity_defaults(project, model_key)
    return project.upsert_model(
        model_id=model_key,
        entity_key=str(entity_defaults["key"]),
        relation=str(spec.get("relation", model_key)),
        primary_key=list(entity_defaults["primary_key"] or [f"{entity_defaults['key']}_id"]),
        description=str(spec.get("description", "")),
        **updates,
    )


def _apply_authoring_change(
    project: ArchitectProject,
    ref: PackageReference,
    before_warnings: set[str],
    *,
    kind: str,
    key: str,
    label: str,
    existing: dict[str, Any] | None,
    target: str,
    preview: dict[str, Any],
    apply: Any,
    next_action: str,
    on_commit: Callable[[ArchitectMutation, str], None] | None = None,
) -> ArchitectMutation:
    operation = "update" if existing else "create"
    print()
    print(f"Preview - {operation} {kind} `{key}`")
    print(f"  label   {label}")
    print(f"  file    {target}")
    current_backend().show_yaml(preview)
    if not _author_confirm(f"{operation.title()} this {kind}?", default=False):
        raise _AuthoringCancelled

    mutation = apply()
    report = mutation.report
    if not report.get("ok"):
        status = str(report.get("status", "failed"))
        print(f"[error] Authoring {status}; original files were restored.")
        for message in _authoring_error_messages(report)[:5]:
            print(f"  - {message}")
        raise _AuthoringCancelled

    # Record a successful commit before any output can be interrupted. The
    # metric wizard then restores every committed part on cancellation.
    if on_commit is not None:
        on_commit(mutation, f"{kind} `{key}`")

    after_warnings = set(_authoring_warning_messages(report.get("parse", {})))
    new_warnings = sorted(after_warnings - before_warnings)
    unchanged = len(after_warnings & before_warnings)
    completed_operation = f"{operation}d"
    print(f"[ok] {kind.title()} {completed_operation} and parse-validated.")
    print(f"  changed {', '.join(report.get('changed_files', []) or [target])}")
    if new_warnings:
        print(f"[warning] {len(new_warnings)} new authoring warning(s):")
        for warning in new_warnings[:5]:
            print(f"  - {warning}")
    if unchanged:
        print(
            f"  {unchanged} existing warning(s) unchanged; run `validate` to inspect package health."
        )
    print(f"  next    {next_action}")
    print("  undo    Type `undo` to restore the previous files in this REPL session.")
    return mutation


def _author_identity(
    project: ArchitectProject,
    inventory: dict[str, Any],
    kind: str,
    default_key: str,
    *,
    parent: str = "",
) -> tuple[str, str, dict[str, Any] | None]:
    """Ask for a key and label, again when the person declines to update an existing
    object or wants wording less like another definition's."""

    while True:
        answer = _author_key_and_label(project, inventory, kind, default_key, parent)
        if answer is None:
            print(f"Enter another {kind} key, or type cancel.")
            continue
        key, label, existing = answer
        # An update that keeps its label cannot become ambiguous; a new label can.
        same_label = existing is not None and label == str(existing.get("label", ""))
        if same_label or _distinct(project, kind, key, label):
            return answer
        default_key = key


def _author_key_and_label(
    project: ArchitectProject, inventory: dict[str, Any], kind: str, default_key: str, parent: str
) -> tuple[str, str, dict[str, Any] | None] | None:
    """One key and label; None when the key is an existing object the person won't update."""

    raw = _author_prompt(f"{kind.title()} key", default_key)
    key = (
        ".".join(_slug(part, fallback="item") for part in raw.split("."))
        if kind in {"metric", "segment"} and "." in raw
        else _slug(raw, fallback=default_key)
    )
    if key != raw:
        print(f"  normalized `{raw}` -> `{key}` (YAML-safe key)")
    candidates = _inventory_items(inventory, kind)
    if kind == "measure" and parent:
        other_parent = next(
            (
                row
                for row in candidates
                if str(row.get("key", "")) == key and str(row.get("parent", "")) != parent
            ),
            None,
        )
        if other_parent:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                (
                    f"Measure `{key}` already exists on model `{other_parent.get('parent')}`. "
                    "Measure IDs are package-wide; choose a different key or manage that model."
                ),
            )
    existing = next(
        (
            row
            for row in candidates
            if str(row.get("key", "")) == key
            and (not parent or str(row.get("parent", "")) == parent)
        ),
        None,
    )
    if existing:
        print(
            f"[warning] `{key}` already exists in {existing.get('relative_path', 'the package')} "
            f"as {existing.get('label') or key}."
        )
        if not _author_confirm(f"Manage and update this existing {kind}?", default=False):
            return None
    default_label = str(existing.get("label", "")) if existing else _title(key)
    label = _author_prompt("Business label", default_label)
    return key, label, existing


def _distinct(project: ArchitectProject, kind: str, key: str, label: str) -> bool:
    """Show definitions that sound similar; False when the person wants other wording."""

    similar = project.find_similar(kind=kind, key=key, label=label)
    if not similar:
        return True
    print("\n[warning] This sounds similar to existing definitions:")
    for row in similar[:3]:
        reason = str(row.get("reason", "similar wording"))
        print(
            f"  - {row.get('kind', kind)} {row.get('id') or row.get('key')} "
            f"- {row.get('label', '')} ({reason})"
        )
    decision = _author_choice(
        "How should we proceed?",
        [
            ("restart", "Choose a different key and label"),
            ("continue", "Create it as a deliberately distinct definition"),
            ("cancel", "Cancel without writing"),
        ],
        default="restart",
    )
    if decision == "cancel":
        raise _AuthoringCancelled
    return decision == "continue"


def _select_model(inventory: dict[str, Any]) -> dict[str, Any]:
    models = _inventory_items(inventory, "model")
    if not models:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Create a model/entity first with `author model`.",
        )
    calendars = {
        str((row.get("spec") or {}).get("model", ""))
        for row in _inventory_items(inventory, "entity")
        if (row.get("spec") or {}).get("kind") == "time"
    }
    # A calendar is a date spine; recommend a model that holds facts first.
    models.sort(key=lambda row: str(row.get("key", "")) in calendars)
    return _select_inventory_item("Model to extend", models)


_CREATE = "new"


def _select_inventory_item(
    label: str,
    rows: list[dict[str, Any]],
    *,
    default_key: str | None = "",
    create: tuple[str, Callable[[], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Pick one inventory row. ``create`` adds a last option that makes a new one instead.

    ``default_key`` matches a row's canonical ID before any row's key, so a saved
    ID wins over another row whose key spells it. ``None`` offers no default.
    """

    if not rows and create is None:
        raise SemanticLayerError("INVALID_CONFIG", f"No choices are available for {label.lower()}")
    visible = list(rows)
    preferred = next((row for row in visible if default_key and row.get("id") == default_key), None)
    preferred = preferred or next(
        (row for row in visible if default_key and row.get("key") == default_key), None
    )
    # Arrow-key pickers filter as you type; plain prompts ask for search words first.
    if len(visible) > 12 and not current_backend().filters_long_lists:
        while True:
            search = _author_prompt(
                f"Filter {label.lower()} ({len(visible)} choices; type words, or Enter for a short list)",
                "",
            ).lower()
            if not search:
                visible = ([preferred] if preferred else []) + [
                    row for row in visible if row is not preferred
                ][: 12 - bool(preferred)]
                print("Showing a short list. Type search words at the filter prompt to narrow it.")
                break
            matched = [
                row
                for row in visible
                if search
                in " ".join(
                    str(row.get(field, ""))
                    for field in ("kind", "key", "id", "label", "description", "parent")
                ).lower()
            ]
            if matched:
                # Truncation keeps a matching saved choice selectable.
                keep = [row for row in matched[12:] if row is preferred]
                visible = [*keep, *matched[: 12 - len(keep)]]
                print(f"Found {len(matched)} matching choice(s).")
                break
            print("No matching objects. Try a key, label, ID, or model name.")
    default_index = (
        0
        if default_key == ""
        else next((index for index, row in enumerate(visible) if row is preferred), None)
    )
    options: list[tuple[str, str]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(visible, start=1):
        value = str(index)
        key = str(row.get("key") or row.get("id") or value)
        kind = str(row.get("kind", "object"))
        description = f"[{kind}] {row.get('label') or key}"
        if row.get("parent"):
            description += f" - {row['parent']}"
        options.append((value, f"{key} - {description}"))
        lookup[value] = row
    if create is not None:
        options.append((_CREATE, create[0]))
    selected = _author_choice(
        label,
        options,
        default=str(default_index + 1) if default_index is not None and visible else "",
    )
    if selected == _CREATE and create is not None:
        return create[1]()
    if selected not in lookup:
        raise SemanticLayerError("INVALID_CONFIG", f"Choose {label.lower()} before continuing")
    return lookup[selected]


def _inventory_items(inventory: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    key = {"entity": "entities", "time": "times"}.get(kind, f"{kind}s")
    return [dict(row) for row in list(inventory.get(key, []) or [])]


def _model_entity_defaults(project: ArchitectProject, model_id: str) -> dict[str, Any]:
    graph_path = Path(project.project_path) / "graph.yml"
    raw = (
        dict(yaml.safe_load(graph_path.read_text(encoding="utf-8")) or {})
        if graph_path.is_file()
        else {}
    )
    entities = dict((raw.get("graph", {}) or {}).get("entities", {}) or {})
    for key, spec_raw in entities.items():
        spec = dict(spec_raw or {})
        if str(spec.get("model", "")) == model_id:
            primary = spec.get("key", [])
            keys = [str(item) for item in primary] if isinstance(primary, list) else [str(primary)]
            return {"key": str(key), "primary_key": [item for item in keys if item]}
    return {"key": _singular(model_id), "primary_key": []}


def _singular(word: str) -> str:
    """`orders` -> `order`, `categories` -> `category`, `addresses` -> `address`; `status` stays."""

    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")) and len(word) > 1:
        return word[:-1]
    return word


def _authoring_namespace(project: ArchitectProject) -> str:
    package_path = Path(project.project_path) / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    package = dict(raw.get("package", {}) or {})
    return _slug(
        str(package.get("namespace") or package.get("id") or Path(project.project_path).name)
    )


def _package_block(ref: PackageReference) -> dict[str, Any]:
    """The ``package:`` block of package.yml, or {} when it can't be read."""
    source = Path(ref.source_path)
    package_path = source / "package.yml" if source.is_dir() else source
    try:
        raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    except (OSError, yaml.YAMLError):
        return {}
    return dict(raw.get("package", {}) or {})


def _authoring_warehouse(ref: PackageReference) -> str:
    return str(_package_block(ref).get("warehouse", "configured") or "configured")
