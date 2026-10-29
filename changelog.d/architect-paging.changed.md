- The Architect MCP pages its long answers: on a warehouse of about 100 tables, answers that ran
  from 20,000 to 1.9 million characters now come in pages of about 16,000. `list_tables`,
  `describe_table`, `profile_columns`, `suggest_model`, `suggest_links`,
  `suggest_models_from_dbt` and `list_project_files` take `offset` and `limit` and return
  `total_count`, `has_more` and `next_offset`, as `valid-values` does; `list_tables` no longer
  stops at 200 tables, nor `suggest_models_from_dbt` at 20 models. Summaries (writes,
  `validate_project`, `project_status`, `promotion_check`, and now `diff_project` and
  `impact_project`) fit one page and name each list they cut short in `pages`; `page` reads that
  list. Every item stays reachable, and `full_report: true` still returns everything. A route
  pair, a release's blockers and the next actions are never cut. Pass a page's `revision` as
  `expected_revision` on the next page: a package that changed in between refuses with
  `CONFIG_CONFLICT` instead of skipping items. See [Paging](docs/ARCHITECT_MCP.md#paging).
- A write without `expected_revision` says which values it takes, and `suggest_links` names the
  entity whose model reads a relation the warehouse lacks, with `list_tables` as the next step.
