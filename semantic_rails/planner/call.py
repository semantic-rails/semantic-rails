"""The planner's draft: the query call it hands back, built through one builder.

A draft is a :class:`Call`, the caller's own spelling of a query (``select``, ``group_by``,
``where``, ``having``, ``time``, ``order_by``, ``limit``, ``routes``, ``observation_scope``)
as frozen values. Every planner path produces its draft with :class:`QueryBuilder`, whose
``build()`` checks what the planner can get wrong before the engine reads the call: every id
exists, a relative window carries no dates, an order names a column the call returns, and
every filter is one of the operator words. :meth:`Call.to_call` writes the call the plan
returns and validates; :meth:`Call.from_call` reads a caller's seed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .. import operators
from ..clock import Window, resolve_window
from ..naming import last_token
from ..query_call import (
    EXPRESSION_KEYS,
    REFERENCE_PREFIXES,
    TIME_AXIS,
    ordered_column,
    own_measure,
    refusal,
)

# The window keys of a time block; ``last`` and ``this`` read relative to ``now``.
WINDOW_KEYS = ("last", "this", "start", "end")


@dataclass(frozen=True)
class Select:
    """One select item: a measure or metric id, or an inline kind in the call's spelling."""

    expression: Any
    alias: str

    @property
    def ref(self) -> str:
        """The id this item reads when it is a plain reference (or a measure override)."""
        if isinstance(self.expression, str):
            return self.expression
        if isinstance(self.expression, Mapping) and self.expression.get("kind") == "measure":
            return str(self.expression.get("measure") or "")
        return ""

    @property
    def kind(self) -> str:
        if isinstance(self.expression, str):
            return self.expression.split(".", 1)[0]
        return str(self.expression.get("kind") or "")

    @property
    def aggregation(self) -> str:
        """An explicit aggregation override; "" for the package's own."""
        if isinstance(self.expression, Mapping) and self.expression.get("kind") == "measure":
            return str(self.expression.get("aggregation") or "")
        return ""

    def references(self) -> list[str]:
        return _expression_refs(self.expression)

    def to_call(self) -> Any:
        if isinstance(self.expression, str) and self.alias == last_token(self.expression):
            return self.expression
        return {"expression": _plain(self.expression), "as": self.alias}


@dataclass(frozen=True)
class Condition:
    """A row condition ``{field, op, value}``; in ``having`` the field is an alias or an id."""

    field: str
    op: str
    value: Any

    def to_call(self) -> dict[str, Any]:
        value = list(self.value) if isinstance(self.value, tuple) else self.value
        return {"field": self.field, "op": self.op, "value": value}

    @property
    def values(self) -> list[Any]:
        """The values an ``is``/``not`` condition names."""
        return operators.values_of(self.op, self.value)


@dataclass(frozen=True)
class EntityFilter:
    """An entity's rows tested: some match (``any``), none match, or its aggregates
    (``having``, taken ``over`` all_time, the query window or each period)."""

    entity: str
    match: str
    conditions: tuple[Condition, ...]
    over: str = ""

    def to_call(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "entity": self.entity,
            self.match: [row.to_call() for row in self.conditions],
        }
        if self.over:
            out["over"] = self.over
        return out


@dataclass(frozen=True)
class Time:
    """The time block: clock, grain and one window spelling."""

    temporal_role: str = ""
    grain: str = ""
    start: str = ""
    end: str = ""
    last: tuple[str, int] | None = None
    this: str = ""
    fill: bool = False

    @property
    def window(self) -> dict[str, Any]:
        """The window keys in the call's spelling (empty for all time)."""
        if self.last is not None:
            return {"last": {"unit": self.last[0], "value": self.last[1]}}
        if self.this:
            return {"this": self.this}
        return {key: value for key, value in (("start", self.start), ("end", self.end)) if value}

    @property
    def relative(self) -> bool:
        return self.last is not None or bool(self.this)

    @property
    def named(self) -> bool:
        """Whether the window always answers with its rows (``last``, ``this``, both bounds)."""
        return self.relative or bool(self.start and self.end)

    def with_window(self, window: Mapping[str, Any]) -> Time:
        return replace(self, **_window_fields(window))

    def resolved(self, now: datetime, timezone: str = "UTC") -> Window:
        return resolve_window(self.window, now=now, timezone=timezone)

    def to_call(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.temporal_role:
            out["temporal_role"] = self.temporal_role
        if self.grain:
            out["grain"] = self.grain
        out.update(self.window)
        if self.fill:
            out["fill"] = True
        return out


def _window_fields(window: Mapping[str, Any]) -> dict[str, Any]:
    """``Time`` fields for window keys in the call's spelling; every other window cleared."""
    last = window.get("last")
    return {
        "last": (str(last["unit"]), int(last["value"])) if isinstance(last, Mapping) else None,
        "this": str(window.get("this") or ""),
        "start": str(window.get("start") or ""),
        "end": str(window.get("end") or ""),
    }


def time_of(raw: Mapping[str, Any] | None) -> Time | None:
    """A time block in the call's spelling as a :class:`Time`; an empty block stays one, for
    validation to refuse rather than the draft to drop."""
    if raw is None:
        return None
    return Time(
        temporal_role=str(raw.get("temporal_role") or ""),
        grain=str(raw.get("grain") or ""),
        fill=bool(raw.get("fill")),
        **_window_fields(raw),
    )


@dataclass(frozen=True)
class Order:
    field: str
    direction: str = "asc"

    def to_call(self) -> dict[str, Any]:
        return {"field": self.field, "direction": self.direction}


@dataclass(frozen=True)
class Route:
    source: str
    target: str
    via: tuple[str, ...]

    def to_call(self) -> dict[str, Any]:
        return {"from": self.source, "to": self.target, "via": list(self.via)}


@dataclass(frozen=True)
class Call:
    """A query call as the planner drafts it."""

    select: tuple[Select, ...] = ()
    group_by: tuple[str, ...] = ()
    where: tuple[Condition | EntityFilter, ...] = ()
    having: tuple[Condition, ...] = ()
    time: Time | None = None
    order_by: tuple[Order, ...] = ()
    limit: int | None = None
    routes: tuple[Route, ...] = ()
    observation_scope: str = ""

    @property
    def conditions(self) -> tuple[Condition, ...]:
        """The top-level row conditions (entity filters excluded)."""
        return tuple(item for item in self.where if isinstance(item, Condition))

    @property
    def entity_filters(self) -> tuple[EntityFilter, ...]:
        return tuple(item for item in self.where if isinstance(item, EntityFilter))

    @property
    def aliases(self) -> list[str]:
        return [item.alias for item in self.select]

    @property
    def subjects(self) -> list[str]:
        """The id each select reads directly, in order and once."""
        return list(dict.fromkeys(item.ref for item in self.select if item.ref))

    def select_named(self, alias: str) -> Select | None:
        return next((item for item in self.select if item.alias == alias), None)

    def references(self) -> list[str]:
        """Every id the call names: selects, filters, entities, groupings and the clock."""
        ids: list[str] = []
        for item in self.select:
            ids.extend(item.references())
        for row in self.where:
            if isinstance(row, EntityFilter):
                ids.append(row.entity)
                ids.extend(condition.field for condition in row.conditions)
            else:
                ids.append(row.field)
        ids.extend(row.field for row in self.having if row.field.startswith(REFERENCE_PREFIXES))
        ids.extend(self.group_by)
        if self.time is not None and self.time.temporal_role:
            ids.append(self.time.temporal_role)
        return list(dict.fromkeys(item for item in ids if item))

    def to_call(self) -> dict[str, Any]:
        call: dict[str, Any] = {}
        if self.select:
            call["select"] = [item.to_call() for item in self.select]
        if self.group_by:
            call["group_by"] = list(self.group_by)
        if self.where:
            call["where"] = [item.to_call() for item in self.where]
        if self.having:
            call["having"] = [item.to_call() for item in self.having]
        if self.time is not None:
            call["time"] = self.time.to_call()
        if self.order_by:
            call["order_by"] = [item.to_call() for item in self.order_by]
        if self.limit is not None:
            call["limit"] = self.limit
        if self.routes:
            call["routes"] = [item.to_call() for item in self.routes]
        if self.observation_scope:
            call["observation_scope"] = self.observation_scope
        return call

    @classmethod
    def from_call(cls, payload: Mapping[str, Any] | None, measures: Sequence[Any] = ()) -> Call:
        """A call in the caller's spelling (already read by the front door) as a draft; the
        package's ``measures`` say which select item an order_by id names."""
        raw = dict(payload or {})
        select = []
        for index, item in enumerate(raw.get("select") or []):
            if isinstance(item, str):
                select.append(Select(item.strip(), last_token(item.strip())))
                continue
            expression = item.get("expression")
            expression = expression.strip() if isinstance(expression, str) else expression
            alias = str(item.get("as") or "").strip() or _default_alias(expression, index)
            select.append(Select(expression, alias))
        where: list[Condition | EntityFilter] = []
        for item in raw.get("where") or []:
            if "entity" in item:
                match = next(key for key in ("any", "none", "having") if key in item)
                where.append(
                    EntityFilter(
                        entity=str(item["entity"]).strip(),
                        match=match,
                        conditions=tuple(_condition(row) for row in item[match]),
                        over=str(item.get("over") or ""),
                    )
                )
            else:
                where.append(_condition(item))
        # An order_by field naming a selected id names its column, as the front door reads it.
        columns = [(item.alias, item.expression) for item in select]
        return cls(
            select=tuple(select),
            group_by=tuple(str(item).strip() for item in raw.get("group_by") or []),
            where=tuple(where),
            having=tuple(_condition(row) for row in raw.get("having") or []),
            time=time_of(raw.get("time")),
            order_by=tuple(
                Order(
                    ordered_column(
                        str(row["field"]).strip(), f"order_by[{index}].field", columns, measures
                    ),
                    str(row.get("direction") or "asc").lower(),
                )
                for index, row in enumerate(raw.get("order_by") or [])
            ),
            limit=raw.get("limit"),
            routes=tuple(
                Route(str(row["from"]), str(row["to"]), tuple(row["via"]))
                for row in raw.get("routes") or []
            ),
            observation_scope=str(raw.get("observation_scope") or ""),
        )


def _default_alias(expression: Any, index: int) -> str:
    if isinstance(expression, str):
        return last_token(expression)
    if isinstance(expression, Mapping) and expression.get("kind") == "measure":
        return last_token(str(expression.get("measure") or ""))
    return f"expr_{index + 1}"


def _condition(row: Mapping[str, Any]) -> Condition:
    return condition(str(row.get("field") or "").strip(), row.get("op", "is"), row.get("value"))


def condition(field_id: str, op: str, value: Any) -> Condition:
    """A condition with its op read as a word and its value in canonical form (a list of
    several values is a tuple, so a condition is a value)."""
    word, canonical = operators.normalize_condition(op, value, path="where", internal=True)
    return Condition(field_id, word, tuple(canonical) if isinstance(canonical, list) else canonical)


def _plain(expression: Any) -> Any:
    """An expression with mapping values as plain dicts and lists."""
    if isinstance(expression, Mapping):
        return {key: _plain(value) for key, value in expression.items()}
    if isinstance(expression, (list, tuple)):
        return [_plain(item) for item in expression]
    return expression


def _expression_refs(expression: Any) -> list[str]:
    """The measure and metric ids an expression in the call's spelling reads, in order."""
    if isinstance(expression, str):
        return [expression] if expression.startswith(REFERENCE_PREFIXES) else []
    out: list[str] = []
    if isinstance(expression, Mapping):
        for key, value in expression.items():
            if key in {"measure", "metric"} and isinstance(value, str):
                out.append(value)
            elif (
                key in EXPRESSION_KEYS
                or key in {"over", "value", "else", "input"}
                or isinstance(value, (Mapping, list, tuple))
            ):
                out.extend(_expression_refs(value))
    elif isinstance(expression, (list, tuple)):
        for item in expression:
            out.extend(_expression_refs(item))
    return out


class QueryBuilder:
    """The one way the planner produces a draft.

    ``QueryBuilder(config, base)`` starts from ``base`` (or an empty call); the setters return
    the builder; ``build()`` checks the call and returns it.
    """

    def __init__(self, config: Any, base: Call | None = None) -> None:
        self._config = config
        self._call = base or Call()
        self._fields: dict[str, Any] = {}

    def _get(self, name: str) -> Any:
        return self._fields.get(name, getattr(self._call, name))

    def select(self, expression: Any, alias: str = "") -> QueryBuilder:
        """Add a select item; a measure at its package aggregation is its id."""
        expression = self._canonical(expression)
        item = Select(expression, alias or _default_alias(expression, len(self._get("select"))))
        self._fields["select"] = (*self._get("select"), item)
        return self

    def measure(self, measure_id: str, aggregation: str = "", alias: str = "") -> QueryBuilder:
        expression: Any = (
            {"kind": "measure", "measure": measure_id, "aggregation": aggregation}
            if aggregation
            else measure_id
        )
        return self.select(expression, alias)

    def selects(self, items: Iterable[Select]) -> QueryBuilder:
        self._fields["select"] = tuple(items)
        return self

    def group(self, *dimensions: str) -> QueryBuilder:
        self._fields["group_by"] = tuple(dict.fromkeys([*self._get("group_by"), *dimensions]))
        return self

    def groups(self, dimensions: Iterable[str]) -> QueryBuilder:
        self._fields["group_by"] = tuple(dict.fromkeys(dimensions))
        return self

    def where(self, field_id: str, op: str, value: Any) -> QueryBuilder:
        return self.filter(condition(field_id, op, value))

    def filter(self, item: Condition | EntityFilter) -> QueryBuilder:
        if item not in self._get("where"):
            self._fields["where"] = (*self._get("where"), item)
        return self

    def filters(self, items: Iterable[Condition | EntityFilter]) -> QueryBuilder:
        self._fields["where"] = tuple(dict.fromkeys(items))
        return self

    def having(self, items: Iterable[Condition]) -> QueryBuilder:
        self._fields["having"] = tuple(items)
        return self

    def time(self, time: Time | None) -> QueryBuilder:
        self._fields["time"] = time
        return self

    def order(self, field_id: str, direction: str = "asc") -> QueryBuilder:
        item = Order(field_id, direction.lower())
        if item not in self._get("order_by"):
            self._fields["order_by"] = (*self._get("order_by"), item)
        return self

    def orders(self, items: Iterable[Order]) -> QueryBuilder:
        self._fields["order_by"] = tuple(dict.fromkeys(items))
        return self

    def limit(self, count: int | None) -> QueryBuilder:
        self._fields["limit"] = count
        return self

    def routes(self, items: Iterable[Route]) -> QueryBuilder:
        self._fields["routes"] = tuple(items)
        return self

    def observation_scope(self, scope: str) -> QueryBuilder:
        self._fields["observation_scope"] = scope
        return self

    def _canonical(self, expression: Any) -> Any:
        """A measure override equal to its package aggregation is the measure's id."""
        return own_measure(expression, self._config.measures) or expression

    def build(self) -> Call:
        call = replace(self._call, **self._fields)
        _check(self._config, call)
        return call


_WORDS = f"Use a filter word: {', '.join(operators.WORDS)}."


def _check(config: Any, call: Call) -> None:
    """What ``build`` refuses: unknown ids, dated relative windows, orders on columns the call
    doesn't return, and filter ops that aren't words."""
    known = {
        row.id
        for rows in (
            config.measures,
            config.metric_recipes,
            config.dimensions,
            config.entities,
            config.temporal_roles,
            getattr(config, "relationships", []),
        )
        for row in rows
    }
    for index, ref in enumerate(call.references()):
        if ref not in known:
            raise refusal(
                f"references[{index}]",
                f"unknown id '{ref}'",
                fix="Name an id discover lists.",
                code="OBJECT_NOT_FOUND",
            )
    aliases = call.aliases
    if len(set(aliases)) != len(aliases):
        raise refusal(
            "select",
            "every select item has its own alias",
            fix="Give each select item its own 'as'.",
            code="DUPLICATE_OUTPUT_ALIAS",
        )
    for index, row in enumerate(call.where):
        for item in row.conditions if isinstance(row, EntityFilter) else (row,):
            if item.op not in operators.WORDS:
                raise refusal(f"where[{index}].op", f"'{item.op}' is not a filter word", fix=_WORDS)
    for index, row in enumerate(call.having):
        if row.op not in operators.WORDS:
            raise refusal(f"having[{index}].op", f"'{row.op}' is not a filter word", fix=_WORDS)
        if row.field not in aliases and not row.field.startswith(REFERENCE_PREFIXES):
            raise refusal(
                f"having[{index}].field",
                "names a select alias, measure or metric",
                fix="Name a select item's 'as', or a measure or metric id.",
            )
    time = call.time
    if time is not None and time.relative and (time.start or time.end):
        raise refusal("time", "a relative window carries no dates", fix="Drop 'start' and 'end'.")
    if time is not None and time.last is not None and time.this:
        raise refusal("time", "one window spelling: last or this", fix="Keep 'last' or 'this'.")
    columns = {*aliases, *call.group_by, *([TIME_AXIS] if time is not None and time.grain else [])}
    for index, order in enumerate(call.order_by):
        if order.field not in columns:
            raise refusal(
                f"order_by[{index}].field",
                "names a select alias, a group_by id or period",
                fix="Order by a select item's 'as', a group_by id, or period with a grain.",
                code="INVALID_ORDER_BY",
            )
        if order.direction not in {"asc", "desc"}:
            raise refusal(
                f"order_by[{index}].direction",
                "is asc or desc",
                fix="Use 'asc' or 'desc'.",
                code="INVALID_ORDER_BY",
            )


__all__ = [
    "Call",
    "Condition",
    "EntityFilter",
    "Order",
    "QueryBuilder",
    "Route",
    "Select",
    "Time",
    "WINDOW_KEYS",
    "condition",
    "time_of",
]
