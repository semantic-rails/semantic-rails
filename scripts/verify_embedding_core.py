#!/usr/bin/env python3
"""Compile the bundled package with its policies through the embedding facade alone.

A host that embeds the engine to load a package, compile calls and enforce policies imports
neither DuckDB, the MCP Python SDK nor pydantic: DuckDB loads at the first DuckDB connection,
and the MCP SDK and pydantic belong to the Architect MCP (the `architect` extra). CI runs this
script in an environment without them (`--absent`); the import contract tests run it beside
them, where none may load.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

LEAN = ("duckdb", "mcp", "pydantic")
PACKAGE = Path(__file__).resolve().parents[1] / "configs" / "semantic_rails" / "jaffle_shop"


def _denying_policies(runtime, call, context) -> set[str]:
    from semantic_rails.embedding import SemanticLayerError

    try:
        runtime.compile(call, context=context)
    except SemanticLayerError as exc:
        if exc.code != "POLICY_DENIED":
            raise
        return {effect["policy_id"] for effect in exc.details.get("policy_effects", [])}
    return set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--package", default=str(PACKAGE), help="package directory")
    parser.add_argument("--absent", action="store_true", help="require that none is installed")
    args = parser.parse_args(argv)
    installed = [name for name in LEAN if importlib.util.find_spec(name) is not None]
    if args.absent and installed:
        raise SystemExit(f"installed, but this check needs them absent: {installed}")

    from semantic_rails.embedding import RequestContext, Runtime

    runtime = Runtime.from_path(args.package)
    by_store = {
        "select": ["measure.jaffle.revenue_usd"],
        "group_by": ["dimension.jaffle_store_name"],
    }
    sales = RequestContext(roles=("sales",))
    compiled = runtime.compile(by_store, context=sales)
    applied = {effect["policy_id"] for effect in compiled["policy_effects"]}
    if compiled["status"] != "ok" or "policy.jaffle.gtm_revenue_store_cuts" not in applied:
        raise SystemExit(f"the sales store-level cut did not compile under its policy: {applied}")
    by_customer = {**by_store, "group_by": ["dimension.jaffle_customer_name"]}
    for call, context, policy in (
        (by_store, RequestContext(audience="external_partner"), "redact_revenue_for_external"),
        (by_customer, sales, "gtm_revenue_store_cuts"),
    ):
        if f"policy.jaffle.{policy}" not in _denying_policies(runtime, call, context):
            raise SystemExit(f"policy.jaffle.{policy} did not deny {call} for {context}")
    loaded = sorted({name.partition(".")[0] for name in sys.modules} & set(LEAN))
    if loaded:
        raise SystemExit(f"loading, compiling and enforcing policies imported {loaded}")
    print(f"Compiled {Path(args.package).name} with its policies; imported none of {list(LEAN)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
