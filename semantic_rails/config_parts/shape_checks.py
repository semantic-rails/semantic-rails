"""Raw-YAML key allowlists and the authoring check every file-based package load runs."""

from __future__ import annotations

from typing import Any

from ..naming import slug
from .package_loader import _RELATIONSHIP_PASSTHROUGH, _column_list


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
_VALID_MEASURE_KINDS: frozenset[str] = frozenset({"aggregate", "entity_count", "lookup"})
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
# What the loader reads from an `accumulation:` block (config.py `_normalize_accumulation`).
_ACCUMULATION_KEYS: frozenset[str] = frozenset({"kind", "snapshot"})
# The loader reads any other snapshot as end_of_period, so a typo would read the closing balance.
_VALID_ACCUMULATION_SNAPSHOTS: frozenset[str] = frozenset({"start_of_period", "end_of_period"})
_REPLACES_DEFAULT = "a measure's `accumulation:` replaces `defaults.measure.accumulation` whole"

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
        "path_policy",
        "path_preferences",
        "examples",
        "tests",
    }
)
_UPGRADE = "(`semantic-rails project upgrade` rewrites it)"
_AS = f"the key derives the id; write `as:` only to keep a public id {_UPGRADE}"
# A key a release retired from a block, and where its meaning is authored now: the one hint
# the unknown-key error adds, by (block, key).
_REPLACED_KEYS: dict[tuple[str, str], str] = {
    ("document", "aggregate_relations"): "declare rollups under the model's `variants:`",
    ("package", "schema_strict"): f"strict is the only profile; delete this line {_UPGRADE}",
    ("entity", "id"): _AS,
    ("dimension", "id"): _AS,
    ("measure", "id"): _AS,
    ("model", "grain"): "the entity's key keys the model's rows; key finer rows as their "
    f"own entity, related to it {_UPGRADE}",
    ("model", "joins"): f"write each join as a `graph.relationships` row {_UPGRADE}",
}
_PACKAGE_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "namespace",
        "name",
        "description",
        "warehouse",
        "default_db",
        "environments",
        "seed",
        "connection",
        "planner",
    }
)
_SEED_KEYS: frozenset[str] = frozenset({"kind", "source", "post_sql", "null_strings"})
_DEFAULTS_KEYS: frozenset[str] = frozenset(
    {"dimension", "time", "measure", "relationship", "operational", "meta", "observation_scope"}
)
_GRAPH_KEYS: frozenset[str] = frozenset(
    {"entities", "relationships", "path_policy", "path_preferences"}
)
_PATH_POLICY_KEYS: frozenset[str] = frozenset({"max_hops"})
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
        "traversal",
        "allowed_directions",
        "temporal_validity",
        "target_key_type",
        "join_semantics",
        "rollup_safe_aggregations_reverse",
        "entities",
    }
)
# What the loader reads from a `graph.relationships` entry when it projects it onto a join.
_GRAPH_RELATIONSHIP_KEYS: frozenset[str] = frozenset(_RELATIONSHIP_PASSTHROUGH) | {
    "id",
    "as",
    "entities",
    "cardinality",
    "allowed_directions",
    "rollup_safe",
}
_ROLLUP_SAFE_KEYS: frozenset[str] = frozenset({"reverse"})
_CAVEAT_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "kind",
        "message",
        "object_ids",
        "entity_values",
        "time",
        "audiences",
        "environments",
        "severity",
        "owner",
        "references",
    }
)
_CAVEAT_TIME_KEYS: frozenset[str] = frozenset({"at", "from", "to"})
_GRAPH_ENTITY_KEYS: frozenset[str] = frozenset(
    {
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
        "keys",
        "entity",
        "entities",
        "times",
        "dimensions",
        "measures",
        "meta",
        "topics",
        "operational_defaults",
        "calendar_id",
        "freshness_source",
        "freshness_sla_seconds",
        "freshness_as_of",
        "time_entity",
        "time_column",
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
_MODEL_VARIANT_SELECTION_KEYS: frozenset[str] = frozenset({"priority"})
_MODEL_VARIANT_EQUIVALENCE_KEYS: frozenset[str] = frozenset({"kind"})
# A rollup's `columns:` entry for a measure or a dimension; see acceleration/selection.py.
_MEASURE_BINDING_KEYS: frozenset[str] = frozenset({"column", "rollup", "aggregation", "holds"})
_DIMENSION_BINDING_KEYS: frozenset[str] = frozenset({"column", "path"})
_MODEL_ENTITY_REF_KEYS: frozenset[str] = frozenset({"expr", "label"})
_DIMENSION_KEYS: frozenset[str] = frozenset(
    {
        "as",
        "name",
        "label",
        "kind",
        "column",
        "domain",
        "valid_values",
        "value_domain_id",
        "synonyms",
        "description",
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
        "description",
        "topics",
        "sample_values_strategy",
        "filterable",
        "groupable",
        "supported_grains",
        "default",
        "timezone",
        "column_timezone",
    }
)
_MEASURE_KEYS: frozenset[str] = frozenset(
    {
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
        "additive",
        "entity_key",
        "times",
        "time",
        "examples",
        "default_temporal_role",
        "operational",
        "value_type",
        "currency",
        "synonyms",
        "description",
        "comparison_family",
        "comparison_mode",
        "meta",
        "validity_windows",
        "external_discontinuities",
        "cross_window_policy",
        "accumulation",
        "publish",
        "from",
        "via",
    }
)
_METRIC_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "as",
        "name",
        "label",
        "synonyms",
        "description",
        "kind",
        "measure",
        "aggregation",
        "numerator",
        "denominator",
        "window",
        "window_scope",
        "offset",
        "period",
        "partition_by",
        "order_by",
        "expression",
        "temporal_role",
        "compatible_temporal_roles",
        "comparison_family",
        "comparison_mode",
        "preferred_companion_metrics",
        "operational",
        "meta",
        "examples",
        "value_type",
        "currency",
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
        "membership",
        "preview_dimensions",
        "synonyms",
    }
)
# The keys the loader reads from a segment's `membership:` block.
_SEGMENT_MEMBERSHIP_KEYS: frozenset[str] = frozenset(
    {"where", "metric_filters", "time", "temporal_role_overrides"}
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
    block: str = "",
) -> None:
    """Flag authored keys the loader would silently ignore; keys starting with ``_`` are
    annotations. A key ``_REPLACED_KEYS`` lists for ``block`` names its current form."""
    from difflib import get_close_matches

    unknown = sorted(
        str(key) for key in spec if str(key) not in allowed and not str(key).startswith("_")
    )
    for key in unknown:
        hints = get_close_matches(key, sorted(allowed), n=2, cutoff=0.6)
        if (block, key) in _REPLACED_KEYS:
            hint = f"; {_REPLACED_KEYS[block, key]}"
        elif hints:
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


def _key_columns(model_id: str, model: dict[str, Any], graph_entities: dict[str, Any]) -> set[str]:
    """The key and foreign-key columns the loader turns into ``kind: id`` dimensions."""
    entities = model.get("entities")
    names = {
        *(str(name) for name in (entities if isinstance(entities, dict) else ())),
        str(model.get("entity", "") or ""),
        *(
            str(name)
            for name, entity in graph_entities.items()
            if isinstance(entity, dict) and str(entity.get("model", "") or "").strip() == model_id
        ),
    }
    # A graph entity without `model:` binds the model of its own name.
    named = graph_entities.get(model_id)
    if model_id in graph_entities and not (isinstance(named, dict) and named.get("model")):
        names.add(model_id)
    columns: set[str] = set()
    for name in names - {"", "bridge"}:
        override = entities.get(name) if isinstance(entities, dict) else None
        if isinstance(override, dict) and override.get("expr") is not None:
            columns.update(_column_list(override["expr"]))
        entity = graph_entities.get(name)
        if isinstance(entity, dict):
            columns.update(_column_list(entity.get("key")))
    keys = model.get("keys")
    if isinstance(keys, dict):
        columns.update(_column_list(keys.get("primary")))
        foreign = keys.get("foreign")
        for spec in foreign.values() if isinstance(foreign, dict) else ():
            columns.update(_column_list(spec))
    return columns


def _binding_names(
    model_id: str, model: dict[str, Any], graph_entities: dict[str, Any], namespace: str
) -> tuple[set[str], set[str]]:
    """The measure and dimension names a rollup ``columns:`` entry can bind, as the loader reads
    them: a row by its key or by the one id the loader gives it — ``as:`` when set, else ``id:``,
    else (a measure) the id the namespace gives it — and a key or foreign-key column the loader
    turns into a key dimension. An ``id:`` that ``as:`` replaces names nothing."""
    found = []
    for block in ("measures", "dimensions"):
        rows = model.get(block)
        rows = rows if isinstance(rows, dict) else {}
        names = {*map(str, rows)}
        for key, row in rows.items():
            spec = row if isinstance(row, dict) else {}
            resolved = next(
                (text for field in ("as", "id") if (text := str(spec.get(field) or "").strip())),
                f"measure.{namespace}.{slug(str(key))}" if block == "measures" else "",
            )
            if resolved:
                names.add(resolved)
        found.append(names)
    measures, dimensions = found
    return measures, dimensions | _key_columns(model_id, model, graph_entities)


def _unbound_column_error(label: str, name: str, names: tuple[set[str], set[str]]) -> str:
    """A rollup ``columns:`` entry the loader reads for nothing, and the names it could mean."""
    from difflib import get_close_matches

    candidates = sorted(names[0] | names[1])
    hints = get_close_matches(name, candidates, n=3, cutoff=0.6)
    fix = (
        f"did you mean {' or '.join(repr(hint) for hint in hints)}?"
        if hints
        else f"bind one of: {', '.join(candidates) or 'none (the model has no measures or dimensions)'}"
    )
    return (
        f"{label} names no measure, dimension or key column of the model — it is ignored by "
        f"the loader, so this would silently change behavior; {fix}"
    )


def _binding_keys(name: str, measures: set[str], dimensions: set[str]) -> frozenset[str] | None:
    """The keys a rollup ``columns:`` entry may hold: a measure's binding, a dimension's, or what
    both take when the name is both; ``None`` when the name binds neither. A ``dimension.`` name
    the model lacks is left to the loader, which refuses a dimension it doesn't know."""
    allowed: frozenset[str] | None = None
    for names, prefix, keys in (
        (measures, None, _MEASURE_BINDING_KEYS),
        (dimensions, "dimension.", _DIMENSION_BINDING_KEYS),
    ):
        if name in names or (prefix and name.startswith(prefix)):
            allowed = keys if allowed is None else allowed & keys
    return allowed


def _check_model_shape(
    model_id: str,
    model: dict[str, Any],
    *,
    path_label: str,
    errors: list[str],
    graph_entities: dict[str, Any] | None = None,
    namespace: str,
    default_accumulation: Any = None,
) -> None:
    """Authoring-shape checks for one model: unknown keys, list-typed fields, and the
    entities: block in place of keys and a singular entity."""
    label = f"{path_label}: model '{model_id}'"
    _unknown_key_errors(model, _MODEL_KEYS, label=label, errors=errors, block="model")
    keys: dict[str, Any] = model["keys"] if isinstance(model.get("keys"), dict) else {}
    for problem, current in (
        ("entity" in model and "entities" not in model, "a singular 'entity:'"),
        ("foreign" in keys, "'keys.foreign:'"),
        ("entities" in model and "primary" in keys, "'keys.primary:' beside 'entities:'"),
    ):
        if problem:
            add_error(
                errors,
                f"{label} authors {current}; list the model's entities under 'entities:' "
                f"{_UPGRADE}",
            )

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
        _unknown_key_errors(
            dim_raw, _DIMENSION_KEYS, label=dim_label, errors=errors, block="dimension"
        )
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
        _unknown_key_errors(
            measure_raw, _MEASURE_KEYS, label=measure_label, errors=errors, block="measure"
        )
        _check_publish(measure_raw, label=measure_label, errors=errors)
        kind_value = str(measure_raw.get("kind", "") or "").strip().lower()
        if not kind_value:
            add_error(
                errors,
                f"{measure_label} has no 'kind:'; declare one of "
                f"{', '.join(sorted(_VALID_MEASURE_KINDS))}.",
            )
        elif kind_value not in _VALID_MEASURE_KINDS:
            add_error(
                errors,
                f"{measure_label} has unknown kind {kind_value!r}. Valid kinds: "
                f"{', '.join(sorted(_VALID_MEASURE_KINDS))}.",
            )
        if "accumulation" in measure_raw:
            _check_accumulation(
                measure_raw["accumulation"],
                label=measure_label,
                errors=errors,
                default=default_accumulation,
            )

    variants = model.get("variants")
    if isinstance(variants, dict):
        binding_names = _binding_names(model_id, model, graph_entities or {}, namespace)
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
            columns = variant_raw.get("columns")
            for column_key, binding in columns.items() if isinstance(columns, dict) else ():
                column_label = f"{variant_label} column '{column_key}'"
                binding_keys = _binding_keys(str(column_key), *binding_names)
                if binding_keys is None:
                    add_error(
                        errors, _unbound_column_error(column_label, str(column_key), binding_names)
                    )
                elif isinstance(binding, dict):
                    _unknown_key_errors(binding, binding_keys, label=column_label, errors=errors)
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
            kind_value = dim_raw.get("kind")
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
            class_value = time_raw.get("class")
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
    metric_key: str, spec: dict[str, Any], *, path_label: str, errors: list[str]
) -> None:
    """Shape checks, then the rule that every metric declares value_type.

    The loader defaults a missing or empty value_type to "number", so the loaded config
    can't tell an authored "number" from the default; only the authored spec can.
    """
    _check_metric_shape(metric_key, spec, path_label=path_label, errors=errors)
    value_type = spec.get("value_type")
    if not (isinstance(value_type, str) and value_type.strip()):
        add_error(
            errors,
            f"{path_label}: metric {metric_key!r}: missing 'value_type:'; declare it "
            f"(common values: number, percent, currency, count, ratio).",
        )


def _accumulation_parts(accumulation: Any) -> tuple[str, str | None]:
    """The kind and snapshot the loader reads from an ``accumulation:`` value
    (config.py ``_normalize_accumulation``); the snapshot is None when the value sets none."""
    if not isinstance(accumulation, dict):
        return str(accumulation or "").strip().lower(), None
    kind = str(accumulation.get("kind", "") or "").strip().lower()
    if "snapshot" not in accumulation:
        return kind, None
    return kind, str(accumulation["snapshot"] or "").strip().lower()


def _check_accumulation(
    accumulation: Any, *, label: str, errors: list[str], default: Any = None
) -> None:
    """Unknown keys, kind and snapshot of an ``accumulation:`` value, in the mapping or scalar
    form. The loader reads a measure's value in place of ``default`` (the
    ``defaults.measure.accumulation``), not merged with it, so the value must state the kind,
    and the snapshot the default sets, itself."""
    acc_kind, snapshot = _accumulation_parts(accumulation)
    if isinstance(accumulation, dict):
        _unknown_key_errors(
            accumulation, _ACCUMULATION_KEYS, label=f"{label} accumulation", errors=errors
        )
    if acc_kind and acc_kind not in _VALID_ACCUMULATION_KINDS:
        add_error(
            errors,
            f"{label} has unknown accumulation kind {acc_kind!r}. Valid "
            f"kinds: {', '.join(sorted(_VALID_ACCUMULATION_KINDS))}.",
        )
    if snapshot is not None:
        if snapshot not in _VALID_ACCUMULATION_SNAPSHOTS:
            add_error(
                errors,
                f"{label} has unknown accumulation snapshot {snapshot!r}. Valid snapshots: "
                f"{', '.join(sorted(_VALID_ACCUMULATION_SNAPSHOTS))}; {_REPLACES_DEFAULT}.",
            )
        if acc_kind != "stock":
            add_error(
                errors,
                f"{label} accumulation sets 'snapshot:' without 'kind: stock'; only a stock "
                f"reads a snapshot, so write the kind beside it ({_REPLACES_DEFAULT}).",
            )
        return
    if default is None:
        return
    default_kind, default_snapshot = _accumulation_parts(default)
    if not acc_kind and (default_kind or default_snapshot):
        add_error(
            errors,
            f"{label} accumulation names no 'kind:'; {_REPLACES_DEFAULT}, so write the kind "
            "the measure means.",
        )
    elif acc_kind == "stock" and default_snapshot:
        add_error(
            errors,
            f"{label} accumulation names no 'snapshot:' although the default sets "
            f"{default_snapshot!r}; {_REPLACES_DEFAULT}, so write the snapshot the measure means.",
        )


def _check_publish(spec: dict[str, Any], *, label: str, errors: list[str]) -> None:
    """``publish: false`` marks a building-block measure; a measure publishes no metric itself."""
    if "publish" in spec and not isinstance(spec["publish"], bool):
        add_error(
            errors,
            f"{label} publish must be true or false; author the metric under metrics: {_UPGRADE}",
        )


def _check_segment_shape(
    segment_key: str, spec: dict[str, Any], *, path_label: str, errors: list[str]
) -> None:
    label = f"{path_label}: segment '{segment_key}'"
    membership_spellings = _SEGMENT_MEMBERSHIP_KEYS | _SEGMENT_MEMBERSHIP_ALIASES.keys()
    for key in sorted(membership_spellings & set(spec)):
        add_error(errors, f"{label} has {key!r} outside membership: — {_membership_fix(key)}")
    top_level = {key: value for key, value in spec.items() if key not in membership_spellings}
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


def authoring_errors(raw: dict[str, Any], *, path_label: str) -> list[str]:
    """Every authoring error in a package as authored, in any layout. Loading refuses a package
    with any, so a key the loader would ignore never changes what it serves."""
    errors: list[str] = []
    _check_package_shapes(raw, path_label=path_label, errors=errors)
    models = raw.get("models")
    if isinstance(models, dict):
        _check_typed_field_enums(path_label, models, errors)
    return errors


def _check_package_shapes(raw: dict[str, Any], *, path_label: str, errors: list[str]) -> None:
    """Unknown keys and wrong-typed fields across every authored block."""
    _unknown_key_errors(raw, _TOP_LEVEL_KEYS, label=path_label, errors=errors, block="document")
    defaults = raw.get("defaults")
    if isinstance(defaults, dict):
        label = f"{path_label}: defaults"
        _unknown_key_errors(defaults, _DEFAULTS_KEYS, label=label, errors=errors)
        for key, allowed in (
            ("dimension", _DIMENSION_KEYS),
            ("time", _TIME_KEYS),
            ("measure", _MEASURE_KEYS),
            ("relationship", _JOIN_KEYS),
        ):
            if isinstance(defaults.get(key), dict):
                _unknown_key_errors(
                    defaults[key], allowed, label=f"{label}.{key}", errors=errors, block=key
                )
        # Every measure without its own `accumulation:` or `publish:` takes this one.
        measure = defaults.get("measure")
        if isinstance(measure, dict):
            _check_publish(measure, label=f"{label}.measure", errors=errors)
            _check_accumulation(
                measure.get("accumulation"), label=f"{label}.measure", errors=errors
            )
    caveats = raw.get("semantic_caveats")
    for index, row in enumerate(caveats if isinstance(caveats, list) else ()):
        if isinstance(row, dict):
            label = f"{path_label}: caveat {row.get('id', index)!r}"
            _unknown_key_errors(row, _CAVEAT_KEYS, label=label, errors=errors)
            if isinstance(row.get("time"), dict):
                _unknown_key_errors(
                    row["time"], _CAVEAT_TIME_KEYS, label=f"{label} time", errors=errors
                )

    package = raw.get("package")
    if isinstance(package, dict):
        _unknown_key_errors(
            package,
            _PACKAGE_KEYS,
            label=f"{path_label}: package block",
            errors=errors,
            block="package",
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
                        block="entity",
                    )
        relationships = graph.get("relationships")
        for name, row in relationships.items() if isinstance(relationships, dict) else ():
            if isinstance(row, dict):
                label = f"{path_label}: graph relationship '{name}'"
                _unknown_key_errors(row, _GRAPH_RELATIONSHIP_KEYS, label=label, errors=errors)
                if isinstance(row.get("rollup_safe"), dict):
                    _unknown_key_errors(
                        row["rollup_safe"],
                        _ROLLUP_SAFE_KEYS,
                        label=f"{label} rollup_safe",
                        errors=errors,
                    )
    for block, label in ((raw, "path_policy"), (graph, "graph path_policy")):
        policy = block.get("path_policy") if isinstance(block, dict) else None
        if isinstance(policy, dict):
            _unknown_key_errors(
                policy, _PATH_POLICY_KEYS, label=f"{path_label}: {label}", errors=errors
            )

    models = raw.get("models")
    if isinstance(models, dict):
        # The namespace the loader derives ids from (`normalize_package`).
        block = package if isinstance(package, dict) else {}
        namespace = str(block.get("namespace", block.get("id", "")) or "").strip()
        measure_defaults = defaults.get("measure") if isinstance(defaults, dict) else None
        default_accumulation = (
            measure_defaults.get("accumulation") if isinstance(measure_defaults, dict) else None
        )
        for model_id, model in models.items():
            if isinstance(model, dict) and str(model.get("id", model_id)).strip() != str(model_id):
                add_error(
                    errors,
                    f"{path_label}: model '{model_id}' authors id {model['id']!r}; the model's "
                    "key is its id, so delete id:",
                )
            if isinstance(model, dict):
                _check_model_shape(
                    str(model_id),
                    model,
                    path_label=path_label,
                    errors=errors,
                    graph_entities=graph_entities,
                    namespace=namespace,
                    default_accumulation=default_accumulation,
                )

    metrics = raw.get("metrics")
    if not isinstance(metrics, dict):
        try:
            metrics = dict(metrics or {})
        except (TypeError, ValueError):
            add_error(errors, f"{path_label}: metrics block must be a mapping or key/value pairs")
            metrics = {}
    for metric_key, spec in metrics.items():
        if not isinstance(spec, dict):
            try:
                spec = dict(spec or {})
            except (TypeError, ValueError):
                add_error(
                    errors,
                    f"{path_label}: metric {metric_key!r} must be a mapping or key/value pairs",
                )
                continue
        _check_metric(str(metric_key), spec, path_label=path_label, errors=errors)

    segments = raw.get("segments")
    if isinstance(segments, dict):
        for segment_key, spec in segments.items():
            if isinstance(spec, dict):
                _check_segment_shape(str(segment_key), spec, path_label=path_label, errors=errors)
