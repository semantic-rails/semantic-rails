"""Import-layer contract for ``semantic_rails``.

Each module belongs to one layer, and imports may only point to the same or a lower layer.
Imports inside functions and ``TYPE_CHECKING`` blocks count, because they couple modules as
much as top-level ones. The known exceptions and import cycles are listed below; both lists
may only shrink, so removing an exception or breaking a cycle means deleting its entry here.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2] / "semantic_rails"

# Highest layer first. Keys are module or package paths relative to ``semantic_rails``;
# the longest matching key wins, so ``contracts.generation`` sits above ``contracts``.
LAYERS = [
    "cli repl __main__ embedding mcp_manager local_config",
    "api asgi http_core http_request mcp mcp_server mcp_streamable_http architect_mcp"
    " contracts.generation",
    "architect_service architect_transactions architect_introspection architect_scaffold"
    " dbt_artifacts package_tools config_validation semantic_collisions contracts interop",
    "planner",
    "metadata metadata_parts catalog_service",
    "runtime runtime_parts manifest segments caveats resource_access policies cache"
    " request_context api_keys",
    "compiler compiler_parts fanout relation_pipelines renderer ir registry diagnostics"
    " acceleration",
    "db db_parts seed_provenance",
    "config config_parts package_snapshot yaml_loader operational meta_contract",
    "dialects sql_preparation sql_ast sql_identifiers",
    "ast expressions schema errors request_payload catalog_search scope",
]
LAYER_OF = {key: rank for rank, keys in enumerate(LAYERS) for key in keys.split()}
LAYER_OF[""] = len(LAYERS) - 1  # the package root only reads its version

# Upward imports that exist today, all inside functions. Remove each one, then its entry.
ALLOWED_UPWARD = {
    ("manifest", "metadata"),
    ("metadata", "config_validation"),
    ("resource_access", "metadata"),
    ("resource_access", "metadata_parts.capabilities"),
}

# Each entry is one strongly connected group of modules; break a cycle, then shrink its entry.
KNOWN_CYCLES = [
    "expressions schema",
    "config package_snapshot",
    "compiler compiler_parts.conversion compiler_parts.predicate compiler_parts.sql_lowering",
    "config_validation manifest metadata metadata_parts.capabilities metadata_parts.valid_values"
    " resource_access runtime",
]


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


def test_import_cycles_are_only_the_known_ones():
    graph = _import_graph()
    reach = {}
    for start in graph:
        seen, todo = set(), list(graph[start])
        while todo:
            module = todo.pop()
            if module not in seen:
                seen.add(module)
                todo.extend(graph[module])
        reach[start] = seen
    cycles = {frozenset(m for m in reach[s] if s in reach[m]) for s in graph if s in reach[s]}
    assert cycles == {frozenset(cycle.split()) for cycle in KNOWN_CYCLES}, "import cycles changed"
