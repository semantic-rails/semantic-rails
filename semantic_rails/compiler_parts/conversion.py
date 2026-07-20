from __future__ import annotations


def _conversion_leaf_cte(*args, **kwargs):
    from ..compiler import _conversion_leaf_cte as fn

    return fn(*args, **kwargs)
