"""One spelling for warehouse relations: the runtime's raw dotted form.

The SQL renderer splits an authored relation such as ``sales-data.fct orders``
on its dots and quotes each part, so a part may hold spaces, hyphens or quotes
but never a dot. Authoring and introspection accept and return that form.
"""

from __future__ import annotations

import re

_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def relation_parts(relation: str) -> list[str] | None:
    """A relation's dotted parts (at most three), or None when it cannot be one."""
    parts = str(relation or "").strip().split(".")
    # A relation is a dotted sequence of names, not a SQL fragment or a path.
    if len(parts) > 3 or not all(
        part and part.isprintable() and not any(ch in "/\\" for ch in part) for part in parts
    ):
        return None
    return parts


def plain_relation_parts(relation: str) -> list[str] | None:
    """A relation's dotted parts when each is a plain SQL identifier, else None.

    A probe the runtime builds outside the compiler accepts only these names, so
    a package value never carries quotes, statements or file paths into its SQL.
    """
    parts = relation_parts(relation)
    if parts is None or not all(_PLAIN_NAME.fullmatch(part) for part in parts):
        return None
    return parts


def quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def quote_relation(relation: str) -> str:
    """Quote each dotted part, as the SQL renderer splits it."""
    return ".".join(quote_identifier(part) for part in str(relation).split("."))
