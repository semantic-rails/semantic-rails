- `import_dbt_project` no longer fails with a duplicate measure id when two dbt models share a
  measure column, such as an order fact and its line fact: the later model's measure gets its
  entity as a prefix (`order_line_usd_to_local_rate`), and re-importing keeps the keys.
