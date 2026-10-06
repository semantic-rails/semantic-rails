- Declare all physical rollups under model `variants:`, including fact-model and
  pre-joined dimension rollups. The loader refuses top-level `aggregate_relations:`,
  variant time aliases, string grains, filtered rollups, and unused variant options.
- Routing rejects unsafe declared pre-join paths even for foreign-key columns,
  preserving base-table totals when a rollup join would repeat fact rows.
