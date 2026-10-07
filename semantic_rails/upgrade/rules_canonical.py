"""Rewrite policy aliases to one flat authored row without changing enforcement."""

from collections.abc import Iterator, Mapping

from .model import Edit, Finding, PackageFiles, Rule

ALIASES = {"config", "visibility", "rule", "description"}
COMMON = {"id", "kind", "object_ids", "audiences", "environments", "roles", "action", "rationale"}


def _flat(files: PackageFiles) -> Iterator[Finding]:
    for file, path, row in files.policies():
        if not ALIASES.intersection(row) or (row.get("kind") == "row_filter" and "config" in row):
            continue
        nested = row.get("config", {})
        if not isinstance(nested, Mapping):
            yield Finding(
                "policy-flat",
                file,
                files.line(file, path),
                path,
                f"Policy '{row['id']}': config must be a mapping; rewrite it by hand.",
            )
            continue
        conflict = next(iter(sorted((COMMON - {"action", "rationale"}).intersection(nested))), "")
        twins = [(key, nested[key], row[key]) for key in nested.keys() & row.keys()]
        for aliases in (("action", "visibility"), ("rationale", "rule", "description")):
            values = [
                (key, values[key]) for values in (row, nested) for key in aliases if key in values
            ]
            twins.extend((key, value, values[0][1]) for key, value in values[1:])
        conflict = conflict or next(
            (key for key, left, right in twins if type(left) is not type(right) or left != right),
            "",
        )
        if conflict:
            yield Finding(
                "policy-flat",
                file,
                files.line(file, path),
                path,
                f"Policy '{row['id']}': key '{conflict}' cannot be rewritten automatically.",
            )
            continue
        config = {
            **nested,
            **{key: value for key, value in row.items() if key not in COMMON | {"config"}},
        }
        action = (
            str(
                str(row.get("action", ""))
                or config.get("action", "")
                or config.get("visibility", "")
            )
            .strip()
            .lower()
        )
        rationale = str(
            str(row.get("rationale", row.get("rule", "")))
            or config.get("rule", "")
            or config.get("rationale", "")
            or config.get("description", "")
        )
        if (
            row.get("kind") == "package_release"
            and str(config.get("label", "") or str(row.get("action", "")) or "").strip()
            != str(config.get("label", "") or action or "").strip()
        ):
            yield Finding(
                "policy-flat",
                file,
                files.line(file, path),
                path,
                f"Policy '{row['id']}': key 'action' would change the release label.",
            )
            continue
        target = {
            key: value
            for key, value in config.items()
            if key not in ALIASES | {"action", "rationale"}
        }
        target.update({key: value for key, value in row.items() if key in COMMON})
        if action or "action" in row:
            target["action"] = (
                "deny" if row.get("kind") == "object_access" and action == "redact" else action
            )
        if rationale or "rationale" in row:
            target["rationale"] = rationale
        edits = tuple(
            Edit(file, "delete", (*path, key)) for key in row if key not in target
        ) + tuple(
            Edit(file, "replace", (*path, key), value=value)
            if key in row
            else Edit(file, "insert", path, key=key, value=value)
            for key, value in target.items()
            if key not in row or type(row[key]) is not type(value) or row[key] != value
        )
        yield Finding(
            "policy-flat",
            file,
            files.line(file, path),
            path,
            "Flatten policy config and rationale/action aliases.",
            edits,
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
