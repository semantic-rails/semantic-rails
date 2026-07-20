from __future__ import annotations


def _all_metric_predicates(*args, **kwargs):
    from ..compiler import _all_metric_predicates as fn

    return fn(*args, **kwargs)


def _predicate_ctes_and_join(*args, **kwargs):
    from ..compiler import _predicate_ctes_and_join as fn

    return fn(*args, **kwargs)


def _predicate_time_alias(*args, **kwargs):
    from ..compiler import _predicate_time_alias as fn

    return fn(*args, **kwargs)


def _predicate_time_spec(*args, **kwargs):
    from ..compiler import _predicate_time_spec as fn

    return fn(*args, **kwargs)


def _predicate_where_condition(*args, **kwargs):
    from ..compiler import _predicate_where_condition as fn

    return fn(*args, **kwargs)


def _scoped_predicate_expr_payload(*args, **kwargs):
    from ..compiler import _scoped_predicate_expr_payload as fn

    return fn(*args, **kwargs)
