"""Query IR rules: version 1 is the only contract, with the same query shape as version 2."""

from __future__ import annotations

from collections.abc import Iterator

from .model import Edit, Finding, PackageFiles, Rule


def _version_two(files: PackageFiles) -> Iterator[Finding]:
    for file, path, query in files.queries():
        if query.get("version") == 2:
            edit = Edit(file, "replace", (*path, "version"), value=1)
            line = files.line(file, edit.path)
            yield Finding("query-ir-version", file, line, path, "version: 2 becomes 1", (edit,))


RULES: tuple[Rule, ...] = (
    Rule(
        "query-ir-version",
        "0.3.2",
        "same_meaning",
        "Write version: 1 in example, test and segment membership queries; version 2 had the "
        "same query shape.",
        _version_two,
    ),
)
