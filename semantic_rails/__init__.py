"""Semantic Rails — governed semantic-layer runtime.

The public surface lives in :mod:`semantic_rails.runtime` (programmatic
use), :mod:`semantic_rails.cli` (CLI commands), :mod:`semantic_rails.api`
(threaded HTTP server), :mod:`semantic_rails.asgi` (ASGI app), and
:mod:`semantic_rails.mcp` (MCP adapter). This package deliberately keeps
``__all__`` empty so consumers import from the explicit submodules and
nothing leaks by accident.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("semantic-rails")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = []
