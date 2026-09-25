- In the REPL's arrow-key prompts, typing replaces a suggested default instead of appending to
  it. Typing `cancel` and Enter now cancels at a text prompt or a list. At a yes/no question,
  `y` or `n` waits for Enter, so that Enter no longer answers the next question.
- `run` and `ask` say why a planned query cannot run instead of stopping with no result and no
  error. For example, a growth metric by month needs a calendar with a `month_start` column.
- The metric wizard offers rolling windows, prior periods and growth only in the units the
  package calendar can fill, and names the calendar columns the other units need.
- A similar-name warning now comes right after the key and label, and choosing different
  wording asks for them again. Declining to update an existing object also asks for another
  key. Both used to end the wizard.
