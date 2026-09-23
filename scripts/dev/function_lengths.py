"""Report long functions: the line-count side of the complexity ratchet.

Lists every function and method longer than ``--min`` lines (default 150),
longest first, then how many are over 150 and over 300 lines. A function's
length runs from its ``def`` line to its last line, docstring included.
Nested functions count on their own and inside their parent.

To check that no function a change touches got longer, save
``--by-name --min 0`` before and after and diff the two: each line is
``qualified.name length path:line``, sorted by name, so a function moved to
another module differs only in its path.

Report-only: exits 0 unless the arguments are wrong. A file that can't be
read or parsed is skipped with a note on stderr.

Usage:
    uv run python scripts/dev/function_lengths.py [--min LINES] [--by-name] [PATH ...]

PATH is a .py file or a directory to search recursively (default: the
semantic_rails package). Paths print relative to the repository root.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
THRESHOLDS = (150, 300)


def function_lengths(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(length, qualified.name, "path:line")`` for every function in ``path``."""
    # Parsing bytes honours a BOM and a PEP 263 coding cookie.
    tree = ast.parse(path.read_bytes(), filename=str(path))
    resolved = path.resolve()
    shown = resolved.relative_to(REPO_ROOT) if resolved.is_relative_to(REPO_ROOT) else path
    found: list[tuple[int, str, str]] = []

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = ".".join((*scope, child.name))
                length = (child.end_lineno or child.lineno) - child.lineno + 1
                found.append((length, name, f"{shown}:{child.lineno}"))
                visit(child, (*scope, child.name))
            elif isinstance(child, ast.ClassDef):
                visit(child, (*scope, child.name))
            else:
                visit(child, scope)

    visit(tree, ())
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", metavar="PATH")
    parser.add_argument("--min", type=int, default=THRESHOLDS[0], metavar="LINES")
    parser.add_argument(
        "--by-name", action="store_true", help="print 'name length path:line', sorted by name"
    )
    args = parser.parse_args(argv)

    paths = [Path(raw) for raw in args.paths] or [REPO_ROOT / "semantic_rails"]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        parser.error(f"no such path: {', '.join(missing)}")
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])

    lengths: list[tuple[int, str, str]] = []
    for file in files:
        try:
            lengths.extend(function_lengths(file))
        except (OSError, SyntaxError, ValueError) as exc:
            print(f"skipped {file}: {exc}", file=sys.stderr)

    listed = [row for row in lengths if row[0] > args.min]
    if args.by_name:
        for length, name, where in sorted(listed, key=lambda row: (row[1], row[2])):
            print(f"{name} {length} {where}")
    else:
        for length, name, where in sorted(listed, key=lambda row: (-row[0], row[2])):
            print(f"{length:6d}  {where} {name}")
    totals = ", ".join(
        f"over {limit} lines: {sum(length > limit for length, _, _ in lengths)}"
        for limit in THRESHOLDS
    )
    print(f"functions: {len(lengths)}, {totals}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
