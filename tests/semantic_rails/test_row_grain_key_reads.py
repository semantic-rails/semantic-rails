"""Every read of an entity's key, pinned by file and function.

A row-grain model has no key: ``EntityConfig.key`` raises ``ROW_GRAIN_UNSUPPORTED`` for it, so
a read of ``.key`` or ``.primary_key`` refuses unless someone deliberately switched it to
``key_columns`` (or ``is_row_grain``), where an empty key is a legitimate answer. A new read in
either list, or one that moves between them, fails here until the lists are reviewed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "semantic_rails"
# Their `.key` reads are not on an entity: authored YAML, dbt manifests, upgrade rules.
EXCLUDED = ("architect_", "dbt_artifacts.py", "upgrade/", "repl/")

# Reads where an empty key is a legitimate "no": they read `key_columns` or `is_row_grain`.
SWITCHED = {
    ("cli/commands/project.py", "cmd_project_validate"),
    ("compiler_parts/bind.py", "_measure_count_distinct_key_columns"),
    ("compiler_parts/bind.py", "_synthetic_conditional_measure"),
    ("compiler_parts/indexes.py", "_measure_has_source_row_key"),
    # A distinct count across links needs no row key; every other aggregation reads `.key`.
    ("compiler_parts/leaves.py", "_fanout_dedup_leaf_select"),
    # A listing's start row is told apart by its key; a row-grain start by its own columns.
    ("compiler_parts/listings.py", "listing_rows"),
    ("compiler_parts/lowering_common.py", "_entity_in_terms_of_anchor_plan"),
    ("compiler_parts/paths.py", "_direct_dimension_source_expr"),
    ("compiler_parts/paths.py", "display_key"),
    ("compiler_parts/paths.py", "keyed_entity"),
    ("compiler_parts/plan_checks.py", "_dedup_refusal"),
    ("compiler_parts/plan_checks.py", "child_group_route"),
    # Also the keyed sum of a non-additive measure: a row-grain model's refuses with its own fix.
    ("compiler_parts/plan_checks.py", "_validate_non_additive_sums"),
    ("compiler_parts/post_aggregation.py", "_summing_window_parts.additive"),
    ("compiler_parts/row_counts.py", "_names_in_use"),
    # Also a join's source key: a row-grain source holds no key, so its side reads "foreign".
    ("config_parts/package_rows.py", "_parse_package"),
    # A one-per-key model has a key (loading refuses one without); a link's source has none.
    ("config_parts/one_per_key.py", "_columns_read"),
    ("config_parts/one_per_key.py", "one_per_key_relation"),
    ("config_parts/one_per_key.py", "with_one_per_key.link"),
    ("contracts/producer.py", "_resources_from_normalized"),
    # Refusal context only: a row-grain target is named.
    ("diagnostics.py", "enrich_path_not_found"),
    ("fanout.py", "pass_through_disclosure"),
    # Refusal context only: a row-grain target gets no missing link, and holds no key to match.
    ("path_gap.py", "_Columns.__init__"),
    ("path_gap.py", "path_gap"),
    ("interop/ossie/export.py", "_Exporter.datasets"),
    ("interop/package_writer.py", "_Writer.dimensions"),
    ("interop/package_writer.py", "_Writer.graph"),
    ("interop/package_writer.py", "_Writer.model"),
    ("metadata.py", "_object_card"),
    ("metadata.py", "discover_payload"),
    ("package_tools.py", "check_warehouse_column_reachability_report"),
    ("planner/groupings.py", "_entity_stand_ins"),
    ("planner/unmatched_words.py", "_grouped_entities"),
    ("runtime.py", "_named_key_refusal"),
    ("runtime.py", "_unknown_rows_kept_warnings"),
    ("runtime_parts/answer_warnings.py", "_history_result_warnings"),
    ("schema.py", "EntityConfig.is_row_grain"),
    ("schema.py", "EntityConfig.key"),
}
# Reads that need a key by meaning, or that a row-grain model can't reach: they refuse.
REFUSES = {
    ("compiler_parts/conversion.py", "_conversion_entity_key_fields"),
    ("compiler_parts/conversion.py", "_conversion_event_cte"),
    ("compiler_parts/conversion.py", "_conversion_operand_problem"),
    ("compiler_parts/conversion.py", "_looked_up_property_keys"),
    ("compiler_parts/conversion.py", "_match_key_route"),
    ("compiler_parts/conversion.py", "_measure_conversion_source"),
    ("compiler_parts/indexes.py", "_measure_has_source_row_key"),
    ("compiler_parts/leaves.py", "_fanout_dedup_leaf_select"),
    ("compiler_parts/leaves.py", "_parent_lookup_leaf_select"),
    ("compiler_parts/lowering_common.py", "_stock_row_key"),
    ("compiler_parts/paths.py", "_direct_entity_key_source_expr"),
    ("compiler_parts/paths.py", "_entity_key_dimension_ids"),
    ("compiler_parts/plan_checks.py", "_can_project_entity_key_from_source"),
    ("compiler_parts/plan_checks.py", "_validate_non_additive_sums"),
    ("compiler_parts/predicate.py", "_predicate_ctes_and_join"),
    ("compiler_parts/predicate_sets.py", "_anchored_entity_set_plan"),
    ("compiler_parts/predicate_sets.py", "_snapshot_select_fields"),
    ("compiler_parts/temporal.py", "_clock_where_cuts"),
    ("config_parts/lookup_measures.py", "_one_link"),
    ("config_parts/lookup_measures.py", "resolve_lookup_measures"),
    ("config_parts/model_rows.py", "_with_displays"),
    # Also a join's target key, which decides an inferred cardinality; a row-grain target is
    # refused before it is read.
    ("config_parts/package_rows.py", "_parse_package"),
    ("planner/answer_shape.py", "_lists_entity_rows.keys_of"),
    ("schema.py", "EntityConfig.primary_key"),
    ("segments.py", "member_key_dimensions"),
}
# `.key` or `.primary_key` on something else: CLI arguments and visibility keys.
NOT_AN_ENTITY = {
    ("cli/commands/project.py", "cmd_init_project"),
    ("cli/commands/project.py", "cmd_project_new"),
    # ParentHop.key is the join's single owner column, not EntityConfig.key.
    ("row_filters.py", "Visibility._predicate"),
    ("visible_view.py", "request_view"),
}
# Reads and projections of `key_columns` by field name (a string), not by attribute: each
# must keep every key column, whatever the caller may see.
BY_NAME = {
    ("interop/ossie/export.py", "_Exporter.datasets"),
    ("interop/ossie/reader.py", "_Importer.read_datasets"),
    ("interop/ossie/reader.py", "_Importer.read_relationships"),
    ("interop/package_writer.py", "_Writer.graph"),
    ("runtime_parts/answer_warnings.py", "_history_result_warnings"),
    ("visible_view.py", "<module>"),
}
_SWITCHED_ATTRS = {"key_columns", "is_row_grain"}
_KEY_ATTRS = {"key", "primary_key"}


def _reads(path: Path) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    relative = str(path.relative_to(ROOT))

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            attr = ""
            if isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                attr = child.attr
            elif (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "getattr"
                and len(child.args) >= 2
                and isinstance(child.args[1], ast.Constant)
            ):
                attr = str(child.args[1].value)
            if attr in _SWITCHED_ATTRS | _KEY_ATTRS:
                found.add((relative, scope or "<module>", attr))
            if isinstance(child, ast.Constant) and re.search(r"\bkey_columns\b", str(child.value)):
                found.add((relative, scope or "<module>", "by_name"))
            visit(child, inner)

    visit(ast.parse(path.read_text(encoding="utf-8")), "")
    return found


def _inventory() -> set[tuple[str, str, str]]:
    reads: set[tuple[str, str, str]] = set()
    for path in sorted(ROOT.rglob("*.py")):
        if not str(path.relative_to(ROOT)).startswith(EXCLUDED):
            reads |= _reads(path)
    return reads


def test_every_key_read_is_switched_or_refuses() -> None:
    reads = _inventory()
    switched = {(path, scope) for path, scope, attr in reads if attr in _SWITCHED_ATTRS}
    keyed = {(path, scope) for path, scope, attr in reads if attr in _KEY_ATTRS}
    assert switched == SWITCHED
    assert keyed == REFUSES | NOT_AN_ENTITY
    assert {(path, scope) for path, scope, attr in reads if attr == "by_name"} == BY_NAME


def test_the_visible_view_keeps_every_key_column() -> None:
    """A hidden dimension named like a key column never drops it from the key."""
    from semantic_rails.schema import EntityConfig
    from semantic_rails.visible_view import ROW_FIELDS

    assert ROW_FIELDS[EntityConfig]["key_columns"] == "kept"
