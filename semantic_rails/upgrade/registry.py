"""Ordered upgrade rule registry; each release contributes its own area rules."""

from . import rules_authoring, rules_canonical, rules_query_ir, rules_retired
from .model import Rule

RULES: tuple[Rule, ...] = (
    *rules_retired.RULES,
    *rules_canonical.RULES,
    *rules_authoring.RULES,
    *rules_query_ir.RULES,
)
