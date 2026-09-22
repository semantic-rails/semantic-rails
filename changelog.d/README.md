# Changelog fragments

A pull request with a user-facing change adds one fragment here instead of editing
`CHANGELOG.md`, so parallel pull requests do not conflict on it.

- Name: `<slug>.<category>.md`, where the slug is short lowercase kebab-case and may start
  with the pull request number, for example `142-duckdb-timeouts.fixed.md`.
- Category: `added`, `changed`, `deprecated`, `removed`, `fixed`, or `security`, in
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) order.
- Body: one or more `- ` bullets wrapped like `CHANGELOG.md`, with continuation lines
  indented two spaces. No headings (the release adds them), UTF-8, ending with a newline.

Entries are user-facing: say what changed for people using Semantic Rails, not how it was
implemented. Write paths and links as they will read from `CHANGELOG.md`. `security`
fragments use neutral wording (what changed, not how to exploit it) until any advisory is public.

```bash
uv run python scripts/changelog_fragments.py check    # validate fragments (CI runs this)
uv run python scripts/changelog_fragments.py preview  # print the pending Unreleased section
uv run python scripts/changelog_fragments.py release --version 0.3.0 --date 2026-10-01 \
  --title "Short release theme"
```

`release` inserts `## 0.3.0 — 2026-10-01 — Short release theme` below `## Unreleased`, with
`### Added`, `### Changed`, … subsections in the order above and bullets sorted by file
name, then deletes the folded fragments. This README stays.
