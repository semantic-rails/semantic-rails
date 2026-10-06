"""Closed, reported spelling normalization at the MCP query boundary."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from .errors import SemanticLayerError
from .expressions import validate_expression_shapes

_COMPARISONS = {
    "eq": "=",
    "equals": "=",
    "neq": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "is_not_null": "IS NOT NULL",
}
_ARITHMETIC = {"add": "add", "sub": "subtract", "mul": "multiply", "div": "divide"}


def _invalid(message: str, path: str, matches: list[str]) -> SemanticLayerError:
    return SemanticLayerError(
        "INVALID_QUERY", message, details={"path": path, "closest_matches": matches}
    )


def normalize_arguments(
    arguments: Mapping[str, Any], known: frozenset[str]
) -> tuple[dict[str, Any], list[str]]:
    """Decode before trusted-context injection so encoded claims cannot survive it."""
    out = dict(arguments)
    notes: list[str] = []
    query = out.get("query")
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except (ValueError, RecursionError) as exc:
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "query must encode a JSON object.",
                details={"field": "query", "argument_type": "str"},
            ) from exc
        if not isinstance(query, dict):
            raise SemanticLayerError(
                "INVALID_MCP_ARGUMENTS",
                "query must encode a JSON object.",
                details={"field": "query", "argument_type": "str"},
            )
        notes.append("query: parsed JSON string")
    if isinstance(query, Mapping):
        query = dict(query)
        for key in sorted(known - {"query", "policy_context"}):
            if key not in query:
                continue
            # These Query IR options already have documented inner precedence.
            if key in out and key in {"verbosity", "sql_profile"}:
                continue
            if key in out and out[key] != query[key]:
                raise _invalid(f"Conflicting outer and nested {key}.", f"query.{key}", [key])
            out[key] = query.pop(key)
            notes.append(f"query.{key}: lifted to tool arguments")
        out["query"] = query
    return out, notes


def normalize_query_spellings(query: dict[str, Any], notes: list[str]) -> dict[str, Any]:
    def walk(value: Any, path: str, depth: int = 0) -> Any:
        if depth > 128:
            raise _invalid("Query expression nesting is too deep.", path, [])
        if isinstance(value, list):
            return [walk(row, f"{path}[{idx}]", depth + 1) for idx, row in enumerate(value)]
        if not isinstance(value, dict):
            return value
        if "kind" in value and value["kind"] not in ("dimension", "group", "ref"):
            # Use the parser's closed kind list, rejecting before visiting children.
            validate_expression_shapes({"kind": value["kind"]}, path=path)
        if value.get("kind") == "literal":
            return dict(value)
        row = {key: walk(item, f"{path}.{key}", depth + 1) for key, item in value.items()}
        op = row.get("op")
        kind = row.get("kind")
        if isinstance(op, str):
            aliases = _ARITHMETIC if kind in ("arithmetic", "binary") else _COMPARISONS
            if op in aliases and op != aliases[op]:
                row["op"] = aliases[op]
                notes.append(f"{path}.op: {op} -> {aliases[op]}")
        if kind in ("arithmetic", "binary") and ("operands" in row or "terms" in row):
            keys = set(row) & {"operands", "terms", "left", "right"}
            if len(keys) != 1:
                raise _invalid("Arithmetic needs one operand shape.", path, ["left", "right"])
            key = next(iter(keys))
            operands = row.pop(key)
            if not isinstance(operands, list) or len(operands) < 2:
                raise _invalid("Arithmetic needs at least two operands.", path, ["left", "right"])
            left = operands[0]
            for right in operands[1:-1]:
                left = {"kind": "arithmetic", "op": row.get("op"), "left": left, "right": right}
            row.update(left=left, right=operands[-1])
            notes.append(f"{path}.{key}: folded left")
        return row

    out = dict(query)
    for key in ("where", "metric_filters"):
        if isinstance(out.get(key), list):
            # Predicate values are caller data, not expression nodes.
            def predicates(rows: list[Any], path: str, depth: int = 0) -> list[Any]:
                if depth > 128:
                    raise _invalid("Query predicate nesting is too deep.", path, [])
                result = []
                for idx, item in enumerate(rows):
                    if not isinstance(item, dict):
                        result.append(item)
                        continue
                    row = dict(item)
                    prefix = f"{path}[{idx}]"
                    op = row.get("op")
                    if isinstance(op, str) and op in _COMPARISONS:
                        row["op"] = _COMPARISONS[op]
                        notes.append(f"{prefix}.op: {op} -> {row['op']}")
                    if "expression" in row:
                        row["expression"] = walk(
                            row["expression"], f"{prefix}.expression", depth + 1
                        )
                    if isinstance(row.get("where"), list):
                        row["where"] = predicates(row["where"], f"{prefix}.where", depth + 1)
                    result.append(row)
                return result

            out[key] = predicates(out[key], f"query.{key}")
    if isinstance(out.get("select"), list):
        out["select"] = [
            walk(item, f"query.select[{idx}]") for idx, item in enumerate(out["select"])
        ]
    return out


def normalize_routes(
    query: dict[str, Any], notes: list[str], validate: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """An id answers only the current refusal; decisions still pass the runtime guard."""
    raw = query.get("route_decisions")
    if not isinstance(raw, list):
        return query
    rows = []
    ids = []
    for idx, item in enumerate(raw):
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and "decision" in item:
            decision = item["decision"]
            if (
                set(item) - {"id", "meaning", "relationship_path", "decision", "conflicts_with"}
                or not isinstance(decision, dict)
                or (
                    "relationship_path" in item
                    and item["relationship_path"] != decision.get("relationship_path")
                )
            ):
                raise _invalid(
                    "Conflicting or malformed route option.",
                    f"query.route_decisions[{idx}]",
                    ["decision"],
                )
            rows.append(decision)
            notes.append(f"query.route_decisions[{idx}]: used option decision")
        else:
            rows.append(item)
    out = {**query, "route_decisions": rows}
    while ids:
        refusal = validate({**out, "verbosity": "full"})
        errors = refusal.get("errors", [])
        first = errors[0] if errors else {}
        options = (
            first.get("details", {}).get("clarification", {}).get("options", [])
            if first.get("code") == "AMBIGUOUS_PATH"
            else []
        )
        matches = [(name, option) for name in ids for option in options if option.get("id") == name]
        if len(matches) != 1:
            raise _invalid(
                "Route option id must uniquely answer the current clarification.",
                "query.route_decisions",
                [option["id"] for option in options],
            )
        name, option = matches[0]
        rows.append(option["decision"])
        ids.remove(name)
        notes.append(f"query.route_decisions: resolved option id {name}")
    return out
