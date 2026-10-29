"""Import-layer and embeddable-core contracts for ``semantic_rails``.

Each module belongs to one layer, and imports may only point to the same or a lower layer.
Imports inside functions and ``TYPE_CHECKING`` blocks count, because they couple modules as
much as top-level ones. The known exceptions and import cycles are listed below; both lists
may only shrink, so removing an exception or breaking a cycle means deleting its entry here.

The embeddable core (``semantic_rails.embedding``: load a package, compile, enforce policies)
imports neither DuckDB, the MCP SDK nor pydantic, and the separations below keep its
modules apart, through any chain of imports.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "semantic_rails"

# Highest layer first. Keys are module or package paths relative to ``semantic_rails``;
# the longest matching key wins, so ``contracts.generation`` sits above ``contracts``.
LAYERS = [
    "cli repl __main__ embedding mcp_manager local_config",
    "api asgi http_core http_request mcp mcp_query mcp_server mcp_session mcp_streamable_http architect_mcp"
    " contracts.generation",
    "architect_service architect_transactions architect_introspection architect_scaffold"
    " dbt_artifacts package_tools upgrade config_validation semantic_collisions contracts interop"
    " operations",
    "planner",
    "metadata metadata_parts catalog_service lookalike_models",
    "runtime runtime_parts runtime_parts.answer_warnings answer manifest segments caveats resource_access policies cache"
    " request_context api_keys audit visible_view",
    "compiler compiler_parts fanout route_census route_groups relation_pipelines renderer ir registry"
    " diagnostics path_gap acceleration top_n period_completeness row_filter_visibility",
    "db db_parts seed_provenance",
    "config config_parts package_snapshot yaml_loader operational meta_contract"
    " config_parts.aggregate_rows config_parts.loader_chain config_parts.measure_rows"
    " config_parts.metric_rows config_parts.model_rows config_parts.package_rows config_parts.policy_rows"
    " config_parts.row_values config_parts.temporal_rows",
    "dialects sql_preparation sql_ast sql_identifiers row_filters",
    "ast expressions schema errors issues request_payload catalog_format catalog_search scope"
    " temporal_support"
    " result_values policy_rules naming atomic_files operators clock query_call",
]
LAYER_OF = {key: rank for rank, keys in enumerate(LAYERS) for key in keys.split()}
LAYER_OF[""] = len(LAYERS) - 1  # the package root only reads its version

# Upward imports kept as exceptions: none; one that points up fails.
ALLOWED_UPWARD: set[tuple[str, str]] = set()

# Each entry is one strongly connected group of modules; break a cycle, then shrink its entry.
KNOWN_CYCLES = [
    "expressions schema",
    "config package_snapshot",
]

# The module on the left reaches no module of the package on the right.
SEPARATED = [
    # Request identity is read without the transports' API-key auth.
    ("request_context", "api_keys"),
    # The compiler's limited order needs no warehouse adapter.
    ("top_n", "db_parts"),
]
# Reaches kept on purpose, with the reason; an entry whose reach is gone fails, so delete it.
ALLOWED_REACH = {
    ("config_validation", "runtime"): (
        "package checks run the package through a Runtime: runtime validation, warehouse "
        "probes and the data values of filters"
    ),
    ("compiler", "row_filters"): (
        "the compiler writes each row filter's predicate into the SQL it lowers, and "
        "row_filters reads only errors, policy_rules, schema, sql_ast and sql_preparation"
    ),
}


def _modules() -> dict[str, Path]:
    modules = {}
    for path in PACKAGE.rglob("*.py"):
        parts = path.relative_to(PACKAGE).with_suffix("").parts
        modules[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = path
    return modules


def _import_graph() -> dict[str, set[str]]:
    """Map each module to the ``semantic_rails`` modules it imports anywhere in its body."""
    modules = _modules()
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = [node.module] if node.module else []
                if node.level:
                    parents = ["semantic_rails", *filter(None, package.split("."))]
                    base = parents[: len(parents) - node.level + 1] + base
                targets = [".".join([*base, alias.name]) for alias in node.names]
            else:
                continue
            for target in targets:
                if target.partition(".")[0] != "semantic_rails":
                    continue
                module = target.partition(".")[2]  # "" is the package root
                while module not in modules:  # `from x import name` names a symbol
                    module = module.rpartition(".")[0]
                if module != name:
                    graph[name].add(module)
    return graph


def _layer(module: str) -> int:
    key = module
    while key not in LAYER_OF:
        if "." not in key:
            raise AssertionError(f"semantic_rails.{module} has no layer; add it to LAYERS")
        key = key.rpartition(".")[0]
    return LAYER_OF[key]


def test_imports_point_down_the_layers():
    graph = _import_graph()
    rank = {module: _layer(module) for module in graph}
    upward = {(src, dst) for src, dsts in graph.items() for dst in dsts if rank[dst] < rank[src]}
    assert upward - ALLOWED_UPWARD == set(), "these imports point to a higher layer"
    assert ALLOWED_UPWARD - upward == set(), "these exceptions are gone; delete them"


def _reach(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    """Map each module to every module it imports, directly or through others."""
    reach = {}
    for start in graph:
        seen, todo = set(), list(graph[start])
        while todo:
            module = todo.pop()
            if module not in seen:
                seen.add(module)
                todo.extend(graph[module])
        reach[start] = seen
    return reach


def test_import_cycles_are_only_the_known_ones():
    graph = _import_graph()
    reach = _reach(graph)
    cycles = {frozenset(m for m in reach[s] if s in reach[m]) for s in graph if s in reach[s]}
    assert cycles == {frozenset(cycle.split()) for cycle in KNOWN_CYCLES}, "import cycles changed"


def test_separated_modules_stay_apart():
    reach = _reach(_import_graph())

    def reaches(source: str, target: str) -> bool:
        return any(m == target or m.startswith(f"{target}.") for m in reach[source])

    assert [pair for pair in SEPARATED if reaches(*pair)] == [], "these modules now reach"
    assert [pair for pair in ALLOWED_REACH if not reaches(*pair)] == [], (
        "these allowed reaches are gone; delete them"
    )


def test_the_embedding_core_imports_no_duckdb_mcp_or_pydantic():
    # A fresh, isolated interpreter: the test session's DuckDB startup hook stays out of it.
    script = PACKAGE.parent / "scripts" / "verify_embedding_core.py"
    result = subprocess.run(
        [sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr or result.stdout
