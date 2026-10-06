"""Ordered upgrade rule registry; each release contributes its own area rules."""

from .model import Rule

RULES: tuple[Rule, ...] = ()
