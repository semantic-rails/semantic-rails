- `mcp setup` and `mcp client-config` run through `uvx` now write client configs that
  start the server with `uv tool run --from <the same install>`. They used to name the
  Python inside uv's cache, so the client stopped starting the server after
  `uv cache prune` or `uv cache clean`. The requirement keeps the version (or source)
  and any installed extras. `mcp status` lists launch commands the same way, so they
  work when `semantic-rails` isn't on `PATH`. Installed commands are unchanged.
