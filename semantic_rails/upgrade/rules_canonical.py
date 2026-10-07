"""Rewrite policy aliases to one flat authored row without changing enforcement."""

from collections.abc import Iterator, Mapping

from .model import Edit, Finding, Option, PackageFiles, Rule

ALIASES = {"config", "visibility", "rule", "description"}
COMMON = {"id", "kind", "object_ids", "audiences", "environments", "roles", "action", "rationale"}


def _flat(files: PackageFiles) -> Iterator[Finding]:
    for file, path, row in files.policies():
        if not ALIASES.intersection(row) or (row.get("kind") == "row_filter" and "config" in row):
            continue
        nested = row.get("config", {})
        if not isinstance(nested, Mapping):
            continue
        if (COMMON - {"action", "rationale"}).intersection(nested):
            yield Finding(
                "policy-flat",
                file,
                files.line(file, path),
                path,
                "Rewrite nested scope fields by hand; they were not policy scopes.",
            )
            continue

        def edits_for(values, nested=nested, row=row, file=file, path=path):
            merged = {**nested, **values}
            config = {
                **nested,
                **{key: value for key, value in values.items() if key not in COMMON | {"config"}},
            }
            action = (
                str(
                    values.get("action", "")
                    or config.get("action")
                    or config.get("visibility")
                    or ""
                )
                .strip()
                .lower()
            )
            rationale = (
                values.get("rationale", values.get("rule", ""))
                or config.get("rule")
                or config.get("rationale")
                or config.get("description")
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
        alternatives = {
            f"config.{key}": {**row, key: nested[key]}
            for key in nested.keys() & row.keys()
            if nested[key] != row[key]
        }
        actions = {
            key: value
            for key, value in (
                ("action", row.get("action")),
                ("config.action", nested.get("action")),
                ("visibility", row.get("visibility")),
                ("config.visibility", nested.get("visibility")),
            )
            if value
        }
        if len({str(value).strip().lower() for value in actions.values()}) > 1:
            alternatives.update({key: {**row, "action": value} for key, value in actions.items()})
        options = (
            (
                Option("keep-effective", "Keep current enforcement.", False, edits),
                *(
                    Option(
                        f"use-{key}",
                        f"Use {key}; other fields keep current enforcement.",
                        True,
                        edits_for(values),
                    )
                    for key, values in sorted(alternatives.items())
                ),
            )
            if alternatives
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
                "redact never masked values; it refused like deny. Refusals and inspect now name deny.",
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
        "redact never masked values; it refused like deny. Refusals and inspect now name deny.",
        _redact,
    ),
)
