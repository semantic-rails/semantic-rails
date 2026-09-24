- `--base-ref` comparisons (`check`, `diff-package`, `impact-report` and
  `promote-package`, and `base_ref` in the Architect MCP's `diff_project`,
  `impact_project` and `promotion_check`) resolve the ref in the git
  repository that holds the package, so they work for packages outside the
  engine's own checkout. The extracted baseline no longer stays behind in a
  temporary directory, only regular files with plain names are extracted, and
  a ref that looks like an option is refused. For a `base_ref` comparison the
  report's `comparison.source_path` now reads `<ref>@<commit>:<path>` instead
  of naming the temporary directory. Repository and package paths retain
  valid trailing whitespace.
