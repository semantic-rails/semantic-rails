"""Write an in-memory ``PackageConfig`` back out as an authored package directory.

The loader derives much of a package (ids, names, key dimensions, aggregation sets, joins) from
the compact authoring form that ``schema_strict`` packages must use. This writer goes the other
way: each object in the keys the loader reads, leaving out what the loader derives anyway, so
loading the directory gives the same objects back. What it can't write yet is returned by id.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

from ..architect_scaffold import dump_project_yaml
from ..config import _default_topics
from ..expressions import expr_to_dict
from ..schema import PackageConfig

# PackageConfig collections this writer doesn't write back yet.
UNWRITTEN = ("relations", "aggregate_relations", "path_preferences")
_COUNTED = {"event_count", "distinct_population"}
_ADDITIVE = ["sum", "avg", "min", "max", "median", "percentile"]
# The aggregations the loader allows each measure class before `disallowed_aggregations:`.
_ALLOWED = {
    **dict.fromkeys(_COUNTED, ["count_distinct"]),
    "semi_additive": ["last_value", "first_value", *_ADDITIVE],
}
# Authored keys for compiled fields whose names differ.
_RENAMES = {
    "aliases": "synonyms",
    "compatible_temporal_roles": "times",
    "default_aggregation": "default_agg",
    "default_query_time_axis": "default_query_axis",
    "example_entries": "examples",
    "semantic_kind": "kind",
    "source_columns": "via",
    "target_columns": "target",
    "temporal_class": "class",
    "from_": "from",
}
_CARDINALITY = {
    "1:1": "one_to_one",
    "N:1": "many_to_one",
    "1:N": "one_to_many",
    "M:N": "many_to_many",
}
_ROLLUPS = {"rollup_safe_aggregations": "forward", "rollup_safe_aggregations_reverse": "reverse"}
_MODEL_FIELDS = {"calendar_id", "freshness_source", "freshness_sla_seconds", "freshness_as_of"}
_MEMBERSHIP = ("where", "metric_filters", "time", "temporal_role_overrides", "path_policy")


def _slug(value: str) -> str:
    return "_".join(part for part in re.split(r"[^a-z0-9]+", value.lower()) if part)


def _keyed(sr_id: str, prefix: str) -> tuple[str, dict[str, str]]:
    """The authoring key the loader derives ``sr_id`` from, or a key plus an ``as:`` override."""
    tail = sr_id[len(prefix) :] if sr_id.startswith(prefix) else ""
    return (tail, {}) if tail and tail == _slug(tail) else (_slug(sr_id), {"as": sr_id})


def _authored(
    row: Any, keep: tuple[str, ...] = (), skip: Any = (), renames: dict[str, str] = _RENAMES
) -> dict[str, Any]:
    """``row``'s fields under their authored keys: those in ``keep`` always, others when set."""
    out: dict[str, Any] = {}
    for item in fields(row):
        value = getattr(row, item.name)
        factory = item.default_factory
        default = factory() if factory is not MISSING else item.default
        if item.name in skip or (item.name not in keep and value == default):
            continue
        if isinstance(value, list) and value and hasattr(value[0], "__dataclass_fields__"):
            value = [_authored(entry, renames=renames) for entry in value]
        elif hasattr(value, "__dataclass_fields__"):
            value = _authored(value, renames=renames)
        out[renames.get(item.name, item.name)] = value
    return out


def _described(row: Any, *keep: str, skip: Any = (), renames: dict[str, str] = _RENAMES) -> dict:
    return _authored(row, ("name", "label", "description", *keep), {"id", *skip}, renames)


class _Writer:
    def __init__(self, config: PackageConfig, namespace: str) -> None:
        self.config, self.ns = config, namespace
        self.unwritten: dict[str, list[str]] = defaultdict(list)
        self.domains = {row.id: row for row in config.value_domains}
        self.entities = {row.id: row for row in config.entities}
        self.keys = {row.id: _keyed(row.id, f"entity.{namespace}_") for row in config.entities}
        self.models: dict[str, dict[str, Any]] = {}
        # A relation pipeline's entity isn't written yet, and neither is anything on it.
        self.pipelines = {row.id for row in config.entities if row.relation_id}

    def unwritable(self, collection: str, sr_id: str, *entity_ids: str) -> bool:
        if self.pipelines.isdisjoint(entity_ids):
            return False
        self.unwritten[collection].append(sr_id)
        return True

    def model(self, entity_id: str, relation: str = "") -> tuple[str, dict[str, Any]]:
        entity, key = self.entities[entity_id], self.keys[entity_id][0]
        spec: dict[str, Any] = {"relation": entity.table, "entities": {key: None}}
        if relation and relation != entity.table:  # a fact model bound to the entity's calendar
            spec = {"kind": "fact", "relation": relation, "time_entity": key}
            key = f"{key}_{_slug(relation)}"
        return key, self.models.setdefault(key, spec)

    def graph(self) -> dict[str, Any]:
        entities: dict[str, Any] = {}
        for entity in self.config.entities:
            if self.unwritable("entities", entity.id, entity.id):
                continue
            key, alias = self.keys[entity.id]
            role = next(iter(entity.key_roles.values()), "primary")
            columns = list(entity.key or [entity.primary_key])
            skip = {"table", "primary_key", "key", "relation_id", "identifiers", "key_roles"}
            entities[key] = {
                **alias,
                "key": columns if role == "primary" else {"columns": columns, "role": role},
                "model": key,
                **_described(
                    entity, skip={*skip, "foreign_keys", "foreign_key_roles", *_MODEL_FIELDS}
                ),
            }
            model = self.model(entity.id)[1]
            model.update(_authored(entity, skip={f.name for f in fields(entity)} - _MODEL_FIELDS))
            if entity.foreign_keys:  # graph.relationships states every join, so infer none
                model["entities"].update({n: {"expr": c} for n, c in entity.foreign_keys.items()})
                model["entities"]["bridge"] = False
        return {"entities": entities}

    def dimensions(self) -> None:
        roles = {role.dimension: role for role in self.config.temporal_roles}
        for dimension in self.config.dimensions:
            if self.unwritable("dimensions", dimension.id, dimension.entity):
                continue
            entity = self.entities[dimension.entity]
            keys = {*entity.key, *(c for cols in entity.foreign_keys.values() for c in cols)}
            if dimension.semantic_kind == "id" and dimension.column in keys:
                continue  # the loader creates key and foreign-key dimensions itself
            prefix = f"dimension.{self.ns}_{self.keys[entity.id][0]}_"
            key, alias = _keyed(dimension.id, prefix)
            skip = {"entity", "data_type", "value_domain"}
            spec = {**alias, **_described(dimension, "semantic_kind", skip=skip)}
            domain = self.domains.get(dimension.value_domain)
            if domain is not None:
                spec["domain"] = [
                    _authored(v, ("label", "description"), (), {}) for v in domain.values
                ]
                spec["value_domain_id"] = domain.id
            model, role = self.model(entity.id)[1], roles.get(dimension.id)
            if role is None:
                model.setdefault("dimensions", {})[key] = spec
                continue
            time = _described(role, "temporal_class", "supported_grains", skip={"dimension"})
            time["id"] = role.id
            if (dimension.name, dimension.label) == (role.name, role.label) and not (
                dimension.aliases or domain
            ):  # one times entry creates the role and its dimension
                spec.pop("as", None)
                model.setdefault("times", {})[key] = {**spec, **time, "dimension_id": dimension.id}
            else:  # a times entry with the dimension's key reuses that dimension
                model.setdefault("dimensions", {})[key] = spec
                model.setdefault("times", {})[key] = time

    def measures(self) -> None:
        for measure in self.config.measures:
            if self.unwritable("measures", measure.id, measure.entity):
                continue
            model_key, model = self.model(measure.entity, measure.source_relation)
            if (
                model.get("kind") == "fact"
                or measure.row_grain != self.entities[measure.entity].key
            ):
                model["grain"] = list(measure.row_grain)
            key, alias = _keyed(measure.id, f"measure.{self.ns}.")
            skip = {"entity", "row_grain", "source_relation", "expr", "measure_class"}
            skip |= {"allowed_aggregations", "invalid_aggregations", "authoring_warnings"}
            spec = {
                **alias,
                "kind": "entity_count" if measure.measure_class in _COUNTED else "aggregate",
                "expr": expr_to_dict(measure.expr),
                **_described(measure, skip={*skip, "subject_entity", "aggregation_entity"}),
            }
            allowed = _ALLOWED.get(measure.measure_class, _ADDITIVE)
            disallowed = [agg for agg in allowed if agg not in measure.allowed_aggregations]
            if disallowed:
                spec["disallowed_aggregations"] = disallowed
            for name in ("subject_entity", "aggregation_entity"):
                if getattr(measure, name) != measure.entity:
                    spec[name] = getattr(measure, name)
            if measure.topics == _default_topics(measure.name, fallback=model_key):
                spec.pop("topics", None)
            if not self.config.package.schema_strict:
                spec["publish"] = False  # every metric is written out explicitly
            model.setdefault("measures", {})[key] = spec

    def relationship(self, row: Any) -> tuple[str, dict[str, Any]]:
        ends = {"source_entity", "target_entity", "source_column", "target_column", *_ROLLUPS}
        spec = {
            "id": row.id,
            "entities": [self.keys[row.source_entity][0], self.keys[row.target_entity][0]],
            **_described(row, "safety", skip=ends),
            "cardinality": _CARDINALITY.get(row.cardinality, row.cardinality),
        }
        rollups = {side: getattr(row, name) for name, side in _ROLLUPS.items()}
        if any(rollups.values()):
            spec["rollup_safe"] = {side: aggs for side, aggs in rollups.items() if aggs}
        return _slug(row.id.split(".", 1)[-1]), spec

    def metric(self, row: Any) -> tuple[str, dict[str, Any]]:
        skip = {"expression", "filter_spec", "window_spec", "aliases"}
        spec = {"id": row.id, "expression": expr_to_dict(row.expression)}
        spec.update(
            _described(
                row, "kind", "value_type", skip=skip, renames={"example_entries": "examples"}
            )
        )
        if row.topics == _default_topics(row.name):
            spec.pop("topics", None)
        return row.id.split(".", 1)[-1], spec

    def segment(self, row: Any) -> tuple[str, dict[str, Any]]:
        spec = {"id": row.id, **_described(row, "entity", "basis_metric", skip=_MEMBERSHIP)}
        membership = {key: getattr(row, key) for key in _MEMBERSHIP if getattr(row, key)}
        if membership:
            spec["membership"] = membership
        if row.topics == _default_topics(row.name, fallback="segments"):
            spec.pop("topics", None)
        return row.id.split(".", 1)[-1], spec

    def documents(self) -> dict[str, dict[str, Any]]:
        config = self.config
        package = {"id": config.package.package_id, "namespace": self.ns}
        package.update(
            _authored(config.package, ("name", "description", "schema_strict"), {"package_id"})
        )
        graph = self.graph()
        self.dimensions()
        self.measures()
        joins: dict[str, Any] = {}
        for row in config.relationships:
            if self.unwritable("relationships", row.id, row.source_entity, row.target_entity):
                continue
            key, spec = self.relationship(row)
            # The loader keeps one join per entity pair, keyed by the target.
            if key in joins or any(j["entities"] == spec["entities"] for j in joins.values()):
                self.unwritten["relationships"].append(row.id)
                continue
            joins[key] = spec
        if joins:
            graph["relationships"] = joins
        if config.path_policy != type(config.path_policy)():
            graph["path_policy"] = _authored(config.path_policy)
        documents: dict[str, dict[str, Any]] = {
            "package.yml": {"schema_version": config.version, "package": package},
            "graph.yml": {"graph": graph},
            **{f"models/{k}.yml": {"model": {"id": k, **m}} for k, m in self.models.items()},
        }
        if config.metric_recipes:
            documents["metrics.yml"] = {"metrics": dict(map(self.metric, config.metric_recipes))}
        if config.segments:
            documents["segments.yml"] = {"segments": dict(map(self.segment, config.segments))}
        for name, file, keep in (
            ("semantic_policies", "policies.yml", ("id", "kind")),
            ("semantic_caveats", "caveats.yml", ("id", "kind", "message")),
        ):
            rows = [
                {**row.config, **_authored(row, keep, {"config"})} for row in getattr(config, name)
            ]
            if rows:
                documents[file] = {name: rows}
        for name in UNWRITTEN:
            for row in getattr(config, name):
                self.unwritten[name].append(getattr(row, "id", name))
        return documents


def package_documents(
    config: PackageConfig, *, namespace: str
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """The authored files for ``config`` (relative path -> YAML document), plus the ids of
    objects this writer can't write yet, by collection."""
    writer = _Writer(config, namespace)
    return writer.documents(), dict(writer.unwritten)


def write_package(documents: dict[str, dict[str, Any]], directory: str | Path) -> Path:
    """Write ``documents`` under ``directory``, which must not exist yet."""
    root = Path(directory).expanduser()
    if root.exists():
        raise FileExistsError(f"{root} already exists; choose a new output directory")
    for relative, document in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_project_yaml(document), encoding="utf-8")
    return root
