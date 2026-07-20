"""``python -m semantic_rails`` entry point.

Delegates to :func:`semantic_rails.cli.main` so ``python -m semantic_rails
<subcommand>`` and the ``semantic-rails`` console script behave
identically.
"""

from .cli import main

if __name__ == "__main__":
    main()
