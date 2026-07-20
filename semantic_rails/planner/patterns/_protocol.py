"""Pattern protocol — extracted so individual pattern modules can
import it without circular dependencies on ``patterns/__init__.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .._base import RuntimeCompositionDraft


@dataclass(frozen=True)
class IntentPattern:
    """A composable matcher that turns an intent into a query draft.

    ``match`` receives the runtime, the (already lowered) intent text,
    and the pre-tokenized term set. It returns either a
    ``RuntimeCompositionDraft`` (the pattern fired) or ``None`` (the
    pattern declines this intent).
    """

    name: str
    match: Callable[[Any, str, set[str]], RuntimeCompositionDraft | None]


__all__ = ["IntentPattern"]
