"""Parse captured authored package rows into the typed package configuration."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..dialects import dialect_for_warehouse
from ..errors import SemanticLayerError
from ..expressions import parse_config_expression, validate_expression_calls
from ..meta_contract import load_meta_contract
from ..operational import (
    load_operational_contract,
    merge_operational_payloads,
    normalize_operational_payload,
    validate_operational_payload,
)
from ..policy_rules import policy_action
from ..row_filters import validate_row_filters
from ..schema import (
    AggregateRelationConfig,
    DimensionConfig,
    EntityConfig,
    MeasureConfig,
    MetricConfig,
    PackageConfig,
    RelationshipConfig,
    SegmentConfig,
    SemanticPolicyConfig,
    TemporalRoleConfig,
    ValueDomainConfig,
    ValueDomainValue,
    ZeroThrough,
)
from .aggregate_rows import _ROLLUP_HOLDS, _resolve_variant_specs
from .loader_chain import _ensure_unique_object_ids, _parse_package_meta
from .lookup_measures import lookup_measure_spec, resolve_lookup_measures
from .measure_governance import with_published_flags
from .measure_rows import (
    _authored_additive,
    _default_topics,
    _derive_measure_semantics,
    _normalize_accumulation,
    _suggested_aggregations,
)
from .metric_rows import _parse_metric_expression, _translate_metric_direct_fields
from .model_rows import _key_columns_and_role, _map_dimension_kind, _parse_relations, _with_displays
from .policy_rows import (
    _parse_caveats,
    _parse_path_policy,
    _parse_path_preferences,
    _validate_caveat_refs,
    _validate_policy_scopes,
)
from .row_values import (
    _ensure_list,
    _merge_meta,
    _normalize_examples,
    _normalize_meta,
    _optional_int,
    _slug,
    _titleize,
    _unique_aliases,
)
from .temporal_rows import (
    _coarser_time_grains,
    _parse_external_discontinuities,
    _parse_validity_windows,
)
from .zero_through import FieldOf, field_reader, zero_through


def _parse_package(raw: dict[str, Any], *, path: str) -> PackageConfig:
    package_raw = dict(raw.get("package", {}) or {})
    namespace = str(package_raw.get("namespace", package_raw.get("id", "")) or "").strip()
    defaults = dict(raw.get("defaults", {}) or {})
    graph = dict(raw.get("graph", {}) or {})
    graph_entities = dict(graph.get("entities", {}) or {})
    model_rows = dict(raw.get("models", {}) or {})
    metrics_rows = dict(raw.get("metrics", {}) or {})
    segments_rows = dict(raw.get("segments", {}) or {})
    relations, relation_aliases = _parse_relations(raw, namespace=namespace, path=path)
    relations_by_id = {row.id: row for row in relations}

    if not graph_entities:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: graph.entities must not be empty")
    if not model_rows:
        raise SemanticLayerError("INVALID_CONFIG", f"{path}: models must not be empty")

    entities: list[EntityConfig] = []
    entity_lookup: dict[str, str] = {}
    model_to_entity: dict[str, str] = {}
    model_rows = {str(k): dict(v or {}) for k, v in model_rows.items()}
    time_calendar_ids_seen: set[str] = set()

    # Split model rows by kind. Fact models declare a single time_entity
    # join and never bind to a graph entity (they are not reachable via
    # inferred multi-hop joins).
    fact_models: dict[str, dict[str, Any]] = {}
    regular_model_rows: dict[str, dict[str, Any]] = {}
    for model_id, model in model_rows.items():
        model_kind = str(model.get("kind", "model") or "model").strip().lower()
        if model_kind == "fact":
            fact_models[model_id] = model
        elif model_kind == "model":
            regular_model_rows[model_id] = model
        else:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' has invalid kind '{model_kind}' "
                f"(allowed: model, fact)",
            )

    for entity_key, entity_spec_raw in graph_entities.items():
        entity_spec = dict(entity_spec_raw or {})
        entity_kind = str(entity_spec.get("kind", "regular") or "regular").strip().lower()
        if entity_kind not in {"regular", "time"}:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' has invalid kind '{entity_kind}' "
                f"(allowed: regular, time)",
            )
        model_id = str(entity_spec.get("model", "")).strip()
        if model_id not in model_rows:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' references unknown model '{model_id}'",
            )
        if model_id in fact_models:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: graph entity '{entity_key}' references fact model "
                f"'{model_id}' — fact models cannot be graph entities",
            )
        model = model_rows[model_id]
        model_keys = dict(model.get("keys", {}) or {})
        key, primary_role = _key_columns_and_role(
            entity_spec.get("key") or model_keys.get("primary"),
            default_role="primary",
        )
        if not key and not entity_spec.get("_row_grain"):
            raise SemanticLayerError(
                "INVALID_CONFIG", f"{path}: graph entity '{entity_key}' must declare key"
            )
        if entity_kind == "time":
            time_calendar_id = str(model.get("calendar_id", "") or "default")
            if time_calendar_id in time_calendar_ids_seen:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: graph.entities declares more than one kind: time entity for "
                    f"calendar_id '{time_calendar_id}' (only one time entity per calendar is allowed)",
                )
            time_calendar_ids_seen.add(time_calendar_id)
        foreign_keys: dict[str, list[str]] = {}
        foreign_key_roles: dict[str, str] = {}
        for foreign_name, foreign_spec in dict(model_keys.get("foreign", {}) or {}).items():
            foreign_columns, foreign_role = _key_columns_and_role(
                foreign_spec, default_role="foreign"
            )
            foreign_keys[str(foreign_name)] = foreign_columns
            foreign_key_roles[str(foreign_name)] = foreign_role
        entity_id = str(entity_spec.get("id", f"entity.{_slug(entity_key)}"))
        entity_name = str(entity_spec.get("name", entity_key))
        entity_label = str(entity_spec.get("label", _titleize(entity_key)))
        relation_ref = str(model.get("relation", "")).strip()
        relation_id = relation_aliases.get(relation_ref, "")
        relation_table = (
            relations_by_id[relation_id].output_name
            if relation_id
            else str(model.get("relation", ""))
        )
        if not relation_table:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' must declare relation",
            )
        entities.append(
            EntityConfig(
                id=entity_id,
                table=relation_table,
                key_columns=list(key),
                kind=entity_kind,
                relation_id=relation_id,
                identifiers=list(key),
                key_roles={column: primary_role for column in key},
                foreign_keys=foreign_keys,
                foreign_key_roles=foreign_key_roles,
                aliases=list(entity_spec.get("synonyms", []) or []),
                name=entity_name,
                label=entity_label,
                description=str(entity_spec.get("description", entity_label)),
                topics=_ensure_list(entity_spec.get("topics")),
                calendar_id=str(model.get("calendar_id", "")),
                allowed_as_root=bool(entity_spec.get("allowed_as_root", True)),
                freshness_source=str(
                    model.get("freshness_source", entity_spec.get("freshness_source", ""))
                ),
                freshness_sla_seconds=_optional_int(
                    model.get("freshness_sla_seconds", entity_spec.get("freshness_sla_seconds"))
                ),
                freshness_as_of=str(
                    model.get("freshness_as_of", entity_spec.get("freshness_as_of", "")) or ""
                ),
                disallowed_names=[
                    str(item).strip()
                    for item in _ensure_list(entity_spec.get("disallowed_names"))
                    if str(item).strip()
                ],
                bridge=model.get("_bridge_declared") is True,
                model=model_id if entity_spec.get("_row_grain") else "",
            )
        )
        entity_lookup[str(entity_key)] = entity_id
        entity_lookup[entity_name] = entity_id
        model_to_entity[model_id] = entity_id

    dimensions: list[DimensionConfig] = []
    temporal_roles: list[TemporalRoleConfig] = []
    measures: list[MeasureConfig] = []
    relationships: list[RelationshipConfig] = []
    value_domains: list[ValueDomainConfig] = []
    dimension_lookup: dict[tuple[str, str], str] = {}
    temporal_lookup: dict[tuple[str, str], str] = {}
    measure_lookup: dict[tuple[str, str], str] = {}
    value_domain_ids: dict[tuple[str, str], str] = {}

    dim_defaults = dict(defaults.get("dimension", {}) or {})
    time_defaults = dict(defaults.get("time", {}) or {})
    if "default" in time_defaults:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path}: defaults.time.default is not supported; declare default: true on a model's time role",
        )
    measure_defaults = dict(defaults.get("measure", {}) or {})
    relationship_defaults = dict(defaults.get("relationship", {}) or {})
    operational_contract = load_operational_contract(defaults, path=path)
    meta_contract = load_meta_contract(defaults, path=path)

    model_default_times: dict[str, str] = {}
    for model_id, model in model_rows.items():
        is_fact = model_id in fact_models
        model_meta = _normalize_meta(model.get("meta"), f"{path}: model '{model_id}'")
        fact_source_relation = ""
        if is_fact:
            # Resolve the declared time entity for this fact model.
            time_entity_ref = str(model.get("time_entity", "") or "").strip()
            if not time_entity_ref:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' must declare time_entity",
                )
            time_entity_id = entity_lookup.get(time_entity_ref)
            if time_entity_id is None:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' references unknown "
                    f"time_entity '{time_entity_ref}'",
                )
            entity_cfg = next(row for row in entities if row.id == time_entity_id)
            if entity_cfg.kind != "time":
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' time_entity must point at "
                    f"a kind:time entity (got '{entity_cfg.kind}')",
                )
            entity_id = time_entity_id
            # Resolve the fact model's own relation (its FROM table) — measures
            # built from this model carry this as `source_relation`.
            fact_relation_ref = str(model.get("relation", "")).strip()
            fact_relation_id = relation_aliases.get(fact_relation_ref, "")
            fact_source_relation = (
                relations_by_id[fact_relation_id].output_name
                if fact_relation_id
                else str(model.get("relation", ""))
            )
            if not fact_source_relation:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: fact model '{model_id}' must declare relation",
                )
        else:
            entity_id = model_to_entity[model_id]
            entity_cfg = next(row for row in entities if row.id == entity_id)
        model_topics = _ensure_list(model.get("topics"))
        model_operational_defaults = normalize_operational_payload(
            model.get("operational_defaults"),
            contract=operational_contract,
            target="measure",
            path=f"{path}: model '{model_id}' operational_defaults",
        )
        dims = dict(model.get("dimensions", {}) or {})
        times = dict(model.get("times", {}) or {})
        default_keys = [key for key, spec in times.items() if bool(dict(spec or {}).get("default"))]
        if len(default_keys) > 1:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: model '{model_id}' has multiple default: true time roles: {sorted(default_keys)}; declare at most one",
            )
        model_default_times[model_id] = str(next(iter(default_keys), ""))

        for dim_key, dim_spec_raw in dims.items():
            dim_spec = {**dim_defaults, **dict(dim_spec_raw or {})}
            dim_id = str(dim_spec.get("id", f"dimension.{_slug(entity_cfg.name)}_{_slug(dim_key)}"))
            label = str(dim_spec.get("label", _titleize(dim_key)))
            name = str(dim_spec.get("name", f"{entity_cfg.name}.{dim_key}"))
            kind = str(dim_spec.get("kind", "categorical"))
            domain_values = list(dim_spec.get("domain", dim_spec.get("valid_values", [])) or [])
            value_domain_id = ""
            if domain_values:
                value_domain_id = str(
                    dim_spec.get(
                        "value_domain_id", f"value_domain.{_slug(entity_cfg.name)}_{_slug(dim_key)}"
                    )
                )
                value_domains.append(
                    ValueDomainConfig(
                        id=value_domain_id,
                        dimensions=[dim_id],
                        values=[
                            ValueDomainValue(
                                value=value.get("value") if isinstance(value, dict) else value,
                                label=str(
                                    value.get("label", value.get("value"))
                                    if isinstance(value, dict)
                                    else value
                                ),
                                aliases=_ensure_list(value.get("aliases", []))
                                if isinstance(value, dict)
                                else [],
                                description=str(
                                    value.get(
                                        "description", value.get("label", value.get("value", ""))
                                    )
                                    if isinstance(value, dict)
                                    else ""
                                ),
                            )
                            for value in domain_values
                        ],
                        name=name,
                        label=label,
                        description=str(dim_spec.get("description", label)),
                    )
                )
                value_domain_ids[(model_id, dim_key)] = value_domain_id
            dimensions.append(
                DimensionConfig(
                    id=dim_id,
                    entity=entity_id,
                    column=str(dim_spec.get("column", dim_key)),
                    data_type=_map_dimension_kind(kind),
                    aliases=list(dim_spec.get("synonyms", []) or []),
                    name=name,
                    label=label,
                    description=str(dim_spec.get("description", label)),
                    semantic_kind=kind,
                    topics=_unique_aliases(model_topics, _ensure_list(dim_spec.get("topics"))),
                    sample_values_strategy=str(dim_spec.get("sample_values_strategy", "")),
                    filterable=bool(dim_spec.get("filterable", True)),
                    groupable=bool(dim_spec.get("groupable", True)),
                    value_domain=value_domain_id,
                    unknown_values=list(dim_spec.get("unknown_values") or []),
                )
            )
            dimension_lookup[(model_id, dim_key)] = dim_id

        for time_key, time_spec_raw in times.items():
            time_spec = {**time_defaults, **dict(time_spec_raw or {})}
            dim_id = str(
                time_spec.get(
                    "dimension_id", f"dimension.{_slug(entity_cfg.name)}_{_slug(time_key)}"
                )
            )
            label = str(time_spec.get("label", _titleize(time_key)))
            name = str(time_spec.get("name", f"{entity_cfg.name}.{time_key}"))
            if (model_id, time_key) not in dimension_lookup:
                dimensions.append(
                    DimensionConfig(
                        id=dim_id,
                        entity=entity_id,
                        column=str(time_spec.get("column", time_key)),
                        data_type=_map_dimension_kind(time_spec.get("kind", "timestamp")),
                        name=name,
                        label=label,
                        description=str(time_spec.get("description", label)),
                        semantic_kind=str(time_spec.get("kind", "timestamp")),
                        topics=_unique_aliases(model_topics, _ensure_list(time_spec.get("topics"))),
                        sample_values_strategy=str(time_spec.get("sample_values_strategy", "")),
                        filterable=bool(time_spec.get("filterable", True)),
                        groupable=bool(time_spec.get("groupable", True)),
                    )
                )
                dimension_lookup[(model_id, time_key)] = dim_id
            temporal_id = str(
                time_spec.get("id", f"temporal_role.{_slug(entity_cfg.name)}_{_slug(time_key)}")
            )
            temporal_roles.append(
                TemporalRoleConfig(
                    id=temporal_id,
                    dimension=dimension_lookup[(model_id, time_key)],
                    temporal_class=str(time_spec.get("class", "event_time")),
                    name=name,
                    label=label,
                    supported_grains=list(
                        time_spec.get(
                            "supported_grains", ["day", "week", "month", "quarter", "year"]
                        )
                    ),
                    default_query_time_axis=time_key == model_default_times[model_id],
                    timezone=str(time_spec.get("timezone", "UTC")),
                    column_timezone=str(time_spec.get("column_timezone", "") or ""),
                )
            )
            temporal_lookup[(model_id, time_key)] = temporal_id

        default_time = model_default_times[model_id]
        row_grain = _ensure_list(dict(model.get("keys", {}) or {}).get("primary"))
        # Fact models infer grain from the declared time_column (the row's
        # time-key on the fact table).
        if is_fact and not row_grain:
            time_column = str(model.get("time_column", "") or "").strip()
            if time_column:
                row_grain = [time_column]
        for measure_key, measure_spec_raw in dict(model.get("measures", {}) or {}).items():
            raw_measure_spec = dict(measure_spec_raw or {})
            measure_spec = {**measure_defaults, **raw_measure_spec}
            measure_spec = lookup_measure_spec(
                raw_measure_spec, measure_spec, f"{path}: measure '{measure_key}'"
            )
            measure_id = str(
                measure_spec.get("id", f"measure.{_slug(entity_cfg.name)}_{_slug(measure_key)}")
            )
            measure_name = str(measure_spec.get("name", f"{entity_cfg.name}.{measure_key}"))
            measure_label = str(measure_spec.get("label", _titleize(measure_key)))
            default_aggregation, allowed_aggregations, invalid_aggregations, measure_class = (
                _derive_measure_semantics(measure_spec)
            )
            if measure_class == "semi_additive" and entity_cfg.is_row_grain:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: measure '{measure_key}' is a stock, but model '{model_id}' is row "
                    "grain (no key), and a stock sums one snapshot per series: the key minus the "
                    f"clock. Declare a key on {model_id}, or make it a flow.",
                )
            # Author-facing override: `default_agg:` is canonical.
            authored_default_agg = measure_spec.get("default_agg")
            if authored_default_agg is not None:
                authored_default_agg_value = str(authored_default_agg).strip().lower()
                if authored_default_agg_value:
                    default_aggregation = authored_default_agg_value
            # Author-facing subtraction: `disallowed_aggregations:` removes the
            # listed aggregations from the derived allowed set.
            disallowed = {
                str(item).strip().lower()
                for item in _ensure_list(measure_spec.get("disallowed_aggregations"))
                if str(item).strip()
            }
            if disallowed:
                allowed_aggregations = [
                    agg for agg in allowed_aggregations if agg.lower() not in disallowed
                ]
                invalid_aggregations = list(invalid_aggregations) + [
                    agg
                    for agg in disallowed
                    if agg not in {a.lower() for a in invalid_aggregations}
                ]
            if default_aggregation and default_aggregation not in allowed_aggregations:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: measure '{measure_key}' has default_agg '{default_aggregation}' which is not in allowed_aggregations {allowed_aggregations}",
                )
            kind = str(measure_spec.get("kind", "")).strip().lower()
            authoring_warnings: list[str] = []
            raw_aggregation = str(raw_measure_spec.get("aggregation", "") or "").strip().lower()
            if raw_aggregation == "count_distinct" and kind != "entity_count":
                authoring_warnings.append(
                    f"measure '{measure_id}' declares aggregation: count_distinct, but measure-level aggregation is ignored for kind '{kind or 'additive'}'; use kind: entity_count with entity_key for count-distinct measures"
                )
            entity_key = _ensure_list(measure_spec.get("entity_key") or row_grain)
            if kind == "entity_count":
                expr_raw = measure_spec.get("expr")
                if expr_raw is None:
                    if len(entity_key) != 1:
                        raise SemanticLayerError(
                            "INVALID_CONFIG",
                            f"{path}: entity_count measure '{measure_key}' requires a single-column entity_key",
                        )
                    expr_raw = {"kind": "column", "column": entity_key[0]}
            else:
                expr_raw = measure_spec.get("expr")
                if expr_raw is None:
                    if not kind:
                        raise SemanticLayerError(
                            "INVALID_CONFIG",
                            f"{path}: measure '{measure_key}' declares neither kind nor expr; "
                            f"declare kind: aggregate with an expr:, or kind: entity_count "
                            f"with an entity_key:",
                        )
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"{path}: measure '{measure_key}' (kind '{kind}') is missing expr",
                    )
            temporal_refs = _ensure_list(
                measure_spec.get("times")
                or measure_spec.get("time")
                or ([default_time] if default_time else [])
            )
            compatible_temporal_roles = [
                temporal_lookup[(model_id, ref)] if (model_id, ref) in temporal_lookup else str(ref)
                for ref in temporal_refs
            ]
            _, structured_example_entries = _normalize_examples(measure_spec.get("examples"))
            measure_topics = _unique_aliases(
                model_topics, _ensure_list(measure_spec.get("topics"))
            ) or _default_topics(measure_name, fallback=model_id)
            default_temporal_role = str(
                measure_spec.get("default_temporal_role")
                or (compatible_temporal_roles[0] if compatible_temporal_roles else "")
            )
            measure_operational = validate_operational_payload(
                merge_operational_payloads(
                    model_operational_defaults,
                    normalize_operational_payload(
                        measure_spec.get("operational"),
                        contract=operational_contract,
                        target="measure",
                        path=f"{path}: measure '{measure_id}' operational",
                    ),
                ),
                contract=operational_contract,
                target="measure",
                path=f"{path}: measure '{measure_id}' operational",
            )
            measure = MeasureConfig(
                id=measure_id,
                entity=entity_id,
                row_grain=list(row_grain),
                source_relation=fact_source_relation,
                expr=parse_config_expression(expr_raw),
                default_aggregation=default_aggregation,
                allowed_aggregations=allowed_aggregations,
                invalid_aggregations=invalid_aggregations,
                measure_class=measure_class,
                accumulation=_normalize_accumulation(measure_spec),
                compatible_temporal_roles=compatible_temporal_roles,
                value_type=str(measure_spec.get("value_type", "number")),
                currency=str(measure_spec.get("currency", "")),
                aliases=list(measure_spec.get("synonyms", []) or []),
                name=measure_name,
                label=measure_label,
                description=str(measure_spec.get("description", measure_label)),
                topics=measure_topics,
                suggested_aggregations=_ensure_list(measure_spec.get("suggested_aggregations"))
                or _suggested_aggregations(
                    default_aggregation, allowed_aggregations, measure_class
                ),
                comparison_family=str(measure_spec.get("comparison_family", "")),
                comparison_mode=str(measure_spec.get("comparison_mode", "")),
                preferred_companion_metrics=_ensure_list(
                    measure_spec.get("preferred_companion_metrics")
                ),
                operational=measure_operational,
                default_temporal_role=default_temporal_role,
                meta=_merge_meta(
                    model_meta,
                    _normalize_meta(measure_spec.get("meta"), f"{path}: measure '{measure_key}'"),
                ),
                example_entries=structured_example_entries,
                validity_windows=_parse_validity_windows(measure_spec.get("validity_windows")),
                external_discontinuities=_parse_external_discontinuities(
                    measure_spec.get("external_discontinuities")
                ),
                cross_window_policy=str(
                    measure_spec.get("cross_window_policy", "caveat") or "caveat"
                ),
                authoring_warnings=authoring_warnings,
                additive=_authored_additive(measure_spec, kind, f"{path}: measure '{measure_key}'"),
                lookup_from=str(measure_spec.get("from", "") or "").strip(),
                lookup_via=str(measure_spec.get("via", "") or "").strip(),
                publish=measure_spec.get("publish") is not False,
            )
            measures.append(measure)
            measure_lookup[(model_id, str(measure_key))] = measure_id

        keys = dict(model.get("keys", {}) or {})
        foreign_keys = {}
        foreign_key_roles = {}
        for foreign_name, foreign_spec in dict(keys.get("foreign", {}) or {}).items():
            foreign_columns, foreign_role = _key_columns_and_role(
                foreign_spec, default_role="foreign"
            )
            foreign_keys[str(foreign_name)] = foreign_columns
            foreign_key_roles[str(foreign_name)] = foreign_role
        for edge_key, join_spec_raw in dict(model.get("joins", {}) or {}).items():
            join_spec = {**relationship_defaults, **dict(join_spec_raw or {})}
            target_ref = str(join_spec.get("to", edge_key))
            if target_ref not in entity_lookup:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: join '{model_id}.{edge_key}' references unknown graph entity '{target_ref}'",
                )
            local_cols = _ensure_list(join_spec.get("via") or foreign_keys.get(edge_key))
            target_cfg = next(row for row in entities if row.id == entity_lookup[target_ref])
            rel_id = str(join_spec.get("id", f"relationship.{_slug(model_id)}_{_slug(edge_key)}"))
            if target_cfg.is_row_grain:  # nothing can look up one row of a row-grain model
                source = next(key for key, value in entity_lookup.items() if value == entity_id)
                via = _ensure_list(join_spec.get("target")) or list(entity_cfg.key_columns)
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{rel_id} joins into '{target_ref}', a row-grain model (no key), so one row "
                    f"can't be looked up there. Fix: write it from {target_ref}'s side (entities: "
                    f"[{target_ref}, {source}], cardinality: many_to_one, via: "
                    f"[{', '.join(via)}]), or declare a key on {target_ref} if its rows have one.",
                    details={"relationship": rel_id, "model": target_ref},
                )
            target_cols = _ensure_list(join_spec.get("target") or target_cfg.key)
            if len(local_cols) != len(target_cols):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: join '{model_id}.{edge_key}' key width does not match target key",
                )
            local_role = str(
                join_spec.get("source_key_role", foreign_key_roles.get(str(edge_key), "")) or ""
            ).lower()
            if not local_role:
                local_role = (
                    "primary"
                    if all(column in set(entity_cfg.key_columns) for column in local_cols)
                    else "foreign"
                )
            target_role = str(join_spec.get("target_key_role", "") or "").lower()
            if not target_role:
                target_roles = {
                    target_cfg.key_roles.get(column, "primary") for column in target_cols
                }
                target_role = next(iter(target_roles)) if len(target_roles) == 1 else "natural"
            if "cardinality" in join_spec:
                cardinality = str(join_spec["cardinality"])
            elif local_role in {"primary", "unique"} and target_role in {"primary", "unique"}:
                cardinality = "1:1"
            elif local_role == "foreign" and target_role in {"primary", "unique"}:
                cardinality = "N:1"
            elif local_role in {"primary", "unique"} and target_role == "foreign":
                cardinality = "1:N"
            else:
                cardinality = "M:N"
            safety_default = "safe" if cardinality in {"1:1", "N:1"} else "requires_rewrite"
            safety = str(join_spec.get("safety", safety_default)).lower()
            if safety not in {"safe", "requires_rewrite", "unsafe"}:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: join '{model_id}.{edge_key}' has invalid safety '{safety}' (must be 'safe', 'requires_rewrite', or 'unsafe')",
                )
            relationships.append(
                RelationshipConfig(
                    id=rel_id,
                    source_entity=entity_id,
                    target_entity=entity_lookup[target_ref],
                    source_column=local_cols[0],
                    target_column=target_cols[0],
                    cardinality=cardinality,
                    safety=safety,
                    source_columns=list(local_cols),
                    target_columns=list(target_cols),
                    source_key_role=local_role,
                    target_key_role=target_role,
                    name=str(join_spec.get("name", f"{entity_cfg.name}_TO_{target_cfg.name}")),
                    label=str(join_spec.get("label", f"{entity_cfg.label} to {target_cfg.label}")),
                    description=str(join_spec.get("description", join_spec.get("label", ""))),
                    allowed_directions=list(
                        join_spec.get("traversal", ["forward", "reverse"]) or ["forward", "reverse"]
                    ),
                    temporal_validity={
                        str(k): str(v)
                        for k, v in dict(join_spec.get("temporal_validity", {}) or {}).items()
                    },
                    target_key_type=str(join_spec.get("target_key_type", "primary") or "primary"),
                    join_semantics=str(join_spec.get("join_semantics", "")),
                    rollup_safe_aggregations_reverse=_ensure_list(
                        join_spec.get("rollup_safe_aggregations_reverse")
                    ),
                )
            )
    entities = _with_displays(
        entities, graph_entities, model_rows, dimension_lookup, dimensions, path=path
    )
    path_preferences = _parse_path_preferences(
        raw,
        entities=entities,
        entity_lookup=entity_lookup,
        relationships=relationships,
        path=path,
    )
    measures = _with_zero_through(
        measures,
        model_rows,
        defaults,
        warehouse=str(package_raw.get("warehouse", "duckdb")).strip().lower(),
        field_of=field_reader(
            model_rows,
            relation_aliases,
            _load_fields(dimension_lookup, temporal_lookup, dimensions, temporal_roles),
        ),
        model_of={measure: model for (model, _), measure in measure_lookup.items()},
        path=path,
    )
    measures = resolve_lookup_measures(
        measures,
        relationships,
        entities,
        entity_lookup,
        path_preferences=path_preferences,
        path=str(path),
    )

    metric_recipes_by_id: dict[str, MetricConfig] = {}

    def _add_metric_recipe(metric: MetricConfig, *, source: str) -> None:
        existing = metric_recipes_by_id.get(metric.id)
        if existing is not None:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: duplicate metric id '{metric.id}' in {existing.name or existing.id} and {source}; metric ids must be unique",
            )
        metric_recipes_by_id[metric.id] = metric

    # Build a measure key/name → measure_id index so metric authoring can
    # use package-relative keys (`revenue_usd`) instead of fully qualified
    # IDs (`measure.jaffle.revenue_usd`).
    measure_key_index: dict[str, str] = {}
    for measure in measures:
        measure_key_index[measure.id] = measure.id
        if measure.name:
            measure_key_index[measure.name] = measure.id
        # Tail of the id (after `measure.<ns>.`) is the local key.
        tail = measure.id.split(".", 2)[-1] if measure.id.startswith("measure.") else ""
        if tail:
            measure_key_index[tail] = measure.id
    # Add the local-key index from the YAML side (the YAML mapping keys
    # under `measures:`).
    for _model_id, model in model_rows.items():
        for measure_key in dict(model.get("measures", {}) or {}):
            # Match against a measure already in `measures` whose tail matches
            # this key (best-effort).
            if str(measure_key) in measure_key_index:
                continue
            for measure in measures:
                tail = measure.id.split(".", 2)[-1] if measure.id.startswith("measure.") else ""
                if tail == str(measure_key):
                    measure_key_index[str(measure_key)] = measure.id
                    break

    def _resolve_measure_ref(ref: Any, *, model_id: str = "") -> str:
        text = str(ref or "").strip()
        if not text:
            return text
        if model_id and (model_id, text) in measure_lookup:
            return measure_lookup[(model_id, text)]
        if text in measure_key_index:
            return measure_key_index[text]
        return text  # leave as-is for downstream validation

    measure_model = {measure_id: model for (model, _key), measure_id in measure_lookup.items()}

    def _resolve_measure_dimension_ref(measure_id: str, field: str) -> str:
        """A short dimension key on the measure's own model; anything else is left as written."""
        return dimension_lookup.get((measure_model.get(measure_id, ""), field), field)

    # Build a metric key/name → metric_id index so metric authoring can refer
    # to top-level metrics (not just measures) via package-relative keys.
    # Sources: metrics_rows (with id auto-derived by the package loader) and
    # metric.name when authored.
    metric_key_index: dict[str, str] = {}
    for metric_key, metric_spec_raw in metrics_rows.items():
        spec = dict(metric_spec_raw or {})
        metric_id = str(
            spec.get(
                "id",
                f"metric.{namespace}.{_slug(str(metric_key))}"
                if namespace
                else f"metric.{_slug(str(metric_key))}",
            )
        )
        metric_key_index[metric_id] = metric_id
        local_key = str(metric_key)
        # Local key (YAML mapping key under `metrics:`).
        if local_key and local_key not in metric_key_index:
            metric_key_index[local_key] = metric_id
        # Authored name (often namespace-qualified, e.g. `synth.premium_share`).
        name = str(spec.get("name", "") or "")
        if name and name not in metric_key_index:
            metric_key_index[name] = metric_id
        # Tail of the id (after `metric.<ns>.`) is also a valid local handle.
        if metric_id.startswith("metric."):
            tail = metric_id.split(".", 2)[-1]
            if tail and tail not in metric_key_index:
                metric_key_index[tail] = metric_id

    def _resolve_metric_ref_full(ref: Any) -> str:
        """Resolve a metric reference to a fully-qualified id.

        Resolution order:
          1. Already fully-qualified (`metric.<ns>.<key>`) — return as-is.
          2. Top-level metric (from the `metrics:` block) — preferred.
          3. Measure (auto-published as a metric via convention) — fallback.

        If the same key matches BOTH a top-level metric and a measure, we
        raise a clear ambiguity error pointing the author at both
        candidates and asking them to use the fully-qualified id.
        """
        text = str(ref or "").strip()
        if not text:
            return text
        if text.startswith("metric."):
            return text
        metric_hit = metric_key_index.get(text)
        measure_hit_id = measure_key_index.get(text)
        if metric_hit and measure_hit_id and measure_hit_id.startswith("measure."):
            measure_as_metric = measure_hit_id.replace("measure.", "metric.", 1)
            if metric_hit != measure_as_metric:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    (
                        f"{path}: ambiguous metric reference '{text}' — matches "
                        f"both top-level metric '{metric_hit}' and measure "
                        f"'{measure_hit_id}' (also reachable at "
                        f"'{measure_as_metric}'); use the fully-qualified id "
                        f"to disambiguate"
                    ),
                )
        if metric_hit:
            return metric_hit
        if measure_hit_id and measure_hit_id.startswith("measure."):
            return measure_hit_id.replace("measure.", "metric.", 1)
        return text  # leave as-is for downstream validation

    known_dimension_ids = frozenset(dimension_lookup.values())
    for metric_key, metric_spec_raw in metrics_rows.items():
        spec = dict(metric_spec_raw or {})

        # Translate direct named fields per metric kind into the runtime
        # expression AST shape. Authors can still write the AST directly
        # under `expression:` for long-tail kinds (`derived`, `conversion`).
        def _classify_ref(text: str) -> str:
            """Tells the ratio-operand expander whether a key resolves to
            a top-level metric or a measure (or neither). Returns one of
            'metric', 'measure', or '' when no match was found.
            """
            text = str(text or "").strip()
            if not text or text.startswith("metric.") or text.startswith("measure."):
                return ""
            if text in metric_key_index:
                return "metric"
            if text in measure_key_index:
                return "measure"
            return ""

        def _suggest_refs(text: str) -> list[str]:
            from difflib import get_close_matches

            candidates = sorted(
                key
                for key in (*measure_key_index, *metric_key_index)
                if "." not in key  # offer the short authoring keys, not full ids
            )
            return get_close_matches(str(text), candidates, n=3, cutoff=0.5)

        spec = _translate_metric_direct_fields(
            spec,
            resolve=_resolve_measure_ref,
            resolve_metric=_resolve_metric_ref_full,
            resolve_dimension=_resolve_measure_dimension_ref,
            known_dimensions=known_dimension_ids,
            classify_ref=_classify_ref,
            context=f"{path}: metric '{metric_key}'",
            suggest=_suggest_refs,
        )
        expression = spec.get("expression") or {}
        if not expression:
            metric_kind = str(spec.get("kind", "") or "").strip().lower()
            requirement_by_kind = {
                "aggregate": "a measure: field",
                "semi_additive": "a measure: field",
                "cumulative": "a measure: field",
                "rolling": "a measure: field",
                "prior_period": "a measure: field",
                "period_to_date": "a measure: field",
                "ratio": "numerator: and denominator: fields",
                "derived": "an expression: block",
                "conversion": "an expression: block",
            }
            requirement = requirement_by_kind.get(metric_kind)
            if requirement is None:
                detail = (
                    f"unknown kind '{metric_kind}'; valid kinds: "
                    f"{', '.join(sorted(requirement_by_kind))}"
                    if metric_kind
                    else "it declares no kind:; valid kinds: "
                    + ", ".join(sorted(requirement_by_kind))
                )
            else:
                detail = f"kind '{metric_kind}' requires {requirement}"
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: metric '{metric_key}' produced no expression — {detail}",
            )
        temporal_role = str(spec.get("temporal_role", "")).strip()
        metric_id = str(spec.get("id", f"metric.{metric_key}"))
        _, metric_example_entries = _normalize_examples(spec.get("examples"))
        metric_compatible_temporal_roles = list(
            spec.get("compatible_temporal_roles", [temporal_role] if temporal_role else []) or []
        )
        metric = MetricConfig(
            id=metric_id,
            kind=str(spec.get("kind", "derived")),
            expression=_parse_metric_expression(
                expression,
                context=f"{path}: metric '{metric_key}'",
            ),
            temporal_role=temporal_role,
            compatible_temporal_roles=metric_compatible_temporal_roles,
            filter_spec=expression.get("filter", {}) or {},
            window_spec=expression.get("window", {}) or {},
            aliases=list(spec.get("synonyms", []) or []),
            name=str(spec.get("name", metric_key)),
            label=str(spec.get("label", _titleize(metric_key))),
            description=str(spec.get("description", spec.get("label", ""))),
            topics=_ensure_list(spec.get("topics"))
            or _default_topics(str(spec.get("name", metric_key))),
            comparison_family=str(spec.get("comparison_family", "")),
            comparison_mode=str(spec.get("comparison_mode", "")),
            preferred_companion_metrics=_ensure_list(spec.get("preferred_companion_metrics")),
            operational=validate_operational_payload(
                normalize_operational_payload(
                    spec.get("operational"),
                    contract=operational_contract,
                    target="metric",
                    path=f"{path}: metric '{metric_id}' operational",
                ),
                contract=operational_contract,
                target="metric",
                path=f"{path}: metric '{metric_id}' operational",
            ),
            meta=_normalize_meta(spec.get("meta"), f"{path}: {metric_id}"),
            example_entries=metric_example_entries,
            value_type=str(spec.get("value_type", "number") or "number"),
        )
        _add_metric_recipe(metric, source=f"metric '{metric_key}'")

    # Defined once with the per-segment ``model_id`` threaded as an arg
    # rather than closed over. The previous form defined this function
    # inside the per-segment loop, closing over ``model_id`` — correct
    # by accident (immediate invocation in the same iteration) but a
    # well-known late-binding footgun if the function were ever stored
    # and called later. (The mainline ruff/mypy pass landed a parallel
    # default-arg fix; this hoisted form supersedes it.)
    def _resolve_segment_dimension_ref(value: Any, model_id_for_segment: str) -> str:
        ref = str(value or "").strip()
        if not ref:
            return ref
        if ref.startswith("dimension."):
            return ref
        if model_id_for_segment and (model_id_for_segment, ref) in dimension_lookup:
            return dimension_lookup[(model_id_for_segment, ref)]
        return ref

    segments: list[SegmentConfig] = []
    for segment_key, segment_spec_raw in segments_rows.items():
        spec = dict(segment_spec_raw or {})
        segment_id = str(spec.get("id", f"segment.{segment_key}"))
        entity_ref = str(spec.get("entity", "")).strip()
        entity_id = entity_lookup.get(entity_ref, entity_ref)
        membership = dict(spec.get("membership", {}) or {})
        model_id = next((key for key, value in model_to_entity.items() if value == entity_id), "")

        preview_dimensions = [
            _resolve_segment_dimension_ref(value, model_id)
            for value in _ensure_list(spec.get("preview_dimensions"))
        ]
        segments.append(
            SegmentConfig(
                id=segment_id,
                entity=entity_id,
                basis_metric=str(spec.get("basis_metric", "")),
                preview_dimensions=preview_dimensions,
                where=list(membership.get("where", []) or []),
                metric_filters=list(membership.get("metric_filters", []) or []),
                time=dict(membership.get("time", {}) or {}),
                temporal_role_overrides={
                    str(k): str(v)
                    for k, v in dict(membership.get("temporal_role_overrides", {}) or {}).items()
                },
                aliases=list(spec.get("synonyms", []) or []),
                name=str(spec.get("name", segment_id)),
                label=str(spec.get("label", _titleize(segment_key))),
                description=str(spec.get("description", spec.get("label", ""))),
                topics=_ensure_list(spec.get("topics"))
                or _default_topics(str(spec.get("name", segment_id)), fallback="segments"),
            )
        )

    policies = []
    for row in list(raw.get("semantic_policies", []) or []):
        row_dict = dict(row or {})
        policies.append(
            SemanticPolicyConfig(
                id=str(row_dict["id"]),
                kind=str(row_dict["kind"]),
                config={
                    key: value
                    for key, value in row_dict.items()
                    if key
                    not in {
                        "id",
                        "kind",
                        "object_ids",
                        "audiences",
                        "environments",
                        "roles",
                        "action",
                        "rationale",
                    }
                    or (row_dict["kind"] == "row_filter" and key in {"object_ids", "action"})
                },
                object_ids=_ensure_list(row_dict.get("object_ids")),
                audiences=_ensure_list(row_dict.get("audiences")),
                environments=_ensure_list(row_dict.get("environments")),
                roles=_ensure_list(row_dict.get("roles")),
                action=str(row_dict.get("action", "")),
                rationale=str(row_dict.get("rationale", "")),
            )
        )

    caveats = _parse_caveats(raw.get("semantic_caveats", []), path=path)

    entity_ids = {entity.id for entity in entities}
    dimension_ids = {dimension.id for dimension in dimensions}
    temporal_role_ids = {role.id for role in temporal_roles}
    relationship_ids = {row.id for row in relationships}
    measures_by_id = {measure.id: measure for measure in measures}
    model_measure_keys: dict[str, set[str]] = {
        model_id: set(dict(model.get("measures", {}) or {}))
        for model_id, model in model_rows.items()
    }
    model_dimension_keys: dict[str, set[str]] = {
        model_id: set(dict(model.get("dimensions", {}) or {}))
        for model_id, model in model_rows.items()
    }

    def _resolve_dimension_ref(ref: Any, *, model_id: str = "") -> str:
        text = str(ref or "").strip()
        if not text:
            return ""
        if text in dimension_ids:
            return text
        if model_id and (model_id, text) in dimension_lookup:
            return dimension_lookup[(model_id, text)]
        return text

    def _resolve_entity_ref(ref: Any) -> str:
        text = str(ref or "").strip()
        return entity_lookup.get(text, text)

    def _aggregate_from_row(row_dict: dict[str, Any]) -> AggregateRelationConfig:
        source_entity = row_dict["source_entity"]
        raw_measure_bindings = row_dict["measures"]
        measure_columns: dict[str, str] = {}
        measure_rollups: dict[str, str] = {}
        measure_aggregations: dict[str, str] = {}
        measure_holds: dict[str, str] = {}
        for measure_id, binding in raw_measure_bindings.items():
            column = str(binding.get("column", "") or "").strip()
            if column:
                measure_columns[measure_id] = column
            measure_rollups[measure_id] = str(binding.get("rollup", "") or "").strip()
            measure_aggregations[measure_id] = str(binding.get("aggregation", "") or "").strip()
            holds = str(binding.get("holds", "") or "").strip().lower()
            allowed = getattr(measures_by_id.get(measure_id), "allowed_aggregations", [holds])
            if holds and (holds not in _ROLLUP_HOLDS or holds not in allowed):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: aggregate relation measure '{measure_id}' declares holds '{holds}';"
                    f" use one of {sorted(_ROLLUP_HOLDS & set(allowed))}",
                )
            if holds:
                measure_holds[measure_id] = holds
        raw_dimension_bindings = row_dict["dimensions"]
        dimension_columns: dict[str, str] = {}
        dimension_paths: dict[str, list[str]] = {}
        for dim_id, binding in raw_dimension_bindings.items():
            column = str(binding.get("column", "") or "").strip()
            if column:
                dimension_columns[dim_id] = column
            join_path = [str(item).strip() for item in _ensure_list(binding.get("path"))]
            unknown_relationships = sorted(set(join_path) - relationship_ids)
            if unknown_relationships:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"{path}: aggregate relation dimension '{dim_id}' path names unknown"
                    f" relationships {unknown_relationships}",
                )
            if join_path:
                dimension_paths[dim_id] = join_path
        grain = str(row_dict.get("grain", "")).strip().lower()
        eligible = _ensure_list(row_dict.get("eligible_time_grains")) or _coarser_time_grains(grain)
        relation_id = row_dict["id"]
        source = str(row_dict.get("source", "default") or "default")
        if source != "default":
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' declares source '{source}', but MVP aggregate routing only supports source: default",
            )
        unknown_dimensions = sorted(
            item for item in raw_dimension_bindings if item and item not in dimension_ids
        )
        if unknown_dimensions:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown dimensions {unknown_dimensions}",
            )
        temporal_role = str(row_dict.get("temporal_role", ""))
        if temporal_role and temporal_role not in temporal_role_ids:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown temporal_role '{temporal_role}'",
            )
        entity_grain = [
            _resolve_entity_ref(item) for item in _ensure_list(row_dict.get("entity_grain"))
        ]
        unknown_entity_grain = sorted(
            item for item in entity_grain if item and item not in entity_ids
        )
        if unknown_entity_grain:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"{path}: aggregate relation '{relation_id}' references unknown entity_grain entries {unknown_entity_grain}",
            )
        return AggregateRelationConfig(
            id=relation_id,
            relation=str(row_dict.get("relation", "")),
            source_entity=source_entity,
            measures=list(raw_measure_bindings),
            dimensions=list(raw_dimension_bindings),
            temporal_role=temporal_role,
            grain=grain,
            entity_grain=entity_grain,
            description=str(row_dict.get("description", "")),
            freshness_source=str(row_dict.get("freshness_source", "")),
            freshness_sla_seconds=_optional_int(row_dict.get("freshness_sla_seconds")),
            freshness_as_of=str(row_dict.get("freshness_as_of", "") or ""),
            model_id=row_dict["model_id"],
            variant_id=str(row_dict.get("variant_id", "") or ""),
            source=source,
            time_column=str(row_dict.get("time_column", "") or ""),
            eligible_time_grains=[
                str(item).strip().lower() for item in eligible if str(item).strip()
            ],
            measure_columns=measure_columns,
            measure_rollups=measure_rollups,
            measure_aggregations=measure_aggregations,
            measure_holds=measure_holds,
            dimension_columns=dimension_columns,
            dimension_paths=dimension_paths,
            excluded_entities=row_dict["excluded_entities"],
            excluded_dimensions=row_dict["excluded_dimensions"],
            selection_priority=int(row_dict.get("selection_priority", 0) or 0),
            equivalence_kind=str(row_dict.get("equivalence_kind", "") or ""),
        )

    def _aggregate_rows_from_model_variants() -> list[AggregateRelationConfig]:
        rows: list[AggregateRelationConfig] = []
        for model_id, model in model_rows.items():
            variants_raw = dict(model.get("variants", {}) or {})
            if not variants_raw:
                continue
            variants = _resolve_variant_specs(variants_raw, path=path, model_id=model_id)
            source_entity = model_to_entity.get(model_id) or _resolve_entity_ref(
                model["time_entity"]
            )
            default_time_key = model_default_times[model_id] or str(
                next(iter(model.get("times", {}) or {}), "")
            )
            base_time_spec = dict(
                dict(model.get("times", {}) or {}).get(default_time_key, {}) or {}
            )
            for variant_id, variant in variants.items():
                grain_spec = dict(variant.get("grain", {}) or {})
                time_grain = str(grain_spec.get("time", "") or "").strip().lower()
                if not time_grain or time_grain == "transaction":
                    continue
                relation = str(variant.get("relation", "") or "").strip()
                if not relation:
                    continue
                excludes = dict(variant.get("excludes", {}) or {})
                excluded_dimensions = {
                    _resolve_dimension_ref(item, model_id=model_id)
                    for item in _ensure_list(excludes.get("dimensions"))
                }
                excluded_entities = {
                    _resolve_entity_ref(item) for item in _ensure_list(excludes.get("entities"))
                }
                excluded_measures = {
                    _resolve_measure_ref(item, model_id=model_id)
                    for item in _ensure_list(excludes.get("measures"))
                }
                columns = dict(variant.get("columns", {}) or {})
                measure_bindings: dict[str, dict[str, str]] = {}
                for measure_key in sorted(model_measure_keys.get(model_id, set())):
                    measure_id = _resolve_measure_ref(measure_key, model_id=model_id)
                    if measure_id in excluded_measures:
                        continue
                    raw_measure_spec = dict(
                        dict(model.get("measures", {}) or {}).get(measure_key, {}) or {}
                    )
                    raw_column = columns.get(measure_id, columns.get(measure_key, measure_key))
                    if isinstance(raw_column, dict):
                        column = str(raw_column.get("column", "") or "").strip()
                        rollup = str(
                            raw_column.get("rollup", raw_measure_spec.get("rollup", "")) or ""
                        ).strip()
                        holds = str(raw_column.get("holds", "") or "").strip()
                        aggregation = str(
                            raw_column.get("aggregation", "" if holds else "sum") or ""
                        ).strip()
                    else:
                        holds = ""
                        column = str(raw_column or "").strip()
                        rollup = str(
                            raw_measure_spec.get("rollup", "additive") or "additive"
                        ).strip()
                        aggregation = "sum"
                    measure_bindings[measure_id] = {
                        "column": column,
                        "rollup": rollup,
                        "aggregation": aggregation,
                        "holds": holds,
                    }
                dimension_bindings: dict[str, dict[str, Any]] = {}
                for dim_key in sorted(model_dimension_keys.get(model_id, set())):
                    dim_id = _resolve_dimension_ref(dim_key, model_id=model_id)
                    dim_cfg = next((row for row in dimensions if row.id == dim_id), None)
                    raw_dim_spec = dict(
                        dict(model.get("dimensions", {}) or {}).get(dim_key, {}) or {}
                    )
                    if dim_id in excluded_dimensions:
                        continue
                    if (
                        str(raw_dim_spec.get("kind", "") or "").lower() == "id"
                        and dim_key not in columns
                        and dim_id not in columns
                    ):
                        continue
                    raw_column = columns.get(dim_id, columns.get(dim_key, ""))
                    dimension_bindings[dim_id] = (
                        raw_column
                        if isinstance(raw_column, dict)
                        else {
                            "column": str(
                                raw_column or getattr(dim_cfg, "column", dim_key) or dim_key
                            ).strip()
                        }
                    )
                for dim_id, raw_column in columns.items():
                    if (
                        str(dim_id).startswith("dimension.")
                        and dim_id not in dimension_bindings
                        and dim_id not in excluded_dimensions
                    ):
                        dimension_bindings[dim_id] = (
                            raw_column if isinstance(raw_column, dict) else {"column": raw_column}
                        )
                time_spec = dict(variant.get("time", {}) or {})
                role_ref = str(time_spec.get("role", default_time_key) or "").strip()
                temporal_role = temporal_lookup.get((model_id, role_ref), role_ref)
                time_column = str(
                    time_spec.get("column", base_time_spec.get("column", default_time_key)) or ""
                ).strip()
                selection = dict(variant.get("selection", {}) or {})
                equivalence = dict(variant.get("equivalence", {}) or {})
                rows.append(
                    _aggregate_from_row(
                        {
                            "id": str(
                                variant.get(
                                    "id",
                                    f"aggregate_relation.{_slug(model_id)}_{_slug(variant_id)}",
                                )
                            ),
                            "relation": relation,
                            "source_entity": source_entity,
                            "temporal_role": temporal_role,
                            "time_column": time_column,
                            "grain": time_grain,
                            "entity_grain": _ensure_list(grain_spec.get("entities")),
                            "measures": measure_bindings,
                            "dimensions": dimension_bindings,
                            "eligible_time_grains": _ensure_list(
                                variant.get("eligible_time_grains")
                            )
                            or _coarser_time_grains(time_grain),
                            "excluded_entities": list(excluded_entities),
                            "excluded_dimensions": list(excluded_dimensions),
                            "selection_priority": int(selection.get("priority", 0) or 0),
                            "equivalence_kind": str(equivalence.get("kind", "exact") or "exact"),
                            "model_id": model_id,
                            "variant_id": variant_id,
                            "source": str(variant.get("source", "default") or "default"),
                            "description": str(variant.get("description", "") or ""),
                            "freshness_source": str(variant.get("freshness_source", "") or ""),
                            "freshness_sla_seconds": variant.get("freshness_sla_seconds"),
                            "freshness_as_of": str(variant.get("freshness_as_of", "") or ""),
                        },
                    )
                )
        return rows

    aggregate_relations = _aggregate_rows_from_model_variants()

    config = PackageConfig(
        version=1,
        package=_parse_package_meta(package_raw, path=path, defaults=defaults),
        entities=entities,
        dimensions=dimensions,
        temporal_roles=temporal_roles,
        relationships=relationships,
        value_domains=value_domains,
        measures=measures,
        metric_recipes=sorted(metric_recipes_by_id.values(), key=lambda row: row.id),
        segments=sorted(segments, key=lambda row: row.id),
        path_preferences=path_preferences,
        path_policy=_parse_path_policy(raw, path=path),
        semantic_policies=policies,
        semantic_caveats=caveats,
        aggregate_relations=aggregate_relations,
        relations=relations,
        operational_contract=operational_contract,
        meta_contract=meta_contract,
    )
    config = with_published_flags(config)
    _ensure_unique_object_ids(config, path=path)
    _validate_caveat_refs(config, path=path)
    validate_row_filters(config)
    for policy in config.semantic_policies:
        policy_action(policy)
    _validate_policy_scopes(config, path=path)
    validate_expression_calls(config, dialect_for_warehouse(config.package.warehouse))
    return config


def _load_fields(
    dimension_lookup: dict[tuple[str, str], str],
    temporal_lookup: dict[tuple[str, str], str],
    dimensions: list[DimensionConfig],
    temporal_roles: list[TemporalRoleConfig],
) -> dict[tuple[str, str], tuple[str, str, str]]:
    """Each model field's column, data type and the zone its naive values are stored in."""
    dims = {row.id: row for row in dimensions}
    roles = {row.id: row for row in temporal_roles}
    out = {}
    for key, dim_id in dimension_lookup.items():
        role = roles.get(temporal_lookup.get(key, ""))
        zone = (role.column_timezone or role.timezone) if role is not None else ""
        out[key] = (dims[dim_id].column, dims[dim_id].data_type, zone or "UTC")
    return out


def _with_zero_through(
    measures: list[MeasureConfig],
    model_rows: dict[str, dict[str, Any]],
    defaults: dict[str, Any],
    *,
    warehouse: str,
    field_of: FieldOf,
    model_of: dict[str, str],
    path: str,
) -> list[MeasureConfig]:
    """Each measure with its model's ``zero_through``, else the project's default."""

    def edge(value: Any, where: str) -> ZeroThrough | None:
        return zero_through(value, where=where, path=path, warehouse=warehouse, field_of=field_of)

    default = edge(defaults.get("zero_through"), "defaults.zero_through")
    edges = {
        model_id: edge(model["zero_through"], f"models.{model_id}.zero_through")
        if "zero_through" in model
        else default
        for model_id, model in model_rows.items()
    }
    return [replace(row, zero_through=edges.get(model_of.get(row.id, ""))) for row in measures]
