"""Rewrite example and test queries written in the previous call shape as the current call."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..errors import refusal_text
from .model import Edit, Finding, PackageFiles, Rule, YamlPath
from .v1_to_v2 import (
    FLAG_CODES,
    KEPT,
    Result,
    previous_call,
    refusal,
    translate_columns,
    translate_rows,
)

_RULE = "query-call-v2"
# The arguments beside a call that an example or test entry holds, beside its query.
_HELD = ("now", "context")
# Arguments that can change what an entry returns, with no place in an entry.
_UNHELD = ("max_rows", "limits")
# Each entry key that names output columns, and how its time column is renamed.
_OUTPUTS = (
    (("expected_shape", "columns"), translate_columns),
    (("columns",), translate_columns),
    (("expected_rows",), translate_rows),
)


def _at(node: Any, path: YamlPath) -> Any:
    """The value at ``path``, or None."""
    for part in path:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and isinstance(part, int) and part < len(node):
            node = node[part]
        else:
            return None
    return node


def _site(text: str) -> YamlPath:
    """A translator path (``where[0].op``) as a YAML path."""
    return tuple(int(part) if part.isdigit() else part for part in re.findall(r"[^.\[\]]+", text))


def rewritten(files: PackageFiles, file: str, path: YamlPath, query: Any) -> Result | None:
    """The translation of an example's or test's query written in the previous call shape;
    None for every other query, a refusal test's included (its query is meant to be refused)."""
    entry = _at(files.documents[file], path[:-1])
    if path[-1:] != ("query",) or not isinstance(entry, dict):
        return None
    if entry.get("kind") == "validate_fails_with_code":
        return None
    return previous_call(query)


def _edits(file: str, path: YamlPath, old: Any, new: Any) -> list[Edit]:
    """The fewest path edits that turn ``old`` into ``new``, so unchanged lines keep their form."""
    if isinstance(old, dict) and isinstance(new, dict):
        edits: list[Edit] = []
        previous = ""
        for key, value in new.items():
            if key in old:
                edits.extend(_edits(file, (*path, key), old[key], value))
            else:
                edits.append(Edit(file, "insert", path, key=key, value=value, after=previous))
            previous = key
        return edits + [Edit(file, "delete", (*path, key)) for key in old if key not in new]
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [
            edit
            for index, (before, after) in enumerate(zip(old, new, strict=True))
            for edit in _edits(file, (*path, index), before, after)
        ]
    if type(old) is type(new) and old == new:
        return []
    return [Edit(file, "replace", path, value=new)]


def _calls(files: PackageFiles) -> Iterator[Finding]:
    for file, path, query in files.queries():
        result = rewritten(files, file, path, query)
        if result is None:
            continue
        entry_path = path[:-1]
        entry = _at(files.documents[file], entry_path)
        # Each site the rewrite can't make, as (its path, why).
        stops = [((*path, *_site(flag["path"])), flag["detail"]) for flag in result.kept_flags]
        if not stops and (error := refusal(result.query)) is not None:
            where = str(error.details.get("path") or "")
            text, hint = refusal_text(str(error), error.details)
            detail = text.removeprefix(f"{where}: ") + (f" Hint: {hint}" if hint else "")
            stops.append(((*path, *_site(where)), detail))
        stops += [
            ((*path, key), f"an example or test has no {key}; delete it by hand")
            for key in _UNHELD
            if key in result.call
        ]
        stops += [
            ((*entry_path, key), f"the query's {key} differs from the entry's; keep one by hand")
            for key in _HELD
            if key in result.call and entry.get(key) not in (None, result.call[key])
        ]
        edits = _edits(file, path, query, result.query)
        after = "query"
        for key in _HELD:
            if key in result.call and entry.get(key) is None:
                edits.append(
                    Edit(file, "insert", entry_path, key=key, value=result.call[key], after=after)
                )
                after = key
        for keys, translate in _OUTPUTS:
            if (outputs := _at(entry, keys)) is not None:
                renamed, flags = translate(outputs, ".".join(keys))
                stops += [((*entry_path, *_site(flag["path"])), flag["detail"]) for flag in flags]
                edits += _edits(file, (*entry_path, *keys), outputs, renamed)
        if stops:
            details: dict[YamlPath, list[str]] = {}
            for site, detail in stops:
                details.setdefault(site, []).append(detail)
            for site, reasons in details.items():
                where = "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}"
                    for part in site[len(entry_path) :]
                ).lstrip(".")
                message = f"{where}: {'; '.join(reasons)}"
                yield Finding(_RULE, file, files.line(file, site), site, message)
            continue
        notes = [
            f"{flag['path'] or 'query'}: {flag['detail']}"
            for flag in result.flags
            if FLAG_CODES[flag["code"]][0] != KEPT
        ]
        message = "; ".join(["Rewrite the query as the current call", *notes])
        yield Finding(_RULE, file, files.line(file, path), path, message, tuple(edits))


RULES: tuple[Rule, ...] = (
    Rule(
        _RULE,
        "0.3.2",
        "same_meaning",
        "Rewrite each example and test query written in the previous call shape as the current "
        "call, with its now and context beside the query and its expected time column renamed "
        "period. A hit names each site whose meaning a person should check: not keeps unknown "
        "rows, and the text words a LIKE becomes ignore case. A site with no current form "
        "stops the upgrade; refusal tests keep their queries.",
        _calls,
    ),
)
