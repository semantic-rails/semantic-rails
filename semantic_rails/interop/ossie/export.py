"""Export a package as an Apache Ossie 0.1.1 document plus a Semantic Rails sidecar.

Ossie 0.1.1 has no extension slot for Semantic Rails (``vendor_name`` is a closed enum), so
whatever the document can't carry goes into ``<package-id>.semantic_rails.json`` beside it, and
every such construct is counted in a warning: nothing is dropped silently. A metric, measure or
relationship whose meaning Ossie can't state is left out of the document and kept whole in the
sidecar rather than approximated, so a consumer never computes a different number under its
name. The export covers the semantic model, not deployment settings (connection, seed, default
database) or the package's examples and tests.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

import yaml

from ... import __version__
from ...config import LoadedPackageSnapshot, load_package_snapshot
from ...contracts.metric_portability import export_metric_portability
from ...dialects import dialect_for_warehouse
from ...errors import SemanticLayerError
from ...expressions import (
    AggregateExpr,
    ArithmeticExpr,
    LiteralExpr,
    MeasureRefExpr,
    MetricRecipeRefExpr,
    RatioExpr,
    SemanticExpr,
    collect_column_refs,
    expr_kind,
    expr_to_dict,
)
from ...package_snapshot import canonicalize_semantics

# The engine's own scalar-expression lowering, shared so field SQL matches what it runs.
from ...relation_pipelines import _semantic_expr_to_sql
from ...renderer import render_expr, use_dialect
from ...schema import MeasureConfig, PackageConfig, RelationshipConfig
from ...sql_ast import SqlBinary, SqlCall, SqlExpr, SqlIdentifier, SqlLiteral

OSSIE_VERSION = "0.1.1"
SIDECAR_FORMAT_VERSION = 1
# 0.1.1's dialect enum names no other warehouse the engine runs on.
_DIALECTS = {"snowflake": "SNOWFLAKE", "databricks": "DATABRICKS"}
_AGGREGATES = {
    "count_distinct": "COUNT",
    **{a: a.upper() for a in ("sum", "count", "avg", "min", "max")},
}
_OPERATORS = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}
_METRIC_KINDS = frozenset({"aggregate", "ratio", "derived"})
_TIME_TYPES = frozenset({"date", "timestamp"})
_LABELLED = ("label", "description", "aliases")
_JOIN_FIELDS = frozenset(
    f"{side}_{part}" for side in ("source", "target") for part in ("entity", "column", "columns")
) | {"aliases"}
# Collections carried by Ossie elements: the element that names them, and the warning label.
_CORE = {
    "entities": ("datasets", "entity"),
    "dimensions": ("fields", "dimension"),
    "measures": ("fields", "measure"),
    "relationships": ("relationships", "relationship"),
    "metric_recipes": ("metrics", "metric"),
}
# Identified collections with no Ossie 0.1.1 element; any other package field is handled
# generically, so a field added to PackageConfig later lands in the sidecar too.
_SIDECAR_ONLY = (
    "temporal_roles",
    "value_domains",
    "segments",
    "semantic_policies",
    "semantic_caveats",
    "aggregate_relations",
    "relations",
)
_DEPLOYMENT = frozenset({"connection", "default_db", "seed"})
_OMITTED = "Ossie 0.1.1 can't state these; left out of the document and kept whole in the sidecar"
_UNENFORCED = (
    "Ossie consumers don't read the sidecar and won't enforce these policies; anyone given the "
    "document sees every exported dataset, field and metric"
)


class _Unsupported(Exception):
    def __init__(self, construct: str) -> None:
        super().__init__(construct)
        self.construct = construct


def _name(object_id: str, taken: set[str]) -> str:
    """An identifier-shaped Ossie name: the id without its type prefix, unique among ``taken``."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", object_id.split(".", 1)[-1])
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}_{suffix}", suffix + 1
    taken.add(name)
    return name


def _attributes(row: Any, carried: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """``row``'s non-default attributes, except those the Ossie document carries."""
    out: dict[str, Any] = {}
    for item in fields(row):
        value = getattr(row, item.name)
        if item.name in carried:
            continue
        if item.default is not MISSING and value == item.default:
            continue
        if item.default_factory is not MISSING and value == item.default_factory():
            continue
        out[item.name] = expr_to_dict(value) if item.name in {"expr", "expression"} else value
    return canonicalize_semantics(out)


def _described(row: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    """Ossie's presentation fields for ``row``, with its aliases as ``ai_context`` synonyms."""
    out = {key: getattr(row, key) for key in keys if key != "aliases" and getattr(row, key)}
    if "aliases" in keys and row.aliases:
        out["ai_context"] = {"synonyms": list(row.aliases)}
    return out


def _warning(construct: str, ids: list[str], message: str) -> dict[str, Any]:
    return {"construct": construct, "count": len(ids), "ids": sorted(ids), "message": message}


def _divide(left: SqlExpr, right: SqlExpr) -> SqlExpr:
    # The engine divides by NULLIF(denominator, 0) for every ratio and division.
    return SqlBinary(left, "/", SqlCall("NULLIF", [right, SqlLiteral(0)]))


def _relationship_gap(relationship: RelationshipConfig) -> str:
    """Why Ossie's many-to-one join can't state ``relationship``, or ``""`` when it can."""
    cardinality, safety = relationship.cardinality, relationship.safety
    if cardinality not in {"N:1", "1:1", "1:N"}:
        return f"{cardinality} relationships"
    if relationship.temporal_validity:
        return "time-valid relationships"
    # A 1:N join is exported reversed, where its default requires_rewrite no longer applies.
    if safety == "unsafe" or (safety != "safe" and cardinality != "1:N"):
        return f"{safety} relationships"
    if ("reverse" if cardinality == "1:N" else "forward") not in relationship.allowed_directions:
        return "relationships closed to many-to-one joins"
    return ""


class _Exporter:
    def __init__(self, config: PackageConfig) -> None:
        self.config = config
        self.warehouse = config.package.warehouse
        self.dialect = _DIALECTS.get(self.warehouse, "ANSI_SQL")
        self.entities = {row.id: row for row in config.entities}
        self.measures = {row.id: row for row in config.measures}
        self.metrics = {row.id: row for row in config.metric_recipes}
        self.names: dict[str, dict[str, str]] = defaultdict(dict)
        self.objects: dict[str, Any] = defaultdict(dict)
        self.lost: dict[str, list[str]] = defaultdict(list)
        self.partial: dict[str, list[str]] = defaultdict(list)
        self.columns: dict[str, list[str]] = {}
        self.metric_sql: dict[str, SqlExpr | _Unsupported] = {}
        # The exact AST behind each exported expression, so an import gets it back unchanged.
        self.expressions: dict[str, dict[str, Any]] = defaultdict(dict)

    def omit(self, collection: str, row: Any, construct: str) -> None:
        self.objects[collection][row.id] = _attributes(row)
        self.lost[construct].append(row.id)

    def keep(self, collection: str, row: Any, name: str, carried: set[str]) -> None:
        self.names[_CORE[collection][0]][name] = row.id
        extra = _attributes(row, carried | {"id"})
        if extra:
            self.objects[collection][row.id] = extra
            self.partial[collection].append(row.id)

    def expression(self, sql: SqlExpr) -> dict[str, Any]:
        bound = None if self.dialect == "ANSI_SQL" else dialect_for_warehouse(self.warehouse)
        with use_dialect(bound):
            return {"dialects": [{"dialect": self.dialect, "expression": render_expr(sql)}]}

    def model(self) -> dict[str, Any]:
        config, package = self.config, self.config.package
        model: dict[str, Any] = {"name": package.package_id}
        if package.description:
            model["description"] = package.description
        extra = _attributes(package, {"package_id", "description"} | _DEPLOYMENT)
        if extra:
            self.objects["package"] = {package.package_id: extra}
            self.partial["package"].append(package.package_id)
        model["datasets"] = self.datasets()
        model["relationships"] = self.relationships()
        model["metrics"] = self.exported_metrics()
        for collection in _SIDECAR_ONLY:
            for row in getattr(config, collection):
                self.omit(collection, row, collection.replace("_", " "))
        handled = {"version", "package", *_CORE, *_SIDECAR_ONLY}
        for name, value in _attributes(config, handled).items():
            self.objects[name] = value
            many = isinstance(value, list)
            self.lost[name.replace("_", " ")] = (
                [f"{name}[{index}]" for index in range(len(value))] if many else [name]
            )
        return model

    def datasets(self) -> list[dict[str, Any]]:
        config = self.config
        taken: set[str] = set()
        # A relation pipeline's output is a CTE the compiler builds, not a table a consumer can read.
        dataset_names = {
            entity.id: _name(entity.id, taken)
            for entity in config.entities
            if not entity.relation_id
        }
        field_names: dict[str, set[str]] = defaultdict(set)
        entity_fields: dict[str, list[dict[str, Any]]] = defaultdict(list)
        role_dimensions = {role.dimension for role in config.temporal_roles}
        for dimension in config.dimensions:
            if dimension.entity not in dataset_names:
                self.omit("dimensions", dimension, "dimensions over relation pipelines")
                continue
            dataset = dataset_names[dimension.entity]
            name = _name(dimension.id, field_names[dataset])
            is_time = dimension.data_type in _TIME_TYPES or dimension.id in role_dimensions
            entity_fields[dimension.entity].append(
                {
                    "name": name,
                    "expression": self.expression(SqlIdentifier(parts=[dimension.column])),
                    "dimension": {"is_time": is_time},
                    **_described(dimension, _LABELLED),
                }
            )
            self.keep(
                "dimensions", dimension, f"{dataset}.{name}", {"entity", "column", *_LABELLED}
            )
        for measure in config.measures:
            try:
                sql = self.measure_sql(measure)
            except _Unsupported as exc:
                self.omit("measures", measure, exc.construct)
                continue
            dataset = dataset_names[measure.entity]
            name = _name(measure.id, field_names[dataset])
            self.columns[measure.id] = [dataset, name]
            entity_fields[measure.entity].append(
                {"name": name, "expression": self.expression(sql), **_described(measure, _LABELLED)}
            )
            self.keep("measures", measure, f"{dataset}.{name}", {"entity", "expr", *_LABELLED})
            self.expressions["measures"][measure.id] = expr_to_dict(measure.expr)
        datasets: list[dict[str, Any]] = []
        for entity in config.entities:
            if entity.id not in dataset_names:
                self.omit("entities", entity, "entities over relation pipelines")
                continue
            row: dict[str, Any] = {"name": dataset_names[entity.id], "source": entity.table}
            key = [column for column in entity.key or [entity.primary_key] if column]
            if key:
                row["primary_key"] = key
            row.update(_described(entity, ("description", "aliases")))
            if entity_fields[entity.id]:
                row["fields"] = entity_fields[entity.id]
            datasets.append(row)
            carried = {"table", "primary_key", "key", "description", "aliases"}
            self.keep("entities", entity, dataset_names[entity.id], carried)
        return datasets

    def measure_sql(self, measure: MeasureConfig) -> SqlExpr:
        entity = self.entities[measure.entity]
        if entity.relation_id:
            raise _Unsupported("measures over relation pipelines")
        if measure.source_relation not in ("", entity.table):
            raise _Unsupported("measures on other relations")
        refs = collect_column_refs(measure.expr)
        if any(ref.table or ref.entity not in ("", measure.entity) for ref in refs):
            raise _Unsupported("measures reading other entities")
        try:
            return _semantic_expr_to_sql(measure.expr, default_alias="", warehouse=self.warehouse)
        except SemanticLayerError:
            raise _Unsupported(f"measures with {expr_kind(measure.expr)} expressions") from None

    def relationships(self) -> list[dict[str, Any]]:
        by_entity = {entity_id: name for name, entity_id in self.names["datasets"].items()}
        taken: set[str] = set()
        relationships = []
        for row in self.config.relationships:
            gap = _relationship_gap(row)
            if not gap and not {row.source_entity, row.target_entity} <= by_entity.keys():
                gap = "relationships to relation pipelines"
            if gap:
                self.omit("relationships", row, gap)
                continue
            ends = [
                (by_entity[row.source_entity], row.source_columns or [row.source_column]),
                (by_entity[row.target_entity], row.target_columns or [row.target_column]),
            ]
            (many, many_columns), (one, one_columns) = (
                ends[::-1] if row.cardinality == "1:N" else ends
            )
            name = _name(row.id, taken)
            relationships.append(
                {
                    "name": name,
                    "from": many,
                    "to": one,
                    "from_columns": list(many_columns),
                    "to_columns": list(one_columns),
                    **_described(row, ("aliases",)),
                }
            )
            # The sidecar keeps a 1:1 or reversed 1:N cardinality, and any safety but "safe".
            carried = set(_JOIN_FIELDS)
            if row.cardinality == "N:1":
                carried.add("cardinality")
            if row.safety == "safe":
                carried.add("safety")
            self.keep("relationships", row, name, carried)
        return relationships

    def exported_metrics(self) -> list[dict[str, Any]]:
        taken: set[str] = set()
        metrics = []
        for metric in self.config.metric_recipes:
            sql = self.metric_expression(metric.id)
            if isinstance(sql, _Unsupported):
                self.omit("metric_recipes", metric, sql.construct)
                continue
            name = _name(metric.id, taken)
            metrics.append(
                {
                    "name": name,
                    "expression": self.expression(sql),
                    **_described(metric, ("description", "aliases")),
                }
            )
            self.keep("metric_recipes", metric, name, {"expression", "description", "aliases"})
            self.expressions["metric_recipes"][metric.id] = expr_to_dict(metric.expression)
        return metrics

    def metric_expression(self, metric_id: str) -> SqlExpr | _Unsupported:
        if metric_id not in self.metric_sql:
            # Seeded first so a reference cycle resolves to "omitted" instead of recursing.
            self.metric_sql[metric_id] = _Unsupported("metrics built on omitted objects")
            metric = self.metrics.get(metric_id)
            try:
                if metric is None:
                    raise _Unsupported("metrics built on omitted objects")
                if metric.kind not in _METRIC_KINDS:
                    raise _Unsupported(f"{metric.kind} metrics")
                if metric.filter_spec:
                    raise _Unsupported("filtered metrics")
                if metric.window_spec:
                    raise _Unsupported("windowed metrics")
                self.metric_sql[metric_id] = self.metric_node(metric.expression)
            except _Unsupported as exc:
                self.metric_sql[metric_id] = exc
        return self.metric_sql[metric_id]

    def metric_node(self, expr: SemanticExpr) -> SqlExpr:
        if isinstance(expr, (AggregateExpr, MeasureRefExpr)):
            return self.aggregate(expr)
        if isinstance(expr, MetricRecipeRefExpr):
            inner = self.metric_expression(expr.metric_recipe)
            if isinstance(inner, _Unsupported):
                raise _Unsupported("metrics built on omitted objects")
            return inner
        if isinstance(expr, LiteralExpr):
            return SqlLiteral(expr.value)
        if isinstance(expr, RatioExpr):
            return _divide(self.metric_node(expr.numerator), self.metric_node(expr.denominator))
        if isinstance(expr, ArithmeticExpr) and expr.op in _OPERATORS:
            left, right = self.metric_node(expr.left), self.metric_node(expr.right)
            if expr.op == "divide":
                return _divide(left, right)
            if expr.null_behavior == "coalesce_zero" and expr.op in {"add", "subtract"}:
                left = SqlCall("COALESCE", [left, SqlLiteral(0)])
                right = SqlCall("COALESCE", [right, SqlLiteral(0)])
            return SqlBinary(left, _OPERATORS[expr.op], right)
        raise _Unsupported(f"metrics using {expr_kind(expr)}")

    def aggregate(self, expr: AggregateExpr | MeasureRefExpr) -> SqlExpr:
        if isinstance(expr, AggregateExpr) and expr.filter:
            raise _Unsupported("filtered metrics")
        if isinstance(expr, AggregateExpr) and expr.window:
            raise _Unsupported("windowed metrics")
        measure, column = self.measures.get(expr.measure), self.columns.get(expr.measure)
        if measure is None or column is None:
            raise _Unsupported("metrics built on omitted objects")
        # The engine aggregates a semi-additive measure over each key's latest snapshot row.
        if measure.measure_class == "semi_additive":
            raise _Unsupported("metrics on semi-additive measures")
        # ...and re-aggregates (or refuses) a measure rolled up to another entity.
        if measure.aggregation_entity not in ("", measure.entity):
            raise _Unsupported("metrics rolled up to another entity")
        aggregation = (expr.aggregation or measure.default_aggregation).lower()
        if aggregation not in _AGGREGATES:
            raise _Unsupported(f"metrics using {aggregation}")
        if expr.temporal_role or expr.parameters:
            raise _Unsupported("metrics with clock or parameter overrides")
        return SqlCall(
            _AGGREGATES[aggregation],
            [SqlIdentifier(parts=column)],
            distinct=aggregation == "count_distinct",
        )

    def warnings(self) -> list[dict[str, Any]]:
        rows = [_warning(construct, ids, _OMITTED) for construct, ids in self.lost.items()]
        for collection, ids in self.partial.items():
            names = ", ".join(sorted({key for i in ids for key in self.objects[collection][i]}))
            label = _CORE[collection][1] if collection in _CORE else collection
            message = (
                f"exported without {names}, which Ossie 0.1.1 has no field for; kept in the sidecar"
            )
            rows.append(_warning(f"{label} attributes", ids, message))
        if "semantic policies" in self.lost:
            rows.append(_warning("policy enforcement", self.lost["semantic policies"], _UNENFORCED))
        return sorted(rows, key=lambda row: row["construct"])


def export_ossie(
    path: str | Path | LoadedPackageSnapshot,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(document, sidecar)`` for one package; the sidecar's warnings count every gap."""
    snapshot = load_package_snapshot(path)
    exporter = _Exporter(snapshot.config)
    document = {"version": OSSIE_VERSION, "semantic_model": [exporter.model()]}
    sidecar = {
        "format": "semantic_rails.ossie_sidecar",
        "format_version": SIDECAR_FORMAT_VERSION,
        "ossie_version": OSSIE_VERSION,
        "producer": {"name": "semantic-rails", "version": __version__},
        # The metric-portability identity: (package.namespace, metric id).
        "package": export_metric_portability(snapshot)["package"],
        "names": dict(exporter.names),
        "objects": dict(exporter.objects),
        "expressions": canonicalize_semantics(dict(exporter.expressions)),
        "warnings": exporter.warnings(),
    }
    return document, sidecar


def write_ossie_export(
    path: str | Path | LoadedPackageSnapshot, output_dir: str | Path
) -> dict[str, Any]:
    """Write ``<package-id>.ossie.yaml`` and ``<package-id>.semantic_rails.json``; report both."""
    document, sidecar = export_ossie(path)
    output = Path(output_dir).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", sidecar["package"]["id"]).lstrip(".") or "package"
    document_path = output / f"{stem}.ossie.yaml"
    sidecar_path = output / f"{stem}.semantic_rails.json"
    document_path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    sidecar_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    model = document["semantic_model"][0]
    return {
        "ok": True,
        "format": "ossie",
        "ossie_version": OSSIE_VERSION,
        "document": str(document_path),
        "sidecar": str(sidecar_path),
        "exported": {key: len(model[key]) for key in ("datasets", "relationships", "metrics")},
        "warnings": [
            {key: row[key] for key in ("construct", "count", "message")}
            for row in sidecar["warnings"]
        ],
    }
