from __future__ import annotations

from typing import Any

from ..errors import SemanticLayerError
from ..naming import slug as _slug
from ..naming import title as _titleize

# `graph.relationships` keys copied as they are onto the join a relationship projects.
_RELATIONSHIP_PASSTHROUGH = (
    "safety",
    "temporal_validity",
    "target_key_type",
    "join_semantics",
    "label",
    "description",
    "name",
    "via",
    "target",
    "source_key_role",
    "target_key_role",
)


def _with_default(mapping: dict[str, Any], key: str, value: Any) -> None:
    if mapping.get(key) in {None, ""}:
        mapping[key] = value


def _apply_as(mapping: dict[str, Any]) -> None:
    """`as:` keeps a public id the key can't derive (another namespace's, a renamed key's)."""
    if public := str(mapping.get("as") or "").strip():
        mapping["id"] = public


def _column_list(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("columns", value.get("column"))
    if value is None:
        return []
    return [str(col) for col in (value if isinstance(value, list) else [value])]


def _model_mapping(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    models = raw.get("models", {}) or {}
    if isinstance(models, dict):
        return {str(key): dict(value or {}) for key, value in models.items()}
    return {}


def normalize_package(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the ergonomic authoring contract into the canonical
    PackageConfig-shaped dict.

    Fills namespace-derived IDs for fields the author left out. Existing
    explicit values are preserved unchanged.
    """
    out = dict(raw or {})
    package = dict(out.get("package", {}) or {})
    namespace = str(package.get("namespace", package.get("id", "")) or "").strip()
    if namespace:
        package["namespace"] = namespace
    out["package"] = package

    graph = dict(out.get("graph", {}) or {})
    graph_entities = dict(graph.get("entities", {}) or {})
    models = _model_mapping(out)
    bound_entities: dict[str, str] = {}
    for entity_key, entity_raw in graph_entities.items():
        entity = dict(entity_raw or {})
        if entity.get("model"):
            entity["model"] = str(entity["model"]).strip()
            graph_entities[entity_key] = entity
        if not entity.get("model"):
            continue
        model_id = str(entity["model"])
        if model_id in bound_entities:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"model '{model_id}' is the primary home of both graph entities "
                f"'{bound_entities[model_id]}' and '{entity_key}'; bind each to its own model",
            )
        bound_entities[model_id] = str(entity_key)

    # Translate `model.entities:` (the ergonomic authoring block) into the
    # canonical singular `entity:` + `keys.primary:` + `keys.foreign:`
    # shape so the downstream parser stays bijective. The block lists every entity
    # the model exposes; graph model bindings determine the primary entity,
    # otherwise an explicit entity or the model's own name identifies it. Per-entity options:
    #   expr:    column rename when authored column != graph's canonical key
    #   label:   display override (passed through as model_entity_labels meta)
    # Block-level option:
    #   bridge: false   — model is not eligible as a multi-hop join path
    #
    # When `entities:` is authored on a model, it produces:
    #   - `entity:` set to the resolved primary
    #   - `keys.primary:` synthesized from primary's canonical column (with
    #     `expr:` override applied)
    #   - `keys.foreign:` populated with non-primary entries (column = expr
    #     override or canonical key from graph)
    # If `entities:` is absent, the singular-form fields stay untouched.
    for model_id, model in list(models.items()):
        # Fact models declare a single time_entity + time_column join and
        # do NOT translate `model.entities:` → `graph.entities`. They are
        # not reachable via inferred multi-hop joins.
        model_kind = str(model.get("kind", "model") or "model").strip().lower()
        if model_kind == "fact":
            # Drop any informational `entities:` block authored on a fact
            # model — it must not produce a graph-entity translation.
            model.pop("entities", None)
            models[model_id] = model
            continue
        if model_id in bound_entities:
            model["entity"] = bound_entities[model_id]
        entities_block_raw = model.get("entities")
        if not isinstance(entities_block_raw, dict):
            continue
        entities_block = dict(entities_block_raw)
        # Block-level options
        bridge = entities_block.pop("bridge", True)
        # Persist `bridge` as a top-level model flag for the inferred-relationship pass.
        model["_bridge_eligible"] = bool(bridge)
        model["_bridge_declared"] = entities_block_raw.get("bridge") is True  # a link table
        # Per-entity entries
        per_entity: dict[str, dict[str, Any]] = {}
        for ent_name, ent_raw in entities_block.items():
            if not isinstance(ent_raw, dict) and ent_raw is not None:
                # Allow `entities: { customer: }` shorthand (None value).
                ent_raw = {}
            per_entity[str(ent_name)] = dict(ent_raw or {})

        def _canonical_key_for(entity_name: str) -> list[str]:
            ent = graph_entities.get(entity_name) or {}
            ent_key = ent.get("key") if isinstance(ent, dict) else None
            return _column_list(ent_key)

        # Resolve each entity's effective columns (expr override or canonical).
        effective_cols: dict[str, list[str]] = {}
        for ent_name, ent_spec in per_entity.items():
            expr_override = ent_spec.get("expr")
            if expr_override is not None:
                cols = expr_override if isinstance(expr_override, list) else [expr_override]
                effective_cols[ent_name] = [str(c) for c in cols]
            else:
                effective_cols[ent_name] = _canonical_key_for(ent_name)

        # Explicit bindings and authored identity precede the model-name default.
        primary_entity = str(model.get("entity", "") or "").strip()
        # Empty blocks retain the graph's existing model-name default.
        if (
            not primary_entity
            and (model_id in per_entity or not per_entity)
            and not (graph_entities.get(model_id) or {}).get("model")
        ):
            primary_entity = model_id
        if not primary_entity:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"model '{model_id}' must identify its primary entity with a graph model "
                "binding (graph.entities.<entity>.model), or list an unbound entity with the "
                "model's name",
            )

        # Translate to canonical singular-entity shape.
        model.setdefault("entity", primary_entity)
        keys: dict[str, Any] = {
            "primary": list(
                effective_cols.get(primary_entity) or _canonical_key_for(primary_entity)
            )
        }
        foreign = {name: cols for name, cols in effective_cols.items() if name != primary_entity}
        if foreign:
            keys["foreign"] = foreign
        model["keys"] = keys

        # Inferred relationships pass: for every non-primary entity in the
        # entities: block (excluding when `bridge: false`), synthesize a
        # default join entry if one isn't already authored. The downstream
        # parser produces a RelationshipConfig from each joins entry.
        if bool(bridge):
            joins = {name: {"to": name} for name in per_entity if name != primary_entity}
            if joins:
                model["joins"] = joins
        # Don't propagate the entities: block to the downstream parser; it's been translated.
        model.pop("entities", None)
        models[model_id] = model

    # Back-fill `graph.entities.<x>.model:` from the model whose primary
    # entity is <x>. Authors can leave the binding implicit when the model's name
    # disambiguates which model owns each entity.
    for model_id, model in models.items():
        # Fact models don't bind to a graph entity — skip the back-fill.
        model_kind = str(model.get("kind", "model") or "model").strip().lower()
        if model_kind == "fact":
            continue
        primary = str(model.get("entity", "") or "").strip()
        if primary and primary in graph_entities:
            entity_dict = dict(graph_entities[primary] or {})
            if (
                primary not in bound_entities.values()
                and entity_dict.get("model")
                and entity_dict["model"] != model_id
            ):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"models '{entity_dict['model']}' and '{model_id}' both claim unbound "
                    f"graph entity '{primary}'; bind each to its own entity",
                )
            if not entity_dict.get("model"):
                entity_dict["model"] = model_id
            graph_entities[primary] = entity_dict

    # Check final bindings, including back-filled identities and name defaults.
    bound_entities = {}
    for entity_key, entity_raw in list(graph_entities.items()):
        entity = dict(entity_raw or {})
        model_id = str(entity.get("model", entity_key) or entity_key)
        if model_id in bound_entities:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"model '{model_id}' is the primary home of both graph entities "
                f"'{bound_entities[model_id]}' and '{entity_key}'; bind each to its own model",
            )
        bound_entities[model_id] = str(entity_key)
        entity["model"] = model_id
        if namespace:
            entity["id"] = f"entity.{namespace}_{_slug(str(entity_key))}"
            _with_default(
                entity, "name", f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}"
            )
        _apply_as(entity)
        graph_entities[entity_key] = entity

        if model_id not in models:
            continue
        model = dict(models[model_id] or {})
        primary = str(model.get("entity", "") or "").strip()
        if (
            str(model.get("kind", "model") or "model").strip().lower() == "model"
            and primary
            and primary != entity_key
        ):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"model '{model_id}' declares entity '{primary}' but is bound to "
                f"graph entity '{entity_key}'",
            )
        # Refuse keyless graph entities before translating authored relationships.
        # Only this entity's declared key or its own model's key can supply it.
        model_keys = dict(model.get("keys", {}) or {})
        if not _column_list(entity.get("key") or model_keys.get("primary")):
            raise SemanticLayerError(
                "INVALID_CONFIG",
                f"graph entity '{entity_key}' must declare key (model '{model_id}')",
            )
        model.setdefault("id", model_id)
        model.setdefault("entity", entity_key)

        dimensions = dict(model.get("dimensions", {}) or {})
        for dim_key, dim_raw in list(dimensions.items()):
            dim = dict(dim_raw or {})
            if namespace:
                dim["id"] = f"dimension.{namespace}_{_slug(str(entity_key))}_{_slug(str(dim_key))}"
                _with_default(
                    dim,
                    "name",
                    f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}.{dim_key}",
                )
            _apply_as(dim)
            dimensions[dim_key] = dim

        # Auto-create key dimensions from graph entity keys.
        # The primary entity for this model is `entity_key`. Author-side
        # dropping of `kind: id` dimensions is supported by synthesizing
        # them here with stable IDs so segment membership / multi-hop joins
        # / FK lookups all resolve. Author can override by declaring a
        # dimension with the same column name (we skip auto-creation when
        # the column is already declared).
        existing_columns = {str((dimensions[dk] or {}).get("column", dk)) for dk in dimensions}
        # Also exclude columns covered by the times: block — they get a
        # backing dimension created by the temporal-roles pass in config.py
        # (using the role's `dimension_id:` if authored).
        for tk, tv in (model.get("times") or {}).items():
            if isinstance(tv, dict):
                existing_columns.add(str(tv.get("column", tk)))
        entity_canonical_key = entity.get("key") or []
        if isinstance(entity_canonical_key, list):
            cols_to_synth = list(entity_canonical_key)
        elif entity_canonical_key:
            cols_to_synth = [str(entity_canonical_key)]
        else:
            cols_to_synth = []
        for col in cols_to_synth:
            if str(col) in existing_columns or str(col) in dimensions:
                continue
            col_s = str(col)
            slug_entity = _slug(str(entity_key))
            slug_col = _slug(col_s)
            # If column slug starts with entity slug + "_", drop the
            # duplication for a cleaner ID (e.g., entity_key=customer,
            # col=customer_id → dimension.jaffle_customer_id, not
            # dimension.jaffle_customer_customer_id).
            if slug_col == slug_entity or slug_col.startswith(slug_entity + "_"):
                short_id_tail = slug_col
            else:
                short_id_tail = f"{slug_entity}_{slug_col}"
            synth = {
                "id": f"dimension.{namespace}_{short_id_tail}"
                if namespace
                else f"dimension.{short_id_tail}",
                "name": f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}.{col_s}"
                if namespace
                else f"{_titleize(str(entity_key))}.{col_s}",
                "label": _titleize(col_s),
                "column": col_s,
                "kind": "id",
            }
            dimensions[col_s] = synth

        # Auto-create dimensions for FK columns declared via the
        # `keys.foreign:` block (the loader fills it from `model.entities:`
        # earlier in this pass). The dimension's entity is the *current*
        # model's primary entity (FK column lives on this model's table).
        keys_fk = dict((model.get("keys") or {}).get("foreign") or {})
        for _fk_entity, fk_cols in keys_fk.items():
            if isinstance(fk_cols, str):
                fk_cols = [fk_cols]
            for col in fk_cols or []:
                col_s = str(col)
                if col_s in existing_columns or col_s in dimensions:
                    continue
                slug_entity = _slug(str(entity_key))
                slug_col = _slug(col_s)
                # FK dimensions also use the deduplication-friendly path so
                # `dimension.jaffle_order_customer_id` rather than
                # `dimension.jaffle_order_customer_id_customer_id`.
                if slug_col == slug_entity or slug_col.startswith(slug_entity + "_"):
                    short_id_tail = slug_col
                else:
                    short_id_tail = f"{slug_entity}_{slug_col}"
                synth = {
                    "id": f"dimension.{namespace}_{short_id_tail}"
                    if namespace
                    else f"dimension.{short_id_tail}",
                    "name": f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}.{col_s}"
                    if namespace
                    else f"{_titleize(str(entity_key))}.{col_s}",
                    "label": _titleize(col_s),
                    "column": col_s,
                    "kind": "id",
                }
                dimensions[col_s] = synth
                existing_columns.add(col_s)

        model["dimensions"] = dimensions

        times = dict(model.get("times", {}) or {})
        for time_key, time_raw in list(times.items()):
            time_spec = dict(time_raw or {})
            if namespace:
                time_spec["id"] = (
                    f"temporal_role.{namespace}_{_slug(str(entity_key))}_{_slug(str(time_key))}"
                )
                _with_default(
                    time_spec,
                    "dimension_id",
                    f"dimension.{namespace}_{_slug(str(entity_key))}_{_slug(str(time_key))}",
                )
                _with_default(
                    time_spec,
                    "name",
                    f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}.{time_key}",
                )
            _apply_as(time_spec)
            times[time_key] = time_spec
        model["times"] = times

        measures = dict(model.get("measures", {}) or {})
        for measure_key, measure_raw in list(measures.items()):
            measure = dict(measure_raw or {})
            if namespace:
                measure["id"] = f"measure.{namespace}.{_slug(str(measure_key))}"
                _with_default(measure, "name", f"{namespace}.{_slug(str(measure_key))}")
            _apply_as(measure)
            measures[measure_key] = measure
        model["measures"] = measures
        models[model_id] = model

    # Fact models — apply the same id/name normalization for their times,
    # measures, and dimensions. They don't bind to a graph entity, so the
    # graph-entities loop above skips them.
    for model_id, model in list(models.items()):
        model_kind = str(model.get("kind", "model") or "model").strip().lower()
        if model_kind != "fact":
            continue
        # Use the model_id as the entity-name slug for id derivation.
        entity_name_slug = _slug(str(model_id))
        # Times.
        times = dict(model.get("times", {}) or {})
        for time_key, time_raw in list(times.items()):
            time_spec = dict(time_raw or {})
            if namespace:
                time_spec["id"] = (
                    f"temporal_role.{namespace}_{entity_name_slug}_{_slug(str(time_key))}"
                )
                _with_default(
                    time_spec,
                    "dimension_id",
                    f"dimension.{namespace}_{entity_name_slug}_{_slug(str(time_key))}",
                )
                _with_default(
                    time_spec,
                    "name",
                    f"{namespace}.{_titleize(str(model_id)).replace(' ', '')}.{time_key}",
                )
            _apply_as(time_spec)
            times[time_key] = time_spec
        model["times"] = times
        # Measures.
        measures = dict(model.get("measures", {}) or {})
        for measure_key, measure_raw in list(measures.items()):
            measure = dict(measure_raw or {})
            if namespace:
                measure["id"] = f"measure.{namespace}.{_slug(str(measure_key))}"
                _with_default(measure, "name", f"{namespace}.{_slug(str(measure_key))}")
            _apply_as(measure)
            measures[measure_key] = measure
        model["measures"] = measures
        models[model_id] = model

    graph["entities"] = graph_entities

    # Top-level `graph.relationships:` block — translate the bidirectional
    # `entities: [a, b]` form into per-model `joins:` overrides keyed by
    # (source_model, target_entity). The downstream parser already accepts
    # `joins:` as the relationship surface, so we project the top-level
    # entries down onto the appropriate source model.
    #
    # Wire shape inside `graph.relationships:`:
    #   <name>:
    #     entities: [a, b]              # unordered pair
    #     cardinality: many_to_one      # first→second
    #     safety: safe
    #     allowed_directions: [...]
    #     temporal_validity: { ... }
    #     rollup_safe:
    #       reverse: [...]              # b→a population-count rewrite permission
    relationship_block = graph.get("relationships") or {}
    if not isinstance(relationship_block, dict):
        raise SemanticLayerError("INVALID_CONFIG", "graph.relationships must be a mapping")
    if relationship_block:
        # Only graph-bound models can hold relationships consumed by the parser.
        entity_to_model = {name: entity["model"] for name, entity in graph_entities.items()}
        for rel_name, rel_raw in list(relationship_block.items()):
            if not isinstance(rel_raw, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG", f"graph relationship '{rel_name}' must be a mapping"
                )
            spec = dict(rel_raw or {})
            entities_pair = spec.get("entities") or []
            if not isinstance(entities_pair, list) or len(entities_pair) != 2:
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"graph relationship '{rel_name}' must declare entities: [source, target]",
                )
            a, b = str(entities_pair[0]), str(entities_pair[1])
            for endpoint in (a, b):
                endpoint_model = entity_to_model.get(endpoint, "")
                if (
                    endpoint_model not in models
                    or str(models[endpoint_model].get("kind", "model") or "model").strip().lower()
                    != "model"
                ):
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"graph relationship '{rel_name}' cannot attach entity '{endpoint}' "
                        "to a graph model",
                    )
            cardinality = str(spec.get("cardinality", "")).strip().lower()
            # Translate friendly cardinality terms.
            cardinality_map = {
                "one_to_one": "1:1",
                "many_to_one": "N:1",
                "one_to_many": "1:N",
                "many_to_many": "M:N",
            }
            cardinality = cardinality_map.get(cardinality, cardinality)
            rollup_safe = spec.get("rollup_safe", {})
            if not isinstance(rollup_safe, dict):
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"graph relationship '{rel_name}' rollup_safe must be a mapping "
                    "with only the 'reverse' key; forward rollup declarations are not supported",
                )
            reverse_rollup = list(rollup_safe.get("reverse", []) or [])

            # Attach to source model `a`'s joins block as edge `b`.
            source_model = entity_to_model[a]
            model = dict(models[source_model] or {})
            joins = dict(model.get("joins", {}) or {})
            edge_spec: dict[str, Any] = {
                # `as:` keeps a public id, as on every other object.
                "id": str(spec.get("as") or "").strip() or f"relationship.{_slug(rel_name)}",
                "to": b,
            }
            if cardinality:
                edge_spec["cardinality"] = cardinality
            for passthrough in _RELATIONSHIP_PASSTHROUGH:
                if passthrough in spec:
                    edge_spec[passthrough] = spec[passthrough]
            # `allowed_directions:` on the graph.relationships block maps
            # to the join-spec `traversal:` field (the canonical reader
            # in config.py reads `traversal:` when populating
            # RelationshipConfig.allowed_directions). Author-facing field stays
            # `allowed_directions:`; we rename here at the translator boundary.
            if "allowed_directions" in spec:
                edge_spec["traversal"] = spec["allowed_directions"]
            if reverse_rollup:
                edge_spec["rollup_safe_aggregations_reverse"] = reverse_rollup
            # A model keeps every relationship to an entity. A route is its source
            # columns (an unset `via` is the model's foreign key to the entity).
            # An entry on the columns of the inferred join replaces it; another
            # column set is another role of the same entity, kept under its own
            # key. Two authored relationships on the same columns are refused
            # rather than merged, so no declaration order decides which one a
            # query gets and no authored attribute is dropped.
            foreign = (model.get("keys") or {}).get("foreign") or {}
            source_columns = _column_list(edge_spec.get("via")) or _column_list(foreign.get(b))
            same_route = [
                key
                for key, join in joins.items()
                if source_columns
                and (key == b or join.get("to") == b)
                and (_column_list(join.get("via")) or _column_list(foreign.get(key)))
                == source_columns
            ]
            if len(same_route) > 1 or any(joins[key].get("id") is not None for key in same_route):
                names = {str(joins[key].get("id") or f"joins.{key}") for key in same_route}
                raise SemanticLayerError(
                    "INVALID_CONFIG",
                    f"relationships {', '.join(sorted({*names, str(edge_spec['id'])}))} all "
                    f"join {a} to {b} on {source_columns}: keep one, or give each a different "
                    "`via:` if they are different roles.",
                )
            if same_route:
                joins[same_route[0]] = edge_spec
            elif b not in joins:
                joins[b] = edge_spec
            else:
                if not source_columns:
                    raise SemanticLayerError(
                        "INVALID_CONFIG",
                        f"relationship '{edge_spec['id']}' joins {a} to {b}, which the model "
                        "already reaches another way, but names no source columns: set `via:`.",
                    )
                edge_spec["via"] = source_columns
                join_key = f"{b}__{_slug(rel_name)}"
                while join_key in joins:
                    join_key += "_"
                joins[join_key] = edge_spec
            model["joins"] = joins
            models[source_model] = model

    # `graph.path_policy:` and `graph.path_preferences:` are authored next
    # to the relationships they govern, but the canonical parser reads them
    # at the document top level. Lift them out of the graph block; an
    # explicit top-level value (single-file authoring) wins.
    if "path_policy" in graph:
        out.setdefault("path_policy", graph.pop("path_policy"))
    if "path_preferences" in graph:
        out.setdefault("path_preferences", graph.pop("path_preferences"))

    out["graph"] = graph
    out["models"] = models

    # Top-level metrics block — auto-derive id/name and apply `as:` overrides.
    metrics_rows = out.get("metrics", {}) or {}
    if isinstance(metrics_rows, dict):
        normalized_metrics: dict[str, Any] = {}
        for metric_key, metric_raw in metrics_rows.items():
            metric_spec = dict(metric_raw or {})
            if namespace:
                metric_spec["id"] = f"metric.{namespace}.{_slug(str(metric_key))}"
                _with_default(metric_spec, "name", f"{namespace}.{_slug(str(metric_key))}")
            _apply_as(metric_spec)
            normalized_metrics[str(metric_key)] = metric_spec
        out["metrics"] = normalized_metrics

    # Top-level segments block — same treatment.
    segments_rows = out.get("segments", {}) or {}
    if isinstance(segments_rows, dict):
        normalized_segments: dict[str, Any] = {}
        for segment_key, segment_raw in segments_rows.items():
            segment_spec = dict(segment_raw or {})
            if namespace:
                segment_spec["id"] = f"segment.{namespace}.{_slug(str(segment_key))}"
                _with_default(segment_spec, "name", f"{namespace}.{_slug(str(segment_key))}")
            _apply_as(segment_spec)
            normalized_segments[str(segment_key)] = segment_spec
        out["segments"] = normalized_segments

    return out
