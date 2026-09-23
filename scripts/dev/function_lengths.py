"""Report long functions: the line-count side of the complexity ratchet.

Lists every function and method longer than ``--min`` lines (default 150),
longest first, then how many are over 150 and over 300 lines. A function's
length runs from its ``def`` line to its last line, docstring included.
Nested functions count on their own and inside their parent.

Report-only: always exits 0. Run it before and after splitting a module; no
function the change touches should get longer.

Usage:
    uv run python scripts/dev/function_lengths.py [--min LINES] [PATH ...]

PATH is a .py file or a directory to search recursively (default: semantic_rails).
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

THRESHOLDS = (150, 300)


def function_lengths(path: Path) -> list[tuple[int, str]]:
    """Return ``(length, "path:line qualified.name")`` for every function in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = ".".join((*scope, child.name))
                length = (child.end_lineno or child.lineno) - child.lineno + 1
                found.append((length, f"{path}:{child.lineno} {name}"))
                visit(child, (*scope, child.name))
            elif isinstance(child, ast.ClassDef):
                visit(child, (*scope, child.name))
            else:
                visit(child, scope)

    visit(tree, ())
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", default=["semantic_rails"], metavar="PATH")
    parser.add_argument("--min", type=int, default=THRESHOLDS[0], metavar="LINES")
    args = parser.parse_args(argv)

    files: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    lengths = [row for file in files for row in function_lengths(file)]

    for length, where in sorted(lengths, key=lambda row: (-row[0], row[1])):
        if length > args.min:
            print(f"{length:6d}  {where}")
    totals = ", ".join(
        f"over {limit} lines: {sum(length > limit for length, _ in lengths)}"
        for limit in THRESHOLDS
    )
    print(f"functions: {len(lengths)}, {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
