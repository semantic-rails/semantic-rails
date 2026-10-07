"""Canonical spellings for model, dimension, time, metric and segment fields."""

from collections.abc import Iterator
from itertools import chain

from .model import Edit, Finding, PackageFiles, Rule


def _aliases(files: PackageFiles) -> Iterator[Finding]:
    defaults = list(files.defaults())
    effective_defaults = {key: value for _, _, row in defaults for key, value in row.items()}
    for rows, old, new, default_key in (
        (files.models(), "relation_ref", "relation", ""),
        (files.dimensions(), "type", "kind", "dimension"),
        (files.times(), "temporal_class", "class", "time"),
        (files.metrics(), "time", "temporal_role", ""),
        (files.segments(), "metric", "basis_metric", ""),
    ):
        default_rows = (
            (file, (*path, default_key), row[default_key])
            for file, path, row in defaults
            if default_key and isinstance(row.get(default_key), dict)
        )
        for file, path, row in chain(rows, default_rows):
            if old not in row:
                continue
            inherited = path[-2:] != ("defaults", default_key) and new in (
                effective_defaults.get(default_key) or {}
            )
            conflict = new in row and (type(row[new]) is not type(row[old]) or row[new] != row[old])
            edits: tuple[Edit, ...] = (
                ()
                if inherited or conflict
                else (Edit(file, "delete", (*path, old)),)
                if new in row
                else (Edit(file, "rename", (*path, old), key=new),)
            )
            yield Finding(
                "authoring-aliases",
                file,
                files.line(file, (*path, old)),
                path,
                f"defaults.{default_key}.{new} supplies {new}; rewrite by hand."
                if inherited
                else f"Rewrite {old} as {new}."
                if edits
                else f"{old} and {new} disagree; rewrite by hand.",
                edits,
            )
    default_measures = (
        (file, (*path, "measure"), row["measure"])
        for file, path, row in defaults
        if isinstance(row.get("measure"), dict)
    )
    for file, path, row in chain(files.measures(), default_measures):
        if "snapshot_policy" not in row:
            continue
        raw = row.get("accumulation")
        legacy = str(row["snapshot_policy"]).strip().lower()
        edits = ()
        message = "Move snapshot_policy into accumulation.snapshot."
        measure_defaults = effective_defaults.get("measure") or {}
        if path[-2:] == ("defaults", "measure"):
            message = "defaults.measure.snapshot_policy changes inheritance; rewrite by hand."
        elif inherited_keys := {"accumulation", "snapshot_policy"} & measure_defaults.keys():
            message = (
                f"defaults.measure.{sorted(inherited_keys)[0]} supplies the key; rewrite by hand."
            )
        elif isinstance(raw, dict) and "snapshot" in raw:
            if str(raw["snapshot"]).strip().lower() != legacy:
                message = "snapshot_policy and accumulation.snapshot disagree; rewrite by hand."
            else:
                edits = (Edit(file, "delete", (*path, "snapshot_policy")),)
        elif "accumulation" not in row or isinstance(raw, (str, dict)):
            value = (
                {**raw, "snapshot": legacy}
                if isinstance(raw, dict)
                else {"kind": raw, "snapshot": legacy}
                if "accumulation" in row
                else {"snapshot": legacy}
            )
            edits = (
                Edit(file, "delete", (*path, "snapshot_policy")),
                Edit(file, "replace", (*path, "accumulation"), value=value)
                if "accumulation" in row
                else Edit(file, "insert", path, key="accumulation", value=value),
            )
        else:
            message = "Unsupported accumulation value; rewrite by hand."
        yield Finding(
            "authoring-aliases",
            file,
            files.line(file, (*path, "snapshot_policy")),
            path,
            message,
            edits,
        )


RULES = (
    Rule(
        "authoring-aliases",
        "0.3.2",
        "same_meaning",
        "Use canonical authoring keys.",
        _aliases,
        refused=True,
    ),
)
