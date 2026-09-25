"""One spelling for warehouse relations: the runtime's raw dotted form.

The SQL renderer splits an authored relation such as ``sales-data.fct orders``
on its dots and quotes each part, so a part may hold spaces, hyphens or quotes
but never a dot. Authoring and introspection accept and return that form.
"""

from __future__ import annotations


def relation_parts(relation: str) -> list[str] | None:
    """A relation's dotted parts (at most three), or None when it cannot be one."""
    parts = str(relation or "").strip().split(".")
    # A relation is a dotted sequence of names, not a SQL fragment or a path.
    if len(parts) > 3 or not all(
        part and part.isprintable() and not any(ch in "/\\" for ch in part) for part in parts
    ):
        return None
    return parts


def quote_identifier(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def quote_relation(relation: str) -> str:
    """Quote each dotted part, as the SQL renderer splits it."""
    return ".".join(quote_identifier(part) for part in str(relation).split("."))
