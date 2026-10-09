"""Remove keys released writers wrote where the loader never read them."""

from collections.abc import Iterator
from typing import Any

from ..schema import OBSERVATION_SCOPES
from .model import Edit, Finding, Option, PackageFiles, Rule, YamlPath


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


RULES = (
    Rule(
        "ignored-key",
        "0.3.2",
        "same_meaning",
        "Remove keys the loader never read.",
        _keys,
        refused=True,
    ),
)
