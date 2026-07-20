from __future__ import annotations

from typing import Any


def _slug(value: str) -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or ""))
    parts = [part for part in raw.split("_") if part]
    return "_".join(parts)


def _titleize(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value or "").replace("_", " ").split())


def _with_default(mapping: dict[str, Any], key: str, value: Any) -> None:
    if mapping.get(key) in {None, ""}:
        mapping[key] = value


def _apply_as_override(mapping: dict[str, Any], expected_kind: str, *, namespace: str) -> None:
    """If the author wrote `as: <full_id>`, override the auto-derived `id`.

    The `as:` value is an escape hatch for preserving public IDs that the
    natural key-derived form cannot reproduce — primarily IDs whose
    prefix differs from the package's namespace (e.g., a measure named
    `sales.revenue_usd` published under `metric.sales.revenue_usd` even
    though the package namespace is `jaffle`).

    Cross-package authoring (referencing objects in a different package) is
    out of scope — but the `as:` escape within a single package's YAML is
    exactly the mechanism for preserving non-namespace-prefixed public IDs.
    We accept any `<expected_kind>.<anything>` value; the package boundary
    is enforced elsewhere (loader does not load metrics from other
    packages).
    """
    raw = mapping.get("as")
    if raw is None:
        return
    text = str(raw).strip()
    if not text:
        return
    mapping["id"] = text


def _model_mapping(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    models = raw.get("models", {}) or {}
    if isinstance(models, dict):
        return {str(key): dict(value or {}) for key, value in models.items()}
    return {}


def normalize_package(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the ergonomic authoring contract into the canonical
    PackageConfig-shaped dict.

    Fills namespace-derived IDs and default publish blocks for fields the
    author left out. Existing explicit values are preserved unchanged.
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

    # Translate `model.entities:` (the ergonomic authoring block) into the
    # canonical singular `entity:` + `keys.primary:` + `keys.foreign:`
    # shape so the downstream parser stays bijective. The block lists every entity
    # the model exposes; the primary entity is auto-detected from grain
    # matching graph's <entity>.key. Per-entity options:
    #   expr:    column rename when authored column != graph's canonical key
    #   label:   display override (passed through as model_entity_labels meta)
    # Block-level option:
    #   bridge: false   — model is not eligible as a multi-hop join path
    #
    # When `entities:` is authored on a model, it produces:
    #   - `entity:` set to the auto-detected primary (or co-primary's first)
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
        entities_block_raw = model.get("entities")
        if not isinstance(entities_block_raw, dict):
            continue
        entities_block = dict(entities_block_raw)
        # Block-level options
        bridge = entities_block.pop("bridge", True)
        # Persist `bridge` as a top-level model flag for the inferred-relationship pass.
        model["_bridge_eligible"] = bool(bridge)
        # Per-entity entries
        per_entity: dict[str, dict[str, Any]] = {}
        for ent_name, ent_raw in entities_block.items():
            if not isinstance(ent_raw, dict) and ent_raw is not None:
                # Allow `entities: { customer: }` shorthand (None value).
                ent_raw = {}
            per_entity[str(ent_name)] = dict(ent_raw or {})
        if not per_entity:
            continue

        # Auto-detect primary entity by matching grain → canonical entity key.
        # Compute from graph_entities (which the loader will further normalize
        # below — but `key:` is authored, so it's available now).
        model_grain = model.get("grain")
        if model_grain is not None:
            grain_cols = [
                str(c) for c in (model_grain if isinstance(model_grain, list) else [model_grain])
            ]
        else:
            existing_primary = (
                (model.get("keys") or {}).get("primary")
                if isinstance(model.get("keys"), dict)
                else None
            )
            grain_cols = [
                str(c)
                for c in (
                    existing_primary
                    if isinstance(existing_primary, list)
                    else ([existing_primary] if existing_primary else [])
                )
            ]

        def _canonical_key_for(entity_name: str) -> list[str]:
            ent = graph_entities.get(entity_name) or {}
            ent_key = ent.get("key") if isinstance(ent, dict) else None
            if isinstance(ent_key, list):
                return [str(c) for c in ent_key]
            if ent_key:
                return [str(ent_key)]
            return []

        # Resolve each entity's effective columns (expr override or canonical).
        effective_cols: dict[str, list[str]] = {}
        for ent_name, ent_spec in per_entity.items():
            expr_override = ent_spec.get("expr")
            if expr_override is not None:
                cols = expr_override if isinstance(expr_override, list) else [expr_override]
                effective_cols[ent_name] = [str(c) for c in cols]
            else:
                effective_cols[ent_name] = _canonical_key_for(ent_name)

        # Auto-detect primary: the entity whose effective columns match grain.
        primary_entity: str = ""
        if grain_cols:
            for ent_name, cols in effective_cols.items():
                if cols == grain_cols:
                    primary_entity = ent_name
                    break
        if not primary_entity:
            # Fallback: first entity in declaration order is primary.
            primary_entity = next(iter(per_entity))

        # Translate to canonical singular-entity shape.
        model.setdefault("entity", primary_entity)
        keys = dict(model.get("keys", {}) or {})
        if "primary" not in keys:
            keys["primary"] = list(
                effective_cols.get(primary_entity) or _canonical_key_for(primary_entity)
            )
        foreign = dict(keys.get("foreign", {}) or {})
        for ent_name, cols in effective_cols.items():
            if ent_name == primary_entity:
                continue
            foreign.setdefault(ent_name, list(cols))
        if foreign:
            keys["foreign"] = foreign
        model["keys"] = keys

        # Inferred relationships pass: for every non-primary entity in the
        # entities: block (excluding when `bridge: false`), synthesize a
        # default join entry if one isn't already authored. The downstream
        # parser produces a RelationshipConfig from each joins entry.
        if bool(bridge):
            existing_joins = dict(model.get("joins", {}) or {})
            for ent_name in per_entity:
                if ent_name == primary_entity:
                    continue
                if ent_name in existing_joins:
                    continue
                existing_joins[ent_name] = {"to": ent_name}
            if existing_joins:
                model["joins"] = existing_joins
        # Don't propagate the entities: block to the downstream parser; it's been translated.
        model.pop("entities", None)
        models[model_id] = model

    # Back-fill `graph.entities.<x>.model:` from the model whose primary
    # entity is <x>. Authors can leave the binding implicit when grain
    # disambiguates which model owns each entity.
    if not any(isinstance(v, dict) and v.get("model") for v in graph_entities.values()):
        # Only auto-derive when no entity has explicit model binding (avoid
        # mixing implicit and explicit in a single graph).
        for model_id, model in models.items():
            # Fact models don't bind to a graph entity — skip the back-fill.
            model_kind = str(model.get("kind", "model") or "model").strip().lower()
            if model_kind == "fact":
                continue
            primary = str(model.get("entity", "") or "").strip()
            if primary and primary in graph_entities:
                entity_dict = dict(graph_entities[primary] or {})
                entity_dict.setdefault("model", model_id)
                graph_entities[primary] = entity_dict

    for entity_key, entity_raw in list(graph_entities.items()):
        entity = dict(entity_raw or {})
        model_id = str(entity.get("model", entity_key) or entity_key)
        entity["model"] = model_id
        if namespace:
            _with_default(entity, "id", f"entity.{namespace}_{_slug(str(entity_key))}")
            _with_default(
                entity, "name", f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}"
            )
        _apply_as_override(entity, "entity", namespace=namespace)
        graph_entities[entity_key] = entity

        if model_id not in models:
            continue
        model = dict(models[model_id] or {})
        model.setdefault("id", model_id)
        model.setdefault("entity", entity_key)
        model.setdefault("defaults", {})

        dimensions = dict(model.get("dimensions", {}) or {})
        for dim_key, dim_raw in list(dimensions.items()):
            dim = dict(dim_raw or {})
            if namespace:
                _with_default(
                    dim,
                    "id",
                    f"dimension.{namespace}_{_slug(str(entity_key))}_{_slug(str(dim_key))}",
                )
                _with_default(
                    dim,
                    "name",
                    f"{namespace}.{_titleize(str(entity_key)).replace(' ', '')}.{dim_key}",
                )
            _apply_as_override(dim, "dimension", namespace=namespace)
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
                _with_default(
                    time_spec,
                    "id",
                    f"temporal_role.{namespace}_{_slug(str(entity_key))}_{_slug(str(time_key))}",
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
            _apply_as_override(time_spec, "temporal_role", namespace=namespace)
            times[time_key] = time_spec
        model["times"] = times

        measures = dict(model.get("measures", {}) or {})
        for measure_key, measure_raw in list(measures.items()):
            measure = dict(measure_raw or {})
            if namespace:
                _with_default(measure, "id", f"measure.{namespace}.{_slug(str(measure_key))}")
                _with_default(measure, "name", f"{namespace}.{_slug(str(measure_key))}")
            _apply_as_override(measure, "measure", namespace=namespace)
            if measure.get("publish") is None:
                measure["publish"] = {
                    "id": f"metric.{namespace}.{_slug(str(measure_key))}"
                    if namespace
                    else f"metric.{_slug(str(measure_key))}",
                    "label": str(measure.get("label", _titleize(str(measure_key)))),
                }
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
                _with_default(
                    time_spec,
                    "id",
                    f"temporal_role.{namespace}_{entity_name_slug}_{_slug(str(time_key))}",
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
            _apply_as_override(time_spec, "temporal_role", namespace=namespace)
            times[time_key] = time_spec
        model["times"] = times
        # Measures.
        measures = dict(model.get("measures", {}) or {})
        for measure_key, measure_raw in list(measures.items()):
            measure = dict(measure_raw or {})
            if namespace:
                _with_default(measure, "id", f"measure.{namespace}.{_slug(str(measure_key))}")
                _with_default(measure, "name", f"{namespace}.{_slug(str(measure_key))}")
            _apply_as_override(measure, "measure", namespace=namespace)
            if measure.get("publish") is None:
                measure["publish"] = {
                    "id": f"metric.{namespace}.{_slug(str(measure_key))}"
                    if namespace
                    else f"metric.{_slug(str(measure_key))}",
                    "label": str(measure.get("label", _titleize(str(measure_key)))),
                }
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
    #     rollup_safe:                  # per-direction (forward = a→b)
    #       forward: [...]
    #       reverse: [...]              # captured but downstream schema only
    #                                   # has one rollup_safe_aggregations list
    relationship_block = graph.get("relationships") or {}
    if isinstance(relationship_block, dict) and relationship_block:
        # Build entity → primary model lookup so we can attach the join to
        # the right source model.
        entity_to_model: dict[str, str] = {}
        for model_id, model in models.items():
            primary = str(model.get("entity", "") or "").strip()
            if primary:
                entity_to_model.setdefault(primary, model_id)
        for rel_name, rel_raw in list(relationship_block.items()):
            spec = dict(rel_raw or {})
            entities_pair = spec.get("entities") or []
            if not isinstance(entities_pair, list) or len(entities_pair) != 2:
                # Scalar from/to shape handled by passthrough; skip.
                continue
            a, b = str(entities_pair[0]), str(entities_pair[1])
            cardinality = str(spec.get("cardinality", "")).strip().lower()
            # Translate friendly cardinality terms.
            cardinality_map = {
                "one_to_one": "1:1",
                "many_to_one": "N:1",
                "one_to_many": "1:N",
                "many_to_many": "M:N",
            }
            cardinality = cardinality_map.get(cardinality, cardinality)
            # rollup_safe: forward is a→b; reverse is b→a.
            rollup_safe = spec.get("rollup_safe") or {}
            forward_rollup: list[str] = []
            reverse_rollup: list[str] = []
            if isinstance(rollup_safe, dict):
                forward_rollup = list(rollup_safe.get("forward", []) or [])
                reverse_rollup = list(rollup_safe.get("reverse", []) or [])
            elif isinstance(rollup_safe, list):
                forward_rollup = list(rollup_safe)

            # Attach to source model `a`'s joins block as edge `b`.
            source_model = entity_to_model.get(a)
            if not source_model or source_model not in models:
                continue
            model = dict(models[source_model] or {})
            joins = dict(model.get("joins", {}) or {})
            edge_spec: dict[str, Any] = {
                "id": str(spec.get("id", f"relationship.{_slug(rel_name)}")),
                "to": b,
            }
            if cardinality:
                edge_spec["cardinality"] = cardinality
            for passthrough in (
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
                "path_preference",
            ):
                if passthrough in spec:
                    edge_spec[passthrough] = spec[passthrough]
            # `allowed_directions:` on the graph.relationships block maps
            # to the join-spec `traversal:` field (the canonical reader
            # in config.py reads `traversal:` when populating
            # RelationshipConfig.allowed_directions). Author-facing field stays
            # `allowed_directions:`; we rename here at the translator boundary.
            if "allowed_directions" in spec:
                edge_spec["traversal"] = spec["allowed_directions"]
            if forward_rollup:
                edge_spec["rollup_safe_aggregations"] = forward_rollup
            # Reverse rollup is captured as a sibling key for now; the
            # runtime schema has a single rollup_safe_aggregations field
            # which is forward-only. Reverse stays available for future
            # bidirectional rollup checks.
            if reverse_rollup:
                edge_spec["rollup_safe_aggregations_reverse"] = reverse_rollup
            joins[b] = edge_spec
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
                _with_default(metric_spec, "id", f"metric.{namespace}.{_slug(str(metric_key))}")
                _with_default(metric_spec, "name", f"{namespace}.{_slug(str(metric_key))}")
            _apply_as_override(metric_spec, "metric", namespace=namespace)
            normalized_metrics[str(metric_key)] = metric_spec
        out["metrics"] = normalized_metrics

    # Top-level segments block — same treatment.
    segments_rows = out.get("segments", {}) or {}
    if isinstance(segments_rows, dict):
        normalized_segments: dict[str, Any] = {}
        for segment_key, segment_raw in segments_rows.items():
            segment_spec = dict(segment_raw or {})
            if namespace:
                _with_default(segment_spec, "id", f"segment.{namespace}.{_slug(str(segment_key))}")
                _with_default(segment_spec, "name", f"{namespace}.{_slug(str(segment_key))}")
            _apply_as_override(segment_spec, "segment", namespace=namespace)
            normalized_segments[str(segment_key)] = segment_spec
        out["segments"] = normalized_segments

    return out
