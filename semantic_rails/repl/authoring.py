"""Guided authoring wizards for models, dimensions, times, measures,
metrics and segments.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from ..architect_service import ArchitectMutation, ArchitectProject
from ..cli.common import _quote, _ref_label, _slug, _title
from ..cli.output import _authoring_error_messages, _authoring_warning_messages
from ..cli.reports import project_validation_report
from ..config_validation import PackageReference
from ..errors import SemanticLayerError
from .prompts import (
    _author_choice,
    _author_confirm,
    _author_prompt,
    _author_slug_prompt,
    _AuthoringCancelled,
)

_AUTHORING_KINDS = ("model", "dimension", "time", "measure", "metric", "segment")


_AUTHORING_ALIASES = {
    "entity": "model",
    "model/entity": "model",
    "time_dimension": "time",
    "times": "time",
    "dimensions": "dimension",
    "measures": "measure",
    "metrics": "metric",
    "segments": "segment",
}


def _run_authoring_flow(
    current_ref: PackageReference,
    *,
    requested_kind: str = "",
) -> ArchitectMutation | None:
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
        print("Type `cancel` at any prompt to return without writing files.")

        before_warnings = set(_authoring_warning_messages(initial))
        dispatch = {
            "model": _author_model,
            "dimension": _author_dimension,
            "time": _author_time,
            "measure": _author_measure,
            "metric": _author_metric,
            "segment": _author_segment,
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
                "Usage: author [model|dimension|time|measure|metric|segment]",
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
    ]
    return _author_choice("What do you want to create or update?", options, default=recommended)


def _author_model(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    key, label, existing = _author_identity(project, inventory, "model", "orders")
    spec = dict(existing.get("spec", {}) or {}) if existing else {}
    existing_entity = _model_entity_defaults(project, key)
    relation = _author_prompt(
        "Warehouse table or relation (for example raw_orders)",
        str(spec.get("relation", key)),
    )
    entity_key = _author_slug_prompt(
        "Business entity at one row of this model",
        str(existing_entity.get("key", key.rstrip("s") or key)),
    )
    entity_conflict = next(
        (
            row
            for row in _inventory_items(inventory, "entity")
            if str(row.get("key", "")) == entity_key
            and str((row.get("spec", {}) or {}).get("model", "")) != key
        ),
        None,
    )
    if entity_conflict:
        owner = str((entity_conflict.get("spec", {}) or {}).get("model", ""))
        raise SemanticLayerError(
            "INVALID_CONFIG",
            (
                f"Entity `{entity_key}` already belongs to model `{owner}`. "
                "Choose a different entity key or manage its existing model."
            ),
        )
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
    kind = _author_choice(
        "How should values behave?",
        [
            ("categorical", "Categorical - labels such as placed, shipped, cancelled"),
            ("boolean", "Boolean - true/false"),
            ("integer", "Integer - whole numbers that can be grouped or filtered"),
            ("continuous", "Continuous - numeric values used as dimensions"),
        ],
        default=str(current.get("kind", "categorical")),
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
    kind = _author_choice(
        "Column type",
        [("timestamp", "Timestamp - date and time"), ("date", "Date - calendar date only")],
        default=str(current.get("kind", "timestamp")),
    )
    clock_class = _author_choice(
        "What does this clock mean?",
        [
            ("event_time", "Event time - when an event happened"),
            ("state_time", "State time - when a snapshot was observed"),
            ("as_of_time", "As-of time - effective point for temporal joins"),
            ("calendar_time", "Calendar time - a date-spine or calendar model"),
        ],
        default=str(current.get("class", "event_time")),
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
    model = _select_model(inventory)
    key, label, existing = _author_identity(
        project, inventory, "measure", "revenue", parent=str(model.get("key", ""))
    )
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    measure_kind = _author_choice(
        "What primitive fact is this?",
        [
            ("aggregate", "Aggregate - sum, average, minimum, or maximum"),
            ("entity_count", "Entity count - distinct count of business keys"),
        ],
        default=str(current.get("kind", "aggregate")),
    )
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
            "accumulation": {"kind": "event"},
            "value_type": "count",
            "meta": {
                "owner_team": "analytics",
                "review_priority": "medium",
                "change_risk": "medium",
                **dict(current.get("meta", {}) or {}),
            },
        }
        if current.get("kind") != "entity_count":
            for stale_key in ("expr", "default_agg", "currency", "rollup"):
                measure.pop(stale_key, None)
    else:
        expression = _author_prompt(
            "Column or scalar expression (for example amount_cents / 100.0)",
            str(current.get("expr", key)),
        )
        aggregation = _author_choice(
            "Default aggregation",
            [(value, value.replace("_", " ").title()) for value in ("sum", "avg", "min", "max")],
            default=str(current.get("default_agg", "sum")),
        )
        value_type = _author_choice(
            "Result type",
            [
                ("number", "Number"),
                ("currency", "Currency"),
                ("percent", "Percent"),
                ("count", "Count"),
            ],
            default=str(current.get("value_type", "number")),
        )
        measure = {
            **current,
            "label": label,
            "description": description,
            "kind": "aggregate",
            "expr": expression,
            "default_agg": aggregation,
            "accumulation": {"kind": "flow"},
            "value_type": value_type,
            "meta": {
                "owner_team": "analytics",
                "review_priority": "medium",
                "change_risk": "medium",
                **dict(current.get("meta", {}) or {}),
            },
        }
        if current.get("kind") != "aggregate":
            measure.pop("entity_key", None)
        if value_type == "currency":
            measure["currency"] = _author_prompt(
                "Currency code", str(current.get("currency", "USD"))
            ).upper()
    return _apply_authoring_change(
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
    )


def _author_metric(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    measures = _inventory_items(inventory, "measure")
    metrics = _inventory_items(inventory, "metric")
    if not measures:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Add a primitive measure first with `author measure`, then create a governed metric.",
        )
    published_measures = {str((row.get("spec", {}) or {}).get("measure", "")) for row in metrics}
    recommended_measure = next(
        (row for row in measures if str(row.get("key", "")) not in published_measures),
        measures[0],
    )
    recommended_key = str(recommended_measure.get("key", "metric"))
    key, label, existing = _author_identity(project, inventory, "metric", recommended_key)
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    metric_kind = _author_choice(
        "Metric recipe",
        [
            ("aggregate", "Aggregate - publish one measure as a stable KPI"),
            ("ratio", "Ratio - divide one measure/metric by another"),
        ],
        default=str(current.get("kind", "aggregate"))
        if current.get("kind") in {"aggregate", "ratio"}
        else "aggregate",
    )
    description = _author_prompt(
        "Business definition",
        str(current.get("description", f"Governed definition of {label.lower()}.")),
    )
    matching_measure = next(
        (row for row in measures if str(row.get("key", "")) == key),
        None,
    )
    value_default = str(
        current.get("value_type")
        or ((matching_measure or {}).get("spec", {}) or {}).get("value_type")
        or "number"
    )
    value_options = [
        ("number", "Number"),
        ("currency", "Currency"),
        ("percent", "Percent"),
        ("count", "Count"),
    ]
    if metric_kind == "ratio":
        value_options = [
            ("percent", "Percent or share"),
            ("ratio", "Dimensionless ratio"),
            ("currency", "Currency per unit"),
        ]
        if value_default not in {value for value, _ in value_options}:
            value_default = "percent"
    value_type = _author_choice(
        "Result type",
        value_options,
        default=value_default
        if value_default in {value for value, _ in value_options}
        else value_options[0][0],
    )
    namespace = _authoring_namespace(project)
    metric_id = str(current.get("as") or current.get("id") or "")
    if not metric_id:
        metric_id = f"metric.{key}" if "." in key else f"metric.{namespace}.{key}"
    spec: dict[str, Any] = {
        **current,
        "as": metric_id,
        "label": label,
        "description": description,
        "kind": metric_kind,
        "value_type": value_type,
        "meta": {
            "owner_team": "analytics",
            "review_priority": "medium",
            "change_risk": "medium",
            **dict(current.get("meta", {}) or {}),
        },
    }
    if metric_kind == "aggregate":
        for stale_key in ("numerator", "denominator", "null_behavior", "expression"):
            spec.pop(stale_key, None)
        source_default = str(current.get("measure", ""))
        if not source_default and matching_measure is not None:
            source_default = key
        selected = _select_inventory_item(
            "Measure to publish",
            measures,
            default_key=source_default or None,
        )
        spec["measure"] = str(selected.get("id") or selected.get("key", ""))
        example = f"What is {label.lower()} by month?"
    else:
        spec.pop("measure", None)
        spec.pop("aggregation", None)
        spec.pop("expression", None)
        operands: list[dict[str, Any]] = []
        seen_operand_ids: set[str] = set()
        for row in [*measures, *metrics]:
            operand_id = str(row.get("id") or row.get("key", ""))
            if (
                not operand_id
                or operand_id in seen_operand_ids
                or (row.get("kind") == "metric" and str(row.get("key", "")) == key)
            ):
                continue
            operands.append(row)
            seen_operand_ids.add(operand_id)
        numerator_default = str(current.get("numerator", ""))
        if not numerator_default and any(str(row.get("key", "")) == key for row in operands):
            numerator_default = key
        numerator = _select_inventory_item(
            "Numerator", operands, default_key=numerator_default or None
        )
        denominator_options = [
            row
            for row in operands
            if str(row.get("id") or row.get("key", ""))
            != str(numerator.get("id") or numerator.get("key", ""))
        ]
        if not denominator_options:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "A ratio needs two distinct measures or metrics. Add another primitive first.",
            )
        denominator_default = str(current.get("denominator", ""))
        if not any(
            denominator_default in {str(row.get("key", "")), str(row.get("id", ""))}
            for row in denominator_options
        ):
            denominator_default = str(
                denominator_options[0].get("id") or denominator_options[0].get("key", "")
            )
        denominator = _select_inventory_item(
            "Denominator", denominator_options, default_key=denominator_default
        )
        spec.update(
            {
                "numerator": str(numerator.get("id") or numerator.get("key", "")),
                "denominator": str(denominator.get("id") or denominator.get("key", "")),
                "null_behavior": "null_if_zero",
            }
        )
        example = f"How does {label.lower()} trend by month?"
    if value_type == "currency":
        spec["currency"] = _author_prompt(
            "Currency code", str(current.get("currency", "USD"))
        ).upper()
    temporal = _default_temporal_reference(inventory)
    if temporal:
        spec["temporal_role"] = temporal
    spec["examples"] = [example]
    target = (
        str(existing.get("relative_path", ""))
        if existing
        else f"metrics/core/{_slug(key, fallback='metric')}.yml"
    )
    return _apply_authoring_change(
        project,
        ref,
        before_warnings,
        kind="metric",
        key=key,
        label=label,
        existing=existing,
        target=target,
        preview={"metrics": {key: spec}},
        apply=lambda: project.upsert_metric(metric_key=key, spec=spec, group="core", replace=True),
        next_action=f"Try `ask {example}`.",
    )


def _author_segment(
    project: ArchitectProject,
    inventory: dict[str, Any],
    ref: PackageReference,
    before_warnings: set[str],
) -> ArchitectMutation:
    entities = _inventory_items(inventory, "entity")
    dimensions = _inventory_items(inventory, "dimension")
    metrics = _inventory_items(inventory, "metric")
    if not entities or not dimensions or not metrics:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Segments need an entity, a dimension, and a metric. Add those abstractions first.",
        )
    key, label, existing = _author_identity(project, inventory, "segment", "active_customers")
    current = dict(existing.get("spec", {}) or {}) if existing else {}
    entity = _select_inventory_item("Entity whose members this segment contains", entities)
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
    model_clock_ids = {
        str(row.get("id", ""))
        for row in _inventory_items(inventory, "time")
        if str(row.get("parent", "")) == entity_model
    }
    compatible_metrics = [
        row
        for row in metrics
        if not model_clock_ids
        or not str((row.get("spec", {}) or {}).get("temporal_role", ""))
        or str((row.get("spec", {}) or {}).get("temporal_role", "")) in model_clock_ids
    ]
    if not compatible_metrics:
        compatible_metrics = metrics
    basis = _select_inventory_item(
        "Metric used when previewing segment size",
        compatible_metrics,
        default_key=str(current.get("basis_metric", "")),
    )
    dimension_kind = str((dimension.get("spec", {}) or {}).get("kind", "categorical"))
    comparison_options = [("=", "Equals"), ("!=", "Does not equal")]
    if dimension_kind in {
        "integer",
        "continuous",
        "number",
        "percent",
        "currency",
        "date",
        "timestamp",
    }:
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
    domain_default: Any = "true" if dimension_kind == "boolean" else ""
    if domain:
        first_domain = domain[0]
        domain_default = (
            first_domain.get("value", "") if isinstance(first_domain, dict) else first_domain
        )
    value_default = current_filter.get("value", domain_default)
    value = _author_scalar(
        _author_prompt(
            "Comparison value (use a value from the authored domain when available)",
            str(value_default),
        )
    )
    description = _author_prompt(
        "Description",
        str(
            current.get(
                "description",
                f"{label} where {dimension.get('label') or dimension.get('key')} {op} {value}.",
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
        entity_key=str(entity_defaults.get("key", model_key.rstrip("s") or model_key)),
        relation=str(spec.get("relation", model_key)),
        primary_key=list(entity_defaults.get("primary_key", []) or [f"{model_key.rstrip('s')}_id"]),
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
) -> ArchitectMutation:
    operation = "update" if existing else "create"
    print()
    print(f"Preview - {operation} {kind} `{key}`")
    print(f"  label   {label}")
    print(f"  file    {target}")
    rendered = yaml.safe_dump(preview, sort_keys=False, allow_unicode=False).rstrip()
    for line in rendered.splitlines():
        print(f"  {line}")
    similar = project.find_similar(kind=kind, key=key, label=label)
    if similar:
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
                ("restart", "Cancel and restart with different wording"),
                ("continue", "Create it as a deliberately distinct definition"),
                ("cancel", "Cancel without writing"),
            ],
            default="restart",
        )
        if decision != "continue":
            raise _AuthoringCancelled
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
            raise _AuthoringCancelled
    default_label = str(existing.get("label", "")) if existing else _title(key)
    label = _author_prompt("Business label", default_label)
    return key, label, existing


def _select_model(inventory: dict[str, Any]) -> dict[str, Any]:
    models = _inventory_items(inventory, "model")
    if not models:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "Create a model/entity first with `author model`.",
        )
    return _select_inventory_item("Model to extend", models)


def _select_inventory_item(
    label: str,
    rows: list[dict[str, Any]],
    *,
    default_key: str | None = "",
) -> dict[str, Any]:
    if not rows:
        raise SemanticLayerError("INVALID_CONFIG", f"No choices are available for {label.lower()}")
    visible = list(rows)
    if len(visible) > 12:
        while True:
            search = _author_prompt(
                f"Filter {label.lower()} ({len(visible)} choices; type words, or Enter for a short list)",
                "",
            ).lower()
            if not search:
                preferred = next(
                    (
                        row
                        for row in visible
                        if default_key
                        and default_key in {str(row.get("key", "")), str(row.get("id", ""))}
                    ),
                    None,
                )
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
                visible = matched[:12]
                print(f"Found {len(matched)} matching choice(s).")
                break
            print("No matching objects. Try a key, label, ID, or model name.")
    default_index = (
        0
        if default_key == ""
        else next(
            (
                index
                for index, row in enumerate(visible)
                if default_key and default_key in {str(row.get("key", "")), str(row.get("id", ""))}
            ),
            None,
        )
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
    selected = _author_choice(
        label,
        options,
        default=str(default_index + 1) if default_index is not None else "",
    )
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
    return {"key": model_id.rstrip("s") or model_id, "primary_key": []}


def _authoring_namespace(project: ArchitectProject) -> str:
    package_path = Path(project.project_path) / "package.yml"
    raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    package = dict(raw.get("package", {}) or {})
    return _slug(
        str(package.get("namespace") or package.get("id") or Path(project.project_path).name)
    )


def _authoring_warehouse(ref: PackageReference) -> str:
    source = Path(ref.source_path)
    package_path = source / "package.yml" if source.is_dir() else source
    try:
        raw = dict(yaml.safe_load(package_path.read_text(encoding="utf-8")) or {})
    except (OSError, yaml.YAMLError):
        return "configured"
    return str((raw.get("package", {}) or {}).get("warehouse", "configured") or "configured")


def _default_temporal_reference(inventory: dict[str, Any]) -> str:
    times = _inventory_items(inventory, "time")
    if not times:
        return ""
    selected = next(
        (row for row in times if bool((row.get("spec", {}) or {}).get("default"))),
        times[0],
    )
    return str(selected.get("id") or selected.get("key") or "")


def _author_scalar(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value
