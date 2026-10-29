"""Experimental relation-pipeline surface (planner-only).

EXPERIMENTAL: This module exposes a small declarative pipeline DSL —
join/filter/aggregate step lists — that the compiler can fold into a
governed query plan. It is planner-only, not exposed via HTTP/MCP, and
flagged for possible removal before V2. See ``docs/CAPABILITIES.md`` for the
scope-boundary note. Production users
should compose Query IR directly rather than authoring pipelines.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from .dialects import dialect_for_warehouse
from .errors import SemanticLayerError
from .expressions import (
    ArithmeticExpr,
    BooleanExpr,
    CallExpr,
    CaseExpr,
    ColumnRefExpr,
    ComparisonExpr,
    DateAddExpr,
    InExpr,
    LiteralExpr,
    SemanticExpr,
    filter_word,
    parse_config_expression,
    parse_semantic_expression,
    reads_temporal_value,
    validate_boolean_argument_count,
)
from .naming import slug as _slug
from .schema import PackageConfig, RelationConfig, RelationPipelineStep
from .sql_ast import (
    SqlBinary,
    SqlCall,
    SqlCase,
    SqlCaseWhen,
    SqlCast,
    SqlCte,
    SqlExists,
    SqlExpr,
    SqlField,
    SqlIdentifier,
    SqlIn,
    SqlIsNull,
    SqlJoin,
    SqlJsonExtract,
    SqlLiteral,
    SqlOrderTerm,
    SqlQuery,
    SqlSelect,
    SqlSetQuery,
    SqlTableFunction,
    SqlTableRef,
    SqlWindow,
    build_comparison_condition,
    build_filter_condition,
    build_negation,
    is_null_literal,
)

# The date and timestamp columns a filter reads, by table alias, and the alias an unqualified
# column reads: text compared with one is a temporal literal (``SqlLiteral.temporal``).
_TEMPORAL_COLUMNS: ContextVar[tuple[Mapping[str, frozenset[str]], str]] = ContextVar(
    "relation_temporal_columns", default=({}, "src")
)


@dataclass
class _RelationState:
    source_name: str = ""
    columns: list[str] = field(default_factory=list)
    ctes: list[SqlCte] = field(default_factory=list)
    preaggregated: bool = False
    grain_columns: set[str] = field(default_factory=set)
    # Each column's lineage: the source column it carries unchanged through renames and
    # pass-throughs, or the step that computed it. A filter reads a column as a date or
    # timestamp when what the relation's readers or its source declare shares its lineage.
    lineage: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _LoweredRelation:
    relation: RelationConfig
    ctes: list[SqlCte]
    cte_names: set[str]
    dependencies: set[str] = field(default_factory=set)


def _step_name(relation: RelationConfig, index: int, kind: str, *, final: bool) -> str:
    if final:
        return relation.output_name
    return f"{relation.output_name}__{index + 1}_{_slug(kind, fallback='relation')}"


def _ensure_mapping(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SemanticLayerError("INVALID_CONFIG", f"{path} must be a mapping")
    return dict(value)


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _parse_expr(raw: Any) -> SemanticExpr:
    if isinstance(raw, dict):
        return parse_semantic_expression(raw, context="relation")
    return parse_config_expression(raw)


def _semantic_expr_to_sql(
    expr: SemanticExpr, *, default_alias: str = "src", warehouse: str = "duckdb"
) -> SqlExpr:
    if isinstance(expr, ColumnRefExpr):
        qualifier = expr.table or default_alias
        return (
            SqlIdentifier(parts=[qualifier, expr.column])
            if qualifier
            else SqlIdentifier(parts=[expr.column])
        )
    if isinstance(expr, LiteralExpr):
        return SqlLiteral(expr.value)
    if isinstance(expr, ArithmeticExpr):
        op = {"add": "+", "subtract": "-", "multiply": "*", "divide": "/"}.get(expr.op, expr.op)
        return SqlBinary(
            _semantic_expr_to_sql(expr.left, default_alias=default_alias, warehouse=warehouse),
            op,
            _semantic_expr_to_sql(expr.right, default_alias=default_alias, warehouse=warehouse),
        )
    if isinstance(expr, ComparisonExpr) and (word := filter_word(expr)):
        # A filter word on a column reads as a where filter on it (a list as membership).
        assert isinstance(expr.right, LiteralExpr)
        column = _semantic_expr_to_sql(expr.left, default_alias=default_alias, warehouse=warehouse)
        temporal = _temporal(expr.left)
        return build_filter_condition(column, word, expr.right.value, temporal=temporal)
    if isinstance(expr, ComparisonExpr):
        return build_comparison_condition(
            _operand_sql(expr.left, _temporal(expr.right), default_alias, warehouse),
            expr.op,
            _operand_sql(expr.right, _temporal(expr.left), default_alias, warehouse),
        )
    if isinstance(expr, InExpr):
        temporal = any(_temporal(item) for item in (expr.expr, *expr.values))
        return SqlIn(
            expr=_operand_sql(expr.expr, temporal, default_alias, warehouse),
            values=[
                _operand_sql(value, temporal, default_alias, warehouse) for value in expr.values
            ],
            negated=expr.negated,
        )
    if isinstance(expr, BooleanExpr):
        validate_boolean_argument_count(expr.op, len(expr.args))
        args = [
            _semantic_expr_to_sql(arg, default_alias=default_alias, warehouse=warehouse)
            for arg in expr.args
        ]
        if not args:
            return SqlLiteral(True)
        if expr.op.lower() == "not":
            if len(args) != 1:
                raise SemanticLayerError(
                    "INVALID_EXPRESSION_AST",
                    f"Boolean 'not' expressions require exactly one arg, got {len(args)}",
                )
            return build_negation(args[0])
        current = args[0]
        for arg in args[1:]:
            current = SqlBinary(current, expr.op.upper(), arg)
        return current
    if isinstance(expr, CallExpr):
        return dialect_for_warehouse(warehouse).scalar_call(
            expr.name,
            [
                _semantic_expr_to_sql(arg, default_alias=default_alias, warehouse=warehouse)
                for arg in expr.args
            ],
            distinct=expr.distinct,
        )
    if isinstance(expr, DateAddExpr):
        return dialect_for_warehouse(warehouse).date_add(
            expr.unit,
            _semantic_expr_to_sql(expr.value, default_alias=default_alias, warehouse=warehouse),
            _semantic_expr_to_sql(expr.date, default_alias=default_alias, warehouse=warehouse),
        )
    if isinstance(expr, CaseExpr):
        return SqlCase(
            whens=[
                SqlCaseWhen(
                    condition=_semantic_expr_to_sql(
                        item.when, default_alias=default_alias, warehouse=warehouse
                    ),
                    result=_semantic_expr_to_sql(
                        item.then, default_alias=default_alias, warehouse=warehouse
                    ),
                )
                for item in expr.whens
            ],
            else_expr=_semantic_expr_to_sql(
                expr.else_expr, default_alias=default_alias, warehouse=warehouse
            )
            if expr.else_expr is not None
            else None,
        )
    raise SemanticLayerError(
        "INVALID_CONFIG",
        f"Relation expressions support columns, literals, arithmetic, comparisons, booleans, calls, and case expressions; got {type(expr).__name__}",
    )


def _temporal(expr: SemanticExpr) -> bool:
    """Whether ``expr`` reads a date or timestamp of a table the filter reads, by the column's
    qualifier (an unqualified column reads the filter's own table)."""
    tables, default_alias = _TEMPORAL_COLUMNS.get()
    return bool(tables) and reads_temporal_value(
        expr, lambda ref: ref.column in tables.get(ref.table or default_alias, frozenset())
    )


def _operand_sql(expr: SemanticExpr, temporal: bool, default_alias: str, warehouse: str) -> SqlExpr:
    """One side of a comparison or an operand of ``in``: text compared with a date or timestamp
    (``temporal``) is a temporal literal; text compared with anything else stays text."""
    if temporal and isinstance(expr, LiteralExpr) and isinstance(expr.value, str):
        return SqlLiteral(expr.value, temporal=True)
    return _semantic_expr_to_sql(expr, default_alias=default_alias, warehouse=warehouse)


def _expr(raw: Any, *, default_alias: str = "src", warehouse: str = "duckdb") -> SqlExpr:
    return _semantic_expr_to_sql(_parse_expr(raw), default_alias=default_alias, warehouse=warehouse)


def _column_fields(columns: Iterable[str], *, alias: str = "src") -> list[SqlField]:
    return [SqlField(SqlIdentifier(parts=[alias, str(column)]), str(column)) for column in columns]


def _predicate(
    raw: Any,
    *,
    default_alias: str = "src",
    warehouse: str = "duckdb",
    temporal: Mapping[str, frozenset[str]] | None = None,
) -> SqlExpr:
    """A filter step's condition; ``temporal`` names the date and timestamp columns of each
    table alias it reads."""
    temporal = temporal or {}
    if isinstance(raw, dict) and "field" in raw:
        field_name = str(raw["field"])
        left = SqlIdentifier(parts=[default_alias, field_name])
        # The query's filter words: `not` (and its NOT IN / != spellings) keeps unknown rows.
        return build_filter_condition(
            left,
            raw.get("op", "is"),
            raw.get("value"),
            path="relation.filter",
            temporal=field_name in temporal.get(default_alias, frozenset()),
        )
    token = _TEMPORAL_COLUMNS.set((temporal, default_alias))
    try:
        return _expr(raw, default_alias=default_alias, warehouse=warehouse)
    finally:
        _TEMPORAL_COLUMNS.reset(token)


def _temporal_tables(config: PackageConfig) -> dict[str, frozenset[str]]:
    """Each table or relation an entity reads, with the columns its date and timestamp
    dimensions declare."""
    entities = {entity.id: entity for entity in config.entities}
    tables: dict[str, set[str]] = {}
    for dim in config.dimensions:
        entity = entities.get(dim.entity)
        if entity is None or dim.data_type not in {"date", "timestamp"}:
            continue
        for name in filter(None, (entity.table, entity.relation_id)):
            tables.setdefault(name, set()).add(dim.column)
    return {name: frozenset(columns) for name, columns in tables.items()}


# Functions whose result has their argument's type: MIN(day) is a date when day is.
_TYPE_PRESERVING = frozenset(
    {"MIN", "MAX", "SINGLE_VALUE", "LAG", "LEAD", "FIRST_VALUE", "LAST_VALUE"}
)


# A table alias's lookup from a column it reads to that column's lineage (None if unknown).
_Lineages = Mapping[str, Callable[[str], str | None]]


def _bare_column(raw: Any, tables: _Lineages, default: str) -> str | None:
    """The lineage of the column ``raw`` reads unchanged, by its table (``default`` when
    unqualified), or None for a computed value or an unknown column."""
    expr = _parse_expr(raw)
    lookup = tables.get(expr.table or default) if isinstance(expr, ColumnRefExpr) else None
    return lookup(expr.column) if lookup and isinstance(expr, ColumnRefExpr) else None


def _preserved(spec: Any, tables: _Lineages) -> str | None:
    """The lineage of an aggregate or window that keeps its argument's type
    (:data:`_TYPE_PRESERVING`) over a column read unchanged, or None."""
    if not isinstance(spec, dict):
        return None
    function = str(spec.get("function", "") or "").upper()
    expr_raw = spec.get("expr")
    if function not in _TYPE_PRESERVING or expr_raw is None or spec.get("distinct"):
        return None
    return _bare_column(expr_raw, tables, "src")


def _lineage(
    step: RelationPipelineStep, before: _RelationState, after: _RelationState, step_id: str
) -> dict[str, str]:
    """Each output column's lineage (:attr:`_RelationState.lineage`), only where it is certain:
    a filter keeps its input columns; a projection carries a column it reads unchanged (or
    through a type-preserving aggregate or window) under its new name, resolved by its table;
    a join's right-hand column is that table's. Anything else starts its own lineage, so a
    filter on it is never typed by another column's declaration."""
    config = dict(step.config or {})
    src: _Lineages = {"src": before.lineage.get}
    carried: dict[str, str | None] = {}
    if step.kind == "source":
        source = _source_from_config(config)
        carried = {column: f"{source}.{column}" for column in after.columns}
    elif step.kind in {"where", "semi_join", "anti_join", "exclude"}:
        carried = dict(before.lineage)
    elif step.kind == "select":
        columns = config.get("columns", config.get("value", config))
        carried = {str(alias): _bare_column(raw, src, "src") for alias, raw in columns.items()}
    elif step.kind == "group_by":
        dimensions = config.get("dimensions", {}) or {}
        aggregates = config.get("aggregates", {}) or {}
        carried = {str(alias): _bare_column(raw, src, "src") for alias, raw in dimensions.items()}
        carried |= {str(alias): _preserved(spec, src) for alias, spec in aggregates.items()}
    elif step.kind == "window":
        windows = config.get("windows", config)
        carried = dict(before.lineage)
        carried |= {str(alias): _preserved(spec, src) for alias, spec in windows.items()}
    elif step.kind == "join":
        pre_aggregate = dict(config.get("pre_aggregate", {}) or {})
        table = _source_from_config(config)
        sides: _Lineages = {
            **({} if pre_aggregate.get("left") else {"left": before.lineage.get}),
            **({} if pre_aggregate.get("right") else {"right": lambda column: f"{table}.{column}"}),
        }
        projection = config.get("select") or {
            column: {"kind": "column", "table": "left", "column": column}
            for column in before.columns
        }
        carried = {
            str(alias): _bare_column(raw, sides, "left") for alias, raw in projection.items()
        }
    # Known limit: a union, an explode, a JSON extract or a state_as_of starts its columns' own
    # lineage, so a filter on them is typed only by the relation's readers' declarations.
    # A name two of the stage's columns share is ambiguous: it gets no lineage, so no type.
    ambiguous = {column for column in after.columns if after.columns.count(column) > 1}
    return {
        column: carried.get(column) or f"{step_id}.{column}"
        for column in after.columns
        if column not in ambiguous
    }


def _temporal_lineage(
    relation: RelationConfig, final: _RelationState, tables: Mapping[str, frozenset[str]]
) -> frozenset[str]:
    """The lineages a relation's dates and timestamps carry: the columns its readers declare,
    traced back from its output, and those the tables it reads (its source, a joined table)
    declare. A relation another relation reads exports only its readers' declarations: types
    inferred through it reach no dependent semi-join (left untyped, never mistyped)."""
    declared = tables.get(relation.output_name, frozenset()) | tables.get(relation.id, frozenset())
    lineages = {final.lineage[column] for column in declared if column in final.lineage}
    for step in relation.steps:
        config = dict(step.config or {})
        if step.kind in {"source", "join"}:
            table = _source_from_config(config)
            lineages |= {f"{table}.{column}" for column in tables.get(table, frozenset())}
    return frozenset(lineages)


def _stage_temporal(state: _RelationState, lineages: frozenset[str]) -> frozenset[str]:
    """The columns of ``state`` whose lineage is a date or timestamp."""
    return frozenset(column for column in state.columns if state.lineage.get(column) in lineages)


def _source_from_config(config: dict[str, Any]) -> str:
    value = config.get("relation", config.get("value", ""))
    text = str(value or "").strip()
    if not text:
        raise SemanticLayerError("INVALID_CONFIG", "source relation step requires relation")
    return text


def _date_rows(start: str, end: str, *, max_days: int = 3660) -> list[date]:
    try:
        current = date.fromisoformat(str(start))
        stop = date.fromisoformat(str(end))
    except ValueError as exc:
        raise SemanticLayerError(
            "INVALID_CONFIG", "date_spine start/end must be ISO dates"
        ) from exc
    if stop < current:
        raise SemanticLayerError("INVALID_CONFIG", "date_spine end must be on or after start")
    rows: list[date] = []
    while current <= stop:
        rows.append(current)
        if len(rows) > max_days:
            raise SemanticLayerError("INVALID_CONFIG", f"date_spine exceeds max_days={max_days}")
        current += timedelta(days=1)
    return rows


def _date_spine_query(config: dict[str, Any]) -> tuple[SqlSetQuery | SqlSelect, list[str]]:
    column = str(config.get("column", "date_day") or "date_day")
    start = str(config.get("start", "") or "")
    end = str(config.get("end", "") or "")
    if not start or not end:
        raise SemanticLayerError("INVALID_CONFIG", "date_spine requires bounded start and end")
    queries = [
        SqlSelect(select=[SqlField(SqlCast(SqlLiteral(row.isoformat()), "DATE"), column)])
        for row in _date_rows(start, end, max_days=int(config.get("max_days", 3660)))
    ]
    if len(queries) == 1:
        return queries[0], [column]
    return SqlSetQuery(operator="UNION ALL", queries=queries), [column]


def _lower_select(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    columns = _ensure_mapping(
        config.get("columns", config.get("value", config)), path="relation select columns"
    )
    select_fields = [
        SqlField(_expr(expr_raw, warehouse=warehouse), str(alias))
        for alias, expr_raw in columns.items()
    ]
    query = SqlSelect(select=select_fields, from_table=SqlTableRef(state.source_name, alias="src"))
    output_columns = [str(alias) for alias in columns]
    grain_columns = set(state.grain_columns) & set(output_columns)
    return _RelationState(
        source_name=output_name,
        columns=output_columns,
        ctes=[*state.ctes, SqlCte(output_name, query)],
        preaggregated=state.preaggregated and state.grain_columns <= set(output_columns),
        grain_columns=grain_columns,
    )


def _lower_where(
    config: dict[str, Any],
    state: _RelationState,
    *,
    output_name: str,
    warehouse: str,
    temporal: frozenset[str] = frozenset(),
) -> _RelationState:
    if not state.columns:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "where relation step requires known columns from a prior select/source columns declaration",
        )
    raw_predicates = config.get("predicates", config.get("value", []))
    predicates = [
        _predicate(item, warehouse=warehouse, temporal={"src": temporal})
        for item in _ensure_list(raw_predicates)
    ]
    query = SqlSelect(
        select=_column_fields(state.columns),
        from_table=SqlTableRef(state.source_name, alias="src"),
        where=predicates,
    )
    return _RelationState(
        source_name=output_name,
        columns=list(state.columns),
        ctes=[*state.ctes, SqlCte(output_name, query)],
        preaggregated=state.preaggregated,
        grain_columns=set(state.grain_columns),
    )


def _lower_json_extract(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    if not state.columns:
        raise SemanticLayerError(
            "INVALID_CONFIG", "json_extract relation step requires known columns"
        )
    source_column = str(config.get("column", "") or "")
    alias = str(config.get("as", source_column) or "")
    if not source_column or not alias:
        raise SemanticLayerError("INVALID_CONFIG", "json_extract requires column and as")
    path = [str(item) for item in _ensure_list(config.get("path"))]
    if not path:
        raise SemanticLayerError("INVALID_CONFIG", "json_extract requires path")
    fields = _column_fields(state.columns)
    json_expr = dialect_for_warehouse(warehouse).json_extract(
        SqlIdentifier(parts=["src", source_column]), path, as_text=bool(config.get("as_text", True))
    )
    fields.append(SqlField(json_expr, alias))
    query = SqlSelect(select=fields, from_table=SqlTableRef(state.source_name, alias="src"))
    return _RelationState(
        source_name=output_name,
        columns=[*state.columns, alias],
        ctes=[*state.ctes, SqlCte(output_name, query)],
    )


def _lower_explode(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    if not state.columns:
        raise SemanticLayerError("INVALID_CONFIG", "explode relation step requires known columns")
    source_column = str(config.get("column", "") or "")
    alias = str(config.get("as", "value") or "value")
    delimiter = str(config.get("delimiter", "") or "")
    if not source_column:
        raise SemanticLayerError("INVALID_CONFIG", "explode requires column")
    fields = _column_fields([column for column in state.columns if column != source_column])
    table, element = dialect_for_warehouse(warehouse).explode(
        SqlIdentifier(parts=["src", source_column]), delimiter, alias="exploded", column=alias
    )
    fields.append(SqlField(element, alias))
    query = SqlSelect(
        select=fields,
        from_table=SqlTableRef(state.source_name, alias="src"),
        joins=[SqlJoin("CROSS", table=table)],
    )
    output_columns = [column for column in state.columns if column != source_column]
    output_columns.append(alias)
    return _RelationState(
        source_name=output_name,
        columns=output_columns,
        ctes=[*state.ctes, SqlCte(output_name, query)],
    )


def _aggregate_expr(spec: Any, *, warehouse: str) -> SqlExpr:
    if isinstance(spec, str):
        return SqlCall(spec, [SqlLiteral(1)])
    config = _ensure_mapping(spec, path="group_by aggregate")
    function = str(config.get("function", "") or "").upper()
    if not function:
        raise SemanticLayerError("INVALID_CONFIG", "group_by aggregate requires function")
    expr_raw = config.get("expr", "*")
    if function == "SINGLE_VALUE":  # the group's one value, NULL where it holds two
        return dialect_for_warehouse(warehouse).single_value(
            _expr(expr_raw, warehouse=warehouse), boolean=bool(config.get("boolean"))
        )
    if expr_raw == "*":
        return SqlCall(function, [SqlLiteral(1)], distinct=bool(config.get("distinct", False)))
    return SqlCall(
        function,
        [_expr(expr_raw, warehouse=warehouse)],
        distinct=bool(config.get("distinct", False)),
    )


def _lower_group_by(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    dimensions = _ensure_mapping(config.get("dimensions", {}), path="relation group_by dimensions")
    aggregates = _ensure_mapping(config.get("aggregates", {}), path="relation group_by aggregates")
    fields = [
        SqlField(_expr(expr_raw, warehouse=warehouse), str(alias))
        for alias, expr_raw in dimensions.items()
    ]
    group_exprs = [field.expression for field in fields]
    fields.extend(
        SqlField(_aggregate_expr(spec, warehouse=warehouse), str(alias))
        for alias, spec in aggregates.items()
    )
    query = SqlSelect(
        select=fields, from_table=SqlTableRef(state.source_name, alias="src"), group_by=group_exprs
    )
    columns = [str(alias) for alias in dimensions] + [str(alias) for alias in aggregates]
    return _RelationState(
        source_name=output_name,
        columns=columns,
        ctes=[*state.ctes, SqlCte(output_name, query)],
        preaggregated=True,
        grain_columns={str(alias) for alias in dimensions},
    )


def _join_condition(
    item: Any, *, warehouse: str, left_alias: str = "left", right_alias: str = "right"
) -> SqlExpr:
    config = _ensure_mapping(item, path="relation join on")
    left = _expr(config.get("left"), default_alias=left_alias, warehouse=warehouse)
    right = _expr(config.get("right"), default_alias=right_alias, warehouse=warehouse)
    op = str(config.get("op", "=") or "=")
    lower = str(config.get("transform", "") or "").lower() == "lower"
    # A null literal stays bare under the transform, so the comparison still sees it.
    sides = [
        SqlCall("LOWER", [s]) if lower and not is_null_literal(s) else s for s in (left, right)
    ]
    condition: SqlExpr = build_comparison_condition(sides[0], op, sides[1], path="relation.join")
    lag = config.get("date_lag")
    if isinstance(lag, dict):
        unit = str(lag.get("unit", "day") or "day")
        max_value = int(lag.get("max", 0) or 0)
        if max_value:
            lag_expr = dialect_for_warehouse(str(lag.get("warehouse", warehouse))).date_diff(
                unit, left, right
            )
            condition = SqlBinary(
                condition, "AND", SqlBinary(lag_expr, "<=", SqlLiteral(max_value))
            )
    return condition


def _column_name_from_join_side(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.split(".")[-1]
    if isinstance(raw, dict) and str(raw.get("kind", "") or "").strip() == "column":
        return str(raw.get("column", "") or "").strip()
    return ""


def _join_key_columns(on_items: list[Any], side: str) -> set[str]:
    columns: set[str] = set()
    for item in on_items:
        if not isinstance(item, dict):
            continue
        column = _column_name_from_join_side(item.get(side))
        if column:
            columns.add(column)
    return columns


def _validate_preaggregate_keys(
    side: str, on_items: list[Any], columns: list[str], *, path: str
) -> None:
    required = _join_key_columns(on_items, side)
    missing = sorted(required - set(columns))
    if missing:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            f"{path} must preserve join key columns: {', '.join(missing)}",
            details={"side": side, "missing_join_keys": missing},
        )


def _lower_preaggregate_side(
    source_name: str,
    spec_raw: Any,
    *,
    output_name: str,
    warehouse: str,
    on_items: list[Any],
    side: str,
) -> tuple[SqlCte, list[str], set[str]]:
    spec = _ensure_mapping(spec_raw, path=f"relation join pre_aggregate.{side}")
    state = _RelationState(
        source_name=source_name,
        columns=[str(item) for item in _ensure_list(spec.get("columns", []))],
    )
    lowered = _lower_group_by(spec, state, output_name=output_name, warehouse=warehouse)
    if len(lowered.ctes) != 1:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"relation join pre_aggregate.{side} must produce one grouped CTE"
        )
    _validate_preaggregate_keys(
        side, on_items, sorted(lowered.grain_columns), path=f"relation join pre_aggregate.{side}"
    )
    return lowered.ctes[0], lowered.columns, set(lowered.grain_columns)


def _lower_join(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    relation = str(config.get("relation", "") or "").strip()
    if not relation:
        raise SemanticLayerError("INVALID_CONFIG", "join relation step requires relation")
    on_items = _ensure_list(config.get("on", []))
    if not on_items:
        raise SemanticLayerError("INVALID_CONFIG", "join relation step requires on")
    pre_aggregate = dict(config.get("pre_aggregate", {}) or {})
    join_ctes = list(state.ctes)
    left_source = state.source_name
    right_source = relation
    left_columns = list(state.columns)
    right_columns: list[str] = []
    if pre_aggregate.get("left"):
        left_cte, left_columns, _left_grain_columns = _lower_preaggregate_side(
            state.source_name,
            pre_aggregate["left"],
            output_name=f"{output_name}__left_preaggregate",
            warehouse=warehouse,
            on_items=on_items,
            side="left",
        )
        join_ctes.append(left_cte)
        left_source = left_cte.name
    if pre_aggregate.get("right"):
        right_cte, right_columns, _right_grain_columns = _lower_preaggregate_side(
            relation,
            pre_aggregate["right"],
            output_name=f"{output_name}__right_preaggregate",
            warehouse=warehouse,
            on_items=on_items,
            side="right",
        )
        join_ctes.append(right_cte)
        right_source = right_cte.name

    if bool(config.get("require_pre_aggregate", False)):
        has_boundary = (
            state.preaggregated
            or bool(pre_aggregate.get("left"))
            or bool(pre_aggregate.get("right"))
        )
        if not has_boundary:
            raise SemanticLayerError(
                "INVALID_CONFIG", "join requires a pre-aggregation boundary before execution"
            )
    if state.preaggregated:
        _validate_preaggregate_keys(
            "left", on_items, sorted(state.grain_columns), path="relation join prior group_by"
        )

    condition = _join_condition(on_items[0], warehouse=warehouse)
    for item in on_items[1:]:
        condition = SqlBinary(condition, "AND", _join_condition(item, warehouse=warehouse))
    select_config = _ensure_mapping(config.get("select", {}), path="relation join select")
    if not select_config:
        select_config = {
            column: {"kind": "column", "table": "left", "column": column} for column in left_columns
        }
    fields = [
        SqlField(_expr(expr_raw, default_alias="left", warehouse=warehouse), str(alias))
        for alias, expr_raw in select_config.items()
    ]
    query = SqlSelect(
        select=fields,
        from_table=SqlTableRef(left_source, alias="left"),
        joins=[
            SqlJoin(
                str(config.get("type", "inner")).upper(),
                SqlTableRef(right_source, alias="right"),
                condition,
            )
        ],
    )
    return _RelationState(
        source_name=output_name,
        columns=[str(alias) for alias in select_config],
        ctes=[*join_ctes, SqlCte(output_name, query)],
    )


def _lower_semi_join(
    config: dict[str, Any],
    state: _RelationState,
    *,
    output_name: str,
    warehouse: str,
    negated: bool,
    temporal: Mapping[str, frozenset[str]] | None = None,
) -> _RelationState:
    relation = str(config.get("relation", "") or "").strip()
    if not relation:
        raise SemanticLayerError("INVALID_CONFIG", "semi_join relation step requires relation")
    on_items = _ensure_list(config.get("on", []))
    if not on_items:
        raise SemanticLayerError("INVALID_CONFIG", "semi_join relation step requires on")
    condition = _join_condition(
        on_items[0], warehouse=warehouse, left_alias="src", right_alias="right"
    )
    for item in on_items[1:]:
        condition = SqlBinary(
            condition,
            "AND",
            _join_condition(item, warehouse=warehouse, left_alias="src", right_alias="right"),
        )
    right_predicates = [
        _predicate(item, default_alias="right", warehouse=warehouse, temporal=temporal)
        for item in _ensure_list(config.get("where", []))
    ]
    subquery_where = [condition, *right_predicates]
    subquery = SqlSelect(
        select=[SqlField(SqlLiteral(1), "match")],
        from_table=SqlTableRef(relation, alias="right"),
        where=subquery_where,
    )
    query = SqlSelect(
        select=_column_fields(state.columns),
        from_table=SqlTableRef(state.source_name, alias="src"),
        where=[SqlExists(subquery, negated=negated)],
    )
    return _RelationState(
        source_name=output_name,
        columns=list(state.columns),
        ctes=[*state.ctes, SqlCte(output_name, query)],
        preaggregated=state.preaggregated,
        grain_columns=set(state.grain_columns),
    )


def _lower_window(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    windows = _ensure_mapping(config.get("windows", config), path="relation window columns")
    fields = _column_fields(state.columns)
    for alias, spec_raw in windows.items():
        spec = _ensure_mapping(spec_raw, path=f"relation window {alias}")
        function = str(spec.get("function", "") or "").upper()
        if not function:
            raise SemanticLayerError("INVALID_CONFIG", "window step requires function")
        args = (
            [] if function == "ROW_NUMBER" else [_expr(spec.get("expr", "1"), warehouse=warehouse)]
        )
        order_by = [
            SqlOrderTerm(
                _expr(item.get("expr", item), warehouse=warehouse),
                str(item.get("direction", "ASC")) if isinstance(item, dict) else "ASC",
            )
            for item in _ensure_list(spec.get("order_by", []))
        ]
        partition_by = [
            _expr(item, warehouse=warehouse) for item in _ensure_list(spec.get("partition_by", []))
        ]
        fields.append(
            SqlField(
                SqlWindow(
                    SqlCall(function, args),
                    partition_by=partition_by,
                    order_by=order_by,
                    frame=str(spec.get("frame", "") or ""),
                ),
                str(alias),
            )
        )
    query = SqlSelect(select=fields, from_table=SqlTableRef(state.source_name, alias="src"))
    return _RelationState(
        source_name=output_name,
        columns=[*state.columns, *[str(alias) for alias in windows]],
        ctes=[*state.ctes, SqlCte(output_name, query)],
    )


def _lower_state_as_of(
    config: dict[str, Any], state: _RelationState, *, output_name: str, warehouse: str
) -> _RelationState:
    spine = str(config.get("spine", "") or "").strip()
    date_column = str(config.get("date_column", "date_day") or "date_day")
    valid_from = str(config.get("valid_from", "") or "")
    valid_to = str(config.get("valid_to", "") or "")
    if not spine or not valid_from:
        raise SemanticLayerError("INVALID_CONFIG", "state_as_of requires spine and valid_from")
    conditions: list[SqlExpr] = [
        SqlBinary(
            SqlIdentifier(parts=["spine", date_column]),
            ">=",
            SqlIdentifier(parts=["src", valid_from]),
        )
    ]
    if valid_to:
        conditions.append(
            SqlBinary(
                SqlBinary(
                    SqlIdentifier(parts=["spine", date_column]),
                    "<",
                    SqlIdentifier(parts=["src", valid_to]),
                ),
                "OR",
                SqlIsNull(SqlIdentifier(parts=["src", valid_to])),
            )
        )
    for key in _ensure_list(config.get("keys", [])):
        conditions.append(
            SqlBinary(
                SqlIdentifier(parts=["spine", str(key)]),
                "=",
                SqlIdentifier(parts=["src", str(key)]),
            )
        )
    condition = conditions[0]
    for item in conditions[1:]:
        condition = SqlBinary(condition, "AND", item)
    select_config = dict(config.get("select", {}) or {})
    if not select_config:
        select_config = {
            column: {"kind": "column", "table": "src", "column": column} for column in state.columns
        }
        select_config[date_column] = {"kind": "column", "table": "spine", "column": date_column}
    fields = [
        SqlField(_expr(expr_raw, warehouse=warehouse), str(alias))
        for alias, expr_raw in select_config.items()
    ]
    query = SqlSelect(
        select=fields,
        from_table=SqlTableRef(state.source_name, alias="src"),
        joins=[SqlJoin("INNER", SqlTableRef(spine, alias="spine"), condition)],
    )
    return _RelationState(
        source_name=output_name,
        columns=[str(alias) for alias in select_config],
        ctes=[*state.ctes, SqlCte(output_name, query)],
    )


def _lower_attribution_join(
    config: dict[str, Any], *, output_name: str, warehouse: str
) -> _RelationState:
    base = str(config.get("base", "") or "").strip()
    attributed = str(config.get("attributed", "") or "").strip()
    base_time = str(config.get("base_time", "") or "").strip()
    attributed_time = str(config.get("attributed_time", "") or "").strip()
    if not base or not attributed or not base_time or not attributed_time:
        raise SemanticLayerError(
            "INVALID_CONFIG",
            "attribution_join requires base, attributed, base_time, and attributed_time",
        )
    conditions: list[SqlExpr] = []
    for key in _ensure_list(config.get("keys", [])):
        if isinstance(key, dict):
            left = str(key.get("base", "") or "").strip()
            right = str(key.get("attributed", left) or "").strip()
            transform = str(key.get("transform", "") or "").lower()
        else:
            left = right = str(key)
            transform = ""
        if not left or not right:
            raise SemanticLayerError(
                "INVALID_CONFIG", "attribution_join key mappings require base/attributed columns"
            )
        left_expr: SqlExpr = SqlIdentifier(parts=["base", left])
        right_expr: SqlExpr = SqlIdentifier(parts=["attributed", right])
        if transform == "lower":
            left_expr = SqlCall("LOWER", [left_expr])
            right_expr = SqlCall("LOWER", [right_expr])
        conditions.append(SqlBinary(left_expr, "=", right_expr))
    conditions.append(
        SqlBinary(
            SqlIdentifier(parts=["attributed", attributed_time]),
            ">=",
            SqlIdentifier(parts=["base", base_time]),
        )
    )
    lookback = dict(config.get("lookback", {}) or {})
    if lookback:
        unit = str(lookback.get("unit", "day") or "day")
        value = int(lookback.get("value", 0) or 0)
        if value > 0:
            conditions.append(
                SqlBinary(
                    dialect_for_warehouse(warehouse).date_diff(
                        unit,
                        SqlIdentifier(parts=["base", base_time]),
                        SqlIdentifier(parts=["attributed", attributed_time]),
                    ),
                    "<=",
                    SqlLiteral(value),
                )
            )
    if not conditions:
        raise SemanticLayerError(
            "INVALID_CONFIG", "attribution_join requires at least one join predicate"
        )
    condition = conditions[0]
    for item in conditions[1:]:
        condition = SqlBinary(condition, "AND", item)
    select_config = _ensure_mapping(config.get("select", {}), path="attribution_join select")
    if not select_config:
        raise SemanticLayerError(
            "INVALID_CONFIG", "attribution_join requires explicit select outputs"
        )
    fields = [
        SqlField(_expr(expr_raw, default_alias="base", warehouse=warehouse), str(alias))
        for alias, expr_raw in select_config.items()
    ]
    query = SqlSelect(
        select=fields,
        from_table=SqlTableRef(base, alias="base"),
        joins=[
            SqlJoin(
                str(config.get("type", "left")).upper(),
                SqlTableRef(attributed, alias="attributed"),
                condition,
            )
        ],
    )
    return _RelationState(
        source_name=output_name,
        columns=[str(alias) for alias in select_config],
        ctes=[SqlCte(output_name, query)],
    )


def _branch_select(source: str, columns: dict[str, Any], *, warehouse: str) -> SqlSelect:
    return SqlSelect(
        select=[
            SqlField(_expr(expr_raw, warehouse=warehouse), str(alias))
            for alias, expr_raw in columns.items()
        ],
        from_table=SqlTableRef(source, alias="src"),
    )


def _lower_union_all(config: dict[str, Any], *, output_name: str, warehouse: str) -> _RelationState:
    branches = [
        _ensure_mapping(item, path="union_all branch")
        for item in _ensure_list(config.get("branches", config.get("value", [])))
    ]
    if not branches:
        raise SemanticLayerError("INVALID_CONFIG", "union_all requires branches")
    queries: list[SqlSelect] = []
    expected_columns: list[str] = []
    for index, branch in enumerate(branches):
        source = str(branch.get("relation", "") or "").strip()
        columns = _ensure_mapping(branch.get("columns", {}), path="union_all branch columns")
        if not source or not columns:
            raise SemanticLayerError(
                "INVALID_CONFIG", "union_all branches require relation and columns"
            )
        branch_columns = [str(alias) for alias in columns]
        if index == 0:
            expected_columns = branch_columns
        elif branch_columns != expected_columns:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "union_all branch columns must match exactly",
                details={"expected": expected_columns, "actual": branch_columns},
            )
        queries.append(_branch_select(source, columns, warehouse=warehouse))
    return _RelationState(
        source_name=output_name,
        columns=expected_columns,
        ctes=[SqlCte(output_name, SqlSetQuery("UNION ALL", queries))],
    )


def _lower_step(
    step: RelationPipelineStep,
    state: _RelationState,
    relation: RelationConfig,
    index: int,
    *,
    warehouse: str,
    tables: Mapping[str, frozenset[str]],
    lineages: frozenset[str],
) -> _RelationState:
    """One step lowered, its columns' lineage recorded; ``lineages`` are the dates and
    timestamps a filter compares text with (:func:`_temporal_lineage`)."""
    after = _lower_step_sql(
        step, state, relation, index, warehouse=warehouse, tables=tables, lineages=lineages
    )
    return replace(after, lineage=_lineage(step, state, after, f"{relation.id}#{index}"))


def _lower_step_sql(
    step: RelationPipelineStep,
    state: _RelationState,
    relation: RelationConfig,
    index: int,
    *,
    warehouse: str,
    tables: Mapping[str, frozenset[str]],
    lineages: frozenset[str],
) -> _RelationState:
    final = index == len(relation.steps) - 1
    output_name = _step_name(relation, index, step.kind, final=final)
    config = dict(step.config or {})
    if step.kind == "source":
        source = _source_from_config(config)
        columns = [str(item) for item in _ensure_list(config.get("columns", relation.columns))]
        return _RelationState(source_name=source, columns=columns, ctes=list(state.ctes))
    if step.kind == "date_spine":
        query, columns = _date_spine_query(config)
        return _RelationState(
            source_name=output_name, columns=columns, ctes=[*state.ctes, SqlCte(output_name, query)]
        )
    if step.kind == "union_all":
        if state.ctes:
            raise SemanticLayerError(
                "INVALID_CONFIG", "union_all must be the first relation-producing step"
            )
        return _lower_union_all(config, output_name=output_name, warehouse=warehouse)
    if step.kind == "attribution_join":
        if state.ctes:
            raise SemanticLayerError(
                "INVALID_CONFIG", "attribution_join must be the first relation-producing step"
            )
        return _lower_attribution_join(config, output_name=output_name, warehouse=warehouse)
    if not state.source_name:
        raise SemanticLayerError(
            "INVALID_CONFIG", f"Relation step '{step.kind}' requires a source step before it"
        )
    if step.kind == "select":
        return _lower_select(config, state, output_name=output_name, warehouse=warehouse)
    if step.kind == "where":
        return _lower_where(
            config,
            state,
            output_name=output_name,
            warehouse=warehouse,
            temporal=_stage_temporal(state, lineages),
        )
    if step.kind == "json_extract":
        return _lower_json_extract(config, state, output_name=output_name, warehouse=warehouse)
    if step.kind == "explode":
        return _lower_explode(config, state, output_name=output_name, warehouse=warehouse)
    if step.kind == "group_by":
        return _lower_group_by(config, state, output_name=output_name, warehouse=warehouse)
    if step.kind == "join":
        return _lower_join(config, state, output_name=output_name, warehouse=warehouse)
    if step.kind in {"semi_join", "anti_join", "exclude"}:
        return _lower_semi_join(
            config,
            state,
            output_name=output_name,
            warehouse=warehouse,
            negated=step.kind in {"anti_join", "exclude"},
            temporal={
                "src": _stage_temporal(state, lineages),
                "right": tables.get(
                    str(config.get("relation", "") or "").strip(),
                    frozenset(),
                ),
            },
        )
    if step.kind == "window":
        return _lower_window(config, state, output_name=output_name, warehouse=warehouse)
    if step.kind == "state_as_of":
        return _lower_state_as_of(config, state, output_name=output_name, warehouse=warehouse)
    raise SemanticLayerError("INVALID_CONFIG", f"Unsupported relation step kind '{step.kind}'")


def _lower_steps(
    relation: RelationConfig,
    *,
    warehouse: str,
    tables: Mapping[str, frozenset[str]],
    lineages: frozenset[str],
) -> _RelationState:
    state = _RelationState()
    for index, step in enumerate(relation.steps):
        state = _lower_step(
            step, state, relation, index, warehouse=warehouse, tables=tables, lineages=lineages
        )
    return state


def lower_relation(
    relation: RelationConfig,
    *,
    warehouse: str,
    tables: Mapping[str, frozenset[str]] | None = None,
) -> tuple[list[SqlCte], list[str]]:
    """``tables`` names each table's date and timestamp columns (:func:`_temporal_tables`).

    A first pass traces each column's lineage; when any is a date or timestamp, a second
    lowers the filters with it."""
    tables = tables or {}
    state = _lower_steps(relation, warehouse=warehouse, tables=tables, lineages=frozenset())
    lineages = _temporal_lineage(relation, state, tables)
    if lineages:
        state = _lower_steps(relation, warehouse=warehouse, tables=tables, lineages=lineages)
    if state.source_name != relation.output_name:
        if not state.columns:
            raise SemanticLayerError(
                "INVALID_CONFIG", f"Relation '{relation.id}' does not declare output columns"
            )
        query = SqlSelect(
            select=_column_fields(state.columns),
            from_table=SqlTableRef(state.source_name, alias="src"),
        )
        state.ctes.append(SqlCte(relation.output_name, query))
        state.source_name = relation.output_name
    return state.ctes, state.columns


def _table_ref_name(table: Any) -> str:
    if isinstance(table, SqlTableFunction):  # UNNEST(...) and friends read no relation
        return ""
    return str(getattr(table, "name", "") or "").strip()


def _collect_table_refs(query: SqlQuery) -> set[str]:
    refs: set[str] = set()

    def _walk_expr(expr: SqlExpr | None) -> None:
        if expr is None:
            return
        if isinstance(expr, SqlBinary):
            _walk_expr(expr.left)
            _walk_expr(expr.right)
        elif isinstance(expr, SqlCall):
            for arg in expr.args:
                _walk_expr(arg)
        elif isinstance(expr, SqlIn):
            _walk_expr(expr.expr)
            for value in expr.values:
                _walk_expr(value)
        elif isinstance(expr, SqlExists):
            _walk_select(expr.query)
        elif isinstance(expr, SqlCase):
            for when in expr.whens:
                _walk_expr(when.condition)
                _walk_expr(when.result)
            _walk_expr(expr.else_expr)
        elif isinstance(expr, SqlCast):
            _walk_expr(expr.expr)
        elif isinstance(expr, SqlWindow):
            _walk_expr(expr.function)
            for item in expr.partition_by:
                _walk_expr(item)
            for order_term in expr.order_by:
                _walk_expr(order_term.expr)
        elif isinstance(expr, SqlJsonExtract):
            _walk_expr(expr.expr)

    def _walk_select(node: SqlSelect) -> None:
        if node.from_table is not None:
            name = _table_ref_name(node.from_table)
            if name:
                refs.add(name)
        for join in node.joins:
            name = _table_ref_name(join.table)
            if name:
                refs.add(name)
            _walk_expr(join.on)
        for select_field in node.select:
            _walk_expr(select_field.expression)
        for expr in [*node.where, *node.group_by, *node.having, *node.qualify]:
            _walk_expr(expr)
        for order in node.order_by:
            _walk_expr(order.expression)
        for cte in node.ctes:
            _walk_query(cte.query)

    def _walk_query(node: SqlQuery) -> None:
        if isinstance(node, SqlSetQuery):
            for branch in node.queries:
                _walk_select(branch)
            return
        _walk_select(node)

    _walk_query(query)
    return refs


def _lower_relations_for_config(config: PackageConfig) -> dict[str, _LoweredRelation]:
    lowered: dict[str, _LoweredRelation] = {}
    output_to_relation: dict[str, str] = {}
    cte_to_relation: dict[str, str] = {}

    tables = _temporal_tables(config)
    for relation in config.relations:
        ctes, _ = lower_relation(relation, warehouse=config.package.warehouse, tables=tables)
        cte_names = {cte.name for cte in ctes}
        lowered[relation.id] = _LoweredRelation(relation=relation, ctes=ctes, cte_names=cte_names)
        output_to_relation[relation.output_name] = relation.id
        for cte_name in cte_names:
            cte_to_relation[cte_name] = relation.id

    for relation_id, row in list(lowered.items()):
        dependencies: set[str] = set()
        for cte in row.ctes:
            for ref in _collect_table_refs(cte.query):
                dep = output_to_relation.get(ref) or cte_to_relation.get(ref)
                if dep and dep != relation_id:
                    dependencies.add(dep)
        lowered[relation_id] = _LoweredRelation(
            relation=row.relation,
            ctes=row.ctes,
            cte_names=row.cte_names,
            dependencies=dependencies,
        )
    return lowered


def relation_source_tables(config: PackageConfig, relation_ids: Iterable[str]) -> set[str]:
    """Return the stored tables the given relation pipelines read.

    Follows each pipeline's dependencies on other pipelines and drops every
    compiler-generated CTE name, so only warehouse relations remain.
    """
    lowered = _lower_relations_for_config(config)
    generated = {name for row in lowered.values() for name in row.cte_names}
    tables: set[str] = set()
    pending = [relation_id for relation_id in relation_ids if relation_id in lowered]
    seen: set[str] = set()
    while pending:
        relation_id = pending.pop()
        if relation_id in seen:
            continue
        seen.add(relation_id)
        pending.extend(lowered[relation_id].dependencies)
        for cte in lowered[relation_id].ctes:
            tables.update(ref for ref in _collect_table_refs(cte.query) if ref not in generated)
    return tables


def _required_relation_ctes(config: PackageConfig, query: SqlSelect) -> list[SqlCte]:
    lowered = _lower_relations_for_config(config)
    if not lowered:
        return []
    output_to_relation = {
        row.relation.output_name: relation_id for relation_id, row in lowered.items()
    }
    cte_to_relation = {
        cte_name: relation_id for relation_id, row in lowered.items() for cte_name in row.cte_names
    }
    query_cte_names = {cte.name for cte in query.ctes}
    roots: set[str] = set()
    for ref in _collect_table_refs(query):
        if ref in query_cte_names:
            continue
        relation_id = output_to_relation.get(ref) or cte_to_relation.get(ref)
        if relation_id:
            roots.add(relation_id)

    ordered: list[str] = []
    permanent: set[str] = set()
    temporary: set[str] = set()

    def visit(relation_id: str) -> None:
        if relation_id in permanent:
            return
        if relation_id in temporary:
            raise SemanticLayerError(
                "INVALID_CONFIG",
                "Relation pipeline dependency cycle detected",
                details={"relation_id": relation_id},
            )
        temporary.add(relation_id)
        for dependency_id in sorted(lowered[relation_id].dependencies):
            visit(dependency_id)
        temporary.remove(relation_id)
        permanent.add(relation_id)
        ordered.append(relation_id)

    for relation_id in sorted(roots):
        visit(relation_id)

    ctes: list[SqlCte] = []
    seen: set[str] = set()
    for relation_id in ordered:
        for cte in lowered[relation_id].ctes:
            if cte.name in seen or cte.name in query_cte_names:
                continue
            seen.add(cte.name)
            ctes.append(cte)
    return ctes


def pipeline_cte_names(config: PackageConfig) -> frozenset[str]:
    """Every CTE name ``attach_relation_ctes`` may add: each pipeline's output and steps."""
    if not config.relations:
        return frozenset()
    lowered = _lower_relations_for_config(config).values()
    return frozenset(
        {row.relation.output_name for row in lowered}
        | {name for row in lowered for name in row.cte_names}
    )


def attach_relation_ctes(config: PackageConfig, query: SqlSelect) -> SqlSelect:
    if not config.relations:
        return query
    relation_ctes = _required_relation_ctes(config, query)
    if not relation_ctes:
        return query
    return SqlSelect(
        select=query.select,
        from_table=query.from_table,
        joins=query.joins,
        where=query.where,
        group_by=query.group_by,
        having=query.having,
        qualify=query.qualify,
        order_by=query.order_by,
        limit=query.limit,
        offset=query.offset,
        ctes=[*relation_ctes, *query.ctes],
    )
