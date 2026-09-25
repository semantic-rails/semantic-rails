"""Human-readable output for CLI and REPL reports, including result tables."""

from __future__ import annotations

import json
import math
import unicodedata
from decimal import Decimal
from typing import Any

from .common import _BUNDLED_NOTE, _print, _quote

_MAX_HUMAN_ROWS = 500


_MAX_CELL_WIDTH = 40


def _authoring_warning_messages(report: dict[str, Any]) -> list[str]:
    warnings = list(report.get("warnings", []) or [])
    parse = report.get("parse", {})
    if isinstance(parse, dict):
        warnings.extend(list(parse.get("warnings", []) or []))
    messages: list[str] = []
    for warning in warnings:
        message = str(warning.get("message", "")) if isinstance(warning, dict) else str(warning)
        if message and message not in messages:
            messages.append(message)
    return messages


def _authoring_error_messages(report: dict[str, Any]) -> list[str]:
    errors = list(report.get("errors", []) or [])
    parse = report.get("parse", {})
    if isinstance(parse, dict):
        errors.extend(list(parse.get("errors", []) or []))
    messages: list[str] = []
    for error in errors:
        message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
        if message and message not in messages:
            messages.append(message)
    return messages


def _package_display(package: dict[str, Any]) -> str:
    package_id = str(package.get("id", "") or "")
    source_path = str(package.get("source_path", "") or "")
    if package.get("bundled"):
        return package_id + _BUNDLED_NOTE
    if package_id and source_path:
        return f"{package_id} ({source_path})"
    return package_id or source_path or "(unknown)"


def _print_setup_report(report: dict[str, Any]) -> None:
    print("Semantic Rails setup")
    print(f"Repo: {report['repo_root']}")
    for check in report["checks"]:
        _print_check_line(check)
    print()
    print("Next commands:")
    for action in report["next_actions"]:
        print(f"  {action}")


def _print_debug_report(report: dict[str, Any]) -> None:
    print("Semantic Rails debug")
    print(f"Repo: {report['repo_root']}")
    for check in report["checks"]:
        _print_check_line(check)
    failing = [check for check in report["checks"] if not check.get("ok")]
    if failing:
        print()
        print("Fix the failed checks above, then rerun semantic-rails debug.")


def _print_objects(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    label = package.get("id") or "(package)"
    if package.get("bundled"):
        label += _BUNDLED_NOTE
    print(f"{label}: {report['count']} {report['resource_type']} object(s)")
    if report.get("search"):
        print(f"Search: {report['search']}")
    for row in list(report.get("objects", []) or []):
        status = "" if row.get("available", True) else " unavailable"
        label = row.get("label") or row.get("id")
        print(f"  {row.get('kind')}: {row.get('id')}{status}")
        if label and label != row.get("id"):
            print(f"       {label}")
    if report.get("truncated"):
        print("  ... use --limit 0 or --json to see all objects")


def _print_ask_report(report: dict[str, Any]) -> None:
    package = dict(report.get("package", {}) or {})
    print(f"Question: {report.get('question', '')}")
    print(f"Package: {_package_display(package)}")
    interpretation = str(report.get("interpretation", "") or "")
    if interpretation:
        print(f"Interpreted as: {interpretation}")
    plan = dict(report.get("plan", {}) or {})
    print(f"Plan: {plan.get('pattern') or '(none)'}")
    resolved = list(plan.get("resolved", []) or [])
    if resolved:
        print("Resolved:")
        for row in resolved:
            print(f"  {row.get('kind')}: {row.get('id')} ({row.get('label')})")
    warnings: list[Any] = []
    result = report.get("result")
    if isinstance(result, dict):
        rows = list(result.get("rows", []) or [])
        count = result.get("row_count", len(rows))
        truncated = bool(result.get("truncated"))
        planned = result.get("planned_limit")
        planned_row_limit = result.get("planned_row_limit")
        plan_capped = bool(planned_row_limit) and planned_row_limit == result.get("row_limit")
        if truncated and plan_capped:
            limit_note = (
                f" (stopped at the planned query's own {planned_row_limit}-row cap;"
                " more rows match)"
            )
        elif truncated:
            limit_note = f" (stopped at the {result.get('row_limit')}-row limit; more rows match)"
        elif planned and count >= planned:
            limit_note = f" (the planned query itself returns at most {planned} rows)"
        else:
            limit_note = ""
        print(f"Rows: {count}{limit_note}")
        shown = rows[:_MAX_HUMAN_ROWS]
        _print_rows(shown, list(result.get("output_columns", []) or []))
        if len(rows) > len(shown):
            print(f"... {len(rows) - len(shown)} more rows not shown; use --json to see them all.")
        if truncated and not plan_capped:
            hint = (
                f"To lift the {result.get('row_limit')}-row cap, run: {_every_row_command(report)}"
            )
            if planned:
                hint += f" (the planned query's own limit of {planned} rows still applies)"
            if planned_row_limit:
                hint += f" (the planned query's own {planned_row_limit}-row cap still applies)"
            print(hint)
        warnings.extend(list(result.get("warnings", []) or []))
        warnings.extend(list(result.get("assumptions", []) or []))
    compiled = report.get("compile")
    if isinstance(compiled, dict):
        print("SQL:")
        print(compiled.get("sql", ""))
        warnings.extend(list(compiled.get("warnings", []) or []))
    if warnings:
        print("Warnings:")
        lines = []
        for warning in warnings:
            if isinstance(warning, dict):
                code = warning.get("code") or warning.get("kind") or "WARNING"
                lines.append(f"  {code}: {warning.get('message', '')}")
            else:
                lines.append(f"  {warning}")
        # The engine repeats a planning warning for each measure it applies to (a ratio
        # has two), and the line doesn't name the measure, so print each line once.
        for line in dict.fromkeys(lines):
            print(line)
    query = report.get("query")
    if query:
        print("Query IR:")
        print(json.dumps(query, indent=2, sort_keys=True, default=str))
    errors = list(report.get("errors", []) or [])
    if errors:
        print("Errors:")
        for error in errors[:5]:
            print(f"  {error.get('code', 'ERROR')}: {error.get('message', error)}")
            # The engine ranks its recovery hints; --json has them all.
            hints = [h for h in error.get("recovery_hints", []) or [] if isinstance(h, dict)]
            if hints and hints[0].get("message"):
                print(f"    Try: {hints[0]['message']}")


def _every_row_command(report: dict[str, Any]) -> str:
    package = dict(report.get("package", {}) or {})
    source = (
        f"--package {_quote(str(package['id']))}"
        if package.get("bundled") and package.get("id")
        else f"--path {_quote(str(package.get('source_path', '')))}"
    )
    return f"semantic-rails ask {source} {_quote(str(report.get('question', '')))} --run --limit 0"


def _print_project_list(report: dict[str, Any]) -> None:
    print(f"Semantic Rails projects ({report['count']})")
    for row in report["packages"]:
        status = "ok" if row.get("ok") else "fail"
        label = row.get("id") or "(unknown)"
        origin = row.get("origin", "")
        print(f"  [{status}] {label} ({origin})")
        print(f"       {row.get('source_path')}")
        summary = row.get("summary")
        if isinstance(summary, dict) and summary:
            metrics = summary.get("metric_recipes", summary.get("metrics", 0))
            print(
                "       "
                f"entities={summary.get('entities', 0)} "
                f"measures={summary.get('measures', 0)} "
                f"metrics={metrics} "
                f"warnings={summary.get('warnings', 0)} "
                f"errors={summary.get('errors', 0)}"
            )


def _print_project_created(report: dict[str, Any]) -> None:
    print("Created Semantic Rails project")
    print(f"Package: {report['package_id']}")
    print(f"Path: {report['project_path']}")
    print("Files:")
    for path in report["changed_files"]:
        print(f"  {path}")
    checks = report.get("checks", {})
    if checks:
        print("Checks:")
        for name, check in checks.items():
            if isinstance(check, dict):
                _print_named_check(name, check)
    print("Next commands:")
    for action in report["next_actions"]:
        print(f"  {action}")


def _print_project_status(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    label = package.get("id") or "(unknown)"
    if package.get("bundled"):
        label += _BUNDLED_NOTE
    print(f"Semantic Rails project: {label}")
    print(f"Path: {report['source_path']}")
    print(f"Layout: {report['layout']}")
    print(f"Files: {len(report['files'])}")
    checks = report.get("checks", {})
    if checks:
        print("Checks:")
        for name, check in checks.items():
            if isinstance(check, dict):
                _print_named_check(name, check)
    print("Next commands:")
    for action in report["next_actions"]:
        print(f"  {action}")


def _print_project_validation(report: dict[str, Any]) -> None:
    package = report.get("package", {})
    label = package.get("id") or package.get("source_path") or "(unknown)"
    print(f"Validation: {label}{_BUNDLED_NOTE if package.get('bundled') else ''}")
    checks = report.get("checks", {})
    for name, check in checks.items():
        if isinstance(check, dict):
            _print_named_check(name, check)
    warnings = _authoring_warning_messages(report)
    if warnings:
        print("Warnings:")
        for warning in warnings[:10]:
            print(f"  {warning}")
        if len(warnings) > 10:
            print(f"  ... {len(warnings) - 10} more warning(s)")
    errors = list(report.get("errors", []) or [])
    if errors:
        print("Errors:")
        # Each probe reports a shared failure (a missing seed, say) again: print it once.
        counts: dict[str, int] = {}
        for error in errors:
            message = error.get("message") or error if isinstance(error, dict) else error
            check = error.get("check") if isinstance(error, dict) else None
            line = f"  {check}: {message}" if check else f"  {message}"
            counts[line] = counts.get(line, 0) + 1
        for line, count in list(counts.items())[:10]:
            print(line + (f" ({count} times)" if count > 1 else ""))
        if len(counts) > 10:
            print(f"  ... {len(counts) - 10} more error(s)")


def _print_profile_report(report: dict[str, Any]) -> None:
    print("Semantic Rails profile")
    print(f"Path: {report.get('path')}")
    print(f"Configured: {report.get('exists')}")
    print(f"Profile: {report.get('active_profile') or '(none)'}")
    print(f"Target: {report.get('active_target') or '(none)'}")
    print(f"Package: {report.get('resolved_package_path') or '(none)'}")
    profiles = ", ".join(list(report.get("profiles", []) or [])) or "(none)"
    if profiles != "(none)":
        print(f"Profiles: {profiles}")
    print("Scope: local CLI only")


def _print_check_line(check: dict[str, Any]) -> None:
    status = "ok" if check.get("ok") else ("warn" if not check.get("required", True) else "fail")
    print(f"  [{status}] {check['name']}: {check.get('summary', '')}")
    if not check.get("ok") and check.get("message"):
        print(f"       {check['message']}")


def _print_named_check(name: str, check: dict[str, Any]) -> None:
    status = "ok" if check.get("ok", True) else "fail"
    bits = [f"[{status}] {name}"]
    for key in (
        "entities",
        "measures",
        "metrics",
        "probes_total",
        "passed",
        "failed",
        "examples_total",
        "tests_total",
        "warnings",
        "errors",
    ):
        if key in check:
            bits.append(f"{key}={check[key]}")
    print("  " + " ".join(bits))


def _print_rows(
    rows: list[dict[str, Any]], output_columns: list[dict[str, Any]] | None = None
) -> None:
    for line in _table_lines(rows, output_columns or []):
        print(line)


def _table_lines(rows: list[dict[str, Any]], output_columns: list[dict[str, Any]]) -> list[str]:
    """Render result rows as an aligned text table for people.

    Numbers get thousands separators and consistent decimals per column and
    are right-aligned and never cut short. Long text is shortened with
    ``...``. JSON output keeps the raw values.
    """

    if not rows:
        return []
    columns: list[str] = []
    for row in rows:
        for key in row:
            if str(key) not in columns:
                columns.append(str(key))
    meta = {
        str(column.get("field", "")): column
        for column in output_columns
        if isinstance(column, dict) and column.get("field")
    }
    headers = _column_headers(columns, meta)
    cells: dict[str, list[str]] = {}
    numeric: dict[str, bool] = {}
    for column in columns:
        info = dict(meta.get(column, {}) or {})
        cells[column], numeric[column] = _format_column(
            [row.get(column) for row in rows],
            column_type=str(info.get("type", "") or ""),
            as_stored=_is_dimension_column(info),
        )
    widths = {
        column: max(len(headers[column]), *(len(cell) for cell in cells[column]))
        for column in columns
    }

    def fit(text: str, column: str) -> str:
        return text.rjust(widths[column]) if numeric[column] else text.ljust(widths[column])

    lines = [" | ".join(fit(headers[column], column) for column in columns).rstrip()]
    lines.append("-+-".join("-" * widths[column] for column in columns))
    for index in range(len(rows)):
        lines.append(" | ".join(fit(cells[column][index], column) for column in columns).rstrip())
    return lines


def _column_headers(columns: list[str], meta: dict[str, dict[str, Any]]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for column in columns:
        info = dict(meta.get(column, {}) or {})
        label = str(info.get("display_label") or column)
        if info.get("type") == "time" and "__" in column:
            label += f" ({column.rsplit('__', 1)[1]})"
        labels[column] = _shorten(_printable(label))
    shown = list(labels.values())
    headers: dict[str, str] = {}
    for column in columns:
        # A label two columns share falls back to the column's own field name.
        header = labels[column]
        if shown.count(header) > 1:
            header = _shorten(_printable(column))
        base, suffix = header, 2
        while header in headers.values():
            header, suffix = f"{base} #{suffix}", suffix + 1
        headers[column] = header
    return headers


def _is_dimension_column(info: dict[str, Any]) -> bool:
    """Group-by and time columns hold keys, codes and years: print them as stored."""

    semantic_id = str(info.get("semantic_id", "") or "")
    return semantic_id.startswith(("dimension.", "temporal_role.", "entity.")) or info.get(
        "type"
    ) in {"id", "time"}


def _format_column(
    values: list[Any], *, column_type: str, as_stored: bool = False
) -> tuple[list[str], bool]:
    present = [value for value in values if value is not None]
    if present and all(_is_number(value) for value in present):
        if as_stored:
            return [_format_scalar(value) for value in values], True
        decimals = _column_decimals(present, column_type=column_type)
        return [
            "NULL"
            if value is None
            else _format_number(value, decimals)
            if decimals is not None
            else _format_significant(value)
            for value in values
        ], True
    return [_shorten(_format_scalar(value)) for value in values], False


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def _is_finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Decimal):
        return value.is_finite()
    return True


def _is_integral(value: Any) -> bool:
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, Decimal):
        return value == value.to_integral_value()
    return True


def _column_decimals(values: list[Any], *, column_type: str) -> int | None:
    """Decimals for a numeric column; ``None`` when its values are too small for fixed decimals."""

    if column_type == "currency":
        return 2
    finite = [value for value in values if _is_finite(value)]
    if all(_is_integral(value) for value in finite):
        return 0
    # Only magnitudes below 1 need extra decimals; comparing (not converting)
    # keeps an int too large for a float from overflowing.
    fractions = [abs(value) for value in finite if value != 0 and abs(value) < 1]
    if not fractions:
        return 2
    # Keep about three significant digits on the smallest value, e.g. 0.00340.
    # Past six decimals, fixed notation would drop them (5.1e-7 as 0.000001),
    # so such a column prints significant digits instead.
    needed = 2 - _magnitude(min(fractions))
    return needed if needed <= 6 else None


def _magnitude(value: Any) -> int:
    """``floor(log10(value))`` for a positive number, never sending a ``Decimal`` through float."""

    if isinstance(value, Decimal):
        return value.adjusted()  # 1E-400 would be 0.0 as a float
    return math.floor(math.log10(value))


def _format_significant(value: Any) -> str:
    """A value in a column of very small numbers, with three significant digits below 1.

    Integers stay exact and values of 1 or more keep two decimals. Below 1, the
    digits are always shown (0.9996 is ``1.000``, never ``1``), in fixed
    notation down to 0.0001 and in scientific notation under that.
    """

    if not _is_finite(value):
        return _format_number(value, 2)  # nan, inf; checked before any comparison
    if _is_integral(value):
        return _format_number(value, 0)
    magnitude = _magnitude(abs(value))
    if magnitude >= 0:
        return _format_number(value, 2)
    if magnitude >= -4:
        return _format_number(value, 2 - magnitude)
    return format(value, ".2e")


def _format_number(value: Any, decimals: int) -> str:
    if not _is_finite(value):
        return str(value).lower()
    if isinstance(value, int):
        # Exact at any size: formatting an int with "f" goes through float.
        text = f"{value:,}" + ("." + "0" * decimals if decimals else "")
    else:
        text = f"{value:,.{decimals}f}"
    if not any(digit in text for digit in "123456789"):
        # Never show a nonzero value as zero; drop only the sign of a true zero.
        return f"{value:.3g}" if value else text.lstrip("-")
    return text


def _format_scalar(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True, default=str)
    return _printable(str(value))


# Characters that reorder how a terminal draws text around them.
_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _printable(text: str) -> str:
    """Keep a cell on one line, and never send raw control codes to the terminal.

    Only control characters and bidirectional overrides are escaped: other
    characters (no-break spaces, zero-width joiners, CJK spaces) print as
    stored, so a value shown here still matches when copied into a filter.
    """

    return "".join(
        " "
        if char in "\n\r\t\u2028\u2029"
        else char.encode("unicode_escape").decode("ascii")
        if unicodedata.category(char) == "Cc" or char in _BIDI_CONTROLS
        else char
        for char in text
    )


def _shorten(text: str, width: int = _MAX_CELL_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def _print_error_envelope(issue: dict[str, Any]) -> None:
    """Print the CLI error envelope to stdout (exactly once).

    The structured issue — code, ``details.closest_matches``, and
    ``recovery_hints`` — is emitted once under ``error``. Earlier
    versions also repeated the full issue under ``errors`` and hoisted
    ``recovery_hints`` to the top level, so every CLI failure printed
    the same payload three times. ``jq '.error.code'`` /
    ``jq '.error.recovery_hints'`` remain stable.
    """
    _print({"ok": False, "status": "error", "error": issue})
