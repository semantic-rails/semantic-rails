"""Rewrite forms the strict authoring rules refuse: keys the loader never read, legacy model
shapes, ``id:`` overrides and measures that published their own metric."""

from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..config_parts.package_loader import _RELATIONSHIP_PASSTHROUGH, _column_list
from ..naming import slug, title
from ..schema import OBSERVATION_SCOPES
from .model import Edit, Finding, Option, PackageFiles, Rule, YamlPath

Graph = dict[str, tuple[str, YamlPath, dict[str, Any]]]


def _ignored(
    files: PackageFiles, file: str, key: YamlPath, value: Any, read: Any, use: tuple[Edit, ...]
) -> Finding:
    """Delete ``key`` when the loader already reads ``value``; otherwise ask."""
    delete = Edit(file, "delete", key)
    name = ".".join(map(str, key[-2:]))
    if value == read:
        message = f"The loader never read {name}, and reads the same value elsewhere; delete it."
        return Finding("ignored-key", file, files.line(file, key), key, message, (delete,))
    return Finding(
        "ignored-key",
        file,
        files.line(file, key),
        key,
        f"The loader never read {name}, which differs from the value it reads ({read!r}).",
        options=(
            Option("delete", f"Delete it; answers stay {read!r}.", False, (delete,)),
            Option("use", f"Read {value!r} instead; answers may change.", True, use),
        ),
    )


def _observation_scope(files: PackageFiles) -> Iterator[Finding]:
    defaults = [(file, path, row) for file, path, row in files.defaults() if isinstance(row, dict)]
    read = next(
        (row["observation_scope"] for _, _, row in defaults if "observation_scope" in row),
        OBSERVATION_SCOPES[0],
    )
    for file, path, package in files.package():
        if not isinstance(package, dict) or "observation_scope" not in package:
            continue
        key, value = (*path, "observation_scope"), package["observation_scope"]
        if not defaults:
            move = Edit(file, "insert", (), key="defaults", value={"observation_scope": value})
        elif "observation_scope" in defaults[0][2]:
            target = (*defaults[0][1], "observation_scope")
            move = Edit(defaults[0][0], "replace", target, value=value)
        else:
            move = Edit(
                defaults[0][0], "insert", defaults[0][1], key="observation_scope", value=value
            )
        yield _ignored(files, file, key, value, read, (Edit(file, "delete", key), move))


def _dimension_expr(files: PackageFiles) -> Iterator[Finding]:
    for file, path, dimension in files.dimensions():
        if "expr" not in dimension:
            continue
        key, value = (*path, "expr"), dimension["expr"]
        use = (
            (Edit(file, "delete", key), Edit(file, "replace", (*path, "column"), value=value))
            if "column" in dimension
            else (Edit(file, "rename", key, key="column"),)
        )
        yield _ignored(files, file, key, value, dimension.get("column", path[-1]), use)


def _keys(files: PackageFiles) -> Iterator[Finding]:
    yield from _observation_scope(files)
    yield from _dimension_expr(files)


def _namespace(files: PackageFiles) -> str:
    for _, _, package in files.package():
        if isinstance(package, dict):
            return str(package.get("namespace", package.get("id", "")) or "").strip()
    return ""


def _models(files: PackageFiles) -> Iterator[tuple[str, str, YamlPath, dict[str, Any]]]:
    """Each model's id as the loader keys it (a ``models:`` key outside ``models/``), with its
    file, path and row."""
    for file, path, row in files.models():
        key = path[-1] if path and path[-1] != "model" else Path(file).stem
        model_id = row.get("id") or key if Path(file).parts[0] == "models" else key
        yield str(model_id), file, path, row


def _graph(files: PackageFiles) -> Graph:
    return {
        str(path[-1]): (file, path, row)
        for file, path, row in files.graph_entities()
        if isinstance(row, dict)
    }


def _fact(model: dict[str, Any]) -> bool:
    return str(model.get("kind") or "model").strip().lower() == "fact"


def _home(entity: dict[str, Any]) -> str:
    """The model a graph entity binds, as the loader strips it."""
    return str(entity.get("model") or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _columns(model: dict[str, Any], graph: Graph, name: str) -> list[str]:
    """Entity ``name``'s key columns on the model: its ``expr:`` override, else the graph key."""
    block = _mapping(model.get("entities"))
    override = block.get(name)
    if isinstance(override, dict) and override.get("expr") is not None:
        return _column_list(override["expr"])
    return _column_list(graph[name][2].get("key")) if name in graph else []


def _primary(model_id: str, model: dict[str, Any], graph: Graph) -> tuple[str, str, list[str]]:
    """How ``normalize_package`` identifies the model's primary entity (``bound``, ``entity``,
    ``grain`` or ``name``), the entity, and its key columns on the model; empty if nothing does."""
    block = _mapping(model.get("entities"))
    grain = _column_list(model.get("grain"))
    matches = [n for n in block if n != "bridge" and _columns(model, graph, n) == grain]
    unbound = not _home(graph.get(model_id, ("", (), {}))[2])
    how, primary = next(
        (
            (how, name)
            for how, name in (
                ("bound", next((n for n, e in graph.items() if _home(e[2]) == model_id), "")),
                ("entity", str(model.get("entity") or "").strip()),
                ("grain", matches[0] if block and grain and len(matches) == 1 else ""),
                ("name", model_id if (model_id in block or not block) and unbound else ""),
            )
            if name
        ),
        ("", ""),
    )
    keys = _mapping(model.get("keys"))
    columns = _columns(model, graph, primary) if primary else []
    return how, primary, columns or _column_list(keys.get("primary"))


def _model_grain(files: PackageFiles) -> Iterator[Finding]:
    graph = _graph(files)
    for model_id, file, path, model in _models(files):
        # A model without entities: reads its row grain from grain:; model-primary-key moves it.
        if "grain" not in model or not (_fact(model) or isinstance(model.get("entities"), dict)):
            continue
        key, grain = (*path, "grain"), _column_list(model["grain"])
        edits = [Edit(file, "delete", key)]
        primary = ""
        if _fact(model):
            keys = _mapping(model.get("keys"))
            columns = _column_list(keys.get("primary") or model.get("time_column"))
        else:
            how, primary, columns = _primary(model_id, model, graph)
            entity = graph.get(primary)
            if how == "grain" and entity and not _home(entity[2]):
                edits.append(Edit(entity[0], "insert", entity[1], key="model", value=model_id))
            elif how == "grain":
                edits.append(Edit(file, "insert", path, key="entity", value=primary))
        same = bool(grain) and grain == columns
        yield Finding(
            "model-grain",
            file,
            files.line(file, key),
            key,
            "Delete grain: the model's rows are keyed by its entity."
            if same
            else f"Model '{model_id}' rows are finer than its entity's key {columns}: model "
            f"them as their own entity keyed by {grain}, related to '{primary or 'its entity'}'.",
            tuple(edits) if same else (),
        )


def _model_primary_key(files: PackageFiles) -> Iterator[Finding]:
    graph = _graph(files)
    for model_id, file, path, model in _models(files):
        block = model.get("entities")
        keys = model.get("keys")
        legacy = not isinstance(block, dict) and not _fact(model)
        if not (isinstance(keys, dict) and keys) and not (
            legacy and {"entity", "keys", "grain"} & set(model)
        ):
            continue
        keys = _mapping(keys)
        foreign = keys.get("foreign") or {}
        _, primary, _ = _primary(model_id, model, graph)
        problem = (
            not primary or not isinstance(foreign, dict) or bool(set(keys) - {"primary", "foreign"})
        )
        rows = _column_list(model.get("grain")) or _column_list(keys.get("primary"))
        listed = {primary: rows, **(foreign if isinstance(foreign, dict) else {})}
        # A key with a role ({columns, role}) has no entities: spelling.
        problem = problem or any(
            isinstance(columns, dict) for columns in (keys.get("primary"), *listed.values())
        )
        if isinstance(block, dict):
            # The block derives both keys: each must say what it already does.
            listed[primary] = keys.get("primary")
            problem = problem or any(
                columns is not None
                and (name not in block or _column_list(columns) != _columns(model, graph, name))
                for name, columns in listed.items()
            )
            edits: tuple[Edit, ...] = (Edit(file, "delete", (*path, "keys")),)
        else:
            # The model's rows are keyed by grain, else keys.primary: both must agree, and
            # some row key must exist, or the entities block would give it one.
            primary_keys = _column_list(keys.get("primary"))
            problem = problem or not rows or bool(primary_keys and primary_keys != rows)
            entities: dict[str, Any] = {}
            for name, columns in listed.items():
                canonical = _columns(model, graph, name)
                columns = _column_list(columns) or canonical
                problem = problem or not columns
                entities[name] = (
                    {}
                    if columns == canonical
                    else {"expr": columns[0] if len(columns) == 1 else columns}
                )
            if foreign:
                entities["bridge"] = False  # the legacy keys inferred no relationship
            edits = (
                *(Edit(file, "delete", (*path, k)) for k in ("keys", "grain") if k in model),
                Edit(file, "insert", path, key="entities", value=entities),
            )
        yield Finding(
            "model-primary-key",
            file,
            files.line(file, path),
            path,
            f"Model '{model_id}': its keys disagree with its entities or name no row key; key "
            "finer rows as their own entity and list every entity under entities: by hand."
            if problem
            else f"Model '{model_id}': state its keys in its entities: block.",
            () if problem else edits,
        )


_JOIN_KEYS = {"to", "id", "cardinality", "traversal", "rollup_safe_aggregations_reverse"}
# A join reads its cardinality as written; a graph relationship reads these names for it.
_CARDINALITY = {
    "1:1": "one_to_one",
    "N:1": "many_to_one",
    "1:N": "one_to_many",
    "M:N": "many_to_many",
}


def _model_joins(files: PackageFiles) -> Iterator[Finding]:
    graph = _graph(files)
    block = next(((f, p, g) for f, p, g in files._sections("graph") if isinstance(g, dict)), None)
    names = set(_mapping(block[2].get("relationships")) if block else ())
    rows = []
    for model_id, file, path, model in _models(files):
        if "joins" not in model:
            continue
        _, primary, _ = _primary(model_id, model, graph)
        home = _home(graph.get(primary, ("", (), {}))[2])
        # A graph relationship attaches to its first entity's home model.
        problem = not block or _fact(model) or primary not in graph or home not in {"", model_id}
        problem = bool(problem)
        specs: dict[str, dict[str, Any]] = {}
        for edge, raw in (model["joins"] if isinstance(model["joins"], dict) else {}).items():
            join, name = dict(raw or {}), f"{slug(model_id)}_{slug(str(edge))}"
            target = str(join.get("to", edge))
            # Without via:, the join reads the foreign key listed under its own key.
            problem = problem or name in names or (target != edge and not join.get("via"))
            problem = problem or bool(set(join) - _JOIN_KEYS - set(_RELATIONSHIP_PASSTHROUGH))
            problem = problem or ("cardinality" in join and join["cardinality"] not in _CARDINALITY)
            names.add(name)
            specs[name] = {
                "entities": [primary, target],
                **{
                    k: v
                    for k, v in join.items()
                    if k not in {"to", "traversal"} and k in _JOIN_KEYS
                },
                **{k: v for k, v in join.items() if k in _RELATIONSHIP_PASSTHROUGH},
                **({"allowed_directions": join["traversal"]} if "traversal" in join else {}),
            }
            if "cardinality" in join:
                specs[name]["cardinality"] = _CARDINALITY.get(join["cardinality"])
            if "rollup_safe_aggregations_reverse" in specs[name]:
                reverse = specs[name].pop("rollup_safe_aggregations_reverse")
                specs[name]["rollup_safe"] = {"reverse": reverse}
        rows.append((model_id, file, (*path, "joins"), specs, problem))
    added = {
        name: spec for *_, specs, problem in rows if not problem for name, spec in specs.items()
    }
    for model_id, file, key, specs, problem in rows:
        edits = [Edit(file, "delete", key)]
        if block and isinstance(block[2].get("relationships"), dict):
            into = (*block[1], "relationships")
            edits += [Edit(block[0], "insert", into, key=k, value=v) for k, v in specs.items()]
        elif block and added and not problem:
            edits.append(Edit(block[0], "insert", block[1], key="relationships", value=added))
            added = {}
        yield Finding(
            "model-joins",
            file,
            files.line(file, key),
            key,
            f"Model '{model_id}': write its joins as graph.relationships rows by hand."
            if problem
            else f"Model '{model_id}': its joins are graph.relationships rows now.",
            () if problem else tuple(edits),
        )


def _object_as(files: PackageFiles) -> Iterator[Finding]:
    namespace, graph = _namespace(files), _graph(files)
    rows = [
        (file, path, row, f"entity.{namespace}_{slug(name)}")
        for name, (file, path, row) in graph.items()
    ]
    for model_id, file, path, model in _models(files):
        _, primary, _ = _primary(model_id, model, graph)
        entity = slug(primary) if primary in graph and not _fact(model) else ""
        for block, derived in (
            ("dimensions", f"dimension.{namespace}_{entity}_{{}}" if entity else ""),
            ("measures", f"measure.{namespace}.{{}}"),
        ):
            for key, row in (model.get(block) or {}).items():
                if isinstance(row, dict):
                    rows.append((file, (*path, block, key), row, derived.format(slug(str(key)))))
    for file, path, row, derived in rows:
        if "id" not in row:
            continue
        key = (*path, "id")
        redundant = "as" in row or (bool(namespace) and str(row["id"]) == derived)
        yield Finding(
            "object-as",
            file,
            files.line(file, key),
            key,
            "Delete id: the key derives it." if redundant else "Rename id to as.",
            (Edit(file, "delete", key) if redundant else Edit(file, "rename", key, key="as"),),
        )


def _loaded(files: PackageFiles) -> tuple[dict[tuple[str, str], Any], set[str]]:
    """Each measure as the loader reads it, by (model id, key), and the metric keys and ids
    authored beside it; nothing when the package won't load."""
    from ..config import _load_package_source, _parse_package, normalize_package
    from ..package_snapshot import CapturedSource

    captured = CapturedSource(
        str(files.source), files.directory, tuple(sorted(files.contents.items()))
    )
    try:
        normalized = normalize_package(_load_package_source(str(files.source), captured=captured))
        config = _parse_package(deepcopy(normalized), path=str(files.source))
    except Exception:  # noqa: BLE001 - each finding stops instead
        return {}, set()
    by_id = {measure.id: measure for measure in config.measures}
    measures = {
        (str(model_id), str(key)): by_id.get(str((row or {}).get("id")))
        for model_id, model in normalized["models"].items()
        for key, row in (model.get("measures") or {}).items()
    }
    metrics = _mapping(normalized.get("metrics"))
    return measures, {
        *map(str, metrics),
        *(str(_mapping(row).get("id")) for row in metrics.values()),
    }


def _metric(measure: Any, publish: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """The metric ``publish:`` made of ``measure``, as authored fields."""
    name = str(publish.get("name") or measure.name or measure.id.split("measure.", 1)[-1])
    label = str(publish.get("label", measure.label or title(name.split(".")[-1])))
    roles = list(measure.compatible_temporal_roles)
    spec: dict[str, Any] = {
        "as": str(publish.get("id") or f"metric.{name}"),
        "name": name,
        "kind": "semi_additive" if measure.measure_class == "semi_additive" else "aggregate",
        "measure": measure.id,
        "aggregation": measure.default_aggregation,
        "label": label,
        "description": str(publish.get("description", measure.description or label)),
        "value_type": str(publish.get("value_type") or measure.value_type or "number"),
        **({"temporal_role": roles[0]} if roles else {}),
        **({"compatible_temporal_roles": roles} if roles[1:] else {}),
        "meta": {**measure.meta, **dict(publish.get("meta") or {})},
        "operational": {**measure.operational, **dict(publish.get("operational") or {})},
        "examples": publish.get("examples") or row.get("examples"),
    }
    for field in ("comparison_family", "comparison_mode", "preferred_companion_metrics"):
        spec[field] = publish.get(field, row.get(field))
    return {key: value for key, value in spec.items() if value}


def _measure_publish(files: PackageFiles) -> Iterator[Finding]:
    strict = any(
        isinstance(package, dict) and package.get("schema_strict")
        for _, _, package in files.package()
    )
    namespace = _namespace(files)
    rows = [
        (model_id, file, (*path, "measures", key, "publish"), str(key), row)
        for model_id, file, path, model in _models(files)
        for key, row in (model.get("measures") or {}).items()
        if isinstance(row, dict) and isinstance(row.get("publish"), dict)
    ]
    measures, taken = _loaded(files) if rows and not strict else ({}, set())
    metrics: dict[str, dict[str, Any]] = {}
    found: list[tuple[str, YamlPath, str, tuple[Edit, ...]]] = []
    for model_id, file, key, name, row in rows:
        measure = measures.get((model_id, name))
        if strict:  # the profile never read it
            found.append(
                (
                    file,
                    key,
                    "Delete publish: the package never read it.",
                    (Edit(file, "delete", key),),
                )
            )
            continue
        spec = _metric(measure, row["publish"], row) if measure is not None else {}
        if not spec or {name, spec["as"]} & taken or "topics" in row["publish"] or "topics" in row:
            found.append((file, key, f"Author the metric measure '{name}' published by hand.", ()))
            continue
        if spec["as"] == f"metric.{namespace}.{slug(name)}":
            del spec["as"]  # the key derives both
        if spec["name"] == f"{namespace}.{slug(name)}":
            del spec["name"]
        metrics[name] = spec
        message = f"Author the metric measure '{name}' published under metrics:."
        found.append((file, key, message, (Edit(file, "replace", key, value=False),)))
    blocks = [(f, p, v) for f, p, v in files._sections("metrics") if p[-1:] == ("metrics",)]
    if not metrics:
        inserts: tuple[Edit, ...] = ()
    elif blocks and isinstance(blocks[0][2], dict) and blocks[0][2]:
        file, path, _ = blocks[0]
        inserts = tuple(Edit(file, "insert", path, key=k, value=v) for k, v in metrics.items())
    elif blocks:
        inserts = (Edit(blocks[0][0], "replace", blocks[0][1], value=metrics),)
    elif files.directory:
        inserts = (Edit("metrics.yml", "create", value={"metrics": metrics}),)
    else:
        inserts = (Edit(files.source.name, "insert", (), key="metrics", value=metrics),)
    for file, key, message, edits in found:
        if edits and not strict:
            edits, inserts = (*edits, *inserts), ()
        yield Finding("measure-auto-publish", file, files.line(file, key), key, message, edits)


def _profile(files: PackageFiles) -> Iterator[Finding]:
    for file, path, package in files.package():
        if isinstance(package, dict) and "schema_strict" in package:
            key = (*path, "schema_strict")
            message = "Delete schema_strict: strict is the only profile."
            yield Finding(
                "package-schema-strict",
                file,
                files.line(file, key),
                key,
                message,
                (Edit(file, "delete", key),),
            )


RULES = (
    Rule(
        "ignored-key",
        "0.3.2",
        "same_meaning",
        "Remove keys the loader never read.",
        _keys,
        refused=True,
    ),
    Rule(
        "package-schema-strict",
        "0.3.2",
        "drops",
        "Delete package.schema_strict; strict is the only profile.",
        _profile,
        masks=("package.schema_strict",),
        refused=True,
    ),
    Rule(
        "measure-auto-publish",
        "0.3.2",
        "same_meaning",
        "Author each metric a measure's publish: block published.",
        _measure_publish,
        refused=True,
    ),
    Rule(
        "model-grain",
        "0.3.2",
        "same_meaning",
        "Delete a model grain: equal to its entity's key; bind the entity instead.",
        _model_grain,
        refused=True,
    ),
    Rule(
        "model-primary-key",
        "0.3.2",
        "same_meaning",
        "State a model's keys and singular entity in its entities: block.",
        _model_primary_key,
        refused=True,
    ),
    Rule(
        "model-joins",
        "0.3.2",
        "same_meaning",
        "Move model joins: to graph.relationships rows with the same ids.",
        _model_joins,
        refused=True,
    ),
    Rule(
        "object-as",
        "0.3.2",
        "same_meaning",
        "Rename id: to as: on graph entities, dimensions and measures.",
        _object_as,
        refused=True,
    ),
)
