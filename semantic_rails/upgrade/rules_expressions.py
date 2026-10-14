"""Expression rules: each expression node has one spelling."""

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


def _expression_nodes(files: PackageFiles) -> Iterator[Row]:
    """Expression nodes in metrics, segments, measure `expr:`, relation steps and every example,
    test and membership query."""
    roots = {(file, path) for file, path, _ in (*files.metrics(), *files.segments())}
    yield from (row for row in files.expressions() if row[:2] not in roots)
    trees = [
        (file, (*path, "expr"), measure["expr"])
        for file, path, measure in files.measures()
        if isinstance(measure.get("expr"), dict)
    ]
    trees += [
        (file, (*path, "steps"), relation["steps"])
        for file, path, relation in files.relations()
        if isinstance(relation.get("steps"), list)
    ]
    trees += [row for row in files.queries() if row[1][-1:] != ("membership",)]
    for file, path, tree in trees:
        for child_path, child in _walk(tree, path, expression=True):
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
        "expression-arithmetic",
        "0.3.2",
        "same_meaning",
        "Write one spelling per expression node: kind arithmetic (not binary), measure (not "
        "measure_ref), conversion matching_mode (not matching), in/not_in expr (not left).",
        _arithmetic,
        refused=True,
    ),
)
