"""Error type and the registered error-code catalog.

Defines :class:`SemanticLayerError` — the exception type the runtime
raises (``compiler.NonAdditiveRefusal`` is a subclass carrying in-process-only
data). ``ERROR_CODES`` are the refusals of the code table in
:mod:`semantic_rails.issues`; the contract test in
``tests/semantic_rails/test_error_codes_contract.py`` fails on a code missing from it.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .issues import ERROR_CODES as ERROR_CODES

# What a caller reads about an unexpected exception; its text goes to the server log only.
UNEXPECTED_ERROR_MESSAGE = "An unexpected engine error occurred; the detail is in the server log."


class SemanticLayerError(Exception):
    # A cache entry's details (``with_shared_details``); never handed out.
    _shared_details: dict[str, Any] | None = None

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    @classmethod
    def with_shared_details(
        cls, code: str, message: str, details: dict[str, Any]
    ) -> SemanticLayerError:
        """The error again from a cache entry's ``details``, which stay the entry's: this
        error's ``details`` are a deep copy of them, made when first read. No caller can reach
        the entry to change it, and a caller that reads only the code copies nothing."""
        error = cls(code, message)
        del error.__dict__["_details"]
        error._shared_details = details
        return error

    @property
    def details(self) -> dict[str, Any]:
        state = self.__dict__
        if "_details" not in state:
            # The first read of an error from ``with_shared_details``.
            state.setdefault("_details", dict(deepcopy(self._shared_details or {})))
        details: dict[str, Any] = state["_details"]
        return details

    @details.setter
    def details(self, value: dict[str, Any]) -> None:
        self.__dict__["_details"] = value


def refusal_text(message: str, details: Mapping[str, Any]) -> tuple[str, str]:
    """A refusal for a person to read: its message, led by its path when the message does not
    name it, and its first hint ("" when it has none)."""
    path = str(details.get("path") or "")
    text = message if not path or message.startswith(path) else f"{path}: {message}"
    hints = [hint for hint in details.get("recovery_hints") or [] if isinstance(hint, Mapping)]
    return text, str(hints[0].get("message") or "") if hints else ""


def query_execution_error(details: dict[str, Any]) -> SemanticLayerError:
    """Translate driver failures without serializing driver text or credentials.

    Callers supply only structural metadata and retain the original exception
    as ``__cause__`` for in-process diagnostics. Raw driver messages can include
    SQL, rows, connection strings, and secrets; truncating them is not redaction.
    """
    return SemanticLayerError(
        "QUERY_EXECUTION_ERROR",
        "Warehouse query execution failed. Check warehouse query history and operator diagnostics.",
        details=details,
    )
