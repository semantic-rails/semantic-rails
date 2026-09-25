"""``semantic-rails`` console-script implementation.

Commands live in :mod:`semantic_rails.cli.commands`, shared plumbing in
:mod:`semantic_rails.cli.common`, report builders in
:mod:`semantic_rails.cli.reports` and human output in
:mod:`semantic_rails.cli.output`. :func:`main` is the console-script entry
point and is re-exported by :mod:`semantic_rails.__main__`.
"""

from __future__ import annotations

from .app import main

__all__ = ["main"]
