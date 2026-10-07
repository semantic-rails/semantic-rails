"""Canonical spellings for model, dimension, time, metric and segment fields."""

from collections.abc import Iterator

from .model import Edit, Finding, PackageFiles, Rule


def _aliases(files: PackageFiles) -> Iterator[Finding]:
    for rows, old, new in (
        (files.models(), "relation_ref", "relation"),
        (files.dimensions(), "type", "kind"),
        (files.times(), "temporal_class", "class"),
        (files.metrics(), "time", "temporal_role"),
        (files.segments(), "metric", "basis_metric"),
    ):
        for file, path, row in rows:
            if old not in row:
                continue
            conflict = new in row and (type(row[new]) is not type(row[old]) or row[new] != row[old])
            edits = (
                ()
                if conflict
                else (Edit(file, "delete", (*path, old)),)
                if new in row
                else (Edit(file, "rename", (*path, old), key=new),)
            )
            yield Finding(
                "authoring-aliases",
                file,
                files.line(file, (*path, old)),
                path,
                f"Rewrite {old} as {new}."
                if edits
                else f"{old} and {new} disagree; rewrite by hand.",
                edits,
            )
    for file, path, row in files.measures():
        if "snapshot_policy" not in row:
            continue
        raw = row.get("accumulation", "")
        legacy = str(row["snapshot_policy"]).strip().lower()
        value = (
            {**raw, "snapshot": str(raw.get("snapshot", legacy) or legacy).strip().lower()}
            if isinstance(raw, dict)
            else {"kind": str(raw or "").strip().lower(), "snapshot": legacy}
        )
        yield Finding(
            "authoring-aliases",
            file,
            files.line(file, (*path, "snapshot_policy")),
            path,
            "Move snapshot_policy into accumulation.snapshot.",
            (
                Edit(file, "delete", (*path, "snapshot_policy")),
                Edit(file, "replace", (*path, "accumulation"), value=value)
                if "accumulation" in row
                else Edit(file, "insert", path, key="accumulation", value=value),
            ),
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
