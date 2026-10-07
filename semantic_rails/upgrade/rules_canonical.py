"""Rewrite policy aliases to one flat authored row without changing enforcement."""

from collections.abc import Iterator, Mapping

from .model import Edit, Finding, Option, PackageFiles, Rule

ALIASES = {"config", "visibility", "rule", "description"}
COMMON = {"id", "kind", "object_ids", "audiences", "environments", "roles", "action", "rationale"}


def _flat(files: PackageFiles) -> Iterator[Finding]:
    for file, path, row in files.policies():
        if not ALIASES.intersection(row):
            continue
        nested = row.get("config", {})
        if not isinstance(nested, Mapping):
            continue

        def edits_for(values, nested=nested, row=row, file=file, path=path):
            merged = {**nested, **values}
            action = values.get("action") or nested.get("action") or merged.get("visibility") or ""
            rationale = (
                values.get("rationale")
                or values.get("rule")
                or merged.get("rule")
                or merged.get("rationale")
                or merged.get("description")
                or ""
            )
            target = {key: value for key, value in merged.items() if key not in ALIASES | COMMON}
            target.update({key: value for key, value in values.items() if key in COMMON})
            if action:
                target["action"] = (
                    "deny"
                    if values.get("kind") == "object_access"
                    and str(action).strip().lower() == "redact"
                    else action
                )
            if rationale:
                target["rationale"] = rationale
            return tuple(
                Edit(file, "delete", (*path, key)) for key in row if key not in target
            ) + tuple(
                Edit(file, "replace", (*path, key), value=value)
                if key in row
                else Edit(file, "insert", path, key=key, value=value)
                for key, value in target.items()
                if key not in row or row[key] != value
            )

        edits = edits_for(row)
        conflicts = {
            key: nested[key] for key in nested.keys() & row.keys() if nested[key] != row[key]
        }
        options = (
            (
                Option("flat", "Keep the flat values and current enforcement.", False, edits),
                Option(
                    "nested",
                    "Use the conflicting nested values.",
                    True,
                    edits_for({**row, **conflicts}),
                ),
            )
            if conflicts
            else ()
        )
        yield Finding(
            "policy-flat",
            file,
            files.line(file, path),
            path,
            "Flatten policy config and write action and rationale explicitly.",
            () if options else edits,
            options,
        )


def _redact(files: PackageFiles) -> Iterator[Finding]:
    for file, path, row in files.policies():
        if (
            not ALIASES.intersection(row)
            and row.get("kind") == "object_access"
            and str(row.get("action", "")).strip().lower() == "redact"
        ):
            yield Finding(
                "policy-redact-deny",
                file,
                files.line(file, (*path, "action")),
                (*path, "action"),
                "redact denied access; write deny.",
                (Edit(file, "replace", (*path, "action"), value="deny"),),
            )


RULES = (
    Rule(
        "policy-flat",
        "0.3.2",
        "same_meaning",
        "Flatten policy config and rationale/action aliases.",
        _flat,
    ),
    Rule(
        "policy-redact-deny",
        "0.3.2",
        "same_meaning",
        "Replace object_access redact with the equivalent deny action.",
        _redact,
    ),
)
