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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import parsers
from .filter_parser import parse_filter

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


def translate(
    source: Path | str,
    output_dir: Path | str,
    *,
    package_id: str,
    namespace: str | None = None,
    warehouse: str = "duckdb",
    default_db: str | None = None,
    description: str | None = None,
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
    """
    namespace = namespace or package_id
    src = Path(source)
    out_root = Path(output_dir) / package_id
    out_root.mkdir(parents=True, exist_ok=True)

    raw = parsers.load(src)
    report = TranslationReport(package_dir=out_root)

    graph = _build_graph(raw["semantic_models"], report)
    _write_package_yml(
        out_root,
        package_id=package_id,
        namespace=namespace,
        warehouse=warehouse,
        default_db=default_db,
        description=description,
    )
    _write_graph_yml(out_root, graph)

    measure_owner: dict[str, str] = {}
    measure_to_value_type: dict[str, str] = {}
    measure_to_agg: dict[str, str] = {}
    # Collect explicit metric names so we can suppress measure
    # auto-publish for any measure whose name will collide with a
    # metric we author explicitly. Without `publish: false` Semantic
    # Rails auto-creates a measure-derived metric and the explicit
    # metric becomes a duplicate ID.
    metric_names: set[str] = {m["name"] for m in raw["metrics"] if m.get("name")}

    owning_models: set[str] = graph.get("_owning_models", set())
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
            sm, graph, report, suppress_publish=metric_names
        )
        (models_dir / f"{name}.yml").write_text(_dump_yaml({"model": model_doc}))
        report.models_emitted.append(name)
        for measure_name, vt, default_agg in measures_in_model:
            measure_owner[measure_name] = name
            measure_to_value_type[measure_name] = vt
            measure_to_agg[measure_name] = default_agg

    metrics_by_owner: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for metric in raw["metrics"]:
        translated = _build_metric(
            metric,
            measure_owner=measure_owner,
            measure_value_type=measure_to_value_type,
            measure_agg=measure_to_agg,
            report=report,
        )
        if translated is None:
            continue
        metric_name, metric_doc, owner_hint = translated
        metrics_by_owner.setdefault(owner_hint, []).append((metric_name, metric_doc))
        report.metrics_emitted.append(metric_name)

    if metrics_by_owner:
        metrics_dir = out_root / "metrics"
        metrics_dir.mkdir(exist_ok=True)
        for owner, entries in sorted(metrics_by_owner.items()):
            grouped = {name: doc for name, doc in entries}
            (metrics_dir / f"{owner}.yml").write_text(_dump_yaml({"metrics": grouped}))

    return report


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
) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
    """Build the Semantic Rails `model:` body for one MetricFlow
    semantic_model. Returns `(model_doc, measures)` where `measures` is
    a list of `(name, value_type, default_agg)` for the metric-stage
    translator to consult. Returns an empty list if the model has no
    primary entity and no measures (skipped).
    """
    name = sm["name"]
    relation = (sm.get("node_relation") or {}).get("alias") or name
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


def _coerce_filter_string(raw: Any) -> str:
    """MetricFlow's authoring YAML stores filters as plain strings, but
    the parsed `semantic_manifest.json` wraps them as
    ``{where_filters: [{where_sql_template: "<jinja>"}]}``. Both forms
    arrive at the same end state — a Jinja-templated string — so we
    coerce to a single string here. Multiple where_filters are joined
    with ``AND`` because that's MetricFlow's semantics.
    """
    if raw in (None, ""):
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        clauses = raw.get("where_filters") or []
        if not clauses:
            return ""
        parts = [c.get("where_sql_template", "") for c in clauses if isinstance(c, dict)]
        parts = [p.strip() for p in parts if p and p.strip()]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return " AND ".join(f"({p})" for p in parts)
    return ""


def _build_metric(
    metric: dict[str, Any],
    *,
    measure_owner: dict[str, str],
    measure_value_type: dict[str, str],
    measure_agg: dict[str, str],
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
    filter_str = _coerce_filter_string(metric.get("filter"))
    # Semantic Rails marks every published metric as "curated" and
    # requires a non-empty description. MetricFlow allows blank
    # descriptions; fall back to the label so the validator passes.
    description = (metric.get("description") or "").strip()
    label = metric.get("label") or _humanize(name)
    if not description:
        description = label

    if mtype == "simple":
        measure = _normalize_metric_ref(type_params.get("measure")).get("name")
        if not measure:
            report.warnings.append(
                f"metric `{name}`: simple metric missing `type_params.measure.name`"
            )
            return None
        owner = measure_owner.get(measure, "core")
        vt = measure_value_type.get(measure, "number")
        if filter_str:
            filt = parse_filter(filter_str)
            if filt is None:
                report.warnings.append(
                    f"metric `{name}`: could not parse filter `{filter_str!r}` — "
                    "emitting unfiltered metric."
                )
                doc = _aggregate_metric_doc(label, description, measure, vt)
            else:
                # Filtered aggregate must use the expression AST.
                doc = {
                    "label": label,
                    "description": description,
                    "kind": "aggregate",
                    "value_type": vt,
                    "expression": {
                        "kind": "aggregate",
                        "measure": measure,
                        "aggregation": measure_agg.get(measure, "sum"),
                        "filter": filt,
                    },
                }
        else:
            doc = _aggregate_metric_doc(label, description, measure, vt)
        return name, doc, owner

    if mtype == "ratio":
        num = _normalize_metric_ref(type_params.get("numerator"))
        den = _normalize_metric_ref(type_params.get("denominator"))
        num_name = num.get("name")
        den_name = den.get("name")
        if not num_name or not den_name:
            report.warnings.append(f"metric `{name}`: ratio missing numerator/denominator")
            return None
        # Filters on either side force the derived-AST path.
        num_filter = _coerce_filter_string(num.get("filter"))
        den_filter = _coerce_filter_string(den.get("filter"))
        if num_filter or den_filter or filter_str:
            report.warnings.append(
                f"metric `{name}`: ratio uses per-side filters; emitting "
                "as `kind: derived` with the filter folded into a "
                "filtered aggregate. If the filter could not be parsed, "
                "the metric is emitted unfiltered with a warning."
            )
            num_expr = _filtered_aggregate_ast(num, measure_agg, report, name)
            den_expr = _filtered_aggregate_ast(den, measure_agg, report, name)
            doc = {
                "label": label,
                "description": description,
                "kind": "derived",
                "value_type": measure_value_type.get(num_name, "number"),
                "expression": {
                    "kind": "arithmetic",
                    "op": "divide",
                    "left": num_expr,
                    "right": den_expr,
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
        measure = _normalize_metric_ref(type_params.get("measure")).get("name")
        if not measure:
            report.warnings.append(
                f"metric `{name}`: cumulative missing `type_params.measure.name`"
            )
            return None
        owner = measure_owner.get(measure, "core")
        vt = measure_value_type.get(measure, "number")
        doc = {
            "label": label,
            "description": description,
            "kind": "cumulative",
            "measure": measure,
            "value_type": vt,
        }
        # Window can live at the top of type_params (legacy) or under
        # cumulative_type_params (current). Forward whichever exists.
        cum_params = type_params.get("cumulative_type_params") or {}
        window = type_params.get("window") or cum_params.get("window")
        grain_to_date = cum_params.get("grain_to_date")
        if window:
            doc["window"] = window
        if grain_to_date:
            doc["grain_to_date"] = grain_to_date
        return name, doc, owner

    if mtype == "derived":
        expr_str = type_params.get("expr")
        input_metrics = type_params.get("metrics") or []
        if not expr_str:
            report.warnings.append(f"metric `{name}`: derived metric missing `type_params.expr`")
            return None
        input_metrics = [_normalize_metric_ref(im) for im in input_metrics]
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


def _filtered_aggregate_ast(
    side: dict[str, Any],
    measure_agg: dict[str, str],
    report: TranslationReport,
    metric_name: str,
) -> dict[str, Any]:
    """Build an AST node representing one side of a ratio with an
    optional filter. Falls back to an unfiltered measure ref when the
    filter cannot be parsed."""
    measure = side.get("name")
    if not measure:
        return {"kind": "literal", "value": 0}
    base: dict[str, Any] = {
        "kind": "aggregate",
        "measure": measure,
        "aggregation": measure_agg.get(measure, "sum"),
    }
    filter_str = _coerce_filter_string(side.get("filter"))
    if filter_str:
        filt = parse_filter(filter_str)
        if filt is not None:
            base["filter"] = filt
        else:
            report.warnings.append(
                f"metric `{metric_name}`: ratio side filter "
                f"`{filter_str!r}` not parseable — side emitted unfiltered."
            )
    return base


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
) -> None:
    # The translator emits `schema_strict: false` because MetricFlow
    # measure metadata is too thin to satisfy strict checks out of the
    # gate (most measures lack a meaningful `value_type:` distinction,
    # so ratio/derived metrics fall back to `number` and strict mode
    # rejects them). The author should flip this to `true` after
    # reviewing measure value_types and adding business meaning.
    pkg: dict[str, Any] = {
        "id": package_id,
        "namespace": namespace,
        "warehouse": warehouse,
        "schema_strict": False,
        "environments": ["development", "staging", "production"],
    }
    if description:
        pkg["description"] = description
    if warehouse == "duckdb":
        pkg["default_db"] = default_db or f"data/{package_id}.duckdb"
        # DuckDB packages require a seed block. We emit a placeholder
        # `sql_script` seed pointing to a file the author will create.
        # Without this the loader rejects the package outright.
        pkg["seed"] = {
            "kind": "sql_script",
            "source": f"data/seed_{package_id}.sql",
        }
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
