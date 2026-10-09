"""Error type and the registered error-code catalog.

Defines :class:`SemanticLayerError` — the exception type the runtime
raises (``compiler.NonAdditiveRefusal`` is a subclass carrying in-process-only
data). ``ERROR_CODES`` are the refusals of the code table in
:mod:`semantic_rails.issues`; the contract test in
``tests/semantic_rails/test_error_codes_contract.py`` fails on a code missing from it.
"""

from __future__ import annotations

from typing import Any

from .issues import ERROR_CODES as ERROR_CODES

# What a caller reads about an unexpected exception; its text goes to the server log only.
UNEXPECTED_ERROR_MESSAGE = "An unexpected engine error occurred; the detail is in the server log."


class SemanticLayerError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


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
