"""Import an Apache Ossie document (0.1.x or 0.2) as a Semantic Rails package directory.

With the sidecar the export writes beside the document, every object comes back exactly: the
document supplies what it carries (tables, keys, columns, labels, joins), the sidecar the rest,
including the objects the export left out. Without it, datasets, column fields, joins and
metrics in the aggregate SQL the export writes are imported with defaults. Every default and
every construct skipped is counted in a warning; nothing is guessed.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import fields, is_dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

import yaml

from ...config import (
    _default_topics,
    _derive_measure_semantics,
    _suggested_aggregations,
    load_package_snapshot,
)
from ...config_parts.package_loader import _slug, _titleize
from ...errors import SemanticLayerError
from ...expressions import (
    AggregateExpr,
    ArithmeticExpr,
    LiteralExpr,
    SemanticExpr,
    parse_config_expression,
    parse_semantic_expression,
)
from ...relation_pipelines import _semantic_expr_to_sql
from ...renderer import render_expr
from ...schema import ConnectionSpec, PackageConfig, PackageMeta, SeedSpec
from ...yaml_loader import load_yaml_file
from ..package_writer import write_package
from .export import SIDECAR_FORMAT_VERSION, export_ossie

# The keys each element may carry; any other key is counted as not imported.
_READ = {
    "model": {
        "name",
        "description",
        "datasets",
        "relationships",
        "metrics",
        "version",
        "ai_context",
    },
    "dataset": {"name", "source", "primary_key", "description", "ai_context", "fields"},
    "field": {"name", "expression", "dimension", "label", "description", "ai_context", "datatype"},
    "relationship": {"name", "from", "to", "from_columns", "to_columns", "ai_context"},
    "metric": {"name", "expression", "description", "ai_context"},
}
_DEFAULTED = "given Semantic Rails defaults for what the document doesn't say; review them"
_MESSAGES = {
    "dimensions typed by default": _DEFAULTED,
    "measures given defaults": f"{_DEFAULTED} (aggregation from their metrics, time from their dataset)",
    "temporal roles with default grains": _DEFAULTED,
    "names that collide once normalized": "not imported: another element already has that id",
}
_AGGREGATES = {"SUM": "sum", "AVG": "avg", "MIN": "min", "MAX": "max", "COUNT": "count_distinct"}
_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)|(\S))")
_IDENTIFIER = re.compile(r'([A-Za-z_][\w$]*)|"((?:[^"]|"")+)"|`([^`]+)`')


def _build(cls: Any, data: dict[str, Any]) -> Any:
    """``cls`` from its canonical attributes, rebuilding nested dataclasses and expressions."""
    hints = get_type_hints(cls)
    kept = [f.name for f in fields(cls) if f.name in data]
    return cls(**{name: _value(hints[name], data[name], name) for name in kept})


def _value(hint: Any, value: Any, name: str) -> Any:
    if name in {"expr", "expression"} and isinstance(value, dict):
        return parse_semantic_expression(value, context=f"Ossie sidecar {name}")
    if is_dataclass(hint):
        return _build(hint, value)
    args = get_args(hint)
    if get_origin(hint) is list and args and is_dataclass(args[0]):
        return [_build(args[0], row) for row in value]
    return value


def _dialects(node: dict[str, Any]) -> list[dict[str, Any]]:
    return list(dict(node.get("expression") or {}).get("dialects") or [])


def _sql(node: dict[str, Any]) -> str:
    dialects = _dialects(node) or [{}]
    ansi = [d for d in dialects if d.get("dialect") in {"ANSI_SQL", "OSSIE_SQL_2026"}]
    return str((ansi or dialects)[0].get("expression", "")).strip()


def _synonyms(node: dict[str, Any]) -> list[str]:
    context = node.get("ai_context")
    synonyms = context.get("synonyms") if isinstance(context, dict) else None
    return [str(item) for item in synonyms] if isinstance(synonyms, list) else []


def _fact(sql: str) -> SemanticExpr | None:
    """A fact's row-level SQL, when the engine's expression parser reads it and renders it back."""
    try:
        expr = parse_config_expression(sql)
        rendered = render_expr(_semantic_expr_to_sql(expr, default_alias=""))
    except SemanticLayerError:
        return None
    return expr if rendered.split() == sql.split() else None


class _Coalesced:
    """``COALESCE(x, 0)``, which the export writes only as both operands of + or -."""

    def __init__(self, expr: SemanticExpr) -> None:
        self.expr = expr


def _plain(node: Any) -> SemanticExpr:
    if isinstance(node, _Coalesced):
        raise ValueError("COALESCE outside + or -")
    return node


class _MetricParser:
    """The aggregate SQL the export writes: SUM, AVG, MIN, MAX and COUNT(DISTINCT) over
    ``dataset.field``, numbers, + - *, ``/ NULLIF(x, 0)``, ``COALESCE(x, 0)`` on both sides of
    + or -, and parentheses. ``parse`` returns ``None`` for anything else."""

    def __init__(self, sql: str, measures: dict[str, str]) -> None:
        self.tokens = [next(t for t in match.groups() if t) for match in _TOKEN.finditer(sql)]
        self.measures, self.position = measures, 0
        self.used: list[tuple[str, str]] = []

    def parse(self) -> SemanticExpr | None:
        try:
            expr = _plain(self.expression())
        except (IndexError, KeyError, ValueError):
            return None
        return expr if self.position == len(self.tokens) else None

    def take(self, expected: str = "") -> str:
        token = self.tokens[self.position]
        if expected and token.upper() != expected:
            raise ValueError(token)
        self.position += 1
        return token

    def next_is(self, *tokens: str) -> bool:
        return self.position < len(self.tokens) and self.tokens[self.position] in tokens

    def zero_call(self, function: str) -> Any:
        """``function(x, 0)``, returning x."""
        self.take(function)
        self.take("(")
        inner = self.expression()
        for token in (",", "0", ")"):
            self.take(token)
        return inner

    def expression(self) -> Any:
        left = self.term()
        while self.next_is("+", "-"):
            op, right = "add" if self.take() == "+" else "subtract", self.term()
            if isinstance(left, _Coalesced) and isinstance(right, _Coalesced):
                left = ArithmeticExpr(op, left.expr, right.expr, "coalesce_zero")
            else:
                left = ArithmeticExpr(op, _plain(left), _plain(right))
        return left

    def term(self) -> Any:
        left = self.factor()
        while self.next_is("*", "/"):
            if self.take() == "*":
                left = ArithmeticExpr("multiply", _plain(left), _plain(self.factor()))
            else:  # the export divides as the engine does, by NULLIF(denominator, 0)
                right = _plain(self.zero_call("NULLIF"))
                left = ArithmeticExpr("divide", _plain(left), right, "null_if_zero")
        return left

    def factor(self) -> Any:
        if self.next_is("("):
            self.take()
            inner = self.expression()
            self.take(")")
            return inner
        token = self.tokens[self.position]
        if re.fullmatch(r"\d+(\.\d+)?", token):
            self.take()
            return LiteralExpr(float(token) if "." in token else int(token))
        if token.upper() == "COALESCE":
            return _Coalesced(_plain(self.zero_call("COALESCE")))
        aggregation = _AGGREGATES[self.take().upper()]
        self.take("(")
        if aggregation == "count_distinct":
            self.take("DISTINCT")
        measure = self.measures[self.take()]
        self.take(")")
        self.used.append((measure, aggregation))
        return AggregateExpr(measure=measure, aggregation=aggregation)


class _Importer:
    def __init__(self, document: dict[str, Any], sidecar: dict[str, Any] | None) -> None:
        self.sidecar = sidecar or {}
        self.warnings: dict[str, list[str]] = defaultdict(list)
        models = list(document.get("semantic_model") or [document])  # 0.1.x wraps; 0.2 doesn't
        for extra in models[1:]:
            self.skip("semantic models after the first", str(extra.get("name", "")))
        self.model = dict(models[0])
        self.names: dict[str, dict[str, str]] = dict(self.sidecar.get("names") or {})
        self.objects: dict[str, Any] = dict(self.sidecar.get("objects") or {})
        self.exact: dict[str, dict[str, Any]] = dict(self.sidecar.get("expressions") or {})
        self.rows: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.datasets: dict[str, str] = {}
        self.fields: dict[str, str] = {}
        self.role_entities: dict[str, str] = {}
        self.ns = ""

    def skip(self, construct: str, name: str) -> None:
        self.warnings[construct].append(name)

    def residual(self, collection: str, sr_id: str) -> dict[str, Any] | None:
        return dict(self.objects.get(collection) or {}).get(sr_id)

    def row(self, collection: str, sr_id: str, attributes: dict, **defaults: Any) -> bool:
        """The document's ``attributes``, overlaid with the sidecar's, or else ``defaults``;
        false (and counted) when another element already took ``sr_id``."""
        if sr_id in self.rows[collection]:
            self.skip("names that collide once normalized", sr_id)
            return False
        extra = self.residual(collection, sr_id)
        self.rows[collection][sr_id] = {"id": sr_id, **attributes, **(extra or defaults)}
        return True

    def node(self, kind: str, node: dict[str, Any]) -> str:
        """``node``'s name, counting the keys and ``ai_context`` text this import doesn't read."""
        name = str(node.get("name", ""))
        for key in sorted(set(node) - _READ[kind]):
            self.skip(f"{kind} {key}", name)
        context = node.get("ai_context")
        extra = isinstance(context, dict) and not isinstance(context.get("synonyms", []), list)
        if isinstance(context, str) or extra or set(dict(context or {})) - {"synonyms"}:
            self.skip(f"{kind} ai_context text", name)
        return name

    def _fields(self) -> list[dict[str, Any]]:
        return [f for d in self.model.get("datasets") or [] for f in d.get("fields") or []]

    def mismatch(self) -> list[str]:
        """How the document's element names differ from the sidecar's (edits after the export)."""
        datasets = self.model.get("datasets") or []
        present = {
            "datasets": {d.get("name") for d in datasets},
            "fields": {
                f"{d.get('name')}.{f.get('name')}" for d in datasets for f in d.get("fields") or []
            },
            "relationships": {r.get("name") for r in self.model.get("relationships") or []},
            "metrics": {m.get("name") for m in self.model.get("metrics") or []},
        }
        out = []
        for group, names in present.items():
            expected = set(dict(self.names.get(group) or {}))
            out += [f"{group} {n} (not in the document)" for n in sorted(expected - names)]
            out += [f"{group} {n} (not in the sidecar)" for n in sorted(map(str, names - expected))]
        return out

    def sr_id(self, group: str, name: str, default: str) -> str:
        return dict(self.names.get(group) or {}).get(name) or default

    def read_datasets(self) -> None:
        self.node("model", self.model)
        for dataset in self.model.get("datasets") or []:
            name = self.node("dataset", dataset)
            source, key = str(dataset.get("source", "")), list(dataset.get("primary_key") or [])
            if not source or re.search(r"\s", source) or not key:
                self.skip("datasets without a primary key or defined by a query", name)
                continue
            entity_id = self.sr_id("datasets", name, f"entity.{self.ns}_{_slug(name)}")
            document = {"table": source, "primary_key": key[0], "key": key}
            document.update(
                description=str(dataset.get("description") or ""), aliases=_synonyms(dataset)
            )
            defaults = {"name": name, "label": _titleize(name), "identifiers": key}
            roles = dict.fromkeys(key, "primary")
            if not self.row("entities", entity_id, document, **defaults, key_roles=roles):
                continue
            self.datasets[name] = entity_id
            for field in dataset.get("fields") or []:
                self.read_field(name, entity_id, key, field)

    def read_field(self, dataset: str, entity_id: str, key: list[str], field: dict) -> None:
        name, sql = self.node("field", field), _sql(field)
        ref, label = f"{dataset}.{name}", str(field.get("label") or "")
        document = {"entity": entity_id, "label": label, "aliases": _synonyms(field)}
        document["description"] = str(field.get("description") or "")
        if "dimension" not in field:
            measure_id = self.sr_id(
                "fields", ref, f"measure.{self.ns}.{_slug(dataset)}_{_slug(name)}"
            )
            exact = dict(self.exact.get("measures") or {}).get(measure_id)
            expr = _value(None, exact, "expr") if exact else _fact(sql)
            if expr is None:
                self.skip("facts outside the supported expression grammar", ref)
                return
            if self.row("measures", measure_id, {**document, "expr": expr}, row_grain=key):
                self.fields[ref] = measure_id
            return
        match = _IDENTIFIER.fullmatch(sql)
        if match is None:
            self.skip("dimensions with computed expressions", ref)
            return
        document["column"] = next(group for group in match.groups() if group).replace('""', '"')
        dim_id = self.sr_id("fields", ref, f"dimension.{self.ns}_{_slug(dataset)}_{_slug(name)}")
        if self.residual("dimensions", dim_id) is not None:
            self.row("dimensions", dim_id, document)
            return
        datatype = str(field.get("datatype", "")).lower()  # 0.2 only
        marker = field["dimension"] if isinstance(field["dimension"], dict) else {}
        is_time = datatype in {"date", "timestamp"} or bool(marker.get("is_time"))
        kind = (datatype if datatype == "date" else "timestamp") if is_time else "categorical"
        document["label"] = label = label or _titleize(name)
        document["description"] = document["description"] or label
        data_type = "string" if kind == "categorical" else kind
        typed = {"name": name, "data_type": data_type, "semantic_kind": kind}
        if not self.row("dimensions", dim_id, document, **typed):
            return
        self.skip("dimensions typed by default", dim_id)
        if is_time:
            role_id = f"temporal_role.{self.ns}_{_slug(dataset)}_{_slug(name)}"
            self.skip("temporal roles with default grains", role_id)
            role: dict[str, Any] = {
                "id": role_id,
                "dimension": dim_id,
                "name": name,
                "label": label,
            }
            role.update(
                temporal_class="event_time",
                supported_grains=["day", "week", "month", "quarter", "year"],
            )
            self.rows["temporal_roles"][role_id] = role
            self.role_entities[role_id] = entity_id

    def read_relationships(self) -> None:
        for node in self.model.get("relationships") or []:
            name = self.node("relationship", node)
            ends = [self.datasets.get(str(node.get(side, ""))) or "" for side in ("from", "to")]
            columns = [list(node.get(f"{side}_columns") or []) for side in ("from", "to")]
            if not all(ends) or not all(columns):
                self.skip("relationships to datasets not imported", name)
                continue
            rel_id = self.sr_id("relationships", name, f"relationship.{_slug(name)}")
            if (self.residual("relationships", rel_id) or {}).get("cardinality") == "1:N":
                ends, columns = ends[::-1], columns[::-1]  # the export writes 1:N joins reversed
            elif not self.sidecar and set(columns[1]) != set(self.rows["entities"][ends[1]]["key"]):
                self.skip("relationships not to their target's primary key", name)  # maybe not N:1
                continue
            document: dict[str, Any] = {"source_entity": ends[0], "target_entity": ends[1]}
            document["aliases"] = _synonyms(node)
            document.update(source_column=columns[0][0], source_columns=columns[0])
            document.update(target_column=columns[1][0], target_columns=columns[1])
            document.update(cardinality="N:1", safety="safe")
            # The key roles the loader derives: a join on the source's own key is primary.
            key = self.rows["entities"][ends[0]]["key"]
            source_role = "primary" if set(columns[0]) <= set(key) else "foreign"
            defaults = {"name": name, "label": _titleize(name), "source_key_role": source_role}
            self.row("relationships", rel_id, document, **defaults, target_key_role="primary")

    def read_metrics(self) -> None:
        parsed: list[tuple[str, dict[str, Any], str, SemanticExpr, list[tuple[str, str]]]] = []
        for node in self.model.get("metrics") or []:
            name = self.node("metric", node)
            metric_id = self.sr_id("metrics", name, f"metric.{self.ns}.{_slug(name)}")
            exact = dict(self.exact.get("metric_recipes") or {}).get(metric_id)
            parser = _MetricParser(_sql(node), self.fields)
            expr = _value(None, exact, "expression") if exact else parser.parse()
            if expr is None:
                self.skip("metrics outside the aggregate grammar", name)
            elif metric_id in {row[0] for row in parsed}:
                self.skip("names that collide once normalized", metric_id)
            else:
                parsed.append((metric_id, node, name, expr, parser.used))
        counted = {
            measure for *_, used in parsed for measure, agg in used if agg == "count_distinct"
        }
        summed: dict[str, set[str]] = defaultdict(set)
        for metric_id, node, name, expr, used in parsed:
            # A measure is either counted distinct or aggregated otherwise, never both.
            if any(agg != "count_distinct" and measure in counted for measure, agg in used):
                self.skip("metrics summing a field other metrics count distinct", name)
                continue
            ratio = isinstance(expr, ArithmeticExpr) and expr.op == "divide" and len(used) == 2
            kind = (
                "aggregate" if isinstance(expr, AggregateExpr) else "ratio" if ratio else "derived"
            )
            description = str(node.get("description") or "")
            document = {"expression": expr, "description": description, "aliases": _synonyms(node)}
            defaults: dict[str, Any] = {"kind": kind, "name": name, "label": _titleize(name)}
            defaults.update(
                description=description or _titleize(name), topics=_default_topics(name)
            )
            if self.row("metric_recipes", metric_id, document, **defaults):
                for measure, agg in used:
                    summed[measure].add(agg)
        # What the loader derives for each defaulted measure, so it loads back as built.
        for measure_id, row in self.rows["measures"].items():
            if self.residual("measures", measure_id) is not None:
                continue
            self.skip("measures given defaults", measure_id)
            spec = {"kind": "entity_count"} if measure_id in counted else {}
            default, allowed, invalid, measure_class = _derive_measure_semantics(spec)
            aggs = summed[measure_id]
            default = default if spec else "sum" if "sum" in aggs or not aggs else min(aggs)
            name = measure_id.split(".", 1)[-1]
            roles = [r for r, entity in self.role_entities.items() if entity == row["entity"]]
            row.update(subject_entity=row["entity"], aggregation_entity=row["entity"], name=name)
            row.update(default_aggregation=default, allowed_aggregations=allowed)
            row.update(invalid_aggregations=invalid, measure_class=measure_class)
            row["suggested_aggregations"] = _suggested_aggregations(default, allowed, measure_class)
            row.update(topics=_default_topics(name), compatible_temporal_roles=roles)
            row["default_temporal_role"] = (roles or [""])[0]
            row["label"] = row["label"] or _titleize(name.rsplit(".", 1)[-1])
            row["description"] = row["description"] or row["label"]

    def package(self, package_id: str, namespace: str) -> PackageConfig:
        identity = dict(self.sidecar.get("package") or {})
        package_id = package_id or str(identity.get("id") or self.model.get("name", ""))
        self.ns = namespace or str(identity.get("namespace") or package_id)
        self.read_datasets()
        self.read_relationships()
        self.read_metrics()
        if self.sidecar and self.warnings:  # the export writes nothing an import skips
            raise ValueError(
                _stale([f"{c}: {', '.join(i)}" for c, i in sorted(self.warnings.items())])
            )
        objects = dict(self.objects)
        residual = dict(objects.pop("package", None) or {}).get(identity.get("id")) or {}
        # Deployment isn't part of the export; the import sets it, never the sidecar.
        residual = {
            k: v for k, v in residual.items() if k not in {"connection", "seed", "default_db"}
        }
        document = {
            "package_id": package_id,
            "name": package_id,
            "description": str(self.model.get("description") or ""),
        }
        meta = _build(PackageMeta, {**document, **residual})
        if not residual:  # the warehouse the document's SQL is written for
            nodes = [*(self.model.get("metrics") or []), *self._fields()]
            dialects = {str(d.get("dialect")) for n in nodes for d in _dialects(n)} & {
                "SNOWFLAKE",
                "DATABRICKS",
            }
            if len(dialects) > 1:
                raise ValueError(f"expressions for more than one warehouse: {sorted(dialects)}")
            meta = replace(meta, warehouse=(dialects or {"duckdb"}).pop().lower())
        hints, built = get_type_hints(PackageConfig), dict[str, Any]()
        for item in fields(PackageConfig):
            if item.name in {"version", "package"}:
                continue
            value, hint = objects.pop(item.name, None), hints[item.name]
            args = get_args(hint)
            if get_origin(hint) is list and args and is_dataclass(args[0]):
                rows: dict[Any, dict[str, Any]] = dict(self.rows.get(item.name) or {})
                # Objects the export left out of the document come whole from the sidecar.
                left_out = value.items() if isinstance(value, dict) else enumerate(value or [])
                rows.update({i: row for i, row in left_out if i not in rows})
                built[item.name] = [_build(args[0], row) for row in rows.values()]
            elif value is not None:
                built[item.name] = _value(hint, value, item.name)
        for name in objects:
            self.skip("sidecar sections this version doesn't read", name)
        return PackageConfig(version=int(identity.get("schema_version", 1)), package=meta, **built)


def _stale(edits: list[str]) -> str:
    listed = "; ".join(edits)
    return f"it doesn't match its sidecar ({listed}); import the document alone or export again"


def _changed(old: Any, new: Any, path: str, depth: int) -> list[str]:
    if depth and isinstance(old, dict) and isinstance(new, dict):
        keys = sorted(old.keys() | new.keys())
        return [p for k in keys for p in _changed(old.get(k), new.get(k), f"{path}.{k}", depth - 1)]
    return [] if old == new else [path]


def _by_name(model: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {key: model.get(key) for key in ("name", "description")}
    for key in ("datasets", "relationships", "metrics"):
        rows = model.get(key) or []
        out[key] = {
            r.get("name"): {
                **r,
                "fields": sorted(r.get("fields") or [], key=lambda f: f.get("name")),
            }
            for r in rows
        }
    return out


def _round_trip(
    directory: Path, model: dict[str, Any], sidecar: dict[str, Any], source: Path
) -> None:
    """Refuse unless an export of the written package gives back the document and sidecar."""
    document, again = export_ossie(load_package_snapshot(str(directory)))
    kept = ("package", "names", "objects", "expressions")
    old, new = (
        {k: sidecar.get(k) for k in kept},
        json.loads(json.dumps({k: again.get(k) for k in kept})),
    )
    differences = _changed(
        _by_name(model), _by_name(document["semantic_model"][0]), "document", 2
    ) + _changed(old, new, "sidecar", 3)
    if differences:
        message = f"{source}: can't import it ({_stale(differences)})"
        raise SemanticLayerError("INVALID_CONFIG", message, details={"differences": differences})


def import_ossie(
    source: str | Path,
    output_dir: str | Path,
    *,
    package_id: str = "",
    namespace: str = "",
    default_db: str = "",
) -> dict[str, Any]:
    """Write ``<output_dir>/<package-id>/`` from an Ossie document, with the sidecar the export
    writes beside it (``<name>.semantic_rails.json``) when there is one. Reports the counts, the
    warnings and, with a sidecar, whether exporting the result gives the same files back."""
    source = Path(source).expanduser()
    sidecar_path = source.with_name(
        re.sub(r"(\.ossie)?\.ya?ml$", "", source.name) + ".semantic_rails.json"
    )
    try:
        document = load_yaml_file(source)
        version = str(document.get("version")) if isinstance(document, dict) else ""
        if not re.match(r"0\.[12](\.|$)", version):
            raise ValueError("not an Ossie 0.1.x or 0.2 document")
        sidecar = json.loads(sidecar_path.read_text("utf-8")) if sidecar_path.is_file() else None
        if sidecar is not None and (
            sidecar.get("format"),
            sidecar.get("format_version"),
        ) != ("semantic_rails.ossie_sidecar", SIDECAR_FORMAT_VERSION):
            raise ValueError(f"{sidecar_path} is not a version-1 Semantic Rails sidecar")
        importer = _Importer(document, sidecar)
        identity = dict(importer.sidecar.get("package") or {})
        for key, given in (("id", package_id), ("namespace", namespace)):
            if sidecar is not None and given not in ("", str(identity.get(key) or "")):
                raise ValueError(f"with its sidecar, the package {key} is {identity.get(key)!r}")
        # A sidecar describes the document as exported; one edited since would mix the two.
        mismatch = importer.mismatch() if sidecar is not None else []
        if mismatch:
            raise ValueError(_stale(mismatch))
        config = importer.package(package_id, namespace)
    except (AttributeError, KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise SemanticLayerError("INVALID_CONFIG", f"{source}: can't import it ({exc})") from exc
    meta, pid = config.package, config.package.package_id
    if not re.fullmatch(r"\w[\w.-]*", pid):  # it names the output directory
        raise SemanticLayerError("INVALID_CONFIG", f"Package id {pid!r} can't name a directory")
    if meta.warehouse == "duckdb":  # another tool built the data the document describes
        meta = replace(
            meta, default_db=default_db or f"data/{pid}.duckdb", seed=SeedSpec("external")
        )
    elif meta.warehouse == "snowflake":
        meta = replace(meta, connection=ConnectionSpec(kind="snowflake_cli", name=pid))
    else:
        message = f"Importing a {meta.warehouse} package isn't supported yet"
        raise SemanticLayerError("INVALID_CONFIG", message)
    config = replace(config, package=meta)
    # With a sidecar, exporting what was written must give back the files that were read.
    check = partial(_round_trip, model=importer.model, sidecar=importer.sidecar, source=source)
    try:
        directory = write_package(
            config,
            Path(output_dir).expanduser() / pid,
            namespace=importer.ns,
            check=None if sidecar is None else check,
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:  # a sidecar at odds with itself
        raise SemanticLayerError("INVALID_CONFIG", f"{source}: can't import it ({exc!r})") from exc
    report: dict[str, Any] = {"ok": True, "format": "ossie", "ossie_version": version}
    report["package_dir"] = str(directory)
    report["sidecar"] = str(sidecar_path) if sidecar is not None else None
    if sidecar is not None:
        report["round_trip"] = "exact"
    collections = ("entities", "dimensions", "measures", "relationships", "metric_recipes")
    report["imported"] = {name: len(getattr(config, name)) for name in collections}
    report["warnings"] = [
        {
            "construct": c,
            "count": len(ids),
            "ids": sorted(ids),
            "message": _MESSAGES.get(c, "not imported"),
        }
        for c, ids in sorted(importer.warnings.items())
        if ids
    ]
    return report
