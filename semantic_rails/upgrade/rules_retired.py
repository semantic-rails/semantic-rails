"""Rules for declarations 0.3.2rc3 retired; deleting them is the only current form."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from .model import Edit, Finding, PackageFiles, Rule, YamlPath, _walk

_NOTE = "see the 0.3.2rc3 upgrade notes"


def _delete(
    rule: str, files: PackageFiles, file: str, path: YamlPath, keys: Iterable[str], message: str
) -> Finding:
    edits = tuple(Edit(file, "delete", (*path, key)) for key in keys)
    return Finding(rule, file, files.line(file, edits[0].path), path, message, edits)


def _prune(
    rule: str, files: PackageFiles, file: str, path: YamlPath, keys: Iterable[str], message: str
) -> Finding:
    """Delete ``keys``, or the block itself when they are all it holds.

    Only for blocks the loader reads the same empty or absent (defaults, ``rollup_safe``), so
    the editor removes whole lines instead of reformatting an emptied block mapping.
    """
    document: Any = files.documents[file]
    for part in path:
        document = document[part]
    keys = tuple(key for key in keys if key in document)
    if path and set(document) == set(keys):
        path, keys = path[:-1], (str(path[-1]),)
    return _delete(rule, files, file, path, keys, message)


def _null_behavior(files: PackageFiles) -> Iterator[Finding]:
    message = f"null_behavior was retired; delete it ({_NOTE})"
    rows = [*files.metrics(), *files.expressions()]
    for file, path, query in files.queries():
        rows.extend(
            (file, child_path, child) for child_path, child in _walk(query, path, expression=True)
        )
    seen = set()
    for file, path, row in rows:
        if isinstance(row, dict) and "null_behavior" in row and (file, path) not in seen:
            seen.add((file, path))
            yield _delete("null-behavior", files, file, path, ("null_behavior",), message)


def _measure_parent_rollup(files: PackageFiles) -> Iterator[Finding]:
    keys = ("subject_entity", "aggregation_entity")
    message = f"parent-rollup declarations were retired; delete them ({_NOTE})"
    for file, path, defaults in files.defaults():
        measure = defaults.get("measure") if isinstance(defaults, dict) else None
        if isinstance(measure, dict) and set(keys) & set(measure):
            yield _prune("measure-parent-rollup", files, file, (*path, "measure"), keys, message)
    for file, path, measure in files.measures():
        if present := [key for key in keys if key in measure]:
            yield _delete("measure-parent-rollup", files, file, path, present, message)


def _forward_rollup_hints(files: PackageFiles) -> Iterator[Finding]:
    keys = ("rollup_safe_aggregations", "rollup_safe")
    message = f"forward rollup hints were retired; delete them ({_NOTE})"
    for file, path, defaults in files.defaults():
        relationship = defaults.get("relationship") if isinstance(defaults, dict) else None
        if isinstance(relationship, dict) and set(keys) & set(relationship):
            yield _prune(
                "forward-rollup-hints", files, file, (*path, "relationship"), keys, message
            )
    for file, path, row in files.relationships():
        rollup = row.get("rollup_safe")
        if path[-2] == "joins" and (present := [key for key in keys if key in row]):
            yield _delete("forward-rollup-hints", files, file, path, present, message)
        elif isinstance(rollup, list):
            yield _delete("forward-rollup-hints", files, file, path, ("rollup_safe",), message)
        elif isinstance(rollup, dict) and "forward" in rollup:
            # Only the reverse population-count permission stays under graph.relationships.
            path = (*path, "rollup_safe")
            yield _prune("forward-rollup-hints", files, file, path, ("forward",), message)


def _path_preference(files: PackageFiles) -> Iterator[Finding]:
    message = (
        "path_preference weights were retired; delete them and record any route an "
        f"ambiguous pair needs as a graph.path_preferences row ({_NOTE})"
    )
    for file, path, row in files.relationships():
        if "path_preference" in row:
            yield _delete(
                "relationship-path-preference", files, file, path, ("path_preference",), message
            )


def _query_path_policy(files: PackageFiles) -> Iterator[Finding]:
    message = f"the query key path_policy was retired; delete it ({_NOTE})"
    for file, path, query in files.queries():
        if "path_policy" in query:
            yield _delete("query-path-policy", files, file, path, ("path_policy",), message)


RULES: tuple[Rule, ...] = (
    Rule(
        "null-behavior",
        "0.3.2rc3",
        "retired",
        "Delete null_behavior from metrics, expressions and queries; aggregation and "
        f"observation_scope decide empty groups ({_NOTE}).",
        _null_behavior,
        refused=True,
    ),
    Rule(
        "measure-parent-rollup",
        "0.3.2rc3",
        "retired",
        "Delete subject_entity and aggregation_entity from measures and defaults.measure; "
        f"measures aggregate at their own model's grain ({_NOTE}).",
        _measure_parent_rollup,
        refused=True,
    ),
    Rule(
        "forward-rollup-hints",
        "0.3.2rc3",
        "retired",
        "Delete forward rollup hints: rollup_safe_aggregations and rollup_safe in relationship "
        "defaults and model joins, and rollup_safe.forward in graph.relationships, where "
        f"rollup_safe.reverse stays ({_NOTE}).",
        _forward_rollup_hints,
        refused=True,
    ),
    Rule(
        "relationship-path-preference",
        "0.3.2rc3",
        "retired",
        "Delete path_preference from relationships and joins; an ambiguous pair needs a "
        f"recorded graph.path_preferences route, which the upgrade never picks ({_NOTE}).",
        _path_preference,
        refused=True,
    ),
    Rule(
        "query-path-policy",
        "0.3.2rc3",
        "retired",
        "Delete the query key path_policy from example, test and segment membership queries "
        f"({_NOTE}).",
        _query_path_policy,
    ),
)
