- `semantic-rails project upgrade` rewrites a package written for an earlier release to the
  current forms in one change, and the Architect tool `upgrade_project` does the same. It
  previews the rules, a proof line and the diff by default; `--write` writes every file in one
  parse-gated transaction, keeping comments. Each rewrite is `proven` (the package's semantic
  fingerprint and its example and test SQL are unchanged on this engine) or `certified` (this
  engine refuses the legacy form); a rewrite that changes an answer is refused with
  `upgrade_not_equivalent`. The rules delete the declarations 0.3.2rc3 retired and move queries
  from `version: 2` to `version: 1`. A rule that needs a decision stops with exit code 2 until
  `--choose KEY=OPTION` answers it. `project validate`, `project_status` and Architect writes
  that fail to parse say when upgrade rules match. See
  [Upgrading a package](docs/PACKAGE_AUTHORING.md#upgrading-a-package).
