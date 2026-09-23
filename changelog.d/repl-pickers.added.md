- `pip install 'semantic-rails[repl]'` gives the REPL's authoring wizards arrow-key pickers
  with type-to-filter, checkboxes and highlighted YAML previews. Without the extra, or when
  stdin and stdout aren't a terminal, the REPL keeps its plain line prompts;
  `SEMANTIC_RAILS_UI=plain` forces them (for example with a screen reader) and
  `SEMANTIC_RAILS_UI=pickers` insists on pickers.
