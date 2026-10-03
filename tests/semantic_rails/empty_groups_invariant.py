"""The empty-group invariant, checked on compiled SQL.

Every sum, count and distinct count a projection reads comes from ``guarded_base``, and no
other ``COALESCE(<measure>, 0)`` turns a NULL into 0. Each sum there also reads the count of
the rows it read in the group, so it fills 0 only where there were none. Shared by the unit
tests and the differential correctness corpus, so both hold every query they compile to it.
"""

from __future__ import annotations

from typing import Any

from semantic_rails.compiler import resolve_compile_config
from semantic_rails.compiler_parts.empty_groups import (
    GUARDED_BASE,
    base_reads,
    sql_nodes,
    zero_aliases,
    zero_outputs,
)
from semantic_rails.compiler_parts.sql_lowering import (
    _anchored_entity_set_select,
    _plan_requires_agent_dag_lowering,
)
from semantic_rails.schema import PackageConfig
from semantic_rails.sql_ast import SqlCall, SqlCase, SqlCte, SqlIdentifier, SqlLiteral, SqlSelect


def _is_zero_fill(node: Any) -> bool:
    return (
        isinstance(node, SqlCall)
        and node.name.upper() == "COALESCE"
        and len(node.args) == 2
        and (node.args[1] == SqlLiteral(0) or isinstance(node.args[1], SqlCase))
    )


def _projection(select: SqlSelect) -> SqlSelect:
    ctes = {cte.name: cte.query for cte in select.ctes}
    return ctes.get("projected") or ctes.get("agent_projected") or select  # type: ignore[return-value]


def assert_settled_in_one_place(compiled: dict[str, Any], config: PackageConfig) -> None:
    """Assert the compiled query settles its empty groups in ``guarded_base`` and nowhere else."""
    select, plan = compiled["sql_ast"], compiled["logical_plan"]
    config = resolve_compile_config(plan, config)  # with the query's aggregate_if measures
    nodes = list(sql_nodes(select))
    guards = [
        node
        for node in nodes
        if isinstance(node, SqlCte) and node.name.split("__")[-1] == GUARDED_BASE
    ]
    inside = {id(node) for guard in guards for node in sql_nodes(guard.query)}
    stray = [node for node in nodes if _is_zero_fill(node) and id(node) not in inside]
    assert not stray, f"COALESCE(x, 0) outside guarded_base: {stray}"

    if _anchored_entity_set_select(plan, config) is not None:
        return  # one aggregate over another, NULL when the denominator is empty
    # A branch-combined query settles each branch and then its combine.
    dag = _plan_requires_agent_dag_lowering(plan)
    expected = zero_outputs(plan, config) if dag else zero_aliases(plan.measure_plans, config)
    source = _projection(select).from_table
    assert source is not None
    assert (source.name == GUARDED_BASE) is bool(expected), (
        f"the projection reads {source.name}, and should read {GUARDED_BASE} for {sorted(expected)}"
    )
    if expected:
        guard = next(node for node in guards if node.name == GUARDED_BASE)
        settled = [
            field
            for field in guard.query.select
            if isinstance(field.expression, SqlCase) or _is_zero_fill(field.expression)
        ]
        assert len(settled) == len(expected), (
            f"{GUARDED_BASE} settles {len(settled)} measures, expected {sorted(expected)}"
        )
        # Besides its own value (and the keys, for coverage), a sum reads exactly one column:
        # its row count. A count reads none.
        keys = {
            field.alias
            for field in guard.query.select
            if isinstance(field.expression, SqlIdentifier)
        }
        counted = [
            field
            for field in settled
            if len(base_reads(field.expression) - {field.alias} - keys) == 1
        ]
        sums = [aggregation for aggregation in expected.values() if aggregation == "sum"]
        assert len(counted) == len(sums), (
            f"{GUARDED_BASE} reads a row count for {len(counted)} measures, "
            f"expected one for each of its {len(sums)} sums"
        )
