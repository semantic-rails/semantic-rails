"""Query IR rules: version 1 is the only contract, and each expression node has one spelling."""

from __future__ import annotations

from collections.abc import Iterator

from .model import Edit, Finding, PackageFiles, Row, Rule, _walk

# Retired expression kinds, and the kind that parses to the same node.
_KINDS = {"binary": "arithmetic", "measure_ref": "measure"}
# Retired expression keys by kind, as (retired, current); the current key wins when both appear.
_KEYS = {
    "conversion": ("matching", "matching_mode"),
    "in": ("left", "expr"),
    "not_in": ("left", "expr"),
}


def _version_two(files: PackageFiles) -> Iterator[Finding]:
    for file, path, query in files.queries():
        if not path or path[-1] != "query" or isinstance(query.get("version"), bool):
            continue
        try:
            version = int(query.get("version", 1))
        except (TypeError, ValueError, OverflowError):
            continue
        if version == 2:
            edit = Edit(file, "replace", (*path, "version"), value=1)
            line = files.line(file, edit.path)
            yield Finding("query-ir-version", file, line, path, "version: 2 becomes 1", (edit,))


def _expression_nodes(files: PackageFiles) -> Iterator[Row]:
    """Expression nodes in metrics, segments and every example, test and membership query."""
    roots = {(file, path) for file, path, _ in (*files.metrics(), *files.segments())}
    yield from (row for row in files.expressions() if row[:2] not in roots)
    for file, path, query in files.queries():
        if path[-1:] != ("membership",):
            for child_path, child in _walk(query, path, expression=True):
                if isinstance(child, dict):
                    yield file, child_path, child


def _arithmetic(files: PackageFiles) -> Iterator[Finding]:
    for file, path, node in _expression_nodes(files):
        kind = str(node.get("kind", "")).strip()
        if kind in _KINDS:
            edit = Edit(file, "replace", (*path, "kind"), value=_KINDS[kind])
            message = f"kind {kind} becomes {_KINDS[kind]}"
        elif kind in _KEYS and _KEYS[kind][0] in node:
            old, new = _KEYS[kind]
            if new in node:
                edit, message = Edit(file, "delete", (*path, old)), f"Delete {old}; {new} wins."
            else:
                edit, message = Edit(file, "rename", (*path, old), key=new), f"{old} becomes {new}"
        else:
            continue
        line = files.line(file, edit.path)
        yield Finding("expression-arithmetic", file, line, edit.path, message, (edit,))


RULES: tuple[Rule, ...] = (
    Rule(
        "query-ir-version",
        "0.3.2",
        "same_meaning",
        "Write version: 1 in example and test queries; version 2 had the same query shape.",
        _version_two,
    ),
    Rule(
        "expression-arithmetic",
        "0.3.2",
        "same_meaning",
        "Write one spelling per expression node: kind arithmetic (not binary), measure (not "
        "measure_ref), conversion matching_mode (not matching), in/not_in expr (not left).",
        _arithmetic,
        refused=True,
    ),
)
