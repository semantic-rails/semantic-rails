"""Expression rules: each expression node has one spelling."""

from __future__ import annotations

from collections.abc import Iterator

from .model import Edit, Finding, PackageFiles, Row, Rule, _walk
from .rules_calls import rewritten
from .v1_to_v2 import RENAMED_KEYS as _KEYS
from .v1_to_v2 import RENAMED_KINDS as _KINDS


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
    # A query in the previous call shape is rewritten whole, these spellings included.
    trees += [
        row
        for row in files.queries()
        if row[1][-1:] != ("membership",) and rewritten(files, *row) is None
    ]
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
