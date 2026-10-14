"""Ordered upgrade rule registry; each release contributes its own area rules."""

from . import (
    rules_authoring,
    rules_calls,
    rules_canonical,
    rules_expressions,
    rules_retired,
    rules_strict,
    rules_time,
)
from .model import Rule

RULES: tuple[Rule, ...] = (
    *rules_retired.RULES,
    *rules_canonical.RULES,
    *rules_authoring.RULES,
    *rules_expressions.RULES,
    *rules_calls.RULES,
    *rules_time.RULES,
    *rules_strict.RULES,
)
