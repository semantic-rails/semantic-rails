- The REPL's `author calendar` writes the package calendar, so rolling, prior-period and
  growth metrics can be authored without editing YAML; the metric wizard offers the units
  of the calendar that queries fill from.
- In the REPL, `ls` accepts `--limit N` and `--json`, a bare `ls` of a large package counts
  objects by kind, and `help <command>` shows that command.
- `ask` and `run` print the engine's first recovery hint under each error.
- In an arrow-key list, typing `cancel` picks an option that contains it, such as
  "Cancelled orders", instead of ending the wizard.
