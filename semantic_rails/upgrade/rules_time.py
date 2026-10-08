"""Remove the separate query-axis hint; each model's default time supplies its axis."""

from collections.abc import Iterator
from itertools import chain

from .model import Edit, Finding, PackageFiles, Rule


def _axis(files: PackageFiles) -> Iterator[Finding]:
    defaults = (
        (file, (*path, "time"), row["time"])
        for file, path, row in files.defaults()
        if isinstance(row.get("time"), dict)
    )
    for file, path, row in chain(files.times(), defaults):
        if "default_query_axis" in row:
            key = (*path, "default_query_axis")
            yield Finding(
                "time-default-axis",
                file,
                files.line(file, key),
                key,
                "Remove default_query_axis; default: true declares the model's default time.",
                (Edit(file, "delete", key),),
            )


RULES = (
    Rule(
        "time-default-axis",
        "0.3.2",
        "drops",
        "Remove the separate query-axis hint.",
        _axis,
        masks=("temporal_roles.*.default_query_time_axis",),
    ),
)
