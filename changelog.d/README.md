# Changelog fragments

A pull request with a user-facing change adds one fragment here instead of editing
`CHANGELOG.md`, so parallel pull requests do not conflict on it. `check` fails if anything but
the placeholder line sits under `## Unreleased` in `CHANGELOG.md`.

- Name: `<slug>.<category>.md`, where the slug is short lowercase kebab-case and may start
  with the pull request number, for example `142-duckdb-timeouts.fixed.md`.
- Category: `added`, `changed`, `deprecated`, `removed`, `fixed`, or `security`, in
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) order.
- Body: one or more `- ` bullets wrapped like `CHANGELOG.md`, with continuation lines
  indented two spaces. No blank lines and no headings (the release adds them). UTF-8, ending
  with a newline.
- Only this README and fragments live here: dotfiles, editor backups and subdirectories fail
  `check`.

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
`### Added`, `### Changed`, … subsections in the order above, then deletes the folded
fragments. Bullets keep file-name order compared as text (`142-…` sorts before `99-…`), so
zero-pad a numeric prefix if the order matters.
