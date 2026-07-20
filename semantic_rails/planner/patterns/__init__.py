"""Intent pattern registry.

Each module under this package contributes one ``IntentPattern`` to
``PATTERNS``. The orchestrator evaluates active patterns, then uses
draft score and registry order to pick the winning draft.

Adding a new pattern:

1. Create ``patterns/<name>.py`` exporting a ``PATTERN: IntentPattern``.
2. Append it to ``PATTERNS`` below, choosing the priority slot
   carefully — patterns earlier in the list win ties.
3. Add a unit test in ``tests/semantic_rails/test_plan_<name>.py``.
"""

from __future__ import annotations

from ._protocol import IntentPattern
from .distribution_entity_value import PATTERN as _DISTRIBUTION_ENTITY_VALUE
from .filtered_adoption_funnel import PATTERN as _FILTERED_ADOPTION_FUNNEL
from .inline_comparison import PATTERN as _INLINE_COMPARISON
from .inline_period_shift import PATTERN as _INLINE_PERIOD_SHIFT
from .metric_by_dimension_rollup import PATTERN as _METRIC_BY_DIMENSION_ROLLUP
from .qualified_metric_rollup import PATTERN as _QUALIFIED_METRIC_ROLLUP
from .runtime_metric_arithmetic import PATTERN as _RUNTIME_METRIC_ARITHMETIC
from .scoped_predicate_ratio import PATTERN as _SCOPED_PREDICATE_RATIO

# Earlier patterns preempt later ones on score ties because some intents
# (e.g. an adoption funnel with a threshold) can weakly match more than
# one pattern. Catch-alls run last so the more specific patterns win.
PATTERNS: list[IntentPattern] = [
    _FILTERED_ADOPTION_FUNNEL,
    _QUALIFIED_METRIC_ROLLUP,
    _INLINE_PERIOD_SHIFT,
    _DISTRIBUTION_ENTITY_VALUE,
    _SCOPED_PREDICATE_RATIO,
    _RUNTIME_METRIC_ARITHMETIC,
    _INLINE_COMPARISON,
    _METRIC_BY_DIMENSION_ROLLUP,
]

__all__ = ["IntentPattern", "PATTERNS"]
