"""Semantic Rails — governed semantic-layer runtime.

Python hosts import from :mod:`semantic_rails.embedding`, the engine's one public Python
surface; from 0.4.0 it follows Semantic Versioning (docs/EMBEDDING.md, "Stability"). The
``semantic-rails`` CLI, the ``/api/v1`` HTTP API (``semantic_rails.asgi:app`` or
``semantic-rails serve``) and the MCP servers are the other public interfaces, versioned by
the JSON contracts in docs/CONTRACTS.md. Every other module is internal and can change in any
release. This package keeps ``__all__`` empty so nothing leaks by accident.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("semantic-rails")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = []
