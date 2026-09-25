"""Raw-YAML key allowlists and shape checks that ``config_validation`` runs before loading."""

from __future__ import annotations

from typing import Any


def add_error(errors: list[str], message: str) -> None:
    errors.append(message)


# Canonical enum for authored `dimension.kind:` values. Kept in sync with
# `_map_dimension_kind` in config.py and the supported set listed in
# docs/PACKAGE_AUTHORING.md. `id` is intentionally excluded — key
# dimensions auto-create from graph entity keys (rejected separately).
_VALID_DIMENSION_KINDS: frozenset[str] = frozenset(
    {
        "categorical",
        "boolean",
        "integer",
        "continuous",
        "number",
        "percent",
        "currency",
    }
)

# Compiled-config dimension-kind enum. Broader than the YAML authoring
# set because `times:` blocks AUTO-GENERATE a paired DimensionConfig
# with semantic_kind="date"/"timestamp" — those are legitimate at the
# compiled layer even though the user is forbidden from authoring them
# under `dimensions:`.
_VALID_COMPILED_DIMENSION_KINDS: frozenset[str] = _VALID_DIMENSION_KINDS | {"date", "timestamp"}


# Canonical enum for authored `times.<role>.kind:` values. A times block
# always describes a temporal column, so only date/timestamp are valid.
_VALID_TIME_KINDS: frozenset[str] = frozenset({"date", "timestamp"})

# Canonical enum for authored `times.<role>.class:` values. The compiler
# branches on these (e.g. _allows_coarse_snapshot_alignment) so unknown
# values silently disable behavior; better to fail loudly.
_VALID_TIME_CLASSES: frozenset[str] = frozenset(
    {"event_time", "calendar_time", "as_of_time", "state_time"}
)

# Measure/metric kind and accumulation enums. As with the dimension/time
# enums above, an unknown value would silently fall through to default
# behavior (additive semantics, empty expression) — fail loudly instead.
_VALID_MEASURE_KINDS: frozenset[str] = frozenset({"aggregate", "entity_count"})
_VALID_METRIC_KINDS: frozenset[str] = frozenset(
    {
        "aggregate",
        "semi_additive",
        "ratio",
        "cumulative",
        "rolling",
        "prior_period",
        "period_to_date",
        "derived",
        "conversion",
    }
)
_VALID_ACCUMULATION_KINDS: frozenset[str] = frozenset({"event", "flow", "stock", "population"})

# Allowed-key sets for every authored spec shape. The loader ignores keys
# it does not read, so a typo'd key (`agg:` for `default_agg:`) silently
# changes behavior — the same "worst class of authoring bug" the enum
# checks above exist for. Each set is the union of every key the loader
# (config.py / config_parts/package_loader.py) actually reads for that
# shape, plus the `id`/`as`/`name` override escape hatches.
_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "package",
        "defaults",
        "graph",
        "models",
        "metrics",
        "segments",
        "relations",
        "semantic_policies",
        "semantic_caveats",
        "aggregate_relations",
        "path_policy",
        "path_preferences",
        "examples",
        "tests",
    }
)
_PACKAGE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "namespace",
        "name",
        "description",
        "warehouse",
        "default_db",
        "schema_strict",
        "environments",
        "seed",
        "connection",
        "planner",
    }
)
_SEED_KEYS: frozenset[str] = frozenset({"kind", "source", "post_sql", "null_strings"})
_GRAPH_KEYS: frozenset[str] = frozenset(
    {"entities", "relationships", "path_policy", "path_preferences"}
)
_GRAPH_ENTITY_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "key",
        "kind",
        "model",
        "synonyms",
        "description",
        "topics",
        "allowed_as_root",
        "freshness_source",
        "freshness_sla_seconds",
        "freshness_as_of",
        "disallowed_names",
    }
)
_MODEL_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "description",
        "kind",
        "relation",
        "relation_ref",
        "grain",
        "keys",
        "entity",
        "entities",
        "times",
        "dimensions",
        "measures",
        "joins",
        "meta",
        "topics",
        "operational_defaults",
        "default_time",
        "calendar_id",
        "freshness_source",
        "freshness_sla_seconds",
        "freshness_as_of",
        "defaults",
        "time_entity",
        "time_column",
        "default_variant",
        "variants",
    }
)
_MODEL_VARIANT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "relation",
        "inherits_from",
        "grain",
        "time",
        "time_grain",
        "time_column",
        "temporal_role",
        "covers",
        "excludes",
        "columns",
        "eligible_time_grains",
        "selection",
        "equivalence",
        "source",
        "description",
        "freshness_source",
        "freshness_sla_seconds",
        "freshness_as_of",
    }
)
_MODEL_VARIANT_GRAIN_KEYS: frozenset[str] = frozenset({"time", "entities"})
_MODEL_VARIANT_TIME_KEYS: frozenset[str] = frozenset({"role", "column"})
_MODEL_VARIANT_EXCLUDES_KEYS: frozenset[str] = frozenset({"entities", "dimensions", "measures"})
_MODEL_VARIANT_SELECTION_KEYS: frozenset[str] = frozenset({"priority", "prefer_for_grains"})
_MODEL_VARIANT_EQUIVALENCE_KEYS: frozenset[str] = frozenset({"kind", "baseline"})
_MODEL_ENTITY_REF_KEYS: frozenset[str] = frozenset({"expr", "label"})
_DIMENSION_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "kind",
        "type",
        "column",
        "domain",
        "valid_values",
        "value_domain_id",
        "synonyms",
        "description",
        "topics",
        "preferred_filter_ops",
        "sample_values_strategy",
        "filterable",
        "groupable",
    }
)
_TIME_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "dimension_id",
        "name",
        "label",
        "column",
        "kind",
        "class",
        "temporal_class",
        "description",
        "topics",
        "preferred_filter_ops",
        "sample_values_strategy",
        "filterable",
        "groupable",
        "supported_grains",
        "default_query_axis",
        "default",
        "timezone",
        "column_timezone",
    }
)
_MEASURE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "kind",
        "expr",
        "aggregation",
        "default_agg",
        "disallowed_aggregations",
        "suggested_aggregations",
        "rollup",
        "entity_key",
        "times",
        "time",
        "examples",
        "topics",
        "default_temporal_role",
        "operational",
        "subject_entity",
        "aggregation_entity",
        "value_type",
        "currency",
        "synonyms",
        "description",
        "comparison_family",
        "comparison_mode",
        "comparison_peers",
        "clock_variants",
        "preferred_companion_metrics",
        "meta",
        "validity_windows",
        "external_discontinuities",
        "cross_window_policy",
        "accumulation",
        "snapshot_policy",
        "publish",
        # `primitive:` is rejected by the loader with a dedicated migration
        # message — listed here so that message fires instead of a generic
        # unknown-key error.
        "primitive",
    }
)
_JOIN_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "to",
        "via",
        "target",
        "source_key_role",
        "target_key_role",
        "cardinality",
        "safety",
        "name",
        "label",
        "description",
        "path_preference",
        "traversal",
        "allowed_directions",
        "temporal_validity",
        "target_key_type",
        "join_semantics",
        "rollup_safe_aggregations",
        "rollup_safe_aggregations_reverse",
        "rollup_safe",
        "entities",
    }
)
_METRIC_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "description",
        "kind",
        "measure",
        "aggregation",
        "numerator",
        "denominator",
        "null_behavior",
        "window",
        "offset",
        "period",
        "partition_by",
        "order_by",
        "expression",
        "temporal_role",
        "time",
        "compatible_temporal_roles",
        "topics",
        "comparison_family",
        "comparison_mode",
        "comparison_peers",
        "clock_variants",
        "preferred_companion_metrics",
        "operational",
        "meta",
        "examples",
        "value_type",
        "currency",
        "primitive",
    }
)
_SEGMENT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "description",
        "entity",
        "basis_metric",
        "metric",
        "membership",
        "preview_dimensions",
        "synonyms",
        "topics",
    }
)
# The keys the loader reads from a segment's `membership:` block.
_SEGMENT_MEMBERSHIP_KEYS: frozenset[str] = frozenset(
    {"where", "metric_filters", "time", "temporal_role_overrides", "path_policy"}
)
# Membership spellings the loader doesn't read, from other tools or a singular typo,
# and the membership key that holds such conditions.
_SEGMENT_MEMBERSHIP_ALIASES: dict[str, str] = {
    "dimension_filter": "where",
    "dimension_filters": "where",
    "filter": "where",
    "filters": "where",
    "metric_filter": "metric_filters",
}

# Fields each metric kind requires when the expression AST is not
# authored directly. Keeps the "metric produced no expression" error
# able to say exactly what is missing.
_METRIC_KIND_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "aggregate": ("measure",),
    "semi_additive": ("measure",),
    "cumulative": ("measure",),
    "rolling": ("measure",),
    "prior_period": ("measure",),
    "period_to_date": ("measure",),
    "ratio": ("numerator", "denominator"),
    "derived": ("expression",),
    "conversion": ("expression",),
}


def _unknown_key_errors(
    spec: dict[str, Any],
    allowed: frozenset[str],
    *,
    label: str,
    errors: list[str],
    fuzzy_only: bool = False,
) -> None:
    """Flag authored keys the loader would silently ignore.

    With ``fuzzy_only`` (used for the document top level, where extra
    keys are tolerated as annotation blocks — e.g. the capabilities
    reference artifact), only keys that closely match an allowed key are
    flagged: those are near-certain typos, not annotations."""
    from difflib import get_close_matches

    unknown = sorted(
        str(key) for key in spec if str(key) not in allowed and not str(key).startswith("_")
    )
    for key in unknown:
        hints = get_close_matches(key, sorted(allowed), n=2, cutoff=0.6)
        if fuzzy_only and not hints:
            continue
        if hints:
            hint = f"; did you mean {' or '.join(repr(h) for h in hints)}?"
        else:
            hint = f". Allowed keys: {', '.join(sorted(allowed))}"
        add_error(
            errors,
            f"{label} has unknown key {key!r} — unknown keys are ignored by the "
            f"loader, so this would silently change behavior{hint}",
        )


def _expect_value_list(value: Any, *, label: str, errors: list[str]) -> None:
    """Reject scalar values where a list is required. A scalar string is
    the dangerous case: `list("new")` iterates characters, silently
    turning `domain: new` into the value set ['n', 'e', 'w']."""
    if value is None or isinstance(value, list):
        return
    example = f"[{value}]" if not isinstance(value, (dict, bool)) else "[...]"
    add_error(
        errors,
        f"{label} must be a list (got {type(value).__name__} {value!r}); write it as {example}",
    )


def _check_model_shape(
    model_id: str,
    model: dict[str, Any],
    *,
    path_label: str,
    errors: list[str],
    graph_entities: dict[str, Any] | None = None,
) -> None:
    """Authoring-shape checks for one model: unknown keys, list-typed
    fields, and grain ↔ entity-key consistency."""
    label = f"{path_label}: model '{model_id}'"
    _unknown_key_errors(model, _MODEL_KEYS, label=label, errors=errors)

    entities_block = model.get("entities")
    if isinstance(entities_block, dict):
        for ent_name, ent_raw in entities_block.items():
            if str(ent_name) == "bridge":
                continue
            if isinstance(ent_raw, dict):
                _unknown_key_errors(
                    ent_raw,
                    _MODEL_ENTITY_REF_KEYS,
                    label=f"{label} entities entry '{ent_name}'",
                    errors=errors,
                )

    for dim_key, dim_raw in (model.get("dimensions") or {}).items():
        if not isinstance(dim_raw, dict):
            continue
        dim_label = f"{label} dimension '{dim_key}'"
        _unknown_key_errors(dim_raw, _DIMENSION_KEYS, label=dim_label, errors=errors)
        for field in ("domain", "valid_values"):
            if field in dim_raw:
                _expect_value_list(dim_raw.get(field), label=f"{dim_label} {field}", errors=errors)

    for time_key, time_raw in (model.get("times") or {}).items():
        if not isinstance(time_raw, dict):
            continue
        time_label = f"{label} times entry '{time_key}'"
        _unknown_key_errors(time_raw, _TIME_KEYS, label=time_label, errors=errors)
        if "supported_grains" in time_raw:
            _expect_value_list(
                time_raw.get("supported_grains"),
                label=f"{time_label} supported_grains",
                errors=errors,
            )

    for measure_key, measure_raw in (model.get("measures") or {}).items():
        if not isinstance(measure_raw, dict):
            continue
        measure_label = f"{label} measure '{measure_key}'"
        _unknown_key_errors(measure_raw, _MEASURE_KEYS, label=measure_label, errors=errors)
        kind_value = str(measure_raw.get("kind", "") or "").strip().lower()
        if kind_value and kind_value not in _VALID_MEASURE_KINDS:
            add_error(
                errors,
                f"{measure_label} has unknown kind {kind_value!r}. Valid kinds: "
                f"{', '.join(sorted(_VALID_MEASURE_KINDS))}.",
            )
        accumulation = measure_raw.get("accumulation")
        acc_kind = ""
        if isinstance(accumulation, dict):
            acc_kind = str(accumulation.get("kind", "") or "").strip().lower()
        elif accumulation is not None:
            acc_kind = str(accumulation or "").strip().lower()
        if acc_kind and acc_kind not in _VALID_ACCUMULATION_KINDS:
            add_error(
                errors,
                f"{measure_label} has unknown accumulation kind {acc_kind!r}. Valid "
                f"kinds: {', '.join(sorted(_VALID_ACCUMULATION_KINDS))}.",
            )

    for join_key, join_raw in (model.get("joins") or {}).items():
        if isinstance(join_raw, dict):
            _unknown_key_errors(
                join_raw,
                _JOIN_KEYS,
                label=f"{label} join '{join_key}'",
                errors=errors,
            )

    variants = model.get("variants")
    if isinstance(variants, dict):
        for variant_key, variant_raw in variants.items():
            if not isinstance(variant_raw, dict):
                add_error(
                    errors,
                    f"{label} variant '{variant_key}' must be a mapping",
                )
                continue
            variant_label = f"{label} variant '{variant_key}'"
            _unknown_key_errors(
                variant_raw, _MODEL_VARIANT_KEYS, label=variant_label, errors=errors
            )
            for nested_key, allowed in (
                ("grain", _MODEL_VARIANT_GRAIN_KEYS),
                ("time", _MODEL_VARIANT_TIME_KEYS),
                ("excludes", _MODEL_VARIANT_EXCLUDES_KEYS),
                ("selection", _MODEL_VARIANT_SELECTION_KEYS),
                ("equivalence", _MODEL_VARIANT_EQUIVALENCE_KEYS),
            ):
                nested = variant_raw.get(nested_key)
                if isinstance(nested, dict):
                    _unknown_key_errors(
                        nested,
                        allowed,
                        label=f"{variant_label} {nested_key}",
                        errors=errors,
                    )
            grain = variant_raw.get("grain")
            if isinstance(grain, dict) and "entities" in grain:
                _expect_value_list(
                    grain.get("entities"), label=f"{variant_label} grain.entities", errors=errors
                )
            excludes = variant_raw.get("excludes")
            if isinstance(excludes, dict):
                for field in ("entities", "dimensions", "measures"):
                    if field in excludes:
                        _expect_value_list(
                            excludes.get(field),
                            label=f"{variant_label} excludes.{field}",
                            errors=errors,
                        )
            if "eligible_time_grains" in variant_raw:
                _expect_value_list(
                    variant_raw.get("eligible_time_grains"),
                    label=f"{variant_label} eligible_time_grains",
                    errors=errors,
                )

    # Grain ↔ entity-key consistency. When a model authors an `entities:`
    # block plus an explicit `grain:`, the loader auto-detects the primary
    # entity by matching grain columns against each entity's key (or
    # `expr:` override). A grain that matches nothing silently falls back
    # to the first entity — almost always a typo'd column name.
    model_kind = str(model.get("kind", "model") or "model").strip().lower()
    if (
        model_kind == "model"
        and isinstance(entities_block, dict)
        and graph_entities is not None
        and model.get("grain") is not None
    ):
        grain_raw = model.get("grain")
        grain_cols = [str(c) for c in (grain_raw if isinstance(grain_raw, list) else [grain_raw])]
        candidates: dict[str, list[str]] = {}
        for ent_name, ent_raw in entities_block.items():
            if str(ent_name) == "bridge":
                continue
            ent_spec = ent_raw if isinstance(ent_raw, dict) else {}
            expr_override = ent_spec.get("expr")
            if expr_override is not None:
                cols = expr_override if isinstance(expr_override, list) else [expr_override]
                candidates[str(ent_name)] = [str(c) for c in cols]
            else:
                graph_ent = graph_entities.get(str(ent_name))
                ent_key = graph_ent.get("key") if isinstance(graph_ent, dict) else None
                if isinstance(ent_key, list):
                    candidates[str(ent_name)] = [str(c) for c in ent_key]
                elif ent_key:
                    candidates[str(ent_name)] = [str(ent_key)]
        if (
            grain_cols
            and candidates
            and not any(cols == grain_cols for cols in candidates.values())
        ):
            rendered = ", ".join(
                f"{name}={'/'.join(cols)}" for name, cols in sorted(candidates.items())
            )
            add_error(
                errors,
                f"{label} grain {grain_cols} does not match the key of any entity "
                f"in its entities: block ({rendered}) — primary-entity detection "
                f"would silently fall back to the first entry; fix the grain or "
                f"the entity key",
            )


def _check_typed_field_enums(path_label: str, models: dict[str, Any], errors: list[str]) -> None:
    """Enum checks for typed fields on models (dimension kinds, time kinds
    and classes). Shared by the directory and single-file validators —
    a typo'd enum value silently falls through to default behavior."""
    for model_id, model in models.items():
        if not isinstance(model, dict):
            continue
        # dimensions: declared kinds are user-facing and route to
        # _map_dimension_kind. Unknown values fall through to "string".
        for dim_key, dim_raw in (model.get("dimensions") or {}).items():
            if not isinstance(dim_raw, dict):
                continue
            kind_value = dim_raw.get("kind", dim_raw.get("type"))
            if kind_value is None:
                continue
            kind_str = str(kind_value).strip().lower()
            if not kind_str:
                continue
            if kind_str not in _VALID_DIMENSION_KINDS and kind_str not in {
                "date",
                "timestamp",
                "datetime",
                "time",
            }:
                add_error(
                    errors,
                    f"{path_label}: dimension {model_id}.{dim_key} has unknown kind "
                    f"{kind_value!r}. Valid kinds: "
                    f"{', '.join(sorted(_VALID_DIMENSION_KINDS))}.",
                )
        # times: kinds are date/timestamp only (the times block describes
        # a temporal column).
        for time_key, time_raw in (model.get("times") or {}).items():
            if not isinstance(time_raw, dict):
                continue
            kind_value = time_raw.get("kind")
            if kind_value is None:
                continue
            kind_str = str(kind_value).strip().lower()
            if not kind_str:
                continue
            if kind_str not in _VALID_TIME_KINDS:
                add_error(
                    errors,
                    f"{path_label}: times {model_id}.{time_key} has unknown kind "
                    f"{kind_value!r}. Valid kinds: "
                    f"{', '.join(sorted(_VALID_TIME_KINDS))}.",
                )
            class_value = time_raw.get("class", time_raw.get("temporal_class"))
            if class_value is not None:
                class_str = str(class_value).strip().lower()
                if class_str and class_str not in _VALID_TIME_CLASSES:
                    add_error(
                        errors,
                        f"{path_label}: times {model_id}.{time_key} has unknown class "
                        f"{class_value!r}. Valid classes: "
                        f"{', '.join(sorted(_VALID_TIME_CLASSES))}.",
                    )


def _check_metric_shape(
    metric_key: str, spec: dict[str, Any], *, path_label: str, errors: list[str]
) -> None:
    label = f"{path_label}: metric '{metric_key}'"
    _unknown_key_errors(spec, _METRIC_KEYS, label=label, errors=errors)
    kind = str(spec.get("kind", "") or "").strip().lower()
    if kind and kind not in _VALID_METRIC_KINDS:
        add_error(
            errors,
            f"{label} has unknown kind {kind!r}. Valid kinds: "
            f"{', '.join(sorted(_VALID_METRIC_KINDS))}.",
        )
        return
    if spec.get("expression"):
        return
    required = _METRIC_KIND_REQUIRED_FIELDS.get(kind or "derived", ())
    missing = [field for field in required if spec.get(field) in (None, "", {})]
    if missing:
        add_error(
            errors,
            f"{label} (kind '{kind or 'derived'}') is missing required field"
            f"{'s' if len(missing) > 1 else ''}: {', '.join(missing)}",
        )


def _check_metric(
    metric_key: str,
    spec: dict[str, Any],
    strict: bool,
    *,
    path_label: str,
    errors: list[str],
) -> None:
    """Shape checks, then the schema_strict rule that every metric declares value_type.

    The loader defaults a missing or empty value_type to "number", so the loaded config
    can't tell an authored "number" from the default; only the authored spec can. This
    runs on the specs of every layout the loader reads, single-file included.
    """
    _check_metric_shape(metric_key, spec, path_label=path_label, errors=errors)
    if not strict:
        return
    value_type = spec.get("value_type")
    if not (isinstance(value_type, str) and value_type.strip()):
        add_error(
            errors,
            f"{path_label}: metric {metric_key!r}: missing 'value_type:'. Under schema_strict, "
            f"declare value_type explicitly (common values: number, percent, currency, count, "
            f"ratio).",
        )


def _check_segment_shape(
    segment_key: str, spec: dict[str, Any], *, path_label: str, errors: list[str]
) -> None:
    label = f"{path_label}: segment '{segment_key}'"
    membership_spellings = _SEGMENT_MEMBERSHIP_KEYS | _SEGMENT_MEMBERSHIP_ALIASES.keys()
    for key in sorted(membership_spellings & set(spec)):
        add_error(errors, f"{label} has {key!r} outside membership: — {_membership_fix(key)}")
    top_level = {key: value for key, value in spec.items() if key not in membership_spellings}
    if "meta" in top_level:
        # The fuzzy match would suggest `metric`, the legacy alias of basis_metric.
        del top_level["meta"]
        add_error(errors, f"{label} has unknown key 'meta' — segments don't read meta:; remove it")
    _unknown_key_errors(top_level, _SEGMENT_KEYS, label=label, errors=errors)
    membership = spec.get("membership")
    if not isinstance(membership, dict):
        return
    for key in sorted(_SEGMENT_MEMBERSHIP_ALIASES.keys() & set(membership)):
        add_error(errors, f"{label} membership has unknown key {key!r} — {_membership_fix(key)}")
    rest = {
        key: value for key, value in membership.items() if key not in _SEGMENT_MEMBERSHIP_ALIASES
    }
    _unknown_key_errors(rest, _SEGMENT_MEMBERSHIP_KEYS, label=f"{label} membership", errors=errors)


def _membership_fix(key: str) -> str:
    """Where the loader reads what a segment author wrote under ``key``."""
    target = _SEGMENT_MEMBERSHIP_ALIASES.get(key, key)
    rows = " as {field, op, value} rows" if target == "where" and key != target else ""
    return (
        f"the loader reads membership.{target} only, so the segment ignores it; "
        f"write it under membership.{target}{rows}"
    )


def _check_package_shapes(
    raw: dict[str, Any], *, path_label: str, errors: list[str], top_level: bool = True
) -> None:
    """Authoring-shape checks shared by single-file and directory packages:
    unknown keys and wrong-typed fields across every authored block."""
    if top_level:
        # Fuzzy-only at the document top level: unmatched extra keys are
        # tolerated as annotation blocks (the capabilities reference
        # artifact relies on this); near-matches are near-certain typos.
        _unknown_key_errors(raw, _TOP_LEVEL_KEYS, label=path_label, errors=errors, fuzzy_only=True)

    package = raw.get("package")
    if isinstance(package, dict):
        _unknown_key_errors(
            package, _PACKAGE_KEYS, label=f"{path_label}: package block", errors=errors
        )
        _expect_value_list(
            package.get("environments"),
            label=f"{path_label}: package.environments",
            errors=errors,
        )
        seed = package.get("seed")
        if isinstance(seed, dict):
            _unknown_key_errors(
                seed, _SEED_KEYS, label=f"{path_label}: package.seed", errors=errors
            )

    graph = raw.get("graph")
    graph_entities: dict[str, Any] = {}
    if isinstance(graph, dict):
        _unknown_key_errors(graph, _GRAPH_KEYS, label=f"{path_label}: graph block", errors=errors)
        entities = graph.get("entities")
        if isinstance(entities, dict):
            graph_entities = entities
            for entity_key, entity_raw in entities.items():
                if isinstance(entity_raw, dict):
                    _unknown_key_errors(
                        entity_raw,
                        _GRAPH_ENTITY_KEYS,
                        label=f"{path_label}: graph entity '{entity_key}'",
                        errors=errors,
                    )

    models = raw.get("models")
    if isinstance(models, dict):
        for model_id, model in models.items():
            if isinstance(model, dict):
                _check_model_shape(
                    str(model_id),
                    model,
                    path_label=path_label,
                    errors=errors,
                    graph_entities=graph_entities,
                )

    try:
        strict = bool(dict(package or {}).get("schema_strict", False))
    except (TypeError, ValueError):
        strict = False  # The loader reports an invalid package block.
    metrics = raw.get("metrics")
    if strict and not isinstance(metrics, dict):
        try:
            metrics = dict(metrics or {})
        except (TypeError, ValueError):
            add_error(errors, f"{path_label}: metrics block must be a mapping or key/value pairs")
            metrics = {}
    if isinstance(metrics, dict):
        for metric_key, spec in metrics.items():
            if strict and not isinstance(spec, dict):
                try:
                    spec = dict(spec or {})
                except (TypeError, ValueError):
                    add_error(
                        errors,
                        f"{path_label}: metric {metric_key!r} must be a mapping or key/value pairs",
                    )
                    continue
            if isinstance(spec, dict):
                _check_metric(str(metric_key), spec, strict, path_label=path_label, errors=errors)

    segments = raw.get("segments")
    if isinstance(segments, dict):
        for segment_key, spec in segments.items():
            if isinstance(spec, dict):
                _check_segment_shape(str(segment_key), spec, path_label=path_label, errors=errors)
