"""A joined call's part, settled on every key of the call (``reading_part_domain``).

A part of a joined call (``joined_parts``) is lowered as the call it is, with one step added
between its groups and the guard that settles them: its groups are read beside the keys of
the whole joined call, so a key another part has and this one doesn't is one of this part's
groups too, and the guard settles it exactly as it settles a group a call's own leaves never
reached (0 where the part's rows are in scope and its bucket is loaded, NULL otherwise). A
derived output (a difference, a ratio) is then computed over those settled values by the
part's own projection.

Three CTEs carry that step, in this order, after the part's groups (``base``):

- ``ROWS`` the part's groups, each marked as the part's own (``PRESENT`` true);
- ``KEYS`` the keys every part joins on. Inside the part it lists the part's own keys that
  have no NULL, so the part's statement alone answers exactly as the call alone does; the
  joined call replaces it with the keys of every part (plain equality, NULL never matches);
- ``DOMAIN`` those keys FULL OUTER JOINed to the part's groups: every key of the call once,
  plus the part's own groups with a NULL key, which no other part's key ever matches.

Only the request's own lowering takes the step (``plan_is_root``): a total's coarser call, a
predicate's source and any other nested compile read their own rows as before.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from ..sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCte,
    SqlField,
    SqlIdentifier,
    SqlJoin,
    SqlLiteral,
    SqlSelect,
    SqlTableRef,
)
from .dependencies import holding, plan_is_root, settles_as_a_call

ROWS = "joined_part_rows"
KEYS = "joined_part_keys"
DOMAIN = "joined_part_domain"
# The hidden column marking a part's own groups: true on them, false on a key only another
# part has.
PRESENT = "joined_part_present"


@dataclass
class PartDomain:
    """What lowering recorded for one part: its key columns (as its output names them) and
    whether the step was taken."""

    keys: list[str] = field(default_factory=list)
    taken: int = 0


_domain: ContextVar[PartDomain | None] = ContextVar("part_domain", default=None)


@contextmanager
def reading_part_domain() -> Iterator[PartDomain]:
    """Lower the request's own query as a part of a joined call."""
    domain = PartDomain()
    with holding(_domain, domain):
        yield domain


def part_domain() -> PartDomain | None:
    """The part being lowered, when the query lowering now is a joined call's part."""
    domain = _domain.get()
    if domain is None or not plan_is_root() or not settles_as_a_call():
        return None
    return domain


def domain_ctes(
    domain: PartDomain,
    base: str,
    keys: Sequence[str],
    columns: Sequence[str],
    *,
    window_bucket: tuple[str, Any] | None = None,
) -> list[SqlCte]:
    """The part's groups (``base``, with ``columns``) read beside every key of the call; the
    last CTE (``DOMAIN``) stands where ``base`` stood. Under a window total the time column is
    no key, and a key only another part has reads the window's one bucket
    (``window_bucket``: the column and its value), as the part's own rows do."""
    domain.keys = list(keys)
    domain.taken += 1
    rows = SqlSelect(
        select=[
            *(SqlField(SqlIdentifier(parts=["base", name]), name) for name in columns),
            SqlField(SqlLiteral(True), PRESENT),
        ],
        from_table=SqlTableRef(name=base, alias="base"),
    )
    own_keys = SqlSelect(
        select=[SqlField(SqlIdentifier(parts=["own", key]), key) for key in keys],
        from_table=SqlTableRef(name=ROWS, alias="own"),
        where=[
            SqlBinary(SqlIdentifier(parts=["own", key]), "IS NOT", SqlLiteral(None)) for key in keys
        ],
        distinct=True,
    )
    on: Any = None
    for key in keys:
        # Plain equality: a NULL key is the part's own group, never another part's.
        equal = SqlBinary(
            SqlIdentifier(parts=["keys", key]), "=", SqlIdentifier(parts=["own", key])
        )
        on = equal if on is None else SqlBinary(on, "AND", equal)

    def value(name: str) -> Any:
        column = SqlIdentifier(parts=["own", name])
        if name in keys:
            return SqlCall("COALESCE", [SqlIdentifier(parts=["keys", name]), column])
        if window_bucket is not None and name == window_bucket[0]:
            return SqlCall("COALESCE", [column, SqlLiteral(window_bucket[1])])
        return column

    domain_select = SqlSelect(
        select=[
            *(SqlField(value(name), name) for name in columns),
            SqlField(
                SqlCall("COALESCE", [SqlIdentifier(parts=["own", PRESENT]), SqlLiteral(False)]),
                PRESENT,
            ),
        ],
        from_table=SqlTableRef(name=KEYS, alias="keys"),
        joins=[SqlJoin("FULL OUTER", SqlTableRef(name=ROWS, alias="own"), on=on)],
    )
    return [
        SqlCte(name=ROWS, query=rows),
        SqlCte(name=KEYS, query=own_keys),
        SqlCte(name=DOMAIN, query=domain_select),
    ]
