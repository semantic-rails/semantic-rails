"""Deterministic limited ordering and disclosure from one boundary row."""

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from .db_parts.base import QueryRows
from .errors import SemanticLayerError
from .sql_ast import SqlCase, SqlCaseWhen, SqlIdentifier, SqlIsNull, SqlLiteral, SqlOrder, SqlSelect


def limit_order(query: SqlSelect, groups: Sequence[str]) -> tuple[SqlSelect, tuple[str, ...]]:
    """Keep the requested order, then break ties by the remaining ``groups`` (the group_by
    columns, then the period) ascending, NULLS LAST, so a cut-off keeps the same rows."""
    if not query.order_by or query.limit is None:
        return query, ()
    keys = []
    for order in query.order_by:
        expression = order.expression
        # Policy ranking can prefix a column with its portable NULL indicator.
        # The following column term determines ties; its NULL indicator adds
        # no distinction between rows whose column values are equal.
        if (
            isinstance(expression, SqlCase)
            and len(expression.whens) == 1
            and isinstance(expression.whens[0].condition, SqlIsNull)
            and expression
            == SqlCase([SqlCaseWhen(expression.whens[0].condition, SqlLiteral(0))], SqlLiteral(1))
        ):
            expression = expression.whens[0].condition.expr
        matches = [
            field.alias
            for field in query.select
            if expression == field.expression or expression == SqlIdentifier(parts=[field.alias])
        ]
        if not matches and isinstance(expression, SqlIdentifier) and len(expression.parts) == 1:
            matches = [
                field.alias
                for field in query.select
                if isinstance(field.expression, SqlIdentifier)
                and field.expression.parts[-1:] == expression.parts
            ]
        if len(matches) != 1:
            raise SemanticLayerError(
                "INVALID_ORDER_BY", "Limited ordering must resolve to an output column."
            )
        keys.append(matches[0])
    outputs = {field.alias for field in query.select}
    remaining = [
        SqlOrder(SqlIdentifier(parts=[alias]), "ASC", nulls_last=True)
        for alias in dict.fromkeys(groups)
        if alias in outputs and alias not in keys
    ]
    return replace(query, order_by=[*query.order_by, *remaining]), tuple(keys)


def limit_rows(
    rows: list[dict[str, Any]], limit: int, keys: tuple[str, ...]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Discard the probe and count only observed rows sharing the cutoff key."""
    warnings = []
    if limit > 0 and len(rows) > limit:
        # Warehouses can fold unquoted output aliases. Resolve once against
        # the returned schema, preferring an exact key over folded matches.
        resolved_keys = []
        for key in keys:
            if key in rows[0]:
                resolved_keys.append(key)
                continue
            matches = [name for name in rows[0] if name.casefold() == key.casefold()]
            if len(matches) != 1:
                raise KeyError(key)
            resolved_keys.append(matches[0])
        keys = tuple(resolved_keys)
        # Index rather than .get(): an adapter missing a sort column cannot
        # establish a tie by silently comparing absent values as NULL.
        boundary = tuple(rows[limit - 1][key] for key in keys)
        if boundary == tuple(rows[limit][key] for key in keys):
            count = sum(tuple(row[key] for key in keys) == boundary for row in rows[: limit + 1])
            warnings.append(
                {
                    "code": "TIES_AT_LIMIT",
                    "severity": "warning",
                    "message": (
                        f"At least {count} rows tie on the requested order at the limit of "
                        f"{limit}; the group_by columns, then the period, ascending, chose "
                        "which of them are returned."
                    ),
                    "details": {"at": limit, "tied": count},
                }
            )
    return QueryRows(rows[:limit], truncated=bool(getattr(rows, "truncated", False))), warnings
