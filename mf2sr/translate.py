"""Deterministic MetricFlow -> Semantic Rails translator.

Entry point: :func:`translate`. Given a MetricFlow input (a YAML
directory or a `semantic_manifest.json` path) and a target Semantic
Rails package directory, produces a complete, loadable package:

    <output_dir>/<package_id>/
        package.yml
        graph.yml
        models/<name>.yml          # one per MetricFlow semantic_model
        metrics/<group>.yml        # grouped by source semantic_model

The translation is intentionally lossy in specific places (notably
free-text Jinja filters that don't match a known shape). Every loss
is recorded on the returned :class:`TranslationReport` so callers can
surface warnings without aborting the run.
"""

from __future__ import annotations

import ast as pyast
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import parsers
from .filter_parser import filter_clauses

# ---------------------------------------------------------------------------
# Aggregation name mapping
# ---------------------------------------------------------------------------

# MetricFlow agg -> Semantic Rails default_agg + how to wrap the expr.
# When `expr_wrap` is set, the original measure expression is rewritten
# (e.g. `sum_boolean` becomes `sum` over a CASE expression).
_AGG_MAP: dict[str, tuple[str, str | None]] = {
    "sum": ("sum", None),
    "count": ("count", None),
    "count_distinct": ("count_distinct", None),
    "average": ("avg", None),
    "avg": ("avg", None),
    "min": ("min", None),
    "max": ("max", None),
    "median": ("median", None),
    "percentile": ("percentile", None),
    # MetricFlow's sum_boolean = sum a boolean cast. We materialize the
    # cast into the expression so the runtime never has to recognize
    # the agg name.
    "sum_boolean": ("sum", "case_boolean"),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class TranslationReport:
    """What was emitted, what was skipped, and why.

    Attributes:
        package_dir:        absolute path of the emitted package directory.
        models_emitted:     one entry per semantic_model that produced a model.
        metrics_emitted:    one entry per metric that produced a Semantic Rails metric.
        warnings:           non-fatal issues. Each warning is human-readable
                            and includes the source object name when available.
    """

    package_dir: Path
    models_emitted: list[str] = field(default_factory=list)
    metrics_emitted: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Optional export evidence: digest of parsed input plus translation losses.
    provenance: dict[str, Any] = field(default_factory=dict)


def translate(
    source: Path | str,
    output_dir: Path | str,
    *,
    package_id: str,
    namespace: str | None = None,
    warehouse: str = "duckdb",
    default_db: str | None = None,
    description: str | None = None,
    schema_strict: bool = False,
) -> TranslationReport:
    """Translate a MetricFlow input into a Semantic Rails package
    directory and return a :class:`TranslationReport`.

    Args:
        source:       MetricFlow YAML directory OR `semantic_manifest.json`
                      file path.
        output_dir:   Directory under which `<package_id>/` will be created
                      (e.g. ``configs/semantic_rails``).
        package_id:   Semantic Rails package id. Also drives the namespace
                      when `namespace` is omitted.
        namespace:    Optional namespace prefix for auto-derived IDs.
                      Defaults to `package_id`.
        warehouse:    `duckdb` or `snowflake`. Controls the `package.yml`
                      shape; DuckDB packages additionally need `default_db`.
        default_db:   File path for DuckDB. Ignored for Snowflake.
        description:  Optional package description.
        schema_strict: Write a ``schema_strict: true`` package whose relations
                      keep the schema (and, on a catalog warehouse, the
                      database) of their ``node_relation``, as dbt's
                      ``semantic_manifest.json`` records it. The package is
                      parse-checked, and each strict error is a warning.
    """
    namespace = namespace or package_id
    src = Path(source)
    out_root = Path(output_dir) / package_id
    # The package can contain authored files, and older runs have no file
    # ownership record. Refuse reuse before writing so skipped or removed
    # metrics cannot survive as apparently current output.
    if out_root.is_symlink() or (
        out_root.exists() and (not out_root.is_dir() or any(out_root.iterdir()))
    ):
        raise FileExistsError(
            f"mf2sr cannot reuse nonempty package destination {out_root}; "
            "choose a fresh output path or review and remove the old package first"
        )
    out_root.mkdir(parents=True, exist_ok=True)

    raw = parsers.load(src)
    report = TranslationReport(package_dir=out_root)

    graph = _build_graph(raw["semantic_models"], report)
    dimension_ids = _dimension_ids(raw["semantic_models"], graph, namespace)
    _write_package_yml(
        out_root,
        package_id=package_id,
        namespace=namespace,
        warehouse=warehouse,
        default_db=default_db,
        description=description,
        schema_strict=schema_strict,
    )
    _write_graph_yml(out_root, graph)

    measure_owner: dict[str, str] = {}
    measure_to_value_type: dict[str, str] = {}
    measure_to_agg: dict[str, str] = {}
    running_total_problems: dict[str, str] = {}
    # Collect explicit metric names so we can suppress measure
    # auto-publish for any measure whose name will collide with a
    # metric we author explicitly. Without `publish: false` Semantic
    # Rails auto-creates a measure-derived metric and the explicit
    # metric becomes a duplicate ID.
    metric_names: set[str] = {m["name"] for m in raw["metrics"] if m.get("name")}
    # Preserve explicit source references before lowering a ratio side to a
    # measure aggregate. A source metric wins over a same-named measure, even
    # when that metric cannot be emitted.
    metric_dependencies = {
        metric["name"]: _source_metric_refs(metric, metric_names)
        for metric in raw["metrics"]
        if metric.get("name")
    }
    source_metrics = {metric["name"]: metric for metric in raw["metrics"] if metric.get("name")}

    owning_models: set[str] = graph.get("_owning_models", set())
    databases = Counter(
        str((sm.get("node_relation") or {}).get("database") or "") for sm in raw["semantic_models"]
    )
    usual_database = databases.most_common(1)[0][0] if databases else ""
    models_dir = out_root / "models"
    models_dir.mkdir(exist_ok=True)
    for sm in raw["semantic_models"]:
        name = sm.get("name")
        if not name:
            report.warnings.append("semantic_model without `name` — skipped")
            continue
        if name not in owning_models:
            # Already warned during graph extraction; just skip emit.
            continue
        model_doc, measures_in_model = _build_model(
            sm,
            graph,
            report,
            suppress_publish=metric_names,
            relation=_relation(
                sm,
                warehouse=warehouse,
                keep_schema=schema_strict,
                usual_database=usual_database,
                report=report,
            ),
        )
        (models_dir / f"{name}.yml").write_text(_dump_yaml({"model": model_doc}))
        report.models_emitted.append(name)
        for measure_name, vt, default_agg in measures_in_model:
            measure_owner[measure_name] = name
            measure_to_value_type[measure_name] = vt
            measure_to_agg[measure_name] = default_agg
        running_total_problems.update(_running_total_problems(sm, model_doc, graph))

    metrics_by_owner: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for metric in raw["metrics"]:
        translated = _build_metric(
            metric,
            measure_owner=measure_owner,
            measure_value_type=measure_to_value_type,
            measure_agg=measure_to_agg,
            dimension_ids=dimension_ids,
            running_total_problems=running_total_problems,
            source_metrics=source_metrics,
            report=report,
        )
        if translated is None:
            continue
        metric_name, metric_doc, owner_hint = translated
        metrics_by_owner.setdefault(owner_hint, []).append((metric_name, metric_doc))
        report.metrics_emitted.append(metric_name)
    _drop_dependents(metrics_by_owner, metric_dependencies, report)

    rolling = sorted(
        metric_name
        for entries in metrics_by_owner.values()
        for metric_name, metric_doc in entries
        if metric_doc.get("kind") == "rolling"
    )
    if rolling:
        report.warnings.append(
            f"rolling metrics ({', '.join(rolling)}) are computed over the package calendar, "
            "which mf2sr doesn't write: add a `kind: time` entity whose table has date_day, "
            "week_start, month_start, quarter_start and year_start before querying them"
        )

    if metrics_by_owner:
        metrics_dir = out_root / "metrics"
        metrics_dir.mkdir(exist_ok=True)
        for owner, entries in sorted(metrics_by_owner.items()):
            grouped = {name: doc for name, doc in entries}
            (metrics_dir / f"{owner}.yml").write_text(_dump_yaml({"metrics": grouped}))

    if schema_strict:  # semantic_rails loads only here, so mf2sr imports without it
        from semantic_rails.config_validation import PackageReference, parse_config_report

        parse, _ = parse_config_report(PackageReference(source_path=str(out_root)))
        report.warnings.extend(f"parse: {e.get('message', '')}" for e in parse["errors"])
    report.provenance = {
        "format_version": 1,
        "framework": "metricflow",
        "parsed_input_hash": "sha256:"
        + hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest(),
        "warnings": list(report.warnings),
    }
    return report


def _relation(
    sm: dict[str, Any],
    *,
    warehouse: str,
    keep_schema: bool,
    usual_database: str,
    report: TranslationReport,
) -> str:
    """The relation a semantic model reads: its ``node_relation`` alias, and in
    strict mode also its schema and database, named as ``import_dbt_project``
    names a dbt relation."""
    node = dict(sm.get("node_relation") or {})
    alias = str(node.get("alias") or sm["name"])
    if not keep_schema:
        return alias
    if not node.get("schema_name"):
        report.warnings.append(
            f"semantic model `{sm['name']}` names no schema, so its relation stays `{alias}`; "
            "translate dbt's target/semantic_manifest.json to keep schemas"
        )
        return alias
    from semantic_rails.dbt_artifacts import qualified_relation

    return qualified_relation(
        str(node.get("database") or ""),
        str(node["schema_name"]),
        alias,
        default_database=usual_database,
        default_schema="main" if warehouse == "duckdb" else "",  # as dbt-duckdb builds it
    )


# ---------------------------------------------------------------------------
# Graph extraction
# ---------------------------------------------------------------------------


def _build_graph(
    semantic_models: list[dict[str, Any]], report: TranslationReport
) -> dict[str, Any]:
    """Walk all semantic_models and synthesize the entities block.

    Semantic Rails requires a 1:1 link between graph entities and the
    models that declare them primary — every model must own at least
    one entity, and every entity points at exactly one owning model.
    MetricFlow allows the same entity to be `type: primary` in
    multiple semantic_models (e.g. a dim table and a fact table can
    both declare `user` primary), and allows pure dimension models
    with no measures.

    We resolve this with greedy assignment in semantic_model
    declaration order:

      1. For each semantic_model, find the first `type: primary`
         (or `type: unique`) entity not yet claimed and claim it.
         A bare `primary_entity:` field counts only when no explicit
         primary-type entity is in the list.
      2. Models that cannot claim any entity (every primary they
         declare is already claimed) are dropped with a warning —
         their measures/dimensions would dangle.
      3. Entities that only appear as `type: foreign` across all
         models are dropped from the graph; model `entities:` blocks
         strip references to them so the package still loads.
    """
    claimed_entities: set[str] = set()
    model_to_entity: dict[str, str] = {}
    entity_to_model: dict[str, str] = {}
    entity_key_col: dict[str, str] = {}
    entity_label: dict[str, str] = {}

    referenced: set[str] = set()

    for sm in semantic_models:
        sm_name = sm.get("name")
        if not sm_name:
            continue
        candidates: list[tuple[str, str, str | None]] = []  # (name, key, label)
        for ent in sm.get("entities") or []:
            ename = ent.get("name")
            if not ename:
                continue
            referenced.add(ename)
            etype = (ent.get("type") or "").lower()
            if etype in ("primary", "unique"):
                key = ent.get("expr") or ename
                candidates.append((ename, key, ent.get("label")))
        if not candidates and sm.get("primary_entity"):
            pe = sm["primary_entity"]
            referenced.add(pe)
            candidates.append((pe, f"{pe}_id", None))

        # Claim the first unclaimed candidate.
        for ename, key, label in candidates:
            if ename in claimed_entities:
                continue
            claimed_entities.add(ename)
            model_to_entity[sm_name] = ename
            entity_to_model[ename] = sm_name
            entity_key_col[ename] = key
            if label:
                entity_label[ename] = label
            break
        else:
            if candidates:
                report.warnings.append(
                    f"model `{sm_name}` declares primary entit{'y' if len(candidates) == 1 else 'ies'} "
                    f"{[c[0] for c in candidates]!r}, but all of them are "
                    "already owned by earlier models. The model is dropped "
                    "because Semantic Rails requires every model to own a "
                    "graph entity. Move measures into the canonical owning "
                    "model or rename this primary entity to recover it."
                )

    # Pick up labels from foreign declarations if we missed them.
    for sm in semantic_models:
        for ent in sm.get("entities") or []:
            ename = ent.get("name")
            if (
                ename
                and ename in claimed_entities
                and ename not in entity_label
                and ent.get("label")
            ):
                entity_label[ename] = ent["label"]

    dropped = referenced - claimed_entities
    for ename in sorted(dropped):
        report.warnings.append(
            f"entity `{ename}` appears only as foreign across all "
            "semantic_models — dropped from the graph. References to "
            "it are also stripped from model `entities:` blocks. Add a "
            "primary declaration in a source model to restore the join."
        )

    entities: dict[str, Any] = {}
    for ename, owner in entity_to_model.items():
        entities[ename] = {
            "label": entity_label.get(ename) or _humanize(ename),
            "key": [entity_key_col[ename]],
            "model": owner,
        }

    return {
        "entities": entities,
        "_owning_models": set(model_to_entity.keys()),
        "_dropped_entities": dropped,
    }


def _has_primary_entity_in_list(sm: dict[str, Any]) -> bool:
    for ent in sm.get("entities") or []:
        if (ent.get("type") or "").lower() in ("primary", "unique"):
            return True
    return False


# ---------------------------------------------------------------------------
# Model translation
# ---------------------------------------------------------------------------


def _build_model(
    sm: dict[str, Any],
    graph: dict[str, Any],
    report: TranslationReport,
    *,
    suppress_publish: set[str] | None = None,
    relation: str,
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Build the Semantic Rails `model:` body for one MetricFlow
    semantic_model. Returns `(model_doc, measures)` where `measures` is
    a list of `(name, value_type, default_agg)` for the metric-stage
    translator to consult. Returns an empty list if the model has no
    primary entity and no measures (skipped).
    """
    name = sm["name"]
    description = sm.get("description") or sm.get("label") or name

    doc: dict[str, Any] = {
        "id": name,
        "relation": relation,
        "description": description,
    }

    # Entities block — skip entities that didn't make it into the graph
    # (FK-only, dropped during graph extraction).
    entities_block: dict[str, Any] = {}
    raw_entities = list(sm.get("entities") or [])
    primary_entity = sm.get("primary_entity")
    has_primary_in_list = any(
        (e.get("type") or "").lower() in ("primary", "unique") for e in raw_entities
    )
    valid_entities = set(graph["entities"].keys())
    if primary_entity and not has_primary_in_list and primary_entity in valid_entities:
        entities_block[primary_entity] = {}
    for ent in raw_entities:
        ename = ent.get("name")
        if not ename or ename not in valid_entities:
            continue
        canonical_key_list = graph["entities"][ename].get("key", [ename])
        canonical_key = (
            canonical_key_list[0]
            if isinstance(canonical_key_list, list) and canonical_key_list
            else canonical_key_list
        )
        expr = ent.get("expr") or ename
        if expr == canonical_key:
            entities_block[ename] = {}
        else:
            entities_block[ename] = {"expr": expr}
    doc["entities"] = entities_block

    # Times block — collect type:time dimensions
    times: dict[str, Any] = {}
    default_time_name = (sm.get("defaults") or {}).get("agg_time_dimension")
    time_dim_names: set[str] = set()
    for dim in sm.get("dimensions") or []:
        if (dim.get("type") or "").lower() != "time":
            continue
        dname = dim.get("name")
        if not dname:
            continue
        time_dim_names.add(dname)
        granularity = (dim.get("type_params") or {}).get("time_granularity") or "day"
        kind = "date" if granularity in ("day", "week", "month", "quarter", "year") else "timestamp"
        # MetricFlow time dims at sub-daily grain still surface as
        # `timestamp` to Semantic Rails. Default to `timestamp` only
        # when granularity is below day; everything daily-and-up gets
        # `date` for cleaner storage semantics.
        entry: dict[str, Any] = {
            "label": _humanize(dname),
            "column": dim.get("expr") or dname,
            "kind": kind,
            "class": "event_time",
            "supported_grains": _grains_at_or_above(granularity),
        }
        if dname == default_time_name:
            entry["default"] = True
        times[dname] = entry
    if times:
        # If no entry was marked default but agg_time_dimension was set
        # and matches a known role, mark it. If none of those, mark the
        # first entry as default to satisfy the loader.
        if not any(t.get("default") for t in times.values()):
            first_name = next(iter(times))
            times[first_name]["default"] = True
            report.warnings.append(
                f"model `{name}`: no `agg_time_dimension` resolved — defaulted to `{first_name}`."
            )
        doc["times"] = times

    # Categorical/boolean dimensions
    dimensions: dict[str, Any] = {}
    for dim in sm.get("dimensions") or []:
        if (dim.get("type") or "").lower() == "time":
            continue
        dname = dim.get("name")
        if not dname:
            continue
        kind = _infer_dimension_kind(dname)
        dim_entry: dict[str, Any] = {
            "label": dim.get("label") or _humanize(dname),
            "kind": kind,
        }
        if dim.get("expr") and dim["expr"] != dname:
            dim_entry["expr"] = dim["expr"]
        dimensions[dname] = dim_entry
    if dimensions:
        doc["dimensions"] = dimensions

    # Measures
    measures: dict[str, Any] = {}
    measures_summary: list[tuple[str, str, str]] = []
    suppress = suppress_publish or set()
    for measure in sm.get("measures") or []:
        result = _build_measure(measure, name, graph, sm, report)
        if result is None:
            continue
        mname, mdoc, value_type, default_agg = result
        # Suppress auto-publish when an explicit metric of the same
        # name exists — otherwise the loader raises duplicate metric id.
        if mname in suppress:
            mdoc["publish"] = False
        measures[mname] = mdoc
        measures_summary.append((mname, value_type, default_agg))
    if measures:
        doc["measures"] = measures

    return doc, measures_summary


def _build_measure(
    measure: dict[str, Any],
    model_name: str,
    graph: dict[str, Any],
    sm: dict[str, Any],
    report: TranslationReport,
) -> tuple[str, dict[str, Any], str, str] | None:
    """Translate a single MetricFlow measure. Returns
    ``(name, doc, value_type, default_agg)`` or ``None`` to skip.

    Mapping decisions:
      - ``agg: count_distinct`` or ``expr: 1`` -> ``kind: entity_count``
        targeting whichever graph entity owns the matching key column.
        If we cannot resolve an entity, the measure is dropped with a
        warning (Semantic Rails has no other shape that allows
        count_distinct over an aggregate measure).
      - ``agg: sum_boolean`` -> sum over a CASE expression (AST form).
      - Everything else -> ``kind: aggregate`` with the inferred
        accumulation class and value type.
    """
    name = measure.get("name")
    if not name:
        report.warnings.append(f"model `{model_name}`: skipping measure without `name`.")
        return None
    agg_raw = (measure.get("agg") or "sum").lower()
    if agg_raw not in _AGG_MAP:
        report.warnings.append(
            f"model `{model_name}`: measure `{name}` uses unsupported "
            f"agg `{agg_raw}`. Falling back to `sum`."
        )
        agg_raw = "sum"
    default_agg, expr_wrap = _AGG_MAP[agg_raw]

    expr = measure.get("expr")

    # entity_count path: count of rows (`expr: 1`) or count_distinct
    # over an entity key column. We try to resolve to a graph entity
    # so the measure binds to a real join key.
    wants_entity_count = (expr in ("1", 1)) or (agg_raw in ("count_distinct", "count"))
    if wants_entity_count:
        target_entity = _resolve_entity_for_count(
            sm_name=model_name,
            sm=sm,
            expr=expr,
            graph=graph,
        )
        if target_entity is None:
            # MetricFlow `count(col)` semantic: count rows where col is
            # not null. Express as SUM(CASE WHEN col IS NOT NULL THEN 1
            # ELSE 0 END) so we stay inside the flow accumulation class.
            if agg_raw in ("count", "count_distinct") and isinstance(expr, str):
                count_col = expr
                report.warnings.append(
                    f"model `{model_name}`: measure `{name}` "
                    f"(agg=`{agg_raw}`, expr=`{count_col}`) has no "
                    "matching graph entity — emitting as "
                    "SUM(CASE WHEN col IS NOT NULL THEN 1 ELSE 0 END). "
                    f"{'count_distinct semantics are lost' if agg_raw == 'count_distinct' else 'Result matches MetricFlow count(col).'}"
                )
                fallback_expr = {
                    "kind": "case",
                    "whens": [
                        {
                            "when": {
                                "kind": "comparison",
                                "op": "!=",
                                "left": {"kind": "column", "column": count_col},
                                "right": {"kind": "literal", "value": None},
                            },
                            "then": {"kind": "literal", "value": 1},
                        }
                    ],
                    "else": {"kind": "literal", "value": 0},
                }
                doc = {
                    "label": measure.get("label") or _humanize(name),
                    "kind": "aggregate",
                    "expr": fallback_expr,
                    "default_agg": "sum",
                    "accumulation": {"kind": "flow"},
                    "value_type": "count",
                }
                measure_time = measure.get("agg_time_dimension")
                if measure_time:
                    doc["time"] = measure_time
                return name, doc, "count", "sum"
            report.warnings.append(
                f"model `{model_name}`: measure `{name}` (agg=`{agg_raw}`, "
                f"expr=`{expr}`) cannot be mapped to a Semantic Rails "
                "entity_count — no matching primary entity in the graph. "
                "The measure is dropped. Add a model that declares the "
                "missing entity as primary to recover it."
            )
            return None
        # `entity_key:` in Semantic Rails is the COLUMN name on this
        # model (not the entity name). Resolve via the graph entity's
        # canonical key list. If this model declares the entity via an
        # FK with a different `expr:` column, that column wins over the
        # graph's canonical key — it's the local column reference the
        # compiler emits in COUNT(DISTINCT ...).
        entity_meta = graph["entities"][target_entity]
        canonical_key = entity_meta.get("key", [])
        if isinstance(canonical_key, list):
            key_col = canonical_key[0] if canonical_key else target_entity
        else:
            key_col = canonical_key
        for ent_ref in sm.get("entities") or []:
            if ent_ref.get("name") == target_entity and ent_ref.get("expr"):
                key_col = ent_ref["expr"]
                break
        doc = {
            "label": measure.get("label") or _humanize(name),
            "kind": "entity_count",
            "entity_key": key_col,
            "accumulation": {"kind": "event"},
            "value_type": "count",
        }
        # Override time when MetricFlow sets it at the measure level.
        measure_time = measure.get("agg_time_dimension")
        if measure_time:
            doc["time"] = measure_time
        return name, doc, "count", "count_distinct"

    if expr_wrap == "case_boolean":
        # MetricFlow `sum_boolean` -> SUM(CASE WHEN <col> = true THEN 1 ELSE 0 END).
        # Semantic Rails' string expression parser doesn't accept SQL
        # CASE syntax, so we emit the AST dict form instead. The runtime
        # treats `expr:` as either a string or a parsed expression dict.
        bool_col = expr or name
        expr = {
            "kind": "case",
            "whens": [
                {
                    "when": {
                        "kind": "comparison",
                        "op": "=",
                        "left": {"kind": "column", "column": bool_col},
                        "right": {"kind": "literal", "value": True},
                    },
                    "then": {"kind": "literal", "value": 1},
                }
            ],
            "else": {"kind": "literal", "value": 0},
        }

    value_type, accumulation = _infer_value_type_and_accumulation(name, agg_raw)

    final_expr = expr if expr is not None else name
    # If `expr:` is a SQL string that Semantic Rails' Python-AST-based
    # expression parser cannot consume (e.g. inline `CASE WHEN`,
    # function calls like `COALESCE`, or `LIKE` patterns), drop the
    # measure with a clear warning. The author can rewrite the expr as
    # an AST dict or push the SQL down to the model SQL.
    if isinstance(final_expr, str) and not _is_simple_expr(final_expr):
        report.warnings.append(
            f"model `{model_name}`: measure `{name}` uses SQL-shaped "
            f"expr `{final_expr!r}` that Semantic Rails' expression "
            "parser cannot consume. The measure is dropped. Rewrite "
            "the expression as an AST (kind: case) or move the SQL "
            "into the underlying warehouse model."
        )
        return None

    doc = {
        "label": measure.get("label") or _humanize(name),
        "kind": "aggregate",
        "expr": final_expr,
        "default_agg": default_agg,
        "accumulation": {"kind": accumulation},
        "value_type": value_type,
    }

    # Override time per measure when MetricFlow sets agg_time_dimension
    # at the measure level (differs from the model default).
    measure_time = measure.get("agg_time_dimension")
    if measure_time:
        doc["time"] = measure_time

    # Percentile parameters carry over verbatim.
    agg_params = measure.get("agg_params")
    if agg_params and agg_raw == "percentile":
        doc["agg_params"] = agg_params

    return name, doc, value_type, default_agg


def _is_simple_expr(text: str) -> bool:
    """Return True if `text` looks like a measure expression Semantic
    Rails can parse: bare columns, arithmetic, simple comparisons.
    Returns False for SQL-shaped strings containing keywords like
    `CASE`, `LIKE`, `COALESCE`, etc. Use as a guard before emitting
    a string expression."""
    import re as _re

    if not text or not text.strip():
        return False
    sql_keywords = (
        r"\b(case|when|then|else|end|like|coalesce|nullif|cast|"
        r"convert|interval|date_trunc|substring|extract|sum|count|"
        r"avg|min|max|is|null|not|and|or|in|between)\b"
    )
    return not _re.search(sql_keywords, text, _re.IGNORECASE)


def _resolve_entity_for_count(
    *,
    sm_name: str,
    sm: dict[str, Any],
    expr: Any,
    graph: dict[str, Any],
) -> str | None:
    """Pick the graph entity that an entity_count measure should bind to.

    Order of preference:
      1. ``expr: 1`` (or 1) -> the model's own primary entity, since the
         measure is counting rows at the model's grain.
      2. ``expr: <column>`` matching a graph entity's primary key column
         -> that entity. This covers `expr: order_id, agg: count_distinct`.
      3. ``expr: <column>`` matching a known entity expression on the
         current model (e.g. an FK column) and that entity exists in
         the graph -> that entity.
      4. None — caller drops the measure with a warning.
    """
    entities = graph["entities"]

    # 1) row-count style: bind to the model's primary entity.
    if expr in ("1", 1, None):
        for ename, meta in entities.items():
            if meta.get("model") == sm_name:
                return ename

    # 2) column matches a graph entity's primary key column.
    if isinstance(expr, str):
        for ename, meta in entities.items():
            key_list = meta.get("key", [])
            if isinstance(key_list, list) and expr in key_list:
                return ename
            if isinstance(key_list, str) and expr == key_list:
                return ename

    # 3) column matches an FK expression on the current model.
    if isinstance(expr, str):
        for ent_ref in sm.get("entities") or []:
            if ent_ref.get("expr") == expr and ent_ref.get("name") in entities:
                return ent_ref["name"]

    return None


def _grains_at_or_above(granularity: str) -> list[str]:
    """Return the standard set of grains at or above the declared
    base granularity. MetricFlow's `time_granularity` is the finest
    grain the column supports; Semantic Rails expects the full ladder
    so the planner can roll up freely."""
    ladder = ["day", "week", "month", "quarter", "year"]
    if granularity in ladder:
        idx = ladder.index(granularity)
        return ladder[idx:]
    # sub-daily — surface day-and-above so most queries still work.
    return ladder


def _infer_dimension_kind(name: str) -> str:
    """Map a dimension name to a Semantic Rails `kind:` using
    structural heuristics — `is_*`, `has_*` -> boolean; numeric-looking
    suffixes -> integer; everything else -> categorical."""
    lowered = name.lower()
    if lowered.startswith("is_") or lowered.startswith("has_"):
        return "boolean"
    if lowered.endswith("_count") or lowered.endswith("_number"):
        return "integer"
    return "categorical"


def _infer_value_type_and_accumulation(measure_name: str, agg: str) -> tuple[str, str]:
    """Best-effort default for `value_type:` and `accumulation.kind:`.

    Currency and count cues come from naming. Aggregation kind drives
    accumulation: counts of events are `event`; sums of dollar amounts
    are `flow`; min/max/avg of balances default to `stock` since
    snapshot-style measures rarely sum across time.
    """
    lowered = measure_name.lower()
    if "_usd" in lowered or "revenue" in lowered or "_dollars" in lowered or "_cents" in lowered:
        value_type = "currency"
    elif lowered.endswith("_count") or "count" in lowered or agg == "count_distinct":
        value_type = "count"
    elif agg in ("min", "max", "median", "percentile", "avg"):
        value_type = "number"
    else:
        value_type = "number"

    if agg in ("sum",) and value_type != "count":
        accumulation = "flow"
    elif agg in ("count", "count_distinct"):
        accumulation = "event"
    elif agg in ("min", "max"):
        accumulation = "stock"
    else:
        accumulation = "flow"
    return value_type, accumulation


# ---------------------------------------------------------------------------
# Metric translation
# ---------------------------------------------------------------------------


def _normalize_metric_ref(raw: Any) -> dict[str, Any]:
    """MetricFlow accepts both `numerator: foo` (string shorthand) and
    `numerator: {name: foo, filter: ..., alias: ...}` (object form).
    Normalize to the object form so the caller can treat both
    uniformly."""
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {"name": raw}
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _filter_strings(raw: Any) -> list[str]:
    """A MetricFlow filter as its separate conditions, which MetricFlow ANDs.

    Authoring YAML holds a string or a list of strings; the parsed
    ``semantic_manifest.json`` wraps them as
    ``{where_filters: [{where_sql_template: "<jinja>"}]}``.
    """
    if isinstance(raw, dict):
        raw = [
            clause.get("where_sql_template")
            for clause in raw.get("where_filters") or []
            if isinstance(clause, dict)
        ]
    items = raw if isinstance(raw, list) else [raw]
    return [item.strip() for item in items if isinstance(item, str) and item.strip()]


def _build_metric(
    metric: dict[str, Any],
    *,
    measure_owner: dict[str, str],
    measure_value_type: dict[str, str],
    measure_agg: dict[str, str],
    dimension_ids: dict[str, str | None],
    running_total_problems: dict[str, str],
    source_metrics: dict[str, dict[str, Any]],
    report: TranslationReport,
) -> tuple[str, dict[str, Any], str] | None:
    """Translate a MetricFlow metric. Returns
    ``(name, doc, owner_hint)`` where `owner_hint` is the source
    semantic_model name to use for grouping output files."""
    name = metric.get("name")
    if not name:
        report.warnings.append("metric without `name` — skipped")
        return None
    mtype = (metric.get("type") or "simple").lower()
    type_params = metric.get("type_params") or {}
    metric_filters = _filter_strings(metric.get("filter"))
    # Semantic Rails marks every published metric as "curated" and
    # requires a non-empty description. MetricFlow allows blank
    # descriptions; fall back to the label so the validator passes.
    description = (metric.get("description") or "").strip()
    label = metric.get("label") or _humanize(name)
    if not description:
        description = label

    if mtype == "simple":
        measure_ref = _normalize_metric_ref(type_params.get("measure"))
        measure = measure_ref.get("name")
        if not measure:
            report.warnings.append(
                f"metric `{name}`: simple metric missing `type_params.measure.name`"
            )
            return None
        owner = measure_owner.get(measure, "core")
        vt = measure_value_type.get(measure, "number")
        spec, problem = _metric_filter(
            [*metric_filters, *_filter_strings(measure_ref.get("filter"))], dimension_ids
        )
        if problem:
            report.warnings.append(f"metric `{name}`: {problem}; skipped")
            return None
        if spec is None:
            doc = _aggregate_metric_doc(label, description, measure, vt)
        else:
            # A filtered aggregate is written as the expression AST.
            doc = {
                "label": label,
                "description": description,
                "kind": "aggregate",
                "value_type": vt,
                "expression": _aggregate_ast(measure, spec, measure_agg),
            }
        return name, doc, owner

    if mtype == "ratio":
        num = _normalize_metric_ref(type_params.get("numerator"))
        den = _normalize_metric_ref(type_params.get("denominator"))
        num_name = num.get("name")
        den_name = den.get("name")
        if not num_name or not den_name:
            report.warnings.append(f"metric `{name}`: ratio missing numerator/denominator")
            return None
        # MetricFlow applies the metric's filter to both sides, each ANDed
        # with its own input filter. A filtered side needs the expression AST.
        specs: list[dict[str, Any] | None] = []
        problem = ""
        for side in (num, den):
            spec, side_problem = _metric_filter(
                [*metric_filters, *_filter_strings(side.get("filter"))], dimension_ids
            )
            specs.append(spec)
            problem = problem or side_problem
        if problem:
            # Without its filters a ratio can divide a measure by itself.
            report.warnings.append(
                f"metric `{name}`: {problem}; skipped, since a ratio without its filters is a different ratio"
            )
            return None
        num_spec, den_spec = specs
        if num_spec or den_spec:
            operands = []
            for side_name, side_spec in ((num_name, num_spec), (den_name, den_spec)):
                if side_spec is None and side_name in source_metrics:
                    operands.append({"kind": "metric", "metric": side_name})
                    continue
                if side_spec is not None and side_name in source_metrics:
                    source_metric = source_metrics[side_name]
                    source_params = source_metric.get("type_params") or {}
                    source_measure = _normalize_metric_ref(source_params.get("measure"))
                    if (
                        (source_metric.get("type") or "simple").lower() != "simple"
                        or source_measure.get("name") != side_name
                        or _filter_strings(source_metric.get("filter"))
                        or _filter_strings(source_measure.get("filter"))
                    ):
                        report.warnings.append(
                            f"metric `{name}`: cannot apply a ratio-side filter to source "
                            f"metric `{side_name}` without changing its meaning; skipped"
                        )
                        return None
                operands.append(_aggregate_ast(side_name, side_spec, measure_agg))
            doc = {
                "label": label,
                "description": description,
                "kind": "derived",
                "value_type": measure_value_type.get(num_name, "number"),
                "expression": {
                    "kind": "arithmetic",
                    "op": "divide",
                    "left": operands[0],
                    "right": operands[1],
                    "null_behavior": "null_if_zero",
                },
            }
        else:
            doc = {
                "label": label,
                "description": description,
                "kind": "ratio",
                "numerator": num_name,
                "denominator": den_name,
                "null_behavior": "null_if_zero",
                "value_type": measure_value_type.get(num_name, "number"),
            }
        owner = measure_owner.get(num_name, "core")
        return name, doc, owner

    if mtype == "cumulative":
        measure_ref = _normalize_metric_ref(type_params.get("measure"))
        measure = measure_ref.get("name")
        if not measure:
            report.warnings.append(
                f"metric `{name}`: cumulative missing `type_params.measure.name`"
            )
            return None
        shape, problem = _cumulative_shape(type_params)
        if problem:
            report.warnings.append(f"metric `{name}`: {problem}; skipped")
            return None
        if measure in running_total_problems:
            report.warnings.append(
                f"metric `{name}`: Semantic Rails adds up each period's value of `{measure}`, "
                f"which {running_total_problems[measure]}, so its totals would be wrong; skipped"
            )
            return None
        cumulative_params = dict(type_params.get("cumulative_type_params") or {})
        period_agg = str(cumulative_params.get("period_agg") or "first").strip().lower()
        caveats = _cumulative_caveats(shape, period_agg)
        if caveats:
            report.warnings.append(f"metric `{name}`: {'; '.join(caveats)}")
        spec, problem = _metric_filter(
            [*metric_filters, *_filter_strings(measure_ref.get("filter"))], dimension_ids
        )
        if problem:
            report.warnings.append(f"metric `{name}`: {problem}; skipped")
            return None
        doc = {"label": label, "description": description, "kind": shape["kind"]}
        if spec is None:
            doc.update({"measure": measure, **{k: v for k, v in shape.items() if k != "kind"}})
        else:
            doc["expression"] = {
                **shape,
                "input": _aggregate_ast(measure, spec, measure_agg),
            }
        doc["value_type"] = measure_value_type.get(measure, "number")
        return name, doc, measure_owner.get(measure, "core")

    if mtype == "derived":
        expr_str = type_params.get("expr")
        input_metrics = type_params.get("metrics") or []
        if not expr_str:
            report.warnings.append(f"metric `{name}`: derived metric missing `type_params.expr`")
            return None
        input_metrics = [_normalize_metric_ref(im) for im in input_metrics]
        offsets = [
            f"`{im.get('alias') or im.get('name')}` ({key})"
            for im in input_metrics
            for key in ("offset_window", "offset_to_grain")
            if im.get(key)
        ]
        if offsets:
            report.warnings.append(
                f"metric `{name}`: input {', '.join(offsets)} reads another period, which "
                "mf2sr doesn't translate; skipped rather than computed over the same period"
            )
            return None
        filtered = [
            f"`{im.get('alias') or im.get('name')}`"
            for im in input_metrics
            if _filter_strings(im.get("filter"))
        ]
        if filtered:
            report.warnings.append(
                f"metric `{name}`: mf2sr doesn't carry the filters on its inputs "
                f"({', '.join(filtered)}) into a derived metric; skipped rather than computed "
                "unfiltered"
            )
            return None
        if metric_filters:
            report.warnings.append(
                f"metric `{name}`: mf2sr doesn't carry a filter into a derived metric; "
                "skipped rather than computed unfiltered"
            )
            return None
        alias_map = {}
        for im in input_metrics:
            base = im.get("name")
            alias = im.get("alias") or base
            if alias and base:
                alias_map[alias] = base
        ast_expr = _parse_derived_expression(expr_str, alias_map, report, name)
        if ast_expr is None:
            # As a last resort, emit a placeholder kind:aggregate over
            # the first input metric's measure with the formula in the
            # description. Caller will see the warning and edit it.
            fallback = input_metrics[0].get("name") if input_metrics else None
            if not fallback:
                report.warnings.append(
                    f"metric `{name}`: could not parse derived expression "
                    f"`{expr_str}` and no input metrics — skipping."
                )
                return None
            report.warnings.append(
                f"metric `{name}`: could not parse derived expression "
                f"`{expr_str}` — emitted as aggregate over `{fallback}` "
                "with the formula stored in `description`. Author should "
                "rewrite by hand."
            )
            vt = measure_value_type.get(fallback, "number")
            doc = _aggregate_metric_doc(
                label,
                f"{description}\n\nOriginal MetricFlow derived expr: {expr_str}",
                fallback,
                vt,
            )
            owner = measure_owner.get(fallback, "core")
            return name, doc, owner
        # Pick value_type from the first input metric's owning measure.
        first_input = input_metrics[0].get("name") if input_metrics else None
        vt = measure_value_type.get(first_input, "number") if first_input else "number"
        owner = measure_owner.get(first_input, "core") if first_input else "core"
        doc = {
            "label": label,
            "description": description,
            "kind": "derived",
            "value_type": vt,
            "expression": ast_expr,
        }
        return name, doc, owner

    if mtype == "conversion":
        # MetricFlow conversion semantics map to Semantic Rails
        # `kind: conversion`. The Semantic Rails surface differs enough
        # that we emit the metric with the MetricFlow type_params
        # intact and surface a warning — the author should adapt.
        report.warnings.append(
            f"metric `{name}`: `type: conversion` translated as a "
            "best-effort stub. Review the emitted metric and adapt to "
            "the Semantic Rails conversion shape (event-pair matching)."
        )
        doc = {
            "label": label,
            "description": (
                description + "\n\n[mf2sr] MetricFlow conversion metric — review and complete."
            ),
            "kind": "conversion",
            "value_type": "number",
            "type_params": type_params,
        }
        return name, doc, "conversions"

    report.warnings.append(f"metric `{name}`: unsupported type `{mtype}` — skipped.")
    return None


def _aggregate_metric_doc(
    label: str, description: str, measure: str, value_type: str
) -> dict[str, Any]:
    return {
        "label": label,
        "description": description,
        "kind": "aggregate",
        "measure": measure,
        "value_type": value_type,
    }


def _aggregate_ast(
    measure: str, spec: dict[str, Any] | None, measure_agg: dict[str, str]
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "kind": "aggregate",
        "measure": measure,
        "aggregation": measure_agg.get(measure, "sum"),
    }
    if spec:
        node["filter"] = spec
    return node


def _metric_filter(
    filters: list[str], dimension_ids: dict[str, str | None]
) -> tuple[dict[str, Any] | None, str]:
    """MetricFlow filter conditions, ANDed, as the filter the engine applies, or why not.

    That filter is ``{all: [{field, op, value}]}``. When one condition can't
    be written that way, the caller skips the metric and warns with the
    reason returned.
    """
    clauses: list[dict[str, Any]] = []
    for text in filters:
        found, problem = filter_clauses(text, dimension_ids)
        if problem:
            return None, problem
        clauses.extend(found)
    return ({"all": clauses} if clauses else None), ""


# MetricFlow's time granularities, and the ones the engine's windowed kinds use.
_METRICFLOW_GRAINS = frozenset(
    {
        "nanosecond",
        "microsecond",
        "millisecond",
        "second",
        "minute",
        "hour",
        "day",
        "week",
        "month",
        "quarter",
        "year",
    }
)
_WINDOW_UNITS = ("day", "week", "month", "quarter", "year")
_TO_DATE_PERIODS = ("week", "month", "quarter", "year")


def _cumulative_caveats(shape: dict[str, Any], period_agg: str) -> list[str]:
    """Where a translated cumulative metric's values can differ from MetricFlow's.

    At the time dimension's own grain they match. The engine reads a coarser
    period's value at its end, and counts its windows in whole query-grain
    periods.
    """
    caveats = []
    if period_agg != "last":
        caveats.append(
            f"at grains coarser than its time dimension's, MetricFlow reports each period's "
            f"{period_agg} value (period_agg: {period_agg}), and Semantic Rails the value at "
            "the period's end"
        )
    period = shape.get("period")
    if shape["kind"] == "period_to_date" and period != "week":
        caveats.append(
            f"at week grain Semantic Rails counts each week toward the {period} it starts in, "
            f"so a week that crosses into a new {period} differs from MetricFlow"
        )
    unit = dict(shape.get("window") or {}).get("unit")
    if shape["kind"] == "rolling" and unit in {"month", "quarter", "year"}:
        caveats.append(
            f"Semantic Rails sums whole calendar {unit}s, while MetricFlow's window reaches "
            f"back from each day, so values near a {unit}'s end can differ"
        )
    return caveats


def _running_total_problems(
    sm: dict[str, Any], model_doc: dict[str, Any], graph: dict[str, Any]
) -> dict[str, str]:
    """Why each of a model's measures doesn't add up across periods, by measure name.

    The engine builds cumulative, rolling and period-to-date totals by adding
    each period's value, which is right only when the periods add up: sums,
    and counts of the model's own rows.
    """
    own_keys = {
        str(spec["key"][0])
        for spec in graph["entities"].values()
        if spec.get("model") == sm.get("name") and spec.get("key")
    }
    source = {measure.get("name"): measure for measure in sm.get("measures") or []}
    problems: dict[str, str] = {}
    for name, doc in dict(model_doc.get("measures") or {}).items():
        measure = dict(source.get(name) or {})
        if measure.get("non_additive_dimension"):
            problems[name] = "is semi-additive (non_additive_dimension)"
        elif doc.get("kind") == "entity_count":
            if doc.get("entity_key") not in own_keys:
                problems[name] = f"counts distinct {doc.get('entity_key')} values"
        elif str(measure.get("agg") or "").lower() == "count_distinct":
            problems[name] = "counts distinct values"
        elif doc.get("default_agg") != "sum":
            problems[name] = f"aggregates with {doc.get('default_agg')}"
    return problems


def _drop_dependents(
    metrics_by_owner: dict[str, list[tuple[str, dict[str, Any]]]],
    metric_dependencies: dict[str, set[str]],
    report: TranslationReport,
) -> None:
    """Drop metrics whose explicit source metric inputs mf2sr skipped."""
    skipped = metric_dependencies.keys() - set(report.metrics_emitted)
    while True:
        dropped = []
        for entries in metrics_by_owner.values():
            for name, doc in list(entries):
                # Source refs retain ratio identity through lowering. Scan
                # emitted expressions as a fallback for derived formulas
                # that omitted their MetricFlow ``metrics`` input list.
                refs = metric_dependencies.get(name, set()) | _expression_metric_refs(
                    doc.get("expression")
                )
                missing = sorted(refs & skipped)
                if missing:
                    report.warnings.append(
                        f"metric `{name}`: it uses {', '.join(f'`{m}`' for m in missing)}, "
                        "which mf2sr skipped; skipped too"
                    )
                    entries.remove((name, doc))
                    report.metrics_emitted.remove(name)
                    dropped.append(name)
        if not dropped:
            return
        skipped.update(dropped)


def _source_metric_refs(metric: dict[str, Any], metric_names: set[str]) -> set[str]:
    """Explicit metric inputs in MetricFlow's source definition.

    Simple and cumulative ``measure`` inputs are measures even when a metric
    shares their name. Ratio inputs resolve to an explicit metric first, then
    to a measure. Derived inputs are metrics by definition.
    """
    params = metric.get("type_params") or {}
    mtype = (metric.get("type") or "simple").lower()
    if mtype == "ratio":
        inputs = [params.get("numerator"), params.get("denominator")]
    elif mtype == "derived":
        inputs = params.get("metrics") or []
    else:
        return set()
    return {
        name for item in inputs if (name := _normalize_metric_ref(item).get("name")) in metric_names
    }


def _expression_metric_refs(expression: Any) -> set[str]:
    refs: set[str] = set()
    pending = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if node.get("kind") == "metric" and node.get("metric"):
                refs.add(str(node["metric"]))
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return refs


def _cumulative_shape(type_params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The engine kind that computes a MetricFlow cumulative metric, or why none does.

    No window or grain: a running total (``cumulative``). ``window: 7 days``: a
    trailing window (``rolling``). ``grain_to_date: month``: a running total that
    restarts each month (``period_to_date``). As in MetricFlow,
    ``cumulative_type_params`` wins over the older top-level fields.
    """
    params = dict(type_params.get("cumulative_type_params") or {})
    window = params.get("window") or type_params.get("window")
    grain = params.get("grain_to_date") or type_params.get("grain_to_date")
    if window and grain:
        return {}, "it has both a window and grain_to_date, which MetricFlow rejects"
    if grain:
        period = str(grain).strip().lower()
        if period not in _TO_DATE_PERIODS:
            return {}, (
                f"{period}-to-date isn't computed; Semantic Rails computes week-, month-, "
                "quarter- and year-to-date"
            )
        return {"kind": "period_to_date", "period": period}, ""
    if window:
        parsed = _time_window(window)
        if parsed is None:
            return {}, f"its window {window!r} isn't `<count> <granularity>`"
        count, unit = parsed
        if unit not in _WINDOW_UNITS:
            return {}, (
                f"{unit} windows aren't computed; Semantic Rails windows are days, weeks, "
                "months, quarters or years"
            )
        return {"kind": "rolling", "window": {"unit": unit, "value": count}}, ""
    return {"kind": "cumulative"}, ""


def _time_window(raw: Any) -> tuple[int, str] | None:
    """``7 days`` (YAML) or ``{count: 7, granularity: day}`` (manifest) as ``(7, "day")``."""
    if isinstance(raw, dict):
        count, grain = raw.get("count"), raw.get("granularity")
    elif isinstance(raw, str) and len(raw.split()) == 2:
        count, grain = raw.split()
    else:
        return None
    count_text = str(count).strip()
    unit = str(grain or "").strip().lower()
    if not count_text.isdigit() or int(count_text) < 1 or not unit:
        return None
    if unit.endswith("s") and unit[:-1] in _METRICFLOW_GRAINS:
        unit = unit[:-1]
    return int(count_text), unit


def _dimension_ids(
    semantic_models: list[dict[str, Any]], graph: dict[str, Any], namespace: str
) -> dict[str, str | None]:
    """MetricFlow ``entity__dimension`` references, by the dimension id the loader gives each.

    A dimension belongs to its model's graph entity, and the loader names it
    ``dimension.<namespace>_<entity>_<dimension>``. A time dimension maps to
    ``None``: in a filter, MetricFlow compares it truncated to its grain, and
    its Semantic Rails dimension holds the raw column.
    """
    entity_of = {spec["model"]: entity for entity, spec in graph["entities"].items()}
    ids: dict[str, str | None] = {}
    for sm in semantic_models:
        entity = entity_of.get(str(sm.get("name")))
        if entity is None:
            continue
        for dim in sm.get("dimensions") or []:
            if dim.get("name"):
                ids[f"{entity}__{dim['name']}"] = (
                    None
                    if str(dim.get("type") or "").lower() == "time"
                    else f"dimension.{namespace}_{_id_part(entity)}_{_id_part(dim['name'])}"
                )
    return ids


def _id_part(value: str) -> str:
    """One part of a loader-made id: lowercase words joined by single underscores."""
    words = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).split("_")
    return "_".join(word for word in words if word)


# ---------------------------------------------------------------------------
# Derived-expression parser (Python AST -> Semantic Rails AST)
# ---------------------------------------------------------------------------

_BINOP_MAP = {
    pyast.Add: "add",
    pyast.Sub: "subtract",
    pyast.Mult: "multiply",
    pyast.Div: "divide",
}


def _parse_derived_expression(
    expr: str,
    alias_map: dict[str, str],
    report: TranslationReport,
    metric_name: str,
) -> dict[str, Any] | None:
    """Parse a MetricFlow derived `expr:` (e.g. `bookings * 1.0 / bookers`)
    into the Semantic Rails arithmetic AST.

    We handle the cases that appear in real MetricFlow projects: nested
    binary arithmetic over metric names (with optional aliases) and
    numeric literals, plus a single special-cased `NULLIF(x, 0)` form
    that maps to `null_behavior: null_if_zero` on the parent divide.
    Anything else returns None and the caller picks a fallback.
    """
    # Normalize `NULLIF(x, 0)` so Python's parser doesn't trip on it.
    # We replace `NULLIF(x, 0)` with `__nullif_zero__(x)` and recognize
    # that function as a marker to set null_behavior on the parent.
    normalized = expr.strip()
    normalized = _rewrite_nullif_zero(normalized)
    try:
        tree = pyast.parse(normalized, mode="eval")
    except SyntaxError:
        return None
    return _to_semantic_ast(tree.body, alias_map, report, metric_name)


def _rewrite_nullif_zero(expr: str) -> str:
    """`NULLIF(x, 0)` -> `__nullif_zero__(x)` for any `x`. Case-insensitive."""
    import re as _re

    return _re.sub(
        r"NULLIF\s*\(\s*([^,]+?)\s*,\s*0\s*\)",
        r"__nullif_zero__(\1)",
        expr,
        flags=_re.IGNORECASE,
    )


def _to_semantic_ast(
    node: pyast.AST,
    alias_map: dict[str, str],
    report: TranslationReport,
    metric_name: str,
) -> dict[str, Any] | None:
    if isinstance(node, pyast.BinOp):
        op_type = type(node.op)
        if op_type not in _BINOP_MAP:
            return None
        left = _to_semantic_ast(node.left, alias_map, report, metric_name)
        right = _to_semantic_ast(node.right, alias_map, report, metric_name)
        if left is None or right is None:
            return None
        result: dict[str, Any] = {
            "kind": "arithmetic",
            "op": _BINOP_MAP[op_type],
            "left": left,
            "right": right,
        }
        # If divisor is wrapped in NULLIF(_, 0), promote to null_if_zero.
        if op_type is pyast.Div and right.get("__nullif_zero__"):
            result["null_behavior"] = "null_if_zero"
            right.pop("__nullif_zero__", None)
        return result
    if isinstance(node, pyast.UnaryOp) and isinstance(node.op, pyast.USub):
        inner = _to_semantic_ast(node.operand, alias_map, report, metric_name)
        if inner is None:
            return None
        return {
            "kind": "arithmetic",
            "op": "subtract",
            "left": {"kind": "literal", "value": 0},
            "right": inner,
        }
    if isinstance(node, pyast.Constant) and isinstance(node.value, (int, float)):
        return {"kind": "literal", "value": node.value}
    if isinstance(node, pyast.Name):
        canonical = alias_map.get(node.id, node.id)
        return {"kind": "metric", "metric": canonical}
    if (
        isinstance(node, pyast.Call)
        and isinstance(node.func, pyast.Name)
        and node.func.id == "__nullif_zero__"
        and len(node.args) == 1
    ):
        inner = _to_semantic_ast(node.args[0], alias_map, report, metric_name)
        if inner is None:
            return None
        inner = dict(inner)
        inner["__nullif_zero__"] = True
        return inner
    return None


# ---------------------------------------------------------------------------
# YAML I/O
# ---------------------------------------------------------------------------


def _write_package_yml(
    out_root: Path,
    *,
    package_id: str,
    namespace: str,
    warehouse: str,
    default_db: str | None,
    description: str | None,
    schema_strict: bool,
) -> None:
    # Without --schema-strict the package is `schema_strict: false`, so a
    # project whose relations or types need review still loads; strict mode
    # parse-checks the output instead.
    pkg: dict[str, Any] = {
        "id": package_id,
        "namespace": namespace,
        "warehouse": warehouse,
        "schema_strict": schema_strict,
        "environments": ["development", "staging", "production"],
    }
    if description:
        pkg["description"] = description
    if warehouse == "duckdb":
        pkg["default_db"] = default_db or f"data/{package_id}.duckdb"
        # DuckDB packages require a seed block. We emit a placeholder
        # `sql_script` seed pointing to a file the author will create.
        # Without this the loader rejects the package outright.
        pkg["seed"] = (
            {"kind": "external"}  # a strict package reads the database dbt built
            if schema_strict
            else {"kind": "sql_script", "source": f"data/seed_{package_id}.sql"}
        )
    elif warehouse == "snowflake":
        # Snowflake packages need a connection block. We emit the
        # env-var-indirection shape so the YAML is safe to commit; the
        # author wires up the real env vars before running queries.
        pkg["connection"] = {
            "kind": "snowflake_native",
            "name": f"{package_id}_native",
            "options": {
                "account_env": "SNOWFLAKE_ACCOUNT",
                "user_env": "SNOWFLAKE_USER",
                "password_env": "SNOWFLAKE_PASSWORD",
                "warehouse": "COMPUTE_WH",
                "query_tag": f"mf2sr-{package_id}",
            },
        }
    body = {
        "schema_version": 1,
        "package": pkg,
        "defaults": {
            "dimension": {"groupable": True, "filterable": True},
            "time": {
                "timezone": "UTC",
                "supported_grains": ["day", "week", "month", "quarter", "year"],
            },
        },
    }
    (out_root / "package.yml").write_text(_dump_yaml(body))


def _write_graph_yml(out_root: Path, graph: dict[str, Any]) -> None:
    payload = {"entities": graph["entities"]}
    (out_root / "graph.yml").write_text(_dump_yaml({"graph": payload}))


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        width=100,
        indent=2,
    )


def _humanize(name: str) -> str:
    """`weekly_active_users` -> `Weekly active users`. Used for default
    labels when MetricFlow doesn't supply one."""
    return name.replace("_", " ").strip().capitalize()
